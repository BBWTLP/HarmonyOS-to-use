"""M0 acceptance accounting: setup is never scored as a primitive.

The defect this pins down: `ensure_search_editor()` failing (the page was never
reached) was recorded as a *primitive* failure, so a navigation problem silently
lowered `back`/`input` success rates and hid that the sample was never measured.

Cases come straight from the fix plan:
  Case 1  setup fails            -> valid_attempts unchanged, setup_failures +1
  Case 2  setup ok, primitive fails -> valid_attempts +1, primitive_failures +1
  Case 3  setup refused 3x then ok  -> setup_refusals=3, valid_attempts=1, success=1
  Case 4  setup budget exhausted    -> insufficient_valid_samples -> gate fails
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from accept_m0_primitives import (MAX_SETUP_FAILURES, PrimitiveRunner,  # noqa: E402
                                  evaluate_gate)
from agent_harness import WEIBO  # noqa: E402
from agent_harness import (AgentHarness, HarnessError, SetupBudget,  # noqa: E402
                           SetupStats, SetupUnavailable, setup_act)

OK_RESULT = {"status": "ok", "execution_status": "executed",
             "verification_status": "verified"}


class FakeHarness:
    """Scripted stand-in for the MCP client: outcomes are consumed in order."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls: list[dict] = []
        self.observations = 0

    async def observe(self, mode: str = "FAST") -> dict:
        self.observations += 1
        return {
            "observation_id": f"obs{self.observations}",
            "actionable": True,
            "catalog": [{"action_id": "n1", "type": "Flex", "bounds": [40, 100, 1000, 220],
                         "clickable": True, "enabled": True,
                         "target_fingerprint": "f" * 64}],
        }

    async def act(self, *, observation_id: str, action: dict, expected=None,
                  timeout_ms: int = 10000) -> dict:
        self.calls.append({"observation_id": observation_id, "action": action})
        if not self.outcomes:
            return dict(OK_RESULT)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def runner(harness, *, per_primitive: int = 1) -> PrimitiveRunner:
    return PrimitiveRunner(harness, per_primitive=per_primitive, only=("back",))


