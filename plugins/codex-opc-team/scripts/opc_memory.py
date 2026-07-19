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
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from opc_governance import (
    CONTEXT_VERSION,
    CURATION_VERSION,
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    MIGRATION_VERSION,
    SCHEMA_V2,
    GovernanceError,
    applicability_reasons,
    canonical_citation,
    load_contract as load_governance_contract,
    migrate_record,
    normalize_relations,
    relation_applies,
    relation_cycles,
    strict_json_bytes,
    validate_query_context,
    validate_record,
)


SCHEMA_VERSION = 1
KNOWLEDGE_SCHEMA_VERSION = SCHEMA_V2
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
LEGACY_RUNTIME_DIRECTORIES = ("evaluations/events",)
LEGACY_RUNTIME_EXACT_PATHS = ("hook-events.jsonl", "evaluations/hook-events.jsonl")
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


def parse_json_object(value: str | None, *, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if len(value.encode("utf-8")) > 64 * 1024:
        raise OpcMemoryError(f"{label} exceeds the configured size limit")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise OpcMemoryError(f"{label} must be strict JSON") from exc
    if not isinstance(parsed, dict):
        raise OpcMemoryError(f"{label} must be a JSON object")
    return parsed


def parse_relation_objects(values: Sequence[str] | None) -> list[dict[str, Any]] | None:
    if values is None:
        return None
    if len(values) > 64:
        raise OpcMemoryError("relations exceeds the configured item limit")
    return [
        parse_json_object(value, label=f"relation[{index}]") or {}
        for index, value in enumerate(values)
    ]


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


def _lexical_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _assert_unlinked_ancestors(path: Path, *, label: str) -> Path:
    """Inspect lexical ancestors before resolve; accept normal Windows aliases by identity."""

    candidate = _lexical_absolute(path)
    current = candidate
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OpcMemoryError(f"{label} boundary could not be inspected") from exc
        else:
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(current):
                raise OpcMemoryError(f"{label} crosses a symlink or reparse boundary")
        parent = current.parent
        if parent == current:
            return candidate
        current = parent


def _strict_record_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise OpcMemoryError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise OpcMemoryError(f"{label} must be a JSON object")
    try:
        validate_record(value)
    except GovernanceError as exc:
        raise OpcMemoryError(f"{label}: {exc}") from exc
    return value


def _read_bounded_bytes(path: Path, *, label: str, maximum: int = MAX_RECORD_BYTES) -> bytes:
    candidate = _assert_unlinked_ancestors(path, label=label)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise OpcMemoryError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
    ):
        raise OpcMemoryError(f"{label} must be one bounded, uniquely linked regular file")
    try:
        # Lazy import avoids a module cycle: opc_feedback itself uses opc_memory.
        from opc_feedback import FeedbackError, _BoundDirectory

        with _BoundDirectory(candidate.parent, candidate.parent) as bound:
            return bound.read_bytes(
                candidate.name,
                max_bytes=maximum,
                require_single_link=True,
            )
    except (FeedbackError, OSError) as exc:
        raise OpcMemoryError(f"{label} could not be read through a stable directory") from exc


def _read_bounded_record(path: Path, *, label: str) -> dict[str, Any]:
    return _strict_record_json(
        _read_bounded_bytes(path, label=label),
        label=label,
    )


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


def _lstat_identity(path: Path) -> dict[str, int]:
    """Return metadata that identifies an object without reading its contents."""
    value = path.lstat()
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "mode": int(value.st_mode),
        "links": int(value.st_nlink),
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
    }


def _metadata_token(value: Mapping[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _object_token(identity: Mapping[str, int]) -> str:
    """Identify the same filesystem object across a hard-link operation."""
    return _metadata_token(
        {
            key: identity[key]
            for key in ("device", "inode", "mode", "size", "mtime_ns")
        }
    )


def _directory_object_token(identity: Mapping[str, int]) -> str:
    """Bind a directory object while allowing expected entry metadata changes."""
    return _metadata_token(
        {key: identity[key] for key in ("device", "inode", "mode")}
    )


def _bind_archive_parent(path: Path, archive_root: Path, *, source_label: str) -> str:
    """Validate containment and return a stable identity for a directory object."""
    try:
        identity = _lstat_identity(path)
        resolved = path.resolve()
        resolved.relative_to(archive_root)
    except (OSError, ValueError) as exc:
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: archive parent is unavailable or escaped "
            f"private data ({source_label})"
        ) from exc
    if path.is_symlink() or not stat.S_ISDIR(identity["mode"]):
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: archive parent is not a stable directory "
            f"({source_label})"
        )
    return _directory_object_token(identity)


def _archive_parent_unchanged(
    path: Path, archive_root: Path, expected_token: str
) -> bool:
    try:
        identity = _lstat_identity(path)
        path.resolve().relative_to(archive_root)
    except (OSError, ValueError):
        return False
    return (
        not path.is_symlink()
        and stat.S_ISDIR(identity["mode"])
        and _directory_object_token(identity) == expected_token
    )


def _rollback_created_link(destination: Path, created_object_token: str | None) -> bool:
    """Remove only the object just linked; never unlink an unverified competitor."""
    if not created_object_token:
        return False
    try:
        identity = _lstat_identity(destination)
        if _object_token(identity) != created_object_token:
            return False
        destination.unlink()
        return True
    except OSError:
        return False


@contextmanager
def _legacy_archive_lock(data_root: Path):
    """Serialize legacy archive apply operations inside private runtime data."""
    archive_path = data_root / "legacy-event-archive"
    try:
        archive_path.resolve().relative_to(data_root)
    except ValueError as exc:
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: archive root escaped private data"
        ) from exc
    if archive_path.is_symlink():
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: archive root is a symbolic link"
        )
    try:
        archive_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: private archive root could not be created"
        ) from exc
    if archive_path.is_symlink() or archive_path.resolve().parent != data_root:
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: archive root changed during lock setup"
        )
    lock_path = archive_path / ".opc-legacy-events.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            for attempt in range(100):
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if attempt == 99:
                        raise OpcMemoryError(
                            "LEGACY_EVENT_LOCK_BUSY: another archive apply is running"
                        )
                    time.sleep(0.01)
        else:
            import fcntl

            for attempt in range(100):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if attempt == 99:
                        raise OpcMemoryError(
                            "LEGACY_EVENT_LOCK_BUSY: another archive apply is running"
                        )
                    time.sleep(0.01)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_archive_parent(
    archive_path: Path, destination_parent: Path, *, source_label: str
) -> None:
    """Create archive subdirectories without traversing symbolic links."""
    archive_root = archive_path.resolve()
    try:
        relative = destination_parent.relative_to(archive_path)
    except ValueError as exc:
        raise OpcMemoryError(
            "LEGACY_EVENT_MOVE_BLOCKED: archive destination escaped private data "
            f"({source_label})"
        ) from exc
    current = archive_path
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise OpcMemoryError(
                "LEGACY_EVENT_MOVE_BLOCKED: archive destination contains a "
                f"symbolic link ({source_label})"
            )
        try:
            current.mkdir(exist_ok=True)
        except OSError as exc:
            raise OpcMemoryError(
                "LEGACY_EVENT_MOVE_BLOCKED: archive directory could not be created "
                f"({source_label})"
            ) from exc
        if current.is_symlink() or not current.is_dir():
            raise OpcMemoryError(
                "LEGACY_EVENT_MOVE_BLOCKED: archive destination is not a safe "
                f"directory ({source_label})"
            )
        try:
            current.resolve().relative_to(archive_root)
        except ValueError as exc:
            raise OpcMemoryError(
                "LEGACY_EVENT_MOVE_BLOCKED: archive destination escaped private data "
                f"({source_label})"
            ) from exc


