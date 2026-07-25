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

Every mutation is `probe -> preview -> explicit confirmation -> apply ->
fresh-process verification`. The preview is in memory and performs no write.
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
| Claude | The official Claude CLI resolves user-scope plugin state. The Adapter retains a generated, versioned marketplace projection under App-owned state. |
| Kimi | `$KIMI_CODE_HOME/skills/<skill>` when set, otherwise `~/.kimi-code/skills/<skill>`. Tests always inject a disposable `KIMI_CODE_HOME`. |

The browser API emits logical targets such as `kimi:skill/opc-manager`, not
absolute user-home paths.

## Conflict and recovery rules

- Install refuses an existing unknown target.
- Update and uninstall require an ownership manifest and an unchanged current
  hash. User edits and links are preserved as conflicts.
- Kimi directory swaps use App-owned staging and backups. A partial swap
  restores already moved targets in reverse order.
- Claude data is preserved on uninstall. Codex knowledge initialization is
  explicitly skipped by the Adapter because knowledge lifecycle is independent.
- Uninstall never touches `OPC_KNOWLEDGE_HOME`, project `.opc`, Git history,
  manager preferences/rules/experience, optional Mem0 data, or other hosts.
- Verification failure is not PASS. The operation attempts rollback and returns
  a structured failure or recovery requirement.

## Manual installed-state QA

Automated tests prove plan/apply separation, one-time confirmation, fake-home
isolation, exact logical Diff, ownership/hash conflict behavior, partial-failure
recovery, and host command construction. They do not prove real logged-in host
discovery.

Run release QA in disposable host homes:

| Host | Independent acceptance |
|---|---|
| Codex | Install through the App, start a fresh `codex` process, run `codex plugin list --json`, and invoke one OPC Skill. Update, uninstall, rollback, and repeat discovery. |
| Claude | Use Claude Code 2.1.212 or newer, install/update/uninstall, verify with a fresh `claude plugin list --json`, and confirm plugin data remains after uninstall. |
| Kimi | Configure a disposable model/provider, install the Skill projection, start a fresh `kimi` process, invoke `/skill:opc-manager`, then repeat after update, uninstall, and rollback. |

Record the exact host versions, disposable home roots, command output, and
reviewer identity outside the public repository. Do not accept implementer
self-report as independent QA evidence.

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
