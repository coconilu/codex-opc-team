# Structured Feedback Contract

Use structured feedback only after a manager-facing handoff or when a measured outcome arrives later. Keep it concise and project-private.

## Evidence classes

| Class | Meaning | Status field |
|---|---|---|
| `confirmed_outcome` | An observed task or product result | `pass`, `fail`, or `partial` |
| `manager_judgment` | The manager's subjective acceptance or concern | `accepted`, `changes_requested`, `mixed`, `neutral`, or `unknown` |
| `independent_qa_evidence` | Evidence produced by an independent acceptance gate | `pass`, `fail`, `partial`, or `unknown` plus a portable artifact reference |
| `hypothesis` | A possible causal lesson that still needs validation | no outcome, judgment, or QA status |
| `unverified` | An outcome that has not arrived or cannot yet be confirmed | `unknown` |

Never convert one class into another. Manager acceptance is not independent QA, a metric is not a manager judgment, and a hypothesis is not a confirmed lesson.

## Storage lifecycle

```text
private project .opc/run.json
  -> private .opc/feedback/<run_id>.json
  -> later evaluation input
  -> optional retrospective candidate
  -> separate manager approval and File/Git publication gates
```

The versioned contract and synthetic tests ship with the public plugin. Real records remain under the approved project's private `.opc` boundary. They do not enter this public repository, canonical knowledge, Mem0, a recall index, or a public CI artifact. Do not record raw chat, Hook payloads, credentials, unrelated project content, host paths, URLs, UUIDs, or session/turn/thread identifiers.

An existing run with no feedback sidecar is valid and reads as `structured_feedback: null`. Do not invent defaults or migrate it. An unsupported feedback version fails closed and requires an explicit future migration.

## Portable references

Every event repeats the stable `project_id` and `run_id` from the current project files. Candidate IDs use `exp-*`. Artifact and aggregate references are bounded project-relative identifiers: no absolute path, `..`, URL, or host name. Metric references use a metric ID from `opc-evaluation-contract-v1`, a safe aggregate reference, its SHA-256, and an interpretation of `supporting`, `conflicting`, or `unknown`. Do not embed private per-task values or change the baseline contract.

## Safe recording

Prepare one strict JSON event outside the public plugin tree, then run:

```json
{
  "event_id": "feedback-outcome-synthetic",
  "recorded_at": "2026-01-01T00:00:00Z",
  "category": "unverified",
  "epistemic_status": "unverified",
  "summary": "Product outcome has not arrived.",
  "outcome_status": "unknown",
  "manager_judgment": "not_applicable",
  "qa_status": "not_applicable",
  "references": {
    "project_id": "project-synthetic",
    "run_id": "opc-run-synthetic",
    "candidate_ids": [],
    "metric_refs": [],
    "artifact_refs": []
  }
}
```

The public tests provide PASS, FAIL, partial, and unknown cases. Replace only the synthetic values with concise private values that pass the same v1 schema and runtime checks.

Then run:

```text
python <plugin-root>/scripts/opc_feedback.py show --project-root <project>
python <plugin-root>/scripts/opc_feedback.py record --project-root <project> --event-file <event.json> --expected-revision <revision>
python <plugin-root>/scripts/opc_feedback.py report --project-root <project>
```

Use the revision returned by `show`. Each event is immutable. Retrying the same `event_id` with equivalent JSON is a no-op even if the caller still holds the old revision; reusing the ID with different content is rejected. A stale revision, concurrent lock, reference mismatch, invalid time/order, secret marker, extra field, or path-boundary ambiguity fails closed.

Record an `unverified` event while a completed run is still current when its product outcome is expected later. After a new run starts, address that established sidecar with `--run-id <old-run-id>` on `show`, `record`, and `report`. An arbitrary historical run with no existing sidecar is rejected because its project relationship is no longer locally verifiable.

The JSON record is the only machine source. The compact Markdown report is generated deterministically from that record and must not be edited into a different conclusion.

Recording feedback performs no candidate approval, Git staging/commit, indexing, publishing, payment, or external communication. Ask separately before any later action that has one of those effects.
