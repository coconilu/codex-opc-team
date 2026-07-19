#!/usr/bin/env python3
"""Zero-dependency hierarchical File/Git recall and ContextPacket assembly.

The hierarchy is a private, disposable navigation aid.  Every injected L2
leaf is re-read from canonical File/Git and revalidated against current HEAD.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from opc_governance import (
    GovernanceError,
    applicability_reasons,
    canonical_citation,
    normalize_relations,
    relation_applies,
    relation_cycles,
    validate_query_context,
)
from opc_memory import (
    MAX_RECORDS,
    MEMORY_STATUSES,
    FileGitBackend,
    OpcMemoryError,
    RecallProvider,
    _assert_unlinked_ancestors,
    _call_with_timeout,
    _directory_object_token,
    _git,
    _lstat_identity,
    _read_bounded_bytes,
    safe_record_id,
    sha256_bytes,
    validate_root_isolation,
)


# The conditional import expression above is intentionally replaced below.
# Keeping imports explicit makes the module's zero-dependency boundary obvious.

CONTRACT_VERSION = "opc-hierarchical-context-contract-v1"
INDEX_VERSION = "opc-hierarchical-index-v1"
PACKET_VERSION = "opc-context-packet-v1"
TRACE_VERSION = "opc-recall-trace-v1"
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_BUDGET_TOKENS = 200_000
MAX_CANONICAL_READS = 64
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PLUGIN_ROOT / "assets" / "context" / "hierarchical-context-contract.v1.json"
DERIVED_RELATIVE = Path(".opc") / "derived" / "hierarchical-recall-v1"
INDEX_NAME = "index.json"
PORTABLE = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
TOKEN_TERMS = re.compile(r"[\w-]+", re.UNICODE)


class HierarchicalError(RuntimeError):
    """A redacted, user-actionable hierarchical recall error."""


def _strict_json_bytes(value: Mapping[str, Any], *, maximum: int = MAX_INDEX_BYTES) -> bytes:
    def reject_non_finite(item: Any) -> None:
        if isinstance(item, bool) or item is None or isinstance(item, (str, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise HierarchicalError("non-finite number is forbidden")
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise HierarchicalError("JSON object keys must be strings")
                reject_non_finite(nested)
            return
        if isinstance(item, list):
            for nested in item:
                reject_non_finite(nested)
            return
        raise HierarchicalError("value is not strict JSON")

    reject_non_finite(value)
    try:
        payload = (
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HierarchicalError("value is not strict JSON") from exc
    if len(payload) > maximum:
        raise HierarchicalError("derived index exceeds the configured size limit")
    return payload


def _read_json(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    try:
        raw = _read_bounded_bytes(path, label=label, maximum=maximum)
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OpcMemoryError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise HierarchicalError(f"{label} is not valid bounded JSON") from exc
    if not isinstance(value, dict):
        raise HierarchicalError(f"{label} must be an object")
    _strict_json_bytes(value, maximum=maximum)
    return value


def load_contract() -> dict[str, Any]:
    try:
        metadata = CONTRACT_PATH.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > 128 * 1024
        ):
            raise HierarchicalError("hierarchical contract is not one bounded regular file")
        raw = CONTRACT_PATH.read_bytes()
        contract = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise HierarchicalError("hierarchical contract is invalid") from exc
    if not isinstance(contract, dict):
        raise HierarchicalError("hierarchical contract must be an object")
    _strict_json_bytes(contract, maximum=128 * 1024)
    expected = {
        "contract_version": CONTRACT_VERSION,
        "index_version": INDEX_VERSION,
        "context_packet_version": PACKET_VERSION,
        "recall_trace_version": TRACE_VERSION,
        "authority": "file-git-only",
        "derived_data_authoritative": False,
        "provider_authoritative": False,
        "preview_writes": False,
        "hard_filter_before_navigation": True,
        "l2_revalidation_required": True,
    }
    for key, value in expected.items():
        if contract.get(key) != value:
            raise HierarchicalError("hierarchical contract drifted from runtime")
    limits = contract.get("limits")
    if not isinstance(limits, dict) or limits != {
        "index_bytes": MAX_INDEX_BYTES,
        "records": MAX_RECORDS,
        "canonical_reads": MAX_CANONICAL_READS,
        "budget_tokens": MAX_BUDGET_TOKENS,
    }:
        raise HierarchicalError("hierarchical contract limits drifted from runtime")
    return contract


def _head(root: Path) -> str | None:
    value = _git(root, ("rev-parse", "HEAD"))
    return value if isinstance(value, str) and GIT_COMMIT.fullmatch(value) else None


def _portable(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or not PORTABLE.fullmatch(value):
        raise HierarchicalError(f"{label} must be a portable identifier")
    lowered = value.lower()
    if any(marker in lowered for marker in ("session-id", "session_id", "turn-id", "turn_id")):
        raise HierarchicalError(f"{label} contains a forbidden runtime identifier")
    return value


def _metadata_navigation(value: Any) -> dict[str, Any]:
    """Keep only explicit role metadata; arbitrary metadata may be sensitive."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("role", "fixture_role"):
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str) and len(item) <= 128 and PORTABLE.fullmatch(item):
            result[key] = item
    return result


def _leaf_uri(record: Mapping[str, Any]) -> str:
    root = "global" if record.get("scope") == "global" else f"projects/{record['project_id']}"
    return f"opc://{root}/{record['type']}/{record['id']}"


