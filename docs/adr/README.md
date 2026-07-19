# Architecture Decision Records

ADR 用于保存影响长期架构、安全边界或用户数据的决策。每份 ADR 记录背景、选择、后果和被拒绝方案；已接受 ADR 如需改变，应新增替代 ADR，而不是抹去历史。

| ADR | 状态 | 决策 |
|---|---|---|
| [0001](0001-codex-remains-the-harness.md) | Accepted | Codex 保持 Harness 地位 |
| [0002](0002-public-code-private-portable-knowledge.md) | Accepted | 公开代码与私人可移植知识分离 |
| [0003](0003-filegit-authority-optional-mem0.md) | Accepted | File/Git 为权威源，Mem0 为可选召回索引 |
| [0004](0004-controlled-knowledge-promotion.md) | Accepted | 经验通过受控流程晋升 |
| [0005](0005-no-silent-global-config-mutation.md) | Accepted | 不静默修改 Codex 全局配置 |
| [0006](0006-independent-qa-before-manager-handoff.md) | Accepted | 独立 QA PASS 后才向经理交接 |
| [0007](0007-runtime-events-outside-canonical-knowledge.md) | Accepted | Hook 与运行事件永不进入权威知识库 |
| [0008](0008-public-evaluation-private-pilot-boundary.md) | Accepted | 公开合成评测与私有试点聚合证据严格分离 |
| [0009](0009-private-structured-feedback-sidecars.md) | Accepted | 结构化反馈使用项目私有不可变 sidecar |
| [0010](0010-read-only-shadow-evaluation.md) | Accepted | Shadow Evaluation 是只读证据层，不授予晋升权限 |
| [0011](0011-deterministic-knowledge-governance.md) | Accepted | 知识关系、适用性、冲突与迁移使用统一确定性契约 |
| [0012](0012-hierarchical-file-recall-context-packet.md) | Accepted | 分层 File 召回只导航 derived L0/L1，并在 L2 重验后构建 ContextPacket |
| [0013](0013-private-knowledge-use-lineage.md) | Accepted | 知识使用链路是私有派生关联证据，不是因果证明 |

新 ADR 使用递增四位编号，至少包含：Status、Date、Context、Decision、Consequences、Rejected Alternatives。
