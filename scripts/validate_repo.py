#!/usr/bin/env python3
"""Self-contained validation for the public marketplace repository."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-opc-team"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
PLUGIN_REFERENCE = re.compile(r"<plugin-root>/([A-Za-z0-9_./-]+)")
SKILL_REFERENCE = re.compile(r"(?<!<plugin-root>/)((?:references|assets)/[A-Za-z0-9_./-]+)")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest() -> None:
    data = load_json(MANIFEST)
    require(data.get("name") == "codex-opc-team", "plugin name must match folder")
    require(bool(SEMVER.fullmatch(str(data.get("version", "")))), "plugin version must be semver")
    require(data.get("license") == "Apache-2.0", "plugin license must be Apache-2.0")
    require(data.get("skills") == "./skills/", "skills path must be relative")
    interface = data.get("interface") or {}
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        require(bool(interface.get(key)), f"missing interface.{key}")
    require(len(interface.get("defaultPrompt") or []) <= 3, "defaultPrompt supports at most three entries")


def validate_version_contract() -> None:
    manifest_version = str(load_json(MANIFEST).get("version", ""))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_section = pyproject.split("[project]", 1)[-1].split("[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project_section, re.MULTILINE)
    require(match is not None, "pyproject [project].version is required")
    require(
        match.group(1) == manifest_version,
        "pyproject and plugin manifest versions must match",
    )
    expected_ref = f"--ref v{manifest_version}"
    for readme in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        require(
            expected_ref in readme.read_text(encoding="utf-8"),
            f"{readme.name} must use the manifest version in its fixed-tag install command",
        )

    def dependency_lines(path: Path) -> list[str]:
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    require(
        dependency_lines(ROOT / "requirements-mem0.txt")
        == dependency_lines(PLUGIN / "requirements-mem0.txt"),
        "repository and packaged Mem0 requirements must stay identical",
    )

    tags = subprocess.run(
        ["git", "-C", str(ROOT), "tag", "--points-at", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    require(
        tags.returncode == 0,
        "Git tag enumeration at HEAD failed; version state is unknown",
    )
    # Do not pre-filter through the valid SemVer regex.  A PEP 440 spelling
    # such as ``v0.1.1rc1`` or a malformed ``v0.1.1-`` is precisely the state
    # this contract must reject, not silently reinterpret as "no version tag".
    # An untagged candidate commit is allowed; once any v-prefixed tag points
    # at HEAD, the only permitted value is the exact manifest-derived tag.
    version_tags = [
        tag for tag in tags.stdout.splitlines() if tag.startswith(("v", "V"))
    ]
    if version_tags:
        expected_tag = f"v{manifest_version}"
        require(
            version_tags == [expected_tag],
            f"version tag at HEAD must be exactly {expected_tag}",
        )
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        require(
            f"## [{manifest_version}] - " in changelog,
            "a version tag requires a dated matching CHANGELOG release section",
        )


def validate_marketplace() -> None:
    data = load_json(MARKETPLACE)
    require(data.get("name") == "opc", "marketplace name must be opc")
    entries = [item for item in data.get("plugins", []) if item.get("name") == "codex-opc-team"]
    require(len(entries) == 1, "marketplace must contain exactly one codex-opc-team entry")
    entry = entries[0]
    require(entry.get("source", {}).get("path") == "./plugins/codex-opc-team", "invalid source path")
    require((ROOT / "plugins" / "codex-opc-team").is_dir(), "marketplace source does not exist")
    require(entry.get("policy", {}).get("installation") == "AVAILABLE", "invalid installation policy")
    require(bool(entry.get("policy", {}).get("authentication")), "authentication policy is required")
    require(bool(entry.get("category")), "category is required")


def parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    require(text.startswith("---\n"), f"{path}: missing YAML frontmatter")
    _, raw, _ = text.split("---", 2)
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def validate_skills() -> None:
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    require(bool(skills), "plugin must contain at least one skill")
    for skill_md in skills:
        folder = skill_md.parent.name
        metadata = parse_frontmatter(skill_md)
        skill_text = skill_md.read_text(encoding="utf-8")
        require(metadata.get("name") == folder, f"{skill_md}: name must match folder")
        require(bool(metadata.get("description")), f"{skill_md}: description is required")
        require("[TODO:" not in skill_text, f"{skill_md}: TODO placeholder remains")
        openai_yaml = skill_md.parent / "agents" / "openai.yaml"
        require(openai_yaml.is_file(), f"{folder}: agents/openai.yaml is required")
        ui = openai_yaml.read_text(encoding="utf-8")
        require(f"${folder}" in ui, f"{openai_yaml}: default prompt must mention ${folder}")
        for relative in PLUGIN_REFERENCE.findall(skill_text):
            require((PLUGIN / relative).exists(), f"{skill_md}: missing plugin reference {relative}")
        for relative in SKILL_REFERENCE.findall(skill_text):
            require((skill_md.parent / relative).exists(), f"{skill_md}: missing skill reference {relative}")


def validate_hooks() -> None:
    hooks = PLUGIN / "hooks" / "hooks.json"
    require(hooks.is_file(), "hooks/hooks.json is required")
    data = load_json(hooks)
    require(bool(data.get("hooks")), "hooks map must not be empty")
    hook_script = PLUGIN / "scripts" / "opc_hook.py"
    require(hook_script.is_file(), "opc_hook.py is required")


def validate_mem0_disclosure() -> None:
    """Keep the optional-provider privacy contract aligned with real writes."""
    documents = {
        "adr": ROOT / "docs" / "adr" / "0003-filegit-authority-optional-mem0.md",
        "architecture": ROOT / "docs" / "architecture.md",
        "memory": ROOT / "docs" / "memory-architecture.md",
        "security": ROOT / "docs" / "security-and-privacy.md",
        "installation": ROOT / "docs" / "installation-and-distribution.md",
    }
    combined = "\n".join(path.read_text(encoding="utf-8") for path in documents.values())
    require(
        "Mem0 只保存或返回已批准条目的引用" not in combined,
        "Mem0 disclosure must not claim that the provider stores references only",
    )
    for label, path in documents.items():
        text = path.read_text(encoding="utf-8")
        require(
            "摘要" in text and "正文" in text,
            f"{label} must disclose that approved summary and content enter the Mem0 index",
        )
    require(
        "OpenAI" in documents["adr"].read_text(encoding="utf-8")
        and "OpenAI" in documents["security"].read_text(encoding="utf-8")
        and "OpenAI" in documents["installation"].read_text(encoding="utf-8"),
        "Mem0 disclosure must identify the default external model/embedder data flow",
    )


def validate_architecture_api_contract() -> None:
    """Prevent conceptual architecture names from masquerading as v0.1 imports."""
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    memory = (ROOT / "docs" / "memory-architecture.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    adr = (
        ROOT / "docs" / "adr" / "0003-filegit-authority-optional-mem0.md"
    ).read_text(encoding="utf-8")
    for token in (
        "`MemoryService.export_decision_context(...)`",
        "`FileGitBackend.query(...)`",
        "`Mem0Provider.add(...)` / `search(...)`",
        "概念名",
    ):
        require(token in architecture, f"architecture must map the v0.1 API: {token}")
    require(
        "`opc-context-packet-v1`" in memory
        and "`FileGitBackend.query_context(...)`" in memory
        and "`opc-recall-trace-v1`" in memory,
        "memory architecture must map flat and hierarchical context surfaces",
    )
    require(
        "health=unavailable-file-fallback" in memory and "`not_installed`" not in memory,
        "memory status names must match the v0.1 status output",
    )
    require(
        "`FileGitBackend`" in roadmap
        and "`Mem0Provider`" in roadmap
        and "`doctor`" in roadmap,
        "roadmap must name the implemented v0.1 memory classes",
    )
    require(
        "v0.1 的实现类为 `Mem0Provider`" in adr,
        "ADR must map its conceptual provider to the v0.1 implementation",
    )


def validate_published_memory_contract() -> None:
    """Keep manager approval separate from HEAD-verifiable publication."""
    adr = (
        ROOT / "docs" / "adr" / "0004-controlled-knowledge-promotion.md"
    ).read_text(encoding="utf-8")
    memory = (ROOT / "docs" / "memory-architecture.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    require(
        "`approved` 不等于已发布" in adr and "HEAD-verifiable" in adr,
        "ADR must distinguish approval from Git-verifiable publication",
    )
    require(
        "当前 Git HEAD 验证" in memory and "canonical blob" in memory,
        "memory architecture must require the current HEAD for normal recall",
    )
    require(
        "current HEAD" in readme,
        "public README must disclose the Git publication gate",
    )


def validate_markdown_links() -> None:
    for path in sorted(ROOT.rglob("*.md")):
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for raw in MARKDOWN_LINK.findall(text):
            target = raw.strip().strip("<>").split(maxsplit=1)[0]
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = unquote(target.split("#", 1)[0])
            if not relative:
                continue
            require((path.parent / relative).exists(), f"{path}: broken local link {target}")


def validate_evaluation_baseline() -> None:
    evaluation = ROOT / "evaluation"
    contract_path = evaluation / "contracts" / "baseline-contract.v1.json"
    fixture_path = evaluation / "fixtures" / "synthetic-suite.v1.json"
    result_path = evaluation / "baselines" / "file-git-no-enhancement.v1.json"
    report_path = evaluation / "baselines" / "file-git-no-enhancement.v1.md"
    for path in (contract_path, fixture_path, result_path, report_path):
        require(path.is_file(), f"missing versioned evaluation artifact: {path}")
    contract = load_json(contract_path)
    fixture = load_json(fixture_path)
    result = load_json(result_path)
    version = contract.get("contract_version")
    require(version == "opc-evaluation-contract-v1", "unsupported evaluation contract")
    require(fixture.get("contract_version") == version, "fixture contract version mismatch")
    require(result.get("contract_version") == version, "result contract version mismatch")
    require(
        result.get("baseline_id") == contract.get("baseline_id"),
        "result baseline id mismatch",
    )
    require(
        result.get("contract_sha256") == hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "result is not bound to the exact evaluation contract",
    )
    require(
        result.get("source_sha256") == hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "result is not bound to the exact synthetic fixture",
    )
    require(result.get("overall_safety_status") == "pass", "committed safety baseline must pass")
    report = report_path.read_text(encoding="utf-8")
    require(str(result.get("dataset_id")) in report, "human evaluation report dataset mismatch")
    require("Safety: **PASS**" in report, "human evaluation report safety mismatch")


def validate_structured_feedback_contract() -> None:
    feedback = PLUGIN / "assets" / "feedback"
    contract_path = feedback / "structured-feedback-contract.v1.json"
    schema_path = feedback / "structured-feedback.v1.schema.json"
    script_path = PLUGIN / "scripts" / "opc_feedback.py"
    for path in (contract_path, schema_path, script_path):
        require(path.is_file(), f"missing structured feedback artifact: {path}")
    contract = load_json(contract_path)
    schema = load_json(schema_path)
    baseline = load_json(ROOT / "evaluation" / "contracts" / "baseline-contract.v1.json")
    require(
        contract.get("contract_version") == "opc-structured-feedback-contract-v1",
        "unsupported structured feedback contract",
    )
    require(
        contract.get("schema_version") == "opc-structured-feedback-v1",
        "structured feedback schema version mismatch",
    )
    require(
        contract.get("metric_contract") == baseline.get("contract_version"),
        "structured feedback must reuse the evaluation metric contract",
    )
    metric_ids = [metric.get("id") for metric in baseline.get("metrics", [])]
    require(
        contract.get("metric_ids") == metric_ids,
        "structured feedback metric ids must exactly match the baseline contract",
    )
    require(schema.get("additionalProperties") is False, "feedback record schema must be strict")
    definitions = schema.get("$defs") or {}
    for name in ("metricRef", "references", "event"):
        require(
            definitions.get(name, {}).get("additionalProperties") is False,
            f"feedback schema {name} must reject additional properties",
        )
    require(
        definitions.get("evidenceClass", {}).get("enum") == contract.get("evidence_classes"),
        "feedback evidence classes must match the contract",
    )
    require(
        definitions.get("observedOutcome", {}).get("enum") == contract.get("outcome_statuses"),
        "feedback outcome statuses must match the contract",
    )
    require(
        definitions.get("managerAcceptance", {}).get("enum") == contract.get("manager_judgments"),
        "feedback manager judgments must match the contract",
    )
    require(
        definitions.get("independentQaStatus", {}).get("enum") == contract.get("qa_statuses"),
        "feedback QA statuses must match the contract",
    )
    require(
        set(contract.get("forbidden_side_effects", []))
        == {"candidate_approval", "git_commit", "indexing", "publishing", "payment", "external_communication"},
        "structured feedback forbidden side effects are incomplete",
    )


def validate_shadow_evaluation_contract() -> None:
    contract_path = PLUGIN / "assets" / "evaluation" / "shadow-evaluation-contract.v1.json"
    replay_schema_path = ROOT / "evaluation" / "schemas" / "shadow-replay.v1.schema.json"
    result_schema_path = ROOT / "evaluation" / "schemas" / "shadow-result.v1.schema.json"
    script_path = PLUGIN / "scripts" / "opc_shadow.py"
    skill_path = PLUGIN / "skills" / "opc-shadow-evaluation" / "SKILL.md"
    for path in (contract_path, replay_schema_path, result_schema_path, script_path, skill_path):
        require(path.is_file(), f"missing Shadow Evaluation artifact: {path}")
    contract = load_json(contract_path)
    replay_schema = load_json(replay_schema_path)
    result_schema = load_json(result_schema_path)
    contract_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    baseline_path = ROOT / "evaluation" / "contracts" / "baseline-contract.v1.json"
    baseline = load_json(baseline_path)
    baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    require(
        contract.get("contract_version") == "opc-shadow-evaluation-contract-v1",
        "unsupported Shadow Evaluation contract",
    )
    require(
        contract.get("metric_contract") == baseline.get("contract_version")
        and contract.get("metric_contract_sha256") == baseline_hash,
        "Shadow Evaluation must bind the exact #4 metric contract",
    )
    baseline_groups = {
        "quality_metrics": [
            metric["id"] for metric in baseline["metrics"] if metric["category"] == "product_outcome"
        ],
        "safety_metrics": [
            metric["id"] for metric in baseline["metrics"] if metric["category"] == "safety_gate"
        ],
        "telemetry_metrics": [
            metric["id"] for metric in baseline["metrics"] if metric["category"] == "diagnostic_telemetry"
        ],
    }
    arm = contract.get("arm_contract") or {}
    for group, expected in baseline_groups.items():
        require(arm.get(group) == expected, f"Shadow Evaluation {group} drifted from #4")
    require(
        replay_schema.get("additionalProperties") is False
        and result_schema.get("additionalProperties") is False,
        "Shadow Evaluation top-level schemas must reject extra fields",
    )
    for name in ("dataset", "candidate", "dependency", "ratio", "arm", "case"):
        require(
            replay_schema.get("$defs", {}).get(name, {}).get("additionalProperties") is False,
            f"Shadow replay schema {name} must reject extra fields",
        )
    result_objects = (
        "dataset",
        "candidateSnapshot",
        "preflight",
        "aggregateRatio",
        "contextAggregate",
        "latencyAggregate",
        "armAggregate",
        "metricComparison",
        "positiveMetricComparison",
        "counterMetricComparison",
        "metricRef",
        "feedbackEvidence",
        "evidenceBuckets",
        "confidence",
        "failureMode",
        "conflictingMeasuredFailure",
        "governance",
        "measurements",
    )
    for name in result_objects:
        require(
            result_schema.get("$defs", {}).get(name, {}).get("additionalProperties") is False,
            f"Shadow result schema {name} must reject extra fields",
        )
    result_properties = result_schema.get("properties", {})
    require(
        result_properties.get("metric_contract_sha256", {}).get("const") == baseline_hash
        and result_properties.get("contract_sha256", {}).get("const") == contract_hash,
        "Shadow result schema must bind exact baseline and Shadow contract hashes",
    )
    limits = contract.get("limits") or {}
    require(
        limits
        == {
            "replay_bytes": 524288,
            "feedback_bytes": 524288,
            "result_bytes": 1048576,
            "cases": 20,
            "evidence_items": 200,
            "failure_modes": 64,
            "identifier_characters": 128,
            "portable_reference_characters": 240,
            "ratio_component": 1000000,
            "safety_count": 1000000,
            "context_tokens_per_task": 10000000,
            "latency_ms": 86400000,
            "aggregate_ratio_component": 20000000,
            "aggregate_safety_count": 20000000,
            "aggregate_context_tokens": 200000000,
            "aggregate_latency_ms": 1728000000,
        },
        "Shadow Evaluation numeric and artifact limits drifted",
    )
    replay_defs = replay_schema.get("$defs", {})
    replay_metrics = replay_defs.get("arm", {}).get("properties", {}).get("metrics", {}).get("properties", {})
    require(
        replay_defs.get("ratio", {}).get("properties", {}).get("numerator", {}).get("maximum")
        == limits["ratio_component"]
        and replay_defs.get("ratio", {}).get("properties", {}).get("denominator", {}).get("maximum")
        == limits["ratio_component"]
        and replay_metrics.get("scope_leakage_acceptances", {}).get("maximum")
        == limits["safety_count"]
        and replay_metrics.get("stale_obsolete_acceptances", {}).get("maximum")
        == limits["safety_count"]
        and replay_metrics.get("context_tokens_per_task", {}).get("maximum")
        == limits["context_tokens_per_task"]
        and replay_metrics.get("latency_ms", {}).get("maximum") == limits["latency_ms"],
        "Shadow replay schema numeric limits drifted from the contract",
    )
    governance = result_schema.get("$defs", {}).get("governance", {}).get("properties", {})
    require(
        all(
            governance.get(name, {}).get("const") is False
            for name in (
                "automatic_promotion",
                "candidate_status_changed",
                "canonical_knowledge_written",
                "git_written",
                "provider_index_written",
                "project_source_written",
            )
        ),
        "Shadow result governance write permissions must be schema-constant false",
    )
    positive = result_schema.get("$defs", {}).get("positiveMetricComparison", {})
    counter = result_schema.get("$defs", {}).get("counterMetricComparison", {})
    conflict_failure = result_schema.get("$defs", {}).get("conflictingMeasuredFailure", {})
    result_conditions = json.dumps(result_schema.get("allOf", []), sort_keys=True)
    require(
        positive.get("properties", {}).get("direction", {}).get("const") == "supporting"
        and positive.get("properties", {}).get("source_kind", {}).get("const") == "measured",
        "Shadow positive result schema must require measured supporting evidence",
    )
    require(
        counter.get("properties", {}).get("direction", {}).get("const")
        == "counterevidence"
        and counter.get("properties", {}).get("source_kind", {}).get("const")
        == "measured"
        and conflict_failure.get("properties", {}).get("code", {}).get("const")
        == "conflicting_measured_results"
        and '"$ref": "#/$defs/conflictingMeasuredFailure"' in result_conditions
        and '"$ref": "#/$defs/counterMetricComparison"' in result_conditions
        and '"maxContains": 1' in result_conditions,
        "Shadow result schema must preserve exact measured conflict evidence",
    )
    decision_policy = contract.get("decision_policy") or {}
    require(
        decision_policy.get("conflicting_quality_deltas") == "inconclusive"
        and decision_policy.get("conflicting_measured_quality_or_safety_failure")
        == "conflicting_measured_results"
        and decision_policy.get(
            "positive_forbids_measured_quality_or_safety_counterevidence"
        )
        is True,
        "Shadow measured conflict policy is incomplete",
    )
    require(
        set(contract.get("forbidden_side_effects", []))
        == {
            "candidate_status_change",
            "canonical_knowledge_write",
            "git_write",
            "provider_index_write",
            "project_source_write",
            "automatic_promotion",
        },
        "Shadow Evaluation forbidden side effects are incomplete",
    )
    require(
        (ROOT / "docs" / "adr" / "0010-read-only-shadow-evaluation.md").is_file(),
        "Shadow Evaluation architecture boundary requires ADR-0010",
    )


def validate_knowledge_governance_contract() -> None:
    """Bind the published knowledge contract, runtime, schema, and ADR."""
    asset_root = PLUGIN / "assets" / "knowledge"
    contract_path = asset_root / "knowledge-governance-contract.v1.json"
    schema_path = PLUGIN / "assets" / "knowledge-template" / "schemas" / "experience.schema.json"
    runtime_path = PLUGIN / "scripts" / "opc_governance.py"
    adr_path = ROOT / "docs" / "adr" / "0011-deterministic-knowledge-governance.md"
    guide_path = ROOT / "docs" / "knowledge-governance.md"
    for path in (contract_path, schema_path, runtime_path, adr_path, guide_path):
        require(path.is_file(), f"missing knowledge-governance artifact: {path}")

    spec = importlib.util.spec_from_file_location("opc_governance_contract", runtime_path)
    require(spec is not None and spec.loader is not None, "cannot load governance runtime")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    contract = load_json(contract_path)
    try:
        runtime.validate_contract(contract)
    except runtime.GovernanceError as exc:
        raise ValueError(str(exc)) from exc

    schema = load_json(schema_path)
    require(
        schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        and schema.get("$id") == "urn:codex-opc-team:schema:experience:v2",
        "knowledge record schema must publish Draft 2020-12 Schema 2",
    )
    versions = {
        branch.get("allOf", [{}, {}])[1]
        .get("properties", {})
        .get("schema_version", {})
        .get("const")
        for branch in schema.get("oneOf", [])
        if len(branch.get("allOf", [])) == 2
    }
    require(versions == {1, 2}, "knowledge schema must preserve Schema 1/2 readability")
    require(
        contract.get("ranking_boundary")
        == {
            "file_git_authority": True,
            "provider_is_candidate_source_only": True,
            "provider_score_can_override_hard_filter": False,
            "provider_score_can_override_canonical_order": False,
        },
        "provider ranking boundary must preserve File/Git authority",
    )
    require(
        contract.get("conflict_policy")
        == {
            "unresolved_records_enter_context": False,
            "both_canonical_citations_required": True,
            "body_in_diagnostics": False,
            "manager_curation_required": True,
        },
        "unresolved conflict policy must withhold both bodies",
    )
    require(
        set(contract.get("fail_safe_relation_reasons", []))
        == {
            "relation_target_missing",
            "relation_target_ineligible",
            "relation_cycle",
            "relations_invalid",
        },
        "relation failure policy is incomplete",
    )
    require(
        "record_invalid" in set(contract.get("excluded_reason_codes", [])),
        "invalid canonical records require a redacted omission reason",
    )
    governance = contract.get("governance", {})
    require(
        all(
            governance.get(key) is True
            for key in (
                "migration_inventory_unique_across_statuses",
                "relation_graph_after_hard_filters",
                "relation_cycle_detection_iterative_and_bounded",
                "relation_effects_order_independent",
                "curation_preview_binds_final_timestamp_and_bytes",
                "timezone_aware_evaluation_required",
            )
        ),
        "knowledge governance safety invariants are incomplete",
    )
    require(
        "knowledge-governance-contract.v1.json" in guide_path.read_text(encoding="utf-8")
        and "Schema 1" in guide_path.read_text(encoding="utf-8")
        and "Schema 2" in guide_path.read_text(encoding="utf-8"),
        "knowledge-governance guide must document the contract and migration",
    )


def validate_hierarchical_recall_contract() -> None:
    context_contract_path = PLUGIN / "assets" / "context" / "hierarchical-context-contract.v1.json"
    packet_schema_path = PLUGIN / "assets" / "context" / "context-packet.v1.schema.json"
    trace_schema_path = PLUGIN / "assets" / "context" / "recall-trace.v1.schema.json"
    evaluation_contract_path = ROOT / "evaluation" / "contracts" / "hierarchical-recall-contract.v1.json"
    fixture_path = ROOT / "evaluation" / "fixtures" / "hierarchical-synthetic-suite.v1.json"
    result_path = ROOT / "evaluation" / "baselines" / "hierarchical-recall-comparison.v1.json"
    report_path = ROOT / "evaluation" / "baselines" / "hierarchical-recall-comparison.v1.md"
    latency_path = ROOT / "evaluation" / "baselines" / "hierarchical-recall-latency.v1.json"
    script_path = PLUGIN / "scripts" / "opc_hierarchical.py"
    runner_path = ROOT / "scripts" / "hierarchical_evaluation.py"
    for path in (
        context_contract_path, packet_schema_path, trace_schema_path,
        evaluation_contract_path, fixture_path, result_path,
        report_path, latency_path, script_path, runner_path,
    ):
        require(path.is_file(), f"missing hierarchical recall artifact: {path}")
    context_contract = load_json(context_contract_path)
    packet_schema = load_json(packet_schema_path)
    trace_schema = load_json(trace_schema_path)
    require(
        context_contract.get("contract_version") == "opc-hierarchical-context-contract-v1"
        and context_contract.get("authority") == "file-git-only"
        and context_contract.get("derived_data_authoritative") is False
        and context_contract.get("provider_authoritative") is False
        and context_contract.get("preview_writes") is False
        and context_contract.get("hard_filter_before_navigation") is True
        and context_contract.get("l2_revalidation_required") is True
        and context_contract.get("shared_relation_governance") is True
        and context_contract.get("canonical_governance_snapshot_required") is True
        and context_contract.get("canonical_content_materialization_l2_only") is True
        and context_contract.get("joint_packet_trace_validation") is True
        and context_contract.get("publish_failure_restores_pre_call_tree") is True,
        "hierarchical context authority boundary is incomplete",
    )
    derived = context_contract.get("derived_storage", {})
    require(
        derived.get("relative_to_explicit_private_data_root")
        == ".opc/derived/hierarchical-recall-v1/index.json"
        and all(
            derived.get(key) is False
            for key in ("canonical_write", "provider_write", "project_source_write", "automatic_approval")
        ),
        "hierarchical derived storage boundary is incomplete",
    )
    require(
        packet_schema.get("additionalProperties") is False
        and trace_schema.get("additionalProperties") is False
        and set(packet_schema.get("required", []))
        == {
            "schema_version", "query_sha256", "mode", "facts", "decisions",
            "experiences", "procedures", "citations", "conflicts", "budget",
            "omitted_summary",
        }
        and set(trace_schema.get("required", []))
        == {
            "schema_version", "query_sha256", "mode", "root_selection", "expansions",
            "discards", "fallbacks", "final_leaves", "canonical_reads",
            "canonical_read_count", "injected_token_cost",
        },
        "ContextPacket or RecallTrace schema drifted from runtime",
    )
    require(
        context_contract.get("limits")
        == {
            "index_bytes": 16777216,
            "records": 5000,
            "canonical_reads": 64,
            "budget_tokens": 200000,
            "packet_items": 1000,
            "trace_items": 10002,
            "omitted_items": 20000,
            "navigation_score": 1000000,
        }
        and packet_schema.get("$defs", {}).get("items", {}).get("maxItems") == 1000
        and trace_schema.get("properties", {}).get("canonical_reads", {}).get("maxItems") == 64
        and trace_schema.get("properties", {}).get("expansions", {}).get("maxItems") == 10002,
        "hierarchical schema/runtime bounds drifted",
    )
    fixture = load_json(fixture_path)
    result = load_json(result_path)
    latency = load_json(latency_path)
    contract = load_json(evaluation_contract_path)
    require(
        fixture.get("contract_version") == contract.get("contract_version")
        == result.get("contract_version")
        == latency.get("contract_version")
        == "opc-hierarchical-recall-evaluation-v1",
        "hierarchical evaluation versions are inconsistent",
    )
    require(
        result.get("contract_sha256")
        == hashlib.sha256(evaluation_contract_path.read_bytes()).hexdigest()
        and result.get("fixture_sha256") == hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        and latency.get("fixture_sha256") == result.get("fixture_sha256"),
        "hierarchical result is not bound to contract and fixture bytes",
    )
    require(
        result.get("latency_sha256")
        == hashlib.sha256(
            (json.dumps(latency, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        ).hexdigest(),
        "hierarchical result is not bound to the separate latency artifact",
    )
    safety = result.get("aggregate", {}).get("safety", {})
    require(
        safety.get("scope_leakage_acceptances") == 0
        and safety.get("stale_obsolete_acceptances") == 0,
        "hierarchical committed evaluation must have zero safety acceptance",
    )
    require(
        result.get("comparison_status") in {"superior", "not_superior"}
        and (
            result.get("comparison_status") != "not_superior"
            or "not superior" in str(result.get("claim", ""))
        ),
        "hierarchical evaluation claim does not match measured status",
    )
    report = report_path.read_text(encoding="utf-8")
    for token in ("support precision@5", "canonical leaf recall@5", "injected token median", "p95 latency"):
        require(token in report, f"hierarchical report is missing {token}")


def validate_knowledge_lineage_contract() -> None:
    asset_root = PLUGIN / "assets" / "lineage"
    contract_path = asset_root / "knowledge-lineage-contract.v1.json"
    schema_path = asset_root / "knowledge-lineage.v1.schema.json"
    runtime_path = PLUGIN / "scripts" / "opc_lineage.py"
    adr_path = ROOT / "docs" / "adr" / "0013-private-knowledge-use-lineage.md"
    guide_path = ROOT / "docs" / "knowledge-lineage.md"
    skill_refs = (
        PLUGIN / "skills" / "opc-manager" / "references" / "knowledge-lineage.md",
        PLUGIN / "skills" / "opc-qa-gate" / "references" / "lineage-evidence.md",
        PLUGIN / "skills" / "opc-retrospective" / "references" / "knowledge-lineage.md",
        PLUGIN / "skills" / "opc-memory" / "references" / "knowledge-lineage.md",
    )
    for path in (contract_path, schema_path, runtime_path, adr_path, guide_path, *skill_refs):
        require(path.is_file(), f"missing knowledge-lineage artifact: {path}")

    scripts = str(PLUGIN / "scripts")
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location("opc_lineage_contract", runtime_path)
        require(spec is not None and spec.loader is not None, "cannot load lineage runtime")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        contract, contract_hash = runtime._load_contract()
    finally:
        sys.path.pop(0)
    schema = load_json(schema_path)
    require(
        contract.get("contract_version") == "opc-knowledge-lineage-contract-v1"
        and contract.get("schema_version") == "opc-knowledge-lineage-v1"
        and contract.get("authority") == "file-git-only"
        and contract.get("causal_claim_allowed") is False
        and contract.get("evidence_association_only") is True
        and contract.get("report_claim") == "association/evidence only",
        "knowledge-lineage authority or claim boundary drifted",
    )
    require(
        contract_hash == hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "knowledge-lineage contract hash is not exact file bytes",
    )
    storage = contract.get("storage") or {}
    require(
        storage.get("project_relative") == ".opc/lineage/{run_id}.json"
        and storage.get("private_or_git_ignored") is True
        and storage.get("git_ignored_boundary") == ".opc/lineage/"
        and set(storage.get("transaction_artifacts", []))
        == {"final", "lock", "pending", "backup"}
        and storage.get("subject_binding") == "exact-project-run-instances"
        and storage.get("preview_writes") is False
        and all(
            storage.get(key) is False
            for key in (
                "canonical_write", "provider_write", "project_source_write",
                "remote_telemetry",
            )
        ),
        "knowledge-lineage storage boundary is incomplete",
    )
    require(
        set(contract.get("knowledge_states", []))
        == {"recalled", "injected", "adopted", "ignored", "overridden", "contradicted", "omitted"}
        and set(contract.get("provider_states", []))
        == {"available", "missing", "disabled", "failed", "stale", "no_memory"}
        and set(contract.get("evidence_kinds", []))
        == {"qa", "feedback", "outcome", "shadow", "evaluation"},
        "knowledge-lineage states or portable evidence kinds drifted",
    )
    forbidden = set(contract.get("forbidden_content", []))
    require(
        {
            "raw_chat", "raw_prompt", "chain_of_thought", "hook_payload",
            "tool_payload", "credentials", "embeddings", "session_id",
            "turn_id", "thread_id", "user_home_path", "private_body",
        }
        <= forbidden,
        "knowledge-lineage privacy denylist is incomplete",
    )
    require(
        set(contract.get("forbidden_side_effects", []))
        == {
            "candidate_approval", "candidate_rejection", "knowledge_rewrite",
            "knowledge_promotion", "git_write", "provider_index_write",
            "external_communication",
        },
        "knowledge-lineage forbidden side effects are incomplete",
    )
    compatibility = contract.get("compatibility") or {}
    require(
        compatibility.get("v0_1_without_lineage") == "readable-as-lineage-unavailable"
        and compatibility.get("migration_required") is False
        and compatibility.get("fabricate_defaults") is False,
        "knowledge-lineage v0.1 compatibility is incomplete",
    )
    limits = contract.get("limits") or {}
    require(
        limits.get("events") == 500
        and limits.get("states") == 500
        and limits.get("lineage_bytes") == 1048576
        and limits.get("context_result_bytes") == 2097152
        and schema.get("properties", {}).get("events", {}).get("maxItems") == limits["events"]
        and schema.get("properties", {}).get("states", {}).get("maxItems") == limits["states"],
        "knowledge-lineage schema/runtime bounds drifted",
    )
    require(
        schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        and schema.get("additionalProperties") is False
        and set(schema.get("required", []))
        == {
            "schema_version", "contract_version", "contract_sha256", "project_ref",
            "run_ref", "revision", "created_at", "updated_at", "events", "states",
        },
        "knowledge-lineage top-level schema is not strict",
    )
    for name in (
        "instance", "contextPacket", "knowledgeRef", "provider", "evidenceRef",
        "event", "state",
    ):
        require(
            schema.get("$defs", {}).get(name, {}).get("additionalProperties") is False,
            f"knowledge-lineage schema {name} must reject extra fields",
        )
    event_rules = schema.get("$defs", {}).get("event", {}).get("allOf", [])
    evidence_rules = {
        rule.get("if", {}).get("properties", {}).get("event_type", {}).get("const"):
        rule.get("then", {}).get("properties", {}).get("evidence_refs", {})
        for rule in event_rules
    }
    require(
        evidence_rules.get("knowledge", {}).get("maxItems") == 0
        and evidence_rules.get("provider", {}).get("maxItems") == 0
        and evidence_rules.get("association", {}).get("minItems") == 1,
        "knowledge-lineage evidence refs must be association-only in schema",
    )
    guide = guide_path.read_text(encoding="utf-8")
    require(
        "association/evidence only" in guide
        and "lineage unavailable" in guide
        and "30 天" in guide
        and "base-record CAS" in guide
        and "exact subject binding" in guide
        and "transaction artifacts" in guide
        and "ID-only RecallTrace" in guide
        and "Evidence ref 只允许" in guide
        and "opc_lineage.py" in guide,
        "knowledge-lineage guide omits claim, compatibility, retention, or CLI",
    )


def main() -> int:
    checks = [
        validate_manifest,
        validate_version_contract,
        validate_marketplace,
        validate_skills,
        validate_hooks,
        validate_mem0_disclosure,
        validate_architecture_api_contract,
        validate_published_memory_contract,
        validate_evaluation_baseline,
        validate_structured_feedback_contract,
        validate_shadow_evaluation_contract,
        validate_knowledge_governance_contract,
        validate_hierarchical_recall_contract,
        validate_knowledge_lineage_contract,
        validate_markdown_links,
    ]
    try:
        for check in checks:
            check()
        privacy = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "privacy_scan.py"),
                str(ROOT),
                "--git-history",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        print(privacy.stdout, end="")
        require(privacy.returncode == 0, "privacy scan failed")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"REPO_VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 1
    print("REPO_VALIDATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
