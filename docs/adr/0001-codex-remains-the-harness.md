# ADR-0001：Codex 保持 Harness 地位

- **Status:** Accepted
- **Date:** 2026-07-13

## Context

用户已经依赖 Codex 的文件操作、命令执行、浏览器、网络检索、工具调用、权限和子 Agent 编排。此前独立 Agent MVP 的探索需要重复建设这些基础能力，增加维护成本，也会让工作流离开用户日常使用的 Codex 环境。

OPC 的差异化价值在于团队运行机制、角色职责、长期记忆、反馈治理和独立验收，不在于再次实现模型循环和工具运行时。

## Decision

Codex 是 OPC 唯一的执行 Harness。OPC 以 Codex Plugin 的 Skills、Hooks、脚本、模板和子 Agent 协作协议实现，不自建替代 Codex 的模型运行、浏览器、文件系统或权限层。

OPC 行为应适配 Codex 的公开插件契约；当 Codex 能力变化时优先更新适配层，而不是复制其实现。

## Consequences

### Positive

- 直接复用成熟工具和权限边界；
- 用户无需离开 Codex 或维护第二套工作环境；
- 工程重点集中在组织运行与记忆治理；
- 可通过 Git Marketplace 分发。

### Negative

- 受 Codex 插件、Hook 和子 Agent 生命周期约束；
- 需要跟踪 Codex 兼容性变化；
- 不承诺在其他 Agent Harness 中直接运行。

## Rejected alternatives

- **独立 Web/桌面 Agent 平台：** 重复建设 Harness，偏离现有工作习惯。
- **在 Codex 外运行永久总管服务：** 增加部署、认证和状态同步复杂度。
- **抽象成运行时无关框架后再接 Codex：** v0.1 阶段过早泛化，延迟真实闭环验证。
