# 受控能力进化

`opc_evolution.py` 把候选经验转成可审计的角色、Skill 或组织策略版本，但不自动修改全局 Codex 配置，也不自动 stage、commit、push 或 merge。机器事实由 `plugins/codex-opc-team/assets/evolution/` 下的 contract 和 strict Schema 定义；项目私有状态位于 `.opc/evolution/<proposal_id>.json`。

## 不变量

| 边界 | v1 行为 |
|---|---|
| Authority | capability 是 exact File/Git blob；private sidecar 只是派生 evidence |
| Candidate | candidate commit 必须从 current commit 可达，且 range 只改一个 allowlisted target |
| Pilot | 1–20 个 paired cases；每个 arm 记录 exact capability、knowledge versions 和 lineage ref |
| Evaluation | control/candidate 使用同一 `opc-evaluation-contract-v1`；报告质量、安全、经理介入、context cost、latency、confounders |
| Approval | pilot 与 promotion 分别需要 explicit manager、independent QA 和 Shadow evidence |
| Failure | regression、scope/privacy failure、missing evidence、timeout、Provider unavailable/error 均阻止晋升或产生 inconclusive |
| Git | apply 只产生一个 unstaged target diff；显式 commit 后才能 confirm active |
| Privacy | `.opc/evolution/` 整目录 private 或 Git-ignored/untracked；禁止 raw chat、Hook/tool payload、credential、embedding、runtime ID 和 home path |
| Claims | 报告只说明 `association/evidence only`，不证明因果或跨项目泛化 |

Allowlist 仅包含：仓库内 `roles/`、`agents/`、plugin `assets/agent-configs/`；Skill 的 `SKILL.md` 或一层 `references/*.md`；`AGENTS.md`、`policies/*.md` 或 `docs/policies/*.md`。用户 home 的 `config.toml`、全局 roles、feature flags 和 hooks 不在范围内。

## Proposal

Proposal 必须符合 `capability-change-proposal.v1.schema.json`：

- `sources`：至少一个 candidate ID、evaluation ref 和 lineage ref；所有文件引用带 relative `.opc` path 与 SHA-256；
- `capability`：kind 与单个 allowlisted target；
- `current_version / candidate_version / rollback_target`：version、同一 source path、exact commit 与 Git blob SHA-256；
- `scope / owner / pilot`：project/organization 边界、责任人、min/max/observation case 数。

`rollback_target` 在 v1 必须等于开始时的 current version。Proposal 和 private record 均拒绝额外字段、非有限数、绝对路径、UUID runtime token 与 session/turn/thread ID。

## 操作顺序

以下命令都先运行 `*-preview`；apply 必须携带 preview 返回的 exact `plan_token`。JSON payload 只保存 portable ID/hash/ref，不保存正文。

```text
python <plugin-root>/scripts/opc_evolution.py open-preview \
  --project-root <private-project> --repository-root <capability-git-root> \
  --proposal <private-proposal.json>

python <plugin-root>/scripts/opc_evolution.py open ... --plan-token <sha256>

python <plugin-root>/scripts/opc_evolution.py action-preview \
  --project-root <private-project> --proposal-id cap-example \
  --expected-revision 1 --action authorize_pilot \
  --payload <private-authorization.json> --now <UTC>

python <plugin-root>/scripts/opc_evolution.py action ... --plan-token <sha256>
```

`action` 依次支持：`authorize_pilot`、`record_pilot_case`、`evaluate`、`authorize_promotion`、`observe`、`reject`。每个 pilot case 的 control/candidate 均携带同一个 evaluation contract version/hash；execution status 可显式为 `completed / timeout / provider_unavailable / provider_error / failed`。

晋升或回滚：

```text
python <plugin-root>/scripts/opc_evolution.py transition-preview \
  --project-root <private-project> --repository-root <capability-git-root> \
  --proposal-id cap-example --expected-revision <n> --kind promotion \
  --authorization <private-promotion-authorization.json> --now <UTC>

python <plugin-root>/scripts/opc_evolution.py transition ... --plan-token <sha256>
```

成功的 `transition` 返回唯一 `git_stage_pathspecs`，但 `staged=false`、`committed=false`。调用者必须先检查 `git status --short -- <exact-path>` 与 `git diff -- <exact-path>`，只显式提交该 path；若有任何其他 worktree change，runtime 会在 transition 前阻止操作。Commit 后运行 `confirm-preview` / `confirm`，确认从 base 到新 HEAD 的整个 commit range 仍只改 target 且 blob hash 完全一致。

Rollback 使用 `--kind rollback --rollback-evidence <private-ref.json>`，然后同样由用户显式提交与 confirm。History、pilot、evaluation 和 approved knowledge 不删除。

## Strict evidence and Git confirmation hardening

- Every source candidate, feedback, evaluation, lineage, manager, QA, Shadow, observation, and rollback reference resolves to `capability-evolution-evidence.v1.schema.json`. The envelope binds the proposal, target, current/candidate versions, optional exact run, the complete pilot/lineage set, decision, safety verdict, and timestamp.
- `evaluate`, promotion, and `confirm` re-read every cumulative source, pilot authorization, pilot lineage, evaluation, QA, and Shadow envelope. Deleted, replaced, stale, denied, failed, harmful, unsafe, inconclusive, or mismatched evidence fails closed.
- A non-completed arm has `measurements=null` plus an exact `unavailable_reason`. It never contributes to metric aggregation; reports say `not measured (<reason>)` instead of fabricating zero metrics.
- Git objects must be regular `blob` objects in mode `100644` or `100755`. Candidate and confirmation ranges must be strict linear descendants: merge commits, symlinks, gitlinks, trees, type changes, renames/copies, empty commits, source-history resets, and any intermediate non-target path are rejected.
- Record history uses the contract's exact action/from/to mapping, contiguous revisions, non-decreasing bounded timestamps, single-use action rules, and action-specific evidence kinds. Reports reject invalid history before rendering.

## v0.1 compatibility

没有 `.opc/evolution/` record 的旧项目保持可读，`show` 返回 `unversioned-v0.1 / unavailable`；旧角色和 Skill 照常使用。`migration-preview` 从当前 HEAD 与 candidate commit 生成确定性 proposal，重复调用得到相同 token，且 `writes=false`：

```text
python <plugin-root>/scripts/opc_evolution.py migration-preview \
  --repository-root <capability-git-root> --kind skill \
  --target-path skills/example/SKILL.md --project-id project-example \
  --owner manager --proposal-id cap-example --candidate-commit <sha> \
  --candidate-version v1.0.0-candidate.1 --created-at <UTC>
```

Migration preview 的 synthetic placeholder evidence hash 必须替换为实际 private evaluation/lineage 文件 hash，proposal 才能 open；runtime 不伪造证据，也不自动注册全局 capability。

## 失败与恢复

Preview 是零写入。Apply 会复验 exact private base hash/revision、Git HEAD、clean worktree、project/`.opc`/evolution 与 target parent identity。正常异常、`KeyboardInterrupt` 和 `SystemExit` 都在 bound parent 内用原物理字节恢复。若恢复本身失败，命令显式失败，private record 不前进，用户只需检查报告的 exact target；runtime 不删除未知或相似文件，也不宣称晋升/回滚完成。

`report` 只从 strict record 生成，并固定声明 `association/evidence only`。有益 synthetic/pilot evidence 支持当前受控决策，但不是能力效果的因果证明或普适性证明。
