"""The order panel in a real Chromium: TradingSession and PocketBroker on a LOCAL imitation page.

The page, served by the test server on 127.0.0.1 itself (Chromium blocks a WebSocket to the local
network from a page that is not on it), copies the structure of the Pocket
Option's order panel (time with the M5 preset, amount, Buy and Sell, balance, account) and
talks socket.io to a WebSocket server on 127.0.0.1 run by this test, which answers as the
platform does (successopenOrder, successupdateBalance, successcloseOrder, failopenOrder).
Nothing reaches the Pocket Option: passing says that the session types, clicks and reads the
socket as written, not that the platform accepts an order.

Skipped without Playwright, unless POCKETAUTONOMO_REQUIRE_BROWSER_TESTS=1.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pocketautonomo import arming
from pocketautonomo.broker import ORDER_ACCEPTED, ORDER_REJECTED, OrderRequest
from pocketautonomo.cycle import CALL, LOSS, PUT
from pocketautonomo.pocket_broker import REAL, PocketBroker
from pocketautonomo.pocket_socket import SocketMonitor

REQUIRED = os.environ.get("POCKETAUTONOMO_REQUIRE_BROWSER_TESTS") == "1"
try:
    import playwright.sync_api  # noqa: F401

    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

PAGE_PATH = "/cabinet/quick-high-low/"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{background:#1f2232;color:#fff;font:14px sans-serif} .hidden{display:none}
.expiration-inputs-list-modal{position:absolute;top:60px;left:300px;background:#333;padding:8px}
.dops__timeframes-item{display:inline-block;padding:4px;cursor:pointer}
.btn{display:inline-block;padding:10px;margin:4px;cursor:pointer}
</style></head><body>
<div class="balance-info-block"><div class="balance-info-block__label">QT Real</div>
<div class="balance-info-block__balance">R$1,000.00</div></div>
<span class="current-symbol current-symbol_cropped">EUR/USD OTC</span>
<div class="block block--expiration-inputs"><div class="block__control control">
<div class="control__value value value--several-items"><div class="value__val">00:01:00</div></div></div></div>
<div class="block block--bet-amount"><div class="control__value"><div class="value__val">
<input type="text" autocomplete="off" value="10"></div></div></div>
<div class="value__val-start">+92%</div>
<div class="tour-action-buttons-container"><a class="btn btn-call">Buy</a><a class="btn btn-put">Sell</a></div>
<div class="hidden"><a class="btn btn-call js-change-slide">buy</a><a class="btn btn-put js-change-slide">sell</a></div>
<script>
const ws = new WebSocket('ws://127.0.0.1:PORT/socket.io/?EIO=4&transport=websocket');
let requestId = 12252090;
const value = document.querySelector('.block--expiration-inputs .value__val');
const amount = document.querySelector('.block--bet-amount input');
document.querySelector('.block--expiration-inputs .control__value').addEventListener('click', () => {
  const open = document.querySelector('.expiration-inputs-list-modal');
  if (open) { open.remove(); return; }
  const modal = document.createElement('div');
  modal.className = 'drop-down-modal trading-panel-modal expiration-inputs-list-modal';
  modal.innerHTML = '<div class="dops__timeframes">' + ['S3','S15','S30','M1','M3','M5','M30','H1']
    .map(t => '<div class="dops__timeframes-item">' + t + '</div>').join('') + '</div>';
  modal.addEventListener('click', (e) => {
    const t = e.target.textContent.trim();
    const seconds = {S3:3,S15:15,S30:30,M1:60,M3:180,M5:300,M30:1800,H1:3600}[t];
    if (seconds) value.textContent = new Date(seconds * 1000).toISOString().slice(11, 19);
  });
  document.body.appendChild(modal);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { const m = document.querySelector('.expiration-inputs-list-modal'); if (m) m.remove(); }
});
amount.addEventListener('blur', () => { const n = Math.min(20000, Math.max(1, parseFloat(amount.value) || 1)); amount.value = String(n); });
const send = (action) => {
  const [h, m, s] = value.textContent.split(':').map(Number);
  ws.send('42' + JSON.stringify(['openOrder', {asset: 'EURUSD_otc', amount: parseFloat(amount.value), action,
    isDemo: 0, requestId: ++requestId, optionType: 100, time: h * 3600 + m * 60 + s}]));
};
document.querySelector('.tour-action-buttons-container .btn-call').addEventListener('click', () => send('call'));
document.querySelector('.tour-action-buttons-container .btn-put').addEventListener('click', () => send('put'));
</script></body></html>"""


