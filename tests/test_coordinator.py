import threading
import time
import unittest
from unittest import mock

from pocketautonomo.coordinator import (
    Coordinator,
    CoordinatorError,
    Reservation,
    UnknownReservationError,
)
from pocketautonomo.cycle import (
    ABANDONED,
    CALL,
    CANCELLED,
    DRAW,
    LOSS,
    PUT,
    REJECTED,
    UNKNOWN,
    WIN,
    Cycle,
    Entry,
)

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents
THREADS = 16


def started(direction=CALL):
    coordinator = Coordinator()
    coordinator.start_cycle(ASSET, direction)
    return coordinator


def play(coordinator, result):
    """Reserve the next entry and record its result; return the reservation."""
    reservation = coordinator.reserve_next_entry(BALANCE)
    coordinator.record_result(reservation.reservation_id, result)
    return reservation


class StartCycleTest(unittest.TestCase):
    def test_first_cycle_starts_at_attempt_one_with_five_reais(self):
        coordinator = Coordinator()
        self.assertTrue(coordinator.status().can_start_cycle)
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        cycle_id = coordinator.start_cycle(ASSET, PUT)
        status = coordinator.status()
        self.assertEqual((status.cycle_id, status.asset, status.direction), (cycle_id, ASSET, PUT))
        self.assertEqual((status.attempt, status.is_active, status.pending), (1, True, None))
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry, Entry(ASSET, PUT, 1, 500))

    def test_only_one_active_cycle_per_coordinator(self):
        coordinator = started(CALL)
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle("GBPUSD_otc", PUT)
        play(coordinator, LOSS)  # still active, nothing pending
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle(ASSET, CALL)
        status = coordinator.status()
        self.assertEqual((status.cycle_id, status.attempt, status.direction), (1, 2, CALL))

    def test_cannot_start_while_a_reservation_is_pending(self):
        coordinator = started()
        reservation = coordinator.reserve_next_entry(BALANCE)
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle(ASSET, CALL)
        coordinator.record_result(reservation.reservation_id, UNKNOWN)
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle(ASSET, CALL)
        self.assertEqual(coordinator.status().pending, reservation)

    def test_new_cycle_after_previous_ended_starts_again_at_five_reais(self):
        endings = {"WIN": (LOSS, LOSS, WIN), "fourth LOSS": (LOSS, LOSS, LOSS, LOSS)}
        for ending, results in endings.items():
            with self.subTest(ending=ending):
                coordinator = started(CALL)
                for result in results:
                    play(coordinator, result)
                self.assertTrue(coordinator.status().can_start_cycle)
                self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
                reservation = coordinator.reserve_next_entry(BALANCE)
                self.assertEqual(reservation.entry, Entry(ASSET, PUT, 1, 500))
                self.assertEqual(reservation.cycle_id, 2)

    def test_invalid_asset_or_direction_does_not_start_a_cycle(self):
        coordinator = Coordinator()
        for asset, direction in (("", CALL), (ASSET, "UP")):
            with self.subTest(asset=asset, direction=direction):
                with self.assertRaises(ValueError):
                    coordinator.start_cycle(asset, direction)
        self.assertIsNone(coordinator.status().cycle_id)
        self.assertEqual(coordinator.start_cycle(ASSET, CALL), 1)


