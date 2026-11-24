# SOP：B 站 UP 主全量内容归档开发、建档与日常运维手册

本 SOP 配套 [bilibili-full-archive-pdr.md](bilibili-full-archive-pdr.md) 使用，覆盖 **开发落地 → 首次全量建档 → 日常增量运行 → 异常风控处置 → 定期维护** 全生命周期。

---

## 1. 前置检查清单（Pre-Flight Checklist）

在开始开发联调或执行正式归档前，确认以下条件全部满足：

- [ ] **基础依赖体检通过**：运行 `biliyt check`，确认 Python 3.11+、`yt-dlp`、`curl_cffi`、FFmpeg/FFprobe 均为 `OK`。
- [ ] **B 站登录 Cookie 有效**：`cookies/bilibili.txt` 为 Netscape 格式，且包含 `.bilibili.com` 域下的 `SESSDATA`、`bili_jct`、`DedeUserID`、`buvid3`。
- [ ] **代理隔离**：确认 `platforms.bilibili.proxy` 为 `null`（直连 B 站，避免海外代理节点触发 B 站风控）。
- [ ] **磁盘空间评估**：高画质长视频单个可达 `0.5–3 GB`；动态与专栏原图一般占 `50–500 MB / 千条`。开始全量建档前确保目标磁盘预留足够余量。
- [ ] **敏感文件隔离**：确认 `git status` 中未追踪 `config.yaml`、`cookies/`、`downloads/`、`state/`。

---

## 2. 开发与上线实施 SOP（Dev SOP）

开发阶段严格遵循「**离线单测先行 → 单目标小批量冒烟 → 幂等复验 → 再放开全量**」四步法，禁止直接对着线上接口无上限调试。

### Step 2.1 按里程碑递进开发
1. **M1（客户端与 WBI）**：实现 `wbi.py` 与 `client.py`，用单条 `nav` + `acc/info` 请求验证 WBI 签名与 Cookie 鉴权通过。
2. **M2（非视频内容归档）**：实现 `profile`、`dynamics`、`articles`、`favorites` 及 Markdown/图片本地化渲染。
3. **M3（精选评论）**：实现「置顶 + UP 主回复 + 热门前 50」抓取与挂载。
4. **M4（CLI 与状态集成）**：接入 `probe` / `sync --only` / `sync --full` / `status` / `--retry-failed`。

### Step 2.2 冒烟验收操作步骤（以 `bili_example` 为例）
每完成一个阶段，按顺序执行以下验证：

```bash
# 1) 运行全量单元测试
uv run pytest -v

# 2) 配置与依赖体检
biliyt check

# 3) 轻量探查（不落盘，确认接口连通、无 -352/-412）
biliyt probe --target bili_example --limit 5

# 4) 小批量分模块冒烟（每次先试 2 条）
biliyt sync --target bili_example --only profile,favorites
biliyt sync --target bili_example --only dynamics,articles --limit 2
biliyt sync --target bili_example --only comments --limit 2

# 5) 幂等复验：立刻重跑一遍，确认新下载数全为 0
biliyt sync --target bili_example --only dynamics,articles --limit 2

# 6) 检查汇总状态
biliyt status
```

### Step 2.3 产物抽检清单
人工抽查至少 1 条动态、1 篇专栏、1 个收藏夹、1 份视频评论：
- [ ] `.md` 文件用本地 Markdown 阅读器打开，内嵌图片全部正常显示且为相对路径（断网可看）。
- [ ] `.json` 文件为合法 UTF-8 JSON，无乱码、无截断。
- [ ] `.comments.md` 中包含置顶评论（如有）、UP 主回复（如有）以及热门评论（≤ 配置的 `top_n`）。
- [ ] `favorites/` 目录下仅有 `.json` 和 `.md` 列表文件，未误下载其他 UP 主的视频文件。

---

## 3. 首次全量建档 SOP（First-Time Full Archive）

针对一个全新目标 UP 主做「历史全部内容」归档时，**不要一把梭同时拉完几百个视频和几千条动态**，请严格按「先轻后重、分批落盘」的节奏执行：

### Step 3.1 规模摸底
```bash
biliyt probe --target <target_id>
```
记录该 UP 主的大致体量（投稿数、动态页数、专栏数、收藏夹数），估算磁盘空间与执行时间。

### Step 3.2 第一批：轻量文本与清单（低风控、高价值）
```bash
biliyt sync --target <target_id> --only profile,favorites --full
biliyt sync --target <target_id> --only articles --full
biliyt sync --target <target_id> --only dynamics --full
```
- 如动态超过 300 条，建议保持 `api_sleep_min: 2.0`、`api_sleep_max: 4.5`，单次持续抓取期间不要同时开多个爬虫进程共用同一份 Cookie。

### Step 3.3 第二批：投稿视频分批回溯（大体积）
高画质视频单条体积大，推荐每次 `--full --limit 10` 或 `--full --limit 20` 分批执行，或在夜间低峰期执行：
```bash
# 示例：全量扫描并每批补下 15 条历史未归档视频，利用归档文件自动断点续传
biliyt sync --target <target_id> --only videos --full --limit 15
```
重复运行直到 `probe` 显示视频待下载数为 `0`。

