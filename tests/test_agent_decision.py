import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import (WEIBO, FakeTransportProvider, calibration,
                         make_decider_provider, weibo_home, weibo_results)
from harmony_agent.candidates import CandidateRegistry
from harmony_agent.contracts import Predicate
from harmony_agent.decision.providers.decider import (CircuitBreaker, DeciderProvider,
                                                      DeciderError, estimate_tokens)
from harmony_agent.decision.router import Router, evaluate_propositions
from harmony_agent.grounding import GroundingIntent
from harmony_agent.state_builder import build_questions, build_state
from harmony_runtime.observation import snapshot

REVISION = "7789eb65d5cf519737608e218fa88819bddea0af"


def observation(tree, observation_id="obs_1", epoch=0):
    obs = snapshot(tree, (1080, 2340, 0), {"status": "ok", "bundle": WEIBO})
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = epoch
    obs["actionable"] = True
    obs["mode"] = "FULL"
    return obs


def candidate_set(registry, obs, **overrides):
    kwargs = dict(task_id="task_1", subgoal_id="sub_1", scope_id="scope_1",
                  observation=obs, controller_epoch=0,
                  intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
                  action_kind="tap", arguments={},
                  expected_predicates=[Predicate(id="p", type="foreground_is", value=WEIBO)])
    kwargs.update(overrides)
    return registry.build(**kwargs)


def decider_response(choice, *, status=200, revision=REVISION, probabilities=None):
    def transport(payload, token, timeout):
        criteria = list(payload["questions"]["action"]["criteria"])
        distribution = probabilities or {name: 1.0 / len(criteria) for name in criteria}
        if choice not in distribution:
            distribution = {**distribution, choice: 0.9}
        answers = {"action": {"type": "choice", "choice": choice,
                              "confidence": max(distribution.values()),
                              "certainty": 0.7, "probabilities": distribution}}
        return status, {"answers": answers,
                        "deployment": {"model_revision": revision, "mode": "shadow_only",
                                       "precision": "bfloat16", "backend": "test"}}, {}
    return transport


class RulesProviderTests(unittest.TestCase):
    def test_single_candidate_executes_from_rules(self):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        outcome = asyncio.run(Router().decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=obs,
            candidate_set=candidate_set(registry, obs), controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.route, "execute")
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.decision.native_scores.model_dump()["rule_id"],
                         "single_candidate")
        self.assertIsNone(outcome.shadow)

    def test_no_candidate_reobserves(self):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        empty = candidate_set(registry, obs,
                              intent=GroundingIntent(action_kind="tap", text="不存在的按钮"))
        self.assertEqual(empty.candidates, [])
        outcome = asyncio.run(Router().decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=obs,
            candidate_set=empty, controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.route, "reobserve")

    def test_ambiguous_text_uses_only_an_explicit_identity_tie_break(self):
        obs = observation(weibo_results("鸿蒙"))
        registry = CandidateRegistry()
        ambiguous = candidate_set(registry, obs,
                                  intent=GroundingIntent(action_kind="tap", text="鸿蒙"))
        self.assertGreaterEqual(len(ambiguous.candidates), 1)
        outcome = asyncio.run(Router().decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=obs,
            candidate_set=ambiguous, controller_epoch=0, goal="g"))
        if len(ambiguous.candidates) > 1:
            # Two nodes carry the same text; only one carries a stable identity,
            # so the rule records that tie-break explicitly instead of guessing.
            self.assertEqual(outcome.decision.native_scores.model_dump()["rule_id"],
                             "unique_stable_identity")
            selected = ambiguous.by_id(outcome.decision.selected_candidate_id)
            self.assertTrue(selected.target.identity)
        else:
            self.assertEqual(outcome.decision.route, "execute")


