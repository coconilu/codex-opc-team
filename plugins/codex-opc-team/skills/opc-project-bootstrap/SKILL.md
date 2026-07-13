---
name: opc-project-bootstrap
description: Onboard a local repository or project into the Codex OPC team by creating portable project metadata, a small project brief, an acceptance contract, and scoped guidance without replacing existing conventions. Use when preparing an OPC-managed project, bringing an existing repository into the team, or when opc-manager finds no project contract or `.opc` project state.
---

# OPC Project Bootstrap

Create only the minimum project-local structure the team needs.

## Workflow

1. Resolve the skill directory as the directory containing this `SKILL.md`, then resolve the plugin root as `<skill-dir>/../..`; read `references/project-layout.md`.
2. Inspect the repository root, nearest `AGENTS.md`, README, manifests, build and test commands, and Git status.
3. Preserve all existing instructions and user changes. Do not replace `AGENTS.md`; propose a scoped addition and obtain approval before changing it.
4. Run `python <plugin-root>/scripts/opc_knowledge.py init-project --project-root <project> --project-id <portable-id> --name <name>` to create versionable `.opc/project.json` metadata.
5. Create or fill `.opc/project.md` and `.opc/acceptance.md` from the bundled assets and real repository evidence. Keep all three project contract files versionable and never replace a populated contract without review.
6. Ensure the repository `.gitignore` contains the exact lines from `assets/gitignore.snippet`: `/.opc/run.json`, `/.opc/events.jsonl`, `/.opc/events.jsonl.*`, and `/.opc/.opc-hook.lock`. Do not ignore the whole `.opc` directory or overwrite broader ignore rules.
7. Do not start a run or hand-author `run.json`; `$opc-manager` is the single owner that starts or resumes an OPC run after onboarding succeeds.
8. Keep future runtime output in project `.opc` or `PLUGIN_DATA`. Never write hook payloads or project-local runtime state into the File/Git organizational knowledge root.
9. Report created files, inferred commands, Git changes, readiness for `$opc-manager`, and any missing information that materially blocks alignment.

Do not create a server, database, dashboard, new framework, duplicated source tree, custom Agent registration, or global `config.toml` mutation during onboarding.
