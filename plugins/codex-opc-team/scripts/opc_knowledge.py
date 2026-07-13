#!/usr/bin/env python3
"""Safe OPC project-run lifecycle and compatibility CLI.

Project-local runtime state lives under ``.opc``.  Durable organizational
experience is delegated to :mod:`opc_memory` through reviewed retrospective
candidates; active run snapshots are never copied into the knowledge root.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from opc_memory import (
    FileGitBackend,
    MemoryService,
    OpcMemoryError,
    atomic_write_json,
    load_json,
    parse_pairs,
    resolve_data_root,
    resolve_knowledge_root,
    utc_now,
    validate_private_root_against_plugin,
)


SCHEMA_VERSION = 1
RUN_STATUSES = {
    "aligning",
    "planned",
    "implementing",
    "validating",
    "ready_for_manager",
    "completed",
    "paused",
    "failed",
}
READY_EVIDENCE = {"implementation", "verification", "qa"}
TERMINAL_STATUSES = {"completed", "paused", "failed"}
SAFE_PROJECT_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
RUN_TTL_HOURS = 24 * 7


class OpcError(OpcMemoryError):
    """A user-actionable OPC lifecycle error."""


def resolve_project_root(value: str | None = None) -> Path:
    return Path(value or os.getcwd()).expanduser().resolve()


def _portable_id(value: str, label: str = "project_id") -> str:
    if not value or any(character not in SAFE_PROJECT_ID for character in value):
        raise OpcError(f"{label} must contain only letters, digits, dot, underscore, or hyphen")
    return value


def project_file(project_root: Path) -> Path:
    return project_root / ".opc" / "project.json"


def run_path(project_root: Path) -> Path:
    return project_root / ".opc" / "run.json"


def find_run_path(start: Path) -> Path | None:
    """Find the nearest project run without creating files or logging."""
    current = start.expanduser().resolve()
    while True:
        candidate = current / ".opc" / "run.json"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def _template_root() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "knowledge-template"


def _copy_template(source: Path, destination: Path, *, force: bool) -> list[str]:
    copied: list[str] = []
    if not source.is_dir():
        raise OpcError(f"Knowledge template not found: {source}")
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(relative.as_posix())
    return copied


def init_knowledge(
    *,
    root: Path,
    template: Path | None = None,
    force: bool = False,
    git_init: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    validate_private_root_against_plugin(root, label="knowledge_root")
    bootstrap_marker = root / ".opc-bootstrap-state.json"
    root_existed = root.exists()
    root_had_content = root_existed and any(root.iterdir())
    bootstrap_recovery = False
    if bootstrap_marker.exists():
        try:
            marker = load_json(bootstrap_marker)
        except OpcMemoryError as exc:
            raise OpcError(
                f"Invalid OPC bootstrap recovery marker: {bootstrap_marker}"
            ) from exc
        bootstrap_recovery = (
            marker.get("schema_version") == SCHEMA_VERSION
            and marker.get("owner") == "codex-opc-team"
            and marker.get("operation") == "init-knowledge"
        )
        if not bootstrap_recovery:
            raise OpcError(
                f"Unrecognized bootstrap marker; refusing automatic recovery: {bootstrap_marker}"
            )
    root.mkdir(parents=True, exist_ok=True)
    bootstrap_owned = bool(git_init and (not root_had_content or bootstrap_recovery))
    if bootstrap_owned:
        atomic_write_json(
            bootstrap_marker,
            {
                "schema_version": SCHEMA_VERSION,
                "owner": "codex-opc-team",
                "operation": "init-knowledge",
                "state": "initializing",
                "updated_at": utc_now(),
            },
        )
    copied = _copy_template(template or _template_root(), root, force=force)
    backend = FileGitBackend(root)
    backend.ensure_layout()
    for relative in ("evaluations/runs", "evaluations/events", "promotions"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    git_initialized = False
    git_baseline_commit: str | None = None
    git_preserved = False
    git_recovered = False
    git_skipped_reason: str | None = None
    quiet = {
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 10,
    }
    existing_head: str | None = None
    if git_init and (root / ".git").exists():
        try:
            existing_head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                **quiet,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            existing_head = None

    if git_init and existing_head:
        git_baseline_commit = existing_head
        if bootstrap_recovery:
            bootstrap_marker.unlink(missing_ok=True)
            git_initialized = True
            git_recovered = True
        else:
            git_preserved = True
    elif git_init and not bootstrap_owned:
        git_skipped_reason = (
            "Existing knowledge content without an OPC bootstrap marker was preserved "
            "without automatic Git initialization or commit."
        )
    elif git_init and bootstrap_owned:
        try:
            subprocess.run(
                ["git", "-C", str(root), "init", "-b", "main"],
                **quiet,
            )
            subprocess.run(
                ["git", "-C", str(root), "add", "-A", "--", "."],
                **quiet,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rm",
                    "--cached",
                    "--ignore-unmatch",
                    "--",
                    bootstrap_marker.name,
                ],
                **quiet,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=OPC Knowledge Bootstrap",
                    "-c",
                    "user.email=opc-knowledge@users.noreply.github.com",
                    "commit",
                    "-m",
                    "chore: initialize private OPC knowledge",
                ],
                **quiet,
            )
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                **quiet,
            ).stdout.strip()
            bootstrap_marker.unlink(missing_ok=True)
            git_initialized = True
            git_recovered = bootstrap_recovery
            git_baseline_commit = commit
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            atomic_write_json(
                bootstrap_marker,
                {
                    "schema_version": SCHEMA_VERSION,
                    "owner": "codex-opc-team",
                    "operation": "init-knowledge",
                    "state": "git_failed",
                    "error_type": type(exc).__name__,
                    "updated_at": utc_now(),
                },
            )
            raise OpcError(f"Could not initialize the Git baseline: {exc}") from exc
    return {
        "knowledge_root": str(root),
        "copied": copied,
        "git_initialized": git_initialized,
        "git_baseline_commit": git_baseline_commit,
        "git_preserved": git_preserved,
        "git_recovered": git_recovered,
        "git_skipped_reason": git_skipped_reason,
        "note": "Knowledge remains private and independent from the public plugin repository.",
    }


def init_project(
    *, project_root: Path, project_id: str | None = None, name: str | None = None
) -> dict[str, Any]:
    project_root.mkdir(parents=True, exist_ok=True)
    opc_dir = project_root / ".opc"
    opc_dir.mkdir(parents=True, exist_ok=True)
    existing_path = project_file(project_root)
    if existing_path.exists():
        existing = load_json(existing_path)
        if project_id and existing.get("project_id") != project_id:
            raise OpcError(
                f"Project already initialized with project_id={existing.get('project_id')}"
            )
        return existing
    selected_id = _portable_id(project_id or project_root.name)
    now = utc_now()
    project = {
        "schema_version": SCHEMA_VERSION,
        "project_id": selected_id,
        "name": (name or project_root.name).strip(),
        "created_at": now,
        "updated_at": now,
    }
    atomic_write_json(existing_path, project)
    for relative in ("qa", "handoff"):
        (opc_dir / relative).mkdir(parents=True, exist_ok=True)
    return project


def _load_project(project_root: Path) -> dict[str, Any]:
    path = project_file(project_root)
    if not path.exists():
        return init_project(project_root=project_root)
    project = load_json(path)
    _portable_id(str(project.get("project_id", "")))
    return project


def start_run(
    *,
    root: Path,
    project_root: Path,
    title: str,
    manager: str = "user",
    force: bool = False,
) -> dict[str, Any]:
    if not title.strip():
        raise OpcError("Run title must be non-empty")
    path = run_path(project_root)
    if path.exists() and load_json(path).get("active") and not force:
        raise OpcError(f"An active OPC run already exists at {path}; pause or complete it first")
    project = _load_project(project_root)
    now = utc_now()
    run_id = f"opc-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "title": title.strip(),
        "project_id": project["project_id"],
        "manager": manager.strip() or "user",
        "status": "aligning",
        "active": True,
        "allow_stop": False,
        "team": [],
        "acceptance_criteria": [],
        "evidence": {},
        "notes": [],
        "created_at": now,
        "updated_at": now,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=RUN_TTL_HOURS)
        ).isoformat().replace("+00:00", "Z"),
    }
    atomic_write_json(path, run)
    return run


def validate_transition(run: Mapping[str, Any], status: str) -> None:
    if status not in RUN_STATUSES:
        raise OpcError(f"Unsupported status: {status}")
    evidence = run.get("evidence", {})
    if not isinstance(evidence, dict):
        raise OpcError("Run evidence must be an object")
    if status in {"ready_for_manager", "completed"}:
        missing = sorted(key for key in READY_EVIDENCE if not evidence.get(key))
        if missing:
            raise OpcError(
                "Cannot mark the run ready without evidence for: " + ", ".join(missing)
            )
    if status == "completed" and not evidence.get("manager_handoff"):
        raise OpcError("Cannot complete the run before manager_handoff evidence is recorded")


def update_run(
    *,
    root: Path,
    project_root: Path,
    status: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    note: str | None = None,
    allow_stop: bool | None = None,
) -> dict[str, Any]:
    path = run_path(project_root)
    run = load_json(path)
    if evidence:
        normalized = {key: str(value) for key, value in evidence.items()}
        run.setdefault("evidence", {}).update(normalized)
    if note:
        run.setdefault("notes", []).append(note.strip())
    if allow_stop is not None:
        run["allow_stop"] = allow_stop
    if status:
        validate_transition(run, status)
        run["status"] = status
        run["active"] = status not in TERMINAL_STATUSES
    run["updated_at"] = utc_now()
    if run.get("active") is True:
        run["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=RUN_TTL_HOURS)
        ).isoformat().replace("+00:00", "Z")
    atomic_write_json(path, run)
    return run


def doctor(root: Path, project_root: Path | None = None) -> dict[str, Any]:
    memory = FileGitBackend(root).doctor()
    project_report: dict[str, Any] | None = None
    if project_root:
        problems: list[str] = []
        try:
            project = load_json(project_file(project_root))
            _portable_id(str(project.get("project_id", "")))
            if run_path(project_root).exists():
                run = load_json(run_path(project_root))
                if run.get("project_id") != project.get("project_id"):
                    problems.append("run.project_id does not match project.project_id")
                if run.get("status") not in RUN_STATUSES:
                    problems.append("run.status is invalid")
        except OpcMemoryError as exc:
            problems.append(str(exc))
        project_report = {
            "project_id": project.get("project_id") if "project" in locals() else None,
            "ok": not problems,
            "problems": problems,
        }
    return {
        "ok": memory["ok"] and (project_report is None or project_report["ok"]),
        "knowledge": memory,
        "project": project_report,
    }


def _memory_service(args: argparse.Namespace) -> MemoryService:
    return MemoryService.from_paths(
        resolve_knowledge_root(args.knowledge_root), resolve_data_root(args.data_root)
    )


def _add_candidate(service: MemoryService, args: argparse.Namespace) -> dict[str, Any]:
    project_root = resolve_project_root(args.project_root)
    project = _load_project(project_root)
    current_run = load_json(run_path(project_root)) if run_path(project_root).exists() else None
    source = args.source or (current_run.get("run_id") if current_run else None)
    project_id = str(project["project_id"]) if args.scope.strip().lower() == "project" else None
    return service.add_candidate(
        memory_type=args.experience_type,
        summary=args.summary,
        content=args.content,
        scope=args.scope,
        owner=args.owner,
        source=source,
        confidence=args.confidence,
        evidence=parse_pairs(args.evidence),
        metadata=parse_pairs(args.metadata),
        keywords=args.keyword,
        project_id=project_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-root")
    parser.add_argument("--data-root")
    commands = parser.add_subparsers(dest="command", required=True)

    init_knowledge_parser = commands.add_parser("init-knowledge")
    init_knowledge_parser.add_argument("--template")
    init_knowledge_parser.add_argument("--force", action="store_true")
    init_knowledge_parser.add_argument("--git-init", action="store_true")

    init_project_parser = commands.add_parser("init-project")
    init_project_parser.add_argument("--project-root", required=True)
    init_project_parser.add_argument("--project-id")
    init_project_parser.add_argument("--name")

    start = commands.add_parser("start-run")
    start.add_argument("--project-root", required=True)
    start.add_argument("--title", required=True)
    start.add_argument("--manager", default="user")
    start.add_argument("--force", action="store_true")

    show = commands.add_parser("show-run")
    show.add_argument("--project-root", required=True)

    update = commands.add_parser("update-run")
    update.add_argument("--project-root", required=True)
    update.add_argument("--status", choices=sorted(RUN_STATUSES))
    update.add_argument("--evidence", action="append", default=[])
    update.add_argument("--note")
    update.add_argument("--allow-stop", choices=("true", "false"))

    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("--project-root")

    for command in ("candidate", "add-candidate"):
        candidate = commands.add_parser(command)
        candidate.add_argument("--project-root", required=True)
        candidate.add_argument("--type", dest="experience_type", required=True)
        candidate.add_argument("--summary", required=True)
        content = candidate.add_mutually_exclusive_group(required=True)
        content.add_argument("--content")
        content.add_argument("--lesson", dest="content")
        candidate.add_argument("--scope", default="project")
        candidate.add_argument("--owner", default="opc-team")
        candidate.add_argument("--source")
        candidate.add_argument("--confidence", type=float, default=0.5)
        candidate.add_argument("--evidence", action="append", default=[])
        candidate.add_argument("--metadata", action="append", default=[])
        candidate.add_argument("--keyword", action="append", default=[])

    for command in ("approve", "promote-candidate"):
        approve = commands.add_parser(command)
        approve.add_argument("record_id")
        approve.add_argument("--approved-by", required=True)
        approve.add_argument("--validation", required=True)

    for command in ("reject", "reject-candidate"):
        reject = commands.add_parser(command)
        reject.add_argument("record_id")
        reject.add_argument("--rejected-by", required=True)
        reject.add_argument("--reason", required=True)

    for command in ("list", "list-candidates"):
        listing = commands.add_parser(command)
        listing.add_argument(
            "--status", choices=("candidate", "approved", "rejected", "obsolete"), default="candidate"
        )
        listing.add_argument("--type", dest="memory_type")
        listing.add_argument("--limit", type=int, default=100)
    return parser


def _list_status(
    backend: FileGitBackend,
    status: str,
    memory_type: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    folder = backend.root / {
        "candidate": "experiences/candidates",
        "approved": "experiences/approved",
        "rejected": "experiences/rejected",
        "obsolete": "experiences/obsolete",
    }[status]
    for path in sorted(folder.glob("*.json")):
        record = load_json(path)
        if memory_type and record.get("type") != memory_type:
            continue
        record["_source_path"] = path.relative_to(backend.root).as_posix()
        records.append(record)
        if len(records) >= limit:
            break
    return records


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = resolve_knowledge_root(args.knowledge_root)
    try:
        if args.command == "init-knowledge":
            result: Any = init_knowledge(
                root=root,
                template=Path(args.template).expanduser().resolve() if args.template else None,
                force=args.force,
                git_init=args.git_init,
            )
        elif args.command == "init-project":
            result = init_project(
                project_root=resolve_project_root(args.project_root),
                project_id=args.project_id,
                name=args.name,
            )
        elif args.command == "start-run":
            result = start_run(
                root=root,
                project_root=resolve_project_root(args.project_root),
                title=args.title,
                manager=args.manager,
                force=args.force,
            )
        elif args.command == "show-run":
            result = load_json(run_path(resolve_project_root(args.project_root)))
        elif args.command == "update-run":
            result = update_run(
                root=root,
                project_root=resolve_project_root(args.project_root),
                status=args.status,
                evidence=parse_pairs(args.evidence),
                note=args.note,
                allow_stop=None if args.allow_stop is None else args.allow_stop == "true",
            )
        elif args.command == "doctor":
            result = doctor(
                root,
                resolve_project_root(args.project_root) if args.project_root else None,
            )
        elif args.command in {"candidate", "add-candidate"}:
            result = _add_candidate(_memory_service(args), args)
        elif args.command in {"approve", "promote-candidate"}:
            result = _memory_service(args).approve(
                args.record_id,
                approved_by=args.approved_by,
                validation=args.validation,
            )
        elif args.command in {"reject", "reject-candidate"}:
            result = _memory_service(args).reject(
                args.record_id,
                rejected_by=args.rejected_by,
                reason=args.reason,
            )
        elif args.command in {"list", "list-candidates"}:
            result = _list_status(
                FileGitBackend(root), args.status, args.memory_type, args.limit
            )
        else:
            parser.error(f"Unhandled command: {args.command}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "doctor" and not result["ok"]:
            return 1
        return 0
    except (OpcMemoryError, OSError, ValueError) as exc:
        print(f"OPC_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
