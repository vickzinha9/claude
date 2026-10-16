"""Tests of the favorites scan, pocketautonomo.scan.

No test here opens a browser or reaches the network, and none needs Playwright.
FakePlatform is a trading screen in memory, with its own clock: after a click on a
favorite, the asset's name, the payout and the chart change at different moments, as the
delays of each test say. FakeScanSession stands in for ScanSession on top of it; its waits
only move that clock. Every name, selector and value below was invented for the tests;
none came from the real platform.
"""

import ast
import contextlib
import csv
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # chart_images and test_observe

import chart_images  # noqa: E402
from test_observe import BLUE_MARKER, FORBIDDEN_ATTRIBUTES, START, Answers, shown  # noqa: E402

from pocketautonomo import observe, scan  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
URL = "https://pocketoption.com/pt/tela-de-teste/"  # invented path, never opened
ASSET, QUOTE, PAYOUT, CHART, FAVORITES = "#ativo", "#cotacao", "#payout", "#grafico", ".fav"
EUR = ("EUR/USD OTC", "+92%", 1.08530)
GBP = ("GBP/USD OTC", "+90%", 1.27340)
AUD = ("AUD/CAD OTC", "+85%", 0.91210)


class FakeItem:
    """One element of the favorites selector; target is the asset its click selects, or None."""

    def __init__(self, text, target=None, visible=True, width=110.0, height=32.0, fields=0, classes="fav"):
        self.text, self.target, self.visible = text, target, visible
        self.width, self.height, self.fields, self.classes = width, height, fields, classes


class FakePlatform:
    """The trading screen: assets, the favorites bar and what a click changes, and when.

    named, paid and drawn are the assets shown by the asset field, the payout field and the
    chart. A click on a favorite changes each of them after its own delay, in seconds of
    the platform's clock, which only waits and readings move.
    """

    def __init__(self, assets=(EUR, GBP, AUD), selected=0, delays=(0.2, 0.3, 0.4)):
        self.assets = [list(asset) for asset in assets]  # name, payout text or None, base or None
        self.items = [FakeItem(f"{name}\n{payout.lstrip('+') if payout else ''}", index) for index, (name, payout, _) in enumerate(assets)]
        self.named = self.paid = self.drawn = selected
        self.delays = dict(zip(("named", "paid", "drawn"), delays))
        self.t = 0.0
        self.pending = []
        self.clicks = []  # (time, index of the favorite)
        self.ignored = set()  # favorites whose click changes nothing
        self.removed_on_click = set()  # favorites that a click removes from the bar
        self.form_fields = 0
        self.url = URL
        self.suggestions = {}  # selector -> what ScanSession.class_suggestions finds
        self.other_lists = {}  # selector other than FAVORITES -> the FakeItems it finds
        self.quote_reads = 0  # readings of the quote so far
        # quote_override(n), when given, may answer the n-th reading of the quote instead of
        # the chart: an ElementText, or None to read the chart.
        self.quote_override = None
        self.quote_delay = 0.0  # seconds that each reading of the quote takes
        self.payout_delay = 0.0  # seconds that each reading of the payout takes

    def now(self):
        return START + timedelta(seconds=self.t)

    def monotonic(self):
        return 100.0 + self.t

    def advance(self, seconds):
        self.t += seconds
        for event in [event for event in self.pending if event[0] <= self.t]:
            self.pending.remove(event)
            setattr(self, event[1], event[2])

    def click(self, index):
        self.clicks.append((round(self.t, 3), index))
        item = self.items[index]
        if index in self.removed_on_click:
            del self.items[index]
            return
        if item.target is None or index in self.ignored:
            return
        for attribute, delay in self.delays.items():
            self.pending.append((self.t + delay, attribute, item.target))

    def quote(self, index):
        base = self.assets[index][2]
        return None if base is None else f"{base + 0.00002 * math.sin(self.t * 3):.5f}"


class PlatformEngine:
    """Stands in for the OCR: it reads the quote that the chart shows now."""

    def __init__(self, platform):
        self.platform = platform

    def recognize(self, gray):
        return self.platform.quote(self.platform.drawn), 0.99


class FakeScanSession:
    """Stands in for ScanSession on top of FakePlatform; it records every call it gets.

    on_wait(seconds), when given, runs after each wait: a test uses it to change the
    platform or to press Ctrl+C at a chosen moment.
    """

    def __init__(self, platform, on_wait=None):
        self.platform = platform
        self.on_wait = on_wait
        self.calls = []
        self.waits = []
        self.opened = []
        self.window_closed = False
        self.closes = 0

    def open(self, url):
        self.opened.append(url)

    def current_url(self):
        return self.platform.url

    def is_closed(self):
        return self.window_closed

    def read(self, selector, timeout_ms):
        platform = self.platform
        platform.advance(0.005)
        self.calls.append(("read", selector))
        if selector == ASSET:
            return shown(platform.assets[platform.named][0])
        if selector == QUOTE:
            platform.advance(platform.quote_delay)
            platform.quote_reads += 1
            if platform.quote_override is not None:
                override = platform.quote_override(platform.quote_reads)
                if override is not None:
                    return override
            text = platform.quote(platform.drawn)
            return observe.ElementText(0) if text is None else shown(text)
        if selector == PAYOUT:
            platform.advance(platform.payout_delay)
            payout = platform.assets[platform.paid][1]
            return observe.ElementText(0) if payout is None else shown(payout)
        return observe.ElementText(0)

    def capture(self, selector, timeout_ms, strip=None):
        self.platform.advance(0.05)
        self.calls.append(("capture", selector, strip))
        image = chart_images.chart(marker_y=150)
        if strip is not None:
            image = chart_images.right_strip(image, strip)
        return observe.Capture(
            1, True, image, image.width, element_ms=3.0, screenshot_ms=40.0, decode_ms=5.0, strip_css=strip
        )

    def favorite_items(self, selector, limit):
        self.platform.advance(0.01)
        self.calls.append(("list", selector))
        items = self.platform.items if selector == FAVORITES else self.platform.other_lists.get(selector, [])
        if not items or len(items) > limit:
            return scan.FavoriteList(len(items))
        return scan.FavoriteList(
            len(items), tuple(item.text for item in items), tuple(item.visible for item in items), self.platform.form_fields
        )

    def click_favorite(self, selector, index, check):
        item = self.platform.items[index]
        problem = check(scan.FavoriteItem(item.text, item.visible, item.width, item.height, item.fields))
        if problem is not None:
            self.calls.append(("refused", index, problem))
            return problem
        self.calls.append(("click", index))
        self.platform.click(index)
        return None

    def class_suggestions(self, selector, limit):
        return self.platform.suggestions.get(selector, [])

    def favorite_details(self, selector, indices):
        self.platform.advance(0.01)
        self.calls.append(("details", tuple(indices)))
        items = self.platform.items if selector == FAVORITES else []
        return {
            index: scan.FavoriteItem(
                items[index].text, items[index].visible, items[index].width, items[index].height,
                items[index].fields, 100.0 + 120.0 * index, 64.0, items[index].classes,
            )
            for index in indices
            if 0 <= index < len(items)
        }

    def wait(self, seconds):
        self.waits.append(seconds)
        self.platform.advance(seconds)
        if self.on_wait is not None:
            self.on_wait(seconds)

    def close(self):
        self.closes += 1

    def clicked(self):
        return [call[1] for call in self.calls if call[0] == "click"]


