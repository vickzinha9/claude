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
import errno
import io
import json
import mmap
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # chart_images, next to the tests

import chart_images  # noqa: E402
from pocketautonomo import observe, quote_ocr  # noqa: E402

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
    "route_from_har", "route_web_socket", "select_option", "select_text",
    "set_checked", "set_extra_http_headers", "set_input_files", "storage_state", "tap",
    "touchscreen", "type", "uncheck",
}
# screenshot is allowed only with a clip, to memory: see test_the_collector_has_no_call_that_acts_on_the_page.

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

    def __init__(self, elements=None, url=URL, wait_actions=None, captures=None, texts=None):
        self.elements = dict(elements or {})
        self.url = url
        self.wait_actions = wait_actions or {}
        self.captures = dict(captures or {})  # chart selector -> RgbImage, Capture or exception
        self.texts = list(texts or [])  # what find_quote_texts finds
        self.opened = []
        self.reads = []
        self.capture_calls = []
        self.strips = []
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

    def capture(self, selector, timeout_ms, strip=None):
        """A Capture at 1 device pixel per CSS pixel, like read() for the chart.

        With strip, only the image's rightmost strip columns are returned, as
        PlaywrightSession clips the screenshot; strips records each width asked for.
        """
        self.capture_calls.append(selector)
        self.strips.append(strip)
        answer = self.captures[selector]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, observe.Capture):
            return answer
        if strip is not None and strip < answer.width:
            part = chart_images.right_strip(answer, strip)
            return observe.Capture(1, True, part, part.width, strip_css=strip)
        return observe.Capture(1, True, answer, answer.width)

    def find_quote_texts(self, limit):
        return self.texts[:limit]

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


