# Shadow Evaluation contract

## Surfaces

```text
<memory-python> "<plugin-root>/scripts/opc_shadow.py" preview \
  --knowledge-root <knowledge-root> --replay <replay.json> [--project-root <approved-private-project>]

<memory-python> "<plugin-root>/scripts/opc_shadow.py" evaluate \
  --knowledge-root <knowledge-root> --replay <replay.json> \
  --expected-preview-sha256 <preview-sha256> --artifact-root <private-data-root> \
  [--project-root <approved-private-project>]

<memory-python> "<plugin-root>/scripts/opc_shadow.py" report --result <result.json>
```

`preview` writes nothing. `evaluate` creates one immutable JSON result and one deterministic Markdown report in a pre-created private artifact root; it refuses existing names and roots overlapping plugin source, canonical knowledge, or project source.

Replay ratio components are bounded at 1,000,000, safety counts at 1,000,000, context tokens at 10,000,000, and latency at 86,400,000 ms. Their 20-case aggregate bounds are 20,000,000, 20,000,000, 200,000,000, and 1,728,000,000 respectively. Runtime checks integers without float conversion and turns every arithmetic overflow into a redacted `OPC_SHADOW_ERROR`.

## Comparable arms

Every case has the same strict fields in `control` and `treatment`. Control sets `candidate_applied=false`; treatment sets it to `true`. Both carry the #4 raw quality ratios, zero-tolerance safety counts, context tokens, latency, and explicit execution status. The replay also versions its engine, determinism boundary, and seed.

Quality and safety determine benefit or harm. Context cost and latency remain diagnostic and cannot produce a positive recommendation by themselves. Any treatment scope leakage or stale/obsolete acceptance is counterevidence.

## Evidence and confidence

The report preserves support, counterevidence, neutral/unknown results, scope rejection, and failure codes. Structured feedback is read through the #5 sidecar contract and classified as:

| Feedback class | Shadow evidence class | Confidence weight |
|---|---|---:|
| confirmed outcome / independent QA | measured | 3 |
| manager judgment | human judgment | 1 |
| hypothesis | model inference | 0 |
| unverified | unverified | 0 |

`beta-v1` starts with one support and one counterevidence prior. The evaluated confidence is versioned and evidence-derived, but always reports `approval_permission=false`. Model inference is not independent QA.

## Private pilot boundary

An `approved_private_pilot` requires `--project-root`, an exact matching portable `project_id`, a portable `approval_ref`, and a valid structured feedback sidecar if present. Real cases, feedback, and reports stay in the approved private boundary or an isolated private data root. Only synthetic fixtures belong in this public repository.

Shadow Evaluation has no command for promotion, status transition, Git write, provider indexing, publishing, deletion, payment, or external communication.

Every user path is inspected before resolution, including existing ancestors. Symlink, junction, reparse, hard-link, parent-identity-change, and assert-to-publish replacement cases fail closed. Replay, result, canonical candidate, and published artifact files require one link. Result rendering first validates the exact current contract hashes, all strict nested fields, measured evidence, confidence, failures, and false write permissions.
