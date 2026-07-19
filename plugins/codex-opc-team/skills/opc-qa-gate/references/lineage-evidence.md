# Lineage evidence checks

Lineage is supporting audit evidence, not independent QA by itself.

1. Read the machine view from `opc_lineage.py show`, then independently render `report` from current File/Git.
2. Require exact Packet/Trace version+hash and full citation provenance for every claimed recalled/injected revision and exact role/step identity; reject ID-only revision matching.
3. Verify `recalled`, `injected`, and `adopted` remain distinct; lack of an adoption event is not adoption.
4. Require current HEAD/status/scope/hash/relations revalidation and fail any claim that labels stale, obsolete, cross-project, conflict, or missing evidence usable.
5. Require evidence refs only on association events, then resolve QA/feedback/outcome/shadow/evaluation refs to existing bounded single-link `.opc` files; do not accept copied prose.
6. Require explicit provider degraded/no-memory events and continued File/Git operation.
7. Require the exact phrase `association/evidence only`, confounders, and unknowns; reject causal language unsupported by a controlled comparison.
8. Require the preview token to bind exact project/run IDs and instance hashes, and require the entire untracked `.opc/lineage/` directory—not only the final JSON—to be ignored so final/lock/pending/backup remain private.
9. Require record-time process-local binding of the project root, `.opc`, and lineage directory object from internal preview through cleanup. Same-byte directory replacement, rename, symlink/junction/reparse, or ancestor identity drift must fail closed without publishing, while equivalent Windows 8.3 aliases remain valid. Filesystem tokens, handles, absolute paths, and runtime identifiers must not appear in plan, record, view, report, or logs, and all handles must be released on every exit path.

Do not write or repair product artifacts while acting as independent QA. A missing v0.1 sidecar means lineage unavailable, not FAIL or PASS.
