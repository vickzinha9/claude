"""PlaywrightSession and the collector on a real Chromium, headless, with a local test page.

These tests run only where Playwright and its Chromium are installed. Elsewhere, as in the
main CI jobs, they are skipped, unless POCKETAUTONOMO_REQUIRE_BROWSER_TESTS=1, which makes
a missing Playwright fail them instead: the Windows browser job of the CI sets it. The page
below was written for these tests and is served by Playwright itself for the reserved name
observe.test, so nothing goes to the network and nothing here touches the Pocket Option.
Its CALL and PUT buttons count their clicks, to show that the reading never clicks them.
Passing here says nothing about the real platform.
"""

import contextlib
import csv
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from pocketautonomo import observe

HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
HAVE_OCR = HAVE_PLAYWRIGHT and importlib.util.find_spec("rapidocr") is not None
REQUIRED = os.environ.get("POCKETAUTONOMO_REQUIRE_BROWSER_TESTS") == "1"
TEST_URL = "https://observe.test/tela/?ssid=SEGREDO#SEGREDO"
SHOWN_URL = "https://observe.test/tela/"
PRICE_SELECTOR = 'div[data-field="price"]'  # pasted with its quotes, as DevTools copies it
PAYOUT_SELECTOR = r"#pay\:out"  # the CSS escape of the id pay:out, with its backslash
PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Página de teste local</title></head>
<body>
<p>Página de teste do PocketAutonomo; não é a Pocket Option.</p>
<div id="asset">ATIVO DE TESTE</div>
<div id="price" data-field="price">1.00000</div>
<div id="pay:out">+90%</div>
<div class="twice">a</div><div class="twice">b</div>
<div id="hidden" style="display: none">oculto</div>
<button id="call" onclick="countClick()">CALL</button>
<button id="put" onclick="countClick()">PUT</button>
<span id="clicks">0</span>
<span id="visits"></span>
<script>
  function countClick() {
    const clicks = document.getElementById("clicks");
    clicks.textContent = Number(clicks.textContent) + 1;
  }
  const visits = Number(localStorage.getItem("visits") || 0) + 1;
  localStorage.setItem("visits", String(visits));
  document.getElementById("visits").textContent = String(visits);
  let tick = 0;
  setInterval(() => {
    tick += 1;
    document.getElementById("price").textContent = (1 + tick / 100000).toFixed(5);
  }, 100);
