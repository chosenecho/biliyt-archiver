"""Tests for yt-dlp option building and filter evaluation."""

from pathlib import Path

from biliyt.config import AppConfig, FilterConfig, MediaConfig, resolve_target
from biliyt.options import (
    build_download_opts,
    build_format_options,
    detect_js_runtimes,
    evaluate_video_filters,
    parse_cookies_from_browser,
)
from biliyt.platform import Platform


def test_parse_cookies_from_browser() -> None:
    assert parse_cookies_from_browser("chrome") == ("chrome", None, None, None)
    assert parse_cookies_from_browser("edge:Default") == ("edge", "Default", None, None)
    assert parse_cookies_from_browser("firefox+gnomekeyring:work::personal") == (
        "firefox",
        "work",
        "GNOMEKEYRING",
        "personal",
    )


def test_build_format_options() -> None:
    fmt, sort_order = build_format_options(MediaConfig(max_height=1080, container="mp4", prefer_codec="avc"))
    assert "height<=1080" in fmt
    assert "vcodec^=avc" in fmt
    assert "res:1080" in sort_order
    assert "vcodec:h264" in sort_order
    assert "ext:mp4:m4a" in sort_order


def test_detect_js_runtimes_prefers_deno_then_node(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)
    assert detect_js_runtimes("auto") == {"node": {"path": "/usr/bin/node"}}
    assert detect_js_runtimes("none") == {}
    assert detect_js_runtimes("deno:/opt/deno") == {"deno": {"path": "/opt/deno"}}


def test_evaluate_video_filters() -> None:
    filters = FilterConfig(
        min_duration=60,
        max_duration=600,
        min_views=1000,
        include_keywords=["python", "ai"],
        exclude_keywords=["广告"],
        skip_live=True,
        skip_shorts=True,
    )

    assert evaluate_video_filters({"live_status": "is_live"}, filters, Platform.YOUTUBE) is not None
    assert (
        evaluate_video_filters(
            {"webpage_url": "https://www.youtube.com/shorts/abc"}, filters, Platform.YOUTUBE
        )
        is not None
    )
    assert evaluate_video_filters({"title": "Python 实战", "duration": 30}, filters, Platform.YOUTUBE) is not None
    assert evaluate_video_filters({"title": "Python 实战", "duration": 900}, filters, Platform.YOUTUBE) is not None
    assert (
        evaluate_video_filters(
            {"title": "Python 实战", "duration": 180, "view_count": 500}, filters, Platform.YOUTUBE
        )
        is not None
    )
    assert (
        evaluate_video_filters(
            {"title": "Python 广告推广", "duration": 180, "view_count": 5000}, filters, Platform.YOUTUBE
        )
        is not None
    )
    # Missing include keyword fails on full metadata, deferred on incomplete listing
    assert (
        evaluate_video_filters(
            {"title": "日常 Vlog", "duration": 180, "view_count": 5000},
            filters,
            Platform.YOUTUBE,
            incomplete=False,
        )
        is not None
    )
    assert (
        evaluate_video_filters(
            {"title": "日常 Vlog", "duration": 180, "view_count": 5000},
            filters,
            Platform.YOUTUBE,
            incomplete=True,
        )
        is None
    )
    # Valid match passes
    assert (
        evaluate_video_filters(
            {"title": "AI Agent 教程", "duration": 240, "view_count": 5000},
            filters,
            Platform.YOUTUBE,
            incomplete=False,
        )
        is None
    )


def test_build_download_opts_bilibili_and_youtube(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node" if name == "node" else None)

    cfg = AppConfig.model_validate(
        {
            "media": {
                "write_danmaku": True,
                "sponsorblock_remove": ["sponsor"],
                "subtitle_format": "srt",
                "convert_thumbnail": "jpg",
            },
            "platforms": {
                "youtube": {"proxy": "http://127.0.0.1:7890", "impersonate": "chrome"},
                "bilibili": {"auth": {"cookies_from_browser": "edge"}},
            },
            "targets": [
                {"id": "b1", "source": "946974"},
                {"id": "y1", "source": "@mkbhd"},
            ],
        }
    )

    b_target = resolve_target(cfg, cfg.targets[0], tmp_path)
    b_opts = build_download_opts(b_target, archive_path=tmp_path / "b1.txt")
    assert "danmaku" in b_opts["subtitleslangs"]
    assert b_opts["cookiesfrombrowser"] == ("edge", None, None, None)
    assert "js_runtimes" not in b_opts
    assert "proxy" not in b_opts
    pp_keys = [pp["key"] for pp in b_opts["postprocessors"]]
    assert "SponsorBlock" not in pp_keys
    assert "FFmpegSubtitlesConvertor" in pp_keys
    assert "FFmpegThumbnailsConvertor" in pp_keys
    assert "FFmpegMetadata" in pp_keys

    y_target = resolve_target(cfg, cfg.targets[1], tmp_path)
    y_opts = build_download_opts(y_target, archive_path=tmp_path / "y1.txt")
    assert "danmaku" not in y_opts["subtitleslangs"]
    assert y_opts["proxy"] == "http://127.0.0.1:7890"
    assert y_opts["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}
    assert str(y_opts["impersonate"]) == "chrome"
    y_pp_keys = [pp["key"] for pp in y_opts["postprocessors"]]
    assert "SponsorBlock" in y_pp_keys
    assert "ModifyChapters" in y_pp_keys


def test_build_download_opts_sets_temp_dir(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {
                "download_dir": str(tmp_path / "dl"),
                "state_dir": str(tmp_path / "state"),
                "temp_dir": str(tmp_path / "fast_tmp"),
            },
            "targets": [{"id": "b1", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    opts = build_download_opts(target, archive_path=tmp_path / "b1.txt")
    assert opts["paths"]["home"] == str((tmp_path / "dl" / "bilibili" / "b1").resolve())
    assert opts["paths"]["temp"] == str((tmp_path / "fast_tmp").resolve())
