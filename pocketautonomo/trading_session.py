"""The browser session of the real execution: the scan's session plus the order panel.

TradingSession is the only code of the program that touches the Pocket Option's order panel:
the time of the order (the M5 preset, 00:05:00), the amount and the Buy or Sell button. It
also hands every frame of the page's WebSocket to a pocket_socket.SocketMonitor, which only
reads them: the answers of the platform (order accepted or refused, balance, result) come
from there. It never types a password, never reads cookies or storage, and never sends a
frame of its own: the order goes out as when you click the button yourself.

The selectors are those of the platform's trading page (checked on 2026-11-04). Every action
checks that exactly one visible element matches before acting.
"""

import re

from pocketautonomo import scan
from pocketautonomo.observe import ReadError, describe

AMOUNT_INPUT = ".block--bet-amount input"
EXPIRATION_VALUE = ".block--expiration-inputs .control__value"
EXPIRATION_MODAL = ".expiration-inputs-list-modal"
EXPIRATION_PRESET = ".expiration-inputs-list-modal .dops__timeframes-item"
CALL_BUTTON = ".btn-call"
PUT_BUTTON = ".btn-put"
ACTION_TIMEOUT_MS = 2000
FIVE_MINUTES = "00:05:00"
M5 = "M5"

# One read of everything the order needs, in a single round trip; it only reads the page.
_PANEL_SCRIPT = """() => {
  const all = (s) => [...document.querySelectorAll(s)];
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const one = (s) => { const v = all(s).filter(visible); return v.length === 1 ? v[0] : null; };
  const text = (s) => { const e = one(s); return e ? (e.innerText || '').trim() : null; };
  const amount = one('%(amount)s');
  return {
    asset: text('.current-symbol'),
    account: text('.balance-info-block__label'),
    balance: text('.balance-info-block__balance'),
    expiration: text('%(expiration)s .value__val'),
    amount: amount ? amount.value : null,
    payout: text('.value__val-start'),
    call: all('%(call)s').filter(visible).length,
    put: all('%(put)s').filter(visible).length,
    modal: all('%(modal)s').filter(visible).length,
    covering: all('%(covering)s').filter(visible).length,
  };
}""" % {
    "amount": AMOUNT_INPUT, "expiration": EXPIRATION_VALUE, "call": CALL_BUTTON, "put": PUT_BUTTON,
    "modal": EXPIRATION_MODAL, "covering": ".tutorial-v1",
}


class TradingError(Exception):
    """An action on the order panel did not happen; nothing was sent."""


class ClickUncertain(Exception):
    """The click on Buy or Sell failed in a way that may still have reached the page."""


class TradingSession(scan.ScanSession):
    """ScanSession with the order panel and the page's WebSocket; see the module's docstring."""

    monitor = None

    def attach_monitor(self, monitor):
        """Give every WebSocket frame of the page, from now on, to monitor."""
        self.monitor = monitor

        def on_socket(socket):
            socket.on("framesent", monitor.sent)
            socket.on("framereceived", monitor.received)

        try:
            self._page.on("websocket", on_socket)
        except self._error_type as error:
            raise ReadError(describe(error)) from error

    def pump(self, milliseconds):
        """Let Playwright deliver the browser's events (the socket frames) for milliseconds."""
        try:
            self._page.wait_for_timeout(milliseconds)
        except self._error_type as error:
            if not self._page.is_closed():
                raise ReadError(describe(error)) from error

    def panel(self):
        try:
            return self._page.evaluate(_PANEL_SCRIPT)
        except self._error_type as error:
            raise ReadError(describe(error)) from error

    def set_expiration(self):
        """Choose the M5 preset (00:05:00) in the time of the order."""
        try:
            control = self._only(EXPIRATION_VALUE)
            if self._page.locator(EXPIRATION_MODAL).count() == 0:
                control.click(timeout=ACTION_TIMEOUT_MS)
            preset = self._page.locator(EXPIRATION_PRESET).filter(has_text=re.compile(rf"^\s*{M5}\s*$"))
            if preset.count() != 1:
                raise TradingError(f"o atalho {M5} do tempo da ordem não apareceu (achados: {preset.count()})")
            preset.click(timeout=ACTION_TIMEOUT_MS)
            self._page.keyboard.press("Escape")
            if self._page.locator(EXPIRATION_MODAL).count():
                control.click(timeout=ACTION_TIMEOUT_MS)  # the control opens and closes the list
        except self._error_type as error:
            raise TradingError(f"o tempo da ordem não foi escolhido ({describe(error)})") from error

    def set_amount(self, text):
        """Type text in the amount of the order, as a person does, and leave the field."""
        try:
            field = self._only(AMOUNT_INPUT)
            field.click(timeout=ACTION_TIMEOUT_MS)
            field.press("Control+A")
            field.press_sequentially(text)
            field.press("Tab")
        except self._error_type as error:
            raise TradingError(f"o valor da ordem não foi digitado ({describe(error)})") from error

    def click_direction(self, direction):
        """Click Buy (CALL) or Sell (PUT): the order goes to the account."""
        selector = CALL_BUTTON if direction == "CALL" else PUT_BUTTON if direction == "PUT" else None
        if selector is None:
            raise TradingError(f"direção inválida: {direction!r}")
        try:
            button = self._only(selector)
        except self._error_type as error:
            raise TradingError(f"o botão {direction} não foi achado ({describe(error)})") from error
        try:
            button.click(timeout=ACTION_TIMEOUT_MS, no_wait_after=True)
        except self._error_type as error:
            raise ClickUncertain(describe(error)) from error

    def _only(self, selector):
        """The only visible element of selector; TradingError when there is not exactly one."""
        locator = self._page.locator(f"{selector} >> visible=true")
        found = locator.count()
        if found != 1:
            raise TradingError(f"{found} elementos visíveis para {selector}; a ordem precisa de exatamente um")
        return locator


def open_trading_session(profile_dir, *, headless=False):
    """Start Playwright and open Chromium with the persistent profile, with the order panel."""
    from pathlib import Path

    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as error:
        from pocketautonomo.observe import ConfigError, install_hint

        raise ConfigError(install_hint()) from error
    return TradingSession(sync_playwright, Error, Path(profile_dir), headless)