class ScanTestCase(unittest.TestCase):
    """Every test uses its own runtime directory, with the collector's sources in it."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name) / "runtime"
        patcher = mock.patch.object(observe, "RUNTIME_DIR", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_sources()
        self.write_favorites(FAVORITES)

    def write_sources(self, **sources):
        self.runtime.mkdir(parents=True, exist_ok=True)
        data = {"asset": ASSET, "price": QUOTE, "payout": PAYOUT}
        data.update(sources)
        (self.runtime / observe.SELECTORS_FILE_NAME).write_text(json.dumps(data), encoding="utf-8")

    def write_favorites(self, selector):
        path = self.runtime / scan.SCAN_SELECTORS_FILE_NAME
        if selector is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"favorites": selector}), encoding="utf-8")

    def run_scan(self, *args, platform, answers=("",), on_wait=None, engine=None, url=URL):
        """main() with --url url and args on FakeScanSession; answers go to input()."""
        session = FakeScanSession(platform, on_wait)
        self.session = session
        self.answers = Answers(answers)
        self.opened_profiles = []

        def open_session(profile_dir):
            self.opened_profiles.append(profile_dir)
            return session

        argv = (["--url", url] if url is not None else []) + list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scan.main(
                argv,
                open_session=open_session,
                read_line=self.answers,
                now=platform.now,
                monotonic=platform.monotonic,
                engine=chart_images.FakeEngineHolder(engine) if engine is not None else None,
            )
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def rows(self):
        """The data rows of the only scan CSV, as dicts, after checking its header."""
        files = sorted(self.runtime.glob("scan-*.csv"))
        self.assertEqual(len(files), 1, files)
        with open(files[0], newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), scan.SCAN_COLUMNS)
        return [dict(zip(header, row)) for row in rows]


def in_range(quote, base):
    return abs(float(quote) - base) < 0.001


class SwitchTest(ScanTestCase):
    def test_switches_between_favorites_and_records_each_asset(self):
        platform = FakePlatform()
        code = self.run_scan("--cycles", "2", "--interval", "7", platform=platform)
        self.assertEqual((code, self.err), (0, ""), self.out)
        rows = self.rows()
        names = [EUR[0], GBP[0], AUD[0]]
        self.assertEqual([row["asset"] for row in rows], names * 2)
        self.assertEqual([row["cycle"] for row in rows], ["1"] * 3 + ["2"] * 3)
        self.assertEqual([row["favorite_position"] for row in rows], ["1", "2", "3"] * 2)
        self.assertEqual([row["switch"] for row in rows], ["já selecionado"] + ["confirmada"] * 5)
        bases = dict(zip(names, (EUR[2], GBP[2], AUD[2])))
        for row in rows:
            self.assertTrue(in_range(row["price"], bases[row["asset"]]), row)  # never another asset's quote
            self.assertEqual(row["completeness"], "completa")
            self.assertEqual(row["asset_status"], f"ok: {ASSET}")
        self.assertEqual([row["payout_percent"] for row in rows], ["92", "90", "85"] * 2)
        self.assertEqual([row["payout_eligible"] for row in rows], ["sim", "sim", "não"] * 2)
        # One click per switch, on the favorites only, in the order of the list.
        self.assertEqual(self.session.clicked(), [1, 2, 0, 1, 2])
        self.assertEqual(rows[1]["previous_asset"], EUR[0])
        self.assertTrue(in_range(rows[1]["previous_quote"], EUR[2]))
        self.assertEqual(rows[0]["previous_asset"], "")
        for row in rows[1:]:
            self.assertGreater(float(row["switch_ms"]), 0)
            self.assertGreaterEqual(int(row["polls"]), 2)  # two readings in a row that agree
        self.assertIn(7, self.session.waits)  # --interval between the two cycles
        self.assertIn(
            f"CICLO 1 | favorito 2 de 3, {GBP[0]} | COMPLETA | ativo {GBP[0]} | cotação do ativo", self.out
        )
        ready = "dados para análise: SIM (completos e atuais) | gravada no CSV"
        self.assertIn(f"payout 90% | payout ELEGÍVEL (≥ 90%) | {ready}", self.out)
        self.assertIn(f"payout 85% | payout não elegível (< 90%) | {ready}", self.out)
        self.assertIn("  tempos: troca confirmada em ", self.out)
        self.assertRegex(self.out, r"\| cotação antes da troca 1\.085\d\d em 1 tentativa, \d+ ms \|")
        self.assertIn("Payout elegível (≥ 90%) e dados para análise: EUR/USD OTC, GBP/USD OTC.", self.out)
        self.assertNotIn("SEM dados para análise", self.out)
        self.assertIn("não é probabilidade de vitória", self.out.lower())
        self.assertIn("Concluído: 2 ciclos, 6 visitas a favoritos, 5 cliques em favoritos", self.out)
        self.assertIn("6 leituras completas e 0 parciais, 6 com dados para análise.", self.out)
        # The previous asset's quote, read before each click, and the analysis columns.
        self.assertEqual(rows[0]["previous_quote_status"], "")  # no click: no reference read
        for row in rows[1:]:
            self.assertEqual((row["previous_quote_status"], row["previous_quote_attempts"]), (f"ok: {QUOTE}", "1"))
            self.assertGreater(float(row["previous_quote_ms"]), 0)
        for row in rows:
            self.assertEqual(row["analysis_ready"], "sim", row)
            self.assertEqual(row["analysis_note"], "ativo, cotação e payout lidos juntos, conferidos e atuais")
            self.assertLess(float(row["data_age_ms"]), scan.MAX_DATA_AGE_SECONDS * 1000)
        self.assertEqual(self.session.closes, 1)

    def test_a_slow_switch_never_mixes_the_previous_asset(self):
        # The name changes after 0.6 s, the payout after 1.9 s and the chart after 2.6 s:
        # a reading right after the name changed would mix the new name with the old data.
        platform = FakePlatform(assets=(EUR, GBP), delays=(0.6, 1.9, 2.6))
        code = self.run_scan("--cycles", "1", platform=platform)
        self.assertEqual((code, self.err), (0, ""), self.out)
        first, second = self.rows()
        self.assertEqual((second["asset"], second["payout_percent"]), (GBP[0], "90"))
        self.assertTrue(in_range(second["price"], GBP[2]), second)
        self.assertGreaterEqual(float(second["switch_ms"]), 600)
        self.assertGreaterEqual(int(second["polls"]), 4)  # it waited for the chart and the payout
        click_time = platform.clicks[0][0]
        captured = observe.utc_text(START + timedelta(seconds=click_time + 2.6))
        self.assertGreater(second["captured_at_utc"], captured)  # read after the chart changed

    def test_a_chart_that_does_not_change_leaves_the_quote_unavailable(self):
        platform = FakePlatform(assets=(EUR, GBP), delays=(0.2, 0.3, 60.0))
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 0, self.err)
        second = self.rows()[1]
        self.assertEqual((second["asset"], second["payout_percent"], second["price"]), (GBP[0], "90", ""))
        self.assertEqual(second["completeness"], "parcial")
        self.assertRegex(
            second["price_status"],
            r"^indisponível: a cotação lida, 1\.085\d\d, está a menos de 0,1% da lida no ativo anterior antes"
            r" da troca, 1\.085\d\d: o gráfico pode ainda ser o do ativo anterior \(limite de 5 s por favorito\)$",
        )
        self.assertEqual(second["payout_eligible"], "sim")

    def test_an_unconfirmed_switch_is_recorded_without_data(self):
        platform = FakePlatform()
        platform.ignored.add(1)  # the click on the second favorite changes nothing
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.err)  # the PAUSA ends the run as a failure
        rows = self.rows()
        self.assertEqual([row["switch"] for row in rows], ["já selecionado", "não confirmada", "confirmada"])
        unconfirmed = rows[1]
        self.assertEqual((unconfirmed["asset"], unconfirmed["price"], unconfirmed["payout_percent"]), ("", "", ""))
        self.assertEqual(
            unconfirmed["asset_status"],
            f"indisponível: a troca para {GBP[0]} não foi confirmada em 5 s: o ativo na tela continuou {EUR[0]}",
        )
        self.assertTrue(unconfirmed["price_status"].startswith("indisponível: não lida: a troca para"))
        self.assertEqual(unconfirmed["payout_eligible"], "indisponível")
        # The unconfirmed switch starts the PAUSA, which its own line already says.
        pause = f"PAUSA desde a troca não confirmada para {GBP[0]} no ciclo 1: o gráfico pode ser de outro ativo"
        self.assertTrue(unconfirmed["quote_association"].startswith(pause), unconfirmed["quote_association"])
        self.assertEqual(rows[0]["quote_association"], "")  # before it, nothing changes
        # The favorite after it reads its own asset, but its quote and its payout are withheld:
        # the abandoned click could still change the chart with a delay, and nothing tells it.
        after = rows[2]
        self.assertEqual((after["asset"], after["price"], after["payout_percent"]), (AUD[0], "", ""))
        self.assertEqual((after["completeness"], after["analysis_ready"], after["payout_eligible"]), ("parcial", "não", "indisponível"))
        self.assertEqual(after["price_status"], "indisponível: retida pela PAUSA desde o ciclo 1")
        self.assertEqual(after["payout_status"], "indisponível: retido pela PAUSA desde o ciclo 1")
        self.assertEqual(after["analysis_note"], "PAUSA desde o ciclo 1: nenhuma cotação ou payout lido entra na análise")
        self.assertEqual(after["quote_association"], unconfirmed["quote_association"])
        self.assertEqual(after["analysis_decision"], "AGUARDAR")
        self.assertIn("troca NÃO confirmada em ", self.out)
        self.assertIn(f"SEM CAMPOS | SEM LEITURA: a troca para {GBP[0]} não foi confirmada em 5 s", self.out)
        self.assertIn(f"  {pause}", self.out)
        self.assertIn(f"PAUSA: a troca para {GBP[0]} no ciclo 1 não foi confirmada", self.err)
        self.assertIn(scan.DEPENDENCY, self.err)  # what resuming needs, said when it starts and at the end
        self.assertEqual(self.err.count(scan.DEPENDENCY), 2)
        # What the screen and the list showed, read without any click, for finding the cause.
        diagnostic = unconfirmed["switch_diagnostic"]
        self.assertRegex(diagnostic, rf"^ativo na tela durante a espera: {EUR[0]} em \d+ leituras; ")
        self.assertIn(f"lista: 3 elementos no clique e 3 agora; {GBP[0]} na posição 2 no clique, e agora na(s) posição(ões) 2", diagnostic)
        self.assertIn(f"posição 1 (ativo na tela): texto '{EUR[0]} 92%', visível, 110x32 px em (100, 64), classe 'fav'", diagnostic)
        self.assertIn(f"posição 2 (clicado): texto '{GBP[0]} 90%', visível, 110x32 px em (220, 64), classe 'fav'", diagnostic)
        self.assertIn(f"  diagnóstico da troca: {diagnostic}", self.out)
        self.assertEqual([call for call in self.session.calls if call[0] == "details"], [("details", (0, 1))])
        self.assertEqual(unconfirmed["favorite_standby"], "")  # one failure does not set it aside
        for row in (rows[0], rows[2]):
            self.assertEqual((row["switch_diagnostic"], row["favorite_standby"]), ("", ""))

    def test_switches_that_keep_failing_end_the_cycle_without_more_clicks(self):
        assets = (EUR, GBP, AUD, ("NZD/USD OTC", "+88%", 0.59870), ("USD/CHF OTC", "+91%", 0.88120))
        platform = FakePlatform(assets=assets)
        platform.ignored.update({1, 2, 3, 4})  # no click changes the asset
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.err)  # PAUSA from the first of them
        self.assertEqual(self.session.clicked(), [1, 2, 3])  # the fifth favorite was never clicked
        self.assertIn("3 trocas seguidas não foram confirmadas: o ciclo 1 termina aqui", self.out)
        self.assertEqual([row["switch"] for row in self.rows()], ["já selecionado"] + ["não confirmada"] * 3)

    def test_unavailable_fields_keep_their_reason(self):
        platform = FakePlatform(assets=(EUR, ("USD/JPY OTC", None, 151.235), ("BTC/USD OTC", "+80%", None)))
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 0, self.err)
        _, no_payout, no_quote = self.rows()
        self.assertEqual((no_payout["asset"], no_payout["payout_percent"]), ("USD/JPY OTC", ""))
        self.assertEqual(
            no_payout["payout_status"],
            "indisponível: o seletor não encontrou nenhum elemento (limite de 5 s por favorito)",
        )
        self.assertEqual((no_payout["payout_eligible"], no_payout["completeness"]), ("indisponível", "parcial"))
        self.assertTrue(in_range(no_payout["price"], 151.235))
        self.assertEqual((no_quote["asset"], no_quote["price"], no_quote["payout_eligible"]), ("BTC/USD OTC", "", "não"))
        self.assertEqual(
            no_quote["price_status"],
            "indisponível: o seletor não encontrou nenhum elemento (limite de 5 s por favorito)",
        )
        self.assertIn("payout INDISPONÍVEL (o seletor não encontrou nenhum elemento (limite de 5 s por favorito)) | elegibilidade indisponível (sem payout)", self.out)

    def test_without_the_quote_before_the_click_the_new_quote_is_unavailable(self):
        platform = FakePlatform(assets=(EUR, GBP))
        platform.assets[0][2] = None  # the first chart has no readable quote
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 0, self.err)
        second = self.rows()[1]
        self.assertEqual((second["asset"], second["previous_quote"], second["price"]), (GBP[0], "", ""))
        # The original reason of the failure, from each of the attempts, stays in the message.
        reason = "em 3 tentativas: o seletor não encontrou nenhum elemento"
        self.assertEqual(
            second["price_status"], f"indisponível: {scan.NO_REFERENCE} ({reason}); {scan.NO_PROOF}"
        )
        self.assertEqual(second["previous_quote_status"], f"indisponível: {reason}")
        self.assertEqual(second["previous_quote_attempts"], "3")
        # The payout is eligible, but the data is not usable for analysis: two different things.
        self.assertEqual((second["payout_percent"], second["payout_eligible"]), ("90", "sim"))
        self.assertEqual((second["analysis_ready"], second["analysis_note"]), ("não", "falta a cotação do ativo"))
        self.assertIn(
            "payout 90% | payout ELEGÍVEL (≥ 90%) | dados para análise: NÃO (falta a cotação do ativo)", self.out
        )
        self.assertIn(
            f"Payout elegível (≥ 90%) e dados para análise: nenhum. Payout elegível, mas SEM dados para análise:"
            f" {EUR[0]} (falta a cotação do ativo); {GBP[0]} (falta a cotação do ativo).",
            self.out,
        )
        self.assertIn("cotação antes da troca INDISPONÍVEL em 3 tentativas", self.out)

    def test_the_visual_reading_of_the_quote_goes_into_the_scan(self):
        self.write_sources(price="", price_ocr={"chart": CHART, "marker": BLUE_MARKER})
        platform = FakePlatform(assets=(EUR, GBP))
        code = self.run_scan("--cycles", "1", platform=platform, engine=PlatformEngine(platform))
        self.assertEqual((code, self.err), (0, ""), self.out)
        rows = self.rows()
        self.assertTrue(in_range(rows[1]["price"], GBP[2]))
        for row in rows:
            self.assertEqual((row["price_source"], row["price_status"]), ("ocr", f"ok: ocr {CHART}"))
            self.assertGreater(float(row["price_capture_ms"]), 0)
        self.assertRegex(self.out, r"cotação \d+ ms \(captura \d+ ms \(elemento 3, tela 40, decodificação 5\), detecção \d+ ms, OCR \d+ ms\)")


class SlowFavoritePlatform(FakePlatform):
    """A FakePlatform where the favorites in slow switch only after their own delays."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slow = {}  # favorite index -> (named, paid, drawn) delays

    def click(self, index):
        if index not in self.slow or index in self.ignored:
            return super().click(index)
        self.clicks.append((round(self.t, 3), index))
        for attribute, delay in zip(("named", "paid", "drawn"), self.slow[index]):
            self.pending.append((self.t + delay, attribute, self.items[index].target))


