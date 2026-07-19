#!/usr/bin/env python3
"""Preview and run privacy-safe, read-only Shadow Evaluation for one candidate."""

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
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from opc_feedback import (
    FeedbackError,
    MAX_SIDECAR_BYTES,
    _BoundDirectory,
    _directory_identity,
    _existing_object_is_within,
    _file_identity,
    _is_reparse,
    read_feedback,
    validate_record as validate_feedback_record,
)
from opc_memory import FileGitBackend, MEMORY_STATUSES, OpcMemoryError, load_json
from opc_sensitive import SENSITIVE_PATTERNS


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PLUGIN_ROOT / "assets" / "evaluation" / "shadow-evaluation-contract.v1.json"
BASELINE_CONTRACT_VERSION = "opc-evaluation-contract-v1"
BASELINE_CONTRACT_SHA256 = "f7eda22695e25f91f15031d6a94d0183e399fc3b52b34a21130b7f567180444d"
CONTRACT_VERSION = "opc-shadow-evaluation-contract-v1"
REPLAY_VERSION = "opc-shadow-replay-v1"
RESULT_VERSION = "opc-shadow-result-v1"
MAX_REPLAY_BYTES = 512 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_CASES = 20
MAX_EVIDENCE_ITEMS = 200
MAX_FAILURE_MODES = 64
MAX_ID = 128
MAX_REF = 240
MAX_RATIO_COMPONENT = 1_000_000
MAX_SAFETY_COUNT = 1_000_000
MAX_CONTEXT_TOKENS = 10_000_000
MAX_LATENCY_MS = 86_400_000
MAX_AGGREGATE_RATIO_COMPONENT = MAX_RATIO_COMPONENT * MAX_CASES
MAX_AGGREGATE_SAFETY_COUNT = MAX_SAFETY_COUNT * MAX_CASES
MAX_AGGREGATE_CONTEXT_TOKENS = MAX_CONTEXT_TOKENS * MAX_CASES
MAX_AGGREGATE_LATENCY_MS = MAX_LATENCY_MS * MAX_CASES
PORTABLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
PORTABLE_CANDIDATE = re.compile(r"^exp-[A-Za-z0-9._-]+$")
PORTABLE_REF = re.compile(
    r"^(?!/)(?![A-Za-z]:)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
QUALITY_METRICS = (
    "manager_intervention_rate",
    "qa_catch_rate",
    "rework_loops_per_task",
    "valid_knowledge_reuse_rate",
    "false_recall_rate",
)
SAFETY_METRICS = ("scope_leakage_acceptances", "stale_obsolete_acceptances")
TELEMETRY_METRICS = ("context_tokens_per_task", "latency_ms")
ALL_METRICS = (*QUALITY_METRICS, *SAFETY_METRICS, *TELEMETRY_METRICS)
LOWER_IS_BETTER = {
    "manager_intervention_rate",
    "rework_loops_per_task",
    "false_recall_rate",
    *SAFETY_METRICS,
    *TELEMETRY_METRICS,
}
FEEDBACK_KINDS = {
    "confirmed_outcome": "measured",
    "independent_qa_evidence": "measured",
    "manager_judgment": "human_judgment",
    "hypothesis": "model_inference",
    "unverified": "unverified",
}
WEIGHTS = {"measured": 3, "human_judgment": 1, "model_inference": 0, "unverified": 0}


class ShadowError(RuntimeError):
    """Expected fail-closed Shadow Evaluation error."""


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ShadowError(f"{label} must contain exactly the v1 fields")


def _portable(value: Any, pattern: re.Pattern[str], label: str, *, maximum: int = MAX_ID) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or not pattern.fullmatch(value):
        raise ShadowError(f"{label} is not a portable v1 identifier")
    lowered = value.lower()
    if any(token in lowered for token in ("session_id", "session-id", "turn_id", "turn-id", "thread_id", "thread-id")):
        raise ShadowError(f"{label} must not contain a runtime identifier")
    return value


def _strict_number(
    value: Any,
    label: str,
    *,
    integer: bool = False,
    positive: bool = False,
    maximum: int | float,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise ShadowError(f"{label} must be a finite {'integer' if integer else 'number'}")
    # Python integers are arbitrary precision.  Never convert them to float:
    # conversion itself can overflow before the v1 bound is checked.
    if isinstance(value, float) and not math.isfinite(value):
        raise ShadowError(f"{label} is outside the v1 bounds")
    if value < (1 if positive else 0) or value > maximum:
        raise ShadowError(f"{label} is outside the v1 bounds")
    return value


def _lexical_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _assert_unlinked_path(path: Path, *, label: str) -> Path:
    """Reject linked/reparse components before any resolve or file access."""

    candidate = _lexical_absolute(path)
    current = candidate
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ShadowError(f"{label} boundary could not be inspected") from exc
        else:
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse(current):
                raise ShadowError(f"{label} crosses a symlink or reparse boundary")
        parent = current.parent
        if parent == current:
            return candidate
        current = parent


def _assert_existing_directory(path: Path, *, label: str) -> Path:
    candidate = _assert_unlinked_path(path, label=label)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ShadowError(f"{label} must be an existing stable directory") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(candidate):
        raise ShadowError(f"{label} must be an existing stable directory")
    return candidate


def _read_bound_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    candidate = _assert_unlinked_path(path, label=label)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ShadowError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ShadowError(f"{label} must be a regular non-linked file")
    if metadata.st_size > maximum:
        raise ShadowError(f"{label} exceeds the configured size limit")
    try:
        with _BoundDirectory(candidate.parent, candidate.parent) as bound:
            return bound.read_bytes(
                candidate.name,
                max_bytes=maximum,
                require_single_link=True,
            )
    except (FeedbackError, OSError) as exc:
        raise ShadowError(f"{label} could not be read as one stable, uniquely linked file") from exc


def _read_trusted_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    """Read an installed immutable file without applying private-project policy."""

    candidate = _assert_unlinked_path(path, label=label)
    try:
        before = candidate.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum
        ):
            raise ShadowError(f"{label} is not one bounded immutable file")
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before):
                raise ShadowError(f"{label} identity changed while being opened")
            raw = os.read(descriptor, maximum + 1)
            if len(raw) > maximum:
                raise ShadowError(f"{label} exceeds the configured size limit")
            after = candidate.lstat()
            if _file_identity(after) != _file_identity(before) or after.st_nlink != 1:
                raise ShadowError(f"{label} identity changed while being read")
            return raw
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ShadowError(f"{label} could not be read safely") from exc


