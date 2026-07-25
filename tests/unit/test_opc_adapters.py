from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
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
        self.discovery_nonce = {host: "initial" for host in opc_adapters.HOST_IDS}
        self.fail_next_claude_step: str | None = None
        self.partial_claude_failure = False
        self.claude_marketplaces = {"unrelated-marketplace"}
        self.claude_plugin_data = {"sentinel": "preserved"}
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
                1 if host in self.invalid_config else 0,
                self.discovery_nonce[host],
            )
        if host == "codex" and values[-3:] == ["plugin", "list", "--json"]:
            if host in self.invalid_discovery:
                return opc_adapters.CommandResult(0, "{broken")
            installed = (
                [{"pluginId": "codex-opc-team@opc"}]
                if self.installed["codex"] and host not in self.fail_verify
                else []
            )
            return opc_adapters.CommandResult(
                0,
                json.dumps(
                    {
                        "installed": installed,
                        "discoveryNonce": self.discovery_nonce[host],
                    }
                ),
            )
        if host == "claude" and "plugin" in values and "list" in values:
            if host in self.invalid_discovery:
                return opc_adapters.CommandResult(0, "{broken")
            plugins = (
                [{"id": "codex-opc-team@opc"}]
                if self.installed["claude"] and host not in self.fail_verify
                else []
            )
            return opc_adapters.CommandResult(
                0,
                json.dumps(
                    {
                        "plugins": plugins,
                        "discoveryNonce": self.discovery_nonce[host],
                    }
                ),
            )
        if host == "claude" and "plugin" in values:
            step = None
            if values[1:4] == ["plugin", "marketplace", "add"]:
                step = "marketplace_add"
            elif values[1:4] == ["plugin", "marketplace", "update"]:
                step = "marketplace_update"
            elif values[1:3] == ["plugin", "install"]:
                step = "plugin_install"
            elif values[1:3] == ["plugin", "update"]:
                step = "plugin_update"
            elif values[1:3] == ["plugin", "uninstall"]:
                step = "plugin_uninstall"
            if step == self.fail_next_claude_step:
                self.fail_next_claude_step = None
                if self.partial_claude_failure and step == "plugin_install":
                    self.installed["claude"] = True
                return opc_adapters.CommandResult(1, "", f"injected {step} failure")
            if ("install" in values or "update" in values) and host in self.fail_apply:
                return opc_adapters.CommandResult(1, "", "injected apply failure")
            if step == "marketplace_add":
                self.claude_marketplaces.add("opc")
            if step == "plugin_uninstall":
                self.installed["claude"] = False
            elif step in {"plugin_install", "plugin_update"}:
                self.installed["claude"] = True
            return opc_adapters.CommandResult(0)
        if host == "kimi" and "--prompt" in values:
            if not self.kimi_verify:
                return opc_adapters.CommandResult(1, "", "No model configured")
            if host in self.fail_verify:
                return opc_adapters.CommandResult(0, "")
            skills_root = Path(values[values.index("--skills-dir") + 1])
            skills = [
                path.name
                for path in skills_root.iterdir()
                if path.is_dir()
            ]
            return opc_adapters.CommandResult(
                0,
                json.dumps(
                    {
                        "role": "assistant",
                        "content": f"OPC_SKILLS_JSON {json.dumps(sorted(skills))}",
                    }
                ),
            )
        return opc_adapters.CommandResult(0)


