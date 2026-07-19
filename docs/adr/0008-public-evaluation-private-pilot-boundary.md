# ADR-0008：公开合成评测与私有试点证据分离

- Status: Accepted
- Date: 2026-07-19

## Context

反馈、冲突治理和召回增强需要共享基线，但真实项目的源码、组织知识、对话和运行证据不能进入公开插件仓库。只发布手工摘要又无法证明 scope、状态和 Git provenance 由当前实现真实验证；发布逐任务数据则会扩大隐私和重识别风险。

## Decision

公开仓库只保存版本化 contract/schema、纯合成 File/Git fixture、确定性 runner 与聚合结果。Runner 在临时独立 Git 仓库中实际调用当前 `FileGitBackend.query(...)` 和 `read_authoritative(...)`，不建立第二套召回实现。

真实项目试点固定为 3–5 tasks。逐任务原始证据始终留在获批的私有项目边界，不进入公开仓库、canonical knowledge 或召回索引；公开或跨边界输出只允许严格 schema 的整体计数与分布，不允许项目名、自由文本、路径、逐任务数组和 artifact 引用。聚合出口拒绝非有限 JSON 数值，并且必须证明 total、median 与 nearest-rank p95 能由对应数量的正数样本实现，但不重建或保存逐任务值。scope leakage 与 stale/obsolete acceptance 是零容忍硬门禁；缺字段、零分母、不可实现聚合和无法验证不能解释为 PASS。

机器 JSON 是权威评测产物，Markdown 报告必须从该 JSON 确定性生成。指标 contract 的破坏性变化通过新版本发布，不覆盖历史基线。

## Consequences

- Windows/Linux 可以从相同合成输入复现完全一致的机器结果和报告。
- 私有试点可以比较产品结果、安全、token 与 latency，而无需发布项目内容。
- 公开合成结果不能代替真实项目效果，也不能证明统计普适性。
- 逐任务调试仍需在私有边界完成；严格聚合出口刻意牺牲公开可诊断细节。
- 本决策不授权 Shadow Evaluation、分层召回、自动晋升或 Provider 变更。

## Rejected Alternatives

- **把脱敏逐任务记录提交到公开仓库：** 小样本、时间和行为组合仍可能重识别项目或人员。
- **只维护人工 Markdown：** 无法证明计算、File/Git 路径和报告一致性。
- **在 runner 中重写召回算法：** 会测试替代实现，而不是当前 no-enhancement 基线。
- **把评测原始数据写入 canonical knowledge：** 混淆运行证据与组织知识生命周期，并扩大召回暴露面。
