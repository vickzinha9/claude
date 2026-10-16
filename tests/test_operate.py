"""Tests of the simulated operation's program, pocketautonomo.operate.

No test here opens a browser or reaches the network. The live runs use FakePlatform and
FakeScanSession of tests/test_scan.py, a trading screen in memory with its own clock, and an
analysis history built from the synthetic series of tests/test_analysis.py, so that a rule
validates: the decisions, quotes and results are ARTIFICIAL and say nothing about the market.
"""

import ast
import contextlib
import csv
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_scan and test_analysis

import test_scan as ts  # noqa: E402
from test_analysis import legs  # noqa: E402

from pocketautonomo import analysis, observe, operate, operation, scan  # noqa: E402
from pocketautonomo.coordinator import Coordinator  # noqa: E402
from pocketautonomo.cycle import CANCELLED, LOSS  # noqa: E402
from pocketautonomo.paper_broker import PaperBroker  # noqa: E402
from pocketautonomo.storage import SqliteStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def csv_moment(text):
    """A UTC moment of the operation's CSV."""
    return datetime.fromisoformat(text)


class RisingPlatform(ts.FakePlatform):
    """Every quote rises slowly with the platform's clock: a PUT always loses."""

    def quote(self, index):
        base = self.assets[index][2]
        return None if base is None else f"{base + 0.000001 * self.t:.5f}"


def validated_history(name):
    """A history whose series for name validates a trend rule and ends just before ts.START."""
    series = legs(6000)
    shift = ts.START - timedelta(seconds=30) - series.samples[-1].at
    history = analysis.History()
    for sample in series.samples:
        history.add(name, analysis.Sample(sample.at + shift, sample.price, sample.quote, sample.payout))
    return history


