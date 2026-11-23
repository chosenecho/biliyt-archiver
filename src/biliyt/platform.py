"""Platform detection and target URL normalization for Bilibili and YouTube."""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlparse


class Platform(str, Enum):
    BILIBILI = "bilibili"
    YOUTUBE = "youtube"


_YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{2,}$")
_BILIBILI_UID_RE = re.compile(r"^(?:(?:uid|bili):)?(\d{1,20})$", re.IGNORECASE)
_YOUTUBE_TAB_PATHS = {"videos", "shorts", "streams", "playlists", "releases", "featured", "community"}


def detect_platform(source: str) -> Platform:
    """Infer platform from a user-provided URL, UID, handle, or channel ID."""
    cleaned = source.strip()
    lowered = cleaned.lower()

    if any(host in lowered for host in ("bilibili.com", "b23.tv", "bilibili.tv", "biliintl.com")):
        return Platform.BILIBILI
    if any(host in lowered for host in ("youtube.com", "youtu.be")):
        return Platform.YOUTUBE
    if _YOUTUBE_HANDLE_RE.match(cleaned) or _YOUTUBE_CHANNEL_ID_RE.match(cleaned):
        return Platform.YOUTUBE
    if _BILIBILI_UID_RE.match(cleaned):
        return Platform.BILIBILI

    raise ValueError(
        f"无法自动识别平台: {source!r}。请在配置中显式指定 platform: bilibili 或 youtube。"
    )


def normalize_target_url(
    source: str,
    platform: Platform,
    youtube_tab: str = "videos",
) -> str:
    """Normalize shorthand user identifiers or profile links into yt-dlp list URLs."""
    cleaned = source.strip()

    if platform == Platform.BILIBILI:
        uid_match = _BILIBILI_UID_RE.match(cleaned)
        if uid_match:
            uid = uid_match.group(1)
            return f"https://space.bilibili.com/{uid}/upload/video"

        if not cleaned.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"

        parsed = urlparse(cleaned)
        host = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        if host == "space.bilibili.com":
            # Bare space profile URL -> user's uploaded videos page
            if re.fullmatch(r"/\d+", path):
                return f"https://space.bilibili.com{path}/upload/video"
        return cleaned

    if platform == Platform.YOUTUBE:
        if _YOUTUBE_HANDLE_RE.match(cleaned):
            base = f"https://www.youtube.com/{cleaned}"
            return base if youtube_tab == "raw" else f"{base}/{youtube_tab}"

        if _YOUTUBE_CHANNEL_ID_RE.match(cleaned):
            base = f"https://www.youtube.com/channel/{cleaned}"
            return base if youtube_tab == "raw" else f"{base}/{youtube_tab}"

        if not cleaned.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"

        if youtube_tab == "raw":
            return cleaned

        parsed = urlparse(cleaned)
        host = parsed.netloc.lower()
        if "youtube.com" not in host:
            return cleaned

        path = parsed.path.rstrip("/")
        # Leave playlists, single videos, or URLs that already specify a channel tab untouched.
        if parsed.query or path.startswith(("/watch", "/playlist", "/shorts/")):
            return cleaned

        segments = [seg for seg in path.split("/") if seg]
        if not segments:
            return cleaned
        if segments[-1].lower() in _YOUTUBE_TAB_PATHS:
            return cleaned

        is_channel_root = (
            (len(segments) == 1 and segments[0].startswith("@"))
            or (len(segments) == 2 and segments[0] in {"channel", "c", "user"})
        )
        if is_channel_root:
            return f"{parsed.scheme}://{parsed.netloc}{path}/{youtube_tab}"

        return cleaned

    return cleaned
