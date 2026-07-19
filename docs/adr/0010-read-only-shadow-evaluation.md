# ADR-0010：Shadow Evaluation 是只读证据层

- Status: Accepted
- Date: 2026-07-19

## Context

候选经验即使看起来合理，也可能只在一次运行中偶然有效、已经过期、跨项目越界或扩大为错误规则。直接用模型自评或一次成功推动晋升，会把推断误当独立证据并绕过 ADR-0004 的经理治理；把回放产物写入 canonical knowledge、Provider 索引或项目源码，又会混淆派生证据与权威事实。

## Decision

Shadow Evaluation 是版本化、只读、可审计的 control/treatment 证据层。两组 arm 使用 ADR-0008/#4 的 exact metric contract：产品质量、安全门禁、上下文成本和延迟从同一原始字段聚合；shadow contract 记录该 contract 的版本与 SHA-256，仓库验证拒绝指标漂移。非确定性依赖必须记录 engine、version、determinism 和 seed 边界。

评测前从 File/Git 读取候选，并要求 `status=candidate`、project scope 匹配、相对 canonical 路径和内容哈希都由知识库当前 Git HEAD 的 exact blob 验证。跨项目、stale、obsolete 或其他非候选状态在读取 arm 测量前拒绝。超时、Provider 不可用/错误、零分母或质量证据冲突只能产生 `inconclusive/degraded`，不能产生正向建议。

ADR-0009/#5 的结构化反馈可以作为输入，但必须保留 confirmed outcome、独立 QA、经理判断、模型假设和未验证信息的类别。报告分别保存 supporting、counterevidence、neutral/unknown、scope rejection 和 failure mode；`beta-v1` 置信度只由版本化证据权重派生，且永远不授予批准权限。模型推断权重为零，也不构成独立 QA。

默认 `preview` 零写入；`evaluate` 只有在 exact preview fingerprint 被确认后，才可向用户明确选择的私有派生数据根创建不可覆盖的 JSON/Markdown。该根不得与公开插件、canonical knowledge 或项目源码重叠。读取和写入有大小上限、敏感值拒绝、symlink/reparse/hard-link 与父目录 identity 门禁，错误不回显匹配内容。

v1 对每个 ratio component、安全计数、context token 和 latency 都规定显式数值上限，并分别规定最多 20 个 case 的聚合上限。Schema、runtime 与仓库 validator 共享这些常量；整数在检查上限前不得转换为浮点数，聚合加法与中位数也必须 fail closed。越界、非有限值或算术异常统一成为脱敏 `OPC_SHADOW_ERROR`，不能产生 traceback 或部分产物。

机器结果不是可任意编辑后重渲染的展示缓存。Result Schema 的每个嵌套对象都必须 strict；runtime 在序列化与 `report` 渲染前重新核对当前 Shadow contract hash、#4 baseline hash、preflight/status/measurement/evidence/confidence/failure/governance 跨字段不变量。正向建议必须由通过的 preflight、零 failure、质量或安全指标的 measured support 与全部为 `false` 的写权限共同证明。

所有用户提供的 replay、result、knowledge/project root 与 artifact root 都在 `resolve` 前逐级检查现存祖先，拒绝 symlink、junction 或其他 reparse point；8.3 等指向同一普通目录对象的 Windows 路径别名不因此被拒绝。replay、result、canonical candidate 与最终 artifact 必须各自只有一个 filesystem link。读取绑定输入父目录对象并复核文件 identity；artifact root 必须由用户预先创建，assert 与 publish 绑定同一目录 identity，整个发布和回滚都在同一目录 handle 内完成。

Shadow Evaluation 不提供候选状态变更、canonical 写入、Git 写入、Provider 索引或自动晋升能力。即使建议为 beneficial，经理/curator 仍必须独立 preview 和批准 canonical transition，提交 exact blob，再对可选 reindex 单独 preview 和批准。

## Consequences

- 有益、中性、有害、冲突和降级结果可比较且可追溯，不把 persuasive prose 当事实。
- 私有 pilot 可以复用结构化反馈，但真实输入和逐项报告不会进入公开仓库或组织知识。
- exact HEAD 门禁意味着未提交候选不能参与正式 shadow replay；需要先以候选状态提交到私人知识 Git。
- 预先创建 artifact root 增加一个显式准备步骤，但消除了检查后跟随新建路径或目录替换的歧义。
- 小样本 Shadow Evaluation 只能提供策展证据，不能证明统计普适性或替代经理判断。

## Rejected Alternatives

- **模型读取候选后自由打分：** 不稳定、难比较，并会把自评伪装成 QA。
- **评测完成自动批准或拒绝：** 绕过 ADR-0004 的责任与 exact Git 边界。
- **把报告写回候选记录：** 会修改 canonical knowledge，并使证据生成和状态迁移无法独立审计。
- **Provider 失败时回退为正向启发式：** 把缺失证据误报为收益。
