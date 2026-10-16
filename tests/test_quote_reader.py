"""Tests of quote_reader: finding the current-quote marker and validating what the OCR read.

The captures are synthetic (chart_images) and FakeEngine stands in for the OCR, so these
tests need neither a browser nor the OCR packages. They show the rules, not that the
marker of the real platform is found: that needs the comparison with the screen.
"""

import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # chart_images, next to the tests

import chart_images  # noqa: E402
from chart_images import (  # noqa: E402
    CANDLE_GREEN,
    CANDLE_RED,
    CURSOR,
    DARK_BACKGROUND,
    GREEN,
    LABEL_ALPHA,
    LABEL_BASE,
    MARKER,
    FakeEngine,
    blend,
    chart,
)
from pocketautonomo import quote_reader as qr  # noqa: E402

WIDTH = 400  # chart() default


def model_from(image, color=MARKER):
    [box] = [box for box in qr.candidate_boxes(image, 1.0) if box.color == color]
    return qr.MarkerModel.from_box(box, image, 1.0)


class CandidateTest(unittest.TestCase):
    def test_only_boxes_with_text_on_the_right_are_candidates(self):
        image = chart(marker_y=150, cursor_y=60)
        boxes = qr.candidate_boxes(image, 1.0)
        # The cursor label and the marker, top to bottom; no candle, grid line or scale text.
        self.assertEqual([(box.color, box.y0) for box in boxes], [(CURSOR, 60), (MARKER, 150)])
        marker = boxes[1]
        # The dashed price line touching the marker does not widen it.
        self.assertEqual((marker.x0, marker.x1, marker.y1), (WIDTH - 69, WIDTH - 1, 168))
        self.assertTrue(0.03 <= marker.text_ratio <= 0.65)
        self.assertFalse(marker.cut)

    def test_a_box_cut_by_the_edge_is_marked(self):
        image = chart(marker_y=-6)  # the top of the marker is outside the capture
        [box] = qr.candidate_boxes(image, 1.0)
        self.assertTrue(box.cut)

    def test_scaled_capture(self):
        canvas = chart_images.Canvas(800, 600)
        canvas.fill(800 - 140, 300, 800 - 2, 336, MARKER)
        canvas.strokes(800 - 130, 308, 7, chart_images.TEXT, height=20)
        boxes = qr.candidate_boxes(canvas.image(), 2.0)  # 2 device pixels per CSS pixel
        self.assertEqual(len(boxes), 1)
        model = qr.MarkerModel.from_box(boxes[0], canvas.image(), 2.0)
        self.assertEqual((model.width, model.height, model.right_gap), (69.0, 18.0, 1.0))


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.model = model_from(chart(marker_y=150))

    def locate(self, image, model=None):
        return qr.locate_marker(image, 1.0, model or self.model)

    def test_the_marker_is_found_at_any_height(self):
        for y in (25, 150, 270):
            with self.subTest(y=y):
                box, reason = self.locate(chart(marker_y=y, cursor_y=100 if y != 100 else 40))
                self.assertIsNone(reason)
                self.assertEqual((box.y0, box.color), (y, MARKER))

    def test_missing_ambiguous_cut_or_misaligned_marker_is_unavailable(self):
        cases = {
            "o marcador da cotação não apareceu": chart(marker_y=None),
            "2 marcadores com a cor, o tamanho e a posição": chart(marker_y=150, extra=[(WIDTH - 69, 60, MARKER)]),
            "cortado pela borda": chart(marker_y=290),
            # Inside the searched band, but 42 px from the right edge instead of 1.
            "nenhuma alinhada à borda direita": chart(marker_y=None, extra=[(WIDTH - 110, 100, MARKER)]),
        }
        for reason, image in cases.items():
            with self.subTest(reason=reason):
                box, problem = self.locate(image)
                self.assertIsNone(box)
                self.assertIn(reason, problem)

    def test_a_box_of_the_same_color_away_from_the_axis_is_ignored(self):
        box, reason = self.locate(chart(marker_y=150, extra=[(150, 60, MARKER)]))
        self.assertIsNone(reason)
        self.assertEqual(box.y0, 150)

    def test_another_color_needs_to_be_added(self):
        green = chart(marker_y=150, marker_color=GREEN)
        box, reason = self.locate(green)
        self.assertIsNone(box)
        self.assertIn("não apareceu", reason)
        both = self.model.with_colors([GREEN])
        self.assertEqual(both.colors, (MARKER, GREEN))
        self.assertIs(both.with_colors([(GREEN[0] + 5, GREEN[1], GREEN[2])]), both)  # already known
        box, reason = self.locate(green, both)
        self.assertIsNone(reason)
        self.assertEqual(box.color, GREEN)

    def test_model_json_round_trip_and_errors(self):
        data = self.model.to_json()
        self.assertEqual(data, {"colors": ["#2962ff"], "width": 68.0, "height": 18.0, "right_gap": 1.0, "tolerance": 40})
        self.assertEqual(qr.MarkerModel.from_json(data), self.model)
        broken = (
            [],
            {**data, "colors": []},
            {**data, "colors": ["azul"]},
            {**data, "width": 2},
            {**data, "height": True},
            {**data, "tolerance": 99},
            {**data, "y": 150},  # a vertical position is never stored
        )
        for value in broken:
            with self.subTest(value=value), self.assertRaises(ValueError):
                qr.MarkerModel.from_json(value)


