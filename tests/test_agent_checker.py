import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, weibo_home, weibo_search_editor, weibo_results
from harmony_agent.checker import (ReadOnlyChecker, CheckerDenied, evaluate, normalize_target_key)
from harmony_agent.contracts import Predicate
from harmony_runtime.observation import snapshot


def observation(tree, epoch=0):
    obs = snapshot(tree, (1080, 2340, 0), {"status": "ok", "bundle": WEIBO})
    obs["controller_epoch"] = epoch
    obs["actionable"] = True
    obs["mode"] = "FAST"
    return obs


class TargetKeyTests(unittest.TestCase):
    def test_prefixes_are_stripped(self):
        self.assertEqual(normalize_target_key("id:tab_home"), "tab_home")
        self.assertEqual(normalize_target_key("a11y:close"), "close")
        self.assertEqual(normalize_target_key("type:TextInput"), "TextInput")
        self.assertEqual(normalize_target_key("搜索"), "搜索")

    def test_type_key_matches_by_node_type(self):
        obs = observation(weibo_search_editor())
        verdict = evaluate(Predicate(id="p", type="element_present",
                                     target_key="type:TextInput"), obs)
        self.assertEqual(verdict.verdict, "pass")

    def test_id_key_matches_resource_id(self):
        obs = observation(weibo_results("鸿蒙"))
        verdict = evaluate(Predicate(id="p", type="element_present",
                                     target_key="id:tab_user"), obs)
        self.assertEqual(verdict.verdict, "pass")

    def test_absent_element_reports_the_observable_catalog(self):
        obs = observation(weibo_home())
        verdict = evaluate(Predicate(id="p", type="element_absent",
                                     target_key="id:tab_missing"), obs)
        self.assertEqual(verdict.verdict, "pass")
        self.assertIn("absence describes", verdict.detail)


class PredicateVerdictTests(unittest.TestCase):
    def test_input_equals_compares_the_observed_value(self):
        obs = observation(weibo_search_editor("鸿蒙"))
        verdict = evaluate(Predicate(id="p", type="input_equals", target_key="type:TextInput",
                                     value_ref="query"), obs, arguments={"query": "鸿蒙"})
        self.assertEqual(verdict.verdict, "pass")
        verdict = evaluate(Predicate(id="p", type="input_equals", target_key="type:TextInput",
                                     value_ref="query"), obs, arguments={"query": "harmony"})
        self.assertEqual(verdict.verdict, "fail")

    def test_missing_target_is_inconclusive_not_false(self):
        obs = observation(weibo_home())
        verdict = evaluate(Predicate(id="p", type="input_equals",
                                     target_key="id:search_input", value="x"), obs)
        self.assertEqual(verdict.verdict, "inconclusive")

    def test_foreground_unknown_is_inconclusive(self):
        obs = observation(weibo_home())
        obs["foreground_bundle"] = None
        verdict = evaluate(Predicate(id="p", type="foreground_is", value=WEIBO), obs)
        self.assertEqual(verdict.verdict, "inconclusive")

    def test_page_changed_requires_a_baseline(self):
        obs = observation(weibo_home())
        verdict = evaluate(Predicate(id="p", type="page_changed"), obs)
        self.assertEqual(verdict.verdict, "inconclusive")
        other = observation(weibo_results("鸿蒙"))
        verdict = evaluate(Predicate(id="p", type="page_changed"), other, baseline=obs)
        self.assertEqual(verdict.verdict, "pass")

    def test_page_assertion_needs_a_semantic_evaluator(self):
        obs = observation(weibo_results("鸿蒙"))
        verdict = evaluate(Predicate(id="p", type="page_assertion",
                                     description="结果页展示查询结果"), obs)
        self.assertEqual(verdict.verdict, "inconclusive")
        verdict = evaluate(Predicate(id="p", type="page_assertion",
                                     description="结果页展示查询结果"), obs,
                           assertion_evaluator=lambda text, obs: ("pass", "evaluator"))
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.decided_by, "model_assisted")

    def test_all_of_combines_children(self):
        obs = observation(weibo_results("鸿蒙"))
        predicate = Predicate(id="p", type="all_of", predicates=[
            {"id": "a", "type": "foreground_is", "value": WEIBO},
            {"id": "b", "type": "text_equals", "value": "综合"},
        ])
        self.assertEqual(evaluate(predicate, obs).verdict, "pass")

    def test_text_equality_is_exact(self):
        obs = observation(weibo_results("鸿蒙"))
        self.assertEqual(evaluate(Predicate(id="p", type="text_equals",
                                            value="综合"), obs).verdict, "pass")
        self.assertEqual(evaluate(Predicate(id="p", type="text_equals",
                                            value="综合推荐"), obs).verdict, "fail")


class CheckerCapabilityTests(unittest.TestCase):
    def test_checker_refuses_to_dispatch(self):
        with self.assertRaises(CheckerDenied):
            ReadOnlyChecker().dispatch("tap")

    def test_unresolved_incident_blocks_success(self):
        obs = observation(weibo_results("鸿蒙"))
        checker = ReadOnlyChecker()
        report = checker.check([Predicate(id="p", type="text_equals", value="综合")], obs,
                               incident_free=False)
        self.assertEqual(report.verdict, "inconclusive")
        self.assertIn("unresolved device write", " ".join(report.limitations))


if __name__ == "__main__":
    unittest.main()
