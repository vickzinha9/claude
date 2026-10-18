"""Synthetic chart captures for the tests, drawn pixel by pixel with the standard library.

They imitate the shapes that quote_reader looks for, not any real platform: label boxes
with strokes of contrasting "text", plain scale text, solid candle bodies, grid lines and
a dashed price line touching the marker. FakeEngine stands in for the OCR: it cannot read
the strokes, so each test says what it "reads".
"""

import math

from pocketautonomo.quote_reader import MarkerModel, RgbImage

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


class CandleChart:
    """A synthetic capture with known candles, and what candles.read_candles should find in it.

    ARTIFICIAL DATA: the candles are the ones given, drawn pixel by pixel; they say nothing about
    any market. Prices map to rows on a straight line, high_price at row top and low_price at row
    bottom, scaled by `scale` device pixels per CSS pixel. The axis labels are strokes that the
    FakeEngine cannot read, so `answers` holds what it must "read", in the order that read_candles
    asks: the marker's line twice, then each label from the top down, twice.
    """

    def __init__(
        self, bars, low_price, high_price, *, width=640, height=360, axis_width=72, spacing=12, body=8,
        label_step=None, decimals=5, scale=1, marker_price=None, skip=(), lines=(), blobs=(), answers=None,
        background=DARK_BACKGROUND, up=CANDLE_GREEN, down=CANDLE_RED, top=20, bottom=340,
        marker_height=18, marker_top=None, countdown=None, label_shift=0, grid=None, price_line=True,
        under_axis=(),
    ):
        """marker_top: CSS pixels from the price's row to the top of the marker's box (centered by default).
        countdown: a second line of text in the marker, under the price, that the OCR "reads" as given.
        label_shift: CSS pixels from each label's price to the middle of its digits, down.
        grid: the color of a grid line drawn across the chart at each label's price, under the candles.
        price_line: whether the dashed price line is drawn. under_axis: (top, bottom, color) of shapes in
        the columns of the axis, drawn before the labels, as a candle under them.
        """
        s = scale
        self.scale, self.bars, self.decimals = scale, list(bars), decimals
        self.width, self.height = width * s, height * s
        self.top, self.bottom = top * s, bottom * s
        self.low_price, self.high_price = low_price, high_price
        canvas = Canvas(self.width, self.height, background=background)
        axis = self.width - axis_width * s
        self.axis = axis
        count = len(self.bars)
        # A spacing such as 12.25 puts the candles a whole pixel apart in turns, as a display scale of 125% does.
        left = axis - 18 * s - round((count - 1) * spacing * s) - body * s
        self.boxes = []
        unit = 10 ** -decimals
        units = max(1, round((label_step if label_step is not None else (high_price - low_price) / 6) / unit))
        ticks = []
        k = math.ceil(low_price / unit / units)
        while k * units * unit <= high_price:
            price = k * units * unit
            row = round(self.row(price))
            if self.top <= row <= self.bottom:
                ticks.append((row, f"{price:.{decimals}f}"))
            k += 1
        if grid is not None:
            for row, _ in ticks:
                canvas.fill(0, row, axis, row + s, grid)
        for index, (open_, high, low, close) in enumerate(self.bars):
            if index in skip:
                continue
            color = up if close >= open_ else down
            x0 = left + round(index * spacing * s)
            body_top = round(self.row(max(open_, close)))
            body_bottom = max(body_top + 1, round(self.row(min(open_, close))))
            canvas.fill(x0, body_top, x0 + body * s, body_bottom, color)
            wick = x0 + (body * s) // 2
            canvas.fill(wick, round(self.row(high)), wick + s, round(self.row(low)), color)
        for row, x0, x1, color in lines:
            canvas.fill(x0 * s, row * s, x1 * s, row * s + s, color)
        for x0, y0, x1, y1, color in blobs:
            canvas.fill(x0 * s, y0 * s, x1 * s, y1 * s, color)
        for y0, y1, color in under_axis:
            canvas.fill(axis + 2 * s, y0 * s, axis + 60 * s, y1 * s, color)
        # The labels sit at round prices, as on an axis: multiples of a step of whole units of the last decimal.
        self.labels = sorted(ticks)
        self.marker_price = marker_price if marker_price is not None else self.bars[-1][3]
        self.marker_text = f"{self.marker_price:.{decimals}f}"
        marker_row = self.price_row = round(self.row(self.marker_price))
        height_px = marker_height * s
        marker_top = marker_row + (marker_top * s if marker_top is not None else -(height_px // 2))
        self.marker_box_rows = (marker_top, marker_top + height_px)
        if price_line:
            canvas.dashed_line(marker_row, 0, axis + s, MARKER)
        canvas.fill(axis + s, marker_top, axis + 69 * s, marker_top + height_px, MARKER)
        if countdown is None:
            text_top = marker_top + (height_px - 10 * s) // 2
            for n in range(len(self.marker_text)):
                x = axis + 6 * s + n * 8 * s
                canvas.fill(x, text_top, x + 2 * s, text_top + 10 * s, TEXT)
        else:  # the price above, the countdown below, each 6 pixels high
            for n in range(len(self.marker_text)):
                x = axis + 6 * s + n * 8 * s
                canvas.fill(x, marker_top + 3 * s, x + 2 * s, marker_top + 9 * s, TEXT)
            for n in range(len(countdown)):
                x = axis + 6 * s + n * 8 * s
                canvas.fill(x, marker_top + height_px - 9 * s, x + 2 * s, marker_top + height_px - 3 * s, TEXT)
        skipped = (marker_top - 2 * s, marker_top + height_px + 2 * s)
        self.read_labels = []
        for row, text in self.labels:
            y0 = row - 5 * s + label_shift * s
            for n in range(len(text)):
                x = axis + 8 * s + n * 7 * s
                canvas.fill(x, y0, x + 2 * s, y0 + 10 * s, SCALE_TEXT)
            if (y0 + 10 * s <= skipped[0] or y0 >= skipped[1]) and 0 < y0 and y0 + 10 * s < self.height:
                self.read_labels.append((row, text))
        self.read_labels = self.read_labels[:8]
        self.image = canvas.image()
        self.marker = MarkerModel((MARKER,), 68.0, float(marker_height), float(axis_width - 69), 20)
        if answers is None:
            answers = [self.marker_text] * 2 + ([countdown] * 2 if countdown is not None else [])
            answers += [text for _, text in self.read_labels for _ in range(2)]
        self.answers = [(text, 0.99) if isinstance(text, str) else text for text in answers]

    def row(self, price):
        return self.top + (self.high_price - price) / (self.high_price - self.low_price) * (self.bottom - self.top)

    @property
    def resolution(self):
        return (self.high_price - self.low_price) / (self.bottom - self.top)

    def engine(self):
        return FakeEngine(answers=list(self.answers))


def artificial_bars(count=40, start=1.08500, step=0.00004, drift=0.0, seed=7):
    """ARTIFICIAL candles (open, high, low, close), oldest first: a deterministic pseudo-random walk."""
    state = seed
    bars, price = [], start
    for _ in range(count):
        state = (state * 1103515245 + 12345) % 2**31
        move = ((state % 2001) / 1000 - 1) * step + drift
        state = (state * 1103515245 + 12345) % 2**31
        upper = (state % 1000) / 1000 * step
        state = (state * 1103515245 + 12345) % 2**31
        lower = (state % 1000) / 1000 * step
        open_, close = round(price, 5), round(price + move, 5)
        bars.append((open_, round(max(open_, close) + upper, 5), round(min(open_, close) - lower, 5), close))
        price = close
    return bars


def right_strip(image, width):
    """The rightmost width columns of image, as a capture of the chart's right strip gives them."""
    width = min(image.width, int(width))
    start = image.width - width
    rows = [image.data[(y * image.width + start) * 3:(y + 1) * image.width * 3] for y in range(image.height)]
    return RgbImage(width, image.height, b"".join(rows))


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
