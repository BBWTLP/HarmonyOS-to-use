"""T10–T11 offline unit tests."""
import unittest

from harmony_runtime.ocr import detect_engine, ocr_boxes
from harmony_agent.experience_retrieval import experience_matches, retrieve


class OcrAdapterTests(unittest.TestCase):
    def test_no_engine_yields_empty_boxes(self):
        # In this environment there is typically no local OCR engine; the
        # adapter must not invent detections.
        if detect_engine() is None:
            self.assertEqual(ocr_boxes(b"not-an-image"), [])

    def test_detect_engine_is_none_or_string(self):
        engine = detect_engine()
        self.assertTrue(engine is None or isinstance(engine, str))


class ExperienceRetrievalTests(unittest.TestCase):
    def entry(self, **overrides):
        base = {
            "experience_id": "exp1",
            "scope": {"app": "com.sina.weibo.stage", "build": "1.0"},
            "trigger": {"goal_pattern": "search harmony"},
            "outcome": "verified",
            "expires_at": 2000000000.0,
        }
        base.update(overrides)
        return base

    def test_filters_by_app_build_goal_and_expiry(self):
        entries = [
            self.entry(),
            self.entry(experience_id="exp2", outcome="failed"),
            self.entry(experience_id="exp3", scope={"app": "com.other", "build": "1.0"}),
            self.entry(experience_id="exp4", expires_at=1.0),
        ]
        hits = retrieve(entries, app="com.sina.weibo.stage", build="1.0",
                        goal_pattern="search", now=100.0)
        self.assertEqual([h["experience_id"] for h in hits], ["exp1"])
        self.assertTrue(experience_matches(self.entry(), app="com.sina.weibo.stage",
                                           goal_pattern="harmony", now=100.0))
        self.assertFalse(experience_matches(self.entry(), app="com.sina.weibo.stage",
                                            goal_pattern="pay", now=100.0))

    def test_no_match_is_valid(self):
        self.assertEqual(retrieve([], app="com.x"), [])


if __name__ == "__main__":
    unittest.main()
