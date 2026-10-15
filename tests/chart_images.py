"""Synthetic chart captures for the tests, drawn pixel by pixel with the standard library.

They imitate the shapes that quote_reader looks for, not any real platform: label boxes
with strokes of contrasting "text", plain scale text, solid candle bodies, grid lines and
a dashed price line touching the marker. FakeEngine stands in for the OCR: it cannot read
the strokes, so each test says what it "reads".
"""

from pocketautonomo.quote_reader import RgbImage

BACKGROUND = (27, 31, 45)
MARKER = (41, 98, 255)
CURSOR = (74, 79, 94)
TEXT = (255, 255, 255)
SCALE_TEXT = (138, 143, 158)
GREEN, RED = (38, 166, 154), (239, 83, 80)
GRID = (38, 43, 59)


class Canvas:
    def __init__(self, width=400, height=300, background=BACKGROUND):
        self.width, self.height = width, height
        self.pixels = bytearray(bytes(background) * (width * height))

    def fill(self, x0, y0, x1, y1, color):
        for y in range(max(0, y0), min(self.height, y1)):
            for x in range(max(0, x0), min(self.width, x1)):
                i = (y * self.width + x) * 3
                self.pixels[i:i + 3] = bytes(color)

    def strokes(self, x0, y0, glyphs, color, height=10):
        """glyphs fake characters, 2 px strokes 8 px apart, like a line of text."""
        for n in range(glyphs):
            x = x0 + n * 8
            self.fill(x, y0, x + 2, y0 + height, color)
            if n % 2:
                self.fill(x, y0, x + 5, y0 + 2, color)

    def label(self, x0, y0, color, width=68, height=18, glyphs=7, text=TEXT, lines=1):
        self.fill(x0, y0, x0 + width, y0 + height, color)
        if lines == 1:
            self.strokes(x0 + 5, y0 + 4, glyphs, text)
        else:
            self.strokes(x0 + 5, y0 + 3, glyphs, text, height=5)
            self.strokes(x0 + 5, y0 + height - 8, 4, text, height=5)

    def dashed_line(self, y, x0, x1, color):
        for x in range(x0, x1, 6):
            self.fill(x, y, x + 4, y + 1, color)

    def image(self):
        return RgbImage(self.width, self.height, bytes(self.pixels))


def chart(marker_y=150, cursor_y=None, marker_color=MARKER, extra=(), width=400, height=300):
    """A capture with candles, grid, scale text, the marker at marker_y and its price line."""
    canvas = Canvas(width, height)
    axis = width - 70
    for y in range(20, height, 40):
        canvas.fill(0, y, axis, y + 1, GRID)
        canvas.strokes(axis + 6, y - 5, 6, SCALE_TEXT)
    for n, x in enumerate(range(10, axis - 20, 14)):
        top = 60 + (n * 37) % 150
        canvas.fill(x, top, x + 9, top + 30 + n % 20, GREEN if n % 3 else RED)
        canvas.fill(x + 4, top - 8, x + 5, top, GREEN if n % 3 else RED)  # wick
    if cursor_y is not None:
        canvas.label(axis + 1, cursor_y, CURSOR)
    if marker_y is not None:
        canvas.dashed_line(marker_y + 9, 0, axis + 1, marker_color)
        canvas.label(axis + 1, marker_y, marker_color)
    for x0, y0, color in extra:
        canvas.label(x0, y0, color)
    return canvas.image()


class FakeEngine:
    """Stands in for the OCR: every line reads as text with score, unless answers says otherwise.

    answers, when given, is a list of (text, score) used one per call, in order.
    """

    def __init__(self, text="1.26543", score=0.99, answers=None):
        self.text, self.score = text, score
        self.answers = list(answers or [])
        self.calls = []

    def recognize(self, gray):
        self.calls.append((gray.width, gray.height))
        if self.answers:
            return self.answers.pop(0)
        return self.text, self.score


class FakeEngineHolder:
    """Stands in for observe.LazyEngine."""

    def __init__(self, engine=None):
        self.engine = FakeEngine() if engine is None else engine
        self.gets = 0

    def get(self):
        self.gets += 1
        return self.engine