class ReservationTest(unittest.TestCase):
    def test_reservation_id_is_unique_and_tied_to_cycle_and_attempt(self):
        coordinator = Coordinator()
        ids = []
        for expected_cycle_id, results in ((1, (WIN,)), (2, (LOSS, LOSS, LOSS, WIN)), (3, (LOSS, WIN))):
            cycle_id = coordinator.start_cycle(ASSET, CALL)
            self.assertEqual(cycle_id, expected_cycle_id)
            for attempt, result in enumerate(results, start=1):
                reservation = play(coordinator, result)
                self.assertEqual((reservation.cycle_id, reservation.attempt), (cycle_id, attempt))
                ids.append(reservation.reservation_id)
        ids.append(started().reserve_next_entry(BALANCE).reservation_id)
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(isinstance(rid, str) and rid for rid in ids))

    def test_repeated_reservations_while_pending_return_nothing(self):
        coordinator = started()
        reservation = coordinator.reserve_next_entry(BALANCE)
        self.assertIsInstance(reservation, Reservation)
        for _ in range(3):
            self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        status = coordinator.status()
        self.assertEqual((status.pending, status.attempt), (reservation, 1))
        coordinator.record_result(reservation.reservation_id, LOSS)
        following = coordinator.reserve_next_entry(BALANCE)
        self.assertEqual(following.entry, Entry(ASSET, CALL, 2, 1000))
        self.assertNotEqual(following.reservation_id, reservation.reservation_id)

    def test_insufficient_balance_prevents_reservation(self):
        coordinator = started()
        self.assertIsNone(coordinator.reserve_next_entry(0))
        self.assertIsNone(coordinator.reserve_next_entry(499))
        self.assertIsNone(coordinator.status().pending)
        first = coordinator.reserve_next_entry(500)
        self.assertEqual(first.entry, Entry(ASSET, CALL, 1, 500))
        coordinator.record_result(first.reservation_id, LOSS)
        self.assertIsNone(coordinator.reserve_next_entry(999))
        self.assertIsNone(coordinator.status().pending)
        self.assertEqual(coordinator.reserve_next_entry(1000).entry, Entry(ASSET, CALL, 2, 1000))

    def test_rejects_non_integer_balance_in_any_state(self):
        with_pending = started()
        reservation = with_pending.reserve_next_entry(BALANCE)
        states = {"no cycle": Coordinator(), "cycle started": started(), "pending": with_pending}
        for state, coordinator in states.items():
            for balance in (500.0, "500", True, None):
                with self.subTest(state=state, balance=balance):
                    with self.assertRaises(ValueError):
                        coordinator.reserve_next_entry(balance)
        self.assertIsNone(states["cycle started"].status().pending)
        self.assertEqual(with_pending.status().pending, reservation)


class CycleRulesTest(unittest.TestCase):
    def test_win_ends_cycle_and_releases_reservation(self):
        coordinator = started()
        play(coordinator, LOSS)
        play(coordinator, WIN)
        status = coordinator.status()
        self.assertEqual((status.is_active, status.outcome, status.attempt), (False, WIN, 2))
        self.assertIsNone(status.pending)
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))

    def test_each_loss_advances_one_attempt_and_fourth_ends_without_fifth_entry(self):
        coordinator = started()
        stakes = [play(coordinator, LOSS).entry.stake_cents for _ in range(4)]
        self.assertEqual(stakes, [500, 1000, 2000, 4000])
        status = coordinator.status()
        self.assertEqual((status.is_active, status.outcome, status.attempt), (False, LOSS, 4))
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))


class EndingResultTest(unittest.TestCase):
    def test_draw_cancelled_and_rejected_end_cycle_and_release_reservation(self):
        for result in (DRAW, CANCELLED, REJECTED):
            with self.subTest(result=result):
                coordinator = started()
                play(coordinator, LOSS)
                reservation = coordinator.reserve_next_entry(BALANCE)
                self.assertTrue(coordinator.record_result(reservation.reservation_id, result))
                status = coordinator.status()
                self.assertEqual(
                    (status.is_active, status.outcome, status.attempt), (False, result, 2)
                )
                self.assertIsNone(status.pending)
                self.assertTrue(status.can_start_cycle)
                self.assertIsNone(coordinator.reserve_next_entry(BALANCE))

    def test_duplicates_and_wrong_ids_do_not_change_ended_cycle(self):
        for result in (DRAW, CANCELLED, REJECTED):
            with self.subTest(result=result):
                coordinator = started()
                rid = play(coordinator, result).reservation_id
                ended = coordinator.status()
                self.assertFalse(coordinator.record_result(rid, result))
                self.assertFalse(coordinator.record_result(rid, UNKNOWN))
                with self.assertRaises(ValueError):
                    coordinator.record_result(rid, WIN)
                with self.assertRaises(UnknownReservationError):
                    coordinator.record_result(rid + "0", result)
                self.assertEqual(coordinator.status(), ended)

    def test_timeout_stays_unknown_until_rejection_is_confirmed(self):
        coordinator = started()
        reservation = coordinator.reserve_next_entry(BALANCE)
        with self.assertRaises(ValueError):
            coordinator.record_result(reservation.reservation_id, "TIMEOUT")
        coordinator.record_result(reservation.reservation_id, UNKNOWN)  # timeout or network error
        status = coordinator.status()
        self.assertEqual((status.pending, status.is_blocked), (reservation, True))
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        with self.assertRaises(CoordinatorError):
            coordinator.start_cycle(ASSET, CALL)
        self.assertTrue(coordinator.record_result(reservation.reservation_id, REJECTED))
        status = coordinator.status()
        self.assertEqual((status.outcome, status.pending, status.is_blocked), (REJECTED, None, False))
        self.assertEqual(coordinator.start_cycle(ASSET, CALL), 2)
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry, Entry(ASSET, CALL, 1, 500))


