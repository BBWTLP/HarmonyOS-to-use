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
from contextlib import asynccontextmanager

from harmony_runtime.service import Client as ServiceClient
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.observation import catalog as tree_catalog, input_value_matches
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

WEIBO = "com.sina.weibo.stage"
MAIN_NAV = {"首页", "发现", "消息", "我"}

GENERAL = "实时热点，每分钟更新一次"
TECH = "科技热点，洞悉行业发展新趋势"


class _ServiceToolResult:
    def __init__(self, data=None, error=None):
        self.structured_content = data or {"error": error} if error else (data or {})
        self.is_error = error is not None


class _ServiceMcpAdapter:
    _METHODS = {
        "mobile_session": "session",
        "mobile_observe": "observe",
        "mobile_act": "act",
        "mobile_wait": "wait",
        "mobile_burst": "burst",
        "mobile_history": "history",
    }

    def __init__(self, root):
        self.client = ServiceClient(root)

    async def initialize(self):
        return None

    async def call_tool(self, name, payload):
        method = self._METHODS.get(name)
        if method is None:
            return _ServiceToolResult(error={"code": "invalid_arguments", "message": "Unknown tool"})
        try:
            # The stdio MCP server receives ActRequest/BurstRequest as a
            # single ``request`` object, while the authenticated service
            # exposes Runtime.act/burst with an ``arguments`` keyword. Keep
            # the adapter faithful to both transports so the acceptance
            # runner cannot accidentally send the wrapper object to Runtime.
            if name in ("mobile_act", "mobile_burst"):
                arguments = payload.get("request")
                if not isinstance(arguments, dict):
                    return _ServiceToolResult(error={
                        "code": "invalid_arguments",
                        "message": "request object is required",
                    })
                return _ServiceToolResult(self.client.call(method, arguments=arguments))
            return _ServiceToolResult(self.client.call(method, **payload))
        except RuntimeFault as error:
            return _ServiceToolResult(error={"code": error.code, "message": "runtime operation failed"})


@asynccontextmanager
async def _transport(args):
    if args.transport == "service":
        client = _ServiceMcpAdapter(args.state_dir.resolve())
        await client.initialize()
        yield client
        return
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "harmony_runtime.cli", "mcp", "--state-dir", str(args.state_dir.resolve())])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            yield client


class AcceptanceFailure(RuntimeError):
    pass


class ToolFailure(AcceptanceFailure):
    def __init__(self, tool, code):
        super().__init__("mcp_tool_error:" + tool)
        self.code = code


def require_weibo(obs):
    if obs.get("blocking_dialog"):
        raise AcceptanceFailure("authentication_required")
    if obs.get("foreground_bundle") != WEIBO or not obs.get("foreground_consistent"):
        raise AcceptanceFailure("weibo_foreground_unverified")
    if not obs.get("actionable"):
        raise AcceptanceFailure("capture_unstable")


def texts(obs):
    return {n.get("text", "") for n in obs.get("catalog", [])}


def is_weibo_profile_screen(present):
    """Recognize a profile detail page so a new run can return safely to main nav."""
    return ("返回" in present and "关注" in present
            and ("他的热门内容" in present or "联系" in present))


def topic_matches(observed, topic):
    """Only accept the exact topic or its visible hashtag wrapper."""
    return topic in observed or "#" + topic + "#" in observed


def video_identity(obs):
    descriptions = [n["text"] for n in obs.get("catalog", [])
                    if n.get("resource_id") == "0" and n.get("type") == "Text" and len(n.get("text", "")) >= 10]
    return descriptions[0] if len(descriptions) == 1 else None


def video_progress(obs):
    sliders = [n.get("text", "") for n in obs.get("catalog", []) if n.get("type") in ("Slider", "Progress")]
    return float(sliders[0]) if len(sliders) == 1 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", sliders[0]) else -1


def semantic_action_id(obs, text):
    """Resolve a visible label to its smallest clickable catalog ancestor."""
    nodes = obs.get("catalog", [])
    matches = [n for n in nodes if n.get("enabled") and n.get("text") == text]
    if not matches:
        raise AcceptanceFailure("label_not_observed")
    candidates = []
    for leaf in matches:
        bounds = leaf.get("bounds") or []
        if len(bounds) != 4:
            continue
        x1, y1, x2, y2 = bounds
        for node in nodes:
            nb = node.get("bounds") or []
            if not (node.get("enabled") and node.get("clickable") and len(nb) == 4):
                continue
            nx1, ny1, nx2, ny2 = nb
            if nx1 <= x1 and ny1 <= y1 and nx2 >= x2 and ny2 >= y2:
                candidates.append((max(1, (nx2-nx1)*(ny2-ny1)), node.get("action_id")))
    candidates = [(area, action_id) for area, action_id in candidates if action_id]
    if not candidates:
        raise AcceptanceFailure("clickable_label_parent_unavailable")
    return min(candidates)[1]


