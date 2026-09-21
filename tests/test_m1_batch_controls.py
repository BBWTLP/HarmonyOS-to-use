"""M1 batch must survive (and report) a lost device.

Device evidence (2026-09-21): the 30-run M1 batch aborted with a traceback when
the phone screen locked mid-run, discarding the five runs it had already
collected. A device-level failure is now a recorded blocked run, and the batch
stops cleanly after a bounded run of them.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from accept_m1_weibo import (BATCH_FATAL_CODES,  # noqa: E402
                             MAX_CONSECUTIVE_DEVICE_FAILURES, DeviceFailureTracker)


class DeviceFailureTrackerTests(unittest.TestCase):
    def test_a_successful_run_resets_the_streak(self):
        tracker = DeviceFailureTracker(limit=3)
        self.assertIsNone(tracker.record("screen_locked"))
        self.assertIsNone(tracker.record("screen_locked"))
        self.assertIsNone(tracker.record(None))          # a run worked
        self.assertIsNone(tracker.record("screen_locked"))
        self.assertEqual(tracker.consecutive, 1)

    def test_the_batch_stops_after_the_bounded_streak(self):
        tracker = DeviceFailureTracker(limit=3)
        self.assertIsNone(tracker.record("runtime_transport_error"))
        self.assertIsNone(tracker.record("runtime_transport_error"))
        reason = tracker.record("runtime_transport_error")
        self.assertIsNotNone(reason)
        self.assertIn("restored before resuming", reason)
        self.assertEqual(tracker.consecutive, 3)

    def test_a_task_level_block_does_not_stop_the_batch(self):
        """`stale_observation` is a grounding refusal, not a lost device."""
        tracker = DeviceFailureTracker(limit=2)
        for _ in range(5):
            self.assertIsNone(tracker.record("stale_observation"))
        self.assertEqual(tracker.consecutive, 0)
        self.assertNotIn("stale_observation", BATCH_FATAL_CODES)
        self.assertNotIn("editor_unavailable", BATCH_FATAL_CODES)

    def test_the_default_limit_is_explicit(self):
        self.assertEqual(DeviceFailureTracker().limit,
                         MAX_CONSECUTIVE_DEVICE_FAILURES)
        self.assertGreaterEqual(MAX_CONSECUTIVE_DEVICE_FAILURES, 2)
        for code in ("screen_locked", "runtime_unavailable", "device_quarantined"):
            self.assertIn(code, BATCH_FATAL_CODES)


if __name__ == "__main__":
    unittest.main()
