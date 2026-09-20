"""Versioned plan with bounded subgoal sequencing and loop detection."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .contracts import Predicate, TaskSubmit

#: Default subgoal budget: 8 dispatches / 120 s, per the architecture defaults.
DEFAULT_SUBGOAL_DISPATCHES = 8
DEFAULT_SUBGOAL_SECONDS = 120


@dataclass
class Subgoal:
    subgoal_id: str
    description: str
    intent_text: str | None = None
    intent_resource_id: str | None = None
    intent_accessibility_id: str | None = None
    action_kind: str = "tap"
    argument_refs: dict[str, str] = field(default_factory=dict)
    expected: list[Predicate] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0


@dataclass
class Plan:
    goal: str
    subgoals: list[Subgoal]
    version: int = 1
    created_at: float = field(default_factory=time.time)
    reasons: list[str] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        payload = repr([(s.subgoal_id, s.description, s.action_kind, s.intent_text,
                         s.intent_resource_id, s.depends_on) for s in self.subgoals])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def next_pending(self) -> Subgoal | None:
        done = {s.subgoal_id for s in self.subgoals if s.status in ("verified", "skipped")}
        for subgoal in self.subgoals:
            if subgoal.status in ("pending", "retry") and all(dep in done or not dep for dep in subgoal.depends_on):
                return subgoal
        return None

    def by_id(self, subgoal_id: str) -> Subgoal | None:
        return next((s for s in self.subgoals if s.subgoal_id == subgoal_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "version": self.version, "hash": self.hash,
                "created_at": self.created_at, "reasons": list(self.reasons),
                "parameters": dict(self.parameters), "constraints": list(self.constraints),
                "subgoals": [{"subgoal_id": s.subgoal_id, "description": s.description,
                              "action_kind": s.action_kind, "status": s.status,
                              "attempts": s.attempts, "depends_on": list(s.depends_on)}
                             for s in self.subgoals]}


class LoopDetector:
    """Three consecutive identical semantic actions with an unchanged state."""

    def __init__(self, limit: int = 3):
        self.limit = limit
        self.history: list[tuple[str, str]] = []
        self.scroll_streak = 0

    def observe(self, signature: str, state_fingerprint: str, *, is_scroll: bool = False) -> bool:
        if is_scroll:
            # An explicit scrolling subgoal is not a loop.
            return False
        self.history.append((signature, state_fingerprint))
        self.history = self.history[-self.limit:]
        if len(self.history) < self.limit:
            return False
        return len(set(self.history)) == 1


#: Predicate types that name a target the plan must act on. Every other
#: predicate is a postcondition that the checker decides at the end.
ACTION_PREDICATES = ("element_present", "input_equals")

#: Reserved task argument holding an ordered delegated step list.
STEPS_ARGUMENT = "steps"

STEP_ACTIONS = ("tap", "long_press", "replace_text", "input_text", "swipe",
                "back", "home", "launch")


def plan_from_task(task: TaskSubmit) -> Plan:
    """Derive an ordered action plan from the caller's own criteria.

    Delegated and auto tasks both describe what must be true at the end. The
    deterministic planner only turns a criterion that names a target into an
    action step; anything else is left to verification. A criterion that is
    already satisfied is skipped without dispatching.
    """
    if task.arguments.get(STEPS_ARGUMENT):
        return _plan_from_steps(task)
    return _plan_from_criteria(task)


def _plan_from_steps(task: TaskSubmit) -> Plan:
    """Delegated mode: the caller supplied an ordered plan, the runner guards it.

    Step syntax (``|`` separated):
      ``tap:搜索`` ``tap:id:search_submit`` ``replace_text:id:search_input=query``
      ``replace_text:搜索=literal:鸿蒙`` ``swipe:down`` ``back`` ``home``
      ``launch:com.sina.weibo.stage``
    A value after ``=`` names a task argument (``arg.query`` or ``query``);
    ``literal:`` prefixes a caller-supplied constant. Targets are semantic
    (text or resource id) and are re-grounded before every dispatch.
    """
    parameters = dict(task.arguments)
    subgoals: list[Subgoal] = []
    raw_steps = [step.strip() for step in task.arguments[STEPS_ARGUMENT].split("|") if step.strip()]
    if not raw_steps:
        raise ValueError("steps argument is empty")
    for index, raw in enumerate(raw_steps, start=1):
        kind, _, target = raw.partition(":")
        kind = kind.strip()
        if kind not in STEP_ACTIONS:
            raise ValueError(f"unsupported step action {kind!r}")
        subgoal = Subgoal(subgoal_id=f"step_{index:02d}", description=raw,
                          action_kind=kind)
        target = target.strip()
        if kind in ("back", "home"):
            if target:
                raise ValueError(f"{kind} takes no target")
        elif kind == "launch":
            subgoal.argument_refs = {"bundle": target}
        elif kind == "swipe":
            subgoal.argument_refs = {"direction": target or "down"}
        else:
            value = None
            if "=" in target:
                target, _, value = target.partition("=")
                target, value = target.strip(), value.strip()
            if not target:
                raise ValueError(f"{kind} requires a target")
            subgoal.intent_text = None if target.startswith("id:") else target
            subgoal.intent_resource_id = target[3:] if target.startswith("id:") else None
            if value:
                if value.startswith("literal:"):
                    name = f"literal_{index}"
                    parameters[name] = value[len("literal:"):]
                    reference = f"arg.{name}"
                else:
                    reference = value if value.startswith("arg.") else f"arg.{value}"
                subgoal.argument_refs = {"text": reference}
        subgoals.append(subgoal)
    return Plan(goal=task.goal, subgoals=subgoals, parameters=parameters,
                constraints=_constraints(task))


def _plan_from_criteria(task: TaskSubmit) -> Plan:
    subgoals: list[Subgoal] = []
    parameters = dict(task.arguments)
    for index, item in enumerate(task.success_criteria, start=1):
        if item.type not in ACTION_PREDICATES or not item.target_key:
            continue
        key = item.target_key
        intent_text = None if key.startswith("id:") else key
        intent_resource_id = key[3:] if key.startswith("id:") else None
        if item.type == "input_equals":
            if item.value_ref:
                reference = (item.value_ref if item.value_ref.startswith("arg.")
                             else f"arg.{item.value_ref}")
            else:
                name = f"literal_{index}"
                parameters[name] = str(item.value)
                reference = f"arg.{name}"
            subgoals.append(Subgoal(
                subgoal_id=f"action_{index:02d}",
                description=item.description or f"设置 {key} 的内容",
                intent_text=intent_text,
                intent_resource_id=intent_resource_id,
                action_kind="replace_text",
                argument_refs={"text": reference},
            ))
        else:
            subgoals.append(Subgoal(
                subgoal_id=f"action_{index:02d}",
                description=item.description or f"打开 {key}",
                intent_text=intent_text,
                intent_resource_id=intent_resource_id,
                action_kind="tap",
            ))
    return Plan(goal=task.goal, subgoals=subgoals, parameters=parameters,
                constraints=_constraints(task))


def _constraints(task: TaskSubmit) -> list[str]:
    return [f"allowed_apps={task.scope.allowed_apps}",
            f"allowed_actions={task.scope.allowed_actions}",
            f"cloud_data_policy={task.scope.cloud_data_policy}"]


def replan(plan: Plan, incident: str, *, reason: str) -> Plan:
    """Append a version; never reset budgets or rewrite the original goal."""
    return Plan(goal=plan.goal, version=plan.version + 1, subgoals=list(plan.subgoals),
                parameters=dict(plan.parameters), constraints=list(plan.constraints),
                reasons=[*plan.reasons, f"{reason}:{incident}"])


def budget_for(subgoal: Subgoal, remaining: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_dispatches": min(DEFAULT_SUBGOAL_DISPATCHES, int(remaining.get("dispatches", 0))),
        "max_seconds": min(DEFAULT_SUBGOAL_SECONDS, float(remaining.get("seconds", 0))),
        "max_model_calls": int(remaining.get("model_calls", 0)),
    }
