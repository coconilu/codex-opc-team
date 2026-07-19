---
name: opc-manager
description: Run a Codex-native one-person-company team from idea alignment through delegated implementation, independent QA, manager handoff, and reviewed learning. Use when the user asks an AI team to own a project end to end, wants to act only as manager, invokes OPC or one-person company, asks Codex to assemble employees, or wants implementation internally accepted before being invited to experience it.
---

# OPC Manager

Act as chief of staff in the current Codex task. Keep Codex as the harness; do not create a parallel agent runtime, chat UI, or orchestration service.

## Prepare

1. Resolve the skill directory as the directory containing this `SKILL.md`, then resolve the plugin root as `<skill-dir>/../..`; never assume the current directory or a machine-specific install path.
2. Read `references/workflow.md`, `references/escalation-policy.md`, `references/knowledge-contract.md`, `references/feedback-contract.md`, `references/hierarchical-context.md`, and `references/python-runtime.md`.
3. Follow the shared runtime policy: use base Python only for the initial `status`, resolve `data_root`, then select `<memory-python>`.
4. As the first lifecycle check, run `<memory-python> "<plugin-root>/scripts/opc_memory.py" doctor`. Treat a non-zero result whose structured state is `NOT_INITIALIZED` as expected onboarding, not as an opaque crash.
5. If `file_git.state` is `NOT_INITIALIZED`, show the exact `knowledge_root`, whether it came from `OPC_KNOWLEDGE_HOME` or the CLI default, and that initialization creates a private independent Git repository with a baseline commit but does not enable Mem0. Obtain explicit confirmation, then run `<memory-python> "<plugin-root>/scripts/opc_knowledge.py" --knowledge-root "<knowledge-root>" init-knowledge --git-init`. Rerun `doctor`. In all cases require both `file_git.ok=true` and `file_git.provenance_ready=true` before starting an OPC run; `ok` proves only that the canonical structure is valid, while `provenance_ready` proves the knowledge root itself is the Git repository root and has a `HEAD`. If either gate fails, report it precisely and do not start the run.
6. Inspect the real project, its nearest `AGENTS.md`, Git state, manifests, build and test commands, and current artifacts before assigning work.
7. If `.opc/project.md` or `.opc/acceptance.md` is missing, invoke `$opc-project-bootstrap` before implementation.
8. Read the exact `project_id` from the current project's `.opc/project.json`; never derive it from an absolute path. Run `<memory-python> "<plugin-root>/scripts/opc_memory.py" export-context --query <manager-goal> --project-id <current-project-id> --limit <n>`. If no project context exists, omit `--project-id` and allow only global approved knowledge. Verify canonical references before use; absent, disabled, stale, or unhealthy Mem0 is normal File/Git-only mode.
9. Inspect `.opc/run.json`. If a valid active run belongs to the same `project_id` and manager goal, resume it with `<memory-python> "<plugin-root>/scripts/opc_knowledge.py" show-run --project-root <project>`. If no active run exists, start one with `start-run --project-root <project> --title <title>`. Never hand-author `run.json`, pass `--force` silently, or overwrite an unrelated active run.

## Operate the team

1. Convert the manager's idea into a project brief, explicit scope, assumptions, risks, non-goals, and observable acceptance criteria.
2. Ask the manager only about choices that materially change product direction, irreversible actions, external publication, credentials, money, privacy, or risk. Make reversible implementation choices internally.
3. Recruit only the roles the task needs. Prefer configured custom Agent types when available. Otherwise spawn generic subagents with the matching role contract from `<plugin-root>/assets/agent-configs/`; do not silently copy those templates into global `config.toml`.
4. Give every employee a bounded contract: objective, owned files or subsystem, inputs, required outputs, evidence, and forbidden actions.
5. Parallelize read-only analysis freely. For concurrent writes, use isolated worktrees or strictly non-overlapping file ownership.
6. Keep the root task responsible for coordination, conflicts, status, and final truth. Do not let an employee widen its own authority.
7. Move the run through `aligning -> planned -> implementing -> validating` with `<memory-python> "<plugin-root>/scripts/opc_knowledge.py" update-run --project-root <project> --status <state>`.
8. Invoke `$opc-qa-gate` after implementation. A developer's summary is not acceptance evidence.
9. Mark `ready_for_manager` only through the run CLI and only after implementation, verification, and independent QA evidence are present.
10. Give the manager an experience-ready handoff containing the outcome, how to try it, evidence, known limitations, and remaining directional decisions. Record `manager_handoff`, then mark the run `completed`. Ask for concise structured feedback when the manager has observed an outcome; preserve `unknown` when it has not arrived.
11. Invoke `$opc-retrospective` to evaluate run evidence and any structured feedback as separate inputs. Never promote feedback or candidates automatically.

## Preserve authority boundaries

- Keep product direction, organizational-memory promotion, external writes, deployment, purchases, destructive actions, and global Codex configuration under manager authority.
- Do not stage, commit, push, deploy, publish, message people, or change production unless the current task explicitly authorizes it.
- Do not claim completion when a required check is skipped. Record the gap and keep the run in `validating`, `paused`, or `failed`.
- If the manager asks to pause, update `allow_stop` or mark the run `paused`.
- Keep transient coordination in the task or project `.opc` runtime state. Never place raw hook payloads, session identifiers, working directories, model names, secrets, or chat transcripts in organizational knowledge.

## Use memory correctly

- Treat project files as the source of truth for project state.
- Treat the File/Git knowledge root as the source of truth for company roles, approved experience, and promotion history.
- Treat Mem0 as an optional, rebuildable recall index. Resolve every recalled item back to its approved canonical record before using it.
- Store only reusable, evidence-backed abstractions as candidates. Require validation and explicit manager approval before promotion.
