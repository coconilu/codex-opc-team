# 结构化反馈与任务结果

结构化反馈把经理主观判断、已确认结果、独立 QA 证据、待验证假设和未知信息分开记录。它服务于后续评测，不是情绪分析，也不会自动产生组织真相。

| 产物 | 生命周期 | 是否可公开 |
|---|---|---|
| v1 contract/schema 与 synthetic tests | 插件版本控制 | 是 |
| `.opc/feedback/<run_id>.json` | 项目私有运行边界 | 否 |
| Metric aggregate reference | 指向既有私有安全聚合 | 仅引用可携带，逐任务原值不可公开 |
| Retrospective candidate | 独立待审核生命周期 | 否，除非经过单独治理与发布 |

## 写入与更新模型

机器 JSON 是唯一事实源，Markdown 报告只由它确定性生成。事件写入要求当前 `revision`；落后 revision、并发锁、同 ID 不同内容、越界路径、额外字段、非有限 JSON、敏感标记或引用不一致都会拒绝。完全相同的事件重试是幂等 no-op。

晚到 product outcome 或 metric aggregate 追加为新事件，不改写旧事件。`pass`、`fail`、`partial` 和 `unknown` 都是一等状态；系统不会把未知强迫解释成成功或失败。

若结果预计晚到，应在该 run 仍为当前 run 时先记录 `unverified` 事件。新 run 开始后，可用显式 `--run-id` 继续追加这个已建立的 sidecar；没有 sidecar 的任意历史 ID 因无法再验证项目关系而拒绝。

## 隐私与治理边界

真实反馈只保存在获批的私有项目 `.opc` 目录，不进入公开仓库、canonical knowledge、Mem0 或索引。自由文本限制为单行 500 字符，引用数量有上限，并拒绝 raw chat、Hook payload、凭证、URL、主机路径、UUID 和会话类 ID。

记录动作不执行候选批准、Git commit、索引、发布、付款或外部通信。经理交接会请求可选反馈；复盘可把反馈当作一类证据，但仍需独立形成候选、验证并由经理批准。

完整命令和事件字段见插件内 [`feedback-contract.md`](../plugins/codex-opc-team/skills/opc-manager/references/feedback-contract.md)。
