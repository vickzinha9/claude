"""Simulated operation on the Pocket Option screen: python -m pocketautonomo.operate.

SIMULATED EXECUTION ONLY: analysis -> coordinator -> simulated executor. No order goes to the
Pocket Option and no money moves. The entries go to paper_broker.PaperBroker, a simulator on
this computer settled by the quotes read on the screen, through the same OrderExecutor and
Coordinator that a real executor will use.

The program is the favorites scan (pocketautonomo.scan) with the operation around each
visit (operation.Operator):

- After each visit, the local analysis' decision goes to Operator.consider(). AGUARDAR, an
  expired decision, a payout below 90% or a decision already used never start a cycle.
  While the analysis answers AGUARDAR, the scan simply goes on collecting data.
- A cycle starts with a validated, current CALL or PUT decision and keeps its asset and
  direction to the end. Until it ends, the navigation between favorites is suspended: the
  program stays on the cycle's asset, which is the one on the screen after its visit, and
  only reads it every POLL_SECONDS, without any click. Every reading goes to the timings
  file. Those that the scan's checks accept are real market quotes: they go to the
  operation, to the analysis' history of this run and to a new
  runtime/cotacoes-ciclo-<data>-<hora>-<sufixo>.csv, which the analysis loads at the next
  start, all with the origin ciclo (CycleQuotes), whatever the result, the direction or the
  payout. The results of the simulator, in the operation's CSV and in its database, never
  feed the analysis, and recording the quotes never starts a cycle or validates a rule
  again: the next visit of the scan decides with them.
- Stakes of R$ 5, R$ 10, R$ 20 and R$ 40, four attempts at most; only a LOSS confirmed by
  the simulator moves on, and each gale has its own window after that result
  (--gale-window). When the cycle ends, the scan goes on to the next favorite.
- When the scan enters its PAUSA (scan.Pause, after a switch that the asset field did not
  confirm), the operation enters it too (Operator.pause_collection), until the end of the
  run: no decision starts a cycle, nothing read enters the analysis, a pending entry keeps
  what was recorded, and the run ends with exit code 1 (130 after Ctrl+C). The PAUSA starts
  at the end of a visit, and a visit only runs with no cycle open, so it never starts during
  a cycle.

The state of the coordinator is kept in runtime/operacao-simulada.db and the simulated
orders in runtime/operacao-simulada-ordens.db, both reused by every run: a decision used
once stays used after a restart, and a pending entry is checked at the next start, never
sent again. Each run writes its events to a new runtime/operacao-<data>-<hora>-<sufixo>.csv.

--continuo runs one Execution after the other (continuous.py): after a failure of the browser,
a lost session or cycles without a complete reading, a new run opens the browser again and
waits by itself for the trading screen; after a PAUSA or a safety stop, it never starts again
by itself. Each run is the same as one of main().

--demonstracao runs, without the browser and with a simulated clock, scenes with
artificial decisions and quotes, in new runtime/demonstracao-... files: AGUARDAR, an
expired decision, four LOSS without a fifth entry, a repeated decision, a payout drop
during the gales, an expired gale window, an uncertain submission and a restart.

Exit codes: 0 when the scan wrote at least one line with data (or the demonstration passed
all its checks), 1 when the run failed or stopped, 2 for invalid arguments or
configuration, and 130 after Ctrl+C.
"""

import argparse
import csv
import math
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pocketautonomo import analysis, observe, operation, scan
from pocketautonomo.coordinator import Coordinator, CoordinatorError
from pocketautonomo.cycle import CALL, LOSS, MAX_ATTEMPTS, PUT, WIN
from pocketautonomo.observe import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    BrowserError,
    ConfigError,
    ObserverStopped,
    count,
    say,
    warn,
)
from pocketautonomo.operation import OperationStopped, money
from pocketautonomo.paper_broker import PaperBroker
from pocketautonomo.storage import SqliteStore, StorageError

DATABASE_NAME = "operacao-simulada.db"
ORDERS_NAME = "operacao-simulada-ordens.db"
CSV_PREFIX = "operacao-"
DEMO_PREFIX = "demonstracao-"
POLL_SECONDS = scan.POLL_SECONDS  # between two readings of the cycle's asset
HOLD_MOMENT = "durante o ciclo simulado"  # the moment of these readings in the timings file
PAYOUT_CONFIRMED = "payout confirmado por duas leituras seguidas"
WRITE_RETRY_SECONDS = 30.0  # after a failed write, the wait before a new file of the cycle's quotes
CYCLE_QUOTE_NOTE = "cotação de mercado lida na tela durante o ciclo, conferida e atual"
# The file of the real quotes read during cycles (analysis.CYCLE_PREFIX). It starts with the
# columns that the analysis reads, as the scan's CSV has them, and says its origin in each line.
CYCLE_QUOTE_COLUMNS = analysis.COLUMNS + (
    analysis.ORIGIN_COLUMN,  # always ciclo: a quote read on the screen, never a result of the simulator
    "recorded_at_utc",  # this computer's clock, in UTC, when the reading was accepted for the history
    "data_age_ms",  # from the capture of the quote to the line
    "cycle_id",  # the cycle of the simulated operation that held the asset
    "attempt",  # its attempt when the quote was read
    "operation_state",  # CICLO ATIVO or PENDÊNCIA when the quote was read
    "reading_ms",  # the reading of the asset, the quote, the payout and the asset again
    "payout_note",  # confirmed by two readings in a row, or why the payout is empty
)

