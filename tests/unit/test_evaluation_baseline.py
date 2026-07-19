import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import evaluation_baseline as baseline  # noqa: E402


SUITE_PATH = ROOT / "evaluation" / "fixtures" / "synthetic-suite.v1.json"
RESULT_PATH = ROOT / "evaluation" / "baselines" / "file-git-no-enhancement.v1.json"
REPORT_PATH = ROOT / "evaluation" / "baselines" / "file-git-no-enhancement.v1.md"
PRIVATE_EXAMPLE = ROOT / "evaluation" / "private-pilot-summary.example.json"


class EvaluationBaselineTests(unittest.TestCase):
    def suite(self):
        return json.loads(SUITE_PATH.read_text(encoding="utf-8"))

    def private_summary(self):
        return json.loads(PRIVATE_EXAMPLE.read_text(encoding="utf-8"))

    def test_committed_baseline_and_report_are_byte_reproducible(self):
        first = baseline._synthetic_result(SUITE_PATH)
        second = baseline._synthetic_result(SUITE_PATH)
        self.assertEqual(first, second)
        self.assertEqual(first["contract_sha256"], baseline._contract_sha256())
        self.assertEqual(baseline._json_bytes(first), RESULT_PATH.read_bytes())
        self.assertEqual(baseline._report_bytes(first), REPORT_PATH.read_bytes())
        self.assertNotIn(tempfile.gettempdir(), json.dumps(first))

    def test_incomplete_metric_contract_fails_closed(self):
        contract = json.loads(baseline.DEFAULT_CONTRACT.read_text(encoding="utf-8"))
        del contract["metrics"][0]["confounders"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(baseline.EvaluationError, "incomplete"):
                baseline._contract_sha256(path)

    def test_baseline_runs_real_file_git_query_and_provenance_paths(self):
        original_query = baseline.opc_memory.FileGitBackend.query
        calls = []

        def observed_query(backend, *args, **kwargs):
            calls.append((args, kwargs))
            return original_query(backend, *args, **kwargs)

        with patch.object(baseline.opc_memory.FileGitBackend, "query", observed_query):
            result = baseline._synthetic_result(SUITE_PATH)
        self.assertEqual(len(calls), 6)
        self.assertEqual(result["overall_safety_status"], "pass")
        self.assertEqual(
            result["safety_gates"]["provenance_probes"]["value"],
            {
                "probe-syn-stale-hash": "rejected",
                "probe-syn-stale-commit": "rejected",
            },
        )
        evidence = {item["case_id"]: item["file_git_hit_ids"] for item in result["file_git_evidence"]}
        self.assertEqual(
            evidence["case-syn-project-and-global"],
            ["exp-syn-global-alpha-rule", "exp-syn-project-alpha-rule"],
        )
        self.assertEqual(evidence["case-syn-stale-and-obsolete"], [])

    def test_scope_leakage_is_a_hard_failure(self):
        original_query = baseline.opc_memory.FileGitBackend.query

        def leaking_query(backend, *args, **kwargs):
            hits = original_query(backend, *args, **kwargs)
            if kwargs.get("project_id") == "project-syn-alpha" and args[0] == "alpha-marker":
                path = backend.root / "experiences" / "approved" / "exp-syn-project-beta-lookalike.json"
                hits.append(baseline.opc_memory.load_json(path))
            return hits

        with patch.object(baseline.opc_memory.FileGitBackend, "query", leaking_query):
            result = baseline._synthetic_result(SUITE_PATH)
        self.assertEqual(result["overall_safety_status"], "fail")
        self.assertEqual(result["safety_gates"]["scope_leakage_acceptances"]["status"], "fail")

    def test_obsolete_acceptance_is_a_hard_failure(self):
        original_query = baseline.opc_memory.FileGitBackend.query

        def stale_query(backend, *args, **kwargs):
            hits = original_query(backend, *args, **kwargs)
            if args[0] == "stale-marker":
                path = backend.root / "experiences" / "obsolete" / "exp-syn-obsolete-rule.json"
                hits.append(baseline.opc_memory.load_json(path))
            return hits

        with patch.object(baseline.opc_memory.FileGitBackend, "query", stale_query):
            result = baseline._synthetic_result(SUITE_PATH)
        self.assertEqual(result["overall_safety_status"], "fail")
        self.assertEqual(result["safety_gates"]["stale_obsolete_acceptances"]["status"], "fail")

    def test_missing_fields_and_zero_denominators_fail_closed(self):
        suite = self.suite()
        del suite["cases"][0]["observed"]["known_defects"]
        with self.assertRaisesRegex(baseline.EvaluationError, "missing fields"):
            baseline._score_synthetic(suite, "0" * 64)
        suite = self.suite()
        for case in suite["cases"]:
            case["observed"]["known_defects"] = 0
            case["observed"]["qa_caught_defects"] = 0
        with self.assertRaisesRegex(baseline.EvaluationError, "denominator"):
            baseline._score_synthetic(suite, "0" * 64)

    def test_unknown_file_git_hit_fails_closed(self):
        original_query = baseline.opc_memory.FileGitBackend.query

        def unknown_query(backend, *args, **kwargs):
            hits = original_query(backend, *args, **kwargs)
            hits.append({"id": "exp-syn-not-in-fixture"})
            return hits

        with patch.object(baseline.opc_memory.FileGitBackend, "query", unknown_query):
            with self.assertRaisesRegex(baseline.EvaluationError, "unknown File/Git hit"):
                baseline._synthetic_result(SUITE_PATH)

    def test_private_summary_accepts_only_strict_aggregate(self):
        result = baseline._score_private_summary(self.private_summary())
        self.assertEqual(result["mode"], "private-aggregate")
        self.assertIsNone(result["source_sha256"])
        self.assertEqual(result["file_git_evidence"], [])
        self.assertEqual(result["safety_gates"]["provenance_probes"]["status"], "not_applicable")
        self.assertEqual(result["overall_safety_status"], "pass")

        for forbidden in ("project_name", "tasks", "raw_text", "artifact_path"):
            value = self.private_summary()
            value[forbidden] = "not allowed"
            with self.assertRaisesRegex(baseline.EvaluationError, "unsupported fields"):
                baseline._score_private_summary(value)

    def test_private_summary_rejects_semantic_ids_and_task_count_outside_three_to_five(self):
        value = self.private_summary()
        value["pilot_id"] = "pilot-project-name"
        with self.assertRaisesRegex(baseline.EvaluationError, "12 hexadecimal"):
            baseline._score_private_summary(value)
        for count in (2, 6):
            value = self.private_summary()
            value["task_count"] = count
            with self.assertRaises(baseline.EvaluationError):
                baseline._score_private_summary(value)

    def test_private_summary_rejects_zero_or_inconsistent_denominators(self):
        for denominator in (
            "eligible_manager_decisions",
            "known_defects",
            "valid_reuse_opportunities",
            "accepted_recalls",
        ):
            value = self.private_summary()
            value["counts"][denominator] = 0
            with self.assertRaisesRegex(baseline.EvaluationError, "cannot be zero"):
                baseline._score_private_summary(value)
        value = self.private_summary()
        value["counts"]["accepted_recalls"] = 5
        with self.assertRaisesRegex(baseline.EvaluationError, "accepted recalls"):
            baseline._score_private_summary(value)

    def test_json_input_and_output_reject_non_finite_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.json"
            for token in ("NaN", "Infinity", "-Infinity", "1e999"):
                with self.subTest(token=token):
                    path.write_text(f'{{"value": {token}}}', encoding="utf-8")
                    with self.assertRaisesRegex(baseline.EvaluationError, "non-finite"):
                        baseline._load_object(path)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(baseline.EvaluationError):
                    baseline._number(value, "value")
                with self.assertRaisesRegex(baseline.EvaluationError, "strict JSON"):
                    baseline._json_bytes({"value": value})

    def test_private_summary_rejects_impossible_aggregate_distributions(self):
        invalid_cases = (
            (
                "context_tokens",
                {"total": 3, "median": 999999, "p95_nearest_rank": 999999},
            ),
            ("context_tokens", {"total": 100, "median": 40, "p95_nearest_rank": 60}),
            (
                "context_tokens",
                {"total": 4200, "median": 1350, "p95_nearest_rank": 1600.5},
            ),
            (
                "context_tokens",
                {"total": 4200, "median": 1350.5, "p95_nearest_rank": 1600},
            ),
            (
                "latency_ms",
                {"total": 1, "median": 999999, "p95_nearest_rank": 999999},
            ),
            ("latency_ms", {"total": 138, "median": 53, "p95_nearest_rank": 52}),
        )
        for field, distribution in invalid_cases:
            with self.subTest(field=field, distribution=distribution):
                value = self.private_summary()
                value[field] = distribution
                with self.assertRaises(baseline.EvaluationError):
                    baseline._score_private_summary(value)

    def test_private_summary_accepts_feasible_even_task_aggregates(self):
        value = self.private_summary()
        value["task_count"] = 4
        value["context_tokens"] = {
            "total": 100,
            "median": 25,
            "p95_nearest_rank": 40,
        }
        value["latency_ms"] = {
            "total": 10,
            "median": 2.5,
            "p95_nearest_rank": 4,
        }
        result = baseline._score_private_summary(value)
        self.assertEqual(result["task_count"], 4)
        self.assertEqual(result["metrics"]["context_tokens_per_task"]["mean"], 25)

        value["context_tokens"]["total"] = 200
        with self.assertRaisesRegex(baseline.EvaluationError, "cannot describe"):
            baseline._score_private_summary(value)

    def test_private_summary_accepts_feasible_five_task_aggregates(self):
        value = self.private_summary()
        value["task_count"] = 5
        value["context_tokens"] = {
            "total": 150,
            "median": 30,
            "p95_nearest_rank": 50,
        }
        value["latency_ms"] = {
            "total": 15,
            "median": 3,
            "p95_nearest_rank": 5,
        }
        result = baseline._score_private_summary(value)
        self.assertEqual(result["task_count"], 5)

        value["context_tokens"]["total"] = 100
        with self.assertRaisesRegex(baseline.EvaluationError, "cannot describe"):
            baseline._score_private_summary(value)

    def test_private_safety_gate_failure_is_not_reported_as_pass(self):
        value = self.private_summary()
        value["counts"]["false_recall_acceptances"] = 2
        value["counts"]["valid_reuses"] = 2
        value["counts"]["scope_leakage_acceptances"] = 1
        result = baseline._score_private_summary(value)
        self.assertEqual(result["overall_safety_status"], "fail")

    def test_all_versioned_json_artifacts_are_valid_json(self):
        for path in sorted((ROOT / "evaluation").rglob("*.json")):
            with self.subTest(path=path):
                self.assertIsInstance(baseline._load_object(path), dict)

    def test_cli_verify_and_private_summary(self):
        verify = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "evaluation_baseline.py"), "verify"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn("EVALUATION_BASELINE_OK", verify.stdout)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            report = Path(temporary) / "report.md"
            private = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "evaluation_baseline.py"),
                    "private-summary",
                    "--summary",
                    str(PRIVATE_EXAMPLE),
                    "--output",
                    str(output),
                    "--report",
                    str(report),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(private.returncode, 0, private.stderr)
            self.assertTrue(output.is_file())
            self.assertEqual(report.read_bytes(), baseline._report_bytes(json.loads(output.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main()
