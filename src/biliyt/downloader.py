"""Core two-stage probe, incremental sync, and failure-retry engine built on yt-dlp."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import yt_dlp

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
        if self._on_message:
            self._on_message("info", msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)
        if self._on_message:
            self._on_message("warning", msg)

    def error(self, msg: str) -> None:
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


def _matches_archive(video_id: str | None, archived_ids: set[str]) -> bool:
    if not video_id:
        return False
    if video_id in archived_ids:
        return True
    # Bilibili multi-part entries can be stored as `<bvid>_p1`
    prefix = f"{video_id}_"
    return any(aid.startswith(prefix) for aid in archived_ids)


class SyncEngine:
    """Orchestrates target probing, incremental downloading, and failure retries."""

    def __init__(
        self,
        state: StateStore,
        *,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state = state
        self._ydl_factory = ydl_factory or yt_dlp.YoutubeDL
        self._on_event = on_event
        self._sleeper = sleeper

    def _emit(self, event: str, **payload: Any) -> None:
        if self._on_event:
            self._on_event(event, payload)

    def probe_target(
        self,
        target: ResolvedTarget,
        *,
        limit_override: int | None = None,
    ) -> ProbeResult:
        logger = _CaptureLogger()
        opts = build_probe_opts(target, limit_override=limit_override, logger=logger)
        result = ProbeResult(target=target)

        try:
            with self._ydl_factory(opts) as ydl:
                info = ydl.extract_info(target.url, download=False)
        except Exception as exc:
            result.error = str(exc)
            return result

        if not info:
            result.error = "; ".join(logger.errors) or "未获取到目标投稿信息"
            return result

        raw_entries = info.get("entries")
        if raw_entries is None:
            entries_iter = [info]
        else:
            entries_iter = [e for e in raw_entries if isinstance(e, dict)]

        archived_ids = self.state.read_archive_video_ids(target.id)
        daterange = build_daterange(target.filters)

        for entry in entries_iter:
            video_id = str(entry.get("id")) if entry.get("id") is not None else None
            title = str(entry.get("title") or video_id or "Untitled")
            url = _entry_url(entry, target.platform, target.url)
            duration = entry.get("duration")
            upload_date = entry.get("upload_date")

            if _matches_archive(video_id, archived_ids):
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
                continue

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
        archive_path = self.state.archive_path(target.id)
        before_entries = self.state.read_archive_entries(target.id)

        downloaded_flag = False
        logger = _CaptureLogger()

        def _progress_hook(data: dict[str, Any]) -> None:
            nonlocal downloaded_flag
            status = data.get("status")
            if status == "finished":
                downloaded_flag = True
            self._emit("progress", target_id=target.id, url=url, title=title, data=data)

        opts = build_download_opts(
            target,
            archive_path=archive_path,
            simulate=False,
            progress_hooks=[_progress_hook],
            logger=logger,
        )

        try:
            with self._ydl_factory(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            err_msg = str(exc)
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
            err_msg = str(exc)
            self.state.record_failure(
                target_id=target.id,
                platform=target.platform.value,
                url=url,
                video_id=video_id,
                title=title,
                error=err_msg,
            )
            return "failed", err_msg

        after_entries = self.state.read_archive_entries(target.id)
        if downloaded_flag or len(after_entries) > len(before_entries):
            self.state.remove_failure(target.id, url)
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
    ) -> TargetRunSummary:
        summary = TargetRunSummary(
            target_id=target.id,
            target_name=target.name,
            platform=target.platform.value,
            url=target.url,
            dry_run=dry_run,
        )

        self._emit("probe_start", target=target)
        probe = self.probe_target(target, limit_override=limit_override)
        if probe.error:
            summary.failed += 1
            summary.errors.append(probe.error)
            self._emit("target_error", target=target, error=probe.error)
            return summary

        summary.discovered = len(probe.entries)
        summary.already_archived = probe.archived_count
        summary.filtered_out = probe.filtered_count
        candidates = probe.pending

        max_dl = target.filters.max_downloads
        if limit_override is not None and (max_dl is None or limit_override < max_dl):
            max_dl = limit_override

        self._emit("probe_done", target=target, probe=probe, max_downloads=max_dl)

        if dry_run:
            return summary

        for idx, item in enumerate(candidates, start=1):
            if max_dl is not None and summary.downloaded >= max_dl:
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
                summary.downloaded += 1
            elif outcome == "filtered":
                summary.filtered_out += 1
            elif outcome == "archived":
                summary.already_archived += 1
            else:
                summary.failed += 1
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

            has_more = idx < len(candidates) and (max_dl is None or summary.downloaded < max_dl)
            if has_more and outcome in {"downloaded", "failed"}:
                self._sleep_between_items(target)

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
                outcome, detail = self._download_single(
                    target,
                    rec.url,
                    video_id=rec.video_id,
                    title=rec.title,
                )
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

            summaries.append(summary)

        return summaries


def build_run_report(mode: str, started_at: datetime, summaries: list[TargetRunSummary]) -> RunReport:
    return RunReport(
        started_at=started_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        finished_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        mode=mode,
        targets=summaries,
    )
