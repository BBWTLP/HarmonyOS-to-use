"""C01: controlled segmented performance baseline (hot samples).

Runs the existing benchmark entry point for each observation mode and stores the
raw reports plus a digest. The plan also asks for 20 cold samples per mode; this
script does not fabricate them, because the project has no controlled cold-start
procedure (force-stop, app reload, cache state). The missing cold baseline is
reported explicitly instead of being renamed from "first sample".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODES = (("FAST", False), ("FAST", True), ("FULL", False))


def label(mode: str, include_image: bool) -> str:
    return f"{mode.lower()}{'-image' if include_image else ''}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--out-dir", default="docs/acceptance/2026-09-20")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that observations may wake or unlock the phone")
    args = parser.parse_args()
    if not args.execute:
        parser.error("accept-c01-benchmark requires --execute")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {"schema_version": 1, "task": "C01 hot baseline",
                     "samples_per_mode": args.samples, "reports": [],
                     "cold_baseline": "not_produced",
                     "cold_reason": ("no controlled cold-start procedure exists in this "
                                     "project; first-sample ordering is not a cold start"),
                     "status": "not_ready"}
    started = time.time()
    for mode, include_image in MODES:
        name = label(mode, include_image)
        report_path = out_dir / f"benchmark-c01-{name}.json"
        command = [sys.executable, "-m", "harmony_runtime.cli", "benchmark",
                   "--execute", "--samples", str(args.samples), "--mode", mode,
                   "--state-dir", args.state_dir]
        if include_image:
            command.append("--include-image")
        completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace")
        entry: dict = {"mode": mode, "include_image": include_image,
                       "exit_code": completed.returncode, "report": str(report_path)}
        try:
            payload = json.loads(completed.stdout)
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                              sort_keys=True) + "\n", encoding="utf-8")
            entry.update(status=payload.get("status"),
                         attempted=payload.get("attempted"),
                         succeeded=payload.get("succeeded"),
                         failed=payload.get("failed"),
                         p50_ms=(payload.get("latency") or {}).get("p50_ms"),
                         p95_ms=(payload.get("latency") or {}).get("p95_ms"))
        except Exception as error:
            entry["error"] = f"{type(error).__name__}: {str(error)[:120]}"
            entry["stderr_tail"] = completed.stderr[-400:]
        summary["reports"].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    summary["duration_seconds"] = round(time.time() - started, 3)
    summary["status"] = ("ok" if all(item.get("exit_code") == 0
                                     for item in summary["reports"]) else "not_ready")
    target = out_dir / "benchmark-c01-summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
