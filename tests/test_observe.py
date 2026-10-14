"""Tests of the read-only collector, pocketautonomo.observe.

No test here opens a browser or reaches the network, and none needs Playwright. FakeSession
stands in for the browser page, and the fake playwright.sync_api module below stands in for
Playwright. Both have only the read methods the collector may call, so a click, a key
press, a script or a cookie read would fail with AttributeError. Every selector and value
below was invented for the tests; none of them came from the real platform.
"""

import argparse
import ast
import collections
import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from pocketautonomo import observe

ROOT = Path(__file__).resolve().parent.parent
URL = "https://pocketoption.com/pt/tela-de-teste/"  # invented path, never opened
SECRET_URL = URL + "?ssid=SEGREDO#SEGREDO"  # the query and the fragment must never show up
START = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SELECTORS = {"asset": "#ativo-teste", "price": "#preco-teste", "payout": "#payout-teste"}

# Attributes that would act on the page, run code in it or reach secrets or the network.
FORBIDDEN_ATTRIBUTES = {
    "add_cookies", "add_init_script", "add_script_tag", "check", "clear_cookies", "click",
    "content", "cookies", "dblclick", "dispatch_event", "drag_and_drop", "drag_to",
    "evaluate", "evaluate_all", "evaluate_handle", "expect_request", "expect_response",
    "expect_websocket", "expose_binding", "expose_function", "fill", "focus", "hover",
    "keyboard", "mouse", "on", "once", "press", "press_sequentially", "request", "route",
    "route_from_har", "route_web_socket", "screenshot", "select_option", "select_text",
    "set_checked", "set_extra_http_headers", "set_input_files", "storage_state", "tap",
    "touchscreen", "type", "uncheck",
}

# What one main() call returned and printed, with its FakeSession and FakeSleep.
Run = collections.namedtuple("Run", "code out err session sleep")


def shown(text):
    """What a selector finds when it matches exactly one visible element with text."""
    return observe.ElementText(1, True, text)


def press_ctrl_c():
    raise KeyboardInterrupt


class FakeSession:
    """Stands in for PlaywrightSession.

    elements maps a selector to what read() answers: an ElementText, or an exception to
    raise. A list gives one answer per read, and its last answer repeats.
    """

    def __init__(self, elements=None, url=URL):
        self.elements = dict(elements or {})
        self.url = url
        self.opened = []
        self.reads = []
        self.window_closed = False
        self.closes = 0

    def open(self, url):
        self.opened.append(url)

    def current_url(self):
        return self.url

    def is_closed(self):
        return self.window_closed

    def read(self, selector, timeout_ms):
        self.reads.append(selector)
        answer = self.elements[selector]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closes += 1


