"""A/B diagnostic of the chart capture: python -m pocketautonomo.capture_check.

READ ONLY, NO ORDERS. The diagnostic opens the trading screen in the same visible Chromium
and browser profile as the collector, waits for your login and then only reads the quote of
the asset already on the screen, from the chart, with the visual source saved in
runtime/observe-selectors.json. It never clicks, types or changes the asset.

Why: in the collector's test on your Windows, the step "tela" of the capture (Playwright's
page.screenshot) took about 2 s, while the page answered the count of the chart element in
milliseconds and the same code, in the scan, took 0.1 to 0.3 s. The two readings run the
same code; what differed was around it. This diagnostic measures, on your computer and on
the Pocket Option, those differences, one at a time, with the others fixed:

1. the method of the capture: page.screenshot, the one the collector and the scan use, or
   the direct capture, Chromium's own Page.captureScreenshot with the same clip, without
   Playwright's preparation of the page (an evaluation in every frame, the wait for
   document.fonts.ready and the restoration). The two alternate, reading by reading;
2. the wait before a capture: 0.5 s, as between the scan's readings, or 5 s, as between the
   collector's observations;
3. the browser window covered: visible, covered by this terminal, and visible again;
4. the browser window minimized, in the last phase. In the Windows job of the CI, on a
   local page, a minimized window made each capture take 1 or 2 s, with both methods,
   while a covered one did not change it.

Before every reading, the browser, not the page, tells the state of its window (normal,
maximized or minimized), so each phase shows what it really measured.

Each reading is a whole visual reading, with the detection of the marker and the OCR, so
the diagnostic also counts how the OCR's two readings agreed. Every reading goes to a new
CSV file in runtime/, diagnostico-captura-<data>-<hora>-<sufixo>.csv, and the terminal ends
with the medians and a conclusion that names only the differences that are clear: three
times slower or more, and 300 ms or more apart. It measures your computer at that moment;
it proves nothing about other moments.

The DevTools session can send only READ_ONLY_CDP_METHODS, checked before every command:
Browser.getWindowForTarget, which the collector also uses to read the window's state, and,
only here, Page.getLayoutMetrics and Page.captureScreenshot for the direct capture. The
collector and the scan keep page.screenshot.

Exit codes: 0 when the four phases were measured, 1 when the diagnostic failed or stopped,
2 for invalid arguments or configuration, and 130 after Ctrl+C.
"""

import argparse
import base64
import csv
import math
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pocketautonomo import observe, quote_timings
from pocketautonomo.observe import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    MINIMIZED,
    BrowserError,
    Capture,
    ConfigError,
    ObserverStopped,
    QuoteOcrSource,
    ReadError,
    ask,
    describe,
    say,
)
from pocketautonomo.quote_ocr import OcrUnavailable

# The collector's command, which reads the window's state, and the two of the direct capture.
READ_ONLY_CDP_METHODS = observe.READ_ONLY_CDP_METHODS + ("Page.getLayoutMetrics", "Page.captureScreenshot")
SCREENSHOT, DIRECT = "page.screenshot", "captura direta"
SHORT_WAIT = 0.5  # seconds before a capture, as between the scan's readings
LONG_WAIT = 5.0  # seconds before a capture, as between the collector's observations
PAIRS = 6  # readings of each method, with the short wait, in each phase
LONG_READINGS = 3  # readings with page.screenshot after the long wait, in phase A
CLEAR_RATIO = 3.0  # a clear difference: three times slower or more...
CLEAR_GAP_MS = 300.0  # ...and at least this far apart
SLOW_MS = 1000.0  # a median of the step "tela" this long or longer is slow
BUSY_COUNT_MS = 200.0  # a median count this long or longer: the page answered late
PREFIX = "diagnostico-captura"
DIAGNOSTIC_COLUMNS = ("phase", "method", "wait_s", "window") + quote_timings.TIMING_COLUMNS
MOMENT = "durante o diagnóstico da captura"

