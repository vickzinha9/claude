"""Tests of SimulatedBroker, the broker simulated in memory.

Its SIM-... order ids and the WIN and LOSS results given to settle_order() are simulated
test data: nothing here reaches a real broker.
"""

import os
import tempfile
import unittest
import uuid
from contextlib import closing

from pocketautonomo.broker import Broker, OrderRequest, OrderStatus, SubmissionResponse
from pocketautonomo.coordinator import Coordinator
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
from pocketautonomo.executor import (
    ACCEPTANCE_RECORDED,
    NO_PENDING,
    ORDER_OPEN,
    PENDING,
    RESULT_RECORDED,
    OrderExecutor,
    ReconciliationReport,
)
from pocketautonomo.simulated_broker import SimulatedBroker
from pocketautonomo.storage import SqliteStore, connect_read_only
from pocketautonomo.submission import ACCEPTED

ASSET = "SIM_ASSET"
BALANCE = 10_000  # a fictitious balance in cents, only to let the reservation through
PREFIX = "SIM-"


def request(number, direction=CALL, stake_cents=500):
    """An OrderRequest as the executor builds it, for a made-up reservation."""
    return OrderRequest(f"reservation-{number}", ASSET, direction, stake_cents)


class SimulatedBrokerTest(unittest.TestCase):
    def submit(self, broker, number=1):
        """Submit a made-up request to broker and return the order id it gave."""
        return broker.submit_order(request(number)).broker_order_id

    def assert_simulated_id(self, order_id):
        """order_id is SIM- followed by a uuid4 in its canonical form."""
        self.assertTrue(order_id.startswith(PREFIX), order_id)
        suffix = order_id[len(PREFIX):]
        parsed = uuid.UUID(suffix)
        self.assertEqual((str(parsed), parsed.version), (suffix, 4))

    def test_submit_accepts_with_distinct_ids_and_open_orders(self):
        broker = SimulatedBroker()
        self.assertIsInstance(broker, Broker)
        first, second = request(1, CALL, 500), request(2, PUT, 1000)
        responses = [broker.submit_order(first), broker.submit_order(second)]
        ids = [response.broker_order_id for response in responses]
        self.assertNotEqual(ids[0], ids[1])
        for response, order_id in zip(responses, ids):
            with self.subTest(order_id=order_id):
                self.assertEqual(response, SubmissionResponse.accepted(order_id))
                self.assert_simulated_id(order_id)
                self.assertEqual(broker.get_order(order_id), OrderStatus(order_id, ORDER_OPEN))

        history = broker.requests
        self.assertIsInstance(history, tuple)
        self.assertEqual(history, (first, second))  # in the order they arrived
        third = request(3)
        broker.submit_order(third)
        self.assertEqual(history, (first, second))  # an earlier tuple never changes
        self.assertEqual(broker.requests, (first, second, third))
        with self.assertRaises(AttributeError):
            broker.requests = ()  # read-only

    def test_get_order_answers_about_exactly_the_requested_id(self):
        broker = SimulatedBroker()
        first, second = self.submit(broker, 1), self.submit(broker, 2)
        self.assertTrue(broker.settle_order(first, WIN))
        self.assertEqual(broker.get_order(first), OrderStatus(first, WIN))
        self.assertEqual(broker.get_order(second), OrderStatus(second, ORDER_OPEN))  # untouched
        for wrong in (first[len(PREFIX):], first[:-1], first + "0", f" {first}"):
            with self.subTest(wrong=wrong):
                with self.assertRaises(KeyError):
                    broker.get_order(wrong)

    def test_settle_win_or_loss_once_and_refuse_a_different_result(self):
        for result, other in ((WIN, LOSS), (LOSS, WIN)):
            with self.subTest(result=result):
                broker = SimulatedBroker()
                order_id = self.submit(broker)
                self.assertTrue(broker.settle_order(order_id, result))
                self.assertEqual(broker.get_order(order_id), OrderStatus(order_id, result))
                self.assertFalse(broker.settle_order(order_id, result))  # the same again
                with self.assertRaisesRegex(ValueError, f"already settled as {result}"):
                    broker.settle_order(order_id, other)
                self.assertEqual(broker.get_order(order_id), OrderStatus(order_id, result))
                self.assertFalse(broker.settle_order(order_id, result))

    def test_unknown_id_and_invalid_result_are_refused(self):
        broker = SimulatedBroker()
        order_id = self.submit(broker)
        from_another_broker = self.submit(SimulatedBroker())  # memory is never shared
        for unknown in ("SIM-unknown", "", from_another_broker):
            with self.subTest(unknown=unknown):
                with self.assertRaisesRegex(KeyError, "unknown simulated order id"):
                    broker.get_order(unknown)
                with self.assertRaisesRegex(KeyError, "unknown simulated order id"):
                    broker.settle_order(unknown, WIN)
        for invalid in (DRAW, CANCELLED, REJECTED, UNKNOWN, ORDER_OPEN, "win", "", None):
            with self.subTest(result=invalid):
                with self.assertRaisesRegex(ValueError, "only as WIN or LOSS"):
                    broker.settle_order(order_id, invalid)
        self.assertEqual(broker.get_order(order_id), OrderStatus(order_id, ORDER_OPEN))
        self.assertEqual(broker.requests, (request(1),))


