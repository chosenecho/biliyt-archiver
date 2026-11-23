"""Build yt-dlp YoutubeDL option dictionaries from resolved target configs."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DateRange, parse_filesize

from .config import FilterConfig, MediaConfig, ResolvedTarget
from .platform import Platform


def detect_js_runtimes(setting: str | None) -> dict[str, dict[str, str | None]]:
    """Resolve yt-dlp `js_runtimes` option for YouTube EJS challenge solving."""
    if not setting or setting.lower() == "none":
        return {}

    if setting.lower() == "auto":
        deno = shutil.which("deno")
        if deno:
            return {"deno": {"path": deno}}
        node = shutil.which("node")
        if node:
            return {"node": {"path": node}}
        return {}

    if ":" in setting:
        name, path = setting.split(":", 1)
        return {name.strip().lower(): {"path": path.strip() or None}}

    runtime_name = setting.strip().lower()
    detected = shutil.which(runtime_name)
    return {runtime_name: {"path": detected}}


def parse_cookies_from_browser(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse browser cookie spec like 'chrome', 'edge:Default', 'firefox+keyring:profile::container'."""
    container: str | None = None
    profile: str | None = None
    keyring: str | None = None

    rest = spec.strip()
    if "::" in rest:
        rest, container = rest.split("::", 1)
        container = container.strip() or None
    if ":" in rest:
        rest, profile = rest.split(":", 1)
        profile = profile.strip() or None
    if "+" in rest:
        browser, keyring = rest.split("+", 1)
        browser = browser.strip().lower()
        keyring = keyring.strip().upper() or None
    else:
        browser = rest.strip().lower()

    return (browser, profile, keyring, container)


def build_format_options(media: MediaConfig) -> tuple[str, list[str]]:
    """Return `(format_selector, format_sort)` for yt-dlp."""
    height_filter = f"[height<={media.max_height}]" if media.max_height else ""

    codec_prefix_map = {
        "avc": "avc",
        "hevc": "hev",
        "av1": "av01",
    }
    codec_sort_map = {
        "avc": "vcodec:h264",
        "hevc": "vcodec:h265",
        "av1": "vcodec:av01",
    }

    if media.prefer_codec in codec_prefix_map:
        prefix = codec_prefix_map[media.prefer_codec]
        fmt = (
            f"bv*{height_filter}[vcodec^={prefix}]+ba/"
            f"bv*{height_filter}+ba/"
            f"b{height_filter}/best"
        )
    else:
        fmt = f"bv*{height_filter}+ba/b{height_filter}/best"

    sort_order: list[str] = []
    if media.max_height:
        sort_order.append(f"res:{media.max_height}")
    if media.prefer_codec in codec_sort_map:
        sort_order.append(codec_sort_map[media.prefer_codec])
    if media.container == "mp4":
        sort_order.append("ext:mp4:m4a")
    elif media.container == "webm":
        sort_order.append("ext:webm:opus")

    return fmt, sort_order


def evaluate_video_filters(
    info: dict[str, Any],
    filters: FilterConfig,
    platform: Platform,
    *,
    incomplete: bool = False,
) -> str | None:
    """Return a skip reason string if the video does not satisfy filters, else None."""
    if filters.skip_live:
        live_status = info.get("live_status")
        if info.get("is_live") or live_status in {"is_live", "is_upcoming"}:
            return f"跳过直播/预告 (live_status={live_status or 'is_live'})"

    if filters.skip_shorts and platform == Platform.YOUTUBE:
        url = str(info.get("webpage_url") or info.get("url") or "")
        if "/shorts/" in url:
            return "跳过 YouTube Shorts"
        width = info.get("width")
        height = info.get("height")
        duration = info.get("duration")
        if (
            isinstance(width, (int, float))
            and isinstance(height, (int, float))
            and isinstance(duration, (int, float))
            and height > width
            and duration <= 60
        ):
            return "跳过竖屏短视频 (Shorts)"

    duration = info.get("duration")
    if isinstance(duration, (int, float)):
        if filters.min_duration is not None and duration < filters.min_duration:
            return f"时长过短 ({int(duration)}s < {filters.min_duration}s)"
        if filters.max_duration is not None and duration > filters.max_duration:
            return f"时长过长 ({int(duration)}s > {filters.max_duration}s)"

    view_count = info.get("view_count")
    if filters.min_views is not None and isinstance(view_count, (int, float)):
        if view_count < filters.min_views:
            return f"播放量低于门槛 ({int(view_count)} < {filters.min_views})"

    title = str(info.get("title") or "")
    tags = info.get("tags") or []
    searchable = f"{title} {' '.join(str(t) for t in tags)}".lower()

    if filters.exclude_keywords and searchable.strip():
        for kw in filters.exclude_keywords:
            if kw.strip() and kw.strip().lower() in searchable:
                return f"命中排除关键词: {kw}"

    if filters.include_keywords:
        has_match = any(kw.strip().lower() in searchable for kw in filters.include_keywords if kw.strip())
        if not has_match:
            # In incomplete/flat listings tags may be missing; defer rejection until full info is fetched
            if not incomplete or not tags:
                if not incomplete:
                    return f"未命中包含关键词: {', '.join(filters.include_keywords)}"
                # If title alone already doesn't match and we're in probe, only reject when tags won't help?
                # Wait: keep incomplete lenient so full metadata tags still have a chance to match.
                return None

    return None


def build_match_filter(
    filters: FilterConfig,
    platform: Platform,
) -> Callable[..., str | None]:
    """Create a yt-dlp compatible `match_filter` callable."""

    def _match_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        return evaluate_video_filters(info, filters, platform, incomplete=incomplete)

    return _match_filter