class FakeClock:
    """Stands in for the UTC clock: START, then 5 seconds later at each call."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        moment = START + timedelta(seconds=5 * self.calls)
        self.calls += 1
        return moment


class FakeSleep:
    """Stands in for time.sleep: records each wait and runs actions[n] on the n-th call."""

    def __init__(self, actions=None):
        self.calls = []
        self.actions = actions or {}

    def __call__(self, seconds):
        self.calls.append(seconds)
        action = self.actions.get(len(self.calls))
        if action is not None:
            action()


class FakePlaywrightError(Exception):
    """Stands in for playwright.sync_api.Error."""


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def count(self):
        self._page.calls.append(("count", self._selector))
        return len(self._page.dom.get(self._selector, ()))

    def is_visible(self):
        self._page.calls.append(("is_visible", self._selector))
        return self._page.dom[self._selector][0][1]

    def inner_text(self, *, timeout):
        self._page.calls.append(("inner_text", self._selector, timeout))
        return self._page.dom[self._selector][0][0]


class FakePage:
    """dom maps a selector to the (text, visible) of each element it matches."""

    def __init__(self, dom, goto_error=None):
        self.dom = dom
        self.url = "about:blank"
        self.calls = []
        self._goto_error = goto_error

    def goto(self, url, *, wait_until, timeout):
        self.calls.append(("goto", url, wait_until, timeout))
        self.url = url  # like a page that loaded but did not finish in time
        if self._goto_error is not None:
            raise FakePlaywrightError(self._goto_error)

    def is_closed(self):
        return False

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return FakeLocator(self, selector)


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context, launch_error=None):
        self.context = context
        self.launches = []
        self._launch_error = launch_error

    def launch_persistent_context(self, user_data_dir, *, headless, no_viewport):
        self.launches.append((user_data_dir, headless, no_viewport))
        if self._launch_error is not None:
            raise FakePlaywrightError(self._launch_error)
        return self.context


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


def fake_playwright_modules(playwright):
    """sys.modules entries that make `from playwright.sync_api import ...` load the fakes."""
    module = types.ModuleType("playwright.sync_api")
    module.Error = FakePlaywrightError
    module.sync_playwright = lambda: types.SimpleNamespace(start=lambda: playwright)
    return {"playwright": types.ModuleType("playwright"), "playwright.sync_api": module}


class UrlTest(unittest.TestCase):
    def test_only_https_pages_on_pocketoption_are_accepted(self):
        accepted = (
            URL,
            "https://m.pocketoption.com/",
            "HTTPS://PocketOption.COM/pt/",
            "https://pocketoption.com:443/pt/?ssid=SEGREDO#x",
            "  https://pocketoption.com/  ",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(observe.official_url(url), url.strip())
        refused = (
            "http://pocketoption.com/",
            "ftp://pocketoption.com/",
            "pocketoption.com/pt/",
            "https://pocketoption.com.example.org/",
            "https://evilpocketoption.com/",
            "https://pocketoption.com./",
            "https://user:SEGREDO@pocketoption.com/",
            "https://pocketoption.com@example.org/",
            "https://pocketoption.com:8443/",
            "https://pocketoption.com:porta/",
            "https://pocketoption.com/a b",
            "https://pocketoption.com/a\tb",
            "https://[::1]/",
            "",
        )
        for url in refused:
            with self.subTest(url=url):
                with self.assertRaises(argparse.ArgumentTypeError) as raised:
                    observe.official_url(url)
                self.assertNotIn("SEGREDO", str(raised.exception))  # the URL is never repeated

    def test_shown_urls_keep_no_user_port_query_or_fragment(self):
        self.assertEqual(
            observe.display_url("https://PocketOption.com:443/pt/x/?ssid=SEGREDO#SEGREDO"),
            "https://pocketoption.com/pt/x/",
        )
        self.assertEqual(observe.display_url("https://example.org/entrar?ssid=SEGREDO"), "https://example.org")
        self.assertEqual(observe.display_url("https://user:senha@example.org/"), "https://example.org")
        self.assertEqual(observe.display_url("about:blank"), "about:")
        self.assertEqual(observe.display_url("data:text/html,SEGREDO"), "data:")
        self.assertEqual(
            observe.redact(f"Page.goto: net::ERR_FAILED at {SECRET_URL}, waiting"),
            f"Page.goto: net::ERR_FAILED at {URL} waiting",
        )


class ObserveTestCase(unittest.TestCase):
    """Every test uses its own runtime directory inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name) / "runtime"
        patcher = mock.patch.object(observe, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile_dirs = []
        self.logins = 0

    def write_selectors(self, **selectors):
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(selectors), encoding="utf-8")

    def run_main(self, *args, session=None, read_line=None, sleep=None, url=URL, real_adapter=False):
        """Run main() with --url url and args, capturing what it prints.

        Unless real_adapter is true, main() gets session, or a new FakeSession, instead of
        opening Playwright.
        """
        session = FakeSession() if session is None else session
        sleep = FakeSleep() if sleep is None else sleep

        def open_session(profile_dir):
            self.profile_dirs.append(profile_dir)
            return session

        def login():
            self.logins += 1
            return "" if read_line is None else read_line()

        options = {"read_line": login, "sleep": sleep, "now": FakeClock()}
        if not real_adapter:
            options["open_session"] = open_session
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(["--url", url, *args], **options)
        return Run(code, out.getvalue(), err.getvalue(), session, sleep)

    def csv_files(self):
        return sorted(self.runtime.glob("observations-*.csv"))

    def csv_rows(self):
        """The data rows of the only CSV of the run, after checking its header."""
        files = self.csv_files()
        self.assertEqual(len(files), 1, files)
        with open(files[0], newline="", encoding="utf-8") as file:
            rows = list(csv.reader(file))
        self.assertEqual(tuple(rows[0]), observe.CSV_COLUMNS)
        return rows[1:]


