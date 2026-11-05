"""The real execution: orders sent to the Pocket Option account open in the browser.

REAL MONEY. PocketBroker places each entry of the progression on the account that is logged
in the browser window, on the asset on the screen, for 300 seconds, with the stake of the
attempt: R$ 5, R$ 10, R$ 20 or R$ 40 (progression.PROGRESSION_CENTS), nothing else. It answers
through the same Broker contract as the simulator, so OrderExecutor and the Coordinator keep
every rule: one order per reservation at most, a gale only after a LOSS confirmed by the
account, nothing sent again after an uncertain answer.

Before the click, everything must hold, or the order is refused explicitly and nothing is sent:
the execution is armed (armed() says so, read anew for every order); the account on the screen
is the one chosen (real or demo); the asset on the screen is the order's; the time of the order
is 00:05:00 and the amount is the stake, set and read back; the platform's socket is alive; the
balance covers the stake; the deadline of the entry has not passed.

After the click, the platform must answer, and only its answer counts:

- the page sent exactly one openOrder, with this asset, amount, direction, 300 s and account;
- successopenOrder with the same requestId: ACCEPTED, with the platform's order id. The
  balance must then fall by the stake;
- failopenOrder: REJECTED, with the platform's reason;
- nothing sent while the socket was alive: REJECTED, the click sent nothing;
- anything else (no answer, the socket silent): an exception, so that the reservation stays
  pending as UNKNOWN and is never sent again.

The result comes from successcloseOrder of that order id only: WIN when its net profit is
positive, LOSS when negative, DRAW when zero, and only when the open and close prices agree
with it. The quotes read on the screen at the expiry are recorded beside it; a divergence is
written, never used to change the platform's result. Without the result by the expiry plus
SETTLE_GRACE_SECONDS, get_order raises: the operation stops in PAUSA, nothing advances.

A broken expectation after the order was placed (another stake, asset, duration or account in
the platform's messages, a balance that did not fall by the stake) sets fault: the operation
records what happened and stops before any new order.

Every order is saved in its own SQLite file before the click (SENDING) and after each answer.
"""

import json
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from pocketautonomo import analysis
from pocketautonomo.broker import Broker, OrderRequest, OrderStatus, SubmissionResponse
from pocketautonomo.cycle import CALL, DIRECTIONS, DRAW, LOSS, PUT, REJECTED, UNKNOWN, WIN
from pocketautonomo.executor import ORDER_OPEN
from pocketautonomo.paper_broker import DEADLINE_PASSED
from pocketautonomo.pocket_socket import ACTION_OF, COMMAND_OF, order_result, same_asset
from pocketautonomo.progression import PROGRESSION_CENTS
from pocketautonomo.storage import DatabaseInUseError, StorageError, _lock_exclusively, _unlock

REAL = "real"
DEMO = "demo"
ACCOUNTS = (REAL, DEMO)
SENDING = "SENDING"
SCHEMA_VERSION = 1
ORDER_SECONDS = 300
ACCEPT_TIMEOUT_SECONDS = 6.0  # from the click to the platform's answer
SEND_TIMEOUT_SECONDS = 1.5  # from the click to the page's openOrder frame
BALANCE_WAIT_SECONDS = 1.5  # after the acceptance, for the new balance
SOCKET_SILENCE_SECONDS = 3.0  # the socket sends quotes every half second: longer is a dead socket
SETTLE_GRACE_SECONDS = 90.0  # after the expiry, for the result
LATE_ACCEPT_SECONDS = 10.0  # a lookup by reservation waits this long for a late answer
PUMP_MS = 15
DISARMED = "execução real desarmada"
NOT_SENT = "o clique não enviou ordem nenhuma"

