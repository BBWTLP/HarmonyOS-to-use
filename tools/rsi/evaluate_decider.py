"""Shadow / calibration / canary evaluation for the local Decider (P5-03/P5-04).

Runs the local Decider service against a saved, grouped decision-state dataset and
reports the metrics the plan requires: coverage, abstention, wrong-allow, ECE,
Brier, latency percentiles and the rules baseline on the same rows. It never flips
the runtime profile: the output is a *gate verdict* plus the reasons it failed, and
`local_canary` stays off until every condition holds on the holdout split.

This tool reads data only. It does not dispatch, does not touch a device and does
not write a calibration artifact unless `--write-calibration` is given.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_agent.decision.calibration import dataset_digest
from harmony_agent.decision.providers.decider import DEFAULT_REVISION, DeciderProvider
from harmony_agent.evals import (evaluate_with_decider, fit_thresholds, load_dataset,
                                 rules_baseline, summarise, summarise_by_split)


def percentiles(rows: list[dict]) -> dict:
    values = sorted(float(row["elapsed_ms"]) for row in rows
                    if isinstance(row.get("elapsed_ms"), (int, float)))
    if not values:
        return {"p50_ms": None, "p95_ms": None, "samples": 0}
    def nearest_rank(fraction: float) -> float:
        index = max(1, int(round(fraction * len(values)))) - 1
        return round(values[min(index, len(values) - 1)], 3)
    return {"p50_ms": nearest_rank(0.5), "p95_ms": nearest_rank(0.95),
            "samples": len(values)}


def canary_gate(*, holdout: dict, rules_holdout: dict, calibration_ready: bool,
                latency: dict, min_samples: int, p95_budget_ms: float,
                task_success_delta: float | None) -> dict:
    """The plan's canary conditions, evaluated one by one."""
    reasons: list[str] = []
    if holdout.get("samples", 0) < min_samples:
        reasons.append(f"insufficient_holdout_samples:{holdout.get('samples', 0)}")
    if holdout.get("accepted_errors", 0) != 0:
        reasons.append(f"holdout_wrong_allow:{holdout.get('accepted_errors')}")
    if not calibration_ready:
        reasons.append("calibration_artifact_missing")
    if latency.get("p95_ms") is None or latency["p95_ms"] > p95_budget_ms:
        reasons.append(f"p95_budget_exceeded:{latency.get('p95_ms')}")
    if (holdout.get("coverage") or 0.0) <= 0.0:
        reasons.append("no_coverage_on_holdout")
    if rules_holdout.get("samples") and holdout.get("accepted") == 0:
        reasons.append("abstains_on_every_holdout_state")
    if task_success_delta is not None and task_success_delta < 0:
        reasons.append("task_success_rate_regressed")
    return {"verdict": "local_canary_allowed" if not reasons else "keep_shadow_only",
            "reasons": reasons,
            "checked": {"holdout_wrong_allow": holdout.get("accepted_errors"),
                        "holdout_samples": holdout.get("samples"),
                        "calibration_ready": calibration_ready,
                        "p95_ms": latency.get("p95_ms")}}