class AbandonTest(unittest.TestCase):
    def test_abandon_ends_cycle_without_a_bet_result(self):
        coordinator = started()
        lost = play(coordinator, LOSS)
        coordinator.abandon_cycle()
        status = coordinator.status()
        self.assertEqual(
            (status.outcome, status.is_active, status.attempt, status.pending),
            (ABANDONED, False, 2, None),
        )
        self.assertIsNone(coordinator.reserve_next_entry(BALANCE))
        self.assertFalse(coordinator.record_result(lost.reservation_id, LOSS))
        with self.assertRaises(ValueError):
            coordinator.record_result(lost.reservation_id, ABANDONED)  # not a bet result
        self.assertEqual(coordinator.start_cycle(ASSET, PUT), 2)
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry, Entry(ASSET, PUT, 1, 500))

    def test_abandon_is_refused_without_open_cycle_or_with_pending_reservation(self):
        with self.assertRaises(CoordinatorError):
            Coordinator().abandon_cycle()
        ended = started()
        play(ended, WIN)
        pending = started()
        pending.reserve_next_entry(BALANCE)
        for coordinator in (ended, pending):
            before = coordinator.status()
            with self.assertRaises(CoordinatorError):
                coordinator.abandon_cycle()
            self.assertEqual(coordinator.status(), before)


class UnknownResultTest(unittest.TestCase):
    def setUp(self):
        self.coordinator = started()
        self.first = play(self.coordinator, LOSS)
        self.second = self.coordinator.reserve_next_entry(BALANCE)
        self.assertTrue(self.coordinator.record_result(self.second.reservation_id, UNKNOWN))

    def assert_still_blocked(self):
        status = self.coordinator.status()
        self.assertEqual((status.pending, status.attempt), (self.second, 2))
        self.assertTrue(status.is_blocked)
        self.assertTrue(status.is_active)
        self.assertIsNone(self.coordinator.reserve_next_entry(BALANCE))
        with self.assertRaises(CoordinatorError):
            self.coordinator.start_cycle(ASSET, CALL)

    def test_unknown_keeps_reservation_pending_and_blocks_entries(self):
        self.assert_still_blocked()

    def test_repeated_unknown_changes_nothing(self):
        self.assertFalse(self.coordinator.record_result(self.second.reservation_id, UNKNOWN))
        self.assert_still_blocked()

    def test_results_for_other_reservations_do_not_release_block(self):
        self.assertFalse(self.coordinator.record_result(self.first.reservation_id, LOSS))
        with self.assertRaises(ValueError):
            self.coordinator.record_result(self.first.reservation_id, WIN)
        with self.assertRaises(UnknownReservationError):
            self.coordinator.record_result("unknown-id", WIN)
        self.assert_still_blocked()

    def test_definitive_loss_for_same_reservation_releases_and_advances(self):
        self.assertTrue(self.coordinator.record_result(self.second.reservation_id, LOSS))
        status = self.coordinator.status()
        self.assertEqual((status.pending, status.is_blocked, status.attempt), (None, False, 3))
        next_entry = self.coordinator.reserve_next_entry(BALANCE).entry
        self.assertEqual(next_entry, Entry(ASSET, CALL, 3, 2000))

    def test_definitive_win_for_same_reservation_ends_cycle(self):
        self.assertTrue(self.coordinator.record_result(self.second.reservation_id, WIN))
        status = self.coordinator.status()
        self.assertEqual((status.outcome, status.pending, status.is_blocked), (WIN, None, False))
        self.assertTrue(status.can_start_cycle)


