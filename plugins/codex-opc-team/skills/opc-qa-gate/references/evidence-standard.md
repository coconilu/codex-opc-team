# Acceptance Evidence Standard

| Claim | Strong evidence | Insufficient alone |
|---|---|---|
| Implemented | Real diff, artifact, runtime behavior | Employee summary |
| Builds | Current successful build output and exit code | A build script exists |
| Tests pass | Current targeted and required suite results | Tests passed in an older run |
| UI works | Browser interaction, visible state, screenshot or trace | Source inspection only |
| Safe | Permission boundary, bounded diff, rollback evidence | “The change is small” |
| Ready to experience | Reproducible command, URL, package, or artifact | “It should work” |

Record the command, relevant environment, artifact reference, timestamp, and result. Redact secrets, private identifiers, and unrelated machine paths. A broad completion claim requires broad evidence; do not generalize from one narrow smoke check.
