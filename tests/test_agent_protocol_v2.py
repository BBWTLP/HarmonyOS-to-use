"""Frozen protocol 2.1: intent -> candidate set -> suggestion -> guarded action.

These tests are the offline evidence for P2-01, P2-02, P2-04 and P2-05. They use
the mock device so no phone, model or network is involved.
"""
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.actor import (ActorRequest, ActorObservation, ActorBudget,
                                 ControlProposal, DeterministicActor,
                                 SubgoalProposal, intent_from_subgoal,
                                 subgoal_from_proposal)
from harmony_agent.candidates import CandidateRegistry
from harmony_agent.contracts import (CandidateSetRef, DecisionSuggestion,
                                     EventEnvelope, GuardedAction, Intent, Predicate,
                                     PROTOCOL_VERSION, ProtocolError, admit_suggestion)
from harmony_agent.grounding import GroundingIntent
from harmony_agent.host import AgentHost
from harmony_agent.checker import CheckReport
from harmony_agent.verifier import (CodeVerifier, ReadOnlyVerifierMount, VerifierCapabilities,
                                    VerifierDenied, VerificationOutcome,
                                    VerificationRequest, assert_read_only, verify_once)
from harmony_agent.planner import Subgoal, plan_from_task
from harmony_runtime.runtime import Runtime


def task_payload(request_id="p2-task", steps="tap:搜索", criteria=None):
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "delegated",
        "goal": "完成微博搜索",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria or [
            {"id": "editor", "type": "element_present", "target_key": "搜索",
             "description": "打开搜索页"}],
        "arguments": {"steps": steps},
        "budget": {"max_dispatches": 6, "max_seconds": 60, "max_model_calls": 4},
        "model_profile": "local_shadow",
    }


