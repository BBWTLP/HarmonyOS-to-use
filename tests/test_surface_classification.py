"""A foreign surface must never be steered as a Weibo page.

Real failure: with the phone on the launcher, `foreground_bundle` was null and
`surface_kind()` returned "discover" because the only evidence it used was
"one clickable Flex near the top" - something the launcher also provides. The
setup then tapped launcher UI while trying to reach Weibo's search editor.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from agent_fakes import WEIBO, weibo_home  # noqa: E402
from agent_harness import has_weibo_evidence, surface_kind  # noqa: E402
from harmony_runtime.observation import catalog  # noqa: E402

DISPLAY = (1080, 2340, 0)


def root(children, bundle):
    return {"attributes": {"bundleName": bundle, "visible": "true",
                           "bounds": "[0,0][1080,2340]", "type": "Root"},
            "children": children}


def node(**attributes):
    attributes.setdefault("visible", "true")
    return {"attributes": attributes, "children": []}


def observation(tree, foreground_bundle=None):
    return {"foreground_bundle": foreground_bundle, "catalog": catalog(tree, DISPLAY)}


def launcher_observation():
    """A desktop whose only clickable top container is an anonymous Flex."""
    return observation(root([
        node(bundleName="com.ohos.sceneboard", type="Flex", bounds="[80,100][1000,220]",
             clickable="true"),
        node(bundleName="com.ohos.sceneboard", type="Text", text="时钟",
             bounds="[80,60][400,96]"),
    ], "com.ohos.sceneboard"))


def discover_observation():
    return observation(root([
        node(bundleName=WEIBO, type="Flex", bounds="[47,154][1273,276]", clickable="true"),
        node(bundleName=WEIBO, type="List", bounds="[0,306][1320,1645]"),
    ], WEIBO), foreground_bundle=WEIBO)


def editor_observation():
    return observation(root([
        node(bundleName=WEIBO, type="TextInput", text="", hint="搜索微博",
             bounds="[80,100][820,220]", clickable="true", focused="true"),
        node(bundleName=WEIBO, type="List", id="hot_search_list",
             bounds="[0,300][1080,2340]"),
    ], WEIBO), foreground_bundle=WEIBO)


class WeiboEvidenceTests(unittest.TestCase):
    def test_the_device_foreground_is_evidence(self):
        self.assertTrue(has_weibo_evidence(
            {"foreground_bundle": WEIBO, "catalog": []}))

    def test_the_ui_tree_bundle_is_independent_evidence(self):
        """Some builds never report a foreground bundle; the tree still can."""
        self.assertTrue(has_weibo_evidence(observation(weibo_home())))

    def test_a_foreign_tree_is_not_evidence(self):
        self.assertFalse(has_weibo_evidence(launcher_observation()))


class SurfaceKindTests(unittest.TestCase):
    def test_a_desktop_with_a_clickable_flex_is_foreign_not_discover(self):
        """RC4-A.1: no Weibo evidence means FOREIGN, a state of its own.

        It used to be "unknown", which conflated "this is not our app" with
        "this is our app but the page shape is unrecognised".
        """
        self.assertEqual(surface_kind(launcher_observation()), "foreign")

    def test_an_unidentified_surface_is_unknown(self):
        self.assertEqual(surface_kind({"foreground_bundle": None, "catalog": []}),
                         "foreign")

    def test_weibo_home_is_tabs(self):
        self.assertEqual(surface_kind(observation(weibo_home())), "tabs")

    def test_weibo_discover_is_discover(self):
        self.assertEqual(surface_kind(discover_observation()), "discover")

    def test_weibo_search_editor_is_search_editor(self):
        self.assertEqual(surface_kind(editor_observation()), "search_editor")


if __name__ == "__main__":
    unittest.main()
