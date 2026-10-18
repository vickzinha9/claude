"""Tests of the analysis of the candles, pocketautonomo.chart_analysis.

ARTIFICIAL DATA: every candle here is made by the tests, to put the analysis in a known situation
(a rise, a fall, a range, a conflict, a gap, a series with or without an edge). None of them comes
from a market, and passing here says nothing about a strategy on the real platform.
"""

import csv
import random
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pocketautonomo import analysis, candles  # noqa: E402
from pocketautonomo import chart_analysis as ca  # noqa: E402
from pocketautonomo.analysis import CALL, DRAW, PUT, WAIT  # noqa: E402

START = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
PERIOD = 60
RESOLUTION = 0.000002


def bars_of(prices, start=START, period=PERIOD, resolution=RESOLUTION, skip=()):
    """Bars from (open, high, low, close), one period apart; the positions in skip are left out."""
    bars = []
    for index, (open_, high, low, close) in enumerate(prices):
        if index in skip:
            continue
        bars.append(ca.Bar(open_, high, low, close, start + timedelta(seconds=index * period), resolution))
    return bars


def trend(count, step=0.0001, start=1.08500, pullback_every=4):
    """ARTIFICIAL rise (step > 0) or fall (step < 0): every pullback_every-th candle goes back a little."""
    prices, price = [], start
    for index in range(count):
        move = -step * 0.3 if pullback_every and index % pullback_every == pullback_every - 1 else step
        open_, close = price, price + move
        prices.append((open_, max(open_, close) + abs(step) * 0.1, min(open_, close) - abs(step) * 0.1, close))
        price = close
    return prices


def flat(count, start=1.08500, body=0.00002, wick=0.00004):
    """ARTIFICIAL range: small candles going up and down around one price, with even wicks."""
    prices = []
    for index in range(count):
        open_ = start + (body if index % 2 else 0)
        close = start + (0 if index % 2 else body)
        prices.append((open_, max(open_, close) + wick, min(open_, close) - wick, close))
    return prices


