"""Tests of the simulated operation, pocketautonomo.operation: decision -> coordinator -> simulator.

Every test runs the real Coordinator with its SQLite store and the real PaperBroker, in a
temporary directory, with a fake clock that only the tests move. The decisions, quotes and
results are ARTIFICIAL, built here: they test the rules of the operation and never touch
the scan's CSV files, the only data of the analysis.
"""

import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pocketautonomo import analysis, operation
from pocketautonomo.broker import OrderRequest
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.cycle import ABANDONED, CALL, CANCELLED, DRAW, LOSS, PUT, REJECTED, UNKNOWN, WIN
from pocketautonomo.operation import (
    ACTIVE_CYCLE,
    COLLECTING,
    OPERATION_COLUMNS,
    PAUSED,
    PENDING_ENTRY,
    WAITING_DECISION,
    OperationLog,
    OperationStopped,
    Operator,
)
from pocketautonomo.paper_broker import PaperBroker
from pocketautonomo.storage import SqliteStore, StorageError, connect_read_only
from pocketautonomo.submission import ACCEPTED, NOT_SUBMITTED, RESERVED

START = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ASSET = "EUR/USD OTC"
HORIZON = 300.0
STAKES = [500, 1000, 2000, 4000]


class Clock:
    def __init__(self):
        self.t = START
        self.m = 1000.0

    def now(self):
        return self.t

    def monotonic(self):
        return self.m

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        self.m += seconds


class LostAnswer(PaperBroker):
    """Saves the order and then loses the answer of the next submission: UNKNOWN."""

    lose = 0

    def submit_order(self, request):
        response = super().submit_order(request)
        if self.lose:
            self.lose -= 1
            raise TimeoutError("resposta perdida depois de gravar a ordem (simulado)")
        return response


class NeverReached(PaperBroker):
    """The next submission fails before anything is saved: UNKNOWN, and no order exists."""

    fail = 0

    def submit_order(self, request):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("o pedido não chegou ao simulador (simulado)")
        return super().submit_order(request)


class SlowReservation(SqliteStore):
    """Saving a reservation takes `delay` seconds of the test's clock."""

    clock = None
    delay = 0.0

    def save_reservation(self, reservation):
        super().save_reservation(reservation)
        self.clock.advance(self.delay)


class FailingStart(SqliteStore):
    """Saving the start of a submission fails, as a full or broken disk would."""

    def save_submission_start(self, reservation_id):
        raise StorageError("simulated write failure")


class SlowStart(SqliteStore):
    """Saving the start of a submission takes `delay` seconds of the test's clock."""

    clock = None
    delay = 0.0

    def save_submission_start(self, reservation_id):
        super().save_submission_start(reservation_id)
        self.clock.advance(self.delay)


class OrderAppearsLate(PaperBroker):
    """The first lookup by reservation misses an order that the simulator did save."""

    missed = 0

    def find_order_for(self, reservation_id):
        if self.missed:
            self.missed -= 1
            return None
        return super().find_order_for(reservation_id)


