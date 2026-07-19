# OPC evaluation baseline report

- Contract: `opc-evaluation-contract-v1`
- Contract SHA-256: `f7eda22695e25f91f15031d6a94d0183e399fc3b52b34a21130b7f567180444d`
- Baseline: `file-git-no-enhancement-v1`
- Dataset: `dataset-syn-file-git-v1`
- Mode: `public-synthetic`
- Tasks: 6
- Safety: **PASS**

## Product outcomes

| Metric | Numerator | Denominator | Value |
|---|---:|---:|---:|
| `manager_intervention_rate` | 3 | 16 | 0.1875 |
| `qa_catch_rate` | 7 | 7 | 1.0 |
| `rework_loops_per_task` | 3 | 6 | 0.5 |
| `valid_knowledge_reuse_rate` | 4 | 4 | 1.0 |
| `false_recall_rate` | 2 | 6 | 0.3333 |

## Safety gates

| Gate | Observed | Required | Status |
|---|---:|---:|---|
| `scope_leakage_acceptances` | 0 | 0 | pass |
| `stale_obsolete_acceptances` | 0 | 0 | pass |
| `provenance_probes` | 2 | all_rejected | pass |

## Diagnostic telemetry

| Metric | Mean | Median | p95 (nearest rank) |
|---|---:|---:|---:|
| Context tokens/task | 1028.3333 | 990.0 | 1240 |
| Latency (ms) | 39.3333 | 38.0 | 48.0 |

> This versioned baseline is not statistical generality. Safety gates are mandatory; no quality, token, or latency metric is sufficient on its own.
