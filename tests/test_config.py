"""Tests for YAML config loading, validation, and override layering."""

from pathlib import Path

import pytest

from biliyt.config import (
    AppConfig,
    BilibiliArchiveConfig,
    FilterConfig,
    MediaConfig,
    PlatformConfig,
    RateLimitConfig,
    TargetConfig,
    load_config,
    resolve_target,
)
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


def test_config_example_yaml_loads_and_resolves() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg, base_dir = load_config(example)
    assert cfg.paths.temp_dir is None
    assert cfg.rate_limit.concurrency == 1
    assert cfg.filters.early_stop_after == 3
    assert cfg.platforms[Platform.BILIBILI].archive is not None
    assert cfg.platforms[Platform.BILIBILI].archive.comments.refresh_recent_days == 0
    ids = [t.id for t in cfg.targets]
    assert ids == ["bili_example", "yt_example"]
    for t in cfg.targets:
        resolved = resolve_target(cfg, t, base_dir)
        assert resolved.url.startswith("https://")
        assert resolved.enabled is False


def test_cli_init_copies_template_and_creates_cookies_dir(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from biliyt.cli import app

    out_cfg = tmp_path / "work" / "config.yaml"
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--config", str(out_cfg)])
    assert result.exit_code == 0, result.output
    assert out_cfg.is_file()
    assert (out_cfg.parent / "cookies").is_dir()
    cfg, _ = load_config(out_cfg)
    assert len(cfg.targets) == 2


def test_merged_duration_bounds_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "filters:\n  min_duration: 300\ntargets:\n"
        "  - id: short_target\n    source: '@mkbhd'\n"
        "    filters:\n      max_duration: 60\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="short_target.*filters") as exc:
        load_config(path)
    assert "min_duration" in str(exc.value)


def test_partial_bounds_are_validated_only_after_merge(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "filters": {"min_duration": 300, "max_duration": 600},
            "targets": [
                {
                    "id": "short",
                    "source": "946974",
                    "filters": {
                        "min_duration": None,
                        "max_duration": 60,
                    },
                }
            ],
        }
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.filters.min_duration is None
    assert resolved.filters.max_duration == 60
    assert cfg.targets[0].filters.model_fields_set == {"min_duration", "max_duration"}


@pytest.mark.parametrize("layer", ["platform", "target"])
@pytest.mark.parametrize("patch", [{"max_sleep_interval": 1.0}, {"sleep_interval": 12.0}])
def test_partial_sleep_does_not_normalize_against_defaults(
    tmp_path: Path,
    layer: str,
    patch: dict,
) -> None:
    raw = {
        "rate_limit": {"sleep_interval": 0.5, "max_sleep_interval": 20.0},
        "targets": [{"id": "yt", "source": "@mkbhd"}],
    }
    if layer == "platform":
        raw["platforms"] = {"youtube": {"rate_limit": patch}}
    else:
        raw["targets"][0]["rate_limit"] = patch
    cfg = AppConfig.model_validate(raw)
    override = cfg.platforms[Platform.YOUTUBE].rate_limit if layer == "platform" else cfg.targets[0].rate_limit
    assert override.model_fields_set == set(patch)
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.rate_limit.sleep_interval == patch.get("sleep_interval", 0.5)
    assert resolved.rate_limit.max_sleep_interval == patch.get("max_sleep_interval", 20.0)


def test_archive_sleep_partial_override_and_nested_inheritance(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "platforms": {
                "bilibili": {
                    "archive": {
                        "api_sleep_min": 0.5,
                        "api_sleep_max": 20,
                        "comments": {"enabled": True, "top_n": 25},
                    }
                }
            },
            "targets": [
                {
                    "id": "bili",
                    "source": "946974",
                    "archive": {
                        "api_sleep_max": 1,
                        "comments": {"top_n": 5},
                    },
                }
            ],
        }
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.archive.api_sleep_min == 0.5
    assert resolved.archive.api_sleep_max == 1
    assert resolved.archive.comments.enabled is True
    assert resolved.archive.comments.top_n == 5


@pytest.mark.parametrize(
    "raw",
    [
        {"medai": {}},
        {"paths": {"download_dr": "x"}},
        {"media": {"max_heigth": 720}},
        {"filters": {"max_duraton": 60}},
        {"rate_limit": {"sleep_intervl": 1}},
        {"platforms": {"youtube": {"proxxy": "x"}}},
        {"platforms": {"youtube": {"auth": {"cookie_file": "x"}}}},
        {"platforms": {"bilibili": {"archive": {"profiel": True}}}},
        {"platforms": {"bilibili": {"archive": {"comments": {"topn": 5}}}}},
        {"targets": [{"id": "a", "source": "946974", "enabld": True}]},
        {"targets": [{"id": "a", "source": "946974", "filters": {"max_duraton": 60}}]},
        {"targets": [{"id": "a", "source": "946974", "rate_limit": {"sleep_intervl": 1}}]},
    ],
)
def test_unknown_config_fields_rejected(raw: dict) -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        AppConfig.model_validate(raw)


@pytest.mark.parametrize("value", ["20260230", "20261301", "2026-01-01", "now+7days", "", "nope"])
def test_invalid_date_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="date_after"):
        AppConfig.model_validate({"filters": {"date_after": value}})


@pytest.mark.parametrize("value", ["20240229", "20260101", "now-7days", "today", "yesterday", "now-1month"])
def test_supported_dates(value: str) -> None:
    assert FilterConfig(date_after=value).date_after == value


