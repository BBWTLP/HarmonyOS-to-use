"""RC4-A: cross-observation target identity is evidence, not catalog position.

The defect this pins down (measured on the acceptance device): across two
consecutive observations 103 of 116 nodes matched by type+bounds were identical
in every attribute, label, geometry and subtree hash, yet their observation-local
`action_id` / `parent_action_id` had moved - because the catalog is rebuilt on
every read. The runtime compared the whole entry, so a control that had not
changed was refused as `Target changed`.

Every case below is one line from the RC4-A requirement list.
"""
from __future__ import annotations

import copy
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice, weibo_home
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.observation import catalog
from harmony_runtime.runtime import Runtime
from harmony_runtime.target_identity import (AMBIGUOUS, CHANGED, EXACT, MISSING,
                                             STABLE_REBIND, StableTargetMatcher)

DEVICE = "fake-device"
DISPLAY = (1080, 2340, 0)
CONTROL = {"type": "Button", "resource_id": "search_entry", "text": "搜索",
           "description": "", "hint": "", "bounds": [900, 100, 1060, 200],
           "hit_bounds": [900, 100, 1060, 200], "enabled": True, "clickable": True,
           "focused": False, "hierarchy": "ROOT0,0,2", "host_window_id": "1",
           "accessibility_id": "31985", "bundle": WEIBO, "checked": None,
           "selected": None, "text_observed": True, "parent_action_id": "n0",
           "action_id": "n5", "target_fingerprint": "f" * 64}


def variant(**overrides) -> dict:
    return {**copy.deepcopy(CONTROL), **overrides}


class MatcherIdentityTests(unittest.TestCase):
    def setUp(self):
        self.matcher = StableTargetMatcher()

    def test_only_action_id_changed_matches(self):
        result = self.matcher.match(variant(), [variant(action_id="n9")])
        self.assertEqual(result.outcome, EXACT)
        self.assertEqual(result.entry["action_id"], "n9")

    def test_only_parent_action_id_changed_matches(self):
        result = self.matcher.match(variant(), [variant(parent_action_id="n42")])
        self.assertEqual(result.outcome, EXACT)

    def test_an_animated_descendant_matches(self):
        result = self.matcher.match(variant(), [variant(target_fingerprint="a" * 64)])
        self.assertEqual(result.outcome, STABLE_REBIND)

    def test_a_volatile_accessibility_id_still_matches_on_other_evidence(self):
        result = self.matcher.match(variant(), [variant(accessibility_id="32000")])
        self.assertEqual(result.outcome, STABLE_REBIND)
        self.assertIn("hierarchy", result.evidence["evidence"])

    def test_moved_bounds_are_rejected(self):
        result = self.matcher.match(variant(), [variant(bounds=[900, 400, 1060, 500],
                                                        hit_bounds=[900, 400, 1060, 500])])
        self.assertEqual(result.outcome, CHANGED)
        self.assertIn("geometry", result.evidence["changed_fields"])

    def test_disabled_or_unclickable_is_rejected(self):
        for field in ("enabled", "clickable"):
            result = self.matcher.match(variant(), [variant(**{field: False})])
            self.assertEqual(result.outcome, CHANGED, field)
            self.assertIn(field, result.evidence["changed_fields"])

    def test_a_changed_label_or_identity_is_rejected(self):
        for field, value in (("text", "搜索热榜"), ("resource_id", "other_entry")):
            result = self.matcher.match(variant(), [variant(**{field: value})])
            self.assertEqual(result.outcome, CHANGED, field)

    def test_a_replaced_control_is_rejected(self):
        """Same slot, different control: refuse rather than tap the new occupant."""
        replacement = variant(resource_id="", hierarchy="ROOT0,0,9", action_id="n5",
                              text="😀", accessibility_id="999")
        result = self.matcher.match(variant(), [replacement])
        self.assertEqual(result.outcome, CHANGED)

    def test_two_equally_good_candidates_are_ambiguous(self):
        first = variant(resource_id="", hierarchy="", accessibility_id="1",
                        action_id="n1")
        second = variant(resource_id="", hierarchy="", accessibility_id="2",
                         action_id="n2")
        result = self.matcher.match(first, [second, copy.deepcopy(second)])
        self.assertEqual(result.outcome, AMBIGUOUS)
        self.assertIsNone(result.entry)

    def test_a_different_host_window_is_rejected(self):
        result = self.matcher.match(variant(), [variant(host_window_id="2")])
        self.assertEqual(result.outcome, CHANGED)
        self.assertIn("host_window_id", result.evidence["changed_fields"])

    def test_no_candidate_is_missing(self):
        result = self.matcher.match(variant(), [variant(bounds=[0, 0, 10, 10],
                                                        hit_bounds=[0, 0, 10, 10],
                                                        type="Text")])
        self.assertEqual(result.outcome, MISSING)

    def test_the_subtree_hash_alone_can_never_confirm_a_match(self):
        """A node that only shares the subtree hash is not the same control."""
        stranger = variant(resource_id="", hierarchy="ROOT9", accessibility_id="",
                           action_id="n1", target_fingerprint="f" * 64,
                           bounds=[0, 1200, 100, 1300], hit_bounds=[0, 1200, 100, 1300])
        result = self.matcher.match(variant(), [stranger])
        self.assertNotIn(result.outcome, (EXACT, STABLE_REBIND))