</script>
</body></html>
"""


@unittest.skipUnless(HAVE_PLAYWRIGHT or REQUIRED, "Playwright não está instalado neste Python")
class RealChromiumTest(unittest.TestCase):
    def setUp(self):
        # On Windows, the browser can hold a profile file for a moment after closing.
        options = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
        directory = tempfile.TemporaryDirectory(**options)
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name) / "runtime"
        for name, value in (
            ("RUNTIME_DIR", self.runtime),
            ("ALLOWED_DOMAIN", "observe.test"),
            ("HOST_PATTERN", re.compile(r"observe\.test")),
        ):
            patcher = mock.patch.object(observe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_session(self, profile_dir):
        """A real PlaywrightSession, headless, with observe.test served from PAGE."""
        session = observe.open_playwright_session(profile_dir, headless=True)
        self.addCleanup(session.close)
        session._context.route(
            "https://observe.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=PAGE),
        )
        return session

    def test_reads_without_clicking_and_keeps_the_profile(self):
        profile = self.runtime / "browser-profile"
        first = self.open_session(profile)
        first.open(TEST_URL)
        self.assertEqual(first.current_url(), TEST_URL)
        self.assertEqual(first.read("#asset", 2_000), observe.ElementText(1, True, "ATIVO DE TESTE"))
        self.assertEqual(first.read(PAYOUT_SELECTOR, 2_000), observe.ElementText(1, True, "+90%"))
        self.assertEqual(first.read(".twice", 2_000), observe.ElementText(2))
        self.assertEqual(first.read("#hidden", 2_000), observe.ElementText(1))
        self.assertEqual(first.read("#missing", 2_000), observe.ElementText(0))
        with self.assertRaises(observe.ReadError):
            first.read("[[", 2_000)
        self.assertEqual(first.read("#call", 2_000).text, "CALL")
        self.assertEqual(first.read("#put", 2_000).text, "PUT")
        self.assertEqual(first.read("#clicks", 2_000).text, "0")  # no click reached the buttons
        self.assertEqual(first.read("#visits", 2_000).text, "1")
        first.close()
        self.assertTrue(first.is_closed())
        self.assertTrue(any(profile.iterdir()))
        second = self.open_session(profile)  # the same profile, as in a new run
        second.open(TEST_URL)
        self.assertEqual(second.read("#visits", 2_000).text, "2")  # the page data survived

    def test_configure_and_observe_end_to_end_on_the_local_page(self):
        answers = iter(
            [
                "http://observe.test/tela/",  # refused: not https
                TEST_URL,  # the link, typed in the terminal
                "",  # Enter after the login
                "#asset", "",  # asset: preview, accepted
                ".twice", PRICE_SELECTOR, "",  # price: two elements, then the right selector
                PAYOUT_SELECTOR, "",  # payout: pasted with its backslash, accepted
                "",  # summary: Enter saves and starts the observations
            ]
        )
        before = datetime.now(timezone.utc)
        before = before.replace(microsecond=before.microsecond // 1000 * 1000)  # CSV keeps ms
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(
                ["--configure", "--count", "3", "--interval", "1"],
                open_session=self.open_session,
                read_line=lambda prompt="": next(answers),
            )
        after = datetime.now(timezone.utc)
        self.assertEqual((code, err.getvalue()), (0, ""), out.getvalue())
        self.assertEqual(list(answers), [])
        self.assertIn("Prévia da cotação do ativo, lida agora: INDISPONÍVEL (o seletor encontrou 2 elementos", out.getvalue())
        self.assertIn("Prévia do ativo, lida agora: ATIVO DE TESTE. Confira se é o que a tela mostra.", out.getvalue())
        saved = json.loads((self.runtime / observe.SELECTORS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            {field: saved[field] for field in observe.FIELDS},
            {"asset": "#asset", "price": PRICE_SELECTOR, "payout": PAYOUT_SELECTOR},
        )
        [path] = self.runtime.glob("observations-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), observe.CSV_COLUMNS)
        self.assertEqual(len(rows), 3)
        times = [datetime.fromisoformat(row[0]) for row in rows]
        self.assertEqual(times, sorted(times))
        self.assertTrue(before <= times[0] and times[-1] <= after)
        # The two waits of --interval 1 went through page.wait_for_timeout, in real time.
        self.assertGreaterEqual((times[-1] - times[0]).total_seconds(), 2.0)
        for row in rows:
            self.assertEqual(row[1:4], [SHOWN_URL, "ATIVO DE TESTE", "ok: #asset"])
            self.assertRegex(row[4], r"^1\.[0-9]{5}$")
            self.assertEqual(row[5:10], [f"ok: {PRICE_SELECTOR}", "90", f"ok: {PAYOUT_SELECTOR}", "completa", "texto"])
            self.assertGreater(float(row[11]), 0)  # how long the quote took, in ms
        self.assertGreater(len({row[4] for row in rows}), 1)  # the page kept running between reads
        self.assertIn("Navegador fechado.", out.getvalue())
        self.assertNotIn("SEGREDO", out.getvalue() + path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


# The quotes that the local chart shows, one after the other, 600 ms each.
SEQUENCE = ("1.26543", "1.26571", "1.26518", "1.26602", "1.26489", "1.26555", "1.26530", "1.26590")
CHART_URL = "https://observe.test/grafico/"
CHART_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Gráfico de teste local</title>
<style>
  body { margin: 0; background: #11131c; color: #ccc; font-family: Arial, sans-serif; }
  #wrap { position: relative; width: 700px; height: 360px; margin: 10px; }
  canvas { position: absolute; left: 0; top: 0; width: 700px; height: 360px; }
</style></head>
<body>
<div id="wrap"><canvas id="grid"></canvas><canvas id="plot"></canvas></div>
<p>Gráfico de teste do PocketAutonomo, desenhado em canvas; não é a Pocket Option.</p>
<div id="asset">ATIVO DE TESTE</div>
<div id="payout">+90%</div>
<p>Fechamento anterior: <span id="fechamento">1.26000</span></p>
<p>Amount <input id="amount" value="10"> <span id="amount-text">10</span></p>
<script>
const SEQUENCE = __SEQUENCE__;
const dpr = window.devicePixelRatio || 1, W = 700, H = 360, AXIS = 620, LOW = 1.2600, HIGH = 1.2700;
for (const id of ["grid", "plot"]) { const c = document.getElementById(id); c.width = W * dpr; c.height = H * dpr; }
function y(p) { return 20 + (HIGH - p) / (HIGH - LOW) * (H - 40); }
function draw(price) {
  const g = document.getElementById("grid").getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.fillStyle = "#1b1f2d"; g.fillRect(0, 0, W, H);
  g.strokeStyle = "#262b3b";
  for (let i = 0; i <= 10; i++) { const yy = Math.round(20 + i * (H - 40) / 10) + 0.5; g.beginPath(); g.moveTo(0, yy); g.lineTo(AXIS, yy); g.stroke(); }
  const p = document.getElementById("plot").getContext("2d");
  p.setTransform(dpr, 0, 0, dpr, 0, 0); p.clearRect(0, 0, W, H);
  let last = 1.265;
  for (let x = 10, n = 0; x < AXIS - 30; x += 14, n++) {
    const next = Math.min(HIGH, Math.max(LOW, last + Math.sin(n * 1.7) * 0.0006));
    p.fillStyle = next >= last ? "#26a69a" : "#ef5350";
    p.fillRect(x, y(Math.max(last, next)), 9, Math.max(1, Math.abs(y(next) - y(last))));
    last = next;
  }
  p.font = "11px Arial"; p.textBaseline = "middle"; p.fillStyle = "#8a8f9e";
  for (let i = 0; i <= 10; i++) { const v = HIGH - i * (HIGH - LOW) / 10; p.fillText(v.toFixed(4), AXIS + 6, y(v)); }
  // The cursor's label, gray, above the marker: it must never be read as the quote.
  p.fillStyle = "#4a4f5e"; p.fillRect(AXIS + 1, y(1.2690) - 9, 78, 18);
  p.fillStyle = "#ffffff"; p.font = "12px Arial"; p.fillText("1.26900", AXIS + 6, y(1.2690));
  // The current-quote marker, with its dashed line.
  const yy = y(Number(price));
  p.setLineDash([4, 2]); p.strokeStyle = "#2962ff"; p.beginPath(); p.moveTo(0, Math.round(yy) + 0.5); p.lineTo(AXIS + 1, Math.round(yy) + 0.5); p.stroke(); p.setLineDash([]);
  p.fillStyle = "#2962ff"; p.beginPath(); p.roundRect(AXIS + 1, yy - 9, 78, 18, 3); p.fill();
  p.fillStyle = "#ffffff"; p.fillText(price, AXIS + 6, yy);
}
let index = 0;
draw(SEQUENCE[0]);
setInterval(() => { index = (index + 1) % SEQUENCE.length; draw(SEQUENCE[index]); }, 600);
</script>
</body></html>
""".replace("__SEQUENCE__", json.dumps(list(SEQUENCE)))


