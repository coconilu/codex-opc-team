"""Installed-only smoke test for the pinned mem0ai adapter.

Run this script in a fresh process after installing requirements-mem0.txt.  It
constructs the real Mem0 2.0.11 client without performing add/search network
operations and proves that history, vector storage, setup data, and telemetry
policy are bound to the OPC private data root.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_memory  # noqa: E402


def main() -> int:
    if importlib.util.find_spec("mem0") is None:
        print("MEM0_INSTALLED_SMOKE_SKIPPED: mem0 is not installed", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="opc-mem0-smoke-") as temporary:
        base = Path(temporary).resolve()
        private_data = base / "plugin-data"
        isolated_home = base / "home"
        isolated_home.mkdir()
        os.environ["HOME"] = str(isolated_home)
        os.environ["USERPROFILE"] = str(isolated_home)
        os.environ["OPENAI_API_KEY"] = "sk" + "-opc-test-not-a-real-key"

        provider = opc_memory.Mem0Provider(
            user_id="opc-installed-smoke",
            data_root=private_data,
        )
        client = provider._get_client()
        try:
            provider_root = (private_data / "mem0").resolve()
            history = Path(client.config.history_db_path).resolve()
            vector_path = Path(client.config.vector_store.config.path).resolve()
            collection = str(client.config.vector_store.config.collection_name)

            if history != provider_root / "history.db":
                raise AssertionError(f"history escaped private data root: {history}")
            if vector_path != provider_root / "qdrant":
                raise AssertionError(f"vector store escaped private data root: {vector_path}")
            if not collection.startswith("opc_"):
                raise AssertionError(f"collection is not OPC namespaced: {collection}")
            if (isolated_home / ".mem0").exists():
                raise AssertionError("Mem0 wrote to the user home instead of OPC private data")
            setup_module = sys.modules.get("mem0.memory.setup")
            if Path(str(getattr(setup_module, "mem0_dir", ""))).resolve() != provider_root:
                raise AssertionError("Mem0 import-time directory is not the OPC private root")
            telemetry_module = sys.modules.get("mem0.memory.telemetry")
            if bool(getattr(telemetry_module, "MEM0_TELEMETRY", True)):
                raise AssertionError("Mem0 telemetry must be disabled for the OPC adapter")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "tested_version": opc_memory.Mem0Provider.package_version(),
                        "history_private": True,
                        "vector_private": True,
                        "telemetry_disabled": True,
                    },
                    indent=2,
                )
            )
            return 0
        finally:
            vector_store = getattr(client, "vector_store", None)
            vector_client = getattr(vector_store, "client", None)
            vector_close = getattr(vector_client, "close", None)
            if callable(vector_close):
                vector_close()
            close = getattr(client, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(main())