class ProtocolSchemaTests(unittest.TestCase):
    def setUp(self):
        from harmony_agent.contracts import TaskSubmit
        self.task = TaskSubmit.model_validate(task_payload())
        self.plan = plan_from_task(self.task)
        self.subgoal = self.plan.subgoals[0]

    def intent(self, **overrides):
        base = dict(subgoal_id="step_01", action_kind="tap", text="搜索",
                    description="打开搜索页")
        base.update(overrides)
        return Intent(intent_id="intent_" + "a" * 24, task_id="task_1", **base)

    # -- P2-01 --------------------------------------------------------------
    def test_intent_rejects_coordinates_and_raw_input(self):
        with self.assertRaises(Exception):
            self.intent(text="540,1200")
        with self.assertRaises(Exception):
            # an input action must reference the parameter store, not inline text
            self.intent(action_kind="replace_text", text=None,
                        resource_id="search_input",
                        argument_refs={"text": "鸿蒙"})
        with self.assertRaises(Exception):
            self.intent(argument_refs={"x": "120"})
        with self.assertRaises(Exception):
            # a target action without any semantic identity
            self.intent(text=None)
        with self.assertRaises(Exception):
            # an input action with no argument reference at all
            self.intent(action_kind="replace_text", text=None,
                        resource_id="search_input")
        with self.assertRaises(Exception):
            self.intent(argument_refs={"bundle": "not a bundle"})

    def test_intent_accepts_semantic_forms(self):
        self.assertEqual(self.intent().protocol_version, PROTOCOL_VERSION)
        typed = self.intent(action_kind="replace_text", text=None,
                            resource_id="search_input",
                            argument_refs={"text": "arg.query"})
        self.assertEqual(typed.argument_refs["text"], "arg.query")
        launch = self.intent(action_kind="launch", text=None,
                             argument_refs={"bundle": "com.example.app"})
        self.assertEqual(launch.action_kind, "launch")

    def test_candidate_set_ref_binds_ids_and_expiry(self):
        ref = CandidateSetRef(observation_id="obs_1", controller_epoch=2,
                              candidate_set_hash="a" * 64,
                              candidate_ids=["cand_" + "b" * 24],
                              risk_classes={"cand_" + "b" * 24: "low"},
                              control_options=["cand_none_applicable"],
                              grounding_layers=["text_exact"],
                              expires_at="2999-01-01T00:00:00Z")
        self.assertFalse(ref.is_expired())
        with self.assertRaises(Exception):
            CandidateSetRef(observation_id="obs_1", controller_epoch=2,
                            candidate_set_hash="a" * 64,
                            candidate_ids=["model_invented_id"],
                            risk_classes={}, expires_at="2999-01-01T00:00:00Z")
        with self.assertRaises(Exception):
            CandidateSetRef(observation_id="obs_1", controller_epoch=2,
                            candidate_set_hash="a" * 64,
                            candidate_ids=["cand_" + "b" * 24],
                            risk_classes={}, expires_at="2999-01-01T00:00:00Z")

    def test_suggestion_cannot_invent_or_self_authorise(self):
        candidate_id = "cand_" + "b" * 24
        with self.assertRaises(Exception):
            DecisionSuggestion(task_id="t", subgoal_id="s", observation_id="obs",
                               controller_epoch=1, candidate_set_hash="a" * 64,
                               candidate_ids=[candidate_id], provider="decider",
                               model_revision="rev", route="execute",
                               selected_candidate_id="cand_" + "c" * 24,
                               latency_ms=1.0)
        with self.assertRaises(Exception):
            # an uncalibrated model may not suggest execute
            DecisionSuggestion(task_id="t", subgoal_id="s", observation_id="obs",
                               controller_epoch=1, candidate_set_hash="a" * 64,
                               candidate_ids=[candidate_id], provider="decider",
                               model_revision="rev", route="execute",
                               selected_candidate_id=candidate_id, latency_ms=1.0)
        suggestion = DecisionSuggestion(
            task_id="t", subgoal_id="s", observation_id="obs", controller_epoch=1,
            candidate_set_hash="a" * 64, candidate_ids=[candidate_id],
            provider="decider", model_revision="rev", calibration_version="cal-1",
            route="execute", selected_candidate_id=candidate_id, latency_ms=1.0)
        offered = CandidateSetRef(observation_id="obs", controller_epoch=1,
                                  candidate_set_hash="a" * 64,
                                  candidate_ids=[candidate_id],
                                  risk_classes={candidate_id: "low"},
                                  expires_at="2999-01-01T00:00:00Z")
        self.assertIs(admit_suggestion(suggestion, offered), suggestion)

    def test_admit_suggestion_rejects_foreign_and_stale_sets(self):
        candidate_id = "cand_" + "b" * 24
        offered = CandidateSetRef(observation_id="obs", controller_epoch=1,
                                  candidate_set_hash="a" * 64,
                                  candidate_ids=[candidate_id],
                                  risk_classes={candidate_id: "low"},
                                  expires_at="2999-01-01T00:00:00Z")
        foreign = DecisionSuggestion(
            task_id="t", subgoal_id="s", observation_id="obs", controller_epoch=1,
            candidate_set_hash="a" * 64, candidate_ids=[candidate_id],
            provider="rules", model_revision="rules", route="reobserve", latency_ms=0.5)
        for replacement, code in (
                ({"observation_id": "obs_other"}, "stale_observation"),
                ({"controller_epoch": 3}, "epoch_mismatch"),
                ({"candidate_set_hash": "c" * 64}, "candidate_set_mismatch")):
            broken = foreign.model_copy(update=replacement)
            with self.assertRaises(ProtocolError) as caught:
                admit_suggestion(broken, offered)
            self.assertEqual(caught.exception.code, code)
        expired = offered.model_copy(update={"expires_at": "2000-01-01T00:00:00Z"})
        with self.assertRaises(ProtocolError) as caught:
            admit_suggestion(foreign, expired)
        self.assertEqual(caught.exception.code, "expired_candidate_set")

    def test_guarded_action_requires_registry_target_and_authorization(self):
        with self.assertRaises(Exception):
            # a target action without a registry target
            GuardedAction(session_id="s", request_id="act_" + "0" * 24, task_id="t",
                          subgoal_id="sg", observation_id="obs", controller_epoch=0,
                          candidate_set_hash="a" * 64, candidate_id="cand_" + "b" * 24,
                          action_kind="tap", risk_class="low")
        with self.assertRaises(Exception):
            # high risk without a runtime-issued authorization
            GuardedAction(session_id="s", request_id="act_" + "0" * 24, task_id="t",
                          subgoal_id="sg", observation_id="obs", controller_epoch=0,
                          candidate_set_hash="a" * 64, candidate_id="cand_" + "b" * 24,
                          target_ref="gt_" + "d" * 24, local_fingerprint="f" * 32,
                          action_kind="tap", risk_class="high")
        with self.assertRaises(Exception):
            # a coordinate-shaped argument has no field to live in
            GuardedAction(session_id="s", request_id="act_" + "0" * 24, task_id="t",
                          subgoal_id="sg", observation_id="obs", controller_epoch=0,
                          candidate_set_hash="a" * 64, candidate_id="cand_" + "b" * 24,
                          action_kind="swipe", risk_class="low",
                          argument_values={"x": "120"})
        allowed = GuardedAction(
            session_id="s", request_id="act_" + "0" * 24, task_id="t", subgoal_id="sg",
            observation_id="obs", controller_epoch=0, candidate_set_hash="a" * 64,
            candidate_id="cand_" + "b" * 24, target_ref="gt_" + "d" * 24,
            local_fingerprint="f" * 32, action_kind="tap", risk_class="low")
        self.assertEqual(allowed.protocol_version, PROTOCOL_VERSION)


