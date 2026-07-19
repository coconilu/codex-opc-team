---
name: opc-shadow-evaluation
description: Preview, run, and report a privacy-safe control/treatment Shadow Evaluation for a File/Git knowledge candidate. Use when a manager or curator wants replay evidence about whether a candidate is beneficial, neutral, harmful, conflicting, stale, obsolete, or over-scoped before the separate governed promotion flow.
---

# OPC Shadow Evaluation

1. Resolve this `SKILL.md`, then `<plugin-root>` as `<skill-dir>/../..`; read `references/evaluation-contract.md` and `references/python-runtime.md`.
2. Resolve `<memory-python>` and run `<memory-python> "<plugin-root>/scripts/opc_memory.py" doctor`. Require File/Git structure, a Git repository rooted at the knowledge root, and a current `HEAD`; Shadow Evaluation never needs Mem0.
3. Select exactly one `candidate` record. Do not approve, reject, obsolete, edit, commit, reindex, or copy its content into the replay file.
4. Build an `opc-shadow-replay-v1` input outside the public plugin tree. Bind it to the candidate's portable ID, canonical relative path, exact current-HEAD commit, and SHA-256. Use only synthetic cases or a manager-approved private pilot with a portable approval reference.
5. Run `preview` first. Treat cross-project scope, non-candidate state, obsolete state, or stale provenance as a preflight rejection. Preview must list no writes.
6. Present the preview fingerprint. Continue only after the user confirms that exact preview and a pre-created private artifact root outside the plugin, canonical knowledge, and project source. Do not use a symlink, junction, reparse point, or linked ancestor.
7. Run `evaluate` with `--expected-preview-sha256`. Treat timeout, unavailable/erroring providers, zero denominators, conflicting measured results, or changed provenance as inconclusive/degraded. Never reinterpret them as support.
8. Use `report` to validate and render an existing uniquely linked machine result without mutation. It must match the current exact contracts and governed cross-field invariants. Preserve measured, human judgment, model inference, and unverified evidence as separate classes.
9. If evidence is beneficial, say only that it may be considered in a separate curation flow. The manager/curator must still preview and approve the canonical transition, commit the exact blob, then separately preview and approve any optional reindex.

Never place replay inputs or reports in public source, canonical knowledge, provider indexes, or Git history. Never expose credentials, raw conversation, Hook payloads, host paths, or matched sensitive values in output.
