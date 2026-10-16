"""Tests of the A/B diagnostic of the capture, pocketautonomo.capture_check.

No test here opens a browser. FakeDiagnosticSession stands in for DiagnosticSession: its two
captures return a synthetic chart, and the time of the step "tela" comes from a scenario
that knows the method, the wait before the capture and whether the window is covered, which
the answers to the phases change, as you would by maximizing the terminal. These tests show
how the diagnostic measures and concludes, not what your computer does: only the run on it
can say that.
"""

import ast
import base64
import contextlib
import csv
import io
import json
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart_images  # noqa: E402
from test_observe import BLUE_MARKER, FORBIDDEN_ATTRIBUTES, Answers, FakeClock, FakeMonotonic, shown  # noqa: E402

from pocketautonomo import capture_check, observe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
URL = "https://pocketoption.com/pt/tela-de-teste/"  # invented path, never opened
VISUAL = {"chart": "#grafico", "marker": BLUE_MARKER}


class FakeDiagnosticSession:
    """Stands in for DiagnosticSession; tela(method, wait, covered) gives the step's time."""

    def __init__(self, tela, asset="USD/JPY OTC"):
        self.tela = tela
        self.asset = asset
        self.covered = False
        self.last_wait = None
        self.calls, self.waits = [], []
        self.window_closed = False
        self.closes = 0

    def open(self, url):
        self.calls.append(("open", url))

    def current_url(self):
        return URL

    def is_closed(self):
        return self.window_closed

    def read(self, selector, timeout_ms):
        self.calls.append(("read", selector))
        return shown(self.asset)

    def wait(self, seconds):
        self.waits.append(seconds)
        self.last_wait = seconds

    def _capture(self, method, strip):
        self.calls.append((method, strip))
        image = chart_images.right_strip(chart_images.chart(marker_y=150), strip)
        ms = self.tela(method, self.last_wait, self.covered)
        return observe.Capture(1, True, image, image.width, element_ms=4.0, screenshot_ms=ms, decode_ms=1.0, count_ms=2.0, strip_css=strip)

    def capture(self, selector, timeout_ms, strip=None):
        return self._capture(capture_check.SCREENSHOT, strip)

    def capture_direct(self, selector, timeout_ms, strip=None):
        return self._capture(capture_check.DIRECT, strip)

    def close(self):
        self.closes += 1


class SeparatorEngine:
    """Stands in for the OCR: the gray reading has a comma and the black and white one a point."""

    def __init__(self):
        self.calls = 0

    def recognize(self, gray):
        self.calls += 1
        return ("162,192", 0.97) if self.calls % 2 else ("162.192", 0.96)


class DiagnosticTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name) / "runtime"
        patcher = mock.patch.object(observe, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_selectors(asset="#ativo", price="", payout="#payout", price_ocr=VISUAL)

    def write_selectors(self, **data):
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(data), encoding="utf-8")

    def run_check(self, tela, answers=None, engine=None):
        session = FakeDiagnosticSession(tela)
        self.session = session

        def phase(covered):
            def answer():
                session.covered = covered
                return ""

            return answer

        answers = answers or ["", phase(False), phase(True), phase(False)]  # login, then A, B and C
        self.answers = Answers(answers)
        self.opened = []

        def open_session(profile_dir):
            self.opened.append(profile_dir)
            return session

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = capture_check.main(
                ["--url", URL],
                open_session=open_session,
                read_line=self.answers,
                now=FakeClock(),
                monotonic=FakeMonotonic(),
                engine=chart_images.FakeEngineHolder(engine),
            )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def rows(self):
        [path] = self.runtime.glob("diagnostico-captura-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), capture_check.DIAGNOSTIC_COLUMNS)
        return [dict(zip(header, row)) for row in rows]


class PhasesTest(DiagnosticTestCase):
    def test_a_covered_window_is_found_with_one_variable_at_a_time(self):
        code = self.run_check(lambda method, wait, covered: 2000.0 if covered else 150.0)
        self.assertEqual((code, self.err), (0, ""), self.out)
        self.assertEqual(self.answers.pending, [])
        # The phases, each started with Enter, and the plan of each one.
        self.assertIn("FASE A: navegador visível.", self.out)
        self.assertIn("FASE B: navegador coberto por este terminal. Maximize este terminal", self.out)
        self.assertIn("FASE C: navegador visível de novo.", self.out)
        strips = {call[1] for call in self.session.calls if call[0] in (capture_check.SCREENSHOT, capture_check.DIRECT)}
        self.assertEqual(strips, {observe.quote_reader.strip_width(observe.quote_reader.MarkerModel.from_json(BLUE_MARKER))})
        methods = [call[0] for call in self.session.calls if call[0] in (capture_check.SCREENSHOT, capture_check.DIRECT)]
        self.assertEqual(len(methods), 12 + 3 + 12 + 12)
        self.assertEqual(methods[:4], [capture_check.SCREENSHOT, capture_check.DIRECT, capture_check.DIRECT, capture_check.SCREENSHOT])
        self.assertEqual(self.session.waits, [0.5] * 12 + [5.0] * 3 + [0.5] * 24)
        rows = self.rows()
        self.assertEqual(len(rows), 39)
        self.assertEqual([row["phase"] for row in rows], ["A"] * 15 + ["B"] * 12 + ["C"] * 12)
        self.assertEqual({row["method"] for row in rows}, {capture_check.SCREENSHOT, capture_check.DIRECT})
        self.assertEqual({row["result"] for row in rows}, {"ok"})
        self.assertEqual({row["asset"] for row in rows}, {"USD/JPY OTC"})
        self.assertEqual({row["screenshot_ms"] for row in rows if row["phase"] == "B"}, {"2000.0"})
        # The comparisons, and the only clear difference: the covered window.
        self.assertIn("1. Método, na fase A, com espera de 0,5 s: page.screenshot 150 ms × captura direta 150 ms: sem diferença clara.", self.out)
        self.assertIn("2. Espera antes da captura, na fase A, com page.screenshot: 0,5 s 150 ms × 5 s 150 ms: sem diferença clara.", self.out)
        self.assertIn("visível (A) 150 ms × coberta (B) 2000 ms × visível de novo (C) 150 ms; com a captura direta, coberta (B) 2000 ms.", self.out)
        self.assertIn(
            "  - a captura fica lenta quando outra janela cobre o navegador e volta ao normal quando ele fica"
            " visível também com a captura direta.",
            self.out,
        )
        self.assertNotIn("preparação da página pelo Playwright", self.out)
        self.assertIn("Resposta da página à contagem do elemento do gráfico: mediana 2 ms, rápida.", self.out)
        self.assertIn("OCR: 39 de 39 leituras com cotação;", self.out)
        self.assertEqual(self.session.closes, 1)
        self.assertNotIn("click", [call[0] for call in self.session.calls])

    def test_each_variable_is_concluded_only_when_its_difference_is_clear(self):
        cases = {
            "o page.screenshot é mais lento que a captura direta": lambda m, w, c: 1500.0 if m == capture_check.SCREENSHOT else 120.0,
            "a captura depois de uma espera de 5 s é mais lenta": lambda m, w, c: 2000.0 if w == 5.0 else 150.0,
            "nenhuma demora apareceu": lambda m, w, c: 140.0,
            "Causa não identificada por este diagnóstico": lambda m, w, c: 2000.0,
            "resultado misto": lambda m, w, c: 1400.0 if m == capture_check.SCREENSHOT else 900.0,
        }
        for expected, tela in cases.items():
            with self.subTest(expected=expected):
                for path in self.runtime.glob("diagnostico-captura-*.csv"):
                    path.unlink()
                self.assertEqual(self.run_check(tela), 0, self.out)
                conclusion = self.out.split("Conclusão, só sobre este computador neste momento:")[1]
                self.assertIn(expected, conclusion)
                if expected != "a captura fica lenta quando outra janela cobre":
                    self.assertNotIn("outra janela cobre o navegador", conclusion)

    def test_the_ocr_agreements_on_the_separator_are_counted(self):
        code = self.run_check(lambda m, w, c: 150.0, engine=SeparatorEngine())
        self.assertEqual(code, 0, self.out)
        self.assertIn("OCR: 39 de 39 leituras com cotação; em 39 delas, as duas leituras só divergiram no separador decimal", self.out)
        self.assertEqual({row["quote"] for row in self.rows()}, {"162.192"})
        self.assertIn("cotação 162.192 (as duas leituras do OCR só divergiram no separador decimal: '162,192' e '162.192')", self.out)


class StopTest(DiagnosticTestCase):
    def test_without_the_visual_source_the_browser_never_opens(self):
        self.write_selectors(asset="#ativo", price="#cotacao", payout="#payout")
        code = self.run_check(lambda m, w, c: 150.0, answers=[])
        self.assertEqual(code, 2)
        self.assertIn("não tem a leitura visual da cotação (price_ocr)", self.err)
        self.assertEqual(self.opened, [])

    def test_ctrl_c_keeps_what_was_measured(self):
        code = self.run_check(lambda m, w, c: 150.0, answers=["", "", KeyboardInterrupt])
        self.assertEqual(code, 130)
        self.assertIn("Interrompido por Ctrl+C.", self.out)
        self.assertEqual({row["phase"] for row in self.rows()}, {"A"})  # phase A was kept in the CSV
        self.assertIn("RESULTADO DO DIAGNÓSTICO", self.out)
        self.assertEqual(self.session.closes, 1)

    def test_a_page_outside_pocketoption_stops_without_reading(self):
        session_url = "https://example.org/fora"
        with mock.patch.object(FakeDiagnosticSession, "current_url", lambda self: session_url):
            code = self.run_check(lambda m, w, c: 150.0, answers=[""])
        self.assertEqual(code, 1)
        self.assertIn("não está em pocketoption.com; o diagnóstico não lê fora dela e parou", self.err)
        self.assertFalse([call for call in self.session.calls if call[0] in (capture_check.SCREENSHOT, capture_check.DIRECT)])


def fake_png(width, height):
    png = chart_images.RgbImage(width, height, bytes(width * height * 3))
    return base64.b64encode(observe.quote_reader.annotated_png(png, [])).decode("ascii")


def stdlib_decode(data):
    """RgbImage of a PNG written by annotated_png (RGB, filter 0 on every row), without Pillow."""
    width, height = struct.unpack(">II", data[16:24])
    chunks, position = [], 8
    while position < len(data):
        (length,) = struct.unpack(">I", data[position:position + 4])
        kind = data[position + 4:position + 8]
        if kind == b"IDAT":
            chunks.append(data[position + 8:position + 8 + length])
        position += 12 + length
    raw = zlib.decompress(b"".join(chunks))
    stride = width * 3 + 1
    pixels = b"".join(raw[row * stride + 1:(row + 1) * stride] for row in range(height))
    return chart_images.RgbImage(width, height, pixels)


class FakeDevTools:
    def __init__(self, metrics, shot):
        self.metrics, self.shot, self.sent = metrics, shot, []

    def send(self, method, params):
        self.sent.append((method, params))
        return self.metrics if method == "Page.getLayoutMetrics" else self.shot(params)


class DirectCaptureTest(unittest.TestCase):
    """DiagnosticSession on fake Playwright objects: the clip it sends and its only commands."""

    def setUp(self):
        patcher = mock.patch.object(observe, "decode_png", stdlib_decode)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, box, metrics):
        page = types.SimpleNamespace(calls=[])
        locator = types.SimpleNamespace(
            count=lambda: 1, is_visible=lambda: True, bounding_box=lambda *, timeout: dict(box)
        )
        page.locator = lambda selector: locator

        def shot(params):
            clip = params["clip"]
            return {"data": fake_png(round(clip["width"] * self.device), round(clip["height"] * self.device))}

        devtools = FakeDevTools(metrics, shot)
        created = []
        context = types.SimpleNamespace(new_cdp_session=lambda target: created.append(target) or devtools)
        session = object.__new__(capture_check.DiagnosticSession)
        session._page, session._context, session._error_type, session._devtools = page, context, RuntimeError, None
        return session, devtools, created

    def metrics(self, width=1366, height=900, scale=1.0, page_x=0.0, page_y=0.0):
        return {
            "cssVisualViewport": {"clientWidth": width, "clientHeight": height, "pageX": page_x, "pageY": page_y, "scale": scale},
            "visualViewport": {"clientWidth": width, "clientHeight": height, "pageX": page_x, "pageY": page_y, "scale": scale},
        }

    def test_the_clip_is_playwrights_and_only_two_commands_are_sent(self):
        self.device = 1.25
        session, devtools, created = self.session({"x": 100.5, "y": 80.0, "width": 554.0, "height": 700.0}, self.metrics())
        capture = session.capture_direct("canvas.layer.plot", 5_000, strip=154.0)
        self.assertEqual([method for method, _ in devtools.sent], ["Page.getLayoutMetrics", "Page.captureScreenshot"])
        params = devtools.sent[1][1]
        self.assertEqual(params["clip"], {"x": 100.5 + 554.0 - 154.0, "y": 80.0, "width": 154, "height": 700, "scale": 1.0})
        self.assertEqual((params["format"], params["captureBeyondViewport"]), ("png", False))
        self.assertEqual((capture.image.width, capture.image.height), (192, 875))
        self.assertEqual((capture.css_width, capture.strip_css, capture.clipped), (154.0, 154.0, False))
        self.assertGreaterEqual(capture.screenshot_ms, 0.0)
        session.capture_direct("canvas.layer.plot", 5_000, strip=154.0)
        self.assertEqual(len(created), 1)  # one DevTools session for the whole run

    def test_a_strip_outside_the_window_is_refused_before_any_capture(self):
        self.device = 1.0
        session, devtools, _ = self.session({"x": 900.0, "y": 80.0, "width": 554.0, "height": 700.0}, self.metrics(width=1366))
        capture = session.capture_direct("canvas.layer.plot", 5_000, strip=154.0)
        self.assertTrue(capture.clipped)
        self.assertIsNone(capture.image)
        self.assertEqual([method for method, _ in devtools.sent], ["Page.getLayoutMetrics"])

    def test_commands_outside_the_read_only_list_are_refused(self):
        self.device = 1.0
        session, devtools, created = self.session({"x": 0, "y": 0, "width": 10, "height": 10}, self.metrics())
        for method in ("Runtime.evaluate", "Input.dispatchMouseEvent", "Network.getCookies", "Page.navigate"):
            with self.subTest(method=method), self.assertRaises(observe.ReadError):
                session._cdp(method, {})
        self.assertEqual((created, devtools.sent), ([], []))
        self.assertEqual(capture_check.READ_ONLY_CDP_METHODS, ("Page.getLayoutMetrics", "Page.captureScreenshot"))

    def test_an_answer_of_another_shape_is_a_read_error(self):
        self.device = 1.0
        session, devtools, _ = self.session({"x": 0, "y": 0, "width": 300, "height": 200}, {"unexpected": True})
        with self.assertRaises(observe.ReadError) as raised:
            session.capture_direct("canvas.layer.plot", 5_000, strip=154.0)
        self.assertIn("resposta inesperada do DevTools", str(raised.exception))


class SourceTest(unittest.TestCase):
    def test_the_diagnostic_only_reads_and_sends_only_read_only_commands(self):
        tree = ast.parse((ROOT / "pocketautonomo" / "capture_check.py").read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes & (FORBIDDEN_ATTRIBUTES | {"sleep", "screenshot"}), set())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertEqual(names & {"getattr", "setattr", "eval", "exec", "__import__"}, set())
        sends = []
        cdp_calls = []
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "send":
                        sends.append(function.name)
                    if node.func.attr == "_cdp":
                        cdp_calls.append(node)
        self.assertEqual(sends, ["_cdp"])  # the only way to the DevTools session
        self.assertTrue(cdp_calls)
        for call in cdp_calls:
            self.assertIsInstance(call.args[0], ast.Constant)
            self.assertIn(call.args[0].value, capture_check.READ_ONLY_CDP_METHODS)


if __name__ == "__main__":
    unittest.main()
