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
   read_marker_text): its price, and the row of its center.
2. The price scale. The labels of the axis, the plain text in the columns of the marker, are
   cut into lines, and each line is read twice by the OCR, in gray and in black and white, with
   the checks of the marker (QUOTE_PATTERN, agreed_quote, MIN_SCORE). A straight line is fitted
   through the (row, price) of the labels read: at least MIN_LABELS of them, each within
   SCALE_TOLERANCE of the line (one label far from it is left out when four or more remain), with
   the price going down the rows. The marker must fall on that line too, within
   MARKER_TOLERANCE. Two readings that do not depend on each other, the axis and the marker, must
   agree on the scale, or nothing is read.
3. The candles. Every pixel left of the axis is an up candle's, a down candle's or neither, by the
   calibrated colors (CandleModel) and their blends with the background, so that the
   anti-aliased edges and wicks count. Runs of one color in a row longer than MAX_CANDLE_WIDTH are
   lines (the price line, a level), not candles, and are left out. The runs that touch across rows
   make one candle: its body is where most of its columns are, its wick its tallest column. The
   candles are evenly spaced: a gap of k spacings means k - 1 candles that were not read, and an
   irregular gap or a shape wider than MAX_WIDTH_RATIO times the others makes the reading
   unavailable, because something there is not a candle. A candle cut by an edge of the capture
   is not read.
4. The rightmost candle is the one still forming: its close must be at the marker's row, within
   MARKER_TOLERANCE. Otherwise the chart may be scrolled back, zoomed or still loading, and
   nothing is read.
5. Prices. Each edge of a body or a wick gives a price on the fitted line. The resolution, the
   price of one device pixel, says how precise every value is: nothing smaller than it can be
   seen.