class DuplicateResultTest(unittest.TestCase):
    def test_duplicate_loss_does_not_advance_again(self):
        coordinator = started()
        first = coordinator.reserve_next_entry(BALANCE)
        self.assertTrue(coordinator.record_result(first.reservation_id, LOSS))
        self.assertFalse(coordinator.record_result(first.reservation_id, LOSS))
        second = coordinator.reserve_next_entry(BALANCE)
        self.assertFalse(coordinator.record_result(first.reservation_id, LOSS))
        status = coordinator.status()
        self.assertEqual((status.attempt, status.pending), (2, second))
        self.assertEqual(second.entry.stake_cents, 1000)

    def test_duplicate_after_cycle_ended_changes_nothing(self):
        coordinator = started()
        last = play(coordinator, WIN)
        self.assertFalse(coordinator.record_result(last.reservation_id, WIN))
        self.assertFalse(coordinator.record_result(last.reservation_id, UNKNOWN))
        status = coordinator.status()
        self.assertEqual((status.outcome, status.attempt, status.is_blocked), (WIN, 1, False))

    def test_conflicting_result_is_rejected_without_changing_cycle(self):
        coordinator = started()
        first = play(coordinator, LOSS)
        with self.assertRaises(ValueError):
            coordinator.record_result(first.reservation_id, WIN)
        status = coordinator.status()
        self.assertEqual((status.is_active, status.outcome, status.attempt), (True, None, 2))

    def test_late_results_from_previous_cycle_do_not_touch_new_cycle(self):
        coordinator = started()
        old_loss = play(coordinator, LOSS)
        old_win = play(coordinator, WIN)
        coordinator.start_cycle(ASSET, PUT)
        current = coordinator.reserve_next_entry(BALANCE)
        self.assertFalse(coordinator.record_result(old_loss.reservation_id, LOSS))
        self.assertFalse(coordinator.record_result(old_win.reservation_id, WIN))
        self.assertFalse(coordinator.record_result(old_loss.reservation_id, UNKNOWN))
        with self.assertRaises(ValueError):
            coordinator.record_result(old_win.reservation_id, LOSS)
        status = coordinator.status()
        self.assertEqual((status.cycle_id, status.attempt, status.pending), (2, 1, current))
        self.assertFalse(status.is_blocked)
        self.assertTrue(coordinator.record_result(current.reservation_id, LOSS))
        self.assertEqual(coordinator.status().attempt, 2)


