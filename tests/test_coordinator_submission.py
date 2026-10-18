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
from pocketautonomo.cycle import CALL, LOSS, PUT, RESULTS, WIN, Entry
from pocketautonomo.storage import SqliteStore, StorageError, connect_read_only
from pocketautonomo.submission import ACCEPTED, RESERVED, SUBMITTING, UNVERIFIED

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents
THREADS = 16

# A database written before submissions were tracked: attempt 2 of an open cycle is
# pending with UNKNOWN.
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
INSERT INTO reservations VALUES ('old-2', 1, 2, 1000, 'UNKNOWN');
"""


def started():
    coordinator = Coordinator()
    coordinator.start_cycle(ASSET, CALL)
    return coordinator


def submit(coordinator):
    """Reserve the next entry and start its submission; return the reservation."""
    reservation = coordinator.reserve_next_entry(BALANCE)
    coordinator.begin_submission(reservation.reservation_id)
    return reservation


def run_simultaneously(test, function):
    """Call function from THREADS threads released together; return what each got."""
    barrier = threading.Barrier(THREADS)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait(timeout=5)
        try:
            value = function()
        except Exception as error:  # handed back to the test thread
            value = error
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        test.assertFalse(thread.is_alive(), "a thread did not finish")
    test.assertEqual(len(results), THREADS)
    return results


class SlowStore(SqliteStore):
    """A store whose submission writes take a while and are counted."""

    def __init__(self, path):
        super().__init__(path)
        self.calls = {"save_submission_start": 0, "save_acceptance": 0}

    def save_submission_start(self, reservation_id):
        self.calls["save_submission_start"] += 1
        time.sleep(0.01)  # widen the window between the check and the memory update
        return super().save_submission_start(reservation_id)

    def save_acceptance(self, reservation_id, broker_order_id):
        self.calls["save_acceptance"] += 1
        time.sleep(0.01)
        return super().save_acceptance(reservation_id, broker_order_id)


class BeginSubmissionTest(unittest.TestCase):
    def test_reserved_entry_starts_its_submission_only_once(self):
        coordinator = started()
        reservation = coordinator.reserve_next_entry(BALANCE)
        status = coordinator.status()
        self.assertEqual((status.submission_state, status.broker_order_id), (RESERVED, None))
        self.assertEqual(coordinator.begin_submission(reservation.reservation_id), reservation)
        self.assertEqual(coordinator.status().submission_state, SUBMITTING)
        with self.assertRaisesRegex(CoordinatorError, "starts only once"):
            coordinator.begin_submission(reservation.reservation_id)
        coordinator.confirm_acceptance(reservation.reservation_id, "ORD-1")
        with self.assertRaisesRegex(CoordinatorError, "starts only once"):
            coordinator.begin_submission(reservation.reservation_id)
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (reservation, ACCEPTED, "ORD-1"),
        )

    def test_only_the_pending_reservation_can_start(self):
        coordinator = started()
        settled = submit(coordinator)
        coordinator.record_result(settled.reservation_id, LOSS)
        current = coordinator.reserve_next_entry(BALANCE)
        with self.assertRaisesRegex(CoordinatorError, "is not pending"):
            coordinator.begin_submission(settled.reservation_id)
        foreign = started().reserve_next_entry(BALANCE).reservation_id
        for unknown in ("unknown-id", foreign):
            with self.assertRaises(UnknownReservationError):
                coordinator.begin_submission(unknown)
        with self.assertRaises(ValueError):
            coordinator.begin_submission(None)
        status = coordinator.status()
        self.assertEqual((status.pending, status.submission_state), (current, RESERVED))
        coordinator.close()
        with self.assertRaisesRegex(CoordinatorError, "closed"):
            coordinator.begin_submission(current.reservation_id)


class ConfirmAcceptanceTest(unittest.TestCase):
    def test_acceptance_does_not_settle_the_bet(self):
        coordinator = started()
        reservation = submit(coordinator)
        self.assertTrue(coordinator.confirm_acceptance(reservation.reservation_id, "ORD-1"))
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (reservation, ACCEPTED, "ORD-1"),
        )
        self.assertEqual((status.attempt, status.is_active, status.outcome), (1, True, None))
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))  # still waiting for a result
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle(ASSET, PUT)
        self.assertTrue(coordinator.record_result(reservation.reservation_id, LOSS))
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry.stake_cents, 1000)

    def test_repeated_acceptance_changes_nothing_and_conflicts_are_refused(self):
        coordinator = started()
        first = submit(coordinator)
        coordinator.confirm_acceptance(first.reservation_id, "ORD-1")
        self.assertFalse(coordinator.confirm_acceptance(first.reservation_id, "ORD-1"))
        for other in ("ORD-2", "ord-1"):
            with self.subTest(other=other):
                with self.assertRaisesRegex(CoordinatorError, "already accepted as order ORD-1"):
                    coordinator.confirm_acceptance(first.reservation_id, other)
        coordinator.record_result(first.reservation_id, LOSS)

        second = coordinator.reserve_next_entry(BALANCE)
        with self.assertRaisesRegex(CoordinatorError, "is RESERVED"):
            coordinator.confirm_acceptance(second.reservation_id, "ORD-2")  # never submitted
        coordinator.begin_submission(second.reservation_id)
        with self.assertRaisesRegex(
            CoordinatorError, f"ORD-1 already belongs to reservation {first.reservation_id}"
        ):
            coordinator.confirm_acceptance(second.reservation_id, "ORD-1")
        with self.assertRaises(UnknownReservationError):
            coordinator.confirm_acceptance("unknown-id", "ORD-3")
        for invalid in (None, 7, "", "  "):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    coordinator.confirm_acceptance(second.reservation_id, invalid)
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (second, SUBMITTING, None),
        )


class StoreTestCase(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "state.db")

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

    def submission(self, reservation_id):
        return self.query(
            "SELECT submission_state, broker_order_id FROM reservations"
            " WHERE reservation_id = ?",
            (reservation_id,),
        )[0]

    def settled_rows(self):
        """Cycle outcomes and reservation results: what an acceptance must never change."""
        return (
            self.query("SELECT * FROM cycles ORDER BY cycle_id"),
            self.query(
                "SELECT reservation_id, cycle_id, attempt, stake_cents, result"
                " FROM reservations ORDER BY cycle_id, attempt"
            ),
        )

    def fail_updates(self):
        """Make every UPDATE on reservations fail inside SQLite; return the undo function."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "CREATE TRIGGER fail_update BEFORE UPDATE ON reservations"
                " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
            )
            connection.commit()

        def undo():
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("DROP TRIGGER fail_update")
                connection.commit()

        return undo


