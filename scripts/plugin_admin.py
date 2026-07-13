#!/usr/bin/env python3
"""Install or remove the repository marketplace without editing Codex global roles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "codex-opc-team@opc"
MARKETPLACE = "opc"
PLUGIN_SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

import opc_knowledge  # noqa: E402


def run_codex(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    command = ["codex", "plugin", *args]
    return subprocess.run(command, check=True, text=True, capture_output=capture)


def codex_json(*args: str) -> dict:
    result = run_codex(*args, "--json", capture=True)
    return json.loads(result.stdout)


def marketplaces() -> list[dict]:
    return list(codex_json("marketplace", "list").get("marketplaces", []))


def installed_plugins() -> list[dict]:
    return list(codex_json("list").get("installed", []))


def knowledge_home(value: str | None = None) -> Path:
    configured = value or os.environ.get("OPC_KNOWLEDGE_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / "opc-knowledge"


def validate_knowledge(target: Path) -> None:
    required = (
        target / "catalog.json",
        target / "company" / "knowledge-policy.md",
        target / "schemas" / "experience.schema.json",
        target / "schemas" / "run.schema.json",
    )
    missing = [str(path.relative_to(target)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"Existing knowledge directory is not a valid OPC knowledge root: {target}; "
            f"missing {', '.join(missing)}"
        )


def initialize_knowledge(target: Path) -> str:
    target = target.expanduser().resolve()
    recovery_marker = target / ".opc-bootstrap-state.json"
    if target.exists() and not recovery_marker.exists():
        validate_knowledge(target)
        return f"Knowledge preserved: {target}"
    try:
        result = opc_knowledge.init_knowledge(root=target, git_init=True)
    except opc_knowledge.OpcError as exc:
        raise RuntimeError(str(exc)) from exc
    validate_knowledge(target)
    if not result.get("git_baseline_commit"):
        raise RuntimeError(
            f"Knowledge files exist but Git provenance is not ready: {target}; "
            "resolve Git and rerun initialization"
        )
    action = "recovered" if result.get("git_recovered") else "initialized"
    return (
        f"Knowledge {action}: {target}; private Git repository initialized "
        f"with baseline {result['git_baseline_commit']}"
    )


def install(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve() if args.source else ROOT
    known = {item.get("name"): item for item in marketplaces()}
    current = known.get(MARKETPLACE, {}).get("root")
    if current and Path(current).resolve() != source:
        raise RuntimeError(
            f"Marketplace '{MARKETPLACE}' already points to {current}. "
            "Remove that marketplace explicitly before installing this source."
        )
    installed = {item.get("pluginId") for item in installed_plugins()}
    target_knowledge = knowledge_home(args.knowledge_home)
    plan = {
        "dry_run": not args.apply,
        "marketplace_source": str(source),
        "add_marketplace": MARKETPLACE not in known,
        "plugin": PLUGIN_ID,
        "plugin_action": (
            "reinstall"
            if PLUGIN_ID in installed and args.force_reinstall
            else "install" if PLUGIN_ID not in installed else "keep"
        ),
        "knowledge_home": str(target_knowledge),
        "knowledge_action": (
            "skip"
            if args.skip_knowledge_init
            else "validate-existing" if target_knowledge.exists() else "initialize-private-git"
        ),
        "global_codex_config_action": "none",
    }
    print(json.dumps(plan, indent=2))
    if not args.apply:
        print("Dry run only. Re-run with --apply after reviewing this plan.")
        return 0

    if MARKETPLACE not in known:
        run_codex("marketplace", "add", str(source))
        print(f"Marketplace added: {MARKETPLACE} <- {source}")
    else:
        print(f"Marketplace already configured: {MARKETPLACE}")

    if PLUGIN_ID in installed and args.force_reinstall:
        run_codex("remove", PLUGIN_ID)
        installed.remove(PLUGIN_ID)
    if PLUGIN_ID not in installed:
        run_codex("add", PLUGIN_ID)
        print(f"Plugin installed: {PLUGIN_ID}")
    else:
        print(f"Plugin already installed: {PLUGIN_ID}")

    if not args.skip_knowledge_init:
        print(initialize_knowledge(target_knowledge))
    print("No Codex [agents] or feature settings were modified.")
    print("Start a new Codex task before testing newly installed skills and hooks.")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    installed = {item.get("pluginId") for item in installed_plugins()}
    known = {item.get("name") for item in marketplaces()}
    target_knowledge = knowledge_home(args.knowledge_home)
    plan = {
        "dry_run": not args.apply,
        "plugin": PLUGIN_ID,
        "plugin_action": "remove" if PLUGIN_ID in installed else "none",
        "marketplace": MARKETPLACE,
        "marketplace_action": (
            "remove" if args.remove_marketplace and MARKETPLACE in known else "keep"
        ),
        "knowledge_home": str(target_knowledge),
        "knowledge_action": "preserve",
        "global_codex_config_action": "none",
    }
    print(json.dumps(plan, indent=2))
    if not args.apply:
        print("Dry run only. Re-run with --apply after reviewing this plan.")
        return 0

    if PLUGIN_ID in installed:
        run_codex("remove", PLUGIN_ID)
        print(f"Plugin removed: {PLUGIN_ID}")
    else:
        print(f"Plugin is not installed: {PLUGIN_ID}")
    if args.remove_marketplace:
        if MARKETPLACE in known:
            run_codex("marketplace", "remove", MARKETPLACE)
            print(f"Marketplace removed: {MARKETPLACE}")
    print(f"Knowledge preserved: {target_knowledge}")
    print("Uninstall never deletes organizational memory. Use the guarded memory purge workflow separately.")
    return 0


def status(_: argparse.Namespace) -> int:
    known = {item.get("name"): item for item in marketplaces()}
    installed = {item.get("pluginId"): item for item in installed_plugins()}
    print(
        json.dumps(
            {
                "marketplace": known.get(MARKETPLACE),
                "plugin": installed.get(PLUGIN_ID),
                "knowledge_home": str(knowledge_home()),
            },
            indent=2,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    add = sub.add_parser("install")
    add.add_argument("--source", help="Local marketplace root; defaults to this repository")
    add.add_argument("--knowledge-home")
    add.add_argument("--skip-knowledge-init", action="store_true")
    add.add_argument("--force-reinstall", action="store_true")
    mode = add.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    add.set_defaults(handler=install)

    remove = sub.add_parser("uninstall")
    remove.add_argument("--knowledge-home")
    remove.add_argument("--remove-marketplace", action="store_true")
    remove_mode = remove.add_mutually_exclusive_group()
    remove_mode.add_argument("--apply", action="store_true")
    remove_mode.add_argument("--dry-run", action="store_true")
    remove.set_defaults(handler=uninstall)

    show = sub.add_parser("status")
    show.set_defaults(handler=status)
    return root


def main() -> int:
    args = parser().parse_args()
    if shutil.which("codex") is None:
        print("Codex CLI was not found on PATH.", file=sys.stderr)
        return 2
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"OPC_PLUGIN_ADMIN_FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
