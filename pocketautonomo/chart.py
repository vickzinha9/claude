"""The chart of the asset on the screen, read live: python -m pocketautonomo.chart.

NO ORDERS AND NO CLICKS. The program opens the same visible Chromium and browser profile as
pocketautonomo.observe, waits for your login and reads, every --interval seconds, the chart of the
asset that is on the screen: its candles (pocketautonomo.candles), the quote of its marker and the
payout. It never clicks, never types and never changes the asset: you choose the asset in the
browser, and every reading checks that the asset is the same before and after the capture.

After each reading it shows, kept apart (pocketautonomo.chart_analysis):

- the EXPLORATORY READING: whether the completed candles favor a rise, a fall or neither, with
  every signal and the limits of the reading. It is never a signal to enter;
- the VALIDATED DECISION: CALL or PUT only with a rule of the candles validated on separate data,
  a current reading in the first seconds of a candle and a payout of 90% or more; otherwise
  AGUARDAR, with what is missing.

The completed candles go to runtime/velas-<data>-<hora>-<sufixo>.csv, where the scan, the simulated
operation and analisar_windows.cmd find them.

--configure calibrates the reading first: it captures the chart, finds the colors of the up and
the down candles, asks the period of each candle, reads the candles with that calibration and
shows them numbered on a new image, runtime/velas-previa-<data>-<hora>-<sufixo>.png, beside the
labels of the axis read by the OCR and the last candles' prices. Nothing is saved before you
accept a reading that worked; the calibration goes to runtime/chart-candles.json. Every reading
of the calibration also shows its diagnosis (candles.geometry_lines): where the marker, each line
of text of the axis, the price line and the close of the forming candle are, in the pixels of the
capture, and how far the labels put the marker's quote from its row in the chart. A reading that
fails saves the capture as it is, with a ruler on its right that marks those rows, to a new
runtime/velas-diagnostico-<data>-<hora>-<sufixo>.png.

Exit codes: 0 with at least one usable reading of the candles, 1 without any or after a failure, 2
for invalid arguments or configuration, and 130 after Ctrl+C.
"""

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import timedelta

from pocketautonomo import analysis, candles, chart_analysis, observe, scan
from pocketautonomo.observe import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    BrowserError,
    ConfigError,
    ObserverStopped,
    QuoteOcrSource,
    ask,
    count,
    say,
    warn,
)

DEFAULT_INTERVAL = 5.0
DEFAULT_PERIOD = 60
PREVIEW_PREFIX = "velas-previa"
PREVIEW_TITLE = "Velas lidas, numeradas da que se forma (0) para trás, com o número logo acima de cada uma"
DIAGNOSTIC_PREFIX = "velas-diagnostico"  # the capture of a reading that failed in the calibration
CONFIGURATION_MOMENT = "durante a calibração das velas; nada foi gravado"
INTRO = (
    "LEITURA DO GRÁFICO do PocketAutonomo: lê as velas do ativo que está na tela, sem clicar, sem digitar e sem enviar"
    " ordens. Escolha o ativo você mesmo, no navegador, e depois não use a janela."
)
CHART_ADVICE = (
    "Deixe o gráfico em velas (candles), sem indicadores desenhados sobre ele, no fim (a vela que se forma à direita),"
    " com um zoom em que cada vela tenha alguns pixels de largura e espaço entre elas, e o mouse fora do gráfico."
)


@dataclass
class Progress:
    readings: int = 0
    usable: int = 0  # readings whose candles were read
    added: int = 0  # new candles recorded
    readings_by_direction: dict = None

    def __post_init__(self):
        self.readings_by_direction = {}


