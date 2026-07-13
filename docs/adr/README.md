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

新 ADR 使用递增四位编号，至少包含：Status、Date、Context、Decision、Consequences、Rejected Alternatives。
