"""Trusted reconciliation of one unknown write (P1-04).

An unknown write blocks every further write on the device. It may only be closed
against fresh evidence: either the action's original semantic postcondition now
holds, or the page fingerprint is byte-identical to the pre-dispatch fingerprint
and a caller attests that the action did not execute. Reconciliation never
dispatches and never replays.
"""
import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.host import AgentHost
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime


class LostResponseDevice(FakeWeiboDevice):
    """Loses the response of the first write, with or without applying it."""

    def __init__(self, serial, *, apply_first: bool = True):
        super().__init__(serial)
        self.apply_first = apply_first
        self.lost = False
        self.dispatch_attempts = 0

    def dispatch(self, action, target):
        self.dispatch_attempts += 1
        if not self.lost:
            self.lost = True
            if self.apply_first:
                super().dispatch(action, target)
            raise OSError("link lost during dispatch")
        return super().dispatch(action, target)


def task_payload(request_id, *, criteria, steps="tap:搜索"):
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "delegated",
        "goal": "打开微博搜索页",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria,
        "arguments": {"steps": steps},
        "budget": {"max_dispatches": 4, "max_seconds": 60, "max_model_calls": 4},
        "model_profile": "local_shadow",
    }


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.apply_first = True
        outer = self

        class Device(LostResponseDevice):
            def __init__(self, serial):
                super().__init__(serial, apply_first=outer.apply_first)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: ["fake-device"])
        self.owner = "operator"
        self.session_id = self.runtime.session(
            self.owner, "open", device_id="fake-device")["session_id"]
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_shadow", repo_root=self.root)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def run_task(self, payload, timeout=20.0):
        created = self.host.run_task(self.owner, session_id=self.session_id, task=payload)
        task_id = created["task_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in (
                    "RECONCILIATION_REQUIRED", "PAUSED", "WAITING_USER"):
                return task_id, status
            time.sleep(0.02)
        self.fail("task did not finish")

    def unknown_write(self, *, criteria):
        payload = task_payload("recon-1", criteria=criteria)
        task_id, status = self.run_task(payload)
        self.assertEqual(status["status"], "RECONCILIATION_REQUIRED", status)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        request_id = result["unresolved_actions"][0]["request_id"]
        session = self.runtime.session(self.owner, "status", session_id=self.session_id)
        self.assertTrue(session["recovery_required"])
        return request_id

    def unknown_direct_write(self, *, expected):
        """One v1 mobile_act whose response is lost after the phone applied it.

        The v1 path is the one that stores a semantic (salted text) recovery
        condition, so it is the only path that can be closed as
        ``postcondition_verified``.
        """
        observation = self.runtime.observe(self.owner, self.session_id, mode="FAST")
        result = self.runtime.act(self.owner, {
            "session_id": self.session_id,
            "request_id": "act_lost_response_1",
            "observation_id": observation["observation_id"],
            "action": {"kind": "tap", "target": {"text": "搜索"}},
            "expected": expected,
            "timeout_ms": 8000})
        self.assertEqual(result["execution_status"], "unknown", result)
        self.assertTrue(result["incident_id"])
        return "act_lost_response_1"

    # -- positive paths -----------------------------------------------------
    def test_postcondition_verified_closes_the_barrier_without_replay(self):
        request_id = self.unknown_direct_write(expected={"text": "热搜榜"})
        outcome = self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                       request_id=request_id,
                                       evidence_kind="postcondition_verified")
        self.assertEqual(outcome["status"], "reconciled")
        self.assertEqual(outcome["evidence"]["reason"],
                         "original_text_postcondition_observed")
        self.assertEqual(outcome["replay"], "never")
        session = self.runtime.session(self.owner, "status", session_id=self.session_id)
        self.assertFalse(session["recovery_required"])
        self.assertEqual(session["unresolved_actions"], [])
        # The original dispatch is never repeated: the device saw exactly one write.
        device = self.runtime.devices["fake-device"]
        self.assertEqual(device.dispatch_attempts, 1)
        # A later write is admitted again through the normal guard.
        observation = self.runtime.observe(self.owner, self.session_id, mode="FAST")
        follow_up = self.runtime.act(self.owner, {
            "session_id": self.session_id,
            "request_id": "act_after_reconcile",
            "observation_id": observation["observation_id"],
            "action": {"kind": "tap", "target": {"text": "热搜榜"}},
            "expected": {"changed": True},
            "timeout_ms": 8000})
        self.assertEqual(follow_up["execution_status"], "executed", follow_up)
        self.assertEqual(device.dispatch_attempts, 2)
        # A second reconcile attempt on a closed incident is refused.
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                 request_id=request_id,
                                 evidence_kind="postcondition_verified")
        self.assertEqual(fault.exception.code, "incident_already_closed")

    def test_not_executed_requires_an_unchanged_page_and_an_attestation(self):
        self.apply_first = False  # the write never reached the phone
        request_id = self.unknown_write(
            criteria=[{"id": "editor", "type": "text_equals", "value": "热搜榜"}])
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                 request_id=request_id, evidence_kind="not_executed")
        self.assertEqual(fault.exception.code, "attestation_required")
        outcome = self.runtime.session(
            self.owner, "reconcile", session_id=self.session_id, request_id=request_id,
            evidence_kind="not_executed",
            attestation="checked the phone: the tap never registered")
        self.assertEqual(outcome["status"], "reconciled")
        self.assertEqual(outcome["evidence"]["reason"], "page_unchanged_since_dispatch")
        self.assertEqual(outcome["action_state"], "reconciled_not_executed")
        self.assertFalse(self.runtime.session(
            self.owner, "status", session_id=self.session_id)["recovery_required"])
        self.assertEqual(self.runtime.devices["fake-device"].dispatch_attempts, 1)

    # -- refusals -----------------------------------------------------------
    def test_postcondition_that_does_not_hold_is_refused(self):
        self.apply_first = False
        request_id = self.unknown_write(
            criteria=[{"id": "editor", "type": "text_equals", "value": "热搜榜"}])
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                 request_id=request_id,
                                 evidence_kind="postcondition_verified")
        self.assertEqual(fault.exception.code, "evidence_insufficient")
        self.assertTrue(self.runtime.session(
            self.owner, "status", session_id=self.session_id)["recovery_required"])

    def test_changed_page_cannot_be_attested_as_not_executed(self):
        request_id = self.unknown_write(
            criteria=[{"id": "editor", "type": "element_present", "target_key": "热搜榜"}])
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                 request_id=request_id, evidence_kind="not_executed",
                                 attestation="assumed no effect")
        self.assertEqual(fault.exception.code, "evidence_insufficient")
        self.assertTrue(self.runtime.session(
            self.owner, "status", session_id=self.session_id)["recovery_required"])

    def test_stale_observation_and_unknown_evidence_kind_are_refused(self):
        self.apply_first = False
        request_id = self.unknown_write(
            criteria=[{"id": "editor", "type": "text_equals", "value": "热搜榜"}])
        incident = self.runtime.journal.incidents("fake-device")[0]
        stale = {"observation_id": "obs_old", "fingerprint": "f" * 64,
                 "captured_at": incident["created"] - 1, "catalog": []}
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.journal.close_incident_with_evidence(
                "fake-device", request_id, stale, evidence_kind="not_executed",
                attestation="stale")
        self.assertEqual(fault.exception.code, "evidence_stale")
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                 request_id=request_id, evidence_kind="trust_me")
        self.assertEqual(fault.exception.code, "invalid_arguments")
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                 request_id="not-a-request")
        self.assertEqual(fault.exception.code, "unknown_request")
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session("outsider", "reconcile", session_id=self.session_id,
                                 request_id=request_id,
                                 evidence_kind="postcondition_verified")
        self.assertEqual(fault.exception.code, "session_invalid")
        # Legacy records without a device binding are never closed per device.
        self.runtime.journal.begin("legacy-request", "digest")
        self.runtime.journal.finish("legacy-request", {
            "execution_status": "unknown", "verification_status": "inconclusive"})
        observation = self.runtime.observe(self.owner, self.session_id, mode="FAST")
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.journal.close_incident_with_evidence(
                "fake-device", "legacy-request", observation,
                evidence_kind="not_executed", attestation="legacy")
        self.assertEqual(fault.exception.code, "unknown_request")

    def test_status_queries_reject_reconciliation_arguments(self):
        with self.assertRaises(RuntimeFault) as fault:
            self.runtime.session(self.owner, "action_status", session_id=self.session_id,
                                 request_id="any", evidence_kind="not_executed")
        self.assertEqual(fault.exception.code, "invalid_arguments")

    def test_an_agent_task_unknown_write_is_reconcilable_by_its_own_goal(self):
        """The task's terminal condition is recorded as recovery evidence.

        Before this, an agent task's dispatch carried only "the page changed",
        so its unknown write could never be closed as `postcondition_verified` —
        only as "the page did not move". The planner now binds the task's own
        semantic criterion to the final step's recovery condition.
        """
        request_id = self.unknown_write(criteria=[
            {"id": "editor", "type": "text_equals", "value": "热搜榜"}])
        # The ledger shows a semantic condition, not only a page fingerprint.
        row = self.runtime.journal.db.execute(
            "SELECT expected FROM recovery_conditions WHERE request_id=?",
            (request_id,)).fetchone()
        self.assertTrue(row[0], "no recovery condition was recorded")
        stored = json.loads(row[0])
        self.assertEqual(stored.get("format"), "text_digest_v1")
        outcome = self.runtime.session(self.owner, "reconcile", session_id=self.session_id,
                                       request_id=request_id,
                                       evidence_kind="postcondition_verified")
        self.assertEqual(outcome["status"], "reconciled")
        self.assertEqual(outcome["evidence"]["reason"],
                         "original_text_postcondition_observed")
        self.assertFalse(self.runtime.session(
            self.owner, "status", session_id=self.session_id)["recovery_required"])
        self.assertEqual(self.runtime.devices["fake-device"].dispatch_attempts, 1)

    def test_the_bound_goal_does_not_turn_a_failed_step_into_retries(self):
        """A declared goal that is not reached must not cause extra dispatches."""
        self.apply_first = False
        payload = task_payload("recon-no-retry", criteria=[
            {"id": "editor", "type": "text_equals", "value": "这个文本不会出现"}])
        task_id, status = self.run_task(payload)
        self.assertIn(status["status"], ("FAILED", "PARTIAL", "RECONCILIATION_REQUIRED"))
        device = self.runtime.devices["fake-device"]
        self.assertEqual(device.dispatch_attempts, 1)


if __name__ == "__main__":
    unittest.main()
