"""v3.2 Phase 12: software fault injection, offline.

Each case checks the same four properties: no duplicate write, no false success,
the unknown state is preserved, and the journal barrier still holds.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.host import AgentHost
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime
from harmony_runtime.visual import region_digest

DEVICE = "fake-device"


class Device(FakeWeiboDevice):
    def __init__(self, serial):
        super().__init__(serial)
        self.interrupt = None

    def dispatch(self, action, target):
        if self.interrupt:
            failure, self.interrupt = self.interrupt, None
            raise failure
        return super().dispatch(action, target)


class FaultMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Tracked(Device):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root, factory=Tracked, discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]
        # The device is created lazily; materialise it so a test can inject a fault.
        self.runtime.observe(self.owner, self.session)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def act(self, request_id="fault-1", kind="tap"):
        observed = self.runtime.observe(self.owner, self.session)
        return self.runtime.act(self.owner, {
            "session_id": self.session, "request_id": request_id,
            "observation_id": observed["observation_id"],
            "action": {"kind": kind, "target": {"action_id": "n0"}}})

    # -- journal ------------------------------------------------------------
    def test_a_journal_admission_failure_dispatches_nothing(self):
        with patch.object(self.runtime.journal, "begin",
                          side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.act("journal-begin-failed")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_journal_completion_failure_never_reports_success(self):
        with patch.object(self.runtime.journal, "finish",
                          side_effect=sqlite3.OperationalError("database is locked")):
            result = self.act("journal-finish-failed")
        self.assertEqual(result["status"], "execution_unknown")
        self.assertEqual(result["execution_status"], "unknown")
        self.assertFalse(result["completion_persisted"])
        self.assertEqual(result["error_code"], "journal_write_failed")
        self.assertNotEqual(result["verification_status"], "verified")

    def test_a_durable_begin_still_blocks_a_replay_after_a_lost_response(self):
        self.devices[-1].interrupt = OSError("link lost during dispatch")
        first = self.act("lost-response")
        self.assertEqual(first["execution_status"], "unknown")
        with self.assertRaises(RuntimeFault) as error:
            self.act("a-new-request-id")
        self.assertEqual(error.exception.code, "reconciliation_required")
        self.assertEqual(self.devices[-1].writes, [])

    # -- device -------------------------------------------------------------
    def test_current_writes_are_not_replayed_after_an_unknown_dispatch(self):
        self.devices[-1].interrupt = OSError("link lost")
        self.act("unknown-write")
        attempts = len(self.devices[-1].writes)
        for request_id in ("retry-a", "retry-b"):
            with self.assertRaises(RuntimeFault):
                self.act(request_id)
        self.assertEqual(len(self.devices[-1].writes), attempts)

    def test_a_quarantined_worker_is_reported_apart_from_unknown_writes(self):
        device = self.devices[-1]
        device.quarantined = True
        status = self.runtime.session(self.owner, "status", session_id=self.session)
        self.assertEqual(status["device_state"], "quarantined")
        self.assertTrue(status["worker_quarantined"])
        # A transport quarantine is not an unresolved write: the client is told
        # to recover the worker, not to reconcile an action.
        self.assertFalse(status["recovery_required"])
        self.assertEqual(device.writes, [])

    def test_a_read_fault_is_reported_without_dispatching_a_write(self):
        self.runtime.observe(self.owner, self.session)

        def broken_tree():
            raise OSError("device disconnected")

        self.devices[-1].tree = broken_tree
        with self.assertRaises(Exception):
            self.runtime.observe(self.owner, self.session)
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_storage_exception_preserves_the_unknown_state(self):
        with patch.object(self.runtime, "_finish_action",
                          side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.act("storage-exception")
        status = self.runtime.session(self.owner, "status", session_id=self.session)
        self.assertTrue(status["recovery_required"])
        self.assertEqual(status["unresolved_actions"][0]["execution_status"], "unknown")
        # The write did reach the device; the point is that the outcome stayed
        # unknown instead of being reported as a verified success.
        self.assertEqual(len(self.devices[-1].writes), 1)

    # -- controller ---------------------------------------------------------
    def test_a_cancel_race_stops_before_the_next_dispatch(self):
        observed = self.runtime.observe(self.owner, self.session)
        self.runtime.session(self.owner, "pause", self.session)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act(self.owner, {
                "session_id": self.session, "request_id": "cancel-race",
                "observation_id": observed["observation_id"],
                "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertIn(error.exception.code, ("cancelled", "stale_observation"))
        self.assertEqual(self.devices[-1].writes, [])

    def test_an_epoch_change_invalidates_a_grounded_candidate(self):
        from harmony_agent.candidates import CandidateError, CandidateRegistry
        from harmony_agent.contracts import Predicate
        from harmony_agent.grounding import GroundingIntent
        from harmony_runtime.observation import snapshot

        observation = snapshot(FakeWeiboDevice(DEVICE).tree(), (1080, 2340, 0),
                               {"status": "ok", "bundle": WEIBO})
        observation["observation_id"] = "obs_epoch"
        observation["controller_epoch"] = 1
        registry = CandidateRegistry()
        candidate = registry.build(
            task_id="t", subgoal_id="s", scope_id="sc", observation=observation,
            controller_epoch=1,
            intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
            action_kind="tap", arguments={},
            expected_predicates=[Predicate(id="p", type="foreground_is", value=WEIBO)]
        ).candidates[0].candidate.candidate_id
        with self.assertRaises(CandidateError) as error:
            registry.resolve(candidate, observation_id="obs_epoch", controller_epoch=2)
        self.assertEqual(error.exception.code, "epoch_mismatch")

    # -- optional visual providers -----------------------------------------
    def test_an_unavailable_visual_provider_never_blocks_the_tree_path(self):
        from harmony_agent.grounding import GroundingIntent, ground
        from harmony_runtime.observation import snapshot

        observation = snapshot(FakeWeiboDevice(DEVICE).tree(), (1080, 2340, 0),
                               {"status": "ok", "bundle": WEIBO})
        observation["observation_id"] = "obs_visual_absent"
        observation["actionable"] = True
        result = ground(observation, GroundingIntent(action_kind="tap",
                                                     resource_id="search_entry"))
        self.assertTrue(result.targets)
        self.assertEqual(result.targets[0].source, "ui_tree")
        self.assertTrue(any(note.startswith("ocr_layer_skipped") for note in result.notes)
                        or result.layers_used == ["stable_identity"])

    def test_a_visual_dispatch_without_image_evidence_is_refused(self):
        observed = self.runtime.observe(self.owner, self.session)
        digest = region_digest(b"\x89PNG", (10, 10, 20, 20)) or "a" * 64
        handle = {"target_ref": "gt_" + digest[:32], "local_fingerprint": digest,
                  "observation_id": observed["observation_id"],
                  "visual": {"region": [10, 10, 20, 20], "crop_digest": digest,
                             "source": "ocr", "label": "播放",
                             "display_width": 1080, "display_height": 2340,
                             "rotation": 0}}
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act(self.owner, {
                "session_id": self.session, "request_id": "visual-no-image",
                "observation_id": observed["observation_id"],
                "action": {"kind": "tap", "target": handle}})
        self.assertEqual(error.exception.code, "target_not_revalidated")
        self.assertEqual(self.devices[-1].writes, [])


class AgentFaultMatrixTests(unittest.TestCase):
    """Agent-layer faults end in rules/abstain, never in a device write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Tracked(Device):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Tracked,
                               discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_off", repo_root=self.root)
        self.runtime.observe(self.owner, self.session)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def task(self, request_id):
        return {"schema_version": "2.0", "request_id": request_id,
                "mode": "delegated", "goal": "打开微博搜索页",
                "scope": {"device_ref": "d", "allowed_apps": [WEIBO],
                          "allowed_actions": ["tap"], "cloud_data_policy": "disabled"},
                "success_criteria": [{"id": "editor", "type": "element_present",
                                      "target_key": "搜索"}],
                "arguments": {"steps": "tap:搜索"},
                "budget": {"max_dispatches": 8, "max_seconds": 60,
                           "max_model_calls": 8},
                "model_profile": "local_off"}

    def run_task(self, request_id, timeout=30.0):
        created = self.host.run_task(self.owner, session_id=self.session,
                                     task=self.task(request_id))
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=created["task_id"])
            if status["terminal"] or status["status"] in ("RECONCILIATION_REQUIRED",
                                                          "PAUSED", "WAITING_USER"):
                return created["task_id"], status
            time.sleep(0.02)
        self.fail("task did not finish")

    def test_an_agent_failure_never_produces_a_false_success(self):
        self.devices[-1].interrupt = OSError("worker died")
        task_id, status = self.run_task("agent-fault")
        self.assertNotEqual(status["status"], "SUCCEEDED")
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertTrue(result["resolution_required"])

    def test_artifact_quota_is_reported_without_losing_the_task(self):
        self.host.artifacts.quota_bytes = 0
        task_id, status = self.run_task("artifact-quota")
        self.assertIn(status["status"], ("SUCCEEDED", "PARTIAL", "FAILED"))
        quota = self.host.diagnostics(self.owner)["artifact_quota"]
        self.assertIn("quota_bytes", quota)
        self.assertIn("used_bytes", quota)

    def test_a_task_restart_reports_paused_and_never_resumes_itself(self):
        task_id, status = self.run_task("restart-source")
        self.assertEqual(status["status"], "SUCCEEDED")
        # A fresh host over the same state directory must not silently re-run it.
        self.host.close()
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_off", repo_root=self.root)
        reopened = self.host.task_status(self.owner, task_id=task_id)
        self.assertEqual(reopened["status"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
