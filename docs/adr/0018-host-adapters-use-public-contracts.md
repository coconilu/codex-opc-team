# ADR-0018: Host Adapters use public lifecycle contracts

- Status: Accepted
- Date: 2026-07-25
- Depends on: ADR-0002, ADR-0005, ADR-0017

## Context

OPC must project one canonical package into several local Agent hosts without
making their extension models appear equivalent. Blind file copying would
bypass official discovery, create ambiguous ownership, and make safe uninstall
impossible.

## Decision

The App calls a host-neutral Adapter core with stable `probe`, preview-plan,
explicit `apply`, `verify`, and `rollback` stages. Host implementations remain
separate:

- Codex delegates to the existing Marketplace/Plugin lifecycle and
  `plugin_admin.py`.
- Claude uses its documented non-interactive Marketplace Plugin CLI and
  preserves plugin data during uninstall.
- Kimi manages documented user Skill directories. Plugin automation remains
  blocked while the public Plugin manager is interactive-only.

File/Git remains authoritative. Adapter manifests and backups are operational,
rebuildable App state, not organizational memory. A host target may be changed
or removed only when the manifest proves OPC ownership and the current hash
still matches. Unknown host versions and unsupported mechanisms fail closed.

## Consequences

The three hosts can expose different support levels without false parity.
Plans are auditable and writes require explicit confirmation. Real installed
state still needs independent host-specific QA because clean-room fake CLIs
cannot prove logged-in discovery or interactive Skill invocation.