class SelectorsAndValuesTest(ObserveTestCase):
    def test_selectors_file_is_created_empty_and_checked(self):
        path = self.runtime / observe.SELECTORS_FILE_NAME
        none = {"asset": None, "price": None, "payout": None}
        self.assertEqual(observe.load_selectors(path), none)  # no file: no selector
        self.assertTrue(observe.create_selectors_file(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), observe.SELECTORS_TEMPLATE)
        self.assertEqual(observe.load_selectors(path), none)
        # A BOM, a comment key, spaces, "" and null are accepted; the file is never overwritten.
        path.write_text('\ufeff{"_nota": 1, "asset": " #a ", "price": "", "payout": null}', encoding="utf-8")
        self.assertFalse(observe.create_selectors_file(path))
        self.assertEqual(observe.load_selectors(path), {"asset": "#a", "price": None, "payout": None})
        for content in ("{", "[]", '{"payot": "#p"}', '{"price": 1}'):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(observe.ConfigError):
                observe.load_selectors(path)

    def test_values_are_checked_before_being_accepted(self):
        accepted = {
            observe.parse_asset: [("EUR/USD OTC", "EUR/USD OTC"), ("  Ativo   de teste ", "Ativo de teste")],
            observe.parse_price: [("1.08534", "1.08534"), ("62,345.12", "62,345.12"), (" 100 ", "100")],
            observe.parse_payout: [("+92%", "92"), ("92 %", "92"), ("85.5%", "85.5"), ("90\u00a0%", "90")],
        }
        refused = {
            observe.parse_asset: ["", "  ", "1.08534", "EUR/USD\n92%", "A" * 61, "=HYPERLINK(1)", "-teste"],
            observe.parse_price: ["", "Preço", "1.0853 ▲", "-1.2", "1..2", "1.2%"],
            observe.parse_payout: ["", "92", "%", "1000%", "Payout 92%", "-92%"],
        }
        for parse, cases in accepted.items():
            for text, value in cases:
                with self.subTest(parse=parse.__name__, text=text):
                    self.assertEqual(parse(text), (value, None))
        for parse, texts in refused.items():
            for text in texts:
                with self.subTest(parse=parse.__name__, text=text):
                    value, reason = parse(text)
                    self.assertIsNone(value)
                    self.assertTrue(reason)


class ObserveOnceTest(ObserveTestCase):
    def test_each_field_comes_only_from_its_own_selector(self):
        selectors = {"asset": "#ativo", "price": "#preco", "payout": None}
        session = FakeSession({"#ativo": shown("EUR/USD OTC"), "#preco": shown("1.08534")})
        observation = observe.observe_once(session, selectors, FakeClock())
        self.assertEqual(session.reads, ["#ativo", "#preco"])  # no selector, no reading
        self.assertEqual((observation.captured_at, observation.page), (START, URL))
        self.assertTrue(observation.valid)
        self.assertEqual(
            observation.readings,
            {
                "asset": observe.FieldReading("EUR/USD OTC", "ok: #ativo"),
                "price": observe.FieldReading("1.08534", "ok: #preco"),
                "payout": observe.FieldReading(None, "indisponível: sem seletor em observe-selectors.json"),
            },
        )
        failures = (
            (observe.ElementText(0), "o seletor não encontrou nenhum elemento"),
            (observe.ElementText(2), "o seletor encontrou 2 elementos; precisa encontrar exatamente 1"),
            (observe.ElementText(1), "o elemento encontrado não está visível"),
            (observe.ReadError("Error: seletor inválido"), "a leitura do seletor falhou: Error: seletor inválido"),
            (shown("Preço"), "texto não reconhecido como preço: 'Preço'"),
        )
        for answer, reason in failures:
            with self.subTest(reason=reason):
                session = FakeSession({"#preco": answer})
                observation = observe.observe_once(session, {"price": "#preco"}, FakeClock())
                self.assertEqual(observation.readings["price"], observe.FieldReading(None, f"indisponível: {reason}"))
                self.assertFalse(observation.valid)

    def test_a_page_outside_pocketoption_is_not_read(self):
        session = FakeSession({"#ativo": shown("EUR/USD OTC")}, url="https://example.org/entrar?ssid=SEGREDO")
        observation = observe.observe_once(session, {"asset": "#ativo"}, FakeClock())
        self.assertEqual(session.reads, [])
        self.assertFalse(observation.valid)
        self.assertEqual(observation.page, "https://example.org")
        reason = "indisponível: a página atual (https://example.org) não está em pocketoption.com; nada foi lido"
        self.assertEqual({r.status for r in observation.readings.values()}, {reason})


