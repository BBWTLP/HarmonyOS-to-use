"""v3.2 Pre-Device Gate: dummy-device soak for lifecycle leaks.

This is not a performance claim. It exercises a fake device many times and
reports resource deltas, then asserts only structural invariants that are true
by contract: no monotonic thread growth, no duplicate dispatch, no unexplained
unresolved write, and a clean close. Where a number has no agreed threshold it is
reported, not asserted.

Cycles are configurable so the gate can be widened on a release machine:
``HARMONY_SOAK_CYCLES`` (default 1200) and ``HARMONY_SOAK_TASKS`` (default 24).
"""
from __future__ import annotations

import ctypes
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.host import AgentHost
from harmony_agent.decision.providers.base import ProviderUnavailable
from harmony_runtime.runtime import Runtime

DEVICE = "fake-device"
CYCLES = int(os.environ.get("HARMONY_SOAK_CYCLES", "1200"))
TASKS = int(os.environ.get("HARMONY_SOAK_TASKS", "24"))


def _rss_bytes() -> int | None:
    """Best-effort resident set size; None when unavailable."""
    try:
        import psutil                       # not a dependency; used when present
        return int(psutil.Process().memory_info().rss)
    except Exception:
        pass
    if os.name == "nt":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                                        counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            return None
    return None


def _handle_count() -> int | None:
    if os.name != "nt":
        return None
    try:
        count = ctypes.c_ulong(0)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            return int(count.value)
    except Exception:
        return None
    return None


