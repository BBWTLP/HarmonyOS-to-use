"""Opt-in Weibo navigation acceptance over the production stdio MCP boundary.

No posting, liking, following or account changes. Reports contain metadata only.
An inconclusive action stops the run; it is never automatically replayed.
"""
import argparse
import asyncio
import json
import hashlib
import re
import math
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

GENERAL = "实时热点，每分钟更新一次"
TECH = "科技热点，洞悉行业发展新趋势"


class AcceptanceFailure(RuntimeError):
    pass


class ToolFailure(AcceptanceFailure):
    def __init__(self, tool, code):
        super().__init__("mcp_tool_error:" + tool)
        self.code = code


def texts(obs):
    return {n.get("text", "") for n in obs.get("catalog", [])}


def topic_matches(observed, topic):
    """Only accept the exact topic or its visible hashtag wrapper."""
    return topic in observed or "#" + topic + "#" in observed


def video_identity(obs):
    descriptions = [n["text"] for n in obs.get("catalog", [])
                    if n.get("resource_id") == "0" and n.get("type") == "Text" and len(n.get("text", "")) >= 10]
    return descriptions[0] if len(descriptions) == 1 else None


def video_progress(obs):
    sliders = [n.get("text", "") for n in obs.get("catalog", []) if n.get("type") == "Slider"]
    return float(sliders[0]) if len(sliders) == 1 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", sliders[0]) else -1


def stats(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"count": len(values), "p50_ms": ordered[math.ceil(len(values)*.5)-1],
            "p95_ms": ordered[math.ceil(len(values)*.95)-1], "max_ms": max(values)}


