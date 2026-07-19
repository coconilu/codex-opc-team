# Codex OPC Team

[简体中文](README.zh-CN.md) · [v0.1.1-rc.1 notes](docs/release-notes-v0.1.1-rc.1.en.md) · [Stable v0.1.0 notes](docs/release-notes-v0.1.0.en.md) · [Architecture](docs/architecture.md) · [Security](SECURITY.md) · [Roadmap](docs/roadmap.md)

Codex OPC Team is an open-source, Codex-native operating model for a one-person company. It turns a project request into an aligned plan, delegated implementation, independent QA, and an evidence-backed retrospective while keeping the user in the manager role.

Codex remains the harness. The project does not replace Codex's file, browser, web, tool, and sub-agent capabilities with another agent runtime.

## Design principles

| Principle | Meaning |
|---|---|
| Codex-native | Reuse Codex as the execution harness and distribute the team as a plugin. |
| Manager-first | Ask the user for direction and material decisions, not routine implementation details. |
| Portable memory | Git-managed files are the durable source of truth. |
| Optional Mem0 | Mem0 can improve semantic recall, but the full workflow must work without it. |
| Progressive context | Private, disposable L0/L1 navigation limits canonical L2 reads; every injected leaf is revalidated against current File/Git HEAD. |
| Auditable use | Private role/step lineage distinguishes recall, injection, adoption, omission, and evidence association without claiming causality. |
| Controlled learning | Experience moves from candidate to manager-approved knowledge, then becomes recallable only after an exact Git commit is verifiable at the current HEAD; it is never silently promoted. |
| Independent acceptance | A developer's self-report is not QA evidence. The manager is notified only after an independent gate. |
| Private by default | Public plugin code is separated from private organizational knowledge and runtime data. |

## Operating loop

```mermaid
flowchart LR
    U["Manager intent"] --> M["OPC manager: align and contract"]
    M --> D["Delegate to role agents"]
    D --> Q["Independent QA"]
    Q -->|"FAIL"| D
    Q -->|"PASS"| H["Manager handoff"]
    H --> R["Retrospective candidate"]
    R --> V["Validate and approve"]
    V --> K["Portable knowledge"]
```

## Project status

`v0.1.0` is the first stable release. The Codex-native team loop, File/Git memory, optional Mem0 adapter, safe hooks, installer, and automated gates have passed the release checks. Use the fixed `v0.1.0` tag rather than `main` as the stable install source. See the [v0.1.0 release notes](docs/release-notes-v0.1.0.en.md), [roadmap](docs/roadmap.md), and [acceptance contract](docs/testing-and-acceptance.md).

`v0.1.1-rc.1` is the public release candidate for stricter runtime-data isolation and installed-plugin lifecycle acceptance. It is pre-release software, not the stable channel. Reviewers and release testers may install the immutable candidate snapshot with `codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.1-rc.1`; production users should remain on `v0.1.0` until the stable release Gate passes. See the [release-candidate notes](docs/release-notes-v0.1.1-rc.1.en.md).

The v0.2 context, feedback, evaluation, conflict, lineage, and governed capability-evolution components are implemented on `main`, and their public synthetic release evidence passes. **v0.2.0 is not release-ready:** the required representative private 3–5 task pilot and exact-release-commit gates do not yet exist. Public fixtures or templates cannot substitute for that evidence. See [v0.2 release readiness](docs/release-readiness-v0.2.0.md).

## Installation

Prerequisites: Codex CLI, Git, and Python 3.10 or newer. Mem0 is not required.

Add the `v0.1.0` repository snapshot as a Codex marketplace and install the plugin:

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

The default File/Git memory mode has no Mem0 dependency. Mem0 setup is optional and must degrade safely when unavailable. Detailed install, upgrade, removal, and data-retention behavior is documented in [installation and distribution](docs/installation-and-distribution.md); release-specific compatibility, migration, rollback, and evidence are in the [v0.1.0 release notes](docs/release-notes-v0.1.0.en.md).

## Public code, private knowledge

This repository contains plugin behavior, schemas, empty templates, tests, and documentation. It must not contain a user's manager profile, project history, approved organizational experience, raw conversations, credentials, local paths, or runtime identifiers.

Private knowledge is initialized outside the plugin cache and remains user-controlled. Removing the plugin must not delete that knowledge.

