# Codex OPC Team v0.1.0 发布说明

[English](release-notes-v0.1.0.en.md)

发布日期：2026-07-13

`v0.1.0` 是 Codex OPC Team 的首个稳定版本。它在 Codex Harness 内提供“经理对齐 → 角色委派 → 独立 QA → 经理交接 → 受控复盘”的完整闭环，并将 File/Git 作为可移植组织记忆的权威源。

## 兼容范围

| 项目 | v0.1.0 支持范围 |
|---|---|
| Codex | 支持 Codex CLI 的 Plugin/Marketplace 安装与 Skill 发现机制；本次发布在 `codex-cli 0.144.1` 实测，不据此声明最低 Codex 版本；安装后需开启新 Codex 任务重新加载插件 |
| Python | `>=3.10`；插件脚本与核心测试覆盖 Python 3.10、3.12 |
| 操作系统 | Windows 与 Linux |
| 核心记忆 | File/Git，默认启用且不依赖 Mem0、向量数据库或外部模型凭据 |
| 可选语义召回 | `mem0ai==2.0.11` 与 `httpx[socks]==0.28.1`；使用隔离虚拟环境和私有数据目录 |
| 私人数据 | 位于插件缓存和公开仓库之外，由用户控制；卸载插件不会删除 |

## 安装

前置条件：Codex CLI、Git、Python 3.10 或更高版本。

```powershell
codex plugin marketplace add coconilu/codex-opc-team --ref v0.1.0
codex plugin add codex-opc-team@opc
```

安装完成后开启一个新的 Codex 任务，再调用 `$opc-manager`。首次运行会先执行 Doctor；私人知识库只有在展示目标路径并得到确认后才会从空白模板初始化。

## 核心模式与可选 Mem0

| 模式 | 数据流 | 故障行为 |
|---|---|---|
| File/Git（默认） | 从当前 Git HEAD 验证并读取已批准的 canonical 知识 | 保持完整的对齐、委派、QA、复盘、审批与召回闭环 |
| Mem0（可选） | 为已批准知识建立可重建的语义索引；命中后仍须用 Commit 与内容哈希回读 File/Git | 未安装、禁用、版本不匹配、超时或索引过期时安全降级到 File/Git |

Mem0 不会被静默安装或启用。`v0.1.0` 只验证固定版本 `mem0ai==2.0.11`；其默认 LLM/Embedder 配置可能需要 `OPENAI_API_KEY`，并可能把已批准条目的摘要和正文发送给 OpenAI。启用前必须预览依赖、路径和真实数据流。完全本地 Provider、Mem0 Cloud 和自托管 Mem0 Server 不在本版承诺范围内。

## 数据与 Schema 迁移

| 对象 | v0.1.0 规则 |
|---|---|
| 新安装 | 插件安装与私人知识初始化分离；未经确认不创建用户数据 |
| 知识 Schema | 首版 Schema 为 `1`；初始化会生成独立私有 Git 仓库与基线 Commit |
| 旧本地原型 | 先快照和只读盘点，再预览迁移；不整体复制旧历史、日志、凭据或本机路径 |
| 已有私人知识 | 非空目录不覆盖；Schema 变更必须可预览、可重复、有备份，并验证条目、Hash、引用和 Git Diff |
| Mem0 索引 | 仅为派生数据；可删除并从当前可验证的 File/Git 知识重建，不迁移为权威事实 |
| 卸载 | 保留私人知识、Git 历史和 Mem0 数据；额外清理需要单独确认 |

从未发布版本或旧原型切换时，请按[本地原型迁移指南](migration-from-local-prototype.md)执行蓝绿切换，不要同时激活两个同名 Skill 集合。

## 已知限制

| 限制 | 影响与处理 |
|---|---|
| 插件发现按任务加载 | 安装、升级或回滚后需要开启新 Codex 任务 |
| Mem0 默认链路并非完全本地 | 启用前审查凭据与数据流；不接受时保持 File/Git 模式 |
| Mem0 在线模型调用不属于 CI Gate | CI 验证固定依赖的导入、真实 Adapter 构造、存储隔离与降级契约，不承诺外部服务可用性 |
| 只验证 Windows/Linux | 其他平台尚未纳入发布矩阵 |
| 成长必须人工治理 | 候选经验不会自动晋升；批准后仍须精确 Git 提交并由当前 HEAD 验证 |
| 没有常驻自治服务 | 团队运行依赖当前 Codex 任务、工具权限和用户授权边界 |

## 回滚

先移除插件，再按需要移除 Marketplace；这些命令不应删除私人知识：

```powershell
codex plugin remove codex-opc-team@opc
codex plugin marketplace remove opc
```

如果只需停用 Mem0，请使用 `$opc-memory` 先预览 `disable --dry-run`，确认后再执行 `disable --apply`；File/Git 权威知识保持可用。若从旧本地原型迁移而来，应恢复迁移前快照、Personal Marketplace 与仅属于旧 OPC 的配置，并在新任务中验证插件发现、Hook 零越界记录、知识读取和最小项目流程。

## Release Gate 证据

以下为 2026-07-13 的脱敏发布证据；不包含私人路径、知识内容、凭据、会话标识或原始 Hook 载荷。

| Gate | 证据 | 结果 |
|---|---|---:|
| G1 设计 | Repository validator、Plugin validator 与 6 个 Skill quick validator | PASS |
| G2 隐私 | 公开工作区与 Git 历史 privacy scan；Hook 越界、Marker、轮换与并发测试 | PASS |
| G3 核心 | 本地完整测试 `72/72`；无 Mem0 File/Git 闭环 | PASS |
| G4 可选后端 | 固定 Mem0 依赖的隔离安装、真实 Adapter 构造、私有 History/Qdrant、Telemetry 关闭与降级测试 | PASS |
| G5 分发 | 公开仓库提交在隔离 `CODEX_HOME` 安装/发现 6 个 Skills，并成功卸载且保留数据 | PASS |
| G6 端到端 | 脱敏执行链：Developer 5 → QA 132 **FAIL** → repair Developer 6 → 使用未改变的 QA matrix 复验 **PASS** → manager handoff `completed` | PASS |
| G7 回滚 | 旧原型快照 bundle verify；隔离 `CODEX_HOME` 回滚安装/卸载演练 | PASS |
| G8 发布 | 版本、Changelog、README、安装/迁移说明与本发布说明一致 | PASS |

GitHub Actions 证据：

| Run | 范围 | 结果 |
|---|---|---:|
| [29234352042](https://github.com/coconilu/codex-opc-team/actions/runs/29234352042) | Windows/Linux 核心与可选 Mem0 发布矩阵 | SUCCESS |
| [29234559980](https://github.com/coconilu/codex-opc-team/actions/runs/29234559980) | 最终 Actions runtime 复验；6/6 jobs，`actions/checkout@v7`、`actions/setup-python@v6`，无 Node 20 annotation | SUCCESS |

独立 QA 首次给出 FAIL、修复后按同一矩阵复验，是发布门禁的预期行为，不是被删除或降级的失败证据。

## 进一步阅读

- [安装与分发](installation-and-distribution.md)
- [测试与验收](testing-and-acceptance.md)
- [记忆架构](memory-architecture.md)
- [安全与隐私](security-and-privacy.md)
- [本地原型迁移](migration-from-local-prototype.md)
