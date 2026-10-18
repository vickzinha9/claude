"""Tests of the continuous mode of the simulated operation, pocketautonomo.continuous.

No test here opens a browser, reaches the network or needs Playwright. Every run of the
continuous mode goes through operate.main(["--continuo", ...]) on test_scan's FakePlatform, the
trading screen in memory with its own clock; each run opens a new ScreenSession on it, as a new
browser would open on the same platform. The waits between two runs only move that clock. Every
name, selector and value below was invented for the tests; none came from the real platform.
"""

import ast
import contextlib
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_operate as to  # noqa: E402
import test_scan as ts  # noqa: E402
from test_observe import FORBIDDEN_ATTRIBUTES  # noqa: E402

from pocketautonomo import analysis, continuous, observe, operate, operation, scan  # noqa: E402
from pocketautonomo.cycle import LOSS, WIN  # noqa: E402
from pocketautonomo.paper_broker import PaperBroker  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00 ([|!?]) (.*)$")
STAKES = ["500", "1000", "2000", "4000"]


class ScreenSession(ts.FakeScanSession):
    """FakeScanSession whose platform can be logged out: then neither the favorites nor the asset show."""

    def read(self, selector, timeout_ms):
        if getattr(self.platform, "logged_in", True):
            return super().read(selector, timeout_ms)
        self.platform.advance(0.005)
        self.calls.append(("read", selector))
        return observe.ElementText(0)

    def favorite_items(self, selector, limit):
        if getattr(self.platform, "logged_in", True):
            return super().favorite_items(selector, limit)
        self.platform.advance(0.01)
        self.calls.append(("list", selector))
        return scan.FavoriteList(0)


class Runaway(BaseException):
    """A run went on past the time a test allows: the program catches none of its exceptions' bases."""


class Awake:
    """Records the flags given to SetThreadExecutionState through continuous.KeepAwake."""

    def __init__(self, answer=1):
        self.flags = []
        self.answer = answer

    def __call__(self, flags):
        self.flags.append(flags)
        return self.answer


