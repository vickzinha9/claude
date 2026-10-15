"""Automatic scan of the favorite assets of the Pocket Option: python -m pocketautonomo.scan.

NO ORDERS. The scan opens the trading screen in the same visible Chromium and browser
profile as pocketautonomo.observe, waits for your manual login and then goes through your
favorite assets, one at a time: it selects each one, confirms the selected asset, waits for
its data and reads the asset, the quote and the payout with observe's reader and with the
sources that you checked for observe, in runtime/observe-selectors.json.

Its only action on the page is a click on a favorite asset, to select it.
ScanSession.click_favorite is the only call in the program that clicks, and it clicks only
an element of the favorites selector that you checked, right after reading that element
again and finding the favorite it expects there. The scan never clicks BUY, SELL, CALL or
PUT, never types, never touches the Amount field and sends no order. A favorites selector
that also finds an element with a word of those buttons or of money, or a form field such
as the Amount, is refused as a whole, and so is one that finds more than MAX_FAVORITES
elements. Before each click, the page must still be on pocketoption.com.

No selector ships with the program. The favorites selector, kept in
runtime/scan-selectors.json, comes from --configure, which reads it right away and lists
every element it finds, so that you compare the list with the favorites on the screen
before any click.

Mixing two assets is the main risk: after a click, the asset's name, its payout and the
chart change at different moments. So each switch is first confirmed by the asset field.
Then the readings wait SETTLE_SECONDS, and each reading checks the asset before and after
reading the quote and the payout. A field is kept only when two readings in a row agree:
the same asset, the same payout, and quotes less than QUOTE_JUMP apart. After a switch, the
quote must also be farther than PREVIOUS_QUOTE_GAP from the quote read on the previous
asset's chart right before the click; closer than that, the chart may still be the previous
asset's, and the quote waits. Without that earlier quote, nothing tells the two charts
apart, and the quote is unavailable. The payout is read after the quote, so a payout read
once the chart has changed comes after the change of asset. What does not settle within
--load-timeout seconds of the click is unavailable, with the reason. Only one navigation
runs at a time: the next click waits for the end of the current visit, and runtime/scan.lock
keeps a second scan from starting.

Each visit is written as soon as it ends to a new CSV file in runtime/, one line per
favorite per cycle, with observe's columns and the scan's own. payout_eligible is sim when
the numeric payout is ELIGIBLE_PAYOUT% or more, the criterion for a future first entry: it
is not a chance of winning, and the assets below it are recorded too. analysis_ready tells
apart, whatever the payout, the visits whose data is complete and current, the only ones
usable for analysis. Times come from this computer's clock, in UTC; none of them is the
server's. Every reading of the quote also goes, with the time of each step, to a timings
file next to the CSV (quote_timings).

After each visit, the local analysis (pocketautonomo.analysis) receives the visit's quote
when its data is usable, together with the usable quotes of the earlier scan files, and
says CALL, PUT or AGUARDAR, with the reason, in the terminal and in the CSV. Without a rule
validated on separate data, it says AGUARDAR. The scan sends no order whatever it says.

Exit codes: 0 when at least one line with data was written, 1 when the scan failed or wrote
none, 2 for invalid arguments or configuration, including a missing Playwright, and 130
after Ctrl+C.
"""

import argparse
import csv
import errno
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from pocketautonomo import analysis, observe, quote_timings
from pocketautonomo.observe import (
    COMPLETE,
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    FIELDS,
    LABELS,
    UNAVAILABLE,
    BrowserError,
    ConfigError,
    ObserverStopped,
    QuoteOcrSource,
    ReadError,
    ask,
    count,
    describe,
    preview,
    say,
    unavailable,
    utc_text,
    warn,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SCAN_SELECTORS_FILE_NAME = "scan-selectors.json"
LOCK_FILE_NAME = "scan.lock"
FAVORITES_KEY = "favorites"

ELIGIBLE_PAYOUT = 90  # percent: eligibility for a future first entry, not a chance of winning
DEFAULT_INTERVAL = 10.0  # seconds between the end of a cycle and the start of the next
MIN_INTERVAL = 1.0
MAX_PAUSE = 120.0  # the longest pause after cycles without any complete reading
DEFAULT_LOAD_TIMEOUT = 20.0  # seconds after the click to confirm the asset and settle its data
MIN_LOAD_TIMEOUT, MAX_LOAD_TIMEOUT = 5.0, 120.0
ASSET_POLL_SECONDS = 0.25  # between two readings of the asset while the switch is confirmed
SETTLE_SECONDS = 1.0  # after the switch is confirmed, before the first reading of its data
POLL_SECONDS = 0.5  # between two readings of the data
QUOTE_JUMP = 0.01  # two readings in a row of one asset never move 1% apart
PREVIOUS_QUOTE_GAP = 0.001  # closer than 0.1% to the previous asset's quote: maybe its chart
# The quote of the previous asset, read before the click: a few short attempts, all of them
# before the click, so on the previous asset's chart. No attempt starts later than
# REFERENCE_BUDGET_SECONDS after the first one.
REFERENCE_ATTEMPTS = 3
REFERENCE_RETRY_SECONDS = 0.3
REFERENCE_BUDGET_SECONDS = 2.5
# Data for analysis: a complete visit whose quote was captured at most this long before the
# visit was recorded. Older data is never presented as current.
MAX_DATA_AGE_SECONDS = 5.0
MAX_FAVORITES = 30
MAX_FAVORITE_WIDTH, MAX_FAVORITE_HEIGHT = 400, 120  # CSS pixels: a favorite is a small label
CLICK_TIMEOUT_MS = 5_000
REMOVED_RECHECK_SECONDS = 1.0
MAX_UNCONFIRMED = 3  # switches in a row without confirmation end the cycle, with no more clicks

# The value of the switch column.
ALREADY = "já selecionado"
CONFIRMED = "confirmada"
NOT_CONFIRMED = "não confirmada"
NOT_CLICKED = "não clicado"
# The value of the payout_eligible column.
ELIGIBLE, NOT_ELIGIBLE, UNKNOWN_ELIGIBILITY = "sim", "não", "indisponível"
# The value of the analysis_ready column: data complete and current, usable for analysis.
READY, NOT_READY = "sim", "não"
# The moments of the readings in the timings file.
TIMING_REFERENCE = "referência antes da troca"
TIMING_AFTER_SWITCH = "depois da troca"
TIMING_NO_SWITCH = "sem troca"
# The verdict column of the timings file: whether the scan's checks kept a quote it read.
ACCEPTED, REFUSED = quote_timings.ACCEPTED, quote_timings.REFUSED

SCAN_COLUMNS = observe.CSV_COLUMNS + (
    # The scan's own columns, after the 16 of observe's CSV.
    "cycle",  # 1, 2, ...
    "favorite_position",  # the favorite's position in the list, from 1
    "favorite_text",  # the favorite's text on the screen, as read
    "switch",  # já selecionado, confirmada, não confirmada or não clicado
    "previous_asset",  # the asset on the screen before the click
    "previous_quote",  # the quote read before the click: what the new one must differ from
    "switch_ms",  # from the click to the asset confirmed on the screen
    "load_ms",  # from the confirmed asset to the settled data, or to the time limit
    "polls",  # readings of the data in this visit
    "payout_eligible",  # sim: payout >= ELIGIBLE_PAYOUT%; não: below; indisponível: no payout
    # Added after the first validation of the scan; the columns above keep their order.
    "previous_quote_status",  # ok: <source>, or indisponível: <the original reason>
    "previous_quote_attempts",  # attempts to read the previous asset's quote before the click
    "previous_quote_ms",  # the time of those attempts, in milliseconds
    "analysis_ready",  # sim: complete and current data, usable for analysis; não otherwise
    "analysis_note",  # why the data is not usable for analysis, or what makes it usable
    "data_age_ms",  # from the capture of the quote to the recording of the visit
    "analysis_decision",  # CALL, PUT or AGUARDAR: what the local analysis said right after the visit
    "analysis_decision_note",  # its reason; a CALL or PUT also says until when it is valid
)

# Words of the trade buttons, of the Amount and of money operations, in English and in
# Portuguese. An element of the favorites selector with one of them refuses the selector.
FINANCIAL_WORDS = frozenset(
    {
        "buy", "sell", "call", "put", "compra", "comprar", "venda", "vender",
        "amount", "valor", "investimento", "investment", "investir",
        "deposit", "depositar", "depósito", "deposito", "withdraw", "withdrawal",
        "saque", "sacar", "retirar", "trade", "negociar", "operar",
    }
)
FORM_FIELDS = "input, textarea, select"
PERCENT_PATTERN = re.compile(r"[+\-]?\d{1,3}(?:[.,]\d+)?\s*%")
WORD_PATTERN = re.compile(r"[^\W\d_]+")
LOCK_HELD = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}

