# 配置详解（config.yaml）

`biliyt` 使用单份 `config.yaml` 管理全部下载任务，配置遵循三层覆盖优先级：

```text
全局默认（media / filters / rate_limit）
  └─▶ 平台默认（platforms.bilibili / platforms.youtube）
        └─▶ 单目标覆盖（targets[i]）
```

> 局部覆盖机制：在 `targets[i]` 中只填写需要修改的字段即可，未填写的字段会自动继承全局或平台配置。

---

## 1. `paths`：目录与命名模板

```yaml
paths:
  download_dir: "./downloads"
  state_dir: "./state"
  filename_template: "%(upload_date>%Y-%m-%d|unknown)s - %(title).120B [%(id)s].%(ext)s"
```

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `download_dir` | `./downloads` | 下载根目录。每个目标默认输出到 `<download_dir>/<platform>/<target.id>/`，可通过单目标 `output_subdir` 自定义。 |
| `state_dir` | `./state` | 状态目录，包含 `archives/<target.id>.txt`、`failed.jsonl`、`last_run.json` 与 `reports/`。 |
| `filename_template` | 见上 | yt-dlp 输出文件名模板。程序默认开启 `windowsfilenames: true`，自动清理 Windows 非法字符。 |

---

## 2. `media`：画质、容器、元数据与字幕

```yaml
media:
  max_height: 1080
  container: "mp4"
  prefer_codec: "auto"
  write_info_json: true
  write_thumbnail: true
  convert_thumbnail: "jpg"
  embed_metadata: true
  embed_chapters: true
  embed_thumbnail: false
  write_subtitles: true
  write_auto_subtitles: true
  subtitle_langs: ["zh-Hans", "zh-CN", "zh-Hant", "zh", "en", "en-US", "ai-zh"]
  subtitle_format: "srt"
  embed_subtitles: false
  write_danmaku: false
  sponsorblock_remove: []
```

| 字段 | 可选值 / 类型 | 说明 |
| :--- | :--- | :--- |
| `max_height` | `int \| null` | 分辨率上限，如 `1080`、`2160`；设 `null` 表示不限最高画质。 |
| `container` | `mp4` \| `mkv` \| `webm` | 音视频合并后的目标封装容器。 |
| `prefer_codec` | `auto` \| `avc` \| `hevc` \| `av1` | 视频编码偏好。若后续需要最大兼容性（剪辑软件/移动端），建议 `avc`（H.264）。 |
| `write_info_json` | `bool` | 保存完整 `.info.json`（标题、简介、标签、发布时间、播放量、分 P 信息等）。 |
| `write_thumbnail` | `bool` | 保存封面图。 |
| `convert_thumbnail` | `jpg` \| `png` \| `webp` \| `none` | 通过 FFmpeg 转换封面格式，默认统一为 `.jpg` 方便搬运/整理。 |
| `embed_metadata` | `bool` | 将标题、作者、发布日期等元信息写入视频容器。 |
| `embed_chapters` | `bool` | 写入视频章节标记（YouTube 章节 / B 站看点 `view_points`）。 |
| `embed_thumbnail` | `bool` | 是否把封面同时内嵌进视频文件。 |
| `write_subtitles` | `bool` | 下载人工字幕（B 站 CC 字幕 / YouTube 人工字幕）。 |
| `write_auto_subtitles` | `bool` | 下载平台自动生成字幕（主要用于 YouTube）。 |
| `subtitle_langs` | `list[str]` | 字幕语言代码列表（支持 yt-dlp 通配，如 `zh.*`、`en.*`）。 |
| `subtitle_format` | `srt` \| `vtt` \| `ass` \| `original` | 字幕统一转换格式。 |
| `embed_subtitles` | `bool` | 是否把字幕封装进视频文件内。 |
| `write_danmaku` | `bool` | 仅对 B 站生效：设为 `true` 时额外下载原始 XML 弹幕（`danmaku` 轨）。 |
| `sponsorblock_remove` | `list[str]` | 仅对 YouTube 生效：自动剪除指定片段，如 `["sponsor", "selfpromo", "intro"]`。 |

---

## 3. `filters`：下载范围与内容过滤

