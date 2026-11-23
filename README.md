# biliyt — Bilibili / YouTube 目标用户批量增量下载工作流

基于 `yt-dlp` 封装的 CLI 工作流，通过一份 `config.yaml` 统一管理 B 站 UP 主与 YouTube 频道的批量增量下载、元数据归档、字幕抓取、分平台代理与 Cookie 鉴权。

## 核心特性

- **统一配置管理**：一份 `config.yaml` 管理多个 B 站 / YouTube 目标，支持 **全局默认 → 平台默认 → 单目标** 三层局部覆盖。
- **两阶段增量引擎**：先轻量探查目标投稿列表并比对本地归档（`state/archives/<id>.txt`），跳过已下载与不符合筛选条件的视频，再逐条下载，大幅降低平台风控风险。
- **精细范围过滤**：支持最近 N 条（`recent_limit`）、单次上限（`max_downloads`）、日期区间（支持 `now-30days`）、时长区间、播放量门槛、标题/标签关键词白黑名单、跳过直播与 YouTube Shorts。
- **完整素材输出**：自动混流为 MP4（可限最高分辨率与编码偏好），同步保存 `.info.json` 元数据、封面图（默认转 `.jpg`）、字幕（B 站 CC / YouTube 人工与自动字幕，默认转 `.srt`），并可选 B 站 XML 弹幕与 YouTube SponsorBlock 去广告。
- **分平台网络与鉴权**：支持 YouTube 走本地 HTTP/SOCKS 代理 + B 站直连；支持浏览器直读 Cookie 与 `cookies.txt` 文件。
- **YouTube EJS 自动适配**：自动检测本机 `deno` 或 `node`（Node 20+）并注入 `js_runtimes`，开箱兼容新版 yt-dlp YouTube 解析。
- **失败队列与断点重试**：单条下载失败自动写入 `state/failed.jsonl`，随时通过 `--retry-failed` 精准补下。

## 目录结构

```text
bli-youtube/
├── biliyt.cmd                 # Windows 统一命令行与定时任务入口
├── config.example.yaml        # 带注释的完整配置模板
├── pyproject.toml             # 项目与依赖定义
├── docs/
│   ├── configuration.md       # config.yaml 全字段配置详解
│   └── troubleshooting.md     # B 站 / YouTube 常见问题与升级运维
├── src/biliyt/
│   ├── cli.py                 # Typer + Rich CLI（init/check/probe/sync/status）
│   ├── config.py              # Pydantic v2 配置模型与三层覆盖解析
│   ├── doctor.py              # 依赖、配置、Cookie 与代理连通性体检
│   ├── downloader.py          # 两阶段 probe / sync / retry_failed 引擎
│   ├── options.py             # yt-dlp YoutubeDL 参数构造与过滤器
│   ├── platform.py            # 平台识别与目标 URL 规范化
│   └── state.py               # 归档记录、失败队列与运行报告持久化
└── tests/                     # 单元测试套件（pytest）
```

## 快速开始

### 1. 环境准备

- **Python 3.11+**（推荐 `uv` 管理虚拟环境）
- **FFmpeg / FFprobe**（加入系统 `PATH`，用于音视频混流与字幕/封面转换）
- **Deno 2.3+ 或 Node.js 20+**（用于 YouTube EJS 解析）

安装依赖：

```powershell
uv sync --python-preference only-system --python 3.12
```

### 2. 环境体检

```powershell
.\biliyt.cmd check
```

### 3. 初始化配置

```powershell
.\biliyt.cmd init
```

编辑生成的 `config.yaml`（已被 `.gitignore` 忽略，不会误提交 Cookie 与代理）：
- 按需设置 `platforms.youtube.proxy`（例如 `http://127.0.0.1:7890`），B 站保持 `null` 直连
- 按需配置 `cookies_file` 或 `cookies_from_browser`
- 在 `targets` 中添加目标 UP 主 UID / YouTube `@handle`，并将 `enabled` 改为 `true`

### 4. 探查与增量同步

```powershell
# 1) 仅探查目标列表与过滤统计，不下载
.\biliyt.cmd probe

# 2) Dry-run 演练（预览将要下载的具体条目）
.\biliyt.cmd sync --dry-run

# 3) 正式增量同步全部启用的目标
.\biliyt.cmd sync

# 4) 仅同步指定目标 / 指定平台 / 临时限制条数
.\biliyt.cmd sync --target bili_demo
.\biliyt.cmd sync --platform youtube --limit 5

# 5) 仅重试历史失败队列
.\biliyt.cmd sync --retry-failed

# 6) 查看累计归档数、失败队列与最近运行报告
.\biliyt.cmd status
```

## 命令速查

| 命令 | 说明 | 常用参数 |
| :--- | :--- | :--- |
| `biliyt init` | 复制生成 `config.yaml` 与 `cookies/` 目录 | `-c/--config`, `-f/--force` |
| `biliyt check` | 检查 Python/yt-dlp/FFmpeg/JS Runtime/配置/代理/Cookie | `-c/--config` |
| `biliyt probe` | 轻量拉取列表并统计已归档/跳过/待下载数量 | `-t/--target`, `-p/--platform`, `-n/--limit`, `--show` |
| `biliyt sync` | 执行增量下载或失败重试，并写入 JSON 运行报告 | `-t/--target`, `-p/--platform`, `-n/--limit`, `--dry-run`, `--retry-failed` |
| `biliyt status` | 展示各目标累计归档、待重试失败数与上次运行摘要 | `-c/--config` |

## 接入 Windows 定时任务

在 Windows「任务计划程序」中新建任务：
- **程序或脚本**：`C:\WorkSpace\bli-youtube\biliyt.cmd`
- **添加参数**：`sync`
- **起始于**：`C:\WorkSpace\bli-youtube`

## 运行测试

```powershell
$env:PYTHONPATH = "C:\WorkSpace\bli-youtube\.venv\Lib\site-packages;C:\WorkSpace\bli-youtube\src"
python -m pytest -v
```

## 延伸文档

- [配置详解（docs/configuration.md）](docs/configuration.md)
- [常见问题与运维排查（docs/troubleshooting.md）](docs/troubleshooting.md)