SCAN_NOTE = (
    "Gravado pelo modo --configure da varredura (no Windows, varrer_windows.cmd), que lista"
    " os favoritos encontrados antes de gravar. favorites é o seletor que encontra cada ativo"
    " favorito, e só eles, conferido por você na tela de operações. Chaves que começam com _"
    " são ignoradas."
)
INTRO = (
    "VARREDURA DOS FAVORITOS do PocketAutonomo: não envia ordens, não clica em BUY, SELL, CALL"
    " ou PUT e não mexe no Amount. O único clique é num ativo favorito, para selecioná-lo."
)
ELIGIBILITY_NOTE = (
    f"Elegível quer dizer payout de {ELIGIBLE_PAYOUT}% ou mais, o critério para uma futura"
    " primeira entrada. Não é probabilidade de vitória, e os ativos abaixo disso também são"
    " gravados."
)
HANDS_OFF = (
    "Durante a varredura, não use o mouse nem o teclado na janela do navegador: um clique seu"
    " troca o ativo, e o mouse sobre o gráfico cria a etiqueta do cursor."
)
FAVORITES_INTRO = (
    "A varredura passa pelos seus ativos favoritos, um de cada vez, e clica só no favorito,"
    " para selecioná-lo: nunca em BUY, SELL, CALL ou PUT, nem no Amount. Para achar o seletor:"
    " na janela do navegador, F12 abre o DevTools e Ctrl+Shift+C liga a inspeção. Clique num"
    " favorito e veja, no painel Elements, a tag e as classes dele. O seletor precisa"
    " encontrar todos os favoritos, e só eles: use a tag e a classe comuns a eles, sem"
    " :nth-child. O Copy selector do DevTools costuma dar o seletor de um elemento só."
)
CONFIGURATION_MOMENT = "durante a configuração dos favoritos; nada foi gravado"
NO_REFERENCE = "a cotação do ativo anterior não pôde ser lida antes da troca"
NO_PROOF = "sem ela, nada mostra que o gráfico já é o deste ativo"


def no_reference(reference):
    """Why a quote after a switch is unavailable when the previous asset's quote is missing.

    The original reason of each failed attempt stays in the message, so that the cause can
    be found and fixed; it is never replaced by a generic text.
    """
    if reference is None or not reference.reasons:
        return f"{NO_REFERENCE}; {NO_PROOF}"
    return f"{NO_REFERENCE} ({reference.reason}); {NO_PROOF}"


class ScanBusy(Exception):
    """Another scan already uses this runtime folder; exit code 1."""


class WindowClosed(Exception):
    """The browser window was closed: the scan ends with what it recorded."""


@dataclass(frozen=True)
class FavoriteList:
    """What the favorites selector found: how many elements, their texts and visibility."""

    total: int
    texts: tuple = ()
    visible: tuple = ()
    form_fields: int = 0  # input, textarea and select elements found or inside one found


@dataclass(frozen=True)
class FavoriteItem:
    """One element of the favorites selector, read right before a click."""

    text: str
    visible: bool
    width: float = 0.0  # CSS pixels
    height: float = 0.0
    form_fields: int = 0


@dataclass(frozen=True)
class Favorite:
    index: int  # 0-based, in the order of the selector
    text: str  # the element's text, whitespace collapsed
    name: object  # the asset that the text names, or None
    problem: object = None  # why it is skipped, or None

    @property
    def position(self):
        return self.index + 1

    @property
    def key(self):
        return asset_key(self.name) if self.name else None


@dataclass(frozen=True)
class Favorites:
    """The favorites found now. problem refuses them all; unsafe means it stops the scan."""

    selector: str
    total: int
    items: tuple = ()
    problem: object = None
    unsafe: bool = False

    @property
    def usable(self):
        return [] if self.problem else [item for item in self.items if item.problem is None]

    @property
    def keys(self):
        return {item.key for item in self.usable}


@dataclass(frozen=True)
class Poll:
    """One reading of a favorite's data: the asset before and after the quote and the payout."""

    captured_at: datetime  # this computer's clock, in UTC, at the start of the reading
    page: str
    asset_before: object  # FieldReading
    price: object
    payout: object
    asset_after: object
    duration_ms: float
    outside: bool = False  # the page was not on pocketoption.com: nothing was read


@dataclass(frozen=True)
class Reference:
    """The previous asset's quote, read on its chart before the click, or why it is missing.

    reasons keeps the original reason of every failed attempt, in order.
    """

    value: object  # the quote, or None
    status: str  # "ok: <source>" or "indisponível: <reason>"
    attempts: int
    ms: float  # the time of all the attempts
    reasons: tuple = ()

    @property
    def available(self):
        return self.value is not None

    @property
    def reason(self):
        """The reasons of the failed attempts, each said once, in order, after how many failed."""
        if not self.reasons:
            return None
        distinct = list(dict.fromkeys(self.reasons))
        return f"em {count(len(self.reasons), 'tentativa', 'tentativas')}: " + " / ".join(distinct)


@dataclass(frozen=True)
class Analysis:
    """Whether a visit's data is complete and current, so usable for analysis, and why."""

    ready: bool
    note: str
    age_ms: object = None  # from the capture of the quote to the recording of the visit


@dataclass(frozen=True)
class Visit:
    """One favorite in one cycle: how it was selected and what was read."""

    cycle: int
    favorite: Favorite
    total: int  # favorites in the list of that cycle
    switch: str  # ALREADY, CONFIRMED, NOT_CONFIRMED or NOT_CLICKED
    observation: object  # observe.Observation
    previous_asset: object = None
    previous_quote: object = None
    switch_ms: object = None
    load_ms: object = None
    polls: int = 0
    reference: object = None  # Reference, when the previous asset's quote was read for a click
    recorded_at: object = None  # this computer's clock, in UTC, when the visit ended
    decision: object = None  # analysis.Decision right after the visit, when the analysis runs

    @property
    def eligibility(self):
        return eligibility(self.observation.readings["payout"])

    @property
    def analysis(self):
        return analysis_of(self)


@dataclass
class AssetSummary:
    name: str
    visits: int = 0
    complete: int = 0
    ready: int = 0  # visits with data complete and current, usable for analysis
    last_ready: object = None  # the last such Visit: its quote and payout were read together
    last_visit: object = None  # the last Visit, whatever its data


@dataclass
class ScanProgress:
    cycles: int = 0
    visits: int = 0
    valid: int = 0
    complete: int = 0
    ready: int = 0
    clicks: int = 0
    csv: object = None  # ScanCsv, created with the first visit
    csv_path: object = None  # its path, chosen when the scan starts
    timings: object = None  # quote_timings.TimingRecorder of every reading of the quote
    assets: dict = field(default_factory=dict)  # asset key -> AssetSummary


@dataclass
class Scan:
    """What the visits share: the sources, the favorites selector, the limits and the progress."""

    selectors: dict
    favorites_selector: str
    load_timeout: float
    progress: ScanProgress
    last_clicked: object = None  # (key, how many elements the list had) of the last click
    analyzer: object = None  # analysis.Analyzer, from the start of the scan

    @property
    def sources(self):
        return {name: bool(self.selectors.get(name)) for name in FIELDS}


class ScanLock:
    """An exclusive lock on runtime/scan.lock: one scan at a time drives the browser.

    flock() on Linux and msvcrt.locking() on Windows. The operating system drops the lock
    when the file is closed or the process ends, even abruptly; the file may stay in
    runtime/, and only the lock counts.
    """

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in LOCK_HELD:
                raise ScanBusy(
                    f"outra varredura já está rodando com esta pasta (a trava {path} está em"
                    " uso); feche-a antes de começar outra"
                ) from error
            raise
        self._descriptor = descriptor

    def release(self):
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)


