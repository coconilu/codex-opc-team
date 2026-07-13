#!/usr/bin/env python3
"""Portable OPC organizational memory with optional Mem0 recall.

Files (and their Git history, when present) are always authoritative.  Mem0 is
an optional recall index: the module is imported lazily, every hit is checked
against its source file, and failures fall back to deterministic file search.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4


SCHEMA_VERSION = 1
INDEX_STATE_VERSION = 1
MEMORY_STATUSES = ("candidate", "approved", "rejected", "obsolete")
STATUS_DIRS = {
    "candidate": "experiences/candidates",
    "approved": "experiences/approved",
    "rejected": "experiences/rejected",
    "obsolete": "experiences/obsolete",
}
AUTHORITATIVE_KNOWLEDGE_PREFIXES = (
    "catalog.json",
    "company/",
    "schemas/",
    "experiences/approved/",
    "experiences/rejected/",
    "experiences/obsolete/",
    "evaluations/",
    "promotions/",
)
DEFAULT_TIMEOUT_SECONDS = 3.0
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class OpcMemoryError(RuntimeError):
    """A user-actionable memory error."""


class StaleSourceError(OpcMemoryError):
    """A recall hit no longer matches its authoritative source."""


class ProviderTimeout(OpcMemoryError):
    """The optional recall provider exceeded its latency budget."""


class RecallProvider(Protocol):
    """Small adapter boundary for optional semantic recall providers."""

    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        ...

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        ...


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return true when either resolved path contains the other."""
    left = left.expanduser().resolve()
    right = right.expanduser().resolve()
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


def validate_private_root_against_plugin(root: Path, *, label: str) -> None:
    resolved = root.expanduser().resolve()
    if _paths_overlap(resolved, PLUGIN_ROOT):
        raise OpcMemoryError(
            f"ROOT_ISOLATION_ERROR: {label} ({resolved}) must not overlap the "
            f"installed plugin tree ({PLUGIN_ROOT})"
        )


def validate_root_isolation(knowledge_root: Path, data_root: Path) -> None:
    """Keep canonical knowledge, derived private data, and plugin code disjoint."""
    knowledge = knowledge_root.expanduser().resolve()
    data = data_root.expanduser().resolve()
    if _paths_overlap(knowledge, data):
        raise OpcMemoryError(
            "ROOT_ISOLATION_ERROR: knowledge_root and data_root must be separate, "
            f"non-overlapping directories ({knowledge}; {data})"
        )
    validate_private_root_against_plugin(knowledge, label="knowledge_root")
    validate_private_root_against_plugin(data, label="data_root")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_knowledge_root(value: str | None = None) -> Path:
    configured = value or os.environ.get("OPC_KNOWLEDGE_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / "opc-knowledge").resolve()
    )


def resolve_data_root(value: str | None = None) -> Path:
    configured = value or os.environ.get("OPC_MEMORY_DATA_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    plugin_data = os.environ.get("PLUGIN_DATA")
    if plugin_data:
        return (Path(plugin_data).expanduser().resolve() / "opc-memory")
    return (Path.home() / ".codex-opc-team" / "opc-memory").resolve()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OpcMemoryError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OpcMemoryError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OpcMemoryError(f"Expected a JSON object in {path}")
    return value


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False) + "\n")


def safe_record_id(value: str) -> str:
    if not value or Path(value).name != value or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise OpcMemoryError(f"Invalid record id: {value}")
    return value


def _normalized_keywords(values: Sequence[str] | None) -> list[str]:
    return sorted({value.strip().lower() for value in values or [] if value.strip()})


def _json_scalar_or_text(value: str) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, (dict, list)):
        return value
    return parsed