INTRO = (
    "DIAGNÓSTICO DA CAPTURA do PocketAutonomo: somente leitura. Não envia ordens, não clica"
    " e não troca de ativo; só lê a cotação do ativo que está na tela, pelo gráfico."
)
HANDS_OFF = (
    "Não use o mouse nem o teclado na janela do navegador durante o diagnóstico, e não a"
    " minimize: um clique seu muda a tela, e o mouse sobre o gráfico cria a etiqueta do cursor."
)


@dataclass(frozen=True)
class Phase:
    key: str
    title: str
    instruction: str
    long_wait: bool  # whether page.screenshot is also measured after LONG_WAIT


PHASES = (
    Phase(
        "A",
        "navegador visível",
        "Deixe o navegador do tamanho de sempre, com o gráfico à mostra, e diminua este terminal"
        " para um canto da tela, sem cobrir o gráfico; feche o visualizador de imagens, se estiver"
        " aberto. Não mude o tamanho do navegador até o fim. Com dois monitores, deixe o terminal"
        " no mesmo monitor do navegador.",
        True,
    ),
    Phase(
        "B",
        "navegador coberto por este terminal",
        "Maximize este terminal, para ele cobrir o navegador inteiro. Não minimize o navegador.",
        False,
    ),
    Phase(
        "C",
        "navegador visível de novo",
        "Restaure este terminal ao tamanho da fase A, num canto, sem cobrir o gráfico.",
        False,
    ),
    Phase(
        "D",
        "navegador minimizado",
        "Minimize o navegador pelo botão de minimizar, no canto superior direito da janela dele,"
        " sem clicar dentro da página. No fim, restaure o navegador.",
        False,
    ),
)
WINDOW_STATES = {"normal": "normal", "maximized": "maximizada", "minimized": "minimizada", "fullscreen": "tela cheia"}


@dataclass(frozen=True)
class Sample:
    """One reading of the diagnostic: where, how, and what observe.read_field gave."""

    phase: str
    method: str
    wait_s: float
    number: int
    reading: object  # observe.FieldReading of the quote
    window: object = None  # the browser window's state right before, as the browser told it

    @property
    def screenshot_ms(self):
        timings = self.reading.timings
        return None if timings is None else timings.screenshot_ms

    @property
    def count_ms(self):
        timings = self.reading.timings
        return None if timings is None else timings.count_ms


def median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def clearly_slower(slow, fast):
    """Whether the median slow is clearly above fast: CLEAR_RATIO times and CLEAR_GAP_MS apart."""
    if slow is None or fast is None:
        return False
    return slow >= CLEAR_RATIO * max(fast, 1.0) and slow - fast >= CLEAR_GAP_MS


def ms_text(value):
    return "sem leitura" if value is None else f"{value:.0f} ms"


def seconds_text(value):
    return f"{value:g}".replace(".", ",")


class DirectView:
    """The session as observe.read_quote_visually sees it, with the direct capture as capture."""

    def __init__(self, session):
        self._session = session

    def capture(self, selector, timeout_ms, strip=None):
        return self._session.capture_direct(selector, timeout_ms, strip=strip)

    def window_state(self):
        return self._session.window_state()