def focused_input_action_id(obs):
    inputs = [n for n in obs.get("catalog", [])
              if n.get("enabled") and n.get("focused") and "input" in n.get("type", "").lower()]
    if len(inputs) != 1 or not inputs[0].get("action_id"):
        raise AcceptanceFailure("focused_search_input_unavailable")
    return inputs[0]["action_id"]


def search_suggestion_action_id(obs, text):
    """Use the observed suggestion row, never a same-named account card.

    Current-device Weibo suggestions are clickable Columns under a List.
    Account cards use Rows. Unknown layouts fail closed rather than falling
    back to coordinates, smallest enclosing bounds, or the first text match.
    """
    require_weibo(obs)
    if not is_search_editor(obs):
        raise AcceptanceFailure("search_editor_unavailable")
    nodes = obs.get("catalog", [])
    indexed = {n["action_id"]: n for n in nodes if n.get("action_id")}
    if len(indexed) != len(nodes):
        raise AcceptanceFailure("search_catalog_identity_invalid")
    candidates = set()
    for leaf in nodes:
        if not (leaf.get("enabled") and leaf.get("type") == "Text"
                and leaf.get("text") == text and leaf.get("bundle") == WEIBO):
            continue
        row = indexed.get(leaf.get("parent_action_id"), {})
        container = indexed.get(row.get("parent_action_id"), {})
        if (row.get("type") == "Column" and row.get("enabled") and row.get("clickable")
                and row.get("bundle") == WEIBO and container.get("type") == "List"
                and container.get("enabled") and container.get("bundle") == WEIBO):
            candidates.add(row["action_id"])
    if len(candidates) != 1:
        raise AcceptanceFailure("search_suggestion_ambiguous_or_unavailable")
    return candidates.pop()


def search_tab_node(obs, label):
    """Ground a result tab by ancestry and the observed Tabs header bounds."""
    nodes = obs.get("catalog", [])
    tabs = [n for n in nodes if n.get("type") == "Tabs" and n.get("resource_id") == "tab"
            and n.get("enabled") and n.get("bundle") == WEIBO and len(n.get("bounds", [])) == 4]
    indexed = {n.get("action_id"): n for n in nodes}
    if None in indexed or len(indexed) != len(nodes):
        raise AcceptanceFailure("search_catalog_identity_invalid")
    candidates = {}
    if len(tabs) != 1:
        raise AcceptanceFailure("search_tabs_unavailable")
    for leaf in nodes:
        row = indexed.get(leaf.get("parent_action_id"), {})
        bounds = row.get("bounds") or []
        if (leaf.get("text") == label and leaf.get("enabled") and leaf.get("type") == "Text"
                and leaf.get("bundle") == WEIBO and row.get("type") == "Column"
                and row.get("bundle") == WEIBO and row.get("clickable") and row.get("enabled")
                and row.get("action_id") and len(bounds) == 4 and bounds[1] == tabs[0]["bounds"][1]):
            candidates[row["action_id"]] = row
    if len(candidates) != 1:
        raise AcceptanceFailure("search_tab_ambiguous_or_unavailable")
    return next(iter(candidates.values()))


def search_result_selected(obs, query, label):
    try:
        require_weibo(obs)
        tab = search_tab_node(obs, label)
    except AcceptanceFailure:
        return False
    query_in_header = any(n.get("text") == query and n.get("type") == "Text"
                          and n.get("bundle") == WEIBO and n.get("enabled")
                          and len(n.get("bounds", [])) == 4 and n["bounds"][3] <= tab["bounds"][1]
                          for n in obs.get("catalog", []))
    return tab.get("selected") in (True, "true") and query_in_header


async def settle_observation(after, observe, predicate, timeout_ms):
    """Read-only settling; never dispatch or replay the preceding action."""
    deadline = time.monotonic() + timeout_ms / 1000
    reads = 0
    while not predicate(after) and time.monotonic() < deadline:
        await asyncio.sleep(.25)
        after = await observe()
        reads += 1
        require_weibo(after)
    return after, reads


