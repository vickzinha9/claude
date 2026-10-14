"""Tests of the read-only collector, pocketautonomo.observe.

No test here opens a browser or reaches the network, and none needs Playwright. FakeSession
stands in for the browser page, with a fake wait, and the fake playwright.sync_api module
below stands in for Playwright. Both have only the read methods the collector may call, so
a click, a key press, a script or a cookie read would fail with AttributeError. Answers
stands in for input(). Every link, selector and value below was invented for the tests;
none of them came from the real platform.
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
NO_SELECTORS = {"asset": None, "price": None, "payout": None}

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

# What one main() call returned and printed, with its FakeSession.
Run = collections.namedtuple("Run", "code out err session")


def shown(text):
    """What a selector finds when it matches exactly one visible element with text."""
    return observe.ElementText(1, True, text)


def press_ctrl_c():
    raise KeyboardInterrupt


class FakeSession:
    """Stands in for PlaywrightSession.

    elements maps a selector to what read() answers: an ElementText, or an exception to
    raise. A list gives one answer per read, and its last answer repeats. wait() records
    each wait and runs wait_actions[n] on the n-th one.
    """

    def __init__(self, elements=None, url=URL, wait_actions=None):
        self.elements = dict(elements or {})
        self.url = url
        self.wait_actions = wait_actions or {}
        self.opened = []
        self.reads = []
        self.waits = []
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

    def wait(self, seconds):
        self.waits.append(seconds)
        action = self.wait_actions.get(len(self.waits))
        if action is not None:
            action()

    def close(self):
        self.closes += 1


class Answers:
    """Stands in for input(): gives the answers in order and records every prompt.

    An exception class in the list is raised, and a function is called for its answer.
    Running out of answers fails the test, because the program asked more than expected.
    """

    def __init__(self, answers):
        self.pending = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.pending:
            raise AssertionError(f"pergunta inesperada: {prompt!r}")
        answer = self.pending.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer
        return answer() if callable(answer) else answer


class FakeClock:
    """Stands in for the UTC clock: START, then 5 seconds later at each call."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        moment = START + timedelta(seconds=5 * self.calls)
        self.calls += 1
        return moment


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
    """dom maps a selector to the (text, visible) of each element it matches.

    goto_error and wait_error make goto and wait_for_timeout fail; closing_wait also closes
    the page in the failing wait, like a window closed during the wait.
    """

    def __init__(self, dom, goto_error=None, wait_error=None, closing_wait=False):
        self.dom = dom
        self.url = "about:blank"
        self.calls = []
        self.closed = False
        self._goto_error = goto_error
        self._wait_error = wait_error
        self._closing_wait = closing_wait

    def goto(self, url, *, wait_until, timeout):
        self.calls.append(("goto", url, wait_until, timeout))
        self.url = url  # like a page that loaded but did not finish in time
        if self._goto_error is not None:
            raise FakePlaywrightError(self._goto_error)

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return FakeLocator(self, selector)

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))
        if self._wait_error is not None:
            self.closed = self._closing_wait
            raise FakePlaywrightError(self._wait_error)


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


