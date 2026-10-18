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

    def test_candles_a_fraction_of_a_pixel_apart_keep_their_places(self):
        # 12.25 pixels apart: gaps of 12 and 13 in turns, as at a display scale of 125%. Measured by the
        # median gap alone, the oldest candles took the index of their neighbors.
        for skip in ((), (20,)):
            with self.subTest(skip=skip):
                chart = chart_of(ci.artificial_bars(40), spacing=12.25, skip=set(skip))
                reading = read(chart)
                self.assert_candles(reading, chart, skip=set(skip))
                self.assertAlmostEqual(reading.spacing, 12.25, delta=0.05)
                self.assertEqual(reading.missing, tuple(39 - index for index in skip))

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


yen_bars, yen_chart = ci.yen_bars, ci.yen_chart
HANGING = {"marker_height": 23, "marker_top": 0, "countdown": "00:42"}  # a 23-pixel marker below its price


class QuoteRowTest(unittest.TestCase):
    """The row of the marker's quote comes from the chart's drawing, not from the middle of the marker's box.

    ARTIFICIAL DATA. The marker of the reported failure was 23 pixels high and its center 11 to 13 pixels
    from where the labels put its quote: these charts draw such markers, with a second line of text,
    and check that every price is read within 1.5 pixels.
    """

    def assert_read(self, chart, reading):
        self.assertTrue(reading.available, reading.reason)
        last = len(chart.bars) - 1
        self.assertEqual(reading.indices[-1], 0)
        for index, candle in zip(reading.indices, reading.candles):
            bar = chart.bars[last - index]
            for got, want in zip((candle.open, candle.high, candle.low, candle.close), bar):
                self.assertLessEqual(abs(got - want), 1.5 * chart.resolution, (index, candle, bar))

    def test_a_two_line_marker_hanging_below_its_price_is_read_where_the_chart_draws_the_quote(self):
        chart = yen_chart(**HANGING)
        reading = read(chart)
        self.assert_read(chart, reading)
        self.assertEqual(len(reading.candles), 40)
        g = reading.geometry
        self.assertEqual(g.marker_box.y0, chart.price_row)  # the box hangs from the price's row
        self.assertEqual((g.marker_box.y0 + g.marker_box.y1) / 2 - g.price_row, 11.5)
        self.assertEqual(g.marker_rows, (chart.price_row + 3, chart.price_row + 9))  # the quote's line, not the countdown
        self.assertAlmostEqual(g.price_line, chart.price_row, delta=0.5)
        self.assertLessEqual(abs(g.deviation), candles.MARKER_TOLERANCE)
        # The middle of the box, which the reading used to take for the quote's row, is 11.3 pixels from
        # where the labels put the quote: the failure that was reported.
        center = (g.marker_box.y0 + g.marker_box.y1) / 2
        self.assertGreater(abs(g.label_line.row(reading.marker_price) - center), 11)
        self.assertAlmostEqual(reading.scale.price(g.price_row), 161.085, places=9)  # the scale goes through the quote

    def test_without_a_price_line_the_close_of_the_forming_candle_gives_the_row(self):
        chart = yen_chart(price_line=False, **HANGING)
        reading = read(chart)
        self.assert_read(chart, reading)
        g = reading.geometry
        self.assertIsNone(g.price_line)
        self.assertEqual(g.marker_lines, ())
        self.assertEqual(g.price_row, g.close_row)

    def test_a_marker_above_its_price_and_a_screen_of_higher_density_are_read_as_well(self):
        for options in ({"marker_height": 23, "marker_top": -23}, {"scale": 2, **HANGING}):
            with self.subTest(options=options):
                chart = yen_chart(**options)
                self.assert_read(chart, read(chart))

    def test_the_marker_misread_in_a_hanging_box_is_still_refused(self):
        chart = yen_chart(**HANGING)
        answers = list(chart.answers)
        answers[0] = answers[1] = ("161.115", 0.99)  # 0.030 off: 13 pixels of this scale
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertFalse(reading.available)
        self.assertIn("o marcador (161.115) não confere com a escala lida no eixo: os rótulos põem esse preço na linha", reading.reason)
        self.assertIn("e o limite é 3.0", reading.reason)

    def test_a_chart_scrolled_back_without_a_price_line_is_refused(self):
        # The marker at a quote above the last close, as on a chart scrolled back: 42 pixels above it, the
        # close is outside the marker's box; 24 pixels above, it is just under the box, and the labels
        # refuse the quote at that row.
        for quote, problem in (
            (161.180, "a última vela não termina na cotação do marcador (a linha da cotação no gráfico, 293 da captura"
                      " pelo fechamento da vela que se forma, fica fora das linhas do marcador, 251 a 273)"),
            (161.140, "o marcador (161.140) não confere com a escala lida no eixo"),
        ):
            with self.subTest(quote=quote):
                reading = read(yen_chart(marker_price=quote, price_line=False, **HANGING))
                self.assertFalse(reading.available)
                self.assertIn(problem, reading.reason)

    def test_a_forming_candle_hidden_by_the_price_line_is_not_taken_for_the_one_before(self):
        # Flat candles: the last one, just opened, is two rows high, and the opaque dashes of the price line
        # cover all of it but three pixels.
        chart = yen_chart(bars=yen_bars(step=0.004))
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertIn("a vela que se forma pode estar escondida pela linha do preço", reading.reason)

    def test_a_candle_cut_by_the_left_edge_does_not_let_a_hidden_candle_through(self):
        flat = yen_bars(step=0.004)
        row = yen_chart(bars=flat).price_row
        chart = yen_chart(bars=flat, blobs=[(0, row - 10, 6, row + 10, ci.CANDLE_GREEN)])  # across the quote's row
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertIn("a vela que se forma pode estar escondida pela linha do preço", reading.reason)

    def test_a_thin_line_of_a_candle_color_where_the_next_candle_would_be_is_no_hidden_candle(self):
        chart = yen_chart()
        column = chart.axis - 18 - 8 + 12 + 4  # the middle of the place one spacing right of the last candle
        drawn = yen_chart(blobs=[(column, 0, column + 1, 360, ci.CANDLE_GREEN)])
        reading = read(drawn)
        self.assert_read(drawn, reading)
        self.assertEqual(len(reading.candles), 40)

    def test_a_bluish_line_at_the_marker_is_no_price_line(self):
        # No price line of the marker's paint, and a faint bluish level 5 pixels below the quote, in the rows of
        # the marker: it is listed, not taken for the price line.
        chart = yen_chart(price_line=False)
        drawn = yen_chart(price_line=False, lines=[(chart.price_row + 5, 0, 566, (44, 52, 90))])
        reading = read(drawn)
        self.assertTrue(reading.available, reading.reason)
        g = reading.geometry
        self.assertEqual([line.top for line in g.marker_lines], [chart.price_row + 5])
        self.assertIsNone(g.price_line)
        self.assertEqual(g.price_row, g.close_row)

    def test_a_price_line_of_a_candle_color_does_not_pass_for_a_hidden_candle(self):
        chart = yen_chart()
        line = [(chart.price_row, 0, 566, ci.CANDLE_GREEN)]  # an opaque green line at the quote, across the chart
        drawn = ci.CandleChart(chart.bars, 160.98, 161.70, decimals=3, label_step=0.2, lines=line)
        reading = read(drawn)
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual(len(reading.candles), 40)


