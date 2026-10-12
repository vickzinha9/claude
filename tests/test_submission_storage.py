import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import ABANDONED, CALL, LOSS, RESULTS, UNKNOWN, WIN
from pocketautonomo.storage import (
    SCHEMA_VERSION,
    SqliteStore,
    StorageError,
    connect_read_only,
)
from pocketautonomo.submission import (
    ACCEPTED,
    NOT_SUBMITTED,
    RESERVED,
    SUBMISSION_STATES,
    SUBMITTING,
    UNVERIFIED,
)

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents

# Tables as every version up to 5f363a2 wrote them, before submissions were tracked,
# with three cycles: one won, one abandoned and one still open.
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
INSERT INTO cycles VALUES (1, 'EURUSD_otc', 'CALL', 'WIN');
INSERT INTO cycles VALUES (2, 'EURUSD_otc', 'PUT', 'ABANDONED');
INSERT INTO cycles VALUES (3, 'GBPUSD_otc', 'CALL', NULL);
INSERT INTO reservations VALUES ('old-1', 1, 1, 500, 'LOSS');
INSERT INTO reservations VALUES ('old-2', 1, 2, 1000, 'WIN');
INSERT INTO reservations VALUES ('old-3', 2, 1, 500, 'LOSS');
INSERT INTO reservations VALUES ('old-4', 3, 1, 500, 'LOSS');
"""


class SubmissionStorageTestCase(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "state.db")

    def open_store(self):
        store = SqliteStore(self.path)
        self.addCleanup(store.close)
        return store

    def reserve(self, store):
        """Start a cycle and reserve its first entry; return the coordinator and the id."""
        coordinator = Coordinator(store)
        coordinator.resume()
        coordinator.start_cycle(ASSET, CALL)
        return coordinator, coordinator.reserve_next_entry(BALANCE).reservation_id

    def started(self):
        """A store holding a reservation whose submission has started."""
        store = self.open_store()
        coordinator, reservation_id = self.reserve(store)
        coordinator.begin_submission(reservation_id)
        return store, coordinator, reservation_id

    def make_legacy_database(self, pending_result=None):
        """Write a version 0 database whose open cycle has a pending reservation, old-5."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(LEGACY_DATABASE)
            connection.execute(
                "INSERT INTO reservations VALUES ('old-5', 3, 2, 1000, ?)", (pending_result,)
            )
            connection.commit()

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

    def rows(self):
        """Every saved row of both tables."""
        return (
            self.query("SELECT * FROM cycles ORDER BY cycle_id"),
            self.query("SELECT * FROM reservations ORDER BY cycle_id, attempt"),
        )

    def settled_rows(self):
        """Cycle outcomes and reservation results: what an acceptance must never change."""
        return (
            self.query("SELECT * FROM cycles ORDER BY cycle_id"),
            self.query(
                "SELECT reservation_id, cycle_id, attempt, stake_cents, result"
                " FROM reservations ORDER BY cycle_id, attempt"
            ),
        )

    def execute(self, sql, parameters=()):
        """Write through a raw connection that bypasses the store."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def fail_updates(self, table="reservations"):
        """Make every UPDATE on table fail inside SQLite; return the undo function."""
        trigger = f"fail_update_{table}"
        self.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table}"
            " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        return lambda: self.execute(f"DROP TRIGGER {trigger}")


class MigrationTest(SubmissionStorageTestCase):
    def test_migration_keeps_every_row_and_marks_old_reservations_unverified(self):
        self.make_legacy_database(pending_result=UNKNOWN)
        cycles, reservations = self.rows()
        self.assertEqual(self.query("PRAGMA user_version"), [(0,)])

        store = self.open_store()
        self.assertEqual(self.query("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        self.assertEqual(store.load(), (cycles, reservations))  # same format as before
        self.assertEqual(
            self.query("SELECT * FROM reservations ORDER BY cycle_id, attempt"),
            [row + (UNVERIFIED, None) for row in reservations],
        )
        self.assertEqual(
            store.load_submissions(), {row[0]: (UNVERIFIED, None) for row in reservations}
        )
        status = Coordinator(store).status()  # the coordinator restores the migrated state
        self.assertEqual(
            (status.cycle_id, status.attempt, status.is_blocked, status.recovery_pending),
            (3, 2, True, True),
        )
        self.assertEqual(
            (status.pending.reservation_id, status.submission_state, status.broker_order_id),
            ("old-5", UNVERIFIED, None),
        )

    def test_failed_migration_is_rolled_back(self):
        self.make_legacy_database()
        # An index with the name the migration creates makes its last statement fail,
        # after both columns were added in the same transaction.
        self.execute("CREATE INDEX reservations_broker_order_id ON reservations (stake_cents)")
        before = self.rows()
        with self.assertRaisesRegex(StorageError, "migrating the database failed"):
            SqliteStore(self.path)
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.query("PRAGMA table_info(reservations)")), 5)
        self.assertEqual(self.query("PRAGMA user_version"), [(0,)])

        self.execute("DROP INDEX reservations_broker_order_id")
        store = self.open_store()  # the failed open released the lock
        self.assertEqual(store.load(), before)
        self.assertEqual(set(store.load_submissions().values()), {(UNVERIFIED, None)})

    def test_unsupported_schema_version_is_refused_untouched(self):
        self.make_legacy_database()
        self.execute("PRAGMA user_version = 99")
        before = self.rows()
        with self.assertRaisesRegex(StorageError, "schema version 99"):
            SqliteStore(self.path)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.query("PRAGMA user_version"), [(99,)])
        self.assertEqual(len(self.query("PRAGMA table_info(reservations)")), 5)


class SubmissionStartTest(SubmissionStorageTestCase):
    def test_new_reservation_is_reserved_without_order_id(self):
        store = self.open_store()
        _, reservation_id = self.reserve(store)
        self.assertEqual(self.submission(reservation_id), (RESERVED, None))
        self.assertEqual(store.load_submissions(), {reservation_id: (RESERVED, None)})

    def test_submission_start_is_committed_before_returning(self):
        store = self.open_store()
        _, reservation_id = self.reserve(store)
        self.assertIsNone(store.save_submission_start(reservation_id))
        # An independent connection only sees committed data.
        self.assertEqual(self.submission(reservation_id), (SUBMITTING, None))

    def test_second_submission_start_is_refused_also_after_restart(self):
        store = self.open_store()
        _, reservation_id = self.reserve(store)
        store.save_submission_start(reservation_id)
        with self.assertRaisesRegex(StorageError, "starts only once"):
            store.save_submission_start(reservation_id)
        store.close()

        restarted = self.open_store()
        with self.assertRaisesRegex(StorageError, "starts only once"):
            restarted.save_submission_start(reservation_id)
        restarted.save_acceptance(reservation_id, "ORD-1")
        with self.assertRaisesRegex(StorageError, "starts only once"):
            restarted.save_submission_start(reservation_id)
        self.assertEqual(restarted.load_submissions(), {reservation_id: (ACCEPTED, "ORD-1")})

    def test_submission_start_needs_a_reserved_reservation_without_result(self):
        self.make_legacy_database()  # old-5 is pending, without a result
        store = self.open_store()
        with self.assertRaisesRegex(StorageError, "UNVERIFIED: it may already have been"):
            store.save_submission_start("old-5")
        with self.assertRaisesRegex(StorageError, "does not exist"):
            store.save_submission_start("unknown-id")
        with self.assertRaises(ValueError):
            store.save_submission_start(None)
        self.assertEqual(self.submission("old-5"), (UNVERIFIED, None))

        for result in (UNKNOWN, WIN):
            with self.subTest(result=result):
                self.path = os.path.join(self.directory, f"{result}.db")
                store = self.open_store()
                _, reservation_id = self.reserve(store)
                # The coordinator refuses results before the submission starts, so a
                # RESERVED row with a result only comes from older code or tampering.
                self.execute(
                    "UPDATE reservations SET result = ? WHERE reservation_id = ?",
                    (result, reservation_id),
                )
                with self.assertRaisesRegex(StorageError, f"already has the result {result}"):
                    store.save_submission_start(reservation_id)
                self.assertEqual(self.submission(reservation_id), (RESERVED, None))


class AcceptanceTest(SubmissionStorageTestCase):
    def test_acceptance_is_saved_and_repeating_it_changes_nothing(self):
        store, _, reservation_id = self.started()
        self.assertTrue(store.save_acceptance(reservation_id, "ORD-1"))
        self.assertEqual(self.submission(reservation_id), (ACCEPTED, "ORD-1"))  # committed
        self.assertFalse(store.save_acceptance(reservation_id, "ORD-1"))
        store.close()

        restarted = self.open_store()
        self.assertEqual(restarted.load_submissions(), {reservation_id: (ACCEPTED, "ORD-1")})
        self.assertFalse(restarted.save_acceptance(reservation_id, "ORD-1"))
        self.assertEqual(self.submission(reservation_id), (ACCEPTED, "ORD-1"))

    def test_conflicting_order_id_is_refused(self):
        store, _, reservation_id = self.started()
        store.save_acceptance(reservation_id, "ORD-1")
        for other in ("ORD-2", "ord-1", "ORD-1 "):
            with self.subTest(other=other):
                with self.assertRaisesRegex(StorageError, "already accepted as order ORD-1"):
                    store.save_acceptance(reservation_id, other)
        self.assertEqual(self.submission(reservation_id), (ACCEPTED, "ORD-1"))

    def test_order_id_belongs_to_a_single_reservation(self):
        store, coordinator, first = self.started()
        store.save_acceptance(first, "ORD-1")
        coordinator.record_result(first, LOSS)
        second = coordinator.reserve_next_entry(BALANCE).reservation_id
        coordinator.begin_submission(second)
        with self.assertRaisesRegex(StorageError, f"ORD-1 already belongs to reservation {first}"):
            store.save_acceptance(second, "ORD-1")
        self.assertEqual(self.submission(second), (SUBMITTING, None))
        with self.assertRaises(sqlite3.IntegrityError):  # the database refuses it as well
            self.execute(
                "UPDATE reservations SET submission_state = 'ACCEPTED',"
                " broker_order_id = 'ORD-1' WHERE reservation_id = ?",
                (second,),
            )
        self.assertEqual(self.submission(second), (SUBMITTING, None))
        self.assertTrue(store.save_acceptance(second, "ORD-2"))

    def test_acceptance_needs_a_started_submission_and_never_changes_the_result(self):
        store, coordinator, reservation_id = self.started()
        coordinator.record_result(reservation_id, UNKNOWN)  # e.g. a timeout: still pending
        self.assertTrue(store.save_acceptance(reservation_id, "ORD-1"))
        coordinator.record_result(reservation_id, LOSS)

        later = coordinator.reserve_next_entry(BALANCE).reservation_id
        with self.assertRaisesRegex(StorageError, "is RESERVED: only SUBMITTING or UNVERIFIED"):
            store.save_acceptance(later, "ORD-2")  # its submission never started
        coordinator.begin_submission(later)
        coordinator.record_result(later, WIN)
        settled = self.settled_rows()
        self.assertTrue(store.save_acceptance(later, "ORD-2"))  # late: after its result
        self.assertEqual(self.submission(later), (ACCEPTED, "ORD-2"))
        self.assertEqual(self.settled_rows(), settled)  # results and outcomes untouched
        with self.assertRaisesRegex(StorageError, "does not exist"):
            store.save_acceptance("unknown-id", "ORD-3")

    def test_unverified_reservations_can_be_accepted_also_after_their_result(self):
        self.make_legacy_database()
        store = self.open_store()
        settled = self.settled_rows()
        self.assertTrue(store.save_acceptance("old-5", "ORD-OLD-5"))  # pending
        self.assertTrue(store.save_acceptance("old-4", "ORD-OLD-4"))  # resolved with LOSS
        self.assertEqual(self.submission("old-4"), (ACCEPTED, "ORD-OLD-4"))
        self.assertEqual(self.settled_rows(), settled)
        with self.assertRaisesRegex(StorageError, "ORD-OLD-4 already belongs to reservation old-4"):
            store.save_acceptance("old-3", "ORD-OLD-4")  # unique also for late links
        self.assertEqual(self.submission("old-3"), (UNVERIFIED, None))

    def test_only_non_empty_text_is_an_order_id(self):
        store, _, reservation_id = self.started()
        for invalid in (None, 123, b"ORD-1", "", "   ", "\t\n"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    store.save_acceptance(reservation_id, invalid)
        for value in ("''", "'   '", "x'4f5244'"):  # the database refuses them as well
            with self.subTest(sql=value):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.execute(f"UPDATE reservations SET broker_order_id = {value}")
        self.assertEqual(self.submission(reservation_id), (SUBMITTING, None))


class WriteFailureTest(SubmissionStorageTestCase):
    def test_failed_submission_start_is_rolled_back(self):
        store = self.open_store()
        _, reservation_id = self.reserve(store)
        undo = self.fail_updates()
        with self.assertRaisesRegex(StorageError, "saving the submission start failed"):
            store.save_submission_start(reservation_id)
        self.assertEqual(self.submission(reservation_id), (RESERVED, None))
        undo()
        store.save_submission_start(reservation_id)  # the failed transaction did not stay open
        self.assertEqual(self.submission(reservation_id), (SUBMITTING, None))

    def test_failed_acceptance_is_rolled_back_and_stays_uncertain(self):
        store, _, reservation_id = self.started()
        undo = self.fail_updates()
        with self.assertRaisesRegex(StorageError, "saving the acceptance failed"):
            store.save_acceptance(reservation_id, "ORD-1")
        self.assertEqual(self.submission(reservation_id), (SUBMITTING, None))
        with self.assertRaisesRegex(StorageError, "starts only once"):
            store.save_submission_start(reservation_id)  # still never submitted again
        undo()
        self.assertTrue(store.save_acceptance(reservation_id, "ORD-1"))
        self.assertEqual(self.submission(reservation_id), (ACCEPTED, "ORD-1"))


class IntegrityTest(SubmissionStorageTestCase):
    def test_inconsistent_submission_rows_are_refused(self):
        tamperings = (
            "UPDATE reservations SET submission_state = 'SENT'",
            "UPDATE reservations SET submission_state = 'ACCEPTED'",  # without an order id
            "UPDATE reservations SET broker_order_id = 'ORD-1'",  # RESERVED with an order id
        )
        for number, sql in enumerate(tamperings):
            with self.subTest(sql=sql):
                self.path = os.path.join(self.directory, f"tampered{number}.db")
                store = self.open_store()
                self.reserve(store)
                self.execute(sql)
                with self.assertRaisesRegex(StorageError, "inconsistent saved state"):
                    store.load_submissions()


class NotSubmittedTest(SubmissionStorageTestCase):
    def closed(self):
        """A store whose only reservation, at attempt 1, was closed as NOT_SUBMITTED."""
        store = self.open_store()
        coordinator, reservation_id = self.reserve(store)
        reservation = coordinator.status().pending
        self.assertTrue(store.save_not_submitted(reservation_id))
        return store, reservation

    def test_closure_marks_the_reservation_and_abandons_the_cycle_together(self):
        store = self.open_store()
        coordinator, first = self.reserve(store)
        coordinator.begin_submission(first)
        coordinator.record_result(first, LOSS)
        second = coordinator.reserve_next_entry(BALANCE).reservation_id  # attempt 2
        self.assertTrue(store.save_not_submitted(second))
        # An independent connection only sees committed data. No result is saved, the
        # attempt does not move and the earlier LOSS is untouched.
        self.assertEqual(
            self.query(
                "SELECT reservation_id, attempt, stake_cents, result, submission_state,"
                " broker_order_id FROM reservations ORDER BY attempt"
            ),
            [
                (first, 1, 500, LOSS, SUBMITTING, None),
                (second, 2, 1000, None, NOT_SUBMITTED, None),
            ],
        )
        self.assertEqual(self.query("SELECT cycle_id, outcome FROM cycles"), [(1, ABANDONED)])

    def test_repeated_closure_changes_nothing_and_later_writes_are_refused(self):
        self.assertIn(NOT_SUBMITTED, SUBMISSION_STATES)
        self.assertNotIn(NOT_SUBMITTED, RESULTS)  # not a bet result
        store, closed = self.closed()
        rows = self.rows()
        self.assertFalse(store.save_not_submitted(closed.reservation_id))
        writes = {
            "result": lambda: store.save_result(closed, LOSS, None),
            "UNKNOWN": lambda: store.save_result(closed, UNKNOWN, None),
            "submission start": lambda: store.save_submission_start(closed.reservation_id),
            "acceptance": lambda: store.save_acceptance(closed.reservation_id, "ORD-1"),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                with self.assertRaisesRegex(StorageError, "closed as NOT_SUBMITTED"):
                    write()
        with self.assertRaisesRegex(StorageError, "is not open without pending reservations"):
            store.save_abandonment(closed.cycle_id)  # the cycle already ended
        self.assertEqual(self.rows(), rows)

    def test_closure_persists_after_reopening_the_store(self):
        store, closed = self.closed()
        store.close()

        reopened = self.open_store()
        cycles, reservations = reopened.load()  # same format as before
        self.assertEqual(cycles, [(1, ASSET, CALL, ABANDONED)])
        self.assertEqual(reservations, [(closed.reservation_id, 1, 1, 500, None)])
        self.assertEqual(
            reopened.load_submissions(), {closed.reservation_id: (NOT_SUBMITTED, None)}
        )
        rows = self.rows()
        self.assertFalse(reopened.save_not_submitted(closed.reservation_id))
        with self.assertRaisesRegex(StorageError, "closed as NOT_SUBMITTED"):
            reopened.save_result(closed, WIN, WIN)
        with self.assertRaisesRegex(StorageError, "closed as NOT_SUBMITTED"):
            reopened.save_submission_start(closed.reservation_id)
        with self.assertRaisesRegex(StorageError, "closed as NOT_SUBMITTED"):
            reopened.save_acceptance(closed.reservation_id, "ORD-1")
        self.assertEqual(self.rows(), rows)

    def test_reservations_that_may_have_been_sent_or_are_invalid_are_refused(self):
        def begin(coordinator, reservation_id):
            coordinator.begin_submission(reservation_id)

        def accept(coordinator, reservation_id):
            begin(coordinator, reservation_id)
            coordinator.confirm_acceptance(reservation_id, "ORD-1")

        def time_out(coordinator, reservation_id):
            begin(coordinator, reservation_id)
            coordinator.record_result(reservation_id, UNKNOWN)  # timeout or network error

        def tamper(sql):
            return lambda coordinator, reservation_id: self.execute(sql, (reservation_id,))

        cases = (
            ("SUBMITTING without order id", begin, "is SUBMITTING"),
            ("ACCEPTED", accept, "is ACCEPTED"),
            ("SUBMITTING with UNKNOWN", time_out, "is SUBMITTING"),
            # The shapes below only come from older code or tampering.
            (
                "RESERVED with UNKNOWN",
                tamper("UPDATE reservations SET result = 'UNKNOWN' WHERE reservation_id = ?"),
                "has an UNKNOWN result",
            ),
            (
                "RESERVED with a result",
                tamper("UPDATE reservations SET result = 'LOSS' WHERE reservation_id = ?"),
                "already has the result LOSS",
            ),
            (
                "RESERVED with an order id",
                tamper(
                    "UPDATE reservations SET broker_order_id = 'ORD-9' WHERE reservation_id = ?"
                ),
                "linked to order ORD-9",
            ),
            (
                "ended cycle",
                tamper(
                    "UPDATE cycles SET outcome = 'WIN' WHERE cycle_id ="
                    " (SELECT cycle_id FROM reservations WHERE reservation_id = ?)"
                ),
                "cycle 1 already ended with WIN",
            ),
        )
        for number, (name, prepare, message) in enumerate(cases):
            with self.subTest(case=name):
                self.path = os.path.join(self.directory, f"case{number}.db")
                store = self.open_store()
                coordinator, reservation_id = self.reserve(store)
                prepare(coordinator, reservation_id)
                rows = self.rows()
                with self.assertRaisesRegex(StorageError, message):
                    store.save_not_submitted(reservation_id)
                self.assertEqual(self.rows(), rows)

        self.path = os.path.join(self.directory, "legacy.db")
        self.make_legacy_database()  # old-5 is pending without a result
        store = self.open_store()
        rows = self.rows()
        with self.assertRaisesRegex(StorageError, "is UNVERIFIED"):
            store.save_not_submitted("old-5")
        with self.assertRaisesRegex(StorageError, "does not exist"):
            store.save_not_submitted("unknown-id")
        with self.assertRaises(ValueError):
            store.save_not_submitted(None)
        self.assertEqual(self.rows(), rows)

    def test_failed_or_refused_cycle_update_rolls_back_the_closure(self):
        store = self.open_store()
        _, reservation_id = self.reserve(store)
        rows = self.rows()
        undo = self.fail_updates("cycles")
        with self.assertRaisesRegex(
            StorageError, "saving the NOT_SUBMITTED closure failed: simulated write failure"
        ):
            store.save_not_submitted(reservation_id)
        undo()
        self.assertEqual(self.rows(), rows)  # the reservation update was undone as well

        # Another pending reservation in the cycle, only possible through tampering,
        # makes the database refuse the abandonment, with the same rollback.
        self.execute(
            "INSERT INTO reservations (reservation_id, cycle_id, attempt, stake_cents,"
            " submission_state) VALUES ('extra', 1, 2, 1000, 'RESERVED')"
        )
        rows = self.rows()
        with self.assertRaisesRegex(StorageError, "another reservation of the cycle is still pending"):
            store.save_not_submitted(reservation_id)
        self.assertEqual(self.rows(), rows)

        self.execute("DELETE FROM reservations WHERE reservation_id = 'extra'")
        self.assertTrue(store.save_not_submitted(reservation_id))  # no transaction stayed open
        self.assertEqual(self.submission(reservation_id), (NOT_SUBMITTED, None))

    def test_only_a_consistent_closure_is_repeated(self):
        tamperings = (
            "UPDATE cycles SET outcome = NULL",  # the cycle was reopened
            "UPDATE reservations SET result = 'LOSS'",  # the closure got a result
            "UPDATE reservations SET broker_order_id = 'ORD-1'",  # and an order id
            "INSERT INTO reservations (reservation_id, cycle_id, attempt, stake_cents,"
            " submission_state) VALUES ('extra', 1, 2, 1000, 'RESERVED')",  # pending again
        )
        for number, sql in enumerate(tamperings):
            with self.subTest(sql=sql):
                self.path = os.path.join(self.directory, f"tampered-closure{number}.db")
                store, closed = self.closed()
                self.execute(sql)
                rows = self.rows()
                with self.assertRaisesRegex(StorageError, "inconsistent saved state"):
                    store.save_not_submitted(closed.reservation_id)
                self.assertEqual(self.rows(), rows)


if __name__ == "__main__":
    unittest.main()
