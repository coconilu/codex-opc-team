# OPC host adapters

## Product boundary

Adapters manage an OPC integration unit inside an already installed host. They
do not install the host application, manage accounts or models, run Agent
sessions, or change File/Git knowledge authority.

```text
canonical OPC package
        |
        +-- Codex  -> existing marketplace + plugin_admin.py
        +-- Claude -> generated local marketplace plugin
        `-- Kimi   -> documented user Skill directory projection
```

Every mutation is `probe -> preview -> explicit confirmation -> re-probe ->
apply -> fresh-process verification`. The preview is in memory, expires after
120 seconds, is consumed on its first apply attempt, does not survive an App
restart, and performs no write. Before any mutation, the App compares a
constant-time digest over the source version/ref/content, host version and
official discovery result, capability contract, ownership manifest, and
resolved target state. Any drift fails with zero writes.
The ownership manifest is App-private state and contains only adapter/source
versions, source ref, hashes, logical managed targets, and backup references.
It never contains knowledge text, credentials, host configuration, or telemetry.

## Capability matrix

The machine-readable source is
`plugins/codex-opc-team/assets/adapters/capability-matrix.v1.json`.

| Host | Verified contract | Actual managed unit | Important asymmetry |
|---|---|---|---|
| Codex | `>=0.144.1,<0.145.0` | Marketplace Plugin | Reuses `scripts/plugin_admin.py`; it does not copy the Codex cache or enable global roles/features. |
| Claude Code | `>=2.1.212,<3.0.0` | Marketplace Plugin | Uninstall always uses `--keep-data`. Versions below 2.1.212 are blocked because Anthropic documents a same-name cross-marketplace uninstall bug fixed in 2.1.212. |
| Kimi Code CLI | `>=0.29.1,<0.30.0` | User Skill projection | Kimi Plugins exist, but management is currently interactive `/plugins`; the App does not automate that surface. Fresh-process Skill discovery requires a configured model and can remain `verification_required`. |

Unknown versions fail closed. A missing CLI is `unavailable`; an incompatible
version or unusable official discovery result is `blocked`. Neither state
writes an ownership manifest or host file.

## Installation location resolution

| Host | Resolution |
|---|---|
| Codex | The official Codex CLI resolves its own config and cache. The Adapter passes the canonical marketplace source to the existing lifecycle script. |
| Claude | The official Claude CLI resolves user-scope plugin state. The Adapter registers one stable App-owned marketplace source and retains immutable version/hash projections for recovery. |
| Kimi | `$KIMI_CODE_HOME/skills/<skill>` when set, otherwise `~/.kimi-code/skills/<skill>`. Verification passes the common `skills` root once to `--skills-dir` and compares exact canonical Skill identities. Tests always inject a disposable `KIMI_CODE_HOME`. |

The browser API emits logical targets such as `kimi:skill/opc-manager`, not
absolute user-home paths.

## Conflict and recovery rules

- Install refuses an existing unknown target.
- Update and uninstall require an ownership manifest and an unchanged current
  hash. User edits and links are preserved as conflicts.
- Kimi directory swaps use App-owned staging and backups. A partial swap
  restores already moved targets in reverse order.
- Claude update and rollback never remove its marketplace. The stable source
  is swapped atomically, then refreshed with `plugin marketplace update` and
  `plugin update`; every command is checked and a failed step restores the
  previous source and plugin. Uninstall explicitly uses `--keep-data`.
- Codex knowledge initialization is explicitly skipped by the Adapter because
  knowledge lifecycle is independent.
- Uninstall never touches `OPC_KNOWLEDGE_HOME`, project `.opc`, Git history,
  manager preferences/rules/experience, optional Mem0 data, or other hosts.
- Verification failure is not PASS. The operation attempts rollback and returns
  a structured failure or recovery requirement.

## Installed-state QA

Automated tests prove plan/apply separation, expiry/replay/restart behavior,
write-time state fingerprints, fake-home isolation, exact logical Diff,
ownership/hash conflict behavior, Claude intermediate-step recovery, Kimi
common-root discovery, and host command construction.

On 2026-07-25 the Developer also ran a disposable-home acceptance pass. This is
implementation evidence, not independent QA:

| Host | Disposable acceptance result |
|---|---|
| Codex 0.144.1 | Preview, install, fresh JSON discovery, update no-op, uninstall, reinstall, rollback, and unrelated sentinel preservation passed. |
| Claude 2.1.212 | The pinned official npm package (`@anthropic-ai/claude-code@2.1.212`, package SHA-256 `2162841dd793d21671eccb7fe76fe9c3da6816adf447ba3890a4871b7e5f4e69`) passed local marketplace install, real update, fresh JSON discovery, uninstall, reinstall, rollback, and config/plugin-data sentinel preservation. The user's installed 2.1.204 was not upgraded or used. |
| Kimi 0.29.1 | A disposable `KIMI_CODE_HOME` and loopback-only OpenAI-compatible stub passed install, exact seven-Skill request discovery, update no-op, uninstall, reinstall, rollback, and unrelated sentinel preservation. Raw prompts, requests, paths, and session identifiers were not retained. |

Run release QA in disposable host homes:

| Host | Independent acceptance |
|---|---|
| Codex | Install through the App, start a fresh `codex` process, run `codex plugin list --json`, and invoke one OPC Skill. Update, uninstall, rollback, and repeat discovery. |
| Claude | Use Claude Code 2.1.212 or newer, install/update/uninstall, verify with a fresh `claude plugin list --json`, and confirm plugin data remains after uninstall. |
| Kimi | Configure a disposable model/provider, install the Skill projection, start a fresh `kimi` process, invoke `/skill:opc-manager`, then repeat after update, uninstall, and rollback. |

Independent release QA must repeat the relevant gates. Record only
privacy-safe versions, hashes, results, and reviewer identity; keep disposable
home paths and raw command/model traffic out of the public repository. Do not
accept implementer self-report as independent QA evidence.

## Primary sources

- Codex plugins and marketplaces:
  <https://developers.openai.com/plugins/build/plugins>
- Codex CLI plugin commands:
  <https://developers.openai.com/codex/developer-commands>
- Claude plugin discovery:
  <https://code.claude.com/docs/en/discover-plugins>
- Claude plugin reference:
  <https://code.claude.com/docs/en/plugins-reference>
- Claude marketplaces and uninstall compatibility note:
  <https://code.claude.com/docs/en/plugin-marketplaces>
- Kimi Skills:
  <https://moonshotai.github.io/kimi-code/en/customization/skills>
- Kimi Plugins:
  <https://moonshotai.github.io/kimi-code/en/customization/plugins.html>
- Kimi command reference:
  <https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html>