class OperationTestCase(unittest.TestCase):
    """Each test has its own directory; start() opens the operation, restart() reopens it."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.clock = Clock()
        self.price = 1.10000
        self.lines = []
        self.logs = []
        self.coordinator = self.broker = None
        self.addCleanup(self.close)

    def start(self, store=SqliteStore, broker=PaperBroker, gale_window=operation.GALE_WINDOW_SECONDS):
        self.store = store(self.directory / "operacao-simulada.db")
        if isinstance(self.store, (SlowReservation, SlowStart)):
            self.store.clock = self.clock
        self.coordinator = Coordinator(self.store)
        self.broker = broker(self.directory / "operacao-simulada-ordens.db", HORIZON, self.clock.now)
        self.log = OperationLog(self.directory / f"operacao-{len(self.logs)}.csv", say=self.lines.append)
        self.logs.append(self.log)
        self.operator = Operator(
            self.coordinator, self.broker, self.log, now=self.clock.now, monotonic=self.clock.monotonic,
            horizon=HORIZON, gale_window=gale_window,
        )
        self.operator.recover()
        return self.operator

    def close(self):
        for closing in (self.coordinator, self.broker):
            if closing is not None:
                closing.close()
        self.coordinator = self.broker = None
        for log in self.logs:
            log.close()

    def restart(self, **options):
        self.close()
        self.clock.advance(2)
        return self.start(**options)

    def decision(self, action=CALL, payout=92.0, age=0.5, validated=True, asset=ASSET):
        """An ARTIFICIAL decision whose quote was captured age seconds ago."""
        self.clock.advance(30)
        self.price = round(self.price + 0.001, 5)
        at = self.clock.now() - timedelta(seconds=age)
        sample = analysis.Sample(at, self.price, f"{self.price:.5f}", payout)
        validation = analysis.Validation(validated, "regra de teste (artificial)", analysis.Rule(analysis.TREND, 1))
        if action == analysis.WAIT:
            return analysis.Decision(asset, action, "dados insuficientes (artificial)", validation, sample)
        until = at + timedelta(seconds=analysis.DECISION_WINDOW_SECONDS)
        return analysis.Decision(asset, action, validation.reason, validation, sample, until)

    def consider(self, decision):
        return self.operator.consider(decision, reading_ms=95.0, decision_ms=4.0)

    def settle(self, result, payout=92.0, late=0.2):
        """Move to the expiry of the pending order and read the quote that gives result."""
        order = self.broker.order(self.coordinator.status().broker_order_id)
        self.clock.t = order.expires_at + timedelta(seconds=late)
        if result == DRAW:
            self.price = order.entry_price
        else:
            up = (result == WIN) == (order.direction == CALL)
            self.price = round(order.entry_price + (0.0002 if up else -0.0002), 5)
        self.operator.observe(ASSET, self.clock.now(), self.price, f"{self.price:.5f}", payout, 60.0)

    def read(self, payout=92.0, wait=0.5, captured_ago=0.0):
        """A reading of the cycle's asset, wait seconds after the last one."""
        self.clock.advance(wait)
        at = self.clock.now() - timedelta(seconds=captured_ago)
        self.operator.observe(ASSET, at, self.price, f"{self.price:.5f}", payout, 60.0)

    def orders(self, cycle_id=None):
        """(stake, state) of the simulated orders, in order; of one cycle when given."""
        orders = [order for order in self.broker.orders() if order.order_id is not None]
        if cycle_id is not None:
            ids = {row[0] for row in self.query("SELECT reservation_id FROM reservations WHERE cycle_id = ?", (cycle_id,))}
            orders = [order for order in orders if order.reservation_id in ids]
        return [(order.stake_cents, order.state) for order in orders]

    def query(self, sql, parameters=()):
        connection = connect_read_only(self.directory / "operacao-simulada.db")
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def rows(self, event=None):
        rows = [row for log in self.logs for row in log.rows]
        return [row for row in rows if event is None or row["event"] == event]

    def states(self):
        return [line.split(" — ")[0][len("ESTADO: "):] for line in self.lines if line.startswith("ESTADO: ")]

    def run_cycle(self, results, action=CALL, payout=92.0, gale_payout=92.0):
        """Start a cycle and settle its attempts with results, reading a new quote before each gale."""
        started = self.consider(self.decision(action, payout))
        self.assertTrue(started.started, started)
        for index, result in enumerate(results):
            self.settle(result, gale_payout)
            if index + 1 < len(results):
                self.read(gale_payout)
        return started.cycle_id


