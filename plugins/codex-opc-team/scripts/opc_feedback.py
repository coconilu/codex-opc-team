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
import stat
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from opc_memory import OpcMemoryError, load_json, utc_now


SCHEMA_VERSION = "opc-structured-feedback-v1"
CONTRACT_VERSION = "opc-structured-feedback-contract-v1"
VIEW_VERSION = "opc-structured-feedback-view-v1"
MAX_EVENTS = 200
MAX_SUMMARY_LENGTH = 500
MAX_REFS = 20

PORTABLE_PROJECT = re.compile(r"^[A-Za-z0-9._-]+$")
PORTABLE_RUN = re.compile(r"^opc-[A-Za-z0-9._-]+$")
PORTABLE_EVENT = re.compile(r"^feedback-[A-Za-z0-9._-]+$")
PORTABLE_CANDIDATE = re.compile(r"^exp-[A-Za-z0-9._-]+$")
PORTABLE_REF = re.compile(
    r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
UUID_TOKEN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
RUNTIME_ID = re.compile(r"(?i)\b(?:session|turn|thread)[-_ ]?id\b")
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:^|[\s\"'])(?:[A-Z]:[\\/]|\\\\)")
POSIX_ABSOLUTE = re.compile(r"(?:^|[\s\"'])/(?:home|Users|tmp|var|etc|opt)/")
URL = re.compile(r"(?i)\b(?:https?|file|ssh)://")
SECRET_MARKER = re.compile(
    r"(?i)(?:authorization\s*[:=]\s*bearer|api[-_ ]?key\s*[:=]|"
    r"access[-_ ]?token\s*[:=]|password\s*[:=]|secret\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
RAW_PAYLOAD = re.compile(
    r"(?i)(?:\braw[-_ ]?(?:chat|conversation|hook|payload)\b|"
    r"\bhook[-_ ]?payload\b|"
    r"\btool[-_ ]?call[-_ ]?id\b|\bmessages\s*[:=]\s*\[)"
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


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
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
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise FeedbackError(f"{label} is not a portable identifier")
    if UUID_TOKEN.search(value):
        raise FeedbackError(f"{label} must not contain a UUID runtime token")
    if RUNTIME_ID.search(value):
        raise FeedbackError(f"{label} must not contain a session/turn/thread runtime identifier")
    return value


def _date_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
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
        "secret or credential marker": SECRET_MARKER,
        "raw chat or Hook payload marker": RAW_PAYLOAD,
    }
    for reason, pattern in checks.items():
        if pattern.search(value):
            raise FeedbackError(f"{label} contains forbidden {reason}")
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


def _directory_token(path: Path) -> tuple[int, int, int, int]:
    metadata = path.lstat()
    if path.is_symlink() or _is_reparse(path) or not stat.S_ISDIR(metadata.st_mode):
        raise FeedbackError("feedback parent is not a stable private directory")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _require_directory_token(path: Path, expected: tuple[int, int, int, int]) -> None:
    if _directory_token(path) != expected:
        raise FeedbackError("feedback parent changed during the update; refusing TOCTOU write")


@contextmanager
def _exclusive_update_lock(path: Path) -> Iterator[tuple[int, int, int, int]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_private_containment(path.parents[2], path)
    parent_token = _directory_token(path.parent)
    lock = path.with_suffix(path.suffix + ".lock")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(lock, flags, 0o600)
        created = True
        os.write(descriptor, b"opc-structured-feedback-v1\n")
        os.close(descriptor)
        descriptor = None
        _assert_private_containment(path.parents[2], path)
        _require_directory_token(path.parent, parent_token)
        yield parent_token
    except FileExistsError as exc:
        raise FeedbackError("feedback update is already locked; refusing concurrent overwrite") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and lock.exists() and not lock.is_symlink():
            lock.unlink()


def _atomic_write_feedback(
    path: Path,
    value: Mapping[str, Any],
    parent_token: tuple[int, int, int, int],
) -> None:
    _require_directory_token(path.parent, parent_token)
    pending = path.with_suffix(path.suffix + ".pending")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(pending, flags, 0o600)
        created = True
        _require_directory_token(path.parent, parent_token)
        payload = (json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _require_directory_token(path.parent, parent_token)
        os.replace(pending, path)
        created = False
        _require_directory_token(path.parent, parent_token)
    except FileExistsError as exc:
        raise FeedbackError("feedback pending file already exists; refusing ambiguous write") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and pending.exists() and not pending.is_symlink():
            pending.unlink()


def read_feedback(project_root: Path, run_id: str | None = None) -> dict[str, Any]:
    _, project_id, selected_run_id, path = _context(project_root, run_id)
    if not path.exists():
        return {
            "schema_version": VIEW_VERSION,
            "project_ref": project_id,
            "run_ref": selected_run_id,
            "structured_feedback": None,
        }
    record = _strict_json(path)
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
    with _exclusive_update_lock(path) as parent_token:
        if path.exists():
            record = _strict_json(path)
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
        _assert_private_containment(project, path)
        _atomic_write_feedback(path, updated, parent_token)
        _assert_private_containment(project, path)
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
                _strict_json(Path(args.event_file)),
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