class ConcurrencyTest(StoreTestCase):
    def test_simultaneous_begins_start_a_single_submission(self):
        store = SlowStore(self.path)
        coordinator = self.running(store)
        reservation = coordinator.reserve_next_entry(BALANCE)
        results = run_simultaneously(
            self, lambda: coordinator.begin_submission(reservation.reservation_id)
        )
        self.assertEqual([r for r in results if isinstance(r, Reservation)], [reservation])
        refused = [r for r in results if isinstance(r, CoordinatorError)]
        self.assertEqual(len(refused), THREADS - 1)
        self.assertTrue(all("starts only once" in str(error) for error in refused))
        self.assertEqual(store.calls["save_submission_start"], 1)
        self.assertEqual(self.submission(reservation.reservation_id), (SUBMITTING, None))

    def test_simultaneous_confirmations_save_one_acceptance(self):
        store = SlowStore(self.path)
        coordinator = self.running(store)
        reservation = submit(coordinator)
        results = run_simultaneously(
            self, lambda: coordinator.confirm_acceptance(reservation.reservation_id, "ORD-1")
        )
        self.assertEqual((results.count(True), results.count(False)), (1, THREADS - 1))
        self.assertEqual(store.calls["save_acceptance"], 1)
        self.assertEqual(self.submission(reservation.reservation_id), (ACCEPTED, "ORD-1"))


