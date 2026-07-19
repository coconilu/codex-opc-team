#!/usr/bin/env python3
"""Run the versioned OPC evaluation baseline against the real File/Git backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
MEMORY_SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(MEMORY_SCRIPTS))

import opc_memory  # noqa: E402


DEFAULT_SUITE = ROOT / "evaluation" / "fixtures" / "synthetic-suite.v1.json"
DEFAULT_CONTRACT = ROOT / "evaluation" / "contracts" / "baseline-contract.v1.json"
DEFAULT_RESULT = ROOT / "evaluation" / "baselines" / "file-git-no-enhancement.v1.json"
DEFAULT_REPORT = ROOT / "evaluation" / "baselines" / "file-git-no-enhancement.v1.md"
CONTRACT_VERSION = "opc-evaluation-contract-v1"
RESULT_SCHEMA_VERSION = "opc-evaluation-result-v1"
BASELINE_ID = "file-git-no-enhancement-v1"
CONTRACT_METRICS = {
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


class EvaluationError(RuntimeError):
    """Raised when evaluation input or evidence violates the contract."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON input must be an object: {path}")
    return value


def _contract_sha256(path: Path = DEFAULT_CONTRACT) -> str:
    raw = path.read_bytes()
    contract = _load_object(path)
    if contract.get("contract_version") != CONTRACT_VERSION:
        raise EvaluationError("evaluation contract version does not match the runner")
    if contract.get("result_schema_version") != RESULT_SCHEMA_VERSION:
        raise EvaluationError("evaluation contract result schema does not match the runner")
    if contract.get("baseline_id") != BASELINE_ID:
        raise EvaluationError("evaluation contract baseline id does not match the runner")
    metrics = contract.get("metrics")
    if not isinstance(metrics, list) or any(not isinstance(item, dict) for item in metrics):
        raise EvaluationError("evaluation contract metrics must be an array of objects")
    ids = [item.get("id") for item in metrics]
    if len(ids) != len(set(ids)) or set(ids) != CONTRACT_METRICS:
        raise EvaluationError("evaluation contract metric ids do not match the runner")
    required_metric_fields = {
        "id",
        "category",
        "numerator",
        "denominator",
        "undefined_when",
        "direction",
        "threshold",
        "pass_fail_interpretation",
        "confounders",
    }
    for item in metrics:
        if set(item) != required_metric_fields or not isinstance(item["confounders"], list):
            raise EvaluationError(f"metric contract is incomplete: {item.get('id', '<unknown>')}")
    return hashlib.sha256(raw).hexdigest()


def _require_keys(
    value: Mapping[str, Any], *, required: set[str], allowed: set[str], label: str
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - allowed)
    if missing:
        raise EvaluationError(f"{label} missing fields: {', '.join(missing)}")
    if extra:
        raise EvaluationError(f"{label} has unsupported fields: {', '.join(extra)}")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvaluationError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: Any, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise EvaluationError(f"{label} must be a number >= {minimum}")
    return float(value)


def _identifier(value: Any, label: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise EvaluationError(f"{label} must start with {prefix!r}")
    tail = value[len(prefix) :]
    if not tail or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in tail):
        raise EvaluationError(f"{label} must use lowercase letters, digits, or hyphens")
    return value