def _leaf_from_record(
    backend: FileGitBackend, record: Mapping[str, Any], source_path: str
) -> dict[str, Any]:
    provenance = backend.source_metadata(source_path)
    applicability = record.get("applicability")
    if not isinstance(applicability, dict):
        applicability = {
            "roles": [],
            "knowledge_types": [record.get("type")],
            "constraints": {},
            "valid_from": None,
            "valid_until": None,
        }
    return {
        "node_id": str(record["id"]),
        "uri": _leaf_uri(record),
        "node_kind": "leaf",
        "level": "L0",
        "parent_uri": _leaf_uri(record).rsplit("/", 1)[0],
        "summary": str(record["summary"]),
        "knowledge_type": str(record["type"]),
        "keywords": sorted(str(item) for item in record.get("keywords", [])),
        "metadata": _metadata_navigation(record.get("metadata")),
        "scope": str(record["scope"]),
        "project_id": record.get("project_id"),
        "status": str(record["status"]),
        "sensitivity": str(record.get("sensitivity", "internal")),
        "applicability": applicability,
        "relations": normalize_relations(record),
        "source_path": source_path,
        "source_commit": provenance.get("source_commit"),
        "content_sha256": provenance.get("content_hash"),
    }


def _containers(leaves: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    children: dict[str, list[Mapping[str, Any]]] = {}
    for leaf in leaves:
        parent = str(leaf["parent_uri"])
        children.setdefault(parent, []).append(leaf)
    roots = {"opc://global"}
    roots.update(
        f"opc://projects/{leaf['project_id']}"
        for leaf in leaves
        if leaf.get("scope") == "project"
    )
    result: list[dict[str, Any]] = []
    for parent in sorted(children):
        members = sorted(children[parent], key=lambda item: str(item["node_id"]))
        terms = sorted(
            {
                str(term).lower()
                for member in members
                for term in member.get("keywords", [])
            }
        )[:32]
        result.append(
            {
                "node_id": hashlib.sha256(parent.encode("utf-8")).hexdigest()[:16],
                "uri": parent,
                "node_kind": "directory",
                "level": "L1",
                "parent_uri": parent.rsplit("/", 1)[0],
                "child_ids": [str(member["node_id"]) for member in members],
                "overview": " ".join([parent.rsplit("/", 1)[-1], *terms]),
            }
        )
    for root in sorted(roots):
        child_uris = sorted(
            node["uri"] for node in result if node["parent_uri"] == root
        )
        result.append(
            {
                "node_id": hashlib.sha256(root.encode("utf-8")).hexdigest()[:16],
                "uri": root,
                "node_kind": "namespace",
                "level": "L1",
                "parent_uri": "opc://",
                "child_uris": child_uris,
                "overview": root.replace("opc://", "").replace("/", " "),
            }
        )
    result.append(
        {
            "node_id": hashlib.sha256(b"opc://").hexdigest()[:16],
            "uri": "opc://",
            "node_kind": "namespace",
            "level": "L1",
            "parent_uri": None,
            "child_uris": sorted(roots),
            "overview": "global projects",
        }
    )
    return sorted(result, key=lambda item: str(item["uri"]))


class HierarchicalIndex:
    """Previewed, private and atomically published derived navigation index."""

    def __init__(self, backend: FileGitBackend, data_root: Path | str):
        self.backend = backend
        lexical = _assert_unlinked_ancestors(Path(data_root), label="data_root")
        self.data_root = lexical.resolve()
        validate_root_isolation(backend.root, self.data_root)
        self.directory = self.data_root / DERIVED_RELATIVE
        self.path = self.directory / INDEX_NAME

    def _reject_git_worktree(self) -> None:
        existing = self.data_root
        while not existing.exists() and existing.parent != existing:
            existing = existing.parent
        top = _git(existing, ("rev-parse", "--show-toplevel")) if existing.exists() else None
        if isinstance(top, str):
            try:
                self.data_root.relative_to(Path(top).resolve())
            except ValueError:
                return
            raise HierarchicalError("derived data_root must not be inside a Git worktree")

    def _snapshot(self) -> dict[str, Any]:
        load_contract()
        head = _head(self.backend.root)
        if head is None:
            raise HierarchicalError("canonical knowledge must be in a Git repository")
        leaves: list[dict[str, Any]] = []
        ids: set[str] = set()
        for status in MEMORY_STATUSES:
            paths = sorted(self.backend._folder(status).glob("*.json"))
            if len(paths) > MAX_RECORDS:
                raise HierarchicalError("knowledge status exceeds the record limit")
            for path in paths:
                record = self.backend._load_record(path)
                record_id = safe_record_id(str(record["id"]))
                if path.stem != record_id or record_id in ids:
                    raise HierarchicalError("duplicate or mismatched canonical record id")
                ids.add(record_id)
                relative = path.relative_to(self.backend.root).as_posix()
                leaves.append(_leaf_from_record(self.backend, record, relative))
        leaves.sort(key=lambda item: str(item["node_id"]))
        contract_hash = sha256_bytes(CONTRACT_PATH.read_bytes())
        result = {
            "schema_version": INDEX_VERSION,
            "contract_version": CONTRACT_VERSION,
            "contract_sha256": contract_hash,
            "authority": "file-git-only",
            "derived": True,
            "canonical_head": head,
            "virtual_root": "opc://",
            "leaves": leaves,
            "nodes": _containers(leaves),
        }
        _strict_json_bytes(result)
        return result

    def preview(self) -> dict[str, Any]:
        self._reject_git_worktree()
        index = self._snapshot()
        payload = _strict_json_bytes(index)
        return {
            "schema_version": INDEX_VERSION,
            "dry_run": True,
            "writes_performed": 0,
            "data_root": str(self.data_root),
            "derived_relative_path": (DERIVED_RELATIVE / INDEX_NAME).as_posix(),
            "canonical_head": index["canonical_head"],
            "record_count": len(index["leaves"]),
            "node_count": len(index["nodes"]),
            "approval_token": sha256_bytes(payload),
        }

    def build(self, *, approval_token: str | None) -> dict[str, Any]:
        if not isinstance(approval_token, str) or not SHA256.fullmatch(approval_token):
            raise HierarchicalError("build requires the exact preview approval token")
        plan = self.preview()
        if plan["approval_token"] != approval_token:
            raise HierarchicalError("hierarchical index plan changed")
        index = self._snapshot()
        payload = _strict_json_bytes(index)
        if sha256_bytes(payload) != approval_token:
            raise HierarchicalError("canonical knowledge changed after preview")
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        if self.directory.parent.is_symlink():
            raise HierarchicalError("derived parent is linked")
        self.directory.mkdir(exist_ok=True)
        try:
            self.directory.resolve().relative_to(self.data_root)
        except ValueError as exc:
            raise HierarchicalError("derived directory escaped private data_root") from exc
        parent_token = _directory_object_token(_lstat_identity(self.directory))
        ignore = self.data_root / ".opc" / ".gitignore"
        ignore_payload = b"*\n!.gitignore\n"
        if ignore.exists():
            existing_ignore = _read_bounded_bytes(
                ignore, label="derived ignore marker", maximum=64
            )
            if existing_ignore != ignore_payload:
                raise HierarchicalError("private .opc ignore marker is not owned by this contract")
        else:
            descriptor = os.open(ignore, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, ignore_payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if self.path.exists():
            _read_bounded_bytes(
                self.path,
                label="existing derived hierarchical index",
                maximum=MAX_INDEX_BYTES,
            )
        temporary = self.directory / f".{INDEX_NAME}.tmp-{os.getpid()}"
        if temporary.exists():
            raise HierarchicalError("derived temporary path already exists")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if _directory_object_token(_lstat_identity(self.directory)) != parent_token:
                raise HierarchicalError("derived parent changed during publish")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            **plan,
            "dry_run": False,
            "writes_performed": 1,
            "index_sha256": sha256_bytes(payload),
        }

    def delete_preview(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"dry_run": True, "exists": False, "writes_performed": 0, "approval_token": None}
        raw = _read_bounded_bytes(self.path, label="derived hierarchical index", maximum=MAX_INDEX_BYTES)
        return {
            "dry_run": True,
            "exists": True,
            "writes_performed": 0,
            "approval_token": sha256_bytes(raw),
        }

    def delete(self, *, approval_token: str | None) -> dict[str, Any]:
        plan = self.delete_preview()
        if not plan["exists"]:
            return {**plan, "dry_run": False}
        if approval_token != plan["approval_token"]:
            raise HierarchicalError("delete requires the unchanged preview approval token")
        parent_token = _directory_object_token(_lstat_identity(self.directory))
        if _directory_object_token(_lstat_identity(self.path.parent)) != parent_token:
            raise HierarchicalError("derived parent changed before delete")
        self.path.unlink()
        return {**plan, "dry_run": False, "writes_performed": 1}

    def load(self) -> dict[str, Any]:
        load_contract()
        value = _read_json(self.path, maximum=MAX_INDEX_BYTES, label="derived hierarchical index")
        if value.get("schema_version") != INDEX_VERSION or value.get("contract_version") != CONTRACT_VERSION:
            raise HierarchicalError("derived index version is unsupported")
        if value.get("authority") != "file-git-only" or value.get("derived") is not True:
            raise HierarchicalError("derived index authority boundary is invalid")
        expected_contract = sha256_bytes(CONTRACT_PATH.read_bytes())
        if value.get("contract_sha256") != expected_contract:
            raise HierarchicalError("derived index contract is stale")
        leaves = value.get("leaves")
        nodes = value.get("nodes")
        if not isinstance(leaves, list) or not isinstance(nodes, list) or len(leaves) > MAX_RECORDS:
            raise HierarchicalError("derived index shape is invalid")
        seen_ids: set[str] = set()
        seen_uris: set[str] = set()
        required_leaf = {
            "node_id", "uri", "node_kind", "level", "parent_uri", "summary",
            "knowledge_type", "keywords", "metadata", "scope", "project_id",
            "status", "sensitivity", "applicability", "relations", "source_path",
            "source_commit", "content_sha256",
        }
        for leaf in leaves:
            if not isinstance(leaf, dict) or set(leaf) != required_leaf:
                raise HierarchicalError("derived leaf contract is invalid")
            record_id = _portable(leaf["node_id"], "derived leaf id")
            uri = leaf["uri"]
            if (
                record_id in seen_ids
                or not isinstance(uri, str)
                or not uri.startswith("opc://")
                or "\\" in uri
                or ".." in uri.split("/")
            ):
                raise HierarchicalError("derived leaf identity is invalid")
            seen_ids.add(record_id)
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("uri"), str):
                raise HierarchicalError("derived node contract is invalid")
            uri = node["uri"]
            if uri in seen_uris or not uri.startswith("opc://"):
                raise HierarchicalError("derived node identity is invalid")
            seen_uris.add(uri)
        return value

    def status(self) -> dict[str, Any]:
        try:
            value = self.load()
            head = _head(self.backend.root)
            fresh = bool(head and value.get("canonical_head") == head)
            return {
                "available": True,
                "fresh": fresh,
                "health": "ready" if fresh else "stale-flat-fallback",
                "record_count": len(value["leaves"]),
                "derived": True,
                "authority": "file-git-only",
            }
        except (HierarchicalError, OpcMemoryError, OSError):
            return {
                "available": False,
                "fresh": False,
                "health": "missing-or-invalid-flat-fallback",
                "record_count": 0,
                "derived": True,
                "authority": "file-git-only",
            }


