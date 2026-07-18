# 安装态生命周期验收

## 1. 目标与证据边界

`scripts/plugin_lifecycle_acceptance.py` 使用真实 Codex Plugin/Marketplace CLI，在专用 clean-room 中执行完整生命周期：

```mermaid
flowchart LR
    P["预览计划"] --> I["安装候选版本"]
    I --> D["新进程精确发现 6 个 Skills"]
    D --> A["候选重复安装"]
    A --> U["仅卸载 OPC"]
    U --> N["新进程确认 OPC 为 0"]
    N --> R["重装并接回既有知识"]
    R --> B["回滚上一支持版本"]
    B --> V["复验发现、幂等与数据不变"]
```

自动发现 Gate 调用 Codex 自带的 `debug prompt-input`。每次调用都是新的操作系统进程，读取真实安装态并渲染模型可见 Prompt；它不调用模型、不使用登录凭据，也不创建任务记录。

Skill 发现按完整 canonical name 精确比较：

| 预期 OPC Skill | 安装/重装/回滚 | 卸载后 |
|---|---:|---:|
| `codex-opc-team:opc-manager` | 必须存在 | 必须不存在 |
| `codex-opc-team:opc-project-bootstrap` | 必须存在 | 必须不存在 |
| `codex-opc-team:opc-qa-gate` | 必须存在 | 必须不存在 |
| `codex-opc-team:opc-retrospective` | 必须存在 | 必须不存在 |
| `codex-opc-team:opc-memory-curator` | 必须存在 | 必须不存在 |
| `codex-opc-team:opc-memory` | 必须存在 | 必须不存在 |

子串命中不算通过；缺少任意一个、出现额外 OPC Skill，或无关 fixture Skill 消失，都会失败。

发布负责人仍需在安装、升级和回滚后分别开启一个新的交互式 Codex 任务，完成 UI/交互级抽查。自动 Gate 不能替代这一步，也不能把 `plugin list` 成功误报成新任务发现成功。

## 2. Clean-room 隔离

只设置 `CODEX_HOME` 不足以隔离 Personal Skills。Codex 还会扫描工作区祖先的 `.agents/skills` 和操作系统用户目录，因此脚本使用独立 Git probe、最小子进程环境和专用数据根：

| 边界 | clean-room 值或策略 |
|---|---|
| Codex 配置与插件缓存 | `<workspace>/codex-home` |
| `HOME`、`USERPROFILE` | `<workspace>/user-home` |
| `APPDATA`、`LOCALAPPDATA` | `<workspace>/appdata`、`<workspace>/localappdata` |
| XDG 配置、数据、缓存 | `<workspace>/xdg-*` |
| 临时文件 | `<workspace>/runtime-tmp` |
| 插件运行数据 | `<workspace>/plugin-data` |
| File/Git 知识 | `<workspace>/knowledge` |
| 可选记忆数据 | `<workspace>/memory-data` |
| Git global/system config | 禁用；使用空白 config、template 与 hooks 目录 |
| Git 签名/凭据 Helper | 禁用签名、交互和 credential helper |
| 凭据、会话、SSH Agent | 不从宿主环境继承 |

发现结果会检查每个 file-backed Skill 的 locator。Codex 自带的 `skills/.system` 可以位于 CLI 安装根；除此之外，任何 clean-room 外的用户、仓库或插件 Skill 都会阻断验收。测试还会注入宿主 `.agents/skills` sentinel、凭据/会话 sentinel 和恶意 Git config/template/hook，证明它们不可见且不执行。

Windows 上 Codex 可能仍根据操作系统账户解析真实用户目录，而忽略进程级 `HOME`/`USERPROFILE`。因此，普通开发机若存在真实 Personal Skills，脚本应安全失败；真实安装态 PASS 只能在明确的一次性 Windows/Linux Runner、容器或 disposable OS 中产生。设置一个环境变量不能把普通开发机变成可信的一次性环境。

工作目录必须为空，或包含本工具生成的所有权 Marker。公开仓库、真实 Home、真实 `CODEX_HOME` 和无 Marker 的非空目录都会失败关闭。Fixture 只包含合成知识、禁用的可选记忆 sentinel、无关配置和无关测试插件，不读取或复制真实私人知识。

## 3. 本地预览与包级检查

PowerShell：