def main(argv=None, *, open_session=None, read_line=input, now=None, monotonic=None, engine=None, show_image=None):
    """Run the live reading of the chart and return its exit code; the keyword arguments exist for tests."""
    args = build_parser().parse_args(argv)
    open_session = observe.open_playwright_session if open_session is None else open_session
    ctx = observe.Context(now=now, engine=engine)
    if monotonic is not None:
        ctx.monotonic = monotonic
    if show_image is not None:
        ctx.show_image = show_image
    runtime_dir = ctx.runtime_dir
    selectors_path = runtime_dir / observe.SELECTORS_FILE_NAME
    calibration_path = runtime_dir / candles.CALIBRATION_FILE_NAME
    progress = Progress()
    lock = session = log = None
    interrupted = False
    try:
        say(INTRO)
        selectors = scan.load_reader_selectors(selectors_path)
        source = selectors["price"]
        if not isinstance(source, QuoteOcrSource):
            raise ConfigError(
                "as velas são lidas no gráfico, pela leitura visual da cotação, e a cotação não usa essa leitura. Configure"
                " a cotação com ocr no observar_windows.cmd (python -m pocketautonomo.observe --configure)."
            )
        try:
            model = candles.load_calibration(calibration_path)
        except candles.CalibrationError as error:
            if not args.configure:
                raise ConfigError(str(error)) from error
            warn(f"AVISO: {error}. A calibração será feita do zero.")
            model = None
        if model is None and not args.configure:
            raise ConfigError(
                f"{calibration_path} não existe: calibre a leitura das velas antes, com --configure (no Windows,"
                " grafico_windows.cmd)."
            )
        ctx.engine.get()  # a missing OCR stops here, before the browser opens
        observe.announce_ocr(ctx)
        lock = scan.ScanLock(runtime_dir / scan.LOCK_FILE_NAME)  # one program at a time drives the browser
        url = args.url if args.url is not None else observe.ask_url(read_line)
        say(f"Página: {observe.display_url(url)} (parâmetros e fragmento da URL não são exibidos)")
        plan = "até Ctrl+C" if args.count is None else count(args.count, "leitura", "leituras")
        say(
            f"Leitura: {plan}, uma a cada {scan.seconds_text(args.interval)} s, com operações hipotéticas de"
            f" {scan.seconds_text(args.horizon)} s. Horários pelo relógio deste computador, em UTC."
        )
        session = open_session(runtime_dir / observe.PROFILE_DIR_NAME)
        observe.open_page(session, url)
        say("Faça o login você mesmo, na janela do navegador. O programa não lê, não grava e não mostra senha, cookies ou tokens.")
        ask(
            read_line,
            "Com a tela de operações e o ativo escolhido visíveis, pressione Enter aqui para começar (Ctrl+C cancela). ",
            "antes da confirmação do login",
        )
        if session.is_closed():
            raise ObserverStopped("a janela do navegador foi fechada antes da confirmação do login")
        if args.configure:
            model = configure(session, selectors, model, read_line, ctx)
        history = chart_analysis.load_candles(runtime_dir)
        log = chart_analysis.CandleLog(runtime_dir, warn)
        setup = chart_analysis.ChartSetup(model, source, selectors["asset"], args.horizon, history, log)
        for line in chart_analysis.report_lines(runtime_dir, args.horizon):
            say(line)
        say(CHART_ADVICE)
        watch(session, setup, selectors, args, ctx, progress)
        return finish(progress, log)
    except ConfigError as error:
        warn(f"CONFIGURAÇÃO: {error}")
        return EXIT_CONFIG
    except (ObserverStopped, BrowserError, scan.ScanBusy, OSError) as error:
        warn(f"PARADA: {error}")
        summary(progress, log)
        return EXIT_FAILED
    except KeyboardInterrupt:
        interrupted = True
        say("Interrompido por Ctrl+C.")
        summary(progress, log)
        return EXIT_INTERRUPTED
    finally:
        if log is not None:
            log.close()
        observe.close_session(session, interrupted)
        if lock is not None:
            lock.release()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m pocketautonomo.chart",
        description=(
            "Lê as velas do gráfico do ativo que está na tela, sem clicar e sem enviar ordens, e mostra a leitura"
            " exploratória e a decisão validada (CALL, PUT ou AGUARDAR)."
        ),
    )
    parser.add_argument("--url", type=observe.official_url, help="link oficial da tela de operações; sem ele, o programa pede")
    parser.add_argument(
        "--configure", action="store_true",
        help="calibra antes a leitura das velas (cores e período), com uma prévia numerada, e grava runtime/chart-candles.json",
    )
    parser.add_argument(
        "--interval", type=scan.seconds_from(observe.MIN_INTERVAL, math.inf), default=DEFAULT_INTERVAL,
        help=f"segundos entre duas leituras (padrão {DEFAULT_INTERVAL:g})",
    )
    parser.add_argument("--count", type=observe.positive_int, help="quantas leituras fazer; sem esta opção, até Ctrl+C")
    parser.add_argument(
        "--horizon", type=analysis.horizon_seconds, default=analysis.DEFAULT_HORIZON,
        help=f"segundos das operações hipotéticas da validação (padrão {analysis.DEFAULT_HORIZON:g})",
    )
    return parser