class AdapterTests(unittest.TestCase):
    def manager(
        self,
        base: Path,
        fake: FakeHosts,
        *,
        source_root: Path = ROOT,
        **manager_options,
    ) -> opc_adapters.AdapterManager:
        homes = {}
        executables = {}
        for host in opc_adapters.HOST_IDS:
            home = base / f"{host}-home"
            home.mkdir(exist_ok=True)
            homes[host] = {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "KIMI_CODE_HOME": str(home / "kimi-state"),
            }
            executables[host] = str(base / f"{host}.fake")
        return opc_adapters.AdapterManager(
            source_root=source_root,
            app_state_root=base / "app-state",
            runner=fake,
            environments=homes,
            executables=executables,
            **manager_options,
        )

    @staticmethod
    def copied_source(base: Path) -> Path:
        source = base / "source"
        plugin = source / "plugins" / "codex-opc-team"
        plugin.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "plugins" / "codex-opc-team", plugin)
        scripts = source / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts" / "plugin_admin.py", scripts / "plugin_admin.py")
        return source

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

    def test_plans_expire_are_one_time_and_do_not_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            now = [100.0]
            manager = self.manager(
                base,
                fake,
                clock=lambda: now[0],
                plan_ttl_seconds=5,
            )
            expired = manager.create_plan("codex", "install")
            now[0] = 106.0
            with self.assertRaisesRegex(opc_adapters.AdapterError, "PLAN_EXPIRED"):
                manager.apply(
                    plan_id=expired["plan_id"],
                    confirmation_token=expired["confirmation_token"],
                )
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "PLAN_NOT_FOUND_OR_USED"
            ):
                manager.apply(
                    plan_id=expired["plan_id"],
                    confirmation_token=expired["confirmation_token"],
                )

            restart_plan = manager.create_plan("claude", "install")
            restarted = self.manager(base, fake)
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "PLAN_NOT_FOUND_OR_USED"
            ):
                restarted.apply(
                    plan_id=restart_plan["plan_id"],
                    confirmation_token=restart_plan["confirmation_token"],
                )
            self.assertFalse((base / "app-state").exists())

    def test_every_host_rejects_host_version_and_discovery_drift_before_write(self):
        for host in opc_adapters.HOST_IDS:
            with self.subTest(host=f"{host}-version"), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts()
                manager = self.manager(base, fake)
                plan = manager.create_plan(host, "install")
                fake.versions[host] = {
                    "codex": "codex-cli 0.144.2",
                    "claude": "2.1.213 (Claude Code)",
                    "kimi": "0.29.2",
                }[host]
                with self.assertRaisesRegex(
                    opc_adapters.AdapterError, "PLAN_STATE_CHANGED"
                ):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                self.assertFalse((base / "app-state").exists())

            with self.subTest(host=f"{host}-discovery"), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts()
                manager = self.manager(base, fake)
                plan = manager.create_plan(host, "install")
                fake.discovery_nonce[host] = "changed"
                with self.assertRaisesRegex(
                    opc_adapters.AdapterError, "PLAN_STATE_CHANGED"
                ):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                self.assertFalse((base / "app-state").exists())

    def test_every_host_rejects_manifest_source_or_target_drift_before_write(self):
        for host in opc_adapters.HOST_IDS:
            with self.subTest(host=f"{host}-manifest"), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts()
                manager = self.manager(base, fake)
                plan = manager.create_plan(host, "install")
                manifest_path = manager.store.manifest_path(host)
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                opc_adapters._atomic_json(
                    manifest_path,
                    {
                        "schema_version": opc_adapters.OWNERSHIP_SCHEMA,
                        "host_id": host,
                        "adapter_version": opc_adapters.ADAPTER_VERSION,
                        "source_version": "0.0.0",
                        "source_ref": "release/0.0.0",
                        "content_hash": "0" * 64,
                        "managed_targets": [],
                        "backup_refs": [],
                    },
                )
                with self.assertRaisesRegex(
                    opc_adapters.AdapterError, "PLAN_STATE_CHANGED"
                ):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                self.assertFalse(fake.installed.get(host, False))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            plan = manager.create_plan("kimi", "install")
            target = manager.adapters["kimi"]._target(
                manager.adapters["kimi"].skill_names[0]
            )
            target.mkdir(parents=True)
            (target / "user-sentinel").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "PLAN_STATE_CHANGED"
            ):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )
            self.assertEqual((target / "user-sentinel").read_text(), "keep")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            adapter = manager.adapters["claude"]
            plan = manager.create_plan("claude", "install")
            original_hash = type(adapter).content_hash
            with mock.patch.object(
                type(adapter),
                "content_hash",
                new_callable=mock.PropertyMock,
                return_value="f" * 64,
            ):
                with self.assertRaisesRegex(
                    opc_adapters.AdapterError, "PLAN_STATE_CHANGED"
                ):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
            self.assertIsNotNone(original_hash)
            self.assertFalse(fake.installed["claude"])

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

    def test_kimi_update_rejects_new_user_owned_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = self.copied_source(base)
            manager = self.manager(base, FakeHosts(), source_root=source)
            self.apply(manager, "kimi", "install")
            plugin_skills = source / "plugins" / "codex-opc-team" / "skills"
            shutil.copytree(plugin_skills / "opc-manager", plugin_skills / "user-skill")
            target = manager.adapters["kimi"]._target("user-skill")
            target.mkdir()
            sentinel = target / "user-sentinel"
            sentinel.write_text("preserve", encoding="utf-8")

            plan = manager.create_plan("kimi", "update")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError,
                "UNKNOWN_TARGET_CONFLICT",
            ):
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertNotIn(
                "user-skill",
                {
                    item["name"]
                    for item in manager.store.read("kimi")["managed_targets"]
                },
            )

    def test_kimi_update_removes_deleted_owned_skill_and_rollback_restores_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = self.copied_source(base)
            manager = self.manager(base, FakeHosts(), source_root=source)
            self.apply(manager, "kimi", "install")
            manifest = manager.store.read("kimi")
            removed_name = manifest["managed_targets"][0]["name"]
            removed_target = manager.adapters["kimi"]._target(removed_name)
            removed_hash = opc_adapters.tree_digest(removed_target)
            shutil.rmtree(
                source
                / "plugins"
                / "codex-opc-team"
                / "skills"
                / removed_name
            )

            plan = manager.create_plan("kimi", "update")
            self.assertIn(
                {"action": "remove", "target": f"kimi:skill/{removed_name}"},
                plan["changes"],
            )
            updated = manager.apply(
                plan_id=plan["plan_id"],
                confirmation_token=plan["confirmation_token"],
            )
            self.assertFalse(removed_target.exists())
            self.assertNotIn(
                removed_name,
                {
                    item["name"]
                    for item in manager.store.read("kimi")["managed_targets"]
                },
            )

            manager.rollback("kimi", updated["rollback_id"])
            self.assertTrue(removed_target.is_dir())
            self.assertEqual(opc_adapters.tree_digest(removed_target), removed_hash)
            self.assertIn(
                removed_name,
                {
                    item["name"]
                    for item in manager.store.read("kimi")["managed_targets"]
                },
            )

    def test_kimi_rollback_rejects_post_operation_user_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = self.copied_source(base)
            manager = self.manager(base, FakeHosts(), source_root=source)
            self.apply(manager, "kimi", "install")
            changed = (
                source
                / "plugins"
                / "codex-opc-team"
                / "skills"
                / "opc-manager"
                / "SKILL.md"
            )
            changed.write_text(
                changed.read_text(encoding="utf-8") + "\nUpdate marker.\n",
                encoding="utf-8",
            )
            updated = self.apply(manager, "kimi", "update")
            target = manager.adapters["kimi"]._target("opc-manager")
            edit = target / "user-after-update.txt"
            edit.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(
                opc_adapters.AdapterError,
                "ROLLBACK_CONFLICT",
            ):
                manager.rollback("kimi", updated["rollback_id"])

            self.assertEqual(edit.read_text(encoding="utf-8"), "preserve")

    def test_codex_projection_key_includes_content_hash_and_claude_requires_exact_id(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = self.copied_source(base)
            manager = self.manager(base, FakeHosts(), source_root=source)
            codex = manager.adapters["codex"]
            first = codex._codex_source()
            skill = (
                source
                / "plugins"
                / "codex-opc-team"
                / "skills"
                / "opc-manager"
                / "SKILL.md"
            )
            skill.write_text(
                skill.read_text(encoding="utf-8") + "\nContent revision.\n",
                encoding="utf-8",
            )
            second = codex._codex_source()
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith(codex.content_hash[:16]))
            self.assertFalse(
                opc_adapters.ClaudeAdapter._is_installed(
                    [{"name": "codex-opc-team"}]
                )
            )
            self.assertTrue(
                opc_adapters.ClaudeAdapter._is_installed(
                    [{"id": "codex-opc-team@opc"}]
                )
            )

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

    def test_kimi_verification_uses_one_common_root_and_exact_skill_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            self.apply(manager, "kimi", "install")
            prompt_call = next(
                call
                for call in fake.calls
                if "kimi.fake" in call[0] and "--prompt" in call
            )
            self.assertEqual(prompt_call.count("--skills-dir"), 1)
            index = prompt_call.index("--skills-dir")
            self.assertEqual(
                Path(prompt_call[index + 1]),
                base / "kimi-home" / "kimi-state" / "skills",
            )

            fake.fail_verify.add("kimi")
            verification = manager.adapters["kimi"].verify(True)
            self.assertEqual(verification["state"], "failed")

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

    def test_claude_install_failure_steps_preserve_data_and_unrelated_marketplace(self):
        for step in ("marketplace_add", "plugin_install"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts()
                manager = self.manager(base, fake)
                fake.fail_next_claude_step = step
                fake.partial_claude_failure = step == "plugin_install"
                data_before = dict(fake.claude_plugin_data)
                plan = manager.create_plan("claude", "install")
                with self.assertRaisesRegex(opc_adapters.AdapterError, "APPLY_FAILED"):
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                self.assertIsNone(manager.store.read("claude"))
                self.assertEqual(
                    manager.adapters["claude"].marketplace_root.exists(),
                    step == "plugin_install",
                )
                self.assertFalse(fake.installed["claude"])
                self.assertEqual(fake.claude_plugin_data, data_before)
                self.assertIn("unrelated-marketplace", fake.claude_marketplaces)
                self.assertFalse(
                    any(
                        call[1:4] == ["plugin", "marketplace", "remove"]
                        for call in fake.calls
                    )
                )

    def test_claude_partial_install_cleanup_failure_is_explicit_and_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            fake.partial_claude_failure = True
            fake.fail_next_claude_step = "plugin_install"
            original_call = fake.__call__
            cleanup_failed = False

            def fail_cleanup(command, environment=None):
                nonlocal cleanup_failed
                values = [str(item) for item in command]
                if (
                    fake.installed["claude"]
                    and values[1:3] == ["plugin", "uninstall"]
                    and not cleanup_failed
                ):
                    cleanup_failed = True
                    fake.calls.append(values)
                    return opc_adapters.CommandResult(1, "", "injected cleanup failure")
                return original_call(command, environment)

            manager.adapters["claude"].runner = fail_cleanup
            plan = manager.create_plan("claude", "install")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError,
                "ROLLBACK_FAILED",
            ) as caught:
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )
            self.assertIsNotNone(caught.exception.rollback_id)
            self.assertIsNone(manager.store.read("claude"))
            self.assertTrue(manager.adapters["claude"].marketplace_root.is_dir())
            self.assertTrue(fake.installed["claude"])
            self.assertEqual(fake.claude_plugin_data["sentinel"], "preserved")
            self.assertIn("unrelated-marketplace", fake.claude_marketplaces)
            self.assertTrue(cleanup_failed)
            recovery = {
                item["host_id"]: item
                for item in manager.inventory()["hosts"]
            }["claude"]["recovery"]
            self.assertEqual(recovery["rollback_id"], caught.exception.rollback_id)
            self.assertEqual(recovery["status"], "failed")

            rolled_back = manager.rollback("claude", caught.exception.rollback_id)
            self.assertEqual(rolled_back["state"], "rolled_back")
            self.assertFalse(fake.installed["claude"])
            self.assertNotIn(
                "recovery",
                {
                    item["host_id"]: item
                    for item in manager.inventory()["hosts"]
                }["claude"],
            )

    def test_manifest_publication_failure_keeps_a_recoverable_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manager = self.manager(base, FakeHosts())
            original_write = manager.store.write
            injected = False

            def fail_once(host_id, manifest):
                nonlocal injected
                if host_id == "kimi" and not injected:
                    injected = True
                    raise OSError("injected manifest publication failure")
                return original_write(host_id, manifest)

            manager.store.write = fail_once
            plan = manager.create_plan("kimi", "install")
            with self.assertRaisesRegex(
                opc_adapters.AdapterError,
                "APPLY_FAILED",
            ) as caught:
                manager.apply(
                    plan_id=plan["plan_id"],
                    confirmation_token=plan["confirmation_token"],
                )
            self.assertTrue(injected)
            self.assertIsNotNone(caught.exception.rollback_id)
            self.assertIsNone(manager.store.read("kimi"))
            recovery = {
                item["host_id"]: item
                for item in manager.inventory()["hosts"]
            }["kimi"]["recovery"]
            self.assertEqual(recovery["rollback_id"], caught.exception.rollback_id)
            self.assertEqual(recovery["status"], "failed")

            rolled_back = manager.rollback("kimi", caught.exception.rollback_id)
            self.assertEqual(rolled_back["state"], "rolled_back")
            for name in manager.adapters["kimi"].skill_names:
                self.assertFalse(manager.adapters["kimi"]._target(name).exists())

    def test_same_host_applies_are_serialized_across_state_check_and_write(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manager = self.manager(base, FakeHosts())
            first_plan = manager.create_plan("kimi", "install")
            second_plan = manager.create_plan("kimi", "install")
            adapter = manager.adapters["kimi"]
            original = adapter.apply_operation
            entered = threading.Event()
            release = threading.Event()
            calls = 0

            def blocked_apply(operation, operation_id):
                nonlocal calls
                calls += 1
                entered.set()
                self.assertTrue(release.wait(timeout=10))
                return original(operation, operation_id)

            adapter.apply_operation = blocked_apply
            outcomes: list[str] = []

            def run(plan):
                try:
                    manager.apply(
                        plan_id=plan["plan_id"],
                        confirmation_token=plan["confirmation_token"],
                    )
                    outcomes.append("completed")
                except opc_adapters.AdapterError as exc:
                    outcomes.append(exc.code)

            first = threading.Thread(target=run, args=(first_plan,))
            second = threading.Thread(target=run, args=(second_plan,))
            first.start()
            self.assertTrue(entered.wait(timeout=10))
            second.start()
            release.set()
            first.join(timeout=20)
            second.join(timeout=20)

            self.assertEqual(sorted(outcomes), ["PLAN_STATE_CHANGED", "completed"])
            self.assertEqual(calls, 1)

    def test_claude_uninstall_rollback_install_failure_preserves_absent_prestate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = FakeHosts()
            manager = self.manager(base, fake)
            self.apply(manager, "claude", "install")
            removed = self.apply(manager, "claude", "uninstall")
            source_before = opc_adapters.tree_digest(
                manager.adapters["claude"].marketplace_root
            )
            fake.fail_next_claude_step = "plugin_install"
            with self.assertRaisesRegex(
                opc_adapters.AdapterError, "ROLLBACK_FAILED"
            ):
                manager.rollback("claude", removed["rollback_id"])
            self.assertFalse(fake.installed["claude"])
            self.assertIsNone(manager.store.read("claude"))
            self.assertEqual(
                opc_adapters.tree_digest(manager.adapters["claude"].marketplace_root),
                source_before,
            )
            self.assertEqual(fake.claude_plugin_data["sentinel"], "preserved")

    def test_claude_update_failure_steps_restore_old_working_source_and_manifest(self):
        for step in ("marketplace_update", "plugin_update"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts()
                manager = self.manager(base, fake)
                self.apply(manager, "claude", "install")
                adapter = manager.adapters["claude"]
                manifest_before = manager.store.manifest_path("claude").read_bytes()
                source_before = opc_adapters.tree_digest(adapter.marketplace_root)
                data_before = dict(fake.claude_plugin_data)
                fake.fail_next_claude_step = step
                with (
                    mock.patch.object(
                        opc_adapters.HostAdapter,
                        "source_version",
                        new_callable=mock.PropertyMock,
                        return_value="9.9.9",
                    ),
                    mock.patch.object(
                        opc_adapters.HostAdapter,
                        "content_hash",
                        new_callable=mock.PropertyMock,
                        return_value="f" * 64,
                    ),
                ):
                    plan = manager.create_plan("claude", "update")
                    with self.assertRaisesRegex(
                        opc_adapters.AdapterError, "APPLY_FAILED"
                    ):
                        manager.apply(
                            plan_id=plan["plan_id"],
                            confirmation_token=plan["confirmation_token"],
                        )
                self.assertTrue(fake.installed["claude"])
                self.assertEqual(
                    opc_adapters.tree_digest(adapter.marketplace_root),
                    source_before,
                )
                self.assertEqual(
                    manager.store.manifest_path("claude").read_bytes(),
                    manifest_before,
                )
                self.assertEqual(fake.claude_plugin_data, data_before)
                self.assertIn("unrelated-marketplace", fake.claude_marketplaces)
                self.assertFalse(
                    any(
                        call[1:4] == ["plugin", "marketplace", "remove"]
                        for call in fake.calls
                    )
                )

    def test_claude_rollback_failure_steps_restore_current_working_source(self):
        for step in ("marketplace_update", "plugin_update"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = FakeHosts()
                manager = self.manager(base, fake)
                self.apply(manager, "claude", "install")
                with (
                    mock.patch.object(
                        opc_adapters.HostAdapter,
                        "source_version",
                        new_callable=mock.PropertyMock,
                        return_value="9.9.9",
                    ),
                    mock.patch.object(
                        opc_adapters.HostAdapter,
                        "content_hash",
                        new_callable=mock.PropertyMock,
                        return_value="f" * 64,
                    ),
                ):
                    updated = self.apply(manager, "claude", "update")
                current_manifest = manager.store.manifest_path("claude").read_bytes()
                current_source = opc_adapters.tree_digest(
                    manager.adapters["claude"].marketplace_root
                )
                fake.fail_next_claude_step = step
                with self.assertRaisesRegex(
                    opc_adapters.AdapterError, "ROLLBACK_FAILED"
                ):
                    manager.rollback("claude", updated["rollback_id"])
                self.assertEqual(
                    opc_adapters.tree_digest(manager.adapters["claude"].marketplace_root),
                    current_source,
                )
                self.assertEqual(
                    manager.store.manifest_path("claude").read_bytes(),
                    current_manifest,
                )
                self.assertTrue(fake.installed["claude"])
                self.assertEqual(fake.claude_plugin_data["sentinel"], "preserved")
                self.assertIn("unrelated-marketplace", fake.claude_marketplaces)
                self.assertFalse(
                    any(
                        call[1:4] == ["plugin", "marketplace", "remove"]
                        for call in fake.calls
                    )
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
