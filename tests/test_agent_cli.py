import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harmony_agent.artifacts import ArtifactStore
from harmony_agent.cli import (evidence_index, list_tasks, main, replay, task_detail)
from harmony_agent.contracts import TaskSubmit
from harmony_agent.supervisor import TaskStore


def task_payload(request_id="cli-1"):
    return {
        "schema_version": "2.0", "request_id": request_id, "mode": "delegated",
        "goal": "在微博搜索", "scope": {"device_ref": "d",
                                        "allowed_apps": ["com.sina.weibo.stage"],
                                        "allowed_actions": ["tap"],
                                        "cloud_data_policy": "disabled"},
        "success_criteria": [{"id": "c1", "type": "text_equals", "value": "综合"}],
        "arguments": {}, "budget": {"max_dispatches": 4, "max_seconds": 60,
                                    "max_model_calls": 2},
        "model_profile": "local_shadow",
    }


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name)
        self.store = TaskStore(self.state / "agent-tasks.sqlite3")
        self.artifacts = ArtifactStore(self.state / "artifacts")
        created = self.store.create(TaskSubmit.model_validate(task_payload()),
                                    controller_epoch=3)
        self.task_id = created["task_id"]
        self.store.add_event(self.task_id, "task_started", {"plan_version": 1})
        self.store.set_state(self.task_id, "SUCCEEDED")
        self.store.save_result(self.task_id, {"status": "SUCCEEDED",
                                              "unresolved_actions": []})
        self.store.add_event(self.task_id, "task_finished", {"status": "SUCCEEDED"})
        self.artifact = self.artifacts.put_blob(b"evidence", kind="event",
                                                task_id=self.task_id)

    def tearDown(self):
        self.store.close()
        self.artifacts.close()
        self.tmp.cleanup()

    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv, self.state)
        return code, json.loads(buffer.getvalue())

    def test_tasks_listing_is_paginated_and_read_only(self):
        payload = list_tasks(self.state, 10, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["task_id"], self.task_id)
        self.assertIn("never dispatches", payload["note"])

    def test_task_detail_reports_events_and_cursor(self):
        detail = task_detail(self.state, self.task_id, events=10, include_result=True)
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["state"], "SUCCEEDED")
        self.assertEqual(detail["controller_epoch"], 3)
        self.assertEqual([item["type"] for item in detail["events"]],
                         ["task_started", "task_finished"])
        self.assertEqual(detail["last_seq"], 2)
        self.assertEqual(detail["result"]["status"], "SUCCEEDED")
        self.assertTrue(detail["goal_present"])

    def test_unknown_task_is_not_found(self):
        payload = task_detail(self.state, "task_missing", events=5, include_result=False)
        self.assertEqual(payload["status"], "not_found")

    def test_evidence_index_marks_missing_files(self):
        payload = evidence_index(self.state, task_id=self.task_id, limit=10, offset=0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["items"][0]["artifact_id"], self.artifact["artifact_id"])
        self.assertTrue(payload["items"][0]["present"])
        self.assertFalse(payload["items"][0]["missing"])
        self.assertNotIn("relative_path", payload["items"][0])
        # Remove the blob, keep the record: the CLI must say it is missing.
        (self.state / "artifacts" / self.artifact["relative_path"]).unlink()
        payload = evidence_index(self.state, task_id=self.task_id, limit=10, offset=0)
        self.assertEqual(payload["missing_files"], 1)
        self.assertTrue(payload["items"][0]["missing"])

    def test_replay_is_read_only_and_reports_evidence(self):
        payload = replay(self.state, self.task_id, limit=10)
        self.assertTrue(payload["read_only"])
        self.assertIn("does not replay", payload["note"])
        self.assertEqual(payload["state"], "SUCCEEDED")
        self.assertEqual(len(payload["timeline"]), 2)

    def test_missing_database_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as other:
            payload = list_tasks(pathlib.Path(other), 5, 0)
        self.assertEqual(payload["status"], "not_found")

    def test_cli_rejects_out_of_range_limit(self):
        code, payload = self.run_cli(["tasks", "--limit", "5000"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "invalid_arguments")

    def test_cli_returns_zero_for_a_known_task(self):
        code, payload = self.run_cli(["task", "--task-id", self.task_id, "--events", "5"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["task_id"], self.task_id)

    def test_runtime_cli_routes_agent_subcommand(self):
        import harmony_runtime.cli as runtime_cli
        saved = sys.argv
        buffer = io.StringIO()
        try:
            sys.argv = ["harmony-runtime", "agent", "tasks", "--state-dir", str(self.state)]
            with redirect_stdout(buffer):
                code = runtime_cli.main()
        finally:
            sys.argv = saved
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["total"], 1)


if __name__ == "__main__":
    unittest.main()