class MarketReadingTest(unittest.TestCase):
    def test_a_steady_rise_favors_alta_with_its_signals(self):
        reading = ca.read_market(bars_of(trend(40)), RESOLUTION, PERIOD)
        self.assertEqual(reading.direction, ca.ALTA, reading.summary)
        self.assertTrue(reading.summary.startswith("favorece ALTA:"))
        self.assertIn("nenhum de baixa", reading.summary)
        keys = {signal.key: signal for signal in reading.signals}
        self.assertEqual(set(keys), {"short", "medium", "impulse", "sequence", "wicks", "levels", "volatility"})
        self.assertEqual((keys["short"].direction, keys["medium"].direction), (1, 1))
        self.assertIn("nas últimas 5 velas, o preço subiu", keys["short"].text)
        self.assertEqual(keys["volatility"].direction, 0)  # context, never a direction
        self.assertIn(ca.NOT_VALIDATED, reading.limitations)
        self.assertTrue(reading.describe().startswith("LEITURA EXPLORATÓRIA, não é sinal de entrada: favorece ALTA"))
        text = (reading.summary + reading.signals_text() + " ".join(reading.limitations)).lower()
        for word in ("probabilidade", "confiança", "chance", "garantid"):
            self.assertNotIn(word, text.replace("não garante", ""))

    def test_a_steady_fall_favors_baixa(self):
        reading = ca.read_market(bars_of(trend(40, step=-0.0001)), RESOLUTION, PERIOD)
        self.assertEqual(reading.direction, ca.BAIXA, reading.summary)
        self.assertIn("nenhum de alta", reading.summary)

    def test_a_range_is_undefined_without_signals(self):
        reading = ca.read_market(bars_of(flat(40)), RESOLUTION, PERIOD)
        self.assertEqual(reading.direction, ca.INDEFINIDO)
        self.assertIn("sem sinais suficientes", reading.summary)

    def test_a_rise_that_is_rejected_at_its_high_is_a_conflict(self):
        prices = trend(40)
        last = prices[-1][3]
        resistance = max(high for _, high, _, _ in prices[-21:])
        prices.append((last, resistance + 0.0004, last - 0.00001, last - 0.00002))  # a long upper wick, closed back
        reading = ca.read_market(bars_of(prices), RESOLUTION, PERIOD)
        self.assertEqual(reading.direction, ca.INDEFINIDO)
        self.assertIn("sinais em conflito", reading.summary)
        keys = {signal.key: signal for signal in reading.signals}
        self.assertEqual(keys["wicks"].direction, -1)
        self.assertIn("a alta foi rejeitada", keys["wicks"].text)
        self.assertEqual(keys["levels"].direction, -1)
        self.assertIn("rejeição da resistência", keys["levels"].text)

    def test_a_close_above_the_range_is_a_breakout(self):
        prices = flat(30)
        prices.append((1.08502, 1.08530, 1.08500, 1.08528))
        keys = {signal.key: signal for signal in ca.read_market(bars_of(prices), RESOLUTION, PERIOD).signals}
        self.assertEqual(keys["levels"].direction, 1)
        self.assertIn("rompimento de alta", keys["levels"].text)

    def test_what_stops_a_reading(self):
        cases = (
            (bars_of(trend(15)), None, "dados insuficientes: 15 velas completas seguidas, de pelo menos 21"),
            (bars_of(trend(40), skip={25}), None, "dados insuficientes: 14 velas completas seguidas"),
            (bars_of(trend(40, step=0.000004)), None, "pequenas demais na escala do gráfico"),
            (bars_of(trend(40)), 6.0, "leitura desatualizada: capturada há 6,0 s"),
        )
        for bars, age, message in cases:
            with self.subTest(message=message):
                reading = ca.read_market(bars, RESOLUTION, PERIOD, age)
                self.assertEqual(reading.direction, ca.INDEFINIDO)
                self.assertIn(message, reading.summary)
        gap = ca.read_market(bars_of(trend(60), skip={25}), RESOLUTION, PERIOD)
        self.assertIn("faltam velas no gráfico lido: só as últimas 34 seguidas entram", gap.limitations)


