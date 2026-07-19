from __future__ import annotations

import hashlib
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
        self.assertEqual(result["recommendation"], "do_not_promote_on_shadow_evidence")
        self.assertTrue(any(item["metric_id"] == "scope_leakage_acceptances" for item in result["evidence"]["counterevidence"]))

    def test_conflicting_candidate_is_inconclusive(self) -> None:
        result = self.run_result(treatment="conflicting")
        self.assertEqual(result["status"], "inconclusive")
        self.assertTrue(any(item["code"] == "conflicting_measured_results" for item in result["failure_modes"]))

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
        self.assertFalse(self.artifacts.exists())

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

    def test_report_states_separate_manager_flow(self) -> None:
        report = shadow.render_report(self.run_result())
        self.assertIn("separate preview and approval", report)
        self.assertIn("exact canonical transition", report)
        self.assertIn("model_inference", report)


if __name__ == "__main__":
    unittest.main()
