"""Tests of the live reading of the chart (pocketautonomo.chart) and of the candles in the scan and the operation.

ARTIFICIAL DATA: the charts are drawn by chart_images.CandleChart from candles made by the tests,
and the OCR is a fake that "reads" what each test says. No test here opens a browser, reaches the
network or needs Playwright, and passing here says nothing about the real platform.
"""

import ast
import contextlib
import csv
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart_images as ci  # noqa: E402
import test_operate as to  # noqa: E402
import test_scan as ts  # noqa: E402
from test_observe import FORBIDDEN_ATTRIBUTES, shown  # noqa: E402

from pocketautonomo import analysis, candles, chart, chart_analysis, observe, operate, operation, scan  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CLOCK = datetime(2026, 10, 18, 12, 0, 5, tzinfo=timezone.utc)
MARKER_JSON = {"colors": ["#2962ff"], "width": 68.0, "height": 18.0, "right_gap": 3.0, "tolerance": 20}
CALIBRATION = candles.CandleModel(ci.CANDLE_GREEN, ci.CANDLE_RED, 60)


def rising_bars(count=40, start=1.08500, last=None):
    """ARTIFICIAL candles that rise steadily, ending at last when given."""
    bars, price = [], start
    for index in range(count):
        move = -0.00003 if index % 4 == 3 else 0.0001
        open_, close = round(price, 5), round(price + move, 5)
        bars.append((open_, round(max(open_, close) + 0.00001, 5), round(min(open_, close) - 0.00001, 5), close))
        price = close
    if last is not None:
        shift = round(last - bars[-1][3], 5)
        bars = [tuple(round(value + shift, 5) for value in bar) for bar in bars]
    return bars


def chart_for(bars):
    low = min(bar[2] for bar in bars) - 0.0002
    high = max(bar[1] for bar in bars) + 0.0002
    return ci.CandleChart(bars, low, high)


class ScriptedEngine:
    """The OCR: the answers queued by the last capture of a whole chart, then fallback's, if any."""

    def __init__(self, fallback=None):
        self.queue = []
        self.fallback = fallback

    def recognize(self, gray):
        if self.queue:
            return self.queue.pop(0)
        if self.fallback is not None:
            return self.fallback.recognize(gray)
        return "", 0.0


class ChartSession:
    """Stands in for PlaywrightSession on a screen whose chart is drawn by CandleChart; it never clicks."""

    def __init__(self, charts, asset="EUR/USD OTC", payout="+92%", on_wait=None):
        self.charts = list(charts)
        self.engine = ScriptedEngine()
        self.asset, self.payout = asset, payout
        self.url = ts.URL
        self.t = 0.0
        self.closed = False
        self.on_wait = on_wait
        self.captures = 0
        self.calls = []

    def now(self):
        return CLOCK + timedelta(seconds=self.t)

    def monotonic(self):
        return 100.0 + self.t

    def open(self, url):
        self.calls.append(("open", url))

    def current_url(self):
        return self.url

    def is_closed(self):
        return self.closed

    def read(self, selector, timeout_ms):
        self.t += 0.01
        self.calls.append(("read", selector))
        if selector == ts.ASSET:
            return observe.ElementText(0) if self.asset is None else shown(self.asset)
        if selector == ts.PAYOUT:
            return observe.ElementText(0) if self.payout is None else shown(self.payout)
        return observe.ElementText(0)

    def capture(self, selector, timeout_ms, strip=None):
        self.t += 0.05
        self.calls.append(("capture", selector, strip))
        drawn = self.charts[min(self.captures, len(self.charts) - 1)]
        self.captures += 1
        self.engine.queue = list(drawn.answers)
        return observe.Capture(1, True, drawn.image, drawn.image.width / drawn.scale)

    def wait(self, seconds):
        self.t += seconds
        self.calls.append(("wait", seconds))
        if self.on_wait is not None:
            self.on_wait(self, seconds)

    def close(self):
        self.calls.append(("close",))


