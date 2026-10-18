"""The candles of the chart, read from a capture: open, high, low and close of each, by its pixels.

NO ORDERS. Like quote_reader, this module is pure Python, without third-party packages: the
capture of the chart element comes from observe (page.screenshot with a clip, in memory) and
the OCR engine is passed in. Nothing here acts on the page.

Why from the pixels: the chart of the Pocket Option is drawn in canvas (canvas.layer.plot), so
the page has no text or element with the values of the candles. Their data reaches the page
through its WebSocket, and reading it there would mean listening to the network or running
JavaScript of our own in the page, which this program never does (INTEGRATION, sections 3 and
4). What the screen shows is what can be read: the candles, the price scale and the marker of
the current quote.

Reading a capture (read_candles):

1. The marker of the current quote is found and read as in quote_reader (locate_marker and
   read_marker_text): its price. Where in the box that price lies is not assumed: a label may
   hold a second line, such as a countdown, or hang beside the price instead of being centered on
   it. On the Pocket Option, the center of the box was 11 to 13 pixels from where the axis put the
   marker's price, in every attempt and at every zoom (STATUS, item 38).
2. The labels of the axis (read_axis): the plain text in the columns of the marker. The pixels
   that differ from the background are text unless they are of a candle color (a candle under the
   axis), part of a vertical run taller than a line of text (a vertical line of the chart) or of a
   horizontal run longer than MAX_TEXT_RUN (a line drawn across the axis). The touching pixels
   make glyphs, and the glyphs side by side at the same height make lines of text. Each line of the
   size and shape of a price is read twice by the OCR, in gray and in black and white, with the
   checks of the marker (QUOTE_PATTERN, agreed_quote, MIN_SCORE); the black-and-white reading is
   made on the line enlarged first, so that the decimal point of a small font survives. The row
   of a label's price is the middle of its digits or, when every label has a thin line of the grid
   beside it, at the same distance from all of them, that line (grid_ticks). A straight line is
   fitted through the (row, price) of the labels: at least MIN_LABELS of them, each within
   SCALE_TOLERANCE of the line (one label far from it is left out when four or more remain), with
   the price going down the rows. Its slope is the price of a pixel.
3. The candles. Every pixel left of the axis is an up candle's, a down candle's or neither, by the
   calibrated colors (CandleModel) and their blends with the background, so that the
   anti-aliased edges and wicks count. Runs of one color in a row longer than MAX_CANDLE_WIDTH are
   lines (the price line, a level), not candles, and are left out. The runs that touch across rows
   make one candle: its body is where most of its columns are, its wick its tallest column. The
   candles are evenly spaced: a gap of k spacings means k - 1 candles that were not read, and an
   irregular gap or a shape wider than MAX_WIDTH_RATIO times the others makes the reading
   unavailable, because something there is not a candle. A candle cut by an edge of the capture
   is not read.
4. The row of the marker's quote, from what the chart draws at that price: the close of the
   rightmost candle, the one still forming, and the price line, a thin line across the chart
   with the marker's color at the marker's height, when there is one. The two must agree within
   MARKER_TOLERANCE, and that row must be within the marker's box: otherwise the chart may be
   scrolled back, zoomed or still loading, and nothing is read. Pieces of a candle color one
   spacing right of the last candle, at that row, are a candle just opened and hidden by the price
   line: the one before it would pass for the forming one, and nothing is read either. The candles
   are numbered by counting the spacings from each to the forming one, gap by gap.
5. The check of the scale. The line of the labels must put the marker's quote at that row,
   within MARKER_TOLERANCE: the labels and the marker, two readings of the OCR that do not depend
   on each other, and the drawing of the chart must agree, or nothing is read. Then the scale
   is the slope of the labels through the marker's quote at its row.
6. Prices. Each edge of a body or a wick gives a price on the scale. The resolution, the price
   of one device pixel, says how precise every value is: nothing smaller than it can be seen.

Every reading carries its Geometry: where each of these was found, in the pixels of the capture,
for the diagnosis that the calibration shows (geometry_lines and diagnostic_png).

Times. The time axis of the chart is not read. The period comes from the calibration, and the
times from this computer's clock (stamp): the forming candle started at the last multiple of the
period, the candle before it one period earlier, and so on. Near a change of candle, within
BOUNDARY_GUARD_SECONDS, the clock cannot tell which candle is forming, so the candles of such a
reading get no times. This needs the clock of this computer to be right within a few seconds;
the same candle read again with other values reveals a wrong clock, a wrong period or another
asset's chart (chart_analysis.CandleHistory).
"""

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import groupby

from pocketautonomo import quote_reader
from pocketautonomo.quote_reader import (
    MIN_SCORE,
    QUOTE_PATTERN,
    Box,
    GrayImage,
    agreed_quote,
    background_color,
    black_and_white,
    color_hex,
    locate_marker,
    parse_color_hex,
    read_marker_text,
)

UP, DOWN = "alta", "baixa"  # the two colors of candles
MIN_LABELS = 2  # labels of the axis read, at least; the marker is checked against their line
MAX_AXIS_LABELS = 8  # labels read, at most, from the top down: each costs two readings of the OCR
MAX_AXIS_READINGS = 16  # lines of the axis given to the OCR, at most, labels or not
SCALE_TOLERANCE = 1.5  # CSS pixels: how far a label may be from the fitted line
# CSS pixels: the quote's row by the labels against the chart's, and the forming candle's close against
# the price line and the marker's box.
MARKER_TOLERANCE = 2.5
AXIS_CONTRAST = 48  # smallest difference per channel between the axis text and the background
TEXT_HEIGHT = (5.0, 24.0)  # CSS pixels: the height of a line of the axis
LABEL_REACH = 24.0  # CSS pixels: the labels of the axis are looked for from this far left of the marker
# A pixel with this much of a candle color over the background, or more, is a candle's and not text, when it
# lies that close to the blend: its distance to the blend is at most OFF_AXIS times its share. Near the
# background every color is a few levels from every blend, and the faint edges of gray digits are no candle.
EDGE_ALPHA = 0.15
LINE_GAP = 8.0  # CSS pixels: glyphs this close side by side, at the same height, are one line of text
MAX_TEXT_RUN = 16.0  # CSS pixels: a longer run of text pixels in a row is a line drawn across the axis
MIN_TEXT_RATIO = 1.5  # a line of text narrower than this many times its height is not a price
BW_HEIGHT = 32  # device pixels: a lower line of the axis is enlarged before its black-and-white reading
LINE_WIDTH = 2.0  # CSS pixels: the thickest line taken for a thin line of the chart (price line, grid)
LINE_CONTRAST = 24  # per channel: a pixel of the price line differs this much from the rows around it
GRID_CONTRAST = 10  # the same for a line of the grid, which may be faint
LINE_SHARE = 0.3  # share of the columns left of the axis that such a line covers, at least
GRID_REACH = 1.5  # heights of a label's digits: how far from them its grid line may be
GRID_AGREEMENT = 1.0  # CSS pixels: how much the labels' distances to their grid lines may differ
MAX_CANDLE_WIDTH = 40.0  # CSS pixels: a longer run of one color in a row is a line
MIN_CANDLE_WIDTH = 2.0  # CSS pixels: a narrower shape is not a candle
MAX_WIDTH_RATIO = 1.7  # a shape this much wider than the median candle is not one candle
SPACING_TOLERANCE = 0.3  # share of the spacing that a gap between two candles may be off by
MIN_CANDLES = 6  # candles in a reading, at least, with the forming one
OFF_AXIS = 36  # a pixel farther than this from every blend of a candle color and the background is not it
MIN_ALPHA = 0.45  # a pixel with less of a candle color than this over the background is not it
ROW_GAP = 2  # rows of another color that may cross a candle without splitting it (the price line)
MIN_COLOR_PIXELS = 60  # sampled pixels of a color, at least, for the calibration to take it
BOUNDARY_GUARD_SECONDS = 3.0
MIN_PERIOD, MAX_PERIOD = 5, 3600  # seconds of a candle


