"""v3.2 Phase 6/7: OCR adapter and optional VLM grounding provider.

Both providers are offline-testable adapters over the existing grounding seams.
They may propose regions and nothing else: every answer is normalised, and every
failure mode degrades to "no visual evidence" instead of reaching the device.
"""
from __future__ import annotations

import base64
import io
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from PIL import Image
from harmony_agent.grounding import (GroundingIntent, GroundingUnavailable, ground)
from harmony_agent.ocr import OcrAdapter, build_ocr_engine
from harmony_agent.visual_regions import (clip_and_validate, display_size,
                                          normalize_bounds, normalize_confidence,
                                          normalize_regions)
from harmony_agent.vlm import VlmGroundingProvider, build_vlm_provider
from harmony_runtime.observation import snapshot

WEIBO = "com.sina.weibo.stage"
DISPLAY = {"width": 1080, "height": 2340, "rotation": 0}


def png_bytes(colour=(40, 40, 40), size=(1080, 2340)):
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def observation(*, tree=None, display=DISPLAY, image=True, observation_id="obs_v"):
    tree = tree or {"attributes": {"bundleName": WEIBO, "type": "Root",
                                   "bounds": "[0,0][1080,2340]"}, "children": []}
    obs = snapshot(tree, (display["width"], display["height"], display["rotation"]),
                   {"status": "ok", "bundle": WEIBO})
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = 0
    obs["actionable"] = True
    obs["mode"] = "FULL"
    if image:
        obs["image"] = {"mime_type": "image/png",
                        "base64": base64.b64encode(png_bytes()).decode("ascii")}
    return obs


class Backend:
    """Scripted OCR backend: `recognize(image_bytes)`."""

    available = True

    def __init__(self, entries=None, *, error=None):
        self.entries = entries if entries is not None else []
        self.error = error
        self.calls = []

    def recognize(self, image_bytes):
        self.calls.append(len(image_bytes))
        if self.error:
            raise self.error
        return self.entries


class VlmBackend(Backend):
    """Scripted VLM backend: `locate(request, image_bytes)`."""

    def __init__(self, entries=None, *, error=None):
        super().__init__(entries, error=error)
        self.requests = []

    def locate(self, request, image_bytes):
        self.requests.append(request)
        return self.recognize(image_bytes)


class RegionNormalisationTests(unittest.TestCase):
    def test_bounds_accept_lists_tuples_and_dicts(self):
        self.assertEqual(normalize_bounds([1, 2, 3, 4]), (1, 2, 3, 4))
        self.assertEqual(normalize_bounds((1, 2, 3, 4)), (1, 2, 3, 4))
        self.assertEqual(normalize_bounds({"left": 1, "top": 2, "right": 3, "bottom": 4}),
                         (1, 2, 3, 4))

    def test_malformed_bounds_are_rejected(self):
        for value in ([1, 2, 3], "1,2,3,4", None, [1, 2, 3, 4, 5],
                      [True, 2, 3, 4], [4, 4, 4, 4], ["a", 2, 3, 4]):
            self.assertIsNone(normalize_bounds(value), value)

    def test_rotation_swaps_the_usable_surface(self):
        self.assertEqual(display_size(DISPLAY, 0), (1080, 2340))
        self.assertEqual(display_size(DISPLAY, 1), (2340, 1080))
        self.assertIsNone(display_size(None))
        self.assertIsNone(display_size({"width": 0, "height": 100}))

    def test_a_region_with_no_on_screen_area_is_dropped(self):
        self.assertIsNone(clip_and_validate((1200, 100, 1400, 200), (1080, 2340)))
        self.assertEqual(clip_and_validate((-50, 2300, 100, 2400), (1080, 2340)),
                         (0, 2300, 100, 2340))

    def test_confidence_is_clamped_and_non_finite_is_rejected(self):
        self.assertEqual(normalize_confidence(1.7), 1.0)
        self.assertEqual(normalize_confidence(-0.4), 0.0)
        self.assertIsNone(normalize_confidence(float("nan")))
        self.assertIsNone(normalize_confidence(float("inf")))
        self.assertIsNone(normalize_confidence("0.9"))
        self.assertIsNone(normalize_confidence(True))

    def test_regions_are_clipped_scored_and_deduplicated(self):
        regions = normalize_regions(
            [{"text": "搜索", "bounds": (-40, 100, 200, 200), "confidence": 0.9},
             {"text": "搜索", "bounds": (0, 100, 200, 200), "confidence": 0.9},
             {"text": "低分", "bounds": (10, 300, 200, 400), "confidence": 0.1},
             {"text": "越界", "bounds": (2000, 300, 2100, 400), "confidence": 0.9},
             {"bounds": (10, 500, 200, 600), "confidence": 0.9},
             "not-a-region"],
            display=DISPLAY)
        # A region without text is still a region: the image/VLM layer labels it
        # from the requested description, so the normaliser must not invent one.
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["text"], "搜索")
        self.assertEqual(regions[0]["bounds"], (0, 100, 200, 200))
        self.assertEqual(regions[0]["confidence"], 0.9)
        self.assertEqual(regions[1]["text"], "")
        self.assertEqual(regions[1]["bounds"], (10, 500, 200, 600))


