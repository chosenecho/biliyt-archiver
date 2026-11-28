# Changelog

本项目版本变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循 [语义化版本（SemVer）](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- `biliyt repair`：默认只读预览，`--apply` 执行，支持 `--only index,metadata,subtitles,images` 和每目标按内容条目计数的 `--limit`；整合失败队列与附件缺项，重试本地索引或补齐独立字幕、元数据、图片。
- 每目标 `.biliyt/manifests/` 逐条产物清单，记录实际相对路径、附件完成状态与索引状态；`status` 和运行报告增加部分完成、待索引、待修复条目数，计数可重叠。
- 主页历史追加保存到 `profile/snapshots/`；快照采用稳定 `snapshot_id`，运行报告包含 `run_id` 且文件名防同秒覆盖。
- 数据库 v2 按版本迁移，为主页快照与运行记录增加稳定标识及唯一索引，离线导入相同历史保持幂等。
- 开源治理与发布基建：`LICENSE`（MIT）、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、`AGENTS.md`、GitHub Issue/PR 模板、Dependabot 配置与 GitHub Actions CI/Release 工作流。
- `biliyt init` 支持在打包安装（wheel / `uv tool`）场景下自动回退读取内置的 `config.example.yaml` 模板。
- 新增静态 SVG 品牌标识 [docs/assets/biliyt-logo.svg](docs/assets/biliyt-logo.svg)。
- 强化增量抓取能力：
  - 视频探查支持 `filters.early_stop_after`（默认 `3`）配合 `yt-dlp` `lazy_playlist` 惰性早停，B 站专栏 `sync_articles` 补齐连续已归档早停；
  - 拆分 `--limit` 与扫描窗口语义，支持 `--full --limit N` 全量扫描并按批定量补齐历史未归档视频、动态、专栏与评论；
  - 新增 `archive.comments.refresh_recent_days` 近期评论自动增量刷新窗口（含 12 小时冷却间隔），收藏夹 `sync_favorites` 新增基于数量/修改时间/标题指纹的未变更跳过；
  - 新增跨平台进程级同步锁 `state/.sync.lock`（Windows `msvcrt` / POSIX `fcntl`），防止定时任务与手动 `biliyt sync` 重叠执行；
  - 视频增量探查支持基于 `playlist_count` / `n_entries` 检测 B 站多 P 视频后续追加分 P。

### Changed
- `db rebuild --target` 保持增量导入，禁止与全库 `--reset` 组合；全库重建先通过 SQLite backup 保留原库并导出独有历史，在同目录临时库完成导入、完整性及计数校验后再处理 WAL、关闭连接并替换。导入或替换失败保留原库并报告错误及已有备份路径。
- `sync`、`repair --apply`、`db rebuild` 共用写锁，统一提示其他写入正在进行；锁冲突退出码为 `3`，部分处理失败为 `2`，配置或参数错误为 `1`。
- 备份说明明确涵盖 `downloads/`（含隐藏产物清单与主页历史）和 `state/`（含报告、归档文本、失败队列）；重建恢复现存内容和运行历史，不承诺恢复旧归档中已经丢失的信息。
- 本轮不新增必填配置或运行时依赖；旧归档 `unknown` 附件不自动批量联网补抓，字幕修复初版仅补独立字幕文件，不重封装已有视频。
- 重写 `README.md` 信息架构，以跨平台全局 `biliyt` CLI（`uv tool install`）为首选入口，补充与原生 `yt-dlp` 的差异化对比、非目标边界、真实 CLI 输出示例与诚实的项目状态声明。
- 在 `config.example.yaml` 与 `docs/configuration.md` 中补齐 `paths.temp_dir` 与 `rate_limit.concurrency` 配置文档。
- 清理文档中的本机绝对路径引用，完善 `pyproject.toml` 发布元数据与 Ruff 规则。

### Removed
- 移除仅限 Windows 源码目录的 `biliyt.cmd` 批处理包装脚本，统一使用跨平台 `biliyt` CLI 入口（`[project.scripts]` / `python -m biliyt`）。

### Fixed
- 局部配置只合并显式字段，延迟依赖默认值的跨字段规范化；最终合并后正式校验并附目标 ID 和配置路径，保留 `null` 语义，检查未知字段、日期格式与起止顺序。此前静默忽略的拼错字段会开始报错。
- 视频索引消费后处理后的实际产物路径，覆盖自定义文件名、分 P、格式转换与临时目录搬运；索引失败与字幕降级以结构化警告和待修复状态保留。
- 状态、报告、元数据及清单等文件采用同目录临时写入后原子替换，降低中断留下截断文件的概率；断电持久性仍取决于操作系统与文件系统。
- 修复 `src/biliyt/bilibili/client.py` 中多余的 f-string（Ruff `F541`）。
- 修复并发视频下载（`rate_limit.concurrency > 1`）时多个 worker 竞争写入同一 `download_archive` 文件及 `newly_archived` 跨线程误判问题，改为每任务独立临时归档文件并持 `StateStore` 锁原子合并。
- 修复 `StateStore` 命名空间归档读写未加锁及 `retry_failed_item` 重试失败评论后未同步写入 SQLite 数据库的问题。

## [0.1.0] - 2026-11-24

### Added
- **Bilibili / YouTube 增量视频工作流**：基于 `yt-dlp` 封装的 Typer + Rich CLI（`init`、`check`、`probe`、`sync`、`status`），支持 `全局 → 平台 → 单目标` 三层局部配置覆盖、两阶段探查与增量归档、丰富过滤规则、分平台代理与 Cookie 鉴权、YouTube EJS 运行时自动检测、失败队列与 `--retry-failed` 断点重试。
- **B 站 UP 主全量内容归档**：内置 WBI 动态签名、随机节流与 `-352`/`-412` 风控熔断的 `curl_cffi` Web API 客户端，支持主页档案与合集、动态（含转发与原图本地化）、专栏（兼容旧 `cv` HTML 与新版 Opus）、公开收藏夹清单及精选评论（置顶 + UP 主回复 + 热门前 N 条），双格式落盘（Markdown + 原始 JSON），支持 `--only` 分模块同步、`--full` 全量回溯与 `--refresh-comments`。
- **SQLite 结构化存储与中文全文检索**：基于标准库 `sqlite3`（WAL 模式 + FTS5 trigram 索引）的本地归档数据库 `state/biliyt.db`，新增 `biliyt db stats`、`biliyt db rebuild`（离线零网络重建）与 `biliyt search` 跨视频/字幕/动态/专栏/评论检索。
- **并行视频下载与临时目录支持**：新增 `rate_limit.concurrency`（1–8 并发）、`paths.temp_dir` 临时混流目录与线程安全的 `StateStore` 状态写入。

### Changed
- 评论载体扫描顺序调整为 `videos → articles → dynamics`，减少低价值重复请求。

[Unreleased]: https://github.com/chosenecho/biliyt-archiver/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chosenecho/biliyt-archiver/releases/tag/v0.1.0
