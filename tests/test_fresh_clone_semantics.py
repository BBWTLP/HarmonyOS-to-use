"""OFFLINE-1: the offline suite must be reproducible from a fresh clone.

``DeciderProvider`` reads its bearer token from a file. The production default
is the repo-relative runtime token (``services/decider/.runtime/api-token``),
which is gitignored and only exists on a machine that has started the Decider
service. A test that relies on that default is not hermetic.

These tests pin the three semantics that matter:

* Case A - no token, ``local_off``: the Direct Runtime is unaffected and no
  provider is ever constructed.
* Case B - no token, ``local_shadow``: an actual Decider call fails safely with
  ``token_unavailable``, the decision falls back to rules, nothing crashes.
* Case C - fake transport plus a temporary test token: fully reproducible.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, make_decider_provider, weibo_home
from harmony_agent.candidates import CandidateRegistry
from harmony_agent.contracts import Predicate
from harmony_agent.decision.factory import ProviderConfig, resolve_fast_provider
from harmony_agent.decision.providers.decider import (DeciderError, DeciderProvider,
                                                      DEFAULT_REVISION)
from harmony_agent.decision.router import Router
from harmony_agent.grounding import GroundingIntent
from harmony_agent.state_builder import build_questions, build_state
from harmony_runtime.observation import snapshot


def observation(tree, observation_id="obs_1", epoch=0):
    obs = snapshot(tree, (1080, 2340, 0), {"status": "ok", "bundle": WEIBO})
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = epoch
    obs["actionable"] = True
    obs["mode"] = "FULL"
    return obs


def candidate_set(registry, obs):
    return registry.build(
        task_id="task_1", subgoal_id="sub_1", scope_id="scope_1",
        observation=obs, controller_epoch=0,
        intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
        action_kind="tap", arguments={},
        expected_predicates=[Predicate(id="p", type="foreground_is", value=WEIBO)])


class FreshCloneSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.obs = observation(weibo_home())
        self.registry = CandidateRegistry()
        self.candidates = candidate_set(self.registry, self.obs)
        self.selected = self.candidates.candidates[0].candidate.candidate_id
        self.state = build_state(self.obs, self.candidates, goal="搜索鸿蒙")
        self.questions = build_questions(self.candidates)
        self.context = {"state": self.state, "controller_epoch": 0}

    # -- Case A ------------------------------------------------------------
    def test_case_a_local_off_never_builds_a_provider(self):
        """No token, rules-only profile: Direct Runtime is untouched."""
        config = ProviderConfig(profile="local_off")
        build = resolve_fast_provider("local_off", config)
        self.assertIsNone(build.provider)
        self.assertFalse(build.available())
        self.assertEqual(build.status, "rules_only")
        self.assertFalse(config.wants_fast_provider())

    def test_case_a_rules_only_decision_never_touches_a_token(self):
        outcome = asyncio.run(Router(profile="local_off").decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.candidates, controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.decision.selected_candidate_id, self.selected)

    # -- Case B ------------------------------------------------------------
    def test_case_b_missing_token_is_token_unavailable(self):
        """A real Decider call without a token fails closed, not silently."""
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "api-token"  # deliberately absent
            provider = DeciderProvider(token_file=missing)
            with self.assertRaises(DeciderError) as error:
                asyncio.run(provider.evaluate(self.context, self.questions,
                                              remaining_model_calls=4))
            self.assertEqual(error.exception.code, "token_unavailable")
            # The failure is local and does not poison provider health.
            self.assertFalse(provider.health_snapshot()["circuit_open"])

    def test_case_b_shadow_profile_falls_back_to_rules_without_dispatching(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "api-token"
            provider = DeciderProvider(token_file=missing)
            self.assertTrue(provider.capabilities().requires_calibration)
            outcome = asyncio.run(Router(fast_provider=provider,
                                         profile="local_shadow").decide(
                task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
                candidate_set=self.candidates, controller_epoch=0, goal="g"))
        # The deterministic rules decision still stands: nothing was dispatched
        # by the provider and the runtime did not crash.
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.decision.selected_candidate_id, self.selected)
        self.assertTrue(outcome.provider_available is False)

    # -- Case C ------------------------------------------------------------
    def test_case_c_fake_transport_with_a_temporary_token_is_reproducible(self):
        def transport(payload, token, timeout):
            self.assertEqual(token, "test-token")
            criteria = list(payload["questions"]["action"]["criteria"])
            choice = criteria[0]
            return 200, {
                "answers": {"action": {"type": "choice", "choice": choice,
                                       "confidence": 0.9, "certainty": 0.8,
                                       "probabilities": {name: 1.0 / len(criteria)
                                                         for name in criteria}}},
                "deployment": {"model_revision": DEFAULT_REVISION,
                               "mode": "shadow_only", "precision": "bfloat16",
                               "backend": "test"}}, {}

        provider = make_decider_provider(self, transport)
        self.assertEqual(provider.token(), "test-token")
        result = asyncio.run(provider.evaluate(self.context, self.questions))
        self.assertEqual(result.provider, "decider")
        self.assertEqual(result.model_revision, DEFAULT_REVISION)


if __name__ == "__main__":
    unittest.main()