class DiagnosticLog:
    """A new CSV file with DIAGNOSTIC_COLUMNS, flushed after every line."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "x", newline="", encoding="utf-8")
        try:
            self._writer = csv.writer(self._file)
            self._writer.writerow(DIAGNOSTIC_COLUMNS)
            self._file.flush()
        except BaseException:
            self._file.close()
            raise

    def write(self, sample, asset):
        row = observe.timing_row(sample.reading, moment=sample.phase, attempt=sample.number, asset=asset)
        row.update(phase=sample.phase, method=sample.method, wait_s=sample.wait_s, window=sample.window)
        self._writer.writerow([quote_timings.cell(row.get(name)) for name in DIAGNOSTIC_COLUMNS])
        self._file.flush()

    def close(self):
        self._file.close()


def new_diagnostic_path(runtime_dir):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    while True:
        path = runtime_dir / f"{PREFIX}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csv"
        if not path.exists():
            return path


def main(argv=None, *, open_session=None, read_line=input, now=None, monotonic=None, engine=None):
    """Run the diagnostic and return its exit code; the keyword arguments exist for tests."""
    args = build_parser().parse_args(argv)
    open_session = open_diagnostic_session if open_session is None else open_session
    ctx = observe.Context(now=now, engine=engine)
    if monotonic is not None:
        ctx.monotonic = monotonic
    ctx.slow_capture_warned = True  # this program measures slow captures on purpose
    runtime_dir = ctx.runtime_dir
    selectors_path = runtime_dir / observe.SELECTORS_FILE_NAME
    samples, log, session, interrupted = [], None, None, False
    try:
        say(INTRO)
        selectors = observe.load_selectors(selectors_path)
        source = selectors["price"]
        if not isinstance(source, QuoteOcrSource):
            raise ConfigError(
                f"{selectors_path} não tem a leitura visual da cotação (price_ocr); configure-a"
                " antes com observar_windows.cmd (python -m pocketautonomo.observe --configure)"
            )
        ctx.engine.get()  # a missing OCR stops here, before the browser opens
        observe.announce_ocr(ctx)
        url = args.url if args.url is not None else observe.ask_url(read_line)
        session = open_session(runtime_dir / observe.PROFILE_DIR_NAME)
        observe.open_page(session, url)
        say(
            "Faça o login você mesmo, se a página pedir, e deixe a tela de operações com o"
            " gráfico do ativo que quiser medir. O programa não lê, não grava e não mostra"
            " senha, cookies ou tokens."
        )
        say(HANDS_OFF)
        ask(read_line, "Com o gráfico visível, pressione Enter aqui (Ctrl+C cancela). ", "antes da confirmação do login")
        log = DiagnosticLog(new_diagnostic_path(runtime_dir))
        say(f"CSV do diagnóstico: {log.path}")
        for phase in PHASES:
            check_page(session)
            say("")
            say(f"FASE {phase.key}: {phase.title}. {phase.instruction}")
            ask(read_line, f"Enter começa a fase {phase.key}: ", MOMENT)
            samples += run_phase(session, selectors, source, ctx, phase, log)
            say(phase_summary(phase, samples))
        for line in report_lines(samples):
            say(line)
        say(f"CSV do diagnóstico: {log.path}")
        return EXIT_OK
    except ConfigError as error:
        observe.warn(f"CONFIGURAÇÃO: {error}")
        return EXIT_CONFIG
    except (ObserverStopped, BrowserError, OSError) as error:
        observe.warn(f"PARADA: {error}")
        report_partial(samples, log)
        return EXIT_FAILED
    except KeyboardInterrupt:
        interrupted = True
        say("Interrompido por Ctrl+C.")
        report_partial(samples, log)
        return EXIT_INTERRUPTED
    finally:
        if log is not None:
            log.close()
        observe.close_session(session, interrupted)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.capture_check",
        description=(
            "Diagnóstico A/B da captura do gráfico, somente leitura: mede a etapa tela com o"
            " navegador visível, coberto, visível de novo e minimizado, com espera curta e longa"
            " e com dois métodos de captura. Não envia ordens e não clica."
        ),
    )
    parser.add_argument(
        "--url",
        type=observe.official_url,
        help=f"link oficial da tela de operações, em https://{observe.ALLOWED_DOMAIN}; sem ele, o"
        " programa pede o link no terminal",
    )
    return parser


def check_page(session):
    if session.is_closed():
        raise ObserverStopped("a janela do navegador foi fechada durante o diagnóstico")
    url = session.current_url()
    if observe.url_problem(url) is not None:
        raise ObserverStopped(
            f"a página atual ({observe.display_url(url)}) não está em {observe.ALLOWED_DOMAIN};"
            " o diagnóstico não lê fora dela e parou"
        )


def run_phase(session, selectors, source, ctx, phase, log):
    """The Samples of one phase: PAIRS of each method, alternating, then the long waits."""
    asset = read_asset(session, selectors, ctx)
    plan = []
    for number in range(1, PAIRS + 1):
        methods = (SCREENSHOT, DIRECT) if number % 2 else (DIRECT, SCREENSHOT)
        plan += [(method, SHORT_WAIT, number) for method in methods]
    if phase.long_wait:
        plan += [(SCREENSHOT, LONG_WAIT, number) for number in range(1, LONG_READINGS + 1)]
    samples = []
    direct = DirectView(session)
    for method, wait_s, number in plan:
        session.wait(wait_s)
        check_page(session)
        window = session.window_state()
        view = session if method == SCREENSHOT else direct
        reading = observe.read_field(view, "price", source, ctx)
        sample = Sample(phase.key, method, wait_s, number, reading, window)
        samples.append(sample)
        log.write(sample, asset)
        say(sample_line(sample))
    after = read_asset(session, selectors, ctx)
    if asset is not None and after is not None and after != asset:
        observe.warn(
            f"AVISO: o ativo na tela mudou de {asset} para {after} durante a fase {phase.key}: as"
            " leituras desta fase podem misturar dois gráficos."
        )
    return samples


def read_asset(session, selectors, ctx):
    """The asset on the screen, by its text selector, or None without one or when unavailable."""
    if not selectors.get("asset"):
        return None
    reading = observe.read_field(session, "asset", selectors["asset"], ctx)
    return reading.value if reading.available else None


def sample_line(sample):
    reading, timings = sample.reading, sample.reading.timings
    value = observe.shown_value("price", reading)
    parts = [
        f"  {sample.phase} | {sample.method} | espera {seconds_text(sample.wait_s)} s | leitura {sample.number}",
        f"janela {window_text(sample.window)}",
        f"cotação {value}",
        f"tela {ms_text(sample.screenshot_ms)}",
        f"leitura inteira {ms_text(reading.processing_ms)}",
    ]
    if timings is not None and timings.count_ms is not None:
        parts.append(f"contagem {timings.count_ms:.0f} ms")
    return " | ".join(parts)


def window_text(state):
    return "desconhecida" if state is None else WINDOW_STATES.get(state, state)


def phase_windows(samples, phase):
    """The window states that the browser told during phase, each once, in order."""
    return list(dict.fromkeys(s.window for s in samples if s.phase == phase))


def group(samples, phase, method, wait_s):
    return [s for s in samples if s.phase == phase and s.method == method and s.wait_s == wait_s]


def describe_group(samples):
    times = [s.screenshot_ms for s in samples if s.screenshot_ms is not None]
    read = sum(s.reading.available for s in samples)
    text = f"{observe.count(len(samples), 'leitura', 'leituras')}, {read} com cotação"
    if times:
        text += (
            f"; tela: mediana {median(times):.0f} ms (mínimo {min(times):.0f}, máximo {max(times):.0f});"
            f" contagem: mediana {ms_text(median(s.count_ms for s in samples))}"
        )
    return text


def phase_summary(phase, samples):
    states = ", ".join(window_text(state) for state in phase_windows(samples, phase.key))
    lines = [f"Fase {phase.key} ({phase.title}); janela do navegador, segundo ele: {states}:"]
    for method in (SCREENSHOT, DIRECT):
        lines.append(f"  {method}, espera {seconds_text(SHORT_WAIT)} s: {describe_group(group(samples, phase.key, method, SHORT_WAIT))}")
    if phase.long_wait:
        lines.append(
            f"  {SCREENSHOT}, espera {seconds_text(LONG_WAIT)} s: {describe_group(group(samples, phase.key, SCREENSHOT, LONG_WAIT))}"
        )
    return "\n".join(lines)


def tela(samples, phase, method=SCREENSHOT, wait_s=SHORT_WAIT):
    return median(s.screenshot_ms for s in group(samples, phase, method, wait_s))


def verdict(slow, fast, slow_label, fast_label):
    if clearly_slower(slow, fast):
        return f"diferença clara: {slow_label} mais lenta"
    if clearly_slower(fast, slow):
        return f"diferença clara: {fast_label} mais lenta"
    return "sem diferença clara"


@dataclass(frozen=True)
class Medians:
    """The medians of the step "tela" that the comparisons use, in milliseconds or None."""

    screenshot_a: object
    direct_a: object
    long_a: object
    covered_b: object
    direct_b: object
    visible_c: object
    minimized_d: object
    direct_d: object

    @classmethod
    def of(cls, samples):
        return cls(
            tela(samples, "A"),
            tela(samples, "A", DIRECT),
            tela(samples, "A", SCREENSHOT, LONG_WAIT),
            tela(samples, "B"),
            tela(samples, "B", DIRECT),
            tela(samples, "C"),
            tela(samples, "D"),
            tela(samples, "D", DIRECT),
        )


def report_lines(samples):
    """The comparisons, one variable at a time, the OCR's agreements and a conclusion."""
    m = Medians.of(samples)
    screenshot_a, direct_a, long_a = m.screenshot_a, m.direct_a, m.long_a
    visible_a, covered_b, visible_c, direct_b = m.screenshot_a, m.covered_b, m.visible_c, m.direct_b
    lines = [
        "",
        "RESULTADO DO DIAGNÓSTICO: mediana da etapa tela, uma variável por vez; diferença clara é"
        f" {seconds_text(CLEAR_RATIO)} vezes ou mais e {CLEAR_GAP_MS:.0f} ms ou mais.",
        f"  1. Método, na fase A, com espera de {seconds_text(SHORT_WAIT)} s: {SCREENSHOT} {ms_text(screenshot_a)} ×"
        f" {DIRECT} {ms_text(direct_a)}: {verdict(screenshot_a, direct_a, SCREENSHOT, DIRECT)}.",
        f"  2. Espera antes da captura, na fase A, com {SCREENSHOT}: {seconds_text(SHORT_WAIT)} s {ms_text(screenshot_a)} ×"
        f" {seconds_text(LONG_WAIT)} s {ms_text(long_a)}: {verdict(long_a, screenshot_a, f'a espera de {seconds_text(LONG_WAIT)} s', f'a espera de {seconds_text(SHORT_WAIT)} s')}.",
        f"  3. Janela coberta, com {SCREENSHOT} e espera de {seconds_text(SHORT_WAIT)} s: visível (A) {ms_text(visible_a)} ×"
        f" coberta (B) {ms_text(covered_b)} × visível de novo (C) {ms_text(visible_c)}; com a {DIRECT}, coberta (B)"
        f" {ms_text(direct_b)}.",
        f"  4. Janela minimizada, com espera de {seconds_text(SHORT_WAIT)} s: visível de novo (C) {ms_text(visible_c)} ×"
        f" minimizada (D) {ms_text(m.minimized_d)} com {SCREENSHOT}; {ms_text(m.direct_d)} com a {DIRECT}:"
        f" {verdict(m.minimized_d, visible_c, 'a janela minimizada', 'a janela visível')}.",
    ]
    for phase in PHASES:
        states = phase_windows(samples, phase.key)
        if phase.key == "D" and states and MINIMIZED not in states:
            lines.append(
                "  AVISO: na fase D, o navegador não informou a janela minimizada"
                f" ({', '.join(window_text(state) for state in states)}); a comparação 4 não mediu uma janela minimizada."
            )
        if phase.key != "D" and MINIMIZED in states:
            lines.append(
                f"  AVISO: na fase {phase.key}, o navegador informou a janela minimizada em parte das leituras;"
                " essa fase não mediu uma janela à vista."
            )
    counts = median(s.count_ms for s in samples)
    if counts is not None:
        answered = "atrasada: a página estava ocupada" if counts >= BUSY_COUNT_MS else "rápida"
        lines.append(f"  Resposta da página à contagem do elemento do gráfico: mediana {counts:.0f} ms, {answered}.")
    lines += ocr_lines(samples)
    lines += conclusion_lines(m, counts)
    return lines