class StripTest(unittest.TestCase):
    """A capture of the right strip of strip_width reads exactly as the whole chart."""

    def readings(self, image, model, scale=1.0):
        """(value, reason, box gap to the right edge, box size) on the whole image and on its strip."""
        strip = chart_images.right_strip(image, qr.strip_width(model) * scale)
        out = []
        for capture in (image, strip):
            reading = qr.read_quote(capture, scale, model, FakeEngine())
            box = reading.box
            where = None if box is None else (capture.width - box.x0, capture.width - box.x1, box.y0, box.y1, box.color)
            out.append((reading.value, reading.reason, where))
        return out, strip

    def test_every_case_reads_the_same_on_the_strip(self):
        model = model_from(chart(marker_y=150))
        cases = {
            "marcador": chart(marker_y=150, cursor_y=60),
            "ausente": chart(marker_y=None),
            "ambíguo": chart(marker_y=150, extra=[(WIDTH - 69, 60, MARKER)]),
            "cortado": chart(marker_y=290),
            "desalinhado": chart(marker_y=None, extra=[(WIDTH - 110, 100, MARKER)]),
            "longe do eixo": chart(marker_y=150, extra=[(150, 60, MARKER)]),
        }
        for name, image in cases.items():
            with self.subTest(case=name):
                (whole, part), strip = self.readings(image, model)
                self.assertEqual(whole, part)
                self.assertLess(strip.width, image.width)
                self.assertEqual(strip.height, image.height)

    def test_the_translucent_marker_and_a_scaled_capture_read_the_same(self):
        image = chart_images.translucent_chart(label_y=150, candle=(140, 200))
        [box] = qr.candidate_boxes(image, 1.0)
        model = qr.MarkerModel.from_box(box, image, 1.0)
        for label_y in (30, 150, 240):
            with self.subTest(label_y=label_y):
                (whole, part), _ = self.readings(chart_images.translucent_chart(label_y=label_y), model)
                self.assertEqual(whole, part)
                self.assertEqual(whole[0], "1.26543")
        canvas = chart_images.Canvas(800, 600)
        canvas.fill(800 - 140, 300, 800 - 2, 336, MARKER)
        canvas.strokes(800 - 130, 308, 7, chart_images.TEXT, height=20)
        scaled = canvas.image()
        model = qr.MarkerModel.from_box(qr.candidate_boxes(scaled, 2.0)[0], scaled, 2.0)
        (whole, part), strip = self.readings(scaled, model, scale=2.0)  # 2 device pixels per CSS pixel
        self.assertEqual(whole, part)
        self.assertEqual(whole[0], "1.26543")
        self.assertEqual(strip.width, int(qr.strip_width(model) * 2))

    def test_the_strip_holds_every_column_the_search_looks_at(self):
        model = qr.MarkerModel(((41, 98, 255),), 78.0, 18.0, 1.0)
        reach = qr.marker_reach(model)
        self.assertEqual(reach, 1.0 + 78.0 * qr.WIDTH_RANGE[1] + qr.ALIGN_TOLERANCE + 4)
        self.assertGreaterEqual(qr.strip_width(model), reach + qr.STRIP_MARGIN)
        self.assertLess(qr.strip_width(model), reach + qr.STRIP_MARGIN + 1)


