# OPC Desktop (Tauri)

OPC Desktop is the Windows lifecycle and distribution layer for the existing
OPC App. It is neither a second OPC service nor an Agent harness. Tauri owns
single-instance enforcement, the window, its packaged sidecar, and NSIS
distribution; the existing Python modules remain authoritative for Snapshot,
redaction, explicit projects, Adapters, Memory, and governance behavior.

## Architecture and trust boundary

```text
OPC App.exe (single instance)
  -> fixed opc-sidecar --no-open --host 127.0.0.1 --port 0
  -> accept only OPC App: http://127.0.0.1:<port>/
  -> confirm the loopback listener
  -> load the existing UI/API in the WebView
  -> stop only the owned child on window exit or sidecar failure
```

| Layer | May do | Must not do |
|---|---|---|
| Tauri Rust | Single instance, fixed sidecar lifecycle, window, distribution | Snapshot, governance, Adapter/Memory policy |
| WebView | Call the existing same-origin HTTP API | Arbitrary shell, filesystem, remote HTTP, or process arguments |
| Python sidecar | Existing OPC App contract | Remote binding, Agent model loop, or telemetry |
| NSIS | Install/uninstall current-user program files | Delete App state, projects, knowledge, or Agent configuration |

See [ADR-0019](adr/0019-tauri-desktop-shell-managed-python-sidecar.md) for the
accepted boundary.

## Build

A clean Windows build requires Python 3.10+ (build-time only), Node.js 20+,
the Rust MSVC toolchain, and Tauri's Windows/WebView2/NSIS prerequisites.
Installed users need none of those tools and no active Agent session.

```powershell
Set-Location apps/opc-desktop
npm ci --ignore-scripts
npm run tauri:build
```

The Tauri pre-build command creates a project-local isolated environment,
installs the exact `requirements-build.txt` versions, packages the current
public plugin snapshot with PyInstaller, and emits the target-triple external
binary. Python environments, sidecars, Cargo targets, installers, WebView2
payloads, and logs are never committed.

For Rust contract checks without building the sidecar:

```powershell
$env:TAURI_CONFIG='{"bundle":{"externalBin":[]}}'
cargo fmt --manifest-path src-tauri/Cargo.toml -- --check
cargo test --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
Remove-Item Env:TAURI_CONFIG
```

## Development installer and acceptance

The unsigned NSIS installer is emitted under
`apps/opc-desktop/src-tauri/target/release/bundle/nsis/`. It is for development
acceptance and can trigger SmartScreen. It is not production- or Store-ready
without signing and release evidence.

The repository's `.github/workflows/desktop-build.yml` reproduces the same
build on `windows-latest` with Node.js 24, Python 3.12, and Rust stable, then
uploads exactly one unsigned NSIS installer and its SHA-256 for 14 days. Pull
requests, `main`, version tags, and manual dispatch are supported. The workflow
has only `contents: read` permission and does not create a Release, sign, or
publish the installer.

There is no portable build today. Tauri only configures a current-user NSIS
target, and App state remains under `%LOCALAPPDATA%\OPC\App` by default.
Copying `opc-desktop.exe` alone omits the managed sidecar and is not a portable
distribution. A future portable ZIP needs explicit relative-state, upgrade and
rollback, WebView2 prerequisite, sidecar-integrity, and zero-residue contracts;
zipping the Release directory is not sufficient.

Acceptance must use a newly installed process, not `cargo run`, and prove the
real WebView/API, single-instance behavior, owned-child cleanup, and state
preservation after uninstall. Implementer self-tests do not replace an
independent Reviewer's installed and UI evidence.

| Failure | Expected recovery |
|---|---|
| Missing or failed sidecar | No main window; reinstall the same trusted package |
| Non-exact loopback startup URL | Reject navigation and stop the owned child |
| Startup timeout or runtime crash | Exit the desktop process; diagnose state-root access and restart |
| Second launch | Focus the existing window without a second sidecar |
| Uninstall | Remove only program files and preserve all App/business state |

The existing Python runtime remains a compatible entry; see
[OPC App](opc-app.en.md). Do not run two writable App processes against the
same state root.