def main(argv=None, *, open_session=None, read_line=input, now=None, monotonic=None, engine=None):
    """Run the scan and return its exit code.

    The keyword arguments exist for tests. They replace open_scan_session, input, the UTC
    clock, time.perf_counter and the OCR's LazyEngine. Every wait goes through the session,
    page.wait_for_timeout in ScanSession, so a fake session brings a fake wait.
    """
    args = parse_arguments(argv)
    open_session = open_scan_session if open_session is None else open_session
    ctx = observe.Context(now=now, engine=engine)
    if monotonic is not None:
        ctx.monotonic = monotonic
    runtime_dir = ctx.runtime_dir
    profile_dir = runtime_dir / observe.PROFILE_DIR_NAME
    selectors_path = runtime_dir / observe.SELECTORS_FILE_NAME
    scan_path = runtime_dir / SCAN_SELECTORS_FILE_NAME
    progress = ScanProgress()
    lock = session = None
    interrupted = False
    try:
        say(INTRO)
        selectors = load_reader_selectors(selectors_path)
        favorites_selector, invalid = current_favorites_selector(scan_path, args.configure)
        if isinstance(selectors["price"], QuoteOcrSource):
            ctx.engine.get()  # a missing OCR stops here, before the browser opens
            observe.announce_ocr(ctx)
        lock = ScanLock(runtime_dir / LOCK_FILE_NAME)
        url = args.url
        if url is None:
            url = observe.ask_url(read_line)
        print_plan(url, args, profile_dir, selectors_path, scan_path)
        session = open_session(profile_dir)
        observe.open_page(session, url)
        wait_for_login(read_line, args.configure)
        if session.is_closed():
            raise ObserverStopped("a janela do navegador foi fechada antes da confirmação do login")
        if args.configure:
            favorites_selector = configure_favorites(
                session, scan_path, favorites_selector, invalid, selectors, read_line, ctx
            )
        say(HANDS_OFF)
        scan = Scan(selectors, favorites_selector, args.load_timeout, progress)
        run_scan(session, scan, args, ctx)
        return finish(progress)
    except ConfigError as error:
        warn(f"CONFIGURAÇÃO: {error}")
        return EXIT_CONFIG
    except (ObserverStopped, BrowserError, ScanBusy, OSError) as error:
        warn(f"PARADA: {error}")
        report(progress)
        return EXIT_FAILED
    except KeyboardInterrupt:
        interrupted = True
        say(f"Interrompido por Ctrl+C: {written_summary(progress)}.")
        report(progress)
        return EXIT_INTERRUPTED
    finally:
        if progress.csv is not None:
            progress.csv.close()
        if progress.timings is not None:
            progress.timings.close()
        observe.close_session(session, interrupted)
        if lock is not None:
            lock.release()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.scan",
        description=(
            "Varredura dos ativos favoritos da Pocket Option: seleciona cada favorito, confere"
            " o ativo, espera os dados e lê ativo, cotação do ativo e payout com as fontes do"
            " coletor. Não envia ordens e não clica em BUY, SELL, CALL ou PUT nem no Amount:"
            " o único clique é no favorito."
        ),
    )
    parser.add_argument(
        "--url",
        type=observe.official_url,
        help=f"link oficial da tela de operações, em https://{observe.ALLOWED_DOMAIN}; sem ele,"
        " o programa pede o link no terminal",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="depois do login, pede o seletor dos favoritos, mostra a lista encontrada e grava"
        f" {SCAN_SELECTORS_FILE_NAME} em runtime/ antes da varredura",
    )
    parser.add_argument(
        "--cycles",
        type=observe.positive_int,
        help="quantos ciclos fazer, cada um passando uma vez por todos os favoritos; sem esta"
        " opção, a varredura continua até Ctrl+C",
    )
    parser.add_argument(
        "--interval",
        type=seconds_from(MIN_INTERVAL, math.inf),
        default=DEFAULT_INTERVAL,
        help=f"segundos entre o fim de um ciclo e o começo do próximo (padrão {DEFAULT_INTERVAL:g},"
        f" mínimo {MIN_INTERVAL:g}); depois de um ciclo sem nenhuma leitura completa, a espera"
        f" dobra, até {MAX_PAUSE:g} s",
    )
    parser.add_argument(
        "--load-timeout",
        type=seconds_from(MIN_LOAD_TIMEOUT, MAX_LOAD_TIMEOUT),
        default=DEFAULT_LOAD_TIMEOUT,
        help="segundos, depois do clique, para confirmar o ativo e ter dados estáveis (padrão"
        f" {DEFAULT_LOAD_TIMEOUT:g}, de {MIN_LOAD_TIMEOUT:g} a {MAX_LOAD_TIMEOUT:g}); o que não"
        " estabilizar fica indisponível",
    )
    parser.add_argument(
        "--horizon",
        type=analysis.horizon_seconds,
        default=analysis.DEFAULT_HORIZON,
        help="segundos das operações hipotéticas da análise local CALL/PUT (padrão"
        f" {analysis.DEFAULT_HORIZON:g}, de {analysis.MIN_HORIZON:g} a {analysis.MAX_HORIZON:g}); a"
        " análise não envia ordens",
    )
    return parser


def parse_arguments(argv):
    return build_parser().parse_args(argv)


def seconds_from(low, high):
    """argparse type: a number of seconds from low to high."""

    def parse(text):
        try:
            value = float(text)
        except ValueError:
            value = math.nan
        if not (math.isfinite(value) and low <= value <= high):
            limit = f"a partir de {low:g}" if high == math.inf else f"de {low:g} a {high:g}"
            raise argparse.ArgumentTypeError(f"precisa ser um número de segundos {limit}, não {text!r}")
        return value

    return parse


def load_reader_selectors(path):
    """observe's sources; the asset's is required, because it confirms every switch."""
    selectors = observe.load_selectors(path)
    asset = selectors["asset"]
    if not asset:
        raise ConfigError(
            f"a varredura confirma cada troca pelo ativo, e {path} não tem o seletor do ativo."
            " Configure-o antes com observar_windows.cmd (python -m pocketautonomo.observe"
            " --configure)."
        )
    missing = [LABELS[name] for name in ("price", "payout") if not selectors[name]]
    if missing:
        warn(
            f"AVISO: {path} não tem fonte para {' e '.join(missing)}: esse campo fica"
            " indisponível em todas as linhas."
        )
    return selectors


def current_favorites_selector(path, configure):
    """(selector or None, whether the file is invalid); without --configure, it must exist."""
    try:
        selector = load_favorites_selector(path)
    except ConfigError as error:
        if not configure:
            raise
        warn(
            f"AVISO: {error}. O seletor dos favoritos será pedido do zero; o arquivo atual só"
            " muda quando você gravar o novo, e antes disso ele é guardado como cópia."
        )
        return None, True
    if selector is None and not configure:
        raise ConfigError(
            f"{path} não tem o seletor dos favoritos. Rode com --configure (no Windows,"
            " varrer_windows.cmd) para escolhê-lo com a lista do que ele encontra."
        )
    return selector, False


def load_favorites_selector(path):
    """The favorites selector in path, or None when the file or the selector is missing."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError(f"não foi possível ler {path}: {error}") from error
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ConfigError(f"{path} não é um JSON válido: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f'{path} precisa ser um objeto JSON, como {{"{FAVORITES_KEY}": "..."}}')
    unknown = sorted(key for key in data if key != FAVORITES_KEY and not key.startswith("_"))
    if unknown:
        raise ConfigError(f"{path} tem chave desconhecida ({', '.join(unknown)}); use só {FAVORITES_KEY}")
    value = data.get(FAVORITES_KEY)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path}: o valor de {FAVORITES_KEY} precisa ser texto")
    return (value or "").strip() or None


def save_favorites_selector(path, selector, replace_invalid):
    """Write the selector with json; an invalid file is first kept as a copy next to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if replace_invalid and path.exists():
        suffix = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        copy = path.with_name(f"scan-selectors.invalido-{suffix}.json")
        os.replace(path, copy)
        say(f"O arquivo inválido foi guardado como {copy}.")
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump({"_leia_me": SCAN_NOTE, FAVORITES_KEY: selector}, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)


def print_plan(url, args, profile_dir, selectors_path, scan_path):
    say(f"Página: {observe.display_url(url)} (parâmetros e fragmento da URL não são exibidos)")
    say(f"Perfil do navegador: {profile_dir}, o mesmo do coletor (guarda a sua sessão; não compartilhe)")
    say(f"Fontes de ativo, cotação e payout: {selectors_path}; seletor dos favoritos: {scan_path}")
    plan = "até Ctrl+C" if args.cycles is None else count(args.cycles, "ciclo", "ciclos")
    say(
        f"Varredura: {plan}, com {seconds_text(args.interval)} s entre um ciclo e o próximo e até"
        f" {seconds_text(args.load_timeout)} s por favorito para confirmar a troca e ter dados estáveis."
        " Horários pelo relógio deste computador, em UTC; não são o horário do servidor da"
        " Pocket Option."
    )
    say(ELIGIBILITY_NOTE)


def wait_for_login(read_line, configure):
    say(
        "Faça o login você mesmo, na janela do navegador. O programa não lê, não grava e não"
        " mostra senha, cookies ou tokens."
    )
    action = "conferir os favoritos" if configure else "começar a varredura"
    ask(
        read_line,
        f"Com a tela de operações visível, pressione Enter aqui para {action} (Ctrl+C cancela). ",
        "antes da confirmação do login",
    )


