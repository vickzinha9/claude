import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace

from pocketautonomo.coordinator import (
    Coordinator,
    CoordinatorError,
    Status,
    UnknownReservationError,
)
from pocketautonomo.cycle import (
    CALL,
    CANCELLED,
    DRAW,
    LOSS,
    PUT,
    REJECTED,
    UNKNOWN,
    WIN,
    Entry,
)
from pocketautonomo.storage import SqliteStore, StorageError

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents


def play(coordinator, result):
    """Reserve the next entry and record its result; return the reservation."""
    reservation = coordinator.reserve_next_entry(BALANCE)
    coordinator.record_result(reservation.reservation_id, result)
    return reservation


class StorageTestCase(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "state.db")

    def open(self):
        """Open the database the way a restarted process would."""
        store = SqliteStore(self.path)
        self.addCleanup(store.close)
        return Coordinator(store)

    def running(self):
        coordinator = self.open()
        coordinator.resume()
        return coordinator

    def query(self, sql):
        """Read committed rows through an independent connection."""
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql).fetchall()

    def execute(self, sql):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(sql)
            connection.commit()

    def fail_writes(self, operation, table):
        """Make every OPERATION on TABLE fail inside SQLite; return a function that undoes it."""
        name = f"fail_{operation}_{table}".lower()
        self.execute(
            f"CREATE TRIGGER {name} BEFORE {operation} ON {table}"
            " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        return lambda: self.execute(f"DROP TRIGGER {name}")


class RestoreTest(StorageTestCase):
    def test_reservation_is_saved_before_it_is_returned(self):
        coordinator = self.running()
        coordinator.start_cycle(ASSET, PUT)
        reservation = coordinator.reserve_next_entry(BALANCE)
        self.assertEqual(
            self.query("SELECT reservation_id, cycle_id, attempt, stake_cents, result"
                       " FROM reservations"),
            [(reservation.reservation_id, 1, 1, 500, None)],
        )
        self.assertEqual(self.query("SELECT * FROM cycles"), [(1, ASSET, PUT, None)])

    def test_opens_paused_even_without_saved_state(self):
        coordinator = self.open()
        self.assertEqual(coordinator.status(), Status(is_paused=True))
        self.assertFalse(coordinator.status().can_start_cycle)
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle(ASSET, CALL)
        coordinator.resume()
        self.assertTrue(coordinator.status().can_start_cycle)
        self.assertEqual(coordinator.start_cycle(ASSET, CALL), 1)

    def test_restored_cycle_releases_no_entry_until_resume(self):
        before = self.running()
        before.start_cycle(ASSET, PUT)
        play(before, LOSS)

        after = self.open()
        self.assertEqual(after.status(), replace(before.status(), is_paused=True))
        self.assertIsNone(after.reserve_next_entry(BALANCE))
        with self.assertRaises(CoordinatorError):
            after.start_cycle(ASSET, CALL)
        after.resume()
        self.assertEqual(after.reserve_next_entry(BALANCE).entry, Entry(ASSET, PUT, 2, 1000))

    def test_pending_reservation_is_restored_but_never_handed_out_again(self):
        before = self.running()
        before.start_cycle(ASSET, CALL)
        play(before, LOSS)
        pending = before.reserve_next_entry(BALANCE)

        after = self.open()
        self.assertEqual(after.status(), replace(before.status(), is_paused=True))
        after.resume()
        self.assertIsNone(after.reserve_next_entry(BALANCE))
        self.assertEqual(after.status().pending, pending)
        self.assertEqual(len(self.query("SELECT * FROM reservations")), 2)

    def test_results_for_restored_reservation_are_accepted_while_paused(self):
        before = self.running()
        before.start_cycle(ASSET, CALL)
        pending = before.reserve_next_entry(BALANCE)

        after = self.open()
        self.assertTrue(after.record_result(pending.reservation_id, LOSS))
        self.assertIsNone(after.reserve_next_entry(BALANCE))  # still paused
        after.resume()
        self.assertEqual(after.reserve_next_entry(BALANCE).entry, Entry(ASSET, CALL, 2, 1000))

    def test_ended_cycles_are_restored_with_their_outcome(self):
        histories = {
            WIN: (LOSS, WIN),
            LOSS: (LOSS, LOSS, LOSS, LOSS),
            DRAW: (DRAW,),
            CANCELLED: (LOSS, LOSS, CANCELLED),
            REJECTED: (LOSS, REJECTED),
        }
        for outcome, results in histories.items():
            with self.subTest(outcome=outcome):
                self.path = os.path.join(self.directory, f"{outcome}.db")
                before = self.running()
                before.start_cycle(ASSET, CALL)
                for result in results:
                    play(before, result)

                after = self.open()
                self.assertEqual(after.status(), replace(before.status(), is_paused=True))
                self.assertEqual(after.status().outcome, outcome)
                self.assertEqual(self.query("SELECT outcome FROM cycles"), [(outcome,)])
                self.assertFalse(after.status().can_start_cycle)
                after.resume()
                self.assertTrue(after.status().can_start_cycle)


class UnknownAfterRestartTest(StorageTestCase):
    def test_unknown_pending_stays_blocked_after_restart(self):
        before = self.running()
        before.start_cycle(ASSET, CALL)
        play(before, LOSS)
        pending = before.reserve_next_entry(BALANCE)
        before.record_result(pending.reservation_id, UNKNOWN)

        after = self.running()
        status = after.status()
        self.assertEqual((status.pending, status.is_blocked, status.attempt), (pending, True, 2))
        self.assertIsNone(after.reserve_next_entry(BALANCE))
        with self.assertRaises(CoordinatorError):
            after.start_cycle(ASSET, CALL)
        self.assertFalse(after.record_result(pending.reservation_id, UNKNOWN))
        self.assertTrue(after.record_result(pending.reservation_id, LOSS))
        self.assertEqual(after.reserve_next_entry(BALANCE).entry, Entry(ASSET, CALL, 3, 2000))

    def test_confirmation_after_unknown_survives_another_restart(self):
        before = self.running()
        before.start_cycle(ASSET, CALL)
        pending = before.reserve_next_entry(BALANCE)
        before.record_result(pending.reservation_id, UNKNOWN)
        self.assertEqual(self.query("SELECT result FROM reservations"), [(UNKNOWN,)])

        self.assertTrue(self.open().record_result(pending.reservation_id, CANCELLED))

        after = self.open()
        status = after.status()
        self.assertEqual(
            (status.outcome, status.pending, status.is_blocked), (CANCELLED, None, False)
        )
        self.assertFalse(after.record_result(pending.reservation_id, CANCELLED))
        self.assertEqual(self.query("SELECT result FROM reservations"), [(CANCELLED,)])


class DuplicateAfterRestartTest(StorageTestCase):
    def test_duplicate_results_are_ignored_after_restart(self):
        before = self.running()
        before.start_cycle(ASSET, CALL)
        lost = play(before, LOSS)
        won = play(before, WIN)
        saved = self.query("SELECT * FROM reservations ORDER BY attempt")

        after = self.open()
        self.assertFalse(after.record_result(lost.reservation_id, LOSS))
        self.assertFalse(after.record_result(won.reservation_id, WIN))
        self.assertFalse(after.record_result(won.reservation_id, UNKNOWN))
        with self.assertRaises(ValueError):
            after.record_result(lost.reservation_id, WIN)
        with self.assertRaises(UnknownReservationError):
            after.record_result("unknown-id", WIN)
        self.assertEqual(after.status(), replace(before.status(), is_paused=True))
        self.assertEqual(self.query("SELECT * FROM reservations ORDER BY attempt"), saved)

    def test_old_results_do_not_touch_a_cycle_started_after_restart(self):
        before = self.running()
        before.start_cycle(ASSET, CALL)
        old = play(before, WIN)

        after = self.running()
        self.assertEqual(after.start_cycle(ASSET, PUT), 2)
        current = after.reserve_next_entry(BALANCE)
        self.assertEqual(current.entry, Entry(ASSET, PUT, 1, 500))
        self.assertNotEqual(current.reservation_id, old.reservation_id)
        self.assertFalse(after.record_result(old.reservation_id, WIN))
        status = after.status()
        self.assertEqual((status.cycle_id, status.attempt, status.pending), (2, 1, current))


class WriteFailureTest(StorageTestCase):
    def test_failed_reservation_write_releases_no_entry(self):
        coordinator = self.running()
        coordinator.start_cycle(ASSET, CALL)
        undo = self.fail_writes("INSERT", "reservations")
        with self.assertRaises(StorageError):
            coordinator.reserve_next_entry(BALANCE)
        self.assertIsNone(coordinator.status().pending)
        self.assertEqual(self.query("SELECT * FROM reservations"), [])
        self.assertIsNone(self.open().status().pending)  # nothing pending after a restart
        undo()
        reservation = coordinator.reserve_next_entry(BALANCE)
        self.assertEqual(reservation.entry, Entry(ASSET, CALL, 1, 500))
        self.assertEqual(len(self.query("SELECT * FROM reservations")), 1)

    def test_failed_result_write_keeps_reservation_pending(self):
        coordinator = self.running()
        coordinator.start_cycle(ASSET, CALL)
        reservation = coordinator.reserve_next_entry(BALANCE)
        before = coordinator.status()
        undo = self.fail_writes("UPDATE", "reservations")
        for result in (UNKNOWN, LOSS, WIN):
            with self.subTest(result=result):
                with self.assertRaises(StorageError):
                    coordinator.record_result(reservation.reservation_id, result)
                self.assertEqual(coordinator.status(), before)
        self.assertEqual(self.query("SELECT result FROM reservations"), [(None,)])
        undo()
        self.assertTrue(coordinator.record_result(reservation.reservation_id, LOSS))
        self.assertEqual(coordinator.status().attempt, 2)

    def test_failed_outcome_write_rolls_back_the_whole_result(self):
        coordinator = self.running()
        coordinator.start_cycle(ASSET, CALL)
        reservation = coordinator.reserve_next_entry(BALANCE)
        undo = self.fail_writes("UPDATE", "cycles")
        with self.assertRaises(StorageError):
            coordinator.record_result(reservation.reservation_id, WIN)
        self.assertEqual(self.query("SELECT result FROM reservations"), [(None,)])
        status = coordinator.status()
        self.assertEqual((status.pending, status.is_active), (reservation, True))
        undo()
        self.assertTrue(coordinator.record_result(reservation.reservation_id, WIN))
        self.assertEqual(self.query("SELECT outcome FROM cycles"), [(WIN,)])

    def test_failed_cycle_write_starts_nothing(self):
        coordinator = self.running()
        undo = self.fail_writes("INSERT", "cycles")
        with self.assertRaises(StorageError):
            coordinator.start_cycle(ASSET, CALL)
        self.assertIsNone(coordinator.status().cycle_id)
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        undo()
        self.assertEqual(coordinator.start_cycle(ASSET, CALL), 1)


class IntegrityTest(StorageTestCase):
    def test_inconsistent_saved_state_is_refused(self):
        tamperings = (
            "UPDATE cycles SET outcome = 'WIN'",
            "INSERT INTO reservations VALUES ('extra', 1, 2, 1000, NULL)",
            "UPDATE reservations SET result = 'TIMEOUT'",
            "INSERT INTO cycles VALUES (2, 'EURUSD_otc', 'CALL', NULL)",
        )
        for number, sql in enumerate(tamperings):
            with self.subTest(sql=sql):
                self.path = os.path.join(self.directory, f"tampered{number}.db")
                coordinator = self.running()
                coordinator.start_cycle(ASSET, CALL)
                coordinator.reserve_next_entry(BALANCE)
                self.execute(sql)
                with self.assertRaises(StorageError):
                    self.open()

    def test_store_never_overwrites_a_definitive_result(self):
        coordinator = self.running()
        coordinator.start_cycle(ASSET, CALL)
        reservation = play(coordinator, LOSS)
        store = SqliteStore(self.path)
        self.addCleanup(store.close)
        with self.assertRaises(StorageError):
            store.save_result(reservation, WIN, WIN)
        self.assertEqual(self.query("SELECT result FROM reservations"), [(LOSS,)])
        self.assertEqual(self.query("SELECT outcome FROM cycles"), [(None,)])

    def test_simultaneous_reservations_save_a_single_entry(self):
        coordinator = self.running()
        coordinator.start_cycle(ASSET, CALL)
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait(timeout=5)
            results.append(coordinator.reserve_next_entry(BALANCE))

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        reserved = [r for r in results if r is not None]
        self.assertEqual((len(results), len(reserved)), (8, 1))
        self.assertEqual(
            self.query("SELECT reservation_id FROM reservations"), [(reserved[0].reservation_id,)]
        )


if __name__ == "__main__":
    unittest.main()
