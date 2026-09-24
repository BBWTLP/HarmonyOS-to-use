"""T05: post-observation is consumed once; skip-check does not double-read."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.host import AgentHost
from harmony_runtime.runtime import Runtime


def task_payload(request_id, *, steps, criteria=None):
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "delegated",
        "goal": "打开微博搜索页",
        "scope": {"device_ref": "current-authorized-device",
                  "allowed_apps": [WEIBO],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria or [
            {"id": "ok", "type": "element_present", "target_key": "热搜榜"}],
        "arguments": {"steps": steps, "query": "鸿蒙"},
        "budget": {"max_dispatches": 6, "max_seconds": 60, "max_model_calls": 4},
        "model_profile": "local_off",
    }


class CountingRuntime(Runtime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observe_calls = 0
        self.observe_modes = []

    def observe(self, owner, session_id, include_image=False, mode="FAST", cache=True):
        self.observe_calls += 1
        self.observe_modes.append(mode)
        return super().observe(owner, session_id, include_image=include_image,
                               mode=mode, cache=cache)


class ObserveReuseTests(unittest.TestCase):
    def test_short_task_does_not_double_read_per_step(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = CountingRuntime(root, factory=FakeWeiboDevice,
                                      discover=lambda: ["fake-device"])
            try:
                owner = "tester"
                sid = runtime.session(owner, "open", device_id="fake-device")["session_id"]
                host = AgentHost(runtime, pathlib.Path(root) / "agent",
                                 profile="local_off", repo_root=pathlib.Path(root))
                try:
                    payload = task_payload("obs-reuse-1", steps="tap:搜索")
                    created = host.run_task(owner, session_id=sid, task=payload)
                    task_id = created["task_id"]
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        status = host.task_status(owner, task_id=task_id)
                        if status["terminal"] or status["status"] in (
                                "RECONCILIATION_REQUIRED", "PAUSED", "WAITING_USER"):
                            break
                        time.sleep(0.02)
                    result = host.task_result(owner, task_id=task_id)["result"]
                    # One facade-level observe per subgoal attempt is enough;
                    # skip-check + attempt must share one capture. Preflight
                    # lives inside act and is not counted here.
                    self.assertLessEqual(
                        runtime.observe_calls, 3,
                        f"too many facade observes: {runtime.observe_modes}")
                    self.assertNotIn("FULL", runtime.observe_modes,
                                     "normal nav must prefer FAST")
                    self.assertEqual(result.get("unresolved_actions") or [], [])
                finally:
                    host.close()
            finally:
                runtime.close()

    def test_post_observation_helper_requires_actionable_fresh_capture(self):
        from harmony_agent.supervisor import post_observation
        self.assertIsNone(post_observation(None))
        self.assertIsNone(post_observation({"execution_status": "unknown"}))
        self.assertIsNone(post_observation({
            "execution_status": "executed",
            "before_observation_id": "obs1",
            "observation": {"observation_id": "obs1", "actionable": True},
        }))
        reused = post_observation({
            "execution_status": "executed",
            "before_observation_id": "obs1",
            "observation": {"observation_id": "obs2", "actionable": True},
        })
        self.assertEqual(reused["observation_id"], "obs2")
        self.assertIsNone(post_observation({
            "execution_status": "executed",
            "before_observation_id": "obs1",
            "observation": {"observation_id": "obs2", "actionable": False},
        }))


if __name__ == "__main__":
    unittest.main()
