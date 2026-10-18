"""Tests of the candle reader, pocketautonomo.candles, on synthetic captures.

ARTIFICIAL DATA: every chart here is drawn by chart_images.CandleChart from candles chosen by the
tests, and FakeEngine "reads" the labels that the test says. Nothing here says anything about a
market or about the real platform.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart_images as ci  # noqa: E402

from pocketautonomo import candles  # noqa: E402

MODEL = candles.CandleModel(ci.CANDLE_GREEN, ci.CANDLE_RED, 60)


def chart_of(bars, **options):
    low = min(bar[2] for bar in bars) - 0.0002
    high = max(bar[1] for bar in bars) + 0.0002
    return ci.CandleChart(bars, low, high, **options)


def read(chart, model=MODEL, engine=None):
    return candles.read_candles(chart.image, chart.scale, model, chart.marker, engine or chart.engine())


class ReadCandlesTest(unittest.TestCase):
    def assert_candles(self, reading, chart, skip=()):
        self.assertTrue(reading.available, reading.reason)
        expected = [(index, bar) for index, bar in enumerate(chart.bars) if index not in skip]
        self.assertEqual(len(reading.candles), len(expected))
        last = len(chart.bars) - 1
        self.assertEqual(list(reading.indices), [last - index for index, _ in expected])
        for candle, (_, bar) in zip(reading.candles, expected):
            for got, want in zip((candle.open, candle.high, candle.low, candle.close), bar):
                self.assertLessEqual(abs(got - want), 1.5 * chart.resolution, (candle, bar))
            self.assertEqual(candle.kind, candles.UP if bar[3] >= bar[0] else candles.DOWN)

    def test_every_candle_is_read_with_its_open_high_low_and_close(self):
        chart = chart_of(ci.artificial_bars(40))
        reading = read(chart)
        self.assert_candles(reading, chart)
        self.assertEqual(reading.missing, ())
        self.assertEqual(reading.marker_text, chart.marker_text)
        self.assertGreaterEqual(len(reading.scale.labels), 4)
        self.assertAlmostEqual(reading.resolution, chart.resolution, delta=chart.resolution * 0.01)
        self.assertEqual(reading.spacing, 12)
        self.assertEqual(reading.text(reading.forming.close)[:6], chart.marker_text[:6])
        self.assertEqual(len(reading.completed), 39)

    def test_a_high_density_screen_reads_the_same(self):
        chart = chart_of(ci.artificial_bars(30, seed=3), scale=2)
        reading = read(chart)
        self.assert_candles(reading, chart)
        self.assertAlmostEqual(reading.spacing, 24)

    def test_a_candle_that_was_not_read_is_a_known_gap(self):
        chart = chart_of(ci.artificial_bars(40), skip={30})
        reading = read(chart)
        self.assert_candles(reading, chart, skip={30})
        self.assertEqual(reading.missing, (9,))  # 39 - 30: nine candles before the forming one

    def test_one_misread_label_is_left_out_when_enough_remain(self):
        chart = chart_of(ci.artificial_bars(40))
        answers = list(chart.answers)
        wrong = answers[2][0][:-1] + ("9" if answers[2][0][-1] != "9" else "1")
        answers[2] = answers[3] = (wrong, 0.99)  # the first label, read wrong twice
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assert_candles(reading, chart)
        self.assertEqual([text for _, text in reading.scale.dropped], [wrong])

    def test_without_two_labels_read_there_is_no_scale(self):
        chart = chart_of(ci.artificial_bars(40))
        answers = list(chart.answers[:4]) + [("", 0.1)] * 20  # one label read; the others unreadable
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertFalse(reading.available)
        self.assertIn("a escala de preços não pôde ser lida: 1 rótulo(s) do eixo lido(s), de pelo menos 2", reading.reason)
        self.assertEqual(reading.candles, ())

    def test_labels_that_do_not_fall_on_a_line_refuse_the_scale(self):
        chart = chart_of(ci.artificial_bars(40))
        answers = list(chart.answers)
        for at, pixels in ((4, 20), (6, -20)):  # two labels read 20 pixels off, in order still
            shifted = f"{float(answers[at][0]) + pixels * chart.resolution:.5f}"
            answers[at] = answers[at + 1] = (shifted, 0.99)
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertFalse(reading.available)
        self.assertIn("não ficam numa linha reta", reading.reason)

    def test_labels_that_go_up_the_rows_are_not_a_price_axis(self):
        chart = chart_of(ci.artificial_bars(40))
        texts = [text for _, text in chart.read_labels][::-1]
        answers = list(chart.answers[:2]) + [(text, 0.99) for text in texts for _ in range(2)]
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertFalse(reading.available)
        self.assertIn("não diminui de cima para baixo", reading.reason)

    def test_a_marker_that_disagrees_with_the_axis_refuses_the_reading(self):
        chart = chart_of(ci.artificial_bars(40))
        answers = list(chart.answers)
        shifted = f"{float(chart.marker_text) + 40 * chart.resolution:.5f}"
        answers[0] = answers[1] = (shifted, 0.99)
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertFalse(reading.available)
        self.assertIn(f"o marcador ({shifted}) não confere com a escala lida no eixo", reading.reason)

    def test_a_chart_whose_last_candle_is_not_at_the_marker_is_not_read(self):
        bars = ci.artificial_bars(40)
        price = round(bars[-1][3] + 0.00012, 5)  # the marker elsewhere on the scale, as on a chart scrolled back
        chart = chart_of(bars, marker_price=price)
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertIn("a última vela não termina na cotação do marcador", reading.reason)

    def test_candles_touching_each_other_are_ambiguous(self):
        chart = chart_of(ci.artificial_bars(40), spacing=8, body=8)
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertTrue(
            "mais larga(s) que as velas" in reading.reason or "se sobrepõem" in reading.reason, reading.reason
        )

    def test_a_long_line_of_a_candle_color_is_not_a_candle(self):
        bars = ci.artificial_bars(40)
        chart = chart_of(bars, lines=[(200, 0, 560, ci.CANDLE_GREEN)])  # a level drawn across the chart
        reading = read(chart)
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual(len(reading.candles), 40)  # the line is no candle, and splits none
        for candle, bar in zip(reading.candles, bars):
            # The line covers the row where a wick may end: that end is one row off, no more.
            for got, want in zip((candle.open, candle.high, candle.low, candle.close), bar):
                self.assertLessEqual(abs(got - want), 2 * chart.resolution, (candle, bar))

    def test_the_price_line_across_a_tiny_forming_candle_does_not_split_it(self):
        bars = ci.artificial_bars(30)
        price = bars[-1][3]
        bars[-1] = (price, round(price + 0.00002, 5), round(price - 0.00003, 5), price)  # a candle just opened
        chart = chart_of(bars)
        reading = read(chart)
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual(len(reading.candles), 30)
        self.assertEqual(reading.indices[-1], 0)
        self.assertLessEqual(abs(reading.forming.close - price), 1.5 * chart.resolution)

    def test_something_of_a_candle_color_out_of_place_refuses_the_reading(self):
        chart = chart_of(ci.artificial_bars(40), blobs=[(300, 30, 304, 40, ci.CANDLE_GREEN)])
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertIn("espaçamento entre as velas é irregular", reading.reason)

    def test_without_the_marker_nothing_is_read(self):
        chart = chart_of(ci.artificial_bars(40))
        other = candles.quote_reader.MarkerModel(((10, 200, 10),), 68.0, 18.0, 3.0, 20)
        reading = candles.read_candles(chart.image, 1, MODEL, other, chart.engine())
        self.assertFalse(reading.available)
        self.assertIn("o marcador da cotação não foi achado", reading.reason)

    def test_other_colors_give_too_few_candles(self):
        chart = chart_of(ci.artificial_bars(40))
        wrong = candles.CandleModel((200, 40, 250), (250, 200, 40), 60)
        reading = read(chart, model=wrong)
        self.assertFalse(reading.available)
        self.assertIn("com as cores calibradas no gráfico, de pelo menos 6", reading.reason)

    def test_a_dashed_line_of_the_calibrated_color_is_not_read_as_candles(self):
        chart = chart_of(ci.artificial_bars(40))
        wrong = candles.CandleModel(ci.MARKER, ci.CANDLE_RED, 60)  # the price line's color taken for the up candles
        reading = read(chart, model=wrong)
        self.assertFalse(reading.available)
        self.assertTrue(
            "linhas de um pixel" in reading.reason or "irregular" in reading.reason or "mais larga" in reading.reason,
            reading.reason,
        )


class CalibrationTest(unittest.TestCase):
    def test_the_colors_of_the_candles_are_found(self):
        chart = chart_of(ci.artificial_bars(40))
        self.assertEqual(candles.detect_colors(chart.image, chart.axis), ((ci.CANDLE_GREEN, ci.CANDLE_RED), None))
        flat = ci.Canvas(200, 100, background=ci.DARK_BACKGROUND).image()
        colors, reason = candles.detect_colors(flat, 150)
        self.assertIsNone(colors)
        self.assertIn("nenhuma cor verde", reason)

    def test_the_file_keeps_the_calibration_and_refuses_what_is_wrong(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / candles.CALIBRATION_FILE_NAME
            self.assertIsNone(candles.load_calibration(path))
            candles.save_calibration(path, MODEL)
            self.assertEqual(candles.load_calibration(path), MODEL)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((data["up"], data["down"], data["period"]), ("#45b734", "#ff3e1f", 60))
            for wrong, message in (
                ({"up": "#45b734", "down": "#ff3e1f", "period": 2}, "period"),
                ({"up": "#45b734", "down": "#46b735", "period": 60}, "próximas demais"),
                ({"up": "verde", "down": "#ff3e1f", "period": 60}, "cor inválida"),
                ({"up": "#45b734", "down": "#ff3e1f", "period": 60, "zoom": 1}, "chave desconhecida"),
            ):
                with self.subTest(message=message):
                    path.write_text(json.dumps(wrong), encoding="utf-8")
                    with self.assertRaises(candles.CalibrationError) as raised:
                        candles.load_calibration(path)
                    self.assertIn(message, str(raised.exception))

    def test_the_preview_marks_the_candles(self):
        chart = chart_of(ci.artificial_bars(20))
        reading = read(chart)
        png = candles.preview_png(chart.image, reading)
        self.assertTrue(png.startswith(b"\x89PNG"))


class StampTest(unittest.TestCase):
    def test_the_candles_get_their_starts_from_this_computers_clock(self):
        chart = chart_of(ci.artificial_bars(10))
        reading = read(chart)
        moment = datetime(2026, 10, 18, 12, 0, 30, tzinfo=timezone.utc)
        starts, problem = candles.stamp(reading, moment, 60)
        self.assertIsNone(problem)
        self.assertEqual(starts[0], datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(starts[9], datetime(2026, 10, 18, 11, 51, tzinfo=timezone.utc))
        self.assertEqual(candles.seconds_into(moment, 60), 30)

    def test_near_a_change_of_candle_the_starts_are_not_certain(self):
        reading = read(chart_of(ci.artificial_bars(10)))
        for moment in (
            datetime(2026, 10, 18, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 18, 12, 0, 58, tzinfo=timezone.utc),
        ):
            with self.subTest(moment=moment):
                starts, problem = candles.stamp(reading, moment, 60)
                self.assertIn("da troca de vela", problem)
                self.assertEqual(len(starts), 10)  # still in order, for the reading of the chart
        starts, problem = candles.stamp(reading, datetime(2026, 10, 18, 12, 0, 4, tzinfo=timezone.utc), 60)
        self.assertIsNone(problem)
        self.assertEqual(starts[1] + timedelta(seconds=60), starts[0])


if __name__ == "__main__":
    unittest.main()
