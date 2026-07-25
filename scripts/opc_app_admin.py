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
import re
import secrets
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
RELEASE_MANIFEST_SCHEMA = "opc-app.release-manifest.v1"
LAUNCHER_MANIFEST_SCHEMA = "opc-app.launcher-manifest.v1"
RELEASE_ID = re.compile(r"^v[A-Za-z0-9.-]{1,64}-[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_RELEASE_FILES = 4096
LAUNCHER_NAMES = ("opc-app.py", "opc-app.cmd", "opc-app")


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
        expanded = Path(configured).expanduser()
        if not expanded.is_absolute():
            raise AppInstallError("OPC_APP_HOME must be an absolute path")
        return Path(os.path.abspath(expanded))
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


def _assert_runtime_boundary(root: Path, candidate: Path) -> None:
    lexical_root = Path(os.path.abspath(root))
    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError as exc:
        raise AppInstallError("runtime artifact escapes the install root") from exc
    if not relative.parts:
        raise AppInstallError("runtime artifact cannot replace the install root")

    canonical_root = lexical_root.resolve(strict=False)
    canonical_candidate = lexical_candidate.resolve(strict=False)
    try:
        canonical_candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise AppInstallError("runtime artifact resolves outside the install root") from exc

    current = lexical_root
    if current.exists() and _is_link(current):
        raise AppInstallError("runtime artifact has a linked ancestor")
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_link(current):
            raise AppInstallError("runtime artifact has a linked ancestor")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if os.name == "nt":
            return
        raise AppInstallError("failed to open runtime directory for synchronization") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if os.name != "nt":
            raise AppInstallError("failed to synchronize runtime directory") from exc
    finally:
        os.close(descriptor)


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


def _release_manifest(plugin: Path, version: str, release: str) -> dict[str, Any]:
    files = []
    for path in _source_files(plugin):
        payload = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(plugin).as_posix(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {
        "schema_version": RELEASE_MANIFEST_SCHEMA,
        "release": release,
        "version": version,
        "files": files,
    }


def _release_tree_files(plugin: Path) -> list[Path]:
    files: list[Path] = []
    try:
        entries = list(plugin.rglob("*"))
    except OSError as exc:
        raise AppInstallError("installed release tree is unreadable") from exc
    for path in entries:
        if _is_link(path):
            raise AppInstallError("installed release contains linked content")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise AppInstallError("installed release tree is unreadable") from exc
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AppInstallError("installed release contains unsafe content")
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(plugin).as_posix())


def _validate_release(
    release_root: Path,
    *,
    expected_release: str | None = None,
) -> dict[str, Any]:
    manifest_path = release_root / "release-manifest.json"
    plugin = release_root / "plugin"
    if _is_link(release_root) or _is_link(manifest_path) or _is_link(plugin):
        raise AppInstallError("installed release contains linked content")
    try:
        manifest_metadata = manifest_path.lstat()
    except OSError as exc:
        raise AppInstallError("installed release manifest is missing") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_nlink != 1
        or manifest_metadata.st_size <= 0
        or manifest_metadata.st_size > MAX_MANIFEST_BYTES * 8
    ):
        raise AppInstallError("installed release manifest is unsafe")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppInstallError("installed release manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "release", "version", "files"}
        or payload.get("schema_version") != RELEASE_MANIFEST_SCHEMA
        or not isinstance(payload.get("release"), str)
        or RELEASE_ID.fullmatch(payload["release"]) is None
        or not isinstance(payload.get("version"), str)
        or not payload["version"]
        or len(payload["version"]) > 64
        or not isinstance(payload.get("files"), list)
        or not payload["files"]
        or len(payload["files"]) > MAX_RELEASE_FILES
    ):
        raise AppInstallError("installed release manifest is invalid")
    if expected_release is not None and payload["release"] != expected_release:
        raise AppInstallError("installed release identity mismatch")

    expected_files: dict[str, tuple[int, str]] = {}
    for item in payload["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or len(item["path"]) > 240
            or "\\" in item["path"]
            or item["path"].startswith("/")
            or any(part in {"", ".", ".."} for part in item["path"].split("/"))
            or not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or HEX64.fullmatch(item["sha256"]) is None
            or item["path"] in expected_files
        ):
            raise AppInstallError("installed release manifest is invalid")
        expected_files[item["path"]] = (item["size"], item["sha256"])

    actual_files = _release_tree_files(plugin)
    actual_names = [path.relative_to(plugin).as_posix() for path in actual_files]
    if set(actual_names) != set(expected_files):
        raise AppInstallError("installed release file inventory mismatch")
    for path, name in zip(actual_files, actual_names):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AppInstallError("installed release file is unreadable") from exc
        size, digest = expected_files[name]
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise AppInstallError("installed release content digest mismatch")
    calculated = _release_id(plugin, payload["version"])
    if calculated != payload["release"]:
        raise AppInstallError("installed release content identity mismatch")
    return payload


