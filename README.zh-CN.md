# Codex OPC Team

[English](README.md) · [v0.1.1-rc.1 候选说明](docs/release-notes-v0.1.1-rc.1.md) · [稳定版 v0.1.0 说明](docs/release-notes-v0.1.0.md) · [架构](docs/architecture.md) · [安全](SECURITY.md) · [路线图](docs/roadmap.md)

Codex OPC Team 是一套开源、Codex 原生的“一人公司”团队运行机制。它把一个想法转化为：目标对齐、任务委派、具体实现、独立验收和有证据的复盘；用户始终扮演经理，只把握方向与关键决策。

本项目不另起一个 Agent 运行时。Codex 继续承担 Harness 的职责，文件操作、浏览器、联网检索、工具调用和子 Agent 编排都复用 Codex 已有能力。

## 它要解决什么问题

每进入一个新项目都从头 vibe coding，会出现三个断点：

| 断点 | 期望变化 |
|---|---|
| 想法只能被单轮响应 | 团队先理解、梳理并形成可验收的工作契约 |
| 用户持续盯住实现细节 | 总管进行内部委派，开发完成后由独立 QA 验收 |
| 问题解决后经验消失 | 经验带着来源和证据进入可移植知识库，供以后检索 |

## 设计原则

| 原则 | 含义 |
|---|---|
| Codex 原生 | 以插件扩展 Codex，不替换成熟 Harness |
| 经理优先 | 日常细节由团队处理，只在方向、风险和范围发生实质变化时升级给用户 |
| 记忆可移植 | Git 管理的文件是持久知识权威源，不绑定某个供应商或运行时 |
| Mem0 可选 | Mem0 只增强语义召回；没有安装、被禁用或发生故障时仍能完整工作 |
| 渐进上下文 | 私有可删的 L0/L1 只负责导航，限制 canonical L2 读取；每个注入叶子都对 current File/Git HEAD 重验 |
| 使用可审计 | 私有角色/步骤链路区分召回、注入、采用、省略和证据关联，不声称因果 |
| 受控成长 | 经验必须经历“候选—验证—经理批准—精确 Git 提交”；只有当前 HEAD 可验证的 approved 条目才能召回，不得静默变成组织规则 |
| 独立验收 | 开发者的自述不是验收证据；独立 QA PASS 后才通知经理体验 |
| 默认私有 | 公开插件代码和私人组织知识严格分离 |

## 团队运行闭环

```mermaid
flowchart LR
    U["经理提出想法"] --> M["总管梳理并对齐"]
    M --> C["形成范围与验收契约"]
    C --> D["委派角色 Agent 实现"]
    D --> Q["独立 QA 验收"]
    Q -->|"FAIL"| D
    Q -->|"PASS"| H["通知经理体验"]
    H --> R["生成复盘候选"]
    R --> V["验证与批准"]
    V --> K["进入可移植知识库"]
```

## 当前状态

`v0.1.0` 是首个稳定版本。Codex 原生团队闭环、File/Git 记忆、可选 Mem0 Adapter、安全 Hook、安装器与自动化门禁均已通过发布检查。稳定安装应固定到 `v0.1.0` 标签，不要使用持续变化的 `main`。兼容范围、迁移、限制、回滚与验证证据见 [v0.1.0 发布说明](docs/release-notes-v0.1.0.md)，后续进度见[路线图](docs/roadmap.md)和[测试与验收契约](docs/testing-and-acceptance.md)。

`v0.1.1-rc.1` 是用于复审更严格运行数据隔离和真实插件生命周期验收的公开候选版，属于预发布版本，不是稳定通道。评审者和发布测试者可用 `codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.1-rc.1` 安装不可变候选快照；正式发布 Gate 通过前，生产使用者应继续固定 `v0.1.0`。详见 [候选发布说明](docs/release-notes-v0.1.1-rc.1.md)。