def _terms(text: str) -> set[str]:
    return {term.lower() for term in TOKEN_TERMS.findall(text) if term}


def _navigation_score(query: str, text: str, keywords: Sequence[str] = ()) -> int:
    wanted = _terms(query)
    if not wanted:
        return 1
    haystack = text.lower()
    keyword_set = {str(item).lower() for item in keywords}
    return (8 if query.strip().lower() in haystack else 0) + sum(
        3 if term in keyword_set else 1 if term in haystack else 0 for term in wanted
    )


def _token_cost(value: str) -> int:
    return max(1, (len(value.encode("utf-8")) + 3) // 4)


def _bucket(record: Mapping[str, Any]) -> str:
    kind = str(record.get("type", ""))
    if kind == "decision":
        return "decisions"
    if kind == "procedure":
        return "procedures"
    if kind in {"lesson", "experience"}:
        return "experiences"
    return "facts"


def validate_context_packet(packet: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "query_sha256", "mode", "facts", "decisions",
        "experiences", "procedures", "citations", "conflicts", "budget",
        "omitted_summary",
    }
    if not isinstance(packet, Mapping) or set(packet) != expected:
        raise HierarchicalError("ContextPacket fields are not strict")
    if packet.get("schema_version") != PACKET_VERSION or not SHA256.fullmatch(
        str(packet.get("query_sha256", ""))
    ):
        raise HierarchicalError("ContextPacket identity is invalid")
    if packet.get("mode") not in {"hierarchical-file-git", "flat-file-git-fallback"}:
        raise HierarchicalError("ContextPacket mode is invalid")

    def validate_citation(value: Any) -> None:
        expected_citation = {
            "record_id", "source_path", "source_commit", "content_sha256", "scope",
            "project_id", "knowledge_type", "status", "sensitivity",
        }
        if not isinstance(value, Mapping) or set(value) != expected_citation:
            raise HierarchicalError("ContextPacket citation fields are not strict")
        _portable(value["record_id"], "citation record id")
        source_path = value["source_path"]
        if (
            not isinstance(source_path, str)
            or not source_path
            or "\\" in source_path
            or source_path.startswith("/")
            or any(part in {"", ".", ".."} for part in source_path.split("/"))
            or not GIT_COMMIT.fullmatch(str(value["source_commit"]))
            or not SHA256.fullmatch(str(value["content_sha256"]))
            or value["status"] != "approved"
            or value["scope"] not in {"global", "project"}
            or value["sensitivity"] not in {"public", "internal", "restricted"}
        ):
            raise HierarchicalError("ContextPacket citation provenance is invalid")
        project_id = value["project_id"]
        if project_id is not None:
            _portable(project_id, "citation project id")
        _portable(value["knowledge_type"], "citation knowledge type")

    for bucket in ("facts", "decisions", "experiences", "procedures"):
        items = packet.get(bucket)
        if not isinstance(items, list):
            raise HierarchicalError("ContextPacket category must be an array")
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "record_id", "content", "citation", "token_cost"
            }:
                raise HierarchicalError("ContextPacket item fields are not strict")
            _portable(item["record_id"], "ContextPacket record id")
            if (
                not isinstance(item["content"], str)
                or isinstance(item["token_cost"], bool)
                or not isinstance(item["token_cost"], int)
                or item["token_cost"] < 1
            ):
                raise HierarchicalError("ContextPacket item is invalid")
            validate_citation(item["citation"])
    citations = packet.get("citations")
    if not isinstance(citations, list):
        raise HierarchicalError("ContextPacket citations must be an array")
    for citation in citations:
        validate_citation(citation)
    conflicts = packet.get("conflicts")
    if not isinstance(conflicts, list):
        raise HierarchicalError("ContextPacket conflicts must be an array")
    for conflict in conflicts:
        if (
            not isinstance(conflict, dict)
            or set(conflict) != {"reason_code", "citations"}
            or conflict["reason_code"] != "unresolved_conflict"
            or not isinstance(conflict["citations"], list)
            or len(conflict["citations"]) != 2
        ):
            raise HierarchicalError("ContextPacket conflict fields are invalid")
        for citation in conflict["citations"]:
            validate_citation(citation)
    budget = packet.get("budget")
    if not isinstance(budget, dict) or set(budget) != {
        "limit_tokens", "used_tokens", "remaining_tokens"
    }:
        raise HierarchicalError("ContextPacket budget fields are invalid")
    limit = budget["limit_tokens"]
    used = budget["used_tokens"]
    remaining = budget["remaining_tokens"]
    if (
        any(isinstance(item, bool) or not isinstance(item, int) for item in (limit, used, remaining))
        or not 1 <= limit <= MAX_BUDGET_TOKENS
        or used < 0
        or remaining < 0
        or used + remaining != limit
    ):
        raise HierarchicalError("ContextPacket budget aggregate is impossible")
    omitted = packet.get("omitted_summary")
    if not isinstance(omitted, dict) or set(omitted) != {"count", "reason_codes"}:
        raise HierarchicalError("ContextPacket omitted summary is invalid")
    if isinstance(omitted["count"], bool) or not isinstance(omitted["count"], int) or omitted["count"] < 0:
        raise HierarchicalError("ContextPacket omitted count is invalid")
    if (
        not isinstance(omitted["reason_codes"], list)
        or any(not isinstance(item, str) or not item for item in omitted["reason_codes"])
        or omitted["reason_codes"] != sorted(set(omitted["reason_codes"]))
    ):
        raise HierarchicalError("ContextPacket omission reasons are invalid")


