"""P0-01/P0-03: record the frozen baseline a batch is reproducible from.

Writes one JSON record: repository commits (Runtime and the RSIAgent reference),
Python, dependency lock digests, the device-baseline digest, the exact test command
and the report schema pointer. Read-only: it runs `git`/`python -m pip check` and
hashes local files, and never touches a device.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_COMMAND = ("{python} -m unittest discover -s tests -p test*.py")
DEVICE_BASELINE = "docs/acceptance/2026-09-19/device-baseline-20260919-batched.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def git(args: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                   text=True, check=True)
    except Exception:
        return ""
    return completed.stdout.strip()


def pip_check() -> dict:
    completed = subprocess.run([sys.executable, "-m", "pip", "check"],
                               cwd=REPO_ROOT, capture_output=True, text=True)
    return {"exit_code": completed.returncode,
            "summary": (completed.stdout or completed.stderr).strip().splitlines()[-1]
            if (completed.stdout or completed.stderr).strip() else ""}


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the frozen baseline")
    parser.add_argument("--rsi-repo", default=str(REPO_ROOT.parent / "RSIAgent"))
    parser.add_argument("--out", default="docs/acceptance/2026-09-20/baseline-freeze.json")
    args = parser.parse_args()
    rsi = Path(args.rsi_repo)
    lock = REPO_ROOT / "requirements.lock"
    payload = {
        "schema_version": 1,
        "task": "P0-01/P0-03 baseline freeze",
        "runtime": {
            "path": str(REPO_ROOT),
            "branch": git(["rev-parse", "--abbrev-ref", "HEAD"], REPO_ROOT),
            "commit": git(["rev-parse", "HEAD"], REPO_ROOT),
            "dirty_paths": len([line for line in
                                git(["status", "--porcelain"], REPO_ROOT).splitlines()
                                if line.strip()]),
            "python": ".".join(str(item) for item in sys.version_info[:3]),
        },
        "rsi_reference": {
            "path": str(rsi),
            "present": rsi.exists(),
            "commit": git(["rev-parse", "HEAD"], rsi) if rsi.exists() else "",
            "role": "reference implementation only; never a Runtime dependency",
        },
        "dependencies": {
            "requirements.lock": {"sha256": sha256(lock)},
            "pyproject.toml": {"sha256": sha256(REPO_ROOT / "pyproject.toml")},
            "pip_check": pip_check(),
        },
        "device_baseline": {
            "path": DEVICE_BASELINE,
            "sha256": sha256(REPO_ROOT / DEVICE_BASELINE),
            "note": "device identity is recorded per batch; a device change invalidates",
        },
        "frozen_commands": {
            "regression": TEST_COMMAND.format(python=sys.executable),
            "regression_wrapper": "python scripts/reproduce.py",
            "secret_scan": "python scripts/secret_scan.py --report docs/acceptance/2026-09-20/secret-scan.json",
            "offline_practice": "python tools/rsi/author_wave.py && python tools/rsi/run_practice.py --approval-ref approval_<id>",
            "batch_report": "python scripts/experiment_report.py --wave-report .runtime/rsi/wave-report.json",
        },
        "report_schema": {
            "module": "harmony_agent.reporting.ExperimentReport",
            "plan_section": "§8.3",
            "required_fields": ["run_id", "code_revision", "device_baseline_hash",
                                "task_set_hash", "memory_manifest_hash", "model_revision",
                                "attempted", "success", "failed", "blocked", "unknown",
                                "p50_ms", "p95_ms", "safety_violations",
                                "artifacts_redacted"],
        },
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "written", "path": str(out),
                      "runtime_commit": payload["runtime"]["commit"],
                      "rsi_commit": payload["rsi_reference"]["commit"],
                      "pip_check": payload["dependencies"]["pip_check"]["exit_code"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