class FirstEntryTest(OperationTestCase):
    def test_aguardar_starts_nothing_and_the_collection_goes_on(self):
        self.start()
        result = self.consider(self.decision(analysis.WAIT, validated=False))
        self.assertFalse(result.started)
        self.assertIsNone(self.coordinator.status().cycle_id)
        self.assertEqual(self.operator.state(), COLLECTING)
        self.assertEqual(self.rows(), [])  # nothing to record: the scan's CSV has the decision
        result = self.consider(self.decision(analysis.WAIT))  # validated, but AGUARDAR (old quote, low payout)
        self.assertFalse(result.started)
        self.assertEqual(self.operator.state(), WAITING_DECISION)
        no_data = analysis.Decision(ASSET, analysis.WAIT, "sem dados para análise nesta visita: falta o payout")
        self.assertFalse(self.consider(no_data).started)
        self.assertEqual(self.operator.state(), WAITING_DECISION)  # a visit without data changes nothing
        self.assertEqual(self.broker.orders(), [])

    def test_a_valid_decision_starts_a_cycle_with_the_first_stake_and_is_consumed(self):
        self.start()
        decision = self.decision(PUT, payout=90.0)  # 90% is enough
        result = self.consider(decision)
        self.assertTrue(result.started)
        status = self.coordinator.status()
        self.assertEqual((status.cycle_id, status.asset, status.direction, status.attempt), (1, ASSET, PUT, 1))
        self.assertEqual(status.decision_id, operation.decision_identifier(decision, HORIZON))
        self.assertEqual(self.query("SELECT decision_id, cycle_id, payout FROM decisions"), [(status.decision_id, 1, 90.0)])
        [order] = self.broker.orders()
        self.assertEqual((order.stake_cents, order.direction, order.entry_quote, order.state), (500, PUT, decision.sample.quote, "OPEN"))
        self.assertEqual(self.operator.state(), PENDING_ENTRY)
        self.assertTrue(self.operator.busy())

    def test_an_expired_decision_is_refused_without_a_late_order_and_is_not_consumed(self):
        self.start()
        expired = self.decision(CALL, age=5.001)
        result = self.consider(expired)
        self.assertFalse(result.started)
        [row] = self.rows(operation.DEADLINE_REFUSED)
        self.assertIn("nenhuma ordem atrasada é enviada", row["reason"])
        self.assertEqual(self.operator.counts["deadline"], 1)
        self.assertEqual((self.broker.orders(), self.query("SELECT * FROM decisions")), ([], []))
        self.assertIsNone(self.coordinator.decision_cycle(operation.decision_identifier(expired, HORIZON)))
        self.assertTrue(self.consider(self.decision(CALL, age=4.9)).started)  # within the window

    def test_a_decision_without_validation_or_with_a_low_payout_is_refused(self):
        self.start()
        cases = (
            ("não vem de uma regra validada", {"validated": False}),
            ("payout 89,9%: a primeira entrada exige 90% ou mais", {"payout": 89.9}),
            ("payout indisponível", {"payout": None}),
        )
        for reason, options in cases:
            with self.subTest(reason=reason):
                result = self.consider(self.decision(CALL, **options))
                self.assertFalse(result.started)
                self.assertIn(reason, result.reason)
        self.assertEqual((self.broker.orders(), self.coordinator.status().cycle_id), ([], None))
        self.assertEqual(self.operator.counts["refused"], 3)

    def test_the_first_window_closing_between_the_reservation_and_the_submission(self):
        self.start(store=SlowReservation)
        self.store.delay = 6.0  # the reservation is saved after the decision's validity
        decision = self.decision(CALL)
        self.consider(decision)
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.pending), (ABANDONED, None))
        self.assertEqual(self.broker.orders(), [])  # nothing reached the simulator
        self.assertEqual(self.query("SELECT submission_state FROM reservations"), [(NOT_SUBMITTED,)])
        [row] = self.rows(operation.DEADLINE_REFUSED)
        self.assertIn("entre a reserva e o envio", row["reason"])
        self.assertEqual(self.coordinator.decision_cycle(status.decision_id), 1)  # consumed with its cycle
        self.store.delay = 0.0
        self.assertFalse(self.consider(decision).started)