class OcrAdapterTests(unittest.TestCase):
    def test_an_unconfigured_engine_is_explicitly_unavailable(self):
        self.assertIsNone(build_ocr_engine(None))
        adapter = OcrAdapter(None)
        self.assertFalse(adapter.available)
        self.assertEqual(adapter.reason, "no_ocr_backend_configured")
        self.assertEqual(adapter.recognize(b"x"), [])

    def test_chinese_and_english_text_is_normalised(self):
        adapter = OcrAdapter(Backend([
            {"text": "搜索", "bounds": (900, 100, 1060, 200), "confidence": 0.92},
            {"text": "Search", "bounds": (40, 300, 300, 360), "confidence": 0.88},
        ]), display=DISPLAY)
        regions = adapter.recognize(png_bytes())
        self.assertEqual([region["text"] for region in regions], ["搜索", "Search"])
        self.assertTrue(all(region["method"] == "region" for region in regions))

    def test_duplicate_text_is_deduplicated_by_region(self):
        adapter = OcrAdapter(Backend([
            {"text": "搜索", "bounds": (900, 100, 1060, 200), "confidence": 0.9},
            {"text": "搜索", "bounds": (900, 100, 1060, 200), "confidence": 0.7},
            {"text": "搜索", "bounds": (40, 300, 200, 360), "confidence": 0.7},
        ]), display=DISPLAY)
        regions = adapter.recognize(png_bytes())
        self.assertEqual(len(regions), 2)

    def test_low_confidence_results_are_dropped(self):
        adapter = OcrAdapter(Backend([
            {"text": "模糊", "bounds": (40, 300, 200, 360), "confidence": 0.05},
        ]), display=DISPLAY, min_confidence=0.4)
        self.assertEqual(adapter.recognize(png_bytes()), [])

    def test_out_of_bounds_results_are_dropped(self):
        adapter = OcrAdapter(Backend([
            {"text": "屏幕外", "bounds": (1200, 100, 1400, 200), "confidence": 0.9},
        ]), display=DISPLAY)
        self.assertEqual(adapter.recognize(png_bytes()), [])

    def test_a_rotated_surface_clips_against_the_rotated_size(self):
        adapter = OcrAdapter(Backend([
            {"text": "横向", "bounds": (1200, 100, 1400, 200), "confidence": 0.9},
        ]), display=DISPLAY, rotation=1)
        regions = adapter.recognize(png_bytes())
        self.assertEqual(regions[0]["bounds"], (1200, 100, 1400, 200))
        self.assertEqual(regions[0]["rotation"], 1)

    def test_an_empty_answer_is_no_evidence(self):
        adapter = OcrAdapter(Backend([]), display=DISPLAY)
        self.assertEqual(adapter.recognize(png_bytes()), [])

    def test_an_engine_exception_becomes_no_evidence(self):
        adapter = OcrAdapter(Backend(error=RuntimeError("ocr process died")),
                             display=DISPLAY)
        self.assertEqual(adapter.recognize(png_bytes()), [])
        self.assertEqual(adapter.reason, "ocr_failed:RuntimeError")

    def test_an_unavailable_engine_reports_its_reason(self):
        class Offline:
            available = False
            reason = "engine_not_installed"

            def recognize(self, image_bytes):
                raise AssertionError("must not be called")

        adapter = OcrAdapter(Offline())
        self.assertFalse(adapter.available)
        self.assertEqual(adapter.reason, "engine_not_installed")
        self.assertEqual(adapter.recognize(png_bytes()), [])

    def test_the_region_budget_is_bounded(self):
        entries = [{"text": f"t{index}", "bounds": (10, 10 + index * 20, 200, 25 + index * 20),
                    "confidence": 0.9} for index in range(10)]
        adapter = OcrAdapter(Backend(entries), display=DISPLAY, max_regions=3)
        regions = adapter.recognize(png_bytes())
        self.assertEqual(len(regions), 3)
        self.assertEqual(adapter.reason, "ocr_region_budget_exceeded")


