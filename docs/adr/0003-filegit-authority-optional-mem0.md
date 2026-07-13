# ADR-0003：File/Git 权威源与可选 Mem0

- **Status:** Accepted
- **Date:** 2026-07-13

## Context

文件记忆透明、可版本控制，但随着知识增长，仅靠路径和关键词检索会降低召回质量。Mem0 等工具能提供语义召回，却可能增加 Python、模型、向量存储或凭据依赖。若把 Mem0 变成必需组件，未安装用户的体验会残缺；若把它当权威源，则索引错误、供应商变化或卸载可能导致知识不可审计或丢失。

## Decision

File/Git `KnowledgeRepository` 是唯一权威知识源，并提供零额外依赖的 `FileRecallProvider`。这两个名称表达概念责任；v0.1 由 `FileGitBackend` 及其 `query(...)` 实现。Mem0 通过 `RecallProvider` 协议作为用户明确启用的可选语义召回索引，v0.1 的实现类为 `Mem0Provider`。

Mem0 可接收已批准条目的摘要和正文用于语义索引，同时携带指向 File/Git 权威条目的路径、Commit 和内容哈希元数据。Mem0 的召回结果只是候选引用，不是可直接注入上下文的权威原文；每个结果必须验证条目状态、Revision/Commit 和内容哈希，并从 File/Git 回读原文。v0.1 启用默认 OpenAI-backed Mem0 Provider 时，这些文本还可能发送给 OpenAI 模型或嵌入服务，必须在用户确认前披露真实数据流。Mem0 未安装、禁用、超时、配置错误或索引过期时，系统自动降级到 File/Git，核心工作流保持完整。

Mem0 索引可以删除并从已批准知识重建。v0.1 只承诺可选 OSS Library Adapter，不承诺 Cloud 或自托管 Server 运维。

## Consequences

### Positive

- 默认安装简单且离线可用；
- 语义召回可增强但不会锁定知识；
- 故障和卸载不会丢失权威历史；
- 未来可以增加其他 RecallProvider。

### Negative

- 需要维护双层一致性和过期索引检测；
- Mem0 结果多一次验证/回读；
- FileRecall 的相关性可能低于语义索引，需要良好元数据。

## Rejected alternatives

- **Mem0 必装并作为主库：** 增加门槛和单点故障，破坏可移植性。
- **只用文件不提供接口：** 无法平滑增强召回，也会让行为层耦合目录结构。
- **直接相信向量结果：** 可能注入过期、失效或作用域不符的知识。
- **首版同时支持 Cloud/Server：** 数据、认证和运维范围过大。
