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
from pocketautonomo.cycle import CANCELLED, LOSS, WIN  # noqa: E402
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


class PauseOperationTest(OperateTestCase):
    """After a switch that was not confirmed, nothing read in the run starts a cycle: the PAUSA."""

    TARGET = ts.CLOSE[2][0]  # AUD/CAD OTC, payout 91%, visited right after the abandoned click

    def test_the_rule_starts_a_cycle_on_a_clean_screen(self):
        # The control: the same rule and the same asset, without any switch that failed.
        code = self.run_operation(
            "--cycles", "1", "--load-timeout", "5", platform=ts.FakePlatform(assets=ts.CLOSE),
            history=validated_history(self.TARGET),
        )
        self.assertEqual((code, self.err), (0, ""), self.out)
        started = [row for row in self.operation_rows() if row["event"] == "ciclo iniciado"]
        self.assertEqual([row["asset"] for row in started], [self.TARGET])
        self.assertNotIn("PAUSA", self.out)

    def test_without_the_pause_the_contaminated_reading_would_start_a_cycle(self):
        # What the PAUSA blocks: with it switched off, the wrong chart read under the right name passes
        # every check and opens a cycle with another asset's quote.
        platform = ts.PersistentLeakPlatform(ts.CLOSE)
        with mock.patch.object(scan, "with_pause", lambda state, visit: visit):
            self.run_operation(
                "--cycles", "1", "--load-timeout", "5", platform=platform, history=validated_history(self.TARGET),
            )
        [started] = [row for row in self.operation_rows() if row["event"] == "ciclo iniciado"]
        self.assertEqual(started["asset"], self.TARGET)
        self.assertTrue(ts.in_range(started["quote"], ts.CLOSE[1][2]), started)  # GBP's chart, not AUD's
        self.assertFalse(ts.in_range(started["quote"], ts.CLOSE[2][2]), started)

    def test_a_contaminated_screen_never_starts_a_cycle_even_with_a_validated_rule(self):
        platform = ts.PersistentLeakPlatform(ts.CLOSE)  # the wrong chart, with close quotes, for good
        code = self.run_operation(
            "--cycles", "3", "--interval", "3", "--load-timeout", "5", platform=platform,
            history=validated_history(self.TARGET),
        )
        self.assertEqual(code, 1, self.out)
        rows = self.operation_rows()
        self.assertEqual([row["event"] for row in rows], [operation.SCAN_PAUSED])  # no decision, no cycle
        self.assertTrue(rows[0]["reason"].startswith(f"a troca para {ts.GBP[0]} no ciclo 1 não foi confirmada"), rows[0])
        target = [row for row in self.rows() if row["asset"] == self.TARGET]
        self.assertEqual(len(target), 3)  # visited in each cycle, under the right name
        for row in target:
            self.assertEqual((row["price"], row["analysis_ready"], row["analysis_decision"]), ("", "não", "AGUARDAR"))
        self.assertEqual(list(self.runtime.glob(f"{analysis.CYCLE_PREFIX}*.csv")), [])  # no cycle quote either
        broker = PaperBroker(self.runtime / operate.ORDERS_NAME, 300.0, platform.now)
        self.addCleanup(broker.close)
        self.assertEqual(broker.orders(), [])
        for text in (
            f"ESTADO: PAUSA — varredura em PAUSA: a troca para {ts.GBP[0]} no ciclo 1 não foi confirmada",
            "Operação simulada: 0 ciclos iniciados, 0 entradas simuladas",
            "Estado final: PAUSA.",
        ):
            self.assertIn(text, self.out)
        self.assertIn(f"PAUSA: a troca para {ts.GBP[0]} no ciclo 1 não foi confirmada", self.err)
        self.assertNotIn("Navegação entre favoritos suspensa", self.out)
        # The next run starts without the PAUSA, from the recovery, and keeps the database.
        coordinator = Coordinator(SqliteStore(self.runtime / operate.DATABASE_NAME))
        self.addCleanup(coordinator.close)
        self.assertIsNone(coordinator.status().cycle_id)


class FallingPlatform(ts.FakePlatform):
    """Every quote falls slowly with the platform's clock: a PUT always wins."""

    def quote(self, index):
        base = self.assets[index][2]
        return None if base is None else f"{base - 0.000001 * self.t:.5f}"


