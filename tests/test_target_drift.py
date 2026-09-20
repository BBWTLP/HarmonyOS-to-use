"""Why a tap on an unchanged control can still be refused as stale.

Root cause proven here on the real control shape: the acceptance device's Weibo
search entry is a plain `Flex` whose *only* differences between two observations
are its own auto-increment `accessibilityId` (31985 -> 31995 -> 32000, measured)
and its subtree bytes. Its type, bounds, clickable/enabled state, resource id and
hierarchy are identical.

Two mechanisms are pinned down:
  1. `target_fingerprint` hashes the whole node subtree, so a decorative
     descendant that animates changes it even though the control did not.
  2. The dispatch comparison compares the entire catalog entry, so a volatile
     label on an otherwise identical control also refuses the tap.

Nothing here loosens a guard: the Runtime keeps refusing, and the adversarial
cases below prove that real semantic changes are still refused.
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
from harmony_runtime.observation import catalog, snapshot
from harmony_runtime.runtime import Runtime

DEVICE = "fake-device"
DISPLAY = (1080, 2340, 0)
FOREGROUND = {"status": "ok", "bundle": WEIBO}
COMPARED_FIELDS = ("type", "bounds", "hit_bounds", "enabled", "clickable",
                   "resource_id", "accessibility_id", "hierarchy", "host_window_id",
                   "bundle")


def search_entry(tree):
    return next(child for child in tree["children"]
                if child["attributes"].get("id") == "search_entry")


def decorative_child(frame: int) -> dict:
    """An unlabelled, non-clickable image: visible, but not actionable."""
    return {"attributes": {"bundleName": WEIBO, "visible": "true", "type": "Image",
                           "bounds": f"[{900 + frame},100][{1000 + frame},200]"},
            "children": []}


def entry_of(tree, resource_id):
    return next(item for item in catalog(tree, DISPLAY)
                if item.get("resource_id") == resource_id)


class SubtreeFingerprintTests(unittest.TestCase):
    """The fixture the plan asked for: only an unrelated descendant changes."""

    def setUp(self):
        self.before_tree = weibo_home()
        search_entry(self.before_tree)["children"] = [decorative_child(0)]
        self.after_tree = copy.deepcopy(self.before_tree)
        search_entry(self.after_tree)["children"] = [decorative_child(1)]
        self.before = entry_of(self.before_tree, "search_entry")
        self.after = entry_of(self.after_tree, "search_entry")

    def test_the_control_itself_is_byte_identical(self):
        for field in COMPARED_FIELDS:
            self.assertEqual(self.before.get(field), self.after.get(field), field)

    def test_the_page_identity_is_unchanged(self):
        """navigation_tree already normalises this animation, so the page is same."""
        first = snapshot(self.before_tree, DISPLAY, FOREGROUND)
        second = snapshot(self.after_tree, DISPLAY, FOREGROUND)
        self.assertEqual(first["navigation_fingerprint"], second["navigation_fingerprint"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_the_subtree_fingerprint_still_changes(self):
        self.assertNotEqual(self.before["target_fingerprint"],
                            self.after["target_fingerprint"])


class DriftingDevice(FakeWeiboDevice):
    """Weibo home whose search control is perturbed from the second read on."""

    def __init__(self, serial="fake-device", perturb=None):
        super().__init__(serial)
        self.perturb = perturb
        self.frames = 0

    def tree(self):
        self.frames += 1
        base = copy.deepcopy(weibo_home())
        search_entry(base)["children"] = [decorative_child(0)]
        if self.perturb is not None and self.frames >= 2:
            self.perturb(base, search_entry(base), self.frames)
        return base


def decorative(base, control, frame):
    control["children"] = [decorative_child(frame)]


def moved(base, control, frame):
    control["attributes"]["bounds"] = f"[{900 + frame},100][{1060 + frame},200]"


def relabelled(base, control, frame):
    control["attributes"]["text"] = "搜索" + "热" * frame


def disabled(base, control, frame):
    control["attributes"]["enabled"] = "false"


def replaced(base, control, frame):
    control["attributes"]["id"] = "different_entry"


def new_page(base, control, frame):
    base["children"].append({"attributes": {
        "bundleName": WEIBO, "visible": "true", "type": "Button",
        "text": "新页面", "bounds": "[40,300][600,400]", "clickable": "true"},
        "children": []})


class DispatchStalenessTests(unittest.TestCase):
    """Every attempt below grounds on the search entry and then taps it."""

    def attempt(self, perturb):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            created = []

            def factory(serial):
                device = DriftingDevice(serial, perturb)
                created.append(device)
                return device

            runtime = Runtime(root, factory=factory, discover=lambda: [DEVICE])
            try:
                session = runtime.session("owner", "open", device_id=DEVICE)["session_id"]
                observation = runtime.observe("owner", session, mode="FAST")
                entry = next(item for item in observation["catalog"]
                             if item.get("resource_id") == "search_entry")
                try:
                    runtime.act("owner", {
                        "session_id": session, "request_id": "r1",
                        "observation_id": observation["observation_id"],
                        "action": {"kind": "tap",
                                   "target": {"action_id": entry["action_id"]}}})
                    return None
                except RuntimeFault as fault:
                    return fault.code
            finally:
                runtime.close()

    def test_an_animated_decorative_descendant_refuses_the_tap(self):
        self.assertEqual(self.attempt(decorative), "stale_observation")

    def test_a_moved_control_is_refused(self):
        self.assertEqual(self.attempt(moved), "stale_observation")

    def test_a_relabelled_control_is_refused(self):
        self.assertEqual(self.attempt(relabelled), "stale_observation")

    def test_a_disabled_control_is_refused(self):
        self.assertEqual(self.attempt(disabled), "stale_observation")

    def test_a_replaced_control_is_refused(self):
        self.assertEqual(self.attempt(replaced), "stale_observation")

    def test_a_changed_page_is_refused(self):
        self.assertEqual(self.attempt(new_page), "stale_observation")

    def test_a_control_that_never_changes_is_dispatched(self):
        self.assertIsNone(self.attempt(None))


if __name__ == "__main__":
    unittest.main()
