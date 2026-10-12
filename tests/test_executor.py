"""Tests of the initial submission of an order by OrderExecutor.

FakeBroker is a test double: its order ids and answers are fake test data, and nothing
here reaches a real broker.
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing

from pocketautonomo.broker import Broker, OrderRequest, SubmissionResponse
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import CALL, LOSS, PUT, REJECTED, UNKNOWN
from pocketautonomo.executor import (
    ACCEPTANCE_RECORDED,
    NOT_STARTED,
    REJECTION_RECORDED,
    UNCERTAIN,
    OrderExecutor,
)
from pocketautonomo.storage import SqliteStore, connect_read_only
from pocketautonomo.submission import ACCEPTED, RESERVED, SUBMITTING

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents
THREADS = 16


class FakeBroker(Broker):
    """Records every order it receives and answers with reply(request).

    reply returns a SubmissionResponse or raises, like a broker that timed out.
    """

    def __init__(self, reply, delay=0.0):
        self.reply = reply
        self.delay = delay
        self.requests = []
        self._lock = threading.Lock()

    def submit_order(self, request):
        with self._lock:
            self.requests.append(request)
        time.sleep(self.delay)  # lets simultaneous callers overlap
        return self.reply(request)


def accept(order_id):
    """A reply that accepts every order as order_id."""
    return lambda request: SubmissionResponse.accepted(order_id)


class ExecutorTestCase(unittest.TestCase):
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
        """A resumed coordinator with an open cycle."""
        coordinator = self.open()
        coordinator.resume()
        coordinator.start_cycle(ASSET, direction)
        return coordinator

    def restart(self, previous):
        """Close the previous instance, then open the database again."""
        previous.close()
        return self.open()

    def saved(self, reservation_id):
        """(submission_state, broker_order_id, result) as committed in the database."""
        with closing(connect_read_only(self.path)) as connection:
            return connection.execute(
                "SELECT submission_state, broker_order_id, result FROM reservations"
                " WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()

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


class StartRefusedTest(ExecutorTestCase):
    def test_refused_or_failed_start_never_calls_the_broker(self):
        broker = FakeBroker(accept("FAKE-ORD-1"))
        before = self.running()
        restored = before.reserve_next_entry(BALANCE)  # the process stops before sending
        coordinator = self.restart(before)  # opens paused, in recovery
        executor = OrderExecutor(coordinator, broker)
        paused = executor.submit(restored.reservation_id)
        coordinator.resume()
        after_resume = executor.submit(restored.reservation_id)
        unknown = executor.submit("unknown-id")
        for report, message in (
            (paused, "paused"),
            (after_resume, "a restored reservation is never submitted"),
            (unknown, "unknown reservation id"),
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    (report.outcome, report.request, report.response), (NOT_STARTED, None, None)
                )
                self.assertEqual(len(report.errors), 1)
                self.assertIn(message, report.errors[0])
        self.assertEqual(broker.requests, [])
        status = coordinator.status()  # left as it was: never closed automatically
        self.assertEqual(
            (status.pending, status.submission_state, status.outcome), (restored, RESERVED, None)
        )
        self.assertEqual(self.saved(restored.reservation_id), (RESERVED, None, None))

        # A start whose write fails sends nothing either.
        self.path = os.path.join(self.directory, "failing.db")
        coordinator = self.running()
        reservation = coordinator.reserve_next_entry(BALANCE)
        undo = self.fail_updates()
        report = OrderExecutor(coordinator, broker).submit(reservation.reservation_id)
        undo()
        self.assertEqual((report.outcome, report.request), (NOT_STARTED, None))
        self.assertEqual(
            report.errors,
            ("StorageError: saving the submission start failed: simulated write failure",),
        )
        self.assertEqual(broker.requests, [])
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.outcome),
            (reservation, RESERVED, None),
        )
        self.assertIn("simulated write failure", status.submission_failure)
        self.assertEqual(self.saved(reservation.reservation_id), (RESERVED, None, None))


class AcceptanceTest(ExecutorTestCase):
    def test_accepted_order_is_recorded_and_the_bet_stays_pending(self):
        coordinator = self.running(PUT)
        order_ids = iter(("FAKE-ORD-1", "FAKE-ORD-2"))
        seen_by_broker = []  # what the database held when each order left

        def accept_next(request):
            seen_by_broker.append(self.saved(request.reservation_id))
            return SubmissionResponse.accepted(next(order_ids))

        broker = FakeBroker(accept_next)
        executor = OrderExecutor(coordinator, broker)
        first = coordinator.reserve_next_entry(BALANCE)
        self.assertEqual(executor.submit(first.reservation_id).outcome, ACCEPTANCE_RECORDED)
        coordinator.record_result(first.reservation_id, LOSS)  # settled elsewhere
        second = coordinator.reserve_next_entry(BALANCE)  # attempt 2

        report = executor.submit(second.reservation_id)
        sent = OrderRequest(second.reservation_id, ASSET, PUT, 1000)
        self.assertEqual(
            (report.outcome, report.request, report.broker_order_id, report.errors),
            (ACCEPTANCE_RECORDED, sent, "FAKE-ORD-2", ()),
        )
        self.assertFalse(report.unknown_recorded)
        self.assertEqual(
            broker.requests, [OrderRequest(first.reservation_id, ASSET, PUT, 500), sent]
        )
        # SUBMITTING was committed before each order left.
        self.assertEqual(seen_by_broker, [(SUBMITTING, None, None)] * 2)
        self.assertEqual(self.saved(second.reservation_id), (ACCEPTED, "FAKE-ORD-2", None))
        status = coordinator.status()  # accepted, but the bet has no result yet
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (second, ACCEPTED, "FAKE-ORD-2"),
        )
        self.assertEqual(
            (status.is_active, status.outcome, status.attempt, status.is_blocked),
            (True, None, 2, False),
        )
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))

        again = executor.submit(second.reservation_id)
        self.assertEqual(again.outcome, NOT_STARTED)
        self.assertIn("starts only once", again.errors[0])
        self.assertEqual(len(broker.requests), 2)  # no second send


class RejectionTest(ExecutorTestCase):
    def test_explicit_rejection_records_rejected_and_ends_the_cycle(self):
        coordinator = self.running()
        rejection = SubmissionResponse.rejected("fake rejection: asset closed")
        broker = FakeBroker(lambda request: rejection)
        executor = OrderExecutor(coordinator, broker)
        reservation = coordinator.reserve_next_entry(BALANCE)
        report = executor.submit(reservation.reservation_id)
        self.assertEqual(
            (report.outcome, report.response, report.broker_order_id, report.errors),
            (REJECTION_RECORDED, rejection, None, ()),
        )
        self.assertEqual(broker.requests, [OrderRequest(reservation.reservation_id, ASSET, CALL, 500)])
        self.assertEqual(self.saved(reservation.reservation_id), (SUBMITTING, None, REJECTED))
        status = coordinator.status()
        self.assertEqual(
            (status.outcome, status.is_active, status.pending, status.attempt),
            (REJECTED, False, None, 1),
        )
        self.assertEqual(executor.submit(reservation.reservation_id).outcome, NOT_STARTED)
        self.assertEqual(len(broker.requests), 1)
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))  # no gale
        self.assertEqual(coordinator.start_cycle(ASSET, CALL), 2)


class ConcurrencyTest(ExecutorTestCase):
    def test_simultaneous_submits_send_the_order_once(self):
        coordinator = self.running()
        broker = FakeBroker(accept("FAKE-ORD-1"), delay=0.05)
        executor = OrderExecutor(coordinator, broker)
        reservation = coordinator.reserve_next_entry(BALANCE)
        barrier = threading.Barrier(THREADS)
        reports = []
        reports_lock = threading.Lock()

        def worker():
            barrier.wait(timeout=5)
            report = executor.submit(reservation.reservation_id)
            with reports_lock:
                reports.append(report)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "a thread did not finish")

        self.assertEqual(
            broker.requests, [OrderRequest(reservation.reservation_id, ASSET, CALL, 500)]
        )
        self.assertEqual(
            sorted(report.outcome for report in reports),
            [ACCEPTANCE_RECORDED] + [NOT_STARTED] * (THREADS - 1),
        )
        refused = [report for report in reports if report.outcome == NOT_STARTED]
        self.assertTrue(all(report.request is None for report in refused))
        self.assertTrue(all("starts only once" in report.errors[0] for report in refused))
        self.assertEqual(self.saved(reservation.reservation_id), (ACCEPTED, "FAKE-ORD-1", None))


class TimeoutTest(ExecutorTestCase):
    def test_timeout_after_acceptance_keeps_the_pending_and_never_resends(self):
        coordinator = self.running()
        placed = []  # orders the fake broker accepted

        def accept_then_time_out(request):
            placed.append(request)  # the broker accepted the order...
            raise TimeoutError("fake timeout: the acceptance never arrived")  # ...answer lost

        broker = FakeBroker(accept_then_time_out)
        executor = OrderExecutor(coordinator, broker)
        reservation = coordinator.reserve_next_entry(BALANCE)
        report = executor.submit(reservation.reservation_id)
        self.assertEqual(report.outcome, UNCERTAIN)
        self.assertEqual(report.request, OrderRequest(reservation.reservation_id, ASSET, CALL, 500))
        self.assertEqual((report.response, report.broker_order_id), (None, None))
        self.assertTrue(report.unknown_recorded)
        self.assertEqual(report.errors, ("TimeoutError: fake timeout: the acceptance never arrived",))
        self.assertEqual(self.saved(reservation.reservation_id), (SUBMITTING, None, UNKNOWN))
        status = coordinator.status()  # neither LOSS nor NOT_SUBMITTED: still pending
        self.assertEqual(
            (status.pending, status.submission_state, status.is_blocked, status.outcome,
             status.attempt),
            (reservation, SUBMITTING, True, None, 1),
        )
        again = executor.submit(reservation.reservation_id)
        self.assertEqual(again.outcome, NOT_STARTED)
        self.assertIn("starts only once", again.errors[0])
        self.assertEqual((len(broker.requests), len(placed)), (1, 1))  # never sent again
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))

        # When recording UNKNOWN fails as well, the reservation still stays pending and
        # the report lists both errors.
        self.path = os.path.join(self.directory, "unknown-fails.db")
        coordinator = self.running()
        undo = None

        def time_out_while_the_database_fails(request):
            nonlocal undo
            undo = self.fail_updates()
            raise TimeoutError("fake timeout")

        broker = FakeBroker(time_out_while_the_database_fails)
        executor = OrderExecutor(coordinator, broker)
        reservation = coordinator.reserve_next_entry(BALANCE)
        report = executor.submit(reservation.reservation_id)
        undo()
        self.assertEqual((report.outcome, report.unknown_recorded), (UNCERTAIN, False))
        self.assertEqual(len(report.errors), 2)
        self.assertEqual(report.errors[0], "TimeoutError: fake timeout")
        self.assertIn("recording UNKNOWN failed", report.errors[1])
        self.assertIn("simulated write failure", report.errors[1])
        self.assertEqual(self.saved(reservation.reservation_id), (SUBMITTING, None, None))
        status = coordinator.status()
        self.assertEqual(
            (status.pending, status.submission_state, status.is_blocked, status.outcome),
            (reservation, SUBMITTING, False, None),
        )
        self.assertEqual(executor.submit(reservation.reservation_id).outcome, NOT_STARTED)
        self.assertEqual(len(broker.requests), 1)


class AcceptanceWriteFailureTest(ExecutorTestCase):
    def test_failed_acceptance_write_reports_the_received_order_id(self):
        coordinator = self.running()
        undo = None

        def accept_while_the_database_fails(request):
            nonlocal undo
            undo = self.fail_updates()  # the database fails while the order is out
            return SubmissionResponse.accepted("FAKE-ORD-7")

        broker = FakeBroker(accept_while_the_database_fails)
        executor = OrderExecutor(coordinator, broker)
        reservation = coordinator.reserve_next_entry(BALANCE)
        report = executor.submit(reservation.reservation_id)
        undo()
        self.assertEqual(report.outcome, UNCERTAIN)  # not reported as recorded
        self.assertEqual(report.response, SubmissionResponse.accepted("FAKE-ORD-7"))
        self.assertEqual(report.broker_order_id, "FAKE-ORD-7")  # the id actually received
        self.assertFalse(report.unknown_recorded)
        self.assertEqual(
            report.errors,
            (
                "recording the acceptance of order FAKE-ORD-7 failed: StorageError: saving"
                " the acceptance failed: simulated write failure",
            ),
        )
        self.assertEqual(self.saved(reservation.reservation_id), (SUBMITTING, None, None))
        status = coordinator.status()  # the existing stops are kept
        self.assertEqual(
            (status.pending, status.submission_state, status.broker_order_id),
            (reservation, SUBMITTING, None),
        )
        self.assertIn("simulated write failure", status.submission_failure)
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        self.assertEqual(executor.submit(reservation.reservation_id).outcome, NOT_STARTED)
        self.assertEqual(len(broker.requests), 1)

        # The reported id is what recovery needs: after reopening, still paused, the
        # acceptance can be confirmed with it.
        reopened = self.restart(coordinator)
        self.assertIsNone(reopened.status().submission_failure)
        self.assertTrue(
            reopened.confirm_acceptance(reservation.reservation_id, report.broker_order_id)
        )
        self.assertEqual(self.saved(reservation.reservation_id), (ACCEPTED, "FAKE-ORD-7", None))


if __name__ == "__main__":
    unittest.main()