@dataclass(frozen=True)
class CandleModel:
    """The calibration of the candles: the color of the up and of the down candles, and the period."""

    up: tuple  # (r, g, b)
    down: tuple
    period: int  # seconds of each candle, as the chart shows them

    def to_json(self):
        return {"up": color_hex(self.up), "down": color_hex(self.down), "period": self.period}

    @classmethod
    def from_json(cls, data):
        """The model in data; ValueError tells what is wrong."""
        if not isinstance(data, dict):
            raise ValueError("a calibração das velas precisa ser um objeto JSON")
        unknown = sorted(key for key in data if key not in ("up", "down", "period") and not key.startswith("_"))
        if unknown:
            raise ValueError(f"chave desconhecida: {', '.join(unknown)}")
        up, down = parse_color_hex(data.get("up")), parse_color_hex(data.get("down"))
        period = data.get("period")
        if isinstance(period, bool) or not isinstance(period, int) or not MIN_PERIOD <= period <= MAX_PERIOD:
            raise ValueError(f"period precisa ser um número inteiro de segundos de {MIN_PERIOD} a {MAX_PERIOD}")
        if max(abs(a - b) for a, b in zip(up, down)) < 2 * OFF_AXIS:
            raise ValueError("as cores de alta e de baixa estão próximas demais para separar as velas")
        return cls(up, down, period)


@dataclass(frozen=True)
class ScaleLine:
    """The price scale: price = intercept + slope * row, in device pixels from the top edge."""

    intercept: float
    slope: float  # price per device pixel, negative: the price goes down the rows
    labels: tuple  # (row, text) of the labels on the line
    dropped: tuple = ()  # (row, text) of a label left out, far from the line
    residual: float = 0.0  # the largest distance of a label from the line, in device pixels

    def price(self, row):
        return self.intercept + self.slope * row

    def row(self, price):
        return (price - self.intercept) / self.slope


@dataclass(frozen=True)
class Candle:
    """One candle: its place in the capture, in device pixels, and its prices."""

    kind: str  # UP or DOWN
    x0: int
    x1: int  # exclusive
    top: int  # the first row of the wick, or of the body without a wick above it
    body_top: int
    body_bottom: int  # inclusive
    bottom: int  # inclusive
    open: float
    high: float
    low: float
    close: float

    @property
    def center(self):
        return (self.x0 + self.x1) / 2

    def box(self):
        return Box(self.x0, self.top, self.x1, self.bottom + 1, (0, 0, 0))


@dataclass(frozen=True)
class CandleReading:
    """What a capture showed: the candles, oldest first, the last one still forming, or why nothing.

    indices[i] is how many candles before the forming one candles[i] is: 0 for the forming one.
    missing are the indices between them where no candle was read.
    """

    reason: object = None  # why the reading is unavailable, or None
    candles: tuple = ()
    indices: tuple = ()
    missing: tuple = ()
    scale: object = None  # ScaleLine
    marker_text: object = None  # the quote of the marker, as read
    marker_price: object = None
    marker_row: object = None  # the row of that quote in the chart, in device pixels: the price line or the close
    spacing: float = 0.0  # device pixels between two candles
    resolution: float = 0.0  # price of one device pixel
    decimals: int = 0  # of the marker's quote: how the prices are shown
    unread_labels: int = 0  # lines of the axis that the OCR could not read as a price
    geometry: object = None  # Geometry: where each part was found, for the diagnosis
    # The countdown of the marker, a line such as 00:42 under the quote: its seconds and its text, when both
    # readings of the OCR gave the same text with MIN_SCORE or more. Only shown and compared with this
    # computer's clock (countdown_check): nothing is decided by it.
    countdown: object = None
    countdown_text: object = None
    marker_others: tuple = ()  # (text, text): the two readings of each line of the marker that is not the quote

    @property
    def available(self):
        return self.reason is None

    @property
    def forming(self):
        return self.candles[-1] if self.candles else None

    @property
    def completed(self):
        """(index, Candle) of the completed candles, oldest first."""
        return tuple(zip(self.indices[:-1], self.candles[:-1]))

    def text(self, value):
        return f"{value:.{self.decimals}f}"


@dataclass(frozen=True)
class AxisLine:
    """A line of text in the columns of the axis, in device pixels, and what became of it."""

    top: int  # the first row of its digits
    bottom: int  # one past their last row
    left: int
    right: int  # exclusive
    text: object = None  # the quote that both readings of the OCR agreed on
    price: object = None  # its value, when the line is a label of the scale
    problem: object = None  # why it is not a label, or None
    read: bool = False  # whether the OCR read it
    tick: object = None  # the row of the grid line beside it, when the grid marks the labels
    note: object = None  # how its two readings agreed, when they were not the same text

    @property
    def center(self):
        return (self.top + self.bottom) / 2

    @property
    def row(self):
        """The row of its price: its grid line, or else the middle of its digits."""
        return self.center if self.tick is None else self.tick


@dataclass(frozen=True)
class Geometry:
    """Where read_candles found each part of the chart, in the pixels of the capture, row 0 at the top.

    For the diagnosis of a reading (geometry_lines, diagnostic_png); nothing here changes a price.
    """

    scale: float = 1.0
    width: int = 0
    height: int = 0
    marker_box: object = None  # Box
    marker_rows: object = None  # (first, last + 1) of the line of the quote inside the marker
    axis_x0: object = None  # the first column of the axis: the candles are read left of it
    label_x0: object = None  # the first column where the labels are looked for
    axis_lines: tuple = ()  # AxisLine, top down
    grid_offset: object = None  # rows from the middle of the labels' digits to their grid lines, when used
    label_line: object = None  # ScaleLine through the labels alone, before the check
    shapes: int = 0  # shapes of the candle colors taken for candles
    forming: object = None  # the Shape of the forming candle
    close_row: object = None  # the row of its close
    marker_lines: object = None  # ThinLine of each thin line across the chart at the marker's height, once looked for
    price_line: object = None  # the row of the price marked by the only one of them of the marker's color
    price_row: object = None  # the row of the marker's quote: the price line, else the close
    predicted_row: object = None  # where label_line puts the marker's quote

    @property
    def deviation(self):
        """Rows from the quote's row in the chart to where the labels put it, or None."""
        if self.predicted_row is None or self.price_row is None:
            return None
        return self.predicted_row - self.price_row


def unavailable(reason, **values):
    return CandleReading(reason=reason, **values)


def number_of(text):
    """The number of a price such as 1.42582, 1,42582 or 65,432.10: the last separator is the decimal one."""
    separators = [index for index, char in enumerate(text) if char in ".,"]
    if not separators:
        return None
    last = separators[-1]
    digits = "".join(char for char in text[:last] if char.isdigit()) + "." + text[last + 1:]
    try:
        return float(digits)
    except ValueError:
        return None


def decimals_of(text):
    separators = [index for index, char in enumerate(text) if char in ".,"]
    return len(text) - separators[-1] - 1 if separators else 0


# The colors.


def color_class(color, families, background):
    """UP, DOWN or None: which candle color color is, alone or blended with the background."""
    found = set()
    for kind, base in families:
        axis = (base[0] - background[0], base[1] - background[1], base[2] - background[2])
        norm = axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2]
        if not norm:
            continue
        t = (
            (color[0] - background[0]) * axis[0]
            + (color[1] - background[1]) * axis[1]
            + (color[2] - background[2]) * axis[2]
        ) / norm
        if t < MIN_ALPHA:
            continue
        t = min(t, 1.0)
        off = max(abs(color[i] - (background[i] + t * axis[i])) for i in range(3))
        if off <= OFF_AXIS:
            found.add(kind)
    return found.pop() if len(found) == 1 else None


def classifier(model, background):
    families = ((UP, model.up), (DOWN, model.down))
    return quote_reader._Classifier(lambda color: color_class(color, families, background))


def detect_colors(image, x_end):
    """((up, down), None) with the most frequent green and red of the chart, or (None, reason).

    Only for the calibration, which shows what it found: a candle body is filled with one color, so
    its exact color is the most frequent of its family. Green means more green than red, and at
    least as much green as blue; red, more red than green and than blue.
    """
    counts = {}
    for y in range(0, image.height, 2):
        for color in list(image.row(y, 0, x_end))[::2]:
            counts[color] = counts.get(color, 0) + 1

    def saturated(color):
        return max(color) - min(color) >= 60 and max(color) >= 90

    greens = [(n, c) for c, n in counts.items() if saturated(c) and c[1] >= c[0] + 30 and c[1] >= c[2]]
    reds = [(n, c) for c, n in counts.items() if saturated(c) and c[0] >= c[1] + 40 and c[0] >= c[2] + 40]
    up = max(greens, default=None)
    down = max(reds, default=None)
    if up is None or up[0] < MIN_COLOR_PIXELS:
        return None, "nenhuma cor verde de vela foi encontrada no gráfico"
    if down is None or down[0] < MIN_COLOR_PIXELS:
        return None, "nenhuma cor vermelha de vela foi encontrada no gráfico"
    return (up[1], down[1]), None


# The price scale.

_TEXT, _REMOVED = 1, 2  # the text pixels of the axis, and those of a line of the chart across it


