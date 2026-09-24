"""T08 M2 runner: typed steps parse; no fake success on unsupported."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from accept_m2 import dry_run, parse_step, judge
from harmony_agent.memory import Memory


class ParseStepTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(parse_step("tap:首页"), ("tap", "首页"))
        self.assertEqual(parse_step("back"), ("back", ""))
        self.assertEqual(parse_step("wait:text_present=综合"),
                         ("wait", "text_present=综合"))
        self.assertEqual(parse_step("replace_text:id:search_input=query"),
                         ("replace_text", "id:search_input=query"))


class DryRunTests(unittest.TestCase):
    def test_shipped_m2_file_is_well_formed(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "evals" / "tasks" / "m2-30.json"
        plan = dry_run(path)
        self.assertEqual(plan["status"], "ok", plan)
        self.assertGreaterEqual(plan["task_count"], 30)

    def test_long_device_file_is_well_formed(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "evals" / "tasks" / "long-device.json"
        from accept_long_task import dry_run as long_dry
        plan = long_dry(path)
        self.assertEqual(plan["status"], "ok", plan)

    def test_missing_criteria_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"tasks": [{"id": "x", "steps": ["back"]}]}),
                           encoding="utf-8")
            self.assertEqual(dry_run(bad)["status"], "invalid")


class JudgeTests(unittest.TestCase):
    def test_inconclusive_is_not_pass(self):
        obs = {"observation_id": "o1", "catalog": [], "foreground_bundle": None,
               "actionable": True}
        report = judge([{"id": "a", "type": "foreground_is", "value": "com.x"}], obs)
        self.assertEqual(report["verdict"], "inconclusive")

    def test_surface_is_uses_classifier(self):
        obs = {"observation_id": "o1", "catalog": [], "foreground_bundle": "com.x",
               "actionable": True}
        report = judge([{"id": "s", "type": "surface_is", "value": "foreign"}],
                       obs, baseline=None)
        self.assertEqual(report["verdict"], "pass")


class ResumeCheckpointTests(unittest.TestCase):
    def test_memory_snapshot_hash_is_stable_and_sensitive(self):
        m1 = Memory(goal="g", constraints=["allowed_apps"])
        m2 = Memory(goal="g", constraints=["allowed_apps"])
        self.assertEqual(m1.snapshot_hash(), m2.snapshot_hash())
        m2.record_fact("k", "v", "evidence")
        self.assertNotEqual(m1.snapshot_hash(), m2.snapshot_hash())


if __name__ == "__main__":
    unittest.main()
