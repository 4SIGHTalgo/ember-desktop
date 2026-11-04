"""Receipt settling after one Place Bet click: bounded, never replayed."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import wager_submission


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def settle(reads, **options):
    """reads(now) -> receipt dict; returns (result, read times)."""
    clock, times = Clock(), []
    driver = Mock()

    def read(script):
        times.append(clock.now)
        return reads(clock.now)
    driver.execute_script.side_effect = read
    with patch.object(wager_submission, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)):
        result = wager_submission.settle_submission(driver, {"status": "submitted"}, "RECEIPT", **options)
    return result, times


PLACING = {"placed": False, "failed": False, "text": "PLACING YOUR BETS Loading", "continuation": None}
QUIET = {"placed": False, "failed": False, "text": "Place Bet $3.33", "continuation": None}


class SettleSubmissionTests(unittest.TestCase):
    def test_dk_still_placing_after_twelve_seconds_is_awaited_to_its_receipt(self):
        # #1017, 2026-11-02: placed by DK, but still "PLACING YOUR BETS" when the 12 s wait ended.
        result, times = settle(lambda now: {"placed": True, "text": "Bet placed"} if now >= 15 else PLACING)
        self.assertTrue(result["placed"])
        self.assertGreaterEqual(times[-1], 15)
        self.assertEqual(result["actions"], [{"status": "submitted"}])  # Waiting never clicks again.

    def test_without_dk_placing_state_the_wait_still_ends_at_its_timeout(self):
        result, times = settle(lambda now: QUIET)
        self.assertFalse(result["placed"])
        self.assertLess(times[-1], 12)

    def test_dk_placing_state_is_bounded_and_then_stays_unconfirmed(self):
        result, times = settle(lambda now: PLACING)
        self.assertFalse(result["placed"])
        self.assertLess(times[-1], wager_submission.PLACING_WAIT)
        self.assertGreaterEqual(times[-1], wager_submission.PLACING_WAIT-.1)
        self.assertEqual(len(result["actions"]), 1)

    def test_a_placing_state_that_ends_without_a_receipt_stops_at_the_normal_bound(self):
        # Once DK leaves its in-flight state the original 12 s bound applies again.
        result, times = settle(lambda now: PLACING if now < 3 else QUIET)
        self.assertFalse(result["placed"])
        self.assertLess(times[-1], 12)

    def test_a_failure_or_location_block_still_ends_the_wait_at_once(self):
        for ending in ({"failed": True, "text": "Bet was rejected"}, {"locationBlocked": True, "text": "PLACING YOUR BETS"}):
            with self.subTest(ending=ending):
                result, times = settle(lambda now: {**PLACING, **ending} if now >= 13 else PLACING)
                self.assertLess(times[-1], 13.1)
                self.assertFalse(result.get("placed"))


if __name__ == "__main__":
    unittest.main()
