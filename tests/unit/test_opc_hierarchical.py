from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_hierarchical  # noqa: E402
import opc_memory  # noqa: E402


class FailingProvider:
    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        return None

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("synthetic provider failure")


class SlowProvider(FailingProvider):
    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        time.sleep(0.2)
        return []


class StaticProvider(FailingProvider):
    def __init__(self, record_id: str):
        self.record_id = record_id

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        return [{"metadata": {"record_id": self.record_id}, "score": 999}]


class HierarchicalRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("git"):
            self.skipTest("Git is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.knowledge = root / "knowledge"
        self.data = root / "private-data"
        self.backend = opc_memory.FileGitBackend(self.knowledge)
        self.backend.ensure_layout()
        subprocess.run(["git", "init", "-b", "main", str(self.knowledge)], check=True, capture_output=True)

    def add_approved(
        self,
        *,
        summary: str,
        content: str,
        scope: str = "project",
        project_id: str | None = "project-alpha",
        memory_type: str = "decision",
        roles: list[str] | None = None,
        relations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        candidate = self.backend.add_candidate(
            memory_type=memory_type,
            summary=summary,
            content=content,
            keywords=summary.lower().split(),
            scope=scope,
            project_id=project_id if scope == "project" else None,
            applicable_roles=roles or ["developer"],
            relations=relations,
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

    def build(self) -> opc_hierarchical.HierarchicalRecall:
        index = opc_hierarchical.HierarchicalIndex(self.backend, self.data)
        plan = index.preview()
        index.build(approval_token=plan["approval_token"])
        return opc_hierarchical.HierarchicalRecall(self.backend, self.data)

    def test_preview_is_zero_write_and_build_is_private(self) -> None:
        self.add_approved(summary="alpha deployment", content="canonical-only-body")
        self.commit()
        index = opc_hierarchical.HierarchicalIndex(self.backend, self.data)
        plan = index.preview()
        self.assertEqual(plan["writes_performed"], 0)
        self.assertFalse(self.data.exists())
        result = index.build(approval_token=plan["approval_token"])
        self.assertEqual(result["writes_performed"], 1)
        self.assertTrue(index.path.is_file())
        self.assertNotIn("canonical-only-body", index.path.read_text(encoding="utf-8"))
        self.assertEqual((self.data / ".opc" / ".gitignore").read_text(), "*\n!.gitignore\n")

    def test_hierarchical_packet_has_canonical_l2_and_trace_has_no_body(self) -> None:
        record = self.add_approved(summary="alpha deployment", content="canonical-only-body")
        self.commit()
        result = self.build().query(
            "alpha deployment", project_id="project-alpha", role="developer", limit=5
        )
        packet = result["context_packet"]
        trace = result["recall_trace"]
        self.assertEqual(packet["mode"], "hierarchical-file-git")
        self.assertEqual(packet["decisions"][0]["record_id"], record["id"])
        self.assertEqual(packet["decisions"][0]["content"], "canonical-only-body")
        self.assertNotIn("canonical-only-body", json.dumps(trace))
        self.assertEqual(trace["final_leaves"], [record["id"]])
        self.assertTrue(packet["citations"][0]["source_commit"])

    def test_cross_project_and_obsolete_are_never_injected(self) -> None:
        alpha = self.add_approved(summary="shared deploy", content="alpha body")
        beta = self.add_approved(
            summary="shared deploy", content="beta body", project_id="project-beta"
        )
        obsolete = self.add_approved(summary="shared deploy", content="obsolete body")
        self.backend.mark_obsolete(obsolete["id"], reason="synthetic obsolete")
        self.commit()
        result = self.build().query(
            "shared deploy", project_id="project-alpha", role="developer", limit=5
        )
        ids = [item["record_id"] for item in result["context_packet"]["decisions"]]
        self.assertEqual(ids, [alpha["id"]])
        self.assertNotIn(beta["id"], ids)
        reasons = {item.get("record_id"): item.get("reason_codes") for item in result["recall_trace"]["discards"]}
        self.assertIn("project_scope_mismatch", reasons[beta["id"]])
        self.assertIn("obsolete", reasons[obsolete["id"]])

    def test_unresolved_conflict_emits_two_citations_and_no_body(self) -> None:
        left_candidate = self.backend.add_candidate(
            memory_type="decision",
            summary="conflict alpha left",
            content="left body must be withheld",
            scope="global",
            applicable_roles=["developer"],
        )
        right_candidate = self.backend.add_candidate(
            memory_type="decision",
            summary="conflict alpha right",
            content="right body must be withheld",
            scope="global",
            applicable_roles=["developer"],
        )
        for candidate, target in (
            (left_candidate, right_candidate),
            (right_candidate, left_candidate),
        ):
            path = self.knowledge / candidate["_source_path"]
            value = json.loads(path.read_text(encoding="utf-8"))
            value["relations"] = [
                {
                    "kind": "conflicts",
                    "target_id": target["id"],
                    "scope": "global",
                    "project_id": None,
                }
            ]
            path.write_bytes(opc_memory.canonical_record_bytes(value))
            self.backend.approve(candidate["id"], approved_by="manager", validation="synthetic")
        self.commit()
        result = self.build().query(
            "conflict alpha", project_id="project-alpha", role="developer"
        )
        packet = result["context_packet"]
        self.assertEqual(packet["decisions"], [])
        self.assertEqual(len(packet["conflicts"]), 1)
        self.assertEqual(len(packet["conflicts"][0]["citations"]), 2)
        rendered = json.dumps(packet["conflicts"])
        self.assertNotIn("left body", rendered)
        self.assertNotIn("right body", rendered)

    def test_missing_and_stale_index_degrade_to_flat_file_git(self) -> None:
        self.add_approved(summary="alpha deployment", content="body")
        self.commit()
        recall = opc_hierarchical.HierarchicalRecall(self.backend, self.data)
        missing = recall.query("alpha", project_id="project-alpha", role="developer")
        self.assertEqual(missing["context_packet"]["mode"], "flat-file-git-fallback")
        recall = self.build()
        self.add_approved(summary="new alpha", content="new body")
        self.commit("canonical changed")
        stale = recall.query("alpha", project_id="project-alpha", role="developer")
        self.assertEqual(stale["context_packet"]["mode"], "flat-file-git-fallback")

    def test_delete_and_rebuild_require_exact_tokens(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        self.build()
        index = opc_hierarchical.HierarchicalIndex(self.backend, self.data)
        delete = index.delete_preview()
        with self.assertRaises(opc_hierarchical.HierarchicalError):
            index.delete(approval_token="0" * 64)
        index.delete(approval_token=delete["approval_token"])
        self.assertFalse(index.path.exists())
        plan = index.preview()
        index.build(approval_token=plan["approval_token"])
        self.assertTrue(index.path.exists())

    def test_provider_failure_timeout_and_disagreement_do_not_block(self) -> None:
        alpha = self.add_approved(summary="alpha", content="body")
        beta = self.add_approved(summary="alpha", content="beta", project_id="project-beta")
        self.commit()
        self.build()
        for provider, timeout in ((FailingProvider(), 1), (SlowProvider(), 0.01), (StaticProvider(beta["id"]), 1)):
            recall = opc_hierarchical.HierarchicalRecall(
                self.backend,
                self.data,
                provider=provider,
                provider_enabled=True,
                timeout_seconds=timeout,
            )
            result = recall.query("alpha", project_id="project-alpha", role="developer")
            ids = [item["record_id"] for item in result["context_packet"]["decisions"]]
            self.assertEqual(ids, [alpha["id"]])

    def test_budget_truncation_is_explicit(self) -> None:
        self.add_approved(summary="alpha one", content="A" * 300)
        self.add_approved(summary="alpha two", content="B" * 300)
        self.commit()
        result = self.build().query(
            "alpha", project_id="project-alpha", role="developer", budget_tokens=100
        )
        packet = result["context_packet"]
        self.assertLessEqual(packet["budget"]["used_tokens"], 100)
        self.assertIn("budget_exhausted", packet["omitted_summary"]["reason_codes"])
        self.assertGreater(packet["omitted_summary"]["count"], 0)

    def test_canonical_change_between_navigation_and_injection_is_rejected(self) -> None:
        record = self.add_approved(summary="alpha", content="body")
        self.commit()
        recall = self.build()
        original = self.backend.read_authoritative

        def changed(**values: Any) -> dict[str, Any]:
            path = self.knowledge / record["_source_path"]
            document = json.loads(path.read_text(encoding="utf-8"))
            document["content"] = "changed after navigation"
            path.write_text(json.dumps(document), encoding="utf-8")
            return original(**values)

        with patch.object(self.backend, "read_authoritative", side_effect=changed):
            result = recall.query("alpha", project_id="project-alpha", role="developer")
        self.assertEqual(result["context_packet"]["decisions"], [])
        self.assertIn("l2_revalidation_failed", {reason for item in result["recall_trace"]["discards"] for reason in item.get("reason_codes", [])})

    def test_hardlink_and_symlink_index_are_rejected(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        self.build()
        index = opc_hierarchical.HierarchicalIndex(self.backend, self.data)
        linked = index.directory / "linked.json"
        try:
            os.link(index.path, linked)
        except OSError:
            self.skipTest("hard links are unavailable")
        with self.assertRaises(opc_hierarchical.HierarchicalError):
            index.load()

    def test_symlinked_data_root_and_reparse_boundary_are_rejected(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        target = Path(self.temporary.name) / "real-private-data"
        target.mkdir()
        linked = Path(self.temporary.name) / "linked-private-data"
        try:
            os.symlink(target, linked, target_is_directory=True)
        except OSError:
            linked = None
        if linked is not None:
            with self.assertRaises(opc_memory.OpcMemoryError):
                opc_hierarchical.HierarchicalIndex(self.backend, linked)
        with patch("opc_memory._is_reparse", return_value=True):
            with self.assertRaises(opc_memory.OpcMemoryError):
                opc_hierarchical.HierarchicalIndex(
                    self.backend, Path(self.temporary.name) / "new-private-data"
                )

    def test_duplicate_ids_across_statuses_fail_build(self) -> None:
        record = self.add_approved(summary="alpha", content="body")
        duplicate = dict(record)
        duplicate.pop("_source_path", None)
        duplicate["status"] = "obsolete"
        duplicate["obsolete_at"] = "2025-01-01T00:00:00Z"
        duplicate["obsolete_reason"] = "synthetic"
        path = self.backend._path("obsolete", record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(duplicate), encoding="utf-8")
        self.commit()
        with self.assertRaises(opc_hierarchical.HierarchicalError):
            opc_hierarchical.HierarchicalIndex(self.backend, self.data).preview()

    def test_parent_identity_change_aborts_publish_and_cleans_temporary(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        index = opc_hierarchical.HierarchicalIndex(self.backend, self.data)
        plan = index.preview()
        with patch(
            "opc_hierarchical._directory_object_token",
            side_effect=["parent-before", "parent-after"],
        ):
            with self.assertRaises(opc_hierarchical.HierarchicalError):
                index.build(approval_token=plan["approval_token"])
        self.assertFalse(index.path.exists())
        self.assertEqual(list(index.directory.glob(".index.json.tmp-*")), [])

    def test_data_root_inside_project_git_is_rejected_without_writes(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        project = Path(self.temporary.name) / "project-source"
        project.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
        data = project / ".opc" / "private-data"
        index = opc_hierarchical.HierarchicalIndex(self.backend, data)
        with self.assertRaises(opc_hierarchical.HierarchicalError):
            index.preview()
        self.assertFalse(data.exists())

    def test_oversized_canonical_record_fails_closed(self) -> None:
        record = self.add_approved(summary="alpha", content="body")
        path = self.knowledge / record["_source_path"]
        path.write_bytes(b"{" + b"x" * (opc_memory.MAX_RECORD_BYTES + 1))
        self.commit()
        with self.assertRaises((opc_hierarchical.HierarchicalError, opc_memory.OpcMemoryError)):
            opc_hierarchical.HierarchicalIndex(self.backend, self.data).preview()

    def test_runtime_validators_reject_trace_body_and_impossible_budget(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        result = self.build().query("alpha", project_id="project-alpha", role="developer")
        trace = dict(result["recall_trace"])
        trace["discards"] = [{"content": "forbidden"}]
        with self.assertRaises(opc_hierarchical.HierarchicalError):
            opc_hierarchical.validate_recall_trace(trace)
        packet = dict(result["context_packet"])
        packet["budget"] = {"limit_tokens": 10, "used_tokens": 9, "remaining_tokens": 9}
        with self.assertRaises(opc_hierarchical.HierarchicalError):
            opc_hierarchical.validate_context_packet(packet)

    def test_runtime_products_match_published_top_level_schemas(self) -> None:
        self.add_approved(summary="alpha", content="body")
        self.commit()
        result = self.build().query("alpha", project_id="project-alpha", role="developer")
        assets = Path(__file__).resolve().parents[2] / "plugins" / "codex-opc-team" / "assets" / "context"
        for product, schema_name in (
            (result["context_packet"], "context-packet.v1.schema.json"),
            (result["recall_trace"], "recall-trace.v1.schema.json"),
        ):
            schema = json.loads((assets / schema_name).read_text(encoding="utf-8"))
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(product))


if __name__ == "__main__":
    unittest.main()
