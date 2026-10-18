"""Analysis of the chart's candles: what the chart shows now, and whether a rule on it holds up.

NO ORDERS, AND NO PAID AI. Everything here is computed on this computer, with fixed rules and the
standard library, from the candles that pocketautonomo.candles read on the screen.

Two answers, always kept apart:

- The exploratory reading (read_market): whether the completed candles on the chart now favor a
  rise (ALTA), a fall (BAIXA) or neither (INDEFINIDO), with every signal found, in plain words,
  and the limits of the reading. It is NEVER a decision to enter: the way it weighs the signals
  was written before any data and was not validated.
- The validated decision (decide): CALL or PUT only when a fixed rule on the candles (CHART_RULES)
  was chosen on development data and measured on separate evaluation data, with the criteria of
  analysis.validate: at least MIN_DEV_TRADES operations of the rule to choose it, at least
  MIN_EVAL_TRADES to measure it, and the lower bound of the 99% Wilson interval of its rate of wins
  above the break-even of a 90% payout. The reading must also be current, made in the first
  ENTRY_WINDOW_SECONDS of a candle, as the evaluation assumes, consistent with the candles already
  recorded, with a payout of 90% or more. Otherwise AGUARDAR, with the reason.

The signals (signals_of), chosen because each can be measured from the open, high, low and close of
visible candles and stays above the resolution of the reading (one pixel). Every size is in units
of the average range of the candles (ATR of ATR_BARS candles), so they compare across assets:

- short trend: the move of the last SHORT_BARS candles;
- medium trend: the slope of a straight line through the closes of the last MEDIUM_BARS candles;
- impulse: the move of the last IMPULSE_BARS candles, and whether it speeds up or slows down
  against the IMPULSE_BARS before them;
- sequence: SEQUENCE candles or more of one color in a row at the end, and the size of their bodies;
- wicks: a wick longer than half the range of the last candle and twice its body, a rejection;
- levels: the highest high and the lowest low of the LEVEL_BARS candles before the last one, as
  approximate resistance and support: a close beyond them is a breakout, and a wick beyond them
  that closes back inside is a rejection of the level;
- volatility: the average range of the last 5 candles against the last 20, expanding or
  contracting; it is context, not a direction.

The reading favors one side only with MIN_AGREEING directional signals or more on that side,
including a trend, and none on the other; a single pattern is never enough, and no confidence or
probability is given, because none was calibrated.

The candles of each reading are recorded in runtime/velas-<data>-<hora>-<sufixo>.csv
(CandleLog), the forming candle never. CandleHistory merges every file: one candle per asset,
period and start; the same candle read again with other values, beyond the resolution of the two
readings, is a conflict and leaves the analysis, because one of the readings was wrong (the clock,
the period, or another asset's chart). The evaluation (validate) uses only completed candles:
each decision at the close of a candle sees only that candle and the ones before it, and its
result is the close of the candle that ends HORIZON later. A difference smaller than the
resolution of the readings is not a win or a loss: it counts apart, as a draw. The decisions are
a horizon apart, so that no two operations overlap, and the reference (BASELINE, the color of the
last candle) is measured on the same evaluation decisions, to compare.
"""

import csv
import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from pocketautonomo import analysis, candles, observe
from pocketautonomo.analysis import CALL, DRAW, LOSS, PUT, WAIT, WIN, Trade, evaluate

ALTA, BAIXA, INDEFINIDO = "ALTA", "BAIXA", "INDEFINIDO"
ORIGIN_CHART = "gráfico"  # a candle read on the screen, from the chart's pixels
CANDLE_PREFIX = "velas-"
CANDLE_COLUMNS = (
    "asset", "period_s", "start_utc", "open", "high", "low", "close", "kind", "resolution", "read_at_utc", "origin",
)

ATR_BARS = 14
SHORT_BARS = 5
MEDIUM_BARS = 20
IMPULSE_BARS = 3
LEVEL_BARS = 20
MIN_BARS = LEVEL_BARS + 1  # completed candles in a row, at least, for a reading: every signal can be computed
MIN_ATR_PIXELS = 3.0  # an average range smaller than this many pixels is too small to read
TREND_SHORT = 0.5  # ATR
TREND_MEDIUM = 1.0  # ATR
IMPULSE = 0.5  # ATR
SEQUENCE = 3
WICK_SHARE = 0.5
MIN_AGREEING = 3
EXPANSION = 1.5
MAX_READING_AGE = analysis.DECISION_WINDOW_SECONDS
ENTRY_WINDOW_SECONDS = 10.0  # a decision is made only this soon after a candle closed, as the evaluation assumes
WRITE_RETRY_SECONDS = 30.0

EXPLORATORY = "LEITURA EXPLORATÓRIA, não é sinal de entrada"
NOT_VALIDATED = (
    "a forma de juntar os sinais foi escrita antes de qualquer dado e não foi validada: esta leitura descreve o"
    " gráfico, não diz que o próximo movimento vai acontecer"
)


@dataclass(frozen=True)
class Bar:
    """A completed candle: its prices, its start by this computer's clock and the resolution of its reading."""

    open: float
    high: float
    low: float
    close: float
    start: object = None  # datetime, UTC
    resolution: float = 0.0  # the price of one pixel of the reading
    kind: object = None  # candles.UP or candles.DOWN, by its color