class ReadTextTest(unittest.TestCase):
    def setUp(self):
        self.image = chart(marker_y=150)
        self.model = model_from(self.image)

    def read(self, engine, image=None):
        return qr.read_quote(image or self.image, 1.0, self.model, engine)

    def test_a_quote_is_accepted_only_when_both_readings_agree(self):
        reading = self.read(FakeEngine("1.26543", 0.99))
        self.assertEqual((reading.value, reading.reason), ("1.26543", None))
        self.assertEqual(reading.box.y0, 150)
        refused = {
            "divergiram": FakeEngine(answers=[("1.26543", 0.99), ("1.26548", 0.99)]),
            "confiança 0.50": FakeEngine("1.26543", 0.5),
            "não é uma cotação": FakeEngine("1265", 0.99),  # no decimals: never a quote
            "'abc' no marcador": FakeEngine("abc", 0.99),
        }
        for reason, engine in refused.items():
            with self.subTest(reason=reason):
                reading = self.read(engine)
                self.assertIsNone(reading.value)
                self.assertIn(reason, reading.reason)

    def test_readings_that_differ_only_in_the_decimal_separator_agree(self):
        # The cases of the collector's test on Windows, observations 3 and 4, in both orders.
        for text_a, text_b in (("162,192", "162.192"), ("162.198", "162,198"), ("1,26543", "1.26543")):
            with self.subTest(texts=(text_a, text_b)):
                reading = self.read(FakeEngine(answers=[(text_a, 0.95), (text_b, 0.93)]))
                expected = text_a.replace(",", ".")
                self.assertEqual((reading.value, reading.reason), (expected, None))
                self.assertEqual(
                    reading.note,
                    f"as duas leituras do OCR só divergiram no separador decimal: {text_a!r} e {text_b!r}",
                )
        # Equal texts keep their own separator, with no note, as before.
        reading = self.read(FakeEngine("162,192", 0.99))
        self.assertEqual((reading.value, reading.note), ("162,192", None))
        reading = self.read(FakeEngine("65,432.10", 0.99))
        self.assertEqual((reading.value, reading.note), ("65,432.10", None))

    def test_other_divergences_stay_refused(self):
        refused = {
            # observation 2 of the collector's test: one reading lost the point
            ("162.189", "162189"): "divergiram: '162.189' e '162189'",
            ("162189", "162.189"): "divergiram: '162189' e '162.189'",
            ("162.192", "162.193"): "divergiram",  # another digit
            ("162.192", "162,1920"): "divergiram",  # another precision
            ("162,19", "162.192"): "divergiram",
            ("1.62192", "162.192"): "divergiram",  # the separator in another place
            ("1,234.56", "1.234,56"): "divergiram",  # more than one separator: ambiguous
            ("65,432.10", "65.432.10"): "divergiram",
            ("162.192", ".162192"): "divergiram",
        }
        for (text_a, text_b), reason in refused.items():
            with self.subTest(texts=(text_a, text_b)):
                reading = self.read(FakeEngine(answers=[(text_a, 0.99), (text_b, 0.99)]))
                self.assertIsNone(reading.value)
                self.assertIn(reason, reading.reason)
        # The minimum confidence still holds for both readings of an agreement.
        for scores in ((0.95, 0.85), (0.85, 0.95)):
            with self.subTest(scores=scores):
                engine = FakeEngine(answers=[("162,192", scores[0]), ("162.192", scores[1])])
                reading = self.read(engine)
                self.assertIsNone(reading.value)
                self.assertEqual(
                    reading.reason, "o OCR leu '162,192' e '162.192' com confiança 0.85, abaixo de 0.90"
                )

    def test_agreed_quote_never_inserts_rounds_or_compares_sizes(self):
        self.assertEqual(qr.agreed_quote("162.192", "162.192"), ("162.192", None))
        self.assertEqual(qr.agreed_quote("162,192", "162.192")[0], "162.192")
        self.assertEqual(qr.agreed_quote("0,91210", "0.91210")[0], "0.91210")  # trailing zero kept
        for text_a, text_b in (
            ("162.192", "162192"),  # no separator to insert
            ("162.1920", "162.192"),  # same size, other text
            ("162.192", "162.19200"),
            ("162.192", "162.191"),
            ("1.2.3", "1,2,3"),
            ("", "162.192"),
        ):
            with self.subTest(texts=(text_a, text_b)):
                self.assertEqual(qr.agreed_quote(text_a, text_b), (None, None))

    def test_two_lines_one_quote_and_a_countdown(self):
        canvas = chart_images.Canvas()
        canvas.label(WIDTH - 69, 140, MARKER, height=26, lines=2)
        image = canvas.image()
        model = qr.MarkerModel.from_box(qr.candidate_boxes(image, 1.0)[0], image, 1.0)
        countdown = FakeEngine(answers=[("1.26543", 0.99), ("1.26543", 0.99), ("00:42", 0.99), ("00:42", 0.99)])
        reading = qr.read_quote(image, 1.0, model, countdown)
        self.assertEqual(reading.value, "1.26543")
        self.assertEqual(len(countdown.calls), 4)  # two lines, two readings each
        two_numbers = FakeEngine("1.26543", 0.99)
        reading = qr.read_quote(image, 1.0, model, two_numbers)
        self.assertIsNone(reading.value)
        self.assertIn("mais de uma linha com número", reading.reason)

    def test_dark_text_on_light_and_light_text_on_dark_give_the_same_crop(self):
        for box_color, text_color in ((MARKER, (255, 255, 255)), ((240, 240, 240), (20, 20, 20))):
            with self.subTest(box=box_color):
                canvas = chart_images.Canvas(120, 40, background=(10, 10, 10))
                canvas.label(10, 10, box_color, text=text_color)
                image = canvas.image()
                [box] = qr.find_label_boxes(image, 1.0, 0, 120, lambda c: None if c == (10, 10, 10) else c)
                gray = qr._luminance_free_crop(image, box)
                values = set(gray.data)
                self.assertEqual(min(values), 0)  # the strokes are black
                self.assertEqual(max(values), 255)  # the box is white
                [line] = qr._text_lines(gray)
                self.assertEqual(line.data[0], 255)  # padded with white


