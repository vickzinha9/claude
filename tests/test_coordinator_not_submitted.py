import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace

from pocketautonomo.coordinator import (
    Coordinator,
    CoordinatorError,
    Reservation,
    UnknownReservationError,
)
from pocketautonomo.cycle import ABANDONED, CALL, LOSS, PUT, RESULTS, UNKNOWN, WIN, Entry
from pocketautonomo.storage import SqliteStore, StorageError, connect_read_only
from pocketautonomo.submission import NOT_SUBMITTED, RESERVED, SUBMITTING

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents
THREADS = 16

# A database written before submissions were tracked: attempt 2 of an open cycle is
# pending without a result, so the migration makes it UNVERIFIED.
LEGACY_DATABASE = """
CREATE TABLE cycles (
    cycle_id INTEGER PRIMARY KEY,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    outcome TEXT
);
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles (cycle_id),
    attempt INTEGER NOT NULL,
    stake_cents INTEGER NOT NULL,
    result TEXT,
    UNIQUE (cycle_id, attempt)
);
INSERT INTO cycles VALUES (1, 'EURUSD_otc', 'CALL', NULL);
INSERT INTO reservations VALUES ('old-1', 1, 1, 500, 'LOSS');
INSERT INTO reservations VALUES ('old-2', 1, 2, 1000, NULL);
"""


def submit(coordinator):
    """Reserve the next entry and start its submission; return the reservation."""
    reservation = coordinator.reserve_next_entry(BALANCE)
    coordinator.begin_submission(reservation.reservation_id)
    return reservation


def assert_closed_reservation_is_refused(test, coordinator, reservation_id):
    """No result, acceptance or submission start is accepted for a closed reservation."""
    for result in RESULTS:
        with test.subTest(result=result):
            with test.assertRaisesRegex(CoordinatorError, "closed as NOT_SUBMITTED"):
                coordinator.record_result(reservation_id, result)
    with test.assertRaisesRegex(CoordinatorError, "closed as NOT_SUBMITTED"):
        coordinator.confirm_acceptance(reservation_id, "ORD-1")
    with test.assertRaisesRegex(CoordinatorError, "closed as NOT_SUBMITTED"):
        coordinator.begin_submission(reservation_id)


def run_together(test, functions):
    """Call each function in its own thread, all released together; return the results."""
    barrier = threading.Barrier(len(functions))
    results = [None] * len(functions)

    def worker(index, function):
        barrier.wait(timeout=5)
        try:
            results[index] = function()
        except Exception as error:  # handed back to the test thread
            results[index] = error

    threads = [
        threading.Thread(target=worker, args=(index, function), daemon=True)
        for index, function in enumerate(functions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        test.assertFalse(thread.is_alive(), "a thread did not finish")
    return results


class SlowStore(SqliteStore):
    """A store whose submission start and closure take a while and are recorded."""

    def __init__(self, path):
        super().__init__(path)
        self.writes = []

    def save_submission_start(self, reservation_id):
        self.writes.append("save_submission_start")
        time.sleep(0.01)  # widen the window between the checks and the memory update
        return super().save_submission_start(reservation_id)

    def save_not_submitted(self, reservation_id):
        self.writes.append("save_not_submitted")
        time.sleep(0.01)
        return super().save_not_submitted(reservation_id)


class StartSavedThenFails(SqliteStore):
    """A store that commits the submission start but reports a failure."""

    def save_submission_start(self, reservation_id):
        super().save_submission_start(reservation_id)
        raise StorageError("simulated failure after the commit")


class WithoutStoreTest(unittest.TestCase):
    def test_closure_without_a_store_works_the_same(self):
        coordinator = Coordinator()
        coordinator.start_cycle(ASSET, CALL)
        lost = submit(coordinator)
        coordinator.record_result(lost.reservation_id, LOSS)
        reserved = coordinator.reserve_next_entry(BALANCE)  # attempt 2, never submitted
        with self.assertRaisesRegex(CoordinatorError, "is not pending"):
            coordinator.close_not_submitted(lost.reservation_id)
        foreign = Coordinator()
        foreign.start_cycle(ASSET, CALL)
        for unknown in ("unknown-id", foreign.reserve_next_entry(BALANCE).reservation_id):
            with self.assertRaises(UnknownReservationError):
                coordinator.close_not_submitted(unknown)
        with self.assertRaises(ValueError):
            coordinator.close_not_submitted(None)

        self.assertTrue(coordinator.close_not_submitted(reserved.reservation_id))
        status = coordinator.status()
        self.assertEqual(
            (status.outcome, status.is_active, status.attempt, status.pending),
            (ABANDONED, False, 2, None),
        )
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))  # no gale of this cycle
        self.assertFalse(coordinator.close_not_submitted(reserved.reservation_id))
        assert_closed_reservation_is_refused(self, coordinator, reserved.reservation_id)
        self.assertEqual(coordinator.status(), status)
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry, Entry(ASSET, PUT, 1, 500))