v0.2 的上下文、反馈、评估、冲突、知识链路与受控能力演进组件已进入 `main`，公开 synthetic release evidence 已通过；但 **v0.2.0 尚未发布就绪**：当前缺少必需的代表性私有 3–5 task pilot 和 exact-release-commit gates。公开 fixture 或模板不能替代这两类证据。详见 [v0.2 发布就绪度](docs/release-readiness-v0.2.0.md)。

## 安装

前置条件：Codex CLI、Git、Python 3.10 或更高版本。Mem0 不是必需依赖。

把 GitHub 仓库的 `v0.1.0` 快照作为 Codex Marketplace 添加并安装插件：

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

默认 File/Git 记忆模式不依赖 Mem0。Mem0 需要用户明确选择后才安装或启用，并且故障时必须安全降级。安装、升级、卸载和数据保留规则详见[安装与分发](docs/installation-and-distribution.md)；本版兼容范围、迁移、回滚和门禁证据见 [v0.1.0 发布说明](docs/release-notes-v0.1.0.md)。

## 安装后快速开始

安装、升级或回滚插件后，先新建一个 Codex 任务，让插件目录重新加载。多数工作直接从 `$opc-manager` 开始；如果只想完成一个独立步骤，可按场景选择其他 Skill。

| 你现在要做什么 | 使用入口 | 你会得到什么 |
|---|---|---|
| 把功能、修复或小项目从想法交付到可体验状态 | `$opc-manager` | 目标对齐、项目契约、角色委派、独立 QA、经理交接和受控复盘 |
| 只把已有仓库纳入 OPC 管理 | `$opc-project-bootstrap` | 最小、可版本化的项目说明与验收契约，不启动实现 |
| 独立验证已有实现 | `$opc-qa-gate` | 逐条验收证据；不满足条件时返回可复现的修复合同，而不是勉强 PASS |
| 从一次完成或失败的任务提炼经验候选 | `$opc-retrospective` | 带来源和证据的候选经验，不自动进入组织规则 |
| 审核候选经验是否值得长期采用 | `$opc-memory-curator` | 作用域、冲突、隐私、回放与回滚检查，以及交给经理的明确批准请求 |
| 查看或管理记忆召回层 | `$opc-memory` | File/Git 状态与 Doctor；可选 Mem0 的安装、重建、停用操作均先预览再授权 |

首次运行 `$opc-manager` 会执行 Doctor。如果私人 File/Git 知识库尚未初始化，它会展示目标目录，并说明初始化将创建一个独立的私有 Git 仓库和基线 Commit；只有得到你的明确确认后才会写入，且不会同时启用 Mem0。

### `main` 开发版本地 Dashboard

`main` 提供一个显式启动、仅本机访问、只读的经理 Dashboard。它不属于稳定版 `v0.1.0`，不会扫描磁盘发现项目，也没有批准或晋升操作：

```powershell
python plugins/codex-opc-team/scripts/opc_dashboard.py --project-root .
```

命令会打印本地 URL 并尝试用默认浏览器打开；按 `Ctrl+C` 停止。数据语义、隐私边界与降级规则见 [OPC Dashboard](docs/opc-dashboard.md)。

### 谁负责什么

| 经理（你） | OPC 团队 |
|---|---|
| 决定产品方向、范围变化、风险取舍和是否继续 | 检查真实仓库，形成任务范围、假设、非目标和验收标准 |
| 明确授权提交、推送、部署、外部消息、凭据、付费或破坏性操作 | 在授权范围内委派角色、实现、测试和修复 |
| 体验通过独立 QA 的结果，并批准或拒绝经验晋升 | 用独立证据验收；实现者自述不能代替 QA PASS |

### 可复制示例

下面的项目名和数据都是合成示例。请在目标仓库中开启新的 Codex 任务，再按实际情况替换示例内容。

**示例 1：端到端交付一个新功能**

