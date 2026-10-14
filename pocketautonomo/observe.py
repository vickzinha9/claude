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

Each observation carries its capture time, taken from this computer's clock in UTC right
before the reading; no time is read from the platform. Observations with at least one
available field are written, line by line, to a new CSV file in runtime/; the others are
only shown in the terminal. The query and the fragment of a URL are never shown or written.
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_DOMAIN = "pocketoption.com"
RUNTIME_DIR = Path("runtime")  # relative to the current directory, like the simulation
PROFILE_DIR_NAME = "browser-profile"
SELECTORS_FILE_NAME = "observe-selectors.json"
PLAYWRIGHT_REQUIREMENT = "playwright==1.64.0"

FIELDS = ("asset", "price", "payout")
LABELS = {"asset": "ativo", "price": "preço", "payout": "payout"}
CSV_COLUMNS = (
    "captured_at_utc",
    "page",
    "asset",
    "asset_status",
    "price",
    "price_status",
    "payout_percent",
    "payout_status",
)
OK = "ok"
UNAVAILABLE = "indisponível"

DEFAULT_INTERVAL = 5.0
MIN_INTERVAL = 1.0
NAVIGATION_TIMEOUT_MS = 60_000
READ_TIMEOUT_MS = 2_000
MAX_ASSET_LENGTH = 60

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

HOST_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*" + re.escape(ALLOWED_DOMAIN))
PRICE_PATTERN = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
PAYOUT_PATTERN = re.compile(r"\+?([0-9]{1,3}(?:[.,][0-9]+)?)\s*%")
URL_TAIL_PATTERN = re.compile(r"""([a-z][a-z0-9+.-]*:[^\s?#"'<>]*)[?#][^\s"'<>]*""", re.IGNORECASE)
FORMULA_PREFIXES = ("=", "+", "-", "@")  # a spreadsheet could run such a cell as a formula

