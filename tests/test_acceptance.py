"""Acceptance must not accept wrong topics or publish transport exception data."""
import contextlib
import io
import json
import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from scripts import accept_weibo
from harmony_runtime.mcp_server import flat_tree


class AcceptanceTests(unittest.TestCase):

    @staticmethod
    def input_observation(value="鸿蒙"):
        attrs = {"type": "TextInput", "bounds": "[10,10][90,30]", "text": value,
                 "hint": "搜索", "focused": "true", "enabled": "true", "id": "search",
                 "bundleName": accept_weibo.WEIBO, "accessibilityId": "input-1", "hostWindowId": "win-1"}
        tree = {"attributes": attrs, "children": []}
        nodes = accept_weibo.tree_catalog(tree, (100, 200))
        nodes.append({"type": "List", "enabled": True, "action_id": "list"})
        return {"observation_id": "obs", "capture_ms": 1, "foreground_bundle": accept_weibo.WEIBO,
                "foreground_consistent": True, "actionable": True, "catalog": nodes, "tree": tree,
                "display": {"width": 100, "height": 200}}

    def test_strict_input_uses_raw_text_not_legacy_hint(self):
        obs = self.input_observation("")
        obs["catalog"][0]["text"] = "搜索"
        target = accept_weibo.raw_input_target(obs)
        self.assertTrue(accept_weibo.strict_input_matches(obs, target, ""))
        self.assertFalse(accept_weibo.strict_input_matches(obs, target, "搜索"))

    def test_strict_input_reads_mcp_flat_tree_and_inherits_bundle(self):
        obs = self.input_observation("")
        child = obs["tree"]
        child["attributes"].pop("bundleName")
        tree = {"attributes": {"bundleName": accept_weibo.WEIBO}, "children": [child]}
        flat = flat_tree(tree)
        self.assertEqual(accept_weibo.full_tree(flat), tree)
        obs["tree"] = flat
        target = accept_weibo.raw_input_target(obs)
        self.assertTrue(accept_weibo.strict_input_matches(obs, target, ""))

    def test_flat_tree_rejects_invalid_topology(self):
        tree = {"attributes": {}, "children": [self.input_observation()["tree"]]}
        for change in ("duplicate", "orphan", "cycle", "root", "format", "children"):
            flat = flat_tree(tree)
            if change == "duplicate": flat["nodes"][1]["node_id"] = "t0"
            elif change == "orphan": flat["nodes"][1]["parent_id"] = "missing"
            elif change == "cycle": flat["nodes"][1]["parent_id"] = "t1"
            elif change == "root": flat["root_id"] = "other"
            elif change == "format": flat["format"] = "unknown"
            elif change == "children": flat["nodes"][0]["has_children_field"] = False
            with self.subTest(change=change), self.assertRaises(accept_weibo.AcceptanceFailure):
                accept_weibo.full_tree(flat)

    def test_strict_input_rejects_missing_value_tree_or_identity(self):
        for key in ("text", "id", "tree"):
            obs = self.input_observation()
            if key == "tree":
                obs.pop("tree")
            elif key == "id":
                obs["tree"]["attributes"].pop("id")
                obs["tree"]["attributes"].pop("accessibilityId")
            else:
                obs["tree"]["attributes"].pop(key)
            with self.subTest(key=key), self.assertRaises(accept_weibo.AcceptanceFailure):
                accept_weibo.raw_input_target(obs)

    def test_strict_input_rejects_changed_window_or_appended_value(self):
        original = self.input_observation()
        target = accept_weibo.raw_input_target(original)
        appended = self.input_observation("鸿蒙鸿蒙")
        self.assertFalse(accept_weibo.strict_input_matches(appended, target, "鸿蒙"))
        changed = self.input_observation()
        changed["tree"]["attributes"]["hostWindowId"] = "win-2"
        self.assertFalse(accept_weibo.strict_input_matches(changed, target, "鸿蒙"))

    def test_strict_input_failed_read_records_write_without_replay(self):
        calls = []
        good = self.input_observation()
        class Client:
            async def call_tool(self, name, payload):
                calls.append((name, payload))
                if name == "mobile_session":
                    data = {"session_id": "test-session"}
                elif name == "mobile_observe":
                    if sum(n == "mobile_act" for n, _ in calls) == 2:
                        raise OSError("private backend content")
                    data = good
                else:
                    data = {"execution_status": "executed", "verification_status": "verified", "observation": good}
                return SimpleNamespace(is_error=False, structured_content=data)
        @contextlib.asynccontextmanager
        async def transport(args):
            yield Client()
        report = {"capture_ms": [], "steps": [], "rounds": [], "pre_dispatch_refreshes": 0}
        with patch.object(accept_weibo, "_transport", transport), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                asyncio.run(accept_weibo.run(SimpleNamespace(profile="strict-input", rounds=1), report))
        self.assertEqual(sum(n == "mobile_act" for n, _ in calls), 2)  # launch + one replace
        self.assertEqual(report["steps"][-1]["execution_status"], "executed")
        self.assertEqual(report["steps"][-1]["validation_error"], "settle_observation_failed")
        self.assertFalse(report["steps"][-1]["raw_value_verified"])
        self.assertNotIn("private backend content", json.dumps(report))
        self.assertEqual(calls[-1][1]["operation"], "close")

    def test_strict_input_profile_verifies_all_three_values_over_full_reads(self):
        calls = []
        owner = self
        class Client:
            value = ""
            async def call_tool(self, name, payload):
                calls.append((name, payload))
                if name == "mobile_session":
                    data = {"session_id": "test-session"}
                elif name == "mobile_observe":
                    data = owner.input_observation(self.value)
                    data["tree"] = flat_tree(data["tree"])
                else:
                    action = payload["request"]["action"]
                    if action["kind"] == "replace_text":
                        self.value = action["text"]
                    data = {"execution_status": "executed", "verification_status": "verified",
                            "observation": owner.input_observation(self.value)}
                return SimpleNamespace(is_error=False, structured_content=data)
        @contextlib.asynccontextmanager
        async def transport(args):
            yield Client()
        report = {"capture_ms": [], "steps": [], "rounds": [], "pre_dispatch_refreshes": 0}
        with patch.object(accept_weibo, "_transport", transport), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(accept_weibo.run(SimpleNamespace(profile="strict-input", rounds=1), report))
        actions = [p["request"]["action"] for n, p in calls if n == "mobile_act"]
        self.assertEqual([a["text"] for a in actions if a["kind"] == "replace_text"], ["鸿蒙", "鸿蒙", ""])
        self.assertEqual(sum(n == "mobile_observe" and p["mode"] == "FULL" for n, p in calls), 6)
        self.assertTrue(all(s["raw_value_verified"] for s in report["steps"][1:]))
        self.assertEqual(report["status"], "passed")
    def suggestion_observation(self):
        nodes = [
            {"action_id": "input", "type": "TextInput", "focused": True},
            {"action_id": "list", "type": "List", "clickable": True},
            {"action_id": "suggestion", "type": "Column", "clickable": True, "parent_action_id": "list"},
            {"action_id": "label", "type": "Text", "text": "鸿蒙智行", "parent_action_id": "suggestion"},
            {"action_id": "account", "type": "Row", "clickable": True, "parent_action_id": "list"},
            {"action_id": "account_label", "type": "Text", "text": "鸿蒙智行", "parent_action_id": "account"},
        ]
        return {"foreground_bundle": accept_weibo.WEIBO, "foreground_consistent": True,
                "actionable": True, "catalog": [{"enabled": True, "bundle": accept_weibo.WEIBO, **n} for n in nodes]}

    def test_suggestion_does_not_select_same_named_account(self):
        obs = self.suggestion_observation()
        self.assertEqual(accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行"), "suggestion")
        obs["catalog"].reverse()
        self.assertEqual(accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行"), "suggestion")

    def test_suggestion_requires_exact_label_and_parent(self):
        for changes in ({"text": "鸿蒙智行问界"}, {"parent_action_id": None},
                        {"parent_action_id": "missing"}, {"bundle": "other"}):
            obs = self.suggestion_observation()
            obs["catalog"][3].update(changes)
            with self.subTest(changes=changes), self.assertRaises(accept_weibo.AcceptanceFailure):
                accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行")

    def test_suggestion_rejects_duplicate_rows(self):
        obs = self.suggestion_observation()
        obs["catalog"] += [{**obs["catalog"][2], "action_id": "second"},
                           {**obs["catalog"][3], "action_id": "second_label", "parent_action_id": "second"}]
        with self.assertRaisesRegex(accept_weibo.AcceptanceFailure, "ambiguous"):
            accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行")

    def test_suggestion_rejects_unknown_disabled_or_unparented_rows(self):
        for changes in ({"type": "Row"}, {"enabled": False}, {"clickable": False},
                        {"parent_action_id": None}, {"bundle": "other"}):
            obs = self.suggestion_observation()
            obs["catalog"][2].update(changes)
            with self.subTest(changes=changes), self.assertRaises(accept_weibo.AcceptanceFailure):
                accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行")

    def test_suggestion_requires_editor_and_verified_foreground(self):
        for changes in ({"foreground_bundle": "other"}, {"actionable": False}):
            with self.assertRaises(accept_weibo.AcceptanceFailure):
                accept_weibo.search_suggestion_action_id({**self.suggestion_observation(), **changes}, "鸿蒙智行")
        obs = self.suggestion_observation()
        obs["catalog"][0]["focused"] = False
        with self.assertRaisesRegex(accept_weibo.AcceptanceFailure, "editor_unavailable"):
            accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行")

    def test_suggestion_rejects_duplicate_catalog_ids(self):
        obs = self.suggestion_observation()
        obs["catalog"].append(dict(obs["catalog"][2]))
        with self.assertRaisesRegex(accept_weibo.AcceptanceFailure, "identity_invalid"):
            accept_weibo.search_suggestion_action_id(obs, "鸿蒙智行")

    def result_observation(self):
        obs = self.suggestion_observation()
        obs["catalog"] = [{"enabled": True, "bundle": accept_weibo.WEIBO, **n} for n in [
            {"action_id": "header", "type": "Text", "text": "鸿蒙智行", "bounds": [100, 100, 800, 200]},
            {"action_id": "tabs", "type": "Tabs", "resource_id": "tab", "bounds": [0, 250, 1320, 2400]},
            {"action_id": "video", "type": "Column", "clickable": True, "selected": "true", "bounds": [800, 250, 1000, 400]},
            {"action_id": "label", "type": "Text", "text": "视频", "parent_action_id": "video"},
            {"action_id": "feed", "type": "Column", "clickable": True, "bounds": [0, 500, 500, 800]},
            {"action_id": "feed_label", "type": "Text", "text": "视频", "parent_action_id": "feed"},
        ]]
        return obs

    def test_search_tab_requires_header_identity_and_selected_state(self):
        obs = self.result_observation()
        self.assertEqual(accept_weibo.search_tab_node(obs, "视频")["action_id"], "video")
        self.assertTrue(accept_weibo.search_result_selected(obs, "鸿蒙智行", "视频"))
        obs["catalog"][2]["selected"] = "false"
        self.assertFalse(accept_weibo.search_result_selected(obs, "鸿蒙智行", "视频"))
        obs["catalog"][2]["selected"] = "true"
        obs["catalog"][0]["bounds"] = [0, 500, 800, 600]
        self.assertFalse(accept_weibo.search_result_selected(obs, "鸿蒙智行", "视频"))

    def test_search_tab_rejects_ambiguity(self):
        obs = self.result_observation()
        obs["catalog"] += [{**obs["catalog"][2], "action_id": "second"},
                           {**obs["catalog"][3], "action_id": "second_label", "parent_action_id": "second"}]
        with self.assertRaisesRegex(accept_weibo.AcceptanceFailure, "ambiguous"):
            accept_weibo.search_tab_node(obs, "视频")

    def test_settle_is_read_only_and_zero_budget_does_not_read(self):
        calls = []
        async def observe():
            calls.append("observe")
            return {**self.result_observation(), "ready": True}
        after, reads = asyncio.run(accept_weibo.settle_observation({}, observe, lambda o: o.get("ready"), 1000))
        self.assertTrue(after["ready"])
        self.assertEqual((reads, calls), (1, ["observe"]))
        calls.clear()
        _, reads = asyncio.run(accept_weibo.settle_observation({}, observe, lambda o: False, 0))
        self.assertEqual((reads, calls), (0, []))

    def test_settle_stops_on_foreground_loss(self):
        async def observe():
            return {**self.result_observation(), "foreground_bundle": "other"}
        with self.assertRaisesRegex(accept_weibo.AcceptanceFailure, "foreground_unverified"):
            asyncio.run(accept_weibo.settle_observation({}, observe, lambda o: False, 1000))

    def test_topic_variants_preserve_exact_identity(self):
        self.assertTrue(accept_weibo.topic_matches({"测试主题"}, "测试主题"))
        self.assertTrue(accept_weibo.topic_matches({"#测试主题#"}, "测试主题"))
        self.assertFalse(accept_weibo.topic_matches({"测试主题的另一篇文章"}, "测试主题"))
        self.assertFalse(accept_weibo.topic_matches({"##测试主题##"}, "测试主题"))
        self.assertFalse(accept_weibo.topic_matches({"综合", "实时"}, "测试主题"))

    def test_cleanup_failure_overrides_pass_and_redacts_exception(self):
        async def failed_cleanup(args, report):
            report["status"] = "passed"
            raise ExceptionGroup("secret endpoint", [OSError("private-token-example")])
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "report.json"
            with patch("sys.argv", ["accept_weibo", "--execute", "--report", str(destination)]), \
                 patch.object(accept_weibo, "run", failed_cleanup), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(accept_weibo.main(), 1)
            content = destination.read_text(encoding="utf-8")
            self.assertEqual(json.loads(content)["status"], "failed")
            self.assertNotIn("private-token", content)
            self.assertNotIn("secret endpoint", content)

    def test_video_identity_and_progress_require_unambiguous_evidence(self):
        obs = {"catalog": [{"type": "Text", "resource_id": "0", "text": "A unique video description"},
                           {"type": "Slider", "text": "12.500000"}]}
        self.assertEqual(accept_weibo.video_identity(obs), "A unique video description")
        self.assertEqual(accept_weibo.video_progress(obs), 12.5)
        obs["catalog"].append({"type": "Slider", "text": "15"})
        self.assertEqual(accept_weibo.video_progress(obs), -1)
        obs["catalog"].append({"type": "Text", "resource_id": "0", "text": "Another video description"})
        self.assertIsNone(accept_weibo.video_identity(obs))
        self.assertEqual(accept_weibo.video_progress({"catalog": [{"type": "Slider", "text": "NaN"}]}), -1)

    def test_requires_explicit_execution(self):
        with patch("sys.argv", ["accept_weibo"]), patch.object(accept_weibo, "run") as runner, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                accept_weibo.main()
            runner.assert_not_called()


    def test_service_adapter_unwraps_action_requests(self):
        adapter = accept_weibo._ServiceMcpAdapter(Path("."))
        calls = []
        class FakeClient:
            def call(self, method, **params):
                calls.append((method, params))
                return {"status": "ok"}
        adapter.client = FakeClient()
        request = {"session_id": "s", "request_id": "r", "observation_id": "o",
                   "action": {"kind": "back"}}
        result = asyncio.run(adapter.call_tool("mobile_act", {"request": request}))
        self.assertFalse(result.is_error)
        self.assertEqual(calls, [("act", {"arguments": request})])

    def test_service_adapter_rejects_missing_action_request(self):
        adapter = accept_weibo._ServiceMcpAdapter(Path("."))
        result = asyncio.run(adapter.call_tool("mobile_burst", {}))
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "invalid_arguments")

    def test_foreground_guard_rejects_matching_text_in_unverified_app(self):
        valid = {"foreground_bundle": accept_weibo.WEIBO, "foreground_consistent": True,
                 "actionable": True, "catalog": [{"text": "更多热搜"}]}
        accept_weibo.require_weibo(valid)
        for changes, code in [
            ({"foreground_bundle": "com.example.other"}, "weibo_foreground_unverified"),
            ({"foreground_bundle": None}, "weibo_foreground_unverified"),
            ({"foreground_consistent": False}, "weibo_foreground_unverified"),
            ({"actionable": False}, "capture_unstable"),
            ({"blocking_dialog": {"code": "authentication_required"}}, "authentication_required"),
        ]:
            with self.subTest(changes=changes), self.assertRaisesRegex(accept_weibo.AcceptanceFailure, code):
                accept_weibo.require_weibo({**valid, **changes})

    def test_dispatched_auth_failure_is_recorded_and_not_replayed(self):
        calls = []
        class Client:
            async def call_tool(self, name, payload):
                calls.append((name, payload))
                if name == "mobile_session":
                    data = {"session_id": "test-session"}
                elif name == "mobile_observe":
                    data = {"observation_id": "test-observation", "capture_ms": 1}
                else:
                    data = {"status": "authentication_required", "execution_status": "executed",
                            "verification_status": "inconclusive",
                            "observation": {"blocking_dialog": {"code": "authentication_required"}}}
                return SimpleNamespace(is_error=False, structured_content=data)
        @contextlib.asynccontextmanager
        async def transport(args):
            yield Client()
        report = {"capture_ms": [], "steps": [], "rounds": [], "pre_dispatch_refreshes": 0}
        with patch.object(accept_weibo, "_transport", transport), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(accept_weibo.AcceptanceFailure, "authentication_required"):
                asyncio.run(accept_weibo.run(SimpleNamespace(profile="search", rounds=1), report))
        self.assertEqual(len(report["steps"]), 1)
        self.assertEqual(report["steps"][0]["execution_status"], "executed")
        self.assertEqual(report["steps"][0]["validation_error"], "authentication_required")
        self.assertFalse(report["steps"][0]["semantic_pass"])
        self.assertEqual(sum(name == "mobile_act" for name, _ in calls), 1)
        self.assertEqual(calls[-1], ("mobile_session", {"operation": "close", "session_id": "test-session"}))

    def test_progress_profile_accepts_harmony_progress_nodes(self):
        self.assertEqual(accept_weibo.video_progress({"catalog": [{"type": "Progress", "text": "9.5"}]}), 9.5)


if __name__ == "__main__":
    unittest.main()
