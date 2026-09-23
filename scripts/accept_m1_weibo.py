"""M1 acceptance: ten low-risk Weibo tasks, three runs each, on the real device.

The delegating client authors each subgoal plan (goal + semantic steps +
success criteria). Every step re-grounds its target from a fresh observation
through the runtime service, and the verdict comes from programme checks over a
new observation, never from the client's own claim.

Reports contain metadata only: no UI text, screenshots, or input values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_harness import (AgentHarness, HarnessError, ServiceClientHarness, WEIBO,
                           clickable_node, editor_input, find_node, find_search_bar,
                           focused_input, post_observation, surface_kind)
from harmony_agent.checker import check
from harmony_agent.contracts import Predicate

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = REPO_ROOT / "evals" / "tasks" / "m1-weibo.json"

DISCOVER_TAB = "发现"
HOME_TAB = "首页"
SCROLL_TOP_TAB = "回到顶部"
SETTLE_SECONDS = 0.4
MAX_STEP_REOBSERVES = 2
MAX_INNER_ATTEMPTS = 4

#: Device/service-level failures that end a run before any dispatch. They are
#: recorded as a blocked run (with the code) instead of aborting the batch, so a
#: dark screen or a lost service cannot destroy the other runs' evidence.
BATCH_FATAL_CODES = (
    "screen_locked", "screen_state_unknown", "screen_unavailable",
    "wake_unlock_unconfirmed", "runtime_unavailable", "runtime_transport_error",
    "device_unavailable", "device_quarantined", "device_busy", "timeout",
    "client_invalid", "session_invalid", "session_reopen_required",
)

#: How many consecutive device-level failures stop the batch cleanly.
MAX_CONSECUTIVE_DEVICE_FAILURES = 3


class TaskBlocked(HarnessError):
    pass


@dataclass
class DeviceFailureTracker:
    """Decide when a batch must stop because the *device*, not the task, failed.

    A long unattended batch can lose the device (screen locked, service gone).
    The batch must then write what it has and stop cleanly: continuing would
    produce a long row of blocked runs that hides the real cause, and aborting
    without a report loses the evidence already collected.
    """

    limit: int = MAX_CONSECUTIVE_DEVICE_FAILURES
    consecutive: int = 0

    def record(self, code: str | None) -> str | None:
        """Feed one run's failure code; return a stop reason when the batch ends."""
        if code is None:
            self.consecutive = 0
            return None
        if code not in BATCH_FATAL_CODES:
            return None
        self.consecutive += 1
        if self.consecutive >= self.limit:
            return (f"stopped after {self.consecutive} consecutive device failures; "
                    "device/service state must be restored before resuming")
        return None


async def settle() -> None:
    await asyncio.sleep(SETTLE_SECONDS)


# -- entry states ------------------------------------------------------------

async def restore_discover_label(harness: AgentHarness, observation: dict) -> dict:
    """The middle bottom tab flips to 回到顶部 after scroll; tap it to restore 发现."""
    if find_node(observation, text=DISCOVER_TAB) is not None:
        return observation
    scroll_top = find_node(observation, text=SCROLL_TOP_TAB)
    if scroll_top is None:
        return observation
    node = clickable_node(observation, scroll_top)
    await settle()
    result = await harness.act(
        observation_id=observation["observation_id"],
        action={"kind": "tap", "target": {"action_id": node["action_id"]}},
        expected={"changed": True}, timeout_ms=10000)
    return post_observation(result) or await harness.observe(mode="FAST")


async def goto_tabs(harness: AgentHarness) -> dict:
    """Reset helper: return to a page showing the bottom navigation."""
    last: dict | None = None
    fresh: dict | None = None
    for _ in range(8):
        # The runtime already captured the device to verify the last action;
        # consume that capture instead of buying a second look at the same page.
        observation = fresh or await harness.stable_observation()
        fresh = None
        last = observation
        if observation.get("foreground_bundle") != WEIBO:
            result = await harness.act(observation_id=observation["observation_id"],
                                       action={"kind": "launch", "bundle": WEIBO},
                                       expected={"bundle": WEIBO}, timeout_ms=15000)
            fresh = post_observation(result)
            continue
        kind = surface_kind(observation)
        if kind == "tabs":
            return await restore_discover_label(harness, observation)
        node = find_node(observation, text=HOME_TAB)
        try:
            if node is not None:
                result = await harness.act(observation_id=observation["observation_id"],
                                           action={"kind": "tap", "target": {"text": HOME_TAB}},
                                           expected={"changed": True}, timeout_ms=10000)
            else:
                result = await harness.act(observation_id=observation["observation_id"],
                                           action={"kind": "back"},
                                           expected={"changed": True}, timeout_ms=10000)
            fresh = post_observation(result)
        except HarnessError:
            await asyncio.sleep(0.8)
            continue
        if fresh is None:
            await asyncio.sleep(0.5)
    if last is not None and last.get("foreground_bundle") == WEIBO:
        return last
    raise TaskBlocked("home_unavailable", "Could not reach a Weibo tab page")


