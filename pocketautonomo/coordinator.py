import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from pocketautonomo.cycle import RESULTS, UNKNOWN, Cycle, Entry


class CoordinatorError(Exception):
    """The coordinator's current state does not allow the operation."""


class UnknownReservationError(CoordinatorError):
    """The reservation id was not issued by this coordinator."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    cycle_id: int
    entry: Entry

    @property
    def attempt(self):
        return self.entry.attempt


@dataclass(frozen=True)
class Status:
    """Consistent snapshot of a coordinator; every field is empty before the first cycle."""

    cycle_id: Optional[int] = None
    asset: Optional[str] = None
    direction: Optional[str] = None
    attempt: Optional[int] = None
    outcome: Optional[str] = None
    is_active: bool = False
    is_blocked: bool = False
    pending: Optional[Reservation] = None

    @property
    def can_start_cycle(self):
        """A new cycle needs the previous one ended and no reservation pending."""
        return not self.is_active and self.pending is None


class Coordinator:
    """Runs one cycle at a time and hands out its entries as reservations.

    Every operation runs under one lock, so simultaneous callers can never get two
    reservations, start two cycles or apply the same result twice.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cycle = None
        self._cycle_id = 0
        self._pending = None
        self._resolved = {}  # reservation_id -> definitive result (WIN or LOSS)

    def start_cycle(self, asset, direction):
        """Start a new independent cycle at attempt 1 and return its id."""
        with self._lock:
            if not self._status().can_start_cycle:
                raise CoordinatorError(
                    "a new cycle needs the previous one ended and no reservation pending"
                )
            cycle = Cycle(asset, direction)
            self._cycle_id += 1
            self._cycle = cycle
            return self._cycle_id

    def reserve_next_entry(self, balance_cents):
        """Reserve the next entry of the current cycle.

        Returns a Reservation with a unique reservation_id, or None when no entry is
        available: no cycle, a reservation still pending, the cycle has ended, or the
        balance does not cover the stake.
        """
        if isinstance(balance_cents, bool) or not isinstance(balance_cents, int):
            raise ValueError("balance_cents must be an integer")
        with self._lock:
            if self._cycle is None or self._pending is not None:
                return None
            entry = self._cycle.next_entry(balance_cents)
            if entry is None:
                return None
            self._pending = Reservation(uuid.uuid4().hex, self._cycle_id, entry)
            return self._pending

    def record_result(self, reservation_id, result):
        """Record the result of a reservation. Returns True if the state changed.

        Only the pending reservation can change the cycle. UNKNOWN keeps it pending
        until WIN or LOSS arrives for the same id; repeating the result of a resolved
        reservation returns False and changes nothing.
        """
        if not isinstance(reservation_id, str):
            raise ValueError("reservation_id must be a string")
        if result not in RESULTS:
            raise ValueError("result must be WIN, LOSS or UNKNOWN")
        with self._lock:
            pending = self._pending
            if pending is not None and reservation_id == pending.reservation_id:
                changed = self._cycle.record_result(pending.attempt, result)
                if result != UNKNOWN:
                    self._resolved[reservation_id] = result
                    self._pending = None
                return changed
            recorded = self._resolved.get(reservation_id)
            if recorded is None:
                raise UnknownReservationError(f"unknown reservation id {reservation_id!r}")
            if result in (recorded, UNKNOWN):
                return False  # duplicate or late result: never advances again
            raise ValueError(
                f"conflicting result for reservation {reservation_id}: {recorded} then {result}"
            )

    def status(self):
        """Consistent snapshot of the current state."""
        with self._lock:
            return self._status()

    def _status(self):
        cycle = self._cycle
        if cycle is None:
            return Status()
        return Status(
            cycle_id=self._cycle_id,
            asset=cycle.asset,
            direction=cycle.direction,
            attempt=cycle.attempt,
            outcome=cycle.outcome,
            is_active=cycle.is_active,
            is_blocked=cycle.is_blocked,
            pending=self._pending,
        )
