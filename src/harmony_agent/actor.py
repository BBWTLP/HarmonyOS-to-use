"""ActorProvider protocol (P2-04).

The actor is the only component that proposes *what to try next*. Its output is
deliberately narrow: a typed subgoal (action kind + semantic grounding intent +
expected postcondition) or a control exit (reobserve / escalate / wait / stop).

An actor never receives a device handle, a candidate id, a coordinate or a shell.
Everything it can say is expressed in `Intent`, so a proposal that tries to smuggle
a coordinate, a literal input value or a raw action is rejected by the contract
before the runtime sees it. The deterministic planner is exposed through this same
protocol so a task can run with no model at all.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from .contracts import (ACTION_KINDS, AgentContract, Intent, Predicate, TaskSubmit,
                        TARGET_ACTION_KINDS)
from .planner import Plan, Subgoal, plan_from_task

#: Control exits an actor may take instead of proposing a subgoal.
ACTOR_CONTROLS = ("reobserve", "escalate", "wait", "stop")


class ActorObservation(AgentContract):
    """Bounded, redacted view of the current observation handed to an actor."""

    observation_id: str = Field(min_length=1, max_length=128)
    controller_epoch: int = Field(ge=0)
    foreground_bundle: str | None = Field(default=None, max_length=256)
    fingerprint: str | None = Field(default=None, max_length=128)
    screen: dict[str, Any] = Field(default_factory=dict)
    facts: list[str] = Field(default_factory=list, max_length=32)
    recent_outcomes: list[str] = Field(default_factory=list, max_length=8)
    blocking_dialog: str | None = Field(default=None, max_length=256)


class ActorBudget(AgentContract):
    dispatches: int = Field(ge=0)
    seconds: float = Field(ge=0)
    model_calls: int = Field(ge=0)


class ActorRequest(AgentContract):
    """Everything an actor may see. No device access, no raw payloads."""

    request_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=8000)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    arguments: dict[str, str] = Field(default_factory=dict)
    allowed_apps: list[str] = Field(min_length=1)
    allowed_actions: list[str] = Field(min_length=1)
    observation: ActorObservation
    plan_version: int = Field(ge=1)
    pending_subgoal_ids: list[str] = Field(default_factory=list, max_length=64)
    budget: ActorBudget
    frozen_memory_hash: str | None = Field(default=None, max_length=128)
    memory_facts: list[str] = Field(default_factory=list, max_length=32)


class SubgoalProposal(AgentContract):
    """A typed next step. `intent` carries the semantics, nothing else does."""

    kind: Literal["subgoal"] = "subgoal"
    subgoal_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    intent: Intent
    expected: list[Predicate] = Field(default_factory=list, max_length=16)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    rationale: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def identity_is_consistent(self):
        if self.intent.subgoal_id != self.subgoal_id:
            raise ValueError("intent subgoal_id does not match the proposal")
        if self.intent.action_kind in TARGET_ACTION_KINDS and not (
                self.intent.text or self.intent.resource_id
                or self.intent.accessibility_id):
            raise ValueError("a target subgoal must name a semantic identity")
        return self


class ControlProposal(AgentContract):
    """A non-dispatching exit. `wait` and `stop` never write to the device."""

    kind: Literal["control"] = "control"
    control: Literal[ACTOR_CONTROLS]
    reason_code: str = Field(min_length=1, max_length=128)
    detail: str | None = Field(default=None, max_length=512)


ActorProposal = SubgoalProposal | ControlProposal


@runtime_checkable
class ActorProvider(Protocol):
    """Structural protocol implemented by planners and model-backed actors.

    Implementations also expose ``name`` and ``revision`` strings so a task event
    can record which actor produced a proposal. Only `propose` is part of the
    runtime-checked surface.
    """

    async def propose(self, request: ActorRequest) -> ActorProposal:  # pragma: no cover
        ...


class ActorUnavailable(RuntimeError):
    """Raised when an actor cannot answer; the caller must fall back, not guess."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class DeterministicActor:
    """The no-model actor: it replays the caller's own delegated plan.

    When the plan has no pending subgoal left it exits with ``stop`` rather than
    inventing work, so a task can complete (or fail) with no model calls at all.
    """

    name = "deterministic_planner"
    revision = "planner:1"

    def __init__(self, task: TaskSubmit | None = None, plan: Plan | None = None):
        self.task = task
        self.plan = plan
        self._served: set[str] = set()
        self._by_task: dict[str, TaskSubmit] = {}
        if task is not None:
            self._by_task[task.request_id] = task

    def reset(self) -> None:
        self._served.clear()

    def bind_task(self, task: TaskSubmit, plan: Plan | None = None) -> None:
        self.task = task
        self.plan = plan or plan_from_task(task)
        self._by_task[task.request_id] = task

    async def propose(self, request: ActorRequest) -> ActorProposal:
        if self.plan is None:
            task = self._by_task.get(request.request_id) or self.task
            if task is None:
                return ControlProposal(control="escalate", reason_code="no_bound_task")
            self.plan = plan_from_task(task)
        for subgoal in self.plan.subgoals:
            if subgoal.subgoal_id in self._served:
                continue
            if subgoal.status not in ("pending", "retry"):
                continue
            self._served.add(subgoal.subgoal_id)
            return proposal_from_subgoal(request.task_id, subgoal)
        if self._served:
            return ControlProposal(control="stop", reason_code="plan_exhausted")
        return ControlProposal(control="escalate", reason_code="empty_plan")


