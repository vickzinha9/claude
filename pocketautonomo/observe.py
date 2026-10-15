"""Read-only collector of the Pocket Option trading screen: python -m pocketautonomo.observe.

READ ONLY: the program sends no order, never clicks CALL or PUT and makes no financial
operation. It opens the page given with --url, which must be https on pocketoption.com or
one of its subdomains, in a visible Chromium window driven by Playwright. You log in
yourself, in that window. The browser profile stays in runtime/browser-profile, inside the
current directory and outside Git, so the browser data survives between runs; that folder
holds your session, so do not share it. The program never types, clicks, presses keys,
evaluates JavaScript of its own, reads cookies or storage or listens to the network: through
Playwright's locators, it only counts the elements a selector finds and reads their text.

Pocket Option documents no programming interface for the CALL/PUT web terminal (see
INTEGRATION.md). The fields are therefore read from the page, and only through selectors
that you checked on the real screen, kept in runtime/observe-selectors.json. No selector
ships with the program, because none was verified against the real platform. A field is
unavailable, with the reason, when it has no selector, when its selector finds no element,
more than one element or a hidden one, or when the text does not look like that field. A
page outside pocketoption.com is never read.

Without --configure, the program creates that file with empty fields on its first run, and
you fill it yourself; it is read once, after you press Enter. With --configure, the program
asks in the terminal for the link, when --url is missing, and, after your login, for the
selector of each field. It reads each selector right away and shows the value it found, or
why the field is unavailable, so that you can correct the selector. Only after you confirm
does it write the file, with json, so you never escape backslashes or quotes yourself. Text
found by a selector proves nothing by itself: only you, comparing it with the screen, can
tell whether the selector reads the right element.

The quote of the asset (cotação do ativo, kept under the key "price") can come from text,
like the other fields, or from the chart: the trading screen draws its current-quote
marker on a canvas, which has no text. With the visual source, set up by --configure, the
program captures only the chart element given by a selector you checked, finds the
current-quote marker in that image with quote_reader and reads it with the local OCR of
quote_ocr. The image stays in memory; only the configuration's previews are written to
runtime/, each to a new file, so that no image still open in a viewer is overwritten. A
reading that is missing, ambiguous or does not look like a quote is unavailable, with the
reason: no zero, no correction, no earlier value. A candidate without a valid reading
cannot be chosen, and the visual reading is accepted only when all its previews read a
quote. To check whether the quote also exists as text, --configure can search the page for
visible elements whose text looks like a quote.

Each observation carries its capture time, taken from this computer's clock in UTC right
before the reading; no time is read from the platform, and none of these times is the
server's. The quote also records its source, the moment of its capture and how long the
capture and the reading took, split, for the visual reading, into the capture (element,
screenshot and decoding), the detection of the marker and the OCR. Each observation is
marked complete, when the asset, the quote and the payout are all available, or partial.
Observations with at least one available field are written, line by line, to a new CSV
file in runtime/; the others are only shown in the terminal. The query and the fragment
of a URL are never shown or written.
Between two observations, the program waits with Playwright's page.wait_for_timeout, not
time.sleep, so that Playwright keeps processing the browser's events; each value is still
read at one moment and can change right after.

Playwright is imported only when the browser opens, so this module and its tests run
without it. Exit codes: 0 when the run ended with at least one valid observation, 1 when it
failed or ended without one, 2 for invalid arguments or configuration, including a missing
Playwright, and 130 after Ctrl+C.
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pocketautonomo import quote_reader
from pocketautonomo.quote_ocr import OcrUnavailable, RapidOcrEngine, decode_png

ALLOWED_DOMAIN = "pocketoption.com"
RUNTIME_DIR = Path("runtime")  # relative to the current directory, like the simulation
PROFILE_DIR_NAME = "browser-profile"
SELECTORS_FILE_NAME = "observe-selectors.json"
PLAYWRIGHT_REQUIREMENT = "playwright==1.64.0"

FIELDS = ("asset", "price", "payout")
# The files keep the keys asset, price and payout; only the texts shown changed. "price" is
# the quote of the asset, never the amount of a bet (Amount).
LABELS = {"asset": "ativo", "price": "cotação do ativo", "payout": "payout"}
OF_LABELS = {"asset": "do ativo", "price": "da cotação do ativo", "payout": "do payout"}
QUOTE_NOTE = (
    "A cotação do ativo é o preço do ativo no gráfico, que muda a cada instante, como"
    " 1.08534. Não é o valor da aposta: esse é o campo Amount da Pocket Option, o dinheiro"
    " aplicado numa operação."
)
CSV_COLUMNS = (
    # The first 8 columns are those of the earlier CSV files, in the same order.
    "captured_at_utc",  # this computer's clock, in UTC; not the server's time
    "page",
    "asset",
    "asset_status",
    "price",  # the quote of the asset (cotação do ativo), never the bet amount
    "price_status",
    "payout_percent",
    "payout_status",
    "completeness",  # completa: asset, quote and payout available; parcial: not all of them
    "price_source",  # texto, ocr, or empty without a source
    "price_captured_at_utc",  # this computer's clock right before the quote was captured
    "price_processing_ms",  # capture and reading of the quote, in milliseconds
    "observation_ms",  # the whole observation, in milliseconds
    # The visual reading of the quote, step by step, in milliseconds; empty for text.
    "price_capture_ms",  # finding the chart element, the screenshot and decoding it
    "price_detection_ms",  # finding the marker in the image
    "price_ocr_ms",  # preparing the marker's text and the OCR readings
)
OK = "ok"
UNAVAILABLE = "indisponível"
COMPLETE, PARTIAL, EMPTY = "completa", "parcial", "vazia"
TEXT_SOURCE, OCR_SOURCE = "texto", "ocr"
OCR_KEY = "price_ocr"  # the visual source of the quote in observe-selectors.json

DEFAULT_INTERVAL = 5.0
MIN_INTERVAL = 1.0
NAVIGATION_TIMEOUT_MS = 60_000
READ_TIMEOUT_MS = 2_000
CAPTURE_TIMEOUT_MS = 5_000
MAX_ASSET_LENGTH = 60
PREVIEW_READINGS = 3
SEARCH_RESULTS = 15
SEARCH_SCAN = 60
# Each capture of the configuration gets its own image, never overwritten: on Windows, an
# image still open in a viewer cannot be truncated ([Errno 22] Invalid argument).
CANDIDATES_PREFIX = "cotacao-candidatos"
PREVIEW_PREFIX = "cotacao-previa"
PREVIEW_GIVE_UP = "desistir"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

HOST_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*" + re.escape(ALLOWED_DOMAIN))
PRICE_PATTERN = quote_reader.QUOTE_PATTERN  # digits with at least one decimal separator
INTEGER_PATTERN = re.compile(r"[0-9]+")
# Visible text that looks like a quote, for the search of --configure.
QUOTE_SEARCH_PATTERN = re.compile(r"^\s*[0-9]+(?:[.,][0-9]+)+\s*$")
PAYOUT_PATTERN = re.compile(r"\+?([0-9]{1,3}(?:[.,][0-9]+)?)\s*%")
URL_TAIL_PATTERN = re.compile(r"""([a-z][a-z0-9+.-]*:[^\s?#"'<>]*)[?#][^\s"'<>]*""", re.IGNORECASE)
FORMULA_PREFIXES = ("=", "+", "-", "@")  # a spreadsheet could run such a cell as a formula

SELECTORS_TEMPLATE = {
    "_leia_me": (
        "Gravado pelo modo --configure (no Windows, observar_windows.cmd), que pede os"
        " seletores no terminal e mostra a prévia de cada um. Se editar à mão, use em asset"
        " (ativo), price (cotação do ativo, não o valor da aposta, Amount) e payout"
        " seletores conferidos na tela de operações, como explica o INTEGRATION.md, e deixe"
        " \"\" no campo sem seletor: ele fica indisponível. Chaves que começam com _ são"
        " ignoradas."
    ),
    "asset": "",
    "price": "",
    "payout": "",
}
# Answers that pick a field to correct at the end of --configure.
FIELD_ANSWERS = {
    "ativo": "asset",
    "asset": "asset",
    "cotação": "price",
    "cotacao": "price",
    "cotação do ativo": "price",
    "cotacao do ativo": "price",
    "preço": "price",
    "preco": "price",
    "price": "price",
    "payout": "payout",
}
COMPARE_WITH_SCREEN = (
    "Compare cada valor com o que a tela mostra agora. Texto encontrado não comprova que o"
    " elemento é o certo: só essa comparação, feita por você, valida um campo."
)
CONFIGURATION_MOMENT = "durante a configuração dos seletores; nada foi gravado"
WINDOW_CLOSED_IN_CONFIGURATION = (
    "a janela do navegador foi fechada durante a configuração; nada foi gravado"
)


class ConfigError(Exception):
    """Something you have to fix before running again, such as the selectors; exit code 2."""


class ObserverStopped(Exception):
    """The run cannot go on; exit code 1."""


class BrowserError(Exception):
    """The browser failed. The message never carries a URL query or fragment."""


class ReadError(Exception):
    """Reading one selector failed."""


@dataclass(frozen=True)
class ElementText:
    """What a selector found: how many elements, whether the only one is visible, its text."""

    count: int
    visible: bool = False
    text: object = None  # the text, when exactly one visible element was found


@dataclass(frozen=True)
class FieldReading:
    """One field of an observation: the value, or None, and "ok: <selector>" or the reason.

    source is texto or ocr; captured_at is this computer's clock, in UTC, right before the
    capture; processing_ms is how long the capture and the reading took. All three are
    None when there was nothing to read. timings, for the visual reading only, splits that
    time into capture, detection and OCR.
    """

    value: object
    status: str
    source: object = None
    captured_at: object = None
    processing_ms: object = None
    timings: object = None  # QuoteTimings

    @property
    def available(self):
        return self.value is not None

    @property
    def reason(self):
        """Why the field is unavailable, or None when it is available."""
        return None if self.available else self.status.split(": ", 1)[1]


@dataclass(frozen=True)
class Observation:
    captured_at: datetime  # this computer's clock, in UTC, at the start of the observation
    page: str  # https://host/path, without user, port, query or fragment
    readings: dict  # field -> FieldReading, in the order of FIELDS
    duration_ms: object = None

    @property
    def valid(self):
        return any(reading.available for reading in self.readings.values())

    @property
    def completeness(self):
        """completa with asset, quote and payout available, parcial with some, vazia with none."""
        available = sum(reading.available for reading in self.readings.values())
        return COMPLETE if available == len(FIELDS) else PARTIAL if available else EMPTY


@dataclass(frozen=True)
class Capture:
    """What a capture of the chart found: how many elements, visibility, the image."""

    count: int
    visible: bool = False
    image: object = None  # quote_reader.RgbImage, in device pixels
    css_width: float = 0.0  # the element's width in CSS pixels
    clipped: bool = False  # part of the element was outside the window: no image
    # Milliseconds of each step, when the session measured them.
    element_ms: object = None  # finding the element: count, visibility and position
    screenshot_ms: object = None  # page.screenshot
    decode_ms: object = None  # decoding the PNG

    @property
    def scale(self):
        return self.image.width / self.css_width


@dataclass(frozen=True)
class QuoteTimings:
    """Milliseconds of each step of one visual reading; None for a step that did not run."""

    capture_ms: object = None
    detection_ms: object = None
    ocr_ms: object = None
    element_ms: object = None
    screenshot_ms: object = None
    decode_ms: object = None

    def describe(self):
        parts = []
        if self.capture_ms is not None:
            text = f"captura {self.capture_ms:.0f} ms"
            steps = [
                f"{name} {value:.0f}"
                for name, value in (
                    ("elemento", self.element_ms),
                    ("tela", self.screenshot_ms),
                    ("decodificação", self.decode_ms),
                )
                if value is not None
            ]
            if steps:
                text += f" ({', '.join(steps)})"
            parts.append(text)
        for name, value in (("detecção", self.detection_ms), ("OCR", self.ocr_ms)):
            if value is not None:
                parts.append(f"{name} {value:.0f} ms")
        return ", ".join(parts)


@dataclass(frozen=True)
class QuoteOcrSource:
    """The visual source of the quote: the chart element and its current-quote marker."""

    chart: str
    marker: object  # quote_reader.MarkerModel

    def describe(self):
        colors = ", ".join(quote_reader.color_hex(color) for color in self.marker.colors)
        return f"leitura visual (OCR) do marcador {colors}, no elemento {self.chart}"


@dataclass(frozen=True)
class TextMatch:
    """A visible element whose text looks like a quote, from the search of --configure."""

    text: str
    selector: object  # a selector that finds only this element, or None


class LazyEngine:
    """The OCR engine, created on first use; a missing OCR becomes a ConfigError."""

    def __init__(self, factory=RapidOcrEngine):
        self._factory = factory
        self._engine = None

    def get(self):
        if self._engine is None:
            try:
                self._engine = self._factory()
            except OcrUnavailable as error:
                raise ConfigError(str(error)) from error
        return self._engine


def show_image_file(path):
    """Open path in the image viewer of Windows; elsewhere, the path is only shown."""
    if os.name == "nt":
        try:
            os.startfile(path)  # noqa: S606 - opens a file the program itself wrote
        except OSError as error:
            warn(f"AVISO: não foi possível abrir {path} ({error}); abra-o você mesmo.")


@dataclass
class Context:
    """What the readings need besides the session; tests replace any of it."""

    now: object = None  # the UTC clock
    monotonic: object = time.perf_counter
    engine: object = None  # LazyEngine
    show_image: object = show_image_file
    runtime_dir: object = None

    def __post_init__(self):
        self.now = utc_now if self.now is None else self.now
        self.engine = LazyEngine() if self.engine is None else self.engine
        self.runtime_dir = RUNTIME_DIR if self.runtime_dir is None else self.runtime_dir

    def elapsed_ms(self, start):
        return round((self.monotonic() - start) * 1000, 1)


@dataclass
class Progress:
    """What this run did so far."""

    observations: int = 0
    valid: int = 0
    complete: int = 0
    csv: object = None  # the CsvLog, created with the first valid observation


def main(
    argv=None, *, open_session=None, read_line=input, now=None, monotonic=None, engine=None, show_image=None
):
    """Run the collector and return its exit code.

    The keyword arguments exist for tests. They replace, in this order,
    open_playwright_session, input, the UTC clock, time.perf_counter, the LazyEngine of the
    OCR and the opening of the preview images. The wait between two observations is the
    session's own wait, page.wait_for_timeout in PlaywrightSession, so a fake session brings
    a fake wait.
    """
    args = parse_arguments(argv)
    open_session = open_playwright_session if open_session is None else open_session
    ctx = Context(now=now, engine=engine)
    if monotonic is not None:
        ctx.monotonic = monotonic
    if show_image is not None:
        ctx.show_image = show_image
    runtime_dir = ctx.runtime_dir
    profile_dir = runtime_dir / PROFILE_DIR_NAME
    selectors_path = runtime_dir / SELECTORS_FILE_NAME
    progress = Progress()
    session = None
    interrupted = False
    try:
        say(
            "COLETOR SOMENTE LEITURA do PocketAutonomo: não envia ordens, não clica em CALL/PUT"
            " e não faz operações financeiras."
        )
        url = args.url
        if args.configure:
            say(
                "Modo de configuração: você informa o link, faz o login e escolhe os seletores"
                " no terminal; depois vêm as observações."
            )
            if url is None:
                url = ask_url(read_line)
        print_plan(url, args, profile_dir, selectors_path)
        if not args.configure and create_selectors_file(selectors_path):
            say(
                f"Criei {selectors_path} com os campos vazios; preencha-o antes de confirmar"
                " o login, ou rode com --configure para escolher os seletores no terminal."
            )
        session = open_session(profile_dir)
        open_page(session, url)
        wait_for_login(read_line, selectors_path, args.configure)
        if session.is_closed():
            raise ObserverStopped("a janela do navegador foi fechada antes da confirmação do login")
        if args.configure:
            selectors = configure_selectors(session, selectors_path, read_line, ctx)
        else:
            selectors = load_selectors(selectors_path)
        announce_selectors(selectors, selectors_path)
        if isinstance(selectors["price"], QuoteOcrSource):
            ctx.engine.get()  # a missing OCR stops the run before the first observation
        run_observations(session, selectors, args, ctx, progress)
        return finish(progress)
    except ConfigError as error:
        warn(f"CONFIGURAÇÃO: {error}")
        return EXIT_CONFIG
    except (ObserverStopped, BrowserError, OSError) as error:
        warn(f"PARADA: {error}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        interrupted = True
        report_interruption(progress)
        return EXIT_INTERRUPTED
    finally:
        if progress.csv is not None:
            progress.csv.close()
        close_session(session, interrupted)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.observe",
        description=(
            "Coletor SOMENTE LEITURA da tela de operações da Pocket Option: abre o navegador"
            " para você fazer o login e lê ativo, cotação do ativo e payout pelos seletores"
            " que você conferiu. Não envia ordens, não clica em CALL/PUT e não faz operações."
        ),
    )
    parser.add_argument(
        "--url",
        type=official_url,
        help=f"link oficial da tela de operações, em https://{ALLOWED_DOMAIN}; obrigatório"
        " sem --configure",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="pede no terminal o link (sem --url) e os seletores, mostra a prévia de cada"
        f" um e grava {SELECTORS_FILE_NAME} em runtime/; depois faz as observações",
    )
    parser.add_argument(
        "--interval",
        type=interval_seconds,
        default=DEFAULT_INTERVAL,
        help=f"segundos entre uma leitura e a próxima (padrão {DEFAULT_INTERVAL:g},"
        f" mínimo {MIN_INTERVAL:g})",
    )
    parser.add_argument(
        "--count",
        type=positive_int,
        help="quantas observações fazer; sem esta opção, lê até Ctrl+C",
    )
    return parser


def parse_arguments(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.url is None and not args.configure:
        parser.error("informe --url, ou use --configure para colar o link no terminal")
    return args


def official_url(text):
    """argparse type of --url. The message never repeats the URL, which may carry a secret."""
    url = text.strip()
    problem = url_problem(url)
    if problem is not None:
        raise argparse.ArgumentTypeError(
            f"{problem}; use o link oficial da tela de operações, em https://{ALLOWED_DOMAIN}/..."
        )
    return url


def url_problem(url):
    """Why url is not an https page on pocketoption.com or a subdomain, or None when it is."""
    if any(char.isspace() or not char.isprintable() for char in url):
        return "a URL tem espaço ou caractere de controle"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "a URL é inválida"
    if parts.scheme != "https":
        return "a URL precisa começar com https://"
    if parts.username is not None or parts.password is not None:
        return "a URL não pode trazer usuário ou senha"
    if port not in (None, 443):
        return "a URL não pode indicar outra porta"
    if host is None or not HOST_PATTERN.fullmatch(host):
        return f"o endereço precisa ser {ALLOWED_DOMAIN} ou um subdomínio dele"
    return None


def display_url(url):
    """url without user, port, query and fragment; only the scheme and host for other pages."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "(URL inválida)"
    if url_problem(url) is None:
        return f"https://{host}{parts.path or '/'}"
    return f"{parts.scheme}://{host}" if host else f"{parts.scheme}:"


def redact(text):
    """text with the query and the fragment of every URL in it removed."""
    return URL_TAIL_PATTERN.sub(r"\1", text)


def interval_seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(
            f"precisa ser um número de segundos a partir de {MIN_INTERVAL:g}, não {text!r}"
        )
    return value


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"precisa ser um inteiro positivo, não {text!r}")
    return value


def utc_now():
    return datetime.now(timezone.utc)


def create_selectors_file(path):
    """Create path with empty selectors when it does not exist; return True when created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as file:
            json.dump(SELECTORS_TEMPLATE, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except FileExistsError:
        return False
    return True


def load_selectors(path):
    """The sources in path, as {field: selector or None}; a missing file has none.

    The quote's entry is a QuoteOcrSource when the file has price_ocr. Keys that start with
    _ are comments. Any other key besides asset, price, payout and price_ocr, a value that
    is not text or null, a price_ocr that is not valid, a price_ocr together with a price
    selector, or invalid JSON raises ConfigError.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")  # also accepts a BOM from Windows editors
    except FileNotFoundError:
        return {field: None for field in FIELDS}
    except OSError as error:
        raise ConfigError(f"não foi possível ler {path}: {error}") from error
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ConfigError(f"{path} não é um JSON válido: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f'{path} precisa ser um objeto JSON, como {{"asset": "..."}}')
    known = set(FIELDS) | {OCR_KEY}
    unknown = sorted(key for key in data if key not in known and not key.startswith("_"))
    if unknown:
        raise ConfigError(
            f"{path} tem chave desconhecida ({', '.join(unknown)}); use só asset, price,"
            f" payout e {OCR_KEY}"
        )
    selectors = {}
    for field in FIELDS:
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"{path}: o valor de {field} precisa ser texto")
        selectors[field] = (value or "").strip() or None
    if data.get(OCR_KEY) is not None:
        if selectors["price"]:
            raise ConfigError(
                f"{path} tem duas fontes para a cotação do ativo, price e {OCR_KEY}; deixe só"
                " uma, ou rode com --configure para escolher"
            )
        selectors["price"] = quote_source_from_json(data[OCR_KEY], path)
    return selectors


def quote_source_from_json(data, path):
    if not isinstance(data, dict) or set(data) - {"chart", "marker"}:
        raise ConfigError(f'{path}: {OCR_KEY} precisa ser um objeto com "chart" e "marker"')
    chart = data.get("chart")
    if not isinstance(chart, str) or not chart.strip():
        raise ConfigError(f"{path}: {OCR_KEY}.chart precisa ser o seletor do elemento do gráfico")
    try:
        marker = quote_reader.MarkerModel.from_json(data.get("marker"))
    except ValueError as error:
        raise ConfigError(f"{path}: {OCR_KEY}.marker inválido: {error}") from error
    return QuoteOcrSource(chart.strip(), marker)


def describe_source(source):
    if isinstance(source, QuoteOcrSource):
        return source.describe()
    return f"seletor {source}"


def announce_selectors(selectors, selectors_path):
    configured = [
        LABELS[field] + (" (leitura visual)" if isinstance(selectors[field], QuoteOcrSource) else "")
        for field in FIELDS
        if selectors[field]
    ]
    missing = [LABELS[field] for field in FIELDS if not selectors[field]]
    if not configured:
        raise ConfigError(
            f"nenhum seletor preenchido em {selectors_path}, então nenhum campo pode ser lido."
            " Rode com --configure (no Windows, observar_windows.cmd) para escolher os"
            " seletores no terminal, ou preencha o arquivo como explica o INTEGRATION.md."
        )
    line = f"Campos com fonte: {', '.join(configured)}"
    if missing:
        line += f" | sem fonte, ficam indisponíveis: {', '.join(missing)}"
    say(line)


def parse_asset(text):
    """(asset, None) for a one-line name with letters, or (None, reason)."""
    stripped = text.strip()
    if not stripped:
        return None, "o elemento está sem texto"
    if "\n" in stripped or "\r" in stripped:
        return None, f"o texto tem mais de uma linha: {preview(text)}"
    value = " ".join(stripped.split())
    if len(value) > MAX_ASSET_LENGTH:
        return None, f"texto longo demais para um ativo ({len(value)} caracteres): {preview(text)}"
    if not any(char.isalpha() for char in value):
        return None, f"texto sem letras, não parece um ativo: {preview(text)}"
    if value.startswith(FORMULA_PREFIXES):
        return None, f"o texto começa com {value[0]!r}, que uma planilha leria como fórmula"
    return value, None


def parse_price(text):
    """(quote, None) for digits with at least one decimal separator, or (None, reason).

    The quote is kept exactly as shown, because the platform's number format was not
    verified. A whole number is refused: quotes have decimals, and a whole number, such as
    10, may be the bet amount (Amount).
    """
    value = text.strip()
    if PRICE_PATTERN.fullmatch(value):
        return value, None
    if INTEGER_PATTERN.fullmatch(value):
        return None, (
            f"número sem casas decimais ({preview(value)}): a cotação do ativo tem casas"
            " decimais, como 1.08534, e um valor inteiro pode ser o da aposta (Amount)"
        )
    return None, f"texto não reconhecido como cotação do ativo: {preview(text)}"


def parse_payout(text):
    """(number, None) for a percentage such as +92% or 92 %, or (None, reason)."""
    match = PAYOUT_PATTERN.fullmatch(text.strip())
    if match:
        return match.group(1), None
    return None, f"texto não reconhecido como payout em %: {preview(text)}"


PARSERS = {"asset": parse_asset, "price": parse_price, "payout": parse_payout}


def preview(text, limit=40):
    return repr(text if len(text) <= limit else text[:limit] + "...")


def unavailable(reason):
    return FieldReading(None, f"{UNAVAILABLE}: {reason}")


def observe_once(session, selectors, ctx):
    """Read every field once, but only from a page on pocketoption.com."""
    start = ctx.monotonic()
    captured_at = ctx.now()
    url = session.current_url()
    page = display_url(url)
    if url_problem(url) is not None:
        reading = unavailable(
            f"a página atual ({page}) não está em {ALLOWED_DOMAIN}; nada foi lido"
        )
        readings = {field: reading for field in FIELDS}
    else:
        readings = {field: read_field(session, field, selectors.get(field), ctx) for field in FIELDS}
    return Observation(captured_at, page, readings, ctx.elapsed_ms(start))


def read_field(session, field, source, ctx):
    """The FieldReading of field from its source, with the capture moment and duration."""
    if not source:
        return unavailable(f"sem seletor em {SELECTORS_FILE_NAME}")
    start = ctx.monotonic()
    captured_at = ctx.now()
    timings = None
    if isinstance(source, QuoteOcrSource):
        visual = read_quote_visually(session, source, ctx)
        reading, timings = visual.reading, visual.timings
        kind = OCR_SOURCE
    else:
        reading = read_text_field(session, field, source)
        kind = TEXT_SOURCE
    return replace(
        reading,
        source=kind,
        captured_at=captured_at,
        processing_ms=ctx.elapsed_ms(start),
        timings=timings,
    )


def read_text_field(session, field, selector):
    try:
        element = session.read(selector, READ_TIMEOUT_MS)
    except ReadError as error:
        return unavailable(f"a leitura do seletor falhou: {error}")
    if element.count == 0:
        return unavailable("o seletor não encontrou nenhum elemento")
    if element.count > 1:
        return unavailable(
            f"o seletor encontrou {element.count} elementos; precisa encontrar exatamente 1"
        )
    if not element.visible:
        return unavailable("o elemento encontrado não está visível")
    value, problem = PARSERS[field](element.text)
    if problem is not None:
        return unavailable(problem)
    return FieldReading(value, f"{OK}: {selector}")


def capture_chart(session, chart):
    """(Capture, None) of the chart element, or (None, reason)."""
    try:
        capture = session.capture(chart, CAPTURE_TIMEOUT_MS)
    except ReadError as error:
        return None, f"a captura do gráfico falhou: {error}"
    if capture.count == 0:
        return None, f"o seletor do gráfico ({chart}) não encontrou nenhum elemento"
    if capture.count > 1:
        return None, (
            f"o seletor do gráfico ({chart}) encontrou {capture.count} elementos; precisa"
            " encontrar exatamente 1"
        )
    if capture.clipped:
        return None, (
            f"o elemento do gráfico ({chart}) não cabe inteiro na área visível da janela;"
            " maximize a janela do navegador ou role a página até o gráfico aparecer inteiro"
        )
    if not capture.visible or capture.image is None or capture.css_width <= 0:
        return None, f"o elemento do gráfico ({chart}) não está visível"
    return capture, None


@dataclass(frozen=True)
class VisualReading:
    """One visual reading of the quote: the field, the capture, the marker and the times."""

    reading: FieldReading
    capture: object = None  # Capture, when the chart was captured
    box: object = None  # quote_reader.Box of the marker, when it was found
    timings: object = None  # QuoteTimings


def read_quote_visually(session, source, ctx):
    """The VisualReading of the quote, with capture, detection and OCR timed apart."""
    start = ctx.monotonic()
    capture, problem = capture_chart(session, source.chart)
    capture_ms = ctx.elapsed_ms(start)
    steps = {}
    if capture is not None:
        steps = {
            "element_ms": capture.element_ms,
            "screenshot_ms": capture.screenshot_ms,
            "decode_ms": capture.decode_ms,
        }
    if capture is None:
        return VisualReading(unavailable(problem), None, None, QuoteTimings(capture_ms, **steps))
    start = ctx.monotonic()
    box, reason = quote_reader.locate_marker(capture.image, capture.scale, source.marker)
    detection_ms = ctx.elapsed_ms(start)
    if box is None:
        timings = QuoteTimings(capture_ms, detection_ms, **steps)
        return VisualReading(unavailable(reason), capture, None, timings)
    start = ctx.monotonic()
    found = quote_reader.read_marker_text(capture.image, box, ctx.engine.get())
    timings = QuoteTimings(capture_ms, detection_ms, ctx.elapsed_ms(start), **steps)
    if found.value is None:
        return VisualReading(unavailable(found.reason), capture, box, timings)
    value, problem = parse_price(found.value)
    if problem is not None:
        return VisualReading(unavailable(problem), capture, box, timings)
    reading = FieldReading(value, f"{OK}: {OCR_SOURCE} {source.chart}")
    return VisualReading(reading, capture, box, timings)


def run_observations(session, selectors, args, ctx, progress):
    """Observe until args.count observations were made, the window closes or Ctrl+C.

    Between two observations, session.wait waits args.interval seconds. In PlaywrightSession
    it is page.wait_for_timeout, which, unlike time.sleep, lets Playwright keep processing
    the browser's events. Each value is still read at one moment and can change right after.
    """
    number = 0
    while args.count is None or number < args.count:
        if session.is_closed():
            say("A janela do navegador foi fechada; a leitura terminou.")
            return
        number += 1
        observation = observe_once(session, selectors, ctx)
        progress.observations += 1
        if observation.valid:
            if progress.csv is None:
                progress.csv = CsvLog(new_csv_path(ctx.runtime_dir))
                say(f"CSV: {progress.csv.path}")
            progress.csv.write(observation)
            progress.valid += 1
            progress.complete += observation.completeness == COMPLETE
        say(observation_line(number, observation))
        if args.count is None or number < args.count:
            session.wait(args.interval)


class CsvLog:
    """A new CSV file with CSV_COLUMNS, flushed after every line."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "x", newline="", encoding="utf-8")
        try:
            self._writer = csv.writer(self._file)
            self._writer.writerow(CSV_COLUMNS)
            self._file.flush()
        except BaseException:
            self._file.close()
            raise

    def write(self, observation):
        self._writer.writerow(csv_row(observation))
        self._file.flush()

    def close(self):
        self._file.close()


def csv_row(observation):
    row = [utc_text(observation.captured_at), observation.page]
    for field in FIELDS:
        reading = observation.readings[field]
        row += [reading.value if reading.available else "", reading.status]
    price = observation.readings["price"]
    row += [
        observation.completeness,
        price.source or "",
        utc_text(price.captured_at) if price.captured_at else "",
        "" if price.processing_ms is None else f"{price.processing_ms:.1f}",
        "" if observation.duration_ms is None else f"{observation.duration_ms:.1f}",
    ]
    timings = price.timings or QuoteTimings()
    row += [
        "" if value is None else f"{value:.1f}"
        for value in (timings.capture_ms, timings.detection_ms, timings.ocr_ms)
    ]
    return row


def utc_text(moment):
    return moment.isoformat(timespec="milliseconds")


def new_csv_path(runtime_dir):
    """A new CSV path with a unique name in runtime_dir, which is created if needed."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        name = f"observations-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csv"
        path = runtime_dir / name
        if not path.exists():
            return path


KIND_LABELS = {COMPLETE: "COMPLETA", PARTIAL: "PARCIAL", EMPTY: "SEM CAMPOS"}
SOURCE_LABELS = {TEXT_SOURCE: "texto", OCR_SOURCE: "leitura visual"}


def observation_line(number, observation):
    parts = [
        f"OBSERVAÇÃO {number} {KIND_LABELS[observation.completeness]}",
        f"relógio local {utc_text(observation.captured_at)}",
        observation.page,
    ]
    for field in FIELDS:
        reading = observation.readings[field]
        text = f"{LABELS[field]} {shown_value(field, reading)}"
        # The time of a visual reading is shown even when it failed, to find what is slow.
        timed = reading.available or reading.timings is not None
        if field == "price" and reading.source and reading.processing_ms is not None and timed:
            detail = f"{SOURCE_LABELS[reading.source]}, {reading.processing_ms:.0f} ms"
            if reading.timings is not None:
                detail += f": {reading.timings.describe()}"
            text += f" ({detail})"
        parts.append(text)
    parts.append("gravada no CSV" if observation.valid else "não gravada: nenhum campo disponível")
    return " | ".join(parts)


def shown_value(field, reading):
    """How the terminal shows a reading: its value, or INDISPONÍVEL with the reason."""
    if not reading.available:
        return f"INDISPONÍVEL ({reading.reason})"
    return f"{reading.value}%" if field == "payout" else reading.value


def print_plan(url, args, profile_dir, selectors_path):
    say(f"Página: {display_url(url)} (parâmetros e fragmento da URL não são exibidos)")
    say(f"Perfil do navegador: {profile_dir} (fora do Git; guarda a sua sessão, não compartilhe)")
    say(f"Seletores: {selectors_path}")
    plan = "até Ctrl+C" if args.count is None else count(args.count, "observação", "observações")
    say(
        f"Leitura: {plan}, uma a cada {args.interval:g} s; horário de captura em UTC, pelo"
        " relógio deste computador, que não é o horário do servidor da Pocket Option."
        " Riscos e limitações: INTEGRATION.md."
    )


def open_page(session, url):
    say(f"Abrindo {display_url(url)} no navegador.")
    try:
        session.open(url)
    except BrowserError as error:
        warn(
            f"AVISO: a abertura da página não terminou ({error}). Confira a janela do"
            " navegador; você pode chegar à tela de operações por ela mesma."
        )


def ask(read_line, prompt, moment):
    """One line typed in the terminal. The end of the input stops the run."""
    try:
        return read_line(prompt)
    except EOFError:
        raise ObserverStopped(f"a entrada do terminal terminou {moment}") from None


def ask_url(read_line):
    """Ask for the link until official_url accepts it; the link is never repeated."""
    say(f"Cole o link oficial da sua tela de operações, em https://{ALLOWED_DOMAIN}/...")
    while True:
        text = ask(read_line, "Link: ", "antes do link da tela de operações")
        try:
            return official_url(text)
        except argparse.ArgumentTypeError as error:
            say(f"Link recusado ({error}). Cole de novo, ou use Ctrl+C para sair.")


def wait_for_login(read_line, selectors_path, configure):
    say(
        "Faça o login você mesmo, na janela do navegador. O programa não lê, não grava e não"
        " mostra senha, cookies ou tokens."
    )
    if configure:
        prompt = (
            "Com a tela de operações visível, pressione Enter aqui para escolher os seletores"
            " (Ctrl+C cancela). "
        )
    else:
        prompt = (
            f"Com a tela de operações visível e {selectors_path} preenchido, pressione Enter"
            " aqui para começar a leitura (Ctrl+C cancela). "
        )
    ask(read_line, prompt, "antes da confirmação do login")


def configure_selectors(session, path, read_line, ctx):
    """Ask for each field's source, preview it, and write the file once you confirm.

    Enter keeps the current source, so the ones already saved can be reused. Nothing is
    written until the final Enter, and json writes the file, escaping what is needed.
    """
    selectors, invalid = current_selectors(path)
    say("")
    say("CONFIGURAÇÃO DOS SELETORES")
    say(
        "Para cada campo, na janela do navegador: F12 abre o DevTools e Ctrl+Shift+C liga a"
        " inspeção. Clique no elemento e, no painel Elements, use Copy > Copy selector. Cole"
        " aqui o seletor como veio, sem escapar barras nem aspas."
    )
    say(
        "O programa lê cada seletor na hora e mostra o que encontrou. Não clique nos botões"
        " CALL e PUT: o programa não clica, mas um clique seu fora da inspeção é de verdade."
    )
    for field in FIELDS:
        selectors[field] = ask_source(session, field, selectors[field], read_line, ctx)
    while True:
        show_summary(session, selectors, ctx)
        answer = ask(
            read_line,
            "Enter grava os seletores e começa as observações; para corrigir um campo, digite"
            " ativo, cotação ou payout: ",
            CONFIGURATION_MOMENT,
        )
        if not answer.strip():
            break
        field = FIELD_ANSWERS.get(answer.strip().lower())
        if field is None:
            say(
                f"Resposta não reconhecida: {preview(answer.strip())}. Use ativo, cotação ou"
                " payout."
            )
            continue
        selectors[field] = ask_source(session, field, selectors[field], read_line, ctx)
    if not any(selectors.values()):
        raise ConfigError(f"nenhum seletor foi escolhido, e {path} não foi alterado")
    while True:
        try:
            save_selectors(path, selectors, invalid)
            break
        except OSError as error:
            warn(f"AVISO: não foi possível gravar {path} ({describe(error)}).")
            answer = ask(
                read_line,
                "Enter tenta gravar de novo; - desiste sem gravar: ",
                CONFIGURATION_MOMENT,
            ).strip()
            if answer == "-":
                raise ObserverStopped(f"os seletores não foram gravados em {path}") from None
    say(f"Seletores gravados em {path}.")
    return selectors


def current_selectors(path):
    """The selectors already in path, and whether the file is invalid."""
    try:
        return load_selectors(path), False
    except ConfigError as error:
        warn(
            f"AVISO: {error}. Os seletores serão pedidos do zero. O arquivo atual só muda"
            " quando você gravar os novos, e antes disso ele é guardado como cópia."
        )
        return {field: None for field in FIELDS}, True


def ask_source(session, field, current, read_line, ctx):
    if field == "price":
        return ask_quote_source(session, current, read_line, ctx)
    return ask_selector(session, field, current, read_line, ctx)


def ask_selector(session, field, current, read_line, ctx):
    """The selector you accept for field after seeing its preview, or None for none."""
    label, of_label = LABELS[field], OF_LABELS[field]
    if current:
        say(f"{label.capitalize()}: seletor atual {current}")
        prompt = f"Seletor {of_label} (Enter mantém o atual; - deixa sem seletor): "
    else:
        say(f"{label.capitalize()}: ainda sem seletor.")
        prompt = f"Seletor {of_label} (Enter deixa sem seletor): "
    selector = ask(read_line, prompt, CONFIGURATION_MOMENT).strip() or current
    while selector and selector != "-":
        reading = preview_reading(session, field, selector, ctx)
        line = f"  Prévia {of_label}, lida agora: {shown_value(field, reading)}"
        if reading.available:
            line += ". Confira se é o que a tela mostra."
        say(line)
        answer = ask(
            read_line,
            f"Enter aceita este seletor {of_label}; cole outro para trocar; - deixa sem"
            " seletor: ",
            CONFIGURATION_MOMENT,
        ).strip()
        if not answer:
            return selector
        selector = answer
    say(f"{label.capitalize()}: sem seletor, o campo fica INDISPONÍVEL.")
    return None


QUOTE_PROMPT = (
    "Fonte da cotação: cole um seletor, digite buscar para procurar a cotação em texto na"
    " página, ou ocr para lê-la no gráfico"
)
VISUAL_INTRO = (
    "O programa captura só o elemento do gráfico, procura na metade direita dele as caixas"
    " com texto, como o marcador da cotação, numera essas caixas numa imagem e lê cada uma"
    " com o OCR local. Você escolhe a que acompanha a cotação atual; um candidato sem cotação"
    " legível não pode ser escolhido. O programa guarda as cores do preenchimento, o tamanho"
    " e o alinhamento à borda direita desse marcador, nunca a altura dele: a cada leitura, o"
    " marcador é procurado de novo. Cada captura gera uma imagem nova em runtime/, sem"
    " sobrescrever as anteriores. Deixe a janela do navegador visível e o mouse fora do"
    " gráfico, para que a etiqueta do cursor não apareça."
)
COMPARE_QUOTE = (
    "Compare cada prévia com a cotação que a tela mostrava naquele instante. A leitura visual"
    " não comprova sozinha que o marcador escolhido é o da cotação atual: só essa comparação,"
    " feita por você, valida o campo."
)
MARKER_AGAIN = "marcador"  # review_visual_source: choose the marker again


def ask_quote_source(session, current, read_line, ctx):
    """The quote's source you accept: a selector, a QuoteOcrSource, or None for none."""
    say(QUOTE_NOTE)
    if current is None:
        say("Cotação do ativo: ainda sem fonte.")
        keep = "Enter deixa sem fonte"
    else:
        say(f"Cotação do ativo: fonte atual: {describe_source(current)}.")
        keep = "Enter mantém a atual; - deixa sem fonte"
    again = f"{QUOTE_PROMPT} (Enter deixa sem fonte): "
    answer = ask(read_line, f"{QUOTE_PROMPT} ({keep}): ", CONFIGURATION_MOMENT).strip()
    choice = answer or current
    results = []
    while True:
        if isinstance(choice, QuoteOcrSource):
            outcome = review_visual_source(session, choice, read_line, ctx)
            if outcome == MARKER_AGAIN:
                outcome = configure_visual_source(session, choice, read_line, ctx)
            if outcome is not None:
                return outcome
            choice = ask(read_line, again, CONFIGURATION_MOMENT).strip()
            continue
        if not choice or choice == "-":
            say("Cotação do ativo: sem fonte, o campo fica INDISPONÍVEL.")
            return None
        command = choice.lower()
        if command == "buscar":
            results = search_quote_texts(session)
            follow = f"{QUOTE_PROMPT}, ou o número de um resultado (Enter deixa sem fonte): "
            choice = ask(read_line, follow if results else again, CONFIGURATION_MOMENT).strip()
            continue
        if command == "ocr":
            previous = current if isinstance(current, QuoteOcrSource) else None
            source = configure_visual_source(session, previous, read_line, ctx)
            if source is not None:
                return source
            choice = ask(read_line, again, CONFIGURATION_MOMENT).strip()
            continue
        if choice.isdigit() and results:
            number = int(choice)
            if not 1 <= number <= len(results) or results[number - 1].selector is None:
                say("  Esse número não é de um resultado com seletor único.")
                choice = ask(read_line, again, CONFIGURATION_MOMENT).strip()
                continue
            choice = results[number - 1].selector
            say(f"  Seletor do resultado {number}: {choice}")
        reading = preview_reading(session, "price", choice, ctx)
        line = f"  Prévia da cotação do ativo, lida agora: {shown_value('price', reading)}"
        if reading.available:
            line += ". Confira se é o que a tela mostra."
        say(line)
        answer = ask(
            read_line,
            "Enter aceita este seletor da cotação do ativo; cole outro, buscar ou ocr para"
            " trocar; - deixa sem fonte: ",
            CONFIGURATION_MOMENT,
        ).strip()
        if not answer:
            return choice
        choice = answer


def search_quote_texts(session):
    """List the visible text elements that look like a quote; nothing is chosen by itself."""
    try:
        matches = session.find_quote_texts(SEARCH_RESULTS)
    except ReadError as error:
        say(f"  A busca falhou: {error}")
        return []
    if not matches:
        say(
            "  Nenhum elemento de texto visível da página tem um número com casas decimais."
            " A busca vale para o documento principal, não para iframes nem para o que é"
            " desenhado em canvas. Se a cotação só aparece no gráfico, use ocr."
        )
        return []
    say(
        "  Elementos de texto visíveis com números parecidos com uma cotação, lidos agora."
        " Compare com a tela; nenhum é escolhido sozinho:"
    )
    for number, match in enumerate(matches, 1):
        where = f"seletor {match.selector}" if match.selector else "sem seletor único; use o DevTools nele"
        say(f"  {number}: {match.text} | {where}")
    return matches


def configure_visual_source(session, previous, read_line, ctx):
    """Choose the chart element and its current-quote marker: a QuoteOcrSource, or None."""
    say("")
    say("LEITURA VISUAL DA COTAÇÃO")
    say(VISUAL_INTRO)
    ctx.engine.get()  # a missing OCR stops here, with the installation hint
    chart = previous.chart if previous is not None else None
    keep = f"Enter mantém {chart}; - desiste" if chart else "- desiste"
    answer = ask(
        read_line,
        f"Seletor do elemento do gráfico, o que o DevTools mostra sobre o marcador da cotação ({keep}): ",
        CONFIGURATION_MOMENT,
    ).strip()
    if answer == "-" or not (answer or chart):
        say("Leitura visual não configurada.")
        return None
    chart = answer or chart
    while True:
        if session.is_closed():
            raise ObserverStopped(WINDOW_CLOSED_IN_CONFIGURATION)
        capture, problem = capture_chart(session, chart)
        if capture is None:
            say(f"  {problem}.")
            answer = ask(
                read_line,
                "Enter captura de novo; cole outro seletor do gráfico; - desiste: ",
                CONFIGURATION_MOMENT,
            ).strip()
            if answer == "-":
                say("Leitura visual não configurada.")
                return None
            chart = answer or chart
            continue
        boxes = quote_reader.candidate_boxes(capture.image, capture.scale)
        engine = ctx.engine.get()
        readings = [quote_reader.read_marker_text(capture.image, box, engine) for box in boxes]
        shown = save_preview(
            ctx, CANDIDATES_PREFIX, capture.image, list(enumerate(boxes, 1)), read_line,
            "Imagem da captura do gráfico, com os candidatos numerados",
        )
        if shown == PREVIEW_GIVE_UP:
            say("Leitura visual não configurada.")
            return None
        if not boxes:
            say(
                "  Nenhuma caixa com texto apareceu na metade direita do gráfico. Confira, na"
                " imagem, se o elemento inclui a borda direita, onde fica o marcador, e se a"
                " janela do navegador está visível."
            )
            answer = ask(
                read_line,
                "Enter captura de novo; cole outro seletor do gráfico; - desiste: ",
                CONFIGURATION_MOMENT,
            ).strip()
            if answer == "-":
                say("Leitura visual não configurada.")
                return None
            chart = answer or chart
            continue
        say("  Candidatos, de cima para baixo; compare com a tela e com a imagem:")
        for number, (box, reading) in enumerate(zip(boxes, readings), 1):
            if reading.value:
                text = reading.value
            else:
                text = f"nenhuma cotação legível ({reading.reason}); não pode ser escolhido"
            gap = (capture.image.width - box.x1) / capture.scale
            cut = "; cortado pela borda" if box.cut else ""
            say(
                f"  {number}: caixa {quote_reader.color_hex(box.color)} de"
                f" {box.width / capture.scale:.0f}x{box.height / capture.scale:.0f} px, a"
                f" {gap:.0f} px da borda direita{cut}; texto lido: {text}"
            )
        answer = ask(
            read_line,
            "Número do marcador da cotação atual, o que acompanha a cotação (não o do cursor"
            " nem o de uma operação). Enter captura de novo; - desiste: ",
            CONFIGURATION_MOMENT,
        ).strip()
        if answer == "-":
            say("Leitura visual não configurada.")
            return None
        if not answer:
            continue
        if not (answer.isdigit() and 1 <= int(answer) <= len(boxes)):
            say(f"  {preview(answer)} não é um dos números da lista.")
            continue
        number = int(answer)
        box, reading = boxes[number - 1], readings[number - 1]
        if box.cut:
            say("  Esse candidato está cortado pela borda da captura; capture de novo com ele inteiro.")
            continue
        if reading.value is None or parse_price(reading.value)[1] is not None:
            say(
                f"  O candidato {number} não tem uma cotação legível"
                f" ({reading.reason or parse_price(reading.value)[1]}). Uma calibração precisa"
                " de uma leitura válida do marcador: escolha outro número, ou pressione Enter"
                " para capturar de novo."
            )
            continue
        marker = quote_reader.MarkerModel.from_box(box, capture.image, capture.scale)
        if previous is not None and previous.chart == chart:
            # The colors already saved come first; the size and the gap are the new box's.
            kept = replace(marker, colors=previous.marker.colors)
            merged = kept.with_colors(marker.colors)
            if merged is not kept:  # the new box has a color that the saved ones do not cover
                add = ask(
                    read_line,
                    "Enter usa só as cores deste marcador; + soma a elas as cores do marcador"
                    " já gravado, para quando a cor muda: ",
                    CONFIGURATION_MOMENT,
                ).strip()
                if add == "+":
                    marker = merged
        source = QuoteOcrSource(chart, marker)
        outcome = review_visual_source(session, source, read_line, ctx)
        if outcome == MARKER_AGAIN:
            previous = source
            continue
        return outcome


def review_visual_source(session, source, read_line, ctx):
    """Preview source a few times: the source when accepted, None to give up, or MARKER_AGAIN.

    The visual reading can be accepted only when every preview read a quote: a calibration
    whose previews were unavailable is never accepted.
    """
    while True:
        say(
            f"  Prévia da leitura visual: {PREVIEW_READINGS} leituras, uma por segundo. Olhe a"
            " cotação na tela a cada uma."
        )
        last, valid = None, 0
        for number in range(1, PREVIEW_READINGS + 1):
            if number > 1:
                session.wait(1.0)
            if session.is_closed():
                raise ObserverStopped(WINDOW_CLOSED_IN_CONFIGURATION)
            start, moment = ctx.monotonic(), ctx.now()
            visual = read_quote_visually(session, source, ctx)
            say(
                f"  Prévia {number}, relógio local {utc_text(moment)}:"
                f" {shown_value('price', visual.reading)} (em {ctx.elapsed_ms(start):.0f} ms:"
                f" {visual.timings.describe()})"
            )
            valid += visual.reading.available
            last = visual
        if last.capture is not None:
            boxes = [(1, last.box)] if last.box else []
            shown = save_preview(
                ctx, PREVIEW_PREFIX, last.capture.image, boxes, read_line,
                "Imagem da última prévia, com o marcador lido destacado como 1",
            )
            if shown == PREVIEW_GIVE_UP:
                say("Leitura visual não configurada.")
                return None
        if valid == PREVIEW_READINGS:
            say(COMPARE_QUOTE)
            prompt = (
                "Enter aceita a leitura visual; r repete a prévia; n escolhe o marcador de"
                " novo; - desiste: "
            )
        else:
            say(
                f"  {PREVIEW_READINGS - valid} de {PREVIEW_READINGS} prévias ficaram"
                " INDISPONÍVEIS: a leitura visual não pode ser aceita assim. Tire o mouse do"
                " gráfico e repita, ou escolha o marcador de novo. O marcador é"
                " semitransparente: se ele estava sobre uma vela de outra cor, escolha-o de novo"
                " com n e some a cor nova com +."
            )
            prompt = "Enter ou r repete a prévia; n escolhe o marcador de novo; - desiste: "
        answer = ask(read_line, prompt, CONFIGURATION_MOMENT).strip().lower()
        if not answer and valid == PREVIEW_READINGS:
            return source
        if answer in ("", "r"):
            continue
        if answer == "n":
            return MARKER_AGAIN
        if answer == "-":
            say("Leitura visual não configurada.")
            return None
        say(f"  Resposta não reconhecida: {preview(answer)}.")


def new_preview_path(runtime_dir, prefix):
    """A new image path with a unique name in runtime_dir, which is created if needed."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        name = f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.png"
        path = runtime_dir / name
        if not path.exists():
            return path


def write_preview(ctx, prefix, image, labelled_boxes):
    """Write a preview PNG to a new file in runtime/ and return its path.

    Each image gets a new name and is created with "x", so no file is ever overwritten: an
    earlier image may still be open in a viewer, and on Windows a file open there cannot be
    truncated. The capture stays out of every other file.
    """
    data = quote_reader.annotated_png(image, labelled_boxes)
    path = new_preview_path(ctx.runtime_dir, prefix)
    with open(path, "xb") as file:
        file.write(data)
    return path


def save_preview(ctx, prefix, image, labelled_boxes, read_line, title):
    """Write and open a preview image; PREVIEW_GIVE_UP, None without the image, or the path.

    A failure to write it is reported with its cause and lets you try again, go on without
    the image, or give up the visual reading; the configuration goes on.
    """
    while True:
        try:
            path = write_preview(ctx, prefix, image, labelled_boxes)
        except OSError as error:
            warn(
                f"AVISO: não foi possível gravar a imagem de conferência em {ctx.runtime_dir}"
                f" ({describe(error)}). Confira se há espaço em disco e se a pasta pode ser"
                " gravada; as imagens anteriores não foram tocadas."
            )
            answer = ask(
                read_line,
                "Enter tenta gravar de novo; c continua sem a imagem; - desiste da leitura"
                " visual: ",
                CONFIGURATION_MOMENT,
            ).strip().lower()
            if answer == "c":
                say("  Continuando sem a imagem: confira os candidatos e as prévias pelo terminal.")
                return None
            if answer == "-":
                return PREVIEW_GIVE_UP
            continue
        say(f"  {title}: {path}")
        ctx.show_image(path)
        return path


def preview_reading(session, field, selector, ctx):
    """One reading of field with selector, under the same rules as an observation."""
    if session.is_closed():
        raise ObserverStopped(WINDOW_CLOSED_IN_CONFIGURATION)
    return observe_once(session, {field: selector}, ctx).readings[field]


def show_summary(session, selectors, ctx):
    """Read every field once more, together, and show the values beside their sources."""
    if session.is_closed():
        raise ObserverStopped(WINDOW_CLOSED_IN_CONFIGURATION)
    observation = observe_once(session, selectors, ctx)
    say("")
    say(
        f"Resumo, lido agora (relógio local {utc_text(observation.captured_at)}), na página"
        f" {observation.page}:"
    )
    for field in FIELDS:
        source = selectors[field]
        origin = f" ({describe_source(source)})" if source else ""
        say(f"  {LABELS[field]}: {shown_value(field, observation.readings[field])}{origin}")
    say(COMPARE_WITH_SCREEN)


def save_selectors(path, selectors, replace_invalid):
    """Write the sources to path with json, which escapes backslashes and quotes itself.

    The comment keys of a valid file are kept. An invalid file is first renamed to a copy
    next to it, so that nothing written in it is lost. The visual source of the quote goes
    to price_ocr, with price left empty.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"_leia_me": SELECTORS_TEMPLATE["_leia_me"]}
    if replace_invalid and path.exists():
        suffix = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        copy = path.with_name(f"observe-selectors.invalido-{suffix}.json")
        os.replace(path, copy)
        say(f"O arquivo inválido foi guardado como {copy}.")
    elif path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError:  # changed by hand during the configuration
            existing = {}
        if isinstance(existing, dict):
            data.update((key, value) for key, value in existing.items() if key.startswith("_"))
        data["_leia_me"] = SELECTORS_TEMPLATE["_leia_me"]
    for field in FIELDS:
        source = selectors[field]
        data[field] = "" if isinstance(source, QuoteOcrSource) or not source else source
    price = selectors["price"]
    if isinstance(price, QuoteOcrSource):
        data[OCR_KEY] = {"chart": price.chart, "marker": price.marker.to_json()}
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)


def written_summary(progress):
    partial = progress.valid - progress.complete
    return (
        f"{count(progress.observations, 'observação', 'observações')},"
        f" {count(progress.valid, 'válida gravada', 'válidas gravadas')}"
        f" ({count(progress.complete, 'completa', 'completas')} e"
        f" {count(partial, 'parcial', 'parciais')})"
    )


def finish(progress):
    say(f"Concluído: {written_summary(progress)}.")
    if progress.csv is None:
        warn(
            "PARADA: nenhuma observação válida, então nenhum CSV foi criado. Confira os"
            " motivos acima e os seletores."
        )
        return EXIT_FAILED
    say(f"CSV: {progress.csv.path}")
    say(
        "Compare as observações com a tela: o CSV sozinho não comprova que os seletores"
        " leem os campos certos."
    )
    return EXIT_OK


def report_interruption(progress):
    say(f"Interrompido por Ctrl+C: {written_summary(progress)}.")
    if progress.csv is not None:
        say(f"CSV preservado: {progress.csv.path}")


def close_session(session, interrupted):
    if session is None:
        return
    try:
        session.close()
    except Exception as error:
        detail = error if isinstance(error, BrowserError) else describe(error)
        if interrupted:
            # In a terminal, Ctrl+C also reaches the Playwright driver, which then exits.
            say(
                f"O Ctrl+C também encerrou a conexão com o navegador ({detail}). Se a janela"
                " continuar aberta, feche-a."
            )
        else:
            warn(
                f"AVISO: o navegador não fechou normalmente ({detail}); se a janela"
                " continuar aberta, feche-a."
            )
    else:
        say("Navegador fechado.")


def open_playwright_session(profile_dir, *, headless=False):
    """Start Playwright and open Chromium with the persistent profile in profile_dir.

    headless exists only for automated checks: the collector always shows the window,
    because you log in there.
    """
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as error:
        raise ConfigError(install_hint()) from error
    return PlaywrightSession(sync_playwright, Error, Path(profile_dir), headless)


def install_hint():
    version = ".".join(str(part) for part in sys.version_info[:3])
    return (
        f"o Playwright não está instalado neste Python ({version}, {sys.executable})."
        " Com esse mesmo Python, rode:\n"
        f"  python -m pip install {PLAYWRIGHT_REQUIREMENT}\n"
        "  python -m playwright install chromium\n"
        "O Playwright 1.64.0 exige Python 3.10 ou mais novo. Veja o README."
    )


class PlaywrightSession:
    """The only code that touches Playwright: it opens one page and reads text from it.

    Besides starting and stopping the browser, it calls only goto, url, is_closed, locator,
    count, is_visible, inner_text, wait_for_timeout, bounding_box, get_by_text, nth,
    get_attribute and screenshot, which captures only the clip of the chart element and
    returns the image to memory, never to a file. Nothing here clicks, types, presses keys,
    evaluates JavaScript of its own, reads cookies or storage, or listens to the network.
    """

    def __init__(self, sync_playwright, error_type, profile_dir, headless):
        self._error_type = error_type
        self._playwright = None
        self._context = None
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(profile_dir.resolve()), headless=headless, no_viewport=True
            )
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
        except BaseException as error:
            self._close_quietly()
            if isinstance(error, error_type):
                hints = "".join(f" Playwright: {hint}" for hint in playwright_hints(error))
                raise BrowserError(
                    f"o Chromium não abriu ({describe(error)}).{hints} Se ele não estiver"
                    " instalado, rode: python -m playwright install chromium. Se outra execução"
                    f" estiver usando {profile_dir}, feche-a antes."
                ) from error
            raise

    def open(self, url):
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except self._error_type as error:
            raise BrowserError(describe(error)) from error

    def current_url(self):
        return self._page.url

    def wait(self, seconds):
        """Wait with page.wait_for_timeout instead of time.sleep.

        Playwright's sync API processes the browser's events only while one of its calls
        runs, so time.sleep would leave its state outdated (Playwright documentation,
        "time.sleep() leads to outdated state"). Closing the window just ends the wait.
        """
        try:
            self._page.wait_for_timeout(seconds * 1000)
        except self._error_type as error:
            if not self._page.is_closed():
                raise BrowserError(
                    f"a espera entre as leituras falhou ({describe(error)})"
                ) from error

    def is_closed(self):
        return self._page.is_closed()

    def read(self, selector, timeout_ms):
        """ElementText for selector: the text only when exactly one visible element matches."""
        try:
            locator = self._page.locator(selector)
            matches = locator.count()
            if matches != 1:
                return ElementText(matches)
            if not locator.is_visible():
                return ElementText(1)
            return ElementText(1, True, locator.inner_text(timeout=timeout_ms))
        except self._error_type as error:
            raise ReadError(describe(error)) from error

    def capture(self, selector, timeout_ms):
        """Capture of the only visible element of selector: the page clipped to its box.

        page.screenshot with a clip neither scrolls nor touches the page, unlike an element
        screenshot, and caret="initial" leaves the text caret as it is.
        """
        start = time.perf_counter()
        try:
            locator = self._page.locator(selector)
            matches = locator.count()
            if matches != 1:
                return Capture(matches)
            if not locator.is_visible():
                return Capture(1)
            box = locator.bounding_box(timeout=timeout_ms)
            if box is None or box["width"] < 1 or box["height"] < 1:
                return Capture(1)
            clip = {key: box[key] for key in ("x", "y", "width", "height")}
            located = time.perf_counter()
            png = self._page.screenshot(clip=clip, caret="initial", timeout=timeout_ms)
            shot = time.perf_counter()
        except self._error_type as error:
            raise ReadError(describe(error)) from error
        try:
            image = decode_png(png)
        except OcrUnavailable as error:
            raise ConfigError(str(error)) from error
        decoded = time.perf_counter()
        steps = {
            "element_ms": round((located - start) * 1000, 1),
            "screenshot_ms": round((shot - located) * 1000, 1),
            "decode_ms": round((decoded - shot) * 1000, 1),
        }
        # The screenshot keeps only what is inside the window. A cropped capture has
        # different scales across and down, and its right edge is not the element's.
        across, down = image.width / box["width"], image.height / box["height"]
        if abs(across - down) > 0.02 * max(across, down):
            return Capture(1, True, None, box["width"], clipped=True, **steps)
        return Capture(1, True, image, box["width"], **steps)

    def find_quote_texts(self, limit):
        """TextMatch of up to limit visible elements whose whole text looks like a quote."""
        found = []
        try:
            matches = self._page.get_by_text(QUOTE_SEARCH_PATTERN)
            total = matches.count()
        except self._error_type as error:
            raise ReadError(describe(error)) from error
        for index in range(min(total, SEARCH_SCAN)):
            element = matches.nth(index)
            try:  # the page keeps changing: an element that went away is skipped
                if not element.is_visible():
                    continue
                text = " ".join(element.inner_text(timeout=READ_TIMEOUT_MS).split())
                selector = self._unique_selector(element)
            except self._error_type:
                continue
            found.append(TextMatch(text, selector))
            if len(found) >= limit:
                break
        return found

    def _unique_selector(self, element):
        """A selector by id, by classes or by the parent's classes that finds only element."""
        candidates = []
        identifier = element.get_attribute("id", timeout=READ_TIMEOUT_MS)
        if identifier:
            candidates.append("#" + css_identifier(identifier))
        classes = (element.get_attribute("class", timeout=READ_TIMEOUT_MS) or "").split()
        if classes:
            own = "".join("." + css_identifier(name) for name in classes)
            candidates.append(own)
            parent = element.locator("xpath=..").get_attribute("class", timeout=READ_TIMEOUT_MS)
            if parent:
                prefix = "".join("." + css_identifier(name) for name in parent.split())
                candidates.append(prefix + " > " + own)
        for selector in candidates:
            if self._page.locator(selector).count() == 1:
                return selector
        return None

    def close(self):
        """Close the browser and stop Playwright; BrowserError tells what failed, if anything."""
        problems = []
        if self._context is not None:
            context, self._context = self._context, None
            try:
                context.close()
            except Exception as error:
                problems.append(describe(error))
        if self._playwright is not None:
            playwright, self._playwright = self._playwright, None
            try:
                playwright.stop()
            except Exception as error:
                problems.append(describe(error))
        if problems:
            raise BrowserError("; ".join(problems))

    def _close_quietly(self):
        try:
            self.close()
        except BrowserError:
            pass


def css_identifier(name):
    """name escaped as a CSS identifier, as CSS.escape does, for selectors by id or class."""
    out = []
    for index, char in enumerate(name):
        if char == "\0":
            out.append("\ufffd")
        elif char.isdigit() and (index == 0 or (index == 1 and name[0] == "-")):
            out.append(f"\\{ord(char):x} ")
        elif index == 0 and char == "-" and len(name) == 1:
            out.append("\\-")
        elif (char.isascii() and char.isalnum()) or char in "-_" or ord(char) >= 0x80:
            out.append(char)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\{ord(char):x} ")
        else:
            out.append("\\" + char)
    return "".join(out)


def playwright_hints(error):
    """The "Looks like ..." hints that Playwright puts in a box inside its launch errors."""
    hints = []
    for line in str(error).splitlines():
        text = line.strip().strip("║").strip()
        if text.startswith("Looks like"):
            hints.append(redact(text)[:300])
    return hints


def describe(error):
    """The error type and the first line of its message, without URL queries or fragments."""
    lines = str(error).strip().splitlines()
    first = redact(lines[0])[:300] if lines else ""
    return f"{type(error).__name__}: {first}" if first else type(error).__name__


def count(number, singular, plural):
    return f"{number} {singular if number == 1 else plural}"


def say(text):
    print(text, flush=True)


def warn(text):
    print(text, file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