@dataclass(frozen=True)
class Signal:
    key: str
    name: str
    direction: int  # +1 favors a rise, -1 a fall, 0 neither
    text: str


@dataclass(frozen=True)
class MarketReading:
    """The exploratory reading of the chart: ALTA, BAIXA or INDEFINIDO, its signals and its limits."""

    direction: str
    summary: str
    signals: tuple = ()
    limitations: tuple = ()
    bars: int = 0  # completed candles in a row used

    def describe(self):
        return f"{EXPLORATORY}: {self.summary}"

    def signals_text(self):
        return "; ".join(f"{signal.name}: {signal.text}" for signal in self.signals)


def number(value, decimals=2):
    return f"{value:.{decimals}f}".replace(".", ",")


def atr_of(bars, count=ATR_BARS):
    """The average true range of the last count candles, or None with fewer than two candles."""
    ranges = []
    for previous, bar in zip(bars, bars[1:]):
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close)))
    ranges = ranges[-count:]
    return sum(ranges) / len(ranges) if ranges else None


def kind_of(bar):
    if bar.kind in (candles.UP, candles.DOWN):
        return bar.kind
    if bar.close > bar.open:
        return candles.UP
    if bar.close < bar.open:
        return candles.DOWN
    return None


def sign(value, threshold):
    return 1 if value >= threshold else -1 if value <= -threshold else 0


def moved(value):
    return f"subiu {number(value)} ATR" if value >= 0 else f"caiu {number(-value)} ATR"


def signals_of(bars, atr, resolution):
    """The Signals of the last candles of bars, which has MIN_BARS candles or more."""
    found = []
    move = (bars[-1].close - bars[-1 - SHORT_BARS].close) / atr
    found.append(Signal("short", "tendência curta", sign(move, TREND_SHORT), f"nas últimas {SHORT_BARS} velas, o preço {moved(move)}"))
    closes = [bar.close for bar in bars[-MEDIUM_BARS:]]
    n = len(closes)
    middle, mean = (n - 1) / 2, sum(closes) / n
    slope = sum((i - middle) * (close - mean) for i, close in enumerate(closes)) / sum((i - middle) ** 2 for i in range(n))
    implied = slope * (n - 1) / atr
    found.append(
        Signal(
            "medium", "tendência média", sign(implied, TREND_MEDIUM),
            f"a reta das últimas {MEDIUM_BARS} velas {'sobe' if implied >= 0 else 'desce'} {number(abs(implied))} ATR",
        )
    )
    recent = (bars[-1].close - bars[-1 - IMPULSE_BARS].close) / atr
    previous = (bars[-1 - IMPULSE_BARS].close - bars[-1 - 2 * IMPULSE_BARS].close) / atr
    direction = sign(recent, IMPULSE)
    if direction and previous * recent > 0:
        pace = "acelerando" if abs(recent) > abs(previous) else "no mesmo sentido, mais devagar"
    elif direction and abs(previous) >= IMPULSE:
        pace = f"virando, depois de {'subir' if previous > 0 else 'cair'} {number(abs(previous))} ATR"
    elif direction:
        pace = "depois de pouco movimento"
    elif abs(previous) >= IMPULSE:
        pace = f"perdendo força: antes, {'subiu' if previous > 0 else 'caiu'} {number(abs(previous))} ATR"
    else:
        pace = "pouco movimento"
    found.append(Signal("impulse", "impulso", direction, f"nas últimas {IMPULSE_BARS} velas, {moved(recent)}, {pace}"))
    last_kind = kind_of(bars[-1])
    run = 0
    for bar in reversed(bars):
        if last_kind is None or kind_of(bar) != last_kind:
            break
        run += 1
    bodies = [abs(bar.close - bar.open) / atr for bar in bars[-run:]] if run else []
    strength = f", corpos de {number(sum(bodies) / len(bodies))} ATR em média" if bodies else ""
    if run >= SEQUENCE:
        direction = 1 if last_kind == candles.UP else -1
        text = f"{run} velas de {last_kind} seguidas{strength}"
    else:
        direction = 0
        text = f"{run} vela(s) de {last_kind} seguida(s){strength}" if last_kind else "a última vela não tem corpo"
    found.append(Signal("sequence", "sequência", direction, text))
    last = bars[-1]
    span = last.high - last.low
    body = abs(last.close - last.open)
    upper = last.high - max(last.open, last.close)
    lower = min(last.open, last.close) - last.low
    if span < MIN_ATR_PIXELS * resolution:
        found.append(Signal("wicks", "pavios", 0, "a última vela é pequena demais para medir os pavios"))
    elif upper >= WICK_SHARE * span and upper >= 2 * body:
        found.append(Signal("wicks", "pavios", -1, f"pavio superior de {number(upper / span * 100, 0)}% da última vela: a alta foi rejeitada"))
    elif lower >= WICK_SHARE * span and lower >= 2 * body:
        found.append(Signal("wicks", "pavios", 1, f"pavio inferior de {number(lower / span * 100, 0)}% da última vela: a queda foi rejeitada"))
    else:
        found.append(Signal("wicks", "pavios", 0, "sem pavio dominante na última vela"))
    before = bars[-1 - LEVEL_BARS:-1]
    resistance, support = max(bar.high for bar in before), min(bar.low for bar in before)
    if last.close > resistance:
        found.append(Signal("levels", "níveis", 1, f"fechou acima da máxima das {LEVEL_BARS} velas anteriores: rompimento de alta"))
    elif last.close < support:
        found.append(Signal("levels", "níveis", -1, f"fechou abaixo da mínima das {LEVEL_BARS} velas anteriores: rompimento de baixa"))
    elif last.high > resistance:
        found.append(Signal("levels", "níveis", -1, f"passou da máxima das {LEVEL_BARS} velas anteriores e fechou abaixo dela: rejeição da resistência"))
    elif last.low < support:
        found.append(Signal("levels", "níveis", 1, f"passou da mínima das {LEVEL_BARS} velas anteriores e fechou acima dela: rejeição do suporte"))
    else:
        found.append(
            Signal(
                "levels", "níveis", 0,
                f"entre o suporte e a resistência das {LEVEL_BARS} velas anteriores, a {number((resistance - last.close) / atr)} ATR"
                f" da resistência e a {number((last.close - support) / atr)} ATR do suporte",
            )
        )
    fast, slow = atr_of(bars, 5), atr_of(bars, 20)
    ratio = fast / slow if slow else 1.0
    if ratio >= EXPANSION:
        text = f"em expansão: as últimas 5 velas variam {number(ratio)} vezes a média das últimas 20"
    elif ratio <= 1 / EXPANSION:
        text = f"em contração: as últimas 5 velas variam {number(ratio)} vez a média das últimas 20"
    else:
        text = f"estável: as últimas 5 velas variam {number(ratio)} vez a média das últimas 20"
    found.append(Signal("volatility", "volatilidade", 0, text))
    return found, ratio


