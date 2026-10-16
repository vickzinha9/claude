"""Tests of the local CALL/PUT analysis, pocketautonomo.analysis.

The series are synthetic, built here with the standard library: a price that moves in long
straight legs, where following the last move is right most of the time, and a seeded random
walk, where no rule can be. They show the rules of the validation and of the decision, not
that any rule works on the real platform: only quotes recorded there can say that.
"""

import contextlib
import csv
import io
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pocketautonomo import analysis as a
from pocketautonomo import scan

ROOT = Path(__file__).resolve().parent.parent
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
HORIZON = 300.0


def legs(n, step=30.0, payout=92.0):
    """A price in straight legs of 20 horizons up and 20 down: the last move tells the next."""
    series = a.Series("TESTE/TENDÊNCIA OTC")
    period = 40 * HORIZON
    for i in range(n):
        phase = (i * step % period) / period
        price = 1.2 + 0.01 * (2 * phase if phase < 0.5 else 2 - 2 * phase)
        series.add(a.Sample(START + timedelta(seconds=i * step), round(price, 5), f"{price:.5f}", payout))
    return series


def walk(n, step=30.0, seed=7, payout=92.0):
    """A seeded random walk: no rule can predict it."""
    rnd = random.Random(seed)
    series = a.Series("TESTE/ACASO OTC")
    price = 1.2
    for i in range(n):
        price += rnd.gauss(0, 0.0001)
        series.add(a.Sample(START + timedelta(seconds=i * step), round(price, 5), f"{price:.5f}", payout))
    return series


class StatisticsTest(unittest.TestCase):
    def test_wilson_interval_and_break_even(self):
        low, high = a.wilson(50, 100, 1.96)
        self.assertAlmostEqual(low, 0.4038, places=3)
        self.assertAlmostEqual(high, 0.5962, places=3)
        low, high = a.wilson(0, 10)
        self.assertAlmostEqual(low, 0.0, places=12)
        self.assertGreater(high, 0.0)
        self.assertIsNone(a.wilson(0, 0))
        self.assertAlmostEqual(a.break_even(90), 1 / 1.9)
        self.assertAlmostEqual(a.break_even(92), 1 / 1.92)
        self.assertEqual(a.percent_text(a.break_even(90)), "52,6%")


class SeriesTest(unittest.TestCase):
    def test_samples_stay_in_time_order_without_repeats(self):
        series = a.Series("X")
        for seconds in (60, 0, 30):
            self.assertTrue(series.add(a.Sample(START + timedelta(seconds=seconds), 1.0 + seconds, "q")))
        self.assertFalse(series.add(a.Sample(START + timedelta(seconds=30), 9.0, "q")))
        self.assertEqual([sample.price for sample in series.samples], [1.0, 31.0, 61.0])
        moment = START + timedelta(seconds=31)
        self.assertEqual(series.first_at_or_after(moment, 30).price, 61.0)
        self.assertIsNone(series.first_at_or_after(moment, 28))
        self.assertEqual(series.nearest(moment, 5).price, 31.0)
        self.assertIsNone(series.nearest(START + timedelta(seconds=45), 10))
        self.assertEqual(series.median_interval(), 30)

    def test_an_outcome_needs_a_quote_right_after_the_horizon(self):
        series = a.Series("X")
        for seconds, price in ((0, 1.0), (310, 1.1), (900, 0.9)):
            series.add(a.Sample(START + timedelta(seconds=seconds), price, "q"))
        first = series.samples[0]
        self.assertEqual(a.outcome(series, first, HORIZON, a.CALL), a.WIN)
        self.assertEqual(a.outcome(series, first, HORIZON, a.PUT), a.LOSS)
        self.assertIsNone(a.outcome(series, series.samples[1], HORIZON, a.CALL))  # nothing 300 to 330 s later
        series.add(a.Sample(START + timedelta(seconds=620), 1.1, "q"))
        self.assertEqual(a.outcome(series, series.samples[1], HORIZON, a.CALL), a.DRAW)


