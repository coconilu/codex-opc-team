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
from unittest import mock


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
            injected = {
                "OPENAI_API_KEY": "synthetic-secret-that-must-not-propagate",
                "CODEX_ACCESS_TOKEN": "host-auth-sentinel",
                "CODEX_CONNECTORS_TOKEN": "host-connector-sentinel",
                "CODEX_GITHUB_PERSONAL_ACCESS_TOKEN": "host-github-sentinel",
                "CODEX_THREAD_ID": "host-thread-sentinel",
                "PLUGIN_DATA": str(Path(temp) / "host-plugin-data"),
                "MEM0_DIR": str(Path(temp) / "host-mem0"),
                "SSH_AUTH_SOCK": str(Path(temp) / "host-agent"),
            }
            with mock.patch.dict(os.environ, injected, clear=False):
                env = lifecycle._clean_env(paths)

            self.assertEqual(str(paths["codex_home"]), env["CODEX_HOME"])
            self.assertEqual(str(paths["user_home"]), env["HOME"])
            self.assertEqual(str(paths["user_home"]), env["USERPROFILE"])
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("SSH_AUTH_SOCK", env)
            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
            self.assertNotIn("CODEX_CONNECTORS_TOKEN", env)
            self.assertNotIn("CODEX_GITHUB_PERSONAL_ACCESS_TOKEN", env)
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertEqual(str(paths["plugin_data"]), env["PLUGIN_DATA"])
            self.assertEqual(str(paths["memory"] / "mem0"), env["MEM0_DIR"])
            self.assertEqual(str(paths["runtime_tmp"]), env["TEMP"])
            self.assertEqual("1", env["GIT_CONFIG_NOSYSTEM"])

    def test_exact_skill_catalog_rejects_each_missing_opc_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            allowed = workspace / "codex-home" / "skills"
            entries = [
                {
                    "name": name,
                    "locator_kind": "file",
                    "locator": str(allowed / name / "SKILL.md"),
                }
                for name in (*lifecycle.SKILLS, lifecycle.FIXTURE_SKILL)
            ]
            result = lifecycle._validate_skill_catalog(
                entries, expect_opc=True, workspace=workspace
            )
            self.assertEqual(sorted(lifecycle.SKILLS), result["opc_skills"])
            for missing in lifecycle.SKILLS:
                reduced = [entry for entry in entries if entry["name"] != missing]
                with self.subTest(missing=missing), self.assertRaisesRegex(
                    lifecycle.AcceptanceError, "missed exact OPC skills"
                ):
                    lifecycle._validate_skill_catalog(
                        reduced, expect_opc=True, workspace=workspace
                    )

    def test_candidate_and_rollback_validate_their_own_installed_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            installed = Path(temp) / "installed-plugin"
            old_skills = tuple(
                name for name in lifecycle.SKILLS if name != "codex-opc-team:opc-shadow-evaluation"
            )
            for name in old_skills:
                folder = name.split(":", 1)[1]
                skill = installed / "skills" / folder / "SKILL.md"
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text("synthetic", encoding="utf-8")
            expected = lifecycle._installed_opc_skills({"installedPath": str(installed)})
            self.assertEqual(tuple(sorted(old_skills)), expected)
            entries = [
                {
                    "name": name,
                    "locator_kind": "file",
                    "locator": str(workspace / "skills" / name / "SKILL.md"),
                }
                for name in (*old_skills, lifecycle.FIXTURE_SKILL)
            ]
            result = lifecycle._validate_skill_catalog(
                entries,
                expect_opc=True,
                workspace=workspace,
                expected_opc=expected,
            )
            self.assertEqual(list(expected), result["opc_skills"])
            entries.insert(
                -1,
                {
                    "name": "codex-opc-team:opc-shadow-evaluation",
                    "locator_kind": "file",
                    "locator": str(workspace / "skills" / "unexpected" / "SKILL.md"),
                },
            )
            with self.assertRaisesRegex(lifecycle.AcceptanceError, "unexpected OPC skill set"):
                lifecycle._validate_skill_catalog(
                    entries,
                    expect_opc=True,
                    workspace=workspace,
                    expected_opc=expected,
                )

    def test_skill_catalog_rejects_injected_host_locator(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            entries = [
                {
                    "name": name,
                    "locator_kind": "file",
                    "locator": str(workspace / "skills" / name / "SKILL.md"),
                }
                for name in (*lifecycle.SKILLS, lifecycle.FIXTURE_SKILL)
            ]
            entries.append(
                {
                    "name": "injected-host-sentinel",
                    "locator_kind": "file",
                    "locator": str(Path(temp) / "host-home" / ".agents" / "skills" / "sentinel" / "SKILL.md"),
                }
            )
            with self.assertRaisesRegex(lifecycle.AcceptanceError, "escaped the clean room"):
                lifecycle._validate_skill_catalog(
                    entries, expect_opc=True, workspace=workspace
                )

    def test_skill_catalog_allows_only_codex_system_files_outside_clean_room(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "clean-room"
            entries = [
                {
                    "name": name,
                    "locator_kind": "file",
                    "locator": str(workspace / "skills" / name / "SKILL.md"),
                }
                for name in (*lifecycle.SKILLS, lifecycle.FIXTURE_SKILL)
            ]
            entries.append(
                {
                    "name": "openai-docs",
                    "locator_kind": "file",
                    "locator": str(
                        Path(temp)
                        / "codex-install"
                        / "skills"
                        / ".system"
                        / "openai-docs"
                        / "SKILL.md"
                    ),
                }
            )
            result = lifecycle._validate_skill_catalog(
                entries, expect_opc=True, workspace=workspace
            )
            self.assertEqual(["openai-docs"], result["allowed_codex_system_skills"])

    def test_catalog_parser_uses_canonical_full_names(self):
        workspace = Path("synthetic-clean-room").resolve()
        lines = [
            f"- {name}: synthetic (file: {workspace / name / 'SKILL.md'})"
            for name in (*lifecycle.SKILLS, lifecycle.FIXTURE_SKILL)
        ]
        prompt = [
            {
                "type": "input_text",
                "text": "<skills_instructions>\n### Available skills\n"
                + "\n".join(lines)
                + "\n</skills_instructions>",
            }
        ]
        parsed = lifecycle._parse_skill_catalog(prompt)
        self.assertEqual(
            set((*lifecycle.SKILLS, lifecycle.FIXTURE_SKILL)),
            {entry["name"] for entry in parsed},
        )

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

    @unittest.skipUnless(shutil.which("git"), "Git is required for fixture isolation")
    def test_fixture_git_ignores_host_config_templates_hooks_and_signing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "clean-room"
            paths = lifecycle._paths(workspace)
            hostile_templates = root / "host-templates"
            hostile_hooks = hostile_templates / "hooks"
            hostile_hooks.mkdir(parents=True)
            hook_marker = root / "host-hook-ran.txt"
            hook = hostile_hooks / "pre-commit"
            hook.write_text(
                "#!/bin/sh\nprintf hostile > \""
                + hook_marker.as_posix()
                + "\"\nexit 1\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            hostile_config = root / "host.gitconfig"
            hostile_config.write_text(
                "[commit]\n\tgpgSign = true\n"
                "[tag]\n\tgpgSign = true\n"
                "[init]\n\ttemplateDir = "
                + hostile_templates.as_posix()
                + "\n[core]\n\thooksPath = "
                + hostile_hooks.as_posix()
                + "\n[credential]\n\thelper = !false\n",
                encoding="utf-8",
            )
            hostile_env = {
                "GIT_CONFIG_GLOBAL": str(hostile_config),
                "GIT_TEMPLATE_DIR": str(hostile_templates),
                "GIT_CONFIG_SYSTEM": str(hostile_config),
                "GIT_OBJECT_DIRECTORY": str(root / "host-objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(root / "host-alternates"),
            }
            with mock.patch.dict(os.environ, hostile_env, clear=False):
                lifecycle._prepare_fixture_tree(paths)

            self.assertFalse(hook_marker.exists())
            self.assertFalse(
                (paths["knowledge"] / ".git" / "hooks" / "pre-commit").exists()
            )
            self.assertEqual(
                "",
                lifecycle._git(
                    paths["knowledge"],
                    "status",
                    "--porcelain=v1",
                    env=lifecycle._git_env(paths),
                ),
            )
            self.assertEqual(
                paths["probe"].resolve(),
                Path(
                    lifecycle._git(
                        paths["probe"],
                        "rev-parse",
                        "--show-toplevel",
                        env=lifecycle._git_env(paths),
                    )
                ).resolve(),
            )

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

    def test_release_resolution_rejects_moving_or_same_commit_refs(self):
        oid_a = "a" * 40
        oid_b = "b" * 40
        with self.assertRaisesRegex(lifecycle.AcceptanceError, "moving branch"):
            lifecycle._validate_release_resolutions(
                {"requested_ref": "main", "resolved_oid": oid_a, "ref_kind": "moving"},
                {"requested_ref": "v0.1.0", "resolved_oid": oid_b, "ref_kind": "tag"},
            )
        with self.assertRaisesRegex(lifecycle.AcceptanceError, "same commit OID"):
            lifecycle._validate_release_resolutions(
                {"requested_ref": oid_a, "resolved_oid": oid_a, "ref_kind": "oid"},
                {"requested_ref": "v0.1.0", "resolved_oid": oid_a, "ref_kind": "tag"},
            )

    def test_reapply_version_change_fails_closed(self):
        lifecycle._require_same_version("0.1.1", "0.1.1", "candidate")
        with self.assertRaisesRegex(lifecycle.AcceptanceError, "changed"):
            lifecycle._require_same_version("0.1.1", "0.1.2", "candidate")

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
    os.environ.get("OPC_DISPOSABLE_LIFECYCLE_HOST") == "1"
    and codex_supports_prompt_discovery(),
    "real installed-state acceptance requires an explicitly disposable OS/container",
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
            candidate_payload["version"] = "0.1.1-rc.1"
            candidate_manifest.write_text(
                json.dumps(candidate_payload, indent=2) + "\n", encoding="utf-8"
            )
            rollback_manifest = (
                rollback_source
                / "plugins"
                / "codex-opc-team"
                / ".codex-plugin"
                / "plugin.json"
            )
            rollback_payload = json.loads(rollback_manifest.read_text(encoding="utf-8"))
            rollback_payload["version"] = "0.1.0"
            rollback_manifest.write_text(
                json.dumps(rollback_payload, indent=2) + "\n", encoding="utf-8"
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

            # An explicit hostile host profile and auth/session environment
            # prove the child process does not inherit either discovery state
            # or credentials from the runner account.
            host_home = Path(temp) / "host-user"
            host_skill = host_home / ".agents" / "skills" / "host-sentinel"
            host_skill.mkdir(parents=True)
            (host_skill / "SKILL.md").write_text(
                "---\nname: host-sentinel\ndescription: must not be discovered\n---\n",
                encoding="utf-8",
            )
            hostile_environment = {
                "HOME": str(host_home),
                "USERPROFILE": str(host_home),
                "OPENAI_API_KEY": "synthetic-host-secret",
                "CODEX_ACCESS_TOKEN": "synthetic-host-session",
                "CODEX_THREAD_ID": "synthetic-host-thread",
            }

            with mock.patch.dict(os.environ, hostile_environment, clear=False):
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
            self.assertEqual(
                "0.1.1-rc.1", first["checks"]["candidate_install"]["version"]
            )
            self.assertEqual("0.1.0", first["checks"]["rollback"]["version"])
            self.assertTrue(first["checks"]["rollback"]["distinct_version"])
            self.assertNotIn(str(workspace), json.dumps(first))
            self.assertNotIn("host-sentinel", json.dumps(first))
            self.assertNotIn("synthetic-host", json.dumps(first))

            # A second apply reuses only the tool-owned clean room.  This proves
            # recovery/idempotency without deleting knowledge or provider data.
            with mock.patch.dict(os.environ, hostile_environment, clear=False):
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
