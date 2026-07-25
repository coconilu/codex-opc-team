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
  preserves plugin data during uninstall. One stable App-owned marketplace
  path is registered once; update and rollback atomically change that source
  and use public marketplace/plugin update commands. Marketplace removal is
  not an update or rollback primitive.
- Kimi manages documented user Skill directories and passes their common scan
  root once to `--skills-dir`. Plugin automation remains blocked while the
  public Plugin manager is interactive-only.

Preview plans are short-lived, one-time, and process-local. Apply re-captures
and compares the source, host/version, official discovery, capability contract,
ownership manifest, and resolved target state before the first write.

File/Git remains authoritative. Adapter manifests and backups are operational,
rebuildable App state, not organizational memory. A host target may be changed
or removed only when the manifest proves OPC ownership and the current hash
still matches. Unknown host versions and unsupported mechanisms fail closed.

Each host has one mutation lock spanning final state verification, host writes,
fresh-process verification, manifest publication, and operation-record
publication. An operation record is written before mutation and retains only
logical rollback identity, prior manifest, hashes, status, and a stable error
code. Completed or recoverable failed operations appear in the App as recovery
entries. Rollback is a separate Host/Origin/CSRF-protected request and the UI
requires its own confirmation dialog; it rechecks the recorded post-operation
fingerprint before changing anything. Codex and Claude cannot claim byte-level
host-cache drift because their public discovery contracts do not expose a
supported cache digest; Kimi can hash its managed public Skill directories.

## Consequences

The three hosts can expose different support levels without false parity.
Plans are auditable and writes require explicit confirmation. Real installed
state still needs independent host-specific QA; Developer-run disposable-host
acceptance is implementation evidence, not release approval.
