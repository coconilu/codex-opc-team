# Codex OPC Team v0.1.1-rc.1 Release Candidate Notes

[简体中文](release-notes-v0.1.1-rc.1.md)

## Release status

`v0.1.1-rc.1` is a public release candidate for pre-release review of runtime-data isolation and real installed-state lifecycle gates. It is not the stable release; production installations should remain pinned to `v0.1.0`. The candidate tag may be created only after the original reviewer approves the exact candidate commit, and the tag must never be moved afterward.

## Main changes

| Area | Release-candidate behavior |
|---|---|
| Runtime-data isolation | Hook/runtime events stay outside canonical File/Git knowledge; legacy events use a redacted, preview-first controlled archive flow |
| Skill discovery | A fresh Codex process parses the model-visible catalog and compares the six canonical OPC Skill names exactly |
| Host isolation | Child environments are deny-by-default; Git config, templates, hooks, signing, and credential helpers are isolated; non-system Skills outside the clean room block acceptance |
| Lifecycle | Candidate install, reapply, removal, reinstall, rollback, version idempotency, and knowledge/config/optional-memory preservation are verified |
| Release refs | Tags or full OIDs resolve to exact commit OIDs first; moving refs, identical OIDs, or version drift fail closed |
| CI | Pull requests and `main` run real Codex installed-state lifecycles on disposable Windows and Linux runners |

## Install the candidate

This installation is intended only for reviewers and release testers:

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.1-rc.1
codex plugin add codex-opc-team@opc
```

After installation, start a new Codex task and verify that `$opc-manager`, `$opc-project-bootstrap`, `$opc-qa-gate`, `$opc-retrospective`, `$opc-memory-curator`, and `$opc-memory` are all visible. A task opened before installation is not valid discovery evidence.

## Data and migration

This candidate does not increment the canonical knowledge schema, so existing File/Git knowledge requires no migration. Installation, removal, and rollback must preserve private knowledge, Git history, and optional Mem0 data.

If an older build placed runtime events in the knowledge tree, `opc-memory` reports only known path metadata and does not read raw content. Archival requires a dry run followed by explicit application of an unchanged plan. Automatic deletion, commit, or upload remains forbidden.

## Roll back to stable

```powershell
codex plugin remove codex-opc-team@opc
codex plugin marketplace remove opc
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

Close the old task after rollback, start a new one, and recheck all six Skills plus File/Git knowledge status. Stable `v0.1.0` does not contain the new installed-lifecycle tooling, but the private knowledge format remains compatible.

## Gate status

| Gate | Requirement before creating the candidate tag |
|---|---|
| Repository validation, full tests, Git-history privacy scan | PASS on the exact candidate commit |
| Official Plugin Validator and six Skill quick validators | PASS on the exact candidate commit |
| Standard PR CI | PASS on Windows and Linux |
| Real installed-state PR CI | PASS on disposable Windows and Linux |
| Original reviewer pre-release review | PASS against the exact candidate commit |
| `v0.1.1-rc.1 → v0.1.0` fixed-ref Gate | Run after the candidate tag exists; currently PENDING |

The fixed-ref Gate sets `release_gate.eligible` only when every exact-OID, distinct-version, idempotency, preservation, exact-discovery, and removal assertion is `true`. A PR branch, `main`, or working-tree result cannot substitute for the tag Gate.

## Known limitations

- This is pre-release software and does not carry the stable-channel compatibility promise.
- The automated `debug prompt-input` Gate does not call a model and does not replace manual new-task checks after install and rollback.
- Mem0 remains optional and supports only the pinned `mem0ai==2.0.11`; its default provider may require OpenAI credentials and send approved content.
- A normal Windows development account may expose real Personal Skills; trusted PASS evidence must come from a disposable OS or container.
