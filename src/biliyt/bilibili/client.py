"""Lightweight Bilibili Web API client using curl_cffi with WBI signing and circuit breaker."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Callable

from curl_cffi import requests as cffi_requests

from .wbi import extract_wbi_keys, sign_wbi_params

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_AUTH_ERROR_CODES = {-101, -111}
_RISK_CONTROL_CODES = {-352, -412, -509, -799}
_RISK_HTTP_STATUSES = {412, 429}


class BilibiliApiError(RuntimeError):
    """Generic Bilibili Web API error."""

    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class BilibiliAuthError(BilibiliApiError):
    """Raised when Bilibili Cookie/CSRF authentication is missing or expired."""


class BilibiliRiskControlError(BilibiliApiError):
    """Raised when Bilibili risk control (-352 / -412 / HTTP 412 / 429) trips circuit breaker."""


def load_netscape_cookies(cookie_path: Path) -> dict[str, str]:
    """Parse a Netscape HTTP Cookie File into a `{name: value}` mapping for Bilibili domains."""
    if not cookie_path.exists():
        raise FileNotFoundError(f"Cookie 文件不存在: {cookie_path}")

    cookies: dict[str, str] = {}
    for raw_line in cookie_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lower()
        name = parts[5].strip()
        value = parts[6].strip()
        if not name:
            continue
        if "bilibili.com" in domain or "biligame.com" in domain or "hdslb.com" in domain:
            cookies[name] = value
    return cookies


class BilibiliWebClient:
    """Bilibili Web API client with WBI signing, throttling, and risk-control circuit breaker."""

    def __init__(
        self,
        *,
        cookies_file: Path | str | None = None,
        cookies: dict[str, str] | None = None,
        proxy: str | None = None,
        sleep_min: float = 1.5,
        sleep_max: float = 3.5,
        risk_backoff_seconds: float = 30.0,
        timeout: float = 20.0,
        session: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep_min = max(0.0, sleep_min)
        self._sleep_max = max(self._sleep_min, sleep_max)
        self._risk_backoff_seconds = max(0.0, risk_backoff_seconds)
        self._timeout = timeout
        self._sleeper = sleeper
        self._request_count = 0
        self._circuit_open = False
        self._wbi_keys: tuple[str, str] | None = None
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

        loaded_cookies: dict[str, str] = {}
        if cookies_file:
            loaded_cookies.update(load_netscape_cookies(Path(cookies_file)))
        if cookies:
            loaded_cookies.update(cookies)
        self.cookies = loaded_cookies

        if session is not None:
            self._session = session
            self._owns_session = False
        else:
            self._session = cffi_requests.Session(impersonate="chrome")
            self._owns_session = True

    def close(self) -> None:
        if self._owns_session and hasattr(self._session, "close"):
            try:
                self._session.close()
            except Exception:
                pass

    def __enter__(self) -> BilibiliWebClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def _throttle(self, *, light: bool = False) -> None:
        if self._request_count == 0:
            self._request_count += 1
            return
        self._request_count += 1
        if light:
            delay = random.uniform(min(0.2, self._sleep_min), min(0.6, self._sleep_max))
        else:
            delay = random.uniform(self._sleep_min, self._sleep_max)
        if delay > 0:
            self._sleeper(delay)

    def _build_headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": referer or "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _ensure_wbi_keys(self, *, force_refresh: bool = False) -> tuple[str, str]:
        if self._wbi_keys is not None and not force_refresh:
            return self._wbi_keys
        data = self.get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            signed=False,
            referer="https://www.bilibili.com/",
            allow_unauth_data=True,
        )
        self._wbi_keys = extract_wbi_keys(data)
        return self._wbi_keys

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        referer: str | None = None,
        allow_unauth_data: bool = False,
    ) -> dict[str, Any]:
        """Execute a GET request to Bilibili JSON API with optional WBI signing and circuit breaker."""
        if self._circuit_open:
            raise BilibiliRiskControlError("B 站风控熔断器已开启，跳过后续 API 请求")

        raw_params = dict(params or {})
        attempt = 0
        while True:
            attempt += 1
            query = dict(raw_params)
            if signed:
                img_key, sub_key = self._ensure_wbi_keys(force_refresh=(attempt > 1))
                query = sign_wbi_params(query, img_key, sub_key)

            self._throttle()
            resp = self._session.get(
                url,
                params=query,
                headers=self._build_headers(referer),
                cookies=self.cookies,
                proxies=self._proxies,
                timeout=self._timeout,
            )

            status = int(getattr(resp, "status_code", 200))
            if status in _RISK_HTTP_STATUSES:
                if attempt == 1:
                    if self._risk_backoff_seconds > 0:
                        self._sleeper(self._risk_backoff_seconds)
                    continue
                self._circuit_open = True
                raise BilibiliRiskControlError(
                    f"触发 B 站风控限流 (HTTP {status})，已熔断中止",
                    http_status=status,
                )

            if status >= 400:
                raise BilibiliApiError(f"B 站接口 HTTP {status}: {url}", http_status=status)

            try:
                payload = resp.json()
            except Exception as exc:
                raise BilibiliApiError(f"B 站接口返回非 JSON 内容: {exc}") from exc

            if not isinstance(payload, dict):
                raise BilibiliApiError("B 站接口返回结构异常（非 JSON 对象）")

            code = payload.get("code")
            message = str(payload.get("message") or payload.get("msg") or "")
            data = payload.get("data")

            if code == 0:
                return data if isinstance(data, dict) else {"value": data}

            # /x/web-interface/nav returns code=-101 when logged out, yet still includes wbi_img in data
            if allow_unauth_data and code == -101 and isinstance(data, dict) and data.get("wbi_img"):
                return data

            if code in _AUTH_ERROR_CODES:
                raise BilibiliAuthError(
                    f"B 站鉴权失败 (code={code}: {message})，请更新 cookies/bilibili.txt",
                    code=code,
                )

            if code in _RISK_CONTROL_CODES:
                if attempt == 1:
                    if self._risk_backoff_seconds > 0:
                        self._sleeper(self._risk_backoff_seconds)
                    continue
                self._circuit_open = True
                raise BilibiliRiskControlError(
                    f"触发 B 站风控拦截 (code={code}: {message})，已开启熔断保护",
                    code=code,
                )

            raise BilibiliApiError(
                f"B 站接口错误 (code={code}: {message})",
                code=code if isinstance(code, int) else None,
            )

    def get_text(self, url: str, *, referer: str | None = None) -> str:
        """Fetch an HTML/text page (e.g. legacy cv article page)."""
        if self._circuit_open:
            raise BilibiliRiskControlError("B 站风控熔断器已开启，跳过页面抓取")

        self._throttle()
        headers = self._build_headers(referer)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        resp = self._session.get(
            url,
            headers=headers,
            cookies=self.cookies,
            proxies=self._proxies,
            timeout=self._timeout,
        )
        status = int(getattr(resp, "status_code", 200))
        if status in _RISK_HTTP_STATUSES:
            self._circuit_open = True
            raise BilibiliRiskControlError(f"页面抓取触发风控 (HTTP {status})", http_status=status)
        if status >= 400:
            raise BilibiliApiError(f"页面抓取失败 (HTTP {status}): {url}", http_status=status)
        return str(resp.text)

    def download_binary(self, url: str, *, referer: str | None = None) -> tuple[bytes, str]:
        """Download binary asset (avatar/banner/image). Returns `(bytes, content_type)`."""
        if url.startswith("//"):
            url = f"https:{url}"
        self._throttle(light=True)
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": referer or "https://www.bilibili.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        resp = self._session.get(
            url,
            headers=headers,
            proxies=self._proxies,
            timeout=self._timeout,
        )
        status = int(getattr(resp, "status_code", 200))
        if status >= 400:
            raise BilibiliApiError(f"资源下载失败 (HTTP {status}): {url}", http_status=status)
        ctype = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        return bytes(resp.content), ctype

    # ---- High-level Bilibili Web API endpoints ----

    def get_user_info(self, uid: int) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params={"mid": uid},
            signed=True,
            referer=f"https://space.bilibili.com/{uid}/",
        )

    def get_relation_stat(self, uid: int) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/relation/stat",
            params={"vmid": uid},
            signed=False,
            referer=f"https://space.bilibili.com/{uid}/",
        )

    def get_upstat(self, uid: int) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/space/upstat",
            params={"mid": uid},
            signed=False,
            referer=f"https://space.bilibili.com/{uid}/",
        )

    def get_collections(self, uid: int, *, page_num: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/polymer/web-space/seasons_collections_list",
            params={
                "mid": uid,
                "page_num": page_num,
                "page_size": page_size,
            },
            signed=False,
            referer=f"https://space.bilibili.com/{uid}/channel/collectiondetail",
        )

    def get_dynamics_page(self, uid: int, *, offset: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {
            "host_mid": uid,
            "timezone_offset": -480,
            "platform": "web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
        }
        if offset:
            params["offset"] = offset
        return self.get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params=params,
            signed=True,
            referer=f"https://space.bilibili.com/{uid}/dynamic",
        )

    def get_articles_page(self, uid: int, *, pn: int = 1, ps: int = 30) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/space/wbi/article",
            params={
                "mid": uid,
                "pn": pn,
                "ps": ps,
                "sort": "publish_time",
            },
            signed=True,
            referer=f"https://space.bilibili.com/{uid}/article",
        )

    def get_opus_detail(self, opus_id: str | int) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail",
            params={
                "id": str(opus_id),
                "features": "onlyfansVote,onlyfansAssetsV2",
            },
            signed=True,
            referer=f"https://www.bilibili.com/opus/{opus_id}",
        )

    def get_article_view(self, cv_id: int | str) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/article/view",
            params={"id": str(cv_id)},
            signed=True,
            referer=f"https://www.bilibili.com/read/cv{cv_id}",
        )

    def get_article_html(self, cv_id: int | str) -> str:
        return self.get_text(
            f"https://www.bilibili.com/read/cv{cv_id}",
            referer="https://www.bilibili.com/",
        )

    def get_created_favorites(self, uid: int) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
            params={"up_mid": uid},
            signed=False,
            referer=f"https://space.bilibili.com/{uid}/favlist",
        )

    def get_favorite_resources_page(
        self,
        media_id: int,
        *,
        pn: int = 1,
        ps: int = 20,
    ) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/v3/fav/resource/list",
            params={
                "media_id": media_id,
                "pn": pn,
                "ps": ps,
                "platform": "web",
            },
            signed=False,
            referer=f"https://space.bilibili.com/",
        )

    def get_replies_main_page(
        self,
        *,
        oid: str | int,
        rtype: int,
        next_offset: str = "",
        mode: int = 3,
    ) -> dict[str, Any]:
        pagination_obj = {"offset": next_offset} if next_offset else {"offset": ""}
        return self.get_json(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            params={
                "oid": str(oid),
                "type": rtype,
                "mode": mode,
                "pagination_str": json.dumps(pagination_obj, separators=(",", ":")),
                "plat": 1,
                "web_location": 1315875,
            },
            signed=True,
            referer="https://www.bilibili.com/",
        )

    def get_sub_replies_page(
        self,
        *,
        oid: str | int,
        rtype: int,
        root_rpid: int | str,
        pn: int = 1,
        ps: int = 20,
    ) -> dict[str, Any]:
        return self.get_json(
            "https://api.bilibili.com/x/v2/reply/reply",
            params={
                "oid": str(oid),
                "type": rtype,
                "root": str(root_rpid),
                "pn": pn,
                "ps": ps,
            },
            signed=False,
            referer="https://www.bilibili.com/",
        )