class FakeMonotonic:
    """Stands in for time.perf_counter: 2 ms later at each call."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 100 + 0.002 * self.calls


def context(**options):
    """An observe.Context with the fake clocks, for calls outside main()."""
    options.setdefault("now", FakeClock())
    options.setdefault("monotonic", FakeMonotonic())
    return observe.Context(**options)


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

    def run_main(self, *args, session=None, answers=("",), url=URL, real_adapter=False, engine=None):
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

        self.shown_images = []
        self.engine = chart_images.FakeEngineHolder(engine)
        options = {
            "read_line": self.answers,
            "now": FakeClock(),
            "monotonic": FakeMonotonic(),
            "engine": self.engine,
            "show_image": self.shown_images.append,
        }
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
            observe.parse_price: [("1.08534", "1.08534"), ("62,345.12", "62,345.12"), (" 158.123 ", "158.123")],
            observe.parse_payout: [("+92%", "92"), ("92 %", "92"), ("85.5%", "85.5"), ("90\u00a0%", "90")],
        }
        refused = {
            observe.parse_asset: ["", "  ", "1.08534", "EUR/USD\n92%", "A" * 61, "=HYPERLINK(1)", "-teste"],
            observe.parse_price: ["", "Preço", "1.0853 ▲", "-1.2", "1..2", "1.2%", "100", "10", "$10.00"],
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
        observation = observe.observe_once(session, selectors, context())
        self.assertEqual(session.reads, ["#ativo", "#preco"])  # no selector, no reading
        self.assertEqual((observation.captured_at, observation.page), (START, URL))
        self.assertTrue(observation.valid)
        self.assertEqual(
            {field: (reading.value, reading.status, reading.source) for field, reading in observation.readings.items()},
            {
                "asset": ("EUR/USD OTC", "ok: #ativo", "texto"),
                "price": ("1.08534", "ok: #preco", "texto"),
                "payout": (None, "indisponível: sem seletor em observe-selectors.json", None),
            },
        )
        price = observation.readings["price"]
        self.assertEqual((price.captured_at, price.processing_ms), (START + timedelta(seconds=10), 2.0))
        self.assertEqual((observation.completeness, observation.duration_ms), ("parcial", 10.0))
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
                observation = observe.observe_once(session, {"price": "#preco"}, context())
                self.assertEqual(observation.readings["price"].status, f"indisponível: {reason}")
                self.assertEqual(observation.readings["price"].reason, reason)
                self.assertFalse(observation.valid)

    def test_a_page_outside_pocketoption_is_not_read(self):
        session = FakeSession({"#ativo": shown("EUR/USD OTC")}, url="https://example.org/entrar?ssid=SEGREDO")
        observation = observe.observe_once(session, {"asset": "#ativo"}, context())
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
        # The quote is read after the asset: 5 s later on the fake clock, in 2 ms on the fake
        # timer; the whole observation took 7 timer steps, 14 ms.
        self.assertEqual(
            self.csv_rows(),
            [
                [
                    f"2026-10-14T12:00:{start:02d}.000+00:00", URL, "EUR/USD OTC", ok[0], price, ok[1], "92", ok[2],
                    "completa", "texto", f"2026-10-14T12:00:{start + 10:02d}.000+00:00", "2.0", "14.0", "", "", "",
                ]
                for start, price in ((0, "1.08534"), (20, "1.08540"), (40, "1.08529"))
            ],
        )
        self.assertRegex(self.csv_files()[0].name, r"^observations-\d{8}-\d{6}-[0-9a-f]{8}\.csv$")
        self.assertIn(
            f"OBSERVAÇÃO 1 COMPLETA | relógio local 2026-10-14T12:00:00.000+00:00 | {URL} | ativo EUR/USD OTC"
            " | cotação do ativo 1.08534 (texto, 2 ms) | payout 92% | gravada no CSV",
            run.out,
        )
        self.assertIn("Concluído: 3 observações, 3 válidas gravadas (3 completas e 0 parciais).", run.out)
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
        self.assertIn("Campos com fonte: ativo, cotação do ativo | sem fonte, ficam indisponíveis: payout", run.out)
        rows = self.csv_rows()
        self.assertEqual(len(rows), 2)  # the third observation had no available field
        self.assertEqual(
            rows[1][2:],
            [
                "EUR/USD OTC", "ok: #ativo-teste",
                "", "indisponível: o seletor não encontrou nenhum elemento",
                "", "indisponível: sem seletor em observe-selectors.json",
                "parcial", "texto", "2026-10-14T12:00:25.000+00:00", "2.0", "10.0", "", "", "",
            ],
        )
        self.assertIn(
            "ativo EUR/USD OTC | cotação do ativo INDISPONÍVEL (o seletor não encontrou nenhum elemento)"
            " | payout INDISPONÍVEL (sem seletor em observe-selectors.json) | gravada no CSV",
            run.out,
        )
        # The clock is read at the start and before each field with a source: 3 times here.
        self.assertIn("OBSERVAÇÃO 2 PARCIAL | relógio local 2026-10-14T12:00:15.000+00:00", run.out)
        self.assertIn("OBSERVAÇÃO 3 SEM CAMPOS | relógio local 2026-10-14T12:00:30.000+00:00", run.out)
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
        self.assertIn("Interrompido por Ctrl+C: 1 observação, 1 válida gravada (1 completa e 0 parciais).", run.out)
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
        self.assertIn("Cotação do ativo: ainda sem fonte.", run.out)
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
        self.assertEqual(self.csv_rows()[0][2:9], ["EUR/USD OTC", f"ok: {asset}", "1.08534", f"ok: {price}", "92", f"ok: {payout}", "completa"])

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


# A model saved by the earlier version, with tolerance 20; the file keeps working.
BLUE_MARKER = {"colors": ["#2962ff"], "width": 68.0, "height": 18.0, "right_gap": 1.0, "tolerance": 20}
VISUAL = {"chart": "#grafico", "marker": BLUE_MARKER}
# What a new calibration of the same marker saves.
NEW_VISUAL = {"chart": "#grafico", "marker": {**BLUE_MARKER, "tolerance": 40}}


class VisualQuoteTest(ObserveTestCase):
    """The quote read from a synthetic chart, with FakeEngine in place of the OCR."""

    def test_the_file_keeps_the_visual_source_apart_from_the_price_key(self):
        self.write_selectors(asset="#ativo", price="", payout="#payout", price_ocr=VISUAL)
        selectors = observe.load_selectors(self.selectors_path)
        source = selectors["price"]
        self.assertIsInstance(source, observe.QuoteOcrSource)
        self.assertEqual((source.chart, source.marker.to_json()), ("#grafico", BLUE_MARKER))
        observe.save_selectors(self.selectors_path, selectors, False)
        saved = self.saved_selectors()
        self.assertEqual((saved["price"], saved["price_ocr"]), ("", VISUAL))
        selectors["price"] = "#cotacao"  # back to text: price_ocr leaves the file
        observe.save_selectors(self.selectors_path, selectors, False)
        self.assertNotIn("price_ocr", self.saved_selectors())
        broken = (
            {"price": "#cotacao", "price_ocr": VISUAL},  # two sources for the quote
            {"price_ocr": {"chart": "", "marker": BLUE_MARKER}},
            {"price_ocr": {"chart": "#grafico", "marker": {**BLUE_MARKER, "colors": ["azul"]}}},
            {"price_ocr": {"chart": "#grafico", "marker": BLUE_MARKER, "y": 150}},
        )
        for data in broken:
            with self.subTest(data=data):
                self.write_selectors(**data)
                with self.assertRaises(observe.ConfigError):
                    observe.load_selectors(self.selectors_path)

    def test_observations_record_the_source_the_moment_and_the_duration(self):
        self.write_selectors(asset="#ativo", price="", payout="#payout", price_ocr=VISUAL)
        session = FakeSession(
            {"#ativo": shown("GBP/USD OTC"), "#payout": shown("92%")},
            captures={"#grafico": [chart_images.chart(marker_y=150), chart_images.chart(marker_y=None)]},
        )
        run = self.run_main("--count", "2", session=session)
        self.assertEqual((run.code, run.err), (0, ""))
        rows = self.csv_rows()
        self.assertEqual(
            rows[0][2:],
            [
                "GBP/USD OTC", "ok: #ativo", "1.26543", "ok: ocr #grafico", "92", "ok: #payout",
                # The quote took 14 ms on the fake timer: capture, detection and OCR, 2 ms each.
                "completa", "ocr", "2026-10-14T12:00:10.000+00:00", "14.0", "26.0", "2.0", "2.0", "2.0",
            ],
        )
        # Without the marker the quote is unavailable: empty, never zero, nor the earlier value.
        self.assertEqual(rows[1][4:9], ["", rows[1][5], "92", "ok: #payout", "parcial"])
        self.assertIn("indisponível: o marcador da cotação não apareceu", rows[1][5])
        self.assertEqual(rows[1][9], "ocr")
        self.assertEqual(rows[1][13:], ["2.0", "2.0", ""])  # without a marker, no OCR ran
        self.assertIn(
            "cotação do ativo 1.26543 (leitura visual, 14 ms: captura 2 ms, detecção 2 ms, OCR 2 ms)", run.out
        )
        self.assertIn("(leitura visual, 10 ms: captura 2 ms, detecção 2 ms) | payout 92%", run.out)
        self.assertIn("OBSERVAÇÃO 2 PARCIAL", run.out)
        self.assertIn("Campos com fonte: ativo, cotação do ativo (leitura visual), payout", run.out)
        self.assertEqual(session.capture_calls, ["#grafico", "#grafico"])
        # Once before the first observation, then only for a marker found: 1 + 1.
        self.assertEqual(self.engine.gets, 2)

    def test_capture_problems_are_reasons(self):
        cases = (
            (observe.Capture(0), "o seletor do gráfico (#grafico) não encontrou nenhum elemento"),
            (observe.Capture(2), "encontrou 2 elementos; precisa encontrar exatamente 1"),
            (observe.Capture(1), "o elemento do gráfico (#grafico) não está visível"),
            (observe.Capture(1, True, None, 900.0, clipped=True), "não cabe inteiro na área visível da janela"),
            (observe.ReadError("Error: Timeout 5000ms exceeded"), "a captura do gráfico falhou: Error: Timeout"),
        )
        source = observe.QuoteOcrSource("#grafico", observe.quote_reader.MarkerModel.from_json(BLUE_MARKER))
        for answer, reason in cases:
            with self.subTest(reason=reason):
                session = FakeSession(captures={"#grafico": answer})
                ctx = context(engine=chart_images.FakeEngineHolder())
                reading = observe.read_field(session, "price", source, ctx)
                self.assertIsNone(reading.value)
                self.assertIn(reason, reading.status)
                self.assertEqual(reading.source, "ocr")

    def test_a_missing_ocr_stops_before_the_first_observation(self):
        self.write_selectors(asset="#ativo", price="", payout="#payout", price_ocr=VISUAL)

        def missing():
            raise observe.OcrUnavailable("o OCR local não está instalado neste Python")

        session = FakeSession({"#ativo": shown("GBP/USD OTC"), "#payout": shown("92%")})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = observe.main(
                ["--url", URL, "--count", "1"],
                open_session=lambda profile: session,
                read_line=Answers([""]),
                now=FakeClock(),
                engine=observe.LazyEngine(missing),
            )
        self.assertEqual(code, 2)
        self.assertIn("CONFIGURAÇÃO: o OCR local não está instalado neste Python", err.getvalue())
        self.assertEqual((session.reads, session.capture_calls, self.csv_files()), ([], [], []))


class ConfigureQuoteTest(ObserveTestCase):
    def test_ocr_lists_candidates_previews_and_saves_the_marker(self):
        image = chart_images.chart(marker_y=150, cursor_y=60)
        session = FakeSession(
            {"#ativo": shown("GBP/USD OTC"), "#payout": shown("92%")},
            captures={"#grafico": image},
        )
        answers = [
            "",  # Enter after the login
            "#ativo", "",
            "ocr",  # the quote, read from the chart
            "#grafico",  # the chart element
            "2",  # candidate 2 is the current-quote marker; 1 is the cursor's label
            "",  # the previews matched the screen: accept
            "#payout", "",
            "",  # summary: save
        ]
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        saved = self.saved_selectors()
        self.assertEqual(saved["price"], "")
        self.assertEqual(saved["price_ocr"], NEW_VISUAL)  # colors, size and right gap; no height
        candidates, previews = self.shown_images
        self.assertRegex(candidates.name, r"^cotacao-candidatos-\d{8}-\d{6}-[0-9a-f]{8}\.png$")
        self.assertRegex(previews.name, r"^cotacao-previa-\d{8}-\d{6}-[0-9a-f]{8}\.png$")
        for path in (candidates, previews):
            self.assertEqual(path.parent, self.runtime)
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("  1: caixa #4a4f5e de 68x18 px, a 1 px da borda direita; texto lido: 1.26543", run.out)
        self.assertIn("  2: caixa #2962ff de 68x18 px, a 1 px da borda direita; texto lido: 1.26543", run.out)
        self.assertEqual(run.out.count("Prévia 1, relógio local"), 1)
        self.assertIn("Prévia 3, relógio local", run.out)
        self.assertEqual(session.waits, [1.0, 1.0])  # between the previews, with the session's wait
        self.assertIn(observe.COMPARE_QUOTE, run.out)
        self.assertIn("cotação do ativo: 1.26543 (leitura visual (OCR) do marcador #2962ff, no elemento #grafico)", run.out)
        self.assertEqual(self.csv_rows()[0][4:10], ["1.26543", "ok: ocr #grafico", "92", "ok: #payout", "completa", "ocr"])
        # The candidates come from the whole chart; every reading after them, previews and
        # observation, from the right strip that the marker's search needs.
        strip = observe.quote_reader.strip_width(observe.quote_reader.MarkerModel.from_json(NEW_VISUAL["marker"]))
        self.assertEqual(session.strips[0], None)
        self.assertEqual(set(session.strips[1:]), {strip})
        self.assertLess(strip, image.width)
        # Besides the CSV of the observations and its timings, only the two preview images
        # were added to runtime/.
        [main_csv] = self.csv_files()
        timings = self.runtime / ("tempos-" + main_csv.name)
        self.assertEqual(
            sorted(path.name for path in self.runtime.iterdir() if path not in (main_csv, timings)),
            sorted([candidates.name, previews.name, "observe-selectors.json"]),
        )
        with open(timings, newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), observe.quote_timings.TIMING_COLUMNS)
        [row] = [dict(zip(header, row)) for row in rows]  # the observation; the previews are not written
        self.assertEqual(
            (row["moment"], row["result"], row["quote"], row["source"], row["asset"], row["strip_css"]),
            ("observação", "ok", "1.26543", "ocr", "GBP/USD OTC", f"{strip:.1f}"),
        )
        self.assertEqual(row["image_px"], f"{int(strip)}x{image.height}")
        self.assertIn("Tempos da leitura da cotação, mediana/máximo em ms, por momento da leitura:", run.out)
        self.assertIn("  observação (1 leitura, 1 com cotação): total ", run.out)
        self.assertIn(f"Tempos de cada leitura: {timings}", run.out)

    def test_search_lists_text_candidates_and_uses_the_one_chosen(self):
        session = FakeSession(
            {"#ativo": shown("GBP/USD OTC"), "#fechamento": shown("1.26000"), "#payout": shown("92%")},
            texts=[observe.TextMatch("1.26000", "#fechamento"), observe.TextMatch("62,345.12", None)],
        )
        answers = [
            "",
            "#ativo", "",
            "buscar",
            "2",  # no unique selector
            "buscar",
            "1", "",  # the first result, previewed and accepted
            "#payout", "",
            "",
        ]
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertIn("  1: 1.26000 | seletor #fechamento", run.out)
        self.assertIn("  2: 62,345.12 | sem seletor único; use o DevTools nele", run.out)
        self.assertIn("nenhum é escolhido sozinho", run.out)
        self.assertIn("Esse número não é de um resultado com seletor único.", run.out)
        self.assertIn("Prévia da cotação do ativo, lida agora: 1.26000. Confira se é o que a tela mostra.", run.out)
        self.assertEqual(self.saved_selectors()["price"], "#fechamento")
        self.assertNotIn("price_ocr", self.saved_selectors())
        # Without results, the search says so and points to ocr. This run reuses the file
        # saved above: Enter keeps the asset, then buscar, - for no quote, - for no payout.
        session = FakeSession({"#ativo": shown("GBP/USD OTC")})
        run = self.run_main("--configure", "--count", "1", session=session, answers=["", "", "", "buscar", "-", "-", ""])
        self.assertIn("Nenhum elemento de texto visível da página tem um número com casas decimais.", run.out)
        self.assertIn("Cotação do ativo: sem fonte, o campo fica INDISPONÍVEL.", run.out)

    def test_a_changed_marker_color_can_be_added(self):
        self.write_selectors(asset="#ativo", price="", payout="#payout", price_ocr=VISUAL)
        green = chart_images.chart(marker_y=150, marker_color=chart_images.GREEN)
        session = FakeSession({"#ativo": shown("GBP/USD OTC"), "#payout": shown("92%")}, captures={"#grafico": green})
        answers = [
            "",
            "", "",  # asset kept
            "",  # the quote's current source kept: previews follow
            "n",  # the previews were unavailable: choose the marker again
            "",  # the same chart element
            "1",  # the green marker
            "+",  # add its color to the saved one
            "",  # previews accepted
            "", "",  # payout kept
            "",
        ]
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertIn("Prévia 1, relógio local 2026-10-14T12:00:", run.out)
        self.assertIn("INDISPONÍVEL (o marcador da cotação não apareceu", run.out)
        self.assertEqual(self.saved_selectors()["price_ocr"]["marker"]["colors"], ["#2962ff", "#26a69a"])
        self.assertEqual(self.csv_rows()[0][4], "1.26543")

    def configure_visual(self, captures, answers, engine=None):
        """--configure with asset and payout kept and the quote read from the chart."""
        self.write_selectors(asset="#ativo", price="", payout="#payout")
        session = FakeSession({"#ativo": shown("GBP/USD OTC"), "#payout": shown("92%")}, captures={"#grafico": captures})
        before = ["", "", "", "ocr", "#grafico"]  # login, asset kept and accepted, ocr, chart
        after = ["", "", ""]  # payout kept and accepted, summary saved
        run = self.run_main("--configure", "--count", "1", session=session, answers=before + answers + after, engine=engine)
        return run, session

    def test_choosing_the_marker_again_writes_new_images_and_keeps_the_old_ones(self):
        # The validation: n after three unavailable previews stopped the run with
        # "PARADA: [Errno 22] Invalid argument: 'runtime\\cotacao-candidatos.png'".
        self.runtime.mkdir(parents=True)
        old = {name: f"imagem antiga {name}".encode() for name in ("cotacao-candidatos.png", "cotacao-previa.png")}
        for name, data in old.items():
            (self.runtime / name).write_bytes(data)
        marker, gone = chart_images.chart(marker_y=150), chart_images.chart(marker_y=None)
        captures = [marker, gone, gone, gone, marker]  # candidates, 3 previews, candidates, ...
        run, session = self.configure_visual(captures, ["1", "n", "1", ""])
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertEqual(len(self.shown_images), 4)  # candidates, preview, candidates, preview
        self.assertEqual(len(set(self.shown_images)), 4)
        for path in self.shown_images:
            self.assertEqual(path.parent, self.runtime)
            self.assertNotIn(path.name, old)
        for name, data in old.items():
            self.assertEqual((self.runtime / name).read_bytes(), data)
        self.assertIn("3 de 3 prévias ficaram INDISPONÍVEIS", run.out)
        self.assertEqual(self.saved_selectors()["price_ocr"], NEW_VISUAL)

    def test_a_failed_image_write_can_be_retried_or_skipped(self):
        marker = chart_images.chart(marker_y=150)
        real_write = observe.write_preview
        cases = (
            # (answer after the failure, images shown, exit code, quote saved)
            ("", 2, 0, True),  # Enter tries again
            ("c", 1, 0, True),  # go on without the candidates' image
            ("-", 0, 0, False),  # give up the visual reading; then - for no quote
        )
        for answer, images, code, saved in cases:
            with self.subTest(answer=answer):
                failures = [OSError(22, "Invalid argument", str(self.runtime / "cotacao-candidatos-x.png"))]

                def flaky(*args, **kwargs):
                    if failures:
                        raise failures.pop()
                    return real_write(*args, **kwargs)

                choices = [answer, "1", ""] if answer != "-" else ["-", "-"]
                with mock.patch.object(observe, "write_preview", flaky):
                    run, _ = self.configure_visual([marker], choices)
                self.assertEqual(run.code, code, run.err)
                self.assertEqual(self.answers.pending, [])
                self.assertIn(
                    f"AVISO: não foi possível gravar a imagem de conferência em {self.runtime}"
                    " (OSError: [Errno 22] Invalid argument", run.err,
                )
                self.assertNotIn("PARADA", run.err)
                self.assertEqual(len(self.shown_images), images)
                self.assertEqual("price_ocr" in self.saved_selectors(), saved)

    def test_a_candidate_without_a_valid_reading_is_refused(self):
        image = chart_images.chart(marker_y=150, cursor_y=60)
        engine = chart_images.FakeEngine(answers=[("abc", 0.99), ("abc", 0.99)])  # candidate 1
        run, _ = self.configure_visual([image], ["1", "2", ""], engine=engine)
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertIn("texto lido: nenhuma cotação legível (o OCR leu 'abc' no marcador, que não é uma cotação); não pode ser escolhido", run.out)
        self.assertIn("O candidato 1 não tem uma cotação legível", run.out)
        self.assertEqual(self.saved_selectors()["price_ocr"], NEW_VISUAL)  # candidate 2, the marker

    def test_previews_with_an_unavailable_reading_cannot_be_accepted(self):
        marker, gone = chart_images.chart(marker_y=150), chart_images.chart(marker_y=None)
        captures = [marker, marker, gone, marker, marker]  # candidates, previews: ok, missing, ok
        run, _ = self.configure_visual(captures, ["1", "", ""])  # Enter repeats; then Enter accepts
        self.assertEqual((run.code, run.err), (0, ""))
        self.assertEqual(self.answers.pending, [])
        self.assertIn("1 de 3 prévias ficaram INDISPONÍVEIS: a leitura visual não pode ser aceita assim", run.out)
        self.assertEqual(run.out.count("Prévia 1, relógio local"), 2)  # the first Enter repeated them
        self.assertEqual(self.saved_selectors()["price_ocr"], NEW_VISUAL)

    def test_previews_show_where_the_time_goes(self):
        image = chart_images.chart(marker_y=150)
        timed = observe.Capture(1, True, image, image.width, element_ms=3.0, screenshot_ms=2900.0, decode_ms=12.0)
        run, _ = self.configure_visual([timed], ["1", ""])
        self.assertEqual((run.code, run.err), (0, ""))
        # The fake timer adds 2 ms per reading of the clock; the steps of the capture come
        # from the session, as PlaywrightSession measures them.
        self.assertIn(
            "Prévia 1, relógio local 2026-10-14T12:00:10.000+00:00: 1.26543 (em 14 ms: captura 2 ms"
            " (elemento 3, tela 2900, decodificação 12), detecção 2 ms, OCR 2 ms)",
            run.out,
        )

    def test_a_failed_save_of_the_selectors_can_be_retried(self):
        real_save = observe.save_selectors
        failures = [OSError(28, "No space left on device")]

        def flaky(*args, **kwargs):
            if failures:
                raise failures.pop()
            return real_save(*args, **kwargs)

        self.write_selectors(asset="#ativo")
        session = FakeSession({"#ativo": shown("GBP/USD OTC")})
        answers = ["", "", "", "-", "-", "", ""]  # login, asset, no quote, no payout, save, retry
        with mock.patch.object(observe, "save_selectors", flaky):
            run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual((run.code, self.answers.pending), (0, []))
        self.assertIn(f"AVISO: não foi possível gravar {self.selectors_path} (OSError: [Errno 28] No space left on device).", run.err)
        self.assertIn(f"Seletores gravados em {self.selectors_path}.", run.out)

    def test_giving_up_the_visual_reading_keeps_the_file(self):
        self.write_selectors(asset="#ativo", price="", payout="#payout", price_ocr=VISUAL)
        original = self.selectors_path.read_bytes()
        session = FakeSession({"#ativo": shown("GBP/USD OTC")}, captures={"#grafico": chart_images.chart(marker_y=None)})
        answers = ["", "", "", "ocr", "", "-", EOFError]  # no candidate: give up, then stop
        run = self.run_main("--configure", "--count", "1", session=session, answers=answers)
        self.assertEqual(run.code, 1)
        self.assertIn("Nenhuma caixa com texto apareceu na metade direita do gráfico.", run.out)
        self.assertIn("Leitura visual não configurada.", run.out)
        self.assertEqual(self.selectors_path.read_bytes(), original)


@unittest.skipUnless(os.name == "nt", "o mapeamento de arquivo do Windows só existe no Windows")
class OpenImageOnWindowsTest(ObserveTestCase):
    """The cause of the [Errno 22] of the validation, reproduced on Windows.

    A viewer maps the image it shows into memory, and Windows refuses to truncate a file
    with a mapped view (ERROR_USER_MAPPED_FILE), which Python reports as [Errno 22] Invalid
    argument. mmap stands in for the viewer here.
    """

    def test_an_image_open_in_a_viewer_is_never_overwritten(self):
        self.runtime.mkdir(parents=True)
        shown_before = self.runtime / "cotacao-candidatos.png"
        shown_before.write_bytes(b"imagem aberta num visualizador")
        image = chart_images.chart(marker_y=150)
        with open(shown_before, "rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ):
            with self.assertRaises(OSError) as raised:
                shown_before.write_bytes(b"nova")  # what the earlier version did
            self.assertEqual(raised.exception.errno, errno.EINVAL)
            written = observe.write_preview(context(), observe.CANDIDATES_PREFIX, image, [])
        self.assertNotEqual(written, shown_before)
        self.assertEqual(written.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(shown_before.read_bytes(), b"imagem aberta num visualizador")


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


class FakePng(bytes):
    """What the fake screenshot returns: PNG-like bytes that carry the size of the image."""

    def __new__(cls, width, height):
        png = super().__new__(cls, b"\x89PNG\r\n\x1a\n" + b"\0" * 92)
        png.width, png.height = width, height
        return png


def fake_decode(png):
    return chart_images.RgbImage(png.width, png.height, bytes(png.width * png.height * 3))


class FakeChartPage:
    """A page with one chart element at box, drawn at scale device pixels per CSS pixel.

    window, a (width, height) pair in CSS pixels, trims the screenshot as a window does.
    """

    def __init__(self, box, scale=1.25, window=(1920, 1080)):
        self.box, self.scale, self.window = box, scale, window
        self.calls = []

    def locator(self, selector):
        page = self

        class Locator:
            def count(self):
                page.calls.append("count")
                return 1

            def is_visible(self):
                page.calls.append("is_visible")
                return True

            def bounding_box(self, *, timeout):
                page.calls.append("bounding_box")
                return dict(page.box)

        return Locator()

    def screenshot(self, *, clip, caret, timeout):
        self.calls.append(("screenshot", dict(clip), caret))
        right = min(clip["x"] + clip["width"], self.window[0])
        bottom = min(clip["y"] + clip["height"], self.window[1])
        return FakePng(round((right - clip["x"]) * self.scale), round((bottom - clip["y"]) * self.scale))


class CaptureStripTest(unittest.TestCase):
    """PlaywrightSession.capture on a fake page: the right strip, and the time of each step."""

    def capture(self, page, strip=None):
        session = object.__new__(observe.PlaywrightSession)
        session._page, session._error_type = page, RuntimeError
        with mock.patch.object(observe, "decode_png", fake_decode):
            return session.capture("canvas.layer.plot", 5_000, strip=strip)

    def test_only_the_right_strip_is_captured(self):
        page = FakeChartPage({"x": 100.5, "y": 80.0, "width": 554.0, "height": 871.0})
        capture = self.capture(page, strip=154.0)
        [shot] = [call for call in page.calls if call[0] == "screenshot"]
        self.assertEqual(shot[1], {"x": 100.5 + 554.0 - 154.0, "y": 80.0, "width": 154.0, "height": 871.0})
        self.assertEqual(shot[2], "initial")
        self.assertEqual((capture.css_width, capture.strip_css), (154.0, 154.0))
        self.assertEqual((capture.image.width, capture.image.height), (192, 1089))
        self.assertAlmostEqual(capture.scale, 1.25, places=2)
        self.assertFalse(capture.clipped)
        self.assertEqual(capture.png_bytes, 100)
        for step in ("count_ms", "visible_ms", "box_ms", "element_ms", "screenshot_ms", "decode_ms"):
            self.assertGreaterEqual(getattr(capture, step), 0.0, step)
        self.assertEqual(page.calls[:3], ["count", "is_visible", "bounding_box"])  # read anew each time

    def test_without_a_strip_or_with_a_wide_one_the_whole_element_is_captured(self):
        box = {"x": 10.0, "y": 20.0, "width": 300.0, "height": 200.0}
        for strip in (None, 300.0, 450.0):
            with self.subTest(strip=strip):
                page = FakeChartPage(box)
                capture = self.capture(page, strip=strip)
                [shot] = [call for call in page.calls if call[0] == "screenshot"]
                self.assertEqual(shot[1], box)
                self.assertEqual((capture.css_width, capture.strip_css), (300.0, None))

    def test_a_strip_cut_by_the_window_is_refused(self):
        box = {"x": 100.0, "y": 80.0, "width": 554.0, "height": 871.0}
        for window in ((600, 1080), (1920, 700)):  # the right edge, or the bottom, outside the window
            with self.subTest(window=window):
                capture = self.capture(FakeChartPage(box, window=window), strip=154.0)
                self.assertTrue(capture.clipped)
                self.assertIsNone(capture.image)

    def test_the_timings_of_a_reading_carry_the_strip_and_the_image(self):
        image = chart_images.chart(marker_y=150)
        session = FakeSession(captures={"#grafico": image})
        source = observe.QuoteOcrSource("#grafico", observe.quote_reader.MarkerModel.from_json(BLUE_MARKER))
        visual = observe.read_quote_visually(session, source, context(engine=chart_images.FakeEngineHolder()))
        strip = observe.quote_reader.strip_width(source.marker)
        self.assertEqual(session.strips, [strip])
        self.assertEqual(visual.reading.value, "1.26543")
        self.assertEqual((visual.timings.strip_css, visual.timings.image_px), (strip, f"{int(strip)}x{image.height}"))
        reading = observe.read_field(session, "price", source, context(engine=chart_images.FakeEngineHolder()))
        row = observe.timing_row(reading, attempt=1)
        self.assertEqual((row["result"], row["quote"], row["source"], row["strip_css"]), ("ok", "1.26543", "ocr", strip))


class OcrWarmUpTest(unittest.TestCase):
    """The OCR engine is created once, and its first recognition is made on a known sample."""

    def test_the_engine_is_warmed_up_once_and_reused(self):
        created = []

        class WarmEngine(observe.RapidOcrEngine):
            def __init__(self):  # no packages: only what LazyEngine calls
                created.append(self)
                self.warmed = 0

            def warm_up(self):
                self.warmed += 1
                return quote_ocr.WarmUp("1.26543", 0.99, 80.0)

        lazy = observe.LazyEngine(WarmEngine)
        ctx = context(engine=lazy)
        self.assertIs(lazy.get(), lazy.get())
        self.assertEqual((len(created), created[0].warmed), (1, 1))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            observe.announce_ocr(ctx)
            observe.announce_ocr(ctx)  # once per run
        self.assertEqual(out.getvalue().count("OCR local pronto: carregado em "), 1)
        self.assertIn("leitura de aquecimento em 80 ms, antes de qualquer cotação, na amostra 1.26543: leu '1.26543'", out.getvalue())
        self.assertNotIn("AVISO", out.getvalue())
        wrong = observe.OcrStartup(500.0, quote_ocr.WarmUp("1.2654", 0.95, 90.0))
        self.assertIn("AVISO: não é a amostra", wrong.describe())


class ReadOnlySourceTest(unittest.TestCase):
    def test_the_collector_has_no_call_that_acts_on_the_page(self):
        for name in ("observe.py", "quote_reader.py", "quote_ocr.py"):
            with self.subTest(module=name):
                tree = ast.parse((ROOT / "pocketautonomo" / name).read_text(encoding="utf-8"))
                attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                self.assertEqual(attributes & FORBIDDEN_ATTRIBUTES, set())
                self.assertNotIn("sleep", attributes)  # waits go through Playwright, never time.sleep
                names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                self.assertEqual(names & {"getattr", "setattr", "eval", "exec", "__import__"}, set())
                # The only screenshot is the chart's clip, returned to memory: no file, no full page.
                shots = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "screenshot"
                ]
                self.assertEqual(len(shots), 1 if name == "observe.py" else 0)
                for call in shots:
                    keywords = {keyword.arg for keyword in call.keywords}
                    self.assertEqual(call.args, [])
                    self.assertIn("clip", keywords)
                    self.assertEqual(keywords & {"path", "full_page"}, set())

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