def reads_of(selectors):
    """The page calls of reading each selector once through PlaywrightSession."""
    return [
        call
        for selector in selectors
        for call in (
            ("locator", selector),
            ("count", selector),
            ("is_visible", selector),
            ("inner_text", selector, 2_000),
        )
    ]


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
        self.selectors_path = self.runtime / observe.SELECTORS_FILE_NAME
        patcher = mock.patch.object(observe, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile_dirs = []
        self.answers = Answers([])

    def write_selectors(self, **selectors):
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.selectors_path.write_text(json.dumps(selectors), encoding="utf-8")

    def run_main(self, *args, session=None, answers=("",), url=URL, real_adapter=False):
        """Run main() with --url url, unless url is None, and args, capturing what it prints.

        answers go, in order, to the questions of the program; the default Enter confirms
        the login. Unless real_adapter is true, main() gets session, or a new FakeSession,
        instead of opening Playwright.
        """
        session = FakeSession() if session is None else session
        self.answers = Answers(answers)

        def open_session(profile_dir):
            self.profile_dirs.append(profile_dir)
            return session

        options = {"read_line": self.answers, "now": FakeClock()}
        if not real_adapter:
            options["open_session"] = open_session
        argv = (["--url", url] if url is not None else []) + list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(argv, **options)
        return Run(code, out.getvalue(), err.getvalue(), session)

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

    def saved_selectors(self):
        """The whole JSON object of the selectors file."""
        return json.loads(self.selectors_path.read_text(encoding="utf-8"))


class SelectorsAndValuesTest(ObserveTestCase):
    def test_selectors_file_is_created_empty_and_checked(self):
        path = self.selectors_path
        self.assertEqual(observe.load_selectors(path), NO_SELECTORS)  # no file: no selector
        self.assertTrue(observe.create_selectors_file(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), observe.SELECTORS_TEMPLATE)
        self.assertEqual(observe.load_selectors(path), NO_SELECTORS)
        # A BOM, a comment key, spaces, "" and null are accepted; the file is never overwritten.
        path.write_text('\ufeff{"_nota": 1, "asset": " #a ", "price": "", "payout": null}', encoding="utf-8")
        self.assertFalse(observe.create_selectors_file(path))
        self.assertEqual(observe.load_selectors(path), {"asset": "#a", "price": None, "payout": None})
        for content in ("{", "[]", '{"payot": "#p"}', '{"price": 1}'):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(observe.ConfigError):
                observe.load_selectors(path)

    def test_saved_selectors_keep_backslashes_quotes_and_comments(self):
        selectors = {
            "asset": 'div[data-name="ativo"] > span',
            "price": r"#preco\:valor",
            "payout": None,
        }
        self.write_selectors(_nota="minha anotação", asset="#antigo")
        observe.save_selectors(self.selectors_path, selectors, False)
        self.assertEqual(observe.load_selectors(self.selectors_path), selectors)
        raw = self.selectors_path.read_text(encoding="utf-8")
        self.assertIn(r'"asset": "div[data-name=\"ativo\"] > span"', raw)  # json escaped them
        self.assertIn(r'"price": "#preco\\:valor"', raw)
        saved = self.saved_selectors()
        self.assertEqual(saved["_nota"], "minha anotação")
        self.assertEqual(saved["_leia_me"], observe.SELECTORS_TEMPLATE["_leia_me"])
        self.assertEqual(saved["payout"], "")
        self.assertEqual(sorted(path.name for path in self.runtime.iterdir()), [observe.SELECTORS_FILE_NAME])

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
            (shown("Preço"), "texto não reconhecido como cotação do ativo: 'Preço'"),
        )
        for answer, reason in failures:
            with self.subTest(reason=reason):
                session = FakeSession({"#preco": answer})
                observation = observe.observe_once(session, {"price": "#preco"}, FakeClock())
                self.assertEqual(observation.readings["price"], observe.FieldReading(None, f"indisponível: {reason}"))
                self.assertEqual(observation.readings["price"].reason, reason)
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
        self.assertEqual((session.opened, len(self.answers.prompts), session.closes), ([URL], 1, 1))
        self.assertIn("pressione Enter aqui para começar a leitura", self.answers.prompts[0])
        self.assertEqual(session.reads, ["#ativo-teste", "#preco-teste", "#payout-teste"] * 3)
        self.assertEqual(session.waits, [5.0, 5.0])  # the session's wait, between observations only
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
            " | cotação do ativo 1.08534 | payout 92% | gravada no CSV",
            run.out,
        )
        self.assertIn("Concluído: 3 observações, 3 válidas gravadas.", run.out)
        self.assertIn("Compare as observações com a tela", run.out)
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
        self.assertIn("Campos com seletor: ativo, cotação do ativo | sem seletor, ficam indisponíveis: payout", run.out)
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
        self.assertIn(
            "ativo EUR/USD OTC | cotação do ativo INDISPONÍVEL (o seletor não encontrou nenhum elemento)"
            " | payout INDISPONÍVEL (sem seletor em observe-selectors.json) | gravada no CSV",
            run.out,
        )
        self.assertIn("OBSERVAÇÃO 3 | 2026-10-14T12:00:10.000+00:00", run.out)
        self.assertIn("não gravada: nenhum campo disponível", run.out)
        self.assertEqual(session.waits, [1.0, 1.0])

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
        self.assertEqual(self.saved_selectors(), observe.SELECTORS_TEMPLATE)
        self.assertIn(f"Criei {self.selectors_path} com os campos vazios", run.out)
        self.assertIn("CONFIGURAÇÃO: nenhum seletor preenchido", run.err)
        self.assertIn("Rode com --configure", run.err)
        self.assertEqual((session.reads, len(self.answers.prompts), session.closes), ([], 1, 1))
        self.assertEqual(self.csv_files(), [])

    def test_ctrl_c_closes_the_browser_and_keeps_the_csv(self):
        self.write_selectors(**SELECTORS)
        elements = {"#ativo-teste": shown("EUR/USD OTC"), "#preco-teste": shown("1.08534"), "#payout-teste": shown("92%")}
        session = FakeSession(elements, wait_actions={1: press_ctrl_c})  # until Ctrl+C
        run = self.run_main(session=session)
        self.assertEqual((run.code, run.err), (130, ""))
        self.assertEqual(len(self.csv_rows()), 1)
        self.assertEqual(session.closes, 1)
        self.assertIn("Interrompido por Ctrl+C: 1 observação, 1 válida gravada.", run.out)
        self.assertIn("CSV preservado:", run.out)
        self.assertIn("Navegador fechado.", run.out)
        # In a terminal, Ctrl+C also stops the Playwright driver, so closing may fail.
        error = observe.BrowserError("Exception: Connection closed while reading from the driver")
        session = FakeSession(elements, wait_actions={1: press_ctrl_c})
        session.close = mock.Mock(side_effect=error)
        run = self.run_main(session=session)
        self.assertEqual((run.code, run.err), (130, ""))
        self.assertIn(f"O Ctrl+C também encerrou a conexão com o navegador ({error}).", run.out)

    def test_ctrl_c_or_end_of_input_at_the_login_prompt(self):
        for problem, code in ((KeyboardInterrupt, 130), (EOFError, 1)):
            with self.subTest(problem=problem.__name__):
                self.write_selectors(**SELECTORS)
                session = FakeSession()
                run = self.run_main(session=session, answers=[problem])
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

        session.wait_actions = {1: close_window}
        run = self.run_main(session=session)
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
            ["--configure", "--url", "https://pocketoption.com.example.org/?ssid=SEGREDO"],
            ["--url", URL, "--interval", "0.5"],
            ["--url", URL, "--interval", "nan"],
            ["--url", URL, "--count", "0"],
            [],  # without --configure, --url is required
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


