# Manager knowledge-lineage handoff

Use `opc_lineage.py` only for an active OPC project whose `.opc` is private or Git-ignored. Preview every event first and apply only the unchanged plan token. Keep event and RecallResult inputs private.

| Moment | Event |
|---|---|
| Recall completes | one explicit `recalled` event per canonical revision and role/step |
| Packet is handed to a role | `injected`, with the exact RecallResult |
| Role reports a disposition | `adopted`, `ignored`, `overridden`, or `contradicted`; never infer from injection |
| Governance withholds a revision | `omitted` with stale/scope/status/conflict reason |
| Optional memory is absent/unhealthy | separate provider `missing/disabled/failed/stale/no_memory` event |
| QA/feedback/outcome arrives | association event with existing hashed `.opc` references |

Before handoff, render the report with the current knowledge root. Treat any stale, cross-project, obsolete, conflict, evidence failure, or provider failure as degraded. The report is `association/evidence only`; do not convert event order into causality. Lineage never authorizes promotion or changes the run gate.
