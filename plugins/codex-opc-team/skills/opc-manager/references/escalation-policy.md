# Manager Escalation Policy

| Decide internally | Escalate to the manager |
|---|---|
| Reversible implementation details | Product direction or scope change |
| Local refactors inside approved scope | Destructive or hard-to-reverse actions |
| Test strategy and internal tooling | Credentials, payments, legal or financial commitments |
| Choice among equivalent libraries | Public release, deployment, push, or messages to people |
| Bug fixes required by acceptance criteria | Material security, privacy, data-retention, or memory-promotion risk |
| Retry after an understood local failure | A repeated blocker that changes cost or feasibility |

Do not ask the manager to choose among technical options the team can decide from evidence. Escalate with a recommendation, tradeoff, bounded impact, and safe default.