async def goto_editor(harness: AgentHarness) -> dict:
    for _ in range(8):
        observation = await harness.stable_observation()
        field = editor_input(observation)
        if field is not None:
            if field.get("focused"):
                return observation
            try:
                await harness.act(
                    observation_id=observation["observation_id"],
                    action={"kind": "tap", "target": {"action_id": field["action_id"]}},
                    expected={"changed": True}, timeout_ms=10000)
            except HarnessError:
                pass
            await asyncio.sleep(0.5)
            continue
        if surface_kind(observation) == "discover":
            bar = find_search_bar(observation)
            if bar is None:
                await asyncio.sleep(0.8)
                continue
            try:
                await harness.act(
                    observation_id=observation["observation_id"],
                    action={"kind": "tap", "target": {"action_id": bar["action_id"]}},
                    expected={"changed": True}, timeout_ms=10000)
            except HarnessError:
                pass
            await asyncio.sleep(0.6)
            continue
        tab = find_node(observation, text=DISCOVER_TAB)
        if tab is None:
            observation = await goto_tabs(harness)
            tab = find_node(observation, text=DISCOVER_TAB)
            if tab is None:
                continue
        try:
            await harness.act(observation_id=observation["observation_id"],
                              action={"kind": "tap", "target": {"text": DISCOVER_TAB}},
                              expected={"changed": True}, timeout_ms=10000)
        except HarnessError:
            pass
        await asyncio.sleep(0.6)
    raise TaskBlocked("editor_unavailable", "Could not reach the Weibo search editor")


async def setup_entry(harness: AgentHarness, entry: str) -> dict:
    if entry == "editor":
        return await goto_editor(harness)
    return await goto_tabs(harness)


# -- steps -------------------------------------------------------------------

