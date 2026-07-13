# Codex OPC Team v0.1.0 Release Notes

[简体中文](release-notes-v0.1.0.md)

Release date: 2026-07-13

`v0.1.0` is the first stable release of Codex OPC Team. It provides a complete “manager alignment → role delegation → independent QA → manager handoff → governed retrospective” loop inside the Codex harness, with File/Git as the authoritative source for portable organizational memory.

## Compatibility

| Item | v0.1.0 support |
|---|---|
| Codex | Codex CLI Plugin/Marketplace installation and Skill discovery; this release was exercised with `codex-cli 0.144.1`, without claiming it as a minimum supported version; start a new Codex task after installation to reload the plugin |
| Python | `>=3.10`; plugin scripts and core tests cover Python 3.10 and 3.12 |
| Operating systems | Windows and Linux |
| Core memory | File/Git, enabled by default with no Mem0, vector database, or external model credential dependency |
| Optional semantic recall | `mem0ai==2.0.11` and `httpx[socks]==0.28.1`; isolated virtual environment and private data directory |
| Private data | Stored outside the plugin cache and public repository under user control; plugin removal does not delete it |

## Installation

Prerequisites: Codex CLI, Git, and Python 3.10 or newer.

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

After installation, start a new Codex task and invoke `$opc-manager`. The first run starts with Doctor. A private knowledge repository is initialized from the empty template only after its target path is shown and confirmed.

## Core mode and optional Mem0

| Mode | Data flow | Failure behavior |
|---|---|---|
| File/Git (default) | Verifies and reads approved canonical knowledge from the current Git HEAD | Preserves the complete alignment, delegation, QA, retrospective, approval, and recall loop |
| Mem0 (optional) | Builds a rebuildable semantic index for approved knowledge; every hit is still read back from File/Git after Commit and content-hash verification | Safely falls back to File/Git when missing, disabled, version-incompatible, timed out, or stale |

Mem0 is never installed or enabled silently. `v0.1.0` validates only the pinned `mem0ai==2.0.11` release. Its default LLM/Embedder configuration may require `OPENAI_API_KEY` and may send approved-entry summaries and bodies to OpenAI. Dependencies, paths, and the actual data flow must be previewed before enablement. Fully local providers, Mem0 Cloud, and self-hosted Mem0 Server are outside this release's support commitment.

## Data and schema migration

| Object | v0.1.0 rule |
|---|---|
| New installation | Plugin installation and private knowledge initialization are separate; no user data is created without confirmation |
| Knowledge schema | The initial schema version is `1`; initialization creates a separate private Git repository and baseline commit |
| Legacy local prototype | Snapshot and inventory it read-only before previewing migration; do not copy old history, logs, credentials, or machine paths wholesale |
| Existing private knowledge | Never overwrite a non-empty directory; schema changes must be previewable, repeatable, backed up, and verified by entry count, hash, references, and Git diff |
| Mem0 index | Derived data only; it can be deleted and rebuilt from currently verifiable File/Git knowledge and never becomes the authority |
| Removal | Preserve private knowledge, Git history, and Mem0 data; additional cleanup requires separate confirmation |

When switching from an unreleased build or legacy prototype, follow the [local prototype migration guide](migration-from-local-prototype.md) for a blue-green cutover. Do not activate two Skill sets with the same names at the same time.

## Known limitations

| Limitation | Impact and handling |
|---|---|
| Plugin discovery is task-scoped | Start a new Codex task after installation, upgrade, or rollback |
| The default Mem0 chain is not fully local | Review credentials and data flow before enabling; otherwise remain in File/Git mode |
| Online Mem0 model calls are outside the CI gate | CI validates pinned dependency import, real adapter construction, storage isolation, and fallback contracts; it does not guarantee external service availability |
| Only Windows and Linux are validated | Other platforms are not yet part of the release matrix |
| Learning requires human governance | Experience candidates are never auto-promoted; approval still requires an exact Git commit verifiable from the current HEAD |
| No resident autonomous service | Team execution depends on the active Codex task, available tool permissions, and user authorization boundaries |

## Rollback

Remove the plugin first, then remove the marketplace if desired. These commands must not delete private knowledge:

```powershell
codex plugin remove codex-opc-team@opc
codex plugin marketplace remove opc
```

To disable only Mem0, use `$opc-memory` to preview `disable --dry-run`, then confirm `disable --apply`; authoritative File/Git knowledge remains available. For a migration from the legacy local prototype, restore the pre-migration snapshot, Personal Marketplace, and only the configuration owned by the old OPC installation. Then verify plugin discovery, zero out-of-scope Hook records, knowledge reads, and a minimal project flow in a new task.

## Release gate evidence

The following is the redacted release evidence from 2026-07-13. It contains no private paths, knowledge bodies, credentials, session identifiers, or raw Hook payloads.

| Gate | Evidence | Result |
|---|---|---:|
| G1 Design | Repository validator, Plugin validator, and six Skill quick validators | PASS |
| G2 Privacy | Public workspace and Git-history privacy scans; Hook scope, marker, rotation, and concurrency tests | PASS |
| G3 Core | Complete local test suite `72/72`; File/Git loop with no Mem0 | PASS |
| G4 Optional backend | Isolated pinned Mem0 installation, real adapter construction, private History/Qdrant, telemetry disabled, and fallback tests | PASS |
| G5 Distribution | Public repository commit installed into an isolated `CODEX_HOME`, discovered six Skills, then removed while preserving data | PASS |
| G6 End to end | Redacted execution chain: Developer 5 → QA 132 **FAIL** → repair Developer 6 → unchanged QA matrix **PASS** → manager handoff `completed` | PASS |
| G7 Rollback | Legacy prototype snapshot bundle verification; isolated `CODEX_HOME` rollback install/remove exercise | PASS |
| G8 Release | Version, Changelog, READMEs, installation/migration documentation, and these release notes agree | PASS |

GitHub Actions evidence:

| Run | Scope | Result |
|---|---|---:|
| [29234352042](https://github.com/coconilu/codex-opc-team/actions/runs/29234352042) | Windows/Linux core and optional Mem0 release matrix | SUCCESS |
| [29234559980](https://github.com/coconilu/codex-opc-team/actions/runs/29234559980) | Final Actions runtime verification; 6/6 jobs, `actions/checkout@v7`, `actions/setup-python@v6`, and no Node 20 annotation | SUCCESS |

The initial independent-QA FAIL and the unchanged-matrix PASS after repair are expected gate behavior, not evidence that was deleted or weakened.

## Further reading

- [Installation and distribution](installation-and-distribution.md)
- [Testing and acceptance](testing-and-acceptance.md)
- [Memory architecture](memory-architecture.md)
- [Security and privacy](security-and-privacy.md)
- [Local prototype migration](migration-from-local-prototype.md)