def _reject_sensitive(value: Any) -> None:
    if isinstance(value, Mapping):
        for nested in value.values():
            _reject_sensitive(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive(nested)
    elif isinstance(value, str):
        for _, pattern in SENSITIVE_PATTERNS:
            if pattern.search(value):
                raise ShadowError("input contains a credential-like value; matched content is not displayed")


def _read_json(
    path: Path,
    *,
    maximum: int,
    label: str,
    trusted_installed_file: bool = False,
) -> tuple[dict[str, Any], bytes]:
    raw = (
        _read_trusted_bytes(path, maximum=maximum, label=label)
        if trusted_installed_file
        else _read_bound_bytes(path, maximum=maximum, label=label)
    )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ShadowError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ShadowError(f"{label} must be a JSON object")
    _reject_sensitive(value)
    return value, raw


def _validate_ratio(value: Any, label: str) -> None:
    _exact(value, {"numerator", "denominator"}, label)
    numerator = _strict_number(
        value["numerator"],
        f"{label}.numerator",
        integer=True,
        maximum=MAX_RATIO_COMPONENT,
    )
    denominator = _strict_number(
        value["denominator"],
        f"{label}.denominator",
        integer=True,
        maximum=MAX_RATIO_COMPONENT,
    )
    if numerator > denominator and label.rsplit(".", 1)[-1] != "rework_loops_per_task":
        raise ShadowError(f"{label} numerator cannot exceed denominator")


def _validate_arm(value: Any, label: str, *, candidate_applied: bool) -> None:
    _exact(value, {"candidate_applied", "execution_status", "failure_code", "metrics"}, label)
    if value["candidate_applied"] is not candidate_applied:
        raise ShadowError(f"{label}.candidate_applied violates the control/treatment contract")
    status = value["execution_status"]
    if status not in {"completed", "timeout", "provider_unavailable", "provider_error"}:
        raise ShadowError(f"{label}.execution_status is unsupported")
    failure = value["failure_code"]
    if status == "completed" and failure is not None:
        raise ShadowError(f"{label} completed execution must not include failure_code")
    if status != "completed":
        _portable(failure, PORTABLE_ID, f"{label}.failure_code")
    metrics = value["metrics"]
    _exact(metrics, set(ALL_METRICS), f"{label}.metrics")
    for metric in QUALITY_METRICS:
        _validate_ratio(metrics[metric], f"{label}.metrics.{metric}")
    for metric in SAFETY_METRICS:
        _strict_number(
            metrics[metric],
            f"{label}.metrics.{metric}",
            integer=True,
            maximum=MAX_SAFETY_COUNT,
        )
    _strict_number(
        metrics["context_tokens_per_task"],
        f"{label}.metrics.context_tokens_per_task",
        integer=True,
        positive=True,
        maximum=MAX_CONTEXT_TOKENS,
    )
    _strict_number(
        metrics["latency_ms"],
        f"{label}.metrics.latency_ms",
        positive=True,
        maximum=MAX_LATENCY_MS,
    )


def validate_replay(value: Mapping[str, Any]) -> None:
    _exact(value, {"schema_version", "contract_version", "evaluation_id", "dataset", "candidate", "dependency", "cases"}, "replay")
    if value["schema_version"] != REPLAY_VERSION or value["contract_version"] != CONTRACT_VERSION:
        raise ShadowError("unsupported replay schema or contract version; migrate explicitly")
    _portable(value["evaluation_id"], PORTABLE_ID, "evaluation_id")
    dataset = value["dataset"]
    _exact(dataset, {"kind", "dataset_id", "project_id", "approval_ref"}, "dataset")
    if dataset["kind"] not in {"synthetic", "approved_private_pilot"}:
        raise ShadowError("dataset.kind is unsupported")
    _portable(dataset["dataset_id"], PORTABLE_ID, "dataset_id")
    _portable(dataset["project_id"], PORTABLE_ID, "project_id")
    if dataset["kind"] == "synthetic":
        if dataset["approval_ref"] is not None:
            raise ShadowError("synthetic data must not claim a private approval")
    else:
        _portable(dataset["approval_ref"], PORTABLE_REF, "approval_ref", maximum=MAX_REF)
    candidate = value["candidate"]
    _exact(candidate, {"candidate_id", "source_path", "source_commit", "content_sha256"}, "candidate")
    _portable(candidate["candidate_id"], PORTABLE_CANDIDATE, "candidate_id")
    _portable(candidate["source_path"], PORTABLE_REF, "source_path", maximum=MAX_REF)
    if not isinstance(candidate["source_commit"], str) or not GIT_COMMIT.fullmatch(candidate["source_commit"]):
        raise ShadowError("candidate.source_commit must be an exact Git commit")
    if not isinstance(candidate["content_sha256"], str) or not SHA256.fullmatch(candidate["content_sha256"]):
        raise ShadowError("candidate.content_sha256 must be a lowercase SHA-256")
    dependency = value["dependency"]
    _exact(dependency, {"engine", "version", "determinism", "seed"}, "dependency")
    _portable(dependency["engine"], PORTABLE_ID, "dependency.engine")
    _portable(dependency["version"], PORTABLE_ID, "dependency.version")
    if dependency["determinism"] not in {"deterministic", "versioned_nondeterministic"}:
        raise ShadowError("dependency.determinism is unsupported")
    if dependency["seed"] is not None:
        _portable(dependency["seed"], PORTABLE_ID, "dependency.seed")
    cases = value["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ShadowError(f"cases must contain 1..{MAX_CASES} items")
    identifiers: set[str] = set()
    for index, case in enumerate(cases):
        _exact(case, {"case_id", "control", "treatment"}, f"case[{index}]")
        case_id = _portable(case["case_id"], PORTABLE_ID, f"case[{index}].case_id")
        if case_id in identifiers:
            raise ShadowError("case ids must be unique")
        identifiers.add(case_id)
        _validate_arm(case["control"], f"case[{index}].control", candidate_applied=False)
        _validate_arm(case["treatment"], f"case[{index}].treatment", candidate_applied=True)


def _git_head(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _preflight(knowledge_root: Path, replay: Mapping[str, Any]) -> dict[str, Any]:
    backend = FileGitBackend(_assert_existing_directory(knowledge_root, label="knowledge root"))
    candidate_id = replay["candidate"]["candidate_id"]
    found: list[dict[str, Any]] = []
    for status in MEMORY_STATUSES:
        for record in backend.list_by_status(status, limit=10000):
            if record.get("id") == candidate_id:
                found.append(record)
    reasons: list[str] = []
    if len(found) != 1:
        reasons.append("candidate_missing_or_duplicate")
        return {"passed": False, "reasons": reasons, "candidate_snapshot": None}
    record = found[0]
    status = str(record.get("status", ""))
    if status != "candidate":
        reasons.append("obsolete_or_non_candidate")
    project_id = replay["dataset"]["project_id"]
    if record.get("scope") == "project" and record.get("project_id") != project_id:
        reasons.append("cross_project_scope")
    elif record.get("scope") == "global" and record.get("project_id"):
        reasons.append("cross_project_scope")
    elif record.get("scope") not in {"global", "project"}:
        reasons.append("cross_project_scope")
    source_path = str(record.get("_source_path", ""))
    expected = replay["candidate"]
    canonical_raw = _read_bound_bytes(
        backend.root / source_path,
        maximum=MAX_REPLAY_BYTES,
        label="candidate canonical record",
    )
    try:
        metadata = backend.source_metadata(source_path)
        if hashlib.sha256(canonical_raw).hexdigest() != metadata.get("content_hash"):
            raise ShadowError("candidate canonical identity changed during provenance verification")
    except (OpcMemoryError, OSError):
        metadata = {"source_path": source_path, "content_hash": None, "source_commit": None}
    head = _git_head(backend.root)
    if (
        source_path != expected["source_path"]
        or metadata.get("content_hash") != expected["content_sha256"]
        or metadata.get("source_commit") != expected["source_commit"]
        or head != expected["source_commit"]
    ):
        reasons.append("stale_provenance")
    reasons = sorted(set(reasons))
    snapshot = {
        "candidate_id": candidate_id,
        "status": status,
        "scope": record.get("scope"),
        "project_id": record.get("project_id"),
        "source_path": source_path,
        "source_commit": metadata.get("source_commit"),
        "content_sha256": metadata.get("content_hash"),
        "declared_confidence": record.get("confidence"),
    }
    return {"passed": not reasons, "reasons": reasons, "candidate_snapshot": snapshot}


def _project_feedback(project_root: Path | None, replay: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = replay["dataset"]["kind"]
    if kind == "synthetic":
        if project_root is not None:
            raise ShadowError("synthetic replay must not read a private project")
        return None
    if project_root is None:
        raise ShadowError("approved_private_pilot requires --project-root")
    project = _assert_existing_directory(project_root, label="private pilot project").resolve(
        strict=True
    )
    if _existing_object_is_within(project, PLUGIN_ROOT) or _existing_object_is_within(PLUGIN_ROOT, project):
        raise ShadowError("private pilot project must not overlap the installed/public plugin tree")
    project_record = load_json(project / ".opc" / "project.json")
    if project_record.get("project_id") != replay["dataset"]["project_id"]:
        raise ShadowError("private pilot project_id does not match the replay contract")
    try:
        view = read_feedback(project)
    except (FeedbackError, OpcMemoryError, OSError) as exc:
        raise ShadowError("private structured feedback could not be validated") from exc
    record = view.get("structured_feedback")
    if record is not None:
        validate_feedback_record(record)
    return record


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        payload = (json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ShadowError("result could not be serialized as strict JSON") from exc
    if len(payload) > MAX_RESULT_BYTES:
        raise ShadowError("result exceeds the configured size limit")
    return payload


def _load_contract() -> tuple[dict[str, Any], bytes]:
    contract, raw = _read_json(
        CONTRACT_PATH,
        maximum=64 * 1024,
        label="Shadow Evaluation contract",
        trusted_installed_file=True,
    )
    if contract.get("contract_version") != CONTRACT_VERSION:
        raise ShadowError("unsupported Shadow Evaluation contract version")
    if contract.get("metric_contract") != BASELINE_CONTRACT_VERSION:
        raise ShadowError("Shadow Evaluation metric contract is unsupported")
    arm = contract.get("arm_contract")
    if not isinstance(arm, dict) or (
        tuple(arm.get("quality_metrics", [])) != QUALITY_METRICS
        or tuple(arm.get("safety_metrics", [])) != SAFETY_METRICS
        or tuple(arm.get("telemetry_metrics", [])) != TELEMETRY_METRICS
    ):
        raise ShadowError("Shadow Evaluation arm metrics drifted from the runtime contract")
    metric_hash = contract.get("metric_contract_sha256")
    if metric_hash != BASELINE_CONTRACT_SHA256:
        raise ShadowError("Shadow Evaluation metric contract hash is unsupported")
    limits = contract.get("limits")
    expected_limits = {
        "replay_bytes": MAX_REPLAY_BYTES,
        "feedback_bytes": MAX_SIDECAR_BYTES,
        "result_bytes": MAX_RESULT_BYTES,
        "cases": MAX_CASES,
        "evidence_items": MAX_EVIDENCE_ITEMS,
        "failure_modes": MAX_FAILURE_MODES,
        "identifier_characters": MAX_ID,
        "portable_reference_characters": MAX_REF,
        "ratio_component": MAX_RATIO_COMPONENT,
        "safety_count": MAX_SAFETY_COUNT,
        "context_tokens_per_task": MAX_CONTEXT_TOKENS,
        "latency_ms": MAX_LATENCY_MS,
        "aggregate_ratio_component": MAX_AGGREGATE_RATIO_COMPONENT,
        "aggregate_safety_count": MAX_AGGREGATE_SAFETY_COUNT,
        "aggregate_context_tokens": MAX_AGGREGATE_CONTEXT_TOKENS,
        "aggregate_latency_ms": MAX_AGGREGATE_LATENCY_MS,
    }
    if limits != expected_limits:
        raise ShadowError("Shadow Evaluation limits drifted from the runtime contract")
    return contract, raw


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def build_preview(
    knowledge_root: Path,
    replay: Mapping[str, Any],
    replay_raw: bytes,
    *,
    project_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    validate_replay(replay)
    feedback = _project_feedback(project_root, replay)
    preflight = _preflight(knowledge_root, replay)
    preview: dict[str, Any] = {
        "preview_version": "opc-shadow-preview-v1",
        "contract_version": CONTRACT_VERSION,
        "evaluation_id": replay["evaluation_id"],
        "replay_sha256": hashlib.sha256(replay_raw).hexdigest(),
        "candidate": preflight["candidate_snapshot"],
        "preflight": {"passed": preflight["passed"], "reasons": preflight["reasons"]},
        "feedback_revision": feedback.get("revision") if feedback is not None else None,
        "planned_cases": len(replay["cases"]),
        "planned_writes": [],
        "forbidden_side_effects": [
            "candidate_status_change",
            "canonical_knowledge_write",
            "git_write",
            "provider_index_write",
            "project_source_write",
            "automatic_promotion",
        ],
        "manager_curation_still_required": True,
    }
    preview["preview_sha256"] = _fingerprint(preview)
    return preview, feedback


def _checked_integer_sum(
    values: Sequence[Any],
    *,
    label: str,
    item_maximum: int,
    aggregate_maximum: int,
) -> int:
    total = 0
    for index, value in enumerate(values):
        checked = _strict_number(
            value,
            f"{label}[{index}]",
            integer=True,
            maximum=item_maximum,
        )
        total += int(checked)
        if total > aggregate_maximum:
            raise ShadowError(f"{label} aggregate exceeds the v1 bounds")
    return total


def _checked_number_sum(
    values: Sequence[Any],
    *,
    label: str,
    item_maximum: int | float,
    aggregate_maximum: int | float,
) -> int | float:
    total: int | float = 0
    try:
        for index, value in enumerate(values):
            checked = _strict_number(
                value,
                f"{label}[{index}]",
                maximum=item_maximum,
            )
            total += checked
            if (
                isinstance(total, float)
                and not math.isfinite(total)
            ) or total > aggregate_maximum:
                raise ShadowError(f"{label} aggregate exceeds the v1 bounds")
    except ArithmeticError as exc:
        raise ShadowError(f"{label} aggregate exceeds the v1 bounds") from exc
    return total


def _bounded_median(
    values: Sequence[int | float],
    *,
    label: str,
    item_maximum: int | float,
) -> int | float:
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    pair = _checked_number_sum(
        [values[middle - 1], values[middle]],
        label=f"{label}.median_pair",
        item_maximum=item_maximum,
        aggregate_maximum=item_maximum * 2,
    )
    return pair / 2


def _aggregate_arm(cases: Sequence[Mapping[str, Any]], arm: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not 1 <= len(cases) <= MAX_CASES:
        raise ShadowError("aggregation case count exceeds the v1 bounds")
    aggregate: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    for case in cases:
        observation = case[arm]
        if observation["execution_status"] != "completed":
            failures.append(
                {
                    "arm": arm,
                    "case_id": case["case_id"],
                    "code": observation["execution_status"],
                    "failure_ref": observation["failure_code"],
                }
            )
    for metric in QUALITY_METRICS:
        numerator = _checked_integer_sum(
            [case[arm]["metrics"][metric]["numerator"] for case in cases],
            label=f"{arm}.{metric}.numerator",
            item_maximum=MAX_RATIO_COMPONENT,
            aggregate_maximum=MAX_AGGREGATE_RATIO_COMPONENT,
        )
        denominator = _checked_integer_sum(
            [case[arm]["metrics"][metric]["denominator"] for case in cases],
            label=f"{arm}.{metric}.denominator",
            item_maximum=MAX_RATIO_COMPONENT,
            aggregate_maximum=MAX_AGGREGATE_RATIO_COMPONENT,
        )
        aggregate[metric] = {
            "numerator": numerator,
            "denominator": denominator,
            "value": round(numerator / denominator, 6) if denominator else None,
        }
        if denominator == 0:
            failures.append({"arm": arm, "case_id": "aggregate", "code": "zero_denominator", "failure_ref": metric})
    for metric in SAFETY_METRICS:
        aggregate[metric] = _checked_integer_sum(
            [case[arm]["metrics"][metric] for case in cases],
            label=f"{arm}.{metric}",
            item_maximum=MAX_SAFETY_COUNT,
            aggregate_maximum=MAX_AGGREGATE_SAFETY_COUNT,
        )
    token_values = sorted(case[arm]["metrics"]["context_tokens_per_task"] for case in cases)
    token_total = _checked_integer_sum(
        token_values,
        label=f"{arm}.context_tokens_per_task",
        item_maximum=MAX_CONTEXT_TOKENS,
        aggregate_maximum=MAX_AGGREGATE_CONTEXT_TOKENS,
    )
    aggregate["context_tokens_per_task"] = {
        "total": token_total,
        "median": round(
            _bounded_median(
                token_values,
                label=f"{arm}.context_tokens_per_task",
                item_maximum=MAX_CONTEXT_TOKENS,
            ),
            6,
        ),
        "p95_nearest_rank": token_values[math.ceil(0.95 * len(token_values)) - 1],
    }
    latency_values = sorted(case[arm]["metrics"]["latency_ms"] for case in cases)
    latency_total = _checked_number_sum(
        latency_values,
        label=f"{arm}.latency_ms",
        item_maximum=MAX_LATENCY_MS,
        aggregate_maximum=MAX_AGGREGATE_LATENCY_MS,
    )
    aggregate["latency_ms"] = {
        "total": round(latency_total, 6),
        "median": round(
            _bounded_median(
                latency_values,
                label=f"{arm}.latency_ms",
                item_maximum=MAX_LATENCY_MS,
            ),
            6,
        ),
        "p95_nearest_rank": round(
            latency_values[math.ceil(0.95 * len(latency_values)) - 1],
            6,
        ),
    }
    return aggregate, failures


def _metric_comparison(control: Mapping[str, Any], treatment: Mapping[str, Any]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for metric in QUALITY_METRICS:
        left = control[metric]
        right = treatment[metric]
        if left["denominator"] == 0 or right["denominator"] == 0:
            direction = "unknown"
        else:
            left_fraction = Fraction(left["numerator"], left["denominator"])
            right_fraction = Fraction(right["numerator"], right["denominator"])
            if left_fraction == right_fraction:
                direction = "neutral"
            else:
                treatment_lower = right_fraction < left_fraction
                direction = "supporting" if treatment_lower == (metric in LOWER_IS_BETTER) else "counterevidence"
        comparisons.append(
            {
                "metric_id": metric,
                "control": left["value"],
                "treatment": right["value"],
                "direction": direction,
                "source_kind": "measured",
            }
        )
    for metric in SAFETY_METRICS:
        left = int(control[metric])
        right = int(treatment[metric])
        if left == right:
            direction = "neutral"
        else:
            direction = "supporting" if right < left else "counterevidence"
        comparisons.append(
            {
                "metric_id": metric,
                "control": left,
                "treatment": right,
                "direction": direction,
                "source_kind": "measured",
            }
        )
    for metric in TELEMETRY_METRICS:
        left = control[metric]["median"]
        right = treatment[metric]["median"]
        if left == right:
            direction = "neutral"
        else:
            direction = "supporting" if right < left else "counterevidence"
        comparisons.append(
            {
                "metric_id": metric,
                "control": left,
                "treatment": right,
                "direction": direction,
                "source_kind": "measured",
            }
        )
    return comparisons


def _feedback_direction(event: Mapping[str, Any]) -> str:
    category = event["category"]
    if category == "confirmed_outcome":
        return {"pass": "supporting", "fail": "counterevidence", "partial": "counterevidence"}[event["outcome_status"]]
    if category == "manager_judgment":
        return {
            "accepted": "supporting",
            "changes_requested": "counterevidence",
            "mixed": "counterevidence",
            "neutral": "neutral",
            "unknown": "unknown",
        }[event["manager_judgment"]]
    if category == "independent_qa_evidence":
        return {"pass": "supporting", "fail": "counterevidence", "partial": "counterevidence", "unknown": "unknown"}[event["qa_status"]]
    return "unknown"


def _feedback_evidence(feedback: Mapping[str, Any] | None, candidate_id: str) -> list[dict[str, Any]]:
    if feedback is None:
        return []
    evidence: list[dict[str, Any]] = []
    for event in feedback["events"]:
        refs = event["references"]
        if candidate_id not in refs["candidate_ids"]:
            continue
        metric_refs = [
            {
                "metric_id": item["metric_id"],
                "aggregate_ref": item["aggregate_ref"],
                "aggregate_sha256": item["aggregate_sha256"],
                "interpretation": item["interpretation"],
            }
            for item in refs["metric_refs"]
        ]
        evidence.append(
            {
                "evidence_id": event["event_id"],
                "source_kind": FEEDBACK_KINDS[event["category"]],
                "evidence_class": event["category"],
                "direction": _feedback_direction(event),
                "metric_refs": metric_refs,
                "artifact_refs": list(refs["artifact_refs"]),
            }
        )
    return evidence


def _confidence(evidence: Sequence[Mapping[str, Any]], declared: Any) -> dict[str, Any]:
    support = 1
    counter = 1
    weighted = {"supporting": 0, "counterevidence": 0}
    for item in evidence:
        if item.get("metric_id") in TELEMETRY_METRICS:
            # #4 defines context cost and latency as diagnostics. They remain
            # visible in the report but cannot inflate governance confidence.
            continue
        direction = item.get("direction")
        kind = str(item.get("source_kind", "unverified"))
        weight = WEIGHTS.get(kind, 0)
        if direction in weighted:
            weighted[direction] += weight
    support += weighted["supporting"]
    counter += weighted["counterevidence"]
    return {
        "formula_version": "beta-v1",
        "evidence_derived": True,
        "declared_candidate_confidence": declared,
        "weighted_support": weighted["supporting"],
        "weighted_counterevidence": weighted["counterevidence"],
        "evaluated_confidence": round(support / (support + counter), 6),
        "approval_permission": False,
    }


def evaluate(
    knowledge_root: Path,
    replay: Mapping[str, Any],
    replay_raw: bytes,
    *,
    expected_preview_sha256: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    preview, feedback = build_preview(knowledge_root, replay, replay_raw, project_root=project_root)
    if not SHA256.fullmatch(expected_preview_sha256) or preview["preview_sha256"] != expected_preview_sha256:
        raise ShadowError("preview fingerprint changed; preview the exact inputs again")
    contract, contract_raw = _load_contract()
    preflight = preview["preflight"]
    candidate = preview["candidate"]
    governance = {
        "automatic_promotion": False,
        "candidate_status_changed": False,
        "canonical_knowledge_written": False,
        "git_written": False,
        "provider_index_written": False,
        "project_source_written": False,
        "next_required_steps": ["manager_preview", "manager_approval", "exact_git_commit", "optional_reindex_preview_and_approval"],
    }
    base: dict[str, Any] = {
        "schema_version": RESULT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "metric_contract": BASELINE_CONTRACT_VERSION,
        "metric_contract_sha256": contract["metric_contract_sha256"],
        "contract_sha256": hashlib.sha256(contract_raw).hexdigest(),
        "replay_sha256": hashlib.sha256(replay_raw).hexdigest(),
        "evaluation_id": replay["evaluation_id"],
        "dataset": dict(replay["dataset"]),
        "candidate": candidate,
        "preflight": preflight,
    }
    if not preflight["passed"]:
        evidence: list[dict[str, Any]] = []
        result = {
            **base,
            "status": "rejected_preflight",
            "degradation_status": "none",
            "recommendation": "preflight_rejected",
            "measurements": None,
            "evidence": {"support": [], "counterevidence": [], "neutral_or_unknown": []},
            "confidence": _confidence(evidence, candidate.get("declared_confidence") if candidate else None),
            "failure_modes": [{"arm": "preflight", "case_id": "none", "code": reason, "failure_ref": "candidate"} for reason in preflight["reasons"]],
            "governance": governance,
        }
        validate_result(result)
        _json_bytes(result)
        return result

    control, control_failures = _aggregate_arm(replay["cases"], "control")
    treatment, treatment_failures = _aggregate_arm(replay["cases"], "treatment")
    comparisons = _metric_comparison(control, treatment)
    feedback_items = _feedback_evidence(feedback, replay["candidate"]["candidate_id"])
    all_evidence: list[dict[str, Any]] = [*comparisons, *feedback_items]
    support = [item for item in all_evidence if item.get("direction") == "supporting"]
    counter = [item for item in all_evidence if item.get("direction") == "counterevidence"]
    neutral = [item for item in all_evidence if item.get("direction") not in {"supporting", "counterevidence"}]
    failures = [*control_failures, *treatment_failures]
    quality_support = [item for item in comparisons if item["metric_id"] in (*QUALITY_METRICS, *SAFETY_METRICS) and item["direction"] == "supporting"]
    quality_counter = [item for item in comparisons if item["metric_id"] in (*QUALITY_METRICS, *SAFETY_METRICS) and item["direction"] == "counterevidence"]
    if treatment["scope_leakage_acceptances"] or treatment["stale_obsolete_acceptances"]:
        quality_counter.append({"metric_id": "safety_gate", "direction": "counterevidence"})
    if failures:
        status = "inconclusive"
        degradation_status = "degraded"
        recommendation = "inconclusive"
    elif quality_support and quality_counter:
        status = "inconclusive"
        degradation_status = "none"
        recommendation = "inconclusive"
        failures.append({"arm": "comparison", "case_id": "aggregate", "code": "conflicting_measured_results", "failure_ref": "quality_safety"})
    elif quality_counter:
        status = "conclusive"
        degradation_status = "none"
        recommendation = "do_not_promote_on_shadow_evidence"
    elif quality_support:
        status = "conclusive"
        degradation_status = "none"
        recommendation = "consider_for_separate_curation"
    else:
        status = "conclusive"
        degradation_status = "none"
        recommendation = "do_not_promote_on_shadow_evidence"
    result = {
        **base,
        "status": status,
        "degradation_status": degradation_status,
        "recommendation": recommendation,
        "measurements": {
            "control": control,
            "treatment": treatment,
            "comparisons": comparisons,
            "context_cost_and_latency_are_diagnostic_only": True,
        },
        "evidence": {"support": support, "counterevidence": counter, "neutral_or_unknown": neutral},
        "confidence": _confidence(all_evidence, candidate.get("declared_confidence")),
        "failure_modes": failures,
        "governance": governance,
    }
    # Fail closed if the candidate or Git HEAD moved after measurements were read.
    final_preflight = _preflight(knowledge_root, replay)
    if final_preflight != {"passed": True, "reasons": [], "candidate_snapshot": candidate}:
        raise ShadowError("candidate provenance changed during evaluation; no artifact was written")
    validate_result(result)
    _json_bytes(result)
    return result


RESULT_FIELDS = {
    "schema_version",
    "contract_version",
    "metric_contract",
    "metric_contract_sha256",
    "contract_sha256",
    "replay_sha256",
    "evaluation_id",
    "dataset",
    "candidate",
    "preflight",
    "status",
    "degradation_status",
    "recommendation",
    "measurements",
    "evidence",
    "confidence",
    "failure_modes",
    "governance",
}
PREFLIGHT_REASONS = {
    "candidate_missing_or_duplicate",
    "obsolete_or_non_candidate",
    "cross_project_scope",
    "stale_provenance",
}
GOVERNANCE_STEPS = (
    "manager_preview",
    "manager_approval",
    "exact_git_commit",
    "optional_reindex_preview_and_approval",
)


def _validate_result_dataset(value: Any) -> None:
    _exact(value, {"kind", "dataset_id", "project_id", "approval_ref"}, "result.dataset")
    if value["kind"] not in {"synthetic", "approved_private_pilot"}:
        raise ShadowError("result dataset kind is unsupported")
    _portable(value["dataset_id"], PORTABLE_ID, "result.dataset.dataset_id")
    _portable(value["project_id"], PORTABLE_ID, "result.dataset.project_id")
    if value["kind"] == "synthetic":
        if value["approval_ref"] is not None:
            raise ShadowError("synthetic result must not claim a private approval")
    else:
        _portable(
            value["approval_ref"],
            PORTABLE_REF,
            "result.dataset.approval_ref",
            maximum=MAX_REF,
        )


def _validate_candidate_snapshot(value: Any) -> None:
    if value is None:
        return
    _exact(
        value,
        {
            "candidate_id",
            "status",
            "scope",
            "project_id",
            "source_path",
            "source_commit",
            "content_sha256",
            "declared_confidence",
        },
        "result.candidate",
    )
    _portable(value["candidate_id"], PORTABLE_CANDIDATE, "result.candidate.candidate_id")
    if value["status"] not in MEMORY_STATUSES:
        raise ShadowError("result candidate status is unsupported")
    if value["scope"] not in {"global", "project"}:
        raise ShadowError("result candidate scope is unsupported")
    if value["project_id"] is not None:
        _portable(value["project_id"], PORTABLE_ID, "result.candidate.project_id")
    if value["scope"] == "project" and value["project_id"] is None:
        raise ShadowError("project candidate result requires project_id")
    if value["scope"] == "global" and value["project_id"] is not None:
        raise ShadowError("global candidate result cannot contain project_id")
    _portable(
        value["source_path"],
        PORTABLE_REF,
        "result.candidate.source_path",
        maximum=MAX_REF,
    )
    if value["source_commit"] is not None and (
        not isinstance(value["source_commit"], str)
        or not GIT_COMMIT.fullmatch(value["source_commit"])
    ):
        raise ShadowError("result candidate source_commit is invalid")
    if value["content_sha256"] is not None and (
        not isinstance(value["content_sha256"], str)
        or not SHA256.fullmatch(value["content_sha256"])
    ):
        raise ShadowError("result candidate content_sha256 is invalid")
    if value["declared_confidence"] is not None:
        _strict_number(
            value["declared_confidence"],
            "result.candidate.declared_confidence",
            maximum=1,
        )


def _validate_preflight_result(value: Any) -> None:
    _exact(value, {"passed", "reasons"}, "result.preflight")
    if not isinstance(value["passed"], bool):
        raise ShadowError("result preflight.passed must be boolean")
    reasons = value["reasons"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > len(PREFLIGHT_REASONS)
        or len(reasons) != len(set(reasons))
        or any(reason not in PREFLIGHT_REASONS for reason in reasons)
    ):
        raise ShadowError("result preflight reasons are invalid")
    if value["passed"] == bool(reasons):
        raise ShadowError("result preflight passed/reasons are inconsistent")


def _validate_aggregate_ratio(value: Any, label: str) -> None:
    _exact(value, {"numerator", "denominator", "value"}, label)
    numerator = _strict_number(
        value["numerator"],
        f"{label}.numerator",
        integer=True,
        maximum=MAX_AGGREGATE_RATIO_COMPONENT,
    )
    denominator = _strict_number(
        value["denominator"],
        f"{label}.denominator",
        integer=True,
        maximum=MAX_AGGREGATE_RATIO_COMPONENT,
    )
    expected = round(numerator / denominator, 6) if denominator else None
    if value["value"] != expected:
        raise ShadowError(f"{label}.value does not match its exact aggregate")


def _validate_telemetry_aggregate(
    value: Any,
    label: str,
    *,
    item_maximum: int,
    aggregate_maximum: int,
    integer_total: bool,
) -> None:
    _exact(value, {"total", "median", "p95_nearest_rank"}, label)
    _strict_number(
        value["total"],
        f"{label}.total",
        integer=integer_total,
        positive=True,
        maximum=aggregate_maximum,
    )
    for field in ("median", "p95_nearest_rank"):
        _strict_number(
            value[field],
            f"{label}.{field}",
            positive=True,
            maximum=item_maximum,
        )


def _validate_arm_aggregate(value: Any, label: str) -> None:
    _exact(value, set(ALL_METRICS), label)
    for metric in QUALITY_METRICS:
        _validate_aggregate_ratio(value[metric], f"{label}.{metric}")
    for metric in SAFETY_METRICS:
        _strict_number(
            value[metric],
            f"{label}.{metric}",
            integer=True,
            maximum=MAX_AGGREGATE_SAFETY_COUNT,
        )
    _validate_telemetry_aggregate(
        value["context_tokens_per_task"],
        f"{label}.context_tokens_per_task",
        item_maximum=MAX_CONTEXT_TOKENS,
        aggregate_maximum=MAX_AGGREGATE_CONTEXT_TOKENS,
        integer_total=True,
    )
    _validate_telemetry_aggregate(
        value["latency_ms"],
        f"{label}.latency_ms",
        item_maximum=MAX_LATENCY_MS,
        aggregate_maximum=MAX_AGGREGATE_LATENCY_MS,
        integer_total=False,
    )


def _metric_value_from_aggregate(aggregate: Mapping[str, Any], metric: str) -> Any:
    if metric in QUALITY_METRICS:
        return aggregate[metric]["value"]
    if metric in SAFETY_METRICS:
        return aggregate[metric]
    return aggregate[metric]["median"]


def _validate_metric_comparison(value: Any, label: str) -> None:
    _exact(value, {"metric_id", "control", "treatment", "direction", "source_kind"}, label)
    metric = value["metric_id"]
    if metric not in ALL_METRICS:
        raise ShadowError(f"{label}.metric_id is unsupported")
    if value["source_kind"] != "measured" or value["direction"] not in {
        "supporting",
        "counterevidence",
        "neutral",
        "unknown",
    }:
        raise ShadowError(f"{label} evidence classification is invalid")
    for field in ("control", "treatment"):
        observed = value[field]
        if observed is None:
            if metric not in QUALITY_METRICS:
                raise ShadowError(f"{label}.{field} cannot be null")
            continue
        maximum = (
            MAX_AGGREGATE_RATIO_COMPONENT
            if metric in QUALITY_METRICS
            else MAX_AGGREGATE_SAFETY_COUNT
            if metric in SAFETY_METRICS
            else MAX_CONTEXT_TOKENS
            if metric == "context_tokens_per_task"
            else MAX_LATENCY_MS
        )
        _strict_number(
            observed,
            f"{label}.{field}",
            integer=metric in SAFETY_METRICS,
            maximum=maximum,
        )


def _validate_feedback_evidence(value: Any, label: str) -> None:
    _exact(
        value,
        {
            "evidence_id",
            "source_kind",
            "evidence_class",
            "direction",
            "metric_refs",
            "artifact_refs",
        },
        label,
    )
    _portable(value["evidence_id"], PORTABLE_ID, f"{label}.evidence_id")
    evidence_class = value["evidence_class"]
    if evidence_class not in FEEDBACK_KINDS or value["source_kind"] != FEEDBACK_KINDS[evidence_class]:
        raise ShadowError(f"{label} feedback class/source_kind is invalid")
    if value["direction"] not in {"supporting", "counterevidence", "neutral", "unknown"}:
        raise ShadowError(f"{label}.direction is invalid")
    metric_refs = value["metric_refs"]
    artifact_refs = value["artifact_refs"]
    if not isinstance(metric_refs, list) or len(metric_refs) > 20:
        raise ShadowError(f"{label}.metric_refs exceeds the v1 bounds")
    for index, item in enumerate(metric_refs):
        _exact(
            item,
            {"metric_id", "aggregate_ref", "aggregate_sha256", "interpretation"},
            f"{label}.metric_refs[{index}]",
        )
        if item["metric_id"] not in ALL_METRICS or item["interpretation"] not in {
            "supporting",
            "conflicting",
            "unknown",
        }:
            raise ShadowError(f"{label}.metric_refs[{index}] is invalid")
        _portable(
            item["aggregate_ref"],
            PORTABLE_REF,
            f"{label}.metric_refs[{index}].aggregate_ref",
            maximum=MAX_REF,
        )
        if not isinstance(item["aggregate_sha256"], str) or not SHA256.fullmatch(
            item["aggregate_sha256"]
        ):
            raise ShadowError(f"{label}.metric_refs[{index}].aggregate_sha256 is invalid")
    if (
        not isinstance(artifact_refs, list)
        or len(artifact_refs) > 20
        or len(artifact_refs) != len(set(artifact_refs))
    ):
        raise ShadowError(f"{label}.artifact_refs exceeds the v1 bounds")
    for index, item in enumerate(artifact_refs):
        _portable(item, PORTABLE_REF, f"{label}.artifact_refs[{index}]", maximum=MAX_REF)


def _validate_evidence_result(
    value: Any,
    comparisons: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    _exact(value, {"support", "counterevidence", "neutral_or_unknown"}, "result.evidence")
    all_items: list[Mapping[str, Any]] = []
    expected_direction = {
        "support": {"supporting"},
        "counterevidence": {"counterevidence"},
        "neutral_or_unknown": {"neutral", "unknown"},
    }
    metric_items: dict[str, Mapping[str, Any]] = {}
    evidence_ids: set[str] = set()
    for bucket, directions in expected_direction.items():
        items = value[bucket]
        if not isinstance(items, list):
            raise ShadowError(f"result.evidence.{bucket} must be an array")
        for index, item in enumerate(items):
            label = f"result.evidence.{bucket}[{index}]"
            if not isinstance(item, dict) or item.get("direction") not in directions:
                raise ShadowError(f"{label} is in the wrong evidence bucket")
            if "metric_id" in item:
                _validate_metric_comparison(item, label)
                metric_id = item["metric_id"]
                if metric_id in metric_items:
                    raise ShadowError("result evidence repeats a measured metric")
                metric_items[metric_id] = item
            else:
                _validate_feedback_evidence(item, label)
                evidence_id = item["evidence_id"]
                if evidence_id in evidence_ids:
                    raise ShadowError("result evidence repeats a feedback event")
                evidence_ids.add(evidence_id)
            all_items.append(item)
    if len(all_items) > MAX_EVIDENCE_ITEMS:
        raise ShadowError("result evidence exceeds the v1 bounds")
    if comparisons is None:
        if metric_items:
            raise ShadowError("rejected result cannot contain measured comparisons")
    else:
        expected = {item["metric_id"]: item for item in comparisons}
        if metric_items != expected:
            raise ShadowError("result evidence does not exactly match measured comparisons")
    return all_items


def _validate_result_confidence(
    value: Any,
    evidence: Sequence[Mapping[str, Any]],
    declared: Any,
) -> None:
    _exact(
        value,
        {
            "formula_version",
            "evidence_derived",
            "declared_candidate_confidence",
            "weighted_support",
            "weighted_counterevidence",
            "evaluated_confidence",
            "approval_permission",
        },
        "result.confidence",
    )
    if value != _confidence(evidence, declared):
        raise ShadowError("result confidence does not match the governed evidence formula")


def _validate_failure_modes(value: Any) -> None:
    if not isinstance(value, list) or len(value) > MAX_FAILURE_MODES:
        raise ShadowError("result failure modes exceed the v1 bounds")
    for index, item in enumerate(value):
        _exact(item, {"arm", "case_id", "code", "failure_ref"}, f"result.failure_modes[{index}]")
        if item["arm"] not in {"control", "treatment", "preflight", "comparison"}:
            raise ShadowError(f"result.failure_modes[{index}].arm is invalid")
        for field in ("case_id", "code", "failure_ref"):
            _portable(item[field], PORTABLE_ID, f"result.failure_modes[{index}].{field}")


def _validate_governance(value: Any) -> None:
    fields = {
        "automatic_promotion",
        "candidate_status_changed",
        "canonical_knowledge_written",
        "git_written",
        "provider_index_written",
        "project_source_written",
        "next_required_steps",
    }
    _exact(value, fields, "result.governance")
    for field in fields - {"next_required_steps"}:
        if value[field] is not False:
            raise ShadowError("result governance write permissions must all be false")
    steps = value["next_required_steps"]
    if not isinstance(steps, list) or tuple(steps) != GOVERNANCE_STEPS:
        raise ShadowError("result governance steps are invalid")


def validate_result(value: Mapping[str, Any]) -> None:
    _exact(value, RESULT_FIELDS, "result")
    if (
        value["schema_version"] != RESULT_VERSION
        or value["contract_version"] != CONTRACT_VERSION
        or value["metric_contract"] != BASELINE_CONTRACT_VERSION
    ):
        raise ShadowError("unsupported Shadow Evaluation result")
    contract, contract_raw = _load_contract()
    if (
        value["metric_contract_sha256"] != BASELINE_CONTRACT_SHA256
        or value["metric_contract_sha256"] != contract["metric_contract_sha256"]
        or value["contract_sha256"] != hashlib.sha256(contract_raw).hexdigest()
        or not isinstance(value["replay_sha256"], str)
        or not SHA256.fullmatch(value["replay_sha256"])
    ):
        raise ShadowError("Shadow Evaluation result contract hashes are invalid")
    _portable(value["evaluation_id"], PORTABLE_ID, "result.evaluation_id")
    _validate_result_dataset(value["dataset"])
    _validate_candidate_snapshot(value["candidate"])
    _validate_preflight_result(value["preflight"])
    if value["status"] not in {"conclusive", "inconclusive", "rejected_preflight"}:
        raise ShadowError("result status is invalid")
    if value["degradation_status"] not in {"none", "degraded"}:
        raise ShadowError("result degradation status is invalid")
    if value["recommendation"] not in {
        "consider_for_separate_curation",
        "do_not_promote_on_shadow_evidence",
        "inconclusive",
        "preflight_rejected",
    }:
        raise ShadowError("result recommendation is invalid")

    comparisons: list[Mapping[str, Any]] | None = None
    measurements = value["measurements"]
    if measurements is not None:
        _exact(
            measurements,
            {"control", "treatment", "comparisons", "context_cost_and_latency_are_diagnostic_only"},
            "result.measurements",
        )
        if measurements["context_cost_and_latency_are_diagnostic_only"] is not True:
            raise ShadowError("result telemetry governance marker is invalid")
        _validate_arm_aggregate(measurements["control"], "result.measurements.control")
        _validate_arm_aggregate(measurements["treatment"], "result.measurements.treatment")
        comparisons = measurements["comparisons"]
        if not isinstance(comparisons, list) or len(comparisons) != len(ALL_METRICS):
            raise ShadowError("result measurements must contain every exact metric comparison")
        seen: set[str] = set()
        for index, item in enumerate(comparisons):
            _validate_metric_comparison(item, f"result.measurements.comparisons[{index}]")
            metric = item["metric_id"]
            if metric in seen or item["control"] != _metric_value_from_aggregate(
                measurements["control"], metric
            ) or item["treatment"] != _metric_value_from_aggregate(
                measurements["treatment"], metric
            ):
                raise ShadowError("result comparison does not match its exact aggregate")
            seen.add(metric)
        if seen != set(ALL_METRICS):
            raise ShadowError("result comparison metric set is incomplete")
        expected_comparisons = {
            item["metric_id"]: item
            for item in _metric_comparison(
                measurements["control"], measurements["treatment"]
            )
        }
        if {item["metric_id"]: item for item in comparisons} != expected_comparisons:
            raise ShadowError("result comparison direction is not derived from its aggregates")

    evidence = _validate_evidence_result(value["evidence"], comparisons)
    declared = value["candidate"].get("declared_confidence") if value["candidate"] else None
    _validate_result_confidence(value["confidence"], evidence, declared)
    _validate_failure_modes(value["failure_modes"])
    _validate_governance(value["governance"])

    passed = value["preflight"]["passed"]
    if not passed:
        if (
            value["status"] != "rejected_preflight"
            or value["recommendation"] != "preflight_rejected"
            or value["degradation_status"] != "none"
            or measurements is not None
        ):
            raise ShadowError("rejected result violates the preflight governance invariant")
    else:
        candidate = value["candidate"]
        if (
            candidate is None
            or candidate["status"] != "candidate"
            or candidate["source_commit"] is None
            or candidate["content_sha256"] is None
            or measurements is None
            or value["status"] == "rejected_preflight"
            or value["recommendation"] == "preflight_rejected"
        ):
            raise ShadowError("passed result lacks exact candidate measurements")
        if candidate["scope"] == "project" and candidate["project_id"] != value["dataset"]["project_id"]:
            raise ShadowError("passed result candidate scope does not match the dataset")

    failures = value["failure_modes"]
    recommendation = value["recommendation"]
    if recommendation == "consider_for_separate_curation":
        measured_support = [
            item
            for item in value["evidence"]["support"]
            if item.get("source_kind") == "measured"
            and item.get("metric_id") in (*QUALITY_METRICS, *SAFETY_METRICS)
            and item.get("direction") == "supporting"
        ]
        if (
            not passed
            or value["status"] != "conclusive"
            or value["degradation_status"] != "none"
            or failures
            or not measured_support
        ):
            raise ShadowError("positive result lacks governed measured support")
    if value["status"] == "conclusive" and (
        value["degradation_status"] != "none"
        or recommendation not in {
            "consider_for_separate_curation",
            "do_not_promote_on_shadow_evidence",
        }
        or failures
    ):
        raise ShadowError("conclusive result has inconsistent failures or recommendation")
    if value["status"] == "inconclusive" and (
        recommendation != "inconclusive" or not failures
    ):
        raise ShadowError("inconclusive result must preserve its failure evidence")


def render_report(result: Mapping[str, Any]) -> str:
    validate_result(result)
    lines = [
        "# Shadow Evaluation report",
        "",
        f"- Evaluation: `{html.escape(str(result['evaluation_id']))}`",
        f"- Candidate: `{html.escape(str((result.get('candidate') or {}).get('candidate_id', 'unresolved')))}`",
        f"- Status: **{str(result['status']).upper()}**",
        f"- Degradation: `{result['degradation_status']}`",
        f"- Recommendation: `{result['recommendation']}`",
        f"- Contract: `{result['contract_version']}`",
        f"- Replay SHA-256: `{result['replay_sha256']}`",
        "",
        "> This report is evidence input only. It cannot approve, reject, obsolete, publish, commit, or reindex knowledge.",
        "",
        "## Preflight",
        "",
        f"- Passed: `{str(bool(result['preflight']['passed'])).lower()}`",
        f"- Rejection reasons: `{', '.join(result['preflight']['reasons']) or 'none'}`",
        "",
    ]
    measurements = result.get("measurements")
    if measurements is not None:
        lines.extend(["## Comparable measurements", "", "| Metric | Control | Treatment | Evidence direction |", "|---|---:|---:|---|"])
        for item in measurements["comparisons"]:
            lines.append(f"| `{item['metric_id']}` | {item['control']} | {item['treatment']} | `{item['direction']}` |")
        lines.append("")
    lines.extend(["## Evidence boundary", "", "| Class | Supporting | Counterevidence | Neutral/unknown |", "|---|---:|---:|---:|"])
    classes = ("measured", "human_judgment", "model_inference", "unverified")
    buckets = result["evidence"]
    for kind in classes:
        counts = [sum(1 for item in buckets[name] if item.get("source_kind") == kind) for name in ("support", "counterevidence", "neutral_or_unknown")]
        lines.append(f"| `{kind}` | {counts[0]} | {counts[1]} | {counts[2]} |")
    confidence = result["confidence"]
    lines.extend(
        [
            "",
            "## Confidence and failure modes",
            "",
            f"- Evidence-derived confidence (`{confidence['formula_version']}`): `{confidence['evaluated_confidence']}`",
            f"- Candidate-declared confidence (not permission): `{confidence['declared_candidate_confidence']}`",
            f"- Failure codes: `{', '.join(item['code'] for item in result['failure_modes']) or 'none'}`",
            "",
            "## Required governance",
            "",
            "A manager/curator must still perform a separate preview and approval, commit the exact canonical transition, then separately preview and approve any optional reindex.",
            "",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class _ArtifactRootPlan:
    path: Path
    identity: tuple[int, int, int, int]


def _artifact_root_plan(path: Path) -> _ArtifactRootPlan:
    root = _assert_existing_directory(path, label="artifact root")
    return _ArtifactRootPlan(root, _directory_identity(root.lstat()))


def _assert_artifact_root(
    artifact_root: Path,
    *,
    knowledge_root: Path,
    project_root: Path | None,
) -> _ArtifactRootPlan:
    plan = _artifact_root_plan(artifact_root)
    resolved_root = plan.path.resolve(strict=True)
    boundaries = [PLUGIN_ROOT, _assert_existing_directory(knowledge_root, label="knowledge root").resolve(strict=True)]
    if project_root is not None:
        boundaries.append(
            _assert_existing_directory(project_root, label="private pilot project").resolve(
                strict=True
            )
        )
    for boundary in boundaries:
        if _existing_object_is_within(resolved_root, boundary) or (
            _existing_object_is_within(boundary, resolved_root)
        ):
            raise ShadowError("artifact root overlaps plugin, canonical knowledge, or project source")
    return plan


def _publish_artifacts(
    root: _ArtifactRootPlan | Path,
    evaluation_id: str,
    result: Mapping[str, Any],
) -> tuple[Path, Path]:
    plan = root if isinstance(root, _ArtifactRootPlan) else _artifact_root_plan(root)
    result_name = f"{evaluation_id}.json"
    report_name = f"{evaluation_id}.md"
    payloads = {result_name: _json_bytes(result), report_name: render_report(result).encode("utf-8")}
    if len(payloads[report_name]) > MAX_RESULT_BYTES:
        raise ShadowError("report exceeds the configured size limit")
    published: list[tuple[str, tuple[int, int, int, int, int]]] = []
    pending: list[tuple[str, tuple[int, int, int, int, int]]] = []
    nonce = secrets.token_hex(24)
    try:
        with _BoundDirectory(plan.path, plan.path) as bound:
            if bound.token != plan.identity:
                raise ShadowError("artifact root identity changed after validation")
            try:
                for name in payloads:
                    if bound.child_identity(name) is not None:
                        raise ShadowError("an immutable Shadow Evaluation artifact already exists")
                for index, (name, payload) in enumerate(payloads.items()):
                    pending_name = f".{name}.pending-{nonce}-{index}"
                    descriptor = bound.open_exclusive(pending_name)
                    try:
                        with os.fdopen(descriptor, "wb") as handle:
                            descriptor = -1
                            handle.write(payload)
                            handle.flush()
                            os.fsync(handle.fileno())
                            identity = _file_identity(os.fstat(handle.fileno()))
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    pending.append((pending_name, identity))
                for (name, _), (pending_name, identity) in zip(payloads.items(), pending):
                    linked_identity = bound.link(pending_name, name)
                    if linked_identity != identity:
                        raise ShadowError("artifact identity changed during publication")
                    published.append((name, linked_identity))
                bound.verify_current()
                for pending_name, identity in pending:
                    if not bound.unlink_owned(pending_name, identity):
                        raise ShadowError("artifact pending identity changed during cleanup")
                pending.clear()
                for name, identity in published:
                    metadata = bound.child_stat(name)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or _file_identity(metadata) != identity
                    ):
                        raise ShadowError("published artifact is not one uniquely linked file")
                bound.verify_current()
            except Exception:
                for name, identity in published:
                    bound.unlink_owned(name, identity)
                for name, identity in pending:
                    bound.unlink_owned(name, identity)
                raise
    except (FeedbackError, OSError) as exc:
        raise ShadowError("artifact root could not be bound or written safely") from exc
    return plan.path / result_name, plan.path / report_name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--knowledge-root", required=True)
        command.add_argument("--replay", required=True)
        command.add_argument("--project-root")
        if name == "evaluate":
            command.add_argument("--expected-preview-sha256", required=True)
            command.add_argument("--artifact-root", required=True)
    report = commands.add_parser("report")
    report.add_argument("--result", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "report":
            result, _ = _read_json(Path(args.result), maximum=MAX_RESULT_BYTES, label="result")
            print(render_report(result), end="")
            return 0
        replay, raw = _read_json(Path(args.replay), maximum=MAX_REPLAY_BYTES, label="replay")
        knowledge_root = Path(args.knowledge_root)
        project_root = Path(args.project_root) if args.project_root else None
        preview, _ = build_preview(knowledge_root, replay, raw, project_root=project_root)
        if args.command == "preview":
            print(json.dumps(preview, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
            return 0
        result = evaluate(
            knowledge_root,
            replay,
            raw,
            expected_preview_sha256=args.expected_preview_sha256,
            project_root=project_root,
        )
        root = _assert_artifact_root(Path(args.artifact_root), knowledge_root=knowledge_root, project_root=project_root)
        result_path, report_path = _publish_artifacts(root, replay["evaluation_id"], result)
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "degradation_status": result["degradation_status"],
                    "recommendation": result["recommendation"],
                    "result_ref": result_path.name,
                    "report_ref": report_path.name,
                    "automatic_promotion": False,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except ArithmeticError:
        print("OPC_SHADOW_ERROR: numeric computation exceeded the v1 bounds", file=sys.stderr)
        return 2
    except (ShadowError, OpcMemoryError, FeedbackError, OSError) as exc:
        print(f"OPC_SHADOW_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
