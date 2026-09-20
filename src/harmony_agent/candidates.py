"""Server-side candidate registry.

A candidate_id is issued here and never by a model. Every candidate stays bound
to one observation, one controller epoch, one parameter set and one expiry, so a
late or replayed model answer cannot address a page it never saw.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contracts import Candidate, Predicate, candidate_set_hash, iso, utc_now
from .grounding import (DEFAULT_TTL_SECONDS, GroundedTarget, GroundingIntent,
                        GroundingResult, filter_for_intent, ground)
from harmony_runtime.risk import risk_class_for as _shared_risk_class

#: Non-action routing exits. They are control options, never phone actions.
NONE_APPLICABLE = "cand_none_applicable"
REOBSERVE = "cand_reobserve"
ESCALATE = "cand_escalate"
STOP = "cand_stop"

CONTROL_OPTIONS = (NONE_APPLICABLE, REOBSERVE, ESCALATE, STOP)

CONTROL_ROUTES = {
    NONE_APPLICABLE: "reobserve",
    REOBSERVE: "reobserve",
    ESCALATE: "escalate",
    STOP: "stop",
}

class CandidateError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def risk_class_for(label: str) -> str:
    """Routing hint only. The Runtime Guard remains the dispatch authority.

    This reads the shared taxonomy in `harmony_runtime.risk`, so the agent can
    never classify a target as safer than the runtime does.
    """
    return _shared_risk_class(label)


@dataclass
class RegisteredCandidate:
    candidate: Candidate
    target: GroundedTarget
    argument_values: dict[str, str]
    description: str


@dataclass
class CandidateSet:
    observation_id: str
    controller_epoch: int
    task_id: str
    subgoal_id: str
    scope_id: str
    candidates: list[RegisteredCandidate] = field(default_factory=list)
    control_options: tuple[str, ...] = CONTROL_OPTIONS
    grounding: GroundingResult | None = None

    @property
    def hash(self) -> str:
        return candidate_set_hash([item.candidate for item in self.candidates])

    def by_id(self, candidate_id: str) -> RegisteredCandidate | None:
        return next((item for item in self.candidates if item.candidate.candidate_id == candidate_id), None)

    def choice_criteria(self, max_criteria: int = 16) -> dict[str, str]:
        """Decider `choice` criteria: 2..16 options including control exits."""
        criteria = {item.candidate.candidate_id: item.description for item in self.candidates}
        if len(criteria) >= max_criteria:
            criteria = dict(list(criteria.items())[: max_criteria - 1])
        criteria[NONE_APPLICABLE] = "以上候选都不符合当前目标"
        return criteria

    def options(self) -> list[str]:
        return list(self.choice_criteria())

    def control_route(self, candidate_id: str) -> str | None:
        return CONTROL_ROUTES.get(candidate_id)


class CandidateRegistry:
    """Registers grounded targets as immutable, epoch-bound candidates."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._issued: dict[str, RegisteredCandidate] = {}

    def register(self, *, task_id: str, subgoal_id: str, scope_id: str,
                 observation: dict[str, Any], controller_epoch: int,
                 target: GroundedTarget, action_kind: str,
                 argument_refs: dict[str, str], arguments: dict[str, str],
                 expected_predicates: list[Predicate],
                 evidence_refs: list[str] | None = None,
                 description: str | None = None) -> RegisteredCandidate:
        if target.observation_id != observation.get("observation_id"):
            raise CandidateError("stale_observation", "Target does not belong to this observation")
        if target.controller_epoch != controller_epoch:
            raise CandidateError("epoch_mismatch", "Target was grounded under a different epoch")
        values = _resolve_arguments(argument_refs, arguments)
        candidate = Candidate(
            candidate_id="cand_" + uuid.uuid4().hex[:24],
            observation_id=observation["observation_id"],
            controller_epoch=controller_epoch,
            target_ref=target.target_ref,
            action_kind=action_kind,
            argument_refs=dict(argument_refs),
            expected_predicates=expected_predicates,
            evidence_refs=evidence_refs or [f"obs:{observation['observation_id']}", f"src:{target.source}"],
            risk_class=risk_class_for(f"{target.text} {description or ''}"),
            expires_at=target.expires_at,
        )
        item = RegisteredCandidate(candidate=candidate, target=target,
                                   argument_values=values,
                                   description=description or _describe(target))
        self._issued[candidate.candidate_id] = item
        return item

    def build(self, *, task_id: str, subgoal_id: str, scope_id: str,
              observation: dict[str, Any], controller_epoch: int,
              intent: GroundingIntent, action_kind: str, arguments: dict[str, str],
              expected_predicates: list[Predicate],
              argument_refs: dict[str, str] | None = None,
              ocr=None, matcher=None, max_candidates: int = 16) -> CandidateSet:
        """Ground one intent and register every candidate the layers returned."""
        grounding = ground(observation, intent, ocr=ocr, matcher=matcher,
                           ttl_seconds=self.ttl_seconds)
        usable = filter_for_intent(grounding.targets, intent)
        grounded_out = [t for t in grounding.targets if t not in usable]
        candidate_set = CandidateSet(
            observation_id=observation["observation_id"],
            controller_epoch=controller_epoch,
            task_id=task_id,
            subgoal_id=subgoal_id,
            scope_id=scope_id,
            grounding=grounding,
        )
        for target in usable[:max_candidates]:
            candidate_set.candidates.append(self.register(
                task_id=task_id, subgoal_id=subgoal_id, scope_id=scope_id,
                observation=observation, controller_epoch=controller_epoch,
                target=target, action_kind=action_kind,
                argument_refs=dict(argument_refs or {}), arguments=arguments,
                expected_predicates=expected_predicates,
            ))
        if grounded_out:
            grounding.rejected.extend(
                {"target_ref": t.target_ref, "reason": "filtered_by_capability_or_precondition"}
                for t in grounded_out)
        return candidate_set

    def resolve(self, candidate_id: str, *, observation_id: str,
                controller_epoch: int, now: datetime | None = None) -> RegisteredCandidate:
        item = self._issued.get(candidate_id)
        if item is None:
            raise CandidateError("unknown_candidate", "Candidate was not issued by this registry")
        if item.candidate.observation_id != observation_id:
            raise CandidateError("stale_observation", "Candidate belongs to another observation")
        if item.candidate.controller_epoch != controller_epoch:
            raise CandidateError("epoch_mismatch", "Candidate belongs to a superseded epoch")
        moment = now or utc_now()
        expiry = datetime.fromisoformat(item.candidate.expires_at.replace("Z", "+00:00"))
        if moment.astimezone(timezone.utc) >= expiry:
            raise CandidateError("candidate_expired", "Candidate has expired; observe again")
        return item

    def prune(self, now: datetime | None = None) -> int:
        moment = (now or utc_now()).astimezone(timezone.utc)
        stale = [key for key, item in self._issued.items()
                 if datetime.fromisoformat(item.candidate.expires_at.replace("Z", "+00:00")) <= moment]
        for key in stale:
            del self._issued[key]
        return len(stale)


def _resolve_arguments(argument_refs: dict[str, str], arguments: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, ref in argument_refs.items():
        # `arg.query` and the bare `query` both mean "the service resolves this
        # from the task arguments"; a literal can never come from a model.
        key = ref[4:] if ref.startswith("arg.") else ref
        if not key or key.startswith("arg."):
            raise CandidateError("invalid_argument_ref", f"{name} must reference a task argument")
        if key not in arguments:
            raise CandidateError("unknown_argument", f"argument {key!r} is not part of the task")
        values[name] = arguments[key]
    return values


def _describe(target: GroundedTarget) -> str:
    bits = []
    if target.text:
        bits.append(f"文本“{target.text}”")
    if target.identity.get("resource_id"):
        bits.append(f"id={target.identity['resource_id']}")
    if target.identity.get("accessibility_id"):
        bits.append(f"a11y={target.identity['accessibility_id']}")
    if target.role:
        bits.append(f"类型={target.role}")
    return " ".join(bits) or f"{target.source} 区域"


def observation_digest(observation: dict[str, Any]) -> str:
    return hashlib.sha256(str(observation.get("fingerprint", "")).encode()).hexdigest()
