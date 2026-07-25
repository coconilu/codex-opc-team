# OPC App

OPC App 是一个可独立安装、显式启动、只在本机访问的私人 OPC 控制台。它不需要
Codex、Claude 或 Kimi 会话正在运行，但它也不是新的 Agent Harness：不运行
模型循环、工具、登录、Key、代理或 Agent 编排。

## 1. 能力边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| OPC App | 可视化、筛选、钻取、显式项目接入、App 自有偏好 | 批准、晋升、提交、删除、部署 |
| 旧 Dashboard | 从启动参数读取显式项目的兼容只读视图 | 安装态 App 状态和项目清单 |
| Agent Harness | 模型循环、工具、权限和任务会话 | OPC App 本地状态 |
| 后续 Adapters（#26） | 宿主 OPC 集成物的安装生命周期 | Agent 本体、账号、模型和 Key |
| File/Git knowledge | 唯一权威知识与 provenance | UI 缓存或可选索引 |

旧 Dashboard 与 App 通过 `opc_snapshot_service.py` 使用同一个
`opc-dashboard.snapshot.v1` 聚合和脱敏契约，不存在两套状态判断。

## 2. 从 checkout 体验

合成数据不会读取或写入私人目录：

```powershell
python plugins/codex-opc-team/scripts/opc_app.py --demo
```

真实模式允许空项目清单启动；在“项目”页面明确输入一个包含有效
`.opc/project.json` 的绝对目录：

```powershell
python plugins/codex-opc-team/scripts/opc_app.py
```

权威知识根和可重建数据根不会通过磁盘扫描发现。需要切换时，停止 App 后从启动
入口显式传入；移除覆盖参数即可回到既有 OPC 环境变量或平台默认根：

```powershell
python plugins/codex-opc-team/scripts/opc_app.py `
  --knowledge-root C:\path\to\opc-knowledge `
  --data-root C:\path\to\opc-private-data
```

Linux 使用对应的绝对路径。浏览器只收到脱敏后的知识状态，不会收到这些根路径。

默认 URL 是 `http://127.0.0.1:8570/`。按 `Ctrl+C` 停止；关闭后没有必须常驻
的 Agent 进程。端口冲突时可显式使用 `--port`，自动化可加 `--no-open`。

## 3. 独立安装、升级和回滚

管理脚本默认只输出计划；只有 `--apply` 才写 App runtime：

```powershell
python scripts/opc_app_admin.py install --dry-run
python scripts/opc_app_admin.py install --apply
python scripts/opc_app_admin.py status
```

Windows 从安装结果中的 `bin\opc-app.cmd` 启动，Linux 从 `bin/opc-app` 启动。
安装器同时生成 Python launcher；三个入口读取同一个原子 release pointer。

更新和回滚同样先预览：

```powershell
python scripts/opc_app_admin.py update --dry-run
python scripts/opc_app_admin.py update --apply
python scripts/opc_app_admin.py rollback
python scripts/opc_app_admin.py rollback --apply
```

App runtime 默认位于平台用户数据目录；App 状态与 runtime 分开：

| 平台 | 默认 App 状态根 |
|---|---|
| Windows | `%LOCALAPPDATA%\OPC\App` |
| Linux | `${XDG_STATE_HOME:-$HOME/.local/state}/opc-app` |

可用 `OPC_APP_HOME` 显式覆盖状态根。状态根保存项目接入清单和当前项目选择，
可以删除重建，但不是 OPC 业务事实来源。

卸载只删除 App runtime：

```powershell
python scripts/opc_app_admin.py uninstall
python scripts/opc_app_admin.py uninstall --apply
```

App 状态、项目 `.opc`、File/Git knowledge、Git 历史、用户配置和 Mem0 数据均
保留。安装器不安装 Codex、Claude、Kimi，也不编辑任何 Agent 全局配置。

## 4. 项目接入与隐私

接入流程固定为：

```text
用户输入一个绝对目录
  → 服务端只检查该目录和 .opc/project.json
  → 原子更新 App 自有 settings.json
  → 浏览器只收到项目安全名称、portable ID 和“显式目录 N”
```

App 不扫描磁盘，不把绝对路径、私人正文、凭据、session/turn、Hook Payload 或
原始日志放入响应。项目不可读、App 设置损坏、历史不完整、Mem0 缺失/禁用/故障
时会显示明确降级，不用演示数据填充。

如果 App 接入清单损坏，服务会拒绝覆盖并以空清单降级。先停止 App，保留一份
`settings.json` 备份，再移走该 App 自有文件即可重建；不要删除项目或知识目录。

## 5. 网络与写入边界

- 只允许 `127.0.0.1` 或显式 `::1`；
- Host 必须与实际 loopback authority 精确一致；
- Origin 必须缺失或与当前本地 URL 精确同源；
- 无 CORS、远程资源、遥测、响应缓存和默认远程监听；
- `/api/snapshot` 与所有 OPC 治理数据保持只读；
- `POST`/`DELETE` 只用于 App 项目清单和当前选择，必须携带同源进程级 CSRF；
- 所有其他方法和路由拒绝。

远程、多用户、治理写操作、完整历史数据库或高级分析需要独立 ADR 和验收。

## 6. 与旧 Dashboard 的关系

`opc_dashboard.py` 继续支持原命令和安全契约：

```powershell
python plugins/codex-opc-team/scripts/opc_dashboard.py --project-root .
```

不安装、不启动或卸载 OPC App 都不会改变 Codex Plugin、Skills、Hook、脚本、
旧 Dashboard 或安装态生命周期 Gate。