def asset_key(name):
    """name in capitals with letters and digits only: GBP/USD OTC and GBPUSD OTC are one key."""
    return "".join(char for char in name.upper() if char.isalnum())


def financial_word(text):
    """The first word of text that belongs to a trade button, the Amount or money, or None."""
    for word in WORD_PATTERN.findall(text.lower()):
        if word in FINANCIAL_WORDS:
            return word
    return None


def favorite_name(text):
    """(asset, None) for the asset that a favorite's text names, or (None, reason).

    Percentages, such as the payout that a favorite may show, are left out of the name.
    """
    joined = " ".join(text.split())
    if not joined:
        return None, "o elemento está sem texto"
    name = " ".join(PERCENT_PATTERN.sub(" ", joined).split())
    if not name:
        return None, f"o texto só tem um percentual, sem o nome de um ativo: {preview(joined)}"
    return observe.parse_asset(name)


def classify(selector, listing):
    """The Favorites in what the selector found.

    The whole selector is refused when it finds nothing, more than MAX_FAVORITES elements, a
    form field or an element with a financial word; the last three are unsafe and stop a
    running scan. Otherwise each element is a favorite, unless it is hidden, does not name an
    asset or repeats an earlier favorite.
    """
    if listing.total == 0:
        return Favorites(selector, 0, (), "o seletor não encontrou nenhum elemento")
    if listing.total > MAX_FAVORITES:
        return Favorites(
            selector,
            listing.total,
            (),
            f"o seletor encontrou {listing.total} elementos, mais que {MAX_FAVORITES}: não parece"
            " ser só a lista de favoritos",
            unsafe=True,
        )
    items, seen, financial = [], {}, None
    for index, (text, visible) in enumerate(zip(listing.texts, listing.visible)):
        shown = " ".join(text.split())
        word = financial_word(shown)
        if word is not None:
            problem = f"tem {word!r}, palavra de botão financeiro ou de dinheiro"
            financial = financial or (index + 1, word, shown)
            items.append(Favorite(index, shown, None, problem))
            continue
        name, problem = favorite_name(shown)
        if problem is None and not visible:
            problem = "o elemento não está visível"
        if problem is None:
            key = asset_key(name)
            if key in seen:
                problem = f"repete o favorito {seen[key]}"
            else:
                seen[key] = index + 1
        items.append(Favorite(index, shown, name, problem))
    items = tuple(items)
    if financial is not None:
        position, word, shown = financial
        return Favorites(
            selector,
            listing.total,
            items,
            f"o seletor também encontra o elemento {position}, {preview(shown)}, com {word!r}:"
            " a varredura não usa um seletor que encontra botões ou campos financeiros",
            unsafe=True,
        )
    if listing.form_fields:
        return Favorites(
            selector,
            listing.total,
            items,
            "o seletor também encontra um campo de formulário, como o Amount: a varredura não"
            " usa um seletor assim",
            unsafe=True,
        )
    if not any(item.problem is None for item in items):
        return Favorites(selector, listing.total, items, "nenhum elemento encontrado é um favorito utilizável")
    return Favorites(selector, listing.total, items)


def list_favorites(session, selector):
    """The Favorites that selector finds now; a failed reading refuses them, as not unsafe."""
    try:
        listing = session.favorite_items(selector, MAX_FAVORITES)
    except ReadError as error:
        return Favorites(selector, 0, (), f"a leitura dos favoritos falhou: {error}")
    return classify(selector, listing)


def platform_problem(session):
    """Why the current page must not be read or clicked, or None when it is on pocketoption.com."""
    url = session.current_url()
    if observe.url_problem(url) is None:
        return None
    return f"a página atual ({observe.display_url(url)}) não está em {observe.ALLOWED_DOMAIN}"


def ensure_on_platform(session):
    problem = platform_problem(session)
    if problem is not None:
        raise ObserverStopped(f"{problem}: a varredura não lê nem clica fora dela e parou")


def describe_favorites(favorites, current):
    """The terminal lines of a preview of favorites; current is the asset reading of now."""
    lines = [f"  O seletor {favorites.selector} encontrou agora {count(favorites.total, 'elemento', 'elementos')}."]
    current_key = asset_key(current.value) if current.available else None
    for item in favorites.items:
        if item.problem is None:
            mark = " (selecionado agora)" if item.key == current_key else ""
            lines.append(f"    {item.position}: {item.name}{mark} | texto {preview(item.text, 60)}")
        else:
            lines.append(f"    {item.position}: ignorado, {item.problem} | texto {preview(item.text, 60)}")
    if favorites.problem is not None:
        lines.append(f"  Este seletor não pode ser usado: {favorites.problem}.")
        return lines
    if not current.available:
        lines.append(f"  O ativo selecionado agora não pôde ser lido ({current.reason}).")
    elif current_key in favorites.keys:
        [match] = [item for item in favorites.usable if item.key == current_key]
        lines.append(
            f"  O ativo selecionado agora, {current.value}, é o favorito {match.position}: a"
            " confirmação de cada troca compara o nome do favorito com o do ativo."
        )
    else:
        lines.append(
            f"  AVISO: o ativo selecionado agora, {current.value}, não aparece entre os favoritos"
            " acima. A confirmação compara o nome do favorito com o do ativo; se um favorito"
            " escreve o nome de outro jeito, a troca para ele não será confirmada."
        )
    return lines


def configure_favorites(session, path, current, invalid, selectors, read_line, ctx):
    """Ask for the favorites selector, list what it finds, and write it once you accept it."""
    say("")
    say("FAVORITOS")
    say(FAVORITES_INTRO)
    if current:
        say(f"Seletor atual dos favoritos: {current}")
        prompt = "Seletor dos favoritos (Enter mantém o atual; - cancela): "
    else:
        say("Ainda sem seletor dos favoritos.")
        prompt = "Seletor dos favoritos (- cancela): "
    selector = ask(read_line, prompt, CONFIGURATION_MOMENT).strip() or current
    while True:
        if not selector or selector == "-":
            raise ConfigError(f"a varredura precisa do seletor dos favoritos, e {path} não foi alterado")
        if session.is_closed():
            raise ObserverStopped(observe.WINDOW_CLOSED_IN_CONFIGURATION)
        outside = platform_problem(session)
        if outside is not None:
            say(f"  {outside}; nada foi lido. Volte à tela de operações na janela do navegador.")
            favorites = Favorites(selector, 0, (), outside)
        else:
            favorites = list_favorites(session, selector)
            asset = observe.read_field(session, "asset", selectors["asset"], ctx)
            for line in describe_favorites(favorites, asset):
                say(line)
            if favorites.total == 1:
                suggest_selectors(session, selector)
        if favorites.problem is None:
            say(
                "  Compare a lista com os favoritos da tela. A varredura clica só nos favoritos"
                " aceitos, um de cada vez, na ordem da lista."
            )
            follow = "Enter aceita este seletor e começa a varredura; cole outro para trocar; - cancela: "
        else:
            follow = "Cole outro seletor; Enter lê este de novo; - cancela: "
        answer = ask(read_line, follow, CONFIGURATION_MOMENT).strip()
        if answer:
            selector = answer
            continue
        if favorites.problem is None:
            break
    while True:
        try:
            save_favorites_selector(path, selector, invalid)
            break
        except OSError as error:
            warn(f"AVISO: não foi possível gravar {path} ({describe(error)}).")
            answer = ask(read_line, "Enter tenta gravar de novo; - desiste sem gravar: ", CONFIGURATION_MOMENT)
            if answer.strip() == "-":
                raise ObserverStopped(f"o seletor dos favoritos não foi gravado em {path}") from None
    say(f"Seletor dos favoritos gravado em {path}.")
    return selector


def suggest_selectors(session, selector):
    """For a selector that finds one element: selectors from its classes that find more."""
    say(
        "  Para varrer, o seletor precisa encontrar todos os favoritos, não só um. Use a tag e"
        " a classe comuns a eles, sem :nth-child."
    )
    try:
        found = session.class_suggestions(selector, MAX_FAVORITES)
    except ReadError:
        return
    if found:
        options = ", ".join(f"{candidate} ({count(total, 'elemento', 'elementos')})" for candidate, total in found)
        say(f"  Tirados das classes desse elemento, e ainda não conferidos: {options}. Cole um para ver a lista.")


