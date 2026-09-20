import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeTransportProvider, FakeWeiboDevice
from harmony_agent.artifacts import ArtifactStore, Retention
from harmony_agent.host import AgentHost
from harmony_agent.supervisor import TaskError, TaskStore
from harmony_runtime.runtime import Runtime

WEIBO_DEVICE = "fake-device"


def task_payload(request_id, criteria, *, arguments=None, mode="auto",
                 goal="完成微博搜索", budget=None):
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": mode,
        "goal": goal,
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria,
        "arguments": arguments or {},
        "budget": budget or {"max_dispatches": 8, "max_seconds": 60, "max_model_calls": 8},
        "model_profile": "local_shadow",
    }


class AgentTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        # An explicit class (not a closure) so the runtime can detect the
        # adapter's `supports_foreground` capability before opening a session.
        self.devices = []
        self.dispatch_failure = None
        outer = self

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

            def dispatch(self, action, target):
                if outer.dispatch_failure is not None:
                    failure = outer.dispatch_failure
                    outer.dispatch_failure = None
                    raise failure
                return super().dispatch(action, target)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: [WEIBO_DEVICE])
        self.owner = "tester"
        opened = self.runtime.session(self.owner, "open", device_id=WEIBO_DEVICE)
        self.session_id = opened["session_id"]
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_shadow", repo_root=self.root)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def run_task(self, payload, timeout=30.0):
        created = self.host.run_task(self.owner, session_id=self.session_id, task=payload)
        task_id = created["task_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in ("RECONCILIATION_REQUIRED",
                                                          "PAUSED", "WAITING_USER"):
                return task_id, status
            time.sleep(0.02)
        self.fail(f"task {task_id} did not finish: {self.host.task_status(self.owner, task_id=task_id)}")

    # -- happy path ---------------------------------------------------------
    def test_two_delegated_subgoals_reach_the_weibo_result_page(self):
        open_search = task_payload(
            "m1-open-search",
            [{"id": "editor", "type": "element_present", "target_key": "搜索",
              "description": "打开微博搜索页"}],
            arguments={"steps": "tap:搜索"}, goal="打开微博搜索页")
        task_id, status = self.run_task(open_search)
        self.assertEqual(status["status"], "SUCCEEDED", status)
        result = self.host.task_result(self.owner, task_id=task_id)
        self.assertTrue(result["available"])
        self.assertEqual(result["result"]["conditions"][0]["verdict"], "pass")
        self.assertGreaterEqual(result["result"]["usage"]["dispatches"], 1)

        search = task_payload(
            "m1-search-query",
            [{"id": "query", "type": "input_equals", "target_key": "id:search_input",
              "value_ref": "query"},
             {"id": "app", "type": "foreground_is", "value": WEIBO}],
            arguments={"query": "鸿蒙",
                       "steps": "replace_text:id:search_input=query"},
            goal="在微博搜索鸿蒙")
        task_id, status = self.run_task(search)
        self.assertEqual(status["status"], "SUCCEEDED", status)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        verdicts = {item["id"]: item["verdict"] for item in result["conditions"]}
        self.assertEqual(verdicts, {"query": "pass", "app": "pass"})
        self.assertEqual(self.devices[-1].query, "鸿蒙")
        self.assertEqual(self.devices[-1].stage, "editor")

        submit = task_payload(
            "m1-submit-search",
            [{"id": "results", "type": "text_equals", "value": "鸿蒙 的相关结果"}],
            arguments={"steps": "tap:id:search_confirm_btn"}, goal="提交搜索")
        task_id, status = self.run_task(submit)
        self.assertEqual(status["status"], "SUCCEEDED", status)
        self.assertEqual(self.devices[-1].stage, "results")

    def test_events_are_paginated_and_ordered(self):
        payload = task_payload(
            "m1-events",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
        task_id, _ = self.run_task(payload)
        events = self.host.task_events(self.owner, task_id=task_id)
        self.assertGreater(len(events["items"]), 0)
        sequences = [item["sequence"] for item in events["items"]]
        self.assertEqual(sequences, sorted(sequences))
        page = self.host.task_events(self.owner, task_id=task_id, after_seq=sequences[0])
        self.assertTrue(all(item["sequence"] > sequences[0] for item in page["items"]))

    def test_shadow_suggestion_is_recorded_without_dispatch(self):
        provider = FakeTransportProvider({})

        async def evaluate(context, questions, *, remaining_model_calls=None):
            from harmony_agent.decision.providers.base import ProviderResult
            criteria = list(questions["action"]["criteria"])
            choice = criteria[0]
            answer = {"action": {"type": "choice", "choice": choice, "confidence": 0.9,
                                 "certainty": 0.8,
                                 "probabilities": {name: 1.0 / len(criteria)
                                                   for name in criteria}}}
            return ProviderResult(provider="decider", model_revision="fake",
                                  answer=answer,
                                  native_scores={"score_semantics": "decider_systemone",
                                                 "answers": answer},
                                  elapsed_ms=2.0)

        provider.evaluate = evaluate
        # v3.2: the injected provider is the generic fast provider slot; the
        # `decider` name is only a deprecated read-only alias.
        self.host.fast_provider = provider
        self.assertIs(self.host.decider, provider)
        payload = task_payload(
            "m1-shadow",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
        task_id, status = self.run_task(payload)
        self.assertEqual(status["status"], "SUCCEEDED", status)
        decisions = [item for item in self.host.task_events(self.owner, task_id=task_id)["items"]
                     if item["type"] == "decision"]
        self.assertTrue(decisions)
        self.assertIn("shadow", decisions[0]["payload"])
        self.assertEqual(decisions[0]["payload"]["shadow"]["route"], "escalate")

    # -- safety paths -------------------------------------------------------
    def test_unknown_write_blocks_and_requires_reconciliation(self):
        self.dispatch_failure = OSError("link lost")
        payload = task_payload(
            "m1-unknown",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
        task_id, status = self.run_task(payload)
        self.assertEqual(status["status"], "RECONCILIATION_REQUIRED", status)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertTrue(result["resolution_required"])
        self.assertEqual(result["unresolved_actions"][0]["execution_status"], "unknown")
        session = self.runtime.session(self.owner, "status", session_id=self.session_id)
        self.assertTrue(session["recovery_required"])

    def test_budget_exhaustion_fails_without_extra_dispatch(self):
        payload = task_payload(
            "m1-budget",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}],
            budget={"max_dispatches": 1, "max_seconds": 1, "max_model_calls": 0})
        task_id, status = self.run_task(payload)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertLessEqual(result["usage"]["dispatches"], 1)
        self.assertIn(status["status"], ("SUCCEEDED", "FAILED", "PARTIAL"))

    def test_duplicate_submission_is_deduplicated_and_conflicts_are_rejected(self):
        payload = task_payload(
            "m1-dedupe",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
        first = self.host.run_task(self.owner, session_id=self.session_id, task=payload)
        second = self.host.run_task(self.owner, session_id=self.session_id, task=payload)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["task_id"], second["task_id"])
        changed = task_payload(
            "m1-dedupe",
            [{"id": "editor", "type": "element_present", "target_key": "我"}])
        with self.assertRaises(Exception):
            self.host.run_task(self.owner, session_id=self.session_id, task=changed)

    def test_control_pause_and_cancel_are_recorded(self):
        payload = task_payload(
            "m1-control",
            [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
        created = self.host.run_task(self.owner, session_id=self.session_id, task=payload)
        task_id = created["task_id"]
        paused = self.host.task_control(self.owner, task_id=task_id, operation="pause")
        self.assertTrue(paused["applied"])
        deadline = time.time() + 5
        status = paused["status"]
        while time.time() < deadline and status == "RUNNING":
            time.sleep(0.02)
            status = self.host.task_status(self.owner, task_id=task_id)["status"]
        self.assertIn(status, ("PAUSED", "SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"))
        self.host.task_control(self.owner, task_id=task_id, operation="cancel")
        events = [item["type"] for item in self.host.task_events(self.owner, task_id=task_id)["items"]]
        self.assertIn("control_pause", events)
        self.assertIn("control_cancel", events)

    def test_unknown_task_is_reported(self):
        with self.assertRaises(TaskError):
            self.host.task_status(self.owner, task_id="task_missing")

    def test_session_status_exposes_device_state(self):
        status = self.runtime.session(self.owner, "status", session_id=self.session_id)
        self.assertEqual(status["device_state"], "ready")
        self.assertFalse(status["worker_quarantined"])

    def test_advisory_decide_never_dispatches(self):
        observation = self.runtime.observe(self.owner, self.session_id, mode="FULL")
        result = self.host.decide(self.owner, session_id=self.session_id,
                                  observation_id=observation["observation_id"],
                                  intent={"action_kind": "tap", "text": "搜索"},
                                  goal="打开搜索页")
        self.assertFalse(result["dispatch_permitted"])
        self.assertIn(result["route"], ("execute", "reobserve", "escalate", "stop"))
        self.assertEqual(self.devices[-1].writes, [])
        # The advisory payload must survive JSON serialisation (the service
        # returns it over IPC).
        json.dumps(result, ensure_ascii=False)

    def test_advisory_decide_rejects_a_foreign_observation(self):
        from harmony_runtime.contracts import RuntimeFault
        with self.assertRaises(RuntimeFault) as context:
            self.host.decide(self.owner, session_id=self.session_id,
                             observation_id="obs_unknown",
                             intent={"action_kind": "tap", "text": "搜索"})
        self.assertEqual(context.exception.code, "stale_observation")


class TaskStorePersistenceTests(unittest.TestCase):
    def test_running_task_becomes_paused_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "tasks.sqlite3"
            store = TaskStore(path)
            task = task_payload(
                "m1-restart",
                [{"id": "editor", "type": "element_present", "target_key": "搜索"}])
            from harmony_agent.contracts import TaskSubmit
            created = store.create(TaskSubmit.model_validate(task), controller_epoch=0)
            store.set_state(created["task_id"], "RUNNING")
            store.close()
            reopened = TaskStore(path)
            self.assertEqual(reopened.state(created["task_id"]), "PAUSED")
            reopened.close()


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.store = ArtifactStore(self.root, Retention(image_days=1, event_days=1,
                                                        quota_bytes=1024 * 1024))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_blob_round_trip_verifies_the_hash(self):
        record = self.store.put_blob(b"hello", kind="event", task_id="task_1")
        self.assertEqual(self.store.read(record["artifact_id"]), b"hello")
        self.assertEqual(record["bytes"], 5)

    def test_expired_artifacts_are_swept_but_pinned_records_survive(self):
        expired = self.store.put_blob(b"old", kind="event", ttl_days=1)
        pinned = self.store.put_blob(b"unknown", kind="unresolved_action")
        report = self.store.sweep(now=time.time() + 10 * 86400)
        self.assertEqual(report["swept"], 1)
        self.assertIsNone(self.store.get(expired["artifact_id"]))
        self.assertIsNotNone(self.store.get(pinned["artifact_id"]))
        self.assertEqual(report["pinned_retained"], 1)

    def test_quota_is_reported_and_enforced(self):
        self.store.put_blob(b"x" * 100, kind="event")
        quota = self.store.quota()
        self.assertEqual(quota["used_bytes"], 100)
        self.assertFalse(self.store.has_room(2 * 1024 * 1024))

    def test_path_guard_rejects_escapes(self):
        with self.assertRaises(ValueError):
            self.store._guard(self.root.parent / "outside.bin")

    def test_redacted_export_has_no_content(self):
        record = self.store.put_blob(b"secret page text", kind="event", task_id="task_1")
        export = self.store.redacted_export("task_1")
        self.assertEqual(export["items"][0]["artifact_id"], record["artifact_id"])
        self.assertNotIn("secret", str(export))
        self.assertNotIn("relative_path", export["items"][0])


if __name__ == "__main__":
    unittest.main()
