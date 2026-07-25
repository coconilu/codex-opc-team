from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_snapshot_service as snapshot_core  # noqa: E402


STAMP = "2026-07-25T00:00:00Z"


class SnapshotCoreTests(unittest.TestCase):
    def test_core_has_no_presentation_entry_import(self):
        source = (SCRIPTS / "opc_snapshot_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertNotIn("opc_dashboard", imported)
        self.assertNotIn("opc_app", imported)
        app_source = (SCRIPTS / "opc_app.py").read_text(encoding="utf-8")
        self.assertNotIn("from opc_dashboard import", app_source)

    def test_service_and_direct_core_are_equal_for_one_explicit_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            opc = project / ".opc"
            opc.mkdir(parents=True)
            (opc / "project.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": "core-equality",
                        "name": "Core Equality",
                        "created_at": STAMP,
                        "updated_at": STAMP,
                    }
                ),
                encoding="utf-8",
            )
            knowledge = base / "knowledge"
            data = base / "data"
            knowledge.mkdir()
            data.mkdir()

            direct = snapshot_core.aggregate_snapshot(
                [project],
                knowledge_root=knowledge,
                data_root=data,
                now=lambda: STAMP,
            )
            service = snapshot_core.SnapshotService(
                project_roots_provider=lambda: [project],
                knowledge_root=knowledge,
                data_root=data,
                now_provider=lambda: STAMP,
            )

            self.assertEqual(service.snapshot(), direct)
            self.assertEqual(direct["schema_version"], snapshot_core.SCHEMA_VERSION)
            snapshot_core._assert_redacted(direct)


if __name__ == "__main__":
    unittest.main()
