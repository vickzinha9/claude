"""Visual reading of the asset's quote (cotação do ativo) from a capture of the chart.

The chart draws the quote on a canvas, so the page has no text to read there. This module
finds the current-quote marker in an RGB capture of the chart region and reads its text
with an OCR engine passed in. It is pure Python, without third-party packages, so the
stdlib-only tests cover it; the capture (Playwright and Pillow) and the OCR engine
(RapidOCR) live in observe.py and quote_ocr.py.

Finding the marker:

- find_label_boxes looks, in the right part of the capture, for filled boxes with
  contrasting text inside: the shape of a price label. Scale prices are text without a
  box, and candles and bars are boxes without text, so neither qualifies. The cursor's
  label, an open trade's label or a tooltip may qualify: the configuration lists every
  candidate, numbered on a preview image, and you choose the one that follows the current
  quote. Only the chart region is captured, so the Amount field and the rest of the page
  never enter.
- The marker of the Pocket Option is semi-transparent: on the captures of the validation,
  its fill was #3f638e over the dark background and #498390 over a green candle. So a box
  is never made of one exact color. In the configuration, each region of one exact color
  is only a seed: the box grows from it to every touching pixel within GROW_TOLERANCE of
  the seed's color, so the whole label is found, whatever part of it lies over a candle.
- A row belongs to a box when the box's pixels reach both of its sides, the left and the
  right margins of the label, even in the rows of the text, whose glyphs cut the row into
  pieces. A thin line of another color across the label, up to ROW_GAP rows, does not
  split it.
- MarkerModel keeps what was learned from that choice: the fill colors of the box (each
  that covers FILL_SHARE of it or more), its size and its distance to the right edge of the
  region, in CSS pixels. No vertical position is stored: the marker moves with the quote
  and is found anew in each capture.
- locate_marker accepts exactly one box whose pixels are within the tolerance of the
  model's colors, with its size and alignment, fully inside the capture. No box, more than
  one, or a box cut by the edge of the capture makes the reading unavailable, with the
  reason.

Reading the text: the box is cropped, turned into dark text on white and split into text
lines. Each line is read twice, in gray and in black and white. A value is accepted only
when exactly one line reads as a quote (digits with a decimal separator), both readings of
that line agree and both have confidence MIN_SCORE or more. Otherwise the reading is
unavailable, with the reason: nothing is replaced by zero, no digit is corrected and no
earlier value is reused.
"""

import re
import struct
import zlib
from dataclasses import dataclass
from itertools import groupby

QUOTE_PATTERN = re.compile(r"[0-9]+(?:[.,][0-9]+)+")  # at least one decimal separator
MIN_SCORE = 0.9
# Largest difference per channel to count as the marker's color. A semi-transparent label
# changes color with what is behind it: 32 between the two fills seen in the validation.
COLOR_TOLERANCE = 40
GROW_TOLERANCE = 40  # configuration: how far from the seed's color the box may grow
TEXT_CONTRAST = 64  # smallest difference per channel between the text and its box
BACKGROUND_DISTANCE = 24  # configuration: colors closer than this to the background are skipped
MIN_RUN = 3  # device pixels of the same color in a row
ROW_GAP = 2  # rows of another color that may cross a box without splitting it
EDGE_FRACTION = 0.15  # how close to each side of the box a row's pixels must reach
FILL_SHARE = 0.05  # smallest share of a box's fill for a color to enter the model
MAX_MODEL_COLORS = 4
SEARCH_FRACTION = 0.5  # configuration: the right half of the capture is searched
MAX_CANDIDATES = 12

# A label box, in CSS pixels; the capture is in device pixels, `scale` per CSS pixel.
MIN_BOX_HEIGHT, MAX_BOX_HEIGHT = 8, 48
MIN_BOX_WIDTH, MAX_BOX_WIDTH = 16, 240
TEXT_RATIO_RANGE = (0.03, 0.65)  # share of the box interior taken by contrasting pixels
# What may change between the configuration and a later capture, in CSS pixels or ratios.
ALIGN_TOLERANCE = 8
WIDTH_RANGE = (0.6, 1.6)
HEIGHT_RANGE = (0.7, 1.4)


