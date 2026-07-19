# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows Semantic Versioning once the first release is tagged.

## [Unreleased]

### Added

- Added zero-dependency hierarchical File/Git recall with private rebuildable L0/L1 navigation, bounded current-HEAD L2 reads, strict ContextPacket/RecallTrace contracts, flat/provider degradation, and a versioned public flat-vs-hierarchical evaluation.
- Hardened hierarchical recall with the shared #7 relation engine, canonical governance snapshots, exact publish rollback, joint Packet/Trace validation, and evidence-recomputed evaluation rendering.
- Added a versioned, deterministic File/Git evaluation contract, public synthetic fixtures, machine and human baseline artifacts, and a strict aggregate-only protocol for private 3–5-task pilots.
- Added a strict, versioned structured-feedback sidecar with portable references, auditable late updates, deterministic reporting, and fail-closed privacy/concurrency gates.
- Added read-only candidate Shadow Evaluation with exact File/Git preflight, #4-compatible control/treatment metrics, #5 feedback evidence, deterministic confidence/reporting, and no promotion or canonical/provider writes.
- Added deterministic Schema 2 knowledge governance for applicability, sensitivity, conflicts, supersession and invalidation, with Schema 1 compatibility, previewed backed-up migration, exact manager curation, and File/Git-authoritative context filtering.

## [0.1.1-rc.1] - 2026-07-19

This is a public release candidate for review and fixed-ref lifecycle validation. Stable installations remain on `v0.1.0` until the final release Gate passes.

### Added

- Added a preview-first, real-Codex clean-room acceptance flow for fixed-ref install, fresh-process Skill discovery, uninstall, reinstall, rollback, idempotency, and synthetic knowledge/config/data preservation on Windows and Linux.
- Hardened lifecycle acceptance with exact canonical Skill discovery, deny-by-default child environments, isolated Git config/hooks/signing/credentials, host-sentinel negative tests, commit-OID pinning, moving-ref rejection, and disposable Windows/Linux PR gates.

### Fixed

- Isolated Hook/runtime events from canonical knowledge even when `PLUGIN_DATA` is missing or misconfigured, and added redacted preview-first legacy event diagnostics and archival.
- Made release-tag validation fail closed when Git enumeration fails or any malformed, unexpected, or duplicate `v`-prefixed tag points at the candidate commit.

### Security

- Removed the runtime event directory from new knowledge templates and required an unchanged preview token plus explicit approval before moving eligible legacy private event files; automatic deletion, commit, and upload remain forbidden.

## [0.1.0] - 2026-07-13

### Added

- Codex-native OPC plugin repository and marketplace layout.
- Manager, project bootstrap, independent QA, retrospective, memory curator, and memory administration skills.
- File/Git organizational memory with governed candidate, approved, rejected, and obsolete states.
- Optional Mem0 2.x recall adapter with lazy loading, provenance validation, timeout/error fallback, and a local outbox.
- Dry-run-first local install and uninstall helpers that preserve private knowledge and do not edit global Codex roles.
- Safe Stop hooks that write only for active OPC runs and retain only allowlisted fields.
- Windows and Linux CI, unit/integration tests, repository validation, and Git-history privacy scanning.
- Public documentation for product scope, architecture, memory, distribution, migration, security, testing, and roadmap.
- Architecture Decision Records for the six foundational decisions.

### Security

- Defined a strict boundary between public plugin artifacts and private organizational knowledge.
- Defined OPC-only hook scoping and publication privacy gates.
- Added source hash and Git commit checks before any optional recall hit is trusted.
- Kept active run snapshots project-local so normal run updates cannot dirty or bypass the private knowledge repository's Git provenance gate.
- Made unsupported or malformed memory scopes fail closed, including hand-edited global records that incorrectly retain a project identity.

[Unreleased]: https://github.com/coconilu/codex-opc-team/compare/v0.1.1-rc.1...HEAD
[0.1.1-rc.1]: https://github.com/coconilu/codex-opc-team/compare/v0.1.0...v0.1.1-rc.1
[0.1.0]: https://github.com/coconilu/codex-opc-team/releases/tag/v0.1.0