class FakePlatformServer:
    """A WebSocket server on 127.0.0.1 that answers the page as the platform does."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.received = []  # every text frame the page sent
        self.refuse = False
        self.balance = 1000.0
        self.client = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        self.listener.settimeout(0.2)
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except OSError:
                continue
            client.settimeout(2)
            request = b""
            try:
                while b"\r\n\r\n" not in request:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    request += chunk
            except OSError:
                pass
            keys = [
                line.split(":", 1)[1].strip() for line in request.decode("latin-1").split("\r\n")
                if line.lower().startswith("sec-websocket-key")
            ]
            if not keys:  # the page itself, or a preconnection of the browser
                if request.startswith(b"GET "):
                    body = PAGE.replace("PORT", str(self.port)).encode("utf-8")
                    client.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                    )
                client.close()
                continue
            key = keys[0]
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            client.sendall(
                ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            )
            self.client = client
            threading.Thread(target=self.stream, daemon=True).start()
            self.read(client)

    def stream(self):
        while not self.stop.is_set() and self.client is not None:
            self.event("updateStream", [["EURUSD_otc", 1793772466.2, 1.11442]])
            self.stop.wait(0.3)

    def read(self, client):
        client.settimeout(0.2)
        buffer = b""
        while not self.stop.is_set():
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while len(buffer) >= 2:
                length = buffer[1] & 0x7F
                offset = 2
                if length == 126:
                    length, offset = struct.unpack(">H", buffer[2:4])[0], 4
                elif length == 127:
                    length, offset = struct.unpack(">Q", buffer[2:10])[0], 10
                if len(buffer) < offset + 4 + length:
                    break
                opcode = buffer[0] & 0x0F
                mask = buffer[offset:offset + 4]
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(buffer[offset + 4:offset + 4 + length]))
                buffer = buffer[offset + 4 + length:]
                if opcode == 0x1:
                    self.on_text(data.decode("utf-8"))

    def frame(self, payload, binary):
        header = bytearray([0x82 if binary else 0x81])
        if len(payload) < 126:
            header.append(len(payload))
        else:
            header.append(126)
            header += struct.pack(">H", len(payload))
        with self.lock:
            try:
                self.client.sendall(bytes(header) + payload)
            except OSError:
                pass

    def event(self, name, data):
        self.frame(f'451-["{name}",{{"_placeholder":true,"num":0}}]'.encode(), False)
        self.frame(json.dumps(data).encode(), True)

    def on_text(self, text):
        self.received.append(text)
        if not text.startswith('42["openOrder"'):
            return
        order = json.loads(text[2:])[1]
        if self.refuse:
            self.event("failopenOrder", {"asset": order["asset"], "amount": order["amount"], "error": "not_money"})
            return
        self.balance -= order["amount"]
        self.event("successupdateBalance", {"isDemo": 0, "balance": self.balance})
        self.event("successopenOrder", {
            "id": f"ID{order['requestId']}", "openTimestamp": 1793772475, "closeTimestamp": 1793772475 + order["time"],
            "isDemo": 0, "amount": order["amount"], "profit": order["amount"] * 0.92, "percentProfit": 92,
            "openPrice": 1.11451, "closePrice": 0, "command": 0 if order["action"] == "call" else 1,
            "asset": order["asset"], "requestId": order["requestId"], "currency": "BRL",
        })

    def close_order(self, order_id, amount, command, result):
        profit = -amount if result == LOSS else round(amount * 0.92, 2)
        close = 1.11391 if (result == LOSS) == (command == 0) else 1.11491
        self.event("successcloseOrder", {"profit": profit, "deals": [{
            "id": order_id, "amount": amount, "profit": profit, "openPrice": 1.11451, "closePrice": close,
            "command": command, "asset": "EURUSD_otc", "isDemo": 0,
        }]})

    def shutdown(self):
        self.stop.set()
        for closing in (self.client, self.listener):
            try:
                closing.close()
            except (OSError, AttributeError):
                pass


@unittest.skipUnless(HAVE_PLAYWRIGHT or REQUIRED, "Playwright não está instalado neste Python")
class RealTradingPanelTest(unittest.TestCase):
    def setUp(self):
        from pocketautonomo.trading_session import open_trading_session

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.server = FakePlatformServer()
        self.addCleanup(self.server.shutdown)
        self.session = open_trading_session(self.directory / "profile", headless=True)
        self.addCleanup(self.session.close)
        self.monitor = SocketMonitor()
        self.session.attach_monitor(self.monitor)  # before the page opens, as operate does
        self.session.open(f"http://127.0.0.1:{self.server.port}{PAGE_PATH}")
        self.session.pump(800)
        self.switch = arming.ArmSwitch(self.directory, "real")
        self.broker = PocketBroker(
            self.directory / "ordens.db", account=REAL, armed=self.switch, session=self.session, monitor=self.monitor,
        )
        self.addCleanup(self.broker.close)

    def request(self, number, stake, direction=CALL):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        return OrderRequest(f"r{number}", "EUR/USD OTC", direction, stake, deadline)

    def orders_sent(self):
        return [json.loads(text[2:])[1] for text in self.server.received if text.startswith('42["openOrder"')]

    def test_armed_orders_go_out_with_the_level_stake_and_300_seconds_and_disarmed_none(self):
        self.assertEqual(self.broker.submit_order(self.request(0, 500)).status, ORDER_REJECTED)  # not armed yet
        self.assertEqual(self.orders_sent(), [])
        self.switch.arm("teste")
        accepted = []
        for number, stake in enumerate((500, 1000, 2000, 4000), start=1):
            response = self.broker.submit_order(self.request(number, stake, PUT if number % 2 else CALL))
            self.assertEqual(response.status, ORDER_ACCEPTED, response)
            accepted.append(response.broker_order_id)
        sent = self.orders_sent()
        self.assertEqual([order["amount"] for order in sent], [5, 10, 20, 40])
        self.assertEqual({order["time"] for order in sent}, {300})
        self.assertEqual([order["action"] for order in sent], ["put", "call", "put", "call"])
        for order_id in accepted:
            order = self.broker.order(order_id)
            self.assertEqual(order.balance_before_cents - order.stake_cents, order.balance_after_open_cents)
            self.assertLess(order.submit_ms, 3000)
        self.assertIsNone(self.broker.fault)
        first = self.broker.order(accepted[0])
        self.server.close_order(accepted[0], 5, 1, LOSS)
        self.session.pump(500)
        self.assertEqual(self.broker.get_order(accepted[0]).state, LOSS)
        self.assertEqual(self.broker.order(accepted[0]).profit_cents, -500)
        self.assertEqual(first.state, "OPEN")
        self.server.refuse = True
        refused = self.broker.submit_order(self.request(9, 500))
        self.assertEqual(refused.status, ORDER_REJECTED)
        self.assertIn("not_money", refused.reason)
        self.switch.disarm()
        before = len(self.orders_sent())
        self.assertEqual(self.broker.submit_order(self.request(10, 500)).status, ORDER_REJECTED)
        self.assertEqual(len(self.orders_sent()), before)


if __name__ == "__main__":
    unittest.main()