_TABLE = """CREATE TABLE real_orders (
    reservation_id TEXT NOT NULL PRIMARY KEY CHECK (trim(reservation_id) <> ''),
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    stake_cents INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    order_id TEXT UNIQUE CHECK (order_id IS NULL OR trim(order_id) <> ''),
    request_id INTEGER,
    accepted_at TEXT,
    expires_at TEXT,
    settle_by TEXT,
    settled_at TEXT,
    prepare_ms REAL,
    submit_ms REAL,
    open_price REAL,
    close_price REAL,
    duration_s REAL,
    payout_percent REAL,
    profit_cents INTEGER,
    balance_before_cents INTEGER,
    balance_after_open_cents INTEGER,
    balance_after_close_cents INTEGER,
    screen_entry_quote TEXT,
    screen_entry_at TEXT,
    screen_exit_quote TEXT,
    screen_exit_at TEXT,
    sent_json TEXT,
    opened_json TEXT,
    closed_json TEXT,
    note TEXT
)"""
_COLUMNS = (
    "reservation_id", "account", "asset", "direction", "stake_cents", "state", "started_at", "order_id",
    "request_id", "accepted_at", "expires_at", "settle_by", "settled_at", "prepare_ms", "submit_ms", "open_price",
    "close_price", "duration_s", "payout_percent", "profit_cents", "balance_before_cents", "balance_after_open_cents",
    "balance_after_close_cents", "screen_entry_quote", "screen_entry_at", "screen_exit_quote", "screen_exit_at",
    "sent_json", "opened_json", "closed_json", "note",
)
_MOMENTS = ("started_at", "accepted_at", "expires_at", "settle_by", "settled_at", "screen_entry_at", "screen_exit_at")


class PocketBrokerError(Exception):
    """The platform's answer breaks the rules, or a lookup cannot be answered safely."""


class SubmissionUncertain(Exception):
    """The order may have reached the account; it is never sent again."""


@dataclass(frozen=True)
class RealOrder:
    """One saved row: an order sent to the account, or a reservation refused before the click."""

    reservation_id: str
    account: str
    asset: str
    direction: str
    stake_cents: int
    state: str  # SENDING, OPEN, WIN, LOSS, DRAW, REJECTED or UNKNOWN
    started_at: datetime
    order_id: Optional[str] = None
    request_id: Optional[int] = None
    accepted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    settle_by: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    prepare_ms: Optional[float] = None
    submit_ms: Optional[float] = None
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    duration_s: Optional[float] = None
    payout_percent: Optional[float] = None
    profit_cents: Optional[int] = None
    balance_before_cents: Optional[int] = None
    balance_after_open_cents: Optional[int] = None
    balance_after_close_cents: Optional[int] = None
    screen_entry_quote: Optional[str] = None
    screen_entry_at: Optional[datetime] = None
    screen_exit_quote: Optional[str] = None
    screen_exit_at: Optional[datetime] = None
    sent_json: Optional[str] = None
    opened_json: Optional[str] = None
    closed_json: Optional[str] = None
    note: Optional[str] = None

    # The names the operation's log reads from an order, as from a simulated one.
    @property
    def entry_quote(self):
        return None if self.open_price is None else f"{self.open_price:g}"

    @property
    def entry_captured_at(self):
        return self.accepted_at

    @property
    def exit_quote(self):
        return None if self.close_price is None else f"{self.close_price:g}"

    @property
    def exit_captured_at(self):
        return self.settled_at


def utc_now():
    return datetime.now(timezone.utc)


def _text(moment):
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _cents(value):
    return None if value is None else int(round(value * 100))