def build_daterange(filters: FilterConfig) -> DateRange | None:
    if not filters.date_after and not filters.date_before:
        return None
    return DateRange(filters.date_after, filters.date_before)


def _apply_common_network_and_auth(opts: dict[str, Any], target: ResolvedTarget) -> None:
    if target.proxy:
        opts["proxy"] = target.proxy

    if target.auth.cookies_file:
        opts["cookiefile"] = target.auth.cookies_file
    elif target.auth.cookies_from_browser:
        opts["cookiesfrombrowser"] = parse_cookies_from_browser(target.auth.cookies_from_browser)

    if target.impersonate:
        opts["impersonate"] = ImpersonateTarget.from_str(target.impersonate)

    if target.platform == Platform.YOUTUBE:
        runtimes = detect_js_runtimes(target.js_runtime)
        if runtimes:
            opts["js_runtimes"] = runtimes

    if target.extractor_args:
        opts["extractor_args"] = target.extractor_args

    if target.rate_limit.sleep_requests > 0:
        opts["sleep_interval_requests"] = target.rate_limit.sleep_requests
    opts["retries"] = target.rate_limit.retries


def build_probe_opts(
    target: ResolvedTarget,
    *,
    limit_override: int | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Build lightweight yt-dlp options for flat listing / probing target feeds."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if logger is not None:
        opts["logger"] = logger

    effective_limit = limit_override if limit_override is not None else target.filters.recent_limit
    if effective_limit is not None:
        opts["playlistend"] = effective_limit

    _apply_common_network_and_auth(opts, target)
    return opts


def build_download_opts(
    target: ResolvedTarget,
    *,
    archive_path: Path | None,
    simulate: bool = False,
    progress_hooks: list[Callable[[dict[str, Any]], None]] | None = None,
    postprocessor_hooks: list[Callable[[dict[str, Any]], None]] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Build complete yt-dlp options for downloading a single video or playlist item."""
    media = target.media
    fmt, sort_order = build_format_options(media)

    postprocessors: list[dict[str, Any]] = []
    if target.platform == Platform.YOUTUBE and media.sponsorblock_remove:
        cats = set(media.sponsorblock_remove)
        postprocessors.append(
            {
                "key": "SponsorBlock",
                "categories": cats,
                "api": "https://sponsor.ajay.app",
                "when": "after_filter",
            }
        )

    if media.write_subtitles and media.subtitle_format != "original":
        postprocessors.append(
            {
                "key": "FFmpegSubtitlesConvertor",
                "format": media.subtitle_format,
                "when": "before_dl",
            }
        )

    if media.write_thumbnail and media.convert_thumbnail != "none":
        postprocessors.append(
            {
                "key": "FFmpegThumbnailsConvertor",
                "format": media.convert_thumbnail,
                "when": "before_dl",
            }
        )

    if target.platform == Platform.YOUTUBE and media.sponsorblock_remove:
        cats = set(media.sponsorblock_remove)
        postprocessors.append(
            {
                "key": "ModifyChapters",
                "remove_sponsor_segments": cats,
                "force_keyframes": False,
                "remove_chapters_patterns": [],
                "remove_ranges": [],
                "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l",
            }
        )

    if media.embed_subtitles and media.write_subtitles:
        postprocessors.append({"key": "FFmpegEmbedSubtitle"})

    if media.embed_metadata or media.embed_chapters:
        postprocessors.append(
            {
                "key": "FFmpegMetadata",
                "add_metadata": media.embed_metadata,
                "add_chapters": media.embed_chapters,
                "add_infojson": "if_exists",
            }
        )

    if media.embed_thumbnail and media.write_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail"})

    subtitle_langs = list(media.subtitle_langs)
    if target.platform == Platform.BILIBILI and media.write_danmaku and "danmaku" not in subtitle_langs:
        subtitle_langs.append("danmaku")

    opts: dict[str, Any] = {
        "format": fmt,
        "format_sort": sort_order,
        "merge_output_format": media.container,
        "paths": {"home": str(target.download_dir)},
        "outtmpl": {"default": target.filename_template},
        "windowsfilenames": True,
        "nooverwrites": True,
        "continuedl": True,
        "ignoreerrors": False,
        "quiet": True,
        "no_warnings": False,
        "simulate": simulate,
        "writeinfojson": media.write_info_json,
        "writethumbnail": media.write_thumbnail,
        "writesubtitles": media.write_subtitles or (target.platform == Platform.BILIBILI and media.write_danmaku),
        "writeautomaticsub": media.write_auto_subtitles and media.write_subtitles,
        "subtitleslangs": subtitle_langs,
        "postprocessors": postprocessors,
        "match_filter": build_match_filter(target.filters, target.platform),
        "concurrent_fragment_downloads": target.rate_limit.concurrent_fragments,
        "sleep_interval": target.rate_limit.sleep_interval,
        "max_sleep_interval": target.rate_limit.max_sleep_interval,
    }

    daterange = build_daterange(target.filters)
    if daterange is not None:
        opts["daterange"] = daterange

    if archive_path is not None and not simulate:
        opts["download_archive"] = str(archive_path)

    if target.rate_limit.rate_limit:
        parsed_limit = parse_filesize(target.rate_limit.rate_limit)
        if parsed_limit:
            opts["ratelimit"] = parsed_limit

    if progress_hooks:
        opts["progress_hooks"] = progress_hooks
    if postprocessor_hooks:
        opts["postprocessor_hooks"] = postprocessor_hooks
    if logger is not None:
        opts["logger"] = logger

    _apply_common_network_and_auth(opts, target)
    return opts
