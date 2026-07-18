from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "plugin_lifecycle_acceptance",
        ROOT / "scripts" / "plugin_lifecycle_acceptance.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lifecycle = load_module()


def codex_supports_prompt_discovery() -> bool:
    executable = shutil.which("codex")
    if not executable:
        return False
    result = subprocess.run(
        [executable, "debug", "prompt-input", "--help"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class LifecycleContractTests(unittest.TestCase):
    def test_preview_is_non_mutating_and_describes_data_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "planned-clean-room"
            parser = lifecycle.build_parser()
            args = parser.parse_args(["--workspace", str(workspace), "--dry-run"])
            args.rollback_source = args.candidate_source

            result = lifecycle.run_acceptance(args)

            self.assertTrue(result["dry_run"])
            self.assertFalse(workspace.exists())
            self.assertEqual("none-outside-clean-room", result["global_codex_config_action"])
            self.assertIn("read-only", result["canonical_knowledge_action"])

            report = Path(temp) / "preview.json"
            lifecycle._write_report(str(report), result)
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual("isolated-clean-room", saved["workspace"])
            self.assertNotIn(str(workspace), report.read_text(encoding="utf-8"))

    def test_nonempty_unowned_workspace_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "not-owned"
            workspace.mkdir()
            (workspace / "user-file.txt").write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(lifecycle.AcceptanceError, "ownership marker"):
                lifecycle.validate_workspace(workspace)

            self.assertEqual(
                "preserve", (workspace / "user-file.txt").read_text(encoding="utf-8")
            )

    def test_public_repository_cannot_be_used_as_clean_room(self):
        with self.assertRaisesRegex(lifecycle.AcceptanceError, "public repository"):
            lifecycle.validate_workspace(ROOT / ".lifecycle-fixture")

    def test_clean_environment_isolates_home_and_removes_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = lifecycle._paths(Path(temp) / "clean-room")
            old_key = os.environ.get("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = "synthetic-secret-that-must-not-propagate"
            try:
                env = lifecycle._clean_env(paths)
            finally:
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key

            self.assertEqual(str(paths["codex_home"]), env["CODEX_HOME"])
            self.assertEqual(str(paths["user_home"]), env["HOME"])
            self.assertEqual(str(paths["user_home"]), env["USERPROFILE"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("SSH_AUTH_SOCK", env)
            self.assertEqual("1", env["GIT_CONFIG_NOSYSTEM"])

    def test_owned_config_filter_ignores_only_opc_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text(
                "[unrelated]\npreserve = \"yes\"\n\n"
                "[marketplaces.opc]\nsource = \"first\"\n\n"
                "[plugins.\"codex-opc-team@opc\"]\nenabled = true\n",
                encoding="utf-8",
            )
            before = lifecycle._unrelated_config_sha256(config)
            config.write_text(
                "[unrelated]\npreserve = \"yes\"\n\n"
                "[marketplaces.opc]\nsource = \"second\"\n",
                encoding="utf-8",
            )
            self.assertEqual(before, lifecycle._unrelated_config_sha256(config))
            config.write_text(
                "[unrelated]\npreserve = \"changed\"\n",
                encoding="utf-8",
            )
            self.assertNotEqual(before, lifecycle._unrelated_config_sha256(config))

    def test_release_mode_requires_distinct_fixed_git_refs(self):
        with tempfile.TemporaryDirectory() as temp:
            parser = lifecycle.build_parser()
            args = parser.parse_args(
                [
                    "--workspace",
                    str(Path(temp) / "clean-room"),
                    "--candidate-source",
                    str(ROOT),
                    "--rollback-ref",
                    "v0.1.0",
                    "--require-fixed-refs",
                    "--apply",
                ]
            )
            args.rollback_source = args.candidate_source
            with self.assertRaisesRegex(lifecycle.AcceptanceError, "requires candidate"):
                lifecycle.run_acceptance(args)

    def test_release_mode_rejects_same_manifest_version(self):
        with tempfile.TemporaryDirectory() as temp:
            parser = lifecycle.build_parser()
            args = parser.parse_args(
                [
                    "--workspace",
                    str(Path(temp) / "clean-room"),
                    "--candidate-source",
                    "example/project",
                    "--candidate-ref",
                    "v0.1.1",
                    "--expected-candidate-version",
                    "0.1.0",
                    "--rollback-source",
                    "example/project",
                    "--rollback-ref",
                    "v0.1.0",
                    "--expected-rollback-version",
                    "0.1.0",
                    "--require-fixed-refs",
                    "--apply",
                ]
            )
            with self.assertRaisesRegex(lifecycle.AcceptanceError, "distinct candidate"):
                lifecycle.run_acceptance(args)

    def test_report_source_strips_url_credentials_and_query(self):
        result = lifecycle._source_report(
            "https://user:token@example.invalid/org/repo?secret=value", "v1.2.3"
        )
        serialized = json.dumps(result)
        self.assertEqual("https://example.invalid/org/repo", result["source"])
        self.assertNotIn("token", serialized)
        self.assertNotIn("secret", serialized)

    def test_report_cannot_pollute_knowledge_or_public_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            with self.assertRaisesRegex(lifecycle.AcceptanceError, "inside knowledge"):
                lifecycle.validate_report_target(
                    str(workspace / "knowledge" / "report.json"), workspace
                )
            with self.assertRaisesRegex(lifecycle.AcceptanceError, "public repository"):
                lifecycle.validate_report_target(
                    str(ROOT / "lifecycle-report.json"), workspace
                )

    def test_dry_run_with_report_does_not_create_clean_room(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            report = Path(temp) / "preview.json"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = lifecycle.main(
                    [
                        "--workspace",
                        str(workspace),
                        "--report",
                        str(report),
                        "--dry-run",
                    ]
                )
            self.assertEqual(0, exit_code)
            self.assertFalse(workspace.exists())
            self.assertTrue(report.is_file())


@unittest.skipUnless(
    codex_supports_prompt_discovery(),
    "Codex CLI with debug prompt-input is required for installed-state acceptance",
)
class RealCodexLifecycleTests(unittest.TestCase):
    def test_real_clean_room_install_discover_uninstall_reinstall_and_reapply(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            report_path = workspace / "redacted-report.json"
            candidate_source = Path(temp) / "candidate-marketplace"
            rollback_source = Path(temp) / "rollback-marketplace"
            for source in (candidate_source, rollback_source):
                ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
                shutil.copytree(ROOT / ".agents", source / ".agents", ignore=ignore)
                shutil.copytree(ROOT / "plugins", source / "plugins", ignore=ignore)
            candidate_manifest = (
                candidate_source
                / "plugins"
                / "codex-opc-team"
                / ".codex-plugin"
                / "plugin.json"
            )
            candidate_payload = json.loads(candidate_manifest.read_text(encoding="utf-8"))
            candidate_payload["version"] = "0.1.1"
            candidate_manifest.write_text(
                json.dumps(candidate_payload, indent=2) + "\n", encoding="utf-8"
            )
            command = [
                "--workspace",
                str(workspace),
                "--candidate-source",
                str(candidate_source),
                "--rollback-source",
                str(rollback_source),
                "--report",
                str(report_path),
                "--apply",
            ]

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                first_exit = lifecycle.main(command)
            self.assertEqual(0, first_exit)
            first = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual("pass", first["status"])
            self.assertEqual(
                sorted(lifecycle.SKILLS),
                first["checks"]["candidate_install"]["discovery"]["opc_skills"],
            )
            self.assertEqual(
                [], first["checks"]["uninstall"]["discovery"]["opc_skills"]
            )
            self.assertTrue(first["checks"]["uninstall"]["unrelated_plugin_present"])
            self.assertTrue(first["protected_data"]["preserved"])
            self.assertFalse(first["release_gate"]["eligible"])
            self.assertEqual("0.1.1", first["checks"]["candidate_install"]["version"])
            self.assertEqual("0.1.0", first["checks"]["rollback"]["version"])
            self.assertTrue(first["checks"]["rollback"]["distinct_version"])
            self.assertNotIn(str(workspace), json.dumps(first))

            # A second apply reuses only the tool-owned clean room.  This proves
            # recovery/idempotency without deleting knowledge or provider data.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                second_exit = lifecycle.main(command)
            self.assertEqual(0, second_exit)
            second = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual("pass", second["status"])
            self.assertEqual(
                first["protected_data"]["knowledge_head"],
                second["protected_data"]["knowledge_head"],
            )
            self.assertEqual(
                first["protected_data"]["memory_files_sha256"],
                second["protected_data"]["memory_files_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