INTRO = (
    "OPERAÇÃO SIMULADA do PocketAutonomo: análise -> coordenador -> executor SIMULADO. Nenhuma"
    " ordem vai para a Pocket Option e nenhum dinheiro é movimentado: as entradas vão para um"
    " simulador neste computador, liquidado pelas cotações lidas na tela. O único clique continua"
    " sendo num favorito, para selecioná-lo, e nunca durante um ciclo."
)


def main(
    argv=None, *, open_session=None, read_line=input, now=None, monotonic=None, engine=None, sleep=None,
    keep_awake=None,
):
    """Run the simulated operation and return its exit code.

    The keyword arguments exist for tests, as in scan.main: they replace open_scan_session,
    input, the UTC clock, time.perf_counter and the OCR's LazyEngine; sleep and keep_awake
    replace, in the continuous mode only, time.sleep between two runs and the prevention of
    Windows' sleep (continuous.KeepAwake).
    """
    args = parse_arguments(argv)
    if args.demonstracao:
        return demonstration(args, now=now)
    open_session = scan.open_scan_session if open_session is None else open_session
    ctx = observe.Context(now=now, engine=engine)
    if monotonic is not None:
        ctx.monotonic = monotonic
    if args.continuo:
        from pocketautonomo import continuous  # it builds on this module

        return continuous.run(args, ctx, open_session, read_line, sleep=sleep, keep_awake=keep_awake)
    runtime_dir = ctx.runtime_dir
    profile_dir = runtime_dir / observe.PROFILE_DIR_NAME
    selectors_path = runtime_dir / observe.SELECTORS_FILE_NAME
    scan_path = runtime_dir / scan.SCAN_SELECTORS_FILE_NAME
    run = Execution(args, ctx)
    lock = session = None
    interrupted = False
    try:
        say(INTRO)
        selectors = scan.load_reader_selectors(selectors_path)
        favorites_selector, invalid = scan.current_favorites_selector(scan_path, args.configure)
        if isinstance(selectors["price"], observe.QuoteOcrSource):
            ctx.engine.get()  # a missing OCR stops here, before the browser opens
            observe.announce_ocr(ctx)
        lock = scan.ScanLock(runtime_dir / scan.LOCK_FILE_NAME)
        scan.warn_kept_pause(runtime_dir)
        run.open()
        url = args.url if args.url is not None else observe.ask_url(read_line)
        scan.print_plan(url, args, profile_dir, selectors_path, scan_path)
        session = open_session(profile_dir)
        observe.open_page(session, url)
        scan.wait_for_login(read_line, args.configure)
        if session.is_closed():
            raise ObserverStopped("a janela do navegador foi fechada antes da confirmação do login")
        if args.configure:
            favorites_selector = scan.configure_favorites(
                session, scan_path, favorites_selector, invalid, selectors, read_line, ctx
            )
        say(scan.HANDS_OFF)
        run.visit(session, selectors, favorites_selector)
        return run.finish()
    except ConfigError as error:
        warn(f"CONFIGURAÇÃO: {error}")
        return EXIT_CONFIG
    except STOPS as error:
        warn(f"PARADA: {error}")
        run.stopped()
        return EXIT_FAILED
    except KeyboardInterrupt:
        interrupted = True
        say(f"Interrompido por Ctrl+C: {scan.written_summary(run.progress)}.")
        run.stopped()
        return EXIT_INTERRUPTED
    finally:
        run.close_files()
        observe.close_session(session, interrupted)
        run.close_state()
        if lock is not None:
            lock.release()


# What stops a run with exit code 1: the scan's safety stops, the browser, the files and the
# operation's own stops. Ctrl+C is 130, and a configuration problem is 2.
STOPS = (ObserverStopped, BrowserError, scan.ScanBusy, OSError, StorageError, CoordinatorError, OperationStopped)