class StandbyTest(ScanTestCase):
    """A favorite whose switch is never confirmed is set aside and verified again, without mixing."""

    ASSETS = (EUR, GBP, AUD)  # in the order of the favorites

    def by_asset(self, rows, name):
        [position] = [index + 1 for index, asset in enumerate(self.ASSETS) if asset[0] == name]
        return [row for row in rows if row["favorite_position"] == str(position)]

    def check_own_quotes(self, rows):
        """Every quote recorded belongs to the asset of its line: never another asset's."""
        for row in rows:
            name, _, base = self.ASSETS[int(row["favorite_position"]) - 1]
            if row["price"]:
                self.assertEqual(row["asset"], name, row)
                self.assertTrue(in_range(row["price"], base), row)
            if row["switch"] in ("não confirmada", "em espera"):
                self.assertEqual((row["asset"], row["price"], row["payout_percent"], row["analysis_ready"]), ("", "", "", "não"))

    def test_a_favorite_that_never_switches_is_set_aside_and_verified_less_often(self):
        platform = FakePlatform()
        platform.ignored.add(1)  # GBP/USD OTC: its click never changes the screen, which stays on EUR/USD OTC
        code = self.run_scan("--cycles", "12", "--interval", "3", platform=platform)
        self.assertEqual(code, 1, self.err)  # PAUSA from GBP's first unconfirmed switch, in cycle 1
        rows = self.rows()
        gbp = self.by_asset(rows, GBP[0])
        self.assertEqual(len(gbp), 12)  # one line per cycle, also without a click: the standby goes on in the PAUSA
        tried = ["não confirmada"]
        self.assertEqual(
            [row["switch"] for row in gbp],
            tried * 2 + ["em espera"] + tried + ["em espera"] * 2 + tried + ["em espera"] * 4 + tried,
        )
        self.assertEqual([index for _, index in platform.clicks].count(1), 5)  # cycles 1, 2, 4, 7 and 12, not 12
        waits = [float(row["switch_ms"]) for row in gbp if row["switch"] == "não confirmada"]
        self.assertTrue(all(20_000 <= ms < 20_400 for ms in waits), waits)  # each verification: the whole 20 s
        self.assertEqual({row["switch_ms"] for row in gbp if row["switch"] == "em espera"}, {""})
        self.assertIn(
            "2 trocas não confirmadas seguidas desde o ciclo 1: o favorito fica em espera, sem clique, no ciclo 3;"
            " nova verificação no ciclo 4",
            gbp[1]["favorite_standby"],
        )
        self.assertIn("nos ciclos 5 a 6; nova verificação no ciclo 7", gbp[3]["favorite_standby"])
        self.assertIn("nos ciclos 8 a 11; nova verificação no ciclo 12", gbp[6]["favorite_standby"])
        self.assertIn("nos ciclos 13 a 20; nova verificação no ciclo 21", gbp[11]["favorite_standby"])  # at most 8
        self.assertEqual(gbp[2]["favorite_standby"], "em espera até o ciclo 3, sem clique")
        self.assertEqual(
            gbp[2]["asset_status"],
            "indisponível: favorito em espera, sem clique: 2 trocas não confirmadas seguidas desde o ciclo 1;"
            " nova verificação no ciclo 4",
        )
        self.assertTrue(all(row["switch_diagnostic"] for row in gbp if row["switch"] == "não confirmada"))
        self.assertTrue(all(row["switch_diagnostic"] == "" for row in gbp if row["switch"] == "em espera"))
        self.assertIn("na nova verificação do favorito em espera", gbp[3]["asset_status"])
        # EUR/USD OTC comes before GBP/USD OTC: only its visit of cycle 1, before the PAUSA, keeps its
        # data. From then on every visit reads its asset, but its quote and payout are withheld, also
        # in the cycles where GBP stands by without a click: the PAUSA lasts until the end of the run.
        eur, aud = self.by_asset(rows, EUR[0]), self.by_asset(rows, AUD[0])
        self.assertEqual([row["completeness"] for row in eur], ["completa"] + ["parcial"] * 11)
        self.assertEqual([row["completeness"] for row in aud], ["parcial"] * 12)
        self.assertEqual(eur[0]["quote_association"], "")
        for row in eur[1:] + aud:
            self.assertEqual((row["price"], row["payout_percent"], row["analysis_ready"]), ("", "", "não"))
            self.assertTrue(row["quote_association"].startswith(f"PAUSA desde a troca não confirmada para {GBP[0]} no ciclo 1"))
        self.assertTrue(all(row["quote_association"] for row in gbp))
        self.check_own_quotes(rows)
        self.assertIn(f"Em espera, sem clique: {GBP[0]}.", self.out)
        self.assertIn("SEM CAMPOS | SEM LEITURA: favorito em espera, sem clique: 2 trocas", self.out)
        self.assertIn("Concluído: 12 ciclos, 29 visitas a favoritos", self.out)
        self.assertIn("7 passagens de favoritos em espera, sem clique", self.out)
        self.assertIn(f"  {GBP[0]}: 5 visitas, 0 completas, 0 com dados para análise; nenhuma leitura completa", self.out)
        self.assertIn("; 5 trocas não confirmadas e 7 passagens em espera, sem clique (último registro: ", self.out)
        history = scan.analysis.load_history(self.runtime)
        self.assertNotIn(scan.asset_key(GBP[0]), history.series)  # nothing of it reached the analysis
        self.assertNotIn(scan.asset_key(AUD[0]), history.series)
        self.assertEqual(len(history.get(EUR[0]).samples), 1)  # the visit before the PAUSA

    def test_a_favorite_set_aside_comes_back_once_its_switch_is_confirmed(self):
        platform = FakePlatform()
        platform.ignored.add(1)
        pauses = []

        def becomes_available(seconds):
            if seconds == 3:  # the pause between two cycles
                pauses.append(seconds)
                if len(pauses) == 5:  # after cycle 5: GBP/USD OTC switches again
                    platform.ignored.discard(1)

        code = self.run_scan("--cycles", "10", "--interval", "3", platform=platform, on_wait=becomes_available)
        self.assertEqual(code, 1, self.err)
        rows = self.rows()
        gbp = self.by_asset(rows, GBP[0])
        self.assertEqual(
            [row["switch"] for row in gbp],
            ["não confirmada"] * 2 + ["em espera", "não confirmada", "em espera", "em espera"] + ["confirmada"] * 4,
        )
        back = gbp[6]  # cycle 7: the verification confirmed the switch
        self.assertTrue(back["favorite_standby"].startswith("de volta: a troca foi confirmada na nova verificação,"))
        self.assertIn("depois de 3 trocas não confirmadas desde o ciclo 1 e 3 passagens em espera", back["favorite_standby"])
        # Back to every cycle, with its switch confirmed and its own name, but a confirmed switch proves
        # nothing about the chart: the PAUSA of cycle 1 stays, and nothing it reads is used.
        for row in gbp[6:]:
            self.assertEqual((row["asset"], row["previous_asset"], row["switch_diagnostic"]), (GBP[0], EUR[0], ""))
            self.assertEqual((row["completeness"], row["price"], row["analysis_ready"]), ("parcial", "", "não"))
            self.assertTrue(row["quote_association"].startswith(f"PAUSA desde a troca não confirmada para {GBP[0]} no ciclo 1"))
        self.assertEqual([row["favorite_standby"] for row in gbp[7:]], ["", "", ""])  # back to every cycle
        self.check_own_quotes(rows)
        self.assertIn("  favorito: de volta: a troca foi confirmada na nova verificação", self.out)
        history = scan.analysis.load_history(self.runtime)
        self.assertNotIn(scan.asset_key(GBP[0]), history.series)
        self.assertEqual(len(history.get(EUR[0]).samples), 1)  # only the visit before the PAUSA

    def test_a_favorite_that_comes_back_slower_is_confirmed_in_its_verification(self):
        platform = SlowFavoritePlatform()
        platform.ignored.add(1)
        pauses = []

        def slow_from_cycle_4(seconds):
            if seconds == 3:  # the pause between two cycles
                pauses.append(seconds)
                if len(pauses) == 3:  # it switches again, but its name after 5,2 s and its chart after 6,5 s
                    platform.ignored.discard(1)
                    platform.slow[1] = (5.2, 5.3, 6.5)

        code = self.run_scan("--cycles", "6", "--interval", "3", platform=platform, on_wait=slow_from_cycle_4)
        self.assertEqual(code, 1, self.err)  # in PAUSA since cycle 1
        rows = self.rows()
        gbp = self.by_asset(rows, GBP[0])
        self.assertEqual([row["switch"] for row in gbp], ["não confirmada"] * 2 + ["em espera"] + ["confirmada"] * 3)
        for row in gbp[3:]:  # the verification waited for it
            self.assertGreater(float(row["switch_ms"]), 5_000)
            self.assertEqual((row["asset"], row["completeness"], row["price"]), (GBP[0], "parcial", ""))
        self.assertTrue(gbp[3]["favorite_standby"].startswith("de volta: a troca foi confirmada na nova verificação"))
        # The checks still refused the previous asset's chart; what they kept, from GBP's own chart,
        # was withheld by the PAUSA all the same.
        with open(next(self.runtime.glob("tempos-scan-*.csv")), newline="", encoding="utf-8") as file:
            timings = [row for row in csv.DictReader(file) if row["favorite_position"] == "2" and row["moment"] == "depois da troca"]
        kept = [row for row in timings if row["verdict"].endswith("(as conferências da visita a aceitaram)")]
        self.assertTrue(kept)
        self.assertTrue(all(in_range(row["quote"], GBP[2]) for row in kept), kept)
        self.assertTrue(all(row["verdict"].startswith("recusada: ") for row in timings))
        aud = self.by_asset(rows, AUD[0])
        self.assertEqual([row["completeness"] for row in aud], ["parcial"] * 6)
        self.assertTrue(all(row["asset"] == AUD[0] for row in aud))
        self.check_own_quotes(rows)

    def test_the_verification_that_finds_the_favorite_already_selected_brings_it_back(self):
        state = scan.Standby(5, failures=3, skip=2, retry_cycle=9, passes=2)
        stub = types.SimpleNamespace(standby={"GBPUSDOTC": state})
        favorite = scan.Favorite(1, GBP[0], GBP[0])
        note = scan.update_standby(stub, favorite, types.SimpleNamespace(switch=scan.ALREADY), 9)
        self.assertEqual(
            note,
            "de volta: o favorito já estava selecionado na tela na nova verificação, sem clique, depois de 3 trocas"
            " não confirmadas desde o ciclo 5 e 2 passagens em espera; o favorito volta a todos os ciclos",
        )
        self.assertEqual(stub.standby, {})
        stub.standby["GBPUSDOTC"] = scan.Standby(5, failures=1)
        self.assertIsNone(scan.update_standby(stub, favorite, types.SimpleNamespace(switch=scan.NOT_CLICKED), 6))
        self.assertEqual(stub.standby["GBPUSDOTC"].failures, 1)  # nothing was tried: the count stays

    def test_the_diagnostic_never_stops_the_scan(self):
        platform = FakePlatform()
        session = FakeScanSession(platform)
        stub = types.SimpleNamespace(favorites_selector=FAVORITES, last_target=None)
        favorite = scan.Favorite(1, GBP[0], GBP[0])
        reading = observe.FieldReading(EUR[0], "ok: #ativo")
        seen = {EUR[0]: 80}

        def failing(*args):
            raise scan.ReadError("TimeoutError: simulada")

        session.favorite_details = failing
        text = scan.switch_diagnostic(session, stub, favorite, seen, reading)
        self.assertEqual(
            text,
            f"ativo na tela durante a espera: {EUR[0]} em 80 leituras; lista: None elementos no clique e 3 agora;"
            f" {GBP[0]} na posição ? no clique, e agora na(s) posição(ões) 2; a leitura dos detalhes falhou"
            " (TimeoutError: simulada)",
        )
        session.favorite_items = failing
        self.assertIn("a lista de favoritos não pôde ser lida agora (a leitura dos favoritos falhou: TimeoutError: simulada)",
                      scan.switch_diagnostic(session, stub, favorite, seen, reading))
        platform.url = "https://example.org/"
        self.assertTrue(scan.switch_diagnostic(session, stub, favorite, seen, reading).endswith(
            "não está em pocketoption.com: nada mais foi lido"
        ))

    def test_a_pass_in_standby_neither_counts_nor_resets_the_unconfirmed_streak(self):
        assets = (EUR, AUD, GBP, ("NZD/USD OTC", "+88%", 0.59870), ("USD/CHF OTC", "+91%", 0.88120), ("USD/JPY OTC", "+90%", 151.235))
        platform = FakePlatform(assets=assets)
        platform.ignored.add(2)  # GBP/USD OTC never switches: in standby from cycle 3
        pauses = []

        def three_more_fail(seconds):
            if seconds == 3:
                pauses.append(seconds)
                if len(pauses) == 2:  # cycle 3: AUD, NZD and USD/CHF stop switching too
                    platform.ignored.update({1, 3, 4})

        code = self.run_scan("--cycles", "3", "--interval", "3", "--load-timeout", "5", platform=platform, on_wait=three_more_fail)
        self.assertEqual(code, 1, self.err)
        third = [row for row in self.rows() if row["cycle"] == "3"]
        self.assertEqual([row["switch"] for row in third], ["confirmada", "não confirmada", "em espera", "não confirmada", "não confirmada"])
        self.assertIn("3 trocas seguidas não foram confirmadas: o ciclo 3 termina aqui", self.out)
        self.assertNotIn(5, [index for _, index in platform.clicks[-4:]])  # USD/JPY OTC was not clicked in cycle 3


