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
                "valid_from": None,
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
                manager_approval="manager-approval-01",
                set_status="approved",
                validation="synthetic replay passed",
                relations=[relation],
            )
        result = self.backend.apply_curation(
            candidate["id"],
            plan_token=preview["plan_token"],
            manager_approval="manager-approval-01",
            set_status="approved",
            validation="synthetic replay passed",
            relations=[relation],
        )
        self.assertTrue(result["git_commit_required"])
        self.assertFalse(result["provider_write_performed"])
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
            if name == source.name and bound.path == source.parent:
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
                    manager_approval="manager-approval-rollback",
                    set_status="approved",
                    validation="synthetic replay passed",
                )
        self.assertEqual(original, source.read_bytes())
        self.assertFalse(
            (self.knowledge / opc_memory.STATUS_DIRS["approved"] / source.name).exists()
        )

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
        from jsonschema import Draft202012Validator

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
        validator = Draft202012Validator(schema)
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

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases only")
    def test_windows_short_normal_directory_alias_keeps_identity(self) -> None:
        import ctypes

        self.knowledge.mkdir(exist_ok=True)
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


if __name__ == "__main__":
    unittest.main()