def validate_recall_trace(trace: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "query_sha256", "mode", "root_selection", "expansions",
        "discards", "fallbacks", "final_leaves", "canonical_read_count",
        "injected_token_cost",
    }
    if not isinstance(trace, Mapping) or set(trace) != expected:
        raise HierarchicalError("RecallTrace fields are not strict")
    if trace.get("schema_version") != TRACE_VERSION or not SHA256.fullmatch(
        str(trace.get("query_sha256", ""))
    ):
        raise HierarchicalError("RecallTrace identity is invalid")
    if trace.get("mode") not in {"hierarchical-file-git", "flat-file-git-fallback"}:
        raise HierarchicalError("RecallTrace mode is invalid")
    forbidden_keys = {
        "content", "body", "raw_chat", "hook_payload", "credential", "secret",
        "session_id", "turn_id", "home_path", "summary", "overview",
    }

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            if any(str(key).lower() in forbidden_keys for key in value):
                raise HierarchicalError("RecallTrace contains forbidden content")
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)
        elif isinstance(value, float) and not math.isfinite(value):
            raise HierarchicalError("RecallTrace contains a non-finite score")

    inspect(trace)
    if not isinstance(trace["root_selection"], list) or not isinstance(trace["expansions"], list):
        raise HierarchicalError("RecallTrace navigation fields must be arrays")
    for root in trace["root_selection"]:
        if (
            not isinstance(root, dict)
            or set(root) != {"uri", "score"}
            or not isinstance(root["uri"], str)
            or not root["uri"].startswith("opc://")
            or isinstance(root["score"], bool)
            or not isinstance(root["score"], int)
            or root["score"] < 0
        ):
            raise HierarchicalError("RecallTrace root selection is invalid")
    for expansion in trace["expansions"]:
        if not isinstance(expansion, dict) or set(expansion) not in (
            {"uri", "score", "action"},
            {"uri", "leaf_id", "score", "action"},
        ):
            raise HierarchicalError("RecallTrace expansion fields are invalid")
        if (
            not isinstance(expansion["uri"], str)
            or not expansion["uri"].startswith("opc://")
            or isinstance(expansion["score"], bool)
            or not isinstance(expansion["score"], int)
            or expansion["score"] < 0
        ):
            raise HierarchicalError("RecallTrace expansion is invalid")
        if "leaf_id" in expansion:
            _portable(expansion["leaf_id"], "RecallTrace expanded leaf")
            if expansion["action"] not in {"selected", "discarded"}:
                raise HierarchicalError("RecallTrace leaf action is invalid")
        elif expansion["action"] != "expanded":
            raise HierarchicalError("RecallTrace node action is invalid")
    if not isinstance(trace["discards"], list):
        raise HierarchicalError("RecallTrace discards must be an array")
    for discard in trace["discards"]:
        if not isinstance(discard, dict) or set(discard) not in (
            {"reason_codes"},
            {"record_id", "reason_codes"},
        ):
            raise HierarchicalError("RecallTrace discard fields are invalid")
        if "record_id" in discard:
            _portable(discard["record_id"], "RecallTrace discarded leaf")
        reasons = discard["reason_codes"]
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(item, str) or not item for item in reasons)
            or reasons != sorted(set(reasons))
        ):
            raise HierarchicalError("RecallTrace discard reasons are invalid")
    if (
        not isinstance(trace["fallbacks"], list)
        or any(
            item not in {"flat-file-git", "provider-error-file-hierarchy"}
            for item in trace["fallbacks"]
        )
    ):
        raise HierarchicalError("RecallTrace fallbacks are invalid")
    for field in ("canonical_read_count", "injected_token_cost"):
        value = trace[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HierarchicalError("RecallTrace aggregate is invalid")
    if trace["canonical_read_count"] > MAX_CANONICAL_READS:
        raise HierarchicalError("RecallTrace canonical read limit was exceeded")
    if trace["injected_token_cost"] > MAX_BUDGET_TOKENS:
        raise HierarchicalError("RecallTrace injected token cost is invalid")
    if not isinstance(trace["final_leaves"], list):
        raise HierarchicalError("RecallTrace final leaves must be an array")
    if len(trace["final_leaves"]) != len(set(trace["final_leaves"])):
        raise HierarchicalError("RecallTrace final leaves must be unique")
    for record_id in trace["final_leaves"]:
        _portable(record_id, "RecallTrace final leaf")


class HierarchicalRecall:
    """Governed L0/L1 navigation with bounded L2 canonical injection."""

    def __init__(
        self,
        backend: FileGitBackend,
        data_root: Path | str,
        *,
        provider: RecallProvider | None = None,
        provider_enabled: bool = False,
        timeout_seconds: float = 3.0,
    ):
        self.backend = backend
        self.index = HierarchicalIndex(backend, data_root)
        self.provider = provider
        self.provider_enabled = bool(provider_enabled and provider is not None)
        self.timeout_seconds = max(0.01, float(timeout_seconds))

    def _flat_fallback(self, query: str, **values: Any) -> dict[str, Any]:
        limit = int(values["limit"])
        context = self.backend.query_context(
            query,
            memory_type=values.get("memory_type"),
            project_id=values.get("project_id"),
            role=values.get("role"),
            applicability=values.get("applicability"),
            allowed_sensitivity=values.get("allowed_sensitivity"),
            at=values.get("at"),
            limit=limit,
        )
        packet = self._packet_from_records(
            query=query,
            records=context["records"],
            conflicts=context["conflicts"],
            budget_tokens=int(values["budget_tokens"]),
            pre_omissions=context["omissions"],
            mode="flat-file-git-fallback",
        )
        trace = {
            "schema_version": TRACE_VERSION,
            "query_sha256": sha256_bytes(query.encode("utf-8")),
            "mode": "flat-file-git-fallback",
            "root_selection": [],
            "expansions": [],
            "discards": [{"reason_codes": ["derived_index_unavailable_or_stale"]}],
            "fallbacks": ["flat-file-git"],
            "final_leaves": [record["id"] for record in context["records"]],
            "canonical_read_count": len(context["records"]),
            "injected_token_cost": packet["budget"]["used_tokens"],
        }
        validate_context_packet(packet)
        validate_recall_trace(trace)
        return {"context_packet": packet, "recall_trace": trace}

    def _packet_from_records(
        self,
        *,
        query: str,
        records: Sequence[Mapping[str, Any]],
        conflicts: Sequence[Mapping[str, Any]],
        budget_tokens: int,
        pre_omissions: Sequence[Mapping[str, Any]] = (),
        mode: str,
    ) -> dict[str, Any]:
        packet: dict[str, Any] = {
            "schema_version": PACKET_VERSION,
            "query_sha256": sha256_bytes(query.encode("utf-8")),
            "mode": mode,
            "facts": [],
            "decisions": [],
            "experiences": [],
            "procedures": [],
            "citations": [],
            "conflicts": [dict(item) for item in conflicts],
            "budget": {"limit_tokens": budget_tokens, "used_tokens": 0, "remaining_tokens": budget_tokens},
            "omitted_summary": {"count": len(pre_omissions), "reason_codes": sorted({reason for item in pre_omissions for reason in item.get("reason_codes", [])})},
        }
        used = 0
        omitted = list(packet["omitted_summary"]["reason_codes"])
        for record in records:
            content = str(record.get("content", record.get("lesson", "")))
            citation = record.get("_citation")
            if not isinstance(citation, Mapping):
                continue
            item_cost = _token_cost(content) + _token_cost(json.dumps(citation, sort_keys=True))
            if used + item_cost > budget_tokens:
                packet["omitted_summary"]["count"] += 1
                omitted.append("budget_exhausted")
                continue
            item = {"record_id": record["id"], "content": content, "citation": dict(citation), "token_cost": item_cost}
            packet[_bucket(record)].append(item)
            packet["citations"].append(dict(citation))
            used += item_cost
        packet["budget"] = {
            "limit_tokens": budget_tokens,
            "used_tokens": used,
            "remaining_tokens": budget_tokens - used,
        }
        packet["omitted_summary"]["reason_codes"] = sorted(set(omitted))
        return packet

    def query(
        self,
        query: str,
        *,
        project_id: str | None = None,
        role: str | None = None,
        memory_type: str | None = None,
        applicability: Mapping[str, str] | None = None,
        allowed_sensitivity: Sequence[str] | None = None,
        at: str | None = None,
        limit: int = 5,
        budget_tokens: int = 2000,
        canonical_read_limit: int = 10,
    ) -> dict[str, Any]:
        if isinstance(budget_tokens, bool) or not 1 <= budget_tokens <= MAX_BUDGET_TOKENS:
            raise HierarchicalError("budget_tokens is outside the contract")
        if isinstance(canonical_read_limit, bool) or not 1 <= canonical_read_limit <= MAX_CANONICAL_READS:
            raise HierarchicalError("canonical_read_limit is outside the contract")
        try:
            context_values, sensitivities = validate_query_context(
                project_id=project_id,
                role=role,
                applicability=applicability,
                allowed_sensitivity=allowed_sensitivity,
                limit=limit,
            )
        except GovernanceError as exc:
            raise HierarchicalError(str(exc)) from exc
        try:
            if at is None:
                evaluation_time = datetime.now(timezone.utc)
            else:
                parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError
                evaluation_time = parsed.astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError) as exc:
            raise HierarchicalError("at must be timezone aware") from exc
        fallback_values = {
            "project_id": project_id,
            "role": role,
            "memory_type": memory_type,
            "applicability": context_values,
            "allowed_sensitivity": sensitivities,
            "at": at,
            "limit": limit,
            "budget_tokens": budget_tokens,
        }
        try:
            index = self.index.load()
        except (HierarchicalError, OpcMemoryError, OSError):
            return self._flat_fallback(query, **fallback_values)
        head = _head(self.backend.root)
        if not head or index.get("canonical_head") != head:
            return self._flat_fallback(query, **fallback_values)

        leaves = {str(item.get("node_id")): item for item in index["leaves"] if isinstance(item, dict)}
        base_reasons: dict[str, set[str]] = {}
        for record_id, leaf in leaves.items():
            reasons: set[str] = set()
            if leaf.get("status") != "approved":
                reasons.add(str(leaf.get("status") or "status_invalid"))
            scope = leaf.get("scope")
            if scope == "global":
                if leaf.get("project_id") is not None:
                    reasons.add("project_scope_mismatch")
            elif scope != "project" or not project_id or leaf.get("project_id") != project_id:
                reasons.add("project_scope_mismatch")
            if leaf.get("source_commit") != head or not SHA256.fullmatch(str(leaf.get("content_sha256", ""))):
                reasons.add("stale_provenance")
            if leaf.get("sensitivity") not in sensitivities:
                reasons.add("sensitivity_not_authorized")
            if memory_type and leaf.get("knowledge_type") != memory_type:
                reasons.add("knowledge_type_not_applicable")
            try:
                shadow_record = {
                    "schema_version": 2,
                    "type": leaf.get("knowledge_type"),
                    "applicability": leaf.get("applicability"),
                }
                reasons.update(
                    applicability_reasons(
                        shadow_record,
                        role=role,
                        knowledge_type=memory_type,
                        context=context_values,
                        at=evaluation_time,
                    )
                )
            except GovernanceError:
                reasons.add("applicability_invalid")
            base_reasons[record_id] = reasons

        eligible = {record_id for record_id, reasons in base_reasons.items() if not reasons}
        relation_reasons: dict[str, set[str]] = {record_id: set() for record_id in leaves}
        relations: list[tuple[str, str, str]] = []
        edges: dict[str, set[str]] = {}
        for source_id in sorted(eligible):
            value = leaves[source_id].get("relations")
            if not isinstance(value, list):
                relation_reasons[source_id].add("relations_invalid")
                continue
            for relation in value:
                if not isinstance(relation, Mapping) or not relation_applies(relation, project_id):
                    continue
                target = str(relation.get("target_id", ""))
                kind = str(relation.get("kind", ""))
                if target not in leaves:
                    relation_reasons[source_id].add("relation_target_missing")
                    continue
                if target not in eligible:
                    if kind in {"superseded_by", "invalidated_by"}:
                        relation_reasons[source_id].add("relation_target_ineligible")
                    continue
                relations.append((source_id, target, kind))
                if kind != "conflicts":
                    edges.setdefault(source_id, set()).add(target)
        for record_id in relation_cycles(edges):
            relation_reasons[record_id].add("relation_cycle")
        for source, target, kind in relations:
            if relation_reasons[source] or relation_reasons[target]:
                continue
            if kind in {"supersedes", "invalidates"}:
                relation_reasons[target].add("superseded" if kind == "supersedes" else "invalidated")
            elif kind in {"superseded_by", "invalidated_by"}:
                relation_reasons[source].add("superseded" if kind == "superseded_by" else "invalidated")
        conflict_pairs = {
            tuple(sorted((source, target)))
            for source, target, kind in relations
            if kind == "conflicts" and not relation_reasons[source] and not relation_reasons[target]
        }
        conflicted = {item for pair in conflict_pairs for item in pair}
        for item in conflicted:
            relation_reasons[item].add("unresolved_conflict")
        eligible = {
            record_id for record_id in eligible if not relation_reasons[record_id]
        }

        trace: dict[str, Any] = {
            "schema_version": TRACE_VERSION,
            "query_sha256": sha256_bytes(query.encode("utf-8")),
            "mode": "hierarchical-file-git",
            "root_selection": [],
            "expansions": [],
            "discards": [],
            "fallbacks": [],
            "final_leaves": [],
            "canonical_read_count": 0,
            "injected_token_cost": 0,
        }
        for record_id in sorted(leaves):
            reasons = sorted(base_reasons[record_id] | relation_reasons[record_id])
            if reasons:
                trace["discards"].append({"record_id": record_id, "reason_codes": reasons})

        root_uris = ["opc://global"]
        if project_id:
            root_uris.append(f"opc://projects/{project_id}")
        node_by_uri = {str(node.get("uri")): node for node in index["nodes"] if isinstance(node, dict)}
        for root_uri in root_uris:
            node = node_by_uri.get(root_uri)
            if node:
                score = _navigation_score(query, str(node.get("overview", "")))
                trace["root_selection"].append({"uri": root_uri, "score": score})

        candidates = [leaves[record_id] for record_id in eligible]
        provider_ids: set[str] = set()
        if self.provider_enabled and query.strip():
            try:
                hits = _call_with_timeout(
                    lambda: self.provider.search(query, max(limit, canonical_read_limit)),
                    self.timeout_seconds,
                )
                if not isinstance(hits, list):
                    raise HierarchicalError("provider result is not a list")
                for hit in hits:
                    if not isinstance(hit, Mapping):
                        continue
                    metadata = hit.get("metadata")
                    if isinstance(metadata, Mapping):
                        candidate_id = metadata.get("record_id")
                        if isinstance(candidate_id, str) and candidate_id in eligible:
                            provider_ids.add(candidate_id)
                        elif isinstance(candidate_id, str):
                            try:
                                safe_candidate_id = _portable(candidate_id, "provider record id")
                            except HierarchicalError:
                                trace["discards"].append(
                                    {"reason_codes": ["provider_candidate_invalid"]}
                                )
                            else:
                                trace["discards"].append({"record_id": safe_candidate_id, "reason_codes": ["provider_disagreement_or_ineligible"]})
            except Exception:
                trace["fallbacks"].append("provider-error-file-hierarchy")

        # Expand namespace -> type directory -> leaf through one deterministic
        # priority queue. Only already hard-filtered leaves participate; L0/L1
        # text is navigation metadata and is never copied into the packet.
        eligible_leaves = {str(item["node_id"]): item for item in candidates}
        queue: list[tuple[int, int, str, str, Mapping[str, Any]]] = []
        for root_uri in root_uris:
            node = node_by_uri.get(root_uri)
            if node:
                score = _navigation_score(query, str(node.get("overview", "")))
                heapq.heappush(queue, (-score, 0, root_uri, "node", node))
        selected: list[Mapping[str, Any]] = []
        expansion_limit = min(
            len(index["nodes"]) + len(eligible_leaves),
            MAX_RECORDS * 2,
        )
        expansion_count = 0
        while (
            queue
            and len(selected) < min(canonical_read_limit, max(limit * 2, limit))
            and expansion_count < expansion_limit
        ):
            negative, _, identity, kind, item = heapq.heappop(queue)
            score = -negative
            expansion_count += 1
            if kind == "node":
                trace["expansions"].append(
                    {"uri": identity, "score": score, "action": "expanded"}
                )
                child_uris = item.get("child_uris", [])
                child_ids = item.get("child_ids", [])
                if isinstance(child_uris, list):
                    for child_uri in child_uris:
                        child = node_by_uri.get(str(child_uri))
                        if not child:
                            continue
                        child_score = _navigation_score(
                            query, str(child.get("overview", ""))
                        )
                        heapq.heappush(
                            queue,
                            (-child_score, 1, str(child_uri), "node", child),
                        )
                if isinstance(child_ids, list):
                    for record_id in child_ids:
                        leaf = eligible_leaves.get(str(record_id))
                        if not leaf:
                            continue
                        leaf_score = _navigation_score(
                            query,
                            " ".join(
                                [
                                    str(leaf.get("summary", "")),
                                    str(leaf.get("knowledge_type", "")),
                                    json.dumps(leaf.get("metadata", {}), sort_keys=True),
                                ]
                            ),
                            leaf.get("keywords", []),
                        )
                        if record_id in provider_ids:
                            leaf_score += 1
                        heapq.heappush(
                            queue,
                            (-leaf_score, 2, str(record_id), "leaf", leaf),
                        )
                continue
            record_id = identity
            trace["expansions"].append(
                {
                    "uri": str(item["parent_uri"]),
                    "leaf_id": record_id,
                    "score": score,
                    "action": "selected" if score > 0 or record_id in provider_ids else "discarded",
                }
            )
            if query.strip() and score <= 0 and record_id not in provider_ids:
                trace["discards"].append(
                    {"record_id": record_id, "reason_codes": ["navigation_score_zero"]}
                )
                continue
            selected.append(item)

        canonical_records: list[dict[str, Any]] = []
        for leaf in selected:
            if len(canonical_records) >= limit:
                break
            try:
                record = self.backend.read_authoritative(
                    source_path=str(leaf["source_path"]),
                    content_hash=str(leaf["content_sha256"]),
                    source_commit=str(leaf["source_commit"]),
                    approved_only=True,
                )
                trace["canonical_read_count"] += 1
                if record.get("id") != leaf.get("node_id") or not self.backend._scope_matches(record, project_id):
                    raise HierarchicalError("canonical scope or identity changed")
                reasons = applicability_reasons(
                    record,
                    role=role,
                    knowledge_type=memory_type,
                    context=context_values,
                    at=evaluation_time,
                )
                if reasons or record.get("sensitivity", "internal") not in sensitivities:
                    raise HierarchicalError("canonical applicability changed")
                provenance = self.backend.source_metadata(str(leaf["source_path"]))
                if provenance.get("source_commit") != head or provenance.get("content_hash") != leaf.get("content_sha256"):
                    raise HierarchicalError("canonical provenance changed")
                result = dict(record)
                result["_citation"] = canonical_citation(record, provenance)
                result["_recall_source"] = "hierarchical-file"
                result["_authority"] = "file-git"
                canonical_records.append(result)
            except (GovernanceError, HierarchicalError, OpcMemoryError, OSError):
                trace["discards"].append({"record_id": str(leaf.get("node_id")), "reason_codes": ["l2_revalidation_failed"]})

        conflicts: list[dict[str, Any]] = []
        for left, right in sorted(conflict_pairs):
            citations: list[dict[str, Any]] = []
            for record_id in (left, right):
                if trace["canonical_read_count"] >= MAX_CANONICAL_READS:
                    citations = []
                    break
                leaf = leaves[record_id]
                try:
                    record = self.backend.read_authoritative(
                        source_path=str(leaf["source_path"]),
                        content_hash=str(leaf["content_sha256"]),
                        source_commit=str(leaf["source_commit"]),
                        approved_only=True,
                    )
                    trace["canonical_read_count"] += 1
                    citations.append(
                        canonical_citation(record, self.backend.source_metadata(str(leaf["source_path"])))
                    )
                except (GovernanceError, OpcMemoryError, OSError):
                    citations = []
                    break
            if len(citations) == 2:
                conflicts.append({"reason_code": "unresolved_conflict", "citations": citations})

        packet = self._packet_from_records(
            query=query,
            records=canonical_records,
            conflicts=conflicts,
            budget_tokens=budget_tokens,
            mode="hierarchical-file-git",
        )
        trace["final_leaves"] = [record["id"] for record in canonical_records]
        trace["injected_token_cost"] = packet["budget"]["used_tokens"]
        validate_context_packet(packet)
        validate_recall_trace(trace)
        return {"context_packet": packet, "recall_trace": trace}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-root", required=True)
    parser.add_argument("--data-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("index-preview")
    build = commands.add_parser("index-build")
    build.add_argument("--approval-token", required=True)
    commands.add_parser("index-delete-preview")
    delete = commands.add_parser("index-delete")
    delete.add_argument("--approval-token", required=True)
    query = commands.add_parser("query")
    query.add_argument("text")
    query.add_argument("--project-id")
    query.add_argument("--role")
    query.add_argument("--type", dest="memory_type")
    query.add_argument("--allow-sensitivity", action="append")
    query.add_argument("--applicability", action="append", default=[])
    query.add_argument("--at")
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--budget-tokens", type=int, default=2000)
    query.add_argument("--canonical-read-limit", type=int, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        backend = FileGitBackend(args.knowledge_root)
        index = HierarchicalIndex(backend, args.data_root)
        if args.command == "status":
            result: Any = index.status()
        elif args.command == "index-preview":
            result = index.preview()
        elif args.command == "index-build":
            result = index.build(approval_token=args.approval_token)
        elif args.command == "index-delete-preview":
            result = index.delete_preview()
        elif args.command == "index-delete":
            result = index.delete(approval_token=args.approval_token)
        else:
            applicability: dict[str, str] = {}
            for pair in args.applicability:
                if "=" not in pair:
                    raise HierarchicalError("applicability values must be key=value")
                key, value = pair.split("=", 1)
                applicability[_portable(key, "applicability key")] = _portable(value, "applicability value")
            result = HierarchicalRecall(backend, args.data_root).query(
                args.text,
                project_id=args.project_id,
                role=args.role,
                memory_type=args.memory_type,
                applicability=applicability,
                allowed_sensitivity=args.allow_sensitivity,
                at=args.at,
                limit=args.limit,
                budget_tokens=args.budget_tokens,
                canonical_read_limit=args.canonical_read_limit,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (HierarchicalError, GovernanceError, OpcMemoryError, OSError, ValueError) as exc:
        print(f"OPC_HIERARCHICAL_ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
