#!/usr/bin/env python3
"""Run the independent, local-only OPC App control plane.

The App is not an Agent harness. It reuses the Dashboard snapshot contract,
writes only its own explicit-project registry, and never mutates project or
knowledge state.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
import sys
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from opc_snapshot_service import (
    DashboardError,
    DashboardHTTPServer,
    DashboardRequestHandler,
    IPv6DashboardHTTPServer,
    PORTABLE_ID,
    _assert_redacted,
    _authority,
    _read_json,
    _safe_text,
    utc_now,
    validate_bind_host,
    SnapshotService,
)
from opc_memory import resolve_data_root, resolve_knowledge_root
from opc_adapters import (
    ADAPTER_API_SCHEMA,
    AdapterError,
    AdapterManager,
)


APP_CONTEXT_SCHEMA = "opc-app.context.v1"
APP_SETTINGS_SCHEMA = "opc-app.settings.v1"
MAX_SETTINGS_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_PROJECTS = 64
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE_CONTAINER_ROOT = Path(__file__).resolve().parents[3]
APP_ASSET_ROOT = PLUGIN_ROOT / "assets" / "app"


class AppSettingsError(RuntimeError):
    """An App-owned settings error safe to expose by code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def resolve_app_state_root(value: str | None = None) -> Path:
    configured = value or os.environ.get("OPC_APP_HOME")
    if configured:
        expanded = Path(configured).expanduser()
        if not expanded.is_absolute():
            raise AppSettingsError("ABSOLUTE_APP_STATE_ROOT_REQUIRED")
        return Path(os.path.abspath(expanded))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "OPC" / "App"
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "opc-app"