class EngineIntegrationTest(unittest.TestCase):
    """SimulatedBroker with Coordinator, SqliteStore and OrderExecutor."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "state.db")

    def test_simulated_win_ends_the_cycle_through_the_engine(self):
        coordinator = Coordinator(SqliteStore(self.path))
        self.addCleanup(coordinator.close)  # runs before the directory is removed
        coordinator.resume()
        coordinator.start_cycle(ASSET, CALL)
        broker = SimulatedBroker()
        executor = OrderExecutor(coordinator, broker)

        reservation = coordinator.reserve_next_entry(BALANCE)
        self.assertEqual(reservation.entry, Entry(ASSET, CALL, 1, 500))
        reservation_id = reservation.reservation_id
        sent = executor.submit(reservation_id)
        order_id = sent.broker_order_id
        self.assertEqual(sent.outcome, ACCEPTANCE_RECORDED)
        self.assertTrue(order_id.startswith(PREFIX), order_id)
        self.assertEqual(broker.requests, (OrderRequest(reservation_id, ASSET, CALL, 500),))
        self.assertEqual(coordinator.status().broker_order_id, order_id)

        # Still OPEN: the lookup changes nothing and the bet stays pending.
        self.assertEqual(broker.get_order(order_id), OrderStatus(order_id, ORDER_OPEN))
        self.assertEqual(
            executor.reconcile_pending(), ReconciliationReport(PENDING, reservation_id, order_id)
        )
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.is_active, status.outcome), (reservation, True, None)
        )

        self.assertTrue(broker.settle_order(order_id, WIN))
        self.assertEqual(
            executor.reconcile_pending(),
            ReconciliationReport(RESULT_RECORDED, reservation_id, order_id, WIN),
        )
        status = coordinator.status()
        self.assertEqual(
            (status.is_active, status.outcome, status.pending, status.attempt),
            (False, WIN, None, 1),
        )
        self.assertEqual(executor.reconcile_pending(), ReconciliationReport(NO_PENDING))
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))  # no gale after a WIN
        self.assertEqual(len(broker.requests), 1)  # sent once

        with closing(connect_read_only(self.path)) as connection:
            cycles = connection.execute(
                "SELECT cycle_id, asset, direction, outcome FROM cycles"
            ).fetchall()
            reservations = connection.execute(
                "SELECT reservation_id, attempt, stake_cents, result, submission_state,"
                " broker_order_id FROM reservations"
            ).fetchall()
        self.assertEqual(cycles, [(1, ASSET, CALL, WIN)])
        self.assertEqual(reservations, [(reservation_id, 1, 500, WIN, ACCEPTED, order_id)])


if __name__ == "__main__":
    unittest.main()
