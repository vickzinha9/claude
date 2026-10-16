"""A paper broker: simulated orders, settled by the quotes observed on this computer.

SIMULATED EXECUTION ONLY. PaperBroker never reaches the Pocket Option or any other broker:
it sends nothing to a platform and moves no money. It answers through the same Broker
contract that a real adapter will, so that OrderExecutor and the coordinator already run
as they will with real orders.

- Entry: an order is accepted only when a quote of its asset was observed, through
  observe(), at most MAX_QUOTE_AGE_SECONDS before the submission; that quote is the entry.
  Without one, or after the request's deadline, the order is explicitly rejected: a late
  order is never placed.
- Expiry: horizon seconds after the capture of the entry quote, the horizon of the
  analysis' hypothetical operations (300 s by default).
- Settlement: the first quote of the asset observed at or after the expiry, at most
  analysis.tolerance(horizon) seconds later, settles it, as the analysis labels its
  hypothetical operations: above the entry, CALL wins and PUT loses; below, the opposite;
  equal, DRAW. When that time passes without such a quote, the order is CANCELLED with the
  reason NO_QUOTE_AT_EXPIRY: no result is made up.

Every order and every rejection is committed to the broker's own SQLite file before
submit_order() returns, so a restart finds it: get_order() answers by the order id, and
find_order_for() by the reservation id, the lookup that resolves a submission whose
answer was lost (UNKNOWN). confirm_absent() records that a reservation never got an
order, so that none is accepted for it later. The file has an exclusive lock, like the
coordinator's database.

The results are simulated from quotes read on this computer's screen: they prove nothing
about how the Pocket Option settles an order, and they never go to the scan's CSV files,
the only data of the analysis.
"""

import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from pocketautonomo import analysis
from pocketautonomo.broker import Broker, OrderRequest, OrderStatus, SubmissionResponse
from pocketautonomo.cycle import CALL, CANCELLED, DIRECTIONS, DRAW, LOSS, REJECTED, WIN
from pocketautonomo.executor import ORDER_OPEN
from pocketautonomo.storage import (  # the same exclusive lock as the coordinator's database
    DatabaseInUseError,
    StorageError,
    _lock_exclusively,
    _unlock,
)

PAPER_ORDER_PREFIX = "SIM-"  # every simulated order id is this prefix and a uuid4
MAX_QUOTE_AGE_SECONDS = 5.0  # the entry quote is at most this old at the submission
SCHEMA_VERSION = 1
NO_QUOTE_AT_EXPIRY = "sem cotação observada no vencimento"
NEVER_ACCEPTED = "nenhuma ordem simulada foi aceita para esta reserva"
DEADLINE_PASSED = "prazo da entrada vencido"  # the start of the reason of a late order's rejection

_TABLE = """CREATE TABLE paper_orders (
    reservation_id TEXT NOT NULL PRIMARY KEY CHECK (trim(reservation_id) <> ''),
    order_id TEXT UNIQUE CHECK (order_id IS NULL OR trim(order_id) <> ''),
    asset TEXT NOT NULL,
    asset_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    stake_cents INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    state TEXT NOT NULL,
    entry_price REAL,
    entry_quote TEXT,
    entry_captured_at TEXT,
    expires_at TEXT,
    settle_by TEXT,
    exit_price REAL,
    exit_quote TEXT,
    exit_captured_at TEXT,
    settled_at TEXT,
    note TEXT
)"""
_COLUMNS = (
    "reservation_id", "order_id", "asset", "asset_key", "direction", "stake_cents",
    "submitted_at", "state", "entry_price", "entry_quote", "entry_captured_at", "expires_at",
    "settle_by", "exit_price", "exit_quote", "exit_captured_at", "settled_at", "note",
)
_BASE_COLUMNS = ("reservation_id", "asset", "asset_key", "direction", "stake_cents", "submitted_at", "state")
_MOMENTS = ("submitted_at", "entry_captured_at", "expires_at", "settle_by", "exit_captured_at", "settled_at")


class PaperBrokerError(Exception):
    """The simulator refused a request that breaks its rules, such as a second order."""


@dataclass(frozen=True)
class Observation:
    """A quote of an asset observed on this computer: its capture time, in UTC, and value."""

    asset: str
    captured_at: datetime
    price: float
    quote: str


@dataclass(frozen=True)
class PaperOrder:
    """One saved row: a simulated order, or a reservation that never got one (order_id None)."""

    reservation_id: str
    order_id: Optional[str]
    asset: str
    asset_key: str
    direction: str
    stake_cents: int
    submitted_at: datetime
    state: str  # OPEN, WIN, LOSS, DRAW, CANCELLED or, without an order id, REJECTED
    entry_price: Optional[float] = None
    entry_quote: Optional[str] = None
    entry_captured_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    settle_by: Optional[datetime] = None  # the last moment a quote can settle it
    exit_price: Optional[float] = None
    exit_quote: Optional[str] = None
    exit_captured_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    note: Optional[str] = None


def utc_now():
    return datetime.now(timezone.utc)