class ValidationTest(unittest.TestCase):
    def test_a_predictable_series_validates_a_rule_on_separate_data(self):
        series = legs(6000)
        validation = a.validate(series, HORIZON)
        self.assertTrue(validation.validated, validation.reason)
        self.assertEqual(validation.rule, a.Rule(a.TREND, 1))
        self.assertGreaterEqual(validation.evaluation.trades, a.MIN_EVAL_TRADES)
        low, _ = validation.evaluation.interval()
        self.assertGreater(low, a.break_even(90))
        self.assertRegex(
            validation.reason,
            r"^regra tendência de 300 s validada: \d+ operações de avaliação \(\d+ acertos e \d+ erros\), acerto de"
            r" \d+,\d%, intervalo de 99% de \d+,\d% a \d+,\d%, acima de 52,6%$",
        )

    def test_no_operation_of_development_uses_the_evaluation_time(self):
        series = legs(6000)
        development, evaluation, labelled = a.split(series, HORIZON)
        cut = evaluation[0].at
        end = timedelta(seconds=HORIZON + a.tolerance(HORIZON))
        self.assertTrue(all(sample.at + end <= cut for sample in development))
        self.assertTrue(all(sample.at >= cut for sample in evaluation))
        self.assertAlmostEqual(len([s for s in labelled if s.at < cut]) / len(labelled), a.DEV_FRACTION, delta=0.01)
        # The moments are at least one horizon apart: no two operations overlap.
        moments = a.decision_moments(series, HORIZON)
        self.assertTrue(all((b.at - a_.at).total_seconds() >= HORIZON for a_, b in zip(moments, moments[1:])))

    def test_a_random_walk_is_never_validated(self):
        for seed in (1, 2, 3, 4, 5):
            with self.subTest(seed=seed):
                validation = a.validate(walk(8000, seed=seed), HORIZON)
                self.assertFalse(validation.validated, validation.reason)

    def test_without_enough_data_no_rate_is_shown(self):
        for series in (walk(40), walk(300), legs(2400)):
            with self.subTest(samples=len(series.samples)):
                validation = a.validate(series, HORIZON)
                self.assertFalse(validation.validated)
                self.assertNotIn("%", validation.reason.replace("52,6%", ""))  # counts only, no rate
        self.assertIn("dados insuficientes", a.validate(walk(300), HORIZON).reason)
        self.assertIn("em avaliação:", a.validate(legs(2400), HORIZON).reason)
        self.assertIn("nenhuma operação hipotética", a.validate(walk(5), HORIZON).reason)


class DecisionTest(unittest.TestCase):
    def test_a_validated_rule_decides_only_on_a_fresh_quote_with_an_eligible_payout(self):
        series = legs(6000)
        last = series.samples[-1]
        decision = a.decide(series, HORIZON, last.at + timedelta(seconds=1))
        expected = a.direction_of(a.Rule(a.TREND, 1), series, last, HORIZON)
        self.assertIn(decision.action, (a.CALL, a.PUT))
        self.assertEqual(decision.action, expected)
        self.assertEqual(decision.valid_until, last.at + timedelta(seconds=a.DECISION_WINDOW_SECONDS))
        self.assertIn("válida até", decision.describe())
        stale = a.decide(series, HORIZON, last.at + timedelta(seconds=a.DECISION_WINDOW_SECONDS + 1))
        self.assertEqual(stale.action, a.WAIT)
        self.assertIn("uma decisão só vale até 5 s depois da captura", stale.reason)
        low = legs(6000, payout=85.0)
        self.assertEqual(a.decide(low, HORIZON, low.samples[-1].at).action, a.WAIT)
        self.assertIn("o payout 85% não permite a primeira entrada", a.decide(low, HORIZON, low.samples[-1].at).reason)
        unknown = legs(6000, payout=None)
        self.assertIn("payout indisponível", a.decide(unknown, HORIZON, unknown.samples[-1].at).reason)

    def test_without_validation_the_decision_is_to_wait(self):
        for series in (walk(8000), walk(100), a.Series("VAZIO")):
            with self.subTest(samples=len(series.samples)):
                now = series.samples[-1].at if series.samples else START
                decision = a.decide(series, HORIZON, now)
                self.assertEqual(decision.action, a.WAIT)
                self.assertIsNone(decision.valid_until)


