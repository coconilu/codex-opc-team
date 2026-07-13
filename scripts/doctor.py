#!/usr/bin/env python3
"""Read-only health report for the repository, Codex plugin, and optional memory adapter."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(name: str, state: str, detail: str) -> dict[str, str]:
    return {"check": name, "state": state, "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results: list[dict[str, str]] = []

    python_state = "READY" if sys.version_info >= (3, 10) else "ERROR"
    results.append(check("Python", python_state, sys.version.split()[0]))

    validation = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_repo.py")],
        text=True,
        capture_output=True,
        check=False,
    )
    results.append(
        check(
            "Repository",
            "READY" if validation.returncode == 0 else "ERROR",
            validation.stdout.strip() or validation.stderr.strip(),
        )
    )

    codex = shutil.which("codex")
    if codex:
        listed = subprocess.run(
            [codex, "plugin", "list", "--json"], text=True, capture_output=True, check=False
        )
        plugin_state = "NOT_INSTALLED"
        if listed.returncode == 0:
            payload = json.loads(listed.stdout)
            if any(item.get("pluginId") == "codex-opc-team@opc" for item in payload.get("installed", [])):
                plugin_state = "READY"
        results.append(check("Codex plugin", plugin_state, codex))
    else:
        results.append(check("Codex plugin", "ERROR", "Codex CLI not found"))

    knowledge = Path(os.environ.get("OPC_KNOWLEDGE_HOME", Path.home() / "opc-knowledge")).expanduser()
    if not knowledge.is_dir():
        results.append(check("File/Git knowledge", "NOT_INITIALIZED", str(knowledge)))
    else:
        memory_cli = ROOT / "plugins" / "codex-opc-team" / "scripts" / "opc_memory.py"
        memory_check = subprocess.run(
            [sys.executable, str(memory_cli), "--knowledge-root", str(knowledge), "doctor"],
            text=True,
            capture_output=True,
            check=False,
        )
        git_check = subprocess.run(
            ["git", "-C", str(knowledge), "rev-parse", "--verify", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        state = "READY" if memory_check.returncode == 0 and git_check.returncode == 0 else "ERROR"
        detail = str(knowledge)
        if memory_check.returncode != 0:
            detail += "; memory schema/layout check failed"
        if git_check.returncode != 0:
            detail += "; Git baseline commit missing"
        results.append(check("File/Git knowledge", state, detail))

    if importlib.util.find_spec("mem0") is None:
        results.append(check("Semantic memory", "OPTIONAL_NOT_ENABLED", "mem0ai is not installed; File/Git remains complete"))
    else:
        try:
            version = importlib.metadata.version("mem0ai")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        results.append(
            check(
                "Semantic memory",
                "AVAILABLE_NOT_VERIFIED",
                f"mem0ai {version}; no network or write test was performed",
            )
        )

    if args.json:
        print(json.dumps({"checks": results}, indent=2, ensure_ascii=False))
    else:
        width = max(len(item["check"]) for item in results)
        for item in results:
            print(f"{item['check']:<{width}}  {item['state']:<24}  {item['detail']}")
    return 1 if any(item["state"] == "ERROR" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
