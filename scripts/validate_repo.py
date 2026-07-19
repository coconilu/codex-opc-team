#!/usr/bin/env python3
"""Self-contained validation for the public marketplace repository."""

from __future__ import annotations

import hashlib
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
        "v0.1 尚未发布独立 `ContextPacket` 类型" in memory,
        "memory architecture must label ContextPacket as a conceptual target",
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
