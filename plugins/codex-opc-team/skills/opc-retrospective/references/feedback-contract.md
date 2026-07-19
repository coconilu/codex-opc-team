# Feedback Use in Retrospectives

Read the private structured-feedback sidecar with:

```text
python <plugin-root>/scripts/opc_feedback.py show --project-root <project>
```

Treat `structured_feedback: null` as “not recorded,” not PASS. Keep these evidence classes separate: confirmed outcome, manager judgment, independent QA evidence, hypothesis, and unverified information. A manager's `accepted` judgment is not independent QA and does not approve an experience candidate.

Use only portable project/run/candidate/artifact/metric aggregate references. Never copy raw chat, Hook payloads, credentials, host paths, URLs, runtime IDs, or private metric values into a candidate. State which feedback event supports an observation and which causal step remains a hypothesis.

Feedback can justify proposing or rejecting a retrospective candidate, but it never stages, commits, indexes, publishes, approves, pays, or communicates externally. Candidate validation, manager approval, and File/Git publication remain separate gates.
