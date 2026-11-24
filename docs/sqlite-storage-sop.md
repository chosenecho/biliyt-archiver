# SOP：biliyt SQLite 结构化存储与全文检索设计实施手册

- **适用版本**：`biliyt v0.1.0+`（Python 3.11+ 标准库 `sqlite3`，零新增第三方依赖）
- **数据库路径**：`<state_dir>/biliyt.db`（默认 `state/biliyt.db`，已在 `.gitignore` 中被 `state/` 忽略）
- **核心架构原则**：**双写 + 可重建（Hybrid Architecture）**——大体积二进制与离线可读稿留在 `downloads/`，结构化元数据、字幕文本、评论树、运行日志与 FTS5 全文索引存入 SQLite。

---

## 1. 存储分层与职责边界

| 层级 | 路径 | 存放内容 | 设计约束 |
| :--- | :--- | :--- | :--- |
| **文件层（Source of Truth for Media）** | `downloads/` | `.mp4` 视频、`.jpg/.png` 封面与原图、`.srt` 字幕原文件、`.danmaku.xml`、`.md` 离线可读稿、原始 `.json` | **严禁**将视频/图片二进制作为 BLOB 存入 SQLite；文件层保证脱离程序也能直接浏览播放 |
| **数据库层（Structured Index & Search）** | `state/biliyt.db` | UP 主档案与历史快照、视频元数据+字幕纯文本、动态、专栏、精选评论、收藏夹清单、同步历史、FTS5 跨模态倒排索引 | 所有路径均存**相对 `download_dir` 的 POSIX 路径**，迁移目录不失效；支持从 `downloads/` 随时无损重建 |
| **归档兼容层** | `state/archives/*.txt` | 保留给 `yt-dlp` 自身的 `--download-archive` 与轻量幂等校验 | 过渡期继续保留，与 SQLite 同步更新，零破坏现有逻辑 |

---

## 2. 数据库 Schema 规范（v1）

### 2.1 连接与运行参数（必须强制开启）
每次打开 `biliyt.db` 必须执行：
```sql
PRAGMA journal_mode = WAL;          -- 读写并发友好，防断电损坏
PRAGMA synchronous = NORMAL;        -- WAL 模式下兼顾性能与安全
PRAGMA foreign_keys = ON;           -- 强外键约束
PRAGMA busy_timeout = 5000;         -- 5 秒锁等待，防止并发任务瞬间报错
```

### 2.2 核心表设计（DDL 规范）

1. **`schema_migrations`（版本迁移记录）**
   - `version INTEGER PRIMARY KEY`, `applied_at TEXT NOT NULL`, `description TEXT`
2. **`profiles`（UP 主最新档案）& `profile_snapshots`（历史趋势快照）**
   - `profiles`: `target_id TEXT PRIMARY KEY`, `platform TEXT`, `uid TEXT`, `name TEXT`, `sign TEXT`, `level INTEGER`, `official_title TEXT`, `follower INTEGER`, `following INTEGER`, `archive_views INTEGER`, `likes INTEGER`, `avatar_relpath TEXT`, `banner_relpath TEXT`, `collections_json TEXT`, `raw_json TEXT`, `updated_at TEXT`
   - `profile_snapshots`: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `target_id TEXT`, `fetched_at TEXT`, `follower INTEGER`, `following INTEGER`, `archive_views INTEGER`, `likes INTEGER`
