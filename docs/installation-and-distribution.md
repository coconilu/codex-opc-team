# 安装与分发

## 1. 分发目标

安装必须满足：从公开 Git 仓库可重复安装；不依赖维护者本机绝对路径；核心模式零 Mem0 依赖；不静默修改 Codex 全局配置；升级和卸载不损坏私人知识。

## 2. 仓库与 Marketplace 布局

```text
codex-opc-team/
├─ .agents/plugins/marketplace.json
└─ plugins/codex-opc-team/
   ├─ .codex-plugin/plugin.json
   ├─ skills/
   ├─ hooks/
   ├─ scripts/
   ├─ assets/
   └─ assets/knowledge-template/
```

Marketplace 名称是 `opc`，插件名称是 `codex-opc-team`。版本由插件清单和 Git 标签共同标识，Release 前必须验证二者一致。

## 3. 发布版本安装

`v0.1.0` 的推荐安装方式：

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

首次安装后开启一个新的 Codex 任务，让插件发现和 Skill 注册状态重新加载。调用 `$opc-manager` 时，它会先运行 Doctor；若私人知识库尚未初始化，会展示目标路径，得到确认后用模板创建私有 Git 仓库和基线 Commit。也可调用 `$opc-memory` 只检查记忆状态。未经确认不得在用户目录创建知识库。

请固定使用 `v0.1.0`；`main` 是持续演进分支，不作为稳定安装源。本版兼容范围、数据与 Schema 迁移、已知限制、回滚和 Gate 证据见 [v0.1.0 发布说明](release-notes-v0.1.0.md)。

### 3.1 公开候选版 `v0.1.1-rc.1`

`v0.1.1-rc.1` 是预发布候选，不替代稳定版 `v0.1.0`。它只供评审和发布验证使用；候选 tag 必须指向已通过预发布复审的精确 commit，不能移动到后续 commit，也不能用 `main` 代替。

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.1-rc.1
codex plugin add codex-opc-team@opc
```

候选安装后必须新建 Codex 任务验证六个 OPC Skills。若需要回滚，先移除候选插件与 Marketplace 条目，再固定安装稳定版：

```powershell
codex plugin remove codex-opc-team@opc
codex plugin marketplace remove opc
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

卸载和回滚不得删除 File/Git 私人知识、Git 历史或可选 Mem0 数据。详细范围、无 Schema Migration 结论、已知限制和 Gate 状态见 [`v0.1.1-rc.1` 候选发布说明](release-notes-v0.1.1-rc.1.md)。

## 4. 本地开发和验证

开发者应克隆仓库，在隔离的 Codex Home 或临时测试环境中添加本地 Marketplace，再执行插件结构验证、安装和端到端测试。不要用正在工作的个人配置作为首个测试环境，也不要同时激活两个同名 Skill 集合。

```mermaid
flowchart LR
    C["Clone"] --> V["验证清单、Skills、Hooks"]
    V --> I["隔离环境安装"]
    I --> N["新任务发现测试"]
    N --> E["端到端 OPC + QA"]
    E --> U["卸载与数据保留测试"]
```

仓库中的辅助安装脚本是便利层，不替代 Codex Marketplace。脚本必须提供预览/说明，不应通过复制缓存目录伪造安装。

在本地克隆目录中，安装器默认只输出计划；只有显式 `--apply` 才会变更隔离环境或当前 Codex Home：

```powershell
python scripts/plugin_admin.py install --dry-run
python scripts/plugin_admin.py install --apply
```

PowerShell 包装命令等价：

```powershell
.\scripts\install.ps1 -DryRun
.\scripts\install.ps1 -Apply
```

## 5. 私人知识初始化

安装插件和创建知识库是两个独立动作：

| 动作 | 结果 |
|---|---|
| 安装插件 | 安装行为、Schema 和空白模板 |
| 初始化知识 | 从模板生成用户拥有的独立私有目录 |
| 接入现有知识 | 验证 Schema 和 Git 状态，不覆盖现有内容 |
| 启用 Mem0 | 创建可重建的可选索引，不迁移权威事实 |

知识目录优先由 `OPC_KNOWLEDGE_HOME` 显式指定；未指定时才使用安装器文档化的用户级默认路径。路径不得硬编码到仓库或全局角色配置。

初始化必须遵守：目标目录非空时不盲目覆盖；生成 `.gitignore` 防止运行日志、索引、虚拟环境和密钥进入 Git；新建知识库包含一个通用 noreply 身份生成的模板基线 Commit；是否创建私人远端由用户自行决定。批准、拒绝或失效迁移由记忆策展流程精确展示 Diff，只提交该迁移涉及的路径，不能顺带提交其他用户改动。

## 6. 无 Mem0 模式

File/Git 是默认模式。插件脚本需要 Python 3.10 或更高版本；除此之外，即使系统没有 Mem0、向量数据库或外部模型凭据，以下能力仍应工作：项目对齐、角色委派、独立 QA、复盘候选、批准知识写入和基线检索。

系统可以在初次 Doctor 中说明 Mem0 的可选价值，但不得阻塞使用或在每个任务中重复推销。

## 7. 可选 Mem0 引导

