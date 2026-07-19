from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_governance as governance  # noqa: E402
import opc_memory  # noqa: E402


class StaticProvider:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits

    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        raise AssertionError("query test must not write the provider")

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        return self.hits[:limit]


class KnowledgeGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.knowledge = self.base / "knowledge"
        self.data = self.base / "data"
        self.backups = self.base / "backups"
        self.backups.mkdir()
        self.backend = opc_memory.FileGitBackend(self.knowledge)
        self.backend.ensure_layout()

    @staticmethod
    def relation(
        kind: str,
        target_id: str,
        *,
        scope: str = "global",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "target_id": target_id,
            "scope": scope,
            "project_id": project_id,
        }

    @staticmethod
    def record(
        record_id: str,
        *,
        marker: str = "governance-marker",
        scope: str = "global",
        project_id: str | None = None,
        status: str = "approved",
        role: str | None = None,
        knowledge_type: str = "decision",
        constraints: Mapping[str, list[str]] | None = None,
        sensitivity: str = "internal",
        valid_from: str | None = None,
        valid_until: str | None = None,
        relations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = "2026-01-01T00:00:00Z"
        value: dict[str, Any] = {
            "schema_version": 2,
            "id": record_id,
            "type": knowledge_type,
            "summary": f"Synthetic {record_id}",
            "content": marker,
            "keywords": [marker],
            "metadata": {},
            "scope": scope,
            "owner": "synthetic-fixture",
            "evidence": {"kind": "synthetic"},
            "confidence": 0.8,
            "status": status,
            "sensitivity": sensitivity,
            "applicability": {
                "roles": [role] if role else [],
                "knowledge_types": [knowledge_type],
                "constraints": dict(constraints or {}),
                "valid_from": valid_from,
                "valid_until": valid_until,
            },
            "relations": sorted(
                relations or [],
                key=lambda item: (
                    item["kind"],
                    item["target_id"],
                    item["scope"],
                    item.get("project_id") or "",
                ),
            ),
            "created_at": now,
            "updated_at": now,
        }
        if scope == "project":
            value["project_id"] = project_id
        if status == "approved":
            value.update(
                {
                    "approved_by": "synthetic-manager",
                    "approved_at": now,
                    "validation": "synthetic validation",
                }
            )
        elif status == "candidate":
            pass
        elif status == "rejected":
            value.update(
                {
                    "rejected_by": "synthetic-manager",
                    "rejected_at": now,
                    "rejection_reason": "synthetic rejection",
                }
            )
        elif status == "obsolete":
            value.update(
                {
                    "obsolete_at": now,
                    "obsolete_reason": "synthetic obsolescence",
                }
            )
        governance.validate_record(value)
        return value

    def write(self, record: Mapping[str, Any]) -> Path:
        status = str(record["status"])
        path = self.knowledge / opc_memory.STATUS_DIRS[status] / f"{record['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(governance.strict_json_bytes(record))
        return path

    def commit(self, message: str = "synthetic governed knowledge") -> str:
        if not shutil.which("git"):
            self.skipTest("Git is required for provenance tests")
        if not (self.knowledge / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main", str(self.knowledge)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(self.knowledge), "config", "user.name", "Synthetic Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(self.knowledge), "config", "user.email", "synthetic@example.invalid"],
                check=True,
            )
        subprocess.run(
            ["git", "-C", str(self.knowledge), "add", "-A", "--", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.knowledge), "commit", "-m", message],
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", str(self.knowledge), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def test_scope_role_type_applicability_sensitivity_and_staleness_are_hard_filters(self) -> None:
        records = [
            self.record("exp-global"),
            self.record("exp-project-a", scope="project", project_id="project-a"),
            self.record("exp-project-b", scope="project", project_id="project-b"),
            self.record("exp-qa", role="qa", knowledge_type="procedure"),
            self.record("exp-linux", constraints={"platform": ["linux"]}),
            self.record("exp-restricted", sensitivity="restricted"),
            self.record("exp-stale", valid_until="2026-01-02T00:00:00Z"),
        ]
        for record in records:
            self.write(record)
        self.commit()

        missing = self.backend.query_context(
            "governance-marker", at="2026-01-01T12:00:00Z"
        )
        self.assertEqual(
            {item["id"] for item in missing["records"]},
            {"exp-global", "exp-stale"},
        )
        for_a = self.backend.query_context(
            "governance-marker",
            project_id="project-a",
            role="qa",
            memory_type="procedure",
            applicability={"platform": "linux"},
            at="2026-01-01T12:00:00Z",
        )
        self.assertEqual([item["id"] for item in for_a["records"]], ["exp-qa"])
        at_later_time = self.backend.query_context(
            "governance-marker",
            project_id="project-a",
            role="developer",
            applicability={"platform": "windows"},
            at="2026-02-01T00:00:00Z",
        )
        self.assertEqual(
            {item["id"] for item in at_later_time["records"]},
            {"exp-project-a", "exp-global"},
        )
        self.assertIn("stale", at_later_time["omitted_summary"]["reason_codes"])
        self.assertIn(
            "sensitivity_not_authorized",
            at_later_time["omitted_summary"]["reason_codes"],
        )

    def test_query_time_requires_timezone_and_respects_window_boundaries(self) -> None:
        windowed = self.record(
            "exp-windowed",
            valid_from="2026-01-01T00:00:00Z",
            valid_until="2026-01-02T00:00:00Z",
        )
        self.write(windowed)
        self.commit()
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "timezone-aware"):
            self.backend.query_context("governance-marker", at="2026-01-01T00:00:00")
        at_start_z = self.backend.query_context(
            "governance-marker", at="2026-01-01T00:00:00Z"
        )
        at_start_offset = self.backend.query_context(
            "governance-marker", at="2026-01-01T08:00:00+08:00"
        )
        self.assertEqual(
            [item["id"] for item in at_start_z["records"]],
            [item["id"] for item in at_start_offset["records"]],
        )
        self.assertEqual([windowed["id"]], [item["id"] for item in at_start_z["records"]])
        before = self.backend.query_context(
            "governance-marker", at="2025-12-31T23:59:59Z"
        )
        self.assertEqual([], before["records"])
        self.assertIn("not_yet_applicable", before["omitted_summary"]["reason_codes"])
        before_end = self.backend.query_context(
            "governance-marker", at="2026-01-01T23:59:59.999999Z"
        )
        self.assertEqual([windowed["id"]], [item["id"] for item in before_end["records"]])
        at_end = self.backend.query_context(
            "governance-marker", at="2026-01-02T00:00:00Z"
        )
        self.assertEqual([], at_end["records"])
        self.assertIn("stale", at_end["omitted_summary"]["reason_codes"])

    def test_unresolved_conflict_withholds_both_bodies_and_preserves_citations(self) -> None:
        secret_a = "synthetic-private-body-a"
        secret_b = "synthetic-private-body-b"
        global_rule = self.record(
            "exp-global-conflict",
            marker=secret_a,
            relations=[
                self.relation(
                    "conflicts",
                    "exp-project-conflict",
                    scope="project",
                    project_id="project-a",
                )
            ],
        )
        project_rule = self.record(
            "exp-project-conflict",
            marker=secret_b,
            scope="project",
            project_id="project-a",
        )
        self.write(global_rule)
        self.write(project_rule)
        self.commit()

        no_project = self.backend.query_context(secret_a)
        self.assertEqual([item["id"] for item in no_project["records"]], [global_rule["id"]])
        governed = self.backend.query_context("synthetic-private-body", project_id="project-a")
        self.assertEqual(governed["records"], [])
        self.assertEqual(len(governed["conflicts"]), 1)
        citations = governed["conflicts"][0]["citations"]
        self.assertEqual(
            {item["record_id"] for item in citations},
            {global_rule["id"], project_rule["id"]},
        )
        serialized = json.dumps(governed, ensure_ascii=False)
        self.assertNotIn(secret_a, serialized)
        self.assertNotIn(secret_b, serialized)
        self.assertNotIn(str(self.knowledge), serialized)

    def test_cycles_and_missing_targets_fail_only_related_records(self) -> None:
        records = [
            self.record(
                "exp-cycle-a",
                relations=[self.relation("superseded_by", "exp-cycle-b")],
            ),
            self.record(
                "exp-cycle-b",
                relations=[self.relation("superseded_by", "exp-cycle-a")],
            ),
            self.record(
                "exp-broken",
                relations=[self.relation("invalidated_by", "exp-missing")],
            ),
            self.record("exp-unrelated"),
        ]
        for record in records:
            self.write(record)
        self.commit()
        result = self.backend.query_context("governance-marker")
        self.assertEqual([item["id"] for item in result["records"]], ["exp-unrelated"])
        reasons = {
            item["record_id"]: set(item["reason_codes"])
            for item in result["omissions"]
        }
        self.assertIn("relation_cycle", reasons["exp-cycle-a"])
        self.assertIn("relation_cycle", reasons["exp-cycle-b"])
        self.assertIn("relation_target_missing", reasons["exp-broken"])

    def test_relation_cycles_are_iterative_bounded_and_isolated_after_hard_filters(self) -> None:
        size = 1201
        ring = {
            f"exp-ring-{index}": {f"exp-ring-{(index + 1) % size}"}
            for index in range(size)
        }
        self.assertEqual(set(ring), governance.relation_cycles(ring))
        chain = {
            f"exp-chain-{index}": {f"exp-chain-{index + 1}"}
            for index in range(size)
        }
        self.assertEqual(set(), governance.relation_cycles(chain))

        for index in range(size):
            relations = (
                [self.relation("invalidated_by", f"exp-rejected-{index + 1}")]
                if index + 1 < size
                else []
            )
            self.write(
                self.record(
                    f"exp-rejected-{index}",
                    status="rejected",
                    relations=relations,
                )
            )
        valid = self.record("exp-independent-valid")
        self.write(valid)
        self.commit("large rejected relation chain")
        with mock.patch.object(
            opc_memory,
            "relation_cycles",
            wraps=governance.relation_cycles,
        ) as cycle_check:
            result = self.backend.query_context("governance-marker")
        self.assertEqual([valid["id"]], [item["id"] for item in result["records"]])
        self.assertEqual({}, cycle_check.call_args.args[0])

    def test_supersession_invalidation_and_status_chains_exclude_old_guidance(self) -> None:
        records = [
            self.record("exp-old"),
            self.record(
                "exp-new", relations=[self.relation("supersedes", "exp-old")]
            ),
            self.record("exp-invalid-target"),
            self.record(
                "exp-invalidator",
                relations=[self.relation("invalidates", "exp-invalid-target")],
            ),
            self.record("exp-obsolete", status="obsolete"),
            self.record("exp-rejected", status="rejected"),
        ]
        for record in records:
            self.write(record)
        self.commit()
        result = self.backend.query_context("governance-marker")
        self.assertEqual(
            {item["id"] for item in result["records"]},
            {"exp-new", "exp-invalidator"},
        )
        reasons = {
            item["record_id"]: set(item["reason_codes"])
            for item in result["omissions"]
        }
        self.assertIn("superseded", reasons["exp-old"])
        self.assertIn("invalidated", reasons["exp-invalid-target"])

    def test_provider_rank_cannot_override_canonical_order_or_current_head(self) -> None:
        project = self.record(
            "exp-ranked-project", scope="project", project_id="project-a"
        )
        global_record = self.record("exp-ranked-global")
        self.write(project)
        self.write(global_record)
        self.commit("first canonical head")
        project_meta = self.backend.source_metadata(
            f"experiences/approved/{project['id']}.json"
        )
        global_meta = self.backend.source_metadata(
            f"experiences/approved/{global_record['id']}.json"
        )
        provider = StaticProvider(
            [
                {"score": 0.999, "metadata": {"record_id": global_record["id"], **global_meta}},
                {"score": 0.001, "metadata": {"record_id": project["id"], **project_meta}},
            ]
        )
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        result = service.query_context("semantic-only", project_id="project-a")
        self.assertEqual(
            [item["id"] for item in result["records"]],
            [global_record["id"], project["id"]],
        )
        (self.knowledge / "head-marker.txt").write_text("new head\n", encoding="utf-8")
        self.commit("advance canonical head")
        stale = service.query_context("semantic-only", project_id="project-a")
        self.assertEqual(stale["records"], [])

    def test_schema1_compatibility_preview_migration_backup_and_idempotence(self) -> None:
        schema1 = self.record("exp-schema-one", status="candidate")
        schema1["schema_version"] = 1
        schema1.pop("sensitivity")
        schema1.pop("applicability")
        schema1.pop("relations")
        governance.validate_record(schema1)
        source = self.write(schema1)
        before = source.read_bytes()
        preview = self.backend.schema_migration_plan(
            record_id=schema1["id"], backup_root=self.backups
        )
        self.assertTrue(preview["zero_write"])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(self.backups.iterdir()), [])
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "MIGRATION_PLAN_CHANGED"):
            self.backend.apply_schema_migration(
                record_id=schema1["id"],
                backup_root=self.backups,
                plan_token="0" * 64,
            )
        applied = self.backend.apply_schema_migration(
            record_id=schema1["id"],
            backup_root=self.backups,
            plan_token=preview["plan_token"],
        )
        self.assertTrue(applied["changed"])
        migrated = self.backend._load_record(source)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["sensitivity"], "internal")
        backup = self.backups / applied["backup_ref"]
        self.assertEqual(backup.read_bytes(), before)
        second_preview = self.backend.schema_migration_plan(
            record_id=schema1["id"], backup_root=self.backups
        )
        self.assertEqual(second_preview["pending_count"], 0)
        second = self.backend.apply_schema_migration(
            record_id=schema1["id"],
            backup_root=self.backups,
            plan_token=second_preview["plan_token"],
        )
        self.assertFalse(second["changed"])
        self.assertTrue(second["idempotent"])

    def test_migration_preview_rejects_cross_status_duplicates_before_any_write(self) -> None:
        record_id = "exp-migration-duplicate"
        legacy = self.record(record_id, status="approved")
        legacy["schema_version"] = 1
        for field in ("sensitivity", "applicability", "relations"):
            legacy.pop(field)
        candidate = self.record(record_id, status="candidate")
        source_a = self.write(legacy)
        source_b = self.write(candidate)
        before = {source_a: source_a.read_bytes(), source_b: source_b.read_bytes()}
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "duplicate record id"):
            self.backend.schema_migration_plan(
                record_id=record_id, backup_root=self.backups
            )
        obsolete = self.record(record_id, status="obsolete")
        source_c = self.write(obsolete)
        before[source_c] = source_c.read_bytes()
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "duplicate record id"):
            self.backend.schema_migration_plan(
                record_id=record_id, backup_root=self.backups
            )
        self.assertEqual([], list(self.backups.iterdir()))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_migration_inventory_limit_and_apply_single_identity_fail_closed(self) -> None:
        oversized = mock.Mock()
        oversized.glob.return_value = [
            Path(f"exp-over-limit-{index}.json")
            for index in range(governance.MAX_RECORDS + 1)
        ]
        with mock.patch.object(self.backend, "_folder", return_value=oversized):
            with self.assertRaisesRegex(opc_memory.OpcMemoryError, "per-status"):
                self.backend.schema_migration_plan(backup_root=self.backups)

        forged_preview = {
            "plan_token": "exact-token",
            "items": [
                {"record_id": "exp-one"},
                {"record_id": "exp-one"},
            ],
        }
        with mock.patch.object(
            self.backend, "schema_migration_plan", return_value=forged_preview
        ):
            with self.assertRaisesRegex(opc_memory.OpcMemoryError, "exactly one unique"):
                self.backend.apply_schema_migration(
                    record_id="exp-one",
                    backup_root=self.backups,
                    plan_token="exact-token",
                )
        self.assertEqual([], list(self.backups.iterdir()))

    def test_migration_failure_preserves_source_and_removes_owned_backup(self) -> None:
        import opc_feedback

        legacy = self.record("exp-migration-rollback", status="candidate")
        legacy["schema_version"] = 1
        legacy.pop("sensitivity")
        legacy.pop("applicability")
        legacy.pop("relations")
        source = self.write(legacy)
        original = source.read_bytes()
        preview = self.backend.schema_migration_plan(backup_root=self.backups)
        with mock.patch.object(
            opc_feedback,
            "_atomic_write_feedback",
            side_effect=opc_feedback.FeedbackError("synthetic canonical write failure"),
        ):
            with self.assertRaisesRegex(opc_memory.OpcMemoryError, "without a canonical partial"):
                self.backend.apply_schema_migration(
                    record_id=legacy["id"],
                    backup_root=self.backups,
                    plan_token=preview["plan_token"],
                )
        self.assertEqual(original, source.read_bytes())
        self.assertEqual([], list(self.backups.iterdir()))

    def test_duplicate_id_in_three_statuses_never_reenters_context(self) -> None:
        record = self.record("exp-duplicate-three")
        self.write(record)
        for status in ("candidate", "obsolete"):
            duplicate = self.record("exp-duplicate-three", status=status)
            self.write(duplicate)
        self.commit()
        context = self.backend.query_context("governance-marker")
        self.assertEqual([], context["records"])
        self.assertEqual(3, context["omitted_summary"]["invalid_record_count"])

    def test_malformed_approved_lifecycle_is_redacted_as_invalid_omission(self) -> None:
        valid = self.record("exp-valid-lifecycle")
        invalid_by = self.record("exp-invalid-approved-by")
        invalid_by["approved_by"] = ["not", "a", "string"]
        invalid_by["content"] = "secret-invalid-approved-by-body"
        invalid_validation = self.record("exp-invalid-validation")
        invalid_validation["validation"] = 7
        invalid_validation["content"] = "secret-invalid-validation-body"
        self.write(valid)
        self.write(invalid_by)
        self.write(invalid_validation)
        self.commit("invalid lifecycle fixtures")

        context = self.backend.query_context("governance-marker")
        self.assertEqual([valid["id"]], [item["id"] for item in context["records"]])
        invalid = {
            item["record_id"]: item["reason_codes"]
            for item in context["omissions"]
        }
        self.assertEqual(["record_invalid"], invalid[invalid_by["id"]])
        self.assertEqual(["record_invalid"], invalid[invalid_validation["id"]])
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn(invalid_by["content"], serialized)
        self.assertNotIn(invalid_validation["content"], serialized)

    def test_curation_requires_exact_preview_and_exposes_narrow_git_paths(self) -> None:
        candidate = self.record("exp-curate", status="candidate")
        target = self.record("exp-curate-target")
        self.write(candidate)
        self.write(target)
        self.commit("candidate baseline")
        unrelated = self.knowledge / "company" / "local-note.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("unrelated user change\n", encoding="utf-8")
        relation = self.relation("conflicts", target["id"])
        preview = self.backend.curation_plan(
            candidate["id"],
            manager_approval="manager-approval-01",
            set_status="approved",
            validation="synthetic replay passed",
            relations=[relation],
        )
        self.assertTrue(preview["zero_write"])
        self.assertTrue(
            {
                "approved_at",
                "approved_by",
                "relations",
                "status",
                "updated_at",
                "validation",
            }.issubset(preview["changed_fields"])
        )
        self.assertEqual(
            set(preview["transition_paths"]),
            {
                f"experiences/candidates/{candidate['id']}.json",
                f"experiences/approved/{candidate['id']}.json",
            },
        )
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "CURATION_PLAN_CHANGED"):
            self.backend.apply_curation(
                candidate["id"],
                plan_token="0" * 64,
                transition_at=preview["transition_at"],
                manager_approval="manager-approval-01",
                set_status="approved",
                validation="synthetic replay passed",
                relations=[relation],
            )
        result = self.backend.apply_curation(
            candidate["id"],
            plan_token=preview["plan_token"],
            transition_at=preview["transition_at"],
            manager_approval="manager-approval-01",
            set_status="approved",
            validation="synthetic replay passed",
            relations=[relation],
        )
        self.assertTrue(result["git_commit_required"])
        self.assertFalse(result["provider_write_performed"])
        destination = self.knowledge / preview["destination_path"]
        self.assertEqual(
            preview["proposed_sha256"],
            opc_memory.sha256_bytes(destination.read_bytes()),
        )
        applied_bytes = destination.read_bytes()
        with self.assertRaises(opc_memory.OpcMemoryError):
            self.backend.apply_curation(
                candidate["id"],
                plan_token=preview["plan_token"],
                transition_at=preview["transition_at"],
                manager_approval="manager-approval-01",
                set_status="approved",
                validation="synthetic replay passed",
                relations=[relation],
            )
        self.assertEqual(applied_bytes, destination.read_bytes())
        subprocess.run(
            [
                "git", "-C", str(self.knowledge), "add", "-A", "--",
                *result["git_stage_pathspecs"],
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.knowledge), "commit", "--only", "-m",
                "memory: approve exp-curate", "--", *result["git_stage_pathspecs"],
            ],
            check=True,
            capture_output=True,
        )
        changed = subprocess.run(
            [
                "git", "-C", str(self.knowledge), "show", "--pretty=", "--name-only",
                "--no-renames", "HEAD",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        self.assertEqual(set(changed), set(result["git_stage_pathspecs"]))
        self.assertIn("company/local-note.md", subprocess.run(
            ["git", "-C", str(self.knowledge), "status", "--short", "-uall"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.replace("\\", "/"))

    def test_in_place_relation_curation_reproduces_preview_bytes(self) -> None:
        record = self.record("exp-in-place")
        target = self.record("exp-in-place-target")
        source = self.write(record)
        self.write(target)
        relation = self.relation("conflicts", target["id"])
        preview = self.backend.curation_plan(
            record["id"],
            manager_approval="manager-in-place",
            relations=[relation],
            transition_at="2026-02-03T04:05:06+08:00",
        )
        self.assertEqual("2026-02-02T20:05:06Z", preview["transition_at"])
        self.assertEqual(["relations", "updated_at"], preview["changed_fields"])
        result = self.backend.apply_curation(
            record["id"],
            plan_token=preview["plan_token"],
            manager_approval="manager-in-place",
            relations=[relation],
            transition_at=preview["transition_at"],
        )
        self.assertEqual(preview["proposed_sha256"], result["proposed_sha256"])
        self.assertEqual(
            preview["proposed_sha256"], opc_memory.sha256_bytes(source.read_bytes())
        )

    def test_curation_move_failure_rolls_back_owned_destination(self) -> None:
        import opc_feedback

        candidate = self.record("exp-curation-rollback", status="candidate")
        source = self.write(candidate)
        original = source.read_bytes()
        preview = self.backend.curation_plan(
            candidate["id"],
            manager_approval="manager-approval-rollback",
            set_status="approved",
            validation="synthetic replay passed",
        )
        real_unlink = opc_feedback._BoundDirectory.unlink_owned

        def fail_only_source(bound, name, identity):
            if (
                name == source.name
                and bound.path.resolve(strict=True) == source.parent.resolve(strict=True)
            ):
                return False
            return real_unlink(bound, name, identity)

        with mock.patch.object(
            opc_feedback._BoundDirectory,
            "unlink_owned",
            autospec=True,
            side_effect=fail_only_source,
        ):
            with self.assertRaisesRegex(opc_memory.OpcMemoryError, "source changed"):
                self.backend.apply_curation(
                    candidate["id"],
                    plan_token=preview["plan_token"],
                    transition_at=preview["transition_at"],
                    manager_approval="manager-approval-rollback",
                    set_status="approved",
                    validation="synthetic replay passed",
                )
        self.assertEqual(original, source.read_bytes())
        self.assertFalse(
            (self.knowledge / opc_memory.STATUS_DIRS["approved"] / source.name).exists()
        )

    def test_curation_parent_replacement_is_blocked_or_detected_and_rolled_back(self) -> None:
        import opc_feedback

        candidate = self.record("exp-curation-parent-swap", status="candidate")
        source = self.write(candidate)
        original = source.read_bytes()
        unrelated = self.knowledge / "company" / "unrelated.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("preserve\n", encoding="utf-8")
        preview = self.backend.curation_plan(
            candidate["id"],
            manager_approval="manager-approval-parent-swap",
            set_status="approved",
            validation="synthetic replay passed",
        )
        destination = (
            self.knowledge / opc_memory.STATUS_DIRS["approved"] / source.name
        )
        displaced = source.parent.with_name("candidates-displaced")
        real_read = opc_feedback._BoundDirectory.read_bytes
        outcome = {"blocked": False, "swapped": False}

        def replace_parent_at_source_read(bound, name, **kwargs):
            if (
                name == source.name
                and destination.exists()
                and os.path.samefile(bound.path, source.parent)
            ):
                try:
                    source.parent.rename(displaced)
                    source.parent.mkdir()
                    outcome["swapped"] = True
                except OSError as exc:
                    outcome["blocked"] = True
                    raise opc_feedback.FeedbackError(
                        "source parent mutation was blocked"
                    ) from exc
            return real_read(bound, name, **kwargs)

        try:
            with mock.patch.object(
                opc_feedback._BoundDirectory,
                "read_bytes",
                autospec=True,
                side_effect=replace_parent_at_source_read,
            ):
                with self.assertRaises(opc_memory.OpcMemoryError):
                    self.backend.apply_curation(
                        candidate["id"],
                        plan_token=preview["plan_token"],
                        transition_at=preview["transition_at"],
                        manager_approval="manager-approval-parent-swap",
                        set_status="approved",
                        validation="synthetic replay passed",
                    )
        finally:
            if outcome["swapped"]:
                source.parent.rmdir()
                displaced.rename(source.parent)

        self.assertTrue(outcome["blocked"] or outcome["swapped"])
        self.assertEqual(original, source.read_bytes())
        self.assertFalse(destination.exists())
        self.assertEqual("preserve\n", unrelated.read_text(encoding="utf-8"))

    def test_nonfinite_oversized_hardlink_and_linked_roots_fail_closed(self) -> None:
        invalid = self.record("exp-nonfinite", status="candidate")
        invalid["confidence"] = math.inf
        with self.assertRaises(governance.GovernanceError):
            governance.validate_record(invalid)
        oversized = self.record("exp-oversized", status="candidate")
        oversized["content"] = "x" * (governance.MAX_TEXT + 1)
        with self.assertRaises(governance.GovernanceError):
            governance.validate_record(oversized)

        linked_record = self.record("exp-hardlinked", status="candidate")
        source = self.write(linked_record)
        alias = self.base / "hardlink.json"
        try:
            os.link(source, alias)
        except OSError:
            self.skipTest("hard links are unavailable")
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "uniquely linked"):
            self.backend._load_record(source)
        alias.unlink()

        target = self.base / "linked-target"
        target.mkdir()
        linked = self.base / "linked-root"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("directory links are unavailable")
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "symlink|reparse"):
            opc_memory.FileGitBackend(linked)

    @unittest.skipUnless(
        importlib.util.find_spec("jsonschema") is not None,
        "jsonschema is optional in the dependency-free core job",
    )
    def test_schema_and_runtime_accept_v1_v2_and_reject_the_same_bad_relation(self) -> None:
        from jsonschema import Draft202012Validator, FormatChecker

        schema = json.loads(
            (
                ROOT
                / "plugins"
                / "codex-opc-team"
                / "assets"
                / "knowledge-template"
                / "schemas"
                / "experience.schema.json"
            ).read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        v2 = self.record("exp-schema-v2", status="candidate")
        v1 = copy.deepcopy(v2)
        v1["schema_version"] = 1
        for field in ("sensitivity", "applicability", "relations"):
            v1.pop(field)
        self.assertTrue(validator.is_valid(v1))
        self.assertTrue(validator.is_valid(v2))
        governance.validate_record(v1)
        governance.validate_record(v2)
        invalid = copy.deepcopy(v2)
        invalid["relations"] = [
            self.relation(
                "conflicts", "exp-other", scope="project", project_id=None
            )
        ]
        self.assertFalse(validator.is_valid(invalid))
        with self.assertRaises(governance.GovernanceError):
            governance.validate_record(invalid)

        lifecycle_cases: list[tuple[str, dict[str, Any]]] = []
        for label, field, value in (
            ("approved_by_list", "approved_by", ["manager"]),
            ("validation_number", "validation", 7),
            ("rejected_by_number", "rejected_by", 7),
            ("obsolete_reason_empty", "obsolete_reason", ""),
            ("approved_by_oversized", "approved_by", "x" * 4097),
        ):
            case = self.record(f"exp-{label.replace('_', '-')}", status="candidate")
            case[field] = value
            lifecycle_cases.append((label, case))
        naive_time = self.record("exp-naive-approved-time")
        naive_time["approved_at"] = "2026-01-01T00:00:00"
        lifecycle_cases.append(("approved_at_naive", naive_time))
        for label, case in lifecycle_cases:
            with self.subTest(label=label):
                self.assertFalse(validator.is_valid(case))
                with self.assertRaises(governance.GovernanceError):
                    governance.validate_record(case)

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases only")
    def test_windows_short_normal_directory_alias_keeps_identity(self) -> None:
        import ctypes

        record = self.record("exp-windows-short-alias", status="candidate")
        self.write(record)
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short.restype = ctypes.c_uint
        source = str(self.knowledge.resolve(strict=True))
        size = get_short(source, None, 0)
        if size == 0:
            self.skipTest("8.3 aliases are unavailable")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = get_short(source, buffer, len(buffer))
        if written == 0 or Path(buffer.value) == Path(source):
            self.skipTest("this volume did not produce a distinct 8.3 alias")
        alias_backend = opc_memory.FileGitBackend(Path(buffer.value))
        self.assertTrue(os.path.samefile(alias_backend.root, self.backend.root))
        alias_record = (
            Path(buffer.value)
            / opc_memory.STATUS_DIRS["candidate"]
            / f"{record['id']}.json"
        )
        self.assertEqual(record["id"], alias_backend._load_record(alias_record)["id"])


if __name__ == "__main__":
    unittest.main()
