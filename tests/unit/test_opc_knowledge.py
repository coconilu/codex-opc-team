from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "codex-opc-team"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import opc_knowledge  # noqa: E402


class KnowledgeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.knowledge = base / "knowledge"
        self.project = base / "project"
        self.project.mkdir()
        opc_knowledge.init_knowledge(root=self.knowledge)
        opc_knowledge.init_project(
            project_root=self.project, project_id="portable-demo", name="Demo"
        )

    def test_start_run_contains_portable_project_id_not_absolute_path(self) -> None:
        run = opc_knowledge.start_run(
            root=self.knowledge,
            project_root=self.project,
            title="Build demo",
        )
        self.assertEqual(run["project_id"], "portable-demo")
        serialized = json.dumps(run)
        self.assertNotIn(str(self.project), serialized)
        self.assertEqual(
            opc_knowledge.load_json(self.project / ".opc" / "run.json")["run_id"],
            run["run_id"],
        )
        self.assertFalse(
            (self.knowledge / "evaluations" / "runs" / f"{run['run_id']}.json").exists()
        )

    @unittest.skipUnless(shutil.which("git"), "Git is required for cleanliness test")
    def test_run_lifecycle_does_not_dirty_knowledge_repository(self) -> None:
        knowledge = Path(self.tempdir.name) / "clean-knowledge"
        project = Path(self.tempdir.name) / "clean-project"
        project.mkdir()
        opc_knowledge.init_knowledge(root=knowledge, git_init=True)
        opc_knowledge.init_project(
            project_root=project, project_id="clean-project", name="Clean"
        )
        opc_knowledge.start_run(
            root=knowledge, project_root=project, title="Keep runtime local"
        )
        opc_knowledge.update_run(
            root=knowledge,
            project_root=project,
            status="planned",
            note="Runtime evidence remains project-local.",
        )
        status = subprocess.run(
            ["git", "-C", str(knowledge), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "")
        self.assertEqual(
            list((knowledge / "evaluations" / "runs").glob("*.json")), []
        )

    def test_ready_and_complete_require_independent_evidence(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Build demo"
        )
        with self.assertRaisesRegex(opc_knowledge.OpcError, "implementation"):
            opc_knowledge.update_run(
                root=self.knowledge,
                project_root=self.project,
                status="ready_for_manager",
            )
        ready = opc_knowledge.update_run(
            root=self.knowledge,
            project_root=self.project,
            evidence={
                "implementation": "commit",
                "verification": "tests",
                "qa": "independent-report",
            },
            status="ready_for_manager",
        )
        self.assertEqual(ready["status"], "ready_for_manager")
        with self.assertRaisesRegex(opc_knowledge.OpcError, "manager_handoff"):
            opc_knowledge.update_run(
                root=self.knowledge,
                project_root=self.project,
                status="completed",
            )

    def test_candidate_flow_uses_project_id(self) -> None:
        run = opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Build demo"
        )
        service = opc_knowledge.MemoryService.from_paths(
            self.knowledge, Path(self.tempdir.name) / "data"
        )
        args = type(
            "Args",
            (),
            {
                "project_root": str(self.project),
                "source": None,
                "experience_type": "decision",
                "summary": "Keep QA independent",
                "content": "Do not let implementers approve their own work.",
                "scope": "global",
                "owner": "qa",
                "confidence": 0.8,
                "evidence": ["report=qa.md"],
                "metadata": [],
                "keyword": ["qa"],
            },
        )()
        candidate = opc_knowledge._add_candidate(service, args)
        self.assertNotIn("project_id", candidate)
        self.assertEqual(candidate["source"], run["run_id"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for baseline test")
    def test_init_knowledge_git_init_creates_main_baseline_commit(self) -> None:
        root = Path(self.tempdir.name) / "git-knowledge"
        result = opc_knowledge.init_knowledge(root=root, git_init=True)
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertTrue(result["git_initialized"])
        self.assertEqual(result["git_baseline_commit"], head)
        self.assertEqual(branch, "main")
        self.assertEqual(status, "")

    @unittest.skipUnless(shutil.which("git"), "Git is required for preservation test")
    def test_init_knowledge_preserves_existing_repo_and_user_changes(self) -> None:
        root = Path(self.tempdir.name) / "existing-git-knowledge"
        first = opc_knowledge.init_knowledge(root=root, git_init=True)
        marker = root / "local-marker.txt"
        marker.write_text("keep", encoding="utf-8")
        charter = root / "company" / "charter.md"
        charter.write_text("user change\n", encoding="utf-8")
        second = opc_knowledge.init_knowledge(root=root, git_init=True)
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(head, first["git_baseline_commit"])
        self.assertTrue(second["git_preserved"])
        self.assertFalse(second["git_initialized"])
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(charter.read_text(encoding="utf-8"), "user change\n")
        self.assertIn("local-marker.txt", status)
        self.assertIn("company/charter.md", status.replace("\\", "/"))

    @unittest.skipUnless(shutil.which("git"), "Git is required for recovery test")
    def test_init_knowledge_recovers_after_git_bootstrap_failure(self) -> None:
        root = Path(self.tempdir.name) / "recoverable-git-knowledge"
        with patch.object(
            opc_knowledge.subprocess,
            "run",
            side_effect=FileNotFoundError("git unavailable"),
        ):
            with self.assertRaisesRegex(
                opc_knowledge.OpcError, "Could not initialize the Git baseline"
            ):
                opc_knowledge.init_knowledge(root=root, git_init=True)

        marker = root / ".opc-bootstrap-state.json"
        self.assertTrue(marker.is_file())
        self.assertTrue((root / "company" / "charter.md").is_file())

        recovered = opc_knowledge.init_knowledge(root=root, git_init=True)
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertTrue(recovered["git_initialized"])
        self.assertTrue(recovered["git_recovered"])
        self.assertEqual(recovered["git_baseline_commit"], head)
        self.assertFalse(marker.exists())
        self.assertEqual(status, "")


if __name__ == "__main__":
    unittest.main()
