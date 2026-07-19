# ADR-0011：确定性的知识关系与适用性治理

- Status: Accepted
- Date: 2026-07-19

## Context

仅有 `candidate / approved / rejected / obsolete` 状态不足以回答“哪条知识此刻能进入执行上下文”。同一主题可能存在冲突、替代、失效、角色限制、项目限制和敏感级别；语义 Provider 的排序分数也不能证明 canonical 文件仍被当前 Git HEAD 发布。若各消费者自行解释这些条件，同一知识会在 manager、Shadow、recall 和 curator 中得到不同结果。

现有 Schema 1 条目仍需可读，公开插件也不能把真实组织知识、运行标识或机器绝对路径作为迁移样本提交到仓库。

## Decision

发布 `opc-knowledge-governance-v1` 作为所有知识消费者共享的治理契约，并由 `opc_governance.py` 实现严格运行时校验。Schema 2 新增：

| 字段 | 决策 |
|---|---|
| `sensitivity` | `public / internal / restricted`，调用方必须显式获准读取 |
| `applicability` | 明确角色、知识类型、约束和有效期；缺失上下文时 fail closed |
| `relations` | 版本化表示 `conflicts`、替代和失效关系，并携带显式 scope/project identity |

进入执行上下文前，固定按以下顺序做 hard filter：

1. scope 与显式 `project_id`；缺少项目上下文时只能读取 global，绝不从绝对路径推断项目；
2. 仅 `approved`；
3. 当前 Git HEAD 的 canonical commit 与内容 hash；
4. sensitivity 授权；
5. 显式 applicability；
6. invalidation 与 supersession。

Provider 只提供候选 ID，不提供 authority，也不能用 rank/score 改写 hard filter 或 canonical 排序。未解决冲突的双方正文都不进入上下文；诊断必须给出双方 canonical citation，但不得包含正文。缺失目标、非法关系和有向环只使相关节点失败，不拖垮无关知识。

Schema 1 保持只读兼容。升级到 Schema 2 必须先生成零写入 preview，绑定原始 SHA-256、已存在且位于 canonical knowledge 外部的备份目录，以及精确 plan token；apply 每次只迁移一条并保留原始备份。经理 curation 同样使用 exact preview token，只修改该 ID 的精确 old/new canonical 路径，不写 Provider，不自动 commit。

Shadow Evaluation 仍用于 candidate 的只读证据生成。它可以读取 Schema 1/2 candidate，但不负责解决关系、改变状态或授予执行上下文资格；最终 recall 必须重新通过本 ADR 的完整治理筛选。

补充的 fail-closed 实现约束如下：关系图只能在非关系硬过滤之后构建，环检测必须是有界迭代算法；迁移 inventory 必须验证跨状态 ID 唯一并执行每状态数量上限；curation token 必须绑定规范化 transition timestamp 与最终规范字节 hash，apply 写后验证同一字节；所有查询和生命周期时间必须显式带时区。无法通过 schema/runtime 校验的 approved 文件只产生不含正文的 `record_invalid` omission。

## Consequences

- 不同消费者使用同一套版本、字段、限制、reason code 和排序边界。
- 冲突与失效不再依赖语义相似度或自由文本约定，诊断可审计且不泄露正文。
- 旧知识无需原地批量升级，但任何写治理字段的操作必须先逐条迁移并备份。
- Provider 故障、陈旧索引或排名变化不会改变 File/Git 权威结论。
- schema、runtime、契约或文档漂移会由仓库 validator 和测试阻断。

## Rejected Alternatives

- **让 Provider 直接返回最终上下文：** 无法证明 current-HEAD provenance，也会把排序分数误当 authority。
- **冲突时选择分数更高的一方：** 分数不是经理决策，可能静默执行互斥规则。
- **缺少 project ID 时从工作目录推断：** 绝对路径和目录名既不可移植，也容易造成跨项目泄露。
- **启动时自动批量迁移 Schema 1：** 缺少逐条 preview、外部备份、明确批准和窄 Git 审计范围。
- **让 Shadow 的 positive 结果自动批准：** 违反 ADR-0004 与 ADR-0010 的权限边界。
