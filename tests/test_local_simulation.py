"""Tests of the local simulation program, pocketautonomo.local_simulation.

Every result comes from --results and every SIM-... order is simulated in memory: nothing
here reaches a real broker. FakeSleep stands in for time.sleep, so the runs are instant.
"""

import collections
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pocketautonomo import local_simulation
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import CALL, LOSS, PUT, WIN
from pocketautonomo.executor import ORDER_OPEN
from pocketautonomo.simulated_broker import SimulatedBroker
from pocketautonomo.storage import SqliteStore, connect_read_only
from pocketautonomo.submission import ACCEPTED

ROOT = Path(__file__).resolve().parent.parent
ASSET = local_simulation.DEFAULT_ASSET

# What one main() call returned and printed, with the FakeSleep and the broker it used.
Run = collections.namedtuple("Run", "code out err sleep broker")


class FakeSleep:
    """Stands in for time.sleep: records each wait and runs actions[n] on the n-th call."""

    def __init__(self, actions=None):
        self.calls = []
        self.actions = actions or {}

    def __call__(self, seconds):
        self.calls.append(seconds)
        action = self.actions.get(len(self.calls))
        if action is not None:
            action()


def press_ctrl_c():
    raise KeyboardInterrupt


class LocalSimulationTest(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "simulation.db")

    def run_main(self, *args, sleep=None):
        """Run main() on the database at self.path, capturing what it prints."""
        sleep = FakeSleep() if sleep is None else sleep
        broker = SimulatedBroker()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = local_simulation.main(["--db", self.path, *args], sleep=sleep, broker=broker)
        return Run(code, out.getvalue(), err.getvalue(), sleep, broker)

    def rows(self):
        """The committed cycles and reservations, in order."""
        with closing(connect_read_only(self.path)) as connection:
            cycles = connection.execute(
                "SELECT cycle_id, asset, direction, outcome FROM cycles ORDER BY cycle_id"
            ).fetchall()
            reservations = connection.execute(
                "SELECT reservation_id, cycle_id, attempt, stake_cents, result,"
                " submission_state, broker_order_id FROM reservations"
                " ORDER BY cycle_id, attempt"
            ).fetchall()
        return cycles, reservations

    def assert_released(self):
        """The program closed its database: another store can take the lock."""
        SqliteStore(self.path).close()

    def assert_sent_once(self, run, reservations, asset, direction):
        """Each saved, settled reservation was sent once, in order, and shown in the output."""
        self.assertEqual(
            [(r.reservation_id, r.asset, r.direction, r.stake_cents) for r in run.broker.requests],
            [(row[0], asset, direction, row[3]) for row in reservations],
        )
        order_ids = [row[6] for row in reservations]
        self.assertEqual(len(set(order_ids)), len(order_ids))
        for _, cycle_id, attempt, stake, result, state, order_id in reservations:
            self.assertEqual(state, ACCEPTED)
            self.assertTrue(order_id.startswith("SIM-"), order_id)
            self.assertEqual(run.broker.get_order(order_id).state, result)
            line = (
                f"SIMULAÇÃO | ciclo {cycle_id} | tentativa {attempt} | ativo {asset}"
                f" | direção {direction} | valor {stake} centavos | ordem {order_id} | "
            )
            self.assertIn(line + "estado OPEN", run.out)
            self.assertIn(line + f"resultado {result}", run.out)

    def test_three_cycles_of_loss_loss_win(self):
        run = self.run_main("--cycles", "3", "--results", "LOSS,LOSS,WIN")
        self.assertEqual((run.code, run.err), (0, ""))
        cycles, reservations = self.rows()
        self.assertEqual(cycles, [(n, ASSET, CALL, WIN) for n in (1, 2, 3)])
        self.assertEqual(
            [row[1:5] for row in reservations],
            [
                (n, attempt, stake, result)
                for n in (1, 2, 3)
                for attempt, stake, result in ((1, 500, LOSS), (2, 1000, LOSS), (3, 2000, WIN))
            ],
        )
        self.assert_sent_once(run, reservations, ASSET, CALL)  # nine orders, one per entry
        self.assertEqual(run.sleep.calls, [0.25] * 9)  # one wait of the default interval each
        self.assertEqual(run.out.count("encerrado com WIN na tentativa 3"), 3)
        for text in (
            "SIMULAÇÃO LOCAL",
            "Os resultados vêm de --results (LOSS,LOSS,WIN)",
            "não há análise de mercado nem cálculo de lucratividade",
            "Saldo fictício de 10000 centavos",
            "Concluído: 3 ciclos encerrados e 9 ordens simuladas nesta execução.",
        ):
            self.assertIn(text, run.out)
        self.assert_released()

    def test_two_cycles_of_four_losses_end_without_a_fifth_entry(self):
        run = self.run_main(
            "--cycles", "2", "--results", "LOSS,LOSS,LOSS,LOSS",
            "--asset", "EURUSD_otc", "--direction", PUT, "--poll-interval", "0.5",
        )
        self.assertEqual((run.code, run.err), (0, ""))
        cycles, reservations = self.rows()
        self.assertEqual(cycles, [(n, "EURUSD_otc", PUT, LOSS) for n in (1, 2)])
        self.assertEqual(  # eight orders, and no attempt 5
            [row[1:5] for row in reservations],
            [
                (n, attempt, stake, LOSS)
                for n in (1, 2)
                for attempt, stake in ((1, 500), (2, 1000), (3, 2000), (4, 4000))
            ],
        )
        self.assert_sent_once(run, reservations, "EURUSD_otc", PUT)
        self.assertEqual(run.sleep.calls, [0.5] * 8)
        self.assertEqual(
            run.out.count("encerrado com LOSS na tentativa 4 (quarta LOSS: sem quinta entrada)"),
            2,
        )
        self.assertNotIn("tentativa 5", run.out)
        self.assert_released()

    def test_results_are_used_in_order_and_repeated(self):
        run = self.run_main("--cycles", "3", "--results", "LOSS,WIN,WIN")
        self.assertEqual((run.code, run.err), (0, ""))
        cycles, reservations = self.rows()
        self.assertEqual([row[3] for row in cycles], [WIN, WIN, WIN])
        # Cycle 1: LOSS, WIN. Cycle 2: WIN. Cycle 3, after the list starts again: LOSS, WIN.
        self.assertEqual(
            [row[1:5] for row in reservations],
            [
                (1, 1, 500, LOSS), (1, 2, 1000, WIN), (2, 1, 500, WIN),
                (3, 1, 500, LOSS), (3, 2, 1000, WIN),
            ],
        )
        self.assert_sent_once(run, reservations, ASSET, CALL)

    def test_ctrl_c_keeps_the_pending_and_releases_the_database(self):
        run = self.run_main(
            "--cycles", "3", "--results", "LOSS,LOSS,WIN", sleep=FakeSleep({2: press_ctrl_c})
        )
        self.assertEqual((run.code, run.err), (130, ""))
        cycles, reservations = self.rows()
        self.assertEqual(cycles, [(1, ASSET, CALL, None)])  # the cycle stays open
        self.assertEqual(
            [row[1:6] for row in reservations],
            [(1, 1, 500, LOSS, ACCEPTED), (1, 2, 1000, None, ACCEPTED)],
        )
        pending_id, pending_order = reservations[1][0], reservations[1][6]
        self.assertEqual(  # each entry sent once, nothing sent again
            [r.reservation_id for r in run.broker.requests], [row[0] for row in reservations]
        )
        self.assertEqual(run.broker.get_order(pending_order).state, ORDER_OPEN)  # no result invented
        self.assertIn(
            "Interrompido por Ctrl+C: 0 ciclos encerrados e 2 ordens simuladas nesta execução.",
            run.out,
        )
        self.assertIn(
            f"Pendência preservada: ciclo 1, tentativa 2, reserva {pending_id}, envio ACCEPTED,"
            f" ordem {pending_order}.",
            run.out,
        )
        self.assertIn(f"Banco preservado: {self.path}", run.out)
        self.assert_released()

    def test_database_with_an_open_restored_cycle_is_refused_untouched(self):
        # A pending reservation left by Ctrl+C during the wait of attempt 2.
        self.run_main("--cycles", "1", "--results", "LOSS,WIN", sleep=FakeSleep({2: press_ctrl_c}))
        interrupted = self.path
        # An open cycle without a pending reservation: attempt 1 lost, then the run stopped.
        after_loss = os.path.join(self.directory, "after-loss.db")
        coordinator = Coordinator(SqliteStore(after_loss))
        coordinator.resume()
        coordinator.start_cycle(ASSET, CALL)
        reservation = coordinator.reserve_next_entry(local_simulation.FICTITIOUS_BALANCE_CENTS)
        coordinator.begin_submission(reservation.reservation_id)
        coordinator.record_result(reservation.reservation_id, LOSS)
        coordinator.close()

        cases = (
            (interrupted, "da tentativa 2, envio ACCEPTED, ordem SIM-"),
            (after_loss, "sem reserva pendente; a próxima seria a tentativa 2"),
        )
        for path, detail in cases:
            with self.subTest(detail=detail):
                self.path = path
                rows = self.rows()
                run = self.run_main("--cycles", "1", "--results", "WIN")
                self.assertEqual(run.code, 1)
                self.assertIn(
                    f"PARADA: o banco {path} tem o ciclo 1 aberto desde uma execução anterior",
                    run.err,
                )
                self.assertIn(detail, run.err)
                self.assertIn(
                    "não reenvia ordens, não inventa resultados e não abandona ciclos restaurados",
                    run.err,
                )
                self.assertEqual((run.broker.requests, run.sleep.calls), ((), []))
                self.assertNotIn("SIMULAÇÃO |", run.out)
                self.assertEqual(self.rows(), rows)  # untouched
                self.assert_released()

    def test_storage_failure_stops_the_run_and_keeps_the_pending(self):
        def fail_writes():  # SQLite refuses every change to the reservations from now on
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_update BEFORE UPDATE ON reservations"
                    " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
                )
                connection.commit()

        run = self.run_main("--cycles", "1", "--results", "WIN", sleep=FakeSleep({1: fail_writes}))
        self.assertEqual(run.code, 1)
        self.assertIn("PARADA: o resultado WIN da ordem SIM-", run.err)
        self.assertIn("simulated write failure", run.err)
        self.assertIn("a reserva continua pendente", run.err)
        self.assertIn(f"Banco preservado: {self.path}", run.err)
        cycles, reservations = self.rows()
        self.assertEqual(cycles, [(1, ASSET, CALL, None)])
        self.assertEqual([row[1:6] for row in reservations], [(1, 1, 500, None, ACCEPTED)])
        self.assertEqual(len(run.broker.requests), 1)  # never sent again
        self.assert_released()

    def test_forever_runs_cycles_until_ctrl_c(self):
        run = self.run_main(
            "--forever", "--results", "LOSS,LOSS,WIN", sleep=FakeSleep({7: press_ctrl_c})
        )
        self.assertEqual((run.code, run.err), (130, ""))
        cycles, reservations = self.rows()
        self.assertEqual([row[3] for row in cycles], [WIN, WIN, None])  # the third stays open
        self.assertEqual(
            [row[3] for row in reservations], [500, 1000, 2000, 500, 1000, 2000, 500]
        )
        self.assertEqual(len(run.broker.requests), 7)
        self.assertIn("Execução: contínua, até Ctrl+C", run.out)
        self.assertIn(
            "Interrompido por Ctrl+C: 2 ciclos encerrados e 7 ordens simuladas nesta execução.",
            run.out,
        )
        self.assert_released()

    def test_ended_database_is_reused_and_numbering_goes_on(self):
        self.assertEqual(self.run_main("--cycles", "1", "--results", "WIN").code, 0)
        run = self.run_main("--cycles", "1", "--results", "WIN")
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertIn(
            "Banco existente: ciclos encerrados até o ciclo 1; a numeração continua.", run.out
        )
        self.assertIn("SIMULAÇÃO | ciclo 2 encerrado com WIN na tentativa 1", run.out)
        cycles, _ = self.rows()
        self.assertEqual(cycles, [(1, ASSET, CALL, WIN), (2, ASSET, CALL, WIN)])

    def test_invalid_arguments_are_refused_before_anything_is_created(self):
        cases = (
            [],
            ["--results", "WIN"],
            ["--cycles", "1"],
            ["--cycles", "1", "--forever", "--results", "WIN"],
            ["--cycles", "0", "--results", "WIN"],
            ["--cycles", "-2", "--results", "WIN"],
            ["--cycles", "1.5", "--results", "WIN"],
            ["--forever", "--results", ""],
            ["--forever", "--results", "LOSS,DRAW"],
            ["--forever", "--results", "loss,win"],
            ["--forever", "--results", "WIN,,LOSS"],
            ["--forever", "--results", "WIN", "--direction", "UP"],
            ["--forever", "--results", "WIN", "--asset", "  "],
            *(
                ["--forever", "--results", "WIN", "--poll-interval", value]
                for value in ("0", "-1", "nan", "inf", "abc")
            ),
        )
        for args in cases:
            with self.subTest(args=args):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        local_simulation.main(["--db", self.path, *args], sleep=FakeSleep())
                self.assertEqual(caught.exception.code, 2)
                self.assertFalse(os.path.exists(self.path))

    def test_default_databases_get_unique_names_in_runtime(self):
        runtime = Path(self.directory) / "runtime"
        first = local_simulation.default_database_path(runtime)
        second = local_simulation.default_database_path(runtime)
        self.assertTrue(runtime.is_dir())
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertEqual(path.parent, runtime)
            self.assertRegex(path.name, r"^simulation-\d{8}-\d{6}-[0-9a-f]{8}\.db$")
            self.assertFalse(path.exists())  # only the name is chosen; the run creates it

    def test_module_runs_as_a_program(self):
        completed = subprocess.run(
            [
                sys.executable, "-m", "pocketautonomo.local_simulation", "--cycles", "1",
                "--results", "LOSS,WIN", "--poll-interval", "0.01", "--db", self.path,
            ],
            cwd=ROOT,
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=60,
        )
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertIn("SIMULAÇÃO | ciclo 1 encerrado com WIN na tentativa 2", completed.stdout)
        self.assertIn(
            "Concluído: 1 ciclo encerrado e 2 ordens simuladas nesta execução.", completed.stdout
        )


if __name__ == "__main__":
    unittest.main()
