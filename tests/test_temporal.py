import tempfile
import unittest
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault
from test_full_observation import ImageDevice


class TemporalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = ImageDevice("fake")
        self.runtime = Runtime(self.tmp.name, factory=lambda _: self.device, discover=lambda: ["fake"])
        self.sid = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_samples_preserve_timing_but_never_become_action_handles(self):
        result = self.runtime.observe("owner", self.sid, mode="TEMPORAL")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["frames"]), 5)
        self.assertTrue(result["stable_state"]["last_two_samples_equal"])
        self.assertFalse(result["stable_state"]["continuous_stability_proven"])
        for frame in result["frames"]:
            self.assertGreaterEqual(frame["actual_offset_ms"], frame["planned_offset_ms"] - 1)
            self.assertTrue(frame["historical"])
            self.assertFalse(frame["actionable"])
            self.assertNotIn(frame["observation_id"], self.runtime.sessions[self.sid].observations)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act("owner", dict(session_id=self.sid, request_id="past",
                observation_id=result["frames"][-1]["observation_id"],
                action=dict(kind="tap", target=dict(text="Settings"))))
        self.assertEqual(error.exception.code, "stale_observation")
        self.assertEqual(self.device.writes, 0)

    def test_pixel_changes_do_not_claim_equal_samples_even_with_same_tree(self):
        def screenshot():
            self.device.captures += 1
            self.device.image.putpixel((0, 0), (self.device.captures, 0, 0))
            return self.device.image
        self.device.screenshot = screenshot
        result = self.runtime.observe("owner", self.sid, mode="TEMPORAL")
        self.assertFalse(result["stable_state"]["last_two_samples_equal"])
        self.assertEqual(len({f["fingerprint"] for f in result["frames"]}), 1)
        self.assertEqual(len({f["visual_digest"] for f in result["frames"]}), 5)

    def test_pause_invalidates_sampling_without_exposing_partial_handles(self):
        original = self.device.screenshot
        def screenshot():
            image = original()
            self.runtime.session("owner", "pause", self.sid)
            return image
        self.device.screenshot = screenshot
        with self.assertRaises(RuntimeFault):
            self.runtime.observe("owner", self.sid, mode="TEMPORAL")
        self.assertEqual(self.device.captures, 1)
        self.assertEqual(self.runtime.sessions[self.sid].observations, {})
        self.assertEqual(self.device.writes, 0)


if __name__ == "__main__":
    unittest.main()