@dataclass(frozen=True)
class RgbImage:
    """An RGB image, 3 bytes per pixel, row by row, top to bottom."""

    width: int
    height: int
    data: bytes

    def __post_init__(self):
        if self.width <= 0 or self.height <= 0 or len(self.data) != self.width * self.height * 3:
            raise ValueError("imagem RGB com tamanho inconsistente")

    def pixel(self, x, y):
        i = (y * self.width + x) * 3
        return tuple(self.data[i:i + 3])

    def row(self, y, x0, x1):
        """The pixels of row y from x0 to x1, as (r, g, b) tuples."""
        start = (y * self.width + x0) * 3
        part = self.data[start:start + (x1 - x0) * 3]
        return zip(part[0::3], part[1::3], part[2::3])


@dataclass(frozen=True)
class GrayImage:
    """A grayscale image, 1 byte per pixel, row by row; 0 is black and 255 is white."""

    width: int
    height: int
    data: bytes

    def __post_init__(self):
        if self.width <= 0 or self.height <= 0 or len(self.data) != self.width * self.height:
            raise ValueError("imagem em tons de cinza com tamanho inconsistente")


@dataclass(frozen=True)
class Box:
    """A filled box in a capture, in device pixels; x1 and y1 are exclusive."""

    x0: int
    y0: int
    x1: int
    y1: int
    color: tuple
    text_ratio: float = 0.0
    cut: bool = False  # touches the top or the bottom of the capture

    @property
    def width(self):
        return self.x1 - self.x0

    @property
    def height(self):
        return self.y1 - self.y0


@dataclass(frozen=True)
class MarkerModel:
    """What identifies the current-quote marker, learned from your choice; CSS pixels."""

    colors: tuple  # one or more (r, g, b)
    width: float
    height: float
    right_gap: float  # from the right edge of the box to the right edge of the capture
    tolerance: int = COLOR_TOLERANCE

    @classmethod
    def from_box(cls, box, image, scale):
        """The model of box in image: its fill colors, size and gap to the right edge."""
        return cls(
            colors=fill_colors(image, box),
            width=round(box.width / scale, 1),
            height=round(box.height / scale, 1),
            right_gap=round((image.width - box.x1) / scale, 1),
        )

    def with_colors(self, colors):
        """This model also accepting colors; one that already matches is not added.

        The colors are kept in order, up to MAX_MODEL_COLORS: the model's own come first.
        """
        known = list(self.colors)
        for color in colors:
            if len(known) < MAX_MODEL_COLORS and not any(
                colors_match(color, other, self.tolerance // 2) for other in known
            ):
                known.append(tuple(color))
        if len(known) == len(self.colors):
            return self
        return MarkerModel(tuple(known), self.width, self.height, self.right_gap, self.tolerance)

    def matches(self, color):
        return any(colors_match(color, known, self.tolerance) for known in self.colors)

    def to_json(self):
        return {
            "colors": [color_hex(color) for color in self.colors],
            "width": self.width,
            "height": self.height,
            "right_gap": self.right_gap,
            "tolerance": self.tolerance,
        }

    @classmethod
    def from_json(cls, data):
        """The model in data; ValueError tells what is wrong."""
        if not isinstance(data, dict):
            raise ValueError("o marcador precisa ser um objeto JSON")
        unknown = sorted(set(data) - {"colors", "width", "height", "right_gap", "tolerance"})
        if unknown:
            raise ValueError(f"chave desconhecida no marcador: {', '.join(unknown)}")
        colors = data.get("colors")
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_MODEL_COLORS:
            raise ValueError(f"colors precisa ter de 1 a {MAX_MODEL_COLORS} cores")
        parsed = tuple(parse_color_hex(color) for color in colors)
        numbers = {}
        for key, low, high in (("width", MIN_BOX_WIDTH, MAX_BOX_WIDTH), ("height", MIN_BOX_HEIGHT, MAX_BOX_HEIGHT), ("right_gap", 0, 400)):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{key} precisa ser um número de {low} a {high}")
            numbers[key] = float(value)
        tolerance = data.get("tolerance", COLOR_TOLERANCE)
        if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 60:
            raise ValueError("tolerance precisa ser um inteiro de 0 a 60")
        return cls(parsed, numbers["width"], numbers["height"], numbers["right_gap"], tolerance)


@dataclass(frozen=True)
class QuoteReading:
    """The result of reading the marker: the value, or None and the reason."""

    value: object
    reason: object = None
    box: object = None  # the marker that was read, when one was found


def color_hex(color):
    return "#%02x%02x%02x" % tuple(color)


def parse_color_hex(text):
    if not isinstance(text, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        raise ValueError(f"cor inválida: {text!r}; use #rrggbb")
    return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))


