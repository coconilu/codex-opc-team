# ADR-0013：知识使用链路是私有派生证据，不是因果证明

- Status: Accepted
- Date: 2026-07-19

## Context

ContextPacket、QA、结构化反馈和 Shadow Evaluation 已分别可审计，但此前没有一条可移植记录说明：某个角色在某一步看到了哪个 Packet、哪些 canonical revision 被召回或注入，以及后来被采用、忽略、覆盖、矛盾或省略。只按时间把召回和结果并列，会把相关性误写成因果；把正文、prompt 或工具载荷写进 trace，又会扩大隐私与保留风险。

## Decision

发布 `opc-knowledge-lineage-contract-v1`、严格 Draft 2020-12 Schema 和 `opc_lineage.py`。每个项目 run 使用 `.opc/lineage/<run_id>.json` 私有 sidecar；若项目位于 Git worktree，目标 `.opc` 必须已被 ignore。非 Git 项目把项目根视为用户选择的私有运行边界。公开插件、canonical knowledge、Provider、项目源码和远程服务均不接收 trace。

事件分为 `knowledge`、`provider`、`association`。知识状态严格区分 `recalled / injected / adopted / ignored / overridden / contradicted / omitted`，并用确定性前驱状态、`previous_event_id` 和 materialized state replay 校验。召回或注入绝不自动推导采用；Provider 缺失、禁用、失败、过期或 no-memory 是独立降级事件。事件保存 project/run Schema 与原始文件 SHA-256、ContextPacket/RecallTrace 版本与规范 JSON SHA-256、canonical `source_path / HEAD commit / content hash / status / scope / project`，但不保存 Packet 正文。

写入必须先零写入 preview，再用 exact plan token、revision CAS、独占 lock、单链接文件和已审计的 `BoundDirectory` 事务原子发布。同 event ID 同内容为幂等 no-op；同 ID 不同内容、stale revision、并发冲突和父目录身份变化均 fail closed。错误不回显输入正文。v0.1 run 没有 sidecar 时返回 `lineage unavailable`，不迁移 `run.json`，也不伪造默认状态。

任何报告把 canonical revision 写成 usable 前，必须重新验证 current File/Git HEAD、内容 Hash、approved status、scope/project、角色适用性和关系治理。stale、cross-project、obsolete、conflict 与验证失败转为 omission/degraded；QA、feedback/outcome、Shadow 和 evaluation 引用也必须是 `.opc` 下已有、portable、bounded、single-link、Hash 匹配的文件。机器 JSON 是权威记录，Markdown 由重验后的 view 确定生成，并固定写明 `association/evidence only`、confounders 和 unknowns；不支持单次 run 的因果结论。

默认保留期为 30 天的私有派生运行证据，项目策略可以缩短。Redaction 删除整个派生 lineage artifact，不重写 canonical knowledge。记录和报告禁止 raw chat/prompt、CoT、Hook/tool payload、凭据、embedding、session/turn/thread ID、用户主目录和 private body。记录不授权批准、拒绝、改写、晋升、Git 写入、Provider 写入或外部通信。

## Consequences

- 多角色、多步骤可以精确识别同一个 Packet 实例和所提供的 canonical revision。
- 晚到 QA、反馈和 outcome 可追加关联，历史知识状态不被改写。
- 可选 Provider 故障不会阻塞 File/Git 核心，但会留下明确 degraded/no-memory 证据。
- HEAD 前进会使旧 commit citation 在当前报告中降级；这是有意的严格 freshness 语义，不等于知识曾被使用或未被使用。
- sidecar 是可删派生证据，不进入知识晋升链；若需要长期经验，仍须走候选、独立验证、经理批准和 exact Git 发布。

## Rejected Alternatives

- **在 `run.json` 增加 required lineage：** 破坏 v0.1 可读性，并把晚到事件变成 run Schema 迁移。
- **只记录最终 adopted：** 无法区分 recall、injection、未使用和矛盾，也鼓励事后补写因果故事。
- **从 recalled/injected 自动推断 adopted：** 模型收到内容不等于实际采用。
- **保存 prompt、对话或 CoT 证明使用：** 隐私代价高，且仍不能证明因果。
- **把 trace 写入 canonical knowledge、Mem0 或远程 telemetry：** 混淆运行证据与组织事实，并扩大泄漏面。
- **只在写入时校验 citation：** 无法发现报告时已经 stale、obsolete、cross-project 或 conflict 的 revision。
