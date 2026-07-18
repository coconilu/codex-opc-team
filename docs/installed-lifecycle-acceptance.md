# 安装态生命周期验收

## 1. 目标与证据边界

`scripts/plugin_lifecycle_acceptance.py` 使用真实 Codex Plugin/Marketplace CLI，在专用 clean-room 中执行以下链路：

```mermaid
flowchart LR
    P["预览计划"] --> I["固定候选版本安装"]
    I --> D["新进程发现 6 个 Skills"]
    D --> U["仅卸载 OPC"]
    U --> N["新进程确认 OPC 已消失"]
    N --> R["重装并接回现有知识"]
    R --> B["回滚到上一支持版本"]
    B --> V["复验发现与数据不变"]
```

自动发现 Gate 调用 Codex 自带的 `debug prompt-input`。每次调用都是新的操作系统进程，读取真实已安装插件并渲染模型可见 Prompt；它不调用模型、不使用登录凭据，也不创建任务记录。这比检查缓存目录更接近真实任务加载，同时不会把网络、模型或账号状态混入包正确性证据。

发布负责人仍需在安装、升级和回滚后各开启一个新的交互式 Codex 任务完成 UI/交互级抽查。自动 Gate 不能替代这一步，也不得把 `plugin list` 的成功误报成新任务发现成功。

## 2. Clean-room 隔离

只设置 `CODEX_HOME` 不足以隔离 Personal Marketplace。Codex 还可能从操作系统用户 Home 解析 Personal Marketplace，因此验收脚本同时绑定：

| 边界 | clean-room 值 |
|---|---|
| Codex 配置与插件缓存 | `<workspace>/codex-home` |
| `HOME`、`USERPROFILE` | `<workspace>/user-home` |
| `APPDATA`、`LOCALAPPDATA` | `<workspace>/appdata`、`<workspace>/localappdata` |
| XDG 配置、数据、缓存 | `<workspace>/xdg-*` |
| File/Git 知识 | `<workspace>/knowledge` |
| 可选记忆数据 | `<workspace>/memory-data` |

子进程会移除 `*_API_KEY` 和常见云端凭据环境变量，并关闭 Mem0 Telemetry。工作目录必须为空，或包含本工具生成的所有权 Marker；公开仓库、真实 Home、真实 `CODEX_HOME` 和无 Marker 的非空目录会失败关闭。

Fixture 只含合成数据：一个两次 Commit 的 File/Git 知识库、一条批准经验、禁用的 Mem0 配置及 provider sentinel、一个无关 Codex 配置项，以及一个无关测试插件。脚本不会读取或复制真实私人知识。

## 3. 本地预览与包验收

PowerShell：

```powershell
$cleanRoom = Join-Path $env:TEMP "opc-lifecycle-v0.1.1"
python scripts/plugin_lifecycle_acceptance.py `
  --workspace $cleanRoom `
  --candidate-source . `
  --rollback-source . `
  --dry-run

python scripts/plugin_lifecycle_acceptance.py `
  --workspace $cleanRoom `
  --candidate-source . `
  --rollback-source . `
  --report (Join-Path $cleanRoom "report.json") `
  --apply
```

Linux：

```bash
clean_room="${TMPDIR:-/tmp}/opc-lifecycle-v0.1.1"
python3 scripts/plugin_lifecycle_acceptance.py \
  --workspace "$clean_room" \
  --candidate-source . \
  --rollback-source . \
  --dry-run

python3 scripts/plugin_lifecycle_acceptance.py \
  --workspace "$clean_room" \
  --candidate-source . \
  --rollback-source . \
  --report "$clean_room/report.json" \
  --apply
```

本地路径模式验证当前包的安装、发现、卸载、重装、重复执行和数据保留，但不能证明发布标签正确。报告中的 `fixed_ref=false` 或 `distinct_version=false` 不得用作发布回滚证据。

## 4. 固定版本发布 Gate

候选标签存在后，使用公开 Git Marketplace 和两个不同的固定 Ref：

```powershell
python scripts/plugin_lifecycle_acceptance.py `
  --workspace $cleanRoom `
  --candidate-source coconilu/codex-opc-team `
  --candidate-ref <candidate-tag> `
  --expected-candidate-version <candidate-version> `
  --rollback-source coconilu/codex-opc-team `
  --rollback-ref v0.1.0 `
  --expected-rollback-version 0.1.0 `
  --require-fixed-refs `
  --report (Join-Path $cleanRoom "report.json") `
  --apply
```

`--require-fixed-refs` 会拒绝本地路径、缺失 Ref 或相同的候选/回滚 Ref。候选版本和清单版本不一致时也会失败。

GitHub Actions 的 `Installed lifecycle acceptance` 手动工作流在 Windows/Linux 上执行同一命令。工作流把“安装固定 Codex CLI”作为独立的工具/网络步骤，把“执行本地包生命周期”作为后续步骤；前者失败不能解释为包错误，后者失败会在脱敏报告中标识失败阶段。

## 5. 自动比较内容

| 对象 | 验证方式 |
|---|---|
| 权威知识 | Git HEAD、完整 Commit 列表、Schema、工作区状态、逐文件 SHA-256 |
| 已批准经验 | 独立逐文件 SHA-256 |
| 可选记忆数据 | 配置和 provider sentinel 逐文件 SHA-256 |
| 无关 Codex 配置 | 排除精确 OPC-owned TOML 表后的语义归一化 Hash |
| 无关插件 | 安装后始终存在，并在新进程中保持可发现 |
| OPC Skills | 安装/重装/回滚时 6/6；卸载后 0/6 |

公开报告不写入 clean-room 绝对路径、CLI 原始输出、缓存、凭据、任务/会话标识或 Hook Payload。真实路径只会出现在操作者本机的预览和 Codex CLI 临时诊断中。

## 6. 失败恢复与人工新任务 Gate

| 失败阶段 | 处理方式 |
|---|---|
| Codex CLI 安装或 Git Marketplace 拉取 | 单独检查网络、代理、Ref 是否存在；不要修改验收标准 |
| 本地插件安装/发现 | 保留 clean-room 和脱敏报告，修复包或清单后复用同一 Marker 工作区重跑 |
| 数据或无关配置 Hash 变化 | 立即视为阻断；不得通过重建 Fixture 掩盖差异 |
| 回滚发现失败 | 保持上一支持版本 Ref 不变，修复回滚兼容问题后完整重跑 |
| 交互任务发现失败 | 确认任务确实在安装后新建；记录 Codex 版本和脱敏现象，不复制个人配置或登录材料 |

重复 `--dry-run` 不创建目录。重复 `--apply` 会先按精确 ID 移除本工具拥有的 OPC/fixture 安装态，再复用原有 synthetic knowledge 与 memory data；无 Marker 或内容被人工修改时拒绝覆盖。

人工 Gate 使用专用测试账号或隔离测试配置开启新任务，依次确认 `$opc-manager`、`$opc-project-bootstrap`、`$opc-qa-gate`、`$opc-retrospective`、`$opc-memory-curator`、`$opc-memory` 可见。升级和回滚后必须关闭旧任务并重新新建；不得把已加载任务继续可用当作新版本发现证据。