class StoreTestCase(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "state.db")

    def open(self, store=None):
        coordinator = Coordinator(store if store is not None else SqliteStore(self.path))
        self.addCleanup(coordinator.close)
        return coordinator

    def running(self, store=None):
        """A resumed coordinator with an open cycle."""
        coordinator = self.open(store)
        coordinator.resume()
        coordinator.start_cycle(ASSET, CALL)
        return coordinator

    def restart(self, previous):
        """Close the previous instance, then open the database again."""
        previous.close()
        return self.open()

    def query(self, sql, parameters=()):
        """Read committed rows through an independent read-only connection."""
        with closing(connect_read_only(self.path)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def execute(self, sql, parameters=()):
        """Write through a raw connection that bypasses the store."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def rows(self):
        """Every saved row of both tables."""
        return (
            self.query("SELECT * FROM cycles ORDER BY cycle_id"),
            self.query("SELECT * FROM reservations ORDER BY cycle_id, attempt"),
        )

    def submission(self, reservation_id):
        return self.query(
            "SELECT submission_state, broker_order_id FROM reservations"
            " WHERE reservation_id = ?",
            (reservation_id,),
        )[0]

    def fail_updates(self, table):
        """Make every UPDATE on table fail inside SQLite; return the undo function."""
        trigger = f"fail_update_{table}"
        self.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table}"
            " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        return lambda: self.execute(f"DROP TRIGGER {trigger}")


class CloseTest(StoreTestCase):
    def test_closure_ends_the_cycle_without_a_result_or_a_gale(self):
        coordinator = self.running()
        lost = submit(coordinator)
        coordinator.record_result(lost.reservation_id, LOSS)
        reserved = coordinator.reserve_next_entry(BALANCE)  # attempt 2, never submitted
        self.assertTrue(coordinator.close_not_submitted(reserved.reservation_id))
        status = coordinator.status()
        self.assertEqual(
            (status.outcome, status.is_active, status.attempt, status.pending,
             status.submission_state, status.submission_failure),
            (ABANDONED, False, 2, None, None, None),
        )
        self.assertEqual(
            self.query(
                "SELECT reservation_id, attempt, result, submission_state FROM reservations"
                " ORDER BY attempt"
            ),
            [(lost.reservation_id, 1, LOSS, SUBMITTING),
             (reserved.reservation_id, 2, None, NOT_SUBMITTED)],
        )
        self.assertEqual(self.query("SELECT outcome FROM cycles"), [(ABANDONED,)])
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))  # no gale of this cycle
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry, Entry(ASSET, PUT, 1, 500))

    def test_restore_after_a_closure_keeps_results_links_and_the_pause(self):
        before = self.running()
        accepted = submit(before)
        before.confirm_acceptance(accepted.reservation_id, "ORD-1")
        before.record_result(accepted.reservation_id, LOSS)
        reserved = before.reserve_next_entry(BALANCE)
        before.close_not_submitted(reserved.reservation_id)
        closed = before.status()

        after = self.restart(before)
        status = after.status()
        self.assertEqual(status, replace(closed, is_paused=True))  # nothing pending
        self.assertFalse(status.recovery_pending)
        self.assertFalse(after.close_not_submitted(reserved.reservation_id))
        self.assertFalse(after.record_result(accepted.reservation_id, LOSS))  # result kept
        self.assertFalse(after.confirm_acceptance(accepted.reservation_id, "ORD-1"))  # link kept
        with self.assertRaisesRegex(CoordinatorError, "already accepted as order ORD-1"):
            after.confirm_acceptance(accepted.reservation_id, "ORD-2")
        self.assertEqual(after.status(), status)
        with self.assertRaisesRegex(CoordinatorError, "paused"):
            after.start_cycle(ASSET, PUT)
        after.resume()
        self.assertEqual(after.start_cycle(ASSET, PUT), 2)

    def test_restored_reserved_reservation_is_closed_while_paused(self):
        before = self.running()
        pending = before.reserve_next_entry(BALANCE)  # the process stops before submitting
        after = self.restart(before)
        status = after.status()
        self.assertEqual(
            (status.is_paused, status.recovery_pending, status.pending, status.submission_state),
            (True, True, pending, RESERVED),
        )
        with self.assertRaisesRegex(CoordinatorError, "close_not_submitted"):
            after.abandon_cycle()  # still pending: the error points to the closure
        self.assertTrue(after.close_not_submitted(pending.reservation_id))  # paused, recovery
        status = after.status()
        self.assertEqual(
            (status.is_paused, status.recovery_pending, status.pending, status.outcome,
             status.attempt),
            (True, False, None, ABANDONED, 1),
        )
        self.assertEqual(self.submission(pending.reservation_id), (NOT_SUBMITTED, None))
        after.resume()
        with self.assertRaisesRegex(CoordinatorError, "closed as NOT_SUBMITTED"):
            after.begin_submission(pending.reservation_id)
        self.assertEqual(after.start_cycle(ASSET, CALL), 2)


class RefusalTest(StoreTestCase):
    def submitting(self):
        coordinator = self.running()
        return coordinator, submit(coordinator).reservation_id

    def timed_out(self):
        coordinator, reservation_id = self.submitting()
        coordinator.record_result(reservation_id, UNKNOWN)  # timeout or network error
        return coordinator, reservation_id

    def accepted(self):
        coordinator, reservation_id = self.submitting()
        coordinator.confirm_acceptance(reservation_id, "ORD-1")
        return coordinator, reservation_id

    def unverified(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(LEGACY_DATABASE)
        return self.open(), "old-2"

    def reserved_with_unknown(self):
        before = self.running()
        reservation_id = before.reserve_next_entry(BALANCE).reservation_id
        before.close()
        # Only older code saved results for a RESERVED reservation.
        self.execute(
            "UPDATE reservations SET result = 'UNKNOWN' WHERE reservation_id = ?",
            (reservation_id,),
        )
        return self.open(), reservation_id

    def test_closure_is_refused_when_the_order_may_have_been_sent(self):
        cases = (
            ("SUBMITTING without order id", self.submitting, "is SUBMITTING"),
            ("SUBMITTING with UNKNOWN", self.timed_out, "is SUBMITTING"),
            ("ACCEPTED", self.accepted, "is ACCEPTED"),
            ("UNVERIFIED", self.unverified, "is UNVERIFIED"),
            ("RESERVED with UNKNOWN", self.reserved_with_unknown, "has an UNKNOWN result"),
        )
        for number, (name, build, message) in enumerate(cases):
            with self.subTest(case=name):
                self.path = os.path.join(self.directory, f"case{number}.db")
                coordinator, reservation_id = build()
                status, rows = coordinator.status(), self.rows()
                with self.assertRaisesRegex(CoordinatorError, message):
                    coordinator.close_not_submitted(reservation_id)
                self.assertEqual(coordinator.status(), status)
                self.assertEqual(self.rows(), rows)

    def test_repeated_closure_does_not_touch_a_new_cycle(self):
        coordinator = self.running()
        closed = coordinator.reserve_next_entry(BALANCE).reservation_id
        self.assertTrue(coordinator.close_not_submitted(closed))
        self.assertFalse(coordinator.close_not_submitted(closed))
        coordinator.start_cycle(ASSET, PUT)
        current = submit(coordinator)
        before, rows = coordinator.status(), self.rows()
        self.assertFalse(coordinator.close_not_submitted(closed))
        assert_closed_reservation_is_refused(self, coordinator, closed)
        with self.assertRaisesRegex(ValueError, "result must be one of"):
            coordinator.record_result(closed, NOT_SUBMITTED)  # not a bet result
        self.assertEqual(coordinator.status(), before)
        self.assertEqual(self.rows(), rows)

        after = self.restart(coordinator)
        restored = after.status()
        self.assertEqual(restored, replace(before, is_paused=True, recovery_pending=True))
        self.assertFalse(after.close_not_submitted(closed))
        after.resume()
        assert_closed_reservation_is_refused(self, after, closed)
        self.assertEqual(after.status(), replace(restored, is_paused=False))
        self.assertEqual(self.rows(), rows)
        self.assertTrue(after.record_result(current.reservation_id, WIN))  # still works


class FailureTest(StoreTestCase):
    def test_closure_after_a_failed_start_needs_a_reopen(self):
        coordinator = self.running()
        reservation = coordinator.reserve_next_entry(BALANCE)
        undo = self.fail_updates("reservations")
        with self.assertRaisesRegex(StorageError, "saving the submission start failed"):
            coordinator.begin_submission(reservation.reservation_id)
        undo()
        failure = coordinator.status().submission_failure
        self.assertIn("simulated write failure", failure)
        with self.assertRaisesRegex(CoordinatorError, "close and reopen the coordinator"):
            coordinator.close_not_submitted(reservation.reservation_id)
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.submission_failure),
            (reservation, RESERVED, failure),  # the refused closure kept the stop
        )
        self.assertEqual(self.submission(reservation.reservation_id), (RESERVED, None))

        reopened = self.restart(coordinator)  # the saved state says RESERVED
        self.assertIsNone(reopened.status().submission_failure)
        self.assertTrue(reopened.close_not_submitted(reservation.reservation_id))
        self.assertEqual(self.submission(reservation.reservation_id), (NOT_SUBMITTED, None))

        # When the failed start was saved anyway, the reopened coordinator sees
        # SUBMITTING and refuses the closure: the order may have been sent.
        self.path = os.path.join(self.directory, "saved-anyway.db")
        coordinator = self.running(StartSavedThenFails(self.path))
        reservation_id = coordinator.reserve_next_entry(BALANCE).reservation_id
        with self.assertRaisesRegex(StorageError, "after the commit"):
            coordinator.begin_submission(reservation_id)
        self.assertEqual(coordinator.status().submission_state, RESERVED)
        with self.assertRaisesRegex(CoordinatorError, "close and reopen the coordinator"):
            coordinator.close_not_submitted(reservation_id)
        reopened = self.restart(coordinator)
        self.assertEqual(reopened.status().submission_state, SUBMITTING)
        with self.assertRaisesRegex(CoordinatorError, "is SUBMITTING"):
            reopened.close_not_submitted(reservation_id)
        self.assertEqual(self.submission(reservation_id), (SUBMITTING, None))

    def test_failed_closure_keeps_the_reservation_and_stops_new_submissions(self):
        coordinator = self.running()
        reservation = coordinator.reserve_next_entry(BALANCE)
        before, rows = coordinator.status(), self.rows()
        undo = self.fail_updates("cycles")
        with self.assertRaisesRegex(StorageError, "saving the NOT_SUBMITTED closure failed"):
            coordinator.close_not_submitted(reservation.reservation_id)
        undo()
        self.assertEqual(self.rows(), rows)  # neither the reservation nor the cycle changed
        status = coordinator.status()
        self.assertIn("simulated write failure", status.submission_failure)
        self.assertEqual(replace(status, submission_failure=None), before)  # memory kept
        with self.assertRaisesRegex(CoordinatorError, "stopped after a storage failure"):
            coordinator.begin_submission(reservation.reservation_id)  # it is never sent now
        with self.assertRaisesRegex(CoordinatorError, "close and reopen the coordinator"):
            coordinator.close_not_submitted(reservation.reservation_id)
        coordinator.resume()  # does not clear the stop
        self.assertEqual(coordinator.status().submission_failure, status.submission_failure)

        reopened = self.restart(coordinator)
        self.assertTrue(reopened.close_not_submitted(reservation.reservation_id))
        status = reopened.status()
        self.assertEqual(
            (status.is_paused, status.outcome, status.pending, status.submission_failure),
            (True, ABANDONED, None, None),
        )
        self.assertEqual(self.submission(reservation.reservation_id), (NOT_SUBMITTED, None))


class ConcurrencyTest(StoreTestCase):
    def test_simultaneous_begin_and_close_have_a_single_winner(self):
        store = SlowStore(self.path)
        coordinator = self.running(store)
        reservation_id = coordinator.reserve_next_entry(BALANCE).reservation_id
        begin = lambda: coordinator.begin_submission(reservation_id)  # noqa: E731
        close = lambda: coordinator.close_not_submitted(reservation_id)  # noqa: E731
        results = run_together(self, [begin, close] * (THREADS // 2))
        begins, closes = results[0::2], results[1::2]

        unexpected = [r for r in results if isinstance(r, Exception)
                      and not isinstance(r, CoordinatorError)]
        self.assertEqual(unexpected, [])
        started = [r for r in begins if isinstance(r, Reservation)]
        self.assertEqual(len(started) + closes.count(True), 1)  # a single winner
        self.assertEqual(len(store.writes), 1)  # and a single write to the database
        if started:
            self.assertTrue(all(isinstance(r, CoordinatorError) for r in closes))
            self.assertEqual(self.submission(reservation_id), (SUBMITTING, None))
            self.assertEqual(coordinator.status().submission_state, SUBMITTING)
        else:
            self.assertEqual(closes.count(False), THREADS // 2 - 1)  # repeated closures
            self.assertTrue(all(isinstance(r, CoordinatorError) for r in begins))
            self.assertEqual(self.submission(reservation_id), (NOT_SUBMITTED, None))
            self.assertEqual(coordinator.status().outcome, ABANDONED)


class RestoreIntegrityTest(StoreTestCase):
    def test_inconsistent_closures_are_refused_on_restore(self):
        tamperings = (
            ("UPDATE reservations SET result = 'LOSS' WHERE submission_state = 'NOT_SUBMITTED'",
             "is NOT_SUBMITTED with result 'LOSS'"),
            ("UPDATE reservations SET broker_order_id = 'ORD-1'"
             " WHERE submission_state = 'NOT_SUBMITTED'",
             "is NOT_SUBMITTED with order id 'ORD-1'"),
            ("UPDATE reservations SET attempt = 3 WHERE submission_state = 'NOT_SUBMITTED'",
             "at attempt 3, but cycle 1 was waiting for attempt 2"),
            ("UPDATE cycles SET outcome = NULL",  # the cycle was reopened
             "must be ABANDONED, not open"),
            ("UPDATE cycles SET outcome = 'WIN'",  # or ended by a bet
             "must be ABANDONED, not WIN"),
            ("INSERT INTO reservations (reservation_id, cycle_id, attempt, stake_cents, result,"
             " submission_state) VALUES ('later', 1, 3, 2000, 'LOSS', 'SUBMITTING')",
             "reservation later comes after"),
        )
        for number, (sql, message) in enumerate(tamperings):
            with self.subTest(sql=sql):
                self.path = os.path.join(self.directory, f"tampered{number}.db")
                coordinator = self.running()
                lost = submit(coordinator)
                coordinator.record_result(lost.reservation_id, LOSS)
                reserved = coordinator.reserve_next_entry(BALANCE)  # attempt 2
                coordinator.close_not_submitted(reserved.reservation_id)
                coordinator.close()
                self.execute(sql)
                with self.assertRaisesRegex(StorageError, "inconsistent saved state") as caught:
                    self.open()
                self.assertIn(message, str(caught.exception))
                SqliteStore(self.path).close()  # the refused restore released the lock


if __name__ == "__main__":
    unittest.main()