class TranslucentMarkerTest(unittest.TestCase):
    """The marker of the validation, which is about 80% opaque: its fill depends on what is behind.

    translucent_chart reproduces what was measured on the user's captures (#3f638e over the
    background, #498390 over a green candle, digits with gaps of 2 px), not the captures.
    """

    over_background = blend(LABEL_BASE, DARK_BACKGROUND, LABEL_ALPHA)  # #3f638e
    over_green = blend(LABEL_BASE, CANDLE_GREEN, LABEL_ALPHA)  # #498391
    over_red = blend(LABEL_BASE, CANDLE_RED, LABEL_ALPHA)  # #6e6b8d

    def calibrate(self, image):
        [box] = qr.candidate_boxes(image, 1.0)
        return box, qr.MarkerModel.from_box(box, image, 1.0)

    def test_the_whole_label_is_one_candidate_even_partly_over_a_candle(self):
        box, model = self.calibrate(chart_images.translucent_chart(label_y=150, candle=(100, 200)))
        # All 55x23 px, the rows of the text included, although only the margins are over
        # the background and the digits leave gaps of 2 px.
        self.assertEqual((box.x0, box.y0, box.x1, box.y1), (WIDTH - 57, 150, WIDTH - 2, 173))
        self.assertEqual(box.color, self.over_green)
        # Both fills enter the model: over the candle, most of it, and over the background.
        self.assertEqual(model.colors, (self.over_green, self.over_background))
        self.assertEqual((model.width, model.height, model.right_gap, model.tolerance), (55.0, 23.0, 2.0, 40))

    def test_the_marker_is_found_over_the_background_and_over_candles(self):
        _, model = self.calibrate(chart_images.translucent_chart(label_y=150, candle=(100, 200)))
        images = {
            "fundo": chart_images.translucent_chart(label_y=60),
            "vela verde": chart_images.translucent_chart(label_y=200, candle=(120, 260)),
            "vela vermelha": chart_images.translucent_chart(label_y=200, candle=(120, 260), candle_color=CANDLE_RED),
        }
        for where, image in images.items():
            with self.subTest(where=where):
                box, reason = qr.locate_marker(image, 1.0, model)
                self.assertIsNone(reason)
                self.assertEqual((box.width, box.height), (55, 23))

    def test_the_fragment_of_the_validation_does_not_find_the_label(self):
        # What the earlier version learned on the user's capture: the part of the label over
        # the candle, 46x9 px, #498390, with the tolerance of 20.
        fragment = qr.MarkerModel(((0x49, 0x83, 0x90),), 46.0, 9.0, 7.0, 20)
        box, reason = qr.locate_marker(chart_images.translucent_chart(label_y=60), 1.0, fragment)
        self.assertIsNone(box)
        self.assertIn("o marcador da cotação não apareceu", reason)

    def test_a_label_fully_over_a_red_candle_needs_its_color(self):
        # Calibrated over the background only, the model does not cover the fill over a red
        # candle (47 from #3f638e): the reading is unavailable until that color is added.
        _, model = self.calibrate(chart_images.translucent_chart(label_y=60))
        self.assertEqual(model.colors, (self.over_background,))
        red = chart_images.translucent_chart(label_y=200, candle=(120, 260), candle_color=CANDLE_RED)
        box, reason = qr.locate_marker(red, 1.0, model)
        self.assertIsNone(box)
        self.assertIn("não apareceu", reason)
        box, reason = qr.locate_marker(red, 1.0, model.with_colors([self.over_red]))
        self.assertIsNone(reason)
        self.assertEqual((box.width, box.height), (55, 23))

    def test_a_line_across_the_label_does_not_split_it(self):
        image = chart_images.translucent_chart(label_y=150, line_through=(11, (255, 255, 255)))
        box, model = self.calibrate(image)
        self.assertEqual((box.y0, box.y1, box.width), (150, 173, 55))
        found, reason = qr.locate_marker(image, 1.0, model)
        self.assertIsNone(reason)
        self.assertEqual((found.y0, found.y1), (150, 173))

    def test_the_background_is_the_dark_picture_not_the_most_frequent_color(self):
        # A background picture has many slightly different colors; in the validation, the
        # most frequent exact color was a red candle's.
        canvas = chart_images.Canvas(200, 200)
        for y in range(200):
            for x in range(200):
                canvas.fill(x, y, x + 1, y + 1, (20 + (x * 7 + y * 3) % 12, 23 + (x + y) % 10, 38 + (x * y) % 9))
        canvas.fill(20, 20, 70, 180, CANDLE_RED)
        image = canvas.image()
        counts = {}
        for y in range(image.height):
            for color in image.row(y, 0, image.width):
                counts[color] = counts.get(color, 0) + 1
        self.assertEqual(max(counts, key=counts.get), CANDLE_RED)  # the old rule's choice
        background = qr.background_color(image, 0, image.width)
        self.assertTrue(qr.colors_match(background, DARK_BACKGROUND, 12), background)


class PreviewImageTest(unittest.TestCase):
    def test_annotated_png_is_a_valid_png(self):
        image = chart(marker_y=150, cursor_y=60)
        boxes = qr.candidate_boxes(image, 1.0)
        data = qr.annotated_png(image, list(enumerate(boxes, 1)))
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        position, chunks = 8, {}
        while position < len(data):
            (length,) = struct.unpack(">I", data[position:position + 4])
            kind = data[position + 4:position + 8]
            body = data[position + 8:position + 8 + length]
            (crc,) = struct.unpack(">I", data[position + 8 + length:position + 12 + length])
            self.assertEqual(crc, zlib.crc32(kind + body) & 0xFFFFFFFF)
            chunks[kind] = body
            position += 12 + length
        width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
        self.assertEqual((width, height), (image.width, image.height))
        raw = zlib.decompress(chunks[b"IDAT"])
        self.assertEqual(len(raw), height * (1 + width * 3))
        self.assertIn(bytes(qr.MARK_COLOR), raw)  # the outlines were drawn
        self.assertNotEqual(raw, b"".join(b"\x00" + image.data[y * width * 3:(y + 1) * width * 3] for y in range(height)))


if __name__ == "__main__":
    unittest.main()