class WrongIdentifierTest(unittest.TestCase):
    def setUp(self):
        self.coordinator = started()
        self.reservation = self.coordinator.reserve_next_entry(BALANCE)

    def assert_untouched(self):
        status = self.coordinator.status()
        self.assertEqual((status.pending, status.attempt), (self.reservation, 1))
        self.assertFalse(status.is_blocked)

    def test_unknown_id_is_rejected_without_changing_state(self):
        rid = self.reservation.reservation_id
        for wrong in ("", "unknown-id", rid + "0", rid[:-1]):
            for result in (WIN, LOSS, UNKNOWN):
                with self.subTest(wrong=wrong, result=result):
                    with self.assertRaises(UnknownReservationError):
                        self.coordinator.record_result(wrong, result)
        self.assert_untouched()
        self.assertTrue(self.coordinator.record_result(rid, WIN))

    def test_id_from_another_coordinator_is_rejected(self):
        other = started()
        foreign = other.reserve_next_entry(BALANCE)
        with self.assertRaises(UnknownReservationError):
            self.coordinator.record_result(foreign.reservation_id, WIN)
        self.assert_untouched()
        self.assertEqual(other.status().pending, foreign)

    def test_non_string_id_or_invalid_result_is_rejected(self):
        rid = self.reservation.reservation_id
        invalid = (
            (None, LOSS), (1, LOSS), (self.reservation, LOSS),
            (rid, "TIMEOUT"), (rid, "NETWORK_ERROR"), (rid, "win"),
        )
        for wrong_id, result in invalid:
            with self.subTest(wrong_id=wrong_id, result=result):
                with self.assertRaises(ValueError):
                    self.coordinator.record_result(wrong_id, result)
        self.assert_untouched()

    def test_result_before_any_reservation_is_rejected(self):
        coordinator = started()
        with self.assertRaises(UnknownReservationError):
            coordinator.record_result("unknown-id", LOSS)
        self.assertEqual(coordinator.status().attempt, 1)


class ConcurrencyTest(unittest.TestCase):
    def run_simultaneously(self, function):
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
            self.assertFalse(thread.is_alive(), "a thread did not finish")
        self.assertEqual(len(results), THREADS)
        return results

    def test_simultaneous_reservations_hand_out_a_single_entry(self):
        coordinator = started()
        original = Cycle.next_entry

        def slow_next_entry(cycle, balance_cents):
            time.sleep(0.01)  # widen the window between the check and the reservation
            return original(cycle, balance_cents)

        with mock.patch.object(Cycle, "next_entry", slow_next_entry):
            results = self.run_simultaneously(lambda: coordinator.reserve_next_entry(BALANCE))

        reserved = [r for r in results if isinstance(r, Reservation)]
        self.assertEqual(len(reserved), 1)
        self.assertEqual(results.count(None), THREADS - 1)
        self.assertEqual(coordinator.status().pending, reserved[0])

    def test_simultaneous_starts_open_a_single_cycle(self):
        coordinator = Coordinator()
        original = Cycle.__init__

        def slow_init(cycle, asset, direction):
            time.sleep(0.01)  # widen the window between the check and the new cycle
            original(cycle, asset, direction)

        with mock.patch.object(Cycle, "__init__", slow_init):
            results = self.run_simultaneously(lambda: coordinator.start_cycle(ASSET, CALL))

        self.assertEqual([r for r in results if not isinstance(r, CoordinatorError)], [1])
        self.assertEqual(coordinator.status().cycle_id, 1)

    def test_simultaneous_duplicate_results_advance_once(self):
        coordinator = started()
        reservation = coordinator.reserve_next_entry(BALANCE)
        results = self.run_simultaneously(
            lambda: coordinator.record_result(reservation.reservation_id, LOSS)
        )
        self.assertEqual(sorted(results), [False] * (THREADS - 1) + [True])
        self.assertEqual(coordinator.status().attempt, 2)
        self.assertEqual(coordinator.reserve_next_entry(BALANCE).entry.stake_cents, 1000)

    def test_concurrent_workers_play_each_attempt_once(self):
        coordinator = started()
        taken = []  # only the holder of the single pending reservation appends
        deadline = time.monotonic() + 5

        def work():
            while time.monotonic() < deadline:
                reservation = coordinator.reserve_next_entry(BALANCE)
                if reservation is not None:
                    taken.append(reservation)
                    coordinator.record_result(reservation.reservation_id, LOSS)
                elif not coordinator.status().is_active:
                    return "done"
            return "timeout"

        self.assertEqual(self.run_simultaneously(work), ["done"] * THREADS)
        self.assertEqual([r.entry.stake_cents for r in taken], [500, 1000, 2000, 4000])
        self.assertEqual(len({r.reservation_id for r in taken}), 4)
        self.assertEqual(coordinator.status().outcome, LOSS)


if __name__ == "__main__":
    unittest.main()