class AxisTest(unittest.TestCase):
    """The labels of the axis: where their prices are, and what is not one. ARTIFICIAL DATA."""

    def test_labels_drawn_off_their_prices_are_refused_without_a_grid(self):
        chart = yen_chart(label_shift=4)  # the middle of each label's digits 4 pixels below its price
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertIn("a 4.2 pixels da linha da cotação no gráfico (293, a linha do preço), e o limite é 3.0", reading.reason)
        self.assertIsNone(reading.geometry.grid_offset)

    def test_a_grid_line_beside_every_label_gives_the_row_of_its_price(self):
        for shift in (4, 12, -9):
            with self.subTest(shift=shift):
                chart = yen_chart(label_shift=shift, grid=(34, 38, 56))
                reading = read(chart)
                self.assertTrue(reading.available, reading.reason)
                g = reading.geometry
                self.assertAlmostEqual(g.grid_offset, -shift - 0.0, delta=1.0)
                for line in g.axis_lines:
                    self.assertAlmostEqual(line.tick, line.center - shift, delta=1.0)
                self.assertLessEqual(abs(g.deviation), 1.0)

    def test_grid_lines_at_different_distances_from_their_labels_are_no_grid(self):
        rows = [row for row, _ in yen_chart().labels]
        drawn = yen_chart(lines=[(row + 4 * n, 0, 566, (34, 38, 56)) for n, row in enumerate(rows)])  # 0, 4, 8, 12 below
        reading = read(drawn)
        self.assertTrue(reading.available, reading.reason)
        self.assertIsNone(reading.geometry.grid_offset)

    def test_a_bright_line_across_a_label_is_taken_out_of_its_text(self):
        chart = yen_chart()
        row = chart.labels[1][0]  # through the middle of the digits of 161.400
        drawn = yen_chart(lines=[(row, 540, 640, (150, 150, 160))])
        engine = RecordingEngine(answers=list(drawn.answers))
        reading = read(drawn, engine=engine)
        self.assertTrue(reading.available, reading.reason)
        line = reading.geometry.axis_lines[1]
        self.assertEqual((line.top, line.bottom, line.text), (row - 5, row + 5, "161.400"))
        gray = engine.images[4]  # the gray reading of the second label
        for y in range(gray.height):  # no row of the crop is dark from edge to edge: the line was taken out
            self.assertTrue(any(value >= 128 for value in gray.data[y * gray.width:(y + 1) * gray.width]))

    def test_at_most_eight_labels_are_read(self):
        drawn = yen_chart(label_step=0.05)  # 15 labels on the axis
        reading = read(drawn)
        self.assertTrue(reading.available, reading.reason)
        g = reading.geometry
        self.assertEqual(sum(1 for line in g.axis_lines if line.price is not None), candles.MAX_AXIS_LABELS)
        self.assertIn("não lida: 8 rótulos já tinham sido lidos acima dela", [line.problem for line in g.axis_lines])

    def test_a_line_beside_only_one_label_is_no_grid(self):
        chart = yen_chart(lines=[(150, 0, 560, (34, 38, 56))])  # a level drawn near 161.400 only
        reading = read(chart)
        self.assertTrue(reading.available, reading.reason)
        self.assertIsNone(reading.geometry.grid_offset)
        self.assertTrue(all(line.tick is None for line in reading.geometry.axis_lines))

    def test_a_vertical_line_across_the_axis_leaves_the_labels_readable(self):
        # Like a line of the expiration time in the columns of the axis: every row has a pixel of it.
        chart = yen_chart(blobs=[(608, 0, 609, 360, (120, 120, 140))])
        reading = read(chart)
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual([line.text for line in reading.geometry.axis_lines], ["161.600", "161.400", "161.200", "161.000"])

    def test_a_candle_under_the_axis_is_not_text(self):
        chart = yen_chart(under_axis=[(40, 200, ci.CANDLE_GREEN)])
        reading = read(chart)
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual(len(reading.scale.labels), 4)

    def test_a_label_whose_two_readings_disagree_or_are_unsure_is_left_out(self):
        chart = yen_chart()
        for wrong, problem in (
            (("161.460", 0.99), "as duas leituras do OCR divergiram: '161.400' e '161.460'"),
            (("161409", 0.99), "as duas leituras do OCR divergiram: '161.400' e '161409'"),  # another digit, too
            (("16.1400", 0.99), "as duas leituras do OCR divergiram: '161.400' e '16.1400'"),
            (("161.400", 0.62), "o OCR leu '161.400' com confiança 0.62, abaixo de 0.90"),
        ):
            with self.subTest(problem=problem):
                answers = list(chart.answers)
                answers[5] = wrong  # the black-and-white reading of the second label
                reading = read(chart, engine=ci.FakeEngine(answers=answers))
                self.assertTrue(reading.available, reading.reason)  # three labels are enough
                self.assertEqual(reading.unread_labels, 1)
                self.assertEqual([text for _, text in reading.scale.labels], ["161.600", "161.200", "161.000"])
                [unread] = [line for line in reading.geometry.axis_lines if line.price is None]
                self.assertEqual(unread.problem, problem)

    def test_a_decimal_point_lost_by_one_reading_is_confirmed_by_the_decimals_of_the_marker(self):
        # As on the Windows of the CI: the black-and-white reading lost the faint point of every label.
        chart = yen_chart()
        answers = list(chart.answers)
        for at in range(3, len(answers), 2):
            answers[at] = (answers[at][0].replace(".", ""), 0.99)
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual([text for _, text in reading.scale.labels], ["161.600", "161.400", "161.200", "161.000"])
        line = reading.geometry.axis_lines[0]
        self.assertEqual(
            line.note, "o separador decimal só apareceu numa das leituras ('161.600' e '161600'), com as casas do marcador"
        )
        self.assertIn("rótulo 161.600 (o separador decimal só apareceu numa das leituras", "\n".join(candles.geometry_lines(reading)))

    def test_a_lost_point_needs_the_digits_and_the_decimals_of_the_marker(self):
        for gray, bw, decimals, quote in (
            ("161.600", "161600", 3, "161.600"),
            ("161600", "161,600", 3, "161.600"),  # either reading may keep it, a comma too
            ("161.600", "161600", 5, None),  # a label with other decimals than the marker's
            ("16.1600", "161600", 3, None),
            ("161.600", "161690", 3, None),
            ("161.600", "16160", 3, None),
            ("161.600", "161600", None, None),
            ("1.61.600", "161600", 3, None),
        ):
            with self.subTest(gray=gray, bw=bw, decimals=decimals):
                self.assertEqual(candles.lost_separator(gray, bw, decimals), quote)

    def test_without_any_label_read_the_reading_says_why_for_each_line(self):
        chart = yen_chart()
        answers = list(chart.answers[:2]) + [("", 0.1)] * 8  # an attempt where the OCR reads none of them
        reading = read(chart, engine=ci.FakeEngine(answers=answers))
        self.assertFalse(reading.available)
        self.assertIn("0 rótulo(s) do eixo lido(s), de pelo menos 2", reading.reason)
        self.assertEqual(
            [line.problem for line in reading.geometry.axis_lines], ["o OCR leu 'nada', que não é um preço"] * 4
        )
        self.assertEqual(reading.unread_labels, 4)

    def test_the_black_and_white_reading_gets_the_line_enlarged(self):
        chart = yen_chart()
        engine = chart.engine()
        read(chart, engine=engine)
        label_calls = engine.calls[2:]  # after the two readings of the marker
        self.assertEqual(len(label_calls), 8)
        for gray, enlarged in zip(label_calls[0::2], label_calls[1::2]):
            self.assertGreaterEqual(enlarged[1], candles.BW_HEIGHT)
            self.assertEqual(enlarged[0] * gray[1], gray[0] * enlarged[1])  # the same line, by a whole factor


