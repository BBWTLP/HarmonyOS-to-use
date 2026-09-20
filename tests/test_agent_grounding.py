import base64
import io
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, weibo_home, weibo_results
from harmony_agent.candidates import CandidateError, CandidateRegistry
from harmony_agent.contracts import Predicate
from harmony_agent.grounding import (GroundingIntent, GroundingUnavailable, ground,
                                     is_black_screen)
from harmony_runtime.observation import snapshot, resolve
from harmony_runtime.contracts import Target, RuntimeFault


def observation(tree, *, observation_id="obs_1", epoch=0, actionable=True, image=None):
    obs = snapshot(tree, (1080, 2340, 0), {"status": "ok", "bundle": WEIBO})
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = epoch
    obs["actionable"] = actionable
    obs["mode"] = "FULL"
    if image is not None:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        obs["image"] = {"base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                        "mime_type": "image/png"}
    return obs


def png(colour=(40, 40, 40)):
    from PIL import Image
    return Image.new("RGB", (1080, 2340), colour)


class GroundingTests(unittest.TestCase):
    def test_stable_identity_beats_text(self):
        obs = observation(weibo_home())
        result = ground(obs, GroundingIntent(action_kind="tap", text="搜索",
                                             resource_id="search_entry"))
        self.assertEqual(result.layers_used, ["stable_identity"])
        self.assertEqual(result.targets[0].identity["resource_id"], "search_entry")
        self.assertEqual(result.targets[0].bundle, WEIBO)

    def test_exact_text_then_structure_layer(self):
        obs = observation(weibo_home())
        exact = ground(obs, GroundingIntent(action_kind="tap", text="我"))
        self.assertEqual(exact.layers_used, ["text_exact"])
        structural = ground(obs, GroundingIntent(action_kind="tap", text="微博"))
        self.assertIn("text_structure", structural.layers_used)
        self.assertTrue(all("substring matches" in note for note in structural.notes))

    def test_duplicate_text_reports_every_candidate_not_the_first(self):
        obs = observation(weibo_results("鸿蒙"))
        intent = GroundingIntent(action_kind="tap", text="鸿蒙")
        result = ground(obs, intent)
        self.assertGreaterEqual(len(result.targets), 1)
        refs = {target.target_ref for target in result.targets}
        self.assertEqual(len(refs), len(result.targets))

    def test_missing_tree_falls_back_and_reports_unavailable(self):
        obs = observation({"attributes": {"bundleName": WEIBO}, "children": []}, image=png())
        result = ground(obs, GroundingIntent(action_kind="tap", text="搜索"))
        self.assertEqual(result.targets, [])
        self.assertIn("all_layers_exhausted", result.notes)
        self.assertTrue(any(note.startswith("ocr_layer_skipped") for note in result.notes))
        self.assertTrue(any(note.startswith("image_layer_skipped") for note in result.notes))

    def test_black_screen_is_not_evidence(self):
        obs = observation(weibo_home(), image=png((0, 0, 0)))
        self.assertTrue(is_black_screen(obs))
        with self.assertRaises(GroundingUnavailable) as context:
            ground(obs, GroundingIntent(action_kind="tap", text="搜索"))
        self.assertEqual(context.exception.code, "no_visual_evidence")

    def test_ocr_layer_produces_a_visual_target_with_geometry(self):
        class StubOcr:
            available = True

            def recognize(self, image_bytes):
                return [{"text": "搜索", "bounds": (900, 100, 1060, 200), "confidence": 0.8}]

        obs = observation({"attributes": {"bundleName": WEIBO}, "children": []}, image=png())
        result = ground(obs, GroundingIntent(action_kind="tap", text="搜索"), ocr=StubOcr())
        self.assertEqual(result.layers_used, ["ocr_region"])
        target = result.targets[0]
        self.assertEqual(target.source, "ocr")
        self.assertEqual(target.geometry["crop"], [900, 100, 1060, 200])
        self.assertIn("no_ui_tree_evidence", target.missing)

    def test_unusable_observation_is_rejected_before_layers(self):
        obs = observation(weibo_home(), actionable=False)
        with self.assertRaises(GroundingUnavailable) as context:
            ground(obs, GroundingIntent(action_kind="tap", text="搜索"))
        self.assertEqual(context.exception.code, "stale_observation")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.obs = observation(weibo_home(), epoch=3)
        self.registry = CandidateRegistry()

    def build(self, **overrides):
        arguments = {"query": "鸿蒙", "text": "鸿蒙"}
        kwargs = dict(task_id="task_1", subgoal_id="sub_1", scope_id="scope_1",
                      observation=self.obs, controller_epoch=3,
                      intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
                      action_kind="tap", arguments=arguments,
                      expected_predicates=[Predicate(id="p", type="foreground_is",
                                                     value=WEIBO)])
        kwargs.update(overrides)
        return self.registry.build(**kwargs)

    def test_candidate_is_bound_to_observation_and_epoch(self):
        candidate_set = self.build()
        item = candidate_set.candidates[0]
        self.assertEqual(item.candidate.observation_id, self.obs["observation_id"])
        self.assertEqual(item.candidate.controller_epoch, 3)
        self.assertEqual(len(candidate_set.hash), 64)

    def test_argument_reference_is_resolved_by_the_service(self):
        candidate_set = self.build(argument_refs={"text": "arg.query"})
        self.assertEqual(candidate_set.candidates[0].argument_values["text"], "鸿蒙")

    def test_unknown_argument_reference_is_rejected(self):
        with self.assertRaises(CandidateError):
            self.build(argument_refs={"text": "arg.missing"})

    def test_epoch_change_invalidates_the_candidate(self):
        item = self.build().candidates[0]
        self.registry.resolve(item.candidate.candidate_id,
                              observation_id=self.obs["observation_id"], controller_epoch=3)
        with self.assertRaises(CandidateError) as context:
            self.registry.resolve(item.candidate.candidate_id,
                                  observation_id=self.obs["observation_id"], controller_epoch=4)
        self.assertEqual(context.exception.code, "epoch_mismatch")

    def test_candidate_set_offers_a_control_exit(self):
        candidate_set = self.build()
        criteria = candidate_set.choice_criteria()
        self.assertIn("cand_none_applicable", criteria)
        self.assertGreaterEqual(len(criteria), 2)


class GroundedTargetRuntimeTests(unittest.TestCase):
    """The runtime must accept the v2 handle and keep rejecting bare coordinates."""

    def setUp(self):
        self.obs = observation(weibo_results("鸿蒙"))

    def test_target_ref_resolves_to_exactly_one_node(self):
        intent = GroundingIntent(action_kind="tap", text="鸿蒙")
        target = ground(self.obs, intent).targets[0]
        handle = Target(target_ref=target.target_ref,
                        local_fingerprint=target.local_fingerprint,
                        observation_id=self.obs["observation_id"])
        node = resolve(self.obs, handle)
        self.assertEqual(node["target_fingerprint"], target.local_fingerprint)

    def test_target_ref_does_not_match_a_different_page(self):
        intent = GroundingIntent(action_kind="tap", text="鸿蒙")
        target = ground(self.obs, intent).targets[0]
        handle = Target(target_ref=target.target_ref,
                        local_fingerprint="f" * 64,
                        observation_id=self.obs["observation_id"])
        with self.assertRaises(RuntimeFault) as context:
            resolve(self.obs, handle)
        self.assertEqual(context.exception.code, "target_not_found")

    def test_mixing_v1_and_v2_selectors_is_rejected(self):
        with self.assertRaises(Exception):
            Target(text="鸿蒙", target_ref="gt_" + "a" * 32,
                   local_fingerprint="b" * 64)

    def test_target_ref_requires_the_local_fingerprint(self):
        with self.assertRaises(Exception):
            Target(target_ref="gt_" + "a" * 32)

    def test_model_cannot_forge_a_target_ref_format(self):
        with self.assertRaises(Exception):
            Target(target_ref="home", local_fingerprint="b" * 64)


if __name__ == "__main__":
    unittest.main()
