"""Tests for YAML config loading, validation, and override layering."""

from pathlib import Path

import pytest

from biliyt.config import AppConfig, load_config, resolve_target
from biliyt.platform import Platform


def test_load_and_resolve_partial_overrides(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
paths:
  download_dir: "./out"
  state_dir: "./st"
media:
  max_height: 1080
  write_subtitles: true
filters:
  recent_limit: 20
  date_after: "20260101"
  skip_live: true
platforms:
  youtube:
    proxy: "http://127.0.0.1:7890"
    auth:
      cookies_file: "./cookies/yt.txt"
    rate_limit:
      sleep_interval: 2.0
      max_sleep_interval: 5.0
targets:
  - id: "yt_creator"
    source: "@mkbhd"
    media:
      max_height: 2160
    filters:
      recent_limit: 5
  - id: "bili_up"
    source: "946974"
    proxy: null
""",
        encoding="utf-8",
    )

    cfg, base_dir = load_config(cfg_file)
    assert base_dir == tmp_path.resolve()

    yt_target = resolve_target(cfg, cfg.targets[0], base_dir)
    assert yt_target.platform == Platform.YOUTUBE
    assert yt_target.url == "https://www.youtube.com/@mkbhd/videos"
    assert yt_target.proxy == "http://127.0.0.1:7890"
    assert yt_target.auth.cookies_file == str((tmp_path / "cookies/yt.txt").resolve())
    # Overridden field
    assert yt_target.media.max_height == 2160
    assert yt_target.filters.recent_limit == 5
    # Inherited global fields preserved
    assert yt_target.media.write_subtitles is True
    assert yt_target.filters.date_after == "20260101"
    assert yt_target.filters.skip_live is True
    assert yt_target.rate_limit.sleep_interval == 2.0
    assert yt_target.download_dir == (tmp_path / "out/youtube/yt_creator").resolve()

    bili_target = resolve_target(cfg, cfg.targets[1], base_dir)
    assert bili_target.platform == Platform.BILIBILI
    assert bili_target.proxy is None
    assert bili_target.url == "https://space.bilibili.com/946974/upload/video"


def test_duplicate_target_ids_rejected() -> None:
    with pytest.raises(ValueError, match="重复"):
        AppConfig.model_validate(
            {
                "targets": [
                    {"id": "dup", "source": "946974"},
                    {"id": "dup", "source": "@mkbhd"},
                ]
            }
        )


def test_invalid_duration_bounds_rejected() -> None:
    with pytest.raises(ValueError, match="min_duration"):
        AppConfig.model_validate({"filters": {"min_duration": 300, "max_duration": 60}})
