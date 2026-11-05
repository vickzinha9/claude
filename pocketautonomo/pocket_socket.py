"""The Pocket Option's own socket.io messages, read as the page sends and receives them.

The trading page talks to the platform through one WebSocket (socket.io, Engine.IO 4). The
browser session hands every frame of it to SocketMonitor, which only reads them: nothing here
sends a frame. The messages that matter to the real execution, as the platform sends them
(checked on its page on 2026-11-04):

- sent by the page when Buy or Sell is clicked:
  42["openOrder",{"asset":"EURUSD_otc","amount":5,"action":"put","isDemo":1,
  "requestId":12252090,"optionType":100,"time":300}]
- received: 451-["successopenOrder",{"_placeholder":true,"num":0}] and then one binary
  frame with the order: {"id":"KfwF...","openTimestamp":...,"closeTimestamp":...,
  "amount":5,"percentProfit":92,"openPrice":1.11451,"command":1,"asset":"EURUSD_otc",
  "isDemo":1,"requestId":12252090,...}. command 0 is CALL (Buy), 1 is PUT (Sell).
- received: failopenOrder, {"asset","amount","error",...}, when the platform refused it.
- received: successupdateBalance, {"isDemo":1,"balance":49990}.
- received: successcloseOrder, {"profit":-5,"deals":[{"id":..., "profit":-5,
  "openPrice":1.11423,"closePrice":1.11391,"command":0,...}]}: profit is the net result
  of each order, negative for a loss.

The platform's timestamps are on its own clock, hours away from this computer's: only
their difference (the order's duration) is used, never the moment itself.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

OPEN_ORDER = "openOrder"
SUCCESS_OPEN = "successopenOrder"
FAIL_OPEN = "failopenOrder"
SUCCESS_CLOSE = "successcloseOrder"
BALANCE = "successupdateBalance"
CLOSED_DEALS = ("updateClosedDeals", "successcloseOrder")
COMMAND_OF = {0: "CALL", 1: "PUT"}
ACTION_OF = {"CALL": "call", "PUT": "put"}

# 42["event", data] or 451-["event", {"_placeholder": true, "num": 0}] (one binary frame follows).
_EVENT = re.compile(r"^4(?P<binary>5(?P<count>\d+)-)?2?(?P<body>\[.*\])$", re.S)
MAX_EVENTS = 2000  # kept in memory; the quote stream is not kept at all
IGNORED = ("updateStream", "updateAssets", "updateCharts", "loadHistoryPeriod", "updateHistoryNewFast")


@dataclass(frozen=True)
class Event:
    seq: int
    at: float  # time.monotonic() of this computer when the frame arrived
    name: str
    data: object
    sent: bool = False


@dataclass
class _Pending:
    name: str
    left: int
    parts: list = field(default_factory=list)


def asset_code(name):
    """The platform's code of an asset shown as name: 'EUR/USD OTC' -> 'EURUSD_otc'."""
    text = " ".join(str(name).split())
    otc = text.upper().endswith(" OTC")
    if otc:
        text = text[:-4]
    code = re.sub(r"[^A-Za-z0-9#]", "", text)
    return code + ("_otc" if otc else "")


def same_asset(shown, code):
    """Whether the asset shown on the screen is the platform's code."""
    if not shown or not code:
        return False
    return asset_code(shown).lower().lstrip("#") == str(code).lower().lstrip("#")


def _decode(payload):
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return payload if isinstance(payload, str) else None


