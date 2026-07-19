#!/usr/bin/env python3
"""Record bounded, private knowledge lineage without claiming causality.

The persisted artifact is an append-only sidecar under ``.opc/lineage``.  It
contains portable identifiers, hashes, citations, states, and evidence links;
it never contains packet bodies, prompts, conversations, or tool payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from opc_feedback import (
    FeedbackError,
    _BoundDirectory,
    _assert_private_containment,
    _directory_identity,
    _exclusive_update_lock,
    _file_identity,
    _verify_checkpoint,
    validate_record as validate_feedback_record,
)
from opc_hierarchical import (
    PACKET_VERSION,
    TRACE_VERSION,
    HierarchicalError,
    validate_recall_result,
)
from opc_memory import (
    FileGitBackend,
    OpcMemoryError,
    StaleSourceError,
    _assert_unlinked_ancestors,
)
from opc_sensitive import sensitive_text_label


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PLUGIN_ROOT / "assets" / "lineage" / "knowledge-lineage-contract.v1.json"
SCHEMA_VERSION = "opc-knowledge-lineage-v1"
CONTRACT_VERSION = "opc-knowledge-lineage-contract-v1"
VIEW_VERSION = "opc-knowledge-lineage-view-v1"

MAX_EVENT_INPUT_BYTES = 128 * 1024
MAX_LINEAGE_BYTES = 1024 * 1024
MAX_EVENTS = 500
MAX_STATES = 500
MAX_REFS = 20
MAX_REASON_CODES = 20
MAX_ID = 128
MAX_REF = 240
MAX_ROLE = 64
MAX_STEP = 128
MAX_PROVIDER = 64
MAX_TIMESTAMP = 32
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_RECALL_RESULT_BYTES = 2 * 1024 * 1024

PORTABLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
PORTABLE_RUN = re.compile(r"^opc-[A-Za-z0-9._-]+$")
PORTABLE_EVENT = re.compile(r"^lineage-[A-Za-z0-9._-]+$")
PORTABLE_REF = re.compile(
    r"^(?!/)(?![A-Za-z]:)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
UUID_TOKEN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
RUNTIME_ID = re.compile(r"(?i)(?:session|turn|thread)[._ -]?id")

EVENT_TYPES = {"knowledge", "provider", "association"}
KNOWLEDGE_STATES = {
    "recalled", "injected", "adopted", "ignored", "overridden",
    "contradicted", "omitted",
}
PROVIDER_STATES = {"available", "missing", "disabled", "failed", "stale", "no_memory"}
EVIDENCE_KINDS = {"qa", "feedback", "outcome", "shadow", "evaluation"}
TRANSITIONS: dict[str, set[str | None]] = {
    "recalled": {None},
    "injected": {"recalled"},
    "adopted": {"injected"},
    "ignored": {"injected"},
    "overridden": {"injected", "adopted"},
    "contradicted": {"injected", "adopted"},
    "omitted": {None, "recalled", "injected"},
}

RECORD_KEYS = {
    "schema_version", "contract_version", "contract_sha256", "project_ref",
    "run_ref", "revision", "created_at", "updated_at", "events", "states",
}
EVENT_KEYS = {
    "event_id", "sequence", "recorded_at", "event_type", "role", "step_id",
    "project_instance", "run_instance", "context_packet", "knowledge_ref",
    "knowledge_state", "provider", "evidence_refs", "reason_codes",
    "previous_event_id",
}
EVENT_INPUT_KEYS = {
    "event_id", "recorded_at", "event_type", "role", "step_id",
    "knowledge_ref", "knowledge_state", "provider", "evidence_refs",
    "reason_codes", "previous_event_id",
}
INSTANCE_KEYS = {"schema_version", "sha256"}
PACKET_REF_KEYS = {
    "schema_version", "sha256", "query_sha256", "mode",
    "recall_trace_version", "recall_trace_sha256",
}
KNOWLEDGE_REF_KEYS = {
    "record_id", "source_path", "source_commit", "content_sha256", "status",
    "scope", "project_id", "knowledge_type", "sensitivity",
}
PROVIDER_KEYS = {"provider_id", "state", "authoritative"}
EVIDENCE_REF_KEYS = {"kind", "ref", "sha256"}
STATE_KEYS = {"role", "step_id", "knowledge_ref", "state", "last_event_id", "context_packet"}


class LineageError(OpcMemoryError):
    """Fail-closed lineage error whose messages never include input bodies."""


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise LineageError(f"{label} fields are not strict")


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise LineageError("non-finite JSON numbers are forbidden")
    if isinstance(value, Mapping):
        for nested in value.values():
            _reject_non_finite(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_non_finite(nested)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    _reject_non_finite(value)
    try:
        return (
            json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LineageError("lineage value cannot be serialized safely") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_json_bytes(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite")

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LineageError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise LineageError(f"{label} must be a JSON object")
    _reject_non_finite(value)
    return value


def _read_input(path: Path, *, maximum: int, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        candidate = _assert_unlinked_ancestors(path, label=label)
        parent_before = candidate.parent.lstat()
        metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise LineageError(f"{label} must be a single-link regular file")
        if metadata.st_size > maximum:
            raise LineageError(f"{label} exceeds its size limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(metadata):
                raise LineageError(f"{label} changed before it was opened")
            raw = os.read(descriptor, maximum + 1)
        finally:
            os.close(descriptor)
        if len(raw) > maximum:
            raise LineageError(f"{label} exceeds its size limit")
        after = candidate.lstat()
        parent_after = candidate.parent.lstat()
        if (
            _file_identity(after) != _file_identity(metadata)
            or _directory_identity(parent_after) != _directory_identity(parent_before)
        ):
            raise LineageError(f"{label} changed while being read")
    except (FileNotFoundError, OpcMemoryError) as exc:
        raise LineageError(f"{label} is unavailable") from exc
    return _strict_json_bytes(raw, label=label), raw


def _portable(value: Any, pattern: re.Pattern[str], label: str, *, maximum: int = MAX_ID) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or not pattern.fullmatch(value):
        raise LineageError(f"{label} is not portable")
    if UUID_TOKEN.search(value) or RUNTIME_ID.search(value):
        raise LineageError(f"{label} contains a forbidden runtime identifier")
    if sensitive_text_label(value) is not None:
        raise LineageError(f"{label} contains forbidden credential material")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP or not UTC_TIMESTAMP.fullmatch(value):
        raise LineageError(f"{label} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LineageError(f"{label} is invalid") from exc
    return parsed


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise LineageError(f"{label} must be lowercase SHA-256")
    return value


def _load_contract() -> tuple[dict[str, Any], str]:
    contract, raw = _read_input(CONTRACT_PATH, maximum=64 * 1024, label="lineage contract")
    limits = contract.get("limits")
    expected_limits = {
        "event_input_bytes": MAX_EVENT_INPUT_BYTES,
        "lineage_bytes": MAX_LINEAGE_BYTES,
        "events": MAX_EVENTS,
        "states": MAX_STATES,
        "references_per_event": MAX_REFS,
        "reason_codes_per_event": MAX_REASON_CODES,
        "identifier_characters": MAX_ID,
        "portable_reference_characters": MAX_REF,
        "role_characters": MAX_ROLE,
        "step_characters": MAX_STEP,
        "provider_characters": MAX_PROVIDER,
        "timestamp_characters": MAX_TIMESTAMP,
        "evidence_file_bytes": MAX_EVIDENCE_BYTES,
        "context_result_bytes": MAX_RECALL_RESULT_BYTES,
    }
    if (
        contract.get("contract_version") != CONTRACT_VERSION
        or contract.get("schema_version") != SCHEMA_VERSION
        or contract.get("view_version") != VIEW_VERSION
        or contract.get("context_packet_version") != PACKET_VERSION
        or contract.get("recall_trace_version") != TRACE_VERSION
        or contract.get("authority") != "file-git-only"
        or contract.get("causal_claim_allowed") is not False
        or contract.get("report_claim") != "association/evidence only"
        or set(contract.get("event_types", [])) != EVENT_TYPES
        or set(contract.get("knowledge_states", [])) != KNOWLEDGE_STATES
        or set(contract.get("provider_states", [])) != PROVIDER_STATES
        or set(contract.get("evidence_kinds", [])) != EVIDENCE_KINDS
        or limits != expected_limits
    ):
        raise LineageError("lineage contract drifted from the runtime")
    return contract, _sha256(raw)


def _validate_instance(value: Any, label: str) -> None:
    _exact(value, INSTANCE_KEYS, label)
    version = value["schema_version"]
    if isinstance(version, bool) or not (
        isinstance(version, int) and version >= 1
        or isinstance(version, str) and 0 < len(version) <= MAX_ID and PORTABLE_ID.fullmatch(version)
    ):
        raise LineageError(f"{label} schema version is invalid")
    _hash(value["sha256"], f"{label}.sha256")


def _validate_packet_ref(value: Any) -> None:
    if value is None:
        return
    _exact(value, PACKET_REF_KEYS, "context packet reference")
    if value["schema_version"] != PACKET_VERSION or value["recall_trace_version"] != TRACE_VERSION:
        raise LineageError("context packet versions are unsupported")
    for key in ("sha256", "query_sha256", "recall_trace_sha256"):
        _hash(value[key], f"context_packet.{key}")
    if value["mode"] not in {"hierarchical-file-git", "flat-file-git-fallback"}:
        raise LineageError("context packet mode is invalid")


def _validate_knowledge_ref(value: Any) -> None:
    if value is None:
        return
    _exact(value, KNOWLEDGE_REF_KEYS, "knowledge reference")
    _portable(value["record_id"], PORTABLE_ID, "record_id")
    _portable(value["source_path"], PORTABLE_REF, "source_path", maximum=MAX_REF)
    if "\\" in value["source_path"]:
        raise LineageError("source_path must use portable separators")
    if not isinstance(value["source_commit"], str) or not GIT_COMMIT.fullmatch(value["source_commit"]):
        raise LineageError("source_commit is invalid")
    _hash(value["content_sha256"], "content_sha256")
    if value["status"] not in {"candidate", "approved", "rejected", "obsolete"}:
        raise LineageError("knowledge status is invalid")
    if value["scope"] not in {"global", "project"}:
        raise LineageError("knowledge scope is invalid")
    if value["scope"] == "global" and value["project_id"] is not None:
        raise LineageError("global knowledge must not carry project_id")
    if value["scope"] == "project":
        _portable(value["project_id"], PORTABLE_ID, "knowledge project_id")
    _portable(value["knowledge_type"], PORTABLE_ID, "knowledge_type")
    if value["sensitivity"] not in {"public", "internal", "restricted"}:
        raise LineageError("knowledge sensitivity is invalid")


def _validate_provider(value: Any) -> None:
    if value is None:
        return
    _exact(value, PROVIDER_KEYS, "provider status")
    _portable(value["provider_id"], PORTABLE_ID, "provider_id", maximum=MAX_PROVIDER)
    if value["state"] not in PROVIDER_STATES or value["authoritative"] is not False:
        raise LineageError("provider status is invalid or authoritative")


def _validate_evidence_ref(value: Any) -> None:
    _exact(value, EVIDENCE_REF_KEYS, "evidence reference")
    if value["kind"] not in EVIDENCE_KINDS:
        raise LineageError("evidence kind is invalid")
    ref = _portable(value["ref"], PORTABLE_REF, "evidence ref", maximum=MAX_REF)
    if not ref.startswith(".opc/"):
        raise LineageError("runtime evidence must remain below the private .opc boundary")
    _hash(value["sha256"], "evidence sha256")


def _state_key(role: str, step_id: str, ref: Mapping[str, Any]) -> tuple[str, ...]:
    return (role, step_id, ref["record_id"], ref["source_commit"], ref["content_sha256"])


def _derive_states(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    current: dict[tuple[str, ...], dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != "knowledge":
            continue
        ref = event["knowledge_ref"]
        key = _state_key(event["role"], event["step_id"], ref)
        previous = current.get(key)
        previous_state = previous["state"] if previous else None
        expected_previous = previous["last_event_id"] if previous else None
        if event["previous_event_id"] != expected_previous:
            raise LineageError("knowledge event previous_event_id is not the current state event")
        state = event["knowledge_state"]
        if previous_state not in TRANSITIONS[state]:
            raise LineageError("knowledge state transition is invalid")
        current[key] = {
            "role": event["role"],
            "step_id": event["step_id"],
            "knowledge_ref": dict(ref),
            "state": state,
            "last_event_id": event["event_id"],
            "context_packet": dict(event["context_packet"]) if event["context_packet"] else None,
        }
    return [current[key] for key in sorted(current)]


def validate_event(event: Mapping[str, Any], *, project_id: str, run_id: str) -> None:
    _exact(event, EVENT_KEYS, "lineage event")
    _portable(event["event_id"], PORTABLE_EVENT, "event_id")
    if isinstance(event["sequence"], bool) or not isinstance(event["sequence"], int) or not 1 <= event["sequence"] <= MAX_EVENTS:
        raise LineageError("event sequence is invalid")
    _timestamp(event["recorded_at"], "recorded_at")
    if event["event_type"] not in EVENT_TYPES:
        raise LineageError("event type is invalid")
    _portable(event["role"], PORTABLE_ID, "role", maximum=MAX_ROLE)
    _portable(event["step_id"], PORTABLE_ID, "step_id", maximum=MAX_STEP)
    _validate_instance(event["project_instance"], "project instance")
    _validate_instance(event["run_instance"], "run instance")
    _validate_packet_ref(event["context_packet"])
    _validate_knowledge_ref(event["knowledge_ref"])
    _validate_provider(event["provider"])
    refs = event["evidence_refs"]
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise LineageError("evidence refs exceed the bounded limit")
    for ref in refs:
        _validate_evidence_ref(ref)
    ref_keys = [(item["kind"], item["ref"]) for item in refs]
    if len(ref_keys) != len(set(ref_keys)):
        raise LineageError("evidence refs must be unique")
    reasons = event["reason_codes"]
    if not isinstance(reasons, list) or len(reasons) > MAX_REASON_CODES:
        raise LineageError("reason codes exceed the bounded limit")
    normalized = [_portable(item, PORTABLE_ID, "reason code") for item in reasons]
    if normalized != sorted(set(normalized)):
        raise LineageError("reason codes must be sorted and unique")
    if event["previous_event_id"] is not None:
        _portable(event["previous_event_id"], PORTABLE_EVENT, "previous_event_id")

    if event["event_type"] == "knowledge":
        if event["knowledge_ref"] is None or event["knowledge_state"] not in KNOWLEDGE_STATES or event["provider"] is not None:
            raise LineageError("knowledge event fields are contradictory")
        ref = event["knowledge_ref"]
        if event["knowledge_state"] != "omitted" and (
            ref["status"] != "approved"
            or ref["scope"] == "project" and ref["project_id"] != project_id
        ):
            raise LineageError("only currently eligible snapshots may enter non-omitted states")
        if event["knowledge_state"] != "omitted" and event["context_packet"] is None:
            raise LineageError("non-omitted knowledge event requires an exact ContextPacket")
        if event["knowledge_state"] == "omitted" and not reasons:
            raise LineageError("omitted knowledge requires an explicit reason")
    elif event["event_type"] == "provider":
        if event["knowledge_ref"] is not None or event["knowledge_state"] is not None or event["provider"] is None or event["previous_event_id"] is not None:
            raise LineageError("provider event fields are contradictory")
        degraded = event["provider"]["state"] != "available"
        if degraded != bool(reasons):
            raise LineageError("provider degradation must have reasons and availability must not")
    else:
        if event["knowledge_state"] is not None or event["provider"] is not None or event["previous_event_id"] is not None or not refs:
            raise LineageError("association event fields are contradictory")


def validate_record(record: Mapping[str, Any]) -> None:
    _exact(record, RECORD_KEYS, "lineage record")
    _, contract_hash = _load_contract()
    if (
        record["schema_version"] != SCHEMA_VERSION
        or record["contract_version"] != CONTRACT_VERSION
        or record["contract_sha256"] != contract_hash
    ):
        raise LineageError("lineage contract identity is unsupported")
    project_id = _portable(record["project_ref"], PORTABLE_ID, "project_ref")
    run_id = _portable(record["run_ref"], PORTABLE_RUN, "run_ref")
    revision = record["revision"]
    events = record["events"]
    states = record["states"]
    if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= MAX_EVENTS:
        raise LineageError("revision is invalid")
    if not isinstance(events, list) or revision != len(events) or len(events) > MAX_EVENTS:
        raise LineageError("revision must equal the bounded immutable event count")
    if not isinstance(states, list) or len(states) > MAX_STATES:
        raise LineageError("states exceed the bounded limit")
    created = _timestamp(record["created_at"], "created_at")
    updated = _timestamp(record["updated_at"], "updated_at")
    if updated < created:
        raise LineageError("updated_at precedes created_at")
    identifiers: set[str] = set()
    previous_time: datetime | None = None
    for index, event in enumerate(events, start=1):
        validate_event(event, project_id=project_id, run_id=run_id)
        if event["sequence"] != index or event["event_id"] in identifiers:
            raise LineageError("event order or identity is invalid")
        identifiers.add(event["event_id"])
        recorded = _timestamp(event["recorded_at"], "recorded_at")
        if previous_time is not None and recorded < previous_time:
            raise LineageError("events must be appended in timestamp order")
        previous_time = recorded
    if previous_time is not None and previous_time > updated:
        raise LineageError("updated_at precedes the last event")
    expected_states = _derive_states(events)
    if states != expected_states:
        raise LineageError("materialized states differ from deterministic event replay")
    if len(_canonical_bytes(record)) > MAX_LINEAGE_BYTES:
        raise LineageError("lineage record exceeds the configured size limit")


def _read_project_subject(project_root: Path) -> tuple[Path, str, str, dict[str, Any], dict[str, Any]]:
    project = project_root.expanduser().resolve(strict=True)
    _assert_private_containment(project, project / ".opc" / "placeholder")
    with _BoundDirectory(project / ".opc", project) as bound:
        project_raw = bound.read_bytes(
            "project.json", max_bytes=512 * 1024, require_single_link=True, binary=True
        )
        run_raw = bound.read_bytes(
            "run.json", max_bytes=512 * 1024, require_single_link=True, binary=True
        )
    project_record = _strict_json_bytes(project_raw, label="project record")
    run_record = _strict_json_bytes(run_raw, label="run record")
    project_id = _portable(project_record.get("project_id"), PORTABLE_ID, "project_id")
    run_id = _portable(run_record.get("run_id"), PORTABLE_RUN, "run_id")
    if run_record.get("project_id") != project_id:
        raise LineageError("run and project identities differ")
    project_instance = {"schema_version": project_record.get("schema_version"), "sha256": _sha256(project_raw)}
    run_instance = {"schema_version": run_record.get("schema_version"), "sha256": _sha256(run_raw)}
    _validate_instance(project_instance, "project instance")
    _validate_instance(run_instance, "run instance")
    return project, project_id, run_id, project_instance, run_instance


def _lineage_path(project: Path, run_id: str) -> Path:
    path = project / ".opc" / "lineage" / f"{run_id}.json"
    _assert_private_containment(project, path)
    return path


def _assert_private_or_ignored(project: Path, path: Path) -> None:
    """A Git worktree may store lineage only at an ignored .opc path."""

    try:
        top = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return
    if not top:
        return
    root = Path(top).resolve()
    try:
        relative = path.resolve(strict=False).relative_to(root).as_posix()
    except ValueError as exc:
        raise LineageError("lineage path escaped its Git worktree") from exc
    try:
        ignored = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", relative],
            check=False, capture_output=True, timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise LineageError("could not verify the ignored .opc boundary") from exc
    if ignored.returncode != 0:
        raise LineageError("project-local lineage requires an ignored .opc path")


def _read_stored(path: Path, project: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with _BoundDirectory(path.parent, project) as bound:
        raw = bound.read_bytes(
            path.name, max_bytes=MAX_LINEAGE_BYTES, require_single_link=True, binary=True
        )
    record = _strict_json_bytes(raw, label="lineage sidecar")
    validate_record(record)
    return record


def _packet_reference(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    try:
        validate_recall_result(result)
    except HierarchicalError as exc:
        raise LineageError("ContextPacket and RecallTrace joint validation failed") from exc
    packet = result["context_packet"]
    trace = result["recall_trace"]
    return {
        "schema_version": packet["schema_version"],
        "sha256": _sha256(_canonical_bytes(packet)),
        "query_sha256": packet["query_sha256"],
        "mode": packet["mode"],
        "recall_trace_version": trace["schema_version"],
        "recall_trace_sha256": _sha256(_canonical_bytes(trace)),
    }


def _citation_matches(reference: Mapping[str, Any], citation: Mapping[str, Any]) -> bool:
    return all(reference[key] == citation[key] for key in KNOWLEDGE_REF_KEYS)


def _validate_recall_membership(event: Mapping[str, Any], result: Mapping[str, Any] | None) -> None:
    if event["event_type"] not in {"knowledge", "association"} or result is None:
        return
    reference = event.get("knowledge_ref")
    if reference is None:
        return
    packet = result["context_packet"]
    trace = result["recall_trace"]
    citations = packet["citations"]
    state = event.get("knowledge_state")
    if state in {"injected", "adopted", "ignored", "overridden", "contradicted"}:
        if not any(_citation_matches(reference, citation) for citation in citations):
            raise LineageError("knowledge revision is not present in the exact ContextPacket")
    elif state == "recalled":
        recalled_ids = set(trace["canonical_reads"])
        recalled_ids.update(
            item.get("record_id") for item in trace["discards"] if isinstance(item, Mapping)
        )
        if reference["record_id"] not in recalled_ids:
            raise LineageError("knowledge revision is not present in the exact RecallTrace")
    elif state == "omitted":
        injected_ids = {item["record_id"] for item in citations}
        if reference["record_id"] in injected_ids:
            raise LineageError("an injected packet citation cannot be recorded as omitted")


def _read_evidence(project: Path, project_id: str, run_id: str, reference: Mapping[str, Any]) -> None:
    _validate_evidence_ref(reference)
    relative = Path(reference["ref"])
    target = project / relative
    _assert_private_containment(project, target)
    if not target.is_file():
        raise LineageError("evidence reference is unavailable")
    with _BoundDirectory(target.parent, project) as bound:
        raw = bound.read_bytes(
            target.name, max_bytes=MAX_EVIDENCE_BYTES, require_single_link=True, binary=True
        )
    if _sha256(raw) != reference["sha256"]:
        raise LineageError("evidence reference hash is stale")
    kind = reference["kind"]
    if kind in {"feedback", "outcome"}:
        feedback = _strict_json_bytes(raw, label="feedback evidence")
        try:
            validate_feedback_record(feedback)
        except FeedbackError as exc:
            raise LineageError("feedback evidence is invalid") from exc
        if feedback["project_ref"] != project_id or feedback["run_ref"] != run_id:
            raise LineageError("feedback evidence belongs to another project or run")
        if kind == "outcome" and not any(event["category"] == "confirmed_outcome" for event in feedback["events"]):
            raise LineageError("outcome reference does not contain a confirmed outcome")
    elif kind == "shadow":
        try:
            from opc_shadow import ShadowError, validate_result as validate_shadow_result

            validate_shadow_result(_strict_json_bytes(raw, label="shadow evidence"))
        except (ShadowError, LineageError) as exc:
            raise LineageError("shadow evidence is invalid") from exc


def _validate_current_reference(
    backend: FileGitBackend,
    reference: Mapping[str, Any],
    *,
    project_id: str,
    role: str,
) -> list[str]:
    reasons: list[str] = []
    if reference["status"] != "approved":
        reasons.append("obsolete_or_nonapproved")
    if reference["scope"] == "project" and reference["project_id"] != project_id:
        reasons.append("cross_project_scope")
    if reasons:
        return reasons
    try:
        record = backend.read_authoritative(
            source_path=reference["source_path"],
            content_hash=reference["content_sha256"],
            source_commit=reference["source_commit"],
            approved_only=True,
        )
    except (OpcMemoryError, StaleSourceError, OSError):
        return ["stale_provenance"]
    expected = {
        "record_id": record.get("id"),
        "status": record.get("status"),
        "scope": record.get("scope"),
        "project_id": record.get("project_id"),
        "knowledge_type": record.get("type"),
        "sensitivity": record.get("sensitivity", "internal"),
    }
    if any(reference[key] != value for key, value in expected.items()):
        return ["canonical_metadata_changed"]
    try:
        context = backend.query_context(
            "",
            project_id=project_id,
            role=role,
            allowed_sensitivity=("public", "internal", "restricted"),
            extra_candidate_ids=(reference["record_id"],),
            limit=1,
        )
    except (OpcMemoryError, OSError):
        return ["governance_revalidation_failed"]
    if any(record.get("id") == reference["record_id"] for record in context["records"]):
        return []
    if any(
        reference["record_id"] in {citation["record_id"] for citation in conflict["citations"]}
        for conflict in context["conflicts"]
    ):
        return ["unresolved_conflict"]
    omission_reasons = {
        reason
        for omission in context.get("omissions", [])
        if omission.get("record_id") in {None, reference["record_id"]}
        for reason in omission.get("reason_codes", [])
    }
    return sorted(omission_reasons) or ["governance_ineligible"]


def _build_event(
    event_input: Mapping[str, Any],
    *,
    sequence: int,
    project_instance: Mapping[str, Any],
    run_instance: Mapping[str, Any],
    packet_ref: Mapping[str, Any] | None,
) -> dict[str, Any]:
    _exact(event_input, EVENT_INPUT_KEYS, "lineage event input")
    return {
        "event_id": event_input["event_id"],
        "sequence": sequence,
        "recorded_at": event_input["recorded_at"],
        "event_type": event_input["event_type"],
        "role": event_input["role"],
        "step_id": event_input["step_id"],
        "project_instance": dict(project_instance),
        "run_instance": dict(run_instance),
        "context_packet": dict(packet_ref) if packet_ref else None,
        "knowledge_ref": dict(event_input["knowledge_ref"]) if event_input["knowledge_ref"] else None,
        "knowledge_state": event_input["knowledge_state"],
        "provider": dict(event_input["provider"]) if event_input["provider"] else None,
        "evidence_refs": [dict(item) for item in event_input["evidence_refs"]],
        "reason_codes": list(event_input["reason_codes"]),
        "previous_event_id": event_input["previous_event_id"],
    }


def preview_event(
    project_root: Path,
    event_input: Mapping[str, Any],
    *,
    expected_revision: int,
    recall_result: Mapping[str, Any] | None = None,
    knowledge_root: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise LineageError("expected_revision must be a non-negative integer")
    project, project_id, run_id, project_instance, run_instance = _read_project_subject(project_root)
    path = _lineage_path(project, run_id)
    _assert_private_or_ignored(project, path)
    existing = _read_stored(path, project)
    _, contract_hash = _load_contract()
    # The two-phase CLI runs preview and record as separate processes.  Bind
    # the default update time to the immutable event time so an unchanged plan
    # has the same token in both processes; callers may still inject ``now``
    # for controlled replay or tests.
    timestamp = now or event_input.get("recorded_at")
    _timestamp(timestamp, "now")
    if existing is None:
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "contract_sha256": contract_hash,
            "project_ref": project_id,
            "run_ref": run_id,
            "revision": 0,
            "created_at": timestamp,
            "updated_at": timestamp,
            "events": [],
            "states": [],
        }
    else:
        record = existing
        if record["project_ref"] != project_id or record["run_ref"] != run_id:
            raise LineageError("stored lineage belongs to another project or run")

    packet_ref = _packet_reference(recall_result)
    event = _build_event(
        event_input,
        sequence=record["revision"] + 1,
        project_instance=project_instance,
        run_instance=run_instance,
        packet_ref=packet_ref,
    )
    validate_event(event, project_id=project_id, run_id=run_id)
    _validate_recall_membership(event, recall_result)
    if event["event_type"] == "association" and event["knowledge_ref"] is not None:
        if not any(
            state["knowledge_ref"] == event["knowledge_ref"]
            for state in record["states"]
        ):
            raise LineageError("association knowledge revision has no recorded role/step state")
        if recall_result is not None and not any(
            _citation_matches(event["knowledge_ref"], citation)
            for citation in recall_result["context_packet"]["citations"]
        ):
            raise LineageError("association knowledge revision differs from the exact ContextPacket")
    for evidence in event["evidence_refs"]:
        _read_evidence(project, project_id, run_id, evidence)
    for prior in record["events"]:
        if prior["event_id"] == event["event_id"]:
            comparable = dict(event)
            comparable["sequence"] = prior["sequence"]
            if prior != comparable:
                raise LineageError("event_id already exists with different content")
            core = {
                "project_ref": project_id,
                "run_ref": run_id,
                "expected_revision": expected_revision,
                "current_revision": record["revision"],
                "idempotent": True,
                "event": prior,
            }
            return {**core, "plan_token": _sha256(_canonical_bytes(core)), "record": record}
    if record["revision"] != expected_revision:
        raise LineageError("stale lineage revision")
    if record["revision"] >= MAX_EVENTS:
        raise LineageError("lineage reached its bounded event limit")
    if record["events"] and _timestamp(event["recorded_at"], "recorded_at") < _timestamp(record["events"][-1]["recorded_at"], "recorded_at"):
        raise LineageError("new event timestamp precedes the immutable event log")
    if _timestamp(timestamp, "now") < _timestamp(record["updated_at"], "updated_at"):
        raise LineageError("lineage update time cannot move backward")
    updated = dict(record)
    updated["events"] = [*record["events"], event]
    updated["revision"] = record["revision"] + 1
    updated["updated_at"] = timestamp
    updated["states"] = _derive_states(updated["events"])
    validate_record(updated)
    if event["event_type"] == "knowledge" and event["knowledge_state"] != "omitted":
        if knowledge_root is None:
            raise LineageError("non-omitted knowledge requires current File/Git revalidation")
        reasons = _validate_current_reference(
            FileGitBackend(knowledge_root), event["knowledge_ref"],
            project_id=project_id, role=event["role"],
        )
        if reasons:
            raise LineageError("canonical knowledge is not currently usable; record an omission")
    core = {
        "project_ref": project_id,
        "run_ref": run_id,
        "expected_revision": expected_revision,
        "current_revision": record["revision"],
        "idempotent": False,
        "event": event,
        "record_sha256": _sha256(_canonical_bytes(updated)),
    }
    return {**core, "plan_token": _sha256(_canonical_bytes(core)), "record": updated}


def _atomic_write(bound: _BoundDirectory, target_name: str, record: Mapping[str, Any]) -> None:
    nonce = secrets.token_hex(24)
    pending_name = f"{target_name}.pending-{nonce}"
    backup_name = f"{target_name}.backup-{nonce}"
    pending_identity = None
    backup_identity = None
    published = False
    had_original = bound.child_identity(target_name) is not None
    descriptor: int | None = None
    try:
        if had_original:
            backup_identity = bound.link(target_name, backup_name)
        _verify_checkpoint(bound, "before_pending_creation")
        descriptor = bound.open_exclusive(pending_name, binary=True)
        pending_identity = _file_identity(os.fstat(descriptor))
        _verify_checkpoint(bound, "after_pending_creation")
        payload = (json.dumps(dict(record), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_LINEAGE_BYTES:
            raise LineageError("lineage record exceeds the configured size limit")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            pending_identity = _file_identity(os.fstat(handle.fileno()))
        _verify_checkpoint(bound, "before_replace")
        bound.replace(pending_name, target_name, expected_source=pending_identity)
        published = True
        _verify_checkpoint(bound, "after_replace")
        _verify_checkpoint(bound, "before_final_cleanup")
        if backup_identity is not None and not bound.unlink_owned(backup_name, backup_identity):
            raise LineageError("lineage backup identity changed during cleanup")
        backup_identity = None
    except Exception:
        if published:
            if had_original and backup_identity is not None:
                bound.replace(backup_name, target_name, expected_source=backup_identity, require_current=False)
                backup_identity = None
            else:
                bound.unlink_owned(target_name, pending_identity)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        bound.unlink_owned(pending_name, pending_identity)
        bound.unlink_owned(backup_name, backup_identity)


def record_event(
    project_root: Path,
    event_input: Mapping[str, Any],
    *,
    expected_revision: int,
    plan_token: str,
    recall_result: Mapping[str, Any] | None = None,
    knowledge_root: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    _hash(plan_token, "plan_token")
    preview = preview_event(
        project_root, event_input, expected_revision=expected_revision,
        recall_result=recall_result, knowledge_root=knowledge_root, now=now,
    )
    if preview["plan_token"] != plan_token:
        raise LineageError("lineage plan token no longer matches the exact preview")
    if preview["idempotent"]:
        return {"idempotent": True, "record": preview["record"]}
    project, _, run_id, _, _ = _read_project_subject(project_root)
    path = _lineage_path(project, run_id)
    parent_existed = path.parent.exists()
    bound: _BoundDirectory | None = None
    try:
        with _BoundDirectory(path.parent, project) as bound, _exclusive_update_lock(bound, path.name):
            current = None
            if bound.child_identity(path.name) is not None:
                current = _strict_json_bytes(
                    bound.read_bytes(
                        path.name,
                        max_bytes=MAX_LINEAGE_BYTES,
                        require_single_link=True,
                        binary=True,
                    ),
                    label="lineage sidecar",
                )
                validate_record(current)
            current_revision = current["revision"] if current else 0
            if current_revision != expected_revision:
                raise LineageError("stale lineage revision")
            bound.verify_current()
            _atomic_write(bound, path.name, preview["record"])
            bound.verify_current()
    except FeedbackError as exc:
        raise LineageError("private lineage transaction failed safely") from exc
    finally:
        if not parent_existed and bound is not None and bound.token is not None:
            try:
                metadata = path.parent.lstat()
                if _directory_identity(metadata) == bound.token and not any(path.parent.iterdir()):
                    path.parent.rmdir()
            except OSError:
                pass
    return {"idempotent": False, "record": preview["record"]}


def build_view(
    project_root: Path,
    *,
    run_id: str | None = None,
    knowledge_root: Path | None = None,
) -> dict[str, Any]:
    project, project_id, active_run_id, _, _ = _read_project_subject(project_root)
    selected_run = active_run_id if run_id is None else _portable(run_id, PORTABLE_RUN, "run_id")
    path = _lineage_path(project, selected_run)
    record = _read_stored(path, project)
    if record is None:
        if selected_run != active_run_id:
            raise LineageError("historical lineage sidecar is unavailable")
        return {
            "schema_version": VIEW_VERSION,
            "project_ref": project_id,
            "run_ref": selected_run,
            "lineage_status": "unavailable",
            "record": None,
            "verification": [],
            "provider_degradations": [],
            "associations": [],
            "claim": "association/evidence only",
            "confounders": _load_contract()[0]["confounders"],
            "unknowns": _load_contract()[0]["unknowns"],
        }
    if record["project_ref"] != project_id or record["run_ref"] != selected_run:
        raise LineageError("stored lineage belongs to another project or run")
    backend = FileGitBackend(knowledge_root) if knowledge_root is not None else None
    verification: list[dict[str, Any]] = []
    for state in record["states"]:
        reasons = list(
            _validate_current_reference(
                backend, state["knowledge_ref"], project_id=project_id, role=state["role"]
            ) if backend is not None else ["knowledge_root_unavailable"]
        )
        if state["state"] == "omitted":
            event = next(item for item in record["events"] if item["event_id"] == state["last_event_id"])
            reasons = sorted(set([*reasons, *event["reason_codes"], "declared_omission"]))
        usable = not reasons and state["state"] != "omitted"
        verification.append({
            "role": state["role"],
            "step_id": state["step_id"],
            "record_id": state["knowledge_ref"]["record_id"],
            "source_commit": state["knowledge_ref"]["source_commit"],
            "content_sha256": state["knowledge_ref"]["content_sha256"],
            "state": state["state"],
            "usable": usable,
            "reason_codes": sorted(set(reasons)),
        })
    provider_degradations = [
        {
            "role": event["role"], "step_id": event["step_id"],
            "provider_id": event["provider"]["provider_id"],
            "state": event["provider"]["state"], "reason_codes": event["reason_codes"],
        }
        for event in record["events"]
        if event["event_type"] == "provider" and event["provider"]["state"] != "available"
    ]
    associations: list[dict[str, Any]] = []
    for event in record["events"]:
        if event["event_type"] != "association":
            continue
        association_reasons: list[str] = []
        for reference in event["evidence_refs"]:
            try:
                _read_evidence(project, project_id, selected_run, reference)
            except (LineageError, FeedbackError, OSError):
                association_reasons.append("evidence_unavailable_or_stale")
        associations.append({
            "event_id": event["event_id"],
            "role": event["role"],
            "step_id": event["step_id"],
            "knowledge_record_id": event["knowledge_ref"]["record_id"] if event["knowledge_ref"] else None,
            "evidence_refs": event["evidence_refs"],
            "usable": not association_reasons,
            "reason_codes": sorted(set(association_reasons)),
        })
    status = "degraded" if (
        provider_degradations
        or any(not item["usable"] for item in verification)
        or any(not item["usable"] for item in associations)
    ) else "available"
    contract = _load_contract()[0]
    return {
        "schema_version": VIEW_VERSION,
        "project_ref": project_id,
        "run_ref": selected_run,
        "lineage_status": status,
        "record": record,
        "verification": verification,
        "provider_degradations": provider_degradations,
        "associations": associations,
        "claim": "association/evidence only",
        "confounders": contract["confounders"],
        "unknowns": contract["unknowns"],
    }


def validate_view(view: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "project_ref", "run_ref", "lineage_status", "record",
        "verification", "provider_degradations", "associations", "claim",
        "confounders", "unknowns",
    }
    _exact(view, expected, "lineage view")
    if view["schema_version"] != VIEW_VERSION or view["claim"] != "association/evidence only":
        raise LineageError("lineage view identity is invalid")
    _portable(view["project_ref"], PORTABLE_ID, "view project_ref")
    _portable(view["run_ref"], PORTABLE_RUN, "view run_ref")
    contract = _load_contract()[0]
    if view["confounders"] != contract["confounders"] or view["unknowns"] != contract["unknowns"]:
        raise LineageError("lineage view claim boundaries drifted")
    if not isinstance(view["verification"], list) or not isinstance(view["provider_degradations"], list) or not isinstance(view["associations"], list):
        raise LineageError("lineage view aggregates must be arrays")
    record = view["record"]
    if record is None:
        if (
            view["lineage_status"] != "unavailable"
            or view["verification"]
            or view["provider_degradations"]
            or view["associations"]
        ):
            raise LineageError("lineage unavailable view fabricates aggregates")
        return
    validate_record(record)
    if (
        record["project_ref"] != view["project_ref"]
        or record["run_ref"] != view["run_ref"]
    ):
        raise LineageError("lineage view subject differs from its record")
    expected_state_keys = [
        (
            state["role"], state["step_id"], state["knowledge_ref"]["record_id"],
            state["knowledge_ref"]["source_commit"], state["knowledge_ref"]["content_sha256"],
            state["state"],
        )
        for state in record["states"]
    ]
    actual_state_keys: list[tuple[str, ...]] = []
    for item in view["verification"]:
        _exact(
            item,
            {
                "role", "step_id", "record_id", "source_commit", "content_sha256",
                "state", "usable", "reason_codes",
            },
            "lineage verification",
        )
        if not isinstance(item["usable"], bool):
            raise LineageError("lineage verification usable flag is invalid")
        reasons = item["reason_codes"]
        if not isinstance(reasons, list) or reasons != sorted(set(reasons)):
            raise LineageError("lineage verification reasons are invalid")
        for reason in reasons:
            _portable(reason, PORTABLE_ID, "verification reason")
        if bool(reasons) == item["usable"]:
            raise LineageError("lineage verification usability contradicts reasons")
        if item["state"] == "omitted" and item["usable"]:
            raise LineageError("omitted knowledge cannot be usable")
        actual_state_keys.append(
            (
                item["role"], item["step_id"], item["record_id"], item["source_commit"],
                item["content_sha256"], item["state"],
            )
        )
    if actual_state_keys != expected_state_keys:
        raise LineageError("lineage verification differs from materialized states")
    expected_degradations = [
        (
            event["role"], event["step_id"], event["provider"]["provider_id"],
            event["provider"]["state"], event["reason_codes"],
        )
        for event in record["events"]
        if event["event_type"] == "provider" and event["provider"]["state"] != "available"
    ]
    actual_degradations: list[tuple[Any, ...]] = []
    for item in view["provider_degradations"]:
        _exact(item, {"role", "step_id", "provider_id", "state", "reason_codes"}, "provider degradation")
        actual_degradations.append((item["role"], item["step_id"], item["provider_id"], item["state"], item["reason_codes"]))
    if actual_degradations != expected_degradations:
        raise LineageError("provider degradation aggregate differs from events")
    association_events = [event for event in record["events"] if event["event_type"] == "association"]
    if len(view["associations"]) != len(association_events):
        raise LineageError("association aggregate count differs from events")
    for item, event in zip(view["associations"], association_events):
        _exact(
            item,
            {
                "event_id", "role", "step_id", "knowledge_record_id",
                "evidence_refs", "usable", "reason_codes",
            },
            "evidence association",
        )
        expected_core = (
            event["event_id"], event["role"], event["step_id"],
            event["knowledge_ref"]["record_id"] if event["knowledge_ref"] else None,
            event["evidence_refs"],
        )
        if (
            item["event_id"], item["role"], item["step_id"],
            item["knowledge_record_id"], item["evidence_refs"],
        ) != expected_core:
            raise LineageError("evidence association differs from immutable event")
        if not isinstance(item["usable"], bool) or bool(item["reason_codes"]) == item["usable"]:
            raise LineageError("evidence association usability is invalid")
        expected_reasons = [] if item["usable"] else ["evidence_unavailable_or_stale"]
        if item["reason_codes"] != expected_reasons:
            raise LineageError("evidence association reasons are not deterministic")
    expected_status = "degraded" if (
        view["provider_degradations"]
        or any(not item["usable"] for item in view["verification"])
        or any(not item["usable"] for item in view["associations"])
    ) else "available"
    if view["lineage_status"] != expected_status:
        raise LineageError("lineage status differs from verified aggregates")


def render_report(view: Mapping[str, Any]) -> str:
    validate_view(view)
    lines = [
        "# Knowledge lineage", "",
        f"- Project: `{view['project_ref']}`", f"- Run: `{view['run_ref']}`",
        f"- Status: `{view['lineage_status']}`", "",
        "> association/evidence only — this report does not establish causal contribution.", "",
    ]
    record = view["record"]
    if record is None:
        lines.extend([
            "Lineage unavailable. This v0.1-compatible run has no lineage sidecar; no usage is inferred or fabricated.", "",
        ])
    else:
        validate_record(record)
        lines.extend([f"- Revision: `{record['revision']}`", "", "## ContextPacket instances", "", "| Role | Step | Packet | RecallTrace |", "|---|---|---|---|"])
        packets = sorted({
            (event["role"], event["step_id"], event["context_packet"]["sha256"], event["context_packet"]["recall_trace_sha256"])
            for event in record["events"] if event["context_packet"] is not None
        })
        if packets:
            for role, step, packet_hash, trace_hash in packets:
                lines.append(f"| `{role}` | `{step}` | `{PACKET_VERSION}@{packet_hash}` | `{TRACE_VERSION}@{trace_hash}` |")
        else:
            lines.append("| _none_ | _none_ | _none_ | _none_ |")
        lines.extend(["", "## Current knowledge states", "", "| Role | Step | Revision | State | Usable now | Reasons |", "|---|---|---|---|---|---|"])
        for item in view["verification"]:
            reasons = ", ".join(item["reason_codes"]) or "-"
            lines.append(f"| `{item['role']}` | `{item['step_id']}` | `{item['record_id']}@{item['source_commit']}#{item['content_sha256']}` | `{item['state']}` | `{'yes' if item['usable'] else 'no'}` | {html.escape(reasons)} |")
        if not view["verification"]:
            lines.append("| _none_ | _none_ | _none_ | _none_ | `no` | no knowledge state recorded |")
        lines.extend(["", "## Provider degradation", ""])
        if view["provider_degradations"]:
            for item in view["provider_degradations"]:
                lines.append(f"- `{item['role']}/{item['step_id']}`: `{item['provider_id']}` is `{item['state']}` ({', '.join(item['reason_codes'])}).")
        else:
            lines.append("No provider degradation recorded.")
        lines.extend(["", "## Evidence associations", ""])
        if view["associations"]:
            for association in view["associations"]:
                links = ", ".join(f"`{item['kind']}:{item['ref']}#{item['sha256']}`" for item in association["evidence_refs"])
                state = "usable" if association["usable"] else "degraded: " + ", ".join(association["reason_codes"])
                lines.append(f"- `{association['event_id']}` ({association['role']}/{association['step_id']}, {state}): {links}")
        else:
            lines.append("No QA, feedback, outcome, shadow, or evaluation association recorded.")
        lines.append("")
    lines.extend(["## Confounders", "", *[f"- {item}" for item in view["confounders"]], "", "## Unknowns", "", *[f"- {item}" for item in view["unknowns"]], ""])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "record"):
        command = commands.add_parser(name)
        command.add_argument("--project-root", required=True)
        command.add_argument("--knowledge-root")
        command.add_argument("--event-file", required=True)
        command.add_argument("--recall-result-file")
        command.add_argument("--expected-revision", required=True, type=int)
        if name == "record":
            command.add_argument("--plan-token", required=True)
    for name in ("show", "report"):
        command = commands.add_parser(name)
        command.add_argument("--project-root", required=True)
        command.add_argument("--knowledge-root")
        command.add_argument("--run-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"preview", "record"}:
            event, _ = _read_input(Path(args.event_file), maximum=MAX_EVENT_INPUT_BYTES, label="lineage event input")
            recall = None
            if args.recall_result_file:
                recall, _ = _read_input(Path(args.recall_result_file), maximum=MAX_RECALL_RESULT_BYTES, label="recall result")
            values = {
                "project_root": Path(args.project_root),
                "event_input": event,
                "expected_revision": args.expected_revision,
                "recall_result": recall,
                "knowledge_root": Path(args.knowledge_root) if args.knowledge_root else None,
            }
            result: Any = preview_event(**values) if args.command == "preview" else record_event(**values, plan_token=args.plan_token)
        else:
            view = build_view(
                Path(args.project_root), run_id=args.run_id,
                knowledge_root=Path(args.knowledge_root) if args.knowledge_root else None,
            )
            result = view if args.command == "show" else render_report(view)
        if isinstance(result, str):
            print(result, end="")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (LineageError, FeedbackError) as exc:
        print(f"OPC_LINEAGE_ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("OPC_LINEAGE_ERROR: private lineage operation failed safely", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
