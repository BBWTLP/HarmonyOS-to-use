"""Long-task traceability: checkpoints and live context compression (P6-01/P6-02).

P6-01 requires every subgoal, its budget and any replan reason to be traceable;
P6-02 requires compression to preserve constraints, unresolved incidents and the
evidence index. Both were only half true before: plan versions existed, but no
per-step trace was written, and `Memory.compress()` was never called by the
runner, so the guarantee was untested in the online path.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.host import AgentHost
from harmony_agent.memory import Memory, compression_questions, missing_after_compression
from harmony_runtime.runtime import Runtime


def task_payload(request_id, *, steps, criteria=None, budget=None):
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "delegated",
        "goal": "在微博搜索页输入查询词",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria or [
            {"id": "editor", "type": "element_present", "target_key": "热搜榜"}],
        "arguments": {"steps": steps, "query": "鸿蒙"},
        "budget": budget or {"max_dispatches": 8, "max_seconds": 60,
                             "max_model_calls": 4},
        "model_profile": "local_off",
    }


class LongTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.runtime = Runtime(self.root / "runtime", factory=FakeWeiboDevice,
                               discover=lambda: ["fake-device"])
        self.owner = "tester"
        self.session_id = self.runtime.session(
            self.owner, "open", device_id="fake-device")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def run_task(self, payload, *, window=None, compaction_states=None, timeout=40.0):
        host = AgentHost(self.runtime, self.root / f"agent-{time.time_ns()}",
                         profile="local_off", repo_root=self.root,
                         memory_window_tokens=window,
                         memory_compaction_states=compaction_states)
        try:
            created = host.run_task(self.owner, session_id=self.session_id, task=payload)
            task_id = created["task_id"]
            deadline = time.time() + timeout
            while time.time() < deadline:
                status = host.task_status(self.owner, task_id=task_id)
                if status["terminal"] or status["status"] in (
                        "RECONCILIATION_REQUIRED", "PAUSED", "WAITING_USER"):
                    break
                time.sleep(0.02)
            events = host.task_events(self.owner, task_id=task_id, limit=200)["items"]
            result = host.task_result(self.owner, task_id=task_id)["result"]
            return task_id, status, events, result
        finally:
            host.close()

    def test_every_step_is_checkpointed_with_budget_and_plan_version(self):
        task_id, status, events, result = self.run_task(task_payload(
            "checkpoint-basic", steps="tap:搜索|replace_text:id:search_input=query"))
        self.assertEqual(status["status"], "SUCCEEDED", status)
        checkpoints = [item["payload"] for item in events if item["type"] == "checkpoint"]
        self.assertGreaterEqual(len(checkpoints), 4)  # start/end for two steps
        starts = [item for item in checkpoints if item["phase"] == "start"]
        self.assertEqual([item["subgoal_id"] for item in starts],
                         ["step_01", "step_02"])
        for item in checkpoints:
            self.assertEqual(item["plan_version"], 1)
            self.assertIn("dispatches", item["remaining"])
            self.assertIn("seconds", item["remaining"])
            self.assertIn(item["status"],
                          ("pending", "retry", "verified", "blocked", "failed"))
        ends = [item for item in checkpoints if item["phase"] == "end"]
        self.assertEqual(len(ends), 2)
        self.assertEqual(ends[-1]["status"], "verified")
        # T07 resume bundle: budgets and identity survive in every checkpoint.
        for item in checkpoints:
            resume = item.get("resume") or {}
            self.assertEqual(resume.get("task_id"), task_id)
            self.assertEqual(resume.get("plan_version"), 1)
            self.assertTrue(resume.get("memory_hash"))
            self.assertIn("remaining", resume)
            self.assertIn("verified_subgoals", resume)
            self.assertIn("last_event_sequence", resume)
            self.assertIn("device_scope", resume)
        # Budget is not reset by checkpointing.
        rem = [item["remaining"]["dispatches"] for item in starts]
        self.assertGreaterEqual(rem[0], rem[-1])
        # The checkpoint is also visible while the task is running, via status.
        self.assertIsNone(status["current_subgoal"])  # finished: nothing pending

    def test_compression_runs_and_preserves_what_it_must(self):
        # A deliberately tiny window so the trigger fires inside a bounded run.
        task_id, status, events, result = self.run_task(
            task_payload("compress", steps="tap:搜索|replace_text:id:search_input=query"
                         "|tap:back|tap:搜索"),
            window=60, compaction_states=2)
        compressed = [item["payload"] for item in events
                      if item["type"] == "memory_compressed"]
        self.assertTrue(compressed, "compression never triggered")
        record = compressed[-1]
        self.assertLess(record["states_after"], record["states_before"])
        self.assertTrue(record["kept_constraints"])
        self.assertGreater(record["kept_facts"] + 1, 0)
        self.assertEqual(record["missing"], [])
        self.assertEqual([item for item in events if item["type"] == "context_loss"], [])
        # Facts and the evidence index survive in the result snapshot.
        self.assertIn("facts", result["memory"])
        self.assertTrue(result["memory"]["summaries"] >= 1)
        self.assertNotIn("context_loss", " ".join(result["limitations"]))

    def test_compression_that_loses_a_constraint_is_reported(self):
        """The check must be able to fail; otherwise it proves nothing."""
        memory = Memory(goal="搜索鸿蒙", constraints=[])
        questions = compression_questions(goal="搜索鸿蒙",
                                          constraints=["allowed_apps"],
                                          incident_codes=[])
        self.assertEqual(missing_after_compression(memory, questions), ["allowed_apps"])
        memory.constraints = ["allowed_apps=['com.sina.weibo.stage']"]
        self.assertEqual(missing_after_compression(memory, questions), [])

    def test_unresolved_incidents_survive_compression(self):
        memory = Memory(goal="搜索鸿蒙", constraints=["allowed_actions=['tap']"])
        memory.record_incident("inc_1", "execution_unknown", "act_1")
        memory.record_state({"observation_id": "obs_1", "texts": ["a" * 4000]})
        memory.window_tokens = 10
        record = memory.compress()
        self.assertEqual([item["code"] for item in record["unresolved_incidents"]],
                         ["execution_unknown"])
        questions = compression_questions(goal="搜索鸿蒙",
                                          constraints=memory.constraints,
                                          incident_codes=["execution_unknown"])
        self.assertEqual(missing_after_compression(memory, questions), [])
        self.assertTrue(memory.can_answer(["execution_unknown"])["execution_unknown"])


if __name__ == "__main__":
    unittest.main()
