"""schema_version 2.0 task, candidate and decision contracts.

Mirrors the frozen review contract (review-contracts:2.1). The schema checks
structure; probability normalisation, candidate membership, digest, epoch,
authorization and success conditions are enforced by this module's semantic
validators and by the runtime guard.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "2.0"

ACTION_KINDS = (
    "tap",
    "long_press",
    "swipe",
    "input_text",
    "replace_text",
    "back",
    "home",
    "launch",
)

PREDICATE_TYPES = (
    "text_equals",
    "input_equals",
    "selected_is",
    "element_present",
    "element_absent",
    "foreground_is",
    "page_changed",
    "page_assertion",
    "all_of",
)

#: Predicate types whose truth value is decided by code, never by a model.
PROGRAMMATIC_PREDICATES = (
    "text_equals",
    "input_equals",
    "selected_is",
    "element_present",
    "element_absent",
    "foreground_is",
    "page_changed",
)

LEDGER_STATES = (
    "planned",
    "in_progress",
    "implemented",
    "verified",
    "blocked",
    "deferred",
)

ROUTES = ("execute", "reobserve", "escalate", "stop")


class AgentContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(AgentContract):
    device_ref: str = Field(min_length=1, max_length=128)
    allowed_apps: list[str] = Field(min_length=1)
    allowed_actions: list[Literal[ACTION_KINDS]] = Field(min_length=1)
    cloud_data_policy: Literal["disabled", "redacted_text", "explicit_image_scope"] = "disabled"

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.allowed_apps)) != len(self.allowed_apps):
            raise ValueError("allowed_apps must be unique")
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must be unique")
        return self


class Budget(AgentContract):
    max_dispatches: int = Field(ge=1, le=1000)
    max_seconds: int = Field(ge=1, le=86400)
    max_model_calls: int = Field(ge=0, le=2000)
    max_cost_usd: float | None = Field(default=None, ge=0)


class Predicate(AgentContract):
    id: str = Field(min_length=1, max_length=128)
    type: Literal[PREDICATE_TYPES]
    target_key: str | None = Field(default=None, min_length=1)
    value_ref: str | None = Field(default=None, min_length=1)
    # An empty comparison value is structurally allowed by the review schema but
    # is rejected here: it would match every blank node and silently "pass".
    value: str | bool | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    predicates: list["Predicate"] | None = Field(default=None, min_length=1, max_length=16)

    @model_validator(mode="after")
    def required_arguments(self):
        if self.type == "all_of":
            if not self.predicates:
                raise ValueError("all_of requires nested predicates")
            depth = 1
            stack = list(self.predicates)
            while stack:
                node = stack.pop()
                stack.extend(node.predicates or [])
                depth += 1
                if depth > 64:
                    raise ValueError("predicate nesting is too deep")
        elif self.predicates is not None:
            raise ValueError("predicates is only valid for all_of")
        if self.type == "page_assertion" and not self.description:
            raise ValueError("page_assertion requires a description")
        if self.type in ("input_equals", "selected_is", "element_present", "element_absent") and not self.target_key:
            raise ValueError(f"{self.type} requires target_key")
        if self.type in ("text_equals", "input_equals", "selected_is", "foreground_is"):
            if (self.value is None) == (self.value_ref is None):
                raise ValueError(f"{self.type} requires exactly one of value or value_ref")
        return self


class TaskSubmit(AgentContract):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    mode: Literal["delegated", "auto"]
    goal: str = Field(min_length=1, max_length=8000)
    scope: Scope
    success_criteria: list[Predicate] = Field(min_length=1, max_length=32)
    arguments: dict[str, str] = Field(default_factory=dict)
    budget: Budget
    model_profile: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def criteria(self):
        ids = [item.id for item in self.success_criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("success_criteria ids must be unique")
        for key in _value_refs(self.success_criteria):
            if key not in self.arguments:
                raise ValueError(f"value_ref {key!r} is not present in arguments")
        for key, value in self.arguments.items():
            if not isinstance(value, str):
                raise ValueError("arguments values must be strings")
        return self


def _value_refs(predicates: list[Predicate]) -> set[str]:
    found: set[str] = set()
    stack = list(predicates)
    while stack:
        node = stack.pop()
        if node.value_ref:
            found.add(node.value_ref)
        stack.extend(node.predicates or [])
    return found


class RemainingBudget(AgentContract):
    dispatches: int = Field(ge=0)
    seconds: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class Candidate(AgentContract):
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16,64}$", max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    target_ref: str | None
    action_kind: Literal[ACTION_KINDS]
    argument_refs: dict[str, str] = Field(default_factory=dict)
    expected_predicates: list[Predicate] = Field(min_length=1, max_length=16)
    evidence_refs: list[str] = Field(min_length=1)
    risk_class: Literal["low", "high"]
    expires_at: str

    @model_validator(mode="after")
    def placeholder_ids_are_not_allowed(self):
        # A model may never invent a handle that the service did not register.
        if self.target_ref is not None and not self.target_ref.startswith("gt_"):
            raise ValueError("target_ref must be a service-registered grounded target")
        try:
            value = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as error:  # pragma: no cover - defensive
            raise ValueError("expires_at must be an ISO-8601 timestamp") from error
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone aware")
        return self


class Fact(AgentContract):
    key: str = Field(min_length=1)
    value: str | float | bool | None
    evidence_ref: str = Field(min_length=1, max_length=128)


class DecisionContext(AgentContract):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    task_id: str = Field(min_length=1, max_length=128)
    subgoal_id: str = Field(min_length=1, max_length=128)
    scope_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    observation_id: str = Field(min_length=1, max_length=128)
    candidate_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal: str = Field(min_length=1)
    facts: list[Fact] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list, max_length=32)
    recent_outcomes: list[str] = Field(default_factory=list, max_length=8)
    budget_remaining: RemainingBudget

    @model_validator(mode="after")
    def candidates_are_bound(self):
        for candidate in self.candidates:
            if candidate.observation_id != self.observation_id:
                raise ValueError("candidate observation does not match the context observation")
            if candidate.controller_epoch != self.controller_epoch:
                raise ValueError("candidate epoch does not match the context epoch")
        if self.candidates and candidate_set_hash(self.candidates) != self.candidate_set_hash:
            raise ValueError("candidate_set_hash does not match the supplied candidates")
        return self


class DeciderChoice(AgentContract):
    type: Literal["choice"]
    choice: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1)
    certainty: float = Field(ge=0, le=1)
    probabilities: dict[str, float] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def finite_and_normalised(self):
        for key, value in self.probabilities.items():
            if not key or len(key) > 128:
                raise ValueError("invalid candidate key")
            if not isinstance(value, float) or math.isnan(value) or math.isinf(value):
                raise ValueError("non-finite probability")
            if not 0.0 <= value <= 1.0:
                raise ValueError("probability out of range")
        total = sum(self.probabilities.values())
        if abs(total - 1.0) > 0.02:
            raise ValueError("choice probabilities are not normalised")
        if self.choice not in self.probabilities:
            raise ValueError("choice is not one of the scored candidates")
        return self


class DeciderNoul(AgentContract):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class DeciderNative(AgentContract):
    score_semantics: Literal["decider_systemone"]
    answers: dict[str, DeciderChoice | DeciderNoul] = Field(min_length=1, max_length=4)


class RulesNative(AgentContract):
    score_semantics: Literal["deterministic_rule"]
    rule_id: str = Field(min_length=1, max_length=128)


class JevNative(AgentContract):
    score_semantics: Literal["jev_native"]
    choice_distribution: dict[str, float]
    provider_confidence: float | None = Field(default=None, ge=0, le=1)
    noul_scores: dict[str, float]


class OpenJevNative(AgentContract):
    score_semantics: Literal["openjev_nli_per_candidate"]
    candidates: dict[str, dict[str, float]]


NativeScores = JevNative | OpenJevNative | RulesNative | DeciderNative


class DecisionResult(AgentContract):
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    task_id: str = Field(min_length=1, max_length=128)
    subgoal_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    candidate_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["rules", "typesafe", "openjev", "decider"]
    model_revision: str = Field(min_length=1, max_length=128)
    calibration_version: str | None = None
    elapsed_ms: float = Field(ge=0)
    route: Literal[ROUTES]
    selected_candidate_id: str | None = None
    native_scores: NativeScores
    reason_code: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def route_consistency(self):
        if self.route == "execute":
            if not self.selected_candidate_id:
                raise ValueError("execute requires a selected candidate")
            if self.provider != "rules" and not self.calibration_version:
                raise ValueError("an uncalibrated model result may not route to execute")
        elif self.selected_candidate_id is not None:
            raise ValueError("only execute may name a selected candidate")
        expected = {"rules": RulesNative, "typesafe": JevNative,
                    "openjev": OpenJevNative, "decider": DeciderNative}[self.provider]
        if not isinstance(self.native_scores, expected):
            raise ValueError(f"native_scores do not match provider {self.provider}")
        return self


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def candidate_set_hash(candidates: list[Candidate]) -> str:
    """Stable digest over the registry-issued candidate set.

    Parameter references and evidence are part of the digest so that a mutated
    candidate set invalidates a late model response.
    """
    payload = sorted(
        (
            item.candidate_id,
            item.observation_id,
            item.controller_epoch,
            item.target_ref,
            item.action_kind,
            canonical(item.argument_refs),
            canonical([p.model_dump() for p in item.expected_predicates]),
            canonical(sorted(item.evidence_refs)),
            item.risk_class,
            item.expires_at,
        )
        for item in candidates
    )
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


Predicate.model_rebuild()
