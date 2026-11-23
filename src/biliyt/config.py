"""Configuration schema, YAML loading, and target override resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .platform import Platform, detect_platform, normalize_target_url

TModel = TypeVar("TModel", bound=BaseModel)


class PathsConfig(BaseModel):
    download_dir: str = "./downloads"
    state_dir: str = "./state"
    filename_template: str = "%(upload_date>%Y-%m-%d|unknown)s - %(title).120B [%(id)s].%(ext)s"


class MediaConfig(BaseModel):
    max_height: int | None = Field(default=1080, ge=144)
    container: Literal["mp4", "mkv", "webm"] = "mp4"
    prefer_codec: Literal["auto", "avc", "hevc", "av1"] = "auto"
    write_info_json: bool = True
    write_thumbnail: bool = True
    convert_thumbnail: Literal["jpg", "png", "webp", "none"] = "jpg"
    embed_metadata: bool = True
    embed_chapters: bool = True
    embed_thumbnail: bool = False
    write_subtitles: bool = True
    write_auto_subtitles: bool = True
    subtitle_langs: list[str] = Field(
        default_factory=lambda: ["zh-Hans", "zh-CN", "zh-Hant", "zh", "en", "en-US", "ai-zh"]
    )
    subtitle_format: Literal["srt", "vtt", "ass", "original"] = "srt"
    embed_subtitles: bool = False
    write_danmaku: bool = False
    sponsorblock_remove: list[str] = Field(default_factory=list)


class FilterConfig(BaseModel):
    recent_limit: int | None = Field(default=None, ge=1)
    max_downloads: int | None = Field(default=None, ge=1)
    date_after: str | None = None
    date_before: str | None = None
    min_duration: int | None = Field(default=None, ge=0)
    max_duration: int | None = Field(default=None, ge=1)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    skip_live: bool = True
    skip_shorts: bool = False
    min_views: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_duration_bounds(self) -> FilterConfig:
        if (
            self.min_duration is not None
            and self.max_duration is not None
            and self.min_duration > self.max_duration
        ):
            raise ValueError("min_duration 不能大于 max_duration")
        return self


class AuthConfig(BaseModel):
    cookies_from_browser: str | None = None
    cookies_file: str | None = None

    @field_validator("cookies_from_browser", "cookies_file", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class RateLimitConfig(BaseModel):
    sleep_interval: float = Field(default=3.0, ge=0.0)
    max_sleep_interval: float = Field(default=8.0, ge=0.0)
    sleep_requests: float = Field(default=1.0, ge=0.0)
    concurrent_fragments: int = Field(default=4, ge=1, le=32)
    retries: int = Field(default=5, ge=0)
    rate_limit: str | None = None

    @model_validator(mode="after")
    def _validate_sleep_bounds(self) -> RateLimitConfig:
        if self.max_sleep_interval and self.max_sleep_interval < self.sleep_interval:
            self.max_sleep_interval = self.sleep_interval
        return self


class PlatformConfig(BaseModel):
    proxy: str | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig | None = None
    impersonate: str | None = None
    js_runtime: str | None = "auto"
    extractor_args: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    @field_validator("proxy", "impersonate", "js_runtime", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class TargetConfig(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    name: str | None = None
    platform: Platform | None = None
    source: str = Field(min_length=1)
    enabled: bool = True
    youtube_tab: Literal["videos", "shorts", "streams", "raw"] = "videos"
    output_subdir: str | None = None
    media: MediaConfig | None = None
    filters: FilterConfig | None = None
    proxy: str | None = None
    auth: AuthConfig | None = None
    rate_limit: RateLimitConfig | None = None
    impersonate: str | None = None
    js_runtime: str | None = None
    extractor_args: dict[str, dict[str, list[str]]] | None = None

    @field_validator("proxy", "impersonate", "js_runtime", "output_subdir", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _infer_platform(self) -> TargetConfig:
        if self.platform is None:
            self.platform = detect_platform(self.source)
        return self


class AppConfig(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    platforms: dict[Platform, PlatformConfig] = Field(
        default_factory=lambda: {
            Platform.BILIBILI: PlatformConfig(js_runtime=None),
            Platform.YOUTUBE: PlatformConfig(js_runtime="auto"),
        }
    )
    targets: list[TargetConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_target_ids(self) -> AppConfig:
        seen: set[str] = set()
        for t in self.targets:
            if t.id in seen:
                raise ValueError(f"存在重复的目标 id: {t.id}")
            seen.add(t.id)
        for p in (Platform.BILIBILI, Platform.YOUTUBE):
            self.platforms.setdefault(
                p,
                PlatformConfig(js_runtime="auto" if p == Platform.YOUTUBE else None),
            )
        return self


@dataclass(frozen=True)
class ResolvedTarget:
    id: str
    name: str
    platform: Platform
    source: str
    url: str
    enabled: bool
    download_dir: Path
    state_dir: Path
    filename_template: str
    media: MediaConfig
    filters: FilterConfig
    auth: AuthConfig
    rate_limit: RateLimitConfig
    proxy: str | None
    impersonate: str | None
    js_runtime: str | None
    extractor_args: dict[str, dict[str, list[str]]]


def _merge_model(base: TModel, override: TModel | None) -> TModel:
    """Merge only fields explicitly set on `override` into `base`."""
    if override is None:
        return base.model_copy(deep=True)
    patch = {field: getattr(override, field) for field in override.model_fields_set}
    return base.model_copy(update=patch, deep=True)


def _resolve_path(raw: str, base_dir: Path) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (base_dir / p).resolve()


def resolve_target(config: AppConfig, target: TargetConfig, base_dir: Path) -> ResolvedTarget:
    """Resolve effective settings for a target by layering global -> platform -> target."""
    assert target.platform is not None
    platform_cfg = config.platforms.get(target.platform, PlatformConfig())

    media = _merge_model(config.media, target.media)
    filters = _merge_model(config.filters, target.filters)
    rate_limit = _merge_model(config.rate_limit, platform_cfg.rate_limit)
    rate_limit = _merge_model(rate_limit, target.rate_limit)
    auth = _merge_model(platform_cfg.auth, target.auth)

    if auth.cookies_file:
        auth = auth.model_copy(update={"cookies_file": str(_resolve_path(auth.cookies_file, base_dir))})

    proxy = target.proxy if "proxy" in target.model_fields_set else platform_cfg.proxy
    impersonate = (
        target.impersonate if "impersonate" in target.model_fields_set else platform_cfg.impersonate
    )
    js_runtime = (
        target.js_runtime if "js_runtime" in target.model_fields_set else platform_cfg.js_runtime
    )

    merged_extractor_args: dict[str, dict[str, list[str]]] = {
        k: {ak: list(av) for ak, av in v.items()}
        for k, v in platform_cfg.extractor_args.items()
    }
    if target.extractor_args:
        for ie_key, ie_val in target.extractor_args.items():
            merged_extractor_args.setdefault(ie_key, {}).update(
                {ak: list(av) for ak, av in ie_val.items()}
            )

    download_root = _resolve_path(config.paths.download_dir, base_dir)
    subdir = target.output_subdir or f"{target.platform.value}/{target.id}"
    download_dir = (download_root / subdir).resolve()
    state_dir = _resolve_path(config.paths.state_dir, base_dir)

    url = normalize_target_url(target.source, target.platform, youtube_tab=target.youtube_tab)
    return ResolvedTarget(
        id=target.id,
        name=target.name or target.id,
        platform=target.platform,
        source=target.source,
        url=url,
        enabled=target.enabled,
        download_dir=download_dir,
        state_dir=state_dir,
        filename_template=config.paths.filename_template,
        media=media,
        filters=filters,
        auth=auth,
        rate_limit=rate_limit,
        proxy=proxy,
        impersonate=impersonate,
        js_runtime=js_runtime,
        extractor_args=merged_extractor_args,
    )


def load_config(config_path: Path) -> tuple[AppConfig, Path]:
    """Load and validate YAML config file, returning (config, config_dir)."""
    resolved = config_path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"配置文件不存在: {resolved}")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是 YAML 映射对象")
    return AppConfig.model_validate(raw), resolved.parent
