"""Bilibili extended content archiver (profile, dynamics, articles, favorites, comments)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import ResolvedTarget
from ..platform import Platform, extract_bilibili_uid
from ..state import FailedRecord, StateStore
from .client import (
    BilibiliApiError,
    BilibiliAuthError,
    BilibiliRiskControlError,
    BilibiliWebClient,
)
from .render import (
    ImageLocalizer,
    extract_dynamic_image_urls,
    extract_legacy_html_images,
    extract_opus_image_urls,
    format_unix_date,
    normalize_url,
    render_article_markdown,
    render_comments_markdown,
    render_dynamic_markdown,
    render_favorite_folder_markdown,
    render_favorites_index_markdown,
    render_profile_markdown,
    sanitize_filename,
)

# Official Bilibili BV1 -> AV (aid) conversion constants (2023+ algorithm).
_BV_XOR_CODE = 23442827791579
_BV_MASK_CODE = 2251799813685247  # (1 << 51) - 1
_BV_BASE = 58
_BV_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"


def bv_to_av(bvid: str) -> int | None:
    """Convert a modern Bilibili BV1... string to numeric aid without network requests."""
    cleaned = (bvid or "").strip()
    if len(cleaned) != 12 or not cleaned.startswith("BV1"):
        return None
    chars = list(cleaned)
    chars[3], chars[9] = chars[9], chars[3]
    chars[4], chars[7] = chars[7], chars[4]
    tmp = 0
    try:
        for ch in chars[3:]:
            tmp = tmp * _BV_BASE + _BV_ALPHABET.index(ch)
    except ValueError:
        return None
    return (tmp & _BV_MASK_CODE) ^ _BV_XOR_CODE


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass
class ExtendedModuleStats:
    discovered: int = 0
    already_archived: int = 0
    saved: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "already_archived": self.already_archived,
            "saved": self.saved,
            "failed": self.failed,
        }


@dataclass
class ExtendedProbeSummary:
    uid: int | None = None
    dynamics_sampled: int = 0
    dynamics_pending: int = 0
    articles_total: int = 0
    articles_pending: int = 0
    favorites_folders: int = 0
    error: str | None = None


@dataclass
class _CommentCarrier:
    kind: str  # "videos" | "dynamics" | "articles"
    oid: str
    rtype: int
    title: str
    json_path: Path
    md_path: Path
    archive_key: str
    source_url: str


class BilibiliArchiver:
    """Orchestrates archiving of Bilibili profile, dynamics, articles, favorites, and comments."""

    def __init__(
        self,
        state: StateStore,
        *,
        client_factory: Callable[[ResolvedTarget], BilibiliWebClient] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.state = state
        self._client_factory = client_factory or self._default_client_factory
        self._on_event = on_event

    @staticmethod
    def _default_client_factory(target: ResolvedTarget) -> BilibiliWebClient:
        return BilibiliWebClient(
            cookies_file=target.auth.cookies_file,
            proxy=target.proxy,
            sleep_min=target.archive.api_sleep_min,
            sleep_max=target.archive.api_sleep_max,
        )

    def _emit(self, event: str, **payload: Any) -> None:
        if self._on_event:
            self._on_event(event, payload)

    def _open_db(self) -> Any:
        from ..db import ArchiveDB

        return ArchiveDB(self.state.state_dir / "biliyt.db")

    # ------------------------------------------------------------------
    # Lightweight probe
    # ------------------------------------------------------------------

    def probe_extended(
        self,
        target: ResolvedTarget,
        *,
        limit_override: int | None = None,
    ) -> ExtendedProbeSummary:
        summary = ExtendedProbeSummary()
        if target.platform != Platform.BILIBILI:
            return summary

        uid = extract_bilibili_uid(target.source)
        summary.uid = uid
        if uid is None:
            summary.error = "无法从 target.source 提取 B 站 UID（合集/收藏夹直链跳过扩展探查）"
            return summary

        cfg = target.archive
        try:
            with self._client_factory(target) as client:
                if cfg.dynamics:
                    page = client.get_dynamics_page(uid)
                    items = [i for i in (page.get("items") or []) if isinstance(i, dict)]
                    archived = self.state.read_namespace_archive(target.id, "dynamics")
                    lim = limit_override or cfg.dynamics_limit
                    sampled = items[:lim] if lim else items
                    summary.dynamics_sampled = len(sampled)
                    summary.dynamics_pending = sum(
                        1 for it in sampled if str(it.get("id_str") or "") not in archived
                    )

                if cfg.articles:
                    page = client.get_articles_page(uid, pn=1, ps=30)
                    articles = [a for a in (page.get("articles") or []) if isinstance(a, dict)]
                    archived = self.state.read_namespace_archive(target.id, "articles")
                    summary.articles_total = int(page.get("count") or len(articles))
                    lim = limit_override or cfg.articles_limit
                    sampled = articles[:lim] if lim else articles
                    summary.articles_pending = sum(
                        1 for a in sampled if f"cv{a.get('id')}" not in archived
                    )

                if cfg.favorites:
                    favs = client.get_created_favorites(uid)
                    folders = [f for f in (favs.get("list") or []) if isinstance(f, dict)]
                    summary.favorites_folders = len(folders)
        except Exception as exc:
            summary.error = str(exc)

        return summary

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def sync_profile(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        uid: int,
        *,
        dry_run: bool = False,
    ) -> ExtendedModuleStats:
        stats = ExtendedModuleStats(discovered=1)
        self._emit("ext_module_start", target=target, module="profile")
        if dry_run:
            self._emit("ext_module_done", target=target, module="profile", stats=stats)
            return stats

        profile_dir = target.download_dir / "profile"
        try:
            user_info = client.get_user_info(uid)
            relation_stat: dict[str, Any] | None = None
            upstat: dict[str, Any] | None = None
            collections: list[dict[str, Any]] = []

            try:
                relation_stat = client.get_relation_stat(uid)
            except BilibiliApiError:
                pass
            try:
                upstat = client.get_upstat(uid)
            except BilibiliApiError:
                pass
            try:
                col_data = client.get_collections(uid)
                items_lists = col_data.get("items_lists") or {}
                for key in ("seasons_list", "series_list"):
                    for col in items_lists.get(key) or []:
                        if isinstance(col, dict):
                            collections.append(col)
            except BilibiliApiError:
                pass

            localizer = ImageLocalizer(
                client,
                enabled=target.archive.download_images,
                referer=f"https://space.bilibili.com/{uid}/",
            )
            avatar_url = normalize_url(str(user_info.get("face") or ""))
            banner_url = normalize_url(str(user_info.get("top_photo") or ""))
            img_map = localizer.localize(
                [u for u in (avatar_url, banner_url) if u],
                profile_dir / "assets",
                rel_prefix="assets",
            )
            avatar_rel = img_map.get(avatar_url)
            banner_rel = img_map.get(banner_url)
            fetched_at = _utc_now_iso()

            _write_json(
                profile_dir / "profile.json",
                {
                    "uid": uid,
                    "fetched_at": fetched_at,
                    "user_info": user_info,
                    "relation_stat": relation_stat,
                    "upstat": upstat,
                },
            )
            _write_json(profile_dir / "collections.json", collections)
            _write_text(
                profile_dir / "profile.md",
                render_profile_markdown(
                    uid=uid,
                    user_info=user_info,
                    relation_stat=relation_stat,
                    upstat=upstat,
                    collections=collections,
                    avatar_rel=avatar_rel,
                    banner_rel=banner_rel,
                    fetched_at=fetched_at,
                ),
            )
            with self._open_db() as db:
                db.upsert_profile(
                    target_id=target.id,
                    platform=target.platform.value,
                    uid=uid,
                    user_info=user_info,
                    relation_stat=relation_stat,
                    upstat=upstat,
                    collections=collections,
                    avatar_relpath=f"profile/{avatar_rel}" if avatar_rel else None,
                    banner_relpath=f"profile/{banner_rel}" if banner_rel else None,
                    fetched_at=fetched_at,
                )
            stats.saved = 1
        except (BilibiliRiskControlError, BilibiliAuthError) as exc:
            stats.failed = 1
            stats.errors.append(str(exc))
            self._emit("ext_module_done", target=target, module="profile", stats=stats)
            raise
        except Exception as exc:
            stats.failed = 1
            stats.errors.append(str(exc))

        self._emit("ext_module_done", target=target, module="profile", stats=stats)
        return stats

    # ------------------------------------------------------------------
    # Dynamics
    # ------------------------------------------------------------------

    @staticmethod
    def _is_pinned_dynamic(item: dict[str, Any]) -> bool:
        tag = ((item.get("modules") or {}).get("module_tag") or {}).get("text")
        return str(tag or "").strip() == "置顶"

    def _save_single_dynamic(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        item: dict[str, Any],
    ) -> Path:
        dyn_id = str(item.get("id_str") or "")
        author = (item.get("modules") or {}).get("module_author") or {}
        date_str = format_unix_date(author.get("pub_ts"))
        folder = target.download_dir / "dynamics" / f"{date_str} - {dyn_id}"

        img_urls = extract_dynamic_image_urls(item)
        localizer = ImageLocalizer(
            client,
            enabled=target.archive.download_images,
            referer=f"https://t.bilibili.com/{dyn_id}",
        )
        image_map = localizer.localize(img_urls, folder / "images", rel_prefix="images")

        md_text = render_dynamic_markdown(item, image_map)
        _write_json(folder / "dynamic.json", item)
        _write_text(folder / "dynamic.md", md_text)
        rel_folder = folder.relative_to(target.download_dir).as_posix()
        rel_imgs = [f"{rel_folder}/{rel}" for rel in image_map.values()]
        with self._open_db() as db:
            db.upsert_dynamic(
                target_id=target.id,
                item=item,
                markdown_content=md_text,
                images_relpaths=rel_imgs,
                folder_relpath=rel_folder,
            )
        return folder

    def sync_dynamics(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        uid: int,
        *,
        limit_override: int | None = None,
        full: bool = False,
        dry_run: bool = False,
    ) -> ExtendedModuleStats:
        stats = ExtendedModuleStats()
        self._emit("ext_module_start", target=target, module="dynamics")
        archived = self.state.read_namespace_archive(target.id, "dynamics")
        effective_limit = None if full else (limit_override if limit_override is not None else target.archive.dynamics_limit)

        offset = ""
        consecutive_archived = 0
        stop = False

        while not stop:
            page = client.get_dynamics_page(uid, offset=offset)
            items = [it for it in (page.get("items") or []) if isinstance(it, dict)]
            if not items:
                break

            for item in items:
                dyn_id = str(item.get("id_str") or "")
                if not dyn_id:
                    continue
                if effective_limit is not None and stats.discovered >= effective_limit:
                    stop = True
                    break

                stats.discovered += 1
                is_pinned = self._is_pinned_dynamic(item)

                if dyn_id in archived:
                    stats.already_archived += 1
                    if not is_pinned:
                        consecutive_archived += 1
                    # Incremental early stop once we hit 2 consecutive non-pinned archived items
                    if not full and consecutive_archived >= 2:
                        stop = True
                        break
                    continue

                consecutive_archived = 0
                if dry_run:
                    continue

                try:
                    self._save_single_dynamic(target, client, item)
                    self.state.append_namespace_archive(target.id, "dynamics", dyn_id)
                    archived.add(dyn_id)
                    self.state.remove_failure(target.id, f"https://t.bilibili.com/{dyn_id}", item_type="dynamic")
                    stats.saved += 1
                except (BilibiliRiskControlError, BilibiliAuthError):
                    raise
                except Exception as exc:
                    stats.failed += 1
                    stats.errors.append(f"dynamic {dyn_id}: {exc}")
                    self.state.record_failure(
                        target_id=target.id,
                        platform=target.platform.value,
                        url=f"https://t.bilibili.com/{dyn_id}",
                        video_id=dyn_id,
                        title=f"动态 {dyn_id}",
                        error=str(exc),
                        item_type="dynamic",
                        extra={"dynamic_id": dyn_id},
                    )

            has_more = bool(page.get("has_more"))
            next_offset = str(page.get("offset") or "")
            if not has_more or not next_offset or next_offset == offset:
                break
            offset = next_offset

        self._emit("ext_module_done", target=target, module="dynamics", stats=stats)
        return stats

    # ------------------------------------------------------------------
    # Articles (Opus + legacy cv HTML)
    # ------------------------------------------------------------------

    def _save_single_article(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        meta: dict[str, Any],
    ) -> Path:
        cv_id = str(meta.get("id") or "")
        title = str(meta.get("title") or f"cv{cv_id}")
        date_str = format_unix_date(meta.get("publish_time"))
        safe_title = sanitize_filename(title, max_len=70, fallback=f"cv{cv_id}")
        folder = target.download_dir / "articles" / f"{date_str} - {safe_title} [cv{cv_id}]"

        view_data: dict[str, Any] | None = None
        opus_detail: dict[str, Any] | None = None
        html_text: str | None = None
        opus_id = str(meta.get("dyn_id_str") or meta.get("opus_id") or "").strip()

        # Primary path: official JSON API /x/article/view (returns full Opus paragraphs or raw HTML content)
        if cv_id and hasattr(client, "get_article_view"):
            try:
                view_data = client.get_article_view(cv_id)
                if isinstance(view_data, dict):
                    opus_id = opus_id or str(view_data.get("dyn_id_str") or "").strip()
                    if isinstance(view_data.get("opus"), dict):
                        opus_detail = view_data["opus"]
                    raw_content = view_data.get("content")
                    if isinstance(raw_content, str) and raw_content.strip():
                        html_text = raw_content
            except BilibiliApiError:
                view_data = None

        if opus_detail is None and opus_id:
            try:
                opus_detail = client.get_opus_detail(opus_id)
            except BilibiliApiError:
                opus_detail = None

        if opus_detail is None and html_text is None and cv_id:
            try:
                fetched_html = client.get_article_html(cv_id)
                if "risk-captcha" not in fetched_html and "v_voucher" not in fetched_html:
                    html_text = fetched_html
            except BilibiliApiError:
                html_text = None

        img_urls: list[str] = []
        if opus_detail:
            img_urls.extend(extract_opus_image_urls(opus_detail))
        if html_text:
            img_urls.extend(extract_legacy_html_images(html_text))
        for cover_url in (meta.get("origin_image_urls") or meta.get("image_urls") or []):
            if isinstance(cover_url, str) and cover_url:
                img_urls.append(normalize_url(cover_url))

        localizer = ImageLocalizer(
            client,
            enabled=target.archive.download_images,
            referer=f"https://www.bilibili.com/read/cv{cv_id}",
        )
        image_map = localizer.localize(img_urls, folder / "images", rel_prefix="images")

        payload = {
            "cv_id": cv_id,
            "opus_id": opus_id or None,
            "fetched_at": _utc_now_iso(),
            "meta": meta,
            "view": view_data,
            "opus_detail": opus_detail,
        }
        md_text = render_article_markdown(
            meta=meta,
            opus_detail=opus_detail,
            html_text=html_text,
            image_map=image_map,
        )
        _write_json(folder / "article.json", payload)
        _write_text(folder / "article.md", md_text)
        rel_folder = folder.relative_to(target.download_dir).as_posix()
        rel_imgs = [f"{rel_folder}/{rel}" for rel in image_map.values()]
        with self._open_db() as db:
            db.upsert_article(
                target_id=target.id,
                payload=payload,
                markdown_content=md_text,
                images_relpaths=rel_imgs,
                folder_relpath=rel_folder,
            )
        return folder

    def sync_articles(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        uid: int,
        *,
        limit_override: int | None = None,
        full: bool = False,
        dry_run: bool = False,
    ) -> ExtendedModuleStats:
        stats = ExtendedModuleStats()
        self._emit("ext_module_start", target=target, module="articles")
        archived = self.state.read_namespace_archive(target.id, "articles")
        effective_limit = None if full else (limit_override if limit_override is not None else target.archive.articles_limit)

        pn = 1
        ps = 30
        stop = False
        while not stop:
            page = client.get_articles_page(uid, pn=pn, ps=ps)
            articles = [a for a in (page.get("articles") or []) if isinstance(a, dict)]
            if not articles:
                break

            for meta in articles:
                cv_id = str(meta.get("id") or "")
                if not cv_id:
                    continue
                if effective_limit is not None and stats.discovered >= effective_limit:
                    stop = True
                    break

                stats.discovered += 1
                key = f"cv{cv_id}"
                if key in archived:
                    stats.already_archived += 1
                    continue

                if dry_run:
                    continue

                try:
                    self._save_single_article(target, client, meta)
                    self.state.append_namespace_archive(target.id, "articles", key)
                    archived.add(key)
                    self.state.remove_failure(
                        target.id,
                        f"https://www.bilibili.com/read/cv{cv_id}",
                        item_type="article",
                    )
                    stats.saved += 1
                except (BilibiliRiskControlError, BilibiliAuthError):
                    raise
                except Exception as exc:
                    stats.failed += 1
                    stats.errors.append(f"article cv{cv_id}: {exc}")
                    self.state.record_failure(
                        target_id=target.id,
                        platform=target.platform.value,
                        url=f"https://www.bilibili.com/read/cv{cv_id}",
                        video_id=key,
                        title=str(meta.get("title") or key),
                        error=str(exc),
                        item_type="article",
                        extra={"cv_id": cv_id, "meta": meta},
                    )

            total = int(page.get("count") or 0)
            if len(articles) < ps or (total > 0 and pn * ps >= total):
                break
            pn += 1

        self._emit("ext_module_done", target=target, module="articles", stats=stats)
        return stats

    # ------------------------------------------------------------------
    # Public Favorites (Metadata list ONLY)
    # ------------------------------------------------------------------

    def sync_favorites(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        uid: int,
        *,
        dry_run: bool = False,
    ) -> ExtendedModuleStats:
        stats = ExtendedModuleStats()
        self._emit("ext_module_start", target=target, module="favorites")

        data = client.get_created_favorites(uid)
        folders = [f for f in (data.get("list") or []) if isinstance(f, dict)]
        stats.discovered = len(folders)

        if dry_run or not folders:
            self._emit("ext_module_done", target=target, module="favorites", stats=stats)
            return stats

        fav_dir = target.download_dir / "favorites"
        index_entries: list[dict[str, Any]] = []

        for folder in folders:
            media_id = folder.get("id")
            if not media_id:
                continue
            title = str(folder.get("title") or f"fav_{media_id}")
            safe_title = sanitize_filename(title, max_len=60, fallback=f"fav_{media_id}")
            base_name = f"fav_{media_id}_{safe_title}"

            medias: list[dict[str, Any]] = []
            pn = 1
            ps = 20
            try:
                while True:
                    page = client.get_favorite_resources_page(int(media_id), pn=pn, ps=ps)
                    batch = [m for m in (page.get("medias") or []) if isinstance(m, dict)]
                    medias.extend(batch)
                    if not page.get("has_more") or not batch:
                        break
                    pn += 1

                _write_json(
                    fav_dir / f"{base_name}.json",
                    {
                        "fetched_at": _utc_now_iso(),
                        "folder": folder,
                        "medias": medias,
                    },
                )
                _write_text(
                    fav_dir / f"{base_name}.md",
                    render_favorite_folder_markdown(folder, medias),
                )
                with self._open_db() as db:
                    db.upsert_favorite_folder(
                        target_id=target.id,
                        folder=folder,
                        medias=medias,
                    )
                folder_copy = dict(folder)
                folder_copy["_md_filename"] = f"{base_name}.md"
                index_entries.append(folder_copy)
                stats.saved += 1
            except (BilibiliRiskControlError, BilibiliAuthError):
                raise
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"favorite {media_id}: {exc}")

        if index_entries:
            _write_text(fav_dir / "index.md", render_favorites_index_markdown(uid, index_entries))

        self._emit("ext_module_done", target=target, module="favorites", stats=stats)
        return stats

    # ------------------------------------------------------------------
    # Curated Comments (Pinned + UP Replies + Top N)
    # ------------------------------------------------------------------

    @staticmethod
    def _reply_involves_up(reply: dict[str, Any], up_uid: int) -> bool:
        up_str = str(up_uid)
        if str((reply.get("member") or {}).get("mid") or "") == up_str:
            return True
        up_action = reply.get("up_action") or {}
        if isinstance(up_action, dict) and up_action.get("reply"):
            return True
        for sub in reply.get("replies") or []:
            if isinstance(sub, dict) and str((sub.get("member") or {}).get("mid") or "") == up_str:
                return True
        return False

    def _enrich_sub_replies(
        self,
        client: BilibiliWebClient,
        *,
        oid: str,
        rtype: int,
        reply: dict[str, Any],
        max_sub: int,
    ) -> dict[str, Any]:
        """Fetch full sub-replies (up to `max_sub`) for a pinned or UP-replied root comment."""
        if max_sub <= 0:
            return reply
        rcount = int(reply.get("rcount") or 0)
        existing = reply.get("replies") or []
        if rcount <= len(existing):
            return reply

        rpid = reply.get("rpid") or reply.get("rpid_str")
        if not rpid:
            return reply

        collected: list[dict[str, Any]] = []
        pn = 1
        ps = 20
        try:
            while len(collected) < max_sub:
                page = client.get_sub_replies_page(
                    oid=oid,
                    rtype=rtype,
                    root_rpid=rpid,
                    pn=pn,
                    ps=ps,
                )
                batch = [s for s in (page.get("replies") or []) if isinstance(s, dict)]
                if not batch:
                    break
                collected.extend(batch)
                if len(batch) < ps:
                    break
                pn += 1
        except BilibiliApiError:
            return reply

        if collected:
            updated = dict(reply)
            updated["replies"] = collected[:max_sub]
            return updated
        return reply

    def fetch_curated_comments(
        self,
        client: BilibiliWebClient,
        *,
        oid: str,
        rtype: int,
        up_uid: int,
        top_n: int = 50,
        include_pinned: bool = True,
        include_up_replies: bool = True,
        max_sub_replies_per_root: int = 20,
    ) -> dict[str, Any]:
        """Fetch Pinned + UP-replied + Top N hot comments for one carrier."""
        pinned: list[dict[str, Any]] = []
        pinned_ids: set[str] = set()
        top_collected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        total_count: int | None = None

        next_offset = ""
        # Cap pages conservatively to stay risk-control safe (each hot page ~20 items)
        max_pages = max(1, (top_n // 15) + 2) if top_n > 0 else 1

        for page_idx in range(max_pages):
            page = client.get_replies_main_page(
                oid=oid,
                rtype=rtype,
                next_offset=next_offset,
                mode=3,
            )
            cursor = page.get("cursor") or {}
            if total_count is None and cursor.get("all_count") is not None:
                total_count = int(cursor["all_count"])

            if page_idx == 0 and include_pinned:
                top_obj = page.get("top") or {}
                candidates: list[dict[str, Any]] = []
                if isinstance(top_obj, dict) and isinstance(top_obj.get("upper"), dict):
                    candidates.append(top_obj["upper"])
                for tr in page.get("top_replies") or []:
                    if isinstance(tr, dict):
                        candidates.append(tr)
                for c in candidates:
                    rpid = str(c.get("rpid_str") or c.get("rpid") or "")
                    if rpid and rpid not in pinned_ids:
                        pinned_ids.add(rpid)
                        seen_ids.add(rpid)
                        pinned.append(c)

            replies = [r for r in (page.get("replies") or []) if isinstance(r, dict)]
            for r in replies:
                rpid = str(r.get("rpid_str") or r.get("rpid") or "")
                if not rpid or rpid in seen_ids:
                    continue
                seen_ids.add(rpid)
                top_collected.append(r)
                if len(top_collected) >= top_n:
                    break

            if len(top_collected) >= top_n:
                break
            if cursor.get("is_end"):
                break
            pag_reply = cursor.get("pagination_reply") or {}
            new_offset = str(pag_reply.get("next_offset") or "")
            if not new_offset or new_offset == next_offset:
                break
            next_offset = new_offset

        # Enrich pinned comments and UP-replied comments with sub-replies
        enriched_pinned = [
            self._enrich_sub_replies(
                client,
                oid=oid,
                rtype=rtype,
                reply=r,
                max_sub=max_sub_replies_per_root,
            )
            for r in pinned
        ]

        up_replied: list[dict[str, Any]] = []
        enriched_top: list[dict[str, Any]] = []
        for r in top_collected[:top_n]:
            if include_up_replies and self._reply_involves_up(r, up_uid):
                enriched = self._enrich_sub_replies(
                    client,
                    oid=oid,
                    rtype=rtype,
                    reply=r,
                    max_sub=max_sub_replies_per_root,
                )
                up_replied.append(enriched)
                enriched_top.append(enriched)
            else:
                enriched_top.append(r)

        return {
            "oid": str(oid),
            "type": rtype,
            "fetched_at": _utc_now_iso(),
            "total_count": total_count,
            "strategy": {
                "top_n": top_n,
                "include_pinned": include_pinned,
                "include_up_replies": include_up_replies,
                "max_sub_replies_per_root": max_sub_replies_per_root,
            },
            "pinned": enriched_pinned,
            "up_replied": up_replied,
            "top": enriched_top,
        }

    def _discover_comment_carriers(self, target: ResolvedTarget) -> list[_CommentCarrier]:
        carriers: list[_CommentCarrier] = []
        enabled_targets = set(target.archive.comments.targets)

        if "videos" in enabled_targets:
            video_dirs = [target.download_dir]
            if (target.download_dir / "videos").is_dir():
                video_dirs.append(target.download_dir / "videos")
            for vdir in video_dirs:
                if not vdir.exists():
                    continue
                for info_file in sorted(vdir.glob("*.info.json"), reverse=True):
                    try:
                        info = json.loads(info_file.read_text(encoding="utf-8", errors="ignore"))
                    except Exception:
                        continue
                    bvid = str(info.get("id") or info.get("display_id") or "")
                    aid = info.get("aid") or bv_to_av(bvid)
                    if not aid:
                        continue
                    stem = info_file.name[: -len(".info.json")]
                    carriers.append(
                        _CommentCarrier(
                            kind="videos",
                            oid=str(aid),
                            rtype=1,
                            title=str(info.get("title") or bvid),
                            json_path=info_file.with_name(f"{stem}.comments.json"),
                            md_path=info_file.with_name(f"{stem}.comments.md"),
                            archive_key=f"video:{aid}",
                            source_url=str(info.get("webpage_url") or f"https://www.bilibili.com/video/{bvid}"),
                        )
                    )

        if "dynamics" in enabled_targets:
            dyn_root = target.download_dir / "dynamics"
            if dyn_root.is_dir():
                for dyn_file in sorted(dyn_root.glob("*/dynamic.json"), reverse=True):
                    try:
                        item = json.loads(dyn_file.read_text(encoding="utf-8", errors="ignore"))
                    except Exception:
                        continue
                    dyn_id = str(item.get("id_str") or "")
                    basic = item.get("basic") or {}
                    oid = str(basic.get("comment_id_str") or dyn_id)
                    rtype = int(basic.get("comment_type") or 17)
                    if not oid:
                        continue
                    carriers.append(
                        _CommentCarrier(
                            kind="dynamics",
                            oid=oid,
                            rtype=rtype,
                            title=f"动态 {dyn_id}",
                            json_path=dyn_file.with_name("comments.json"),
                            md_path=dyn_file.with_name("comments.md"),
                            archive_key=f"dynamic:{rtype}:{oid}",
                            source_url=f"https://t.bilibili.com/{dyn_id}",
                        )
                    )

        if "articles" in enabled_targets:
            art_root = target.download_dir / "articles"
            if art_root.is_dir():
                for art_file in sorted(art_root.glob("*/article.json"), reverse=True):
                    try:
                        payload = json.loads(art_file.read_text(encoding="utf-8", errors="ignore"))
                    except Exception:
                        continue
                    cv_id = str(payload.get("cv_id") or "")
                    meta = payload.get("meta") or {}
                    if not cv_id:
                        continue
                    carriers.append(
                        _CommentCarrier(
                            kind="articles",
                            oid=cv_id,
                            rtype=12,
                            title=str(meta.get("title") or f"专栏 cv{cv_id}"),
                            json_path=art_file.with_name("comments.json"),
                            md_path=art_file.with_name("comments.md"),
                            archive_key=f"article:{cv_id}",
                            source_url=f"https://www.bilibili.com/read/cv{cv_id}",
                        )
                    )

        return carriers

    def sync_comments(
        self,
        target: ResolvedTarget,
        client: BilibiliWebClient,
        uid: int,
        *,
        limit_override: int | None = None,
        full: bool = False,
        dry_run: bool = False,
        refresh: bool = False,
    ) -> ExtendedModuleStats:
        stats = ExtendedModuleStats()
        self._emit("ext_module_start", target=target, module="comments")
        ccfg = target.archive.comments
        if ccfg.strategy == "off":
            self._emit("ext_module_done", target=target, module="comments", stats=stats)
            return stats

        carriers = self._discover_comment_carriers(target)
        archived = self.state.read_namespace_archive(target.id, "comments")
        effective_limit = None if full else limit_override

        for carrier in carriers:
            if effective_limit is not None and stats.saved >= effective_limit:
                break
            stats.discovered += 1
            if not refresh and carrier.archive_key in archived:
                stats.already_archived += 1
                continue

            if dry_run:
                continue

            try:
                bundle = self.fetch_curated_comments(
                    client,
                    oid=carrier.oid,
                    rtype=carrier.rtype,
                    up_uid=uid,
                    top_n=ccfg.top_n,
                    include_pinned=ccfg.include_pinned,
                    include_up_replies=ccfg.include_up_replies,
                    max_sub_replies_per_root=ccfg.max_sub_replies_per_root,
                )
                _write_json(carrier.json_path, bundle)
                _write_text(
                    carrier.md_path,
                    render_comments_markdown(
                        title=carrier.title,
                        oid=carrier.oid,
                        rtype=carrier.rtype,
                        bundle=bundle,
                        up_uid=uid,
                    ),
                )
                carrier_kind_singular = {"videos": "video", "dynamics": "dynamic", "articles": "article"}.get(
                    carrier.kind, carrier.kind
                )
                with self._open_db() as db:
                    db.upsert_comments(
                        target_id=target.id,
                        carrier_kind=carrier_kind_singular,
                        oid=carrier.oid,
                        rtype=carrier.rtype,
                        carrier_title=carrier.title,
                        source_url=carrier.source_url,
                        bundle=bundle,
                        up_uid=uid,
                    )
                self.state.append_namespace_archive(
                    target.id,
                    "comments",
                    carrier.archive_key,
                    meta=_utc_now_iso(),
                )
                archived.add(carrier.archive_key)
                self.state.remove_failure(target.id, carrier.source_url, item_type="comment")
                stats.saved += 1
            except (BilibiliRiskControlError, BilibiliAuthError):
                raise
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"comment {carrier.archive_key}: {exc}")
                self.state.record_failure(
                    target_id=target.id,
                    platform=target.platform.value,
                    url=carrier.source_url,
                    video_id=carrier.archive_key,
                    title=f"评论: {carrier.title}",
                    error=str(exc),
                    item_type="comment",
                    extra={
                        "oid": carrier.oid,
                        "rtype": carrier.rtype,
                        "title": carrier.title,
                        "json_path": str(carrier.json_path),
                        "md_path": str(carrier.md_path),
                        "archive_key": carrier.archive_key,
                    },
                )

        self._emit("ext_module_done", target=target, module="comments", stats=stats)
        return stats

    # ------------------------------------------------------------------
    # Coordinator
    # ------------------------------------------------------------------

    def sync_all(
        self,
        target: ResolvedTarget,
        *,
        only: set[str] | None = None,
        limit_override: int | None = None,
        full: bool = False,
        dry_run: bool = False,
        refresh_comments: bool = False,
    ) -> dict[str, ExtendedModuleStats]:
        """Run all selected extended Bilibili archive modules for one target."""
        results: dict[str, ExtendedModuleStats] = {}
        if target.platform != Platform.BILIBILI:
            return results

        cfg = target.archive
        wanted_profile = ("profile" in only) if only is not None else cfg.profile
        wanted_favorites = ("favorites" in only) if only is not None else cfg.favorites
        wanted_articles = ("articles" in only) if only is not None else cfg.articles
        wanted_dynamics = ("dynamics" in only) if only is not None else cfg.dynamics
        wanted_comments = (
            ("comments" in only)
            if only is not None
            else (cfg.comments.enabled and cfg.comments.strategy != "off")
        )

        if not any((wanted_profile, wanted_favorites, wanted_articles, wanted_dynamics, wanted_comments)):
            return results

        uid = extract_bilibili_uid(target.source)
        if uid is None:
            results["profile"] = ExtendedModuleStats(
                failed=1,
                errors=[f"无法从 source={target.source!r} 解析 B 站 UID，跳过扩展归档"],
            )
            return results

        with self._client_factory(target) as client:
            try:
                if wanted_profile:
                    results["profile"] = self.sync_profile(target, client, uid, dry_run=dry_run)
                if wanted_favorites:
                    results["favorites"] = self.sync_favorites(target, client, uid, dry_run=dry_run)
                if wanted_articles:
                    results["articles"] = self.sync_articles(
                        target,
                        client,
                        uid,
                        limit_override=limit_override,
                        full=full,
                        dry_run=dry_run,
                    )
                if wanted_dynamics:
                    results["dynamics"] = self.sync_dynamics(
                        target,
                        client,
                        uid,
                        limit_override=limit_override,
                        full=full,
                        dry_run=dry_run,
                    )
                if wanted_comments:
                    results["comments"] = self.sync_comments(
                        target,
                        client,
                        uid,
                        limit_override=limit_override,
                        full=full,
                        dry_run=dry_run,
                        refresh=refresh_comments,
                    )
            except (BilibiliRiskControlError, BilibiliAuthError) as exc:
                results.setdefault("circuit_breaker", ExtendedModuleStats(failed=1, errors=[str(exc)]))

        return results

    def retry_failed_item(
        self,
        target: ResolvedTarget,
        record: FailedRecord,
    ) -> tuple[str, str]:
        """Retry one non-video failed record (`comment` / `article` / `dynamic`)."""
        uid = extract_bilibili_uid(target.source)
        if uid is None:
            return "failed", f"无法从 {target.source!r} 解析 B 站 UID"

        try:
            with self._client_factory(target) as client:
                if record.item_type == "comment":
                    extra = record.extra or {}
                    oid = str(extra.get("oid") or "")
                    rtype = int(extra.get("rtype") or 1)
                    title = str(extra.get("title") or record.title or oid)
                    json_path = Path(str(extra.get("json_path") or ""))
                    md_path = Path(str(extra.get("md_path") or ""))
                    archive_key = str(extra.get("archive_key") or record.video_id or "")
                    if not oid or not str(json_path):
                        return "failed", "失败记录缺少评论重试元数据"
                    ccfg = target.archive.comments
                    bundle = self.fetch_curated_comments(
                        client,
                        oid=oid,
                        rtype=rtype,
                        up_uid=uid,
                        top_n=ccfg.top_n,
                        include_pinned=ccfg.include_pinned,
                        include_up_replies=ccfg.include_up_replies,
                        max_sub_replies_per_root=ccfg.max_sub_replies_per_root,
                    )
                    _write_json(json_path, bundle)
                    _write_text(
                        md_path,
                        render_comments_markdown(
                            title=title,
                            oid=oid,
                            rtype=rtype,
                            bundle=bundle,
                            up_uid=uid,
                        ),
                    )
                    if archive_key:
                        self.state.append_namespace_archive(
                            target.id,
                            "comments",
                            archive_key,
                            meta=_utc_now_iso(),
                        )
                    self.state.remove_failure(target.id, record.url, item_type="comment")
                    return "downloaded", ""

                if record.item_type == "article":
                    extra = record.extra or {}
                    meta = extra.get("meta")
                    if not isinstance(meta, dict):
                        cv_id = str(extra.get("cv_id") or "").lstrip("cv")
                        meta = {"id": cv_id, "title": record.title or f"cv{cv_id}"}
                    self._save_single_article(target, client, meta)
                    key = f"cv{meta.get('id')}"
                    self.state.append_namespace_archive(target.id, "articles", key)
                    self.state.remove_failure(target.id, record.url, item_type="article")
                    return "downloaded", ""

                if record.item_type == "dynamic":
                    dyn_id = str((record.extra or {}).get("dynamic_id") or record.video_id or "")
                    if not dyn_id:
                        return "failed", "失败记录缺少 dynamic_id"
                    detail = client.get_json(
                        "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
                        params={"id": dyn_id, "features": "itemOpusStyle"},
                        signed=True,
                        referer=f"https://t.bilibili.com/{dyn_id}",
                    )
                    item = detail.get("item") if isinstance(detail.get("item"), dict) else None
                    if not item:
                        return "failed", "未获取到动态详情"
                    self._save_single_dynamic(target, client, item)
                    self.state.append_namespace_archive(target.id, "dynamics", dyn_id)
                    self.state.remove_failure(target.id, record.url, item_type="dynamic")
                    return "downloaded", ""

                return "failed", f"不支持的失败重试类型: {record.item_type}"
        except Exception as exc:
            self.state.record_failure(
                target_id=target.id,
                platform=target.platform.value,
                url=record.url,
                video_id=record.video_id,
                title=record.title,
                error=str(exc),
                item_type=record.item_type,
                extra=dict(record.extra or {}),
            )
            return "failed", str(exc)
