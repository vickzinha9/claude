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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pocketautonomo import observe

HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
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
        self.assertIn("Prévia de preço, lida agora: INDISPONÍVEL (o seletor encontrou 2 elementos", out.getvalue())
        self.assertIn("Prévia de ativo, lida agora: ATIVO DE TESTE. Confira se é o que a tela mostra.", out.getvalue())
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
            self.assertEqual(row[5:], [f"ok: {PRICE_SELECTOR}", "90", f"ok: {PAYOUT_SELECTOR}"])
        self.assertGreater(len({row[4] for row in rows}), 1)  # the page kept running between reads
        self.assertIn("Navegador fechado.", out.getvalue())
        self.assertNotIn("SEGREDO", out.getvalue() + path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