class MainTest(ObserveTestCase):
    def test_valid_observations_go_to_a_new_csv_in_runtime(self):
        self.write_selectors(**SELECTORS)
        session = FakeSession(
            {
                "#ativo-teste": shown("EUR/USD OTC"),
                "#preco-teste": [shown("1.08534"), shown("1.08540"), shown("1.08529")],
                "#payout-teste": shown("+92%"),
            }
        )
        run = self.run_main("--count", "3", session=session)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.profile_dirs, [self.runtime / "browser-profile"])
        self.assertEqual((session.opened, self.logins, session.closes), ([URL], 1, 1))
        self.assertEqual(session.reads, ["#ativo-teste", "#preco-teste", "#payout-teste"] * 3)
        self.assertEqual(run.sleep.calls, [5.0, 5.0])  # between the observations only
        ok = ["ok: #ativo-teste", "ok: #preco-teste", "ok: #payout-teste"]
        self.assertEqual(
            self.csv_rows(),
            [
                [f"2026-10-14T12:00:{second:02d}.000+00:00", URL, "EUR/USD OTC", ok[0], price, ok[1], "92", ok[2]]
                for second, price in ((0, "1.08534"), (5, "1.08540"), (10, "1.08529"))
            ],
        )
        self.assertRegex(self.csv_files()[0].name, r"^observations-\d{8}-\d{6}-[0-9a-f]{8}\.csv$")
        self.assertIn(
            f"OBSERVAÇÃO 1 | 2026-10-14T12:00:00.000+00:00 | {URL} | ativo EUR/USD OTC"
            " | preço 1.08534 | payout 92% | gravada no CSV",
            run.out,
        )
        self.assertIn("Concluído: 3 observações, 3 válidas gravadas.", run.out)
        self.assertIn("Navegador fechado.", run.out)

    def test_unavailable_fields_are_marked_and_empty_observations_are_not_written(self):
        self.write_selectors(asset="#ativo-teste", price="#preco-teste", payout="")
        session = FakeSession(
            {
                "#ativo-teste": [shown("EUR/USD OTC"), shown("EUR/USD OTC"), observe.ElementText(0)],
                "#preco-teste": [shown("1.08534"), observe.ElementText(0), observe.ElementText(3)],
            }
        )
        run = self.run_main("--count", "3", "--interval", "1", session=session)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertIn("Campos com seletor: ativo, preço | sem seletor, ficam indisponíveis: payout", run.out)
        rows = self.csv_rows()
        self.assertEqual(len(rows), 2)  # the third observation had no available field
        self.assertEqual(
            rows[1][2:],
            [
                "EUR/USD OTC", "ok: #ativo-teste",
                "", "indisponível: o seletor não encontrou nenhum elemento",
                "", "indisponível: sem seletor em observe-selectors.json",
            ],
        )
        self.assertIn("OBSERVAÇÃO 3 | 2026-10-14T12:00:10.000+00:00", run.out)
        self.assertIn("não gravada: nenhum campo disponível", run.out)
        self.assertEqual(run.sleep.calls, [1.0, 1.0])

    def test_without_valid_observations_no_csv_is_created(self):
        self.write_selectors(asset="#ativo-teste")
        session = FakeSession({"#ativo-teste": observe.ElementText(0)})
        run = self.run_main("--count", "2", session=session)
        self.assertEqual(run.code, 1)
        self.assertIn("PARADA: nenhuma observação válida, então nenhum CSV foi criado.", run.err)
        self.assertEqual((self.csv_files(), session.closes), ([], 1))

    def test_without_selectors_nothing_is_read(self):
        session = FakeSession()
        run = self.run_main("--count", "1", session=session)
        self.assertEqual(run.code, 2)
        path = self.runtime / observe.SELECTORS_FILE_NAME
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), observe.SELECTORS_TEMPLATE)
        self.assertIn(f"Criei {path} com os campos vazios", run.out)
        self.assertIn("CONFIGURAÇÃO: nenhum seletor preenchido", run.err)
        self.assertEqual((session.reads, self.logins, session.closes), ([], 1, 1))
        self.assertEqual(self.csv_files(), [])

    def test_ctrl_c_closes_the_browser_and_keeps_the_csv(self):
        self.write_selectors(**SELECTORS)
        session = FakeSession(
            {"#ativo-teste": shown("EUR/USD OTC"), "#preco-teste": shown("1.08534"), "#payout-teste": shown("92%")}
        )
        run = self.run_main(session=session, sleep=FakeSleep({1: press_ctrl_c}))  # until Ctrl+C
        self.assertEqual((run.code, run.err), (130, ""))
        self.assertEqual(len(self.csv_rows()), 1)
        self.assertEqual(session.closes, 1)
        self.assertIn("Interrompido por Ctrl+C: 1 observação, 1 válida gravada.", run.out)
        self.assertIn("CSV preservado:", run.out)
        self.assertIn("Navegador fechado.", run.out)
        # In a terminal, Ctrl+C also stops the Playwright driver, so closing may fail.
        error = observe.BrowserError("Exception: Connection closed while reading from the driver")
        session.close = mock.Mock(side_effect=error)
        run = self.run_main(session=session, sleep=FakeSleep({1: press_ctrl_c}))
        self.assertEqual((run.code, run.err), (130, ""))
        self.assertIn(f"O Ctrl+C também encerrou a conexão com o navegador ({error}).", run.out)

    def test_ctrl_c_or_end_of_input_at_the_login_prompt(self):
        for problem, code in ((KeyboardInterrupt, 130), (EOFError, 1)):
            with self.subTest(problem=problem.__name__):
                self.write_selectors(**SELECTORS)
                session = FakeSession()

                def stop():
                    raise problem

                run = self.run_main(session=session, read_line=stop)
                self.assertEqual(run.code, code)
                self.assertEqual((session.reads, session.closes, self.csv_files()), ([], 1, []))
        self.assertIn("PARADA: a entrada do terminal terminou antes da confirmação do login", run.err)

    def test_closing_the_window_ends_the_reading(self):
        self.write_selectors(**SELECTORS)
        session = FakeSession(
            {"#ativo-teste": shown("EUR/USD OTC"), "#preco-teste": shown("1.08534"), "#payout-teste": shown("92%")}
        )

        def close_window():
            session.window_closed = True

        run = self.run_main(session=session, sleep=FakeSleep({1: close_window}))
        self.assertEqual(run.code, 0)
        self.assertEqual(len(self.csv_rows()), 1)
        self.assertIn("A janela do navegador foi fechada; a leitura terminou.", run.out)
        # Closed before Enter: nothing is read.
        session = FakeSession()
        session.window_closed = True
        run = self.run_main(session=session)
        self.assertEqual((run.code, session.reads), (1, []))
        self.assertIn("a janela do navegador foi fechada antes da confirmação do login", run.err)

    def test_invalid_arguments_stop_before_anything_is_created(self):
        cases = (
            ["--url", "http://pocketoption.com/"],
            ["--url", "https://pocketoption.com.example.org/?ssid=SEGREDO"],
            ["--url", URL, "--interval", "0.5"],
            ["--url", URL, "--interval", "nan"],
            ["--url", URL, "--count", "0"],
            [],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    observe.main(argv, open_session=self.must_not_run, read_line=self.must_not_run)
                self.assertEqual(raised.exception.code, 2)
                self.assertNotIn("SEGREDO", err.getvalue())
        self.assertFalse(self.runtime.exists())

    def must_not_run(self, *args):
        raise AssertionError("main() went past the arguments")


class PlaywrightSessionTest(ObserveTestCase):
    """main() with the real PlaywrightSession on top of the fake playwright.sync_api."""

    def test_visible_browser_runtime_profile_and_reading_only(self):
        self.write_selectors(**SELECTORS)
        dom = {
            "#ativo-teste": [("EUR/USD OTC", True)],
            "#preco-teste": [("1.08534", True)],
            "#payout-teste": [("+92%", True)],
        }
        page = FakePage(dom, goto_error=f"Page.goto: net::ERR_TIMED_OUT at {SECRET_URL}\nCall log: {SECRET_URL}")
        context = FakeContext(page)
        chromium = FakeChromium(context)
        playwright = FakePlaywright(chromium)
        with mock.patch.dict(sys.modules, fake_playwright_modules(playwright)):
            run = self.run_main("--count", "1", url=SECRET_URL, real_adapter=True)
        self.assertEqual(run.code, 0, run.err)
        profile = self.runtime / "browser-profile"
        self.assertEqual(chromium.launches, [(str(profile.resolve()), False, True)])  # visible window
        self.assertTrue(profile.is_dir())
        self.assertEqual(page.calls[0], ("goto", SECRET_URL, "domcontentloaded", 60_000))
        self.assertEqual(
            page.calls[1:],
            [
                call
                for selector in SELECTORS.values()
                for call in (
                    ("locator", selector),
                    ("count", selector),
                    ("is_visible", selector),
                    ("inner_text", selector, 2_000),
                )
            ],
        )
        self.assertTrue(context.closed and playwright.stopped)
        self.assertIn(f"AVISO: a abertura da página não terminou (FakePlaywrightError: Page.goto: net::ERR_TIMED_OUT at {URL})", run.err)
        csv_text = self.csv_files()[0].read_text(encoding="utf-8")
        self.assertEqual(self.csv_rows()[0][1:3], [URL, "EUR/USD OTC"])
        self.assertNotIn("SEGREDO", run.out + run.err + csv_text)

    def test_launch_failure_stops_playwright_and_explains(self):
        self.write_selectors(**SELECTORS)
        message = (
            "BrowserType.launch_persistent_context: Executable doesn't exist at /fake/chrome\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║ Looks like Playwright was just installed or updated.     ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )
        chromium = FakeChromium(None, launch_error=message)
        playwright = FakePlaywright(chromium)
        with mock.patch.dict(sys.modules, fake_playwright_modules(playwright)):
            run = self.run_main("--count", "1", real_adapter=True)
        self.assertEqual(run.code, 1)
        self.assertIn(
            "PARADA: o Chromium não abriu (FakePlaywrightError: BrowserType.launch_persistent_context:"
            " Executable doesn't exist at /fake/chrome). Playwright: Looks like Playwright was just"
            " installed or updated. Se ele não estiver instalado, rode: python -m playwright install chromium.",
            run.err,
        )
        self.assertTrue(playwright.stopped)
        self.assertEqual(self.logins, 0)

    def test_missing_playwright_explains_the_installation(self):
        self.write_selectors(**SELECTORS)
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            run = self.run_main("--count", "1", real_adapter=True)
        self.assertEqual(run.code, 2)
        self.assertIn("CONFIGURAÇÃO: o Playwright não está instalado neste Python", run.err)
        self.assertIn("python -m pip install playwright==1.64.0", run.err)
        self.assertIn("python -m playwright install chromium", run.err)
        self.assertEqual((self.logins, self.csv_files()), (0, []))


class ReadOnlySourceTest(unittest.TestCase):
    def test_the_collector_has_no_call_that_acts_on_the_page(self):
        tree = ast.parse((ROOT / "pocketautonomo" / "observe.py").read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes & FORBIDDEN_ATTRIBUTES, set())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertEqual(names & {"getattr", "setattr", "eval", "exec", "__import__"}, set())

    def test_runs_with_python_dash_m(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")

            def run(*args):
                return subprocess.run(
                    [sys.executable, "-m", "pocketautonomo.observe", *args],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=60,
                )

            shown_help = run("--help")
            self.assertEqual(shown_help.returncode, 0, shown_help.stderr)
            self.assertIn("--url", shown_help.stdout)
            refused = run("--url", "https://example.org/?ssid=SEGREDO")
            self.assertEqual(refused.returncode, 2)
            self.assertIn("pocketoption.com", refused.stderr)
            self.assertNotIn("SEGREDO", refused.stdout + refused.stderr)
            self.assertFalse((Path(directory) / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
