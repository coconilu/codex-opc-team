#!/usr/bin/env python3
"""Record private, portable structured feedback without promoting knowledge.

The authoritative record is a project-local ``.opc/feedback`` sidecar.  This
module never writes canonical knowledge, invokes Git, indexes data, publishes,
or communicates externally.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from opc_memory import OpcMemoryError, load_json, utc_now
from opc_sensitive import sensitive_text_label


SCHEMA_VERSION = "opc-structured-feedback-v1"
CONTRACT_VERSION = "opc-structured-feedback-contract-v1"
VIEW_VERSION = "opc-structured-feedback-view-v1"
MAX_EVENTS = 200
MAX_SUMMARY_LENGTH = 500
MAX_REFS = 20
MAX_ID_LENGTH = 128
MAX_REF_LENGTH = 240
MAX_TIMESTAMP_LENGTH = 32
MAX_EVENT_FILE_BYTES = 64 * 1024
MAX_SIDECAR_BYTES = 512 * 1024

PORTABLE_PROJECT = re.compile(r"^[A-Za-z0-9._-]+$")
PORTABLE_RUN = re.compile(r"^opc-[A-Za-z0-9._-]+$")
PORTABLE_EVENT = re.compile(r"^feedback-[A-Za-z0-9._-]+$")
PORTABLE_CANDIDATE = re.compile(r"^exp-[A-Za-z0-9._-]+$")
PORTABLE_REF = re.compile(
    r"^(?!/)(?![A-Za-z]:)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
UUID_TOKEN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
RUNTIME_ID = re.compile(r"(?i)(?:session|turn|thread)[._ -]?id")
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:^|[\s\"'])(?:[A-Z]:[\\/]|\\\\)")
POSIX_ABSOLUTE = re.compile(r"(?:^|[\s\"'])/(?:home|Users|tmp|var|etc|opt)/")
URL = re.compile(r"(?i)\b(?:https?|file|ssh)://")
RAW_PAYLOAD = re.compile(
    r"(?i)(?:\braw[-_ ]?(?:chat|conversation|hook|payload)\b|"
    r"\bhook[-_ ]?payload\b|"
    r"\btool[-_ ]?call[-_ ]?id\b|\bmessages\s*[:=]\s*\[)"
)
CREDENTIAL_FIELD = re.compile(
    r"(?i)\b(?:api[-_ ]?key|access[-_ ]?token|token|password|secret)\s*[:=]"
)

EVENT_CATEGORIES = {
    "confirmed_outcome",
    "manager_judgment",
    "independent_qa_evidence",
    "hypothesis",
    "unverified",
}
OUTCOME_STATUSES = {"pass", "fail", "partial", "unknown", "not_applicable"}
MANAGER_JUDGMENTS = {
    "accepted",
    "changes_requested",
    "mixed",
    "neutral",
    "unknown",
    "not_applicable",
}
QA_STATUSES = {"pass", "fail", "partial", "unknown", "not_applicable"}
METRIC_IDS = {
    "manager_intervention_rate",
    "qa_catch_rate",
    "rework_loops_per_task",
    "valid_knowledge_reuse_rate",
    "false_recall_rate",
    "scope_leakage_acceptances",
    "stale_obsolete_acceptances",
    "context_tokens_per_task",
    "latency_ms",
}

EVENT_KEYS = {
    "event_id",
    "recorded_at",
    "category",
    "epistemic_status",
    "summary",
    "outcome_status",
    "manager_judgment",
    "qa_status",
    "references",
}
REFERENCE_KEYS = {
    "project_id",
    "run_id",
    "candidate_ids",
    "metric_refs",
    "artifact_refs",
}
METRIC_REF_KEYS = {"metric_id", "aggregate_ref", "aggregate_sha256", "interpretation"}
RECORD_KEYS = {
    "schema_version",
    "contract_version",
    "project_ref",
    "run_ref",
    "revision",
    "created_at",
    "updated_at",
    "events",
}


class FeedbackError(OpcMemoryError):
    """A fail-closed structured-feedback error."""


def _strict_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise FeedbackError("JSON input exceeds the configured size limit")
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise FeedbackError("JSON input exceeds the configured size limit")
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except FileNotFoundError as exc:
        raise FeedbackError(f"Missing required file: {path}") from exc
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise FeedbackError(f"Invalid strict JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FeedbackError(f"Expected a JSON object in {path}")
    _reject_non_finite(value)
    return value


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise FeedbackError("non-finite numbers are forbidden")
    if isinstance(value, dict):
        for child in value.values():
            _reject_non_finite(child)
    elif isinstance(value, list):
        for child in value:
            _reject_non_finite(child)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FeedbackError(f"{label} fields mismatch; missing={missing}, extra={extra}")


def _portable(value: Any, pattern: re.Pattern[str], label: str) -> str:
    max_length = MAX_REF_LENGTH if pattern is PORTABLE_REF else MAX_ID_LENGTH
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or not pattern.fullmatch(value)
    ):
        raise FeedbackError(f"{label} is not a portable identifier")
    if UUID_TOKEN.search(value):
        raise FeedbackError(f"{label} must not contain a UUID runtime token")
    if RUNTIME_ID.search(value):
        raise FeedbackError(f"{label} must not contain a session/turn/thread runtime identifier")
    return value


def _date_time(value: Any, label: str) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) > MAX_TIMESTAMP_LENGTH
        or not UTC_TIMESTAMP.fullmatch(value)
    ):
        raise FeedbackError(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise FeedbackError(f"{label} is not a valid timestamp") from exc
    if parsed.utcoffset() is None:
        raise FeedbackError(f"{label} must include UTC")
    return parsed


def _safe_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise FeedbackError(f"{label} must be a string")
    if not value or len(value) > MAX_SUMMARY_LENGTH or value.strip() != value:
        raise FeedbackError(f"{label} must be 1..{MAX_SUMMARY_LENGTH} trimmed characters")
    if "\n" in value or "\r" in value or any(ord(character) < 32 for character in value):
        raise FeedbackError(f"{label} must be one printable line")
    checks = {
        "runtime identifier": RUNTIME_ID,
        "UUID runtime token": UUID_TOKEN,
        "host-specific Windows path": WINDOWS_ABSOLUTE,
        "host-specific POSIX path": POSIX_ABSOLUTE,
        "URL": URL,
        "raw chat or Hook payload marker": RAW_PAYLOAD,
        "credential field": CREDENTIAL_FIELD,
    }
    for reason, pattern in checks.items():
        if pattern.search(value):
            raise FeedbackError(f"{label} contains forbidden {reason}")
    if sensitive_text_label(value) is not None:
        raise FeedbackError(f"{label} contains forbidden credential material")
    return value


def _bounded_unique_strings(
    values: Any, pattern: re.Pattern[str], label: str
) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_REFS:
        raise FeedbackError(f"{label} must be an array with at most {MAX_REFS} items")
    normalized = [_portable(value, pattern, label) for value in values]
    if len(normalized) != len(set(normalized)):
        raise FeedbackError(f"{label} must contain unique values")
    return normalized


def _validate_metric_ref(value: Any) -> None:
    if not isinstance(value, dict):
        raise FeedbackError("metric reference must be an object")
    _exact_keys(value, METRIC_REF_KEYS, "metric reference")
    if value["metric_id"] not in METRIC_IDS:
        raise FeedbackError("metric reference uses an unknown v1 metric id")
    _portable(value["aggregate_ref"], PORTABLE_REF, "aggregate_ref")
    if not isinstance(value["aggregate_sha256"], str) or not SHA256.fullmatch(
        value["aggregate_sha256"]
    ):
        raise FeedbackError("aggregate_sha256 must be a lowercase SHA-256")
    if value["interpretation"] not in {"supporting", "conflicting", "unknown"}:
        raise FeedbackError("metric interpretation is invalid")


def validate_event(event: Mapping[str, Any], *, project_id: str, run_id: str) -> None:
    if not isinstance(event, dict):
        raise FeedbackError("feedback event must be an object")
    _exact_keys(event, EVENT_KEYS, "feedback event")
    _portable(event["event_id"], PORTABLE_EVENT, "event_id")
    _date_time(event["recorded_at"], "recorded_at")
    category = event["category"]
    if category not in EVENT_CATEGORIES or event["epistemic_status"] != category:
        raise FeedbackError("category and epistemic_status must match a supported evidence class")
    _safe_text(event["summary"], "summary")
    if event["outcome_status"] not in OUTCOME_STATUSES:
        raise FeedbackError("outcome_status is invalid")
    if event["manager_judgment"] not in MANAGER_JUDGMENTS:
        raise FeedbackError("manager_judgment is invalid")
    if event["qa_status"] not in QA_STATUSES:
        raise FeedbackError("qa_status is invalid")

    refs = event["references"]
    if not isinstance(refs, dict):
        raise FeedbackError("references must be an object")
    _exact_keys(refs, REFERENCE_KEYS, "references")
    if refs["project_id"] != project_id or refs["run_id"] != run_id:
        raise FeedbackError("feedback references do not match the active project and run")
    _portable(refs["project_id"], PORTABLE_PROJECT, "project_id")
    _portable(refs["run_id"], PORTABLE_RUN, "run_id")
    _bounded_unique_strings(refs["candidate_ids"], PORTABLE_CANDIDATE, "candidate_ids")
    artifacts = _bounded_unique_strings(refs["artifact_refs"], PORTABLE_REF, "artifact_refs")
    metric_refs = refs["metric_refs"]
    if not isinstance(metric_refs, list) or len(metric_refs) > MAX_REFS:
        raise FeedbackError(f"metric_refs must have at most {MAX_REFS} items")
    for metric_ref in metric_refs:
        _validate_metric_ref(metric_ref)
    metric_keys = [(item["metric_id"], item["aggregate_ref"]) for item in metric_refs]
    if len(metric_keys) != len(set(metric_keys)):
        raise FeedbackError("metric references must be unique")

    expected = {
        "confirmed_outcome": (event["outcome_status"] in {"pass", "fail", "partial"}, True),
        "manager_judgment": (event["manager_judgment"] != "not_applicable", True),
        "independent_qa_evidence": (event["qa_status"] != "not_applicable", bool(artifacts)),
        "hypothesis": (
            event["outcome_status"] == "not_applicable"
            and event["manager_judgment"] == "not_applicable"
            and event["qa_status"] == "not_applicable",
            True,
        ),
        "unverified": (event["outcome_status"] == "unknown", True),
    }[category]
    if not all(expected):
        raise FeedbackError(f"{category} has contradictory status or evidence fields")
    if category != "confirmed_outcome" and category != "unverified" and event["outcome_status"] != "not_applicable":
        raise FeedbackError("only outcome or unverified events may set outcome_status")
    if category != "manager_judgment" and event["manager_judgment"] != "not_applicable":
        raise FeedbackError("only manager_judgment events may set manager_judgment")
    if category != "independent_qa_evidence" and event["qa_status"] != "not_applicable":
        raise FeedbackError("only independent_qa_evidence events may set qa_status")


def validate_record(record: Mapping[str, Any]) -> None:
    if not isinstance(record, dict):
        raise FeedbackError("feedback record must be an object")
    _exact_keys(record, RECORD_KEYS, "feedback record")
    if record["schema_version"] != SCHEMA_VERSION or record["contract_version"] != CONTRACT_VERSION:
        raise FeedbackError("unsupported feedback schema or contract version; migrate explicitly")
    project_id = _portable(record["project_ref"], PORTABLE_PROJECT, "project_ref")
    run_id = _portable(record["run_ref"], PORTABLE_RUN, "run_ref")
    revision = record["revision"]
    events = record["events"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise FeedbackError("revision must be a non-negative integer")
    if not isinstance(events, list) or len(events) > MAX_EVENTS or revision != len(events):
        raise FeedbackError("revision must equal the bounded immutable event count")
    created = _date_time(record["created_at"], "created_at")
    updated = _date_time(record["updated_at"], "updated_at")
    if updated < created:
        raise FeedbackError("updated_at cannot precede created_at")
    identifiers: set[str] = set()
    previous: datetime | None = None
    for event in events:
        validate_event(event, project_id=project_id, run_id=run_id)
        event_id = event["event_id"]
        if event_id in identifiers:
            raise FeedbackError("event ids must be unique")
        identifiers.add(event_id)
        recorded = _date_time(event["recorded_at"], "recorded_at")
        if previous is not None and recorded < previous:
            raise FeedbackError("events must be appended in recorded_at order")
        previous = recorded
    if previous is not None and previous > updated:
        raise FeedbackError("updated_at cannot precede the latest recorded event")


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_private_containment(project_root: Path, target: Path) -> Path:
    project = project_root.expanduser().resolve(strict=True)
    plugin = Path(__file__).resolve().parents[1]
    try:
        project.relative_to(plugin)
        raise FeedbackError("project runtime must not be stored inside the installed plugin tree")
    except ValueError:
        pass
    try:
        plugin.relative_to(project)
        raise FeedbackError("project runtime must not contain the installed plugin tree")
    except ValueError:
        pass
    candidate = target.resolve(strict=False)
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise FeedbackError("feedback path escapes the private project boundary") from exc
    current = project
    for part in target.relative_to(project).parts[:-1]:
        current = current / part
        if current.exists() and (current.is_symlink() or _is_reparse(current)):
            raise FeedbackError("feedback path crosses a symlink or reparse boundary")
    if target.exists() and (target.is_symlink() or _is_reparse(target)):
        raise FeedbackError("feedback record must not be a symlink or reparse point")
    if target.exists() and target.is_file() and target.lstat().st_nlink != 1:
        raise FeedbackError("feedback record must not be a hard-linked file")
    return project


def _context(project_root: Path, run_id: str | None = None) -> tuple[Path, str, str, Path]:
    project = project_root.expanduser().resolve(strict=True)
    project_record = load_json(project / ".opc" / "project.json")
    run_record = load_json(project / ".opc" / "run.json")
    project_id = _portable(project_record.get("project_id"), PORTABLE_PROJECT, "project_id")
    active_run_id = _portable(run_record.get("run_id"), PORTABLE_RUN, "run_id")
    if run_record.get("project_id") != project_id:
        raise FeedbackError("run.project_id does not match project.project_id")
    selected_run_id = active_run_id if run_id is None else _portable(run_id, PORTABLE_RUN, "run_id")
    path = project / ".opc" / "feedback" / f"{selected_run_id}.json"
    _assert_private_containment(project, path)
    if selected_run_id != active_run_id and not path.is_file():
        raise FeedbackError(
            "historical run feedback requires an existing sidecar created while that run was current"
        )
    return project, project_id, selected_run_id, path


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(getattr(value, "st_file_attributes", 0)),
    )


class _BoundDirectory:
    """Hold one directory object for every child operation in a transaction."""

    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.project_root = project_root
        self.fd: int | None = None
        self.windows_handle: int | None = None
        self.token: tuple[int, int, int, int] | None = None

    def __enter__(self) -> "_BoundDirectory":
        self.path.mkdir(parents=True, exist_ok=True)
        _assert_private_containment(self.project_root, self.path / "placeholder")
        if os.name == "nt":
            self._open_windows_directory()
            metadata = self.path.lstat()
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            self.fd = os.open(self.path, flags)
            metadata = os.fstat(self.fd)
        if not stat.S_ISDIR(metadata.st_mode) or self.path.is_symlink() or _is_reparse(self.path):
            self.close()
            raise FeedbackError("feedback parent is not a stable private directory")
        self.token = _directory_identity(metadata)
        self.verify_current()
        return self

    def _open_windows_directory(self) -> None:
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(self.path),
            0x1 | 0x80,
            0x1 | 0x2,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise FeedbackError("feedback parent could not be bound safely")
        self.windows_handle = int(handle)

    def _windows_bound_path(self) -> Path:
        """Resolve the directory object's current name, not the original path."""
        if self.windows_handle is None:
            raise FeedbackError("feedback parent Windows handle is unavailable")
        import ctypes
        from ctypes import wintypes

        get_name = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_name.restype = wintypes.DWORD
        size = get_name(self.windows_handle, None, 0, 0)
        if size == 0:
            raise FeedbackError("feedback parent object name is unavailable")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = get_name(self.windows_handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise FeedbackError("feedback parent object name is unavailable")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def _operation_path(self, name: str) -> Path:
        parent = self._windows_bound_path() if self.windows_handle is not None else self.path
        return parent / name

    def verify_current(self) -> None:
        if self.token is None:
            raise FeedbackError("feedback parent is not bound")
        _assert_private_containment(self.project_root, self.path / "placeholder")
        try:
            current = self.path.lstat()
        except OSError as exc:
            raise FeedbackError("feedback parent changed during the update") from exc
        if self.path.is_symlink() or _is_reparse(self.path) or _directory_identity(current) != self.token:
            raise FeedbackError("feedback parent changed during the update; refusing TOCTOU write")

    def _child_stat(self, name: str) -> os.stat_result:
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return self._operation_path(name).lstat()

    def child_identity(self, name: str) -> tuple[int, int, int, int, int] | None:
        try:
            metadata = self._child_stat(name)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise FeedbackError("feedback child is a symlink or reparse point")
        return _file_identity(metadata)

    def open_exclusive(self, name: str) -> int:
        self.verify_current()
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        if self.fd is not None:
            descriptor = os.open(name, flags, 0o600, dir_fd=self.fd)
        else:
            descriptor = os.open(self._operation_path(name), flags, 0o600)
        identity = _file_identity(os.fstat(descriptor))
        try:
            self.verify_current()
        except Exception:
            os.close(descriptor)
            self.unlink_owned(name, identity)
            raise
        return descriptor

    def read_bytes(self, name: str, *, max_bytes: int) -> bytes:
        self.verify_current()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = (
            os.open(name, flags, dir_fd=self.fd)
            if self.fd is not None
            else os.open(self._operation_path(name), flags)
        )
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size > max_bytes or not stat.S_ISREG(metadata.st_mode):
                raise FeedbackError("feedback sidecar exceeds the configured size limit")
            raw = os.read(descriptor, max_bytes + 1)
            if len(raw) > max_bytes:
                raise FeedbackError("feedback sidecar exceeds the configured size limit")
            self.verify_current()
            return raw
        finally:
            os.close(descriptor)

    def link(self, source: str, destination: str) -> tuple[int, int, int, int, int]:
        self.verify_current()
        if self.fd is not None:
            os.link(
                source,
                destination,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        else:
            os.link(
                self._operation_path(source),
                self._operation_path(destination),
                follow_symlinks=False,
            )
        identity = self.child_identity(destination)
        if identity is None:
            raise FeedbackError("feedback backup disappeared")
        try:
            self.verify_current()
        except Exception:
            self.unlink_owned(destination, identity)
            raise
        return identity

    def replace(
        self,
        source: str,
        destination: str,
        *,
        expected_source: tuple[int, int, int, int, int],
        require_current: bool = True,
    ) -> None:
        if require_current:
            self.verify_current()
        if self.child_identity(source) != expected_source:
            raise FeedbackError("feedback temporary file identity changed")
        if self.fd is not None:
            os.replace(source, destination, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.replace(self._operation_path(source), self._operation_path(destination))

    def unlink_owned(
        self, name: str, expected: tuple[int, int, int, int, int] | None
    ) -> bool:
        if expected is None or self.child_identity(name) != expected:
            return False
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(self._operation_path(name))
        return True

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.windows_handle is not None:
            import ctypes
            from ctypes import wintypes

            close_handle = ctypes.windll.kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            close_handle(self.windows_handle)
            self.windows_handle = None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _verify_checkpoint(bound: _BoundDirectory, label: str) -> None:
    """Name security-relevant identity checks so every write phase is testable."""
    del label
    bound.verify_current()


def _decode_json(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise FeedbackError(f"Invalid strict JSON in {label}") from exc
    if not isinstance(value, dict):
        raise FeedbackError(f"Expected a JSON object in {label}")
    _reject_non_finite(value)
    return value


@contextmanager
def _exclusive_update_lock(
    bound: _BoundDirectory, target_name: str
) -> Iterator[None]:
    lock_name = target_name + ".lock"
    nonce = secrets.token_hex(32)
    descriptor: int | None = None
    identity: tuple[int, int, int, int, int] | None = None
    try:
        descriptor = bound.open_exclusive(lock_name)
        os.write(descriptor, nonce.encode("ascii"))
        os.fsync(descriptor)
        identity = _file_identity(os.fstat(descriptor))
        os.close(descriptor)
        descriptor = None
        bound.verify_current()
        yield
    except FileExistsError as exc:
        raise FeedbackError("feedback update is already locked; refusing concurrent overwrite") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        bound.unlink_owned(lock_name, identity)


def _atomic_write_feedback(
    bound: _BoundDirectory,
    target_name: str,
    value: Mapping[str, Any],
) -> None:
    operation = secrets.token_hex(24)
    pending_name = f"{target_name}.pending-{operation}"
    backup_name = f"{target_name}.backup-{operation}"
    pending_identity: tuple[int, int, int, int, int] | None = None
    backup_identity: tuple[int, int, int, int, int] | None = None
    published = False
    had_original = bound.child_identity(target_name) is not None
    descriptor: int | None = None
    try:
        if had_original:
            backup_identity = bound.link(target_name, backup_name)
        _verify_checkpoint(bound, "before_pending_creation")
        descriptor = bound.open_exclusive(pending_name)
        pending_identity = _file_identity(os.fstat(descriptor))
        _verify_checkpoint(bound, "after_pending_creation")
        payload = (json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_SIDECAR_BYTES:
            raise FeedbackError("feedback sidecar exceeds the configured size limit")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            pending_identity = _file_identity(os.fstat(handle.fileno()))
        _verify_checkpoint(bound, "before_replace")
        bound.replace(
            pending_name,
            target_name,
            expected_source=pending_identity,
        )
        published = True
        _verify_checkpoint(bound, "after_replace")
        _verify_checkpoint(bound, "before_final_cleanup")
        if backup_identity is not None and not bound.unlink_owned(backup_name, backup_identity):
            raise FeedbackError("feedback backup identity changed during cleanup")
        backup_identity = None
    except Exception:
        if published:
            if had_original and backup_identity is not None:
                bound.replace(
                    backup_name,
                    target_name,
                    expected_source=backup_identity,
                    require_current=False,
                )
                backup_identity = None
            else:
                bound.unlink_owned(target_name, pending_identity)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        bound.unlink_owned(pending_name, pending_identity)
        bound.unlink_owned(backup_name, backup_identity)


def read_feedback(project_root: Path, run_id: str | None = None) -> dict[str, Any]:
    _, project_id, selected_run_id, path = _context(project_root, run_id)
    if not path.exists():
        return {
            "schema_version": VIEW_VERSION,
            "project_ref": project_id,
            "run_ref": selected_run_id,
            "structured_feedback": None,
        }
    record = _strict_json(path, max_bytes=MAX_SIDECAR_BYTES)
    validate_record(record)
    if record["project_ref"] != project_id or record["run_ref"] != selected_run_id:
        raise FeedbackError("stored feedback does not match the project run")
    return {
        "schema_version": VIEW_VERSION,
        "project_ref": project_id,
        "run_ref": selected_run_id,
        "structured_feedback": record,
    }


def record_feedback(
    project_root: Path,
    event: Mapping[str, Any],
    *,
    expected_revision: int,
    run_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise FeedbackError("expected_revision must be a non-negative integer")
    project, project_id, selected_run_id, path = _context(project_root, run_id)
    validate_event(event, project_id=project_id, run_id=selected_run_id)
    with _BoundDirectory(path.parent, project) as bound, _exclusive_update_lock(
        bound, path.name
    ):
        if bound.child_identity(path.name) is not None:
            record = _decode_json(
                bound.read_bytes(path.name, max_bytes=MAX_SIDECAR_BYTES),
                label="feedback sidecar",
            )
            validate_record(record)
            if record["project_ref"] != project_id or record["run_ref"] != selected_run_id:
                raise FeedbackError("stored feedback does not match the project run")
        else:
            timestamp = now or utc_now()
            _date_time(timestamp, "now")
            record = {
                "schema_version": SCHEMA_VERSION,
                "contract_version": CONTRACT_VERSION,
                "project_ref": project_id,
                "run_ref": selected_run_id,
                "revision": 0,
                "created_at": timestamp,
                "updated_at": timestamp,
                "events": [],
            }

        for existing in record["events"]:
            if existing["event_id"] == event["event_id"]:
                if existing != event:
                    raise FeedbackError("event_id already exists with different content")
                return {"idempotent": True, "record": record}
        if record["revision"] != expected_revision:
            raise FeedbackError(
                f"stale feedback revision: expected {expected_revision}, current {record['revision']}"
            )
        if record["revision"] >= MAX_EVENTS:
            raise FeedbackError("feedback record reached its bounded event limit")
        if record["events"] and _date_time(event["recorded_at"], "recorded_at") < _date_time(
            record["events"][-1]["recorded_at"], "recorded_at"
        ):
            raise FeedbackError("new feedback recorded_at precedes the latest audit event")
        timestamp = now or utc_now()
        _date_time(timestamp, "now")
        if _date_time(timestamp, "now") < _date_time(record["updated_at"], "updated_at"):
            raise FeedbackError("feedback update time cannot move backward")
        updated = dict(record)
        updated["events"] = [*record["events"], dict(event)]
        updated["revision"] = record["revision"] + 1
        updated["updated_at"] = timestamp
        validate_record(updated)
        bound.verify_current()
        _atomic_write_feedback(bound, path.name, updated)
        bound.verify_current()
        return {"idempotent": False, "record": updated}


def render_report(view: Mapping[str, Any]) -> str:
    if set(view) != {"schema_version", "project_ref", "run_ref", "structured_feedback"}:
        raise FeedbackError("invalid feedback view")
    if view["schema_version"] != VIEW_VERSION:
        raise FeedbackError("unsupported feedback view")
    lines = [
        "# Structured feedback",
        "",
        f"- Project: `{view['project_ref']}`",
        f"- Run: `{view['run_ref']}`",
    ]
    record = view["structured_feedback"]
    if record is None:
        return "\n".join([*lines, "- Revision: `0`", "", "No structured feedback recorded.", ""])
    validate_record(record)
    lines.extend([f"- Revision: `{record['revision']}`", "", "| Recorded at | Class | Status | Summary |", "|---|---|---|---|"])
    for event in record["events"]:
        status = {
            "confirmed_outcome": event["outcome_status"],
            "manager_judgment": event["manager_judgment"],
            "independent_qa_evidence": event["qa_status"],
            "hypothesis": "hypothesis",
            "unverified": "unknown",
        }[event["category"]]
        summary = html.escape(event["summary"], quote=True).replace("|", "\\|")
        lines.append(f"| {event['recorded_at']} | `{event['category']}` | `{status}` | {summary} |")
    lines.extend(
        [
            "",
            "> Feedback is evaluation input only. It is not candidate approval, publication, or organizational knowledge.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "report"):
        command = commands.add_parser(name)
        command.add_argument("--project-root", required=True)
        command.add_argument("--run-id")
    record = commands.add_parser("record")
    record.add_argument("--project-root", required=True)
    record.add_argument("--run-id")
    record.add_argument("--event-file", required=True)
    record.add_argument("--expected-revision", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project_root = Path(args.project_root)
        if args.command == "record":
            result: Any = record_feedback(
                project_root,
                _strict_json(Path(args.event_file), max_bytes=MAX_EVENT_FILE_BYTES),
                expected_revision=args.expected_revision,
                run_id=args.run_id,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            view = read_feedback(project_root, args.run_id)
            if args.command == "show":
                print(json.dumps(view, ensure_ascii=False, indent=2))
            else:
                print(render_report(view), end="")
        return 0
    except (FeedbackError, OSError) as exc:
        print(f"OPC_FEEDBACK_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
