# Organizational Knowledge Contract

| Layer | Purpose | Authority and lifetime |
|---|---|---|
| Task context | Current request and temporary coordination | Disposable |
| Project files and `AGENTS.md` | Project facts, commands, decisions, constraints | Project source of truth |
| Project `.opc` runtime | Active run marker, QA evidence, transient state | Local and project-scoped |
| File/Git knowledge root | Roles, evaluated experience, promotion history | Portable organizational source of truth |
| Mem0 | Semantic recall over approved records | Optional and rebuildable index |

Resolve the knowledge root from `OPC_KNOWLEDGE_HOME`; otherwise use the core CLI default. Resolve runtime/index data from `PLUGIN_DATA` or `OPC_MEMORY_DATA_HOME`. Never put runtime databases or provider caches in the public plugin tree or canonical knowledge repository.

A candidate must contain scope, owner, type, source, confidence, status, timestamps, evidence, and supersession links. Do not store raw conversations, secrets, unsupported opinions, machine-specific absolute paths, or one-off details.

Promotion requires all of:

1. A resolvable source run or artifact.
2. Reproducible evidence.
3. A reusable lesson rather than a copied trajectory.
4. Replay, shadow, regression, or equivalent validation.
5. Explicit manager approval.
6. A versioned reversible change and rollback path.
7. Reindexing only after the canonical change is committed; stale or unavailable Mem0 results must not override canonical records.
8. `doctor.file_git.ok=true` and `doctor.file_git.provenance_ready=true`; structural validity alone does not establish Git provenance.
