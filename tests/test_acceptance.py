"""Acceptance must not accept wrong topics or publish transport exception data."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import accept_weibo


class AcceptanceTests(unittest.TestCase):
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

    def test_progress_profile_accepts_harmony_progress_nodes(self):
        self.assertEqual(accept_weibo.video_progress({"catalog": [{"type": "Progress", "text": "9.5"}]}), 9.5)


if __name__ == "__main__":
    unittest.main()