class LeakPlatform(FakePlatform):
    """GBP/USD OTC's click is never confirmed, and its chart leaks onto the next favorite's visit.

    GBP (index 1) is ignored, so its click never switches the asset name: it is an abandoned
    click. When AUD/CAD OTC (index 2) is clicked, GBP's delayed chart finally lands, so during
    AUD's visit the asset field names AUD while the chart still shows GBP's quote: the dangerous
    state the review found, a correct name with a stale chart. The leak clears when the favorite
    after AUD (index 3) is clicked.
    """

    def __init__(self, assets):
        super().__init__(assets=assets)
        self.ignored.add(1)  # GBP's click never changes the asset name: abandoned
        self._leak = False

    def click(self, index):
        super().click(index)
        if index == 2:  # moving to AUD: GBP's delayed chart lands now, after the reference was read
            self._leak = True
        elif index == 3:
            self._leak = False  # it clears before the next favorite

    def quote(self, index):
        return super().quote(1 if self._leak else index)  # the chart shows GBP's leftover while leaking


# Close quotes: each one within QUOTE_JUMP of the others, and farther than PREVIOUS_QUOTE_GAP from them.
CLOSE = (
    ("EUR/USD OTC", "+92%", 1.08530),
    ("GBP/USD OTC", "+90%", 1.08800),
    ("AUD/CAD OTC", "+91%", 1.09070),
    ("NZD/USD OTC", "+88%", 1.09340),
)


class PersistentLeakPlatform(FakePlatform):
    """After GBP/USD OTC's abandoned click, the chart stays one click behind, and it never recovers.

    GBP (index 1) is ignored: its click never switches the asset name. From then on, every click
    switches the asset name and the payout to the favorite clicked, but the chart to the favorite
    clicked before it, so that GBP's late chart lands on the next visit and every later visit shows
    another asset's chart under the right name. The contamination is kept to the end: nothing on
    this screen ever puts the right chart back.
    """

    def __init__(self, assets):
        super().__init__(assets=assets)
        self.ignored.add(1)
        self.leaking = False
        self.last_target = self.drawn  # the asset of the last click, also of an ignored one

    def click(self, index):
        previous, self.last_target = self.last_target, self.items[index].target
        added = len(self.pending)
        super().click(index)
        if self.leaking:  # the chart goes to the favorite clicked before this one, never to this one
            self.pending[added:] = [
                (when, attribute, previous if attribute == "drawn" else target)
                for when, attribute, target in self.pending[added:]
            ]
        self.leaking = self.leaking or index == 1


