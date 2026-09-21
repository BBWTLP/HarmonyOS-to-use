"""Emit the plan §8.3 batch report (P0-03).

Accepts either a wave report from `tools/rsi/run_practice.py` or explicit counts,
and prints/writes the single report schema every batch must publish: code revision,
device/task-set/memory/model identity, failure denominators, latency percentiles,
safety violations and the redaction flag.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_agent.reporting import ExperimentReport, code_revision, report_from_wave


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit a plan §8.3 batch report")
    parser.add_argument("--wave-report", default=None,
                        help="a wave report produced by tools/rsi/run_practice.py")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--kind", default="batch")
    parser.add_argument("--device-baseline-hash", default="")
    parser.add_argument("--task-set-hash", default="")
    parser.add_argument("--memory-manifest-hash", default="")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--attempted", type=int, default=None)
    parser.add_argument("--success", type=int, default=0)
    parser.add_argument("--failed", type=int, default=0)
    parser.add_argument("--blocked", type=int, default=0)
    parser.add_argument("--unknown", type=int, default=0)
    parser.add_argument("--safety-violations", type=int, default=0)
    parser.add_argument("--durations-ms", nargs="*", type=float, default=[])
    parser.add_argument("--artifacts-redacted", dest="artifacts_redacted",
                        action="store_true", default=True)
    parser.add_argument("--write", default=None, help="write the report JSON here")
    args = parser.parse_args()

    if args.wave_report:
        wave_path = Path(args.wave_report)
        if not wave_path.is_absolute():
            wave_path = REPO_ROOT / wave_path
        wave = json.loads(wave_path.read_text(encoding="utf-8"))
        report = report_from_wave(wave, run_id=args.run_id)
        report.device_baseline_hash = args.device_baseline_hash
        report.task_set_hash = args.task_set_hash
        report.model_revision = args.model_revision
    else:
        if args.attempted is None:
            print(json.dumps({"status": "error",
                              "message": "supply --wave-report or --attempted"},
                             ensure_ascii=False))
            return 2
        from harmony_agent.reporting import nearest_rank_percentiles
        report = ExperimentReport(
            run_id=args.run_id or "batch", kind=args.kind,
            code_revision=code_revision(), device_baseline_hash=args.device_baseline_hash,
            task_set_hash=args.task_set_hash,
            memory_manifest_hash=args.memory_manifest_hash,
            model_revision=args.model_revision, attempted=args.attempted,
            success=args.success, failed=args.failed, blocked=args.blocked,
            unknown=args.unknown, **nearest_rank_percentiles(args.durations_ms),
            safety_violations=args.safety_violations,
            artifacts_redacted=args.artifacts_redacted, notes=[])
    payload = report.model_dump()
    if args.write:
        out = Path(args.write)
        if not out.is_absolute():
            out = REPO_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["report_path"] = str(out)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
