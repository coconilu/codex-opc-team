# v0.2.0 发布就绪度：Evaluated Feedback Loop

## 当前结论

**公开 synthetic evidence 已通过；`v0.2.0` 发布仍为 BLOCKED。** 当前仓库没有、也不应包含真实私有 3–5 task pilot。缺少该 pilot 和最终 release commit 的跨平台/验证/独立 QA envelope 时，不能把 `main`、本分支或模板描述成完整 v0.2 发布。

这不是“功能尚未实现”的同义词。#1–#9 的组件已经进入 Git；剩余阻断是 Issue #10 明确要求的代表性私有验证与 exact-release-commit 发布证据。

## 依赖交付

| Issue | 已进入 `main` 的交付 | Merge commit | Release gate 解释 |
|---:|---|---|---|
| #2 | runtime data isolation | `3a010ae` | v0.1.1 prerequisite |
| #3 | installed lifecycle acceptance | `0e4592b` | v0.1.1 prerequisite |
| #4 | versioned File/Git baseline/private aggregate contract | `a3250ae` | public/private metric contract |
| #5 | structured manager feedback/outcome sidecar | `8a1f28a` | pilot feedback evidence |
| #6 | read-only replay and Shadow Evaluation | `caf26fd` | control/treatment evidence |
| #7 | deterministic conflict/invalidation/applicability | `b7e1dcb` | stale/conflict safety gate |
| #1 | budgeted cited Context Packet | `383d34b` | context quality/cost evidence |
| #8 | private role/step knowledge-use lineage | `ff25012` | association, not causality |
| #9 | versioned capability pilot/promotion/observation/rollback | `adb0515` | controlled evolution gate |

Runner 还会对每个 issue 的关键公开 contract/doc 计算 SHA-256。表中 commit 是依赖到达当前基线的 Git 证据，不替代其独立 PR review 或测试记录。

## 发布级证据表

| 维度 | 当前证据 | 状态 | 允许的结论 |
|---|---|---|---|
| 功能闭环 | 8 个 targeted synthetic scenarios 实际运行 | PASS | 合同在这些 fixture 中可互操作 |
| 安全 | public baseline 与 hierarchical fixture 的 scope/stale acceptance 均为 0 | PASS（synthetic） | 只适用于公开 fixture |
| 隐私 | public evidence 无 private body；private runner 只输出 aggregate/verdict | PASS（设计与测试） | 不证明尚未运行的真实 pilot |
| 兼容/降级 | Provider failure/timeout/disagreement、index delete/rebuild targeted tests | PASS（synthetic） | File/Git fallback contract 可执行 |
| Context quality/cost | precision@5 `0.20 → 1.00`、canonical recall@5 `1.00 → 1.00`、median tokens `661 → 107` | MEASURED（synthetic） | 不能外推真实项目 |
| Latency | versioned hierarchical latency artifact | MEASURED（环境相关） | 不能声称跨机器提升 |
| 私有代表性 pilot | 无真实 3–5 task aggregate/envelopes | **BLOCKED** | 无真实质量或干预改善主张 |
| Rollback | synthetic evolution lifecycle test；private exact restore 尚缺 | PARTIAL | 不能替代 private drill |
| Windows/Linux | PR CI 可验证 public gate；最终 release commit gates 尚缺 | **BLOCKED** | 分支 CI 不能替代 tag/commit gate |
| 独立 release QA | 最终 release commit 尚无 typed envelope | **BLOCKED** | Developer 自报不算 QA |

已提交的机器证据是 `evaluation/baselines/v0.2-public-synthetic-evidence.v1.json`，Markdown 由同一结果确定性生成。机器结果明确写出 `release_status=blocked`，避免把 public PASS 误读成 release PASS。

## 公开证据重放

```text
python scripts/v0_2_release_evidence.py verify-public
```

该命令真实执行：

1. File/Git baseline byte-for-byte verify；
2. hierarchical comparison byte-for-byte verify；
3. Context Packet、structured feedback、Shadow、conflict governance、lineage、capability evolution、Provider degradation 与 delete/rebuild 的命名 synthetic tests；
4. 已提交 public JSON/Markdown 的精确重建比较。

`verify-public` 成功返回码只表示 public evidence PASS；stdout 仍显示 release BLOCKED。

## 私有 3–5 task pilot

### 1. 在仓库外准备

把 `evaluation/private-pilot-v0.2.template.json` 复制到批准的 private OPC project，例如 `.opc/release-evidence/pilot.json`。删除 `$instructions`，替换所有 `null`，并把 `schema_version` 改为 `opc-v0.2-private-pilot-aggregate-v1`、`evidence_class` 改为 `representative-private-pilot`。不要在本仓库修改模板来伪造一次 pilot。

任务必须在执行前固定，包含至少 2 种风险和 2 种工作类型；control/treatment 使用 exact `opc-evaluation-contract-v1`。两组都报告：