@unittest.skipUnless(HAVE_OCR or REQUIRED, "Playwright e OCR local (rapidocr) não estão instalados neste Python")
class RealVisualQuoteTest(unittest.TestCase):
    """The visual reading of the quote, with real Chromium and the real OCR, on a local chart."""

    def setUp(self):
        options = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
        directory = tempfile.TemporaryDirectory(**options)
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name) / "runtime"
        for name, value in (
            ("RUNTIME_DIR", self.runtime),
            ("ALLOWED_DOMAIN", "observe.test"),
            ("HOST_PATTERN", re.compile(r"observe\.test")),
        ):
            patcher = mock.patch.object(observe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_session(self, profile_dir):
        session = observe.open_playwright_session(profile_dir, headless=True)
        self.addCleanup(session.close)
        session._context.route(
            "https://observe.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=CHART_PAGE),
        )
        return session

    def test_configure_and_observe_the_quote_from_the_canvas(self):
        answers = iter(
            [
                CHART_URL,
                "",  # Enter after the login
                "#asset", "",
                "buscar",  # the quote: first, look for it in text
                "ocr",  # then read it from the chart
                "#plot",  # the chart element
                "2",  # 1 is the cursor's label, 2 the current-quote marker
                "",  # previews accepted
                "#payout", "",
                "",  # summary: save and observe
            ]
        )
        shown = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(
                ["--configure", "--count", "4", "--interval", "1"],
                open_session=self.open_session,
                read_line=lambda prompt="": next(answers),
                show_image=shown.append,
            )
        text = out.getvalue()
        self.assertEqual((code, err.getvalue()), (0, ""), text)
        self.assertEqual(list(answers), [])
        # The search lists the text decoy, which the comparison with the screen would reject,
        # and not the Amount, a whole number.
        self.assertIn("  1: 1.26000 | seletor #fechamento", text)
        self.assertNotIn(": 10 |", text)
        # The marker sits at a fractional height, so its anti-aliased box is 17 or 18 px tall.
        self.assertRegex(text, r"  1: caixa #4a4f5e de 78x18 px, a 1 px da borda direita; texto lido: 1\.26900")
        self.assertRegex(text, r"  2: caixa #2962ff de 78x1[78] px, a 1 px da borda direita; texto lido: 1\.26\d{3}")
        previews = re.findall(r"Prévia \d, relógio local [0-9T:.+-]+: ([0-9.]+) \(em \d+ ms\)", text)
        self.assertEqual(len(previews), 3, text)
        self.assertTrue(set(previews) <= set(SEQUENCE), previews)
        self.assertEqual(shown, [self.runtime / "cotacao-candidatos.png", self.runtime / "cotacao-previa.png"])
        saved = json.loads((self.runtime / observe.SELECTORS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual((saved["price"], saved["price_ocr"]["chart"]), ("", "#plot"))
        self.assertEqual(saved["price_ocr"]["marker"]["colors"], ["#2962ff"])
        [path] = self.runtime.glob("observations-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), observe.CSV_COLUMNS)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIn(row[4], SEQUENCE)  # only quotes the chart really showed
            self.assertEqual(row[5], "ok: ocr #plot")
            self.assertEqual(row[8:10], ["completa", "ocr"])
            self.assertLess(datetime.fromisoformat(row[0]), datetime.fromisoformat(row[10]) + timedelta(seconds=1))
            self.assertGreater(float(row[11]), 0)
        self.assertGreater(len({row[4] for row in rows}), 1)  # the quote changed between readings
