from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


privacy_scan = load("privacy_scan", "scripts/privacy_scan.py")
migrate_legacy = load("migrate_legacy", "scripts/migrate_legacy.py")
plugin_admin = load("plugin_admin", "scripts/plugin_admin.py")
validate_repo = load("validate_repo", "scripts/validate_repo.py")


class VersionContractTests(unittest.TestCase):
    @staticmethod
    def tag_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git", "tag", "--points-at", "HEAD"],
            returncode=returncode,
            stdout=stdout,
            stderr="synthetic tag enumeration failure" if returncode else "",
        )

    @mock.patch.object(validate_repo.subprocess, "run")
    def test_exact_release_candidate_tag_passes(self, run):
        run.return_value = self.tag_result("v0.1.1-rc.1\n")
        validate_repo.validate_version_contract()

    @mock.patch.object(validate_repo.subprocess, "run")
    def test_untagged_candidate_commit_passes(self, run):
        run.return_value = self.tag_result("")
        validate_repo.validate_version_contract()

    @mock.patch.object(validate_repo.subprocess, "run")
    def test_pep440_style_release_candidate_tag_fails(self, run):
        run.return_value = self.tag_result("v0.1.1rc1\n")
        with self.assertRaisesRegex(ValueError, "must be exactly v0.1.1-rc.1"):
            validate_repo.validate_version_contract()

    @mock.patch.object(validate_repo.subprocess, "run")
    def test_wrong_or_multiple_release_candidate_tags_fail(self, run):
        for stdout in (
            "v0.1.2-rc.1\n",
            "v0.1.1-rc.1\nv0.1.1-rc.2\n",
        ):
            with self.subTest(stdout=stdout):
                run.return_value = self.tag_result(stdout)
                with self.assertRaisesRegex(ValueError, "must be exactly v0.1.1-rc.1"):
                    validate_repo.validate_version_contract()

    @mock.patch.object(validate_repo.subprocess, "run")
    def test_malformed_semver_tag_fails(self, run):
        run.return_value = self.tag_result("v0.1.1-\n")
        with self.assertRaisesRegex(ValueError, "must be exactly v0.1.1-rc.1"):
            validate_repo.validate_version_contract()

    @mock.patch.object(validate_repo.subprocess, "run")
    def test_git_tag_enumeration_failure_fails_closed(self, run):
        run.return_value = self.tag_result("", returncode=128)
        with self.assertRaisesRegex(ValueError, "version state is unknown"):
            validate_repo.validate_version_contract()


class PrivacyScanTests(unittest.TestCase):
    def test_clean_repository_fragment_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("portable example", encoding="utf-8")
            self.assertEqual([], privacy_scan.scan(root))

    def test_user_home_and_runtime_log_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private_home = "C:" + r"\Users\alice\private"
            (root / "sample.md").write_text(private_home, encoding="utf-8")
            (root / "hook-events.jsonl").write_text("{}\n", encoding="utf-8")
            findings = privacy_scan.scan(root)
            self.assertTrue(any("Windows user home" in item for item in findings))
            self.assertTrue(any("forbidden private/runtime filename" in item for item in findings))

    def test_env_variants_private_keys_and_key_material_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.local").write_text("SAFE=example\n", encoding="utf-8")
            (root / "id_rsa").write_text("not-a-real-key\n", encoding="utf-8")
            (root / "certificate.pem").write_text("example\n", encoding="utf-8")
            header = "-----BEGIN " + "PRIVATE KEY-----"
            (root / "sample.txt").write_text(header + "\n", encoding="utf-8")
            findings = privacy_scan.scan(root)
            self.assertGreaterEqual(
                sum("forbidden private/runtime filename" in item for item in findings),
                3,
            )
            self.assertTrue(any("private key material" in item for item in findings))

    def test_safe_env_example_name_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.example").write_text("OPTIONAL_VALUE=example\n", encoding="utf-8")
            self.assertEqual([], privacy_scan.scan(root))

    def test_safe_env_example_content_is_still_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_secret = "sk" + "-" + ("a" * 24)
            (root / ".env.example").write_text(
                f"OPENAI_API_KEY={fake_secret}\n", encoding="utf-8"
            )
            findings = privacy_scan.scan(root)
            self.assertTrue(any("OpenAI-style secret" in item for item in findings))

    @mock.patch.object(privacy_scan.subprocess, "run")
    def test_git_history_scan_fails_closed_when_git_is_unavailable(self, run):
        run.side_effect = FileNotFoundError("git")
        findings = privacy_scan.scan_git_history(ROOT)
        self.assertTrue(any("HISTORY_SCAN_UNAVAILABLE" in item for item in findings))


