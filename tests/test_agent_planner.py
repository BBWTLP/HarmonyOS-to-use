import asyncio
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harmony_agent.contracts import TaskSubmit
from harmony_agent.planner import (LoopDetector, STEPS_ARGUMENT, plan_from_task, replan)


def task(**overrides):
    payload = {
        "schema_version": "2.0", "request_id": "r1", "mode": "delegated",
        "goal": "在微博搜索", "scope": {"device_ref": "d",
                                        "allowed_apps": ["com.sina.weibo.stage"],
                                        "allowed_actions": ["tap", "replace_text", "back"],
                                        "cloud_data_policy": "disabled"},
        "success_criteria": [{"id": "c1", "type": "input_equals",
                              "target_key": "id:search_input", "value_ref": "query"}],
        "arguments": {"query": "鸿蒙"},
        "budget": {"max_dispatches": 8, "max_seconds": 60, "max_model_calls": 4},
        "model_profile": "local_shadow",
    }
    payload.update(overrides)
    return TaskSubmit.model_validate(payload)


class CriteriaPlanTests(unittest.TestCase):
    def test_input_criterion_becomes_a_replace_text_step(self):
        plan = plan_from_task(task())
        self.assertEqual(len(plan.subgoals), 1)
        subgoal = plan.subgoals[0]
        self.assertEqual(subgoal.action_kind, "replace_text")
        self.assertEqual(subgoal.intent_resource_id, "search_input")
        self.assertEqual(subgoal.argument_refs, {"text": "arg.query"})

    def test_element_criterion_becomes_a_tap_step(self):
        plan = plan_from_task(task(success_criteria=[
            {"id": "c1", "type": "element_present", "target_key": "搜索"}]))
        self.assertEqual(plan.subgoals[0].action_kind, "tap")
        self.assertEqual(plan.subgoals[0].intent_text, "搜索")

    def test_verification_only_criteria_produce_no_action(self):
        plan = plan_from_task(task(success_criteria=[
            {"id": "c1", "type": "text_equals", "value": "综合"},
            {"id": "c2", "type": "foreground_is", "value": "com.sina.weibo.stage"}]))
        self.assertEqual(plan.subgoals, [])

    def test_literal_value_is_placed_in_the_plan_parameters(self):
        plan = plan_from_task(task(success_criteria=[
            {"id": "c1", "type": "input_equals", "target_key": "id:search_input",
             "value": "鸿蒙"}]))
        self.assertEqual(plan.parameters["literal_1"], "鸿蒙")
        self.assertEqual(plan.subgoals[0].argument_refs, {"text": "arg.literal_1"})


class StepPlanTests(unittest.TestCase):
    def plan(self, steps, **kwargs):
        arguments = {"query": "鸿蒙", STEPS_ARGUMENT: steps}
        arguments.update(kwargs)
        return plan_from_task(task(arguments=arguments))

    def test_delegated_steps_preserve_order_and_targets(self):
        plan = self.plan("tap:发现|tap:搜索|replace_text:id:search_input=query")
        kinds = [item.action_kind for item in plan.subgoals]
        self.assertEqual(kinds, ["tap", "tap", "replace_text"])
        self.assertEqual(plan.subgoals[0].intent_text, "发现")
        self.assertEqual(plan.subgoals[2].intent_resource_id, "search_input")
        self.assertEqual(plan.subgoals[2].argument_refs, {"text": "arg.query"})

    def test_literal_step_value_is_resolved_from_parameters(self):
        plan = self.plan("replace_text:搜索=literal:鸿蒙")
        reference = plan.subgoals[0].argument_refs["text"]
        self.assertTrue(reference.startswith("arg.literal_"))
        self.assertEqual(plan.parameters[reference[4:]], "鸿蒙")

    def test_device_level_steps_carry_no_target(self):
        plan = self.plan("back|home|swipe:up|launch:com.sina.weibo.stage")
        self.assertEqual([item.action_kind for item in plan.subgoals],
                         ["back", "home", "swipe", "launch"])
        self.assertEqual(plan.subgoals[2].argument_refs, {"direction": "up"})
        self.assertEqual(plan.subgoals[3].argument_refs,
                         {"bundle": "com.sina.weibo.stage"})

    def test_unknown_step_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan("delete_all:everything")

    # -- terminal postcondition binding (P1-04 reconciliation evidence) -----
    def test_only_the_last_step_carries_the_terminal_semantic_condition(self):
        """The terminal goal condition must not gate intermediate steps.

        Binding it to every step would make a step retry whenever the *goal* is
        not yet reached, which is how a single tap turned into a second,
        unrelated dispatch. Only the final step declares it, and only as
        recovery evidence.
        """
        plan = plan_from_task(task(
            success_criteria=[{"id": "results", "type": "text_equals",
                               "value": "鸿蒙 的相关结果"}],
            arguments={"steps": "tap:发现|tap:搜索"}))
        self.assertEqual(plan.subgoals[0].recovery_expected, [])
        self.assertEqual([item.id for item in plan.subgoals[-1].recovery_expected],
                         ["results"])
        listed = plan.to_dict()["subgoals"][-1]
        self.assertEqual(listed["recovery_expected"], ["results"])
        self.assertEqual(listed["expected"], [])

    def test_a_non_semantic_criterion_binds_nothing(self):
        plan = plan_from_task(task(success_criteria=[
            {"id": "editor", "type": "element_present", "target_key": "搜索"}],
            arguments={"steps": "tap:搜索"}))
        self.assertEqual(plan.subgoals[-1].recovery_expected, [])

    def test_the_bound_condition_keeps_its_argument_reference(self):
        plan = plan_from_task(task(
            success_criteria=[{"id": "results", "type": "text_equals",
                               "value_ref": "query"}],
            arguments={"query": "鸿蒙", "steps": "tap:搜索"}))
        condition = plan.subgoals[-1].recovery_expected[0]
        self.assertEqual(condition.type, "text_equals")
        self.assertEqual(condition.value_ref, "query")

    def test_empty_step_list_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan("   ")

    def test_back_with_target_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plan("back:nowhere")


