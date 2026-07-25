#!/usr/bin/env python3
"""Install, update, roll back, inspect, or uninstall the local OPC App.

This manages only the independent App runtime. It does not install an Agent,
register an OPC host Adapter, edit global configuration, or delete App state,
project data, knowledge, Git history, or Mem0 data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAX_MANIFEST_BYTES = 64 * 1024
POINTER_SCHEMA = "opc-app.install.v1"
OWNER_SCHEMA = "opc-app.runtime-owner.v1"


class AppInstallError(RuntimeError):
    pass


def default_install_root() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "OPC" / "AppRuntime"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "opc-app"


def default_state_root() -> Path:
    configured = os.environ.get("OPC_APP_HOME")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "OPC" / "App"
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "opc-app"


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _safe_install_root(value: str | Path) -> Path:
    root = Path(os.path.abspath(Path(value).expanduser()))
    home = Path(os.path.abspath(Path.home()))
    source = Path(os.path.abspath(ROOT))
    anchor = Path(root.anchor) if root.anchor else None
    if root == home or root == source or anchor is None or root == anchor:
        raise AppInstallError("refusing broad or source install root")
    try:
        source.relative_to(root)
    except ValueError:
        pass
    else:
        raise AppInstallError("install root cannot contain the source checkout")
    try:
        root.relative_to(source)
    except ValueError:
        pass
    else:
        raise AppInstallError("install root cannot be inside the source checkout")
    if root.exists() and (_is_link(root) or not root.is_dir()):
        raise AppInstallError("install root is not a safe directory")
    return root


def _plugin_source(source: Path) -> Path:
    plugin = source / "plugins" / "codex-opc-team"
    required = (
        plugin / ".codex-plugin" / "plugin.json",
        plugin / "scripts" / "opc_app.py",
        plugin / "scripts" / "opc_dashboard.py",
        plugin / "scripts" / "opc_snapshot_service.py",
        plugin / "assets" / "app" / "index.html",
        plugin / "assets" / "app" / "dashboard.css",
        plugin / "assets" / "app" / "dashboard.js",
    )
    if any(not path.is_file() for path in required):
        raise AppInstallError("source does not contain a complete OPC App")
    if any(_is_link(path) for path in plugin.rglob("*")):
        raise AppInstallError("source contains linked content")
    return plugin


def _manifest(plugin: Path) -> dict[str, Any]:
    path = plugin / ".codex-plugin" / "plugin.json"
    raw = path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise AppInstallError("plugin manifest is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppInstallError("plugin manifest is invalid") from exc
    version = payload.get("version")
    if not isinstance(version, str) or not version or len(version) > 64:
        raise AppInstallError("plugin version is invalid")
    return payload


def _source_files(plugin: Path) -> list[Path]:
    files: list[Path] = []
    for path in plugin.rglob("*"):
        if not path.is_file() or _is_link(path):
            continue
        relative = path.relative_to(plugin)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(plugin).as_posix())


def _release_id(plugin: Path, version: str) -> str:
    digest = hashlib.sha256()
    for path in _source_files(plugin):
        relative = path.relative_to(plugin).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    safe_version = "".join(char if char.isalnum() or char in ".-" else "-" for char in version)
    return f"v{safe_version}-{digest.hexdigest()[:12]}"


def _read_pointer(root: Path) -> dict[str, Any]:
    path = root / "current.json"
    if not path.is_file() or _is_link(path):
        return {"schema_version": POINTER_SCHEMA, "current": None, "previous": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppInstallError("installed release pointer is invalid") from exc
    if (
        payload.get("schema_version") != POINTER_SCHEMA
        or set(payload) != {"schema_version", "current", "previous"}
        or any(
            value is not None and (not isinstance(value, str) or len(value) > 128)
            for value in (payload.get("current"), payload.get("previous"))
        )
    ):
        raise AppInstallError("installed release pointer is invalid")
    return payload


def _owned_root(root: Path) -> bool:
    owner = root / ".opc-app-owned.json"
    if not owner.is_file() or _is_link(owner):
        return False
    try:
        payload = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return payload == {
        "schema_version": OWNER_SCHEMA,
        "owner": "coconilu/codex-opc-team:opc-app",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise AppInstallError("failed to update installed release pointer") from exc
    finally:
        temporary.unlink(missing_ok=True)


LAUNCHER = """#!/usr/bin/env python3
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
release = pointer.get("current")
if not isinstance(release, str) or not release:
    raise SystemExit("OPC_APP_LAUNCH_FAILED: no active release")
script = root / "releases" / release / "plugin" / "scripts" / "opc_app.py"
if not script.is_file():
    raise SystemExit("OPC_APP_LAUNCH_FAILED: active release is incomplete")