SELECTORS_TEMPLATE = {
    "_leia_me": (
        "Gravado pelo modo --configure (no Windows, observar_windows.cmd), que pede os"
        " seletores no terminal e mostra a prévia de cada um. Se editar à mão, use em asset"
        " (ativo), price (preço) e payout seletores conferidos na tela de operações, como"
        " explica o INTEGRATION.md, e deixe \"\" no campo sem seletor: ele fica"
        " indisponível. Chaves que começam com _ são ignoradas."
    ),
    "asset": "",
    "price": "",
    "payout": "",
}
# Answers that pick a field to correct at the end of --configure.
FIELD_ANSWERS = {
    "ativo": "asset",
    "asset": "asset",
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
    """One field of an observation: the value, or None, and "ok: <selector>" or the reason."""

    value: object
    status: str

    @property
    def available(self):
        return self.value is not None

    @property
    def reason(self):
        """Why the field is unavailable, or None when it is available."""
        return None if self.available else self.status.split(": ", 1)[1]


@dataclass(frozen=True)
class Observation:
    captured_at: datetime
    page: str  # https://host/path, without user, port, query or fragment
    readings: dict  # field -> FieldReading, in the order of FIELDS

    @property
    def valid(self):
        return any(reading.available for reading in self.readings.values())


@dataclass
class Progress:
    """What this run did so far."""

    observations: int = 0
    valid: int = 0
    csv: object = None  # the CsvLog, created with the first valid observation


def main(argv=None, *, open_session=None, read_line=input, now=None):
    """Run the collector and return its exit code.

    open_session, read_line and now exist for tests. They replace, in this order,
    open_playwright_session, input and the UTC clock. The wait between two observations is
    the session's own wait, page.wait_for_timeout in PlaywrightSession, so a fake session
    brings a fake wait.
    """
    args = parse_arguments(argv)
    open_session = open_playwright_session if open_session is None else open_session
    now = utc_now if now is None else now
    runtime_dir = RUNTIME_DIR
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
            selectors = configure_selectors(session, selectors_path, read_line, now)
        else:
            selectors = load_selectors(selectors_path)
        announce_selectors(selectors, selectors_path)
        run_observations(session, selectors, args, now, runtime_dir, progress)
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
            " para você fazer o login e lê ativo, preço e payout pelos seletores que você"
            " conferiu. Não envia ordens, não clica em CALL/PUT e não faz operações."
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
    """The selectors in path, as {field: selector or None}; a missing file has none.

    Keys that start with _ are comments. Any other key besides asset, price and payout, a
    value that is not text or null, or invalid JSON raises ConfigError.
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
    unknown = sorted(key for key in data if key not in FIELDS and not key.startswith("_"))
    if unknown:
        raise ConfigError(
            f"{path} tem chave desconhecida ({', '.join(unknown)}); use só asset, price e payout"
        )
    selectors = {}
    for field in FIELDS:
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"{path}: o valor de {field} precisa ser texto")
        selectors[field] = (value or "").strip() or None
    return selectors


def announce_selectors(selectors, selectors_path):
    configured = [LABELS[field] for field in FIELDS if selectors[field]]
    missing = [LABELS[field] for field in FIELDS if not selectors[field]]
    if not configured:
        raise ConfigError(
            f"nenhum seletor preenchido em {selectors_path}, então nenhum campo pode ser lido."
            " Rode com --configure (no Windows, observar_windows.cmd) para escolher os"
            " seletores no terminal, ou preencha o arquivo como explica o INTEGRATION.md."
        )
    line = f"Campos com seletor: {', '.join(configured)}"
    if missing:
        line += f" | sem seletor, ficam indisponíveis: {', '.join(missing)}"
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
    """(price, None) when the text is only digits with . or , between them, or (None, reason).

    The price is kept exactly as shown, because the platform's number format was not
    verified.
    """
    value = text.strip()
    if PRICE_PATTERN.fullmatch(value):
        return value, None
    return None, f"texto não reconhecido como preço: {preview(text)}"


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


def observe_once(session, selectors, now):
    """Read every field once, but only from a page on pocketoption.com."""
    captured_at = now()
    url = session.current_url()
    page = display_url(url)
    if url_problem(url) is not None:
        reading = unavailable(
            f"a página atual ({page}) não está em {ALLOWED_DOMAIN}; nada foi lido"
        )
        return Observation(captured_at, page, {field: reading for field in FIELDS})
    readings = {field: read_field(session, field, selectors.get(field)) for field in FIELDS}
    return Observation(captured_at, page, readings)


def read_field(session, field, selector):
    if not selector:
        return unavailable(f"sem seletor em {SELECTORS_FILE_NAME}")
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


def run_observations(session, selectors, args, now, runtime_dir, progress):
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
        observation = observe_once(session, selectors, now)
        progress.observations += 1
        if observation.valid:
            if progress.csv is None:
                progress.csv = CsvLog(new_csv_path(runtime_dir))
                say(f"CSV: {progress.csv.path}")
            progress.csv.write(observation)
            progress.valid += 1
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
    row = [observation.captured_at.isoformat(timespec="milliseconds"), observation.page]
    for field in FIELDS:
        reading = observation.readings[field]
        row += [reading.value if reading.available else "", reading.status]
    return row


def new_csv_path(runtime_dir):
    """A new CSV path with a unique name in runtime_dir, which is created if needed."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        name = f"observations-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csv"
        path = runtime_dir / name
        if not path.exists():
            return path


def observation_line(number, observation):
    parts = [
        f"OBSERVAÇÃO {number}",
        observation.captured_at.isoformat(timespec="milliseconds"),
        observation.page,
    ]
    for field in FIELDS:
        parts.append(f"{LABELS[field]} {shown_value(field, observation.readings[field])}")
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
        " relógio deste computador. Riscos e limitações: INTEGRATION.md."
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


def configure_selectors(session, path, read_line, now):
    """Ask for each field's selector, preview it, and write the file once you confirm.

    Enter keeps the current selector, so the ones already saved can be reused. Nothing is
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
        selectors[field] = ask_selector(session, field, selectors[field], read_line, now)
    while True:
        show_summary(session, selectors, now)
        answer = ask(
            read_line,
            "Enter grava os seletores e começa as observações; para corrigir um campo, digite"
            " ativo, preço ou payout: ",
            CONFIGURATION_MOMENT,
        )
        if not answer.strip():
            break
        field = FIELD_ANSWERS.get(answer.strip().lower())
        if field is None:
            say(f"Resposta não reconhecida: {preview(answer.strip())}. Use ativo, preço ou payout.")
            continue
        selectors[field] = ask_selector(session, field, selectors[field], read_line, now)
    if not any(selectors.values()):
        raise ConfigError(f"nenhum seletor foi escolhido, e {path} não foi alterado")
    save_selectors(path, selectors, invalid)
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


def ask_selector(session, field, current, read_line, now):
    """The selector you accept for field after seeing its preview, or None for none."""
    label = LABELS[field]
    if current:
        say(f"{label.capitalize()}: seletor atual {current}")
        prompt = f"Seletor de {label} (Enter mantém o atual; - deixa sem seletor): "
    else:
        say(f"{label.capitalize()}: ainda sem seletor.")
        prompt = f"Seletor de {label} (Enter deixa sem seletor): "
    selector = ask(read_line, prompt, CONFIGURATION_MOMENT).strip() or current
    while selector and selector != "-":
        reading = preview_reading(session, field, selector, now)
        line = f"  Prévia de {label}, lida agora: {shown_value(field, reading)}"
        if reading.available:
            line += ". Confira se é o que a tela mostra."
        say(line)
        answer = ask(
            read_line,
            f"Enter aceita este seletor de {label}; cole outro para trocar; - deixa sem"
            " seletor: ",
            CONFIGURATION_MOMENT,
        ).strip()
        if not answer:
            return selector
        selector = answer
    say(f"{label.capitalize()}: sem seletor, o campo fica INDISPONÍVEL.")
    return None


def preview_reading(session, field, selector, now):
    """One reading of field with selector, under the same rules as an observation."""
    if session.is_closed():
        raise ObserverStopped(WINDOW_CLOSED_IN_CONFIGURATION)
    return observe_once(session, {field: selector}, now).readings[field]


def show_summary(session, selectors, now):
    """Read every field once more, together, and show the values beside their selectors."""
    if session.is_closed():
        raise ObserverStopped(WINDOW_CLOSED_IN_CONFIGURATION)
    observation = observe_once(session, selectors, now)
    say("")
    say(
        f"Resumo, lido agora ({observation.captured_at.isoformat(timespec='milliseconds')}),"
        f" na página {observation.page}:"
    )
    for field in FIELDS:
        origin = f" (seletor {selectors[field]})" if selectors[field] else ""
        say(f"  {LABELS[field]}: {shown_value(field, observation.readings[field])}{origin}")
    say(COMPARE_WITH_SCREEN)


def save_selectors(path, selectors, replace_invalid):
    """Write the selectors to path with json, which escapes backslashes and quotes itself.

    The comment keys of a valid file are kept. An invalid file is first renamed to a copy
    next to it, so that nothing written in it is lost.
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
    data.update((field, selectors[field] or "") for field in FIELDS)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)


def finish(progress):
    say(
        f"Concluído: {count(progress.observations, 'observação', 'observações')},"
        f" {count(progress.valid, 'válida gravada', 'válidas gravadas')}."
    )
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
    say(
        f"Interrompido por Ctrl+C: {count(progress.observations, 'observação', 'observações')},"
        f" {count(progress.valid, 'válida gravada', 'válidas gravadas')}."
    )
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
    count, is_visible, inner_text and wait_for_timeout. Nothing here clicks, types, presses
    keys, evaluates JavaScript of its own, reads cookies or storage, or listens to the
    network.
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