def contiguous_tail(bars, period):
    """The last candles of bars that follow each other without a missing one (all, without starts)."""
    if not bars or bars[-1].start is None or period is None:
        return list(bars)
    tail = [bars[-1]]
    for bar in reversed(bars[:-1]):
        if bar.start is None or (tail[-1].start - bar.start).total_seconds() != period:
            break
        tail.append(bar)
    return list(reversed(tail))


def read_market(bars, resolution, period=None, age=None):
    """The MarketReading of the completed candles bars, oldest first; age in seconds, when known."""
    limitations = [NOT_VALIDATED]
    if age is not None and age > MAX_READING_AGE:
        return MarketReading(
            INDEFINIDO,
            f"leitura desatualizada: capturada há {number(age, 1)} s, mais que {number(MAX_READING_AGE, 0)} s",
            limitations=tuple(limitations),
        )
    tail = contiguous_tail(bars, period)
    if len(tail) < len(bars):
        limitations.append(f"faltam velas no gráfico lido: só as últimas {len(tail)} seguidas entram")
    if len(tail) < MIN_BARS:
        return MarketReading(
            INDEFINIDO,
            f"dados insuficientes: {len(tail)} velas completas seguidas, de pelo menos {MIN_BARS}",
            limitations=tuple(limitations),
            bars=len(tail),
        )
    tail = tail[-max(MIN_BARS, ATR_BARS + 1, MEDIUM_BARS + 1):]
    atr = atr_of(tail)
    if not atr or atr < MIN_ATR_PIXELS * resolution:
        pixels = atr / resolution if atr and resolution else 0
        return MarketReading(
            INDEFINIDO,
            f"as velas estão pequenas demais na escala do gráfico (variação média de {number(pixels, 1)} pixels, de pelo"
            f" menos {number(MIN_ATR_PIXELS, 0)}): aproxime o zoom do gráfico",
            limitations=tuple(limitations),
            bars=len(tail),
        )
    found, ratio = signals_of(tail, atr, resolution)
    if ratio >= EXPANSION:
        limitations.append("volatilidade em expansão: movimentos maiores e mais incertos que o normal")
    up = [signal for signal in found if signal.direction > 0]
    down = [signal for signal in found if signal.direction < 0]
    trend_up = any(signal.key in ("short", "medium") for signal in up)
    trend_down = any(signal.key in ("short", "medium") for signal in down)

    def names(group):
        return ", ".join(signal.name for signal in group)

    if up and down:
        direction = INDEFINIDO
        summary = f"indefinido, sinais em conflito: {len(up)} de alta ({names(up)}) e {len(down)} de baixa ({names(down)})"
    elif len(up) >= MIN_AGREEING and trend_up:
        direction, summary = ALTA, f"favorece ALTA: {len(up)} sinais de alta ({names(up)}) e nenhum de baixa"
    elif len(down) >= MIN_AGREEING and trend_down:
        direction, summary = BAIXA, f"favorece BAIXA: {len(down)} sinais de baixa ({names(down)}) e nenhum de alta"
    else:
        side, group = ("alta", up) if up else ("baixa", down)
        shown = f"{len(group)} sinal(is) de {side} ({names(group)})" if group else "nenhum sinal de direção"
        direction = INDEFINIDO
        summary = (
            f"indefinido, sem sinais suficientes: {shown}; a leitura pede {MIN_AGREEING} de um lado, com uma tendência, e"
            " nenhum do outro"
        )
    limitations.append("um padrão de velas não garante o próximo movimento")
    return MarketReading(direction, summary, tuple(found), tuple(limitations), len(tail))


# The rules for the validation, fixed before any data, and the reference.


@dataclass(frozen=True)
class ChartRule:
    """A fixed rule on the completed candles; kind and lookback_factor name it in a decision's id."""

    kind: str
    lookback_factor: int

    def describe(self, horizon):
        return f"{self.kind} (velas do gráfico)"


