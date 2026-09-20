"""v3.2 Phase 5: a visual region is a proposal, never a dispatch authority.

OCR/VLM produce a region; the runtime re-derives the crop digest from the
pre-dispatch screenshot and refuses the region when geometry, bounds or pixels
no longer match. These tests use a fake device, so they prove offline semantics
only (`blocked_device` for real execution).
"""
from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from PIL import Image
from harmony_agent.grounding import GroundingIntent, ground
from harmony_runtime.contracts import RuntimeFault, Target
from harmony_runtime.observation import snapshot
from harmony_runtime.runtime import Runtime
from harmony_runtime.visual import region_digest

DEVICE = "fake-device"
WEIBO = "com.sina.weibo.stage"
DISPLAY = (1080, 2340, 0)
REGION = (900, 100, 1060, 200)


class VisualDevice:
    """A device whose screen is a canvas the test can repaint."""

    supports_foreground = True

    def __init__(self, serial, colour=(40, 40, 40)):
        self.serial = serial
        self.writes = []
        self.colour = colour
        self.rotation = 0
        self.tree_calls = 0

    # -- read path ----------------------------------------------------------
    def screen_state(self):
        return {"screen_on": True, "screen_locked": False}

    def foreground(self):
        return {"status": "ok", "bundle": WEIBO}

    def display(self):
        return (DISPLAY[0], DISPLAY[1], self.rotation)

    def tree(self):
        self.tree_calls += 1
        # A canvas app: no UI-tree node exists for the region at all.
        return {"attributes": {"bundleName": WEIBO, "type": "Root",
                               "bounds": "[0,0][1080,2340]"},
                "children": [{"attributes": {"bundleName": WEIBO, "type": "Text",
                                             "text": "画布",
                                             "bounds": "[40,3000][300,3060]"},
                              "children": []}]}

    def screenshot(self):
        return Image.new("RGB", (DISPLAY[0], DISPLAY[1]), self.colour)

    def dispatch(self, action, target):
        hit = list(target.get("hit_bounds") or [])
        centre = ((hit[0] + hit[2]) // 2, (hit[1] + hit[3]) // 2) if len(hit) == 4 else None
        self.writes.append({"kind": action.kind, "hit_bounds": hit, "centre": centre,
                            "type": target.get("type"),
                            "visual_source": target.get("visual_source")})

    def close(self):
        pass


def observation_for(device, observation_id="obs_visual", epoch=0):
    tree = device.tree()
    obs = snapshot(tree, device.display(), device.foreground())
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = epoch
    obs["actionable"] = True
    obs["mode"] = "FULL"
    obs["tree_captured_at"] = obs["captured_at"]
    obs["image_captured_at"] = obs["captured_at"]
    buffer = io.BytesIO()
    device.screenshot().save(buffer, format="PNG")
    import base64
    obs["image"] = {"mime_type": "image/png",
                    "base64": base64.b64encode(buffer.getvalue()).decode("ascii")}
    return obs


class ProposalTests(unittest.TestCase):
    """The proposer produces a digest; it never produces a dispatchable point."""

    def setUp(self):
        self.device = VisualDevice(DEVICE)
        self.obs = observation_for(self.device)

    def test_an_ocr_region_becomes_a_revalidatable_proposal(self):
        class Ocr:
            available = True

            def recognize(self, image_bytes):
                return [{"text": "播放", "bounds": REGION, "confidence": 0.9}]

        result = ground(self.obs, GroundingIntent(action_kind="tap", text="播放"),
                        ocr=Ocr())
        target = result.targets[0]
        self.assertEqual(target.source, "ocr")
        self.assertTrue(target.target_ref.startswith("gt_"))
        # The handle fingerprint is the runtime's own crop digest, not a guess.
        expected = region_digest(
            __import__("base64").b64decode(self.obs["image"]["base64"]), REGION,
            (DISPLAY[0], DISPLAY[1]))
        self.assertEqual(target.local_fingerprint, expected)
        visual = target.visual_ref()
        self.assertEqual(visual["region"], list(REGION))
        self.assertEqual(visual["crop_digest"], expected)
        self.assertEqual(visual["source"], "ocr")
        self.assertEqual(visual["label"], "播放")
        self.assertEqual((visual["display_width"], visual["display_height"],
                          visual["rotation"]), DISPLAY)

    def test_a_region_outside_the_display_is_rejected_not_clamped(self):
        class Ocr:
            available = True

            def recognize(self, image_bytes):
                return [{"text": "播放", "bounds": (1200, 100, 1400, 200),
                         "confidence": 0.9}]

        result = ground(self.obs, GroundingIntent(action_kind="tap", text="播放"),
                        ocr=Ocr())
        self.assertEqual(result.targets, [])
        self.assertIn({"text": "播放", "reason": "region_not_revalidatable"},
                      result.rejected)

    def test_a_ui_tree_target_carries_no_visual_block(self):
        obs = snapshot({"attributes": {"bundleName": WEIBO, "type": "Root"},
                        "children": [{"attributes": {
                            "bundleName": WEIBO, "type": "Button", "text": "播放",
                            "clickable": "true", "id": "play",
                            "bounds": "[900,100][1060,200]"}, "children": []}]},
                       DISPLAY, {"status": "ok", "bundle": WEIBO})
        obs["observation_id"] = "obs_tree"
        target = ground(obs, GroundingIntent(action_kind="tap", text="播放")).targets[0]
        self.assertIsNone(target.visual_ref())
        self.assertNotIn("visual", target.to_ref())


class ContractTests(unittest.TestCase):
    def visual(self, **overrides):
        payload = {"region": list(REGION), "crop_digest": "a" * 64, "source": "ocr",
                   "label": "播放", "display_width": 1080, "display_height": 2340,
                   "rotation": 0}
        payload.update(overrides)
        return payload

    def test_a_visual_region_requires_a_grounded_handle(self):
        with self.assertRaises(Exception):
            Target(text="播放", visual=self.visual())

    def test_the_handle_fingerprint_must_equal_the_crop_digest(self):
        with self.assertRaises(Exception):
            Target(target_ref="gt_" + "a" * 32, local_fingerprint="b" * 64,
                   visual=self.visual())

    def test_an_empty_label_is_rejected(self):
        with self.assertRaises(Exception):
            Target(target_ref="gt_" + "a" * 32, local_fingerprint="a" * 64,
                   visual=self.visual(label=""))

    def test_an_unknown_visual_source_is_rejected(self):
        with self.assertRaises(Exception):
            Target(target_ref="gt_" + "a" * 32, local_fingerprint="a" * 64,
                   visual=self.visual(source="llm_guess"))

    def test_a_valid_visual_handle_is_accepted(self):
        target = Target(target_ref="gt_" + "a" * 32, local_fingerprint="a" * 64,
                        visual=self.visual())
        self.assertEqual(target.visual.source, "ocr")


class RuntimeVisualDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Device(VisualDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root, factory=Device, discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session_id = self.runtime.session(self.owner, "open",
                                               device_id=DEVICE)["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def observe_with_image(self):
        return self.runtime.observe(self.owner, self.session_id, include_image=True)

    def visual_handle(self, observation, region=REGION, label="播放",
                      crop_digest=None, **overrides):
        import base64
        raw = base64.b64decode(observation["image"]["base64"])
        digest = crop_digest or region_digest(raw, region, (DISPLAY[0], DISPLAY[1]))
        visual = {"region": list(region), "crop_digest": digest, "source": "ocr",
                  "label": label, "display_width": observation["display"]["width"],
                  "display_height": observation["display"]["height"],
                  "rotation": observation["display"]["rotation"]}
        visual.update(overrides)
        return Target(target_ref="gt_" + digest[:32], local_fingerprint=digest,
                      observation_id=observation["observation_id"], visual=visual)

    def act(self, observation_id, target, request_id="visual-1", kind="tap", text=None):
        action = {"kind": kind, "target": target.model_dump(exclude_none=True)}
        if kind in ("input_text", "replace_text"):
            action["text"] = "鸿蒙" if text is None else text
        return self.runtime.act(self.owner, {
            "session_id": self.session_id, "request_id": request_id,
            "observation_id": observation_id, "action": action})

    # -- the happy path -----------------------------------------------------
    def test_a_matching_region_is_dispatched_at_its_centre(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed)
        result = self.act(observed["observation_id"], handle)
        self.assertEqual(result["execution_status"], "executed")
        writes = self.devices[-1].writes
        self.assertEqual(writes[-1]["kind"], "tap")
        self.assertEqual(writes[-1]["hit_bounds"], list(REGION))
        self.assertEqual(writes[-1]["centre"],
                         ((REGION[0] + REGION[2]) // 2, (REGION[1] + REGION[3]) // 2))

    # -- refusal paths ------------------------------------------------------
    def test_changed_pixels_refuse_the_region(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed)
        self.devices[-1].colour = (200, 30, 30)
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], handle)
        self.assertEqual(error.exception.code, "target_not_revalidated")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_rotation_change_refuses_the_region(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed)
        self.devices[-1].rotation = 1
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], handle)
        self.assertEqual(error.exception.code, "target_geometry_changed")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_resized_display_refuses_the_region(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed)
        device = self.devices[-1]
        device.display = lambda: (720, 1280, 0)
        device.screenshot = lambda: Image.new("RGB", (720, 1280), device.colour)
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], handle)
        self.assertEqual(error.exception.code, "target_geometry_changed")
        self.assertEqual(device.writes, [])

    def test_a_region_with_no_area_inside_the_display_is_refused(self):
        observed = self.observe_with_image()
        # The caller cannot even compute a digest for an off-screen region, so
        # the handle carries a digest the runtime must fail to re-derive.
        handle = self.visual_handle(observed, region=(1080, 100, 1200, 200),
                                    crop_digest="a" * 64)
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], handle)
        self.assertEqual(error.exception.code, "target_out_of_bounds")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_forged_digest_cannot_be_dispatched(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed)
        forged = handle.model_copy(deep=True)
        forged.visual.crop_digest = "f" * 64
        forged.local_fingerprint = "f" * 64
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], forged, request_id="forged")
        self.assertEqual(error.exception.code, "target_not_revalidated")
        self.assertEqual(self.devices[-1].writes, [])

    def test_visual_regions_cannot_receive_input(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed)
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], handle, request_id="visual-input",
                     kind="input_text")
        self.assertEqual(error.exception.code, "unsupported_capability")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_sensitive_visual_label_is_blocked_by_policy(self):
        observed = self.observe_with_image()
        handle = self.visual_handle(observed, label="删除")
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], handle, request_id="visual-danger")
        self.assertEqual(error.exception.code, "approval_required")
        self.assertEqual(self.devices[-1].writes, [])

    def test_an_unknown_handle_is_still_refused(self):
        observed = self.observe_with_image()
        with self.assertRaises(RuntimeFault) as error:
            self.act("obs_never_issued", self.visual_handle(observed))
        self.assertEqual(error.exception.code, "stale_observation")
        self.assertEqual(self.devices[-1].writes, [])


class RawCoordinateTests(unittest.TestCase):
    """The v1 surface still has no way to express an arbitrary point."""

    def test_a_coordinate_string_is_only_ever_a_text_selector(self):
        tree = {"attributes": {"bundleName": WEIBO, "type": "Root", "bounds": "[0,0][1080,2340]"},
                "children": []}
        obs = snapshot(tree, DISPLAY, {"status": "ok", "bundle": WEIBO})
        obs["observation_id"] = "obs_coords"
        obs["actionable"] = True
        result = ground(obs, GroundingIntent(action_kind="tap", text="540,1200"))
        self.assertEqual(result.targets, [])

    def test_a_visual_region_requires_the_visual_block(self):
        with self.assertRaises(Exception):
            Target(target_ref="gt_" + "a" * 32, local_fingerprint="a" * 64,
                   text="540,1200")


if __name__ == "__main__":
    unittest.main()