class OperateTestCase(ts.ScanTestCase):
    """A runtime directory with the collector's sources and the favorites selector."""

    def run_operation(self, *args, platform, history=None, on_wait=None):
        session = ts.FakeScanSession(platform, on_wait)
        self.session = session
        patches = contextlib.ExitStack()
        if history is not None:
            patches.enter_context(mock.patch.object(scan.analysis, "load_history", return_value=history))
        out, err = io.StringIO(), io.StringIO()
        with patches, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = operate.main(
                ["--url", ts.URL, *args],
                open_session=lambda profile: session,
                read_line=ts.Answers(("",)),
                now=platform.now,
                monotonic=platform.monotonic,
            )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def operation_rows(self):
        """The rows of every operation CSV, in the order of the runs."""
        rows = []
        for path in sorted(self.runtime.glob("operacao-2*.csv")):
            with open(path, newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                self.assertEqual(tuple(reader.fieldnames), operation.OPERATION_COLUMNS)
                rows += list(reader)
        return rows


class LiveOperationTest(OperateTestCase):
    def test_a_validated_decision_runs_a_cycle_on_the_screen_without_clicks(self):
        platform = RisingPlatform()
        code = self.run_operation("--cycles", "1", platform=platform, history=validated_history(ts.EUR[0]))
        self.assertEqual((code, self.err), (0, ""), self.out)
        rows = self.operation_rows()
        started = [row for row in rows if row["event"] == "ciclo iniciado"]
        self.assertEqual([(row["asset"], row["direction"]) for row in started], [(ts.EUR[0], "PUT")])
        entries = [row for row in rows if row["event"] == "entrada simulada enviada"]
        self.assertEqual([row["stake_cents"] for row in entries], ["500", "1000", "2000", "4000"])
        self.assertEqual([row["result"] for row in rows if row["event"] == "resultado simulado"], [LOSS] * 4)
        self.assertEqual(rows[-1]["outcome"], LOSS)
        self.assertIn("sem quinta entrada", rows[-1]["reason"])
        self.assertTrue(all(row["execution"] == "simulada" for row in rows))
        self.assertTrue(entries[0]["reading_ms"] and entries[0]["decision_ms"] and entries[0]["simulated_submit_ms"])
        # The navigation stayed on EUR/USD OTC: no click while the cycle ran, then the scan went on.
        begin, end = (
            (csv_moment(rows[0]["at_utc"]) - ts.START).total_seconds(),
            (csv_moment(rows[-1]["at_utc"]) - ts.START).total_seconds(),
        )
        self.assertGreater(end - begin, 4 * 300)  # four attempts of 300 s
        self.assertEqual([index for moment, index in platform.clicks if begin <= moment <= end], [])
        self.assertEqual([index for moment, index in platform.clicks if moment > end], [1, 2])
        self.assertGreater(self.session.waits.count(operate.POLL_SECONDS), 2000)  # the readings of the cycle
        # The cycle's readings never went to the scan's CSV: one line per favorite.
        self.assertEqual([row["asset"] for row in self.rows()], [ts.EUR[0], ts.GBP[0], ts.AUD[0]])
        timings = next(self.runtime.glob("tempos-scan-*.csv")).read_text(encoding="utf-8")
        self.assertIn(operate.HOLD_MOMENT, timings)
        for text in (
            "ESTADO: COLETANDO", "ESTADO: PENDÊNCIA", "ESTADO: CICLO ATIVO", "ESTADO: AGUARDANDO DECISÃO",
            "Navegação entre favoritos suspensa: o ciclo de operação fica em EUR/USD OTC",
            "Ciclo de operação encerrado: a navegação entre favoritos continua.",
            "Operação simulada: 1 ciclo iniciado, 4 entradas simuladas",
            operation.SIMULATOR_TIME_NOTE,
        ):
            self.assertIn(text, self.out)

    def test_without_a_validated_rule_the_scan_only_collects(self):
        code = self.run_operation("--cycles", "2", "--interval", "3", platform=ts.FakePlatform())
        self.assertEqual((code, self.err), (0, ""), self.out)
        self.assertEqual({row["analysis_decision"] for row in self.rows()}, {"AGUARDAR"})
        self.assertEqual(list(self.runtime.glob("operacao-2*.csv")), [])  # no operation event at all
        self.assertIn("ESTADO: COLETANDO — sem regra validada, a análise responde AGUARDAR; a coleta de dados continua", self.out)
        self.assertNotIn("ESTADO: PENDÊNCIA", self.out)
        self.assertEqual(self.session.clicked(), [1, 2, 0, 1, 2])
        self.assertIn("Operação simulada: 0 ciclos iniciados, 0 entradas simuladas", self.out)

    def test_ctrl_c_during_a_cycle_keeps_the_pending_entry_for_the_next_start(self):
        platform = RisingPlatform()
        polls = []

        def press_ctrl_c(seconds):
            if any(self.runtime.glob("operacao-2*.csv")) and seconds == operate.POLL_SECONDS:
                polls.append(seconds)
                if len(polls) == 20:
                    raise KeyboardInterrupt

        code = self.run_operation("--cycles", "1", platform=platform, history=validated_history(ts.EUR[0]), on_wait=press_ctrl_c)
        self.assertEqual(code, 130, self.err)
        self.assertIn("Pendência preservada: ciclo 1, tentativa 1 (R$ 5,00)", self.out)
        self.assertIn("ordem simulada SIM-", self.out)
        self.assertEqual(self.session.closes, 1)
        # The second run, after the order's time passed: the restored entry is checked, not resent.
        platform = ts.FakePlatform()
        platform.t = 400.0
        code = self.run_operation("--cycles", "1", platform=platform)
        self.assertEqual(code, 0, self.err)
        recovered = self.operation_rows()
        results = [row for row in recovered if row["event"] == "resultado simulado"]
        self.assertEqual([(row["attempt"], row["result"]) for row in results], [("1", CANCELLED)])
        self.assertIn("sem cotação observada no vencimento", results[0]["reason"])
        self.assertEqual(sum(row["event"] == "entrada simulada enviada" for row in recovered), 1)
        broker = PaperBroker(self.runtime / operate.ORDERS_NAME, 300.0, platform.now)
        self.addCleanup(broker.close)
        self.assertEqual([order.state for order in broker.orders()], [CANCELLED])
        coordinator = Coordinator(SqliteStore(self.runtime / operate.DATABASE_NAME))  # the locks were released
        self.addCleanup(coordinator.close)
        self.assertEqual(coordinator.status().outcome, CANCELLED)

    def test_closing_the_browser_during_a_cycle_is_not_a_success(self):
        platform = RisingPlatform()
        polls = []

        def close_window(seconds):
            if any(self.runtime.glob("operacao-2*.csv")) and seconds == operate.POLL_SECONDS:
                polls.append(seconds)
                if len(polls) == 20:
                    self.session.window_closed = True

        code = self.run_operation("--cycles", "1", platform=platform, history=validated_history(ts.EUR[0]), on_wait=close_window)
        self.assertEqual(code, 1, self.out)
        self.assertIn("A janela do navegador foi fechada; a varredura terminou.", self.out)
        self.assertIn("PARADA: a varredura terminou com um ciclo de operação aberto", self.err)
        self.assertIn("Pendência preservada: ciclo 1, tentativa 1", self.out)

    def test_a_storage_failure_stops_the_run_and_the_next_start_recovers(self):
        class FailingStart(SqliteStore):
            def save_submission_start(self, reservation_id):
                raise operate.StorageError("simulated write failure")

        with mock.patch.object(operate, "SqliteStore", FailingStart):
            code = self.run_operation("--cycles", "1", platform=RisingPlatform(), history=validated_history(ts.EUR[0]))
        self.assertEqual(code, 1, self.out)
        self.assertIn("PARADA: o início do envio não pôde ser gravado", self.err)
        self.assertIn("Estado final: PAUSA.", self.out)
        self.assertEqual(self.session.clicked(), [])  # it stopped right after the first visit
        code = self.run_operation("--cycles", "1", platform=ts.FakePlatform())
        self.assertEqual(code, 0, self.err)
        recovered = [row for row in self.operation_rows() if row["event"] == operation.RECOVERY]
        self.assertEqual(len(recovered), 1)
        self.assertIn("nunca foi enviada (RESERVED)", recovered[0]["reason"])

    def test_another_asset_on_the_screen_never_settles_the_cycle(self):
        platform = RisingPlatform()

        def switch_screen(seconds):
            if any(self.runtime.glob("operacao-2*.csv")) and platform.named == 0:
                platform.named = platform.paid = platform.drawn = 1  # someone selected GBP/USD OTC

        code = self.run_operation("--cycles", "1", platform=platform, history=validated_history(ts.EUR[0]), on_wait=switch_screen)
        self.assertEqual(code, 0, self.err)
        self.assertIn(
            "leitura durante o ciclo não usada: a tela mostra GBP/USD OTC, e não EUR/USD OTC; a operação não"
            " clica durante o ciclo",
            self.out,
        )
        rows = self.operation_rows()
        self.assertEqual([row["result"] for row in rows if row["event"] == "resultado simulado"], [CANCELLED])
        self.assertEqual(sum(row["event"] == "entrada simulada enviada" for row in rows), 1)  # no gale
        self.assertEqual(rows[-1]["outcome"], CANCELLED)


class DemonstrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name) / "runtime"
        patcher = mock.patch.object(observe, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def demonstrate(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = operate.main(["--demonstracao", *args])
        return code, out.getvalue(), err.getvalue()

    def test_the_demonstration_passes_its_checks_in_files_of_its_own(self):
        code, out, err = self.demonstrate("--configure")  # the launcher always passes --configure
        self.assertEqual((code, err), (0, ""), out)
        for number in range(1, 9):
            self.assertIn(f"CENA {number}: ", out)
        self.assertNotIn("PARADA", out)
        self.assertEqual(out.count("  conferido: "), 10)
        self.assertIn("Demonstração concluída: as 8 cenas passaram nas conferências", out)
        self.assertIn("DEMONSTRAÇÃO | PENDÊNCIA | entrada simulada enviada", out)
        names = sorted(path.name for path in self.runtime.iterdir() if not path.name.endswith("-lock"))
        self.assertEqual(len(names), 3, names)
        self.assertTrue(all(name.startswith("demonstracao-") for name in names), names)
        [log] = self.runtime.glob("demonstracao-operacao-*.csv")
        with open(log, newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual({row["execution"] for row in rows}, {operation.DEMONSTRATION})
        self.assertEqual(analysis.load_history(self.runtime).files, 0)  # nothing for the analysis
        code, out, err = self.demonstrate()  # a second run gets new files and passes again
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(list(self.runtime.glob("demonstracao-operacao-*.csv"))), 2)


class CommandLineTest(unittest.TestCase):
    def test_runs_with_python_dash_m(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")

            def run(*args):
                return subprocess.run(
                    [sys.executable, "-m", "pocketautonomo.operate", *args],
                    cwd=directory, env=env, capture_output=True, encoding="utf-8", timeout=120,
                )

            shown = run("--help")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            for option in ("--demonstracao", "--gale-window", "--horizon", "--cycles", "--configure"):
                self.assertIn(option, shown.stdout)
            for args in (("--gale-window", "0.5"), ("--horizon", "5"), ("--url", "http://example.org/")):
                with self.subTest(args=args):
                    self.assertEqual(run(*args).returncode, 2)
            self.assertFalse((Path(directory) / "runtime").exists())
            demo = run("--demonstracao")
            self.assertEqual(demo.returncode, 0, demo.stdout + demo.stderr)
            self.assertIn("Demonstração concluída", demo.stdout)
            created = sorted(path.name for path in (Path(directory) / "runtime").iterdir())
            self.assertTrue(created and all(name.startswith("demonstracao-") for name in created), created)


class SafetyTest(unittest.TestCase):
    def test_the_operation_never_clicks_and_reaches_no_broker_or_network(self):
        forbidden = {"playwright", "requests", "urllib", "socket", "http", "websocket", "websockets", "aiohttp"}
        for module in (operate, operation, sys.modules["pocketautonomo.paper_broker"]):
            with self.subTest(module=module.__name__):
                tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported |= {alias.name.split(".")[0] for alias in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        imported.add((node.module or "").split(".")[0])
                self.assertFalse(imported & forbidden, imported)
                names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                self.assertEqual({name for name in names if "click" in name.lower()}, set())
                self.assertNotIn("SimulatedBroker", names)  # only the persisted paper broker
        hold = ast.parse(Path(operate.__file__).read_text(encoding="utf-8"))
        [function] = [node for node in hold.body if isinstance(node, ast.FunctionDef) and node.name == "hold"]
        calls = {
            f"{node.func.value.id}.{node.func.attr}"
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
        }
        session_calls = {call for call in calls if call.startswith("session.")}
        self.assertEqual(session_calls, {"session.wait"})  # it reads only through scan.read_poll
        self.assertIn("scan.read_poll", calls)


if __name__ == "__main__":
    unittest.main()