def parse_pairs(values: Sequence[str] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for pair in values or []:
        if "=" not in pair:
            raise OpcMemoryError(f"Expected KEY=VALUE, got: {pair}")
        key, value = pair.split("=", 1)
        if not key.strip() or not value.strip():
            raise OpcMemoryError(f"KEY and VALUE must be non-empty: {pair}")
        result[key.strip()] = _json_scalar_or_text(value.strip())
    return result


def _reject_machine_paths(value: Any, label: str) -> None:
    """Reject absolute path references in structured portable metadata."""
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_machine_paths(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_machine_paths(item, f"{label}[{index}]")
        return
    if not isinstance(value, str) or "://" in value:
        return
    if Path(value).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
        raise OpcMemoryError(f"{label} must use a portable relative reference, not: {value}")


def redact_error(error: Exception) -> str:
    """Keep actionable provider errors without persisting common credential forms."""
    text = str(error)[:2000]
    patterns = (
        (r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_API_KEY]"),
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    for name, secret in os.environ.items():
        if (
            len(secret) >= 8
            and any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        ):
            text = text.replace(secret, "[REDACTED_ENV_SECRET]")
    return text[:500]


def sha256_file(path: Path) -> str:
    """Hash text sources canonically across Git LF/CRLF checkout policies."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line.replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value.replace(b"\r\n", b"\n")).hexdigest()


def _git(
    cwd: Path, args: Sequence[str], *, binary: bool = False, timeout: float = 3.0
) -> bytes | str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=True,
            timeout=timeout,
            text=not binary,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout if binary else result.stdout.strip()


class FileGitBackend:
    """Canonical JSON-file repository with optional Git provenance checks."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        validate_private_root_against_plugin(self.root, label="knowledge_root")

    def ensure_layout(self) -> None:
        for relative in STATUS_DIRS.values():
            (self.root / relative).mkdir(parents=True, exist_ok=True)

    def _folder(self, status: str) -> Path:
        try:
            return self.root / STATUS_DIRS[status]
        except KeyError as exc:
            raise OpcMemoryError(f"Unsupported memory status: {status}") from exc

    def _path(self, status: str, record_id: str) -> Path:
        return self._folder(status) / f"{safe_record_id(record_id)}.json"

    def _locate(
        self, record_id: str, statuses: Sequence[str] = MEMORY_STATUSES
    ) -> tuple[str, Path]:
        safe_record_id(record_id)
        found = [
            (status, self._path(status, record_id))
            for status in statuses
            if self._path(status, record_id).is_file()
        ]
        if not found:
            raise OpcMemoryError(f"Memory record not found: {record_id}")
        if len(found) > 1:
            raise OpcMemoryError(f"Duplicate memory record across status folders: {record_id}")
        return found[0]

    def _with_source(self, record: dict[str, Any], path: Path) -> dict[str, Any]:
        result = dict(record)
        result["_source_path"] = path.relative_to(self.root).as_posix()
        return result

    def add_candidate(
        self,
        *,
        memory_type: str,
        summary: str,
        content: str,
        keywords: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        scope: str = "project",
        owner: str = "opc-team",
        evidence: Mapping[str, Any] | None = None,
        confidence: float = 0.5,
        project_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        if not memory_type.strip() or not summary.strip() or not content.strip():
            raise OpcMemoryError("type, summary, and content must be non-empty")
        if not 0 <= confidence <= 1:
            raise OpcMemoryError("confidence must be between 0 and 1")
        normalized_scope = scope.strip().lower()
        if not normalized_scope:
            raise OpcMemoryError("scope must be non-empty")
        if normalized_scope not in {"global", "project"}:
            raise OpcMemoryError(
                f"Unsupported scope without a dedicated identity contract: {normalized_scope}"
            )
        if project_id and not re.fullmatch(r"[A-Za-z0-9._-]+", project_id):
            raise OpcMemoryError("project_id must be portable and contain no path separators")
        if normalized_scope == "project" and not project_id:
            raise OpcMemoryError("scope=project requires a project_id")
        if normalized_scope == "global" and project_id:
            raise OpcMemoryError("scope=global must not include a project_id")
        _reject_machine_paths(metadata or {}, "metadata")
        _reject_machine_paths(evidence or {}, "evidence")
        if source:
            _reject_machine_paths(source, "source")
        self.ensure_layout()
        now = utc_now()
        record_id = f"exp-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "id": record_id,
            "type": memory_type.strip(),
            "summary": summary.strip(),
            "content": content.strip(),
            "keywords": _normalized_keywords(keywords),
            "metadata": dict(metadata or {}),
            "scope": normalized_scope,
            "owner": owner,
            "evidence": dict(evidence or {}),
            "confidence": confidence,
            "status": "candidate",
            "created_at": now,
            "updated_at": now,
        }
        if project_id:
            record["project_id"] = project_id
        if source:
            record["source"] = source
        path = self._path("candidate", record_id)
        atomic_write_json(path, record)
        return self._with_source(record, path)

    def approve(
        self, record_id: str, *, approved_by: str, validation: str
    ) -> dict[str, Any]:
        if not approved_by.strip() or not validation.strip():
            raise OpcMemoryError("approval requires approved_by and validation")
        _, source = self._locate(record_id, ("candidate",))
        record = load_json(source)
        record.update(
            {
                "status": "approved",
                "approved_by": approved_by.strip(),
                "approved_at": utc_now(),
                "validation": validation.strip(),
                "updated_at": utc_now(),
            }
        )
        destination = self._path("approved", record_id)
        if destination.exists():
            raise OpcMemoryError(f"Approved destination already exists: {destination}")
        atomic_write_json(destination, record)
        source.unlink()
        return self._with_source(record, destination)

    def reject(
        self, record_id: str, *, rejected_by: str, reason: str
    ) -> dict[str, Any]:
        if not rejected_by.strip() or not reason.strip():
            raise OpcMemoryError("rejection requires rejected_by and reason")
        _, source = self._locate(record_id, ("candidate",))
        record = load_json(source)
        record.update(
            {
                "status": "rejected",
                "rejected_by": rejected_by.strip(),
                "rejected_at": utc_now(),
                "rejection_reason": reason.strip(),
                "updated_at": utc_now(),
            }
        )
        destination = self._path("rejected", record_id)
        atomic_write_json(destination, record)
        source.unlink()
        return self._with_source(record, destination)

    def mark_obsolete(
        self, record_id: str, *, reason: str, superseded_by: str | None = None
    ) -> dict[str, Any]:
        if not reason.strip():
            raise OpcMemoryError("obsolete transition requires a reason")
        _, source = self._locate(record_id, ("approved",))
        record = load_json(source)
        record.update(
            {
                "status": "obsolete",
                "obsolete_at": utc_now(),
                "obsolete_reason": reason.strip(),
                "updated_at": utc_now(),
            }
        )
        if superseded_by:
            record["superseded_by"] = safe_record_id(superseded_by)
        destination = self._path("obsolete", record_id)
        atomic_write_json(destination, record)
        source.unlink()
        return self._with_source(record, destination)

    @staticmethod
    def _metadata_matches(record: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        actual = record.get("metadata", {})
        if not isinstance(actual, dict):
            return False
        return all(actual.get(key) == value for key, value in expected.items())

    @staticmethod
    def _score(record: Mapping[str, Any], text: str) -> float:
        if not text.strip():
            return 1.0
        query = text.strip().lower()
        searchable = "\n".join(
            [
                str(record.get("type", "")),
                str(record.get("summary", "")),
                str(record.get("content", record.get("lesson", ""))),
                " ".join(str(value) for value in record.get("keywords", [])),
                json.dumps(record.get("metadata", {}), ensure_ascii=False, sort_keys=True),
            ]
        ).lower()
        score = 6.0 if query in searchable else 0.0
        terms = {term for term in re.findall(r"[\w-]+", query, flags=re.UNICODE) if term}
        score += sum(1.0 for term in terms if term in searchable)
        keyword_set = {str(value).lower() for value in record.get("keywords", [])}
        score += sum(2.0 for term in terms if term in keyword_set)
        return score

    @staticmethod
    def _scope_matches(record: Mapping[str, Any], project_id: str | None) -> bool:
        scope = str(record.get("scope", "")).strip().lower()
        if scope == "global":
            # Fail closed for hand-edited or migrated records.  Global
            # knowledge must never retain a project identity.
            return not record.get("project_id")
        if scope == "project":
            record_project = record.get("project_id")
            return bool(
                project_id
                and isinstance(record_project, str)
                and re.fullmatch(r"[A-Za-z0-9._-]+", record_project)
                and record_project == project_id
            )
        # Organization and future scopes require a dedicated context identifier;
        # they are denied rather than silently exposed to every project.
        return False

    def record_matches(
        self,
        record: Mapping[str, Any],
        *,
        text: str = "",
        memory_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        keywords: Sequence[str] | None = None,
        project_id: str | None = None,
    ) -> bool:
        if not self._scope_matches(record, project_id):
            return False
        if memory_type and record.get("type") != memory_type:
            return False
        if metadata and not self._metadata_matches(record, metadata):
            return False
        required_keywords = set(_normalized_keywords(keywords))
        actual_keywords = {str(value).lower() for value in record.get("keywords", [])}
        if required_keywords and not required_keywords.issubset(actual_keywords):
            return False
        return not text.strip() or self._score(record, text) > 0

    def query(
        self,
        text: str = "",
        *,
        approved_only: bool = True,
        memory_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        keywords: Sequence[str] | None = None,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        if project_id and not re.fullmatch(r"[A-Za-z0-9._-]+", project_id):
            raise OpcMemoryError("project_id must be portable and contain no path separators")
        statuses = ("approved",) if approved_only else MEMORY_STATUSES
        hits: list[dict[str, Any]] = []
        for status in statuses:
            for path in sorted(self._folder(status).glob("*.json")):
                record = load_json(path)
                if record.get("status") == "approved":
                    relative_source = path.relative_to(self.root).as_posix()
                    try:
                        provenance = self.source_metadata(relative_source)
                    except (OpcMemoryError, OSError):
                        continue
                    if not provenance.get("source_commit"):
                        # ``approved`` is a canonical transition state; normal
                        # recall treats it as published only after Git HEAD can
                        # verify the exact blob.
                        continue
                if not self.record_matches(
                    record,
                    text=text,
                    memory_type=memory_type,
                    metadata=metadata,
                    keywords=keywords,
                    project_id=project_id,
                ):
                    continue
                hit = self._with_source(record, path)
                hit["_score"] = self._score(record, text)
                hit["_recall_source"] = "file"
                hits.append(hit)
        hits.sort(
            key=lambda item: (
                -float(item.get("_score", 0)),
                str(item.get("updated_at", "")),
                str(item.get("id", "")),
            )
        )
        return hits[:limit]

    def list_by_type(
        self, memory_type: str, *, approved_only: bool = True, limit: int = 100
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        statuses = ("approved",) if approved_only else MEMORY_STATUSES
        records: list[dict[str, Any]] = []
        for status in statuses:
            remaining = limit - len(records)
            records.extend(
                self.list_by_status(status, memory_type=memory_type, limit=remaining)
            )
            if len(records) >= limit:
                break
        return records

    def list_by_status(
        self,
        status: str,
        *,
        memory_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status not in MEMORY_STATUSES:
            raise OpcMemoryError(f"Unsupported memory status: {status}")
        records: list[dict[str, Any]] = []
        for path in sorted(self._folder(status).glob("*.json")):
            record = load_json(path)
            if memory_type and record.get("type") != memory_type:
                continue
            records.append(self._with_source(record, path))
            if len(records) >= limit:
                break
        return records

    def export_decision_context(
        self,
        query: str = "",
        *,
        memory_type: str | None = "decision",
        project_id: str | None = None,
        limit: int = 20,
    ) -> str:
        records = self.query(
            query,
            approved_only=True,
            memory_type=memory_type,
            project_id=project_id,
            limit=limit,
        )
        lines = ["# OPC decision context", ""]
        if not records:
            lines.append("No approved decision memory matched the request.")
            return "\n".join(lines) + "\n"
        for record in records:
            lines.extend(
                [
                    f"## {record['summary']}",
                    "",
                    str(record.get("content", record.get("lesson", ""))),
                    "",
                    f"- ID: `{record['id']}`",
                    f"- Type: `{record['type']}`",
                    f"- Source: `{record['_source_path']}`",
                ]
            )
            if record.get("keywords"):
                lines.append("- Keywords: " + ", ".join(record["keywords"]))
            if record.get("validation"):
                lines.append(f"- Validation: {record['validation']}")
            lines.append("")
        return "\n".join(lines)

    def _resolve_source(self, source_path: str) -> Path:
        if not source_path or Path(source_path).is_absolute():
            raise StaleSourceError("Recall source_path must be relative to the knowledge root")
        candidate = (self.root / Path(source_path)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise StaleSourceError("Recall source_path escapes the knowledge root") from exc
        return candidate

    def source_commit(self, path: Path, content_hash: str) -> str | None:
        top_text = _git(self.root, ("rev-parse", "--show-toplevel"))
        head = _git(self.root, ("rev-parse", "HEAD"))
        if not isinstance(top_text, str) or not isinstance(head, str) or not head:
            return None
        top = Path(top_text).resolve()
        try:
            relative = path.resolve().relative_to(top).as_posix()
        except ValueError:
            return None
        committed = _git(top, ("show", f"{head}:{relative}"), binary=True)
        if not isinstance(committed, bytes):
            return None
        return head if sha256_bytes(committed) == content_hash else None

    def source_metadata(self, source_path: str) -> dict[str, Any]:
        path = self._resolve_source(source_path)
        if not path.is_file():
            raise StaleSourceError(f"Authoritative source is missing: {source_path}")
        content_hash = sha256_file(path)
        return {
            "source_path": source_path,
            "content_hash": content_hash,
            "source_commit": self.source_commit(path, content_hash),
        }

    def read_authoritative(
        self,
        *,
        source_path: str,
        content_hash: str,
        source_commit: str | None = None,
        approved_only: bool = True,
    ) -> dict[str, Any]:
        path = self._resolve_source(source_path)
        if not path.is_file():
            raise StaleSourceError(f"Authoritative source is missing: {source_path}")
        actual_hash = sha256_file(path)
        if not content_hash or actual_hash != content_hash:
            raise StaleSourceError(f"Authoritative source hash changed: {source_path}")
        if source_commit:
            top_text = _git(self.root, ("rev-parse", "--show-toplevel"))
            if not isinstance(top_text, str):
                raise StaleSourceError("Cannot verify source_commit outside a Git repository")
            top = Path(top_text).resolve()
            try:
                relative = path.relative_to(top).as_posix()
            except ValueError as exc:
                raise StaleSourceError("Authoritative source is outside the Git repository") from exc
            committed = _git(top, ("show", f"{source_commit}:{relative}"), binary=True)
            if not isinstance(committed, bytes):
                raise StaleSourceError(f"Cannot resolve source_commit: {source_commit}")
            if sha256_bytes(committed) != content_hash:
                raise StaleSourceError("source_commit does not contain the indexed source content")
        record = load_json(path)
        if approved_only and record.get("status") != "approved":
            raise StaleSourceError("Recall source is no longer approved")
        return self._with_source(record, path)

    @staticmethod
    def _is_authoritative_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("./")
        return any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in AUTHORITATIVE_KNOWLEDGE_PREFIXES
        )

    def git_audit(self) -> dict[str, Any]:
        """Report Git provenance without staging, committing, or changing files."""
        top_text = _git(self.root, ("rev-parse", "--show-toplevel"))
        if not isinstance(top_text, str) or not top_text:
            return {
                "is_repo": False,
                "repo_root": None,
                "root_is_repo_root": False,
                "head": None,
                "branch": None,
                "provenance_ready": False,
                "dirty": False,
                "dirty_paths": [],
                "staged": [],
                "unstaged": [],
                "untracked": [],
                "authoritative_uncommitted": [],
                "warning_codes": ["KNOWLEDGE_NOT_GIT"],
            }

        repo_root = Path(top_text).expanduser().resolve()
        head = _git(self.root, ("rev-parse", "--verify", "HEAD"))
        branch = _git(self.root, ("symbolic-ref", "--quiet", "--short", "HEAD"))
        porcelain = _git(
            self.root,
            (
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
            ),
        )
        lines = porcelain.splitlines() if isinstance(porcelain, str) else []
        dirty_paths: list[str] = []
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        for line in lines:
            if len(line) < 4:
                continue
            index_state, worktree_state = line[0], line[1]
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[1]
            path = path.replace("\\", "/")
            dirty_paths.append(path)
            if index_state == "?" and worktree_state == "?":
                untracked.append(path)
                continue
            if index_state != " ":
                staged.append(path)
            if worktree_state != " ":
                unstaged.append(path)

        authoritative = [
            path for path in dirty_paths if self._is_authoritative_path(path)
        ]
        warning_codes: list[str] = []
        if repo_root != self.root:
            warning_codes.append("KNOWLEDGE_ROOT_NOT_REPO_ROOT")
        if not isinstance(head, str) or not head:
            warning_codes.append("GIT_HEAD_MISSING")
        if authoritative:
            warning_codes.append("UNCOMMITTED_KNOWLEDGE")
        provenance_ready = bool(repo_root == self.root and isinstance(head, str) and head)
        return {
            "is_repo": True,
            "repo_root": str(repo_root),
            "root_is_repo_root": repo_root == self.root,
            "head": head if isinstance(head, str) and head else None,
            "branch": branch if isinstance(branch, str) and branch else None,
            "provenance_ready": provenance_ready,
            "dirty": bool(dirty_paths),
            "dirty_paths": dirty_paths,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "authoritative_uncommitted": authoritative,
            "warning_codes": warning_codes,
        }

    def doctor(self) -> dict[str, Any]:
        required = (
            "catalog.json",
            "company/charter.md",
            "company/knowledge-policy.md",
            "company/principles.md",
            "schemas/experience.schema.json",
            "schemas/run.schema.json",
        )
        missing = [relative for relative in required if not (self.root / relative).is_file()]
        invalid: list[str] = []
        counts: dict[str, int] = {}
        seen: dict[str, str] = {}
        for status in MEMORY_STATUSES:
            paths = sorted(self._folder(status).glob("*.json"))
            counts[status] = len(paths)
            for path in paths:
                try:
                    record = load_json(path)
                    record_id = safe_record_id(str(record.get("id", "")))
                    if record.get("status") != status:
                        invalid.append(f"Status mismatch in {path}")
                    scope = str(record.get("scope", "")).strip().lower()
                    record_project = record.get("project_id")
                    if scope == "global" and record_project:
                        invalid.append(f"Global record must not include project_id: {path}")
                    elif scope == "project" and (
                        not isinstance(record_project, str)
                        or not re.fullmatch(r"[A-Za-z0-9._-]+", record_project)
                    ):
                        invalid.append(f"Project record requires a valid project_id: {path}")
                    elif scope not in {"global", "project"}:
                        invalid.append(f"Unsupported memory scope in {path}: {scope}")
                    previous = seen.get(record_id)
                    if previous:
                        invalid.append(f"Duplicate id {record_id}: {previous}, {path}")
                    seen[record_id] = str(path)
                except OpcMemoryError as exc:
                    invalid.append(str(exc))
        git_report = self.git_audit()
        return {
            "ok": self.root.is_dir() and not missing and not invalid,
            "knowledge_root": str(self.root),
            "state": (
                "NOT_INITIALIZED"
                if not self.root.is_dir()
                else ("READY" if not missing and not invalid else "INVALID")
            ),
            "exists": self.root.is_dir(),
            "missing": missing,
            "counts": counts,
            "invalid": invalid,
            "git": git_report,
            "provenance_ready": git_report["provenance_ready"],
            "warnings": list(git_report["warning_codes"]),
        }


class Mem0Provider:
    """Lazy adapter for the mem0ai 2.x OSS Python library.

    ``Memory()`` may use OpenAI-backed defaults and therefore is not claimed to
    be fully local.  Installation, API-key setup, and provider selection remain
    explicit user responsibilities.
    """

    _import_lock = threading.Lock()

    def __init__(
        self,
        *,
        user_id: str | None = None,
        data_root: Path | str | None = None,
        importer: Callable[[str], Any] | None = None,
        client_factory: Callable[[Any, Mapping[str, Any]], Any] | None = None,
    ):
        self.user_id = user_id or f"opc-ephemeral-{uuid4().hex}"
        self.data_root = (
            Path(data_root).expanduser().resolve()
            if data_root is not None
            else resolve_data_root()
        )
        validate_private_root_against_plugin(self.data_root, label="data_root")
        self.mem0_root = self.data_root / "mem0"
        self._importer = importer or importlib.import_module
        self._client_factory = client_factory
        self._client: Any | None = None

    @staticmethod
    def installed() -> bool:
        try:
            return importlib.util.find_spec("mem0") is not None
        except (ImportError, AttributeError, ValueError):
            return False

    @staticmethod
    def package_version() -> str | None:
        try:
            return importlib_metadata.version("mem0ai")
        except importlib_metadata.PackageNotFoundError:
            return None

    @classmethod
    def supported_version(cls) -> bool | None:
        version = cls.package_version()
        if version is None:
            return None
        return version == "2.0.11"

    def _private_config(self) -> dict[str, Any]:
        namespace = hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:20]
        return {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": f"opc_{namespace}",
                    "path": str(self.mem0_root / "qdrant"),
                    "on_disk": True,
                },
            },
            "history_db_path": str(self.mem0_root / "history.db"),
        }

    def _assert_private_module(self, module: Any) -> None:
        if getattr(module, "__name__", None) != "mem0":
            return
        setup_module = sys.modules.get("mem0.memory.setup")
        configured_dir = getattr(setup_module, "mem0_dir", None)
        if configured_dir and Path(configured_dir).expanduser().resolve() != self.mem0_root:
            raise OpcMemoryError(
                "Mem0 was already imported with storage outside OPC private data_root; use an isolated OPC process."
            )
        telemetry_module = sys.modules.get("mem0.memory.telemetry")
        if bool(getattr(telemetry_module, "MEM0_TELEMETRY", False)):
            raise OpcMemoryError(
                "Mem0 telemetry was already enabled before OPC initialization; use an isolated OPC process."
            )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        self.mem0_root.mkdir(parents=True, exist_ok=True)
        with self._import_lock:
            previous_dir = os.environ.get("MEM0_DIR")
            previous_telemetry = os.environ.get("MEM0_TELEMETRY")
            os.environ["MEM0_DIR"] = str(self.mem0_root)
            os.environ["MEM0_TELEMETRY"] = "False"
            try:
                module = self._importer("mem0")
                self._assert_private_module(module)
                config = self._private_config()
                if self._client_factory:
                    self._client = self._client_factory(module, config)
                else:
                    memory_class = getattr(module, "Memory", None)
                    if memory_class is None:
                        raise OpcMemoryError("Installed mem0 module does not expose Memory")
                    from_config = getattr(memory_class, "from_config", None)
                    if not callable(from_config):
                        raise OpcMemoryError("Installed mem0 Memory does not expose from_config")
                    self._client = from_config(config)
                self._assert_private_module(module)
            finally:
                if previous_dir is None:
                    os.environ.pop("MEM0_DIR", None)
                else:
                    os.environ["MEM0_DIR"] = previous_dir
                if previous_telemetry is None:
                    os.environ.pop("MEM0_TELEMETRY", None)
                else:
                    os.environ["MEM0_TELEMETRY"] = previous_telemetry
        return self._client

    def add(self, text: str, metadata: Mapping[str, Any]) -> Any:
        return self._get_client().add(
            text,
            user_id=self.user_id,
            metadata=dict(metadata),
            infer=False,
        )

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        response = self._get_client().search(
            query=query,
            top_k=limit,
            filters={"user_id": self.user_id},
        )
        if isinstance(response, dict):
            items = response.get("results", response.get("memories", []))
        else:
            items = response
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]


def _call_with_timeout(call: Callable[[], Any], timeout_seconds: float) -> Any:
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, call()))
        except Exception as exc:  # provider failures must never break File/Git
            result_queue.put((False, exc))

    thread = threading.Thread(target=invoke, daemon=True, name="opc-memory-provider")
    thread.start()
    try:
        ok, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise ProviderTimeout(
            f"Optional recall provider timed out after {timeout_seconds:.1f}s"
        ) from exc
    if not ok:
        raise value
    return value


def load_config(data_root: Path) -> dict[str, Any]:
    path = data_root / "config.json"
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "installation_id": None,
            "mem0": {"enabled": False, "user_id": None},
        }
    config = load_json(path)
    mem0 = config.setdefault("mem0", {})
    if not isinstance(mem0, dict):
        raise OpcMemoryError(f"mem0 config must be an object: {path}")
    mem0.setdefault("enabled", False)
    mem0.setdefault("user_id", None)
    config.setdefault("installation_id", None)
    return config


