# 常见问题与运维排查指南

## 1. Bilibili 常见问题

### Q1：只能下载 360P / 480P，没有 1080P 或 4K？
B 站对未登录请求限制画质，且 1080P 高码率 / 4K / HDR / 杜比需要大会员 Cookie（含 `SESSDATA`）。
- **解决**：在 `config.yaml` 的 `platforms.bilibili.auth` 下配置 `cookies_file`（或 `cookies_from_browser`），然后运行 `biliyt check` 确认文件有效。

### Q2：批量抓取时报错 `HTTP Error 412` 或 `-352` 风控？
请求频率过高或单次翻页过多触发了 B 站风控。
- **解决**：
  1. 将 `filters.recent_limit` 控制在 `10~30`，日常增量无需拉取全量历史列表。
  2. 适当调大 `platforms.bilibili.rate_limit` 中的 `sleep_interval`（如 `5.0~12.0`）和 `sleep_requests`（如 `2.0`）。
  3. 导出最新的浏览器 `cookies.txt`（包含 `buvid3` / `SESSDATA`）。
  4. 等待 10~30 分钟后执行 `biliyt sync --retry-failed` 补下失败条目。

### Q3：如何下载 B 站弹幕？
在全局或单个 B 站目标下设置 `media.write_danmaku: true`，`biliyt` 会自动拉取原始 XML 弹幕文件（`*.danmaku.xml`）。如需转成带滚动效果的 `.ass` 字幕，可用 DanmakuFactory 或 `yt-dlp-danmaku` 后续处理。

---

## 2. YouTube 常见问题

### Q1：提示 `No supported JavaScript runtime` 或只能下载低画质？
自 yt-dlp `2025.11.12` 起，YouTube 需要外部 JS 运行时（Deno 2.3+ 或 Node.js 20+）配合 `yt-dlp-ejs`。
- **排查**：运行 `biliyt check`，确认 `yt-dlp-ejs` 与 `JS Runtime` 均为 `OK`。
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

### Q2：PowerShell 执行策略拦截或本地 `.venv` 报错 `_ssl` / `_ctypes` DLL 加载失败？
- **优先使用全局 CLI**：通过 `uv tool install` 安装的 `biliyt` 在 Windows 上会生成原生可执行文件 `biliyt.exe`（可用 `where.exe biliyt` 查看路径），在 PowerShell、CMD 与 Windows 任务计划程序中均可直接运行，不受 `.ps1` 执行策略限制。
- **源码模式 `.venv` 异常回退**：若直接从仓库运行 `uv run biliyt` 时遇到 `.venv\Scripts\python.exe` 加载 `_ssl` / `_ctypes` DLL 失败，可用系统 Python 配合 `PYTHONPATH` 运行：
  ```powershell
  $env:PYTHONPATH = "$PWD\.venv\Lib\site-packages;$PWD\src"
  python -m biliyt check
  ```

---

## 4. 升级 `yt-dlp`（重要）

B 站与 YouTube 接口变更频繁，遇到解析报错时**第一步永远是更新 `yt-dlp`**：

```bash
# 全局 CLI 安装方式（推荐）：升级工具与底层依赖
uv tool upgrade biliyt

# 源码开发模式：升级锁文件并同步依赖
uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
uv sync

# 源码开发模式：如需升级到 yt-dlp nightly 预发布版
uv pip install -U --pre "yt-dlp[default,curl-cffi]"
```

更新后运行 `biliyt check` 确认版本号生效。

---

## 5. 状态文件与重置说明

| 路径 | 作用 | 操作建议 |
| :--- | :--- | :--- |
| `state/archives/<target_id>.txt` | 记录目标已成功下载的视频 ID | 想重下某条视频时，删除对应行；想重下整个目标时，删除该文件。 |
| `state/failed.jsonl` | 记录下载失败的条目及重试次数 | 运行 `biliyt sync --retry-failed` 补下；成功后自动移除。 |
| `state/reports/run_*.json` | 带稳定 `run_id` 的同步或修复运行报告 | 保留用于审计及数据库运行历史恢复；同秒报告分别保存。 |
| `state/last_run.json` | 最近一次运行摘要 | `biliyt status` 的最近运行信息来源。 |
| `downloads/<platform>/<target_id>/.biliyt/manifests/` | 逐条产物清单，保存附件与索引完成情况 | 随下载目录备份，保留隐藏目录；附件缺项优先用 `repair`。 |
| `downloads/<platform>/<target_id>/profile/snapshots/` | 带 `snapshot_id` 的主页历史与资产路径 | 与最新主页共同参与恢复；勿仅备份 `profile.json`。 |

