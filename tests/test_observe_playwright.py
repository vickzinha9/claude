"""PlaywrightSession, the collector and the favorites scan on a real Chromium, headless.

These tests run only where Playwright and its Chromium are installed. Elsewhere, as in the
main CI jobs, they are skipped, unless POCKETAUTONOMO_REQUIRE_BROWSER_TESTS=1, which makes
a missing Playwright fail them instead: the Windows browser job of the CI sets it. The
pages below were written for these tests and are served by Playwright itself for the
reserved name observe.test, so nothing goes to the network and nothing here touches the
Pocket Option. Their CALL and PUT buttons, and the BUY and SELL buttons and the Amount
field of the favorites page, count what reaches them, to show that the reading never
clicks them and that the scan clicks only favorites. Passing here says nothing about the
real platform.
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

from pocketautonomo import observe, scan

HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
HAVE_OCR = HAVE_PLAYWRIGHT and importlib.util.find_spec("rapidocr") is not None
REQUIRED = os.environ.get("POCKETAUTONOMO_REQUIRE_BROWSER_TESTS") == "1"
TEST_URL = "https://observe.test/tela/?ssid=SEGREDO#SEGREDO"
# The status of a visual reading of the chart #plot: the note appears only when the two OCR
# readings differed in the decimal separator alone.
OCR_OK = (
    r"^ok: ocr #plot( \(as duas leituras do OCR só divergiram no separador decimal:"
    r" '[0-9]+[.,][0-9]+' e '[0-9]+[.,][0-9]+'\))?$"
)
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



# The local chart starts still at CALIBRATION_QUOTE, with its marker over a green candle,
# as in the validation's calibration; then it shows the quotes of SEQUENCE, 600 ms each,
# most with the marker over the background. Its marker is 80% opaque, like the platform's.
CALIBRATION_QUOTE = "1.26543"
SEQUENCE = ("1.26690", "1.26418", "1.26752", "1.26381", "1.26633", "1.26455", "1.26718", "1.26402")
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
const LABEL = "rgba(74, 118, 168, 0.8)";  // #3f638e over the background, as measured
for (const id of ["grid", "plot"]) { const c = document.getElementById(id); c.width = W * dpr; c.height = H * dpr; }
function y(p) { return 20 + (HIGH - p) / (HIGH - LOW) * (H - 40); }
function draw(price) {
  const g = document.getElementById("grid").getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.fillStyle = "#141726"; g.fillRect(0, 0, W, H);
  g.strokeStyle = "#262b3b";
  for (let i = 0; i <= 10; i++) { const yy = Math.round(20 + i * (H - 40) / 10) + 0.5; g.beginPath(); g.moveTo(0, yy); g.lineTo(AXIS, yy); g.stroke(); }
  const p = document.getElementById("plot").getContext("2d");
  p.setTransform(dpr, 0, 0, dpr, 0, 0); p.clearRect(0, 0, W, H);
  let last = 1.265;
  for (let x = 10, n = 0; x < AXIS - 30; x += 14, n++) {
    const next = Math.min(HIGH, Math.max(LOW, last + Math.sin(n * 1.7) * 0.0006));
    p.fillStyle = next >= last ? "#45b734" : "#ff3e1f";
    p.fillRect(x, y(Math.max(last, next)), 9, Math.max(1, Math.abs(y(next) - y(last))));
    last = next;
  }
  // The last candle, under the marker's columns, as on the platform's chart.
  p.fillStyle = "#45b734"; p.fillRect(AXIS + 4, y(1.2659), 70, y(1.2650) - y(1.2659));
  p.font = "11px Arial"; p.textBaseline = "middle"; p.fillStyle = "#8a8f9e";
  for (let i = 0; i <= 10; i++) { const v = HIGH - i * (HIGH - LOW) / 10; p.fillText(v.toFixed(4), AXIS + 6, y(v)); }
  // The cursor's label, gray and opaque: it must never be read as the quote.
  p.fillStyle = "#4a4f5e"; p.fillRect(AXIS + 1, y(1.2690) - 9, 78, 18);
  p.fillStyle = "#ffffff"; p.font = "12px Arial"; p.fillText("1.26900", AXIS + 6, y(1.2690));
  // The current-quote marker, semi-transparent, with its price line.
  const yy = y(Number(price));
  p.strokeStyle = LABEL; p.beginPath(); p.moveTo(0, Math.round(yy) + 0.5); p.lineTo(AXIS + 1, Math.round(yy) + 0.5); p.stroke();
  p.fillStyle = LABEL; p.beginPath(); p.roundRect(AXIS + 1, yy - 9, 78, 18, 3); p.fill();
  p.fillStyle = "#ffffff"; p.fillText(price, AXIS + 6, yy);
}
let index = -1;
draw(__CALIBRATION__);
window.startCycling = () => setInterval(() => { index = (index + 1) % SEQUENCE.length; draw(SEQUENCE[index]); }, 600);
</script>
</body></html>
""".replace("__SEQUENCE__", json.dumps(list(SEQUENCE))).replace("__CALIBRATION__", json.dumps(CALIBRATION_QUOTE))


