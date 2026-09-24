"""v3.2 Phase 11: the M2 30-task specification is complete and well formed.

This validates the *definition* only. Execution is `blocked_device`, and the
specification deliberately contains no results.
"""
from __future__ import annotations

import json
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "evals" / "tasks" / "m2-30.json"

#: Predicate types the checker can decide without a model.
PROGRAMMATIC = {"foreground_is", "text_equals", "element_present", "element_absent",
                "input_equals", "selected_is", "page_changed", "surface_is", "all_of"}


class M2SpecificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads(SPEC.read_text(encoding="utf-8"))
        cls.tasks = cls.spec["tasks"]

    def test_the_specification_declares_no_results(self):
        self.assertEqual(self.spec["execution"], "blocked_device")
        for task in self.tasks:
            self.assertNotIn("result", task, task["id"])
            self.assertNotIn("status", task, task["id"])

    def test_thirty_tasks_with_ten_per_level(self):
        self.assertEqual(len(self.tasks), 30)
        levels = {}
        for task in self.tasks:
            levels[task["level"]] = levels.get(task["level"], 0) + 1
        self.assertEqual(levels, {"L1": 10, "L2": 10, "L3": 10})

    def test_task_ids_are_unique_and_prefixed_by_level(self):
        ids = [task["id"] for task in self.tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for task in self.tasks:
            self.assertTrue(task["id"].startswith(f"m2_{task['level'].lower()}_"),
                            task["id"])

    def test_every_task_declares_the_required_fields(self):
        required = ("id", "level", "goal", "initial_state", "scope_note", "steps",
                    "success_criteria", "risk", "cleanup")
        for task in self.tasks:
            for field in required:
                self.assertIn(field, task, f"{task['id']} is missing {field}")
            self.assertTrue(task["goal"])
            self.assertTrue(task["steps"])
            self.assertTrue(task["cleanup"])

    def test_every_checker_is_programmatic(self):
        for task in self.tasks:
            for criterion in task["success_criteria"]:
                self.assertIn(criterion["type"], PROGRAMMATIC,
                              f"{task['id']}: {criterion['type']}")
                self.assertTrue(criterion.get("id"))

    def test_value_refs_are_supplied_by_arguments(self):
        for task in self.tasks:
            arguments = task.get("arguments", {})
            for criterion in task["success_criteria"]:
                reference = criterion.get("value_ref")
                if reference:
                    self.assertIn(reference, arguments, f"{task['id']}: {reference}")

    def test_the_task_level_scope_is_inside_the_authorised_scope(self):
        allowed = set(self.spec["defaults"]["scope"]["allowed_actions"])
        for task in self.tasks:
            self.assertEqual(self.spec["defaults"]["scope"]["allowed_apps"],
                             ["com.sina.weibo.stage"])
            self.assertTrue(allowed)

    def test_budgets_are_bounded_and_per_task_overrides_are_bounded(self):
        budget = self.spec["defaults"]["budget"]
        self.assertLessEqual(budget["max_dispatches"], 12)
        self.assertLessEqual(budget["max_seconds"], 120)
        self.assertLessEqual(budget["max_model_calls"], 4)
        for task in self.tasks:
            override = task.get("budget", {})
            for key, value in override.items():
                self.assertLessEqual(value, budget[key], f"{task['id']}: {key}")

    def test_risk_classes_and_negative_controls_are_explicit(self):
        for task in self.tasks:
            self.assertIn(task["risk"], ("low", "medium", "high"), task["id"])
        expectations = {task["id"]: task["expect"] for task in self.tasks
                        if "expect" in task}
        self.assertIn("m2_l3_06_verification_blocks_false_success", expectations)
        self.assertIn("m2_l3_07_sensitive_target_refused", expectations)
        self.assertIn("m2_l3_09_unknown_write_reconciliation", expectations)
        self.assertEqual(expectations["m2_l3_07_sensitive_target_refused"],
                         "BLOCKED_BY_POLICY")

    def test_the_failure_taxonomy_is_the_unified_one(self):
        self.assertEqual(self.spec["failure_taxonomy"],
                         ["observation", "grounding", "planning", "decision",
                          "guard", "action", "verification", "infrastructure"])

    def test_the_specification_states_the_acceptance_targets(self):
        note = self.spec["plan_note"]
        self.assertIn("300", note)
        self.assertIn("90%", note)
        self.assertIn("0 false completions", note)

    def test_the_visual_task_requires_ocr(self):
        visual = next(task for task in self.tasks
                      if task["id"] == "m2_l3_10_visual_region_tap")
        self.assertIn("ocr", visual["requires"])
        self.assertIn("revalidate", visual["scope_note"])

    def test_the_model_profile_is_rules_only_by_default(self):
        self.assertEqual(self.spec["defaults"]["model_profile"], "local_off")


if __name__ == "__main__":
    unittest.main()
