# OPC Desktop（Tauri）

OPC Desktop 是现有 OPC App 的 Windows 桌面生命周期与分发层。它不是第二套
OPC 服务，也不是 Agent Harness：Tauri 只负责单实例、窗口、随包 sidecar 的
启动/停止和 NSIS 安装；Snapshot、脱敏、项目接入、Adapters、Memory 与治理
规则仍由现有 Python 模块提供。

## 架构与信任边界

```text
OPC App.exe（单实例）
  → 固定启动 opc-sidecar --no-open --host 127.0.0.1 --port 0
  → 只接受 OPC App: http://127.0.0.1:<port>/
  → 确认 loopback 端口可用
  → WebView 加载现有 UI/API
  → 窗口退出或 sidecar 崩溃时只回收自有 child
```

| 层 | 可以做 | 明确不能做 |
|---|---|---|
| Tauri Rust | 单实例、固定 sidecar 生命周期、窗口、分发 | Snapshot、治理、Adapter/Memory 规则 |
| WebView | 调用已有同源 HTTP API | 任意 shell、文件系统、远程 HTTP、进程参数 |
| Python sidecar | 现有 OPC App 契约 | 远程监听、Agent 模型循环、遥测 |
| NSIS | current-user 程序文件安装/卸载 | 删除 App 状态、项目、知识或 Agent 配置 |

完整决策见 [ADR-0019](adr/0019-tauri-desktop-shell-managed-python-sidecar.md)。

## 构建要求

Windows clean build 需要：

- Python 3.10+（仅构建 sidecar）；
- Node.js 20+ 与 lockfile 对应的 npm；
- Rust MSVC toolchain；
- Tauri 的 Windows/WebView2/NSIS 构建前置条件。

安装后的 App 不依赖这些开发工具，也不依赖 Codex、Claude、Kimi 或任何 Agent
会话。构建命令：

```powershell
Set-Location apps/opc-desktop
npm ci --ignore-scripts
npm run tauri:build
```

`beforeBuildCommand` 会在项目内 `.sidecar-build` 隔离环境安装
`requirements-build.txt` 的精确版本，以 PyInstaller 打包当前 checkout 的公开
plugin snapshot，并按 Rust target triple 输出 Tauri external binary。以下均不会
进入 Git：Python 环境、sidecar、Cargo target、NSIS installer、WebView2 和日志。

只验证 Rust 契约而不生成 sidecar 时：

```powershell
$env:TAURI_CONFIG='{"bundle":{"externalBin":[]}}'
cargo fmt --manifest-path src-tauri/Cargo.toml -- --check
cargo test --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
Remove-Item Env:TAURI_CONFIG
```

## 开发安装包与验收

成功构建后，未签名 NSIS 安装包位于
`apps/opc-desktop/src-tauri/target/release/bundle/nsis/`。它仅用于开发验收，
可能触发 SmartScreen；没有签名与发布证据时不得宣称生产就绪或 Store 发布。

验收必须使用 installer 安装后的新进程，而不是 `cargo run`，并逐项证明：

1. 在未提供系统 Python/Node/Cargo 的子进程环境中打开真实 WebView；
2. 根页面及 `/api/app-context`、项目、Dashboard、Adapters 流程可用；
3. 第二次启动不产生第二个 sidecar；
4. 关闭后不残留本次 App 创建的 sidecar；
5. 卸载后预置的 `%LOCALAPPDATA%\OPC\App` 状态标记仍存在。

实现者自测不能代替独立 Reviewer 的安装态与 UI 验收。签名、自动更新、
Microsoft Store 和 macOS/Linux 安装包不属于当前交付。

## 故障与恢复

| 现象 | 行为/处理 |
|---|---|
| sidecar 缺失或完整性/启动失败 | 主窗口不显示，进程 fail closed；重新安装同一可信包 |
| 输出不是精确 loopback URL | 拒绝导航并回收自有 child |
| 启动超时或 sidecar 崩溃 | 桌面进程退出；检查 App 状态根可访问性后重启 |
| 第二次启动 | 聚焦已有窗口，不启动第二个 sidecar |
| 卸载 | 只删除程序文件；App 状态和所有业务数据保留 |

现有 Python runtime 安装器仍是兼容入口，见 [OPC App](opc-app.md)。两者读取同一
App 状态契约，但不要并行启动两个可写实例。