@unittest.skipUnless(HAVE_OCR or REQUIRED, "Playwright e OCR local (rapidocr) não estão instalados neste Python")
class RealOcrWarmUpTest(unittest.TestCase):
    def test_the_real_ocr_reads_its_sample_when_warmed_up(self):
        from pocketautonomo.quote_ocr import WARM_UP_SAMPLE, RapidOcrEngine

        warm = RapidOcrEngine().warm_up()
        self.assertEqual(warm.text, WARM_UP_SAMPLE)
        self.assertGreaterEqual(warm.score, 0.9)
        self.assertGreater(warm.ms, 0)


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
        self.session = session
        return session

    def start_cycling(self):
        """Answer 2 to the candidates' question, then let the chart move: the test drives the page."""
        self.session._page.evaluate("startCycling()")
        return "2"

    def test_configure_over_a_candle_and_read_over_the_background(self):
        answers = iter(
            [
                CHART_URL,
                "",  # Enter after the login
                "#asset", "",
                "buscar",  # the quote: first, look for it in text
                "ocr",  # then read it from the chart
                "#plot",  # the chart element; the marker is still over the green candle
                self.start_cycling,  # 1 is the cursor's label, 2 the current-quote marker
                "",  # previews accepted
                "#payout", "",
                "",  # summary: save and observe
            ]
        )

        def read_line(prompt=""):
            answer = next(answers)
            return answer() if callable(answer) else answer

        shown = []
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(
                ["--configure", "--count", "4", "--interval", "1"],
                open_session=self.open_session,
                read_line=read_line,
                show_image=shown.append,
            )
        text = out.getvalue()
        self.assertEqual((code, err.getvalue()), (0, ""), text)
        self.assertEqual(list(answers), [])
        # The search lists the text decoy, which the comparison with the screen would reject,
        # and not the Amount, a whole number.
        self.assertIn("  1: 1.26000 | seletor #fechamento", text)
        self.assertNotIn(": 10 |", text)
        # The whole marker is one candidate, read whole, although it lay over the candle.
        self.assertRegex(text, r"  1: caixa #4a4f5e de 78x18 px, a 1 px da borda direita; texto lido: 1\.26900")
        self.assertRegex(text, r"  2: caixa #[0-9a-f]{6} de 78x1[78] px, a 1 px da borda direita; texto lido: 1\.26543")
        previews = re.findall(r"Prévia \d, relógio local [0-9T:.+-]+: ([0-9.]+) \(em \d+ ms: captura \d+ ms \(elemento \d+, tela \d+, decodificação \d+\), detecção \d+ ms, OCR \d+ ms\)", text)
        self.assertEqual(len(previews), 3, text)
        self.assertTrue(set(previews) <= set(SEQUENCE) | {CALIBRATION_QUOTE}, previews)
        self.assertEqual(len(shown), 2)
        self.assertRegex(shown[0].name, r"^cotacao-candidatos-\d{8}-\d{6}-[0-9a-f]{8}\.png$")
        self.assertRegex(shown[1].name, r"^cotacao-previa-\d{8}-\d{6}-[0-9a-f]{8}\.png$")
        saved = json.loads((self.runtime / observe.SELECTORS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual((saved["price"], saved["price_ocr"]["chart"]), ("", "#plot"))
        # The fill over the candle comes first, and the fill over the background is there too,
        # from the same capture. Chromium draws this marker as #498390 over the candle, the
        # color measured on the platform, and about #3f638e over the background.
        colors = [observe.quote_reader.parse_color_hex(color) for color in saved["price_ocr"]["marker"]["colors"]]
        self.assertLessEqual(len(colors), observe.quote_reader.MAX_MODEL_COLORS)
        self.assertTrue(observe.quote_reader.colors_match(colors[0], (0x49, 0x83, 0x90), 4), colors)
        self.assertTrue(any(observe.quote_reader.colors_match(c, (0x3f, 0x63, 0x8e), 4) for c in colors), colors)
        [path] = self.runtime.glob("observations-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), observe.CSV_COLUMNS)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIn(row[4], SEQUENCE)  # only quotes the chart really showed, over the background
            self.assertRegex(row[5], OCR_OK)
            self.assertEqual(row[8:10], ["completa", "ocr"])
            self.assertLess(datetime.fromisoformat(row[0]), datetime.fromisoformat(row[10]) + timedelta(seconds=1))
            self.assertGreater(float(row[11]), 0)
            capture, detection, ocr = (float(value) for value in row[13:16])
            self.assertTrue(capture > 0 and detection > 0 and ocr > 0, row)
        self.assertGreater(len({row[4] for row in rows}), 1)  # the quote changed between readings
        # The OCR was warmed up on its sample before the first reading, and every reading
        # after the candidates captured only the chart's right strip, read with real Chromium.
        self.assertRegex(text, r"OCR local pronto: carregado em \d+ ms; leitura de aquecimento em \d+ ms, antes de qualquer cotação, na amostra 1\.26543: leu '1\.26543'")
        marker = observe.quote_reader.MarkerModel.from_json(saved["price_ocr"]["marker"])
        strip = observe.quote_reader.strip_width(marker)
        [timings] = self.runtime.glob("tempos-observations-*.csv")
        self.assertEqual(timings.name, "tempos-" + path.name)
        with open(timings, newline="", encoding="utf-8") as file:
            header, *lines = list(csv.reader(file))
        lines = [dict(zip(header, line)) for line in lines]
        self.assertEqual([line["quote"] for line in lines], [row[4] for row in rows])
        for line in lines:
            self.assertEqual((line["moment"], line["strip_css"], line["result"]), ("observação", f"{strip:.1f}", "ok"))
            width, height = (int(value) for value in line["image_px"].split("x"))
            self.assertEqual((width, height), (int(strip), 360))  # the strip, not the 700 px of the chart
            self.assertGreater(int(line["png_bytes"]), 0)
            for step in ("count_ms", "visible_ms", "box_ms", "screenshot_ms", "decode_ms", "detection_ms", "ocr_ms"):
                self.assertGreaterEqual(float(line[step]), 0, step)
        self.assertIn("  observação (4 leituras, 4 com cotação): total ", text)


# The favorites page: three favorites, the asset and payout fields, a canvas chart with an
# opaque quote marker, and BUY, SELL, CALL, PUT and an Amount field that count what reaches
# them. A click on a favorite changes the asset's name after ASSET_DELAY ms, the payout after
# PAYOUT_DELAY ms and the chart after CHART_DELAY ms, so that a reading right after the name
# changed would mix the new asset with the old payout and the old quote.
SCAN_ASSETS = (("EUR/USD OTC", 92, 1.08530), ("GBP/USD OTC", 90, 1.27340), ("AUD/CAD OTC", 85, 0.91210))
SCAN_URL = "https://observe.test/favoritos/?ssid=SEGREDO"
# The marker as a calibration of this chart saves it: opaque #2962ff, 78x18 px, 1 px from the
# right edge of the canvas.
SCAN_MARKER = {"colors": ["#2962ff"], "width": 78.0, "height": 18.0, "right_gap": 1.0, "tolerance": 40}
SCAN_PAGE_TEMPLATE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Favoritos de teste local</title>
<style>
  body { margin: 0; background: #11131c; color: #ccc; font-family: Arial, sans-serif; }
  #favs { display: flex; gap: 8px; padding: 8px; }
  .fav { padding: 6px 10px; background: #222634; border-radius: 4px; cursor: pointer; }
  #wrap { position: relative; width: 700px; height: 360px; margin: 10px; }
  #plot { position: absolute; left: 0; top: 0; width: 700px; height: 360px; }
</style></head>
<body>
<div id="favs"></div>
<div id="asset"></div>
<div id="payout"></div>
<div id="wrap"><canvas id="plot"></canvas></div>
<p>Página de teste do PocketAutonomo, com favoritos; não é a Pocket Option.</p>
<p>Amount <input id="amount" value="10"></p>
<button class="trade" id="buy">BUY</button> <button class="trade" id="sell">SELL</button>
<button class="trade" id="call">CALL</button> <button class="trade" id="put">PUT</button>
<span id="trade-clicks">0</span> <span id="amount-touches">0</span> <span id="fav-clicks">-</span>
<script>
const ASSETS = __ASSETS__;
const DEAD = __DEAD__;  // favorites whose click changes nothing, as an asset that became unavailable
const ASSET_DELAY = 300, PAYOUT_DELAY = 1400, CHART_DELAY = 1700;
let named = 0, paid = 0, drawn = 0, tick = 0;
function bump(id) { const e = document.getElementById(id); e.textContent = String(Number(e.textContent) + 1); }
document.querySelectorAll(".trade").forEach((b) => b.addEventListener("click", () => bump("trade-clicks")));
for (const kind of ["focus", "input", "click"]) { document.getElementById("amount").addEventListener(kind, () => bump("amount-touches")); }
ASSETS.forEach(([name, payout], i) => {
  const item = document.createElement("div");
  item.className = DEAD.includes(i) ? "fav fechado" : "fav";
  if (DEAD.includes(i)) item.title = "Ativo fechado";
  item.innerHTML = name + ' <span class="pct">' + payout + "%</span>";
  item.addEventListener("click", () => {
    document.getElementById("fav-clicks").textContent += String(i + 1);
    if (DEAD.includes(i)) return;
    setTimeout(() => { named = i; render(); }, ASSET_DELAY);
    setTimeout(() => { paid = i; render(); }, PAYOUT_DELAY);
    setTimeout(() => { drawn = i; }, CHART_DELAY);
  });
  document.getElementById("favs").appendChild(item);
});
function render() {
  document.getElementById("asset").textContent = ASSETS[named][0];
  document.getElementById("payout").textContent = "+" + ASSETS[paid][1] + "%";
}
const dpr = window.devicePixelRatio || 1, W = 700, H = 360, AXIS = 620;
const plot = document.getElementById("plot"); plot.width = W * dpr; plot.height = H * dpr;
function draw() {
  tick += 1;
  const base = ASSETS[drawn][2], price = (base + 0.00003 * Math.sin(tick / 2)).toFixed(5);
  const p = plot.getContext("2d");
  p.setTransform(dpr, 0, 0, dpr, 0, 0);
  p.fillStyle = "#141726"; p.fillRect(0, 0, W, H);
  for (let x = 10, n = 0; x < AXIS - 30; x += 14, n++) {
    p.fillStyle = n % 3 ? "#45b734" : "#ff3e1f"; p.fillRect(x, 100 + (n * 37) % 150, 9, 30 + n % 20);
  }
  const y = Math.round(180 + (Number(price) - base) * 200000);
  p.fillStyle = "#2962ff"; p.fillRect(AXIS + 1, y - 9, 78, 18);
  p.fillStyle = "#ffffff"; p.font = "12px Arial"; p.textBaseline = "middle"; p.fillText(price, AXIS + 6, y);
}
render(); draw(); setInterval(draw, 250);
</script>
</body></html>
"""


def scan_page(dead=()):
    """The favorites page; the favorites at the indices in dead never switch the asset."""
    assets = json.dumps([list(asset) for asset in SCAN_ASSETS])
    return SCAN_PAGE_TEMPLATE.replace("__ASSETS__", assets).replace("__DEAD__", json.dumps(list(dead)))


SCAN_PAGE = scan_page()


@unittest.skipUnless(HAVE_OCR or REQUIRED, "Playwright e OCR local (rapidocr) não estão instalados neste Python")
class RealScanTest(unittest.TestCase):
    """The favorites scan with real Chromium clicks and the real OCR, on the local favorites page."""

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
        self.counters = None
        self.page = SCAN_PAGE

    def open_session(self, profile_dir):
        """A real ScanSession, headless, that reads the page's counters right before it closes."""
        session = scan.open_scan_session(profile_dir, headless=True)
        original_close = session.close
        self.addCleanup(original_close)
        page = self.page
        session._context.route(
            "https://observe.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page),
        )

        def close():
            if not session.is_closed():
                self.counters = {
                    key: session.read(f"#{key}", 2_000).text for key in ("trade-clicks", "amount-touches", "fav-clicks")
                }
            original_close()

        session.close = close
        return session

    def test_an_unconfirmed_switch_gets_a_diagnostic_read_from_the_page(self):
        self.page = scan_page(dead=(1,))  # GBP/USD OTC: its click never switches the asset
        self.runtime.mkdir(parents=True)
        sources = {"asset": "#asset", "price": "", "payout": "#payout", "price_ocr": {"chart": "#plot", "marker": SCAN_MARKER}}
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(sources), encoding="utf-8")
        (self.runtime / scan.SCAN_SELECTORS_FILE_NAME).write_text(json.dumps({"favorites": ".fav"}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scan.main(
                ["--url", SCAN_URL, "--cycles", "1", "--load-timeout", "5"],
                open_session=self.open_session,
                read_line=lambda prompt="": "",
            )
        text = out.getvalue()
        self.assertEqual(code, 1, text)  # the unconfirmed switch put the scan in PAUSA
        self.assertTrue(err.getvalue().startswith("PAUSA: a troca para GBP/USD OTC no ciclo 1 não foi confirmada"), err.getvalue())
        [path] = self.runtime.glob("scan-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([row["switch"] for row in rows], ["já selecionado", "não confirmada", "confirmada"])
        unconfirmed = rows[1]
        self.assertEqual((unconfirmed["asset"], unconfirmed["price"], unconfirmed["analysis_ready"]), ("", "", "não"))
        diagnostic = unconfirmed["switch_diagnostic"]
        self.assertRegex(
            diagnostic,
            r"^ativo na tela durante a espera: EUR/USD OTC em \d+ leituras; lista: 3 elementos no clique e 3 agora;"
            r" GBP/USD OTC na posição 2 no clique, e agora na\(s\) posição\(ões\) 2; ",
        )
        self.assertRegex(diagnostic, r"posição 1 \(ativo na tela\): texto 'EUR/USD OTC 92%', visível, \d+x\d+ px em \(\d+, \d+\), classe 'fav'")
        self.assertRegex(
            diagnostic,
            r"posição 2 \(clicado\): texto 'GBP/USD OTC 90%', visível, \d+x\d+ px em \(\d+, \d+\), classe 'fav fechado',"
            r" título 'Ativo fechado'",
        )
        self.assertIn("título 'Ativo fechado', html 'GBP/USD OTC <span class=\"pct\">90%</span>'", diagnostic)
        self.assertIn(f"  diagnóstico da troca: {diagnostic}", text)
        # The asset after the abandoned click was read as itself, but its quote and its payout are
        # withheld by the PAUSA: GBP's click, never confirmed, could still change the chart with a delay.
        self.assertEqual(
            (rows[2]["asset"], rows[2]["price"], rows[2]["payout_percent"], rows[2]["analysis_ready"]),
            ("AUD/CAD OTC", "", "", "não"),
        )
        for row in rows[1:]:
            self.assertTrue(row["quote_association"].startswith("PAUSA desde a troca não confirmada para GBP/USD OTC no ciclo 1"))
        self.assertEqual(self.counters, {"trade-clicks": "0", "amount-touches": "0", "fav-clicks": "-23"})

    def test_scan_clicks_only_favorites_and_never_mixes_two_assets(self):
        self.runtime.mkdir(parents=True)
        sources = {"asset": "#asset", "price": "", "payout": "#payout", "price_ocr": {"chart": "#plot", "marker": SCAN_MARKER}}
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(sources), encoding="utf-8")
        answers = iter([SCAN_URL, "", ".fav", ""])  # the link, Enter after the login, the favorites, accept
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scan.main(
                ["--configure", "--cycles", "2", "--interval", "1"],
                open_session=self.open_session,
                read_line=lambda prompt="": next(answers),
            )
        text = out.getvalue()
        self.assertEqual((code, err.getvalue()), (0, ""), text)
        self.assertEqual(list(answers), [])
        self.assertIn("    1: EUR/USD OTC (selecionado agora) | texto 'EUR/USD OTC 92%'", text)
        saved = json.loads((self.runtime / scan.SCAN_SELECTORS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["favorites"], ".fav")
        # Two favorite clicks in the first cycle and three in the second; nothing else was
        # clicked, and the Amount field was never focused, clicked or typed in.
        self.assertEqual(self.counters, {"trade-clicks": "0", "amount-touches": "0", "fav-clicks": "-23123"})
        [path] = self.runtime.glob("scan-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), scan.SCAN_COLUMNS)
        rows = [dict(zip(header, row)) for row in rows]
        names = [asset[0] for asset in SCAN_ASSETS]
        self.assertEqual([row["asset"] for row in rows], names * 2)
        self.assertEqual([row["switch"] for row in rows], ["já selecionado"] + ["confirmada"] * 5)
        bases = {name: base for name, _, base in SCAN_ASSETS}
        for row, (name, payout, base) in zip(rows, SCAN_ASSETS * 2):
            # The quote, read by the OCR, and the payout are the selected asset's: never the
            # previous asset's, although its name changed 1.1 s before its payout and 1.4 s
            # before its chart.
            self.assertEqual(row["completeness"], "completa", row)
            self.assertLess(abs(float(row["price"]) - base), 0.0005, row)
            self.assertEqual(row["payout_percent"], str(payout), row)
            self.assertEqual(row["payout_eligible"], "sim" if payout >= 90 else "não")
            self.assertEqual(row["price_source"], "ocr")
            self.assertRegex(row["price_status"], OCR_OK)
            # Complete and current: usable for analysis, apart from the payout's eligibility.
            self.assertEqual((row["analysis_ready"], row["data_age_ms"] != ""), ("sim", True), row)
            if row["switch"] == "confirmada":
                self.assertLess(abs(float(row["previous_quote"]) - bases[row["previous_asset"]]), 0.0005, row)
                self.assertGreaterEqual(int(row["polls"]), 2)
                self.assertRegex(row["previous_quote_status"], OCR_OK)
                self.assertGreaterEqual(int(row["previous_quote_attempts"]), 1)
        self.assertNotIn("SEGREDO", text + path.read_text(encoding="utf-8"))
        # Every reading of the quote is in the timings file: the attempts before each click,
        # on the previous asset's chart, and the readings after it, with the time since it.
        [timings] = self.runtime.glob("tempos-scan-*.csv")
        with open(timings, newline="", encoding="utf-8") as file:
            header, *lines = list(csv.reader(file))
        lines = [dict(zip(header, line)) for line in lines]
        moments = {line["moment"] for line in lines}
        self.assertEqual(moments, {"sem troca", "referência antes da troca", "depois da troca"})
        after = [line for line in lines if line["moment"] == "depois da troca"]
        self.assertGreater(len(after), 5)  # some read the previous chart and were not kept
        self.assertTrue(all(float(line["since_click_ms"]) > 1000 for line in after))
        refused = [line["verdict"] for line in after if line["verdict"].startswith("recusada: a cotação lida")]
        self.assertTrue(refused, after)  # the previous asset's chart, still on the screen, was refused
        self.assertEqual(sum(line["verdict"] == "aceita" for line in after) >= 5, True)
        self.assertTrue(all(line["strip_css"] for line in lines))
        self.assertIn("Tempos da leitura da cotação, mediana/máximo em ms, por momento da leitura:", text)


@unittest.skipUnless(HAVE_OCR or REQUIRED, "Playwright e OCR local (rapidocr) não estão instalados neste Python")
class RealDiagnosticTest(unittest.TestCase):
    """The A/B diagnostic of the capture with real Chromium and the real OCR, on local pages."""

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
        self.counters = None

    def open_session(self, profile_dir, body=SCAN_PAGE):
        from pocketautonomo import capture_check

        session = capture_check.open_diagnostic_session(profile_dir, headless=True)
        original_close = session.close
        self.addCleanup(original_close)
        session._context.route(
            "https://observe.test/**",
            lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body),
        )

        def close():
            if not session.is_closed() and body is SCAN_PAGE:
                self.counters = {
                    key: session.read(f"#{key}", 2_000).text for key in ("trade-clicks", "amount-touches", "fav-clicks")
                }
            original_close()

        session.close = close
        return session

    def test_the_direct_capture_gives_the_same_pixels_as_page_screenshot(self):
        session = self.open_session(self.runtime / "browser-profile", body=CHART_PAGE)
        session.open(CHART_URL)  # the chart stays still until startCycling, which nothing calls here
        for strip in (154.0, None):
            with self.subTest(strip=strip):
                usual = session.capture("#plot", 5_000, strip=strip)
                direct = session.capture_direct("#plot", 5_000, strip=strip)
                self.assertEqual((direct.image.width, direct.image.height), (usual.image.width, usual.image.height))
                self.assertEqual(direct.image.data, usual.image.data)
                self.assertEqual((direct.css_width, direct.strip_css), (usual.css_width, usual.strip_css))
                self.assertGreater(direct.screenshot_ms, 0)

    def test_the_phases_only_read_the_quote(self):
        from pocketautonomo import capture_check

        self.runtime.mkdir(parents=True)
        sources = {"asset": "#asset", "price": "", "payout": "#payout", "price_ocr": {"chart": "#plot", "marker": SCAN_MARKER}}
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(sources), encoding="utf-8")
        answers = iter(["", "", "", "", ""])  # Enter after the login, then phases A, B, C and D
        out, err = io.StringIO(), io.StringIO()
        shorter = {"SHORT_WAIT": 0.1, "LONG_WAIT": 0.3, "PAIRS": 2, "LONG_READINGS": 1}
        with contextlib.ExitStack() as stack:
            for name, value in shorter.items():
                stack.enter_context(mock.patch.object(capture_check, name, value))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = capture_check.main(
                ["--url", SCAN_URL], open_session=self.open_session, read_line=lambda prompt="": next(answers)
            )
        text = out.getvalue()
        self.assertEqual((code, err.getvalue()), (0, ""), text)
        self.assertEqual(list(answers), [])
        [path] = self.runtime.glob("diagnostico-captura-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), capture_check.DIAGNOSTIC_COLUMNS)
        rows = [dict(zip(header, row)) for row in rows]
        self.assertEqual([row["phase"] for row in rows], ["A"] * 5 + ["B"] * 4 + ["C"] * 4 + ["D"] * 4)
        self.assertEqual({row["method"] for row in rows}, {capture_check.SCREENSHOT, capture_check.DIRECT})
        name, _, base = SCAN_ASSETS[0]  # the asset already selected: the diagnostic never switches
        for row in rows:
            self.assertEqual((row["result"], row["asset"]), ("ok", name), row)
            self.assertLess(abs(float(row["quote"]) - base), 0.0005, row)
            self.assertGreater(float(row["screenshot_ms"]), 0)
            self.assertEqual(row["window"], "normal")  # the browser told it: nothing was minimized here
        # Nobody minimized the window in phase D, and the diagnostic says so.
        self.assertIn("AVISO: na fase D, o navegador não informou a janela minimizada (normal)", text)
        self.assertIn("RESULTADO DO DIAGNÓSTICO", text)
        self.assertIn("Conclusão, só sobre este computador neste momento:", text)
        # Nothing was clicked: no favorite, no trade button, and the Amount was never touched.
        self.assertEqual(self.counters, {"trade-clicks": "0", "amount-touches": "0", "fav-clicks": "-"})
        self.assertNotIn("SEGREDO", text + path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