def decorative_child(frame: int) -> dict:
    """An unlabelled, non-clickable image whose bounds animate."""
    return {"attributes": {"bundleName": WEIBO, "visible": "true", "type": "Image",
                           "bounds": f"[{900 + frame},100][{1000 + frame},200]"},
            "children": []}


class PositionalIndexTests(unittest.TestCase):
    """The runtime must not read a shifted index as a changed target."""

    def observe_and_tap(self, device):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime = Runtime(root, factory=lambda serial: device,
                              discover=lambda: [DEVICE])
            try:
                session = runtime.session("owner", "open", device_id=DEVICE)["session_id"]
                observation = runtime.observe("owner", session, mode="FAST")
                entry = next(item for item in observation["catalog"]
                             if item.get("resource_id") == "search_entry")
                result = runtime.act("owner", {
                    "session_id": session, "request_id": "r1",
                    "observation_id": observation["observation_id"],
                    "action": {"kind": "tap", "target": {"action_id": entry["action_id"]}}})
                status = runtime.session("owner", "status", session_id=session)
                return result, status
            finally:
                runtime.close()

    def test_a_target_rebind_is_counted_and_reported(self):
        class DriftingDevice(FakeWeiboDevice):
            def __init__(self, serial="fake-device"):
                super().__init__(serial)
                self.reads = 0

            def tree(self):
                self.reads += 1
                base = copy.deepcopy(weibo_home())
                control = next(child for child in base["children"]
                               if child["attributes"].get("id") == "search_entry")
                control["children"] = [decorative_child(0 if self.reads < 2 else 1)]
                return base

        result, status = self.observe_and_tap(DriftingDevice())
        self.assertEqual(result["execution_status"], "executed")
        self.assertEqual(result["target_match"]["outcome"], "stable_rebind")
        self.assertEqual(status["target_match_counts"], {"stable_rebind": 1})

    def test_a_stable_page_records_no_rebind(self):
        result, status = self.observe_and_tap(FakeWeiboDevice())
        self.assertEqual(result["execution_status"], "executed")
        self.assertNotIn("target_match", result)
        self.assertEqual(status["target_match_counts"], {})

    def test_a_navigation_change_is_still_refused_at_the_page_level(self):
        """RC4-A keeps the navigation identity guard.

        Catalog indices can only move when the page itself changed - catalog
        membership is a subset of the navigation tree - and that case is refused
        *before* the matcher runs. RC4-A therefore re-identifies targets only on
        pages whose navigation identity is unchanged.
        """

        class NewPageDevice(FakeWeiboDevice):
            def __init__(self, serial="fake-device"):
                super().__init__(serial)
                self.reads = 0

            def tree(self):
                self.reads += 1
                base = copy.deepcopy(weibo_home())
                if self.reads >= 2:
                    base["children"].append({
                        "attributes": {"bundleName": WEIBO, "visible": "true",
                                       "type": "Button", "text": "另一个页面",
                                       "bounds": "[40,300][600,400]", "clickable": "true"},
                        "children": []})
                return base

        with self.assertRaises(RuntimeFault) as error:
            self.observe_and_tap(NewPageDevice())
        self.assertEqual(error.exception.code, "stale_observation")
        self.assertIn("Page changed", str(error.exception))

    def test_an_ambiguous_target_is_never_dispatched(self):
        class TwinDevice(FakeWeiboDevice):
            """Two identical controls appear on the second read (dynamic list)."""

            def __init__(self, serial="fake-device"):
                super().__init__(serial)
                self.reads = 0

            def tree(self):
                self.reads += 1
                base = copy.deepcopy(weibo_home())
                control = next(child for child in base["children"]
                               if child["attributes"].get("id") == "search_entry")
                clone = copy.deepcopy(control)
                clone["attributes"]["id"] = ""
                clone["attributes"]["bounds"] = "[40,300][200,360]"
                clone["attributes"].pop("text", None)
                control_copy = copy.deepcopy(clone)
                if self.reads >= 2:
                    # The original loses its distinctive evidence and an exact
                    # twin appears, so no candidate can be told apart.
                    control["attributes"]["id"] = ""
                    control["attributes"]["bounds"] = "[40,300][200,360]"
                    control["attributes"].pop("text", None)
                    base["children"].append(control_copy)
                return base

        with self.assertRaises(RuntimeFault) as error:
            self.observe_and_tap(TwinDevice())
        self.assertIn(error.exception.code, ("target_ambiguous", "stale_observation"))


if __name__ == "__main__":
    unittest.main()