```text
$opc-manager
请接管当前仓库，交付“为设置页增加主题切换”。

目标：用户可在浅色、深色和跟随系统之间切换，刷新后仍保留选择。
流程：先与我对齐目标并形成项目契约，再委派实现角色；实现后由独立 QA 验收，PASS 后交给我体验，最后只生成待治理的复盘候选。
验收：三个选项都能在真实界面切换；刷新后状态正确；现有测试通过；新增行为有自动化测试；独立 QA 按同一验收矩阵 PASS。
权限边界：可以修改当前仓库并运行本地测试；不要提交、推送、部署、使用真实账号或修改全局 Codex 配置，除非我另行授权。
非目标：不重做整套设计系统，不增加云端同步。
交接：告诉我改了什么、如何体验、验证证据、已知限制和仍需我决定的事项。
```

**示例 2：把已有仓库接入 OPC**

```text
$opc-project-bootstrap
请把当前已有仓库纳入 OPC 管理。

目标：基于真实 README、AGENTS.md、清单文件和测试命令建立最小项目契约。
验收：生成可版本化的 .opc/project.json、.opc/project.md 和 .opc/acceptance.md；运行文件保持忽略；现有 AGENTS.md 和工作区改动被保留。
权限边界：只创建接入所需的最小文件；修改 AGENTS.md 前先征求我的同意；不要启动实现、提交或推送。
非目标：不重构源码，不创建新服务、数据库或 Agent 运行时。
交接：列出创建的文件、推断出的验证命令、仍缺失的信息，以及是否已可交给 $opc-manager。
```

**示例 3：修复缺陷，并在交接前独立验收和复盘**

```text
$opc-manager
请修复“导出文件名包含冒号时在 Windows 失败”的问题，并完成独立验收和复盘。

目标：Windows 导出会生成合法且可追踪的文件名，其他平台行为不回退。
验收：先复现原问题；覆盖冒号、保留字符、重复文件名和普通文件名；运行相关测试与仓库要求的完整门禁；由非实现角色 QA，失败时回到修复并按同一矩阵复验。
权限边界：可以编辑和测试当前仓库；不要读取真实用户文件，不要提交、推送或发布。
非目标：不改变导出格式，不把平台专用规则扩散到无关模块。
交接：提供复现与修复证据、QA 结论和体验步骤；复盘只能生成候选经验，不得自动批准或改写组织规则。
```

接入后常见的可版本化项目产物是 `.opc/project.json`、`.opc/project.md`、`.opc/acceptance.md` 和需要时的 `.opc/qa/` 证据；活动运行标记与 Hook 回退事件属于本地运行状态，不应提交为公开知识。

以上入口以稳定版 `v0.1.0` 的 6 个 canonical Skills 为准。Mem0 只是可选、可重建的召回索引；`v0.1.1-rc.1` 仍是预发布候选版，`main` 上的 v0.2 组件也不等于已发布稳定能力。本项目没有常驻自治服务，经验不会自动晋升，插件也不会静默提交、部署或修改全局 Codex 配置。

深入了解：[安装与分发](docs/installation-and-distribution.md) · [系统架构](docs/architecture.md) · [测试与验收](docs/testing-and-acceptance.md) · [记忆架构](docs/memory-architecture.md) · [知识治理契约](docs/knowledge-governance.md)

## 公开代码与私人知识

```text
公开仓库                         用户私有目录
├─ Plugin / Skills / Hooks       ├─ 经理档案
├─ Schema 和空白模板             ├─ 项目历史与决策
├─ 测试与脱敏示例                ├─ 已批准组织经验
└─ 技术文档                      └─ 运行状态与可选 Mem0 索引
```

公开仓库不得包含真实经理档案、项目历史、原始聊天、凭据、本机绝对路径、会话标识或 Hook 原始事件。卸载插件不得删除用户私有知识。

Hook/运行事件只进入私有 `PLUGIN_DATA` 或项目 `.opc` 回退，绝不进入权威知识库。`opc-memory` 只用路径元数据报告已知 legacy 事件；归档前必须先 dry-run，再对未变化的计划单独授权。