class FileGitBackend:
    """Canonical JSON-file repository with optional Git provenance checks."""

    def __init__(self, root: Path | str):
        lexical = _assert_unlinked_ancestors(Path(root), label="knowledge_root")
        self.root = lexical.resolve()
        validate_private_root_against_plugin(self.root, label="knowledge_root")

    def _load_record(self, path: Path) -> dict[str, Any]:
        # Compare parent directory objects, not path spellings.  This accepts a
        # normal Windows 8.3 alias of an authorized status directory without
        # allowing an arbitrary descendant or relying on case folding.  The
        # actual read still uses the lexical path and rejects symlink/reparse
        # ancestors before following anything.
        authorized_parent = False
        for status in MEMORY_STATUSES:
            try:
                if os.path.samefile(path.parent, self._folder(status)):
                    authorized_parent = True
                    break
            except OSError:
                continue
        if not authorized_parent:
            raise OpcMemoryError("knowledge record escaped the canonical root")
        return _read_bounded_record(path, label="canonical knowledge record")

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
        sensitivity: str = "internal",
        applicable_roles: Sequence[str] | None = None,
        applicability: Mapping[str, Sequence[str]] | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        relations: Sequence[Mapping[str, Any]] | None = None,
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
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
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
            "sensitivity": sensitivity,
            "applicability": {
                "roles": sorted(set(applicable_roles or [])),
                "knowledge_types": [memory_type.strip()],
                "constraints": {
                    str(key): sorted(set(str(item) for item in values))
                    for key, values in sorted((applicability or {}).items())
                },
                "valid_from": valid_from,
                "valid_until": valid_until,
            },
            "relations": sorted(
                [dict(item) for item in relations or []],
                key=lambda item: (
                    str(item.get("kind", "")),
                    str(item.get("target_id", "")),
                    str(item.get("scope", "")),
                    str(item.get("project_id") or ""),
                ),
            ),
            "created_at": now,
            "updated_at": now,
        }
        if project_id:
            record["project_id"] = project_id
        if source:
            record["source"] = source
        try:
            validate_record(record)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
        path = self._path("candidate", record_id)
        atomic_write_json(path, record)
        return self._with_source(record, path)

    def approve(
        self, record_id: str, *, approved_by: str, validation: str
    ) -> dict[str, Any]:
        if not approved_by.strip() or not validation.strip():
            raise OpcMemoryError("approval requires approved_by and validation")
        _, source = self._locate(record_id, ("candidate",))
        record = self._load_record(source)
        record.update(
            {
                "status": "approved",
                "approved_by": approved_by.strip(),
                "approved_at": utc_now(),
                "validation": validation.strip(),
                "updated_at": utc_now(),
            }
        )
        try:
            validate_record(record)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
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
        record = self._load_record(source)
        record.update(
            {
                "status": "rejected",
                "rejected_by": rejected_by.strip(),
                "rejected_at": utc_now(),
                "rejection_reason": reason.strip(),
                "updated_at": utc_now(),
            }
        )
        try:
            validate_record(record)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
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
        record = self._load_record(source)
        record.update(
            {
                "status": "obsolete",
                "obsolete_at": utc_now(),
                "obsolete_reason": reason.strip(),
                "updated_at": utc_now(),
            }
        )
        if superseded_by:
            target_id = safe_record_id(superseded_by)
            if record.get("schema_version") == 2:
                record.setdefault("relations", []).append(
                    {
                        "kind": "superseded_by",
                        "target_id": target_id,
                        "scope": record["scope"],
                        "project_id": record.get("project_id"),
                    }
                )
                record["relations"] = sorted(
                    record["relations"],
                    key=lambda item: (
                        item["kind"],
                        item["target_id"],
                        item["scope"],
                        item.get("project_id") or "",
                    ),
                )
            else:
                record["superseded_by"] = target_id
        try:
            validate_record(record)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
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
        role: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
        at: str | None = None,
        extra_candidate_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not approved_only:
            # Unapproved records are inspection-only and never enter the governed
            # Context contract.  Preserve the legacy debug surface without
            # presenting it as executable context.
            records: list[dict[str, Any]] = []
            for status in MEMORY_STATUSES:
                for record in self.list_by_status(status, limit=MAX_RECORDS):
                    if self.record_matches(
                        record,
                        text=text,
                        memory_type=memory_type,
                        metadata=metadata,
                        keywords=keywords,
                        project_id=project_id,
                    ):
                        records.append(record)
            return records[: max(0, limit)]
        return self.query_context(
            text,
            memory_type=memory_type,
            metadata=metadata,
            keywords=keywords,
            project_id=project_id,
            limit=limit,
            role=role,
            applicability=applicability,
            allowed_sensitivity=allowed_sensitivity,
            at=at,
            extra_candidate_ids=extra_candidate_ids,
        )["records"]

    def query_context(
        self,
        text: str = "",
        *,
        memory_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        keywords: Sequence[str] | None = None,
        project_id: str | None = None,
        limit: int = 20,
        role: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
        at: str | None = None,
        extra_candidate_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        try:
            load_governance_contract()
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
        try:
            context_values, sensitivities = validate_query_context(
                project_id=project_id,
                role=role,
                applicability=applicability,
                allowed_sensitivity=allowed_sensitivity,
                limit=limit,
            )
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
        try:
            evaluation_time = (
                datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc)
                if at is not None
                else datetime.now(timezone.utc)
            )
        except (AttributeError, ValueError) as exc:
            raise OpcMemoryError("at must be a timezone-aware RFC 3339 timestamp") from exc
        if evaluation_time.tzinfo is None:
            raise OpcMemoryError("at must include a timezone")
        extra_ids = {safe_record_id(value) for value in extra_candidate_ids or []}

        inventory: dict[str, dict[str, Any]] = {}
        provenance: dict[str, dict[str, Any]] = {}
        duplicate_ids: set[str] = set()
        invalid_count = 0
        for status in MEMORY_STATUSES:
            paths = sorted(self._folder(status).glob("*.json"))
            if len(paths) > MAX_RECORDS:
                raise OpcMemoryError("knowledge repository exceeds the configured record limit")
            for path in paths:
                try:
                    record = self._load_record(path)
                    record_id = str(record["id"])
                    if record_id in duplicate_ids:
                        invalid_count += 1
                        continue
                    if record_id in inventory:
                        inventory.pop(record_id, None)
                        provenance.pop(record_id, None)
                        duplicate_ids.add(record_id)
                        # Both the previously accepted occurrence and this one
                        # are invalid once uniqueness is disproved.
                        invalid_count += 2
                        continue
                    record = self._with_source(record, path)
                    inventory[record_id] = record
                    if record.get("status") == "approved":
                        provenance[record_id] = self.source_metadata(record["_source_path"])
                except (OpcMemoryError, OSError):
                    invalid_count += 1

        base_reasons: dict[str, list[str]] = {}
        for record_id, record in inventory.items():
            reasons: list[str] = []
            if record.get("status") != "approved":
                reasons.append(str(record.get("status") or "status_invalid"))
            if not self._scope_matches(record, project_id):
                reasons.append("project_scope_mismatch")
            source = provenance.get(record_id, {})
            if not source.get("source_commit"):
                reasons.append("stale_provenance")
            if record.get("sensitivity", "internal") not in sensitivities:
                reasons.append("sensitivity_not_authorized")
            try:
                reasons.extend(
                    applicability_reasons(
                        record,
                        role=role,
                        knowledge_type=memory_type,
                        context=context_values,
                        at=evaluation_time,
                    )
                )
            except GovernanceError:
                reasons.append("applicability_invalid")
            base_reasons[record_id] = sorted(set(reasons))

        candidate_ids: set[str] = set(extra_ids)
        for record_id, record in inventory.items():
            if record.get("status") != "approved":
                continue
            if self.record_matches(
                record,
                text=text,
                memory_type=memory_type,
                metadata=metadata,
                keywords=keywords,
                project_id=project_id,
            ):
                candidate_ids.add(record_id)

        relation_reasons: dict[str, set[str]] = {record_id: set() for record_id in inventory}
        edges: dict[str, set[str]] = {}
        active_relations: list[tuple[str, str, str]] = []
        for source_id, record in inventory.items():
            try:
                relations = normalize_relations(record)
            except GovernanceError:
                relation_reasons[source_id].add("relations_invalid")
                continue
            for relation in relations:
                if not relation_applies(relation, project_id):
                    continue
                target_id = relation["target_id"]
                if target_id not in inventory:
                    relation_reasons[source_id].add("relation_target_missing")
                    continue
                kind = relation["kind"]
                active_relations.append((source_id, target_id, kind))
                if kind != "conflicts":
                    edges.setdefault(source_id, set()).add(target_id)

        for record_id in relation_cycles(edges):
            relation_reasons[record_id].add("relation_cycle")

        for source_id, target_id, kind in active_relations:
            if base_reasons.get(source_id) or relation_reasons[source_id]:
                continue
            target_eligible = not base_reasons.get(target_id) and not relation_reasons[target_id]
            if kind in {"supersedes", "invalidates"}:
                if target_eligible:
                    relation_reasons[target_id].add(
                        "superseded" if kind == "supersedes" else "invalidated"
                    )
            elif kind in {"superseded_by", "invalidated_by"}:
                if target_eligible:
                    relation_reasons[source_id].add(
                        "superseded" if kind == "superseded_by" else "invalidated"
                    )
                else:
                    relation_reasons[source_id].add("relation_target_ineligible")

        conflict_pairs: set[tuple[str, str]] = set()
        for source_id, target_id, kind in active_relations:
            if kind != "conflicts":
                continue
            if (
                not base_reasons.get(source_id)
                and not relation_reasons[source_id]
                and not base_reasons.get(target_id)
                and not relation_reasons[target_id]
            ):
                conflict_pairs.add(tuple(sorted((source_id, target_id))))

        conflicted = {record_id for pair in conflict_pairs for record_id in pair}
        records: list[dict[str, Any]] = []
        omissions: list[dict[str, Any]] = []
        for record_id in sorted(candidate_ids):
            record = inventory.get(record_id)
            if record is None:
                continue
            reasons = sorted(set(base_reasons.get(record_id, [])) | relation_reasons[record_id])
            if record_id in conflicted:
                reasons.append("unresolved_conflict")
            if reasons:
                item: dict[str, Any] = {
                    "record_id": record_id,
                    "reason_codes": sorted(set(reasons)),
                }
                try:
                    item["citation"] = canonical_citation(record, provenance.get(record_id, {}))
                except GovernanceError:
                    pass
                omissions.append(item)
                continue
            hit = dict(record)
            hit["_score"] = self._score(record, text)
            hit["_recall_source"] = "file"
            hit["_authority"] = "file-git"
            hit["_citation"] = canonical_citation(record, provenance[record_id])
            records.append(hit)

        records.sort(
            key=lambda item: (
                -float(item.get("_score", 0)),
                str(item.get("id", "")),
            )
        )
        conflicts: list[dict[str, Any]] = []
        for left, right in sorted(conflict_pairs):
            conflicts.append(
                {
                    "reason_code": "unresolved_conflict",
                    "citations": [
                        canonical_citation(inventory[left], provenance[left]),
                        canonical_citation(inventory[right], provenance[right]),
                    ],
                }
            )
        return {
            "schema_version": CONTEXT_VERSION,
            "query": {
                "project_id": project_id,
                "role": role,
                "knowledge_type": memory_type,
                "applicability": context_values,
                "allowed_sensitivity": list(sensitivities),
            },
            "records": records[:limit],
            "conflicts": conflicts[:limit],
            "omissions": omissions[:limit],
            "omitted_summary": {
                "count": len(omissions) + invalid_count,
                "invalid_record_count": invalid_count,
                "reason_codes": sorted(
                    {
                        reason
                        for item in omissions
                        for reason in item["reason_codes"]
                    }
                ),
            },
        }

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
            record = self._load_record(path)
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
        role: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
    ) -> str:
        context = self.query_context(
            query,
            memory_type=memory_type,
            project_id=project_id,
            limit=limit,
            role=role,
            applicability=applicability,
            allowed_sensitivity=allowed_sensitivity,
        )
        records = context["records"]
        lines = ["# OPC decision context", ""]
        if not records and not context["conflicts"]:
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
        if context["conflicts"]:
            lines.extend(["## Unresolved knowledge conflicts", ""])
            for conflict in context["conflicts"]:
                citations = conflict["citations"]
                lines.append(
                    "- `unresolved_conflict`: "
                    + " <> ".join(
                        f"`{item['source_path']}@{item['source_commit']}#{item['content_sha256']}`"
                        for item in citations
                    )
                )
            lines.extend(
                [
                    "",
                    "Conflicting bodies were withheld; manager curation is required.",
                    "",
                ]
            )
        if context["omitted_summary"]["count"]:
            lines.append(
                "- Omitted reason codes: "
                + ", ".join(context["omitted_summary"]["reason_codes"])
            )
        return "\n".join(lines)

    def _resolve_source(self, source_path: str) -> Path:
        lexical = Path(source_path)
        if (
            not source_path
            or lexical.is_absolute()
            or "\\" in source_path
            or any(part in {"", ".", ".."} for part in lexical.parts)
        ):
            raise StaleSourceError("Recall source_path must be relative to the knowledge root")
        unchecked = self.root / lexical
        try:
            _assert_unlinked_ancestors(unchecked, label="canonical source")
        except OpcMemoryError as exc:
            raise StaleSourceError(str(exc)) from exc
        candidate = unchecked.resolve()
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
        raw = _read_bounded_bytes(path, label="canonical source")
        _strict_record_json(raw, label="canonical source")
        content_hash = sha256_bytes(raw)
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
        raw = _read_bounded_bytes(path, label="canonical source")
        actual_hash = sha256_bytes(raw)
        if not content_hash or actual_hash != content_hash:
            raise StaleSourceError(f"Authoritative source hash changed: {source_path}")
        if source_commit:
            current = self.source_commit(path, actual_hash)
            if current != source_commit:
                raise StaleSourceError("source_commit is not the current Git HEAD provenance")
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
        record = _strict_record_json(raw, label="canonical source")
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

    @staticmethod
    def _is_legacy_runtime_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("./")
        if normalized in LEGACY_RUNTIME_EXACT_PATHS:
            return True
        return any(
            normalized.startswith(f"{directory}/")
            and normalized != f"{directory}/.gitkeep"
            for directory in LEGACY_RUNTIME_DIRECTORIES
        )

    def legacy_runtime_artifacts(self) -> list[str]:
        """Inventory known legacy runtime paths without reading file contents."""
        found: set[str] = set()
        for relative in LEGACY_RUNTIME_EXACT_PATHS:
            candidate = self.root / relative
            if candidate.is_file() or candidate.is_symlink():
                found.add(relative)
        for relative in LEGACY_RUNTIME_DIRECTORIES:
            directory = self.root / relative
            if directory.is_symlink():
                found.add(relative)
                continue
            if not directory.is_dir():
                continue
            pending = [directory]
            while pending:
                current = pending.pop()
                try:
                    with os.scandir(current) as iterator:
                        entries = list(iterator)
                except OSError:
                    found.add(current.relative_to(self.root).as_posix())
                    continue
                for entry in entries:
                    candidate = Path(entry.path)
                    candidate_relative = candidate.relative_to(self.root).as_posix()
                    if candidate_relative.endswith("/.gitkeep"):
                        continue
                    if entry.is_symlink():
                        found.add(candidate_relative)
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(candidate)
                    elif entry.is_file(follow_symlinks=False):
                        found.add(candidate_relative)
        return sorted(found)

    def _tracked_path(self, relative: str) -> bool | None:
        top_text = _git(self.root, ("rev-parse", "--show-toplevel"))
        if not isinstance(top_text, str) or not top_text:
            return None
        tracked = _git(self.root, ("ls-files", "--stage", "--", relative))
        if tracked is None:
            return None
        return bool(tracked.strip())

    def legacy_runtime_plan(self, data_root: Path) -> dict[str, Any]:
        """Build a redacted, non-mutating archive plan for legacy event files."""
        data_root = data_root.expanduser().resolve()
        validate_root_isolation(self.root, data_root)
        archive_path = data_root / "legacy-event-archive"
        archive_root = archive_path.resolve()
        try:
            archive_root.relative_to(data_root)
            archive_safe = not archive_path.is_symlink()
        except ValueError:
            archive_safe = False
        entries: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        for relative in self.legacy_runtime_artifacts():
            source = self.root / relative
            tracked = self._tracked_path(relative)
            is_symlink = source.is_symlink()
            is_file = source.is_file() and not is_symlink
            destination = archive_path / relative
            resolved_destination = destination.resolve()
            destination_exists = destination.exists() or destination.is_symlink()
            eligible = (
                tracked is False
                and is_file
                and not destination_exists
                and archive_safe
            )
            if not archive_safe:
                reason = "archive root is a symbolic link or escaped private data"
            elif tracked is None:
                reason = "Git tracked-state diagnosis failed or is unavailable"
            elif tracked:
                reason = "artifact is tracked; automatic movement is refused"
            elif is_symlink:
                reason = "artifact is a symbolic link; automatic movement is refused"
            elif not is_file:
                reason = "artifact is not a regular file"
            elif destination_exists:
                reason = "archive destination already exists"
            else:
                reason = None
            identity = _lstat_identity(source)
            source_identity_token = _metadata_token(identity)
            source_object_token = _object_token(identity)
            entries.append(
                {
                    "source": relative,
                    "destination": destination.relative_to(data_root).as_posix(),
                    "tracked": tracked,
                    "eligible": eligible,
                    "blocked_reason": reason,
                    "source_identity_token": source_identity_token,
                    "source_object_token": source_object_token,
                }
            )
            fingerprints.append(
                {
                    "source": str(source),
                    "destination": str(resolved_destination),
                    "source_identity": identity,
                    "tracked": tracked,
                    "eligible": eligible,
                    "destination_exists": destination_exists,
                }
            )
        approval_token = (
            _metadata_token(
                {
                    "schema_version": 2,
                    "knowledge_root": str(self.root),
                    "data_root": str(data_root),
                    "archive_root": str(archive_root),
                    "entries": fingerprints,
                }
            )
            if fingerprints
            else None
        )
        return {
            "dry_run": True,
            "detected": bool(entries),
            "artifact_count": len(entries),
            "contents_inspected": False,
            "source_provenance": "unresolved_historical",
            "archive_root": str(archive_root),
            "entries": entries,
            "approval_token": approval_token,
            "apply_requirements": [
                "review this preview without opening event contents",
                "obtain explicit approval for the exact source and destination paths",
                "rerun with --apply and the unchanged --plan-token",
            ],
            "automatic_actions_excluded": ["delete", "commit", "upload"],
        }

    def apply_legacy_runtime_plan(
        self, data_root: Path, *, plan_token: str | None
    ) -> dict[str, Any]:
        """Move only previewed, untracked regular files into private runtime data."""
        data_root = data_root.expanduser().resolve()
        plan = self.legacy_runtime_plan(data_root)
        expected = plan["approval_token"]
        if not expected:
            return {**plan, "dry_run": False, "moved": [], "changed": False}
        if not plan_token or plan_token != expected:
            raise OpcMemoryError(
                "LEGACY_EVENT_PLAN_CHANGED: run legacy-events --dry-run and pass "
                "its unchanged approval_token with --apply --plan-token"
            )
        blocked = [entry for entry in plan["entries"] if not entry["eligible"]]
        if blocked:
            blocked_paths = ", ".join(str(entry["source"]) for entry in blocked)
            raise OpcMemoryError(
                "LEGACY_EVENT_MOVE_BLOCKED: automatic movement is limited to "
                f"untracked regular files with unused destinations ({blocked_paths})"
            )
        moved: list[dict[str, str]] = []
        with _legacy_archive_lock(data_root):
            locked_plan = self.legacy_runtime_plan(data_root)
            if locked_plan["approval_token"] != plan_token:
                raise OpcMemoryError(
                    "LEGACY_EVENT_PLAN_CHANGED: source, destination, roots, or Git "
                    "state changed after preview; run a new dry-run"
                )
            locked_blocked = [
                entry for entry in locked_plan["entries"] if not entry["eligible"]
            ]
            if locked_blocked:
                blocked_paths = ", ".join(
                    str(entry["source"]) for entry in locked_blocked
                )
                raise OpcMemoryError(
                    "LEGACY_EVENT_MOVE_BLOCKED: state changed before apply "
                    f"({blocked_paths})"
                )
            archive_path = data_root / "legacy-event-archive"
            archive_root = archive_path.resolve()
            for entry in locked_plan["entries"]:
                source = self.root / str(entry["source"])
                destination = data_root / str(entry["destination"])
                _ensure_archive_parent(
                    archive_path,
                    destination.parent,
                    source_label=str(entry["source"]),
                )
                try:
                    destination.parent.resolve().relative_to(archive_root)
                except ValueError as exc:
                    raise OpcMemoryError(
                        "LEGACY_EVENT_MOVE_BLOCKED: archive destination escaped the "
                        f"private archive root ({entry['source']})"
                    ) from exc
                parent_binding = _bind_archive_parent(
                    destination.parent,
                    archive_root,
                    source_label=str(entry["source"]),
                )
                try:
                    identity = _lstat_identity(source)
                except OSError as exc:
                    raise OpcMemoryError(
                        "LEGACY_EVENT_PLAN_CHANGED: source disappeared before move "
                        f"({entry['source']})"
                    ) from exc
                if (
                    _metadata_token(identity) != entry["source_identity_token"]
                    or not stat.S_ISREG(identity["mode"])
                    or source.is_symlink()
                    or self._tracked_path(str(entry["source"])) is not False
                ):
                    raise OpcMemoryError(
                        "LEGACY_EVENT_PLAN_CHANGED: source identity, type, or Git "
                        f"state changed before move ({entry['source']})"
                    )
                try:
                    destination.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise OpcMemoryError(
                        "LEGACY_EVENT_DESTINATION_EXISTS: refusing to overwrite "
                        f"the archive target ({entry['source']})"
                    )
                created_destination = False
                created_object_token: str | None = None
                created_destination_path: Path | None = None
                try:
                    os.link(source, destination, follow_symlinks=False)
                    created_destination = True
                except FileExistsError as exc:
                    raise OpcMemoryError(
                        "LEGACY_EVENT_DESTINATION_EXISTS: refusing to overwrite "
                        f"the archive target ({entry['source']})"
                    ) from exc
                except OSError as exc:
                    raise OpcMemoryError(
                        "LEGACY_EVENT_MOVE_FAILED: source was not deleted; the "
                        "archive requires same-filesystem hard-link support "
                        f"({entry['source']})"
                    ) from exc
                try:
                    created_destination_path = (
                        destination.parent.resolve() / destination.name
                    )
                    linked_identity = _lstat_identity(created_destination_path)
                    created_object_token = _object_token(linked_identity)
                    current_source_identity = _lstat_identity(source)
                    expected_object = str(entry["source_object_token"])
                    if not _archive_parent_unchanged(
                        destination.parent, archive_root, parent_binding
                    ):
                        raise OpcMemoryError(
                            "LEGACY_EVENT_PLAN_CHANGED: archive parent changed "
                            f"during the no-overwrite move ({entry['source']})"
                        )
                    if (
                        not stat.S_ISREG(linked_identity["mode"])
                        or created_destination_path.is_symlink()
                        or _object_token(linked_identity) != expected_object
                        or _object_token(current_source_identity) != expected_object
                        or self._tracked_path(str(entry["source"])) is not False
                    ):
                        raise OpcMemoryError(
                            "LEGACY_EVENT_PLAN_CHANGED: source or Git state changed "
                            f"during the no-overwrite move ({entry['source']})"
                        )
                    if not _archive_parent_unchanged(
                        destination.parent, archive_root, parent_binding
                    ):
                        raise OpcMemoryError(
                            "LEGACY_EVENT_PLAN_CHANGED: archive parent changed "
                            f"before source removal ({entry['source']})"
                        )
                    source.unlink()
                except Exception as exc:
                    if created_destination and not _rollback_created_link(
                        created_destination_path or destination,
                        created_object_token,
                    ):
                        raise OpcMemoryError(
                            "LEGACY_EVENT_ROLLBACK_FAILED: source was preserved but "
                            "the newly linked archive target could not be safely "
                            f"removed ({entry['source']})"
                        ) from exc
                    raise
                moved.append(
                    {
                        "source": str(entry["source"]),
                        "destination": str(entry["destination"]),
                    }
                )
        return {
            **plan,
            "dry_run": False,
            "moved": moved,
            "changed": bool(moved),
            "automatic_actions_performed": ["move"] if moved else [],
        }

    def schema_migration_plan(
        self,
        *,
        record_id: str | None = None,
        backup_root: Path | None = None,
    ) -> dict[str, Any]:
        """Preview a bounded Schema 1 -> 2 migration; never writes."""

        selected = safe_record_id(record_id) if record_id else None
        backup_token: str | None = None
        if backup_root is not None:
            lexical_backup = _assert_unlinked_ancestors(
                backup_root, label="migration backup root"
            )
            if not lexical_backup.is_dir():
                raise OpcMemoryError("migration backup root must already exist")
            resolved_backup = lexical_backup.resolve(strict=True)
            if _paths_overlap(resolved_backup, self.root):
                raise OpcMemoryError(
                    "migration backup root must not overlap canonical knowledge"
                )
            validate_private_root_against_plugin(
                resolved_backup, label="migration backup root"
            )
            metadata = resolved_backup.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or resolved_backup.is_symlink()
                or _is_reparse(resolved_backup)
            ):
                raise OpcMemoryError("migration backup root is not a stable directory")
            backup_token = _directory_object_token(_lstat_identity(resolved_backup))

        items: list[dict[str, Any]] = []
        for status in MEMORY_STATUSES:
            for path in sorted(self._folder(status).glob("*.json")):
                record = self._load_record(path)
                if selected and record["id"] != selected:
                    continue
                raw = _read_bounded_bytes(path, label="canonical migration source")
                source_path = path.relative_to(self.root).as_posix()
                action = (
                    "migrate_schema_1_to_2"
                    if record.get("schema_version") == 1
                    else "skip_schema_2"
                )
                items.append(
                    {
                        "record_id": record["id"],
                        "source_path": source_path,
                        "source_sha256": sha256_bytes(raw),
                        "status": record["status"],
                        "from_schema": record["schema_version"],
                        "to_schema": 2,
                        "action": action,
                    }
                )
        if selected and not items:
            raise OpcMemoryError(f"Memory record not found: {selected}")
        fingerprint = {
            "migration_version": MIGRATION_VERSION,
            "knowledge_root_identity": _directory_object_token(
                _lstat_identity(self.root)
            ),
            "backup_root_identity": backup_token,
            "items": items,
        }
        return {
            "schema_version": MIGRATION_VERSION,
            "dry_run": True,
            "zero_write": True,
            "backup_root_bound": backup_token is not None,
            "items": items,
            "pending_count": sum(
                item["action"] == "migrate_schema_1_to_2" for item in items
            ),
            "apply_requires_single_record": True,
            "plan_token": _metadata_token(fingerprint),
            "note": "No canonical knowledge, backup, Git, or provider write was performed.",
        }

    def apply_schema_migration(
        self,
        *,
        record_id: str,
        backup_root: Path,
        plan_token: str | None,
    ) -> dict[str, Any]:
        """Apply one previewed record migration with an external immutable backup."""

        preview = self.schema_migration_plan(
            record_id=record_id,
            backup_root=backup_root,
        )
        if not plan_token or plan_token != preview["plan_token"]:
            raise OpcMemoryError(
                "MIGRATION_PLAN_CHANGED: preview the exact record and backup root again"
            )
        item = preview["items"][0]
        if item["action"] == "skip_schema_2":
            return {
                **preview,
                "dry_run": False,
                "changed": False,
                "idempotent": True,
            }
        source = self.root / item["source_path"]
        raw = _read_bounded_bytes(source, label="canonical migration source")
        if sha256_bytes(raw) != item["source_sha256"]:
            raise OpcMemoryError("MIGRATION_PLAN_CHANGED: canonical source changed")
        record = _strict_record_json(raw, label="canonical migration source")
        try:
            migrated = migrate_record(record)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
        backup = _assert_unlinked_ancestors(
            backup_root, label="migration backup root"
        ).resolve(strict=True)
        backup_name = f"{record_id}-schema1-{item['source_sha256']}.json"
        backup_created = False
        backup_identity = None
        try:
            from opc_feedback import (
                FeedbackError,
                _BoundDirectory,
                _atomic_write_feedback,
                _file_identity,
            )

            with _BoundDirectory(backup, backup) as bound_backup:
                existing = bound_backup.child_identity(backup_name)
                if existing is None:
                    descriptor = bound_backup.open_exclusive(backup_name)
                    try:
                        with os.fdopen(descriptor, "wb") as handle:
                            descriptor = -1
                            handle.write(raw)
                            handle.flush()
                            os.fsync(handle.fileno())
                            backup_identity = _file_identity(os.fstat(handle.fileno()))
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    backup_created = True
                else:
                    current = bound_backup.read_bytes(
                        backup_name,
                        max_bytes=MAX_RECORD_BYTES,
                        require_single_link=True,
                    )
                    if current != raw:
                        raise OpcMemoryError(
                            "migration backup name exists with different content"
                        )
                    backup_identity = existing
                with _BoundDirectory(source.parent, source.parent) as bound_source:
                    if bound_source.read_bytes(
                        source.name,
                        max_bytes=MAX_RECORD_BYTES,
                        require_single_link=True,
                    ) != raw:
                        raise OpcMemoryError(
                            "MIGRATION_PLAN_CHANGED: canonical source changed before write"
                        )
                    _atomic_write_feedback(bound_source, source.name, migrated)
        except Exception as exc:
            if backup_created:
                try:
                    from opc_feedback import _BoundDirectory

                    with _BoundDirectory(backup, backup) as cleanup:
                        cleanup.unlink_owned(backup_name, backup_identity)
                except Exception:
                    pass
            if isinstance(exc, OpcMemoryError) and not isinstance(exc, FeedbackError):
                raise
            raise OpcMemoryError(
                "schema migration failed without a canonical partial write"
            ) from exc
        verified = self._load_record(source)
        if verified.get("schema_version") != 2:
            raise OpcMemoryError("schema migration verification failed")
        return {
            **preview,
            "dry_run": False,
            "changed": True,
            "backup_ref": backup_name,
            "transition_paths": [item["source_path"]],
            "git_commit_required": True,
            "provider_write_performed": False,
        }

    def curation_plan(
        self,
        record_id: str,
        *,
        manager_approval: str,
        set_status: str | None = None,
        validation: str | None = None,
        reason: str | None = None,
        relations: Sequence[Mapping[str, Any]] | None = None,
        applicability: Mapping[str, Any] | None = None,
        sensitivity: str | None = None,
    ) -> dict[str, Any]:
        """Preview one exact manager-governed relation/status transition."""

        record_id = safe_record_id(record_id)
        if not manager_approval.strip() or len(manager_approval) > 4096:
            raise OpcMemoryError("curation requires an explicit manager approval reference")
        current_status, source = self._locate(record_id, MEMORY_STATUSES)
        record = self._load_record(source)
        if record.get("schema_version") != 2:
            raise OpcMemoryError(
                "Schema 1 record must use previewed migration before curation"
            )
        target_status = set_status or current_status
        allowed = {
            ("candidate", "candidate"),
            ("candidate", "approved"),
            ("candidate", "rejected"),
            ("approved", "approved"),
            ("approved", "obsolete"),
        }
        if (current_status, target_status) not in allowed:
            raise OpcMemoryError("unsupported governed curation transition")
        if target_status == "approved" and current_status == "candidate" and not validation:
            raise OpcMemoryError("approval transition requires validation evidence")
        if target_status in {"rejected", "obsolete"} and not reason:
            raise OpcMemoryError(f"{target_status} transition requires a reason")
        proposal: dict[str, Any] = {
            "manager_approval": manager_approval.strip(),
            "from_status": current_status,
            "to_status": target_status,
            "validation": validation,
            "reason": reason,
            "relations": (
                sorted(
                    [dict(item) for item in relations],
                    key=lambda item: (
                        str(item.get("kind", "")),
                        str(item.get("target_id", "")),
                        str(item.get("scope", "")),
                        str(item.get("project_id") or ""),
                    ),
                )
                if relations is not None
                else None
            ),
            "applicability": dict(applicability) if applicability is not None else None,
            "sensitivity": sensitivity,
        }
        proposed = dict(record)
        if proposal["relations"] is not None:
            proposed["relations"] = proposal["relations"]
        if proposal["applicability"] is not None:
            proposed["applicability"] = proposal["applicability"]
        if sensitivity is not None:
            proposed["sensitivity"] = sensitivity
        proposed["status"] = target_status
        if target_status == "approved" and current_status == "candidate":
            proposed.update(
                {
                    "approved_by": manager_approval.strip(),
                    "approved_at": record["updated_at"],
                    "validation": validation,
                }
            )
        elif target_status == "rejected":
            proposed.update(
                {
                    "rejected_by": manager_approval.strip(),
                    "rejected_at": record["updated_at"],
                    "rejection_reason": reason,
                }
            )
        elif target_status == "obsolete":
            proposed.update(
                {"obsolete_at": record["updated_at"], "obsolete_reason": reason}
            )
        try:
            validate_record(proposed)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
        destination = self._path(target_status, record_id)
        source_raw = _read_bounded_bytes(source, label="canonical curation source")
        source_path = source.relative_to(self.root).as_posix()
        destination_path = destination.relative_to(self.root).as_posix()
        fingerprint = {
            "curation_version": CURATION_VERSION,
            "record_id": record_id,
            "source_path": source_path,
            "source_sha256": sha256_bytes(source_raw),
            "proposal": proposal,
            "destination_path": destination_path,
        }
        return {
            "schema_version": CURATION_VERSION,
            "dry_run": True,
            "zero_write": True,
            "record_id": record_id,
            "from_status": current_status,
            "to_status": target_status,
            "source_path": source_path,
            "destination_path": destination_path,
            "source_sha256": fingerprint["source_sha256"],
            "proposed_sha256": sha256_bytes(strict_json_bytes(proposed)),
            "changed_fields": sorted(
                key
                for key in ("status", "relations", "applicability", "sensitivity")
                if proposed.get(key) != record.get(key)
            ),
            "transition_paths": sorted(set((source_path, destination_path))),
            "plan_token": _metadata_token(fingerprint),
            "manager_approval_ref": manager_approval.strip(),
            "next_required_step": "apply exact preview, then commit only transition_paths",
            "provider_write_performed": False,
        }

    def apply_curation(
        self,
        record_id: str,
        *,
        plan_token: str | None,
        manager_approval: str,
        set_status: str | None = None,
        validation: str | None = None,
        reason: str | None = None,
        relations: Sequence[Mapping[str, Any]] | None = None,
        applicability: Mapping[str, Any] | None = None,
        sensitivity: str | None = None,
    ) -> dict[str, Any]:
        preview = self.curation_plan(
            record_id,
            manager_approval=manager_approval,
            set_status=set_status,
            validation=validation,
            reason=reason,
            relations=relations,
            applicability=applicability,
            sensitivity=sensitivity,
        )
        if not plan_token or plan_token != preview["plan_token"]:
            raise OpcMemoryError(
                "CURATION_PLAN_CHANGED: preview the exact transition again"
            )
        source = self.root / preview["source_path"]
        destination = self.root / preview["destination_path"]
        record = self._load_record(source)
        now = utc_now()
        if relations is not None:
            record["relations"] = sorted(
                [dict(item) for item in relations],
                key=lambda item: (
                    str(item.get("kind", "")),
                    str(item.get("target_id", "")),
                    str(item.get("scope", "")),
                    str(item.get("project_id") or ""),
                ),
            )
        if applicability is not None:
            record["applicability"] = dict(applicability)
        if sensitivity is not None:
            record["sensitivity"] = sensitivity
        target_status = preview["to_status"]
        current_status = preview["from_status"]
        record["status"] = target_status
        record["updated_at"] = now
        if target_status == "approved" and current_status == "candidate":
            record.update(
                {
                    "approved_by": manager_approval.strip(),
                    "approved_at": now,
                    "validation": validation,
                }
            )
        elif target_status == "rejected":
            record.update(
                {
                    "rejected_by": manager_approval.strip(),
                    "rejected_at": now,
                    "rejection_reason": reason,
                }
            )
        elif target_status == "obsolete":
            record.update({"obsolete_at": now, "obsolete_reason": reason})
        try:
            validate_record(record)
        except GovernanceError as exc:
            raise OpcMemoryError(str(exc)) from exc
        source_raw = _read_bounded_bytes(source, label="canonical curation source")
        if sha256_bytes(source_raw) != preview["source_sha256"]:
            raise OpcMemoryError("CURATION_PLAN_CHANGED: canonical source changed")
        try:
            from opc_feedback import FeedbackError, _BoundDirectory, _atomic_write_feedback

            if source == destination:
                with _BoundDirectory(source.parent, source.parent) as bound:
                    if bound.read_bytes(
                        source.name,
                        max_bytes=MAX_RECORD_BYTES,
                        require_single_link=True,
                    ) != source_raw:
                        raise OpcMemoryError("CURATION_PLAN_CHANGED: source changed")
                    _atomic_write_feedback(bound, source.name, record)
            else:
                if destination.exists() or destination.is_symlink():
                    raise OpcMemoryError("curation destination already exists")
                destination_identity = None
                with _BoundDirectory(destination.parent, destination.parent) as target_bound:
                    _atomic_write_feedback(target_bound, destination.name, record)
                    destination_identity = target_bound.child_identity(destination.name)
                try:
                    with _BoundDirectory(source.parent, source.parent) as source_bound:
                        source_identity = source_bound.child_identity(source.name)
                        current = source_bound.read_bytes(
                            source.name,
                            max_bytes=MAX_RECORD_BYTES,
                            require_single_link=True,
                        )
                        if current != source_raw or not source_bound.unlink_owned(
                            source.name, source_identity
                        ):
                            raise OpcMemoryError("CURATION_PLAN_CHANGED: source changed")
                except Exception:
                    with _BoundDirectory(destination.parent, destination.parent) as rollback:
                        rollback.unlink_owned(destination.name, destination_identity)
                    raise
        except (FeedbackError, OSError) as exc:
            raise OpcMemoryError(
                "curation transaction failed without an owned partial transition"
            ) from exc
        final = self._load_record(destination)
        return {
            **preview,
            "dry_run": False,
            "changed": True,
            "canonical_citation_pending_commit": {
                "record_id": final["id"],
                "source_path": preview["destination_path"],
            },
            "git_commit_required": True,
            "git_stage_pathspecs": preview["transition_paths"],
            "provider_write_performed": False,
        }

    def git_audit(self) -> dict[str, Any]:
        """Report Git provenance without staging, committing, or changing files."""
        legacy_artifacts = self.legacy_runtime_artifacts()
        top_text = _git(self.root, ("rev-parse", "--show-toplevel"))
        if not isinstance(top_text, str) or not top_text:
            warnings = ["KNOWLEDGE_NOT_GIT"]
            if legacy_artifacts:
                warnings.append("LEGACY_RUNTIME_ARTIFACTS")
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
                "legacy_runtime_artifacts": legacy_artifacts,
                "warning_codes": warnings,
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
            path
            for path in dirty_paths
            if self._is_authoritative_path(path)
            and not self._is_legacy_runtime_path(path)
        ]
        warning_codes: list[str] = []
        if repo_root != self.root:
            warning_codes.append("KNOWLEDGE_ROOT_NOT_REPO_ROOT")
        if not isinstance(head, str) or not head:
            warning_codes.append("GIT_HEAD_MISSING")
        if authoritative:
            warning_codes.append("UNCOMMITTED_KNOWLEDGE")
        if legacy_artifacts:
            warning_codes.append("LEGACY_RUNTIME_ARTIFACTS")
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
            "legacy_runtime_artifacts": legacy_artifacts,
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
                    record = self._load_record(path)
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
        legacy_artifacts = list(git_report["legacy_runtime_artifacts"])
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
            "legacy_runtime": {
                "detected": bool(legacy_artifacts),
                "artifact_count": len(legacy_artifacts),
                "paths": legacy_artifacts,
                "contents_inspected": False,
                "source_provenance": "unresolved_historical",
                "action": (
                    "Run `legacy-events --dry-run` and review the redacted archive plan; "
                    "moving data requires a separate --apply with the returned plan token."
                    if legacy_artifacts
                    else None
                ),
            },
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
        role: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
        at: str | None = None,
    ) -> list[dict[str, Any]]:
        if not approved_only:
            return self.backend.query(
                text,
                approved_only=False,
                memory_type=memory_type,
                metadata=metadata,
                keywords=keywords,
                project_id=project_id,
                limit=limit,
            )
        return self.query_context(
            text,
            memory_type=memory_type,
            metadata=metadata,
            keywords=keywords,
            project_id=project_id,
            limit=limit,
            role=role,
            applicability=applicability,
            allowed_sensitivity=allowed_sensitivity,
            at=at,
        )["records"]

    def query_context(
        self,
        text: str = "",
        *,
        memory_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        keywords: Sequence[str] | None = None,
        project_id: str | None = None,
        limit: int = 20,
        role: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        semantic_ids: list[str] = []
        if (
            not self.mem0_enabled
            or not text.strip()
            or isinstance(self.provider, Mem0Provider)
            and self.provider.supported_version() is False
        ):
            return self.backend.query_context(
                text,
                memory_type=memory_type,
                metadata=metadata,
                keywords=keywords,
                project_id=project_id,
                limit=limit,
                role=role,
                applicability=applicability,
                allowed_sensitivity=allowed_sensitivity,
                at=at,
            )
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
                        approved_only=True,
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
                # Provider scores and order are intentionally discarded.  The
                # canonical File/Git pass below owns hard filters and ordering.
                semantic_ids.append(str(record["id"]))
        except Exception:
            semantic_ids = []
        context = self.backend.query_context(
            text,
            memory_type=memory_type,
            metadata=metadata,
            keywords=keywords,
            project_id=project_id,
            limit=limit,
            role=role,
            applicability=applicability,
            allowed_sensitivity=allowed_sensitivity,
            at=at,
            extra_candidate_ids=semantic_ids,
        )
        semantic_set = set(semantic_ids)
        for record in context["records"]:
            if record.get("id") in semantic_set:
                record["_recall_source"] = "mem0"
                record["_authority"] = "file-git"
        return context

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
        role: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
    ) -> str:
        context = self.query_context(
            query,
            memory_type=memory_type,
            project_id=project_id,
            limit=limit,
            role=role,
            applicability=applicability,
            allowed_sensitivity=allowed_sensitivity,
        )
        records = context["records"]
        lines = ["# OPC decision context", ""]
        if not records and not context["conflicts"]:
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
        if context["conflicts"]:
            lines.extend(["## Unresolved knowledge conflicts", ""])
            for conflict in context["conflicts"]:
                lines.append(
                    "- `unresolved_conflict`: "
                    + " <> ".join(
                        f"`{item['source_path']}@{item['source_commit']}#{item['content_sha256']}`"
                        for item in conflict["citations"]
                    )
                )
            lines.extend(
                [
                    "",
                    "Conflicting bodies were withheld; manager curation is required.",
                    "",
                ]
            )
        if context["omitted_summary"]["count"]:
            lines.append(
                "- Omitted reason codes: "
                + ", ".join(context["omitted_summary"]["reason_codes"])
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
        legacy_artifacts = list(git_report["legacy_runtime_artifacts"])
        return {
            "knowledge_root": str(self.backend.root),
            "data_root": str(self.data_root),
            "authority": "file-git",
            "knowledge_git": git_report,
            "warnings": list(git_report["warning_codes"]),
            "legacy_runtime": {
                "detected": bool(legacy_artifacts),
                "artifact_count": len(legacy_artifacts),
                "paths": legacy_artifacts,
                "contents_inspected": False,
                "action": (
                    "Run `legacy-events --dry-run`; apply requires the unchanged "
                    "preview token and explicit approval."
                    if legacy_artifacts
                    else None
                ),
            },
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

    legacy_events = commands.add_parser(
        "legacy-events",
        help="Preview or explicitly archive legacy runtime events outside knowledge",
    )
    _plan_mode(legacy_events)
    legacy_events.add_argument(
        "--plan-token",
        help="Unchanged approval_token from a prior dry-run; required with --apply",
    )

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
    add.add_argument(
        "--sensitivity",
        choices=("public", "internal", "restricted"),
        default="internal",
    )
    add.add_argument("--applicable-role", action="append", default=[])
    add.add_argument("--applicability-json")
    add.add_argument("--valid-from")
    add.add_argument("--valid-until")
    add.add_argument("--relation", action="append")

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
    query.add_argument("--role")
    query.add_argument("--applicability", action="append", default=[])
    query.add_argument(
        "--allow-sensitivity",
        action="append",
        choices=("public", "internal", "restricted"),
    )
    query.add_argument("--limit", type=int, default=20)

    query_context = commands.add_parser(
        "query-context", help="Return governed records, conflicts, and omissions"
    )
    query_context.add_argument("text", nargs="?", default="")
    query_context.add_argument("--type", dest="memory_type")
    query_context.add_argument("--keyword", action="append", default=[])
    query_context.add_argument("--metadata", action="append", default=[])
    query_context.add_argument("--project-id")
    query_context.add_argument("--role")
    query_context.add_argument("--applicability", action="append", default=[])
    query_context.add_argument(
        "--allow-sensitivity",
        action="append",
        choices=("public", "internal", "restricted"),
    )
    query_context.add_argument("--at")
    query_context.add_argument("--limit", type=int, default=20)

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
    export.add_argument("--role")
    export.add_argument("--applicability", action="append", default=[])
    export.add_argument(
        "--allow-sensitivity",
        action="append",
        choices=("public", "internal", "restricted"),
    )
    export.add_argument("--limit", type=int, default=20)

    migrate = commands.add_parser(
        "migrate-schema",
        help="Preview or apply one backed-up Schema 1 to Schema 2 migration",
    )
    _plan_mode(migrate)
    migrate.add_argument("--record-id")
    migrate.add_argument("--backup-root")
    migrate.add_argument("--plan-token")

    curate = commands.add_parser(
        "curate", help="Preview or apply one exact manager-approved curation"
    )
    _plan_mode(curate)
    curate.add_argument("record_id")
    curate.add_argument("--manager-approval", required=True)
    curate.add_argument(
        "--set-status", choices=("candidate", "approved", "rejected", "obsolete")
    )
    curate.add_argument("--validation")
    curate.add_argument("--reason")
    curate.add_argument("--relation", action="append")
    curate.add_argument("--applicability-json")
    curate.add_argument(
        "--sensitivity", choices=("public", "internal", "restricted")
    )
    curate.add_argument("--plan-token")
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
            elif args.command == "legacy-events":
                result = (
                    service.backend.apply_legacy_runtime_plan(
                        service.data_root, plan_token=args.plan_token
                    )
                    if args.apply
                    else service.backend.legacy_runtime_plan(service.data_root)
                )
            elif args.command == "reindex":
                result = (
                    service.reindex_apply(args.limit, force=args.force)
                    if args.apply
                    else service.reindex_plan(args.limit, force=args.force)
                )
            elif args.command == "add-candidate":
                applicability_value = parse_json_object(
                    args.applicability_json,
                    label="applicability constraints",
                ) or {}
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
                    sensitivity=args.sensitivity,
                    applicable_roles=args.applicable_role,
                    applicability=applicability_value,
                    valid_from=args.valid_from,
                    valid_until=args.valid_until,
                    relations=parse_relation_objects(args.relation),
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
                    role=args.role,
                    applicability={
                        key: str(value)
                        for key, value in parse_pairs(args.applicability).items()
                    },
                    allowed_sensitivity=args.allow_sensitivity,
                )
            elif args.command == "query-context":
                result = service.query_context(
                    args.text,
                    memory_type=args.memory_type,
                    metadata=parse_pairs(args.metadata),
                    keywords=args.keyword,
                    project_id=args.project_id,
                    limit=args.limit,
                    role=args.role,
                    applicability={
                        key: str(value)
                        for key, value in parse_pairs(args.applicability).items()
                    },
                    allowed_sensitivity=args.allow_sensitivity,
                    at=args.at,
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
                        role=args.role,
                        applicability={
                            key: str(value)
                            for key, value in parse_pairs(args.applicability).items()
                        },
                        allowed_sensitivity=args.allow_sensitivity,
                    ),
                    end="",
                )
                return 0
            elif args.command == "migrate-schema":
                backup_root = Path(args.backup_root) if args.backup_root else None
                if args.apply:
                    if not args.record_id or backup_root is None:
                        raise OpcMemoryError(
                            "migration apply requires --record-id and --backup-root"
                        )
                    result = service.backend.apply_schema_migration(
                        record_id=args.record_id,
                        backup_root=backup_root,
                        plan_token=args.plan_token,
                    )
                else:
                    result = service.backend.schema_migration_plan(
                        record_id=args.record_id,
                        backup_root=backup_root,
                    )
            elif args.command == "curate":
                values = {
                    "manager_approval": args.manager_approval,
                    "set_status": args.set_status,
                    "validation": args.validation,
                    "reason": args.reason,
                    "relations": parse_relation_objects(args.relation),
                    "applicability": parse_json_object(
                        args.applicability_json,
                        label="applicability",
                    ),
                    "sensitivity": args.sensitivity,
                }
                result = (
                    service.backend.apply_curation(
                        args.record_id,
                        plan_token=args.plan_token,
                        **values,
                    )
                    if args.apply
                    else service.backend.curation_plan(args.record_id, **values)
                )
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
