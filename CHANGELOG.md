# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows Semantic Versioning once the first release is tagged.

## [Unreleased]

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

[Unreleased]: https://github.com/coconilu/codex-opc-team/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/coconilu/codex-opc-team/releases/tag/v0.1.0