CONSENSUS = ChartRule("leitura do gráfico", MIN_BARS)
TREND_RULE = ChartRule("tendência de 5 velas", SHORT_BARS)
REVERSAL_RULE = ChartRule("reversão de 5 velas", SHORT_BARS)
BREAKOUT_RULE = ChartRule("rompimento de 20 velas", LEVEL_BARS)
CHART_RULES = (CONSENSUS, TREND_RULE, REVERSAL_RULE, BREAKOUT_RULE)
BASELINE = ChartRule("cor da última vela", 1)  # the reference: measured, never chosen


def rule_direction(rule, window, resolution):
    """CALL, PUT or None: what rule says after the last candle of window, from window only."""
    if rule is BASELINE:
        last = window[-1]
        if abs(last.close - last.open) <= resolution:
            return None
        return CALL if last.close > last.open else PUT
    atr = atr_of(window)
    if not atr or atr < MIN_ATR_PIXELS * resolution:
        return None
    if rule is CONSENSUS:
        reading = read_market(window, resolution)
        return {ALTA: CALL, BAIXA: PUT}.get(reading.direction)
    if rule in (TREND_RULE, REVERSAL_RULE):
        move = sign((window[-1].close - window[-1 - SHORT_BARS].close) / atr, TREND_SHORT)
        if not move:
            return None
        up = move > 0 if rule is TREND_RULE else move < 0
        return CALL if up else PUT
    if rule is BREAKOUT_RULE:
        before = window[-1 - LEVEL_BARS:-1]
        if window[-1].close > max(bar.high for bar in before):
            return CALL
        if window[-1].close < min(bar.low for bar in before):
            return PUT
        return None
    raise ValueError(f"regra desconhecida: {rule}")


# The history of the candles read.


@dataclass
class CandleSeries:
    """The completed candles of one asset and one period, by start, without conflicts."""

    name: str
    period: int
    bars: list = field(default_factory=list)
    _by_start: dict = field(default_factory=dict, repr=False)
    version: int = 0

    def get(self, start):
        return self._by_start.get(start)

    def insert(self, bar):
        index = len(self.bars)
        while index and self.bars[index - 1].start > bar.start:
            index -= 1
        self.bars.insert(index, bar)
        self._by_start[bar.start] = bar
        self.version += 1

    def remove(self, start):
        bar = self._by_start.pop(start)
        self.bars.remove(bar)
        self.version += 1


@dataclass
class CandleHistory:
    """The candles read on the chart, from every file, with what was left out and why."""

    series: dict = field(default_factory=dict)  # (asset key, period) -> CandleSeries
    conflicts: set = field(default_factory=set)  # (asset key, period, start) read with different values
    repeated: int = 0  # the same candle read again, with the same values
    files: int = 0
    skipped: dict = field(default_factory=dict)  # reason -> count

    def skip(self, reason, count=1):
        self.skipped[reason] = self.skipped.get(reason, 0) + count

    def get(self, name, period):
        return self.series.get((analysis.asset_key(name), period))

    def disagreements(self, name, period, bars):
        """The candles of bars that a recorded candle of the same start contradicts."""
        series = self.get(name, period)
        if series is None:
            return []
        return [bar for bar in bars if series.get(bar.start) is not None and not same_candle(series.get(bar.start), bar)]

    def add(self, name, period, bar):
        """'nova', 'igual' or 'conflito': what adding bar did."""
        key = (analysis.asset_key(name), period)
        if (key[0], period, bar.start) in self.conflicts:
            return "conflito"
        series = self.series.setdefault(key, CandleSeries(name, period))
        known = series.get(bar.start)
        if known is None:
            series.insert(bar)
            return "nova"
        if same_candle(known, bar):
            self.repeated += 1
            return "igual"
        series.remove(bar.start)
        self.conflicts.add((key[0], period, bar.start))
        return "conflito"


def same_candle(a, b):
    """Whether two readings of one candle agree, within two pixels of the coarser of the two readings."""
    tolerance = 2 * max(a.resolution, b.resolution)
    return (
        kind_of(a) == kind_of(b)
        and all(abs(x - y) <= tolerance for x, y in ((a.open, b.open), (a.high, b.high), (a.low, b.low), (a.close, b.close)))
    )


def candle_files(runtime_dir):
    return sorted(runtime_dir.glob(f"{CANDLE_PREFIX}2*.csv"))


