from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_knowledge  # noqa: E402
import opc_hook  # noqa: E402


class HookPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.knowledge = base / "knowledge"
        self.project = base / "project"
        self.unrelated = base / "unrelated"
        self.plugin_data = base / "plugin-data"
        self.project.mkdir()
        self.unrelated.mkdir()
        opc_knowledge.init_knowledge(root=self.knowledge)
        opc_knowledge.init_project(project_root=self.project, project_id="hook-demo")

    def run_hook(self, cwd: Path, event: str = "Stop") -> dict:
        payload = {
            "hook_event_name": event,
            "session_id": "secret-session",
            "turn_id": "secret-turn",
            "cwd": str(cwd),
            "model": "secret-model",
        }
        environment = {
            **os.environ,
            "PLUGIN_DATA": str(self.plugin_data),
            "OPC_KNOWLEDGE_HOME": str(self.knowledge),
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "opc_hook.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=environment,
            check=True,
        )
        return json.loads(result.stdout)

    def test_unrelated_project_creates_no_runtime_or_knowledge_record(self) -> None:
        before = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        response = self.run_hook(self.unrelated)
        after = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        self.assertTrue(response["continue"])
        self.assertFalse(self.plugin_data.exists())
        self.assertEqual(before, after)

    def test_active_stop_blocks_and_logs_only_allowlisted_fields(self) -> None:
        run = opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook test"
        )
        before = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        response = self.run_hook(self.project)
        self.assertFalse(response["continue"])
        event_path = self.plugin_data / "run-events" / f"{run['run_id']}.jsonl"
        event = json.loads(event_path.read_text(encoding="utf-8").strip())
        self.assertEqual(
            set(event), {"event", "at", "run_id", "status", "continue"}
        )
        serialized = json.dumps(event)
        for secret in ("secret-session", "secret-turn", str(self.project), "secret-model"):
            self.assertNotIn(secret, serialized)
        after = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        self.assertEqual(before, after)

    def test_missing_plugin_data_uses_project_runtime_fallback(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook fallback"
        )
        before = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        payload = {"hook_event_name": "Stop", "cwd": str(self.project)}
        environment = {
            key: value for key, value in os.environ.items() if key != "PLUGIN_DATA"
        }
        environment["OPC_KNOWLEDGE_HOME"] = str(self.knowledge)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "opc_hook.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=environment,
            check=True,
        )
        self.assertFalse(json.loads(result.stdout)["continue"])
        self.assertTrue((self.project / ".opc" / "events.jsonl").is_file())
        after = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        self.assertEqual(before, after)

    def test_plugin_data_inside_knowledge_uses_project_runtime_fallback(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook isolation"
        )
        before = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        payload = {"hook_event_name": "Stop", "cwd": str(self.project)}
        environment = {
            **os.environ,
            "PLUGIN_DATA": str(self.knowledge / "misconfigured-runtime"),
            "OPC_KNOWLEDGE_HOME": str(self.knowledge),
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "opc_hook.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=environment,
            check=True,
        )
        self.assertFalse(json.loads(result.stdout)["continue"])
        self.assertTrue((self.project / ".opc" / "events.jsonl").is_file())
        after = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        self.assertEqual(before, after)

    def test_ready_run_allows_stop(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook test"
        )
        opc_knowledge.update_run(
            root=self.knowledge,
            project_root=self.project,
            evidence={"implementation": "diff", "verification": "tests", "qa": "report"},
            status="ready_for_manager",
        )
        self.assertTrue(self.run_hook(self.project)["continue"])

    def test_invalid_run_creates_no_log(self) -> None:
        bad = self.project / ".opc" / "run.json"
        bad.write_text("{not-json", encoding="utf-8")
        self.assertTrue(self.run_hook(self.project)["continue"])
        self.assertFalse(self.plugin_data.exists())

    def test_inactive_run_creates_no_log(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook test"
        )
        opc_knowledge.update_run(
            root=self.knowledge, project_root=self.project, status="paused"
        )
        self.assertTrue(self.run_hook(self.project)["continue"])
        self.assertFalse(self.plugin_data.exists())

    def test_expired_run_creates_no_log(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook test"
        )
        run_path = self.project / ".opc" / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["expires_at"] = "2000-01-01T00:00:00Z"
        run_path.write_text(json.dumps(run), encoding="utf-8")
        self.assertTrue(self.run_hook(self.project)["continue"])
        self.assertFalse(self.plugin_data.exists())

    def test_project_mismatch_creates_no_log(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook test"
        )
        project_path = self.project / ".opc" / "project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["project_id"] = "different-project"
        project_path.write_text(json.dumps(project), encoding="utf-8")
        self.assertTrue(self.run_hook(self.project)["continue"])
        self.assertFalse(self.plugin_data.exists())

    def test_symlinked_marker_escape_creates_no_log(self) -> None:
        base = self.project.parent
        external = base / "external-marker"
        linked = base / "linked-project"
        external.mkdir()
        linked.mkdir()
        opc_knowledge.init_project(
            project_root=external, project_id="external-marker"
        )
        opc_knowledge.start_run(
            root=self.knowledge, project_root=external, title="Hook test"
        )
        try:
            os.symlink(external / ".opc", linked / ".opc", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        self.assertTrue(self.run_hook(linked)["continue"])
        self.assertFalse(self.plugin_data.exists())

    def test_event_log_rotates_at_bounded_size(self) -> None:
        path = self.plugin_data / "run-events" / "opc-rotation.jsonl"
        event = {
            "event": "Stop",
            "at": "2026-07-13T00:00:00Z",
            "run_id": "opc-rotation",
            "status": "implementing",
            "continue": False,
        }
        with mock.patch.object(opc_hook, "MAX_EVENT_LOG_BYTES", 180):
            for _ in range(8):
                opc_hook._append_event(path, event)
        self.assertTrue(path.is_file())
        self.assertTrue(path.with_name(path.name + ".1").is_file())
        self.assertLessEqual(
            len(list(path.parent.glob(path.name + ".*"))),
            opc_hook.MAX_ROTATED_EVENT_LOGS,
        )

    def test_concurrent_hook_processes_append_valid_json_lines(self) -> None:
        run = opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook concurrency"
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(lambda _index: self.run_hook(self.project), range(16)))
        self.assertTrue(all(response["continue"] is False for response in responses))
        event_path = self.plugin_data / "run-events" / f"{run['run_id']}.jsonl"
        lines = event_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(16, len(lines))
        for line in lines:
            event = json.loads(line)
            self.assertEqual(
                {"event", "at", "run_id", "status", "continue"}, set(event)
            )

    def test_log_write_failure_does_not_break_hook_response(self) -> None:
        opc_knowledge.start_run(
            root=self.knowledge, project_root=self.project, title="Hook failure"
        )
        blocked_parent = self.project.parent / "plugin-data-is-a-file"
        blocked_parent.write_text("not a directory", encoding="utf-8")
        payload = {"hook_event_name": "Stop", "cwd": str(self.project)}
        environment = {
            **os.environ,
            "PLUGIN_DATA": str(blocked_parent),
            "OPC_KNOWLEDGE_HOME": str(self.knowledge),
        }
        before = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "opc_hook.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=environment,
            check=True,
        )
        self.assertFalse(json.loads(result.stdout)["continue"])
        after = sorted(path.relative_to(self.knowledge) for path in self.knowledge.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
