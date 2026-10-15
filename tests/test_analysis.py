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
