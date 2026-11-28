<p align="left">
  <img src="docs/assets/biliyt-logo.svg" alt="biliyt logo" width="56" height="56" />
</p>

# biliyt

基于 `yt-dlp`、B 站 WBI Web API 与本地 SQLite FTS5 的命令行归档工具，用一份 `config.yaml` 增量同步 Bilibili UP 主与 YouTube 频道的视频、字幕、动态、专栏、公开收藏夹与精选评论，并支持离线中文全文检索。

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-22C55E)](LICENSE)
[![Status: v0.1.0 Beta](https://img.shields.io/badge/status-v0.1.0%20beta-F59E0B)](CHANGELOG.md)

## 为什么用 biliyt，而不是直接写 yt-dlp 脚本？

`biliyt` 站在 `yt-dlp` 的肩膀上，但解决的是**多目标长期增量追更**与 **B 站非视频内容全量归档** 问题：

| 对比维度 | 原生 `yt-dlp` CLI / 批处理脚本 | `biliyt` |
| :--- | :--- | :--- |
| 多目标差异化配置 | 每个频道一套参数或 shell 脚本，难维护 | 单份 `config.yaml`，支持 **全局 → 平台 → 单目标** 三层局部覆盖（含 B 站直连 + YouTube 独立代理） |
| 增量更新与风控节流 | 直接拉全量列表，容易反复触发限流 | 两阶段同步：先轻量探查并过滤已归档/不达标条目，再按限速与并发上限逐条下载 |
| B 站非视频内容 | 仅覆盖投稿视频 | 补充 **主页档案与合集、动态（含转发与原图本地化）、专栏（旧 `cv` + 新版 Opus）、公开收藏夹清单、精选评论（置顶 + UP 主回复 + 热门前 N）**，Markdown + JSON 双存 |
| 本地检索与元数据管理 | 散落的 `.info.json` 与 `.srt` 文件 | 同步写入本地 `state/biliyt.db`（WAL + FTS5 trigram），支持 `biliyt search` 跨视频/字幕/动态/专栏/评论检索，且可零网络从 `downloads/` 重建 |
| 失败补漏与运维审计 | 报错后需手动找回漏下的链接 | 抓取失败进入 `state/failed.jsonl`；逐条产物清单记录附件缺项与索引状态，`biliyt repair` 预览补齐计划；同步报告保留运行历史 |

**什么时候不该用 `biliyt`：**

- 只需要临时下载单个视频或播放列表 → 直接运行 `yt-dlp` 更轻更快。
- 需要带网页后台的 Docker 媒体服务器（`Pinchflat`、`Tube Archivist`），或面向 Plex/Jellyfin 媒体库自动刮削入库（`ytdl-sub`）→ 那些工具更贴合你的场景。
- 期望免登录绕过 B 站大会员画质或抓取私密收藏夹 → 本工具严格依赖你自己的本地登录 Cookie，仅用于个人离线收藏与研究备份。

## 快速开始

### 前置依赖

- **Python `>=3.11`** 与 [**`uv`**](https://docs.astral.sh/uv/)
- **`ffmpeg` / `ffprobe`**（加入系统 `PATH`，用于音视频混流与封面/字幕转换）
- **Deno `2.3+` 或 Node.js `20+`**（抓取 YouTube 时必需，用于 `yt-dlp-ejs` 挑战脚本解析，`biliyt` 会自动检测并注入）

### 安装与首次体检

```bash
# 全局安装跨平台 CLI（Windows / macOS / Linux 通用）
uv tool install git+https://github.com/chosenecho/biliyt-archiver.git

# 1) 在你的归档工作目录中生成 config.yaml 与 cookies/ 目录
biliyt init

# 2) 检查 Python / yt-dlp / yt-dlp-ejs / curl_cffi / FFmpeg / JS Runtime 与配置有效性
biliyt check
```

> [!NOTE]
> - **从 Releases 离线安装**：也可从 [GitHub Releases](https://github.com/chosenecho/biliyt-archiver/releases) 下载 `.whl` 后运行 `uv tool install biliyt-*.whl`（或 `pipx install biliyt-*.whl`）。
> - **源码开发模式**：克隆仓库后执行 `uv sync`，再用 `uv run biliyt <子命令>` 运行（详情见 [CONTRIBUTING.md](CONTRIBUTING.md)）。

### 编辑配置并开始同步

打开生成的 `config.yaml`（完整字段与默认值见 [`config.example.yaml`](config.example.yaml)），按需配置代理、Cookie，并将目标的 `enabled` 改为 `true`：

```yaml
platforms:
  bilibili:
    proxy: null # B 站建议直连
    auth:
      cookies_file: "./cookies/bilibili.txt" # Netscape 格式导出（不配置也可基础探查，高画质与扩展归档需登录 Cookie；暂不用可设 null）
  youtube:
    proxy: "http://127.0.0.1:7890" # 按需改为本地代理，直连可设 null
    js_runtime: "auto"

targets:
  - id: "bili_example"
    source: "946974" # B 站 UID / 空间链接 / 合集链接
    enabled: true
    archive:
      dynamics: true
      articles: true
      comments:
        enabled: true
        top_n: 50
  - id: "yt_example"
    source: "@mkbhd" # YouTube @handle / 频道或播放列表链接
    enabled: true
```

接着按「先探查 → 演练 → 正式同步 → 检索」的节奏运行：

```bash
# 1) 轻量探查各目标的投稿/扩展内容与待下载数（不落盘）
biliyt probe

# 2) Dry-run 演练：预览将要下载的具体条目
biliyt sync --dry-run

# 3) 正式增量同步所有启用的目标
biliyt sync

# 4) 跨视频、字幕、动态、专栏与评论全文检索
biliyt search "关键词" --kind subtitle,article
```

### 运行输出示例

执行 `biliyt check` 时的终端输出（来自真实 CLI 检查）：

```text
                         biliyt v0.1.0 环境与配置体检
┌────────────┬──────┬─────────────────────────────────────────────────────────┐
│ 检查项     │ 状态 │ 详情                                                    │
├────────────┼──────┼─────────────────────────────────────────────────────────┤
│ Python     │ OK   │ 3.12.10                                                 │
│ yt-dlp     │ OK   │ 2026.10.26                                              │
│ yt-dlp-ejs │ OK   │ 已安装（YouTube 挑战脚本支持）                          │
│ curl_cffi  │ OK   │ 已安装（支持浏览器 TLS 指纹模拟）                       │
│ FFmpeg     │ OK   │ ffmpeg version 8.x                                      │
│ FFprobe    │ OK   │ ffprobe version 8.x                                     │
│ JS Runtime │ OK   │ Node.js: v24.x（自动注入 YouTube EJS）                  │
│ 配置文件   │ OK   │ 校验通过：共 2 个目标（已启用 0 个）                    │
└────────────┴──────┴─────────────────────────────────────────────────────────┘
```

## 命令速查

| 命令 | 说明 | 常用参数 |
| :--- | :--- | :--- |
| `biliyt init` | 生成 `config.yaml` 与 `cookies/` 目录 | `-c/--config`, `-f/--force` |
| `biliyt check` | 检查依赖、JS Runtime、配置语法、代理 TCP 连通性与 Cookie 文件 | `-c/--config` |
| `biliyt probe` | 探查投稿列表与 B 站扩展内容，统计已归档 / 过滤跳过 / 待下载数 | `-c/--config`, `-t/--target`, `-p/--platform`, `-n/--limit`, `--show` |
| `biliyt sync` | 执行增量视频同步、B 站扩展归档或历史失败重试 | `-c/--config`, `-t/--target`, `-p/--platform`, `-n/--limit`, `--only`, `--full`, `--refresh-comments`, `--dry-run`, `--retry-failed` |
| `biliyt status` | 汇总归档数、失败队列、部分完成 / 待索引 / 待修复条目数与最近运行摘要 | `-c/--config` |
| `biliyt repair` | 默认只读预览；显式执行时补齐附件、元数据或重试索引 | `-c/--config`, `-t/--target`, `--only`, `--apply`, `-n/--limit` |
| `biliyt db stats` | 查看 `state/biliyt.db` 体积与各数据表行数 | `-c/--config` |
| `biliyt db rebuild` | 从本地归档与运行报告离线导入；`--reset` 校验临时库后替换全库，禁与 `--target` 同用 | `-c/--config`, `-t/--target`, `--reset` |
| `biliyt search <query>` | 在本地 SQLite 中跨视频、字幕、动态、专栏与评论全文检索 | `-c/--config`, `-t/--target`, `-k/--kind`, `-n/--limit` |

`sync --only` 可按逗号组合模块：`profile,videos,dynamics,articles,favorites,comments`。`search --kind` 支持：`video,subtitle,dynamic,article,comment`。

### 补齐附件与恢复索引

```bash
# 默认只预览，不写入文件或数据库，不联网
biliyt repair --target bili_example

# 仅重试本地索引，无需重新下载媒体
biliyt repair --target bili_example --only index --apply

# 每个目标最多处理 10 个内容条目；可组合所需修复类型
biliyt repair --target bili_example --only metadata,subtitles,images --limit 10 --apply
```

`repair --only` 支持 `index,metadata,subtitles,images`；`--limit` 限制每个目标的内容条目数，同一条目的多个修复操作共同计为一条。字幕初版补齐独立字幕文件，已有视频的嵌入字幕需求仍为待处理，不重新封装视频。旧归档中无法判断是否曾请求的附件标为 `unknown`，默认不会因此批量联网补抓；显式关闭或平台明确不可用的附件也不循环修复。

`status` 的“部分完成 / 待索引 / 待修复”均为基于已知清单的条目数，同一条目可同时出现在多列，不能相加作为总数；旧归档没有缺项计数也不表示附件已验证完整。媒体保存成功后若附件或索引失败，报告仍保留待修复信息。`sync`、`repair --apply` 与 `db rebuild` 共用写锁，写入重叠时退出码为 `3`；修复仍有失败返回 `2`，配置或参数错误返回 `1`。

此次可靠性更新不新增必填配置或运行时依赖。配置会检查未知字段、日期及三层合并后的约束，详见[配置详解](docs/configuration.md)。

## 归档目录与数据边界

`biliyt` 采用**文件层 + SQLite 索引层双写**架构：媒体与离线可读稿保存在 `downloads/`，结构化元数据与 FTS5 索引写入 `state/biliyt.db`（路径均以相对 `download_dir` 的 POSIX 路径保存，目录可整体迁移、可零网络重建）。

```text
downloads/<platform>/<target_id>/
├── 2026-01-01 - 标题 [BVxxx].mp4 / .info.json / .srt / .jpg / .comments.md / .comments.json
├── .biliyt/manifests/         # 逐条产物清单、附件完成情况与索引状态
├── profile/                  # 最新档案与合集；snapshots/ 保存带稳定 ID 的主页历史
├── dynamics/<日期 - ID>/     # B 站动态正文（dynamic.md/.json）、原图（images/）与精选评论
├── articles/<日期 - cvID>/   # B 站专栏（article.md/.json）、插图（images/）与精选评论
└── favorites/                # 公开收藏夹元数据清单（不下载他人视频）

state/
├── biliyt.db                 # SQLite WAL 归档数据库与 FTS5 trigram 全文索引
├── archives/<target_id>.txt  # yt-dlp 与扩展归档幂等记录
├── failed.jsonl              # 失败重试队列（--retry-failed 自动消费）
├── last_run.json             # 最近一次运行摘要
└── reports/                  # 带 run_id 的正式同步与修复执行 JSON 报告
```

备份需同时保留 `downloads/`（包含隐藏的 `.biliyt/manifests/` 与 `profile/snapshots/`）和 `state/`（尤其 `reports/`、`archives/`、`failed.jsonl`），并保存配置。数据库重建从现存文件恢复内容索引与历史，不会重新生成归档文本或失败队列；旧目录只剩最新主页时，已丢失的历史也无法从该文件还原。全库替换前通过 SQLite backup 留存原库副本，导入或校验出错会保留原库，详见[备份与重建 SOP](docs/sqlite-storage-sop.md)。

## 项目状态、开发与安全提醒

- **当前状态**：`v0.1.0`（早期 Beta，个人持续维护）。测试保持全离线，GitHub Actions CI 覆盖 Ubuntu 与 Windows 下的 Python 3.11 / 3.12 / 3.13 及打包冒烟测试（macOS 基于标准 Python / `yt-dlp` 链路，暂未纳入 CI 实测）。
- **平台兼容性说明**：B 站与 YouTube 的风控策略及前端接口会随时调整；遇到解析报错时优先升级 `yt-dlp`（参见 [故障排查指南](docs/troubleshooting.md)），遇到 B 站 `-352` / `-412` 风控时请降低频率并等待冷却。
- **凭据安全红线**：`config.yaml`、`cookies/`、`state/`、`downloads/` 已列入 `.gitignore`，提交 Issue、PR 或日志前请务必清除 `SESSDATA`、`bili_jct`、Cookie 文件内容与代理账密。安全漏洞请勿公开提交 Issue，请按 [SECURITY.md](SECURITY.md) 私密上报。
- **本地开发与测试**：运行 `uv run pytest -v`（全部离线）与 `uvx ruff check src tests`；贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，AI 编程助手上下文见 [AGENTS.md](AGENTS.md)。

## 延伸文档

- [配置模板（`config.example.yaml`）](config.example.yaml) — 带完整中文注释的配置示例
- [配置详解（`docs/configuration.md`）](docs/configuration.md) — 全字段说明、三层覆盖规则与常用场景模板
- [常见问题与运维排查（`docs/troubleshooting.md`）](docs/troubleshooting.md) — B 站 412/-352、YouTube EJS、Windows Cookie 锁库与 `yt-dlp` 升级
- [B 站全量内容归档 PDR（`docs/bilibili-full-archive-pdr.md`）](docs/bilibili-full-archive-pdr.md) — 接口策略、存储结构与风控熔断设计
- [B 站全量内容归档 SOP（`docs/bilibili-full-archive-sop.md`）](docs/bilibili-full-archive-sop.md) — 首次全量建档、日常增量定时任务与应急处置
- [SQLite 存储与全文检索 SOP（`docs/sqlite-storage-sop.md`）](docs/sqlite-storage-sop.md) — Schema 说明、SQL 速查与热备份/重建
- [版本变更记录（`CHANGELOG.md`）](CHANGELOG.md) · [贡献指南（`CONTRIBUTING.md`）](CONTRIBUTING.md) · [安全政策（`SECURITY.md`）](SECURITY.md)

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