### Step 3.4 第三批：精选评论补齐
视频、动态、专栏主体落盘后再统一补齐精选评论：
```bash
biliyt sync --target <target_id> --only comments --full
```
- 如目标内容总数超过 200 条，建议分批按 `--full --limit 30` 执行，避免评论接口连续高密度翻页触发 `-352`。

### Step 3.5 建档收尾与失败补漏
```bash
biliyt status
# 若失败队列 > 0，等待 5~10 分钟后执行精准补漏
biliyt sync --target <target_id> --retry-failed
```

---

## 4. 日常增量同步 SOP（Daily Incremental Sync）

首次建档完成后，切回低频增量模式以兼顾时效性与账号安全：

### 4.1 推荐增量配置
在 `config.yaml` 中为日常运行设置合理的窗口：
- `filters.recent_limit: 20`（视频每次只检查最新 20 条）
- `archive.dynamics_limit: 30`（动态遇到已归档条目即早停）
- `archive.articles_limit: 20`
- `archive.comments.top_n: 50`
- 限速保持：`sleep_interval: 4.0~10.0`（视频）、`api_sleep: 1.5~3.5`（Web API）

### 4.2 手动或定时执行
- **手动触发**：
  ```bash
  biliyt sync
  ```
- **Linux / macOS（`cron`）**（推荐每天 1 次，如凌晨 `03:30`）：
  ```cron
  30 3 * * * cd /path/to/archive-workspace && biliyt sync >> state/cron.log 2>&1
  ```
- **Windows 任务计划程序**（推荐每天 1 次，如凌晨 `03:30`）：
  - **程序或脚本**：`biliyt`（或 `where.exe biliyt` 返回的 `biliyt.exe` 绝对路径）
  - **参数**：`sync`
  - **起始于**：`<你的归档工作目录>`（包含 `config.yaml`）
  - **防重叠**：在任务设置中勾选「如果任务已在运行，则不启动新实例」。

### 4.3 例行巡检（每周 1 次，2 分钟）
```bash
biliyt status
```
检查：
1. 最近一次运行报告（`state/last_run.json`）是否存在 `errors`。
2. `state/failed.jsonl` 待重试条数是否为 `0`；若不为 `0`，先查明原因再跑 `--retry-failed`。
3. 硬盘剩余空间是否充足。

---

## 5. 异常与风控应急处置 SOP（Incident Playbook）

| 故障现象 / 错误码 | 根因判断 | 立即处置动作 | 恢复命令 |
| :--- | :--- | :--- | :--- |
| **B 站返回 `-352` / `-412` 或 HTTP `412`** | 短时间请求过密触发风控熔断 | 1. **立即停止重跑**，等待 `15–60 分钟`<br>2. 在浏览器打开 B 站主页确认是否弹验证码，如有则手动完成验证<br>3. 适当调大 `api_sleep_min/max`（如 `3.0–6.0s`）<br>4. 若需要，重新导出最新 `cookies/bilibili.txt` | `biliyt check` 通过后执行 `biliyt sync --retry-failed` |
| **返回 `-101` / `-111` 或视频降为 360P / 无字幕** | `SESSDATA` 登录态过期或失效 | 1. 在浏览器重新登录 B 站<br>2. 用 *Get cookies.txt LOCALLY* 重新导出并覆盖 `cookies/bilibili.txt` | `biliyt check` → `biliyt sync --retry-failed` |
| **单条动态图片 `403` 或下载失败** | 缺少 `Referer` 或原图链接已失效 | 确认客户端携带 `Referer: https://www.bilibili.com/`；若原图已 404，保留 `.json` 原始 URL 记录不阻塞整批任务 | `biliyt sync --retry-failed` |
| **转发动态显示原动态已删除** | 原作者删除或隐藏了被转发内容 | 属于平台侧正常现象，渲染器在 Markdown 中标记「原动态已不可见」并保留外层转发文字 | 无需处理 |
| **评论区缺失或返回空** | UP 主关闭了评论区 / 精选评论模式 / 接口限流 | 检查原页面评论区状态；若为限流则按 `-352` 处置 | 限流恢复后用 `--retry-failed` 补抓 |
| **下载中途断电 / 手动 Ctrl+C 中断** | 进程异常退出 | 已完成条目的归档记录已即时落盘；清理可能残留的 `.part` 临时文件（`yt-dlp` 会自动续传） | 直接重新执行 `biliyt sync` |

---

## 6. 维护与安全规范（Maintenance SOP）

1. **Cookie 轮换周期**：B 站 `SESSDATA` 通常有效期 1–6 个月，建议 **每 30–60 天**或看到画质/鉴权告警时主动更新 `cookies/bilibili.txt`。
2. **依赖升级**：B 站接口与 `yt-dlp` 规则会随站点升级变化，若遇到解析大面积报错，先升级 `yt-dlp` 并跑单测回归：
   ```bash
   uv lock --upgrade-package yt-dlp
   uv sync
   ```
3. **归档状态备份**：`state/archives/` 与 `config.yaml` 体积很小（KB 级），建议定期备份到网盘或另一块硬盘——只要保留了 `state/archives/`，即使迁移机器也不会重复下载历史数据。
4. **隐私与合规**：严禁将 `cookies/`、`config.yaml` 上传到任何公开代码仓库；归档内容仅限个人离线收藏与研究备份。
