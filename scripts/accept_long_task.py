#!/usr/bin/env python3
"""T07 long-task acceptance: bounded multi-step sequences and resume evidence.

CLI contract (fixed):
  python scripts/accept_long_task.py --execute --state-dir <dir>
    --tasks evals/tasks/long-device.json --runs 1 --report <path>

Without --execute the script only dry-runs the task file (structure, budgets,
step kinds) and exits non-zero on invalid specs. It never touches the phone.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_TASKS = Path(__file__).resolve().parents[1] / "evals" / "tasks" / "long-device.json"

STEP_KINDS = {
    "tap", "replace_text", "back", "swipe", "wait", "observe", "reobserve",
    "recover", "history", "burst", "launch",
}


def dry_run(tasks_path: Path) -> dict:
    problems: list[str] = []
    try:
        definition = json.loads(tasks_path.read_text(encoding="utf-8"))
    except Exception as error:
        return {"status": "invalid", "problems": [f"tasks file unreadable: {error}"]}
    tasks = definition.get("tasks") or []
    if not tasks:
        problems.append("tasks[] is empty")
    for task in tasks:
        tid = task.get("id") or "?"
        steps = task.get("steps") or []
        if not steps:
            problems.append(f"{tid}: no steps")
        if len(steps) < 1:
            problems.append(f"{tid}: empty sequence")
        for step in steps:
            kind = str(step).partition(":")[0]
            if kind not in STEP_KINDS:
                problems.append(f"{tid}: unsupported step kind {kind!r}")
        budget = task.get("budget") or definition.get("defaults", {}).get("budget") or {}
        max_steps = budget.get("max_dispatches")
        if max_steps is not None and len(steps) > int(max_steps) + 5:
            problems.append(f"{tid}: steps {len(steps)} far exceed max_dispatches {max_steps}")
        if not task.get("success_criteria"):
            problems.append(f"{tid}: missing success_criteria")
    return {
        "status": "ok" if not problems else "invalid",
        "problems": problems,
        "task_count": len(tasks),
        "step_kinds": sorted({str(s).partition(":")[0] for t in tasks for s in (t.get("steps") or [])}),
    }


async def run_device(args, definition: dict) -> dict:
    from agent_harness import HarnessError, ServiceClientHarness, AgentHarness

    tasks = definition["tasks"]
    if args.only:
        wanted = set(args.only)
        tasks = [t for t in tasks if t.get("id") in wanted]
    harness_class = ServiceClientHarness if args.transport == "service" else AgentHarness
    kwargs = {"state_dir": args.state_dir}
    if harness_class is AgentHarness:
        kwargs["agent_tools"] = False
    report = {
        "schema_version": 1,
        "milestone": "T07-long",
        "tasks_file": str(args.tasks),
        "runs": args.runs,
        "mode": "execute",
        "planned": len(tasks) * args.runs,
        "attempted": 0,
        "unattempted": 0,
        "passed": 0,
        "failed": 0,
        "blocked": 0,
        "results": [],
        "status": "not_ready",
    }
    async with harness_class(**kwargs) as harness:
        opened = await harness.open(args.device_id)
        report["session"] = {"device_bound": bool(opened.get("device_id"))}
        from accept_m2 import execute_task  # shared typed stepper
        for task in tasks:
            for run_index in range(1, args.runs + 1):
                report["attempted"] += 1
                try:
                    outcome = await execute_task(harness, task)
                except HarnessError as error:
                    outcome = {"task_id": task.get("id"), "status": "blocked",
                               "verdict": {"verdict": "inconclusive"},
                               "blocked": {"code": error.code, "message": str(error)[:160]}}
                outcome["run"] = run_index
                report["results"].append(outcome)
                verdict = (outcome.get("verdict") or {}).get("verdict")
                if verdict == "pass":
                    report["passed"] += 1
                elif outcome.get("status") == "blocked":
                    report["blocked"] += 1
                else:
                    report["failed"] += 1
    report["unattempted"] = report["planned"] - report["attempted"]
    report["status"] = "ok" if report["passed"] == report["planned"] else "not_ready"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Dispatch real device actions (required for device runs)")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--device-id")
    parser.add_argument("--transport", choices=("stdio", "service"), default="service")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be 1..20")

    plan = dry_run(args.tasks)
    if plan["status"] != "ok":
        print(json.dumps({"dry_run": plan}, indent=2, ensure_ascii=False))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"dry_run": plan}, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        return 2
    if not args.execute:
        # Dry-run only: validate the file, never touch the device.
        out = {"dry_run": plan, "status": "dry_run_ok", "execute": False}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        return 0

    definition = json.loads(args.tasks.read_text(encoding="utf-8"))
    report = asyncio_run(run_device(args, definition))
    report["dry_run"] = plan
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())
