"""Tests of the decisions that start cycles: schema version 2 of the store, and the coordinator.

A CALL/PUT decision is saved with the cycle it starts, in one transaction, and starts one cycle
at most, also after a restart. pause() stops new cycles and entries as a restart does.
"""

import math
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pocketautonomo.coordinator import (
    ConsumedDecision,
    Coordinator,
    CoordinatorError,
    DecisionAlreadyUsedError,
)
from pocketautonomo.cycle import CALL, LOSS, PUT, WIN
from pocketautonomo.storage import (
    BASE_TABLES,
    MIGRATIONS,
    SCHEMA_VERSION,
    SqliteStore,
    StorageError,
    connect_read_only,
)
from pocketautonomo.submission import UNVERIFIED

ASSET = "EURUSD_otc"
BALANCE = 100_000
DECISION_ID = "EURUSDOTC:CALL:2026-10-15T12:00:00.000+00:00:tendência-1:300"


def decision(decision_id=DECISION_ID, **changes):
    values = dict(
        decision_id=decision_id,
        captured_at="2026-10-15T12:00:00.000+00:00",
        valid_until="2026-10-15T12:00:05.000+00:00",
        consumed_at="2026-10-15T12:00:01.000+00:00",
        payout=92.0,
        rule="tendência de 300 s",
    )
    values.update(changes)
    return ConsumedDecision(**values)


