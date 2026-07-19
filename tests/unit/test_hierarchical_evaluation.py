from __future__ import annotations

import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "hierarchical_evaluation", ROOT / "scripts" / "hierarchical_evaluation.py"
)
assert SPEC and SPEC.loader
hierarchical_evaluation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hierarchical_evaluation
SPEC.loader.exec_module(hierarchical_evaluation)


class HierarchicalEvaluationTests(unittest.TestCase):
    def test_committed_result_meets_safety_and_superiority_rule(self) -> None:
        result = json.loads(hierarchical_evaluation.RESULT.read_text(encoding="utf-8"))
        self.assertEqual(result["comparison_status"], "superior")
        self.assertEqual(result["aggregate"]["safety"]["scope_leakage_acceptances"], 0)
        self.assertEqual(result["aggregate"]["safety"]["stale_obsolete_acceptances"], 0)
        flat = result["aggregate"]["flat"]
        treatment = result["aggregate"]["hierarchical"]
        self.assertGreaterEqual(treatment["support_precision_at_5"], flat["support_precision_at_5"])
        self.assertLessEqual(
            treatment["injected_tokens_median"],
            flat["injected_tokens_median"] * 0.8,
        )

    def test_not_superior_status_is_mandatory_when_threshold_is_not_met(self) -> None:
        status, rule = hierarchical_evaluation._superiority(
            {
                "flat": {"support_precision_at_5": 0.8, "injected_tokens_median": 100},
                "hierarchical": {"support_precision_at_5": 0.81, "injected_tokens_median": 99},
            }
        )
        self.assertEqual(status, "not_superior")
        self.assertEqual(rule, "threshold_not_met")

    def test_latency_rejects_non_finite_and_impossible_aggregate(self) -> None:
        value = json.loads(hierarchical_evaluation.LATENCY.read_text(encoding="utf-8"))
        invalid = json.loads(json.dumps(value))
        invalid["flat_ms"]["samples"][0] = math.inf
        with self.assertRaises(hierarchical_evaluation.EvaluationError):
            hierarchical_evaluation.validate_latency(invalid)
        invalid = json.loads(json.dumps(value))
        invalid["flat_ms"]["p95_nearest_rank"] = 0.000001
        with self.assertRaises(hierarchical_evaluation.EvaluationError):
            hierarchical_evaluation.validate_latency(invalid)

    def test_strict_json_loader_rejects_nan_and_extra_fixture_fields(self) -> None:
        with self.assertRaises(hierarchical_evaluation.EvaluationError):
            hierarchical_evaluation._reject_constant("NaN")
        fixture = json.loads(hierarchical_evaluation.FIXTURE.read_text(encoding="utf-8"))
        fixture["unexpected"] = True
        with self.assertRaises(hierarchical_evaluation.EvaluationError):
            hierarchical_evaluation._validate_fixture(fixture)


if __name__ == "__main__":
    unittest.main()
