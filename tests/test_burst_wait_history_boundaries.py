"""v3.2 Phase 9: burst / wait / history boundary cases that were not yet pinned.

These do not re-implement any feature; they fix the offline semantics of the
shared 3000ms budget, ambiguous targets and "a timeout is not a success".
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_runtime import FakeDevice
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime


class Pages(FakeDevice):
    """Each verified tap moves the page forward one step."""

    def __init__(self, serial):
        super().__init__(serial)
        self.text = "Page0"
        self.targets = []
        self.delay_seconds = 0.0

    def tree(self):
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return super().tree()

    def dispatch(self, action, target):
        self.targets.append(target["text"])
        self.writes += 1
        self.text = f"Page{self.writes}"


class Ambiguous(FakeDevice):
    def tree(self):
        return {"attributes": {"text": "ambiguous", "bounds": "[0,0][100,100]",
                               "clickable": "true", "type": "Button"},
                "children": [
                    {"attributes": {"text": "重复", "bounds": "[0,0][50,50]",
                                    "clickable": "true", "type": "Button"},
                     "children": []},
                    {"attributes": {"text": "重复", "bounds": "[50,50][100,100]",
                                    "clickable": "true", "type": "Button"},
                     "children": []}]}


class BurstBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = Pages("fake")
        self.runtime = Runtime(self.tmp.name, factory=lambda serial: self.device,
                               discover=lambda: ["fake"])
        self.session = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def request(self, count=3, timeout_ms=3000):
        obs = self.runtime.observe("owner", self.session)
        return {"session_id": self.session, "request_id": "budget",
                "observation_id": obs["observation_id"], "timeout_ms": timeout_ms,
                "steps": [{"action": {"kind": "tap",
                                      "target": {"text": f"Page{index}"}},
                           "expected": {"text": f"Page{index + 1}"}}
                          for index in range(count)]}

    def test_the_shared_budget_stops_the_sequence_before_the_next_write(self):
        request = self.request(count=3, timeout_ms=300)
        original = self.device.dispatch

        def slow_dispatch(action, target):
            # The first step is durable; the device then becomes slow enough that
            # the shared budget cannot cover the second step.
            original(action, target)
            self.device.delay_seconds = 0.2

        self.device.dispatch = slow_dispatch
        result = self.runtime.burst("owner", request)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.device.writes, 1)
        self.assertEqual(self.device.targets, ["Page0"])
        self.assertEqual(result["verified_steps"], 1)
        self.assertEqual(result["steps"][-1]["execution_status"], "not_dispatched")

    def test_budget_exhaustion_does_not_replay_the_sequence(self):
        request = self.request(count=3, timeout_ms=300)
        original = self.device.dispatch

        def slow_dispatch(action, target):
            original(action, target)
            self.device.delay_seconds = 0.2

        self.device.dispatch = slow_dispatch
        self.runtime.burst("owner", request)
        writes = self.device.writes
        replay = self.runtime.burst("owner", request)
        self.assertTrue(replay.get("deduplicated"))
        self.assertEqual(self.device.writes, writes)

    def test_a_sequence_within_budget_still_completes(self):
        result = self.runtime.burst("owner", self.request(count=3))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.device.writes, 3)


class BurstAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = Ambiguous("fake")
        self.runtime = Runtime(self.tmp.name, factory=lambda serial: self.device,
                               discover=lambda: ["fake"])
        self.session = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_an_ambiguous_step_target_stops_before_dispatch(self):
        observed = self.runtime.observe("owner", self.session)
        request = {"session_id": self.session, "request_id": "ambiguous",
                   "observation_id": observed["observation_id"],
                   "steps": [{"action": {"kind": "tap", "target": {"text": "重复"}},
                              "expected": {"text": "done"}}]}
        result = self.runtime.burst("owner", request)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.device.text, "Settings")

    def test_an_ambiguous_target_refuses_a_single_action_too(self):
        observed = self.runtime.observe("owner", self.session)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act("owner", {
                "session_id": self.session, "request_id": "ambiguous-act",
                "observation_id": observed["observation_id"],
                "action": {"kind": "tap", "target": {"text": "重复"}}})
        self.assertEqual(error.exception.code, "target_ambiguous")
        self.assertEqual(self.device.writes, 0)


class WaitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = Pages("fake")
        self.runtime = Runtime(self.tmp.name, factory=lambda serial: self.device,
                               discover=lambda: ["fake"])
        self.session = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_a_timeout_is_not_reported_as_success(self):
        result = self.runtime.wait("owner", session_id=self.session,
                                   expected={"text": "永远不会出现"},
                                   timeout_ms=150, poll_ms=100)
        self.assertNotEqual(result["status"], "matched")

    def test_a_typed_condition_timeout_is_not_success(self):
        result = self.runtime.wait("owner", session_id=self.session,
                                   condition={"type": "text_present",
                                              "value": "永远不会出现"},
                                   timeout_ms=150, poll_ms=100)
        self.assertIn(result["status"], ("timeout", "absent", "not_found"))

    def test_an_unsupported_mode_is_rejected_without_touching_the_device(self):
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.observe("owner", self.session, mode="LIGHT")
        self.assertEqual(error.exception.code, "unsupported_capability")


class HistoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = Pages("fake")
        self.runtime = Runtime(self.tmp.name, factory=lambda serial: self.device,
                               discover=lambda: ["fake"])
        self.session = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def act(self, request_id):
        observed = self.runtime.observe("owner", self.session)
        return self.runtime.act("owner", {
            "session_id": self.session, "request_id": request_id,
            "observation_id": observed["observation_id"],
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})

    def test_pagination_is_newest_first_and_does_not_repeat(self):
        for index in range(5):
            self.act(f"history-{index}")
        first = self.runtime.history("owner", self.session, limit=2)
        second = self.runtime.history("owner", self.session, limit=2,
                                      before=first["next_before"])
        first_ids = [item["request_id"] for item in first["items"]]
        second_ids = [item["request_id"] for item in second["items"]]
        self.assertEqual(first_ids, ["history-4", "history-3"])
        self.assertEqual(second_ids, ["history-2", "history-1"])
        self.assertFalse(set(first_ids) & set(second_ids))

    def test_history_omits_page_content_and_input_values(self):
        self.act("history-privacy")
        item = self.runtime.history("owner", self.session, limit=1)["items"][0]
        self.assertNotIn("observation", item)
        self.assertNotIn("text", item)
        self.assertNotIn("expected", item)
        self.assertIn("execution_status", item)
        self.assertIn("verification_status", item)


if __name__ == "__main__":
    unittest.main()
