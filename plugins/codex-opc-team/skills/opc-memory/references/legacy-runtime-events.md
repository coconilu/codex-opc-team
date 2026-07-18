# Legacy runtime event isolation

Treat `LEGACY_RUNTIME_ARTIFACTS` as private runtime data, not canonical knowledge. The detector uses only known path metadata and never needs file contents.

1. Run `legacy-events --dry-run` with the already resolved memory Python, knowledge root, and data root.
2. Report the redacted relative source paths, private archive target, eligibility, unresolved provenance, and excluded actions. Do not open, summarize, hash, commit, or upload event contents.
3. If any entry is tracked, a symbolic link, not a regular file, or has an existing destination, stop and request a manual security review. Do not alter Git history.
4. Obtain explicit approval for the exact unchanged preview. Approval to install, disable, uninstall, reindex, or purge optional memory is not approval to move private events.
5. Run `legacy-events --apply --plan-token <approval-token>`. A missing or changed token is a hard stop.
6. Rerun `doctor`. Confirm that the legacy warning cleared and that `UNCOMMITTED_KNOWLEDGE`, approved-transition provenance, and File/Git fallback state were not weakened.

Apply uses an atomic same-filesystem move into `data_root/legacy-event-archive`. A cross-filesystem failure preserves the source and requires a separately reviewed manual move. No command in this workflow deletes, commits, uploads, or edits event contents.
