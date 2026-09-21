"""schema_version 2.0 task, candidate and decision contracts.

Mirrors the frozen review contract (review-contracts:2.1). The schema checks
structure; probability normalisation, candidate membership, digest, epoch,
authorization and success conditions are enforced by this module's semantic
validators and by the runtime guard.

Protocol 2.1 adds the frozen ``Intent -> CandidateSetRef -> DecisionSuggestion
-> GuardedAction`` chain plus the event envelope used by the task event log.
Those models carry the plan's hard constraints inside the schema itself:

* an intent names a *semantic* identity (text / resource id / accessibility id)
  or a task-argument reference; it can never carry a coordinate or a raw input
  value that the caller did not already place in the task parameter store;
* a suggestion may only name a candidate id that the registry offered for the
  exact observation, epoch and candidate-set hash;
* a guarded action is the only payload the runner may hand to the runtime, and a
  high-risk action requires a runtime-issued authorization reference — a model's
  own ``approved=true`` is not part of this schema at all.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "2.0"

#: Version of the frozen execution protocol (P2-01/P2-02).
PROTOCOL_VERSION = "2.1"

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

#: Action kinds that must name a semantic target before dispatch.
TARGET_ACTION_KINDS = ("tap", "long_press", "input_text", "replace_text")

#: Keys an intent may resolve from the task parameter store. Anything else is a
#: raw payload the model would have had to invent.
INTENT_ARGUMENT_KEYS = ("text", "direction", "bundle")

#: Keys a guarded action may carry into the runtime, already resolved.
GUARDED_ARGUMENT_KEYS = ("text", "direction", "bundle")

SWIPE_DIRECTIONS = ("up", "down", "left", "right")

_BUNDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_COORDINATE_PAIR = re.compile(r"^\s*-?\d{1,6}(\.\d+)?\s*[,; ]\s*-?\d{1,6}(\.\d+)?\s*$")
_CANDIDATE_ID = r"^cand_[0-9a-f]{16,64}$"
_TARGET_REF = r"^gt_[0-9a-f]{16,64}$"
_AUTHORIZATION_REF = r"^authz_[0-9a-f]{16,64}$"
_SHA256 = r"^[0-9a-f]{64}$"


class ProtocolError(RuntimeError):
    """A frozen-protocol violation. Carries a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _reject_coordinates(field: str, value: str) -> None:
    """Reject a bare coordinate pair smuggled into a semantic field."""
    if _COORDINATE_PAIR.match(value):
        raise ValueError(f"{field} must be semantic; coordinates are not part of the protocol")


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:  # pragma: no cover - defensive
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone aware")
    return parsed


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


class Intent(AgentContract):
    """Frozen grounding intent (P2-01): the first link of the execution chain.

    Built by the planner or an ``ActorProvider``, never by the runtime. It names
    *what* the caller wants to reach, not where it is on screen.
    """

    protocol_version: Literal["2.1"] = PROTOCOL_VERSION
    intent_id: str = Field(pattern=r"^intent_[0-9a-f]{16,64}$")
    task_id: str = Field(min_length=1, max_length=128)
    subgoal_id: str = Field(min_length=1, max_length=128)
    action_kind: Literal[ACTION_KINDS]
    text: str | None = Field(default=None, min_length=1, max_length=512)
    resource_id: str | None = Field(default=None, min_length=1, max_length=256)
    accessibility_id: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, min_length=1, max_length=512)
    argument_refs: dict[str, str] = Field(default_factory=dict)
    expected_predicates: list[Predicate] = Field(default_factory=list, max_length=16)
    require_clickable: bool = False
    constraints: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def semantic_only(self):
        for key, value in self.argument_refs.items():
            if key not in INTENT_ARGUMENT_KEYS:
                raise ValueError(f"intent argument {key!r} is not part of the protocol")
            if not isinstance(value, str) or not value:
                raise ValueError("intent argument references must be non-empty strings")
            _reject_coordinates(f"argument_refs[{key}]", value)
        for field in ("text", "resource_id", "accessibility_id", "description"):
            value = getattr(self, field)
            if value is not None:
                _reject_coordinates(field, value)
        if self.action_kind in ("input_text", "replace_text"):
            # Input content must come from the task parameter store: an inline
            # value here would be a model-supplied payload.
            reference = self.argument_refs.get("text")
            if reference is None or not reference.startswith("arg."):
                raise ValueError("input intent must reference a task argument, not a literal text")
        if "direction" in self.argument_refs:
            if self.argument_refs["direction"] not in SWIPE_DIRECTIONS:
                raise ValueError("swipe direction must be up, down, left or right")
        if "bundle" in self.argument_refs:
            if not _BUNDLE.match(self.argument_refs["bundle"]):
                raise ValueError("launch bundle must be a reverse-DNS application id")
        if self.action_kind in TARGET_ACTION_KINDS:
            if not (self.text or self.resource_id or self.accessibility_id):
                raise ValueError("a target action requires a semantic identity")
        return self


