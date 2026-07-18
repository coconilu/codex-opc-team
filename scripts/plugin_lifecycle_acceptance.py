#!/usr/bin/env python3
"""Run the installed Codex plugin lifecycle in a privacy-safe clean room.

The default mode only prints a plan.  ``--apply`` requires a dedicated
workspace which is either empty or already owned by this acceptance tool.
No command in this module removes canonical knowledge or optional memory data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = ROOT / "plugins" / "codex-opc-team" / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

import opc_knowledge  # noqa: E402


PLUGIN_ID = "codex-opc-team@opc"
MARKETPLACE = "opc"
FIXTURE_PLUGIN_ID = "lifecycle-sentinel@lifecycle-fixture"
FIXTURE_MARKETPLACE = "lifecycle-fixture"
OWNERSHIP_MARKER = ".opc-lifecycle-clean-room.json"
REPORT_SCHEMA = "opc-plugin-lifecycle/v1"
SKILLS = (
    "opc-manager",
    "opc-project-bootstrap",
    "opc-qa-gate",
    "opc-retrospective",
    "opc-memory-curator",
    "opc-memory",
)
FIXTURE_SKILL = "lifecycle-sentinel"
SYNTHETIC_EXPERIENCE = {
    "schema_version": 1,
    "id": "exp-lifecycle-sentinel",
    "type": "decision",
    "summary": "Preserve synthetic canonical knowledge during plugin lifecycle tests.",
    "content": "This public test fixture contains no manager or project information.",
    "keywords": ["lifecycle", "synthetic"],
    "metadata": {"fixture": True},
    "scope": "global",
    "owner": "acceptance-fixture",
    "evidence": {"kind": "synthetic-test"},
    "confidence": 1.0,
    "status": "approved",
    "validation": {"method": "fixture-contract"},
    "approved_by": "acceptance-fixture",
    "approved_at": "2000-01-01T00:00:00Z",
    "created_at": "2000-01-01T00:00:00Z",
    "updated_at": "2000-01-01T00:00:00Z",
}
MEMORY_CONFIG = {
    "schema_version": 1,
    "installation_id": "00000000-0000-4000-8000-000000000003",
    "mem0": {
        "enabled": False,
        "user_id": "opc-00000000-0000-4000-8000-000000000003",
    },
}


class AcceptanceError(RuntimeError):
    """Raised when a lifecycle gate fails closed."""


class CommandError(AcceptanceError):
    def __init__(self, command: Sequence[str], result: subprocess.CompletedProcess[str]):
        self.command = tuple(command)
        self.returncode = result.returncode
        super().__init__(f"command failed with exit code {result.returncode}: {command[0]}")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_exact(path: Path, content: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise AcceptanceError(f"owned fixture changed; refusing overwrite: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unrelated_config_sha256(path: Path) -> str:
    """Hash config after removing only the two OPC-owned TOML tables.

    Codex legitimately records marketplace/plugin state in ``config.toml``.
    Treating the whole file as immutable would reject every real install, so
    this comparison ignores the exact tables owned by this plugin while still
    protecting the sentinel setting and unrelated plugin configuration.
    """

    owned_headers = {
        "[marketplaces.opc]",
        '[plugins."codex-opc-team@opc"]',
    }
    kept: list[str] = []
    in_owned_table = False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_owned_table = stripped in owned_headers
        if not in_owned_table and stripped:
            kept.append(stripped)
    normalized = ("\n".join(kept) + "\n").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _tree_hashes(root: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts) or not path.is_file():
            continue
        result[relative.as_posix()] = _sha256(path)
    return result


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _knowledge_snapshot(root: Path) -> dict[str, Any]:
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "history": _git(root, "rev-list", "--all").splitlines(),
        "status": _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        "schema_version": catalog.get("schema_version"),
        "working_tree": _tree_hashes(root, exclude={".git"}),
        "approved": _tree_hashes(root / "experiences" / "approved"),
    }


def _protected_snapshot(paths: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "config_sha256": _unrelated_config_sha256(paths["config"]),
        "knowledge": _knowledge_snapshot(paths["knowledge"]),
        "memory": _tree_hashes(paths["memory"]),
    }


def _assert_protected_unchanged(
    baseline: Mapping[str, Any], paths: Mapping[str, Path], phase: str
) -> None:
    current = _protected_snapshot(paths)
    if current != baseline:
        raise AcceptanceError(f"protected data changed during {phase}")
    if current["knowledge"]["status"]:
        raise AcceptanceError(f"synthetic knowledge became dirty during {phase}")


def _paths(workspace: Path) -> dict[str, Path]:
    return {
        "workspace": workspace,
        "codex_home": workspace / "codex-home",
        "user_home": workspace / "user-home",
        "appdata": workspace / "appdata",
        "localappdata": workspace / "localappdata",
        "xdg_config": workspace / "xdg-config",
        "xdg_data": workspace / "xdg-data",
        "xdg_cache": workspace / "xdg-cache",
        "knowledge": workspace / "knowledge",
        "memory": workspace / "memory-data",
        "probe": workspace / "probe-project",
        "fixture_marketplace": workspace / "fixture-marketplace",
        "config": workspace / "codex-home" / "config.toml",
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_workspace(workspace: Path) -> Path:
    workspace = workspace.expanduser().resolve()
    repository = ROOT.resolve()
    if (
        workspace == repository
        or _is_relative_to(workspace, repository)
        or _is_relative_to(repository, workspace)
    ):
        raise AcceptanceError("clean-room workspace overlaps the public repository")
    home = Path.home().resolve()
    if workspace == home or _is_relative_to(home, workspace):
        raise AcceptanceError("clean-room workspace contains the real user home")
    configured_codex_home = os.environ.get("CODEX_HOME")
    if configured_codex_home:
        target = Path(configured_codex_home).expanduser().resolve()
        if (
            workspace == target
            or _is_relative_to(workspace, target)
            or _is_relative_to(target, workspace)
        ):
            raise AcceptanceError("clean-room workspace overlaps a protected existing root")
    if workspace.exists() and any(workspace.iterdir()):
        marker = workspace / OWNERSHIP_MARKER
        if not marker.is_file():
            raise AcceptanceError("workspace is non-empty and has no lifecycle ownership marker")
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("schema") != REPORT_SCHEMA or payload.get("owner") != "codex-opc-team":
            raise AcceptanceError("workspace ownership marker is invalid")
    return workspace


def _prepare_fixture_tree(paths: Mapping[str, Path]) -> None:
    workspace = paths["workspace"]
    workspace.mkdir(parents=True, exist_ok=True)
    _write_exact(
        workspace / OWNERSHIP_MARKER,
        _json_bytes({"schema": REPORT_SCHEMA, "owner": "codex-opc-team"}),
    )
    for name in (
        "codex_home",
        "user_home",
        "appdata",
        "localappdata",
        "xdg_config",
        "xdg_data",
        "xdg_cache",
        "memory",
        "probe",
    ):
        paths[name].mkdir(parents=True, exist_ok=True)

    config_sentinel = b'[unrelated]\npreserve = "synthetic-lifecycle-sentinel"\n'
    if paths["config"].exists():
        existing_config = paths["config"].read_text(encoding="utf-8-sig")
        if (
            "[unrelated]" not in existing_config
            or 'preserve = "synthetic-lifecycle-sentinel"' not in existing_config
        ):
            raise AcceptanceError("unrelated clean-room config sentinel changed")
    else:
        _write_exact(paths["config"], config_sentinel)
    _write_exact(paths["memory"] / "config.json", _json_bytes(MEMORY_CONFIG))
    _write_exact(
        paths["memory"] / "provider-data" / "sentinel.bin",
        b"synthetic optional-memory data\x00\x01\n",
    )

    try:
        opc_knowledge.init_knowledge(root=paths["knowledge"], git_init=True)
    except opc_knowledge.OpcError as exc:
        raise AcceptanceError(str(exc)) from exc
    approved = paths["knowledge"] / "experiences" / "approved" / "exp-lifecycle-sentinel.json"
    expected = _json_bytes(SYNTHETIC_EXPERIENCE)
    if approved.exists() and approved.read_bytes() != expected:
        raise AcceptanceError("synthetic approved fixture changed; refusing overwrite")
    if not approved.exists():
        approved.write_bytes(expected)
        _git(paths["knowledge"], "add", "--", "experiences/approved/exp-lifecycle-sentinel.json")
        subprocess.run(
            [
                "git",
                "-C",
                str(paths["knowledge"]),
                "-c",
                "user.name=OPC Lifecycle Acceptance",
                "-c",
                "user.email=opc-lifecycle@users.noreply.github.com",
                "commit",
                "-m",
                "test: add synthetic approved lifecycle fixture",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    if _git(paths["knowledge"], "status", "--porcelain=v1", "--untracked-files=all"):
        raise AcceptanceError("synthetic knowledge fixture must start clean")

    marketplace = paths["fixture_marketplace"]
    _write_exact(
        marketplace / ".agents" / "plugins" / "marketplace.json",
        _json_bytes(
            {
                "name": FIXTURE_MARKETPLACE,
                "plugins": [
                    {
                        "name": "lifecycle-sentinel",
                        "source": {"source": "local", "path": "./plugin"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    }
                ],
            }
        ),
    )
    _write_exact(
        marketplace / "plugin" / ".codex-plugin" / "plugin.json",
        _json_bytes(
            {
                "name": "lifecycle-sentinel",
                "version": "1.0.0",
                "description": "Synthetic unrelated plugin used by lifecycle acceptance.",
                "skills": "./skills/",
            }
        ),
    )
    _write_exact(
        marketplace / "plugin" / "skills" / FIXTURE_SKILL / "SKILL.md",
        (
            "---\nname: lifecycle-sentinel\n"
            "description: Synthetic unrelated skill for isolated lifecycle acceptance.\n---\n\n"
            "# Lifecycle sentinel\n\nThis fixture must survive OPC removal.\n"
        ).encode("utf-8"),
    )


def _clean_env(paths: Mapping[str, Path]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "CODEX_HOME": str(paths["codex_home"]),
            "HOME": str(paths["user_home"]),
            "USERPROFILE": str(paths["user_home"]),
            "APPDATA": str(paths["appdata"]),
            "LOCALAPPDATA": str(paths["localappdata"]),
            "XDG_CONFIG_HOME": str(paths["xdg_config"]),
            "XDG_DATA_HOME": str(paths["xdg_data"]),
            "XDG_CACHE_HOME": str(paths["xdg_cache"]),
            "OPC_KNOWLEDGE_HOME": str(paths["knowledge"]),
            "OPC_MEMORY_DATA_HOME": str(paths["memory"]),
            "MEM0_TELEMETRY": "False",
            "NO_COLOR": "1",
            "GIT_CONFIG_GLOBAL": str(paths["user_home"] / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    for key in tuple(env):
        upper = key.upper()
        if upper.endswith("_API_KEY") or upper in {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AZURE_CLIENT_SECRET",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "SSH_AUTH_SOCK",
        }:
            env.pop(key, None)
    return env


class CodexRunner:
    def __init__(self, executable: str, env: Mapping[str, str], cwd: Path):
        self.executable = executable
        self.env = dict(env)
        self.cwd = cwd

    def run(self, *args: str, json_output: bool = False) -> Any:
        command = [self.executable, *args]
        result = subprocess.run(
            command,
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode:
            raise CommandError(command, result)
        if not json_output:
            return result.stdout
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(f"Codex returned invalid JSON for {args[0]}") from exc

    def plugin_state(self) -> dict[str, Any]:
        return self.run("plugin", "list", "--available", "--json", json_output=True)

    def marketplace_state(self) -> dict[str, Any]:
        return self.run("plugin", "marketplace", "list", "--json", json_output=True)


def _marketplace_names(runner: CodexRunner) -> set[str]:
    return {item.get("name") for item in runner.marketplace_state().get("marketplaces", [])}


def _installed_ids(runner: CodexRunner) -> set[str]:
    return {item.get("pluginId") for item in runner.plugin_state().get("installed", [])}


def _remove_owned(runner: CodexRunner, plugin_id: str, marketplace: str) -> list[str]:
    actions: list[str] = []
    if plugin_id in _installed_ids(runner):
        runner.run("plugin", "remove", plugin_id, "--json", json_output=True)
        actions.append("plugin-removed")
    else:
        actions.append("plugin-already-absent")
    if marketplace in _marketplace_names(runner):
        runner.run("plugin", "marketplace", "remove", marketplace, "--json", json_output=True)
        actions.append("marketplace-removed")
    else:
        actions.append("marketplace-already-absent")
    return actions


def _source_argument(source: str) -> str:
    candidate = Path(source).expanduser()
    return str(candidate.resolve()) if candidate.exists() else source


def _add_marketplace(runner: CodexRunner, source: str, ref: str | None) -> dict[str, Any]:
    command = ["plugin", "marketplace", "add", _source_argument(source)]
    if ref:
        if Path(source).expanduser().exists():
            raise AcceptanceError("--ref cannot be combined with a local marketplace path")
        command.extend(["--ref", ref])
    command.append("--json")
    result = runner.run(*command, json_output=True)
    if result.get("marketplaceName") != MARKETPLACE:
        raise AcceptanceError("candidate source did not register the expected opc marketplace")
    return result


def _install_opc(runner: CodexRunner, source: str, ref: str | None) -> dict[str, Any]:
    _add_marketplace(runner, source, ref)
    result = runner.run("plugin", "add", PLUGIN_ID, "--json", json_output=True)
    if PLUGIN_ID not in _installed_ids(runner):
        raise AcceptanceError("Codex did not report the OPC plugin as installed")
    return result


def _install_fixture(runner: CodexRunner, source: Path) -> None:
    result = runner.run(
        "plugin", "marketplace", "add", str(source), "--json", json_output=True
    )
    if result.get("marketplaceName") != FIXTURE_MARKETPLACE:
        raise AcceptanceError("unrelated fixture marketplace did not register")
    runner.run("plugin", "add", FIXTURE_PLUGIN_ID, "--json", json_output=True)
    if FIXTURE_PLUGIN_ID not in _installed_ids(runner):
        raise AcceptanceError("unrelated fixture plugin did not install")


def _discovery(runner: CodexRunner, *, expect_opc: bool) -> dict[str, Any]:
    # This starts a new OS process and asks Codex to render the real model-visible
    # prompt.  It does not call a model, use credentials, or create a session.
    prompt = runner.run("debug", "prompt-input", "lifecycle acceptance probe")
    try:
        parsed = json.loads(prompt)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("fresh-process prompt discovery returned invalid JSON") from exc
    flattened = json.dumps(parsed, ensure_ascii=False)
    present = sorted(skill for skill in SKILLS if skill in flattened)
    fixture_present = FIXTURE_SKILL in flattened
    if expect_opc and tuple(present) != tuple(sorted(SKILLS)):
        missing = sorted(set(SKILLS) - set(present))
        raise AcceptanceError(f"fresh-process discovery missed OPC skills: {missing}")
    if not expect_opc and present:
        raise AcceptanceError("OPC skills remained model-visible after uninstall")
    if not fixture_present:
        raise AcceptanceError("unrelated fixture skill was removed or undiscoverable")
    return {
        "method": "fresh-process-debug-prompt-input",
        "opc_skills": present,
        "unrelated_fixture_present": fixture_present,
        "model_or_network_call": False,
    }


def _memory_status(install_result: Mapping[str, Any], paths: Mapping[str, Path]) -> dict[str, Any]:
    installed_path = install_result.get("installedPath")
    if not isinstance(installed_path, str):
        raise AcceptanceError("Codex install result omitted installedPath")
    script = Path(installed_path) / "scripts" / "opc_memory.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--knowledge-root",
            str(paths["knowledge"]),
            "--data-root",
            str(paths["memory"]),
            "status",
        ],
        cwd=paths["probe"],
        env=_clean_env(paths),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise CommandError([sys.executable, "opc_memory.py", "status"], result)
    status = json.loads(result.stdout)
    if status.get("authority") != "file-git":
        raise AcceptanceError("reinstalled plugin did not reconnect File/Git authority")
    audit = status.get("knowledge_git", {})
    if audit.get("head") != _git(paths["knowledge"], "rev-parse", "HEAD"):
        raise AcceptanceError("reinstalled plugin reported the wrong knowledge Git HEAD")
    return {
        "authority": status.get("authority"),
        "knowledge_clean": not bool(audit.get("dirty")),
        "mem0_enabled": status.get("mem0", {}).get("enabled"),
        "mem0_health": status.get("mem0", {}).get("health"),
    }


def _source_report(source: str, ref: str | None) -> dict[str, Any]:
    path = Path(source).expanduser()
    if path.exists():
        return {"kind": "local", "name": path.resolve().name, "ref": None}
    if "://" in source:
        parsed = urlsplit(source)
        safe_source = urlunsplit(
            (parsed.scheme, parsed.hostname or "", parsed.path, "", "")
        )
    elif source.count("/") == 1 and not any(char in source for char in ("\\", "@", ":")):
        safe_source = source
    else:
        safe_source = "redacted-git-source"
    return {"kind": "git", "source": safe_source, "ref": ref}


def _failure_domain(exc: BaseException) -> str:
    if not isinstance(exc, CommandError):
        return "local-package-discovery-or-preservation"
    command = set(exc.command)
    if "marketplace" in command and "add" in command:
        return "marketplace-fetch-or-ref"
    if "plugin" in command and "add" in command:
        return "plugin-install"
    if "prompt-input" in command:
        return "fresh-process-discovery"
    if "opc_memory.py" in command:
        return "knowledge-reconnect"
    return "codex-lifecycle-command"


def _plan(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "dry_run": not args.apply,
        "workspace": str(workspace),
        "candidate": _source_report(args.candidate_source, args.candidate_ref),
        "rollback": _source_report(args.rollback_source, args.rollback_ref),
        "operations": [
            "create or reuse an owned isolated CODEX_HOME and user home",
            "initialize synthetic File/Git knowledge plus disabled optional-memory sentinel",
            "install an unrelated fixture plugin",
            "install candidate and verify six Skills from a fresh Codex process",
            "repeat install safely, uninstall only OPC state, and verify OPC Skills disappear",
            "reinstall and prove existing knowledge/data/config reconnect unchanged",
            "install rollback source/ref and repeat fresh-process discovery",
        ],
        "global_codex_config_action": "none-outside-clean-room",
        "canonical_knowledge_action": "initialize-synthetic-only-then-read-only",
        "optional_memory_action": "create-synthetic-disabled-sentinel-then-read-only",
        "external_model_or_credential_action": "none",
    }


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    workspace = validate_workspace(Path(args.workspace))
    plan = _plan(args, workspace)
    if not args.apply:
        return plan
    if args.require_fixed_refs:
        if not args.candidate_ref or not args.rollback_ref:
            raise AcceptanceError("release mode requires candidate and rollback refs")
        if Path(args.candidate_source).expanduser().exists() or Path(
            args.rollback_source
        ).expanduser().exists():
            raise AcceptanceError("release mode requires Git marketplace sources, not local paths")
        if args.candidate_ref == args.rollback_ref:
            raise AcceptanceError("release mode requires distinct candidate and rollback refs")
        if not args.expected_candidate_version or not args.expected_rollback_version:
            raise AcceptanceError("release mode requires expected candidate and rollback versions")
        if args.expected_candidate_version == args.expected_rollback_version:
            raise AcceptanceError("release mode requires distinct candidate and rollback versions")

    executable = shutil.which(args.codex)
    if not executable:
        raise AcceptanceError("Codex CLI is unavailable; installed-state gate was not run")
    paths = _paths(workspace)
    _prepare_fixture_tree(paths)
    runner = CodexRunner(executable, _clean_env(paths), paths["probe"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "running",
        "platform": {"system": platform.system(), "python": platform.python_version()},
        "candidate": _source_report(args.candidate_source, args.candidate_ref),
        "rollback": _source_report(args.rollback_source, args.rollback_ref),
        "checks": {},
        "privacy": {
            "isolated_codex_home": True,
            "isolated_home_and_userprofile": True,
            "credentials_removed_from_child_environment": True,
            "report_contains_host_paths": False,
        },
    }
    phase = "clean-room-reset"
    try:
        report["platform"]["codex"] = runner.run("--version").strip()
        _remove_owned(runner, PLUGIN_ID, MARKETPLACE)
        _remove_owned(runner, FIXTURE_PLUGIN_ID, FIXTURE_MARKETPLACE)
        _install_fixture(runner, paths["fixture_marketplace"])
        baseline = _protected_snapshot(paths)
        if baseline["knowledge"]["status"]:
            raise AcceptanceError("synthetic knowledge baseline is dirty")

        phase = "candidate-install"
        candidate = _install_opc(runner, args.candidate_source, args.candidate_ref)
        candidate_version = candidate.get("version")
        if args.expected_candidate_version and candidate_version != args.expected_candidate_version:
            raise AcceptanceError("candidate version did not match the expected release version")
        report["checks"]["candidate_install"] = {
            "version": candidate_version,
            "fixed_ref": bool(args.candidate_ref),
            "discovery": _discovery(runner, expect_opc=True),
        }
        _assert_protected_unchanged(baseline, paths, phase)

        phase = "candidate-idempotent-reapply"
        repeated = _install_opc(runner, args.candidate_source, args.candidate_ref)
        report["checks"]["candidate_reapply"] = {
            "safe": True,
            "same_version": repeated.get("version") == candidate_version,
        }
        _assert_protected_unchanged(baseline, paths, phase)

        phase = "uninstall"
        first_remove = _remove_owned(runner, PLUGIN_ID, MARKETPLACE)
        second_remove = _remove_owned(runner, PLUGIN_ID, MARKETPLACE)
        report["checks"]["uninstall"] = {
            "first": first_remove,
            "repeated": second_remove,
            "discovery": _discovery(runner, expect_opc=False),
            "unrelated_plugin_present": FIXTURE_PLUGIN_ID in _installed_ids(runner),
        }
        if not report["checks"]["uninstall"]["unrelated_plugin_present"]:
            raise AcceptanceError("uninstall removed the unrelated fixture plugin")
        _assert_protected_unchanged(baseline, paths, phase)

        phase = "reinstall"
        reinstalled = _install_opc(runner, args.candidate_source, args.candidate_ref)
        report["checks"]["reinstall"] = {
            "version": reinstalled.get("version"),
            "discovery": _discovery(runner, expect_opc=True),
            "memory_status": _memory_status(reinstalled, paths),
        }
        _assert_protected_unchanged(baseline, paths, phase)

        phase = "rollback"
        _remove_owned(runner, PLUGIN_ID, MARKETPLACE)
        rollback = _install_opc(runner, args.rollback_source, args.rollback_ref)
        rollback_version = rollback.get("version")
        if args.expected_rollback_version and rollback_version != args.expected_rollback_version:
            raise AcceptanceError("rollback version did not match the expected supported version")
        report["checks"]["rollback"] = {
            "version": rollback_version,
            "distinct_version": rollback_version != candidate_version,
            "fixed_ref": bool(args.rollback_ref),
            "discovery": _discovery(runner, expect_opc=True),
            "memory_status": _memory_status(rollback, paths),
        }
        _assert_protected_unchanged(baseline, paths, phase)

        phase = "rollback-idempotent-reapply"
        repeated_rollback = _install_opc(runner, args.rollback_source, args.rollback_ref)
        report["checks"]["rollback_reapply"] = {
            "safe": True,
            "same_version": repeated_rollback.get("version") == rollback_version,
        }
        _assert_protected_unchanged(baseline, paths, phase)
        report["protected_data"] = {
            "knowledge_head": baseline["knowledge"]["head"],
            "knowledge_history_commits": len(baseline["knowledge"]["history"]),
            "knowledge_schema_version": baseline["knowledge"]["schema_version"],
            "knowledge_files_sha256": baseline["knowledge"]["working_tree"],
            "approved_entries_sha256": baseline["knowledge"]["approved"],
            "memory_files_sha256": baseline["memory"],
            "unrelated_config_sha256": baseline["config_sha256"],
            "preserved": True,
        }
        report["release_gate"] = {
            "eligible": bool(args.require_fixed_refs),
            "candidate_fixed_ref": bool(args.candidate_ref),
            "rollback_fixed_ref": bool(args.rollback_ref),
            "distinct_refs": bool(
                args.candidate_ref
                and args.rollback_ref
                and args.candidate_ref != args.rollback_ref
            ),
            "distinct_versions": rollback_version != candidate_version,
        }
        report["status"] = "pass"
        report["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return report
    except (AcceptanceError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        report["status"] = "fail"
        report["failed_phase"] = phase
        report["failure_domain"] = _failure_domain(exc)
        report["error_type"] = type(exc).__name__
        if isinstance(exc, CommandError):
            report["command_exit_code"] = exc.returncode
        report["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        raise AcceptanceRunFailed(report) from exc


class AcceptanceRunFailed(AcceptanceError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(f"installed lifecycle failed during {report.get('failed_phase')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Dedicated clean-room directory")
    parser.add_argument("--candidate-source", default=str(ROOT))
    parser.add_argument("--candidate-ref")
    parser.add_argument("--rollback-source")
    parser.add_argument("--rollback-ref")
    parser.add_argument("--expected-candidate-version")
    parser.add_argument("--expected-rollback-version")
    parser.add_argument("--require-fixed-refs", action="store_true")
    parser.add_argument("--codex", default="codex", help="Codex CLI executable")
    parser.add_argument("--report", help="Write a redacted machine-readable JSON report")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    return parser


def _write_report(path: str | None, report: Mapping[str, Any]) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    redacted = json.loads(json.dumps(report))
    if "workspace" in redacted:
        redacted["workspace"] = "isolated-clean-room"
    target.write_bytes(_json_bytes(redacted))


def validate_report_target(path: str | None, workspace: Path) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    repository = ROOT.resolve()
    if target == repository or _is_relative_to(target, repository):
        raise AcceptanceError("report must not be written into the public repository")
    protected = _paths(workspace)
    for key in ("codex_home", "user_home", "knowledge", "memory", "fixture_marketplace"):
        root = protected[key].resolve()
        if target == root or _is_relative_to(target, root):
            raise AcceptanceError(f"report must not be written inside {key}")
    if target.exists() and not _is_relative_to(target, workspace):
        raise AcceptanceError("refusing to overwrite an existing report outside the clean room")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.rollback_source = args.rollback_source or args.candidate_source
    try:
        validate_report_target(args.report, Path(args.workspace))
        if args.report and not args.apply:
            report_target = Path(args.report).expanduser().resolve()
            workspace_target = Path(args.workspace).expanduser().resolve()
            if _is_relative_to(report_target, workspace_target):
                raise AcceptanceError(
                    "dry-run report must be outside the non-mutating clean-room workspace"
                )
        report = run_acceptance(args)
    except AcceptanceRunFailed as exc:
        _write_report(args.report, exc.report)
        print(json.dumps(exc.report, indent=2, sort_keys=True))
        print(str(exc), file=sys.stderr)
        return 1
    except (AcceptanceError, OSError, json.JSONDecodeError) as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "blocked",
            "error_type": type(exc).__name__,
        }
        _write_report(args.report, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        print(f"OPC_LIFECYCLE_ACCEPTANCE_BLOCKED: {exc}", file=sys.stderr)
        return 2
    _write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
