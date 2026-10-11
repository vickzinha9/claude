import unittest

from pocketautonomo.cycle import CALL, LOSS, PUT, UNKNOWN, WIN, Cycle, Entry

ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents


class NewCycleTest(unittest.TestCase):
    def test_starts_at_attempt_one_active_keeping_asset_and_direction(self):
        for direction in (CALL, PUT):
            with self.subTest(direction=direction):
                cycle = Cycle(ASSET, direction)
                self.assertEqual(cycle.attempt, 1)
                self.assertTrue(cycle.is_active)
                self.assertFalse(cycle.is_blocked)
                self.assertIsNone(cycle.outcome)
                self.assertEqual((cycle.asset, cycle.direction), (ASSET, direction))
                self.assertEqual(cycle.next_entry(BALANCE), Entry(ASSET, direction, 1, 500))

    def test_entries_keep_asset_and_direction_after_losses(self):
        cycle = Cycle(ASSET, PUT)
        for attempt, stake in enumerate((500, 1000, 2000, 4000), start=1):
            self.assertEqual(cycle.next_entry(BALANCE), Entry(ASSET, PUT, attempt, stake))
            cycle.record_result(attempt, LOSS)

    def test_rejects_invalid_asset_or_direction(self):
        invalid = (("", CALL), ("  ", PUT), (None, CALL), (ASSET, "call"), (ASSET, "UP"))
        for asset, direction in invalid:
            with self.subTest(asset=asset, direction=direction):
                with self.assertRaises(ValueError):
                    Cycle(asset, direction)


class QueryTest(unittest.TestCase):
    def test_querying_next_value_does_not_advance_attempt(self):
        cycle = Cycle(ASSET, CALL)
        for _ in range(3):
            self.assertEqual(cycle.next_stake(), 500)
            self.assertEqual(cycle.next_entry(BALANCE), Entry(ASSET, CALL, 1, 500))
        self.assertEqual(cycle.attempt, 1)


class WinTest(unittest.TestCase):
    def test_win_ends_cycle_immediately(self):
        cycle = Cycle(ASSET, CALL)
        self.assertTrue(cycle.record_result(1, WIN))
        self.assertEqual((cycle.is_active, cycle.outcome, cycle.attempt), (False, WIN, 1))
        self.assertIsNone(cycle.next_stake())
        self.assertIsNone(cycle.next_entry(BALANCE))

    def test_win_after_losses_ends_cycle_without_further_attempts(self):
        cycle = Cycle(ASSET, PUT)
        cycle.record_result(1, LOSS)
        cycle.record_result(2, LOSS)
        cycle.record_result(3, WIN)
        self.assertEqual((cycle.is_active, cycle.outcome, cycle.attempt), (False, WIN, 3))
        self.assertIsNone(cycle.next_entry(BALANCE))
        with self.assertRaises(ValueError):
            cycle.record_result(4, LOSS)


class LossTest(unittest.TestCase):
    def test_each_loss_advances_exactly_one_attempt(self):
        cycle = Cycle(ASSET, CALL)
        for attempt, next_stake in ((1, 1000), (2, 2000), (3, 4000)):
            self.assertTrue(cycle.record_result(attempt, LOSS))
            self.assertEqual(cycle.attempt, attempt + 1)
            self.assertEqual(cycle.next_stake(), next_stake)
            self.assertTrue(cycle.is_active)

    def test_fourth_loss_ends_cycle_without_fifth_entry(self):
        cycle = Cycle(ASSET, CALL)
        for attempt in range(1, 5):
            cycle.record_result(attempt, LOSS)
        self.assertEqual((cycle.is_active, cycle.outcome, cycle.attempt), (False, LOSS, 4))
        self.assertIsNone(cycle.next_stake())
        self.assertIsNone(cycle.next_entry(BALANCE))
        with self.assertRaises(ValueError):
            cycle.record_result(5, LOSS)


class UnknownResultTest(unittest.TestCase):
    def setUp(self):
        self.cycle = Cycle(ASSET, CALL)
        self.cycle.record_result(1, LOSS)
        self.assertTrue(self.cycle.record_result(2, UNKNOWN))

    def assert_blocked_at_attempt_two(self):
        self.assertTrue(self.cycle.is_blocked)
        self.assertTrue(self.cycle.is_active)
        self.assertEqual(self.cycle.attempt, 2)
        self.assertIsNone(self.cycle.next_stake())
        self.assertIsNone(self.cycle.next_entry(BALANCE))

    def test_unknown_blocks_new_entries(self):
        self.assert_blocked_at_attempt_two()

    def test_repeated_unknown_keeps_block(self):
        self.assertFalse(self.cycle.record_result(2, UNKNOWN))
        self.assert_blocked_at_attempt_two()

    def test_result_for_another_attempt_does_not_release_block(self):
        self.assertFalse(self.cycle.record_result(1, LOSS))
        with self.assertRaises(ValueError):
            self.cycle.record_result(3, LOSS)
        self.assert_blocked_at_attempt_two()

    def test_definitive_loss_for_same_attempt_releases_block(self):
        self.assertTrue(self.cycle.record_result(2, LOSS))
        self.assertFalse(self.cycle.is_blocked)
        self.assertEqual(self.cycle.next_entry(BALANCE), Entry(ASSET, CALL, 3, 2000))

    def test_definitive_win_for_same_attempt_ends_cycle(self):
        self.assertTrue(self.cycle.record_result(2, WIN))
        self.assertFalse(self.cycle.is_blocked)
        self.assertEqual((self.cycle.is_active, self.cycle.outcome), (False, WIN))


