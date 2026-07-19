# ADR-0009：结构化反馈使用项目私有不可变 sidecar

- Status: Accepted
- Date: 2026-07-19

## Context

经理判断、独立 QA、产品结果和经验假设具有不同证据强度，且结果可能在 run 完成后才到达。把这些内容塞进 `run.json` 会破坏旧 run 兼容性；写进 canonical knowledge 或公开仓库会混淆运行证据与组织真相，并扩大隐私面。

## Decision

采用版本化 contract/schema 和项目私有 `.opc/feedback/<run_id>.json` sidecar。记录由不可变事件组成，以 portable project/run/candidate/metric/artifact reference 关联，使用 revision compare-and-swap、独占锁和 atomic replace 防止丢失更新。同 event ID 的相同内容重试为幂等 no-op，不同内容或 stale/concurrent 更新 fail closed。

缺少 sidecar 表示“未记录”，旧 run 不迁移也不伪造默认值。机器 JSON 是唯一源，紧凑人类报告只能确定性派生。真实反馈不进入公开仓库、canonical knowledge、Mem0 或索引；只允许使用既有评测 contract 的安全聚合引用，不改写评测 baseline。

记录操作不授权候选批准、Git commit、索引、发布、付款或外部通信。反馈只作为后续评测/复盘输入，知识晋升仍走独立候选、验证、经理批准和 File/Git 发布门禁。

## Consequences

- 晚到 outcome/metric 可以追加并审计，不覆盖历史判断。
- PASS、FAIL、partial 和 unknown 均可表达，不强迫二元评价。
- 严格自由文本和引用门禁降低 raw payload、凭证、运行 ID 与主机路径泄漏风险。
- 每个项目自行负责私有 sidecar 的保留和备份；本插件不会上传或发布它。

## Rejected Alternatives

- **直接扩展 `run.json` required fields：** 会使已有 run 无法读取，并把反馈变成状态机前置条件。
- **写入 canonical knowledge/Mem0：** 反馈尚未经过候选治理，不能成为可召回组织规则。
- **只保留自由文本交接：** 无法稳定区分证据强度、幂等更新或安全聚合引用。
- **自动根据反馈晋升经验：** 把主观判断误当批准，绕过独立验证与 File/Git 门禁。
