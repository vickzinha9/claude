"""Local simulation of PocketAutonomo cycles: python3 -m pocketautonomo.local_simulation.

SIMULATION ONLY: every order goes to a SimulatedBroker in memory, never to a real broker.
The WIN and LOSS results come from --results, used in order and repeated when the list
ends. Nothing here analyzes the market, predicts a result or computes profitability.

The cycles run through the existing engine. The Coordinator, with its SQLite store,
decides the cycle, the attempt and the stake: 500, 1000, 2000 and 4000 cents, four
attempts at most, with the same asset and direction within a cycle; only a LOSS moves to
the next attempt. OrderExecutor sends each entry and reconciles its result. Each entry is
reserved with a fictitious balance of 10000 cents, only to let the reservation through;
it is not a real balance. After the broker accepts an order, the program checks that it
is still OPEN, waits --poll-interval seconds, settles the simulated order with the next
result and reconciles it. A new cycle starts only after the previous one ended.

Without --db, each run creates a new database with a unique name in runtime/, inside the
current directory, and keeps the earlier ones. An existing database is reused only when
none of its cycles is open. A cycle restored still open, with or without a pending
reservation, is refused and the database is left untouched: the program never resends an
order, invents a result or abandons a restored cycle, and a new SimulatedBroker does not
know the SIM-... orders of an earlier run, which lived only in its memory.

On an uncertain answer, a storage failure or any refusal of the coordinator, the program
stops with a clear message and keeps the database. Ctrl+C stops it as well: a pending
reservation is kept as it is, and the database and its lock are released.

Exit codes: 0 when the requested cycles ended, 1 when the run was refused or stopped,
2 for invalid arguments and 130 after Ctrl+C.
"""

import argparse
import itertools
import math
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pocketautonomo.coordinator import Coordinator, CoordinatorError
from pocketautonomo.cycle import CALL, LOSS, PUT
from pocketautonomo.executor import (
    ACCEPTANCE_RECORDED,
    PENDING,
    RESULT_RECORDED,
    OrderExecutor,
    ReconciliationReport,
)
from pocketautonomo.simulated_broker import SETTLEMENT_RESULTS, SimulatedBroker
from pocketautonomo.storage import SqliteStore, StorageError

FICTITIOUS_BALANCE_CENTS = 10_000  # only lets the reservations through; not a real balance
DEFAULT_ASSET = "SIM_ASSET"
DEFAULT_POLL_INTERVAL = 0.25
RUNTIME_DIR = Path("runtime")  # relative to the current directory

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_INTERRUPTED = 130


class SimulationStopped(Exception):
    """The run cannot go on safely; the message tells why. The database is kept."""


@dataclass
class Progress:
    """What this run did so far."""

    cycles_ended: int = 0
    orders: int = 0


def main(argv=None, *, sleep=time.sleep, broker=None):
    """Run the local simulation and return its exit code.

    sleep and broker exist for tests: sleep replaces time.sleep, and broker replaces the
    new SimulatedBroker of the run.
    """
    args = build_parser().parse_args(argv)
    progress = Progress()
    coordinator = None
    path = None
    try:
        path = database_path(args.db)
        print_header(args, path)
        coordinator = open_coordinator(path)
        check_restored_state(coordinator, path)
        coordinator.resume()
        run_cycles(coordinator, SimulatedBroker() if broker is None else broker, args, sleep, progress)
        say(
            f"Concluído: {count(progress.cycles_ended, 'ciclo encerrado', 'ciclos encerrados')}"
            f" e {count(progress.orders, 'ordem simulada', 'ordens simuladas')} nesta execução."
        )
        say(f"Banco preservado: {path}")
        return EXIT_OK
    except (SimulationStopped, StorageError, CoordinatorError) as error:
        detail = error if isinstance(error, SimulationStopped) else describe_error(error)
        warn(f"PARADA: {detail}")
        if path is not None and path.exists():
            warn(f"Banco preservado: {path}")
        return EXIT_STOPPED
    except KeyboardInterrupt:
        report_interruption(coordinator, progress, path)
        return EXIT_INTERRUPTED
    finally:
        if coordinator is not None:
            coordinator.close()  # also releases the database lock


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.local_simulation",
        description=(
            "SIMULAÇÃO local de ciclos do PocketAutonomo, sem corretora real. Os resultados"
            " WIN/LOSS vêm de --results; não há análise de mercado nem cálculo de"
            " lucratividade."
        ),
    )
    duration = parser.add_mutually_exclusive_group(required=True)
    duration.add_argument("--cycles", type=positive_int, help="quantos ciclos completos simular")
    duration.add_argument("--forever", action="store_true", help="simular até Ctrl+C")
    parser.add_argument(
        "--results",
        type=result_list,
        required=True,
        help="WIN/LOSS separados por vírgula, usados em ordem e repetidos ao terminar,"
        " por exemplo LOSS,LOSS,WIN",
    )
    parser.add_argument(
        "--asset",
        type=asset_name,
        default=DEFAULT_ASSET,
        help=f"ativo de todos os ciclos (padrão {DEFAULT_ASSET})",
    )
    parser.add_argument(
        "--direction",
        choices=(CALL, PUT),
        default=CALL,
        help=f"direção de todos os ciclos (padrão {CALL})",
    )
    parser.add_argument(
        "--poll-interval",
        type=positive_seconds,
        default=DEFAULT_POLL_INTERVAL,
        help="segundos de espera entre a consulta da ordem OPEN e sua liquidação"
        f" (padrão {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--db",
        help="banco SQLite a usar; sem ele, cria um banco novo com nome único em runtime/",
    )
    return parser


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"precisa ser um inteiro positivo, não {text!r}")
    return value