def edge_series(count, switch=97):
    """ARTIFICIAL series with an edge: long runs up and down, so the 5-candle trend keeps going."""
    prices, price = [], 1.08500
    for index in range(count):
        step = 0.0001 if (index // switch) % 2 == 0 else -0.0001
        open_, close = price, price + step
        prices.append((open_, max(open_, close) + 0.00001, min(open_, close) - 0.00001, close))
        price = close
    return prices


def walk_series(count, seed=11):
    """ARTIFICIAL pseudo-random walk (Python's Mersenne Twister, fixed seed), without any edge."""
    generator = random.Random(seed)
    prices, price = [], 1.08500
    for _ in range(count):
        step = generator.choice((0.0001, -0.0001)) * (0.5 + generator.random())
        open_, close = price, price + step
        prices.append((open_, max(open_, close) + 0.00002, min(open_, close) - 0.00002, close))
        price = close
    return prices


def series_of(prices, name="ARTIFICIAL/TESTE", **options):
    history = ca.CandleHistory()
    for bar in bars_of(prices, **options):
        history.add(name, PERIOD, bar)
    return history.get(name, PERIOD)


class EvaluationTest(unittest.TestCase):
    def test_a_series_with_an_edge_validates_a_rule_on_separate_data(self):
        series = series_of(edge_series(3200))
        validation = ca.validate(series, 300)
        self.assertTrue(validation.validated, validation.reason)
        self.assertIn(validation.rule, ca.CHART_RULES)
        self.assertGreaterEqual(validation.evaluation.trades, analysis.MIN_EVAL_TRADES)
        low, _ = validation.evaluation.interval()
        self.assertGreater(low, validation.break_even)
        self.assertAlmostEqual(validation.break_even, 1 / 1.9)
        self.assertIn("a referência (cor da última vela), nas mesmas decisões:", validation.reason)
        self.assertIsNotNone(validation.baseline)

    def test_a_walk_without_an_edge_is_not_validated_and_says_why(self):
        validation = ca.validate(series_of(walk_series(3200)), 300)
        self.assertFalse(validation.validated)
        self.assertTrue(
            "não validada" in validation.reason or "nenhuma das 4 regras" in validation.reason
            or "em avaliação" in validation.reason,
            validation.reason,
        )

    def test_too_few_candles_say_exactly_what_is_missing(self):
        validation = ca.validate(series_of(edge_series(120)), 300)
        self.assertFalse(validation.validated)
        self.assertIn("dados insuficientes: 120 velas de 60 s gravadas", validation.reason)
        self.assertIn("são precisos pelo menos 500 momentos de decisão com resultado", validation.reason)
        self.assertIn("cerca de 42 horas de velas de 60 s seguidas", validation.reason)
        self.assertIn("não é múltiplo", ca.validate(series_of(edge_series(120)), 330).reason)
        self.assertEqual(ca.validate(None, 300).reason, "sem velas gravadas deste ativo")

    def test_no_decision_sees_a_candle_after_it(self):
        series = series_of(walk_series(900))
        moments = ca.moments_of(series, 300)
        self.assertGreater(len(moments), 100)
        for moment in moments[::7]:
            window = ca.window_of(series, moment)
            self.assertEqual(window[-1], series.bars[moment.index])
            self.assertTrue(all(bar.start <= series.bars[moment.index].start for bar in window))
            truncated = series_of([(b.open, b.high, b.low, b.close) for b in series.bars[: moment.index + 1]])
            for rule in ca.CHART_RULES + (ca.BASELINE,):
                self.assertEqual(
                    ca.rule_direction(rule, window, RESOLUTION),
                    ca.rule_direction(rule, ca.window_of(truncated, ca.Moment(moment.index, moment.at)), RESOLUTION),
                )
        times = [moment.at for moment in moments]
        self.assertTrue(all((b - a).total_seconds() >= 300 for a, b in zip(times, times[1:])))

    def test_development_ends_before_evaluation_starts(self):
        series = series_of(walk_series(1500))
        development, evaluation, labelled = ca.split(series, 300)
        self.assertTrue(development and evaluation)
        first = evaluation[0].at
        self.assertTrue(all(moment.at + timedelta(seconds=300) <= first for moment in development))
        self.assertLessEqual(len(development) + len(evaluation), len(ca.moments_of(series, 300)))

    def test_a_difference_within_the_resolution_is_a_draw(self):
        prices = edge_series(200)
        series = series_of(prices)
        moment = ca.moments_of(series, 300)[0]
        exit_start = series.bars[moment.index].start + timedelta(seconds=300)
        bar = series.get(exit_start)
        entry = series.bars[moment.index].close
        series.remove(exit_start)
        series.insert(replace(bar, close=entry + RESOLUTION / 2, high=max(bar.high, entry + RESOLUTION)))
        self.assertEqual(ca.result_of(series, moment, 300, CALL), DRAW)

    def test_a_gap_waits_for_a_whole_window_again(self):
        series = series_of(edge_series(200), skip={100})
        moments = ca.moments_of(series, 300)
        after = [moment for moment in moments if series.bars[moment.index].start > START + timedelta(seconds=100 * PERIOD)]
        first = after[0]
        self.assertGreaterEqual(
            (series.bars[first.index].start - (START + timedelta(seconds=100 * PERIOD))).total_seconds(), 21 * PERIOD
        )


class HistoryTest(unittest.TestCase):
    def test_the_same_candle_again_counts_once_and_a_different_one_is_a_conflict(self):
        history = ca.CandleHistory()
        bar = bars_of(trend(1))[0]
        self.assertEqual(history.add("EUR/USD OTC", PERIOD, bar), "nova")
        self.assertEqual(history.add("EURUSD OTC", PERIOD, replace(bar, close=bar.close + RESOLUTION)), "igual")
        self.assertEqual(history.repeated, 1)
        self.assertEqual(history.add("EUR/USD OTC", PERIOD, replace(bar, close=bar.close + 10 * RESOLUTION)), "conflito")
        self.assertEqual(history.get("EUR/USD OTC", PERIOD).bars, [])
        self.assertEqual(history.add("EUR/USD OTC", PERIOD, bar), "conflito")  # the conflict stays
        self.assertEqual(len(history.conflicts), 1)

    def test_the_files_are_read_back_merged_and_never_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            log = ca.CandleLog(runtime, lambda text: None)
            bars = bars_of(trend(30))
            log.write("EUR/USD OTC", PERIOD, bars[:20], START)
            log.close()
            second = ca.CandleLog(runtime, lambda text: None)
            second.write("EUR/USD OTC", PERIOD, bars[10:], START)  # 10 again, with the same values
            second.close()
            other = runtime / "velas-20261018-000000-bad.csv"
            with open(other, "w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(ca.CANDLE_COLUMNS)
                writer.writerow(["EUR/USD OTC", "60", "não é data", "1", "1", "1", "1", "alta", "0.1", "", ca.ORIGIN_CHART])
                writer.writerow(["EUR/USD OTC", "60", START.isoformat(), "1", "2", "0", "1", "alta", "0.1", "", "outra"])
            before = {path.name: path.read_bytes() for path in runtime.iterdir()}
            history = ca.load_candles(runtime)
            self.assertEqual(history.files, 3)
            self.assertEqual(len(history.get("EUR/USD OTC", PERIOD).bars), 30)
            self.assertEqual(history.repeated, 10)
            self.assertEqual(history.skipped, {"linhas de velas inválidas": 2})
            self.assertEqual({path.name: path.read_bytes() for path in runtime.iterdir()}, before)

    def test_a_file_that_cannot_be_written_never_stops_the_reading(self):
        warned = []
        log = ca.CandleLog(Path("/proc/sem-permissao"), warned.append)
        log.write("EUR/USD OTC", PERIOD, bars_of(trend(3)), START)
        log.write("EUR/USD OTC", PERIOD, bars_of(trend(3)), START + timedelta(seconds=1))
        self.assertEqual(len(warned), 1)
        self.assertIn("não foi possível gravar as velas lidas", warned[0])
        self.assertTrue(log.failed)


def observation_of(prices, captured_at, stamp_problem=None, asset="EUR/USD OTC", marker="1.08530"):
    """A ChartObservation made by hand, as observe_chart gives it: the last of prices is the forming candle."""
    count = len(prices)
    made = tuple(
        candles.Candle(candles.UP if close >= open_ else candles.DOWN, 0, 1, 0, 0, 0, 0, open_, high, low, close)
        for open_, high, low, close in prices
    )
    reading = candles.CandleReading(
        None, made, tuple(range(count - 1, -1, -1)), (), candles.ScaleLine(2.0, -RESOLUTION, ((1, "1"), (2, "2"))),
        marker, float(marker), 10.0, 12.0, RESOLUTION, 5,
    )
    stamps, _ = candles.stamp(reading, captured_at, PERIOD)
    return ca.ChartObservation(asset, reading, captured_at, captured_at, stamps, None, stamp_problem)


def setup_of(history=None, log=None):
    model = candles.CandleModel((69, 183, 52), (255, 62, 31), PERIOD)
    return ca.ChartSetup(model, None, "#ativo", 300.0, history or ca.CandleHistory(), log)


class AbsorbTest(unittest.TestCase):
    MOMENT = datetime(2026, 10, 18, 12, 0, 4, tzinfo=timezone.utc)

    def test_the_new_completed_candles_are_recorded_and_the_forming_one_never(self):
        written = []

        class Log:
            def write(self, name, period, bars, read_at):
                written.extend(bars)

        setup = setup_of(log=Log())
        observation = ca.absorb(setup, observation_of(trend(30), self.MOMENT))
        self.assertIsNone(observation.problem)
        self.assertEqual((observation.added, len(written)), (29, 29))
        self.assertEqual(written[-1].start, datetime(2026, 10, 18, 11, 59, tzinfo=timezone.utc))
        again = ca.absorb(setup, observation_of(trend(30), self.MOMENT + timedelta(seconds=20)))
        self.assertEqual((again.added, len(written)), (0, 29))

    def test_a_reading_that_contradicts_many_candles_is_wrong_and_changes_nothing(self):
        setup = setup_of()
        ca.absorb(setup, observation_of(trend(30), self.MOMENT))
        shifted = ca.absorb(setup, observation_of(trend(30, start=1.08600), self.MOMENT))  # another chart, same times
        self.assertIn("a leitura não confere com 29 de 29 velas já gravadas", shifted.problem)
        self.assertFalse(shifted.usable)
        self.assertEqual(len(setup.history.get("EUR/USD OTC", PERIOD).bars), 29)
        self.assertEqual(setup.history.conflicts, set())

    def test_one_or_two_different_candles_leave_both_versions_out(self):
        written = []

        class Log:
            def write(self, name, period, bars, read_at):
                written.extend(bars)

        setup = setup_of(log=Log())
        ca.absorb(setup, observation_of(trend(30), self.MOMENT))
        prices = trend(30)
        open_, high, low, close = prices[5]
        prices[5] = (open_, high + 0.0002, low, close)  # one candle read with another high
        observation = ca.absorb(setup, observation_of(prices, self.MOMENT))
        self.assertIn("1 vela(s) desta leitura diferem das já gravadas", observation.problem)
        self.assertEqual(len(setup.history.conflicts), 1)
        self.assertEqual(len(setup.history.get("EUR/USD OTC", PERIOD).bars), 28)
        self.assertEqual(len(written), 30)  # the other version is written too, for the next start

    def test_near_a_change_of_candle_nothing_is_recorded(self):
        setup = setup_of()
        observation = ca.absorb(setup, observation_of(trend(30), self.MOMENT, stamp_problem="perto da troca"))
        self.assertEqual(observation.added, 0)
        self.assertIsNone(setup.history.get("EUR/USD OTC", PERIOD))


class AssessTest(unittest.TestCase):
    MOMENT = datetime(2026, 10, 18, 12, 0, 4, tzinfo=timezone.utc)

    def validated_setup(self):
        setup = setup_of()
        validation = ca.ChartValidation(True, "regra tendência de 5 velas (velas do gráfico) validada: ...", ca.TREND_RULE)
        setup.cache[(analysis.asset_key("EUR/USD OTC"), PERIOD)] = (0, validation)
        setup.history.series[(analysis.asset_key("EUR/USD OTC"), PERIOD)] = ca.CandleSeries("EUR/USD OTC", PERIOD)
        return setup

    def test_without_a_validated_rule_the_decision_waits_and_the_reading_is_still_shown(self):
        setup = setup_of()
        observation = ca.absorb(setup, observation_of(trend(30), self.MOMENT))
        result = ca.assess(setup, observation, 92.0, self.MOMENT + timedelta(seconds=1))
        self.assertEqual(result.market.direction, ca.ALTA)
        self.assertEqual(result.decision.action, WAIT)
        self.assertIn("dados insuficientes: 29 velas de 60 s gravadas", result.decision.reason)

    def test_a_validated_rule_on_a_current_reading_gives_a_decision_valid_for_moments(self):
        setup = self.validated_setup()
        observation = observation_of(trend(30), self.MOMENT)
        result = ca.assess(setup, observation, 92.0, self.MOMENT + timedelta(seconds=1))
        decision = result.decision
        self.assertEqual(decision.action, CALL)
        self.assertEqual(decision.valid_until, self.MOMENT + timedelta(seconds=5))
        self.assertEqual((decision.sample.quote, decision.sample.payout, decision.sample.origin), ("1.08530", 92.0, ca.ORIGIN_CHART))
        self.assertIs(decision.validation.rule, ca.TREND_RULE)
        falling = ca.assess(setup, observation_of(trend(30, step=-0.0001), self.MOMENT), 92.0, self.MOMENT)
        self.assertEqual(falling.decision.action, PUT)

    def test_what_keeps_a_validated_rule_from_deciding(self):
        setup = self.validated_setup()
        cases = (
            (observation_of(trend(30), self.MOMENT), 92.0, self.MOMENT + timedelta(seconds=6), "a leitura é de há 6,0 s"),
            (observation_of(trend(30), self.MOMENT + timedelta(seconds=16)), 92.0, self.MOMENT + timedelta(seconds=17),
             "a leitura foi 20 s depois da abertura da vela atual, mais que 10 s"),
            (observation_of(trend(30), self.MOMENT), 85.0, self.MOMENT, "o payout 85,0% não permite a primeira entrada"),
            (observation_of(trend(30), self.MOMENT), None, self.MOMENT, "o payout indisponível"),
            (observation_of(trend(30), self.MOMENT, stamp_problem="a captura foi a 1,0 s da troca de vela"), 92.0,
             self.MOMENT, "a captura foi a 1,0 s da troca de vela"),
            (observation_of(flat(30), self.MOMENT), 92.0, self.MOMENT, "a regra não dá direção agora"),
            (replace(observation_of(trend(30), self.MOMENT), problem="a tela mostrava GBP/USD OTC"), 92.0, self.MOMENT,
             "sem leitura das velas: a tela mostrava GBP/USD OTC"),
        )
        for observation, payout, now, message in cases:
            with self.subTest(message=message):
                result = ca.assess(setup, observation, payout, now)
                self.assertEqual(result.decision.action, WAIT)
                self.assertIn(message, result.decision.reason)
                self.assertIsNone(result.decision.valid_until)


class CombineTest(unittest.TestCase):
    def test_the_quotes_decision_keeps_its_place_and_a_disagreement_waits(self):
        wait = analysis.Decision("EUR/USD OTC", WAIT, "aguardando")
        call = analysis.Decision("EUR/USD OTC", CALL, "regra das cotações")
        put = analysis.Decision("EUR/USD OTC", PUT, "regra das velas")
        self.assertIs(ca.combine(wait, None), wait)
        self.assertIs(ca.combine(wait, put), put)
        self.assertIs(ca.combine(call, analysis.Decision("EUR/USD OTC", CALL, "velas")), call)
        self.assertIs(ca.combine(call, wait), call)
        both = ca.combine(call, put)
        self.assertEqual(both.action, WAIT)
        self.assertIn("duas regras validadas discordam", both.reason)


class ReportTest(unittest.TestCase):
    def test_the_report_says_what_was_recorded_and_what_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            self.assertIn("nenhuma vela gravada", "\n".join(ca.report_lines(runtime, 300)))
            log = ca.CandleLog(runtime, lambda text: None)
            log.write("EUR/USD OTC", PERIOD, bars_of(trend(50)), START)
            log.close()
            text = "\n".join(ca.report_lines(runtime, 300))
            self.assertIn("VELAS DO GRÁFICO, lidas na tela: 1 arquivo(s), 50 velas utilizáveis em 1 série(s)", text)
            self.assertIn("EUR/USD OTC, velas de 60 s: 50", text)
            self.assertIn("regras das velas: dados insuficientes: 50 velas de 60 s gravadas", text)


if __name__ == "__main__":
    unittest.main()
