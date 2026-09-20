"""M0 primitive matrix acceptance on the current real device (acceptance app: Weibo).

Every primitive runs `--per-primitive` times through the production stdio MCP
boundary as a real client. Screenshot/tree are read-only; tap/swipe/back/input
are dispatched on pages where the target is observable first, so a page that is
merely in the wrong state is a *setup* step rather than a primitive failure.

Accounting:
  attempts            one logical primitive execution
  success             dispatched and verified within the bounded re-observe
  refusals            pre-dispatch refusals (stale_observation) counted separately
  setup_*             navigation used to reach the measured page, never counted

Reports contain metadata only: no UI text, screenshots, or input values.
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

from agent_harness import (AgentHarness, HarnessError, WEIBO, editor_input, find_node,
                           find_search_bar, focused_input, surface_kind)

#: Ordered cheap-first so partial evidence is still useful if a run is stopped.
PRIMITIVES = ("launch", "tree", "screenshot", "swipe", "tap", "back", "input")

TAB_A = "首页"
TAB_B = "消息"
DISCOVER_TAB = "发现"
SEARCH_INPUT_ID = "search_input"

INPUT_VALUES = ("鸿蒙", "harmony", "测试test", "ABC123")

MAX_CONSECUTIVE_DEVICE_ERRORS = 5
MAX_REOBSERVE_RETRIES = 4
SETTLE_SECONDS = 0.5


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return round(ordered[index], 3)


class PrimitiveRunner:
    def __init__(self, harness: AgentHarness, *, per_primitive: int, only: tuple[str, ...],
                 progress_path: str | Path | None = None):
        self.harness = harness
        self.per_primitive = per_primitive
        self.only = only
        self.results: dict[str, dict] = {}
        self.notes: list[str] = []
        self.progress_path = Path(progress_path) if progress_path else None
        self.retries = 0
        self.setup_actions = 0
        self.current: str | None = None

    # -- progress -----------------------------------------------------------
    def save_progress(self) -> None:
        if self.progress_path is None:
            return
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress_path.write_text(
            json.dumps(self.report(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    # -- navigation ---------------------------------------------------------
    async def settle(self) -> None:
        await asyncio.sleep(SETTLE_SECONDS)

    async def ensure_weibo(self) -> dict:
        observation = await self.harness.observe(mode="FAST")
        if observation.get("foreground_bundle") == WEIBO and observation.get("actionable"):
            return observation
        result = await self.harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "launch", "bundle": WEIBO},
            expected={"bundle": WEIBO}, timeout_ms=15000)
        self.setup_actions += 1
        if result.get("verification_status") != "verified":
            raise HarnessError("weibo_launch_unverified", str(result.get("status")))
        return await self.harness.observe(mode="FAST")

    async def tap_text(self, observation: dict, label: str, *, setup: bool) -> dict:
        await self.settle()
        if setup:
            self.setup_actions += 1
        return await self.harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"text": label}},
            expected={"changed": True}, timeout_ms=10000)

    async def press_back(self, observation: dict, *, setup: bool) -> dict:
        await self.settle()
        if setup:
            self.setup_actions += 1
        return await self.harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "back"},
            expected={"changed": True}, timeout_ms=10000)

    async def ensure_tabs_page(self) -> dict:
        """Home or Messages, i.e. a page showing the bottom navigation."""
        for _ in range(4):
            observation = await self.ensure_weibo()
            kind = surface_kind(observation)
            if kind == "tabs":
                return observation
            try:
                if kind == "search_editor":
                    await self.press_back(observation, setup=True)
                else:
                    node = find_node(observation, text=TAB_A)
                    if node is None:
                        break
                    await self.tap_text(observation, TAB_A, setup=True)
            except HarnessError:
                pass
        return await self.ensure_weibo()

    async def ensure_search_editor(self) -> dict:
        """Weibo's search editor, reached through the Discover search bar."""
        for _ in range(7):
            observation = await self.ensure_weibo()
            field = editor_input(observation)
            if field is not None and field.get("focused"):
                return observation
            if field is not None:
                try:
                    await self.settle()
                    self.setup_actions += 1
                    await self.harness.act(
                        observation_id=observation["observation_id"],
                        action={"kind": "tap", "target": {"action_id": field["action_id"]}},
                        expected={"changed": True}, timeout_ms=10000)
                except HarnessError:
                    pass
                continue
            kind = surface_kind(observation)
            if kind == "discover":
                bar = find_search_bar(observation)
                if bar is None:
                    break
                try:
                    await self.settle()
                    self.setup_actions += 1
                    await self.harness.act(
                        observation_id=observation["observation_id"],
                        action={"kind": "tap", "target": {"action_id": bar["action_id"]}},
                        expected={"changed": True}, timeout_ms=10000)
                except HarnessError:
                    pass
                continue
            tab = find_node(observation, text=DISCOVER_TAB)
            if tab is None:
                try:
                    await self.press_back(observation, setup=True)
                except HarnessError:
                    break
                continue
            try:
                await self.tap_text(observation, DISCOVER_TAB, setup=True)
            except HarnessError:
                pass
        raise HarnessError("search_editor_unavailable",
                           "Search editor could not be reached within the setup budget")

    # -- primitives ---------------------------------------------------------
    async def _launch(self, index: int) -> float:
        observation = await self.ensure_weibo()
        started = time.perf_counter()
        result = await self.harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "launch", "bundle": WEIBO},
            expected={"bundle": WEIBO}, timeout_ms=15000)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self._require(result, "launch")
        return elapsed

    async def _tree(self, index: int) -> float:
        started = time.perf_counter()
        observation = await self.harness.observe(mode="FULL")
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        if not isinstance(observation.get("tree"), dict) or not observation["tree"]:
            raise HarnessError("tree_missing", "FULL observation returned no tree")
        if not observation.get("catalog"):
            raise HarnessError("catalog_empty", "FULL observation returned an empty catalog")
        return elapsed

    async def _screenshot(self, index: int) -> float:
        started = time.perf_counter()
        observation = await self.harness.observe(mode="FAST", include_image=True)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        images = [item for item in self.harness.last_images if item.get("verified")]
        if not images:
            raise HarnessError("screenshot_missing", "Observation returned no decodable image")
        display = observation.get("display") or {}
        if images[0].get("width") != display.get("width") or \
                images[0].get("height") != display.get("height"):
            raise HarnessError("screenshot_dimension_mismatch",
                               "Decoded image differs from the reported display")
        if observation.get("image_tree_consistent") is not True:
            raise HarnessError("screenshot_inconsistent",
                               "Tree bracket around the screenshot did not match")
        return elapsed

    async def _swipe(self, index: int) -> float:
        observation = await self.ensure_tabs_page()
        direction = "up" if index % 2 == 0 else "down"
        started = time.perf_counter()
        result = await self.harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "swipe", "direction": direction},
            expected={"changed": True}, timeout_ms=10000)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self._require(result, "swipe")
        return elapsed

    async def _tap(self, index: int) -> float:
        observation = await self.ensure_tabs_page()
        label = TAB_A if index % 2 == 0 else TAB_B
        if find_node(observation, text=label) is None:
            raise HarnessError("tab_missing", "Main navigation tab is not uniquely observable")
        started = time.perf_counter()
        result = await self.harness.act(
            observation_id=observation["observation_id"],
            action={"kind": "tap", "target": {"text": label}},
            expected={"changed": True}, timeout_ms=10000)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self._require(result, "tap")
        return elapsed

    async def _back(self, index: int) -> float:
        editor = await self.ensure_search_editor()
        started = time.perf_counter()
        result = await self.harness.act(
            observation_id=editor["observation_id"],
            action={"kind": "back"},
            expected={"changed": True}, timeout_ms=10000)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self._require(result, "back")
        return elapsed

    async def _input(self, index: int) -> float:
        editor = await self.ensure_search_editor()
        field = focused_input(editor) or find_node(editor, resource_id=SEARCH_INPUT_ID)
        if field is None:
            raise HarnessError("input_field_missing",
                               "No single focused input field is observable")
        value = INPUT_VALUES[index % len(INPUT_VALUES)]
        started = time.perf_counter()
        result = await self.harness.act(
            observation_id=editor["observation_id"],
            action={"kind": "replace_text",
                    "target": {"action_id": field["action_id"]},
                    "text": value},
            expected=None, timeout_ms=15000)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self._require(result, "input")
        return elapsed

    @staticmethod
    def _require(result: dict, name: str) -> None:
        if result.get("execution_status") != "executed":
            raise HarnessError(f"{name}_not_executed", str(result.get("status")))
        if result.get("verification_status") != "verified":
            raise HarnessError(f"{name}_unverified", str(result.get("status")))

    # -- driver -------------------------------------------------------------
    async def run(self) -> dict:
        for name in PRIMITIVES:
            if name not in self.only:
                continue
            self.current = name
            self.results[name] = await self._run_primitive(name)
            self.save_progress()
        self.current = None
        self.save_progress()
        return self.report()

    async def _run_primitive(self, name: str) -> dict:
        handler = getattr(self, f"_{name}")
        latencies: list[float] = []
        failures: list[dict] = []
        refusals: dict[str, int] = {}
        errors_in_a_row = 0
        started = time.time()

        def publish() -> None:
            """Expose partial counters so a long run stays observable."""
            attempts = len(latencies) + len(failures)
            self.results[name] = {
                "attempts": attempts,
                "success": len(latencies),
                "failure": len(failures),
                "success_rate": round(len(latencies) / attempts, 4) if attempts else 0.0,
                "p50_ms": percentile(latencies, 0.5),
                "p95_ms": percentile(latencies, 0.95),
                "max_ms": round(max(latencies), 3) if latencies else 0.0,
                "refusals_before_dispatch": dict(refusals),
                "failures": failures[:20],
                "failure_codes": sorted({item["code"] for item in failures}),
                "elapsed_seconds": round(time.time() - started, 3),
                "in_progress": True,
            }
            self.save_progress()

        publish()
        for index in range(self.per_primitive):
            try:
                latencies.append(float(await handler(index)))
                errors_in_a_row = 0
            except Exception as error:
                code = getattr(error, "code", type(error).__name__)
                if code == "stale_observation":
                    # The guard refused before dispatch; retry with a fresh view.
                    retried = False
                    for _ in range(MAX_REOBSERVE_RETRIES):
                        self.retries += 1
                        try:
                            latencies.append(float(await handler(index)))
                            retried = True
                            break
                        except Exception as retry_error:
                            code = getattr(retry_error, "code", type(retry_error).__name__)
                            if code != "stale_observation":
                                error = retry_error
                                break
                    if retried:
                        refusals["stale_observation"] = refusals.get("stale_observation", 0) + 1
                        errors_in_a_row = 0
                        publish()
                        continue
                    refusals[code] = refusals.get(code, 0) + 1
                failures.append({"index": index, "code": code,
                                 "message": str(error)[:200],
                                 "setup_actions": self.setup_actions})
                if code in ("device_unavailable", "runtime_unavailable",
                            "device_selection_required", "session_invalid",
                            "lease_expired", "client_invalid",
                            "search_editor_unavailable"):
                    errors_in_a_row += 1
                    if errors_in_a_row >= MAX_CONSECUTIVE_DEVICE_ERRORS:
                        self.note(f"{name}: aborted after {errors_in_a_row} consecutive "
                                  f"'{code}' errors")
                        break
                else:
                    errors_in_a_row = 0
            publish()
        attempts = len(latencies) + len(failures)
        return {
            "attempts": attempts,
            "success": len(latencies),
            "failure": len(failures),
            "success_rate": round(len(latencies) / attempts, 4) if attempts else 0.0,
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
            "max_ms": round(max(latencies), 3) if latencies else 0.0,
            "refusals_before_dispatch": refusals,
            "failures": failures[:20],
            "failure_codes": sorted({item["code"] for item in failures}),
            "elapsed_seconds": round(time.time() - started, 3),
            "in_progress": False,
        }

    def report(self) -> dict:
        summary = {name: self.results[name] for name in PRIMITIVES if name in self.results}
        totals = sum(item["attempts"] for item in summary.values())
        success = sum(item["success"] for item in summary.values())
        return {
            "schema_version": 1,
            "scope": "M0 primitive matrix on the current authorised device; acceptance app = Weibo",
            "per_primitive": self.per_primitive,
            "primitives": summary,
            "current_primitive": self.current,
            "totals": {"attempts": totals, "success": success,
                       "success_rate": round(success / totals, 4) if totals else 0.0},
            "threshold": {"key_primitives_success_rate": 0.99},
            "bounded_reobserves": self.retries,
            "setup_actions": self.setup_actions,
            "notes": self.notes,
            "privacy": "metadata only: no UI text, screenshots, or input values",
        }