async def run(args, report):
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "harmony_runtime.cli", "mcp", "--state-dir", str(args.state_dir.resolve())])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            async def call(name, payload):
                result = await client.call_tool(name, payload)
                data = result.structured_content or {}
                if result.is_error:
                    # Do not write backend messages that might contain UI or identifiers.
                    code = data.get("error", {}).get("code", "unknown")
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
                        code = "unknown"
                    report["tool_failure"] = {"tool": name, "code": code}
                    raise ToolFailure(name, code)
                return data
            opened = await call("mobile_session", {"operation": "open"})
            sid = opened["session_id"]
            async def observe():
                obs = await call("mobile_observe", {"session_id": sid})
                if not obs.get("observation_id"):
                    raise AcceptanceFailure("observation_unavailable")
                report["capture_ms"].append(obs["capture_ms"])
                return obs
            async def act(label, action, anchor, required=(), topic=None, check=None):
                report["active_step"] = label
                started = time.monotonic()
                refreshes = 0
                for attempt in range(4):
                    obs = await observe()
                    try:
                        result = await call("mobile_act", {"request": {
                            "session_id": sid, "request_id": str(uuid.uuid4()),
                            "observation_id": obs["observation_id"], "action": action,
                            "expected": {"text": anchor} if anchor else {"changed": True}, "timeout_ms": 15000}})
                        break
                    except ToolFailure as error:
                        # Runtime raises stale_observation before journal admission/dispatch.
                        # Re-resolve semantic selectors from fresh UI; never reuse action_id.
                        if error.code != "stale_observation" or attempt == 3:
                            raise
                        report.pop("tool_failure", None)
                        refreshes += 1
                        report["pre_dispatch_refreshes"] += 1
                after = result.get("observation", {})
                semantic = ({anchor, *required} if anchor else set(required)).issubset(texts(after))
                if check is not None:
                    semantic = semantic and check(after)
                if topic is not None:
                    semantic = semantic and topic_matches(texts(after), topic)
                row = {"step": label, "execution_status": result.get("execution_status"),
                       "verification_status": result.get("verification_status"),
                       "semantic_pass": semantic, "pre_dispatch_refreshes": refreshes, "elapsed_ms": round((time.monotonic()-started)*1000)}
                report["steps"].append(row)
                print(json.dumps(row), flush=True)
                if row["execution_status"] != "executed" or row["verification_status"] != "verified" or not semantic:
                    raise AcceptanceFailure("step_failed:" + label)
                report.pop("active_step", None)
                return after
            def tap(text):
                return {"kind": "tap", "target": {"text": text}}
            try:
                obs = await observe()
                if args.profile == "video":
                    if not video_identity(obs):
                        if not {"首页", "发现", "消息", "我"}.issubset(texts(obs)):
                            raise AcceptanceFailure("open_weibo_video_or_main_first")
                        obs = await act("open_video", tap("视频"), "短剧", ("推荐",), check=lambda o: bool(video_identity(o)))
                    for index in range(args.rounds):
                        start = time.monotonic()
                        before = await observe()
                        await asyncio.sleep(.5)
                        after = await observe()
                        same_clip = video_identity(before) == video_identity(after) and bool(video_identity(after))
                        advancing = video_progress(after) > video_progress(before) >= 0
                        report["playback_samples"].append({"round": index+1, "same_clip": same_clip, "progress_advanced": advancing})
                        if not same_clip or not advancing:
                            raise AcceptanceFailure("playback_progress_unproven")
                        identity = video_identity(after)
                        await act("swipe_next_video", {"kind": "swipe", "direction": "up"}, None,
                                  ("短剧", "推荐"), check=lambda o: bool(video_identity(o)) and video_identity(o) != identity)
                        report["rounds"].append({"round": index+1, "passed": True, "elapsed_ms": round((time.monotonic()-start)*1000)})
                        print(json.dumps(report["rounds"][-1]), flush=True)
                    report["status"] = "passed"
                    return
                # Normalize only known Weibo screens, never navigate blindly.
                for _ in range(3):
                    present = texts(obs)
                    if {"首页", "发现", "消息", "我"}.issubset(present):
                        break
                    if TECH in present or GENERAL in present:
                        obs = await act("setup_back_to_discovery", {"kind": "back"}, "更多热搜")
                    elif {"综合", "实时", "视频"}.issubset(present):
                        obs = await act("setup_back_to_technology", {"kind": "back"}, TECH)
                    else:
                        raise AcceptanceFailure("open_weibo_main_or_hot_list_first")
                else:
                    raise AcceptanceFailure("setup_navigation_limit")
                await act("setup_home", tap("首页"), "推荐", ("发现",))
                for index in range(args.rounds):
                    start = time.monotonic()
                    await act("discovery", tap("发现"), "更多热搜")
                    await act("hot_list", tap("更多热搜"), GENERAL, ("科技",))
                    obs = await act("technology", tap("科技"), TECH, ("热搜",))
                    # A topic is a text immediately followed by a numeric heat count.
                    # Select it from current evidence; never retain the topic in the report.
                    nodes = obs.get("catalog", [])
                    candidates = [n for n, following in zip(nodes, nodes[1:])
                                  if n.get("type") == "Text" and len(n.get("text", "")) >= 4
                                  and n.get("enabled") and following.get("resource_id") == "0"
                                  and following.get("text", "").isdigit()]
                    if not candidates:
                        raise AcceptanceFailure("no_observed_topic_candidate")
                    topic = candidates[0]["text"]
                    await act("topic_results", tap(topic), "综合", ("实时", "视频", "图片"), topic=topic)
                    await act("return_technology", {"kind": "back"}, TECH)
                    await act("general_category", tap("热搜"), GENERAL)
                    await act("return_discovery", {"kind": "back"}, "更多热搜")
                    await act("return_home", tap("首页"), "推荐", ("发现",))
                    report["rounds"].append({"round": index+1, "passed": True,
                                             "elapsed_ms": round((time.monotonic()-start)*1000)})
                    print(json.dumps(report["rounds"][-1]), flush=True)
                report["status"] = "passed"
            finally:
                await call("mobile_session", {"operation": "close", "session_id": sid})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("hot", "video"), default="hot")
    parser.add_argument("--execute", action="store_true", help="Explicitly allow real navigation")
    parser.add_argument("--state-dir", type=Path, default=Path(".runtime/acceptance"))
    parser.add_argument("--rounds", type=int, default=10, choices=range(1, 31))
    parser.add_argument("--report", type=Path, default=Path(".runtime/weibo-acceptance.json"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("Real device navigation requires --execute")
    report = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "scenario": "weibo_video_progress_and_swipe" if args.profile == "video" else "weibo_discovery_hot_technology_topic_return", "requested_rounds": args.rounds,
              "status": "failed", "playback_samples": [], "pre_dispatch_refreshes": 0, "steps": [], "rounds": [], "capture_ms": [],
              "scope": "same-app semantic navigation; not autonomous planning or release acceptance"}
    try:
        asyncio.run(run(args, report))
    except Exception as error:
        report["status"] = "failed"
        report["failure_type"] = type(error).__name__
        # Only locally authored failure strings; exclude third-party exception details.
        pending = [error]
        while pending:
            failure = pending.pop()
            if isinstance(failure, BaseExceptionGroup):
                pending.extend(failure.exceptions)
            elif isinstance(failure, AcceptanceFailure):
                report["failure_code"] = str(failure)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["observation_latency"] = stats(report.pop("capture_ms"))
        report["action_latency"] = stats([r["elapsed_ms"] for r in report["steps"]])
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("steps", "rounds")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