class CycleQuotesTest(OperateTestCase):
    """The real quotes read during a cycle reach the analysis' history, with their origin."""

    def redirect_output(self, out):
        redirect = contextlib.redirect_stdout(out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def cycle_rows(self):
        """The rows of every cotacoes-ciclo file, in the order of the runs."""
        rows = []
        for path in sorted(self.runtime.glob(f"{analysis.CYCLE_PREFIX}*.csv")):
            with open(path, newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                self.assertEqual(tuple(reader.fieldnames), operate.CYCLE_QUOTE_COLUMNS)
                rows += list(reader)
        return rows

    def check_rows(self, rows, attempts):
        self.assertTrue(rows)
        self.assertEqual({row["origin"] for row in rows}, {analysis.ORIGIN_CYCLE})
        self.assertEqual({row["analysis_ready"] for row in rows}, {"sim"})
        self.assertEqual({row["asset"] for row in rows}, {ts.EUR[0]})
        self.assertEqual({row["attempt"] for row in rows}, {str(attempt) for attempt in attempts})
        self.assertLessEqual({row["operation_state"] for row in rows}, {operation.PENDING_ENTRY, operation.ACTIVE_CYCLE})
        moments = [csv_moment(row["price_captured_at_utc"]) for row in rows]
        self.assertEqual(moments, sorted(set(moments)))  # each quote once, in time order
        self.assertTrue(all(float(row["data_age_ms"]) <= scan.MAX_DATA_AGE_SECONDS * 1000 for row in rows))

    def used_quotes(self, events):
        """The capture times of the quotes that the operation used in these events."""
        return [row["quote_captured_at_utc"] for row in self.operation_rows() if row["event"] in events]

    def test_the_quotes_of_a_cycle_with_four_losses_reach_the_history(self):
        history = validated_history(ts.EUR[0])
        before = dict(history.origins)
        code = self.run_operation("--cycles", "1", platform=RisingPlatform(), history=history)
        self.assertEqual((code, self.err), (0, ""), self.out)
        self.assertEqual([row["result"] for row in self.operation_rows() if row["event"] == "resultado simulado"], [LOSS] * 4)
        rows = self.cycle_rows()
        self.check_rows(rows, attempts=(1, 2, 3, 4))
        self.assertGreater(len(rows), 2000)  # about 0.5 s apart during the four attempts of 300 s
        captured = {row["price_captured_at_utc"] for row in rows}
        settlements = self.used_quotes({"resultado simulado"})
        gales = self.used_quotes({"entrada simulada enviada"})[1:]  # the first entry is the visit's quote
        self.assertEqual(len(settlements), 4)
        self.assertEqual(len(gales), 3)
        self.assertLessEqual(set(settlements) | set(gales), captured)  # the quotes that settled and entered
        # The running analysis got them at once, and the files give the same quotes back.
        self.assertEqual(history.origins.get(analysis.ORIGIN_CYCLE, 0) - before.get(analysis.ORIGIN_CYCLE, 0), len(rows))
        reloaded = analysis.load_history(self.runtime)
        self.assertEqual(reloaded.origins, {analysis.ORIGIN_SCAN: 3, analysis.ORIGIN_CYCLE: len(rows)})
        self.assertEqual((reloaded.usable, reloaded.skipped, reloaded.cycle_files), (3 + len(rows), {}, 1))
        self.assertIn(f"Cotações de mercado lidas durante os ciclos: {len(rows)}, com a origem ciclo", self.out)
        # Nothing of the cycle went to the scan's CSV: still one line per favorite.
        self.assertEqual([row["asset"] for row in self.rows()], [ts.EUR[0], ts.GBP[0], ts.AUD[0]])

    def test_the_quotes_of_a_cycle_won_at_the_first_attempt_reach_the_history(self):
        code = self.run_operation("--cycles", "1", platform=FallingPlatform(), history=validated_history(ts.EUR[0]))
        self.assertEqual((code, self.err), (0, ""), self.out)
        results = [row["result"] for row in self.operation_rows() if row["event"] == "resultado simulado"]
        self.assertEqual(results, [WIN])
        rows = self.cycle_rows()
        self.check_rows(rows, attempts=(1,))
        self.assertGreater(len(rows), 500)
        [settlement] = self.used_quotes({"resultado simulado"})
        self.assertIn(settlement, {row["price_captured_at_utc"] for row in rows})
        self.assertEqual(analysis.load_history(self.runtime).origins[analysis.ORIGIN_CYCLE], len(rows))

    def test_the_payout_never_decides_what_is_recorded(self):
        platform = RisingPlatform()

        def change_payout(seconds):
            if any(self.runtime.glob("operacao-2*.csv")):
                platform.assets[0][1] = "+80%" if platform.t < 400 else None if platform.t < 800 else "+92%"

        code = self.run_operation("--cycles", "1", platform=platform, history=validated_history(ts.EUR[0]), on_wait=change_payout)
        self.assertEqual(code, 0, self.err)
        entries = [row for row in self.operation_rows() if row["event"] == "entrada simulada enviada"]
        self.assertEqual([row["stake_cents"] for row in entries], ["500", "1000", "2000", "4000"])  # gales need no payout
        rows = self.cycle_rows()
        self.check_rows(rows, attempts=(1, 2, 3, 4))
        payouts = {row["payout_percent"] for row in rows}
        self.assertLessEqual({"80", "", "92"}, payouts)
        missing = [row for row in rows if row["payout_percent"] == ""]
        self.assertTrue(all(row["payout_note"].startswith("payout indisponível: ") for row in missing))
        self.assertEqual({row["payout_note"] for row in rows if row["payout_percent"] == "80"}, {operate.PAYOUT_CONFIRMED})
        reloaded = analysis.load_history(self.runtime)
        series = reloaded.get(ts.EUR[0])
        cycle = [sample for sample in series.samples if sample.origin == analysis.ORIGIN_CYCLE]
        self.assertEqual(len(cycle), len(rows))
        self.assertEqual({sample.payout for sample in cycle}, {80.0, None, 92.0})

    def test_restarting_neither_loses_nor_duplicates_the_quotes(self):
        platform = RisingPlatform()
        polls = []

        def press_ctrl_c(seconds):
            if any(self.runtime.glob("operacao-2*.csv")) and seconds == operate.POLL_SECONDS:
                polls.append(seconds)
                if len(polls) == 40:
                    raise KeyboardInterrupt

        code = self.run_operation("--cycles", "1", platform=platform, history=validated_history(ts.EUR[0]), on_wait=press_ctrl_c)
        self.assertEqual(code, 130, self.err)
        first = self.cycle_rows()
        self.assertGreater(len(first), 30)  # written line by line, before Ctrl+C
        # The next start loads the scan's line and every quote of the cycle, once.
        platform = ts.FakePlatform()
        platform.t = 400.0
        code = self.run_operation("--cycles", "1", platform=platform)
        self.assertEqual(code, 0, self.err)
        self.assertIn(f"{1 + len(first)} cotações utilizáveis de 2 arquivos anteriores", self.out)
        reloaded = analysis.load_history(self.runtime)
        self.assertEqual(reloaded.origins, {analysis.ORIGIN_SCAN: 4, analysis.ORIGIN_CYCLE: len(first)})
        self.assertEqual((reloaded.usable, reloaded.skipped), (4 + len(first), {}))
        self.assertEqual(self.cycle_rows(), first)  # the restored entry was settled at once: no new lines
        again = analysis.load_history(self.runtime)
        self.assertEqual((again.usable, again.origins), (reloaded.usable, reloaded.origins))

    def test_the_quotes_that_follow_a_restored_entry_are_recorded_too(self):
        polls = []

        def press_ctrl_c(seconds):
            if any(self.runtime.glob("operacao-2*.csv")) and seconds == operate.POLL_SECONDS:
                polls.append(seconds)
                if len(polls) == 20:
                    raise KeyboardInterrupt

        code = self.run_operation("--cycles", "1", platform=RisingPlatform(), history=validated_history(ts.EUR[0]), on_wait=press_ctrl_c)
        self.assertEqual(code, 130, self.err)
        first = len(self.cycle_rows())
        platform = RisingPlatform()
        platform.t = 30.0  # the simulated order of the first run is still open
        code = self.run_operation("--cycles", "1", platform=platform)
        self.assertEqual(code, 0, self.err)
        self.assertIn(
            f"{1 + first} cotações utilizáveis de 2 arquivos anteriores (1 de visitas da varredura e {first} de"
            " leituras da tela durante ciclos da operação simulada)",
            self.out,
        )  # the second start loads the quotes of the first one
        rows = self.cycle_rows()[first:]  # the second run's file
        self.assertGreater(len(rows), 400)  # from 30 s to the expiry, 0.5 s apart
        self.assertEqual({(row["cycle_id"], row["attempt"], row["operation_state"]) for row in rows}, {("1", "1", operation.PENDING_ENTRY)})
        results = [row for row in self.operation_rows() if row["event"] == "resultado simulado"]
        self.assertEqual([row["result"] for row in results], [LOSS])
        self.assertIn(results[0]["quote_captured_at_utc"], {row["price_captured_at_utc"] for row in rows})
        reloaded = analysis.load_history(self.runtime)
        self.assertEqual(reloaded.origins[analysis.ORIGIN_CYCLE], first + len(rows))
        self.assertEqual(reloaded.skipped, {})

    def test_a_reading_on_which_the_operation_stops_is_still_recorded(self):
        calls = []
        original = operation.Operator.observe

        def observe(operator, *args, **kwargs):
            calls.append(args[1])  # the capture time of the reading
            if len(calls) == 30:
                raise operation.OperationStopped("parada de teste durante o ciclo")
            return original(operator, *args, **kwargs)

        with mock.patch.object(operation.Operator, "observe", observe):
            code = self.run_operation("--cycles", "1", platform=RisingPlatform(), history=validated_history(ts.EUR[0]))
        self.assertEqual(code, 1, self.out)
        self.assertIn("PARADA: parada de teste durante o ciclo", self.err)
        rows = self.cycle_rows()
        self.assertEqual(len(rows), 30)  # also the reading on which the operation stopped
        self.assertEqual(csv_moment(rows[-1]["price_captured_at_utc"]), calls[-1].replace(microsecond=calls[-1].microsecond // 1000 * 1000))
        self.assertEqual(analysis.load_history(self.runtime).origins[analysis.ORIGIN_CYCLE], 30)

    def test_after_a_cycle_the_next_decision_comes_only_from_a_new_visit(self):
        considered = []
        original = operation.Operator.consider

        def consider(operator, decision, *args, **kwargs):
            considered.append((decision.asset, decision.action, decision.sample.origin if decision.sample else None))
            return original(operator, decision, *args, **kwargs)

        with mock.patch.object(operation.Operator, "consider", consider):
            code = self.run_operation(
                "--cycles", "2", "--interval", "3", platform=RisingPlatform(), history=validated_history(ts.EUR[0])
            )
        self.assertEqual(code, 0, self.err)
        # One decision per visit, from the visit's own quote, never from a reading of a cycle.
        self.assertEqual([asset for asset, _, _ in considered], [ts.EUR[0], ts.GBP[0], ts.AUD[0]] * 2)
        self.assertEqual({origin for _, _, origin in considered} - {None}, {analysis.ORIGIN_SCAN})
        started = [row for row in self.operation_rows() if row["event"] == "ciclo iniciado"]
        self.assertEqual([row["asset"] for row in started], [ts.EUR[0], ts.EUR[0]])
        first, second = started
        self.assertNotEqual(first["decision_id"], second["decision_id"])  # a new decision, of the new visit
        self.assertEqual(second["payout_percent"], "92.0")  # the visit's payout, checked for the first entry
        ended = [row for row in self.operation_rows() if row["event"] == "ciclo encerrado"]
        self.assertLess(csv_moment(ended[0]["at_utc"]), csv_moment(second["at_utc"]))  # only after the first ended
        # The second decision looked back at the quotes of the first cycle: they are the history.
        rows = self.cycle_rows()
        self.assertEqual({row["cycle_id"] for row in rows}, {"1", "2"})

    def test_a_decision_gets_its_hypothetical_result_from_a_quote_of_the_cycle(self):
        code = self.run_operation("--cycles", "1", platform=RisingPlatform(), history=validated_history(ts.EUR[0]))
        self.assertEqual(code, 0, self.err)
        horizon = analysis.DEFAULT_HORIZON
        history = analysis.load_history(self.runtime)  # the real files only: no synthetic quote
        series = history.get(ts.EUR[0])
        [visit] = [sample for sample in series.samples if sample.origin == analysis.ORIGIN_SCAN]
        label = series.first_at_or_after(visit.at + timedelta(seconds=horizon), analysis.tolerance(horizon))
        self.assertEqual(label.origin, analysis.ORIGIN_CYCLE)
        self.assertEqual(analysis.outcome(series, visit, horizon, "PUT"), LOSS)  # the quotes rose
        self.assertEqual(analysis.outcome(series, visit, horizon, "CALL"), WIN)
        without = analysis.load_history(self.runtime, exclude=list(self.runtime.glob(f"{analysis.CYCLE_PREFIX}*.csv")))
        self.assertIsNone(analysis.outcome(without.get(ts.EUR[0]), visit, horizon, "PUT"))  # as before this change

    def test_recording_starts_no_cycle_and_does_not_validate_at_each_reading(self):
        considered = []
        original = operation.Operator.consider

        def consider(operator, decision, *args, **kwargs):
            considered.append(decision.asset)
            return original(operator, decision, *args, **kwargs)

        with mock.patch.object(operation.Operator, "consider", consider), mock.patch.object(
            analysis, "validate", wraps=analysis.validate
        ) as validate:
            code = self.run_operation("--cycles", "1", platform=RisingPlatform(), history=validated_history(ts.EUR[0]))
        self.assertEqual(code, 0, self.err)
        self.assertGreater(len(self.cycle_rows()), 2000)
        self.assertEqual(considered, [ts.EUR[0], ts.GBP[0], ts.AUD[0]])  # one per visit, none per reading
        self.assertLess(validate.call_count, 10)  # each visit and the end of the scan's cycle, not each reading
        started = [row for row in self.operation_rows() if row["event"] == "ciclo iniciado"]
        self.assertEqual(len(started), 1)
        self.assertEqual(self.session.clicked(), [1, 2])

    def test_a_quote_that_is_old_or_repeated_is_not_recorded(self):
        out = io.StringIO()
        self.redirect_output(out)
        warnings = []
        quotes = operate.CycleQuotes(self.runtime, warnings.append)
        analyzer = analysis.Analyzer(analysis.DEFAULT_HORIZON)
        seen = operation.Seen(ts.EUR[0], ts.START, 1.0853, "1.08530", 92.0, 20.0, operate.PAYOUT_CONFIRMED)
        context = {"cycle_id": 1, "attempt": 1, "operation_state": operation.PENDING_ENTRY}
        self.assertTrue(quotes.record(seen, ts.START + timedelta(seconds=0.02), context, analyzer))
        self.assertFalse(quotes.record(seen, ts.START + timedelta(seconds=0.5), context, analyzer))  # the same quote
        stale = operation.Seen(ts.EUR[0], ts.START + timedelta(seconds=1), 1.0854, "1.08540")
        self.assertFalse(quotes.record(stale, ts.START + timedelta(seconds=6.5), context, analyzer))  # 5,5 s old
        quotes.close()
        self.assertEqual((quotes.recorded, quotes.written, warnings), (1, 1, []))
        self.assertEqual(analyzer.history.origins, {analysis.ORIGIN_CYCLE: 1})
        [row] = self.cycle_rows()
        self.assertEqual((row["price"], row["payout_percent"], row["data_age_ms"]), ("1.08530", "92", "20.0"))
        # A file that cannot be written: one warning, and the quote stays in this run's history.
        blocked = self.runtime / "bloqueado"
        blocked.write_text("not a folder", encoding="utf-8")
        failing = operate.CycleQuotes(blocked, warnings.append)
        later = operation.Seen(ts.EUR[0], ts.START + timedelta(seconds=2), 1.0855, "1.08550")
        self.assertTrue(failing.record(later, ts.START + timedelta(seconds=2.1), context, analyzer))
        self.assertTrue(failing.record(operation.Seen(ts.EUR[0], ts.START + timedelta(seconds=3), 1.0856, "1.08560"), ts.START + timedelta(seconds=3.1), context, analyzer))
        self.assertEqual((failing.recorded, failing.written, len(warnings)), (2, 0, 1))
        self.assertIn("ficam só na análise desta execução", warnings[0])
        self.assertEqual(analyzer.history.origins, {analysis.ORIGIN_CYCLE: 3})
        # Once the folder can be written again, a later quote goes to a new file.
        blocked.unlink()
        blocked.mkdir()
        retry = ts.START + timedelta(seconds=2.1 + operate.WRITE_RETRY_SECONDS)
        recovered = operation.Seen(ts.EUR[0], retry - timedelta(seconds=0.1), 1.0857, "1.08570")
        self.assertTrue(failing.record(recovered, retry, context, analyzer))
        failing.close()
        self.assertEqual((failing.recorded, failing.written, len(failing.paths), len(warnings)), (3, 1, 1, 1))
        self.assertEqual(analysis.load_history(blocked).origins, {analysis.ORIGIN_CYCLE: 1})


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