```yaml
filters:
  recent_limit: 20
  max_downloads: null
  date_after: "now-30days"
  date_before: null
  min_duration: 60
  max_duration: 7200
  include_keywords: []
  exclude_keywords: ["直播回放", "预告"]
  skip_live: true
  skip_shorts: false
  min_views: null
```

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `recent_limit` | `int \| null` | 探查阶段最多拉取目标最新 N 条投稿（对应 yt-dlp `playlistend`）。定时增量同步建议设为 `10~50`，避免翻页触发风控。 |
| `max_downloads` | `int \| null` | 单次同步每个目标最多实际下载的新视频条数。 |
| `date_after` / `date_before` | `str \| null` | 发布日期区间，支持 `YYYYMMDD`（如 `"20260101"`）或相对日期（如 `"now-7days"`、`"now-1month"`）。 |
| `min_duration` / `max_duration` | `int \| null` | 视频时长门槛（单位：秒）。 |
| `include_keywords` | `list[str]` | 白名单关键词（匹配标题或标签，不区分大小写，命中任一即通过）。 |
| `exclude_keywords` | `list[str]` | 黑名单关键词（匹配标题或标签，不区分大小写，命中任一即跳过）。 |
| `skip_live` | `bool` | 默认 `true`，跳过正在直播和预约直播预告。 |
| `skip_shorts` | `bool` | 仅对 YouTube 生效：跳过 `/shorts/` 链接及竖屏且 ≤60 秒的短视频。 |
| `min_views` | `int \| null` | 最低播放量门槛。 |

---

## 4. `rate_limit`：防风控与速率控制

```yaml
rate_limit:
  sleep_interval: 3.0
  max_sleep_interval: 8.0
  sleep_requests: 1.0
  concurrent_fragments: 4
  retries: 5
  rate_limit: null
```

- `sleep_interval` / `max_sleep_interval`：相邻两个视频下载之间随机休眠的秒数区间。
- `sleep_requests`：请求平台元数据接口的最小间隔秒数。
- `concurrent_fragments`：单个视频 DASH/HLS 分片的并发下载线程数（建议 `4~8`）。
- `rate_limit`：带宽上限，如 `"5M"`、`"800K"`，`null` 不限速。

---

## 5. `platforms`：分平台代理、Cookie 与 JS 运行时

```yaml
platforms:
  bilibili:
    proxy: null
    auth:
      cookies_from_browser: null
      cookies_file: "./cookies/bilibili.txt"
    impersonate: null

  youtube:
    proxy: "http://127.0.0.1:7890"
    js_runtime: "auto"
    auth:
      cookies_from_browser: null
      cookies_file: "./cookies/youtube.txt"
    impersonate: null
    extractor_args: {}
```

### Cookie 鉴权方式（二选一，同时设置时 `cookies_file` 优先）

1. **`cookies_file`（推荐，Windows 最稳定）**
   - 使用浏览器扩展（如 *Get cookies.txt LOCALLY*）导出对应站点的 Netscape 格式 Cookie，保存到 `cookies/bilibili.txt` 或 `cookies/youtube.txt`（`cookies/` 目录已加入 `.gitignore`）。
2. **`cookies_from_browser`**
   - 支持 `"chrome"`、`"edge"`、`"firefox"`，或指定 Profile 如 `"chrome:Default"`、`"edge:Profile 1"`。
   - 注意：Windows 上 Chromium 系浏览器运行期间会锁定 Cookie 数据库，若报错需先关闭浏览器或改用 `cookies_file`。

### YouTube `js_runtime` 说明

自 yt-dlp `2025.11.12` 起，YouTube 完整格式解析需要外部 JS 运行时执行 `yt-dlp-ejs`：
- `"auto"`（默认）：自动优先检测 `deno`，若未安装则自动注入 `node`（需 Node.js 20+）。
- 也可显式指定 `"deno"`、`"node"`、`"node:C:/Program Files/nodejs/node.exe"` 或 `"none"`。

---

## 6. `targets`：目标用户列表

```yaml
targets:
  - id: "bili_tech"
    name: "科技区 UP 主"
    platform: "bilibili"     # 可省略，系统可根据 source 自动推断
    source: "946974"         # 支持纯 UID / 空间链接 / 合集 / 收藏夹
    enabled: true
    output_subdir: "bilibili/tech_up"   # 可选，默认 <platform>/<id>
    filters:
      recent_limit: 10

  - id: "yt_mkbhd"
    name: "MKBHD"
    platform: "youtube"
    source: "@mkbhd"         # 支持 @handle / UC 频道 ID / 频道主页 / 播放列表
    youtube_tab: "videos"    # videos | shorts | streams | raw
    enabled: true
    media:
      max_height: 2160
```

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `id` | 是 | 全局唯一标识（仅字母/数字/`.`/`_`/`-`），用于输出子目录与归档文件名。 |
| `source` | 是 | 目标源地址或简写 ID。 |
| `name` | 否 | 终端展示名称，默认同 `id`。 |
| `platform` | 否 | `bilibili` 或 `youtube`，省略时按 `source` 自动识别。 |
| `enabled` | 否 | 默认 `true`；设为 `false` 时默认跳过（仅在 `--target <id>` 显式指定时执行）。 |
| `youtube_tab` | 否 | 仅对 YouTube 频道根路径生效，默认 `videos`，避免混入 Shorts 与直播回放；若要按原样抓取整频道填 `raw`。 |
