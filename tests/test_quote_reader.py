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
from chart_images import CURSOR, GREEN, MARKER, FakeEngine, chart  # noqa: E402
from pocketautonomo import quote_reader as qr  # noqa: E402

WIDTH = 400  # chart() default


def model_from(image, color=MARKER):
    [box] = [box for box in qr.candidate_boxes(image, 1.0) if box.color == color]
    return qr.MarkerModel.from_box(box, image.width, 1.0)


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
        model = qr.MarkerModel.from_box(boxes[0], 800, 2.0)
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
        both = self.model.with_color(GREEN)
        self.assertEqual(both.colors, (MARKER, GREEN))
        self.assertIs(both.with_color((GREEN[0] + 5, GREEN[1], GREEN[2])), both)  # within tolerance
        box, reason = self.locate(green, both)
        self.assertIsNone(reason)
        self.assertEqual(box.color, GREEN)

    def test_model_json_round_trip_and_errors(self):
        data = self.model.to_json()
        self.assertEqual(data, {"colors": ["#2962ff"], "width": 68.0, "height": 18.0, "right_gap": 1.0, "tolerance": 20})
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

    def test_two_lines_one_quote_and_a_countdown(self):
        canvas = chart_images.Canvas()
        canvas.label(WIDTH - 69, 140, MARKER, height=26, lines=2)
        image = canvas.image()
        model = qr.MarkerModel.from_box(qr.candidate_boxes(image, 1.0)[0], WIDTH, 1.0)
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
                [box] = qr.find_label_boxes(image, 1.0, 0, 120, lambda c: None if c == (10, 10, 10) else c, lambda k: k)
                gray = qr._luminance_free_crop(image, box)
                values = set(gray.data)
                self.assertEqual(min(values), 0)  # the strokes are black
                self.assertEqual(max(values), 255)  # the box is white
                [line] = qr._text_lines(gray)
                self.assertEqual(line.data[0], 255)  # padded with white


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
