"""Local CALL/PUT analysis of the quotes read on the screen: python -m pocketautonomo.analysis.

NO ORDERS, AND NO PAID AI. This module only reads the scan's CSV files and the files of the
quotes read during the cycles of the simulated operation, and computes, on this computer,
with fixed rules and the standard library. It never touches the browser.

For each asset:

1. Data. Only quotes read on the platform's screen count, each with its origin: the scan's
   lines with analysis_ready = sim (varredura: the asset, the quote and the payout read
   together and checked) and the readings of the cycle's asset taken during a cycle of the
   simulated operation (ciclo: files cotacoes-ciclo-*.csv, written by
   pocketautonomo.operate, with the asset and the quote checked the same way and the
   payout only when it was confirmed). Every quote has its capture time from this
   computer's clock (price_captured_at_utc). Lines of files written before that column
   existed, or without it, lines of another origin and the same quote at the same moment
   twice are skipped and counted, never guessed. In each scan file, which holds one
   execution, the line of the first switch that the asset field did not confirm and every
   line after it are left out as potentially inconsistent, the scan's PAUSA: from that switch
   on, the chart may be another asset's while the asset's name is right. So are the quotes
   read during cycles after it in the same execution, and the lines whose execution the files
   cannot tell, as undetermined. Each file with lines left out is reported, and no file is
   ever changed. Results of the simulator, decisions and demonstrations are never read.
2. Hypothetical operations of a horizon H (--horizon, 300 s by default). An operation
   decided at the capture time t of a quote is a WIN for CALL when the first quote at or
   after t + H, at most LABEL_TOLERANCE_FRACTION of H later, is above the quote at t, a
   LOSS when below and a DRAW when equal; draws count in neither the wins nor the losses.
   The decisions are at least H apart, so that no two operations overlap.
3. Rules, fixed before any data: RULES, the direction of the move over the last lookback
   (tendência) or its opposite (reversão), for a lookback of 1 and of 3 times H. A rule
   uses only quotes before its decision.
4. Validation on separate data. The decisions are split in time: the first DEV_FRACTION of
   them (desenvolvimento) only choose the rule, the one with the best rate of wins among
   those with MIN_DEV_TRADES operations or more; the rest (avaliação) never chooses, it only
   measures the chosen rule. No operation of development uses a quote of the evaluation's
   time. The rule is validated only when the evaluation has MIN_EVAL_TRADES operations or
   more and the lower bound of the Wilson interval at 99% of its rate of wins is above the
   break-even rate of a 90% payout, 1 / 1.9, 52.6%.
5. Decision. CALL or PUT only with a validated rule, the direction that it gives at the last
   quote, a quote captured at most DECISION_WINDOW_SECONDS before now and a payout of
   ELIGIBLE_PAYOUT% or more, the condition of a first entry. Otherwise AGUARDAR, with the
   reason. A decision is valid only until DECISION_WINDOW_SECONDS after the capture of its
   quote: a moment that was missed never becomes a late order.

No rate of wins is shown before MIN_EVAL_TRADES operations of evaluation, and no
confidence is made up: a rate is always the one measured on the evaluation, with its
count and its interval. The validation is computed again as data comes in, and repeating a
test raises the chance of a false validation; the 99% interval and the 200 operations
limit that chance, without removing it. Past results on recorded quotes do not guarantee
future results, and a payout is not a chance of winning. With the progression of R$ 5, 10,
20 and 40, each operation keeps the sign of its expected value: below the break-even rate,
a rule loses on average, with or without gales.
"""

import argparse
import bisect
import csv
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_HORIZON = 300.0  # seconds of the hypothetical operation
MIN_HORIZON, MAX_HORIZON = 30.0, 3600.0
LABEL_TOLERANCE_FRACTION = 0.1  # how far from the wanted moment a quote may be, as a share of H
MIN_LABEL_TOLERANCE = 2.0  # seconds
DEV_FRACTION = 0.6  # the first 60% of the decisions choose the rule; the rest measure it
MIN_DEV_TRADES = 50
MIN_EVAL_TRADES = 200
Z = 2.576  # two-sided 99% Wilson interval
ELIGIBLE_PAYOUT = 90.0  # percent: the minimum payout of a first entry
DECISION_WINDOW_SECONDS = 5.0  # a decision is valid until this long after its quote's capture

CALL, PUT, WAIT = "CALL", "PUT", "AGUARDAR"
WIN, LOSS, DRAW = "WIN", "LOSS", "DRAW"
TREND, REVERSAL = "tendência", "reversão"