class RecordingEngine(ci.FakeEngine):
    """FakeEngine that keeps every image it was given."""

    def __init__(self, **options):
        super().__init__(**options)
        self.images = []

    def recognize(self, gray):
        self.images.append(gray)
        return super().recognize(gray)


class DiagnosisTest(unittest.TestCase):
    """What the calibration shows of a reading. ARTIFICIAL DATA."""

    def test_a_reading_refused_between_the_forming_candle_and_its_close_has_a_diagnosis(self):
        for chart, problem in (
            (chart_of(ci.artificial_bars(40), blobs=[(300, 30, 304, 40, ci.CANDLE_GREEN)]), "espaçamento entre as velas é irregular"),
            (chart_of(ci.artificial_bars(40), spacing=8, body=8), ""),
        ):
            with self.subTest(problem=problem):
                reading = read(chart)
                self.assertFalse(reading.available)
                self.assertIn(problem, reading.reason)
                text = "\n".join(candles.geometry_lines(reading))
                self.assertIn("Diagnóstico, em pixels da captura", text)
                self.assertNotIn("linhas finas na altura do marcador", text)  # not looked for before the refusal
                candles.diagnostic_png(chart.image, reading)

    def test_the_diagnosis_tells_where_each_part_was_found(self):
        chart = yen_chart(**HANGING)
        text = "\n".join(candles.geometry_lines(read(chart)))
        for part in (
            "Diagnóstico, em pixels da captura (640x360, 1 por pixel CSS; linha 0 no topo):",
            "marcador: colunas 569 a 636, linhas 293 a 315, centro na linha 304.5; a cotação está nas linhas 296 a 301 dele.",
            "linhas 59 a 68, meio na linha 64: rótulo 161.600",
            "linhas 326 a 335, meio na linha 331: rótulo 161.000",
            "linhas da grade: não há uma ao lado de cada rótulo; vale o meio dos algarismos.",
            "reta dos rótulos: 1 pixel = 0.00224719",
            "linhas finas na altura do marcador: linha 293 (#2962ff).",
            "linha do preço (a única da cor do marcador): preço na linha 293.0.",
            "linha da cotação no gráfico: 293.",
            "o centro da caixa do marcador está a +11.5 pixels dela; a borda de cima, a +0.0; a de baixo, a +23.0.",
            "a reta dos rótulos põe a cotação do marcador na linha 293.2: +0.2 pixels da linha da cotação no gráfico (limite: 3.0).",
        ):
            self.assertIn(part, text)

    def test_a_failed_reading_keeps_what_was_found_up_to_its_failure(self):
        chart = yen_chart(label_shift=4)
        reading = read(chart)
        self.assertFalse(reading.available)
        text = "\n".join(candles.geometry_lines(reading))
        self.assertIn("a reta dos rótulos põe a cotação do marcador na linha 297.2: +4.2 pixels", text)
        png = candles.diagnostic_png(chart.image, reading)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        self.assertEqual((width, height), (640 + candles.RULER, 360))
        bare = candles.diagnostic_png(chart.image)  # a capture where the marker was not found: no marks
        self.assertEqual(int.from_bytes(bare[16:20], "big"), 640 + candles.RULER)


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

