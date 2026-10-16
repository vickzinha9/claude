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


def scan_row(seconds, price, ready="sim", asset="EUR/USD OTC", payout="92", switch="confirmada", cycle=1):
    at = (START + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    return {
        "captured_at_utc": at,
        "asset": asset,
        "price": f"{price:.5f}",
        "price_captured_at_utc": at,
        "payout_percent": payout,
        "analysis_ready": ready,
        "switch": switch,
        "cycle": str(cycle),
    }


def failed_switch(seconds, cycle, position, name):
    """The line of a switch that the asset field did not confirm, as the scan writes it."""
    row = scan_row(seconds, 0, ready="não", asset="", switch="não confirmada", cycle=cycle)
    row.update(price="", price_captured_at_utc="", payout_percent="", favorite_position=str(position),
               favorite_text=f"{name}\n92%")
    return row


class HistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)

    def test_the_columns_read_are_the_scans(self):
        for name in a.COLUMNS + (a.SWITCH_COLUMN, a.CYCLE_COLUMN, a.TIME_COLUMN, "favorite_position", "favorite_text"):
            self.assertIn(name, scan.SCAN_COLUMNS)
        self.assertEqual(a.NOT_CONFIRMED, scan.NOT_CONFIRMED)
        self.assertEqual(
            set(a.SWITCH_VALUES), {scan.ALREADY, scan.CONFIRMED, scan.NOT_CONFIRMED, scan.NOT_CLICKED, scan.STANDBY}
        )

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

    def snapshot(self):
        """The name, bytes and modification time of every file in the runtime folder."""
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(self.runtime.iterdir())}

    def test_the_lines_from_the_first_unconfirmed_switch_on_are_left_out_and_the_files_kept(self):
        failed = self.runtime / "scan-20261015-100000-aaaaaaaa.csv"
        write_scan_csv(
            failed,
            [
                scan_row(0, 1.08530),  # before the failure: kept
                scan_row(30, 0.91210, asset="AUD/CAD OTC"),  # kept
                failed_switch(60, 1, 3, "AED/CNY OTC"),  # line 4: the PAUSA starts here
                scan_row(90, 0.91230, asset="AUD/CAD OTC"),  # the right name, maybe another asset's chart
                scan_row(120, 1.08540, cycle=2),  # a later confirmed switch clears nothing
                scan_row(150, 0.91250, asset="AUD/CAD OTC", cycle=2),
                {**scan_row(180, 1.27340, asset="AED/CNY OTC", cycle=2)},  # the favorite back, still out
                {**scan_row(210, 0, ready="não", asset="", switch="em espera", cycle=3), "price": ""},
            ],
        )
        independent = self.runtime / "scan-20261015-120000-bbbbbbbb.csv"
        write_scan_csv(independent, [scan_row(7200, 1.08600), scan_row(7230, 0.91300, asset="AUD/CAD OTC")])
        before = self.snapshot()
        history = a.load_history(self.runtime)
        self.assertEqual(self.snapshot(), before)  # only read: the same files, bytes and times
        self.assertEqual([sample.price for sample in history.get("EUR/USD OTC").samples], [1.08530, 1.08600])
        self.assertEqual([sample.price for sample in history.get("AUD/CAD OTC").samples], [0.91210, 0.91300])
        self.assertIsNone(history.get("AED/CNY OTC"))
        self.assertEqual((history.files, history.usable), (2, 4))
        # The 4 lines with data from the failed switch on; the other 2 keep their own reason.
        self.assertEqual(history.skipped, {a.POTENTIAL: 4, "linhas sem dados para análise": 2})
        [left] = history.left_out
        self.assertEqual((left.path, left.lines, left.usable, left.undetermined), (failed, 6, 4, False))
        self.assertEqual(left.detail, "a partir da linha 4 (ciclo 1, troca não confirmada para o favorito 3, AED/CNY OTC 92%)")
        text = "\n".join(a.report_lines(history, HORIZON, START + timedelta(hours=3)))
        self.assertIn(
            "  scan-20261015-100000-aaaaaaaa.csv, a partir da linha 4 (ciclo 1, troca não confirmada para o favorito 3,"
            " AED/CNY OTC 92%): 4 linhas com dados para análise excluídas, potencialmente inconsistentes, de 6 linhas"
            " desde ali.",
            text,
        )
        self.assertIn(
            "As linhas com dados para análise excluídas por isso são potencialmente inconsistentes, o que não quer dizer"
            " que todas tinham cotação errada.",
            text,
        )
        self.assertIn("Fora da análise, sem mudar nenhum arquivo", text)
        self.assertNotIn("scan-20261015-120000-bbbbbbbb.csv", text)  # the independent file is whole
        self.assertIn("Dados: 2 arquivos da varredura, 4 cotações utilizáveis em 2 ativos.", text)
        self.assertEqual(self.snapshot(), before)

    def test_lines_whose_execution_cannot_be_told_are_left_out_and_reported(self):
        no_switch = self.runtime / "scan-20261015-100000-aaaaaaaa.csv"
        with open(no_switch, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(a.COLUMNS + (a.TIME_COLUMN,))
            writer.writerow([scan_row(0, 1.08530)[name] for name in a.COLUMNS + (a.TIME_COLUMN,)])
        merged = self.runtime / "scan-20261015-110000-bbbbbbbb.csv"  # two executions in one file
        write_scan_csv(merged, [scan_row(3600, 1.08600), scan_row(3630, 1.08610, cycle=2), scan_row(3660, 1.08620)])
        cut = self.runtime / "scan-20261015-120000-cccccccc.csv"  # a last line cut before the switch
        write_scan_csv(cut, [scan_row(7200, 1.08700), scan_row(7230, 1.08710, switch="")])
        before = self.snapshot()
        history = a.load_history(self.runtime)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([sample.price for sample in history.get("EUR/USD OTC").samples], [1.08600, 1.08610, 1.08700])
        self.assertEqual(history.skipped, {a.UNDETERMINED: 3})
        details = {item.path.name: (item.lines, item.usable, item.undetermined, item.detail) for item in history.left_out}
        self.assertEqual(
            details,
            {
                no_switch.name: (1, 1, True, "a partir da linha 2: o arquivo não tem as colunas switch e cycle, que mostram"
                                 " as trocas e a execução"),
                merged.name: (1, 1, True, "a partir da linha 4: o ciclo 1 vem depois do ciclo 2, então o arquivo pode ter"
                              " mais de uma execução"),
                cut.name: (1, 1, True, "a partir da linha 3 (ciclo 1): o valor '' da coluna switch não diz se a troca foi"
                           " confirmada"),
            },
        )
        text = "\n".join(a.left_out_lines(history))
        self.assertIn(
            f"  {merged.name}, a partir da linha 4: o ciclo 1 vem depois do ciclo 2, então o arquivo pode ter mais de uma"
            " execução: 1 linha com dados para análise excluída, de execução indeterminada, de 1 linha desde ali.",
            text,
        )
        self.assertIn(
            "Também ficam fora as de execução indeterminada, em que os arquivos não permitem saber se houve uma troca não"
            " confirmada antes delas na mesma execução.",
            text,
        )

    def test_quotes_read_during_cycles_follow_the_execution_they_were_read_in(self):
        write_scan_csv(
            self.runtime / "scan-20261015-100000-aaaaaaaa.csv",
            [scan_row(0, 1.08530), failed_switch(30, 1, 2, "GBP/USD OTC"), scan_row(120, 1.08540, cycle=2)],
        )
        write_scan_csv(self.runtime / "scan-20261015-110000-bbbbbbbb.csv", [scan_row(3600, 1.08600), scan_row(3660, 1.08610)])
        cycle = {a.ORIGIN_COLUMN: a.ORIGIN_CYCLE}
        quotes = {
            -100: True,  # before every execution, after none that paused: kept
            10: True,  # during the first one, before its unconfirmed switch: kept
            70: False,  # during it, after the switch: potentially inconsistent
            1800: False,  # after its last line and before the next one: undetermined
            3630: True,  # during the second execution, which never paused: kept
            5000: True,  # after it: whichever execution, none had paused
        }
        self.write_cycle_csv(
            "cotacoes-ciclo-20261015-100001-cccccccc.csv",
            [{**scan_row(seconds, 1.0 + seconds / 1e5), **cycle} for seconds in quotes],
        )
        before = self.snapshot()
        history = a.load_history(self.runtime)
        self.assertEqual(self.snapshot(), before)
        kept = {round((sample.at - START).total_seconds()) for sample in history.get("EUR/USD OTC").samples
                if sample.origin == a.ORIGIN_CYCLE}
        self.assertEqual(kept, {seconds for seconds, used in quotes.items() if used})
        # The scan's line at 120 s and the quote at 70 s; the failed switch's own line has no data.
        self.assertEqual(history.skipped, {a.POTENTIAL: 2, a.UNDETERMINED: 1, "linhas sem dados para análise": 1})
        lines = a.left_out_lines(history)
        self.assertIn(
            "  cotacoes-ciclo-20261015-100001-cccccccc.csv: 1 cotação excluída, potencialmente inconsistente, lida"
            " durante scan-20261015-100000-aaaaaaaa.csv, depois da troca não confirmada da linha 3 (ciclo 1).",
            lines,
        )
        self.assertIn(
            "  cotacoes-ciclo-20261015-100001-cccccccc.csv: 1 cotação excluída, de execução indeterminada, lida entre o"
            " fim de scan-20261015-100000-aaaaaaaa.csv, que já tinha troca não confirmada na linha 3 (ciclo 1), e o"
            " começo da execução seguinte, sem como determinar de qual das duas.",
            lines,
        )
        # A scan file that cannot be read may be the execution of a quote outside the others' times.
        (self.runtime / "scan-20261015-090000-dddddddd.csv").write_bytes(b"\xff\xfe not utf-8")
        history = a.load_history(self.runtime)
        self.assertEqual(history.skipped["arquivos que não puderam ser lidos"], 1)
        self.assertEqual(history.skipped[a.UNDETERMINED], 3)  # 1800 s, and now -100 s and 5000 s too
        self.assertTrue(any("com um arquivo da varredura que não pôde ser lido" in line for line in a.left_out_lines(history)))

    def cycle_quotes(self, seconds):
        """A cycle file with a quote at each of seconds; the seconds of those that the history kept."""
        self.write_cycle_csv(
            "cotacoes-ciclo-20261015-100001-cccccccc.csv",
            [{**scan_row(second, 1.0 + second / 1e5), a.ORIGIN_COLUMN: a.ORIGIN_CYCLE} for second in seconds],
        )
        before = self.snapshot()
        history = a.load_history(self.runtime)
        self.assertEqual(self.snapshot(), before)
        series = history.get("EUR/USD OTC")
        kept = {round((sample.at - START).total_seconds()) for sample in (series.samples if series else ())
                if sample.origin == a.ORIGIN_CYCLE}
        return history, kept

    def test_overlapping_scan_files_are_taken_together(self):
        clean = self.runtime / "scan-20261015-100000-aaaaaaaa.csv"
        write_scan_csv(clean, [scan_row(0, 1.08530), scan_row(100, 1.08540)])
        paused = self.runtime / "scan-20261015-100001-bbbbbbbb.csv"  # a copied or clock-shifted file, inside the first
        write_scan_csv(paused, [scan_row(10, 0.91210, asset="AUD/CAD OTC"), failed_switch(15, 1, 2, "GBP/USD OTC"),
                                scan_row(20, 0.91220, asset="AUD/CAD OTC")])
        history, kept = self.cycle_quotes((5, 50, 200))
        self.assertEqual(kept, {5})  # before the first PAUSA among them
        self.assertEqual(history.skipped[a.UNDETERMINED], 2)
        lines = a.left_out_lines(history)
        self.assertIn(
            f"  cotacoes-ciclo-20261015-100001-cccccccc.csv: 1 cotação excluída, de execução indeterminada, lida num"
            f" horário em que {clean.name} e {paused.name} se sobrepõem, com troca não confirmada na linha 3 (ciclo 1) em"
            f" {paused.name} antes dela, sem como determinar a execução.",
            lines,
        )
        self.assertIn(
            f"  cotacoes-ciclo-20261015-100001-cccccccc.csv: 1 cotação excluída, de execução indeterminada, lida depois do"
            f" fim de {paused.name}, que já tinha troca não confirmada na linha 3 (ciclo 1), sem como determinar se é dela"
            " ou de uma execução seguinte.",
            lines,
        )

    def test_quotes_during_an_undetermined_execution_or_beside_a_file_without_times(self):
        merged = self.runtime / "scan-20261015-100000-aaaaaaaa.csv"
        write_scan_csv(merged, [scan_row(0, 1.08530), scan_row(30, 1.08540, cycle=2), scan_row(60, 1.08550),
                                scan_row(90, 1.08560)])
        history, kept = self.cycle_quotes((10, 70, 1000))
        self.assertEqual(kept, {10})  # before line 4, where the execution becomes undetermined
        lines = a.left_out_lines(history)
        self.assertIn(
            f"  cotacoes-ciclo-20261015-100001-cccccccc.csv: 1 cotação excluída, de execução indeterminada, lida durante"
            f" {merged.name}, depois da linha 4, de onde a execução é indeterminada.",
            lines,
        )
        self.assertIn(
            f"  cotacoes-ciclo-20261015-100001-cccccccc.csv: 1 cotação excluída, de execução indeterminada, lida depois do"
            f" fim de {merged.name}, que já tinha execução indeterminada a partir da linha 4, sem como determinar se é"
            " dela ou de uma execução seguinte.",
            lines,
        )
        write_scan_csv(merged, [scan_row(0, 1.08530), scan_row(90, 1.08560)])  # now a clean execution
        history, kept = self.cycle_quotes((10, 1000))
        self.assertEqual(kept, {10, 1000})
        timeless = self.runtime / "scan-20261015-090000-dddddddd.csv"
        write_scan_csv(timeless, [{**scan_row(500, 1.08600), "captured_at_utc": ""}])
        history, kept = self.cycle_quotes((10, 1000))
        self.assertEqual(kept, {10})  # 1000 s may be of the execution without times
        self.assertTrue(any("com um arquivo da varredura que não pôde ser lido ou sem horários" in line
                            for line in a.left_out_lines(history)))

    def test_the_start_of_a_scan_lists_only_the_files_that_lost_data(self):
        lost = self.runtime / "scan-20261015-100000-aaaaaaaa.csv"
        write_scan_csv(lost, [scan_row(0, 1.08530), failed_switch(30, 1, 2, "GBP/USD OTC"), scan_row(60, 1.08540)])
        paused = self.runtime / "scan-20261015-110000-bbbbbbbb.csv"  # with the PAUSA, nothing usable after the failure
        write_scan_csv(paused, [scan_row(3600, 1.08600), failed_switch(3630, 1, 2, "GBP/USD OTC"),
                                scan_row(3660, 1.08610, ready="não")])
        history = a.load_history(self.runtime)
        full, start = a.left_out_lines(history), a.left_out_lines(history, everything=False)
        self.assertIn(
            f"  {paused.name}, a partir da linha 3 (ciclo 1, troca não confirmada para o favorito 2, GBP/USD OTC 92%):"
            " nenhuma linha com dados para análise a excluir, de 2 linhas desde ali.",
            full,
        )
        self.assertEqual([line for line in start if line.startswith(f"  {lost.name}")], [line for line in full if line.startswith(f"  {lost.name}")])
        self.assertFalse([line for line in start if paused.name in line])
        self.assertEqual(
            start[-1],
            "  Mais 1 arquivo com troca não confirmada ou execução indeterminada, sem nenhuma linha com dados para"
            " análise a excluir; a lista completa está no analisar_windows.cmd.",
        )

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