SCAN_PREFIX = "scan-"
# The quotes of the cycle's asset read on the screen during cycles of the simulated
# operation: real market quotes, with the scan's columns below plus ORIGIN_COLUMN.
CYCLE_PREFIX = "cotacoes-ciclo-"
# The scan's columns that the analysis reads; tests check them against scan.SCAN_COLUMNS.
COLUMNS = ("asset", "price", "price_captured_at_utc", "payout_percent", "analysis_ready")
READY = "sim"
# Where a quote came from. A line of a cycle file is read only with ORIGIN_CYCLE in it.
ORIGIN_COLUMN = "origin"
ORIGIN_SCAN = "varredura"  # a visit of the favorites scan
ORIGIN_CYCLE = "ciclo"  # a reading of the cycle's asset during a cycle of the simulated operation
ORIGIN_LABELS = {
    ORIGIN_SCAN: "de visitas da varredura",
    ORIGIN_CYCLE: "de leituras da tela durante ciclos da operação simulada",
}

# The scan's PAUSA (scan.Pause), applied when the history is loaded. A scan file holds one
# execution: the scan writes a new file in each run and never writes to an older one, with the
# lines in the order of the visits. In each file, the line of the first switch that the asset
# field did not confirm and every line after it are left out of the analysis: from that switch
# on, the chart may be another asset's while the asset's name is right, and nothing in the files
# tells it. Those lines are potentially inconsistent, not necessarily wrong, and the files are
# only read, never changed.
SWITCH_COLUMN, CYCLE_COLUMN, TIME_COLUMN = "switch", "cycle", "captured_at_utc"
NOT_CONFIRMED = "não confirmada"  # scan.NOT_CONFIRMED; the tests check the values against the scan's
SWITCH_VALUES = ("já selecionado", "confirmada", NOT_CONFIRMED, "não clicado", "em espera")
# The reasons of the lines with data for analysis that the PAUSA leaves out; the others keep theirs.
POTENTIAL = "linhas com dados para análise, potencialmente inconsistentes, a partir de uma troca não confirmada"
UNDETERMINED = "linhas com dados para análise, de execução indeterminada"

EXIT_OK = 0
EXIT_FAILED = 1

DISCLAIMER = (
    "Sem ordens e sem IA paga: regras fixas, calculadas neste computador. Resultados passados"
    " não garantem resultados futuros, e payout não é probabilidade de vitória."
)


@dataclass(frozen=True)
class Rule:
    """A fixed rule: the direction of the move over lookback_factor horizons, or its opposite."""

    kind: str  # TREND or REVERSAL
    lookback_factor: int

    def describe(self, horizon):
        return f"{self.kind} de {seconds_text(horizon * self.lookback_factor)} s"


RULES = (Rule(TREND, 1), Rule(TREND, 3), Rule(REVERSAL, 1), Rule(REVERSAL, 3))


@dataclass(frozen=True)
class Sample:
    """One usable quote: its capture time (this computer's clock, UTC), value and payout."""

    at: datetime
    price: float
    quote: str  # the quote as shown
    payout: object = None  # percent, or None
    origin: object = None  # ORIGIN_SCAN, ORIGIN_CYCLE, or None for a quote from no file