sys.path.insert(0, str(script.parent))
sys.argv = [str(script), *sys.argv[1:]]
runpy.run_path(str(script), run_name="__main__")
"""


def _write_launchers(root: Path) -> None:
    target = root / "bin"
    target.mkdir(parents=True, exist_ok=True)
    (target / "opc-app.py").write_text(LAUNCHER, encoding="utf-8", newline="\n")
    (target / "opc-app.cmd").write_text(
        '@echo off\r\npython "%~dp0opc-app.py" %*\r\n',
        encoding="utf-8",
        newline="",
    )
    shell = target / "opc-app"
    shell.write_text(
        '#!/usr/bin/env sh\nexec python3 "$(dirname "$0")/opc-app.py" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    try:
        shell.chmod(0o755)
    except OSError:
        pass


def install_or_update(args: argparse.Namespace) -> int:
    source = Path(os.path.abspath(Path(args.source).expanduser())) if args.source else ROOT
    plugin = _plugin_source(source)
    manifest = _manifest(plugin)
    release = _release_id(plugin, manifest["version"])
    root = _safe_install_root(args.install_root or default_install_root())
    if root.exists() and any(root.iterdir()) and not _owned_root(root):
        raise AppInstallError("non-empty install root is not owned by OPC App")
    pointer = _read_pointer(root) if root.exists() else {
        "schema_version": POINTER_SCHEMA,
        "current": None,
        "previous": None,
    }
    action = "keep" if pointer["current"] == release else "install"
    plan = {
        "dry_run": not args.apply,
        "action": action,
        "version": manifest["version"],
        "release": release,
        "install_root": str(root),
        "launcher": str(root / "bin" / ("opc-app.cmd" if os.name == "nt" else "opc-app")),
        "app_state_root": str(default_state_root()),
        "project_and_knowledge_action": "preserve",
        "agent_adapter_action": "none",
        "global_config_action": "none",
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry run only. Re-run with --apply after reviewing this plan.")
        return 0
    root.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        root / ".opc-app-owned.json",
        {
            "schema_version": OWNER_SCHEMA,
            "owner": "coconilu/codex-opc-team:opc-app",
        },
    )
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    release_root = releases / release
    if not release_root.exists():
        stage = root / f".stage-{release}"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir()
        try:
            shutil.copytree(
                plugin,
                stage / "plugin",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            os.replace(stage, release_root)
        except OSError as exc:
            shutil.rmtree(stage, ignore_errors=True)
            raise AppInstallError("failed to install OPC App release") from exc
    if pointer["current"] != release:
        pointer = {
            "schema_version": POINTER_SCHEMA,
            "current": release,
            "previous": pointer["current"],
        }
        _atomic_json(root / "current.json", pointer)
    _write_launchers(root)
    print(f"OPC App ready: {plan['launcher']}")
    print(f"App state remains separate: {plan['app_state_root']}")
    return 0


def rollback(args: argparse.Namespace) -> int:
    root = _safe_install_root(args.install_root or default_install_root())
    if not root.exists() or not _owned_root(root):
        raise AppInstallError("install root is not owned by OPC App")
    pointer = _read_pointer(root)
    previous = pointer.get("previous")
    if not previous or not (root / "releases" / previous / "plugin").is_dir():
        raise AppInstallError("no complete previous release is available")
    plan = {
        "dry_run": not args.apply,
        "action": "rollback",
        "from_release": pointer["current"],
        "to_release": previous,
        "app_state_root": str(default_state_root()),
        "project_and_knowledge_action": "preserve",
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry run only. Re-run with --apply after reviewing this plan.")
        return 0
    _atomic_json(
        root / "current.json",
        {
            "schema_version": POINTER_SCHEMA,
            "current": previous,
            "previous": pointer["current"],
        },
    )
    print(f"OPC App rolled back to {previous}")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    root = _safe_install_root(args.install_root or default_install_root())
    if root.exists() and not _owned_root(root):
        raise AppInstallError("install root is not owned by OPC App")
    plan = {
        "dry_run": not args.apply,
        "action": "remove-runtime" if root.exists() else "none",
        "install_root": str(root),
        "app_state_root": str(default_state_root()),
        "app_state_action": "preserve",
        "project_and_knowledge_action": "preserve",
        "agent_adapter_action": "none",
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry run only. Re-run with --apply after reviewing this plan.")
        return 0
    if root.exists():
        if _is_link(root) or not _owned_root(root):
            raise AppInstallError("refusing unowned or linked install root")
        shutil.rmtree(root)
    print(f"OPC App runtime removed: {root}")
    print(f"App state preserved: {plan['app_state_root']}")
    print("Project .opc, File/Git knowledge, Git history, user config, and Mem0 data were not touched.")
    return 0


def status(args: argparse.Namespace) -> int:
    root = _safe_install_root(args.install_root or default_install_root())
    pointer = _read_pointer(root) if root.exists() else {
        "schema_version": POINTER_SCHEMA,
        "current": None,
        "previous": None,
    }
    print(
        json.dumps(
            {
                "install_root": str(root),
                "installed": bool(pointer["current"]),
                "current_release": pointer["current"],
                "previous_release": pointer["previous"],
                "launcher": str(root / "bin" / ("opc-app.cmd" if os.name == "nt" else "opc-app")),
                "app_state_root": str(default_state_root()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("install", "update"):
        command = commands.add_parser(name)
        command.add_argument("--source")
        command.add_argument("--install-root")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        command.set_defaults(handler=install_or_update)
    rollback_command = commands.add_parser("rollback")
    rollback_command.add_argument("--install-root")
    rollback_mode = rollback_command.add_mutually_exclusive_group()
    rollback_mode.add_argument("--apply", action="store_true")
    rollback_mode.add_argument("--dry-run", action="store_true")
    rollback_command.set_defaults(handler=rollback)
    uninstall_command = commands.add_parser("uninstall")
    uninstall_command.add_argument("--install-root")
    uninstall_mode = uninstall_command.add_mutually_exclusive_group()
    uninstall_mode.add_argument("--apply", action="store_true")
    uninstall_mode.add_argument("--dry-run", action="store_true")
    uninstall_command.set_defaults(handler=uninstall)
    status_command = commands.add_parser("status")
    status_command.add_argument("--install-root")
    status_command.set_defaults(handler=status)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (AppInstallError, OSError, ValueError) as exc:
        print(f"OPC_APP_ADMIN_FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