def _pilot_identifier(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 18 or not value.startswith("pilot-"):
        raise EvaluationError("pilot_id must be pilot- followed by 12 hexadecimal characters")
    if any(ch not in "0123456789abcdef" for ch in value[6:]):
        raise EvaluationError("pilot_id must be pilot- followed by 12 hexadecimal characters")
    return value


def _ratio(numerator: int, denominator: int, label: str) -> float:
    if denominator <= 0:
        raise EvaluationError(f"{label} denominator must be greater than zero")
    return round(numerator / denominator, 4)


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise EvaluationError("a percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, int(len(ordered) * percentile + 0.9999999999))
    return ordered[min(rank, len(ordered)) - 1]


def _median(values: Sequence[float]) -> float:
    if not values:
        raise EvaluationError("a median requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2, 4)


def _metric(numerator: int, denominator: int, *, unit: str) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": _ratio(numerator, denominator, unit),
        "unit": unit,
    }


def _git(root: Path, *args: str, env: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        text=True,
        capture_output=True,
        env=dict(env),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise EvaluationError(f"synthetic Git setup failed: {detail}")
    return result.stdout.strip()


def _git_environment(root: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "HOME": str(root / "synthetic-home"),
        "USERPROFILE": str(root / "synthetic-home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(root / "synthetic-gitconfig"),
        "GIT_AUTHOR_NAME": "OPC Evaluation Fixture",
        "GIT_AUTHOR_EMAIL": "evaluation-fixture@example.invalid",
        "GIT_COMMITTER_NAME": "OPC Evaluation Fixture",
        "GIT_COMMITTER_EMAIL": "evaluation-fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z",
    }
    (root / "synthetic-home").mkdir()
    (root / "synthetic-gitconfig").write_text("", encoding="utf-8")
    return env


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _validate_record(wrapper: Mapping[str, Any], label: str) -> dict[str, Any]:
    _require_keys(
        wrapper,
        required={"record", "post_commit"},
        allowed={"record", "post_commit"},
        label=label,
    )
    if wrapper["post_commit"] not in {"none", "change_content"}:
        raise EvaluationError(f"{label}.post_commit is unsupported")
    record = wrapper["record"]
    if not isinstance(record, dict):
        raise EvaluationError(f"{label}.record must be an object")
    required = {
        "schema_version",
        "id",
        "type",
        "summary",
        "content",
        "keywords",
        "metadata",
        "scope",
        "owner",
        "evidence",
        "confidence",
        "status",
        "created_at",
        "updated_at",
    }
    allowed = required | {
        "project_id",
        "approved_by",
        "approved_at",
        "validation",
        "rejected_by",
        "rejected_at",
        "rejection_reason",
        "obsolete_at",
        "obsolete_reason",
        "superseded_by",
    }
    _require_keys(record, required=required, allowed=allowed, label=f"{label}.record")
    if record["schema_version"] != 1:
        raise EvaluationError(f"{label}.record schema_version must be 1")
    _identifier(record["id"], f"{label}.record.id", "exp-syn-")
    if record["type"] not in {"decision", "preference", "procedure", "lesson"}:
        raise EvaluationError(f"{label}.record.type is unsupported")
    if record["status"] not in {"candidate", "approved", "rejected", "obsolete"}:
        raise EvaluationError(f"{label}.record.status is unsupported")
    if record["scope"] not in {"global", "project"}:
        raise EvaluationError(f"{label}.record.scope is unsupported")
    if record["scope"] == "global" and "project_id" in record:
        raise EvaluationError(f"{label}.record global scope cannot contain project_id")
    if record["scope"] == "project":
        _identifier(record.get("project_id"), f"{label}.record.project_id", "project-syn-")
    metadata = record["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"role", "conflict_group"}:
        raise EvaluationError(f"{label}.record.metadata must contain only role and conflict_group")
    if metadata["role"] not in {"all", "manager", "developer", "qa", "curator"}:
        raise EvaluationError(f"{label}.record.metadata.role is unsupported")
    if metadata["conflict_group"] is not None:
        _identifier(
            metadata["conflict_group"],
            f"{label}.record.metadata.conflict_group",
            "conflict-syn-",
        )
    return dict(record)


def _prepare_knowledge(
    workspace: Path, wrappers: Sequence[Mapping[str, Any]]
) -> tuple[opc_memory.FileGitBackend, dict[str, dict[str, Any]], str]:
    knowledge = workspace / "synthetic-knowledge"
    knowledge.mkdir()
    env = _git_environment(workspace)
    _git(knowledge, "init", "-b", "main", env=env)
    _git(knowledge, "commit", "--allow-empty", "-m", "synthetic empty baseline", env=env)
    empty_commit = _git(knowledge, "rev-parse", "HEAD", env=env)
    known: dict[str, dict[str, Any]] = {}
    for index, wrapper in enumerate(wrappers):
        record = _validate_record(wrapper, f"records[{index}]")
        record_id = record["id"]
        if record_id in known:
            raise EvaluationError(f"duplicate synthetic record id: {record_id}")
        known[record_id] = {"record": record, "post_commit": wrapper["post_commit"]}
        relative = opc_memory.STATUS_DIRS[record["status"]]
        _write_json(knowledge / relative / f"{record_id}.json", record)
    _git(knowledge, "add", "--", ".", env=env)
    _git(knowledge, "commit", "-m", "synthetic File Git evaluation records", env=env)
    for wrapper in known.values():
        if wrapper["post_commit"] == "change_content":
            record = dict(wrapper["record"])
            record["content"] = f"{record['content']} changed-after-commit"
            relative = opc_memory.STATUS_DIRS[record["status"]]
            _write_json(knowledge / relative / f"{record['id']}.json", record)
    return opc_memory.FileGitBackend(knowledge), known, empty_commit


def _query_reason(
    wrapper: Mapping[str, Any], query: Mapping[str, Any], conflict_counts: Mapping[str, int]
) -> str:
    record = wrapper["record"]
    if record["scope"] == "project" and record.get("project_id") != query["project_id"]:
        return "scope_leakage"
    if record["status"] == "obsolete" or wrapper["post_commit"] != "none":
        return "stale_or_obsolete"
    if record["status"] != "approved":
        return "unapproved_state"
    if record["type"] != query["knowledge_type"]:
        return "type_mismatch"
    if record["metadata"]["role"] not in {"all", query["role"]}:
        return "role_mismatch"
    if opc_memory.FileGitBackend._score(record, query["text"]) <= 0:
        return "query_mismatch"
    group = record["metadata"]["conflict_group"]
    if group and conflict_counts.get(group, 0) > 1:
        return "unresolved_conflict"
    return "eligible"


def _validate_query(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    required = {"text", "project_id", "role", "knowledge_type"}
    _require_keys(value, required=required, allowed=required, label=label)
    if not isinstance(value["text"], str) or not value["text"].strip():
        raise EvaluationError(f"{label}.text must be non-empty")
    _identifier(value["project_id"], f"{label}.project_id", "project-syn-")
    if value["role"] not in {"manager", "developer", "qa", "curator"}:
        raise EvaluationError(f"{label}.role is unsupported")
    if value["knowledge_type"] not in {"decision", "preference", "procedure", "lesson"}:
        raise EvaluationError(f"{label}.knowledge_type is unsupported")
    return dict(value)


def _score_synthetic(
    suite: Mapping[str, Any], source_sha256: str, contract_sha256: str | None = None
) -> dict[str, Any]:
    contract_sha256 = contract_sha256 or _contract_sha256()
    required = {
        "schema_version",
        "contract_version",
        "dataset_id",
        "mode",
        "records",
        "provenance_probes",
        "cases",
    }
    _require_keys(suite, required=required, allowed=required, label="synthetic suite")
    if suite["schema_version"] != "opc-synthetic-suite-v1":
        raise EvaluationError("unsupported synthetic suite schema_version")
    if suite["contract_version"] != CONTRACT_VERSION:
        raise EvaluationError("synthetic suite contract_version does not match the runner")
    if suite["mode"] != "public-synthetic":
        raise EvaluationError("synthetic suite mode must be public-synthetic")
    _identifier(suite["dataset_id"], "dataset_id", "dataset-syn-")
    if not isinstance(suite["records"], list) or not isinstance(suite["cases"], list):
        raise EvaluationError("records and cases must be arrays")
    if not suite["records"] or not suite["cases"]:
        raise EvaluationError("records and cases must not be empty")

    with tempfile.TemporaryDirectory(prefix="opc-evaluation-") as temporary:
        backend, known, empty_commit = _prepare_knowledge(Path(temporary), suite["records"])
        probes = suite["provenance_probes"]
        if not isinstance(probes, list) or not probes:
            raise EvaluationError("provenance_probes must be a non-empty array")
        probe_results: dict[str, str] = {}
        for index, probe in enumerate(probes):
            label = f"provenance_probes[{index}]"
            if not isinstance(probe, dict):
                raise EvaluationError(f"{label} must be an object")
            required_probe = {"probe_id", "record_id", "failure"}
            _require_keys(probe, required=required_probe, allowed=required_probe, label=label)
            probe_id = _identifier(probe["probe_id"], f"{label}.probe_id", "probe-syn-")
            record_id = probe["record_id"]
            if record_id not in known:
                raise EvaluationError(f"{label} references unknown record")
            record = known[record_id]["record"]
            source = f"{opc_memory.STATUS_DIRS[record['status']]}/{record_id}.json"
            metadata = backend.source_metadata(source)
            try:
                if probe["failure"] == "stale_hash":
                    backend.read_authoritative(
                        source_path=source,
                        content_hash="0" * 64,
                        source_commit=metadata.get("source_commit"),
                    )
                elif probe["failure"] == "stale_commit":
                    backend.read_authoritative(
                        source_path=source,
                        content_hash=metadata["content_hash"],
                        source_commit=empty_commit,
                    )
                else:
                    raise EvaluationError(f"{label}.failure is unsupported")
            except opc_memory.StaleSourceError:
                probe_results[probe_id] = "rejected"
            else:
                raise EvaluationError(f"{probe_id} did not fail closed")

        totals = {
            "manager_interventions": 0,
            "eligible_manager_decisions": 0,
            "known_defects": 0,
            "qa_caught_defects": 0,
            "rework_loops": 0,
            "valid_reuse_opportunities": 0,
            "valid_reuses": 0,
            "accepted_recalls": 0,
            "false_recall_acceptances": 0,
            "scope_leakage_acceptances": 0,
            "stale_obsolete_acceptances": 0,
        }
        context_tokens: list[float] = []
        latency_ms: list[float] = []
        case_ids: set[str] = set()
        case_evidence: list[dict[str, Any]] = []
        for index, case in enumerate(suite["cases"]):
            label = f"cases[{index}]"
            if not isinstance(case, dict):
                raise EvaluationError(f"{label} must be an object")
            required_case = {"case_id", "query", "observed"}
            _require_keys(case, required=required_case, allowed=required_case, label=label)
            case_id = _identifier(case["case_id"], f"{label}.case_id", "case-syn-")
            if case_id in case_ids:
                raise EvaluationError(f"duplicate case_id: {case_id}")
            case_ids.add(case_id)
            query = _validate_query(case["query"], f"{label}.query")

            prelim: dict[str, str] = {}
            conflict_counts: dict[str, int] = {}
            for record_id, wrapper in known.items():
                reason = _query_reason(wrapper, query, {})
                prelim[record_id] = reason
                if reason == "eligible":
                    group = wrapper["record"]["metadata"]["conflict_group"]
                    if group:
                        conflict_counts[group] = conflict_counts.get(group, 0) + 1
            reasons = {
                record_id: _query_reason(wrapper, query, conflict_counts)
                for record_id, wrapper in known.items()
            }
            totals["valid_reuse_opportunities"] += sum(
                1 for reason in reasons.values() if reason == "eligible"
            )

            hits = backend.query(
                query["text"],
                approved_only=True,
                memory_type=query["knowledge_type"],
                metadata={"role": query["role"]},
                project_id=query["project_id"],
                limit=100,
            )
            hit_ids = [str(hit["id"]) for hit in hits]
            if len(set(hit_ids)) != len(hit_ids):
                raise EvaluationError(f"{case_id} returned duplicate File/Git hits")
            for record_id in hit_ids:
                if record_id not in reasons:
                    raise EvaluationError(f"{case_id} returned unknown File/Git hit")
                totals["accepted_recalls"] += 1
                reason = reasons[record_id]
                if reason == "eligible":
                    totals["valid_reuses"] += 1
                else:
                    totals["false_recall_acceptances"] += 1
                    if reason == "scope_leakage":
                        totals["scope_leakage_acceptances"] += 1
                    if reason == "stale_or_obsolete":
                        totals["stale_obsolete_acceptances"] += 1
            case_evidence.append({"case_id": case_id, "file_git_hit_ids": hit_ids})

            observed = case["observed"]
            observed_keys = {
                "manager_interventions",
                "eligible_manager_decisions",
                "known_defects",
                "qa_caught_defects",
                "rework_loops",
                "context_tokens",
                "latency_ms",
            }
            if not isinstance(observed, dict):
                raise EvaluationError(f"{label}.observed must be an object")
            _require_keys(observed, required=observed_keys, allowed=observed_keys, label=f"{label}.observed")
            for key in observed_keys - {"latency_ms"}:
                value = _integer(observed[key], f"{label}.observed.{key}")
                if key in totals:
                    totals[key] += value
            if observed["manager_interventions"] > observed["eligible_manager_decisions"]:
                raise EvaluationError(f"{case_id} manager interventions exceed eligible decisions")
            if observed["qa_caught_defects"] > observed["known_defects"]:
                raise EvaluationError(f"{case_id} QA catches exceed known defects")
            context_tokens.append(float(observed["context_tokens"]))
            latency_ms.append(_number(observed["latency_ms"], f"{label}.observed.latency_ms"))

    return _build_result(
        dataset_id=suite["dataset_id"],
        mode=suite["mode"],
        contract_sha256=contract_sha256,
        source_sha256=source_sha256,
        task_count=len(suite["cases"]),
        totals=totals,
        context_tokens=context_tokens,
        latency_ms=latency_ms,
        provenance_probe_results=probe_results,
        case_evidence=case_evidence,
    )


def _build_result(
    *,
    dataset_id: str,
    mode: str,
    contract_sha256: str,
    source_sha256: str | None,
    task_count: int,
    totals: Mapping[str, int],
    context_tokens: Sequence[float],
    latency_ms: Sequence[float],
    provenance_probe_results: Mapping[str, str],
    case_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if mode == "public-synthetic":
        provenance_status = (
            "pass"
            if provenance_probe_results
            and all(value == "rejected" for value in provenance_probe_results.values())
            else "fail"
        )
        provenance_required = "all_rejected"
    else:
        provenance_status = "not_applicable"
        provenance_required = "not_applicable_private_aggregate"
    safety_pass = (
        totals["scope_leakage_acceptances"] == 0
        and totals["stale_obsolete_acceptances"] == 0
        and provenance_status in {"pass", "not_applicable"}
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": contract_sha256,
        "baseline_id": BASELINE_ID,
        "dataset_id": dataset_id,
        "mode": mode,
        "source_sha256": source_sha256,
        "task_count": task_count,
        "quality_interpretation": "baseline_only",
        "metrics": {
            "manager_intervention_rate": _metric(
                totals["manager_interventions"], totals["eligible_manager_decisions"], unit="ratio"
            ),
            "qa_catch_rate": _metric(totals["qa_caught_defects"], totals["known_defects"], unit="ratio"),
            "rework_loops_per_task": _metric(totals["rework_loops"], task_count, unit="loops_per_task"),
            "valid_knowledge_reuse_rate": _metric(
                totals["valid_reuses"], totals["valid_reuse_opportunities"], unit="ratio"
            ),
            "false_recall_rate": _metric(
                totals["false_recall_acceptances"], totals["accepted_recalls"], unit="ratio"
            ),
            "context_tokens_per_task": {
                "total": int(sum(context_tokens)),
                "denominator": task_count,
                "mean": round(sum(context_tokens) / task_count, 4),
                "median": _median(context_tokens),
                "p95_nearest_rank": int(_nearest_rank(context_tokens, 0.95)),
                "unit": "tokens",
            },
            "latency_ms": {
                "count": len(latency_ms),
                "mean": round(sum(latency_ms) / len(latency_ms), 4),
                "median": _median(latency_ms),
                "p95_nearest_rank": _nearest_rank(latency_ms, 0.95),
                "unit": "milliseconds",
                "tolerance_percent": 25,
            },
        },
        "safety_gates": {
            "scope_leakage_acceptances": {
                "value": totals["scope_leakage_acceptances"],
                "threshold": 0,
                "status": "pass" if totals["scope_leakage_acceptances"] == 0 else "fail",
            },
            "stale_obsolete_acceptances": {
                "value": totals["stale_obsolete_acceptances"],
                "threshold": 0,
                "status": "pass" if totals["stale_obsolete_acceptances"] == 0 else "fail",
            },
            "provenance_probes": {
                "value": dict(provenance_probe_results),
                "required": provenance_required,
                "status": provenance_status,
            },
        },
        "file_git_evidence": list(case_evidence),
        "overall_safety_status": "pass" if safety_pass else "fail",
    }


def _score_private_summary(
    summary: Mapping[str, Any], contract_sha256: str | None = None
) -> dict[str, Any]:
    contract_sha256 = contract_sha256 or _contract_sha256()
    required = {
        "schema_version",
        "contract_version",
        "pilot_id",
        "mode",
        "task_count",
        "counts",
        "context_tokens",
        "latency_ms",
    }
    _require_keys(summary, required=required, allowed=required, label="private pilot summary")
    if summary["schema_version"] != "opc-private-pilot-summary-v1":
        raise EvaluationError("unsupported private pilot summary schema_version")
    if summary["contract_version"] != CONTRACT_VERSION:
        raise EvaluationError("private pilot contract_version does not match the runner")
    if summary["mode"] != "private-aggregate":
        raise EvaluationError("private pilot mode must be private-aggregate")
    pilot_id = _pilot_identifier(summary["pilot_id"])
    task_count = _integer(summary["task_count"], "task_count", minimum=3)
    if task_count > 5:
        raise EvaluationError("private pilot task_count must be between 3 and 5")
    count_keys = {
        "manager_interventions",
        "eligible_manager_decisions",
        "known_defects",
        "qa_caught_defects",
        "rework_loops",
        "valid_reuse_opportunities",
        "valid_reuses",
        "accepted_recalls",
        "false_recall_acceptances",
        "scope_leakage_acceptances",
        "stale_obsolete_acceptances",
    }
    counts = summary["counts"]
    if not isinstance(counts, dict):
        raise EvaluationError("private pilot counts must be an object")
    _require_keys(counts, required=count_keys, allowed=count_keys, label="private pilot counts")
    totals = {key: _integer(counts[key], f"counts.{key}") for key in count_keys}
    for denominator in (
        "eligible_manager_decisions",
        "known_defects",
        "valid_reuse_opportunities",
        "accepted_recalls",
    ):
        if totals[denominator] == 0:
            raise EvaluationError(f"counts.{denominator} cannot be zero")
    if totals["manager_interventions"] > totals["eligible_manager_decisions"]:
        raise EvaluationError("manager interventions exceed eligible decisions")
    if totals["qa_caught_defects"] > totals["known_defects"]:
        raise EvaluationError("QA catches exceed known defects")
    if totals["valid_reuses"] > totals["valid_reuse_opportunities"]:
        raise EvaluationError("valid reuses exceed valid reuse opportunities")
    if totals["valid_reuses"] + totals["false_recall_acceptances"] != totals["accepted_recalls"]:
        raise EvaluationError("accepted recalls must equal valid plus false recall acceptances")
    if totals["scope_leakage_acceptances"] > totals["false_recall_acceptances"]:
        raise EvaluationError("scope leakage exceeds false recall acceptances")
    if totals["stale_obsolete_acceptances"] > totals["false_recall_acceptances"]:
        raise EvaluationError("stale/obsolete acceptance exceeds false recall acceptances")

    distribution_keys = {"total", "median", "p95_nearest_rank"}
    context = summary["context_tokens"]
    latency = summary["latency_ms"]
    if not isinstance(context, dict) or not isinstance(latency, dict):
        raise EvaluationError("context_tokens and latency_ms must be aggregate objects")
    _require_keys(context, required=distribution_keys, allowed=distribution_keys, label="context_tokens")
    _require_keys(latency, required=distribution_keys, allowed=distribution_keys, label="latency_ms")
    context_total = _integer(context["total"], "context_tokens.total", minimum=1)
    latency_total = _number(latency["total"], "latency_ms.total", minimum=0.0001)
    context_median = _number(context["median"], "context_tokens.median", minimum=0.0001)
    context_p95 = _number(context["p95_nearest_rank"], "context_tokens.p95_nearest_rank", minimum=0.0001)
    latency_median = _number(latency["median"], "latency_ms.median", minimum=0.0001)
    latency_p95 = _number(latency["p95_nearest_rank"], "latency_ms.p95_nearest_rank", minimum=0.0001)
    if context_median > context_p95 or latency_median > latency_p95:
        raise EvaluationError("aggregate medians cannot exceed p95")
    result = _build_result(
        dataset_id=pilot_id,
        mode="private-aggregate",
        contract_sha256=contract_sha256,
        source_sha256=None,
        task_count=task_count,
        totals=totals,
        context_tokens=[context_total / task_count] * task_count,
        latency_ms=[latency_total / task_count] * task_count,
        provenance_probe_results={},
        case_evidence=[],
    )
    result["metrics"]["context_tokens_per_task"]["median"] = context_median
    result["metrics"]["context_tokens_per_task"]["p95_nearest_rank"] = context_p95
    result["metrics"]["latency_ms"]["median"] = latency_median
    result["metrics"]["latency_ms"]["p95_nearest_rank"] = latency_p95
    return result


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _report_bytes(result: Mapping[str, Any]) -> bytes:
    metrics = result["metrics"]
    gates = result["safety_gates"]
    lines = [
        "# OPC evaluation baseline report",
        "",
        f"- Contract: `{result['contract_version']}`",
        f"- Contract SHA-256: `{result['contract_sha256']}`",
        f"- Baseline: `{result['baseline_id']}`",
        f"- Dataset: `{result['dataset_id']}`",
        f"- Mode: `{result['mode']}`",
        f"- Tasks: {result['task_count']}",
        f"- Safety: **{str(result['overall_safety_status']).upper()}**",
        "",
        "## Product outcomes",
        "",
        "| Metric | Numerator | Denominator | Value |",
        "|---|---:|---:|---:|",
    ]
    for key in (
        "manager_intervention_rate",
        "qa_catch_rate",
        "rework_loops_per_task",
        "valid_knowledge_reuse_rate",
        "false_recall_rate",
    ):
        metric = metrics[key]
        lines.append(f"| `{key}` | {metric['numerator']} | {metric['denominator']} | {metric['value']} |")
    lines.extend(
        [
            "",
            "## Safety gates",
            "",
            "| Gate | Observed | Required | Status |",
            "|---|---:|---:|---|",
            f"| `scope_leakage_acceptances` | {gates['scope_leakage_acceptances']['value']} | 0 | {gates['scope_leakage_acceptances']['status']} |",
            f"| `stale_obsolete_acceptances` | {gates['stale_obsolete_acceptances']['value']} | 0 | {gates['stale_obsolete_acceptances']['status']} |",
            f"| `provenance_probes` | {len(gates['provenance_probes']['value'])} | {gates['provenance_probes']['required']} | {gates['provenance_probes']['status']} |",
            "",
            "## Diagnostic telemetry",
            "",
            "| Metric | Mean | Median | p95 (nearest rank) |",
            "|---|---:|---:|---:|",
            f"| Context tokens/task | {metrics['context_tokens_per_task']['mean']} | {metrics['context_tokens_per_task']['median']} | {metrics['context_tokens_per_task']['p95_nearest_rank']} |",
            f"| Latency (ms) | {metrics['latency_ms']['mean']} | {metrics['latency_ms']['median']} | {metrics['latency_ms']['p95_nearest_rank']} |",
            "",
            "> This versioned baseline is not statistical generality. Safety gates are mandatory; no quality, token, or latency metric is sufficient on its own.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _synthetic_result(
    path: Path, contract_path: Path = DEFAULT_CONTRACT
) -> dict[str, Any]:
    raw = path.read_bytes()
    return _score_synthetic(
        _load_object(path),
        hashlib.sha256(raw).hexdigest(),
        _contract_sha256(contract_path),
    )


def _verify(path: Path, actual: bytes, label: str) -> None:
    try:
        expected = path.read_bytes()
    except OSError as exc:
        raise EvaluationError(f"cannot read committed {label}: {path}: {exc}") from exc
    if expected != actual:
        raise EvaluationError(f"committed {label} is not byte-reproducible: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    synthetic = commands.add_parser("synthetic", help="run the public File/Git synthetic suite")
    synthetic.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    synthetic.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    synthetic.add_argument("--output", type=Path, required=True)
    synthetic.add_argument("--report", type=Path, required=True)
    verify = commands.add_parser("verify", help="reproduce committed result and report bytes")
    verify.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    verify.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    verify.add_argument("--expected-result", type=Path, default=DEFAULT_RESULT)
    verify.add_argument("--expected-report", type=Path, default=DEFAULT_REPORT)
    private = commands.add_parser("private-summary", help="validate allowlisted 3-5 task aggregate")
    private.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    private.add_argument("--summary", type=Path, required=True)
    private.add_argument("--output", type=Path, required=True)
    private.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (
            _synthetic_result(args.suite.resolve(), args.contract.resolve())
            if args.command in {"synthetic", "verify"}
            else _score_private_summary(
                _load_object(args.summary.resolve()), _contract_sha256(args.contract.resolve())
            )
        )
        result_bytes = _json_bytes(result)
        report_bytes = _report_bytes(result)
        if args.command == "verify":
            _verify(args.expected_result.resolve(), result_bytes, "baseline result")
            _verify(args.expected_report.resolve(), report_bytes, "baseline report")
            print("EVALUATION_BASELINE_OK")
        else:
            args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.output.resolve().write_bytes(result_bytes)
            args.report.resolve().write_bytes(report_bytes)
            print(f"EVALUATION_RESULT_OK safety={result['overall_safety_status']}")
        return 0 if result["overall_safety_status"] == "pass" else 2
    except (EvaluationError, OSError) as exc:
        print(f"EVALUATION_BASELINE_FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