class DecisionTestCase(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "state.db")

    def open(self, resume=True):
        coordinator = Coordinator(SqliteStore(self.path))
        self.addCleanup(coordinator.close)
        if resume:
            coordinator.resume()
        return coordinator

    def query(self, sql, parameters=()):
        with closing(connect_read_only(self.path)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def settle(self, coordinator, result=WIN):
        reservation = coordinator.reserve_next_entry(BALANCE)
        coordinator.begin_submission(reservation.reservation_id)
        coordinator.record_result(reservation.reservation_id, result)
        return reservation

    def make_version_1(self):
        """A database as version 1 wrote it, with a won cycle."""
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in BASE_TABLES + MIGRATIONS[1]:
                connection.execute(statement)
            connection.execute("INSERT INTO cycles VALUES (1, 'EURUSD_otc', 'CALL', 'WIN')")
            connection.execute(
                "INSERT INTO reservations (reservation_id, cycle_id, attempt, stake_cents, result,"
                " submission_state, broker_order_id) VALUES ('r-1', 1, 1, 500, 'WIN', 'ACCEPTED', 'SIM-1')"
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()


class MigrationToVersion2Test(DecisionTestCase):
    def test_a_version_1_database_gains_the_decisions_table_and_keeps_its_rows(self):
        self.make_version_1()
        before = self.query("SELECT * FROM cycles"), self.query("SELECT * FROM reservations")
        coordinator = self.open()
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(self.query("PRAGMA user_version"), [(2,)])
        self.assertEqual((self.query("SELECT * FROM cycles"), self.query("SELECT * FROM reservations")), before)
        self.assertEqual(self.query("SELECT * FROM decisions"), [])
        status = coordinator.status()
        self.assertEqual((status.cycle_id, status.outcome, status.decision_id), (1, WIN, None))
        self.assertEqual(coordinator.start_cycle(ASSET, PUT, decision()), 2)
        self.assertEqual(self.query("SELECT decision_id, cycle_id FROM decisions"), [(DECISION_ID, 2)])

    def test_a_version_0_database_goes_through_both_migrations(self):
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in BASE_TABLES:
                connection.execute(statement)
            connection.execute("INSERT INTO cycles VALUES (1, 'EURUSD_otc', 'CALL', 'LOSS')")
            for attempt, stake in enumerate((500, 1000, 2000, 4000), 1):
                connection.execute(
                    "INSERT INTO reservations VALUES (?, 1, ?, ?, 'LOSS')", (f"old-{attempt}", attempt, stake)
                )
            connection.commit()
        coordinator = self.open()
        self.assertEqual(self.query("PRAGMA user_version"), [(SCHEMA_VERSION,)])
        self.assertEqual({row[0] for row in self.query("SELECT submission_state FROM reservations")}, {UNVERIFIED})
        self.assertEqual(self.query("SELECT COUNT(*) FROM decisions"), [(0,)])
        self.assertEqual(coordinator.status().outcome, LOSS)

    def test_a_failed_migration_2_leaves_version_1_untouched(self):
        self.make_version_1()
        # A table with the name that migration 2 creates makes it fail.
        self.execute("CREATE TABLE decisions (other TEXT)")
        before = self.query("SELECT * FROM cycles"), self.query("SELECT * FROM reservations")
        with self.assertRaisesRegex(StorageError, "migrating the database failed"):
            SqliteStore(self.path)
        self.assertEqual(self.query("PRAGMA user_version"), [(1,)])
        self.assertEqual([row[1] for row in self.query("PRAGMA table_info(decisions)")], ["other"])
        self.assertEqual((self.query("SELECT * FROM cycles"), self.query("SELECT * FROM reservations")), before)
        self.execute("DROP TABLE decisions")
        self.open()  # the failed open released the lock
        self.assertEqual(self.query("PRAGMA user_version"), [(2,)])


class DecisionsTest(DecisionTestCase):
    def test_the_decision_is_saved_with_its_cycle(self):
        coordinator = self.open()
        self.assertIsNone(coordinator.decision_cycle(DECISION_ID))
        self.assertEqual(coordinator.start_cycle(ASSET, CALL, decision()), 1)
        self.assertEqual(
            self.query("SELECT * FROM decisions"),
            [(DECISION_ID, 1, "2026-10-15T12:00:00.000+00:00", "2026-10-15T12:00:05.000+00:00",
              "2026-10-15T12:00:01.000+00:00", 92.0, "tendência de 300 s")],
        )
        self.assertEqual(coordinator.status().decision_id, DECISION_ID)
        self.assertEqual(coordinator.decision_cycle(DECISION_ID), 1)
        self.settle(coordinator)
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)  # without a decision: no row
        self.assertIsNone(coordinator.status().decision_id)
        self.assertEqual(self.query("SELECT cycle_id FROM decisions"), [(1,)])

    def test_a_decision_starts_one_cycle_at_most(self):
        coordinator = self.open()
        coordinator.start_cycle(ASSET, CALL, decision())
        # During the cycle, the reason is the decision, not the open cycle.
        with self.assertRaisesRegex(DecisionAlreadyUsedError, "already started cycle 1"):
            coordinator.start_cycle(ASSET, CALL, decision())
        self.settle(coordinator)
        for asset, direction in ((ASSET, CALL), ("GBPUSD_otc", PUT)):
            with self.subTest(asset=asset, direction=direction):
                with self.assertRaises(DecisionAlreadyUsedError):
                    coordinator.start_cycle(asset, direction, decision(payout=95.0))
        self.assertEqual(self.query("SELECT COUNT(*) FROM cycles"), [(1,)])
        self.assertEqual(coordinator.start_cycle(ASSET, CALL, decision("another decision")), 2)

    def test_the_used_decision_survives_a_restart(self):
        coordinator = self.open()
        coordinator.start_cycle(ASSET, CALL, decision())
        self.settle(coordinator, LOSS)  # the cycle stays open, waiting for its gale
        coordinator.close()
        restored = self.open(resume=False)  # paused, with the stale cycle
        self.assertEqual(restored.decision_cycle(DECISION_ID), 1)
        self.assertEqual(restored.status().decision_id, DECISION_ID)
        with self.assertRaises(DecisionAlreadyUsedError):
            restored.start_cycle(ASSET, CALL, decision())
        restored.abandon_cycle()
        restored.resume()
        with self.assertRaises(DecisionAlreadyUsedError):
            restored.start_cycle(ASSET, CALL, decision())
        self.assertEqual(self.query("SELECT COUNT(*) FROM cycles"), [(1,)])

    def test_saving_the_cycle_and_its_decision_is_atomic(self):
        coordinator = self.open()
        self.execute(
            "CREATE TRIGGER fail_decision BEFORE INSERT ON decisions"
            " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        with self.assertRaisesRegex(StorageError, "simulated write failure"):
            coordinator.start_cycle(ASSET, CALL, decision())
        self.assertEqual(self.query("SELECT COUNT(*) FROM cycles"), [(0,)])  # the cycle was rolled back too
        self.assertIsNone(coordinator.status().cycle_id)
        self.assertIsNone(coordinator.decision_cycle(DECISION_ID))
        self.execute("DROP TRIGGER fail_decision")
        self.assertEqual(coordinator.start_cycle(ASSET, CALL, decision()), 1)

    def test_an_invalid_decision_is_refused_before_anything_changes(self):
        coordinator = self.open()
        invalid = (
            "not a decision",
            decision(""),
            decision("   "),
            decision(captured_at=None),
            decision(valid_until=""),
            decision(consumed_at=5),
            decision(payout=True),
            decision(payout=math.nan),
            decision(payout="92"),
            decision(rule=3),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    coordinator.start_cycle(ASSET, CALL, value)
        self.assertEqual(self.query("SELECT COUNT(*) FROM cycles"), [(0,)])
        self.assertEqual(coordinator.start_cycle(ASSET, CALL, decision(payout=None, rule=None)), 1)

    def test_a_decision_of_a_missing_cycle_is_an_inconsistent_state(self):
        self.open().close()
        self.execute(  # a raw connection does not check the foreign key
            "INSERT INTO decisions VALUES ('orphan', 7, 'a', 'b', 'c', NULL, NULL)"
        )
        with self.assertRaisesRegex(StorageError, "inconsistent saved state: decisions of cycles that were never saved"):
            Coordinator(SqliteStore(self.path))
        self.execute("DELETE FROM decisions")
        self.open()  # the refused restore released the lock

    def test_without_a_store_the_decisions_are_kept_in_memory(self):
        coordinator = Coordinator()
        coordinator.start_cycle(ASSET, CALL, decision())
        self.settle(coordinator)
        with self.assertRaises(DecisionAlreadyUsedError):
            coordinator.start_cycle(ASSET, CALL, decision())
        self.assertEqual(coordinator.decision_cycle(DECISION_ID), 1)


class PauseTest(DecisionTestCase):
    def test_pause_stops_new_cycles_and_entries_until_resume(self):
        coordinator = self.open()
        coordinator.start_cycle(ASSET, CALL)
        submitted = coordinator.reserve_next_entry(BALANCE)
        coordinator.begin_submission(submitted.reservation_id)
        coordinator.pause()
        status = coordinator.status()
        self.assertTrue(status.is_paused)
        self.assertFalse(status.can_start_cycle)
        coordinator.record_result(submitted.reservation_id, LOSS)  # results still arrive
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        with self.assertRaisesRegex(CoordinatorError, "paused"):
            coordinator.start_cycle(ASSET, PUT)
        coordinator.abandon_cycle()  # allowed while paused, as after a restart
        with self.assertRaisesRegex(CoordinatorError, "paused"):
            coordinator.start_cycle(ASSET, PUT)
        coordinator.resume()
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
        reserved = coordinator.reserve_next_entry(BALANCE)
        coordinator.pause()
        with self.assertRaisesRegex(CoordinatorError, "paused"):
            coordinator.begin_submission(reserved.reservation_id)
        self.assertTrue(coordinator.close_not_submitted(reserved.reservation_id))


if __name__ == "__main__":
    unittest.main()