def positive_seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(
            f"precisa ser um número positivo e finito de segundos, não {text!r}"
        )
    return value


def result_list(text):
    results = tuple(item.strip() for item in text.split(","))
    if any(result not in SETTLEMENT_RESULTS for result in results):
        raise argparse.ArgumentTypeError(
            f"use só WIN ou LOSS separados por vírgula, como LOSS,LOSS,WIN; não {text!r}"
        )
    return results


def asset_name(text):
    if not text.strip():
        raise argparse.ArgumentTypeError("o ativo não pode ficar vazio")
    return text


def database_path(db):
    """The database given with --db, or a new unique one in RUNTIME_DIR."""
    if db is not None:
        return Path(db)
    try:
        return default_database_path(RUNTIME_DIR)
    except OSError as error:
        raise SimulationStopped(f"não foi possível criar {RUNTIME_DIR}/: {error}") from error


def default_database_path(runtime_dir):
    """A new database path with a unique name in runtime_dir, which is created if needed."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        name = f"simulation-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.db"
        path = runtime_dir / name
        if not path.exists():
            return path


def open_coordinator(path):
    try:
        return Coordinator(SqliteStore(path))
    except StorageError as error:  # also DatabaseInUseError and a refused restore
        raise SimulationStopped(f"o banco {path} não pôde ser aberto: {error}") from error


def check_restored_state(coordinator, path):
    """Refuse a database whose last cycle is still open; the database stays untouched."""
    status = coordinator.status()
    if status.recovery_pending or status.pending is not None:
        pending = status.pending
        if pending is None:
            where = f"sem reserva pendente; a próxima seria a tentativa {status.attempt}"
        else:
            where = (
                f"reserva pendente {pending.reservation_id} da tentativa {pending.attempt},"
                f" envio {status.submission_state}, ordem {status.broker_order_id or 'sem id'}"
            )
        raise SimulationStopped(
            f"o banco {path} tem o ciclo {status.cycle_id} aberto desde uma execução"
            f" anterior ({where}).\nA simulação não reenvia ordens, não inventa resultados"
            " e não abandona ciclos restaurados; uma corretora simulada nova também não"
            " conhece as ordens SIM-... de execuções anteriores.\nO banco não foi"
            " alterado. Para uma nova simulação, rode sem --db ou use outro arquivo."
        )
    if status.cycle_id is not None:
        say(
            f"Banco existente: ciclos encerrados até o ciclo {status.cycle_id};"
            " a numeração continua."
        )


def run_cycles(coordinator, broker, args, sleep, progress):
    """Run cycles until args.cycles of them ended, or until Ctrl+C when it is None."""
    executor = OrderExecutor(coordinator, broker)
    next_result = itertools.cycle(args.results).__next__
    while args.cycles is None or progress.cycles_ended < args.cycles:
        cycle_id = coordinator.start_cycle(args.asset, args.direction)
        while True:
            reservation = coordinator.reserve_next_entry(FICTITIOUS_BALANCE_CENTS)
            if reservation is None:
                raise SimulationStopped(f"o ciclo {cycle_id} não liberou a próxima entrada")
            order_id = send(executor, reservation)
            progress.orders += 1
            confirm_open(executor, reservation, order_id)
            say(order_line(reservation, order_id, "estado OPEN"))
            sleep(args.poll_interval)
            result = next_result()
            settle(broker, order_id, result)
            record(executor, reservation, order_id, result)
            say(order_line(reservation, order_id, f"resultado {result}"))
            status = coordinator.status()
            if not status.is_active:  # WIN, or the fourth LOSS
                say(cycle_end_line(status))
                break
        progress.cycles_ended += 1


def send(executor, reservation):
    """Send the reservation once through the executor; return the accepted order id."""
    report = executor.submit(reservation.reservation_id)
    if report.outcome == ACCEPTANCE_RECORDED:
        return report.broker_order_id
    details = [
        f"o envio da tentativa {reservation.attempt} do ciclo {reservation.cycle_id}"
        f" terminou em {report.outcome}"
    ]
    if report.broker_order_id is not None:
        details.append(f"ordem recebida {report.broker_order_id}")
    details.extend(report.errors)
    raise SimulationStopped("; ".join(details) + "; nada foi reenviado")


def confirm_open(executor, reservation, order_id):
    """Look the order up through the executor before settling it: it must be OPEN.

    SimulatedBroker reports only OPEN, WIN or LOSS, so PENDING without an error means OPEN.
    """
    report = executor.reconcile_pending()
    if report != ReconciliationReport(PENDING, reservation.reservation_id, order_id):
        raise SimulationStopped(
            f"a consulta da ordem {order_id} antes da liquidação não confirmou OPEN"
            f" ({describe_report(report)})"
        )


def settle(broker, order_id, result):
    try:
        settled = broker.settle_order(order_id, result)
    except (KeyError, ValueError) as error:
        raise SimulationStopped(
            f"a ordem simulada {order_id} não pôde ser liquidada com {result}: {error}"
        ) from error
    if not settled:
        raise SimulationStopped(f"a ordem simulada {order_id} já estava liquidada com {result}")


def record(executor, reservation, order_id, result):
    """Reconcile through the executor: the settled result must be recorded."""
    report = executor.reconcile_pending()
    expected = ReconciliationReport(
        RESULT_RECORDED, reservation.reservation_id, order_id, result
    )
    if report != expected:
        raise SimulationStopped(
            f"o resultado {result} da ordem {order_id} não foi registrado"
            f" ({describe_report(report)}); a reserva continua pendente"
        )


def report_interruption(coordinator, progress, path):
    say(
        f"Interrompido por Ctrl+C: {count(progress.cycles_ended, 'ciclo encerrado', 'ciclos encerrados')}"
        f" e {count(progress.orders, 'ordem simulada', 'ordens simuladas')} nesta execução."
    )
    if coordinator is not None:
        status = coordinator.status()
        if status.pending is not None:
            say(
                f"Pendência preservada: ciclo {status.cycle_id}, tentativa {status.attempt},"
                f" reserva {status.pending.reservation_id}, envio {status.submission_state},"
                f" ordem {status.broker_order_id or 'sem id'}."
            )
        if status.is_active:
            say(
                f"O ciclo {status.cycle_id} ficou aberto: ao reabrir este banco, a simulação"
                " vai recusá-lo, porque a corretora simulada desta execução deixou de existir."
            )
    if path is not None and path.exists():
        say(f"Banco preservado: {path}")


def print_header(args, path):
    say("SIMULAÇÃO LOCAL do PocketAutonomo: nenhuma ordem vai para uma corretora real.")
    say(
        f"Os resultados vêm de --results ({','.join(args.results)}), usados em ordem e"
        " repetidos ao terminar; não há análise de mercado nem cálculo de lucratividade."
    )
    say(
        f"Saldo fictício de {FICTITIOUS_BALANCE_CENTS} centavos, usado só para validar as"
        " reservas; não é saldo real."
    )
    plan = "contínua, até Ctrl+C" if args.cycles is None else count(args.cycles, "ciclo", "ciclos")
    say(
        f"Execução: {plan} | ativo {args.asset} | direção {args.direction}"
        f" | espera de {args.poll_interval:g} s antes de cada liquidação"
    )
    say(f"Banco SQLite: {path}")


def order_line(reservation, order_id, state):
    entry = reservation.entry
    return (
        f"SIMULAÇÃO | ciclo {reservation.cycle_id} | tentativa {entry.attempt}"
        f" | ativo {entry.asset} | direção {entry.direction}"
        f" | valor {entry.stake_cents} centavos | ordem {order_id} | {state}"
    )


def cycle_end_line(status):
    line = (
        f"SIMULAÇÃO | ciclo {status.cycle_id} encerrado com {status.outcome}"
        f" na tentativa {status.attempt}"
    )
    if status.outcome == LOSS:
        line += " (quarta LOSS: sem quinta entrada)"
    return line


def describe_report(report):
    return report.outcome if report.error is None else f"{report.outcome}: {report.error}"


def describe_error(error):
    return f"{type(error).__name__}: {error}"


def count(number, singular, plural):
    return f"{number} {singular if number == 1 else plural}"


def say(text):
    print(text, flush=True)


def warn(text):
    print(text, file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