class ProgressionTest(OperationTestCase):
    def test_four_losses_use_r5_r10_r20_r40_and_never_a_fifth_entry(self):
        self.start()
        self.run_cycle([LOSS, LOSS, LOSS, LOSS])
        self.assertEqual(self.orders(), [(stake, LOSS) for stake in STAKES])
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt, status.is_active), (LOSS, 4, False))
        for _ in range(20):  # quotes keep coming: nothing more is sent
            self.read()
        self.clock.advance(60)
        self.operator.tick()
        self.assertEqual(len(self.orders()), 4)
        self.assertEqual(self.query("SELECT attempt, stake_cents, result FROM reservations"), [
            (1, 500, LOSS), (2, 1000, LOSS), (3, 2000, LOSS), (4, 4000, LOSS),
        ])
        [ended] = self.rows(operation.CYCLE_ENDED)
        self.assertIn("sem quinta entrada", ended["reason"])
        self.assertFalse(self.operator.busy())

    def test_a_win_ends_the_cycle_at_any_attempt(self):
        self.start()
        for attempt in range(1, 5):
            with self.subTest(attempt=attempt):
                results = [LOSS] * (attempt - 1) + [WIN]
                cycle_id = self.run_cycle(results, action=PUT if attempt % 2 else CALL)
                self.assertEqual(self.orders(cycle_id), list(zip(STAKES, results)))
                status = self.coordinator.status()
                self.assertEqual((status.cycle_id, status.outcome, status.attempt), (cycle_id, WIN, attempt))
                self.read()
                self.assertEqual(len(self.orders(cycle_id)), attempt)  # nothing after the WIN

    def test_the_payout_is_checked_only_for_the_first_entry(self):
        self.start()
        cycle_id = self.run_cycle([LOSS, LOSS, WIN], payout=92.0, gale_payout=80.0)
        self.assertEqual(self.orders(cycle_id), [(500, LOSS), (1000, LOSS), (2000, WIN)])
        payouts = [row["payout_percent"] for row in self.rows(operation.ENTRY_SUBMITTED)]
        self.assertEqual(payouts, [92.0, 80.0, 80.0])
        cycle_id = self.run_cycle([LOSS, WIN], gale_payout=None)  # a gale also enters without a payout read
        self.assertEqual(self.orders(cycle_id), [(500, LOSS), (1000, WIN)])

    def test_a_draw_or_a_cancellation_ends_the_cycle_without_a_gale(self):
        self.start()
        cycle_id = self.run_cycle([DRAW])
        self.assertEqual((self.orders(cycle_id), self.coordinator.status().outcome), ([(500, DRAW)], DRAW))
        self.consider(self.decision(CALL))
        order = self.broker.order(self.coordinator.status().broker_order_id)
        self.clock.t = order.settle_by + timedelta(seconds=1)  # no quote of the asset at the expiry
        self.operator.tick()
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt), (CANCELLED, 1))
        self.assertIn("sem cotação observada no vencimento", self.rows(operation.RESULT)[-1]["reason"])
        self.read()
        self.assertEqual(len(self.orders()), 2)