Times. The time axis of the chart is not read. The period comes from the calibration, and the
times from this computer's clock (stamp): the forming candle started at the last multiple of the
period, the candle before it one period earlier, and so on. Near a change of candle, within
BOUNDARY_GUARD_SECONDS, the clock cannot tell which candle is forming, so the candles of such a
reading get no times. This needs the clock of this computer to be right within a few seconds;
the same candle read again with other values reveals a wrong clock, a wrong period or another
asset's chart (chart_analysis.CandleHistory).
"""

import math
from dataclasses import dataclass
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
SCALE_TOLERANCE = 1.5  # CSS pixels: how far a label may be from the fitted line
MARKER_TOLERANCE = 2.5  # CSS pixels: the marker on the line, and the forming candle's close at the marker
AXIS_CONTRAST = 48  # smallest difference per channel between the axis text and the background
TEXT_HEIGHT = (5.0, 24.0)  # CSS pixels: the height of a line of the axis
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
    marker_row: object = None  # its center, in device pixels
    spacing: float = 0.0  # device pixels between two candles
    resolution: float = 0.0  # price of one device pixel
    decimals: int = 0  # of the marker's quote: how the prices are shown
    unread_labels: int = 0  # lines of the axis that the OCR could not read as a price

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


def _text_crop(image, x0, y0, x1, y1, background):
    """The region as gray, dark where it differs most from the background."""
    distances = []
    for y in range(y0, y1):
        for pixel in image.row(y, x0, x1):
            distances.append(max(abs(pixel[0] - background[0]), abs(pixel[1] - background[1]), abs(pixel[2] - background[2])))
    full = max(distances) if distances else 0
    if full < AXIS_CONTRAST:
        return None
    data = bytes(255 - min(255, round(255 * distance / full)) for distance in distances)
    return GrayImage(x1 - x0, y1 - y0, data)


def axis_labels(image, scale, x0, skip, engine):
    """([(row, text, price)], unread) of the price labels of the axis, in the columns from x0.

    skip, (first, last + 1), are the rows of the marker, left out. The labels are read from the
    top down, each twice, as the marker's text is.
    """
    background = background_color(image, x0, image.width)

    def text_pixel(color):
        return max(abs(color[0] - background[0]), abs(color[1] - background[1]), abs(color[2] - background[2])) >= AXIS_CONTRAST

    rows = []
    for y in range(image.height):
        if skip is not None and skip[0] <= y < skip[1]:
            rows.append(False)
        else:
            rows.append(any(map(text_pixel, image.row(y, x0, image.width))))
    low, high = TEXT_HEIGHT[0] * scale, TEXT_HEIGHT[1] * scale
    labels, unread = [], 0
    y = 0
    while y < image.height and len(labels) < MAX_AXIS_LABELS:
        if not rows[y]:
            y += 1
            continue
        start = y
        while y < image.height and rows[y]:
            y += 1
        cut = start == 0 or y == image.height or (skip is not None and (start == max(0, skip[0]) or start == skip[1] or y == skip[0]))
        if not low <= y - start <= high or cut:
            continue  # not a line of text, or cut by an edge or by the marker: its middle is not its price's row
        columns = [
            x for x in range(x0, image.width) if any(text_pixel(image.pixel(x, row)) for row in range(start, y))
        ]
        if not columns:
            continue
        left, right = max(0, columns[0] - 2), min(image.width, columns[-1] + 3)
        gray = _text_crop(image, left, max(0, start - 2), right, min(image.height, y + 2), background)
        lines = quote_reader._text_lines(gray) if gray is not None else []
        if len(lines) != 1:
            unread += 1
            continue
        line = lines[0]
        (text_a, score_a), (text_b, score_b) = engine.recognize(line), engine.recognize(black_and_white(line))
        text_a, text_b = "".join(text_a.split()), "".join(text_b.split())
        quote, _ = agreed_quote(text_a, text_b)
        value = number_of(quote) if quote is not None and QUOTE_PATTERN.fullmatch(quote) else None
        if value is None or min(score_a, score_b) < MIN_SCORE:
            unread += 1
            continue
        labels.append(((start + y) / 2, quote, value))
    return labels, unread


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


def read_candles(image, scale, model, marker_model, engine):
    """CandleReading of a capture of the whole chart element, in device pixels, scale per CSS pixel."""
    box, reason = locate_marker(image, scale, marker_model)
    if box is None:
        return unavailable(f"o marcador da cotação não foi achado: {reason}")
    marker = read_marker_text(image, box, engine)
    if marker.value is None:
        return unavailable(f"o marcador da cotação não foi lido: {marker.reason}")
    marker_price = number_of(marker.value)
    marker_row = (box.y0 + box.y1) / 2
    decimals = decimals_of(marker.value)
    found = {"marker_text": marker.value, "marker_price": marker_price, "marker_row": marker_row, "decimals": decimals}
    axis_x0 = max(0, box.x0 - round(2 * scale))
    margin = round(2 * scale)
    labels, unread = axis_labels(image, scale, axis_x0, (box.y0 - margin, box.y1 + margin), engine)
    found["unread_labels"] = unread
    line, problem = fit_scale(labels, scale)
    if line is None:
        return unavailable(problem, **found)
    found["scale"] = line
    found["resolution"] = abs(line.slope)
    tolerance = MARKER_TOLERANCE * scale + 0.5
    expected = line.row(marker_price)
    if abs(expected - marker_row) > tolerance:
        return unavailable(
            f"o marcador ({marker.value}) não confere com a escala lida no eixo: ele está a"
            f" {abs(expected - marker_row) / scale:.1f} pixels de onde a escala põe esse preço",
            **found,
        )
    # The marker is centered on its price, while the ink of a label's digits sits a pixel or so off its
    # tick: the slope comes from the labels, and the line goes through the marker.
    line = ScaleLine(marker_price - line.slope * marker_row, line.slope, line.labels, line.dropped, line.residual)
    found["scale"] = line
    background = background_color(image, 0, axis_x0)
    shapes = [
        shape
        for shape in shapes_of(image, scale, axis_x0, classifier(model, background))
        if shape.width >= MIN_CANDLE_WIDTH * scale and shape.pixels >= 4
    ]
    if shapes and shapes[0].x0 == 0:
        shapes = shapes[1:]  # cut by the left edge of the capture
    if len(shapes) < MIN_CANDLES:
        return unavailable(
            f"{len(shapes)} vela(s) com as cores calibradas no gráfico, de pelo menos {MIN_CANDLES}: confira as cores"
            " com --configure e o zoom do gráfico",
            **found,
        )
    heights = sorted(shape.bottom - shape.top + 1 for shape in shapes)
    if heights[len(heights) // 2] < 2 * scale:
        return unavailable(
            "as formas da cor das velas no gráfico são linhas de um pixel, não velas: confira as cores com --configure",
            **found,
        )
    widths = sorted(shape.width for shape in shapes)
    median_width = widths[len(widths) // 2]
    wide = [shape for shape in shapes if shape.width > MAX_WIDTH_RATIO * median_width]
    if wide:
        return unavailable(
            f"{len(wide)} forma(s) da cor das velas mais larga(s) que as velas ({wide[0].width} pixels, contra"
            f" {median_width}): velas encostadas umas nas outras ou outro desenho no gráfico; afaste o zoom ou tire os"
            " indicadores",
            **found,
        )
    gaps = [b.center - a.center for a, b in zip(shapes, shapes[1:])]
    spacing = sorted(gaps)[len(gaps) // 2]
    if spacing < median_width:
        return unavailable("as velas se sobrepõem no gráfico", **found)
    for gap in gaps:
        steps = round(gap / spacing)
        if steps < 1 or abs(gap - steps * spacing) > SPACING_TOLERANCE * spacing:
            return unavailable(
                "o espaçamento entre as velas é irregular: há no gráfico algo da cor das velas que não é uma vela", **found,
            )
    found["spacing"] = spacing
    forming = shapes[-1]
    close_row = forming.body_top if forming.kind == UP else forming.body_bottom + 1
    if abs(close_row - marker_row) > tolerance:
        return unavailable(
            "a última vela não termina na cotação do marcador: o gráfico pode estar rolado para trás, com outro zoom ou"
            " ainda carregando",
            **found,
        )
    last = forming.center
    candles, indices = [], []
    for shape in shapes:
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
        indices.append(round((last - shape.center) / spacing))
    if not indices or indices[-1] != 0:
        return unavailable("a vela que está se formando foi cortada pela borda da captura", **found)
    present = set(indices)
    missing = tuple(index for index in range(1, max(indices) + 1) if index not in present)
    return CandleReading(
        None, tuple(candles), tuple(indices), missing, line, marker.value, marker_price, marker_row, spacing,
        abs(line.slope), decimals, unread,
    )


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


def preview_png(image, reading, numbered=6):
    """A PNG of the capture with every candle outlined; the last numbered ones carry their index."""
    boxes = []
    for index, candle in zip(reading.indices, reading.candles):
        boxes.append((index if index < numbered else "", candle.box()))
    return quote_reader.annotated_png(image, boxes)


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