def _read_pointer(root: Path) -> dict[str, Any]:
    path = root / "current.json"
    if not path.is_file() or _is_link(path):
        return {"schema_version": POINTER_SCHEMA, "current": None, "previous": None}
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > MAX_MANIFEST_BYTES
        ):
            raise AppInstallError("installed release pointer is unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppInstallError("installed release pointer is invalid") from exc
    if (
        payload.get("schema_version") != POINTER_SCHEMA
        or set(payload) != {"schema_version", "current", "previous"}
        or any(
            value is not None
            and (
                not isinstance(value, str)
                or RELEASE_ID.fullmatch(value) is None
            )
            for value in (payload.get("current"), payload.get("previous"))
        )
    ):
        raise AppInstallError("installed release pointer is invalid")
    return payload


def _validate_pointer_releases(
    root: Path,
    pointer: dict[str, Any],
    *,
    include_previous: bool = True,
) -> None:
    names = [pointer.get("current")]
    if include_previous:
        names.append(pointer.get("previous"))
    for release in names:
        if release is not None:
            _validate_release(
                root / "releases" / release,
                expected_release=release,
            )


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
        raise AppInstallError("failed to atomically update OPC App metadata") from exc
    finally:
        temporary.unlink(missing_ok=True)


LAUNCHER = """#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import sys
from pathlib import Path

def linked(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse and attributes & reparse)

try:
    launcher_path = Path(__file__).absolute()
    launcher_root = launcher_path.parent
    launcher_manifest_path = launcher_root / "launcher-manifest.json"
    if linked(launcher_path) or linked(launcher_root) or linked(launcher_manifest_path):
        raise ValueError
    launcher_manifest_metadata = launcher_manifest_path.lstat()
    if (
        not stat.S_ISREG(launcher_manifest_metadata.st_mode)
        or launcher_manifest_metadata.st_nlink != 1
    ):
        raise ValueError
    launcher_manifest = json.loads(
        launcher_manifest_path.read_text(encoding="utf-8")
    )
    if (
        set(launcher_manifest) != {"schema_version", "files"}
        or launcher_manifest.get("schema_version") != "opc-app.launcher-manifest.v1"
        or not isinstance(launcher_manifest.get("files"), list)
        or len(launcher_manifest["files"]) != 3
    ):
        raise ValueError
    expected_launchers = {}
    for item in launcher_manifest["files"]:
        name = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            set(item) != {"path", "size", "sha256"}
            or name not in {"opc-app.py", "opc-app.cmd", "opc-app"}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or name in expected_launchers
        ):
            raise ValueError
        expected_launchers[name] = (size, digest)
    actual_names = {path.name for path in launcher_root.iterdir()}
    if actual_names != {*expected_launchers, "launcher-manifest.json"}:
        raise ValueError
    for name, expected in expected_launchers.items():
        path = launcher_root / name
        metadata = path.lstat()
        if (
            linked(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ValueError
        raw = path.read_bytes()
        if (len(raw), hashlib.sha256(raw).hexdigest()) != expected:
            raise ValueError

    root = launcher_root.parent
    pointer_path = root / "current.json"
    pointer_metadata = pointer_path.lstat()
    if linked(pointer_path) or not stat.S_ISREG(pointer_metadata.st_mode) or pointer_metadata.st_nlink != 1:
        raise ValueError
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    release = pointer.get("current")
    if not isinstance(release, str) or not release or "/" in release or "\\\\" in release or ".." in release:
        raise ValueError
    release_root = root / "releases" / release
    plugin = release_root / "plugin"
    manifest_path = release_root / "release-manifest.json"
    manifest_metadata = manifest_path.lstat()
    if (
        linked(release_root)
        or linked(plugin)
        or linked(manifest_path)
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_nlink != 1
    ):
        raise ValueError
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        set(manifest) != {"schema_version", "release", "version", "files"}
        or manifest.get("schema_version") != "opc-app.release-manifest.v1"
        or manifest.get("release") != release
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise ValueError
    expected = {}
    for item in manifest["files"]:
        name = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            set(item) != {"path", "size", "sha256"}
            or not isinstance(name, str)
            or not name
            or "\\\\" in name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or name in expected
        ):
            raise ValueError
        expected[name] = (size, digest)
    actual = {}
    raw_files = {}
    for path in plugin.rglob("*"):
        metadata = path.lstat()
        if linked(path):
            raise ValueError
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError
        name = path.relative_to(plugin).as_posix()
        raw = path.read_bytes()
        actual[name] = (len(raw), hashlib.sha256(raw).hexdigest())
        raw_files[name] = raw
    if actual != expected:
        raise ValueError
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError
    identity = hashlib.sha256()
    for name in sorted(raw_files):
        encoded_name = name.encode("utf-8")
        raw = raw_files[name]
        identity.update(len(encoded_name).to_bytes(4, "big"))
        identity.update(encoded_name)
        identity.update(len(raw).to_bytes(8, "big"))
        identity.update(raw)
    safe_version = "".join(char if char.isalnum() or char in ".-" else "-" for char in version)
    if f"v{safe_version}-{identity.hexdigest()[:12]}" != release:
        raise ValueError
except Exception:
    raise SystemExit("OPC_APP_LAUNCH_FAILED: launcher or active release failed integrity validation")

script = plugin / "scripts" / "opc_app.py"
sys.dont_write_bytecode = True
sys.path.insert(0, str(script.parent))
sys.argv = [str(script), *sys.argv[1:]]
runpy.run_path(str(script), run_name="__main__")
"""


CMD_LAUNCHER = b'@echo off\r\npython "%~dp0opc-app.py" %*\r\n'
SHELL_LAUNCHER = b'#!/usr/bin/env sh\nexec python3 "$(dirname "$0")/opc-app.py" "$@"\n'


def _launcher_payloads() -> dict[str, bytes]:
    return {
        "opc-app.py": LAUNCHER.encode("utf-8"),
        "opc-app.cmd": CMD_LAUNCHER,
        "opc-app": SHELL_LAUNCHER,
    }


def _launcher_manifest(payloads: dict[str, bytes]) -> dict[str, Any]:
    return {
        "schema_version": LAUNCHER_MANIFEST_SCHEMA,
        "files": [
            {
                "path": name,
                "size": len(payloads[name]),
                "sha256": hashlib.sha256(payloads[name]).hexdigest(),
            }
            for name in LAUNCHER_NAMES
        ],
    }


def _launcher_manifest_bytes(payloads: dict[str, bytes]) -> bytes:
    return (
        json.dumps(
            _launcher_manifest(payloads),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_launcher_file(path: Path, payload: bytes, *, executable: bool = False) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise AppInstallError("failed to write staged launcher artifact") from exc
    if executable:
        try:
            path.chmod(0o755)
        except OSError:
            pass


def _validate_launcher_container(root: Path, target: Path) -> set[str]:
    _assert_runtime_boundary(root, target)
    if _is_link(target):
        raise AppInstallError("launcher directory contains linked content")
    if not target.exists():
        return set()
    try:
        metadata = target.lstat()
        entries = list(target.iterdir())
    except OSError as exc:
        raise AppInstallError("launcher directory is unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise AppInstallError("launcher path is not a safe directory")
    names: set[str] = set()
    for path in entries:
        _assert_runtime_boundary(root, path)
        if _is_link(path):
            raise AppInstallError("launcher directory contains linked content")
        try:
            entry_metadata = path.lstat()
        except OSError as exc:
            raise AppInstallError("launcher artifact is unreadable") from exc
        if not stat.S_ISREG(entry_metadata.st_mode) or entry_metadata.st_nlink != 1:
            raise AppInstallError("launcher directory contains unsafe content")
        names.add(path.name)
    allowed = set(LAUNCHER_NAMES) | {"launcher-manifest.json"}
    if not names.issubset(allowed):
        raise AppInstallError("launcher directory contains unexpected content")
    return names


def _validate_launcher_set(root: Path, target: Path | None = None) -> dict[str, Any]:
    target = target or root / "bin"
    names = _validate_launcher_container(root, target)
    if names != set(LAUNCHER_NAMES) | {"launcher-manifest.json"}:
        raise AppInstallError("launcher artifact set is incomplete")
    manifest_path = target / "launcher-manifest.json"
    try:
        metadata = manifest_path.lstat()
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise AppInstallError("launcher manifest is unreadable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not raw_manifest
        or len(raw_manifest) > MAX_MANIFEST_BYTES
    ):
        raise AppInstallError("launcher manifest is unsafe")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AppInstallError("launcher manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or manifest.get("schema_version") != LAUNCHER_MANIFEST_SCHEMA
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) != len(LAUNCHER_NAMES)
    ):
        raise AppInstallError("launcher manifest is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or item.get("path") not in LAUNCHER_NAMES
            or item["path"] in expected
            or not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or HEX64.fullmatch(item["sha256"]) is None
        ):
            raise AppInstallError("launcher manifest is invalid")
        expected[item["path"]] = (item["size"], item["sha256"])
    if set(expected) != set(LAUNCHER_NAMES):
        raise AppInstallError("launcher manifest is invalid")
    for name in LAUNCHER_NAMES:
        path = target / name
        _assert_runtime_boundary(root, path)
        try:
            metadata = path.lstat()
            raw = path.read_bytes()
        except OSError as exc:
            raise AppInstallError("launcher artifact is unreadable") from exc
        if (
            _is_link(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (len(raw), hashlib.sha256(raw).hexdigest()) != expected[name]
        ):
            raise AppInstallError("launcher artifact failed integrity validation")
    return manifest


def _write_launchers(root: Path) -> None:
    target = root / "bin"
    stage = root / f".launcher-stage-{secrets.token_hex(6)}"
    backup: Path | None = None
    failed: Path | None = None
    activated = False
    _assert_runtime_boundary(root, target)
    existing_names = _validate_launcher_container(root, target)
    if existing_names and existing_names not in (
        set(LAUNCHER_NAMES),
        set(LAUNCHER_NAMES) | {"launcher-manifest.json"},
    ):
        raise AppInstallError("existing launcher artifact set is incomplete")

    payloads = _launcher_payloads()
    try:
        _assert_runtime_boundary(root, stage)
        stage.mkdir()
        for name in LAUNCHER_NAMES:
            _write_launcher_file(
                stage / name,
                payloads[name],
                executable=name == "opc-app",
            )
        _write_launcher_file(
            stage / "launcher-manifest.json",
            _launcher_manifest_bytes(payloads),
        )
        _fsync_directory(stage)
        _validate_launcher_set(root, stage)

        if target.exists():
            backup = root / f".launcher-backup-{secrets.token_hex(6)}"
            _assert_runtime_boundary(root, backup)
            os.replace(target, backup)
            _fsync_directory(root)
        try:
            os.replace(stage, target)
            activated = True
            _fsync_directory(root)
        except OSError:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
                backup = None
                _fsync_directory(root)
            raise
        _validate_launcher_set(root, target)
        if backup is not None and backup.exists():
            # The verified active set is now the commit point. Backup cleanup
            # is best-effort and must never roll back into a partially removed
            # old directory.
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
    except (OSError, AppInstallError) as exc:
        if activated and target.exists():
            failed = root / f".launcher-failed-{secrets.token_hex(6)}"
            try:
                _assert_runtime_boundary(root, failed)
                os.replace(target, failed)
                activated = False
                _fsync_directory(root)
            except (OSError, AppInstallError):
                failed = None
        if backup is not None and backup.exists() and not target.exists():
            try:
                os.replace(backup, target)
                backup = None
                _fsync_directory(root)
            except (OSError, AppInstallError):
                pass
        if failed is not None and failed.exists() and target.exists():
            shutil.rmtree(failed, ignore_errors=True)
            failed = None
        if isinstance(exc, AppInstallError):
            raise
        raise AppInstallError("failed to activate verified launcher artifacts") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _install_validated_release(
    *,
    root: Path,
    plugin: Path,
    version: str,
    release: str,
) -> None:
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    release_root = releases / release
    stage = root / f".stage-{release}-{secrets.token_hex(6)}"
    quarantine: Path | None = None
    replacement_installed = False
    try:
        stage.mkdir()
        shutil.copytree(
            plugin,
            stage / "plugin",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        _atomic_json(
            stage / "release-manifest.json",
            _release_manifest(stage / "plugin", version, release),
        )
        _validate_release(stage, expected_release=release)

        if release_root.exists():
            quarantine = root / f".corrupt-{release}-{secrets.token_hex(6)}"
            os.replace(release_root, quarantine)
        try:
            os.replace(stage, release_root)
            replacement_installed = True
        except OSError:
            if quarantine is not None and quarantine.exists() and not release_root.exists():
                os.replace(quarantine, release_root)
                quarantine = None
            raise
        _validate_release(release_root, expected_release=release)
        if quarantine is not None:
            shutil.rmtree(quarantine)
            quarantine = None
    except (OSError, AppInstallError) as exc:
        if replacement_installed and release_root.exists():
            shutil.rmtree(release_root, ignore_errors=True)
            replacement_installed = False
        if quarantine is not None and quarantine.exists() and not release_root.exists():
            try:
                os.replace(quarantine, release_root)
                quarantine = None
            except OSError:
                pass
        if isinstance(exc, AppInstallError):
            raise
        raise AppInstallError("failed to install a verified OPC App release") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


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
    release_root = root / "releases" / release
    target_valid = False
    if release_root.exists():
        try:
            _validate_release(release_root, expected_release=release)
            target_valid = True
        except AppInstallError:
            target_valid = False
    for referenced in (pointer.get("current"), pointer.get("previous")):
        if referenced is not None and referenced != release:
            _validate_release(
                root / "releases" / referenced,
                expected_release=referenced,
            )
    if pointer["current"] == release and target_valid:
        action = "keep"
    elif release_root.exists() and not target_valid:
        action = "repair"
    else:
        action = "install"
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
    if not target_valid:
        _install_validated_release(
            root=root,
            plugin=plugin,
            version=manifest["version"],
            release=release,
        )
    _validate_release(release_root, expected_release=release)
    _write_launchers(root)
    if pointer["current"] != release:
        pointer = {
            "schema_version": POINTER_SCHEMA,
            "current": release,
            "previous": pointer["current"],
        }
        _atomic_json(root / "current.json", pointer)
    print(f"OPC App ready: {plan['launcher']}")
    print(f"App state remains separate: {plan['app_state_root']}")
    return 0


def rollback(args: argparse.Namespace) -> int:
    root = _safe_install_root(args.install_root or default_install_root())
    if not root.exists() or not _owned_root(root):
        raise AppInstallError("install root is not owned by OPC App")
    pointer = _read_pointer(root)
    _validate_pointer_releases(root, pointer)
    _validate_launcher_set(root)
    previous = pointer.get("previous")
    if not previous:
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
    _validate_pointer_releases(root, pointer)
    _validate_launcher_set(root)
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
    if root.exists() and not _owned_root(root):
        raise AppInstallError("install root is not owned by OPC App")
    pointer = _read_pointer(root) if root.exists() else {
        "schema_version": POINTER_SCHEMA,
        "current": None,
        "previous": None,
    }
    if pointer["current"] is not None:
        _validate_pointer_releases(root, pointer)
        _validate_launcher_set(root)
    print(
        json.dumps(
            {
                "install_root": str(root),
                "installed": bool(pointer["current"]),
                "current_release": pointer["current"],
                "previous_release": pointer["previous"],
                "integrity": "verified" if pointer["current"] else "not-installed",
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