class WindowTest(OperationTestCase):
    def test_each_gale_has_its_own_window_from_the_confirmed_loss(self):
        self.start()
        self.consider(self.decision(CALL))
        self.settle(LOSS)  # long after the first decision's validity
        [opened] = self.rows(operation.WINDOW_OPENED)
        self.assertEqual(opened["window_opens_utc"], self.clock.now())
        self.assertEqual(opened["window_closes_utc"], self.clock.now() + timedelta(seconds=5))
        self.read(wait=0.5, captured_ago=0.6)  # captured before the LOSS was confirmed: not this gale's
        self.assertEqual(len(self.orders()), 1)
        self.read(wait=4.0)  # 4.5 s after the LOSS, still within the window
        self.assertEqual(self.orders(), [(500, LOSS), (1000, "OPEN")])
        self.settle(LOSS)
        second = self.rows(operation.WINDOW_OPENED)[-1]
        self.assertEqual(second["attempt"], 3)
        self.assertEqual(second["window_opens_utc"], self.clock.now())  # a new window, from this LOSS
        gale = self.rows(operation.ENTRY_SUBMITTED)[1]
        self.assertIsNone(gale["decision_ms"])  # a gale needs no new decision
        self.assertEqual(self.coordinator.status().direction, CALL)

    def test_an_expired_gale_window_interrupts_the_cycle_without_a_late_order(self):
        self.start(gale_window=3.0)
        self.consider(self.decision(PUT))
        self.settle(LOSS)
        self.clock.advance(3.001)
        self.operator.tick()
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt, status.pending), (ABANDONED, 2, None))
        [expired] = self.rows(operation.WINDOW_EXPIRED)
        self.assertIn("janela da tentativa 2 vencida", expired["reason"])
        self.assertIn("sem ordem atrasada", expired["reason"])
        self.read()  # the quote that came too late
        self.assertEqual(self.orders(), [(500, LOSS)])
        self.assertEqual((self.operator.counts["windows"], self.operator.busy()), (1, False))
        self.assertTrue(self.consider(self.decision(CALL)).started)  # the search goes on

    def test_a_slow_start_of_the_submission_never_lets_a_gale_in_late(self):
        self.start(store=SlowStart)
        self.consider(self.decision(CALL))
        self.settle(LOSS)
        self.store.delay = 1.0  # the start of the gale's submission is saved 1 s later
        self.read(wait=4.5)  # a new quote 4.5 s after the LOSS: in time, before the submission
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt, status.pending), (REJECTED, 2, None))
        refused = self.rows(operation.DEADLINE_REFUSED)[-1]
        self.assertEqual((refused["attempt"], refused["result"]), (2, REJECTED))
        self.assertIn("prazo da entrada vencido", refused["reason"])
        self.assertEqual(self.orders(), [(500, LOSS)])  # the late gale never became an order
        self.assertEqual(self.operator.counts["deadline"], 1)

    def test_the_first_entry_carries_the_decision_deadline_to_the_simulator(self):
        self.start(store=SlowStart)
        self.store.delay = 4.8  # the decision, 0.5 s old, expires while the start is saved
        self.consider(self.decision(PUT))
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt), (REJECTED, 1))
        self.assertEqual(self.orders(), [])
        [refused] = self.rows(operation.DEADLINE_REFUSED)
        self.assertIn("prazo da entrada vencido", refused["reason"])


class DuplicateTest(OperationTestCase):
    def test_the_same_decision_never_starts_a_second_cycle(self):
        self.start()
        decision = self.decision(CALL)
        self.assertTrue(self.consider(decision).started)
        during = self.consider(decision)  # during its own cycle
        self.assertIn("decisão já usada pelo ciclo 1", during.reason)
        other = self.consider(self.decision(PUT))  # another decision while the cycle runs
        self.assertIn("está pendente", other.reason)
        self.settle(WIN)
        after_win = self.consider(decision)
        self.assertIn("decisão já usada pelo ciclo 1", after_win.reason)
        self.restart()
        reopened = self.consider(decision)
        self.assertIn("decisão já usada pelo ciclo 1", reopened.reason)
        self.assertFalse(during.started or other.started or after_win.started or reopened.started)
        self.assertEqual((len(self.orders()), self.query("SELECT COUNT(*) FROM cycles")), (1, [(1,)]))