class AssociationTest(ScanTestCase):
    """After a switch that was not confirmed, nothing read in the run is used: the PAUSA."""

    def timings(self):
        [path] = sorted(self.runtime.glob("tempos-scan-*.csv"))
        with open(path, newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def pause_note(self, name=GBP[0], cycle=1):
        return f"PAUSA desde a troca não confirmada para {name} no ciclo {cycle}: o gráfico pode ser de outro ativo"

    def test_a_stale_chart_during_the_next_visit_is_not_attributed_to_it(self):
        platform = LeakPlatform(assets=(EUR, GBP, AUD, ("NZD/USD OTC", "+88%", 0.59870)))
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.err)
        rows = self.rows()
        self.assertEqual(
            [row["switch"] for row in rows],
            ["j\u00e1 selecionado", "n\u00e3o confirmada", "confirmada", "confirmada"],
        )
        timings = [row for row in self.timings() if row["moment"] == "depois da troca"]
        kept = [row for row in timings if row["verdict"].endswith("(as conferências da visita a aceitaram)")]
        # AUD was read as AUD, but the chart was GBP's leftover: the normal checks accepted that quote
        # (it is far from the reference and stable). The PAUSA withheld it.
        leaked = [row for row in kept if row["favorite_position"] == "3"]
        self.assertTrue(leaked and all(in_range(row["quote"], GBP[2]) for row in leaked), leaked)
        aud = rows[2]
        self.assertEqual((aud["asset"], aud["price"], aud["payout_percent"], aud["analysis_ready"]), (AUD[0], "", "", "n\u00e3o"))
        self.assertTrue(aud["quote_association"].startswith(self.pause_note()))
        # NZD's chart is its own again and its switch is confirmed, but nothing on the screen proves
        # that: there is no release, and its quote is withheld too.
        own = [row for row in kept if row["favorite_position"] == "4"]
        self.assertTrue(own and all(in_range(row["quote"], 0.59870) for row in own), own)
        nzd = rows[3]
        self.assertEqual((nzd["asset"], nzd["price"], nzd["analysis_ready"]), ("NZD/USD OTC", "", "n\u00e3o"))
        self.assertTrue(nzd["quote_association"].startswith(self.pause_note()))
        self.assertFalse([row for row in timings if row["verdict"] == "aceita"])
        history = scan.analysis.load_history(self.runtime)
        self.assertEqual(sorted(history.series), [scan.asset_key(EUR[0])])  # only the visit before the PAUSA
        self.assertIn(
            f"PAUSA desde a troca não confirmada para {GBP[0]} no ciclo 1: nada lido depois dela entra na análise"
            " ou em decisões.",
            self.out,
        )
        self.assertIn(
            f"PAUSA desde a troca não confirmada para {GBP[0]} no ciclo 1: depois dela, 2 visitas com cotação ou"
            " payout lidos, todos retidos",
            self.out,
        )

    def test_close_prices_and_two_stable_wrong_readings_are_still_withheld(self):
        # GBP's quote is within QUOTE_JUMP of AUD's, so the jump limit alone cannot tell the charts
        # apart; two consecutive readings of the wrong chart agree. The PAUSA withholds it anyway.
        close_gbp = ("GBP/USD OTC", "+90%", 0.91260)  # AUD/CAD OTC is 0.91210
        platform = LeakPlatform(assets=(EUR, close_gbp, AUD, ("NZD/USD OTC", "+88%", 0.59870)))
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.err)
        aud = self.rows()[2]
        after = [row for row in self.timings() if row["favorite_position"] == "3" and row["moment"] == "depois da troca"]
        wrong = [row for row in after if in_range(row["quote"], close_gbp[2])]
        self.assertGreaterEqual(len(wrong), 2)  # two consecutive readings of the wrong chart
        accepted = [row for row in wrong if row["verdict"].endswith("(as conferências da visita a aceitaram)")]
        self.assertTrue(accepted)  # the jump and reference checks accepted the wrong chart
        for row in accepted:
            self.assertLess(abs(float(row["quote"]) - AUD[2]), AUD[2] * scan.QUOTE_JUMP)  # close to AUD's real quote
            self.assertEqual(row["verdict"], "recusada: retida pela PAUSA desde o ciclo 1 (as conferências da visita a aceitaram)")
        self.assertEqual((aud["asset"], aud["price"], aud["analysis_ready"]), (AUD[0], "", "n\u00e3o"))
        self.assertTrue(aud["quote_association"].startswith(self.pause_note()))

    def test_a_wrong_chart_that_persists_for_several_visits_never_enters_the_analysis(self):
        platform = PersistentLeakPlatform(CLOSE)
        added = []
        original = scan.analysis.Analyzer.add

        def spy(analyzer, name, sample):
            added.append((name, sample.price))
            return original(analyzer, name, sample)

        with mock.patch.object(scan.analysis.Analyzer, "add", spy):
            code = self.run_scan("--cycles", "4", "--interval", "3", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.err)
        rows = self.rows()
        visited = [row for row in rows if row["switch"] == "confirmada"]
        self.assertGreaterEqual(len(visited), 8)  # several visits after the abandoned click, in 4 cycles
        after = [row for row in self.timings() if row["moment"] == "depois da troca"]  # all after the abandoned click
        self.assertNotIn("aceita", {row["verdict"] for row in after})
        by_visit = {}
        for row in self.timings():
            if row["moment"] == "depois da troca":
                by_visit.setdefault((row["cycle"], row["favorite_position"]), []).append(row)
        bases = {name: base for name, _, base in CLOSE}
        for row in visited:
            name = row["asset"]
            self.assertEqual(name, CLOSE[int(row["favorite_position"]) - 1][0])  # the right name on the screen
            readings = by_visit[(row["cycle"], row["favorite_position"])]
            # Two stable readings of another asset's chart, close to this asset's own quote, which the
            # normal checks accepted: the contamination is invisible to them.
            self.assertEqual(len(readings), 2, readings)
            first, second = (float(reading["quote"]) for reading in readings)
            self.assertLess(abs(second - first), first * scan.QUOTE_JUMP)
            self.assertTrue(readings[1]["verdict"].endswith("(as conferências da visita a aceitaram)"), readings)
            self.assertFalse(in_range(second, bases[name]), (name, second))  # never this asset's own chart
            self.assertLess(abs(second - bases[name]), bases[name] * scan.QUOTE_JUMP)  # but close to it
            # None of it was used: no quote, no payout, no analysis, AGUARDAR.
            self.assertEqual((row["price"], row["payout_percent"], row["analysis_ready"]), ("", "", "n\u00e3o"))
            self.assertEqual(row["analysis_decision"], "AGUARDAR")
            self.assertIn("PAUSA desde o ciclo 1", row["analysis_decision_note"])
            self.assertTrue(row["quote_association"].startswith(self.pause_note()))
        self.assertEqual([name for name, _ in added], [CLOSE[0][0]])  # only the first visit, before the click
        self.assertTrue(in_range(added[0][1], CLOSE[0][2]))
        history = scan.analysis.load_history(self.runtime)
        self.assertEqual(sorted(history.series), [scan.asset_key(CLOSE[0][0])])
        self.assertEqual(len(history.get(CLOSE[0][0]).samples), 1)
        # The screen gave complete readings, only withheld: the cycles keep their normal interval.
        self.assertEqual([seconds for seconds in self.session.waits if seconds >= 3], [3, 3, 3])
        self.assertNotIn("Nenhuma leitura completa", self.out)

    def test_ctrl_c_in_the_pause_repeats_the_warning(self):
        platform = FakePlatform()
        platform.ignored.add(1)

        def press_ctrl_c(seconds):
            if seconds == 3:  # the pause after cycle 1
                raise KeyboardInterrupt

        code = self.run_scan("--interval", "3", "--load-timeout", "5", platform=platform, on_wait=press_ctrl_c)
        self.assertEqual(code, 130)
        self.assertEqual(self.err.count(f"PAUSA: a troca para {GBP[0]} no ciclo 1 não foi confirmada"), 2)
        self.assertEqual(self.err.count(scan.DEPENDENCY), 2)  # when it started, and at the end
        self.assertIn(f"PAUSA desde a troca não confirmada para {GBP[0]} no ciclo 1: depois dela, 1 visita", self.out)

    def test_the_pause_keeps_navigating_and_recording_without_using_anything(self):
        platform = starting_off_the_favorites()  # the screen is EUR, a non-favorite; the favorites are GBP and AUD
        platform.ignored.update({0, 1})  # both favorites' clicks change nothing
        code = self.run_scan("--cycles", "8", "--interval", "3", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1)
        self.assertIn("Concluído: 8 ciclos", self.out)  # the PAUSA does not stop the run
        self.assertNotIn("PARADA", self.err)
        rows = self.rows()  # every line is kept, the standby lines too
        self.assertEqual({row["cycle"] for row in rows}, {str(cycle) for cycle in range(1, 9)})
        self.assertIn("em espera", {row["switch"] for row in rows})
        self.assertEqual({row["price"] for row in rows}, {""})  # no quote was ever attributed
        self.assertTrue(all(row["quote_association"].startswith(self.pause_note()) for row in rows))
        scan.ScanLock(self.runtime / scan.LOCK_FILE_NAME).release()  # the lock was released

    def test_without_an_unconfirmed_switch_there_is_no_pause(self):
        code = self.run_scan("--cycles", "3", "--interval", "3", platform=FakePlatform())
        self.assertEqual(code, 0, self.err)
        rows = self.rows()
        self.assertEqual({row["quote_association"] for row in rows}, {""})
        self.assertEqual({row["analysis_ready"] for row in rows}, {"sim"})
        self.assertNotIn("PAUSA", self.out + self.err)
        self.assertEqual({row["verdict"] for row in self.timings() if row["moment"] == "depois da troca"} - {""},
                         {"aceita", "recusada: falta uma segunda leitura da cotação, para conferir que ela não saltou"})


def starting_off_the_favorites(assets=(EUR, GBP, AUD)):
    """A platform whose asset on the screen, the first of assets, is not a favorite.

    The favorites are the other assets, so the first visit already clicks: the first
    readings of the quote are the ones of the previous asset, before that click.
    """
    platform = FakePlatform(assets=assets)
    del platform.items[0]
    return platform