class OcrGroundingIntegrationTests(unittest.TestCase):
    def test_ocr_reaches_a_grounded_target_with_geometry(self):
        obs = observation()
        adapter = OcrAdapter(Backend([
            {"text": "播放", "bounds": (900, 100, 1060, 200), "confidence": 0.9},
        ]), display=obs["display"])
        result = ground(obs, GroundingIntent(action_kind="tap", text="播放"), ocr=adapter)
        self.assertEqual(result.layers_used, ["ocr_region"])
        target = result.targets[0]
        self.assertEqual(target.source, "ocr")
        self.assertEqual(target.geometry["crop"], [900, 100, 1060, 200])
        self.assertTrue(target.geometry["crop_digest"])
        self.assertIsNotNone(target.visual_ref())

    def test_an_engine_crash_does_not_reach_the_grounding_pipeline(self):
        obs = observation()
        adapter = OcrAdapter(Backend(error=RuntimeError("boom")), display=obs["display"])
        result = ground(obs, GroundingIntent(action_kind="tap", text="播放"), ocr=adapter)
        self.assertEqual(result.targets, [])
        self.assertIn("all_layers_exhausted", result.notes)

    def test_an_unavailable_engine_is_reported_in_the_notes(self):
        obs = observation()
        result = ground(obs, GroundingIntent(action_kind="tap", text="播放"))
        self.assertTrue(any(note.startswith("ocr_layer_skipped")
                            for note in result.notes))

    def test_a_region_that_cannot_be_revalidated_is_rejected_not_guessed(self):
        obs = observation(image=False)
        adapter = OcrAdapter(Backend([
            {"text": "播放", "bounds": (900, 100, 1060, 200), "confidence": 0.9},
        ]), display=obs["display"])
        result = ground(obs, GroundingIntent(action_kind="tap", text="播放"), ocr=adapter)
        # Without image evidence the OCR layer never even runs.
        self.assertEqual(result.layers_used, [])
        self.assertIn("ocr_layer_skipped:no_image_evidence", result.notes)


class VlmProviderTests(unittest.TestCase):
    def test_an_unconfigured_vlm_is_unavailable(self):
        self.assertIsNone(build_vlm_provider(None))
        provider = VlmGroundingProvider(None)
        self.assertFalse(provider.available)
        self.assertEqual(provider.reason, "no_vlm_backend_configured")
        self.assertEqual(provider.locate(png_bytes(), "播放"), [])

    def test_the_request_carries_the_documented_inputs(self):
        provider = VlmGroundingProvider(VlmBackend([]), display=DISPLAY)
        provider = provider.with_context(goal="播放视频", subgoal="点击播放",
                                        tree_summary=["首页"], existing_candidates=[{"id": "c1"}])
        request = provider.build_request("播放")
        self.assertEqual(request["goal"], "播放视频")
        self.assertEqual(request["subgoal"], "点击播放")
        self.assertEqual(request["display"]["width"], 1080)
        self.assertEqual(request["tree_summary"], ["首页"])
        self.assertEqual(request["existing_candidates"], [{"id": "c1"}])
        self.assertEqual(request["answer_contract"], "regions_only")
        self.assertNotIn("action", request)

    def test_regions_are_validated_and_scored(self):
        provider = VlmGroundingProvider(VlmBackend([
            {"bounds": {"left": 900, "top": 100, "right": 1060, "bottom": 200},
             "score": 0.8, "method": "vlm_icon"},
        ]), display=DISPLAY)
        regions = provider.locate(png_bytes(), "播放按钮")
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["bounds"], (900, 100, 1060, 200))
        self.assertEqual(regions[0]["confidence"], 0.8)
        self.assertEqual(regions[0]["method"], "vlm_icon")

    def test_a_timeout_is_reported_as_a_timeout(self):
        provider = VlmGroundingProvider(VlmBackend(error=TimeoutError("deadline")),
                                        display=DISPLAY)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(png_bytes(), "播放")
        self.assertEqual(error.exception.code, "vlm_timeout")

    def test_an_engine_crash_is_reported_as_a_failure(self):
        provider = VlmGroundingProvider(VlmBackend(error=RuntimeError("model died")),
                                        display=DISPLAY)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(png_bytes(), "播放")
        self.assertEqual(error.exception.code, "vlm_failed")

    def test_an_unavailable_backend_is_not_called(self):
        class Offline:
            available = False
            reason = "vlm_server_not_running"

            def locate(self, request, image_bytes):
                raise AssertionError("must not be called")

        provider = VlmGroundingProvider(Offline(), display=DISPLAY)
        self.assertFalse(provider.available)
        self.assertEqual(provider.reason, "vlm_server_not_running")
        self.assertEqual(provider.locate(png_bytes(), "播放"), [])

    def test_no_match_returns_no_region(self):
        provider = VlmGroundingProvider(VlmBackend([]), display=DISPLAY)
        self.assertEqual(provider.locate(png_bytes(), "播放"), [])

    def test_invalid_bounds_are_rejected(self):
        provider = VlmGroundingProvider(VlmBackend([
            {"bounds": [1, 2, 3], "score": 0.9},
            {"bounds": "900,100,1060,200", "score": 0.9},
        ]), display=DISPLAY)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(png_bytes(), "播放")
        self.assertEqual(error.exception.code, "vlm_low_confidence")

    def test_low_confidence_matches_are_not_offered(self):
        provider = VlmGroundingProvider(VlmBackend([
            {"bounds": (900, 100, 1060, 200), "score": 0.05},
        ]), display=DISPLAY, min_score=0.5)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(png_bytes(), "播放")
        self.assertEqual(error.exception.code, "vlm_low_confidence")

    def test_a_non_list_answer_is_not_a_region_list(self):
        provider = VlmGroundingProvider(VlmBackend({"action": "tap", "x": 100, "y": 200}),
                                        display=DISPLAY)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(png_bytes(), "播放")
        self.assertEqual(error.exception.code, "vlm_invalid_response")

    def test_a_coordinate_answer_can_never_become_a_region(self):
        """A backend answering with raw x/y has no bounds, so nothing is offered."""
        provider = VlmGroundingProvider(VlmBackend([
            {"action": "tap", "x": 980, "y": 150},
        ]), display=DISPLAY)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(png_bytes(), "播放")
        self.assertEqual(error.exception.code, "vlm_low_confidence")

    def test_the_region_budget_is_bounded(self):
        entries = [{"bounds": (10, 10 + index * 20, 200, 25 + index * 20), "score": 0.9}
                   for index in range(20)]
        provider = VlmGroundingProvider(VlmBackend(entries), display=DISPLAY, max_regions=4)
        self.assertEqual(len(provider.locate(png_bytes(), "播放")), 4)

    def test_an_observation_binds_the_display_geometry(self):
        provider = VlmGroundingProvider(VlmBackend([
            {"bounds": (900, 100, 1060, 200), "score": 0.9},
        ]))
        bound = provider.with_observation(observation())
        self.assertEqual(bound.display["width"], 1080)
        self.assertEqual(len(bound.locate(png_bytes(), "播放")), 1)


