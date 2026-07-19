#!/usr/bin/env python3
"""Strict, portable knowledge-governance contract shared by OPC consumers."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_V1 = 1
SCHEMA_V2 = 2
CONTEXT_VERSION = "opc-knowledge-context-v1"
CONTRACT_VERSION = "opc-knowledge-governance-v1"
MIGRATION_VERSION = "opc-knowledge-schema-migration-v1"
CURATION_VERSION = "opc-knowledge-curation-v1"
MAX_RECORD_BYTES = 512 * 1024
MAX_RECORDS = 5000
MAX_RELATIONS = 64
MAX_KEYWORDS = 128
MAX_CONSTRAINTS = 32
MAX_ALLOWED_VALUES = 32
MAX_TEXT = 262_144
MAX_SHORT_TEXT = 4096
MAX_ID = 128

PORTABLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
PORTABLE_RECORD_ID = re.compile(r"^exp-[A-Za-z0-9._-]+$")
PORTABLE_REF = re.compile(
    r"^(?!/)(?![A-Za-z]:)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")

STATUSES = {"candidate", "approved", "rejected", "obsolete"}
SENSITIVITIES = {"public", "internal", "restricted"}
RELATION_KINDS = {
    "conflicts",
    "supersedes",
    "superseded_by",
    "invalidates",
    "invalidated_by",
}
V1_FIELDS = {
    "schema_version", "id", "type", "summary", "content", "status",
    "keywords", "metadata", "scope", "owner", "project_id", "source",
    "evidence", "confidence", "validation", "approved_by", "approved_at",
    "rejected_by", "rejected_at", "rejection_reason", "obsolete_at",
    "obsolete_reason", "superseded_by", "created_at", "updated_at",
}
V2_FIELDS = (V1_FIELDS - {"superseded_by"}) | {
    "sensitivity", "applicability", "relations"
}
CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "knowledge"
    / "knowledge-governance-contract.v1.json"
)
REQUIRED = {
    "schema_version", "id", "type", "summary", "content", "keywords",
    "metadata", "scope", "owner", "evidence", "confidence", "status",
    "created_at", "updated_at",
}


class GovernanceError(RuntimeError):
    """A fail-closed, non-sensitive governance-contract error."""


def validate_contract(value: Mapping[str, Any]) -> None:
    expected_limits = {
        "record_bytes": MAX_RECORD_BYTES,
        "records_per_status": MAX_RECORDS,
        "query_results": 1000,
        "relations_per_record": MAX_RELATIONS,
        "keywords_per_record": MAX_KEYWORDS,
        "applicability_constraints": MAX_CONSTRAINTS,
        "allowed_values_per_constraint": MAX_ALLOWED_VALUES,
    }
    if (
        value.get("contract_version") != CONTRACT_VERSION
        or value.get("record_schema_current") != SCHEMA_V2
        or value.get("readable_record_schemas") != [SCHEMA_V1, SCHEMA_V2]
        or value.get("context_version") != CONTEXT_VERSION
        or value.get("migration_version") != MIGRATION_VERSION
        or value.get("curation_version") != CURATION_VERSION
        or value.get("limits") != expected_limits
        or value.get("relation_kinds")
        != [
            "conflicts",
            "supersedes",
            "superseded_by",
            "invalidates",
            "invalidated_by",
        ]
        or value.get("hard_filter_order")
        != [
            "scope_and_project_id",
            "approved_status",
            "current_head_commit_and_content_hash",
            "sensitivity_authorization",
            "explicit_applicability",
            "invalidation_and_supersession",
        ]
        or value.get("missing_project_context") != "global_only"
        or value.get("absolute_path_project_inference") is not False
    ):
        raise GovernanceError("knowledge governance contract drifted from runtime")


def load_contract() -> dict[str, Any]:
    try:
        raw = CONTRACT_PATH.read_bytes()
        if len(raw) > 64 * 1024:
            raise GovernanceError("knowledge governance contract exceeds size limit")
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise GovernanceError("knowledge governance contract is invalid") from exc
    if not isinstance(value, dict):
        raise GovernanceError("knowledge governance contract must be an object")
    validate_contract(value)
    return value


def _portable(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ID
        or not pattern.fullmatch(value)
    ):
        raise GovernanceError(f"{label} is not a portable identifier")
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in (
            "session_id", "session-id", "turn_id", "turn-id",
            "thread_id", "thread-id",
        )
    ):
        raise GovernanceError(f"{label} must not contain a runtime identifier")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise GovernanceError(f"{label} must be a bounded RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernanceError(f"{label} must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernanceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _bounded_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_SHORT_TEXT:
        raise GovernanceError(f"{label} must be a non-empty bounded string")
    return value


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GovernanceError("record contains a non-finite number")
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise GovernanceError("record object exceeds the configured field limit")
        for key, nested in value.items():
            if not isinstance(key, str) or len(key) > MAX_SHORT_TEXT:
                raise GovernanceError("record contains an invalid object key")
            _reject_non_finite(nested)
        return
    if isinstance(value, list):
        if len(value) > 1024:
            raise GovernanceError("record array exceeds the configured item limit")
        for nested in value:
            _reject_non_finite(nested)
        return
    raise GovernanceError("record contains a non-JSON value")


def strict_json_bytes(value: Mapping[str, Any], *, maximum: int = MAX_RECORD_BYTES) -> bytes:
    _reject_non_finite(value)
    try:
        payload = (
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GovernanceError("record is not strict JSON") from exc
    if len(payload) > maximum:
        raise GovernanceError("record exceeds the configured size limit")
    return payload


def _string_list(value: Any, label: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise GovernanceError(f"{label} exceeds the configured item limit")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_portable(item, PORTABLE_ID, f"{label}[{index}]"))
    if len(result) != len(set(result)):
        raise GovernanceError(f"{label} must be unique")
    return sorted(result)


def normalize_applicability(record: Mapping[str, Any]) -> dict[str, Any]:
    if record.get("schema_version") == SCHEMA_V1:
        return {
            "roles": [],
            "knowledge_types": [str(record.get("type", ""))],
            "constraints": {},
            "valid_from": None,
            "valid_until": None,
        }
    value = record.get("applicability")
    expected = {"roles", "knowledge_types", "constraints", "valid_from", "valid_until"}
    if not isinstance(value, dict) or set(value) != expected:
        raise GovernanceError("applicability must contain exactly the v2 fields")
    roles = _string_list(value["roles"], "applicability.roles", maximum=MAX_ALLOWED_VALUES)
    types = _string_list(
        value["knowledge_types"],
        "applicability.knowledge_types",
        maximum=MAX_ALLOWED_VALUES,
    )
    if not types or record.get("type") not in types:
        raise GovernanceError("applicability.knowledge_types must include record.type")
    constraints = value["constraints"]
    if not isinstance(constraints, dict) or len(constraints) > MAX_CONSTRAINTS:
        raise GovernanceError("applicability.constraints exceeds the configured limit")
    normalized_constraints: dict[str, list[str]] = {}
    for key in sorted(constraints):
        portable_key = _portable(key, PORTABLE_ID, "applicability constraint key")
        normalized_constraints[portable_key] = _string_list(
            constraints[key],
            f"applicability.constraints.{portable_key}",
            maximum=MAX_ALLOWED_VALUES,
        )
        if not normalized_constraints[portable_key]:
            raise GovernanceError("applicability constraint values must not be empty")
    valid_from = value["valid_from"]
    valid_until = value["valid_until"]
    if valid_from is not None:
        _timestamp(valid_from, "applicability.valid_from")
    if valid_until is not None:
        _timestamp(valid_until, "applicability.valid_until")
    if valid_from is not None and valid_until is not None and _timestamp(
        valid_from, "applicability.valid_from"
    ) >= _timestamp(valid_until, "applicability.valid_until"):
        raise GovernanceError("applicability validity window must be increasing")
    return {
        "roles": roles,
        "knowledge_types": types,
        "constraints": normalized_constraints,
        "valid_from": valid_from,
        "valid_until": valid_until,
    }


def normalize_relations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    if record.get("schema_version") == SCHEMA_V1:
        legacy = record.get("superseded_by")
        if legacy is None:
            return []
        scope = str(record.get("scope", ""))
        return [
            {
                "kind": "superseded_by",
                "target_id": _portable(legacy, PORTABLE_RECORD_ID, "superseded_by"),
                "scope": scope,
                "project_id": record.get("project_id") if scope == "project" else None,
            }
        ]
    value = record.get("relations")
    if not isinstance(value, list) or len(value) > MAX_RELATIONS:
        raise GovernanceError("relations exceeds the configured item limit")
    normalized: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str, str | None]] = set()
    for index, relation in enumerate(value):
        if not isinstance(relation, dict) or set(relation) != {
            "kind", "target_id", "scope", "project_id"
        }:
            raise GovernanceError(f"relations[{index}] must contain exactly the v2 fields")
        kind = relation["kind"]
        if kind not in RELATION_KINDS:
            raise GovernanceError(f"relations[{index}].kind is unsupported")
        target = _portable(
            relation["target_id"], PORTABLE_RECORD_ID, f"relations[{index}].target_id"
        )
        if target == record.get("id"):
            raise GovernanceError("self relations are forbidden")
        scope = relation["scope"]
        project_id = relation["project_id"]
        if scope == "global":
            if project_id is not None:
                raise GovernanceError("global relation must not include project_id")
        elif scope == "project":
            project_id = _portable(
                project_id, PORTABLE_ID, f"relations[{index}].project_id"
            )
        else:
            raise GovernanceError(f"relations[{index}].scope is unsupported")
        key = (kind, target, scope, project_id)
        if key in keys:
            raise GovernanceError("relations must be unique")
        keys.add(key)
        normalized.append(
            {"kind": kind, "target_id": target, "scope": scope, "project_id": project_id}
        )
    return sorted(
        normalized,
        key=lambda item: (
            item["kind"], item["target_id"], item["scope"], item["project_id"] or ""
        ),
    )


def validate_record(record: Mapping[str, Any]) -> None:
    if not isinstance(record, dict):
        raise GovernanceError("knowledge record must be an object")
    strict_json_bytes(record)
    version = record.get("schema_version")
    if version not in {SCHEMA_V1, SCHEMA_V2}:
        raise GovernanceError("unsupported knowledge schema; migrate explicitly")
    allowed = V1_FIELDS if version == SCHEMA_V1 else V2_FIELDS
    if not REQUIRED.issubset(record) or not set(record).issubset(allowed):
        raise GovernanceError(f"knowledge Schema {version} fields are invalid")
    _portable(record.get("id"), PORTABLE_RECORD_ID, "record.id")
    for field in ("type", "summary", "content", "owner"):
        value = record.get(field)
        maximum = MAX_TEXT if field == "content" else MAX_SHORT_TEXT
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise GovernanceError(f"record.{field} is invalid or too large")
    if record.get("status") not in STATUSES:
        raise GovernanceError("record.status is unsupported")
    scope = record.get("scope")
    if scope == "global":
        if record.get("project_id") is not None:
            raise GovernanceError("Global record must not include project_id")
    elif scope == "project":
        _portable(record.get("project_id"), PORTABLE_ID, "record.project_id")
    else:
        raise GovernanceError("record.scope is unsupported")
    keywords = record.get("keywords")
    if not isinstance(keywords, list) or len(keywords) > MAX_KEYWORDS:
        raise GovernanceError("record.keywords exceeds the configured limit")
    if any(not isinstance(item, str) or len(item) > MAX_SHORT_TEXT for item in keywords):
        raise GovernanceError("record.keywords contains an invalid value")
    if len(set(keywords)) != len(keywords):
        raise GovernanceError("record.keywords must be unique")
    confidence = record.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise GovernanceError("record.confidence must be finite and between 0 and 1")
    for field in ("metadata", "evidence"):
        if not isinstance(record.get(field), dict):
            raise GovernanceError(f"record.{field} must be an object")
    _timestamp(record.get("created_at"), "record.created_at")
    _timestamp(record.get("updated_at"), "record.updated_at")
    lifecycle_text = (
        "approved_by",
        "rejected_by",
        "rejection_reason",
        "obsolete_reason",
    )
    for field in lifecycle_text:
        if field in record:
            _bounded_text(record[field], f"record.{field}")
    for field in ("approved_at", "rejected_at", "obsolete_at"):
        if field in record:
            _timestamp(record[field], f"record.{field}")
    if "validation" in record:
        validation = record["validation"]
        if isinstance(validation, str):
            _bounded_text(validation, "record.validation")
        elif not isinstance(validation, dict):
            raise GovernanceError("record.validation must be a bounded string or object")
    required_by_status = {
        "approved": ("approved_by", "approved_at", "validation"),
        "rejected": ("rejected_by", "rejected_at", "rejection_reason"),
        "obsolete": ("obsolete_at", "obsolete_reason"),
    }
    for field in required_by_status.get(record["status"], ()):
        if field not in record:
            raise GovernanceError(f"{record['status']} record requires {field}")
    if version == SCHEMA_V2:
        if record.get("sensitivity") not in SENSITIVITIES:
            raise GovernanceError("record.sensitivity is unsupported")
        normalize_applicability(record)
        normalize_relations(record)
    elif "superseded_by" in record:
        _portable(record["superseded_by"], PORTABLE_RECORD_ID, "record.superseded_by")


def migrate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    validate_record(record)
    if record.get("schema_version") == SCHEMA_V2:
        return dict(record)
    migrated = dict(record)
    migrated["schema_version"] = SCHEMA_V2
    migrated["sensitivity"] = "internal"
    migrated["applicability"] = normalize_applicability(record)
    migrated["relations"] = normalize_relations(record)
    migrated.pop("superseded_by", None)
    validate_record(migrated)
    return migrated


def relation_applies(relation: Mapping[str, Any], project_id: str | None) -> bool:
    if relation.get("scope") == "global":
        return relation.get("project_id") is None
    return bool(
        project_id
        and relation.get("scope") == "project"
        and relation.get("project_id") == project_id
    )


def applicability_reasons(
    record: Mapping[str, Any],
    *,
    role: str | None,
    knowledge_type: str | None,
    context: Mapping[str, str],
    at: datetime,
) -> list[str]:
    value = normalize_applicability(record)
    reasons: list[str] = []
    if value["roles"] and role not in value["roles"]:
        reasons.append("role_not_applicable" if role else "role_context_missing")
    if knowledge_type and knowledge_type not in value["knowledge_types"]:
        reasons.append("knowledge_type_not_applicable")
    for key, allowed in value["constraints"].items():
        if key not in context:
            reasons.append("applicability_context_missing")
        elif context[key] not in allowed:
            reasons.append("explicit_applicability_mismatch")
    if value["valid_from"] is not None and at < _timestamp(
        value["valid_from"], "applicability.valid_from"
    ):
        reasons.append("not_yet_applicable")
    if value["valid_until"] is not None and at >= _timestamp(
        value["valid_until"], "applicability.valid_until"
    ):
        reasons.append("stale")
    return sorted(set(reasons))


def canonical_citation(
    record: Mapping[str, Any], provenance: Mapping[str, Any]
) -> dict[str, Any]:
    source_path = provenance.get("source_path")
    content_hash = provenance.get("content_hash")
    source_commit = provenance.get("source_commit")
    if (
        not isinstance(source_path, str)
        or len(source_path) > 240
        or not PORTABLE_REF.fullmatch(source_path)
        or not isinstance(content_hash, str)
        or not SHA256.fullmatch(content_hash)
        or not isinstance(source_commit, str)
        or not GIT_COMMIT.fullmatch(source_commit)
    ):
        raise GovernanceError("canonical provenance is incomplete")
    return {
        "record_id": record["id"],
        "source_path": source_path,
        "source_commit": source_commit,
        "content_sha256": content_hash,
        "scope": record["scope"],
        "project_id": record.get("project_id"),
        "knowledge_type": record["type"],
        "status": record["status"],
        "sensitivity": record.get("sensitivity", "internal"),
    }


def relation_cycles(edges: Mapping[str, set[str]]) -> set[str]:
    """Return only directed-cycle nodes using a bounded iterative DFS."""

    nodes = set(edges)
    edge_count = 0
    for targets in edges.values():
        edge_count += len(targets)
        nodes.update(targets)
    if len(nodes) > MAX_RECORDS or edge_count > MAX_RECORDS * MAX_RELATIONS:
        raise GovernanceError("relation graph exceeds the configured limit")

    state: dict[str, int] = {}
    path: list[str] = []
    positions: dict[str, int] = {}
    cyclic: set[str] = set()
    for root in sorted(nodes):
        if state.get(root, 0) != 0:
            continue
        state[root] = 1
        positions[root] = len(path)
        path.append(root)
        frames: list[tuple[str, Any]] = [
            (root, iter(sorted(edges.get(root, set()))))
        ]
        while frames:
            node, targets = frames[-1]
            try:
                target = next(targets)
            except StopIteration:
                frames.pop()
                finished = path.pop()
                positions.pop(finished, None)
                state[node] = 2
                continue
            target_state = state.get(target, 0)
            if target_state == 0:
                state[target] = 1
                positions[target] = len(path)
                path.append(target)
                frames.append((target, iter(sorted(edges.get(target, set())))))
            elif target_state == 1:
                cyclic.update(path[positions[target] :])
    return cyclic


def evaluate_relation_governance(
    inventory: Mapping[str, Mapping[str, Any]],
    base_reasons: Mapping[str, Sequence[str]],
    *,
    project_id: str | None,
) -> dict[str, Any]:
    """Evaluate relation structure and effects once from a frozen graph.

    Callers own record loading, provenance and hard-filter evaluation.  This
    function is the single #7 relation engine used by flat and hierarchical
    recall, so ordering and intermediate effects cannot diverge.
    """

    if len(inventory) > MAX_RECORDS or set(inventory) != set(base_reasons):
        raise GovernanceError("relation governance inventory is inconsistent")
    relation_reasons: dict[str, set[str]] = {
        record_id: set() for record_id in inventory
    }
    hard_filter_eligible = {
        record_id for record_id in inventory if not base_reasons.get(record_id)
    }
    normalized_by_source: dict[str, list[dict[str, Any]]] = {}
    for source_id in sorted(hard_filter_eligible):
        try:
            normalized_by_source[source_id] = normalize_relations(
                inventory[source_id]
            )
        except GovernanceError:
            relation_reasons[source_id].add("relations_invalid")

    edges: dict[str, set[str]] = {}
    active_relations: list[tuple[str, str, str]] = []
    for source_id in sorted(hard_filter_eligible):
        if relation_reasons[source_id]:
            continue
        for relation in normalized_by_source.get(source_id, []):
            if not relation_applies(relation, project_id):
                continue
            target_id = relation["target_id"]
            if target_id not in inventory:
                relation_reasons[source_id].add("relation_target_missing")
                continue
            kind = relation["kind"]
            if (
                target_id not in hard_filter_eligible
                or relation_reasons[target_id]
            ):
                if kind in {"superseded_by", "invalidated_by"}:
                    relation_reasons[source_id].add("relation_target_ineligible")
                continue
            active_relations.append((source_id, target_id, kind))
            if kind != "conflicts":
                edges.setdefault(source_id, set()).add(target_id)

    for record_id in relation_cycles(edges):
        relation_reasons[record_id].add("relation_cycle")

    inverse_dependents: dict[str, set[str]] = {}
    for source_id, target_id, kind in active_relations:
        if kind in {"superseded_by", "invalidated_by"}:
            inverse_dependents.setdefault(target_id, set()).add(source_id)
    structurally_ineligible = [
        record_id
        for record_id in sorted(hard_filter_eligible)
        if relation_reasons[record_id]
    ]
    queue_index = 0
    while queue_index < len(structurally_ineligible):
        target_id = structurally_ineligible[queue_index]
        queue_index += 1
        for source_id in sorted(inverse_dependents.get(target_id, set())):
            if relation_reasons[source_id]:
                continue
            relation_reasons[source_id].add("relation_target_ineligible")
            structurally_ineligible.append(source_id)

    relation_effects: dict[str, set[str]] = {
        record_id: set() for record_id in inventory
    }
    for source_id, target_id, kind in active_relations:
        if base_reasons.get(source_id) or relation_reasons[source_id]:
            continue
        target_eligible = (
            not base_reasons.get(target_id)
            and not relation_reasons[target_id]
        )
        if kind in {"supersedes", "invalidates"}:
            if target_eligible:
                relation_effects[target_id].add(
                    "superseded" if kind == "supersedes" else "invalidated"
                )
        elif kind in {"superseded_by", "invalidated_by"}:
            if target_eligible:
                relation_effects[source_id].add(
                    "superseded" if kind == "superseded_by" else "invalidated"
                )
    for record_id, effects in relation_effects.items():
        relation_reasons[record_id].update(effects)

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

    return {
        "relation_reasons": {
            record_id: tuple(sorted(reasons))
            for record_id, reasons in sorted(relation_reasons.items())
        },
        "conflict_pairs": tuple(sorted(conflict_pairs)),
    }


def validate_query_context(
    *,
    project_id: str | None,
    role: str | None,
    applicability: Mapping[str, str] | None,
    allowed_sensitivity: Sequence[str] | None,
    limit: int,
) -> tuple[dict[str, str], tuple[str, ...]]:
    if project_id is not None:
        _portable(project_id, PORTABLE_ID, "project_id")
    if role is not None:
        _portable(role, PORTABLE_ID, "role")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise GovernanceError("limit must be between 1 and 1000")
    normalized_context: dict[str, str] = {}
    if applicability is not None:
        if not isinstance(applicability, Mapping) or len(applicability) > MAX_CONSTRAINTS:
            raise GovernanceError("applicability context exceeds the configured limit")
        for key in sorted(applicability):
            normalized_key = _portable(key, PORTABLE_ID, "applicability context key")
            normalized_context[normalized_key] = _portable(
                applicability[key], PORTABLE_ID, f"applicability context {normalized_key}"
            )
    selected = tuple(sorted(set(allowed_sensitivity or ("public", "internal"))))
    if not selected or any(value not in SENSITIVITIES for value in selected):
        raise GovernanceError("allowed sensitivity set is invalid")
    return normalized_context, selected
