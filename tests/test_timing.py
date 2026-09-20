import tempfile
import unittest
from PIL import Image
from harmony_runtime.runtime import Runtime
from harmony_runtime.timing import Timings
from test_runtime import FakeDevice


class TimingTests(unittest.TestCase):
    def test_accumulates_and_records_failed_calls(self):
        ticks = iter([0, .0012, 1, 1.0023])
        timing = Timings(lambda: next(ticks))
        self.assertEqual(timing.call("read", lambda: 42), 42)
        with self.assertRaises(ValueError):
            with timing.phase("read"):
                raise ValueError("private content")
        self.assertEqual(timing.milliseconds(), {"read_ms": 3.5})

    def test_empty_and_nonnegative(self):
        ticks = iter([2, 1])
        timing = Timings(lambda: next(ticks))
        self.assertEqual(timing.milliseconds(), {})
        timing.call("read", lambda: None)
        self.assertEqual(timing.milliseconds(), {"read_ms": 0})


class RuntimeTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = FakeDevice("fake")
        self.device.screenshot = lambda: Image.new("RGB", (100, 100))
        self.runtime = Runtime(self.tmp.name, factory=lambda serial: self.device,
                               discover=lambda: ["fake"])
        self.sid = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_fast_has_phase_and_request_timings(self):
        obs = self.runtime.observe("owner", self.sid)
        required = {"queue_ms", "worker_setup_ms", "request_ms", "tree_ms", "display_ms",
                    "snapshot_ms", "screen_ready_before_ms", "screen_ready_after_ms"}
        self.assertTrue(required <= obs["timing"].keys())
        self.assertNotIn("screenshot_ms", obs["timing"])
        self.assertTrue(all(type(v) in (int, float) and v >= 0 for v in obs["timing"].values()))
        self.assertTrue(obs["actionable"])
        self.assertIs(self.runtime.sessions[self.sid].observations[obs["observation_id"]][1], obs)
        self.assertEqual(self.device.writes, 0)

    def test_full_preserves_bracketing_and_annotation(self):
        calls = []
        original = self.device.tree
        def tree():
            calls.append("tree")
            return original()
        self.device.tree = tree
        obs = self.runtime.observe("owner", self.sid, mode="FULL")
        self.assertEqual(calls, ["tree", "tree"])
        self.assertTrue(obs["image_tree_consistent"])
        self.assertTrue(obs["som"]["available"])
        self.assertTrue({"screenshot_ms", "tree_after_ms", "display_after_ms",
                         "encode_image_ms", "annotation_ms", "encode_annotation_ms"} <= obs["timing"].keys())

    def test_failed_dispatch_timed_and_never_replayed(self):
        self.check_action(fail=True)

    def test_successful_dispatch_timed_and_dedup_preserves_original(self):
        self.check_action(fail=False)

    def check_action(self, fail):
        obs = self.runtime.observe("owner", self.sid)
        self.device.fail = fail
        request = dict(session_id=self.sid, request_id="timed-request", observation_id=obs["observation_id"],
                       action={"kind": "tap", "target": {"action_id": "n0"}}, expected={"text": "After"})
        result = self.runtime.act("owner", request)
        self.assertEqual(result["execution_status"], "unknown" if fail else "executed")
        self.assertTrue({"queue_ms", "worker_setup_ms", "preflight_observe_ms", "screen_guard_ms",
                         "journal_begin_ms", "dispatch_ms", "total_ms"} <= result["timing"].keys())
        self.assertEqual("verification_ms" in result["timing"], not fail)
        again = self.runtime.act("owner", request)
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["timing"], result["timing"])
        self.assertEqual(self.device.writes, 1)
        self.assertNotIn("observation", again)
