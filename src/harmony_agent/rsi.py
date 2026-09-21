"""Mobile RSI practice loop: waves, barriers, staged memory (P3-02, P3-05, P3-06).

A *wave* runs several practice branches in parallel from the **same** frozen
memory snapshot. Every branch goes through the production path (Runtime guard,
journal, candidate registry, checker, supervisor) against the mock device. Nothing
is merged until every branch has finished: the barrier exists so two branches can
never interleave their lessons into shared memory.

After the barrier, branches are merged in a fixed order. A branch contributes an
experience only when its own read-only verification passed and carried evidence;
an inconclusive verdict, an unknown execution or an infrastructure error is
reported as blocked and never becomes memory.

Phase 3 (online reuse) mounts the frozen snapshot read-only. The store is not
reachable from an online run at all: `FrozenMemory` has no write method.
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .experience import (Experience, ExperienceGate, ExperienceGrounding,
                         ExperienceScope, ExperienceTrigger, ProcedureStep,
                         build_experience)
from .fake_device import MOCK_APP, FaultProfile, open_practice_runtime
from .memory_store import FrozenMemory, MemoryStore, merge_experiences

#: The learning state machine from the plan. A branch advances through it in
#: order; only MEMORY_COMMITTED → MEMORY_FROZEN touches shared memory.
PRACTICE_STATES = ("PRACTICE_AUTHORED", "ACTOR_ATTEMPTED", "VERIFIER_PASS",
                   "VERIFIER_FAIL", "VERIFIER_INCONCLUSIVE", "EXPERIENCE_STAGED",
                   "MEMORY_COMMITTED", "MEMORY_FROZEN")

#: Outcome kinds that may never be merged into memory, with the reason.
BLOCKING_OUTCOMES = {
    "inconclusive": "verifier_inconclusive",
    "execution_unknown": "execution_unknown",
    "infrastructure_error": "infrastructure_error",
    "blocked_user": "user_intervention_required",
}

#: Outcome kinds that justify another practice round (P3-05).
PRACTICE_TRIGGERS = ("failed", "fragile_pass")


@dataclass
class PracticeBranch:
    """One authored practice attempt. Low risk by construction: mock, read-back only."""

    branch_id: str
    task: dict[str, Any]
    label: str = ""
    expect: str = "verified"
    faults: FaultProfile | None = None
    build: str = "mock-1"
    risk_class: str = "R1"


@dataclass
class BranchOutcome:
    branch_id: str
    label: str
    task_id: str
    state: str
    reason: str
    outcome_kind: str
    attempts: int = 0
    dispatches: int = 0
    verification: dict[str, Any] | None = None
    evidence_refs: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    memory_manifest_hash: str | None = None
    state_machine: list[str] = field(default_factory=list)
    experience_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"branch_id": self.branch_id, "label": self.label,
                "task_id": self.task_id, "state": self.state, "reason": self.reason,
                "outcome_kind": self.outcome_kind, "attempts": self.attempts,
                "dispatches": self.dispatches, "verification": self.verification,
                "evidence_refs": list(self.evidence_refs),
                "experience_id": self.experience_id,
                "memory_manifest_hash": self.memory_manifest_hash,
                "state_machine": list(self.state_machine),
                "detail": self.detail}


@dataclass
class WaveReport:
    wave_id: str
    memory_in: dict[str, Any]
    branches: list[BranchOutcome]
    barrier_held: bool
    merged_experience_ids: list[str] = field(default_factory=list)
    blocked_branch_ids: list[str] = field(default_factory=list)
    practice_targets: list[str] = field(default_factory=list)
    memory_out: dict[str, Any] | None = None
    status: str = "NO_MEMORY_CHANGE"
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "wave_id": self.wave_id,
                "memory_in": self.memory_in, "memory_out": self.memory_out,
                "barrier_held": self.barrier_held,
                "status": self.status,
                "merged_experience_ids": list(self.merged_experience_ids),
                "blocked_branch_ids": list(self.blocked_branch_ids),
                "practice_targets": list(self.practice_targets),
                "branches": [branch.to_dict() for branch in self.branches],
                "limitations": list(self.limitations)}


def procedure_from_steps(steps: str) -> list[ProcedureStep]:
    """Parse the delegated step syntax into semantic procedure steps."""
    procedures: list[ProcedureStep] = []
    for raw in [item.strip() for item in (steps or "").split("|") if item.strip()]:
        kind, _, target = raw.partition(":")
        kind = kind.strip()
        target = target.strip()
        intent: dict[str, Any] = {"action_kind": kind}
        expected: dict[str, Any] = {}
        if kind in ("back", "home"):
            pass
        elif kind == "swipe":
            intent["argument_refs"] = {"direction": target or "down"}
        elif kind == "launch":
            intent["argument_refs"] = {"bundle": target}
        else:
            value = None
            if "=" in target:
                target, _, value = target.partition("=")
                target, value = target.strip(), value.strip()
            if target.startswith("id:"):
                intent["resource_id"] = target[3:]
            else:
                intent["text"] = target
            if value:
                reference = value if value.startswith("arg.") else f"arg.{value}"
                intent["argument_refs"] = {"text": reference}
                expected = {"predicate": "input_equals",
                            "target_key": "id:search_input", "value_ref": reference}
            else:
                expected = {"predicate": "page_changed"}
        procedures.append(ProcedureStep(intent=intent, expected=expected))
    return procedures


def grounding_layers_for(steps: str) -> list[str]:
    """Which grounding layers the authored steps actually used."""
    layers: list[str] = []
    for step in procedure_from_steps(steps):
        intent = step.intent
        if intent.get("resource_id"):
            layers.append("resource_id")
        if intent.get("text"):
            layers.append("text_exact")
        if intent.get("action_kind") in ("launch", "swipe", "back", "home"):
            layers.append("page_identity")
    return list(dict.fromkeys(layers)) or ["page_identity"]


class WaveRunner:
    """Run practice branches from one memory snapshot and merge after a barrier."""

    def __init__(self, root: str | Path, *, memory_store: MemoryStore,
                 max_workers: int = 4, gate: ExperienceGate | None = None,
                 branch_timeout: float = 180.0):
        self.root = Path(root)
        self.memory_store = memory_store
        self.max_workers = max(1, int(max_workers))
        self.gate = gate or ExperienceGate()
        self.branch_timeout = branch_timeout

    # -- one branch ---------------------------------------------------------
    def run_branch(self, branch: PracticeBranch, memory: FrozenMemory) -> BranchOutcome:
        owner = f"practice-{branch.branch_id}"
        skills = open_practice_runtime(self.root / "branches" / branch.branch_id,
                                       owner=owner, faults=branch.faults,
                                       frozen_memory=memory)
        machine = ["PRACTICE_AUTHORED"]
        try:
            created = skills.host.run_task(owner, session_id=skills.session_id,
                                           task=branch.task)
            task_id = created["task_id"]
            deadline = time.time() + self.branch_timeout
            status = skills.host.task_status(owner, task_id=task_id)
            while not (status["terminal"] or status["status"] in
                       ("PAUSED", "RECONCILIATION_REQUIRED", "WAITING_USER")):
                if time.time() > deadline:
                    skills.host.task_control(owner, task_id=task_id, operation="cancel")
                    return BranchOutcome(
                        branch_id=branch.branch_id, label=branch.label, task_id=task_id,
                        state=status["status"], reason="branch_timeout",
                        outcome_kind="infrastructure_error",
                        memory_manifest_hash=memory.manifest_hash,
                        state_machine=[*machine, "ACTOR_ATTEMPTED"],
                        detail="practice branch exceeded its wall-clock budget")
                time.sleep(0.02)
                status = skills.host.task_status(owner, task_id=task_id)
            result = skills.host.task_result(owner, task_id=task_id).get("result") or {}
            events = skills.host.task_events(owner, task_id=task_id, limit=200)["items"]
            machine.append("ACTOR_ATTEMPTED")
            verification = next((item["payload"] for item in reversed(events)
                                 if item["type"] == "verification"), None)
            return self._classify(branch, task_id, status["status"], result,
                                  verification, machine, memory)
        finally:
            skills.close()

    def _classify(self, branch: PracticeBranch, task_id: str, state: str,
                  result: dict[str, Any], verification: dict[str, Any] | None,
                  machine: list[str], memory: FrozenMemory) -> BranchOutcome:
        usage = result.get("usage") or {}
        dispatches = int(usage.get("dispatches", 0))
        attempts = max([int(item.get("attempts", 0))
                        for item in (result.get("plan") or {}).get("subgoals", [])] or [0])
        evidence = list(verification.get("evidence_refs") if verification else [] or [])
        for condition in result.get("conditions") or []:
            evidence.extend(condition.get("evidence_refs") or [])
        if result.get("result_artifact_id"):
            evidence.append(f"artifact:{result['result_artifact_id']}")
        if result.get("unresolved_actions"):
            evidence.extend(f"incident:{item.get('request_id')}"
                            for item in result["unresolved_actions"])
        evidence = list(dict.fromkeys(evidence))
        reason = str(result.get("reason") or "")
        verdict = (verification or {}).get("report", {}).get("verdict") if verification else None
        if state == "RECONCILIATION_REQUIRED":
            kind, machine = "execution_unknown", [*machine, "VERIFIER_INCONCLUSIVE"]
        elif state == "WAITING_USER":
            kind, machine = "blocked_user", [*machine, "VERIFIER_INCONCLUSIVE"]
        elif state == "SUCCEEDED":
            kind = "verified" if attempts <= 1 else "fragile_pass"
            machine = [*machine, "VERIFIER_PASS"]
        elif state == "FAILED" and verdict == "fail":
            kind, machine = "failed", [*machine, "VERIFIER_FAIL"]
        elif state in ("FAILED", "PARTIAL", "PAUSED", "CANCELLED"):
            kind, machine = "inconclusive", [*machine, "VERIFIER_INCONCLUSIVE"]
        else:
            kind, machine = "infrastructure_error", [*machine, "VERIFIER_INCONCLUSIVE"]
        return BranchOutcome(
            branch_id=branch.branch_id, label=branch.label, task_id=task_id,
            state=state, reason=reason, outcome_kind=kind, attempts=attempts,
            dispatches=dispatches, verification=verification,
            evidence_refs=evidence, result=result,
            memory_manifest_hash=memory.manifest_hash, state_machine=machine)

    # -- wave ---------------------------------------------------------------
    def run(self, branches: Iterable[PracticeBranch], *, memory: FrozenMemory | None = None,
            approval_ref: str = "", parallel: bool = True,
            freeze: bool = True) -> WaveReport:
        branches = list(branches)
        memory = memory or self.memory_store.freeze()
        wave_id = "wave_" + uuid.uuid4().hex[:16]
        report = WaveReport(wave_id=wave_id,
                            memory_in={"version": memory.version,
                                       "manifest_hash": memory.manifest_hash},
                            branches=[], barrier_held=False)
        if not memory.verify():
            report.status = "BLOCKED_MEMORY_HASH"
            report.limitations.append("frozen memory failed hash verification")
            return report
        if parallel and len(branches) > 1:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(branches))) as pool:
                outcomes = list(pool.map(lambda branch: self.run_branch(branch, memory),
                                         branches))
        else:
            outcomes = [self.run_branch(branch, memory) for branch in branches]
        # Barrier: every branch has finished before anything is merged.
        report.barrier_held = True
        report.branches = sorted(outcomes, key=lambda item: item.branch_id)
        self._merge(report, branches, approval_ref, freeze=freeze)
        return report

    def _merge(self, report: WaveReport, branches: list[PracticeBranch],
               approval_ref: str, *, freeze: bool) -> None:
        by_id = {branch.branch_id: branch for branch in branches}
        candidates: list[Experience] = []
        for outcome in report.branches:
            branch = by_id[outcome.branch_id]
            if outcome.outcome_kind in BLOCKING_OUTCOMES:
                report.blocked_branch_ids.append(outcome.branch_id)
                outcome.detail = outcome.detail or BLOCKING_OUTCOMES[outcome.outcome_kind]
                continue
            if not outcome.evidence_refs:
                report.blocked_branch_ids.append(outcome.branch_id)
                outcome.detail = outcome.detail or "evidence_missing"
                continue
            try:
                experience = self._experience(branch, outcome)
            except Exception as error:  # a malformed lesson is blocked, not merged
                report.blocked_branch_ids.append(outcome.branch_id)
                outcome.detail = f"experience_invalid:{type(error).__name__}"
                continue
            verdict = "pass" if outcome.outcome_kind in ("verified", "fragile_pass") else "fail"
            try:
                staged = self.gate.stage(experience, verdict=verdict,
                                         verifier=(outcome.verification or {}).get("verifier", "code"),
                                         verifier_revision=(outcome.verification or {}).get(
                                             "revision", "checker:1"))
                self.gate.commit(staged, approval_ref=approval_ref)
            except Exception as error:
                report.blocked_branch_ids.append(outcome.branch_id)
                outcome.detail = f"gate:{getattr(error, 'code', type(error).__name__)}"
                continue
            outcome.experience_id = experience.experience_id
            outcome.state_machine = [*outcome.state_machine, "EXPERIENCE_STAGED"]
            candidates.append(experience)
            if outcome.outcome_kind in PRACTICE_TRIGGERS:
                report.practice_targets.append(outcome.branch_id)
        if not candidates:
            report.status = "NO_MEMORY_CHANGE"
            return
        # Snapshots are materialised (full entry list) so an online freeze is
        # self-contained; the parent manifest hash still chains the history.
        previous = self.memory_store.latest()
        merged = merge_experiences([*(previous[1] if previous else []), *candidates])
        manifest = self.memory_store.commit(merged, parent=previous[0] if previous else None)
        report.merged_experience_ids = [item.experience_id for item in candidates]
        for outcome in report.branches:
            if outcome.experience_id in report.merged_experience_ids:
                outcome.state_machine = [*outcome.state_machine, "MEMORY_COMMITTED"]
        if freeze:
            frozen = self.memory_store.freeze(manifest.version)
            report.memory_out = {"version": frozen.version,
                                 "manifest_hash": frozen.manifest_hash,
                                 "frozen": True}
            for outcome in report.branches:
                if outcome.experience_id in report.merged_experience_ids:
                    outcome.state_machine = [*outcome.state_machine, "MEMORY_FROZEN"]
        else:  # pragma: no cover - exercised by the freeze_memory tool
            report.memory_out = {"version": manifest.version,
                                 "manifest_hash": manifest.manifest_hash,
                                 "frozen": False}
        report.memory_out.update({"entry_count": len(merged),
                                  "new_entry_count": len(candidates)})
        report.status = ("MERGED_WITH_BLOCKED" if report.blocked_branch_ids else "MERGED")

    def _experience(self, branch: PracticeBranch, outcome: BranchOutcome) -> Experience:
        task = branch.task
        steps = str((task.get("arguments") or {}).get("steps", ""))
        goal = str(task.get("goal", branch.label or "practice"))
        failure_modes: list[str] = []
        if outcome.outcome_kind == "fragile_pass":
            failure_modes.append("fragile_pass")
        if outcome.outcome_kind == "failed":
            failure_modes.append(outcome.reason or "verification_failed")
        return build_experience(
            scope=ExperienceScope(app=MOCK_APP, build=branch.build,
                                  device_capability="mock_driver"),
            trigger=ExperienceTrigger(foreground=MOCK_APP,
                                      facts=[f"goal={goal}",
                                             f"steps={len(procedure_from_steps(steps))}"],
                                      goal_pattern=branch.label or goal),
            grounding=ExperienceGrounding(layers=grounding_layers_for(steps),
                                          identity=steps or goal),
            procedure=procedure_from_steps(steps),
            outcome="verified" if outcome.outcome_kind in ("verified", "fragile_pass") else "failed",
            failure_modes=failure_modes,
            risk_class=branch.risk_class,
            evidence_refs=outcome.evidence_refs,
            source_task_id=outcome.task_id,
            ttl_days=30)

class PracticeSelector:
    """Pick the next practice round from failures and fragile successes (P3-05)."""

    def __init__(self, catalogue: dict[str, PracticeBranch]):
        self.catalogue = dict(catalogue)

    def select(self, outcomes: Iterable[BranchOutcome], *, limit: int = 4) -> list[PracticeBranch]:
        selected: list[PracticeBranch] = []
        for outcome in sorted(outcomes, key=lambda item: item.branch_id):
            if outcome.outcome_kind not in PRACTICE_TRIGGERS:
                # Infrastructure errors and unknown executions are excluded: they
                # are not evidence that the task itself is hard.
                continue
            branch = self.catalogue.get(outcome.label)
            if branch is None or branch.risk_class == "R3":
                continue
            if branch.branch_id in {item.branch_id for item in selected}:
                continue
            selected.append(branch)
            if len(selected) >= limit:
                break
        return selected


def frozen_facts(memory: FrozenMemory, limit: int = 8) -> list[str]:
    """Convenience wrapper used by the practice CLI."""
    return memory.facts(limit=limit)