def write_scan_csv(path, rows):
    """A scan CSV with the scan's own columns; rows are dicts of some of them."""
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(scan.SCAN_COLUMNS)
        for row in rows:
            writer.writerow([row.get(name, "") for name in scan.SCAN_COLUMNS])


def scan_row(seconds, price, ready="sim", asset="EUR/USD OTC", payout="92"):
    at = (START + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    return {
        "asset": asset,
        "price": f"{price:.5f}",
        "price_captured_at_utc": at,
        "payout_percent": payout,
        "analysis_ready": ready,
    }


class HistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)

    def test_the_columns_read_are_the_scans(self):
        for name in a.COLUMNS:
            self.assertIn(name, scan.SCAN_COLUMNS)

    def test_only_lines_usable_for_analysis_are_loaded(self):
        write_scan_csv(
            self.runtime / "scan-20261015-100000-aaaaaaaa.csv",
            [
                scan_row(0, 1.08530),
                scan_row(30, 1.08540, ready="não"),
                scan_row(60, 1.08550, asset="GBP/USD OTC"),
                scan_row(60, 1.08560, asset="gbpusd otc"),  # the same asset and moment
                {**scan_row(90, 1.08570), "price_captured_at_utc": "2026-10-15T10:00:00"},  # no time zone
            ],
        )
        old = self.runtime / "scan-20261014-100000-bbbbbbbb.csv"
        with open(old, "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows([["asset", "price"], ["EUR/USD OTC", "1.08"]])
        (self.runtime / "tempos-scan-20261015-100000-aaaaaaaa.csv").write_text("not a scan file", encoding="utf-8")
        history = a.load_history(self.runtime)
        self.assertEqual((history.files, history.usable), (2, 2))
        self.assertEqual(sorted(series.name for series in history.series.values()), ["EUR/USD OTC", "GBP/USD OTC"])
        self.assertEqual(
            history.skipped,
            {
                "linhas sem dados para análise": 1,
                "cotações repetidas no mesmo instante": 1,
                "linhas com horário sem fuso": 1,
                "linhas de arquivos anteriores à coluna analysis_ready": 1,
            },
        )
        excluded = a.load_history(self.runtime, exclude=[self.runtime / "scan-20261015-100000-aaaaaaaa.csv"])
        self.assertEqual(excluded.usable, 0)

    def write_cycle_csv(self, name, rows, columns=a.COLUMNS + (a.ORIGIN_COLUMN,)):
        """A file of quotes read during cycles; rows are dicts of some of columns."""
        with open(self.runtime / name, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([row.get(column, "") for column in columns])

    def test_quotes_read_during_cycles_are_loaded_with_their_origin(self):
        write_scan_csv(self.runtime / "scan-20261015-100000-aaaaaaaa.csv", [scan_row(0, 1.08530), scan_row(600, 1.08590)])
        cycle = {a.ORIGIN_COLUMN: a.ORIGIN_CYCLE}
        self.write_cycle_csv(
            "cotacoes-ciclo-20261015-100001-cccccccc.csv",
            [
                {**scan_row(300.5, 1.08550, payout=""), **cycle},  # without a payout: still a quote
                {**scan_row(301, 1.08551, payout="80"), **cycle},  # below 90%: still a quote
                {**scan_row(0, 1.08530), **cycle},  # the scan's own quote again: counted once
                {**scan_row(302, 1.08552), a.ORIGIN_COLUMN: "simulador"},  # never a result of the simulator
                {**scan_row(303, 1.08553), a.ORIGIN_COLUMN: ""},
                {**scan_row(304, 1.08554, ready="não"), **cycle},
            ],
        )
        self.write_cycle_csv("cotacoes-ciclo-20261015-100002-dddddddd.csv", [scan_row(305, 1.08555)], a.COLUMNS)
        # Files of the operation, of the demonstration and of the timings are never read.
        for name in ("operacao-20261015-100000-eeeeeeee.csv", "demonstracao-operacao-20261015-100000-ffffffff.csv",
                     "tempos-scan-20261015-100000-aaaaaaaa.csv"):
            self.write_cycle_csv(name, [{**scan_row(306, 1.2), **cycle}])
        history = a.load_history(self.runtime)
        self.assertEqual((history.files, history.cycle_files, history.usable), (3, 2, 4))
        self.assertEqual(history.origins, {a.ORIGIN_SCAN: 2, a.ORIGIN_CYCLE: 2})
        self.assertEqual(
            history.skipped,
            {
                "cotações repetidas no mesmo instante": 1,
                "linhas de outra origem, que não é cotação lida na tela": 3,
                "linhas sem dados para análise": 1,
            },
        )
        series = history.get("EUR/USD OTC")
        self.assertEqual([sample.origin for sample in series.samples], [a.ORIGIN_SCAN, a.ORIGIN_CYCLE, a.ORIGIN_CYCLE, a.ORIGIN_SCAN])
        self.assertEqual([sample.payout for sample in series.samples], [92.0, None, 80.0, 92.0])
        self.assertNotIn(1.2, [sample.price for sample in series.samples])
        # The quote read 300,5 s after the first one labels it, as a quote of the scan would.
        self.assertEqual(a.outcome(series, series.samples[0], HORIZON, a.CALL), a.WIN)
        text = "\n".join(a.report_lines(history, HORIZON, START + timedelta(hours=1)))
        self.assertIn(
            "Dados: 1 arquivos da varredura e 2 de leituras durante ciclos, 4 cotações utilizáveis em 1 ativos."
            " Origem: 2 de visitas da varredura e 2 de leituras da tela durante ciclos da operação simulada;"
            " resultados do simulador nunca entram.",
            text,
        )

    def test_the_context_of_a_cycle_never_reaches_the_samples(self):
        columns = a.COLUMNS + (a.ORIGIN_COLUMN, "cycle_id", "attempt", "operation_state", "result")
        loaded = []
        for attempt, state, result in (("1", "PENDÊNCIA", "WIN"), ("4", "CICLO ATIVO", "LOSS")):
            runtime = self.runtime / f"tentativa-{attempt}"
            runtime.mkdir()
            rows = [
                {**scan_row(seconds, 1.0853 + seconds / 1e6), a.ORIGIN_COLUMN: a.ORIGIN_CYCLE, "cycle_id": "7",
                 "attempt": attempt, "operation_state": state, "result": result}
                for seconds in (0, 0.5, 1.0)
            ]
            with open(runtime / "cotacoes-ciclo-20261015-100000-aaaaaaaa.csv", "w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(columns)
                writer.writerows([[row.get(column, "") for column in columns] for row in rows])
            loaded.append(a.load_history(runtime).get("EUR/USD OTC").samples)
        self.assertEqual(loaded[0], loaded[1])  # the simulated attempt and its result change nothing
        self.assertEqual(set(vars(loaded[0][0])), {"at", "price", "quote", "payout", "origin"})

    def test_dense_readings_during_a_cycle_add_no_independent_operations(self):
        def price_at(moment):  # the price of legs() at any moment
            phase = ((moment - START).total_seconds() % (40 * HORIZON)) / (40 * HORIZON)
            return round(1.2 + 0.01 * (2 * phase if phase < 0.5 else 2 - 2 * phase), 5)

        full = legs(6000)  # a scan that never stops
        cycles = [(full.samples[first].at, full.samples[first].at + timedelta(seconds=1200)) for first in (1500, 3000, 4500)]
        # The scan stops during each cycle of 20 minutes: before this change, nothing replaced it.
        gap, dense = a.Series("X"), a.Series("X")
        for sample in full.samples:
            if not any(start < sample.at < end for start, end in cycles):
                gap.add(sample)
                dense.add(sample)
        for start, _ in cycles:  # now the readings of the cycle, 0.5 s apart, real quotes
            for index in range(2400):
                at = start + timedelta(seconds=0.25 + 0.5 * index)
                dense.add(a.Sample(at, price_at(at), f"{price_at(at):.5f}", None, a.ORIGIN_CYCLE))
        self.assertEqual(len(dense.samples), len(gap.samples) + 3 * 2400)
        moments = a.decision_moments(dense, HORIZON)
        gaps = [(later.at - earlier.at).total_seconds() for earlier, later in zip(moments, moments[1:])]
        self.assertGreaterEqual(min(gaps), HORIZON)  # never closer than one horizon
        for start, end in cycles:  # 2400 readings, but at most 5 operations in each cycle
            self.assertLessEqual(len([sample for sample in moments if start <= sample.at <= end]), 1200 / HORIZON + 1)
        self.assertLessEqual(len(moments), len(a.decision_moments(full, HORIZON)) + len(cycles))
        tolerance = a.tolerance(HORIZON)

        def labels(series):
            moments = a.decision_moments(series, HORIZON)
            found = [series.first_at_or_after(sample.at + timedelta(seconds=HORIZON), tolerance) for sample in moments]
            return [label for label in found if label is not None]

        self.assertGreater(len(labels(dense)), len(labels(gap)))  # results that the gap had lost
        self.assertTrue(any(label.origin == a.ORIGIN_CYCLE for label in labels(dense)))
        development, evaluation, _ = a.split(dense, HORIZON)
        end = timedelta(seconds=HORIZON + tolerance)
        self.assertLessEqual(max(sample.at for sample in development) + end, min(sample.at for sample in evaluation))
        for series in (full, gap, dense):  # the same limits decide, with or without the readings of the cycles
            validation = a.validate(series, HORIZON)
            self.assertTrue(validation.validated)
            self.assertGreaterEqual(validation.evaluation.trades, a.MIN_EVAL_TRADES)
            trades = validation.development.trades + validation.evaluation.trades
            self.assertLessEqual(trades, len(a.decision_moments(series, HORIZON)))

    def test_the_report_waits_and_says_why(self):
        rows = [scan_row(i * 60, 1.08 + 0.0001 * math.sin(i)) for i in range(30)]
        write_scan_csv(self.runtime / "scan-20261015-100000-aaaaaaaa.csv", rows)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = a.main(["--runtime", str(self.runtime)], now=lambda: START + timedelta(hours=1))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("ANÁLISE LOCAL CALL/PUT, com operações hipotéticas de 300 s.", text)
        self.assertIn("Dados: 1 arquivos da varredura, 30 cotações utilizáveis em 1 ativos.", text)
        self.assertIn("  EUR/USD OTC: 30 cotações, uma a cada 60 s (mediana)", text)
        self.assertIn("    agora: AGUARDAR (dados insuficientes:", text)
        self.assertNotIn("CALL (", text)
        self.assertNotIn("PUT (", text)

    def test_runs_with_python_dash_m(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pocketautonomo.analysis", "--runtime", str(self.runtime)],
            cwd=self.runtime, env=env, capture_output=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AGUARDAR: sem cotações utilizáveis.", result.stdout)
        refused = subprocess.run(
            [sys.executable, "-m", "pocketautonomo.analysis", "--horizon", "5"],
            cwd=self.runtime, env=env, capture_output=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(refused.returncode, 2)

    def test_the_analysis_never_touches_a_browser_or_an_order(self):
        source = (ROOT / "pocketautonomo" / "analysis.py").read_text(encoding="utf-8")
        for word in ("playwright", "import observe", "broker", "executor", "coordinator", "click", "requests", "urllib"):
            self.assertNotIn(word, source.lower().replace("pocketautonomo.observe", ""), word)


if __name__ == "__main__":
    unittest.main()
