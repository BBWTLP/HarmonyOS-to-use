"""Traceable fact memory.

Recent raw states, verified facts, planning hypotheses and unresolved events are
kept apart. A summary references evidence and is never itself an executable
observation. Compression first drops raw states, never user constraints or
unresolved incidents.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .decision.tokens import estimate_tokens

DEFAULT_WINDOW_TOKENS = 3000

#: Questions the runner must still be able to answer after a compression: the
#: task's own constraints, its unresolved incidents and the goal itself. The plan
#: requires compression to preserve exactly these, so they are checked, not
#: assumed.
def compression_questions(*, goal: str, constraints: Iterable[str],
                          incident_codes: Iterable[str]) -> list[str]:
    return list(dict.fromkeys([goal, *constraints, *incident_codes]))


def missing_after_compression(memory: "Memory", questions: Iterable[str]) -> list[str]:
    """Which required questions the compressed context can no longer answer."""
    answers = memory.can_answer(questions)
    return [question for question, ok in answers.items() if not ok]


@dataclass
class Fact:
    key: str
    value: Any
    evidence_ref: str
    observed_at: float
    scope: str = ""
    may_be_stale: bool = False
    kind: str = "fact"          # fact | hypothesis

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "evidence_ref": self.evidence_ref,
                "observed_at": self.observed_at, "scope": self.scope,
                "may_be_stale": self.may_be_stale, "kind": self.kind}


@dataclass
class Incident:
    incident_id: str
    code: str
    request_id: str | None
    created_at: float
    resolved: bool = False
    resolution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"incident_id": self.incident_id, "code": self.code,
                "request_id": self.request_id, "created_at": self.created_at,
                "resolved": self.resolved, "resolution": self.resolution}


@dataclass
class SubgoalRecord:
    subgoal_id: str
    description: str
    status: str = "pending"
    attempts: int = 0
    evidence: list[str] = field(default_factory=list)


class Memory:
    def __init__(self, *, goal: str, constraints: Iterable[str] = (),
                 window_tokens: int = DEFAULT_WINDOW_TOKENS):
        self.goal = goal
        self.constraints = list(constraints)
        self.window_tokens = window_tokens
        self.states: list[dict[str, Any]] = []
        self.facts: dict[str, Fact] = {}
        self.hypotheses: dict[str, Fact] = {}
        self.incidents: list[Incident] = []
        self.subgoals: list[SubgoalRecord] = []
        self.summaries: list[dict[str, Any]] = []
        self.retrieval_log: list[dict[str, Any]] = []

    # -- writes -------------------------------------------------------------
    def record_state(self, observation: dict[str, Any]) -> None:
        self.states.append({
            "observation_id": observation.get("observation_id"),
            "at": observation.get("captured_at", time.time()),
            "foreground": observation.get("foreground_bundle"),
            "fingerprint": observation.get("fingerprint"),
            "texts": [item.get("text") for item in observation.get("catalog", []) if item.get("text")],
            "actionable": observation.get("actionable"),
        })

    def record_fact(self, key: str, value: Any, evidence_ref: str, *,
                    scope: str = "", may_be_stale: bool = False) -> Fact:
        fact = Fact(key=key, value=value, evidence_ref=evidence_ref,
                    observed_at=time.time(), scope=scope, may_be_stale=may_be_stale)
        self.facts[key] = fact
        return fact

    def record_hypothesis(self, key: str, value: Any, evidence_ref: str) -> Fact:
        fact = Fact(key=key, value=value, evidence_ref=evidence_ref,
                    observed_at=time.time(), kind="hypothesis")
        self.hypotheses[key] = fact
        return fact

    def record_incident(self, incident_id: str, code: str, request_id: str | None) -> Incident:
        incident = Incident(incident_id=incident_id, code=code, request_id=request_id,
                            created_at=time.time())
        self.incidents.append(incident)
        return incident

    def resolve_incident(self, incident_id: str, resolution: str) -> bool:
        for incident in self.incidents:
            if incident.incident_id == incident_id:
                incident.resolved = True
                incident.resolution = resolution
                return True
        return False

    def add_subgoal(self, subgoal_id: str, description: str) -> SubgoalRecord:
        record = SubgoalRecord(subgoal_id=subgoal_id, description=description)
        self.subgoals.append(record)
        return record

    # -- reads --------------------------------------------------------------
    def unresolved(self) -> list[Incident]:
        return [incident for incident in self.incidents if not incident.resolved]

    def retrieve(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        for fact in list(self.facts.values()) + list(self.hypotheses.values()):
            if needle in str(fact.key).lower() or needle in str(fact.value).lower():
                hits.append(fact.to_dict())
        for state in reversed(self.states):
            if any(needle in str(text).lower() for text in state["texts"]):
                hits.append({"kind": "state", **state})
            if len(hits) >= limit:
                break
        self.retrieval_log.append({"query": query, "hits": len(hits)})
        return hits[:limit]

    def evidence_index(self) -> dict[str, str]:
        index = {fact.key: fact.evidence_ref for fact in self.facts.values()}
        index.update({fact.key: fact.evidence_ref for fact in self.hypotheses.values()})
        return index

    def context_facts(self) -> list[str]:
        lines = [f"{fact.key}={fact.value}" for fact in self.facts.values()]
        lines += [f"（假设）{fact.key}={fact.value}" for fact in self.hypotheses.values()]
        lines += [f"未决事件：{incident.code}" for incident in self.unresolved()]
        return lines

    # -- compression --------------------------------------------------------
    def window_used_tokens(self) -> int:
        """Estimated tokens held by the raw recent states."""
        return sum(estimate_tokens(json.dumps(state, ensure_ascii=False))
                   for state in self.states)

    def over_window(self) -> bool:
        return self.window_used_tokens() > self.window_tokens

    def compress(self, *, reason: str = "window") -> dict[str, Any]:
        """Drop raw states first; keep constraints, facts and unresolved events."""
        before = len(self.states)
        kept: list[dict[str, Any]] = []
        budget = self.window_tokens
        for state in reversed(self.states):
            cost = estimate_tokens(json.dumps(state, ensure_ascii=False))
            if cost <= budget:
                kept.append(state)
                budget -= cost
        self.states = list(reversed(kept))
        record = {
            "reason": reason,
            "at": time.time(),
            "states_before": before,
            "states_after": len(self.states),
            "kept_constraints": list(self.constraints),
            "kept_facts": len(self.facts),
            "unresolved_incidents": [incident.to_dict() for incident in self.unresolved()],
            "evidence_index": self.evidence_index(),
            "note": "compression output is a reference, not an executable observation",
        }
        self.summaries.append(record)
        return record

    def can_answer(self, questions: Iterable[str]) -> dict[str, bool]:
        """Post-compression check: user constraints, done conditions, unknown writes."""
        answers: dict[str, bool] = {}
        for question in questions:
            needle = question.lower()
            answers[question] = any(
                needle in str(item).lower()
                for item in [self.goal, *self.constraints,
                             *[f"{f.key}={f.value}" for f in self.facts.values()],
                             *[i.code for i in self.unresolved()]]
            )
        return answers

    def snapshot(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "constraints": list(self.constraints),
            "facts": [fact.to_dict() for fact in self.facts.values()],
            "hypotheses": [fact.to_dict() for fact in self.hypotheses.values()],
            "incidents": [incident.to_dict() for incident in self.incidents],
            "subgoals": [{"subgoal_id": s.subgoal_id, "description": s.description,
                          "status": s.status, "attempts": s.attempts,
                          "evidence": list(s.evidence)} for s in self.subgoals],
            "states": len(self.states),
            "summaries": len(self.summaries),
        }