def run_scan(session, scan, args, ctx):
    """Visit every favorite once per cycle, until args.cycles cycles, the window closes or Ctrl+C."""
    progress = scan.progress
    progress.csv_path = new_scan_csv_path(ctx.runtime_dir)
    progress.timings = quote_timings.TimingRecorder(quote_timings.timing_path(progress.csv_path), warn)
    history = analysis.load_history(ctx.runtime_dir, exclude=(progress.csv_path,))
    scan.analyzer = analysis.Analyzer(args.horizon, history)
    say(
        f"Análise local CALL/PUT, com operações hipotéticas de {seconds_text(args.horizon)} s, depois"
        f" de cada visita com dados para análise: {history.usable} cotações utilizáveis de"
        f" {count(history.files, 'arquivo anterior', 'arquivos anteriores')}. Sem regra validada em"
        f" dados separados, a decisão é {analysis.WAIT}. {analysis.DISCLAIMER}"
    )
    streak = 0  # cycles in a row without any complete reading
    while args.cycles is None or progress.cycles < args.cycles:
        if session.is_closed():
            say("A janela do navegador foi fechada; a varredura terminou.")
            return
        ensure_on_platform(session)
        progress.cycles += 1
        cycle = progress.cycles
        start = ctx.monotonic()
        favorites = checked_favorites(session, scan)
        visits = []
        if favorites.problem is not None:
            say(f"CICLO {cycle}: sem favoritos para visitar: {favorites.problem}.")
        else:
            say(cycle_heading(cycle, favorites))
            unconfirmed = 0  # switches in a row that the asset field did not confirm
            try:
                for favorite in favorites.usable:
                    visit = visit_favorite(session, scan, favorite, cycle, favorites.total, ctx)
                    visit = with_decision(visit, scan)
                    record(visit, progress, ctx)
                    visits.append(visit)
                    unconfirmed = unconfirmed + 1 if visit.switch == NOT_CONFIRMED else 0
                    if unconfirmed >= MAX_UNCONFIRMED:
                        say(
                            f"{MAX_UNCONFIRMED} trocas seguidas não foram confirmadas: o ciclo {cycle}"
                            " termina aqui, sem clicar nos favoritos que faltam."
                        )
                        break
            except WindowClosed:
                say("A janela do navegador foi fechada; a varredura terminou.")
                return
        complete = sum(visit.observation.completeness == COMPLETE for visit in visits)
        streak = 0 if complete else streak + 1
        line = cycle_summary(cycle, visits, ctx.elapsed_ms(start) / 1000)
        if args.cycles is not None and cycle >= args.cycles:
            say(line)
            say(analysis_summary(scan))
            return
        pause = pause_seconds(args.interval, streak)
        if streak:
            line += f" Nenhuma leitura completa: próximo ciclo em {seconds_text(pause)} s, com espera maior para não insistir."
        else:
            line += f" Próximo ciclo em {seconds_text(pause)} s."
        say(line)
        say(analysis_summary(scan))
        session.wait(pause)


def pause_seconds(interval, streak):
    """interval, doubled for each cycle in a row without a complete reading, up to MAX_PAUSE."""
    if streak == 0:
        return interval
    return min(interval * 2**streak, max(interval, MAX_PAUSE))


def checked_favorites(session, scan):
    """The favorites of now; stop when the selector became unsafe or the last click removed one.

    A click removed a favorite when the list has fewer elements than before it and the
    clicked favorite is gone, also after a second reading, in case the list was redrawing.
    """
    favorites = list_favorites(session, scan.favorites_selector)
    if favorites.unsafe:
        raise ObserverStopped(
            f"o seletor dos favoritos deixou de servir: {favorites.problem}. A varredura parou,"
            " sem clicar; confira o seletor com --configure"
        )
    if scan.last_clicked is not None and removed(favorites, scan.last_clicked):
        session.wait(REMOVED_RECHECK_SECONDS)
        ensure_on_platform(session)
        favorites = list_favorites(session, scan.favorites_selector)
        if removed(favorites, scan.last_clicked):
            raise ObserverStopped(
                "o último favorito clicado sumiu da lista de favoritos depois do clique. A"
                " varredura parou, para não mudar mais nada na sua lista; confira a tela"
            )
    if favorites.problem is None:
        scan.last_clicked = None
    return favorites


def removed(favorites, clicked):
    key, total = clicked
    named = {item.key for item in favorites.items if item.name}
    return favorites.problem is None and favorites.total < total and key not in named


def visit_favorite(session, scan, favorite, cycle, total, ctx):
    """Select favorite when needed, wait for its data to settle and keep one reading."""
    selectors = scan.selectors
    check_window(session)
    ensure_on_platform(session)
    start = ctx.monotonic()
    where = {"cycle": cycle, "favorite_position": favorite.position}
    before = observe.read_field(session, "asset", selectors["asset"], ctx)
    previous_asset = previous_quote = switch_ms = reference = clicked_at = None
    if on_favorite(before, favorite):
        switch = ALREADY
    else:
        previous_asset = before.value if before.available else None
        reference = read_reference(session, scan, before, ctx, where)
        previous_quote = reference.value if reference is not None else None
        problem = click(session, scan, favorite)
        if problem is not None:
            reason = f"o favorito não foi clicado: {problem}"
            return failed_visit(session, cycle, favorite, total, NOT_CLICKED, reason, previous_asset, reference, ctx)
        start = clicked_at = ctx.monotonic()
        reading = observe.read_field(session, "asset", selectors["asset"], ctx)
        while not on_favorite(reading, favorite) and ctx.monotonic() - start < scan.load_timeout:
            check_window(session)
            session.wait(ASSET_POLL_SECONDS)
            ensure_on_platform(session)
            reading = observe.read_field(session, "asset", selectors["asset"], ctx)
        switch_ms = ctx.elapsed_ms(start)
        if not on_favorite(reading, favorite):
            shown = reading.value if reading.available else f"INDISPONÍVEL ({reading.reason})"
            reason = (
                f"a troca para {favorite.name} não foi confirmada em {seconds_text(scan.load_timeout)} s:"
                f" o ativo na tela continuou {shown}"
            )
            return failed_visit(
                session, cycle, favorite, total, NOT_CONFIRMED, reason, previous_asset, reference, ctx, switch_ms
            )
        switch = CONFIRMED
        session.wait(SETTLE_SECONDS)
    switched = switch == CONFIRMED
    moment = TIMING_AFTER_SWITCH if switched else TIMING_NO_SWITCH
    load_start = ctx.monotonic()
    previous, polls = None, 0
    while True:
        check_window(session)
        poll_start = ctx.monotonic()
        poll = read_poll(session, selectors, ctx)
        if poll.outside:
            ensure_on_platform(session)
        polls += 1
        readings, settled = judge(favorite, previous, poll, reference, switched, scan.sources)
        extra = {"asset": poll.asset_before.value if poll.asset_before.available else None}
        if clicked_at is not None:
            extra["since_click_ms"] = round((poll_start - clicked_at) * 1000, 1)
        if poll.price.available:
            judged = readings["price"]
            extra["verdict"] = ACCEPTED if judged.available else f"{REFUSED}: {judged.reason}"
        record_timing(scan, moment, poll.price, where, polls, **extra)
        if settled or ctx.monotonic() - start >= scan.load_timeout:
            break
        previous = poll
        session.wait(POLL_SECONDS)
    if not settled:
        readings = with_time_limit(readings, scan)
    observation = observe.Observation(poll.captured_at, poll.page, readings, poll.duration_ms)
    return Visit(
        cycle, favorite, total, switch, observation, previous_asset, previous_quote, switch_ms,
        ctx.elapsed_ms(load_start), polls, reference, ctx.now(),
    )


def read_reference(session, scan, before, ctx, where):
    """The previous asset's quote, read on its own chart before the click; None without a source.

    A few short attempts, all before the click, so all on the previous asset's chart: they
    stop at the first quote read, after REFERENCE_ATTEMPTS, or when REFERENCE_BUDGET_SECONDS
    have passed since the first one. Each attempt reads the quote and then the asset again,
    and the quote counts only when the asset is still the one read before (before). The
    original reason of each failed attempt is kept in the Reference, never replaced by a
    generic message.
    """
    source = scan.selectors["price"]
    if not source:
        return None
    start = ctx.monotonic()
    asset = before.value if before.available else None
    reasons, attempt = [], 0
    while attempt < REFERENCE_ATTEMPTS:
        if attempt:
            if ctx.monotonic() - start >= REFERENCE_BUDGET_SECONDS:
                break
            check_window(session)
            session.wait(REFERENCE_RETRY_SECONDS)
            ensure_on_platform(session)
        attempt += 1
        quote = observe.read_field(session, "price", source, ctx)
        if not quote.available:
            record_timing(scan, TIMING_REFERENCE, quote, where, attempt, asset=asset)
            reasons.append(quote.reason)
            continue
        if before.available:
            after = observe.read_field(session, "asset", scan.selectors["asset"], ctx)
            if not (after.available and asset_key(after.value) == asset_key(before.value)):
                shown = after.value if after.available else f"INDISPONÍVEL ({after.reason})"
                problem = f"o ativo na tela mudou de {before.value} para {shown} durante a leitura da cotação"
                record_timing(scan, TIMING_REFERENCE, quote, where, attempt, asset=asset, verdict=f"{REFUSED}: {problem}")
                reasons.append(problem)
                continue
        record_timing(scan, TIMING_REFERENCE, quote, where, attempt, asset=asset, verdict=ACCEPTED)
        return Reference(quote.value, quote.status, attempt, ctx.elapsed_ms(start), tuple(reasons))
    missing = Reference(None, "", attempt, ctx.elapsed_ms(start), tuple(reasons))
    return replace(missing, status=f"{UNAVAILABLE}: {missing.reason}")


