# Codex OPC Team v0.1.1-rc.1 候选发布说明

[English](release-notes-v0.1.1-rc.1.en.md)

## 发布定位

`v0.1.1-rc.1` 是公开 release candidate，用于对运行数据隔离和真实安装态生命周期门禁做预发布复审。它不是稳定版；生产安装仍应固定 `v0.1.0`。候选 tag 只能在原评审者确认精确候选 commit 后创建，并且创建后不得移动。

## 主要变化

| 范围 | 候选版变化 |
|---|---|
| 运行数据隔离 | Hook/runtime events 不进入 canonical File/Git knowledge；legacy events 只做脱敏、preview-first 的受控归档 |
| Skill 发现 | 使用新 Codex 进程解析 model-visible catalog，并精确比较六个 canonical OPC Skill names |
| 宿主隔离 | 子进程采用 deny-by-default 环境；隔离 Git config、template、hooks、签名与 credential helper；clean-room 外非系统 Skill 会阻断 |
| 生命周期 | 验证候选安装、重复安装、卸载、重装、回滚、版本幂等和知识/配置/可选记忆保留 |
| 发布 Ref | tag 或完整 OID 必须先解析到精确 commit OID；moving ref、相同 OID 或版本漂移均失败 |
| CI | Pull Request 和 `main` 在 disposable Windows/Linux Runner 中运行真实 Codex 安装态生命周期 |

## 安装候选版

仅建议评审者和发布测试者安装：

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.1-rc.1
codex plugin add codex-opc-team@opc
```

安装后必须新建 Codex 任务，确认 `$opc-manager`、`$opc-project-bootstrap`、`$opc-qa-gate`、`$opc-retrospective`、`$opc-memory-curator` 和 `$opc-memory` 均可见。不得用安装前已打开的任务作为发现证据。

## 数据与迁移

本候选版不提高 canonical knowledge schema，已有 File/Git 知识无需迁移。插件安装、卸载和回滚都不得删除私人知识、Git 历史或可选 Mem0 数据。

若旧版本曾把 runtime events 放进 knowledge tree，`opc-memory` 只报告已知路径元数据，不读取原始内容；归档必须先 dry-run，再对未变化的计划显式执行。自动删除、提交或上传仍被禁止。

## 回滚到稳定版

```powershell
codex plugin remove codex-opc-team@opc
codex plugin marketplace remove opc
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

回滚后关闭旧任务并新建任务，重新验证六个 Skills 和 File/Git knowledge 状态。稳定版 `v0.1.0` 不包含本候选版新增的安装态生命周期工具，但私人知识格式保持兼容。

## Gate 状态

| Gate | 创建候选 tag 前要求 |
|---|---|
| 仓库验证、完整单测、Git-history 隐私扫描 | 精确候选 commit 必须 PASS |
| 官方 Plugin Validator、六个 Skill quick validators | 精确候选 commit 必须 PASS |
| PR 常规 CI | Windows/Linux 全部 PASS |
| PR 真实安装态 CI | disposable Windows/Linux 全部 PASS |
| 原评审者预发布复审 | 必须针对精确候选 commit PASS |
| `v0.1.1-rc.1 → v0.1.0` fixed-ref Gate | 候选 tag 创建后运行；当前为 PENDING |

fixed-ref Gate 的 `release_gate.eligible` 只有在 exact OID、不同版本、幂等、数据保留、精确发现和卸载等全部断言为 `true` 时才成立。PR 分支、`main` 或工作区结果不能替代 tag Gate。

## 已知限制

- 这是预发布候选，不承诺稳定通道兼容性。
- 自动 `debug prompt-input` Gate 不调用模型，也不替代安装、回滚后人工新任务抽查。
- Mem0 仍为可选后端，只支持已固定测试的 `mem0ai==2.0.11`；默认 Provider 可能需要 OpenAI 凭据并发送已批准内容。
- Windows 普通开发机可能暴露真实 Personal Skills；可信 PASS 必须来自 disposable OS/container。