class ReferenceTest(ScanTestCase):
    """The previous asset's quote, read before the click, in short attempts that keep their reason."""

    def timing_rows(self):
        files = sorted(self.runtime.glob("tempos-scan-*.csv"))
        self.assertEqual(len(files), 1, files)
        with open(files[0], newline="", encoding="utf-8") as file:
            header, *rows = list(csv.reader(file))
        self.assertEqual(tuple(header), observe.quote_timings.TIMING_COLUMNS)
        return [dict(zip(header, row)) for row in rows]

    def test_a_failed_reading_is_tried_again_on_the_previous_asset_before_the_click(self):
        platform = starting_off_the_favorites()
        platform.quote_override = lambda n: observe.ElementText(2) if n == 1 else None  # one bad reading
        code = self.run_scan("--cycles", "1", platform=platform)
        self.assertEqual((code, self.err), (0, ""), self.out)
        first = self.rows()[0]
        self.assertEqual((first["asset"], first["switch"], first["completeness"]), (GBP[0], "confirmada", "completa"))
        self.assertEqual((first["previous_quote_status"], first["previous_quote_attempts"]), (f"ok: {QUOTE}", "2"))
        self.assertTrue(in_range(first["previous_quote"], EUR[2]))
        self.assertTrue(in_range(first["price"], GBP[2]))
        self.assertEqual(first["analysis_ready"], "sim")
        # Both attempts read the quote before the click, so on the previous asset's chart, and
        # the asset was read again after the attempt that counted.
        calls = self.session.calls
        click_at = calls.index(("click", 0))
        quotes = [index for index, call in enumerate(calls) if call == ("read", QUOTE)]
        self.assertEqual(len([index for index in quotes if index < click_at]), 2)
        self.assertEqual(calls[quotes[1] + 1], ("read", ASSET))
        self.assertEqual(self.session.waits[0], scan.REFERENCE_RETRY_SECONDS)  # the short pause between them
        # The timings file has both attempts, the failed one with its reason, and the readings
        # after the switch, with the time since the click.
        rows = self.timing_rows()
        reference = [row for row in rows if row["moment"] == "referência antes da troca"]
        self.assertEqual(
            [(row["attempt"], row["result"], row["verdict"]) for row in reference[:2]],
            [("1", "indisponível", ""), ("2", "ok", "aceita")],
        )
        self.assertEqual(reference[0]["reason"], "o seletor encontrou 2 elementos; precisa encontrar exatamente 1")
        self.assertEqual((reference[0]["asset"], reference[0]["since_click_ms"]), (EUR[0], ""))
        after = [row for row in rows if row["moment"] == "depois da troca" and row["favorite_position"] == "2"]
        self.assertGreaterEqual(len(after), 2)
        self.assertTrue(all(float(row["since_click_ms"]) >= scan.SETTLE_SECONDS * 1000 for row in after))
        # The first reading after the switch has nothing to be compared with; the last is kept.
        self.assertEqual(
            after[0]["verdict"], "recusada: falta uma segunda leitura da cotação, para conferir que ela não saltou"
        )
        self.assertEqual(after[-1]["verdict"], "aceita")
        self.assertIn("  referência antes da troca (", self.out)
        self.assertRegex(self.out, r"  depois da troca \(\d+ leituras, \d+ com cotação, \d+ aceitas? nas conferências\)")

    def test_every_original_reason_is_kept_when_all_attempts_fail(self):
        platform = starting_off_the_favorites(assets=(EUR, GBP))
        answers = {1: observe.ElementText(2), 2: observe.ElementText(0), 3: observe.ElementText(2)}
        platform.quote_override = answers.get
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 0, self.err)
        [row] = self.rows()
        reason = (
            "em 3 tentativas: o seletor encontrou 2 elementos; precisa encontrar exatamente 1"
            " / o seletor não encontrou nenhum elemento"
        )
        self.assertEqual(row["previous_quote_status"], f"indisponível: {reason}")
        self.assertEqual(row["price_status"], f"indisponível: {scan.NO_REFERENCE} ({reason}); {scan.NO_PROOF}")
        self.assertEqual((row["previous_quote"], row["previous_quote_attempts"]), ("", "3"))
        # The attempts are bounded: three, then the click, and no more readings before it.
        calls = self.session.calls
        click_at = calls.index(("click", 0))
        self.assertEqual(calls[:click_at].count(("read", QUOTE)), scan.REFERENCE_ATTEMPTS)
        self.assertEqual((row["asset"], row["payout_eligible"], row["analysis_ready"]), (GBP[0], "sim", "não"))

    def test_the_attempts_stop_at_the_time_limit(self):
        platform = starting_off_the_favorites(assets=(EUR, GBP))
        platform.quote_delay = 1.5  # each reading of the quote takes 1.5 s
        platform.quote_override = lambda n: observe.ElementText(0) if n <= 3 else None
        code = self.run_scan("--cycles", "1", platform=platform)
        self.assertEqual(code, 0, self.err)
        [row] = self.rows()
        # 1.5 s, the pause and 1.5 s more pass the 2.5 s limit: no third attempt.
        self.assertEqual(row["previous_quote_attempts"], "2")
        self.assertEqual(row["previous_quote_status"], "indisponível: em 2 tentativas: o seletor não encontrou nenhum elemento")
        self.assertLess(float(row["previous_quote_ms"]), (scan.REFERENCE_BUDGET_SECONDS + 1.6) * 1000)

    def test_a_quote_read_while_the_asset_changed_does_not_count(self):
        platform = starting_off_the_favorites()

        def the_asset_changes(n):
            if n == 1:
                platform.named = 2  # the name on the screen changes during the first reading
            return None

        platform.quote_override = the_asset_changes
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 0, self.err)
        first = self.rows()[0]
        self.assertEqual(
            first["previous_quote_status"],
            f"indisponível: em 3 tentativas: o ativo na tela mudou de {EUR[0]} para {AUD[0]} durante a leitura da cotação",
        )
        self.assertEqual(first["previous_quote"], "")


class AnalysisReadinessTest(ScanTestCase):
    """Payout eligibility and data usable for analysis are told apart."""

    def test_complete_but_old_data_is_not_usable_for_analysis(self):
        platform = FakePlatform(assets=(EUR,))
        platform.payout_delay = 6.0  # the payout takes 6 s to read, after the quote
        code = self.run_scan("--cycles", "1", "--load-timeout", "30", platform=platform)
        self.assertEqual(code, 0, self.err)
        [row] = self.rows()
        self.assertEqual((row["completeness"], row["payout_eligible"]), ("completa", "sim"))
        self.assertEqual(row["analysis_ready"], "não")
        self.assertRegex(
            row["analysis_note"],
            r"^a cotação foi capturada 6(,\d)? s antes da gravação, mais que o limite de 5 s: não é atual$",
        )
        self.assertGreater(float(row["data_age_ms"]), 5000)
        self.assertIn("Payout elegível, mas SEM dados para análise: EUR/USD OTC (a cotação foi capturada", self.out)

    def test_unconfirmed_switches_and_missing_fields_are_not_usable(self):
        platform = FakePlatform(assets=(EUR, ("USD/JPY OTC", None, 151.235), GBP))
        platform.ignored.add(2)  # the click on GBP/USD OTC changes nothing
        code = self.run_scan("--cycles", "1", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 1, self.err)  # the unconfirmed switch put the scan in PAUSA
        _, no_payout, unconfirmed = self.rows()
        self.assertEqual((no_payout["analysis_ready"], no_payout["analysis_note"]), ("não", "falta o payout"))
        self.assertEqual((no_payout["payout_eligible"], no_payout["quote_association"]), ("indisponível", ""))
        self.assertEqual(
            (unconfirmed["analysis_ready"], unconfirmed["analysis_note"]),
            ("não", "a troca de ativo não foi confirmada, e nada foi lido"),
        )
        self.assertTrue(unconfirmed["quote_association"].startswith(f"PAUSA desde a troca não confirmada para {GBP[0]}"))

    def test_the_summary_never_puts_a_quote_beside_the_payout_of_another_visit(self):
        platform = FakePlatform(assets=(EUR, GBP))

        def second_cycle(seconds):
            if seconds == 3:  # the pause between the two cycles
                platform.assets[1][1] = "+93%"
                platform.assets[1][2] = None  # GBP/USD OTC's chart stops showing a quote

        code = self.run_scan("--cycles", "2", "--interval", "3", "--load-timeout", "5", platform=platform, on_wait=second_cycle)
        self.assertEqual(code, 0, self.err)
        rows = self.rows()
        first_gbp, second_gbp = [row for row in rows if row["favorite_position"] == "2"]
        self.assertEqual((first_gbp["analysis_ready"], second_gbp["analysis_ready"]), ("sim", "não"))
        self.assertEqual(second_gbp["payout_percent"], "93")
        [line] = [line for line in self.out.splitlines() if line.startswith(f"  {GBP[0]}: ")]
        self.assertRegex(
            line,
            rf"^  {GBP[0]}: 2 visitas, 1 completa, 1 com dados para análise; última leitura completa e atual, do"
            r" ciclo 1 \(relógio local [0-9T:.+-]+\): cotação 1\.273\d\d e payout 90%, payout ELEGÍVEL \(≥ 90%\);"
            r" última visita, do ciclo 2: PARCIAL, dados para análise: NÃO \(falta a cotação do ativo\)$",
        )
        self.assertIn(first_gbp["captured_at_utc"][:19], line)  # the time of the reading it shows
        self.assertNotIn("93%", line)


class ScanAnalysisTest(ScanTestCase):
    """The local CALL/PUT analysis inside the scan: fed by usable visits, AGUARDAR without validation."""

    def test_every_visit_is_followed_by_the_analysis_and_waits_without_validation(self):
        platform = FakePlatform(assets=(EUR, GBP, ("USD/JPY OTC", None, 151.235)))
        code = self.run_scan("--cycles", "2", "--interval", "3", "--load-timeout", "5", platform=platform)
        self.assertEqual(code, 0, self.err)
        rows = self.rows()
        self.assertEqual({row["analysis_decision"] for row in rows}, {"AGUARDAR"})
        for row in rows:
            if row["analysis_ready"] == "sim":
                self.assertTrue(row["analysis_decision_note"].startswith("dados insuficientes:"), row)
            else:
                self.assertEqual(row["analysis_decision_note"], f"sem dados para análise nesta visita: {row['analysis_note']}")
        self.assertIn("Análise local CALL/PUT, com operações hipotéticas de 300 s, depois de cada visita", self.out)
        self.assertIn("0 cotações utilizáveis de 0 arquivos anteriores", self.out)
        self.assertIn("  análise CALL/PUT: AGUARDAR (dados insuficientes:", self.out)
        self.assertIn("  análise CALL/PUT: AGUARDAR (sem dados para análise nesta visita: falta o payout)", self.out)
        self.assertIn(
            "ANÁLISE CALL/PUT (operações de 300 s): 2 ativos com dados, 0 com regra validada em dados separados:"
            " AGUARDAR, sem decisão CALL ou PUT.",
            self.out,
        )
        self.assertNotRegex(self.out, r"análise CALL/PUT: (CALL|PUT) ")

    def test_the_analysis_starts_from_the_earlier_scan_files(self):
        platform = FakePlatform(assets=(EUR,))
        self.assertEqual(self.run_scan("--cycles", "1", platform=platform), 0, self.err)
        self.assertEqual(self.run_scan("--cycles", "1", "--horizon", "60", platform=platform), 0, self.err)
        self.assertIn("1 cotações utilizáveis de 1 arquivo anterior", self.out)
        self.assertIn("operações hipotéticas de 60 s", self.out)
        history = scan.analysis.load_history(self.runtime)
        self.assertEqual((history.files, history.usable), (2, 2))  # one usable visit in each file

    def test_a_validated_rule_gives_a_decision_valid_only_moments_after_the_visit(self):
        from test_analysis import legs

        series = legs(6000)
        history = scan.analysis.History()
        last = series.samples[-1]
        for sample in series.samples[:-1]:
            history.add(EUR[0], sample)
        analyzer = scan.analysis.Analyzer(300.0, history)
        stub = types.SimpleNamespace(analyzer=analyzer)
        readings = {
            "asset": observe.FieldReading(EUR[0], "ok: #ativo"),
            "price": observe.FieldReading(last.quote, "ok: #cotacao", "texto", last.at, 5.0),
            "payout": observe.FieldReading("92", "ok: #payout"),
        }
        observation = observe.Observation(last.at, URL, readings, 10.0)
        favorite = scan.Favorite(0, EUR[0], EUR[0])
        visit = scan.Visit(1, favorite, 1, scan.ALREADY, observation, polls=2, recorded_at=last.at + timedelta(seconds=1))
        decided = scan.with_decision(visit, stub)
        self.assertIn(decided.decision.action, ("CALL", "PUT"))
        row = dict(zip(scan.SCAN_COLUMNS, scan.scan_row(decided)))
        self.assertEqual(row["analysis_decision"], decided.decision.action)
        self.assertIn("validada:", row["analysis_decision_note"])
        self.assertIn("válida até", row["analysis_decision_note"])
        self.assertIn(f"  análise CALL/PUT: {decided.decision.action} (regra tendência de 300 s validada:", "\n".join(scan.visit_lines(decided)))
        late = replace(visit, recorded_at=last.at + timedelta(seconds=30))  # recorded too late: data not current
        self.assertEqual(scan.with_decision(late, stub).decision.action, "AGUARDAR")


