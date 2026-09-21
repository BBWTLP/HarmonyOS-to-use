"""The experiment report every batch must publish (plan §8.3).

A single schema so a wave, an M1 batch or a 300-run acceptance prints the same
shape: identity of the code/device/task-set/memory/model, the failure
denominators, latency percentiles, safety violations and whether the artifacts
were redacted. Reporting only successes is not a valid report.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterable

from pydantic import Field, model_validator

from .contracts import AgentContract


def code_revision(repo_root: str | Path | None = None) -> str:
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                   capture_output=True, text=True, check=True)
    except Exception:  # pragma: no cover - a non-git checkout
        return "unknown"
    return completed.stdout.strip()


def nearest_rank_percentiles(values: Iterable[float]) -> dict[str, float | None]:
    """Nearest-rank P50/P95, with no interpolation invented by the reporter."""
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return {"p50_ms": None, "p95_ms": None}
    def rank(fraction: float) -> float:
        index = max(1, int(round(fraction * len(ordered)))) - 1
        return round(ordered[min(index, len(ordered) - 1)], 3)
    return {"p50_ms": rank(0.5), "p95_ms": rank(0.95)}


class ExperimentReport(AgentContract):
    schema_version: int = 1
    run_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(default="batch", max_length=64)
    code_revision: str = Field(min_length=1, max_length=128)
    device_baseline_hash: str = Field(default="", max_length=128)
    task_set_hash: str = Field(default="", max_length=128)
    memory_manifest_hash: str = Field(default="", max_length=128)
    model_revision: str = Field(default="", max_length=128)
    attempted: int = Field(ge=0)
    success: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    unknown: int = Field(ge=0)
    p50_ms: float | None = Field(default=None, ge=0)
    p95_ms: float | None = Field(default=None, ge=0)
    safety_violations: int = Field(ge=0)
    artifacts_redacted: bool
    notes: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def denominators(self):
        outcomes = self.success + self.failed + self.blocked + self.unknown
        if outcomes > self.attempted:
            raise ValueError("outcome counts exceed the attempted denominator")
        if self.p50_ms is not None and self.p95_ms is not None and self.p95_ms < self.p50_ms:
            raise ValueError("p95 must not be below p50")
        return self


#: Outcome kinds a batch counts. Anything else must be mapped by the caller.
OUTCOME_FIELDS = {"verified": "success", "fragile_pass": "success",
                  "failed": "failed", "inconclusive": "blocked",
                  "execution_unknown": "unknown", "infrastructure_error": "blocked",
                  "blocked_user": "blocked"}


def report_from_outcomes(*, run_id: str, kind: str, outcomes: Iterable[dict[str, Any]],
                         durations_ms: Iterable[float] = (),
                         code_revision_value: str | None = None,
                         device_baseline_hash: str = "",
                         task_set_hash: str = "",
                         memory_manifest_hash: str = "",
                         model_revision: str = "",
                         safety_violations: int = 0,
                         artifacts_redacted: bool = True,
                         notes: list[str] | None = None) -> ExperimentReport:
    counts = {"success": 0, "failed": 0, "blocked": 0, "unknown": 0}
    attempted = 0
    for outcome in outcomes:
        attempted += 1
        field = OUTCOME_FIELDS.get(str(outcome.get("outcome_kind") or outcome.get("status")))
        if field is None:
            raise ValueError(f"unmapped outcome {outcome.get('outcome_kind')!r}")
        counts[field] += 1
    percentiles = nearest_rank_percentiles(durations_ms)
    return ExperimentReport(
        run_id=run_id, kind=kind, code_revision=code_revision_value or code_revision(),
        device_baseline_hash=device_baseline_hash, task_set_hash=task_set_hash,
        memory_manifest_hash=memory_manifest_hash, model_revision=model_revision,
        attempted=attempted, **counts, **percentiles,
        safety_violations=safety_violations, artifacts_redacted=artifacts_redacted,
        notes=list(notes or []))


def report_from_wave(wave: dict[str, Any], *, run_id: str | None = None) -> ExperimentReport:
    """Convert a `WaveRunner` report (offline practice) into the batch schema."""
    durations = []
    for branch in wave.get("branches", []):
        usage = (branch.get("result") or {}).get("usage") or {}
        if isinstance(usage.get("elapsed_seconds"), (int, float)):
            durations.append(float(usage["elapsed_seconds"]) * 1000)
    return report_from_outcomes(
        run_id=run_id or str(wave.get("wave_id", "wave")),
        kind="offline_practice_wave", outcomes=wave.get("branches", []),
        durations_ms=durations,
        memory_manifest_hash=str((wave.get("memory_out") or {}).get("manifest_hash", "")),
        notes=["mock device only: not device evidence"],
        safety_violations=0)
