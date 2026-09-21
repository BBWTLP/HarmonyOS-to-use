"""Versioned mobile experience objects and the commit gate (P3-03).

An experience is a *claim about a page*: which app/build it applies to, which
facts were true, how the target was identified, what was tried, what the verifier
found and which evidence supports that. It carries metadata and evidence
references only — never raw trees, screenshots, input values or tokens.

Two rules are enforced here rather than by convention:

* an experience whose verifier was inconclusive, whose execution was unknown, or
  that had an infrastructure error can be *staged* but never *committed*;
* committing requires an explicit approval reference, so staging alone can never
  promote a claim into shared memory.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import (AgentContract, canonical, ACTION_KINDS, INTENT_ARGUMENT_KEYS,
                        TARGET_ACTION_KINDS)

EXPERIENCE_SCHEMA_VERSION = "1.0"

OUTCOMES = ("verified", "failed", "inconclusive")

#: Risk classes in the plan's taxonomy. R0 read-only … R3 irreversible.
RISK_CLASSES = ("R0", "R1", "R2", "R3")

#: Execution statuses that must never become a committed experience.
BLOCKING_EXECUTION = ("unknown", "infrastructure_error", "not_dispatched")

#: Field names that would embed private payloads instead of referencing them.
FORBIDDEN_KEYS = ("raw_tree", "tree", "screenshot", "image", "png", "jpeg", "base64",
                  "token", "password", "cookie", "authorization", "serial",
                  "device_serial", "input_text", "typed_text", "ocr_text")

_EXPERIENCE_ID = r"^exp_[0-9a-f]{16,64}$"
_APPROVAL_REF = r"^approval_[0-9a-f]{16,64}$"
_SHA256 = r"^[0-9a-f]{64}$"

_COORDINATE_PAIR = re.compile(r"^\s*-?\d{1,6}(\.\d+)?\s*[,; ]\s*-?\d{1,6}(\.\d+)?\s*$")


class ExperienceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _scan(value: Any, path: str = "") -> list[str]:
    """Find private payloads by key name anywhere in a nested structure."""
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_KEYS:
                findings.append(here)
            findings.extend(_scan(item, here))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(_scan(item, f"{path}[{index}]"))
    elif isinstance(value, str) and _COORDINATE_PAIR.match(value):
        findings.append(f"{path}=coordinate")
    return findings


class ExperienceScope(AgentContract):
    app: str = Field(min_length=1, max_length=256)
    build: str = Field(min_length=1, max_length=128)
    device_capability: str = Field(default="", max_length=128)


class ExperienceTrigger(AgentContract):
    foreground: str = Field(min_length=1, max_length=256)
    facts: list[str] = Field(default_factory=list, max_length=16)
    goal_pattern: str = Field(min_length=1, max_length=256)


class ExperienceGrounding(AgentContract):
    layers: list[str] = Field(min_length=1, max_length=8)
    identity: str = Field(min_length=1, max_length=256)


class ProcedureStep(AgentContract):
    """One typed step. Keys are limited to semantic intent fields."""

    intent: dict[str, Any]
    expected: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def semantic_intent(self):
        findings = _scan({"intent": self.intent, "expected": self.expected})
        if findings:
            raise ValueError(f"procedure step embeds private payloads: {findings}")
        allowed = {"action_kind", "text", "resource_id", "accessibility_id",
                   "argument_refs"}
        extra = set(self.intent) - allowed
        if extra:
            raise ValueError(f"procedure intent fields are not allowed: {sorted(extra)}")
        kind = self.intent.get("action_kind")
        if kind not in ACTION_KINDS:
            raise ValueError("procedure intent requires a supported action_kind")
        refs = self.intent.get("argument_refs") or {}
        if not isinstance(refs, dict):
            raise ValueError("procedure intent argument_refs must be a mapping")
        for key, value in refs.items():
            if key not in INTENT_ARGUMENT_KEYS:
                raise ValueError(f"procedure argument {key!r} is not part of the protocol")
            if key == "text" and not str(value).startswith("arg."):
                raise ValueError("procedure input must reference a task argument")
        if kind in TARGET_ACTION_KINDS and not any(
                self.intent.get(field) for field in ("text", "resource_id", "accessibility_id")):
            raise ValueError("procedure step requires a semantic identity")
        # Expectations reference arguments, they never inline the compared value:
        # a literal here would put page content into shared memory.
        allowed_expected = {"predicate", "target_key", "value_ref", "changed", "all_of"}
        extra_expected = set(self.expected) - allowed_expected
        if extra_expected:
            raise ValueError(f"procedure expectation fields are not allowed: {sorted(extra_expected)}")
        reference = self.expected.get("value_ref")
        if reference is not None and not str(reference).startswith("arg."):
            raise ValueError("procedure expectation must reference a task argument")
        return self


class Experience(AgentContract):
    schema_version: Literal["1.0"] = EXPERIENCE_SCHEMA_VERSION
    experience_id: str = Field(pattern=_EXPERIENCE_ID)
    scope: ExperienceScope
    trigger: ExperienceTrigger
    grounding: ExperienceGrounding
    procedure: list[ProcedureStep] = Field(min_length=1, max_length=16)
    outcome: Literal[OUTCOMES]
    failure_modes: list[str] = Field(default_factory=list, max_length=16)
    risk_class: Literal[RISK_CLASSES]
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    source_task_id: str = Field(min_length=1, max_length=128)
    created_at: float = Field(ge=0)
    expires_at: float | None = Field(default=None, ge=0)
    content_hash: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def hash_matches(self):
        expected = experience_content_hash(self.model_dump(exclude={"content_hash"}))
        if expected != self.content_hash:
            raise ValueError("content_hash does not match the experience content")
        findings = _scan(self.model_dump(exclude={"content_hash"}))
        if findings:
            raise ValueError(f"experience embeds private payloads: {findings}")
        return self


def experience_content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def experience_id_for(source_task_id: str, goal_pattern: str) -> str:
    seed = f"{source_task_id}\0{goal_pattern}"
    return "exp_" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:24]


def build_experience(*, scope: ExperienceScope | dict[str, Any],
                     trigger: ExperienceTrigger | dict[str, Any],
                     grounding: ExperienceGrounding | dict[str, Any],
                     procedure: list[ProcedureStep | dict[str, Any]],
                     outcome: str, failure_modes: list[str] | None = None,
                     risk_class: str = "R0", evidence_refs: list[str] | None = None,
                     source_task_id: str, created_at: float | None = None,
                     expires_at: float | None = None,
                     ttl_days: int | None = 30,
                     experience_id: str | None = None) -> Experience:
    """Build a hashed experience. Every field is validated by the schema."""
    scope_model = ExperienceScope.model_validate(scope)
    trigger_model = ExperienceTrigger.model_validate(trigger)
    grounding_model = ExperienceGrounding.model_validate(grounding)
    steps = [ProcedureStep.model_validate(step) for step in procedure]
    created = time.time() if created_at is None else float(created_at)
    expiry = expires_at
    if expiry is None and ttl_days is not None:
        expiry = created + ttl_days * 86400
    body = {
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
        "experience_id": experience_id or experience_id_for(source_task_id,
                                                            trigger_model.goal_pattern),
        "scope": scope_model.model_dump(),
        "trigger": trigger_model.model_dump(),
        "grounding": grounding_model.model_dump(),
        "procedure": [step.model_dump() for step in steps],
        "outcome": outcome,
        "failure_modes": list(failure_modes or []),
        "risk_class": risk_class,
        "evidence_refs": list(evidence_refs or []),
        "source_task_id": source_task_id,
        "created_at": created,
        "expires_at": expiry,
    }
    body["content_hash"] = experience_content_hash(body)
    return Experience.model_validate(body)


class StagedExperience(AgentContract):
    """A claim that passed the staging checks but is not yet shared memory."""

    experience: Experience
    verifier: str = Field(min_length=1, max_length=128)
    verifier_revision: str = Field(min_length=1, max_length=128)
    staged_at: float = Field(ge=0)

    def to_dict(self) -> dict[str, Any]:
        return {"experience": self.experience.model_dump(),
                "verifier": self.verifier,
                "verifier_revision": self.verifier_revision,
                "staged_at": self.staged_at}


class ExperienceGate:
    """Staging and commit gate for learning experiences.

    The gate is the only path from "we tried something" to "shared memory". It
    refuses inconclusive verdicts, unknown executions, infrastructure errors and
    evidence-free claims, and requires an explicit approval reference to commit.
    """

    def __init__(self, *, require_evidence: bool = True):
        self.require_evidence = require_evidence
        self.staged: list[StagedExperience] = []

    def stage(self, experience: Experience, *,
              verdict: str,
              verifier: str = "code",
              verifier_revision: str = "checker:1",
              execution_status: str = "executed") -> StagedExperience:
        if execution_status in BLOCKING_EXECUTION:
            raise ExperienceError(f"execution_{execution_status}",
                                  f"execution status {execution_status!r} may not become an experience")
        if verdict == "inconclusive":
            raise ExperienceError("verifier_inconclusive",
                                  "an inconclusive verification may not be committed as experience")
        if verdict not in ("pass", "fail"):
            raise ExperienceError("unknown_verdict", f"unsupported verdict {verdict!r}")
        if verdict == "pass" and experience.outcome != "verified":
            raise ExperienceError("outcome_mismatch",
                                  "a passing verdict requires the verified outcome")
        if verdict == "fail" and experience.outcome not in ("failed", "inconclusive"):
            raise ExperienceError("outcome_mismatch",
                                  "a failing verdict requires the failed outcome")
        if self.require_evidence and not experience.evidence_refs:
            raise ExperienceError("evidence_missing",
                                  "an experience without evidence references cannot be staged")
        if experience.risk_class == "R3":
            raise ExperienceError("risk_class_not_learnable",
                                  "R3 (irreversible) actions are never learned as experience")
        staged = StagedExperience(experience=experience, verifier=verifier,
                                  verifier_revision=verifier_revision,
                                  staged_at=time.time())
        self.staged.append(staged)
        return staged

    def commit(self, staged: StagedExperience, *, approval_ref: str) -> Experience:
        """Promote a staged experience. Requires an explicit approval reference."""
        if not re.match(_APPROVAL_REF, approval_ref or ""):
            raise ExperienceError("approval_required",
                                  "committing experience requires a policy or human approval reference")
        if staged not in self.staged:
            raise ExperienceError("not_staged", "experience was not staged by this gate")
        return staged.experience