class ConfigureTest(ObserveTestCase):
    """--configure: link, login, selectors with previews, corrections and the saved file."""

    def test_asks_the_link_and_each_selector_with_a_preview(self):
        session = FakeSession(
            {
                "#ativo": shown("EUR/USD OTC"),
                ".preco": observe.ElementText(2),
                "#preco": shown("1.08534"),
                "#payout": shown("+92%"),
            }
        )
        answers = [
            "http://pocketoption.com/",  # refused: not https
            SECRET_URL,  # accepted
            "",  # Enter after the login
            "#ativo", "",  # asset: preview, Enter accepts it
            ".preco", "#preco", "",  # price: ambiguous preview, another selector, accepted
            "",  # payout: Enter leaves it without a selector
            "payout", "#payout", "",  # summary: correct the payout, preview, accept
            "",  # summary: Enter saves and starts
        ]
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers, url=None)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertEqual(session.opened, [SECRET_URL])
        self.assertEqual(
            {field: self.saved_selectors()[field] for field in observe.FIELDS},
            {"asset": "#ativo", "price": "#preco", "payout": "#payout"},
        )
        self.assertEqual(
            session.reads,
            ["#ativo", ".preco", "#preco"]  # one preview per selector typed
            + ["#ativo", "#preco"]  # first summary, without payout
            + ["#payout"]  # preview of the correction
            + ["#ativo", "#preco", "#payout"] * 2,  # second summary, then the observation
        )
        self.assertIn("Link recusado (a URL precisa começar com https://;", run.out)
        self.assertIn("Prévia do ativo, lida agora: EUR/USD OTC. Confira se é o que a tela mostra.", run.out)
        self.assertIn(
            "Prévia da cotação do ativo, lida agora: INDISPONÍVEL (o seletor encontrou 2 elementos;"
            " precisa encontrar exatamente 1)",
            run.out,
        )
        self.assertIn("Payout: sem seletor, o campo fica INDISPONÍVEL.", run.out)
        self.assertIn("  payout: INDISPONÍVEL (sem seletor em observe-selectors.json)", run.out)
        self.assertIn("  payout: 92% (seletor #payout)", run.out)
        self.assertEqual(run.out.count(observe.COMPARE_WITH_SCREEN), 2)  # after each summary
        self.assertEqual(run.out.count(observe.QUOTE_NOTE), 1)  # before the quote's selector
        self.assertIn("Não é o valor da aposta: esse é o campo Amount", observe.QUOTE_NOTE)
        self.assertIn("Cotação do ativo: ainda sem seletor.", run.out)
        self.assertIn(f"Seletores gravados em {self.selectors_path}.", run.out)
        self.assertEqual(len(self.csv_rows()), 1)
        self.assertNotIn("SEGREDO", run.out + run.err)
        self.assertEqual(self.answers.prompts[1], "Link: ")

    def test_reuses_saved_selectors_and_writes_the_json_itself(self):
        asset, price = 'div[data-name="ativo"] > span', r"#preco\:valor"
        payout = r'#pay\:out > span[title="Payout \"atual\""]'
        self.write_selectors(_nota="minha anotação", asset=asset, price=price, payout="")
        session = FakeSession({asset: shown("EUR/USD OTC"), price: shown("1.08534"), payout: shown("92%")})
        answers = [
            "",  # Enter after the login
            "", "",  # asset: Enter keeps the saved selector, Enter accepts its preview
            "", "",  # price: the same
            payout, "",  # payout: pasted as copied, without escaping, and accepted
            "",  # summary: Enter saves and starts
        ]
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertIn(f"Ativo: seletor atual {asset}", run.out)
        self.assertEqual(observe.load_selectors(self.selectors_path), {"asset": asset, "price": price, "payout": payout})
        saved = self.saved_selectors()
        self.assertEqual(saved["_nota"], "minha anotação")
        self.assertEqual(saved["_leia_me"], observe.SELECTORS_TEMPLATE["_leia_me"])
        self.assertIn(r'"payout": "#pay\\:out > span[title=\"Payout \\\"atual\\\"\"]"', self.selectors_path.read_text(encoding="utf-8"))
        self.assertEqual(self.csv_rows()[0][2:], ["EUR/USD OTC", f"ok: {asset}", "1.08534", f"ok: {price}", "92", f"ok: {payout}"])

    def test_the_quote_keeps_the_price_key_and_answers_to_its_new_name(self):
        self.write_selectors(asset="#ativo", price="#preco-antigo", payout="#payout")
        session = FakeSession(
            {
                "#ativo": shown("EUR/USD OTC"),
                "#preco-antigo": shown("1.0"),
                "#cotacao": shown("1.08534"),
                "#payout": shown("92%"),
            }
        )
        answers = [
            "",  # Enter after the login
            "", "",  # asset kept
            "", "",  # quote kept for now
            "", "",  # payout kept
            "Cotação",  # summary: correct the quote by its new name
            "#cotacao", "",
            "",  # save
        ]
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        saved = self.saved_selectors()
        self.assertEqual({field: saved[field] for field in observe.FIELDS}, {"asset": "#ativo", "price": "#cotacao", "payout": "#payout"})
        self.assertEqual(self.csv_rows()[0][4:6], ["1.08534", "ok: #cotacao"])  # the CSV keeps the price columns
        for answer in ("cotação", "cotacao", "cotação do ativo", "preço", "preco", "price"):
            self.assertEqual(observe.FIELD_ANSWERS[answer], "price")

    def test_keeps_an_invalid_file_as_a_copy(self):
        self.runtime.mkdir(parents=True)
        self.selectors_path.write_text('{"asset": "#a\\b"', encoding="utf-8")  # broken by hand
        original = self.selectors_path.read_bytes()
        session = FakeSession({"#ativo": shown("EUR/USD OTC")})
        answers = ["", "#ativo", "", "", "", ""]  # login, asset and its preview, price, payout, save
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("não é um JSON válido", run.err)
        [copy] = self.runtime.glob("observe-selectors.invalido-*.json")
        self.assertEqual(copy.read_bytes(), original)
        self.assertIn(f"O arquivo inválido foi guardado como {copy}.", run.out)
        self.assertEqual(observe.load_selectors(self.selectors_path), {"asset": "#ativo", "price": None, "payout": None})

    def test_writes_nothing_when_cancelled_or_left_empty(self):
        def closing_the_window_then(answer):
            def act():
                session.window_closed = True
                return answer

            return act

        cases = (
            # (answers, exit code, message on stderr)
            (["", EOFError], 1, "PARADA: a entrada do terminal terminou durante a configuração dos seletores; nada foi gravado"),
            (["", "", "", KeyboardInterrupt], 130, ""),
            (["", "-", "", "", ""], 2, "CONFIGURAÇÃO: nenhum seletor foi escolhido"),
            (["", closing_the_window_then("")], 1, "PARADA: a janela do navegador foi fechada durante a configuração; nada foi gravado"),
        )
        for answers, code, message in cases:
            with self.subTest(code=code, message=message):
                self.write_selectors(asset="#ativo")
                original = self.selectors_path.read_bytes()
                session = FakeSession({"#ativo": shown("EUR/USD OTC")})
                run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
                self.assertEqual(run.code, code, run.err)
                self.assertIn(message, run.err)
                self.assertEqual(self.selectors_path.read_bytes(), original)
                self.assertEqual((session.closes, self.csv_files()), (1, []))
                self.assertEqual(sorted(path.name for path in self.runtime.iterdir()), [observe.SELECTORS_FILE_NAME])


