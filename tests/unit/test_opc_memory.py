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
from uuid import UUID


SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "codex-opc-team"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import opc_memory  # noqa: E402


class BombProvider:
    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        raise AssertionError("disabled provider must not be called")

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise AssertionError("disabled provider must not be called")


class SearchFailureProvider:
    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        return {"id": "ok"}

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("semantic provider unavailable")


class WriteFailureProvider:
    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        raise RuntimeError("index write unavailable")

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        return []


class StaticRecallProvider:
    def __init__(self, hits: list[dict[str, Any]]):
        self.hits = hits

    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        return {"id": "ok"}

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        return self.hits[:limit]


class CountingProvider:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []

    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        self.add_calls.append({"text": text, "metadata": dict(metadata)})
        return {"id": f"memory-{len(self.add_calls)}"}

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        return []


class SlowProvider:
    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        time.sleep(0.5)
        return {"id": "late"}

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        time.sleep(0.5)
        return []


class MemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.knowledge = base / "knowledge"
        self.data = base / "plugin-data"
        self.backend = opc_memory.FileGitBackend(self.knowledge)
        self.backend.ensure_layout()

    def candidate(self, memory_type: str = "decision") -> dict[str, Any]:
        return self.backend.add_candidate(
            memory_type=memory_type,
            summary="Use independent QA",
            content="Require independent QA evidence before manager handoff.",
            keywords=["qa", "handoff"],
            metadata={"project_kind": "web"},
            project_id="demo-project",
            evidence={"qa_report": "reports/qa.md"},
            confidence=0.9,
        )

    def approved(self, memory_type: str = "decision") -> dict[str, Any]:
        candidate = self.candidate(memory_type)
        return self.backend.approve(
            candidate["id"],
            approved_by="manager",
            validation="Replayed successfully twice",
        )

    def approved_for_scope(
        self,
        *,
        summary: str,
        scope: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        candidate = self.backend.add_candidate(
            memory_type="decision",
            summary=summary,
            content="shared-recall-marker",
            scope=scope,
            project_id=project_id,
        )
        return self.backend.approve(
            candidate["id"], approved_by="manager", validation="scope verified"
        )

    def commit_knowledge(self, message: str = "commit approved knowledge") -> str:
        if not shutil.which("git"):
            self.skipTest("Git is required for provenance tests")
        if not (self.knowledge / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main", str(self.knowledge)],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["git", "-C", str(self.knowledge), "add", "--", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.knowledge),
                "-c",
                "user.name=OPC Test",
                "-c",
                "user.email=opc-test@example.invalid",
                "commit",
                "-m",
                message,
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

    def test_no_mem0_default_mode_is_complete(self) -> None:
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=False,
            provider=BombProvider(),
        )
        candidate = service.add_candidate(
            memory_type="decision",
            summary="File is authority",
            content="Keep canonical memory in Git-tracked files.",
            keywords=["git"],
            project_id="demo-project",
        )
        approved = service.approve(
            candidate["id"], approved_by="manager", validation="reviewed"
        )
        self.assertEqual(approved["_recall_sync"], "disabled")
        self.assertEqual(
            service.query("canonical memory", project_id="demo-project"), []
        )
        self.commit_knowledge()
        hits = service.query("canonical memory", project_id="demo-project")
        self.assertEqual([hit["id"] for hit in hits], [candidate["id"]])
        self.assertEqual(hits[0]["_recall_source"], "file")

    def test_project_scope_requires_project_id_and_global_rejects_it(self) -> None:
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "requires a project_id"):
            self.backend.add_candidate(
                memory_type="decision",
                summary="Unreachable project memory",
                content="This must be rejected before persistence.",
            )
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "must not include"):
            self.backend.add_candidate(
                memory_type="decision",
                summary="Global memory with project identity",
                content="Global knowledge must not be coupled to a project.",
                scope="global",
                project_id="demo-project",
            )
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "Unsupported scope"):
            self.backend.add_candidate(
                memory_type="decision",
                summary="Organization memory without identity contract",
                content="This scope is not available in v0.1.",
                scope="organization",
            )

    def test_hand_edited_global_record_with_project_id_fails_closed(self) -> None:
        malformed = self.approved_for_scope(
            summary="Malformed global rule", scope="global"
        )
        path = self.knowledge / malformed["_source_path"]
        payload = opc_memory.load_json(path)
        payload["project_id"] = "project-a"
        opc_memory.atomic_write_json(path, payload)
        self.commit_knowledge()

        self.assertEqual(self.backend.query("shared-recall-marker"), [])
        self.assertEqual(
            self.backend.query("shared-recall-marker", project_id="project-a"), []
        )
        report = self.backend.doctor()
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                "Global record must not include project_id" in error
                for error in report["invalid"]
            )
        )

    def test_file_recall_isolates_projects_and_denies_unruled_scopes(self) -> None:
        global_record = self.approved_for_scope(
            summary="Global rule", scope="global"
        )
        project_a = self.approved_for_scope(
            summary="Project A rule", scope="project", project_id="project-a"
        )
        project_b = self.approved_for_scope(
            summary="Project B rule", scope="project", project_id="project-b"
        )
        self.commit_knowledge()

        without_context = self.backend.query("shared-recall-marker")
        for_a = self.backend.query(
            "shared-recall-marker", project_id="project-a"
        )
        for_b = self.backend.query(
            "shared-recall-marker", project_id="project-b"
        )
        self.assertEqual({item["id"] for item in without_context}, {global_record["id"]})
        self.assertEqual(
            {item["id"] for item in for_a}, {global_record["id"], project_a["id"]}
        )
        self.assertEqual(
            {item["id"] for item in for_b}, {global_record["id"], project_b["id"]}
        )
        context = self.backend.export_decision_context(
            "shared-recall-marker", project_id="project-a"
        )
        self.assertIn(project_a["id"], context)
        self.assertNotIn(project_b["id"], context)

    def test_fake_mem0_recall_rechecks_project_scope_from_canonical_file(self) -> None:
        project_a = self.approved_for_scope(
            summary="Semantic A", scope="project", project_id="project-a"
        )
        project_b = self.approved_for_scope(
            summary="Semantic B", scope="project", project_id="project-b"
        )
        self.commit_knowledge()
        hits = []
        for score, record in ((0.99, project_b), (0.90, project_a)):
            hits.append(
                {
                    "score": score,
                    "metadata": {
                        "record_id": record["id"],
                        **self.backend.source_metadata(record["_source_path"]),
                    },
                }
            )
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=StaticRecallProvider(hits),
        )
        for_a = service.query("semantic-only-query", project_id="project-a")
        without_context = service.query("semantic-only-query")
        self.assertEqual([item["id"] for item in for_a], [project_a["id"]])
        self.assertEqual(without_context, [])

    def test_disabled_provider_is_never_imported_or_called(self) -> None:
        self.approved()
        self.commit_knowledge()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=False,
            provider=BombProvider(),
        )
        self.assertEqual(
            len(service.query("independent QA", project_id="demo-project")), 1
        )
        self.assertEqual(service.status()["authority"], "file-git")

    def test_mem0_import_failure_falls_back_to_files(self) -> None:
        self.approved()
        self.commit_knowledge()

        def missing(_name: str) -> Any:
            raise ModuleNotFoundError("mem0")

        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=opc_memory.Mem0Provider(importer=missing),
        )
        hits = service.query("independent QA", project_id="demo-project")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["_recall_source"], "file")

    def test_mem0_search_exception_falls_back_to_files(self) -> None:
        self.approved()
        self.commit_knowledge()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=SearchFailureProvider(),
        )
        hits = service.query("manager handoff", project_id="demo-project")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["_recall_source"], "file")

    def test_mem0_timeout_falls_back_to_files(self) -> None:
        self.approved()
        self.commit_knowledge()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=SlowProvider(),
            timeout_seconds=0.02,
        )
        started = time.monotonic()
        hits = service.query("manager handoff", project_id="demo-project")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["_recall_source"], "file")

    def test_valid_mem0_hit_is_resolved_to_authoritative_file(self) -> None:
        approved = self.approved()
        self.commit_knowledge()
        provenance = self.backend.source_metadata(approved["_source_path"])
        provider = StaticRecallProvider(
            [{"score": 0.9, "metadata": {"record_id": approved["id"], **provenance}}]
        )
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        hits = service.query("semantic-only phrase", project_id="demo-project")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], approved["id"])
        self.assertEqual(hits[0]["_recall_source"], "mem0")

    def test_stale_hash_hit_is_discarded(self) -> None:
        approved = self.approved()
        self.commit_knowledge()
        provenance = self.backend.source_metadata(approved["_source_path"])
        provider = StaticRecallProvider(
            [
                {
                    "score": 0.99,
                    "metadata": {
                        "record_id": approved["id"],
                        **provenance,
                        "content_hash": "0" * 64,
                    },
                }
            ]
        )
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        self.assertEqual(
            service.query("semantic-only phrase", project_id="demo-project"), []
        )

    def test_mem0_hit_without_source_commit_is_rejected(self) -> None:
        approved = self.approved()
        self.commit_knowledge()
        provenance = self.backend.source_metadata(approved["_source_path"])
        provenance.pop("source_commit", None)
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=StaticRecallProvider(
                [
                    {
                        "score": 0.99,
                        "metadata": {"record_id": approved["id"], **provenance},
                    }
                ]
            ),
        )
        self.assertEqual(
            service.query("semantic-only phrase", project_id="demo-project"), []
        )

    def test_candidate_promotion_and_obsolete_transition(self) -> None:
        service = opc_memory.MemoryService(
            self.backend, data_root=self.data, mem0_enabled=False
        )
        candidate = self.candidate()
        approved = service.approve(
            candidate["id"], approved_by="manager", validation="verified"
        )
        self.assertEqual(approved["status"], "approved")
        self.assertFalse(
            (self.knowledge / "experiences/candidates" / f"{candidate['id']}.json").exists()
        )
        obsolete = service.mark_obsolete(
            candidate["id"], reason="Replaced by a stricter gate"
        )
        self.assertEqual(obsolete["status"], "obsolete")
        self.assertEqual(
            service.query("independent QA", project_id="demo-project"), []
        )

    def test_keyword_metadata_and_type_filters(self) -> None:
        self.approved("qa_rule")
        self.commit_knowledge()
        hits = self.backend.query(
            "handoff",
            memory_type="qa_rule",
            keywords=["qa"],
            metadata={"project_kind": "web"},
            project_id="demo-project",
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(self.backend.list_by_type("qa_rule")), 1)

    def test_list_status_supports_candidate_without_type(self) -> None:
        candidate = self.candidate("qa_rule")
        args = opc_memory.build_parser().parse_args(["list", "--status", "candidate"])
        self.assertEqual(args.status, "candidate")
        self.assertIsNone(args.memory_type)
        records = opc_memory.MemoryService(
            self.backend, data_root=self.data
        ).list_by_status(args.status, memory_type=args.memory_type)
        self.assertEqual([record["id"] for record in records], [candidate["id"]])

    def test_enabled_approve_never_writes_provider_before_git_commit(self) -> None:
        provider = CountingProvider()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        candidate = self.candidate()
        approved = service.approve(
            candidate["id"], approved_by="manager", validation="verified"
        )
        self.assertEqual(approved["_recall_sync"], "pending_commit")
        self.assertEqual(provider.add_calls, [])
        self.assertFalse(service.index_state_path.exists())
        self.assertFalse(service.outbox_path.exists())

    def test_uncommitted_approved_source_cannot_be_reindexed(self) -> None:
        approved = self.approved()
        provider = CountingProvider()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        plan = service.reindex_plan()
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["items"][0]["record_id"], approved["id"])
        self.assertEqual(plan["items"][0]["action"], "conflict_uncommitted")
        self.assertIn("UNCOMMITTED_APPROVED_SOURCE", plan["conflicts"][0])
        applied = service.reindex_apply()
        self.assertFalse(applied["ok"])
        self.assertEqual(applied["reason"], "REINDEX_PLAN_INVALID")
        self.assertEqual(provider.add_calls, [])
        self.assertFalse(service.index_state_path.exists())
        self.assertFalse(service.outbox_path.exists())

    def test_reindex_preview_is_read_only(self) -> None:
        self.approved()
        self.commit_knowledge()
        self.candidate("qa_rule")
        provider = CountingProvider()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        plan = service.reindex_plan()
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["pending_count"], 1)
        self.assertEqual(plan["items"][0]["action"], "index")
        self.assertEqual(provider.add_calls, [])
        self.assertFalse(service.index_state_path.exists())

    def test_reindex_apply_indexes_approved_with_provenance(self) -> None:
        approved = self.approved()
        commit = self.commit_knowledge()
        provider = CountingProvider()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        result = service.reindex_apply()
        self.assertTrue(result["ok"])
        self.assertEqual(result["indexed_count"], 1)
        self.assertEqual(result["indexed"], [approved["id"]])
        metadata = provider.add_calls[0]["metadata"]
        for key in ("source_path", "content_hash", "source_commit"):
            self.assertIn(key, metadata)
        self.assertEqual(metadata["source_commit"], commit)
        self.assertTrue(metadata["source_path"].startswith("experiences/approved/"))
        self.assertTrue(service.index_state_path.is_file())

    def test_reindex_apply_is_idempotent_from_derived_state(self) -> None:
        self.approved()
        self.commit_knowledge()
        provider = CountingProvider()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        self.assertTrue(service.reindex_apply()["ok"])
        second = service.reindex_apply()
        self.assertTrue(second["ok"])
        self.assertEqual(second["indexed_count"], 0)
        self.assertEqual(second["skipped_count"], 1)
        self.assertEqual(len(provider.add_calls), 1)

    def test_reindex_force_rebuilds_after_verified_index_loss(self) -> None:
        self.approved()
        self.commit_knowledge()
        provider = CountingProvider()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        self.assertTrue(service.reindex_apply()["ok"])
        forced = service.reindex_apply(force=True)
        self.assertTrue(forced["ok"])
        self.assertTrue(forced["force"])
        self.assertEqual(forced["indexed_count"], 1)
        self.assertEqual(len(provider.add_calls), 2)

    def test_reindex_provider_failure_is_outboxed_and_not_successful(self) -> None:
        self.approved()
        self.commit_knowledge()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=WriteFailureProvider(),
        )
        result = service.reindex_apply()
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["indexed_count"], 0)
        event = json.loads(service.outbox_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["operation"], "upsert")
        self.assertFalse(service.index_state_path.exists())

    def test_reindex_provider_timeout_is_outboxed_and_not_successful(self) -> None:
        self.approved()
        self.commit_knowledge()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=SlowProvider(),
            timeout_seconds=0.02,
        )
        started = time.monotonic()
        result = service.reindex_apply()
        # Includes Git HEAD/blob provenance verification on Windows in addition
        # to the 20 ms provider timeout.
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_count"], 1)
        event = json.loads(service.outbox_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["error_type"], "ProviderTimeout")
        self.assertFalse(service.index_state_path.exists())

    def test_reindex_apply_refuses_when_mem0_is_disabled(self) -> None:
        self.approved()
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=False,
            provider=BombProvider(),
        )
        result = service.reindex_apply()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "MEM0_DISABLED")
        self.assertFalse(service.index_state_path.exists())
        self.assertFalse(service.outbox_path.exists())

    def test_reindex_cli_defaults_to_preview_and_supports_apply_force(self) -> None:
        parser = opc_memory.build_parser()
        preview = parser.parse_args(["reindex"])
        self.assertFalse(preview.apply)
        applied = parser.parse_args(["reindex", "--apply", "--force"])
        self.assertTrue(applied.apply)
        self.assertTrue(applied.force)

    def test_query_and_export_cli_accept_explicit_project_context(self) -> None:
        parser = opc_memory.build_parser()
        query = parser.parse_args(
            ["query", "qa", "--project-id", "demo-project"]
        )
        export = parser.parse_args(
            ["export-context", "--query", "qa", "--project-id", "demo-project"]
        )
        self.assertEqual(query.project_id, "demo-project")
        self.assertEqual(export.project_id, "demo-project")

    def test_mem0_2_contract_uses_private_storage_and_search_signature(self) -> None:
        captured: dict[str, Any] = {}

        class ContractClient:
            def add(
                self,
                text: str,
                *,
                user_id: str,
                metadata: Mapping[str, Any],
                infer: bool,
            ) -> dict[str, Any]:
                captured["add"] = {
                    "text": text,
                    "user_id": user_id,
                    "metadata": dict(metadata),
                    "infer": infer,
                }
                return {"results": []}

            def search(
                self,
                *,
                query: str,
                top_k: int,
                filters: Mapping[str, Any],
            ) -> dict[str, Any]:
                captured["search"] = {
                    "query": query,
                    "top_k": top_k,
                    "filters": dict(filters),
                }
                return {"results": []}

        client = ContractClient()

        def factory(module: Any, config: Mapping[str, Any]) -> ContractClient:
            captured["module"] = module
            captured["config"] = dict(config)
            captured["factory_env"] = {
                "MEM0_DIR": os.environ.get("MEM0_DIR"),
                "MEM0_TELEMETRY": os.environ.get("MEM0_TELEMETRY"),
            }
            return client

        before_dir = os.environ.get("MEM0_DIR")
        before_telemetry = os.environ.get("MEM0_TELEMETRY")
        provider = opc_memory.Mem0Provider(
            user_id="opc-contract-user",
            data_root=self.data,
            importer=lambda _name: object(),
            client_factory=factory,
        )
        provider.add("approved text", {"source_path": "experiences/approved/x.json"})
        provider.search("approved", 7)
        config = captured["config"]
        self.assertEqual(
            Path(config["history_db_path"]).resolve(),
            (self.data / "mem0" / "history.db").resolve(),
        )
        vector = config["vector_store"]["config"]
        self.assertEqual(
            Path(vector["path"]).resolve(),
            (self.data / "mem0" / "qdrant").resolve(),
        )
        self.assertTrue(vector["on_disk"])
        self.assertTrue(vector["collection_name"].startswith("opc_"))
        factory_env = captured["factory_env"]
        self.assertEqual(
            Path(factory_env["MEM0_DIR"]).resolve(),
            (self.data / "mem0").resolve(),
        )
        self.assertEqual(factory_env["MEM0_TELEMETRY"], "False")
        self.assertEqual(
            captured["search"],
            {
                "query": "approved",
                "top_k": 7,
                "filters": {"user_id": "opc-contract-user"},
            },
        )
        self.assertFalse(captured["add"]["infer"])
        self.assertEqual(os.environ.get("MEM0_DIR"), before_dir)
        self.assertEqual(os.environ.get("MEM0_TELEMETRY"), before_telemetry)

    def test_mem0_default_factory_uses_memory_from_config(self) -> None:
        captured: dict[str, Any] = {}

        class Client:
            def search(self, **kwargs: Any) -> dict[str, Any]:
                return {"results": []}

        class Memory:
            @classmethod
            def from_config(cls, config: Mapping[str, Any]) -> Client:
                captured["config"] = dict(config)
                return Client()

        module = type("FakeMem0Module", (), {"Memory": Memory})()
        provider = opc_memory.Mem0Provider(
            user_id="opc-from-config",
            data_root=self.data,
            importer=lambda _name: module,
        )
        provider.search("query", 1)
        self.assertEqual(
            Path(captured["config"]["history_db_path"]).resolve(),
            (self.data / "mem0" / "history.db").resolve(),
        )

    def test_mem0_support_is_exactly_the_tested_release(self) -> None:
        with patch.object(opc_memory.Mem0Provider, "package_version", return_value="2.0.11"):
            self.assertTrue(opc_memory.Mem0Provider.supported_version())
        for version in ("2.0.10", "2.0.12", "2.1.0", "3.0.0"):
            with self.subTest(version=version), patch.object(
                opc_memory.Mem0Provider, "package_version", return_value=version
            ):
                self.assertFalse(opc_memory.Mem0Provider.supported_version())

    def test_unsupported_mem0_release_falls_back_without_provider_call(self) -> None:
        self.approved()
        self.commit_knowledge()

        def forbidden_import(_name: str) -> Any:
            raise AssertionError("unsupported provider must not be imported")

        provider = opc_memory.Mem0Provider(
            data_root=self.data,
            importer=forbidden_import,
        )
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=provider,
        )
        with patch.object(opc_memory.Mem0Provider, "supported_version", return_value=False):
            hits = service.query("independent QA", project_id="demo-project")
            rebuild = service.reindex_apply()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["_recall_source"], "file")
        self.assertFalse(rebuild["ok"])
        self.assertEqual(rebuild["reason"], "MEM0_UNSUPPORTED_VERSION")

    def test_approve_succeeds_in_file_git_when_mem0_version_is_unsupported(self) -> None:
        candidate = self.candidate()

        def forbidden_import(_name: str) -> Any:
            raise AssertionError("unsupported provider must not be imported")

        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=opc_memory.Mem0Provider(
                data_root=self.data,
                importer=forbidden_import,
            ),
        )
        with patch.object(opc_memory.Mem0Provider, "supported_version", return_value=False):
            approved = service.approve(
                candidate["id"], approved_by="manager", validation="verified"
            )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["_recall_sync"], "pending_commit")
        self.assertFalse(service.outbox_path.exists())
        self.assertEqual(
            self.backend.query("independent QA", project_id="demo-project"), []
        )
        self.commit_knowledge()
        self.assertEqual(
            len(self.backend.query("independent QA", project_id="demo-project")), 1
        )

    def test_export_decision_context_uses_only_approved_records(self) -> None:
        approved = self.approved()
        self.commit_knowledge()
        self.candidate()
        context = opc_memory.MemoryService(
            self.backend, data_root=self.data
        ).export_decision_context("independent QA", project_id="demo-project")
        self.assertIn(approved["id"], context)
        self.assertEqual(context.count("## Use independent QA"), 1)

    def test_setup_dry_run_does_not_create_state(self) -> None:
        parser = opc_memory.build_parser()
        args = parser.parse_args(
            [
                "--knowledge-root",
                str(self.knowledge / "planned"),
                "--data-root",
                str(self.data / "planned"),
                "setup",
                "--enable-mem0",
                "--dry-run",
            ]
        )
        result = opc_memory._setup_result(args)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.knowledge / "planned").exists())
        self.assertFalse((self.data / "planned").exists())
        commands = "\n".join(
            result["isolated_venv"]["windows"]
            + result["isolated_venv"]["unix"]
        )
        self.assertIn(" -m venv ", commands)
        self.assertIn("requirements-mem0.txt", commands)
        self.assertNotIn("pip install mem0ai", commands)

    def test_setup_dry_run_rejects_overlapping_roots_without_writes(self) -> None:
        parser = opc_memory.build_parser()
        base = Path(self.tempdir.name) / "overlap"
        cases = (
            (base, base),
            (base, base / "data"),
            (base / "knowledge", base),
        )
        for knowledge, data in cases:
            with self.subTest(knowledge=knowledge, data=data):
                args = parser.parse_args(
                    [
                        "--knowledge-root",
                        str(knowledge),
                        "--data-root",
                        str(data),
                        "setup",
                        "--dry-run",
                    ]
                )
                with self.assertRaisesRegex(
                    opc_memory.OpcMemoryError, "ROOT_ISOLATION_ERROR"
                ):
                    opc_memory._setup_result(args)
        self.assertFalse(base.exists())

    def test_service_rejects_overlap_before_default_provider_construction(self) -> None:
        root = Path(self.tempdir.name) / "service-overlap"
        backend = opc_memory.FileGitBackend(root)
        with patch.object(
            opc_memory.Mem0Provider,
            "__init__",
            side_effect=AssertionError("provider must not be constructed"),
        ) as constructor:
            with self.assertRaisesRegex(
                opc_memory.OpcMemoryError, "ROOT_ISOLATION_ERROR"
            ):
                opc_memory.MemoryService(backend, data_root=root / "derived")
        constructor.assert_not_called()

    def test_private_roots_cannot_overlap_installed_plugin_tree(self) -> None:
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "ROOT_ISOLATION_ERROR"):
            opc_memory.FileGitBackend(opc_memory.PLUGIN_ROOT / "private-knowledge")
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "ROOT_ISOLATION_ERROR"):
            opc_memory.Mem0Provider(data_root=opc_memory.PLUGIN_ROOT / "private-data")

    def test_setup_apply_persists_stable_anonymous_identity(self) -> None:
        parser = opc_memory.build_parser()
        arguments = [
            "--knowledge-root",
            str(self.knowledge),
            "--data-root",
            str(self.data),
            "setup",
            "--enable-mem0",
            "--apply",
        ]
        first = opc_memory._setup_result(parser.parse_args(arguments))
        first_config = opc_memory.load_config(self.data)
        UUID(first_config["installation_id"])
        self.assertEqual(
            first_config["mem0"]["user_id"],
            f"opc-{first_config['installation_id']}",
        )
        second = opc_memory._setup_result(parser.parse_args(arguments))
        second_config = opc_memory.load_config(self.data)
        self.assertEqual(first_config, second_config)
        self.assertTrue(first["anonymous_identity_created"])
        self.assertFalse(second["anonymous_identity_created"])
        status = opc_memory.MemoryService.from_paths(
            self.knowledge, self.data
        ).status()
        self.assertTrue(status["mem0"]["anonymous_identity_configured"])

    def test_doctor_reports_missing_root_as_not_initialized(self) -> None:
        report = opc_memory.FileGitBackend(self.knowledge / "missing").doctor()
        self.assertFalse(report["ok"])
        self.assertEqual(report["state"], "NOT_INITIALIZED")
        self.assertFalse(report["provenance_ready"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for legacy audit test")
    def test_legacy_runtime_artifact_is_redacted_and_not_uncommitted_knowledge(self) -> None:
        (self.knowledge / "README.md").write_text(
            "# Synthetic knowledge\n", encoding="utf-8"
        )
        self.commit_knowledge("baseline knowledge")
        legacy = self.knowledge / "evaluations" / "events" / "hook-events.jsonl"
        legacy.parent.mkdir(parents=True)
        synthetic_private_marker = "synthetic-private-hook-payload"
        legacy.write_text(synthetic_private_marker + "\n", encoding="utf-8")

        audit = self.backend.git_audit()
        self.assertIn("LEGACY_RUNTIME_ARTIFACTS", audit["warning_codes"])
        self.assertNotIn("UNCOMMITTED_KNOWLEDGE", audit["warning_codes"])
        self.assertEqual(
            ["evaluations/events/hook-events.jsonl"],
            audit["legacy_runtime_artifacts"],
        )
        self.assertEqual([], audit["authoritative_uncommitted"])

        report = self.backend.doctor()
        self.assertTrue(report["legacy_runtime"]["detected"])
        self.assertFalse(report["legacy_runtime"]["contents_inspected"])
        self.assertNotIn(synthetic_private_marker, json.dumps(report))
        self.assertIn("legacy-events --dry-run", report["legacy_runtime"]["action"])
        status = opc_memory.MemoryService(
            self.backend, data_root=self.data
        ).status()
        self.assertTrue(status["legacy_runtime"]["detected"])
        self.assertIn("legacy-events --dry-run", status["legacy_runtime"]["action"])
        self.assertNotIn(synthetic_private_marker, json.dumps(status))

    @unittest.skipUnless(shutil.which("git"), "Git is required for legacy archive test")
    def test_legacy_runtime_archive_requires_unchanged_preview_token(self) -> None:
        (self.knowledge / "README.md").write_text(
            "# Synthetic knowledge\n", encoding="utf-8"
        )
        self.commit_knowledge("baseline knowledge")
        legacy = self.knowledge / "evaluations" / "events" / "hook-events.jsonl"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("synthetic-event\n", encoding="utf-8")

        preview = self.backend.legacy_runtime_plan(self.data)
        self.assertTrue(preview["dry_run"])
        self.assertTrue(preview["detected"])
        self.assertFalse(preview["contents_inspected"])
        self.assertTrue(legacy.is_file())
        self.assertFalse(self.data.exists())
        self.assertEqual(
            ["delete", "commit", "upload"], preview["automatic_actions_excluded"]
        )

        with self.assertRaisesRegex(
            opc_memory.OpcMemoryError, "LEGACY_EVENT_PLAN_CHANGED"
        ):
            self.backend.apply_legacy_runtime_plan(self.data, plan_token=None)
        self.assertTrue(legacy.is_file())

        applied = self.backend.apply_legacy_runtime_plan(
            self.data, plan_token=preview["approval_token"]
        )
        archived = (
            self.data
            / "legacy-event-archive"
            / "evaluations"
            / "events"
            / "hook-events.jsonl"
        )
        self.assertTrue(applied["changed"])
        self.assertFalse(legacy.exists())
        self.assertEqual("synthetic-event\n", archived.read_text(encoding="utf-8"))
        self.assertEqual([], self.backend.legacy_runtime_artifacts())

    @unittest.skipUnless(shutil.which("git"), "Git is required for tracked legacy test")
    def test_tracked_legacy_runtime_artifact_is_never_moved_automatically(self) -> None:
        legacy = self.knowledge / "evaluations" / "events" / "historic.jsonl"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("synthetic-event\n", encoding="utf-8")
        self.commit_knowledge("synthetic tracked legacy layout")

        preview = self.backend.legacy_runtime_plan(self.data)
        self.assertFalse(preview["entries"][0]["eligible"])
        self.assertTrue(preview["entries"][0]["tracked"])
        with self.assertRaisesRegex(
            opc_memory.OpcMemoryError, "LEGACY_EVENT_MOVE_BLOCKED"
        ):
            self.backend.apply_legacy_runtime_plan(
                self.data, plan_token=preview["approval_token"]
            )
        self.assertTrue(legacy.is_file())

    @unittest.skipUnless(shutil.which("git"), "Git is required for legacy symlink test")
    def test_legacy_runtime_symlink_is_reported_but_never_followed_or_moved(self) -> None:
        (self.knowledge / "README.md").write_text(
            "# Synthetic knowledge\n", encoding="utf-8"
        )
        self.commit_knowledge("baseline knowledge")
        outside = self.knowledge.parent / "outside-private.jsonl"
        outside.write_text("synthetic-outside-event\n", encoding="utf-8")
        linked = self.knowledge / "evaluations" / "events" / "linked.jsonl"
        linked.parent.mkdir(parents=True)
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"File symlinks unavailable: {exc}")

        preview = self.backend.legacy_runtime_plan(self.data)
        self.assertEqual("evaluations/events/linked.jsonl", preview["entries"][0]["source"])
        self.assertFalse(preview["entries"][0]["eligible"])
        self.assertIn("symbolic link", preview["entries"][0]["blocked_reason"])
        with self.assertRaisesRegex(
            opc_memory.OpcMemoryError, "LEGACY_EVENT_MOVE_BLOCKED"
        ):
            self.backend.apply_legacy_runtime_plan(
                self.data, plan_token=preview["approval_token"]
            )
        self.assertTrue(linked.is_symlink())
        self.assertEqual(
            "synthetic-outside-event\n", outside.read_text(encoding="utf-8")
        )

    def test_approved_knowledge_named_like_event_is_not_misclassified(self) -> None:
        approved = self.knowledge / "experiences" / "approved" / "hook-events.jsonl"
        approved.write_text("synthetic-approved-record\n", encoding="utf-8")
        self.assertEqual([], self.backend.legacy_runtime_artifacts())

    def test_legacy_events_cli_defaults_to_preview(self) -> None:
        args = opc_memory.build_parser().parse_args(["legacy-events"])
        self.assertFalse(args.apply)
        self.assertIsNone(args.plan_token)

    @unittest.skipUnless(shutil.which("git"), "Git is required for readiness test")
    def test_git_repository_without_head_is_not_provenance_ready(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.knowledge)],
            check=True,
            capture_output=True,
        )
        audit = self.backend.git_audit()
        self.assertTrue(audit["is_repo"])
        self.assertIsNone(audit["head"])
        self.assertFalse(audit["provenance_ready"])
        self.assertIn("GIT_HEAD_MISSING", audit["warning_codes"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for audit test")
    def test_git_audit_warns_for_uncommitted_authoritative_knowledge(self) -> None:
        subprocess.run(
            ["git", "init", "-b", "main", str(self.knowledge)],
            check=True,
            capture_output=True,
        )
        marker = self.knowledge / "README.md"
        marker.write_text("# Knowledge\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.knowledge), "add", "--", "README.md"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.knowledge),
                "-c",
                "user.name=OPC Test",
                "-c",
                "user.email=opc-test@example.invalid",
                "commit",
                "-m",
                "baseline",
            ],
            check=True,
            capture_output=True,
        )

        approved = self.approved()
        audit = self.backend.git_audit()
        self.assertTrue(audit["is_repo"])
        self.assertTrue(audit["head"])
        self.assertTrue(audit["provenance_ready"])
        self.assertTrue(audit["dirty"])
        self.assertEqual(audit["staged"], [])
        self.assertIn(approved["_source_path"], audit["untracked"])
        self.assertIn(approved["_source_path"], audit["authoritative_uncommitted"])
        self.assertIn("UNCOMMITTED_KNOWLEDGE", audit["warning_codes"])

        status = opc_memory.MemoryService(
            self.backend, data_root=self.data
        ).status()
        self.assertIn("UNCOMMITTED_KNOWLEDGE", status["warnings"])
        subprocess.run(
            [
                "git",
                "-C",
                str(self.knowledge),
                "add",
                "--",
                approved["_source_path"],
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.knowledge),
                "-c",
                "user.name=OPC Test",
                "-c",
                "user.email=opc-test@example.invalid",
                "commit",
                "-m",
                "approve scoped knowledge",
            ],
            check=True,
            capture_output=True,
        )
        clean = self.backend.git_audit()
        self.assertFalse(clean["dirty"])
        self.assertNotIn("UNCOMMITTED_KNOWLEDGE", clean["warning_codes"])

    def test_status_reports_isolated_venv_interpreter_rerun_hint(self) -> None:
        venv_python = self.data / "venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("test", encoding="utf-8")
        service = opc_memory.MemoryService(
            self.backend,
            data_root=self.data,
            mem0_enabled=True,
            provider=opc_memory.Mem0Provider(data_root=self.data),
        )
        with patch.object(opc_memory.Mem0Provider, "installed", return_value=False):
            status = service.status()
        self.assertTrue(status["isolated_venv"]["python_exists"])
        self.assertIn(
            str(venv_python.resolve()), status["isolated_venv"]["rerun_hint"]
        )

    def test_structured_metadata_rejects_machine_absolute_paths(self) -> None:
        with self.assertRaisesRegex(opc_memory.OpcMemoryError, "portable relative"):
            self.backend.add_candidate(
                memory_type="decision",
                summary="Bad provenance",
                content="Do not persist machine paths.",
                evidence={"report": "C:\\Users\\private\\qa.md"},
                project_id="demo-project",
            )

    def test_provider_error_is_redacted_before_outbox(self) -> None:
        fake_key = "sk" + "-example-secret-value"
        error = RuntimeError(f"api_key={fake_key} token=my-token-value")
        redacted = opc_memory.redact_error(error)
        self.assertNotIn(fake_key, redacted)
        self.assertNotIn("my-token-value", redacted)


if __name__ == "__main__":
    unittest.main()
