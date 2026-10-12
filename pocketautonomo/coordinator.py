import copy
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from pocketautonomo.cycle import ABANDONED, RESULTS, UNKNOWN, Cycle, Entry
from pocketautonomo.storage import StorageError
from pocketautonomo.submission import ACCEPTED, RESERVED, SUBMITTING, UNVERIFIED


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

    submission_state and broker_order_id describe the pending reservation.
    submission_failure holds the storage error that stopped every new submission. It
    stays, even after resume(), until the coordinator is closed and reopened.
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
    submission_state: Optional[str] = None
    broker_order_id: Optional[str] = None
    submission_failure: Optional[str] = None

    @property
    def can_start_cycle(self):
        """A new cycle needs no pause, no submission failure, the previous cycle ended
        and no reservation pending."""
        return (
            not self.is_paused
            and self.submission_failure is None
            and not self.is_active
            and self.pending is None
        )


def _check_reservation_id(reservation_id):
    if not isinstance(reservation_id, str):
        raise ValueError("reservation_id must be a string")


class Coordinator:
    """Runs one cycle at a time and hands out its entries as reservations.

    Every operation runs under one lock, so simultaneous callers can never get two
    reservations, start two cycles, start the same submission twice or apply the same
    result twice.

    A reservation is RESERVED when it is handed out. begin_submission() makes it
    SUBMITTING right before it is sent, and confirm_acceptance() makes it ACCEPTED
    with the broker order id. Only a reservation whose submission started can receive
    a result, and an acceptance never settles the bet.

    With a store, every change is saved before it takes effect, so a reservation is
    already saved when it is returned. If saving fails, StorageError is raised and
    nothing changes. A failure while saving a submission start or an acceptance also
    stops every new submission until the coordinator is closed and reopened.

    A coordinator with a store restores the saved state, including the submission
    state and order id of every reservation, and starts paused: results and
    acceptances are accepted, but no cycle starts and no entry is released until
    resume() is called. A restored reservation is never submitted, and a cycle
    restored still open never releases another entry, not even after resume().

    The coordinator owns its store: close() closes it, and so does a failed restore.
    After close() every operation raises CoordinatorError.
    """

    def __init__(self, store=None):
        self._lock = threading.Lock()
        self._store = store
        self._closed = False
        self._cycle = None
        self._cycle_id = 0
        self._pending = None
        self._resolved = {}  # reservation_id -> definitive result
        self._submissions = {}  # reservation_id -> (submission_state, broker_order_id)
        self._order_owners = {}  # broker_order_id -> reservation_id
        self._restored = frozenset()  # ids of the reservations loaded from the store
        self._submission_failure = None  # storage error that stopped new submissions
        self._paused = False
        self._stale_cycle_id = None  # cycle restored still open: its decision has expired
        if store is not None:
            try:
                self._restore(*store.load(), store.load_submissions())
            except BaseException:
                store.close()  # a refused restore must not keep the database locked
                raise
            self._paused = True
            if self._cycle is not None and self._cycle.is_active:
                self._stale_cycle_id = self._cycle_id

    def close(self):
        """Close the coordinator and its store. Closing again does nothing."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._store is not None:
                self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def resume(self):
        """Allow new cycles and entries again.

        A restored open cycle stays stale, and a submission failure keeps stopping new
        submissions.
        """
        with self._lock:
            self._check_open()
            self._paused = False

    def start_cycle(self, asset, direction):
        """Start a new independent cycle at attempt 1 and return its id."""
        with self._lock:
            self._check_open()
            if self._paused:
                raise CoordinatorError("the coordinator is paused; call resume() first")
            self._check_submissions_allowed()
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
        """Reserve the next entry of the current cycle, as RESERVED.

        Returns a Reservation with a unique reservation_id, or None when no entry is
        available: paused, submissions stopped by a storage failure, recovery pending,
        no cycle, a reservation still pending, the cycle has ended, or the balance
        does not cover the stake.
        """
        if isinstance(balance_cents, bool) or not isinstance(balance_cents, int):
            raise ValueError("balance_cents must be an integer")
        with self._lock:
            self._check_open()
            if self._paused or self._submission_failure is not None:
                return None
            if self._cycle is None or self._pending is not None:
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
            self._submissions[reservation.reservation_id] = (RESERVED, None)
            return reservation

    def begin_submission(self, reservation_id):
        """Mark the pending reservation as SUBMITTING and return it; send it only afterwards.

        Only a RESERVED reservation handed out by this coordinator can start, and only
        once: a repeated or simultaneous call raises CoordinatorError, and so does a
        call while paused, during recovery, after a submission failure or for a
        restored reservation. With a store, SUBMITTING is saved before the reservation
        is returned. If saving fails, StorageError is raised, the reservation stays
        pending and RESERVED, and every new submission is stopped until the
        coordinator is reopened.
        """
        _check_reservation_id(reservation_id)
        with self._lock:
            self._check_open()
            if self._paused:
                raise CoordinatorError("the coordinator is paused; call resume() first")
            self._check_submissions_allowed()
            pending = self._pending_reservation(reservation_id)
            if reservation_id in self._restored:
                raise CoordinatorError(
                    f"reservation {reservation_id} was restored from before the restart;"
                    " a restored reservation is never submitted"
                )
            if self._recovery_pending():
                raise CoordinatorError("the cycle restored from before the restart is still open")
            state, _ = self._submissions[reservation_id]
            if state != RESERVED:
                raise CoordinatorError(
                    f"reservation {reservation_id} is {state}: a submission starts only"
                    f" once, from {RESERVED}"
                )
            if self._store is not None:
                try:
                    self._store.save_submission_start(reservation_id)
                except StorageError as error:
                    self._stop_submissions(error)
                    raise
            self._submissions[reservation_id] = (SUBMITTING, None)
            return pending

    def confirm_acceptance(self, reservation_id, broker_order_id):
        """Link a reservation to the broker order that accepted it. Allowed while paused.

        A SUBMITTING or UNVERIFIED reservation becomes ACCEPTED, also after its result:
        the link is for checking and history, so it never settles the bet, changes a
        result, moves the cycle or causes a submission. Returns True when the link is
        recorded and False when the same acceptance was already recorded. A different
        order id, an order id that belongs to another reservation or a reservation
        still RESERVED raises CoordinatorError. With a store, the link is saved before
        it takes effect. If saving fails, StorageError is raised, nothing changes and
        every new submission is stopped until the coordinator is reopened.
        """
        _check_reservation_id(reservation_id)
        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            raise ValueError("broker_order_id must be a non-empty string")
        with self._lock:
            self._check_open()
            submission = self._submissions.get(reservation_id)
            if submission is None:
                raise UnknownReservationError(f"unknown reservation id {reservation_id!r}")
            state, saved_order_id = submission
            if state == ACCEPTED:
                if saved_order_id == broker_order_id:
                    return False  # the same acceptance again
                raise CoordinatorError(
                    f"reservation {reservation_id} was already accepted as order"
                    f" {saved_order_id}, not {broker_order_id}"
                )
            if state not in (SUBMITTING, UNVERIFIED):
                raise CoordinatorError(
                    f"reservation {reservation_id} is {state}: only {SUBMITTING} or"
                    f" {UNVERIFIED} can be accepted"
                )
            owner = self._order_owners.get(broker_order_id)
            if owner is not None:
                raise CoordinatorError(
                    f"order {broker_order_id} already belongs to reservation {owner}"
                )
            changed = True
            if self._store is not None:
                try:
                    changed = self._store.save_acceptance(reservation_id, broker_order_id)
                except StorageError as error:
                    self._stop_submissions(error)
                    raise
            self._submissions[reservation_id] = (ACCEPTED, broker_order_id)
            self._order_owners[broker_order_id] = reservation_id
            return changed

    def record_result(self, reservation_id, result):
        """Record the result of a reservation. Returns True if the state changed.

        Only the pending reservation can change the cycle, and only once its
        submission started: a reservation still RESERVED raises CoordinatorError.
        UNKNOWN keeps it pending until a definitive result arrives for the same id;
        repeating the result of a resolved reservation returns False and changes
        nothing.
        """
        _check_reservation_id(reservation_id)
        if result not in RESULTS:
            raise ValueError("result must be one of " + ", ".join(RESULTS))
        with self._lock:
            self._check_open()
            pending = self._pending
            if pending is not None and reservation_id == pending.reservation_id:
                if self._submissions[reservation_id][0] == RESERVED:
                    raise CoordinatorError(
                        f"reservation {reservation_id} is still {RESERVED}: it was never"
                        " submitted, so it cannot have a result"
                    )
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
            self._check_open()
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
            self._check_open()
            return self._status()

    def _check_open(self):
        if self._closed:
            raise CoordinatorError("the coordinator is closed")

    def _check_submissions_allowed(self):
        if self._submission_failure is not None:
            raise CoordinatorError(
                "new submissions are stopped after a storage failure"
                f" ({self._submission_failure}); close and reopen the coordinator to"
                " check the saved state"
            )

    def _stop_submissions(self, error):
        if self._submission_failure is None:  # keep the first cause
            self._submission_failure = str(error)

    def _pending_reservation(self, reservation_id):
        pending = self._pending
        if pending is not None and pending.reservation_id == reservation_id:
            return pending
        if reservation_id not in self._submissions:
            raise UnknownReservationError(f"unknown reservation id {reservation_id!r}")
        raise CoordinatorError(f"reservation {reservation_id} is not pending")

    def _status(self):
        cycle = self._cycle
        if cycle is None:
            return Status(is_paused=self._paused, submission_failure=self._submission_failure)
        pending = self._pending
        state, order_id = (
            self._submissions[pending.reservation_id] if pending is not None else (None, None)
        )
        return Status(
            cycle_id=self._cycle_id,
            asset=cycle.asset,
            direction=cycle.direction,
            attempt=cycle.attempt,
            outcome=cycle.outcome,
            is_active=cycle.is_active,
            is_blocked=cycle.is_blocked,
            pending=pending,
            is_paused=self._paused,
            recovery_pending=self._recovery_pending(),
            submission_state=state,
            broker_order_id=order_id,
            submission_failure=self._submission_failure,
        )

    def _recovery_pending(self):
        return self._stale_cycle_id == self._cycle_id and self._cycle.is_active

    def _restore(self, cycles, reservations, submissions):
        try:
            self._replay(cycles, reservations)
            if set(submissions) != {row[0] for row in reservations}:
                raise ValueError("the submissions do not match the reservations")
        except ValueError as error:
            raise StorageError(f"inconsistent saved state: {error}") from error
        self._submissions = dict(submissions)
        self._order_owners = {
            order_id: reservation_id
            for reservation_id, (_, order_id) in submissions.items()
            if order_id is not None
        }
        self._restored = frozenset(submissions)

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
