"""v3.2 Pre-Device Release Gate: adversarial cases for the closed findings.

This suite holds the cases that directly guard the Gate A-G fixes. The broader
Journal/Lifecycle matrix lives in `tests/test_offline_fault_matrix.py` and the
provider failure taxonomy in `tests/test_agent_provider_factory.py`; duplicating
them here would blur which fix each test protects.
"""
from __future__ import annotations

import base64
import pathlib
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from PIL import Image
from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.candidates import CandidateError, CandidateRegistry
from harmony_agent.decision.providers.base import ProviderCapabilities, ProviderUnavailable
from harmony_agent.grounding import GroundingIntent, GroundingUnavailable, ground
from harmony_agent.host import AgentHost
from harmony_agent.ocr import OcrAdapter
from harmony_agent.vlm import VlmGroundingProvider
from harmony_runtime.contracts import RuntimeFault, Target
from harmony_runtime.observation import snapshot
from harmony_runtime.runtime import Runtime
from harmony_runtime.visual import region_digest

DEVICE = "fake-device"
DISPLAY = (1080, 2340, 0)
REGION = (900, 100, 1060, 200)


def duplicate_tree():
    """Two clickable nodes with identical text: an ambiguous grounding result."""
    return {"attributes": {"bundleName": WEIBO, "type": "Root",
                           "bounds": "[0,0][1080,2340]"},
            "children": [
                {"attributes": {"bundleName": WEIBO, "type": "Button", "text": "重复",
                                "clickable": "true", "bounds": "[40,400][300,460]"},
                 "children": []},
                {"attributes": {"bundleName": WEIBO, "type": "Button", "text": "重复",
                                "clickable": "true", "bounds": "[40,500][300,560]"},
                 "children": []}]}


class DuplicateDevice(FakeWeiboDevice):
    def tree(self):
        return duplicate_tree()


class CountingTimeoutProvider:
    """A provider that is invoked and always times out."""

    provider = "decider"
    model_revision = "gate-supplier"

    def __init__(self):
        self.calls = 0

    def capabilities(self):
        return ProviderCapabilities(provider="decider", supports_choice=True,
                                    supports_noul=True, max_candidates=16,
                                    max_questions=4, requires_calibration=True)

    async def evaluate(self, context, questions, *, remaining_model_calls=None):
        self.calls += 1
        raise ProviderUnavailable("model_timeout", "simulated deadline")


