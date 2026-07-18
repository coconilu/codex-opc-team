#!/usr/bin/env python3
"""Self-contained validation for the public marketplace repository."""

from __future__ import annotations

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