class Execution:
    """One run of the simulated operation on the screen: its saved state, its files and its visits.

    main() runs one. The continuous mode (continuous.py) runs one after the other through this
    same class, so that each of them has every protection of a run: the saved state of the
    coordinator and of the simulator reopened and recovered (Operator.recover) before any visit,
    a decision that starts one cycle at most, a pending entry checked and never sent again, and
    the PAUSA of the scan.
    """

    def __init__(self, args, ctx):
        self.args = args
        self.ctx = ctx
        self.database = ctx.runtime_dir / DATABASE_NAME
        self.orders = ctx.runtime_dir / ORDERS_NAME
        self.progress = scan.ScanProgress()
        self.coordinator = self.broker = self.log = self.operator = self.quotes = None
        self.state = None  # the scan.Scan of the visits, once they start

    def open(self, rules=True):
        """Open the saved state and resolve what the last run left, before the browser opens."""
        args, ctx = self.args, self.ctx
        self.coordinator = Coordinator(SqliteStore(self.database))
        self.broker = PaperBroker(self.orders, args.horizon, ctx.now)
        self.log = operation.OperationLog(new_csv_path(ctx.runtime_dir, CSV_PREFIX))
        self.quotes = CycleQuotes(ctx.runtime_dir, warn)
        self.operator = operation.Operator(
            self.coordinator, self.broker, self.log, now=ctx.now, monotonic=ctx.monotonic, horizon=args.horizon,
            gale_window=args.gale_window,
        )
        if rules:
            print_rules(args, self.database, self.orders)
        self.operator.recover()

    def visit(self, session, selectors, favorites_selector, prepare=None):
        """The scan of the favorites, with the operation after each visit; prepare(state) may add to it.

        With a calibration of the candles (scan.chart_setup), each clean visit also reads the
        chart's candles: the exploratory reading is shown, and a CALL or PUT of a rule of the
        candles validated on separate data reaches the operation as any other decision.
        """
        state = self.state = scan.Scan(selectors, favorites_selector, self.args.load_timeout, self.progress)
        state.chart = scan.chart_setup(selectors, self.ctx.runtime_dir, self.args.horizon)
        if state.chart is not None:
            self.progress.candle_log = state.chart.log
        state.hooks = OperationHooks(self.operator, state, self.ctx, self.quotes)
        if prepare is not None:
            prepare(state)
        scan.run_scan(session, state, self.args, self.ctx)

    def finish(self):
        """The exit code of visits that ended, with what the scan and the operation did."""
        code = scan.finish(self.progress)
        if self.operator.busy():  # the window was closed during a cycle
            warn(
                "PARADA: a varredura terminou com um ciclo de operação aberto; o estado ficou gravado e"
                " é conferido na próxima abertura, sem reenvio."
            )
            code = EXIT_FAILED
        report(self.operator, self.log, self.database, self.orders, self.quotes)
        return code

    def stopped(self):
        """What a run that stopped or was interrupted shows: the scan so far and the operation."""
        scan.report(self.progress)
        scan.warn_pause(self.progress)
        report(self.operator, self.log, self.database, self.orders, self.quotes)

    def close_files(self):
        for closing in (self.progress.csv, self.progress.timings, self.progress.candle_log, self.log, self.quotes):
            if closing is not None:
                closing.close()

    def close_state(self):
        if self.coordinator is not None:
            self.coordinator.close()  # also releases the database lock
        if self.broker is not None:
            self.broker.close()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.operate",
        description=(
            "Operação SIMULADA: a varredura dos favoritos, a análise local CALL/PUT e o"
            " coordenador de ciclos, com as entradas num simulador neste computador. Não envia"
            " ordens à Pocket Option e não movimenta dinheiro."
        ),
    )
    parser.add_argument(
        "--url",
        type=observe.official_url,
        help=f"link oficial da tela de operações, em https://{observe.ALLOWED_DOMAIN}; sem ele, o"
        " programa pede o link no terminal",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="depois do login, mostra os favoritos que o seletor gravado encontra (ou pede o"
        " seletor) antes de começar",
    )
    parser.add_argument(
        "--cycles",
        type=observe.positive_int,
        help="quantos ciclos da varredura fazer, cada um passando uma vez pelos favoritos; um ciclo"
        " de operação que começar dentro dele vai até o fim; sem esta opção, até Ctrl+C",
    )
    parser.add_argument(
        "--interval",
        type=scan.seconds_from(scan.MIN_INTERVAL, math.inf),
        default=scan.DEFAULT_INTERVAL,
        help=f"segundos entre dois ciclos da varredura (padrão {scan.DEFAULT_INTERVAL:g})",
    )
    parser.add_argument(
        "--load-timeout",
        type=scan.seconds_from(scan.MIN_LOAD_TIMEOUT, scan.MAX_LOAD_TIMEOUT),
        default=scan.DEFAULT_LOAD_TIMEOUT,
        help=f"segundos, depois do clique num favorito, para ter dados estáveis (padrão"
        f" {scan.DEFAULT_LOAD_TIMEOUT:g})",
    )
    parser.add_argument(
        "--horizon",
        type=analysis.horizon_seconds,
        default=analysis.DEFAULT_HORIZON,
        help=f"segundos das operações hipotéticas da análise e da duração de cada ordem simulada"
        f" (padrão {analysis.DEFAULT_HORIZON:g}, de {analysis.MIN_HORIZON:g} a {analysis.MAX_HORIZON:g})",
    )
    parser.add_argument(
        "--gale-window",
        type=scan.seconds_from(operation.MIN_GALE_WINDOW, operation.MAX_GALE_WINDOW),
        default=operation.GALE_WINDOW_SECONDS,
        help="segundos, depois de uma LOSS confirmada, para a entrada do gale seguinte (padrão"
        f" {operation.GALE_WINDOW_SECONDS:g}, de {operation.MIN_GALE_WINDOW:g} a"
        f" {operation.MAX_GALE_WINDOW:g}); vencida a janela, o ciclo é interrompido",
    )
    parser.add_argument(
        "--demonstracao",
        action="store_true",
        help="sem navegador: cenas com decisões e cotações ARTIFICIAIS e relógio simulado, em"
        " arquivos novos runtime/demonstracao-...; os resultados não entram na análise",
    )
    parser.add_argument(
        "--continuo",
        action="store_true",
        help="modo contínuo, até Ctrl+C ou até você fechar a janela do navegador: depois de uma falha do"
        " navegador, da sessão desconectada ou de ciclos sem leitura completa, abre uma execução nova e espera"
        " sozinho a tela de operações, com espera crescente e um limite de falhas seguidas; nunca recomeça"
        " sozinho depois de uma PAUSA ou de uma parada por segurança. Grava runtime/continuo-*.log e"
        " runtime/continuo-estado.json e, no Windows, impede a suspensão enquanto roda; não aceita --cycles",
    )
    return parser