def record_timing(scan, moment, reading, where, attempt, **extra):
    """One line of the timings file for a reading of the quote that has a source."""
    timings = scan.progress.timings
    if timings is None or reading.source is None:
        return
    timings.add(moment, observe.timing_row(reading, attempt=attempt, **where, **extra))


def check_window(session):
    if session.is_closed():
        raise WindowClosed()


def click(session, scan, favorite):
    """None after clicking favorite, or the reason it was not clicked."""
    ensure_on_platform(session)
    favorites = checked_favorites(session, scan)
    if favorites.problem is not None:
        return f"a lista de favoritos não pôde ser usada agora ({favorites.problem})"
    matches = [item for item in favorites.usable if item.key == favorite.key]
    if not matches:
        return f"{favorite.name} não está mais na lista de favoritos"
    target = matches[0]
    try:
        problem = session.click_favorite(scan.favorites_selector, target.index, click_check(target))
    except ReadError as error:
        return f"o clique falhou ({error})"
    if problem is None:
        scan.progress.clicks += 1
        scan.last_clicked = (target.key, favorites.total)
    return problem


def click_check(target):
    """The check of the element at target's position, read right before the click.

    The element must still name target's asset; a percentage beside the name, such as a
    payout, may have changed.
    """

    def accept(item):
        shown = " ".join(item.text.split())
        word = financial_word(shown)
        if word is not None:
            return f"o elemento tem {word!r}, palavra de botão financeiro ou de dinheiro"
        name, _ = favorite_name(shown)
        if name is None or asset_key(name) != target.key:
            return f"o favorito {target.position} mudou antes do clique: agora mostra {preview(shown)}"
        if not item.visible:
            return "o favorito deixou de estar visível"
        if item.form_fields:
            return "o elemento tem um campo de formulário, como o Amount"
        if item.width > MAX_FAVORITE_WIDTH or item.height > MAX_FAVORITE_HEIGHT:
            return f"o elemento tem {item.width:.0f}x{item.height:.0f} px, grande demais para um favorito"
        return None

    return accept


def read_poll(session, selectors, ctx):
    """The asset, the quote, the payout and the asset again, in this order, from one page."""
    start = ctx.monotonic()
    captured_at = ctx.now()
    url = session.current_url()
    page = observe.display_url(url)
    if observe.url_problem(url) is not None:
        reading = unavailable(f"a página atual ({page}) não está em {observe.ALLOWED_DOMAIN}; nada foi lido")
        return Poll(captured_at, page, reading, reading, reading, reading, ctx.elapsed_ms(start), True)
    before = observe.read_field(session, "asset", selectors["asset"], ctx)
    price = observe.read_field(session, "price", selectors["price"], ctx)
    payout = observe.read_field(session, "payout", selectors["payout"], ctx)
    after = observe.read_field(session, "asset", selectors["asset"], ctx)
    return Poll(captured_at, page, before, price, payout, after, ctx.elapsed_ms(start))


def on_favorite(reading, favorite):
    return reading.available and asset_key(reading.value) == favorite.key


def asset_problem(favorite, poll):
    """Why poll is not a reading of favorite, or None when the asset was it before and after."""
    for reading in (poll.asset_before, poll.asset_after):
        if not reading.available:
            return reading.reason
    for reading in (poll.asset_before, poll.asset_after):
        if not on_favorite(reading, favorite):
            return f"a tela mostrava {reading.value}, e não {favorite.name}, durante a leitura"
    return None


def refuse(reading, reason):
    """reading made unavailable with reason, keeping its source and its times."""
    return replace(reading, value=None, status=f"{UNAVAILABLE}: {reason}")


def judge(favorite, previous, poll, reference, switched, sources):
    """(readings, settled): the fields to keep from poll, and whether all of them are final.

    previous is the reading before poll, or None. reference is the Reference of the quote
    read before the click, or None; switched tells whether this visit clicked. A field
    without a source is final as it is; the others need two readings in a row on
    favorite's asset that agree.
    """
    problem = asset_problem(favorite, poll)
    if problem is not None:
        other = f"não aproveitada: {problem}"
        readings = {
            "asset": refuse(poll.asset_after, problem),
            "price": refuse(poll.price, other) if poll.price.available else poll.price,
            "payout": refuse(poll.payout, other) if poll.payout.available else poll.payout,
        }
        return readings, False
    prior = previous if previous is not None and asset_problem(favorite, previous) is None else None
    price, price_final = judge_price(poll.price, prior, reference, switched, sources["price"])
    payout, payout_final = judge_payout(poll.payout, prior, sources["payout"])
    return {"asset": poll.asset_after, "price": price, "payout": payout}, price_final and payout_final


def judge_price(reading, prior, previous_reference, switched, sourced):
    if not sourced:
        return reading, True
    if not reading.available:
        return reading, False
    if switched and (previous_reference is None or not previous_reference.available):
        return refuse(reading, no_reference(previous_reference)), True  # waiting brings no proof
    reference = previous_reference.value if previous_reference is not None else None
    value = quote_number(reading.value)
    if value is None:
        return refuse(reading, f"a cotação {reading.value} não pôde ser comparada"), False
    if switched and near_reference(value, reference):
        return refuse(
            reading,
            f"a cotação lida, {reading.value}, está a menos de {percent(PREVIOUS_QUOTE_GAP)} da"
            f" lida no ativo anterior antes da troca, {reference}: o gráfico pode ainda ser o do"
            " ativo anterior",
        ), False
    earlier = None
    if prior is not None and prior.price.available:
        candidate = quote_number(prior.price.value)
        if candidate is not None and not (switched and near_reference(candidate, reference)):
            earlier = candidate
    if earlier is None:
        return refuse(reading, "falta uma segunda leitura da cotação, para conferir que ela não saltou"), False
    if relative_gap(value, earlier) > QUOTE_JUMP:
        return refuse(
            reading,
            f"a cotação saltou de {prior.price.value} para {reading.value} entre duas leituras"
            " seguidas: o gráfico pode estar carregando",
        ), False
    return reading, True


def judge_payout(reading, prior, sourced):
    if not sourced:
        return reading, True
    if not reading.available:
        return reading, False
    if prior is None or not prior.payout.available:
        return refuse(reading, "falta uma segunda leitura do payout, para conferir que ele não mudou"), False
    if payout_number(prior.payout.value) != payout_number(reading.value):
        return refuse(
            reading,
            f"o payout mudou de {prior.payout.value}% para {reading.value}% entre duas leituras seguidas",
        ), False
    return reading, True


def with_time_limit(readings, scan):
    """readings, with the time limit added to the reason of every unavailable field with a source."""
    limited = {}
    for name, reading in readings.items():
        if not reading.available and scan.sources[name]:
            reading = refuse(reading, f"{reading.reason} (limite de {seconds_text(scan.load_timeout)} s por favorito)")
        limited[name] = reading
    return limited


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


def near_reference(value, reference):
    number = quote_number(reference) if reference is not None else None
    return number is not None and relative_gap(value, number) <= PREVIOUS_QUOTE_GAP


def relative_gap(value, other):
    return abs(value - other) / abs(other) if other else math.inf


def percent(fraction):
    return f"{seconds_text(fraction * 100)}%"


def seconds_text(value):
    """value with a decimal comma, as in 2,5."""
    return f"{value:g}".replace(".", ",")


def payout_number(text):
    try:
        return float(text.replace(",", "."))
    except (AttributeError, ValueError):
        return None


def eligibility(reading):
    """sim for a payout of ELIGIBLE_PAYOUT% or more, não below it, indisponível without a payout."""
    return payout_eligibility(reading.value if reading.available else None)


def payout_eligibility(payout):
    number = payout_number(payout) if payout is not None else None
    if number is None:
        return UNKNOWN_ELIGIBILITY
    return ELIGIBLE if number >= ELIGIBLE_PAYOUT else NOT_ELIGIBLE


