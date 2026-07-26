from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "apps" / "opc-desktop"
TAURI = DESKTOP / "src-tauri"


class DesktopContractTests(unittest.TestCase):
    def test_distribution_is_windows_current_user_with_managed_sidecar(self):
        config = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(config["bundle"]["targets"], ["nsis"])
        self.assertEqual(config["bundle"]["windows"]["nsis"]["installMode"], "currentUser")
        self.assertEqual(config["bundle"]["externalBin"], ["binaries/opc-sidecar"])
        self.assertEqual(config["app"]["windows"], [])
        self.assertNotIn("capabilities", config["app"])

    def test_frontend_has_no_tauri_runtime_api_or_capability_grant(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        dependencies = package.get("dependencies", {})
        self.assertNotIn("@tauri-apps/api", dependencies)
        self.assertFalse((TAURI / "capabilities").exists())
        rust = (TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
        self.assertNotIn("invoke_handler", rust)
        self.assertIn(".set_raw_out(true)", rust)
        self.assertRegex(
            rust,
            re.compile(
                r'\.args\(\[\s*"--no-open",\s*"--host",\s*"127\.0\.0\.1",'
                r'\s*"--port",\s*"0",?\s*\]\)'
            ),
        )

    def test_build_dependencies_are_exact_and_outputs_are_ignored(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        self.assertRegex(
            package["devDependencies"]["@tauri-apps/cli"], r"^\d+\.\d+\.\d+$"
        )
        requirements = (DESKTOP / "requirements-build.txt").read_text(
            encoding="utf-8"
        )
        for line in requirements.splitlines():
            if line:
                self.assertRegex(line, r"^[a-zA-Z0-9-]+==[0-9][A-Za-z0-9.]*$")
        cargo = (TAURI / "Cargo.toml").read_text(encoding="utf-8")
        self.assertNotRegex(cargo, re.compile(r'version\s*=\s*"[~^*]'))
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for output in [
            "apps/opc-desktop/src-tauri/target/",
            "apps/opc-desktop/src-tauri/binaries/*",
            "apps/opc-desktop/.sidecar-build/",
        ]:
            self.assertIn(output, ignored)

    def test_github_desktop_build_is_bounded_and_reproducible(self):
        workflow = (
            ROOT / ".github" / "workflows" / "desktop-build.yml"
        ).read_text(encoding="utf-8")
        for contract in [
            'node-version: "24"',
            'python-version: "3.12"',
            "npm ci --ignore-scripts",
            "npm run tauri:build",
            "permissions:\n  contents: read",
            "if-no-files-found: error",
            "Get-FileHash",
            "unsigned development artifact",
        ]:
            self.assertIn(contract, workflow)
        self.assertNotIn("release-action", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn(".opc/", workflow)

    def test_desktop_does_not_copy_python_business_modules(self):
        forbidden_names = {
            "opc_adapters.py",
            "opc_memory.py",
            "opc_snapshot_service.py",
            "opc_governance.py",
        }
        self.assertFalse(
            forbidden_names.intersection(path.name for path in DESKTOP.rglob("*.py"))
        )

    def test_minimum_desktop_width_uses_compact_two_row_layout(self):
        css = (
            ROOT
            / "plugins"
            / "codex-opc-team"
            / "assets"
            / "app"
            / "dashboard.css"
        ).read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn("overflow-x: hidden", css)


if __name__ == "__main__":
    unittest.main()