def is_search_editor(obs):
    """Recognize Weibo's search editor from structure, not localized labels."""
    inputs = [n for n in obs.get("catalog", [])
              if n.get("enabled") and n.get("focused") and n.get("type") == "TextInput"]
    lists = [n for n in obs.get("catalog", []) if n.get("type") == "List" and n.get("enabled")]
    return len(inputs) == 1 and bool(lists)


def full_tree(tree):
    """Read service trees and lossless MCP flat_tree_v1 without losing ancestry."""
    if not isinstance(tree, dict):
        raise AcceptanceFailure("strict_input_tree_unavailable")
    if "format" not in tree:
        if not isinstance(tree.get("attributes"), dict):
            raise AcceptanceFailure("strict_input_tree_unavailable")
        return tree
    if tree.get("format") != "flat_tree_v1" or not isinstance(tree.get("nodes"), list) or not tree["nodes"]:
        raise AcceptanceFailure("strict_input_tree_invalid")
    nodes = {}
    for index, item in enumerate(tree["nodes"]):
        if not isinstance(item, dict):
            raise AcceptanceFailure("strict_input_tree_invalid")
        nid, parent, data = item.get("node_id"), item.get("parent_id"), item.get("data")
        if (not isinstance(nid, str) or not nid or nid in nodes or not isinstance(data, dict)
                or "children" in data or type(item.get("has_children_field")) is not bool):
            raise AcceptanceFailure("strict_input_tree_invalid")
        node = dict(data)
        if item["has_children_field"]:
            node["children"] = []
        if index == 0:
            if parent is not None or nid != tree.get("root_id"):
                raise AcceptanceFailure("strict_input_tree_invalid")
        elif not isinstance(parent, str) or parent not in nodes or "children" not in nodes[parent]:
            raise AcceptanceFailure("strict_input_tree_invalid")
        else:
            nodes[parent]["children"].append(node)
        nodes[nid] = node
    return nodes[tree["root_id"]]


def raw_input_target(obs):
    """Anchor the focused field in FULL raw evidence, never in placeholder text."""
    require_weibo(obs)
    if not is_search_editor(obs) or not isinstance(obs.get("tree"), dict):
        raise AcceptanceFailure("strict_input_tree_unavailable")
    display = obs.get("display", {})
    if any(type(display.get(k)) is not int or display[k] <= 0 for k in ("width", "height")):
        raise AcceptanceFailure("strict_input_display_unavailable")
    action_id = focused_input_action_id(obs)
    visible = [n for n in obs["catalog"] if n.get("action_id") == action_id]
    if len(visible) != 1:
        raise AcceptanceFailure("strict_input_target_ambiguous")
    raw = tree_catalog(full_tree(obs["tree"]), (display["width"], display["height"]))
    candidates = [n for n in raw if n.get("type") == "TextInput" and n.get("focused")
                  and n.get("bundle") == WEIBO and n.get("bounds") == visible[0].get("bounds")]
    if len(candidates) != 1 or not input_value_matches(raw, candidates[0], candidates[0]["text"]):
        raise AcceptanceFailure("strict_input_identity_unavailable")
    return candidates[0]


def strict_input_matches(obs, target, value):
    current = raw_input_target(obs)
    return input_value_matches([current], target, value)


def search_editor_cancel_action(obs):
    """Resolve the top-right search cancel control without assuming its locale."""
    bounds = [n.get("bounds") for n in obs.get("catalog", [])
              if isinstance(n.get("bounds"), list) and len(n.get("bounds")) == 4]
    right_edge = max((b[2] for b in bounds), default=0)
    candidates = []
    for node in obs.get("catalog", []):
        b = node.get("bounds") or []
        if (node.get("enabled") and node.get("clickable") and node.get("type") == "Text"
                and len(b) == 4 and b[1] < 300 and b[0] >= right_edge * 0.8
                and node.get("action_id")):
            candidates.append(node)
    if len(candidates) != 1:
        raise AcceptanceFailure("search_editor_cancel_unavailable")
    return {"kind": "tap", "target": {"action_id": candidates[0]["action_id"]}}


def stats(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"count": len(values), "p50_ms": ordered[math.ceil(len(values)*.5)-1],
            "p95_ms": ordered[math.ceil(len(values)*.95)-1], "max_ms": max(values)}