def ensure_anonymous_identity(config: dict[str, Any]) -> bool:
    """Persist a stable random identity without using names or email addresses."""
    changed = False
    try:
        installation_id = str(UUID(str(config.get("installation_id"))))
    except (ValueError, AttributeError, TypeError):
        installation_id = str(uuid4())
    if config.get("installation_id") != installation_id:
        config["installation_id"] = installation_id
        changed = True
    mem0 = config.setdefault("mem0", {})
    if not isinstance(mem0, dict):
        mem0 = {"enabled": False}
        config["mem0"] = mem0
        changed = True
    current_user_id = mem0.get("user_id")
    if not current_user_id or current_user_id == "codex-opc-team":
        mem0["user_id"] = f"opc-{installation_id}"
        changed = True
    return changed


def write_config(data_root: Path, config: Mapping[str, Any]) -> None:
    validate_private_root_against_plugin(
        data_root.expanduser().resolve(), label="data_root"
    )
    atomic_write_json(data_root / "config.json", config)


class MemoryService:
    """Stable memory API combining canonical File/Git and optional recall."""

    def __init__(
        self,
        backend: FileGitBackend,
        *,
        data_root: Path | str,
        mem0_enabled: bool = False,
        provider: RecallProvider | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        installation_id: str | None = None,
    ):
        self.backend = backend
        self.data_root = Path(data_root).expanduser().resolve()
        validate_root_isolation(self.backend.root, self.data_root)
        self.mem0_enabled = bool(mem0_enabled)
        self.provider = provider or Mem0Provider(data_root=self.data_root)
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        try:
            self.identity_configured = bool(UUID(str(installation_id)))
        except (ValueError, AttributeError, TypeError):
            self.identity_configured = False

    @classmethod
    def from_paths(
        cls,
        knowledge_root: Path | str,
        data_root: Path | str,
        *,
        provider: RecallProvider | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> "MemoryService":
        knowledge_path = Path(knowledge_root).expanduser().resolve()
        data_path = Path(data_root).expanduser().resolve()
        validate_root_isolation(knowledge_path, data_path)
        config = load_config(data_path)
        mem0_config = config.get("mem0", {})
        selected_provider = provider or Mem0Provider(
            user_id=(
                str(mem0_config["user_id"])
                if mem0_config.get("user_id")
                else None
            ),
            data_root=data_path,
        )
        return cls(
            FileGitBackend(knowledge_path),
            data_root=data_path,
            mem0_enabled=bool(mem0_config.get("enabled", False)),
            provider=selected_provider,
            timeout_seconds=timeout_seconds,
            installation_id=(
                str(config["installation_id"])
                if config.get("installation_id")
                else None
            ),
        )

    @property
    def outbox_path(self) -> Path:
        return self.data_root / "outbox.jsonl"

    @property
    def index_state_path(self) -> Path:
        return self.data_root / "index-state.json"

    def _provider_namespace(self) -> str:
        if isinstance(self.provider, Mem0Provider):
            raw = f"mem0:{self.provider.user_id}"
        else:
            provider_type = type(self.provider)
            raw = f"custom:{provider_type.__module__}.{provider_type.__qualname__}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _empty_index_state(self) -> dict[str, Any]:
        return {
            "schema_version": INDEX_STATE_VERSION,
            "namespaces": {},
            "updated_at": utc_now(),
        }

    def _load_index_state(self) -> dict[str, Any]:
        if not self.index_state_path.exists():
            return self._empty_index_state()
        state = load_json(self.index_state_path)
        if state.get("schema_version") != INDEX_STATE_VERSION:
            raise OpcMemoryError(
                f"Unsupported derived index state version: {self.index_state_path}"
            )
        namespaces = state.get("namespaces")
        if not isinstance(namespaces, dict):
            raise OpcMemoryError(
                f"Derived index state namespaces must be an object: {self.index_state_path}"
            )
        for namespace, bucket in namespaces.items():
            if not isinstance(namespace, str) or not isinstance(bucket, dict):
                raise OpcMemoryError(
                    f"Invalid provider namespace in derived index state: {self.index_state_path}"
                )
            if not isinstance(bucket.get("records", {}), dict):
                raise OpcMemoryError(
                    f"Derived index state records must be an object: {self.index_state_path}"
                )
        return state

    def _namespace_records(
        self, state: dict[str, Any], *, create: bool
    ) -> dict[str, Any]:
        namespace = self._provider_namespace()
        namespaces = state["namespaces"]
        bucket = namespaces.get(namespace)
        if bucket is None:
            if not create:
                return {}
            bucket = {
                "provider": "mem0" if isinstance(self.provider, Mem0Provider) else "custom",
                "records": {},
                "updated_at": utc_now(),
            }
            namespaces[namespace] = bucket
        records = bucket.get("records")
        if not isinstance(records, dict):
            raise OpcMemoryError("Derived index state records must be an object")
        return records

    @staticmethod
    def _state_entry_matches(
        entry: Mapping[str, Any] | None, metadata: Mapping[str, Any]
    ) -> bool:
        if not isinstance(entry, Mapping):
            return False
        return all(
            entry.get(key) == metadata.get(key)
            for key in ("source_path", "content_hash", "source_commit")
        )

    def _record_index_state(self, metadata: Mapping[str, Any]) -> None:
        state = self._load_index_state()
        records = self._namespace_records(state, create=True)
        record_id = safe_record_id(str(metadata["record_id"]))
        records[record_id] = {
            "source_path": metadata["source_path"],
            "content_hash": metadata["content_hash"],
            "source_commit": metadata.get("source_commit"),
            "indexed_at": utc_now(),
        }
        namespace = self._provider_namespace()
        state["namespaces"][namespace]["updated_at"] = utc_now()
        state["updated_at"] = utc_now()
        atomic_write_json(self.index_state_path, state)

    def _queue_outbox(
        self, operation: str, payload: Mapping[str, Any], error: Exception
    ) -> None:
        append_jsonl(
            self.outbox_path,
            {
                "schema_version": SCHEMA_VERSION,
                "id": f"outbox-{uuid4().hex}",
                "operation": operation,
                "payload": dict(payload),
                "error_type": type(error).__name__,
                "error": redact_error(error),
                "created_at": utc_now(),
            },
        )

    @staticmethod
    def _index_text(record: Mapping[str, Any]) -> str:
        return "\n\n".join(
            [str(record.get("summary", "")), str(record.get("content", ""))]
        ).strip()

    def _index_metadata(self, record: Mapping[str, Any]) -> dict[str, Any]:
        source = self.backend.source_metadata(str(record["_source_path"]))
        return {
            "record_id": record["id"],
            "type": record["type"],
            "status": record["status"],
            "scope": record.get("scope"),
            "project_id": record.get("project_id"),
            "keywords": list(record.get("keywords", [])),
            **source,
        }

    def _sync_approved_detail(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not self.mem0_enabled:
            return {"status": "disabled", "record_id": record["id"]}
        if isinstance(self.provider, Mem0Provider) and self.provider.supported_version() is False:
            return {
                "status": "unsupported_version",
                "record_id": record["id"],
                "note": "Canonical File/Git approval succeeded; optional recall requires mem0ai 2.0.11.",
            }
        try:
            metadata = self._index_metadata(record)
            _call_with_timeout(
                lambda: self.provider.add(self._index_text(record), metadata),
                self.timeout_seconds,
            )
        except Exception as exc:
            payload: dict[str, Any] = {"record_id": record["id"]}
            if "metadata" in locals():
                payload.update(metadata)
            self._queue_outbox(
                "upsert",
                payload,
                exc,
            )
            return {
                "status": "outbox",
                "record_id": record["id"],
                "error_type": type(exc).__name__,
                "error": redact_error(exc),
            }
        try:
            self._record_index_state(metadata)
        except Exception as exc:
            self._queue_outbox(
                "index_state_reconcile",
                {"record_id": record["id"], **metadata},
                exc,
            )
            return {
                "status": "state_outbox",
                "record_id": record["id"],
                "error_type": type(exc).__name__,
                "error": redact_error(exc),
            }
        return {
            "status": "indexed",
            "record_id": record["id"],
            "source_path": metadata["source_path"],
            "content_hash": metadata["content_hash"],
            "source_commit": metadata.get("source_commit"),
        }

    def _sync_approved(self, record: Mapping[str, Any]) -> str:
        return str(self._sync_approved_detail(record)["status"])

    def add_candidate(self, **values: Any) -> dict[str, Any]:
        return self.backend.add_candidate(**values)

    def approve(
        self, record_id: str, *, approved_by: str, validation: str
    ) -> dict[str, Any]:
        record = self.backend.approve(
            record_id, approved_by=approved_by, validation=validation
        )
        result = dict(record)
        # Approval is a canonical File/Git transition only.  The curator must
        # commit the exact transition paths before an explicit reindex may
        # write the optional derived provider index.
        result["_recall_sync"] = (
            "pending_commit" if self.mem0_enabled else "disabled"
        )
        return result

    def reject(
        self, record_id: str, *, rejected_by: str, reason: str
    ) -> dict[str, Any]:
        return self.backend.reject(record_id, rejected_by=rejected_by, reason=reason)

    def mark_obsolete(
        self, record_id: str, *, reason: str, superseded_by: str | None = None
    ) -> dict[str, Any]:
        return self.backend.mark_obsolete(
            record_id, reason=reason, superseded_by=superseded_by
        )

    @staticmethod
    def _hit_metadata(hit: Mapping[str, Any]) -> dict[str, Any]:
        metadata = hit.get("metadata", {})
        return dict(metadata) if isinstance(metadata, dict) else {}

    def query(
        self,
        text: str = "",
        *,
        approved_only: bool = True,
        memory_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        keywords: Sequence[str] | None = None,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        file_hits = self.backend.query(
            text,
            approved_only=approved_only,
            memory_type=memory_type,
            metadata=metadata,
            keywords=keywords,
            project_id=project_id,
            limit=limit,
        )
        if not self.mem0_enabled or not text.strip() or limit < 1:
            return file_hits
        if isinstance(self.provider, Mem0Provider) and self.provider.supported_version() is False:
            return file_hits
        semantic: list[dict[str, Any]] = []
        try:
            provider_hits = _call_with_timeout(
                lambda: self.provider.search(text, limit), self.timeout_seconds
            )
            if not isinstance(provider_hits, list):
                raise OpcMemoryError("Optional recall provider returned a non-list result")
            for hit in provider_hits:
                if not isinstance(hit, dict):
                    continue
                provenance = self._hit_metadata(hit)
                source_commit = provenance.get("source_commit")
                if not isinstance(source_commit, str) or not source_commit:
                    continue
                try:
                    record = self.backend.read_authoritative(
                        source_path=str(provenance.get("source_path", "")),
                        content_hash=str(provenance.get("content_hash", "")),
                        source_commit=source_commit,
                        approved_only=approved_only,
                    )
                except (OpcMemoryError, OSError):
                    continue
                try:
                    current_provenance = self.backend.source_metadata(
                        str(provenance.get("source_path", ""))
                    )
                except (OpcMemoryError, OSError):
                    continue
                if (
                    not current_provenance.get("source_commit")
                    or current_provenance.get("content_hash")
                    != provenance.get("content_hash")
                ):
                    continue
                if not self.backend.record_matches(
                    record,
                    memory_type=memory_type,
                    metadata=metadata,
                    keywords=keywords,
                    project_id=project_id,
                ):
                    continue
                record["_score"] = float(hit.get("score", 0) or 0)
                record["_recall_source"] = "mem0"
                semantic.append(record)
        except Exception:
            return file_hits

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in [*semantic, *file_hits]:
            record_id = str(record.get("id", ""))
            if record_id and record_id not in seen:
                seen.add(record_id)
                merged.append(record)
            if len(merged) >= limit:
                break
        return merged

    def list_by_type(
        self, memory_type: str, *, approved_only: bool = True, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.backend.list_by_type(
            memory_type, approved_only=approved_only, limit=limit
        )

    def list_by_status(
        self,
        status: str,
        *,
        memory_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.backend.list_by_status(
            status, memory_type=memory_type, limit=limit
        )

    def export_decision_context(
        self,
        query: str = "",
        *,
        memory_type: str | None = "decision",
        project_id: str | None = None,
        limit: int = 20,
    ) -> str:
        records = self.query(
            query,
            approved_only=True,
            memory_type=memory_type,
            project_id=project_id,
            limit=limit,
        )
        lines = ["# OPC decision context", ""]
        if not records:
            return "# OPC decision context\n\nNo approved decision memory matched the request.\n"
        for record in records:
            lines.extend(
                [
                    f"## {record['summary']}",
                    "",
                    str(record.get("content", "")),
                    "",
                    f"- ID: `{record['id']}`",
                    f"- Type: `{record['type']}`",
                    f"- Source: `{record['_source_path']}`",
                    "",
                ]
            )
        return "\n".join(lines)

    def status(self) -> dict[str, Any]:
        outbox_count = 0
        if self.outbox_path.is_file():
            outbox_count = sum(
                1
                for line in self.outbox_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        installed = (
            self.provider.installed()
            if isinstance(self.provider, Mem0Provider)
            else None
        )
        version = (
            self.provider.package_version()
            if isinstance(self.provider, Mem0Provider)
            else None
        )
        supported_version = (
            self.provider.supported_version()
            if isinstance(self.provider, Mem0Provider)
            else None
        )
        if not self.mem0_enabled:
            provider_health = "disabled"
        elif installed is False:
            provider_health = "unavailable-file-fallback"
        elif supported_version is False:
            provider_health = "unsupported-version-file-fallback"
        else:
            provider_health = "configured-unverified"
        venv_root = self.data_root / "venv"
        windows_python = venv_root / "Scripts" / "python.exe"
        unix_python = venv_root / "bin" / "python"
        available_venv_python = (
            windows_python if windows_python.is_file() else unix_python if unix_python.is_file() else None
        )
        active_interpreter = Path(sys.executable).expanduser().resolve()
        active_is_venv = bool(
            available_venv_python
            and active_interpreter == available_venv_python.expanduser().resolve()
        )
        rerun_hint = None
        if installed is False and available_venv_python is not None and not active_is_venv:
            rerun_hint = (
                f'Use "{available_venv_python}" to run opc_memory.py so the isolated mem0ai install is visible.'
            )
        git_report = self.backend.git_audit()
        return {
            "knowledge_root": str(self.backend.root),
            "data_root": str(self.data_root),
            "authority": "file-git",
            "knowledge_git": git_report,
            "warnings": list(git_report["warning_codes"]),
            "mem0": {
                "enabled": self.mem0_enabled,
                "installed": installed,
                "version": version,
                "supported_version": supported_version,
                "tested_version": "2.0.11",
                "health": provider_health,
                "mode": "optional-recall-index",
                "fallback": "file-keyword-search",
                "fully_local_guaranteed": False,
                "openai_api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
                "anonymous_identity_configured": self.identity_configured,
                "note": "mem0ai Memory() defaults may require OpenAI; configure it explicitly.",
            },
            "isolated_venv": {
                "root": str(venv_root),
                "windows_python": str(windows_python),
                "unix_python": str(unix_python),
                "python_exists": available_venv_python is not None,
                "active_interpreter": str(active_interpreter),
                "active_interpreter_is_venv": active_is_venv,
                "rerun_hint": rerun_hint,
            },
            "outbox_count": outbox_count,
        }

    def doctor(self) -> dict[str, Any]:
        file_report = self.backend.doctor()
        status = self.status()
        warnings: list[str] = list(file_report.get("warnings", []))
        if status["mem0"]["enabled"] and status["mem0"]["installed"] is False:
            warnings.append("Mem0 is enabled but mem0ai is not installed; File/Git fallback remains active.")
        if status["mem0"]["enabled"] and status["mem0"]["supported_version"] is False:
            warnings.append("Installed mem0ai is not the tested 2.0.11 release; File/Git fallback remains active.")
        if status["mem0"]["enabled"] and not status["mem0"]["openai_api_key_present"]:
            warnings.append(
                "Default mem0ai 2.x Memory() commonly requires OPENAI_API_KEY; use an explicit local provider config if desired."
            )
        if status["mem0"]["enabled"] and not status["mem0"]["anonymous_identity_configured"]:
            warnings.append("Mem0 is enabled without a persisted anonymous installation identity; rerun setup --apply.")
        if status["isolated_venv"]["rerun_hint"]:
            warnings.append(str(status["isolated_venv"]["rerun_hint"]))
        return {
            "ok": file_report["ok"],
            "file_git": file_report,
            "runtime": status,
            "warnings": warnings,
        }

    def _reindex_inventory(
        self, *, limit: int, force: bool
    ) -> tuple[list[dict[str, Any]], list[str]]:
        report = self.backend.doctor()
        records = self.backend.list_by_status("approved", limit=limit)
        conflicts = list(report["invalid"])
        try:
            state = self._load_index_state()
            indexed_records = self._namespace_records(state, create=False)
        except (OpcMemoryError, OSError) as exc:
            indexed_records = {}
            conflicts.append(str(exc))
        items: list[dict[str, Any]] = []
        for record in records:
            metadata = self._index_metadata(record)
            if not metadata.get("source_commit"):
                conflict = (
                    "UNCOMMITTED_APPROVED_SOURCE: "
                    f"{record['id']} ({metadata['source_path']}) has no verifiable "
                    "source_commit at Git HEAD"
                )
                conflicts.append(conflict)
                items.append({**metadata, "action": "conflict_uncommitted"})
                continue
            current = self._state_entry_matches(
                indexed_records.get(str(record["id"])), metadata
            )
            items.append(
                {
                    **metadata,
                    "action": "index" if force or not current else "skip_unchanged",
                }
            )
        return items, conflicts

    def reindex_plan(self, limit: int = 1000, *, force: bool = False) -> dict[str, Any]:
        items, conflicts = self._reindex_inventory(limit=limit, force=force)
        pending = [item for item in items if item["action"] == "index"]
        skipped = [item["record_id"] for item in items if item["action"] == "skip_unchanged"]
        return {
            "ok": not conflicts,
            "dry_run": True,
            "force": force,
            "count": len(items),
            "pending_count": len(pending),
            "skipped_count": len(skipped),
            "items": items,
            "skips": skipped,
            "conflicts": conflicts,
            "state_path": str(self.index_state_path),
            "note": "No Mem0 writes were performed.",
        }

    def reindex_apply(self, limit: int = 1000, *, force: bool = False) -> dict[str, Any]:
        if not self.mem0_enabled:
            return {
                "ok": False,
                "dry_run": False,
                "force": force,
                "performed": False,
                "reason": "MEM0_DISABLED",
                "indexed_count": 0,
                "skipped_count": 0,
                "failure_count": 0,
                "note": "Enable optional Mem0 recall explicitly before applying a rebuild.",
            }
        if isinstance(self.provider, Mem0Provider) and self.provider.supported_version() is False:
            return {
                "ok": False,
                "dry_run": False,
                "force": force,
                "performed": False,
                "reason": "MEM0_UNSUPPORTED_VERSION",
                "indexed_count": 0,
                "skipped_count": 0,
                "failure_count": 0,
                "note": "Use the tested mem0ai 2.0.11 release; File/Git remains active.",
            }
        plan = self.reindex_plan(limit, force=force)
        if not plan["ok"]:
            return {
                "ok": False,
                "dry_run": False,
                "force": force,
                "performed": False,
                "reason": "REINDEX_PLAN_INVALID",
                "conflicts": plan["conflicts"],
                "indexed_count": 0,
                "skipped_count": plan["skipped_count"],
                "failure_count": len(plan["conflicts"]),
            }

        indexed: list[str] = []
        failures: list[dict[str, Any]] = []
        for item in plan["items"]:
            if item["action"] != "index":
                continue
            try:
                record = self.backend.read_authoritative(
                    source_path=str(item["source_path"]),
                    content_hash=str(item["content_hash"]),
                    source_commit=(
                        str(item["source_commit"])
                        if item.get("source_commit")
                        else None
                    ),
                    approved_only=True,
                )
            except (OpcMemoryError, OSError) as exc:
                failures.append(
                    {
                        "record_id": item["record_id"],
                        "status": "source_invalid",
                        "error_type": type(exc).__name__,
                        "error": redact_error(exc),
                    }
                )
                continue
            detail = self._sync_approved_detail(record)
            if detail["status"] == "indexed":
                indexed.append(str(item["record_id"]))
            else:
                failures.append(detail)

        return {
            "ok": not failures,
            "dry_run": False,
            "force": force,
            "performed": bool(indexed or failures),
            "eligible_count": plan["count"],
            "indexed_count": len(indexed),
            "skipped_count": plan["skipped_count"],
            "failure_count": len(failures),
            "indexed": indexed,
            "skipped": plan["skips"],
            "failures": failures,
            "state_path": str(self.index_state_path),
            "outbox_path": str(self.outbox_path),
        }


def _service(args: argparse.Namespace) -> MemoryService:
    return MemoryService.from_paths(
        resolve_knowledge_root(args.knowledge_root),
        resolve_data_root(args.data_root),
        timeout_seconds=getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS),
    )


def _plan_mode(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-root")
    parser.add_argument("--data-root")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status")
    commands.add_parser("doctor")

    setup = commands.add_parser("setup", help="Plan or initialize memory configuration")
    setup.add_argument("--enable-mem0", action="store_true")
    _plan_mode(setup)

    disable = commands.add_parser("disable", help="Plan or disable Mem0 recall")
    _plan_mode(disable)

    commands.add_parser("uninstall", help="Explain safe manual uninstall; deletes nothing")

    reindex = commands.add_parser("reindex", help="Preview or apply an approved-memory rebuild")
    _plan_mode(reindex)
    reindex.add_argument(
        "--force",
        action="store_true",
        help="Ignore derived state for a verified full rebuild after index loss",
    )
    reindex.add_argument("--limit", type=int, default=1000)

    add = commands.add_parser("add-candidate")
    add.add_argument("--type", dest="memory_type", required=True)
    add.add_argument("--summary", required=True)
    add.add_argument("--content", required=True)
    add.add_argument("--keyword", action="append", default=[])
    add.add_argument("--metadata", action="append", default=[])
    add.add_argument("--evidence", action="append", default=[])
    add.add_argument("--scope", default="project")
    add.add_argument("--owner", default="opc-team")
    add.add_argument("--confidence", type=float, default=0.5)
    add.add_argument("--project-id")
    add.add_argument("--source")

    approve = commands.add_parser("approve")
    approve.add_argument("record_id")
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--validation", required=True)

    reject = commands.add_parser("reject")
    reject.add_argument("record_id")
    reject.add_argument("--rejected-by", required=True)
    reject.add_argument("--reason", required=True)

    query = commands.add_parser("query")
    query.add_argument("text", nargs="?", default="")
    query.add_argument("--type", dest="memory_type")
    query.add_argument("--keyword", action="append", default=[])
    query.add_argument("--metadata", action="append", default=[])
    query.add_argument("--include-unapproved", action="store_true")
    query.add_argument("--project-id")
    query.add_argument("--limit", type=int, default=20)

    listing = commands.add_parser("list")
    listing.add_argument(
        "--status", choices=MEMORY_STATUSES, default="approved"
    )
    listing.add_argument("--type", dest="memory_type")
    listing.add_argument("--include-unapproved", action="store_true")
    listing.add_argument("--limit", type=int, default=100)

    obsolete = commands.add_parser("obsolete")
    obsolete.add_argument("record_id")
    obsolete.add_argument("--reason", required=True)
    obsolete.add_argument("--superseded-by")

    export = commands.add_parser("export-context")
    export.add_argument("--query", default="")
    export.add_argument("--type", dest="memory_type", default="decision")
    export.add_argument("--project-id")
    export.add_argument("--limit", type=int, default=20)
    return parser


def _setup_result(args: argparse.Namespace) -> dict[str, Any]:
    knowledge_root = resolve_knowledge_root(args.knowledge_root)
    data_root = resolve_data_root(args.data_root)
    validate_root_isolation(knowledge_root, data_root)
    plugin_root = PLUGIN_ROOT
    script_path = Path(__file__).resolve()
    requirements_path = plugin_root / "requirements-mem0.txt"
    venv_root = data_root / "venv"
    windows_python = venv_root / "Scripts" / "python.exe"
    unix_python = venv_root / "bin" / "python"
    plan = {
        "dry_run": not args.apply,
        "knowledge_root": str(knowledge_root),
        "data_root": str(data_root),
        "actions": [
            "initialize File/Git memory folders",
            "write plugin-scoped memory config",
        ],
        "mem0_enabled": bool(args.enable_mem0),
        "provider": "mem0ai 2.0.11 OSS Python library",
        "dependency_action": "none by opc_memory.py; run the isolated commands only after explicit approval",
        "isolated_venv": {
            "root": str(venv_root),
            "requirements": str(requirements_path),
            "windows": [
                f'python -m venv "{venv_root}"',
                f'"{windows_python}" -m pip install -r "{requirements_path}"',
                f'"{windows_python}" "{script_path}" --knowledge-root "{knowledge_root}" --data-root "{data_root}" status',
            ],
            "unix": [
                f"python3 -m venv '{venv_root}'",
                f"'{unix_python}' -m pip install -r '{requirements_path}'",
                f"'{unix_python}' '{script_path}' --knowledge-root '{knowledge_root}' --data-root '{data_root}' status",
            ],
        },
        "network": "Depends on the configured LLM/embedder; Memory() defaults may contact OpenAI.",
        "credential_variables": ["OPENAI_API_KEY (for the default Memory() configuration)"],
        "anonymous_identity_action": "generate and persist a random UUID on apply",
        "provider_note": "mem0ai 2.x Memory() may require OPENAI_API_KEY and is not guaranteed fully local.",
    }
    if args.apply:
        FileGitBackend(knowledge_root).ensure_layout()
        config = load_config(data_root)
        identity_created = ensure_anonymous_identity(config)
        config["mem0"]["enabled"] = bool(args.enable_mem0)
        write_config(data_root, config)
        plan["anonymous_identity_created"] = identity_created
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            result: Any = _setup_result(args)
        elif args.command == "uninstall":
            result = {
                "performed": False,
                "knowledge_preserved": str(resolve_knowledge_root(args.knowledge_root)),
                "data_preserved": str(resolve_data_root(args.data_root)),
                "instructions": [
                    "Run `disable --apply` to stop optional Mem0 recall.",
                    "Remove mem0ai manually only after checking that no other project uses it.",
                    "Never delete the File/Git knowledge root as part of plugin uninstall.",
                ],
            }
        elif args.command == "disable":
            knowledge_root = resolve_knowledge_root(args.knowledge_root)
            data_root = resolve_data_root(args.data_root)
            validate_root_isolation(knowledge_root, data_root)
            result = {
                "dry_run": not args.apply,
                "action": "set mem0.enabled=false",
                "data_root": str(data_root),
                "dependency_action": "none",
            }
            if args.apply:
                config = load_config(data_root)
                config["mem0"]["enabled"] = False
                write_config(data_root, config)
        else:
            service = _service(args)
            if args.command == "status":
                result = service.status()
            elif args.command == "doctor":
                result = service.doctor()
            elif args.command == "reindex":
                result = (
                    service.reindex_apply(args.limit, force=args.force)
                    if args.apply
                    else service.reindex_plan(args.limit, force=args.force)
                )
            elif args.command == "add-candidate":
                result = service.add_candidate(
                    memory_type=args.memory_type,
                    summary=args.summary,
                    content=args.content,
                    keywords=args.keyword,
                    metadata=parse_pairs(args.metadata),
                    evidence=parse_pairs(args.evidence),
                    scope=args.scope,
                    owner=args.owner,
                    confidence=args.confidence,
                    project_id=args.project_id,
                    source=args.source,
                )
            elif args.command == "approve":
                result = service.approve(
                    args.record_id,
                    approved_by=args.approved_by,
                    validation=args.validation,
                )
            elif args.command == "reject":
                result = service.reject(
                    args.record_id,
                    rejected_by=args.rejected_by,
                    reason=args.reason,
                )
            elif args.command == "query":
                result = service.query(
                    args.text,
                    approved_only=not args.include_unapproved,
                    memory_type=args.memory_type,
                    metadata=parse_pairs(args.metadata),
                    keywords=args.keyword,
                    project_id=args.project_id,
                    limit=args.limit,
                )
            elif args.command == "list":
                if args.include_unapproved:
                    if not args.memory_type:
                        raise OpcMemoryError(
                            "--include-unapproved requires --type; use --status for a status listing"
                        )
                    result = service.list_by_type(
                        args.memory_type, approved_only=False, limit=args.limit
                    )
                else:
                    result = service.list_by_status(
                        args.status, memory_type=args.memory_type, limit=args.limit
                    )
            elif args.command == "obsolete":
                result = service.mark_obsolete(
                    args.record_id,
                    reason=args.reason,
                    superseded_by=args.superseded_by,
                )
            elif args.command == "export-context":
                print(
                    service.export_decision_context(
                        args.query,
                        memory_type=args.memory_type,
                        project_id=args.project_id,
                        limit=args.limit,
                    ),
                    end="",
                )
                return 0
            else:
                parser.error(f"Unhandled command: {args.command}")
                return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not result["ok"]:
            return 1
        if args.command == "reindex" and args.apply and not result["ok"]:
            return 1
        return 0
    except (OpcMemoryError, OSError, ValueError) as exc:
        print(f"OPC_MEMORY_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