class RouterShadowTests(unittest.TestCase):
    def setUp(self):
        self.obs = observation(weibo_home())
        self.registry = CandidateRegistry()
        self.candidates = candidate_set(self.registry, self.obs)
        self.selected = self.candidates.candidates[0].candidate.candidate_id

    def decide(self, provider, profile="local_shadow", calibration_record=None,
               calibration_version=None):
        router = Router(decider=provider, profile=profile,
                        calibration=calibration_record,
                        calibration_version=calibration_version)
        return asyncio.run(router.decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.candidates, controller_epoch=0, goal="搜索鸿蒙"))

    def test_shadow_records_a_suggestion_and_never_dispatches(self):
        provider = FakeTransportProvider({"action": {"type": "choice",
                                                     "choice": self.selected,
                                                     "confidence": 0.99,
                                                     "certainty": 0.99,
                                                     "probabilities": {self.selected: 0.99,
                                                                       "cand_none_applicable": 0.01}}})
        outcome = self.decide(provider)
        self.assertIsNotNone(outcome.shadow)
        self.assertEqual(outcome.shadow.route, "escalate")
        self.assertEqual(outcome.shadow.reason_code, "shadow_only")
        self.assertIsNone(outcome.shadow.selected_candidate_id)
        self.assertEqual(provider.calls, 1)

    def test_canary_without_calibration_still_cannot_execute(self):
        provider = FakeTransportProvider({"action": {"type": "choice",
                                                     "choice": self.selected,
                                                     "confidence": 0.99, "certainty": 0.99,
                                                     "probabilities": {self.selected: 0.99,
                                                                       "cand_none_applicable": 0.01}}})
        router = Router(decider=provider, profile="local_canary")
        self.assertFalse(router.may_execute())
        outcome = asyncio.run(router.decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.candidates, controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.provider, "rules")

    def test_calibrated_canary_may_execute_a_low_risk_candidate(self):
        provider = FakeTransportProvider({"action": {"type": "choice",
                                                     "choice": self.selected,
                                                     "confidence": 0.99, "certainty": 0.99,
                                                     "probabilities": {self.selected: 0.99,
                                                                       "cand_none_applicable": 0.01}}})
        outcome = self.decide(provider, profile="local_canary",
                              calibration_record=calibration(),
                              calibration_version="cal-2026-09-20")
        self.assertEqual(outcome.decision.route, "execute")
        self.assertEqual(outcome.decision.provider, "decider")
        self.assertEqual(outcome.decision.calibration_version, "cal-2026-09-20")
        self.assertEqual(outcome.decision.selected_candidate_id, self.selected)

    def test_a_bare_calibration_string_cannot_enable_the_canary(self):
        """v3.2 Gate G: a string is metadata, never proof of calibration."""
        provider = FakeTransportProvider({"action": {"type": "choice",
                                                     "choice": self.selected,
                                                     "confidence": 0.99,
                                                     "certainty": 0.99,
                                                     "probabilities": {
                                                         self.selected: 0.99,
                                                         "cand_none_applicable": 0.01}}})
        router = Router(decider=provider, profile="local_canary",
                        calibration_version="cal-anything")
        self.assertFalse(router.may_execute())
        outcome = asyncio.run(router.decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.candidates, controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertIsNotNone(outcome.shadow)
        self.assertIsNone(outcome.shadow.selected_candidate_id)

    def test_provider_failure_falls_back_to_the_baseline(self):
        class Broken:
            provider = "decider"
            model_revision = "fake"

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                from harmony_agent.decision.providers.base import ProviderUnavailable
                raise ProviderUnavailable("model_busy", "busy")

        outcome = self.decide(Broken())
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.fallback_reason, "model_busy")
        self.assertFalse(outcome.provider_available)

    def test_propositions_map_to_true_false_unknown(self):
        verdicts = evaluate_propositions({"a": 0.9, "b": 0.1, "c": 0.5})
        self.assertEqual(verdicts, {"a": "true", "b": "false", "c": "unknown"})


