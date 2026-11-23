"""Bilibili WBI request parameter signing."""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

# Official Bilibili WBI mixin permutation table (64 indices -> take first 32).
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

_FILTER_CHARS = "!'()*"


def get_mixin_key(orig: str) -> str:
    """Derive 32-char WBI mixin key from concatenated img_key + sub_key."""
    if len(orig) < 64:
        raise ValueError(f"WBI 原始密钥长度不足 64: got {len(orig)}")
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def extract_wbi_keys(nav_data: dict[str, Any]) -> tuple[str, str]:
    """Extract (img_key, sub_key) from /x/web-interface/nav `data` payload."""
    wbi_img = nav_data.get("wbi_img")
    if not isinstance(wbi_img, dict):
        raise ValueError("nav 响应缺少 wbi_img 字段")

    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    if not img_url or not sub_url:
        raise ValueError("nav 响应中 img_url 或 sub_url 为空")

    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    if not img_key or not sub_key:
        raise ValueError("无法从 wbi_img URL 中提取 img_key / sub_key")
    return img_key, sub_key


def sign_wbi_params(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    *,
    wts: int | None = None,
) -> dict[str, str]:
    """Return a new query dict signed with `wts` and `w_rid`."""
    mixin_key = get_mixin_key(img_key + sub_key)
    curr_time = int(time.time()) if wts is None else int(wts)

    prepared: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        cleaned = "".join(ch for ch in str(v) if ch not in _FILTER_CHARS)
        prepared[str(k)] = cleaned
    prepared["wts"] = str(curr_time)

    sorted_items = sorted(prepared.items(), key=lambda item: item[0])
    query = urllib.parse.urlencode(sorted_items, quote_via=urllib.parse.quote)
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()

    signed = dict(sorted_items)
    signed["w_rid"] = w_rid
    return signed