class SetupAccountingTests(unittest.TestCase):
    def test_case1_setup_failure_is_not_a_primitive_attempt(self):
        calls = {"count": 0}

        async def handler(index: int) -> float:
            calls["count"] += 1
            if calls["count"] == 1:
                raise SetupUnavailable("search_editor_unavailable", "page not reached")
            return 12.0

        subject = runner(FakeHarness())
        subject._back = handler
        record = asyncio.run(subject.run())["primitives"]["back"]
        self.assertEqual(record["valid_attempts"], 1)
        self.assertEqual(record["success"], 1)
        self.assertEqual(record["primitive_failures"], 0)
        self.assertEqual(record["failure_codes"], [])
        # A failed setup session is not a measured sample.
        self.assertEqual(record["setup_sessions_failed"], 1)
        self.assertEqual(record["setup"]["failure_code"], "search_editor_unavailable")
        self.assertFalse(record["insufficient_valid_samples"])

    def test_case2_setup_ok_primitive_failure_still_scores(self):
        async def handler(index: int) -> float:
            raise HarnessError("back_not_executed", "the action did not execute")

        subject = runner(FakeHarness())
        subject._back = handler
        record = asyncio.run(subject.run())["primitives"]["back"]
        self.assertEqual(record["valid_attempts"], 1)
        self.assertEqual(record["success"], 0)
        self.assertEqual(record["primitive_failures"], 1)
        self.assertEqual(record["success_rate"], 0.0)
        self.assertEqual(record["failure_codes"], ["back_not_executed"])
        self.assertEqual(record["setup"]["failures"], 0)

    def test_case3_setup_refusals_reobserve_and_relocate(self):
        fake = FakeHarness([HarnessError("stale_observation", "page moved")] * 3)
        stats = SetupStats()
        asyncio.run(setup_act(
            fake, selector="open_search_editor", stats=stats,
            locate=lambda obs: obs["catalog"][0],
            action_for=lambda node: {"kind": "tap",
                                     "target": {"action_id": node["action_id"]}}))
        self.assertEqual(stats.stale_refusals, 3)
        self.assertEqual(stats.attempts, 4)
        self.assertEqual(stats.success, 1)
        self.assertEqual(stats.failures, 0)
        # Every retry observed again, so no retry reused a stale action identity.
        self.assertEqual([call["observation_id"] for call in fake.calls],
                         ["obs1", "obs2", "obs3", "obs4"])
        self.assertEqual(len(stats.drift), 3)
        self.assertTrue(all(entry["resolved_before"] and entry["resolved_current"]
                            for entry in stats.drift))

    def test_case4_setup_budget_exhaustion_is_insufficient_samples(self):
        async def handler(index: int) -> float:
            raise SetupUnavailable("setup_budget_exhausted", "budget gone")

        subject = runner(FakeHarness())
        subject._back = handler
        report = asyncio.run(subject.run())
        record = report["primitives"]["back"]
        self.assertEqual(record["valid_attempts"], 0)
        self.assertEqual(record["success"], 0)
        self.assertTrue(record["insufficient_valid_samples"])
        self.assertEqual(record["setup_sessions_failed"], MAX_SETUP_FAILURES)
        gate = evaluate_gate(report, ("back",))
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["insufficient_valid_samples"], ["back"])

    def test_success_rate_never_uses_the_setup_denominator(self):
        """Scoring only the samples that were measurable must not read as a pass."""
        async def handler(index: int) -> float:
            raise SetupUnavailable("search_editor_unavailable", "page not reached")

        subject = runner(FakeHarness(), per_primitive=4)
        subject._back = handler
        record = asyncio.run(subject.run())["primitives"]["back"]
        self.assertEqual(record["valid_attempts"], 0)
        self.assertEqual(record["success_rate"], 0.0)
        self.assertTrue(record["insufficient_valid_samples"])

    def test_a_stale_budget_bounds_the_setup(self):
        fake = FakeHarness([HarnessError("stale_observation", "page moved")] * 12)
        stats = SetupStats()
        with self.assertRaises(SetupUnavailable) as error:
            asyncio.run(setup_act(
                fake, selector="open_search_editor", stats=stats,
                budget=SetupBudget(max_stale_refusals=2),
                locate=lambda obs: obs["catalog"][0],
                action_for=lambda node: {"kind": "tap",
                                         "target": {"action_id": node["action_id"]}}))
        self.assertEqual(error.exception.code, "setup_stale_budget_exhausted")
        self.assertEqual(stats.stale_refusals, 3)
        self.assertLessEqual(stats.attempts, 4)

    def test_a_missing_locator_is_a_setup_failure_not_a_dispatch(self):
        fake = FakeHarness()
        stats = SetupStats()
        with self.assertRaises(SetupUnavailable) as error:
            asyncio.run(setup_act(fake, selector="tap_absent", stats=stats,
                                  locate=lambda obs: None,
                                  action_for=lambda node: {"kind": "tap"}))
        self.assertEqual(error.exception.code, "setup_target_missing")
        self.assertEqual(fake.calls, [])

    def test_the_record_reports_the_stats_the_navigation_helpers_write(self):
        """Regression: the runner reported a fresh, empty SetupStats.

        Navigation writes into the runner's live stats object, so the primitive
        record must expose that same object - otherwise a run can report
        `setup_attempts: 0` while it actually performed (and was refused) setup
        actions on the device.
        """
        fake = FakeHarness([HarnessError("stale_observation", "page moved")])
        subject = runner(fake)

        async def handler(index: int) -> float:
            await subject._setup_step(
                selector="tap_discover_tab",
                locate=lambda obs: obs["catalog"][0],
                action_for=lambda node: {"kind": "tap",
                                         "target": {"action_id": node["action_id"]}})
            return 5.0

        subject._back = handler
        record = asyncio.run(subject.run())["primitives"]["back"]
        self.assertEqual(record["success"], 1)
        self.assertEqual(record["setup"]["attempts"], 2)
        self.assertEqual(record["setup"]["stale_refusals"], 1)
        self.assertEqual(record["setup"]["success"], 1)
        self.assertEqual(len(record["setup"]["target_drift"]), 1)


def editor_observation(*, focused: bool, resource_id: str = "",
                       include_input: bool = True, include_list: bool = True) -> dict:
    """A search-editor observation as the acceptance device reports it."""
    catalog = [{"action_id": "n1", "type": "TextInput", "text": "",
                "bundle": WEIBO,
                "resource_id": resource_id, "accessibility_id": "13871",
                "focused": focused, "enabled": True, "clickable": True,
                "bounds": [141, 145, 1081, 267]}]
    if include_input is False:
        catalog = [item for item in catalog if item["type"] != "TextInput"]
    if include_list:
        catalog.append({"action_id": "n2", "type": "List", "bounds": [0, 300, 1080, 2340],
                        "enabled": True, "bundle": WEIBO})
    catalog.append({"action_id": "n3", "type": "Text", "text": "热搜榜",
                    "bounds": [40, 400, 600, 500], "enabled": True, "bundle": WEIBO})
    return {"observation_id": "obs-editor", "actionable": True,
            "foreground_bundle": WEIBO, "catalog": catalog}


