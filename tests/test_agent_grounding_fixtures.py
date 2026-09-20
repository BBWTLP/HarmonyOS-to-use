"""v3.2 Phase 4: fixture regression for the existing grounding pipeline.

Nothing here rewrites `grounding.py`. Each fixture is a raw UI tree plus the
intent a planner would send, so the layer order and the ambiguity policy are
pinned by evidence instead of by reading the implementation.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO
from harmony_agent.grounding import (GroundingIntent, filter_for_intent, ground,
                                     looks_like_authentication)
from harmony_runtime.observation import snapshot

SYSTEM = "com.ohos.sceneboard"


def node(**attributes):
    attributes.setdefault("bundleName", WEIBO)
    attributes.setdefault("visible", "true")
    return {"attributes": attributes, "children": []}


def parent(children, **attributes):
    attributes.setdefault("bundleName", WEIBO)
    attributes.setdefault("visible", "true")
    return {"attributes": attributes, "children": children}


def tree(children):
    return {"attributes": {"bundleName": WEIBO, "visible": "true",
                           "bounds": "[0,0][1080,2340]", "type": "Root"},
            "children": children}


def observation(tree_data, *, observation_id="obs_fixture", epoch=0,
                display=(1080, 2340, 0), foreground=WEIBO):
    obs = snapshot(tree_data, display, {"status": "ok", "bundle": foreground})
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = epoch
    obs["actionable"] = True
    obs["mode"] = "FULL"
    return obs


def tap(**intent):
    return GroundingIntent(action_kind="tap", **intent)


class AncestorResolutionTests(unittest.TestCase):
    def test_a_text_label_resolves_to_its_clickable_ancestor(self):
        page = tree([
            parent([node(type="Text", text="首页", bounds="[100,2240][170,2300]")],
                   type="Row", clickable="true", id="tab_home",
                   bounds="[0,2200][270,2340]"),
        ])
        result = ground(observation(page), tap(text="首页"))
        self.assertEqual(result.layers_used, ["text_exact"])
        target = result.targets[0]
        self.assertEqual(target.role, "Row")
        self.assertEqual(target.identity["resource_id"], "tab_home")
        self.assertTrue(target.clickable)

    def test_the_nearest_clickable_ancestor_wins(self):
        page = tree([
            parent([
                parent([node(type="Text", text="设置", bounds="[40,400][120,440]")],
                       type="Row", id="inner", clickable="true",
                       bounds="[0,380][540,900]"),
            ], type="Column", id="outer", clickable="true", bounds="[0,380][1080,900]"),
        ])
        result = ground(observation(page), tap(text="设置"))
        self.assertEqual(result.targets[0].identity["resource_id"], "inner")

    def test_a_label_whose_parent_is_not_clickable_stays_its_own_candidate(self):
        page = tree([
            parent([node(type="Text", text="免责声明", bounds="[40,400][600,460]")],
                   type="Column", id="plain", bounds="[0,380][1080,900]"),
        ])
        result = ground(observation(page), tap(text="免责声明", require_clickable=True))
        self.assertEqual(len(result.targets), 1)
        kept = filter_for_intent(result.targets, tap(text="免责声明",
                                                    require_clickable=True))
        self.assertEqual(kept, [])


class AmbiguityTests(unittest.TestCase):
    def test_duplicate_labels_across_navigation_and_content_are_both_reported(self):
        page = tree([
            node(type="Button", text="搜索", bounds="[900,100][1060,200]",
                 clickable="true", id="search_entry"),
            parent([node(type="Text", text="搜索", bounds="[40,700][200,760]")],
                   type="Column", id="feed_card", clickable="true",
                   bounds="[0,660][1080,900]"),
        ])
        result = ground(observation(page), tap(text="搜索"))
        self.assertEqual(len(result.targets), 2)
        self.assertEqual({target.identity["resource_id"] for target in result.targets},
                         {"search_entry", "feed_card"})
        self.assertEqual(len({target.target_ref for target in result.targets}), 2)

    def test_the_same_text_in_two_roles_is_not_merged(self):
        page = tree([
            node(type="Button", text="确定", bounds="[100,100][300,200]",
                 clickable="true", id="confirm_button"),
            node(type="Text", text="确定", bounds="[100,400][300,460]", id="confirm_label"),
        ])
        result = ground(observation(page), tap(text="确定"))
        self.assertEqual({target.role for target in result.targets}, {"Button", "Text"})

    def test_a_popup_keeps_the_background_candidate_visible(self):
        page = tree([
            parent([node(type="Text", text="删除", bounds="[40,800][200,860]")],
                   type="Column", id="list_row", clickable="true",
                   bounds="[0,760][1080,900]"),
            parent([node(type="Button", text="删除", bounds="[600,1200][900,1300]",
                         clickable="true", id="popup_delete")],
                   type="Dialog", id="confirm_dialog", bounds="[400,1100][1000,1400]"),
        ])
        result = ground(observation(page), tap(text="删除"))
        self.assertEqual(len(result.targets), 2)
        self.assertEqual({target.identity["resource_id"] for target in result.targets},
                         {"list_row", "popup_delete"})


class IdentityLayerTests(unittest.TestCase):
    def test_resource_id_beats_an_ambiguous_text(self):
        page = tree([
            node(type="Button", text="打开", bounds="[40,400][200,460]",
                 clickable="true", id="open_a"),
            node(type="Button", text="打开", bounds="[40,500][200,560]",
                 clickable="true", id="open_b"),
        ])
        result = ground(observation(page), tap(text="打开", resource_id="open_b"))
        self.assertEqual(result.layers_used, ["stable_identity"])
        self.assertEqual(len(result.targets), 1)
        self.assertEqual(result.targets[0].identity["resource_id"], "open_b")

    def test_accessibility_id_is_a_stable_identity(self):
        page = tree([
            node(type="Button", text="发布", bounds="[900,100][1060,200]",
                 clickable="true", accessibilityId="publish_action"),
        ])
        result = ground(observation(page), tap(accessibility_id="publish_action"))
        self.assertEqual(result.layers_used, ["stable_identity"])
        self.assertEqual(result.targets[0].identity["accessibility_id"], "publish_action")
        self.assertEqual(result.targets[0].confidence, 1.0)

    def test_description_is_used_when_no_text_or_id_matches(self):
        page = tree([
            node(type="Image", description="分享到微信", bounds="[900,100][1060,200]",
                 clickable="true", id="share_icon"),
        ])
        result = ground(observation(page), tap(description="分享到微信"))
        self.assertEqual(result.layers_used, ["description_structure"])
        self.assertEqual(result.targets[0].identity["resource_id"], "share_icon")


class VisibilityAndStateTests(unittest.TestCase):
    def test_a_hidden_subtree_is_not_groundable(self):
        page = tree([
            parent([node(type="Button", text="隐藏按钮", bounds="[40,400][200,460]",
                         clickable="true", id="hidden_button")],
                   type="Column", id="hidden_panel", visible="false",
                   bounds="[0,380][1080,900]"),
            node(type="Button", text="可见按钮", bounds="[40,1000][200,1060]",
                 clickable="true", id="visible_button"),
        ])
        obs = observation(page)
        identifiers = {item["resource_id"] for item in obs["catalog"]}
        self.assertNotIn("hidden_button", identifiers)
        self.assertIn("visible_button", identifiers)
        result = ground(obs, tap(text="隐藏按钮"))
        self.assertEqual(result.targets, [])

    def test_a_disabled_node_is_reported_but_not_dispatchable(self):
        page = tree([
            node(type="Button", text="下一页", bounds="[900,2000][1060,2100]",
                 clickable="true", enabled="false", id="next_page"),
        ])
        obs = observation(page)
        result = ground(obs, tap(text="下一页"))
        self.assertEqual(len(result.targets), 1)
        self.assertFalse(result.targets[0].enabled)
        self.assertEqual(filter_for_intent(result.targets, tap(text="下一页")), [])

    def test_a_fully_offscreen_node_is_not_in_the_catalog(self):
        page = tree([
            node(type="Button", text="屏幕外", bounds="[0,2400][200,2500]",
                 clickable="true", id="offscreen"),
            node(type="Button", text="屏幕内", bounds="[0,100][200,200]",
                 clickable="true", id="onscreen"),
        ])
        obs = observation(page)
        identifiers = {item["resource_id"] for item in obs["catalog"]}
        self.assertNotIn("offscreen", identifiers)
        self.assertIn("onscreen", identifiers)

    def test_a_partially_offscreen_node_keeps_clipped_hit_bounds(self):
        page = tree([
            node(type="Button", text="部分可见", bounds="[-100,2200][300,2500]",
                 clickable="true", id="partial"),
        ])
        entry = observation(page)["catalog"][0]
        self.assertEqual(entry["bounds"], [-100, 2200, 300, 2500])
        self.assertEqual(entry["hit_bounds"], [0, 2200, 300, 2340])

    def test_a_focused_input_can_be_required(self):
        page = tree([
            node(type="TextInput", text="", hint="搜索微博", bounds="[80,100][820,220]",
                 clickable="true", focused="true", id="search_input"),
            node(type="TextInput", text="", hint="搜索微博", bounds="[80,300][820,420]",
                 clickable="true", focused="false", id="search_input"),
        ])
        obs = observation(page)
        intent = GroundingIntent(action_kind="input_text", resource_id="search_input",
                                 require_focus=True)
        result = ground(obs, intent)
        # Two nodes carry the same resource id; only the focused one is usable.
        self.assertEqual(len(result.targets), 2)
        kept = filter_for_intent(result.targets, intent)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].focused)
        self.assertEqual(kept[0].identity["resource_id"], "search_input")

    def test_a_placeholder_hint_is_not_text_evidence(self):
        """Documented limitation: `hint` is carried but never matched as text.

        An empty focused search box has text "" plus hint "搜索微博"; a planner
        must address it by resource id or accessibility id, not by placeholder
        copy. Grounding never guesses from placeholder text.
        """
        page = tree([
            node(type="TextInput", text="", hint="搜索微博", bounds="[80,100][820,220]",
                 clickable="true", focused="true", id="search_input"),
        ])
        obs = observation(page)
        self.assertEqual(obs["catalog"][0]["hint"], "搜索微博")
        result = ground(obs, tap(text="搜索微博"))
        self.assertEqual(result.targets, [])
        by_id = ground(obs, tap(resource_id="search_input"))
        self.assertEqual(len(by_id.targets), 1)

    def test_dynamic_numeric_text_matches_exactly(self):
        page = tree([
            node(type="Slider", text="42", bounds="[40,400][1000,460]", id="progress"),
        ])
        result = ground(observation(page), tap(text="42"))
        self.assertEqual(len(result.targets), 1)
        self.assertEqual(result.targets[0].text, "42")


class DegradedEvidenceTests(unittest.TestCase):
    def test_a_partial_tree_records_what_is_missing(self):
        page = tree([node(bounds="[40,400][200,460]")])
        result = ground(observation(page), tap(text="任意"))
        self.assertEqual(result.targets, [])
        self.assertIn("tree_layers_found_no_candidate", result.notes)

    def test_a_node_without_geometry_is_not_a_candidate(self):
        page = tree([
            node(type="Button", text="无边界", clickable="true", id="no_bounds"),
            node(type="Button", text="有边界", clickable="true", id="with_bounds",
                 bounds="[40,400][200,460]"),
        ])
        obs = observation(page)
        identifiers = {item["resource_id"] for item in obs["catalog"]}
        self.assertNotIn("no_bounds", identifiers)
        self.assertIn("with_bounds", identifiers)

    def test_a_missing_tree_grounds_nothing_and_says_so(self):
        result = ground(observation(tree([])), tap(text="搜索"))
        self.assertEqual(result.targets, [])
        self.assertIn("all_layers_exhausted", result.notes)


class AuthenticationFixtureTests(unittest.TestCase):
    def auth_page(self):
        return tree([
            parent([node(bundleName=SYSTEM, type="UIExtensionComponent",
                         id="userauthuiextensionability/window",
                         bounds="[0,0][1080,2340]")],
                   bundleName=SYSTEM, type="Stack", id="auth_root",
                   bounds="[0,0][1080,2340]"),
            parent([node(bundleName=SYSTEM, type="Text", text="请输入隐私密码",
                         id="NumberPasswordTitleGroup", bounds="[300,800][780,900]"),
                    node(bundleName=SYSTEM, type="TextInput", id="pinSix",
                         bounds="[300,950][780,1050]")],
                   bundleName=SYSTEM, type="Column", id="auth_panel",
                   bounds="[0,700][1080,1200]"),
        ])

    def test_the_system_credential_surface_is_recognized(self):
        obs = observation(self.auth_page())
        self.assertEqual(obs["blocking_dialog"]["code"], "authentication_required")
        self.assertEqual(obs["blocking_dialog"]["allowed_actions"],
                         ["back", "home", "launch"])
        self.assertTrue(looks_like_authentication(obs))

    def test_grounding_still_reports_candidates_but_the_service_blocks_interaction(self):
        obs = observation(self.auth_page())
        result = ground(obs, tap(resource_id="pinSix"))
        self.assertTrue(result.targets)
        # The guard, not the grounder, is the authority that refuses the action.
        self.assertEqual(obs["blocking_dialog"]["code"], "authentication_required")


class TargetProvenanceTests(unittest.TestCase):
    """Every candidate is observation-bound, epoch-bound and expiring."""

    def test_grounded_targets_carry_authority_metadata(self):
        page = tree([
            node(type="Button", text="搜索", bounds="[900,100][1060,200]",
                 clickable="true", id="search_entry"),
        ])
        result = ground(observation(page, observation_id="obs_prov", epoch=5),
                        tap(text="搜索"))
        target = result.targets[0]
        self.assertEqual(target.observation_id, "obs_prov")
        self.assertEqual(target.controller_epoch, 5)
        self.assertEqual(target.source, "ui_tree")
        self.assertTrue(target.expires_at)
        self.assertTrue(target.local_fingerprint)
        self.assertEqual(target.revalidate, "identity_and_fingerprint")
        self.assertIn(target.action_kind, ("tap", "long_press"))

    def test_a_target_ref_is_derived_from_the_observation_not_from_the_model(self):
        page = tree([
            node(type="Button", text="搜索", bounds="[900,100][1060,200]",
                 clickable="true", id="search_entry"),
        ])
        first = ground(observation(page, observation_id="obs_a"), tap(text="搜索"))
        second = ground(observation(page, observation_id="obs_b"), tap(text="搜索"))
        self.assertNotEqual(first.targets[0].target_ref, second.targets[0].target_ref)


if __name__ == "__main__":
    unittest.main()
