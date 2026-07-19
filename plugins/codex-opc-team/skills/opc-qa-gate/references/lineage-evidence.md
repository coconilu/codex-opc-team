# Lineage evidence checks

Lineage is supporting audit evidence, not independent QA by itself.

1. Read the machine view from `opc_lineage.py show`, then independently render `report` from current File/Git.
2. Require exact Packet/Trace version+hash and full citation provenance for every claimed recalled/injected revision and exact role/step identity; reject ID-only revision matching.
3. Verify `recalled`, `injected`, and `adopted` remain distinct; lack of an adoption event is not adoption.
4. Require current HEAD/status/scope/hash/relations revalidation and fail any claim that labels stale, obsolete, cross-project, conflict, or missing evidence usable.
5. Require evidence refs only on association events, then resolve QA/feedback/outcome/shadow/evaluation refs to existing bounded single-link `.opc` files; do not accept copied prose.
6. Require explicit provider degraded/no-memory events and continued File/Git operation.
7. Require the exact phrase `association/evidence only`, confounders, and unknowns; reject causal language unsupported by a controlled comparison.

Do not write or repair product artifacts while acting as independent QA. A missing v0.1 sidecar means lineage unavailable, not FAIL or PASS.
