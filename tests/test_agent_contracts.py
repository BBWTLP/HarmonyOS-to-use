import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harmony_agent.candidates import CONTROL_OPTIONS, risk_class_for
from harmony_agent.contracts import (Budget, Candidate, DecisionContext, DecisionResult,
                                     Fact, Predicate, RemainingBudget, Scope, TaskSubmit,
                                     candidate_set_hash)
from pydantic import ValidationError


def task_payload(**overrides):
    payload = {
        "schema_version": "2.0",
        "request_id": "task-request-001",
        "mode": "delegated",
        "goal": "搜索鸿蒙并确认结果页已打开",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": ["com.sina.weibo.stage"],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": [{"id": "query", "type": "input_equals",
                              "target_key": "search_input", "value_ref": "query"}],
        "arguments": {"query": "鸿蒙"},
        "budget": {"max_dispatches": 8, "max_seconds": 120, "max_model_calls": 12},
        "model_profile": "local_shadow",
    }
    payload.update(overrides)
    return payload


def candidate(epoch=0, observation="obs_1", **overrides):
    payload = {
        "candidate_id": "cand_" + "a" * 24,
        "observation_id": observation,
        "controller_epoch": epoch,
        "target_ref": "gt_" + "b" * 32,
        "action_kind": "tap",
        "argument_refs": {},
        "expected_predicates": [{"id": "p1", "type": "foreground_is",
                                 "value": "com.sina.weibo.stage"}],
        "evidence_refs": ["obs:1"],
        "risk_class": "low",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return Candidate.model_validate(payload)


class TaskContractTests(unittest.TestCase):
    def test_valid_task_submits(self):
        task = TaskSubmit.model_validate(task_payload())
        self.assertEqual(task.scope.allowed_actions[0], "tap")
        self.assertEqual(task.budget.max_dispatches, 8)

    def test_unknown_value_ref_is_rejected(self):
        payload = task_payload()
        payload["success_criteria"][0]["value_ref"] = "missing"
        with self.assertRaises(ValidationError):
            TaskSubmit.model_validate(payload)

    def test_duplicate_criteria_ids_are_rejected(self):
        payload = task_payload()
        payload["success_criteria"] = [payload["success_criteria"][0],
                                       dict(payload["success_criteria"][0])]
        with self.assertRaises(ValidationError):
            TaskSubmit.model_validate(payload)

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            TaskSubmit.model_validate(task_payload(unexpected=True))

    def test_scope_requires_allowed_apps(self):
        payload = task_payload()
        payload["scope"]["allowed_apps"] = []
        with self.assertRaises(ValidationError):
            TaskSubmit.model_validate(payload)


class PredicateTests(unittest.TestCase):
    def test_page_assertion_requires_description(self):
        with self.assertRaises(ValidationError):
            Predicate(id="p", type="page_assertion")

    def test_value_and_value_ref_are_exclusive(self):
        with self.assertRaises(ValidationError):
            Predicate(id="p", type="text_equals", value="a", value_ref="b")
        with self.assertRaises(ValidationError):
            Predicate(id="p", type="text_equals")

    def test_all_of_requires_nested_predicates(self):
        with self.assertRaises(ValidationError):
            Predicate(id="p", type="all_of")
        predicate = Predicate(id="p", type="all_of",
                              predicates=[{"id": "c", "type": "foreground_is",
                                           "value": "com.sina.weibo.stage"}])
        self.assertEqual(len(predicate.predicates), 1)

    def test_element_predicates_require_target_key(self):
        with self.assertRaises(ValidationError):
            Predicate(id="p", type="element_present")

    def test_empty_string_is_not_a_valid_value(self):
        with self.assertRaises(ValidationError):
            Predicate(id="p", type="text_equals", value="")


class CandidateTests(unittest.TestCase):
    def test_model_issued_handle_is_rejected(self):
        with self.assertRaises(ValidationError):
            candidate(candidate_id="cand_home")
        with self.assertRaises(ValidationError):
            candidate(target_ref="search_button")

    def test_expiry_must_be_timezone_aware(self):
        with self.assertRaises(ValidationError):
            candidate(expires_at="2099-01-01T00:00:00")

    def test_hash_changes_when_parameters_change(self):
        first = candidate_set_hash([candidate(argument_refs={"text": "arg.query"})])
        second = candidate_set_hash([candidate(argument_refs={"text": "arg.other"})])
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_risk_class_is_not_model_supplied(self):
        self.assertEqual(risk_class_for("搜索按钮"), "low")
        self.assertEqual(risk_class_for("发送微博"), "high")
        self.assertEqual(risk_class_for("delete account"), "high")


class DecisionContractTests(unittest.TestCase):
    def context(self, **overrides):
        items = [candidate()]
        payload = {
            "task_id": "task_1", "subgoal_id": "sub_1", "scope_id": "scope_1",
            "controller_epoch": 0, "observation_id": "obs_1",
            "candidate_set_hash": candidate_set_hash(items),
            "goal": "搜索", "facts": [{"key": "foreground", "value": "com.sina.weibo.stage",
                                        "evidence_ref": "obs:1"}],
            "candidates": [items[0].model_dump()], "recent_outcomes": [],
            "budget_remaining": {"dispatches": 4, "seconds": 60.0, "model_calls": 4,
                                 "cost_usd": None},
        }
        payload.update(overrides)
        return DecisionContext.model_validate(payload)

    def test_context_rejects_mismatched_candidate_observation(self):
        with self.assertRaises(ValidationError):
            self.context(observation_id="obs_2")

    def test_context_rejects_wrong_hash(self):
        with self.assertRaises(ValidationError):
            self.context(candidate_set_hash="f" * 64)

    def test_decider_execute_requires_calibration(self):
        with self.assertRaises(ValidationError):
            DecisionResult.model_validate({
                "task_id": "t", "subgoal_id": "s", "observation_id": "obs_1",
                "controller_epoch": 0, "candidate_set_hash": "a" * 64,
                "provider": "decider", "model_revision": "rev",
                "calibration_version": None, "elapsed_ms": 1.0, "route": "execute",
                "selected_candidate_id": "cand_a",
                "native_scores": {"score_semantics": "decider_systemone",
                                  "answers": {"action": {"type": "choice",
                                                          "choice": "cand_a",
                                                          "confidence": 0.9,
                                                          "certainty": 0.8,
                                                          "probabilities": {"cand_a": 0.9,
                                                                            "cand_b": 0.1}}}},
                "reason_code": "r"})

    def test_non_execute_route_cannot_select_a_candidate(self):
        with self.assertRaises(ValidationError):
            DecisionResult.model_validate({
                "task_id": "t", "subgoal_id": "s", "observation_id": "obs_1",
                "controller_epoch": 0, "candidate_set_hash": "a" * 64,
                "provider": "rules", "model_revision": "rules:1",
                "calibration_version": None, "elapsed_ms": 1.0, "route": "reobserve",
                "selected_candidate_id": "cand_a",
                "native_scores": {"score_semantics": "deterministic_rule",
                                  "rule_id": "x"},
                "reason_code": "r"})

    def test_choice_probabilities_must_be_normalised(self):
        with self.assertRaises(ValidationError):
            DecisionResult.model_validate({
                "task_id": "t", "subgoal_id": "s", "observation_id": "obs_1",
                "controller_epoch": 0, "candidate_set_hash": "a" * 64,
                "provider": "decider", "model_revision": "rev",
                "calibration_version": "cal-1", "elapsed_ms": 1.0, "route": "execute",
                "selected_candidate_id": "cand_a",
                "native_scores": {"score_semantics": "decider_systemone",
                                  "answers": {"action": {"type": "choice",
                                                          "choice": "cand_a",
                                                          "confidence": 0.9,
                                                          "certainty": 0.8,
                                                          "probabilities": {"cand_a": 0.9,
                                                                            "cand_b": 0.9}}}},
                "reason_code": "r"})

    def test_control_options_are_not_phone_actions(self):
        self.assertEqual(len(set(CONTROL_OPTIONS)), 4)
        for option in CONTROL_OPTIONS:
            self.assertTrue(option.startswith("cand_"))


if __name__ == "__main__":
    unittest.main()