def colors_match(a, b, tolerance):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])) <= tolerance


class _Classifier(dict):
    """A cache of function(color) for every color met; most captures have few colors."""

    def __init__(self, function):
        super().__init__()
        self.function = function

    def __missing__(self, color):
        key = self[color] = self.function(color)
        return key


def background_color(image, x0, x1):
    """The background's color: the mean of the most frequent band of colors in a sample.

    The colors are grouped in bands of 16 levels per channel, because a background picture
    has many slightly different colors and none of them alone is the most frequent: in the
    validation, the most frequent exact color was a candle's.
    """
    bands = {}
    for y in range(0, image.height, 3):
        for color in list(image.row(y, x0, x1))[::3]:
            band = (color[0] >> 4, color[1] >> 4, color[2] >> 4)
            entry = bands.get(band)
            if entry is None:
                bands[band] = [1, color[0], color[1], color[2]]
            else:
                entry[0] += 1
                entry[1] += color[0]
                entry[2] += color[1]
                entry[3] += color[2]
    n, r, g, b = max(bands.values(), key=lambda entry: entry[0])
    return (round(r / n), round(g / n), round(b / n))


def _runs(image, x0, x1, classify, rows=None):
    """Every row's runs of MIN_RUN or more pixels with the same key, as (y, xa, xb, key).

    rows, a (first, last + 1) pair, limits the rows; classify is a _Classifier.
    """
    first, end = rows if rows is not None else (0, image.height)
    runs = []
    for y in range(first, end):
        x = x0
        for key, group in groupby(map(classify.__getitem__, image.row(y, x0, x1))):
            n = len(list(group))
            if key is not None and n >= MIN_RUN:
                runs.append((y, x, x + n, key))
            x += n
    return runs