async def run(args) -> int:
    samples = load_dataset(args.dataset)
    if args.limit:
        samples = samples[: args.limit]
    by_split = {}
    for sample in samples:
        by_split.setdefault(sample.split, []).append(sample)
    baseline_rows = rules_baseline(samples)
    provider_rows: list[dict] = []
    health = None
    provider_error = None
    if not args.rules_only:
        provider = DeciderProvider(token_file=args.token_file, revision=args.revision)
        try:
            health = await provider.health()
            provider_rows = await evaluate_with_decider(samples, provider)
        except Exception as error:
            provider_error = f"{type(error).__name__}: {error}"
            provider_rows = [{"state_id": sample.state_id, "split": sample.split,
                              "label": sample.label, "error": "provider_unavailable"}
                             for sample in samples]
    calibration_rows = [row for row in provider_rows if row.get("split") == "calibration"]
    fitted = fit_thresholds(calibration_rows, max_error_rate=args.max_error_rate,
                            min_coverage=args.min_coverage) if calibration_rows else {}
    gate = fitted.get("confidence_gate") or 0.0
    holdout_rows = [row for row in provider_rows if row.get("split") == "holdout"]
    rules_holdout = [row for row in baseline_rows if row.get("split") == "holdout"]
    holdout_summary = summarise(holdout_rows, threshold=gate)
    calibration_ready = bool(args.calibration_file) and Path(args.calibration_file).exists()
    gate_report = canary_gate(
        holdout=holdout_summary, rules_holdout=summarise(rules_holdout, threshold=0.0),
        calibration_ready=calibration_ready, latency=percentiles(holdout_rows),
        min_samples=args.min_holdout, p95_budget_ms=args.p95_budget_ms,
        task_success_delta=args.task_success_delta)
    report = {
        "schema_version": 1,
        "task": "P5 Decider shadow / calibration / canary evaluation",
        "generated_at": time.time(),
        "dataset": {"path": str(args.dataset), "samples": len(samples),
                    "digest": dataset_digest(samples),
                    "splits": {name: len(items) for name, items in sorted(by_split.items())}},
        "model_revision": args.revision,
        "health": health,
        "provider_error": provider_error,
        "rules": {"overall": summarise(baseline_rows, threshold=0.0),
                  "by_split": summarise_by_split(baseline_rows, threshold=0.0)},
        "model": {"overall": summarise(provider_rows, threshold=0.0),
                  "gated_overall": summarise(provider_rows, threshold=gate),
                  "holdout": holdout_summary,
                  "latency": percentiles(provider_rows),
                  "holdout_latency": percentiles(holdout_rows)},
        "calibration": {"fitted_on": "calibration split only", "candidates": fitted},
        "canary_gate": gate_report,
        "limitations": ["a shadow run never dispatches; wrong-allow counts are offline labels"],
    }
    if args.write_calibration:
        artifact = {
            "calibration_id": args.calibration_id,
            "provider_revision": args.revision,
            "confidence_threshold": gate,
            "certainty_threshold": fitted.get("threshold", gate),
            "dataset_sha256": report["dataset"]["digest"]["holdout_sha256"]
            if isinstance(report["dataset"]["digest"], dict) else "",
            "fitted_on": "calibration split only",
            "gate_verdict": gate_report["verdict"],
        }
        out = Path(args.write_calibration)
        if not out.is_absolute():
            out = REPO_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        report["calibration"]["artifact"] = {"path": str(out), **artifact}
    if args.report:
        out = Path(args.report)
        if not out.is_absolute():
            out = REPO_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(out)
    print(json.dumps({"status": "ok" if not provider_error else "degraded",
                      "samples": len(samples), "gate": gate_report["verdict"],
                      "reasons": gate_report["reasons"],
                      "holdout_wrong_allow": gate_report["checked"]["holdout_wrong_allow"],
                      "report": report.get("report_path")}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the local Decider offline")
    parser.add_argument("--dataset", default=".runtime/evals/decision/dataset.json")
    parser.add_argument("--report", default=".runtime/rsi/decider-evaluation.json")
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--calibration-file", default=None,
                        help="existing calibration artifact to check for completeness")
    parser.add_argument("--write-calibration", default=None)
    parser.add_argument("--calibration-id", default="cal-offline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-holdout", type=int, default=300)
    parser.add_argument("--p95-budget-ms", type=float, default=15000.0)
    parser.add_argument("--max-error-rate", type=float, default=0.05)
    parser.add_argument("--min-coverage", type=float, default=0.2)
    parser.add_argument("--task-success-delta", type=float, default=None)
    parser.add_argument("--rules-only", action="store_true",
                        help="score the rules baseline without calling the model")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