def ocr_lines(samples):
    read = [s for s in samples if s.reading.available]
    separator = [s for s in read if s.reading.note]
    missing = {}
    for s in samples:
        if not s.reading.available:
            missing[s.reading.reason] = missing.get(s.reading.reason, 0) + 1
    lines = [
        f"  OCR: {len(read)} de {len(samples)} leituras com cotação; em {len(separator)} delas, as duas"
        " leituras só divergiram no separador decimal e foram aceitas com ponto."
    ]
    for reason, n in sorted(missing.items(), key=lambda item: -item[1]):
        lines.append(f"  Indisponível {observe.count(n, 'vez', 'vezes')}: {reason}")
    return lines


def conclusion_lines(m, counts):
    screenshot_a, direct_a, long_a = m.screenshot_a, m.direct_a, m.long_a
    visible_a, covered_b, visible_c = m.screenshot_a, m.covered_b, m.visible_c
    window = clearly_slower(covered_b, visible_a) and clearly_slower(covered_b, visible_c)
    minimized = clearly_slower(m.minimized_d, visible_c)
    wait = clearly_slower(long_a, screenshot_a)
    method = clearly_slower(screenshot_a, direct_a)
    found = []
    if window:
        both = " também com a captura direta" if clearly_slower(m.direct_b, direct_a) else ""
        found.append(
            "a captura fica lenta quando outra janela cobre o navegador e volta ao normal quando ele"
            f" fica visível{both}. Durante o coletor e a varredura, deixe o navegador visível, sem o"
            " terminal nem o visualizador de imagens por cima"
        )
    if minimized:
        found.append(
            "a captura fica lenta com o navegador minimizado. Durante o coletor e a varredura, não"
            " minimize o navegador"
        )
    if wait:
        found.append(
            f"a captura depois de uma espera de {seconds_text(LONG_WAIT)} s é mais lenta que depois de"
            f" {seconds_text(SHORT_WAIT)} s"
        )
    if method:
        found.append(
            f"o {SCREENSHOT} é mais lento que a {DIRECT}: a demora está na preparação da página"
            " pelo Playwright, e não na produção do quadro pelo navegador"
        )
    lines = ["Conclusão, só sobre este computador neste momento:"]
    if found:
        lines += [f"  - {item}." for item in found]
    else:
        measured = [value for value in (visible_a, covered_b, visible_c, direct_a, long_a, m.minimized_d) if value is not None]
        if not measured:
            lines.append("  - nenhuma captura terminou; veja os motivos acima.")
        elif max(measured) < SLOW_MS:
            lines.append(
                f"  - nenhuma demora apareceu: todas as medianas ficaram abaixo de {SLOW_MS:.0f} ms. A demora"
                " do coletor não se repetiu nestas condições."
            )
        elif min(measured) >= SLOW_MS:
            lines.append(
                "  - a demora apareceu em todas as fases, nos dois métodos e com as duas esperas: não é a"
                " janela coberta nem minimizada, nem a espera, nem a preparação do Playwright. Causa não"
                " identificada por este diagnóstico."
            )
        else:
            lines.append("  - resultado misto, sem uma diferença clara numa só variável.")
    if counts is not None and counts >= BUSY_COUNT_MS:
        lines.append(
            f"  - a página demorou {counts:.0f} ms (mediana) para responder à contagem do elemento: a"
            " linha principal da página estava ocupada, o que atrasa qualquer captura."
        )
    lines.append("  Envie esta saída e o CSV do diagnóstico.")
    return lines