class UncertainTest(OperationTestCase):
    def test_a_lost_answer_is_never_resent_and_the_order_is_found_by_its_reservation(self):
        self.start(broker=LostAnswer)
        self.broker.lose = 1
        self.consider(self.decision(CALL))
        [uncertain] = self.rows(operation.ENTRY_UNCERTAIN)
        self.assertEqual(uncertain["result"], UNKNOWN)
        self.assertIn("nada é reenviado", uncertain["reason"])
        [found] = self.rows(operation.ORDER_FOUND)
        status = self.coordinator.status()
        self.assertEqual((status.broker_order_id, status.is_blocked), (found["simulated_order_id"], True))
        self.assertEqual(self.operator.state(), PENDING_ENTRY)
        self.settle(LOSS)
        self.read()
        self.assertEqual(self.orders(), [(500, LOSS), (1000, "OPEN")])  # one order per attempt
        self.assertEqual(self.query("SELECT COUNT(*) FROM reservations"), [(2,)])

    def test_unknown_during_a_gale_is_resolved_and_the_progression_goes_on(self):
        self.start(broker=LostAnswer)
        self.consider(self.decision(PUT))
        self.settle(LOSS)
        self.broker.lose = 1
        self.read()
        self.assertEqual(len(self.rows(operation.ORDER_FOUND)), 1)
        self.settle(LOSS)
        self.read()
        self.settle(WIN)
        self.assertEqual(self.orders(), [(500, LOSS), (1000, LOSS), (2000, WIN)])

    def test_an_order_that_the_first_lookup_missed_is_linked_not_rejected(self):
        self.start(broker=OrderAppearsLate)
        self.broker.missed = 1
        self.leave_unlinked()
        status = self.coordinator.status()
        self.assertIsNotNone(status.broker_order_id)
        self.assertEqual(self.rows(operation.ORDER_ABSENT), [])
        self.assertEqual(len(self.rows(operation.ORDER_FOUND)), 1)

    def leave_unlinked(self):
        """A submission whose answer was lost after the simulator saved the order."""
        self.broker.observe(ASSET, self.clock.now(), 1.1, "1.10000")
        self.coordinator.start_cycle(ASSET, CALL)
        reservation = self.coordinator.reserve_next_entry(operation.FICTITIOUS_BALANCE_CENTS)
        self.coordinator.begin_submission(reservation.reservation_id)
        self.broker.submit_order(OrderRequest(reservation.reservation_id, ASSET, CALL, 500))
        self.coordinator.record_result(reservation.reservation_id, UNKNOWN)
        self.operator.tick()

    def test_a_submission_that_never_reached_the_simulator_ends_as_rejected(self):
        self.start(broker=NeverReached)
        self.broker.fail = 1
        self.consider(self.decision(CALL))
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.pending), (REJECTED, None))
        [absent] = self.rows(operation.ORDER_ABSENT)
        self.assertIn("nenhuma ordem foi aceita para a reserva", absent["reason"])
        self.assertEqual(self.orders(), [])  # never sent again
        [row] = self.broker.orders()
        self.assertEqual((row.order_id, row.state), (None, REJECTED))
        self.assertTrue(self.consider(self.decision(CALL)).started)


