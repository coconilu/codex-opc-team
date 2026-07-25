# ADR 0017：独立本地 OPC App 是控制平面，不是 Agent Harness

- 状态：Accepted
- 日期：2026-07-25

## 背景

ADR-0016 的本地只读 Dashboard 已验证固定 DTO、服务端脱敏和
loopback-only 浏览器视图可以安全降低经理理解 OPC 状态的成本，但旧入口仍需
用户从仓库或插件目录显式执行 `opc_dashboard.py`，且只能读取启动命令传入的
项目。

用户需要一个不依赖 Codex、Claude 或 Kimi 会话生命周期的稳定产品入口，同时
必须继续遵守 ADR-0001：OPC 不重复建设模型循环、工具运行时、权限层和 Agent
编排 Harness。独立 App 也不能把 File/Git 权威状态复制进第二个数据库，或绕过
现有批准、晋升、提交和发布门禁。

## 决策

提供独立安装和显式启动的本地 OPC App，并保持以下边界：

1. **独立的是控制平面生命周期，不是执行 Harness。** App 只负责可视化、
   显式项目接入和 App 自有偏好；Agent 继续负责模型与工具执行。
2. **一个 Snapshot 语义来源。** `opc_snapshot_service.py` 是旧 Dashboard 和
   新 App 共用的 core/service，实际聚合、状态判断、固定 DTO、脱敏、demo
   校验与稳定读取均位于该模块。`opc_dashboard.py` 和 `opc_app.py` 只是入口
   adapter，不互相 import，也不复制治理规则。
3. **File/Git authoritative。** App 不持久化 OPC 业务事实。Mem0 仍是可选、
   可删除重建的 Provider；缺失或故障时准确降级。
4. **App 状态与业务状态隔离。** 项目接入清单和当前项目选择位于
   `OPC_APP_HOME` 或平台文档化的用户状态目录，不在 checkout、插件安装目录、
   runtime、项目/`.opc`、知识根或可重建数据根内。服务在创建目录或写入前，
   对 lexical path 与 canonical realpath 执行双向 overlap 检查，并拒绝
   symlink/junction ancestor；已有恶意清单也不能绕过。清单可删除重建。
5. **显式接入，不扫描。** 用户提交一个绝对项目目录；服务端只验证该目录的
   `.opc/project.json`。响应仅返回 `显式目录 N`、安全项目名和 portable ID，
   不回传绝对路径。
6. **治理 API 只读。** `/api/snapshot` 继续只读。唯一写路由只管理 App
   接入清单和选择状态；使用精确 Host/Origin、每进程 CSRF token、固定 JSON
   大小、方法与路由白名单。
7. **本地网络边界不变。** 默认只绑定 `127.0.0.1`，也只允许显式 `::1`；
   无 CORS、远程资源、遥测、默认远程监听和常驻 Agent 进程。
8. **无大型构建链。** 延续 Python 标准库服务和无构建步骤的 HTML/CSS/JS。
   当前交互规模没有足够证据引入 Electron、Tauri、React 或 Node 供应链。
9. **独立可回滚分发。** App 安装器把公开插件快照复制到用户级 runtime，
   为每个 release 持久化完整文件清单、大小与 SHA-256，用内容哈希 release
   与原子 current pointer 支持安装、升级和回滚。staging、激活、启动、状态检查
   和回滚前都会复验完整性；损坏 release 不会被激活。launcher 也作为带完整
   manifest 和 SHA-256 的独立制品集合，在 runtime 同级 staging 后以
   `bin → backup`、`stage → bin` 的可恢复目录交换激活；Windows 不将目录交换
   描述为原子操作。失败时先恢复完整旧集合，恢复本身失败则保留 backup 供人工
   恢复。launcher 与 release 解耦并在运行时复验 current release，因此 launcher
   激活完成后才切换 pointer，pointer 写失败时旧 pointer 仍可由新 launcher 安全
   启动。卸载只删除 runtime；
   App 状态、项目、File/Git knowledge、Git 历史、用户配置和 Mem0 数据全部保留。
10. **旧入口兼容。** 不安装或不启动 App 时，Codex Plugin、Skills、Hook、
    脚本和 `opc_dashboard.py` 的行为与生命周期 Gate 不变。

## API 与状态

| 路由 | 方法 | 权限 |
|---|---|---|
| `/api/snapshot` | `GET`, `HEAD` | 共享、脱敏、治理只读 |
| `/api/app-context` | `GET`, `HEAD` | 返回脱敏接入清单和进程级 CSRF |
| `/api/projects` | `POST` | 只新增 App 接入记录 |
| `/api/projects/<app-id>` | `DELETE` | 只移除 App 接入记录 |
| `/api/selection` | `POST` | 只更新 App 当前项目 |

所有其他路由和方法拒绝。未来远程、多用户、治理写操作或完整历史数据库必须
另立 ADR，不能从这些 App 设置路由扩权。

## 结果

正面结果：

- App 可在没有 Agent 会话时启动，关闭前台进程即停止；
- 旧 Dashboard 与 App 对同一项目使用相同 snapshot schema 和状态语义；
- UI 更适合筛选、切换、钻取和理解降级，但不获得治理写权限；
- 安装、升级、回滚和卸载与私人数据生命周期分离。

代价：

- 仍需要本机 Python 3.10+；
- 当前是本地 Web App，不是原生桌面壳或移动客户端；
- App 接入清单损坏时会拒绝覆盖并降级为空清单，需要用户删除或修复 App 自有
  状态文件；
- 没有持久化完整运行历史，不能把 sidecar 推断成历史数据库。

## 未采用方案

| 方案 | 原因 |
|---|---|
| 独立 Agent 平台或聊天界面 | 重复 Harness，违反 ADR-0001 |
| Electron/Tauri/React 首版 | 当前没有足够交互或分发证据承担额外构建链 |
| 自动扫描 Home/磁盘 | 扩大隐私范围，项目边界不可预测 |
| SQLite/搜索数据库 | 形成第二事实来源和新的迁移生命周期 |
| 在 App 内批准、晋升或提交 | 绕过现有确认与独立 QA 门禁 |
| 在本 Issue 安装 Codex/Claude/Kimi Adapter | 属于后续 Issue #26 |
