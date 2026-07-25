# OPC App

OPC App is an independently installable, explicitly started, loopback-only
private OPC console. It can run without an active Codex, Claude, or Kimi
session, but it is not another Agent harness: it does not run model loops,
tools, logins, keys, proxies, or Agent orchestration.

## 1. Responsibility boundaries

| Component | Owns | Does not own |
|---|---|---|
| OPC App | Visualization, filters, drill-down, explicit projects, App settings | Approval, promotion, commits, deletion, deployment |
| Legacy Dashboard | Compatible read-only view for CLI-supplied projects | Installed App registry or lifecycle |
| Agent harness | Model loop, tools, permissions, and task sessions | OPC App local state |
| Future Adapters (#26) | OPC integration lifecycle in supported hosts | Agent applications, accounts, models, or keys |
| File/Git knowledge | Authoritative knowledge and provenance | UI caches or optional indexes |

The legacy Dashboard and OPC App use `opc_snapshot_service.py` and the same
`opc-dashboard.snapshot.v1` aggregation and redaction contract. There is no
second status implementation.

## 2. Run from a checkout

Synthetic mode reads and writes no private directory:

```text
python plugins/codex-opc-team/scripts/opc_app.py --demo
```

Live mode can start with an empty registry. Use the Projects screen to enter
one absolute directory containing a valid `.opc/project.json`:

```text
python plugins/codex-opc-team/scripts/opc_app.py
```

The authoritative knowledge root and rebuildable data root are never found by
disk scanning. Stop the App and pass both explicitly when switching roots;
remove the overrides to return to the existing OPC environment variables or
platform defaults:

```text
python plugins/codex-opc-team/scripts/opc_app.py \
  --knowledge-root /absolute/path/to/opc-knowledge \
  --data-root /absolute/path/to/opc-private-data
```

The browser receives only redacted knowledge status, never either root path.

The default URL is `http://127.0.0.1:8570/`. Press `Ctrl+C` to stop it; no
Agent process must remain resident. Use `--port` for an explicit alternate
port and `--no-open` for automation.

## 3. Independent install, update, and rollback

Administration defaults to a preview. Only `--apply` writes the App runtime:

```text
python scripts/opc_app_admin.py install
python scripts/opc_app_admin.py install --apply
python scripts/opc_app_admin.py status
```

Launch with `bin\opc-app.cmd` on Windows or `bin/opc-app` on Linux. Both use
the same atomic content-addressed release pointer.

Updates and rollback also preview first:

```text
python scripts/opc_app_admin.py update
python scripts/opc_app_admin.py update --apply
python scripts/opc_app_admin.py rollback
python scripts/opc_app_admin.py rollback --apply
```

The runtime and App state are separate. The default state root is
`%LOCALAPPDATA%\OPC\App` on Windows and
`${XDG_STATE_HOME:-$HOME/.local/state}/opc-app` on Linux. `OPC_APP_HOME`
overrides it. This registry and current-project preference are rebuildable App
settings, not OPC facts.

Uninstall removes only the runtime:

```text
python scripts/opc_app_admin.py uninstall
python scripts/opc_app_admin.py uninstall --apply
```

App state, project `.opc`, File/Git knowledge, Git history, existing user
configuration, and Mem0 data are preserved. This installer does not install
Codex, Claude, or Kimi and does not edit Agent global configuration.

## 4. Project and privacy boundary

The user submits one absolute directory. The server checks only that directory
and `.opc/project.json`, atomically updates App-owned settings, and returns
only a safe project name, portable ID, and `Explicit directory N`. It never
scans the disk.

Responses contain no absolute paths, private bodies, credentials,
session/turn identifiers, Hook payloads, or raw logs. Unreadable projects,
corrupt App settings, incomplete history, and missing/disabled/failing Mem0
are reported as degradation; demo data is never substituted.

## 5. Network and mutation boundary

- Binding is limited to `127.0.0.1` or explicit `::1`.
- Host must exactly match the active loopback authority.
- Origin must be absent or exactly same-origin.
- There is no CORS, remote asset, telemetry, response cache, or default remote
  listener.
- `/api/snapshot` and every governance object remain read-only.
- `POST`/`DELETE` are limited to the App project registry and selection, with
  a same-origin per-process CSRF token.
- Every other method and route is rejected.

Remote access, multi-user identity, governance writes, a complete history
database, and advanced analytics require separate ADRs and acceptance.

## 6. Legacy Dashboard compatibility

The original command and security contract remain available:

```text
python plugins/codex-opc-team/scripts/opc_dashboard.py --project-root .
```

Not installing, not starting, or uninstalling OPC App does not change the
Codex Plugin, Skills, Hook, scripts, legacy Dashboard, or installed-lifecycle
gates.