3. **`videos`（投稿视频 + 字幕文本）**
   - `id INTEGER PRIMARY KEY AUTOINCREMENT`
   - `target_id TEXT NOT NULL`, `platform TEXT NOT NULL`, `video_id TEXT NOT NULL`（B 站 BV / YouTube ID）, `aid TEXT`（B 站数字 AV 号）
   - `title TEXT`, `description TEXT`, `uploader TEXT`, `upload_date TEXT`（`YYYY-MM-DD`）, `duration REAL`, `view_count INTEGER`, `like_count INTEGER`, `comment_count INTEGER`, `webpage_url TEXT`
   - `subtitle_text TEXT`（从 `.srt` 清洗去时间轴后的纯文本，供 FTS5 检索与语料导出）
   - `video_relpath TEXT`, `info_json_relpath TEXT`, `subtitle_relpath TEXT`, `thumbnail_relpath TEXT`
   - `raw_json TEXT`, `archived_at TEXT`, `updated_at TEXT`
   - 唯一约束：`UNIQUE(target_id, video_id)`
4. **`dynamics`（动态全量）**
   - `target_id TEXT NOT NULL`, `dynamic_id TEXT NOT NULL`, `dynamic_type TEXT`, `published_at TEXT`, `pub_ts INTEGER`
   - `author_name TEXT`, `text_content TEXT`, `markdown_content TEXT`, `is_forward INTEGER`, `orig_dynamic_id TEXT`, `orig_text TEXT`
   - `like_count INTEGER`, `comment_count INTEGER`, `forward_count INTEGER`
   - `comment_oid TEXT`, `comment_type INTEGER`
   - `images_json TEXT`（本地相对路径数组）, `folder_relpath TEXT`, `raw_json TEXT`, `archived_at TEXT`
   - 唯一约束：`UNIQUE(target_id, dynamic_id)`
5. **`articles`（专栏文章）**
   - `target_id TEXT NOT NULL`, `cv_id TEXT NOT NULL`, `opus_id TEXT`, `title TEXT`, `category TEXT`, `published_at TEXT`, `pub_ts INTEGER`
   - `summary TEXT`, `text_content TEXT`, `markdown_content TEXT`, `words INTEGER`
   - `view_count INTEGER`, `like_count INTEGER`, `reply_count INTEGER`
   - `images_json TEXT`, `folder_relpath TEXT`, `raw_json TEXT`, `archived_at TEXT`
   - 唯一约束：`UNIQUE(target_id, cv_id)`
6. **`comments`（精选评论：置顶 + UP 主回复 + 热门前 N）**
   - `target_id TEXT NOT NULL`, `carrier_kind TEXT NOT NULL`（`video | dynamic | article`）, `oid TEXT NOT NULL`, `rtype INTEGER NOT NULL`, `rpid TEXT NOT NULL`
   - `root_rpid TEXT`（主评论为 `NULL` 或自身，楼中楼指向主评论 `rpid`）
   - `author_mid TEXT`, `author_name TEXT`, `is_up INTEGER`（`0/1`）, `is_pinned INTEGER`（`0/1`）, `has_up_reply INTEGER`（`0/1`）
   - `content TEXT`, `like_count INTEGER`, `ctime INTEGER`, `published_at TEXT`, `fetched_at TEXT`
   - 唯一约束：`UNIQUE(target_id, carrier_kind, oid, rpid)`
   - 常用索引：`INDEX idx_comments_carrier(target_id, carrier_kind, oid)`、`INDEX idx_comments_up(target_id, is_up, is_pinned)`
7. **`favorites` & `favorite_items`（公开收藏夹清单）**
   - `favorites`: `target_id TEXT`, `media_id TEXT`, `title TEXT`, `media_count INTEGER`, `updated_at TEXT`, `UNIQUE(target_id, media_id)`
   - `favorite_items`: `target_id TEXT`, `media_id TEXT`, `bvid TEXT`, `title TEXT`, `upper_name TEXT`, `duration INTEGER`, `fav_time TEXT`, `url TEXT`, `UNIQUE(target_id, media_id, bvid)`
8. **`sync_runs`（同步运行历史）**
   - `id INTEGER PRIMARY KEY AUTOINCREMENT`, `mode TEXT`, `started_at TEXT`, `finished_at TEXT`, `summary_json TEXT`