def period_of(text, current):
    text = text.strip()
    if not text:
        return current, None
    try:
        value = int(text)
    except ValueError:
        return None, f"{text!r} não é um número inteiro de segundos"
    if not candles.MIN_PERIOD <= value <= candles.MAX_PERIOD:
        return None, f"o período precisa ser de {candles.MIN_PERIOD} a {candles.MAX_PERIOD} s"
    return value, None


def configure(session, selectors, current, read_line, ctx):
    """Calibrate the reading of the candles with a preview, and save it once you accept a reading that worked."""
    say("")
    say("CALIBRAÇÃO DAS VELAS")
    say(CHART_ADVICE)
    period = current.period if current is not None else DEFAULT_PERIOD
    while True:
        answer = ask(
            read_line,
            f"Período de cada vela no gráfico, em segundos, como a plataforma mostra (Enter mantém {period}; 60 é M1): ",
            CONFIGURATION_MOMENT,
        )
        value, problem = period_of(answer, period)
        if value is not None:
            period = value
            break
        say(f"  {problem}.")
    source = selectors["price"]
    while True:
        if session.is_closed():
            raise ObserverStopped(observe.WINDOW_CLOSED_IN_CONFIGURATION)
        captured_at = ctx.now()
        capture, problem = observe.capture_chart(session, source.chart)
        captured_by = ctx.now()  # the screen was taken between captured_at and captured_by
        reading = None
        if capture is None:
            say(f"  O gráfico não foi capturado: {problem}.")
        else:
            image, scale = capture.image, capture.scale
            box, reason = candles.locate_marker(image, scale, source.marker)
            colors, reason = (None, f"o marcador da cotação não foi achado: {reason}") if box is None else candles.detect_colors(image, box.x0)
            if colors is None:
                say(f"  As cores das velas não foram achadas: {reason}.")
                save_diagnostic(ctx, image, None, read_line)
            else:
                model = candles.CandleModel(colors[0], colors[1], period)
                reading = candles.read_candles(image, scale, model, source.marker, ctx.engine.get())
                for line in preview_lines(model, reading, captured_at, captured_by) + candles.geometry_lines(reading):
                    say(line)
                if reading.available:
                    path = observe.save_preview(
                        ctx, PREVIEW_PREFIX, image, [], read_line, PREVIEW_TITLE, data=candles.preview_png(image, reading),
                    )
                    if path is not None and path != observe.PREVIEW_GIVE_UP:
                        say(f"  Na imagem, {candles.preview_legend(reading)}.")
                else:
                    save_diagnostic(ctx, image, reading, read_line)
        if reading is not None and reading.available:
            answer = ask(
                read_line,
                "Compare as velas numeradas e os preços acima com o gráfico. Enter aceita e grava; r lê de novo; - desiste: ",
                CONFIGURATION_MOMENT,
            ).strip().lower()
            if not answer:
                break
        else:
            answer = ask(read_line, "Enter lê de novo; - desiste: ", CONFIGURATION_MOMENT).strip().lower()
        if answer == "-":
            raise ConfigError("a calibração das velas foi cancelada, e nada foi gravado")
    path = ctx.runtime_dir / candles.CALIBRATION_FILE_NAME
    candles.save_calibration(path, model)
    say(f"Calibração das velas gravada em {path}.")
    return model