def _is_link(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _normalized_path(path: Path | str, *, canonical: bool) -> Path:
    value = os.path.abspath(Path(path).expanduser())
    if canonical:
        value = os.path.realpath(value)
    return Path(os.path.normcase(value))


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _runtime_root() -> Path | None:
    for parent in (PLUGIN_ROOT, *PLUGIN_ROOT.parents):
        if (parent / ".opc-app-owned.json").is_file():
            return parent
    return None


def _assert_unlinked_ancestors(path: Path) -> None:
    current = Path(os.path.abspath(path))
    while True:
        if os.path.lexists(current):
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise AppSettingsError("STATE_UNAVAILABLE") from exc
            if _is_link(metadata):
                raise AppSettingsError("UNSAFE_STATE_ROOT")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _assert_no_root_overlap(
    state_root: Path,
    forbidden_roots: Sequence[Path | str],
) -> None:
    state_variants = (
        _normalized_path(state_root, canonical=False),
        _normalized_path(state_root, canonical=True),
    )
    for forbidden in forbidden_roots:
        forbidden_variants = (
            _normalized_path(forbidden, canonical=False),
            _normalized_path(forbidden, canonical=True),
        )
        if any(
            _paths_overlap(state_variant, forbidden_variant)
            for state_variant in state_variants
            for forbidden_variant in forbidden_variants
        ):
            raise AppSettingsError("STATE_ROOT_OVERLAP")


def _safe_state_root(
    path: Path,
    *,
    forbidden_roots: Sequence[Path | str] = (),
    create: bool = False,
) -> Path:
    root = Path(os.path.abspath(path))
    _assert_unlinked_ancestors(root)
    built_in_roots: list[Path | str] = [PLUGIN_ROOT, SOURCE_CONTAINER_ROOT]
    runtime_root = _runtime_root()
    if runtime_root is not None:
        built_in_roots.append(runtime_root)
    _assert_no_root_overlap(root, (*built_in_roots, *forbidden_roots))
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(root):
        return root
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise AppSettingsError("STATE_UNAVAILABLE") from exc
    if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise AppSettingsError("UNSAFE_STATE_ROOT")
    return root


def _default_settings() -> dict[str, Any]:
    return {
        "schema_version": APP_SETTINGS_SCHEMA,
        "projects": [],
        "selected_project_id": None,
    }


class AppSettingsStore:
    """Atomic App-owned registry; project paths never leave this process."""

    def __init__(
        self,
        state_root: Path | str,
        *,
        forbidden_roots: Sequence[Path | str] = (),
    ):
        self._configured_forbidden_roots = tuple(Path(item) for item in forbidden_roots)
        self.root = _safe_state_root(
            Path(state_root),
            forbidden_roots=self._configured_forbidden_roots,
        )
        self.path = self.root / "settings.json"
        self._lock = threading.RLock()

    def _assert_state_isolated(
        self,
        project_roots: Sequence[Path | str] = (),
    ) -> None:
        _assert_no_root_overlap(
            self.root,
            (*self._configured_forbidden_roots, *project_roots),
        )

    def _load_strict(self) -> dict[str, Any]:
        if not os.path.lexists(self.path):
            return _default_settings()
        try:
            payload = _read_json(
                self.path,
                root=self.root,
                maximum=MAX_SETTINGS_BYTES,
            )
        except DashboardError as exc:
            raise AppSettingsError("INVALID_SETTINGS") from exc
        if (
            payload.get("schema_version") != APP_SETTINGS_SCHEMA
            or set(payload) != {"schema_version", "projects", "selected_project_id"}
            or not isinstance(payload.get("projects"), list)
            or len(payload["projects"]) > MAX_PROJECTS
        ):
            raise AppSettingsError("INVALID_SETTINGS")
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for item in payload["projects"]:
            if not isinstance(item, dict) or set(item) != {"id", "path", "added_at"}:
                raise AppSettingsError("INVALID_SETTINGS")
            item_id = item.get("id")
            raw_path = item.get("path")
            if (
                not isinstance(item_id, str)
                or not item_id.startswith("project-")
                or len(item_id) > 64
                or not isinstance(raw_path, str)
                or not raw_path
                or len(raw_path) > 4096
                or not isinstance(item.get("added_at"), str)
            ):
                raise AppSettingsError("INVALID_SETTINGS")
            normalized = os.path.normcase(os.path.abspath(raw_path))
            if item_id in seen_ids or normalized in seen_paths:
                raise AppSettingsError("INVALID_SETTINGS")
            seen_ids.add(item_id)
            seen_paths.add(normalized)
        selected = payload.get("selected_project_id")
        if selected is not None and selected not in seen_ids:
            raise AppSettingsError("INVALID_SETTINGS")
        self._assert_state_isolated(
            [Path(item["path"]) for item in payload["projects"]]
        )
        return payload

    def _write(self, payload: Mapping[str, Any]) -> None:
        project_roots = [
            Path(item["path"])
            for item in payload.get("projects", [])
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        ]
        self._assert_state_isolated(project_roots)
        root = _safe_state_root(
            self.root,
            forbidden_roots=(
                *self._configured_forbidden_roots,
                *project_roots,
            ),
            create=True,
        )
        if os.path.lexists(self.path):
            metadata = self.path.lstat()
            if (
                _is_link(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise AppSettingsError("UNSAFE_SETTINGS_FILE")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_SETTINGS_BYTES:
            raise AppSettingsError("SETTINGS_TOO_LARGE")
        temporary = root / f".settings-{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(temporary, flags, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short settings write")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise AppSettingsError("SETTINGS_WRITE_FAILED") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _normalize_project_root(value: str) -> Path:
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise AppSettingsError("INVALID_PROJECT_ROOT")
        expanded = Path(value.strip()).expanduser()
        if not expanded.is_absolute():
            raise AppSettingsError("ABSOLUTE_PROJECT_ROOT_REQUIRED")
        root = Path(os.path.abspath(expanded))
        try:
            metadata = root.lstat()
        except OSError as exc:
            raise AppSettingsError("PROJECT_UNAVAILABLE") from exc
        if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise AppSettingsError("UNSAFE_PROJECT_ROOT")
        try:
            project = _read_json(root / ".opc" / "project.json", root=root)
        except DashboardError as exc:
            raise AppSettingsError("INVALID_OPC_PROJECT") from exc
        project_id = _safe_text(project.get("project_id"), maximum=128)
        name = _safe_text(project.get("name"), maximum=160)
        if project_id is None or PORTABLE_ID.fullmatch(project_id) is None or name is None:
            raise AppSettingsError("INVALID_OPC_PROJECT")
        return root

    def read_state(self) -> tuple[str, dict[str, Any]]:
        with self._lock:
            try:
                return "ready", self._load_strict()
            except AppSettingsError:
                return "invalid", _default_settings()

    def project_roots(self) -> tuple[Path, ...]:
        state, payload = self.read_state()
        if state != "ready":
            return ()
        return tuple(Path(item["path"]) for item in payload["projects"])

    def context(self) -> dict[str, Any]:
        state, payload = self.read_state()
        projects: list[dict[str, Any]] = []
        if state == "ready":
            for index, item in enumerate(payload["projects"]):
                root = Path(item["path"])
                project_state = "available"
                project_id: str | None = None
                name = f"项目 {index + 1}"
                try:
                    raw = _read_json(root / ".opc" / "project.json", root=root)
                    candidate_id = _safe_text(raw.get("project_id"), maximum=128)
                    candidate_name = _safe_text(raw.get("name"), maximum=160)
                    if (
                        candidate_id is None
                        or PORTABLE_ID.fullmatch(candidate_id) is None
                        or candidate_name is None
                    ):
                        raise DashboardError("INVALID_PROJECT")
                    project_id = candidate_id
                    name = candidate_name
                except DashboardError:
                    project_state = "unavailable"
                projects.append(
                    {
                        "id": item["id"],
                        "project_id": project_id,
                        "name": name,
                        "root_label": f"显式目录 {index + 1}",
                        "state": project_state,
                    }
                )
        context = {
            "schema_version": APP_CONTEXT_SCHEMA,
            "settings_state": state,
            "projects": projects,
            "selected_project_id": payload.get("selected_project_id"),
            "warning": (
                None
                if state == "ready"
                else {
                    "code": "INVALID_APP_SETTINGS",
                    "message": "App 接入清单不可读；未扫描磁盘，也未推测项目。",
                }
            ),
        }
        _assert_redacted(context)
        return context

    def add_project(self, value: str) -> None:
        with self._lock:
            payload = self._load_strict()
            root = self._normalize_project_root(value)
            self._assert_state_isolated([root])
            normalized = os.path.normcase(str(root))
            if any(
                os.path.normcase(os.path.abspath(item["path"])) == normalized
                for item in payload["projects"]
            ):
                raise AppSettingsError("PROJECT_ALREADY_REGISTERED")
            if len(payload["projects"]) >= MAX_PROJECTS:
                raise AppSettingsError("PROJECT_LIMIT_REACHED")
            item_id = f"project-{uuid.uuid4().hex[:16]}"
            payload["projects"].append(
                {"id": item_id, "path": str(root), "added_at": utc_now()}
            )
            if payload["selected_project_id"] is None:
                payload["selected_project_id"] = item_id
            self._write(payload)

    def select_project(self, item_id: str) -> None:
        if not isinstance(item_id, str):
            raise AppSettingsError("INVALID_PROJECT_SELECTION")
        with self._lock:
            payload = self._load_strict()
            if item_id not in {item["id"] for item in payload["projects"]}:
                raise AppSettingsError("PROJECT_NOT_REGISTERED")
            payload["selected_project_id"] = item_id
            self._write(payload)

    def remove_project(self, item_id: str) -> None:
        if not isinstance(item_id, str):
            raise AppSettingsError("INVALID_PROJECT_SELECTION")
        with self._lock:
            payload = self._load_strict()
            remaining = [item for item in payload["projects"] if item["id"] != item_id]
            if len(remaining) == len(payload["projects"]):
                raise AppSettingsError("PROJECT_NOT_REGISTERED")
            payload["projects"] = remaining
            if payload["selected_project_id"] == item_id:
                payload["selected_project_id"] = remaining[0]["id"] if remaining else None
            self._write(payload)


class DemoSettingsStore:
    def project_roots(self) -> tuple[Path, ...]:
        return ()

    def context(self) -> dict[str, Any]:
        context = {
            "schema_version": APP_CONTEXT_SCHEMA,
            "settings_state": "demo",
            "projects": [
                {
                    "id": "project-demo-primary",
                    "project_id": "synthetic-dashboard",
                    "name": "本地 OPC Dashboard",
                    "root_label": "合成目录 1",
                    "state": "available",
                },
                {
                    "id": "project-demo-secondary",
                    "project_id": "synthetic-memory",
                    "name": "组织知识治理",
                    "root_label": "合成目录 2",
                    "state": "available",
                },
            ],
            "selected_project_id": "project-demo-primary",
            "warning": {
                "code": "SYNTHETIC_DEMO",
                "message": "当前为合成演示数据，不代表真实组织状态。",
            },
        }
        _assert_redacted(context)
        return context


class OPCAppHTTPServer(DashboardHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler: type[DashboardRequestHandler],
        *,
        snapshot_provider: Any,
        asset_root: Path,
        settings_store: AppSettingsStore | DemoSettingsStore,
        adapter_manager: AdapterManager | None,
        demo: bool,
    ) -> None:
        self.settings_store = settings_store
        self.demo = demo
        self.adapter_manager = adapter_manager
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(
            address,
            handler,
            snapshot_provider=snapshot_provider,
            asset_root=asset_root,
        )


class IPv6OPCAppHTTPServer(OPCAppHTTPServer):
    address_family = socket.AF_INET6


class OPCAppRequestHandler(DashboardRequestHandler):
    server_version = "OPCApp/1"

    @property
    def app_server(self) -> OPCAppHTTPServer:
        return self.server  # type: ignore[return-value]

    def _send_json(
        self,
        status_code: int,
        value: Mapping[str, Any],
        *,
        head_only: bool = False,
    ) -> None:
        _assert_redacted(value)
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(
            status_code,
            payload,
            "application/json; charset=utf-8",
            head_only=head_only,
        )

    def _context_payload(self) -> dict[str, Any]:
        payload = self.app_server.settings_store.context()
        payload["csrf_token"] = self.app_server.csrf_token
        _assert_redacted(payload)
        return payload

    def _handle_read(self, *, head_only: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.path not in {"/api/app-context", "/api/adapters"}:
            super()._handle_read(head_only=head_only)
            return
        if not self._valid_host():
            self._json_error(400, "INVALID_HOST", head_only=head_only)
            return
        if not self._valid_origin():
            self._json_error(403, "ORIGIN_FORBIDDEN", head_only=head_only)
            return
        if parsed.query or parsed.fragment:
            self._json_error(404, "NOT_FOUND", head_only=head_only)
            return
        if parsed.path == "/api/app-context":
            payload = self._context_payload()
        elif self.app_server.adapter_manager is None:
            payload = {
                "schema_version": ADAPTER_API_SCHEMA,
                "hosts": [
                    {
                        "host_id": host_id,
                        "display_name": display_name,
                        "state": "unavailable",
                        "reason": "DEMO_OR_ADAPTER_SERVICE_UNAVAILABLE",
                    }
                    for host_id, display_name in (
                        ("codex", "Codex"),
                        ("claude", "Claude Code"),
                        ("kimi", "Kimi Code CLI"),
                    )
                ],
            }
        else:
            payload = self.app_server.adapter_manager.inventory()
        self._send_json(200, payload, head_only=head_only)

    def _valid_csrf(self) -> bool:
        values = self.headers.get_all("X-OPC-CSRF", [])
        return (
            len(values) == 1
            and secrets.compare_digest(values[0], self.app_server.csrf_token)
        )

    def _read_request_json(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise AppSettingsError("INVALID_REQUEST")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise AppSettingsError("INVALID_REQUEST")
        try:
            length = int(lengths[0])
        except (TypeError, ValueError) as exc:
            raise AppSettingsError("INVALID_REQUEST") from exc
        if length < 2 or length > MAX_REQUEST_BYTES:
            raise AppSettingsError("INVALID_REQUEST")
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise AppSettingsError("INVALID_CONTENT_TYPE")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise AppSettingsError("INVALID_REQUEST") from exc
        if not isinstance(payload, dict):
            raise AppSettingsError("INVALID_REQUEST")
        return payload

    def _prepare_mutation(self) -> bool:
        if not self._valid_host():
            self._json_error(400, "INVALID_HOST")
            return False
        if not self._valid_origin():
            self._json_error(403, "ORIGIN_FORBIDDEN")
            return False
        if not self._valid_csrf():
            self._json_error(403, "CSRF_FORBIDDEN")
            return False
        if self.app_server.demo:
            self._json_error(409, "DEMO_SETTINGS_READ_ONLY")
            return False
        return True

    def do_POST(self) -> None:
        if not self._prepare_mutation():
            return
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._json_error(404, "NOT_FOUND")
            return
        try:
            payload = self._read_request_json()
            store = self.app_server.settings_store
            if not isinstance(store, AppSettingsStore):
                raise AppSettingsError("SETTINGS_UNAVAILABLE")
            if parsed.path == "/api/projects" and set(payload) == {"path"}:
                store.add_project(payload["path"])
                status = 201
            elif parsed.path == "/api/selection" and set(payload) == {"project_id"}:
                store.select_project(payload["project_id"])
                status = 200
            elif (
                parsed.path == "/api/adapters/plan"
                and set(payload) == {"host_id", "operation"}
                and self.app_server.adapter_manager is not None
            ):
                result = self.app_server.adapter_manager.create_plan(
                    payload["host_id"], payload["operation"]
                )
                self._send_json(200, result)
                return
            elif (
                parsed.path == "/api/adapters/apply"
                and set(payload) == {"plan_id", "confirmation_token"}
                and self.app_server.adapter_manager is not None
            ):
                result = self.app_server.adapter_manager.apply(
                    plan_id=payload["plan_id"],
                    confirmation_token=payload["confirmation_token"],
                )
                self._send_json(200, result)
                return
            elif (
                parsed.path == "/api/adapters/rollback"
                and set(payload) == {"host_id", "rollback_id"}
                and self.app_server.adapter_manager is not None
            ):
                result = self.app_server.adapter_manager.rollback(
                    payload["host_id"], payload["rollback_id"]
                )
                self._send_json(200, result)
                return
            else:
                self._json_error(404, "NOT_FOUND")
                return
            self._send_json(status, self._context_payload())
        except AppSettingsError as exc:
            self._json_error(400, exc.code)
        except AdapterError as exc:
            if exc.rollback_id is None:
                self._json_error(409, exc.code)
            else:
                self._send_json(
                    409,
                    {
                        "error": exc.code,
                        "rollback_id": exc.rollback_id,
                    },
                )

    def do_DELETE(self) -> None:
        if not self._prepare_mutation():
            return
        if (
            self.headers.get("Transfer-Encoding") is not None
            or self.headers.get_all("Content-Length", []) not in ([], ["0"])
        ):
            self._json_error(400, "INVALID_REQUEST")
            return
        parsed = urlsplit(self.path)
        prefix = "/api/projects/"
        if parsed.query or parsed.fragment or not parsed.path.startswith(prefix):
            self._json_error(404, "NOT_FOUND")
            return
        item_id = unquote(parsed.path[len(prefix) :])
        if not item_id or "/" in item_id or len(item_id) > 64:
            self._json_error(404, "NOT_FOUND")
            return
        try:
            store = self.app_server.settings_store
            if not isinstance(store, AppSettingsStore):
                raise AppSettingsError("SETTINGS_UNAVAILABLE")
            store.remove_project(item_id)
            self._send_json(200, self._context_payload())
        except AppSettingsError as exc:
            self._json_error(400, exc.code)


def create_app_server(
    *,
    host: str,
    port: int,
    snapshot_provider: Any,
    settings_store: AppSettingsStore | DemoSettingsStore,
    adapter_manager: AdapterManager | None = None,
    demo: bool = False,
    asset_root: Path | str | None = None,
) -> OPCAppHTTPServer:
    validate_bind_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise DashboardError("INVALID_PORT")
    root = Path(asset_root) if asset_root is not None else APP_ASSET_ROOT
    server_type = IPv6OPCAppHTTPServer if host == "::1" else OPCAppHTTPServer
    try:
        return server_type(
            (host, port),
            OPCAppRequestHandler,
            snapshot_provider=snapshot_provider,
            asset_root=root,
            settings_store=settings_store,
            adapter_manager=adapter_manager,
            demo=demo,
        )
    except OSError as exc:
        raise DashboardError("PORT_UNAVAILABLE") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root")
    parser.add_argument("--knowledge-root")
    parser.add_argument("--data-root")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--host", "--bind", dest="host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8570)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_bind_host(args.host)
        if args.demo:
            if args.knowledge_root or args.data_root or args.state_root:
                parser.error("--demo 不能与真实数据根或 App 状态根参数同时使用")
            settings_store: AppSettingsStore | DemoSettingsStore = DemoSettingsStore()
            service = SnapshotService(
                project_roots_provider=settings_store.project_roots,
                demo=True,
            )
        else:
            knowledge_root = resolve_knowledge_root(args.knowledge_root)
            data_root = resolve_data_root(args.data_root)
            settings_store = AppSettingsStore(
                resolve_app_state_root(args.state_root),
                forbidden_roots=(knowledge_root, data_root),
            )
            service = SnapshotService(
                project_roots_provider=settings_store.project_roots,
                knowledge_root=knowledge_root,
                data_root=data_root,
                allow_empty=True,
            )
        adapter_manager = (
            None
            if args.demo
            else AdapterManager(
                source_root=SOURCE_CONTAINER_ROOT,
                app_state_root=settings_store.root,
            )
        )
        server = create_app_server(
            host=args.host,
            port=args.port,
            snapshot_provider=service.snapshot,
            settings_store=settings_store,
            adapter_manager=adapter_manager,
            demo=args.demo,
        )
    except (DashboardError, AppSettingsError) as exc:
        code = exc.code
        print(f"OPC_APP_ERROR: {code}", file=sys.stderr)
        return 2
    url = f"http://{_authority(args.host, int(server.server_address[1]))}/"
    print(f"OPC App: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
