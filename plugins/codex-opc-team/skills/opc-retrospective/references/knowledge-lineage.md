# Retrospective use of lineage

Use lineage to separate observation from inference:

| Evidence | Allowed statement |
|---|---|
| `recalled` | The revision appeared in bounded recall evidence. |
| `injected` | The exact revision was in the role's exact ContextPacket. |
| `adopted` | An explicit disposition event records adoption. |
| QA/feedback/outcome association | The portable artifacts are associated with the run. |
| Event order | Temporal association only; not causality. |

Before proposing a candidate, render the current lineage report and exclude stale, cross-project, obsolete, conflict, omitted, or degraded refs from positive use claims. Cite project-private artifact refs, never copy trace bodies. Preserve confounders and unknowns in the causal-inference field. Lineage does not approve a candidate and must not be copied into canonical knowledge or Provider storage.
