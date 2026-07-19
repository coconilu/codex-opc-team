# ADR-0014：能力进化使用版本化证据门禁与单路径 Git 交接

- Status: Accepted
- Date: 2026-07-19

## Context

OPC 已能形成候选经验、结构化反馈、Shadow Evaluation、治理结果和知识使用链路，但这些证据不会自动把角色、Skill 或组织策略变成新版本。直接修改当前文件会混淆候选与生效版本；仅凭模型自评或一次成功运行晋升，又会绕过经理与独立 QA。另一方面，角色和 Skill 通常是 Git 文件，若自动化笼统 stage、commit 或修改全局 `config.toml`，可能把用户无关改动或权限扩张一起发布。

## Decision

发布 `opc-capability-evolution-contract-v1`、严格 proposal/record Schema 和 `opc_evolution.py`。每个 change proposal 必须绑定 source candidate、feedback/evaluation/lineage 引用、受影响 capability、current/candidate/rollback 的版本、路径、exact commit 和内容 SHA-256，以及 scope、owner 和 bounded pilot。

生命周期为：

```text
candidate
  -> replay/Shadow + independent QA + manager pilot approval
pilot_approved -> piloting -> evaluated
  -> independent QA + manager promotion approval
promotion_pending -> explicit Git commit -> promoted -> observing
  -> regression or manager decision
rollback_pending -> explicit Git commit -> rolled_back
```

候选必须是 current commit 的后代，且 commit range 只能改变一个与 capability kind 匹配的 allowlisted repository path。Pilot 的每个 control/candidate arm 使用相同的 `opc-evaluation-contract-v1`，记录 exact capability version、knowledge revisions 和 private lineage ref；质量、安全、经理介入、上下文成本、延迟、confounder 与 unavailable/timeout 结果均显式保留。任何 scope leakage、privacy failure、缺证据、失败或 Provider 不可用都阻止 positive promotion；neutral 也不视为有益。

`.opc/evolution/` 是私有派生 evidence sidecar。Git 项目必须目录级 ignore 且无 tracked 内容；非 Git 项目是用户明确选择的私有边界。preview 零写入，apply 使用 exact base-record CAS、revision、plan token、project/`.opc`/evolution 目录对象绑定和单链接文件。Preview 与 apply 之间发生 HEAD、worktree、路径身份、parent、内容或并发变化时 fail closed。v0.1 未版本化文件继续可用为 `unversioned-v0.1`；migration 仅生成确定性零写入 proposal preview，不静默注册。

Promotion/rollback 只把一个已验证 Git blob 原子写成 **unstaged** working-tree diff。它不 stage、commit、push 或 merge；只有调用者显式提交 exact path 后，`confirm` 才将该 commit 视为 active。写入 private record 失败时用同一事务绑定的旧物理字节恢复 target；若恢复本身失败，错误必须显式暴露，private record 不前进，也不得声称成功。Rollback 保留 proposal、pilot、evaluation、history 和 approved knowledge。

所有报告固定声明 `association/evidence only`。对照结果支持决策证据，但不证明单一 capability 导致结果，也不证明可以泛化到其他项目。

### Reviewer hardening clarification

Evidence is not an untyped file-existence check. Every private reference uses a strict evidence envelope that binds proposal and capability versions, optional exact run, the complete pilot/lineage set, a typed decision, safety verdict, and bounded time. Evaluation, promotion, and confirmation cumulatively revalidate source, pilot authorization, lineage, evaluation, independent-QA, and Shadow evidence. Manager denial, QA failure, harmful/unsafe/inconclusive Shadow evidence, stale bindings, or missing/replaced evidence fails closed.

The immutable governance identity is `proposal_core_sha256`, computed from every proposal field except circular evidence references. The ordered full pilot cases plus confounders form `pilot_snapshot_sha256`; the evaluation is deterministically recomputed from that snapshot and separately digested. Evaluation and promotion evidence bind all three digests. Record reads and report/promotion/confirmation consumers recompute them, so scope, owner, allowed-project, pilot-limit, rollback, case, measurement, status, lineage, order, or confounder changes cannot reuse an older approval.

Only regular Git blobs with mode `100644` or `100755` are eligible. Governed candidate and confirmation ranges are strict, linear descendants; merge commits and every per-commit add/delete/rename/copy/type change, non-target path, empty commit, or reset to pre-existing source history are rejected. Rollback therefore creates and confirms a new rollback commit rather than moving HEAD to an ancestor. Non-completed pilot arms contain no measurements and are excluded from aggregation.

Every intermediate target blob is privacy-scanned directly from Git object storage without checkout. Private evidence is read through a bound parent and one no-follow descriptor with single-link and before/after identity checks; hardlinks, symlinks, Windows reparse points, same-size replacement, and parent rename races fail closed.

## Consequences

- current、candidate 和 rollback 都有 exact File/Git provenance；未提交工作树不是 active version。
- 能力改变必须经过独立 QA、经理批准、同合同对照和 bounded pilot，不能自主自修改或自评晋升。
- 自动化只触及一个 allowlisted 文件，且不会把用户无关改动带入 Git。
- Windows checkout filter 可改变物理换行；clean Git 状态证明与 HEAD 等价，同时事务仍单独绑定实际物理字节以便精确恢复。
- 跨 private sidecar 与 Git worktree 的提交不能成为单个 filesystem transaction；恢复失败因此是显式人工处置状态，而不是伪造成功。

## Rejected Alternatives

- **Shadow beneficial 后自动替换 active：** Shadow 是只读证据，不能授予批准权。
- **由 runtime 自动 commit/push：** 会扩大权限并可能纳入用户无关改动。
- **用目录或通配符 stage：** 无法证明 Git diff 只包含一个 capability。
- **把 pilot 原始内容提交公开仓库：** 违反公开代码与私有运行证据分离。
- **Rollback 删除历史或 approved knowledge：** 会抹去审计证据并混淆能力版本与知识治理。
- **把未版本化 v0.1 视为不可读：** 会造成破坏性迁移；显式 compatibility label 足以开始受控版本化。