class SocketMonitor:
    """Every event of the page's socket that the execution needs, in order, from now on.

    sent() and received() take the frames as the browser reports them, text or bytes. The
    events are kept with a sequence number: mark() returns the next one, and the lookups
    take only what came after a mark, so an old event never answers a new order.
    """

    def __init__(self, monotonic=time.monotonic):
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._events = []
        self._seq = 0
        self._pending = None  # a binary event waiting for its attachments
        self.last_frame_at = None  # any frame, also the quote stream: the socket is alive
        self.frames = 0
        self.balances = {}  # isDemo (0 or 1) -> (seq, balance)
        self.closed = {}  # order id -> the deal as the platform closed it

    # Frames.

    def sent(self, payload):
        text = _decode(payload)
        if text is None:
            return
        parsed = self._parse_text(text)
        if parsed is not None:
            name, data = parsed
            if name == OPEN_ORDER:
                self._add(name, data, sent=True)

    def received(self, payload):
        self.last_frame_at = self._monotonic()
        self.frames += 1
        pending = self._pending
        if pending is not None and isinstance(payload, (bytes, bytearray, memoryview)):
            pending.left -= 1
            if pending.name:  # the attachments of an ignored event are not even decoded
                pending.parts.append(_decode(payload))
            if pending.left <= 0:
                self._pending = None
                if pending.name:
                    parts = [self._json(part) for part in pending.parts]
                    self._dispatch(pending.name, parts[0] if len(parts) == 1 else parts)
            return
        text = _decode(payload)
        if text is None:
            return
        match = _EVENT.match(text)
        if match is None:
            return
        body = self._json(match.group("body"))
        if not isinstance(body, list) or not body or not isinstance(body[0], str):
            return
        name = body[0]
        if match.group("binary"):
            count = int(match.group("count"))
            # The attachments of an ignored event, such as the quote stream, are dropped unread.
            self._pending = _Pending("" if name in IGNORED else name, count) if count > 0 else None
            return
        self._dispatch(name, body[1] if len(body) > 1 else None)

    # Lookups.

    def mark(self):
        with self._lock:
            return self._seq + 1

    def events(self, name=None, after=0, sent=None):
        with self._lock:
            return [
                event for event in self._events
                if event.seq >= after and (name is None or event.name == name) and (sent is None or event.sent == sent)
            ]

    def open_request(self, after):
        """The first openOrder the page sent since the mark after, or None."""
        found = self.events(OPEN_ORDER, after, sent=True)
        return found[0] if found else None

    def opened(self, request_id, after):
        """The successopenOrder of request_id since after, or None."""
        for event in self.events(SUCCESS_OPEN, after):
            data = event.data
            if isinstance(data, dict) and data.get("requestId") == request_id:
                return event
        return None

    def refused(self, after, asset=None, amount=None, request_id=None):
        """A failopenOrder since after, of request_id when it says it, or of asset and amount."""
        for event in self.events(FAIL_OPEN, after):
            data = event.data if isinstance(event.data, dict) else {}
            if "requestId" in data:
                if data.get("requestId") == request_id:
                    return event
                continue
            if (asset is None or data.get("asset") == asset) and (amount is None or _same_amount(data.get("amount"), amount)):
                return event
        return None

    def balance_after(self, is_demo, after):
        """The last balance of the account (is_demo 0 or 1) received since after, or None."""
        found = None
        for event in self.events(BALANCE, after):
            data = event.data
            if isinstance(data, dict) and data.get("isDemo") == is_demo and _number(data.get("balance")) is not None:
                found = event
        return None if found is None else _number(found.data["balance"])

    def balance(self, is_demo):
        with self._lock:
            known = self.balances.get(is_demo)
        return None if known is None else known[1]

    def close_of(self, order_id):
        with self._lock:
            return self.closed.get(order_id)

    def alive(self, within):
        at = self.last_frame_at
        return at is not None and self._monotonic() - at <= within

    # Internals.

    def _dispatch(self, name, data):
        if not name or name in IGNORED:
            return
        if name == BALANCE and isinstance(data, dict) and data.get("isDemo") in (0, 1):
            value = _number(data.get("balance"))
            if value is not None:
                event = self._add(name, data)
                with self._lock:
                    self.balances[data["isDemo"]] = (event.seq, value)
            return
        if name in CLOSED_DEALS:
            deals = data.get("deals") if isinstance(data, dict) else data
            if isinstance(deals, list):
                with self._lock:
                    for deal in deals:
                        if isinstance(deal, dict) and isinstance(deal.get("id"), str) and "profit" in deal:
                            self.closed[deal["id"]] = deal
            self._add(name, data)
            return
        if name in (SUCCESS_OPEN, FAIL_OPEN):
            self._add(name, data)

    def _add(self, name, data, sent=False):
        with self._lock:
            self._seq += 1
            event = Event(self._seq, self._monotonic(), name, data, sent)
            self._events.append(event)
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]
            return event

    @staticmethod
    def _parse_text(text):
        match = _EVENT.match(text)
        if match is None or match.group("binary"):
            return None
        body = SocketMonitor._json(match.group("body"))
        if not isinstance(body, list) or not body or not isinstance(body[0], str):
            return None
        return body[0], (body[1] if len(body) > 1 else None)

    @staticmethod
    def _json(text):
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _same_amount(value, amount):
    number = _number(value)
    return number is not None and abs(number - amount) < 0.005


def order_result(deal):
    """WIN, LOSS or DRAW of a closed deal, from its net profit and its prices, or (None, why).

    The profit decides; the prices must agree with it, or the result is not taken.
    """
    profit = _number(deal.get("profit"))
    command = deal.get("command")
    open_price, close_price = _number(deal.get("openPrice")), _number(deal.get("closePrice"))
    if profit is None or command not in COMMAND_OF:
        return None, "o fechamento não tem o lucro ou a direção da ordem"
    result = "WIN" if profit > 0 else "LOSS" if profit < 0 else "DRAW"
    if open_price is not None and close_price:
        moved = (close_price > open_price) - (close_price < open_price)
        expected = {1: "WIN", -1: "LOSS", 0: "DRAW"}[moved if command == 0 else -moved]
        if expected != result:
            return None, (
                f"o lucro {profit:g} diz {result}, mas a abertura {open_price:g} e o fechamento"
                f" {close_price:g} dizem {expected}"
            )
    return result, None


def describe_order(data) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    return (
        f"{data.get('asset')} {COMMAND_OF.get(data.get('command'), data.get('action'))} {data.get('amount')}"
        f" id {data.get('id')}"
    )