| 类别 | 必填内容 |
|---|---|
| Quality | manager intervention、QA catch、rework、valid reuse、false recall 的分子/分母 |
| Safety | scope leakage、stale/obsolete acceptance、privacy failure，全部必须为 0 |
| Cost | context token 的 total/median/nearest-rank p95 |
| Performance | latency ms 的 total/median/nearest-rank p95 |
| Coverage | 每 task 的 Packet/feedback/lineage/Shadow/evolution，至少 1 个 conflict 与 rollback drill |
| Fallback | Provider disabled、derived index delete/rebuild、canonical digest unchanged |

Runner 会证明 3–5 个正数样本是否可能产生给出的 total/median/p95，不重建或导出逐任务数组；任何不可能 aggregate 都 fail closed。Treatment 必须至少一项 quality 指标严格改善，其他 quality 指标不得回退；token/latency 仍作为诊断并列报告，不单独决定发布。

### 2. 绑定 typed evidence

四个 attestation ref 必须指向同一 private root 下 `.opc/` 的单链接普通 JSON 文件，并遵循：

- `evaluation/schemas/v0.2-private-evidence-envelope.v1.schema.json`；
- exact `pilot_id`、task count；
- `pilot_core_sha256`：对 pilot JSON 去掉整个 `attestations` 字段后，以 UTF-8、2 空格缩进、末尾 LF 计算 SHA-256；
- manager=`approved/not_applicable`、independent QA=`pass/safe`、Shadow 与 evolution=`beneficial/safe`；
- 只有 independent QA 的 `independent_from_implementer=true`。
- `source_ref/source_sha256` 指向产生该判断的实际 private feedback/QA/Shadow/evolution artifact；runner 同样执行 bounded、single-link、no-follow Hash 校验，不能只提交一个自称 PASS 的 envelope。

Evidence envelope 只证明声明绑定到哪个 aggregate；它不在技术上认证填写人的真实身份。因此经理批准和 Reviewer 独立性仍必须由真实流程保证。

### 3. 验证 private pilot

```text
python scripts/v0_2_release_evidence.py private-pilot \
  --private-root <approved-private-project> \
  --summary <approved-private-project>/.opc/release-evidence/pilot.json
```

POSIX 只支持 stdout，不支持 `--output`。如需落盘，由调用者在已批准的私有边界内安全捕获 stdout；禁止捕获到本公开仓库、公开 CI Artifact 或其他未批准位置。Windows 可在上述命令后追加 `--output <approved-private-project>/.opc/release-evidence/pilot-verdict.json`，runner 会执行 no-overwrite 的边界内写入。

summary、attestation 和 verdict 都留在私有项目；不要提交到本仓库或公开 CI Artifact。

## Exact release commit gate

最终候选 commit 产生且 worktree clean 后，把 `evaluation/release-gates-v0.2.template.json` 复制到 private root。这个 exact clean release commit 是唯一允许接受最终 Gate 的 checkout。每个 `evidence/...` ref 必须是符合 `v0.2-release-check-envelope.v1.schema.json` 的 typed envelope，绑定 exact `release_commit`、private summary SHA-256，以及实际 gate log 的 `source_ref/source_sha256`。

```text
python scripts/v0_2_release_evidence.py release \
  --private-root <approved-private-project> \
  --summary <approved-private-project>/.opc/release-evidence/pilot.json \
  --gates <approved-private-project>/evidence/release-gates.json
```

这里同样适用 stdout-only 边界：POSIX 调用者只能在已批准的私有边界内自行捕获；Windows 可追加 `--output <approved-private-project>/evidence/release-verdict.json`。公开仓库内不得捕获私有 verdict。

完整 gate 必须全部 PASS：Windows CI、Linux CI、repository validation、privacy current+history、官方 Plugin Validator、全部 Skill quick validators、独立 release QA、rollback evidence。Runner 会重跑 public evidence、验证 private pilot、确认当前 checkout 恰为 attested HEAD 且 clean，再输出 `release_status=ready`；它不会 stage、commit、tag、push 或发布。

## 测量、推断与非主张

| 类型 | 内容 |
|---|---|
| 已测量 | 上表公开 fixture 数字；未来 private verdict 中的 aggregate |
| 合理推断 | 命名 synthetic tests 同时通过，说明各 contract 能在这些受控输入下连接 |
| 已知限制 | 小样本、任务选择、oracle、学习顺序、模型/机器/缓存/网络都是 confounder |
| 尚未完成 | 当前真实 private pilot、exact release commit gate、v0.2 tag/发布 |
| 明确不主张 | 因果归因、统计普适性、自主自我改进、AGI、零人工治理 |

所有 lineage、Shadow、pilot 和 release 报告只允许写：`association/evidence only`。
