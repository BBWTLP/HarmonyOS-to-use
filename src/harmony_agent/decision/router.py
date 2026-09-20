"""Decision router: rules baseline, Decider shadow, calibration-gated execution."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..candidates import CONTROL_ROUTES, CandidateSet
from ..contracts import DecisionResult
from ..state_builder import build_questions, build_state, predicate_propositions
from .providers.base import ProviderUnavailable
from .providers.decider import DeciderProvider
from .providers.rules import RulesProvider

#: model_profile values understood by the router.
PROFILES = ("rules_only", "local_off", "local_shadow", "local_canary")


@dataclass
class RouterOutcome:
    decision: DecisionResult
    shadow: DecisionResult | None = None
    fallback_reason: str | None = None
    provider_available: bool = True
    notes: list[str] = field(default_factory=list)


class Router:
    """Rules decide; the fast provider may only advise unless calibrated."""

    def __init__(self, *, rules: RulesProvider | None = None,
                 decider: DeciderProvider | None = None,
                 profile: str = "local_shadow",
                 calibration_version: str | None = None,
                 confidence_threshold: float = 0.0,
                 certainty_threshold: float = 0.0,
                 noul_threshold: float = 0.5):
        if profile not in PROFILES:
            raise ValueError(f"unknown model profile {profile!r}")
        self.rules = rules or RulesProvider()
        self.decider = decider
        self.profile = profile
        self.calibration_version = calibration_version
        self.confidence_threshold = confidence_threshold
        self.certainty_threshold = certainty_threshold
        self.noul_threshold = noul_threshold

    def fast_path_enabled(self) -> bool:
        return self.profile in ("local_shadow", "local_canary") and self.decider is not None

    def may_execute(self) -> bool:
        """Only a calibrated canary profile may route a model answer to execute."""
        return (self.profile == "local_canary"
                and self.decider is not None
                and bool(self.calibration_version))

    async def decide(self, *, task_id: str, subgoal_id: str, scope_id: str,
                     observation: dict[str, Any], candidate_set: CandidateSet,
                     controller_epoch: int, remaining_model_calls: int | None = None,
                     goal: str = "", facts: list[str] | None = None,
                     recent: list[str] | None = None,
                     propositions: dict[str, str] | None = None) -> RouterOutcome:
        started = time.perf_counter()
        base = await self._rules_decision(task_id, subgoal_id, observation, candidate_set,
                                          controller_epoch, started)
        if self.profile in ("rules_only", "local_off") or self.decider is None:
            base.reason_code = f"profile_{self.profile}"
            return RouterOutcome(decision=base)

        state = build_state(observation, candidate_set, goal=goal,
                            facts=facts or [], recent=recent or [])
        propositions = propositions if propositions is not None else predicate_propositions(
            [item.candidate.expected_predicates[0] for item in candidate_set.candidates
             if item.candidate.expected_predicates])
        questions = build_questions(candidate_set, propositions)
        if not questions:
            base.reason_code = "no_choice_question"
            return RouterOutcome(decision=base, provider_available=False,
                                 notes=["candidate set too small for a choice question"])

        context = {"state": state, "controller_epoch": controller_epoch,
                   "observation_id": observation.get("observation_id"),
                   "candidate_set_hash": candidate_set.hash,
                   "profile": self.profile}
        epoch = controller_epoch
        try:
            result = await self.decider.evaluate(context, questions,
                                                 remaining_model_calls=remaining_model_calls)
        except ProviderUnavailable as error:
            base.reason_code = f"fallback_{error.code}"
            return RouterOutcome(decision=base, fallback_reason=error.code,
                                 provider_available=False)

        # The epoch is re-read by the caller immediately before dispatch; a
        # suggestion that arrives after a control transition is discarded there.
        shadow = self._decider_decision(task_id, subgoal_id, observation, candidate_set,
                                        epoch, result, allow_execute=self.may_execute())
        if self.may_execute() and shadow.route == "execute":
            return RouterOutcome(decision=shadow, shadow=None, provider_available=True)
        shadow.route = "escalate"
        shadow.selected_candidate_id = None
        shadow.reason_code = "shadow_only" if self.profile == "local_shadow" else shadow.reason_code
        return RouterOutcome(decision=base, shadow=shadow, provider_available=True,
                             notes=["fast provider suggestion recorded without dispatch"])

    async def _rules_decision(self, task_id, subgoal_id, observation, candidate_set,
                              controller_epoch, started) -> DecisionResult:
        questions = build_questions(candidate_set)
        result = await self.rules.evaluate({"candidates": [
            {"candidate_id": item.candidate.candidate_id,
             "identity": item.target.identity,
             "action_kind": item.candidate.action_kind}
            for item in candidate_set.candidates]}, questions)
        answer = result.answer["action"]
        choice = answer["choice"]
        route = "execute" if candidate_set.by_id(choice) else CONTROL_ROUTES.get(choice, "escalate")
        decision = DecisionResult(
            task_id=task_id, subgoal_id=subgoal_id,
            observation_id=observation["observation_id"],
            controller_epoch=controller_epoch,
            candidate_set_hash=candidate_set.hash,
            provider="rules",
            model_revision=result.model_revision,
            calibration_version=None,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            route=route,
            selected_candidate_id=choice if route == "execute" else None,
            native_scores=result.native_scores,
            reason_code=result.native_scores["rule_id"],
        )
        return decision

    def _decider_decision(self, task_id, subgoal_id, observation, candidate_set,
                          controller_epoch, result, *, allow_execute: bool) -> DecisionResult:
        answer = result.answer.get("action") or {}
        choice = answer.get("choice")
        registered = candidate_set.by_id(choice) if isinstance(choice, str) else None
        confidence = float(answer.get("confidence", 0.0))
        certainty = float(answer.get("certainty", 0.0))
        route = "reobserve"
        reason = "decider_no_candidate"
        if registered is not None:
            if not allow_execute:
                route, reason = "escalate", "shadow_only"
            elif confidence < self.confidence_threshold:
                route, reason = "reobserve", "confidence_below_threshold"
            elif certainty < self.certainty_threshold:
                route, reason = "reobserve", "certainty_below_threshold"
            elif registered.candidate.risk_class != "low":
                route, reason = "escalate", "risk_class_not_low"
            elif registered.target.expires_at <= observation.get("expires_at", ""):
                route, reason = "reobserve", "candidate_expired"
            else:
                route, reason = "execute", "calibrated_canary"
        elif isinstance(choice, str) and choice in CONTROL_ROUTES:
            route, reason = CONTROL_ROUTES[choice], f"control_{choice}"
        noul = {key: value for key, value in result.answer.items() if value.get("type") == "noul"}
        return DecisionResult(
            task_id=task_id, subgoal_id=subgoal_id,
            observation_id=observation["observation_id"],
            controller_epoch=controller_epoch,
            candidate_set_hash=candidate_set.hash,
            provider="decider",
            model_revision=result.model_revision,
            calibration_version=self.calibration_version,
            elapsed_ms=result.elapsed_ms,
            route=route,
            selected_candidate_id=choice if route == "execute" else None,
            native_scores={"score_semantics": "decider_systemone",
                           "answers": {**result.native_scores["answers"], **noul}},
            reason_code=reason,
        )


def evaluate_propositions(noul: dict[str, float], threshold: float = 0.5) -> dict[str, str]:
    """Map noul scores to true/false/unknown; no native unknown class exists."""
    verdicts: dict[str, str] = {}
    for key, score in noul.items():
        if score > threshold:
            verdicts[key] = "true"
        elif score < 1.0 - threshold:
            verdicts[key] = "false"
        else:
            verdicts[key] = "unknown"
    return verdicts
