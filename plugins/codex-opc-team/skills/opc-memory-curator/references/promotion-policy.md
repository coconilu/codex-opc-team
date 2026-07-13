# Knowledge Promotion Policy

| Gate | Pass condition |
|---|---|
| Provenance | Source run and evidence are resolvable without exposing private runtime data |
| Relevance | Scope and trigger are explicit |
| Scope integrity | `project` has the exact project ID; `global` has no project ID; other scopes require a dedicated identity contract |
| Causality | Observation and inference are separated |
| Conflict | Existing guidance is checked and supersession is explicit |
| Validation | Replay, shadow, regression, or equivalent evidence exists |
| Safety | No secret, sensitive inference, raw hook payload, or authority expansion |
| Approval | Manager explicitly approves this candidate and destination |
| Reversibility | Previous version and rollback path remain available |
| Index integrity | File/Git is structurally valid and provenance-ready; the canonical record has a verifiable `source_commit`; the optional index can be rebuilt |

A confidence score or successful Mem0 retrieval never bypasses these gates. Promotion is a versioned canonical change; `approve` does not index it, and explicit reindexing happens only after the exact canonical transition is committed.
