"""PlaywrightSession and the collector on a real Chromium, headless, with a local test page.

These tests run only where Playwright and its Chromium are installed. Elsewhere, as in the
CI, they are skipped. The page below was written for these tests and is served by
Playwright itself for the reserved name observe.test, so nothing goes to the network and
nothing here touches the Pocket Option. Its CALL and PUT buttons count their clicks, to show
that the reading never clicks them. Passing here says nothing about the real platform.
"""

import contextlib
import csv
import importlib.util
import io
import json
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pocketautonomo import observe

HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
TEST_URL = "https://observe.test/tela/?ssid=SEGREDO#SEGREDO"
SHOWN_URL = "https://observe.test/tela/"
PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Página de teste local</title></head>
<body>
<p>Página de teste do PocketAutonomo; não é a Pocket Option.</p>
<div id="asset">ATIVO DE TESTE</div>
<div id="price">1.00000</div>
<div id="payout">+90%</div>
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


@unittest.skipUnless(HAVE_PLAYWRIGHT, "Playwright não está instalado neste Python")
class RealChromiumTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
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
        self.assertEqual(first.read("#payout", 2_000), observe.ElementText(1, True, "+90%"))
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

    def test_collector_end_to_end_on_the_local_page(self):
        self.runtime.mkdir(parents=True)
        selectors = {"asset": "#asset", "price": "#price", "payout": "#payout"}
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(selectors), encoding="utf-8")
        before = datetime.now(timezone.utc)
        before = before.replace(microsecond=before.microsecond // 1000 * 1000)  # CSV keeps ms
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(
                ["--url", TEST_URL, "--count", "3", "--interval", "1"],
                open_session=self.open_session,
                read_line=lambda: "",
                sleep=lambda seconds: time.sleep(0.3),
            )
        after = datetime.now(timezone.utc)
        self.assertEqual((code, err.getvalue()), (0, ""), out.getvalue())
        [path] = self.runtime.glob("observations-*.csv")
        with open(path, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), observe.CSV_COLUMNS)
        self.assertEqual(len(rows), 3)
        times = [datetime.fromisoformat(row[0]) for row in rows]
        self.assertEqual(times, sorted(times))
        self.assertTrue(before <= times[0] and times[-1] <= after)
        for row in rows:
            self.assertEqual(row[1:4], [SHOWN_URL, "ATIVO DE TESTE", "ok: #asset"])
            self.assertRegex(row[4], r"^1\.[0-9]{5}$")
            self.assertEqual(row[5:], ["ok: #price", "90", "ok: #payout"])
        self.assertGreater(len({row[4] for row in rows}), 1)  # each observation read anew
        self.assertIn("Navegador fechado.", out.getvalue())
        self.assertNotIn("SEGREDO", out.getvalue() + path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
