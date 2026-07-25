#!/usr/bin/env python3
"""Host adapter lifecycle for the local OPC App.

The module deliberately keeps host mutation outside the web UI.  A caller must
first create an immutable preview plan and then present its one-time
confirmation token back to :class:`AdapterManager.apply`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ADAPTER_API_SCHEMA = "opc-adapters.api.v1"
ADAPTER_PLAN_SCHEMA = "opc-adapters.plan.v1"
OWNERSHIP_SCHEMA = "opc-adapters.ownership.v1"
CAPABILITY_CONTRACT_SCHEMA = "opc-adapters.capability-matrix.v1"
ADAPTER_VERSION = "1.0.0"
DEFAULT_PLAN_TTL_SECONDS = 120.0
HOST_IDS = ("codex", "claude", "kimi")
PLUGIN_ID = "codex-opc-team"
MARKETPLACE_ID = "opc"
MAX_MANIFEST_BYTES = 512 * 1024
PORTABLE_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:@/-]{0,255}$")
VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")


class AdapterError(RuntimeError):
    """A stable, UI-safe adapter failure."""

    def __init__(self, code: str, *, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Mapping[str, str] | None], CommandResult]


def _run_command(
    command: Sequence[str],
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            env=dict(environment) if environment is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(127, "", type(exc).__name__)
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = VERSION.search(value)
    if match is None:
        return None
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse and attributes & reparse)


def _safe_tree_files(root: Path) -> list[Path]:
    if _is_link(root):
        raise AdapterError("UNSAFE_SOURCE")
    files: list[Path] = []
    for path in root.rglob("*"):
        if _is_link(path):
            raise AdapterError("UNSAFE_SOURCE")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _safe_tree_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise AdapterError("MANIFEST_TOO_LARGE")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise AdapterError("STATE_WRITE_FAILED") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise AdapterError("INVALID_MANIFEST")
        payload = json.loads(raw.decode("utf-8"))
    except AdapterError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterError("INVALID_MANIFEST") from exc
    if not isinstance(payload, dict):
        raise AdapterError("INVALID_MANIFEST")
    return payload


class OwnershipStore:
    """Adapter-owned state, isolated below an already validated App state root."""

    def __init__(self, app_state_root: Path | str):
        self.root = Path(os.path.abspath(app_state_root)) / "adapters"

    def manifest_path(self, host_id: str) -> Path:
        return self.root / host_id / "manifest.json"

    def read(self, host_id: str) -> dict[str, Any] | None:
        path = self.manifest_path(host_id)
        if not path.exists():
            return None
        if _is_link(path) or _is_link(path.parent):
            raise AdapterError("UNSAFE_ADAPTER_STATE")
        payload = _read_json(path)
        required = {
            "schema_version",
            "host_id",
            "adapter_version",
            "source_version",
            "source_ref",
            "content_hash",
            "managed_targets",
            "backup_refs",
        }
        if (
            set(payload) != required
            or payload.get("schema_version") != OWNERSHIP_SCHEMA
            or payload.get("host_id") != host_id
            or not isinstance(payload.get("managed_targets"), list)
            or not isinstance(payload.get("backup_refs"), list)
        ):
            raise AdapterError("INVALID_MANIFEST")
        return payload

    def write(self, host_id: str, payload: Mapping[str, Any]) -> None:
        _atomic_json(self.manifest_path(host_id), payload)

    def remove(self, host_id: str) -> None:
        path = self.manifest_path(host_id)
        if path.exists():
            if _is_link(path):
                raise AdapterError("UNSAFE_ADAPTER_STATE")
            path.unlink()

    def backup_root(self, host_id: str, operation_id: str) -> Path:
        if not PORTABLE_TOKEN.fullmatch(operation_id):
            raise AdapterError("INVALID_OPERATION")
        return self.root / host_id / "backups" / operation_id

    def operation_path(self, host_id: str, operation_id: str) -> Path:
        if not PORTABLE_TOKEN.fullmatch(operation_id):
            raise AdapterError("INVALID_OPERATION")
        return self.root / host_id / "operations" / f"{operation_id}.json"

    def write_operation(
        self,
        host_id: str,
        operation_id: str,
        *,
        operation: str,
        prior_manifest: dict[str, Any] | None,
    ) -> None:
        _atomic_json(
            self.operation_path(host_id, operation_id),
            {
                "schema_version": "opc-adapters.operation.v1",
                "host_id": host_id,
                "operation": operation,
                "prior_manifest": prior_manifest,
            },
        )

    def read_operation(self, host_id: str, operation_id: str) -> dict[str, Any]:
        path = self.operation_path(host_id, operation_id)
        if not path.is_file() or _is_link(path):
            raise AdapterError("ROLLBACK_POINT_UNAVAILABLE")
        payload = _read_json(path)
        if (
            payload.get("schema_version") != "opc-adapters.operation.v1"
            or payload.get("host_id") != host_id
            or payload.get("operation") not in {"install", "update", "uninstall"}
            or not (
                payload.get("prior_manifest") is None
                or isinstance(payload.get("prior_manifest"), dict)
            )
        ):
            raise AdapterError("INVALID_ROLLBACK_POINT")
        return payload


class HostAdapter:
    host_id = ""
    display_name = ""
    integration_unit = ""
    verified_contract = ""

    def __init__(
        self,
        *,
        source_root: Path,
        store: OwnershipStore,
        runner: CommandRunner = _run_command,
        executable: str | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self.source_root = Path(source_root).resolve()
        repository_plugin = self.source_root / "plugins" / "codex-opc-team"
        installed_plugin = self.source_root / "plugin"
        self.plugin_root = (
            repository_plugin if repository_plugin.is_dir() else installed_plugin
        )
        self.store = store
        self.runner = runner
        self.executable = executable
        self.environment = dict(environment) if environment is not None else None

    @property
    def source_version(self) -> str:
        payload = _read_json(self.plugin_root / ".codex-plugin" / "plugin.json")
        value = payload.get("version")
        if not isinstance(value, str) or VERSION.fullmatch(value) is None:
            raise AdapterError("INVALID_SOURCE")
        return value

    @property
    def source_ref(self) -> str:
        value = (
            (self.environment or {}).get("OPC_ADAPTER_SOURCE_REF")
            or os.environ.get("OPC_ADAPTER_SOURCE_REF", "")
        ).strip()
        return value if PORTABLE_TOKEN.fullmatch(value) else f"release/{self.source_version}"

    @property
    def content_hash(self) -> str:
        return tree_digest(self.plugin_root / "skills")

    def _command(self, *arguments: str) -> CommandResult:
        executable = self.executable or shutil.which(self.host_id)
        if not executable:
            return CommandResult(127, "", "host executable unavailable")
        return self.runner((executable, *arguments), self.environment)

    def probe(self) -> dict[str, Any]:
        raise NotImplementedError

    def target_fingerprint(self) -> dict[str, Any]:
        """Return a privacy-safe snapshot of adapter-owned target state."""
        return {"kind": self.integration_unit}

    def capture_plan_state(self) -> dict[str, Any]:
        """Capture every immutable input a plan is authorized against."""
        return {
            "capability_contract_schema": CAPABILITY_CONTRACT_SCHEMA,
            "verified_contract": self.verified_contract,
            "adapter_version": ADAPTER_VERSION,
            "source": {
                "version": self.source_version,
                "ref": self.source_ref,
                "content_hash": self.content_hash,
            },
            "probe": self.probe(),
            "ownership_manifest": self.store.read(self.host_id),
            "target": self.target_fingerprint(),
        }

    def changes(self, operation: str, manifest: dict[str, Any] | None) -> list[dict[str, str]]:
        raise NotImplementedError

    def apply_operation(self, operation: str, operation_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def verify(self, expected_installed: bool) -> dict[str, Any]:
        raise NotImplementedError

    def rollback_operation(
        self,
        operation: str,
        prior_manifest: dict[str, Any] | None,
        operation_id: str,
    ) -> dict[str, Any]:
        inverse = "uninstall" if prior_manifest is None else "install"
        return self.apply_operation(inverse, f"{operation_id}-rollback")

    def _status(self, state: str, **extra: Any) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "display_name": self.display_name,
            "state": state,
            "integration_unit": self.integration_unit,
            "verified_contract": self.verified_contract,
            **extra,
        }

    def plan(
        self,
        operation: str,
        captured_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if operation not in {"install", "update", "uninstall"}:
            raise AdapterError("UNSUPPORTED_OPERATION")
        state = dict(captured_state or self.capture_plan_state())
        probe = state["probe"]
        if probe["state"] not in {"available", "installed", "drifted"}:
            raise AdapterError("HOST_BLOCKED", detail=probe.get("reason"))
        manifest = state["ownership_manifest"]
        if operation == "uninstall" and manifest is None:
            raise AdapterError("NOT_OPC_OWNED")
        if probe.get("state") == "drifted" and manifest is None:
            raise AdapterError("OWNERSHIP_CONFLICT")
        changes = self.changes(operation, manifest)
        source = state["source"]
        no_change = bool(
            operation == "update"
            and manifest
            and manifest.get("content_hash") == source["content_hash"]
            and probe.get("state") == "installed"
        )
        return {
            "schema_version": ADAPTER_PLAN_SCHEMA,
            "host_id": self.host_id,
            "operation": operation,
            "adapter_version": ADAPTER_VERSION,
            "source_version": source["version"],
            "source_ref": source["ref"],
            "content_hash": source["content_hash"],
            "state_fingerprint": _canonical_digest(state),
            "changes": changes,
            "no_change": no_change,
            "preserves": [
                "OPC_KNOWLEDGE_HOME",
                "project .opc",
                "File/Git history",
                "manager preferences/rules/experience",
                "optional Mem0 data",
                "other host adapters",
            ],
            "rollback": "restore this operation's OPC-owned pre-state",
            "confirmation_required": True,
        }


class CodexAdapter(HostAdapter):
    host_id = "codex"
    display_name = "Codex"
    integration_unit = "Marketplace Plugin"
    verified_contract = "codex-cli >=0.144.1,<0.145.0"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        repository_admin = self.source_root / "scripts" / "plugin_admin.py"
        packaged_admin = self.plugin_root / "scripts" / "plugin_admin.py"
        self.admin = repository_admin if repository_admin.is_file() else packaged_admin

    def probe(self) -> dict[str, Any]:
        result = self._command("--version")
        version = _version_tuple(result.stdout or result.stderr)
        if result.returncode != 0 or version is None:
            return self._status("unavailable", reason="HOST_NOT_FOUND")
        if not ((0, 144, 1) <= version < (0, 145, 0)):
            return self._status(
                "blocked",
                host_version=".".join(map(str, version)),
                reason="HOST_VERSION_INCOMPATIBLE",
            )
        listed = self._command("plugin", "list", "--json")
        if listed.returncode != 0:
            return self._status("blocked", reason="HOST_DISCOVERY_FAILED")
        installed = False
        try:
            discovery = json.loads(listed.stdout)
            installed = any(
                item.get("pluginId") == f"{PLUGIN_ID}@{MARKETPLACE_ID}"
                for item in discovery.get("installed", [])
            )
        except (ValueError, AttributeError):
            return self._status("blocked", reason="HOST_DISCOVERY_INVALID")
        manifest = self.store.read(self.host_id)
        state = "installed" if installed and manifest else "available"
        if installed != bool(manifest):
            state = "drifted"
        return self._status(
            state,
            host_version=".".join(map(str, version)),
            installed_version=manifest.get("source_version") if manifest else None,
            target_version=self.source_version,
            drift=state == "drifted",
            ownership=bool(manifest),
            discovery_fingerprint=_canonical_digest(discovery),
        )

    def target_fingerprint(self) -> dict[str, Any]:
        projection = (
            self.store.root
            / self.host_id
            / "projection"
            / f"{self.source_version}-{self.content_hash[:16]}"
        )
        return {
            "projection_exists": projection.is_dir(),
            "projection_is_link": _is_link(projection),
            "projection_hash": (
                tree_digest(projection / "plugins" / PLUGIN_ID / "skills")
                if projection.is_dir() and not _is_link(projection)
                else None
            ),
        }

    def changes(self, operation: str, manifest: dict[str, Any] | None) -> list[dict[str, str]]:
        action = {
            "install": "add",
            "update": (
                "keep"
                if manifest and manifest.get("content_hash") == self.content_hash
                else "replace" if manifest else "add"
            ),
            "uninstall": "remove",
        }[operation]
        return [
            {"action": action, "target": "codex:plugin/codex-opc-team@opc"},
            {
                "action": "keep",
                "target": "codex:global-agent-roles-and-feature-flags",
            },
        ]

    def _run_admin(
        self,
        operation: str,
        *,
        source: Path | None = None,
        remove_marketplace: bool = False,
    ) -> CommandResult:
        if not self.admin.is_file():
            return CommandResult(127, "", "plugin_admin.py unavailable")
        arguments = [
            sys.executable,
            str(self.admin),
            "uninstall" if operation == "uninstall" else "install",
        ]
        if operation != "uninstall":
            source = source or self._codex_source()
            arguments.extend(
                [
                    "--source",
                    str(source),
                    "--skip-knowledge-init",
                ]
            )
            if operation == "update":
                arguments.append("--force-reinstall")
        elif remove_marketplace:
            arguments.append("--remove-marketplace")
        arguments.append("--apply")
        return self.runner(arguments, self.environment)

    def _codex_source(self) -> Path:
        root = self.store.root / self.host_id / "projection" / self.source_version
        if root.exists():
            if tree_digest(root / "plugins" / "codex-opc-team" / "skills") != self.content_hash:
                raise AdapterError("PROJECTION_CONFLICT")
            return root
        stage = root.parent / f".stage-{secrets.token_hex(6)}"
        try:
            target = stage / "plugins" / "codex-opc-team"
            target.parent.mkdir(parents=True)
            shutil.copytree(self.plugin_root, target)
            (stage / ".agents" / "plugins").mkdir(parents=True)
            _atomic_json(
                stage / ".agents" / "plugins" / "marketplace.json",
                {
                    "name": MARKETPLACE_ID,
                    "interface": {"displayName": "Codex OPC Team"},
                    "plugins": [
                        {
                            "name": PLUGIN_ID,
                            "source": {
                                "source": "local",
                                "path": "./plugins/codex-opc-team",
                            },
                            "policy": {
                                "installation": "AVAILABLE",
                                "authentication": "ON_INSTALL",
                            },
                            "category": "Productivity",
                        }
                    ],
                },
            )
            root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, root)
        except AdapterError:
            raise
        except OSError as exc:
            raise AdapterError("PROJECTION_WRITE_FAILED") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        return root

    def apply_operation(self, operation: str, operation_id: str) -> dict[str, Any]:
        prior = self.store.read(self.host_id)
        if operation == "update" and prior is not None:
            removed = self._run_admin("uninstall", remove_marketplace=True)
            if removed.returncode != 0:
                raise AdapterError("APPLY_FAILED")
            result = self._run_admin("install")
            if result.returncode != 0:
                previous = (
                    self.store.root
                    / self.host_id
                    / "projection"
                    / prior["source_version"]
                )
                if previous.is_dir():
                    self._run_admin("install", source=previous)
                raise AdapterError("APPLY_FAILED")
        else:
            result = self._run_admin(operation)
        if result.returncode != 0:
            raise AdapterError("APPLY_FAILED")
        return {"state": "applied", "operation_id": operation_id}

    def rollback_operation(
        self,
        operation: str,
        prior_manifest: dict[str, Any] | None,
        operation_id: str,
    ) -> dict[str, Any]:
        if prior_manifest is None:
            result = self._run_admin("uninstall", remove_marketplace=True)
        else:
            previous = (
                self.store.root
                / self.host_id
                / "projection"
                / prior_manifest["source_version"]
            )
            if not previous.is_dir():
                raise AdapterError("ROLLBACK_POINT_UNAVAILABLE")
            self._run_admin("uninstall", remove_marketplace=True)
            result = self._run_admin("install", source=previous)
        if result.returncode != 0:
            raise AdapterError("ROLLBACK_FAILED")
        return {"state": "rolled_back"}

    def verify(self, expected_installed: bool) -> dict[str, Any]:
        result = self._command("plugin", "list", "--json")
        if result.returncode != 0:
            return {"state": "failed", "reason": "HOST_DISCOVERY_FAILED"}
        try:
            installed = any(
                item.get("pluginId") == f"{PLUGIN_ID}@{MARKETPLACE_ID}"
                for item in json.loads(result.stdout).get("installed", [])
            )
        except (ValueError, AttributeError):
            return {"state": "failed", "reason": "HOST_DISCOVERY_INVALID"}
        return {
            "state": "verified" if installed == expected_installed else "failed",
            "mechanism": "codex plugin list --json (fresh process)",
        }


class ClaudeAdapter(HostAdapter):
    host_id = "claude"
    display_name = "Claude Code"
    integration_unit = "Marketplace Plugin"
    verified_contract = "claude-code >=2.1.212,<3.0.0"

    def projection_root(
        self,
        source_version: str | None = None,
        content_hash: str | None = None,
    ) -> Path:
        version = source_version or self.source_version
        digest = content_hash or self.content_hash
        if not PORTABLE_TOKEN.fullmatch(version) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AdapterError("INVALID_PROJECTION_REF")
        return self.store.root / self.host_id / "projection" / f"{version}-{digest[:16]}"

    @property
    def marketplace_root(self) -> Path:
        return self.store.root / self.host_id / "marketplace-current"

    def probe(self) -> dict[str, Any]:
        result = self._command("--version")
        version = _version_tuple(result.stdout or result.stderr)
        if result.returncode != 0 or version is None:
            return self._status("unavailable", reason="HOST_NOT_FOUND")
        if not ((2, 1, 212) <= version < (3, 0, 0)):
            return self._status(
                "blocked",
                host_version=".".join(map(str, version)),
                reason="HOST_VERSION_INCOMPATIBLE_UNINSTALL_SAFETY",
            )
        listed = self._command("plugin", "list", "--json")
        if listed.returncode != 0:
            return self._status("blocked", reason="HOST_DISCOVERY_FAILED")
        try:
            discovery = json.loads(listed.stdout)
            entries = (
                discovery
                if isinstance(discovery, list)
                else discovery.get("plugins", [])
            )
            installed = self._is_installed(entries)
        except (ValueError, AttributeError):
            return self._status("blocked", reason="HOST_DISCOVERY_INVALID")
        manifest = self.store.read(self.host_id)
        state = "installed" if installed and manifest else "available"
        if installed != bool(manifest):
            state = "drifted"
        return self._status(
            state,
            host_version=".".join(map(str, version)),
            installed_version=manifest.get("source_version") if manifest else None,
            target_version=self.source_version,
            drift=state == "drifted",
            ownership=bool(manifest),
            discovery_fingerprint=_canonical_digest(discovery),
        )

    def target_fingerprint(self) -> dict[str, Any]:
        root = self.marketplace_root
        if not root.exists():
            return {
                "marketplace_exists": False,
                "marketplace_is_link": False,
                "marketplace_hash": None,
            }
        if _is_link(root) or not root.is_dir():
            return {
                "marketplace_exists": True,
                "marketplace_is_link": True,
                "marketplace_hash": None,
            }
        skills = root / "plugin" / "skills"
        return {
            "marketplace_exists": True,
            "marketplace_is_link": False,
            "marketplace_hash": tree_digest(skills) if skills.is_dir() else "invalid",
        }

    @staticmethod
    def _is_installed(entries: Sequence[Any]) -> bool:
        selector = f"{PLUGIN_ID}@{MARKETPLACE_ID}"
        return any(
            (
                item.get("id") == selector
                or (
                    item.get("id") is None
                    and item.get("name") in {PLUGIN_ID, selector}
                )
            )
            for item in entries
            if isinstance(item, dict)
        )

    def _discover_installed(self) -> bool:
        result = self._command("plugin", "list", "--json")
        if result.returncode != 0:
            raise AdapterError("HOST_DISCOVERY_FAILED")
        try:
            payload = json.loads(result.stdout)
            entries = payload if isinstance(payload, list) else payload.get("plugins", [])
            if not isinstance(entries, list):
                raise ValueError
            return self._is_installed(entries)
        except (ValueError, AttributeError, TypeError) as exc:
            raise AdapterError("HOST_DISCOVERY_INVALID") from exc

    def changes(self, operation: str, manifest: dict[str, Any] | None) -> list[dict[str, str]]:
        action = {
            "install": "add",
            "update": (
                "keep"
                if manifest and manifest.get("content_hash") == self.content_hash
                else "replace" if manifest else "add"
            ),
            "uninstall": "remove",
        }[operation]
        return [
            {"action": action, "target": "claude:plugin/codex-opc-team@opc"},
            {"action": "keep", "target": "claude:plugin-data"},
            {"action": "keep", "target": "claude:unrelated-marketplaces"},
        ]

    def _build_projection(self) -> Path:
        root = self.projection_root()
        if root.exists():
            if _is_link(root) or tree_digest(root / "plugin" / "skills") != self.content_hash:
                raise AdapterError("PROJECTION_CONFLICT")
            return root
        stage = root.parent / f".stage-{secrets.token_hex(6)}"
        try:
            plugin = stage / "plugin"
            (plugin / ".claude-plugin").mkdir(parents=True)
            shutil.copytree(self.plugin_root / "skills", plugin / "skills")
            _atomic_json(
                plugin / ".claude-plugin" / "plugin.json",
                {
                    "name": PLUGIN_ID,
                    "version": self.source_version,
                    "description": "OPC governance skills projected from the canonical package.",
                },
            )
            (stage / ".claude-plugin").mkdir()
            _atomic_json(
                stage / ".claude-plugin" / "marketplace.json",
                {
                    "name": MARKETPLACE_ID,
                    "owner": {"name": "coconilu"},
                    "plugins": [
                        {
                            "name": PLUGIN_ID,
                            "source": "./plugin",
                            "version": self.source_version,
                        }
                    ],
                },
            )
            root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, root)
        except AdapterError:
            raise
        except OSError as exc:
            raise AdapterError("PROJECTION_WRITE_FAILED") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        return root

    def _activate_marketplace(self, projection: Path, operation_id: str) -> Path | None:
        """Atomically replace the stable App-owned marketplace source."""
        root = self.marketplace_root
        if root.exists() and (_is_link(root) or not root.is_dir()):
            raise AdapterError("UNSAFE_HOST_TARGET")
        backup_root = self.store.backup_root(self.host_id, operation_id)
        previous = backup_root / "marketplace-current"
        stage = root.parent / f".marketplace-stage-{secrets.token_hex(6)}"
        moved_previous = False
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(projection, stage)
            if tree_digest(stage / "plugin" / "skills") != tree_digest(
                projection / "plugin" / "skills"
            ):
                raise AdapterError("PROJECTION_VERIFY_FAILED")
            if root.exists():
                backup_root.mkdir(parents=True, exist_ok=True)
                if previous.exists():
                    raise AdapterError("BACKUP_CONFLICT")
                os.replace(root, previous)
                moved_previous = True
            os.replace(stage, root)
        except AdapterError:
            if moved_previous and previous.exists() and not root.exists():
                os.replace(previous, root)
            raise
        except OSError as exc:
            if moved_previous and previous.exists() and not root.exists():
                try:
                    os.replace(previous, root)
                except OSError:
                    pass
            raise AdapterError("APPLY_FAILED") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        return previous if moved_previous else None

    def _restore_marketplace(self, previous: Path | None) -> None:
        if previous is None:
            if self.marketplace_root.exists():
                shutil.rmtree(self.marketplace_root)
            return
        if not previous.is_dir() or _is_link(previous):
            raise AdapterError("ROLLBACK_POINT_UNAVAILABLE")
        if self.marketplace_root.exists():
            if _is_link(self.marketplace_root):
                raise AdapterError("ROLLBACK_CONFLICT")
            shutil.rmtree(self.marketplace_root)
        os.replace(previous, self.marketplace_root)

    def _refresh_plugin(self, *, failure_code: str) -> None:
        selector = f"{PLUGIN_ID}@{MARKETPLACE_ID}"
        marketplace = self._command(
            "plugin", "marketplace", "update", MARKETPLACE_ID
        )
        if marketplace.returncode != 0:
            raise AdapterError(failure_code)
        plugin = self._command("plugin", "update", selector, "--scope", "user")
        if plugin.returncode != 0:
            raise AdapterError(failure_code)

    def _restore_after_failed_refresh(self, previous: Path | None) -> None:
        self._restore_marketplace(previous)
        if previous is not None:
            self._refresh_plugin(failure_code="ROLLBACK_FAILED")

    def apply_operation(self, operation: str, operation_id: str) -> dict[str, Any]:
        selector = f"{PLUGIN_ID}@{MARKETPLACE_ID}"
        if operation == "uninstall":
            command = ("plugin", "uninstall", selector, "--scope", "user", "--keep-data")
            result = self._command(*command)
        else:
            projection = self._build_projection()
            previous = self._activate_marketplace(projection, operation_id)
            if operation == "install":
                marketplace = self._command(
                    "plugin", "marketplace", "add", str(self.marketplace_root)
                )
                if marketplace.returncode != 0 and "already" not in (
                    marketplace.stdout + marketplace.stderr
                ).lower():
                    self._restore_marketplace(previous)
                    raise AdapterError("APPLY_FAILED")
                result = self._command("plugin", "install", selector, "--scope", "user")
            else:
                try:
                    self._refresh_plugin(failure_code="APPLY_FAILED")
                except AdapterError as exc:
                    self._restore_after_failed_refresh(previous)
                    raise exc
                result = CommandResult(0)
        if result.returncode != 0:
            if operation != "uninstall":
                if operation == "install":
                    if self._discover_installed():
                        cleanup = self._command(
                            "plugin",
                            "uninstall",
                            selector,
                            "--scope",
                            "user",
                            "--keep-data",
                        )
                        if cleanup.returncode != 0:
                            raise AdapterError("ROLLBACK_FAILED")
                    if previous is not None:
                        self._restore_after_failed_refresh(previous)
                    # With no previous source, retain the successfully registered
                    # stable marketplace so its configuration never dangles.
                else:
                    self._restore_after_failed_refresh(previous)
            raise AdapterError("APPLY_FAILED")
        return {
            "state": "applied",
            "operation_id": operation_id,
            "backup_ref": operation_id if operation != "uninstall" and previous else None,
        }

    def rollback_operation(
        self,
        operation: str,
        prior_manifest: dict[str, Any] | None,
        operation_id: str,
    ) -> dict[str, Any]:
        selector = f"{PLUGIN_ID}@{MARKETPLACE_ID}"
        if prior_manifest is None:
            result = self._command(
                "plugin", "uninstall", selector, "--scope", "user", "--keep-data"
            )
        else:
            projection = self.projection_root(
                prior_manifest["source_version"],
                prior_manifest["content_hash"],
            )
            if not projection.is_dir():
                raise AdapterError("ROLLBACK_POINT_UNAVAILABLE")
            rollback_operation_id = f"{operation_id}-restore"
            current = self._activate_marketplace(projection, rollback_operation_id)
            try:
                if operation == "uninstall":
                    result = self._command(
                        "plugin", "install", selector, "--scope", "user"
                    )
                    if result.returncode != 0:
                        raise AdapterError("ROLLBACK_FAILED")
                else:
                    self._refresh_plugin(failure_code="ROLLBACK_FAILED")
            except AdapterError:
                if operation == "uninstall":
                    self._restore_marketplace(current)
                else:
                    self._restore_after_failed_refresh(current)
                raise
            result = CommandResult(0)
        if result.returncode != 0:
            raise AdapterError("ROLLBACK_FAILED")
        return {"state": "rolled_back"}

    def verify(self, expected_installed: bool) -> dict[str, Any]:
        result = self._command("plugin", "list", "--json")
        if result.returncode != 0:
            return {"state": "failed", "reason": "HOST_DISCOVERY_FAILED"}
        try:
            payload = json.loads(result.stdout)
            entries = payload if isinstance(payload, list) else payload.get("plugins", [])
            installed = self._is_installed(entries)
        except (ValueError, AttributeError):
            return {"state": "failed", "reason": "HOST_DISCOVERY_INVALID"}
        return {
            "state": "verified" if installed == expected_installed else "failed",
            "mechanism": "claude plugin list --json (fresh process)",
        }


class KimiAdapter(HostAdapter):
    host_id = "kimi"
    display_name = "Kimi Code CLI"
    integration_unit = "User Skill projection"
    verified_contract = "kimi-code >=0.29.1,<0.30.0; Plugin apply blocked"

    @property
    def host_home(self) -> Path:
        configured = (
            (self.environment or {}).get("KIMI_CODE_HOME")
            or os.environ.get("KIMI_CODE_HOME")
        )
        return (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".kimi-code"
        )

    @property
    def skill_names(self) -> list[str]:
        return [path.name for path in sorted((self.plugin_root / "skills").iterdir()) if path.is_dir()]

    def _target(self, name: str) -> Path:
        return self.host_home / "skills" / name

    def probe(self) -> dict[str, Any]:
        result = self._command("--version")
        version = _version_tuple(result.stdout or result.stderr)
        if result.returncode != 0 or version is None:
            return self._status("unavailable", reason="HOST_NOT_FOUND")
        if not ((0, 29, 1) <= version < (0, 30, 0)):
            return self._status(
                "blocked",
                host_version=".".join(map(str, version)),
                reason="HOST_VERSION_INCOMPATIBLE",
            )
        doctor = self._command("doctor")
        if doctor.returncode != 0:
            return self._status(
                "blocked",
                host_version=".".join(map(str, version)),
                reason="HOST_CONFIG_INVALID",
            )
        manifest = self.store.read(self.host_id)
        drift = False
        if manifest:
            for item in manifest["managed_targets"]:
                target = self._target(item["name"])
                if (
                    not target.is_dir()
                    or _is_link(target)
                    or tree_digest(target) != item["hash"]
                ):
                    drift = True
                    break
        state = "drifted" if drift else "installed" if manifest else "available"
        return self._status(
            state,
            host_version=".".join(map(str, version)),
            installed_version=manifest.get("source_version") if manifest else None,
            target_version=self.source_version,
            drift=drift,
            ownership=bool(manifest),
            plugin_management="blocked_no_public_noninteractive_cli",
            discovery_fingerprint=_canonical_digest(
                {
                    "doctor_returncode": doctor.returncode,
                    "doctor_stdout_hash": _sha256_bytes(doctor.stdout.encode("utf-8")),
                    "doctor_stderr_hash": _sha256_bytes(doctor.stderr.encode("utf-8")),
                }
            ),
        )

    def target_fingerprint(self) -> dict[str, Any]:
        targets: list[dict[str, Any]] = []
        for name in self.skill_names:
            target = self._target(name)
            is_link = _is_link(target)
            targets.append(
                {
                    "name": name,
                    "exists": target.exists(),
                    "is_link": is_link,
                    "hash": (
                        tree_digest(target)
                        if target.is_dir() and not is_link
                        else None
                    ),
                }
            )
        return {"skills_root": targets}

    def changes(self, operation: str, manifest: dict[str, Any] | None) -> list[dict[str, str]]:
        action = {
            "install": "add",
            "update": (
                "keep"
                if manifest and manifest.get("content_hash") == self.content_hash
                else "replace" if manifest else "add"
            ),
            "uninstall": "remove",
        }[operation]
        return [
            {"action": action, "target": f"kimi:skill/{name}"}
            for name in self.skill_names
        ]

    def _assert_owned_unchanged(self, manifest: dict[str, Any]) -> None:
        for item in manifest["managed_targets"]:
            target = self._target(item["name"])
            if (
                not target.is_dir()
                or _is_link(target)
                or tree_digest(target) != item["hash"]
            ):
                raise AdapterError("USER_MODIFIED_CONFLICT")

    def apply_operation(self, operation: str, operation_id: str) -> dict[str, Any]:
        manifest = self.store.read(self.host_id)
        if manifest:
            self._assert_owned_unchanged(manifest)
        if operation == "uninstall":
            if manifest is None:
                raise AdapterError("NOT_OPC_OWNED")
            backup = self.store.backup_root(self.host_id, operation_id)
            moved: list[tuple[Path, Path]] = []
            try:
                backup.mkdir(parents=True)
                for item in manifest["managed_targets"]:
                    target = self._target(item["name"])
                    saved = backup / item["name"]
                    os.replace(target, saved)
                    moved.append((target, saved))
            except OSError as exc:
                for target, saved in reversed(moved):
                    if saved.exists() and not target.exists():
                        try:
                            os.replace(saved, target)
                        except OSError:
                            pass
                raise AdapterError("APPLY_FAILED") from exc
            return {
                "state": "applied",
                "operation_id": operation_id,
                "backup_ref": operation_id,
            }

        skill_root = self.host_home / "skills"
        if _is_link(skill_root):
            raise AdapterError("UNSAFE_HOST_TARGET")
        skill_root.mkdir(parents=True, exist_ok=True)
        backup = self.store.backup_root(self.host_id, operation_id)
        stage = self.store.root / self.host_id / f".stage-{operation_id}"
        activated: list[tuple[Path, Path | None]] = []
        try:
            stage.mkdir(parents=True)
            for name in self.skill_names:
                source = self.plugin_root / "skills" / name
                staged = stage / name
                shutil.copytree(source, staged)
                if tree_digest(staged) != tree_digest(source):
                    raise AdapterError("PROJECTION_VERIFY_FAILED")
            for name in self.skill_names:
                target = self._target(name)
                if target.exists() and manifest is None:
                    raise AdapterError("UNKNOWN_TARGET_CONFLICT")
                previous: Path | None = None
                if target.exists():
                    backup.mkdir(parents=True, exist_ok=True)
                    previous = backup / name
                    os.replace(target, previous)
                try:
                    os.replace(stage / name, target)
                except OSError:
                    if previous is not None and previous.exists() and not target.exists():
                        os.replace(previous, target)
                    raise
                activated.append((target, previous))
        except AdapterError:
            self._restore_activated(activated)
            raise
        except OSError as exc:
            self._restore_activated(activated)
            raise AdapterError("APPLY_FAILED") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        return {
            "state": "applied",
            "operation_id": operation_id,
            "backup_ref": operation_id if backup.exists() else None,
        }

    @staticmethod
    def _restore_activated(activated: list[tuple[Path, Path | None]]) -> None:
        for target, previous in reversed(activated):
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if previous is not None and previous.exists():
                try:
                    os.replace(previous, target)
                except OSError:
                    pass

    def verify(self, expected_installed: bool) -> dict[str, Any]:
        # Kimi has no public non-interactive skill-list command.  A fresh prompt
        # is the public discovery mechanism; it may require the user's configured
        # model and therefore can be inconclusive without becoming a false PASS.
        expected = sorted(self.skill_names)
        command = [
            "--output-format",
            "stream-json",
            "--skills-dir",
            str(self.host_home / "skills"),
            "--prompt",
            (
                "Inspect the Skills available for this launch. Output one line only as "
                "OPC_SKILLS_JSON followed by a JSON array of their exact canonical names."
            ),
        ]
        result = self._command(*command)
        if not expected_installed:
            absent = all(not self._target(name).exists() for name in self.skill_names)
            return {
                "state": "verified" if absent else "failed",
                "mechanism": "fresh filesystem projection removal check",
            }
        if result.returncode != 0:
            return {
                "state": "verification_required",
                "reason": "KIMI_FRESH_PROCESS_REQUIRES_CONFIGURED_MODEL",
                "mechanism": "fresh kimi --skills-dir prompt",
            }
        assistant_text: list[str] = []
        try:
            for line in result.stdout.splitlines():
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    continue
                role = payload.get("role")
                message = payload.get("message")
                if isinstance(message, dict):
                    role = message.get("role", role)
                    content = message.get("content")
                else:
                    content = payload.get("content")
                if role not in {None, "assistant"}:
                    continue
                if isinstance(content, str):
                    assistant_text.append(content)
                elif isinstance(content, list):
                    assistant_text.extend(
                        str(item.get("text"))
                        for item in content
                        if isinstance(item, dict) and isinstance(item.get("text"), str)
                    )
            marker = re.search(
                r"OPC_SKILLS_JSON\s*(\[[^\r\n]*\])",
                "\n".join(assistant_text),
            )
            discovered = json.loads(marker.group(1)) if marker else None
        except (ValueError, AttributeError, TypeError):
            discovered = None
        return {
            "state": "verified" if discovered == expected else "failed",
            "mechanism": "fresh kimi --skills-dir common-root prompt with exact identities",
        }

    def rollback_operation(
        self,
        operation: str,
        prior_manifest: dict[str, Any] | None,
        operation_id: str,
    ) -> dict[str, Any]:
        backup = self.store.backup_root(self.host_id, operation_id)
        current = self.store.read(self.host_id)
        if operation == "install" and prior_manifest is None:
            targets = (
                current["managed_targets"]
                if current
                else [
                    {"name": name, "hash": tree_digest(self.plugin_root / "skills" / name)}
                    for name in self.skill_names
                ]
            )
            for item in targets:
                target = self._target(item["name"])
                if target.exists():
                    if _is_link(target) or tree_digest(target) != item["hash"]:
                        raise AdapterError("ROLLBACK_CONFLICT")
                    shutil.rmtree(target)
            return {"state": "rolled_back"}
        if not backup.is_dir():
            raise AdapterError("ROLLBACK_POINT_UNAVAILABLE")
        restored: list[str] = []
        for name in self.skill_names:
            source = backup / name
            target = self._target(name)
            if not source.is_dir():
                continue
            if target.exists():
                if current is None:
                    raise AdapterError("ROLLBACK_CONFLICT")
                shutil.rmtree(target)
            os.replace(source, target)
            restored.append(name)
        if not restored:
            raise AdapterError("ROLLBACK_FAILED")
        return {"state": "rolled_back"}


class AdapterManager:
    """Thread-safe preview/apply coordinator shared by CLI and local App API."""

    def __init__(
        self,
        *,
        source_root: Path | str,
        app_state_root: Path | str,
        adapters: Mapping[str, HostAdapter] | None = None,
        runner: CommandRunner = _run_command,
        environments: Mapping[str, Mapping[str, str]] | None = None,
        executables: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        plan_ttl_seconds: float = DEFAULT_PLAN_TTL_SECONDS,
    ):
        if plan_ttl_seconds <= 0:
            raise ValueError("plan_ttl_seconds must be positive")
        self.source_root = Path(source_root).resolve()
        self.store = OwnershipStore(app_state_root)
        self._lock = threading.RLock()
        self._plans: dict[str, dict[str, Any]] = {}
        self._clock = clock
        self._plan_ttl_seconds = float(plan_ttl_seconds)
        if adapters is not None:
            self.adapters = dict(adapters)
        else:
            environments = environments or {}
            executables = executables or {}
            common = {"source_root": self.source_root, "store": self.store, "runner": runner}
            self.adapters = {
                "codex": CodexAdapter(
                    **common,
                    executable=executables.get("codex"),
                    environment=environments.get("codex"),
                ),
                "claude": ClaudeAdapter(
                    **common,
                    executable=executables.get("claude"),
                    environment=environments.get("claude"),
                ),
                "kimi": KimiAdapter(
                    **common,
                    executable=executables.get("kimi"),
                    environment=environments.get("kimi"),
                ),
            }

    def inventory(self) -> dict[str, Any]:
        hosts: list[dict[str, Any]] = []
        for host_id in HOST_IDS:
            try:
                hosts.append(self.adapters[host_id].probe())
            except AdapterError as exc:
                hosts.append(
                    self.adapters[host_id]._status("blocked", reason=exc.code)
                )
        return {"schema_version": ADAPTER_API_SCHEMA, "hosts": hosts}

    def create_plan(self, host_id: str, operation: str) -> dict[str, Any]:
        adapter = self.adapters.get(host_id)
        if adapter is None:
            raise AdapterError("UNKNOWN_HOST")
        captured_state = adapter.capture_plan_state()
        payload = adapter.plan(operation, captured_state)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        plan_id = f"plan-{secrets.token_hex(12)}"
        confirmation = secrets.token_urlsafe(24)
        issued_at = self._clock()
        record = {
            "payload": payload,
            "digest": _sha256_bytes(serialized),
            "confirmation": confirmation,
            "state_digest": _canonical_digest(captured_state),
            "expires_at": issued_at + self._plan_ttl_seconds,
        }
        with self._lock:
            self._plans[plan_id] = record
        return {
            **payload,
            "plan_id": plan_id,
            "confirmation_token": confirmation,
            "expires_in_seconds": self._plan_ttl_seconds,
        }

    def apply(
        self,
        *,
        plan_id: str,
        confirmation_token: str,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._plans.pop(plan_id, None)
        if record is None:
            raise AdapterError("PLAN_NOT_FOUND_OR_USED")
        if not secrets.compare_digest(record["confirmation"], confirmation_token):
            raise AdapterError("CONFIRMATION_REQUIRED")
        if self._clock() >= record["expires_at"]:
            raise AdapterError("PLAN_EXPIRED")
        payload = record["payload"]
        current_digest = _sha256_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        if not secrets.compare_digest(record["digest"], current_digest):
            raise AdapterError("PLAN_CHANGED")
        adapter = self.adapters[payload["host_id"]]
        current_state_digest = _canonical_digest(adapter.capture_plan_state())
        if not secrets.compare_digest(record["state_digest"], current_state_digest):
            raise AdapterError("PLAN_STATE_CHANGED")
        prior_manifest = self.store.read(adapter.host_id)
        operation_id = f"op-{secrets.token_hex(10)}"
        try:
            if payload.get("no_change"):
                verification = adapter.verify(True)
                if verification["state"] == "failed":
                    raise AdapterError("VERIFY_FAILED")
                return {
                    "schema_version": ADAPTER_API_SCHEMA,
                    "host_id": adapter.host_id,
                    "operation": payload["operation"],
                    "state": "no_change",
                    "verification": verification,
                    "rollback_id": None,
                }
            applied = adapter.apply_operation(payload["operation"], operation_id)
            verification = adapter.verify(payload["operation"] != "uninstall")
            if verification["state"] == "failed":
                adapter.rollback_operation(
                    payload["operation"], prior_manifest, operation_id
                )
                raise AdapterError("VERIFY_FAILED_ROLLED_BACK")
            if payload["operation"] == "uninstall":
                self.store.remove(adapter.host_id)
            else:
                backup_refs = []
                if applied.get("backup_ref"):
                    backup_refs.append(applied["backup_ref"])
                managed_targets = [
                    {
                        "name": name,
                        "hash": tree_digest(adapter._target(name)),
                    }
                    for name in adapter.skill_names
                ] if isinstance(adapter, KimiAdapter) else [
                    {
                        "name": f"{adapter.integration_unit}:{PLUGIN_ID}",
                        "hash": adapter.content_hash,
                    }
                ]
                self.store.write(
                    adapter.host_id,
                    {
                        "schema_version": OWNERSHIP_SCHEMA,
                        "host_id": adapter.host_id,
                        "adapter_version": ADAPTER_VERSION,
                        "source_version": adapter.source_version,
                        "source_ref": adapter.source_ref,
                        "content_hash": adapter.content_hash,
                        "managed_targets": managed_targets,
                        "backup_refs": backup_refs,
                    },
                )
            self.store.write_operation(
                adapter.host_id,
                operation_id,
                operation=payload["operation"],
                prior_manifest=prior_manifest,
            )
            return {
                "schema_version": ADAPTER_API_SCHEMA,
                "host_id": adapter.host_id,
                "operation": payload["operation"],
                "state": (
                    "verification_required"
                    if verification["state"] == "verification_required"
                    else "completed"
                ),
                "verification": verification,
                "rollback_id": operation_id,
            }
        except AdapterError:
            raise
        except OSError as exc:
            raise AdapterError("APPLY_FAILED") from exc

    def rollback(self, host_id: str, rollback_id: str) -> dict[str, Any]:
        adapter = self.adapters.get(host_id)
        if adapter is None:
            raise AdapterError("UNKNOWN_HOST")
        record = self.store.read_operation(host_id, rollback_id)
        prior_manifest = record["prior_manifest"]
        result = adapter.rollback_operation(
            record["operation"], prior_manifest, rollback_id
        )
        if prior_manifest is None:
            self.store.remove(host_id)
        else:
            self.store.write(host_id, prior_manifest)
        return {
            "schema_version": ADAPTER_API_SCHEMA,
            "host_id": host_id,
            **result,
        }