class RestartTest(OperationTestCase):
    def test_a_pending_order_is_followed_after_reopening_without_resending(self):
        self.start()
        self.consider(self.decision(CALL))
        self.settle(LOSS)
        self.read()  # attempt 2 is sent, then the program stops
        self.restart()
        self.assertEqual(self.operator.state(), PENDING_ENTRY)
        self.assertTrue(self.operator.recovering)
        refused = self.consider(self.decision(CALL))
        self.assertIn("recuperação", refused.reason)
        self.settle(LOSS)  # the restored attempt 2 settles; a restored cycle releases no gale
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt), (ABANDONED, 3))
        self.assertIn("não sobrevive ao reinício", self.rows(operation.RECOVERY)[-1]["reason"])
        self.assertEqual(self.orders(), [(500, LOSS), (1000, LOSS)])
        self.assertFalse(self.operator.recovering)
        self.assertTrue(self.consider(self.decision(PUT)).started)

    def test_a_pending_order_whose_time_passed_is_cancelled_at_reopening(self):
        self.start()
        self.consider(self.decision(CALL))
        self.close()
        self.clock.advance(HORIZON + 60)  # nobody read the asset at the expiry
        self.start()
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.pending), (CANCELLED, None))
        self.assertEqual(self.operator.state(), WAITING_DECISION if self.operator.validated else COLLECTING)
        self.assertFalse(self.operator.recovering)

    def test_a_restored_open_cycle_without_a_pending_entry_is_abandoned(self):
        self.start()
        self.consider(self.decision(PUT))
        self.settle(LOSS)  # the gale's window is open when the program stops
        self.restart()
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.attempt, status.is_paused), (ABANDONED, 2, False))
        [recovered] = self.rows(operation.RECOVERY)
        self.assertIn("ciclo restaurado aberto: a janela da próxima entrada não sobrevive ao reinício", recovered["reason"])
        self.read()
        self.assertEqual(self.orders(), [(500, LOSS)])

    def leave_unknown(self, with_order):
        """A run that stopped right after recording UNKNOWN, before asking the simulator."""
        coordinator = Coordinator(SqliteStore(self.directory / "operacao-simulada.db"))
        coordinator.resume()
        coordinator.start_cycle(ASSET, PUT)
        reservation = coordinator.reserve_next_entry(operation.FICTITIOUS_BALANCE_CENTS)
        coordinator.begin_submission(reservation.reservation_id)
        if with_order:
            broker = PaperBroker(self.directory / "operacao-simulada-ordens.db", HORIZON, self.clock.now)
            broker.observe(ASSET, self.clock.now(), 1.1, "1.10000")
            broker.submit_order(OrderRequest(reservation.reservation_id, ASSET, PUT, 500))
            broker.close()
        coordinator.record_result(reservation.reservation_id, UNKNOWN)
        coordinator.close()
        self.clock.advance(1)

    def test_an_unknown_left_by_a_stop_is_followed_through_its_order_at_reopening(self):
        self.leave_unknown(with_order=True)
        self.start()
        status = self.coordinator.status()
        self.assertEqual((status.is_blocked, self.operator.state()), (True, PENDING_ENTRY))
        [found] = self.rows(operation.ORDER_FOUND)
        self.assertEqual(status.broker_order_id, found["simulated_order_id"])
        self.settle(LOSS)  # a restored cycle releases no gale
        status = self.coordinator.status()
        self.assertEqual((status.outcome, self.orders()), (ABANDONED, [(500, LOSS)]))
        self.assertFalse(self.operator.recovering)

    def test_an_unknown_without_an_order_ends_as_rejected_at_reopening(self):
        self.leave_unknown(with_order=False)
        self.start()
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.pending, self.orders()), (REJECTED, None, []))
        self.assertIn("nenhuma ordem foi aceita", self.rows(operation.ORDER_ABSENT)[0]["reason"])
        self.assertFalse(self.operator.recovering)
        self.assertTrue(self.consider(self.decision(CALL)).started)

    def test_a_reservation_never_submitted_is_closed_at_reopening(self):
        coordinator = Coordinator(SqliteStore(self.directory / "operacao-simulada.db"))
        coordinator.resume()
        coordinator.start_cycle(ASSET, CALL)
        coordinator.reserve_next_entry(operation.FICTITIOUS_BALANCE_CENTS)  # then the program stops
        coordinator.close()
        self.start()
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.pending), (ABANDONED, None))
        self.assertEqual(self.query("SELECT submission_state FROM reservations"), [(NOT_SUBMITTED,)])
        self.assertIn("nunca foi enviada (RESERVED)", self.rows(operation.RECOVERY)[0]["reason"])
        self.assertEqual(self.broker.orders(), [])


class FailureTest(OperationTestCase):
    def test_a_storage_failure_stops_the_operation_in_pause_and_keeps_the_state(self):
        self.start(store=FailingStart)
        with self.assertRaises(OperationStopped):
            self.consider(self.decision(CALL))
        self.assertEqual(self.operator.state(), PAUSED)
        self.assertEqual(self.states()[-1], PAUSED)
        status = self.coordinator.status()
        self.assertTrue(status.is_paused)
        self.assertEqual((status.submission_state, self.broker.orders()), (RESERVED, []))
        self.assertIn("simulated write failure", self.rows(operation.STOPPED)[0]["reason"])
        with self.assertRaises(OperationStopped):
            self.consider(self.decision(CALL))
        self.restart()  # reopening checks the saved state: the reservation was never sent
        self.assertEqual(self.query("SELECT submission_state FROM reservations"), [(NOT_SUBMITTED,)])
        self.assertTrue(self.consider(self.decision(CALL)).started)
        self.assertEqual(
            self.query("SELECT submission_state FROM reservations ORDER BY cycle_id"), [(NOT_SUBMITTED,), (ACCEPTED,)]
        )