class EditorFieldResolutionTests(unittest.TestCase):
    """The editor field must be resolvable without a stable `resource_id`.

    Device evidence (2026-09-20): the Weibo search editor reports the field with
    an empty `resource_id`, only a volatile auto-increment `accessibilityId`, and
    not always autofocused. The old lookup (`focused_input` or
    `resource_id == search_input`) then missed every time and `input` scored 0/3
    with `input_field_missing`.
    """

    def runner(self, harness=None) -> PrimitiveRunner:
        return PrimitiveRunner(harness or FakeHarness(), per_primitive=1,
                               only=("input",))

    def test_structure_fallback_resolves_the_unfocused_field(self):
        subject = self.runner()
        field, source = subject._editor_field(editor_observation(focused=False))
        self.assertIsNotNone(field)
        self.assertEqual(source, "structure")

    def test_declared_resource_id_still_wins_over_structure(self):
        subject = self.runner()
        field, source = subject._editor_field(
            editor_observation(focused=False, resource_id="search_input"))
        self.assertEqual(source, "resource_id")
        self.assertEqual(field["resource_id"], "search_input")

    def test_focused_field_wins_over_every_other_source(self):
        subject = self.runner()
        field, source = subject._editor_field(
            editor_observation(focused=True, resource_id="search_input"))
        self.assertEqual(source, "focused")

    def test_an_ambiguous_or_missing_field_is_reported_absent(self):
        subject = self.runner()
        field, source = subject._editor_field(editor_observation(focused=False,
                                                                 include_input=False))
        self.assertIsNone(field)
        self.assertEqual(source, "absent")
        # Two top-band inputs (e.g. a stray hint field) is ambiguous, not a match.
        observation = editor_observation(focused=False)
        observation["catalog"].append({"action_id": "n9", "type": "TextInput",
                                       "text": "", "resource_id": "",
                                       "focused": False, "enabled": True,
                                       "bounds": [40, 30, 900, 90]})
        field, source = subject._editor_field(observation)
        self.assertEqual(source, "absent")

    def test_input_focuses_the_field_as_setup_and_never_scores_it(self):
        class FocusingHarness(FakeHarness):
            def __init__(self):
                super().__init__([OK_RESULT, OK_RESULT])
                self.focused = False

            async def observe(self, mode: str = "FAST") -> dict:
                self.observations += 1
                return editor_observation(focused=self.focused)

            async def act(self, *, observation_id: str, action: dict, expected=None,
                          timeout_ms: int = 10000) -> dict:
                self.calls.append({"action": action, "expected": expected})
                if action["kind"] == "tap":
                    self.focused = True
                    return dict(OK_RESULT)
                return dict(OK_RESULT)

        fake = FocusingHarness()
        subject = PrimitiveRunner(fake, per_primitive=1, only=("input",))
        report = asyncio.run(subject.run())
        record = report["primitives"]["input"]
        kinds = [call["action"]["kind"] for call in fake.calls]
        self.assertEqual(kinds, ["tap", "replace_text"])
        self.assertEqual(record["valid_attempts"], 1)
        self.assertEqual(record["success"], 1)
        self.assertEqual(record["primitive_failures"], 0)
        self.assertEqual(record["setup"]["attempts"], 1)  # the focus tap is setup
        self.assertEqual(fake.calls[-1]["expected"], None)  # measurement unchanged

    def test_a_field_that_refuses_focus_is_a_setup_failure(self):
        class StubbornHarness(FakeHarness):
            async def observe(self, mode: str = "FAST") -> dict:
                self.observations += 1
                return editor_observation(focused=False)

        subject = PrimitiveRunner(StubbornHarness([OK_RESULT] * 4),
                                  per_primitive=1, only=("input",))
        with self.assertRaises(SetupUnavailable) as caught:
            asyncio.run(subject._focus_editor_field())
        self.assertEqual(caught.exception.code, "input_focus_unavailable")


class ServicePreflightTests(unittest.TestCase):
    """A missing/stale local service must fail loudly, not stall the caller.

    Device evidence (2026-09-20): with no resident service the stdio frontend
    fails every call, and the orphaned stdio child keeps the caller's pipe open,
    so the run looks like an endless hang with no output and no report.
    """

    def test_missing_endpoint_names_the_service_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            harness = AgentHarness(state_dir=root)
            with self.assertRaises(HarnessError) as caught:
                harness.require_service()
            self.assertEqual(caught.exception.code, "runtime_unavailable")
            self.assertIn("serve", str(caught.exception))

    def test_stale_endpoint_names_the_port_and_the_restart(self):
        import json
        import socket
        import tempfile
        # Reserve a port, then close it, so the endpoint points at nothing.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as root:
            (pathlib.Path(root) / "endpoint.json").write_text(
                json.dumps({"port": port, "token": "abc", "pid": 1, "protocol": 1}),
                encoding="utf-8")
            harness = AgentHarness(state_dir=root)
            with self.assertRaises(HarnessError) as caught:
                harness.require_service()
            self.assertEqual(caught.exception.code, "runtime_unavailable")
            self.assertIn(str(port), str(caught.exception))

    def test_a_listening_service_passes_preflight(self):
        import json
        import socket
        import tempfile
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as root:
                (pathlib.Path(root) / "endpoint.json").write_text(
                    json.dumps({"port": port, "token": "abc", "pid": 1, "protocol": 1}),
                    encoding="utf-8")
                AgentHarness(state_dir=root).require_service()  # must not raise
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
