"""v3.2 Phase 10: the autonomous runner's offline safety boundaries.

The runner may plan, re-plan and remember, but it may not declare success. Only
the programme checker can close a task, and a planner's own report is never
evidence. Memory compression must never drop a user constraint or an unresolved
device write.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.checker import (CheckerDenied, INCONCLUSIVE, ReadOnlyChecker,
                                   check)
from harmony_agent.contracts import Predicate
from harmony_agent.host import AgentHost
from harmony_agent.memory import Memory
from harmony_runtime.observation import snapshot
from harmony_runtime.runtime import Runtime


def observation(tree=None, *, bundle=WEIBO, texts=()):
    tree = tree or {"attributes": {"bundleName": bundle, "type": "Root",
                                   "bounds": "[0,0][1080,2340]"},
                    "children": [{"attributes": {"bundleName": bundle, "type": "Text",
                                                 "text": text,
                                                 "bounds": f"[40,{400 + index * 80}][600,{460 + index * 80}]"},
                                  "children": []}
                                 for index, text in enumerate(texts)]}
    obs = snapshot(tree, (1080, 2340, 0), {"status": "ok", "bundle": bundle})
    obs["observation_id"] = "obs_runner"
    obs["controller_epoch"] = 0
    obs["actionable"] = True
    return obs


class MemoryTests(unittest.TestCase):
    def build(self, window_tokens=3000):
        return Memory(goal="完成微博搜索",
                      constraints=["allowed_apps=com.sina.weibo.stage",
                                   "allowed_actions=tap,replace_text,back"],
                      window_tokens=window_tokens)

    def test_facts_and_hypotheses_stay_separate(self):
        memory = self.build()
        memory.record_fact("search_open", True, "obs:1")
        memory.record_hypothesis("page_is_results", True, "obs:1")
        context = memory.context_facts()
        self.assertIn("search_open=True", context)
        self.assertIn("（假设）page_is_results=True", context)
        kinds = {item["kind"] for item in
                 memory.retrieve("search_open") + memory.retrieve("page_is_results")}
        self.assertEqual(kinds, {"fact", "hypothesis"})

    def test_a_hypothesis_is_never_an_executable_observation(self):
        memory = self.build()
        memory.record_hypothesis("target_visible", True, "obs:1")
        hits = memory.retrieve("target_visible")
        self.assertTrue(hits)
        self.assertNotIn("observation_id", hits[0])

    def test_compression_drops_raw_states_but_keeps_authority(self):
        memory = self.build(window_tokens=1)
        for index in range(12):
            memory.record_state(observation(texts=[f"页面文字{index}"]))
        memory.record_fact("search_open", True, "obs:1")
        memory.record_incident("incident-1", "execution_unknown", "act_1")
        before = len(memory.states)
        record = memory.compress(reason="window")
        self.assertEqual(record["states_before"], before)
        self.assertLess(record["states_after"], before)
        self.assertEqual(record["kept_constraints"], memory.constraints)
        self.assertEqual(record["kept_facts"], 1)
        self.assertEqual([item["code"] for item in record["unresolved_incidents"]],
                         ["execution_unknown"])
        self.assertIn("search_open", record["evidence_index"])
        self.assertIn("not an executable observation", record["note"])

    def test_compression_preserves_the_ability_to_answer_constraints(self):
        memory = self.build(window_tokens=1)
        for index in range(10):
            memory.record_state(observation(texts=[f"页面文字{index}"]))
        memory.compress()
        answers = memory.can_answer(["allowed_apps", "不允许的权限"])
        self.assertTrue(answers["allowed_apps"])
        self.assertFalse(answers["不允许的权限"])

    def test_an_unresolved_incident_survives_compression_and_stays_queryable(self):
        memory = self.build(window_tokens=1)
        memory.record_incident("incident-1", "execution_unknown", "act_1")
        memory.compress()
        self.assertEqual(len(memory.unresolved()), 1)
        self.assertTrue(memory.can_answer(["execution_unknown"])["execution_unknown"])


class CheckerAuthorityTests(unittest.TestCase):
    def test_an_inconclusive_condition_is_not_success(self):
        report = check([Predicate(id="c1", type="input_equals",
                                  target_key="id:missing", value_ref="q")],
                       observation(), arguments={"q": "鸿蒙"})
        self.assertEqual(report.verdict, INCONCLUSIVE)
        self.assertEqual(report.unobserved, ["c1"])
        self.assertTrue(report.limitations)

    def test_a_failed_condition_wins_over_a_passing_one(self):
        report = check([Predicate(id="ok", type="element_present", target_key="存在"),
                        Predicate(id="bad", type="text_equals", value="不存在")],
                       observation(texts=["存在"]))
        self.assertEqual(report.verdict, "fail")
        self.assertEqual([item.id for item in report.conditions if item.verdict == "fail"],
                         ["bad"])

    def test_a_model_assertion_without_an_evaluator_is_inconclusive(self):
        report = check([Predicate(id="semantic", type="page_assertion",
                                  description="结果页已经打开")], observation())
        self.assertEqual(report.verdict, INCONCLUSIVE)
        self.assertEqual(report.conditions[0].decided_by, "unavailable")

    def test_an_unresolved_write_blocks_a_successful_verdict(self):
        report = check([Predicate(id="ok", type="element_present", target_key="存在")],
                       observation(texts=["存在"]), incident_free=False)
        self.assertEqual(report.verdict, INCONCLUSIVE)
        self.assertTrue(any("unresolved device write" in item
                            for item in report.limitations))

    def test_the_checker_refuses_a_write_capability(self):
        with self.assertRaises(CheckerDenied):
            ReadOnlyChecker().dispatch({"kind": "tap"})

    def test_a_verdict_always_carries_evidence_references(self):
        report = check([Predicate(id="ok", type="element_present", target_key="存在")],
                       observation(texts=["存在"]))
        self.assertTrue(report.evidence_refs)
        self.assertIn("obs:obs_runner", report.evidence_refs)


class RunnerDecisionBoundaryTests(unittest.TestCase):
    """End-to-end task behaviour on a fake device: no self-declared success."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: ["fake-device"])
        self.owner = "tester"
        opened = self.runtime.session(self.owner, "open", device_id="fake-device")
        self.session_id = opened["session_id"]
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_off", repo_root=self.root)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def payload(self, request_id, criteria, *, steps="tap:搜索", budget=None):
        return {
            "schema_version": "2.0", "request_id": request_id, "mode": "delegated",
            "goal": "打开微博搜索页",
            "scope": {"device_ref": "current-authorized-device",
                      "allowed_apps": [WEIBO],
                      "allowed_actions": ["tap", "replace_text", "back"],
                      "cloud_data_policy": "disabled"},
            "success_criteria": criteria,
            "arguments": {"steps": steps},
            "budget": budget or {"max_dispatches": 8, "max_seconds": 60,
                                 "max_model_calls": 8},
            "model_profile": "local_off",
        }

    def run_task(self, request_id, criteria, timeout=30.0, **kwargs):
        created = self.host.run_task(self.owner, session_id=self.session_id,
                                     task=self.payload(request_id, criteria, **kwargs))
        task_id = created["task_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in ("RECONCILIATION_REQUIRED",
                                                          "PAUSED", "WAITING_USER"):
                return task_id, status
            time.sleep(0.02)
        self.fail("task did not finish")

    def test_an_executed_step_is_not_a_successful_task(self):
        """The planner's step succeeded, but the success criterion did not."""
        task_id, status = self.run_task(
            "runner-self-report",
            [{"id": "impossible", "type": "text_equals", "value": "这个文本不会出现"}])
        self.assertEqual(status["status"], "FAILED", status)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertEqual(result["conditions"][0]["verdict"], "fail")
        self.assertEqual(result["conditions"][0]["decided_by"], "code")
        self.assertEqual(status["status"], "FAILED", status)
        # The device was still driven: the task failed on verification, not on
        # the action being skipped.
        self.assertEqual(self.devices[-1].stage, "editor")

    def test_an_unobservable_criterion_makes_the_task_partial(self):
        task_id, status = self.run_task(
            "runner-inconclusive",
            [{"id": "unobservable", "type": "input_equals",
              "target_key": "id:missing_field", "value_ref": "steps"}])
        self.assertEqual(status["status"], "PARTIAL", status)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertEqual(result["conditions"][0]["verdict"], "inconclusive")

    def test_an_unknown_write_requires_reconciliation_before_any_success(self):
        class FailingDevice(FakeWeiboDevice):
            def dispatch(self, action, target):
                raise OSError("link lost")

        # Replace the device that will be created for the next session.
        runtime = Runtime(self.root / "runtime-unknown", factory=FailingDevice,
                          discover=lambda: ["fake-device"])
        try:
            session = runtime.session(self.owner, "open", device_id="fake-device")
            host = AgentHost(runtime, self.root / "agent-unknown",
                             profile="local_off", repo_root=self.root)
            try:
                created = host.run_task(self.owner, session_id=session["session_id"],
                                        task=self.payload("runner-unknown", [
                                            {"id": "editor", "type": "element_present",
                                             "target_key": "搜索"}]))
                deadline = time.time() + 30
                while time.time() < deadline:
                    status = host.task_status(self.owner, task_id=created["task_id"])
                    if status["terminal"] or status["status"] in (
                            "RECONCILIATION_REQUIRED", "PAUSED"):
                        break
                    time.sleep(0.02)
                self.assertNotEqual(status["status"], "SUCCEEDED")
                result = host.task_result(self.owner,
                                          task_id=created["task_id"])["result"]
                self.assertTrue(result["resolution_required"])
                self.assertEqual(result["unresolved_actions"][0]["execution_status"],
                                 "unknown")
            finally:
                host.close()
        finally:
            runtime.close()

    def test_task_events_never_claim_success_without_a_checker_verdict(self):
        task_id, status = self.run_task(
            "runner-events",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
        self.assertEqual(status["status"], "SUCCEEDED", status)
        events = self.host.task_events(self.owner, task_id=task_id)["items"]
        self.assertTrue([item for item in events if item["type"] == "decision"])
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        condition = result["conditions"][0]
        self.assertEqual(condition["verdict"], "pass")
        self.assertEqual(condition["decided_by"], "code")


if __name__ == "__main__":
    unittest.main()
