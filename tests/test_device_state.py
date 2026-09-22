import unittest
from harmony_runtime.device import (
    parse_screen_state,
    screen_ready_confirmed,
    screen_sleep_confirmed,
)

class ScreenStateTests(unittest.TestCase):
    def test_explicit_service_evidence(self):
        self.assertEqual(parse_screen_state("Current State: SLEEP  Reason: 1", "* screenLocked  \t\ttrue\t\t100"),
                         {"screen_on": False, "screen_locked": True})
        self.assertEqual(parse_screen_state("Current State: AWAKE", "screenLocked false 100"),
                         {"screen_on": True, "screen_locked": False})

    def test_missing_conflicting_or_unrecognized_evidence_is_unknown(self):
        for power, lock in [("permission denied", "error"),
                            ("Current State: AWAKE\nCurrent State: SLEEP", "screenLocked false\nscreenLocked true"),
                            ("Current State: DOZE", "not_screenLocked false")]:
            self.assertEqual(parse_screen_state(power, lock),
                             {"screen_on": None, "screen_locked": None})

    def test_sleep_confirmed_requires_explicit_off_and_ignores_lock_flag(self):
        # Credential-free device may report either lock flag after sleep.
        self.assertTrue(screen_sleep_confirmed({"screen_on": False, "screen_locked": True}))
        self.assertTrue(screen_sleep_confirmed({"screen_on": False, "screen_locked": False}))
        self.assertFalse(screen_sleep_confirmed({"screen_on": True, "screen_locked": False}))
        self.assertFalse(screen_sleep_confirmed({"screen_on": True, "screen_locked": True}))
        self.assertFalse(screen_sleep_confirmed({"screen_on": None, "screen_locked": True}))
        self.assertFalse(screen_sleep_confirmed({"screen_on": None, "screen_locked": False}))
        self.assertFalse(screen_sleep_confirmed({}))
        self.assertFalse(screen_sleep_confirmed(None))
        # Explicit off is enough; lock is recorded, not required.
        self.assertTrue(screen_sleep_confirmed({"screen_on": False, "screen_locked": None}))

    def test_ready_requires_awake_and_unlocked(self):
        self.assertTrue(screen_ready_confirmed({"screen_on": True, "screen_locked": False}))
        self.assertFalse(screen_ready_confirmed({"screen_on": True, "screen_locked": True}))
        self.assertFalse(screen_ready_confirmed({"screen_on": False, "screen_locked": False}))
        self.assertFalse(screen_ready_confirmed({"screen_on": True, "screen_locked": None}))