9. **`search_fts`（跨模态全文检索虚表）**
   - 使用 SQLite FTS5 `trigram` 分词器（原生支持任意 ≥ 3 字符的中文子串精确匹配，无需外装中文分词库）：
     ```sql
     CREATE VIRTUAL TABLE search_fts USING fts5(
         target_id UNINDEXED,
         kind UNINDEXED,      -- 'video' | 'subtitle' | 'dynamic' | 'article' | 'comment'
         item_id UNINDEXED,   -- BV / dynamic_id / cv_id / rpid
         title,
         body,
         published_at UNINDEXED,
         source_url UNINDEXED,
         tokenize = 'trigram'
     );
     ```
   - **短词兜底策略**：当用户输入关键词长度 `< 3` 个字符（如搜 `"哲学"` 或 `"AI"`）时，查询层自动回退为 `WHERE title LIKE '%词%' OR body LIKE '%词%'`，保证 1–2 字短词同样 100% 命中。

---

## 3. 开发落地 SOP（Dev SOP）

按 4 个小里程碑依次落地，每步必须通过 `pytest` 回归：

### M1：数据层底座 (`src/biliyt/db.py`)
1. 实现 `ArchiveDB(db_path: Path)`：
   - 自动初始化建表、WAL 与 `schema_migrations`。
   - 幂等 `upsert_*` 接口：`upsert_profile`、`upsert_video`、`upsert_dynamic`、`upsert_article`、`upsert_comments`、`upsert_favorite_folder`、`record_sync_run`。
   - 同步维护 `search_fts`（同一事务内先删旧 `(target_id, kind, item_id)` 再插新文本，保证无脏重复）。
   - SRT 清洗工具 `extract_srt_plain_text(srt_path)`（去掉序号、时间码、空行与连续重复行）。
   - `search(query, target_id=None, kinds=None, limit=20)`（≥ 3 字走 `MATCH`，< 3 字走 `LIKE` 兜底，返回高亮片段 `snippet`）。
   - `stats()`（返回各表行数与数据库体积）。
2. 新增单元测试 `tests/test_db.py`：覆盖建表、WAL、UPSERT 幂等、中文 2 字/4 字全文搜索、事务回滚。

### M2：同步链路双写集成
1. **`SyncEngine` (`downloader.py`)**：
   - 单条视频下载完成后，自动解析刚生成的 `.info.json` 与 `.srt` 字幕，调用 `db.upsert_video(...)`。
   - 每次 `sync` 结束后记录 `db.record_sync_run(...)`。
2. **`BilibiliArchiver` (`bilibili/archiver.py`)**：
   - `sync_profile` / `sync_dynamics` / `sync_articles` / `sync_favorites` / `sync_comments` 在写完本地文件后，立即同步写入 `ArchiveDB`。
   - 数据库写入失败不吞静默错误，记录到模块 warnings/errors 以便排查。

### M3：离线重建工具 (`rebuild_from_disk`)
1. 实现 `rebuild_target_from_disk(db, target)`：
   - 扫描 `target.download_dir` 下所有：
     - `profile/profile.json` + `collections.json`
     - `*.info.json`（匹配同前缀 `.srt` 与 `.mp4`/`.jpg`）
     - `dynamics/*/dynamic.json` + `dynamic.md`
     - `articles/*/article.json` + `article.md`
     - `favorites/fav_*.json`
     - 所有 `*.comments.json` 与 `*/comments.json`
   - 全程幂等 `UPSERT`，不发任何网络请求。
2. 实测验证：对已有下载目录执行 `biliyt db rebuild`，验证视频（含字幕）、档案、动态、专栏与评论全部快速入库。

### M4：CLI 命令与文档
在 `cli.py` 新增两类命令：
- `biliyt db stats`：展示数据库路径、体积及各表记录数
- `biliyt db rebuild [--target <id>] [--reset]`：从本地 `downloads/` 全量重建（`--reset` 先清空重来）
- `biliyt search <query> [-t <target>] [-k video,subtitle,dynamic,article,comment] [-n 20]`：终端 Rich 表格/片段展示命中结果与原链接