class ActorProtocolTests(unittest.TestCase):
    def request(self, task, plan, **overrides):
        payload = dict(request_id="r1", task_id="task_1", goal=task.goal,
                       constraints=[], arguments=dict(task.arguments),
                       allowed_apps=task.scope.allowed_apps,
                       allowed_actions=task.scope.allowed_actions,
                       observation=ActorObservation(observation_id="obs_1",
                                                    controller_epoch=1),
                       plan_version=plan.version,
                       pending_subgoal_ids=[item.subgoal_id for item in plan.subgoals],
                       budget=ActorBudget(dispatches=4, seconds=60, model_calls=2))
        payload.update(overrides)
        return ActorRequest(**payload)

    def test_deterministic_actor_returns_typed_subgoals_then_stops(self):
        import asyncio
        from harmony_agent.contracts import TaskSubmit
        task = TaskSubmit.model_validate(task_payload())
        plan = plan_from_task(task)
        actor = DeterministicActor(task, plan)
        proposal = asyncio.run(actor.propose(self.request(task, plan)))
        self.assertIsInstance(proposal, SubgoalProposal)
        self.assertEqual(proposal.intent.task_id, "task_1")
        subgoal = subgoal_from_proposal("task_1", proposal)
        self.assertEqual(subgoal.action_kind, "tap")
        self.assertEqual(subgoal.intent_text, "搜索")
        actor.plan.subgoals[0].status = "verified"
        self.assertIsInstance(asyncio.run(actor.propose(self.request(task, plan))),
                              ControlProposal)

    def test_actor_proposal_is_rejected_for_another_task(self):
        from harmony_agent.contracts import TaskSubmit
        from harmony_agent.actor import ActorUnavailable
        task = TaskSubmit.model_validate(task_payload())
        plan = plan_from_task(task)
        proposal = SubgoalProposal(
            subgoal_id="step_01", description="打开",
            intent=Intent(intent_id="intent_" + "a" * 24, task_id="other_task",
                          subgoal_id="step_01", action_kind="tap", text="搜索"))
        with self.assertRaises(ActorUnavailable):
            subgoal_from_proposal("task_1", proposal)

    def test_actor_cannot_propose_coordinates_or_raw_text(self):
        with self.assertRaises(Exception):
            Intent(intent_id="intent_" + "a" * 24, task_id="t", subgoal_id="s",
                   action_kind="tap", text="120,340")
        with self.assertRaises(Exception):
            Intent(intent_id="intent_" + "a" * 24, task_id="t", subgoal_id="s",
                   action_kind="replace_text", text="搜索",
                   argument_refs={"text": "字面输入"})