async def main(args) -> int:
    started = time.time()
    report: dict = {"status": "not_ready"}
    async with AgentHarness(state_dir=args.state_dir, agent_tools=False) as harness:
        opened = await harness.open(args.device_id)
        runner = PrimitiveRunner(harness, per_primitive=args.per_primitive,
                                 only=tuple(args.only or PRIMITIVES),
                                 progress_path=args.report)
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        report = await runner.run()
        status = await harness.session_status()
        report["session"] = {
            "device_bound": bool(opened.get("device_id")),
            "controller_epoch": status.get("controller_epoch"),
            "unresolved_actions": len(status.get("unresolved_actions", [])),
            "recovery_required": bool(status.get("recovery_required")),
        }
    report["duration_seconds"] = round(time.time() - started, 3)
    complete = set(PRIMITIVES).issubset(report.get("primitives", {}).keys())
    met = complete and all(item["success_rate"] >= 0.99
                           for item in report["primitives"].values())
    report["status"] = "ok" if met and not report["session"]["unresolved_actions"] else "not_ready"
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--per-primitive", type=int, default=100)
    parser.add_argument("--only", nargs="*", choices=list(PRIMITIVES))
    parser.add_argument("--device-id")
    parser.add_argument("--report")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that this run dispatches real device actions")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("accept-m0-primitives requires --execute")
    if not 1 <= args.per_primitive <= 500:
        parser.error("--per-primitive must be between 1 and 500")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
