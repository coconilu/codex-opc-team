---
name: opc-memory
description: Inspect, diagnose, configure, reindex, disable, uninstall, or safely purge the optional OPC memory recall layer while preserving File/Git canonical knowledge. Use when the user asks about OPC memory or Mem0, wants status or doctor checks, wants guided isolated Mem0 setup, needs a reindex preview or apply, wants to disable or uninstall recall, or requests deletion of memory-related data.
---

# OPC Memory

Manage recall without weakening the canonical File/Git knowledge boundary. Mem0 is optional, derived, and rebuildable; the team must continue working when it is absent or unhealthy.

## Establish the boundary

1. Resolve the skill directory as the directory containing this `SKILL.md`, then resolve the plugin root as `<skill-dir>/../..`; never assume a machine-specific install path.
2. Read `references/python-runtime.md`, `references/hierarchical-recall.md`, and `references/knowledge-lineage.md`. Use base Python for the initial `status`, derive `data_root`, and select `<memory-python>` before every other memory command.
3. Use `<memory-python> "<plugin-root>/scripts/opc_memory.py"`. Run `--help` when the installed CLI version is uncertain.
4. Resolve canonical knowledge from `--knowledge-root`, then `OPC_KNOWLEDGE_HOME`, then the CLI default.
5. Resolve provider configuration, outbox, virtual environment, and rebuildable indexes from `--data-root`, then `OPC_MEMORY_DATA_HOME`, then `PLUGIN_DATA/opc-memory`.
6. Never put provider data or its virtual environment in the plugin root/cache, project repository, or canonical knowledge repository. Never print or persist credentials.
7. Resolve recalled records back to an approved canonical relative `source_path` and matching content hash before using them. Treat stale, missing, or rejected records as unusable.
8. For `query` or `export-context` in a project, read `project_id` from `.opc/project.json` and pass `--project-id <current-project-id>`. Without project context, omit it and return global records only; never infer scope from an absolute path.
9. Require `knowledge_root`, private `data_root`, and the installed plugin tree to be pairwise non-overlapping. Treat `ROOT_ISOLATION_ERROR` as a hard stop, including during dry-run.
10. Read the Git audit in `status` or `doctor`: report repository root, HEAD, `provenance_ready`, dirty/staged/untracked paths, `UNCOMMITTED_KNOWLEDGE`, and separate `LEGACY_RUNTIME_ARTIFACTS`. A structurally valid `file_git.ok=true` is not enough to index or start managed work unless `file_git.provenance_ready=true`. Never stage or commit unrelated user changes.

## Choose the safe operation

| User intent | CLI operation | Mutation and approval rule |
|---|---|---|
| Show current mode | `status` | Read-only; run directly |
| Diagnose layout/provider | `doctor` | Read-only; run directly and separate warnings from failures |
| Preview legacy events | `legacy-events --dry-run` | Default and read-only; follow `references/legacy-runtime-events.md` |
| Archive legacy events | `legacy-events --apply --plan-token <preview-token>` | Requires separate approval of the unchanged preview; moves only eligible files |
| Preview setup | `setup --enable-mem0 --dry-run` | Default; show paths, dependencies, network, and credential needs |
| Apply setup | `setup --enable-mem0 --apply` | Obtain explicit approval after preview if packages, downloads, credentials, or persistent config are involved |
| Preview incremental rebuild | `reindex --dry-run [--limit N]` | Default and read-only; report approved records that differ from local derived state |
| Apply incremental rebuild | `reindex --apply [--limit N]` | Requires explicit approval; only writes approved records to an enabled provider and records successful provenance locally |
| Preview full rebuild | `reindex --dry-run --force [--limit N]` | Read-only; use only after the provider index is known to be missing or cleared |
| Apply full rebuild | `reindex --apply --force [--limit N]` | Requires explicit approval that the old provider index is gone; otherwise it can create duplicate provider entries |
| Disable recall | `disable --dry-run`, then `disable --apply` | Preview first; preserve canonical knowledge and index data for rollback |
| Uninstall guidance | `uninstall` | Informational only; never delete knowledge, dependencies, or data automatically |
| Purge data | No CLI command by design | Treat as destructive; follow the audited manual workflow below |

Prefix table operations with `<memory-python> "<plugin-root>/scripts/opc_memory.py"`. Pass `--knowledge-root`, `--data-root`, and `--timeout` before the subcommand. Do not invent a `purge` or destructive uninstall flag. `reindex` defaults to preview; apply and force must always be explicit.