class EligibilityTest(unittest.TestCase):
    def test_eligibility_is_a_payout_of_90_or_more(self):
        cases = {"89": "não", "89,9": "não", "89.99": "não", "90": "sim", "90,0": "sim", "92": "sim", "100": "sim"}
        for payout, expected in cases.items():
            with self.subTest(payout=payout):
                self.assertEqual(scan.eligibility(observe.FieldReading(payout, "ok: #payout")), expected)
        self.assertEqual(scan.eligibility(observe.unavailable("o seletor não encontrou nenhum elemento")), "indisponível")


class FavoritesTest(unittest.TestCase):
    def listing(self, *texts, visible=None, fields=0):
        visible = visible or (True,) * len(texts)
        return scan.FavoriteList(len(texts), texts, tuple(visible), fields)

    def test_each_favorite_names_its_asset(self):
        favorites = scan.classify(
            FAVORITES,
            self.listing("EUR/USD OTC\n92%", "+90% GBP/USD OTC", "★", "92%", "", "EUR/USD OTC 91%", "AUD/CAD OTC", visible=(True,) * 6 + (False,)),
        )
        self.assertIsNone(favorites.problem)
        self.assertEqual([item.name for item in favorites.usable], ["EUR/USD OTC", "GBP/USD OTC"])
        problems = [item.problem for item in favorites.items]
        self.assertIn("não parece um ativo", problems[2])
        self.assertTrue(problems[3].startswith("o texto só tem um percentual"))
        self.assertEqual(problems[4], "o elemento está sem texto")
        self.assertEqual(problems[5], "repete o favorito 1")
        self.assertEqual(problems[6], "o elemento não está visível")
        self.assertEqual(scan.asset_key("GBP/USD OTC"), scan.asset_key("gbpusd  otc"))
        self.assertNotEqual(scan.asset_key("EUR/USD"), scan.asset_key("EUR/USD OTC"))

    def test_a_selector_that_finds_a_financial_element_is_refused_as_a_whole(self):
        for texts, fields, word in (
            (("EUR/USD OTC", "BUY"), 0, "'buy'"),
            (("EUR/USD OTC", "Vender"), 0, "'vender'"),
            (("EUR/USD OTC", "Amount 10"), 0, "'amount'"),
            (("EUR/USD OTC", "GBP/USD OTC"), 1, "campo de formulário"),
        ):
            with self.subTest(texts=texts, fields=fields):
                favorites = scan.classify(FAVORITES, self.listing(*texts, fields=fields))
                self.assertTrue(favorites.unsafe)
                self.assertEqual(favorites.usable, [])
                self.assertIn(word, favorites.problem)
        crowded = scan.classify("div", scan.FavoriteList(scan.MAX_FAVORITES + 1))
        self.assertTrue(crowded.unsafe)
        self.assertIn("mais que 30", crowded.problem)
        self.assertFalse(scan.classify(FAVORITES, scan.FavoriteList(0)).unsafe)

    def test_the_element_is_checked_again_right_before_the_click(self):
        [target] = scan.classify(FAVORITES, self.listing("EUR/USD OTC 92%")).usable
        check = scan.click_check(target)
        self.assertIsNone(check(scan.FavoriteItem("EUR/USD OTC\n92%", True, 110, 32)))
        self.assertIsNone(check(scan.FavoriteItem("EUR/USD OTC 91%", True, 110, 32)))  # only the payout changed
        refusals = {
            "mudou antes do clique": scan.FavoriteItem("GBP/USD OTC 90%", True, 110, 32),
            "'sell'": scan.FavoriteItem("SELL", True, 110, 32),
            "deixou de estar visível": scan.FavoriteItem("EUR/USD OTC 92%", False),
            "campo de formulário": scan.FavoriteItem("EUR/USD OTC 92%", True, 110, 32, 1),
            "grande demais": scan.FavoriteItem("EUR/USD OTC 92%", True, 900, 300),
        }
        for reason, item in refusals.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, check(item))


class SafetyTest(ScanTestCase):
    def test_a_financial_element_in_the_favorites_stops_the_scan_without_clicks(self):
        for extra, fields in ((FakeItem("BUY"), 0), (FakeItem("SELL"), 0), (None, 1)):
            with self.subTest(extra=extra and extra.text, fields=fields):
                platform = FakePlatform()
                if extra is not None:
                    platform.items.append(extra)
                platform.form_fields = fields
                code = self.run_scan("--cycles", "1", platform=platform)
                self.assertEqual(code, 1)
                self.assertIn("PARADA: o seletor dos favoritos deixou de servir:", self.err)
                self.assertEqual(self.session.clicked(), [])
                self.assertEqual(platform.clicks, [])

    def test_a_button_that_appears_in_the_list_stops_the_scan_before_the_next_click(self):
        platform = FakePlatform()

        def add_buy_button(seconds):
            if len(platform.clicks) == 1 and not any(item.text == "BUY" for item in platform.items):
                platform.items.append(FakeItem("BUY"))

        code = self.run_scan("--cycles", "1", platform=platform, on_wait=add_buy_button)
        self.assertEqual(code, 1)
        self.assertEqual(self.session.clicked(), [1])  # the second click never happened
        self.assertIn("com 'buy'", self.err)
        self.assertEqual([row["asset"] for row in self.rows()], [EUR[0], GBP[0]])  # kept in the CSV

    def test_a_favorite_that_a_click_removed_stops_the_scan(self):
        platform = FakePlatform()
        platform.removed_on_click.add(1)
        code = self.run_scan("--cycles", "1", platform=platform)
        self.assertEqual(code, 1)
        self.assertIn("o último favorito clicado sumiu da lista de favoritos", self.err)
        self.assertEqual(self.session.clicked(), [1])

    def test_a_page_outside_pocketoption_is_neither_read_nor_clicked(self):
        platform = FakePlatform()
        left = []

        def leave(seconds):
            if platform.clicks and not left:
                platform.url = "https://example.org/fora"
                left.append(len(self.session.calls))

        code = self.run_scan("--cycles", "1", platform=platform, on_wait=leave)
        self.assertEqual(code, 1)
        self.assertIn(
            "PARADA: a página atual (https://example.org) não está em pocketoption.com: a varredura não lê"
            " nem clica fora dela e parou",
            self.err,
        )
        self.assertEqual(self.session.clicked(), [1])
        self.assertEqual(self.session.calls[left[0]:], [])  # nothing was read or clicked after it left

    def test_the_scan_has_a_single_click_and_only_on_a_favorite(self):
        tree = ast.parse((ROOT / "pocketautonomo" / "scan.py").read_text(encoding="utf-8"))
        acting = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES | {"sleep", "screenshot"}
        ]
        self.assertEqual([node.attr for node in acting], ["click"])
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertEqual(names & {"getattr", "setattr", "eval", "exec", "__import__"}, set())
        [session_class] = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ScanSession"]
        [method] = [node for node in ast.walk(session_class) if isinstance(node, ast.FunctionDef) and node.name == "click_favorite"]
        calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "click"
        ]
        self.assertEqual(len(calls), 1)  # the one click of the program is in click_favorite
        self.assertEqual(calls[0].args, [])
        self.assertEqual({keyword.arg for keyword in calls[0].keywords}, {"timeout"})  # no force, no position