分层召回为零额外依赖的可选能力。虚拟树、L0/L1 摘要和索引只位于显式 private data root，Git ignored、可删除、可重建，永不成为事实。derived/Provider 缺失、过期、非法、disabled、timeout、error 或 disagreement 都降级 File/Git。公开 synthetic 对比结果为 precision@5 `0.20 → 1.00`、canonical leaf recall@5 `1.00 → 1.00`、median injected tokens `661 → 107`，scope/stale acceptance 均为零；这只证明该 fixture，不是普适性能承诺。详见[分层召回与 ContextPacket](docs/hierarchical-recall.md)。

知识链路是可选的私有 `.opc` sidecar。它把精确 run/project 和 ContextPacket/RecallTrace Hash 关联到角色/步骤状态、Provider 降级、QA、反馈、结果、Shadow 与 evaluation 引用；报告前重验 current File/Git provenance，固定写明 `association/evidence only`，绝不推断采用或因果。详见[知识使用链路](docs/knowledge-lineage.md)。

能力进化是角色、Skill 与组织策略的私有证据门禁生命周期。它要求 exact Git blob、replay/Shadow、独立 QA、经理显式批准、bounded paired pilot，以及只产生一个 unstaged path 的晋升或回滚并由用户显式 commit/confirm；绝不修改全局 Codex 配置或声称因果。详见[受控能力进化](docs/capability-evolution.md)。

## 文档导航

| 文档 | 内容 |
|---|---|
| [v0.1.1-rc.1 候选发布说明](docs/release-notes-v0.1.1-rc.1.md) | 预发布范围、验证状态、已知限制和回滚到稳定版 |
| [v0.1.0 发布说明](docs/release-notes-v0.1.0.md) | 兼容范围、安装、迁移、限制、回滚与发布证据 |
| [缘起与决策](docs/origin-and-decisions.md) | 从真实需求到架构选择的演变 |
| [愿景与范围](docs/vision-and-scope.md) | 产品目标、边界和用户体验 |
| [系统架构](docs/architecture.md) | 分层、组件、契约和执行流程 |
| [记忆架构](docs/memory-architecture.md) | File/Git 权威源、可选 Mem0 与成长治理 |
| [知识治理契约](docs/knowledge-governance.md) | 确定性适用性、关系、冲突与 Schema 1→2 迁移 |
| [安装与分发](docs/installation-and-distribution.md) | 本地安装、Marketplace、升级和卸载 |
| [本地原型迁移](docs/migration-from-local-prototype.md) | 从已安装原型安全切换到开源版 |
| [安全与隐私](docs/security-and-privacy.md) | 数据分类、Hook 边界和公开检查 |
| [测试与验收](docs/testing-and-acceptance.md) | 测试矩阵和发布门槛 |
| [评测基线](docs/evaluation-baseline.md) | 版本化合成 File/Git 基线与私有 3–5 task 聚合协议 |
| [结构化反馈](docs/structured-feedback.md) | 私有、可审计的经理判断、QA 证据、结果与假设记录 |
| [Shadow Evaluation](docs/shadow-evaluation.md) | exact provenance、同契约 control/treatment 与零自动晋升的候选回放 |
| [分层召回](docs/hierarchical-recall.md) | 私有 L0/L1 导航、canonical L2 重验、ContextPacket/RecallTrace 与 flat 对比 |
| [知识使用链路](docs/knowledge-lineage.md) | 私有角色/步骤状态、portable 结果关联、current-HEAD 重验与非因果报告 |
| [受控能力进化](docs/capability-evolution.md) | 角色/Skill/策略版本、证据门禁、单路径 Git 交接、观察与回滚 |
| [OPC Dashboard](docs/opc-dashboard.md) | 显式启动的本地只读经理视图、数据语义、安全边界与已知限制 |
| [v0.2 发布就绪度](docs/release-readiness-v0.2.0.md) | 公开 synthetic 证据、私有 3–5 task 协议、exact-commit Gate、阻断项与非主张 |
| [路线图](docs/roadmap.md) | 分阶段交付计划 |

重大架构选择记录在 [`docs/adr`](docs/adr/README.md) 中。

## 参与贡献与安全报告

提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中披露敏感细节。

## 许可证

本项目采用 Apache License 2.0，见 [LICENSE](LICENSE)。
