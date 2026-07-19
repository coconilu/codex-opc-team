# 路线图

路线图按可验证能力而不是日期承诺组织。每个阶段只有通过[测试与验收](testing-and-acceptance.md)中的对应 Gate 才算完成。

## 当前状态

| 阶段 | 状态 | 目标 |
|---|---|---|
| R0 开源基础 | 完成 | 干净仓库、Marketplace、文档、安全迁移和回滚基线 |
| R1 v0.1 核心闭环 | 完成 | Codex 原生团队、File/Git 记忆、独立 QA |
| R2 v0.1 可选 Mem0 | 完成 | 可引导安装、可降级、可卸载的 OSS Adapter |
| R3 v0.1 发布 | 完成 | Windows/Linux CI、固定标签安装和 Release |
| R4 反馈成长增强 | 发布验证阻断 | 组件与 public synthetic evidence 已完成；真实 private 3–5 task pilot 和 exact-commit Gate 尚缺 |
| R5 生态与企业能力 | 候选 | 角色包、策略层、更多后端和官方分发探索 |

## R0：开源基础与安全切换

交付内容：

- 公开仓库和 Codex Git Marketplace 布局；
- Apache-2.0、贡献、安全和完整技术文档；
- 从本地原型选择性迁移，不带私人数据和绝对路径；
- 修复 Hook 在验证 OPC Run Marker 前记录事件的问题；
- 快照、停用旧版、隔离验证和回滚方案；
- 基础 CI、隐私扫描和插件结构验证。

退出条件：非 OPC Hook 零记录；公开历史隐私扫描干净；旧版可恢复。

## R1：v0.1 Codex 原生核心

交付内容：

- 项目 Bootstrap、Project Brief 与 Acceptance Contract；
- OPC Manager 对齐、委派、升级和经理交接协议；
- 产品/研究、开发、独立 QA、复盘和记忆策展角色；
- `FileGitBackend`、`MemoryService` 与 `FileGitBackend.query(...)` 基线召回（对应架构文档中的概念 `KnowledgeRepository/FileRecallProvider`）；
- `candidate / approved / rejected / obsolete` 持久状态迁移，以及策展 Skill 中的验证、精确 Git 提交和可选索引流程；
- `doctor`、默认只预览的安装/卸载，以及“不修改全局 Codex 配置”门禁；
- 无 Mem0 的完整端到端闭环。

退出条件：Windows/Linux 无 Mem0 场景通过；开发者不能给自己最终 PASS；卸载保留私人知识。

## R2：v0.1 可选 Mem0 OSS

交付内容：

- 实现 `RecallProvider` 协议的 `Mem0Provider` 适配器（对应概念 `Mem0RecallProvider`）；
- 隔离依赖和私有数据目录；
- `status`、`setup --dry-run/--apply`、`doctor`、`disable`、`uninstall` 引导；
- 只索引已批准条目；
- 引用 Revision/Commit/Hash 验证和权威原文回读；
- 未安装、禁用、超时、配置错误、索引过期的降级测试。

退出条件：删除整个 Mem0 索引后仍可从 File/Git 恢复；故障不阻塞核心工作流。

## R3：v0.1.0 发布

交付内容：

- 从 `coconilu/codex-opc-team` 固定标签安装；
- Windows 和 Linux CI 全绿；
- 本地原型迁移指南和实际回滚演练；
- Changelog、Release Notes、已知限制和验证证据；
- 一个代表性项目完成经理—总管—开发—独立 QA—经理体验。

退出条件：G1–G8 全部通过，`v0.1.0` 从近似干净环境安装成功。

## R4：反馈与成长增强

已交付组件：

- 经验候选的版本化重放、只读 Shadow Evaluation 和证据派生置信度（已实现 v1；不自动晋升）；
- 跨项目冲突检测、适用范围推断和失效提示；
- 分层 File recall、严格 ContextPacket/RecallTrace 与预算评测（v1 已实现；公开 synthetic superior 只代表该 fixture，不改变 authority）；
- 经理反馈与真实产品指标的结构化关联，以及角色/步骤知识使用链路（v1 已实现；仅 association/evidence，不证明因果）；
- 角色、Skill 与组织策略的版本化候选、同合同 paired pilot、经理/独立 QA 门禁、单路径 Git 晋升、观察与回滚（v1 已实现；不自动 stage/commit，不改全局 Codex 配置）；

v0.2 release-level contract、公开 synthetic runner 和严格私有 aggregate/envelope Gate 已实现，当前状态明确为 `BLOCKED`。退出 R4 还必须完成[代表性私有 3–5 task pilot 与 exact release commit 验证](release-readiness-v0.2.0.md)；公开 fixture、模板、Developer 自测或单项 token/latency 改善都不能替代。知识导出、合并和审计 UI 仍是后续候选，不属于 v0.2 发布阻断项。

这些能力仍遵守“不得自动晋升”的基础决策。任何自动化批准提议都需要新的 ADR 和安全评估。

## R5：生态与企业能力

候选方向包括可分享的脱敏角色包、团队策略模板、更多可替换召回后端、企业私有仓库协作和向官方插件生态提交。Mem0 Cloud、自托管 Server 和远程组织知识服务只有在明确的数据处理、权限和运维契约后才考虑。

## 明确不追求的路线

- 重新实现一个与 Codex 竞争的 Harness；
- 以更多 Agent、更多会议或更长对话作为产品价值；
- 让向量数据库或模型摘要成为不可审计的事实源；
- 自动修改经理目标、自动批准组织规则；
- 在没有外部权限边界的情况下自动发布、付费或联系第三方；
- 用遥测或原始日志换取所谓“成长”。
