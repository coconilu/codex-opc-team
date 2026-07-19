---
name: opc-memory-curator
description: Review pending OPC organizational experience, verify provenance and conflicts, validate it through replay or shadow evidence, and approve, reject, supersede, narrow, or obsolete it only with explicit manager approval. Use when reviewing experience candidates, maintaining company knowledge, updating reusable team rules, or deciding whether feedback belongs in a Skill, AGENTS rule, role version, or approved experience.
---

# OPC Memory Curator

Protect organizational memory from automatic accumulation.

## Workflow

1. Resolve the skill directory as the directory containing this `SKILL.md`, then resolve the plugin root as `<skill-dir>/../..`; read `references/promotion-policy.md`, `references/relation-and-migration-policy.md`, `references/capability-evolution.md`, and `references/python-runtime.md`.
2. Use base Python for the initial memory `status`, select `<memory-python>`, and run `<memory-python> "<plugin-root>/scripts/opc_memory.py" doctor`. Continue only when both `file_git.ok=true` and `file_git.provenance_ready=true`; `ok` covers canonical structure only, while `provenance_ready` requires the knowledge root itself to be the Git repository root with a `HEAD`. Stop on either failure.
3. List pending candidates with `<memory-python> "<plugin-root>/scripts/opc_memory.py" list --status candidate` and resolve each canonical record from File/Git.
4. Verify source, evidence, scope, owner, confidence, duplicates, applicability, sensitivity, conflicts, invalidation, and supersession links. Use `query-context` diagnostics; never choose a conflict winner from Provider rank.
5. Choose the destination: project `AGENTS.md` for a project-only recurring rule; a Skill for a reusable procedure; a role version for durable responsibility or authority; approved experience for contextual recall.
6. Replay the lesson on historical or synthetic cases, or run it in shadow mode without changing authority. If the destination is a role, Skill, or organization policy, use the separate evidence-gated capability lifecycle; knowledge approval alone never activates that version.
7. Present one proposed transition, its candidate, evidence, validation, destination, privacy impact, and rollback to the manager. Treat explicit approval of that transition as commit authorization only for its exact canonical old/new paths.
8. If the record is Schema 1, first run a zero-write `migrate-schema --dry-run` with an external private backup root, obtain approval for one exact record/token, and apply it. For status, relation, applicability, or sensitivity changes, run `curate <id> --dry-run` with the complete proposal; apply only the exact manager-approved proposal and unchanged plan token. Neither migration nor curation writes the Provider or commits Git.
9. Use only the exact `transition_paths`/`git_stage_pathspecs` returned by the applied plan. From `knowledge_root`, show `git status --short -- <transition-paths>` and `git diff -- <transition-paths>`. Stage and inspect only those individually quoted paths; use no directory or wildcard pathspecs, and never stage unrelated user changes.
10. Commit only `<stage-paths>` with `git commit --only -m "memory: <transition> <id>" -- <stage-paths>`. Verify the resulting commit contains no other path. If Git is unavailable, the knowledge root is not a repository, the new path is missing, or staging/commit/verification fails, report `provenance pending`; do not claim the transition published or indexed.
11. Only after the transition commit succeeds, run `<memory-python> "<plugin-root>/scripts/opc_memory.py" reindex --dry-run`. If the plan contains `UNCOMMITTED_APPROVED_SOURCE`, stop: the approved canonical file has no verifiable `source_commit`, so fix and verify the exact Git commit before retrying. Present a clean plan and obtain separate approval before explicitly running `reindex --apply`; never use `--force`, call the provider directly, or clear the provider index merely because canonical knowledge changed.

Do not infer memory approval from product or code approval. Never silently rewrite `AGENTS.md`, Skills, role templates, global `config.toml`, or approved history.
