# Knowledge-lineage operations

Lineage is separate from canonical File/Git, hierarchical derived indexes, and Mem0. It lives at `.opc/lineage/<run_id>.json`; in a Git worktree the entire untracked `.opc/lineage/` directory must be ignored so every transaction artifact stays private. It is not rebuilt by `reindex`.

| Operation | Rule |
|---|---|
| Preview event | Zero write; validate event, exact full-citation RecallResult, current canonical revision, association-only evidence refs, and base sidecar identity |
| Record event | Exact project/run subject + preview token + base-record/revision CAS; final/lock/pending/backup stay under an ignored, untracked lineage directory; Git/ignore uncertainty or subject drift fails closed |
| Show/report | Revalidate current HEAD/governance and evidence hashes before marking usable |
| Provider degraded/no-memory | Record explicit provider event; continue File/Git |
| Redact/expire | Delete the private derived sidecar under project policy; do not alter canonical knowledge |

Never place lineage in `OPC_KNOWLEDGE_HOME`, Mem0, the plugin tree, project source, or remote telemetry. A v0.1 run without a sidecar remains readable as `lineage unavailable`; do not invent a migration or default usage state.
