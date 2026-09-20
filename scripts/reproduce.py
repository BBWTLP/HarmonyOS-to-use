"""A03: single reproducible entry point for the regression suite.

Runs the unittest discovery used by this project, records the code snapshot hash
and dependency versions next to the log, and fails loudly if the suite fails.
The log is written under .runtime/ (never committed) and contains no device data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=".runtime/tests-current.log")
    parser.add_argument("--report", default=".runtime/tests-current.json")
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    log_path = REPO_ROOT / args.log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
               "-p", args.pattern, "-v"]
    with open(log_path, "w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=REPO_ROOT, stdout=log,
                                   stderr=subprocess.STDOUT, text=True)
    duration = round(time.time() - started, 3)
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
    ran = next((line for line in reversed(tail) if line.startswith("Ran ")), "")
    report = {
        "schema_version": 1,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "summary": ran,
        "log": str(log_path.relative_to(REPO_ROOT)),
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                               capture_output=True, text=True).stdout.strip(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    target = REPO_ROOT / args.report
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
