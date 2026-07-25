from __future__ import annotations

import importlib.util
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
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "scripts" / "opc_app_admin.py"
ADMIN_SPEC = importlib.util.spec_from_file_location("opc_app_admin_under_test", ADMIN)
assert ADMIN_SPEC is not None and ADMIN_SPEC.loader is not None
opc_app_admin = importlib.util.module_from_spec(ADMIN_SPEC)
ADMIN_SPEC.loader.exec_module(opc_app_admin)


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


def run_admin_raw(
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADMIN), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )


def make_source(base: Path, version: str) -> Path:
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
    return source


def launcher_snapshot(path: Path) -> dict[str, bytes]:
    return {
        item.name: item.read_bytes()
        for item in path.iterdir()
        if item.is_file() and not item.is_symlink()
    }


def assert_launcher_usable(
    testcase: unittest.TestCase,
    launcher: Path,
    *,
    environment: dict[str, str],
    cwd: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    testcase.assertEqual(result.returncode, 0, result.stderr)
    testcase.assertIn("usage:", result.stdout)


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

            previous_manifest = (
                install_root
                / "releases"
                / first["current"]
                / "release-manifest.json"
            )
            previous_manifest_bytes = previous_manifest.read_bytes()
            previous_manifest.unlink()
            rejected = run_admin_raw(
                "rollback",
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("manifest is missing", rejected.stderr)
            self.assertEqual(
                json.loads((install_root / "current.json").read_text(encoding="utf-8")),
                second,
            )
            previous_manifest.write_bytes(previous_manifest_bytes)

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

    def test_corruption_missing_manifest_conflict_and_link_fail_closed_then_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            source = make_source(base, "9.1.0-integrity")

            run_admin(
                "install",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            pointer = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            release_root = install_root / "releases" / pointer["current"]
            target = release_root / "plugin" / "assets" / "app" / "dashboard.js"
            original = target.read_bytes()

            target.write_bytes(original + b"\n// tampered")
            status = run_admin_raw(
                "status",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertEqual(status.returncode, 1)
            self.assertIn("content digest mismatch", status.stderr)
            launcher = subprocess.run(
                [
                    sys.executable,
                    str(install_root / "bin" / "opc-app.py"),
                    "--demo",
                    "--no-open",
                ],
                cwd=base,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(launcher.returncode, 1)
            self.assertIn("integrity validation", launcher.stderr)

            repair = run_admin(
                "update",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            self.assertIn('"action": "repair"', repair.stdout)
            verified = run_admin(
                "status",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertIn('"integrity": "verified"', verified.stdout)

            extra = release_root / "plugin" / "unexpected.bin"
            extra.write_bytes(b"conflict")
            conflict = run_admin_raw(
                "status",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertEqual(conflict.returncode, 1)
            self.assertIn("file inventory mismatch", conflict.stderr)
            run_admin(
                "update",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )

            manifest_path = release_root / "release-manifest.json"
            manifest_path.unlink()
            missing = run_admin_raw(
                "status",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("manifest is missing", missing.stderr)
            run_admin(
                "update",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )

            outside = base / "outside.js"
            outside.write_bytes(original)
            target = release_root / "plugin" / "assets" / "app" / "dashboard.js"
            target.unlink()
            try:
                os.symlink(outside, target)
            except (OSError, NotImplementedError):
                self.skipTest("file symlink creation is not permitted")
            linked = run_admin_raw(
                "status",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertEqual(linked.returncode, 1)
            self.assertIn("linked content", linked.stderr)

    def test_linked_launcher_boundaries_never_modify_outside_sentinels(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            source = make_source(base, "9.1.1-launcher-links")
            run_admin(
                "install",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            pointer_before = (install_root / "current.json").read_bytes()
            launcher_root = install_root / "bin"
            launcher_backup = base / "safe-launchers"
            shutil.copytree(launcher_root, launcher_backup)

            outside_directory = base / "outside-bin"
            outside_directory.mkdir()
            outside_directory_sentinel = outside_directory / "sentinel.bin"
            outside_directory_sentinel.write_bytes(b"outside-directory-preserve")
            shutil.rmtree(launcher_root)
            try:
                os.symlink(outside_directory, launcher_root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is not permitted")
            linked_bin = run_admin_raw(
                "update",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            self.assertEqual(linked_bin.returncode, 1)
            self.assertIn("outside the install root", linked_bin.stderr)
            self.assertEqual(
                outside_directory_sentinel.read_bytes(),
                b"outside-directory-preserve",
            )
            self.assertEqual((install_root / "current.json").read_bytes(), pointer_before)
            launcher_root.unlink()
            shutil.copytree(launcher_backup, launcher_root)

            outside_file = base / "outside-launcher.cmd"
            outside_file.write_bytes(b"outside-file-preserve")
            linked_launcher = launcher_root / "opc-app.cmd"
            linked_launcher.unlink()
            try:
                os.symlink(outside_file, linked_launcher)
            except (OSError, NotImplementedError):
                self.skipTest("file symlink creation is not permitted")
            linked_entry = run_admin_raw(
                "update",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            self.assertEqual(linked_entry.returncode, 1)
            self.assertIn("outside the install root", linked_entry.stderr)
            self.assertEqual(outside_file.read_bytes(), b"outside-file-preserve")
            self.assertEqual((install_root / "current.json").read_bytes(), pointer_before)

    def test_first_and_second_launcher_write_failures_preserve_old_complete_set(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            source = make_source(base, "9.1.2-launcher-writes")
            run_admin(
                "install",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            launcher_root = install_root / "bin"
            before = launcher_snapshot(launcher_root)
            pointer_before = (install_root / "current.json").read_bytes()
            arguments = SimpleNamespace(
                source=str(source),
                install_root=str(install_root),
                apply=True,
                dry_run=False,
            )
            original_write = opc_app_admin._write_launcher_file

            for fail_at in (1, 2):
                with self.subTest(fail_at=fail_at):
                    calls = 0

                    def fail_selected_write(path, payload, *, executable=False):
                        nonlocal calls
                        calls += 1
                        if calls == fail_at:
                            raise opc_app_admin.AppInstallError(
                                f"injected launcher write {fail_at}"
                            )
                        return original_write(
                            path,
                            payload,
                            executable=executable,
                        )

                    with (
                        mock.patch.object(
                            opc_app_admin,
                            "_write_launcher_file",
                            side_effect=fail_selected_write,
                        ),
                        self.assertRaises(opc_app_admin.AppInstallError),
                    ):
                        opc_app_admin.install_or_update(arguments)
                    self.assertEqual(launcher_snapshot(launcher_root), before)
                    self.assertEqual(
                        (install_root / "current.json").read_bytes(),
                        pointer_before,
                    )
                    self.assertEqual(list(install_root.glob(".launcher-stage-*")), [])
                    self.assertEqual(list(install_root.glob(".launcher-backup-*")), [])
                    assert_launcher_usable(
                        self,
                        launcher_root / "opc-app.py",
                        environment=environment,
                        cwd=base,
                    )

    def test_launcher_digest_tamper_fails_status_and_launch_then_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            source = make_source(base, "9.1.25-launcher-integrity")
            run_admin(
                "install",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            command_launcher = install_root / "bin" / "opc-app.cmd"
            command_launcher.write_bytes(command_launcher.read_bytes() + b"\r\nrem tampered")
            status = run_admin_raw(
                "status",
                "--install-root",
                str(install_root),
                environment=environment,
            )
            self.assertEqual(status.returncode, 1)
            self.assertIn("launcher artifact failed integrity", status.stderr)
            launch = subprocess.run(
                [
                    sys.executable,
                    str(install_root / "bin" / "opc-app.py"),
                    "--help",
                ],
                cwd=base,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(launch.returncode, 1)
            self.assertIn("launcher or active release", launch.stderr)

            run_admin(
                "update",
                "--source",
                str(source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            opc_app_admin._validate_launcher_set(install_root)
            assert_launcher_usable(
                self,
                install_root / "bin" / "opc-app.py",
                environment=environment,
                cwd=base,
            )

    def test_launcher_activation_failure_restores_old_set_and_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            first_source = make_source(base, "9.1.3-launcher-old")
            second_source = make_source(base, "9.1.4-launcher-new")
            run_admin(
                "install",
                "--source",
                str(first_source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            launcher_root = install_root / "bin"
            before = launcher_snapshot(launcher_root)
            pointer_before = (install_root / "current.json").read_bytes()
            arguments = SimpleNamespace(
                source=str(second_source),
                install_root=str(install_root),
                apply=True,
                dry_run=False,
            )
            original_replace = opc_app_admin.os.replace

            def fail_activation(source_path, destination_path):
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                if (
                    source_path.name.startswith(".launcher-stage-")
                    and destination_path == launcher_root
                ):
                    raise PermissionError("injected launcher activation failure")
                return original_replace(source_path, destination_path)

            with (
                mock.patch.object(
                    opc_app_admin.os,
                    "replace",
                    side_effect=fail_activation,
                ),
                self.assertRaises(opc_app_admin.AppInstallError),
            ):
                opc_app_admin.install_or_update(arguments)
            self.assertEqual(launcher_snapshot(launcher_root), before)
            self.assertEqual((install_root / "current.json").read_bytes(), pointer_before)
            self.assertEqual(list(install_root.glob(".launcher-backup-*")), [])
            assert_launcher_usable(
                self,
                launcher_root / "opc-app.py",
                environment=environment,
                cwd=base,
            )

    def test_launcher_restore_failure_preserves_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            first_source = make_source(base, "9.1.5-launcher-old")
            second_source = make_source(base, "9.1.6-launcher-new")
            run_admin(
                "install",
                "--source",
                str(first_source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            launcher_root = install_root / "bin"
            before = launcher_snapshot(launcher_root)
            pointer_before = (install_root / "current.json").read_bytes()
            arguments = SimpleNamespace(
                source=str(second_source),
                install_root=str(install_root),
                apply=True,
                dry_run=False,
            )
            original_replace = opc_app_admin.os.replace

            def fail_activation_and_restore(source_path, destination_path):
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                if destination_path == launcher_root and (
                    source_path.name.startswith(".launcher-stage-")
                    or source_path.name.startswith(".launcher-backup-")
                ):
                    raise PermissionError("injected launcher swap failure")
                return original_replace(source_path, destination_path)

            with (
                mock.patch.object(
                    opc_app_admin.os,
                    "replace",
                    side_effect=fail_activation_and_restore,
                ),
                self.assertRaises(opc_app_admin.AppInstallError),
            ):
                opc_app_admin.install_or_update(arguments)
            self.assertFalse(launcher_root.exists())
            backups = list(install_root.glob(".launcher-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(launcher_snapshot(backups[0]), before)
            opc_app_admin._validate_launcher_set(install_root, backups[0])
            self.assertEqual((install_root / "current.json").read_bytes(), pointer_before)
            assert_launcher_usable(
                self,
                backups[0] / "opc-app.py",
                environment=environment,
                cwd=base,
            )

    def test_permission_and_pointer_activation_failures_preserve_current_release(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            home.mkdir()
            state_root = base / "app-state"
            install_root = base / "runtime"
            environment = clean_environment(home, state_root)
            first_source = make_source(base, "9.2.0-first")
            second_source = make_source(base, "9.2.1-second")

            run_admin(
                "install",
                "--source",
                str(first_source),
                "--install-root",
                str(install_root),
                "--apply",
                environment=environment,
            )
            pointer_path = install_root / "current.json"
            before = json.loads(pointer_path.read_text(encoding="utf-8"))
            arguments = SimpleNamespace(
                source=str(second_source),
                install_root=str(install_root),
                apply=True,
                dry_run=False,
            )

            with (
                mock.patch.object(
                    opc_app_admin.shutil,
                    "copytree",
                    side_effect=PermissionError("denied"),
                ),
                self.assertRaises(opc_app_admin.AppInstallError),
            ):
                opc_app_admin.install_or_update(arguments)
            self.assertEqual(
                json.loads(pointer_path.read_text(encoding="utf-8")),
                before,
            )
            self.assertEqual(list(install_root.glob(".stage-*")), [])

            original_atomic = opc_app_admin._atomic_json

            def fail_pointer(path, payload):
                if Path(path).name == "current.json":
                    raise opc_app_admin.AppInstallError("injected pointer failure")
                return original_atomic(path, payload)

            with (
                mock.patch.object(
                    opc_app_admin,
                    "_atomic_json",
                    side_effect=fail_pointer,
                ),
                self.assertRaises(opc_app_admin.AppInstallError),
            ):
                opc_app_admin.install_or_update(arguments)
            self.assertEqual(
                json.loads(pointer_path.read_text(encoding="utf-8")),
                before,
            )
            opc_app_admin._validate_release(
                install_root / "releases" / before["current"],
                expected_release=before["current"],
            )
            opc_app_admin._validate_launcher_set(install_root)
            assert_launcher_usable(
                self,
                install_root / "bin" / "opc-app.py",
                environment=environment,
                cwd=base,
            )

            second_plugin = opc_app_admin._plugin_source(second_source)
            second_manifest = opc_app_admin._manifest(second_plugin)
            second_release = opc_app_admin._release_id(
                second_plugin,
                second_manifest["version"],
            )
            second_root = install_root / "releases" / second_release
            target = second_root / "plugin" / "assets" / "app" / "dashboard.js"
            target.write_bytes(target.read_bytes() + b"\n// corrupt")
            original_replace = opc_app_admin.os.replace

            def fail_replacement_and_restore(source_path, destination_path):
                source_path = Path(source_path)
                destination_path = Path(destination_path)
                if (
                    destination_path == second_root
                    and (
                        source_path.name.startswith(".stage-")
                        or source_path.name.startswith(".corrupt-")
                    )
                ):
                    raise PermissionError("injected replacement failure")
                return original_replace(source_path, destination_path)

            with (
                mock.patch.object(
                    opc_app_admin.os,
                    "replace",
                    side_effect=fail_replacement_and_restore,
                ),
                self.assertRaises(opc_app_admin.AppInstallError),
            ):
                opc_app_admin._install_validated_release(
                    root=install_root,
                    plugin=second_plugin,
                    version=second_manifest["version"],
                    release=second_release,
                )
            quarantined = list(install_root.glob(f".corrupt-{second_release}-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertFalse(second_root.exists())
            self.assertTrue(
                (
                    quarantined[0]
                    / "plugin"
                    / "assets"
                    / "app"
                    / "dashboard.js"
                ).is_file()
            )
            self.assertEqual(
                json.loads(pointer_path.read_text(encoding="utf-8")),
                before,
            )


if __name__ == "__main__":
    unittest.main()
