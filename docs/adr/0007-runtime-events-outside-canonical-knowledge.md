# ADR-0007：运行事件与权威知识隔离

- **Status:** Accepted
- **Date:** 2026-07-19

## Context

首个公开提交 `cca1885` 同时引入了两个容易混淆的事实：知识模板创建了空的 `evaluations/events` 目录，而 Hook 实现从一开始就只把事件写入 `PLUGIN_DATA/run-events/<run_id>.jsonl`，缺少 `PLUGIN_DATA` 时回退项目 `.opc/events.jsonl`。公开 Git 历史中没有把 `hook-events.jsonl` 写入知识库的代码路径。

一次已脱敏的安装状态检查在权威知识库中发现了未跟踪的 `evaluations/events/hook-events.jsonl`。仓库证据无法判断它由首版发布、安装前的本地原型还是外部脚本产生，因此其具体历史来源记录为 unresolved，不把推测冒充事实。空目录约定仍给错误实现留下了错误暗示，也让 Git 审计把已知运行文件误报为 `UNCOMMITTED_KNOWLEDGE`。

## Decision

Hook 和运行事件不是组织知识。有效 OPC Run 的事件只允许进入 Codex 提供的私有 `PLUGIN_DATA/run-events`；`PLUGIN_DATA` 缺失、无效或与 `OPC_KNOWLEDGE_HOME` 重叠时，只允许回退到已验证项目的 `.opc/events.jsonl`。新的知识模板不再创建 `evaluations/events`。

`status` 和 `doctor` 只按已知 legacy 位置与文件名检查元数据，不读取或输出事件内容。已知 legacy 运行文件以 `LEGACY_RUNTIME_ARTIFACTS` 单独报告，不作为 `UNCOMMITTED_KNOWLEDGE`，也不改变 File/Git 权威条目的判断。

修复流程默认运行 `legacy-events --dry-run`。预览返回源/目标相对路径；plan token 绑定解析后的 knowledge/data/archive roots、精确源/目标和不读取内容的 `lstat` 对象身份。只有再次显式使用 `--apply --plan-token <token>` 才能归档未跟踪的普通文件。Apply 在私有 data-root 锁内重新核验 token、对象类型、Git 状态和目标空缺，再通过同文件系统 hard link 原子创建不覆盖目标，确认源/目标仍是获批的同一对象后才移除源路径。自动流程拒绝符号链接、已跟踪文件、已有目标、变化后的计划和跨文件系统失败，并且永不自动删除、提交或上传事件数据。

## Consequences

### Positive

- 最小 OPC Run 和误配置的 Hook 都不会污染权威知识 Git 状态；
- Doctor 能区分真正的知识变更和历史运行文件，并给出不暴露内容的操作建议；
- 私人事件迁移有预览、明确批准、路径约束和失败保留源文件的门禁；
- File/Git 晋升 Commit、可选 Mem0 与无 Mem0 降级契约不变。

### Negative

- `PLUGIN_DATA` 与知识路径重叠时，事件会转为项目本地保留，而不是使用全局运行目录；
- 已跟踪、符号链接或跨文件系统的 legacy 文件需要用户在审计后手动处理；
- legacy 文件仍会使 Git 工作区显示 dirty，但不会再伪装成待提交的权威知识。

## Rejected alternatives

- **继续保留 `evaluations/events` 并只依赖 `.gitignore`：** 仍把运行数据放在错误生命周期中，也无法防止误提交。
- **Doctor 自动删除或移动：** 会在未获授权时破坏私人审计数据。
- **读取内容判断是不是 Hook 事件：** 扩大敏感数据接触面，且不是安全分类所必需。
- **把 legacy 文件作为组织知识提交：** 违反 File/Git 晋升和数据最小化边界。