def load_candles(runtime_dir, exclude=()):
    """The CandleHistory of every candle file in runtime_dir; the files are only read."""
    history = CandleHistory()
    excluded = {path.resolve() for path in exclude}
    for path in candle_files(runtime_dir):
        if path.resolve() in excluded:
            continue
        try:
            with open(path, newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                if tuple(reader.fieldnames or ()) != CANDLE_COLUMNS:
                    history.skip("arquivos de velas com outras colunas")
                    continue
                rows = list(reader)
        except (OSError, csv.Error, UnicodeDecodeError):
            history.skip("arquivos de velas ilegíveis")
            continue
        history.files += 1
        for row in rows:
            parsed = bar_of(row)
            if parsed is None:
                history.skip("linhas de velas inválidas")
                continue
            name, period, bar = parsed
            history.add(name, period, bar)
    return history


def bar_of(row):
    """(asset, period, Bar) of a line of a candle file, or None when it is not a valid one."""
    if row.get("origin") != ORIGIN_CHART:
        return None
    name = " ".join((row.get("asset") or "").split())
    try:
        period = int(row.get("period_s") or "")
        start = datetime.fromisoformat(row.get("start_utc") or "")
        values = [float(row.get(key) or "") for key in ("open", "high", "low", "close", "resolution")]
    except ValueError:
        return None
    if not name or start.tzinfo is None or not candles.MIN_PERIOD <= period <= candles.MAX_PERIOD:
        return None
    open_, high, low, close, resolution = values
    if not all(math.isfinite(value) for value in values) or resolution <= 0 or not low <= min(open_, close) <= max(open_, close) <= high:
        return None
    kind = row.get("kind") if row.get("kind") in (candles.UP, candles.DOWN) else None
    return name, period, Bar(open_, high, low, close, start.astimezone(timezone.utc), resolution, kind)


class CandleLog:
    """A new runtime/velas-<data>-<hora>-<sufixo>.csv for the candles of one run, flushed after each line.

    A failure to write is said once, through warn; the candles stay in the history of this run,
    and a new file is tried WRITE_RETRY_SECONDS later.
    """

    def __init__(self, runtime_dir, warn):
        self.runtime_dir = runtime_dir
        self.path = None
        self.paths = []
        self.written = 0
        self.failed = False
        self._warn = warn
        self._file = self._writer = None
        self._retry_at = None

    def write(self, name, period, bars, read_at):
        if not bars:
            return
        if self._file is None and self._retry_at is not None and read_at < self._retry_at:
            return
        try:
            if self._file is None:
                self.runtime_dir.mkdir(parents=True, exist_ok=True)
                while True:
                    path = self.runtime_dir / f"{CANDLE_PREFIX}{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csv"
                    if not path.exists():
                        break
                self._file = open(path, "x", newline="", encoding="utf-8")
                self.path = path
                self.paths.append(path)
                self._writer = csv.writer(self._file)
                self._writer.writerow(CANDLE_COLUMNS)
            for bar in bars:
                self._writer.writerow((
                    name, period, observe.utc_text(bar.start), repr(bar.open), repr(bar.high), repr(bar.low),
                    repr(bar.close), bar.kind or "", repr(bar.resolution), observe.utc_text(read_at), ORIGIN_CHART,
                ))
            self._file.flush()
            self.written += len(bars)
        except OSError as error:
            self.close()
            self._retry_at = read_at + timedelta(seconds=WRITE_RETRY_SECONDS)
            if not self.failed:
                self._warn(
                    f"AVISO: não foi possível gravar as velas lidas em {self.path or self.runtime_dir} ({error}); a leitura"
                    f" continua, essas velas ficam só na análise desta execução, e um arquivo novo é tentado a cada"
                    f" {WRITE_RETRY_SECONDS:g} s."
                )
            self.failed = True

    def close(self):
        file, self._file, self._writer = self._file, None, None
        if file is not None:
            try:
                file.close()
            except OSError:
                pass


# The evaluation.


@dataclass(frozen=True)
class Moment:
    index: int  # of the candle whose close is the decision
    at: datetime  # its close: the moment of the decision


@dataclass(frozen=True)
class ChartValidation:
    """The rule chosen on development, its measure on evaluation, and the reference on the same decisions."""

    validated: bool
    reason: str
    rule: object = None
    development: object = None  # analysis.Evaluation
    evaluation: object = None
    baseline: object = None  # the reference on the evaluation decisions
    decisions: int = 0  # decision moments with a known result
    break_even: float = 0.0
    bars: int = 0


def moments_of(series, horizon):
    """The decision moments of series: a close with MIN_BARS candles in a row up to it, a horizon apart."""
    moments, last = [], None
    period = series.period
    run = 0
    for index, bar in enumerate(series.bars):
        if index and (bar.start - series.bars[index - 1].start).total_seconds() == period:
            run += 1
        else:
            run = 1
        if run < MIN_BARS:
            continue
        at = bar.start + timedelta(seconds=period)
        if last is not None and (at - last).total_seconds() < horizon:
            continue
        moments.append(Moment(index, at))
        last = at
    return moments


DRAW_PIXELS = 2.0  # a move this small, in pixels of the readings, is within their precision: neither a win nor a loss


def result_of(series, moment, horizon, direction):
    """WIN, LOSS or DRAW (a move within the precision of the readings) of an operation at moment, or None."""
    bar = series.bars[moment.index]
    exit_bar = series.get(bar.start + timedelta(seconds=horizon))
    if exit_bar is None:
        return None
    if abs(exit_bar.close - bar.close) <= DRAW_PIXELS * max(bar.resolution, exit_bar.resolution):
        return DRAW
    return WIN if (exit_bar.close > bar.close) == (direction == CALL) else LOSS


def window_of(series, moment):
    return series.bars[moment.index - MIN_BARS + 1:moment.index + 1]


def trades_of(rule, series, moments, horizon):
    trades = []
    for moment in moments:
        window = window_of(series, moment)
        direction = rule_direction(rule, window, window[-1].resolution)
        if direction is None:
            continue
        result = result_of(series, moment, horizon, direction)
        if result is not None:
            trades.append(Trade(moment.at, direction, result))
    return trades


def split(series, horizon):
    """(development, evaluation, labelled), split in time as analysis.split does."""
    moments = moments_of(series, horizon)
    labelled = [moment for moment in moments if result_of(series, moment, horizon, CALL) is not None]
    if not labelled:
        return [], [], []
    cut = labelled[min(len(labelled) - 1, int(len(labelled) * analysis.DEV_FRACTION))].at
    development = [moment for moment in moments if moment.at + timedelta(seconds=horizon) <= cut]
    evaluation = [moment for moment in moments if moment.at >= cut]
    return development, evaluation, labelled


def needed_text(series, horizon):
    """What the evaluation still needs, in decisions and in hours of candles in a row."""
    step = max(1, round(horizon / series.period))
    decisions = math.ceil(analysis.MIN_EVAL_TRADES / (1 - analysis.DEV_FRACTION))
    hours = decisions * step * series.period / 3600
    return (
        f"são precisos pelo menos {decisions} momentos de decisão com resultado ({analysis.MIN_DEV_TRADES} operações da regra"
        f" no desenvolvimento e {analysis.MIN_EVAL_TRADES} na avaliação), cerca de {number(hours, 0)} horas de velas de"
        f" {series.period} s seguidas deste ativo"
    )


def validate(series, horizon, payout=analysis.ELIGIBLE_PAYOUT):
    """The ChartValidation of CHART_RULES on series: chosen on development, measured on evaluation."""
    threshold = analysis.break_even(payout)
    count = len(series.bars) if series is not None else 0
    if series is None or not series.bars:
        return ChartValidation(False, "sem velas gravadas deste ativo", break_even=threshold)
    if horizon % series.period:
        return ChartValidation(
            False,
            f"o horizonte de {analysis.seconds_text(horizon)} s não é múltiplo do período das velas, {series.period} s",
            break_even=threshold, bars=count,
        )
    development, measured, labelled = split(series, horizon)
    base = f"{count} velas de {series.period} s gravadas, {len(labelled)} momentos de decisão com resultado"
    if not labelled:
        return ChartValidation(
            False, f"dados insuficientes: {base}; {needed_text(series, horizon)}", break_even=threshold, bars=count,
        )
    results = [(rule, evaluate(trades_of(rule, series, development, horizon))) for rule in CHART_RULES]
    eligible = [(rule, result) for rule, result in results if result.trades >= analysis.MIN_DEV_TRADES]
    if not eligible:
        best = max(result.trades for _, result in results)
        return ChartValidation(
            False,
            f"dados insuficientes: {base}; no máximo {best} de {analysis.MIN_DEV_TRADES} operações por regra no"
            f" desenvolvimento; {needed_text(series, horizon)}",
            decisions=len(labelled), break_even=threshold, bars=count,
        )
    rule, chosen = max(eligible, key=lambda item: item[1].rate)  # the first of CHART_RULES on a tie
    if chosen.rate <= threshold:
        return ChartValidation(
            False,
            f"nenhuma das {len(CHART_RULES)} regras das velas passou de {analysis.percent_text(threshold)} no desenvolvimento"
            f" (a melhor, {rule.describe(horizon)}: {analysis.counts_text(chosen)})",
            rule, chosen, decisions=len(labelled), break_even=threshold, bars=count,
        )
    evaluation = evaluate(trades_of(rule, series, measured, horizon))
    baseline = evaluate(trades_of(BASELINE, series, measured, horizon))
    if evaluation.trades < analysis.MIN_EVAL_TRADES:
        return ChartValidation(
            False,
            f"regra {rule.describe(horizon)} em avaliação: {evaluation.trades} de {analysis.MIN_EVAL_TRADES} operações em"
            " dados separados; sem elas, nenhuma taxa de acerto é mostrada",
            rule, chosen, evaluation, baseline, len(labelled), threshold, count,
        )
    low, _ = evaluation.interval()
    reference = (
        f"; a referência ({BASELINE.kind}), nas mesmas decisões: {analysis.rate_text(baseline)}"
        if baseline.trades
        else f"; a referência ({BASELINE.kind}) não deu direção nessas decisões"
    )
    if low <= threshold:
        return ChartValidation(
            False,
            f"regra {rule.describe(horizon)} não validada: {analysis.rate_text(evaluation)}; o limite inferior precisa passar"
            f" de {analysis.percent_text(threshold)}{reference}",
            rule, chosen, evaluation, baseline, len(labelled), threshold, count,
        )
    return ChartValidation(
        True,
        f"regra {rule.describe(horizon)} validada: {analysis.rate_text(evaluation)}, acima de"
        f" {analysis.percent_text(threshold)}{reference}",
        rule, chosen, evaluation, baseline, len(labelled), threshold, count,
    )


# A reading on the screen, and what it gives.


@dataclass(frozen=True)
class ChartSetup:
    """What reading the candles needs: the calibration, the chart's source and the history."""

    model: object  # candles.CandleModel
    source: object  # observe.QuoteOcrSource: the chart's selector and the marker
    asset_source: str  # the selector of the asset's name
    horizon: float
    history: CandleHistory
    log: object = None  # CandleLog
    cache: dict = field(default_factory=dict)  # (asset key, period) -> (version, ChartValidation)

    def validation(self, name):
        series = self.history.get(name, self.model.period)
        if series is None:
            return validate(None, self.horizon)
        key = (analysis.asset_key(name), self.model.period)
        cached = self.cache.get(key)
        if cached is None or cached[0] != series.version:
            cached = self.cache[key] = (series.version, validate(series, self.horizon))
        return cached[1]


@dataclass(frozen=True)
class ChartObservation:
    """One capture of the chart, its candles and how they fit what was already recorded."""

    asset: str
    reading: object  # candles.CandleReading, or None when nothing was captured
    captured_at: object = None
    finished_at: object = None
    stamps: object = None  # {index: start} by this computer's clock
    problem: object = None  # why nothing usable came out of it
    stamp_problem: object = None  # why those starts are not certain: the candles are neither recorded nor decided on
    ms: object = None
    added: int = 0  # new candles recorded
    conflicts: int = 0  # recorded candles that this reading contradicted

    @property
    def usable(self):
        return self.problem is None and self.reading is not None and self.reading.available

    def bars(self):
        """The completed candles as Bars, oldest first, with their starts."""
        if not self.usable:
            return []
        stamps = self.stamps or {}
        return [
            Bar(candle.open, candle.high, candle.low, candle.close, stamps.get(index), self.reading.resolution, candle.kind)
            for index, candle in self.reading.completed
        ]

    def status(self, period):
        if self.problem is not None:
            return f"indisponível: {self.problem}"
        reading = self.reading
        text = (
            f"{len(reading.completed)} velas completas de {period} s e a que se forma (escala por"
            f" {len(reading.scale.labels)} rótulos do eixo, conferida pelo marcador {reading.marker_text}; 1 pixel ="
            f" {reading.text(reading.resolution)}); {self.added} nova(s) gravada(s)"
        )
        if reading.missing:
            text += f"; {len(reading.missing)} posição(ões) sem vela lida"
        if self.stamp_problem:
            text += f"; velas não gravadas: {self.stamp_problem}"
        return text


def observe_chart(session, setup, expected, ctx):
    """ChartObservation of the chart now: the asset expected on the screen before and after the capture.

    It only reads: the asset field, the chart element's capture and the OCR of its marker and axis.
    """
    start = ctx.monotonic()
    before = observe.read_field(session, "asset", setup.asset_source, ctx)
    if not (before.available and analysis.asset_key(before.value) == analysis.asset_key(expected)):
        shown = before.value if before.available else f"INDISPONÍVEL ({before.reason})"
        return ChartObservation(expected, None, problem=f"a tela mostrava {shown}, e não {expected}, antes da captura do gráfico")
    captured_at = ctx.now()
    capture, problem = observe.capture_chart(session, setup.source.chart)
    after = observe.read_field(session, "asset", setup.asset_source, ctx)
    if capture is None:
        return ChartObservation(expected, None, captured_at, problem=problem, ms=ctx.elapsed_ms(start))
    if not (after.available and analysis.asset_key(after.value) == analysis.asset_key(expected)):
        shown = after.value if after.available else f"INDISPONÍVEL ({after.reason})"
        return ChartObservation(
            expected, None, captured_at, problem=f"a tela mostrava {shown}, e não {expected}, depois da captura do gráfico",
            ms=ctx.elapsed_ms(start),
        )
    reading = candles.read_candles(capture.image, capture.scale, setup.model, setup.source.marker, ctx.engine.get())
    stamps = stamp_problem = None
    if reading.available:
        stamps, stamp_problem = candles.stamp(reading, captured_at, setup.model.period)
    return ChartObservation(
        expected, reading, captured_at, ctx.now(), stamps, None if reading.available else reading.reason, stamp_problem,
        ctx.elapsed_ms(start),
    )


MAX_LOCAL_CONFLICTS = 2  # recorded candles a reading may contradict before the reading itself counts as wrong


def absorb(setup, observation):
    """observation, with its new completed candles recorded when it agrees with the recorded ones.

    A reading that contradicts recorded candles, beyond the resolution of the two, is not used at
    all. When it contradicts many of them (more than MAX_LOCAL_CONFLICTS, or a fifth of those it
    shares), the reading itself is wrong, by its clock, its period or its asset, and nothing of it
    is kept. When it contradicts one or two, there is no telling which reading was right: both
    versions leave the analysis as conflicts, and the new one is written too, so that the next
    start sees the conflict again. The candles of a reading near a change of candle are not
    recorded (stamp_problem).
    """
    if not observation.usable or observation.stamp_problem is not None:
        return observation
    period = setup.model.period
    bars = observation.bars()
    series = setup.history.get(observation.asset, period)
    shared = sum(series.get(bar.start) is not None for bar in bars) if series is not None else 0
    contradicted = setup.history.disagreements(observation.asset, period, bars)
    if contradicted:
        if len(contradicted) > max(MAX_LOCAL_CONFLICTS, shared // 5):
            problem = (
                f"a leitura não confere com {len(contradicted)} de {shared} velas já gravadas deste ativo, além da resolução:"
                " o relógio deste computador, o período calibrado ou o ativo do gráfico podem estar errados; nada desta"
                " leitura foi usado nem gravado"
            )
        else:
            for bar in contradicted:
                setup.history.add(observation.asset, period, bar)
            if setup.log is not None:
                setup.log.write(observation.asset, period, contradicted, observation.captured_at)
            problem = (
                f"{len(contradicted)} vela(s) desta leitura diferem das já gravadas, além da resolução: as duas versões ficam"
                " fora da análise, como conflito, e esta leitura não foi usada"
            )
        return replace(observation, problem=problem, conflicts=len(contradicted))
    new = [bar for bar in bars if setup.history.add(observation.asset, period, bar) == "nova"]
    if setup.log is not None:
        setup.log.write(observation.asset, period, new, observation.captured_at)
    return replace(observation, added=len(new))


@dataclass(frozen=True)
class ChartAssessment:
    """What a reading gives: the exploratory reading and the validated decision, kept apart."""

    observation: ChartObservation
    market: MarketReading
    decision: analysis.Decision
    validation: ChartValidation
    period: int = 0  # seconds of each candle


def assess(setup, observation, payout, now):
    """ChartAssessment of an absorbed observation, at now (this computer's clock, UTC)."""
    name = observation.asset
    validation = setup.validation(name)
    if not observation.usable:
        reason = f"sem leitura das velas: {observation.problem}"
        market = MarketReading(INDEFINIDO, reason, limitations=(NOT_VALIDATED,))
        return ChartAssessment(observation, market, analysis.Decision(name, WAIT, reason, validation), validation, setup.model.period)
    reading = observation.reading
    age = (now - observation.captured_at).total_seconds()
    market = read_market(observation.bars(), reading.resolution, setup.model.period, age)
    sample = analysis.Sample(observation.captured_at, reading.marker_price, reading.marker_text, payout, ORIGIN_CHART)

    def wait(reason):
        return ChartAssessment(observation, market, analysis.Decision(name, WAIT, reason, validation, sample), validation, setup.model.period)

    if not validation.validated:
        return wait(validation.reason)
    if not 0 <= age <= MAX_READING_AGE:
        return wait(f"{validation.reason}; mas a leitura é de há {number(age, 1)} s, e uma decisão só vale até {number(MAX_READING_AGE, 0)} s depois da captura")
    if observation.stamp_problem is not None:
        return wait(f"{validation.reason}; mas {observation.stamp_problem}")
    into = candles.seconds_into(observation.captured_at, setup.model.period)
    if into > ENTRY_WINDOW_SECONDS:
        return wait(
            f"{validation.reason}; mas a regra foi medida com a decisão no fechamento da vela, e a leitura foi"
            f" {number(into, 0)} s depois da abertura da vela atual, mais que {number(ENTRY_WINDOW_SECONDS, 0)} s"
        )
    if payout is None or payout < analysis.ELIGIBLE_PAYOUT:
        shown = "indisponível" if payout is None else f"{number(payout, 1)}%"
        return wait(f"{validation.reason}; mas o payout {shown} não permite a primeira entrada, que exige 90% ou mais")
    bars = contiguous_tail(observation.bars(), setup.model.period)
    if len(bars) < MIN_BARS:
        return wait(f"{validation.reason}; mas a leitura tem só {len(bars)} velas seguidas, de {MIN_BARS}")
    direction = rule_direction(validation.rule, bars[-MIN_BARS:], reading.resolution)
    if direction is None:
        return wait(f"{validation.reason}; mas a regra não dá direção agora")
    decision = analysis.Decision(
        name, direction, validation.reason, validation, sample,
        observation.captured_at + timedelta(seconds=analysis.DECISION_WINDOW_SECONDS),
    )
    return ChartAssessment(observation, market, decision, validation, setup.model.period)


def combine(quote_decision, chart_decision):
    """The decision of a visit from the quotes' analysis and the candles' one.

    The quotes' decision keeps its place: it is the one before the candles existed. A CALL or PUT
    of the candles counts only when the quotes' analysis says AGUARDAR; two validated rules that
    disagree give AGUARDAR.
    """
    if chart_decision is None or chart_decision.action not in (CALL, PUT):
        return quote_decision
    if quote_decision is not None and quote_decision.action in (CALL, PUT):
        if quote_decision.action != chart_decision.action:
            return analysis.Decision(
                quote_decision.asset, WAIT,
                f"duas regras validadas discordam: as cotações dizem {quote_decision.action} e as velas,"
                f" {chart_decision.action}",
                quote_decision.validation, quote_decision.sample,
            )
        return quote_decision
    return chart_decision


def report_lines(runtime_dir, horizon):
    """The terminal lines of the candles recorded and of the validation of their rules, by asset."""
    history = load_candles(runtime_dir)
    lines = [
        f"VELAS DO GRÁFICO, lidas na tela: {history.files} arquivo(s), {sum(len(s.bars) for s in history.series.values())}"
        f" velas utilizáveis em {len(history.series)} série(s) (ativo e período)."
    ]
    if history.repeated:
        lines.append(f"  {history.repeated} leituras repetidas da mesma vela, com os mesmos valores, contadas uma vez.")
    if history.conflicts:
        lines.append(
            f"  Fora da análise: {len(history.conflicts)} vela(s) lida(s) com valores diferentes em duas leituras (conflito)."
        )
    for reason, total in sorted(history.skipped.items()):
        lines.append(f"  Ignorados: {total} {reason}.")
    if not history.series:
        lines.append(
            f"  {WAIT}: nenhuma vela gravada. Calibre a leitura das velas com grafico_windows.cmd; depois, ela roda na"
            " varredura e na operação simulada, a cada visita."
        )
    for series in sorted(history.series.values(), key=lambda item: (item.name, item.period)):
        first, last = series.bars[0].start, series.bars[-1].start
        lines.append(
            f"  {series.name}, velas de {series.period} s: {len(series.bars)}, de {first.isoformat(timespec='minutes')} a"
            f" {last.isoformat(timespec='minutes')}"
        )
        lines.append(f"    regras das velas: {validate(series, horizon).reason}")
    return lines