class CandidateSetRef(AgentContract):
    """Serializable reference to a registry-issued candidate set (P2-01).

    Carries digests and identifiers only. Grounded targets, fingerprints and
    coordinates stay inside the runtime-side registry; this is what may travel
    to a provider, an event log or an evidence bundle.
    """

    protocol_version: Literal["2.1"] = PROTOCOL_VERSION
    observation_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    candidate_set_hash: str = Field(pattern=_SHA256)
    candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    risk_classes: dict[str, Literal["low", "high"]] = Field(default_factory=dict)
    control_options: list[str] = Field(default_factory=list, max_length=8)
    grounding_layers: list[str] = Field(default_factory=list, max_length=8)
    expires_at: str

    @model_validator(mode="after")
    def consistent(self):
        for candidate_id in self.candidate_ids:
            if not re.match(_CANDIDATE_ID, candidate_id):
                raise ValueError("candidate_ids must be registry-issued candidate ids")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        if set(self.risk_classes) != set(self.candidate_ids):
            raise ValueError("risk_classes must describe exactly the candidate set")
        _aware(self.expires_at, "expires_at")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return _aware(self.expires_at, "expires_at") <= (now or utc_now())


class DecisionSuggestion(AgentContract):
    """Advisory provider output (P2-01): the third link of the chain.

    A suggestion is *advice*. It may name one offered candidate id or route away
    (``reobserve``/``escalate``/``stop``); it can never invent a candidate, carry
    a coordinate, or claim its own authorization.
    """

    protocol_version: Literal["2.1"] = PROTOCOL_VERSION
    task_id: str = Field(min_length=1, max_length=128)
    subgoal_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    candidate_set_hash: str = Field(pattern=_SHA256)
    candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    provider: Literal["rules", "typesafe", "openjev", "decider"]
    model_revision: str = Field(min_length=1, max_length=128)
    calibration_version: str | None = Field(default=None, max_length=128)
    route: Literal[ROUTES]
    selected_candidate_id: str | None = Field(default=None, pattern=_CANDIDATE_ID)
    confidence: float | None = Field(default=None, ge=0, le=1)
    certainty: float | None = Field(default=None, ge=0, le=1)
    noul: float | None = Field(default=None, ge=0, le=1)
    abstained: bool = False
    latency_ms: float = Field(ge=0)
    fallback_reason: str | None = Field(default=None, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def route_consistency(self):
        for candidate_id in self.candidate_ids:
            if not re.match(_CANDIDATE_ID, candidate_id):
                raise ValueError("candidate_ids must be registry-issued candidate ids")
        if self.route == "execute":
            if not self.selected_candidate_id:
                raise ValueError("execute requires a selected candidate")
            if self.selected_candidate_id not in self.candidate_ids:
                raise ValueError("the selected candidate was not offered for this observation")
            if self.provider != "rules" and not self.calibration_version:
                raise ValueError("an uncalibrated provider may not suggest execute")
            if self.abstained:
                raise ValueError("an abstaining provider may not suggest execute")
        elif self.selected_candidate_id is not None:
            raise ValueError("only execute may name a selected candidate")
        return self


def admit_suggestion(suggestion: DecisionSuggestion,
                     offered: CandidateSetRef) -> DecisionSuggestion:
    """Cross-check a suggestion against the set the registry actually issued.

    The schema already rejects a suggestion that names a candidate it did not
    list; this check rejects one that names a *foreign* set, a stale observation,
    a superseded epoch or an expired set. Any failure is a refusal, never a
    re-issued candidate.
    """
    if suggestion.observation_id != offered.observation_id:
        raise ProtocolError("stale_observation",
                            "suggestion refers to a different observation")
    if suggestion.controller_epoch != offered.controller_epoch:
        raise ProtocolError("epoch_mismatch", "suggestion was made under another epoch")
    if suggestion.candidate_set_hash != offered.candidate_set_hash:
        raise ProtocolError("candidate_set_mismatch",
                            "suggestion does not belong to the offered candidate set")
    if not set(suggestion.candidate_ids).issubset(set(offered.candidate_ids)):
        raise ProtocolError("unregistered_candidate",
                            "suggestion names a candidate the registry never offered")
    if offered.is_expired():
        raise ProtocolError("expired_candidate_set", "the candidate set has expired")
    return suggestion


class GuardedAction(AgentContract):
    """The only payload the runner may hand to the runtime guard (P2-01).

    Everything here is either registry-issued (candidate id, target ref, epoch,
    candidate-set hash) or resolved from the task parameter store. Coordinates,
    raw targets and ``approved`` flags have no field to live in.
    """

    protocol_version: Literal["2.1"] = PROTOCOL_VERSION
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(pattern=r"^act_[0-9a-f]{16,64}$")
    task_id: str = Field(min_length=1, max_length=128)
    subgoal_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    candidate_set_hash: str = Field(pattern=_SHA256)
    candidate_id: str = Field(pattern=_CANDIDATE_ID)
    target_ref: str | None = Field(default=None, pattern=_TARGET_REF)
    local_fingerprint: str | None = Field(default=None, min_length=8, max_length=128)
    action_kind: Literal[ACTION_KINDS]
    argument_values: dict[str, str] = Field(default_factory=dict)
    expected: list[Predicate] = Field(default_factory=list, max_length=16)
    #: The terminal semantic condition this step is expected to reach, used as the
    #: recovery condition if the dispatch result is lost. It is recorded evidence
    #: for a later reconciliation and never gates the dispatch itself.
    recovery: list[Predicate] = Field(default_factory=list, max_length=16)
    risk_class: Literal["low", "high"]
    authorization_ref: str | None = Field(default=None, pattern=_AUTHORIZATION_REF)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def dispatchable(self):
        for key, value in self.argument_values.items():
            if key not in GUARDED_ARGUMENT_KEYS:
                raise ValueError(f"guarded argument {key!r} is not part of the protocol")
            if not isinstance(value, str) or not value:
                raise ValueError("guarded argument values must be non-empty strings")
        if self.action_kind in TARGET_ACTION_KINDS:
            if not self.target_ref:
                raise ValueError("a target action requires a registry-issued target_ref")
            if not self.local_fingerprint:
                raise ValueError("a target action requires the grounded local fingerprint")
            if set(self.argument_values) - {"text"}:
                raise ValueError("target actions may only resolve input text")
        if self.risk_class == "high" and not self.authorization_ref:
            raise ValueError("a high-risk action requires a runtime-issued authorization")
        if self.authorization_ref and self.risk_class != "high":
            raise ValueError("authorization_ref is only meaningful for a high-risk action")
        return self


class EventEnvelope(AgentContract):
    """Provenance every task event carries (P2-02).

    ``payload`` stays the event-specific body; the envelope records which
    observation, epoch, candidate set, decision revision and calibration were in
    force when the event was written, so a later reviewer can audit an event
    without replaying the run.
    """

    protocol_version: Literal["2.1"] = PROTOCOL_VERSION
    event_type: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    created: float = Field(ge=0)
    observation_id: str | None = Field(default=None, max_length=128)
    candidate_set_hash: str | None = Field(default=None, pattern=_SHA256)
    controller_epoch: int | None = Field(default=None, ge=0)
    model_revision: str | None = Field(default=None, max_length=128)
    calibration_version: str | None = Field(default=None, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    payload: dict[str, Any] = Field(default_factory=dict)


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
