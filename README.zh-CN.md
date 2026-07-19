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

## 安装

前置条件：Codex CLI、Git、Python 3.10 或更高版本。Mem0 不是必需依赖。

把 GitHub 仓库的 `v0.1.0` 快照作为 Codex Marketplace 添加并安装插件：

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

默认 File/Git 记忆模式不依赖 Mem0。Mem0 需要用户明确选择后才安装或启用，并且故障时必须安全降级。安装、升级、卸载和数据保留规则详见[安装与分发](docs/installation-and-distribution.md)；本版兼容范围、迁移、回滚和门禁证据见 [v0.1.0 发布说明](docs/release-notes-v0.1.0.md)。

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

## 文档导航

| 文档 | 内容 |
|---|---|
| [v0.1.1-rc.1 候选发布说明](docs/release-notes-v0.1.1-rc.1.md) | 预发布范围、验证状态、已知限制和回滚到稳定版 |
| [v0.1.0 发布说明](docs/release-notes-v0.1.0.md) | 兼容范围、安装、迁移、限制、回滚与发布证据 |
| [缘起与决策](docs/origin-and-decisions.md) | 从真实需求到架构选择的演变 |
| [愿景与范围](docs/vision-and-scope.md) | 产品目标、边界和用户体验 |
| [系统架构](docs/architecture.md) | 分层、组件、契约和执行流程 |
| [记忆架构](docs/memory-architecture.md) | File/Git 权威源、可选 Mem0 与成长治理 |
| [安装与分发](docs/installation-and-distribution.md) | 本地安装、Marketplace、升级和卸载 |
| [本地原型迁移](docs/migration-from-local-prototype.md) | 从已安装原型安全切换到开源版 |
| [安全与隐私](docs/security-and-privacy.md) | 数据分类、Hook 边界和公开检查 |
| [测试与验收](docs/testing-and-acceptance.md) | 测试矩阵和发布门槛 |
| [评测基线](docs/evaluation-baseline.md) | 版本化合成 File/Git 基线与私有 3–5 task 聚合协议 |
| [结构化反馈](docs/structured-feedback.md) | 私有、可审计的经理判断、QA 证据、结果与假设记录 |
| [路线图](docs/roadmap.md) | 分阶段交付计划 |

重大架构选择记录在 [`docs/adr`](docs/adr/README.md) 中。

## 参与贡献与安全报告

提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中披露敏感细节。

## 许可证

本项目采用 Apache License 2.0，见 [LICENSE](LICENSE)。
