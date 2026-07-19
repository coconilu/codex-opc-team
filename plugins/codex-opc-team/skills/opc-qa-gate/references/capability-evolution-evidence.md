# Capability evolution evidence

For a capability proposal, independently verify:

1. Proposal and private record pass the published strict schemas and bind the current contract hash.
2. Current/candidate/rollback versions resolve to regular Git blobs in mode `100644` or `100755`. Inspect every commit in the strict linear range; reject merges, symlink/gitlink/tree objects, type changes, renames/copies, empty commits, source-history resets, and intermediate non-target paths.
3. Every private ref resolves to the strict evidence envelope and binds the exact proposal, target, current/candidate versions, optional run, complete pilot/lineage set, decision, safety verdict, and bounded timestamp. Pilot and promotion require manager `approved`, independent QA `pass`, and Shadow `beneficial` plus `safe`.
4. Every paired run uses the same exact evaluation contract and records control/candidate capability versions, all knowledge versions, and hashed lineage refs.
5. Quality, safety, manager intervention, context cost, latency, and confounders are preserved only for completed arms. Failed/timeout/unavailable arms require `measurements=null` and an exact reason and are excluded from aggregation. Neutral, missing, conflicting, unsafe, or regressed evidence is not promotable.
6. `transition-preview` writes nothing. Apply changes one unstaged path only and restores on failure; it never touches global Codex roles/features/hooks/config, stages, commits, pushes, or merges.
7. `evaluate`, promotion, and `confirm` re-read all cumulative source, pilot authorization, pilot lineage, evaluation, QA, and Shadow evidence. `confirm` verifies a newly created strict descendant commit and exact regular blob. Rollback creates a new rollback commit while preserving history and approved knowledge.

Do not accept implementer self-report as QA. Report `association/evidence only`; do not infer causality from paired runs.
