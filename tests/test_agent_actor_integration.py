"""Actor integration in the task runner (P4-01, offline half).

The actor is the only component that may propose "try this next". These tests fix
the two properties that matter for a phone: an actor proposal cannot bypass the
guard (it is validated by the intent contract and executed through the ordinary
loop), and it cannot run forever (bounded recovery proposals, remaining budget).

Everything here runs against the fake device through the real Runtime.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.actor import (ActorUnavailable, ControlProposal, SubgoalProposal,
                                 intent_from_subgoal)
from harmony_agent.contracts import Intent
from harmony_agent.host import AgentHost
from harmony_agent.planner import Subgoal
from harmony_runtime.runtime import Runtime


def task_payload(request_id, *, steps, criteria, budget=None):
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "delegated",
        "goal": "在微博搜索页输入查询词",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria,
        "arguments": {"steps": steps, "query": "鸿蒙"},
        "budget": budget or {"max_dispatches": 6, "max_seconds": 60,
                             "max_model_calls": 4},
        "model_profile": "local_off",
    }


def recovery_subgoal(subgoal_id="actor_step_01", *, text="搜索", kind="tap"):
    return Subgoal(subgoal_id=subgoal_id, description="actor recovery",
                   intent_text=text, action_kind=kind)


class ScriptedActor:
    """An actor that answers with pre-scripted proposals or control exits.

    An answer may be a callable taking the request, which is how a real actor
    learns the allocated task id.
    """

    name = "scripted"
    revision = "test:1"

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests: list[object] = []

    async def propose(self, request):
        self.requests.append(request)
        if not self.answers:
            return ControlProposal(control="stop", reason_code="no_more_ideas")
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        return answer


def proposal_for(task_id, subgoal: Subgoal) -> SubgoalProposal:
    return SubgoalProposal(subgoal_id=subgoal.subgoal_id,
                           description=subgoal.description or subgoal.action_kind,
                           intent=intent_from_subgoal(task_id, subgoal))


class ActorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.runtime = Runtime(self.root / "runtime", factory=FakeWeiboDevice,
                               discover=lambda: ["fake-device"])
        self.owner = "tester"
        self.session_id = self.runtime.session(
            self.owner, "open", device_id="fake-device")["session_id"]
        self.devices: list[FakeWeiboDevice] = []
        self.hosts: list[AgentHost] = []

    def tearDown(self):
        for host in self.hosts:
            host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def host(self, actor, *, max_replans=2) -> AgentHost:
        host = AgentHost(self.runtime, self.root / f"agent-{len(self.hosts)}",
                         profile="local_off", repo_root=self.root, actor=actor)
        self.hosts.append(host)
        return host

    def run_task(self, host, payload, timeout=40.0):
        created = host.run_task(self.owner, session_id=self.session_id, task=payload)
        task_id = created["task_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in (
                    "RECONCILIATION_REQUIRED", "PAUSED", "WAITING_USER"):
                return task_id, status
            time.sleep(0.02)
        self.fail("task did not finish")

    def test_a_failed_step_can_be_recovered_by_the_actor(self):
        """The first step targets a control that does not exist; the actor's
        recovery step does the real work."""
        actor = ScriptedActor([
            lambda request: proposal_for(request.task_id, recovery_subgoal(text="搜索"))])
        host = self.host(actor)
        payload = task_payload("actor-recovery",
                               steps="tap:不存在的按钮",
                               criteria=[{"id": "editor", "type": "element_present",
                                          "target_key": "热搜榜"}])
        task_id, status = self.run_task(host, payload)
        self.assertEqual(status["status"], "SUCCEEDED", status)
        events = host.task_events(self.owner, task_id=task_id, limit=200)["items"]
        types = [item["type"] for item in events]
        # The step was blocked (no candidate was grounded for a control that does
        # not exist) rather than failing after its attempts.
        self.assertIn("subgoal_blocked", types)
        self.assertIn("actor_recovery", types)
        recovery = next(item for item in events if item["type"] == "actor_recovery")
        self.assertEqual(recovery["payload"]["after"], "step_01")
        self.assertEqual(recovery["payload"]["plan_version"], 2)
        self.assertEqual(recovery["payload"]["actor"], "scripted")
        # The recovery step ran through the ordinary guarded path.
        dispatch = next(item for item in events if item["type"] == "dispatch")
        self.assertEqual(dispatch["payload"]["candidate_id"][:5], "cand_")
        self.assertEqual(host.store.plans(task_id)[-1]["version"], 2)
        self.assertEqual(actor.requests[0].task_id, task_id)

    def test_the_actor_sees_a_redacted_request_without_device_access(self):
        actor = ScriptedActor([ControlProposal(control="stop", reason_code="done")])
        host = self.host(actor)
        task_id, _ = self.run_task(host, task_payload(
            "actor-request", steps="tap:不存在的按钮",
            criteria=[{"id": "editor", "type": "element_present",
                       "target_key": "热搜榜"}]))
        self.assertTrue(actor.requests, "the actor was never asked")
        request = actor.requests[0]
        self.assertEqual(request.task_id, task_id)
        self.assertFalse(hasattr(request, "facade"))
        self.assertFalse(hasattr(request.observation, "catalog"))
        self.assertTrue(request.observation.observation_id)
        self.assertTrue(request.observation.controller_epoch >= 0)
        self.assertIn("dispatches", request.budget.model_dump())
        self.assertEqual(request.allowed_actions, ["tap", "replace_text", "back"])
        events = host.task_events(self.owner, task_id=task_id)["items"]
        control = next(item for item in events if item["type"] == "actor_control")
        self.assertEqual(control["payload"]["control"], "stop")

    def test_actor_proposals_are_bounded(self):
        """A never-satisfied actor cannot keep the task running: the cap holds."""
        # Every proposal is well-formed but points at a control that does not
        # exist, so each recovery step fails too.
        proposals = [lambda request, i=i: proposal_for(
            request.task_id, recovery_subgoal(f"actor_step_{i:02d}", text="不存在的按钮"))
            for i in range(1, 6)]
        actor = ScriptedActor(proposals)
        host = self.host(actor, max_replans=2)
        task_id, status = self.run_task(host, task_payload(
            "actor-bounded", steps="tap:不存在的按钮",
            criteria=[{"id": "editor", "type": "element_present",
                       "target_key": "热搜榜"}]))
        events = host.task_events(self.owner, task_id=task_id, limit=300)["items"]
        recoveries = [item for item in events if item["type"] == "actor_recovery"]
        self.assertLessEqual(len(recoveries), 2)
        self.assertGreaterEqual(len(recoveries), 1)
        plan_versions = host.store.plans(task_id)
        self.assertLessEqual(len(plan_versions), 3)  # initial + at most two replans
        self.assertIn(status["status"], ("FAILED", "PARTIAL"))
        # The runner stopped by itself: no recovery ran after the cap.
        self.assertEqual(recoveries[-1]["payload"]["remaining_replans"], 0)

    def test_an_invalid_proposal_is_rejected_and_never_dispatches(self):
        # 1. The contract itself refuses a coordinate-shaped intent.
        with self.assertRaises(Exception):
            Intent(intent_id="intent_" + "a" * 24, task_id="t", subgoal_id="s",
                   action_kind="tap", text="120,340")
        # 2. Even an actor that bypasses the schema cannot reach the device: the
        #    coordinate is not a semantic target, so grounding finds nothing.
        raw_intent = Intent.model_construct(
            protocol_version="2.1", intent_id="intent_" + "a" * 24, task_id="unused",
            subgoal_id="actor_step_01", action_kind="tap", text="120,340",
            resource_id=None, accessibility_id=None, description="coordinate",
            argument_refs={}, expected_predicates=[], require_clickable=True,
            constraints=[])
        # A proposal built without validation simulates an actor that bypasses
        # pydantic entirely (a buggy or hostile adapter).
        bypassing = SubgoalProposal.model_construct(
            kind="subgoal", subgoal_id="actor_step_01",
            description="tap by coordinate", intent=raw_intent,
            expected=[], depends_on=[], rationale=None)
        actor = ScriptedActor([bypassing])
        host = self.host(actor)
        task_id, _ = self.run_task(host, task_payload(
            "actor-coordinate", steps="tap:不存在的按钮",
            criteria=[{"id": "editor", "type": "element_present",
                       "target_key": "热搜榜"}]))
        events = host.task_events(self.owner, task_id=task_id, limit=200)["items"]
        self.assertNotIn("actor_recovery", [item["type"] for item in events])
        self.assertEqual([item for item in events if item["type"] == "dispatch"], [])

    def test_an_actor_failure_degrades_to_the_deterministic_outcome(self):
        actor = ScriptedActor([RuntimeError("model offline")])
        host = self.host(actor)
        task_id, status = self.run_task(host, task_payload(
            "actor-failure", steps="tap:不存在的按钮",
            criteria=[{"id": "editor", "type": "element_present",
                       "target_key": "热搜榜"}]))
        events = host.task_events(self.owner, task_id=task_id, limit=200)["items"]
        unavailable = next(item for item in events if item["type"] == "actor_unavailable")
        self.assertEqual(unavailable["payload"]["code"], "RuntimeError")
        self.assertEqual(status["status"], "FAILED")

    def test_no_actor_keeps_the_previous_behaviour(self):
        host = self.host(None)
        task_id, status = self.run_task(host, task_payload(
            "actor-absent", steps="tap:搜索",
            criteria=[{"id": "editor", "type": "element_present",
                       "target_key": "热搜榜"}]))
        self.assertEqual(status["status"], "SUCCEEDED", status)
        events = host.task_events(self.owner, task_id=task_id, limit=200)["items"]
        self.assertNotIn("actor_recovery", [item["type"] for item in events])
        self.assertNotIn("actor_control", [item["type"] for item in events])


class ActorFieldContractTests(unittest.TestCase):
    """Runtime uses `screen_state`; the actor request must see those values."""

    def test_screen_state_is_forwarded_to_actor_observation(self):
        from harmony_agent.actor import ActorObservation
        observation = {
            "observation_id": "obs1",
            "foreground_bundle": WEIBO,
            "fingerprint": "f" * 64,
            "screen_state": {"screen_on": True, "screen_locked": False},
        }
        screen = dict(observation.get("screen_state") or observation.get("screen") or {})
        model = ActorObservation(
            observation_id=observation["observation_id"],
            controller_epoch=1,
            foreground_bundle=observation.get("foreground_bundle"),
            fingerprint=observation.get("fingerprint"),
            screen=screen,
        )
        self.assertEqual(model.screen, {"screen_on": True, "screen_locked": False})

    def test_actor_from_env_defaults_off_and_rejects_unknown_providers(self):
        import os
        from harmony_agent.actor import actor_from_env
        old = os.environ.pop("HARMONY_AGENT_ACTOR", None)
        try:
            self.assertIsNone(actor_from_env())
            os.environ["HARMONY_AGENT_ACTOR"] = "off"
            self.assertIsNone(actor_from_env())
            os.environ["HARMONY_AGENT_ACTOR"] = "some-vendor-api"
            self.assertIsNone(actor_from_env())
            os.environ["HARMONY_AGENT_ACTOR"] = "deterministic"
            actor = actor_from_env()
            self.assertIsNotNone(actor)
            self.assertEqual(actor.name, "deterministic_planner")
        finally:
            if old is None:
                os.environ.pop("HARMONY_AGENT_ACTOR", None)
            else:
                os.environ["HARMONY_AGENT_ACTOR"] = old


if __name__ == "__main__":
    unittest.main()