class MigrationInventoryTests(unittest.TestCase):
    def test_inventory_excludes_absolute_home_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "safe.py").write_text("print('portable')", encoding="utf-8")
            private_home = "C:" + r"\Users\alice\private"
            (scripts / "private.py").write_text(f"PATH = '{private_home}'", encoding="utf-8")
            candidates, excluded = migrate_legacy.inspect_plugin(root)
            self.assertEqual(["scripts\\safe.py"] if __import__("os").name == "nt" else ["scripts/safe.py"], candidates)
            self.assertTrue(any(item["path"].endswith("private.py") for item in excluded))


class KnowledgeInitializationTests(unittest.TestCase):
    def test_template_initialization_is_non_overwriting(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "knowledge"
            result = plugin_admin.initialize_knowledge(target)
            self.assertTrue(target.is_dir())
            self.assertIn("initialized", result.lower())
            marker = target / "local-marker.txt"
            marker.write_text("keep", encoding="utf-8")
            second = plugin_admin.initialize_knowledge(target)
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertIn("preserved", second.lower())

    @unittest.skipUnless(shutil.which("git"), "Git is required for recovery test")
    def test_plugin_admin_recovers_git_bootstrap_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "knowledge"
            with mock.patch.object(
                plugin_admin.opc_knowledge.subprocess,
                "run",
                side_effect=FileNotFoundError("git unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Git baseline"):
                    plugin_admin.initialize_knowledge(target)
            recovery_marker = target / ".opc-bootstrap-state.json"
            self.assertTrue(recovery_marker.is_file())

            result = plugin_admin.initialize_knowledge(target)
            head = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertIn("recovered", result.lower())
            self.assertIn(head, result)
            self.assertFalse(recovery_marker.exists())


class PluginAdminUninstallTests(unittest.TestCase):
    def args(self, *, apply: bool) -> SimpleNamespace:
        return SimpleNamespace(
            apply=apply,
            dry_run=not apply,
            knowledge_home=None,
            remove_marketplace=True,
        )

    @mock.patch.object(plugin_admin, "run_codex")
    @mock.patch.object(plugin_admin, "marketplaces")
    @mock.patch.object(plugin_admin, "installed_plugins")
    def test_uninstall_defaults_to_non_mutating_preview(
        self, installed_plugins, marketplaces, run_codex
    ):
        installed_plugins.return_value = [{"pluginId": plugin_admin.PLUGIN_ID}]
        marketplaces.return_value = [{"name": plugin_admin.MARKETPLACE}]

        self.assertEqual(0, plugin_admin.uninstall(self.args(apply=False)))
        run_codex.assert_not_called()

    @mock.patch.object(plugin_admin, "run_codex")
    @mock.patch.object(plugin_admin, "marketplaces")
    @mock.patch.object(plugin_admin, "installed_plugins")
    def test_uninstall_apply_removes_only_plugin_and_marketplace(
        self, installed_plugins, marketplaces, run_codex
    ):
        installed_plugins.return_value = [{"pluginId": plugin_admin.PLUGIN_ID}]
        marketplaces.return_value = [{"name": plugin_admin.MARKETPLACE}]

        self.assertEqual(0, plugin_admin.uninstall(self.args(apply=True)))
        self.assertEqual(
            [
                mock.call("remove", plugin_admin.PLUGIN_ID),
                mock.call("marketplace", "remove", plugin_admin.MARKETPLACE),
            ],
            run_codex.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
