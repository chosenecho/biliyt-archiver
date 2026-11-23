# 常见问题与运维排查指南

## 1. Bilibili 常见问题

### Q1：只能下载 360P / 480P，没有 1080P 或 4K？
B 站对未登录请求限制画质，且 1080P 高码率 / 4K / HDR / 杜比需要大会员 Cookie（含 `SESSDATA`）。
- **解决**：在 `config.yaml` 的 `platforms.bilibili.auth` 下配置 `cookies_file`（或 `cookies_from_browser`），然后运行 `.\biliyt.cmd check` 确认文件有效。

### Q2：批量抓取时报错 `HTTP Error 412` 或 `-352` 风控？
请求频率过高或单次翻页过多触发了 B 站风控。
- **解决**：
  1. 将 `filters.recent_limit` 控制在 `10~30`，日常增量无需拉取全量历史列表。
  2. 适当调大 `platforms.bilibili.rate_limit` 中的 `sleep_interval`（如 `5.0~12.0`）和 `sleep_requests`（如 `2.0`）。
  3. 导出最新的浏览器 `cookies.txt`（包含 `buvid3` / `SESSDATA`）。
  4. 等待 10~30 分钟后执行 `.\biliyt.cmd sync --retry-failed` 补下失败条目。

### Q3：如何下载 B 站弹幕？
在全局或单个 B 站目标下设置 `media.write_danmaku: true`，`biliyt` 会自动拉取原始 XML 弹幕文件（`*.danmaku.xml`）。如需转成带滚动效果的 `.ass` 字幕，可用 DanmakuFactory 或 `yt-dlp-danmaku` 后续处理。

---

## 2. YouTube 常见问题

### Q1：提示 `No supported JavaScript runtime` 或只能下载低画质？
自 yt-dlp `2025.11.12` 起，YouTube 需要外部 JS 运行时（Deno 2.3+ 或 Node.js 20+）配合 `yt-dlp-ejs`。
- **排查**：运行 `.\biliyt.cmd check`，确认 `yt-dlp-ejs` 与 `JS Runtime` 均为 `OK`。
- `biliyt` 在 `platforms.youtube.js_runtime: "auto"` 下会自动检测 Deno 或 Node.js 并注入，无需手动传 `--js-runtimes`。

### Q2：报错 `Sign in to confirm you're not a bot` 或 `HTTP Error 403`？
通常由代理出口 IP 声誉较差（如常见机房 IP）或高频访问引起。
- **解决**：
  1. 切换更干净的代理节点，并在 `platforms.youtube.proxy` 中配置。
  2. 为 YouTube 配置登录 Cookie（建议使用导出文件 `cookies/youtube.txt`，并尽量用小号避免主号触发异常提醒）。
  3. 更新 `yt-dlp` 到最新版本（甚至 `nightly` 渠道，见下方第 4 节）。
  4. 如频繁遇到 PO Token 校验，可在虚拟环境中安装 `bgutil-ytdlp-pot-provider` 插件。

---

## 3. Windows 环境问题

### Q1：使用 `cookies_from_browser: "chrome"` 或 `"edge"` 报错无法复制/解密 Cookie？
Windows 下 Chrome / Edge 运行时会独占锁定 `Cookies` SQLite 文件，且新版本启用了 App-Bound Encryption。
- **解决**：优先改用 `cookies_file`（使用浏览器插件导出 Netscape 格式 `cookies.txt` 到 `cookies/` 目录），最稳定且无需关闭浏览器。

### Q2：PowerShell 报错“未对文件进行数字签名，无法运行脚本”？
请直接使用批处理入口 **`.\biliyt.cmd`**，不要使用 `.ps1` 脚本。`biliyt.cmd` 在 PowerShell、CMD 以及 Windows 任务计划程序中均可直接运行。

---

## 4. 升级 `yt-dlp`（重要）

B 站与 YouTube 接口变更频繁，遇到解析报错时**第一步永远是更新 `yt-dlp`**：

```powershell
# 升级到最新稳定版
uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
uv sync --python-preference only-system --python 3.12

# 或升级到官方推荐的 nightly 预发布版
uv pip install --python .\.venv\Scripts\python.exe -U --pre "yt-dlp[default,curl-cffi]"
```

更新后运行 `.\biliyt.cmd check` 确认版本号生效。

---

## 5. 状态文件与重置说明

| 路径 | 作用 | 操作建议 |
| :--- | :--- | :--- |
| `state/archives/<target_id>.txt` | 记录目标已成功下载的视频 ID | 想重下某条视频时，删除对应行；想重下整个目标时，删除该文件。 |
| `state/failed.jsonl` | 记录下载失败的条目及重试次数 | 运行 `.\biliyt.cmd sync --retry-failed` 补下；成功后自动移除。 |
| `state/reports/run_*.json` | 每次正式同步的明细报告 | 可直接被外部脚本读取用于通知或审计。 |
| `state/last_run.json` | 最近一次运行摘要 | `.\biliyt.cmd status` 的数据来源。 |
