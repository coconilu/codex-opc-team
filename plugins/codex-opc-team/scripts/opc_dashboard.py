#!/usr/bin/env python3
"""Serve a local, read-only OPC dashboard and build redacted snapshots.

The dashboard is deliberately a projection over existing File/Git and project
state.  It does not mutate OPC state and never returns knowledge bodies, raw
feedback, filesystem paths, or run/session/thread identifiers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from opc_feedback import read_feedback
from opc_lineage import build_view
from opc_memory import (
    FileGitBackend,
    MemoryService,
    resolve_data_root,
    resolve_knowledge_root,
)


SCHEMA_VERSION = "opc-dashboard.snapshot.v1"
MAX_JSON_BYTES = 512 * 1024
MAX_ACCEPTANCE_BYTES = 128 * 1024
MAX_DEMO_BYTES = 512 * 1024
MAX_PROJECTS = 64
PORTABLE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
RUN_STATUSES = {
    "aligning",
    "planned",
    "implementing",
    "validating",
    "ready_for_manager",
    "completed",
    "paused",
    "failed",
}
ASSET_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'none'; connect-src 'self'; "
        "img-src 'self' data:; script-src 'self'; style-src 'self'"
    ),
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "interest-cohort=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class DashboardError(RuntimeError):
    """A safe dashboard error whose code is suitable for redacted output."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_link(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
        int(metadata.st_nlink),
    )


def _read_checkpoint(label: str, path: Path) -> None:
    """Test seam for simulating changes at read transaction boundaries."""


