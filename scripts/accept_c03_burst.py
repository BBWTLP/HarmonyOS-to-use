"""C03: burst and dynamic-control capability arbitration on the real device.

Measures whether a multi-step burst actually completes inside the 3000 ms
contract on the current device/app, instead of assuming the fast path works.
The measured boundary is reported as a capability matrix entry either way.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_harness import (AgentHarness, HarnessError, ServiceClientHarness, editor_input,
                           find_node, find_search_bar, surface_kind)

DISCOVER_TAB = "发现"
VALUES = ("测试一", "测试二", "测试三")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return round(ordered[index], 3)


async def goto_editor(harness) -> dict:
    for _ in range(8):
        observation = await harness.stable_observation()
        field = editor_input(observation)
        if field is not None:
            return observation
        kind = surface_kind(observation)
        try:
            if kind == "discover":
                bar = find_search_bar(observation)
                if bar is None:
                    await asyncio.sleep(0.8)
                    continue
                # The Discover page animates; let the layout settle first.
                await asyncio.sleep(1.5)
                await harness.act(
                    observation_id=observation["observation_id"],
                    action={"kind": "tap", "target": {"action_id": bar["action_id"]}},
                    expected={"changed": True}, timeout_ms=10000)
            else:
                tab = find_node(observation, text=DISCOVER_TAB)
                if tab is None:
                    await asyncio.sleep(0.5)
                    await harness.act(observation_id=observation["observation_id"],
                                      action={"kind": "back"},
                                      expected={"changed": True}, timeout_ms=10000)
                else:
                    await asyncio.sleep(0.5)
                    await harness.act(
                        observation_id=observation["observation_id"],
                        action={"kind": "tap", "target": {"text": DISCOVER_TAB}},
                        expected={"changed": True}, timeout_ms=10000)
        except HarnessError:
            pass
    raise HarnessError("editor_unavailable", "Search editor was not reached")


async def run_round(harness, steps: int, index: int) -> dict:
    observation = await goto_editor(harness)
    field = editor_input(observation)
    if field is None:
        raise HarnessError("input_missing", "No input field is observable")
    burst_steps = []
    for step_index in range(steps):
        value = VALUES[(index + step_index) % len(VALUES)]
        burst_steps.append({
            "action": {"kind": "replace_text",
                       "target": {"action_id": field["action_id"]},
                       "text": value},
            "expected": {"changed": True},
            "watch_timeout_ms": 0,
        })
    started = time.perf_counter()
    result = await harness.burst(request_id=f"burst_{index}_{steps}",
                                 session_id=harness.session_id,
                                 observation_id=observation["observation_id"],
                                 steps=burst_steps, timeout_ms=3000)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return {"steps": steps, "status": result.get("status"),
            "verified_steps": result.get("verified_steps"),
            "stop_reason": result.get("stop_reason"),
            "reported_ms": (result.get("timing") or {}).get("total_ms"),
            "client_ms": elapsed}


async def main(args) -> int:
    report: dict = {"schema_version": 1, "task": "C03 burst arbitration",
                    "rounds_per_shape": args.rounds, "shapes": {}, "status": "not_ready"}
    harness_class = ServiceClientHarness if args.transport == "service" else AgentHarness
    kwargs = ({"state_dir": args.state_dir} if args.transport == "service"
              else {"state_dir": args.state_dir, "agent_tools": False})
    async with harness_class(**kwargs) as harness:
        await harness.open(args.device_id)
        for steps in (1, 2, 3):
            rows = []
            for index in range(args.rounds):
                try:
                    rows.append(await run_round(harness, steps, index))
                except Exception as error:
                    rows.append({"steps": steps, "status": "error",
                                 "error": getattr(error, "code", type(error).__name__)})
            completed = [row for row in rows if row.get("status") == "completed"]
            latencies = [float(row["client_ms"]) for row in completed]
            report["shapes"][str(steps)] = {
                "rounds": len(rows),
                "completed": len(completed),
                "completion_rate": round(len(completed) / len(rows), 4) if rows else 0.0,
                "p50_client_ms": percentile(latencies, 0.5),
                "p95_client_ms": percentile(latencies, 0.95),
                "within_budget": sum(1 for value in latencies if value <= 3000),
                "stop_reasons": sorted({row.get("stop_reason") for row in rows
                                        if row.get("stop_reason")}),
                "errors": sorted({row.get("error") for row in rows if row.get("error")}),
                "rows": rows[:20],
            }
            print(json.dumps({str(steps): report["shapes"][str(steps)]}, ensure_ascii=False),
                  flush=True)
        status = await harness.session_status()
        report["session"] = {"unresolved_actions": len(status.get("unresolved_actions", [])),
                             "device_state": status.get("device_state")}
    supported = [int(steps) for steps, item in report["shapes"].items()
                 if item["rounds"] and item["completion_rate"] >= 0.95
                 and item["within_budget"] == item["completed"] and item["completed"]]
    report["capability_matrix"] = {
        "supported_burst_lengths_within_3000ms": sorted(supported),
        "max_supported_steps": max(supported) if supported else 0,
        "note": ("Only shapes that completed in at least 95% of rounds AND stayed "
                 "within the 3000 ms contract in every completed round are declared "
                 "supported; anything else stays disabled."),
    }
    report["status"] = "ok" if report["session"]["unresolved_actions"] == 0 else "not_ready"
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--transport", choices=("stdio", "service"), default="service")
    parser.add_argument("--device-id")
    parser.add_argument("--report", default="docs/acceptance/2026-09-20/burst-arbitration.json")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that this run dispatches real device actions")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("accept-c03-burst requires --execute")
    if not 1 <= args.rounds <= 50:
        parser.error("--rounds must be between 1 and 50")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