Hook/runtime events live in private `PLUGIN_DATA` or a project `.opc` fallback, never in canonical knowledge. `opc-memory` reports known legacy event artifacts without reading their contents and requires a dry-run plus a separately approved, unchanged plan before archiving them.

Hierarchical recall is zero-dependency and optional. Its virtual tree, L0/L1 summaries, and index live only under an explicit private data root, are Git-ignored, deletable, and rebuildable, and never become facts. Missing, stale, invalid, disabled, timed-out, or disagreeing derived/provider state falls back to File/Git. The public synthetic comparison reports precision@5 `0.20 → 1.00`, canonical leaf recall@5 `1.00 → 1.00`, median injected tokens `661 → 107`, and zero scope/stale acceptance; this is evidence for that fixture, not a universal performance claim. See [hierarchical recall and ContextPacket](docs/hierarchical-recall.md).

Knowledge lineage is an optional private `.opc` sidecar. It binds exact run/project and ContextPacket/RecallTrace hashes to role/step states, provider degradation, QA, feedback, outcome, Shadow, and evaluation references. Reports revalidate current File/Git provenance and always state `association/evidence only`; they never infer adoption or causality. See [knowledge lineage](docs/knowledge-lineage.md).

Capability evolution is an evidence-gated private lifecycle for versioned roles, Skills, and organization policies. It requires exact Git blobs, replay/Shadow evidence, independent QA, explicit manager approvals, bounded paired pilots, and a one-path unstaged promotion or rollback followed by explicit commit confirmation. It never changes global Codex config or claims causality. See [controlled capability evolution](docs/capability-evolution.md).

## Documentation

| Document | Purpose |
|---|---|
| [v0.1.1-rc.1 release-candidate notes](docs/release-notes-v0.1.1-rc.1.en.md) | Pre-release scope, verification, known limitations, and rollback to stable |
| [v0.1.0 release notes](docs/release-notes-v0.1.0.en.md) | Compatibility, installation, migration, limitations, rollback, and gate evidence |
| [Origin and decisions](docs/origin-and-decisions.md) | Why this project exists and how the design converged |
| [Vision and scope](docs/vision-and-scope.md) | Product goals, boundaries, and user experience |
| [Architecture](docs/architecture.md) | Components, contracts, and execution flow |
| [Memory architecture](docs/memory-architecture.md) | File/Git authority, optional Mem0, and learning governance |
| [Knowledge governance](docs/knowledge-governance.md) | Deterministic applicability, relations, conflicts, and Schema 1-to-2 migration |
| [Installation and distribution](docs/installation-and-distribution.md) | Local install, marketplace release, upgrade, and removal |
| [Migration](docs/migration-from-local-prototype.md) | Safe cutover from the local prototype |
| [Security and privacy](docs/security-and-privacy.md) | Data boundaries, hook safety, and publication checks |
| [Testing and acceptance](docs/testing-and-acceptance.md) | Test matrix and release gates |
| [Evaluation baseline](docs/evaluation-baseline.md) | Versioned synthetic File/Git baseline and private 3–5-task aggregate protocol |
| [Structured feedback](docs/structured-feedback.md) | Private, auditable manager judgment, QA evidence, outcome, and hypothesis records |
| [Shadow Evaluation](docs/shadow-evaluation.md) | Read-only candidate control/treatment replay with exact provenance and no automatic promotion |
| [Hierarchical recall](docs/hierarchical-recall.md) | Private L0/L1 navigation, canonical L2 validation, ContextPacket/RecallTrace, and flat comparison |
| [Knowledge lineage](docs/knowledge-lineage.md) | Private role/step knowledge states, portable outcome links, current-HEAD revalidation, and non-causal reports |
| [Capability evolution](docs/capability-evolution.md) | Versioned role/Skill/policy pilots, evidence gates, one-path Git handoff, observation, and rollback |
| [v0.2 release readiness](docs/release-readiness-v0.2.0.md) | Public synthetic evidence, private 3–5 task pilot protocol, exact-commit gates, blockers, and non-claims |
| [Roadmap](docs/roadmap.md) | Planned delivery stages |

Architecture decisions live under [`docs/adr`](docs/adr/README.md).

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing. Report vulnerabilities according to [SECURITY.md](SECURITY.md); do not disclose sensitive reports in public issues.

## License

Apache License 2.0. See [LICENSE](LICENSE).