class AuthorityGateTests(unittest.TestCase):
    """Gate A: the registry, not the candidate set, authorises a dispatch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        self.hosts = []
        outer = self

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_off", repo_root=self.root)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def task(self, request_id):
        return {"schema_version": "2.0", "request_id": request_id,
                "mode": "delegated", "goal": "打开微博搜索页",
                "scope": {"device_ref": "d", "allowed_apps": [WEIBO],
                          "allowed_actions": ["tap"], "cloud_data_policy": "disabled"},
                "success_criteria": [{"id": "editor", "type": "element_present",
                                      "target_key": "搜索"}],
                "arguments": {"steps": "tap:搜索"},
                "budget": {"max_dispatches": 8, "max_seconds": 60, "max_model_calls": 8},
                "model_profile": "local_off"}

    def run_task(self, request_id, timeout=30.0):
        created = self.host.run_task(self.owner, session_id=self.session,
                                     task=self.task(request_id))
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=created["task_id"])
            if status["terminal"] or status["status"] in ("RECONCILIATION_REQUIRED",
                                                          "PAUSED", "WAITING_USER"):
                return created["task_id"], status
            time.sleep(0.02)
        self.fail("task did not finish")

    def rejection_codes(self, task_id):
        return [item["payload"]["code"] for item in
                self.host.task_events(self.owner, task_id=task_id)["items"]
                if item["type"] == "candidate_rejected"]

    def test_an_expired_candidate_is_never_dispatched(self):
        # Every candidate this registry issues is already expired, so the only
        # safe outcome is a refusal at the dispatch gateway.
        self.host.registry = CandidateRegistry(ttl_seconds=-1)
        task_id, status = self.run_task("gate-expired")
        self.assertNotEqual(status["status"], "SUCCEEDED")
        self.assertEqual(self.devices[-1].writes, [])
        self.assertEqual(set(self.rejection_codes(task_id)), {"candidate_expired"})

    def test_a_candidate_the_registry_no_longer_issues_is_never_dispatched(self):
        class Forgetful(CandidateRegistry):
            def build(self, **kwargs):
                candidate_set = super().build(**kwargs)
                self._issued.clear()   # e.g. pruned or expired after the decision
                return candidate_set

        self.host.registry = Forgetful()
        task_id, status = self.run_task("gate-forgotten")
        self.assertNotEqual(status["status"], "SUCCEEDED")
        self.assertEqual(self.devices[-1].writes, [])
        self.assertEqual(set(self.rejection_codes(task_id)), {"unknown_candidate"})

    def test_a_stale_observation_verdict_is_never_dispatched(self):
        class Stale(CandidateRegistry):
            def resolve(self, candidate_id, *, observation_id, controller_epoch, now=None):
                raise CandidateError("stale_observation", "simulated")

        self.host.registry = Stale()
        task_id, status = self.run_task("gate-stale")
        self.assertNotEqual(status["status"], "SUCCEEDED")
        self.assertEqual(self.devices[-1].writes, [])
        self.assertEqual(set(self.rejection_codes(task_id)), {"stale_observation"})

    def test_an_old_epoch_verdict_is_never_dispatched(self):
        class OldEpoch(CandidateRegistry):
            def resolve(self, candidate_id, *, observation_id, controller_epoch, now=None):
                raise CandidateError("epoch_mismatch", "simulated")

        self.host.registry = OldEpoch()
        task_id, status = self.run_task("gate-epoch")
        self.assertNotEqual(status["status"], "SUCCEEDED")
        self.assertEqual(self.devices[-1].writes, [])
        self.assertEqual(set(self.rejection_codes(task_id)), {"epoch_mismatch"})

    def test_a_foreign_candidate_never_reaches_the_dispatch_payload(self):
        """The runtime target carries no candidate id that a caller could forge."""
        from harmony_agent.planner import Subgoal
        from harmony_agent.supervisor import _action_payload

        observation = snapshot(FakeWeiboDevice(DEVICE).tree(), DISPLAY,
                               {"status": "ok", "bundle": WEIBO})
        observation["observation_id"] = "obs_payload"
        from harmony_agent.contracts import Predicate
        registry = CandidateRegistry()
        candidate_set = registry.build(
            task_id="t", subgoal_id="s", scope_id="sc", observation=observation,
            controller_epoch=0,
            intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
            action_kind="tap", arguments={},
            expected_predicates=[Predicate(id="p", type="foreground_is", value=WEIBO)])
        payload = _action_payload(Subgoal(subgoal_id="s", description="d",
                                          action_kind="tap"),
                                  candidate_set.candidates[0])
        self.assertNotIn("candidate_id", payload["target"])
        self.assertEqual(set(payload["target"]),
                         {"target_ref", "observation_id", "local_fingerprint"})

    def test_a_candidate_not_issued_by_a_registry_cannot_be_resolved(self):
        with self.assertRaises(CandidateError) as error:
            CandidateRegistry().resolve("cand_not_issued", observation_id="obs",
                                        controller_epoch=0)
        self.assertEqual(error.exception.code, "unknown_candidate")


class ProviderBudgetGateTests(unittest.TestCase):
    """Gate E: a failed provider attempt still consumes the model budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Device(DuplicateDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]
        self.provider = CountingTimeoutProvider()
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_shadow", fast_provider=self.provider,
                              repo_root=self.root)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def payload(self, request_id, *, max_model_calls):
        return {"schema_version": "2.0", "request_id": request_id,
                "mode": "delegated", "goal": "点击重复目标",
                "scope": {"device_ref": "d", "allowed_apps": [WEIBO],
                          "allowed_actions": ["tap"], "cloud_data_policy": "disabled"},
                "success_criteria": [{"id": "never", "type": "text_equals",
                                      "value": "不会出现"}],
                "arguments": {"steps": "tap:重复"},
                "budget": {"max_dispatches": 8, "max_seconds": 60,
                           "max_model_calls": max_model_calls},
                "model_profile": "local_shadow"}

    def run_task(self, request_id, *, max_model_calls, timeout=30.0):
        created = self.host.run_task(self.owner, session_id=self.session,
                                     task=self.payload(request_id,
                                                       max_model_calls=max_model_calls))
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=created["task_id"])
            if status["terminal"] or status["status"] in ("PAUSED",
                                                          "RECONCILIATION_REQUIRED"):
                return created["task_id"], status
            time.sleep(0.02)
        self.fail("task did not finish")

    def test_two_failed_attempts_exhaust_a_two_call_budget(self):
        task_id, _ = self.run_task("gate-budget", max_model_calls=2)
        events = self.host.task_events(self.owner, task_id=task_id)["items"]
        decisions = [item for item in events if item["type"] == "decision"]
        # Three attempts happen; the third must not reach the provider at all.
        self.assertGreaterEqual(len(decisions), 3, decisions)
        self.assertEqual(self.provider.calls, 2)
        exhausted = [item for item in decisions
                     if item["payload"].get("fallback_reason") == "budget_exhausted"]
        self.assertTrue(exhausted, decisions)
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_failed_attempt_is_charged_even_though_rules_decided(self):
        task_id, _ = self.run_task("gate-count", max_model_calls=8)
        self.assertGreaterEqual(self.provider.calls, 1)
        usage = self.host.task_status(self.owner, task_id=task_id)["usage"]
        self.assertGreaterEqual(usage["model_calls_used"], self.provider.calls)
        events = self.host.task_events(self.owner, task_id=task_id)["items"]
        decisions = [item for item in events if item["type"] == "decision"]
        self.assertTrue(all(item["payload"]["provider"] == "rules"
                            for item in decisions), decisions)

    def test_the_budget_does_not_grow_when_the_provider_keeps_failing(self):
        task_id, _ = self.run_task("gate-no-growth", max_model_calls=1)
        usage = self.host.task_status(self.owner, task_id=task_id)["usage"]
        self.assertLessEqual(usage["model_calls_used"], 3)
        self.assertEqual(self.provider.calls, 1)