def save_diagnostic(ctx, image, reading, read_line):
    """Save the capture of a reading that failed, with the ruler of the rows found, and say how to read it.

    reading is None when the colors of the candles were not found: the ruler then marks nothing.
    """
    title = (
        "Imagem de diagnóstico, a captura sem marcas e uma régua à direita" if reading is not None
        else "Imagem de diagnóstico, a captura sem marcas (nada foi achado para marcar)"
    )
    path = observe.save_preview(ctx, DIAGNOSTIC_PREFIX, image, [], read_line, title, data=candles.diagnostic_png(image, reading))
    if path is None or path == observe.PREVIEW_GIVE_UP:
        return
    if reading is not None:
        say(f"  Na imagem, a {candles.RULER_LEGEND}.")
        say("  Para investigar a falha, envie essa imagem e as linhas do diagnóstico acima; não mostre dados seus nela.")
    else:
        say("  Para investigar a falha, envie essa imagem e o motivo acima; não mostre dados seus nela.")


def preview_lines(model, reading, captured_at=None, captured_by=None):
    """What the calibration found, for you to compare with the chart.

    captured_at and captured_by are this computer's clock just before and just after the capture.
    """
    lines = [
        f"  Cores achadas: velas de alta {candles.color_hex(model.up)}, de baixa {candles.color_hex(model.down)};"
        f" período de {model.period} s."
    ]
    if reading.scale is not None:
        labels = ", ".join(text for _, text in reading.scale.labels)
        lines.append(f"  Eixo de preços lido pelo OCR: {labels}; marcador da cotação {reading.marker_text}.")
        if reading.scale.dropped:
            lines.append(f"  Rótulo deixado de fora, fora da reta dos outros: {', '.join(text for _, text in reading.scale.dropped)}.")
    if not reading.available:
        lines.append(f"  A leitura das velas falhou: {reading.reason}.")
        return lines
    lines.append(
        f"  {len(reading.completed)} velas completas e a que se forma; 1 pixel = {reading.text(reading.resolution)}"
        + (f"; {len(reading.missing)} posição(ões) sem vela lida" if reading.missing else "")
        + "."
    )
    # The same candles as the numbers of the preview: 0, the forming one, to PREVIEW_NUMBERED - 1, oldest first.
    present = dict(zip(reading.indices, reading.candles))
    for index in range(min(candles.PREVIEW_NUMBERED, max(reading.indices) + 1) - 1, -1, -1):
        name = "a que se forma" if index == 0 else f"{index}"
        candle = present.get(index)
        if candle is None:
            lines.append(f"    vela {name}: nenhuma vela lida nesta posição")
            continue
        lines.append(
            f"    vela {name}: {candle.kind}, abertura {reading.text(candle.open)}, máxima {reading.text(candle.high)},"
            f" mínima {reading.text(candle.low)}, fechamento {reading.text(candle.close)}"
        )
    lines += timing_lines(model, reading, captured_at, captured_by)
    return lines


def timing_lines(model, reading, captured_at, captured_by=None):
    """When each candle started by this computer's clock, and the countdown of the marker against that clock.

    The starts come from captured_at, as in every reading (candles.stamp). The countdown is compared at the
    middle of the capture, between captured_at and captured_by, which it may have taken a moment.
    """
    if captured_at is None:
        return []
    starts, near = candles.stamp(reading, captured_at, model.period)
    forming = starts[0]
    previous = forming - timedelta(seconds=model.period)
    lines = [
        f"  Pelo relógio deste computador, a vela que se forma (0) começou às {forming:%H:%M:%S} UTC"
        f" ({forming.astimezone():%H:%M:%S} no fuso deste computador), e a última fechada (1), às {previous:%H:%M:%S}"
        f" UTC ({previous.astimezone():%H:%M:%S}); cada vela, um período antes da seguinte."
        + (f" Atenção: {near}." if near else "")
    ]
    if reading.countdown is not None:
        middle = captured_at if captured_by is None else captured_at + (captured_by - captured_at) / 2
        remaining, problem = candles.countdown_check(reading, middle, model.period)
        verdict = f"não confere: {problem}" if problem else "confere"
        lines.append(
            f"  Contador no marcador: {reading.countdown_text}. Pelo relógio deste computador, com velas de {model.period} s,"
            f" a que se forma fecha em {chart_analysis.number(remaining, 1)} s: {verdict}."
        )
    elif reading.marker_others:
        shown = ", ".join(f"{a!r} e {b!r}" for a, b in reading.marker_others)
        lines.append(f"  Contador no marcador: não lido (o OCR leu {shown}, sem duas leituras iguais de um contador).")
    return lines