async def run_step(harness: AgentHarness, step: str,
                   observable: dict) -> tuple[dict, dict | None]:
    """Execute one semantic step, re-grounding the target from a fresh observation.

    Returns ``(record, post_observation)``. ``post_observation`` is the capture
    the runtime itself made to verify this step; when it is present the caller
    uses it as the next step's input instead of observing the same page again.
    """
    kind, _, payload = step.partition(":")
    started = time.perf_counter()
    post: dict | None = None
    if kind == "tap":
        observation = observable
        if surface_kind(observation) != "tabs":
            observation = await goto_tabs(harness)
        if payload == DISCOVER_TAB:
            observation = await restore_discover_label(harness, observation)
        labels = [n for n in observation.get("catalog") or [] if n.get("text") == payload]
        bottom = [n for n in labels if (n.get("bounds") or [0, 0, 0, 0])[1] > 2400]
        if len(bottom) == 1:
            node = clickable_node(observation, bottom[0])
        elif len(labels) == 1:
            node = clickable_node(observation, labels[0])
        elif len(bottom) > 1:
            # Tightest clickable container among duplicate tab labels.
            by_id = {n["action_id"]: n for n in observation.get("catalog") or [] if n.get("action_id")}
            parents = []
            for item in bottom:
                parent = clickable_node(observation, item)
                if parent.get("action_id"):
                    parents.append(parent)
            uniq = {p["action_id"]: p for p in parents}
            if uniq:
                node = min(uniq.values(), key=lambda n: (
                    (n.get("bounds") or [0, 0, 0, 0])[2] - (n.get("bounds") or [0, 0, 0, 0])[0]))
            else:
                raise TaskBlocked("target_missing",
                                  f"{payload} is not uniquely observable ({len(labels)} labels)")
        else:
            raise TaskBlocked("target_missing",
                              f"{payload} is not uniquely observable ({len(labels)} labels)")
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"action_id": node["action_id"]}},
            expected={"changed": True}, timeout_ms=10000)
    elif kind == "action_id":
        # The Discover page animates (autoplaying video), so the search entry is
        # re-grounded in a bounded loop until the runtime accepts the view.
        result = None
        last_error: HarnessError | None = None
        for attempt in range(MAX_INNER_ATTEMPTS):
            observation = observable if surface_kind(observable) == "discover" else None
            if observation is None:
                observation = await harness.observe(mode="FAST")
            if surface_kind(observation) != "discover":
                observation = await goto_tabs(harness)
                tab = find_node(observation, text=DISCOVER_TAB)
                if tab is None:
                    raise TaskBlocked("discover_unavailable", "Discover tab is not observable")
                try:
                    await settle()
                    moved = await harness.act(
                        observation_id=observation["observation_id"],
                        action={"kind": "tap", "target": {"text": DISCOVER_TAB}},
                        expected={"changed": True}, timeout_ms=10000)
                except HarnessError as error:
                    moved = None
                    last_error = error
                observable = post_observation(moved) or await harness.observe(mode="FAST")
                continue
            node = find_search_bar(observation)
            if node is None:
                observable = await harness.observe(mode="FAST")
                continue
            try:
                # The Discover page autoplays a video; give the layout time to
                # settle before sampling so the guard accepts the view.
                await asyncio.sleep(1.2)
                observation = await harness.observe(mode="FAST")
                node = find_search_bar(observation)
                if node is None:
                    observable = observation
                    continue
                node = clickable_node(observation, node)
                result = await harness.act(
                    observation_id=observation["observation_id"],
                    action={"kind": "tap", "target": {"action_id": node["action_id"]}},
                    expected={"changed": True}, timeout_ms=10000)
                break
            except HarnessError as error:
                last_error = error
                await asyncio.sleep(1.5)
                observable = await harness.observe(mode="FAST")
        if result is None:
            raise last_error or TaskBlocked("search_bar_missing",
                                            "Search bar is not observable")
    elif kind == "tap_focused_input":
        observation = observable
        field = focused_input(observation) or editor_input(observation)
        if field is None:
            observation = await goto_editor(harness)
            field = focused_input(observation) or editor_input(observation)
        if field is None:
            raise TaskBlocked("input_missing", "No input field is observable")
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"action_id": field["action_id"]}},
            expected={"changed": True}, timeout_ms=10000)
    elif kind == "replace_text":
        observation = observable
        field = focused_input(observation) or editor_input(observation)
        if field is None:
            observation = await goto_editor(harness)
            field = focused_input(observation) or editor_input(observation)
        if field is None:
            raise TaskBlocked("input_missing", "No input field is observable")
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "replace_text", "target": {"action_id": field["action_id"]},
                    "text": payload},
            expected=None, timeout_ms=15000)
    elif kind == "back":
        observation = observable
        if surface_kind(observation) != "search_editor":
            observation = await goto_editor(harness)
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "back"}, expected={"changed": True}, timeout_ms=10000)
    elif kind == "submit_search":
        # The editor's submit control is the rightmost labelled, clickable node in
        # the top band; the field itself is excluded and the text is not unique.
        observation = observable
        if surface_kind(observation) != "search_editor":
            observation = await goto_editor(harness)
        candidates = [node for node in observation.get("catalog", [])
                      if node.get("clickable") and node.get("enabled")
                      and node.get("text") and node.get("type") != "TextInput"
                      and (node.get("bounds") or [0, 9999, 0, 0])[1] < 300]
        if not candidates:
            raise TaskBlocked("submit_ambiguous",
                              "no labelled clickable control in the top band")
        candidates.sort(key=lambda node: (node.get("bounds") or [0, 0, 0, 0])[2])
        target_node = candidates[-1]
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"action_id": target_node["action_id"]}},
            expected={"changed": True}, timeout_ms=10000)
    elif kind == "tap_tab":
        # A bottom-navigation label can appear more than once (badges, page
        # titles), so resolve the label's clickable ancestor instead of the text.
        observation = observable
        if surface_kind(observation) != "tabs":
            observation = await goto_tabs(harness)
        by_id = {node["action_id"]: node for node in observation.get("catalog", [])}
        labels = [node for node in observation.get("catalog", [])
                  if node.get("text") == payload]
        parents = {node.get("parent_action_id") for node in labels
                   if node.get("parent_action_id") in by_id}
        if not parents:
            raise TaskBlocked("tab_ambiguous",
                              f"no clickable ancestor for {payload}")
        # Prefer the tightest clickable container: the tab itself, not the bar.
        def area(node):
            bounds = node.get("bounds") or [0, 0, 0, 0]
            return (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        parent = min((by_id[item] for item in parents), key=area)
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"action_id": parent["action_id"]}},
            expected={"changed": True}, timeout_ms=10000)
    elif kind == "tap_first_suggestion":
        # Weibo's search editor has no submit button: the top-band control is
        # cancel, and submission happens by choosing a suggestion row. Rows are
        # clickable Columns under an enabled List (structure taken from the
        # existing regression adapter), so this never falls back to coordinates.
        observation = observable
        if surface_kind(observation) != "search_editor":
            observation = await goto_editor(harness)
        indexed = {node["action_id"]: node for node in observation.get("catalog", [])
                   if node.get("action_id")}
        rows = [node for node in observation.get("catalog", [])
                if node.get("type") == "Column" and node.get("clickable")
                and node.get("enabled")
                and (indexed.get(node.get("parent_action_id")) or {}).get("type") == "List"]
        if not rows:
            raise TaskBlocked("suggestion_unavailable",
                              "no clickable suggestion row under a List")
        rows.sort(key=lambda node: (node.get("bounds") or [0, 0, 0, 0])[1])
        row = rows[0]
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"action_id": row["action_id"]}},
            expected={"changed": True}, timeout_ms=10000)
    elif kind == "swipe":
        observation = await goto_tabs(harness)
        await settle()
        result = await harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "swipe", "direction": payload or "up"},
            expected={"changed": True}, timeout_ms=10000)
    else:
        raise TaskBlocked("unknown_step", f"Unsupported step {step!r}")
    post = post_observation(result)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return ({"step": step, "ms": elapsed, "status": result.get("status"),
             "execution_status": result.get("execution_status"),
             "verification_status": result.get("verification_status"),
             "request_id": result.get("request_id"),
             "post_observation_reused": post is not None}, post)