class RunTest(ScanTestCase):
    def test_ctrl_c_keeps_the_lines_written_and_closes_everything(self):
        platform = FakePlatform()

        def press_ctrl_c(seconds):
            if len(platform.clicks) == 1 and seconds == scan.POLL_SECONDS:
                raise KeyboardInterrupt

        code = self.run_scan("--cycles", "3", platform=platform, on_wait=press_ctrl_c)
        self.assertEqual(code, 130)
        self.assertEqual([row["asset"] for row in self.rows()], [EUR[0]])  # written before Ctrl+C
        self.assertIn("Interrompido por Ctrl+C: 1 ciclo, 1 visita a favoritos, 1 clique em favoritos", self.out)
        self.assertRegex(
            self.out,
            rf"  {EUR[0]}: 1 visita, 1 completa, 1 com dados para análise; última leitura completa e atual, do"
            r" ciclo 1 \(relógio local [0-9T:.+-]+\): cotação 1\.085\d\d e payout 92%, payout ELEGÍVEL \(≥ 90%\)",
        )
        self.assertEqual(self.session.closes, 1)
        scan.ScanLock(self.runtime / scan.LOCK_FILE_NAME).release()  # the lock was released

    def test_without_cycles_it_scans_until_ctrl_c(self):
        platform = FakePlatform(assets=(EUR, GBP))
        pauses = []

        def stop_on_the_third_pause(seconds):
            if seconds == 3:
                pauses.append(seconds)
                if len(pauses) == 3:
                    raise KeyboardInterrupt

        code = self.run_scan("--interval", "3", platform=platform, on_wait=stop_on_the_third_pause)
        self.assertEqual(code, 130)
        self.assertEqual(len(self.rows()), 6)  # three cycles of two favorites, line by line
        self.assertIn("Varredura: até Ctrl+C, com 3 s entre um ciclo e o próximo", self.out)

    def test_a_cycle_without_complete_readings_waits_longer(self):
        platform = FakePlatform(assets=(("EUR/USD OTC", None, 1.0853), ("GBP/USD OTC", None, 1.2734)))

        def payouts_come_back(seconds):
            if seconds == 8:  # the longer pause after the first cycle
                platform.assets[0][1] = platform.assets[1][1] = "+92%"

        code = self.run_scan("--cycles", "3", "--interval", "4", "--load-timeout", "5", platform=platform, on_wait=payouts_come_back)
        self.assertEqual(code, 0, self.err)
        pauses = [seconds for seconds in self.session.waits if seconds >= 4]
        self.assertEqual(pauses, [8, 4])  # doubled after a cycle without complete readings, then back
        self.assertIn("Nenhuma leitura completa: próximo ciclo em 8 s, com espera maior para não insistir.", self.out)
        self.assertEqual([row["completeness"] for row in self.rows()], ["parcial"] * 2 + ["completa"] * 4)
        self.assertEqual(scan.pause_seconds(10, 5), 120)
        self.assertEqual(scan.pause_seconds(200, 2), 200)

    def test_a_second_scan_is_refused_while_one_runs(self):
        lock = scan.ScanLock(self.runtime / scan.LOCK_FILE_NAME)
        self.addCleanup(lock.release)
        code = self.run_scan("--cycles", "1", platform=FakePlatform())
        self.assertEqual(code, 1)
        self.assertIn("PARADA: outra varredura já está rodando com esta pasta", self.err)
        self.assertEqual(self.opened_profiles, [])  # the browser never opened

    def test_the_configuration_must_be_there_before_the_browser_opens(self):
        cases = (
            ({"asset": ""}, FAVORITES, "não tem o seletor do ativo"),
            ({}, None, "não tem o seletor dos favoritos. Rode com --configure"),
        )
        for sources, favorites, message in cases:
            with self.subTest(message=message):
                self.write_sources(**sources)
                self.write_favorites(favorites)
                code = self.run_scan("--cycles", "1", platform=FakePlatform())
                self.assertEqual(code, 2)
                self.assertIn("CONFIGURAÇÃO: ", self.err)
                self.assertIn(message, self.err)
                self.assertEqual(self.opened_profiles, [])

    def test_the_window_closed_ends_the_scan_with_what_it_recorded(self):
        platform = FakePlatform()

        def close_window(seconds):
            if platform.clicks:
                self.session.window_closed = True

        code = self.run_scan("--cycles", "2", platform=platform, on_wait=close_window)
        self.assertEqual(code, 0, self.err)
        self.assertIn("A janela do navegador foi fechada; a varredura terminou.", self.out)
        self.assertEqual([row["asset"] for row in self.rows()], [EUR[0]])

    def test_runs_with_python_dash_m(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")

            def run(*args):
                return subprocess.run(
                    [sys.executable, "-m", "pocketautonomo.scan", *args],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    encoding="utf-8",
                    timeout=60,
                )

            shown_help = run("--help")
            self.assertEqual(shown_help.returncode, 0, shown_help.stderr)
            for option in ("--cycles", "--interval", "--load-timeout", "--configure"):
                self.assertIn(option, shown_help.stdout)
            self.assertEqual(run("--url", "https://example.org/?ssid=SEGREDO").returncode, 2)
            self.assertEqual(run("--interval", "0").returncode, 2)
            self.assertEqual(run("--load-timeout", "500").returncode, 2)
            self.assertFalse((Path(directory) / "runtime").exists())


class ConfigureTest(ScanTestCase):
    def test_configure_lists_the_favorites_and_saves_the_selector(self):
        self.write_favorites(None)
        sources = (self.runtime / observe.SELECTORS_FILE_NAME).read_bytes()
        platform = FakePlatform()
        platform.items.append(FakeItem("★"))  # not an asset: listed and ignored
        platform.other_lists[".um"] = platform.items[:1]  # like a selector copied from one favorite
        platform.suggestions[".um"] = [(".fav", 4)]
        answers = ["", ".um", FAVORITES, ""]  # login; one element; the right selector; accept
        code = self.run_scan("--configure", "--cycles", "1", platform=platform, answers=answers)
        self.assertEqual((code, self.err), (0, ""), self.out)
        self.assertEqual(self.answers.pending, [])
        self.assertIn("O seletor .um encontrou agora 1 elemento.", self.out)
        self.assertIn("Para varrer, o seletor precisa encontrar todos os favoritos, não só um.", self.out)
        self.assertIn("ainda não conferidos: .fav (4 elementos). Cole um para ver a lista.", self.out)
        self.assertIn("O seletor .fav encontrou agora 4 elementos.", self.out)
        self.assertIn(f"    1: {EUR[0]} (selecionado agora) | texto 'EUR/USD OTC 92%'", self.out)
        self.assertIn("    4: ignorado, texto sem letras, não parece um ativo", self.out)
        self.assertIn(f"O ativo selecionado agora, {EUR[0]}, é o favorito 1", self.out)
        saved = json.loads((self.runtime / scan.SCAN_SELECTORS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["favorites"], FAVORITES)
        self.assertEqual((self.runtime / observe.SELECTORS_FILE_NAME).read_bytes(), sources)  # untouched
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.session.clicked(), [1, 2])

    def test_a_refused_selector_cannot_be_accepted(self):
        platform = FakePlatform()
        platform.items.append(FakeItem("COMPRAR"))
        answers = ["", "", "", "-"]  # login; keep .fav; Enter reads it again; cancel
        code = self.run_scan("--configure", "--cycles", "1", platform=platform, answers=answers)
        self.assertEqual(code, 2)
        self.assertEqual(self.out.count("Este seletor não pode ser usado: o seletor também encontra o elemento 4"), 2)
        self.assertIn("CONFIGURAÇÃO: a varredura precisa do seletor dos favoritos", self.err)
        self.assertEqual(platform.clicks, [])
        saved = json.loads((self.runtime / scan.SCAN_SELECTORS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved, {"favorites": FAVORITES})  # unchanged

    def test_one_element_shows_selectors_from_its_classes(self):
        platform = FakePlatform()
        session = FakeScanSession(platform)
        platform.suggestions["#um"] = [(".fav", 3), (".item.fav", 3)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scan.suggest_selectors(session, "#um")
        self.assertIn("Tirados das classes desse elemento, e ainda não conferidos: .fav (3 elementos), .item.fav (3 elementos)", out.getvalue())


class FakeElement:
    """A Playwright locator for ScanSession: one element of FAKE_DOM, or the matches of a selector."""

    def __init__(self, page, selector, index=None, inside=False):
        self.page, self.selector, self.index, self.inside = page, selector, index, inside

    def _matches(self):
        found = self.page.dom.get(self.selector, [])
        return found if self.index is None else found[self.index:self.index + 1]

    def count(self):
        self.page.calls.append(("count", self.selector, self.index, self.inside))
        if self.inside:  # form fields inside the matches
            return sum(element.get("fields", 0) for element in self.page.dom.get(self.selector, []))
        return len(self._matches())

    def all_inner_texts(self):
        self.page.calls.append(("all_inner_texts", self.selector))
        return [element["text"] for element in self._matches()]

    def nth(self, index):
        return FakeElement(self.page, self.selector, index)

    def is_visible(self):
        self.page.calls.append(("is_visible", self.selector, self.index))
        return self._matches()[0].get("visible", True)

    def inner_text(self, *, timeout):
        self.page.calls.append(("inner_text", self.selector, self.index))
        return self._matches()[0]["text"]

    def bounding_box(self, *, timeout):
        return {"x": 0, "y": 0, "width": 100, "height": 30}

    def locator(self, selector):
        return FakeElement(self.page, self.selector, self.index, inside=True)

    def and_(self, other):
        return types.SimpleNamespace(count=lambda: 0)

    def click(self, *, timeout):
        self.page.calls.append(("click", self.selector, self.index, timeout))
        if self.page.opens_tab:
            self.page.context.pages.append(object())


class ScanSessionTest(unittest.TestCase):
    """ScanSession on fake Playwright objects: what it reads, and its only click."""

    def session(self, dom, opens_tab=False):
        page = types.SimpleNamespace(dom=dom, calls=[], opens_tab=opens_tab)
        page.locator = lambda selector: FakeElement(page, selector)
        page.context = types.SimpleNamespace(pages=[page])
        session = object.__new__(scan.ScanSession)
        session._page, session._context, session._error_type = page, page.context, RuntimeError
        return session, page

    def test_listing_only_reads_and_the_click_comes_after_a_new_reading(self):
        dom = {FAVORITES: [{"text": "EUR/USD OTC 92%"}, {"text": "GBP/USD OTC 90%"}]}
        session, page = self.session(dom)
        listing = session.favorite_items(FAVORITES, scan.MAX_FAVORITES)
        self.assertEqual(listing, scan.FavoriteList(2, ("EUR/USD OTC 92%", "GBP/USD OTC 90%"), (True, True), 0))
        self.assertNotIn("click", [call[0] for call in page.calls])
        [target] = [item for item in scan.classify(FAVORITES, listing).usable if item.position == 2]
        page.calls.clear()
        self.assertIsNone(session.click_favorite(FAVORITES, 1, scan.click_check(target)))
        kinds = [call[0] for call in page.calls]
        self.assertEqual(kinds.count("click"), 1)
        self.assertLess(kinds.index("inner_text"), kinds.index("click"))  # read again right before
        self.assertEqual(page.calls[-1], ("click", FAVORITES, 1, scan.CLICK_TIMEOUT_MS))

    def test_a_refused_element_is_not_clicked_and_a_new_tab_stops(self):
        [target] = scan.classify(FAVORITES, scan.FavoriteList(1, ("EUR/USD OTC 92%",), (True,), 0)).usable
        stale = scan.Favorite(1, "EUR/USD OTC 92%", "EUR/USD OTC")  # listed at index 1, then the list changed
        for now_there, reason in (("GBP/USD OTC 90%", "mudou antes do clique"), ("BUY", "'buy'")):
            with self.subTest(now_there=now_there):
                session, page = self.session({FAVORITES: [{"text": "EUR/USD OTC 92%"}, {"text": now_there}]})
                self.assertIn(reason, session.click_favorite(FAVORITES, 1, scan.click_check(stale)))
                self.assertNotIn("click", [call[0] for call in page.calls])
        session, page = self.session({FAVORITES: [{"text": "EUR/USD OTC 92%"}]}, opens_tab=True)
        with self.assertRaises(observe.ObserverStopped) as stopped:
            session.click_favorite(FAVORITES, 0, scan.click_check(target))
        self.assertIn("abriu outra aba ou janela", str(stopped.exception))


if __name__ == "__main__":
    unittest.main()