```powershell
$cleanRoom = Join-Path $env:TEMP "opc-lifecycle-local"
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
clean_room="${TMPDIR:-/tmp}/opc-lifecycle-local"
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

本地路径模式验证当前包的安装、发现、卸载、重装、候选/回滚重复执行和数据保留，但不能证明发布标签正确。它的 `release_gate.eligible` 必须为 `false`。

## 4. 不可变版本发布 Gate

候选标签存在后，使用公开 Git Marketplace 和两个不同的不可变 Ref。`v0.1.1-rc.1` 的正式 Gate 固定验证候选版回滚到稳定版 `v0.1.0`：

```powershell
python scripts/plugin_lifecycle_acceptance.py `
  --workspace $cleanRoom `
  --candidate-source coconilu/codex-opc-team `
  --candidate-ref v0.1.1-rc.1 `
  --expected-candidate-version 0.1.1-rc.1 `
  --rollback-source coconilu/codex-opc-team `
  --rollback-ref v0.1.0 `
  --expected-rollback-version 0.1.0 `
  --require-fixed-refs `
  --report (Join-Path $cleanRoom "report.json") `
  --apply
```

脚本不会把输入字符串直接当作“固定 Ref”。它会在隔离 Git 环境中解析远端 Ref，并把每次 Marketplace 安装钉到解析后的完整 commit OID。

| 检查 | 失败条件 |
|---|---|
| Ref 类型 | 分支等 moving ref |
| Ref 解析 | 不存在、不能解析成 commit OID |
| 候选/回滚差异 | 两个 Ref 解析到同一 OID |
| Manifest | 实际版本与期望版本不一致，或候选/回滚版本相同 |
| 幂等 | 重复安装后版本漂移 |
| 重装 | 未恢复完全相同的候选版本 |
| 总 Gate | 任一发布断言不是 `true` |

`release_gate.eligible` 由全部发布断言共同计算，不能由命令行开关直接置真。候选发布标签尚未创建时，该 Gate 必须保持“待发布执行”，不能用工作区、分支或相同版本替代。

## 5. CI 分层

`.github/workflows/installed-lifecycle.yml` 固定安装 Codex CLI `0.144.1`：

| 触发 | Windows/Linux 行为 | 能证明什么 |
|---|---|---|
| Pull Request、push 到 `main` | disposable Runner 中用真实 Codex 对两个合成本地版本跑完整生命周期和宿主 sentinel 负测 | 当前包安装态、精确发现、卸载、重装、回滚、幂等、隔离 |
| 手动 `workflow_dispatch` | 输入候选/回滚 tag 或完整 OID，解析并钉住两个远端 commit 后执行 | 固定版本发布与回滚 Gate |

固定 Codex CLI 的工具/网络步骤与包生命周期步骤分开。前者失败不解释为包错误；后者失败会在脱敏报告中标识阶段。手动发布 Job 会上传保留 14 天的脱敏报告。

## 6. 自动比较与报告

| 对象 | 验证方式 |
|---|---|
| 权威知识 | Git HEAD、完整 Commit 列表、Schema、工作区状态、逐文件 SHA-256 |
| 已批准经验 | 独立逐文件 SHA-256 |
| 可选记忆数据 | 配置和 provider sentinel 逐文件 SHA-256 |
| 无关 Codex 配置 | 排除精确 OPC-owned TOML 表后的语义归一化 Hash |
| 无关插件 | 安装后始终存在，并在新进程中保持精确可发现 |
| OPC Skills | 安装/重装/回滚为精确 6/6；卸载后精确 0/6 |
| 安装版本 | 候选、重装、回滚及两次 reapply 均按阶段精确比较 |

公开报告不写入 clean-room 绝对路径、CLI 原始输出、缓存、凭据、任务/会话标识或 Hook Payload。真实路径只会出现在操作者本机预览和临时诊断中。

## 7. 失败恢复与人工 Gate

| 失败阶段 | 处理方式 |
|---|---|
| Codex CLI 安装或 Git 拉取 | 检查网络、代理和 Ref；不降低验收标准 |
| clean-room 外 Skill 可见 | 改用 disposable OS/container；不把宿主 Skill 加白名单 |
| 安装/精确发现 | 保留 clean-room 和脱敏报告，修复包或清单后重跑 |
| 数据、配置 Hash 变化 | 立即阻断；不得通过重建 Fixture 掩盖差异 |
| 回滚或幂等失败 | 保持固定 OID 不变，修复兼容问题后完整重跑 |
| 交互新任务失败 | 记录 Codex 版本和脱敏现象，不复制个人配置或登录材料 |

重复 `--dry-run` 不创建 clean-room。重复 `--apply` 只清理工具拥有的安装状态，复用原有 synthetic knowledge 与 memory data；Marker 缺失或内容被人工修改时拒绝覆盖。

人工 Gate 使用专用测试账号或隔离配置开启新任务，依次确认 `$opc-manager`、`$opc-project-bootstrap`、`$opc-qa-gate`、`$opc-retrospective`、`$opc-memory-curator`、`$opc-memory` 可见。升级和回滚后必须关闭旧任务并重新创建。
