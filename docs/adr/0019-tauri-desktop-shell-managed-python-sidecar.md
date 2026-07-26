# ADR-0019: Tauri desktop shell owns a bundled Python sidecar

- Status: Accepted
- Date: 2026-07-26
- Supersedes: ADR-0017 decision 8 only
- Depends on: ADR-0001, ADR-0002, ADR-0005, ADR-0017, ADR-0018

## Context

ADR-0017 deliberately avoided a desktop build chain because the first control
plane had not yet proved a distribution need. The Python App and Host Adapters
now provide that evidence, while users still need a system Python, a command
prompt, and a browser tab. The product requirement is an independently
installable desktop window that survives Agent session changes.

Replacing the Python service with Rust would create a second Snapshot,
redaction, governance, Memory, and Adapter implementation. Letting web content
spawn commands or choose sidecar arguments would instead expand the control
plane into an unsafe general-purpose execution surface.

## Decision

1. `apps/opc-desktop` is a Windows-first Tauri v2 lifecycle and distribution
   shell. Tauri is not an Agent Harness and receives no model, tool, approval,
   promotion, or autonomous execution responsibility.
2. The existing `opc_app.py` service remains the only App business entry. A
   pinned, isolated PyInstaller build packages the public plugin snapshot and
   Python interpreter as a Tauri external binary. Installed users need neither
   system Python, Node, Rust nor an active Agent session.
3. Rust starts exactly its packaged binary with fixed arguments
   `--no-open --host 127.0.0.1 --port 0`. It accepts only the first bounded
   UTF-8 stdout line matching `OPC App: http://127.0.0.1:<1-65535>/`, confirms
   the port is listening, and only then creates the main WebView.
4. Startup timeout, malformed output, remote/ambiguous URL, early termination,
   or window creation failure is fail closed. Tauri kills only the child
   handle it created. Runtime sidecar termination exits the desktop process;
   normal App exit also stops that owned child.
5. The single-instance plugin is registered before sidecar startup. A second
   desktop process focuses the existing window and never starts another
   sidecar, preserving ADR-0017's single-writer state boundary.
6. The WebView navigates to the existing random loopback origin. It therefore
   uses the existing Host/Origin/CSRF/CSP/no-CORS API contract without a
   duplicated frontend or custom IPC transport.
7. No frontend capability grants shell, process, filesystem, remote HTTP,
   arbitrary URL opening, or arbitrary sidecar arguments. The Rust shell
   plugin is used internally only; no JavaScript API dependency or capability
   file is shipped.
8. The first bundle is an unsigned Windows current-user NSIS installer. Its
   install directory contains replaceable program files only. App state remains
   under the existing platform state root, so uninstall must not remove
   projects, `.opc`, File/Git knowledge, Git history, Agent configuration, or
   optional Mem0 data.
9. Lockfiles and exact top-level build dependencies are committed. Generated
   sidecars, installers, Rust targets, Python environments, WebView2 payloads,
   logs, private state, and signing material are ignored.

## Consequences

Users receive a double-clickable, single-window App with no system Python
runtime dependency, while the Python App/CLI remains compatible. The desktop
shell adds Node, Cargo, MSVC, PyInstaller, NSIS, and WebView2 build/acceptance
surface for contributors. An unsigned development installer can trigger
Windows warnings and is not production- or Store-ready.

The first release is Windows-only. Signing, Store publication, automatic
updates, macOS/Linux bundles, background startup, remote access, and a Rust
business-logic port each require separate evidence and decisions.

## Rejected Alternatives

| Alternative | Reason |
|---|---|
| Rewrite the App service in Rust | Duplicates authoritative behavior and creates contract drift |
| Expose shell/sidecar commands to JavaScript | Turns local content into a general execution surface |
| Fixed loopback port | Creates avoidable conflicts and weakens concurrent-process isolation |
| Bundle a second knowledge database | Violates File/Git authority and complicates uninstall |
| Reuse the Python runtime installer as the desktop App | Still requires a command/browser flow and does not own a native window |
| Claim cross-platform or production readiness now | No signed or installed evidence exists for those release channels |
