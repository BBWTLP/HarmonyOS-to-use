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

from agent_harness import (AgentHarness, HarnessError, SetupBudget, SetupStats,
                           SetupUnavailable, WEIBO, editor_input, find_node,
                           find_search_bar, focused_input, has_weibo_evidence,
                           setup_act, surface_kind)

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
#: Setup is navigation, not measurement. It is bounded and reported separately;
#: after this many failed setups a primitive is reported with
#: `insufficient_valid_samples` instead of hammering the device.
MAX_SETUP_FAILURES = 5


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return round(ordered[index], 3)


class PrimitiveRunner:
    def __init__(self, harness: AgentHarness, *, per_primitive: int, only: tuple[str, ...],
                 progress_path: str | Path | None = None,
                 setup_budget: SetupBudget | None = None):
        self.harness = harness
        self.per_primitive = per_primitive
        self.only = only
        self.results: dict[str, dict] = {}
        self.notes: list[str] = []
        self.progress_path = Path(progress_path) if progress_path else None
        self.setup_budget = setup_budget or SetupBudget()
        self.setup = SetupStats()
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

    # -- navigation (setup: never part of the measured primitive) -----------
    async def settle(self) -> None:
        await asyncio.sleep(SETTLE_SECONDS)

    async def _setup_step(self, *, selector: str, locate, action_for,
                          expected=None, timeout_ms: int = 10000) -> dict:
        """One bounded setup action: observe, locate, act, re-locate on refusal."""
        observation, result = await setup_act(
            self.harness, selector=selector, locate=locate, action_for=action_for,
            expected=expected if expected is not None else {"changed": True},
            timeout_ms=timeout_ms, budget=self.setup_budget, stats=self.setup)
        self.setup_actions = self.setup.attempts
        await self.settle()
        return result

    def _setup_fail(self, code: str, message: str) -> SetupUnavailable:
        self.setup.failures += 1
        self.setup.failure_code = code
        self.setup.failure_codes.append(code)
        return SetupUnavailable(code, message)

    async def ensure_weibo(self) -> dict:
        """Bind to Weibo using independent app evidence before any steering."""
        observation = await self.harness.observe(mode="FAST")
        if has_weibo_evidence(observation) and observation.get("actionable"):
            return observation
        if not has_weibo_evidence(observation):
            self.note("foreign or unidentified surface: launching Weibo before measuring")
        result = await self._setup_step(
            selector="launch_weibo",
            locate=lambda obs: {"launch": WEIBO},
            action_for=lambda node: {"kind": "launch", "bundle": WEIBO},
            expected={"bundle": WEIBO}, timeout_ms=15000)
        if result.get("verification_status") != "verified":
            raise self._setup_fail("weibo_launch_unverified", str(result.get("status")))
        observation = await self.harness.observe(mode="FAST")
        if not has_weibo_evidence(observation):
            raise self._setup_fail(
                "weibo_evidence_missing",
                "Weibo was launched but the observation carries no Weibo evidence")
        return observation

    async def ensure_tabs_page(self) -> dict:
        """Home or Messages, i.e. a page showing the bottom navigation."""
        deadline = time.monotonic() + self.setup_budget.max_elapsed_ms / 1000
        while (self.setup.attempts < self.setup_budget.max_actions
               and time.monotonic() < deadline):
            observation = await self.ensure_weibo()
            kind = surface_kind(observation)
            if kind == "tabs":
                return observation
            if kind == "search_editor":
                await self._setup_step(selector="back_from_search_editor",
                                       locate=lambda obs: {"back": True},
                                       action_for=lambda node: {"kind": "back"})
                continue
            if kind == "discover" or kind == "unknown":
                # Known Weibo surface, wrong page: return to the home tab.
                await self._setup_step(
                    selector="tap_home_tab",
                    locate=lambda obs: find_node(obs, text=TAB_A),
                    action_for=lambda node: {"kind": "tap", "target": {"text": TAB_A}})
                continue
        raise SetupUnavailable("tabs_unavailable",
                               "Weibo main tabs were not reached within the setup budget")

    async def ensure_search_editor(self) -> dict:
        """Weibo's search editor, reached through the Discover search entry.

        Every step is a bounded setup step. A failure here means the
        *precondition* could not be established: no primitive was measured.
        """
        deadline = time.monotonic() + self.setup_budget.max_elapsed_ms / 1000
        while (self.setup.attempts < self.setup_budget.max_actions
               and time.monotonic() < deadline):
            observation = await self.ensure_weibo()
            field = editor_input(observation)
            if field is not None and field.get("focused"):
                return observation
            if field is not None:
                await self._setup_step(
                    selector="focus_search_editor_field",
                    locate=editor_input,
                    action_for=lambda node: {"kind": "tap",
                                             "target": {"action_id": node["action_id"]}})
                continue
            kind = surface_kind(observation)
            if kind == "discover":
                if find_search_bar(observation) is None:
                    raise self._setup_fail(
                        "search_entry_missing",
                        "Discover page exposes no unique search entry")
                await self._setup_step(
                    selector="open_search_editor",
                    locate=find_search_bar,
                    action_for=lambda node: {"kind": "tap",
                                             "target": {"action_id": node["action_id"]}})
                continue
            if kind == "tabs":
                await self._setup_step(
                    selector="tap_discover_tab",
                    locate=lambda obs: find_node(obs, text=DISCOVER_TAB),
                    action_for=lambda node: {"kind": "tap",
                                             "target": {"text": DISCOVER_TAB}})
                continue
            await self._setup_step(selector="back_to_known_page",
                                   locate=lambda obs: {"back": True},
                                   action_for=lambda node: {"kind": "back"})
        raise SetupUnavailable("search_editor_unavailable",
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
        # The navigation helpers write into `self.setup`; this primitive's
        # record must report that same object, not a fresh empty one.
        self.setup = SetupStats()
        setup = self.setup
        setup_sessions = 0
        errors_in_a_row = 0
        started = time.time()

        def publish() -> None:
            """Expose partial counters so a long run stays observable."""
            self.results[name] = self._primitive_record(
                name, latencies, failures, refusals, setup, setup_sessions, started, True)
            self.save_progress()

        publish()
        while len(latencies) + len(failures) < self.per_primitive:
            index = len(latencies) + len(failures)
            try:
                latencies.append(float(await handler(index)))
                errors_in_a_row = 0
            except SetupUnavailable as error:
                # The precondition was never established: nothing was measured,
                # so this must not enter the primitive's denominator.
                setup_sessions += 1
                setup.failure_code = error.code
                if error.code not in setup.failure_codes:
                    setup.failure_codes.append(error.code)
                self.note(f"{name}: setup failed ({error.code}); primitive not measured")
                if setup_sessions >= MAX_SETUP_FAILURES:
                    self.note(f"{name}: stopped after {setup_sessions} failed setups")
                    break
                publish()
                continue
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
                        except SetupUnavailable as setup_error:
                            error = setup_error
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
                    if isinstance(error, SetupUnavailable):
                        setup_sessions += 1
                        setup.failure_code = error.code
                        if error.code not in setup.failure_codes:
                            setup.failure_codes.append(error.code)
                        if setup_sessions >= MAX_SETUP_FAILURES:
                            self.note(f"{name}: stopped after {setup_sessions} failed setups")
                            break
                        publish()
                        continue
                    refusals[code] = refusals.get(code, 0) + 1
                failures.append({"index": index, "code": code,
                                 "message": str(error)[:200],
                                 "setup_actions": setup.attempts})
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
        return self._primitive_record(name, latencies, failures, refusals, setup,
                                      setup_sessions, started, False)

    def _primitive_record(self, name: str, latencies: list[float],
                          failures: list[dict], refusals: dict[str, int],
                          setup: SetupStats, setup_sessions: int, started: float,
                          in_progress: bool) -> dict:
        """One primitive's accounting.

        `valid_attempts` counts only samples whose precondition was established
        and whose primitive was actually attempted. Setup is reported next to it
        and never inside the success rate; a run that cannot reach the required
        number of valid attempts is `insufficient_valid_samples`, not a pass.
        """
        valid = len(latencies) + len(failures)
        return {
            "requested_samples": self.per_primitive,
            "valid_attempts": valid,
            "success": len(latencies),
            "primitive_failures": len(failures),
            "success_rate": round(len(latencies) / valid, 4) if valid else 0.0,
            "insufficient_valid_samples": valid < self.per_primitive,
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
            "max_ms": round(max(latencies), 3) if latencies else 0.0,
            "refusals_before_dispatch": dict(refusals),
            "failures": failures[:20],
            "failure_codes": sorted({item["code"] for item in failures}),
            "setup": setup.as_dict(self.setup_budget),
            "setup_sessions_failed": setup_sessions,
            "elapsed_seconds": round(time.time() - started, 3),
            "in_progress": in_progress,
        }

    def report(self) -> dict:
        summary = {name: self.results[name] for name in PRIMITIVES if name in self.results}
        totals = sum(item["valid_attempts"] for item in summary.values())
        success = sum(item["success"] for item in summary.values())
        return {
            "schema_version": 2,
            "scope": "M0 primitive matrix on the current authorised device; acceptance app = Weibo",
            "per_primitive": self.per_primitive,
            "primitives": summary,
            "current_primitive": self.current,
            "totals": {
                "valid_attempts": totals,
                "success": success,
                "success_rate": round(success / totals, 4) if totals else 0.0,
                "setup_attempts": sum(item["setup"]["attempts"]
                                      for item in summary.values()),
                "setup_success": sum(item["setup"]["success"]
                                     for item in summary.values()),
                "setup_failures": sum(item["setup"]["failures"]
                                      for item in summary.values()),
                "setup_stale_refusals": sum(item["setup"]["stale_refusals"]
                                            for item in summary.values()),
            },
            "threshold": {"key_primitives_success_rate": 0.99,
                          "required_valid_attempts": self.per_primitive},
            "setup_budget": self.setup_budget.as_dict(),
            "bounded_reobserves": self.retries,
            "setup_actions": self.setup_actions,
            "notes": self.notes,
            "privacy": "metadata only: no UI text, screenshots, or input values",
        }


def evaluate_gate(report: dict, requested: tuple[str, ...]) -> dict:
    """The M0 gate: validity of the samples AND the success rate.

    `valid_attempts == requested` is required, so setup instability can never be
    hidden by scoring only the samples that happened to be measurable.
    """
    primitives = report.get("primitives", {})
    session = report.get("session") or {}
    complete = set(requested).issubset(primitives.keys())
    insufficient = sorted(name for name, item in primitives.items()
                          if item.get("insufficient_valid_samples"))
    rate_ok = bool(primitives) and all(item["success_rate"] >= 0.99
                                       for item in primitives.values())
    gate = {
        "requested_samples_per_primitive": report.get("per_primitive"),
        "all_requested_primitives_present": complete,
        "insufficient_valid_samples": insufficient,
        "primitive_success_rate_ok": rate_ok,
        "unresolved_actions": session.get("unresolved_actions"),
        "recovery_required": session.get("recovery_required"),
    }
    gate["passed"] = bool(complete and not insufficient and rate_ok
                          and not session.get("unresolved_actions")
                          and not session.get("recovery_required"))
    return gate


async def main(args) -> int:
    started = time.time()
    report: dict = {"status": "not_ready"}
    async with AgentHarness(state_dir=args.state_dir, agent_tools=False) as harness:
        opened = await harness.open(args.device_id)
        runner = PrimitiveRunner(harness, per_primitive=args.per_primitive,
                                 only=tuple(args.only or PRIMITIVES),
                                 progress_path=args.report,
                                 setup_budget=SetupBudget())
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
    requested = tuple(args.only or PRIMITIVES)
    report["gate"] = evaluate_gate(report, requested)
    report["status"] = "ok" if report["gate"]["passed"] else "not_ready"
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