def _directory_bytes(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


class SoakDevice(FakeWeiboDevice):
    """Counts every dispatch so a duplicate write is visible."""

    def __init__(self, serial):
        super().__init__(serial)
        self.dispatches: list[str] = []

    def dispatch(self, action, target):
        self.dispatches.append(action.kind)
        self.writes.append({"kind": action.kind, "request_id": action.kind})
        # Flip between two pages that both expose the target, so every dispatch
        # is a real page change and the run never waits out an 8s deadline.
        self.stage = "editor" if self.stage == "home" else "home"


class SoakDuplicateDevice(SoakDevice):
    """An ambiguous page, so a choice question exists and a provider is consulted."""

    def tree(self):
        return {"attributes": {"bundleName": WEIBO, "type": "Root",
                               "bounds": "[0,0][1080,2340]"},
                "children": [
                    {"attributes": {"bundleName": WEIBO, "type": "Button",
                                    "text": "重复", "clickable": "true",
                                    "bounds": "[40,400][300,460]"}, "children": []},
                    {"attributes": {"bundleName": WEIBO, "type": "Button",
                                    "text": "重复", "clickable": "true",
                                    "bounds": "[40,500][300,560]"}, "children": []}]}


class OfflineSoakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report: dict[str, object] = {"cycles": CYCLES, "tasks": TASKS}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices: list[SoakDevice] = []
        self.hosts: list[AgentHost] = []
        outer = self

        class Device(SoakDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: [DEVICE])
        self.owner = "soak"
        self.session = self.runtime.session(self.owner, "open",
                                            device_id=DEVICE)["session_id"]

    def tearDown(self):
        for host in self.hosts:
            host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def resources(self) -> dict[str, object]:
        journal = self.root / "runtime" / "journal.sqlite3"
        return {"rss": _rss_bytes(), "threads": threading.active_count(),
                "handles": _handle_count(),
                "journal_bytes": journal.stat().st_size if journal.exists() else 0,
                "artifact_bytes": _directory_bytes(self.root / "agent" / "artifacts"),
                "candidate_registry": len(self.hosts[-1].registry._issued)
                if self.hosts else 0}

    # -- observation / action soak -----------------------------------------
    def test_observe_act_history_cycle_does_not_leak(self):
        before = self.resources()
        errors = 0
        act_cycles = max(1, CYCLES // 4)
        for index in range(CYCLES):
            try:
                observed = self.runtime.observe(self.owner, self.session)
                if index % 4 == 0:
                    self.runtime.act(self.owner, {
                        "session_id": self.session,
                        "request_id": f"soak-{index}",
                        "observation_id": observed["observation_id"],
                        "action": {"kind": "tap", "target": {"action_id": "n0"}}})
                if index % 50 == 0:
                    self.runtime.history(self.owner, self.session, limit=5)
                    self.runtime.wait(self.owner, session_id=self.session,
                                      expected={"text": "首页"}, timeout_ms=100,
                                      poll_ms=100)
            except Exception:
                errors += 1
        after = self.resources()
        device = self.devices[-1]
        self.assertEqual(errors, 0)
        self.assertEqual(len(device.dispatches), act_cycles)
        status = self.runtime.session(self.owner, "status", session_id=self.session)
        self.assertFalse(status["recovery_required"])
        self.assertEqual(status["unresolved_actions"], [])
        self.assertLessEqual(after["threads"], before["threads"] + 1)
        self.report["observe_act"] = {"before": before, "after": after,
                                      "errors": errors,
                                      "dispatches": len(device.dispatches)}

    # -- task lifecycle soak -----------------------------------------------
    def payload(self, request_id, *, criterion="搜索", steps="tap:搜索",
                profile="local_off"):
        return {"schema_version": "2.0", "request_id": request_id,
                "mode": "delegated", "goal": "打开微博搜索页",
                "scope": {"device_ref": "d", "allowed_apps": [WEIBO],
                          "allowed_actions": ["tap", "back"],
                          "cloud_data_policy": "disabled"},
                "success_criteria": [{"id": "c", "type": "element_present",
                                      "target_key": criterion}],
                "arguments": {"steps": steps},
                "budget": {"max_dispatches": 4, "max_seconds": 30,
                           "max_model_calls": 2},
                "model_profile": profile}

    def settle(self, host, task_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in ("RECONCILIATION_REQUIRED",
                                                          "PAUSED"):
                return status
            time.sleep(0.01)
        self.fail("task did not finish")

    def test_task_lifecycle_soak_closes_cleanly(self):
        host = AgentHost(self.runtime, self.root / "agent", profile="local_off",
                         repo_root=self.root)
        self.hosts.append(host)
        before = self.resources()
        statuses: list[str] = []
        for index in range(TASKS):
            created = host.run_task(self.owner, session_id=self.session,
                                    task=self.payload(f"soak-task-{index}"))
            statuses.append(self.settle(host, created["task_id"])["status"])
            if index % 6 == 0:
                host.artifacts.sweep(dry_run=True)
        after = self.resources()
        self.assertNotIn("RECONCILIATION_REQUIRED", statuses)
        self.assertTrue(all(state in ("SUCCEEDED", "FAILED", "PARTIAL")
                            for state in statuses), statuses)
        self.assertLessEqual(after["threads"], before["threads"] + 1)
        connection = sqlite3.connect(host.store.path)
        try:
            rows = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(rows, TASKS)
        self.assertFalse(self.runtime.session(self.owner, "status",
                                              session_id=self.session)["recovery_required"])
        self.report["tasks"] = {"before": before, "after": after,
                                "statuses": statuses, "task_rows": rows}

    # -- provider fallback soak --------------------------------------------
    def test_a_failing_provider_does_not_loop_or_leak(self):
        class TimingOut:
            provider = "decider"
            model_revision = "soak"

            def __init__(self):
                self.calls = 0

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                self.calls += 1
                raise ProviderUnavailable("model_timeout", "soak")

        provider = TimingOut()
        # Ambiguous targets so a choice question exists and the provider is
        # actually consulted rather than skipped for a single candidate.
        devices: list[SoakDuplicateDevice] = []

        def factory(serial):
            device = SoakDuplicateDevice(serial)
            devices.append(device)
            return device

        runtime = Runtime(self.root / "runtime-shadow", factory=factory,
                          discover=lambda: [DEVICE])
        session = runtime.session(self.owner, "open", device_id=DEVICE)["session_id"]
        host = AgentHost(runtime, self.root / "agent-shadow",
                         profile="local_shadow", fast_provider=provider,
                         repo_root=self.root)
        self.hosts.append(host)
        before = self.resources()
        try:
            for index in range(6):
                created = host.run_task(
                    self.owner, session_id=session,
                    task=self.payload(f"soak-provider-{index}",
                                      criterion="不会出现", steps="tap:重复",
                                      profile="local_shadow"))
                self.settle(host, created["task_id"])
        finally:
            runtime.close()
        after = self.resources()
        # The budget bounds the attempts: at most two per task with this budget.
        self.assertGreater(provider.calls, 0)
        self.assertLessEqual(provider.calls, 6 * 2)
        self.assertLessEqual(after["threads"], before["threads"] + 1)
        self.report["provider_fallback"] = {"before": before, "after": after,
                                            "provider_calls": provider.calls}

    def test_clear_had_no_duplicate_dispatch(self):
        host = AgentHost(self.runtime, self.root / "agent", profile="local_off",
                         repo_root=self.root)
        self.hosts.append(host)
        created = host.run_task(self.owner, session_id=self.session,
                                task=self.payload("soak-dedupe"))
        self.settle(host, created["task_id"])
        observed = self.runtime.observe(self.owner, self.session)
        request = {"session_id": self.session, "request_id": "soak-dup",
                   "observation_id": observed["observation_id"],
                   "action": {"kind": "tap", "target": {"action_id": "n0"}}}
        first = self.runtime.act(self.owner, dict(request))
        again = self.runtime.act(self.owner, dict(request))
        self.assertEqual(first["execution_status"], "executed")
        self.assertTrue(again.get("deduplicated"))
        self.assertEqual(len(self.devices[-1].dispatches), 2)

    def test_submitting_a_task_prunes_expired_candidates(self):
        """A long-lived host must not accumulate every candidate it ever issued."""
        from harmony_agent.candidates import CandidateRegistry
        from harmony_agent.contracts import Predicate
        from harmony_agent.grounding import GroundingIntent

        host = AgentHost(self.runtime, self.root / "agent", profile="local_off",
                         repo_root=self.root)
        self.hosts.append(host)
        observed = self.runtime.observe(self.owner, self.session)
        registry = CandidateRegistry(ttl_seconds=-1)
        registry.build(task_id="t", subgoal_id="s", scope_id="sc",
                       observation=observed, controller_epoch=0,
                       intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
                       action_kind="tap", arguments={},
                       expected_predicates=[Predicate(id="p", type="foreground_is",
                                                      value=WEIBO)])
        host.registry = registry
        stale = set(registry._issued)
        self.assertTrue(stale)
        created = host.run_task(self.owner, session_id=self.session,
                                task=self.payload("soak-prune"))
        self.settle(host, created["task_id"])
        self.assertFalse(stale & set(registry._issued))

    @classmethod
    def tearDownClass(cls):
        print("\nsoak report: " + json.dumps(cls.report, ensure_ascii=False,
                                             default=str))


if __name__ == "__main__":
    unittest.main()
