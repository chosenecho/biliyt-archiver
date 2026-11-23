"""SQLite structured archive store and FTS5 full-text search for biliyt."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bilibili.archiver import bv_to_av
from .bilibili.render import format_unix_date, format_unix_ts
from .config import ResolvedTarget
from .platform import Platform, extract_bilibili_uid

SCHEMA_VERSION = 1

_SRT_INDEX_RE = re.compile(r"^\d+$")
_SRT_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_SYNTAX_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]+)\]\([^)]*\)|[#>*`~_-]{2,}")

_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        target_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        uid TEXT,
        name TEXT,
        sign TEXT,
        level INTEGER,
        official_title TEXT,
        follower INTEGER,
        following INTEGER,
        archive_views INTEGER,
        likes INTEGER,
        avatar_relpath TEXT,
        banner_relpath TEXT,
        collections_json TEXT,
        raw_json TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        follower INTEGER,
        following INTEGER,
        archive_views INTEGER,
        likes INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_snapshots_target
    ON profile_snapshots(target_id, fetched_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        video_id TEXT NOT NULL,
        aid TEXT,
        title TEXT,
        description TEXT,
        uploader TEXT,
        upload_date TEXT,
        duration REAL,
        view_count INTEGER,
        like_count INTEGER,
        comment_count INTEGER,
        webpage_url TEXT,
        subtitle_text TEXT,
        video_relpath TEXT,
        info_json_relpath TEXT,
        subtitle_relpath TEXT,
        thumbnail_relpath TEXT,
        raw_json TEXT,
        archived_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(target_id, video_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_videos_target_date
    ON videos(target_id, upload_date DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS dynamics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        dynamic_id TEXT NOT NULL,
        dynamic_type TEXT,
        published_at TEXT,
        pub_ts INTEGER,
        author_name TEXT,
        text_content TEXT,
        markdown_content TEXT,
        is_forward INTEGER NOT NULL DEFAULT 0,
        orig_dynamic_id TEXT,
        orig_text TEXT,
        like_count INTEGER,
        comment_count INTEGER,
        forward_count INTEGER,
        comment_oid TEXT,
        comment_type INTEGER,
        images_json TEXT,
        folder_relpath TEXT,
        raw_json TEXT,
        archived_at TEXT NOT NULL,
        UNIQUE(target_id, dynamic_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dynamics_target_ts
    ON dynamics(target_id, pub_ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        cv_id TEXT NOT NULL,
        opus_id TEXT,
        title TEXT,
        category TEXT,
        published_at TEXT,
        pub_ts INTEGER,
        summary TEXT,
        text_content TEXT,
        markdown_content TEXT,
        words INTEGER,
        view_count INTEGER,
        like_count INTEGER,
        reply_count INTEGER,
        images_json TEXT,
        folder_relpath TEXT,
        raw_json TEXT,
        archived_at TEXT NOT NULL,
        UNIQUE(target_id, cv_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_articles_target_ts
    ON articles(target_id, pub_ts DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        carrier_kind TEXT NOT NULL,
        oid TEXT NOT NULL,
        rtype INTEGER NOT NULL,
        rpid TEXT NOT NULL,
        root_rpid TEXT,
        author_mid TEXT,
        author_name TEXT,
        is_up INTEGER NOT NULL DEFAULT 0,
        is_pinned INTEGER NOT NULL DEFAULT 0,
        has_up_reply INTEGER NOT NULL DEFAULT 0,
        content TEXT,
        like_count INTEGER,
        ctime INTEGER,
        published_at TEXT,
        fetched_at TEXT NOT NULL,
        UNIQUE(target_id, carrier_kind, oid, rpid)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_comments_carrier
    ON comments(target_id, carrier_kind, oid)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_comments_up
    ON comments(target_id, is_up, is_pinned)
    """,
    """
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        media_id TEXT NOT NULL,
        title TEXT,
        media_count INTEGER,
        updated_at TEXT NOT NULL,
        UNIQUE(target_id, media_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS favorite_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL,
        media_id TEXT NOT NULL,
        bvid TEXT NOT NULL,
        title TEXT,
        upper_name TEXT,
        duration INTEGER,
        fav_time TEXT,
        url TEXT,
        UNIQUE(target_id, media_id, bvid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
        target_id UNINDEXED,
        kind UNINDEXED,
        item_id UNINDEXED,
        display_title UNINDEXED,
        title,
        body,
        published_at UNINDEXED,
        source_url UNINDEXED,
        tokenize = 'trigram'
    )
    """,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rel_posix(path: Path | None, base_dir: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _normalize_upload_date(raw: str | None) -> str:
    if not raw:
        return "unknown"
    cleaned = str(raw).strip()
    if len(cleaned) == 8 and cleaned.isdigit():
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    return cleaned


def extract_srt_plain_text(srt_path: Path) -> str:
    """Strip SRT indices, timestamps, and duplicate lines into clean searchable plain text."""
    if not srt_path.exists():
        return ""
    lines: list[str] = []
    last_line = ""
    for raw_line in srt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or _SRT_INDEX_RE.match(line) or _SRT_TIME_RE.match(line):
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        cleaned = _HTML_TAG_RE.sub("", line).strip()
        if not cleaned or cleaned == last_line:
            continue
        lines.append(cleaned)
        last_line = cleaned
    return "\n".join(lines)


def _markdown_to_plain_text(md: str) -> str:
    if not md:
        return ""
    # Replace markdown links with link label, drop image links
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_dynamic_plain_text(item: dict[str, Any]) -> tuple[str, str | None, str]:
    """Return `(main_text, orig_dynamic_id, orig_text)` from raw dynamic JSON."""
    def _one(obj: dict[str, Any]) -> str:
        modules = obj.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        parts: list[str] = []
        desc = (dyn.get("desc") or {}).get("text")
        if desc:
            parts.append(str(desc).strip())
        major = dyn.get("major") or {}
        opus = major.get("opus") or {}
        if opus.get("title"):
            parts.append(str(opus["title"]).strip())
        opus_summary = (opus.get("summary") or {}).get("text")
        if opus_summary:
            parts.append(str(opus_summary).strip())
        archive = major.get("archive") or {}
        if archive.get("title"):
            parts.append(str(archive["title"]).strip())
        article = major.get("article") or {}
        if article.get("title"):
            parts.append(str(article["title"]).strip())
        return "\n".join(p for p in parts if p)

    main_text = _one(item)
    orig = item.get("orig")
    if isinstance(orig, dict):
        orig_id = str(orig.get("id_str") or "") or None
        orig_text = _one(orig)
        return main_text, orig_id, orig_text
    return main_text, None, ""


def _build_like_snippet(text: str, query: str, radius: int = 45) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    idx = cleaned.lower().find(query.lower())
    if idx < 0:
        return cleaned[: radius * 2] + ("..." if len(cleaned) > radius * 2 else "")
    start = max(0, idx - radius)
    end = min(len(cleaned), idx + len(query) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(cleaned) else ""
    matched = cleaned[idx : idx + len(query)]
    return f"{prefix}{cleaned[start:idx]}[{matched}]{cleaned[idx + len(query):end]}{suffix}"


@dataclass(frozen=True)
class SearchHit:
    target_id: str
    kind: str
    item_id: str
    title: str
    snippet: str
    published_at: str
    source_url: str


class ArchiveDB:
    """SQLite database manager for structured archive storage and FTS5 full-text search."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._ensure_schema()

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def _ensure_schema(self) -> None:
        with self._conn:
            for stmt in _DDL_STATEMENTS:
                self._conn.execute(stmt)
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            current = int(row["v"]) if row and row["v"] is not None else 0
            if current < SCHEMA_VERSION:
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, _utc_now_iso(), "Initial structured archive + FTS5 trigram schema"),
                )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> ArchiveDB:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def reset(self) -> None:
        """Drop all data tables and recreate schema from scratch."""
        tables = (
            "search_fts",
            "profile_snapshots",
            "profiles",
            "videos",
            "dynamics",
            "articles",
            "comments",
            "favorite_items",
            "favorites",
            "sync_runs",
            "schema_migrations",
        )
        with self._conn:
            for tbl in tables:
                self._conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        self._ensure_schema()

    def _upsert_fts(
        self,
        *,
        target_id: str,
        kind: str,
        item_id: str,
        title: str,
        body: str,
        published_at: str,
        source_url: str,
        display_title: str | None = None,
    ) -> None:
        self._conn.execute(
            "DELETE FROM search_fts WHERE target_id = ? AND kind = ? AND item_id = ?",
            (target_id, kind, item_id),
        )
        if not (title.strip() or body.strip()):
            return
        self._conn.execute(
            """
            INSERT INTO search_fts(
                target_id, kind, item_id, display_title, title, body, published_at, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                kind,
                item_id,
                display_title if display_title is not None else title,
                title,
                body,
                published_at,
                source_url,
            ),
        )

    # ------------------------------------------------------------------
    # Upserts
    # ------------------------------------------------------------------

    def upsert_profile(
        self,
        *,
        target_id: str,
        platform: str,
        uid: int | str,
        user_info: dict[str, Any],
        relation_stat: dict[str, Any] | None = None,
        upstat: dict[str, Any] | None = None,
        collections: list[dict[str, Any]] | None = None,
        avatar_relpath: str | None = None,
        banner_relpath: str | None = None,
        fetched_at: str | None = None,
    ) -> None:
        ts = fetched_at or _utc_now_iso()
        official_title = str((user_info.get("official") or {}).get("title") or "")
        follower = (relation_stat or {}).get("follower")
        following = (relation_stat or {}).get("following")
        archive_views = ((upstat or {}).get("archive") or {}).get("view")
        likes = (upstat or {}).get("likes")

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO profiles(
                    target_id, platform, uid, name, sign, level, official_title,
                    follower, following, archive_views, likes,
                    avatar_relpath, banner_relpath, collections_json, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    platform=excluded.platform,
                    uid=excluded.uid,
                    name=excluded.name,
                    sign=excluded.sign,
                    level=excluded.level,
                    official_title=excluded.official_title,
                    follower=excluded.follower,
                    following=excluded.following,
                    archive_views=excluded.archive_views,
                    likes=excluded.likes,
                    avatar_relpath=COALESCE(excluded.avatar_relpath, profiles.avatar_relpath),
                    banner_relpath=COALESCE(excluded.banner_relpath, profiles.banner_relpath),
                    collections_json=excluded.collections_json,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    target_id,
                    platform,
                    str(uid),
                    str(user_info.get("name") or ""),
                    str(user_info.get("sign") or ""),
                    user_info.get("level"),
                    official_title,
                    follower,
                    following,
                    archive_views,
                    likes,
                    avatar_relpath,
                    banner_relpath,
                    json.dumps(collections or [], ensure_ascii=False),
                    json.dumps(user_info, ensure_ascii=False),
                    ts,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO profile_snapshots(target_id, fetched_at, follower, following, archive_views, likes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (target_id, ts, follower, following, archive_views, likes),
            )

    def upsert_video(
        self,
        *,
        target_id: str,
        platform: str,
        info: dict[str, Any],
        subtitle_text: str = "",
        video_relpath: str | None = None,
        info_json_relpath: str | None = None,
        subtitle_relpath: str | None = None,
        thumbnail_relpath: str | None = None,
    ) -> None:
        video_id = str(info.get("id") or info.get("display_id") or "")
        if not video_id:
            return
        aid = info.get("aid")
        if not aid and platform == Platform.BILIBILI.value:
            aid = bv_to_av(video_id)
        title = str(info.get("title") or video_id)
        desc = str(info.get("description") or "")
        upload_date = _normalize_upload_date(info.get("upload_date"))
        url = str(info.get("webpage_url") or "")
        if not url and platform == Platform.BILIBILI.value:
            url = f"https://www.bilibili.com/video/{video_id}"
        now = _utc_now_iso()

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO videos(
                    target_id, platform, video_id, aid, title, description, uploader,
                    upload_date, duration, view_count, like_count, comment_count,
                    webpage_url, subtitle_text, video_relpath, info_json_relpath,
                    subtitle_relpath, thumbnail_relpath, raw_json, archived_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, video_id) DO UPDATE SET
                    aid=COALESCE(excluded.aid, videos.aid),
                    title=excluded.title,
                    description=excluded.description,
                    uploader=excluded.uploader,
                    upload_date=excluded.upload_date,
                    duration=excluded.duration,
                    view_count=excluded.view_count,
                    like_count=excluded.like_count,
                    comment_count=excluded.comment_count,
                    webpage_url=excluded.webpage_url,
                    subtitle_text=CASE WHEN length(excluded.subtitle_text) > 0 THEN excluded.subtitle_text ELSE videos.subtitle_text END,
                    video_relpath=COALESCE(excluded.video_relpath, videos.video_relpath),
                    info_json_relpath=COALESCE(excluded.info_json_relpath, videos.info_json_relpath),
                    subtitle_relpath=COALESCE(excluded.subtitle_relpath, videos.subtitle_relpath),
                    thumbnail_relpath=COALESCE(excluded.thumbnail_relpath, videos.thumbnail_relpath),
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    target_id,
                    platform,
                    video_id,
                    str(aid) if aid is not None else None,
                    title,
                    desc,
                    str(info.get("uploader") or ""),
                    upload_date,
                    info.get("duration"),
                    info.get("view_count"),
                    info.get("like_count"),
                    info.get("comment_count"),
                    url,
                    subtitle_text,
                    video_relpath,
                    info_json_relpath,
                    subtitle_relpath,
                    thumbnail_relpath,
                    json.dumps(info, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._upsert_fts(
                target_id=target_id,
                kind="video",
                item_id=video_id,
                title=title,
                body=desc,
                published_at=upload_date,
                source_url=url,
            )
            if subtitle_text.strip():
                self._upsert_fts(
                    target_id=target_id,
                    kind="subtitle",
                    item_id=video_id,
                    title=title,
                    body=subtitle_text,
                    published_at=upload_date,
                    source_url=url,
                )

    def upsert_dynamic(
        self,
        *,
        target_id: str,
        item: dict[str, Any],
        markdown_content: str = "",
        images_relpaths: list[str] | None = None,
        folder_relpath: str | None = None,
    ) -> None:
        dyn_id = str(item.get("id_str") or "")
        if not dyn_id:
            return
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        stat = modules.get("module_stat") or {}
        basic = item.get("basic") or {}

        pub_ts = author.get("pub_ts")
        published_at = format_unix_ts(pub_ts) if pub_ts else str(author.get("pub_time") or "unknown")
        main_text, orig_id, orig_text = _extract_dynamic_plain_text(item)
        full_body = "\n\n".join(p for p in (main_text, orig_text) if p)
        if not full_body and markdown_content:
            full_body = _markdown_to_plain_text(markdown_content)
        url = f"https://t.bilibili.com/{dyn_id}"

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO dynamics(
                    target_id, dynamic_id, dynamic_type, published_at, pub_ts, author_name,
                    text_content, markdown_content, is_forward, orig_dynamic_id, orig_text,
                    like_count, comment_count, forward_count, comment_oid, comment_type,
                    images_json, folder_relpath, raw_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, dynamic_id) DO UPDATE SET
                    dynamic_type=excluded.dynamic_type,
                    published_at=excluded.published_at,
                    pub_ts=excluded.pub_ts,
                    author_name=excluded.author_name,
                    text_content=excluded.text_content,
                    markdown_content=excluded.markdown_content,
                    is_forward=excluded.is_forward,
                    orig_dynamic_id=excluded.orig_dynamic_id,
                    orig_text=excluded.orig_text,
                    like_count=excluded.like_count,
                    comment_count=excluded.comment_count,
                    forward_count=excluded.forward_count,
                    comment_oid=excluded.comment_oid,
                    comment_type=excluded.comment_type,
                    images_json=excluded.images_json,
                    folder_relpath=COALESCE(excluded.folder_relpath, dynamics.folder_relpath),
                    raw_json=excluded.raw_json
                """,
                (
                    target_id,
                    dyn_id,
                    str(item.get("type") or ""),
                    published_at,
                    int(pub_ts) if isinstance(pub_ts, (int, float)) else None,
                    str(author.get("name") or ""),
                    main_text,
                    markdown_content,
                    1 if orig_id else 0,
                    orig_id,
                    orig_text,
                    (stat.get("like") or {}).get("count"),
                    (stat.get("comment") or {}).get("count"),
                    (stat.get("forward") or {}).get("count"),
                    str(basic.get("comment_id_str") or dyn_id),
                    int(basic.get("comment_type") or 17),
                    json.dumps(images_relpaths or [], ensure_ascii=False),
                    folder_relpath,
                    json.dumps(item, ensure_ascii=False),
                    _utc_now_iso(),
                ),
            )
            self._upsert_fts(
                target_id=target_id,
                kind="dynamic",
                item_id=dyn_id,
                title=f"动态 {dyn_id}",
                body=full_body,
                published_at=published_at,
                source_url=url,
            )

    def upsert_article(
        self,
        *,
        target_id: str,
        payload: dict[str, Any],
        markdown_content: str = "",
        images_relpaths: list[str] | None = None,
        folder_relpath: str | None = None,
    ) -> None:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
        cv_id = str(payload.get("cv_id") or meta.get("id") or "")
        if not cv_id:
            return
        opus_id = str(payload.get("opus_id") or meta.get("dyn_id_str") or "") or None
        title = str(meta.get("title") or f"cv{cv_id}")
        category = str((meta.get("category") or {}).get("name") or "")
        pub_ts = meta.get("publish_time")
        published_at = format_unix_ts(pub_ts) if pub_ts else "unknown"
        summary = str(meta.get("summary") or "")
        plain_text = _markdown_to_plain_text(markdown_content) or summary
        stats = meta.get("stats") or {}
        url = f"https://www.bilibili.com/read/cv{cv_id}"

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO articles(
                    target_id, cv_id, opus_id, title, category, published_at, pub_ts,
                    summary, text_content, markdown_content, words,
                    view_count, like_count, reply_count,
                    images_json, folder_relpath, raw_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, cv_id) DO UPDATE SET
                    opus_id=COALESCE(excluded.opus_id, articles.opus_id),
                    title=excluded.title,
                    category=excluded.category,
                    published_at=excluded.published_at,
                    pub_ts=excluded.pub_ts,
                    summary=excluded.summary,
                    text_content=excluded.text_content,
                    markdown_content=excluded.markdown_content,
                    words=excluded.words,
                    view_count=excluded.view_count,
                    like_count=excluded.like_count,
                    reply_count=excluded.reply_count,
                    images_json=excluded.images_json,
                    folder_relpath=COALESCE(excluded.folder_relpath, articles.folder_relpath),
                    raw_json=excluded.raw_json
                """,
                (
                    target_id,
                    cv_id,
                    opus_id,
                    title,
                    category,
                    published_at,
                    int(pub_ts) if isinstance(pub_ts, (int, float)) else None,
                    summary,
                    plain_text,
                    markdown_content,
                    meta.get("words"),
                    stats.get("view"),
                    stats.get("like"),
                    stats.get("reply"),
                    json.dumps(images_relpaths or [], ensure_ascii=False),
                    folder_relpath,
                    json.dumps(payload, ensure_ascii=False),
                    _utc_now_iso(),
                ),
            )
            self._upsert_fts(
                target_id=target_id,
                kind="article",
                item_id=f"cv{cv_id}",
                title=title,
                body=plain_text,
                published_at=published_at,
                source_url=url,
            )

    def upsert_comments(
        self,
        *,
        target_id: str,
        carrier_kind: str,
        oid: str,
        rtype: int,
        carrier_title: str,
        source_url: str,
        bundle: dict[str, Any],
        up_uid: int | str | None = None,
    ) -> int:
        """Upsert all root and nested replies from a curated comment bundle. Returns row count."""
        fetched_at = str(bundle.get("fetched_at") or _utc_now_iso())
        up_str = str(up_uid) if up_uid is not None else None

        pinned_ids = {
            str(r.get("rpid_str") or r.get("rpid") or "")
            for r in (bundle.get("pinned") or [])
            if isinstance(r, dict)
        }
        up_replied_ids = {
            str(r.get("rpid_str") or r.get("rpid") or "")
            for r in (bundle.get("up_replied") or [])
            if isinstance(r, dict)
        }

        flattened: dict[str, tuple[dict[str, Any], str | None]] = {}

        def _collect(root_list: list[Any]) -> None:
            for root in root_list:
                if not isinstance(root, dict):
                    continue
                rpid = str(root.get("rpid_str") or root.get("rpid") or "")
                if not rpid:
                    continue
                flattened.setdefault(rpid, (root, None))
                for sub in root.get("replies") or []:
                    if not isinstance(sub, dict):
                        continue
                    srpid = str(sub.get("rpid_str") or sub.get("rpid") or "")
                    if srpid:
                        flattened.setdefault(srpid, (sub, rpid))

        _collect(list(bundle.get("pinned") or []))
        _collect(list(bundle.get("up_replied") or []))
        _collect(list(bundle.get("top") or []))

        if not flattened:
            return 0

        with self._conn:
            for rpid, (reply, root_rpid) in flattened.items():
                member = reply.get("member") or {}
                author_mid = str(member.get("mid") or "")
                author_name = str(member.get("uname") or "")
                is_up = 1 if (up_str is not None and author_mid == up_str) else 0
                is_pinned = 1 if rpid in pinned_ids else 0
                has_up_reply = 1 if (rpid in up_replied_ids or (reply.get("up_action") or {}).get("reply")) else 0
                content = str((reply.get("content") or {}).get("message") or "").strip()
                ctime = reply.get("ctime")
                published_at = format_unix_ts(ctime) if ctime else "unknown"
                likes = reply.get("like", 0)

                self._conn.execute(
                    """
                    INSERT INTO comments(
                        target_id, carrier_kind, oid, rtype, rpid, root_rpid,
                        author_mid, author_name, is_up, is_pinned, has_up_reply,
                        content, like_count, ctime, published_at, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(target_id, carrier_kind, oid, rpid) DO UPDATE SET
                        root_rpid=COALESCE(excluded.root_rpid, comments.root_rpid),
                        author_name=excluded.author_name,
                        is_up=excluded.is_up,
                        is_pinned=MAX(comments.is_pinned, excluded.is_pinned),
                        has_up_reply=MAX(comments.has_up_reply, excluded.has_up_reply),
                        content=excluded.content,
                        like_count=excluded.like_count,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        target_id,
                        carrier_kind,
                        str(oid),
                        int(rtype),
                        rpid,
                        root_rpid,
                        author_mid,
                        author_name,
                        is_up,
                        is_pinned,
                        has_up_reply,
                        content,
                        likes,
                        int(ctime) if isinstance(ctime, (int, float)) else None,
                        published_at,
                        fetched_at,
                    ),
                )
                label = f"{carrier_title} ({author_name}{'[UP主]' if is_up else ''})"
                self._upsert_fts(
                    target_id=target_id,
                    kind="comment",
                    item_id=f"{carrier_kind}:{oid}:{rpid}",
                    display_title=label,
                    title="",
                    body=content,
                    published_at=published_at,
                    source_url=source_url,
                )

        return len(flattened)

    def upsert_favorite_folder(
        self,
        *,
        target_id: str,
        folder: dict[str, Any],
        medias: list[dict[str, Any]],
    ) -> None:
        media_id = str(folder.get("id") or "")
        if not media_id:
            return
        now = _utc_now_iso()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO favorites(target_id, media_id, title, media_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_id, media_id) DO UPDATE SET
                    title=excluded.title,
                    media_count=excluded.media_count,
                    updated_at=excluded.updated_at
                """,
                (target_id, media_id, str(folder.get("title") or ""), len(medias), now),
            )
            for m in medias:
                if not isinstance(m, dict):
                    continue
                bvid = str(m.get("bvid") or m.get("id") or "")
                if not bvid:
                    continue
                url = f"https://www.bilibili.com/video/{bvid}" if bvid.startswith("BV") else ""
                self._conn.execute(
                    """
                    INSERT INTO favorite_items(
                        target_id, media_id, bvid, title, upper_name, duration, fav_time, url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(target_id, media_id, bvid) DO UPDATE SET
                        title=excluded.title,
                        upper_name=excluded.upper_name,
                        duration=excluded.duration,
                        fav_time=excluded.fav_time,
                        url=excluded.url
                    """,
                    (
                        target_id,
                        media_id,
                        bvid,
                        str(m.get("title") or ""),
                        str((m.get("upper") or {}).get("name") or ""),
                        m.get("duration"),
                        format_unix_date(m.get("fav_time")),
                        url,
                    ),
                )

    def record_sync_run(
        self,
        *,
        mode: str,
        started_at: str,
        finished_at: str,
        summary_dict: dict[str, Any],
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_runs(mode, started_at, finished_at, summary_json) VALUES (?, ?, ?, ?)",
                (mode, started_at, finished_at, json.dumps(summary_dict, ensure_ascii=False)),
            )

    # ------------------------------------------------------------------
    # Search & Stats
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        target_id: str | None = None,
        kinds: set[str] | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Full-text search across videos, subtitles, dynamics, articles, and comments."""
        cleaned = (query or "").strip()
        if not cleaned:
            return []

        tokens = [tok for tok in cleaned.split() if tok]
        use_fts = bool(tokens) and all(len(tok) >= 3 for tok in tokens)

        where_clauses: list[str] = []
        params: list[Any] = []

        if target_id:
            where_clauses.append("target_id = ?")
            params.append(target_id)
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            where_clauses.append(f"kind IN ({placeholders})")
            params.extend(sorted(kinds))

        if use_fts:
            fts_expr = " ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)
            where_clauses.insert(0, "search_fts MATCH ?")
            params.insert(0, fts_expr)
            sql = f"""
                SELECT target_id, kind, item_id, display_title,
                       snippet(search_fts, 5, '«', '»', '...', 24) AS snip,
                       body, published_at, source_url
                FROM search_fts
                WHERE {' AND '.join(where_clauses)}
                ORDER BY bm25(search_fts)
                LIMIT ?
            """
            params.append(limit)
            rows = self._conn.execute(sql, params).fetchall()
            hits: list[SearchHit] = []
            for r in rows:
                snip = str(r["snip"] or "").strip()
                if not snip:
                    snip = _build_like_snippet(str(r["body"] or ""), tokens[0])
                hits.append(
                    SearchHit(
                        target_id=str(r["target_id"]),
                        kind=str(r["kind"]),
                        item_id=str(r["item_id"]),
                        title=str(r["display_title"] or ""),
                        snippet=re.sub(r"\s+", " ", snip),
                        published_at=str(r["published_at"] or ""),
                        source_url=str(r["source_url"] or ""),
                    )
                )
            return hits

        # Fallback for short (<3 chars) Chinese/English terms where trigram index has no 3-gram
        for tok in tokens:
            like_pat = f"%{tok}%"
            where_clauses.append("(title LIKE ? OR body LIKE ?)")
            params.extend([like_pat, like_pat])

        sql = f"""
            SELECT target_id, kind, item_id, display_title, body, published_at, source_url
            FROM search_fts
            WHERE {' AND '.join(where_clauses)}
            ORDER BY published_at DESC
            LIMIT ?
        """
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            SearchHit(
                target_id=str(r["target_id"]),
                kind=str(r["kind"]),
                item_id=str(r["item_id"]),
                title=str(r["display_title"] or ""),
                snippet=_build_like_snippet(str(r["body"] or r["display_title"] or ""), tokens[0]),
                published_at=str(r["published_at"] or ""),
                source_url=str(r["source_url"] or ""),
            )
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for tbl in (
            "profiles",
            "profile_snapshots",
            "videos",
            "dynamics",
            "articles",
            "comments",
            "favorites",
            "favorite_items",
            "sync_runs",
            "search_fts",
        ):
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
            counts[tbl] = int(row["n"]) if row else 0
        size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "db_path": str(self.db_path),
            "size_bytes": size_bytes,
            "counts": counts,
        }


# ---------------------------------------------------------------------------
# Disk -> SQLite indexing & rebuild helpers
# ---------------------------------------------------------------------------


def _find_best_subtitle(base_dir: Path, stem: str) -> Path | None:
    preferred_suffixes = (".ai-zh.srt", ".zh-Hans.srt", ".zh-CN.srt", ".zh.srt", ".zh-Hant.srt", ".en.srt")
    for suf in preferred_suffixes:
        cand = base_dir / f"{stem}{suf}"
        if cand.exists():
            return cand
    for cand in sorted(base_dir.iterdir()):
        if cand.is_file() and cand.name.startswith(stem) and cand.suffix.lower() in {".srt", ".vtt"}:
            return cand
    return None


def _find_sibling_by_ext(base_dir: Path, stem: str, exts: tuple[str, ...]) -> Path | None:
    for ext in exts:
        cand = base_dir / f"{stem}{ext}"
        if cand.exists():
            return cand
    return None


def index_video_info_file(db: ArchiveDB, target: ResolvedTarget, info_file: Path) -> bool:
    """Index a single `.info.json` and its sibling subtitle/media files into `ArchiveDB`."""
    try:
        info = json.loads(info_file.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return False
    if not isinstance(info, dict):
        return False

    stem = info_file.name[: -len(".info.json")]
    vdir = info_file.parent
    sub_file = _find_best_subtitle(vdir, stem)
    vid_file = _find_sibling_by_ext(vdir, stem, (".mp4", ".mkv", ".webm"))
    thumb_file = _find_sibling_by_ext(vdir, stem, (".jpg", ".png", ".webp"))
    sub_text = extract_srt_plain_text(sub_file) if sub_file else ""

    db.upsert_video(
        target_id=target.id,
        platform=target.platform.value,
        info=info,
        subtitle_text=sub_text,
        video_relpath=_rel_posix(vid_file, target.download_dir),
        info_json_relpath=_rel_posix(info_file, target.download_dir),
        subtitle_relpath=_rel_posix(sub_file, target.download_dir),
        thumbnail_relpath=_rel_posix(thumb_file, target.download_dir),
    )
    return True


def rebuild_target_from_disk(db: ArchiveDB, target: ResolvedTarget) -> dict[str, int]:
    """Scan a target's download directory and idempotently populate SQLite without network calls."""
    counts = {
        "profiles": 0,
        "videos": 0,
        "dynamics": 0,
        "articles": 0,
        "favorites": 0,
        "comments": 0,
    }
    root = target.download_dir
    if not root.exists():
        return counts

    up_uid = extract_bilibili_uid(target.source) if target.platform == Platform.BILIBILI else None

    # 1. Profile
    profile_json = root / "profile" / "profile.json"
    if profile_json.exists():
        try:
            pdata = json.loads(profile_json.read_text(encoding="utf-8", errors="ignore"))
            cols_path = root / "profile" / "collections.json"
            cols = (
                json.loads(cols_path.read_text(encoding="utf-8", errors="ignore"))
                if cols_path.exists()
                else []
            )
            db.upsert_profile(
                target_id=target.id,
                platform=target.platform.value,
                uid=pdata.get("uid") or up_uid or "",
                user_info=pdata.get("user_info") or {},
                relation_stat=pdata.get("relation_stat"),
                upstat=pdata.get("upstat"),
                collections=cols if isinstance(cols, list) else [],
                fetched_at=pdata.get("fetched_at"),
            )
            counts["profiles"] += 1
        except Exception:
            pass

    # 2. Videos (flat + grouped) + video comments
    video_dirs = [root]
    if (root / "videos").is_dir():
        video_dirs.append(root / "videos")
    for vdir in video_dirs:
        for info_file in sorted(vdir.glob("*.info.json")):
            if index_video_info_file(db, target, info_file):
                counts["videos"] += 1
            stem = info_file.name[: -len(".info.json")]
            cmt_file = vdir / f"{stem}.comments.json"
            if cmt_file.exists():
                try:
                    info = json.loads(info_file.read_text(encoding="utf-8", errors="ignore"))
                    bundle = json.loads(cmt_file.read_text(encoding="utf-8", errors="ignore"))
                    bvid = str(info.get("id") or "")
                    oid = str(bundle.get("oid") or info.get("aid") or bv_to_av(bvid) or bvid)
                    counts["comments"] += db.upsert_comments(
                        target_id=target.id,
                        carrier_kind="video",
                        oid=oid,
                        rtype=int(bundle.get("type") or 1),
                        carrier_title=str(info.get("title") or bvid),
                        source_url=str(info.get("webpage_url") or f"https://www.bilibili.com/video/{bvid}"),
                        bundle=bundle,
                        up_uid=up_uid,
                    )
                except Exception:
                    pass

    # 3. Dynamics + dynamic comments
    dyn_root = root / "dynamics"
    if dyn_root.is_dir():
        for dyn_file in sorted(dyn_root.glob("*/dynamic.json")):
            try:
                item = json.loads(dyn_file.read_text(encoding="utf-8", errors="ignore"))
                md_file = dyn_file.with_name("dynamic.md")
                md_text = md_file.read_text(encoding="utf-8", errors="ignore") if md_file.exists() else ""
                img_dir = dyn_file.parent / "images"
                imgs = (
                    [_rel_posix(p, root) or "" for p in sorted(img_dir.iterdir()) if p.is_file()]
                    if img_dir.is_dir()
                    else []
                )
                db.upsert_dynamic(
                    target_id=target.id,
                    item=item,
                    markdown_content=md_text,
                    images_relpaths=[p for p in imgs if p],
                    folder_relpath=_rel_posix(dyn_file.parent, root),
                )
                counts["dynamics"] += 1

                cmt_file = dyn_file.with_name("comments.json")
                if cmt_file.exists():
                    bundle = json.loads(cmt_file.read_text(encoding="utf-8", errors="ignore"))
                    dyn_id = str(item.get("id_str") or "")
                    basic = item.get("basic") or {}
                    oid = str(bundle.get("oid") or basic.get("comment_id_str") or dyn_id)
                    rtype = int(bundle.get("type") or basic.get("comment_type") or 17)
                    counts["comments"] += db.upsert_comments(
                        target_id=target.id,
                        carrier_kind="dynamic",
                        oid=oid,
                        rtype=rtype,
                        carrier_title=f"动态 {dyn_id}",
                        source_url=f"https://t.bilibili.com/{dyn_id}",
                        bundle=bundle,
                        up_uid=up_uid,
                    )
            except Exception:
                pass

    # 4. Articles + article comments
    art_root = root / "articles"
    if art_root.is_dir():
        for art_file in sorted(art_root.glob("*/article.json")):
            try:
                payload = json.loads(art_file.read_text(encoding="utf-8", errors="ignore"))
                md_file = art_file.with_name("article.md")
                md_text = md_file.read_text(encoding="utf-8", errors="ignore") if md_file.exists() else ""
                img_dir = art_file.parent / "images"
                imgs = (
                    [_rel_posix(p, root) or "" for p in sorted(img_dir.iterdir()) if p.is_file()]
                    if img_dir.is_dir()
                    else []
                )
                db.upsert_article(
                    target_id=target.id,
                    payload=payload,
                    markdown_content=md_text,
                    images_relpaths=[p for p in imgs if p],
                    folder_relpath=_rel_posix(art_file.parent, root),
                )
                counts["articles"] += 1

                cmt_file = art_file.with_name("comments.json")
                if cmt_file.exists():
                    bundle = json.loads(cmt_file.read_text(encoding="utf-8", errors="ignore"))
                    cv_id = str(payload.get("cv_id") or "")
                    title = str((payload.get("meta") or {}).get("title") or f"专栏 cv{cv_id}")
                    counts["comments"] += db.upsert_comments(
                        target_id=target.id,
                        carrier_kind="article",
                        oid=str(bundle.get("oid") or cv_id),
                        rtype=int(bundle.get("type") or 12),
                        carrier_title=title,
                        source_url=f"https://www.bilibili.com/read/cv{cv_id}",
                        bundle=bundle,
                        up_uid=up_uid,
                    )
            except Exception:
                pass

    # 5. Favorites
    fav_root = root / "favorites"
    if fav_root.is_dir():
        for fav_file in sorted(fav_root.glob("fav_*.json")):
            try:
                fdata = json.loads(fav_file.read_text(encoding="utf-8", errors="ignore"))
                folder = fdata.get("folder") or {}
                medias = fdata.get("medias") or []
                db.upsert_favorite_folder(target_id=target.id, folder=folder, medias=medias)
                counts["favorites"] += 1
            except Exception:
                pass

    return counts
