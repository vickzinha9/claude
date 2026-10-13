"""Tests of OrderExecutor.reconcile_pending().

FakeBroker is a test double: its FAKE-... order ids and its answers are fake test data,
and nothing here reaches a real broker.
"""

import os
import tempfile
import unittest
from contextlib import closing

from pocketautonomo.broker import Broker, OrderRequest, OrderStatus, SubmissionResponse
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import CALL, LOSS, UNKNOWN, WIN, Entry
from pocketautonomo.executor import (
    ACCEPTANCE_RECORDED,
    NO_ORDER_ID,
    NO_PENDING,
    ORDER_OPEN,
    PENDING,
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

    def running(self):
        """A resumed coordinator with an open CALL cycle."""
        coordinator = self.open()
        coordinator.resume()
        coordinator.start_cycle(ASSET, CALL)
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


if __name__ == "__main__":
    unittest.main()