def parse_arguments(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.continuo and args.cycles is not None:
        parser.error("--continuo roda até Ctrl+C e não aceita --cycles")
    if args.continuo and args.demonstracao:
        parser.error("--continuo não pode ser usado com --demonstracao")
    return args


def print_rules(args, database, orders):
    say(
        "Primeira entrada só com decisão CALL/PUT de regra validada em dados separados, atual (até"
        f" {analysis.seconds_text(analysis.DECISION_WINDOW_SECONDS)} s depois da captura da cotação) e com"
        f" payout de {analysis.seconds_text(analysis.ELIGIBLE_PAYOUT)}% ou mais; AGUARDAR não inicia ciclo, e"
        " uma decisão inicia um ciclo no máximo."
    )
    say(
        f"Ciclo: {money(500)}, {money(1000)}, {money(2000)} e {money(4000)}, no máximo {MAX_ATTEMPTS}"
        " tentativas, mesmo ativo e mesma direção; só LOSS confirmada avança, e cada gale tem"
        f" {analysis.seconds_text(args.gale_window)} s depois dela para entrar, sem exigir payout. Cada ordem"
        f" simulada vence em {analysis.seconds_text(args.horizon)} s."
    )
    say(
        "Durante um ciclo, a navegação entre favoritos fica suspensa: o programa só lê o ativo do"
        f" ciclo, a cada {analysis.seconds_text(POLL_SECONDS)} s, sem clicar. Os resultados simulados"
        " não entram na análise."
    )
    say(f"Estado gravado: {database} e {orders}; uma pendência é conferida na próxima abertura, sem reenvio.")
    say(f"Os tempos de envio são do simulador: {operation.SIMULATOR_TIME_NOTE}. Ctrl+C encerra.")


class CycleQuotes:
    """The real quotes of the cycle's asset read during cycles, kept for the analysis.

    record() takes a reading that hold_reading accepted, current by scan.current_data (at
    most scan.MAX_DATA_AGE_SECONDS old). It adds it to the running analysis' history, with
    the origin analysis.ORIGIN_CYCLE, and writes it to a new file
    runtime/cotacoes-ciclo-<data>-<hora>-<sufixo>.csv, created with the first one and
    flushed after each line, which the analysis loads at the next start. The moment is kept
    to the millisecond, as in the file, so a quote of an asset at a moment already in the
    history is neither added nor written again. Nothing is computed here: the next visit of
    the scan decides with the new quotes.

    A line is recorded whatever the result of the attempt, the direction or the payout, and
    its payout only when two readings in a row agree. If the file cannot be written, warn is
    called once and the quote stays only in the history of this run; from WRITE_RETRY_SECONDS
    later on, the next quote tries a new file.
    """

    def __init__(self, runtime_dir, warn):
        self.runtime_dir = runtime_dir
        self.path = None  # the file being written
        self.paths = []  # every file created in this run
        self.recorded = 0  # quotes added to the history
        self.written = 0  # lines written to a file
        self.failed = False  # a write failed in this run
        self._warn = warn
        self._file = None
        self._writer = None
        self._retry_at = None  # after a failure, no new file before this moment

    def record(self, seen, recorded_at, context, analyzer):
        """Keep seen, read in context (cycle_id, attempt, operation_state); True when kept."""
        current = scan.current_data(seen.captured_at, recorded_at, CYCLE_QUOTE_NOTE)
        if not current.ready:
            return False
        moment = seen.captured_at.astimezone(timezone.utc)
        captured = observe.utc_text(moment)
        sample = analysis.Sample(
            datetime.fromisoformat(captured), seen.price, seen.quote, seen.payout, analysis.ORIGIN_CYCLE
        )
        if analyzer is not None and not analyzer.history.add(seen.asset, sample):
            return False  # the same quote of the same moment: already in the history
        self.recorded += 1
        row = (
            seen.asset,
            seen.quote,
            captured,
            "" if seen.payout is None else f"{seen.payout:g}",
            analysis.READY,
            analysis.ORIGIN_CYCLE,
            observe.utc_text(recorded_at.astimezone(timezone.utc)),
            f"{current.age_ms:.1f}",
            context.get("cycle_id", ""),
            context.get("attempt", ""),
            context.get("operation_state", ""),
            "" if seen.reading_ms is None else f"{seen.reading_ms:.1f}",
            seen.payout_note or "",
        )
        self._write(row, recorded_at)
        return True

    def _write(self, row, now):
        if self._file is None and self._retry_at is not None and now < self._retry_at:
            return  # a file failed moments ago: this quote stays only in the history
        try:
            if self._file is None:
                path = new_csv_path(self.runtime_dir, analysis.CYCLE_PREFIX)
                self._file = open(path, "x", newline="", encoding="utf-8")
                self.path = path
                self.paths.append(path)
                self._writer = csv.writer(self._file)
                self._writer.writerow(CYCLE_QUOTE_COLUMNS)
                say(f"Cotações lidas durante os ciclos, para a análise: {path}")
            self._writer.writerow(row)
            self._file.flush()
            self.written += 1
        except OSError as error:
            self._discard()
            self._retry_at = now + timedelta(seconds=WRITE_RETRY_SECONDS)
            if not self.failed:
                self._warn(
                    f"AVISO: não foi possível gravar as cotações lidas durante o ciclo em"
                    f" {self.path or self.runtime_dir} ({error}); a operação continua, essas cotações"
                    f" ficam só na análise desta execução, e um arquivo novo é tentado a cada"
                    f" {WRITE_RETRY_SECONDS:g} s."
                )
            self.failed = True

    def _discard(self):
        """Close the file that failed, if any; the next quote opens a new one."""
        file, self._file, self._writer = self._file, None, None
        if file is not None:
            try:
                file.close()
            except OSError:
                pass

    def close(self):
        self._discard()


def cycle_context(operator):
    """The cycle, the attempt and the state of the operation, before a reading is given to it."""
    status = operator.coordinator.status()
    return {"cycle_id": status.cycle_id, "attempt": status.attempt, "operation_state": operator.state()}


class OperationHooks:
    """The scan's hooks: the analysis' decision after each visit, and the hold during a cycle."""

    def __init__(self, operator, state, ctx, quotes=None):
        self.operator = operator
        self.state = state
        self.ctx = ctx
        self.quotes = quotes

    def before_visit(self, session, favorite):
        if self.operator.busy():  # an entry restored from the last run, still pending
            hold(session, self.operator, self.state, self.ctx, favorite.position, self.quotes)

    def after_visit(self, session, visit):
        pause = self.state.progress.paused
        if pause is not None:  # the scan is in PAUSA: no decision starts a cycle until the end of the run
            self.operator.pause_collection(pause.cause)
        if visit.decision is not None:
            self.operator.consider(visit.decision, visit.observation.duration_ms, visit.decision_ms)
        if self.operator.busy():
            hold(session, self.operator, self.state, self.ctx, visit.favorite.position, self.quotes)


def hold(session, operator, state, ctx, position, quotes=None):
    """Stay on the open cycle's asset, reading it without any click, until the cycle ends.

    Every reading that counts goes to the operation and, through quotes (CycleQuotes), to
    the analysis' history: it is recorded after the operation took it, so its deadlines see
    the reading first, and also when the operation stops on it.
    """
    asset = operator.held_asset()
    say(
        f"Navegação entre favoritos suspensa: o ciclo de operação fica em {asset}, lido a cada"
        f" {analysis.seconds_text(POLL_SECONDS)} s, sem cliques, até terminar. Não troque o ativo na tela:"
        " uma leitura de outro ativo não conta, e sem cotação no vencimento a ordem simulada é cancelada."
    )
    where = {"cycle": state.progress.cycles, "favorite_position": position}
    previous = shown = None
    polls = 0
    while operator.busy():
        scan.check_window(session)
        poll = scan.read_poll(session, state.selectors, ctx)
        if poll.outside:
            scan.ensure_on_platform(session)
        polls += 1
        seen, problem = hold_reading(poll, previous, asset, state.sources["payout"])
        verdict = scan.ACCEPTED if seen is not None else f"{scan.REFUSED}: {problem}"
        scan.record_timing(state, HOLD_MOMENT, poll.price, where, polls, asset=asset, verdict=verdict)
        if seen is not None:
            shown = None
            context = cycle_context(operator)
            accepted_at = ctx.now()  # before the operation's own work, which is not the quote's age
            try:
                operator.observe(seen.asset, seen.captured_at, seen.price, seen.quote, seen.payout, seen.reading_ms)
            finally:
                if quotes is not None:
                    quotes.record(seen, accepted_at, context, state.analyzer)
        else:
            if problem != shown:
                say(f"  leitura durante o ciclo não usada: {problem}")
                shown = problem
            operator.tick()
        previous = poll
        if operator.busy():
            session.wait(POLL_SECONDS)
    say("Ciclo de operação encerrado: a navegação entre favoritos continua.")


def hold_reading(poll, previous, asset, payout_sourced=True):
    """(Seen, None) for a reading of the cycle's asset that counts, or (None, why it does not).

    The checks are the scan's. The asset must be the cycle's before and after the reading,
    and the quote must agree, within scan.QUOTE_JUMP, with the reading right before
    (scan.judge_price), on the same asset; its capture time is the quote's own. The payout
    counts only when it equals the one read right before (scan.judge_payout); without that,
    the reading still counts, without a payout, and payout_note tells why.
    """
    value, problem = held_quote(poll, asset)
    if problem is not None:
        return None, problem
    prior = previous if previous is not None and held_quote(previous, asset)[1] is None else None
    price, _ = scan.judge_price(poll.price, prior, None, False, True)
    if not price.available:
        return None, price.reason
    if price.captured_at is None:
        return None, "sem o horário de captura da cotação"
    payout_reading, _ = scan.judge_payout(poll.payout, prior, payout_sourced)
    if payout_reading.available:
        payout, note = scan.payout_number(payout_reading.value), PAYOUT_CONFIRMED
    else:
        payout, note = None, f"payout {observe.UNAVAILABLE}: {payout_reading.reason}"
    return operation.Seen(asset, price.captured_at, value, price.value, payout, poll.duration_ms, note), None


def held_quote(poll, asset):
    """(the quote as a number, None), or (None, why poll is not a reading of asset)."""
    key = analysis.asset_key(asset)
    for reading in (poll.asset_before, poll.asset_after):
        if not reading.available:
            return None, f"ativo indisponível ({reading.reason})"
        if scan.asset_key(reading.value) != key:
            return None, f"a tela mostra {reading.value}, e não {asset}; a operação não clica durante o ciclo"
    if not poll.price.available:
        return None, f"cotação indisponível ({poll.price.reason})"
    value = scan.quote_number(poll.price.value)
    if value is None or not value > 0:
        return None, f"a cotação {poll.price.value} não pôde ser comparada"
    return value, None


def report(operator, log, database, orders, quotes=None):
    """What the operation did, and what it left for the next start."""
    if operator is None:
        return
    counts = operator.counts
    say(
        f"Operação simulada: {count(counts['cycles'], 'ciclo iniciado', 'ciclos iniciados')},"
        f" {count(counts['entries'], 'entrada simulada', 'entradas simuladas')},"
        f" {count(counts['deadline'], 'entrada recusada', 'entradas recusadas')} por prazo vencido e"
        f" {count(counts['windows'], 'janela de gale vencida', 'janelas de gale vencidas')}."
    )
    try:
        status = operator.coordinator.status()
    except CoordinatorError:
        status = None
    if status is not None and status.pending is not None:
        pending = status.pending
        say(
            f"Pendência preservada: ciclo {status.cycle_id}, tentativa {pending.attempt}"
            f" ({money(pending.entry.stake_cents)}), reserva {pending.reservation_id}, envio"
            f" {status.submission_state}, ordem simulada {status.broker_order_id or 'sem id'}. Na próxima"
            " abertura, ela é conferida no simulador, sem reenvio."
        )
    elif status is not None and status.is_active:
        say(
            f"O ciclo {status.cycle_id} ficou aberto sem entrada pendente: na próxima abertura, ele é"
            " abandonado, porque a janela da próxima entrada não sobrevive ao reinício."
        )
    if status is not None:
        say(f"Estado final: {operator.state()}.")
    if log is not None and log.rows:
        say(f"CSV da operação: {log.path}")
    if quotes is not None and quotes.recorded:
        if quotes.written == quotes.recorded:
            where = f"gravadas em {', '.join(str(path) for path in quotes.paths)}"
        elif quotes.written:
            where = (
                f"{quotes.written} gravadas em {', '.join(str(path) for path in quotes.paths)} e"
                f" {quotes.recorded - quotes.written} só na análise desta execução"
            )
        else:
            where = "só na análise desta execução"
        say(
            f"Cotações de mercado lidas durante os ciclos: {quotes.recorded}, com a origem"
            f" {analysis.ORIGIN_CYCLE}, {where}; elas entram na análise, e os resultados do simulador, não."
        )
    say(f"Estado gravado, preservado: {database} e {orders}")


def new_csv_path(runtime_dir, prefix):
    """A new CSV path with a unique name in runtime_dir, which is created if needed."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        path = runtime_dir / f"{prefix}{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csv"
        if not path.exists():
            return path


# The demonstration: artificial decisions and quotes, with a simulated clock.

DEMO_ASSET = "DEMO/ARTIFICIAL OTC"
DEMO_READING_MS = 120.0  # an artificial reading time, shown as such
DEMO_DECISION_MS = 5.0


class DemoFailed(Exception):
    """A check of the demonstration failed: the operation did not behave as described."""


class DemoClock:
    """A clock that only moves when the demonstration moves it."""

    def __init__(self, start):
        self.t = start
        self.m = 0.0

    def now(self):
        return self.t

    def monotonic(self):
        return self.m

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        self.m += seconds


class DemoBroker(PaperBroker):
    """The simulator, which can lose the answer of one submission after saving its order."""

    lose_next_answer = False

    def submit_order(self, request):
        response = super().submit_order(request)
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise TimeoutError("DEMONSTRAÇÃO: a resposta do simulador foi perdida de propósito, depois de gravar a ordem")
        return response


class Demonstration:
    """Scenes of the operation, each with its checks, on new files of runtime/."""

    def __init__(self, args, now):
        self.args = args
        start = (now or observe.utc_now)().replace(microsecond=0)
        self.clock = DemoClock(start)
        runtime_dir = Path(observe.RUNTIME_DIR)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        self.database = runtime_dir / f"{DEMO_PREFIX}{stamp}.db"
        self.orders = runtime_dir / f"{DEMO_PREFIX}{stamp}-ordens.db"
        self.log = operation.OperationLog(runtime_dir / f"{DEMO_PREFIX}operacao-{stamp}.csv", operation.DEMONSTRATION)
        self.coordinator = self.broker = self.operator = None
        self.price = 1.10000

    def open(self):
        self.coordinator = Coordinator(SqliteStore(self.database))
        self.broker = DemoBroker(self.orders, self.args.horizon, self.clock.now)
        self.operator = operation.Operator(
            self.coordinator, self.broker, self.log, now=self.clock.now, monotonic=self.clock.monotonic,
            horizon=self.args.horizon, gale_window=self.args.gale_window,
        )
        self.operator.recover()

    def close(self):
        for closing in (self.coordinator, self.broker):
            if closing is not None:
                closing.close()
        self.coordinator = self.broker = None

    def decision(self, action, payout=92.0, age=0.4, validated=True):
        """An ARTIFICIAL decision, from a quote captured age seconds ago."""
        self.clock.advance(30)  # the next visit of the scan
        self.price = round(self.price + 0.0005, 5)
        at = self.clock.now() - timedelta(seconds=age)
        sample = analysis.Sample(at, self.price, f"{self.price:.5f}", payout)
        rule = analysis.Rule(analysis.TREND, 1)
        reason = "regra ARTIFICIAL da demonstração: nenhuma regra foi validada em dados reais"
        validation = analysis.Validation(validated, reason if validated else "dados insuficientes (artificial)", rule)
        if action == analysis.WAIT:
            return analysis.Decision(DEMO_ASSET, action, validation.reason, validation, sample)
        until = at + timedelta(seconds=analysis.DECISION_WINDOW_SECONDS)
        return analysis.Decision(DEMO_ASSET, action, reason, validation, sample, until)

    def consider(self, decision):
        return self.operator.consider(decision, DEMO_READING_MS, DEMO_DECISION_MS)

    def settle(self, result, payout=92.0):
        """Move to the expiry of the pending order and read the quote that gives result."""
        status = self.coordinator.status()
        order = self.broker.order(status.broker_order_id)
        self.clock.t = order.expires_at
        self.clock.advance(0.3)
        up = (result == WIN) == (order.direction == CALL)
        self.price = round(order.entry_price + (0.0002 if up else -0.0002), 5)
        self.read(payout)

    def read(self, payout=92.0, wait=0.5):
        """One reading of the cycle's asset, wait seconds after the last one."""
        self.clock.advance(wait)
        self.operator.observe(DEMO_ASSET, self.clock.now(), self.price, f"{self.price:.5f}", payout, DEMO_READING_MS)

    def entries(self, cycle_id):
        """(stake, state, payout of the entry) of the orders of a cycle, in order."""
        rows = [row for row in self.log.rows if row["event"] == operation.ENTRY_SUBMITTED and row["cycle_id"] == cycle_id]
        orders = [self.broker.order(row["simulated_order_id"]) for row in rows]
        return [(order.stake_cents, order.state, row["payout_percent"]) for order, row in zip(orders, rows)]

    def status(self):
        return self.coordinator.status()

    def check(self, condition, message):
        if not condition:
            raise DemoFailed(message)
        say(f"  conferido: {message}")

    def scene(self, number, title):
        say("")
        say(f"CENA {number}: {title}")

    def run(self):
        horizon = self.args.horizon
        self.open()
        status = self.status  # follows the coordinator reopened in scene 8
        self.scene(1, "AGUARDAR não inicia ciclo; a coleta continua")
        self.consider(self.decision(analysis.WAIT, validated=False))
        self.check(status().cycle_id is None and self.operator.state() == operation.COLLECTING, "sem ciclo, estado COLETANDO")

        self.scene(2, "decisão CALL com o prazo vencido (cotação de 6 s atrás)")
        self.consider(self.decision(CALL, age=6.0))
        self.check(status().cycle_id is None and self.operator.counts["deadline"] == 1, "recusada por prazo vencido, sem ciclo")

        self.scene(3, "CALL validada, payout 92%: quatro LOSS seguidas")
        first = self.decision(CALL)
        self.consider(first)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.settle(LOSS)
            if attempt < MAX_ATTEMPTS:
                self.read()  # a new quote after the LOSS: the gale's entry
        self.check(
            [entry[:2] for entry in self.entries(1)] == [(500, LOSS), (1000, LOSS), (2000, LOSS), (4000, LOSS)],
            "R$ 5, R$ 10, R$ 20 e R$ 40, todas LOSS",
        )
        self.read()
        self.check(status().outcome == LOSS and not status().is_active and len(self.entries(1)) == 4, "sem quinta entrada")

        self.scene(4, "a mesma decisão da cena 3, apresentada de novo")
        repeated = self.consider(first)
        self.check(not repeated.started and status().cycle_id == 1, "recusada: já iniciou o ciclo 1")

        self.scene(5, "PUT validada com payout 92%; nos gales o payout cai para 80%")
        second = self.decision(PUT)
        self.consider(second)
        self.settle(LOSS, payout=80.0)
        self.read(payout=80.0)
        self.settle(LOSS, payout=80.0)
        self.read(payout=80.0)
        self.settle(WIN, payout=80.0)
        entries = self.entries(2)
        self.check(
            [entry[:2] for entry in entries] == [(500, LOSS), (1000, LOSS), (2000, WIN)]
            and [entry[2] for entry in entries[1:]] == [80.0, 80.0],
            "os gales entraram com payout de 80%, e o WIN na tentativa 3 encerrou o ciclo",
        )

        self.scene(6, f"CALL validada; depois da LOSS, nenhuma cotação nova em {analysis.seconds_text(self.args.gale_window)} s")
        self.consider(self.decision(CALL))
        self.settle(LOSS)
        self.clock.advance(self.args.gale_window + 1)
        self.operator.tick()
        self.read()  # a quote that arrives too late
        self.check(
            status().outcome == "ABANDONED" and len(self.entries(3)) == 1 and self.operator.counts["windows"] == 1,
            "janela da tentativa 2 vencida: ciclo interrompido, sem ordem atrasada",
        )

        self.scene(7, "CALL validada; a resposta do simulador se perde depois de gravar a ordem (UNKNOWN)")
        self.broker.lose_next_answer = True
        self.consider(self.decision(CALL))
        self.check(status().broker_order_id is not None, "a ordem foi achada pela reserva, sem reenvio")
        self.settle(WIN)
        orders = [order for order in self.broker.orders() if order.order_id is not None]
        self.check(len(orders) == 9 and status().outcome == WIN, "uma única ordem para a reserva, e WIN na tentativa 1")

        self.scene(8, "reinício: o coordenador e o simulador são fechados e reabertos")
        self.close()
        self.open()
        again = self.consider(second)
        self.check(not again.started and status().cycle_id == 4, "a decisão da cena 5 continua recusada depois de reabrir")
        say("")
        say(
            f"Demonstração concluída: as 8 cenas passaram nas conferências, com horizonte de {analysis.seconds_text(horizon)} s"
            " e relógio simulado. Decisões, cotações e resultados são ARTIFICIAIS: não dizem nada sobre o"
            " mercado e não entram na análise."
        )


def demonstration(args, now=None):
    say(
        "DEMONSTRAÇÃO da operação simulada: sem navegador, com relógio simulado e decisões, cotações e"
        " resultados ARTIFICIAIS. Nada vai para a Pocket Option, nenhum dinheiro é movimentado e nada"
        " disto entra na análise."
    )
    demo = None
    try:
        demo = Demonstration(args, now)
        say(f"Bancos desta demonstração: {demo.database} e {demo.orders}")
        demo.run()
        return EXIT_OK
    except (DemoFailed, OperationStopped, StorageError, CoordinatorError, OSError) as error:
        warn(f"PARADA: a demonstração não se comportou como descrito: {error}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        say("Interrompido por Ctrl+C.")
        return EXIT_INTERRUPTED
    finally:
        if demo is not None:
            demo.close()
            demo.log.close()
            if demo.log.rows:
                say(f"CSV da demonstração: {demo.log.path}")


if __name__ == "__main__":
    sys.exit(main())
