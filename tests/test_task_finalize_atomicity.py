"""OFFLINE-2 regression: a result-bearing state is never visible without a result.

The defect: the store published the final state and the final result in two
separate commits, so a poller could observe ``RECONCILIATION_REQUIRED`` (or
``SUCCEEDED``/``FAILED``/``CANCELLED``/``PARTIAL``) while ``task()["result"]``
was still ``None``. Two paths caused it: the runner published
``RECONCILIATION_REQUIRED`` early and then finished the task, and ``_finish()``
wrote state and result as two commits.

These tests assert the invariant directly, from a second thread, while the
finalize happens.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.contracts import TaskSubmit
from harmony_agent.host import AgentHost
from harmony_agent.supervisor import RESULT_BEARING_STATES, TaskStore
from harmony_runtime.runtime import Runtime

WEIBO_DEVICE = "fake-device"
#: One iteration per (state, repetition). 5 states x 100 = 500 finalize races.
REPETITIONS = 100


def task_submit(request_id: str) -> TaskSubmit:
    return TaskSubmit.model_validate({
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "auto",
        "goal": "完成微博搜索",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": [{"id": "editor", "type": "element_present",
                              "target_key": "搜索"}],
        "arguments": {},
        "budget": {"max_dispatches": 4, "max_seconds": 30, "max_model_calls": 0},
        "model_profile": "local_off",
    })


class FinalizeVisibilityTests(unittest.TestCase):
    """Storage-level race: poll `task()` while another thread finalizes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(pathlib.Path(self.tmp.name) / "tasks.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _poll_during_finalize(self, state: str, result: dict) -> list[dict]:
        request_id = f"req-{state}-{uuid.uuid4().hex}"
        created = self.store.create(task_submit(request_id), 0)
        task_id = created["task_id"]
        violations: list[dict] = []
        stop = threading.Event()

        def poller() -> None:
            while not stop.is_set():
                record = self.store.task(task_id)
                if record is None:
                    continue
                if record["state"] in RESULT_BEARING_STATES and record["result"] is None:
                    violations.append({"state": record["state"], "result": None})

        thread = threading.Thread(target=poller, name="poller", daemon=True)
        thread.start()
        try:
            # Let the poller observe the pre-finalize row, then publish: the
            # window this test hunts for spans the publish itself.
            time.sleep(0.001)
            self.assertTrue(self.store.finalize_task(
                task_id, state, result, event=("task_finished", {"status": state})))
        finally:
            stop.set()
            thread.join(timeout=5)
        return violations

    def test_no_result_bearing_state_is_ever_visible_without_a_result(self):
        for state in RESULT_BEARING_STATES:
            for _ in range(REPETITIONS):
                violations = self._poll_during_finalize(state, {"status": state})
                self.assertEqual(violations, [], f"{state} exposed without a result")

    def test_finalize_publishes_state_and_result_together(self):
        created = self.store.create(task_submit("req-single"), 0)
        task_id = created["task_id"]
        self.assertIsNone(self.store.task(task_id)["result"])
        self.store.finalize_task(task_id, "RECONCILIATION_REQUIRED",
                                 {"status": "RECONCILIATION_REQUIRED",
                                  "resolution_required": True})
        record = self.store.task(task_id)
        self.assertEqual(record["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(record["result"]["resolution_required"], True)

    def test_finalize_records_the_closing_event_in_the_same_transaction(self):
        created = self.store.create(task_submit("req-event"), 0)
        task_id = created["task_id"]
        self.store.finalize_task(task_id, "SUCCEEDED", {"status": "SUCCEEDED"},
                                 event=("task_finished", {"status": "SUCCEEDED"}))
        events = self.store.events(task_id)["items"]
        self.assertEqual([item["type"] for item in events], ["task_finished"])

    def test_a_terminal_state_is_never_overwritten_by_a_late_outcome(self):
        created = self.store.create(task_submit("req-terminal"), 0)
        task_id = created["task_id"]
        self.assertTrue(self.store.finalize_task(task_id, "CANCELLED",
                                                 {"status": "CANCELLED"}))
        self.assertFalse(self.store.finalize_task(
            task_id, "RECONCILIATION_REQUIRED",
            {"status": "RECONCILIATION_REQUIRED"}))
        record = self.store.task(task_id)
        self.assertEqual(record["state"], "CANCELLED")
        self.assertEqual(record["result"]["status"], "CANCELLED")

    def test_a_non_result_bearing_state_cannot_be_finalized(self):
        created = self.store.create(task_submit("req-running"), 0)
        with self.assertRaises(Exception) as error:
            self.store.finalize_task(created["task_id"], "RUNNING", {"status": "RUNNING"})
        self.assertEqual(getattr(error.exception, "code", None), "invalid_final_state")


class RunnerUnknownWriteVisibilityTests(unittest.TestCase):
    """End-to-end: the real runner must not publish the state before the result."""

    def _run_unknown_write(self, attempt: int) -> tuple[list[dict], dict, dict]:
        """Run one task whose first dispatch dies mid-flight.

        Each attempt gets a fresh runtime: an unknown write leaves the session
        with `recovery_required`, which correctly refuses later dispatches, so a
        second task in the same runtime would not reach the same path.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            pending = {"value": True}

            class Device(FakeWeiboDevice):
                def dispatch(self, action, target):
                    if pending["value"]:
                        pending["value"] = False
                        # Keep the run up long enough for the poller to be inside
                        # the window where the final state is being published.
                        time.sleep(0.05)
                        raise OSError("link lost")
                    return super().dispatch(action, target)

            runtime = Runtime(root / "runtime", factory=Device,
                              discover=lambda: [WEIBO_DEVICE])
            host = AgentHost(runtime, root / "agent", profile="local_off",
                             repo_root=root)
            try:
                session_id = runtime.session("tester", "open",
                                             device_id=WEIBO_DEVICE)["session_id"]
                created = host.run_task("tester", session_id=session_id,
                                        task=task_submit(f"race-{attempt}").model_dump())
                task_id = created["task_id"]
                violations: list[dict] = []
                stop = threading.Event()

                def poller() -> None:
                    while not stop.is_set():
                        status = host.task_status("tester", task_id=task_id)
                        if (status["status"] in RESULT_BEARING_STATES
                                and not status["result_available"]):
                            violations.append({"state": status["status"]})

                thread = threading.Thread(target=poller, name="poller", daemon=True)
                thread.start()
                deadline = time.time() + 30
                try:
                    while time.time() < deadline:
                        status = host.task_status("tester", task_id=task_id)
                        if status["status"] in RESULT_BEARING_STATES:
                            break
                        time.sleep(0.002)
                finally:
                    stop.set()
                    thread.join(timeout=5)
                return violations, status, host.task_result("tester", task_id=task_id)
            finally:
                host.close()
                runtime.close()

    def test_unknown_write_state_is_never_visible_without_its_result(self):
        for attempt in range(10):
            violations, status, result = self._run_unknown_write(attempt)
            self.assertEqual(violations, [], f"attempt {attempt}: state without result")
            self.assertEqual(status["status"], "RECONCILIATION_REQUIRED",
                             f"attempt {attempt}")
            self.assertTrue(result["available"], f"attempt {attempt}")
            self.assertEqual(result["result"]["status"], "RECONCILIATION_REQUIRED")
            self.assertTrue(result["result"]["resolution_required"])


if __name__ == "__main__":
    unittest.main()
