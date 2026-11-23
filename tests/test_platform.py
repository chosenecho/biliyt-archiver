"""Tests for platform detection and URL normalization."""

import pytest

from biliyt.platform import Platform, detect_platform, normalize_target_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("946974", Platform.BILIBILI),
        ("uid:946974", Platform.BILIBILI),
        ("https://space.bilibili.com/946974", Platform.BILIBILI),
        ("https://www.bilibili.com/video/BV1xx411c7mD", Platform.BILIBILI),
        ("@mkbhd", Platform.YOUTUBE),
        ("UCBJycsmduvYEL83R_U4JriQ", Platform.YOUTUBE),
        ("https://www.youtube.com/@mkbhd", Platform.YOUTUBE),
        ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE),
    ],
)
def test_detect_platform(source: str, expected: Platform) -> None:
    assert detect_platform(source) == expected


def test_detect_platform_unknown_raises() -> None:
    with pytest.raises(ValueError):
        detect_platform("not-a-valid-source")


@pytest.mark.parametrize(
    ("source", "platform", "tab", "expected"),
    [
        ("946974", Platform.BILIBILI, "videos", "https://space.bilibili.com/946974/upload/video"),
        ("uid:946974", Platform.BILIBILI, "videos", "https://space.bilibili.com/946974/upload/video"),
        (
            "https://space.bilibili.com/946974/",
            Platform.BILIBILI,
            "videos",
            "https://space.bilibili.com/946974/upload/video",
        ),
        (
            "https://space.bilibili.com/946974/favlist?fid=12345",
            Platform.BILIBILI,
            "videos",
            "https://space.bilibili.com/946974/favlist?fid=12345",
        ),
        ("@mkbhd", Platform.YOUTUBE, "videos", "https://www.youtube.com/@mkbhd/videos"),
        ("@mkbhd", Platform.YOUTUBE, "streams", "https://www.youtube.com/@mkbhd/streams"),
        ("@mkbhd", Platform.YOUTUBE, "raw", "https://www.youtube.com/@mkbhd"),
        (
            "UCBJycsmduvYEL83R_U4JriQ",
            Platform.YOUTUBE,
            "videos",
            "https://www.youtube.com/channel/UCBJycsmduvYEL83R_U4JriQ/videos",
        ),
        (
            "https://www.youtube.com/@mkbhd",
            Platform.YOUTUBE,
            "videos",
            "https://www.youtube.com/@mkbhd/videos",
        ),
        (
            "https://www.youtube.com/@mkbhd/shorts",
            Platform.YOUTUBE,
            "videos",
            "https://www.youtube.com/@mkbhd/shorts",
        ),
        (
            "https://www.youtube.com/playlist?list=PL12345",
            Platform.YOUTUBE,
            "videos",
            "https://www.youtube.com/playlist?list=PL12345",
        ),
    ],
)
def test_normalize_target_url(source: str, platform: Platform, tab: str, expected: str) -> None:
    assert normalize_target_url(source, platform, youtube_tab=tab) == expected
