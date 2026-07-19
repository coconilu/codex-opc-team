from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import opc_shadow as shadow


def strict_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


class ShadowEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.knowledge = self.root / "knowledge"
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.project_id = "project-alpha"
        self.candidate_id = "exp-shadow-candidate"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.knowledge), *args],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def make_candidate(
        self,
        *,
        status: str = "candidate",
        scope: str = "project",
        project_id: str | None = None,
    ) -> tuple[dict, str, str]:
        self.knowledge.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "user.name", "Synthetic Fixture")
        folder = {
            "candidate": "experiences/candidates",
            "approved": "experiences/approved",
            "rejected": "experiences/rejected",
            "obsolete": "experiences/obsolete",
        }[status]
        path = self.knowledge / folder / f"{self.candidate_id}.json"
        path.parent.mkdir(parents=True)
        record = {
            "schema_version": 1,
            "id": self.candidate_id,
            "type": "decision",
            "summary": "Synthetic candidate",
            "content": "Use the synthetic verification gate.",
            "keywords": ["synthetic"],
            "metadata": {},
            "scope": scope,
            "owner": "opc-team",
            "evidence": {},
            "confidence": 0.9,
            "status": status,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        if scope == "project":
            record["project_id"] = project_id or self.project_id
        if status == "obsolete":
            record["obsolete_at"] = "2026-01-02T00:00:00Z"
            record["obsolete_reason"] = "synthetic supersession"
        path.write_bytes(strict_bytes(record))
        self.git("add", ".")
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
            }
        )
        subprocess.run(
            ["git", "-C", str(self.knowledge), "commit", "-q", "-m", "synthetic candidate"],
            check=True,
            env=env,
        )
        commit = self.git("rev-parse", "HEAD")
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        return record, path.relative_to(self.knowledge).as_posix(), commit + ":" + content_hash

    @staticmethod
    def metrics(kind: str) -> dict:
        base = {
            "manager_intervention_rate": {"numerator": 1, "denominator": 2},
            "qa_catch_rate": {"numerator": 1, "denominator": 2},
            "rework_loops_per_task": {"numerator": 1, "denominator": 1},
            "valid_knowledge_reuse_rate": {"numerator": 1, "denominator": 2},
            "false_recall_rate": {"numerator": 1, "denominator": 2},
            "scope_leakage_acceptances": 0,
            "stale_obsolete_acceptances": 0,
            "context_tokens_per_task": 100,
            "latency_ms": 100,
        }
        if kind == "beneficial":
            base.update(
                {
                    "manager_intervention_rate": {"numerator": 0, "denominator": 2},
                    "qa_catch_rate": {"numerator": 2, "denominator": 2},
                    "rework_loops_per_task": {"numerator": 0, "denominator": 1},
                    "valid_knowledge_reuse_rate": {"numerator": 2, "denominator": 2},
                    "false_recall_rate": {"numerator": 0, "denominator": 2},
                    "context_tokens_per_task": 80,
                    "latency_ms": 80,
                }
            )
        elif kind == "harmful":
            base.update(
                {
                    "manager_intervention_rate": {"numerator": 2, "denominator": 2},
                    "qa_catch_rate": {"numerator": 0, "denominator": 2},
                    "rework_loops_per_task": {"numerator": 2, "denominator": 1},
                    "valid_knowledge_reuse_rate": {"numerator": 0, "denominator": 2},
                    "false_recall_rate": {"numerator": 2, "denominator": 2},
                    "scope_leakage_acceptances": 1,
                    "context_tokens_per_task": 120,
                    "latency_ms": 120,
                }
            )
        elif kind == "conflicting":
            base.update(
                {
                    "manager_intervention_rate": {"numerator": 0, "denominator": 2},
                    "qa_catch_rate": {"numerator": 0, "denominator": 2},
                }
            )
        return base

    def replay(
        self,
        source_path: str,
        provenance: str,
        *,
        treatment: str = "beneficial",
        project_id: str | None = None,
        execution_status: str = "completed",
        dataset_kind: str = "synthetic",
    ) -> tuple[dict, bytes]:
        commit, content_hash = provenance.split(":")
        value = {
            "schema_version": shadow.REPLAY_VERSION,
            "contract_version": shadow.CONTRACT_VERSION,
            "evaluation_id": "shadow-synthetic-01",
            "dataset": {
                "kind": dataset_kind,
                "dataset_id": "synthetic-suite-01",
                "project_id": project_id or self.project_id,
                "approval_ref": "approvals/shadow-pilot-v1" if dataset_kind != "synthetic" else None,
            },
            "candidate": {
                "candidate_id": self.candidate_id,
                "source_path": source_path,
                "source_commit": commit,
                "content_sha256": content_hash,
            },
            "dependency": {
                "engine": "synthetic-replay",
                "version": "v1",
                "determinism": "deterministic",
                "seed": "fixed-01",
            },
            "cases": [
                {
                    "case_id": "case-01",
                    "control": {
                        "candidate_applied": False,
                        "execution_status": "completed",
                        "failure_code": None,
                        "metrics": self.metrics("neutral"),
                    },
                    "treatment": {
                        "candidate_applied": True,
                        "execution_status": execution_status,
                        "failure_code": None if execution_status == "completed" else "synthetic-provider-failure",
                        "metrics": self.metrics(treatment),
                    },
                }
            ],
        }
        return value, strict_bytes(value)

    def run_result(self, *, treatment: str = "beneficial", execution_status: str = "completed") -> dict:
        _, source, provenance = self.make_candidate()
        replay, raw = self.replay(source, provenance, treatment=treatment, execution_status=execution_status)
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        return shadow.evaluate(
            self.knowledge,
            replay,
            raw,
            expected_preview_sha256=preview["preview_sha256"],
        )

    def run_safety_conflict_result(self) -> dict:
        _, source, provenance = self.make_candidate()
        replay, raw = self.replay(source, provenance, treatment="neutral")
        control = replay["cases"][0]["control"]["metrics"]
        treatment = replay["cases"][0]["treatment"]["metrics"]
        control["scope_leakage_acceptances"] = 1
        treatment["scope_leakage_acceptances"] = 0
        control["stale_obsolete_acceptances"] = 0
        treatment["stale_obsolete_acceptances"] = 1
        raw = strict_bytes(replay)
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        return shadow.evaluate(
            self.knowledge,
            replay,
            raw,
            expected_preview_sha256=preview["preview_sha256"],
        )

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "opc_shadow.py"), *args],
            check=False,
            text=True,
            capture_output=True,
        )

    def make_directory_alias(self, target: Path, alias: Path) -> None:
        try:
            os.symlink(target, alias, target_is_directory=True)
            return
        except OSError:
            if os.name != "nt":
                self.skipTest("directory symlinks are not permitted")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            check=False,
            text=True,
            capture_output=True,
        )
        if created.returncode != 0:
            self.skipTest("Windows directory junctions are unavailable")

    def test_beneficial_candidate_is_only_recommended_for_separate_curation(self) -> None:
        result = self.run_result(treatment="beneficial")
        self.assertEqual(result["status"], "conclusive")
        self.assertEqual(result["recommendation"], "consider_for_separate_curation")
        self.assertFalse(result["governance"]["automatic_promotion"])
        self.assertFalse(result["confidence"]["approval_permission"])

    def test_neutral_candidate_is_not_positive(self) -> None:
        result = self.run_result(treatment="neutral")
        self.assertEqual(result["recommendation"], "do_not_promote_on_shadow_evidence")

    def test_harmful_candidate_preserves_safety_counterevidence(self) -> None:
        result = self.run_result(treatment="harmful")
        self.assertEqual(result["status"], "conclusive")
        self.assertEqual(result["recommendation"], "do_not_promote_on_shadow_evidence")
        self.assertTrue(any(item["metric_id"] == "scope_leakage_acceptances" for item in result["evidence"]["counterevidence"]))
        self.assertFalse(
            any(
                item["code"] == "conflicting_measured_results"
                for item in result["failure_modes"]
            )
        )

    def test_conflicting_candidate_is_inconclusive(self) -> None:
        result = self.run_result(treatment="conflicting")
        self.assertEqual(result["status"], "inconclusive")
        self.assertTrue(any(item["code"] == "conflicting_measured_results" for item in result["failure_modes"]))

    def test_safety_support_and_counterevidence_are_inconclusive_conflict(self) -> None:
        result = self.run_safety_conflict_result()
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["recommendation"], "inconclusive")
        directions = {
            item["metric_id"]: item["direction"]
            for item in result["measurements"]["comparisons"]
            if item["metric_id"] in shadow.SAFETY_METRICS
        }
        self.assertEqual(
            {
                "scope_leakage_acceptances": "supporting",
                "stale_obsolete_acceptances": "counterevidence",
            },
            directions,
        )
        self.assertEqual(
            1,
            sum(
                item["code"] == "conflicting_measured_results"
                for item in result["failure_modes"]
            ),
        )

    def test_over_scoped_cross_project_candidate_is_rejected_before_measurement(self) -> None:
        _, source, provenance = self.make_candidate(project_id="project-alpha")
        replay, raw = self.replay(source, provenance, project_id="project-beta")
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        result = shadow.evaluate(self.knowledge, replay, raw, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual(result["status"], "rejected_preflight")
        self.assertIsNone(result["measurements"])
        self.assertIn("cross_project_scope", result["preflight"]["reasons"])

    def test_obsolete_candidate_is_rejected_before_measurement(self) -> None:
        _, source, provenance = self.make_candidate(status="obsolete")
        replay, raw = self.replay(source, provenance)
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        result = shadow.evaluate(self.knowledge, replay, raw, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual(result["status"], "rejected_preflight")
        self.assertIn("obsolete_or_non_candidate", result["preflight"]["reasons"])

    def test_stale_candidate_is_rejected_before_measurement(self) -> None:
        _, source, provenance = self.make_candidate()
        candidate_path = self.knowledge / source
        candidate_path.write_text(candidate_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        replay, raw = self.replay(source, provenance)
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        self.assertIn("stale_provenance", preview["preflight"]["reasons"])

    def test_timeout_and_provider_failure_are_degraded_inconclusive(self) -> None:
        for status in ("timeout", "provider_unavailable", "provider_error"):
            with self.subTest(status=status):
                self.tearDown()
                self.setUp()
                result = self.run_result(execution_status=status)
                self.assertEqual(result["status"], "inconclusive")
                self.assertEqual(result["degradation_status"], "degraded")
                self.assertEqual(result["recommendation"], "inconclusive")
                self.assertTrue(any(item["code"] == status for item in result["failure_modes"]))

    def test_preview_is_zero_write_and_exact_fingerprint_is_required(self) -> None:
        _, source, provenance = self.make_candidate()
        before_status = self.git("status", "--porcelain")
        before_head = self.git("rev-parse", "HEAD")
        replay, raw = self.replay(source, provenance)
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        self.assertEqual(preview["planned_writes"], [])
        self.assertEqual(before_status, self.git("status", "--porcelain"))
        self.assertEqual(before_head, self.git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(shadow.ShadowError, "preview fingerprint changed"):
            shadow.evaluate(self.knowledge, replay, raw, expected_preview_sha256="0" * 64)

    def test_candidate_change_during_evaluation_fails_without_artifact(self) -> None:
        _, source, provenance = self.make_candidate()
        replay, raw = self.replay(source, provenance)
        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        original = shadow._preflight
        calls = 0

        def changed(*args, **kwargs):
            nonlocal calls
            calls += 1
            value = original(*args, **kwargs)
            if calls >= 2:
                value = dict(value)
                value["reasons"] = ["stale_provenance"]
                value["passed"] = False
            return value

        with mock.patch.object(shadow, "_preflight", side_effect=changed):
            with self.assertRaisesRegex(shadow.ShadowError, "changed during evaluation"):
                shadow.evaluate(self.knowledge, replay, raw, expected_preview_sha256=preview["preview_sha256"])
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_private_feedback_keeps_measured_and_model_inference_distinct(self) -> None:
        _, source, provenance = self.make_candidate()
        project = self.root / "private-project"
        (project / ".opc" / "feedback").mkdir(parents=True)
        (project / ".opc" / "project.json").write_text(json.dumps({"project_id": self.project_id}), encoding="utf-8")
        (project / ".opc" / "run.json").write_text(json.dumps({"project_id": self.project_id, "run_id": "opc-run-alpha"}), encoding="utf-8")
        refs = {
            "project_id": self.project_id,
            "run_id": "opc-run-alpha",
            "candidate_ids": [self.candidate_id],
            "metric_refs": [],
            "artifact_refs": [],
        }
        events = [
            {
                "event_id": "feedback-outcome",
                "recorded_at": "2026-01-01T00:00:00Z",
                "category": "confirmed_outcome",
                "epistemic_status": "confirmed_outcome",
                "summary": "Synthetic outcome passed.",
                "outcome_status": "pass",
                "manager_judgment": "not_applicable",
                "qa_status": "not_applicable",
                "references": refs,
            },
            {
                "event_id": "feedback-hypothesis",
                "recorded_at": "2026-01-01T00:01:00Z",
                "category": "hypothesis",
                "epistemic_status": "hypothesis",
                "summary": "Synthetic model hypothesis.",
                "outcome_status": "not_applicable",
                "manager_judgment": "not_applicable",
                "qa_status": "not_applicable",
                "references": refs,
            },
        ]
        record = {
            "schema_version": "opc-structured-feedback-v1",
            "contract_version": "opc-structured-feedback-contract-v1",
            "project_ref": self.project_id,
            "run_ref": "opc-run-alpha",
            "revision": 2,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:01:00Z",
            "events": events,
        }
        (project / ".opc" / "feedback" / "opc-run-alpha.json").write_bytes(strict_bytes(record))
        replay, raw = self.replay(source, provenance, dataset_kind="approved_private_pilot")
        preview, _ = shadow.build_preview(self.knowledge, replay, raw, project_root=project)
        result = shadow.evaluate(
            self.knowledge,
            replay,
            raw,
            expected_preview_sha256=preview["preview_sha256"],
            project_root=project,
        )
        kinds = {item["source_kind"] for bucket in result["evidence"].values() for item in bucket}
        self.assertIn("measured", kinds)
        self.assertIn("model_inference", kinds)
        self.assertNotIn("summary", json.dumps(result))

    def test_artifacts_are_immutable_and_outside_all_authoritative_roots(self) -> None:
        result = self.run_result()
        result_path, report_path = shadow._publish_artifacts(self.artifacts, "shadow-synthetic-01", result)
        self.assertTrue(result_path.is_file())
        self.assertTrue(report_path.is_file())
        self.assertEqual(1, result_path.lstat().st_nlink)
        self.assertEqual(1, report_path.lstat().st_nlink)
        with self.assertRaises(shadow.ShadowError):
            shadow._publish_artifacts(self.artifacts, "shadow-synthetic-01", result)
        with self.assertRaises(shadow.ShadowError):
            shadow._assert_artifact_root(self.knowledge / "derived", knowledge_root=self.knowledge, project_root=None)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_artifact_symlink_boundary_fails_closed(self) -> None:
        target = self.root / "target"
        target.mkdir()
        linked = self.root / "linked"
        try:
            os.symlink(target, linked, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation is not permitted")
        result = self.run_result()
        with self.assertRaises(shadow.ShadowError):
            shadow._publish_artifacts(linked, "shadow-synthetic-01", result)

    def test_replay_size_and_credentials_fail_without_echo(self) -> None:
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"{" + b" " * shadow.MAX_REPLAY_BYTES + b"}")
        with self.assertRaisesRegex(shadow.ShadowError, "size limit"):
            shadow._read_json(oversized, maximum=shadow.MAX_REPLAY_BYTES, label="replay")
        credential = self.root / "credential.json"
        credential.write_text(
            json.dumps({"token": "gh" + "p_" + "1" * 36}), encoding="utf-8"
        )
        with self.assertRaises(shadow.ShadowError) as raised:
            shadow._read_json(credential, maximum=shadow.MAX_REPLAY_BYTES, label="replay")
        self.assertNotIn("gh" + "p_", str(raised.exception))

    def test_hard_linked_replay_is_rejected(self) -> None:
        source = self.root / "source.json"
        linked = self.root / "linked.json"
        source.write_text("{}", encoding="utf-8")
        try:
            os.link(source, linked)
        except OSError:
            self.skipTest("hard link creation is not permitted")
        with self.assertRaisesRegex(shadow.ShadowError, "regular non-linked"):
            shadow._read_json(linked, maximum=shadow.MAX_REPLAY_BYTES, label="replay")

    def test_numeric_limits_accept_maximum_and_reject_larger_or_extreme_integers(self) -> None:
        _, source, provenance = self.make_candidate()
        replay, raw = self.replay(source, provenance)
        for arm in ("control", "treatment"):
            metrics = replay["cases"][0][arm]["metrics"]
            for metric in shadow.QUALITY_METRICS:
                metrics[metric] = {
                    "numerator": shadow.MAX_RATIO_COMPONENT,
                    "denominator": shadow.MAX_RATIO_COMPONENT,
                }
            for metric in shadow.SAFETY_METRICS:
                metrics[metric] = shadow.MAX_SAFETY_COUNT
            metrics["context_tokens_per_task"] = shadow.MAX_CONTEXT_TOKENS
            metrics["latency_ms"] = shadow.MAX_LATENCY_MS
        shadow.validate_replay(replay)
        preview, _ = shadow.build_preview(self.knowledge, replay, strict_bytes(replay))
        result = shadow.evaluate(
            self.knowledge,
            replay,
            strict_bytes(replay),
            expected_preview_sha256=preview["preview_sha256"],
        )
        self.assertEqual(
            result["measurements"]["control"]["context_tokens_per_task"]["total"],
            shadow.MAX_CONTEXT_TOKENS,
        )

        mutations = [
            ("ratio", lambda value: value["cases"][0]["control"]["metrics"]["qa_catch_rate"].update(numerator=shadow.MAX_RATIO_COMPONENT + 1)),
            ("safety", lambda value: value["cases"][0]["control"]["metrics"].update(scope_leakage_acceptances=shadow.MAX_SAFETY_COUNT + 1)),
            ("tokens", lambda value: value["cases"][0]["control"]["metrics"].update(context_tokens_per_task=shadow.MAX_CONTEXT_TOKENS + 1)),
            ("latency", lambda value: value["cases"][0]["control"]["metrics"].update(latency_ms=shadow.MAX_LATENCY_MS + 1)),
            ("extreme", lambda value: value["cases"][0]["control"]["metrics"].update(context_tokens_per_task=10**400)),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                invalid = copy.deepcopy(replay)
                mutate(invalid)
                with self.assertRaisesRegex(shadow.ShadowError, "v1 bounds"):
                    shadow.validate_replay(invalid)

        with mock.patch.object(shadow, "MAX_AGGREGATE_RATIO_COMPONENT", 1):
            with self.assertRaisesRegex(shadow.ShadowError, "aggregate exceeds"):
                shadow._aggregate_arm(replay["cases"], "control")

        extreme = copy.deepcopy(replay)
        extreme["cases"][0]["control"]["metrics"]["context_tokens_per_task"] = 10**400
        replay_path = self.root / "extreme-replay.json"
        replay_path.write_bytes(strict_bytes(extreme))
        process = self.run_cli(
            "preview",
            "--knowledge-root",
            str(self.knowledge),
            "--replay",
            str(replay_path),
        )
        self.assertEqual(2, process.returncode)
        self.assertIn("OPC_SHADOW_ERROR:", process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") is not None,
        "jsonschema is optional in the dependency-free core job",
    )
    def test_replay_schema_numeric_limits_match_runtime(self) -> None:
        from jsonschema import Draft202012Validator

        _, source, provenance = self.make_candidate()
        replay, _ = self.replay(source, provenance)
        schema = json.loads(
            (ROOT / "evaluation" / "schemas" / "shadow-replay.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(replay))
        mutations = (
            lambda value: value["cases"][0]["control"]["metrics"]["qa_catch_rate"].update(numerator=shadow.MAX_RATIO_COMPONENT + 1),
            lambda value: value["cases"][0]["control"]["metrics"].update(scope_leakage_acceptances=shadow.MAX_SAFETY_COUNT + 1),
            lambda value: value["cases"][0]["control"]["metrics"].update(context_tokens_per_task=shadow.MAX_CONTEXT_TOKENS + 1),
            lambda value: value["cases"][0]["control"]["metrics"].update(latency_ms=shadow.MAX_LATENCY_MS + 1),
            lambda value: value["cases"][0]["control"]["metrics"].update(context_tokens_per_task=10**400),
        )
        for mutate in mutations:
            invalid = copy.deepcopy(replay)
            mutate(invalid)
            self.assertFalse(validator.is_valid(invalid))
            with self.assertRaises(shadow.ShadowError):
                shadow.validate_replay(invalid)

    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") is not None,
        "jsonschema is optional in the dependency-free core job",
    )
    def test_result_schema_and_renderer_reject_the_same_governance_corruption(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "evaluation" / "schemas" / "shadow-result.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        valid = self.run_result()
        self.assertTrue(validator.is_valid(valid))
        mutations = {
            "empty_dataset": lambda value: value.update(dataset={}),
            "empty_candidate": lambda value: value.update(candidate={}),
            "empty_preflight": lambda value: value.update(preflight={}),
            "governance_extra": lambda value: value["governance"].update(unexpected=False),
            "confidence_extra": lambda value: value["confidence"].update(unexpected=0),
            "evidence_extra": lambda value: value["evidence"].update(unexpected=[]),
            "write_permission": lambda value: value["governance"].update(git_written=True),
            "forged_contract": lambda value: value.update(contract_sha256="0" * 64),
            "positive_failure": lambda value: value["failure_modes"].append(
                {"arm": "comparison", "case_id": "aggregate", "code": "forged", "failure_ref": "quality"}
            ),
            "positive_without_measured_support": lambda value: value["evidence"].update(
                support=[]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                corrupted = copy.deepcopy(valid)
                mutate(corrupted)
                self.assertFalse(validator.is_valid(corrupted))
                with self.assertRaises(shadow.ShadowError):
                    shadow.render_report(corrupted)

    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") is not None,
        "jsonschema is optional in the dependency-free core job",
    )
    def test_conflicting_result_schema_runtime_and_renderer_are_consistent(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "evaluation" / "schemas" / "shadow-result.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        result = self.run_result(treatment="conflicting")
        self.assertTrue(validator.is_valid(result))
        shadow.validate_result(result)
        self.assertIn("conflicting_measured_results", shadow.render_report(result))
        conflict_failure = next(
            item
            for item in result["failure_modes"]
            if item["code"] == "conflicting_measured_results"
        )
        mutations = {
            "missing_conflict_failure": lambda value: value.update(failure_modes=[]),
            "duplicate_conflict_failure": lambda value: value["failure_modes"].append(
                copy.deepcopy(conflict_failure)
            ),
            "forged_conclusive_harmful": lambda value: value.update(
                status="conclusive",
                recommendation="do_not_promote_on_shadow_evidence",
                failure_modes=[],
            ),
            "forged_conclusive_positive": lambda value: value.update(
                status="conclusive",
                recommendation="consider_for_separate_curation",
                failure_modes=[],
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                corrupted = copy.deepcopy(result)
                mutate(corrupted)
                self.assertFalse(validator.is_valid(corrupted))
                with self.assertRaisesRegex(shadow.ShadowError, "conflict|positive"):
                    shadow.validate_result(corrupted)
                with self.assertRaises(shadow.ShadowError):
                    shadow.render_report(corrupted)

    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") is not None,
        "jsonschema is optional in the dependency-free core job",
    )
    def test_safety_conflict_and_counter_only_result_schema_distinction(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "evaluation" / "schemas" / "shadow-result.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        safety_conflict = self.run_safety_conflict_result()
        self.assertTrue(validator.is_valid(safety_conflict))
        shadow.validate_result(safety_conflict)

        self.tearDown()
        self.setUp()
        counter_only = self.run_result(treatment="harmful")
        self.assertTrue(validator.is_valid(counter_only))
        shadow.validate_result(counter_only)
        self.assertEqual(counter_only["status"], "conclusive")
        self.assertEqual(
            counter_only["recommendation"], "do_not_promote_on_shadow_evidence"
        )

    def test_renderer_recomputes_measurement_direction_and_confidence(self) -> None:
        result = self.run_result()
        corrupted = copy.deepcopy(result)
        metric = corrupted["measurements"]["comparisons"][0]
        old_direction = metric["direction"]
        metric["direction"] = "neutral" if old_direction == "supporting" else "supporting"
        for bucket in corrupted["evidence"].values():
            for item in bucket:
                if item.get("metric_id") == metric["metric_id"]:
                    item["direction"] = metric["direction"]
        with self.assertRaises(shadow.ShadowError):
            shadow.render_report(corrupted)
        corrupted = copy.deepcopy(result)
        corrupted["confidence"]["evaluated_confidence"] = 1
        with self.assertRaisesRegex(shadow.ShadowError, "confidence"):
            shadow.render_report(corrupted)

    def test_cli_rejects_linked_replay_and_result_ancestors_without_traceback(self) -> None:
        _, source, provenance = self.make_candidate()
        replay, _ = self.replay(source, provenance)
        target = self.root / "linked-target"
        target.mkdir()
        replay_path = target / "replay.json"
        replay_path.write_bytes(strict_bytes(replay))
        alias = self.root / "linked-parent"
        self.make_directory_alias(target, alias)
        preview = self.run_cli(
            "preview",
            "--knowledge-root",
            str(self.knowledge),
            "--replay",
            str(alias / "replay.json"),
        )
        self.assertEqual(2, preview.returncode)
        self.assertIn("OPC_SHADOW_ERROR:", preview.stderr)
        self.assertNotIn("Traceback", preview.stderr)
        self.assertEqual(replay_path.read_bytes(), strict_bytes(replay))

        result = shadow.evaluate(
            self.knowledge,
            replay,
            strict_bytes(replay),
            expected_preview_sha256=shadow.build_preview(
                self.knowledge, replay, strict_bytes(replay)
            )[0]["preview_sha256"],
        )
        result_path = target / "result.json"
        result_path.write_bytes(strict_bytes(result))
        report = self.run_cli("report", "--result", str(alias / "result.json"))
        self.assertEqual(2, report.returncode)
        self.assertIn("OPC_SHADOW_ERROR:", report.stderr)
        self.assertNotIn("Traceback", report.stderr)
        self.assertFalse(any(path.suffix == ".md" for path in target.iterdir()))

        preview_data, _ = shadow.build_preview(self.knowledge, replay, strict_bytes(replay))
        evaluate = self.run_cli(
            "evaluate",
            "--knowledge-root",
            str(self.knowledge),
            "--replay",
            str(replay_path),
            "--expected-preview-sha256",
            preview_data["preview_sha256"],
            "--artifact-root",
            str(alias),
        )
        self.assertEqual(2, evaluate.returncode)
        self.assertIn("OPC_SHADOW_ERROR:", evaluate.stderr)
        self.assertNotIn("Traceback", evaluate.stderr)
        self.assertEqual(replay_path.read_bytes(), strict_bytes(replay))
        self.assertEqual(result_path.read_bytes(), strict_bytes(result))
        self.assertFalse((target / "shadow-synthetic-01.json").exists())

    def test_candidate_and_result_hard_links_fail_closed_with_zero_artifacts(self) -> None:
        _, source, provenance = self.make_candidate()
        candidate = self.knowledge / source
        candidate_alias = self.root / "candidate-hard-link.json"
        try:
            os.link(candidate, candidate_alias)
        except OSError:
            self.skipTest("hard links are not permitted")
        replay, raw = self.replay(source, provenance)
        with self.assertRaisesRegex(shadow.ShadowError, "non-linked|uniquely linked"):
            shadow.build_preview(self.knowledge, replay, raw)
        self.assertEqual([], list(self.artifacts.iterdir()))
        candidate_alias.unlink()

        preview, _ = shadow.build_preview(self.knowledge, replay, raw)
        result = shadow.evaluate(
            self.knowledge,
            replay,
            raw,
            expected_preview_sha256=preview["preview_sha256"],
        )
        source_result = self.root / "source-result.json"
        linked_result = self.root / "linked-result.json"
        source_result.write_bytes(strict_bytes(result))
        os.link(source_result, linked_result)
        process = self.run_cli("report", "--result", str(linked_result))
        self.assertEqual(2, process.returncode)
        self.assertIn("OPC_SHADOW_ERROR:", process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_artifact_plan_rejects_normal_directory_replacement_and_preserves_other_files(self) -> None:
        result = self.run_result()
        plan = shadow._assert_artifact_root(
            self.artifacts,
            knowledge_root=self.knowledge,
            project_root=None,
        )
        original = self.root / "original-artifacts"
        self.artifacts.rename(original)
        self.artifacts.mkdir()
        sentinel = self.artifacts / "sentinel.txt"
        sentinel.write_text("do not touch", encoding="utf-8")
        with self.assertRaisesRegex(shadow.ShadowError, "identity changed"):
            shadow._publish_artifacts(plan, "shadow-synthetic-01", result)
        self.assertEqual("do not touch", sentinel.read_text(encoding="utf-8"))
        self.assertEqual([], list(original.iterdir()))
        self.assertFalse((self.artifacts / "shadow-synthetic-01.json").exists())

    def test_bound_replay_read_detects_normal_parent_replacement(self) -> None:
        parent = self.root / "replay-parent"
        parent.mkdir()
        replay = parent / "replay.json"
        replay.write_text("{}", encoding="utf-8")
        moved = self.root / "moved-replay-parent"
        original_verify = shadow._BoundDirectory.verify_current
        calls = 0

        def replace_on_read(bound):
            nonlocal calls
            calls += 1
            if calls == 3:
                try:
                    parent.rename(moved)
                except OSError as exc:
                    # Windows holds a no-delete-share directory handle, so the
                    # attempted replacement itself must fail closed.
                    raise shadow.FeedbackError("synthetic parent identity change") from exc
                else:
                    parent.mkdir()
                    (parent / "sentinel.txt").write_text("do not touch", encoding="utf-8")
            return original_verify(bound)

        with mock.patch.object(shadow._BoundDirectory, "verify_current", replace_on_read):
            with self.assertRaises(shadow.ShadowError):
                shadow._read_json(replay, maximum=shadow.MAX_REPLAY_BYTES, label="replay")
        preserved = moved / "replay.json" if moved.exists() else parent / "replay.json"
        self.assertEqual(b"{}", preserved.read_bytes())
        if moved.exists():
            self.assertEqual(
                "do not touch",
                (parent / "sentinel.txt").read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases only")
    def test_windows_short_artifact_alias_preserves_equivalent_directory_identity(self) -> None:
        import ctypes

        self.make_candidate()
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short.restype = ctypes.c_uint
        source = str(self.artifacts.resolve(strict=True))
        size = get_short(source, None, 0)
        if size == 0:
            self.skipTest("8.3 aliases are unavailable")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = get_short(source, buffer, len(buffer))
        if written == 0 or Path(buffer.value) == Path(source):
            self.skipTest("this volume did not produce a distinct 8.3 alias")
        plan = shadow._assert_artifact_root(
            Path(buffer.value),
            knowledge_root=self.knowledge,
            project_root=None,
        )
        self.assertEqual(shadow._directory_identity(self.artifacts.lstat()), plan.identity)

    def test_report_states_separate_manager_flow(self) -> None:
        report = shadow.render_report(self.run_result())
        self.assertIn("separate preview and approval", report)
        self.assertIn("exact canonical transition", report)
        self.assertIn("model_inference", report)


if __name__ == "__main__":
    unittest.main()
