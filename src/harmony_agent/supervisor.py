"""TaskSupervisor and TaskRunner.

The supervisor owns task state, budget and the event log in one SQLite
database. The runner holds no device driver and no writable journal: every
observation and every write goes through the runtime's public session API, so
the guard, epoch checks and the unknown-write barrier still apply.

After a service restart a RUNNING task is not resumed automatically; it is
reported as PAUSED so that a human decides.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .candidates import CandidateError, CandidateRegistry, RegisteredCandidate
from .actor import (ActorObservation, ActorRequest, ActorUnavailable, ControlProposal,
                    SubgoalProposal, subgoal_from_proposal)
from .checker import CheckReport, ReadOnlyChecker
from .contracts import (EventEnvelope, GuardedAction, Predicate, PROTOCOL_VERSION,
                        ProtocolError, TaskSubmit, canonical)
from .decision.router import Router, RouterOutcome
from .grounding import GroundingIntent, GroundingUnavailable
from .memory import Memory, compression_questions, missing_after_compression
from .planner import LoopDetector, Plan, Subgoal, plan_from_task, replan
from .verifier import (CodeVerifier, VerificationOutcome, VerificationRequest,
                       VerifierProvider, verify_once)

TERMINAL = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED")
#: States that carry a final result. Once one of these is readable through
#: `TaskStore.task()`, the matching result must be readable in the same read.
#: `RECONCILIATION_REQUIRED` is not TERMINAL (the task can still be reconciled),
#: but it is result-bearing: it ends the autonomous run and reports an outcome.
RESULT_BEARING_STATES = ("SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED",
                         "RECONCILIATION_REQUIRED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  request_key TEXT UNIQUE NOT NULL,
  goal_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  controller_epoch INTEGER NOT NULL,
  payload TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  result TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  envelope TEXT,
  created REAL NOT NULL,
  PRIMARY KEY (task_id, sequence)
);
CREATE TABLE IF NOT EXISTS task_plans (
  task_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  created REAL NOT NULL,
  PRIMARY KEY (task_id, version)
);
"""


class TaskError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class RunContext:
    """Provenance in force while a run writes its event log (P2-02).

    The runner updates this as it observes, grounds and decides. Every event gets
    a snapshot, so the event log answers "which observation, epoch, candidate set,
    model revision and calibration was this written under?" without replaying the
    run. `model_revision`/`calibration_version` are the most recent decision
    provenance the run has seen; decision events also carry their own exact values
    in the payload.
    """

    observation_id: str | None = None
    candidate_set_hash: str | None = None
    controller_epoch: int | None = None
    model_revision: str | None = None
    calibration_version: str | None = None
    decision_provider: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    #: The last observation dict of this run, used only to build a bounded,
    #: redacted request for an actor. Never handed to a model verbatim.
    last_observation: dict[str, Any] | None = None
    #: The runtime's own post-dispatch capture of the last dispatched action.
    #: It is the freshest device fact the run holds, so the next step consumes
    #: it instead of paying for a second look at the same page. The guard still
    #: re-reads the device before every write.
    post_observation: dict[str, Any] | None = None
    limit: int = 16

    def note_evidence(self, refs: list[str] | tuple[str, ...] | None) -> None:
        for ref in refs or []:
            if ref and ref not in self.evidence_refs:
                self.evidence_refs.append(ref)
        if len(self.evidence_refs) > self.limit:
            self.evidence_refs = self.evidence_refs[-self.limit:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "candidate_set_hash": self.candidate_set_hash,
            "controller_epoch": self.controller_epoch,
            "model_revision": self.model_revision,
            "calibration_version": self.calibration_version,
            "decision_provider": self.decision_provider,
            "evidence_refs": list(self.evidence_refs),
        }


def safety_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__