def watch(session, setup, selectors, args, ctx, progress):
    """Read the chart of the asset on the screen until args.count readings, the window closes or Ctrl+C."""
    while args.count is None or progress.readings < args.count:
        if progress.readings:
            session.wait(args.interval)
        if session.is_closed():
            say("A janela do navegador foi fechada; a leitura terminou.")
            return
        progress.readings += 1
        number = progress.readings
        outside = scan.platform_problem(session)
        if outside is not None:
            say(f"LEITURA {number}: {outside}; nada foi lido. Volte à tela de operações na janela do navegador.")
            continue
        asset = observe.read_field(session, "asset", selectors["asset"], ctx)
        if not asset.available:
            say(f"LEITURA {number}: o ativo não pôde ser lido ({asset.reason}); a sessão pode ter caído. Nada foi lido.")
            continue
        payout_before = observe.read_field(session, "payout", selectors["payout"], ctx)
        observation = chart_analysis.absorb(setup, chart_analysis.observe_chart(session, setup, asset.value, ctx))
        payout_after = observe.read_field(session, "payout", selectors["payout"], ctx)
        payout = None
        if payout_before.available and payout_after.available and payout_before.value == payout_after.value:
            payout = scan.payout_number(payout_after.value)
        assessment = chart_analysis.assess(setup, observation, payout, ctx.now())
        if observation.usable:
            progress.usable += 1
            progress.added += observation.added
        direction = assessment.market.direction
        progress.readings_by_direction[direction] = progress.readings_by_direction.get(direction, 0) + 1
        for line in reading_lines(number, asset.value, assessment, payout):
            say(line)


def reading_lines(number, asset, assessment, payout):
    observation = assessment.observation
    reading = observation.reading
    head = f"LEITURA {number} | {asset}"
    if observation.usable:
        head += f" | cotação {reading.marker_text}"
    head += f" | payout {'indisponível' if payout is None else f'{scan.seconds_text(payout)}%'}"
    if observation.captured_at is not None:
        head += f" | relógio local {observe.utc_text(observation.captured_at)}"
    lines = [head, f"  gráfico: {observation.status(assessment.period)}", f"  {assessment.market.describe()}"]
    if assessment.market.signals:
        lines.append(f"    sinais: {assessment.market.signals_text()}")
    if assessment.market.limitations:
        lines.append(f"    limitações: {'; '.join(assessment.market.limitations)}")
    lines.append(f"  DECISÃO VALIDADA: {assessment.decision.describe()}")
    return lines


def summary(progress, log):
    directions = ", ".join(f"{name} {total}" for name, total in sorted(progress.readings_by_direction.items()))
    say(
        f"Leituras: {progress.readings}, {progress.usable} com as velas lidas, {progress.added} velas novas gravadas"
        + (f"; leituras exploratórias: {directions}" if directions else "")
        + "."
    )
    if log is not None and log.paths:
        say(f"Velas gravadas em {', '.join(str(path) for path in log.paths)}")


def finish(progress, log):
    summary(progress, log)
    if not progress.usable:
        warn(
            "PARADA: nenhuma leitura das velas funcionou. Confira os motivos acima, a calibração (--configure) e o gráfico"
            " na tela."
        )
        return EXIT_FAILED
    say("Compare as leituras com o gráfico: a leitura exploratória descreve o gráfico e não é sinal de entrada.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