@dataclass
class Series:
    """The usable quotes of one asset, in time order."""

    name: str
    samples: list = field(default_factory=list)
    _times: list = field(default_factory=list, repr=False)

    def add(self, sample):
        """Insert sample in time order; False for a second quote at the same moment."""
        index = bisect.bisect_left(self._times, sample.at)
        if index < len(self._times) and self._times[index] == sample.at:
            return False
        self._times.insert(index, sample.at)
        self.samples.insert(index, sample)
        return True

    def first_at_or_after(self, moment, tolerance):
        """The first sample at or after moment, at most tolerance seconds later, or None."""
        index = bisect.bisect_left(self._times, moment)
        if index < len(self.samples) and (self._times[index] - moment).total_seconds() <= tolerance:
            return self.samples[index]
        return None

    def nearest(self, moment, tolerance):
        """The sample closest to moment, at most tolerance seconds away, or None."""
        index = bisect.bisect_left(self._times, moment)
        best = None
        for candidate in (index - 1, index):
            if 0 <= candidate < len(self.samples):
                gap = abs((self._times[candidate] - moment).total_seconds())
                if gap <= tolerance and (best is None or gap < best[0]):
                    best = (gap, self.samples[candidate])
        return best[1] if best else None

    def median_interval(self):
        gaps = sorted((b - a).total_seconds() for a, b in zip(self._times, self._times[1:]))
        return gaps[len(gaps) // 2] if gaps else None


@dataclass(frozen=True)
class Trade:
    """A hypothetical operation of a rule: when it was decided, its direction and its result."""

    at: datetime
    direction: str
    result: str


@dataclass(frozen=True)
class Evaluation:
    """Wins, losses and draws of a set of operations."""

    wins: int
    losses: int
    draws: int

    @property
    def trades(self):
        return self.wins + self.losses

    @property
    def rate(self):
        return self.wins / self.trades if self.trades else None

    def interval(self, z=Z):
        return wilson(self.wins, self.trades, z)


@dataclass(frozen=True)
class Validation:
    """The rule chosen on the development decisions and its measure on the evaluation ones."""

    validated: bool
    reason: str
    rule: object = None
    development: object = None  # Evaluation
    evaluation: object = None  # Evaluation
    decisions: int = 0  # decision moments with a known result
    break_even: float = 0.0


@dataclass(frozen=True)
class Decision:
    """What the analysis says about one asset now: CALL, PUT or AGUARDAR, and why."""

    asset: str
    action: str
    reason: str
    validation: object = None
    sample: object = None  # the last usable quote
    valid_until: object = None  # for CALL and PUT: the end of the moment to enter

    def describe(self):
        text = f"{self.action} ({self.reason})"
        if self.valid_until is not None:
            text += f", válida até {self.valid_until.isoformat(timespec='seconds')} (relógio local, UTC)"
        return text


def seconds_text(value):
    """value with a decimal comma, as in 2,5."""
    return f"{value:g}".replace(".", ",")


def percent_text(fraction):
    return f"{fraction * 100:.1f}%".replace(".", ",")


def wilson(wins, trades, z=Z):
    """(low, high) of the Wilson score interval of wins / trades, or None without trades."""
    if trades <= 0:
        return None
    rate = wins / trades
    denominator = 1 + z * z / trades
    center = (rate + z * z / (2 * trades)) / denominator
    half = z * math.sqrt(rate * (1 - rate) / trades + z * z / (4 * trades * trades)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def break_even(payout_percent):
    """The rate of wins at which operations with this payout neither win nor lose on average."""
    return 1 / (1 + payout_percent / 100)


def tolerance(horizon):
    return max(MIN_LABEL_TOLERANCE, horizon * LABEL_TOLERANCE_FRACTION)


def outcome(series, sample, horizon, direction):
    """WIN, LOSS or DRAW of an operation in direction decided at sample, or None without a quote."""
    later = series.first_at_or_after(sample.at + timedelta(seconds=horizon), tolerance(horizon))
    if later is None:
        return None
    if later.price == sample.price:
        return DRAW
    return WIN if (later.price > sample.price) == (direction == CALL) else LOSS


def direction_of(rule, series, sample, horizon):
    """CALL, PUT or None: what rule says at sample, from quotes before it only."""
    earlier = series.nearest(sample.at - timedelta(seconds=horizon * rule.lookback_factor), tolerance(horizon))
    if earlier is None or earlier.at >= sample.at or earlier.price == sample.price:
        return None
    up = sample.price > earlier.price
    if rule.kind == REVERSAL:
        up = not up
    return CALL if up else PUT


def decision_moments(series, horizon):
    """The samples where an operation is decided: each at least horizon after the previous one."""
    moments, last = [], None
    for sample in series.samples:
        if last is None or (sample.at - last).total_seconds() >= horizon:
            moments.append(sample)
            last = sample.at
    return moments


def trades_of(rule, series, moments, horizon):
    trades = []
    for sample in moments:
        direction = direction_of(rule, series, sample, horizon)
        if direction is None:
            continue
        result = outcome(series, sample, horizon, direction)
        if result is not None:
            trades.append(Trade(sample.at, direction, result))
    return trades


def evaluate(trades):
    return Evaluation(
        sum(trade.result == WIN for trade in trades),
        sum(trade.result == LOSS for trade in trades),
        sum(trade.result == DRAW for trade in trades),
    )


def counts_text(evaluation):
    text = f"{evaluation.wins} acertos e {evaluation.losses} erros"
    return text + (f", {evaluation.draws} empates" if evaluation.draws else "")


def rate_text(evaluation):
    """The measured rate of an evaluation with MIN_EVAL_TRADES operations or more, and its interval."""
    low, high = evaluation.interval()
    return (
        f"{evaluation.trades} operações de avaliação ({counts_text(evaluation)}), acerto de"
        f" {percent_text(evaluation.rate)}, intervalo de 99% de {percent_text(low)} a {percent_text(high)}"
    )


def split(series, horizon):
    """(development, evaluation, labelled): the decision moments, split in time.

    labelled are the moments whose operation has a known result. The first DEV_FRACTION of
    them are development, but only those whose result was known before the first moment of
    evaluation: no operation of development uses a quote of the evaluation's time.
    """
    moments = decision_moments(series, horizon)
    labelled = [sample for sample in moments if outcome(series, sample, horizon, CALL) is not None]
    if not labelled:
        return [], [], []
    cut = labelled[min(len(labelled) - 1, int(len(labelled) * DEV_FRACTION))].at
    end = timedelta(seconds=horizon + tolerance(horizon))
    development = [sample for sample in moments if sample.at + end <= cut]
    evaluation = [sample for sample in moments if sample.at >= cut]
    return development, evaluation, labelled


def validate(series, horizon, payout=ELIGIBLE_PAYOUT):
    """The Validation of RULES on series: chosen on development, measured on evaluation."""
    threshold = break_even(payout)
    development, measured, labelled = split(series, horizon)
    if not labelled:
        return Validation(
            False,
            f"dados insuficientes: nenhuma operação hipotética de {seconds_text(horizon)} s tem resultado"
            " conhecido ainda",
            break_even=threshold,
        )
    results = [(rule, evaluate(trades_of(rule, series, development, horizon))) for rule in RULES]
    eligible = [(rule, result) for rule, result in results if result.trades >= MIN_DEV_TRADES]
    if not eligible:
        best = max(result.trades for _, result in results)
        return Validation(
            False,
            f"dados insuficientes: no máximo {best} de {MIN_DEV_TRADES} operações por regra para"
            f" escolher uma, e depois {MIN_EVAL_TRADES} de avaliação em dados separados",
            decisions=len(labelled),
            break_even=threshold,
        )
    rule, chosen = max(eligible, key=lambda item: item[1].rate)  # the first of RULES on a tie
    if chosen.rate <= threshold:
        return Validation(
            False,
            f"nenhuma das {len(RULES)} regras passou de {percent_text(threshold)} no desenvolvimento"
            f" (a melhor, {rule.describe(horizon)}: {counts_text(chosen)})",
            rule,
            chosen,
            decisions=len(labelled),
            break_even=threshold,
        )
    evaluation = evaluate(trades_of(rule, series, measured, horizon))
    if evaluation.trades < MIN_EVAL_TRADES:
        return Validation(
            False,
            f"regra {rule.describe(horizon)} em avaliação: {evaluation.trades} de {MIN_EVAL_TRADES}"
            " operações em dados separados; sem elas, nenhuma taxa de acerto é mostrada",
            rule,
            chosen,
            evaluation,
            len(labelled),
            threshold,
        )
    low, _ = evaluation.interval()
    if low <= threshold:
        return Validation(
            False,
            f"regra {rule.describe(horizon)} não validada: {rate_text(evaluation)}; o limite inferior"
            f" precisa passar de {percent_text(threshold)}",
            rule,
            chosen,
            evaluation,
            len(labelled),
            threshold,
        )
    return Validation(
        True,
        f"regra {rule.describe(horizon)} validada: {rate_text(evaluation)}, acima de {percent_text(threshold)}",
        rule,
        chosen,
        evaluation,
        len(labelled),
        threshold,
    )


def decide(series, horizon, now):
    """The Decision for series at now (this computer's clock, UTC)."""
    if not series.samples:
        return Decision(series.name, WAIT, "sem cotações utilizáveis")
    last = series.samples[-1]
    validation = validate(series, horizon)
    if not validation.validated:
        return Decision(series.name, WAIT, validation.reason, validation, last)
    age = (now - last.at).total_seconds()
    if not 0 <= age <= DECISION_WINDOW_SECONDS:
        return Decision(
            series.name,
            WAIT,
            f"{validation.reason}; mas a última cotação é de há {seconds_text(round(age, 1))} s, e uma"
            f" decisão só vale até {seconds_text(DECISION_WINDOW_SECONDS)} s depois da captura",
            validation,
            last,
        )
    if last.payout is None or last.payout < ELIGIBLE_PAYOUT:
        shown = "indisponível" if last.payout is None else f"{seconds_text(last.payout)}%"
        return Decision(
            series.name,
            WAIT,
            f"{validation.reason}; mas o payout {shown} não permite a primeira entrada, que exige"
            f" {seconds_text(ELIGIBLE_PAYOUT)}% ou mais",
            validation,
            last,
        )
    direction = direction_of(validation.rule, series, last, horizon)
    if direction is None:
        return Decision(series.name, WAIT, f"{validation.reason}; mas a regra não dá direção agora", validation, last)
    return Decision(
        series.name, direction, validation.reason, validation, last,
        last.at + timedelta(seconds=DECISION_WINDOW_SECONDS),
    )


def asset_key(name):
    """name in capitals with letters and digits only, as the scan compares assets."""
    return "".join(char for char in name.upper() if char.isalnum())


def quote_number(text):
    """The number of a quote such as 1.42582, 1,42582 or 65,432.10; None if it is not one."""
    separators = [index for index, char in enumerate(text) if char in ".,"]
    if not separators:
        return None
    last = separators[-1]
    digits = "".join(char for char in text[:last] if char.isdigit()) + "." + text[last + 1:]
    try:
        return float(digits)
    except ValueError:
        return None


def payout_number(text):
    try:
        return float(text.replace(",", "."))
    except (AttributeError, ValueError):
        return None


def sample_of(row, origin=ORIGIN_SCAN):
    """(asset, Sample) of a CSV line usable for analysis, or (None, why it is skipped).

    A line of a cycle file (origin ORIGIN_CYCLE) must also say that origin itself.
    """
    if origin != ORIGIN_SCAN and row.get(ORIGIN_COLUMN) != origin:
        return None, "linhas de outra origem, que não é cotação lida na tela"
    if row.get("analysis_ready") != READY:
        return None, "linhas sem dados para análise"
    name = " ".join((row.get("asset") or "").split())
    price = quote_number(row.get("price") or "")
    if not name or price is None:
        return None, "linhas sem ativo ou sem cotação numérica"
    try:
        at = datetime.fromisoformat(row.get("price_captured_at_utc") or "")
    except ValueError:
        return None, "linhas sem o horário de captura da cotação"
    if at.tzinfo is None:
        return None, "linhas com horário sem fuso"
    payout = payout_number(row.get("payout_percent") or "")
    return name, Sample(at.astimezone(timezone.utc), price, row["price"], payout, origin)


@dataclass
class History:
    """The usable quotes by asset key, and what was skipped, with counts.

    origins counts the usable quotes by origin, and cycle_files the cycle files read, also
    counted in files.
    """

    series: dict = field(default_factory=dict)
    files: int = 0
    usable: int = 0
    skipped: dict = field(default_factory=dict)  # reason -> count
    origins: dict = field(default_factory=dict)  # origin -> usable quotes
    cycle_files: int = 0
    left_out: list = field(default_factory=list)  # LeftOut of each file with lines left out by the PAUSA

    def skip(self, reason, count=1):
        if count:
            self.skipped[reason] = self.skipped.get(reason, 0) + count

    def add(self, name, sample):
        """Add sample of asset name; False for a second quote of the asset at the same moment."""
        series = self.series.setdefault(asset_key(name), Series(name))
        if series.add(sample):
            self.usable += 1
            self.origins[sample.origin] = self.origins.get(sample.origin, 0) + 1
            return True
        self.skip("cotações repetidas no mesmo instante")
        return False

    def get(self, name):
        return self.series.get(asset_key(name))


@dataclass
class LeftOut:
    """The lines of one file left out of the analysis by the PAUSA, and where and why they start."""

    path: Path
    detail: str  # where they start and why, as in "a partir da linha 12 (ciclo 3, troca não confirmada ...)"
    undetermined: bool = False  # the files cannot tell whether a switch not confirmed came before them
    cycle_file: bool = False  # lines of a file of quotes read during cycles
    lines: int = 0  # lines from where they start; in a cycle file, only the quotes left out
    usable: int = 0  # of them, those that the other criteria would have used: the ones left out


@dataclass(frozen=True)
class Execution:
    """One scan file: when its lines were recorded, and the line from which nothing of it is used."""

    path: Path
    first: object = None  # the earliest captured_at_utc of its lines, or None
    last: object = None  # the latest one
    paused: object = None  # the captured_at_utc of the line from which nothing is used, or None
    line: int = 0  # that line of the file, counting the header as line 1
    cycle: object = None  # its cycle
    undetermined: bool = False  # from that line on, the execution cannot be told


class Timeline:
    """The executions of the scan in time: which one a quote read during a cycle belongs to.

    One scan runs at a time (runtime/scan.lock), and the quotes of a cycle are read between the
    visits of its execution, before its first visit (an entry left pending by the last run) or
    after its last one, never during a visit. So a quote read between the first and the last
    line of a scan file is of that execution, and it is left out from that execution's PAUSA on.
    One read after the last line of an execution that had reached its PAUSA may be of it or of
    the next one: it is undetermined, and so is one read outside the files' times when a scan
    file could not be read. Files whose times overlap, which one scan at a time never writes,
    are taken together: from the first PAUSA among them on, a quote read during them is
    undetermined.
    """

    def __init__(self, executions, complete=True):
        self.groups = []  # [first, last, executions], with the executions whose times overlap together
        for item in sorted((item for item in executions if item.first is not None), key=lambda item: item.first):
            if self.groups and item.first <= self.groups[-1][1]:
                group = self.groups[-1]
                group[1] = max(group[1], item.last)
                group[2].append(item)
            else:
                self.groups.append([item.first, item.last, [item]])
        self.firsts = [group[0] for group in self.groups]
        self.complete = complete  # every scan file could be read, each with the times of its lines

    def reason(self, moment):
        """None when a quote read at moment may be used; otherwise (detail, undetermined)."""
        index = bisect.bisect_right(self.firsts, moment) - 1
        if index >= 0:
            _, last, members = self.groups[index]
            paused = [item for item in members if item.paused is not None]
            if moment <= last:  # during the executions of the group
                if not paused or moment < min(item.paused for item in paused):
                    return None
                if len(members) > 1:
                    names = " e ".join(item.path.name for item in members)
                    earliest = min(paused, key=lambda item: item.paused)
                    return (
                        f"num horário em que {names} se sobrepõem, com {mark(earliest)} em {earliest.path.name} antes"
                        " dela, sem como determinar a execução"
                    ), True
                item = members[0]
                if item.undetermined:
                    return f"durante {item.path.name}, depois da linha {item.line}, de onde a execução é indeterminada", True
                return f"durante {item.path.name}, depois da troca não confirmada da linha {item.line} (ciclo {item.cycle})", False
            if paused:  # after the last line of an execution that had reached its PAUSA
                prior = max(paused, key=lambda item: item.last)
                if index + 1 < len(self.groups):
                    return (
                        f"entre o fim de {prior.path.name}, que já tinha {mark(prior)}, e o começo da execução seguinte,"
                        " sem como determinar de qual das duas"
                    ), True
                return (
                    f"depois do fim de {prior.path.name}, que já tinha {mark(prior)}, sem como determinar se é dela ou"
                    " de uma execução seguinte"
                ), True
        if not self.complete:
            return (
                "fora dos horários dos arquivos da varredura lidos, com um arquivo da varredura que não pôde ser lido"
                " ou sem horários, sem como determinar a execução"
            ), True
        return None


def mark(execution):
    """Where the execution stops being used, in a few words."""
    if execution.undetermined:
        return f"execução indeterminada a partir da linha {execution.line}"
    return f"troca não confirmada na linha {execution.line} (ciclo {execution.cycle})"


def scan_files(runtime_dir):
    return sorted(path for path in Path(runtime_dir).glob(f"{SCAN_PREFIX}*.csv") if path.is_file())


def cycle_files(runtime_dir):
    return sorted(path for path in Path(runtime_dir).glob(f"{CYCLE_PREFIX}*.csv") if path.is_file())


def load_history(runtime_dir, exclude=()):
    """The History of every scan CSV and cycle file in runtime_dir, except the paths in exclude.

    A file that cannot be read is skipped and counted; so are the lines of a file without
    the analysis_ready column, which does not tell whether its data was complete and current,
    and the lines of a cycle file without the origin of a reading of the screen. A quote at
    the moment of one already loaded for the same asset counts once.

    The PAUSA of the scan applies to every file (uncertainty()): in a scan file, the lines from
    the first switch not confirmed on are left out, potentially inconsistent; so are the quotes
    of the cycles read during that execution after it. Lines whose execution the files cannot
    tell are left out too, as undetermined. Each file with lines left out gets a LeftOut in
    history.left_out. The files are only read.
    """
    history = History()
    excluded = {Path(path).resolve() for path in exclude}
    executions, complete = [], True
    for path in scan_files(runtime_dir):
        if path.resolve() in excluded:
            continue
        table = read_table(path)
        if table is None:
            history.skip("arquivos que não puderam ser lidos")
            complete = False
            continue
        history.files += 1
        columns, rows = table
        if any(name not in columns for name in COLUMNS):
            history.skip("linhas de arquivos anteriores à coluna analysis_ready", len(rows))
            continue
        execution = load_scan_file(path, columns, rows, history)
        executions.append(execution)
        complete = complete and (execution.first is not None or not rows)  # without times, no span to place
    timeline = Timeline(executions, complete)
    for path in cycle_files(runtime_dir):
        if path.resolve() in excluded:
            continue
        table = read_table(path)
        if table is None:
            history.skip("arquivos que não puderam ser lidos")
            continue
        history.files += 1
        history.cycle_files += 1
        columns, rows = table
        if any(name not in columns for name in COLUMNS):
            history.skip("linhas de arquivos anteriores à coluna analysis_ready", len(rows))
            continue
        load_cycle_file(path, rows, history, timeline)
    return history


def read_table(path):
    """(columns, rows) of a CSV file, only read; None when it cannot be read."""
    try:
        with open(path, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            return tuple(reader.fieldnames or ()), rows
    except (OSError, UnicodeDecodeError, csv.Error):
        return None


def uncertainty(columns, rows):
    """(index, detail, undetermined) of the first line of a scan file not to be used, or None.

    From the first switch not confirmed on, the chart may be another asset's: the lines are
    potentially inconsistent. A file without the switch or the cycle column, a value of switch
    that the scan never writes, as in a cut last line, and a cycle that is not a number or is
    lower than the one before, as two executions in one file would be, leave the lines from
    there on of an undetermined execution. Lines are counted from the header, line 1, as in a
    spreadsheet.
    """
    if SWITCH_COLUMN not in columns or CYCLE_COLUMN not in columns:
        return 0, "a partir da linha 2: o arquivo não tem as colunas switch e cycle, que mostram as trocas e a execução", True
    previous = None
    for index, row in enumerate(rows):
        line = index + 2
        text = (row.get(CYCLE_COLUMN) or "").strip()
        if not (text.isascii() and text.isdigit()):
            return index, f"a partir da linha {line}: o ciclo {text!r} não é um número, e a execução não pode ser delimitada", True
        cycle = int(text)
        if previous is not None and cycle < previous:
            return index, (
                f"a partir da linha {line}: o ciclo {cycle} vem depois do ciclo {previous}, então o arquivo pode ter"
                " mais de uma execução"
            ), True
        previous = cycle
        switch = row.get(SWITCH_COLUMN)
        if switch == NOT_CONFIRMED:
            favorite = " ".join((row.get("favorite_text") or "").split())
            favorite = favorite if len(favorite) <= 60 else favorite[:60] + "..."
            which = f"o favorito {row.get('favorite_position') or '?'}" + (f", {favorite}" if favorite else "")
            return index, f"a partir da linha {line} (ciclo {cycle}, troca não confirmada para {which})", False
        if switch not in SWITCH_VALUES:
            shown = "" if switch is None else switch
            return index, (
                f"a partir da linha {line} (ciclo {cycle}): o valor {shown!r} da coluna switch não diz se a troca foi"
                " confirmada"
            ), True
    return None


def utc_moment(text):
    """The moment of a time with its time zone in the files, in UTC, or None."""
    try:
        moment = datetime.fromisoformat(text or "")
        return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def load_scan_file(path, columns, rows, history):
    """Add the usable lines of one scan file before its uncertainty to history; return its Execution."""
    found = uncertainty(columns, rows)
    end = len(rows) if found is None else found[0]
    for row in rows[:end]:
        add_row(history, row, ORIGIN_SCAN)
    times = [moment for moment in (utc_moment(row.get(TIME_COLUMN)) for row in rows) if moment is not None]
    first, last = (min(times), max(times)) if times else (None, None)
    if found is None:
        return Execution(path, first, last)
    index, detail, undetermined = found
    usable = 0
    for row in rows[index:]:  # the lines without data for analysis keep their own reason
        name, reason = sample_of(row)
        if name is None:
            history.skip(reason)
        else:
            usable += 1
    history.left_out.append(LeftOut(path, detail, undetermined, False, len(rows) - index, usable))
    history.skip(UNDETERMINED if undetermined else POTENTIAL, usable)
    later = [moment for moment in (utc_moment(row.get(TIME_COLUMN)) for row in rows[index:]) if moment is not None]
    paused = min(later) if later else last
    cycle = (rows[index].get(CYCLE_COLUMN) or "").strip() if index < len(rows) else ""
    return Execution(path, first, last, paused, index + 2, cycle or "?", undetermined)


def load_cycle_file(path, rows, history, timeline):
    """Add the usable quotes of one cycle file to history, except those that the PAUSA leaves out."""
    groups = {}
    for row in rows:
        name, sample = sample_of(row, ORIGIN_CYCLE)
        if name is None:
            history.skip(sample)
            continue
        found = timeline.reason(sample.at)
        if found is None:
            history.add(name, sample)
            continue
        left = groups.get(found)
        if left is None:
            left = groups[found] = LeftOut(path, found[0], found[1], True)
            history.left_out.append(left)
        left.lines += 1
        left.usable += 1
        history.skip(UNDETERMINED if found[1] else POTENTIAL)


def add_row(history, row, origin):
    name, sample = sample_of(row, origin)
    if name is None:
        history.skip(sample)
    else:
        history.add(name, sample)


class Analyzer:
    """The analysis during a scan: the earlier history plus each usable visit as it ends."""

    def __init__(self, horizon, history=None):
        self.horizon = horizon
        self.history = history if history is not None else History()

    def add(self, name, sample):
        self.history.add(name, sample)

    def decide(self, name, now):
        series = self.history.get(name)
        if series is None:
            return Decision(name, WAIT, "sem cotações utilizáveis")
        return decide(series, self.horizon, now)

    def summary(self):
        """(assets, validated) over every asset with usable quotes."""
        validated = [
            series.name for series in self.history.series.values() if validate(series, self.horizon).validated
        ]
        return len(self.history.series), validated


def horizon_seconds(text):
    """argparse type of --horizon."""
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and MIN_HORIZON <= value <= MAX_HORIZON):
        raise argparse.ArgumentTypeError(
            f"precisa ser um número de segundos de {MIN_HORIZON:g} a {MAX_HORIZON:g}, não {text!r}"
        )
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.analysis",
        description=(
            "Análise local CALL/PUT das cotações gravadas pela varredura: regras fixas,"
            " validadas só em dados separados. Mostra AGUARDAR quando faltam dados ou"
            " validação. Não envia ordens e não usa IA paga."
        ),
    )
    parser.add_argument(
        "--horizon",
        type=horizon_seconds,
        default=DEFAULT_HORIZON,
        help=f"segundos de cada operação hipotética (padrão {DEFAULT_HORIZON:g}, de {MIN_HORIZON:g} a"
        f" {MAX_HORIZON:g})",
    )
    parser.add_argument(
        "--runtime",
        default="runtime",
        help="pasta dos CSV da varredura e das cotações lidas durante os ciclos (padrão runtime,"
        " relativa à pasta atual)",
    )
    return parser


def origins_text(history):
    """How many usable quotes came from each origin, as in "30 de visitas da varredura"."""
    return " e ".join(
        f"{history.origins[origin]} {label}" for origin, label in ORIGIN_LABELS.items() if history.origins.get(origin)
    )


def data_line(history):
    """The line about the files and the quotes of the history, with their origins."""
    files = f"{history.files - history.cycle_files} arquivos da varredura"
    if history.cycle_files:
        files += f" e {history.cycle_files} de leituras durante ciclos"
    line = f"Dados: {files}, {history.usable} cotações utilizáveis em {len(history.series)} ativos."
    if history.origins.get(ORIGIN_CYCLE):
        line += f" Origem: {origins_text(history)}; resultados do simulador nunca entram."
    return line


def plural(number, one, many):
    return f"{number} {one if number == 1 else many}"


def left_out_lines(history, everything=True):
    """The terminal lines about what the PAUSA left out, file by file; none without it.

    everything=False lists only the files with lines for analysis left out, and counts the
    others in one line, as the start of a scan does.
    """
    if not history.left_out:
        return []
    lines = [
        "Fora da análise, sem mudar nenhum arquivo: em cada execução da varredura, a linha da primeira troca não"
        " confirmada e as seguintes, porque depois dela o gráfico pode ser de outro ativo sem que o nome do ativo"
        " mostre isso. As linhas com dados para análise excluídas por isso são potencialmente inconsistentes, o que"
        " não quer dizer que todas tinham cotação errada. Também ficam fora as de execução indeterminada, em que os"
        " arquivos não permitem saber se houve uma troca não confirmada antes delas na mesma execução. Por arquivo:"
    ]
    quiet = 0
    for item in history.left_out:
        one = item.usable == 1
        if item.undetermined:
            kind = "de execução indeterminada"
        else:
            kind = "potencialmente inconsistente" if one else "potencialmente inconsistentes"
        if item.cycle_file:
            text = (
                f"{plural(item.usable, 'cotação', 'cotações')} {'excluída' if one else 'excluídas'}, {kind},"
                f" {'lida' if one else 'lidas'} {item.detail}"
            )
        elif item.usable:
            text = (
                f"{item.detail}: {plural(item.usable, 'linha', 'linhas')} com dados para análise"
                f" {'excluída' if one else 'excluídas'}, {kind}, de {plural(item.lines, 'linha', 'linhas')} desde ali"
            )
        elif not everything:
            quiet += 1
            continue
        else:
            text = f"{item.detail}: nenhuma linha com dados para análise a excluir, de {plural(item.lines, 'linha', 'linhas')} desde ali"
        lines.append(f"  {item.path.name}{':' if item.cycle_file else ','} {text}.")
    if quiet:
        lines.append(
            f"  Mais {plural(quiet, 'arquivo', 'arquivos')} com troca não confirmada ou execução indeterminada, sem"
            " nenhuma linha com dados para análise a excluir; a lista completa está no analisar_windows.cmd."
        )
    return lines


def report_lines(history, horizon, now):
    """The terminal lines of an analysis of the whole history, at now."""
    lines = [
        f"ANÁLISE LOCAL CALL/PUT, com operações hipotéticas de {seconds_text(horizon)} s. {DISCLAIMER}",
        data_line(history),
    ]
    for reason, count in sorted(history.skipped.items()):
        lines.append(f"  Ignoradas: {count} {reason}.")
    lines += left_out_lines(history)
    if not history.series:
        lines.append(
            f"{WAIT}: sem cotações utilizáveis. Rode a varredura (varrer_windows.cmd) para juntar dados;"
            " só as linhas com dados para análise contam."
        )
        return lines
    for series in sorted(history.series.values(), key=lambda item: item.name):
        interval = series.median_interval()
        data = f"{len(series.samples)} cotações"
        if interval is not None:
            data += f", uma a cada {seconds_text(round(interval))} s (mediana)"
        data += f", de {series.samples[0].at.isoformat(timespec='seconds')} a {series.samples[-1].at.isoformat(timespec='seconds')}"
        lines.append(f"  {series.name}: {data}")
        lines.append(f"    agora: {decide(series, horizon, now).describe()}")
    return lines


def main(argv=None, *, now=None):
    """Run the analysis of the quotes in the runtime folder and print it; the exit code is 0 or 1."""
    args = build_parser().parse_args(argv)
    moment = now() if now is not None else datetime.now(timezone.utc)
    try:
        history = load_history(args.runtime)
    except OSError as error:
        print(f"PARADA: não foi possível ler {args.runtime} ({error})", file=sys.stderr)
        return EXIT_FAILED
    for line in report_lines(history, args.horizon, moment):
        print(line, flush=True)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
