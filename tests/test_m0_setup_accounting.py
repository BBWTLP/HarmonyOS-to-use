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
from agent_harness import (HarnessError, SetupBudget, SetupStats,  # noqa: E402
                           SetupUnavailable, setup_act)

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


if __name__ == "__main__":
    unittest.main()