class DeciderProviderTests(unittest.TestCase):
    def provider(self, transport, **overrides):
        # Hermetic token: never read the machine-local Decider runtime token.
        return make_decider_provider(self, transport, **overrides)

    def evaluate(self, provider):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        candidate = candidate_set(registry, obs)
        questions = build_questions(candidate)
        state = build_state(obs, candidate, goal="搜索鸿蒙")
        return asyncio.run(provider.evaluate(
            {"state": state, "controller_epoch": 0}, questions))

    def test_unregistered_candidate_is_rejected(self):
        provider = self.provider(decider_response("cand_deadbeef"))
        with self.assertRaises(DeciderError) as context:
            self.evaluate(provider)
        self.assertEqual(context.exception.code, "candidate_not_registered")

    def test_coordinates_cannot_be_smuggled_in(self):
        provider = self.provider(decider_response("100,200"))
        with self.assertRaises(DeciderError) as context:
            self.evaluate(provider)
        self.assertEqual(context.exception.code, "candidate_not_registered")

    def test_revision_mismatch_is_rejected(self):
        provider = self.provider(decider_response("cand_x", revision="other-revision"))
        with self.assertRaises(DeciderError) as context:
            self.evaluate(provider)
        self.assertEqual(context.exception.code, "revision_mismatch")

    def test_valid_answer_preserves_native_scores(self):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        candidate = candidate_set(registry, obs)
        first = candidate.candidates[0].candidate.candidate_id
        provider = self.provider(decider_response(first))
        result = asyncio.run(provider.evaluate(
            {"state": build_state(obs, candidate, goal="搜索"), "controller_epoch": 0},
            build_questions(candidate)))
        self.assertEqual(result.provider, "decider")
        self.assertEqual(result.model_revision, REVISION)
        answer = result.native_scores["answers"]["action"]
        self.assertEqual(answer["type"], "choice")
        self.assertEqual(answer["choice"], first)
        self.assertIn("probabilities", answer)
        self.assertNotIn("coordinates", answer)

    def test_busy_response_trips_the_circuit_breaker(self):
        def busy(payload, token, timeout):
            return 503, {"detail": "busy"}, {}

        provider = self.provider(busy, breaker=CircuitBreaker(threshold=2, cooldown_seconds=60))
        for _ in range(2):
            with self.assertRaises(DeciderError) as context:
                self.evaluate(provider)
            self.assertEqual(context.exception.code, "model_busy")
        with self.assertRaises(DeciderError) as context:
            self.evaluate(provider)
        self.assertEqual(context.exception.code, "circuit_open")

    def test_timeout_response_is_not_a_success(self):
        provider = self.provider(lambda payload, token, timeout: (504, {}, {}))
        with self.assertRaises(DeciderError) as context:
            self.evaluate(provider)
        self.assertEqual(context.exception.code, "model_timeout")

    def test_non_finite_score_is_rejected(self):
        def transport(payload, token, timeout):
            criteria = list(payload["questions"]["action"]["criteria"])
            probabilities = {name: 0.5 for name in criteria}
            probabilities[criteria[0]] = float("nan")
            return 200, {"answers": {"action": {"type": "choice", "choice": criteria[0],
                                                 "confidence": 0.5, "certainty": 0.5,
                                                 "probabilities": probabilities}},
                         "deployment": {"model_revision": REVISION}}, {}

        provider = self.provider(transport)
        with self.assertRaises(DeciderError) as context:
            self.evaluate(provider)
        self.assertIn(context.exception.code, ("non_finite_score", "score_not_normalised"))

    def test_budget_exhaustion_prevents_a_model_call(self):
        provider = self.provider(decider_response("cand_x"))
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        candidate = candidate_set(registry, obs)
        with self.assertRaises(DeciderError) as context:
            asyncio.run(provider.evaluate(
                {"state": build_state(obs, candidate, goal="搜索"), "controller_epoch": 0},
                build_questions(candidate), remaining_model_calls=0))
        self.assertEqual(context.exception.code, "budget_exhausted")

    def test_oversized_state_is_rejected_rather_than_truncated(self):
        provider = self.provider(decider_response("cand_x"))
        with self.assertRaises(DeciderError) as context:
            provider.build_payload("字" * 3000, {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(context.exception.code, "state_too_long")

    def test_choice_question_bounds_are_enforced(self):
        provider = self.provider(decider_response("cand_x"))
        with self.assertRaises(DeciderError):
            provider.build_payload("state", {"q": {"type": "choice", "instructions": "x",
                                                   "criteria": {"only": "one"}}})


class StateBuilderTests(unittest.TestCase):
    def test_state_is_trimmed_to_the_token_budget(self):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        candidate = candidate_set(registry, obs)
        text = build_state(obs, candidate, goal="搜索" * 400,
                           facts=["事实" * 200 for _ in range(20)])
        self.assertLessEqual(estimate_tokens(text), 1024)
        self.assertIn("可选动作", text)

    def test_questions_are_bounded_and_include_a_control_exit(self):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        candidate = candidate_set(registry, obs)
        questions = build_questions(candidate, {"prop1": "结果页已打开"})
        self.assertLessEqual(len(questions), 4)
        self.assertIn("cand_none_applicable", questions["action"]["criteria"])
        self.assertEqual(questions["prop1"]["type"], "noul")

    def test_candidate_ids_are_opaque_keys(self):
        obs = observation(weibo_home())
        registry = CandidateRegistry()
        candidate = candidate_set(registry, obs)
        state = build_state(obs, candidate, goal="搜索")
        self.assertNotIn(candidate.candidates[0].candidate.candidate_id, state)


if __name__ == "__main__":
    unittest.main()