def actor_from_env(task: TaskSubmit | None = None):
    """Optional Actor factory (T09). Default off: Direct six tools need no actor.

    ``HARMONY_AGENT_ACTOR``:
      unset / off / none  → None (Direct path unchanged)
      deterministic       → DeterministicActor
      anything else       → None (no hardcoded commercial API)
    """
    import os
    name = (os.environ.get("HARMONY_AGENT_ACTOR") or "").strip().lower()
    if name in ("", "off", "none", "0", "false"):
        return None
    if name in ("deterministic", "planner", "local_planner"):
        return DeterministicActor(task=task)
    # Unknown provider: do not invent a network client. Direct remains available.
    return None


def intent_id_for(subgoal_id: str) -> str:
    return "intent_" + uuid.uuid5(uuid.NAMESPACE_URL, subgoal_id).hex[:24]


def intent_from_subgoal(task_id: str, subgoal: Subgoal,
                        constraints: list[str] | None = None) -> Intent:
    """Project an internal subgoal onto the frozen intent contract."""
    return Intent(
        intent_id=intent_id_for(subgoal.subgoal_id),
        task_id=task_id,
        subgoal_id=subgoal.subgoal_id,
        action_kind=subgoal.action_kind,
        text=subgoal.intent_text,
        resource_id=subgoal.intent_resource_id,
        accessibility_id=subgoal.intent_accessibility_id,
        description=subgoal.description,
        argument_refs=dict(subgoal.argument_refs),
        expected_predicates=list(subgoal.expected),
        require_clickable=subgoal.action_kind in ("tap", "long_press"),
        constraints=list(constraints or []),
    )


def proposal_from_subgoal(task_id: str, subgoal: Subgoal) -> SubgoalProposal:
    """Deterministic actor projection: subgoal -> typed proposal."""
    return SubgoalProposal(
        subgoal_id=subgoal.subgoal_id,
        description=subgoal.description or subgoal.action_kind,
        intent=intent_from_subgoal(task_id, subgoal),
        expected=list(subgoal.expected),
        depends_on=list(subgoal.depends_on),
        rationale="deterministic planner",
    )


def subgoal_from_proposal(task_id: str, proposal: SubgoalProposal) -> Subgoal:
    """Validate a proposal into an internal subgoal the runner can execute.

    The runner only accepts a proposal that satisfies the intent contract; the
    action kind is taken from the intent so the two can never disagree.
    """
    if proposal.intent.task_id != task_id:
        raise ActorUnavailable("task_mismatch",
                               "proposal intent was created for another task")
    if proposal.intent.action_kind not in ACTION_KINDS:  # pragma: no cover - schema
        raise ActorUnavailable("unsupported_action", "intent action is not supported")
    return Subgoal(
        subgoal_id=proposal.subgoal_id,
        description=proposal.description,
        intent_text=proposal.intent.text,
        intent_resource_id=proposal.intent.resource_id,
        intent_accessibility_id=proposal.intent.accessibility_id,
        action_kind=proposal.intent.action_kind,
        argument_refs=dict(proposal.intent.argument_refs),
        expected=list(proposal.expected or proposal.intent.expected_predicates),
        depends_on=list(proposal.depends_on),
    )
