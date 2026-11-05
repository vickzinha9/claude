"""Tests of the paper broker, pocketautonomo.paper_broker: simulated orders saved in SQLite.

The clock is a fake that only the tests move, and the quotes are invented: they show the
simulator's rules, not how the Pocket Option settles an order.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from pocketautonomo import analysis
from pocketautonomo.broker import ORDER_ACCEPTED, ORDER_REJECTED, OrderRequest, OrderStatus
from pocketautonomo.cycle import CALL, CANCELLED, DRAW, LOSS, PUT, REJECTED, WIN
from pocketautonomo.executor import ORDER_OPEN
from pocketautonomo.paper_broker import (
    DEADLINE_PASSED,
    MAX_QUOTE_AGE_SECONDS,
    NEVER_ACCEPTED,
    NO_QUOTE_AT_EXPIRY,
    PAPER_ORDER_PREFIX,
    PaperBroker,
    PaperBrokerError,
    settlement,
)
from pocketautonomo.storage import DatabaseInUseError, StorageError

START = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ASSET = "EUR/USD OTC"
HORIZON = 300.0


class Clock:
    def __init__(self):
        self.t = START

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        return self.t


class PaperBrokerTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "ordens.db")
        self.clock = Clock()
        self.broker = self.open()

    def open(self):
        broker = PaperBroker(self.path, HORIZON, self.clock.now)
        self.addCleanup(broker.close)
        return broker

    def request(self, reservation_id="r-1", direction=CALL, stake=500, asset=ASSET):
        return OrderRequest(reservation_id, asset, direction, stake)

    def submit(self, reservation_id="r-1", direction=CALL, price=1.1, age=0.5):
        self.broker.observe(ASSET, self.clock.now() - timedelta(seconds=age), price, f"{price:.5f}")
        response = self.broker.submit_order(self.request(reservation_id, direction))
        self.assertEqual(response.status, ORDER_ACCEPTED, response)
        return response.broker_order_id


class SubmissionTest(PaperBrokerTestCase):
    def test_an_order_enters_with_the_latest_quote_and_is_saved_before_the_answer(self):
        self.broker.observe(ASSET, START - timedelta(seconds=3), 1.09, "1.09000")
        self.broker.observe("GBP/USD OTC", START, 1.27, "1.27000")  # another asset is never the entry
        self.broker.observe(ASSET, START - timedelta(seconds=1), 1.1, "1.10000")
        self.broker.observe(ASSET, START - timedelta(seconds=2), 1.0, "1.00000")  # older: not the latest
        response = self.broker.submit_order(self.request())
        self.assertEqual(response.status, ORDER_ACCEPTED)
        self.assertTrue(response.broker_order_id.startswith(PAPER_ORDER_PREFIX))
        with closing(sqlite3.connect(self.path)) as connection:  # committed already
            saved = connection.execute("SELECT order_id, state, entry_quote FROM paper_orders").fetchall()
        self.assertEqual(saved, [(response.broker_order_id, ORDER_OPEN, "1.10000")])
        order = self.broker.order(response.broker_order_id)
        self.assertEqual((order.entry_price, order.entry_captured_at), (1.1, START - timedelta(seconds=1)))
        # The horizon counts from the capture of the entry quote, as the analysis counts it.
        self.assertEqual(order.expires_at, START - timedelta(seconds=1) + timedelta(seconds=HORIZON))
        self.assertEqual(order.settle_by, order.expires_at + timedelta(seconds=analysis.tolerance(HORIZON)))
        self.assertEqual(self.broker.get_order(response.broker_order_id), OrderStatus(response.broker_order_id, ORDER_OPEN))

    def test_without_a_current_quote_the_order_is_explicitly_rejected_and_saved(self):
        response = self.broker.submit_order(self.request("r-1"))
        self.assertEqual((response.status, response.broker_order_id), (ORDER_REJECTED, None))
        self.assertIn("nenhuma cotação de EUR/USD OTC foi observada", response.reason)
        self.broker.observe(ASSET, START - timedelta(seconds=MAX_QUOTE_AGE_SECONDS + 0.1), 1.1, "1.10000")
        old = self.broker.submit_order(self.request("r-2"))
        self.assertEqual(old.status, ORDER_REJECTED)
        self.assertIn("mais que 5 s", old.reason)
        self.broker.observe(ASSET, START + timedelta(seconds=1), 1.1, "1.10000")
        future = self.broker.submit_order(self.request("r-3"))
        self.assertIn("horário depois de agora", future.reason)
        self.assertEqual([(order.state, order.order_id) for order in self.broker.orders()], [(REJECTED, None)] * 3)

    def test_a_request_past_its_deadline_is_rejected_and_never_placed(self):
        self.broker.observe(ASSET, START - timedelta(seconds=0.5), 1.1, "1.10000")
        late = OrderRequest("r-1", ASSET, CALL, 1000, START - timedelta(seconds=0.25))
        response = self.broker.submit_order(late)
        self.assertEqual((response.status, response.broker_order_id), (ORDER_REJECTED, None))
        self.assertTrue(response.reason.startswith(DEADLINE_PASSED), response.reason)
        self.assertIn("0.250 s antes do pedido chegar ao simulador", response.reason)
        in_time = self.broker.submit_order(OrderRequest("r-2", ASSET, CALL, 1000, START))
        self.assertEqual(in_time.status, ORDER_ACCEPTED)  # exactly at the deadline is still in time
        self.assertEqual([(order.reservation_id, order.state) for order in self.broker.orders()], [("r-1", REJECTED), ("r-2", ORDER_OPEN)])
        with self.assertRaises(ValueError):
            self.broker.submit_order(OrderRequest("r-3", ASSET, CALL, 500, START.replace(tzinfo=None)))

    def test_a_reservation_gets_one_order_at_most(self):
        self.submit("r-1")
        with self.assertRaisesRegex(PaperBrokerError, "nunca aceita uma segunda ordem"):
            self.broker.submit_order(self.request("r-1"))
        self.assertEqual(len(self.broker.orders()), 1)

    def test_invalid_requests_and_quotes_are_refused(self):
        for request in (
            "r-1",
            self.request(""),
            self.request(direction="UP"),
            self.request(stake=5.0),
            self.request(stake=True),
            self.request(asset=" "),
        ):
            with self.subTest(request=request):
                with self.assertRaises((TypeError, ValueError)):
                    self.broker.submit_order(request)
        for moment, price in ((START.replace(tzinfo=None), 1.1), (START, 0), (START, float("inf")), (START, True)):
            with self.subTest(moment=moment, price=price):
                with self.assertRaises(ValueError):
                    self.broker.observe(ASSET, moment, price)
        self.assertEqual(self.broker.orders(), [])


class SettlementTest(PaperBrokerTestCase):
    def test_the_first_quote_from_the_expiry_settles_the_order(self):
        cases = ((CALL, 1.2, WIN), (CALL, 1.0, LOSS), (PUT, 1.0, WIN), (PUT, 1.2, LOSS), (CALL, 1.1, DRAW), (PUT, 1.1, DRAW))
        for index, (direction, exit_price, result) in enumerate(cases):
            with self.subTest(direction=direction, exit_price=exit_price):
                self.clock.t = START + timedelta(hours=index)
                order_id = self.submit(f"r-{index}", direction)
                expiry = self.broker.order(order_id).expires_at
                self.assertEqual(self.broker.observe(ASSET, expiry - timedelta(seconds=0.1), 2.0), ())  # too early
                self.clock.t = expiry + timedelta(seconds=1)
                [settled] = self.broker.observe(ASSET, self.clock.now(), exit_price, f"{exit_price:.5f}")
                self.assertEqual((settled.order_id, settled.state, settled.exit_price), (order_id, result, exit_price))
                self.broker.observe(ASSET, self.clock.advance(1), 9.9)  # a later quote changes nothing
                self.assertEqual(self.broker.get_order(order_id).state, result)
        self.assertEqual(settlement(CALL, 1.0, 1.0), DRAW)

    def test_without_a_quote_in_time_the_order_is_cancelled_without_a_result(self):
        order_id = self.submit()
        order = self.broker.order(order_id)
        self.clock.t = order.settle_by
        self.assertEqual(self.broker.get_order(order_id).state, ORDER_OPEN)  # still in time
        self.clock.advance(0.001)
        self.assertEqual(self.broker.observe(ASSET, self.clock.now(), 1.2), ())  # too late to settle it
        self.assertEqual(self.broker.get_order(order_id).state, CANCELLED)
        cancelled = self.broker.order(order_id)
        self.assertEqual((cancelled.note, cancelled.exit_price), (NO_QUOTE_AT_EXPIRY, None))

    def test_unknown_ids_raise_key_error(self):
        for order_id in ("SIM-desconhecida", None, 3):
            with self.subTest(order_id=order_id):
                with self.assertRaises(KeyError):
                    self.broker.get_order(order_id)


class PersistenceTest(PaperBrokerTestCase):
    def test_orders_survive_a_restart_and_are_found_by_their_reservation(self):
        order_id = self.submit("r-1", PUT)
        self.broker.close()
        reopened = self.open()
        found = reopened.find_order_for("r-1")
        self.assertEqual((found.order_id, found.direction, found.state), (order_id, PUT, ORDER_OPEN))
        self.assertIsNone(reopened.find_order_for("r-2"))
        self.assertIsNone(reopened.latest(ASSET))  # the quotes lived only in memory
        self.clock.t = found.expires_at
        [settled] = reopened.observe(ASSET, self.clock.now(), 1.0)
        self.assertEqual(settled.state, WIN)

    def test_a_reservation_without_an_order_is_recorded_as_never_accepted(self):
        self.assertTrue(self.broker.confirm_absent(self.request("r-1")))
        self.assertTrue(self.broker.confirm_absent(self.request("r-1")))  # the same answer again
        absent = self.broker.find_order_for("r-1")
        self.assertEqual((absent.order_id, absent.state, absent.note), (None, REJECTED, NEVER_ACCEPTED))
        self.broker.observe(ASSET, START, 1.1)
        with self.assertRaises(PaperBrokerError):  # and it can never get one afterwards
            self.broker.submit_order(self.request("r-1"))
        self.submit("r-2")
        self.assertFalse(self.broker.confirm_absent(self.request("r-2")))  # it has an order

    def test_one_simulator_per_file_and_unknown_versions_are_refused(self):
        with self.assertRaises(DatabaseInUseError):
            PaperBroker(self.path, HORIZON, self.clock.now)
        self.broker.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 7")
            connection.commit()
        with self.assertRaisesRegex(StorageError, "schema version 7"):
            PaperBroker(self.path, HORIZON, self.clock.now)
        with self.assertRaises(ValueError):
            PaperBroker(os.path.join(os.path.dirname(self.path), "outro.db"), 0)


if __name__ == "__main__":
    unittest.main()