# -- judging -----------------------------------------------------------------

def judge(criteria: list[dict], observation: dict, arguments: dict[str, str],
          baseline: dict | None, incident_free: bool) -> dict:
    predicates = [Predicate.model_validate(item) for item in criteria]
    report = check(predicates, observation, arguments=arguments, baseline=baseline,
                   surface_classifier=surface_kind, incident_free=incident_free)
    return report.to_dict()


async def run_once(harness: AgentHarness, task: dict) -> dict:
    started = time.time()
    outcome: dict = {"task_id": task["id"], "goal": task["goal"], "steps": [],
                     "dispatches": 0, "reobserves": 0, "status": "not_ready"}
    baseline: dict | None = None
    try:
        await goto_tabs(harness)
        entry = await setup_entry(harness, task.get("entry", "tabs"))
        baseline = await harness.observe(mode="FAST")
        observable = entry
        for step in task["steps"]:
            record = None
            for attempt in range(MAX_STEP_REOBSERVES + 1):
                try:
                    record, post = await run_step(harness, step, observable)
                    break
                except HarnessError as error:
                    # A staleness refusal happens before dispatch, so a bounded
                    # re-observe refreshes the view instead of replaying a write.
                    if error.code != "stale_observation" or attempt == MAX_STEP_REOBSERVES:
                        raise
                    outcome["reobserves"] += 1
                    observable = await harness.observe(mode="FAST")
            assert record is not None
            outcome["steps"].append(record)
            if record.get("execution_status") == "executed":
                outcome["dispatches"] += 1
            if record.get("execution_status") == "unknown":
                raise TaskBlocked("execution_unknown", "A dispatch has unknown execution")
            # The runtime already captured the device after the dispatch; reuse
            # that capture instead of a second look at the same page.
            observable = post or await harness.observe(mode="FAST")
    except HarnessError as error:
        outcome["blocked"] = {"code": error.code, "message": str(error)[:200]}
    final = await harness.observe(mode="FAST")
    status = await harness.session_status()
    outcome["verdict"] = judge(task["success_criteria"], final,
                               task.get("arguments", {}), baseline,
                               incident_free=not status.get("unresolved_actions"))
    if outcome.get("blocked"):
        outcome["status"] = "blocked"
    elif outcome["verdict"]["verdict"] == "pass":
        outcome["status"] = "succeeded"
    elif outcome["verdict"]["verdict"] == "fail":
        outcome["status"] = "failed"
    else:
        outcome["status"] = "inconclusive"
    outcome["elapsed_seconds"] = round(time.time() - started, 3)
    return outcome


