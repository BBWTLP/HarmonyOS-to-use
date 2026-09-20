"""F04 step 2 / DF2: offline comparison of rules and the local Decider.

Runs entirely against the local decision service plus a saved dataset: no device
access. Thresholds are fitted on the calibration split only and the holdout
split is used solely for the admission verdict.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_agent.decision.providers.decider import (DEFAULT_REVISION, DeciderProvider)
from harmony_agent.evals import (evaluate_with_decider, fit_thresholds, load_dataset,
                                 rules_baseline, summarise, summarise_by_split)


async def run(args) -> int:
    samples = load_dataset(args.dataset)
    if args.limit:
        samples = samples[: args.limit]
    provider = DeciderProvider(token_file=args.token_file, revision=args.revision)
    health = None
    try:
        health = await provider.health()
    except Exception as error:
        print(json.dumps({"status": "not_ready",
                          "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1
    started = time.time()
    decider_rows = await evaluate_with_decider(samples, provider)
    baseline_rows = rules_baseline(samples)
    by_split = {name: [row for row in decider_rows if row["split"] == name]
                for name in ("development", "calibration", "holdout")}
    calibration = by_split.get("calibration", [])
    fitted = fit_thresholds(calibration, max_error_rate=args.max_error_rate,
                            min_coverage=args.min_coverage)
    gate = fitted.get("confidence_gate") or 0.0
    report = {
        "schema_version": 1,
        "task": "F04 / DF2 offline decision comparison",
        "dataset": str(args.dataset),
        "samples": len(samples),
        "health": health,
        "model_revision": args.revision,
        "rules": {"overall": summarise(baseline_rows, threshold=0.0),
                  "by_split": summarise_by_split(baseline_rows, threshold=0.0)},
        "decider": {"overall": summarise(decider_rows, threshold=0.0),
                    "by_split": summarise_by_split(decider_rows, threshold=0.0),
                    "gated_overall": summarise(decider_rows, threshold=gate),
                    "gated_holdout": summarise(by_split.get("holdout", []), threshold=gate)},
        "calibration": {
            "fitted_on": "calibration split only",
            "candidates": fitted,
            "applied_gate": gate,
            "note": "thresholds are never fitted on the holdout split",
        },
        "holdout_verdict": None,
        # Per-sample rows make the acceptance/refusal pattern auditable. They
        # carry ids and scores only; the underlying state text stays in the
        # local dataset, never in this report.
        "rows": [{"state_id": row.get("state_id"), "split": row.get("split"),
                  "choice": row.get("choice"), "label": row.get("label"),
                  "correct": row.get("choice") == row.get("label"),
                  "confidence": row.get("confidence"),
                  "certainty": row.get("certainty"),
                  "error": row.get("error")}
                 for row in decider_rows],
        "duration_seconds": round(time.time() - started, 3),
    }
    holdout = report["decider"]["gated_holdout"]
    report["holdout_verdict"] = (
        "admit_limited_execution"
        if (holdout["accepted"] > 0
            and (holdout["error_rate_among_accepted"] or 1.0) <= args.max_error_rate)
        else "keep_shadow_only")
    report["status"] = "ok"
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    if args.calibration_out and fitted.get("confidence_gate") is not None:
        artifact = {"calibration_version": args.calibration_version,
                    "provider": "decider", "model_revision": args.revision,
                    "confidence_gate": fitted["confidence_gate"],
                    "certainty_gate": 0.0,
                    "fitted_on_samples": len(calibration),
                    "calibration_metrics": fitted,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        Path(args.calibration_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.calibration_out).write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=".runtime/evals/decision/states.jsonl")
    parser.add_argument("--report", default="docs/acceptance/2026-09-20/decider-calibration.json")
    parser.add_argument("--calibration-out", default=".runtime/evals/decision/calibration.json")
    parser.add_argument("--calibration-version", default="decider-cal-2026-09-20")
    parser.add_argument("--token-file", default=str(REPO_ROOT / "services/decider/.runtime/api-token"))
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--max-error-rate", type=float, default=0.05)
    parser.add_argument("--min-coverage", type=float, default=0.2)
    parser.add_argument("--limit", type=int)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
