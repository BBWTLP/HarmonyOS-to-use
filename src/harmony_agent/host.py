"""Service-side agent host: tasks, decisions and artifacts over the runtime.

The host is owned by the runtime service process. It never constructs a device
driver; it only calls the public Runtime session API, so the guard, epoch and
journal rules of the deterministic path stay in force.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, Retention
from .candidates import CandidateRegistry
from .checker import ReadOnlyChecker
from .contracts import Predicate, TaskSubmit
from .decision.providers.decider import DEFAULT_TOKEN_FILE, CircuitBreaker, DeciderProvider
from .decision.router import PROFILES, Router
from .grounding import GroundingIntent
from .memory import Memory
from .planner import plan_from_task
from .supervisor import (RuntimeFacade, TaskError, TaskRun, TaskStore, TaskSupervisor,
                         safety_code)


class AgentHost:
    def __init__(self, runtime, root: str | Path, *, profile: str = "local_shadow",
                 calibration_version: str | None = None,
                 decider: DeciderProvider | None = None,
                 confidence_threshold: float = 0.0,
                 certainty_threshold: float = 0.0,
                 ocr=None, matcher=None, retention: Retention | None = None,
                 repo_root: str | Path | None = None):
        self.runtime = runtime
        self.root = Path(root)
        self.profile = profile if profile in PROFILES else "local_shadow"
        self.calibration_version = calibration_version
        self.confidence_threshold = confidence_threshold
        self.certainty_threshold = certainty_threshold
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()
        token_file = self.repo_root / DEFAULT_TOKEN_FILE
        self.decider = decider or DeciderProvider(token_file=token_file)
        self.ocr = ocr
        self.matcher = matcher
        self.store = TaskStore(self.root / "agent-tasks.sqlite3")
        self.artifacts = ArtifactStore(self.root / "artifacts", retention)
        self.supervisor = TaskSupervisor(self.store)
        self.registry = CandidateRegistry()
        self.checker = ReadOnlyChecker()
        self.started_at = time.time()

    # -- router ------------------------------------------------------------
    def _router(self) -> Router:
        return Router(decider=self.decider, profile=self.profile,
                      calibration_version=self.calibration_version,
                      confidence_threshold=self.confidence_threshold,
                      certainty_threshold=self.certainty_threshold)

    # -- tasks -------------------------------------------------------------
    def run_task(self, owner: str, *, session_id: str, task: dict[str, Any]) -> dict[str, Any]:
        payload = task.get("task") if isinstance(task.get("task"), dict) else task
        model = TaskSubmit.model_validate(payload)
        scope_epoch = self.runtime.session(owner, "status", session_id=session_id)["controller_epoch"]
        created = self.supervisor.submit(model.model_dump(), controller_epoch=scope_epoch,
                                         session_id=session_id)
        if created["deduplicated"]:
            return created
        plan = plan_from_task(model)
        run = TaskRun(
            task_id=created["task_id"], task=model, plan=plan,
            memory=Memory(goal=model.goal,
                          constraints=[f"allowed_apps={model.scope.allowed_apps}",
                                       f"allowed_actions={model.scope.allowed_actions}"]),
            facade=RuntimeFacade(self.runtime, owner, session_id),
            registry=self.registry, router=self._router(), checker=self.checker,
            store=self.store, artifacts=self.artifacts, ocr=self.ocr, matcher=self.matcher,
            arguments=dict(plan.parameters),
        )
        self.supervisor.start(created["task_id"], run)
        return created

    def task_status(self, owner: str, *, task_id: str, after_event_seq: int = 0) -> dict[str, Any]:
        return self.supervisor.status(task_id, after_event_seq)

    def task_control(self, owner: str, *, task_id: str, operation: str,
                     control_request_id: str | None = None) -> dict[str, Any]:
        return self.supervisor.control(task_id, operation, control_request_id)

    def task_events(self, owner: str, *, task_id: str, after_seq: int = 0,
                    limit: int = 50) -> dict[str, Any]:
        return self.supervisor.events(task_id, after=after_seq, limit=limit)

    def task_result(self, owner: str, *, task_id: str) -> dict[str, Any]:
        return self.supervisor.result(task_id)

    # -- decisions ---------------------------------------------------------
    def decide(self, owner: str, *, session_id: str, observation_id: str,
               intent: dict[str, Any], goal: str = "",
               propositions: dict[str, str] | None = None) -> dict[str, Any]:
        """Advisory decision on a fresh observation. Never dispatches."""
        observation = self._cached_observation(owner, session_id, observation_id)
        grounding_intent = GroundingIntent(
            action_kind=intent.get("action_kind", "tap"),
            text=intent.get("text"),
            resource_id=intent.get("resource_id"),
            accessibility_id=intent.get("accessibility_id"),
            description=intent.get("description"),
            require_clickable=intent.get("action_kind", "tap") in ("tap", "long_press"),
        )
        arguments = {key: str(value) for key, value in (intent.get("arguments") or {}).items()}
        expected = [Predicate.model_validate(item) for item in (intent.get("expected") or [])]
        epoch = self.runtime.session(owner, "status", session_id=session_id)["controller_epoch"]
        candidate_set = self.registry.build(
            task_id=intent.get("task_id", "advisory"),
            subgoal_id=intent.get("subgoal_id", "advisory"),
            scope_id=intent.get("scope_id", "advisory"),
            observation=observation, controller_epoch=epoch, intent=grounding_intent,
            action_kind=grounding_intent.action_kind, arguments=arguments,
            expected_predicates=expected or [_placeholder_predicate(observation)],
            argument_refs=dict(intent.get("argument_refs") or {}),
            ocr=self.ocr, matcher=self.matcher,
        )
        outcome = asyncio.run(self._router().decide(
            task_id=intent.get("task_id", "advisory"),
            subgoal_id=intent.get("subgoal_id", "advisory"),
            scope_id=intent.get("scope_id", "advisory"),
            observation=observation, candidate_set=candidate_set,
            controller_epoch=epoch, goal=goal, propositions=propositions))
        return {
            "observation_id": observation["observation_id"],
            "controller_epoch": epoch,
            "candidate_set_hash": candidate_set.hash,
            "candidate_count": len(candidate_set.candidates),
            "candidates": [{"candidate_id": item.candidate.candidate_id,
                            "target_ref": item.candidate.target_ref,
                            "description": item.description,
                            "risk_class": item.candidate.risk_class,
                            "expires_at": item.candidate.expires_at}
                           for item in candidate_set.candidates],
            "route": outcome.decision.route,
            "provider": outcome.decision.provider,
            "reason_code": outcome.decision.reason_code,
            "selected_candidate_id": outcome.decision.selected_candidate_id,
            # Serialise model output: a live pydantic object here would make the
            # service's JSON response invalid and the client would lose it.
            "native_scores": outcome.decision.native_scores.model_dump(),
            "shadow": None if outcome.shadow is None else {
                "suggestion": _shadow_answer(outcome.shadow, "choice"),
                "confidence": _shadow_answer(outcome.shadow, "confidence"),
                "certainty": _shadow_answer(outcome.shadow, "certainty"),
                "provider": outcome.shadow.provider,
                "route": outcome.shadow.route,
                "reason_code": outcome.shadow.reason_code,
            },
            "fallback_reason": outcome.fallback_reason,
            "profile": self.profile,
            "calibration_version": self.calibration_version,
            "dispatch_permitted": False,
        }

    def _cached_observation(self, owner: str, session_id: str, observation_id: str) -> dict[str, Any]:
        # The runtime keeps the authoritative cache; a fresh observe is the only
        # way to obtain a handle, so an unknown id is an explicit error.
        cached = self.runtime.cached_observation(owner, session_id, observation_id)
        if cached is None:
            from harmony_runtime.contracts import RuntimeFault
            raise RuntimeFault(
                "stale_observation",
                "Observe again; advisory decisions need a fresh observation handle")
        return cached

    # -- maintenance -------------------------------------------------------
    def artifacts_index(self, owner: str, *, task_id: str | None = None,
                        limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self.artifacts.index(task_id=task_id, limit=limit, offset=offset)

    def artifacts_sweep(self, owner: str, *, dry_run: bool = True) -> dict[str, Any]:
        return self.artifacts.sweep(dry_run=dry_run)

    def artifacts_export(self, owner: str, *, task_id: str) -> dict[str, Any]:
        return self.artifacts.redacted_export(task_id)

    def diagnostics(self, owner: str) -> dict[str, Any]:
        return {"profile": self.profile,
                "calibration_version": self.calibration_version,
                "decider_revision": getattr(self.decider, "revision", None),
                "circuit_open": bool(getattr(getattr(self.decider, "breaker", None), "is_open", False)),
                "inflight_tasks": self.store.inflight(),
                "artifact_quota": self.artifacts.quota(),
                "uptime_seconds": round(time.time() - self.started_at, 3)}

    def close(self) -> None:
        self.supervisor.close()
        self.store.close()
        self.artifacts.close()


def host_from_env(runtime, root: str | Path, repo_root: str | Path) -> AgentHost | None:
    """Build a host only when the agent tools are explicitly enabled."""
    import os
    if os.environ.get("HARMONY_AGENT_TOOLS") not in ("1", "true", "yes"):
        return None
    profile = os.environ.get("HARMONY_AGENT_PROFILE", "local_shadow")
    return AgentHost(runtime, root, profile=profile,
                     calibration_version=os.environ.get("HARMONY_AGENT_CALIBRATION") or None,
                     confidence_threshold=float(os.environ.get("HARMONY_AGENT_MIN_CONFIDENCE", "0")),
                     certainty_threshold=float(os.environ.get("HARMONY_AGENT_MIN_CERTAINTY", "0")),
                     repo_root=repo_root)


def _placeholder_predicate(observation: dict[str, Any]) -> Predicate:
    """Advisory decisions need a postcondition slot; use the current foreground."""
    return Predicate(id="advisory", type="foreground_is",
                     value=str(observation.get("foreground_bundle") or "unknown"))


def _shadow_answer(shadow, field: str):
    """Read one field from the shadow provider's native answer."""
    native = shadow.native_scores.model_dump()
    return native.get("answers", {}).get("action", {}).get(field)
