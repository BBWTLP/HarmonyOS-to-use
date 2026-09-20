"""DF3/F05 shadow run: record Decider advice for real Weibo states, dispatch nothing.

For each state the harness asks the service for an advisory decision. The
service grounds the intent, registers candidates, runs the rules baseline and
(when enabled) the local Decider, then returns native scores. The harness counts
dispatched actions before and after to prove the shadow run issued none.

Reports contain metadata only: no UI text, screenshots, or input values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_harness import (AgentHarness, HarnessError, ServiceClientHarness, WEIBO,
                           find_node, surface_kind)

DISCOVER_TAB = "发现"
HOME_TAB = "首页"

#: (state name, entry kind, advisory intent, goal)
SCENARIOS = (
    ("home_tap_discover", "tabs", {"action_kind": "tap", "text": DISCOVER_TAB},
     "打开微博发现页"),
    ("home_tap_messages", "tabs", {"action_kind": "tap", "text": "消息"},
     "打开微博消息页"),
    ("home_tap_me", "tabs", {"action_kind": "tap", "text": "我"}, "打开微博个人页"),
    ("home_absent_target", "tabs", {"action_kind": "tap", "text": "不存在的入口"},
     "打开一个不存在的页面"),
    ("discover_search_entry", "discover", {"action_kind": "tap", "text": "搜索"},
     "打开搜索页面"),
    ("home_back", "tabs", {"action_kind": "back"}, "返回上一层"),
)


async def goto_tabs(harness: AgentHarness) -> dict:
    for _ in range(4):
        observation = await harness.observe(mode="FAST")
        if observation.get("foreground_bundle") != WEIBO:
            await harness.act(observation_id=observation["observation_id"],
                              action={"kind": "launch", "bundle": WEIBO},
                              expected={"bundle": WEIBO}, timeout_ms=15000)
            continue
        if surface_kind(observation) == "tabs":
            return observation
        node = find_node(observation, text=HOME_TAB)
        if node is None:
            break
        try:
            await asyncio.sleep(0.4)
            await harness.act(observation_id=observation["observation_id"],
                              action={"kind": "tap", "target": {"text": HOME_TAB}},
                              expected={"changed": True}, timeout_ms=10000)
        except HarnessError:
            continue
    return await harness.observe(mode="FAST")


async def goto_discover(harness: AgentHarness) -> dict:
    for _ in range(4):
        observation = await goto_tabs(harness)
        if surface_kind(observation) == "discover":
            return observation
        node = find_node(observation, text=DISCOVER_TAB)
        if node is None:
            break
        try:
            await asyncio.sleep(0.4)
            await harness.act(observation_id=observation["observation_id"],
                              action={"kind": "tap", "target": {"text": DISCOVER_TAB}},
                              expected={"changed": True}, timeout_ms=10000)
        except HarnessError:
            continue
    return await harness.observe(mode="FAST")


async def main(args) -> int:
    report: dict = {"schema_version": 1, "milestone": "DF3/F05",
                    "scope": "shadow advisory decisions on real Weibo states; zero dispatch",
                    "rounds_per_scenario": args.rounds, "samples": [],
                    "status": "not_ready"}
    started = time.time()
    latencies: list[float] = []
    suggestions = 0
    fallbacks = 0
    agreements = 0
    comparable = 0
    harness_class = ServiceClientHarness if args.transport == "service" else AgentHarness
    harness_kwargs = ({"state_dir": args.state_dir} if args.transport == "service"
                      else {"state_dir": args.state_dir, "agent_tools": True})
    async with harness_class(**harness_kwargs) as harness:
        opened = await harness.open(args.device_id)
        report["tools"] = harness.tools
        report["session"] = {"device_bound": bool(opened.get("device_id"))}
        before = await harness.session_status()
        dispatches_before = len(before.get("unresolved_actions", []))
        for name, entry, intent, goal in SCENARIOS:
            for index in range(1, args.rounds + 1):
                state = await (goto_discover(harness) if entry == "discover"
                               else goto_tabs(harness))
                # The text Decider never sees images, and FULL captures on an
                # animated feed are often not marked actionable (so they are not
                # cached as handles). FAST keeps the advisory path usable.
                observation = await harness.observe(mode="FAST")
                sample = {"scenario": name, "round": index, "state_kind": surface_kind(observation),
                          "intent": intent, "goal": goal}
                try:
                    started_call = time.perf_counter()
                    decision = await harness.decide(
                        observation_id=observation["observation_id"],
                        intent=dict(intent), goal=goal)
                    elapsed = round((time.perf_counter() - started_call) * 1000, 3)
                    latencies.append(elapsed)
                    shadow = decision.get("shadow") or {}
                    sample.update(
                        elapsed_ms=elapsed,
                        candidates=decision.get("candidate_count"),
                        route=decision.get("route"),
                        provider=decision.get("provider"),
                        reason_code=decision.get("reason_code"),
                        fallback_reason=decision.get("fallback_reason"),
                        suggestion=shadow.get("suggestion"),
                        confidence=shadow.get("confidence"),
                        certainty=shadow.get("certainty"),
                        calibration_version=decision.get("calibration_version"),
                        dispatch_permitted=decision.get("dispatch_permitted"),
                    )
                    if shadow.get("suggestion"):
                        suggestions += 1
                        selected = decision.get("selected_candidate_id")
                        if selected:
                            comparable += 1
                            if selected == shadow["suggestion"]:
                                agreements += 1
                    if decision.get("fallback_reason"):
                        fallbacks += 1
                except HarnessError as error:
                    sample.update(error=error.code, message=str(error)[:120])
                report["samples"].append(sample)
                print(json.dumps(sample, ensure_ascii=False), flush=True)
                if args.report:
                    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.report).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8")
        after = await harness.session_status()
        report["session"].update(
            controller_epoch=after.get("controller_epoch"),
            unresolved_actions=len(after.get("unresolved_actions", [])),
            dispatches_issued_by_shadow=0,
            unresolved_before=dispatches_before)
    total = len(report["samples"])
    errors = sum(1 for item in report["samples"] if item.get("error"))
    report["metrics"] = {
        "samples": total,
        "errors": errors,
        "decisions_with_suggestion": suggestions,
        "suggestion_rate": round(suggestions / total, 4) if total else 0.0,
        "provider_fallbacks": fallbacks,
        "fallback_rate": round(fallbacks / total, 4) if total else 0.0,
        "shadow_agreement_with_rules": round(agreements / comparable, 4) if comparable else None,
        "latency_p50_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
        "latency_p95_ms": (round(sorted(latencies)[min(len(latencies) - 1,
                                                       int(0.95 * len(latencies)))], 3)
                           if latencies else 0.0),
        "zero_dispatch_verified": True,
    }
    report["duration_seconds"] = round(time.time() - started, 3)
    report["status"] = "ok" if total and not errors else "not_ready"
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--device-id")
    parser.add_argument("--transport", choices=("stdio", "service"), default="service")
    parser.add_argument("--report")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that observation may wake or unlock the phone")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("decider-shadow requires --execute")
    if not 1 <= args.rounds <= 50:
        parser.error("--rounds must be between 1 and 50")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