class ChartTestCase(ts.ScanTestCase):
    def setUp(self):
        super().setUp()
        self.write_sources(price="", price_ocr={"chart": ts.CHART, "marker": MARKER_JSON})

    def calibrate(self, model=CALIBRATION):
        candles.save_calibration(self.runtime / candles.CALIBRATION_FILE_NAME, model)

    def run_chart(self, *args, session, answers=("",)):
        self.session = session
        self.answers = ts.Answers(answers)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chart.main(
                ["--url", ts.URL, *args],
                open_session=lambda profile: session,
                read_line=self.answers,
                now=session.now,
                monotonic=session.monotonic,
                engine=ci.FakeEngineHolder(session.engine),
                show_image=lambda path: None,
            )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def candle_rows(self):
        rows = []
        for path in sorted(self.runtime.glob("velas-2*.csv")):
            with open(path, newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                self.assertEqual(tuple(reader.fieldnames), chart_analysis.CANDLE_COLUMNS)
                rows += list(reader)
        return rows


class LiveChartTest(ChartTestCase):
    def test_the_calibration_finds_the_colors_shows_the_candles_and_saves_only_when_accepted(self):
        session = ChartSession([chart_for(rising_bars())])
        code = self.run_chart("--configure", "--count", "2", session=session, answers=("", "60", ""))
        self.assertEqual(code, 0, self.err + self.out)
        self.assertEqual(candles.load_calibration(self.runtime / candles.CALIBRATION_FILE_NAME), CALIBRATION)
        for text in (
            "Cores achadas: velas de alta #45b734, de baixa #ff3e1f; período de 60 s.",
            "Eixo de preços lido pelo OCR:",
            "39 velas completas e a que se forma",
            "    vela a que se forma: baixa, abertura",  # crossed by the price line: a pixel off, and never analyzed
            "    vela 1: alta, abertura 1.0876",  # 1.08763 drawn: read within the resolution, one pixel of 0.00001
            "Velas lidas, numeradas da que se forma (0) para trás:",
        ):
            self.assertIn(text, self.out)
        self.assertEqual(len(list(self.runtime.glob("velas-previa-*.png"))), 1)
        self.assertNotIn("click", {call[0] for call in session.calls})

    def test_each_reading_shows_the_exploratory_reading_apart_from_the_validated_decision(self):
        self.calibrate()
        session = ChartSession([chart_for(rising_bars())])
        code = self.run_chart("--count", "2", "--interval", "10", session=session)
        self.assertEqual(code, 0, self.err + self.out)
        self.assertIn("LEITURA 1 | EUR/USD OTC | cotação", self.out)
        self.assertIn("LEITURA EXPLORATÓRIA, não é sinal de entrada: favorece ALTA", self.out)
        self.assertIn("    sinais: tendência curta: nas últimas 5 velas, o preço subiu", self.out)
        self.assertIn("    limitações: a forma de juntar os sinais foi escrita antes de qualquer dado e não foi validada", self.out)
        self.assertIn("  DECISÃO VALIDADA: AGUARDAR (dados insuficientes: 39 velas de 60 s gravadas", self.out)
        rows = self.candle_rows()
        self.assertEqual(len(rows), 39)  # the second reading, in the same minute, adds nothing
        self.assertEqual({row["origin"] for row in rows}, {chart_analysis.ORIGIN_CHART})
        self.assertEqual(rows[-1]["start_utc"], "2026-10-18T11:59:00.000+00:00")
        self.assertIn("Leituras: 2, 2 com as velas lidas, 39 velas novas gravadas; leituras exploratórias: ALTA 2.", self.out)

    def test_a_session_that_dropped_reads_nothing_and_keeps_going(self):
        self.calibrate()

        def log_out(session, seconds):
            session.asset = None

        session = ChartSession([chart_for(rising_bars())], on_wait=log_out)
        code = self.run_chart("--count", "3", session=session)
        self.assertEqual(code, 0, self.out)
        self.assertEqual(self.out.count("o ativo não pôde ser lido (o seletor não encontrou nenhum elemento); a sessão pode ter caído"), 2)
        self.assertEqual(session.captures, 1)

    def test_an_asset_that_changed_during_the_capture_is_not_used(self):
        self.calibrate()
        session = ChartSession([chart_for(rising_bars())])
        capture = ChartSession.capture

        def switching(self_, selector, timeout_ms, strip=None):
            result = capture(self_, selector, timeout_ms, strip)
            self_.asset = "GBP/USD OTC"  # someone switched the asset during the capture
            return result

        with mock.patch.object(ChartSession, "capture", switching):
            code = self.run_chart("--count", "1", session=session)
        self.assertEqual(code, 1, self.out)
        self.assertIn("a tela mostrava GBP/USD OTC, e não EUR/USD OTC, depois da captura do gráfico", self.out)
        self.assertIn("DECISÃO VALIDADA: AGUARDAR (sem leitura das velas:", self.out)
        self.assertEqual(self.candle_rows(), [])

    def test_a_page_outside_the_platform_is_never_read(self):
        self.calibrate()
        session = ChartSession([chart_for(rising_bars())])
        session.url = "https://example.org/fora"
        code = self.run_chart("--count", "1", session=session)
        self.assertEqual(code, 1)
        self.assertIn("não está em pocketoption.com; nada foi lido", self.out)
        self.assertEqual([call for call in session.calls if call[0] in ("read", "capture")], [])

    def test_what_must_be_there_before_the_browser_opens(self):
        session = ChartSession([chart_for(rising_bars())])
        self.assertEqual(self.run_chart("--count", "1", session=session, answers=()), 2)
        self.assertIn("chart-candles.json não existe: calibre a leitura das velas antes, com --configure", self.err)
        self.write_sources()  # the quote as text, without the visual reading
        self.calibrate()
        self.assertEqual(self.run_chart("--count", "1", session=session, answers=()), 2)
        self.assertIn("a cotação não usa essa leitura", self.err)
        self.assertEqual(session.calls, [])

    def test_ctrl_c_ends_it_with_what_was_recorded(self):
        self.calibrate()

        def press(session, seconds):
            raise KeyboardInterrupt

        session = ChartSession([chart_for(rising_bars())], on_wait=press)
        code = self.run_chart(session=session)
        self.assertEqual(code, 130)
        self.assertIn("Leituras: 1, 1 com as velas lidas, 39 velas novas gravadas", self.out)
        self.assertEqual(len(self.candle_rows()), 39)


class CandleScanSession(ts.FakeScanSession):
    """FakeScanSession whose whole chart is a CandleChart of the asset drawn now, ending at its quote."""

    def __init__(self, platform, on_wait=None):
        super().__init__(platform, on_wait)
        self.chart_captures = 0

    def capture(self, selector, timeout_ms, strip=None):
        if strip is not None:
            return super().capture(selector, timeout_ms, strip)
        self.platform.advance(0.05)
        self.calls.append(("capture", selector, strip))
        self.chart_captures += 1
        platform = self.platform
        quote = float(platform.quote(platform.drawn))
        drawn = chart_for(rising_bars(last=round(quote, 5)))
        platform.engine.queue = list(drawn.answers)
        return observe.Capture(1, True, drawn.image, drawn.image.width / drawn.scale)


class ScanChartTest(ChartTestCase):
    def run_candle_scan(self, *args, platform, history=None, operate_=False):
        platform.engine = ScriptedEngine(ts.PlatformEngine(platform))
        session = CandleScanSession(platform)
        self.session = session
        out, err = io.StringIO(), io.StringIO()
        program = operate.main if operate_ else scan.main
        patches = contextlib.ExitStack()
        if history is not None:
            patches.enter_context(mock.patch.object(scan.analysis, "load_history", return_value=history))
        with patches, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = program(
                ["--url", ts.URL, *args], open_session=lambda profile: session, read_line=ts.Answers(("",)),
                now=platform.now, monotonic=platform.monotonic, engine=ci.FakeEngineHolder(platform.engine),
            )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def test_each_clean_visit_reads_the_chart_and_records_its_candles(self):
        self.calibrate()
        platform = ts.FakePlatform()
        platform.t = 10.0  # away from the change of candle
        code = self.run_candle_scan("--cycles", "2", "--interval", "3", platform=platform)
        self.assertEqual((code, self.err), (0, ""), self.out)
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertTrue(row["chart_status"].startswith("39 velas completas de 60 s e a que se forma"), row["chart_status"])
            self.assertEqual(row["chart_reading"], chart_analysis.ALTA)
            self.assertTrue(row["chart_decision"].startswith("AGUARDAR (dados insuficientes: 39 velas de 60 s gravadas"))
            self.assertEqual(row["analysis_decision"], "AGUARDAR")
        candle_rows = self.candle_rows()
        self.assertEqual(sorted({row["asset"] for row in candle_rows}), sorted([ts.EUR[0], ts.GBP[0], ts.AUD[0]]))
        self.assertEqual(len(candle_rows), 3 * 39)  # the second cycle, in the same minute, adds none
        for text in (
            "Leitura das velas do gráfico: calibrada, velas de 60 s",
            "  LEITURA EXPLORATÓRIA, não é sinal de entrada: favorece ALTA",
            "  decisão pelas regras das velas: AGUARDAR (dados insuficientes",
            "VELAS DO GRÁFICO (velas de 60 s): 3 ativos com velas gravadas, 0 com regra das velas validada",
            "Velas do gráfico: 6 leituras utilizáveis; velas gravadas em",
        ):
            self.assertIn(text, self.out)
        self.assertEqual(self.session.clicked(), [1, 2, 0, 1, 2])  # the chart reading clicks nothing

    def test_no_chart_is_read_from_the_pause_on(self):
        self.calibrate()
        platform = ts.FakePlatform()
        platform.t = 10.0
        platform.ignored.add(1)  # GBP/USD OTC never switches: the PAUSA
        code = self.run_candle_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.out)
        rows = self.rows()
        self.assertTrue(rows[0]["chart_status"].startswith("39 velas"))
        self.assertEqual([row["chart_status"] for row in rows[1:]], ["", ""])
        self.assertEqual({row["asset"] for row in self.candle_rows()}, {ts.EUR[0]})
        self.assertEqual(self.session.chart_captures, 1)

    def test_a_chart_that_did_not_move_while_the_clock_passed_a_candle_is_refused(self):
        self.calibrate()
        platform = ts.FakePlatform(assets=(ts.EUR,))
        platform.t = 40.0
        code = self.run_candle_scan("--cycles", "2", "--interval", "30", platform=platform)
        self.assertEqual(code, 0, self.out)
        first, second = self.rows()
        self.assertTrue(first["chart_status"].startswith("39 velas"))
        self.assertIn("a leitura não confere com", second["chart_status"])
        self.assertIn("nada desta leitura foi usado nem gravado", second["chart_status"])
        self.assertEqual(len(self.candle_rows()), 39)

    def test_a_validated_rule_of_the_candles_opens_a_simulated_cycle_with_every_protection(self):
        self.calibrate()
        platform = to.RisingPlatform(assets=(ts.EUR,))
        platform.t = 4.5  # the first seconds of a candle: the moment the rule was measured at
        validated = chart_analysis.ChartValidation(
            True, "regra tendência de 5 velas (velas do gráfico) validada: (ARTIFICIAL, para o teste)", chart_analysis.TREND_RULE,
        )
        with mock.patch.object(chart_analysis.ChartSetup, "validation", lambda setup, name: validated):
            code = self.run_candle_scan("--cycles", "1", platform=platform, operate_=True)
        self.assertEqual(code, 0, self.err + self.out)
        rows = to.OperateTestCase.operation_rows(self)
        [started] = [row for row in rows if row["event"] == operation.CYCLE_STARTED]
        self.assertEqual((started["asset"], started["direction"]), (ts.EUR[0], "CALL"))
        self.assertIn("tendência de 5 velas", started["decision_id"])
        entries = [row for row in rows if row["event"] == operation.ENTRY_SUBMITTED]
        self.assertEqual(entries[0]["stake_cents"], "500")
        self.assertTrue(all(row["execution"] == operation.SIMULATED for row in rows))
        [scan_row] = self.rows()
        self.assertTrue(scan_row["analysis_decision"].startswith("CALL"))  # the candles' decision, the quotes' waits
        self.assertTrue(scan_row["chart_decision"].startswith("CALL"))

    def test_a_calibration_that_cannot_be_used_stops_before_the_browser(self):
        (self.runtime / candles.CALIBRATION_FILE_NAME).write_text(json.dumps({"up": "#000000"}), encoding="utf-8")
        platform = ts.FakePlatform()
        code = self.run_candle_scan("--cycles", "1", platform=platform)
        self.assertEqual(code, 2)
        self.assertIn("CONFIGURAÇÃO:", self.err)
        self.assertIn("Calibre de novo com grafico_windows.cmd", self.err)


class SafetyTest(unittest.TestCase):
    def test_the_new_modules_never_act_on_the_page_nor_reach_the_network(self):
        for name in ("candles", "chart_analysis", "chart"):
            with self.subTest(module=name):
                tree = ast.parse((ROOT / "pocketautonomo" / f"{name}.py").read_text(encoding="utf-8"))
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported |= {alias.name.split(".")[0] for alias in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        imported.add((node.module or "").split(".")[0])
                self.assertFalse(imported & {"playwright", "requests", "urllib", "socket", "http", "websocket", "aiohttp"})
                attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                self.assertEqual(attributes & (FORBIDDEN_ATTRIBUTES | {"click_favorite", "screenshot", "goto"}), set())
                names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                self.assertEqual(names & {"eval", "exec", "__import__"}, set())


if __name__ == "__main__":
    unittest.main()
