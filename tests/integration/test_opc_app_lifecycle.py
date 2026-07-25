from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "scripts" / "opc_app_admin.py"


def clean_environment(home: Path, state_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(home / "local"),
            "XDG_DATA_HOME": str(home / "share"),
            "XDG_STATE_HOME": str(home / "state"),
            "OPC_APP_HOME": str(state_root),
        }
    )
    return environment


def run_admin(
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADMIN), *arguments],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )


class OPCAppLifecycleTests(unittest.TestCase):
    def test_uninstall_refuses_an_unowned_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "state"
            unowned = base / "unowned"
            unowned.mkdir()
            marker = unowned / "user-file.txt"
            marker.write_text("preserve", encoding="utf-8")
            environment = clean_environment(home, state_root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(ADMIN),
                    "uninstall",
                    "--install-root",
                    str(unowned),
                    "--apply",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 1)
            self.assertTrue(marker.is_file())

    def test_clean_room_install_launch_stop_uninstall_preserves_all_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            install_root = base / "runtime"
            state_root = base / "app-state"
            state_root.mkdir()
            state_marker = state_root / "preference.json"
            state_marker.write_text('{"theme":"system"}', encoding="utf-8")
            project_root = base / "project"
            (project_root / ".opc").mkdir(parents=True)
            project_marker = project_root / ".opc" / "project.json"
            project_marker.write_text('{"project_id":"preserved"}', encoding="utf-8")
            knowledge_root = base / "knowledge"
            (knowledge_root / ".git").mkdir(parents=True)
            knowledge_marker = knowledge_root / "catalog.json"
            knowledge_marker.write_text('{"schema_version":2}', encoding="utf-8")
            mem0_root = base / "mem0"
            mem0_root.mkdir()
            mem0_marker = mem0_root / "index.bin"
            mem0_marker.write_bytes(b"preserve")
            environment = clean_environment(home, state_root)

            preview = run_admin(
                "install",
                "--source",
                str(ROOT),
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertIn('"dry_run": true', preview.stdout)
            self.assertFalse(install_root.exists())

            run_admin(
                "install",
                "--source",
                str(ROOT),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            python_launcher = install_root / "bin" / "opc-app.py"
            self.assertTrue(python_launcher.is_file())
            self.assertTrue((install_root / "bin" / "opc-app.cmd").is_file())
            self.assertTrue((install_root / "bin" / "opc-app").is_file())

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(python_launcher),
                    "--demo",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--no-open",
                ],
                cwd=base,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                self.assertTrue(line.startswith("OPC App: http://127.0.0.1:"), line)
                url = line.removeprefix("OPC App: ")
                with urllib.request.urlopen(url + "api/app-context", timeout=5) as response:
                    context = json.loads(response.read())
                self.assertEqual(context["schema_version"], "opc-app.context.v1")
                self.assertEqual(process.poll(), None)
            finally:
                process.terminate()
                process.wait(timeout=10)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            self.assertIsNotNone(process.returncode)

            run_admin(
                "uninstall",
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            self.assertFalse(install_root.exists())
            self.assertEqual(state_marker.read_text(encoding="utf-8"), '{"theme":"system"}')
            self.assertTrue(project_marker.is_file())
            self.assertTrue(knowledge_marker.is_file())
            self.assertEqual(mem0_marker.read_bytes(), b"preserve")

    def test_update_and_rollback_switch_release_pointer_without_touching_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            state_root.mkdir()
            state_marker = state_root / "settings.json"
            state_marker.write_text('{"private":"preserve"}', encoding="utf-8")
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)

            sources: list[Path] = []
            for version in ("9.0.0-test", "9.0.1-test"):
                source = base / f"source-{version}"
                plugin_target = source / "plugins" / "codex-opc-team"
                shutil.copytree(
                    ROOT / "plugins" / "codex-opc-team",
                    plugin_target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
                manifest_path = plugin_target / ".codex-plugin" / "plugin.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["version"] = version
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                sources.append(source)

            run_admin(
                "install",
                "--source",
                str(sources[0]),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            first = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            run_admin(
                "update",
                "--source",
                str(sources[1]),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            second = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            self.assertNotEqual(first["current"], second["current"])
            self.assertEqual(second["previous"], first["current"])

            preview = run_admin(
                "rollback",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertIn('"dry_run": true', preview.stdout)
            unchanged = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(unchanged, second)

            run_admin(
                "rollback",
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            rolled_back = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(rolled_back["current"], first["current"])
            self.assertEqual(state_marker.read_text(encoding="utf-8"), '{"private":"preserve"}')


if __name__ == "__main__":
    unittest.main()
