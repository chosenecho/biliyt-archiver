# 安全政策（Security Policy）

## 支持的版本

当前仅对最新版本（`main` 分支与最新 `0.x` 发布版）提供安全修复。

| 版本 | 支持状态 |
| :--- | :--- |
| `0.1.x` / `main` | 支持 |

## 报告安全漏洞

如果你发现了安全漏洞（例如路径遍历、命令注入、凭据或 Cookie 泄露、依赖供应链问题等），**请勿在公开 Issue 中披露细节**。

请通过以下方式私密报告：

1. 在 GitHub 仓库页面点击 **Security → Advisories → Report a vulnerability**（[新建私密安全报告](https://github.com/chosenecho/biliyt-archiver/security/advisories/new)）。
2. 在报告中尽量附带：复现步骤、受影响命令或配置、潜在影响范围与（如有）修复建议。

维护者会在 7 天内确认收到并反馈处置计划。

## 用户凭据与隐私安全提醒

`biliyt` 需要读取本地登录 Cookie（如 B 站 `SESSDATA` / `bili_jct` 或 YouTube Cookie）以访问高画质流与扩展接口。使用时请注意：

- **绝不提交运行时文件**：`config.yaml`、`cookies/`、`state/`、`downloads/`、`*.cookies.txt` 已列入 `.gitignore`，提交 PR 或反馈日志前请再次确认未包含真实 Cookie 或代理鉴权信息。
- **最小权限与定期轮换**：YouTube 建议使用单独账号导出 Cookie；B 站 `SESSDATA` 建议每 30–60 天或在账号风控后刷新。
- **问题排查脱敏**：提交 Bug Report 时，请隐去 `cookies_file` 内容、代理账密以及任何个人 Cookie 字段。
