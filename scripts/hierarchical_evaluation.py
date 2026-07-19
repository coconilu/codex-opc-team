#!/usr/bin/env python3
"""Evaluate current flat File/Git recall against hierarchical File/Git recall."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import opc_hierarchical  # noqa: E402
import opc_memory  # noqa: E402


CONTRACT = ROOT / "evaluation" / "contracts" / "hierarchical-recall-contract.v1.json"
FIXTURE = ROOT / "evaluation" / "fixtures" / "hierarchical-synthetic-suite.v1.json"
RESULT = ROOT / "evaluation" / "baselines" / "hierarchical-recall-comparison.v1.json"
REPORT = ROOT / "evaluation" / "baselines" / "hierarchical-recall-comparison.v1.md"
LATENCY = ROOT / "evaluation" / "baselines" / "hierarchical-recall-latency.v1.json"
CONTRACT_VERSION = "opc-hierarchical-recall-evaluation-v1"
RESULT_VERSION = "opc-hierarchical-evaluation-result-v1"
LATENCY_VERSION = "opc-hierarchical-latency-v1"
RECORD_FIELDS = {
    "id", "type", "summary", "content", "keywords", "scope", "project_id",
    "status", "role", "relations",
}
QUERY_FIELDS = {
    "query_id", "text", "project_id", "role", "knowledge_type", "support_ids",
}


class EvaluationError(RuntimeError):
    pass


def _reject_constant(value: str) -> None:
    raise EvaluationError(f"non-finite JSON number: {value}")


def _finite(value: Any, label: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvaluationError(f"{label} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _finite(item, f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _finite(item, f"{label}[{index}]")
        return
    raise EvaluationError(f"{label} contains a non-JSON value")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read strict JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON root must be an object: {path.name}")
    _finite(value, path.name)
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    _finite(value, "output")
    return (
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _portable(value: Any, prefix: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) > 128
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in value)
    ):
        raise EvaluationError(f"{label} is not a portable synthetic identifier")
    return value


def _validate_contract() -> None:
    value = _load(CONTRACT)
    if set(value) != {
        "contract_version", "dataset_schema_version", "result_schema_version", "baseline",
        "treatment", "top_k", "metrics", "safety_thresholds", "superiority_rule",
        "numeric_policy", "reproducibility",
    }:
        raise EvaluationError("hierarchical evaluation contract fields drifted")
    if value.get("contract_version") != CONTRACT_VERSION or value.get("top_k") != 5:
        raise EvaluationError("hierarchical evaluation contract version drifted")


def _validate_fixture(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "schema_version", "contract_version", "dataset_id", "degradation_scenarios",
        "records", "queries",
    }:
        raise EvaluationError("hierarchical fixture fields are not strict")
    if (
        value.get("schema_version") != "opc-hierarchical-synthetic-suite-v1"
        or value.get("contract_version") != CONTRACT_VERSION
        or value.get("dataset_id") != "dataset-syn-hierarchical-recall-v1"
    ):
        raise EvaluationError("hierarchical fixture version drifted")
    records = value.get("records")
    queries = value.get("queries")
    if not isinstance(records, list) or not records or not isinstance(queries, list) or not queries:
        raise EvaluationError("hierarchical fixture records and queries must be non-empty")
    if value.get("degradation_scenarios") != [
        "missing-index", "stale-head", "provider-disabled", "provider-timeout",
        "provider-error", "provider-disagreement",
    ]:
        raise EvaluationError("hierarchical degradation scenarios drifted")
    ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise EvaluationError(f"records[{index}] fields are not strict")
        record_id = _portable(record["id"], "exp-syn-hier-", f"records[{index}].id")
        if record_id in ids:
            raise EvaluationError("hierarchical fixture record ids must be unique")
        ids.add(record_id)
        if record["status"] not in {"approved", "obsolete"}:
            raise EvaluationError("fixture status is unsupported")
        if record["scope"] == "global" and record["project_id"] is not None:
            raise EvaluationError("global fixture record must not carry project_id")
        if record["scope"] == "project":
            _portable(record["project_id"], "project-", "record.project_id")
        if not isinstance(record["relations"], list):
            raise EvaluationError("fixture relations must be an array")
    query_ids: set[str] = set()
    for index, query in enumerate(queries):
        if not isinstance(query, dict) or set(query) != QUERY_FIELDS:
            raise EvaluationError(f"queries[{index}] fields are not strict")
        query_id = _portable(query["query_id"], "query-", f"queries[{index}].query_id")
        if query_id in query_ids:
            raise EvaluationError("query ids must be unique")
        query_ids.add(query_id)
        if not isinstance(query["support_ids"], list) or any(item not in ids for item in query["support_ids"]):
            raise EvaluationError("query support ids must reference fixture records")


def _git(root: Path, *args: str, env: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        env=dict(env),
    )
    if result.returncode != 0:
        raise EvaluationError("synthetic Git setup failed")
    return result.stdout.strip()


def _prepare(base: Path, fixture: Mapping[str, Any]) -> tuple[opc_memory.FileGitBackend, Path, dict[str, Mapping[str, Any]]]:
    knowledge = base / "knowledge"
    data = base / "private-derived"
    knowledge.mkdir()
    home = base / "home"
    home.mkdir()
    gitconfig = base / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_AUTHOR_NAME": "OPC Synthetic Evaluation",
        "GIT_AUTHOR_EMAIL": "evaluation@example.invalid",
        "GIT_COMMITTER_NAME": "OPC Synthetic Evaluation",
        "GIT_COMMITTER_EMAIL": "evaluation@example.invalid",
        "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z",
    }
    _git(knowledge, "init", "-b", "main", env=env)
    known: dict[str, Mapping[str, Any]] = {}
    for item in fixture["records"]:
        record: dict[str, Any] = {
            "schema_version": 2,
            "id": item["id"],
            "type": item["type"],
            "summary": item["summary"],
            "content": item["content"],
            "keywords": item["keywords"],
            "metadata": {"fixture_role": item["role"]},
            "scope": item["scope"],
            "owner": "synthetic-evaluation",
            "evidence": {"method": "public-synthetic"},
            "confidence": 0.8,
            "status": item["status"],
            "sensitivity": "public",
            "applicability": {
                "roles": [item["role"]],
                "knowledge_types": [item["type"]],
                "constraints": {},
                "valid_from": None,
                "valid_until": None,
            },
            "relations": item["relations"],
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }
        if item["scope"] == "project":
            record["project_id"] = item["project_id"]
        if item["status"] == "approved":
            record.update(
                approved_by="synthetic-manager",
                approved_at="2025-01-01T00:00:00Z",
                validation="synthetic replay",
            )
        else:
            record.update(
                obsolete_at="2025-01-01T00:00:00Z",
                obsolete_reason="synthetic obsolete",
            )
        opc_memory.validate_record(record)
        relative = opc_memory.STATUS_DIRS[item["status"]]
        path = knowledge / relative / f"{item['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(opc_memory.canonical_record_bytes(record))
        known[item["id"]] = item
    _git(knowledge, "add", "--", ".", env=env)
    _git(knowledge, "commit", "-m", "synthetic hierarchical corpus", env=env)
    backend = opc_memory.FileGitBackend(knowledge)
    index = opc_hierarchical.HierarchicalIndex(backend, data)
    plan = index.preview()
    index.build(approval_token=plan["approval_token"])
    return backend, data, known


def _token_cost(record: Mapping[str, Any]) -> int:
    content = str(record.get("content", ""))
    citation = record.get("_citation", {})
    return opc_hierarchical._token_cost(content) + opc_hierarchical._token_cost(
        json.dumps(citation, sort_keys=True)
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        raise EvaluationError("metric denominator must be positive")
    return round(numerator / denominator, 4)


def _run_queries(
    backend: opc_memory.FileGitBackend,
    data: Path,
    fixture: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recall = opc_hierarchical.HierarchicalRecall(backend, data)
    cases: list[dict[str, Any]] = []
    flat_hit_total = flat_return_total = hier_hit_total = hier_return_total = expected_total = 0
    flat_tokens: list[int] = []
    hier_tokens: list[int] = []
    scope_leaks = stale_accepts = 0
    known = {item["id"]: item for item in fixture["records"]}
    for query in fixture["queries"]:
        values = {
            "project_id": query["project_id"],
            "role": query["role"],
            "memory_type": query["knowledge_type"],
            "allowed_sensitivity": ["public"],
            "limit": 5,
            "at": "2025-06-01T00:00:00Z",
        }
        flat = backend.query_context(query["text"], **values)
        hierarchical = recall.query(
            query["text"],
            **values,
            budget_tokens=200000,
            canonical_read_limit=10,
        )
        flat_ids = [item["id"] for item in flat["records"]]
        hier_ids = list(hierarchical["recall_trace"]["final_leaves"])
        support = set(query["support_ids"])
        flat_hits = len(support & set(flat_ids))
        hier_hits = len(support & set(hier_ids))
        flat_cost = sum(_token_cost(item) for item in flat["records"])
        hier_cost = int(hierarchical["context_packet"]["budget"]["used_tokens"])
        flat_tokens.append(flat_cost)
        hier_tokens.append(hier_cost)
        flat_hit_total += flat_hits
        flat_return_total += len(flat_ids)
        hier_hit_total += hier_hits
        hier_return_total += len(hier_ids)
        expected_total += len(support)
        for record_id in flat_ids + hier_ids:
            item = known[record_id]
            if item["scope"] == "project" and item["project_id"] != query["project_id"]:
                scope_leaks += 1
            if item["status"] != "approved":
                stale_accepts += 1
        cases.append(
            {
                "query_id": query["query_id"],
                "support_ids": sorted(support),
                "flat": {
                    "leaf_ids": flat_ids,
                    "support_precision_at_5": 1.0 if not flat_ids and not support else _ratio(flat_hits, max(1, len(flat_ids))),
                    "canonical_leaf_recall_at_5": 1.0 if not support else _ratio(flat_hits, len(support)),
                    "injected_tokens": flat_cost,
                },
                "hierarchical": {
                    "leaf_ids": hier_ids,
                    "support_precision_at_5": 1.0 if not hier_ids and not support else _ratio(hier_hits, max(1, len(hier_ids))),
                    "canonical_leaf_recall_at_5": 1.0 if not support else _ratio(hier_hits, len(support)),
                    "injected_tokens": hier_cost,
                },
            }
        )
    if expected_total <= 0:
        raise EvaluationError("fixture must contain supporting canonical leaves")
    aggregate = {
        "flat": {
            "support_precision_at_5": _ratio(flat_hit_total, flat_return_total),
            "canonical_leaf_recall_at_5": _ratio(flat_hit_total, expected_total),
            "injected_tokens_median": median(flat_tokens),
            "injected_tokens_per_query": flat_tokens,
        },
        "hierarchical": {
            "support_precision_at_5": _ratio(hier_hit_total, hier_return_total),
            "canonical_leaf_recall_at_5": _ratio(hier_hit_total, expected_total),
            "injected_tokens_median": median(hier_tokens),
            "injected_tokens_per_query": hier_tokens,
        },
        "safety": {
            "scope_leakage_acceptances": scope_leaks,
            "stale_obsolete_acceptances": stale_accepts,
        },
    }
    return cases, aggregate


def _nearest_p95(values: Sequence[float]) -> float:
    if not values:
        raise EvaluationError("latency samples must not be empty")
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * 0.95))
    return ordered[rank - 1]


def validate_latency(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "schema_version", "contract_version", "fixture_sha256", "method",
        "repeat_count", "flat_ms", "hierarchical_ms",
    }:
        raise EvaluationError("latency artifact fields are not strict")
    if value.get("schema_version") != LATENCY_VERSION or value.get("contract_version") != CONTRACT_VERSION:
        raise EvaluationError("latency artifact version drifted")
    if value.get("fixture_sha256") != _sha(FIXTURE):
        raise EvaluationError("latency artifact fixture binding drifted")
    repeat = value.get("repeat_count")
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 100:
        raise EvaluationError("latency repeat_count is invalid")
    for key in ("flat_ms", "hierarchical_ms"):
        item = value.get(key)
        if not isinstance(item, dict) or set(item) != {"sample_count", "median", "p95_nearest_rank", "samples"}:
            raise EvaluationError("latency aggregate fields are invalid")
        samples = item["samples"]
        if not isinstance(samples, list) or len(samples) != item["sample_count"] or len(samples) != repeat:
            raise EvaluationError("latency aggregate count is impossible")
        if any(isinstance(sample, bool) or not isinstance(sample, (int, float)) or not math.isfinite(float(sample)) or sample <= 0 for sample in samples):
            raise EvaluationError("latency samples must be finite positive numbers")
        expected_median = round(float(median(samples)), 6)
        expected_p95 = round(float(_nearest_p95(samples)), 6)
        if item["median"] != expected_median or item["p95_nearest_rank"] != expected_p95:
            raise EvaluationError("latency aggregate is impossible for its samples")


def _superiority(aggregate: Mapping[str, Any]) -> tuple[str, str]:
    flat = aggregate["flat"]
    hierarchical = aggregate["hierarchical"]
    precision_delta = hierarchical["support_precision_at_5"] - flat["support_precision_at_5"]
    token_reduction = (
        (flat["injected_tokens_median"] - hierarchical["injected_tokens_median"])
        / flat["injected_tokens_median"]
        if flat["injected_tokens_median"] > 0
        else 0
    )
    if hierarchical["support_precision_at_5"] >= flat["support_precision_at_5"] and token_reduction >= 0.2:
        return "superior", "precision_not_lower_and_median_tokens_reduced_at_least_20_percent"
    if hierarchical["injected_tokens_median"] <= flat["injected_tokens_median"] and precision_delta >= 0.10:
        return "superior", "median_tokens_not_higher_and_precision_improved_at_least_0.10"
    return "not_superior", "threshold_not_met"


def build_result(latency: Mapping[str, Any]) -> dict[str, Any]:
    _validate_contract()
    fixture = _load(FIXTURE)
    _validate_fixture(fixture)
    validate_latency(latency)
    with tempfile.TemporaryDirectory(prefix="opc-hierarchical-eval-") as temporary:
        backend, data, _ = _prepare(Path(temporary), fixture)
        cases, aggregate = _run_queries(backend, data, fixture)
        first_query = fixture["queries"][0]
        missing = opc_hierarchical.HierarchicalRecall(
            backend, Path(temporary) / "missing-derived"
        ).query(
            first_query["text"],
            project_id=first_query["project_id"],
            role=first_query["role"],
            memory_type=first_query["knowledge_type"],
            allowed_sensitivity=["public"],
            at="2025-06-01T00:00:00Z",
            limit=5,
        )
        stale_recall = opc_hierarchical.HierarchicalRecall(backend, data)
        marker = backend.add_candidate(
            memory_type="decision",
            summary="synthetic stale-head marker",
            content="synthetic marker",
            scope="project",
            project_id="project-alpha",
            applicable_roles=["developer"],
            sensitivity="public",
        )
        subprocess.run(
            ["git", "-C", str(backend.root), "add", "--", marker["_source_path"]],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git", "-C", str(backend.root), "-c", "user.name=OPC Synthetic Evaluation",
                "-c", "user.email=evaluation@example.invalid", "commit", "-m", "stale index probe",
            ],
            check=True,
            capture_output=True,
        )
        stale = stale_recall.query(
            first_query["text"],
            project_id=first_query["project_id"],
            role=first_query["role"],
            memory_type=first_query["knowledge_type"],
            allowed_sensitivity=["public"],
            at="2025-06-01T00:00:00Z",
            limit=5,
        )
        degradation_probes = {
            "missing_index": missing["context_packet"]["mode"],
            "stale_head": stale["context_packet"]["mode"],
            "provider_scenarios": "covered-by-runtime-contract-tests",
        }
    status, rule = _superiority(aggregate)
    safety = aggregate["safety"]
    if safety["scope_leakage_acceptances"] or safety["stale_obsolete_acceptances"]:
        status = "not_superior"
        rule = "safety_gate_failed"
    aggregate["flat"]["latency_p95_ms"] = latency["flat_ms"]["p95_nearest_rank"]
    aggregate["hierarchical"]["latency_p95_ms"] = latency["hierarchical_ms"]["p95_nearest_rank"]
    return {
        "schema_version": RESULT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "fixture_sha256": _sha(FIXTURE),
        "contract_sha256": _sha(CONTRACT),
        "dataset_id": fixture["dataset_id"],
        "fixture_sha256": _sha(FIXTURE),
        "latency_artifact": LATENCY.name,
        "latency_sha256": hashlib.sha256(_json_bytes(latency)).hexdigest(),
        "latency_reproducibility": "nondeterministic-versioned-separate-artifact",
        "cases": cases,
        "degradation_probes": degradation_probes,
        "aggregate": aggregate,
        "comparison_status": status,
        "comparison_rule": rule,
        "claim": (
            "hierarchical recall is superior on this public synthetic fixture"
            if status == "superior"
            else "hierarchical recall is not superior on this public synthetic fixture"
        ),
    }


def render_report(result: Mapping[str, Any]) -> str:
    flat = result["aggregate"]["flat"]
    hierarchical = result["aggregate"]["hierarchical"]
    safety = result["aggregate"]["safety"]
    lines = [
        "# Hierarchical recall evaluation v1",
        "",
        f"- Dataset: `{result['dataset_id']}`",
        f"- Comparison: **{result['comparison_status'].upper()}**",
        f"- Rule: `{result['comparison_rule']}`",
        f"- Claim: {result['claim']}",
        "",
        "| Metric | Flat File/Git | Hierarchical File/Git |",
        "|---|---:|---:|",
        f"| support precision@5 | {flat['support_precision_at_5']:.4f} | {hierarchical['support_precision_at_5']:.4f} |",
        f"| canonical leaf recall@5 | {flat['canonical_leaf_recall_at_5']:.4f} | {hierarchical['canonical_leaf_recall_at_5']:.4f} |",
        f"| injected token median | {flat['injected_tokens_median']} | {hierarchical['injected_tokens_median']} |",
        f"| p95 latency (ms) | {flat['latency_p95_ms']} | {hierarchical['latency_p95_ms']} |",
        f"| scope leakage | {safety['scope_leakage_acceptances']} | {safety['scope_leakage_acceptances']} |",
        f"| stale/obsolete acceptance | {safety['stale_obsolete_acceptances']} | {safety['stale_obsolete_acceptances']} |",
        "",
        "## Injected tokens per query",
        "",
        "| Query | Flat | Hierarchical |",
        "|---|---:|---:|",
    ]
    for case in result["cases"]:
        lines.append(
            f"| `{case['query_id']}` | {case['flat']['injected_tokens']} | {case['hierarchical']['injected_tokens']} |"
        )
    lines.extend(
        [
            "",
            "Latency is an actual local wall-clock measurement. It is nondeterministic, versioned in a separate artifact, and is not regenerated by byte verification. Retrieval results and this report are byte-reproducible for the committed fixture, contract, and latency artifact.",
            "",
        ]
    )
    return "\n".join(lines)


def benchmark(repeat: int) -> dict[str, Any]:
    fixture = _load(FIXTURE)
    _validate_fixture(fixture)
    flat_samples: list[float] = []
    hierarchical_samples: list[float] = []
    for _ in range(repeat):
        with tempfile.TemporaryDirectory(prefix="opc-hierarchical-bench-") as temporary:
            backend, data, _ = _prepare(Path(temporary), fixture)
            recall = opc_hierarchical.HierarchicalRecall(backend, data)
            start = time.perf_counter_ns()
            for query in fixture["queries"]:
                backend.query_context(
                    query["text"], project_id=query["project_id"], role=query["role"],
                    memory_type=query["knowledge_type"], allowed_sensitivity=["public"],
                    at="2025-06-01T00:00:00Z", limit=5,
                )
            flat_samples.append(round((time.perf_counter_ns() - start) / 1_000_000, 6))
            start = time.perf_counter_ns()
            for query in fixture["queries"]:
                recall.query(
                    query["text"], project_id=query["project_id"], role=query["role"],
                    memory_type=query["knowledge_type"], allowed_sensitivity=["public"],
                    at="2025-06-01T00:00:00Z", limit=5, budget_tokens=200000,
                )
            hierarchical_samples.append(round((time.perf_counter_ns() - start) / 1_000_000, 6))
    value = {
        "schema_version": LATENCY_VERSION,
        "contract_version": CONTRACT_VERSION,
        "method": "local-wall-clock-all-synthetic-queries-warm-process",
        "repeat_count": repeat,
        "flat_ms": {
            "sample_count": repeat,
            "median": round(float(median(flat_samples)), 6),
            "p95_nearest_rank": round(float(_nearest_p95(flat_samples)), 6),
            "samples": flat_samples,
        },
        "hierarchical_ms": {
            "sample_count": repeat,
            "median": round(float(median(hierarchical_samples)), 6),
            "p95_nearest_rank": round(float(_nearest_p95(hierarchical_samples)), 6),
            "samples": hierarchical_samples,
        },
    }
    validate_latency(value)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("benchmark")
    bench.add_argument("--output", type=Path, required=True)
    bench.add_argument("--repeat", type=int, default=5)
    generate = commands.add_parser("generate")
    generate.add_argument("--latency", type=Path, default=LATENCY)
    generate.add_argument("--output", type=Path, default=RESULT)
    generate.add_argument("--report", type=Path, default=REPORT)
    commands.add_parser("verify")
    args = parser.parse_args(argv)
    try:
        if args.command == "benchmark":
            if not 1 <= args.repeat <= 100:
                raise EvaluationError("repeat must be between 1 and 100")
            args.output.write_bytes(_json_bytes(benchmark(args.repeat)))
        elif args.command == "generate":
            latency = _load(args.latency)
            result = build_result(latency)
            args.output.write_bytes(_json_bytes(result))
            args.report.write_text(render_report(result), encoding="utf-8", newline="\n")
        else:
            latency = _load(LATENCY)
            expected = build_result(latency)
            if RESULT.read_bytes() != _json_bytes(expected):
                raise EvaluationError("hierarchical result is not byte reproducible")
            if REPORT.read_text(encoding="utf-8") != render_report(expected):
                raise EvaluationError("hierarchical report is not byte reproducible")
        print("HIERARCHICAL_EVALUATION_OK")
        return 0
    except (EvaluationError, OSError, ValueError, opc_memory.OpcMemoryError, opc_hierarchical.HierarchicalError) as exc:
        print(f"HIERARCHICAL_EVALUATION_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
