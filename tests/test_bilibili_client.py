from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from biliyt.bilibili.client import (
    BilibiliAuthError,
    BilibiliRiskControlError,
    BilibiliWebClient,
    load_netscape_cookies,
)


@dataclass
class _FakeResponse:
    status_code: int = 200
    payload: dict[str, Any] | None = None
    text: str = ""
    content: bytes = b""
    headers: dict[str, str] | None = None

    def json(self) -> dict[str, Any]:
        assert self.payload is not None
        return self.payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


def test_load_netscape_cookies_parses_httponly_and_filters_domain(tmp_path: Path) -> None:
    p = tmp_path / "cookies.txt"
    p.write_text(
        "# Netscape HTTP Cookie File\n"
        "\n"
        ".bilibili.com\tTRUE\t/\tFALSE\t1800000000\tSESSDATA\tabc123\n"
        "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1800000000\tbili_jct\tcsrf456\n"
        ".youtube.com\tTRUE\t/\tFALSE\t1800000000\tSID\tignoreme\n",
        encoding="utf-8",
    )
    cookies = load_netscape_cookies(p)
    assert cookies == {"SESSDATA": "abc123", "bili_jct": "csrf456"}


def test_client_fetches_wbi_keys_and_signs_request() -> None:
    nav_resp = _FakeResponse(
        payload={
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                }
            },
        }
    )
    info_resp = _FakeResponse(payload={"code": 0, "data": {"mid": 23191782, "name": "TestUP"}})
    session = _FakeSession([nav_resp, info_resp])

    sleeps: list[float] = []
    client = BilibiliWebClient(session=session, sleep_min=0.0, sleep_max=0.0, sleeper=sleeps.append)
    data = client.get_user_info(23191782)
    assert data["name"] == "TestUP"
    assert len(session.calls) == 2
    assert "w_rid" in session.calls[1]["params"]
    assert "wts" in session.calls[1]["params"]


def test_client_raises_auth_error_on_minus_101() -> None:
    session = _FakeSession([_FakeResponse(payload={"code": -101, "message": "账号未登录"})])
    client = BilibiliWebClient(session=session, sleep_min=0.0, sleep_max=0.0, sleeper=lambda _: None)
    with pytest.raises(BilibiliAuthError):
        client.get_relation_stat(23191782)


def test_client_circuit_breaker_trips_after_backoff_retry() -> None:
    session = _FakeSession(
        [
            _FakeResponse(payload={"code": -352, "message": "风控校验失败"}),
            _FakeResponse(payload={"code": -352, "message": "风控校验失败"}),
        ]
    )
    sleeps: list[float] = []
    client = BilibiliWebClient(
        session=session,
        sleep_min=0.0,
        sleep_max=0.0,
        risk_backoff_seconds=5.0,
        sleeper=sleeps.append,
    )
    with pytest.raises(BilibiliRiskControlError):
        client.get_relation_stat(23191782)

    assert client.circuit_open is True
    assert 5.0 in sleeps

    # Subsequent calls fail fast without hitting network
    with pytest.raises(BilibiliRiskControlError):
        client.get_relation_stat(23191782)
    assert len(session.calls) == 2
