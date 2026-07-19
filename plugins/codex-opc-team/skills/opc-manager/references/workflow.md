# OPC Run Workflow

| State | Required result | Allowed next states |
|---|---|---|
| `aligning` | Project brief, scope, assumptions, manager decisions | `planned`, `paused`, `failed` |
| `planned` | Team, bounded contracts, dependencies, acceptance criteria | `implementing`, `paused`, `failed` |
| `implementing` | Working artifacts and implementation evidence | `validating`, `paused`, `failed` |
| `validating` | Independent tests, runtime or artifact checks, QA report | `implementing`, `ready_for_manager`, `paused`, `failed` |
| `ready_for_manager` | Reproducible experience path and verified limitations | `completed`, `implementing` |
| `completed` | Manager handoff recorded | terminal |

Use the shared run CLI for state changes:

```text
python <plugin-root>/scripts/opc_knowledge.py update-run --project-root <project> --status validating
```

Before `ready_for_manager`, record non-empty `implementation`, `verification`, and `qa` evidence. Before `completed`, also record `manager_handoff` evidence. Keep `.opc/run.json` local runtime state; do not copy it into organizational knowledge or a public repository.

Structured feedback is an optional versioned sidecar under `.opc/feedback`, not a new run-state requirement. Request it after handoff or when a late product outcome arrives; read a missing sidecar as “not recorded,” not PASS.