class ContinuousTestCase(ts.ScanTestCase):
    """A runtime directory with the collector's sources and the favorites selector."""

    def run_continuous(
        self, *args, platform, answers=("",), on_wait=None, history=None, open_error=None, on_open=None,
        on_sleep=None, limit=6 * 3600,
    ):
        """operate.main(["--url", URL, "--continuo", *args]) with a new ScreenSession for each run.

        on_wait(seconds) runs after each wait of any session; self.sessions holds them, in order,
        and self.opened_at the platform's clock when each one opened. open_error(n), when given,
        may return an error for the n-th opening (0 first) instead of a session, and on_open(n)
        runs before it; on_sleep(seconds) runs after each wait between two runs. Past limit seconds
        of the platform's clock from the start, Runaway ends the test: a run that should have
        ended fails it instead of going on forever.
        """
        self.sessions, self.opened_at, self.sleeps = [], [], []
        self.awake = Awake()
        self.answers = ts.Answers(answers)
        deadline = platform.t + limit

        def bounded(seconds):
            if platform.t > deadline:
                raise Runaway(f"o modo contínuo continuou mais de {limit} s do relógio do teste")
            if on_wait is not None:
                on_wait(seconds)

        def open_session(profile_dir):
            if on_open is not None:
                on_open(len(self.opened_at))
            error = open_error(len(self.opened_at)) if open_error is not None else None
            self.opened_at.append(platform.t)
            if error is not None:
                raise error
            session = ScreenSession(platform, bounded)
            self.sessions.append(session)
            return session

        def sleep(seconds):
            self.sleeps.append(seconds)
            platform.advance(seconds)
            if platform.t > deadline:
                raise Runaway(f"o modo contínuo continuou mais de {limit} s do relógio do teste")
            if on_sleep is not None:
                on_sleep(seconds)

        patches = contextlib.ExitStack()
        if history is not None:
            patches.enter_context(mock.patch.object(scan.analysis, "load_history", side_effect=history))
        out, err = io.StringIO(), io.StringIO()
        with patches, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = operate.main(
                ["--url", ts.URL, "--continuo", *args],
                open_session=open_session,
                read_line=self.answers,
                now=platform.now,
                monotonic=platform.monotonic,
                sleep=sleep,
                keep_awake=continuous.KeepAwake(self.awake),
            )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def state(self):
        path = self.runtime / continuous.STATE_FILE_NAME
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_state(self, **values):
        data = {"versao": continuous.STATE_VERSION, "pausa": None, "ultimo_clique": None, "pausas_liberadas": []}
        data.update(values)
        (self.runtime / continuous.STATE_FILE_NAME).write_text(json.dumps(data), encoding="utf-8")

    def scan_files(self):
        return sorted(self.runtime.glob("scan-*.csv"))

    def operation_rows(self):
        """The rows of every operation CSV, in the order of the runs: by the first moment of each file.

        The file names come from the computer's clock, to the second, and a random suffix, so two
        runs within one second of it do not sort by name.
        """
        files = []
        for path in self.runtime.glob("operacao-2*.csv"):
            with open(path, newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                self.assertEqual(tuple(reader.fieldnames), operation.OPERATION_COLUMNS)
                rows = list(reader)
            if rows:
                files.append((rows[0]["at_utc"], rows))
        return [row for _, rows in sorted(files, key=lambda item: item[0]) for row in rows]

    def scan_rows(self, path):
        with open(path, newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertLessEqual({row["switch"] for row in rows}, set(analysis.SWITCH_VALUES))
        return rows

    def log_lines(self):
        """(marker, text) of every line of every log of the continuous mode, following the files' chain."""
        files = {}
        for path in self.runtime.glob(f"{continuous.LOG_PREFIX}2*.log"):
            lines = []
            for line in path.read_text(encoding="utf-8").splitlines():
                match = LINE.match(line)
                self.assertIsNotNone(match, line)
                lines.append(match.groups())
            files[path] = lines
        following, heads = {}, []
        for path, lines in files.items():
            text = lines[0][1] if lines else ""
            if text.startswith("(continuação de "):
                following[Path(text[len("(continuação de "):-1]).name] = path
            else:
                heads.append(path)
        self.assertEqual(len(heads), 1, heads)
        chain = heads
        while chain[-1].name in following:
            chain.append(following[chain[-1].name])
        self.assertEqual(len(chain), len(files))  # no file outside the chain
        return [entry for path in chain for entry in files[path]]

    def clicks_between(self, platform, start, end=float("inf")):
        return [index for moment, index in platform.clicks if start <= moment <= end]


def stop_at_interval(test, session_number, platform=None, seconds=scan.DEFAULT_INTERVAL):
    """on_wait that presses Ctrl+C at the first wait of seconds of session session_number (1 first)."""

    def on_wait(waited):
        if len(test.sessions) == session_number and waited == seconds:
            raise KeyboardInterrupt

    return on_wait


class RecoveryTest(ContinuousTestCase):
    def test_a_browser_failure_starts_a_new_run_that_waits_for_the_screen_by_itself(self):
        platform = ts.FakePlatform()

        def on_wait(seconds):
            if seconds == scan.DEFAULT_INTERVAL and len(self.sessions) == 1:
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")
            if seconds == scan.DEFAULT_INTERVAL and len(self.sessions) == 2:
                raise KeyboardInterrupt

        code = self.run_continuous(platform=platform, on_wait=on_wait)
        self.assertEqual(code, 130, self.err)
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self.sleeps, [continuous.RESTART_SECONDS])  # the first run had complete readings
        self.assertEqual(len(self.answers.prompts), 1)  # only the login of the first run asked anything
        self.assertIn("FALHA DO NAVEGADOR: a espera entre as leituras falhou", self.err)
        self.assertIn("AGUARDANDO A SESSÃO: o programa só lê a página", self.out)
        # The second run read the page for SCREEN_READY_SECONDS before its first click.
        second = self.opened_at[1]
        first_click = min(moment for moment, index in platform.clicks if moment >= second)
        self.assertGreaterEqual(first_click - second, continuous.SCREEN_READY_SECONDS)
        calls = self.sessions[1].calls
        waiting = calls[: next(number for number, call in enumerate(calls) if call[0] == "click")]
        self.assertTrue(waiting and {call[0] for call in waiting} <= {"read", "list", "capture"}, waiting)
        files = self.scan_files()
        self.assertEqual(len(files), 2)  # one CSV per run
        for path in files:
            self.assertEqual([row["asset"] for row in self.scan_rows(path)], [ts.EUR[0], ts.GBP[0], ts.AUD[0]])
        self.assertEqual(self.awake.flags, [continuous.AWAKE, continuous.ES_CONTINUOUS])
        self.assertIsNone(self.state()["pausa"])
        self.assertIn("MODO CONTÍNUO encerrado: 2 execuções.", self.out)

    def test_a_lost_session_ends_the_run_and_the_next_one_waits_for_the_login_without_clicking(self):
        platform = ts.FakePlatform()
        moments = {}
        polls = []

        def on_wait(seconds):
            if len(self.sessions) == 1 and seconds == scan.DEFAULT_INTERVAL and "logout" not in moments:
                platform.logged_in = False  # the session ended after the first cycle
                moments["logout"] = platform.t
            if len(self.sessions) == 2 and seconds == continuous.SCREEN_POLL_SECONDS and not platform.logged_in:
                polls.append(seconds)
                if len(polls) == 12:  # a minute later, you logged in again
                    platform.logged_in = True
                    moments["login"] = platform.t
            if len(self.sessions) == 2 and seconds == scan.DEFAULT_INTERVAL:
                raise KeyboardInterrupt

        platform.logged_in = True
        code = self.run_continuous(platform=platform, on_wait=on_wait)
        self.assertEqual(code, 130, self.err)
        self.assertEqual(len(self.sessions), 2)
        self.assertIn(
            f"a execução 1 termina aqui: {continuous.SCREEN_LOST_CYCLES} ciclos seguidos sem favoritos para visitar",
            self.out,
        )
        self.assertIn("os favoritos não aparecem (o seletor não encontrou nenhum elemento). Esperando, sem clicar.", self.out)
        self.assertEqual(self.sleeps, [continuous.RESTART_SECONDS])
        self.assertEqual(self.clicks_between(platform, moments["logout"], moments["login"]), [])  # nothing clicked while logged out
        after = self.clicks_between(platform, moments["login"])
        self.assertTrue(after)
        self.assertGreaterEqual(min(moment for moment, _ in platform.clicks if moment >= moments["login"]) - moments["login"], 30)
        self.assertIsNone(self.state()["pausa"])

    def test_runs_without_complete_readings_stop_after_the_limit_with_longer_waits(self):
        platform = ts.FakePlatform(assets=(("EUR/USD OTC", None, 1.0853), ("GBP/USD OTC", None, 1.2734)))
        with mock.patch.object(continuous, "STALL_CYCLES", 3):
            code = self.run_continuous("--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), continuous.MAX_FAILURES)
        self.assertEqual(self.sleeps, list(continuous.FAILURE_WAITS))
        self.assertIn(f"PARADA: {continuous.MAX_FAILURES} execuções seguidas terminaram sem nenhuma leitura completa", self.err)
        self.assertIn("3 ciclos seguidos sem nenhuma leitura completa", self.out)
        self.assertEqual(len(self.scan_files()), continuous.MAX_FAILURES)
        self.assertIsNone(self.state()["pausa"])

    def test_a_browser_that_keeps_failing_after_good_readings_stops_at_the_limit_of_new_runs(self):
        platform = ts.FakePlatform()

        def on_wait(seconds):
            if seconds == scan.DEFAULT_INTERVAL:  # every run fails after a whole cycle with complete readings
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")

        code = self.run_continuous(platform=platform, on_wait=on_wait)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), continuous.MAX_RESTARTS + 1)
        self.assertEqual(self.sleeps, [continuous.RESTART_SECONDS] * continuous.MAX_RESTARTS)
        self.assertIn(
            f"PARADA: {continuous.MAX_RESTARTS + 1} execuções terminaram e pediram uma nova em menos de 60 min", self.err
        )

    def test_a_page_that_did_not_load_is_never_read_and_its_run_ends(self):
        platform = ts.FakePlatform()

        def on_open(number):
            platform.url = "chrome-error://chromewebdata/" if number == 1 else ts.URL  # the second load failed

        def on_wait(seconds):
            if seconds == scan.DEFAULT_INTERVAL and len(self.sessions) == 1:
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")
            if seconds == scan.DEFAULT_INTERVAL and len(self.sessions) == 3:
                raise KeyboardInterrupt

        code = self.run_continuous(platform=platform, on_wait=on_wait, on_open=on_open)
        self.assertEqual(code, 130, self.err)
        self.assertEqual(len(self.sessions), 3)
        self.assertEqual({call[0] for call in self.sessions[1].calls}, set())  # nothing read outside pocketoption.com
        self.assertIn(
            f"a execução 2 termina aqui: a página ficou fora de {observe.ALLOWED_DOMAIN} por 10 min", self.out
        )
        self.assertEqual(self.sleeps, [continuous.RESTART_SECONDS, continuous.FAILURE_WAITS[0]])
        self.assertEqual(self.sessions[2].clicked(), [0, 1, 2])

    def test_ctrl_c_between_two_runs_ends_it(self):
        platform = ts.FakePlatform()

        def on_wait(seconds):
            if seconds == scan.DEFAULT_INTERVAL:
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")

        def on_sleep(seconds):
            raise KeyboardInterrupt

        code = self.run_continuous(platform=platform, on_wait=on_wait, on_sleep=on_sleep)
        self.assertEqual(code, 130, self.err)
        self.assertEqual(len(self.sessions), 1)
        self.assertIn("Interrompido por Ctrl+C entre duas execuções.", self.out)
        self.assertEqual(self.awake.flags, [continuous.AWAKE, continuous.ES_CONTINUOUS])

    def test_a_browser_that_never_opens_is_tried_a_few_times_with_longer_waits(self):
        platform = ts.FakePlatform()
        code = self.run_continuous(
            platform=platform, answers=(),
            open_error=lambda number: observe.BrowserError("o Chromium não abriu (Error: perfil em uso)"),
        )
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.opened_at), continuous.MAX_FAILURES)
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.sleeps, list(continuous.FAILURE_WAITS))
        self.assertEqual(platform.clicks, [])
        self.assertEqual(self.err.count("FALHA DO NAVEGADOR: o Chromium não abriu"), continuous.MAX_FAILURES)