class DuplicateResultTest(unittest.TestCase):
    def test_duplicate_loss_does_not_advance_again(self):
        cycle = Cycle(ASSET, CALL)
        self.assertTrue(cycle.record_result(1, LOSS))
        self.assertFalse(cycle.record_result(1, LOSS))
        self.assertEqual(cycle.attempt, 2)
        self.assertEqual(cycle.next_stake(), 1000)

    def test_duplicate_after_cycle_end_changes_nothing(self):
        won = Cycle(ASSET, CALL)
        won.record_result(1, WIN)
        lost = Cycle(ASSET, CALL)
        for attempt in range(1, 5):
            lost.record_result(attempt, LOSS)
        for cycle, attempt, result in ((won, 1, WIN), (lost, 4, LOSS)):
            with self.subTest(result=result):
                self.assertFalse(cycle.record_result(attempt, result))
                self.assertEqual(
                    (cycle.is_active, cycle.outcome, cycle.attempt), (False, result, attempt)
                )

    def test_late_unknown_for_resolved_attempt_is_ignored(self):
        cycle = Cycle(ASSET, CALL)
        cycle.record_result(1, LOSS)
        self.assertFalse(cycle.record_result(1, UNKNOWN))
        self.assertFalse(cycle.is_blocked)
        self.assertEqual(cycle.next_stake(), 1000)

    def test_conflicting_result_for_resolved_attempt_is_rejected(self):
        cycle = Cycle(ASSET, CALL)
        cycle.record_result(1, LOSS)
        with self.assertRaises(ValueError):
            cycle.record_result(1, WIN)
        self.assertEqual((cycle.is_active, cycle.attempt), (True, 2))


class BalanceTest(unittest.TestCase):
    def test_insufficient_balance_prevents_next_entry(self):
        cycle = Cycle(ASSET, CALL)
        self.assertIsNone(cycle.next_entry(0))
        self.assertIsNone(cycle.next_entry(499))
        self.assertEqual(cycle.next_entry(500), Entry(ASSET, CALL, 1, 500))
        cycle.record_result(1, LOSS)
        self.assertIsNone(cycle.next_entry(999))
        self.assertEqual(cycle.next_entry(1000), Entry(ASSET, CALL, 2, 1000))
        self.assertEqual((cycle.is_active, cycle.attempt), (True, 2))

    def test_rejects_non_integer_balance(self):
        cycle = Cycle(ASSET, CALL)
        for balance in (500.0, "500", True, None):
            with self.subTest(balance=balance):
                with self.assertRaises(ValueError):
                    cycle.next_entry(balance)


class IndependentCycleTest(unittest.TestCase):
    def test_new_cycle_starts_again_at_five_reais(self):
        first = Cycle(ASSET, CALL)
        for attempt in (1, 2, 3):
            first.record_result(attempt, LOSS)
        second = Cycle(ASSET, CALL)
        self.assertEqual(second.next_entry(BALANCE), Entry(ASSET, CALL, 1, 500))
        self.assertEqual(first.next_entry(BALANCE), Entry(ASSET, CALL, 4, 4000))

    def test_new_cycle_after_finished_cycle_starts_at_five_reais(self):
        finished = Cycle(ASSET, PUT)
        for attempt in range(1, 5):
            finished.record_result(attempt, LOSS)
        self.assertEqual(Cycle(ASSET, PUT).next_stake(), 500)


class InvalidInputTest(unittest.TestCase):
    def test_rejects_invalid_attempt_or_result(self):
        cycle = Cycle(ASSET, CALL)
        invalid = ((0, LOSS), (5, LOSS), (True, LOSS), ("1", LOSS), (1, "DRAW"), (1, "win"))
        for attempt, result in invalid:
            with self.subTest(attempt=attempt, result=result):
                with self.assertRaises(ValueError):
                    cycle.record_result(attempt, result)
        self.assertEqual((cycle.attempt, cycle.is_blocked, cycle.is_active), (1, False, True))


if __name__ == "__main__":
    unittest.main()