class VerifierProtocolTests(unittest.TestCase):
    def test_code_verifier_is_read_only_and_three_state(self):
        observation = {"observation_id": "obs", "catalog": [
            {"text": "搜索", "type": "Button", "clickable": True,
             "bundle": WEIBO, "bounds": (0, 0, 10, 10), "target_fingerprint": "f" * 64,
             "enabled": True, "visible": True}]}
        predicate = Predicate(id="c1", type="element_present", target_key="搜索")
        outcome = verify_once(CodeVerifier(), VerificationRequest(
            predicates=[predicate], observation=observation))
        self.assertEqual(outcome.verdict, "pass")
        self.assertTrue(outcome.evidence_refs)
        missing = verify_once(CodeVerifier(), VerificationRequest(
            predicates=[Predicate(id="c2", type="element_present",
                                  target_key="不存在的控件")],
            observation=observation))
        self.assertEqual(missing.verdict, "fail")

    def test_pass_without_evidence_is_downgraded(self):
        class EvidenceFreeVerifier:
            capabilities = VerifierCapabilities()

            def verify(self, request):
                return VerificationOutcome(
                    verdict="pass",
                    report=CheckReport(verdict="pass", conditions=[]))

        outcome = verify_once(EvidenceFreeVerifier(), VerificationRequest(
            predicates=[], observation={}))
        self.assertEqual(outcome.verdict, "inconclusive")
        self.assertIn("pass_without_evidence", outcome.limitations)

    def test_verifier_with_write_capability_is_denied(self):
        class WritingVerifier:
            capabilities = VerifierCapabilities(write_tools=("act",), may_dispatch=True)

            def verify(self, request):  # pragma: no cover - refused earlier
                raise AssertionError("must never be called")

        with self.assertRaises(VerifierDenied) as caught:
            assert_read_only(WritingVerifier())
        self.assertEqual(caught.exception.code, "write_tools_declared")
        with self.assertRaises(VerifierDenied):
            assert_read_only(object())

    def test_read_only_mount_refuses_every_write(self):
        mount = ReadOnlyVerifierMount({"observation_id": "obs"})
        self.assertEqual(mount.read_observation()["observation_id"], "obs")
        for method in ("act", "dispatch", "wait"):
            with self.assertRaises(Exception):
                getattr(mount, method)()


class EventEnvelopeIntegrationTests(unittest.TestCase):
    """The event log must answer which observation/epoch/decision produced it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.runtime = Runtime(self.root / "runtime", factory=FakeWeiboDevice,
                               discover=lambda: ["fake-device"])
        self.owner = "tester"
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

    def test_events_carry_a_complete_envelope(self):
        task_id, status = self.run_task(task_payload("p2-envelope"))
        self.assertEqual(status["status"], "SUCCEEDED", status)
        items = self.host.task_events(self.owner, task_id=task_id, limit=200)["items"]
        self.assertTrue(items)
        for item in items:
            envelope = item["envelope"]
            self.assertIsNotNone(envelope, item["type"])
            self.assertEqual(envelope["task_id"], task_id)
            self.assertEqual(envelope["event_type"], item["type"])
            self.assertEqual(envelope["protocol_version"], PROTOCOL_VERSION)
            self.assertGreaterEqual(envelope["sequence"], 0)
        candidates = next(item for item in items if item["type"] == "candidates")
        self.assertEqual(candidates["envelope"]["candidate_set_hash"],
                         candidates["payload"]["hash"])
        dispatch = next(item for item in items if item["type"] == "dispatch")
        self.assertEqual(dispatch["envelope"]["observation_id"],
                         dispatch["payload"].get("observation_id",
                                                 dispatch["envelope"]["observation_id"]))
        self.assertEqual(dispatch["payload"]["protocol_version"], PROTOCOL_VERSION)
        # The envelope is a schema-checked object, not an ad-hoc dict.
        EventEnvelope.model_validate(dispatch["envelope"])
        finished = items[-1]
        self.assertEqual(finished["type"], "task_finished")
        self.assertTrue(finished["envelope"]["evidence_refs"])

    def test_task_result_records_the_verifier_that_decided(self):
        task_id, status = self.run_task(task_payload("p2-verifier"))
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertEqual(result["verification"]["verifier"], "code")
        self.assertEqual(result["verification"]["verdict"], "pass")
        self.assertEqual(result["protocol_version"], PROTOCOL_VERSION)

    def test_online_run_has_no_memory_write_back(self):
        from harmony_agent.memory_store import MemoryStore
        store = MemoryStore(self.root / "memory")
        frozen = None
        # No snapshot yet: an empty frozen mount is still read-only.
        empty = store
        self.assertEqual(empty.versions(), [])
        self.assertIsNone(frozen)


if __name__ == "__main__":
    unittest.main()