def failed_visit(
    session, cycle, favorite, total, switch, reason, previous_asset, reference, ctx, switch_ms=None
):
    """A visit without data: the favorite was not clicked, or its switch was not confirmed."""
    captured_at = ctx.now()
    page = observe.display_url(session.current_url())
    readings = {name: unavailable(reason if name == "asset" else f"não lida: {reason}") for name in FIELDS}
    observation = observe.Observation(captured_at, page, readings, None)
    previous_quote = reference.value if reference is not None else None
    return Visit(
        cycle, favorite, total, switch, observation, previous_asset, previous_quote, switch_ms, None, 0,
        reference, captured_at,
    )


ARTICLES = {"asset": "o ativo", "price": "a cotação do ativo", "payout": "o payout"}


def analysis_of(visit):
    """Analysis of visit: are its data complete and current, so usable for analysis?

    Complete: the asset, the quote and the payout available. In a visit, that means that
    the three were read in its last reading, with the asset confirmed on the screen before
    and after it, each value equal to the reading before and, after a switch, the quote
    away from the one read on the previous asset's chart before the click. Current: the
    quote was captured at most MAX_DATA_AGE_SECONDS before the visit was recorded. A
    payout of ELIGIBLE_PAYOUT% or more (payout_eligible) proves none of this: it only says
    that the payout allows a first entry on that asset.
    """
    if visit.switch == NOT_CLICKED:
        return Analysis(False, "o favorito não foi clicado, e nada foi lido")
    if visit.switch == NOT_CONFIRMED:
        return Analysis(False, "a troca de ativo não foi confirmada, e nada foi lido")
    readings = visit.observation.readings
    missing = [ARTICLES[name] for name in FIELDS if not readings[name].available]
    if missing:
        verb = "falta" if len(missing) == 1 else "faltam"
        joined = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " e " + missing[-1]
        return Analysis(False, f"{verb} {joined}")
    price = readings["price"]
    if price.captured_at is None or visit.recorded_at is None:
        return Analysis(False, "sem o horário de captura da cotação, a idade dos dados é desconhecida")
    age_ms = round((visit.recorded_at - price.captured_at).total_seconds() * 1000, 1)
    if age_ms > MAX_DATA_AGE_SECONDS * 1000:
        return Analysis(
            False,
            f"a cotação foi capturada {seconds_text(round(age_ms / 1000, 1))} s antes da gravação, mais que o"
            f" limite de {seconds_text(MAX_DATA_AGE_SECONDS)} s: não é atual",
            age_ms,
        )
    return Analysis(True, "ativo, cotação e payout lidos juntos, conferidos e atuais", age_ms)


def with_decision(visit, scan):
    """visit with the local analysis made right after it: CALL, PUT or AGUARDAR, and why.

    Only a visit with data usable for analysis feeds the analysis, with the quote and the
    payout of its last reading; the decision is computed at the moment the visit was
    recorded, so a CALL or PUT can only come from a quote captured moments before.
    """
    if scan.analyzer is None:
        return visit
    state = visit.analysis
    readings = visit.observation.readings
    price = readings["price"]
    number = quote_number(price.value) if price.available else None
    if not state.ready or number is None:
        reason = f"sem dados para análise nesta visita: {state.note}"
        return replace(visit, decision=analysis.Decision(visit.favorite.name, analysis.WAIT, reason))
    name = readings["asset"].value
    sample = analysis.Sample(price.captured_at, number, price.value, payout_number(readings["payout"].value))
    scan.analyzer.add(name, sample)
    return replace(visit, decision=scan.analyzer.decide(name, visit.recorded_at))


def analysis_summary(scan):
    """The line about the analysis at the end of a cycle: how many assets have a validated rule."""
    if scan.analyzer is None:
        return ""
    assets, validated = scan.analyzer.summary()
    horizon = seconds_text(scan.analyzer.horizon)
    line = (
        f"ANÁLISE CALL/PUT (operações de {horizon} s): {count(assets, 'ativo', 'ativos')} com dados,"
        f" {len(validated)} com regra validada em dados separados"
    )
    if validated:
        return line + f" ({', '.join(validated)}); a decisão de cada um vale só logo depois da visita dele."
    return line + f": {analysis.WAIT}, sem decisão CALL ou PUT."


def record(visit, progress, ctx):
    """Write visit to the CSV, count it and show it."""
    if progress.csv is None:
        progress.csv = ScanCsv(progress.csv_path or new_scan_csv_path(ctx.runtime_dir))
        say(f"CSV: {progress.csv.path}")
    progress.csv.write(visit)
    observation = visit.observation
    ready = visit.analysis.ready
    progress.visits += 1
    progress.valid += observation.valid
    progress.complete += observation.completeness == COMPLETE
    progress.ready += ready
    summary = progress.assets.setdefault(visit.favorite.key, AssetSummary(visit.favorite.name))
    summary.visits += 1
    summary.complete += observation.completeness == COMPLETE
    summary.ready += ready
    summary.last_visit = visit
    if ready:
        summary.last_ready = visit  # its quote and payout were read together, never mixed with older ones
    for line in visit_lines(visit):
        say(line)


class ScanCsv:
    """A new CSV file with SCAN_COLUMNS, flushed after every line."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "x", newline="", encoding="utf-8")
        try:
            self._writer = csv.writer(self._file)
            self._writer.writerow(SCAN_COLUMNS)
            self._file.flush()
        except BaseException:
            self._file.close()
            raise

    def write(self, visit):
        self._writer.writerow(scan_row(visit))
        self._file.flush()

    def close(self):
        self._file.close()


def scan_row(visit):
    row = observe.csv_row(visit.observation)
    row += [
        str(visit.cycle),
        str(visit.favorite.position),
        spreadsheet_safe(visit.favorite.text),
        visit.switch,
        visit.previous_asset or "",
        visit.previous_quote or "",
        "" if visit.switch_ms is None else f"{visit.switch_ms:.1f}",
        "" if visit.load_ms is None else f"{visit.load_ms:.1f}",
        str(visit.polls),
        visit.eligibility,
    ]
    reference, state, decision = visit.reference, visit.analysis, visit.decision
    row += [
        reference.status if reference is not None else "",
        str(reference.attempts) if reference is not None else "",
        f"{reference.ms:.1f}" if reference is not None else "",
        READY if state.ready else NOT_READY,
        state.note,
        "" if state.age_ms is None else f"{state.age_ms:.1f}",
        decision.action if decision is not None else "",
        decision_note(decision) if decision is not None else "",
    ]
    return row


def decision_note(decision):
    """The reason of a decision, with the end of its validity for CALL and PUT."""
    if decision.valid_until is None:
        return decision.reason
    return f"{decision.reason}; válida até {decision.valid_until.isoformat(timespec='seconds')} (relógio local, UTC)"


def spreadsheet_safe(text):
    """text, with an apostrophe before a first character that a spreadsheet reads as a formula."""
    return "'" + text if text.startswith(observe.FORMULA_PREFIXES) else text


def new_scan_csv_path(runtime_dir):
    """A new CSV path with a unique name in runtime_dir, which is created if needed."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        name = f"scan-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csv"
        path = runtime_dir / name
        if not path.exists():
            return path


ELIGIBILITY_TEXT = {
    ELIGIBLE: f"payout ELEGÍVEL (≥ {ELIGIBLE_PAYOUT}%)",
    NOT_ELIGIBLE: f"payout não elegível (< {ELIGIBLE_PAYOUT}%)",
    UNKNOWN_ELIGIBILITY: "elegibilidade indisponível (sem payout)",
}


def analysis_text(analysis):
    if analysis.ready:
        return "dados para análise: SIM (completos e atuais)"
    return f"dados para análise: NÃO ({analysis.note})"


def cycle_heading(cycle, favorites):
    names = ", ".join(f"{item.position} {item.name}" for item in favorites.usable)
    line = f"CICLO {cycle}: {count(len(favorites.usable), 'favorito', 'favoritos')} ({names})"
    skipped = [item for item in favorites.items if item.problem is not None]
    if skipped:
        line += "; ignorados: " + "; ".join(f"{item.position} ({item.problem})" for item in skipped)
    return line