class LogTest(OperationTestCase):
    def test_the_states_are_explicit(self):
        self.start()
        self.consider(self.decision(analysis.WAIT, validated=False))
        self.consider(self.decision(analysis.WAIT))  # a rule is validated, but the answer is AGUARDAR
        self.run_cycle([LOSS, WIN])
        self.assertEqual(
            self.states(),
            [COLLECTING, WAITING_DECISION, PENDING_ENTRY, ACTIVE_CYCLE, PENDING_ENTRY, WAITING_DECISION],
        )
        self.assertEqual(
            [row["state"] for row in self.rows()],
            [ACTIVE_CYCLE, PENDING_ENTRY, ACTIVE_CYCLE, ACTIVE_CYCLE, PENDING_ENTRY, WAITING_DECISION, WAITING_DECISION],
        )
        self.restart()
        self.consider(self.decision(CALL))
        self.close()
        self.start()
        self.assertEqual(self.states()[-1], PENDING_ENTRY)  # restored: the pending entry is shown

    def test_the_csv_keeps_reading_decision_and_simulated_submission_times_apart(self):
        self.start()
        self.consider(self.decision(CALL, age=6.0))  # refused by its deadline
        self.run_cycle([LOSS, WIN])
        self.close()
        with open(self.logs[0].path, newline="", encoding="utf-8") as file:
            header, *lines = list(csv.reader(file))
        self.assertEqual(tuple(header), OPERATION_COLUMNS)
        rows = [dict(zip(header, line)) for line in lines]
        self.assertEqual({row["execution"] for row in rows}, {"simulada"})
        refused, started, first, result, opened, gale = rows[:6]
        self.assertEqual(refused["event"], "entrada recusada: prazo vencido")
        self.assertEqual(refused["quote_age_ms"], "6000.0")
        self.assertEqual(started["event"], "ciclo iniciado")
        self.assertEqual((first["event"], first["attempt"], first["stake_cents"]), ("entrada simulada enviada", "1", "500"))
        self.assertEqual((first["reading_ms"], first["decision_ms"], first["simulated_submit_ms"]), ("95.0", "4.0", "0.0"))
        self.assertTrue(first["simulated_order_id"].startswith("SIM-"))
        self.assertEqual((result["event"], result["result"]), ("resultado simulado", LOSS))
        self.assertEqual(opened["event"], "janela do gale aberta")
        self.assertEqual((gale["attempt"], gale["stake_cents"], gale["reading_ms"], gale["decision_ms"]), ("2", "1000", "60.0", ""))
        self.assertTrue(all(row["state"] in operation.STATES for row in rows))
        ended = rows[-1]
        self.assertEqual((ended["event"], ended["outcome"]), ("ciclo encerrado", WIN))
        submitted = [line for line in self.lines if "entrada simulada enviada" in line]
        self.assertTrue(all(operation.SIMULATOR_TIME_NOTE in line for line in submitted))

    def test_simulated_results_never_feed_the_analysis(self):
        runtime = self.directory
        self.start()
        self.run_cycle([LOSS, LOSS, WIN])
        self.close()
        history = analysis.load_history(runtime)
        self.assertEqual((history.files, history.usable, history.series), (0, 0, {}))
        self.assertEqual(sorted(runtime.glob("scan-*")), [])
        source = Path(operation.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".add(", source)  # the operator never adds a sample to an analyzer


if __name__ == "__main__":
    unittest.main()