def _assert_safe_chain(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise DashboardError("PATH_ESCAPE") from exc
    current = root
    for part in (Path("."), *relative.parts):
        if part != Path("."):
            current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise DashboardError("SOURCE_UNAVAILABLE") from exc
        if _is_link(metadata):
            raise DashboardError("LINKED_SOURCE")


def _read_stable_bytes(
    path: Path,
    *,
    root: Path,
    maximum: int,
    label: str,
) -> bytes:
    """Read one bounded regular file without following links or hardlinks."""

    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    _assert_safe_chain(lexical_root, lexical_path)
    try:
        before = lexical_path.lstat()
    except OSError as exc:
        raise DashboardError("SOURCE_UNAVAILABLE") from exc
    if _is_link(before) or not stat.S_ISREG(before.st_mode):
        raise DashboardError("UNSAFE_SOURCE")
    if before.st_nlink != 1:
        raise DashboardError("HARDLINKED_SOURCE")
    if before.st_size > maximum:
        raise DashboardError("SOURCE_TOO_LARGE")
    _read_checkpoint(f"{label}:before_open", lexical_path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(lexical_path, flags)
        opened = os.fstat(descriptor)
        if (
            _is_link(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise DashboardError("SOURCE_CHANGED")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise DashboardError("SOURCE_TOO_LARGE")
        after_descriptor = os.fstat(descriptor)
        _read_checkpoint(f"{label}:before_verify", lexical_path)
        after_path = lexical_path.lstat()
        if (
            _identity(before) != _identity(after_descriptor)
            or _identity(before) != _identity(after_path)
            or _is_link(after_path)
        ):
            raise DashboardError("SOURCE_CHANGED")
        return payload
    except DashboardError:
        raise
    except OSError as exc:
        raise DashboardError("SOURCE_UNAVAILABLE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json(path: Path, *, root: Path, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    raw = _read_stable_bytes(path, root=root, maximum=maximum, label=path.name)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise DashboardError("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise DashboardError("INVALID_JSON")
    return value


def _safe_text(value: Any, *, maximum: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(char) < 32 for char in text)
        or _looks_like_absolute_path(text)
    ):
        return None
    return text


def _looks_like_absolute_path(value: str) -> bool:
    posix_path = re.search(
        r"(?:^|[\s\"'(`])/(?:home|root|etc|var|tmp|opt|usr|private|mnt|srv|workspace|workspaces)(?:/|$)",
        value,
    )
    return bool(
        re.search(r"(?:^|[\s\"'])[A-Za-z]:[\\/]", value)
        or value.startswith("\\\\")
        or value.startswith("/")
        or posix_path
    )


def _safe_time(value: Any) -> str | None:
    text = _safe_text(value, maximum=64)
    if text is None:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _parse_acceptance(path: Path, project_root: Path) -> dict[str, Any]:
    try:
        raw = _read_stable_bytes(
            path,
            root=project_root,
            maximum=MAX_ACCEPTANCE_BYTES,
            label="acceptance",
        )
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise DashboardError("INVALID_ACCEPTANCE") from exc
    lines = [line.strip() for line in text.splitlines()]
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("|")
            and "criterion" in line.lower()
            and "status" in line.lower()
        ),
        None,
    )
    if header_index is None or header_index + 1 >= len(lines):
        raise DashboardError("INVALID_ACCEPTANCE")
    header = [part.strip().lower() for part in lines[header_index].strip("|").split("|")]
    try:
        status_index = header.index("status")
    except ValueError as exc:
        raise DashboardError("INVALID_ACCEPTANCE") from exc
    separator = [part.strip() for part in lines[header_index + 1].strip("|").split("|")]
    if len(separator) != len(header) or not all(re.fullmatch(r":?-{3,}:?", part) for part in separator):
        raise DashboardError("INVALID_ACCEPTANCE")
    statuses: list[str] = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = [part.strip() for part in line.strip("|").split("|")]
        if len(cells) != len(header):
            raise DashboardError("INVALID_ACCEPTANCE")
        statuses.append(cells[status_index].lower())
    if not statuses:
        raise DashboardError("INVALID_ACCEPTANCE")
    passed = sum(status in {"pass", "passed", "done", "complete", "completed"} for status in statuses)
    return {"passed": passed, "total": len(statuses), "state": "available"}


def _unavailable_acceptance(state: str) -> dict[str, Any]:
    return {"passed": 0, "total": 0, "state": state}


def _project_warning(code: str, project_id: str | None, message: str) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "severity": "warning", "message": message}
    if project_id:
        out["project_id"] = project_id
    return out


def _feedback_status(project_root: Path) -> str:
    try:
        view = read_feedback(project_root)
        record = view.get("structured_feedback")
        if record is None:
            return "unavailable"
        events = record.get("events")
        return "available" if isinstance(events, list) else "invalid"
    except Exception:
        return "invalid"


def _lineage_status(project_root: Path, knowledge_root: Path | None) -> str:
    try:
        view = build_view(project_root, knowledge_root=knowledge_root)
        state = view.get("lineage_status")
        return state if state in {"available", "degraded", "unavailable"} else "invalid"
    except Exception:
        return "invalid"


def _read_project(
    project_root: Path,
    *,
    index: int,
    knowledge_root: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fallback_id = f"project-{index + 1}"
    fallback = {
        "id": fallback_id,
        "name": f"项目 {index + 1}",
        "source_state": "invalid",
        "run": {
            "title": "状态不可用",
            "status": "invalid",
            "active": False,
            "updated_at": None,
        },
        "acceptance": _unavailable_acceptance("invalid"),
        "feedback_status": "invalid",
        "lineage_status": "invalid",
    }
    warnings: list[dict[str, Any]] = []
    lexical_root = Path(os.path.abspath(project_root))
    try:
        project = _read_json(lexical_root / ".opc" / "project.json", root=lexical_root)
        project_id = _safe_text(project.get("project_id"), maximum=128)
        name = _safe_text(project.get("name"), maximum=160)
        if (
            project_id is None
            or PORTABLE_ID.fullmatch(project_id) is None
            or name is None
        ):
            raise DashboardError("INVALID_PROJECT")
    except DashboardError as exc:
        warnings.append(
            _project_warning(
                exc.code,
                None,
                f"第 {index + 1} 个项目来源无效，已安全降级。",
            )
        )
        return fallback, warnings

    run_projection = {
        "title": "暂无运行",
        "status": "unavailable",
        "active": False,
        "updated_at": None,
    }
    source_state = "available"
    try:
        run = _read_json(lexical_root / ".opc" / "run.json", root=lexical_root)
        title = _safe_text(run.get("title"), maximum=200)
        status_value = run.get("status")
        active = run.get("active")
        updated_at = _safe_time(run.get("updated_at"))
        if (
            run.get("project_id") != project_id
            or title is None
            or status_value not in RUN_STATUSES
            or not isinstance(active, bool)
            or updated_at is None
        ):
            raise DashboardError("INVALID_RUN")
        run_projection = {
            "title": title,
            "status": status_value,
            "active": active,
            "updated_at": updated_at,
        }
    except DashboardError as exc:
        source_state = "degraded"
        run_projection["status"] = "invalid" if exc.code != "SOURCE_UNAVAILABLE" else "unavailable"
        warnings.append(
            _project_warning(exc.code, project_id, "运行状态不可用，项目仍以只读方式展示。")
        )

    try:
        acceptance = _parse_acceptance(lexical_root / ".opc" / "acceptance.md", lexical_root)
    except DashboardError as exc:
        source_state = "degraded"
        acceptance = _unavailable_acceptance(
            "unavailable" if exc.code == "SOURCE_UNAVAILABLE" else "invalid"
        )
        warnings.append(
            _project_warning(exc.code, project_id, "验收契约不可用，未推测验收进度。")
        )

    feedback_status = _feedback_status(lexical_root)
    lineage_status = _lineage_status(lexical_root, knowledge_root)
    if feedback_status == "invalid" or lineage_status == "invalid":
        source_state = "degraded"
    return (
        {
            "id": project_id,
            "name": name,
            "source_state": source_state,
            "run": run_projection,
            "acceptance": acceptance,
            "feedback_status": feedback_status,
            "lineage_status": lineage_status,
        },
        warnings,
    )


def _knowledge_snapshot(
    knowledge_root: Path,
    data_root: Path,
) -> tuple[dict[str, int | str], dict[str, Any], list[dict[str, Any]]]:
    counts: dict[str, int | str] = {
        "candidate": 0,
        "approved_uncommitted": 0,
        "published": 0,
        "rejected": 0,
        "obsolete": 0,
        "state": "unavailable",
    }
    health = {
        "file_git": {
            "state": "unavailable",
            "label": "File/Git 不可用",
            "detail": "无法读取规范知识状态。",
        },
        "mem0": {
            "state": "unavailable",
            "label": "Mem0 不可用",
            "detail": "可选检索索引状态不可用。",
        },
    }
    warnings: list[dict[str, Any]] = []
    try:
        backend = FileGitBackend(knowledge_root)
        doctor = backend.doctor()
        if doctor.get("state") == "NOT_INITIALIZED":
            counts["state"] = "unavailable"
            health["file_git"] = {
                "state": "unavailable",
                "label": "File/Git 未初始化",
                "detail": "规范知识仓库尚未就绪。",
            }
            warnings.append(
                {
                    "code": "KNOWLEDGE_NOT_INITIALIZED",
                    "severity": "warning",
                    "message": "知识仓库尚未初始化。",
                }
            )
        elif not doctor.get("ok"):
            counts["state"] = "invalid"
            health["file_git"] = {
                "state": "invalid",
                "label": "File/Git 无效",
                "detail": "知识仓库结构或记录未通过校验。",
            }
            warnings.append(
                {
                    "code": "KNOWLEDGE_INVALID",
                    "severity": "warning",
                    "message": "知识仓库存在无效记录，未展示推测数据。",
                }
            )
        else:
            governance = backend.governance_snapshot()
            inventory = governance.get("inventory", {})
            provenance = governance.get("provenance", {})
            for record_id, metadata in inventory.items():
                status_value = metadata.get("status")
                if status_value == "approved":
                    proof = provenance.get(record_id, {})
                    if proof.get("source_commit"):
                        counts["published"] = int(counts["published"]) + 1
                    else:
                        counts["approved_uncommitted"] = int(counts["approved_uncommitted"]) + 1
                elif status_value in {"candidate", "rejected", "obsolete"}:
                    counts[status_value] = int(counts[status_value]) + 1
            counts["state"] = "available"
            git_state = doctor.get("git", {})
            degraded = not doctor.get("provenance_ready") or bool(git_state.get("authoritative_uncommitted"))
            health["file_git"] = {
                "state": "degraded" if degraded else "available",
                "label": "File/Git 需关注" if degraded else "File/Git 正常",
                "detail": (
                    "知识可读，但 Git provenance 尚未完全就绪。"
                    if degraded
                    else "规范知识与 Git provenance 已就绪。"
                ),
            }
        service = MemoryService.from_paths(knowledge_root, data_root)
        memory_status = service.status()
        mem0 = memory_status.get("mem0", {})
        provider_health = mem0.get("health")
        if provider_health == "disabled":
            health["mem0"] = {
                "state": "disabled",
                "label": "Mem0 已停用",
                "detail": "当前使用 File/Git 检索，功能可正常降级。",
            }
        elif provider_health == "configured-unverified":
            health["mem0"] = {
                "state": "degraded",
                "label": "Mem0 待验证",
                "detail": "索引已配置，但本次只读检查未调用外部 Provider。",
            }
        else:
            health["mem0"] = {
                "state": "degraded",
                "label": "Mem0 已降级",
                "detail": "可选索引不可用，仍使用 File/Git。",
            }
    except Exception:
        warnings.append(
            {
                "code": "KNOWLEDGE_SOURCE_UNAVAILABLE",
                "severity": "warning",
                "message": "知识或检索状态不可用，未自动填充演示数据。",
            }
        )
    return counts, health, warnings


def _manager_queue(
    projects: Sequence[Mapping[str, Any]],
    knowledge: Mapping[str, Any],
    health: Mapping[str, Any],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for project in projects:
        project_id = str(project["id"])
        run = project["run"]
        acceptance = project["acceptance"]
        if run["status"] == "ready_for_manager":
            queue.append(
                {
                    "id": f"handoff-{project_id}",
                    "type": "manager_handoff",
                    "severity": "high",
                    "title": f"{project['name']} 等待体验",
                    "description": "实现与内部验证已完成，等待经理体验与方向判断。",
                    "project_id": project_id,
                    "next_step": "按体验路径检查结果并给出反馈。",
                }
            )
        elif run["status"] in {"failed", "paused"}:
            queue.append(
                {
                    "id": f"run-{project_id}",
                    "type": "run_attention",
                    "severity": "high",
                    "title": f"{project['name']} 运行需处理",
                    "description": f"当前运行状态为 {run['status']}。",
                    "project_id": project_id,
                    "next_step": "查看项目并决定恢复、调整或终止。",
                }
            )
        if (
            acceptance.get("state") == "available"
            and acceptance.get("total", 0) > acceptance.get("passed", 0)
        ):
            pending = int(acceptance["total"]) - int(acceptance["passed"])
            queue.append(
                {
                    "id": f"acceptance-{project_id}",
                    "type": "acceptance",
                    "severity": "medium",
                    "title": f"{project['name']} 尚有 {pending} 项验收",
                    "description": "验收契约仍有未通过项。",
                    "project_id": project_id,
                    "next_step": "查看未完成验收项并补齐证据。",
                }
            )
    candidates = int(knowledge.get("candidate", 0))
    if candidates:
        queue.append(
            {
                "id": "knowledge-candidates",
                "type": "knowledge_review",
                "severity": "medium",
                "title": f"{candidates} 条经验候选待审",
                "description": "候选经验不会自动成为组织知识。",
                "project_id": None,
                "next_step": "核对证据、适用范围与冲突后再决定是否批准。",
            }
        )
    uncommitted = int(knowledge.get("approved_uncommitted", 0))
    if uncommitted:
        queue.append(
            {
                "id": "knowledge-uncommitted",
                "type": "knowledge_publish",
                "severity": "high",
                "title": f"{uncommitted} 条已批准知识尚未提交",
                "description": "批准状态尚无当前 Git HEAD provenance。",
                "project_id": None,
                "next_step": "复核精确变更后提交规范知识仓库。",
            }
        )
    if health.get("file_git", {}).get("state") in {"invalid", "unavailable"}:
        queue.append(
            {
                "id": "file-git-health",
                "type": "system_health",
                "severity": "high",
                "title": "File/Git 权威源不可用",
                "description": "知识统计已安全降级，未使用演示数据替代。",
                "project_id": None,
                "next_step": "运行 OPC memory doctor 并修复报告的问题。",
            }
        )
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(queue, key=lambda item: (order[item["severity"]], item["id"]))


def aggregate_snapshot(
    project_roots: Sequence[Path | str],
    *,
    knowledge_root: Path | str,
    data_root: Path | str,
    now: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    if not project_roots or len(project_roots) > MAX_PROJECTS:
        raise DashboardError("INVALID_PROJECT_ROOT_COUNT")
    knowledge_path = Path(knowledge_root)
    data_path = Path(data_root)
    projects: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, root in enumerate(project_roots):
        project, project_warnings = _read_project(
            Path(root),
            index=index,
            knowledge_root=knowledge_path,
        )
        if project["id"] in seen:
            project["source_state"] = "invalid"
            project_warnings.append(
                _project_warning(
                    "DUPLICATE_PROJECT_ID",
                    project["id"],
                    "重复项目 ID 已标为无效。",
                )
            )
        seen.add(project["id"])
        projects.append(project)
        warnings.extend(project_warnings)
    knowledge, health, knowledge_warnings = _knowledge_snapshot(knowledge_path, data_path)
    warnings.extend(knowledge_warnings)
    active_projects = sum(
        project["run"]["active"] is True
        and project["run"]["status"]
        in {"aligning", "planned", "implementing", "validating", "ready_for_manager"}
        for project in projects
    )
    pending_acceptance = sum(
        max(0, int(project["acceptance"]["total"]) - int(project["acceptance"]["passed"]))
        for project in projects
        if project["acceptance"]["state"] == "available"
    )
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "mode": "live",
        "summary": {
            "active_projects": active_projects,
            "pending_acceptance": pending_acceptance,
            "candidates": int(knowledge["candidate"]),
            "published": int(knowledge["published"]),
        },
        "projects": projects,
        "knowledge": knowledge,
        "manager_queue": _manager_queue(projects, knowledge, health),
        "health": health,
        "warnings": warnings,
    }
    _assert_redacted(snapshot)
    return snapshot


def _assert_redacted(value: Any, *, key: str = "") -> None:
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized_key in {"runid", "sessionid", "turnid", "threadid"}:
        raise DashboardError("FORBIDDEN_FIELD")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise DashboardError("INVALID_SNAPSHOT")
            _assert_redacted(child, key=child_key)
    elif isinstance(value, list):
        for child in value:
            _assert_redacted(child, key=key)
    elif isinstance(value, str):
        if _looks_like_absolute_path(value):
            raise DashboardError("ABSOLUTE_PATH_REDACTED")


def _validate_demo_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "generated_at",
        "mode",
        "summary",
        "projects",
        "knowledge",
        "manager_queue",
        "health",
        "warnings",
    }
    if set(snapshot) != required or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise DashboardError("INVALID_DEMO")
    if snapshot.get("mode") != "demo":
        raise DashboardError("INVALID_DEMO")
    _assert_redacted(snapshot)
    return snapshot


def load_demo_snapshot(asset_root: Path | str | None = None) -> dict[str, Any]:
    root = (
        Path(asset_root)
        if asset_root is not None
        else Path(__file__).resolve().parents[1] / "assets" / "dashboard"
    )
    snapshot = _read_json(
        root / "synthetic-dashboard.v1.json",
        root=root,
        maximum=MAX_DEMO_BYTES,
    )
    return _validate_demo_snapshot(snapshot)


def validate_bind_host(host: str) -> str:
    if host not in {"127.0.0.1", "::1"}:
        raise DashboardError("LOOPBACK_REQUIRED")
    return host


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        snapshot_provider: Callable[[], dict[str, Any]],
        asset_root: Path,
    ):
        self.snapshot_provider = snapshot_provider
        self.asset_root = Path(os.path.abspath(asset_root))
        super().__init__(address, handler)
        host, port = self.server_address[:2]
        self.expected_authority = _authority(str(host), int(port))


class IPv6DashboardHTTPServer(DashboardHTTPServer):
    address_family = socket.AF_INET6


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "OPCDashboard/1"
    sys_version = ""

    @property
    def dashboard_server(self) -> DashboardHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host", [])
        return len(values) == 1 and values[0] == self.dashboard_server.expected_authority

    def _valid_origin(self) -> bool:
        values = self.headers.get_all("Origin", [])
        expected = f"http://{self.dashboard_server.expected_authority}"
        return not values or (len(values) == 1 and values[0] == expected)

    def _send(
        self,
        status_code: int,
        payload: bytes,
        content_type: str,
        *,
        head_only: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _json_error(self, status_code: int, code: str, *, head_only: bool = False) -> None:
        payload = json.dumps({"error": code}, separators=(",", ":")).encode("utf-8")
        self._send(status_code, payload, "application/json; charset=utf-8", head_only=head_only)

    def _handle_read(self, *, head_only: bool) -> None:
        if not self._valid_host():
            self._json_error(400, "INVALID_HOST", head_only=head_only)
            return
        if not self._valid_origin():
            self._json_error(403, "ORIGIN_FORBIDDEN", head_only=head_only)
            return
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._json_error(404, "NOT_FOUND", head_only=head_only)
            return
        if parsed.path == "/api/snapshot":
            try:
                snapshot = self.dashboard_server.snapshot_provider()
                _assert_redacted(snapshot)
                payload = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except Exception:
                self._json_error(503, "SNAPSHOT_UNAVAILABLE", head_only=head_only)
                return
            self._send(
                200,
                payload,
                "application/json; charset=utf-8",
                head_only=head_only,
            )
            return
        route = ASSET_ROUTES.get(parsed.path)
        if route is None:
            self._json_error(404, "NOT_FOUND", head_only=head_only)
            return
        filename, content_type = route
        try:
            payload = _read_stable_bytes(
                self.dashboard_server.asset_root / filename,
                root=self.dashboard_server.asset_root,
                maximum=2 * 1024 * 1024,
                label="dashboard_asset",
            )
        except DashboardError:
            self._json_error(404, "ASSET_UNAVAILABLE", head_only=head_only)
            return
        self._send(200, payload, content_type, head_only=head_only)

    def do_GET(self) -> None:
        self._handle_read(head_only=False)

    def do_HEAD(self) -> None:
        self._handle_read(head_only=True)

    def _method_not_allowed(self) -> None:
        if not self._valid_host():
            self._json_error(400, "INVALID_HOST")
            return
        if not self._valid_origin():
            self._json_error(403, "ORIGIN_FORBIDDEN")
            return
        payload = json.dumps(
            {"error": "METHOD_NOT_ALLOWED"},
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(
            405,
            payload,
            "application/json; charset=utf-8",
            extra_headers={"Allow": "GET, HEAD"},
        )

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_TRACE = _method_not_allowed


def create_server(
    *,
    host: str,
    port: int,
    snapshot_provider: Callable[[], dict[str, Any]],
    asset_root: Path | str | None = None,
) -> DashboardHTTPServer:
    validate_bind_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise DashboardError("INVALID_PORT")
    root = (
        Path(asset_root)
        if asset_root is not None
        else Path(__file__).resolve().parents[1] / "assets" / "dashboard"
    )
    server_type = IPv6DashboardHTTPServer if host == "::1" else DashboardHTTPServer
    try:
        return server_type(
            (host, port),
            DashboardRequestHandler,
            snapshot_provider=snapshot_provider,
            asset_root=root,
        )
    except OSError as exc:
        raise DashboardError("PORT_UNAVAILABLE") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", action="append", default=[])
    parser.add_argument("--knowledge-root")
    parser.add_argument("--data-root")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--host", "--bind", dest="host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8569)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_bind_host(args.host)
        if args.demo:
            if args.project_root or args.knowledge_root or args.data_root:
                parser.error("--demo 不能与真实数据根参数同时使用")
            provider = load_demo_snapshot
        else:
            if not args.project_root:
                parser.error("真实模式需要至少一个 --project-root")
            knowledge_root = resolve_knowledge_root(args.knowledge_root)
            data_root = resolve_data_root(args.data_root)

            def provider() -> dict[str, Any]:
                return aggregate_snapshot(
                    args.project_root,
                    knowledge_root=knowledge_root,
                    data_root=data_root,
                )

        server = create_server(
            host=args.host,
            port=args.port,
            snapshot_provider=provider,
        )
    except DashboardError as exc:
        print(f"OPC_DASHBOARD_ERROR: {exc.code}", file=sys.stderr)
        return 2
    url = f"http://{_authority(args.host, int(server.server_address[1]))}/"
    print(f"OPC Dashboard: {url}", flush=True)
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