def report_partial(samples, log):
    if samples:
        for line in report_lines(samples):
            say(line)
    if log is not None:
        say(f"CSV do diagnóstico, com as leituras feitas: {log.path}")


def open_diagnostic_session(profile_dir, *, headless=False):
    """Start Playwright and open Chromium with the persistent profile, as the collector does."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as error:
        raise ConfigError(observe.install_hint()) from error
    return DiagnosticSession(sync_playwright, Error, Path(profile_dir), headless)


class DiagnosticSession(observe.PlaywrightSession):
    """PlaywrightSession with the direct capture, to compare it with page.screenshot.

    capture_direct finds the element and its strip as capture does, then sends Chromium's
    own Page.captureScreenshot through the DevTools session, with the clip that Playwright
    1.64.0 computes for page.screenshot(clip=...): the visual viewport's offset added, the
    size divided by its scale and rounded down, and that scale. Nothing runs in the page's
    frames. The session's _cdp, the only way to DevTools, sends only READ_ONLY_CDP_METHODS:
    they read the window's state and the viewport, and capture pixels.
    """

    cdp_methods = READ_ONLY_CDP_METHODS

    def capture_direct(self, selector, timeout_ms, strip=None):
        """Capture of the only visible element of selector, or of its right strip, without Playwright's preparation."""
        start = time.perf_counter()
        try:
            locator = self._page.locator(selector)
            matches = locator.count()
            counted = time.perf_counter()
            if matches != 1:
                return Capture(matches)
            if not locator.is_visible():
                return Capture(1)
            checked = time.perf_counter()
            box = locator.bounding_box(timeout=timeout_ms)
            if box is None or box["width"] < 1 or box["height"] < 1:
                return Capture(1)
            clip = {key: box[key] for key in ("x", "y", "width", "height")}
            strip_css = None
            if strip is not None and strip < box["width"]:
                clip["x"], clip["width"], strip_css = box["x"] + box["width"] - strip, strip, strip
            located = time.perf_counter()
            metrics = self._cdp("Page.getLayoutMetrics", {})
            shown, visual = metrics["cssVisualViewport"], metrics["visualViewport"]
            steps = {
                "element_ms": round((located - start) * 1000, 1),
                "count_ms": round((counted - start) * 1000, 1),
                "visible_ms": round((checked - counted) * 1000, 1),
                "box_ms": round((located - checked) * 1000, 1),
                "strip_css": strip_css,
            }
            outside = (
                clip["x"] < 0
                or clip["y"] < 0
                or clip["x"] + clip["width"] > shown["clientWidth"] + 0.5
                or clip["y"] + clip["height"] > shown["clientHeight"] + 0.5
            )
            if outside:
                return Capture(1, True, None, clip["width"], clipped=True, **steps)
            scale = visual["scale"]
            params = {
                "format": "png",
                "clip": {
                    "x": visual["pageX"] + clip["x"],
                    "y": visual["pageY"] + clip["y"],
                    "width": math.floor(clip["width"] / scale + 1e-3),
                    "height": math.floor(clip["height"] / scale + 1e-3),
                    "scale": scale,
                },
                "captureBeyondViewport": False,
            }
            png = base64.b64decode(self._cdp("Page.captureScreenshot", params)["data"])
            shot = time.perf_counter()
        except self._error_type as error:
            raise ReadError(describe(error)) from error
        except (KeyError, TypeError, ValueError) as error:  # an answer of another shape
            raise ReadError(f"resposta inesperada do DevTools ({describe(error)})") from error
        try:
            image = observe.decode_png(png)
        except OcrUnavailable as error:
            raise ConfigError(str(error)) from error
        decoded = time.perf_counter()
        steps.update(
            screenshot_ms=round((shot - located) * 1000, 1),
            decode_ms=round((decoded - shot) * 1000, 1),
            png_bytes=len(png),
        )
        across, down = image.width / clip["width"], image.height / clip["height"]
        if abs(across - down) > 0.02 * max(across, down):
            return Capture(1, True, None, clip["width"], clipped=True, **steps)
        return Capture(1, True, image, clip["width"], **steps)


if __name__ == "__main__":
    sys.exit(main())