class PlanLifecycleTests(unittest.TestCase):
    def test_next_pending_respects_dependencies(self):
        plan = plan_from_task(task(success_criteria=[
            {"id": "c1", "type": "element_present", "target_key": "搜索"},
            {"id": "c2", "type": "input_equals", "target_key": "id:search_input",
             "value_ref": "query"}]))
        plan.subgoals[1].depends_on = [plan.subgoals[0].subgoal_id]
        self.assertEqual(plan.next_pending().subgoal_id, plan.subgoals[0].subgoal_id)
        plan.subgoals[0].status = "verified"
        self.assertEqual(plan.next_pending().subgoal_id, plan.subgoals[1].subgoal_id)

    def test_replan_appends_a_version_and_keeps_the_goal(self):
        plan = plan_from_task(task())
        again = replan(plan, "target_missing", reason="reobserve")
        self.assertEqual(again.version, plan.version + 1)
        self.assertEqual(again.goal, plan.goal)
        self.assertEqual(again.parameters, plan.parameters)
        self.assertTrue(again.reasons)

    def test_loop_detector_needs_three_identical_outcomes(self):
        detector = LoopDetector(limit=3)
        self.assertFalse(detector.observe("tap:a", "fp1"))
        self.assertFalse(detector.observe("tap:a", "fp1"))
        self.assertTrue(detector.observe("tap:a", "fp1"))
        self.assertFalse(detector.observe("tap:a", "fp1", is_scroll=True))


class McpToolVisibilityTests(unittest.TestCase):
    def build(self, enabled: bool):
        previous = os.environ.get("HARMONY_AGENT_TOOLS")
        try:
            if enabled:
                os.environ["HARMONY_AGENT_TOOLS"] = "1"
            else:
                os.environ.pop("HARMONY_AGENT_TOOLS", None)
            import importlib
            import harmony_runtime.mcp_server as module
            importlib.reload(module)
            return module.build("/tmp/state")
        finally:
            if previous is None:
                os.environ.pop("HARMONY_AGENT_TOOLS", None)
            else:
                os.environ["HARMONY_AGENT_TOOLS"] = previous

    def names(self, server):
        async def collect():
            tools = await server.list_tools()
            items = getattr(tools, "tools", tools)
            return sorted(tool.name for tool in items)
        return asyncio.run(collect())

    def test_v1_clients_see_exactly_six_tools(self):
        names = self.names(self.build(False))
        self.assertEqual(names, ["mobile_act", "mobile_burst", "mobile_history",
                                 "mobile_observe", "mobile_session", "mobile_wait"])

    def test_v2_tools_are_opt_in(self):
        names = self.names(self.build(True))
        for expected in ("mobile_run_task", "mobile_task_status", "mobile_task_control",
                         "mobile_task_events", "mobile_task_result", "mobile_decide"):
            self.assertIn(expected, names)
        self.assertIn("mobile_session", names)


if __name__ == "__main__":
    unittest.main()