def _components(runs, gap=0):
    """Runs of the same key that overlap across rows, grouped; up to gap rows may separate them."""
    parent = list(range(len(runs)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_row = {}
    for index, (y, xa, xb, key) in enumerate(runs):
        for back in range(1, gap + 2):
            for other in by_row.get(y - back, ()):
                _, oa, ob, okey = runs[other]
                if okey == key and oa < xb and xa < ob:
                    a, b = root(index), root(other)
                    if a != b:
                        parent[a] = b
        by_row.setdefault(y, []).append(index)
    groups = {}
    for index in range(len(runs)):
        groups.setdefault(root(index), []).append(runs[index])
    return list(groups.values())


def _core_rectangle(component, gap=0):
    """The rectangle where most rows of the component sit, without an attached thin line.

    The left and right edges are the medians over the rows, so a price line that touches
    the box in one or two rows, or a candle's wick, does not widen it. A row belongs to the
    rectangle when the component reaches both edges in it, give or take EDGE_FRACTION of
    the width: in the rows of the text, the glyphs cut the fill into short pieces, but the
    label's margins are still there on both sides. The rows are the longest block of such
    rows, where up to gap missing rows in a row are bridged.
    """
    spans = {}
    for y, xa, xb, _ in component:
        span = spans.get(y)
        spans[y] = (xa, xb) if span is None else (min(span[0], xa), max(span[1], xb))
    lefts = sorted(a for a, _ in spans.values())
    rights = sorted(b for _, b in spans.values())
    x0, x1 = lefts[len(lefts) // 2], rights[len(rights) // 2]
    if x1 - x0 < MIN_RUN:
        return None
    edge = max(MIN_RUN, round((x1 - x0) * EDGE_FRACTION))
    good = sorted(y for y, (a, b) in spans.items() if a <= x0 + edge and b >= x1 - edge)
    if not good:
        return None
    blocks = []
    start = previous = good[0]
    for y in good[1:]:
        if y - previous > gap + 1:
            blocks.append((start, previous))
            start = y
        previous = y
    blocks.append((start, previous))
    first, last = max(blocks, key=lambda block: block[1] - block[0])
    return x0, first, x1, last + 1


def _fill_mode(image, rectangle, classify, key):
    """The most frequent color among the pixels of rectangle that classify gives key."""
    x0, y0, x1, y1 = rectangle
    counts = {}
    for y in range(y0, y1):
        for color in image.row(y, x0, x1):
            if classify[color] == key:
                counts[color] = counts.get(color, 0) + 1
    return max(counts, key=counts.get) if counts else None


def text_ratio(image, rectangle, color):
    """The share of the box interior whose color contrasts with the box: its text."""
    x0, y0, x1, y1 = rectangle
    inset = max(1, (y1 - y0) // 6)
    x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    if x1 <= x0 or y1 <= y0:
        return 0.0
    r0, g0, b0 = color
    count = 0
    for y in range(y0, y1):
        for r, g, b in image.row(y, x0, x1):
            if max(abs(r - r0), abs(g - g0), abs(b - b0)) >= TEXT_CONTRAST:
                count += 1
    return count / ((x1 - x0) * (y1 - y0))


def _rectangles(image, x0, x1, classify, rows=None, gap=ROW_GAP):
    """(rectangle, key, runs) of every component of the pixels that classify keeps."""
    classify = classify if isinstance(classify, _Classifier) else _Classifier(classify)
    found = []
    for component in _components(_runs(image, x0, x1, classify, rows), gap):
        rectangle = _core_rectangle(component, gap)
        if rectangle is not None:
            found.append((rectangle, component[0][3], len(component)))
    return found, classify


def find_label_boxes(image, scale, x0, x1, classify, limits=None, rows=None, gap=ROW_GAP):
    """Filled boxes with contrasting text inside, made of the pixels classify keeps.

    classify maps a color to a key, or None for pixels that are not part of a box; a box is
    made of touching pixels with the same key, and its color is the most frequent one of
    its fill. rows, a (first, last + 1) pair, limits the search to those rows.
    """
    limits = limits or (MIN_BOX_WIDTH, MAX_BOX_WIDTH, MIN_BOX_HEIGHT, MAX_BOX_HEIGHT)
    min_w, max_w, min_h, max_h = (value * scale for value in limits)
    found, classify = _rectangles(image, x0, x1, classify, rows, gap)
    boxes = []
    for rectangle, key, run_count in found:
        bx0, by0, bx1, by1 = rectangle
        width, height = bx1 - bx0, by1 - by0
        sized = min_w <= width <= max_w and min_h <= height <= max_h and width >= 1.2 * height
        if run_count < min_h or not sized:
            continue
        color = _fill_mode(image, rectangle, classify, key)
        if color is None:
            continue
        ratio = text_ratio(image, rectangle, color)
        if not TEXT_RATIO_RANGE[0] <= ratio <= TEXT_RATIO_RANGE[1]:
            continue
        cut = by0 == 0 or by1 == image.height
        boxes.append(Box(bx0, by0, bx1, by1, tuple(color), round(ratio, 3), cut))
    boxes.sort(key=lambda box: (box.y0, box.x0))
    return boxes


def _rect(box):
    return box.x0, box.y0, box.x1, box.y1


def _overlap(a, b):
    """The area shared by two (x0, y0, x1, y1) rectangles."""
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def candidate_boxes(image, scale):
    """Configuration: every label box in the right part of the capture, top to bottom.

    Each region of one exact color is a seed, with no need for text; the box grows from it
    to the touching pixels within GROW_TOLERANCE of the seed's color, near the seed. Only
    the grown boxes with text inside are candidates. A box found from more than one seed is
    listed once.
    """
    x0 = int(image.width * (1 - SEARCH_FRACTION))
    background = background_color(image, x0, image.width)

    def exact(color):
        return None if colors_match(color, background, BACKGROUND_DISTANCE) else color

    seeds, _ = _rectangles(image, x0, image.width, exact, gap=0)
    seeds = [
        (rectangle, key)
        for rectangle, key, _ in seeds
        if rectangle[3] - rectangle[1] >= MIN_RUN
        and MIN_BOX_WIDTH * scale * 0.5 <= rectangle[2] - rectangle[0] <= MAX_BOX_WIDTH * scale
    ]
    seeds.sort(key=lambda seed: -(seed[0][2] - seed[0][0]) * (seed[0][3] - seed[0][1]))
    margin_x, margin_y = int(MAX_BOX_WIDTH * scale / 2), int(MAX_BOX_HEIGHT * scale)
    boxes = []
    for (sx0, sy0, sx1, sy1), seed_color in seeds:
        seed = (sx0, sy0, sx1, sy1)
        if any(_overlap(seed, _rect(b)) == (sx1 - sx0) * (sy1 - sy0) for b in boxes):
            continue  # already inside a box found from another seed

        def near_seed(color, seed_color=seed_color):
            if colors_match(color, background, BACKGROUND_DISTANCE):
                return None
            return 0 if colors_match(color, seed_color, GROW_TOLERANCE) else None

        window_x0, window_x1 = max(x0, sx0 - margin_x), min(image.width, sx1 + margin_x)
        rows = (max(0, sy0 - margin_y), min(image.height, sy1 + margin_y))
        for box in find_label_boxes(image, scale, window_x0, window_x1, near_seed, rows=rows):
            rectangle = _rect(box)
            if not _overlap(rectangle, seed):
                continue
            area = box.width * box.height
            same = [
                b for b in boxes if _overlap(rectangle, _rect(b)) >= 0.5 * min(area, b.width * b.height)
            ]
            if same:
                if all(area > b.width * b.height for b in same):
                    boxes = [b for b in boxes if b not in same] + [box]
                continue
            boxes.append(box)
    boxes.sort(key=lambda box: (box.y0, box.x0))
    return boxes[:MAX_CANDIDATES]


def fill_colors(image, box):
    """The colors that fill box, most frequent first, for the model.

    Each one covers FILL_SHARE or more of the pixels within GROW_TOLERANCE of the box's
    color and is farther than COLOR_TOLERANCE // 2 from the colors kept before it; there are
    at most MAX_MODEL_COLORS. The box's outer rows and columns are left out: there, its
    anti-aliased edge mixes the fill with what lies around it.
    """
    counts, total = {}, 0
    x0, x1 = (box.x0 + 1, box.x1 - 1) if box.width > 4 else (box.x0, box.x1)
    y0, y1 = (box.y0 + 2, box.y1 - 2) if box.height > 6 else (box.y0, box.y1)
    for y in range(y0, y1):
        for color in image.row(y, x0, x1):
            if colors_match(color, box.color, GROW_TOLERANCE):
                counts[color] = counts.get(color, 0) + 1
                total += 1
    kept = []
    for color, n in sorted(counts.items(), key=lambda item: -item[1]):
        if n < FILL_SHARE * total or len(kept) == MAX_MODEL_COLORS:
            break
        if not any(colors_match(color, other, COLOR_TOLERANCE // 2) for other in kept):
            kept.append(color)
    return tuple(kept) or (tuple(box.color),)


def locate_marker(image, scale, model):
    """(box, None) for the only box that matches the model, or (None, reason).

    Every pixel within the tolerance of any of the model's colors belongs to the marker's
    fill, so a label partly over a candle and partly over the background stays one box.
    """
    reach = model.right_gap + model.width * WIDTH_RANGE[1] + ALIGN_TOLERANCE + 4
    x0 = max(0, int(image.width - reach * scale))

    def classify(color):
        return 0 if model.matches(color) else None

    # A box cut by the top or the bottom of the capture shows only part of its height, so
    # the search accepts lower boxes; only a whole one can be read.
    limits = (
        model.width * WIDTH_RANGE[0],
        model.width * WIDTH_RANGE[1],
        max(MIN_RUN / scale, model.height * 0.3),
        model.height * HEIGHT_RANGE[1],
    )
    boxes = find_label_boxes(image, scale, x0, image.width, classify, limits)
    boxes = [box for box in boxes if box.cut or box.height / scale >= model.height * HEIGHT_RANGE[0]]
    matching = []
    for box in boxes:
        right_gap = (image.width - box.x1) / scale
        left_gap = (image.width - box.x0) / scale
        if (
            abs(right_gap - model.right_gap) <= ALIGN_TOLERANCE
            or abs(left_gap - (model.right_gap + model.width)) <= ALIGN_TOLERANCE
        ):
            matching.append(box)
    whole = [box for box in matching if not box.cut]
    if len(whole) == 1 and len(matching) == 1:
        return whole[0], None
    if len(matching) > 1:
        return None, (
            f"{len(matching)} marcadores com a cor, o tamanho e a posição do marcador da"
            " cotação; leitura ambígua"
        )
    if matching:
        return None, "o marcador da cotação está cortado pela borda da região capturada"
    if boxes:
        return None, (
            "há caixas com a cor do marcador, mas nenhuma alinhada à borda direita como ele"
        )
    return None, (
        "o marcador da cotação não apareceu: nenhuma caixa com texto na cor, no tamanho e na"
        " posição dele"
    )


OFF_AXIS = 40  # a pixel this far from every blend of box and text colors is not text


def _luminance_free_crop(image, box):
    """The inside of the box as gray: 255 where the box color is, 0 where the text is.

    Each pixel is placed on the line from the box color to the text color, so the result
    does not depend on which of the two is lighter, nor on their hue. A pixel far from that
    line, such as the chart showing through a rounded corner, becomes white.
    """
    x0, y0, x1, y1 = box.x0 + 1, box.y0 + 1, box.x1 - 1, box.y1 - 1
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    base = box.color
    strong = []
    for y in range(y0, y1):
        for pixel in image.row(y, x0, x1):
            distance = max(abs(pixel[0] - base[0]), abs(pixel[1] - base[1]), abs(pixel[2] - base[2]))
            if distance >= TEXT_CONTRAST:
                strong.append((distance, pixel))
    if not strong:
        return None
    strong.sort()
    top = [pixel for _, pixel in strong[len(strong) * 2 // 3:]]
    text = tuple(sum(pixel[i] for pixel in top) / len(top) for i in range(3))
    axis = tuple(text[i] - base[i] for i in range(3))
    norm = sum(value * value for value in axis)
    if norm == 0:
        return None
    values = bytearray()
    for y in range(y0, y1):
        for pixel in image.row(y, x0, x1):
            t = min(1.0, max(0.0, sum((pixel[i] - base[i]) * axis[i] for i in range(3)) / norm))
            off = max(abs(pixel[i] - (base[i] + t * axis[i])) for i in range(3))
            values.append(255 if off > OFF_AXIS else 255 - round(255 * t))
    return GrayImage(x1 - x0, y1 - y0, bytes(values))


def _text_lines(gray):
    """Each text line of gray (dark text on white), cropped and padded with white."""
    width = gray.width
    dark_rows = [
        any(value < 128 for value in gray.data[y * width:(y + 1) * width]) for y in range(gray.height)
    ]
    lines = []
    y = 0
    while y < gray.height:
        if not dark_rows[y]:
            y += 1
            continue
        start = y
        while y < gray.height and dark_rows[y]:
            y += 1
        if y - start >= 3:
            lines.append((start, y))
    result = []
    for start, end in lines:
        columns = [
            x for x in range(width) if any(gray.data[row * width + x] < 128 for row in range(start, end))
        ]
        # One more pixel on each side keeps the light edges of the anti-aliased glyphs.
        left, right = max(0, columns[0] - 1), min(width, columns[-1] + 2)
        start, end = max(0, start - 1), min(gray.height, end + 1)
        pad = max(2, (end - start) // 3)
        out_width, out_height = right - left + 2 * pad, end - start + 2 * pad
        data = bytearray(b"\xff" * (out_width * out_height))
        for row in range(start, end):
            source = gray.data[row * width + left:row * width + right]
            target = (row - start + pad) * out_width + pad
            data[target:target + len(source)] = source
        result.append(GrayImage(out_width, out_height, bytes(data)))
    return result


def black_and_white(gray):
    return GrayImage(gray.width, gray.height, bytes(0 if value < 128 else 255 for value in gray.data))


def read_marker_text(image, box, engine):
    """QuoteReading of the text inside box, read by engine.recognize(GrayImage)."""
    gray = _luminance_free_crop(image, box)
    lines = _text_lines(gray) if gray is not None else []
    if not lines:
        return QuoteReading(None, "o marcador não tem texto legível", box)
    quotes, problems, seen = [], [], []
    for line in lines:
        (text_a, score_a), (text_b, score_b) = engine.recognize(line), engine.recognize(black_and_white(line))
        text_a, text_b = "".join(text_a.split()), "".join(text_b.split())
        seen.append(text_a or text_b)
        if not (QUOTE_PATTERN.fullmatch(text_a) or QUOTE_PATTERN.fullmatch(text_b)):
            continue
        if text_a != text_b:
            problems.append(f"as duas leituras do OCR divergiram: {text_a!r} e {text_b!r}")
        elif min(score_a, score_b) < MIN_SCORE:
            problems.append(
                f"o OCR leu {text_a!r} com confiança {min(score_a, score_b):.2f}, abaixo de {MIN_SCORE:.2f}"
            )
        else:
            quotes.append(text_a)
    if len(quotes) == 1 and not problems:
        return QuoteReading(quotes[0], None, box)
    if len(quotes) + len(problems) > 1:
        return QuoteReading(None, "o marcador tem mais de uma linha com número; leitura ambígua", box)
    if problems:
        return QuoteReading(None, problems[0], box)
    shown = ", ".join(repr(text) for text in seen if text) or "nada"
    return QuoteReading(None, f"o OCR leu {shown} no marcador, que não é uma cotação", box)


def read_quote(image, scale, model, engine):
    """QuoteReading of the current quote in a capture of the chart."""
    box, reason = locate_marker(image, scale, model)
    if box is None:
        return QuoteReading(None, reason)
    return read_marker_text(image, box, engine)


# A 3x5 font for the numbers drawn on the preview images.
_DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}
MARK_COLOR = (255, 0, 255)


def annotated_png(image, labelled_boxes):
    """A PNG of image with each (label, box) outlined and numbered, label left of the box."""
    width, height = image.width, image.height
    pixels = bytearray(image.data)

    def paint(x, y, color=MARK_COLOR):
        if 0 <= x < width and 0 <= y < height:
            i = (y * width + x) * 3
            pixels[i:i + 3] = bytes(color)

    for label, box in labelled_boxes:
        for t in range(2):
            for x in range(box.x0 - 2 - t, box.x1 + 2 + t):
                paint(x, box.y0 - 2 - t)
                paint(x, box.y1 + 1 + t)
            for y in range(box.y0 - 2 - t, box.y1 + 2 + t):
                paint(box.x0 - 2 - t, y)
                paint(box.x1 + 1 + t, y)
        size = 3
        text = str(label)
        left = box.x0 - 6 - len(text) * 4 * size
        top = box.y0 + (box.height - 5 * size) // 2
        for y in range(top - 2, top + 5 * size + 2):
            for x in range(left - 2, left + len(text) * 4 * size + 1):
                paint(x, y, (0, 0, 0))
        for n, char in enumerate(text):
            for row, bits in enumerate(_DIGITS.get(char, ("000",) * 5)):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(size):
                            for dx in range(size):
                                paint(left + (n * 4 + column) * size + dx, top + row * size + dy)
    raw = b"".join(b"\x00" + bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
