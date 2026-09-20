"""v3.2 Phase 3: `Session.observations` is an authority cache, not a screen cache.

A handle is created only by `observe()`. It carries action authority, the epoch
it was captured under, a freshness deadline and the page identity the guard will
compare at dispatch time. Every invalidation path below must remove that
authority; none of them may silently hand an old `observation_id` to a new action.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime

DEVICE = "fake-device"


class ObservationAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Device(FakeWeiboDevice):
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

    # -- helpers ------------------------------------------------------------
    def observe(self, **kwargs):
        return self.runtime.observe(self.owner, self.session_id, **kwargs)

    def act(self, observation_id, request_id="r1", kind="tap"):
        return self.runtime.act(self.owner, {
            "session_id": self.session_id, "request_id": request_id,
            "observation_id": observation_id,
            "action": {"kind": kind, "target": {"action_id": "n0"}}})

    def handles(self):
        return self.runtime.sessions[self.session_id].observations

    # -- authority ----------------------------------------------------------
    def test_the_cached_handle_is_the_same_object_the_guard_uses(self):
        observed = self.observe()
        cached = self.runtime.cached_observation(self.owner, self.session_id,
                                                 observed["observation_id"])
        self.assertIsNotNone(cached)
        self.assertIs(cached, observed)
        self.assertEqual(cached["observation_id"], observed["observation_id"])
        result = self.act(observed["observation_id"])
        self.assertEqual(result["execution_status"], "executed")

    def test_an_unknown_handle_is_a_cache_miss(self):
        self.assertIsNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                          "obs_never_issued"))
        with self.assertRaises(RuntimeFault) as error:
            self.act("obs_never_issued")
        self.assertEqual(error.exception.code, "stale_observation")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_handle_cannot_be_read_through_another_session(self):
        observed = self.observe()
        with self.assertRaises(RuntimeFault):
            self.runtime.cached_observation(self.owner, "not-a-session",
                                            observed["observation_id"])

    # -- invalidation -------------------------------------------------------
    def test_pause_and_resume_invalidate_every_handle(self):
        before = self.observe()["observation_id"]
        paused = self.runtime.session(self.owner, "pause", self.session_id)
        self.assertTrue(paused["controller_epoch"] > 0)
        self.assertEqual(self.handles(), {})
        # A paused session exposes no authority at all, not even a lookup.
        with self.assertRaises(RuntimeFault):
            self.runtime.cached_observation(self.owner, self.session_id, before)
        self.runtime.session(self.owner, "resume", self.session_id)
        self.assertIsNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                          before))
        with self.assertRaises(RuntimeFault) as error:
            self.act(before)
        self.assertEqual(error.exception.code, "stale_observation")

    def test_recover_bumps_the_epoch_and_invalidates_handles(self):
        before = self.observe()["observation_id"]
        epoch_before = self.runtime.session(self.owner, "status",
                                            session_id=self.session_id)["controller_epoch"]
        recovered = self.runtime.session(self.owner, "recover", self.session_id)
        self.assertGreater(recovered["controller_epoch"], epoch_before)
        self.assertIsNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                          before))
        self.assertIsNotNone(recovered["observation"]["observation_id"])

    def test_ttl_expiry_removes_authority(self):
        observed = self.observe()
        observation_id = observed["observation_id"]
        # Rewrite the stored capture time instead of sleeping for the 15s TTL.
        self.handles()[observation_id] = (time.monotonic() - 16, observed)
        self.assertIsNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                          observation_id))
        with self.assertRaises(RuntimeFault) as error:
            self.act(observation_id)
        self.assertEqual(error.exception.code, "stale_observation")

    def test_the_cache_keeps_only_the_newest_eight_handles(self):
        issued = [self.observe()["observation_id"] for _ in range(11)]
        self.assertLessEqual(len(self.handles()), 8)
        self.assertIsNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                          issued[0]))
        self.assertIsNotNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                             issued[-1]))

    def test_an_unverified_capture_never_becomes_an_authority(self):
        # The device is created lazily on first use, so force it into existence.
        self.observe()
        device = self.devices[-1]
        calls = {"n": 0}

        def unstable():
            calls["n"] += 1
            bundle = WEIBO if calls["n"] % 2 else "com.example.other"
            return {"status": "ok", "bundle": bundle}

        device.foreground = unstable
        observed = self.observe()
        self.assertFalse(observed["actionable"])
        self.assertIsNone(self.runtime.cached_observation(self.owner, self.session_id,
                                                          observed["observation_id"]))
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"])
        self.assertEqual(error.exception.code, "stale_observation")
        self.assertEqual(device.writes, [])

    def test_a_locked_screen_clears_existing_handles(self):
        observed = self.observe()
        device = self.devices[-1]
        device.screen_state = lambda: {"screen_on": True, "screen_locked": True}
        with self.assertRaises(RuntimeFault) as error:
            self.observe()
        self.assertEqual(error.exception.code, "screen_locked")
        self.assertEqual(self.handles(), {})
        with self.assertRaises(RuntimeFault):
            self.act(observed["observation_id"])

    def test_a_page_change_within_the_ttl_still_rejects_the_old_handle(self):
        observed = self.observe()
        # The tool navigates on its own: the observed page no longer exists.
        self.devices[-1].stage = "editor"
        with self.assertRaises(RuntimeFault) as error:
            self.act(observed["observation_id"], request_id="after-change")
        self.assertIn(error.exception.code, ("stale_observation", "target_not_found",
                                             "target_ambiguous"))
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_fresh_observation_after_invalidation_restores_authority(self):
        self.runtime.session(self.owner, "pause", self.session_id)
        self.runtime.session(self.owner, "resume", self.session_id)
        fresh = self.observe()
        self.assertTrue(fresh["actionable"])
        self.assertEqual(self.act(fresh["observation_id"])["execution_status"], "executed")


class ObservationModeTests(unittest.TestCase):
    """v3.2 adds no observation mode and no visual shortcut."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(pathlib.Path(self.tmp.name),
                               factory=lambda serial: FakeWeiboDevice(serial),
                               discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session_id = self.runtime.session(self.owner, "open",
                                               device_id=DEVICE)["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_only_fast_full_and_temporal_exist(self):
        for mode in ("LIGHT", "VISUAL", "TURBO"):
            with self.assertRaises(RuntimeFault) as error:
                self.runtime.observe(self.owner, self.session_id, mode=mode)
            self.assertEqual(error.exception.code, "unsupported_capability")
        self.assertIn("mode", self.runtime.observe(self.owner, self.session_id))

    def test_temporal_frames_are_not_action_handles(self):
        result = self.runtime.observe(self.owner, self.session_id, mode="TEMPORAL")
        for frame in result["frames"]:
            self.assertFalse(frame["actionable"])
            self.assertIsNone(self.runtime.cached_observation(
                self.owner, self.session_id, frame["observation_id"]))

    def test_session_capabilities_do_not_claim_ocr_or_webview(self):
        capabilities = self.runtime.session(self.owner, "status",
                                            session_id=self.session_id)["capabilities"]
        self.assertFalse(capabilities["ocr"])
        self.assertFalse(capabilities["webview"])
        self.assertFalse(capabilities["pro"])
        self.assertTrue(capabilities["grounded_target"])


if __name__ == "__main__":
    unittest.main()