def settlement(direction, entry, exit_price):
    """WIN, LOSS or DRAW of an order in direction entered at entry and settled at exit_price."""
    if exit_price == entry:
        return DRAW
    return WIN if (exit_price > entry) == (direction == CALL) else LOSS


def _text(moment):
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _clock(moment):
    moment = moment.astimezone(timezone.utc)
    return f"{moment:%H:%M:%S}.{moment.microsecond // 1000:03d} UTC"


def _check_moment(moment, name):
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{name} must be a datetime with a time zone")


def _order(row):
    values = dict(zip(_COLUMNS, row))
    for name in _MOMENTS:
        if values[name] is not None:
            values[name] = datetime.fromisoformat(values[name])
    return PaperOrder(**values)


class PaperBroker(Broker):
    """The simulator behind OrderExecutor: orders saved in path, settled by observe().

    horizon is the duration of every order, in seconds; now is the UTC clock of the
    submissions and of the expiries, the same clock as the observations' capture times.
    """

    def __init__(self, path, horizon=analysis.DEFAULT_HORIZON, now=None):
        if isinstance(horizon, bool) or not isinstance(horizon, (int, float)) or not horizon > 0:
            raise ValueError("horizon must be a positive number of seconds")
        self.path = path
        self.horizon = float(horizon)
        self.tolerance = analysis.tolerance(self.horizon)
        self._now = utc_now if now is None else now
        self._lock = threading.Lock()
        self._latest = {}  # asset key -> the latest Observation
        self._connection = None
        try:
            self._file_lock = _lock_exclusively(path)
        except DatabaseInUseError as error:
            raise DatabaseInUseError(
                f"outra operação simulada já está usando {path}; feche-a antes de abrir de novo"
            ) from error
        try:
            with self._translated(f"opening {path}"):
                self._connection = sqlite3.connect(path, check_same_thread=False)
                self._connection.execute("PRAGMA synchronous = FULL")
            with self._transaction("preparing the simulated orders") as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    connection.execute(_TABLE)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise StorageError(
                        f"{path} has schema version {version}; the simulator supports only"
                        f" {SCHEMA_VERSION}, and the file was left untouched"
                    )
        except BaseException:
            self.close()
            raise

    def close(self):
        """Close the file and release its lock. Closing again does nothing."""
        connection, self._connection = self._connection, None
        descriptor, self._file_lock = getattr(self, "_file_lock", None), None
        try:
            if connection is not None:
                connection.close()
        finally:
            if descriptor is not None:
                _unlock(descriptor)

    def observe(self, asset, captured_at, price, quote=None):
        """Take a quote of asset observed at captured_at; return the orders it settled.

        Every OPEN order of the asset whose settlement time contains captured_at is
        settled with this quote, in one transaction, before this method returns.
        """
        _check_moment(captured_at, "captured_at")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError("price must be a positive, finite number")
        key = analysis.asset_key(asset or "")
        if not key:
            raise ValueError("asset must name an asset")
        price = float(price)
        observation = Observation(asset, captured_at, price, quote if quote is not None else f"{price:g}")
        with self._lock:
            latest = self._latest.get(key)
            if latest is None or captured_at >= latest.captured_at:
                self._latest[key] = observation
            due = [
                order
                for order in self._select("WHERE state = ? AND asset_key = ?", (ORDER_OPEN, key))
                if order.expires_at <= captured_at <= order.settle_by
            ]
            if not due:
                return ()
            settled = []
            now = self._now()
            with self._transaction("settling the simulated orders") as connection:
                for order in due:
                    result = settlement(order.direction, order.entry_price, price)
                    connection.execute(
                        "UPDATE paper_orders SET state = ?, exit_price = ?, exit_quote = ?,"
                        " exit_captured_at = ?, settled_at = ? WHERE reservation_id = ? AND state = ?",
                        (result, price, observation.quote, _text(captured_at), _text(now),
                         order.reservation_id, ORDER_OPEN),
                    )
                    settled.append(order.order_id)
            return tuple(self._by_order_id(order_id) for order_id in settled)

    def latest(self, asset):
        """The latest Observation of asset, or None."""
        with self._lock:
            return self._latest.get(analysis.asset_key(asset or ""))

    def submit_order(self, request):
        """Accept request with the latest quote of its asset, or reject it explicitly.

        A request past its deadline is rejected with a reason that starts with
        DEADLINE_PASSED. The order, or the rejection, is committed before the answer
        returns. A second request for the same reservation raises PaperBrokerError: the
        simulator never accepts two orders for one reservation.
        """
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be an OrderRequest")
        if not isinstance(request.reservation_id, str) or not request.reservation_id.strip():
            raise ValueError("the request needs a reservation id")
        if request.direction not in DIRECTIONS:
            raise ValueError("the direction must be CALL or PUT")
        stake = request.stake_cents
        if isinstance(stake, bool) or not isinstance(stake, int) or stake <= 0:
            raise ValueError("stake_cents must be a positive integer")
        key = analysis.asset_key(request.asset or "")
        if not key:
            raise ValueError("the request needs an asset")
        if request.deadline is not None:
            _check_moment(request.deadline, "deadline")
        with self._lock:
            if self._row(request.reservation_id) is not None:
                raise PaperBrokerError(
                    f"a reserva {request.reservation_id} já passou pelo simulador: ele nunca"
                    " aceita uma segunda ordem para a mesma reserva"
                )
            now = self._now()
            _check_moment(now, "now")
            entry = self._latest.get(key)
            problem = None
            if request.deadline is not None and now > request.deadline:
                late = (now - request.deadline).total_seconds()
                problem = (
                    f"{DEADLINE_PASSED} às {_clock(request.deadline)}, {late:.3f} s antes do pedido"
                    " chegar ao simulador: nenhuma ordem atrasada é aceita"
                )
            elif entry is None:
                problem = f"nenhuma cotação de {request.asset} foi observada neste computador"
            else:
                age = (now - entry.captured_at).total_seconds()
                if age < 0:
                    problem = f"a última cotação de {request.asset} tem horário depois de agora"
                elif age > MAX_QUOTE_AGE_SECONDS:
                    problem = (
                        f"a última cotação de {request.asset} foi capturada há {age:.1f} s, mais"
                        f" que {MAX_QUOTE_AGE_SECONDS:g} s"
                    )
            base = (request.reservation_id, request.asset, key, request.direction, stake, _text(now))
            if problem is not None:
                self._insert(base + (REJECTED,), note=problem)
                return SubmissionResponse.rejected(problem)
            order_id = f"{PAPER_ORDER_PREFIX}{uuid.uuid4()}"
            expires = entry.captured_at + timedelta(seconds=self.horizon)  # as the analysis counts
            self._insert(
                base + (ORDER_OPEN,),
                order_id=order_id,
                entry_price=entry.price,
                entry_quote=entry.quote,
                entry_captured_at=_text(entry.captured_at),
                expires_at=_text(expires),
                settle_by=_text(expires + timedelta(seconds=self.tolerance)),
            )
        return SubmissionResponse.accepted(order_id)

    def get_order(self, broker_order_id):
        """The OrderStatus of exactly broker_order_id; KeyError when it is unknown.

        An OPEN order whose settlement time has passed without a quote becomes CANCELLED
        here, saved before the answer returns.
        """
        with self._lock:
            order = self._by_order_id(broker_order_id)
            now = self._now()
            if order.state == ORDER_OPEN and now > order.settle_by:
                with self._transaction("cancelling an order without a quote at expiry") as connection:
                    connection.execute(
                        "UPDATE paper_orders SET state = ?, settled_at = ?, note = ?"
                        " WHERE order_id = ? AND state = ?",
                        (CANCELLED, _text(now), NO_QUOTE_AT_EXPIRY, broker_order_id, ORDER_OPEN),
                    )
                order = self._by_order_id(broker_order_id)
        return OrderStatus(broker_order_id, order.state)

    def order(self, broker_order_id):
        """The PaperOrder of broker_order_id; KeyError when it is unknown."""
        with self._lock:
            return self._by_order_id(broker_order_id)

    def find_order_for(self, reservation_id):
        """The PaperOrder saved for reservation_id, or None when the simulator never saw it.

        The simulator saves every order before accepting it, so this answer is final: a
        reservation without a row never got a simulated order.
        """
        with self._lock:
            return self._row(reservation_id)

    def confirm_absent(self, request):
        """Record that the reservation of request never got an order; return True.

        Afterwards no order is ever accepted for it. When the reservation already has an
        order, nothing changes and False is returned: look the order up instead.
        """
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be an OrderRequest")
        with self._lock:
            row = self._row(request.reservation_id)
            if row is not None:
                return row.order_id is None
            base = (
                request.reservation_id, request.asset, analysis.asset_key(request.asset or ""),
                request.direction, request.stake_cents, _text(self._now()),
            )
            self._insert(base + (REJECTED,), note=NEVER_ACCEPTED)
            return True

    def orders(self):
        """Every saved row, in the order of the submissions."""
        with self._lock:
            return self._select("ORDER BY submitted_at, reservation_id", ())

    def _row(self, reservation_id):
        found = self._select("WHERE reservation_id = ?", (reservation_id,))
        return found[0] if found else None

    def _by_order_id(self, broker_order_id):
        found = self._select("WHERE order_id = ?", (broker_order_id,)) if isinstance(broker_order_id, str) else ()
        if not found:
            raise KeyError(f"unknown simulated order id {broker_order_id!r}")
        return found[0]

    def _select(self, clause, parameters):
        connection = self._open()
        with self._translated("reading the simulated orders"):
            rows = connection.execute(f"SELECT {', '.join(_COLUMNS)} FROM paper_orders {clause}", parameters).fetchall()
        return [_order(row) for row in rows]

    def _insert(self, base, **values):
        """Insert a row: base holds the values of _BASE_COLUMNS, values the others."""
        names = _BASE_COLUMNS + tuple(values)
        with self._transaction("saving a simulated order") as connection:
            connection.execute(
                f"INSERT INTO paper_orders ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                base + tuple(values.values()),
            )

    def _open(self):
        if self._connection is None:
            raise StorageError("the simulator is closed")
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