class VisualProvenanceGateTests(unittest.TestCase):
    """Gate B: geometry and pixels are revalidated on every visual dispatch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

            def screenshot(self):
                # Pixels follow the page, so a page change invalidates a region.
                colour = (40, 40, 40) if self.stage == "home" else (200, 30, 30)
                return Image.new("RGB", (DISPLAY[0], DISPLAY[1]), colour)

        self.runtime = Runtime(self.root, factory=Device, discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def observe(self):
        return self.runtime.observe(self.owner, self.session, include_image=True)

    def handle(self, observation, *, region=REGION, label="播放", digest=None,
               **overrides):
        raw = base64.b64decode(observation["image"]["base64"])
        digest = digest or region_digest(raw, region, (DISPLAY[0], DISPLAY[1]))
        visual = {"region": list(region), "crop_digest": digest, "source": "ocr",
                  "label": label, "display_width": DISPLAY[0],
                  "display_height": DISPLAY[1], "rotation": DISPLAY[2]}
        visual.update(overrides)
        return Target(target_ref="gt_" + digest[:32], local_fingerprint=digest,
                      observation_id=observation["observation_id"], visual=visual)

    def act(self, observation_id, target, request_id="gate-visual"):
        return self.runtime.act(self.owner, {
            "session_id": self.session, "request_id": request_id,
            "observation_id": observation_id,
            "action": {"kind": "tap", "target": target.model_dump(exclude_none=True)}})

    def refusal(self, observation_id, target, request_id, expected):
        with self.assertRaises(RuntimeFault) as error:
            self.act(observation_id, target, request_id)
        self.assertEqual(error.exception.code, expected)
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_direct_caller_may_propose_a_region_and_the_runtime_revalidates_it(self):
        observed = self.observe()
        result = self.act(observed["observation_id"], self.handle(observed))
        self.assertEqual(result["execution_status"], "executed")
        self.assertEqual(len(self.devices[-1].writes), 1)

    def test_a_changed_region_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed, region=(400, 400, 600, 500), digest="f" * 64)
        self.refusal(observed["observation_id"], handle, "gate-region",
                     "target_not_revalidated")

    def test_a_forged_digest_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed, digest="f" * 64)
        self.refusal(observed["observation_id"], handle, "gate-digest",
                     "target_not_revalidated")

    def test_changed_geometry_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed, display_width=720)
        self.refusal(observed["observation_id"], handle, "gate-geometry",
                     "target_geometry_changed")

    def test_changed_rotation_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed, rotation=1)
        self.refusal(observed["observation_id"], handle, "gate-rotation",
                     "target_geometry_changed")

    def test_a_region_copied_across_a_page_change_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed)
        self.devices[-1].stage = "editor"
        self.refusal(observed["observation_id"], handle, "gate-page",
                     "target_not_revalidated")

    def test_a_region_reused_against_another_observation_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed)
        self.devices[-1].stage = "editor"
        other = self.observe()
        # The handle is bound to its own observation, so the refusal is an
        # explicit stale handle rather than a pixel comparison.
        self.refusal(other["observation_id"], handle, "gate-other-observation",
                     "stale_observation")

    def test_an_out_of_bounds_region_is_refused(self):
        observed = self.observe()
        handle = self.handle(observed, region=(1080, 100, 1200, 200), digest="a" * 64)
        self.refusal(observed["observation_id"], handle, "gate-oob",
                     "target_out_of_bounds")


class VisualRiskEvidenceGateTests(unittest.TestCase):
    """Gate C: a proposer's benign label cannot hide sensitive on-screen text."""

    SENSITIVE_TREE = {"attributes": {"bundleName": WEIBO, "type": "Root",
                                     "bounds": "[0,0][1080,2340]"},
                      "children": [
                          {"attributes": {"bundleName": WEIBO, "type": "Button",
                                          "text": "立即支付", "clickable": "true",
                                          "id": "pay_now",
                                          "bounds": "[900,100][1060,200]"},
                           "children": []}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self
        tree = self.SENSITIVE_TREE

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

            def tree(self):
                return tree

            def screenshot(self):
                return Image.new("RGB", (DISPLAY[0], DISPLAY[1]), (40, 40, 40))

        self.runtime = Runtime(self.root, factory=Device, discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def handle(self, observation, label):
        raw = base64.b64decode(observation["image"]["base64"])
        digest = region_digest(raw, REGION, (DISPLAY[0], DISPLAY[1]))
        return Target(target_ref="gt_" + digest[:32], local_fingerprint=digest,
                      observation_id=observation["observation_id"],
                      visual={"region": list(REGION), "crop_digest": digest,
                              "source": "vlm", "label": label,
                              "display_width": DISPLAY[0],
                              "display_height": DISPLAY[1], "rotation": DISPLAY[2]})

    def tap(self, observed, label, request_id):
        return self.runtime.act(self.owner, {
            "session_id": self.session, "request_id": request_id,
            "observation_id": observed["observation_id"],
            "action": {"kind": "tap",
                       "target": self.handle(observed, label).model_dump(
                           exclude_none=True)}})

    def test_a_benign_label_over_sensitive_tree_text_is_blocked(self):
        observed = self.runtime.observe(self.owner, self.session, include_image=True)
        with self.assertRaises(RuntimeFault) as error:
            self.tap(observed, "继续", "gate-risk")
        self.assertEqual(error.exception.code, "approval_required")
        self.assertEqual(self.devices[-1].writes, [])

    def test_a_sensitive_label_is_blocked_even_without_overlap(self):
        observed = self.runtime.observe(self.owner, self.session, include_image=True)
        with self.assertRaises(RuntimeFault) as error:
            self.tap(observed, "删除", "gate-risk-label")
        self.assertEqual(error.exception.code, "approval_required")
        self.assertEqual(self.devices[-1].writes, [])

    def test_the_evidence_is_device_derived_not_proposer_supplied(self):
        observed = self.runtime.observe(self.owner, self.session)
        from harmony_runtime.observation import _visual_overlap_evidence
        evidence = _visual_overlap_evidence(observed, REGION)
        self.assertIn("立即支付", evidence)
        self.assertIn("pay_now", evidence)

    def test_a_region_with_no_overlap_has_no_independent_evidence(self):
        """Documented boundary: without overlapping tree text the proposer's
        label is the only semantic signal, which is recorded as empty evidence."""
        observed = self.runtime.observe(self.owner, self.session)
        from harmony_runtime.observation import _visual_overlap_evidence
        self.assertEqual(_visual_overlap_evidence(observed, (200, 1500, 400, 1600)), "")

    def test_visual_regions_still_cannot_receive_input(self):
        observed = self.runtime.observe(self.owner, self.session, include_image=True)
        handle = self.handle(observed, "搜索框").model_dump(exclude_none=True)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act(self.owner, {
                "session_id": self.session, "request_id": "gate-visual-input",
                "observation_id": observed["observation_id"],
                "action": {"kind": "input_text", "target": handle, "text": "x"}})
        self.assertEqual(error.exception.code, "unsupported_capability")
        self.assertEqual(self.devices[-1].writes, [])


class ProviderDeadlineGateTests(unittest.TestCase):
    """Gate F: a hung visual backend is bounded, quarantined and never grows."""

    class HungBackend:
        available = True
        deadline_bounded = False

        def __init__(self):
            self.release = threading.Event()
            self.calls = 0

        def recognize(self, image_bytes):
            self.calls += 1
            self.release.wait(60)
            return []

        def locate(self, request, image_bytes):
            return self.recognize(image_bytes)

    class BoundedBackend:
        available = True
        deadline_bounded = True

        def __init__(self):
            self.calls = 0

        def recognize(self, image_bytes):
            self.calls += 1
            return [{"text": "播放", "bounds": REGION, "confidence": 0.9}]

    def setUp(self):
        self.hung = self.HungBackend()

    def tearDown(self):
        self.hung.release.set()

    def test_an_ocr_backend_that_never_returns_is_bounded_and_quarantined(self):
        adapter = OcrAdapter(self.hung, timeout_seconds=0.2)
        started = time.monotonic()
        self.assertEqual(adapter.recognize(b"image"), [])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(adapter.quarantined)
        self.assertEqual(adapter.reason, "ocr_quarantined")
        self.assertFalse(adapter.production_ready)
        self.assertEqual(adapter.recognize(b"image"), [])
        self.assertEqual(self.hung.calls, 1)

    def test_a_hung_backend_does_not_grow_the_thread_count(self):
        adapter = OcrAdapter(self.hung, timeout_seconds=0.2)
        before = threading.active_count()
        for _ in range(5):
            self.assertEqual(adapter.recognize(b"image"), [])
        self.assertLessEqual(threading.active_count(), before + 1)
        self.assertEqual(self.hung.calls, 1)

    def test_a_hung_vlm_backend_reports_a_timeout_then_stays_quarantined(self):
        provider = VlmGroundingProvider(
            self.hung, display={"width": 1080, "height": 2340, "rotation": 0},
            timeout_seconds=0.2)
        with self.assertRaises(GroundingUnavailable) as error:
            provider.locate(b"image", "播放")
        self.assertEqual(error.exception.code, "vlm_timeout")
        self.assertTrue(provider.quarantined)
        with self.assertRaises(GroundingUnavailable) as second:
            provider.locate(b"image", "播放")
        self.assertEqual(second.exception.code, "vlm_quarantined")
        self.assertEqual(self.hung.calls, 1)

    def test_a_deadline_bounded_backend_is_called_directly(self):
        backend = self.BoundedBackend()
        adapter = OcrAdapter(backend, display={"width": 1080, "height": 2340,
                                               "rotation": 0})
        before = threading.active_count()
        regions = adapter.recognize(b"image")
        self.assertEqual([region["text"] for region in regions], ["播放"])
        self.assertEqual(backend.calls, 1)
        self.assertTrue(adapter.production_ready)
        self.assertLessEqual(threading.active_count(), before)

    def test_a_hung_provider_does_not_stop_the_tree_path(self):
        adapter = OcrAdapter(self.hung, timeout_seconds=0.2)
        observation = snapshot(duplicate_tree(), DISPLAY,
                               {"status": "ok", "bundle": WEIBO})
        observation["observation_id"] = "obs_hung"
        observation["actionable"] = True
        result = ground(observation, GroundingIntent(action_kind="tap", text="重复"),
                        ocr=adapter)
        self.assertTrue(result.targets)
        self.assertEqual(result.targets[0].source, "ui_tree")


class ProfileAuthorityGateTests(unittest.TestCase):
    """Gate D: the deployment profile is the authority; a task cannot elevate it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        self.hosts = []
        outer = self

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: [DEVICE])
        self.owner = "owner"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]

    def tearDown(self):
        for host in self.hosts:
            host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def payload(self, profile, request_id=None):
        return {"schema_version": "2.0",
                "request_id": request_id or f"profile-{profile}",
                "mode": "delegated", "goal": "打开微博搜索页",
                "scope": {"device_ref": "d", "allowed_apps": [WEIBO],
                          "allowed_actions": ["tap"], "cloud_data_policy": "disabled"},
                "success_criteria": [{"id": "e", "type": "element_present",
                                      "target_key": "搜索"}],
                "arguments": {"steps": "tap:搜索"},
                "budget": {"max_dispatches": 4, "max_seconds": 30,
                           "max_model_calls": 2},
                "model_profile": profile}

    def host(self, profile="local_off", **kwargs):
        host = AgentHost(self.runtime, self.root / "agent", profile=profile,
                         repo_root=self.root, **kwargs)
        self.hosts.append(host)
        return host

    def stricter_payload(self):
        """A task that needs a decision: its criterion does not hold yet."""
        payload = self.payload("local_off", request_id="profile-stricter")
        payload["success_criteria"] = [{"id": "never", "type": "text_equals",
                                        "value": "不会出现"}]
        return payload

    def settle(self, host, task_id, timeout=30.0):
        """Wait for a terminal task state so no runner thread outlives the test."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in ("PAUSED",
                                                          "RECONCILIATION_REQUIRED"):
                return status
            time.sleep(0.02)
        self.fail("task did not finish")

    def test_a_task_cannot_request_more_provider_privilege(self):
        host = self.host("local_off")
        for requested in ("local_shadow", "local_canary"):
            with self.assertRaises(RuntimeFault) as error:
                host.run_task(self.owner, session_id=self.session,
                              task=self.payload(requested))
            self.assertEqual(error.exception.code, "profile_escalation_denied",
                             requested)
        # The refusal happens before any observation, so no device is even built.
        self.assertEqual(self.devices, [])

    def test_a_matching_profile_is_accepted(self):
        host = self.host("local_off")
        created = host.run_task(self.owner, session_id=self.session,
                                task=self.payload("local_off"))
        self.assertIn("task_id", created)
        self.settle(host, created["task_id"])

    def test_an_inherit_marker_is_accepted(self):
        host = self.host("local_off")
        created = host.run_task(self.owner, session_id=self.session,
                                task=self.payload("inherit"))
        self.assertIn("task_id", created)
        self.settle(host, created["task_id"])

    def test_an_unknown_profile_is_rejected(self):
        with self.assertRaises(RuntimeFault) as error:
            self.host("local_off").run_task(self.owner, session_id=self.session,
                                            task=self.payload("active"))
        self.assertEqual(error.exception.code, "profile_unknown")

    def test_a_stricter_profile_is_applied_to_that_task(self):
        host = self.host("local_shadow", fast_provider=CountingTimeoutProvider())
        created = host.run_task(self.owner, session_id=self.session,
                                task=self.stricter_payload())
        self.settle(host, created["task_id"])
        events = host.task_events(self.owner, task_id=created["task_id"])["items"]
        decisions = [item for item in events if item["type"] == "decision"]
        self.assertTrue(decisions)
        # local_off never consults a provider, so the rules decision is the only one.
        self.assertTrue(all(item["payload"]["provider"] == "rules"
                            for item in decisions), decisions)

    def test_diagnostics_report_the_profile_and_calibration_policy(self):
        report = self.host("local_canary",
                           calibration_version="not-a-real-artifact").diagnostics("o")
        self.assertEqual(report["task_profile_policy"],
                         "deployment_profile_is_authority")
        # Gate G: a bare calibration string never counts as calibration.
        self.assertFalse(report["calibration_ready"])
        self.assertEqual(report["calibration_status"], "artifact_missing")


class CalibrationGateTests(unittest.TestCase):
    """Gate G: `local_canary` fails closed without a complete artifact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def artifact(self, payload, name="cal.json"):
        import json
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def complete(self, **overrides):
        payload = {"calibration_id": "cal-1", "provider_revision": "rev-1",
                   "confidence_threshold": 0.8, "certainty_threshold": 0.7,
                   "dataset_sha256": "b" * 64}
        payload.update(overrides)
        return payload

    def test_a_missing_artifact_keeps_the_canary_closed(self):
        from harmony_agent.decision.calibration import load_calibration
        for path in (None, "", self.root / "nope.json"):
            record, status = load_calibration(path)
            self.assertIsNone(record, path)
            self.assertEqual(status, "artifact_missing", path)

    def test_an_incomplete_artifact_keeps_the_canary_closed(self):
        from harmony_agent.decision.calibration import load_calibration
        cases = ({}, self.complete(confidence_threshold=None),
                 self.complete(confidence_threshold=2.0),
                 self.complete(certainty_threshold="high"),
                 self.complete(dataset_sha256="short"),
                 self.complete(dataset_sha256="z" * 64),
                 self.complete(calibration_id=""))
        for index, payload in enumerate(cases):
            record, status = load_calibration(self.artifact(payload, f"c{index}.json"))
            self.assertIsNone(record, payload)
            self.assertEqual(status, "artifact_incomplete", payload)

    def test_an_unreadable_artifact_keeps_the_canary_closed(self):
        from harmony_agent.decision.calibration import load_calibration
        path = self.root / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        record, status = load_calibration(path)
        self.assertIsNone(record)
        self.assertEqual(status, "artifact_unreadable")

    def test_a_revision_mismatch_keeps_the_canary_closed(self):
        from harmony_agent.decision.calibration import load_calibration
        path = self.artifact(self.complete())
        record, status = load_calibration(path, expected_revision="other-rev")
        self.assertIsNone(record)
        self.assertEqual(status, "provider_revision_mismatch")

    def test_a_complete_artifact_loads_and_owns_the_thresholds(self):
        from harmony_agent.decision.calibration import load_calibration
        record, status = load_calibration(self.artifact(self.complete()))
        self.assertEqual(status, "ready")
        self.assertEqual(record.calibration_id, "cal-1")
        self.assertEqual(record.confidence_threshold, 0.8)
        self.assertEqual(record.certainty_threshold, 0.7)


if __name__ == "__main__":
    unittest.main()