When `status` or `doctor` reports `LEGACY_RUNTIME_ARTIFACTS`, read and follow `references/legacy-runtime-events.md`. Never open an event file to decide how to classify or remediate it.

## Guide setup

1. Resolve `<base-python>`, `data_root`, and `<memory-python>` through `references/python-runtime.md`; then run `doctor`.
2. If `file_git.state` is `NOT_INITIALIZED`, show the exact knowledge root and its environment/default source, including that `--git-init` creates a private repository and baseline commit. Obtain separate approval, run `<memory-python> "<plugin-root>/scripts/opc_knowledge.py" --knowledge-root "<knowledge-root>" init-knowledge --git-init`, and require a successful `doctor` before Mem0 setup. Do not let provider setup silently replace canonical initialization.
3. Run `setup --enable-mem0 --dry-run` with `<memory-python>`. Present the exact `data_root`, `<data_root>/venv`, pinned requirements file, download/network impact, credential variables, and fallback behavior.
4. Verify `data_root` is outside the plugin root/cache, project repository, and canonical knowledge root. Explain that File/Git remains complete without Mem0 and enabling it changes recall quality, not authority.
5. Obtain explicit approval for the exact virtual-environment path, installing the pinned `mem0ai==2.0.11` and `httpx[socks]==0.28.1` requirements, network downloads, and persistent provider configuration.
6. After approval, create only `<data_root>/venv` with `<base-python> -m venv "<data_root>/venv"`. Do not delete or recreate an existing environment automatically.
7. Install only from the pinned file with the platform venv Python: PowerShell uses `& "<data_root>\venv\Scripts\python.exe" -m pip install -r "<plugin-root>\requirements-mem0.txt"`; Unix uses `"<data_root>/venv/bin/python" -m pip install -r "<plugin-root>/requirements-mem0.txt"`. Never invoke global `pip`, install into base Python, or modify the plugin cache.
8. From this point, set `<memory-python>` to `<venv-python>`. Run `setup --enable-mem0 --apply`, followed by `doctor` and `status`, with that interpreter.
9. Run `reindex --dry-run`, present pending and skipped approved records, and obtain separate explicit approval before `reindex --apply`. Never clear the provider index automatically.
   Approval only performs the canonical transition and returns `pending_commit`; it never writes Mem0. If `UNCOMMITTED_KNOWLEDGE` is present or `provenance_ready=false`, stop before apply; the memory curator must commit only the manager-approved transition paths, then rerun `doctor` and the preview so `source_commit` can be verified from HEAD.
10. Use `--force` only when the optional provider index was independently verified as deleted or reset while local derived state remains. Preview `reindex --dry-run --force`, explain duplicate risk, and obtain approval for that exact `reindex --apply --force` rebuild.
11. After apply, require `ok=true`, zero failures, and a query that resolves results back to canonical records before claiming the index is rebuilt. Provider errors and timeouts remain failures even when queued in the local outbox.
12. On provider failure, preserve the redacted error, keep or return to File/Git mode, and report degradation rather than blocking OPC work.

## Disable or uninstall

- Resolve `<memory-python>`, preview `disable`, show the changed configuration, obtain confirmation, then apply and rerun `status` with the same interpreter.
- Use `uninstall` only to display provider-specific removal guidance. Separate plugin removal, isolated venv removal, provider data deletion, and canonical knowledge retention as distinct choices.
- Never remove the user's File/Git knowledge root as part of disable, plugin uninstall, or Mem0 uninstall.
- Never edit global Codex `config.toml` silently.

## Handle purge requests

1. Run `status` and resolve the data root without following untrusted links or user-provided relative traversal.
2. Classify targets into provider configuration, rebuildable index/cache, outbox, and canonical knowledge.
3. Refuse any plan that includes the canonical knowledge root, approved records, promotion history, project files, or paths outside the resolved private data root.
4. Show exact absolute targets, expected impact, backup or rollback, and a dry inventory. Redact secrets from the report.
5. Require explicit approval for those exact targets. A previous approval to disable or uninstall is not purge approval.
6. Use platform-native safe deletion only after approval, then rerun `status` and `doctor`. If target containment cannot be proven, stop without deleting anything.

## Report results

Return a compact table with canonical mode, optional provider state, canonical root, private data root, validation result, fallback state, changes made, and user action still required. Do not expose secrets, raw embeddings, session IDs, turn IDs, working directories from unrelated projects, or model identifiers.
