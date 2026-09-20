import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harmony_agent.evals import (StateSample, brier_score, build_choice_question,
                                 dataset_digest, expected_calibration_error,
                                 fit_thresholds, load_dataset, rules_baseline,
                                 save_dataset, summarise, summarise_by_split)


def sample(state_id, split, label, candidates=2, tags=None):
    return StateSample(
        state_id=state_id, split=split, goal="打开页面",
        state_text=f"状态 {state_id}",
        candidates=[{"candidate_id": f"cand_{index}", "description": f"候选{index}"}
                    for index in range(candidates)],
        label=label, app="com.sina.weibo.stage", tags=tags or [])


class DatasetTests(unittest.TestCase):
    def test_round_trip_and_digest(self):
        samples = [sample("s1", "development", "cand_0"),
                   sample("s2", "calibration", "none")]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "states.jsonl"
            digest = save_dataset(path, samples)
            restored = load_dataset(path)
        self.assertEqual([item.state_id for item in restored], ["s1", "s2"])
        self.assertEqual(digest["splits"], {"development": 1, "calibration": 1})
        self.assertEqual(digest["total"], 2)
        self.assertEqual(len(dataset_digest(restored)["sha256"]), 64)

    def test_choice_question_includes_a_control_exit(self):
        question = build_choice_question(sample("s1", "development", "cand_0"))
        self.assertIn("cand_none_applicable", question["criteria"])
        self.assertEqual(question["type"], "choice")

    def test_choice_question_is_capped_at_sixteen_options(self):
        question = build_choice_question(sample("s1", "development", "cand_0", candidates=30))
        self.assertLessEqual(len(question["criteria"]), 16)
        self.assertIn("cand_none_applicable", question["criteria"])


class MetricsTests(unittest.TestCase):
    def rows(self, entries):
        return [{"state_id": f"s{i}", "split": split, "label": label,
                 "choice": choice, "confidence": confidence}
                for i, (split, label, choice, confidence) in enumerate(entries)]

    def test_coverage_and_error_denominator_are_reported(self):
        rows = self.rows([
            ("development", "cand_0", "cand_0", 0.9),
            ("development", "cand_1", "cand_0", 0.9),
            ("development", "cand_0", "cand_none_applicable", 0.9),
        ])
        summary = summarise(rows)
        self.assertEqual(summary["samples"], 3)
        self.assertEqual(summary["accepted"], 2)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["accepted_errors"], 1)
        self.assertEqual(summary["error_denominator"], 2)
        self.assertAlmostEqual(summary["coverage"], 2 / 3, places=4)

    def test_confidence_gate_reduces_coverage_and_errors(self):
        rows = self.rows([
            ("development", "cand_0", "cand_0", 0.9),
            ("development", "cand_1", "cand_0", 0.2),
        ])
        gated = summarise(rows, threshold=0.5)
        self.assertEqual(gated["accepted"], 1)
        self.assertEqual(gated["accepted_errors"], 0)
        self.assertEqual(gated["error_rate_among_accepted"], 0.0)

    def test_brier_and_ece_are_zero_for_perfect_calibration(self):
        pairs = [(1.0, True), (1.0, True)]
        self.assertEqual(brier_score(pairs), 0.0)
        self.assertEqual(expected_calibration_error(pairs), 0.0)
        pairs = [(1.0, False), (1.0, False)]
        self.assertGreater(brier_score(pairs), 0.9)

    def test_thresholds_are_fitted_on_the_calibration_split_only(self):
        calibration = self.rows([
            ("calibration", "cand_0", "cand_0", 0.9),
            ("calibration", "cand_0", "cand_0", 0.8),
            ("calibration", "cand_1", "cand_0", 0.3),
        ])
        fitted = fit_thresholds(calibration, max_error_rate=0.0, min_coverage=0.3)
        self.assertIsNotNone(fitted["confidence_gate"])
        self.assertGreaterEqual(fitted["confidence_gate"], 0.3)
        self.assertEqual(fitted["accepted_errors"], 0)

    def test_no_gate_reports_a_reason_instead_of_a_number(self):
        calibration = self.rows([("calibration", "cand_1", "cand_0", 0.99)])
        fitted = fit_thresholds(calibration, max_error_rate=0.0, min_coverage=0.5)
        self.assertIsNone(fitted["confidence_gate"])
        self.assertIn("reason", fitted)

    def test_summarise_by_split_keeps_groups_separate(self):
        rows = self.rows([("development", "cand_0", "cand_0", 0.9),
                          ("holdout", "cand_0", "cand_1", 0.9)])
        grouped = summarise_by_split(rows)
        self.assertEqual(grouped["development"]["accepted_errors"], 0)
        self.assertEqual(grouped["holdout"]["accepted_errors"], 1)

    def test_rules_baseline_only_answers_unambiguous_states(self):
        rows = rules_baseline([sample("s1", "development", "cand_0", candidates=1),
                               sample("s2", "development", "cand_1", candidates=3)])
        self.assertEqual(rows[0]["choice"], "cand_0")
        self.assertEqual(rows[1]["choice"], "cand_none_applicable")


if __name__ == "__main__":
    unittest.main()
