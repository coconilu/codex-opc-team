# Security Policy

## Supported versions

安全修复优先落到受支持的发布线，并按需要同步到 `main`。本表随 Release 更新：

| Version | Supported |
|---|---:|
| `0.1.x` | Yes |
| `main` | Yes |
| 未列出的历史版本 | No |

## Reporting a vulnerability

请使用 GitHub 仓库的 **Private vulnerability reporting** 功能提交安全报告。若该功能暂不可用，请联系仓库维护者并只发送最少必要信息；不要在公开 Issue、Discussion、Pull Request 或日志附件中披露漏洞细节和私人数据。

报告建议包含：

- 受影响版本或提交；
- 可重复的最小步骤；
- 影响范围；
- 已知的临时缓解方式；
- 已脱敏的证据。

维护者确认后会协调修复、验证和披露时间。在修复发布前，请避免公开利用细节。

## Security boundaries

本项目尤其关注以下风险：

| 风险 | 安全要求 |
|---|---|
| Hook 越界采集 | 只有明确存在且有效的 OPC 运行标记时才允许记录 |
| 公开仓库泄露 | 私人知识、原始日志、本机路径、会话标识和凭据禁止提交 |
| 记忆污染 | 召回内容必须验证来源、版本和内容哈希；候选经验不得自动晋升 |
| 可选后端故障 | Mem0 故障不得阻断 File/Git 基线，也不得静默丢失知识 |
| 配置劫持 | 默认不修改 Codex 全局配置；可选修改必须预览、确认、备份并可恢复 |
| 卸载破坏 | 卸载插件不得删除用户的私人知识库和可审计历史 |

完整威胁模型和发布检查见 [安全与隐私](docs/security-and-privacy.md)。