---

## 4. 首次建库与日常使用 SOP（Ops SOP）

### Step 4.1 已有下载目录一键入库（首次启用）
```bash
# 1) 从现有 downloads/ 无损重建数据库（零网络请求）
biliyt db rebuild

# 2) 查看入库统计
biliyt db stats

# 3) 冒烟检索测试（验证中文短词与长词、视频字幕与专栏都能搜到）
biliyt search "关键词" --target bili_example
biliyt search "字幕词" --target bili_example --kind subtitle,comment
```

### Step 4.2 后续全量/日常同步
日常命令**完全不变**，同步时自动双写文件与 `state/biliyt.db`：
```bash
biliyt sync
```

### Step 4.3 常用 SQL 速查（可用 DB Browser for SQLite 或 Python 直接查询）

- **查看 UP 主「视频 + 动态 + 专栏」统一时间轴（最近 30 条）**：
  ```sql
  SELECT published_at, 'video' AS kind, video_id AS id, title FROM videos WHERE target_id='bili_example'
  UNION ALL
  SELECT published_at, 'dynamic' AS kind, dynamic_id AS id, substr(text_content, 1, 60) FROM dynamics WHERE target_id='bili_example'
  UNION ALL
  SELECT published_at, 'article' AS kind, cv_id AS id, title FROM articles WHERE target_id='bili_example'
  ORDER BY published_at DESC LIMIT 30;
  ```
- **导出该 UP 主所有「置顶评论 + UP 主本人发言」**：
  ```sql
  SELECT carrier_kind, oid, published_at, is_pinned, like_count, content
  FROM comments
  WHERE target_id='bili_example' AND (is_pinned = 1 OR is_up = 1)
  ORDER BY published_at DESC;
  ```
- **查出已下载但尚未抓取评论的视频**：
  ```sql
  SELECT v.video_id, v.title, v.upload_date
  FROM videos v
  LEFT JOIN comments c ON c.target_id = v.target_id AND c.carrier_kind = 'video' AND c.oid = v.aid
  WHERE v.target_id = 'bili_example' AND c.rpid IS NULL;
  ```

---

## 5. 备份、恢复与维护 SOP（Maintenance & DR SOP）

### 5.1 安全热备份（严禁运行中直接复制 `.db` 单文件）
WAL 模式下数据可能分散在 `biliyt.db`、`biliyt.db-wal`、`biliyt.db-shm` 三个文件中。备份必须使用一致性快照：
```powershell
# 推荐：使用 SQLite 在线备份接口生成单一紧凑副本
python -c "import sqlite3; c=sqlite3.connect('state/biliyt.db'); c.execute(\"VACUUM INTO 'state/biliyt_backup.db'\"); c.close()"
```
建议每月备份一次 `state/biliyt_backup.db` + `config.yaml` + `state/archives/`。

### 5.2 数据库损坏或 Schema 异常的一键恢复
因为文件层保留了完整的原始 `.json` 与 `.srt`，SQLite 属于**可完全再生资产**：
```bash
# 重置并从 downloads/ 全量无损重建
biliyt db rebuild --reset
biliyt db stats
```

### 5.3 定期空间整理（每季度或大批量建档后执行 1 次）
```powershell
python -c "import sqlite3; c=sqlite3.connect('state/biliyt.db'); c.execute('PRAGMA optimize'); c.execute('VACUUM'); c.close()"
```

### 5.4 Schema 升级纪律
- 任何表结构变更只能通过新增 `schema_migrations` 版本号（如 `v2`）的幂等 `ALTER TABLE` / 迁移函数完成，**禁止**手改线上库结构。
- 每次升级前先用 5.1 节生成 `biliyt_backup.db` 快照。
