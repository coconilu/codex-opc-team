#!/usr/bin/env python3
"""Govern versioned, evidence-gated OPC capability changes.

The lifecycle record is private derived evidence under ``.opc/evolution``.
Capability bytes remain File/Git authoritative.  Promotion and rollback only
materialize one pre-approved Git blob as an unstaged working-tree change; this
module never stages, commits, pushes, merges, or edits global Codex config.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from opc_feedback import (
    FeedbackError,
    _BoundDirectory,
    _assert_private_containment,
    _directory_identity,
    _exclusive_update_lock,
    _file_identity,
    _is_reparse,
)
from opc_sensitive import sensitive_text_label


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PLUGIN_ROOT / "assets" / "evolution"
CONTRACT_PATH = ASSET_ROOT / "capability-evolution-contract.v1.json"
PROPOSAL_SCHEMA_PATH = ASSET_ROOT / "capability-change-proposal.v1.schema.json"
RECORD_SCHEMA_PATH = ASSET_ROOT / "capability-evolution-record.v1.schema.json"
EVIDENCE_SCHEMA_PATH = ASSET_ROOT / "capability-evolution-evidence.v1.schema.json"
EVALUATION_CONTRACT_PATH = PLUGIN_ROOT.parents[1] / "evaluation" / "contracts" / "baseline-contract.v1.json"

CONTRACT_VERSION = "opc-capability-evolution-contract-v1"
PROPOSAL_VERSION = "opc-capability-change-proposal-v1"
RECORD_VERSION = "opc-capability-evolution-record-v1"
EVIDENCE_VERSION = "opc-capability-evolution-evidence-v1"
METRIC_CONTRACT_VERSION = "opc-evaluation-contract-v1"
REPORT_CLAIM = "association/evidence only"
METRIC_CONTRACT_SHA256 = "f7eda22695e25f91f15031d6a94d0183e399fc3b52b34a21130b7f567180444d"

MAX_PROPOSAL_BYTES = 256 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_CAPABILITY_BYTES = 1024 * 1024
MAX_CASES = 20
MAX_KNOWLEDGE = 64
MAX_REFS = 20
MAX_HISTORY = 100
MAX_ID = 128
MAX_REF = 240
MAX_VERSION = 64
MAX_TIMESTAMP = 32
MAX_METRIC = 1_000_000
MAX_CONTEXT = 10_000_000
MAX_LATENCY = 86_400_000

PORTABLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
PORTABLE_PROPOSAL = re.compile(r"^cap-[A-Za-z0-9._-]+$")
PORTABLE_RUN = re.compile(r"^opc-[A-Za-z0-9._-]+$")
PORTABLE_REF = re.compile(
    r"^(?!/)(?![A-Za-z]:)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
VERSION = re.compile(r"^[A-Za-z0-9._+-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
UUID_TOKEN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
RUNTIME_ID = re.compile(r"(?i)(?:session|turn|thread)[._ -]?id")
HOST_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|/(?:home|Users)/)")
RAW_PRIVATE = re.compile(
    r"(?i)(?:raw[-_ ]?(?:chat|conversation|prompt|hook|payload)|"
    r"chain[-_ ]?of[-_ ]?thought|messages\s*[:=]\s*\[)"
)

PROPOSAL_KEYS = {
    "schema_version", "contract_version", "proposal_id", "project_id",
    "sources", "capability", "current_version", "candidate_version",
    "rollback_target", "scope", "owner", "pilot", "created_at",
}
SOURCE_KEYS = {"candidate_refs", "feedback_refs", "evaluation_refs", "lineage_refs"}
SOURCE_REF_KEYS = {"ref", "sha256"}
CAPABILITY_KEYS = {"kind", "target_path"}
VERSION_KEYS = {"version", "source_path", "source_commit", "content_sha256"}
SCOPE_KEYS = {"kind", "project_id"}
PILOT_KEYS = {"min_cases", "max_cases", "observation_cases"}
EVIDENCE_KEYS = {"kind", "ref", "sha256"}
AUTH_KEYS = {"manager_approval", "independent_qa", "shadow", "recorded_at"}
CASE_KEYS = {"case_id", "control", "candidate"}
ARM_KEYS = {
    "run_id", "execution_status", "evaluation_contract", "capability_version",
    "knowledge_versions", "lineage", "measurements", "unavailable_reason",
}
KNOWLEDGE_KEYS = {"record_id", "source_path", "source_commit", "content_sha256"}
METRIC_CONTRACT_KEYS = {"version", "sha256"}
METRICS_KEYS = {
    "manager_intervention_rate", "qa_catch_rate", "rework_loops_per_task",
    "valid_knowledge_reuse_rate", "false_recall_rate",
    "scope_leakage_acceptances", "stale_obsolete_acceptances",
    "privacy_failures", "context_tokens_per_task", "latency_ms",
}
RATIO_METRICS = {
    "manager_intervention_rate", "qa_catch_rate", "rework_loops_per_task",
    "valid_knowledge_reuse_rate", "false_recall_rate",
}
LOWER_IS_BETTER = {
    "manager_intervention_rate", "rework_loops_per_task", "false_recall_rate",
    "context_tokens_per_task", "latency_ms",
}
HIGHER_IS_BETTER = {"qa_catch_rate", "valid_knowledge_reuse_rate"}
SAFETY_METRICS = {
    "scope_leakage_acceptances", "stale_obsolete_acceptances", "privacy_failures",
}
STATES = {
    "candidate", "pilot_approved", "piloting", "evaluated",
    "promotion_pending", "promoted", "observing", "rollback_pending",
    "rolled_back", "rejected",
}
HISTORY_ACTIONS = {
    "opened", "pilot_authorized", "pilot_case_recorded", "evaluated",
    "promotion_authorized", "promotion_applied", "promotion_confirmed",
    "observation_started", "rollback_applied", "rollback_confirmed", "rejected",
}
HISTORY_TRANSITIONS = {
    "opened": (None, "candidate"),
    "pilot_authorized": ("candidate", "pilot_approved"),
    "pilot_case_recorded": ({"pilot_approved", "piloting"}, "piloting"),
    "evaluated": ("piloting", "evaluated"),
    "promotion_authorized": ("evaluated", "evaluated"),
    "promotion_applied": ("evaluated", "promotion_pending"),
    "promotion_confirmed": ("promotion_pending", "promoted"),
    "observation_started": ({"promoted", "observing"}, "observing"),
    "rollback_applied": ({"promoted", "observing"}, "rollback_pending"),
    "rollback_confirmed": ("rollback_pending", "rolled_back"),
    "rejected": ({"candidate", "pilot_approved", "piloting", "evaluated"}, "rejected"),
}
HISTORY_EVIDENCE_KINDS = {
    "opened": (),
    "pilot_authorized": ("independent_qa", "manager_approval", "shadow"),
    "pilot_case_recorded": ("lineage", "lineage"),
    "evaluated": ("evaluation",),
    "promotion_authorized": ("independent_qa", "manager_approval", "shadow"),
    "promotion_applied": ("independent_qa", "manager_approval", "shadow"),
    "promotion_confirmed": (),
    "observation_started": None,
    "rollback_applied": None,
    "rollback_confirmed": (),
    "rejected": None,
}
UNIQUE_HISTORY_ACTIONS = {
    "opened", "pilot_authorized", "evaluated", "promotion_authorized",
    "promotion_applied", "promotion_confirmed", "rollback_applied",
    "rollback_confirmed", "rejected",
}
EXECUTION_STATES = {
    "completed", "timeout", "provider_unavailable", "provider_error", "failed",
}
EVIDENCE_KINDS = {
    "candidate", "manager_approval", "independent_qa", "shadow", "evaluation", "lineage",
    "outcome", "rollback_decision",
}
EVIDENCE_ENVELOPE_KEYS = {
    "schema_version", "evidence_kind", "proposal_id", "capability",
    "run_binding", "pilot_binding", "decision", "safety", "recorded_at",
}
EVIDENCE_CAPABILITY_KEYS = {
    "kind", "target_path", "current_version", "candidate_version",
}
RUN_BINDING_KEYS = {
    "case_id", "arm", "run_id", "capability_version", "knowledge_versions",
}
PILOT_BINDING_KEYS = {
    "case_ids", "control_run_ids", "candidate_run_ids", "lineage_refs",
}
LINEAGE_BINDING_KEYS = {"case_id", "arm", "run_id", "ref", "sha256"}
EVIDENCE_DECISIONS = {
    "proposed", "observed", "verified", "approved", "denied", "pass", "fail",
    "beneficial", "neutral", "harmful", "inconclusive", "regression_detected",
    "rollback_approved", "rollback_denied",
}
EVIDENCE_SAFETY = {"safe", "unsafe", "inconclusive", "not_applicable"}
SOURCE_EVIDENCE_POLICY = {
    "candidate": ({"proposed"}, {"not_applicable"}),
    "outcome": ({"observed"}, {"safe", "unsafe", "inconclusive", "not_applicable"}),
    "evaluation": ({"beneficial", "neutral", "harmful", "inconclusive"}, {"safe", "unsafe", "inconclusive"}),
    "lineage": ({"verified"}, {"not_applicable"}),
}
UNAVAILABLE_REASONS = {"timeout", "provider_unavailable", "provider_error", "failed"}
ALLOWED_GIT_BLOB_MODES = {"100644", "100755"}


class EvolutionError(RuntimeError):
    """Fail-closed error; never include user-provided bodies in messages."""


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise EvolutionError(f"{label} fields are not strict")
    return value


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise EvolutionError("non-finite JSON numbers are forbidden")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_non_finite(child)
    elif isinstance(value, list):
        for child in value:
            _reject_non_finite(child)


def _json_bytes(value: Mapping[str, Any], *, maximum: int) -> bytes:
    _reject_non_finite(value)
    try:
        raw = (
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EvolutionError("value cannot be serialized as strict JSON") from exc
    if len(raw) > maximum:
        raise EvolutionError("JSON value exceeds the configured size limit")
    return raw


def _decode_json(raw: bytes, *, maximum: int, label: str) -> dict[str, Any]:
    if len(raw) > maximum:
        raise EvolutionError(f"{label} exceeds the configured size limit")
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvolutionError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EvolutionError(f"{label} must be a JSON object")
    _reject_non_finite(value)
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _portable(value: Any, pattern: re.Pattern[str], label: str, maximum: int = MAX_ID) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > maximum
        or pattern.fullmatch(value) is None or UUID_TOKEN.search(value)
        or RUNTIME_ID.search(value)
    ):
        raise EvolutionError(f"{label} is not a portable identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise EvolutionError(f"{label} must be a lowercase SHA-256")
    return value


def _commit(value: Any, label: str) -> str:
    if not isinstance(value, str) or GIT_COMMIT.fullmatch(value) is None:
        raise EvolutionError(f"{label} must be an exact Git commit")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP or UTC_TIMESTAMP.fullmatch(value) is None:
        raise EvolutionError(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvolutionError(f"{label} is invalid") from exc


def _read_json_file(path: Path, *, maximum: int, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or _is_reparse(path) or metadata.st_nlink != 1:
            raise EvolutionError(f"{label} must be a single-link regular file")
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
    except FileNotFoundError as exc:
        raise EvolutionError(f"{label} is unavailable") from exc
    except OSError as exc:
        raise EvolutionError(f"{label} could not be read safely") from exc
    return _decode_json(raw, maximum=maximum, label=label), raw


def _load_contract() -> tuple[dict[str, Any], str]:
    value, raw = _read_json_file(CONTRACT_PATH, maximum=64 * 1024, label="evolution contract")
    if (
        value.get("contract_version") != CONTRACT_VERSION
        or value.get("proposal_schema_version") != PROPOSAL_VERSION
        or value.get("record_schema_version") != RECORD_VERSION
        or value.get("evidence_schema_version") != EVIDENCE_VERSION
        or value.get("authority") != "file-git-only"
        or value.get("report_claim") != REPORT_CLAIM
        or value.get("causal_claim_allowed") is not False
    ):
        raise EvolutionError("evolution contract authority or version is unsupported")
    evaluation = value.get("evaluation") or {}
    try:
        metric_raw = EVALUATION_CONTRACT_PATH.read_bytes()
    except OSError as exc:
        raise EvolutionError("evaluation metric contract is unavailable") from exc
    if (
        evaluation.get("metric_contract") != METRIC_CONTRACT_VERSION
        or evaluation.get("metric_contract_sha256") != METRIC_CONTRACT_SHA256
        or _sha(metric_raw) != METRIC_CONTRACT_SHA256
    ):
        raise EvolutionError("evaluation metric contract hash drifted")
    return value, _sha(raw)


def _validate_version(value: Any, label: str) -> None:
    obj = _exact(value, VERSION_KEYS, label)
    _portable(obj["version"], VERSION, f"{label}.version", MAX_VERSION)
    _portable(obj["source_path"], PORTABLE_REF, f"{label}.source_path", MAX_REF)
    _commit(obj["source_commit"], f"{label}.source_commit")
    _hash(obj["content_sha256"], f"{label}.content_sha256")


def _validate_source_ref(value: Any, label: str) -> None:
    obj = _exact(value, SOURCE_REF_KEYS, label)
    ref = _portable(obj["ref"], PORTABLE_REF, f"{label}.ref", MAX_REF)
    if not ref.startswith(".opc/"):
        raise EvolutionError(f"{label}.ref must remain under private .opc")
    _hash(obj["sha256"], f"{label}.sha256")


def _managed_path(kind: str, path: str) -> bool:
    parts = path.split("/")
    if kind == "role":
        return (
            len(parts) == 2 and parts[0] in {"roles", "agents"}
            and re.fullmatch(r"[A-Za-z0-9._-]+\.(?:md|toml|yaml)", parts[1]) is not None
        ) or (
            len(parts) == 5 and parts[0] == "plugins" and parts[2:4] == ["assets", "agent-configs"]
            and parts[4].endswith(".toml")
        )
    if kind == "skill":
        offset = 0
        if len(parts) >= 5 and parts[0] == "plugins" and parts[2] == "skills":
            offset = 2
        if len(parts) == offset + 3 and parts[offset] == "skills" and parts[-1] == "SKILL.md":
            return True
        return (
            len(parts) == offset + 4 and parts[offset] == "skills"
            and parts[offset + 2] == "references" and parts[-1].endswith(".md")
        )
    if kind == "organization_policy":
        return path == "AGENTS.md" or (
            len(parts) == 2 and parts[0] == "policies" and parts[1].endswith(".md")
        ) or (
            len(parts) == 3 and parts[:2] == ["docs", "policies"] and parts[2].endswith(".md")
        )
    return False


def validate_proposal(value: Mapping[str, Any]) -> None:
    proposal = _exact(value, PROPOSAL_KEYS, "proposal")
    if proposal["schema_version"] != PROPOSAL_VERSION or proposal["contract_version"] != CONTRACT_VERSION:
        raise EvolutionError("proposal schema or contract version is unsupported")
    _portable(proposal["proposal_id"], PORTABLE_PROPOSAL, "proposal_id")
    project_id = _portable(proposal["project_id"], PORTABLE_ID, "project_id")
    _portable(proposal["owner"], PORTABLE_ID, "owner")
    _timestamp(proposal["created_at"], "created_at")

    sources = _exact(proposal["sources"], SOURCE_KEYS, "sources")
    candidates = sources["candidate_refs"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_REFS:
        raise EvolutionError("candidate_refs must contain 1..20 items")
    for key, minimum in (("candidate_refs", 1), ("feedback_refs", 0), ("evaluation_refs", 1), ("lineage_refs", 1)):
        refs = sources[key]
        if not isinstance(refs, list) or not minimum <= len(refs) <= MAX_REFS:
            raise EvolutionError(f"{key} has an invalid bounded count")
        for index, ref in enumerate(refs):
            _validate_source_ref(ref, f"{key}[{index}]")
        identities = [(item["ref"], item["sha256"]) for item in refs]
        if len(set(identities)) != len(identities):
            raise EvolutionError(f"{key} must be unique")

    capability = _exact(proposal["capability"], CAPABILITY_KEYS, "capability")
    kind = capability["kind"]
    target = _portable(capability["target_path"], PORTABLE_REF, "target_path", MAX_REF)
    if kind not in {"role", "skill", "organization_policy"} or not _managed_path(kind, target):
        raise EvolutionError("capability target is outside the managed path allowlist")
    for label in ("current_version", "candidate_version", "rollback_target"):
        _validate_version(proposal[label], label)
        if proposal[label]["source_path"] != target:
            raise EvolutionError(f"{label} must identify the exact managed target")
    current = proposal["current_version"]
    candidate = proposal["candidate_version"]
    rollback = proposal["rollback_target"]
    if current["version"] == candidate["version"] or current["content_sha256"] == candidate["content_sha256"]:
        raise EvolutionError("candidate version must differ from current version")
    if rollback != current:
        raise EvolutionError("v1 rollback_target must exactly preserve the current version")

    scope = _exact(proposal["scope"], SCOPE_KEYS, "scope")
    if scope["kind"] == "project":
        if scope["project_id"] != project_id:
            raise EvolutionError("project-scoped proposal must match project_id")
    elif scope["kind"] == "organization":
        if scope["project_id"] is not None:
            raise EvolutionError("organization scope must not infer a project id")
    else:
        raise EvolutionError("proposal scope is unsupported")
    pilot = _exact(proposal["pilot"], PILOT_KEYS, "pilot")
    values = [pilot[key] for key in ("min_cases", "max_cases", "observation_cases")]
    if any(isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= MAX_CASES for item in values):
        raise EvolutionError("pilot bounds must be integers from 1 to 20")
    if pilot["min_cases"] > pilot["max_cases"]:
        raise EvolutionError("pilot minimum cannot exceed maximum")
    _json_bytes(proposal, maximum=MAX_PROPOSAL_BYTES)


def _git(root: Path, args: Sequence[str], *, binary: bool = False) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=False,
            capture_output=True, text=not binary, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise EvolutionError("Git verification is unavailable") from exc


def _git_root(root: Path) -> Path:
    candidate = root.expanduser().resolve(strict=True)
    probe = _git(candidate, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise EvolutionError("capability repository Git detection failed closed")
    top = _git(candidate, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or not top.stdout.strip():
        raise EvolutionError("capability repository boundary is unavailable")
    resolved = Path(top.stdout.strip()).resolve(strict=True)
    try:
        if not os.path.samefile(candidate, resolved):
            raise EvolutionError("capability repository root must be the Git top-level")
    except OSError as exc:
        raise EvolutionError("capability repository identity is unavailable") from exc
    return resolved


def _git_head(root: Path) -> str:
    result = _git(root, ["rev-parse", "HEAD"])
    head = result.stdout.strip() if result.returncode == 0 else ""
    return _commit(head, "repository HEAD")


def _git_blob_info(root: Path, commit: str, path: str) -> tuple[str, str, bytes]:
    _commit(commit, "blob commit")
    _portable(path, PORTABLE_REF, "blob path", MAX_REF)
    listing = _git(root, ["ls-tree", "-z", commit, "--", path], binary=True)
    raw_listing = bytes(listing.stdout)
    entries = [item for item in raw_listing.split(b"\0") if item]
    if listing.returncode != 0 or len(entries) != 1 or b"\t" not in entries[0]:
        raise EvolutionError("managed capability Git object is unavailable or ambiguous")
    header, encoded_path = entries[0].split(b"\t", 1)
    try:
        mode, object_type, object_id = header.decode("ascii").split(" ", 2)
        listed_path = encoded_path.decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise EvolutionError("managed capability Git object metadata is invalid") from exc
    if listed_path.replace("\\", "/") != path:
        raise EvolutionError("managed capability Git object path does not match")
    if mode not in ALLOWED_GIT_BLOB_MODES or object_type != "blob":
        raise EvolutionError("managed capability must be a regular Git blob")
    size = _git(root, ["cat-file", "-s", object_id])
    try:
        object_size = int(size.stdout.strip()) if size.returncode == 0 else -1
    except ValueError as exc:
        raise EvolutionError("managed capability Git blob size is invalid") from exc
    if not 0 <= object_size <= MAX_CAPABILITY_BYTES:
        raise EvolutionError("managed capability Git blob is unavailable or oversized")
    content = _git(root, ["cat-file", "blob", object_id], binary=True)
    if content.returncode != 0 or len(content.stdout) != object_size:
        raise EvolutionError("managed capability Git blob could not be read exactly")
    return mode, object_id, bytes(content.stdout)


def _git_blob(root: Path, commit: str, path: str) -> bytes:
    return _git_blob_info(root, commit, path)[2]


def _strict_linear_target_range(root: Path, base: str, head: str, target: str,
                                *, label: str) -> list[str]:
    _commit(base, f"{label} base")
    _commit(head, f"{label} head")
    if base == head:
        raise EvolutionError(f"{label} requires an explicit new commit")
    ancestor = _git(root, ["merge-base", "--is-ancestor", base, head])
    if ancestor.returncode != 0:
        raise EvolutionError(f"{label} head must strictly descend from its base")
    listing = _git(root, ["rev-list", "--reverse", "--topo-order", f"{base}..{head}"])
    commits = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    if listing.returncode != 0 or not commits:
        raise EvolutionError(f"{label} commit range is unavailable")
    expected_parent = base
    for commit_id in commits:
        _commit(commit_id, f"{label} commit")
        parents = _git(root, ["rev-list", "--parents", "-n", "1", commit_id])
        tokens = parents.stdout.strip().split() if parents.returncode == 0 else []
        if len(tokens) != 2 or tokens[0] != commit_id or tokens[1] != expected_parent:
            raise EvolutionError(f"{label} range must be linear and cannot contain merge commits")
        changed = _git(
            root,
            ["diff-tree", "--no-commit-id", "--name-status", "-r", "-z",
             "--no-renames", expected_parent, commit_id, "--"],
            binary=True,
        )
        fields = [item for item in bytes(changed.stdout).split(b"\0") if item]
        if changed.returncode != 0 or len(fields) != 2:
            raise EvolutionError(f"{label} commit must change exactly the managed target")
        try:
            status = fields[0].decode("ascii")
            changed_path = fields[1].decode("utf-8").replace("\\", "/")
        except UnicodeError as exc:
            raise EvolutionError(f"{label} commit path metadata is invalid") from exc
        if status != "M" or changed_path != target:
            raise EvolutionError(
                f"{label} rejects add/delete/rename/copy/typechange and non-target paths"
            )
        _git_blob_info(root, commit_id, target)
        expected_parent = commit_id
    return commits


def _candidate_is_narrow(root: Path, current: str, candidate: str, target: str) -> None:
    _strict_linear_target_range(root, current, candidate, target, label="candidate")


def _assert_clean(root: Path) -> None:
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status.returncode != 0:
        raise EvolutionError("worktree status verification failed closed")
    if status.stdout.strip():
        raise EvolutionError("capability worktree contains unrelated or uncommitted changes")


def _target_path(root: Path, relative: str) -> Path:
    target = root / Path(*relative.split("/"))
    current = target.parent
    while True:
        if current.is_symlink() or (current.exists() and _is_reparse(current)):
            raise EvolutionError("managed capability path crosses a linked directory")
        try:
            if current.exists() and os.path.samefile(current, root):
                break
        except OSError as exc:
            raise EvolutionError("managed path identity could not be verified") from exc
        parent = current.parent
        if parent == current:
            raise EvolutionError("managed capability path escaped repository root")
        current = parent
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode) or target.is_symlink() or _is_reparse(target) or metadata.st_nlink != 1:
            raise EvolutionError("managed capability must be a single-link regular file")
    return target


def _privacy_scan(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise EvolutionError("managed capability must be UTF-8 text") from exc
    if (
        HOST_PATH.search(text) or RUNTIME_ID.search(text) or UUID_TOKEN.search(text)
        or RAW_PRIVATE.search(text) or sensitive_text_label(text) is not None
    ):
        raise EvolutionError("candidate capability failed the privacy gate")


def _verify_proposal_git(root: Path, proposal: Mapping[str, Any], *, require_head: bool) -> None:
    validate_proposal(proposal)
    target = proposal["capability"]["target_path"]
    current = proposal["current_version"]
    candidate = proposal["candidate_version"]
    rollback = proposal["rollback_target"]
    if require_head and _git_head(root) != current["source_commit"]:
        raise EvolutionError("repository HEAD drifted from the proposal current version")
    for label, version in (("current", current), ("candidate", candidate), ("rollback", rollback)):
        raw = _git_blob(root, version["source_commit"], target)
        if _sha(raw) != version["content_sha256"]:
            raise EvolutionError(f"{label} capability Git blob hash does not match")
        _privacy_scan(raw)
    _candidate_is_narrow(root, current["source_commit"], candidate["source_commit"], target)


def _project_context(project_root: Path, project_id: str) -> tuple[Path, Path]:
    project = project_root.expanduser().resolve(strict=True)
    try:
        record, _ = _read_json_file(project / ".opc" / "project.json", maximum=64 * 1024, label="project record")
    except EvolutionError:
        raise
    if record.get("project_id") != project_id:
        raise EvolutionError("proposal does not match the private project record")
    _portable(project_id, PORTABLE_ID, "project_id")
    path = project / ".opc" / "evolution"
    try:
        _assert_private_containment(project, path / "placeholder")
    except FeedbackError as exc:
        raise EvolutionError("evolution storage escaped the private project boundary") from exc
    return project, path


class _EvolutionBinding:
    """Hold project, .opc, and evolution directory objects across apply preview/write."""

    def __init__(self, project_root: Path):
        self.project = project_root.expanduser().resolve(strict=True)
        self.directory = self.project / ".opc" / "evolution"
        self.project_bound = _BoundDirectory(self.project, self.project)
        self.opc_bound = _BoundDirectory(self.project / ".opc", self.project)
        self.evolution_bound: _BoundDirectory | None = None
        self.evolution_missing = False
        self.acquired = False

    def __enter__(self) -> "_EvolutionBinding":
        try:
            self.project_bound.__enter__()
            self.opc_bound.__enter__()
            try:
                metadata = self.directory.lstat()
            except FileNotFoundError:
                self.evolution_missing = True
            else:
                if (
                    not stat.S_ISDIR(metadata.st_mode) or self.directory.is_symlink()
                    or _is_reparse(self.directory)
                ):
                    raise EvolutionError("evolution directory is not a stable directory")
                self.evolution_bound = _BoundDirectory(self.directory, self.project)
                self.evolution_bound.__enter__()
            self.acquired = True
            self.verify()
            return self
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        if not self.acquired:
            raise EvolutionError("evolution filesystem binding is unavailable")
        try:
            self.project_bound.verify_current()
            self.opc_bound.verify_current()
            if not os.path.samefile(self.opc_bound.path.parent, self.project_bound.path):
                raise EvolutionError("private runtime parent identity changed")
            if self.evolution_bound is None:
                try:
                    self.directory.lstat()
                except FileNotFoundError:
                    return
                raise EvolutionError("evolution directory appeared after preview binding")
            self.evolution_bound.verify_current()
            if not os.path.samefile(self.evolution_bound.path.parent, self.opc_bound.path):
                raise EvolutionError("evolution directory parent identity changed")
        except FeedbackError as exc:
            raise EvolutionError("private evolution filesystem identity changed") from exc
        except OSError as exc:
            raise EvolutionError("private evolution filesystem identity could not be verified") from exc

    def ensure_directory(self) -> _BoundDirectory:
        self.verify()
        if self.evolution_bound is None:
            bound = _BoundDirectory(self.directory, self.project)
            try:
                bound.__enter__()
                self.evolution_bound = bound
                self.evolution_missing = False
                self.verify()
            except BaseException:
                bound.close()
                raise
        return self.evolution_bound

    def close(self) -> None:
        if self.evolution_bound is not None:
            self.evolution_bound.close()
            self.evolution_bound = None
        self.opc_bound.close()
        self.project_bound.close()
        self.acquired = False

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _assert_private_or_ignored(project: Path, directory: Path) -> None:
    """Treat Git detection as git/non-git/error and require directory-level ignore."""
    marker_seen = False
    cursor = project
    while True:
        try:
            (cursor / ".git").lstat()
            marker_seen = True
            break
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EvolutionError("could not inspect the local Git boundary") from exc
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    probe = _git(project, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0:
        diagnostic = (probe.stderr or "").lower()
        if probe.returncode == 128 and "not a git repository" in diagnostic and not marker_seen:
            return
        raise EvolutionError("private project Git detection failed closed")
    if probe.stdout.strip() != "true":
        raise EvolutionError("private project Git detection returned an invalid result")
    top = _git(project, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or not top.stdout.strip():
        raise EvolutionError("private project Git boundary is unavailable")
    root = Path(top.stdout.strip()).resolve(strict=True)
    relative = directory.resolve(strict=False).relative_to(root).as_posix().rstrip("/") + "/"
    tracked = _git(root, ["ls-files", "--", relative])
    if tracked.returncode != 0 or tracked.stdout.strip():
        raise EvolutionError("private evolution directory is tracked or unverifiable")
    ignored = _git(root, ["check-ignore", "-q", "--", relative])
    if ignored.returncode == 1:
        raise EvolutionError("project-local evolution requires an ignored .opc/evolution directory")
    if ignored.returncode != 0:
        raise EvolutionError("private evolution ignore verification failed closed")


_UNSET = object()


def _validate_knowledge_versions(value: Any, label: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_KNOWLEDGE:
        raise EvolutionError(f"{label} exceeds bounds")
    identities: set[tuple[str, str, str, str]] = set()
    for index, item in enumerate(value):
        obj = _exact(item, KNOWLEDGE_KEYS, f"{label}[{index}]")
        _portable(obj["record_id"], PORTABLE_ID, "record_id")
        _portable(obj["source_path"], PORTABLE_REF, "knowledge source_path", MAX_REF)
        _commit(obj["source_commit"], "knowledge source_commit")
        _hash(obj["content_sha256"], "knowledge content_sha256")
        identity = tuple(str(obj[key]) for key in sorted(KNOWLEDGE_KEYS))
        if identity in identities:
            raise EvolutionError("knowledge_versions must be unique")
        identities.add(identity)


def _validate_run_binding(value: Any, label: str) -> None:
    binding = _exact(value, RUN_BINDING_KEYS, label)
    _portable(binding["case_id"], PORTABLE_ID, f"{label}.case_id")
    if binding["arm"] not in {"control", "candidate"}:
        raise EvolutionError(f"{label}.arm is unsupported")
    _portable(binding["run_id"], PORTABLE_RUN, f"{label}.run_id")
    _validate_version(binding["capability_version"], f"{label}.capability_version")
    _validate_knowledge_versions(binding["knowledge_versions"], f"{label}.knowledge_versions")


def _validate_pilot_binding(value: Any, label: str) -> None:
    binding = _exact(value, PILOT_BINDING_KEYS, label)
    for key in ("case_ids", "control_run_ids", "candidate_run_ids"):
        values = binding[key]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_CASES:
            raise EvolutionError(f"{label}.{key} exceeds bounds")
        pattern = PORTABLE_RUN if key != "case_ids" else PORTABLE_ID
        normalized = [_portable(item, pattern, f"{label}.{key}") for item in values]
        if normalized != sorted(set(normalized)):
            raise EvolutionError(f"{label}.{key} must be sorted and unique")
    refs = binding["lineage_refs"]
    if not isinstance(refs, list) or not 2 <= len(refs) <= MAX_CASES * 2:
        raise EvolutionError(f"{label}.lineage_refs exceeds bounds")
    identities: list[tuple[str, str, str, str, str]] = []
    for item in refs:
        obj = _exact(item, LINEAGE_BINDING_KEYS, f"{label}.lineage_ref")
        _portable(obj["case_id"], PORTABLE_ID, "lineage case_id")
        if obj["arm"] not in {"control", "candidate"}:
            raise EvolutionError("lineage binding arm is unsupported")
        _portable(obj["run_id"], PORTABLE_RUN, "lineage run_id")
        _portable(obj["ref"], PORTABLE_REF, "lineage ref", MAX_REF)
        _hash(obj["sha256"], "lineage sha256")
        identities.append((obj["case_id"], obj["arm"], obj["run_id"], obj["ref"], obj["sha256"]))
    if identities != sorted(set(identities)):
        raise EvolutionError(f"{label}.lineage_refs must be sorted and unique")


def _validate_evidence_envelope(value: Any, proposal: Mapping[str, Any], label: str) -> None:
    envelope = _exact(value, EVIDENCE_ENVELOPE_KEYS, label)
    if envelope["schema_version"] != EVIDENCE_VERSION:
        raise EvolutionError(f"{label}.schema_version is unsupported")
    if envelope["evidence_kind"] not in EVIDENCE_KINDS:
        raise EvolutionError(f"{label}.evidence_kind is unsupported")
    if envelope["proposal_id"] != proposal["proposal_id"]:
        raise EvolutionError(f"{label} belongs to another proposal")
    capability = _exact(envelope["capability"], EVIDENCE_CAPABILITY_KEYS, f"{label}.capability")
    if (
        capability["kind"] != proposal["capability"]["kind"]
        or capability["target_path"] != proposal["capability"]["target_path"]
        or capability["current_version"] != proposal["current_version"]
        or capability["candidate_version"] != proposal["candidate_version"]
    ):
        raise EvolutionError(f"{label} capability or version binding is stale")
    if envelope["run_binding"] is not None:
        _validate_run_binding(envelope["run_binding"], f"{label}.run_binding")
    if envelope["pilot_binding"] is not None:
        _validate_pilot_binding(envelope["pilot_binding"], f"{label}.pilot_binding")
    if envelope["decision"] not in EVIDENCE_DECISIONS:
        raise EvolutionError(f"{label}.decision is unsupported")
    if envelope["safety"] not in EVIDENCE_SAFETY:
        raise EvolutionError(f"{label}.safety is unsupported")
    _timestamp(envelope["recorded_at"], f"{label}.recorded_at")


def _private_ref(
    project: Path,
    reference: Mapping[str, Any],
    *,
    proposal: Mapping[str, Any],
    expected_kind: str | None = None,
    expected_run_binding: Any = _UNSET,
    expected_pilot_binding: Any = _UNSET,
    allowed_decisions: set[str] | None = None,
    allowed_safety: set[str] | None = None,
    not_before: str | None = None,
    not_after: str | None = None,
) -> dict[str, Any]:
    _validate_evidence(reference, "evidence")
    if expected_kind is not None and reference["kind"] != expected_kind:
        raise EvolutionError("evidence kind does not match the required gate")
    relative = reference["ref"]
    path = project / Path(*relative.split("/"))
    try:
        _assert_private_containment(project, path)
    except FeedbackError as exc:
        raise EvolutionError("evidence escaped the private project boundary") from exc
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or _is_reparse(path) or metadata.st_nlink != 1:
            raise EvolutionError("evidence must be a single-link private file")
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise EvolutionError("required private evidence is unavailable") from exc
    except OSError as exc:
        raise EvolutionError("required private evidence could not be verified") from exc
    if len(raw) > MAX_RECORD_BYTES or _sha(raw) != reference["sha256"]:
        raise EvolutionError("private evidence hash or size does not match")
    envelope = _decode_json(raw, maximum=MAX_RECORD_BYTES, label="private evidence envelope")
    _validate_evidence_envelope(envelope, proposal, "private evidence envelope")
    if envelope["evidence_kind"] != reference["kind"]:
        raise EvolutionError("evidence reference kind differs from its envelope")
    if expected_run_binding is not _UNSET and envelope["run_binding"] != expected_run_binding:
        raise EvolutionError("evidence run binding is missing or stale")
    if expected_pilot_binding is not _UNSET and envelope["pilot_binding"] != expected_pilot_binding:
        raise EvolutionError("evidence pilot or lineage binding is missing or stale")
    if allowed_decisions is not None and envelope["decision"] not in allowed_decisions:
        raise EvolutionError("evidence decision does not satisfy the required gate")
    if allowed_safety is not None and envelope["safety"] not in allowed_safety:
        raise EvolutionError("evidence safety verdict does not satisfy the required gate")
    recorded = _timestamp(envelope["recorded_at"], "evidence recorded_at")
    if not_before is not None and recorded < _timestamp(not_before, "evidence not_before"):
        raise EvolutionError("evidence predates the lifecycle stage it claims to authorize")
    if not_after is not None and recorded > _timestamp(not_after, "evidence not_after"):
        raise EvolutionError("evidence was recorded after the bounded lifecycle decision")
    return envelope


def _source_ref_to_evidence(kind: str, value: Mapping[str, Any]) -> dict[str, str]:
    return {"kind": kind, "ref": value["ref"], "sha256": value["sha256"]}


def _validate_evidence(value: Any, label: str) -> None:
    ref = _exact(value, EVIDENCE_KEYS, label)
    if ref["kind"] not in EVIDENCE_KINDS:
        raise EvolutionError(f"{label}.kind is unsupported")
    path = _portable(ref["ref"], PORTABLE_REF, f"{label}.ref", MAX_REF)
    if not path.startswith(".opc/"):
        raise EvolutionError(f"{label}.ref must remain private under .opc")
    _hash(ref["sha256"], f"{label}.sha256")


def _validate_authorization(value: Any, label: str) -> None:
    auth = _exact(value, AUTH_KEYS, label)
    _validate_evidence(auth["manager_approval"], f"{label}.manager_approval")
    _validate_evidence(auth["independent_qa"], f"{label}.independent_qa")
    _validate_evidence(auth["shadow"], f"{label}.shadow")
    if (
        auth["manager_approval"]["kind"] != "manager_approval"
        or auth["independent_qa"]["kind"] != "independent_qa"
        or auth["shadow"]["kind"] != "shadow"
    ):
        raise EvolutionError(f"{label} evidence kinds do not satisfy the gates")
    _timestamp(auth["recorded_at"], f"{label}.recorded_at")


def _expected_run_binding(case_id: str, arm_name: str, arm: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "arm": arm_name,
        "run_id": arm["run_id"],
        "capability_version": dict(arm["capability_version"]),
        "knowledge_versions": [dict(item) for item in arm["knowledge_versions"]],
    }


def _pilot_binding(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    case_ids = sorted(case["case_id"] for case in cases)
    control_run_ids = sorted(case["control"]["run_id"] for case in cases)
    candidate_run_ids = sorted(case["candidate"]["run_id"] for case in cases)
    lineage_refs = []
    for case in cases:
        for arm_name in ("control", "candidate"):
            arm = case[arm_name]
            lineage_refs.append({
                "case_id": case["case_id"], "arm": arm_name, "run_id": arm["run_id"],
                "ref": arm["lineage"]["ref"], "sha256": arm["lineage"]["sha256"],
            })
    lineage_refs.sort(key=lambda item: (
        item["case_id"], item["arm"], item["run_id"], item["ref"], item["sha256"]
    ))
    binding = {
        "case_ids": case_ids,
        "control_run_ids": control_run_ids,
        "candidate_run_ids": candidate_run_ids,
        "lineage_refs": lineage_refs,
    }
    _validate_pilot_binding(binding, "pilot binding")
    return binding


def _verify_authorization(
    project: Path,
    proposal: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    pilot_binding: Mapping[str, Any] | None,
    now: str,
    not_before: str,
) -> None:
    _validate_authorization(authorization, "authorization")
    auth_time = authorization["recorded_at"]
    if _timestamp(auth_time, "authorization.recorded_at") > _timestamp(now, "authorization now"):
        raise EvolutionError("authorization is dated after the requested lifecycle action")
    if _timestamp(auth_time, "authorization.recorded_at") < _timestamp(not_before, "authorization not_before"):
        raise EvolutionError("authorization predates the evidence it claims to govern")
    requirements = (
        ("manager_approval", {"approved"}, {"safe", "not_applicable"}),
        ("independent_qa", {"pass"}, {"safe"}),
        ("shadow", {"beneficial"}, {"safe"}),
    )
    for key, decisions, safety in requirements:
        _private_ref(
            project, authorization[key], proposal=proposal, expected_kind=key,
            expected_run_binding=None, expected_pilot_binding=pilot_binding,
            allowed_decisions=decisions, allowed_safety=safety,
            not_before=not_before, not_after=auth_time,
        )


def _validate_metrics(value: Any, label: str) -> None:
    metrics = _exact(value, METRICS_KEYS, label)
    for key in RATIO_METRICS:
        ratio = _exact(metrics[key], {"numerator", "denominator"}, f"{label}.{key}")
        numerator, denominator = ratio["numerator"], ratio["denominator"]
        if any(isinstance(item, bool) or not isinstance(item, int) for item in (numerator, denominator)):
            raise EvolutionError(f"{label}.{key} components must be integers")
        if not 0 <= numerator <= MAX_METRIC or not 1 <= denominator <= MAX_METRIC:
            raise EvolutionError(f"{label}.{key} components exceed bounds")
    for key in SAFETY_METRICS:
        item = metrics[key]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= MAX_METRIC:
            raise EvolutionError(f"{label}.{key} exceeds bounds")
    context = metrics["context_tokens_per_task"]
    latency = metrics["latency_ms"]
    if isinstance(context, bool) or not isinstance(context, int) or not 1 <= context <= MAX_CONTEXT:
        raise EvolutionError(f"{label}.context_tokens_per_task exceeds bounds")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or not 0 < latency <= MAX_LATENCY:
        raise EvolutionError(f"{label}.latency_ms exceeds bounds")


def _validate_arm(value: Any, label: str) -> None:
    arm = _exact(value, ARM_KEYS, label)
    _portable(arm["run_id"], PORTABLE_RUN, f"{label}.run_id")
    if arm["execution_status"] not in EXECUTION_STATES:
        raise EvolutionError(f"{label}.execution_status is unsupported")
    contract = _exact(arm["evaluation_contract"], METRIC_CONTRACT_KEYS, f"{label}.evaluation_contract")
    if contract["version"] != METRIC_CONTRACT_VERSION:
        raise EvolutionError("control and candidate must use the v1 evaluation contract")
    if _hash(contract["sha256"], f"{label}.evaluation_contract.sha256") != METRIC_CONTRACT_SHA256:
        raise EvolutionError("pilot arm evaluation contract hash drifted")
    _validate_version(arm["capability_version"], f"{label}.capability_version")
    _validate_knowledge_versions(arm["knowledge_versions"], f"{label}.knowledge_versions")
    _validate_evidence(arm["lineage"], f"{label}.lineage")
    if arm["lineage"]["kind"] != "lineage":
        raise EvolutionError("pilot run requires a lineage evidence ref")
    if arm["execution_status"] == "completed":
        if arm["unavailable_reason"] is not None:
            raise EvolutionError("completed pilot arm cannot claim an unavailable reason")
        _validate_metrics(arm["measurements"], f"{label}.measurements")
    else:
        if arm["measurements"] is not None:
            raise EvolutionError("unavailable pilot arm must not contain measurements")
        if arm["unavailable_reason"] != arm["execution_status"] or arm["unavailable_reason"] not in UNAVAILABLE_REASONS:
            raise EvolutionError("unavailable pilot arm requires its exact bounded reason")


def validate_pilot_case(value: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
    case = _exact(value, CASE_KEYS, "pilot case")
    _portable(case["case_id"], PORTABLE_ID, "case_id")
    _validate_arm(case["control"], "control")
    _validate_arm(case["candidate"], "candidate")
    control = case["control"]
    candidate = case["candidate"]
    if control["run_id"] == candidate["run_id"]:
        raise EvolutionError("control and candidate require distinct portable runs")
    if control["evaluation_contract"] != candidate["evaluation_contract"]:
        raise EvolutionError("control and candidate must use the exact same evaluation contract")
    if control["capability_version"] != proposal["current_version"]:
        raise EvolutionError("control run did not use the exact current capability version")
    if candidate["capability_version"] != proposal["candidate_version"]:
        raise EvolutionError("candidate run did not use the exact candidate capability version")


def _validate_evaluation(value: Any, label: str) -> None:
    result = _exact(
        value,
        {"status", "conclusion", "case_count", "measured_case_count", "comparisons", "blocking_reasons",
         "confounders", "claim", "evaluated_at"},
        label,
    )
    if result["status"] not in {"conclusive", "inconclusive"}:
        raise EvolutionError(f"{label}.status is unsupported")
    if result["conclusion"] not in {"beneficial", "neutral", "harmful", "inconclusive"}:
        raise EvolutionError(f"{label}.conclusion is unsupported")
    if (result["status"] == "inconclusive") != (result["conclusion"] == "inconclusive"):
        raise EvolutionError(f"{label} status and conclusion contradict")
    count = result["case_count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_CASES:
        raise EvolutionError(f"{label}.case_count exceeds bounds")
    measured_count = result["measured_case_count"]
    if (
        isinstance(measured_count, bool) or not isinstance(measured_count, int)
        or not 0 <= measured_count <= count
    ):
        raise EvolutionError(f"{label}.measured_case_count exceeds bounds")
    comparisons = result["comparisons"]
    expected = set() if measured_count == 0 else METRICS_KEYS
    if not isinstance(comparisons, Mapping) or set(comparisons) != expected:
        raise EvolutionError(f"{label}.comparisons do not match the metric contract")
    if any(direction not in {"improved", "equal", "regressed"} for direction in comparisons.values()):
        raise EvolutionError(f"{label}.comparisons contains an unsupported direction")
    reasons = result["blocking_reasons"]
    confounders = result["confounders"]
    for values, minimum, item_label in ((reasons, 0, "blocking_reasons"), (confounders, 1, "confounders")):
        if not isinstance(values, list) or not minimum <= len(values) <= MAX_REFS:
            raise EvolutionError(f"{label}.{item_label} exceeds bounds")
        normalized = [_portable(item, PORTABLE_ID, f"{label}.{item_label}") for item in values]
        if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
            raise EvolutionError(f"{label}.{item_label} must be unique and sorted")
    if result["status"] == "inconclusive" and not reasons:
        raise EvolutionError(f"{label} inconclusive result requires a blocking reason")
    if result["status"] == "conclusive" and reasons:
        raise EvolutionError(f"{label} conclusive result cannot retain blocking reasons")
    if result["claim"] != REPORT_CLAIM:
        raise EvolutionError(f"{label} exceeds the association-only claim boundary")
    _timestamp(result["evaluated_at"], f"{label}.evaluated_at")


def _history(revision: int, state: str, action: str, timestamp: str,
             refs: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {
        "revision": revision, "state": state, "action": action,
        "recorded_at": timestamp, "evidence_refs": [dict(item) for item in refs],
    }


def validate_record(record: Mapping[str, Any]) -> None:
    keys = {
        "schema_version", "contract_version", "contract_sha256", "proposal",
        "proposal_sha256", "revision", "state", "created_at", "updated_at",
        "pilot_authorization", "pilot_cases", "evaluation",
        "evaluation_evidence", "promotion_authorization", "pending_transition",
        "active_version", "history",
    }
    obj = _exact(record, keys, "evolution record")
    if obj["schema_version"] != RECORD_VERSION or obj["contract_version"] != CONTRACT_VERSION:
        raise EvolutionError("evolution record version is unsupported")
    _, contract_hash = _load_contract()
    if obj["contract_sha256"] != contract_hash:
        raise EvolutionError("evolution record contract hash is stale")
    validate_proposal(obj["proposal"])
    if obj["proposal_sha256"] != _sha(_json_bytes(obj["proposal"], maximum=MAX_PROPOSAL_BYTES)):
        raise EvolutionError("evolution record proposal hash does not match")
    revision = obj["revision"]
    history = obj["history"]
    if (
        isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        or not isinstance(history, list) or len(history) != revision or len(history) > MAX_HISTORY
        or obj["state"] not in STATES
    ):
        raise EvolutionError("evolution revision, history, or state is invalid")
    created = _timestamp(obj["created_at"], "created_at")
    updated = _timestamp(obj["updated_at"], "updated_at")
    if updated < created:
        raise EvolutionError("evolution updated_at precedes created_at")
    previous_state: str | None = None
    previous_time = created
    seen_unique: set[str] = set()
    for index, entry in enumerate(history, start=1):
        _exact(entry, {"revision", "state", "action", "recorded_at", "evidence_refs"}, "history entry")
        if entry["revision"] != index or entry["state"] not in STATES:
            raise EvolutionError("evolution history is not append-only")
        if entry["action"] not in HISTORY_ACTIONS:
            raise EvolutionError("evolution history action is unsupported")
        recorded = _timestamp(entry["recorded_at"], "history timestamp")
        if recorded < previous_time or recorded < created or recorded > updated:
            raise EvolutionError("evolution history timestamps are not monotonic and bounded")
        transition = HISTORY_TRANSITIONS[entry["action"]]
        allowed_from, expected_to = transition
        if index == 1:
            if entry["action"] != "opened" or previous_state is not None:
                raise EvolutionError("evolution history must start with opened")
        else:
            if allowed_from is None:
                raise EvolutionError("opened can only be the first history action")
            allowed = allowed_from if isinstance(allowed_from, set) else {allowed_from}
            if previous_state not in allowed:
                raise EvolutionError("evolution history contains an illegal state transition")
        if entry["state"] != expected_to:
            raise EvolutionError("evolution history action and destination state disagree")
        if entry["action"] in UNIQUE_HISTORY_ACTIONS:
            if entry["action"] in seen_unique:
                raise EvolutionError("evolution history repeats a single-use action")
            seen_unique.add(entry["action"])
        if not isinstance(entry["evidence_refs"], list) or len(entry["evidence_refs"]) > MAX_REFS:
            raise EvolutionError("history evidence refs exceed bounds")
        for ref in entry["evidence_refs"]:
            _validate_evidence(ref, "history evidence")
        expected_kinds = HISTORY_EVIDENCE_KINDS[entry["action"]]
        actual_kinds = tuple(sorted(ref["kind"] for ref in entry["evidence_refs"]))
        if expected_kinds is not None and actual_kinds != expected_kinds:
            raise EvolutionError("history evidence kinds do not match the action")
        if entry["action"] == "observation_started" and (
            len(actual_kinds) != 1 or actual_kinds[0] not in {"evaluation", "outcome", "lineage"}
        ):
            raise EvolutionError("observation history evidence kind is invalid")
        if entry["action"] == "rollback_applied" and (
            len(actual_kinds) != 1 or actual_kinds[0] not in {"rollback_decision", "evaluation", "outcome"}
        ):
            raise EvolutionError("rollback history evidence kind is invalid")
        if entry["action"] == "rejected" and len(actual_kinds) != 1:
            raise EvolutionError("rejection history requires exactly one evidence ref")
        previous_state = entry["state"]
        previous_time = recorded
    if history[-1]["state"] != obj["state"] or history[-1]["recorded_at"] != obj["updated_at"]:
        raise EvolutionError("record state must match the last history entry")
    if history[0]["recorded_at"] != obj["created_at"]:
        raise EvolutionError("opened history timestamp must equal record created_at")
    if obj["pilot_authorization"] is not None:
        _validate_authorization(obj["pilot_authorization"], "pilot_authorization")
    if not isinstance(obj["pilot_cases"], list) or len(obj["pilot_cases"]) > obj["proposal"]["pilot"]["max_cases"]:
        raise EvolutionError("pilot cases exceed the approved bound")
    case_ids: set[str] = set()
    for case in obj["pilot_cases"]:
        validate_pilot_case(case, obj["proposal"])
        if case["case_id"] in case_ids:
            raise EvolutionError("pilot case ids must be unique")
        case_ids.add(case["case_id"])
    if obj["evaluation"] is not None:
        _validate_evaluation(obj["evaluation"], "evaluation")
        if obj["evaluation"]["case_count"] != len(obj["pilot_cases"]):
            raise EvolutionError("evaluation case count differs from the immutable pilot cases")
        measured = sum(
            1 for case in obj["pilot_cases"]
            if case["control"]["execution_status"] == "completed"
            and case["candidate"]["execution_status"] == "completed"
        )
        if obj["evaluation"]["measured_case_count"] != measured:
            raise EvolutionError("evaluation measured case count differs from available pilot arms")
    if obj["evaluation_evidence"] is not None:
        _validate_evidence(obj["evaluation_evidence"], "evaluation_evidence")
        if obj["evaluation_evidence"]["kind"] != "evaluation":
            raise EvolutionError("evaluation_evidence must have evaluation kind")
    if obj["promotion_authorization"] is not None:
        _validate_authorization(obj["promotion_authorization"], "promotion_authorization")
    _validate_version(obj["active_version"], "active_version")
    if obj["pending_transition"] is not None:
        transition = _exact(
            obj["pending_transition"],
            {"kind", "base_head", "source_commit", "target_path", "before_sha256", "after_sha256", "diff_sha256", "plan_token"},
            "pending_transition",
        )
        if transition["kind"] not in {"promotion", "rollback"}:
            raise EvolutionError("pending transition kind is unsupported")
        _commit(transition["base_head"], "pending base_head")
        _commit(transition["source_commit"], "pending source_commit")
        _portable(transition["target_path"], PORTABLE_REF, "pending target_path", MAX_REF)
        for key in ("before_sha256", "after_sha256", "diff_sha256", "plan_token"):
            _hash(transition[key], f"pending_transition.{key}")
    state = obj["state"]
    proposal = obj["proposal"]
    if state == "candidate" and any(
        value is not None and value != []
        for value in (obj["pilot_authorization"], obj["pilot_cases"], obj["evaluation"], obj["evaluation_evidence"], obj["promotion_authorization"], obj["pending_transition"])
    ):
        raise EvolutionError("candidate state contains evidence from a later lifecycle stage")
    if state in {"pilot_approved", "piloting", "evaluated", "promotion_pending", "promoted", "observing", "rollback_pending", "rolled_back"} and obj["pilot_authorization"] is None:
        raise EvolutionError("post-candidate state requires pilot authorization")
    if state == "pilot_approved" and obj["pilot_cases"]:
        raise EvolutionError("pilot_approved state cannot contain completed pilot cases")
    if state in {"piloting", "evaluated", "promotion_pending", "promoted", "observing", "rollback_pending", "rolled_back"} and not obj["pilot_cases"]:
        raise EvolutionError("post-pilot state requires bounded pilot cases")
    if state in {"evaluated", "promotion_pending", "promoted", "observing", "rollback_pending", "rolled_back"} and obj["evaluation"] is None:
        raise EvolutionError("post-pilot state requires an evaluation")
    if state in {"evaluated", "promotion_pending", "promoted", "observing", "rollback_pending", "rolled_back"} and obj["evaluation_evidence"] is None:
        raise EvolutionError("post-pilot state requires bound evaluation evidence")
    if state in {"promotion_pending", "promoted", "observing", "rollback_pending", "rolled_back"} and obj["promotion_authorization"] is None:
        raise EvolutionError("promotion lifecycle requires explicit authorization")
    if state == "promotion_pending" and (obj["pending_transition"] or {}).get("kind") != "promotion":
        raise EvolutionError("promotion_pending requires an exact promotion transition")
    if state == "rollback_pending" and (obj["pending_transition"] or {}).get("kind") != "rollback":
        raise EvolutionError("rollback_pending requires an exact rollback transition")
    if state not in {"promotion_pending", "rollback_pending"} and obj["pending_transition"] is not None:
        raise EvolutionError("only pending states may retain a transition plan")
    if state in {"candidate", "pilot_approved", "piloting", "evaluated", "promotion_pending"} and obj["active_version"] != proposal["current_version"]:
        raise EvolutionError("candidate lifecycle changed active behavior before confirmation")
    if state in {"promoted", "observing", "rollback_pending"}:
        candidate = proposal["candidate_version"]
        active = obj["active_version"]
        if any(active[key] != candidate[key] for key in ("version", "source_path", "content_sha256")):
            raise EvolutionError("promoted lifecycle active version differs from candidate")
    if state == "rolled_back":
        rollback = proposal["rollback_target"]
        active = obj["active_version"]
        if any(active[key] != rollback[key] for key in ("version", "source_path", "content_sha256")):
            raise EvolutionError("rolled_back lifecycle active version differs from rollback target")
    _json_bytes(obj, maximum=MAX_RECORD_BYTES)


def _record_path(project: Path, proposal_id: str) -> Path:
    return project / ".opc" / "evolution" / f"{proposal_id}.json"


def _read_record(project: Path, proposal_id: str) -> tuple[dict[str, Any] | None, str | None]:
    path = _record_path(project, proposal_id)
    if not path.exists():
        return None, None
    record, raw = _read_json_file(path, maximum=MAX_RECORD_BYTES, label="evolution record")
    validate_record(record)
    return record, _sha(raw)


def _verify_source_refs(project: Path, proposal: Mapping[str, Any]) -> None:
    for field, kind in (
        ("candidate_refs", "candidate"), ("feedback_refs", "outcome"),
        ("evaluation_refs", "evaluation"), ("lineage_refs", "lineage"),
    ):
        decisions, safety = SOURCE_EVIDENCE_POLICY[kind]
        for ref in proposal["sources"][field]:
            evidence = _source_ref_to_evidence(kind, ref)
            _private_ref(
                project, evidence, proposal=proposal, expected_kind=kind,
                allowed_decisions=set(decisions), allowed_safety=set(safety),
                not_after=proposal["created_at"],
            )


def preview_open(project_root: Path, repository_root: Path,
                 proposal: Mapping[str, Any]) -> dict[str, Any]:
    validate_proposal(proposal)
    project, directory = _project_context(project_root, proposal["project_id"])
    _assert_private_or_ignored(project, directory)
    root = _git_root(repository_root)
    _verify_proposal_git(root, proposal, require_head=True)
    _verify_source_refs(project, proposal)
    existing, existing_hash = _read_record(project, proposal["proposal_id"])
    timestamp = proposal["created_at"]
    _, contract_hash = _load_contract()
    proposal_hash = _sha(_json_bytes(proposal, maximum=MAX_PROPOSAL_BYTES))
    if existing is not None:
        if existing["proposal_sha256"] != proposal_hash:
            raise EvolutionError("proposal_id already belongs to different content")
        record = existing
        idempotent = True
    else:
        record = {
            "schema_version": RECORD_VERSION,
            "contract_version": CONTRACT_VERSION,
            "contract_sha256": contract_hash,
            "proposal": dict(proposal),
            "proposal_sha256": proposal_hash,
            "revision": 1,
            "state": "candidate",
            "created_at": timestamp,
            "updated_at": timestamp,
            "pilot_authorization": None,
            "pilot_cases": [],
            "evaluation": None,
            "evaluation_evidence": None,
            "promotion_authorization": None,
            "pending_transition": None,
            "active_version": dict(proposal["current_version"]),
            "history": [_history(1, "candidate", "opened", timestamp)],
        }
        validate_record(record)
        idempotent = False
    core = {
        "operation": "open", "proposal_id": proposal["proposal_id"],
        "proposal_sha256": proposal_hash, "repository_head": _git_head(root),
        "base_record": {"exists": existing is not None, "sha256": existing_hash},
        "idempotent": idempotent, "record_sha256": _sha(_json_bytes(record, maximum=MAX_RECORD_BYTES)),
    }
    return {**core, "plan_token": _sha(_json_bytes(core, maximum=MAX_RECORD_BYTES)), "record": record}


def _atomic_private(bound: _BoundDirectory, name: str, record: Mapping[str, Any]) -> None:
    nonce = secrets.token_hex(24)
    pending = f"{name}.pending-{nonce}"
    backup = f"{name}.backup-{nonce}"
    pending_identity = None
    backup_identity = None
    published = False
    original = bound.child_identity(name) is not None
    descriptor: int | None = None
    try:
        bound.verify_current()
        if original:
            backup_identity = bound.link(name, backup)
        descriptor = bound.open_exclusive(pending, binary=True)
        pending_identity = _file_identity(os.fstat(descriptor))
        raw = (json.dumps(dict(record), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise EvolutionError("evolution record exceeds the configured size limit")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            pending_identity = _file_identity(os.fstat(handle.fileno()))
        bound.verify_current()
        bound.replace(pending, name, expected_source=pending_identity)
        published = True
        bound.verify_current()
        if backup_identity is not None and not bound.unlink_owned(backup, backup_identity):
            raise EvolutionError("evolution backup identity changed")
        backup_identity = None
    except BaseException:
        if published:
            if original and backup_identity is not None:
                bound.replace(backup, name, expected_source=backup_identity, require_current=False)
                backup_identity = None
            else:
                bound.unlink_owned(name, pending_identity)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        bound.unlink_owned(pending, pending_identity)
        bound.unlink_owned(backup, backup_identity)


def _write_previewed(binding: _EvolutionBinding, preview: Mapping[str, Any], plan_token: str) -> dict[str, Any]:
    _hash(plan_token, "plan_token")
    if preview["plan_token"] != plan_token:
        raise EvolutionError("plan token does not match the exact preview")
    project = binding.project
    directory = binding.directory
    binding.verify()
    _assert_private_or_ignored(project, directory)
    try:
        bound = binding.ensure_directory()
        path = _record_path(project, preview["proposal_id"])
        with _exclusive_update_lock(bound, path.name):
            binding.verify()
            _assert_private_or_ignored(project, directory)
            current, current_hash = _read_record(project, preview["proposal_id"])
            base = {"exists": current is not None, "sha256": current_hash}
            if base != preview["base_record"]:
                raise EvolutionError("evolution base record changed after preview")
            if preview["idempotent"]:
                return {"idempotent": True, "record": current}
            _atomic_private(bound, path.name, preview["record"])
            binding.verify()
            return {"idempotent": False, "record": preview["record"]}
    except FeedbackError as exc:
        raise EvolutionError("private evolution filesystem identity changed") from exc


def open_proposal(project_root: Path, repository_root: Path,
                  proposal: Mapping[str, Any], *, plan_token: str) -> dict[str, Any]:
    with _EvolutionBinding(project_root) as binding:
        preview = preview_open(binding.project, repository_root, proposal)
        binding.verify()
        return _write_previewed(binding, preview, plan_token)


def _verify_pilot_lineage(project: Path, proposal: Mapping[str, Any],
                          cases: Sequence[Mapping[str, Any]], *, now: str) -> None:
    for case in cases:
        for arm_name in ("control", "candidate"):
            arm = case[arm_name]
            _private_ref(
                project, arm["lineage"], proposal=proposal, expected_kind="lineage",
                expected_run_binding=_expected_run_binding(case["case_id"], arm_name, arm),
                expected_pilot_binding=None, allowed_decisions={"verified"},
                allowed_safety={"not_applicable"}, not_after=now,
            )


def _verify_evaluation_evidence(project: Path, proposal: Mapping[str, Any],
                                evidence: Mapping[str, Any], evaluation: Mapping[str, Any],
                                cases: Sequence[Mapping[str, Any]], *, now: str,
                                not_before: str) -> None:
    safety = {"safe"} if evaluation["conclusion"] == "beneficial" else {"safe", "unsafe", "inconclusive"}
    _private_ref(
        project, evidence, proposal=proposal, expected_kind="evaluation",
        expected_run_binding=None, expected_pilot_binding=_pilot_binding(cases),
        allowed_decisions={evaluation["conclusion"]}, allowed_safety=safety,
        not_before=not_before, not_after=now,
    )


def _revalidate_cumulative_evidence(project: Path, record: Mapping[str, Any], *, now: str,
                                    include_promotion: bool) -> None:
    proposal = record["proposal"]
    _verify_source_refs(project, proposal)
    if record["pilot_authorization"] is None:
        raise EvolutionError("pilot authorization evidence is missing")
    _verify_authorization(
        project, proposal, record["pilot_authorization"], pilot_binding=None,
        now=now, not_before=proposal["created_at"],
    )
    if not record["pilot_cases"]:
        raise EvolutionError("pilot lineage evidence is missing")
    _verify_pilot_lineage(project, proposal, record["pilot_cases"], now=now)
    if record["evaluation"] is None or record["evaluation_evidence"] is None:
        raise EvolutionError("bound evaluation evidence is missing")
    pilot_recorded_at = max(
        entry["recorded_at"] for entry in record["history"]
        if entry["action"] == "pilot_case_recorded"
    )
    _verify_evaluation_evidence(
        project, proposal, record["evaluation_evidence"], record["evaluation"],
        record["pilot_cases"], now=now, not_before=pilot_recorded_at,
    )
    if include_promotion:
        if record["promotion_authorization"] is None:
            raise EvolutionError("promotion authorization evidence is missing")
        _verify_authorization(
            project, proposal, record["promotion_authorization"],
            pilot_binding=_pilot_binding(record["pilot_cases"]), now=now,
            not_before=record["evaluation"]["evaluated_at"],
        )


def _preview_update(project_root: Path, proposal_id: str, *, expected_revision: int,
                    action: str, payload: Mapping[str, Any], now: str) -> dict[str, Any]:
    _portable(proposal_id, PORTABLE_PROPOSAL, "proposal_id")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise EvolutionError("expected_revision must be a positive integer")
    _timestamp(now, "now")
    # Resolve project_id from the private record without trusting the proposal input.
    project = project_root.expanduser().resolve(strict=True)
    directory = project / ".opc" / "evolution"
    _assert_private_or_ignored(project, directory)
    record, base_hash = _read_record(project, proposal_id)
    if record is None:
        raise EvolutionError("evolution record is unavailable")
    _project_context(project, record["proposal"]["project_id"])
    if record["revision"] != expected_revision:
        raise EvolutionError("stale evolution revision")
    if _timestamp(now, "now") < _timestamp(record["updated_at"], "record.updated_at"):
        raise EvolutionError("lifecycle action timestamp cannot move backward")
    updated = json.loads(json.dumps(record))
    refs: list[Mapping[str, Any]] = []
    state = record["state"]
    history_action = action

    if action in {"authorize_pilot", "authorize_promotion"}:
        auth = payload.get("authorization")
        _validate_authorization(auth, "authorization")
        refs = [auth["manager_approval"], auth["independent_qa"], auth["shadow"]]
        if action == "authorize_pilot":
            if state != "candidate":
                raise EvolutionError("pilot authorization requires candidate state")
            _verify_source_refs(project, record["proposal"])
            _verify_authorization(
                project, record["proposal"], auth, pilot_binding=None, now=now,
                not_before=record["proposal"]["created_at"],
            )
            updated["pilot_authorization"] = dict(auth)
            new_state, history_action = "pilot_approved", "pilot_authorized"
        else:
            if state != "evaluated" or not record["evaluation"] or record["evaluation"]["conclusion"] != "beneficial":
                raise EvolutionError("promotion requires a beneficial conclusive pilot")
            _revalidate_cumulative_evidence(project, record, now=now, include_promotion=False)
            _verify_authorization(
                project, record["proposal"], auth,
                pilot_binding=_pilot_binding(record["pilot_cases"]), now=now,
                not_before=record["evaluation"]["evaluated_at"],
            )
            updated["promotion_authorization"] = dict(auth)
            new_state, history_action = "evaluated", "promotion_authorized"
    elif action == "record_pilot_case":
        if state not in {"pilot_approved", "piloting"} or record["pilot_authorization"] is None:
            raise EvolutionError("pilot case requires explicit pilot authorization")
        case = payload.get("case")
        validate_pilot_case(case, record["proposal"])
        _verify_source_refs(project, record["proposal"])
        _verify_authorization(
            project, record["proposal"], record["pilot_authorization"],
            pilot_binding=None, now=now, not_before=record["proposal"]["created_at"],
        )
        _verify_pilot_lineage(project, record["proposal"], record["pilot_cases"], now=now)
        _verify_pilot_lineage(project, record["proposal"], [case], now=now)
        for existing in record["pilot_cases"]:
            if existing["case_id"] == case["case_id"]:
                if existing != case:
                    raise EvolutionError("pilot case id already exists with different content")
                core = {
                    "operation": action, "proposal_id": proposal_id,
                    "expected_revision": expected_revision,
                    "base_record": {"exists": True, "sha256": base_hash},
                    "idempotent": True,
                    "record_sha256": _sha(_json_bytes(record, maximum=MAX_RECORD_BYTES)),
                }
                return {**core, "plan_token": _sha(_json_bytes(core, maximum=MAX_RECORD_BYTES)), "record": record}
        if len(record["pilot_cases"]) >= record["proposal"]["pilot"]["max_cases"]:
            raise EvolutionError("bounded pilot reached its approved maximum")
        updated["pilot_cases"].append(dict(case))
        refs = [case["control"]["lineage"], case["candidate"]["lineage"]]
        new_state, history_action = "piloting", "pilot_case_recorded"
    elif action == "evaluate":
        if state != "piloting":
            raise EvolutionError("evaluation requires recorded pilot cases")
        _verify_source_refs(project, record["proposal"])
        _verify_authorization(
            project, record["proposal"], record["pilot_authorization"],
            pilot_binding=None, now=now, not_before=record["proposal"]["created_at"],
        )
        _verify_pilot_lineage(project, record["proposal"], record["pilot_cases"], now=now)
        confounders = payload.get("confounders")
        evaluation = evaluate_cases(record["pilot_cases"], record["proposal"], confounders, now=now)
        evidence = payload.get("evidence")
        _validate_evidence(evidence, "evaluation evidence")
        if evidence["kind"] != "evaluation":
            raise EvolutionError("evaluation action requires evaluation evidence")
        pilot_recorded_at = max(
            entry["recorded_at"] for entry in record["history"]
            if entry["action"] == "pilot_case_recorded"
        )
        _verify_evaluation_evidence(
            project, record["proposal"], evidence, evaluation, record["pilot_cases"],
            now=now, not_before=pilot_recorded_at,
        )
        updated["evaluation"] = evaluation
        updated["evaluation_evidence"] = dict(evidence)
        refs = [evidence]
        new_state, history_action = "evaluated", "evaluated"
    elif action == "observe":
        if state not in {"promoted", "observing"}:
            raise EvolutionError("observation requires a confirmed promotion")
        _revalidate_cumulative_evidence(project, record, now=now, include_promotion=True)
        evidence = payload.get("evidence")
        _validate_evidence(evidence, "observation evidence")
        if evidence["kind"] not in {"evaluation", "outcome", "lineage"}:
            raise EvolutionError("observation evidence kind is unsupported")
        allowed = SOURCE_EVIDENCE_POLICY[evidence["kind"]]
        _private_ref(
            project, evidence, proposal=record["proposal"],
            allowed_decisions=set(allowed[0]), allowed_safety=set(allowed[1]),
            not_after=now,
        )
        refs = [evidence]
        new_state, history_action = "observing", "observation_started"
    elif action == "reject":
        evidence = payload.get("evidence")
        _validate_evidence(evidence, "rejection evidence")
        _private_ref(
            project, evidence, proposal=record["proposal"],
            allowed_decisions={"denied", "fail", "harmful", "inconclusive", "rollback_denied"},
            allowed_safety={"unsafe", "inconclusive", "not_applicable"}, not_after=now,
        )
        refs = [evidence]
        new_state, history_action = "rejected", "rejected"
    else:
        raise EvolutionError("unsupported lifecycle action")

    updated["revision"] = expected_revision + 1
    updated["state"] = new_state
    updated["updated_at"] = now
    updated["history"].append(_history(updated["revision"], new_state, history_action, now, refs))
    validate_record(updated)
    core = {
        "operation": action, "proposal_id": proposal_id,
        "expected_revision": expected_revision,
        "base_record": {"exists": True, "sha256": base_hash},
        "idempotent": False,
        "record_sha256": _sha(_json_bytes(updated, maximum=MAX_RECORD_BYTES)),
    }
    return {**core, "plan_token": _sha(_json_bytes(core, maximum=MAX_RECORD_BYTES)), "record": updated}


def preview_action(project_root: Path, proposal_id: str, *, expected_revision: int,
                   action: str, payload: Mapping[str, Any], now: str) -> dict[str, Any]:
    return _preview_update(project_root, proposal_id, expected_revision=expected_revision,
                           action=action, payload=payload, now=now)


def apply_action(project_root: Path, proposal_id: str, *, expected_revision: int,
                 action: str, payload: Mapping[str, Any], now: str,
                 plan_token: str) -> dict[str, Any]:
    with _EvolutionBinding(project_root) as binding:
        preview = _preview_update(
            binding.project, proposal_id, expected_revision=expected_revision,
            action=action, payload=payload, now=now,
        )
        binding.verify()
        return _write_previewed(binding, preview, plan_token)


def _aggregate(cases: Sequence[Mapping[str, Any]], arm: str, metric: str) -> Fraction:
    if metric in RATIO_METRICS:
        numerator = sum(case[arm]["measurements"][metric]["numerator"] for case in cases)
        denominator = sum(case[arm]["measurements"][metric]["denominator"] for case in cases)
        if denominator <= 0 or numerator > MAX_METRIC * MAX_CASES or denominator > MAX_METRIC * MAX_CASES:
            raise EvolutionError("pilot ratio aggregate exceeds bounds")
        return Fraction(numerator, denominator)
    total = sum(Fraction(str(case[arm]["measurements"][metric])) for case in cases)
    return total / len(cases)


def evaluate_cases(cases: Sequence[Mapping[str, Any]], proposal: Mapping[str, Any],
                   confounders: Any, *, now: str) -> dict[str, Any]:
    validate_proposal(proposal)
    _timestamp(now, "evaluated_at")
    if not isinstance(cases, Sequence) or len(cases) > proposal["pilot"]["max_cases"]:
        raise EvolutionError("pilot cases exceed the approved bound")
    if not isinstance(confounders, list) or not 1 <= len(confounders) <= MAX_REFS:
        raise EvolutionError("known confounders must contain 1..20 portable ids")
    normalized = [_portable(item, PORTABLE_ID, "confounder") for item in confounders]
    if len(set(normalized)) != len(normalized):
        raise EvolutionError("known confounders must be unique")
    for case in cases:
        validate_pilot_case(case, proposal)
    reasons: list[str] = []
    if len(cases) < proposal["pilot"]["min_cases"]:
        reasons.append("missing_evidence")
    for case in cases:
        for arm in ("control", "candidate"):
            status = case[arm]["execution_status"]
            if status != "completed":
                reasons.append(status)
    measured_cases = [
        case for case in cases
        if case["control"]["execution_status"] == "completed"
        and case["candidate"]["execution_status"] == "completed"
    ]
    comparisons: dict[str, str] = {}
    improved = False
    regressed = False
    if measured_cases:
        for metric in sorted(METRICS_KEYS):
            control = _aggregate(measured_cases, "control", metric)
            candidate = _aggregate(measured_cases, "candidate", metric)
            if candidate == control:
                direction = "equal"
            elif metric in HIGHER_IS_BETTER:
                direction = "improved" if candidate > control else "regressed"
            else:
                direction = "improved" if candidate < control else "regressed"
            comparisons[metric] = direction
            improved = improved or direction == "improved"
            regressed = regressed or direction == "regressed"
        for metric in SAFETY_METRICS:
            if _aggregate(measured_cases, "candidate", metric) > 0:
                reasons.append({
                    "scope_leakage_acceptances": "scope_leakage",
                    "stale_obsolete_acceptances": "regression",
                    "privacy_failures": "privacy_failure",
                }[metric])
    reasons = sorted(set(reasons))
    if reasons:
        status, conclusion = "inconclusive", "inconclusive"
    elif regressed:
        status, conclusion = "conclusive", "harmful"
    elif improved:
        status, conclusion = "conclusive", "beneficial"
    else:
        status, conclusion = "conclusive", "neutral"
    return {
        "status": status, "conclusion": conclusion, "case_count": len(cases),
        "measured_case_count": len(measured_cases),
        "comparisons": comparisons, "blocking_reasons": reasons,
        "confounders": sorted(normalized), "claim": REPORT_CLAIM,
        "evaluated_at": now,
    }


def _unified_diff(path: str, before: bytes, after: bytes) -> str:
    try:
        old = before.decode("utf-8").splitlines(keepends=True)
        new = after.decode("utf-8").splitlines(keepends=True)
    except UnicodeError as exc:
        raise EvolutionError("managed capability must be UTF-8 text") from exc
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}"))


def preview_transition(project_root: Path, repository_root: Path, proposal_id: str,
                       *, expected_revision: int, kind: str, now: str,
                       authorization: Mapping[str, Any] | None = None,
                       rollback_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _timestamp(now, "now")
    project = project_root.expanduser().resolve(strict=True)
    directory = project / ".opc" / "evolution"
    _assert_private_or_ignored(project, directory)
    record, base_hash = _read_record(project, proposal_id)
    if record is None or record["revision"] != expected_revision:
        raise EvolutionError("evolution record is unavailable or stale")
    if _timestamp(now, "now") < _timestamp(record["updated_at"], "record.updated_at"):
        raise EvolutionError("transition timestamp cannot move backward")
    root = _git_root(repository_root)
    _assert_clean(root)
    proposal = record["proposal"]
    target = proposal["capability"]["target_path"]
    if kind == "promotion":
        if record["state"] != "evaluated" or not record["evaluation"] or record["evaluation"]["conclusion"] != "beneficial":
            raise EvolutionError("promotion requires beneficial pilot evidence")
        _revalidate_cumulative_evidence(project, record, now=now, include_promotion=False)
        if authorization is None and record["promotion_authorization"] is None:
            raise EvolutionError("promotion requires explicit authorization")
        chosen_authorization = authorization or record["promotion_authorization"]
        if record["promotion_authorization"] is not None and chosen_authorization != record["promotion_authorization"]:
            raise EvolutionError("promotion authorization differs from the recorded exact evidence")
        _verify_authorization(
            project, proposal, chosen_authorization,
            pilot_binding=_pilot_binding(record["pilot_cases"]), now=now,
            not_before=record["evaluation"]["evaluated_at"],
        )
        refs = [
            chosen_authorization["manager_approval"],
            chosen_authorization["independent_qa"],
            chosen_authorization["shadow"],
        ]
        before_version, after_version = record["active_version"], proposal["candidate_version"]
        new_state, action = "promotion_pending", "promotion_applied"
    elif kind == "rollback":
        if record["state"] not in {"promoted", "observing"}:
            raise EvolutionError("rollback requires a confirmed active candidate")
        if rollback_evidence is None:
            raise EvolutionError("rollback requires explicit regression or manager evidence")
        _validate_evidence(rollback_evidence, "rollback evidence")
        if rollback_evidence["kind"] not in {"rollback_decision", "evaluation", "outcome"}:
            raise EvolutionError("rollback evidence kind is unsupported")
        _revalidate_cumulative_evidence(project, record, now=now, include_promotion=True)
        rollback_decisions = {
            "rollback_decision": {"rollback_approved", "regression_detected"},
            "evaluation": {"harmful", "inconclusive"},
            "outcome": {"observed", "regression_detected"},
        }[rollback_evidence["kind"]]
        _private_ref(
            project, rollback_evidence, proposal=proposal,
            expected_kind=rollback_evidence["kind"],
            allowed_decisions=rollback_decisions,
            allowed_safety={"unsafe", "inconclusive", "not_applicable"},
            not_after=now,
        )
        refs = [rollback_evidence]
        before_version, after_version = record["active_version"], proposal["rollback_target"]
        new_state, action = "rollback_pending", "rollback_applied"
    else:
        raise EvolutionError("transition kind is unsupported")
    _verify_proposal_git(root, proposal, require_head=False)
    head = _git_head(root)
    if head != before_version["source_commit"]:
        raise EvolutionError("repository HEAD drifted from the active version")
    before_blob = _git_blob(root, before_version["source_commit"], target)
    after = _git_blob(root, after_version["source_commit"], target)
    if _sha(before_blob) != before_version["content_sha256"] or _sha(after) != after_version["content_sha256"]:
        raise EvolutionError("transition Git blob hash does not match")
    _privacy_scan(after)
    path = _target_path(root, target)
    # A clean worktree is Git-equivalent to HEAD even when checkout filters
    # (for example core.autocrlf) make its physical bytes differ from the blob.
    # Bind the physical bytes too so apply/restore remains exact.
    before = path.read_bytes()
    diff = _unified_diff(target, before, after)
    diff_hash = _sha(diff.encode("utf-8"))
    transition_core = {
        "kind": kind, "base_head": head, "source_commit": after_version["source_commit"],
        "target_path": target,
        "before_sha256": before_version["content_sha256"], "after_sha256": _sha(after),
        "diff_sha256": diff_hash,
    }
    transition = {
        **transition_core,
        "plan_token": _sha(_json_bytes({
            **transition_core, "proposal_sha256": record["proposal_sha256"],
            "record_sha256": base_hash, "expected_revision": expected_revision,
        }, maximum=MAX_RECORD_BYTES)),
    }
    updated = json.loads(json.dumps(record))
    if kind == "promotion":
        updated["promotion_authorization"] = dict(chosen_authorization)
    updated["pending_transition"] = transition
    updated["revision"] = expected_revision + 1
    updated["state"] = new_state
    updated["updated_at"] = now
    updated["history"].append(_history(updated["revision"], new_state, action, now, refs))
    validate_record(updated)
    core = {
        "operation": kind, "proposal_id": proposal_id,
        "expected_revision": expected_revision,
        "base_record": {"exists": True, "sha256": base_hash},
        "repository_head": head, "transition": transition,
        "diff": diff, "git_stage_pathspecs": [target],
        "record_sha256": _sha(_json_bytes(updated, maximum=MAX_RECORD_BYTES)),
    }
    return {**core, "plan_token": transition["plan_token"], "record": updated,
            "before_bytes": before, "after_bytes": after}


class _ManagedDirectory:
    """Bind an existing repository directory object for one-file replacement."""

    def __init__(self, path: Path, root: Path):
        self.path = path
        self.root = root
        self.fd: int | None = None
        self.windows_handle: int | None = None
        self.token: tuple[int, int, int, int] | None = None

    def __enter__(self) -> "_ManagedDirectory":
        metadata = self.path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or self.path.is_symlink() or _is_reparse(self.path):
            raise EvolutionError("managed parent is not a stable directory")
        self.token = _directory_identity(metadata)
        if os.name == "nt":
            self._open_windows_directory()
        else:
            self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            if _directory_identity(os.fstat(self.fd)) != self.token:
                self.close()
                raise EvolutionError("managed parent identity changed while binding")
        self.verify()
        return self

    def _open_windows_directory(self) -> None:
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(self.path), 0x1 | 0x80, 0x1 | 0x2, None, 3,
            0x02000000 | 0x00200000, None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise EvolutionError("managed parent could not be bound safely")
        self.windows_handle = int(handle)

    def _windows_bound_path(self) -> Path:
        if self.windows_handle is None:
            raise EvolutionError("managed parent Windows handle is unavailable")
        import ctypes
        from ctypes import wintypes

        get_name = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_name.restype = wintypes.DWORD
        size = get_name(self.windows_handle, None, 0, 0)
        if size == 0:
            raise EvolutionError("managed parent object name is unavailable")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = get_name(self.windows_handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise EvolutionError("managed parent object name is unavailable")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def _operation_path(self, name: str) -> Path:
        parent = self._windows_bound_path() if self.windows_handle is not None else self.path
        return parent / name

    def verify(self) -> None:
        if self.token is None:
            raise EvolutionError("managed parent is not bound")
        _target_path(self.root, self.path.relative_to(self.root).as_posix() + "/placeholder")
        try:
            current = self.path.lstat()
            if self.path.is_symlink() or _is_reparse(self.path) or _directory_identity(current) != self.token:
                raise EvolutionError("managed parent identity changed")
            if self.fd is not None and _directory_identity(os.fstat(self.fd)) != self.token:
                raise EvolutionError("managed directory object changed")
        except OSError as exc:
            raise EvolutionError("managed parent identity could not be verified") from exc

    def child_stat(self, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False) if self.fd is not None else self._operation_path(name).lstat()

    def child_identity(self, name: str) -> tuple[int, int, int, int, int] | None:
        try:
            value = self.child_stat(name)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise EvolutionError("managed child is linked")
        return _file_identity(value)

    def open_exclusive(self, name: str) -> int:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        return os.open(name, flags, 0o600, dir_fd=self.fd) if self.fd is not None else os.open(self._operation_path(name), flags, 0o600)

    def read_bytes(self, name: str, *, maximum: int) -> bytes:
        self.verify()
        expected = self.child_identity(name)
        if expected is None:
            raise EvolutionError("managed target is unavailable")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = (
            os.open(name, flags, dir_fd=self.fd)
            if self.fd is not None else os.open(self._operation_path(name), flags)
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                _file_identity(metadata) != expected or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1 or metadata.st_size > maximum
            ):
                raise EvolutionError("managed target identity or size changed")
            raw = os.read(descriptor, maximum + 1)
            if len(raw) > maximum:
                raise EvolutionError("managed target exceeds the configured size limit")
            self.verify()
            if self.child_identity(name) != expected:
                raise EvolutionError("managed target changed while being read")
            return raw
        finally:
            os.close(descriptor)

    def replace(self, source: str, destination: str) -> None:
        if self.fd is not None:
            os.replace(source, destination, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.replace(self._operation_path(source), self._operation_path(destination))

    def unlink_owned(self, name: str, identity: tuple[int, int, int, int, int] | None) -> None:
        if identity is None or self.child_identity(name) != identity:
            return
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            self._operation_path(name).unlink()

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


def _atomic_target(bound: _ManagedDirectory, name: str, before: bytes, after: bytes,
                   verify: Callable[[], None]) -> tuple[Callable[[], None], Callable[[], None]]:
    nonce = secrets.token_hex(24)
    pending = f".{name}.opc-pending-{nonce}"
    pending_identity = None
    descriptor: int | None = None
    published = False

    def restore() -> None:
        nonlocal published
        if not published:
            return
        restore_name = f".{name}.opc-restore-{secrets.token_hex(24)}"
        restore_identity = None
        restore_fd: int | None = None
        try:
            verify()
            restore_fd = bound.open_exclusive(restore_name)
            restore_identity = _file_identity(os.fstat(restore_fd))
            with os.fdopen(restore_fd, "wb") as handle:
                restore_fd = None
                handle.write(before)
                handle.flush()
                os.fsync(handle.fileno())
                restore_identity = _file_identity(os.fstat(handle.fileno()))
            verify()
            if bound.child_identity(restore_name) != restore_identity:
                raise EvolutionError("managed restore identity changed")
            bound.replace(restore_name, name)
            published = False
            restore_identity = None
        finally:
            if restore_fd is not None:
                os.close(restore_fd)
            bound.unlink_owned(restore_name, restore_identity)

    try:
        verify()
        current = bound.read_bytes(name, maximum=MAX_CAPABILITY_BYTES)
        if current != before or bound.child_stat(name).st_nlink != 1:
            raise EvolutionError("managed target changed after preview")
        descriptor = bound.open_exclusive(pending)
        pending_identity = _file_identity(os.fstat(descriptor))
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
            pending_identity = _file_identity(os.fstat(handle.fileno()))
        verify()
        if bound.child_identity(pending) != pending_identity:
            raise EvolutionError("managed pending identity changed")
        bound.replace(pending, name)
        published = True
        verify()

        def cleanup() -> None:
            return None

        return restore, cleanup
    except BaseException:
        if published:
            restore()
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        bound.unlink_owned(pending, pending_identity)


def apply_transition(project_root: Path, repository_root: Path, proposal_id: str,
                     *, expected_revision: int, kind: str, now: str,
                     plan_token: str,
                     authorization: Mapping[str, Any] | None = None,
                     rollback_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    with _EvolutionBinding(project_root) as binding:
        preview = preview_transition(
            binding.project, repository_root, proposal_id,
            expected_revision=expected_revision, kind=kind, now=now,
            authorization=authorization, rollback_evidence=rollback_evidence,
        )
        if preview["plan_token"] != plan_token:
            raise EvolutionError("transition token does not match the exact preview")
        binding.verify()
        project = binding.project
        directory = binding.directory
        private_bound = binding.ensure_directory()
        root = _git_root(repository_root)
        target = _target_path(root, preview["transition"]["target_path"])
        try:
            record_path = _record_path(project, proposal_id)
            with _exclusive_update_lock(private_bound, record_path.name):
                binding.verify()
                current, current_hash = _read_record(project, proposal_id)
                if current is None or current["revision"] != expected_revision or current_hash != preview["base_record"]["sha256"]:
                    raise EvolutionError("evolution record changed after transition preview")
                _assert_private_or_ignored(project, directory)
                _assert_clean(root)
                if _git_head(root) != preview["repository_head"]:
                    raise EvolutionError("repository HEAD changed after transition preview")
                with _ManagedDirectory(target.parent, root) as managed:
                    restore: Callable[[], None] | None = None
                    cleanup: Callable[[], None] | None = None
                    def verify() -> None:
                        binding.verify()
                        managed.verify()
                        _assert_private_or_ignored(project, directory)
                        if _git_head(root) != preview["repository_head"]:
                            raise EvolutionError("repository HEAD changed during transition")

                    try:
                        restore, cleanup = _atomic_target(
                            managed, target.name, preview["before_bytes"],
                            preview["after_bytes"], verify,
                        )
                        # The tracked and complete worktree status are both
                        # exactly the one managed target.
                        tracked = _git(root, ["diff", "--name-only", "--"])
                        tracked_paths = [line.strip().replace("\\", "/") for line in tracked.stdout.splitlines() if line.strip()]
                        status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
                        lines = [line for line in status.stdout.splitlines() if line.strip()]
                        status_paths = {line[3:].replace("\\", "/") for line in lines}
                        if (
                            tracked.returncode != 0
                            or tracked_paths != [preview["transition"]["target_path"]]
                            or status.returncode != 0
                            or status_paths != {preview["transition"]["target_path"]}
                        ):
                            raise EvolutionError("transition produced more than the one managed path")
                        _atomic_private(private_bound, record_path.name, preview["record"])
                    except BaseException:
                        if restore is not None:
                            restore()
                        raise
                    else:
                        if cleanup is not None:
                            cleanup()
        except FeedbackError as exc:
            raise EvolutionError("private evolution filesystem identity changed") from exc
        return {
            "record": preview["record"], "diff": preview["diff"],
            "git_stage_pathspecs": preview["git_stage_pathspecs"],
            "staged": False, "committed": False,
        }


def preview_confirm(project_root: Path, repository_root: Path, proposal_id: str,
                    *, expected_revision: int, now: str) -> dict[str, Any]:
    _timestamp(now, "now")
    project = project_root.expanduser().resolve(strict=True)
    directory = project / ".opc" / "evolution"
    record, base_hash = _read_record(project, proposal_id)
    if record is None or record["revision"] != expected_revision or record["pending_transition"] is None:
        raise EvolutionError("pending transition is unavailable or stale")
    if _timestamp(now, "now") < _timestamp(record["updated_at"], "record.updated_at"):
        raise EvolutionError("confirmation timestamp cannot move backward")
    _revalidate_cumulative_evidence(project, record, now=now, include_promotion=True)
    root = _git_root(repository_root)
    _assert_clean(root)
    _verify_proposal_git(root, record["proposal"], require_head=False)
    head = _git_head(root)
    transition = record["pending_transition"]
    if head == transition["source_commit"]:
        raise EvolutionError("confirmation requires a newly created commit, not a reset to source history")
    target = transition["target_path"]
    _strict_linear_target_range(
        root, transition["base_head"], head, target,
        label=f"{transition['kind']} confirmation",
    )
    _, _, after = _git_blob_info(root, head, target)
    if _sha(after) != transition["after_sha256"]:
        raise EvolutionError("confirmed commit does not contain the previewed capability bytes")
    path = _target_path(root, target)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvolutionError("confirmed checkout target could not be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or _is_reparse(path) or metadata.st_nlink != 1:
        raise EvolutionError("confirmed checkout target must remain a single-link regular file")
    updated = json.loads(json.dumps(record))
    if transition["kind"] == "promotion":
        active = dict(record["proposal"]["candidate_version"])
        new_state, action = "promoted", "promotion_confirmed"
    else:
        active = dict(record["proposal"]["rollback_target"])
        new_state, action = "rolled_back", "rollback_confirmed"
    active["source_commit"] = head
    updated["active_version"] = active
    updated["pending_transition"] = None
    updated["revision"] = expected_revision + 1
    updated["state"] = new_state
    updated["updated_at"] = now
    updated["history"].append(_history(updated["revision"], new_state, action, now))
    validate_record(updated)
    core = {
        "operation": "confirm", "proposal_id": proposal_id,
        "expected_revision": expected_revision,
        "base_record": {"exists": True, "sha256": base_hash},
        "repository_head": head,
        "record_sha256": _sha(_json_bytes(updated, maximum=MAX_RECORD_BYTES)),
        "idempotent": False,
    }
    return {**core, "plan_token": _sha(_json_bytes(core, maximum=MAX_RECORD_BYTES)), "record": updated}


def confirm_transition(project_root: Path, repository_root: Path, proposal_id: str,
                       *, expected_revision: int, now: str, plan_token: str) -> dict[str, Any]:
    with _EvolutionBinding(project_root) as binding:
        preview = preview_confirm(
            binding.project, repository_root, proposal_id,
            expected_revision=expected_revision, now=now,
        )
        if preview["plan_token"] != plan_token:
            raise EvolutionError("confirmation token does not match the exact preview")
        binding.verify()
        return _write_previewed(binding, preview, plan_token)


def migration_preview(repository_root: Path, *, kind: str, target_path: str,
                      project_id: str, owner: str, proposal_id: str,
                      candidate_commit: str, candidate_version: str,
                      created_at: str, scope: str = "project") -> dict[str, Any]:
    """Build a zero-write proposal for a committed unversioned v0.1 asset."""
    root = _git_root(repository_root)
    head = _git_head(root)
    target = _portable(target_path, PORTABLE_REF, "target_path", MAX_REF)
    if not _managed_path(kind, target):
        raise EvolutionError("legacy target is outside the managed path allowlist")
    current_raw = _git_blob(root, head, target)
    candidate_raw = _git_blob(root, candidate_commit, target)
    proposal = {
        "schema_version": PROPOSAL_VERSION,
        "contract_version": CONTRACT_VERSION,
        "proposal_id": proposal_id,
        "project_id": project_id,
        "sources": {
            "candidate_refs": [{"ref": ".opc/evolution/migration-candidate.json", "sha256": "0" * 64}],
            "feedback_refs": [],
            "evaluation_refs": [{"ref": ".opc/evaluation/migration.json", "sha256": "0" * 64}],
            "lineage_refs": [{"ref": ".opc/lineage/migration.json", "sha256": "0" * 64}],
        },
        "capability": {"kind": kind, "target_path": target},
        "current_version": {"version": "unversioned-v0.1", "source_path": target, "source_commit": head, "content_sha256": _sha(current_raw)},
        "candidate_version": {"version": candidate_version, "source_path": target, "source_commit": candidate_commit, "content_sha256": _sha(candidate_raw)},
        "rollback_target": {"version": "unversioned-v0.1", "source_path": target, "source_commit": head, "content_sha256": _sha(current_raw)},
        "scope": {"kind": scope, "project_id": project_id if scope == "project" else None},
        "owner": owner,
        "pilot": {"min_cases": 1, "max_cases": 5, "observation_cases": 1},
        "created_at": created_at,
    }
    validate_proposal(proposal)
    _candidate_is_narrow(root, head, candidate_commit, target)
    core = {"operation": "migration_preview", "writes": False, "proposal": proposal}
    return {**core, "plan_token": _sha(_json_bytes(core, maximum=MAX_PROPOSAL_BYTES))}


def render_report(record: Mapping[str, Any]) -> str:
    validate_record(record)
    evaluation = record["evaluation"] or {
        "status": "not_evaluated", "conclusion": "inconclusive",
        "blocking_reasons": ["missing_evidence"], "confounders": [],
        "measured_case_count": 0,
    }
    unavailable = sorted({
        arm["unavailable_reason"]
        for case in record["pilot_cases"] for arm in (case["control"], case["candidate"])
        if arm["measurements"] is None
    })
    lines = [
        f"# Capability evolution {record['proposal']['proposal_id']}", "",
        f"- State: `{record['state']}`",
        f"- Capability: `{record['proposal']['capability']['kind']}` / `{record['proposal']['capability']['target_path']}`",
        f"- Active version: `{record['active_version']['version']}` @ `{record['active_version']['source_commit']}`",
        f"- Pilot cases: `{len(record['pilot_cases'])}` / `{record['proposal']['pilot']['max_cases']}`",
        f"- Measured paired cases: `{evaluation['measured_case_count']}`; unavailable arms: `{'not measured (' + ', '.join(unavailable) + ')' if unavailable else 'none'}`",
        f"- Evaluation: `{evaluation['status']}` / `{evaluation['conclusion']}`",
        f"- Blocking reasons: `{', '.join(evaluation['blocking_reasons']) or 'none'}`",
        f"- Known confounders: `{', '.join(evaluation['confounders']) or 'not recorded'}`",
        f"- Claim boundary: `{REPORT_CLAIM}`; this record does not prove causality or generalization.",
        "", "History and approved knowledge are preserved on rollback.",
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show")
    show.add_argument("--project-root", type=Path, required=True)
    show.add_argument("--proposal-id", required=True)
    report = sub.add_parser("report")
    report.add_argument("--project-root", type=Path, required=True)
    report.add_argument("--proposal-id", required=True)
    open_preview = sub.add_parser("open-preview")
    open_preview.add_argument("--project-root", type=Path, required=True)
    open_preview.add_argument("--repository-root", type=Path, required=True)
    open_preview.add_argument("--proposal", type=Path, required=True)
    open_apply = sub.add_parser("open")
    open_apply.add_argument("--project-root", type=Path, required=True)
    open_apply.add_argument("--repository-root", type=Path, required=True)
    open_apply.add_argument("--proposal", type=Path, required=True)
    open_apply.add_argument("--plan-token", required=True)
    for name in ("action-preview", "action"):
        command = sub.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--proposal-id", required=True)
        command.add_argument("--expected-revision", type=int, required=True)
        command.add_argument(
            "--action", required=True,
            choices=("authorize_pilot", "authorize_promotion", "record_pilot_case", "evaluate", "observe", "reject"),
        )
        command.add_argument("--payload", type=Path, required=True)
        command.add_argument("--now", required=True)
        if name == "action":
            command.add_argument("--plan-token", required=True)
    for name in ("transition-preview", "transition"):
        command = sub.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--repository-root", type=Path, required=True)
        command.add_argument("--proposal-id", required=True)
        command.add_argument("--expected-revision", type=int, required=True)
        command.add_argument("--kind", choices=("promotion", "rollback"), required=True)
        command.add_argument("--authorization", type=Path)
        command.add_argument("--rollback-evidence", type=Path)
        command.add_argument("--now", required=True)
        if name == "transition":
            command.add_argument("--plan-token", required=True)
    for name in ("confirm-preview", "confirm"):
        command = sub.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--repository-root", type=Path, required=True)
        command.add_argument("--proposal-id", required=True)
        command.add_argument("--expected-revision", type=int, required=True)
        command.add_argument("--now", required=True)
        if name == "confirm":
            command.add_argument("--plan-token", required=True)
    migrate = sub.add_parser("migration-preview")
    migrate.add_argument("--repository-root", type=Path, required=True)
    migrate.add_argument("--kind", required=True)
    migrate.add_argument("--target-path", required=True)
    migrate.add_argument("--project-id", required=True)
    migrate.add_argument("--owner", required=True)
    migrate.add_argument("--proposal-id", required=True)
    migrate.add_argument("--candidate-commit", required=True)
    migrate.add_argument("--candidate-version", required=True)
    migrate.add_argument("--created-at", required=True)
    migrate.add_argument("--scope", choices=("project", "organization"), default="project")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"show", "report"}:
            project = args.project_root.expanduser().resolve(strict=True)
            record, _ = _read_record(project, args.proposal_id)
            if record is None:
                print(json.dumps({"status": "unversioned-v0.1", "evolution": "unavailable"}))
            elif args.command == "show":
                print(json.dumps(record, ensure_ascii=False, indent=2))
            else:
                print(render_report(record), end="")
        elif args.command in {"open-preview", "open"}:
            proposal, _ = _read_json_file(args.proposal, maximum=MAX_PROPOSAL_BYTES, label="proposal input")
            if args.command == "open-preview":
                result = preview_open(args.project_root, args.repository_root, proposal)
            else:
                result = open_proposal(
                    args.project_root, args.repository_root, proposal,
                    plan_token=args.plan_token,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command in {"action-preview", "action"}:
            payload, _ = _read_json_file(args.payload, maximum=MAX_RECORD_BYTES, label="action payload")
            if args.command == "action-preview":
                result = preview_action(
                    args.project_root, args.proposal_id,
                    expected_revision=args.expected_revision, action=args.action,
                    payload=payload, now=args.now,
                )
            else:
                result = apply_action(
                    args.project_root, args.proposal_id,
                    expected_revision=args.expected_revision, action=args.action,
                    payload=payload, now=args.now, plan_token=args.plan_token,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command in {"transition-preview", "transition"}:
            authorization = None
            rollback_evidence = None
            if args.authorization is not None:
                authorization, _ = _read_json_file(args.authorization, maximum=64 * 1024, label="authorization")
            if args.rollback_evidence is not None:
                rollback_evidence, _ = _read_json_file(args.rollback_evidence, maximum=64 * 1024, label="rollback evidence")
            kwargs = dict(
                expected_revision=args.expected_revision, kind=args.kind, now=args.now,
                authorization=authorization, rollback_evidence=rollback_evidence,
            )
            if args.command == "transition-preview":
                result = preview_transition(
                    args.project_root, args.repository_root, args.proposal_id, **kwargs,
                )
                result = {key: value for key, value in result.items() if key not in {"before_bytes", "after_bytes"}}
            else:
                result = apply_transition(
                    args.project_root, args.repository_root, args.proposal_id,
                    plan_token=args.plan_token, **kwargs,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command in {"confirm-preview", "confirm"}:
            if args.command == "confirm-preview":
                result = preview_confirm(
                    args.project_root, args.repository_root, args.proposal_id,
                    expected_revision=args.expected_revision, now=args.now,
                )
            else:
                result = confirm_transition(
                    args.project_root, args.repository_root, args.proposal_id,
                    expected_revision=args.expected_revision, now=args.now,
                    plan_token=args.plan_token,
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = migration_preview(
                args.repository_root, kind=args.kind, target_path=args.target_path,
                project_id=args.project_id, owner=args.owner,
                proposal_id=args.proposal_id, candidate_commit=args.candidate_commit,
                candidate_version=args.candidate_version, created_at=args.created_at,
                scope=args.scope,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (EvolutionError, OSError) as exc:
        del exc
        print("OPC_EVOLUTION_ERROR: capability evolution failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