async def run(args, report):
    async with _transport(args) as client:
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
            strict_anchor = None
            async def observe(mode="FAST"):
                obs = await call("mobile_observe", {"session_id": sid, "mode": mode})
                if not obs.get("observation_id"):
                    raise AcceptanceFailure("observation_unavailable")
                report["capture_ms"].append(obs["capture_ms"])
                return obs
            async def act(label, action, anchor, required=(), topic=None, check=None, launching=False, settle_ms=0):
                nonlocal strict_anchor
                report["active_step"] = label
                started = time.monotonic()
                refreshes = 0
                for attempt in range(4):
                    strict_input = args.profile == "strict-input" and label.startswith("strict_input_")
                    obs = await observe("FULL" if strict_input else "FAST")
                    if not launching:
                        require_weibo(obs)
                    resolved_action = action(obs) if callable(action) else action
                    if strict_input:
                        current = raw_input_target(obs)
                        if strict_anchor is None:
                            strict_anchor = current
                        elif not input_value_matches([current], strict_anchor, current["text"]):
                            raise AcceptanceFailure("strict_input_target_changed")
                    try:
                        result = await call("mobile_act", {"request": {
                            "session_id": sid, "request_id": str(uuid.uuid4()),
                            "observation_id": obs["observation_id"], "action": resolved_action,
                            "expected": {"bundle": WEIBO, **({"text": anchor} if anchor else ({} if launching or resolved_action.get("kind") == "replace_text" else {"changed": True}))}, "timeout_ms": 15000}})
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
                def semantic_check(observation):
                    return (({anchor, *required} if anchor else set(required)).issubset(texts(observation))
                            and (check is None or check(observation))
                            and (topic is None or topic_matches(texts(observation), topic)))
                settle_reads = 0
                initial_semantic = semantic_check(after)
                validation_error = None
                pending_error = None
                raw_value_verified = False
                try:
                    require_weibo(after)
                    if (strict_input and result.get("execution_status") == "executed"
                            and result.get("verification_status") == "verified"):
                        # Additional read only: the resident service may predate
                        # strict value validation. Never replay a dispatched write.
                        after = await observe("FULL")
                        if not strict_input_matches(after, strict_anchor, resolved_action["text"]):
                            raise AcceptanceFailure("strict_input_value_mismatch")
                        raw_value_verified = True
                    if (settle_ms and result.get("execution_status") == "executed"
                            and result.get("verification_status") == "verified"):
                        after, settle_reads = await settle_observation(after, observe, semantic_check, settle_ms)
                except AcceptanceFailure as error:
                    validation_error = str(error)
                    pending_error = error
                    settle_reads = None
                except Exception as error:
                    # Keep the already-dispatched outcome even if a later read
                    # fails. Never hide a write behind a settling transport error.
                    validation_error = "settle_observation_failed"
                    pending_error = error
                    settle_reads = None
                semantic = (validation_error is None
                            and ({anchor, *required} if anchor else set(required)).issubset(texts(after)))
                if check is not None:
                    semantic = semantic and check(after)
                if topic is not None:
                    semantic = semantic and topic_matches(texts(after), topic)
                row = {"step": label, "execution_status": result.get("execution_status"),
                       "verification_status": result.get("verification_status"),
                       "foreground_matches": after.get("foreground_bundle") == WEIBO,
                       "validation_error": validation_error,
                       "semantic_pass": semantic, "initial_semantic_pass": initial_semantic,
                       "settle_reads": settle_reads, "pre_dispatch_refreshes": refreshes, "elapsed_ms": round((time.monotonic()-started)*1000)}
                report["steps"].append(row)
                if strict_input:
                    row["raw_value_verified"] = raw_value_verified
                print(json.dumps(row), flush=True)
                if pending_error is not None:
                    raise pending_error
                if validation_error:
                    raise AcceptanceFailure(validation_error)
                if row["execution_status"] != "executed" or row["verification_status"] != "verified" or not semantic:
                    raise AcceptanceFailure("step_failed:" + label)
                report.pop("active_step", None)
                return after
            def tap(text):
                return lambda obs: {"kind": "tap", "target": {"action_id": semantic_action_id(obs, text)}}
            def replace_text(value):
                return lambda obs: {"kind": "replace_text", "target": {"action_id": focused_input_action_id(obs)}, "text": value}
            def search_bar(obs):
                nodes = [n for n in obs.get("catalog", []) if n.get("enabled") and n.get("clickable")
                         and n.get("type") == "Flex" and n.get("bounds", [0, 999, 0, 0])[1] < 300]
                if len(nodes) != 1 or not nodes[0].get("action_id"):
                    raise AcceptanceFailure("search_bar_unavailable")
                return {"kind": "tap", "target": {"action_id": nodes[0]["action_id"]}}
            try:
                obs = await act("launch_weibo", {"kind": "launch", "bundle": WEIBO}, None, launching=True)
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
                if args.profile in ("search", "strict-input"):
                    for index in range(args.rounds):
                        start = time.monotonic()
                        for setup in range(5):
                            present = texts(obs)
                            if MAIN_NAV.issubset(present):
                                obs = await act("search_setup_discovery", tap("发现"), "更多热搜")
                                obs = await act("open_search", search_bar, None,
                                    check=lambda o: any(n.get("type") == "TextInput" and n.get("focused") for n in o.get("catalog", [])))
                                break
                            if any(n.get("type") == "TextInput" and n.get("focused") for n in obs.get("catalog", [])):
                                break
                            if is_weibo_profile_screen(present):
                                obs = await act("search_setup_back", {"kind": "back"}, None)
                                continue
                            if ({"综合", "实时", "视频"}.issubset(present)
                                    or TECH in present or GENERAL in present
                                    or "取消" in present):
                                obs = await act("search_setup_back", {"kind": "back"}, None)
                            else:
                                raise AcceptanceFailure("search_setup_screen_unknown")
                        else:
                            raise AcceptanceFailure("search_setup_navigation_limit")
                        if args.profile == "strict-input":
                            for label, value in (("replace", "鸿蒙"), ("same_value", "鸿蒙"), ("clear", "")):
                                obs = await act("strict_input_" + label, replace_text(value), None)
                            report["rounds"].append({"round": index+1, "passed": True,
                                "elapsed_ms": round((time.monotonic()-start)*1000)})
                            print(json.dumps(report["rounds"][-1]), flush=True)
                            continue
                        query = "鸿蒙"
                        suggestion = "鸿蒙智行"
                        obs = await act("input_search_query", replace_text(query), None,
                            check=lambda o: any(n.get("type") == "TextInput" and n.get("text") == query for n in o.get("catalog", [])))
                        obs = await act("select_search_suggestion",
                            lambda o: {"kind": "tap", "target": {"action_id": search_suggestion_action_id(o, suggestion)}}, suggestion,
                            required=("综合", "实时", "视频", "图片"), settle_ms=10000,
                            check=lambda o: search_result_selected(o, suggestion, "综合"))
                        obs = await act("search_video_tab",
                            lambda o: {"kind": "tap", "target": {"action_id": search_tab_node(o, "视频")["action_id"]}},
                            "视频", required=(suggestion,), settle_ms=10000,
                            check=lambda o: search_result_selected(o, suggestion, "视频") and any(n.get("resource_id") == "0" and n.get("type") == "Text" and len(n.get("text", "")) >= 8 for n in o.get("catalog", [])))
                        obs = await act("search_realtime_tab",
                            lambda o: {"kind": "tap", "target": {"action_id": search_tab_node(o, "实时")["action_id"]}},
                            "实时", required=(suggestion, "互动", "时间"), settle_ms=10000,
                            check=lambda o: search_result_selected(o, suggestion, "实时") and "按最新互动排序" in texts(o))
                        report["rounds"].append({"round": index+1, "passed": True,
                            "elapsed_ms": round((time.monotonic()-start)*1000)})
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
                    elif is_search_editor(obs):
                        # A prior run may leave the device in the search editor.
                        # Exit through the observed cancel control; do not use a
                        # coordinate or localized string that is not in evidence.
                        obs = await act("setup_cancel_search", search_editor_cancel_action,
                                        None, required=MAIN_NAV)
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
    parser.add_argument("--profile", choices=("hot", "video", "search", "strict-input"), default="hot")
    parser.add_argument("--execute", action="store_true", help="Explicitly allow real navigation")
    parser.add_argument("--state-dir", type=Path, default=Path(".runtime/acceptance"))
    parser.add_argument("--transport", choices=("service", "stdio"), default="service",
                        help="Both transports use the resident service; stdio verifies the agent MCP boundary")
    parser.add_argument("--rounds", type=int, default=10, choices=range(1, 31))
    parser.add_argument("--report", type=Path, default=Path(".runtime/weibo-acceptance.json"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("Real device navigation requires --execute")
    report = {"schema_version": 1, "transport": args.transport, "started_at": datetime.now(timezone.utc).isoformat(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "scenario": ("weibo_video_progress_and_swipe" if args.profile == "video" else
                           "weibo_strict_same_field_replace_repeat_clear" if args.profile == "strict-input" else
                           "weibo_search_input_and_result_tabs" if args.profile == "search" else
                           "weibo_discovery_hot_technology_topic_return"), "requested_rounds": args.rounds,
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