class SubmissionRulesTest(StoreTestCase):
    def test_result_before_the_submission_is_refused_and_saves_nothing(self):
        coordinator = self.running()
        reservation = coordinator.reserve_next_entry(BALANCE)
        before = coordinator.status()
        for result in RESULTS:
            with self.subTest(result=result):
                with self.assertRaisesRegex(CoordinatorError, "never submitted"):
                    coordinator.record_result(reservation.reservation_id, result)
        self.assertEqual(coordinator.status(), before)
        self.assertEqual(
            self.query("SELECT result, submission_state FROM reservations"), [(None, RESERVED)]
        )
        coordinator.begin_submission(reservation.reservation_id)
        self.assertTrue(coordinator.record_result(reservation.reservation_id, LOSS))
        self.assertEqual(coordinator.status().attempt, 2)

    def test_late_acceptance_changes_neither_results_nor_the_current_cycle(self):
        coordinator = self.running()
        lost = submit(coordinator)
        coordinator.record_result(lost.reservation_id, LOSS)  # settled without an order id
        current = submit(coordinator)  # attempt 2 is pending
        before, settled = coordinator.status(), self.settled_rows()
        self.assertTrue(coordinator.confirm_acceptance(lost.reservation_id, "ORD-LATE"))
        self.assertEqual(coordinator.status(), before)
        self.assertEqual(self.settled_rows(), settled)
        self.assertEqual(self.submission(lost.reservation_id), (ACCEPTED, "ORD-LATE"))
        self.assertFalse(coordinator.record_result(lost.reservation_id, LOSS))  # result kept
        with self.assertRaisesRegex(ValueError, "conflicting result"):
            coordinator.record_result(lost.reservation_id, WIN)

        coordinator.record_result(current.reservation_id, WIN)
        ended, settled = coordinator.status(), self.settled_rows()
        self.assertTrue(coordinator.confirm_acceptance(current.reservation_id, "ORD-WIN"))
        self.assertEqual(coordinator.status(), ended)  # the cycle is not reopened
        self.assertEqual(self.settled_rows(), settled)
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry, Entry(ASSET, PUT, 1, 500))