def visit_lines(visit):
    """The terminal lines of a visit: the data, where the time went and the analysis after it."""
    observation = visit.observation
    parts = [
        f"CICLO {visit.cycle} | favorito {visit.favorite.position} de {visit.total}, {visit.favorite.name}",
        observe.KIND_LABELS[observation.completeness],
    ]
    if visit.switch in (NOT_CONFIRMED, NOT_CLICKED):
        parts.append(f"SEM LEITURA: {observation.readings['asset'].reason}")  # one reason for all fields
    else:
        parts += [f"{LABELS[name]} {observe.shown_value(name, observation.readings[name])}" for name in FIELDS]
        parts.append(ELIGIBILITY_TEXT[visit.eligibility])
        parts.append(analysis_text(visit.analysis))
    parts.append("gravada no CSV")
    times = [switch_text(visit)]
    if visit.reference is not None:
        times.append(reference_text(visit.reference))
    if visit.load_ms is not None:
        times.append(f"dados em {visit.load_ms:.0f} ms, {count(visit.polls, 'leitura', 'leituras')}")
    price = observation.readings["price"]
    if price.processing_ms is not None:
        detail = f"cotação {price.processing_ms:.0f} ms"
        if price.timings is not None:
            detail += f" ({price.timings.describe()})"
        times.append(detail)
    times.append(f"relógio local {utc_text(observation.captured_at)}")
    lines = (" | ".join(parts), "  tempos: " + " | ".join(times))
    if visit.decision is not None:
        lines += (f"  análise CALL/PUT: {visit.decision.describe()}",)
    return lines


def reference_text(reference):
    attempts = count(reference.attempts, "tentativa", "tentativas")
    if reference.available:
        return f"cotação antes da troca {reference.value} em {attempts}, {reference.ms:.0f} ms"
    return f"cotação antes da troca INDISPONÍVEL em {attempts}, {reference.ms:.0f} ms"


def switch_text(visit):
    if visit.switch == ALREADY:
        return "sem troca, o favorito já estava selecionado"
    if visit.switch == CONFIRMED:
        return f"troca confirmada em {visit.switch_ms:.0f} ms"
    if visit.switch == NOT_CONFIRMED:
        return f"troca NÃO confirmada em {visit.switch_ms:.0f} ms"
    return "sem clique"


def cycle_summary(cycle, visits, seconds):
    complete = sum(visit.observation.completeness == COMPLETE for visit in visits)
    ready = sum(visit.analysis.ready for visit in visits)
    duration = f"{seconds:.1f}".replace(".", ",")
    line = (
        f"CICLO {cycle} concluído em {duration} s: {count(len(visits), 'favorito visitado', 'favoritos visitados')},"
        f" {count(complete, 'leitura completa', 'leituras completas')},"
        f" {count(ready, 'com dados para análise', 'com dados para análise')}."
    )
    if visits:
        usable = [visit.favorite.name for visit in visits if visit.eligibility == ELIGIBLE and visit.analysis.ready]
        only_payout = [
            f"{visit.favorite.name} ({visit.analysis.note})"
            for visit in visits
            if visit.eligibility == ELIGIBLE and not visit.analysis.ready
        ]
        line += f" Payout elegível (≥ {ELIGIBLE_PAYOUT}%) e dados para análise: {', '.join(usable) or 'nenhum'}."
        if only_payout:
            line += f" Payout elegível, mas SEM dados para análise: {'; '.join(only_payout)}."
    return line


def written_summary(progress):
    partial = progress.valid - progress.complete
    return (
        f"{count(progress.cycles, 'ciclo', 'ciclos')}, {count(progress.visits, 'visita', 'visitas')} a"
        f" favoritos, {count(progress.clicks, 'clique', 'cliques')} em favoritos,"
        f" {count(progress.complete, 'leitura completa', 'leituras completas')} e"
        f" {count(partial, 'parcial', 'parciais')},"
        f" {count(progress.ready, 'com dados para análise', 'com dados para análise')}"
    )


def report(progress):
    """The per-asset summary, the times of the readings and the files of the scan so far.

    The quote and the payout shown for an asset come from the same reading, the last one
    usable for analysis, with its cycle and clock: a quote of one visit is never shown
    beside the payout of another.
    """
    if progress.assets:
        say("Por ativo:")
        for summary in progress.assets.values():
            line = (
                f"  {summary.name}: {count(summary.visits, 'visita', 'visitas')},"
                f" {count(summary.complete, 'completa', 'completas')},"
                f" {count(summary.ready, 'com dados para análise', 'com dados para análise')}"
            )
            ready = summary.last_ready
            if ready is None:
                line += "; nenhuma leitura completa e atual"
            else:
                readings = ready.observation.readings
                line += (
                    f"; última leitura completa e atual, do ciclo {ready.cycle} (relógio local"
                    f" {utc_text(readings['price'].captured_at)}): cotação {readings['price'].value} e"
                    f" payout {readings['payout'].value}%, {ELIGIBILITY_TEXT[ready.eligibility]}"
                )
            last = summary.last_visit
            if last is not None and last is not ready:
                kind = observe.KIND_LABELS[last.observation.completeness]
                line += f"; última visita, do ciclo {last.cycle}: {kind}, {analysis_text(last.analysis)}"
            say(line)
    observe.report_timings(progress.timings)
    if progress.csv is not None:
        say(f"CSV: {progress.csv.path}")


def finish(progress):
    say(f"Concluído: {written_summary(progress)}.")
    report(progress)
    if progress.valid == 0:
        warn(
            "PARADA: nenhuma leitura com dados foi gravada. Confira os motivos acima, o seletor"
            " dos favoritos e as fontes do coletor."
        )
        return EXIT_FAILED
    say(
        "Compare as linhas com a tela: o CSV sozinho não comprova que cada troca e cada leitura"
        " corresponderam ao ativo certo."
    )
    return EXIT_OK


def open_scan_session(profile_dir, *, headless=False):
    """Start Playwright and open Chromium with the persistent profile, as the collector does."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as error:
        raise ConfigError(observe.install_hint()) from error
    return ScanSession(sync_playwright, Error, Path(profile_dir), headless)


class ScanSession(observe.PlaywrightSession):
    """PlaywrightSession with the favorites: list them, and click one to select it.

    click_favorite is the only method in the program that acts on the page. It reads the
    element again and clicks it only when the check given by the scan accepts what it read,
    with Playwright's own checks of the click (the element is visible, stable, enabled and
    the one that receives it) and without changing any option of the click. Besides that,
    this class only reads: locator, count, all_inner_texts, nth, is_visible, inner_text,
    bounding_box, get_attribute and and_, the pages of the context, and what
    PlaywrightSession reads.
    """

    def favorite_items(self, selector, limit):
        """FavoriteList of what selector finds; the texts only when it finds 1 to limit elements."""
        try:
            locator = self._page.locator(selector)
            total = locator.count()
            if total == 0 or total > limit:
                return FavoriteList(total)
            texts = tuple(locator.all_inner_texts())
            fields = self._form_fields(locator)
            visible = tuple(locator.nth(index).is_visible() for index in range(len(texts)))
        except self._error_type as error:
            raise ReadError(describe(error)) from error
        return FavoriteList(len(texts), texts, visible, fields)

    def click_favorite(self, selector, index, check):
        """Click the element at index of selector if check accepts it; None, or check's reason.

        check receives the FavoriteItem read right before the click. A click that opens
        another tab or window stops the scan, so that only one navigation runs.
        """
        try:
            element = self._page.locator(selector).nth(index)
            visible = element.is_visible()
            text = element.inner_text(timeout=observe.READ_TIMEOUT_MS) if visible else ""
            box = element.bounding_box(timeout=observe.READ_TIMEOUT_MS) if visible else None
            item = FavoriteItem(
                text,
                visible,
                box["width"] if box else 0.0,
                box["height"] if box else 0.0,
                self._form_fields(element),
            )
            problem = check(item)
            if problem is not None:
                return problem
            pages = len(self._context.pages)
            element.click(timeout=CLICK_TIMEOUT_MS)
            opened = len(self._context.pages) - pages
        except self._error_type as error:
            raise ReadError(describe(error)) from error
        if opened > 0:
            raise ObserverStopped(
                "o clique no favorito abriu outra aba ou janela; a varredura parou, para manter"
                " uma única navegação. Feche a aba nova e confira o seletor dos favoritos"
            )
        return None

    def class_suggestions(self, selector, limit):
        """(selector, count) from the classes of the only element of selector, finding 2 to limit."""
        try:
            locator = self._page.locator(selector)
            if locator.count() != 1:
                return []
            classes = (locator.get_attribute("class", timeout=observe.READ_TIMEOUT_MS) or "").split()
            candidates = []
            if classes:
                candidates.append("".join("." + observe.css_identifier(name) for name in classes))
                candidates += ["." + observe.css_identifier(name) for name in classes]
            found = []
            for candidate in dict.fromkeys(candidates):
                total = self._page.locator(candidate).count()
                if 2 <= total <= limit:
                    found.append((candidate, total))
        except self._error_type as error:
            raise ReadError(describe(error)) from error
        return found[:4]

    def _form_fields(self, locator):
        """Form fields found by locator or inside what it finds."""
        fields = self._page.locator(FORM_FIELDS)
        return locator.locator(FORM_FIELDS).count() + locator.and_(fields).count()


if __name__ == "__main__":
    sys.exit(main())