def _glyphs(runs):
    """The runs (y, x0, x1) that touch, side by side or at a corner: [top, bottom, left, right, runs]."""
    parent = list(range(len(runs)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_row = {}
    for index, (y, xa, xb) in enumerate(runs):
        for other in by_row.get(y - 1, ()):
            _, oa, ob = runs[other]
            if oa <= xb and xa <= ob:
                a, b = root(index), root(other)
                if a != b:
                    parent[a] = b
        by_row.setdefault(y, []).append(index)
    groups = {}
    for index in range(len(runs)):
        groups.setdefault(root(index), []).append(runs[index])
    return [
        [min(y for y, _, _ in group), max(y for y, _, _ in group) + 1, min(a for _, a, _ in group),
         max(b for _, _, b in group), group]
        for group in groups.values()
    ]


def _text_lines_of(glyphs, gap, bridge=0):
    """The glyphs that share rows and lie within gap of each other side by side, grouped, top down.

    Glyphs up to bridge rows apart count as sharing rows: a thin line of the chart across a label,
    taken out, leaves the upper and the lower parts of its digits.
    """
    glyphs = sorted(glyphs, key=lambda glyph: glyph[2])
    parent = list(range(len(glyphs)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (top, bottom, left, right, _) in enumerate(glyphs):
        for j in range(i):
            other = glyphs[j]
            if min(bottom, other[1]) + bridge > max(top, other[0]) and left - other[3] <= gap:
                a, b = root(i), root(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for index, glyph in enumerate(glyphs):
        groups.setdefault(root(index), []).append(glyph)
    return sorted(groups.values(), key=lambda group: min(glyph[0] for glyph in group))


def _enlarged(gray, height):
    """gray enlarged by a whole factor, by linear interpolation, to height rows or more."""
    factor = -(-height // gray.height)
    if factor <= 1:
        return gray

    def taps(size):
        found = []
        for out in range(size * factor):
            source = (out + 0.5) / factor - 0.5
            low = min(size - 1, max(0, math.floor(source)))
            found.append((low, min(size - 1, low + 1), min(1.0, max(0.0, source - low))))
        return found

    width, data = gray.width, gray.data
    columns = taps(gray.width)
    out = bytearray()
    for y0, y1, fy in taps(gray.height):
        a_row, b_row = y0 * width, y1 * width
        for x0, x1, fx in columns:
            a = data[a_row + x0] + (data[a_row + x1] - data[a_row + x0]) * fx
            b = data[b_row + x0] + (data[b_row + x1] - data[b_row + x0]) * fx
            out.append(round(a + (b - a) * fy))
    return GrayImage(gray.width * factor, gray.height * factor, bytes(out))


def read_axis(image, scale, x0, skip, engine, candle_colors=(), decimals=None):
    """AxisLine of each line of text in the columns from x0 to the right edge, top down.

    skip, (first, last + 1), are the rows of the marker, left out. candle_colors, the colors of the
    candles, tell the pixels of a candle under the axis, its faint edges too: they are not text. The
    lines of the size and shape of a price are read by the OCR from the top down, each twice, as the
    marker's text is, until MAX_AXIS_LABELS of them are labels. decimals, those of the marker's quote,
    confirm a decimal point that only one of the two readings of a label saw (lost_separator).
    """
    width, height = image.width - x0, image.height
    if width <= 0:
        return []
    background = background_color(image, x0, image.width)

    def measure(color):
        for base in candle_colors:
            share, off = blend_share(color, base, background)
            if share >= EDGE_ALPHA and off <= OFF_AXIS * min(1.0, share):
                return -1  # a candle's pixel, never text
        return max(abs(color[0] - background[0]), abs(color[1] - background[1]), abs(color[2] - background[2]))

    distance_of = quote_reader._Classifier(measure)
    first, end = skip if skip is not None else (0, 0)
    distances = [None] * height  # of each pixel to the background, row by row; None in the marker's rows
    mask = bytearray(width * height)
    for y in range(height):
        if first <= y < end:
            continue
        row = distances[y] = [distance_of[color] for color in image.row(y, x0, image.width)]
        base = y * width
        for x, distance in enumerate(row):
            if distance >= AXIS_CONTRAST:
                mask[base + x] = _TEXT
    # A vertical line of the chart in these columns, such as the line of the expiration time, is a run
    # of text pixels down a column taller than a line of text.
    tallest = TEXT_HEIGHT[1] * scale
    for x in range(width):
        y = 0
        while y < height:
            if mask[y * width + x] != _TEXT:
                y += 1
                continue
            start = y
            while y < height and mask[y * width + x] == _TEXT:
                y += 1
            if y - start > tallest:
                for row in range(start, y):
                    mask[row * width + x] = _REMOVED
    # A line of the chart drawn across these columns comes in from their left edge or reaches the right one,
    # or crosses most of them; a run of text, even of digits touching each other, does neither.
    longest = MAX_TEXT_RUN * scale
    runs = []
    for y in range(height):
        base, x = y * width, 0
        while x < width:
            if mask[base + x] != _TEXT:
                x += 1
                continue
            start = x
            while x < width and mask[base + x] == _TEXT:
                x += 1
            if x - start > longest and (start == 0 or x == width or x - start > 0.75 * width):
                mask[base + start:base + x] = bytes([_REMOVED]) * (x - start)
            else:
                runs.append((y, start, x))
    groups = _text_lines_of(_glyphs(runs), LINE_GAP * scale, round(LINE_WIDTH * scale) + 1)
    owner = {}
    for number, group in enumerate(groups):
        for glyph in group:
            for y, xa, xb in glyph[4]:
                for x in range(xa, xb):
                    owner[y * width + x] = number
    low, high = TEXT_HEIGHT[0] * scale, TEXT_HEIGHT[1] * scale
    lines, labels, readings = [], 0, 0
    for number, group in enumerate(groups):
        # The middle of the digits, without the decimal point or a stray dot beside them.
        tallest_glyph = max(glyph[1] - glyph[0] for glyph in group)
        digits = [glyph for glyph in group if glyph[1] - glyph[0] >= 0.5 * tallest_glyph]
        top, bottom = min(glyph[0] for glyph in digits), max(glyph[1] for glyph in digits)
        left, right = min(glyph[2] for glyph in group), max(glyph[3] for glyph in group)
        upper, lower = min(glyph[0] for glyph in group), max(glyph[1] for glyph in group)
        place = (top, bottom, x0 + left, x0 + right)
        if upper == 0 or lower == height:
            problem = "cortada pela borda de cima ou de baixo da captura"
        elif skip is not None and (lower == first or upper == end):
            problem = "cortada pelas linhas do marcador"
        elif left == 0:
            problem = "cortada pela borda esquerda das colunas do eixo"
        elif right == width:
            problem = "cortada pela borda direita da captura"
        elif not low <= bottom - top <= high:
            problem = f"{bottom - top} pixels de altura, fora da altura de uma linha de texto ({low:.0f} a {high:.0f})"
        elif right - left < MIN_TEXT_RATIO * (bottom - top):
            problem = "estreita demais para um preço"
        elif labels >= MAX_AXIS_LABELS:
            problem = f"não lida: {MAX_AXIS_LABELS} rótulos já tinham sido lidos acima dela"
        elif readings >= MAX_AXIS_READINGS:
            problem = f"não lida: {MAX_AXIS_READINGS} linhas já tinham ido para o OCR acima dela"
        else:
            line = _axis_line_image(
                group, number, owner, mask, distances, width, height, (first, end), round(LINE_WIDTH * scale) + 1,
            )
            reading = _read_label(line, engine, decimals)
            lines.append(AxisLine(*place, **reading, read=True))
            labels += reading.get("price") is not None
            readings += 1
            continue
        lines.append(AxisLine(*place, problem=problem))
    return lines


def _axis_line_image(group, number, owner, mask, distances, width, height, skip, reach):
    """The line of text of group as gray, dark where it differs most from the background, padded.

    Only its own glyphs and the faint edges around them are kept: a pixel of another line, of a candle
    or of the marker's rows is white. A pixel of a line of the chart across the text, taken out of it,
    is as light as the lighter of the nearest pixels on its two sides, within reach, across the line:
    a stroke of a digit that the line crosses goes on through it, and the background stays white.
    """
    upper, lower = min(glyph[0] for glyph in group), max(glyph[1] for glyph in group)
    left, right = min(glyph[2] for glyph in group), max(glyph[3] for glyph in group)
    full = max(
        distances[y][x] for glyph in group for y, xa, xb in glyph[4] for x in range(xa, xb)
    )

    def gray(y, x):
        """The gray of the pixel for this line, or None for a pixel taken out."""
        row = distances[y] if 0 <= y < height and 0 <= x < width else None
        if row is None or skip[0] <= y < skip[1] or row[x] < 0:
            return 255
        index = y * width + x
        if mask[index] == _REMOVED:
            return None
        if mask[index] == _TEXT and owner.get(index) != number:
            return 255
        return 255 - min(255, round(255 * row[x] / full))

    def across(y, x, dy, dx):
        for k in range(1, reach + 1):
            value = gray(y + k * dy, x + k * dx)
            if value is not None:
                return value
        return None

    x0, x1 = max(0, left - 2), min(width, right + 3)
    y0, y1 = max(0, upper - 2), min(height, lower + 2)
    data = bytearray()
    for y in range(y0, y1):
        for x in range(x0, x1):
            value = gray(y, x)
            if value is None:
                sides = [(across(y, x, -1, 0), across(y, x, 1, 0)), (across(y, x, 0, -1), across(y, x, 0, 1))]
                pairs = [max(a, b) for a, b in sides if a is not None and b is not None]
                value = pairs[0] if pairs else 255
            data.append(value)
    return GrayImage(x1 - x0, y1 - y0, bytes(data))


def lost_separator(text_a, text_b, decimals):
    """The quote of two readings that differ only in a decimal separator missing from one, or None.

    At the size of the axis's font, the decimal point is a pixel or two, fainter than the digits,
    and the black-and-white reading can lose it (on the Windows of the CI, '161.600' and '161600').
    The digits must be the same in both readings, and the one separator must leave decimals digits
    after it, the decimals of the marker's quote, which its own two readings agreed on: the place of
    the point is then confirmed apart from the reading that lost it.
    """
    for with_point, without in ((text_a, text_b), (text_b, text_a)):
        match = quote_reader.SINGLE_SEPARATOR.fullmatch(with_point)
        if match is not None and without.isdigit() and without == match.group(1) + match.group(2):
            if decimals is not None and len(match.group(2)) == decimals:
                return f"{match.group(1)}.{match.group(2)}"
    return None


def _read_label(gray, engine, decimals=None):
    """{text, price} of a line of the axis read twice by the OCR as a price, or {problem}."""
    lines = quote_reader._text_lines(gray)
    if len(lines) != 1:
        return {"problem": f"o recorte tem {len(lines)} linhas de texto, e não uma"}
    line = lines[0]
    # The black-and-white reading is made on the line enlarged: at the size of the axis's font, the
    # threshold alone can erase the decimal point or join two digits.
    (text_a, score_a), (text_b, score_b) = (
        engine.recognize(line), engine.recognize(black_and_white(_enlarged(line, BW_HEIGHT)))
    )
    text_a, text_b = "".join(text_a.split()), "".join(text_b.split())
    if not (QUOTE_PATTERN.fullmatch(text_a) or QUOTE_PATTERN.fullmatch(text_b)):
        return {"problem": f"o OCR leu {text_a or text_b or 'nada'!r}, que não é um preço"}
    quote, note = agreed_quote(text_a, text_b)
    if quote is None:
        quote = lost_separator(text_a, text_b, decimals)
        note = (
            f"o separador decimal só apareceu numa das leituras ({text_a!r} e {text_b!r}), com as casas do marcador"
            if quote is not None else None
        )
    if quote is None:
        return {"problem": f"as duas leituras do OCR divergiram: {text_a!r} e {text_b!r}"}
    value = number_of(quote)
    if value is None or not QUOTE_PATTERN.fullmatch(quote):
        return {"problem": f"o OCR leu {quote!r}, que não é um preço"}
    if min(score_a, score_b) < MIN_SCORE:
        return {"problem": f"o OCR leu {quote!r} com confiança {min(score_a, score_b):.2f}, abaixo de {MIN_SCORE:.2f}"}
    return {"text": quote, "price": value, "note": note}


@dataclass(frozen=True)
class ThinLine:
    """A thin horizontal line across the chart: its rows, its most frequent color, and that of each row."""

    top: int
    bottom: int  # exclusive
    color: tuple
    row_colors: tuple

    def row_of(self, weight):
        """The row of the price that the line marks, weight(color) telling how much of the line a color is.

        A line one pixel high drawn between two rows of pixels leaves part of its color in each: its
        center is the middle of the rows weighed by that share, and the price is at its upper edge,
        as at the edge of a candle's body (half a pixel off for a line drawn centered on the price).
        """
        weights = [max(0.0, weight(color)) for color in self.row_colors]
        total = sum(weights)
        if total <= 0:
            return float(self.top)
        return sum((self.top + n + 0.5) * w for n, w in enumerate(weights)) / total - 0.5


def blend_share(color, base, background):
    """(share, off): how much of base over background color is, and how far it is from that blend."""
    axis = (base[0] - background[0], base[1] - background[1], base[2] - background[2])
    norm = axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2]
    if not norm:
        return 0.0, 255.0
    share = sum((color[i] - background[i]) * axis[i] for i in range(3)) / norm
    return share, max(abs(color[i] - (background[i] + share * axis[i])) for i in range(3))


def thin_lines(image, scale, x0, x1, first, end, contrast, share=LINE_SHARE):
    """ThinLine of each thin horizontal line across the columns x0..x1, in the rows first..end.

    A pixel is on such a line when it differs by contrast or more from the pixels just above and
    below it, which do not differ that much from each other: a line over the background or over a
    candle's body, and not the edge of a body. A line is at most LINE_WIDTH thick and covers share
    of the columns, at least; its color is the most frequent of its pixels.
    """
    reach = round(LINE_WIDTH * scale) + 1
    width = x1 - x0
    if width <= 0:
        return []
    step = 2 if width >= 200 else 1  # half the columns measure the share as well
    data, stride = image.data, image.width * 3
    rows = []
    for y in range(max(reach, first), min(image.height - reach, end)):
        here = y * stride + 3 * x0
        above, below = here - reach * stride, here + reach * stride
        count, colors = 0, {}
        for offset in range(0, 3 * width, 3 * step):
            i, a, b = here + offset, above + offset, below + offset
            red, green, blue = data[i], data[i + 1], data[i + 2]
            if max(abs(red - data[a]), abs(green - data[a + 1]), abs(blue - data[a + 2])) < contrast:
                continue
            if max(abs(red - data[b]), abs(green - data[b + 1]), abs(blue - data[b + 2])) < contrast:
                continue
            if max(abs(data[a] - data[b]), abs(data[a + 1] - data[b + 1]), abs(data[a + 2] - data[b + 2])) >= contrast:
                continue
            count += 1
            colors[(red, green, blue)] = colors.get((red, green, blue), 0) + 1
        if count * step >= share * width:
            rows.append((y, colors))
    found = []
    for _, group in groupby(enumerate(rows), key=lambda item: item[1][0] - item[0]):
        block = [row for _, row in group]
        if len(block) <= reach:
            colors = {}
            for _, counts in block:
                for color, n in counts.items():
                    colors[color] = colors.get(color, 0) + n
            found.append(
                ThinLine(
                    block[0][0], block[-1][0] + 1, max(colors, key=colors.get),
                    tuple(max(counts, key=counts.get) for _, counts in block),
                )
            )
    return found


MIN_LINE_PAINT = 0.6  # the marker's paint that the price line holds, at least, added up over its rows


def _marker_share(color, model, background):
    """How much of one of the marker's colors over the background color is: 0 when it is no such blend."""
    best = 0.0
    for base in model.colors:
        share, off = blend_share(color, base, background)
        if off <= OFF_AXIS:
            best = max(best, min(share, 1.0))
    return best


def _price_line(lines, model, background):
    """The row of the price line among lines, the thin lines at the marker's height, or None.

    The price line is drawn with the marker's paint: added up over its rows, the share of a marker
    color over the background in each row is MIN_LINE_PAINT or more. A line one pixel high between
    two rows of pixels splits its paint between them, and a faint line of the grid, even a bluish
    one, holds a fraction of it. Only one such line can be taken; none, or two, leave the row to the
    close of the forming candle.
    """
    own = [
        line for line in lines
        if sum(_marker_share(color, model, background) for color in line.row_colors) >= MIN_LINE_PAINT
    ]
    if len(own) != 1:
        return None
    return own[0].row_of(lambda color: _marker_share(color, model, background))


def grid_ticks(image, scale, x_end, labels, skip=None, background=None):
    """(rows, offset): the row of the grid line beside each label, and their distance, or (None, None).

    A line of the grid marks its label's price exactly, wherever the label's digits are drawn around
    it. The lines are taken only when every label has a thin line across the chart beside it, within
    GRID_REACH heights of its digits and outside the marker's rows, all at the same distance from
    their labels within GRID_AGREEMENT; otherwise the middle of the digits stands for each price.
    background is the color of the chart left of x_end, measured when not given.
    """
    if len(labels) < MIN_LABELS:
        return None, None
    background = background if background is not None else background_color(image, 0, x_end)

    def contrast(color):
        return max(abs(color[0] - background[0]), abs(color[1] - background[1]), abs(color[2] - background[2]))

    ticks = []
    for label in labels:
        reach = GRID_REACH * (label.bottom - label.top)
        found = [
            line.row_of(contrast)
            for line in thin_lines(
                image, scale, 0, x_end, math.floor(label.center - reach), math.ceil(label.center + reach) + 1,
                GRID_CONTRAST,
            )
            if skip is None or not skip[0] <= line.top < skip[1]
        ]
        if not found:
            return None, None
        ticks.append(min(found, key=lambda row: abs(row - label.center)))
    offsets = sorted(tick - label.center for tick, label in zip(ticks, labels))
    middle = offsets[len(offsets) // 2]
    if max(abs(offset - middle) for offset in offsets) > GRID_AGREEMENT * scale + 0.5:
        return None, None
    return ticks, middle


def fit_scale(labels, scale):
    """(ScaleLine, None) through the labels, or (None, reason)."""
    if len(labels) < MIN_LABELS:
        return None, (
            f"a escala de preços não pôde ser lida: {len(labels)} rótulo(s) do eixo lido(s), de pelo menos {MIN_LABELS}"
        )
    tolerance = SCALE_TOLERANCE * scale + 0.5
    points, dropped = list(labels), []
    for _ in range(2):
        line = _line(points)
        if line is None:
            return None, "a escala de preços não pôde ser lida: os rótulos do eixo estão na mesma linha ou têm o mesmo preço"
        intercept, slope = line
        if slope >= 0:
            return None, "a escala de preços lida não diminui de cima para baixo: os rótulos lidos não são de um eixo de preços"
        residuals = [abs(row - (value - intercept) / slope) for row, _, value in points]
        worst = max(residuals)
        if worst <= tolerance:
            return ScaleLine(
                intercept, slope, tuple((row, text) for row, text, _ in points),
                tuple((row, text) for row, text, _ in dropped), worst,
            ), None
        if len(points) < 4 or dropped:
            break
        dropped.append(points.pop(residuals.index(worst)))
    shown = ", ".join(text for _, text, _ in labels)
    return None, (
        f"a escala de preços não pôde ser lida: os rótulos do eixo ({shown}) não ficam numa linha reta; o OCR pode ter"
        " lido algum errado"
    )


def _line(points):
    n = len(points)
    mean_row = sum(row for row, _, _ in points) / n
    mean_value = sum(value for _, _, value in points) / n
    srr = sum((row - mean_row) ** 2 for row, _, _ in points)
    if srr == 0:
        return None
    slope = sum((row - mean_row) * (value - mean_value) for row, _, value in points) / srr
    if slope == 0:
        return None
    return mean_value - slope * mean_row, slope


# The candles.


@dataclass(frozen=True)
class Shape:
    """The pixels of one candle color that touch: one candle, or something that is not one."""

    kind: str
    x0: int
    x1: int
    top: int
    body_top: int
    body_bottom: int
    bottom: int
    pixels: int

    @property
    def center(self):
        return (self.x0 + self.x1) / 2

    @property
    def width(self):
        return self.x1 - self.x0


BRIDGE = 2.0  # CSS pixels: pieces of one candle this close side by side are joined (the price line cuts them)


def shapes_of(image, scale, x_end, classify):
    """Every Shape of the candle colors in the columns before x_end, left to right.

    The dashed price line crosses the forming candle exactly at its close, and at the start of a
    candle its body is one or two rows tall: the dashes cut it into pieces side by side. Pieces of
    one color within BRIDGE of each other, overlapping in height, are joined while together they
    stay as narrow as one candle.
    """
    longest = MAX_CANDLE_WIDTH * scale
    runs = []
    for y in range(image.height):
        x = 0
        for key, group in groupby(map(classify.__getitem__, image.row(y, 0, x_end))):
            n = sum(1 for _ in group)
            if key is not None and n <= longest:
                runs.append((y, x, x + n, key))
            x += n
    pieces = []
    for component in quote_reader._components(runs, ROW_GAP):
        columns = {}
        for y, xa, xb, _ in component:
            for x in range(xa, xb):
                extent = columns.get(x)
                columns[x] = (y, y) if extent is None else (min(extent[0], y), max(extent[1], y))
        pieces.append([component[0][3], min(columns), max(columns) + 1, columns, sum(xb - xa for _, xa, xb, _ in component)])
    widths = sorted(piece[2] - piece[1] for piece in pieces if piece[2] - piece[1] > 2 * scale)
    reference = widths[len(widths) // 2] if widths else 0
    bridge = BRIDGE * scale
    pieces.sort(key=lambda piece: piece[1])
    joined = []
    for piece in pieces:
        kind, x0, x1, columns, pixels = piece
        top, bottom = min(t for t, _ in columns.values()), max(b for _, b in columns.values())
        for other in reversed(joined[-3:]):
            okind, ox0, ox1, ocolumns, _ = other
            otop, obottom = min(t for t, _ in ocolumns.values()), max(b for _, b in ocolumns.values())
            if (
                okind == kind
                and x0 - ox1 <= bridge
                and top <= obottom + ROW_GAP
                and otop <= bottom + ROW_GAP
                and max(ox1, x1) - min(ox0, x0) <= 1.25 * reference
            ):
                for x, (t, b) in columns.items():
                    extent = ocolumns.get(x)
                    ocolumns[x] = (t, b) if extent is None else (min(extent[0], t), max(extent[1], b))
                other[1], other[2], other[4] = min(ox0, x0), max(ox1, x1), other[4] + pixels
                break
        else:
            joined.append(piece)
    shapes = []
    for kind, x0, x1, columns, pixels in joined:
        tops = sorted(top for top, _ in columns.values())
        bottoms = sorted(bottom for _, bottom in columns.values())
        shapes.append(Shape(kind, x0, x1, tops[0], tops[len(tops) // 2], bottoms[(len(bottoms) - 1) // 2], bottoms[-1], pixels))
    return sorted(shapes, key=lambda shape: shape.center)


def _px(value):
    """A row or a column for the messages: whole, or with one decimal."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def read_candles(image, scale, model, marker_model, engine):
    """CandleReading of a capture of the whole chart element, in device pixels, scale per CSS pixel."""
    seen = {"scale": scale, "width": image.width, "height": image.height}

    def refuse(reason, **values):
        return unavailable(reason, geometry=Geometry(**seen), **values)

    box, reason = locate_marker(image, scale, marker_model)
    if box is None:
        return refuse(f"o marcador da cotação não foi achado: {reason}")
    seen["marker_box"] = box
    marker = read_marker_text(image, box, engine)
    seen["marker_rows"] = marker.rows
    if marker.value is None:
        return refuse(f"o marcador da cotação não foi lido: {marker.reason}")
    marker_price = number_of(marker.value)
    decimals = decimals_of(marker.value)
    countdown, countdown_text = countdown_of(marker.others)
    found = {
        "marker_text": marker.value, "marker_price": marker_price, "decimals": decimals, "countdown": countdown,
        "countdown_text": countdown_text, "marker_others": tuple((a, b) for a, b, _, _ in marker.others),
    }
    axis_x0 = max(0, box.x0 - round(2 * scale))
    seen["axis_x0"] = axis_x0
    background = background_color(image, 0, axis_x0)
    candle_kind = classifier(model, background)
    margin = round(2 * scale)
    skip = (box.y0 - margin, box.y1 + margin)
    # The labels may start left of the marker: on a chart drawn under its axis, the box of the marker is
    # narrower than the text of the labels or not aligned with it.
    label_x0 = max(0, box.x0 - round(LABEL_REACH * scale))
    seen["label_x0"] = label_x0
    lines = read_axis(image, scale, label_x0, skip, engine, (model.up, model.down), decimals)
    labels = [line for line in lines if line.price is not None]
    ticks, offset = grid_ticks(image, scale, axis_x0, labels, skip, background)
    if ticks is not None:
        tick_of = dict(zip(labels, ticks))
        lines = [replace(line, tick=tick_of[line]) if line in tick_of else line for line in lines]
        labels = [line for line in lines if line.price is not None]
        seen["grid_offset"] = offset
    seen["axis_lines"] = tuple(lines)
    found["unread_labels"] = sum(1 for line in lines if line.read and line.price is None)
    label_line, problem = fit_scale([(line.row, line.text, line.price) for line in labels], scale)
    if label_line is None:
        return refuse(problem, **found)
    seen["label_line"] = label_line
    seen["predicted_row"] = label_line.row(marker_price)
    found["scale"] = label_line
    found["resolution"] = abs(label_line.slope)
    shapes = [
        shape
        for shape in shapes_of(image, scale, axis_x0, candle_kind)
        if shape.width >= MIN_CANDLE_WIDTH * scale and shape.pixels >= 4
    ]
    if shapes and shapes[0].x0 == 0:
        shapes = shapes[1:]  # cut by the left edge of the capture
    seen["shapes"] = len(shapes)
    if len(shapes) < MIN_CANDLES:
        return refuse(
            f"{len(shapes)} vela(s) com as cores calibradas no gráfico, de pelo menos {MIN_CANDLES}: confira as cores"
            " com --configure e o zoom do gráfico",
            **found,
        )
    heights = sorted(shape.bottom - shape.top + 1 for shape in shapes)
    if heights[len(heights) // 2] < 2 * scale:
        return refuse(
            "as formas da cor das velas no gráfico são linhas de um pixel, não velas: confira as cores com --configure",
            **found,
        )
    widths = sorted(shape.width for shape in shapes)
    median_width = widths[len(widths) // 2]
    wide = [shape for shape in shapes if shape.width > MAX_WIDTH_RATIO * median_width]
    if wide:
        return refuse(
            f"{len(wide)} forma(s) da cor das velas mais larga(s) que as velas ({wide[0].width} pixels, contra"
            f" {median_width}): velas encostadas umas nas outras ou outro desenho no gráfico; afaste o zoom ou tire os"
            " indicadores",
            **found,
        )
    forming = shapes[-1]
    # The row of the marker's quote, from what the chart draws at that price: the forming candle closes
    # there, and the price line, when there is one, crosses the chart there.
    close_row = forming.body_top if forming.kind == UP else forming.body_bottom + 1
    seen["forming"], seen["close_row"] = forming, close_row
    centers = [shape.center for shape in shapes]
    if forming.x1 >= axis_x0:
        # Its right part is under the axis, behind the marker: its place comes from its left edge.
        centers[-1] = forming.x0 + median_width / 2
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    spacing = sorted(gaps)[len(gaps) // 2]
    if spacing < median_width:
        return refuse("as velas se sobrepõem no gráfico", **found)
    steps = []
    for gap in gaps:
        step = round(gap / spacing)
        if step < 1 or abs(gap - step * spacing) > SPACING_TOLERANCE * spacing:
            return refuse(
                "o espaçamento entre as velas é irregular: há no gráfico algo da cor das velas que não é uma vela", **found,
            )
        steps.append(step)
    # Each candle's place counts the steps from it to the forming one, gap by gap. At a scale such as 1.25,
    # the candles sit a whole pixel apart in turns (16 and 17 pixels for 16.25), and the median gap is off by
    # a fraction of a pixel: over forty candles, enough to give an old candle the index of its neighbor.
    spacing = (centers[-1] - centers[0]) / sum(steps)
    found["spacing"] = spacing
    places = [0] * len(shapes)
    for index in range(len(shapes) - 2, -1, -1):
        places[index] = places[index + 1] + steps[index]
    marker_lines = thin_lines(image, scale, 0, axis_x0, skip[0], skip[1], LINE_CONTRAST)
    seen["marker_lines"] = tuple(marker_lines)
    price_line = _price_line(marker_lines, marker_model, background)
    seen["price_line"] = price_line
    tolerance = MARKER_TOLERANCE * scale + 0.5
    scrolled = (
        ": o gráfico pode estar rolado para trás, com outro zoom ou ainda carregando; deixe a vela que se forma à vista,"
        " à direita"
    )
    if price_line is not None and abs(price_line - close_row) > tolerance:
        return refuse(
            f"a última vela não termina na cotação do marcador (o fechamento dela está na linha {_px(close_row)} da"
            f" captura, e a linha do preço, na linha {_px(price_line)}){scrolled}",
            **found,
        )
    price_row = price_line if price_line is not None else close_row
    source = "a linha do preço" if price_line is not None else "o fechamento da vela que se forma"
    by = "pela linha do preço" if price_line is not None else "pelo fechamento da vela que se forma"
    seen["price_row"] = price_row
    found["marker_row"] = price_row
    if not box.y0 - tolerance <= price_row <= box.y1 + tolerance:
        return refuse(
            f"a última vela não termina na cotação do marcador (a linha da cotação no gráfico, {_px(price_row)} da"
            f" captura {by}, fica fora das linhas do marcador, {box.y0} a {box.y1 - 1}){scrolled}",
            **found,
        )
    # A candle that has just opened is a row or two high, at the price: an opaque price line over it can
    # hide all of it but a few pixels, and the candle before it would pass for the forming one. Such
    # pixels would be one spacing right of the last candle, in pieces no wider than a candle: a line of a
    # candle color across that place is not one.
    near = round(LINE_WIDTH * scale) + 1
    expected = centers[-1] + spacing
    left = max(forming.x1 + 1, math.floor(expected - median_width))
    right = min(axis_x0, math.ceil(expected + median_width))
    rows = range(max(0, math.floor(price_row) - near), min(image.height, math.floor(price_row) + near + 1))
    smallest = max(2, round(MIN_CANDLE_WIDTH * scale))  # a thinner piece, such as a vertical line, is no candle
    hidden = 0
    if right - left >= median_width:
        for y in rows:
            for key, group in groupby(map(candle_kind.__getitem__, image.row(y, left, right))):
                n = sum(1 for _ in group)
                if key is not None and smallest <= n <= 1.25 * median_width:
                    hidden += n
    if hidden:
        # A line of a candle color along the quote's row, such as a price line colored by the direction,
        # crosses the gaps between the candles too, while a hidden candle is only in its own place.
        between = [(a.x1, b.x0) for a, b in zip(shapes, shapes[1:]) if b.x0 > a.x1]
        crossed = sum(
            1
            for x0, x1 in between
            if any(candle_kind[color] is not None for y in rows for color in image.row(y, x0, x1))
        )
        if len(between) >= MIN_CANDLES - 1 and crossed >= 0.5 * len(between):
            hidden = 0
    if hidden:
        return refuse(
            f"há {hidden} pixel(s) da cor das velas à direita da última vela, na linha do preço: a vela que se forma pode"
            " estar escondida pela linha do preço, e a anterior passaria por ela; leia de novo em alguns segundos",
            **found,
        )
    deviation = label_line.row(marker_price) - price_row
    if abs(deviation) > tolerance:
        return refuse(
            f"o marcador ({marker.value}) não confere com a escala lida no eixo: os rótulos põem esse preço na linha"
            f" {label_line.row(marker_price):.1f} da captura, a {abs(deviation):.1f} pixels da linha da cotação no"
            f" gráfico ({_px(price_row)}, {source}), e o limite é {tolerance:.1f}",
            **found,
        )
    # The slope comes from the labels, and the line goes through the quote's row in the chart: the
    # labels' digits may sit a pixel or so off their ticks.
    line = ScaleLine(
        marker_price - label_line.slope * price_row, label_line.slope, label_line.labels, label_line.dropped,
        label_line.residual,
    )
    found["scale"] = line
    candles, indices = [], []
    for shape, place in zip(shapes, places):
        if shape.top == 0 or shape.bottom >= image.height - 1:
            continue  # cut by the top or the bottom: its high or low is not on the screen
        upper, lower = line.price(shape.body_top), line.price(shape.body_bottom + 1)
        open_, close = (lower, upper) if shape.kind == UP else (upper, lower)
        candles.append(
            Candle(
                shape.kind, shape.x0, shape.x1, shape.top, shape.body_top, shape.body_bottom, shape.bottom,
                open_, line.price(shape.top), line.price(shape.bottom + 1), close,
            )
        )
        indices.append(place)
    if not indices or indices[-1] != 0:
        return refuse("a vela que está se formando foi cortada pela borda da captura", **found)
    present = set(indices)
    missing = tuple(index for index in range(1, max(indices) + 1) if index not in present)
    return CandleReading(
        None, tuple(candles), tuple(indices), missing, line, marker.value, marker_price, price_row, spacing,
        abs(line.slope), decimals, found["unread_labels"], Geometry(**seen), countdown, countdown_text,
        found["marker_others"],
    )


# The countdown of the marker.

COUNTDOWN_PATTERN = quote_reader.COUNTDOWN_PATTERN
COUNTDOWN_TOLERANCE = 2.0  # seconds: the countdown shows whole seconds, and the capture takes a moment


def countdown_of(others):
    """(seconds, text) of the only countdown among the other lines of the marker, or (None, None).

    others are (text, text, score, rows) of the two readings of each line (quote_reader.QuoteReading.others).
    A countdown is MM:SS or HH:MM:SS, read the same by both readings, with MIN_SCORE or more.
    """
    found = []
    for text_a, text_b, score, _ in others:
        match = COUNTDOWN_PATTERN.fullmatch(text_a)
        if match is None or text_a != text_b or score < MIN_SCORE:
            continue
        hours, minutes, seconds = (int(group) if group else 0 for group in match.groups())
        if seconds >= 60 or (match.group(1) is not None and minutes >= 60):
            continue
        found.append((hours * 3600 + minutes * 60 + seconds, text_a))
    return found[0] if len(found) == 1 else (None, None)


def countdown_check(reading, captured_at, period):
    """(remaining, problem): the seconds left in the forming candle by this computer's clock, and what disagrees.

    remaining is None without a countdown read. problem is None when the countdown of the marker, if it is the
    time left until the forming candle closes, agrees with this computer's clock and the period within
    COUNTDOWN_TOLERANCE; otherwise it says what does not. Nothing is refused for it: on the Pocket Option, what
    this countdown counts has not been confirmed.
    """
    if reading.countdown is None or captured_at is None:
        return None, None
    remaining = period - seconds_into(captured_at, period)
    if reading.countdown > period:
        return remaining, (
            f"o contador ({reading.countdown_text}) passa do período calibrado, {period} s: se ele conta o fim da vela,"
            " o gráfico está em velas de outro período"
        )
    difference = reading.countdown - remaining
    # Around a change of candle, the platform may already show the next candle's time, or still this one's.
    near = COUNTDOWN_TOLERANCE
    if (remaining <= near and reading.countdown >= period - near) or (remaining >= period - near and reading.countdown <= near):
        difference = min(difference - period, difference + period, key=abs)
    if abs(difference) > COUNTDOWN_TOLERANCE:
        more = "a mais" if difference > 0 else "a menos"
        return remaining, (
            f"o contador ({reading.countdown_text}) mostra {_seconds(abs(difference))} s {more} que o relógio deste"
            " computador para o fim da vela: o relógio, o período calibrado ou o que o contador conta não conferem"
        )
    return remaining, None


def _seconds(value):
    """Seconds with one decimal and a comma, as the other times of the calibration."""
    return f"{value:.1f}".replace(".", ",")


# The diagnosis.


def geometry_lines(reading, limit=12):
    """What read_candles found where, in the pixels of the capture: the lines of the diagnosis."""
    g = reading.geometry
    if g is None:
        return []
    lines = [
        f"  Diagnóstico, em pixels da captura ({g.width}x{g.height}, {g.scale:g} por pixel CSS; linha 0 no topo):"
    ]
    box = g.marker_box
    if box is not None:
        text = (
            f"    marcador: colunas {box.x0} a {box.x1 - 1}, linhas {box.y0} a {box.y1 - 1}, centro na linha"
            f" {_px((box.y0 + box.y1) / 2)}"
        )
        if g.marker_rows is not None:
            text += f"; a cotação está nas linhas {g.marker_rows[0]} a {g.marker_rows[1] - 1} dele"
        lines.append(text + ".")
    if g.label_x0 is not None:
        lines.append(
            f"    rótulos do eixo, procurados da coluna {g.label_x0} à borda direita: {len(g.axis_lines)} linha(s) de"
            f" texto; as velas, à esquerda da coluna {g.axis_x0}."
        )
        for axis_line in g.axis_lines[:limit]:
            place = f"      linhas {axis_line.top} a {axis_line.bottom - 1}, meio na linha {_px(axis_line.center)}"
            if axis_line.tick is not None:
                place += f", linha da grade {_px(axis_line.tick)}"
            if axis_line.price is not None:
                lines.append(f"{place}: rótulo {axis_line.text}" + (f" ({axis_line.note})" if axis_line.note else ""))
            else:
                lines.append(f"{place}: {axis_line.problem}")
        if len(g.axis_lines) > limit:
            lines.append(f"      e mais {len(g.axis_lines) - limit} linha(s), não mostradas.")
        if g.grid_offset is not None:
            lines.append(
                f"    linhas da grade ao lado de todos os rótulos, a {g.grid_offset:+.1f} pixels do meio dos algarismos: os"
                " preços dos rótulos ficam nelas."
            )
        elif any(axis_line.price is not None for axis_line in g.axis_lines):
            lines.append("    linhas da grade: não há uma ao lado de cada rótulo; vale o meio dos algarismos.")
    if g.label_line is not None:
        text = (
            f"    reta dos rótulos: 1 pixel = {abs(g.label_line.slope):.6g}; maior distância de um rótulo à reta:"
            f" {g.label_line.residual:.1f} pixels"
        )
        if g.label_line.dropped:
            text += f"; deixado de fora: {', '.join(label for _, label in g.label_line.dropped)}"
        lines.append(text + ".")
    if g.forming is not None:
        shape = g.forming
        closes = f", fecha na linha {_px(g.close_row)}" if g.close_row is not None else ""
        lines.append(
            f"    velas: {g.shapes} formas das cores calibradas; a que se forma, de {shape.kind}, nas colunas {shape.x0} a"
            f" {shape.x1 - 1} e linhas {shape.top} a {shape.bottom}{closes}."
        )
    if g.marker_lines is not None:
        if g.marker_lines:
            shown = ", ".join(
                (f"linha {line.top}" if line.bottom - line.top == 1 else f"linhas {line.top} a {line.bottom - 1}")
                + f" ({color_hex(line.color)})"
                for line in g.marker_lines
            )
            lines.append(f"    linhas finas na altura do marcador: {shown}.")
        else:
            lines.append("    linhas finas na altura do marcador: nenhuma.")
        if g.price_line is not None:
            lines.append(f"    linha do preço (a única da cor do marcador): preço na linha {g.price_line:.1f}.")
        else:
            lines.append("    linha do preço: nenhuma linha única da cor do marcador; vale o fechamento da vela.")
    if g.price_row is not None:
        lines.append(f"    linha da cotação no gráfico: {_px(g.price_row)}.")
        if box is not None:
            lines.append(
                f"    o centro da caixa do marcador está a {(box.y0 + box.y1) / 2 - g.price_row:+.1f} pixels dela; a"
                f" borda de cima, a {box.y0 - g.price_row:+.1f}; a de baixo, a {box.y1 - g.price_row:+.1f}."
            )
    if g.predicted_row is not None and g.price_row is not None:
        tolerance = MARKER_TOLERANCE * g.scale + 0.5
        lines.append(
            f"    a reta dos rótulos põe a cotação do marcador na linha {g.predicted_row:.1f}: {g.deviation:+.1f} pixels"
            f" da linha da cotação no gráfico (limite: {tolerance:.1f})."
        )
    elif g.predicted_row is not None:
        lines.append(f"    a reta dos rótulos põe a cotação do marcador na linha {g.predicted_row:.1f}.")
    return lines


RULER = 56  # device pixels: the width of the ruler right of the capture, in the diagnostic image
RULER_COLORS = {
    "marker": (255, 220, 0),  # the top and bottom rows of the marker's box
    "quote": (255, 140, 0),  # the rows of the quote's line inside the marker
    "text": (128, 128, 128),  # the top and bottom rows of each line of text of the axis
    "label": (0, 230, 0),  # the row of a label's price
    "unread": (230, 0, 0),  # the middle of a line of text that is not a label
    "close": (255, 255, 255),  # the close of the forming candle, and the quote's row in the chart
    "line": (0, 220, 255),  # the price line
    "predicted": (255, 0, 255),  # where the labels put the marker's quote
}
RULER_LEGEND = (
    "régua à direita da captura, de 4 colunas: na 1ª, amarelo = bordas de cima e de baixo do marcador e laranja = a linha"
    " da cotação dentro dele; na 2ª, cinza = bordas de cada linha de texto do eixo, verde = posição de um rótulo lido e"
    " vermelho = linha de texto que não é rótulo; na 3ª, branco = fechamento da vela que se forma e ciano = linha do"
    " preço; na 4ª, branco = linha da cotação no gráfico e magenta = onde os rótulos põem a cotação do marcador"
)


def diagnostic_png(image, reading=None):
    """A PNG of the capture as it is, with a ruler on its right that marks the rows found in it."""
    width = image.width + RULER
    pixels = bytearray(width * image.height * 3)
    row_bytes = image.width * 3
    for y in range(image.height):
        pixels[y * width * 3:y * width * 3 + row_bytes] = image.data[y * row_bytes:(y + 1) * row_bytes]

    def mark(row, color, band, length=12):
        if row is None:
            return
        y = math.floor(row)
        if 0 <= y < image.height:
            start = (y * width + image.width + 14 * band + 1) * 3
            pixels[start:start + 3 * length] = bytes(RULER_COLORS[color]) * length

    g = reading.geometry if reading is not None else None
    if g is not None:
        if g.marker_box is not None:
            mark(g.marker_box.y0, "marker", 0)
            mark(g.marker_box.y1 - 1, "marker", 0)
        if g.marker_rows is not None:
            mark(g.marker_rows[0], "quote", 0, 6)
            mark(g.marker_rows[1] - 1, "quote", 0, 6)
        for axis_line in g.axis_lines:
            mark(axis_line.top, "text", 1)
            mark(axis_line.bottom - 1, "text", 1)
            mark(axis_line.row, "label" if axis_line.price is not None else "unread", 1)
        mark(g.close_row, "close", 2)
        mark(g.price_line, "line", 2, 6)
        mark(g.price_row, "close", 3)
        mark(g.predicted_row, "predicted", 3, 6)
    return quote_reader.png_bytes(width, image.height, pixels)


def stamp(reading, captured_at, period):
    """({index: start} of the candles of reading, None or why those times are not certain).

    captured_at is this computer's clock, in UTC, at the capture. The forming candle (index 0)
    started at the last multiple of period; the candle at index k, k periods earlier. The starts
    always say which candles follow each other; near a change of candle, within
    BOUNDARY_GUARD_SECONDS, they may all be one period off, and the reason says so: such times
    are never recorded nor used for a decision.
    """
    epoch = captured_at.timestamp()
    start = math.floor(epoch / period) * period
    into = epoch - start
    starts = {index: datetime.fromtimestamp(start - index * period, timezone.utc) for index in reading.indices}
    if into < BOUNDARY_GUARD_SECONDS or period - into < BOUNDARY_GUARD_SECONDS:
        return starts, (
            f"a captura foi a {min(into, period - into):.1f} s da troca de vela, e o relógio deste computador não"
            " diz com segurança qual vela estava se formando"
        )
    return starts, None


def seconds_into(captured_at, period):
    """Seconds from the start of the forming candle to captured_at, by this computer's clock."""
    epoch = captured_at.timestamp()
    return epoch - math.floor(epoch / period) * period


PREVIEW_NUMBERED = 6  # candles numbered on the preview, from the forming one (0) back: those the terminal shows
MIN_NUMBER_SPACING = 7.0  # device pixels: closer candles get their outline on the preview, but no number
FORMING_COLOR = (255, 220, 0)  # yellow: the outline and the number of the forming candle, 0, on the preview
PREVIEW_COLOR = quote_reader.MARK_COLOR  # magenta: the outline and the number of the others, and the tick under the rest
PREVIEW_LEGEND = (
    f"o número de cada uma das {PREVIEW_NUMBERED} últimas velas fica logo acima dela (abaixo, quando não cabe acima),"
    " sobre as colunas dela e de nenhuma outra: 0, em amarelo, é a vela que se forma, 1 é a última fechada, e assim por"
    " diante; as outras velas lidas têm um traço logo abaixo"
)
DENSE_LEGEND = (
    f"as velas estão a menos de {MIN_NUMBER_SPACING:g} pixels umas das outras, e os números não caberiam sem confusão:"
    f" as {PREVIEW_NUMBERED} últimas têm só um contorno, amarelo na que se forma (0) e magenta nas de 1 a"
    f" {PREVIEW_NUMBERED - 1}, da direita para a esquerda; as outras velas lidas têm um traço logo abaixo. Aproxime o"
    " zoom do gráfico para ver os números"
)


def preview_legend(reading):
    """How to read the preview of reading, for the terminal."""
    return PREVIEW_LEGEND if reading.spacing >= MIN_NUMBER_SPACING else DENSE_LEGEND


@dataclass(frozen=True)
class PreviewMark:
    """How one candle is marked on the preview, in the pixels of the capture."""

    index: int
    candle: Candle
    color: tuple
    number: object = None  # (x, y, size, text): the top left corner of the number's digits, their size and text
    outlined: bool = False  # one of the last PREVIEW_NUMBERED: an outline, else a tick under it


def preview_marks(reading, height, numbered=PREVIEW_NUMBERED):
    """PreviewMark of each candle of reading, in the order of reading.candles.

    The number of a candle is centered on its columns, right above its top, or below its bottom when it does
    not fit above (inside the capture, over its own columns, when it fits neither). Its digits are those of
    quote_reader._DIGITS, 3 by 5 cells of size pixels, and the cells are as large as leaves two cells, at
    least, between two numbers side by side: the digits of one number are one cell apart, so 3 and 2 never
    read as 32, and no number covers another candle. Candles closer than MIN_NUMBER_SPACING get no number,
    only their outline: there, numbers of 1-pixel cells would come next to each other or over the next candle.
    """
    scale = reading.geometry.scale if reading.geometry is not None else 1.0
    largest = max(3, round(3 * scale))
    marks = []
    for index, candle in zip(reading.indices, reading.candles):
        color = FORMING_COLOR if index == 0 else PREVIEW_COLOR
        if index >= numbered or reading.spacing < MIN_NUMBER_SPACING:
            marks.append(PreviewMark(index, candle, color, outlined=index < numbered))
            continue
        text = str(index)
        cells = 4 * len(text) - 1  # 3 per digit and 1 between two
        size = max(1, min(largest, math.floor((reading.spacing - 2) / (cells + 2))))
        width, tall = cells * size, 5 * size
        x = round((candle.x0 + candle.x1 - width) / 2)
        y = candle.top - 3 - tall  # above the outline, with a row free and the black border of the number
        if y - 1 < 0:
            y = candle.bottom + 4
        y = max(1, min(y, height - tall - 1))
        marks.append(PreviewMark(index, candle, color, (x, y, size, text), True))
    return marks


def preview_png(image, reading, numbered=PREVIEW_NUMBERED):
    """A PNG of the capture with each candle read marked, and the last numbered ones with their index.

    The last numbered candles get an outline touching them and, when the candles are MIN_NUMBER_SPACING or
    more apart, their number on their own columns (preview_marks); the forming candle, 0, in FORMING_COLOR.
    Every other candle read gets a tick two rows under it.
    """
    width, height = image.width, image.height
    pixels = bytearray(image.data)

    def paint(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            i = (y * width + x) * 3
            pixels[i:i + 3] = bytes(color)

    marks = preview_marks(reading, height, numbered)
    for mark in marks:
        candle = mark.candle
        if not mark.outlined:
            for x in range(candle.x0, candle.x1):
                paint(x, candle.bottom + 2, mark.color)
            continue
        for x in range(candle.x0 - 1, candle.x1 + 1):
            paint(x, candle.top - 1, mark.color)
            paint(x, candle.bottom + 1, mark.color)
        for y in range(candle.top - 1, candle.bottom + 2):
            paint(candle.x0 - 1, y, mark.color)
            paint(candle.x1, y, mark.color)
    for mark in marks:
        if mark.number is None:
            continue
        x0, y0, size, text = mark.number
        for y in range(y0 - 1, y0 + 5 * size + 1):
            for x in range(x0 - 1, x0 + (4 * len(text) - 1) * size + 1):
                paint(x, y, (0, 0, 0))
        for n, char in enumerate(text):
            for row, bits in enumerate(quote_reader._DIGITS[char]):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(size):
                            for dx in range(size):
                                paint(x0 + (4 * n + column) * size + dx, y0 + row * size + dy, mark.color)
    return quote_reader.png_bytes(width, height, pixels)


# The calibration file.

CALIBRATION_FILE_NAME = "chart-candles.json"
CALIBRATION_NOTE = (
    "Gravado por python -m pocketautonomo.chart --configure (no Windows, grafico_windows.cmd), depois da sua"
    " conferência da prévia: up e down são as cores das velas de alta e de baixa, e period, os segundos de cada"
    " vela no gráfico. Chaves que começam com _ são ignoradas."
)


class CalibrationError(Exception):
    """The calibration file exists and cannot be used; the message says why."""


def load_calibration(path):
    """The CandleModel in path, or None without the file; CalibrationError for an unusable file."""
    import json

    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CalibrationError(f"não foi possível ler {path}: {error}") from error
    try:
        return CandleModel.from_json(json.loads(text))
    except ValueError as error:
        raise CalibrationError(
            f"{path} não serve ({error}). Calibre de novo com grafico_windows.cmd (python -m pocketautonomo.chart"
            " --configure) ou mude o nome do arquivo para continuar sem a leitura das velas"
        ) from error


def save_calibration(path, model):
    """Write model to path through a temporary file put in place at once."""
    import json
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump({"_leia_me": CALIBRATION_NOTE, **model.to_json()}, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)
