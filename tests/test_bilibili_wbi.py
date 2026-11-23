from __future__ import annotations

import pytest

from biliyt.bilibili.archiver import bv_to_av
from biliyt.bilibili.wbi import extract_wbi_keys, get_mixin_key, sign_wbi_params
from biliyt.platform import extract_bilibili_uid


def test_get_mixin_key_deterministic() -> None:
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    mixin = get_mixin_key(img_key + sub_key)
    assert len(mixin) == 32
    assert mixin == "ea1db124af3c7062474693fa704f4ff8"


def test_extract_wbi_keys() -> None:
    nav = {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }
    }
    assert extract_wbi_keys(nav) == (
        "7cd084941338484aae1ad9425b84077c",
        "4932caff0ff746eab6f01bf08b70ac45",
    )


def test_sign_wbi_params_filters_special_chars() -> None:
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    signed = sign_wbi_params({"mid": 23191782, "note": "a!b'c(d)e*f"}, img_key, sub_key, wts=1700000000)
    assert signed["mid"] == "23191782"
    assert signed["note"] == "abcdef"
    assert signed["wts"] == "1700000000"
    assert len(signed["w_rid"]) == 32


def test_bv_to_av_known_vectors() -> None:
    # Classic Bilibili test vector: BV17x411w7KC -> av170001
    assert bv_to_av("BV17x411w7KC") == 170001
    assert bv_to_av("invalid") is None
    aid = bv_to_av("BV1fD3o69EiP")
    assert isinstance(aid, int) and aid > 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("23191782", 23191782),
        ("uid:23191782", 23191782),
        ("https://space.bilibili.com/23191782", 23191782),
        ("https://space.bilibili.com/23191782/upload/video", 23191782),
        ("https://www.bilibili.com/video/BV1fD3o69EiP", None),
        ("@mkbhd", None),
    ],
)
def test_extract_bilibili_uid(raw: str, expected: int | None) -> None:
    assert extract_bilibili_uid(raw) == expected