class PlaywrightSessionTest(ObserveTestCase):
    """main() with the real PlaywrightSession on top of the fake playwright.sync_api."""

    def run_with_fake_playwright(self, page, *args, url=URL):
        context = FakeContext(page)
        chromium = FakeChromium(context)
        playwright = FakePlaywright(chromium)
        with mock.patch.dict(sys.modules, fake_playwright_modules(playwright)):
            run = self.run_main(*args, url=url, real_adapter=True)
        return run, context, chromium, playwright

    def dom(self):
        return {
            "#ativo-teste": [("EUR/USD OTC", True)],
            "#preco-teste": [("1.08534", True)],
            "#payout-teste": [("+92%", True)],
        }

    def test_visible_browser_runtime_profile_reading_and_playwright_wait(self):
        self.write_selectors(**SELECTORS)
        page = FakePage(self.dom(), goto_error=f"Page.goto: net::ERR_TIMED_OUT at {SECRET_URL}\nCall log: {SECRET_URL}")
        run, context, chromium, playwright = self.run_with_fake_playwright(page, "--count", "2", url=SECRET_URL)
        self.assertEqual(run.code, 0, run.err)
        profile = self.runtime / "browser-profile"
        self.assertEqual(chromium.launches, [(str(profile.resolve()), False, True)])  # visible window
        self.assertTrue(profile.is_dir())
        self.assertEqual(page.calls[0], ("goto", SECRET_URL, "domcontentloaded", 60_000))
        reads = reads_of(SELECTORS.values())
        self.assertEqual(page.calls[1:], reads + [("wait_for_timeout", 5_000)] + reads)
        self.assertTrue(context.closed and playwright.stopped)
        self.assertIn(
            "AVISO: a abertura da página não terminou (FakePlaywrightError: Page.goto:"
            f" net::ERR_TIMED_OUT at {URL})",
            run.err,
        )
        csv_text = self.csv_files()[0].read_text(encoding="utf-8")
        self.assertEqual([row[1:3] for row in self.csv_rows()], [[URL, "EUR/USD OTC"]] * 2)
        self.assertNotIn("SEGREDO", run.out + run.err + csv_text)

    def test_a_failed_wait_ends_the_reading(self):
        error = "Page.wait_for_timeout: Target page, context or browser has been closed"
        for closing, code in ((True, 0), (False, 1)):
            with self.subTest(window_closed=closing):
                self.write_selectors(**SELECTORS)
                page = FakePage(self.dom(), wait_error=error, closing_wait=closing)
                run, context, _, playwright = self.run_with_fake_playwright(page, "--count", "3")
                self.assertEqual(run.code, code, run.err)
                self.assertEqual(page.calls.count(("wait_for_timeout", 5_000)), 1)
                self.assertTrue(context.closed and playwright.stopped)
                if closing:  # the window was closed: the reading just ends
                    self.assertIn("A janela do navegador foi fechada; a leitura terminou.", run.out)
                else:
                    self.assertIn(f"PARADA: a espera entre as leituras falhou (FakePlaywrightError: {error})", run.err)
                self.assertEqual(len(self.csv_rows()), 1)  # the observation before the wait
                for path in self.csv_files():
                    path.unlink()

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
        self.assertEqual(self.answers.prompts, [])

    def test_missing_playwright_explains_the_installation(self):
        self.write_selectors(**SELECTORS)
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            run = self.run_main("--count", "1", real_adapter=True)
        self.assertEqual(run.code, 2)
        self.assertIn("CONFIGURAÇÃO: o Playwright não está instalado neste Python", run.err)
        self.assertIn("python -m pip install playwright==1.64.0", run.err)
        self.assertIn("python -m playwright install chromium", run.err)
        self.assertEqual((self.answers.prompts, self.csv_files()), ([], []))


class ReadOnlySourceTest(unittest.TestCase):
    def test_the_collector_has_no_call_that_acts_on_the_page(self):
        tree = ast.parse((ROOT / "pocketautonomo" / "observe.py").read_text(encoding="utf-8"))
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes & FORBIDDEN_ATTRIBUTES, set())
        self.assertNotIn("sleep", attributes)  # the waits go through Playwright, never time.sleep
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
            self.assertIn("--configure", shown_help.stdout)
            refused = run("--url", "https://example.org/?ssid=SEGREDO")
            self.assertEqual(refused.returncode, 2)
            self.assertIn("pocketoption.com", refused.stderr)
            self.assertNotIn("SEGREDO", refused.stdout + refused.stderr)
            self.assertFalse((Path(directory) / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
