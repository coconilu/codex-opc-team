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
        self.assertIn(
            '.args(["--no-open", "--host", "127.0.0.1", "--port", "0"])',
            rust,
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


if __name__ == "__main__":
    unittest.main()
