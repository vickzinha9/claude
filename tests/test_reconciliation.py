"""Tests of OrderExecutor.reconcile_pending().

FakeBroker is a test double: its FAKE-... order ids and its answers are fake test data,
and nothing here reaches a real broker.
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing

from pocketautonomo.broker import Broker, OrderRequest, OrderStatus, SubmissionResponse
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import CALL, LOSS, PUT, UNKNOWN, WIN, Entry
from pocketautonomo.executor import (
    ACCEPTANCE_RECORDED,
    NO_ORDER_ID,
    NO_PENDING,
    ORDER_OPEN,
    PENDING,
    RECORD_FAILED,
    RESULT_ALREADY_RECORDED,
    RESULT_RECORDED,
    OrderExecutor,
    ReconciliationReport,
)
from pocketautonomo.storage import SqliteStore, connect_read_only
from pocketautonomo.submission import ACCEPTED, RESERVED, SUBMITTING

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents
ORDER_ID = "FAKE-ORD-1"  # the id FakeBroker gives to the first order it accepts


class FakeBroker(Broker):
    """Accepts every order as FAKE-ORD-<n> and answers each lookup with answer(order_id).

    answer returns what get_order() hands back, or raises like a broker that timed out.
    requests and lookups record every call, so a test can tell what was sent and which
    order was looked up.
    """

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.lookups = []

    def submit_order(self, request):
        self.requests.append(request)
        return SubmissionResponse.accepted(f"FAKE-ORD-{len(self.requests)}")

    def get_order(self, broker_order_id):
        self.lookups.append(broker_order_id)
        return self.answer(broker_order_id)


def settled(state):
    """An answer that reports the looked-up order in state."""
    return lambda order_id: OrderStatus(order_id, state)


def settled_by_order(results):
    """An answer that reports order FAKE-ORD-<n> with the n-th of results."""
    states = {f"FAKE-ORD-{n}": result for n, result in enumerate(results, start=1)}
    return lambda order_id: OrderStatus(order_id, states[order_id])


def time_out(order_id):
    raise TimeoutError("fake timeout: no answer about the order")


class ReconciliationTest(unittest.TestCase):
    """Every test uses its own database inside a temporary directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.path = os.path.join(self.directory, "state.db")

    def open(self):
        coordinator = Coordinator(SqliteStore(self.path))
        self.addCleanup(coordinator.close)
        return coordinator

    def running(self, direction=CALL):
        """A resumed coordinator with an open cycle, CALL unless told otherwise."""
        coordinator = self.open()
        coordinator.resume()
        coordinator.start_cycle(ASSET, direction)
        return coordinator

    def submitted(self, broker):
        """Send the first entry of a new cycle through broker, which accepts it.

        Returns the coordinator, its executor and the reservation that was sent.
        """
        coordinator = self.running()
        executor = OrderExecutor(coordinator, broker)
        reservation = coordinator.reserve_next_entry(BALANCE)
        report = executor.submit(reservation.reservation_id)
        self.assertEqual(
            (report.outcome, report.broker_order_id), (ACCEPTANCE_RECORDED, ORDER_ID)
        )
        return coordinator, executor, reservation

    def saved(self, reservation_id):
        """(submission_state, broker_order_id, result) as committed in the database."""
        with closing(connect_read_only(self.path)) as connection:
            return connection.execute(
                "SELECT submission_state, broker_order_id, result FROM reservations"
                " WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()

    def rows(self):
        """Every committed row of the cycles and of the reservations."""
        with closing(connect_read_only(self.path)) as connection:
            return (
                connection.execute("SELECT * FROM cycles ORDER BY cycle_id").fetchall(),
                connection.execute(
                    "SELECT * FROM reservations ORDER BY cycle_id, attempt"
                ).fetchall(),
            )

    def fail_updates(self):
        """Make every UPDATE on reservations fail inside SQLite; return the undo function."""
        path = self.path

        def execute(sql):
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(sql)
                connection.commit()

        execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON reservations"
            " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        return lambda: execute("DROP TRIGGER fail_update")

    def test_loss_advances_once(self):
        broker = FakeBroker(settled(LOSS))
        coordinator, executor, reservation = self.submitted(broker)
        reservation_id = reservation.reservation_id

        report = executor.reconcile_pending()
        self.assertEqual(
            report, ReconciliationReport(RESULT_RECORDED, reservation_id, ORDER_ID, LOSS)
        )
        self.assertEqual(broker.lookups, [ORDER_ID])  # only the order linked to it
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, LOSS))  # committed
        status = coordinator.status()
        self.assertEqual(
            (status.attempt, status.pending, status.is_active, status.outcome),
            (2, None, True, None),
        )

        rows = self.rows()
        self.assertEqual(executor.reconcile_pending(), ReconciliationReport(NO_PENDING))
        self.assertEqual(coordinator.status(), status)  # it did not advance again
        self.assertEqual(self.rows(), rows)
        self.assertEqual(broker.lookups, [ORDER_ID])  # and looked nothing up
        self.assertEqual(broker.requests, [OrderRequest(reservation_id, ASSET, CALL, 500)])

        following = coordinator.reserve_next_entry(BALANCE)  # the gale after the loss
        self.assertEqual(following.entry, Entry(ASSET, CALL, 2, 1000))
        self.assertEqual(len(broker.requests), 1)  # reserving sends nothing

    def test_win_after_restart_ends_cycle(self):
        broker = FakeBroker(settled(WIN))
        before, _, reservation = self.submitted(broker)
        reservation_id = reservation.reservation_id
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, None))
        before.close()

        after = self.open()  # restored paused, with its cycle still open
        status = after.status()
        self.assertEqual(
            (status.is_paused, status.recovery_pending, status.pending, status.broker_order_id),
            (True, True, reservation, ORDER_ID),
        )
        report = OrderExecutor(after, broker).reconcile_pending()  # while still paused
        self.assertEqual(
            report, ReconciliationReport(RESULT_RECORDED, reservation_id, ORDER_ID, WIN)
        )
        self.assertEqual(broker.lookups, [ORDER_ID])  # the restored link, nothing else
        status = after.status()
        self.assertEqual(
            (status.is_paused, status.outcome, status.is_active, status.pending,
             status.recovery_pending),
            (True, WIN, False, None, False),
        )
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, WIN))
        cycles, _ = self.rows()
        self.assertEqual(cycles, [(1, ASSET, CALL, WIN)])  # the end of the cycle is committed
        self.assertEqual(broker.requests, [OrderRequest(reservation_id, ASSET, CALL, 500)])

    def test_without_order_id_does_not_query(self):
        for state in (RESERVED, SUBMITTING):
            with self.subTest(state=state):
                self.path = os.path.join(self.directory, f"{state.lower()}.db")
                coordinator = self.running()
                reservation = coordinator.reserve_next_entry(BALANCE)
                if state == SUBMITTING:
                    # Its submission started, but no acceptance was recorded for it.
                    coordinator.begin_submission(reservation.reservation_id)
                broker = FakeBroker(settled(WIN))  # it would answer WIN if it were asked
                before, rows = coordinator.status(), self.rows()
                self.assertEqual(
                    (before.pending, before.submission_state, before.broker_order_id),
                    (reservation, state, None),
                )

                report = OrderExecutor(coordinator, broker).reconcile_pending()
                self.assertEqual(
                    (report.outcome, report.reservation_id, report.broker_order_id,
                     report.result),
                    (NO_ORDER_ID, reservation.reservation_id, None, None),
                )
                self.assertIn(f"is {state} without a linked broker order id", report.error)
                self.assertEqual(coordinator.status(), before)
                self.assertEqual(self.rows(), rows)
                self.assertEqual((broker.requests, broker.lookups), ([], []))

    def test_unsettled_or_invalid_answer_keeps_pending(self):
        # (case, answer of get_order, part of the expected error or None). The answer
        # about another order and the one that is not an OrderStatus carry WIN, which
        # must never be applied.
        cases = (
            ("OPEN", settled(ORDER_OPEN), None),
            ("UNKNOWN", settled(UNKNOWN), None),
            ("timeout", time_out, f"looking up order {ORDER_ID} failed: TimeoutError"),
            (
                "another order id",
                lambda order_id: OrderStatus("FAKE-ORD-9", WIN),
                f"about order 'FAKE-ORD-9' instead of order '{ORDER_ID}'",
            ),
            (
                "not an OrderStatus",
                lambda order_id: {"broker_order_id": order_id, "state": WIN},
                f"invalid broker answer for order {ORDER_ID}",
            ),
            ("unknown state", settled("SETTLED"), f"unknown state 'SETTLED' of order {ORDER_ID}"),
        )
        for number, (case, answer, error) in enumerate(cases):
            with self.subTest(case=case):
                self.path = os.path.join(self.directory, f"case{number}.db")
                broker = FakeBroker(answer)
                coordinator, executor, reservation = self.submitted(broker)
                before, rows = coordinator.status(), self.rows()

                report = executor.reconcile_pending()
                self.assertEqual(
                    (report.outcome, report.reservation_id, report.broker_order_id,
                     report.result),
                    (PENDING, reservation.reservation_id, ORDER_ID, None),
                )
                if error is None:
                    self.assertIsNone(report.error)
                else:
                    self.assertIn(error, report.error)
                self.assertEqual(broker.lookups, [ORDER_ID])
                self.assertEqual(coordinator.status(), before)  # still pending, unchanged
                self.assertEqual(self.rows(), rows)
                self.assertEqual(len(broker.requests), 1)  # nothing sent again

    def test_concurrent_reconciliation_records_once(self):
        both_looked_up = threading.Barrier(2)

        def loss_once_both_looked_up(order_id):
            # Neither lookup answers before both callers captured the same pending.
            both_looked_up.wait(timeout=5)
            return OrderStatus(order_id, LOSS)

        broker = FakeBroker(loss_once_both_looked_up)
        coordinator, executor, reservation = self.submitted(broker)
        reservation_id = reservation.reservation_id
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, None))
        reports, errors = [], []

        def reconcile():
            try:
                reports.append(executor.reconcile_pending())
            except Exception as error:  # handed back to the test thread
                errors.append(error)

        threads = [threading.Thread(target=reconcile, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "a thread did not finish")
        self.assertEqual(errors, [])

        self.assertCountEqual(
            [report.outcome for report in reports], [RESULT_RECORDED, RESULT_ALREADY_RECORDED]
        )
        self.assertEqual(
            {(report.reservation_id, report.broker_order_id, report.result, report.error)
             for report in reports},
            {(reservation_id, ORDER_ID, LOSS, None)},
        )
        self.assertEqual(broker.lookups, [ORDER_ID, ORDER_ID])
        status = coordinator.status()
        self.assertEqual((status.attempt, status.pending, status.is_active), (2, None, True))
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, LOSS))
        self.assertEqual(broker.requests, [OrderRequest(reservation_id, ASSET, CALL, 500)])

    def test_failed_result_write_keeps_pending(self):
        broker = FakeBroker(settled(LOSS))
        coordinator, executor, reservation = self.submitted(broker)
        reservation_id = reservation.reservation_id
        before, rows = coordinator.status(), self.rows()
        undo = self.fail_updates()  # SQLite now refuses every change to the reservations

        report = executor.reconcile_pending()
        self.assertEqual(
            (report.outcome, report.reservation_id, report.broker_order_id, report.result),
            (RECORD_FAILED, reservation_id, ORDER_ID, LOSS),
        )
        self.assertIn(f"recording the result {LOSS} of order {ORDER_ID} failed", report.error)
        self.assertIn("simulated write failure", report.error)
        self.assertEqual(coordinator.status(), before)  # still pending, unchanged
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, None))

        undo()
        retry = executor.reconcile_pending()
        self.assertEqual(
            retry, ReconciliationReport(RESULT_RECORDED, reservation_id, ORDER_ID, LOSS)
        )
        self.assertEqual(self.saved(reservation_id), (ACCEPTED, ORDER_ID, LOSS))
        self.assertEqual(coordinator.status().attempt, 2)  # recorded once: not attempt 3
        following = coordinator.reserve_next_entry(BALANCE)
        self.assertEqual(following.entry, Entry(ASSET, CALL, 2, 1000))
        self.assertEqual(broker.lookups, [ORDER_ID, ORDER_ID])  # looked up again to retry
        self.assertEqual(broker.requests, [OrderRequest(reservation_id, ASSET, CALL, 500)])

    def test_progression_stops_at_four_losses_or_win(self):
        scenarios = (
            ("four losses", CALL, (LOSS, LOSS, LOSS, LOSS), LOSS),
            ("loss, loss, win", PUT, (LOSS, LOSS, WIN), WIN),
        )
        for number, (scenario, direction, results, outcome) in enumerate(scenarios):
            with self.subTest(scenario=scenario):
                self.path = os.path.join(self.directory, f"scenario{number}.db")
                order_ids = [f"FAKE-ORD-{n}" for n in range(1, len(results) + 1)]
                broker = FakeBroker(settled_by_order(results))
                coordinator = self.running(direction)
                executor = OrderExecutor(coordinator, broker)
                expected_requests, expected_rows = [], []
                steps = zip((500, 1000, 2000, 4000), order_ids, results)
                for attempt, (stake, order_id, result) in enumerate(steps, start=1):
                    reservation = coordinator.reserve_next_entry(BALANCE)
                    self.assertEqual(reservation.entry, Entry(ASSET, direction, attempt, stake))
                    reservation_id = reservation.reservation_id
                    sent = executor.submit(reservation_id)
                    self.assertEqual(
                        (sent.outcome, sent.broker_order_id), (ACCEPTANCE_RECORDED, order_id)
                    )
                    self.assertEqual(coordinator.status().broker_order_id, order_id)  # linked
                    self.assertEqual(
                        executor.reconcile_pending(),
                        ReconciliationReport(RESULT_RECORDED, reservation_id, order_id, result),
                    )
                    expected_requests.append(OrderRequest(reservation_id, ASSET, direction, stake))
                    expected_rows.append(
                        (reservation_id, 1, attempt, stake, result, ACCEPTED, order_id)
                    )

                status = coordinator.status()
                self.assertEqual(
                    (status.is_active, status.outcome, status.attempt, status.pending),
                    (False, outcome, len(results), None),
                )
                self.assertIsNone(coordinator.reserve_next_entry(BALANCE))  # no further entry
                self.assertEqual(executor.reconcile_pending(), ReconciliationReport(NO_PENDING))
                self.assertEqual(broker.requests, expected_requests)  # each sent once
                self.assertEqual(broker.lookups, order_ids)  # each linked order looked up once
                cycles, saved = self.rows()
                self.assertEqual(cycles, [(1, ASSET, direction, outcome)])
                self.assertEqual(saved, expected_rows)  # linked ids and results committed


if __name__ == "__main__":
    unittest.main()