class VlmGroundingIntegrationTests(unittest.TestCase):
    def test_vlm_reaches_a_grounded_target_through_the_image_layer(self):
        obs = observation()
        provider = VlmGroundingProvider(VlmBackend([
            {"bounds": (900, 100, 1060, 200), "score": 0.9, "method": "vlm_icon"},
        ]), display=obs["display"])
        result = ground(obs, GroundingIntent(action_kind="tap", description="播放按钮"),
                        matcher=provider)
        self.assertEqual(result.layers_used, ["image_region"])
        target = result.targets[0]
        self.assertEqual(target.source, "image")
        self.assertEqual(target.geometry["crop"], [900, 100, 1060, 200])
        self.assertEqual(target.visual_ref()["label"], "播放按钮")
        self.assertEqual(target.visual_ref()["source"], "image")

    def test_a_vlm_failure_is_a_note_not_an_exception(self):
        obs = observation()
        provider = VlmGroundingProvider(VlmBackend(error=TimeoutError("slow")),
                                        display=obs["display"])
        result = ground(obs, GroundingIntent(action_kind="tap", description="播放按钮"),
                        matcher=provider)
        self.assertEqual(result.targets, [])
        self.assertIn("image_layer_failed:vlm_timeout", result.notes)

    def test_the_tree_layer_still_wins(self):
        tree = {"attributes": {"bundleName": WEIBO, "type": "Root",
                               "bounds": "[0,0][1080,2340]"},
                "children": [{"attributes": {"bundleName": WEIBO, "type": "Button",
                                             "description": "播放按钮",
                                             "clickable": "true", "id": "play",
                                             "bounds": "[900,100][1060,200]"},
                              "children": []}]}
        obs = observation(tree=tree)
        provider = VlmGroundingProvider(VlmBackend([
            {"bounds": (10, 10, 100, 100), "score": 0.9},
        ]), display=obs["display"])
        result = ground(obs, GroundingIntent(action_kind="tap", description="播放按钮"),
                        matcher=provider)
        self.assertEqual(result.layers_used, ["description_structure"])
        self.assertEqual(result.targets[0].source, "ui_tree")


if __name__ == "__main__":
    unittest.main()