Mem0 由用户明确选择后接入。引导应按以下顺序：

1. `status/doctor` 显示当前状态和所需依赖；
2. `setup --dry-run` 或等效预览展示环境、路径和数据流；
3. 用户确认后，由 Agent 在 `<data_root>/venv` 建立隔离环境，并从插件包内的 `requirements-mem0.txt` 安装发布锁定的 `mem0ai==2.0.11` 与 SOCKS 代理兼容依赖；`opc_memory.py setup` 本身只预览或写入 OPC 私有配置，不静默安装包，也不写全局 Python；
4. `v0.1` 只承诺 Mem0 2.0.11 默认的 OpenAI-backed LLM/Embedder 配置，启用前必须展示所需 `OPENAI_API_KEY` 和数据流；完全本地或其他 Provider 配置属于后续版本范围；
5. 只把已批准条目的摘要、正文和 canonical 引用元数据交给 Mem0 索引；
6. 先执行 `reindex --dry-run`；确认后用隔离解释器执行 `reindex --apply`。只有在已确认派生索引被删除或需要全量重建时才使用 `--force`；
7. 执行健康检查、召回和故障降级测试；
8. 提供 `disable` 和 `uninstall`，二者均不得删除 File/Git 知识。

云端或自托管 Mem0 Server 不属于 `v0.1` 的承诺范围。

Mem0 OSS 的 `Memory()` 默认配置可能调用 OpenAI，并不因为向量数据在本机就自动成为“完全本地”。OPC 会把 History/Qdrant/配置固定在私有 `data_root` 并关闭 Mem0 Telemetry，但已批准条目的摘要和正文仍可能发给配置的模型/嵌入服务。Mem0 召回结果只用作候选引用，必须通过其 canonical 路径、Commit 和内容哈希元数据回读 File/Git。引导必须展示真实的数据流和所需凭据；参考 [Mem0 Python Quickstart](https://docs.mem0.ai/open-source/python-quickstart)。

## 8. 全局配置策略

核心能力必须依靠插件自身 Skills 和 Codex 通用子 Agent 能力运行，不能要求安装器静默写入用户级 `config.toml`。

如果某些用户选择注册命名角色，配置流程必须：

```text
读取现状 → 生成精确 Diff → 标识所有权 → 用户确认
       → 备份原文件 → 原子写入 → 验证 → 提供恢复命令
```

卸载时只移除由本插件明确拥有且未被用户修改的条目。`multi_agent`、`goals`、`hooks` 等共享 Feature 不得因为卸载 OPC 被盲目关闭。

## 9. 升级

升级前检查：当前插件版本、知识 Schema 版本、运行中的 OPC 任务、用户配置 Diff 和 Git 工作区状态。Schema Migration 必须可预览、可重复且有备份。插件缓存可替换，私人知识和批准历史不可被安装包覆盖。

推荐发布流程：固定标签安装 → 新任务发现 → Doctor → 基线模式验收 → 可选 Mem0 验收 → 一个代表性项目的独立 QA。

## 10. 本地 Dashboard

`main` 上的 Dashboard 不是常驻服务，也不属于稳定版 `v0.1.0`。用户从目标项目显式启动，它只绑定本机 loopback：

```powershell
python plugins/codex-opc-team/scripts/opc_dashboard.py --project-root .
```

关闭进程即可停止。它不写入项目或私人知识；卸载插件前应先停止正在运行的 Dashboard 进程。完整边界见 [OPC Dashboard](opc-dashboard.md)。

## 11. 卸载

卸载分成三个明确范围：

| 范围 | 默认行为 |
|---|---|
| 插件与 Marketplace 条目 | 经用户确认后移除 |
| 插件生成的可选全局配置 | 展示 Diff，仅移除可确认所有权的条目 |
| 私人知识、Git 历史、Mem0 数据 | 全部保留；额外清理必须单独确认 |

不要手动删除 Codex 缓存作为正常卸载方式。插件卸载后，用户仍应能直接读取和迁移 File/Git 知识库。

卸载同样默认只预览；`--remove-marketplace` 是可选范围，私人知识始终保留：

```powershell
python scripts/plugin_admin.py uninstall --remove-marketplace --dry-run
python scripts/plugin_admin.py uninstall --remove-marketplace --apply
```

## 12. 发布检查

每个 GitHub Release 至少包括：版本兼容范围、安装命令、重要变更、数据/Schema Migration、已知限制、回滚步骤和验证证据。稳定版 `v0.1.0` 的完整记录见 [发布说明](release-notes-v0.1.0.md)，候选版记录见 [`v0.1.1-rc.1` 候选发布说明](release-notes-v0.1.1-rc.1.md)，Release Gate 详见[测试与验收](testing-and-acceptance.md)。

固定候选 Ref 的真实安装、全新进程 Skill 发现、卸载、重装、回滚和数据保留，使用[安装态生命周期验收](installed-lifecycle-acceptance.md)。该流程必须同时隔离 `CODEX_HOME` 与操作系统用户 Home；只改 `CODEX_HOME` 仍可能加载真实 Personal Marketplace，不能作为 clean-room 证据。
