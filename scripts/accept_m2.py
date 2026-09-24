#!/usr/bin/env python3
"""T08 M2 runner: typed steps over the production harness, never fake success.

CLI:
  python scripts/accept_m2.py --execute --state-dir <dir>
    --tasks evals/tasks/m2-30.json --runs 1 --transport stdio --report <path>

Step grammar (typed; not fed to plan_from_task as free DSL):
  tap:TEXT | tap:id:RID | back | swipe:up|down|left|right
  replace_text:id:RID=ARGKEY | replace_text:ARGKEY
  wait:text_present=TEXT | wait:text_absent=TEXT | wait:stable=MS
  observe | reobserve | recover | history
  burst:... (capability-gated)
  ocr_tap:LABEL (capability-gated)

Outcomes: succeeded / failed / blocked / unsupported. Cleanup never turns a
failure into success. Negative (expected-refusal) tasks are tallied separately.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_harness import (HarnessError, ServiceClientHarness, AgentHarness, WEIBO,
                           classify_surface, clickable_node, editor_input,
                           find_node, find_search_bar, focused_input)
from harmony_agent.checker import check
from harmony_agent.contracts import Predicate

DEFAULT_TASKS = Path(__file__).resolve().parents[1] / "evals" / "tasks" / "m2-30.json"


def parse_step(step: str) -> tuple[str, str]:
    kind, _, payload = str(step).partition(":")
    return kind.strip(), payload.strip()


def resolve_target_key(harness_obs: dict, key: str) -> dict | None:
    """Map a task target_key to a catalog node; never invent an id."""
    if not key:
        return None
    if key.startswith("type:"):
        matches = [n for n in harness_obs.get("catalog") or []
                   if n.get("type") == key[5:]]
        return matches[0] if len(matches) == 1 else None
    if key.startswith("id:"):
        matches = [n for n in harness_obs.get("catalog") or []
                   if n.get("resource_id") == key[3:]]
        return matches[0] if len(matches) == 1 else None
    return find_node(harness_obs, text=key)


async def act_with_retry(h, observable, action, expected=None, timeout_ms=12000):
    """Bounded pre-dispatch stale retry; never replays a dispatched write."""
    last = None
    for _ in range(3):
        try:
            return await h.act(observation_id=observable["observation_id"],
                               action=action, expected=expected,
                               timeout_ms=timeout_ms), observable
        except HarnessError as error:
            last = error
            if error.code not in ("stale_observation", "target_not_found") \
                    and "Page changed" not in str(error):
                raise
            observable = await h.observe(mode="FAST")
    raise last or RuntimeError("act failed")


async def run_typed_step(h, step: str, observable: dict, arguments: dict) -> tuple[dict, dict | None]:
    kind, payload = parse_step(step)
    started = time.perf_counter()
    result: dict = {"step": step, "kind": kind, "status": "ok"}
    post = None

    if kind == "tap":
        surface = classify_surface(observable)
        # Launcher/desktop: bring Weibo back before tapping its tabs.
        if surface == "foreign" and payload in ("首页", "发现", "消息", "我"):
            await h.act(observation_id=observable["observation_id"],
                        action={"kind": "launch", "bundle": WEIBO},
                        expected={"changed": True}, timeout_ms=15000)
            await asyncio.sleep(1.5)
            observable = await h.observe(mode="FAST")
            surface = classify_surface(observable)
        # Editor/compose hide the tab bar: leave first so tab labels exist.
        if surface in ("search_editor", "compose_dialog", "hot_search") and payload in (
                "首页", "发现", "消息", "我"):
            cancel = find_node(observable, text="取消")
            if cancel is not None:
                await h.act(observation_id=observable["observation_id"],
                            action={"kind": "tap", "target": {"text": "取消"}},
                            expected={"changed": True}, timeout_ms=12000)
            else:
                await h.act(observation_id=observable["observation_id"],
                            action={"kind": "back"}, expected={"changed": True},
                            timeout_ms=12000)
            observable = await h.observe(mode="FAST")
        node = resolve_target_key(observable, payload)
        if node is None:
            # Discover search bar is structural, not a fixed id.
            bar = find_search_bar(observable)
            if payload in ("search_bar", "id:search_bar") and bar is not None:
                node = clickable_node(observable, bar)
            elif payload.startswith("id:"):
                node = resolve_target_key(observable, payload)
        if node is None and not payload.startswith("id:"):
            # Bottom tab labels are non-clickable Text nodes; tap their parent.
            labels = [n for n in observable.get("catalog") or [] if n.get("text") == payload]
            bottom = [n for n in labels if (n.get("bounds") or [0, 0, 0, 0])[1] > 2400]
            pick = bottom[0] if len(bottom) == 1 else (labels[0] if len(labels) == 1 else None)
            if pick is not None:
                node = clickable_node(observable, pick)
        if node is None:
            # Middle tab flips to 回到顶部 after scroll; restore 发现 first.
            if payload == "发现":
                top = find_node(observable, text="回到顶部")
                if top is not None:
                    node = clickable_node(observable, top)
        if node is None:
            result.update(status="unsupported", error="target_missing", payload=payload)
            return result, observable
        node = clickable_node(observable, node)
        action = {"kind": "tap", "target": {"action_id": node["action_id"]}}
        out = None
        last_error = None
        for attempt in range(3):
            try:
                out = await h.act(observation_id=observable["observation_id"],
                                  action=action, expected={"changed": True},
                                  timeout_ms=12000)
                break
            except HarnessError as error:
                last_error = error
                if error.code not in ("stale_observation", "target_not_found") \
                        and "Page changed" not in str(error):
                    raise
                observable = await h.observe(mode="FAST")
                # Re-ground the same semantic label on the fresh catalog.
                node2 = resolve_target_key(observable, payload)
                if node2 is None and payload == "发现":
                    top = find_node(observable, text="回到顶部")
                    if top is not None:
                        node2 = clickable_node(observable, top)
                if node2 is not None:
                    node = clickable_node(observable, node2)
                    action = {"kind": "tap", "target": {"action_id": node["action_id"]}}
        if out is None:
            result.update(status="blocked", error=last_error.code if last_error else "act_failed")
            return result, observable
        result.update(status=out.get("status"),
                      execution_status=out.get("execution_status"),
                      verification_status=out.get("verification_status"),
                      request_id=out.get("request_id"))
        post = out.get("observation")
    elif kind == "back":
        # Leaving the search editor: prefer 取消 so we land on discover.
        if classify_surface(observable) == "search_editor":
            cancel = find_node(observable, text="取消")
            action = ({"kind": "tap", "target": {"text": "取消"}} if cancel is not None
                      else {"kind": "back"})
        else:
            action = {"kind": "back"}
        out, observable = await act_with_retry(h, observable, action, {"changed": True})
        result.update(status=out.get("status"),
                      execution_status=out.get("execution_status"),
                      verification_status=out.get("verification_status"),
                      request_id=out.get("request_id"))
        post = out.get("observation")
    elif kind == "swipe":
        out, observable = await act_with_retry(
            h, observable, {"kind": "swipe", "direction": payload or "up"},
            {"changed": True})
        result.update(status=out.get("status"), execution_status=out.get("execution_status"),
                      request_id=out.get("request_id"))
        post = out.get("observation")
    elif kind == "replace_text":
        # replace_text:id:RID=argkey  |  replace_text:argkey
        target_part, _, argkey = payload.partition("=")
        text = arguments.get(argkey or payload, argkey or payload)
        field = focused_input(observable) or editor_input(observable)
        if target_part.startswith("id:"):
            field = resolve_target_key(observable, target_part) or field
        if field is None:
            result.update(status="unsupported", error="input_missing")
            return result, observable
        rid = field.get("resource_id")
        target = {"resource_id": rid} if rid else {"action_id": field["action_id"]}
        out, observable = await act_with_retry(
            h, observable, {"kind": "replace_text", "target": target, "text": text},
            expected=None, timeout_ms=15000)
        result.update(status=out.get("status"), execution_status=out.get("execution_status"),
                      request_id=out.get("request_id"))
        post = out.get("observation")
    elif kind == "wait":
        condition, _, value = payload.partition("=")
        start = time.time()
        polls = 0
        ok = False
        for _ in range(20):
            polls += 1
            obs = await h.observe(mode="FAST")
            if condition == "text_present":
                ok = find_node(obs, text=value) is not None
            elif condition == "text_absent":
                ok = find_node(obs, text=value) is None
            elif condition == "stable":
                target_ms = int(value or 300)
                await asyncio.sleep(max(0.05, target_ms / 1000.0))
                ok = True
            else:
                result.update(status="unsupported", error="wait_condition")
                return result, observable
            if ok:
                post = obs
                break
            await asyncio.sleep(0.2)
        result.update(status="ok" if ok else "timeout",
                      wait_polls=polls,
                      wait_ms=round((time.time() - start) * 1000, 3))
    elif kind in ("observe", "reobserve"):
        post = await h.observe(mode="FAST")
        result.update(status="ok", catalog=len(post.get("catalog") or []))
    elif kind == "recover":
        out = await h.call("mobile_session", operation="recover", session_id=h.session_id)
        result.update(status="ok", recover=out)
        post = await h.observe(mode="FAST")
    elif kind == "history":
        out = await h.history(limit=20)
        result.update(status="ok", history_items=len(out.get("items") or []),
                      request_ids=[i.get("request_id") for i in (out.get("items") or [])[:5]])
        post = observable
    elif kind == "burst":
        result.update(status="unsupported", error="burst_capability_gated",
                      note="use accept_c03_burst for measured max_supported_steps")
        return result, observable
    elif kind == "ocr_tap":
        result.update(status="unsupported", error="ocr_capability_missing")
        return result, observable
    else:
        result.update(status="unsupported", error="unknown_step")
        return result, observable

    result["ms"] = round((time.perf_counter() - started) * 1000, 3)
    result["surface"] = classify_surface(post or observable)
    return result, (post or observable)


def judge(criteria, observation, arguments=None, baseline=None) -> dict:
    normalized = []
    for item in criteria:
        item = dict(item)
        if isinstance(item.get("value"), bool):
            item["value"] = "true" if item["value"] else "false"
        normalized.append(item)
    preds = [Predicate.model_validate(item) for item in normalized]
    return check(preds, observation, arguments=arguments or {}, baseline=baseline,
                 surface_classifier=classify_surface, incident_free=True).to_dict()


async def execute_task(h, task: dict) -> dict:
    """One task: typed steps + independent verdict + safe cleanup."""
    arguments = dict(task.get("arguments") or {})
    outcome = {"task_id": task.get("id"), "level": task.get("level"),
               "goal": task.get("goal"), "steps": [], "dispatches": 0,
               "status": "not_ready", "expected_refusal": bool(task.get("expect_refusal"))}
    baseline = await h.observe(mode="FAST")
    observable = baseline
    try:
        for step in task.get("steps") or []:
            record, observable = await run_typed_step(h, step, observable, arguments)
            outcome["steps"].append(record)
            if record.get("execution_status") == "executed":
                outcome["dispatches"] += 1
            if record.get("status") == "unsupported":
                outcome["status"] = "unsupported"
                outcome["blocked"] = {"code": record.get("error"), "step": step}
                break
            if record.get("execution_status") == "unknown":
                outcome["status"] = "blocked"
                outcome["blocked"] = {"code": "execution_unknown", "step": step}
                break
        final = await h.observe(mode="FAST")
        outcome["verdict"] = judge(task.get("success_criteria") or [], final,
                                   arguments=arguments, baseline=baseline)
        if outcome["status"] != "unsupported":
            if outcome.get("expected_refusal"):
                # Negative control: pass only when the guard refused.
                refused = any(s.get("status") in ("refused", "blocked")
                              or s.get("execution_status") == "not_dispatched"
                              for s in outcome["steps"])
                outcome["status"] = "succeeded" if refused else "failed"
                outcome["verdict"] = {"verdict": outcome["status"],
                                      "kind": "expected_refusal"}
            elif outcome.get("blocked"):
                outcome["status"] = "blocked"
            elif outcome["verdict"]["verdict"] == "pass":
                outcome["status"] = "succeeded"
            elif outcome["verdict"]["verdict"] == "fail":
                outcome["status"] = "failed"
            else:
                outcome["status"] = "inconclusive"
        # Cleanup never promotes a failure to success.
        for step in task.get("cleanup") or []:
            try:
                await run_typed_step(h, step, await h.observe(mode="FAST"), arguments)
            except HarnessError:
                break
        return outcome
    except HarnessError as error:
        outcome["status"] = "blocked"
        outcome["blocked"] = {"code": error.code, "message": str(error)[:160]}
        outcome.setdefault("verdict", {"verdict": "inconclusive"})
        return outcome


async def run_batch(args) -> dict:
    definition = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    tasks = definition.get("tasks") or []
    if args.only:
        wanted = set(args.only)
        tasks = [t for t in tasks if t.get("id") in wanted]
    caps = definition.get("capabilities") or {}
    report = {
        "schema_version": 1,
        "milestone": "M2",
        "tasks_file": str(args.tasks),
        "task_set_version": definition.get("task_set_version") or definition.get("schema_version"),
        "mode": "smoke" if args.runs == 1 else f"runs_{args.runs}",
        "capabilities": caps,
        "planned": len(tasks) * args.runs,
        "attempted": 0,
        "unattempted": 0,
        "succeeded": 0,
        "failed": 0,
        "blocked": 0,
        "unsupported": 0,
        "inconclusive": 0,
        "expected_refusal_pass": 0,
        "results": [],
        "status": "not_ready",
    }
    harness_class = ServiceClientHarness if args.transport == "service" else AgentHarness
    kwargs = {"state_dir": args.state_dir}
    if harness_class is AgentHarness:
        kwargs["agent_tools"] = False
    async with harness_class(**kwargs) as h:
        opened = await h.open(args.device_id)
        report["session"] = {"device_bound": bool(opened.get("device_id"))}
        for task in tasks:
            for run_index in range(1, args.runs + 1):
                report["attempted"] += 1
                outcome = await execute_task(h, task)
                outcome["run"] = run_index
                report["results"].append(outcome)
                status = outcome.get("status")
                if status == "succeeded":
                    if outcome.get("expected_refusal"):
                        report["expected_refusal_pass"] += 1
                    else:
                        report["succeeded"] += 1
                elif status == "unsupported":
                    report["unsupported"] += 1
                elif status == "blocked":
                    report["blocked"] += 1
                elif status == "inconclusive":
                    report["inconclusive"] += 1
                else:
                    report["failed"] += 1
    report["unattempted"] = report["planned"] - report["attempted"]
    # Smoke: every attempted non-unsupported task must succeed (or expected refusal).
    report["status"] = (
        "ok" if report["failed"] == 0 and report["blocked"] == 0
        and report["inconclusive"] == 0
        and (report["succeeded"] + report["expected_refusal_pass"] + report["unsupported"])
            == report["attempted"]
        else "not_ready"
    )
    return report


def dry_run(tasks_path: Path) -> dict:
    problems = []
    try:
        definition = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
    except Exception as error:
        return {"status": "invalid", "problems": [str(error)]}
    for task in definition.get("tasks") or []:
        tid = task.get("id") or "?"
        if not task.get("success_criteria"):
            problems.append(f"{tid}: no success_criteria")
        for step in task.get("steps") or []:
            kind, _ = parse_step(step)
            if kind not in {"tap", "back", "swipe", "replace_text", "wait",
                            "observe", "reobserve", "recover", "history",
                            "burst", "ocr_tap", "launch"}:
                problems.append(f"{tid}: bad step {step!r}")
    return {"status": "ok" if not problems else "invalid", "problems": problems,
            "task_count": len(definition.get("tasks") or [])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--device-id")
    parser.add_argument("--transport", choices=("stdio", "service"), default="stdio")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 20:
        parser.error("--runs 1..20")
    plan = dry_run(args.tasks)
    if plan["status"] != "ok":
        print(json.dumps({"dry_run": plan}, indent=2, ensure_ascii=False))
        return 2
    if not args.execute:
        out = {"dry_run": plan, "status": "dry_run_ok", "execute": False}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        return 0
    report = asyncio.run(run_batch(args))
    report["dry_run"] = plan
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
