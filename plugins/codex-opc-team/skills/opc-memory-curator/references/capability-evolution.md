# Capability evolution

Use `opc_evolution.py` only after curation identifies a role, Skill, or organization-policy destination. A knowledge approval does not approve a capability change.

1. Require a strict proposal with source candidate IDs, hashed private feedback/evaluation/lineage refs, one allowlisted capability path, exact current/candidate/rollback Git commits and blob hashes, scope, owner, and bounded pilot.
2. Keep `.opc/evolution/` private. In a Git project require the whole directory ignored and untracked; preview every write and apply only an unchanged plan token/revision/base hash.
3. Require replay or Shadow evidence, independent QA, and explicit manager approval before the pilot. Every paired case must use the same evaluation contract and record exact capability version, knowledge versions, and lineage ref for both arms.
4. Treat missing evidence, timeout, Provider failure, regression, scope leakage, privacy failure, and inconclusive results as non-promotable. Neutral is not beneficial.
5. Obtain a second explicit manager approval and independent QA for promotion. `transition` may produce one unstaged allowlisted target diff; inspect only its exact pathspec and never stage a directory, wildcard, unrelated change, or global Codex config.
6. The user must explicitly commit the exact target. `confirm` verifies that the complete base-to-HEAD range changed only that path and contains the previewed blob before it becomes active.
7. Observe the bounded promoted version. Roll back on regression or explicit manager decision using the same preview, one-path diff, explicit commit, and confirm flow. Preserve all history, evidence, and approved knowledge.

Reports state `association/evidence only`. A successful synthetic or private pilot supports a bounded decision; it does not prove causality or generalization.