class PauseTest(ContinuousTestCase):
    def start_with_a_pause(self, platform):
        """The first start: the switch to GBP/USD OTC is never confirmed."""
        platform.ignored.add(1)
        code = self.run_continuous("--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.out)
        return code

    def test_the_pause_ends_the_run_at_once_and_no_run_starts_by_itself(self):
        platform = ts.FakePlatform()
        self.start_with_a_pause(platform)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.sessions[0].clicked(), [1])  # nothing was clicked after the switch that failed
        [path] = self.scan_files()
        rows = self.scan_rows(path)
        self.assertEqual([(row["asset"], row["switch"]) for row in rows], [(ts.EUR[0], "já selecionado"), ("", "não confirmada")])
        self.assertTrue(rows[1]["quote_association"].startswith(f"PAUSA desde a troca não confirmada para {ts.GBP[0]}"))
        pause = self.state()["pausa"]
        self.assertEqual((pause["ativo"], pause["ciclo"], pause["execucao"], pause["csv"]), (ts.GBP[0], 1, 1, path.name))
        self.assertIn("não foi confirmada", pause["motivo"])
        self.assertIsNone(self.state()["ultimo_clique"])  # the list, read again, still has it
        self.assertIn("mostra que o clique não tirou nenhum favorito dela", self.out)
        self.assertIn("A execução termina aqui, sem outro clique.", self.err)
        self.assertNotIn("A navegação continua só para o diagnóstico", self.err)
        self.assertIn(f"MODO CONTÍNUO: a PAUSA ficou gravada em {self.runtime / continuous.STATE_FILE_NAME}", self.err)
        events = [row["event"] for row in self.operation_rows()]
        self.assertEqual(events, [operation.SCAN_PAUSED])

    def test_a_kept_pause_needs_the_typed_word_after_the_login_and_a_new_page_does_not_release_it(self):
        platform = ts.FakePlatform()
        self.start_with_a_pause(platform)
        kept = self.state()
        clicks = list(platform.clicks)
        for answer in ("", "liberar depois", "sim"):
            with self.subTest(answer=answer):
                code = self.run_continuous(platform=platform, answers=("", answer))
                self.assertEqual(code, 1, self.out)
                self.assertIn("PAUSA DO MODO CONTÍNUO: a operação contínua não recomeça sozinha.", self.err)
                self.assertIn("PARADA: a PAUSA continua gravada", self.err)
                self.assertIn("O programa não verificou, e não consegue verificar, a qual ativo o gráfico pertence", self.out)
                self.assertEqual(platform.clicks, clicks)  # the new page clicked nothing
                self.assertEqual(self.state()["pausa"], kept["pausa"])
                self.assertEqual(len(self.sessions), 1)
        platform.ignored.clear()  # GBP/USD OTC switches again
        code = self.run_continuous(platform=platform, answers=("", "liberar"), on_wait=stop_at_interval(self, 1))
        self.assertEqual(code, 130, self.err)
        self.assertIn("PAUSA liberada por você, depois de conferir a tela", self.out)
        state = self.state()
        self.assertIsNone(state["pausa"])
        [released] = state["pausas_liberadas"]
        self.assertEqual(released["ativo"], ts.GBP[0])
        self.assertIn("liberada_utc", released)
        self.assertEqual(self.sessions[0].clicked(), [1, 2])  # the scan went on after the release

    def test_a_run_that_ends_between_a_click_and_its_confirmation_leaves_a_pause(self):
        cases = (
            ("Ctrl+C", KeyboardInterrupt, 130),
            ("falha do navegador", observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)"), 1),
            ("janela fechada", None, 1),
        )
        for name, error, expected in cases:
            with self.subTest(name=name):
                self.setUp()
                platform = ts.FakePlatform(delays=(0.6, 0.7, 0.8))  # the name changes after the first poll of it

                def on_wait(seconds, platform=platform, error=error):
                    if platform.clicks and seconds == scan.ASSET_POLL_SECONDS and platform.named == 0:
                        if error is None:
                            self.sessions[-1].window_closed = True
                            return
                        raise error

                code = self.run_continuous(platform=platform, on_wait=on_wait)
                self.assertEqual(code, expected, self.err)
                self.assertEqual(len(self.sessions), 1)  # never a new run
                self.assertEqual(self.sleeps, [])
                state = self.state()
                self.assertEqual(state["ultimo_clique"]["estado"], continuous.PENDING)
                self.assertEqual(state["pausa"]["ativo"], ts.GBP[0])
                self.assertIn("antes de a troca ser confirmada", state["pausa"]["motivo"])
                self.assertIn("PAUSA: a execução 1 terminou depois do clique em GBP/USD OTC", self.err)
                code = self.run_continuous(platform=platform, answers=("", ""))
                self.assertEqual(code, 1, self.out)
                self.assertIn("PAUSA DO MODO CONTÍNUO", self.err)
                self.assertEqual(self.sessions[0].clicked(), [])

    def test_a_click_that_failed_with_an_error_and_never_switched_ends_in_a_pause(self):
        def fail(session, selector, index, check):
            raise scan.ClickUncertain("Error: Target page, context or browser has been closed")

        platform = ts.FakePlatform()
        with mock.patch.object(ts.FakeScanSession, "click_favorite", fail):
            code = self.run_continuous("--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.sleeps, [])
        pause = self.state()["pausa"]
        self.assertEqual(pause["ativo"], ts.GBP[0])
        [path] = self.scan_files()
        rows = self.scan_rows(path)
        self.assertEqual(rows[1]["switch"], scan.NOT_CONFIRMED)
        self.assertIn("depois de um clique que falhou com um erro do navegador", rows[1]["asset_status"])

    def test_a_favorite_that_cannot_be_read_before_its_click_is_skipped_without_a_pause(self):
        click = ts.FakeScanSession.click_favorite

        def unreadable(session, selector, index, check):
            if index == 1:
                raise observe.ReadError("Error: elemento sumiu antes da leitura")
            return click(session, selector, index, check)

        platform = ts.FakePlatform()
        with mock.patch.object(ts.FakeScanSession, "click_favorite", unreadable):
            code = self.run_continuous(platform=platform, on_wait=stop_at_interval(self, 1))
        self.assertEqual(code, 130, self.err)
        self.assertIsNone(self.state()["pausa"])
        self.assertEqual(self.state()["ultimo_clique"]["chave"], "AUDCADOTC")  # the refused one was undone
        self.assertEqual(self.sessions[0].clicked(), [2])

    def test_a_pause_that_cannot_be_written_stops_and_the_next_start_still_sees_its_click(self):
        written = continuous.write_json

        def failing(path, data):
            if data.get("pausa") is not None:
                raise OSError(28, "No space left on device")
            written(path, data)

        platform = ts.FakePlatform()
        platform.ignored.add(1)
        with mock.patch.object(continuous, "write_json", failing):
            code = self.run_continuous("--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.out)
        self.assertIn("PARADA: [Errno 28] No space left on device", self.err)
        self.assertIn("AVISO: a PAUSA não pôde ser gravada", self.err)
        state = self.state()
        self.assertIsNone(state["pausa"])
        self.assertEqual((state["ultimo_clique"]["chave"], state["ultimo_clique"]["estado"]), ("GBPUSDOTC", continuous.PENDING))
        code = self.run_continuous(platform=platform, answers=("", ""))
        self.assertEqual(code, 1, self.out)
        self.assertIn("PAUSA DO MODO CONTÍNUO", self.err)
        self.assertIn("o modo contínuo anterior terminou sem gravar o fim da execução 1", self.state()["pausa"]["motivo"])
        self.assertEqual(self.sessions[0].clicked(), [])

    def test_a_release_without_a_readable_list_keeps_the_last_click_for_the_first_list(self):
        record = {
            "chave": "GBPUSDOTC", "ativo": ts.GBP[0], "posicao": 2, "total": 3, "estado": continuous.PENDING,
            "execucao": 1, "ciclo": 1, "em_utc": "2026-10-14T11:00:00.000+00:00",
        }
        pause = {"ativo": ts.GBP[0], "ciclo": 1, "execucao": 1, "csv": None, "desde_utc": record["em_utc"], "motivo": "teste"}
        self.write_state(pausa=pause, ultimo_clique=record)
        platform = ts.FakePlatform()
        del platform.items[1]  # the click on GBP/USD OTC took it out of the bar
        platform.logged_in = False  # and the list cannot be read while you check the screen

        def release():
            platform.logged_in = True
            return "LIBERAR"

        code = self.run_continuous(platform=platform, answers=("", release))
        self.assertEqual(code, 1, self.out)
        self.assertIn("O clique continua gravado e é conferido na primeira leitura da lista.", self.out)
        self.assertIn("o último favorito clicado sumiu da lista de favoritos depois do clique", self.err)
        self.assertEqual(platform.clicks, [])
        state = self.state()
        self.assertIsNone(state["pausa"])
        self.assertEqual(len(state["pausas_liberadas"]), 1)
        self.assertEqual((state["ultimo_clique"]["chave"], state["ultimo_clique"]["estado"]), ("GBPUSDOTC", continuous.CONFIRMED))

    def test_a_click_left_pending_by_an_abrupt_end_is_a_pause_at_the_next_start(self):
        record = {
            "chave": "GBPUSDOTC", "ativo": ts.GBP[0], "posicao": 2, "total": 3, "estado": continuous.PENDING,
            "execucao": 4, "ciclo": 7, "em_utc": "2026-10-14T11:00:00.000+00:00",
        }
        self.write_state(ultimo_clique=record)
        platform = ts.FakePlatform()
        code = self.run_continuous(platform=platform, answers=("", ""))
        self.assertEqual(code, 1, self.out)
        self.assertEqual(platform.clicks, [])
        pause = self.state()["pausa"]
        self.assertEqual((pause["ativo"], pause["execucao"], pause["ciclo"]), (ts.GBP[0], 4, 7))
        self.assertIn("o modo contínuo anterior terminou sem gravar o fim da execução 4", pause["motivo"])
        self.assertIn(f"{ts.GBP[0]} continua na lista", self.out)


class LastClickTest(ContinuousTestCase):
    """The last click survives the end of a run, so that the next run still checks it."""

    def test_the_next_run_checks_the_last_click_and_stops_when_it_removed_the_favorite(self):
        platform = ts.FakePlatform()

        def on_wait(seconds):
            if len(self.sessions) == 1 and len(platform.clicks) == 1 and seconds == scan.POLL_SECONDS:
                del platform.items[1]  # the click on GBP/USD OTC took it out of the bar...
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")  # ...and the run ended

        code = self.run_continuous(platform=platform, on_wait=on_wait)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), 2)  # the browser failure started a new run
        self.assertEqual(self.sessions[1].clicked(), [])  # which clicked nothing
        self.assertIn("o último favorito clicado sumiu da lista de favoritos depois do clique", self.err)
        self.assertIn(f"Último clique gravado, de uma execução anterior: {ts.GBP[0]}, com 3 elementos na lista", self.out)
        self.assertIn("continuo_windows.cmd --configure", self.out)
        record = self.state()["ultimo_clique"]
        self.assertEqual((record["chave"], record["total"], record["estado"]), ("GBPUSDOTC", 3, continuous.CONFIRMED))
        self.assertIsNone(self.state()["pausa"])
        # Reviewing the list with --configure clears it, after showing it.
        clicks = len(platform.clicks)
        code = self.run_continuous("--configure", platform=platform, answers=("", "", ""), on_wait=stop_at_interval(self, 1))
        self.assertEqual(code, 130, self.err)
        self.assertIn(f"ATENÇÃO: {ts.GBP[0]} não está mais na lista de favoritos: eram 3 elementos no clique, e agora são 2", self.err)
        self.assertIn("Registro do último clique apagado: você conferiu a lista de favoritos.", self.out)
        self.assertGreater(len(platform.clicks), clicks)

    def test_a_run_ended_by_the_watch_reads_the_list_once_more_and_stops_on_a_removal(self):
        platform = ts.FakePlatform(assets=(("EUR/USD OTC", None, 1.0853), ("GBP/USD OTC", None, 1.2734), ("AUD/CAD OTC", None, 0.9121)))

        def on_wait(seconds):
            if len(platform.clicks) == 2 and seconds == scan.POLL_SECONDS and len(platform.items) == 3:
                del platform.items[2]  # the confirmed click on AUD/CAD OTC also took it out of the bar

        with mock.patch.object(continuous, "STALL_CYCLES", 1):
            code = self.run_continuous("--load-timeout", "5", platform=platform, on_wait=on_wait)
        self.assertEqual(code, 1, self.out)
        self.assertIn("a execução 1 termina aqui: 1 ciclo sem nenhuma leitura completa", self.out)
        self.assertIn("PARADA: o último favorito clicado sumiu da lista de favoritos depois do clique", self.err)
        self.assertEqual(len(self.sessions), 1)  # no new run after the removal
        self.assertEqual(self.sleeps, [])

    def test_after_a_pause_the_list_is_read_again_and_the_next_start_shows_the_removal(self):
        platform = ts.FakePlatform()
        platform.removed_on_click.add(1)  # the click on GBP/USD OTC removes it and switches nothing
        code = self.run_continuous("--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.out)
        self.assertIn("Na leitura da lista depois da PAUSA: o último favorito clicado sumiu da lista", self.err)
        state = self.state()
        self.assertEqual(state["pausa"]["ativo"], ts.GBP[0])
        self.assertEqual((state["ultimo_clique"]["chave"], state["ultimo_clique"]["total"]), ("GBPUSDOTC", 3))
        code = self.run_continuous(platform=platform, answers=("", ""))
        self.assertEqual(code, 1, self.out)
        self.assertIn(f"ATENÇÃO: {ts.GBP[0]} não está mais na lista de favoritos: eram 3 elementos no clique, e agora são 2", self.err)
        self.assertEqual(self.sessions[0].clicked(), [])
        self.assertEqual(self.state()["ultimo_clique"]["chave"], "GBPUSDOTC")  # still kept


def cycle_rules(test, rows):
    """Check, without assuming a direction, the rules of every cycle in the operation's rows."""
    started = [row for row in rows if row["event"] == operation.CYCLE_STARTED]
    test.assertTrue(started)
    for start in started:
        cycle = [row for row in rows if row["cycle_id"] == start["cycle_id"]]
        test.assertIn(start["direction"], ("CALL", "PUT"))
        for row in cycle:
            if row["asset"]:
                test.assertEqual(row["asset"], start["asset"], row)
            if row["direction"]:
                test.assertEqual(row["direction"], start["direction"], row)  # one direction to the end
        entries = [row for row in cycle if row["event"] == operation.ENTRY_SUBMITTED]
        test.assertLessEqual(len(entries), 4)  # never a fifth entry
        test.assertEqual([row["stake_cents"] for row in entries], STAKES[: len(entries)])
        test.assertEqual([row["attempt"] for row in entries], [str(number) for number in range(1, len(entries) + 1)])
        results = [row for row in cycle if row["event"] == operation.RESULT]
        for result, following in zip(results, entries[1:]):
            test.assertEqual(result["result"], LOSS, cycle)  # a gale only after a confirmed LOSS
        if results and results[-1]["result"] == WIN:
            test.assertEqual(len(entries), len(results))  # a WIN ends the cycle


class OperationTest(ContinuousTestCase):
    def test_a_pending_entry_crosses_a_restart_without_a_second_order_and_keeps_the_cycle_rules(self):
        platform = to.RisingPlatform()
        polls = []

        def on_wait(seconds):
            if len(self.sessions) == 1 and seconds == operate.POLL_SECONDS:
                polls.append(seconds)
                if len(polls) == 100:  # 50 s into the first entry
                    raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")
            if len(self.sessions) == 2 and seconds == scan.DEFAULT_INTERVAL:
                raise KeyboardInterrupt

        history = [to.validated_history(ts.EUR[0]), analysis.History()]  # no rule in the second run
        code = self.run_continuous(platform=platform, on_wait=on_wait, history=history)
        self.assertEqual(code, 130, self.err)
        self.assertEqual(len(self.sessions), 2)
        rows = self.operation_rows()
        cycle_rules(self, rows)
        entries = [row for row in rows if row["event"] == operation.ENTRY_SUBMITTED]
        self.assertEqual(len(entries), 1)  # the pending entry was never sent again
        broker = PaperBroker(self.runtime / operate.ORDERS_NAME, 300.0, platform.now)
        self.addCleanup(broker.close)
        orders = broker.orders()
        self.assertEqual([order.reservation_id for order in orders], [entries[0]["reservation_id"]])
        self.assertIn(orders[0].state, (WIN, LOSS))  # settled by a quote read in the second run
        self.assertEqual([row["event"] for row in rows if row["event"] == operation.WINDOW_OPENED], [])  # no gale after the restart
        self.assertEqual(rows[-1]["event"], operation.CYCLE_ENDED)
        self.assertIn("Pendência preservada: ciclo 1, tentativa 1", self.out)

    def test_a_whole_cycle_keeps_its_rules_whatever_its_direction(self):
        for platform in (to.RisingPlatform(), to.FallingPlatform()):
            with self.subTest(platform=type(platform).__name__):
                self.setUp()
                ended = []

                def on_wait(seconds, ended=ended):
                    if seconds == scan.DEFAULT_INTERVAL and any(self.runtime.glob("operacao-2*.csv")):
                        ended.append(seconds)
                        raise KeyboardInterrupt

                code = self.run_continuous(
                    platform=platform, on_wait=on_wait, history=[to.validated_history(ts.EUR[0])],
                )
                self.assertEqual(code, 130, self.err)
                rows = self.operation_rows()
                cycle_rules(self, rows)
                self.assertEqual(rows[-1]["event"], operation.CYCLE_ENDED)
                self.assertTrue(all(row["execution"] == operation.SIMULATED for row in rows))


class StopTest(ContinuousTestCase):
    def test_a_safety_stop_never_starts_another_run(self):
        platform = ts.FakePlatform()

        def add_buy_button(seconds):
            if len(platform.clicks) == 1 and not any(item.text == "BUY" for item in platform.items):
                platform.items.append(ts.FakeItem("BUY"))

        code = self.run_continuous(platform=platform, on_wait=add_buy_button)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("PARADA: o seletor dos favoritos deixou de servir", self.err)

    def test_a_selector_that_became_unsafe_during_the_wait_for_the_screen_stops_without_clicking(self):
        platform = ts.FakePlatform()

        def on_wait(seconds):
            if len(self.sessions) == 1 and seconds == scan.DEFAULT_INTERVAL:
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")
            if len(self.sessions) == 2 and seconds == continuous.SCREEN_POLL_SECONDS:
                if not any(item.text == "BUY" for item in platform.items):
                    platform.items.append(ts.FakeItem("BUY"))

        code = self.run_continuous(platform=platform, on_wait=on_wait)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self.sessions[1].clicked(), [])
        self.assertEqual(self.sleeps, [continuous.RESTART_SECONDS])
        self.assertIn("PARADA: o seletor dos favoritos deixou de servir", self.err)
        self.assertIn("O modo contínuo parou, sem clicar", self.err)

    def test_closing_the_window_ends_the_continuous_mode(self):
        platform = ts.FakePlatform()

        def close_window(seconds):
            if len(platform.clicks) == 2 and seconds == scan.DEFAULT_INTERVAL:
                self.sessions[-1].window_closed = True

        code = self.run_continuous(platform=platform, on_wait=close_window)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(len(self.sessions), 1)
        self.assertIn("o modo contínuo terminou, sem abri-la de novo", self.out)

    def test_a_window_closed_before_any_line_with_data_is_not_a_success(self):
        platform = ts.FakePlatform()

        def close_window(seconds):
            if seconds == scan.POLL_SECONDS:
                self.sessions[-1].window_closed = True

        code = self.run_continuous(platform=platform, on_wait=close_window)
        self.assertEqual(code, 1, self.err)
        self.assertIn("PARADA: nenhuma leitura com dados foi gravada", self.err)
        self.assertEqual(len(self.sessions), 1)

    def test_a_storage_failure_stops_without_a_new_run(self):
        class FailingStart(operate.SqliteStore):
            def save_submission_start(self, reservation_id):
                raise operate.StorageError("simulated write failure")

        with mock.patch.object(operate, "SqliteStore", FailingStart):
            code = self.run_continuous(platform=to.RisingPlatform(), history=[to.validated_history(ts.EUR[0])])
        self.assertEqual(code, 1, self.out)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("PARADA: o início do envio não pôde ser gravado", self.err)

    def test_options_that_do_not_fit_and_a_second_instance(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            for args in (("--continuo", "--cycles", "2"), ("--continuo", "--demonstracao")):
                result = subprocess.run(
                    [sys.executable, "-m", "pocketautonomo.operate", *args], cwd=directory, env=env,
                    capture_output=True, encoding="utf-8", timeout=60,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--continuo", result.stderr)
            shown = subprocess.run(
                [sys.executable, "-m", "pocketautonomo.operate", "--help"], cwd=directory, env=env,
                capture_output=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("--continuo", shown.stdout)
            self.assertFalse((Path(directory) / "runtime").exists())
        lock = scan.ScanLock(self.runtime / scan.LOCK_FILE_NAME)
        self.addCleanup(lock.release)
        code = self.run_continuous(platform=ts.FakePlatform(), answers=())
        self.assertEqual(code, 1)
        self.assertIn("PARADA: outra varredura já está rodando com esta pasta", self.err)
        self.assertEqual(self.opened_at, [])
        self.assertEqual(list(self.runtime.glob(f"{continuous.LOG_PREFIX}*.log")), [])  # a refused start writes no log


class StateTest(ContinuousTestCase):
    def test_a_state_that_cannot_be_read_stops_before_the_browser_and_is_kept(self):
        cases = (
            ("{ não é json", "não é um JSON válido"),
            (json.dumps({"versao": 9}), "versão 9"),
            (json.dumps({"versao": 1, "ultimo_clique": {"chave": "X", "ativo": "X", "total": 2, "estado": "talvez"}}), "'talvez'"),
            (json.dumps({"versao": 1, "pausa": {"ativo": ""}}), "a pausa não tem ativo e motivo"),
            (json.dumps({"versao": 1, "outra": 1}), "chave desconhecida: outra"),
        )
        path = self.runtime / continuous.STATE_FILE_NAME
        for text, message in cases:
            with self.subTest(message=message):
                path.write_text(text, encoding="utf-8")
                before = path.read_bytes()
                code = self.run_continuous(platform=ts.FakePlatform(), answers=())
                self.assertEqual(code, 1, self.out)
                self.assertIn(message, self.err)
                self.assertIn(f"para começar sem ele, mude o nome de {continuous.STATE_FILE_NAME}", self.err)
                self.assertEqual(self.opened_at, [])
                self.assertEqual(path.read_bytes(), before)

    def test_a_click_that_cannot_be_kept_in_the_state_is_never_made(self):
        written = continuous.write_json

        def failing(path, data):
            record = data.get("ultimo_clique")
            if record is not None and record["estado"] == continuous.PENDING:
                raise OSError(28, "No space left on device")
            written(path, data)

        platform = ts.FakePlatform()
        with mock.patch.object(continuous, "write_json", failing):
            code = self.run_continuous(platform=platform)
        self.assertEqual(code, 1, self.out)
        self.assertEqual(platform.clicks, [])  # the record comes before the click
        self.assertEqual(len(self.sessions), 1)
        self.assertIn("PARADA: [Errno 28] No space left on device", self.err)
        self.assertIsNone(self.state())  # nothing was written, and no PAUSA was made up for a click that never was
        self.assertNotIn("PAUSA", self.err)

    def test_the_state_is_read_only_by_the_holder_of_the_lock(self):
        path = self.runtime / continuous.STATE_FILE_NAME
        path.write_text("{ não é json", encoding="utf-8")
        lock = scan.ScanLock(self.runtime / scan.LOCK_FILE_NAME)
        self.addCleanup(lock.release)
        with mock.patch.object(continuous.ContinuousState, "load", side_effect=AssertionError("lido sem a trava")):
            code = self.run_continuous(platform=ts.FakePlatform(), answers=())
        self.assertEqual(code, 1)
        self.assertIn("PARADA: outra varredura já está rodando com esta pasta", self.err)

    def test_a_run_started_without_the_continuous_mode_says_that_a_pause_is_kept(self):
        pause = {"ativo": ts.GBP[0], "ciclo": 3, "execucao": 2, "csv": None, "desde_utc": "2026-10-14T11:00:00.000+00:00", "motivo": "teste"}
        self.write_state(pausa=pause)
        before = (self.runtime / continuous.STATE_FILE_NAME).read_bytes()
        code = self.run_scan("--cycles", "1", platform=ts.FakePlatform())
        self.assertEqual(code, 0, self.err)
        self.assertIn(f"AVISO: o modo contínuo guarda uma PAUSA em {self.runtime / continuous.STATE_FILE_NAME} (troca para {ts.GBP[0]}", self.err)
        code = to.OperateTestCase.run_operation(self, "--cycles", "1", platform=ts.FakePlatform())
        self.assertEqual(code, 0, self.err)
        self.assertIn("AVISO: o modo contínuo guarda uma PAUSA", self.err)
        self.assertEqual((self.runtime / continuous.STATE_FILE_NAME).read_bytes(), before)  # only read
        self.write_state()  # without a PAUSA, nothing is said
        self.run_scan("--cycles", "1", platform=ts.FakePlatform())
        self.assertNotIn("PAUSA", self.err)

    def test_the_journal_keeps_the_click_until_a_list_checks_it(self):
        path = self.runtime / continuous.STATE_FILE_NAME
        state = continuous.ContinuousState(path, lambda: ts.START)
        journal = continuous.ClickJournal(state, 3, lambda: 12, lambda: ts.START)
        target = scan.Favorite(1, "GBP/USD OTC 90%", "GBP/USD OTC")

        def saved():
            return json.loads(path.read_text(encoding="utf-8"))["ultimo_clique"]

        journal.clicking(target, 3)
        self.assertEqual(
            saved(),
            {"chave": "GBPUSDOTC", "ativo": "GBP/USD OTC", "posicao": 2, "total": 3, "estado": continuous.PENDING,
             "execucao": 3, "ciclo": 12, "em_utc": observe.utc_text(ts.START)},
        )
        journal.not_clicked()  # refused before the click: back to nothing
        self.assertIsNone(saved())
        journal.clicking(target, 3)
        journal.confirmed()
        self.assertEqual(saved()["estado"], continuous.CONFIRMED)
        journal.checked()
        self.assertIsNone(saved())
        self.assertEqual(sorted(item.name for item in self.runtime.iterdir() if item.name.startswith("continuo")), [continuous.STATE_FILE_NAME])


class LogTest(ContinuousTestCase):
    def test_the_log_has_every_line_of_the_terminal_with_the_clock_also_in_new_files(self):
        platform = ts.FakePlatform()

        def on_wait(seconds):
            if seconds == scan.DEFAULT_INTERVAL and len(self.sessions) == 1:
                raise observe.BrowserError("a espera entre as leituras falhou (Error: Target crashed)")
            if seconds == scan.DEFAULT_INTERVAL and len(self.sessions) == 2:
                raise KeyboardInterrupt

        with mock.patch.object(continuous, "MAX_LOG_BYTES", 4000):
            code = self.run_continuous(platform=platform, on_wait=on_wait)
        self.assertEqual(code, 130, self.err)
        files = list(self.runtime.glob(f"{continuous.LOG_PREFIX}2*.log"))
        self.assertGreater(len(files), 3)  # a new file after each 4000 bytes, none deleted
        lines = self.log_lines()
        written = [text for marker, text in lines if marker == "|" and not text.startswith("(continuação de ")]
        warned = [text for marker, text in lines if marker == "!"]
        self.assertEqual(written, self.out.splitlines())
        self.assertEqual(warned, self.err.splitlines())
        asked = [text for marker, text in lines if marker == "?"]
        self.assertEqual(asked, [prompt.rstrip() for prompt in self.answers.prompts])  # the question, never the answer
        self.assertEqual(len(asked), 1)
        self.assertIn("FALHA DO NAVEGADOR: a espera entre as leituras falhou (Error: Target crashed)", warned)
        self.assertTrue(any(text.startswith("CONTÍNUO: execução 2,") for text in written))

    def test_an_error_that_ends_the_program_goes_to_the_log_too(self):
        streams = (sys.stdout, sys.stderr)
        with mock.patch.object(continuous.Continuous, "prepare", side_effect=RuntimeError("falha inesperada do teste")):
            with self.assertRaises(RuntimeError):
                self.run_continuous(platform=ts.FakePlatform())
        lines = self.log_lines()
        warned = [text for marker, text in lines if marker == "!"]
        self.assertIn("Traceback (most recent call last):", warned)
        self.assertEqual(warned[-1], "RuntimeError: falha inesperada do teste")
        self.assertEqual(self.awake.flags, [continuous.AWAKE, continuous.ES_CONTINUOUS])
        self.assertEqual((sys.stdout, sys.stderr), streams)  # the streams are back

    def test_a_log_that_cannot_be_written_never_stops_the_run(self):
        class FailingFile:
            def __init__(self, file):
                self.file = file
                self.lines = 0

            def write(self, text):
                self.lines += 1
                if self.lines > 5:
                    raise OSError(28, "No space left on device")
                return self.file.write(text)

            def __getattr__(self, name):
                return getattr(self.file, name)

        opened = continuous.ContinuousLog._new_file

        def failing(log, previous=None):
            opened(log, previous)
            if log._file is not None:
                log._file = FailingFile(log._file)

        platform = ts.FakePlatform()
        with mock.patch.object(continuous.ContinuousLog, "_new_file", failing):
            code = self.run_continuous(platform=platform, on_wait=stop_at_interval(self, 1, seconds=scan.DEFAULT_INTERVAL))
        self.assertEqual(code, 130, self.err)
        self.assertEqual(self.err.count("AVISO: o log do modo contínuo não pôde ser gravado"), 1)
        self.assertEqual(self.sessions[0].clicked(), [1, 2])  # the scan went on


class KeepAwakeTest(unittest.TestCase):
    def test_the_flags_and_their_end(self):
        awake = Awake()
        keeper = continuous.KeepAwake(awake)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(keeper.start())
            keeper.stop()
            keeper.stop()
        self.assertEqual(
            awake.flags,
            [continuous.ES_CONTINUOUS | continuous.ES_SYSTEM_REQUIRED | continuous.ES_DISPLAY_REQUIRED, continuous.ES_CONTINUOUS],
        )
        self.assertEqual((continuous.ES_CONTINUOUS, continuous.ES_SYSTEM_REQUIRED, continuous.ES_DISPLAY_REQUIRED), (0x80000000, 1, 2))
        self.assertIn("Prevenção da suspensão ligada", out.getvalue())

    def test_a_refusal_is_said_and_changes_nothing(self):
        awake = Awake(answer=0)
        keeper = continuous.KeepAwake(awake)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(keeper.start())
            keeper.stop()
        self.assertEqual(awake.flags, [continuous.AWAKE])  # nothing to restore
        self.assertIn("o Windows recusou a prevenção da suspensão", err.getvalue())

    @unittest.skipIf(os.name == "nt", "fora do Windows")
    def test_outside_windows_nothing_changes(self):
        self.assertIsNone(continuous.execution_state_function())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(continuous.KeepAwake().start())
        self.assertIn("só no Windows", out.getvalue())

    @unittest.skipUnless(os.name == "nt", "SetThreadExecutionState só existe no Windows")
    def test_the_real_call_on_windows(self):
        self.assertIsNotNone(continuous.execution_state_function())
        keeper = continuous.KeepAwake()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(keeper.start())
            keeper.stop()
        self.assertFalse(keeper.active)


class SafetyTest(unittest.TestCase):
    def test_the_continuous_mode_reads_the_page_only_through_the_scan_and_reaches_no_network(self):
        # Its clicks are the scan's: SafetyTest in test_scan pins the only one, and RealContinuousTest counts them.
        tree = ast.parse((ROOT / "pocketautonomo" / "continuous.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"playwright", "requests", "urllib", "socket", "http", "websocket", "aiohttp"}, imported)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes & (FORBIDDEN_ATTRIBUTES | {"click_favorite", "screenshot", "goto"}), set())
        session_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "session"
        }
        self.assertEqual(session_calls, {"is_closed", "current_url", "wait"})  # it reads through scan and observe


if __name__ == "__main__":
    unittest.main()
