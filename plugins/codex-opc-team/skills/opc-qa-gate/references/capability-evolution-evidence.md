# Capability evolution evidence

For a capability proposal, independently verify:

1. Proposal and private record pass the published strict schemas and bind the current contract hash.
2. Current/candidate/rollback versions resolve to exact Git commits and blob hashes; the candidate commit range changes exactly one allowlisted target.
3. Pilot and promotion each have distinct explicit manager approval, independent QA, and replay/Shadow evidence refs under the private `.opc` boundary.
4. Every paired run uses the same exact evaluation contract and records control/candidate capability versions, all knowledge versions, and hashed lineage refs.
5. Quality, safety, manager intervention, context cost, latency, confounders, failed/timeout/unavailable states, and inconclusive outcomes are preserved. Neutral, missing, conflicting, unsafe, or regressed evidence is not promotable.
6. `transition-preview` writes nothing. Apply changes one unstaged path only and restores on failure; it never touches global Codex roles/features/hooks/config, stages, commits, pushes, or merges.
7. `confirm` verifies the complete base-to-HEAD range and exact blob. Rollback restores behavior while preserving history and approved knowledge.

Do not accept implementer self-report as QA. Report `association/evidence only`; do not infer causality from paired runs.
