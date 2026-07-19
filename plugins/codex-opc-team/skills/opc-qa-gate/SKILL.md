---
name: opc-qa-gate
description: Independently validate an OPC-managed implementation against its acceptance contract using real build, test, browser, runtime, artifact, and Git evidence before manager handoff. Use when implementation appears complete, before telling the user to experience a result, after an employee handoff, or when an OPC run is in validating state.
---

# OPC QA Gate

Act as an independent acceptance function. Do not trust implementation summaries as proof and do not modify product source while evaluating it.

## Workflow

1. Resolve the skill directory as the directory containing this `SKILL.md`, then resolve the plugin root as `<skill-dir>/../..`; read `references/evidence-standard.md`, `references/hierarchical-recall-evidence.md`, `references/lineage-evidence.md`, and `references/capability-evolution-evidence.md`.
2. Read `.opc/project.md`, `.opc/acceptance.md`, `.opc/run.json`, applicable `AGENTS.md`, and the real diff or artifact.
3. Derive a criterion-by-criterion evidence table before testing.
4. Run the smallest relevant checks first, then every broader check required by the acceptance contract.
5. Exercise the real critical flow in the relevant browser, viewer, runtime, or application; source inspection alone is not runtime evidence.
6. Separate pre-existing failures from regressions introduced by the current work.
7. If any required criterion lacks strong evidence, return the run to `implementing` with reproducible failure evidence and a bounded repair contract.
8. If every criterion passes, write a QA report under `.opc/qa/`. Use `python <plugin-root>/scripts/opc_knowledge.py update-run --project-root <project> --evidence implementation=<ref> --evidence verification=<ref> --evidence qa=<ref> --status ready_for_manager`.
9. Keep QA artifacts project-local. Do not place raw logs, screenshots with sensitive data, absolute paths, or hook payloads in organizational knowledge.

Never mark a skipped, blocked, stale, or indirectly inferred check as passed. Never mutate global Codex configuration while validating.
