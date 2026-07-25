from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

import opc_adapters  # noqa: E402


class FakeHosts:
    def __init__(
        self,
        *,
        codex_version: str = "codex-cli 0.144.1",
        claude_version: str = "2.1.212 (Claude Code)",
        kimi_version: str = "0.29.1",
        kimi_verify: bool = True,
        invalid_discovery: set[str] | None = None,
        invalid_config: set[str] | None = None,
        fail_apply: set[str] | None = None,
        fail_verify: set[str] | None = None,
    ):
        self.versions = {
            "codex": codex_version,
            "claude": claude_version,
            "kimi": kimi_version,
        }
        self.installed = {"codex": False, "claude": False}
        self.kimi_verify = kimi_verify
        self.invalid_discovery = invalid_discovery or set()
        self.invalid_config = invalid_config or set()
        self.fail_apply = fail_apply or set()
        self.fail_verify = fail_verify or set()
        self.calls: list[list[str]] = []

    def __call__(self, command, environment=None):
        values = [str(item) for item in command]
        self.calls.append(values)
        joined = " ".join(values)
        executable = Path(values[0]).name.lower()
        if "plugin_admin.py" in joined:
            host = "codex"
            if "install" in values and host in self.fail_apply:
                return opc_adapters.CommandResult(1, "", "injected apply failure")
            if "uninstall" in values:
                self.installed[host] = False
            elif "install" in values:
                self.installed[host] = True
            return opc_adapters.CommandResult(0)
        host = next(
            (
                item
                for item in ("codex", "claude", "kimi")
                if item in executable
            ),
            None,
        )
        if host is None:
            return opc_adapters.CommandResult(127)
        if "--version" in values:
            return opc_adapters.CommandResult(0, self.versions[host])
        if host == "kimi" and "doctor" in values:
            return opc_adapters.CommandResult(
                1 if host in self.invalid_config else 0
            )
        if host == "codex" and values[-3:] == ["plugin", "list", "--json"]:
            if host in self.invalid_discovery:
                return opc_adapters.CommandResult(0, "{broken")
            installed = (
                [{"pluginId": "codex-opc-team@opc"}]
                if self.installed["codex"] and host not in self.fail_verify
                else []
            )
            return opc_adapters.CommandResult(0, json.dumps({"installed": installed}))
        if host == "claude" and "plugin" in values and "list" in values:
            if host in self.invalid_discovery:
                return opc_adapters.CommandResult(0, "{broken")
            plugins = (
                [{"id": "codex-opc-team@opc"}]
                if self.installed["claude"] and host not in self.fail_verify
                else []
            )
            return opc_adapters.CommandResult(0, json.dumps(plugins))
        if host == "claude" and "plugin" in values:
            if ("install" in values or "update" in values) and host in self.fail_apply:
                return opc_adapters.CommandResult(1, "", "injected apply failure")
            if "uninstall" in values:
                self.installed["claude"] = False
            elif "install" in values or "update" in values:
                self.installed["claude"] = True
            return opc_adapters.CommandResult(0)
        if host == "kimi" and "--prompt" in values:
            if not self.kimi_verify:
                return opc_adapters.CommandResult(1, "", "No model configured")
            if host in self.fail_verify:
                return opc_adapters.CommandResult(0, "")
            skills = [
                path.name
                for path in (ROOT / "plugins" / "codex-opc-team" / "skills").iterdir()
                if path.is_dir()
            ]
            return opc_adapters.CommandResult(0, " ".join(skills))
        return opc_adapters.CommandResult(0)