class RestoreTest(StoreTestCase):
    def test_states_and_links_of_every_reservation_are_restored(self):
        before = self.running()
        linked = submit(before)
        before.confirm_acceptance(linked.reservation_id, "ORD-A")
        before.record_result(linked.reservation_id, LOSS)
        unlinked = submit(before)
        before.record_result(unlinked.reservation_id, WIN)  # settled without an order id
        before.start_cycle(ASSET, PUT)
        pending = submit(before)
        before.confirm_acceptance(pending.reservation_id, "ORD-C")
        saved = before.status()

        after = self.restart(before)
        status = after.status()
        self.assertEqual(status, replace(saved, is_paused=True, recovery_pending=True))
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (pending, ACCEPTED, "ORD-C"),
        )
        self.assertFalse(after.confirm_acceptance(linked.reservation_id, "ORD-A"))
        with self.assertRaisesRegex(CoordinatorError, "already accepted as order ORD-A"):
            after.confirm_acceptance(linked.reservation_id, "ORD-X")
        with self.assertRaisesRegex(
            CoordinatorError, f"ORD-C already belongs to reservation {pending.reservation_id}"
        ):
            after.confirm_acceptance(unlinked.reservation_id, "ORD-C")
        self.assertTrue(after.confirm_acceptance(unlinked.reservation_id, "ORD-B"))
        self.assertEqual(after.status(), status)
        self.assertEqual(
            self.query(
                "SELECT submission_state, broker_order_id FROM reservations"
                " ORDER BY cycle_id, attempt"
            ),
            [(ACCEPTED, "ORD-A"), (ACCEPTED, "ORD-B"), (ACCEPTED, "ORD-C")],
        )

    def test_restored_reservation_is_never_submitted(self):
        before = self.running()
        pending = before.reserve_next_entry(BALANCE)  # the process stops before submitting
        after = self.restart(before)
        after.resume()
        status = after.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.recovery_pending),
            (pending, RESERVED, True),
        )
        with self.assertRaisesRegex(CoordinatorError, "a restored reservation is never submitted"):
            after.begin_submission(pending.reservation_id)
        with self.assertRaisesRegex(CoordinatorError, "never submitted, so it cannot have a result"):
            after.record_result(pending.reservation_id, LOSS)
        with self.assertRaises(CoordinatorError):
            after.abandon_cycle()  # still pending
        self.assertEqual(self.submission(pending.reservation_id), (RESERVED, None))

    def test_acceptance_is_confirmed_and_saved_while_paused(self):
        before = self.running()
        pending = submit(before)  # sent right before the restart
        after = self.restart(before)
        self.assertTrue(after.confirm_acceptance(pending.reservation_id, "ORD-1"))
        self.assertEqual(self.submission(pending.reservation_id), (ACCEPTED, "ORD-1"))
        status = after.status()
        self.assertEqual(
            (status.is_paused, status.submission_state, status.broker_order_id),
            (True, ACCEPTED, "ORD-1"),
        )
        with self.assertRaisesRegex(CoordinatorError, "paused"):
            after.begin_submission(pending.reservation_id)
        self.assertTrue(after.record_result(pending.reservation_id, WIN))  # also while paused
        self.assertEqual(self.restart(after).status().outcome, WIN)

    def test_old_unverified_records_are_recovered_without_a_submission(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(LEGACY_DATABASE)
        coordinator = self.open()
        status = coordinator.status()
        self.assertEqual(
            (status.pending.reservation_id, status.submission_state, status.is_blocked),
            ("old-2", UNVERIFIED, True),
        )
        coordinator.resume()
        with self.assertRaisesRegex(CoordinatorError, "never submitted"):
            coordinator.begin_submission("old-2")
        self.assertTrue(coordinator.confirm_acceptance("old-1", "ORD-OLD-1"))  # settled one
        self.assertTrue(coordinator.confirm_acceptance("old-2", "ORD-OLD-2"))
        self.assertTrue(coordinator.record_result("old-2", LOSS))
        status = coordinator.status()
        self.assertEqual((status.attempt, status.pending, status.recovery_pending), (3, None, True))
        coordinator.abandon_cycle()
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
        fresh = submit(coordinator)
        self.assertEqual(fresh.entry, Entry(ASSET, PUT, 1, 500))
        self.assertEqual(coordinator.status().submission_state, SUBMITTING)


class WriteFailureTest(StoreTestCase):
    def test_failed_submission_start_stops_new_submissions_until_reopened(self):
        coordinator = self.running()
        reservation = coordinator.reserve_next_entry(BALANCE)
        undo = self.fail_updates()
        with self.assertRaisesRegex(StorageError, "saving the submission start failed"):
            coordinator.begin_submission(reservation.reservation_id)
        undo()
        status = coordinator.status()
        self.assertEqual((status.pending, status.submission_state), (reservation, RESERVED))
        self.assertIn("simulated write failure", status.submission_failure)
        self.assertFalse(status.can_start_cycle)
        coordinator.resume()  # does not clear the stop
        with self.assertRaisesRegex(CoordinatorError, "stopped after a storage failure"):
            coordinator.begin_submission(reservation.reservation_id)
        self.assertEqual(coordinator.status().submission_failure, status.submission_failure)
        self.assertEqual(self.submission(reservation.reservation_id), (RESERVED, None))

        reopened = self.restart(coordinator)
        status = reopened.status()
        self.assertIsNone(status.submission_failure)
        self.assertEqual(
            (status.is_paused, status.pending, status.submission_state),
            (True, reservation, RESERVED),
        )

    def test_failed_acceptance_keeps_the_pending_and_stops_new_entries(self):
        coordinator = self.running()
        reservation = submit(coordinator)
        undo = self.fail_updates()
        with self.assertRaisesRegex(StorageError, "saving the acceptance failed"):
            coordinator.confirm_acceptance(reservation.reservation_id, "ORD-1")
        undo()
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (reservation, SUBMITTING, None),
        )
        self.assertIn("simulated write failure", status.submission_failure)
        self.assertEqual(self.submission(reservation.reservation_id), (SUBMITTING, None))

        # Saving the confirmation again and settling the bet still work.
        self.assertTrue(coordinator.confirm_acceptance(reservation.reservation_id, "ORD-1"))
        self.assertTrue(coordinator.record_result(reservation.reservation_id, WIN))
        coordinator.resume()
        self.assertFalse(coordinator.status().can_start_cycle)
        with self.assertRaisesRegex(CoordinatorError, "stopped after a storage failure"):
            coordinator.start_cycle(ASSET, PUT)
        self.assertIsNotNone(coordinator.status().submission_failure)

        reopened = self.restart(coordinator)
        self.assertIsNone(reopened.status().submission_failure)
        self.assertEqual(self.submission(reservation.reservation_id), (ACCEPTED, "ORD-1"))
        reopened.resume()
        self.assertEqual(reopened.start_cycle(ASSET, PUT), 2)


if __name__ == "__main__":
    unittest.main()