删除归档记录会触发媒体重下。视频已经保存、只缺字幕或索引时，先用下方 `repair` 检查缺项。

## 6. 已下载成功，为何仍显示待修复或搜不到？

媒体成功、附件完整和索引成功分别记录。`status` 的“部分完成 / 待索引 / 待修复”按内容条目计数，可重叠；报告中的 `partial`、`index_failed`、`pending_repair` 也不应相加作为总数。索引写入异常会保留警告与待修复状态。

```bash
# 默认只读预览：不联网、不写清单或数据库
biliyt repair --target <target_id>
# 仅重试本地索引
biliyt repair --target <target_id> --only index --apply
# 每目标最多 10 条，按需补齐元数据、独立字幕或图片
biliyt repair --target <target_id> --only metadata,subtitles,images --limit 10 --apply
```

`repair --only` 可组合 `index,metadata,subtitles,images`；同一条目的多个操作共同占用一个 `--limit` 名额。字幕初版补独立字幕文件，已有视频的嵌入字幕需求仍待处理，不进行重封装。旧归档附件如标为 `unknown`，表示缺少当时请求记录，不会据此自动联网批量补抓；显式关闭、平台明确不可用的附件也不循环重试。

若视频获取本身失败，可继续用 `sync --retry-failed`；默认修复计划也会整合、去重相关失败队列条目。修复执行需联网时，沿用现有代理、Cookie 与 B 站风控熔断规则，先解决接口或登录问题再执行。

## 7. 重建失败、写锁冲突与备份恢复

- `biliyt db rebuild --target <target_id>` 是增量导入，不清空其他目标；`--reset` 表示全库替换，与 `--target` 组合会在写库前报参数错误。
- `sync`、`repair --apply` 与 `db rebuild` 共用写锁。出现“另一个写入操作正在进行”时，等待已有操作结束；不要通过删除锁文件绕过运行中的进程。
- 全库重建先用 SQLite backup 留存原库副本，并导出原库独有历史；在同目录临时库导入文件，经过完整性、外键、计数检查后才处理 WAL、关闭连接并替换。坏 JSON 或导入错误会保留原库，按报告路径修正文件。
- Windows 上有 DB Browser 等其他连接时可能阻止检查点或替换，关闭连接后重试；替换失败信息包含备份路径。手动恢复须先停止写入、关闭连接，将当前数据库及 WAL/SHM 另存，再恢复一致性备份，详见 [SQLite SOP](sqlite-storage-sop.md)。

完整备份需包含 `downloads/`（隐藏清单与主页历史）、`state/reports/`、`state/archives/`、`state/failed.jsonl` 及配置，并通过 SQLite 一致性快照备份数据库。重建可恢复现存内容与运行历史的索引，归档文本和失败队列仍依赖自身备份；旧目录已丢失的主页历史不会从最新主页中还原。数据库 v2 通过逐版迁移和稳定 `snapshot_id` / `run_id` 避免重复导入相同历史。

命令退出码：`0` 成功；`1` 配置或参数错误；`2` 处理仍有失败；`3` 写锁冲突。

## 8. 升级后配置报未知字段、日期或区间错误？

可靠性更新保留现有正确配置，不新增必填配置或运行时依赖，但开始检查此前被忽略的拼错字段。根据错误路径核对 [配置详解](configuration.md)。日期请使用带引号的有效 `YYYYMMDD` 或受支持的相对日期，如 `"now-7days"`，开始日期应早于或等于结束日期。全局、平台与目标先按显式字段合并，再校验最终范围；例如全局最短 300 秒与目标最长 60 秒会报错，需调整实际约束。未填写继续继承，`null` 仅可清空原字段允许为空的值。