def post_observation(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The runtime's own fresh verification capture of a dispatched action.

    It is a current device fact (the runtime took it after the write, to verify
    the postcondition) and it stays a live handle under the runtime's normal
    freshness rule. Reusing it saves a second look at the same page; it never
    replaces the guard, which re-reads the device before every write.
    """
    if not isinstance(result, dict):
        return None
    observation = result.get("observation")
    if not isinstance(observation, dict) or observation.get("actionable") is not True:
        return None
    observation_id = observation.get("observation_id")
    if not observation_id or observation_id == result.get("before_observation_id"):
        return None
    if result.get("execution_status") not in ("executed", None):
        return None
    return observation


class TaskStore:
    """Transactional task state, event log and plan history."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._migrate()
        # A task that was running when the process died is never resumed.
        self.db.execute("UPDATE tasks SET state='PAUSED'"
                        " WHERE state IN ('RUNNING','VERIFYING','RECOVERING')")
        self.db.commit()

    def _migrate(self) -> None:
        """Add the P2-02 event envelope column to a database written before it."""
        columns = {row[1] for row in
                   self.db.execute("PRAGMA table_info(task_events)").fetchall()}
        if "envelope" not in columns:
            self.db.execute("ALTER TABLE task_events ADD COLUMN envelope TEXT")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def create(self, task: TaskSubmit, controller_epoch: int) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                "SELECT task_id, state, payload FROM tasks WHERE request_key=?",
                (task.request_id,)).fetchone()
            if row is not None:
                stored = TaskSubmit.model_validate(json.loads(row[2]))
                if stored.model_dump() != task.model_dump():
                    raise TaskError("request_conflict",
                                    "request_id was already used with different arguments")
                return {"task_id": row[0], "state": row[1], "deduplicated": True}
            task_id = "task_" + uuid.uuid4().hex[:24]
            now = time.time()
            self.db.execute(
                "INSERT INTO tasks (task_id, request_key, goal_hash, state, controller_epoch,"
                " payload, created, updated, result) VALUES (?,?,?,?,?,?,?,?,NULL)",
                (task_id, task.request_id, _digest(task.goal), "QUEUED", controller_epoch,
                 json.dumps(task.model_dump()), now, now))
            self.db.commit()
            return {"task_id": task_id, "state": "QUEUED", "deduplicated": False}

    def set_state(self, task_id: str, state: str) -> None:
        with self.lock:
            if state in TERMINAL:
                self.db.execute("UPDATE tasks SET state=?, updated=? WHERE task_id=?",
                                (state, time.time(), task_id))
            else:
                self.db.execute(
                    "UPDATE tasks SET state=?, updated=? WHERE task_id=?"
                    " AND state NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
                    (state, time.time(), task_id))
            self.db.commit()

    def state(self, task_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return row[0] if row else None

    def add_event(self, task_id: str, event_type: str, payload: dict[str, Any],
                  envelope: dict[str, Any] | None = None) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM task_events WHERE task_id=?",
                (task_id,)).fetchone()
            sequence = int(row[0])
            self.db.execute(
                "INSERT INTO task_events (task_id, sequence, type, payload, envelope, created)"
                " VALUES (?,?,?,?,?,?)",
                (task_id, sequence, event_type,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 json.dumps(envelope, ensure_ascii=False, sort_keys=True) if envelope else None,
                 time.time()))
            self.db.commit()
            return sequence

    def add_plan(self, task_id: str, plan: Plan) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO task_plans (task_id, version, payload, created)"
                " VALUES (?,?,?,?)",
                (task_id, plan.version,
                 json.dumps(plan.to_dict(), ensure_ascii=False), time.time()))
            self.db.commit()

    def save_result(self, task_id: str, result: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute("UPDATE tasks SET result=?, updated=? WHERE task_id=?",
                            (json.dumps(result, ensure_ascii=False, sort_keys=True),
                             time.time(), task_id))
            self.db.commit()

    def finalize_task(self, task_id: str, state: str, result: dict[str, Any],
                      event: tuple[str, dict[str, Any]] | None = None,
                      envelope: dict[str, Any] | None = None) -> bool:
        """Publish a result-bearing state and its result in a single transaction.

        Invariant: as soon as `task()` reports a result-bearing state, that same
        read carries the result. Publishing state and result as two separate
        commits opens a window where a poller sees the final state while
        `task()["result"]` is still ``None``.

        Returns ``False`` when a terminal state already won and the outcome was
        therefore not published (the same precedence rule `set_state` applies).
        """
        if state not in RESULT_BEARING_STATES:
            raise TaskError("invalid_final_state",
                            f"{state} is not a result-bearing task state")
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT state FROM tasks WHERE task_id=?",
                                  (task_id,)).fetchone()
            if row is None:
                raise TaskError("unknown_task", "No such task")
            if state not in TERMINAL and row[0] in TERMINAL:
                return False
            try:
                self.db.execute("UPDATE tasks SET state=?, result=?, updated=?"
                                " WHERE task_id=?", (state, payload, now, task_id))
                if event is not None:
                    event_type, event_payload = event
                    sequence = self.db.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 FROM task_events"
                        " WHERE task_id=?", (task_id,)).fetchone()[0]
                    self.db.execute(
                        "INSERT INTO task_events (task_id, sequence, type, payload,"
                        " envelope, created) VALUES (?,?,?,?,?,?)",
                        (task_id, int(sequence), event_type,
                         json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                         json.dumps(envelope, ensure_ascii=False, sort_keys=True)
                         if envelope else None, now))
            except Exception:
                self.db.rollback()
                raise
            self.db.commit()
            return True

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                "SELECT task_id, request_key, state, controller_epoch, payload, created,"
                " updated, result FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return {"task_id": row[0], "request_key": row[1], "state": row[2],
                "controller_epoch": row[3], "payload": json.loads(row[4]),
                "created": row[5], "updated": row[6],
                "result": json.loads(row[7]) if row[7] else None}

    def events(self, task_id: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        with self.lock:
            rows = self.db.execute(
                "SELECT sequence, type, payload, created, envelope FROM task_events"
                " WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (task_id, int(after), limit)).fetchall()
            last = self.db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM task_events WHERE task_id=?",
                (task_id,)).fetchone()[0]
        items = [{"sequence": r[0], "type": r[1], "payload": json.loads(r[2]),
                  "created": r[3],
                  "envelope": json.loads(r[4]) if r[4] else None}
                 for r in rows]
        return {"items": items, "next_seq": items[-1]["sequence"] if items else int(after),
                "last_seq": int(last), "limit": limit}

    def plans(self, task_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT version, payload FROM task_plans WHERE task_id=? ORDER BY version",
                (task_id,)).fetchall()
        return [{"version": r[0], **json.loads(r[1])} for r in rows]

    def inflight(self) -> list[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT task_id FROM tasks WHERE state NOT IN"
                " ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')").fetchall()
        return [row[0] for row in rows]


@dataclass
class RuntimeFacade:
    """The runner's only device access path: the runtime's public session API."""

    runtime: Any
    owner: str
    session_id: str

    def observe(self, mode: str = "FAST", include_image: bool = False) -> dict[str, Any]:
        return self.runtime.observe(self.owner, self.session_id,
                                    include_image=include_image, mode=mode)

    def act(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.act(self.owner, payload)

    def wait(self, **kwargs) -> dict[str, Any]:
        return self.runtime.wait(self.owner, self.session_id, **kwargs)

    def status(self) -> dict[str, Any]:
        return self.runtime.session(self.owner, "status", session_id=self.session_id)

    def controller_epoch(self) -> int:
        return int(self.status().get("controller_epoch", 0))

    def unresolved(self) -> list[dict[str, Any]]:
        return self.status().get("unresolved_actions", [])


@dataclass
class TaskRun:
    task_id: str
    task: TaskSubmit
    plan: Plan
    memory: Memory
    facade: RuntimeFacade
    registry: CandidateRegistry
    router: Router
    checker: ReadOnlyChecker
    store: TaskStore
    artifacts: Any = None
    ocr: Any = None
    matcher: Any = None
    arguments: dict[str, str] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    dispatches: int = 0
    model_calls: int = 0
    observations: int = 0
    loop: LoopDetector = field(default_factory=LoopDetector)
    started_at: float = field(default_factory=time.time)
    events: int = 0
    context: RunContext = field(default_factory=RunContext)
    #: Optional independent verifier. Absent means the deterministic code checker.
    verifier: VerifierProvider | None = None
    #: Optional read-only learning memory mounted for this run (P3-06).
    frozen_memory: Any = None
    #: Optional actor that may propose one bounded recovery subgoal after a step
    #: fails (P4-01). It has no device access: proposals are validated against the
    #: same intent contract and executed through the same guard.
    actor: Any = None
    max_actor_replans: int = field(
        default_factory=lambda: max(0, int(os.environ.get(
            "HARMONY_AGENT_MAX_ACTOR_REPLANS", "2") or 0)))
    actor_replans: int = 0
    #: Compress the recent-state window once it holds this many states *and* is
    #: over budget. Compression never touches constraints, facts or unresolved
    #: incidents; the runner verifies that afterwards.
    compaction_states: int = 12
    #: Context losses found after a compression; reported in the final result.
    limitations: list[str] = field(default_factory=list)
    #: The most recent verification outcome, recorded for the final result.
    last_verification: VerificationOutcome | None = None

    def remaining(self) -> dict[str, Any]:
        elapsed = time.time() - self.started_at
        return {
            "dispatches": max(0, self.task.budget.max_dispatches - self.dispatches),
            "seconds": max(0.0, float(self.task.budget.max_seconds) - elapsed),
            "model_calls": max(0, self.task.budget.max_model_calls - self.model_calls),
            "cost_usd": None,
        }

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events += 1
        envelope = EventEnvelope(
            event_type=event_type, task_id=self.task_id, sequence=0,
            created=time.time(),
            observation_id=self.context.observation_id,
            candidate_set_hash=self.context.candidate_set_hash,
            controller_epoch=self.context.controller_epoch,
            model_revision=self.context.model_revision,
            calibration_version=self.context.calibration_version,
            evidence_refs=list(self.context.evidence_refs),
            payload=payload,
        )
        self.store.add_event(self.task_id, event_type, payload,
                             envelope=envelope.model_dump())

    def verifier_provider(self) -> VerifierProvider:
        return self.verifier or CodeVerifier(self.checker)

    def check_conditions(self, predicates: list[Predicate],
                         observation: dict[str, Any]) -> VerificationOutcome:
        """Verify with the mounted verifier; a pass without evidence downgrades."""
        request = VerificationRequest(
            predicates=list(predicates), observation=observation,
            arguments=self.arguments, incident_free=not self.facade.unresolved(),
            task_id=self.task_id,
            observation_id=str(observation.get("observation_id", "")))
        outcome = verify_once(self.verifier_provider(), request)
        self.last_verification = outcome
        return outcome


class TaskRunner:
    """Executes one plan through the runtime facade under a hard budget."""

    def __init__(self, run: TaskRun):
        self.run = run

    # -- entry --------------------------------------------------------------
    def execute(self) -> dict[str, Any]:
        run = self.run
        if run.frozen_memory is not None and not getattr(run.frozen_memory, "read_only", False):
            raise TaskError("memory_not_frozen",
                            "an online run may only mount a frozen memory snapshot")
        run.store.set_state(run.task_id, "RUNNING")
        run.event("task_started", {"goal_hash": _digest(run.task.goal),
                                   "plan_version": run.plan.version,
                                   "profile": run.router.profile,
                                   "protocol_version": PROTOCOL_VERSION})
        if run.frozen_memory is not None:
            run.event("memory_mounted",
                      {"manifest_hash": getattr(run.frozen_memory, "manifest_hash", None),
                       "version": getattr(run.frozen_memory, "version", None),
                       "read_only": True})
        try:
            while True:
                if run.cancel.is_set():
                    return self._finish("CANCELLED", "cancelled_by_supervisor")
                if run.pause.is_set():
                    return self._paused()
                subgoal = run.plan.next_pending()
                if subgoal is None:
                    break
                run.event("checkpoint", self._checkpoint_payload(subgoal, "start"))
                outcome = self._run_subgoal(subgoal)
                run.event("checkpoint", self._checkpoint_payload(subgoal, "end",
                                                                outcome=outcome))
                if outcome == "cancel":
                    return self._finish("CANCELLED", "cancelled_by_supervisor")
                if outcome == "paused":
                    return self._paused()
                if outcome == "reconciliation":
                    return self._finish("RECONCILIATION_REQUIRED", "unknown_write")
                if outcome == "stop":
                    return self._finish("FAILED", "subgoal_blocked_or_escalated")
            return self._verify()
        except Exception as error:  # the runner must always persist an outcome
            code = safety_code(error)
            run.event("task_error", {"code": code})
            return self._finish("FAILED", code)

    # -- subgoal ------------------------------------------------------------
    def _checkpoint_payload(self, subgoal: Subgoal, phase: str,
                            *,
                            outcome: str | None = None) -> dict[str, Any]:
        """Durable per-step progress: what ran, what it cost, why it exists.

        A long task must be reconstructable from the log alone, so every step
        records its plan version (a replan bumps it), its status and attempts,
        the remaining budget and, for a step that only exists because of a
        replan, that reason.
        """
        run = self.run
        return {
            "phase": phase,
            "task_id": run.task_id,
            "subgoal_id": subgoal.subgoal_id,
            "description": subgoal.description,
            "action_kind": subgoal.action_kind,
            "status": subgoal.status,
            "blocked_reason": subgoal.blocked_reason,
            "attempts": subgoal.attempts,
            "plan_version": run.plan.version,
            "replan_reasons": list(run.plan.reasons)[-2:],
            "outcome": outcome,
            "remaining": run.remaining(),
            "dispatches_used": run.dispatches,
            "observations": run.observations,
            # Resume bundle (T07): enough to continue after restart without
            # resetting budgets or replaying completed dispatches.
            "resume": {
                "task_id": run.task_id,
                "plan_version": run.plan.version,
                "memory_hash": run.memory.snapshot_hash(),
                "device_scope": {
                    "device_ref": getattr(run.task.scope, "device_ref", None),
                    "allowed_apps": list(getattr(run.task.scope, "allowed_apps", []) or []),
                },
                "verified_subgoals": [s.subgoal_id for s in run.plan.subgoals
                                      if s.status == "verified"],
                "pending_request_id": getattr(run.context, "pending_request_id", None),
                "remaining": run.remaining(),
                "last_event_sequence": run.events,
            },
        }

    def _compress_context_if_needed(self) -> None:
        """Compress the recent-state window when it is genuinely over budget.

        Compression is only useful if what must survive actually survives, so the
        runner asks the compressed context the task's own constraints, its
        unresolved incidents and the goal, and records a limitation when any of
        them can no longer be answered. It never silently proceeds on a context
        that lost a constraint.
        """
        run = self.run
        memory = run.memory
        if len(memory.states) < run.compaction_states or not memory.over_window():
            return
        record = memory.compress(reason="window")
        questions = compression_questions(
            goal=run.task.goal, constraints=memory.constraints,
            incident_codes=[incident.code for incident in memory.unresolved()])
        missing = missing_after_compression(memory, questions)
        run.event("memory_compressed", {
            "states_before": record["states_before"],
            "states_after": record["states_after"],
            "kept_constraints": record["kept_constraints"],
            "kept_facts": record["kept_facts"],
            "unresolved_incidents": [item["code"]
                                     for item in record["unresolved_incidents"]],
            "evidence_refs": sorted(record["evidence_index"].values())[:16],
            "questions_checked": len(questions),
            "missing": missing})
        if missing:
            # A lost constraint or unresolved incident is reported, never hidden.
            run.limitations.append("context_loss:" + ",".join(missing)[:120])
            run.event("context_loss", {"missing": missing,
                                       "plan_version": run.plan.version})

    def _run_subgoal(self, subgoal: Subgoal) -> str:
        run = self.run
        run.memory.add_subgoal(subgoal.subgoal_id, subgoal.description)
        reobserve_budget = 2
        for attempt in range(1, 4):
            if run.cancel.is_set():
                return "cancel"
            if run.pause.is_set():
                return "paused"
            if self._early_progress(subgoal) is True:
                return "continue"
            remaining = run.remaining()
            if remaining["dispatches"] <= 0 or remaining["seconds"] <= 0:
                subgoal.status = "failed"
                run.event("budget_exhausted", {"subgoal_id": subgoal.subgoal_id, **remaining})
                return "stop"
            subgoal.attempts = attempt
            # Reuse the runtime verification capture (or the one early_progress
            # just sampled). Prefer FAST; FULL only when a visual target needs it.
            observation = self._take_post_observation()
            needs_image = False
            for step in getattr(subgoal, "steps", None) or []:
                target = getattr(step, "target", None)
                if getattr(target, "visual", None) is not None:
                    needs_image = True
            if observation is None:
                observation = run.facade.observe(mode="FULL" if needs_image else "FAST")
            run.observations += 1
            run.memory.record_state(observation)
            self._compress_context_if_needed()
            run.context.observation_id = observation.get("observation_id")
            run.context.controller_epoch = run.facade.controller_epoch()
            run.context.candidate_set_hash = None
            run.context.last_observation = observation
            if observation.get("blocking_dialog"):
                run.store.set_state(run.task_id, "WAITING_USER")
                run.event("authentication_required",
                          {"subgoal_id": subgoal.subgoal_id,
                           "source": observation["blocking_dialog"].get("source")})
                return "stop"
            # `expected` keeps governing both the skip check and the dispatch
            # verification. `recovery_expected` travels separately: it is the
            # terminal goal condition recorded as reconciliation evidence, so a
            # step is never retried just because the goal is not reached yet.
            criteria = list(subgoal.expected)
            # An action with no explicit postcondition still declares the effect
            # the runtime will verify: the page must change after the dispatch.
            expected_predicates = criteria or [Predicate(id="__effect",
                                                         type="page_changed")]
            intent = GroundingIntent(
                action_kind=subgoal.action_kind,
                text=subgoal.intent_text,
                resource_id=subgoal.intent_resource_id,
                accessibility_id=subgoal.intent_accessibility_id,
                description=subgoal.description,
                require_clickable=subgoal.action_kind in ("tap", "long_press"),
            )
            try:
                candidate_set = run.registry.build(
                    task_id=run.task_id, subgoal_id=subgoal.subgoal_id,
                    scope_id=run.task.scope.device_ref, observation=observation,
                    controller_epoch=run.facade.controller_epoch(), intent=intent,
                    action_kind=subgoal.action_kind, arguments=run.arguments,
                    expected_predicates=expected_predicates,
                    argument_refs=subgoal.argument_refs,
                    ocr=run.ocr, matcher=run.matcher,
                )
            except GroundingUnavailable as error:
                run.event("grounding_unavailable",
                          {"subgoal_id": subgoal.subgoal_id, "code": error.code})
                if error.code == "stale_observation" and reobserve_budget > 0:
                    reobserve_budget -= 1
                    continue
                return self._block(subgoal, "grounding_unavailable")
            grounding = candidate_set.grounding
            run.context.candidate_set_hash = candidate_set.hash
            run.context.note_evidence(
                [ref for item in candidate_set.candidates for ref in item.candidate.evidence_refs])
            run.event("candidates", {
                "subgoal_id": subgoal.subgoal_id,
                "count": len(candidate_set.candidates),
                "hash": candidate_set.hash,
                "layers": grounding.layers_used if grounding else [],
                "rejected": grounding.rejected if grounding else [],
                "notes": grounding.notes if grounding else [],
            })
            if not candidate_set.candidates:
                if reobserve_budget > 0:
                    reobserve_budget -= 1
                    run.event("reobserve", {"subgoal_id": subgoal.subgoal_id,
                                            "reason": "no_candidate",
                                            "remaining": reobserve_budget})
                    continue
                run.event("subgoal_blocked", {"subgoal_id": subgoal.subgoal_id,
                                              "reason": "no_candidate_after_retry"})
                return self._block(subgoal, "no_candidate_after_retry")
            outcome = self._decide(subgoal, observation, candidate_set)
            run.event("decision", _decision_payload(outcome))
            if outcome.decision.route == "execute":
                selected = candidate_set.by_id(outcome.decision.selected_candidate_id or "")
                if selected is None:
                    run.event("subgoal_blocked", {"subgoal_id": subgoal.subgoal_id,
                                                  "reason": "selected_candidate_missing"})
                    return self._block(subgoal, "selected_candidate_missing")
                try:
                    result = self._dispatch(subgoal, observation, selected,
                                            expected_predicates, candidate_set)
                except Exception as error:
                    # A refused dispatch is a recoverable refusal, not a task
                    # failure: re-observe and let the guard decide again.
                    run.event("dispatch_refused",
                              {"subgoal_id": subgoal.subgoal_id,
                               "code": safety_code(error)})
                    if reobserve_budget > 0:
                        reobserve_budget -= 1
                        continue
                    return self._block(subgoal, "dispatch_refused")
                if result.get("execution_status") == "unknown":
                    # Do not publish the final state here: the state must become
                    # visible together with the result in `_finish()`. An early
                    # write would expose RECONCILIATION_REQUIRED with no result.
                    run.memory.record_incident(result.get("incident_id") or uuid.uuid4().hex,
                                               "execution_unknown", result.get("request_id"))
                    return "reconciliation"
                if result.get("verification_status") == "verified":
                    subgoal.status = "verified"
                    return "continue"
                reobserve_budget = max(reobserve_budget, 1)
                continue
            if outcome.decision.route == "reobserve":
                if reobserve_budget > 0:
                    reobserve_budget -= 1
                    continue
                return self._block(subgoal, "reobserve_budget_exhausted")
            return self._block(subgoal, "decision_not_executable")
        subgoal.status = "failed"
        run.event("subgoal_failed", {"subgoal_id": subgoal.subgoal_id,
                                     "attempts": subgoal.attempts})
        if self._actor_recovery(subgoal):
            return "continue"
        return "stop"

    def _block(self, subgoal: Subgoal, reason: str) -> str:
        """Mark a subgoal blocked, then give the actor one bounded replan chance.

        A blocked step is exactly when replanning is useful: no candidate was
        grounded, or the guard refused every attempt. The subgoal keeps its
        blocked status either way, so the record still shows what failed.
        """
        subgoal.status = "blocked"
        subgoal.blocked_reason = reason
        if self._actor_recovery(subgoal):
            return "continue"
        return "stop"

    def _actor_recovery(self, failed: Subgoal) -> bool:
        """Ask the mounted actor for one bounded recovery subgoal (P4-01).

        The actor sees a redacted request, never a facade, a candidate id or a
        coordinate. Its answer is validated by the intent contract and then runs
        through the ordinary loop, so the guard, epoch checks and budgets are
        unchanged. Any actor failure degrades to "no recovery": the deterministic
        outcome of the plan stands and the task reports it.
        """
        run = self.run
        if run.actor is None or run.actor_replans >= run.max_actor_replans:
            return False
        if run.remaining()["dispatches"] <= 0:
            return False
        observation = run.context.last_observation or {}
        request = ActorRequest(
            request_id=f"actor_{failed.subgoal_id}",
            task_id=run.task_id, goal=run.task.goal,
            constraints=list(run.plan.constraints),
            arguments=dict(run.arguments),
            allowed_apps=list(run.task.scope.allowed_apps),
            allowed_actions=list(run.task.scope.allowed_actions),
            observation=ActorObservation(
                observation_id=str(observation.get("observation_id") or "unknown"),
                controller_epoch=int(run.context.controller_epoch or 0),
                foreground_bundle=observation.get("foreground_bundle"),
                fingerprint=observation.get("fingerprint"),
                # Runtime reports `screen_state`; keep `screen` as a fallback
                # for older observation shapes. Never invent lock/on values.
                screen=dict(observation.get("screen_state")
                            or observation.get("screen") or {}),
                facts=list(run.memory.context_facts())[:16],
                recent_outcomes=[failed.description, failed.status],
                blocking_dialog=(observation.get("blocking_dialog") or {}).get("source")
                if isinstance(observation.get("blocking_dialog"), dict) else None),
            plan_version=run.plan.version,
            pending_subgoal_ids=[item.subgoal_id for item in run.plan.subgoals],
            budget={"dispatches": run.remaining()["dispatches"],
                    "seconds": run.remaining()["seconds"],
                    "model_calls": run.remaining()["model_calls"]},
            frozen_memory_hash=getattr(run.frozen_memory, "manifest_hash", None),
            memory_facts=self._facts()[:16])
        try:
            proposal = asyncio.run(run.actor.propose(request))
        except Exception as error:
            run.event("actor_unavailable", {"code": safety_code(error),
                                            "subgoal_id": failed.subgoal_id})
            return False
        if isinstance(proposal, ControlProposal):
            run.event("actor_control", {"control": proposal.control,
                                        "reason_code": proposal.reason_code,
                                        "subgoal_id": failed.subgoal_id})
            return False
        if not isinstance(proposal, SubgoalProposal):
            run.event("actor_proposal_rejected", {"code": "unsupported_proposal",
                                                  "subgoal_id": failed.subgoal_id})
            return False
        try:
            subgoal = subgoal_from_proposal(run.task_id, proposal)
        except ActorUnavailable as error:
            run.event("actor_proposal_rejected", {"code": error.code,
                                                 "subgoal_id": failed.subgoal_id})
            return False
        if any(item.subgoal_id == subgoal.subgoal_id for item in run.plan.subgoals):
            run.event("actor_proposal_rejected", {"code": "duplicate_subgoal",
                                                  "subgoal_id": subgoal.subgoal_id})
            return False
        updated = replan(run.plan, failed.subgoal_id, reason="actor_recovery")
        updated.subgoals = [*updated.subgoals, subgoal]
        run.plan = updated
        run.actor_replans += 1
        run.store.add_plan(run.task_id, run.plan)
        run.event("actor_recovery", {
            "subgoal_id": subgoal.subgoal_id,
            "after": failed.subgoal_id,
            "plan_version": run.plan.version,
            "actor": getattr(run.actor, "name", "actor"),
            "revision": getattr(run.actor, "revision", None),
            "remaining_replans": run.max_actor_replans - run.actor_replans})
        return True

    def _early_progress(self, subgoal: Subgoal) -> bool | None:
        """Skip a subgoal whose conditions already hold; never dispatches."""
        criteria = list(subgoal.expected)
        if not criteria:
            return None
        if self.run.observations > 0 and subgoal.attempts > 0:
            return None
        reused = self._take_post_observation()
        if reused is not None:
            report = self.run.check_conditions(criteria, reused).report
            if report.verdict == "pass":
                subgoal.status = "verified"
                self.run.event("subgoal_already_satisfied",
                               {"subgoal_id": subgoal.subgoal_id,
                                "evidence": report.evidence_refs[:4],
                                "post_observation_reused": True})
                self.run.context.note_evidence(report.evidence_refs)
                return True
            # The freshest capture says the step is still needed. Keep it for
            # the attempt below instead of discarding a live device fact.
            self.run.context.post_observation = reused
            return False
        try:
            observation = self.run.facade.observe(mode="FAST")
        except Exception:
            return None
        self.run.observations += 1
        self.run.context.observation_id = observation.get("observation_id")
        self.run.context.controller_epoch = self.run.facade.controller_epoch()
        report = self.run.check_conditions(criteria, observation).report
        if report.verdict == "pass":
            subgoal.status = "verified"
            self.run.event("subgoal_already_satisfied",
                           {"subgoal_id": subgoal.subgoal_id,
                            "evidence": report.evidence_refs[:4]})
            self.run.context.note_evidence(report.evidence_refs)
            return True
        # Hand the same capture to the attempt below: a failed skip-check must
        # not buy a second read of the unchanged page.
        self.run.context.post_observation = observation
        return False

    def _take_post_observation(self) -> dict[str, Any] | None:
        """Consume the runtime's last verification capture, if it is usable."""
        observation = self.run.context.post_observation
        self.run.context.post_observation = None
        if not isinstance(observation, dict) or observation.get("actionable") is not True:
            return None
        if not observation.get("observation_id"):
            return None
        return observation

    # -- decision -----------------------------------------------------------
    def _decide(self, subgoal: Subgoal, observation: dict[str, Any],
                candidate_set) -> RouterOutcome:
        run = self.run
        outcome = asyncio.run(run.router.decide(
            task_id=run.task_id, subgoal_id=subgoal.subgoal_id,
            scope_id=run.task.scope.device_ref, observation=observation,
            candidate_set=candidate_set, controller_epoch=run.facade.controller_epoch(),
            remaining_model_calls=run.remaining()["model_calls"],
            goal=run.task.goal, facts=self._facts()))
        # Charge the budget for every provider invocation the router issued,
        # including attempts that timed out, crashed or returned garbage and
        # therefore fell back to rules. Inferring this from the *final* decision
        # provider would let a failing provider be called for free.
        run.model_calls += outcome.provider_calls
        run.context.model_revision = outcome.decision.model_revision
        run.context.calibration_version = outcome.decision.calibration_version
        run.context.decision_provider = outcome.decision.provider
        run.context.note_evidence(outcome.decision.native_scores.model_dump().get("evidence_refs"))
        return outcome

    def _facts(self) -> list[str]:
        """Facts from the in-task memory plus a frozen learning snapshot, if any."""
        facts = list(self.run.memory.context_facts())
        frozen = self.run.frozen_memory
        if frozen is not None:
            facts.extend(frozen.facts(limit=8))
        return facts

    # -- dispatch -----------------------------------------------------------
    def _dispatch(self, subgoal: Subgoal, observation: dict[str, Any],
                  selected: RegisteredCandidate, criteria: list[Predicate],
                  candidate_set) -> dict[str, Any]:
        run = self.run
        epoch = run.facade.controller_epoch()
        if epoch != selected.candidate.controller_epoch:
            run.event("late_candidate_rejected", {
                "subgoal_id": subgoal.subgoal_id,
                "candidate_epoch": selected.candidate.controller_epoch,
                "current_epoch": epoch})
            return {"status": "epoch_mismatch", "execution_status": "not_dispatched",
                    "verification_status": "inconclusive"}
        # Gateway authority: the selected candidate must still be one this
        # registry issued for *this* observation and epoch, and must not have
        # expired between the decision and the dispatch. Membership is checked
        # against the registry itself, not against the candidate set, so pruning
        # or a foreign candidate set cannot authorise a write. A failure is a
        # refusal, never a re-issued candidate that continues the click.
        try:
            run.registry.resolve(selected.candidate.candidate_id,
                                 observation_id=observation["observation_id"],
                                 controller_epoch=epoch)
        except CandidateError as error:
            run.event("candidate_rejected", {
                "subgoal_id": subgoal.subgoal_id,
                "code": error.code,
                "candidate_id": selected.candidate.candidate_id})
            return {"status": error.code, "execution_status": "not_dispatched",
                    "verification_status": "inconclusive"}
        action = _action_payload(subgoal, selected)
        expected = _expected_payload(criteria, run.arguments)
        request_id = "act_" + uuid.uuid4().hex[:24]
        try:
            guarded = _guarded_action(
                run=run, subgoal=subgoal, observation=observation,
                selected=selected, criteria=criteria, action=action,
                candidate_set=candidate_set, request_id=request_id, epoch=epoch)
        except (ProtocolError, ValueError) as error:
            code = getattr(error, "code", None) or "guarded_action_invalid"
            run.event("guarded_action_rejected", {
                "subgoal_id": subgoal.subgoal_id,
                "code": code,
                "detail": str(error)[:200]})
            return {"status": code, "execution_status": "not_dispatched",
                    "verification_status": "inconclusive"}
        # The runtime re-derives the epoch and rejects unknown fields, so the
        # wire payload keeps the v1 shape; the epoch lives in the envelope.
        payload = guarded.model_dump(include={"session_id", "request_id",
                                              "observation_id"})
        payload.update({"action": action, "expected": expected, "timeout_ms": 8000})
        recovery = _expected_payload(list(subgoal.recovery_expected), run.arguments)
        if recovery is not None:
            payload["recovery"] = recovery
        signature = f"{action['kind']}:{selected.candidate.target_ref}"
        if run.loop.observe(signature, observation.get("fingerprint", ""),
                            is_scroll=subgoal.action_kind == "swipe"):
            run.event("loop_detected", {"subgoal_id": subgoal.subgoal_id,
                                        "signature_hash": _digest(signature)[:16]})
            return {"status": "loop_detected", "execution_status": "not_dispatched",
                    "verification_status": "inconclusive"}
        run.dispatches += 1
        run.context.candidate_set_hash = guarded.candidate_set_hash
        run.context.observation_id = guarded.observation_id
        run.context.controller_epoch = guarded.controller_epoch
        run.context.note_evidence(guarded.evidence_refs)
        result = run.facade.act(payload)
        # Hold the runtime's own verification capture for the next step.
        run.context.post_observation = post_observation(result)
        run.event("dispatch", {
            "subgoal_id": subgoal.subgoal_id,
            "request_id": request_id,
            "candidate_id": selected.candidate.candidate_id,
            "target_ref": selected.candidate.target_ref,
            "action_kind": action["kind"],
            "risk_class": selected.candidate.risk_class,
            "candidate_set_hash": guarded.candidate_set_hash,
            "controller_epoch": guarded.controller_epoch,
            "protocol_version": guarded.protocol_version,
            "guarded_action_hash": _digest(canonical(guarded.model_dump())),
            "recovery_condition": bool(recovery),
            "execution_status": result.get("execution_status"),
            "verification_status": result.get("verification_status"),
            "status": result.get("status"),
            "post_observation_reused": run.context.post_observation is not None,
        })
        if run.artifacts is not None and result.get("observation"):
            try:
                artifact = run.artifacts.put_json(
                    {"observation_id": result["observation"].get("observation_id"),
                     "fingerprint": result["observation"].get("fingerprint"),
                     "foreground": result["observation"].get("foreground_bundle"),
                     "catalog_size": len(result["observation"].get("catalog", []))},
                    kind="event", task_id=run.task_id, sensitivity="metadata")
                run.event("evidence", {"artifact_id": artifact["artifact_id"],
                                       "sha256": artifact["sha256"]})
            except Exception as error:  # storage failure must not fake success
                run.event("evidence_incomplete", {"code": safety_code(error)})
        return result

    # -- verification -------------------------------------------------------
    def _verify(self) -> dict[str, Any]:
        run = self.run
        run.store.set_state(run.task_id, "VERIFYING")
        report = CheckReport(verdict="inconclusive", conditions=[])
        verification: VerificationOutcome | None = None
        try:
            observation = run.facade.observe(mode="FULL", include_image=False)
            run.observations += 1
            run.memory.record_state(observation)
            run.context.observation_id = observation.get("observation_id")
            run.context.controller_epoch = run.facade.controller_epoch()
            verification = run.check_conditions(run.task.success_criteria, observation)
            report = verification.report
            run.context.note_evidence(report.evidence_refs)
            run.event("verification", verification.to_dict())
        except Exception as error:
            run.event("verify_failed", {"code": safety_code(error)})
        if report.verdict == "pass":
            return self._finish("SUCCEEDED", "all_conditions_verified", report,
                                verification)
        if report.verdict == "fail":
            return self._finish("FAILED", "condition_failed", report, verification)
        return self._finish("PARTIAL", "conditions_inconclusive", report, verification)

    # -- terminal -----------------------------------------------------------
    def _paused(self) -> dict[str, Any]:
        run = self.run
        run.store.set_state(run.task_id, "PAUSED")
        run.event("task_paused", {"reason": "supervisor_request"})
        return {"task_id": run.task_id, "status": "PAUSED",
                "usage": {"dispatches": run.dispatches, "model_calls": run.model_calls,
                          "observations": run.observations},
                "events": run.events}

    def _finish(self, state: str, reason: str,
                report: CheckReport | None = None,
                verification: VerificationOutcome | None = None) -> dict[str, Any]:
        run = self.run
        if state == "FAILED":
            # A subgoal that never verified used to finish with an empty condition
            # list, which hides *which* criterion was unmet. One read-only check
            # fills that in; it never dispatches.
            report = self._failure_report(report)
        unresolved = run.facade.unresolved()
        verification = verification or run.last_verification
        result = {
            "task_id": run.task_id,
            "status": state,
            "reason": reason,
            "goal_hash": _digest(run.task.goal),
            "plan": run.plan.to_dict(),
            "conditions": [item.to_dict() for item in (report.conditions if report else [])],
            "unobserved_conditions": list(report.unobserved) if report else [],
            "unresolved_actions": unresolved,
            "resolution_required": bool(unresolved),
            "usage": {"dispatches": run.dispatches, "model_calls": run.model_calls,
                      "observations": run.observations,
                      "elapsed_seconds": round(time.time() - run.started_at, 3)},
            "memory": run.memory.snapshot(),
            "limitations": list(dict.fromkeys(
                [*(report.limitations if report else []), *run.limitations])),
            "verification": {
                "verifier": verification.verifier,
                "revision": verification.revision,
                "verdict": verification.verdict,
                "evidence_refs": list(verification.evidence_refs),
            } if verification else None,
            "protocol_version": PROTOCOL_VERSION,
        }
        if run.artifacts is not None:
            try:
                artifact = run.artifacts.put_json(
                    {key: value for key, value in result.items() if key != "memory"},
                    kind="task_result", task_id=run.task_id)
                result["result_artifact_id"] = artifact["artifact_id"]
            except Exception as error:
                result["limitations"] = [*result["limitations"],
                                         f"artifact_store_error:{safety_code(error)}"]
        # State, result and the closing event become visible together: a poller
        # that sees a result-bearing state always sees the matching result.
        published = run.store.finalize_task(
            run.task_id, state, result,
            event=("task_finished",
                   {"status": state, "reason": reason,
                    "resolution_required": result["resolution_required"],
                    "result_artifact_id": result.get("result_artifact_id")}),
            envelope=EventEnvelope(
                event_type="task_finished", task_id=run.task_id, sequence=0,
                created=time.time(),
                observation_id=run.context.observation_id,
                candidate_set_hash=run.context.candidate_set_hash,
                controller_epoch=run.context.controller_epoch,
                model_revision=run.context.model_revision,
                calibration_version=run.context.calibration_version,
                evidence_refs=list(run.context.evidence_refs),
                payload={"status": state, "reason": reason},
            ).model_dump())
        if published:
            run.events += 1
        return result

    def _failure_report(self, previous: CheckReport | None) -> CheckReport | None:
        if previous is not None and previous.conditions:
            return previous
        try:
            observation = self.run.facade.observe(mode="FAST")
            self.run.observations += 1
            outcome = self.run.check_conditions(self.run.task.success_criteria, observation)
            self.run.event("failure_conditions", {"verdict": outcome.verdict})
            return outcome.report
        except Exception as error:
            self.run.event("failure_conditions_unavailable", {"code": safety_code(error)})
            return previous


class TaskSupervisor:
    """Submission, control, status and result queries for tasks."""

    def __init__(self, store: TaskStore, runner_factory=None):
        self.store = store
        self.runner_factory = runner_factory
        self.runs: dict[str, TaskRun] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.RLock()

    def submit(self, payload: dict[str, Any], *, controller_epoch: int,
               session_id: str) -> dict[str, Any]:
        task = TaskSubmit.model_validate(payload)
        created = self.store.create(task, controller_epoch)
        task_id = created["task_id"]
        return {**created, "accepted": True,
                "status": self.store.state(task_id),
                "capability_version": "2.0", "session_id": session_id}

    def start(self, task_id: str, run: TaskRun) -> None:
        with self.lock:
            self.runs[task_id] = run
            self.store.add_plan(task_id, run.plan)
            thread = threading.Thread(target=self._execute, args=(run,),
                                      name=f"task-{task_id[-8:]}", daemon=True)
            self.threads[task_id] = thread
            thread.start()

    def _execute(self, run: TaskRun) -> None:
        try:
            TaskRunner(run).execute()
        finally:
            with self.lock:
                self.threads.pop(run.task_id, None)

    def status(self, task_id: str, after_event_seq: int = 0) -> dict[str, Any]:
        record = self.store.task(task_id)
        if record is None:
            raise TaskError("unknown_task", "No such task")
        events = self.store.events(task_id, after=after_event_seq, limit=1)
        run = self.runs.get(task_id)
        pending = run.plan.next_pending() if run is not None else None
        return {
            "task_id": task_id,
            "status": record["state"],
            "controller_epoch": record["controller_epoch"],
            "created": record["created"],
            "updated": record["updated"],
            "usage": ({**run.remaining(), "dispatches_used": run.dispatches,
                       "model_calls_used": run.model_calls,
                       "observations": run.observations}
                      if run else None),
            "next_event_seq": events["last_seq"],
            "current_subgoal": ({"subgoal_id": pending.subgoal_id,
                                 "description": pending.description,
                                 "status": pending.status,
                                 "attempts": pending.attempts,
                                 "plan_version": run.plan.version}
                                if pending is not None else None),
            "terminal": record["state"] in TERMINAL,
            "result_available": record["result"] is not None,
        }

    def control(self, task_id: str, operation: str,
                control_request_id: str | None = None) -> dict[str, Any]:
        if operation not in ("pause", "cancel", "resume"):
            raise TaskError("invalid_arguments", "operation must be pause, cancel or resume")
        record = self.store.task(task_id)
        if record is None:
            raise TaskError("unknown_task", "No such task")
        state = record["state"]
        if state in TERMINAL and operation != "resume":
            return {"task_id": task_id, "operation": operation, "status": state,
                    "applied": False, "message": "Task is already terminal"}
        with self.lock:
            run = self.runs.get(task_id)
            if operation == "pause":
                if run:
                    run.pause.set()
                else:
                    self.store.set_state(task_id, "PAUSED")
                self.store.add_event(task_id, "control_pause",
                                     {"control_request_id": control_request_id})
                return {"task_id": task_id, "operation": "pause", "applied": True,
                        "status": self.store.state(task_id),
                        "message": "New dispatches are refused; in-flight results drain conservatively"}
            if operation == "cancel":
                if run:
                    run.cancel.set()
                self.store.set_state(task_id, "CANCELLING")
                self.store.add_event(task_id, "control_cancel",
                                     {"control_request_id": control_request_id})
                return {"task_id": task_id, "operation": "cancel", "applied": True,
                        "status": self.store.state(task_id),
                        "message": "Cancellation cannot retract input the phone already received"}
            if state not in ("PAUSED", "WAITING_USER"):
                return {"task_id": task_id, "operation": "resume", "applied": False,
                        "status": state, "message": "resume requires PAUSED or WAITING_USER"}
            if run is None:
                return {"task_id": task_id, "operation": "resume", "applied": False,
                        "status": state,
                        "message": "No live runner; start a new task with a fresh observation"}
            run.pause.clear()
            self.store.set_state(task_id, "RUNNING")
            self.store.add_event(task_id, "control_resume",
                                 {"control_request_id": control_request_id})
            return {"task_id": task_id, "operation": "resume", "applied": True,
                    "status": "RUNNING",
                    "message": "Resuming requires a fresh observation and epoch check"}

    def events(self, task_id: str, after: int = 0, limit: int = 50) -> dict[str, Any]:
        if self.store.task(task_id) is None:
            raise TaskError("unknown_task", "No such task")
        return self.store.events(task_id, after=after, limit=limit)

    def result(self, task_id: str) -> dict[str, Any]:
        record = self.store.task(task_id)
        if record is None:
            raise TaskError("unknown_task", "No such task")
        if record["result"] is None:
            return {"task_id": task_id, "status": record["state"], "available": False,
                    "message": "No final result yet; query status and events"}
        return {"task_id": task_id, "status": record["state"], "available": True,
                "result": record["result"], "plan_versions": self.store.plans(task_id)}

    def close(self) -> None:
        with self.lock:
            for run in self.runs.values():
                run.cancel.set()
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=30)


# -- helpers ----------------------------------------------------------------

ACTION_NEEDS_TARGET = ("tap", "long_press", "input_text", "replace_text")


def _guarded_action(*, run: TaskRun, subgoal: Subgoal, observation: dict[str, Any],
                    selected: RegisteredCandidate, criteria: list[Predicate],
                    action: dict[str, Any], candidate_set,
                    request_id: str, epoch: int) -> GuardedAction:
    """Build the frozen dispatch envelope, or raise before any device call.

    The envelope is validated by its own schema: a target action without a
    registry-issued target, a coordinate-bearing argument or a high-risk action
    without a runtime-issued authorization cannot be constructed at all.
    """
    evidence = next(
        (list(item.candidate.evidence_refs) for item in candidate_set.candidates
         if item.candidate.candidate_id == selected.candidate.candidate_id), [])
    argument_values = {key: action[key] for key in ("text", "direction", "bundle")
                       if key in action}
    return GuardedAction(
        session_id=run.facade.session_id,
        request_id=request_id,
        task_id=run.task_id,
        subgoal_id=subgoal.subgoal_id,
        observation_id=observation["observation_id"],
        controller_epoch=epoch,
        candidate_set_hash=candidate_set.hash,
        candidate_id=selected.candidate.candidate_id,
        target_ref=selected.candidate.target_ref,
        local_fingerprint=(selected.target.local_fingerprint or None),
        action_kind=subgoal.action_kind,
        argument_values=argument_values,
        expected=list(criteria),
        recovery=list(subgoal.recovery_expected),
        risk_class=selected.candidate.risk_class,
        authorization_ref=None,
        evidence_refs=evidence,
    )


def _action_payload(subgoal: Subgoal, selected: RegisteredCandidate) -> dict[str, Any]:
    target = selected.target
    action: dict[str, Any] = {"kind": subgoal.action_kind}
    if subgoal.action_kind in ACTION_NEEDS_TARGET:
        target_payload = {
            "target_ref": target.target_ref,
            "observation_id": target.observation_id,
            "local_fingerprint": target.local_fingerprint,
        }
        # A visual proposal travels with its region digest; the runtime still
        # revalidates it against the pre-dispatch image before dispatching.
        visual = target.visual_ref()
        if visual is not None:
            target_payload["visual"] = visual
        action["target"] = target_payload
        if subgoal.action_kind in ("input_text", "replace_text"):
            value = selected.argument_values.get("text")
            if value is None:
                raise TaskError("missing_argument",
                                "Input action requires a resolved text argument")
            action["text"] = value
    elif subgoal.action_kind == "swipe":
        action["direction"] = subgoal.argument_refs.get("direction", "down")
    elif subgoal.action_kind == "launch":
        bundle = subgoal.argument_refs.get("bundle")
        if not bundle:
            raise TaskError("missing_argument", "launch requires a bundle")
        action["bundle"] = bundle
    return action


def _expected_payload(criteria: list[Predicate], arguments: dict[str, str]) -> dict[str, Any] | None:
    """Map a v2 predicate onto the v1 expected postcondition when possible."""
    for predicate in criteria:
        if predicate.type == "text_equals":
            return {"text": _value(predicate, arguments)}
        if predicate.type == "foreground_is":
            return {"bundle": _value(predicate, arguments)}
        if predicate.type == "page_changed":
            return {"changed": True}
    if criteria:
        return {"changed": True}
    return None


def _value(predicate: Predicate, arguments: dict[str, str]) -> str:
    if predicate.value_ref:
        key = predicate.value_ref[4:] if predicate.value_ref.startswith("arg.") else predicate.value_ref
        return arguments[key]
    return str(predicate.value)


def _decision_payload(outcome: RouterOutcome) -> dict[str, Any]:
    native = outcome.decision.native_scores.model_dump()
    payload = {
        "route": outcome.decision.route,
        "provider": outcome.decision.provider,
        "reason_code": outcome.decision.reason_code,
        "selected_candidate_id": outcome.decision.selected_candidate_id,
        "elapsed_ms": outcome.decision.elapsed_ms,
        "candidate_set_hash": outcome.decision.candidate_set_hash,
        "controller_epoch": outcome.decision.controller_epoch,
        "model_revision": outcome.decision.model_revision,
        "calibration_version": outcome.decision.calibration_version,
        "native_scores": native,
    }
    if outcome.shadow is not None:
        shadow_native = outcome.shadow.native_scores.model_dump()
        shadow_answer = shadow_native.get("answers", {}).get("action", {})
        payload["shadow"] = {
            "provider": outcome.shadow.provider,
            "suggestion": shadow_answer.get("choice"),
            "confidence": shadow_answer.get("confidence"),
            "certainty": shadow_answer.get("certainty"),
            "route": outcome.shadow.route,
            "reason_code": outcome.shadow.reason_code,
            "elapsed_ms": outcome.shadow.elapsed_ms,
            "calibration_version": outcome.shadow.calibration_version,
        }
    if outcome.fallback_reason:
        payload["fallback_reason"] = outcome.fallback_reason
    return payload