@pytest.mark.parametrize(
    "after,before",
    [
        ("20260102", "20260101"),
        ("today", "now-7days"),
    ],
)
def test_date_range_order_rejected(after: str, before: str) -> None:
    with pytest.raises(ValueError, match="date_after.*date_before"):
        AppConfig.model_validate({"filters": {"date_after": after, "date_before": before}})


def test_inherited_date_range_rejected_on_load(tmp_path: Path) -> None:
    path = tmp_path / "date.yaml"
    path.write_text(
        "filters:\n  date_after: '20260102'\ntargets:\n"
        "  - id: date_target\n    source: '946974'\n"
        "    filters:\n      date_before: '20260101'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="date_target.*filters"):
        load_config(path)


def test_null_clears_nullable_fields_and_missing_fields_inherit(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "media": {"max_height": 720},
            "filters": {"recent_limit": 20, "date_after": "20260101", "early_stop_after": 3},
            "platforms": {
                "youtube": {
                    "proxy": "http://localhost:7890",
                    "js_runtime": "node",
                    "auth": {"cookies_file": "cookies/yt.txt", "cookies_from_browser": "firefox"},
                }
            },
            "targets": [
                {
                    "id": "yt",
                    "source": "@mkbhd",
                    "proxy": None,
                    "js_runtime": None,
                    "media": {"max_height": None},
                    "filters": {"date_after": None, "early_stop_after": None},
                    "auth": {"cookies_file": None},
                }
            ],
        }
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.media.max_height is None
    assert resolved.filters.date_after is None
    assert resolved.filters.early_stop_after is None
    assert resolved.filters.recent_limit == 20
    assert resolved.auth.cookies_file is None
    assert resolved.auth.cookies_from_browser == "firefox"
    assert resolved.proxy is None
    assert resolved.js_runtime is None


@pytest.mark.parametrize(
    "section,field",
    [
        ("media", "write_subtitles"),
        ("filters", "skip_live"),
        ("rate_limit", "sleep_interval"),
        ("archive", "api_sleep_min"),
    ],
)
def test_null_rejected_for_nonnullable_override_fields(section: str, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        AppConfig.model_validate(
            {
                "targets": [
                    {
                        "id": "a",
                        "source": "946974",
                        section: {field: None},
                    }
                ]
            }
        )


def test_public_models_remain_accepted_as_overrides(tmp_path: Path) -> None:
    target = TargetConfig(
        id="a",
        source="946974",
        media=MediaConfig(max_height=None),
        filters=FilterConfig(recent_limit=5),
        rate_limit=RateLimitConfig(retries=2),
        archive=BilibiliArchiveConfig(profile=True),
    )
    cfg = AppConfig(
        filters=FilterConfig(skip_live=False),
        targets=[target],
        platforms={
            Platform.BILIBILI: PlatformConfig(
                rate_limit=RateLimitConfig(sleep_requests=2),
                archive=BilibiliArchiveConfig(dynamics=True),
            )
        },
    )
    resolved = resolve_target(cfg, target, tmp_path)
    assert resolved.media.max_height is None
    assert resolved.filters.recent_limit == 5
    assert resolved.filters.skip_live is False
    assert resolved.rate_limit.retries == 2
    assert resolved.rate_limit.sleep_requests == 2
    assert resolved.archive.profile is True
    assert resolved.archive.dynamics is True


def test_final_normalization_uses_all_three_layers(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "rate_limit": {"sleep_interval": 10, "max_sleep_interval": 20},
            "platforms": {
                "bilibili": {
                    "rate_limit": {"max_sleep_interval": 1},
                    "archive": {"api_sleep_min": 10, "api_sleep_max": 1},
                }
            },
            "targets": [
                {
                    "id": "a",
                    "source": "946974",
                    "rate_limit": {"sleep_interval": 0.5},
                    "archive": {"api_sleep_min": 0.5},
                }
            ],
        }
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.rate_limit.max_sleep_interval == 1
    assert resolved.archive.api_sleep_max == 1
    # Resolving must not modify the partial inputs or their explicit field sets.
    assert cfg.platforms[Platform.BILIBILI].rate_limit.model_fields_set == {"max_sleep_interval"}
    assert cfg.platforms[Platform.BILIBILI].archive.api_sleep_max == 1


def test_final_normalization_retains_existing_sleep_semantics(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "targets": [
                {"id": "a", "source": "946974", "rate_limit": {"sleep_interval": 12}, "archive": {"api_sleep_min": 12}}
            ],
        }
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.rate_limit.max_sleep_interval == 12
    assert resolved.archive.api_sleep_max == 12
    assert cfg.targets[0].rate_limit.model_fields_set == {"sleep_interval"}
    cfg.targets[0].rate_limit.max_sleep_interval = 0
    assert resolve_target(cfg, cfg.targets[0], tmp_path).rate_limit.max_sleep_interval == 0


def test_resolve_revalidates_models_after_programmatic_changes(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate({"targets": [{"id": "changed", "source": "946974"}]})
    cfg.targets[0].filters = FilterConfig.model_construct(min_duration=300, max_duration=60)
    with pytest.raises(ValueError, match="changed.*filters"):
        resolve_target(cfg, cfg.targets[0], tmp_path)
    cfg.targets[0].filters = FilterConfig.model_construct(max_duration=-1)
    with pytest.raises(ValueError, match="changed.*filters") as exc:
        resolve_target(cfg, cfg.targets[0], tmp_path)
    assert "max_duration" in str(exc.value)


def test_null_override_section_inherits(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "media": {"max_height": 720},
            "filters": {"recent_limit": 5},
            "targets": [{"id": "a", "source": "946974", "media": None, "filters": None}],
        }
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.media.max_height == 720
    assert resolved.filters.recent_limit == 5