def money_number(text):
    """The number of a money text as the page shows it ('$49,990', 'R$ 1.234,56', '5'), or None."""
    if text is None:
        return None
    cleaned = re.sub(r"[^0-9,.\-]", "", str(text))
    if not re.search(r"\d", cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        cleaned = cleaned.replace(thousands, "").replace(decimal, ".")
    elif "," in cleaned or "." in cleaned:
        mark = "," if "," in cleaned else "."
        head, _, tail = cleaned.rpartition(mark)
        if cleaned.count(mark) == 1 and len(tail) in (1, 2):
            cleaned = head + "." + tail
        elif all(len(part) == 3 for part in cleaned.split(mark)[1:]):
            cleaned = cleaned.replace(mark, "")
        else:
            return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def account_of(label):
    """real or demo from the account label on the screen ('QT Real', 'QT Demo'), or None."""
    words = (label or "").lower()
    if "demo" in words:
        return DEMO
    if "real" in words:
        return REAL
    return None


def stake_text(stake_cents):
    """The amount as it is typed in the panel: 5, 10, 20, 40 (or 7.5 for 750 cents)."""
    whole, cents = divmod(stake_cents, 100)
    return str(whole) if cents == 0 else f"{whole}.{cents:02d}".rstrip("0")


class PocketBroker(Broker):
    """Orders on the account in the browser, saved in path; see the module's docstring.

    session is the TradingSession (panel, set_expiration, set_amount, click_direction, pump),
    monitor the SocketMonitor fed by it. account is REAL or DEMO: an order goes only to that
    account. armed() returns (True, None) or (False, why), read for every order. now is the UTC
    clock, monotonic the duration clock.
    """

    def __init__(
        self, path, *, account, armed, session=None, monitor=None, now=None, monotonic=time.perf_counter,
        horizon=ORDER_SECONDS, accept_timeout=ACCEPT_TIMEOUT_SECONDS, settle_grace=SETTLE_GRACE_SECONDS,
    ):
        if account not in ACCOUNTS:
            raise ValueError("account must be real or demo")
        if horizon != ORDER_SECONDS:
            raise ValueError(f"the real execution sends orders of {ORDER_SECONDS} s only")
        self.path = path
        self.account = account
        self.is_demo = 1 if account == DEMO else 0
        self.armed = armed
        self.session = session
        self.monitor = monitor
        self.horizon = horizon
        self.accept_timeout = accept_timeout
        self.settle_grace = settle_grace
        self._now = utc_now if now is None else now
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._latest = {}  # asset key -> (captured_at, price, quote)
        self._exit = {}  # order id -> (captured_at, quote): the first quote on the screen at or after expiry
        self.fault = None  # why no new order may go out, set after an order broke an expectation
        self.last_ms = {}  # the times of the last submission's steps, for the latency
        self._connection = None
        try:
            self._file_lock = _lock_exclusively(path)
        except DatabaseInUseError as error:
            raise DatabaseInUseError(f"outra execução real já está usando {path}; feche-a antes") from error
        try:
            with self._translated(f"opening {path}"):
                self._connection = sqlite3.connect(path, check_same_thread=False)
                self._connection.execute("PRAGMA synchronous = FULL")
            with self._transaction("preparing the real orders") as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    connection.execute(_TABLE)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise StorageError(f"{path} has schema version {version}; only {SCHEMA_VERSION} is supported")
        except BaseException:
            self.close()
            raise

    def attach(self, session, monitor):
        """The browser session of this run and the monitor of its socket."""
        self.session, self.monitor = session, monitor

    def close(self):
        connection, self._connection = self._connection, None
        descriptor, self._file_lock = getattr(self, "_file_lock", None), None
        try:
            if connection is not None:
                connection.close()
        finally:
            if descriptor is not None:
                _unlock(descriptor)

    # Quotes read on the screen.

    def observe(self, asset, captured_at, price, quote=None):
        """A quote read on the screen: the entry's reference and the expiry's, beside the platform's."""
        key = analysis.asset_key(asset or "")
        if not key or captured_at is None:
            raise ValueError("observe needs an asset and a capture time")
        quote = quote if quote is not None else f"{price:g}"
        with self._lock:
            self._latest[key] = (captured_at, float(price), quote)
            for order in self._select("WHERE state = ? AND order_id IS NOT NULL", (ORDER_OPEN,)):
                if analysis.asset_key(order.asset) == key and order.expires_at is not None:
                    if captured_at >= order.expires_at and order.order_id not in self._exit:
                        self._exit[order.order_id] = (captured_at, quote)
        return ()

    # Preparing the panel before the decision, to send faster.

    def prepare(self, stake_cents):
        """Set the time and the amount of the next order ahead of it; never clicks Buy or Sell."""
        if self.session is None or stake_cents not in PROGRESSION_CENTS:
            return None
        armed, _ = self.armed()
        if not armed:
            return None
        try:
            return self._panel_ready(None, stake_cents)
        except Exception as error:  # noqa: BLE001 - preparing is optional: the order checks again
            return f"preparação do painel falhou: {error}"

    # The Broker contract.

    def submit_order(self, request):
        self._check_request(request)
        with self._lock:
            if self._row(request.reservation_id) is not None:
                raise PocketBrokerError(
                    f"a reserva {request.reservation_id} já passou pela execução real: nunca há segunda ordem"
                )
            started = self._monotonic()
            self.last_ms = {}
            try:
                problem = self._before_click(request)
            except Exception as error:  # noqa: BLE001 - nothing was clicked yet: an explicit refusal
                problem = f"o painel de ordem não ficou pronto, nada foi clicado ({type(error).__name__}: {error})"
            if problem is not None:
                return self._refuse(request, problem)
            prepare_ms = round((self._monotonic() - started) * 1000, 1)
            panel = self._last_panel
            balance = self.monitor.balance(self.is_demo)
            if balance is None:
                balance = money_number(panel.get("balance"))
            if balance is not None and _cents(balance) < request.stake_cents:
                return self._refuse(request, f"saldo {balance:g} não cobre a entrada de {request.stake_cents / 100:g}")
            seen = self._latest.get(analysis.asset_key(request.asset))
            mark = self.monitor.mark()
            self._insert(
                request, SENDING, prepare_ms=prepare_ms, balance_before_cents=_cents(balance),
                screen_entry_quote=seen[2] if seen else None, screen_entry_at=_text(seen[0]) if seen else None,
            )
            click_at = self._monotonic()
            clicked = None
            try:
                self.session.click_direction(request.direction)
            except Exception as error:  # noqa: BLE001 - whether it reached the page is checked below
                clicked = f"{type(error).__name__}: {error}"
            sent = self._wait(lambda: self.monitor.open_request(mark), SEND_TIMEOUT_SECONDS)
            if sent is None:
                if self.monitor.alive(SOCKET_SILENCE_SECONDS):
                    reason = NOT_SENT + (f" (o clique falhou: {clicked})" if clicked else "")
                    self._update(request.reservation_id, state=REJECTED, note=reason)
                    return SubmissionResponse.rejected(reason)
                self._update(request.reservation_id, state=UNKNOWN, note="o socket da plataforma ficou mudo no envio")
                raise SubmissionUncertain("o socket da plataforma ficou mudo no envio: a ordem pode ter saído")
            data = sent.data if isinstance(sent.data, dict) else {}
            request_id = data.get("requestId")
            sent_ms = round((self._monotonic() - click_at) * 1000, 1)
            mismatch = self._sent_mismatch(request, data)
            self._update(request.reservation_id, request_id=request_id, sent_json=json.dumps(data, sort_keys=True))
            if mismatch:
                self._add_fault(f"a página enviou uma ordem diferente da pedida ({mismatch})")
            amount = request.stake_cents / 100
            answer = self._wait(
                lambda: self.monitor.opened(request_id, mark)
                or self.monitor.refused(mark, data.get("asset"), amount, request_id),
                self.accept_timeout,
            )
            if answer is None:
                self._update(request.reservation_id, state=UNKNOWN, note="a plataforma não respondeu ao envio")
                raise SubmissionUncertain(
                    f"a plataforma não respondeu em {self.accept_timeout:g} s ao pedido {request_id}: a ordem pode ter saído"
                )
            if answer.name != "successopenOrder":
                error = (answer.data or {}).get("error") if isinstance(answer.data, dict) else None
                reason = f"a plataforma recusou a ordem: {error or answer.data}"
                self._update(request.reservation_id, state=REJECTED, note=reason)
                return SubmissionResponse.rejected(reason)
            return self._accepted(request, answer.data, mark, click_at, sent_ms)

    def get_order(self, broker_order_id):
        with self._lock:
            order = self._by_order_id(broker_order_id)
            if order.state != ORDER_OPEN:
                return OrderStatus(broker_order_id, order.state)
            deal = self.monitor.close_of(broker_order_id) if self.monitor is not None else None
            if deal is not None:
                return OrderStatus(broker_order_id, self._settle(order, deal))
            if order.settle_by is not None and self._now() > order.settle_by:
                self._update(order.reservation_id, note="o resultado não chegou da plataforma até o prazo")
                raise PocketBrokerError(
                    f"o resultado da ordem {broker_order_id} não chegou da plataforma até"
                    f" {order.settle_by:%H:%M:%S} UTC: nada avança; confira o histórico da conta"
                )
            return OrderStatus(broker_order_id, ORDER_OPEN)

    def order(self, broker_order_id):
        with self._lock:
            return self._by_order_id(broker_order_id)

    def find_order_for(self, reservation_id):
        """The order of reservation_id: saved, or found now by its requestId; None when there is none."""
        with self._lock:
            row = self._row(reservation_id)
            if row is None:
                return None
            if row.order_id is not None or row.state == REJECTED:
                return row  # an order, or a refusal before anything went out: both final
            if row.request_id is None or self.monitor is None or self.session is None:
                return None  # sent or not, unknown: confirm_absent() will not say it was absent
            opened = self._wait(lambda: self.monitor.opened(row.request_id, 0), LATE_ACCEPT_SECONDS)
            if opened is None:
                return None
            request = OrderRequest(row.reservation_id, row.asset, row.direction, row.stake_cents)
            self._accepted(request, opened.data, 0, None, None, late=True)
            return self._row(reservation_id)

    def confirm_absent(self, request):
        """True only when it is certain that no order went out for the reservation of request."""
        with self._lock:
            row = self._row(request.reservation_id)
            if row is None:
                self._insert(request, REJECTED, note="nenhum clique foi feito para esta reserva")
                return True
            if row.order_id is not None:
                return False
            return row.state == REJECTED

    def orders(self):
        with self._lock:
            return self._select("ORDER BY started_at, reservation_id", ())

    # Before the click.

    def _before_click(self, request):
        armed, why = self.armed()
        if not armed:
            return f"{DISARMED}: {why}"
        if self.fault is not None:
            return f"execução parada depois de uma ordem fora do esperado: {self.fault}"
        if request.stake_cents not in PROGRESSION_CENTS:
            return f"entrada de {request.stake_cents} centavos fora da tabela {PROGRESSION_CENTS}"
        if self.session is None or self.monitor is None:
            return "o navegador da execução real não está ligado"
        late = self._late(request)
        if late:
            return late
        problem = self._panel_ready(request.asset, request.stake_cents)
        if problem is not None:
            return problem
        if not self.monitor.alive(SOCKET_SILENCE_SECONDS):
            return "o socket da plataforma está mudo: sem ele não há como confirmar a ordem"
        return self._late(request)

    def _late(self, request):
        now = self._now()
        if request.deadline is not None and now > request.deadline:
            late = (now - request.deadline).total_seconds()
            return f"{DEADLINE_PASSED} há {late:.3f} s: nenhuma ordem atrasada é enviada"
        return None

    def _panel_ready(self, asset, stake_cents):
        """None when the panel shows the account, the asset, 00:05:00 and the stake; else why not."""
        from pocketautonomo.trading_session import FIVE_MINUTES

        start = self._monotonic()
        panel = self._last_panel = self.session.panel()
        if account_of(panel.get("account")) != self.account:
            return f"a conta na tela é {panel.get('account')!r}, e a execução é na conta {self.account}"
        if asset is not None and analysis.asset_key(panel.get("asset") or "") != analysis.asset_key(asset):
            return f"o ativo na tela é {panel.get('asset')!r}, e a ordem é de {asset}"
        if panel.get("covering"):
            return "o tutorial da plataforma cobre a tela: feche-o no navegador; nada foi clicado"
        if panel.get("call") != 1 or panel.get("put") != 1:
            return f"os botões de compra e venda não estão únicos na tela ({panel.get('call')} e {panel.get('put')})"
        if panel.get("expiration") != FIVE_MINUTES:
            self.session.set_expiration()
            panel = self._last_panel = self.session.panel()
            if panel.get("expiration") != FIVE_MINUTES:
                return f"o tempo da ordem ficou {panel.get('expiration')!r}, e não {FIVE_MINUTES}"
        if _cents(money_number(panel.get("amount"))) != stake_cents:
            self.session.set_amount(stake_text(stake_cents))
            panel = self._last_panel = self.session.panel()
            if _cents(money_number(panel.get("amount"))) != stake_cents:
                return f"o valor da ordem ficou {panel.get('amount')!r}, e não {stake_text(stake_cents)}"
            if panel.get("expiration") != FIVE_MINUTES:
                return f"o tempo da ordem mudou para {panel.get('expiration')!r}"
        self.last_ms["panel_ms"] = round((self._monotonic() - start) * 1000, 1)
        return None

    _last_panel = {}

    # After the click.

    def _sent_mismatch(self, request, data):
        problems = []
        if not same_asset(request.asset, data.get("asset")):
            problems.append(f"ativo {data.get('asset')}")
        amount = data.get("amount")
        if not isinstance(amount, (int, float)) or _cents(amount) != request.stake_cents:
            problems.append(f"valor {amount}")
        if data.get("action") != ACTION_OF[request.direction]:
            problems.append(f"direção {data.get('action')}")
        if data.get("time") != self.horizon:
            problems.append(f"tempo {data.get('time')}")
        if data.get("isDemo") != self.is_demo:
            problems.append(f"conta isDemo={data.get('isDemo')}")
        return ", ".join(problems)

    def _accepted(self, request, data, mark, click_at, sent_ms, late=False):
        data = data if isinstance(data, dict) else {}
        order_id = data.get("id")
        if not isinstance(order_id, str) or not order_id.strip():
            self._update(request.reservation_id, state=UNKNOWN, note="aceite sem id de ordem")
            raise SubmissionUncertain("a plataforma aceitou sem dar o id da ordem")
        accepted_at = self._now()
        submit_ms = None if click_at is None else round((self._monotonic() - click_at) * 1000, 1)
        problems = []
        if COMMAND_OF.get(data.get("command")) != request.direction:
            problems.append(f"direção {data.get('command')}")
        if _cents(data.get("amount") if isinstance(data.get("amount"), (int, float)) else None) != request.stake_cents:
            problems.append(f"valor {data.get('amount')}")
        if not same_asset(request.asset, data.get("asset")):
            problems.append(f"ativo {data.get('asset')}")
        if data.get("isDemo") != self.is_demo:
            problems.append(f"conta isDemo={data.get('isDemo')}")
        opened_at, closes_at = data.get("openTimestamp"), data.get("closeTimestamp")
        duration = closes_at - opened_at if isinstance(opened_at, (int, float)) and isinstance(closes_at, (int, float)) else None
        if duration is None or abs(duration - self.horizon) > 1:
            problems.append(f"duração {duration}")
            duration = float(self.horizon)
        expires = accepted_at + timedelta(seconds=duration)
        balance_after = None
        if not late:
            balance_after = self._wait(lambda: self.monitor.balance_after(self.is_demo, mark), BALANCE_WAIT_SECONDS)
        row = self._row(request.reservation_id)
        notes = []
        if problems:
            notes.append(self._add_fault(f"a ordem aceita {order_id} veio diferente da pedida ({', '.join(problems)})"))
        if not late and row.balance_before_cents is not None:
            if balance_after is None:
                notes.append("o saldo novo não chegou depois do aceite")
            elif _cents(balance_after) != row.balance_before_cents - request.stake_cents:
                notes.append(self._add_fault(
                    f"o saldo foi de {row.balance_before_cents / 100:g} para {balance_after:g} com a ordem {order_id},"
                    f" e não caiu exatamente {request.stake_cents / 100:g}"
                ))
        payout = data.get("percentProfit")
        self._update(
            request.reservation_id,
            state=ORDER_OPEN, order_id=order_id, accepted_at=_text(accepted_at), expires_at=_text(expires),
            settle_by=_text(expires + timedelta(seconds=self.settle_grace)), submit_ms=submit_ms,
            open_price=data.get("openPrice") if isinstance(data.get("openPrice"), (int, float)) else None,
            duration_s=duration, payout_percent=payout if isinstance(payout, (int, float)) else None,
            balance_after_open_cents=_cents(balance_after), opened_json=json.dumps(data, sort_keys=True),
            note="; ".join(notes) or ("aceite achado depois, pelo requestId" if late else None),
        )
        if submit_ms is not None:
            self.last_ms.update(sent_ms=sent_ms, accept_ms=submit_ms)
        return SubmissionResponse.accepted(order_id)

    def _settle(self, order, deal):
        result, problem = order_result(deal)
        if result is None:
            self._update(order.reservation_id, note=problem, closed_json=json.dumps(deal, sort_keys=True))
            raise PocketBrokerError(f"o resultado da ordem {order.order_id} é inconsistente: {problem}")
        amount = deal.get("amount")
        if isinstance(amount, (int, float)) and _cents(amount) != order.stake_cents:
            raise PocketBrokerError(f"o fechamento da ordem {order.order_id} traz o valor {amount}")
        notes = [order.note] if order.note else []
        exit_seen = self._exit.get(order.order_id)
        if exit_seen is not None and deal.get("closePrice"):
            close = deal["closePrice"]
            try:
                screen = float(str(exit_seen[1]).replace(",", "."))
            except ValueError:
                screen = None
            if screen is not None and order.open_price is not None:
                moved = (screen > order.open_price) - (screen < order.open_price)
                expected = {1: WIN, -1: LOSS, 0: DRAW}[moved if order.direction == CALL else -moved]
                if expected != result:
                    notes.append(
                        f"divergência: a cotação lida na tela no vencimento ({exit_seen[1]}) indicaria {expected};"
                        f" vale o resultado da conta ({result}, fechamento {close:g})"
                    )
        balance = self.monitor.balance(self.is_demo) if self.monitor is not None else None
        self._update(
            order.reservation_id,
            state=result, settled_at=_text(self._now()),
            close_price=deal.get("closePrice") if isinstance(deal.get("closePrice"), (int, float)) else None,
            profit_cents=_cents(deal.get("profit")), balance_after_close_cents=_cents(balance),
            screen_exit_quote=exit_seen[1] if exit_seen else None,
            screen_exit_at=_text(exit_seen[0]) if exit_seen else None,
            closed_json=json.dumps(deal, sort_keys=True), note="; ".join(notes) or None,
        )
        return result

    # Helpers.

    def _add_fault(self, text):
        """Keep every broken expectation: the first one stops new orders, the others add to it."""
        self.fault = text if self.fault is None else f"{self.fault}; {text}"
        return text

    def _wait(self, found, seconds):
        deadline = self._monotonic() + seconds
        while True:
            value = found()
            if value is not None:
                return value
            if self._monotonic() >= deadline:
                return None
            self.session.pump(PUMP_MS)

    def _refuse(self, request, reason):
        self._insert(request, REJECTED, note=reason)
        return SubmissionResponse.rejected(reason)

    @staticmethod
    def _check_request(request):
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be an OrderRequest")
        if not isinstance(request.reservation_id, str) or not request.reservation_id.strip():
            raise ValueError("the request needs a reservation id")
        if request.direction not in DIRECTIONS:
            raise ValueError("the direction must be CALL or PUT")
        if isinstance(request.stake_cents, bool) or not isinstance(request.stake_cents, int):
            raise ValueError("stake_cents must be an integer")

    def _insert(self, request, state, **values):
        values = dict(values, reservation_id=request.reservation_id, account=self.account, asset=request.asset,
                      direction=request.direction, stake_cents=request.stake_cents, state=state,
                      started_at=_text(self._now()))
        names = tuple(values)
        with self._transaction("saving a real order") as connection:
            connection.execute(
                f"INSERT INTO real_orders ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                tuple(values.values()),
            )

    def _update(self, reservation_id, **values):
        with self._transaction("updating a real order") as connection:
            connection.execute(
                f"UPDATE real_orders SET {', '.join(f'{name} = ?' for name in values)} WHERE reservation_id = ?",
                tuple(values.values()) + (reservation_id,),
            )

    def _row(self, reservation_id):
        found = self._select("WHERE reservation_id = ?", (reservation_id,))
        return found[0] if found else None

    def _by_order_id(self, order_id):
        found = self._select("WHERE order_id = ?", (order_id,)) if isinstance(order_id, str) else ()
        if not found:
            raise KeyError(f"unknown real order id {order_id!r}")
        return found[0]

    def _select(self, clause, parameters):
        connection = self._open()
        with self._translated("reading the real orders"):
            rows = connection.execute(f"SELECT {', '.join(_COLUMNS)} FROM real_orders {clause}", parameters).fetchall()
        return [_order(row) for row in rows]

    def _open(self):
        if self._connection is None:
            raise StorageError("the real orders file is closed")
        return self._connection

    @staticmethod
    @contextmanager
    def _translated(action):
        try:
            yield
        except sqlite3.Error as error:
            raise StorageError(f"{action} failed: {error}") from error

    @contextmanager
    def _transaction(self, action):
        connection = self._open()
        with self._translated(action):
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def _order(row):
    values = dict(zip(_COLUMNS, row))
    for name in _MOMENTS:
        if values[name] is not None:
            values[name] = datetime.fromisoformat(values[name])
    return RealOrder(**values)


def read_orders(path):
    """Every saved real order of path, read-only, without its lock (for the statistics)."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(f"SELECT {', '.join(_COLUMNS)} FROM real_orders ORDER BY started_at").fetchall()
    finally:
        connection.close()
    return [_order(row) for row in rows]


__all__ = ["PocketBroker", "RealOrder", "REAL", "DEMO", "PUT", "CALL", "WIN", "LOSS", "DRAW"]
