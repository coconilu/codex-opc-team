from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_feedback  # noqa: E402
import opc_governance  # noqa: E402
import opc_hierarchical  # noqa: E402
import opc_lineage  # noqa: E402
import opc_memory  # noqa: E402


STAMP = "2026-07-19T00:00:00Z"


class LineageFixture:
    def __init__(self, root: Path):
        self.root = root
        self.project = root / "private-project"
        self.knowledge = root / "private-knowledge"
        self.data = root / "private-derived"
        self.project.mkdir()
        opc = self.project / ".opc"
        opc.mkdir()
        self.project_record = {
            "schema_version": 1,
            "project_id": "project-alpha",
            "name": "Synthetic",
            "created_at": STAMP,
            "updated_at": STAMP,
        }
        self.run_record = {
            "schema_version": 1,
            "run_id": "opc-run-synthetic",
            "title": "Synthetic run",
            "project_id": "project-alpha",
            "status": "implementing",
            "active": True,
            "evidence": {},
            "created_at": STAMP,
            "updated_at": STAMP,
        }
        (opc / "project.json").write_text(json.dumps(self.project_record), encoding="utf-8")
        (opc / "run.json").write_text(json.dumps(self.run_record), encoding="utf-8")
        self.backend = opc_memory.FileGitBackend(self.knowledge)
        self.backend.ensure_layout()
        subprocess.run(["git", "init", "-b", "main", str(self.knowledge)], check=True, capture_output=True)

    def add_approved(self, summary: str, *, project_id: str = "project-alpha") -> dict:
        candidate = self.backend.add_candidate(
            memory_type="decision",
            summary=summary,
            content=f"Synthetic body for {summary}.",
            keywords=summary.split(),
            scope="project",
            project_id=project_id,
            applicable_roles=["developer", "qa", "manager"],
        )
        return self.backend.approve(candidate["id"], approved_by="manager", validation="synthetic")

    def commit(self, message: str = "synthetic knowledge") -> str:
        subprocess.run(["git", "-C", str(self.knowledge), "add", "--", "."], check=True, capture_output=True)
        subprocess.run(
            [
                "git", "-C", str(self.knowledge), "-c", "user.name=OPC Test",
                "-c", "user.email=opc-test@example.invalid", "commit", "-m", message,
            ],
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", str(self.knowledge), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def recall(self, query: str) -> dict:
        return opc_hierarchical.HierarchicalRecall(self.backend, self.data).query(
            query,
            project_id="project-alpha",
            role="developer",
            allowed_sensitivity=("public", "internal", "restricted"),
        )

    def citation(self, record: dict) -> dict:
        return opc_governance.canonical_citation(
            record,
            self.backend.source_metadata(record["_source_path"]),
        )

    def event(
        self,
        event_id: str,
        *,
        event_type: str = "knowledge",
        role: str = "developer",
        step: str = "implement",
        reference: dict | None = None,
        state: str | None = None,
        provider: dict | None = None,
        evidence: list[dict] | None = None,
        reasons: list[str] | None = None,
        previous: str | None = None,
        minute: int = 0,
    ) -> dict:
        return {
            "event_id": event_id,
            "recorded_at": f"2026-07-19T00:{minute:02d}:00Z",
            "event_type": event_type,
            "role": role,
            "step_id": step,
            "knowledge_ref": reference,
            "knowledge_state": state,
            "provider": provider,
            "evidence_refs": evidence or [],
            "reason_codes": sorted(reasons or []),
            "previous_event_id": previous,
        }

    def append(self, event: dict, revision: int, recall: dict | None = None, *, minute: int | None = None) -> dict:
        now_minute = revision if minute is None else minute
        now = f"2026-07-19T00:{now_minute:02d}:30Z"
        preview = opc_lineage.preview_event(
            self.project,
            event,
            expected_revision=revision,
            recall_result=recall,
            knowledge_root=self.knowledge,
            now=now,
        )
        return opc_lineage.record_event(
            self.project,
            event,
            expected_revision=revision,
            plan_token=preview["plan_token"],
            recall_result=recall,
            knowledge_root=self.knowledge,
            now=now,
        )


@unittest.skipUnless(shutil.which("git"), "Git is required")
class KnowledgeLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = LineageFixture(Path(self.temporary.name))

    def one_recall(self) -> tuple[dict, dict]:
        record = self.fixture.add_approved("portable deployment rule")
        self.fixture.commit()
        result = self.fixture.recall("portable deployment rule")
        self.assertEqual(len(result["context_packet"]["citations"]), 1)
        return result, result["context_packet"]["citations"][0]

    def windows_short_alias_or_same(self, path: Path) -> Path:
        if os.name != "nt":
            return path
        import ctypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        source = str(path.resolve(strict=True))
        size = get_short(source, None, 0)
        if size == 0:
            return path
        buffer = ctypes.create_unicode_buffer(size + 1)
        if get_short(source, buffer, len(buffer)) == 0:
            return path
        return Path(buffer.value)

    def test_v01_run_without_lineage_is_readable_and_preview_is_zero_write(self) -> None:
        view = opc_lineage.build_view(self.fixture.project)
        self.assertEqual(view["lineage_status"], "unavailable")
        self.assertIsNone(view["record"])
        self.assertFalse((self.fixture.project / ".opc" / "lineage").exists())
        report = opc_lineage.render_report(view)
        self.assertIn("Lineage unavailable", report)
        self.assertIn("no usage is inferred or fabricated", report)

        result, citation = self.one_recall()
        event = self.fixture.event("lineage-preview", reference=citation, state="recalled")
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            recall_result=result,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:00:30Z",
        )
        self.assertFalse(preview["idempotent"])
        self.assertEqual(
            preview["subject"],
            {
                "project_ref": "project-alpha",
                "run_ref": "opc-run-synthetic",
                "project_instance": preview["record"]["events"][0]["project_instance"],
                "run_instance": preview["record"]["events"][0]["run_instance"],
            },
        )
        event_record = preview["record"]["events"][0]
        self.assertEqual(
            event_record["project_instance"]["sha256"],
            hashlib.sha256(
                (self.fixture.project / ".opc" / "project.json").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            event_record["run_instance"]["sha256"],
            hashlib.sha256(
                (self.fixture.project / ".opc" / "run.json").read_bytes()
            ).hexdigest(),
        )
        self.assertFalse((self.fixture.project / ".opc" / "lineage").exists())

    def test_default_preview_token_is_stable_across_cli_processes(self) -> None:
        recall, citation = self.one_recall()
        event = self.fixture.event(
            "lineage-cross-process", reference=citation, state="recalled"
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            recall_result=recall,
            knowledge_root=self.fixture.knowledge,
        )
        result = opc_lineage.record_event(
            self.fixture.project,
            event,
            expected_revision=0,
            plan_token=preview["plan_token"],
            recall_result=recall,
            knowledge_root=self.fixture.knowledge,
        )
        self.assertEqual(result["record"]["created_at"], event["recorded_at"])
        self.assertEqual(result["record"]["updated_at"], event["recorded_at"])

    def test_recalled_injected_adopted_are_distinct_and_packet_bound(self) -> None:
        recall, citation = self.one_recall()
        events = [
            self.fixture.event("lineage-recalled", reference=citation, state="recalled", minute=0),
            self.fixture.event("lineage-injected", reference=citation, state="injected", previous="lineage-recalled", minute=1),
            self.fixture.event("lineage-adopted", reference=citation, state="adopted", previous="lineage-injected", minute=2),
        ]
        for revision, event in enumerate(events):
            result = self.fixture.append(event, revision, recall)
            self.assertEqual(result["record"]["revision"], revision + 1)
        record = result["record"]
        self.assertEqual([item["knowledge_state"] for item in record["events"]], ["recalled", "injected", "adopted"])
        self.assertEqual(record["states"][0]["state"], "adopted")
        packet_refs = {item["context_packet"]["sha256"] for item in record["events"]}
        self.assertEqual(len(packet_refs), 1)
        self.assertNotIn("Synthetic body", json.dumps(record))
        view = opc_lineage.build_view(self.fixture.project, knowledge_root=self.fixture.knowledge)
        self.assertTrue(view["verification"][0]["usable"])
        report = opc_lineage.render_report(view)
        self.assertIn("association/evidence only", report)
        self.assertIn("## Confounders", report)
        self.assertIn("## Unknowns", report)
        self.assertEqual(report, opc_lineage.render_report(view))
        tampered = copy.deepcopy(view)
        tampered["verification"][0]["state"] = "ignored"
        with self.assertRaisesRegex(opc_lineage.LineageError, "materialized states"):
            opc_lineage.render_report(tampered)
        tampered = copy.deepcopy(view)
        tampered["lineage_status"] = "degraded"
        with self.assertRaisesRegex(opc_lineage.LineageError, "status differs"):
            opc_lineage.render_report(tampered)
        tampered = copy.deepcopy(view)
        tampered["run_ref"] = "opc-run-other"
        with self.assertRaisesRegex(opc_lineage.LineageError, "subject differs"):
            opc_lineage.render_report(tampered)

    def test_recalled_but_unused_and_all_terminal_states_across_roles_steps(self) -> None:
        recall, citation = self.one_recall()
        revision = 0
        recalled_only = self.fixture.event(
            "lineage-recalled-unused", role="qa", step="inspect", reference=citation,
            state="recalled", minute=revision,
        )
        self.fixture.append(recalled_only, revision, recall)
        revision += 1
        for index, terminal in enumerate(("ignored", "overridden", "contradicted"), start=1):
            role = "developer" if terminal != "contradicted" else "manager"
            step = f"step-{terminal}"
            recalled_id = f"lineage-r-{terminal}"
            injected_id = f"lineage-i-{terminal}"
            for state, event_id, previous in (
                ("recalled", recalled_id, None),
                ("injected", injected_id, recalled_id),
            ):
                event = self.fixture.event(
                    event_id, role=role, step=step, reference=citation, state=state,
                    previous=previous, minute=revision,
                )
                self.fixture.append(event, revision, recall)
                revision += 1
            event = self.fixture.event(
                f"lineage-{terminal}", role=role, step=step, reference=citation,
                state=terminal, previous=injected_id, minute=revision,
            )
            self.fixture.append(event, revision, recall)
            revision += 1
        view = opc_lineage.build_view(self.fixture.project, knowledge_root=self.fixture.knowledge)
        states = {(item["role"], item["step_id"]): item["state"] for item in view["verification"]}
        self.assertEqual(states[("qa", "inspect")], "recalled")
        self.assertEqual(states[("developer", "step-ignored")], "ignored")
        self.assertEqual(states[("developer", "step-overridden")], "overridden")
        self.assertEqual(states[("manager", "step-contradicted")], "contradicted")
        self.assertNotIn("adopted", [item["state"] for item in view["verification"]])

    def test_same_event_is_idempotent_conflict_and_stale_revision_fail_closed(self) -> None:
        recall, citation = self.one_recall()
        event = self.fixture.event("lineage-idempotent", reference=citation, state="recalled")
        self.fixture.append(event, 0, recall)
        preview = opc_lineage.preview_event(
            self.fixture.project, event, expected_revision=0, recall_result=recall,
            knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
        )
        self.assertTrue(preview["idempotent"])
        result = opc_lineage.record_event(
            self.fixture.project, event, expected_revision=0,
            plan_token=preview["plan_token"], recall_result=recall,
            knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
        )
        self.assertTrue(result["idempotent"])
        conflict = copy.deepcopy(event)
        conflict["role"] = "qa"
        with self.assertRaisesRegex(opc_lineage.LineageError, "different content"):
            opc_lineage.preview_event(
                self.fixture.project, conflict, expected_revision=1, recall_result=recall,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:01:30Z",
            )
        fresh = self.fixture.event(
            "lineage-stale", role="qa", step="inspect", reference=citation,
            state="recalled", minute=1,
        )
        with self.assertRaisesRegex(opc_lineage.LineageError, "stale"):
            opc_lineage.preview_event(
                self.fixture.project, fresh, expected_revision=0, recall_result=recall,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:01:30Z",
            )

    def test_record_revalidates_current_head_after_external_preview(self) -> None:
        recall, citation = self.one_recall()
        event = self.fixture.event(
            "lineage-head-revalidation", reference=citation, state="recalled"
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            recall_result=recall,
            knowledge_root=self.fixture.knowledge,
        )
        source = self.fixture.knowledge / citation["source_path"]
        changed = json.loads(source.read_text(encoding="utf-8"))
        changed["summary"] = "changed after external preview"
        source.write_bytes(opc_memory.canonical_record_bytes(changed))
        self.fixture.commit("advance HEAD after preview")
        with self.assertRaisesRegex(opc_lineage.LineageError, "currently usable"):
            opc_lineage.record_event(
                self.fixture.project,
                event,
                expected_revision=0,
                plan_token=preview["plan_token"],
                recall_result=recall,
                knowledge_root=self.fixture.knowledge,
            )
        self.assertFalse((self.fixture.project / ".opc" / "lineage").exists())

    def test_recalled_revision_must_exactly_match_packet_citation(self) -> None:
        old_recall, old_citation = self.one_recall()
        source = self.fixture.knowledge / old_citation["source_path"]
        changed = json.loads(source.read_text(encoding="utf-8"))
        changed["summary"] = "new canonical revision with the same record id"
        source.write_bytes(opc_memory.canonical_record_bytes(changed))
        self.fixture.commit("new canonical revision")
        new_recall = self.fixture.recall("new canonical revision")
        new_citation = new_recall["context_packet"]["citations"][0]
        self.assertEqual(new_citation["record_id"], old_citation["record_id"])
        self.assertNotEqual(new_citation["source_commit"], old_citation["source_commit"])
        self.assertNotEqual(new_citation["content_sha256"], old_citation["content_sha256"])
        event = self.fixture.event(
            "lineage-recalled-new-revision",
            reference=new_citation,
            state="recalled",
        )
        with self.assertRaisesRegex(opc_lineage.LineageError, "exact ContextPacket"):
            opc_lineage.preview_event(
                self.fixture.project,
                event,
                expected_revision=0,
                recall_result=old_recall,
                knowledge_root=self.fixture.knowledge,
            )

    def test_invalid_transition_and_packet_citation_mismatch_fail(self) -> None:
        recall, citation = self.one_recall()
        adopted = self.fixture.event("lineage-bad-adopt", reference=citation, state="adopted")
        with self.assertRaisesRegex(opc_lineage.LineageError, "transition"):
            opc_lineage.preview_event(
                self.fixture.project, adopted, expected_revision=0, recall_result=recall,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
        different = self.fixture.add_approved("second rule")
        self.fixture.commit("second")
        wrong = self.fixture.event(
            "lineage-wrong-packet", reference=self.fixture.citation(different), state="injected",
            previous="lineage-never",
        )
        with self.assertRaises(opc_lineage.LineageError):
            opc_lineage.preview_event(
                self.fixture.project, wrong, expected_revision=0, recall_result=recall,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )

    def test_stale_cross_project_obsolete_and_omission_degrade_without_usage_fabrication(self) -> None:
        alpha = self.fixture.add_approved("alpha rule")
        beta = self.fixture.add_approved("beta rule", project_id="project-beta")
        obsolete = self.fixture.add_approved("obsolete rule")
        self.fixture.commit()
        alpha_ref = self.fixture.citation(alpha)
        beta_ref = self.fixture.citation(beta)
        obsolete_ref = self.fixture.citation(obsolete)
        self.fixture.backend.mark_obsolete(obsolete["id"], reason="synthetic obsolete")
        self.fixture.commit("obsolete")
        omitted = self.fixture.event(
            "lineage-cross-project", reference=beta_ref, state="omitted",
            reasons=["cross_project_scope"], minute=0,
        )
        self.fixture.append(omitted, 0, None)
        self.fixture.append(
            self.fixture.event(
                "lineage-obsolete", role="manager", step="curate", reference=obsolete_ref,
                state="omitted", reasons=["obsolete"], minute=1,
            ),
            1,
            None,
            minute=1,
        )
        alpha_recall = self.fixture.recall("alpha rule")
        alpha_ref = alpha_recall["context_packet"]["citations"][0]
        recalled = self.fixture.event(
            "lineage-alpha-recalled", reference=alpha_ref, state="recalled",
            role="qa", step="inspect", minute=2,
        )
        self.fixture.append(recalled, 2, alpha_recall, minute=2)
        path = self.fixture.knowledge / alpha["_source_path"]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["summary"] = "changed after trace"
        path.write_bytes(opc_memory.canonical_record_bytes(value))
        view = opc_lineage.build_view(self.fixture.project, knowledge_root=self.fixture.knowledge)
        by_id = {item["record_id"]: item for item in view["verification"]}
        self.assertFalse(by_id[beta["id"]]["usable"])
        self.assertIn("cross_project_scope", by_id[beta["id"]]["reason_codes"])
        self.assertFalse(by_id[alpha["id"]]["usable"])
        self.assertIn("stale_provenance", by_id[alpha["id"]]["reason_codes"])
        self.assertIn("obsolete", by_id[obsolete["id"]]["reason_codes"])
        self.assertEqual(view["lineage_status"], "degraded")

    def test_unresolved_conflict_is_revalidated_as_an_omission_not_usage(self) -> None:
        _, citation = self.one_recall()
        conflict_context = {
            "records": [],
            "conflicts": [{"citations": [citation, {**citation, "record_id": "exp-conflict-peer"}]}],
            "omissions": [],
        }
        with patch.object(self.fixture.backend, "query_context", return_value=conflict_context):
            reasons = opc_lineage._validate_current_reference(
                self.fixture.backend,
                citation,
                project_id="project-alpha",
                role="developer",
            )
        self.assertEqual(reasons, ["unresolved_conflict"])

    def test_no_memory_and_provider_failures_are_explicit_and_do_not_block_file_git(self) -> None:
        recall, citation = self.one_recall()
        events = [
            self.fixture.event(
                "lineage-no-memory", event_type="provider", role="developer", step="recall",
                provider={"provider_id": "memory", "state": "no_memory", "authoritative": False},
                reasons=["optional_provider_not_configured"], minute=0,
            ),
            self.fixture.event(
                "lineage-provider-failed", event_type="provider", role="qa", step="inspect",
                provider={"provider_id": "mem0", "state": "failed", "authoritative": False},
                reasons=["provider_error"], minute=1,
            ),
            self.fixture.event(
                "lineage-provider-missing", event_type="provider", role="manager", step="plan",
                provider={"provider_id": "mem0", "state": "missing", "authoritative": False},
                reasons=["provider_missing"], minute=2,
            ),
            self.fixture.event(
                "lineage-provider-disabled", event_type="provider", role="developer", step="plan",
                provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
                reasons=["provider_disabled"], minute=3,
            ),
            self.fixture.event(
                "lineage-provider-stale", event_type="provider", role="qa", step="plan",
                provider={"provider_id": "mem0", "state": "stale", "authoritative": False},
                reasons=["provider_stale"], minute=4,
            ),
            self.fixture.event(
                "lineage-filegit-recalled", role="developer", step="implement",
                reference=citation, state="recalled", minute=5,
            ),
        ]
        for revision, event in enumerate(events[:-1]):
            self.fixture.append(event, revision, minute=revision)
        self.fixture.append(events[-1], len(events) - 1, recall, minute=5)
        view = opc_lineage.build_view(self.fixture.project, knowledge_root=self.fixture.knowledge)
        self.assertEqual(
            {item["state"] for item in view["provider_degradations"]},
            {"no_memory", "failed", "missing", "disabled", "stale"},
        )
        self.assertTrue(view["verification"][0]["usable"])

    def test_late_feedback_outcome_and_qa_refs_are_existing_portable_and_bounded(self) -> None:
        recall, citation = self.one_recall()
        self.fixture.append(
            self.fixture.event("lineage-recalled", reference=citation, state="recalled"),
            0,
            recall,
        )
        feedback_event = {
            "event_id": "feedback-late-outcome",
            "recorded_at": "2026-07-19T00:01:00Z",
            "category": "confirmed_outcome",
            "epistemic_status": "confirmed_outcome",
            "summary": "Synthetic late outcome.",
            "outcome_status": "pass",
            "manager_judgment": "not_applicable",
            "qa_status": "not_applicable",
            "references": {
                "project_id": "project-alpha", "run_id": "opc-run-synthetic",
                "candidate_ids": [], "metric_refs": [], "artifact_refs": [],
            },
        }
        feedback_result = opc_feedback.record_feedback(
            self.fixture.project,
            feedback_event,
            expected_revision=0,
            now="2026-07-19T00:01:30Z",
        )
        feedback_path = self.fixture.project / ".opc" / "feedback" / "opc-run-synthetic.json"
        qa_path = self.fixture.project / ".opc" / "qa" / "result.json"
        qa_path.parent.mkdir()
        qa_path.write_text('{"status":"pass"}\n', encoding="utf-8")
        evaluation_path = self.fixture.project / ".opc" / "evaluation" / "result.json"
        evaluation_path.parent.mkdir()
        evaluation_path.write_text('{"status":"synthetic"}\n', encoding="utf-8")
        refs = [
            {"kind": "outcome", "ref": ".opc/feedback/opc-run-synthetic.json", "sha256": hashlib.sha256(feedback_path.read_bytes()).hexdigest()},
            {"kind": "feedback", "ref": ".opc/feedback/opc-run-synthetic.json", "sha256": hashlib.sha256(feedback_path.read_bytes()).hexdigest()},
            {"kind": "qa", "ref": ".opc/qa/result.json", "sha256": hashlib.sha256(qa_path.read_bytes()).hexdigest()},
            {"kind": "evaluation", "ref": ".opc/evaluation/result.json", "sha256": hashlib.sha256(evaluation_path.read_bytes()).hexdigest()},
        ]
        association = self.fixture.event(
            "lineage-late-association", event_type="association", role="manager",
            step="outcome-review", reference=citation, evidence=refs, minute=2,
        )
        self.fixture.append(association, 1, recall, minute=2)
        view = opc_lineage.build_view(self.fixture.project, knowledge_root=self.fixture.knowledge)
        self.assertEqual(len(view["associations"]), 1)
        self.assertEqual(
            {item["kind"] for item in view["associations"][0]["evidence_refs"]},
            {"outcome", "feedback", "qa", "evaluation"},
        )
        self.assertEqual(feedback_result["record"]["revision"], 1)
        qa_path.write_text('{"status":"fail"}\n', encoding="utf-8")
        degraded = opc_lineage.build_view(self.fixture.project, knowledge_root=self.fixture.knowledge)
        self.assertFalse(degraded["associations"][0]["usable"])
        self.assertEqual(degraded["lineage_status"], "degraded")
        tampered = copy.deepcopy(degraded)
        tampered["associations"][0]["reason_codes"] = ["invented_reason"]
        with self.assertRaisesRegex(opc_lineage.LineageError, "not deterministic"):
            opc_lineage.render_report(tampered)
        with self.assertRaisesRegex(opc_lineage.LineageError, "hash is stale"):
            opc_lineage.preview_event(
                self.fixture.project,
                self.fixture.event(
                    "lineage-stale-evidence", event_type="association", role="qa",
                    step="late", evidence=[refs[2]], minute=3,
                ),
                expected_revision=2,
                knowledge_root=self.fixture.knowledge,
                now="2026-07-19T00:03:30Z",
            )

    def test_evidence_refs_are_rejected_outside_association_events(self) -> None:
        recall, citation = self.one_recall()
        evidence = self.fixture.project / ".opc" / "qa" / "result.json"
        evidence.parent.mkdir()
        evidence.write_text('{"status":"pass"}\n', encoding="utf-8")
        reference = {
            "kind": "qa",
            "ref": ".opc/qa/result.json",
            "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        }
        knowledge = self.fixture.event(
            "lineage-knowledge-with-evidence",
            reference=citation,
            state="recalled",
            evidence=[reference],
        )
        provider = self.fixture.event(
            "lineage-provider-with-evidence",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "failed", "authoritative": False},
            evidence=[reference],
            reasons=["provider_error"],
        )
        with self.assertRaisesRegex(opc_lineage.LineageError, "contradictory"):
            opc_lineage.preview_event(
                self.fixture.project,
                knowledge,
                expected_revision=0,
                recall_result=recall,
                knowledge_root=self.fixture.knowledge,
            )
        with self.assertRaisesRegex(opc_lineage.LineageError, "contradictory"):
            opc_lineage.preview_event(
                self.fixture.project,
                provider,
                expected_revision=0,
                knowledge_root=self.fixture.knowledge,
            )

    def test_shadow_reference_validation_and_private_boundary(self) -> None:
        shadow = self.fixture.project / ".opc" / "shadow" / "result.json"
        shadow.parent.mkdir()
        shadow.write_text("{}\n", encoding="utf-8")
        ref = {"kind": "shadow", "ref": ".opc/shadow/result.json", "sha256": hashlib.sha256(shadow.read_bytes()).hexdigest()}
        event = self.fixture.event(
            "lineage-shadow", event_type="association", role="manager", step="evaluate",
            evidence=[ref], minute=0,
        )
        with self.assertRaisesRegex(opc_lineage.LineageError, "shadow evidence is invalid"):
            opc_lineage.preview_event(
                self.fixture.project, event, expected_revision=0,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
        with patch("opc_shadow.validate_result", return_value=None):
            preview = opc_lineage.preview_event(
                self.fixture.project, event, expected_revision=0,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
        self.assertFalse(preview["idempotent"])
        escaped = copy.deepcopy(ref)
        escaped["ref"] = "docs/result.json"
        with self.assertRaisesRegex(opc_lineage.LineageError, "private .opc"):
            opc_lineage._validate_evidence_ref(escaped)

    def test_nonfinite_oversized_ids_sensitive_values_and_schema_runtime_parity(self) -> None:
        recall, citation = self.one_recall()
        event = self.fixture.event("lineage-safe", reference=citation, state="recalled")
        bad = copy.deepcopy(event)
        bad["role"] = "r" * 65
        with self.assertRaises(opc_lineage.LineageError):
            opc_lineage.preview_event(
                self.fixture.project, bad, expected_revision=0, recall_result=recall,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
        with self.assertRaises(opc_lineage.LineageError):
            opc_lineage._canonical_bytes({"bad": float("nan")})
        contract = json.loads((ROOT / "plugins/codex-opc-team/assets/lineage/knowledge-lineage-contract.v1.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "plugins/codex-opc-team/assets/lineage/knowledge-lineage.v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["limits"]["events"], opc_lineage.MAX_EVENTS)
        self.assertTrue(contract["evidence_association_only"])
        self.assertEqual(contract["storage"]["git_ignored_boundary"], ".opc/lineage/")
        self.assertEqual(
            set(contract["storage"]["transaction_artifacts"]),
            {"final", "lock", "pending", "backup"},
        )
        self.assertEqual(
            contract["storage"]["subject_binding"],
            "exact-project-run-instances",
        )
        self.assertEqual(schema["properties"]["events"]["maxItems"], opc_lineage.MAX_EVENTS)
        self.assertFalse(schema["additionalProperties"])
        if importlib.util.find_spec("jsonschema"):
            import jsonschema

            jsonschema.Draft202012Validator.check_schema(schema)
            preview = opc_lineage.preview_event(
                self.fixture.project, event, expected_revision=0, recall_result=recall,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
            validator = jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.FormatChecker()
            )
            validator.validate(preview["record"])
            invalid = copy.deepcopy(preview["record"])
            invalid["events"][0]["evidence_refs"] = [{
                "kind": "qa",
                "ref": ".opc/qa/result.json",
                "sha256": "0" * 64,
            }]
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(invalid)

    def test_hardlink_evidence_and_concurrent_cas_fail_closed_without_partial(self) -> None:
        evidence = self.fixture.project / ".opc" / "qa" / "result.json"
        evidence.parent.mkdir()
        evidence.write_text("{}\n", encoding="utf-8")
        hardlink = self.fixture.project / ".opc" / "qa" / "alias.json"
        try:
            os.link(evidence, hardlink)
        except OSError:
            self.skipTest("hard links unavailable")
        ref = {"kind": "qa", "ref": ".opc/qa/result.json", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
        event = self.fixture.event(
            "lineage-hardlink", event_type="association", role="qa", step="inspect",
            evidence=[ref],
        )
        with self.assertRaisesRegex((opc_lineage.LineageError, opc_feedback.FeedbackError), "single-link|filesystem link|hard-linked"):
            opc_lineage.preview_event(
                self.fixture.project, event, expected_revision=0,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
        hardlink.unlink()
        preview = opc_lineage.preview_event(
            self.fixture.project, event, expected_revision=0,
            knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
        )
        barrier = threading.Barrier(2)
        results: list[str] = []

        def writer() -> None:
            barrier.wait()
            try:
                opc_lineage.record_event(
                    self.fixture.project, event, expected_revision=0,
                    plan_token=preview["plan_token"], knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:00:30Z",
                )
                results.append("ok")
            except opc_lineage.LineageError:
                results.append("closed")

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("closed"), 1)
        lineage_dir = self.fixture.project / ".opc" / "lineage"
        self.assertEqual([path.name for path in lineage_dir.iterdir()], ["opc-run-synthetic.json"])

    def test_same_revision_sidecar_replacement_fails_base_record_cas(self) -> None:
        first = self.fixture.event(
            "lineage-provider-first",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "failed", "authoritative": False},
            reasons=["provider_error"],
        )
        self.fixture.append(first, 0)
        second = self.fixture.event(
            "lineage-provider-second",
            event_type="provider",
            role="qa",
            step="inspect",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
            minute=1,
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            second,
            expected_revision=1,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:01:30Z",
        )
        sidecar = self.fixture.project / ".opc" / "lineage" / "opc-run-synthetic.json"
        self.assertEqual(
            preview["base_record"],
            {"exists": True, "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()},
        )
        competitor = json.loads(sidecar.read_text(encoding="utf-8"))
        competitor["events"][0]["provider"]["state"] = "stale"
        competitor["events"][0]["reason_codes"] = ["provider_stale"]
        opc_lineage.validate_record(competitor)
        original = opc_lineage._verify_checkpoint
        replaced = False

        def replace_same_revision(bound, label):
            nonlocal replaced
            if label == "after_lineage_lock" and not replaced:
                sidecar.write_bytes(
                    (json.dumps(competitor, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                )
                replaced = True
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=replace_same_revision):
            with self.assertRaisesRegex(opc_lineage.LineageError, "base record changed"):
                opc_lineage.record_event(
                    self.fixture.project,
                    second,
                    expected_revision=1,
                    plan_token=preview["plan_token"],
                    knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:01:30Z",
                )
        stored = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(stored["events"][0]["provider"]["state"], "stale")

    def test_different_revision_race_fails_then_same_event_retry_is_idempotent(self) -> None:
        first = self.fixture.event(
            "lineage-race-first",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "failed", "authoritative": False},
            reasons=["provider_error"],
        )
        self.fixture.append(first, 0)
        second = self.fixture.event(
            "lineage-race-second",
            event_type="provider",
            role="qa",
            step="inspect",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
            minute=1,
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            second,
            expected_revision=1,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:01:30Z",
        )
        sidecar = self.fixture.project / ".opc" / "lineage" / "opc-run-synthetic.json"
        competitor = preview["record"]
        original = opc_lineage._verify_checkpoint
        replaced = False

        def append_competitor(bound, label):
            nonlocal replaced
            if label == "after_lineage_lock" and not replaced:
                sidecar.write_bytes(
                    (json.dumps(competitor, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                )
                replaced = True
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=append_competitor):
            with self.assertRaisesRegex(opc_lineage.LineageError, "base record changed"):
                opc_lineage.record_event(
                    self.fixture.project,
                    second,
                    expected_revision=1,
                    plan_token=preview["plan_token"],
                    knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:01:30Z",
                )
        retry = opc_lineage.preview_event(
            self.fixture.project,
            second,
            expected_revision=1,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:01:30Z",
        )
        self.assertTrue(retry["idempotent"])
        result = opc_lineage.record_event(
            self.fixture.project,
            second,
            expected_revision=1,
            plan_token=retry["plan_token"],
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:01:30Z",
        )
        self.assertTrue(result["idempotent"])

    def test_first_creation_race_with_empty_record_fails_closed(self) -> None:
        event = self.fixture.event(
            "lineage-first-create",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:00:30Z",
        )
        self.assertEqual(preview["base_record"], {"exists": False, "sha256": None})
        competitor = copy.deepcopy(preview["record"])
        competitor["revision"] = 0
        competitor["events"] = []
        competitor["states"] = []
        competitor["updated_at"] = competitor["created_at"]
        opc_lineage.validate_record(competitor)
        sidecar = self.fixture.project / ".opc" / "lineage" / "opc-run-synthetic.json"
        original = opc_lineage._verify_checkpoint
        created = False

        def create_competitor(bound, label):
            nonlocal created
            if label == "after_lineage_lock" and not created:
                sidecar.write_bytes(
                    (json.dumps(competitor, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                )
                created = True
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=create_competitor):
            with self.assertRaisesRegex(opc_lineage.LineageError, "base record changed"):
                opc_lineage.record_event(
                    self.fixture.project,
                    event,
                    expected_revision=0,
                    plan_token=preview["plan_token"],
                    knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:00:30Z",
                )
        stored = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(stored["revision"], 0)
        self.assertEqual(stored["events"], [])

    def test_publish_failure_leaves_no_partial_sidecar_or_new_directory(self) -> None:
        event = self.fixture.event(
            "lineage-fail", event_type="provider", role="developer", step="recall",
            provider={"provider_id": "mem0", "state": "failed", "authoritative": False},
            reasons=["provider_error"],
        )
        preview = opc_lineage.preview_event(
            self.fixture.project, event, expected_revision=0,
            knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
        )
        original = opc_lineage._verify_checkpoint

        def fail(bound, label):
            if label == "before_replace":
                raise opc_lineage.LineageError("synthetic checkpoint failure")
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=fail):
            with self.assertRaisesRegex(opc_lineage.LineageError, "synthetic"):
                opc_lineage.record_event(
                    self.fixture.project, event, expected_revision=0,
                    plan_token=preview["plan_token"], knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:00:30Z",
                )
        self.assertFalse((self.fixture.project / ".opc" / "lineage").exists())

    def test_git_detection_only_accepts_explicit_non_git_and_other_failures_close(self) -> None:
        target = self.fixture.project / ".opc" / "lineage" / "opc-run-synthetic.json"
        failures = {
            "unavailable": FileNotFoundError("git unavailable"),
            "timeout": subprocess.TimeoutExpired("git", 5),
            "nonzero": subprocess.CompletedProcess(
                [], 2, stdout="", stderr="fatal: synthetic Git failure"
            ),
            "invalid-success": subprocess.CompletedProcess(
                [], 0, stdout="unexpected", stderr=""
            ),
        }
        for label, result in failures.items():
            with self.subTest(label=label):
                with patch.object(opc_lineage.subprocess, "run", side_effect=[result]):
                    with self.assertRaisesRegex(opc_lineage.LineageError, "Git|git"):
                        opc_lineage._assert_private_or_ignored(self.fixture.project, target)

        explicit_non_git = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: not a git repository (or any parent)"
        )
        with patch.object(
            opc_lineage.subprocess, "run", return_value=explicit_non_git
        ):
            opc_lineage._assert_private_or_ignored(self.fixture.project, target)
        (self.fixture.project / ".git").mkdir()
        with patch.object(
            opc_lineage.subprocess, "run", return_value=explicit_non_git
        ):
            with self.assertRaisesRegex(opc_lineage.LineageError, "failed closed"):
                opc_lineage._assert_private_or_ignored(self.fixture.project, target)

        inside = subprocess.CompletedProcess([], 0, stdout="true\n", stderr="")
        top_failure = subprocess.CompletedProcess(
            [], 2, stdout="", stderr="fatal: boundary failure"
        )
        with patch.object(
            opc_lineage.subprocess, "run", side_effect=[inside, top_failure]
        ):
            with self.assertRaisesRegex(opc_lineage.LineageError, "boundary"):
                opc_lineage._assert_private_or_ignored(self.fixture.project, target)

        top = subprocess.CompletedProcess(
            [], 0, stdout=str(self.fixture.project) + "\n", stderr=""
        )
        tracked_success = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ignore_failure = subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"")
        with patch.object(
            opc_lineage.subprocess,
            "run",
            side_effect=[inside, top, tracked_success, ignore_failure],
        ):
            with self.assertRaisesRegex(opc_lineage.LineageError, "ignore"):
                opc_lineage._assert_private_or_ignored(self.fixture.project, target)

    def test_git_project_requires_lineage_path_to_be_ignored(self) -> None:
        subprocess.run(["git", "init", "-b", "main", str(self.fixture.project)], check=True, capture_output=True)
        event = self.fixture.event(
            "lineage-provider", event_type="provider", role="developer", step="recall",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        with self.assertRaisesRegex(opc_lineage.LineageError, "ignored .opc"):
            opc_lineage.preview_event(
                self.fixture.project, event, expected_revision=0,
                knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
            )
        (self.fixture.project / ".gitignore").write_text(".opc/\n", encoding="utf-8")
        preview = opc_lineage.preview_event(
            self.fixture.project, event, expected_revision=0,
            knowledge_root=self.fixture.knowledge, now="2026-07-19T00:00:30Z",
        )
        self.assertFalse(preview["idempotent"])

    def test_record_rechecks_git_ignore_boundary_inside_transaction(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.fixture.project)],
            check=True,
            capture_output=True,
        )
        ignore = self.fixture.project / ".gitignore"
        ignore.write_text(".opc/\n", encoding="utf-8")
        event = self.fixture.event(
            "lineage-ignore-race",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:00:30Z",
        )
        original = opc_lineage._assert_private_or_ignored
        calls = 0

        def revoke_ignore(project, path):
            nonlocal calls
            calls += 1
            if calls == 2:
                ignore.write_text("", encoding="utf-8")
            return original(project, path)

        with patch.object(
            opc_lineage, "_assert_private_or_ignored", side_effect=revoke_ignore
        ):
            with self.assertRaisesRegex(opc_lineage.LineageError, "ignored .opc"):
                opc_lineage.record_event(
                    self.fixture.project,
                    event,
                    expected_revision=0,
                    plan_token=preview["plan_token"],
                    knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:00:30Z",
                )
        self.assertEqual(calls, 2)
        self.assertFalse((self.fixture.project / ".opc" / "lineage").exists())

    def test_record_binds_old_run_path_and_rejects_run_switch_inside_lock(self) -> None:
        event = self.fixture.event(
            "lineage-run-switch",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:00:30Z",
        )
        original = opc_lineage._verify_checkpoint
        observed_lock: list[str] = []

        def switch_run(bound, label):
            if label == "after_lineage_lock":
                observed_lock.extend(path.name for path in bound.path.iterdir())
                changed = dict(self.fixture.run_record)
                changed["run_id"] = "opc-run-next"
                (self.fixture.project / ".opc" / "run.json").write_text(
                    json.dumps(changed), encoding="utf-8"
                )
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=switch_run):
            with self.assertRaisesRegex(opc_lineage.LineageError, "subject changed"):
                opc_lineage.record_event(
                    self.fixture.project,
                    event,
                    expected_revision=0,
                    plan_token=preview["plan_token"],
                    knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:00:30Z",
                )
        self.assertIn("opc-run-synthetic.json.lock", observed_lock)
        lineage = self.fixture.project / ".opc" / "lineage"
        self.assertFalse((lineage / "opc-run-synthetic.json").exists())
        self.assertFalse((lineage / "opc-run-next.json").exists())
        self.assertEqual([], list(lineage.iterdir()) if lineage.exists() else [])

    def test_record_rejects_project_switch_after_pending_before_publish(self) -> None:
        event = self.fixture.event(
            "lineage-project-switch",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            event,
            expected_revision=0,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:00:30Z",
        )
        original = opc_lineage._verify_checkpoint
        pending_seen = False

        def switch_project(bound, label):
            nonlocal pending_seen
            if label == "after_pending_creation":
                pending_seen = any(".pending-" in path.name for path in bound.path.iterdir())
                project_record = dict(self.fixture.project_record)
                project_record["project_id"] = "project-beta"
                run_record = dict(self.fixture.run_record)
                run_record["project_id"] = "project-beta"
                (self.fixture.project / ".opc" / "project.json").write_text(
                    json.dumps(project_record), encoding="utf-8"
                )
                (self.fixture.project / ".opc" / "run.json").write_text(
                    json.dumps(run_record), encoding="utf-8"
                )
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=switch_project):
            with self.assertRaisesRegex(opc_lineage.LineageError, "subject changed"):
                opc_lineage.record_event(
                    self.fixture.project,
                    event,
                    expected_revision=0,
                    plan_token=preview["plan_token"],
                    knowledge_root=self.fixture.knowledge,
                    now="2026-07-19T00:00:30Z",
                )
        self.assertTrue(pending_seen)
        lineage = self.fixture.project / ".opc" / "lineage"
        self.assertEqual([], list(lineage.iterdir()) if lineage.exists() else [])

    def test_git_requires_directory_ignore_not_exact_final_file_rule(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.fixture.project)],
            check=True,
            capture_output=True,
        )
        ignore = self.fixture.project / ".gitignore"
        ignore.write_text(
            ".opc/lineage/opc-run-synthetic.json\n", encoding="utf-8"
        )
        event = self.fixture.event(
            "lineage-narrow-ignore",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        with self.assertRaisesRegex(opc_lineage.LineageError, "lineage directory"):
            opc_lineage.preview_event(
                self.fixture.project,
                event,
                expected_revision=0,
                knowledge_root=self.fixture.knowledge,
                now="2026-07-19T00:00:30Z",
            )

    def test_directory_ignore_covers_transaction_artifacts_across_filesystem_aliases(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.fixture.project)],
            check=True,
            capture_output=True,
        )
        (self.fixture.project / ".gitignore").write_text(
            ".opc/lineage/\n", encoding="utf-8"
        )
        self.fixture.project = self.windows_short_alias_or_same(
            self.fixture.project
        )
        first = self.fixture.event(
            "lineage-private-first",
            event_type="provider",
            provider={"provider_id": "mem0", "state": "disabled", "authoritative": False},
            reasons=["provider_disabled"],
        )
        self.fixture.append(first, 0)
        second = self.fixture.event(
            "lineage-private-second",
            event_type="provider",
            role="qa",
            step="inspect",
            provider={"provider_id": "mem0", "state": "failed", "authoritative": False},
            reasons=["provider_error"],
            minute=1,
        )
        preview = opc_lineage.preview_event(
            self.fixture.project,
            second,
            expected_revision=1,
            knowledge_root=self.fixture.knowledge,
            now="2026-07-19T00:01:30Z",
        )
        original = opc_lineage._verify_checkpoint
        ignored_artifacts: set[str] = set()

        def inspect_artifacts(bound, label):
            if label == "after_pending_creation":
                self.assertTrue(
                    os.path.samefile(
                        bound.path,
                        self.fixture.project / ".opc" / "lineage",
                    )
                )
                for artifact in bound.path.iterdir():
                    # Windows can expose the fixture root through an 8.3 alias
                    # while the bound handle expands to the long path.  The
                    # portable Git ref is defined by the bound directory and
                    # child name, not lexical parent-path spelling.
                    relative = f".opc/lineage/{artifact.name}"
                    result = subprocess.run(
                        ["git", "-C", str(self.fixture.project), "check-ignore", "-q", "--", relative],
                        check=False,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, artifact.name)
                    ignored_artifacts.add(artifact.name)
            return original(bound, label)

        with patch.object(opc_lineage, "_verify_checkpoint", side_effect=inspect_artifacts):
            result = opc_lineage.record_event(
                self.fixture.project,
                second,
                expected_revision=1,
                plan_token=preview["plan_token"],
                knowledge_root=self.fixture.knowledge,
                now="2026-07-19T00:01:30Z",
            )
        self.assertFalse(result["idempotent"])
        self.assertIn("opc-run-synthetic.json", ignored_artifacts)
        self.assertIn("opc-run-synthetic.json.lock", ignored_artifacts)
        self.assertTrue(any(".pending-" in name for name in ignored_artifacts))
        self.assertTrue(any(".backup-" in name for name in ignored_artifacts))
        self.assertEqual(
            ["opc-run-synthetic.json"],
            [path.name for path in (self.fixture.project / ".opc" / "lineage").iterdir()],
        )

    def test_git_tracked_lineage_and_tracked_probe_failure_close(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.fixture.project)],
            check=True,
            capture_output=True,
        )
        lineage = self.fixture.project / ".opc" / "lineage"
        lineage.mkdir()
        tracked = lineage / "tracked.txt"
        tracked.write_text("must not be public\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.fixture.project), "add", "-f", "--", ".opc/lineage/tracked.txt"],
            check=True,
            capture_output=True,
        )
        (self.fixture.project / ".gitignore").write_text(
            ".opc/lineage/\n", encoding="utf-8"
        )
        target = lineage / "opc-run-synthetic.json"
        with self.assertRaisesRegex(opc_lineage.LineageError, "tracked content"):
            opc_lineage._assert_private_or_ignored(self.fixture.project, target)

        inside = subprocess.CompletedProcess([], 0, stdout="true\n", stderr="")
        top = subprocess.CompletedProcess(
            [], 0, stdout=str(self.fixture.project) + "\n", stderr=""
        )
        failed = subprocess.CompletedProcess([], 2, stdout="", stderr="")
        with patch.object(
            opc_lineage.subprocess,
            "run",
            side_effect=[inside, top, failed],
        ):
            with self.assertRaisesRegex(opc_lineage.LineageError, "tracked-lineage"):
                opc_lineage._assert_private_or_ignored(self.fixture.project, target)

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases only")
    def test_windows_short_path_alias_uses_filesystem_identity(self) -> None:
        source = self.fixture.project.resolve(strict=True)
        alias = self.windows_short_alias_or_same(source)
        if alias == source:
            self.skipTest("no distinct 8.3 alias")
        self.assertTrue(os.path.samefile(alias, source))
        view = opc_lineage.build_view(alias)
        self.assertEqual(view["lineage_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
