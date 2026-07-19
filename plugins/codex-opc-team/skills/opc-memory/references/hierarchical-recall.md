# Hierarchical recall operations

The hierarchical index is derived data separate from Mem0. It lives at `<private-data-root>/.opc/derived/hierarchical-recall-v1/index.json`; the runtime rejects a data root inside any Git worktree, plugin tree, or canonical knowledge.

| Intent | Command suffix | Rule |
|---|---|---|
| Status | `status` | Read-only |
| Preview build | `index-preview` | Zero writes |
| Build | `index-build --approval-token <exact-token>` | Separate approval; atomic private publish |
| Query | `query <text> --project-id <id> --role <role> --budget-tokens <n>` | L0/L1 navigation, bounded L2 reads |
| Preview delete | `index-delete-preview` | Zero writes |
| Delete | `index-delete --approval-token <exact-token>` | Deletes derived index only |

Prefix commands with `<memory-python> "<plugin-root>/scripts/opc_hierarchical.py" --knowledge-root <knowledge-root> --data-root <private-data-root>`. Missing/invalid/stale index falls back to flat File/Git. Provider failure, timeout, disabled state or disagreement falls back to File hierarchy. Never describe L0/L1 as knowledge facts and never rebuild/approve/write Provider during query.
