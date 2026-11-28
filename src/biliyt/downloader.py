"""Core two-stage probe, incremental sync, and failure-retry engine built on yt-dlp."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

from .artifacts import Artifact, ArtifactManifest, ArtifactStore, DownloadResult, clean_error
from .bilibili.archiver import BilibiliArchiver, ExtendedProbeSummary
from .config import ResolvedTarget
from .options import build_daterange, build_download_opts, build_probe_opts, evaluate_video_filters
from .platform import Platform
from .state import RunReport, StateStore, TargetRunSummary


@dataclass
class ProbeEntry:
    url: str
    video_id: str | None
    title: str
    status: str  # "pending" | "archived" | "filtered"
    reason: str = ""
    duration: float | None = None
    upload_date: str | None = None


@dataclass
class ProbeResult:
    target: ResolvedTarget
    entries: list[ProbeEntry] = field(default_factory=list)
    error: str | None = None
    early_stopped: bool = False

    @property
    def pending(self) -> list[ProbeEntry]:
        return [e for e in self.entries if e.status == "pending"]

    @property
    def archived_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "archived")

    @property
    def filtered_count(self) -> int:
        return sum(1 for e in self.entries if e.status == "filtered")


class _CaptureLogger:
    """Capture yt-dlp log messages so errors can be surfaced cleanly and persisted."""

    def __init__(self, on_message: Callable[[str, str], None] | None = None) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self._on_message = on_message

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            return

    def info(self, msg: str) -> None:
        msg = clean_error(msg)
        if self._on_message:
            self._on_message("info", msg)

    def warning(self, msg: str) -> None:
        msg = clean_error(msg)
        self.warnings.append(msg)
        if self._on_message:
            self._on_message("warning", msg)

    def error(self, msg: str) -> None:
        msg = clean_error(msg)
        self.errors.append(msg)
        if self._on_message:
            self._on_message("error", msg)


def _entry_url(entry: dict[str, Any], platform: Platform, fallback_url: str) -> str:
    url = entry.get("webpage_url") or entry.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url

    video_id = entry.get("id")
    if isinstance(video_id, str) and video_id:
        if platform == Platform.YOUTUBE:
            return f"https://www.youtube.com/watch?v={video_id}"
        if platform == Platform.BILIBILI:
            if video_id.upper().startswith(("BV", "AV")):
                return f"https://www.bilibili.com/video/{video_id}"
            return f"https://www.bilibili.com/video/av{video_id}"
    return fallback_url


def _matches_archive(
    video_id: str | None,
    archived_ids: set[str],
    *,
    expected_parts: int | None = None,
) -> bool:
    if not video_id:
        return False
    if video_id in archived_ids:
        return True
    # Bilibili multi-part entries can be stored as `<bvid>_p1`, `<bvid>_p2`, ...
    prefix = f"{video_id}_"
    matched_parts = [aid for aid in archived_ids if aid.startswith(prefix)]
    if not matched_parts:
        return False
    if expected_parts is not None and expected_parts > 1:
        return len(matched_parts) >= expected_parts
    return True


def _expected_part_count(entry: dict[str, Any]) -> int | None:
    for key in ("playlist_count", "n_entries"):
        raw = entry.get(key)
        if isinstance(raw, int) and raw > 1:
            return raw
    return None


def _is_subtitle_download_error(message: str) -> bool:
    return "Unable to download video subtitles" in message


def _without_subtitles(opts: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(opts)
    fallback["writesubtitles"] = False
    fallback["writeautomaticsub"] = False
    fallback.pop("subtitleslangs", None)
    fallback.pop("sleep_interval_subtitles", None)
    postprocessors = fallback.get("postprocessors")
    if isinstance(postprocessors, list):
        fallback["postprocessors"] = [
            pp
            for pp in postprocessors
            if pp.get("key") not in {"FFmpegSubtitlesConvertor", "FFmpegEmbedSubtitle"}
        ]
    return fallback


class _CollectArtifactsPP(PostProcessor):
    """Observe final info after yt-dlp has merged, converted and moved all files."""

    def __init__(self, callback: Callable[[DownloadResult], None]) -> None:
        super().__init__()
        self.callback = callback

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        final_media = Path(info["filepath"]) if info.get("filepath") else None
        moves = info.get("__files_to_move") or {}

        def final_path(raw: Any) -> Path | None:
            if not raw:
                return None
            path = Path(str(moves.get(str(raw)) or raw))
            if not path.is_file() and final_media:
                path = final_media.parent / path.name
            return path if path.is_file() else None

        subtitles = info.get("requested_subtitles") or {}
        result = DownloadResult(
            item_id=str(info.get("id") or ""),
            media_path=final_path(info.get("filepath")),
            info_path=final_path(info.get("infojson_filename") or info.get("__infojson_filename")),
            subtitle_paths={
                lang: path for lang, sub in subtitles.items()
                if (path := final_path(sub.get("filepath"))) is not None
            },
            requested_languages=list(subtitles),
        )
        for thumbnail in reversed(info.get("thumbnails") or []):
            if path := final_path(thumbnail.get("filepath")):
                result.thumbnail_path = path
                break
        self.callback(result)
        return [], info


class SyncEngine:
    """Orchestrates target probing, incremental downloading, and failure retries."""

    def __init__(
        self,
        state: StateStore,
        *,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
        bili_archiver: BilibiliArchiver | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state = state
        self._ydl_factory = ydl_factory or yt_dlp.YoutubeDL
        self._on_event = on_event
        self._sleeper = sleeper
        self._event_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self.bili_archiver = bili_archiver or BilibiliArchiver(state, on_event=on_event)

    def _emit(self, event: str, **payload: Any) -> None:
        if self._on_event:
            with self._event_lock:
                self._on_event(event, payload)

    def probe_extended_bilibili(
        self,
        target: ResolvedTarget,
        *,
        limit_override: int | None = None,
    ) -> ExtendedProbeSummary:
        return self.bili_archiver.probe_extended(target, limit_override=limit_override)

    def probe_target(
        self,
        target: ResolvedTarget,
        *,
        limit_override: int | None = None,
        full: bool = False,
    ) -> ProbeResult:
        logger = _CaptureLogger()
        opts = build_probe_opts(target, limit_override=limit_override, full=full, logger=logger)
        result = ProbeResult(target=target)
        archived_ids = self.state.read_archive_video_ids(target.id)
        daterange = build_daterange(target.filters)
        early_stop_after = None if full else target.filters.early_stop_after

        try:
            with self._ydl_factory(opts) as ydl:
                info = ydl.extract_info(target.url, download=False)
                if not info:
                    result.error = "; ".join(logger.errors) or "未获取到目标投稿信息"
                    return result

                raw_entries = info.get("entries")
                if raw_entries is None:
                    entries_iter: Any = [info]
                else:
                    # Keep lazy iteration so breaking on early-stop avoids fetching extra pages
                    entries_iter = (e for e in raw_entries if isinstance(e, dict))

                consecutive_archived = 0
                for entry in entries_iter:
                    video_id = str(entry.get("id")) if entry.get("id") is not None else None
                    title = str(entry.get("title") or video_id or "Untitled")
                    url = _entry_url(entry, target.platform, target.url)
                    duration = entry.get("duration")
                    upload_date = entry.get("upload_date")
                    expected_parts = _expected_part_count(entry)

                    if _matches_archive(video_id, archived_ids, expected_parts=expected_parts):
                        result.entries.append(
                            ProbeEntry(
                                url=url,
                                video_id=video_id,
                                title=title,
                                status="archived",
                                reason="已在归档记录中",
                                duration=duration,
                                upload_date=upload_date,
                            )
                        )
                        consecutive_archived += 1
                        if early_stop_after is not None and consecutive_archived >= early_stop_after:
                            result.early_stopped = True
                            break
                        continue

                    consecutive_archived = 0

                    if daterange is not None and isinstance(upload_date, str) and len(upload_date) == 8:
                        if upload_date not in daterange:
                            result.entries.append(
                                ProbeEntry(
                                    url=url,
                                    video_id=video_id,
                                    title=title,
                                    status="filtered",
                                    reason=f"发布日期不在范围内 ({upload_date})",
                                    duration=duration,
                                    upload_date=upload_date,
                                )
                            )
                            continue

                    skip_reason = evaluate_video_filters(
                        entry, target.filters, target.platform, incomplete=True
                    )
                    if skip_reason:
                        result.entries.append(
                            ProbeEntry(
                                url=url,
                                video_id=video_id,
                                title=title,
                                status="filtered",
                                reason=skip_reason,
                                duration=duration,
                                upload_date=upload_date,
                            )
                        )
                        continue

                    result.entries.append(
                        ProbeEntry(
                            url=url,
                            video_id=video_id,
                            title=title,
                            status="pending",
                            duration=duration,
                            upload_date=upload_date,
                        )
                    )
        except Exception as exc:
            result.error = str(exc)
            return result

        return result

    def _download_single(
        self,
        target: ResolvedTarget,
        url: str,
        *,
        video_id: str | None = None,
        title: str | None = None,
    ) -> tuple[str, str]:
        """Download one candidate URL. Returns `(outcome, detail)` where outcome in {'downloaded','filtered','archived','failed'}."""
        target.download_dir.mkdir(parents=True, exist_ok=True)
        if target.temp_dir is not None:
            target.temp_dir.mkdir(parents=True, exist_ok=True)

        # Use an isolated per-download archive file and merge new lines back under StateStore's lock
        # so concurrent workers never race on the shared archive text file.
        self.state.ensure_dirs()
        base_entries = self.state.read_archive_entries(target.id)
        temp_archive = self.state.archives_dir / f".tmp_{target.id}_{uuid.uuid4().hex}.txt"
        if base_entries:
            temp_archive.write_text("\n".join(sorted(base_entries)) + "\n", encoding="utf-8")

        def _merge_temp_archive() -> list[str]:
            if not temp_archive.exists():
                return []
            new_entries: list[str] = []
            for line in temp_archive.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped and stripped not in base_entries:
                    new_entries.append(stripped)
            if not new_entries:
                return []
            return self.state.append_archive_entries(target.id, new_entries)

        downloaded_flag = False
        logger = _CaptureLogger()
        results: list[DownloadResult] = []
        subtitle_error = ""

        def collect(ydl: Any) -> None:
            if hasattr(ydl, "add_post_processor"):
                ydl.add_post_processor(_CollectArtifactsPP(results.append), when="after_move")

        def _progress_hook(data: dict[str, Any]) -> None:
            nonlocal downloaded_flag
            status = data.get("status")
            if status == "finished":
                downloaded_flag = True
            self._emit("progress", target_id=target.id, url=url, title=title, data=data)

        opts = build_download_opts(
            target,
            archive_path=temp_archive,
            simulate=False,
            progress_hooks=[_progress_hook],
            logger=logger,
        )

        try:
            try:
                with self._ydl_factory(opts) as ydl:
                    collect(ydl)
                    ydl.download([url])
            except yt_dlp.utils.DownloadError as exc:
                err_msg = clean_error(exc)
                if _is_subtitle_download_error(err_msg) and (
                    opts.get("writesubtitles") or opts.get("writeautomaticsub")
                ):
                    self._emit(
                        "item_warning",
                        target_id=target.id,
                        url=url,
                        title=title,
                        message=f"字幕下载失败（将跳过字幕继续下载视频）: {err_msg}",
                    )
                    subtitle_error = clean_error(err_msg)
                    logger.errors.clear()
                    fallback_opts = _without_subtitles(opts)
                    try:
                        with self._ydl_factory(fallback_opts) as ydl:
                            collect(ydl)
                            ydl.download([url])
                    except Exception as fallback_exc:
                        _merge_temp_archive()
                        fb_msg = clean_error(fallback_exc)
                        self.state.record_failure(
                            target_id=target.id,
                            platform=target.platform.value,
                            url=url,
                            video_id=video_id,
                            title=title,
                            error=fb_msg,
                        )
                        return "failed", fb_msg
                else:
                    _merge_temp_archive()
                    self.state.record_failure(
                        target_id=target.id,
                        platform=target.platform.value,
                        url=url,
                        video_id=video_id,
                        title=title,
                        error=err_msg,
                    )
                    return "failed", err_msg
            except Exception as exc:
                _merge_temp_archive()
                err_msg = clean_error(exc)
                self.state.record_failure(
                    target_id=target.id,
                    platform=target.platform.value,
                    url=url,
                    video_id=video_id,
                    title=title,
                    error=err_msg,
                )
                return "failed", err_msg

            added_entries = _merge_temp_archive()
        finally:
            try:
                if temp_archive.exists():
                    temp_archive.unlink()
            except OSError:
                pass

        if downloaded_flag or bool(added_entries):
            self.state.remove_failure(target.id, url)
            try:
                self._index_downloaded_video(
                    target, video_id, results=results, source_url=url, subtitle_error=subtitle_error,
                )
            except Exception as exc:
                detail = f"媒体已保存，产物清单写入失败: {clean_error(exc)}"
                self.state.record_failure(target_id=target.id, platform=target.platform.value,
                                          url=url, video_id=video_id, title=title, error=detail)
                self._emit("item_warning", target_id=target.id, url=url, message=detail)
                return "failed", detail
            return "downloaded", "ok"

        # If yt-dlp exited cleanly without downloading or archiving, it was either
        # skipped by full-metadata filter / daterange or already archived.
        if logger.errors:
            err_msg = "; ".join(logger.errors)
            self.state.record_failure(
                target_id=target.id,
                platform=target.platform.value,
                url=url,
                video_id=video_id,
                title=title,
                error=err_msg,
            )
            return "failed", err_msg

        self.state.remove_failure(target.id, url)
        return "filtered", "被过滤条件或日期区间跳过"

    def _index_downloaded_video(
        self, target: ResolvedTarget, video_id: str | None, *,
        results: list[DownloadResult] | None = None, source_url: str = "", subtitle_error: str = "",
    ) -> None:
        """Persist actual artifact coordinates before attempting the derived SQLite index."""
        from .db import ArchiveDB, index_video_info_file

        store = ArtifactStore(target.download_dir, target.id)
        collected = {r.item_id: r for r in (results or []) if r.item_id}
        if not collected:
            # Legacy/custom adapters may not implement postprocessors. Match JSON identity,
            # never the user's configurable filename template.
            for path in target.download_dir.rglob("*.info.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    item_id = str(data.get("id") or "")
                except (OSError, ValueError, AttributeError):
                    continue
                if video_id and (item_id == video_id or item_id.startswith(f"{video_id}_p")):
                    collected[item_id] = DownloadResult(item_id, info_path=path)
        if not collected and video_id:
            collected[video_id] = DownloadResult(video_id)
        for result in collected.values():
            artifacts = []
            for kind, path, enabled in (
                ("media", result.media_path, True),
                ("metadata", result.info_path, target.media.write_info_json),
                ("thumbnail", result.thumbnail_path, target.media.write_thumbnail),
            ):
                artifacts.append(Artifact(
                    kind, "complete" if path else ("unknown" if enabled else "disabled"),
                    store.relative(path) if path else None,
                ))
            for language, path in result.subtitle_paths.items():
                artifacts.append(Artifact("subtitle", "complete", store.relative(path), language=language))
            if subtitle_error:
                for language in target.media.subtitle_langs:
                    if language not in result.subtitle_paths:
                        artifacts.append(Artifact("subtitle", "failed", language=language, error=subtitle_error))
            elif not result.subtitle_paths:
                artifacts.append(Artifact(
                    "subtitle", "unavailable" if (target.media.write_subtitles or target.media.write_auto_subtitles) else "disabled",
                ))
            if target.media.embed_subtitles and (target.media.write_subtitles or target.media.write_auto_subtitles):
                artifacts.append(Artifact(
                    "subtitle_embed", "pending" if subtitle_error else ("complete" if result.media_path else "unknown"),
                    store.relative(result.media_path) if result.media_path and not subtitle_error else None,
                    error="独立字幕补齐后仍需重封装；当前 repair 不修改已保存视频" if subtitle_error else "",
                ))
            manifest = ArtifactManifest(
                target.id, "video", result.item_id, source_url, artifacts,
                index_status="pending" if target.media.write_info_json else "disabled",
                context={"metadata_path": store.relative(result.info_path)} if result.info_path else {},
            )
            store.save(manifest)
            if manifest.index_status == "disabled":
                self._emit("item_warning", target_id=target.id, url=source_url,
                           message="元数据保存已关闭，此条目的离线重建和索引不可用")
                continue
            try:
                if not result.info_path:
                    raise ValueError("下载完成但未获取元数据文件路径")
                with self._db_lock:
                    with ArchiveDB(self.state.state_dir / "biliyt.db") as db:
                        paths = {"media": result.media_path, "thumbnail": result.thumbnail_path}
                        if result.subtitle_paths:
                            paths["subtitle"] = next(iter(result.subtitle_paths.values()))
                        if not index_video_info_file(db, target, result.info_path, paths=paths):
                            raise ValueError("元数据格式异常，索引待重试")
                manifest.index_status = "complete"
            except Exception as exc:
                manifest.index_status = "failed"
                manifest.index_error = clean_error(exc)
                self._emit("item_warning", target_id=target.id, url=source_url,
                           message=f"视频已保存，索引待修复: {manifest.index_error}")
            store.save(manifest)

    def _summarize_artifacts(self, target: ResolvedTarget, summary: TargetRunSummary) -> None:
        try:
            manifest_errors: list[str] = []
            stats = ArtifactStore(target.download_dir, target.id).stats(errors=manifest_errors)
            summary.errors.extend(manifest_errors)
            summary.failed += len(manifest_errors)
            summary.partial = stats["partial"]
            summary.index_failed = stats["index_failed"]
            summary.pending_repair = stats["pending_repair"]
            for manifest in ArtifactStore(target.download_dir, target.id).manifests():
                if manifest.index_error:
                    summary.warnings.append(f"{manifest.kind}/{manifest.item_id}: {manifest.index_error}")
                for artifact in manifest.artifacts:
                    if artifact.status in {"failed", "pending"}:
                        summary.warnings.append(
                            f"{manifest.kind}/{manifest.item_id} {artifact.kind}"
                            f" {artifact.language or ''}: {artifact.error or '待补齐'}"
                        )
        except Exception as exc:
            summary.errors.append(f"归档清单读取失败: {clean_error(exc)}")
            summary.failed += 1

    def _sleep_between_items(self, target: ResolvedTarget) -> None:
        low = target.rate_limit.sleep_interval
        high = max(low, target.rate_limit.max_sleep_interval)
        if high > 0:
            delay = random.uniform(low, high)
            self._emit("sleep", target_id=target.id, seconds=delay)
            self._sleeper(delay)

    def sync_target(
        self,
        target: ResolvedTarget,
        *,
        dry_run: bool = False,
        limit_override: int | None = None,
        only: set[str] | None = None,
        full: bool = False,
        refresh_comments: bool = False,
    ) -> TargetRunSummary:
        summary = TargetRunSummary(
            target_id=target.id,
            target_name=target.name,
            platform=target.platform.value,
            url=target.url,
            dry_run=dry_run,
        )

        run_videos = (
            ("videos" in only)
            if only is not None
            else (target.platform != Platform.BILIBILI or target.archive.videos)
        )

        if run_videos:
            self._emit("probe_start", target=target)
            # In sync mode, `--limit` caps new downloads rather than truncating the probe scan window
            # so `--full --limit N` can incrementally backfill historical unarchived videos in batches.
            probe = self.probe_target(target, limit_override=None, full=full)
            if probe.error:
                summary.failed += 1
                summary.errors.append(probe.error)
                summary.module_stats["videos"] = {
                    "discovered": 0,
                    "already_archived": 0,
                    "saved": 0,
                    "failed": 1,
                }
                self._emit("target_error", target=target, error=probe.error)
            else:
                v_discovered = len(probe.entries)
                v_archived = probe.archived_count
                v_filtered = probe.filtered_count
                v_downloaded = 0
                v_failed = 0
                candidates = probe.pending

                if full:
                    max_dl = limit_override
                else:
                    max_dl = target.filters.max_downloads
                    if limit_override is not None and (max_dl is None or limit_override < max_dl):
                        max_dl = limit_override

                self._emit("probe_done", target=target, probe=probe, max_downloads=max_dl)

                if not dry_run and candidates:
                    workers = min(max(1, target.rate_limit.concurrency), len(candidates))
                    if workers == 1:
                        for idx, item in enumerate(candidates, start=1):
                            if max_dl is not None and v_downloaded >= max_dl:
                                break

                            self._emit(
                                "item_start",
                                target=target,
                                index=idx,
                                total=len(candidates),
                                item=item,
                            )
                            outcome, detail = self._download_single(
                                target,
                                item.url,
                                video_id=item.video_id,
                                title=item.title,
                            )
                            if outcome == "downloaded":
                                v_downloaded += 1
                            elif outcome == "filtered":
                                v_filtered += 1
                            elif outcome == "archived":
                                v_archived += 1
                            else:
                                v_failed += 1
                                summary.errors.append(f"{item.title}: {detail}")

                            self._emit(
                                "item_done",
                                target=target,
                                index=idx,
                                total=len(candidates),
                                item=item,
                                outcome=outcome,
                                detail=detail,
                            )

                            has_more = idx < len(candidates) and (max_dl is None or v_downloaded < max_dl)
                            if has_more and outcome in {"downloaded", "failed"}:
                                self._sleep_between_items(target)
                    else:
                        queue_lock = threading.Lock()
                        next_pos = 0
                        in_flight = 0

                        def _worker(worker_idx: int) -> None:
                            nonlocal next_pos, in_flight, v_downloaded, v_filtered, v_archived, v_failed
                            if worker_idx > 0:
                                self._sleeper(worker_idx * 0.5)
                            while True:
                                with queue_lock:
                                    if next_pos >= len(candidates):
                                        return
                                    if max_dl is not None and (v_downloaded + in_flight) >= max_dl:
                                        return
                                    idx = next_pos + 1
                                    item = candidates[next_pos]
                                    next_pos += 1
                                    in_flight += 1

                                self._emit(
                                    "item_start",
                                    target=target,
                                    index=idx,
                                    total=len(candidates),
                                    item=item,
                                )
                                try:
                                    outcome, detail = self._download_single(
                                        target,
                                        item.url,
                                        video_id=item.video_id,
                                        title=item.title,
                                    )
                                finally:
                                    with queue_lock:
                                        in_flight -= 1

                                with queue_lock:
                                    if outcome == "downloaded":
                                        v_downloaded += 1
                                    elif outcome == "filtered":
                                        v_filtered += 1
                                    elif outcome == "archived":
                                        v_archived += 1
                                    else:
                                        v_failed += 1
                                        summary.errors.append(f"{item.title}: {detail}")
                                    has_more = next_pos < len(candidates) and (
                                        max_dl is None or (v_downloaded + in_flight) < max_dl
                                    )

                                self._emit(
                                    "item_done",
                                    target=target,
                                    index=idx,
                                    total=len(candidates),
                                    item=item,
                                    outcome=outcome,
                                    detail=detail,
                                )

                                if has_more and outcome in {"downloaded", "failed"}:
                                    self._sleep_between_items(target)

                        with ThreadPoolExecutor(max_workers=workers) as pool:
                            futures = [pool.submit(_worker, wi) for wi in range(workers)]
                            for fut in futures:
                                fut.result()

                summary.discovered += v_discovered
                summary.already_archived += v_archived
                summary.filtered_out += v_filtered
                summary.downloaded += v_downloaded
                summary.failed += v_failed
                summary.module_stats["videos"] = {
                    "discovered": v_discovered,
                    "already_archived": v_archived,
                    "saved": v_downloaded,
                    "failed": v_failed,
                }
        else:
            self._emit("probe_start", target=target)

        if target.platform == Platform.BILIBILI:
            ext_results = self.bili_archiver.sync_all(
                target,
                only=only,
                limit_override=limit_override,
                full=full,
                dry_run=dry_run,
                refresh_comments=refresh_comments,
            )
            for mod_name, mod_stats in ext_results.items():
                summary.module_stats[mod_name] = mod_stats.to_dict()
                summary.discovered += mod_stats.discovered
                summary.already_archived += mod_stats.already_archived
                summary.downloaded += mod_stats.saved
                summary.failed += mod_stats.failed
                summary.errors.extend(mod_stats.errors)

        if not dry_run:
            self._summarize_artifacts(target, summary)
        return summary

    def retry_failed(
        self,
        targets_by_id: dict[str, ResolvedTarget],
        *,
        target_ids: set[str] | None = None,
        dry_run: bool = False,
    ) -> list[TargetRunSummary]:
        records = self.state.load_failed()
        grouped: dict[str, list[Any]] = {}
        for rec in records:
            if target_ids and rec.target_id not in target_ids:
                continue
            if rec.target_id not in targets_by_id:
                continue
            grouped.setdefault(rec.target_id, []).append(rec)

        summaries: list[TargetRunSummary] = []
        for tid, recs in grouped.items():
            target = targets_by_id[tid]
            summary = TargetRunSummary(
                target_id=target.id,
                target_name=target.name,
                platform=target.platform.value,
                url=target.url,
                discovered=len(recs),
                dry_run=dry_run,
            )
            if dry_run:
                summaries.append(summary)
                continue

            for idx, rec in enumerate(recs, start=1):
                self._emit(
                    "retry_start",
                    target=target,
                    index=idx,
                    total=len(recs),
                    record=rec,
                )
                if rec.item_type == "video":
                    outcome, detail = self._download_single(
                        target,
                        rec.url,
                        video_id=rec.video_id,
                        title=rec.title,
                    )
                else:
                    outcome, detail = self.bili_archiver.retry_failed_item(target, rec)
                if outcome == "downloaded":
                    summary.downloaded += 1
                elif outcome == "filtered":
                    summary.filtered_out += 1
                else:
                    summary.failed += 1
                    summary.errors.append(f"{rec.title or rec.url}: {detail}")

                self._emit(
                    "retry_done",
                    target=target,
                    index=idx,
                    total=len(recs),
                    record=rec,
                    outcome=outcome,
                    detail=detail,
                )
                if idx < len(recs):
                    self._sleep_between_items(target)

            self._summarize_artifacts(target, summary)
            summaries.append(summary)

        return summaries


def build_run_report(mode: str, started_at: datetime, summaries: list[TargetRunSummary]) -> RunReport:
    return RunReport(
        started_at=started_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        finished_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        mode=mode,
        targets=summaries,
    )
