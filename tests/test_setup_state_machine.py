"""RC4-A.1: the setup navigation is an explicit, bounded state machine.

The defect this replaces: a loop that guessed the page and performed one action
per iteration could spend its whole budget while every action "succeeded".
Observed on the device: 9 of 10 setup actions verified, yet the search editor
was never reached, and the same action repeated without changing the surface.

Scenarios required by the plan: normal path, direct discover, already in editor,
stale-then-success, success-without-progress, unknown surface recovery, launch
transition, and loop detection.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from accept_m0_primitives import (SEARCH_EDITOR_FSM, TABS_FSM, PrimitiveRunner,  # noqa: E402
                                  TRANSITION_TARGETS)
from agent_harness import (AgentHarness, HarnessError, SetupBudget,  # noqa: E402
                           SetupUnavailable, WEIBO)

OK = {"status": "ok", "execution_status": "executed", "verification_status": "verified"}


def entry(**overrides):
    base = {"action_id": "n0", "type": "Text", "bounds": [0, 2000, 100, 2100],
            "hit_bounds": [0, 2000, 100, 2100], "text": "", "description": "",
            "hint": "", "resource_id": "", "accessibility_id": "1",
            "hierarchy": "ROOT0,0", "host_window_id": "1", "enabled": True,
            "clickable": False, "focused": False, "bundle": WEIBO, "checked": None,
            "selected": None, "text_observed": True, "parent_action_id": None,
            "target_fingerprint": "f" * 64}
    base.update(overrides)
    return base


def surface_observation(surface: str) -> dict:
    """A minimal catalog that classifies as `surface`."""
    if surface == "foreign":
        catalog = [entry(bundle="com.ohos.sceneboard", type="Flex",
                         bounds=[80, 100, 1000, 220], clickable=True)]
        return {"observation_id": "obs", "actionable": True, "foreground_bundle": None,
                "catalog": catalog}
    if surface == "unknown":
        catalog = [entry(type="Column", bounds=[0, 0, 100, 100])]
    elif surface == "tabs":
        catalog = [entry(action_id="n0", type="Text", text="首页",
                         bounds=[100, 2240, 170, 2300], hierarchy="ROOT0,0,1"),
                   entry(action_id="n1", type="Text", text="发现",
                         bounds=[270, 2240, 340, 2300], hierarchy="ROOT0,0,2"),
                   entry(action_id="n2", type="Text", text="消息",
                         bounds=[540, 2240, 610, 2300], hierarchy="ROOT0,0,2"),
                   entry(action_id="n3", type="Text", text="我",
                         bounds=[810, 2240, 880, 2300], hierarchy="ROOT0,0,3")]
    elif surface == "discover":
        catalog = [entry(action_id="sb", type="Flex", bounds=[47, 154, 1273, 276],
                         clickable=True, hierarchy="ROOT0,0,5")]
    elif surface == "search_surface":
        catalog = [entry(action_id="in", type="TextInput", clickable=True,
                         focused=False, bounds=[141, 145, 1081, 267],
                         hierarchy="ROOT0,0,6")]
    elif surface == "editor":
        catalog = [entry(action_id="in", type="TextInput", clickable=True,
                         focused=True, bounds=[141, 145, 1081, 267],
                         hierarchy="ROOT0,0,6"),
                   entry(action_id="lst", type="List", bounds=[0, 280, 1320, 1680],
                         hierarchy="ROOT0,0,7")]
    else:
        raise AssertionError(f"unknown surface {surface}")
    return {"observation_id": "obs", "actionable": True,
            "foreground_bundle": WEIBO, "catalog": catalog}


def action_key(action: dict, surface: str) -> str:
    if action.get("kind") == "launch":
        return "launch"
    if action.get("kind") == "back":
        return "back"
    target = action.get("target") or {}
    if "text" in target:
        return f"tap_text:{target['text']}"
    if "action_id" in target:
        return f"tap_id:{surface}:{target['action_id']}"
    return action.get("kind", "?")


class FakeHarness:
    """Stateful stand-in: observations report the surface, acts move it."""

    def __init__(self, surface: str, plan: dict[str, str] | None = None,
                 fail_first: dict[str, int] | None = None):
        self.surface = surface
        self.plan = dict(plan or {})
        self.fail_first = dict(fail_first or {})
        self.acts: list[dict] = []
        self.observations = 0

    async def observe(self, mode: str = "FAST") -> dict:
        self.observations += 1
        return surface_observation(self.surface)

    async def act(self, *, observation_id: str, action: dict, expected=None,
                  timeout_ms: int = 10000) -> dict:
        key = action_key(action, self.surface)
        self.acts.append({"key": key, "surface": self.surface})
        if self.fail_first.get(key, 0) > 0:
            self.fail_first[key] -= 1
            raise HarnessError("stale_observation", "scripted refusal")
        if key in self.plan:
            self.surface = self.plan[key]
        return dict(OK)


def drive(surface: str, plan: dict, *, target=SEARCH_EDITOR_FSM,
          budget: SetupBudget | None = None, fail_first=None):
    harness = FakeHarness(surface, plan, fail_first)
    runner = PrimitiveRunner(harness, per_primitive=1, only=("back",),
                             setup_budget=budget or SetupBudget())
    try:
        observation = asyncio.run(runner._drive(
            "search_editor" if target is SEARCH_EDITOR_FSM else "tabs", target))
        return {"outcome": "ok", "observation": observation, "runner": runner,
                "harness": harness}
    except SetupUnavailable as error:
        return {"outcome": error.code, "runner": runner, "harness": harness}


class SetupStateMachineTests(unittest.TestCase):
    def test_normal_path_foreign_tabs_discover_search_editor(self):
        result = drive("foreign", {
            "launch": "tabs",
            "tap_text:发现": "discover",
            "tap_id:discover:sb": "search_surface",
            "tap_id:search_surface:in": "editor",
        })
        self.assertEqual(result["outcome"], "ok")
        trace = result["runner"].setup.trace
        self.assertEqual([step["transition"] for step in trace],
                         ["launch_weibo", "open_discover", "open_search", "focus_editor"])
        self.assertTrue(all(step["progress"] for step in trace))
        session = result["runner"].setup.sessions[-1]
        self.assertEqual((session["outcome"], session["actions"]), ("ok", 4))

    def test_direct_discover_reaches_the_editor_in_one_step(self):
        result = drive("discover", {"tap_id:discover:sb": "editor"})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(len(result["runner"].setup.trace), 1)
        self.assertEqual(result["runner"].setup.trace[0]["transition"], "open_search")

    def test_already_in_the_editor_needs_no_navigation(self):
        result = drive("editor", {})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["runner"].setup.trace, [])
        self.assertEqual(result["harness"].acts, [])
        self.assertEqual(result["runner"].setup.sessions[-1]["actions"], 0)

    def test_a_stale_refusal_is_retried_and_still_reaches_the_editor(self):
        result = drive("discover", {"tap_id:discover:sb": "editor"},
                       fail_first={"tap_id:discover:sb": 1})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["runner"].setup.stale_refusals, 1)
        first = result["runner"].setup.trace[0]
        self.assertTrue(first["stale_refusal"])
        self.assertTrue(first["progress"])

    def test_success_without_progress_is_bounded(self):
        result = drive("discover", {"tap_id:discover:sb": "discover"})
        self.assertEqual(result["outcome"], "setup_no_progress")
        runner = result["runner"]
        self.assertGreaterEqual(runner.setup.no_progress, 3)
        self.assertTrue(all(step["state_changed"] is False
                            for step in runner.setup.trace))
        # The same action is never retried more than the bound allows.
        self.assertLessEqual(len(runner.setup.trace), runner.setup_budget.max_no_progress + 1)

    def test_an_unknown_surface_recovers_with_one_back(self):
        result = drive("unknown", {
            "back": "tabs",
            "tap_text:发现": "discover",
            "tap_id:discover:sb": "editor",
        })
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["runner"].setup.trace[0]["transition"], "back_to_known")

    def test_a_launch_transition_waits_for_a_known_surface(self):
        result = drive("foreign", {"launch": "discover",
                                   "tap_id:discover:sb": "editor"})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["runner"].setup.trace[0]["transition"], "launch_weibo")
        self.assertEqual(result["runner"].setup.trace[1]["surface_before"], "discover")

    def test_a_launch_that_never_settles_reports_app_not_stable(self):
        result = drive("foreign", {"launch": "foreign"})
        self.assertEqual(result["outcome"], "setup_app_not_stable")

    def test_oscillating_states_terminate_with_a_loop_failure(self):
        result = drive("tabs", {"tap_text:发现": "discover",
                                "tap_id:discover:sb": "tabs"})
        self.assertEqual(result["outcome"], "setup_state_loop")
        transitions = [step["transition"] for step in result["runner"].setup.trace]
        self.assertLessEqual(len(transitions),
                             result["runner"].setup_budget.max_same_state_repeats + 2)

    def test_a_missing_locator_is_detected_without_dispatching(self):
        """A transition whose locator target is absent must not be attempted."""
        runner = PrimitiveRunner(FakeHarness("tabs", {}), per_primitive=1,
                                 only=("back",))
        observation = surface_observation("tabs")
        # "focus_editor" needs a top-band input field; a tabs page has none.
        self.assertFalse(runner._transition_possible("focus_editor", observation))
        self.assertIsNone(runner._choose_transition(("focus_editor",), "tabs",
                                                    observation))
        self.assertTrue(runner._transition_possible("open_discover", observation))

    def test_tabs_target_uses_its_own_transition_map(self):
        result = drive("editor", {"back": "tabs"}, target=TABS_FSM)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["runner"].setup.trace[0]["transition"], "back_to_known")
        self.assertIn("back_to_known", TRANSITION_TARGETS)


if __name__ == "__main__":
    unittest.main()
