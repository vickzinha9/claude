import copy
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from pocketautonomo.cycle import ABANDONED, RESULTS, UNKNOWN, Cycle, Entry
from pocketautonomo.storage import StorageError


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
    """Consistent snapshot of a coordinator; every field is empty before the first cycle.

    recovery_pending is True while the current cycle was restored still open. Its
    decision predates the restart, so it releases no entry and has to end, through the
    result of its pending reservation or abandon_cycle(), before a new cycle starts.
    """

    cycle_id: Optional[int] = None
    asset: Optional[str] = None
    direction: Optional[str] = None
    attempt: Optional[int] = None
    outcome: Optional[str] = None
    is_active: bool = False
    is_blocked: bool = False
    pending: Optional[Reservation] = None
    is_paused: bool = False
    recovery_pending: bool = False

    @property
    def can_start_cycle(self):
        """A new cycle needs no pause, the previous one ended and no reservation pending."""
        return not self.is_paused and not self.is_active and self.pending is None


class Coordinator:
    """Runs one cycle at a time and hands out its entries as reservations.

    Every operation runs under one lock, so simultaneous callers can never get two
    reservations, start two cycles or apply the same result twice.

    With a store, every change is saved before it takes effect, so a reservation is
    already saved when it is returned. If saving fails, StorageError is raised and
    nothing changes. A coordinator with a store restores the saved state and starts
    paused: results are accepted, but no cycle starts and no entry is released until
    resume() is called. A restored pending reservation is never handed out again, and
    a cycle restored still open never releases another entry, not even after resume().
    """

    def __init__(self, store=None):
        self._lock = threading.Lock()
        self._store = store
        self._cycle = None
        self._cycle_id = 0
        self._pending = None
        self._resolved = {}  # reservation_id -> definitive result
        self._paused = False
        self._stale_cycle_id = None  # cycle restored still open: its decision has expired
        if store is not None:
            self._restore(*store.load())
            self._paused = True
            if self._cycle is not None and self._cycle.is_active:
                self._stale_cycle_id = self._cycle_id

    def resume(self):
        """Allow new cycles and entries again. A restored open cycle stays stale."""
        with self._lock:
            self._paused = False

    def start_cycle(self, asset, direction):
        """Start a new independent cycle at attempt 1 and return its id."""
        with self._lock:
            if self._paused:
                raise CoordinatorError("the coordinator is paused; call resume() first")
            if self._recovery_pending():
                raise CoordinatorError(
                    "the cycle restored from before the restart is still open; "
                    "record its pending result or call abandon_cycle() first"
                )
            if not self._status().can_start_cycle:
                raise CoordinatorError(
                    "a new cycle needs the previous one ended and no reservation pending"
                )
            cycle = Cycle(asset, direction)
            cycle_id = self._cycle_id + 1
            if self._store is not None:
                self._store.save_cycle(cycle_id, asset, direction)
            self._cycle, self._cycle_id = cycle, cycle_id
            return cycle_id

    def reserve_next_entry(self, balance_cents):
        """Reserve the next entry of the current cycle.

        Returns a Reservation with a unique reservation_id, or None when no entry is
        available: paused, recovery pending, no cycle, a reservation still pending, the
        cycle has ended, or the balance does not cover the stake.
        """
        if isinstance(balance_cents, bool) or not isinstance(balance_cents, int):
            raise ValueError("balance_cents must be an integer")
        with self._lock:
            if self._paused or self._cycle is None or self._pending is not None:
                return None
            if self._recovery_pending():
                return None
            entry = self._cycle.next_entry(balance_cents)
            if entry is None:
                return None
            reservation = Reservation(uuid.uuid4().hex, self._cycle_id, entry)
            if self._store is not None:
                self._store.save_reservation(reservation)  # committed before returning
            self._pending = reservation
            return reservation

    def record_result(self, reservation_id, result):
        """Record the result of a reservation. Returns True if the state changed.

        Only the pending reservation can change the cycle. UNKNOWN keeps it pending
        until a definitive result arrives for the same id; repeating the result of a
        resolved reservation returns False and changes nothing.
        """
        if not isinstance(reservation_id, str):
            raise ValueError("reservation_id must be a string")
        if result not in RESULTS:
            raise ValueError("result must be one of " + ", ".join(RESULTS))
        with self._lock:
            pending = self._pending
            if pending is not None and reservation_id == pending.reservation_id:
                cycle = copy.deepcopy(self._cycle)  # becomes current only once saved
                changed = cycle.record_result(pending.attempt, result)
                if changed and self._store is not None:
                    self._store.save_result(pending, result, cycle.outcome)
                self._cycle = cycle
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

    def abandon_cycle(self):
        """End the open cycle without a bet result. Allowed while paused.

        Refused while a reservation is pending, including after UNKNOWN. No attempt
        receives a result: the cycle outcome becomes ABANDONED. With a store, the
        abandonment is saved before it takes effect.
        """
        with self._lock:
            if self._cycle is None or not self._cycle.is_active:
                raise CoordinatorError("there is no open cycle to abandon")
            if self._pending is not None:
                raise CoordinatorError(
                    f"reservation {self._pending.reservation_id} is still pending; "
                    "record its definitive result before abandoning the cycle"
                )
            cycle = copy.deepcopy(self._cycle)  # becomes current only once saved
            cycle.abandon()
            if self._store is not None:
                self._store.save_abandonment(self._cycle_id)
            self._cycle = cycle

    def status(self):
        """Consistent snapshot of the current state."""
        with self._lock:
            return self._status()

    def _status(self):
        cycle = self._cycle
        if cycle is None:
            return Status(is_paused=self._paused)
        return Status(
            cycle_id=self._cycle_id,
            asset=cycle.asset,
            direction=cycle.direction,
            attempt=cycle.attempt,
            outcome=cycle.outcome,
            is_active=cycle.is_active,
            is_blocked=cycle.is_blocked,
            pending=self._pending,
            is_paused=self._paused,
            recovery_pending=self._recovery_pending(),
        )

    def _recovery_pending(self):
        return self._stale_cycle_id == self._cycle_id and self._cycle.is_active

    def _restore(self, cycles, reservations):
        try:
            self._replay(cycles, reservations)
        except ValueError as error:
            raise StorageError(f"inconsistent saved state: {error}") from error

    def _replay(self, cycles, reservations):
        """Rebuild the saved state by replaying every saved result through Cycle."""
        saved = {}
        for reservation_id, cycle_id, attempt, stake_cents, result in reservations:
            saved.setdefault(cycle_id, []).append((reservation_id, attempt, stake_cents, result))
        for cycle_id, asset, direction, outcome in cycles:
            if self._cycle is not None and self._cycle.is_active:
                raise ValueError(f"cycle {self._cycle_id} is still active")
            cycle = Cycle(asset, direction)
            for reservation_id, attempt, stake_cents, result in saved.pop(cycle_id, ()):
                if result is None or result == UNKNOWN:  # still pending
                    if self._pending is not None or attempt != cycle.attempt:
                        raise ValueError(f"reservation {reservation_id} cannot be pending")
                    entry = Entry(asset, direction, attempt, stake_cents)
                    self._pending = Reservation(reservation_id, cycle_id, entry)
                    if result == UNKNOWN:
                        cycle.record_result(attempt, UNKNOWN)
                else:
                    cycle.record_result(attempt, result)
                    self._resolved[reservation_id] = result
            if outcome == ABANDONED:
                if self._pending is not None:
                    raise ValueError(f"cycle {cycle_id} was abandoned with a reservation pending")
                cycle.abandon()
            if cycle.outcome != outcome:
                raise ValueError(f"cycle {cycle_id} outcome {outcome} does not match its results")
            self._cycle, self._cycle_id = cycle, cycle_id
        if saved:
            raise ValueError(f"reservations without a saved cycle: {sorted(saved)}")
