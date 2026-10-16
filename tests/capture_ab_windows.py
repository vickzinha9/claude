"""Measurement only, for the Windows job of the CI: the capture with a visible browser window.

python tests/capture_ab_windows.py opens the product's DiagnosticSession with a visible
window (not headless), on the local favorites page of tests/test_observe_playwright.py,
served by Playwright for the reserved name observe.test: never the Pocket Option. It reads
the quote, with both capture methods, with the window visible, covered by a second browser
window, visible again and minimized, and prints the median of the step "tela" for each.

It changes the windows itself, through test code (DevTools commands of the Browser domain,
a second browser), which the product never does. It fails nothing: timings depend on the
machine, so it always exits 0 and only prints what it measured, or why it could not. A CI
virtual machine is not your computer, and a local page is not the Pocket Option: this shows
whether a mechanism exists on Windows, not how fast the platform is.
"""

import re
import statistics
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

READINGS = 6  # of each method, alternating, in each condition


def main():
    from pocketautonomo import capture_check, observe
    import test_observe_playwright as pages

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        runtime = Path(directory) / "runtime"
        patches = (
            mock.patch.object(observe, "RUNTIME_DIR", runtime),
            mock.patch.object(observe, "ALLOWED_DOMAIN", "observe.test"),
            mock.patch.object(observe, "HOST_PATTERN", re.compile(r"observe\.test")),
        )
        for patch in patches:
            patch.start()
        session = cover = None
        try:
            session = capture_check.open_diagnostic_session(runtime / "browser-profile", headless=False)
            session._context.route(
                "https://observe.test/**",
                lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=pages.SCAN_PAGE),
            )
            session.open(pages.SCAN_URL)
            session.wait(1.5)
            ctx = observe.Context()
            ctx.slow_capture_warned = True
            source = observe.QuoteOcrSource("#plot", observe.quote_reader.MarkerModel.from_json(pages.SCAN_MARKER))
            devtools = session._context.new_cdp_session(session._page)
            window = devtools.send("Browser.getWindowForTarget")
            print(f"Janela do navegador medido: {window['bounds']}", flush=True)
            results = {}

            def measure(label):
                direct = capture_check.DirectView(session)
                for number in range(READINGS):
                    for method in ((capture_check.SCREENSHOT, capture_check.DIRECT) if number % 2 == 0 else (capture_check.DIRECT, capture_check.SCREENSHOT)):
                        session.wait(capture_check.SHORT_WAIT)
                        view = session if method == capture_check.SCREENSHOT else direct
                        reading = observe.read_field(view, "price", source, ctx)
                        tela = reading.timings.screenshot_ms if reading.timings else None
                        results.setdefault((label, method), []).append((tela, reading.available, reading.reason))
                for method in (capture_check.SCREENSHOT, capture_check.DIRECT):
                    values = results[(label, method)]
                    times = [tela for tela, _, _ in values if tela is not None]
                    read = sum(ok for _, ok, _ in values)
                    shown = f"mediana {statistics.median(times):.0f} ms, máximo {max(times):.0f} ms" if times else "sem captura"
                    reasons = sorted({reason for _, ok, reason in values if not ok and reason})
                    extra = f"; indisponível: {reasons[0]}" if reasons else ""
                    print(f"{label:<24} | {method:<15} | {read}/{len(values)} com cotação | tela: {shown}{extra}", flush=True)

            session._page.bring_to_front()
            measure("visível")
            browser = session._playwright.chromium.launch(headless=False, args=["--start-maximized"])
            cover = browser.new_page(no_viewport=True)
            cover.set_content("<body style='margin:0;background:#202020'><h1 style='color:#eee'>janela por cima</h1></body>")
            cover.bring_to_front()
            browser_devtools = browser.new_browser_cdp_session()
            covering = browser_devtools.send("Browser.getWindowForTarget", {"targetId": cdp_target(cover)})
            measured = window["bounds"]
            bounds = {
                "left": measured["left"] - 20,
                "top": measured["top"] - 20,
                "width": measured["width"] + 40,
                "height": measured["height"] + 40,
            }
            browser_devtools.send("Browser.setWindowBounds", {"windowId": covering["windowId"], "bounds": {"windowState": "normal"}})
            browser_devtools.send("Browser.setWindowBounds", {"windowId": covering["windowId"], "bounds": bounds})
            cover.bring_to_front()
            covering = browser_devtools.send("Browser.getWindowForTarget", {"targetId": cdp_target(cover)})
            print(f"Janela que cobre: {covering['bounds']}", flush=True)
            cover.wait_for_timeout(1500)
            measure("coberta por outra janela")
            browser.close()
            cover = None
            session._page.bring_to_front()
            session.wait(1.5)
            measure("visível de novo")
            devtools.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}})
            session.wait(1.5)
            measure("minimizada")
            devtools.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "normal"}})
        except Exception:
            print("A medição com janela visível não pôde ser feita neste computador:", flush=True)
            traceback.print_exc(file=sys.stdout)
        finally:
            for closable in (cover, session):
                if closable is not None:
                    try:
                        closable.close()
                    except Exception:
                        pass
            for patch in patches:
                patch.stop()
    return 0


def cdp_target(page):
    """The DevTools target id of page, through a page-level session of its own browser."""
    session = page.context.new_cdp_session(page)
    try:
        return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        session.detach()


if __name__ == "__main__":
    sys.exit(main())