class AdapterTests(unittest.TestCase):
    def manager(self, base: Path, fake: FakeHosts) -> opc_adapters.AdapterManager:
        homes = {}
        executables = {}
        for host in opc_adapters.HOST_IDS:
            home = base / f"{host}-home"
            home.mkdir()
            homes[host] = {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "KIMI_CODE_HOME": str(home / "kimi-state"),
            }
            executables[host] = str(base / f"{host}.fake")
        return opc_adapters.AdapterManager(
            source_root=ROOT,
            app_state_root=base / "app-state",
            runner=fake,
            environments=homes,
            executables=executables,
        )

    @staticmethod
    def apply(
        manager: opc_adapters.AdapterManager,
        host: str,
        operation: str,
    ) -> dict:
        plan = manager.create_plan(host, operation)
        return manager.apply(
            plan_id=plan["plan_id"],
            confirmation_token=plan["confirmation_token"],
        )

    def test_probe_is_read_only_and_reports_host_contract_asymmetry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts(claude_version="2.1.204 (Claude Code)")
            manager = self.manager(base, fake)
            state_root = base / "app-state"
            inventory = manager.inventory()
            self.assertFalse(state_root.exists())
            by_host = {item["host_id"]: item for item in inventory["hosts"]}
            self.assertEqual(by_host["codex"]["state"], "available")
            self.assertEqual(by_host["claude"]["state"], "blocked")
            self.assertEqual(
                by_host["claude"]["reason"],
                "HOST_VERSION_INCOMPATIBLE_UNINSTALL_SAFETY",
            )
            self.assertEqual(
                by_host["kimi"]["plugin_management"],
                "blocked_no_public_noninteractive_cli",
            )

    def test_each_host_missing_incompatible_and_invalid_contract_is_zero_write(self):
        cases = (
            (
                FakeHosts(codex_version="", claude_version="", kimi_version=""),
                {"codex": "unavailable", "claude": "unavailable", "kimi": "unavailable"},
            ),
            (
                FakeHosts(
                    codex_version="0.143.0",
                    claude_version="2.1.100",
                    kimi_version="0.28.0",
                ),
                {"codex": "blocked", "claude": "blocked", "kimi": "blocked"},
            ),
            (
                FakeHosts(
                    invalid_discovery={"codex", "claude"},
                    invalid_config={"kimi"},
                ),
                {"codex": "blocked", "claude": "blocked", "kimi": "blocked"},
            ),
        )
        for fake, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                manager = self.manager(base, fake)
                inventory = manager.inventory()
                actual = {item["host_id"]: item["state"] for item in inventory["hosts"]}
                self.assertEqual(actual, expected)
                self.assertFalse((base / "app-state").exists())

    def test_codex_and_claude_apply_and_verify_failures_leave_no_owned_install(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                manager = self.manager(base, FakeHosts(fail_apply={host}))
                plan = manager.create_plan(host, "install")
                with self.assertRaisesRegex(opc_adapters.AdapterError, "APPLY_FAILED"):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                self.assertIsNone(manager.store.read(host))

            with self.subTest(host=f"{host}-verify"), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts(fail_verify={host})
                manager = self.manager(base, fake)
                plan = manager.create_plan(host, "install")
                with self.assertRaisesRegex(
                    opc_adapters.AdapterError, "VERIFY_FAILED_ROLLED_BACK"
                ):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                self.assertIsNone(manager.store.read(host))
                self.assertFalse(fake.installed[host])

    def test_kimi_verification_failure_rolls_back_new_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manager = self.manager(base, FakeHosts(fail_verify={"kimi"}))
            plan = manager.create_plan("kimi", "install")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "VERIFY_FAILED_ROLLED_BACK"
            ):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )
            self.assertIsNone(manager.store.read("kimi"))
            self.assertEqual(
                list((base / "kimi-home" / "kimi-state" / "skills").iterdir()),
                [],
            )

    def test_preview_is_zero_write_and_confirmation_is_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            plan = manager.create_plan("kimi", "install")
            self.assertFalse((base / "app-state").exists())
            self.assertTrue(plan["confirmation_required"])
            self.assertTrue(all("target" in item for item in plan["changes"]))
            with self.assertRaisesRegex(opc_adapters.AdapterError, "CONFIRMATION_REQUIRED"):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token="wrong",
                )
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "PLAN_NOT_FOUND_OR_USED"
            ):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )

    def test_kimi_clean_idempotent_uninstall_reinstall_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            installed = self.apply(manager, "kimi", "install")
            self.assertEqual(installed["state"], "completed")
            manifest = manager.store.read("kimi")
            self.assertIsNotNone(manifest)
            for item in manifest["managed_targets"]:
                self.assertTrue(
                    (base / "kimi-home" / "kimi-state" / "skills" / item["name"]).is_dir()
                )

            no_change = self.apply(manager, "kimi", "update")
            self.assertEqual(no_change["state"], "no_change")

            removed = self.apply(manager, "kimi", "uninstall")
            self.assertEqual(removed["state"], "completed")
            self.assertIsNone(manager.store.read("kimi"))
            rolled_back = manager.rollback("kimi", removed["rollback_id"])
            self.assertEqual(rolled_back["state"], "rolled_back")
            self.assertIsNotNone(manager.store.read("kimi"))

            removed_again = self.apply(manager, "kimi", "uninstall")
            self.assertEqual(removed_again["state"], "completed")
            reinstalled = self.apply(manager, "kimi", "install")
            self.assertEqual(reinstalled["state"], "completed")

    def test_user_modified_target_fails_closed_and_preserves_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            self.apply(manager, "kimi", "install")
            manifest = manager.store.read("kimi")
            name = manifest["managed_targets"][0]["name"]
            target = base / "kimi-home" / "kimi-state" / "skills" / name
            marker = target / "user-edit.txt"
            marker.write_text("preserve", encoding="utf-8")
            plan = manager.create_plan("kimi", "uninstall")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "USER_MODIFIED_CONFLICT"
            ):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertIsNotNone(manager.store.read("kimi"))

    def test_unknown_target_symlink_and_corrupt_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            kimi = manager.adapters["kimi"]
            target = kimi._target(kimi.skill_names[0])
            target.mkdir(parents=True)
            sentinel = target / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            plan = manager.create_plan("kimi", "install")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "UNKNOWN_TARGET_CONFLICT"
            ):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

            manifest = manager.store.manifest_path("kimi")
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(opc_adapters.AdapterError, "INVALID_MANIFEST"):
                manager.create_plan("kimi", "install")

    def test_verification_required_is_not_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manager = self.manager(base, FakeHosts(kimi_verify=False))
            result = self.apply(manager, "kimi", "install")
            self.assertEqual(result["state"], "verification_required")
            self.assertEqual(result["verification"]["state"], "verification_required")

    def test_codex_reuses_plugin_admin_and_preserves_knowledge_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            result = self.apply(manager, "codex", "install")
            self.assertEqual(result["state"], "completed")
            admin_calls = [call for call in fake.calls if "plugin_admin.py" in " ".join(call)]
            self.assertEqual(len(admin_calls), 1)
            self.assertIn("--skip-knowledge-init", admin_calls[0])
            self.assertIn("--apply", admin_calls[0])
            self.assertFalse(any("OPC_KNOWLEDGE_HOME" in " ".join(call) for call in fake.calls))

    def test_claude_uses_keep_data_and_isolated_failure_does_not_touch_kimi(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            self.apply(manager, "kimi", "install")
            sentinel_manifest = manager.store.manifest_path("kimi").read_bytes()
            self.apply(manager, "claude", "install")
            self.apply(manager, "claude", "uninstall")
            uninstall = [
                call
                for call in fake.calls
                if "claude.fake" in call[0] and "uninstall" in call
            ][0]
            self.assertIn("--keep-data", uninstall)
            self.assertEqual(
                manager.store.manifest_path("kimi").read_bytes(),
                sentinel_manifest,
            )

    def test_permission_failure_restores_preexisting_kimi_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manager = self.manager(base, FakeHosts())
            self.apply(manager, "kimi", "install")
            before = {
                path.relative_to(base).as_posix(): path.read_bytes()
                for path in base.rglob("*")
                if path.is_file()
                and "app-state" not in path.parts
            }
            original_replace = os.replace
            host_skill_root = base / "kimi-home" / "kimi-state" / "skills"
            calls = 0

            def fail_second_activation(source, destination):
                nonlocal calls
                destination = Path(destination)
                if destination.parent == host_skill_root:
                    calls += 1
                    if calls == 2:
                        raise PermissionError("injected")
                return original_replace(source, destination)

            # Force a real update rather than the no-change fast path.
            manifest = manager.store.read("kimi")
            manifest["content_hash"] = "0" * 64
            manager.store.write("kimi", manifest)
            plan = manager.create_plan("kimi", "update")
            with mock.patch.object(opc_adapters.os, "replace", side_effect=fail_second_activation):
                with self.assertRaisesRegex(opc_adapters.AdapterError, "APPLY_FAILED"):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
            after = {
                path.relative_to(base).as_posix(): path.read_bytes()
                for path in base.rglob("*")
                if path.is_file()
                and "app-state" not in path.parts
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
