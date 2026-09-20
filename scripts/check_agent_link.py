"""Read-only link check: MCP stdio client -> runtime service -> device.

Prints timing for connect, session open, FAST/FULL observation and an optional
image capture. It never dispatches a device action.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_harness import AgentHarness


async def main(args) -> int:
    steps: list[dict] = []
    report_path = Path(args.report) if args.report else None

    def record(name: str, started: float, detail: dict | None = None) -> None:
        steps.append({"step": name, "ms": round((time.time() - started) * 1000, 3),
                      **(detail or {})})
        # Written incrementally so a long or stuck run still shows its progress.
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps({"steps": steps}, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    started = time.time()
    async with AgentHarness(state_dir=args.state_dir, agent_tools=args.agent_tools) as harness:
        record("connect", started, {"tools": harness.tools})
        started = time.time()
        opened = await harness.open(args.device_id)
        record("session_open", started,
               {"controller_epoch": opened.get("controller_epoch"),
                "device_bound": bool(opened.get("device_id"))})
        for mode in ("FAST", "FULL"):
            started = time.time()
            observation = await harness.observe(mode=mode)
            record(f"observe_{mode.lower()}", started,
                   {"capture_ms": observation.get("capture_ms"),
                    "actionable": observation.get("actionable"),
                    "nodes": len(observation.get("catalog", [])),
                    "foreground_known": observation.get("foreground_bundle") is not None,
                    "screen_on": (observation.get("screen_state") or {}).get("screen_on")})
        started = time.time()
        image_observation = await harness.observe(mode="FAST", include_image=True)
        record("observe_image", started,
               {"images": harness.last_images,
                "image_tree_consistent": image_observation.get("image_tree_consistent"),
                "dimensions_match": image_observation.get("image_dimensions_match")})
        started = time.time()
        status = await harness.session_status()
        record("session_status", started,
               {"status": status.get("status"),
                "unresolved": len(status.get("unresolved_actions", []))})
    report = {"schema_version": 1, "scope": "agent link check (read-only)",
              "steps": steps,
              "status": "ok" if all(step.get("ms", 0) > 0 for step in steps) else "not_ready"}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--device-id")
    parser.add_argument("--agent-tools", action="store_true")
    parser.add_argument("--report")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