class IndexTest(unittest.TestCase):
    """Each candle read: its index, its place in the capture, its color and prices, and its number on the preview.

    ARTIFICIAL DATA. On the preview of the Pocket Option, the 0 was on the red candle before the green one that
    was forming: the numbers were drawn left of each candle's box, over the candle before it. These charts draw
    that case, a green candle forming after a red one, and check, candle by candle, that the reading puts it at
    its index, at the columns where it was drawn, with its color and prices, and that the preview draws that
    index over those columns and no other.
    """

    CASES = (
        ("as on the reported preview", {}),
        ("candles 12.25 pixels apart, as at 125%", {"spacing": 12.25}),
        ("a screen of density 2", {"scale": 2}),
        ("candles 8 pixels apart", {"spacing": 8, "body": 5}),
        ("index 3 not read", {"skip": {36}}),
        ("the hanging two-line marker", HANGING),
    )

    def chart(self, **options):
        return yen_chart(ci.green_after_red(yen_bars()), **options)

    def assert_indexed(self, chart, reading, prices=True):
        """Every candle of reading at the columns, color and prices of bars[last - index], oldest first."""
        self.assertTrue(reading.available, reading.reason)
        last = len(chart.bars) - 1
        self.assertEqual(reading.indices[-1], 0)
        self.assertEqual(list(reading.indices), sorted(reading.indices, reverse=True))
        self.assertEqual(reading.forming.kind, candles.UP)  # the green one, the rightmost
        for index, candle in zip(reading.indices, reading.candles):
            bar = chart.bars[last - index]
            x0, x1 = chart.columns[last - index]
            self.assertLessEqual(max(abs(candle.x0 - x0), abs(candle.x1 - x1)), 1, (index, candle, (x0, x1)))
            self.assertEqual(candle.kind, candles.UP if bar[3] >= bar[0] else candles.DOWN, index)
            for got, want in zip((candle.open, candle.high, candle.low, candle.close), bar if prices else ()):
                self.assertLessEqual(abs(got - want), 1.5 * chart.resolution, (index, candle, bar))

    def assert_numbered(self, chart, reading, png):
        """Each index under PREVIEW_NUMBERED drawn once, over the columns of its own candle and of no other one."""
        image = ci.decode_png(png)
        numbers = ci.preview_numbers(image)
        last = len(chart.bars) - 1
        expected = sorted(index for index in reading.indices if index < candles.PREVIEW_NUMBERED)
        self.assertEqual(sorted(int(text) for text, _, _ in numbers), expected)
        by_index = dict(zip(reading.indices, reading.candles))
        half = chart.spacing_px / 2
        for text, color, (x0, y0, x1, y1) in numbers:
            index = int(text)
            own = chart.columns[last - index]
            center = (own[0] + own[1]) / 2
            self.assertTrue(center - half <= x0 and x1 <= center + half, (index, (x0, x1), own))
            for other, (a, b) in enumerate(chart.columns):
                if other != last - index:
                    self.assertFalse(a < x1 and x0 < b, f"o número {index} cobre a vela {last - other}")
            candle = by_index[index]
            self.assertTrue(y1 <= candle.top or y0 > candle.bottom, (index, (y0, y1), candle))
            self.assertEqual(color, candles.FORMING_COLOR if index == 0 else candles.PREVIEW_COLOR)
        forming = reading.forming
        self.assertEqual(image.pixel(forming.x0 - 1, (forming.top + forming.bottom) // 2), candles.FORMING_COLOR)
        for index, candle in zip(reading.indices, reading.candles):
            if index >= candles.PREVIEW_NUMBERED:  # the other candles read: a tick under each
                self.assertEqual(image.pixel(round(candle.center), candle.bottom + 2), candles.PREVIEW_COLOR, index)

    def test_the_forming_candle_is_0_and_the_last_closed_1_at_their_places_and_on_the_preview(self):
        for name, options in self.CASES:
            with self.subTest(name):
                chart = self.chart(**options)
                reading = read(chart)
                self.assert_indexed(chart, reading)
                by_index = dict(zip(reading.indices, reading.candles))
                self.assertEqual(by_index[1].kind, candles.DOWN)  # the red one before it
                self.assert_numbered(chart, reading, candles.preview_png(chart.image, reading))
                if "skip" in options:
                    self.assertEqual(reading.missing, (3,))

    def test_a_number_that_does_not_fit_above_its_candle_goes_below_it(self):
        bars = ci.green_after_red(yen_bars())
        open_, high, low, close = bars[-4]
        bars[-4] = (open_, 161.70, low, close)  # the wick of candle 3 up to the top of the chart
        chart = yen_chart(bars, top=4)
        reading = read(chart)
        # The places and colors; the prices are those of the other cases, on another scale, where a candle with
        # its open at its close can fall a pixel and a half from its price.
        self.assert_indexed(chart, reading, prices=False)
        numbers = {int(text): box for text, _, box in ci.preview_numbers(ci.decode_png(candles.preview_png(chart.image, reading)))}
        candle = dict(zip(reading.indices, reading.candles))[3]
        self.assertGreater(numbers[3][1], candle.bottom)  # below it
        self.assert_numbered(chart, reading, candles.preview_png(chart.image, reading))

    def test_candles_too_close_for_numbers_get_only_their_outline(self):
        chart = self.chart(spacing=6, body=4)
        reading = read(chart)
        self.assert_indexed(chart, reading)
        self.assertLess(reading.spacing, candles.MIN_NUMBER_SPACING)
        image = ci.decode_png(candles.preview_png(chart.image, reading))
        self.assertEqual(ci.preview_numbers(image), [])  # no number that could be read as another
        forming = reading.forming
        self.assertEqual(image.pixel(forming.x0 - 1, (forming.top + forming.bottom) // 2), candles.FORMING_COLOR)
        self.assertIn("os números não caberiam sem confusão", candles.preview_legend(reading))
        self.assertEqual(candles.preview_legend(read(self.chart())), candles.PREVIEW_LEGEND)

    def test_a_forming_candle_not_read_that_moved_beyond_the_tolerance_is_refused_not_replaced(self):
        # A green that the calibration does not know, risen 0.012, 5 pixels: the red one before it closes 5 pixels
        # from the price line, and the reading is refused. Within MARKER_TOLERANCE, 3 pixels here, it would pass
        # for the red one: the limit that STATUS, item 39, keeps.
        bars = ci.green_after_red(yen_bars())
        chart = yen_chart(bars, colors={len(bars) - 1: (120, 220, 100)})
        reading = read(chart)
        self.assertFalse(reading.available)
        self.assertIn("a última vela não termina na cotação do marcador", reading.reason)
        self.assertEqual(reading.candles, ())

    def test_the_terminal_lists_the_same_candles_as_the_numbers_of_the_preview(self):
        from pocketautonomo import chart as chart_program

        chart = self.chart(skip={36})
        reading = read(chart)
        lines = chart_program.preview_lines(MODEL, reading)
        listed = [line for line in lines if line.startswith("    vela ")]
        self.assertEqual(len(listed), candles.PREVIEW_NUMBERED)
        self.assertTrue(listed[-1].startswith("    vela a que se forma: alta, abertura 161.0"), listed[-1])
        self.assertTrue(listed[-2].startswith("    vela 1: baixa, abertura"), listed[-2])
        self.assertEqual(listed[2], "    vela 3: nenhuma vela lida nesta posição")


class CountdownTest(unittest.TestCase):
    """The countdown of the marker, against this computer's clock. ARTIFICIAL readings."""

    def test_a_countdown_is_read_only_when_both_readings_are_the_same(self):
        for others, expected in (
            ((("00:42", "00:42", 0.99, (1, 2)),), (42, "00:42")),
            ((("01:05:09", "01:05:09", 0.95, (1, 2)),), (3909, "01:05:09")),
            ((("00:42", "00:43", 0.99, (1, 2)),), (None, None)),
            ((("00:42", "00:42", 0.62, (1, 2)),), (None, None)),
            ((("00:61", "00:61", 0.99, (1, 2)),), (None, None)),
            ((("M1", "M1", 0.99, (1, 2)),), (None, None)),
            ((("00:42", "00:42", 0.99, (1, 2)), ("00:10", "00:10", 0.99, (3, 4))), (None, None)),
            ((), (None, None)),
        ):
            with self.subTest(others=others):
                self.assertEqual(candles.countdown_of(others), expected)

    def test_the_countdown_is_compared_with_the_time_left_by_this_computers_clock(self):
        reading = candles.CandleReading(countdown=42, countdown_text="00:42")
        at = datetime(2026, 10, 18, 12, 0, 18, 300000, tzinfo=timezone.utc)  # 41.7 s left by this clock
        remaining, problem = candles.countdown_check(reading, at, 60)
        self.assertAlmostEqual(remaining, 41.7)
        self.assertIsNone(problem)
        remaining, problem = candles.countdown_check(reading, at - timedelta(seconds=8), 60)  # 49.7 s left by this clock
        self.assertIn("o contador (00:42) mostra 7,7 s a menos que o relógio deste computador para o fim da vela", problem)
        self.assertIn("mostra 10,3 s a mais", candles.countdown_check(reading, at + timedelta(seconds=10), 60)[1])  # 31.7 s left
        # 40 s apart in the middle of a candle is 40 s, never 20 s by a period's turn.
        ten = candles.CandleReading(countdown=10, countdown_text="00:10")
        self.assertIn("mostra 40,0 s a menos", candles.countdown_check(ten, datetime(2026, 10, 18, 12, 0, 10, tzinfo=timezone.utc), 60)[1])
        # At a change of candle, the platform already in the next candle, or still in this one, agrees.
        for countdown, second in ((59, 59.5), (0, 0.5)):
            changing = candles.CandleReading(countdown=countdown, countdown_text=f"00:{countdown:02d}")
            moment = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=second)
            self.assertIsNone(candles.countdown_check(changing, moment, 60)[1], (countdown, second))
        other_period = candles.CandleReading(countdown=252, countdown_text="04:12")
        self.assertIn(
            "passa do período calibrado, 60 s: se ele conta o fim da vela", candles.countdown_check(other_period, at, 60)[1]
        )
        self.assertEqual(candles.countdown_check(candles.CandleReading(), at, 60), (None, None))

    def test_the_countdown_line_of_the_marker_is_kept_with_the_reading(self):
        reading = read(yen_chart(**HANGING))
        self.assertTrue(reading.available, reading.reason)
        self.assertEqual((reading.countdown, reading.countdown_text), (42, "00:42"))
        self.assertEqual(reading.marker_others, (("00:42", "00:42"),))


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