async def main(args) -> int:
    definition = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    tasks = definition["tasks"]
    if args.only:
        tasks = [task for task in tasks if task["id"] in set(args.only)]
    report: dict = {"schema_version": 1, "milestone": "M1",
                    "app": definition["app"],
                    "scope": "10 low-risk Weibo tasks, 3 runs each, real device",
                    "runs_per_task": args.runs, "runs": [], "status": "not_ready"}
    started = time.time()
    device_failures = DeviceFailureTracker()
    harness_class = ServiceClientHarness if args.transport == "service" else AgentHarness
    harness_kwargs = ({"state_dir": args.state_dir} if args.transport == "service"
                      else {"state_dir": args.state_dir, "agent_tools": False})
    async with harness_class(**harness_kwargs) as harness:
        print(json.dumps({"step": "connected", "tools": harness.tools}), flush=True)
        opened = await harness.open(args.device_id)
        print(json.dumps({"step": "session_open", "device_bound":
                          bool(opened.get("device_id"))}), flush=True)
        report["session"] = {"device_bound": bool(opened.get("device_id")),
                             "controller_epoch": opened.get("controller_epoch")}
        for task in tasks:
            for run_index in range(1, args.runs + 1):
                device_failure = None
                try:
                    outcome = await run_once(harness, task)
                except HarnessError as error:
                    # A device/service failure (e.g. the phone screen went dark
                    # during a long unattended batch) must be recorded, not
                    # allowed to abort the remaining runs without a report.
                    device_failure = error
                    outcome = {"task_id": task["id"], "goal": task["goal"],
                               "steps": [], "dispatches": 0, "reobserves": 0,
                               "status": "blocked",
                               "verdict": {"verdict": "inconclusive",
                                           "conditions": [], "unobserved": []},
                               "blocked": {"code": error.code,
                                           "message": str(error)[:200]},
                               "device_failure": True}
                outcome["run"] = run_index
                report["runs"].append(outcome)
                print(json.dumps({"task": task["id"], "run": run_index,
                                  "status": outcome["status"],
                                  "verdict": outcome["verdict"]["verdict"],
                                  "dispatches": outcome["dispatches"],
                                  "blocked": outcome.get("blocked")},
                                 ensure_ascii=False), flush=True)
                if args.report:
                    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.report).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8")
                if device_failure is not None:
                    code = device_failure.code
                    stopped = device_failures.record(code)
                    report["blocked_reason"] = {
                        "code": code, "message": str(device_failure)[:200],
                        "consecutive_runs": device_failures.consecutive}
                    if stopped:
                        report["blocked_reason"]["stopped"] = stopped
                        break
                    continue
                device_failures.record(None)
            if report.get("blocked_reason", {}).get("stopped"):
                break
        status = await harness.session_status()
        report["session"].update(unresolved_actions=len(status.get("unresolved_actions", [])),
                                 recovery_required=bool(status.get("recovery_required")))
    runs = report["runs"]
    succeeded = sum(1 for item in runs if item["status"] == "succeeded")
    errored = sum(1 for item in runs if item["verdict"]["verdict"] == "pass"
                  and item.get("blocked"))
    report["totals"] = {
        "runs": len(runs),
        "succeeded": succeeded,
        "failed": sum(1 for item in runs if item["status"] == "failed"),
        "inconclusive": sum(1 for item in runs if item["status"] == "inconclusive"),
        "blocked": sum(1 for item in runs if item["status"] == "blocked"),
        "dispatches": sum(item["dispatches"] for item in runs),
    }
    planned = len(tasks) * args.runs
    attempted = len(runs)
    unattempted = max(0, planned - attempted)
    report["totals"]["planned"] = planned
    report["totals"]["attempted"] = attempted
    report["totals"]["unattempted"] = unattempted
    report["false_success_claims"] = errored
    report["device_failures"] = sum(1 for item in runs if item.get("device_failure"))
    report["duration_seconds"] = round(time.time() - started, 3)
    # Smoke validates the chain on a short planned set; it must not pretend to
    # be the formal 10x3 gate. Formal mode keeps the fixed 27/30 bar.
    if args.mode == "smoke":
        report["threshold"] = {
            "mode": "smoke",
            "required_successes": attempted,
            "total": attempted,
            "note": "smoke: every attempted run must succeed; not the formal 27/30 gate",
        }
        report["status"] = (
            "ok" if (attempted == planned and attempted > 0 and succeeded == attempted
                     and not report["session"]["unresolved_actions"])
            else "not_ready"
        )
    else:
        report["threshold"] = {"mode": "formal", "required_successes": 27, "total": 30}
        report["status"] = (
            "ok" if (planned == 30 and attempted == 30 and succeeded >= 27
                     and not report["session"]["unresolved_actions"])
            else "not_ready"
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--device-id")
    parser.add_argument("--transport", choices=("stdio", "service"), default="service")
    parser.add_argument("--mode", choices=("smoke", "formal"), default="smoke",
                        help="smoke: short planned set, every attempted run must succeed; "
                             "formal: fixed 10x3 gate with >=27/30")
    parser.add_argument("--report")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that this run dispatches real device actions")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("accept-m1-weibo requires --execute")
    if not 1 <= args.runs <= 10:
        parser.error("--runs must be between 1 and 10")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
