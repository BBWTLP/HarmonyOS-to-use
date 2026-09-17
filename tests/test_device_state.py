import unittest
from harmony_runtime.device import parse_screen_state

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
