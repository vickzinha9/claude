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


# The marker measured on the captures of the validation: about 80% opaque, so its fill was
# #3f638e over the dark background and #498390 over a green candle. LABEL_BASE and
# LABEL_ALPHA reproduce those two fills (#3f638e and #498391) over the colors below.
LABEL_BASE, LABEL_ALPHA = (74, 118, 168), 0.8
DARK_BACKGROUND = (20, 23, 38)  # #141726, the background behind the label in the capture
CANDLE_GREEN, CANDLE_RED = (69, 183, 52), (255, 62, 31)  # #45b734 and #ff3e1f


def blend(color, behind, alpha):
    """color drawn with opacity alpha over behind."""
    return tuple(round(alpha * c + (1 - alpha) * b) for c, b in zip(color, behind))


class Canvas:
    def __init__(self, width=400, height=300, background=BACKGROUND):
        self.width, self.height = width, height
        self.pixels = bytearray(bytes(background) * (width * height))

    def fill(self, x0, y0, x1, y1, color):
        for y in range(max(0, y0), min(self.height, y1)):
            for x in range(max(0, x0), min(self.width, x1)):
                i = (y * self.width + x) * 3
                self.pixels[i:i + 3] = bytes(color)

    def get(self, x, y):
        i = (y * self.width + x) * 3
        return tuple(self.pixels[i:i + 3])

    def translucent(self, x0, y0, x1, y1, color, alpha):
        """Draw color with opacity alpha over what is already there."""
        for y in range(max(0, y0), min(self.height, y1)):
            for x in range(max(0, x0), min(self.width, x1)):
                i = (y * self.width + x) * 3
                self.pixels[i:i + 3] = bytes(blend(color, self.get(x, y), alpha))

    def tight_text(self, x0, y0, x1, height=9, color=TEXT):
        """Glyph strokes 3 px wide with 2 px gaps, like the digits of the real marker.

        The gaps are shorter than MIN_RUN, so in these rows only the label's margins are
        left of its fill: the rows that the earlier box detection dropped.
        """
        for x in range(x0, x1 - 2, 5):
            self.fill(x, y0, x + 3, y0 + height, color)

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


def translucent_chart(
    label_y=150, candle=(None, None), candle_color=CANDLE_GREEN, width=400, height=300, line_through=None
):
    """A capture like the validation's: a semi-transparent marker at the right edge.

    candle, a (top, bottom) pair of rows, puts a candle body under the label's middle
    columns, from 3 px inside its left edge to 5 px inside its right edge, as in the
    capture where the calibration took only the part over the candle. line_through, a
    (row, color) pair, draws an opaque line of 2 rows across the label.
    """
    canvas = Canvas(width, height, background=DARK_BACKGROUND)
    x0, x1 = width - 57, width - 2  # 55 px wide, 2 px from the right edge
    top, bottom = candle
    if top is not None:
        canvas.fill(x0 + 3, top, x1 - 5, bottom, candle_color)
    canvas.fill(width - 160, 40, width - 112, 260, CANDLE_RED)  # a candle away from the label
    if label_y is not None:
        canvas.fill(0, label_y + 11, x0, label_y + 12, blend(LABEL_BASE, DARK_BACKGROUND, LABEL_ALPHA))
        canvas.translucent(x0, label_y, x1, label_y + 23, LABEL_BASE, LABEL_ALPHA)
        canvas.tight_text(x0 + 5, label_y + 7, x1 - 6)
        if line_through is not None:
            row, color = line_through
            canvas.fill(x0, label_y + row, x1, label_y + row + 2, color)
    return canvas.image()


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
