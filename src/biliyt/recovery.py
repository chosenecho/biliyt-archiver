"""Read-only repair planning and explicit, resumable archive recovery.

Planning never creates directories, manifests, database connections, or clients.
Execution expects its caller to hold StateStore.acquire_sync_lock(), just like sync.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

from .artifacts import Artifact, ArtifactManifest, ArtifactStore, clean_error, public_url
from .atomic_io import atomic_write_bytes, atomic_write_json, atomic_write_text
from .bilibili.archiver import BilibiliArchiver
from .bilibili.client import BilibiliAuthError, BilibiliRiskControlError
from .bilibili.render import (
    _guess_image_ext,
    extract_dynamic_image_urls,
    extract_legacy_html_images,
    extract_opus_image_urls,
    normalize_url,
    render_article_markdown,
    render_comments_markdown,
    render_dynamic_markdown,
    render_favorite_folder_markdown,
    render_profile_markdown,
)
from .config import ResolvedTarget
from .db import ArchiveDB, extract_srt_plain_text
from .options import build_download_opts
from .platform import extract_bilibili_uid
from .state import FailedRecord, StateStore

_REPAIR_TYPES = frozenset({"index", "metadata", "subtitles", "images"})
_PENDING = {"failed", "pending"}


@dataclass
class RepairJob:
    action: str
    kind: str
    item_id: str
    requires_network: bool
    reason: str = ""
    manifest: ArtifactManifest | None = field(default=None, repr=False)
    record: FailedRecord | None = field(default=None, repr=False)


@dataclass
class RepairPlan:
    target_id: str
    jobs: list[RepairJob] = field(default_factory=list)
    manifests: list[ArtifactManifest] = field(default_factory=list, repr=False)
    errors: list[str] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return len({(j.kind, j.item_id) for j in self.jobs})


@dataclass
class RepairResult:
    target_id: str
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    circuit_open: bool = False


def _selection(only: str | set[str] | list[str] | None) -> set[str]:
    if only is None or only == "all":
        return set(_REPAIR_TYPES)
    values = {v.strip() for v in only.split(",")} if isinstance(only, str) else set(only)
    if not values or values - _REPAIR_TYPES:
        raise ValueError("only must contain index,metadata,subtitles,images")
    return values


def build_repair_plan(
    target: ResolvedTarget,
    state: StateStore,
    only: str | set[str] | list[str] | None = None,
    limit: int | None = None,
) -> RepairPlan:
    """Discover repairs without writes or network; limit counts distinct content items.

    Unknown, disabled and unavailable attachments do not trigger network jobs.
    Fetch failures are included for the default/all or metadata selection only.
    A saved manifest takes precedence over a stale fetch-failure record.
    """
    selected = _selection(only)
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    store = ArtifactStore(target.download_dir, target.id)
    plan = RepairPlan(target.id)
    # Load independently: one corrupt manifest must not hide other repairable items.
    for path in sorted(store.directory.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifest = store.load(str(data["kind"]), str(data["item_id"]))
            if manifest is not None:
                plan.manifests.append(manifest)
        except Exception as exc:
            plan.errors.append(clean_error(f"{path}: {exc}"))
    try:
        plan.manifests.extend(store.import_legacy_videos(persist=False, errors=plan.errors))
    except Exception as exc:
        plan.errors.append(clean_error(exc))

    plan.manifests = list({(m.kind, m.item_id): m for m in reversed(plan.manifests)}.values())
    plan.manifests.reverse()
    seen_items: set[tuple[str, str]] = set()
    known = {(m.kind, m.item_id) for m in plan.manifests}
    known_urls = {m.source_url for m in plan.manifests if m.source_url}

    def add_jobs(jobs: list[RepairJob]) -> None:
        if not jobs:
            return
        identity = (jobs[0].kind, jobs[0].item_id)
        if identity not in seen_items:
            if limit is not None and len(seen_items) >= limit:
                return
            seen_items.add(identity)
        plan.jobs.extend(jobs)

    for manifest in plan.manifests:
        jobs = []
        for action, kind in (("metadata", "metadata"), ("subtitles", "subtitle"), ("images", "image")):
            kinds = {"metadata", "markdown"} if action == "metadata" else {kind}
            missing = [a for a in manifest.artifacts if a.kind in kinds and a.status in _PENDING]
            if action in selected and missing:
                network = True
                if action == "metadata":
                    try:
                        primary = _metadata_path(store, manifest)
                        network = not primary.is_file()
                        if manifest.kind == "profile" and "snapshots" in primary.relative_to(store.root).parts:
                            network = False  # Current remote data cannot reconstruct an old snapshot.
                    except ValueError:
                        pass
                jobs.append(RepairJob(action, manifest.kind, manifest.item_id, network,
                                      f"{len(missing)} {kind} artifact(s) pending", manifest))
        if jobs or ("index" in selected and manifest.index_status in _PENDING):
            # Attachment repairs include their required offline index refresh, even with --only images.
            jobs.append(RepairJob("index", manifest.kind, manifest.item_id, False,
                                  manifest.index_error or "Index is pending", manifest))
        add_jobs(jobs)

    seen_failures: set[tuple[str, str]] = set()
    seen_urls: set[tuple[str, str]] = set()
    if "metadata" in selected:
        for record in state.load_failed(target.id):
            item_id = str(record.video_id or record.extra.get("dynamic_id")
                          or record.extra.get("cv_id") or public_url(record.url) or "unknown")
            identity = (record.item_type, item_id)
            url_key = (record.item_type, public_url(record.url))
            if (identity in known or public_url(record.url) in known_urls
                    or identity in seen_failures or url_key in seen_urls):
                continue
            seen_failures.add(identity)
            seen_urls.add(url_key)
            add_jobs([RepairJob("retry", record.item_type, item_id, True,
                                clean_error(record.error), record=record)])
    return plan


def _metadata_path(store: ArtifactStore, manifest: ArtifactManifest) -> Path:
    relative = manifest.context.get("metadata_path")
    if not relative:
        relative = next((a.path for a in manifest.artifacts if a.kind == "metadata" and a.path), None)
    if not relative:
        raise ValueError("Manifest has no metadata path")
    return store.resolve(str(relative))


def _payload(store: ArtifactStore, manifest: ArtifactManifest) -> dict[str, Any]:
    data = json.loads(_metadata_path(store, manifest).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Metadata must be a JSON object")
    return data


def _markdown_path(store: ArtifactStore, manifest: ArtifactManifest) -> Path:
    relative = manifest.context.get("markdown_path")
    if not relative:
        raise ValueError("Manifest has no Markdown path")
    return store.resolve(str(relative))


def _image_map(
    store: ArtifactStore, manifest: ArtifactManifest, md_path: Path, data: dict[str, Any],
) -> dict[str, str]:
    mapping = {
        normalize_url(a.source_url): Path(os.path.relpath(store.resolve(a.path), md_path.parent)).as_posix()
        for a in manifest.artifacts
        if a.kind == "image" and a.path and store.resolve(a.path).is_file() and a.source_url
    }
    # Manifests omit signed query parameters; renderers still see original raw payload URLs.
    # Add transient lookup aliases in memory without persisting those credentials again.
    urls = []
    if manifest.kind == "dynamic":
        urls = extract_dynamic_image_urls(data)
    elif manifest.kind == "article":
        urls = extract_opus_image_urls(data.get("opus_detail") or {})
        urls += extract_legacy_html_images(data.get("html_text") or (data.get("view") or {}).get("content") or "")
    elif manifest.kind == "profile":
        user = data.get("user_info") or {}
        urls = [str(user.get("face") or ""), str(user.get("top_photo") or "")]
    canonical = {public_url(normalize_url(url)): path for url, path in mapping.items()}
    for url in urls:
        key = public_url(normalize_url(url))
        if key in canonical:
            mapping[normalize_url(url)] = canonical[key]
    return mapping


def _profile_assets(data: dict[str, Any], mapping: dict[str, str]) -> tuple[str | None, str | None]:
    user = data.get("user_info") or {}
    canonical = {public_url(normalize_url(url)): path for url, path in mapping.items()}
    return (canonical.get(public_url(normalize_url(str(user.get("face") or "")))),
            canonical.get(public_url(normalize_url(str(user.get("top_photo") or "")))))


def _collections(store: ArtifactStore, data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("collections"), list):
        return data["collections"]
    path = store.root / "profile" / "collections.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def _render(store: ArtifactStore, manifest: ArtifactManifest, target: ResolvedTarget) -> None:
    data = _payload(store, manifest)
    path = _markdown_path(store, manifest)
    mapping = _image_map(store, manifest, path, data)
    if manifest.kind == "dynamic":
        text = render_dynamic_markdown(data, mapping)
    elif manifest.kind == "article":
        text = render_article_markdown(meta=data.get("meta") or data,
                                       opus_detail=data.get("opus_detail"),
                                       html_text=data.get("html_text") or (data.get("view") or {}).get("content"),
                                       image_map=mapping)
    elif manifest.kind == "profile":
        avatar, banner = _profile_assets(data, mapping)
        kwargs = dict(uid=int(data["uid"]), user_info=data.get("user_info") or {},
                      relation_stat=data.get("relation_stat"), upstat=data.get("upstat"),
                      collections=_collections(store, data), fetched_at=str(data.get("fetched_at") or ""))
        text = render_profile_markdown(**kwargs, avatar_rel=avatar, banner_rel=banner)
        root_mapping = {a.source_url: a.path for a in manifest.artifacts
                        if a.kind == "image" and a.path and store.resolve(a.path).is_file()}
        data["avatar_relpath"], data["banner_relpath"] = _profile_assets(data, root_mapping)
        atomic_write_json(_metadata_path(store, manifest), data)
        latest_path = store.root / "profile" / "profile.json"
        if latest_path.is_file() and data.get("snapshot_id"):
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            if latest.get("snapshot_id") == data["snapshot_id"]:
                atomic_write_json(latest_path, data)
                latest_md = latest_path.with_suffix(".md")
                latest_mapping = _image_map(store, manifest, latest_md, data)
                latest_avatar, latest_banner = _profile_assets(data, latest_mapping)
                atomic_write_text(latest_md, render_profile_markdown(
                    **kwargs, avatar_rel=latest_avatar, banner_rel=latest_banner))
    elif manifest.kind == "favorite":
        text = render_favorite_folder_markdown(data.get("folder") or {}, data.get("medias") or [])
    elif manifest.kind == "comment":
        text = render_comments_markdown(
            title=str(manifest.context.get("title") or manifest.item_id),
            oid=str(manifest.context.get("oid") or data.get("oid") or manifest.item_id),
            rtype=int(manifest.context.get("rtype") or data.get("type") or 1),
            bundle=data, up_uid=extract_bilibili_uid(target.source),
        )
    else:
        raise ValueError(f"Markdown rendering unsupported for {manifest.kind}")
    atomic_write_text(path, text)
    for artifact in manifest.artifacts:
        if artifact.kind == "markdown" and artifact.path == store.relative(path):
            artifact.status, artifact.error = "complete", ""


def _index(db: ArchiveDB, target: ResolvedTarget, store: ArtifactStore, manifest: ArtifactManifest) -> None:
    data = _payload(store, manifest)
    metadata = store.relative(_metadata_path(store, manifest))

    def artifact_path(kind: str) -> str | None:
        return next((a.path for a in manifest.artifacts
                     if a.kind == kind and a.path and store.resolve(a.path).is_file()), None)

    if manifest.kind == "video":
        subtitle = artifact_path("subtitle")
        db.upsert_video(target_id=target.id, platform=target.platform.value, info=data,
                        video_relpath=artifact_path("media"), info_json_relpath=metadata,
                        subtitle_relpath=subtitle, thumbnail_relpath=artifact_path("thumbnail"),
                        subtitle_text=extract_srt_plain_text(store.resolve(subtitle)) if subtitle else "")
        return
    md_relative = manifest.context.get("markdown_path")
    md_path = store.resolve(str(md_relative)) if md_relative else None
    markdown = md_path.read_text(encoding="utf-8") if md_path and md_path.is_file() else ""
    images = [a.path for a in manifest.artifacts
              if a.kind == "image" and a.path and store.resolve(a.path).is_file()]
    folder = Path(metadata).parent.as_posix()
    if manifest.kind == "dynamic":
        db.upsert_dynamic(target_id=target.id, item=data, markdown_content=markdown,
                          images_relpaths=images, folder_relpath=folder)
    elif manifest.kind == "article":
        db.upsert_article(target_id=target.id, payload=data, markdown_content=markdown,
                          images_relpaths=images, folder_relpath=folder)
    elif manifest.kind == "profile":
        mapping = {normalize_url(a.source_url): a.path for a in manifest.artifacts
                   if a.kind == "image" and a.path and store.resolve(a.path).is_file()}
        avatar, banner = _profile_assets(data, mapping)
        kwargs = {"snapshot_id": data["snapshot_id"]} if data.get("snapshot_id") else {}
        db.upsert_profile(target_id=target.id, platform=target.platform.value, uid=data.get("uid") or "",
                          user_info=data.get("user_info") or {}, relation_stat=data.get("relation_stat"),
                          upstat=data.get("upstat"), collections=_collections(store, data),
                          avatar_relpath=avatar, banner_relpath=banner,
                          fetched_at=data.get("fetched_at"), **kwargs)
    elif manifest.kind == "favorite":
        db.upsert_favorite_folder(target_id=target.id, folder=data.get("folder") or {},
                                  medias=data.get("medias") or [])
    elif manifest.kind == "comment":
        db.upsert_comments(target_id=target.id, carrier_kind=str(manifest.context.get("carrier_kind") or "video"),
                           oid=str(manifest.context.get("oid") or data.get("oid") or manifest.item_id),
                           rtype=int(manifest.context.get("rtype") or data.get("type") or 1),
                           carrier_title=str(manifest.context.get("title") or manifest.item_id),
                           source_url=manifest.source_url, bundle=data,
                           up_uid=extract_bilibili_uid(target.source))
    else:
        raise ValueError(f"Indexing unsupported for {manifest.kind}")


class _SubtitlePaths(PostProcessor):
    """Collect after-move paths, including subtitle conversion and temp-dir moves."""

    def __init__(self) -> None:
        super().__init__()
        self.paths: dict[str, Path] = {}

    def observe(self, info: dict[str, Any]) -> None:
        moves = info.get("__files_to_move") or {}
        final_media = Path(info["filepath"]) if info.get("filepath") else None
        for language, subtitle in (info.get("requested_subtitles") or {}).items():
            raw = subtitle.get("filepath")
            if raw:
                path = Path(str(moves.get(str(raw)) or raw))
                if not path.is_file() and final_media is not None:
                    path = final_media.parent / path.name
                if path.is_file():
                    self.paths[language] = path
        for entry in info.get("entries") or []:
            if isinstance(entry, dict):
                self.observe(entry)

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        self.observe(info)
        return [], info


def _video_attachment(
    target: ResolvedTarget, store: ArtifactStore, manifest: ArtifactManifest,
    action: str, factory: Callable[[dict[str, Any]], Any],
) -> None:
    if not manifest.source_url:
        raise ValueError("Video source URL is missing")
    opts = build_download_opts(target, archive_path=None)
    # Restrict this path to standalone attachments; never run media mutation postprocessors.
    opts.update(skip_download=True, writeinfojson=False, writethumbnail=False,
                writesubtitles=action == "subtitles", writeautomaticsub=action == "subtitles",
                noplaylist=True)
    for key in ("download_archive", "match_filter", "daterange"):
        opts.pop(key, None)
    # skip_download bypasses post_process; convert after subtitle writes and before MoveFiles.
    opts["postprocessors"] = [{**p, "when": "before_dl"} for p in opts.get("postprocessors", [])
                              if action == "subtitles" and p.get("key") == "FFmpegSubtitlesConvertor"]
    artifacts = [a for a in manifest.artifacts
                 if a.kind == ("subtitle" if action == "subtitles" else "metadata") and a.status in _PENDING]
    if action == "subtitles":
        opts["subtitleslangs"] = [a.language for a in artifacts if a.language] or list(target.media.subtitle_langs)
    media = next((store.resolve(a.path) for a in manifest.artifacts if a.kind == "media" and a.path), None)
    try:
        metadata = _metadata_path(store, manifest)
    except ValueError:
        metadata = (media.with_suffix(".info.json") if media else
                    store.root / ".biliyt" / "metadata" / f"{hashlib.sha256(manifest.item_id.encode()).hexdigest()}.info.json")
    base = media.with_suffix("") if media else metadata.with_name(metadata.name.removesuffix(".info.json"))
    opts["outtmpl"] = {"default": str(base) + ".%(ext)s"}
    opts["paths"] = {"home": str(base.parent)}
    collector = _SubtitlePaths()
    with factory(opts) as ydl:
        if hasattr(ydl, "add_post_processor"):
            ydl.add_post_processor(collector, when="after_move")
        info = ydl.extract_info(manifest.source_url, download=True)
        if not isinstance(info, dict):
            raise RuntimeError("No video metadata returned")
        collector.observe(info)
        if action == "metadata":
            data = ydl.sanitize_info(info) if hasattr(ydl, "sanitize_info") else info
            atomic_write_json(metadata, data)
            manifest.context["metadata_path"] = store.relative(metadata)
            for artifact in artifacts:
                artifact.path, artifact.status, artifact.error = store.relative(metadata), "complete", ""
            return
    paths = {lang: store.relative(path) for lang, path in collector.paths.items()}
    # A language selector can expand to several concrete language codes.
    for language, path in paths.items():
        existing = next((a for a in manifest.artifacts if a.kind == "subtitle" and a.language == language), None)
        if existing is None:
            manifest.artifacts.append(Artifact("subtitle", "complete", path, language=language))
        else:
            existing.path, existing.status, existing.error = path, "complete", ""
    missing = []
    for artifact in artifacts:
        matches = [path for lang, path in paths.items()
                   if artifact.language is None or lang == artifact.language or re.fullmatch(artifact.language, lang)]
        if matches:
            artifact.path, artifact.status, artifact.error = matches[0], "complete", ""
        elif not info.get("subtitles") and not info.get("automatic_captions") and not info.get("requested_subtitles"):
            artifact.status, artifact.error = "unavailable", "Source has no requested subtitles"
        else:
            missing.append(artifact.language or "requested subtitles")
    if missing:
        raise RuntimeError("Subtitle files missing after extraction: " + ", ".join(missing))


def _local_metadata(store: ArtifactStore, manifest: ArtifactManifest, target: ResolvedTarget) -> None:
    _payload(store, manifest)  # Validate the existing source before reconciling statuses.
    if manifest.context.get("markdown_path"):
        _render(store, manifest, target)
    for artifact in manifest.artifacts:
        if artifact.kind in {"metadata", "markdown"} and artifact.status in _PENDING:
            if not artifact.path or not store.resolve(artifact.path).is_file():
                raise ValueError("Local metadata artifact is missing")
            artifact.status, artifact.error = "complete", ""
    manifest.index_status = "pending"


def _extended_metadata(
    target: ResolvedTarget, store: ArtifactStore, manifest: ArtifactManifest, client: Any, state: StateStore,
) -> None:
    """Restore missing raw metadata without scheduling a media download."""
    path = _metadata_path(store, manifest)
    if manifest.kind == "dynamic":
        detail = client.get_json("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
                                 params={"id": manifest.item_id, "features": "itemOpusStyle"},
                                 signed=True, referer=manifest.source_url)
        data = detail.get("item")
        if not isinstance(data, dict) or not data.get("id_str"):
            raise ValueError("Dynamic source has no item")
    elif manifest.kind == "article":
        cv_id = manifest.item_id.removeprefix("cv")
        view = client.get_article_view(cv_id)
        if not isinstance(view, dict):
            raise ValueError("Article source has no metadata")
        opus_id = str(view.get("dyn_id_str") or view.get("opus_id") or "")
        opus = view.get("opus")
        if not opus and opus_id:
            opus = client.get_opus_detail(opus_id)
        html = view.get("content")
        if not html and not opus:
            html = client.get_article_html(cv_id)
        meta = dict(view)
        meta.setdefault("id", cv_id)
        meta.setdefault("title", manifest.context.get("title") or f"cv{cv_id}")
        data = {"cv_id": cv_id, "opus_id": opus_id or None, "meta": meta,
                "view": view, "opus_detail": opus, "html_text": html}
    elif manifest.kind == "profile":
        uid = extract_bilibili_uid(target.source)
        if uid is None:
            raise ValueError("Target has no Bilibili UID")
        data = {"uid": uid, "user_info": client.get_user_info(uid),
                "relation_stat": client.get_relation_stat(uid), "upstat": client.get_upstat(uid)}
    elif manifest.kind == "favorite":
        uid = extract_bilibili_uid(target.source)
        if uid is None:
            raise ValueError("Target has no Bilibili UID")
        folders = client.get_created_favorites(uid)
        folder = next((f for f in folders.get("list") or []
                       if isinstance(f, dict) and str(f.get("id")) == manifest.item_id), None)
        if folder is None:
            raise ValueError("Favorite folder is no longer available")
        medias = []
        page_number = 1
        while True:
            page = client.get_favorite_resources_page(int(manifest.item_id), pn=page_number, ps=20)
            batch = [item for item in page.get("medias") or [] if isinstance(item, dict)]
            medias.extend(batch)
            if not page.get("has_more") or not batch:
                break
            page_number += 1
        data = {"folder": folder, "medias": medias}
    elif manifest.kind == "comment":
        uid = extract_bilibili_uid(target.source)
        if uid is None:
            raise ValueError("Target has no Bilibili UID")
        config = target.archive.comments
        data = BilibiliArchiver(state).fetch_curated_comments(
            client, oid=str(manifest.context.get("oid") or manifest.item_id),
            rtype=int(manifest.context.get("rtype") or 1), up_uid=uid,
            top_n=config.top_n, include_pinned=config.include_pinned,
            include_up_replies=config.include_up_replies,
            max_sub_replies_per_root=config.max_sub_replies_per_root,
        )
    else:
        raise ValueError(f"Metadata recovery unsupported for {manifest.kind}")
    atomic_write_json(path, data)
    for artifact in manifest.artifacts:
        if artifact.kind == "metadata" and artifact.status in _PENDING:
            artifact.path, artifact.status, artifact.error = store.relative(path), "complete", ""
    manifest.index_status = "pending"
    _render(store, manifest, target)


def _is_circuit_error(error: object) -> bool:
    if isinstance(error, (BilibiliAuthError, BilibiliRiskControlError)):
        return True
    # Existing retry adapters return strings instead of propagating typed exceptions.
    return bool(re.search(r"(?i)(-352|-412|-509|-799|-101|-111|HTTP\s*(?:401|403|412|429)|"
                          r"risk.control|circuit|风控|熔断|鉴权|登录.*(?:失效|过期)|cookie.*(?:expired|失效|过期))", str(error)))


class _CircuitClient:
    """Preserve typed auth/risk failures even when existing retry code catches them."""

    def __init__(self, client: Any, result: RepairResult) -> None:
        self._client = client
        self._result = result

    def __enter__(self) -> _CircuitClient:
        self._client = self._client.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._client.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except (BilibiliAuthError, BilibiliRiskControlError):
                self._result.circuit_open = True
                raise

        return call


def execute_repair_plan(
    plan: RepairPlan,
    target: ResolvedTarget,
    state: StateStore,
    *,
    db: ArchiveDB | None = None,
    client_factory: Callable[[ResolvedTarget], Any] | None = None,
    ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
    engine: Any | None = None,
) -> RepairResult:
    """Execute one target plan. Callers hold the shared process lock.

    Supplied database/client/yt-dlp adapters keep the same interface as production.
    A target's auth/risk failure skips subsequent network jobs, but not offline jobs.
    Counts refer to jobs, not content items; errors are suitable for persisted reports.
    """
    if target.id != plan.target_id:
        raise ValueError("Repair plan belongs to a different target")
    store = ArtifactStore(target.download_dir, target.id)
    result = RepairResult(target.id, errors=list(plan.errors), failed=len(plan.errors))
    ydl_factory = ydl_factory or yt_dlp.YoutubeDL
    make_client = client_factory or BilibiliArchiver._default_client_factory

    def guarded_client(resolved: ResolvedTarget) -> _CircuitClient:
        return _CircuitClient(make_client(resolved), result)

    with ExitStack() as stack:
        client = None
        opened_db = db
        retry_engine = engine
        for job in plan.jobs:
            if job.requires_network and result.circuit_open:
                result.skipped += 1
                continue
            manifest = job.manifest
            try:
                if job.action == "index":
                    if manifest is None:
                        raise ValueError("Index job has no manifest")
                    if opened_db is None:
                        opened_db = stack.enter_context(ArchiveDB(state.state_dir / "biliyt.db"))
                    _index(opened_db, target, store, manifest)
                    manifest.index_status, manifest.index_error = "complete", ""
                elif job.action in {"metadata", "subtitles"}:
                    if manifest is None:
                        raise ValueError("Attachment job has no manifest")
                    if job.action == "metadata" and not job.requires_network:
                        _local_metadata(store, manifest, target)
                    elif manifest.kind == "video":
                        _video_attachment(target, store, manifest, job.action, ydl_factory)
                    elif job.action == "metadata":
                        if client is None:
                            client = stack.enter_context(guarded_client(target))
                        _extended_metadata(target, store, manifest, client, state)
                    else:
                        raise ValueError(f"Subtitle recovery unsupported for {manifest.kind}")
                    manifest.index_status = "pending"
                elif job.action == "images":
                    if manifest is None:
                        raise ValueError("Image job has no manifest")
                    if client is None:
                        client = stack.enter_context(guarded_client(target))
                    image_dir = manifest.context.get("image_dir")
                    if not image_dir:
                        raise ValueError("Manifest has no image directory")
                    # Validate rendering coordinates before making any network requests.
                    _payload(store, manifest)
                    _markdown_path(store, manifest)
                    image_errors = []
                    for artifact in manifest.artifacts:
                        if artifact.kind != "image" or artifact.status not in _PENDING:
                            continue
                        try:
                            if not artifact.source_url:
                                raise ValueError("Image source URL is missing")
                            data, content_type = client.download_binary(artifact.source_url, referer=manifest.source_url)
                            filename = hashlib.sha256(artifact.source_url.encode()).hexdigest()[:20]
                            destination = store.resolve(str(image_dir)) / (filename + _guess_image_ext(artifact.source_url, content_type))
                            atomic_write_bytes(destination, data)
                            artifact.path = store.relative(destination)
                            artifact.status, artifact.error = "complete", ""
                        except Exception as exc:
                            artifact.status, artifact.error = "failed", clean_error(exc)
                            image_errors.append(clean_error(exc))
                            if _is_circuit_error(exc) or getattr(client, "circuit_open", False):
                                result.circuit_open = True
                                break
                    _render(store, manifest, target)
                    manifest.index_status = "pending"
                    if image_errors:
                        raise RuntimeError("; ".join(image_errors))
                elif job.action == "retry":
                    if job.record is None:
                        raise ValueError("Retry job has no failure record")
                    if retry_engine is None:
                        from .downloader import SyncEngine

                        archiver = BilibiliArchiver(state, client_factory=guarded_client)
                        retry_engine = SyncEngine(state, ydl_factory=ydl_factory, bili_archiver=archiver)
                    record = job.record
                    if record.item_type == "video":
                        outcome, detail = retry_engine._download_single(target, record.url,
                                                                        video_id=record.video_id, title=record.title)
                    else:
                        outcome, detail = retry_engine.bili_archiver.retry_failed_item(target, record)
                    if outcome not in {"downloaded", "archived", "filtered"}:
                        raise RuntimeError(detail or "Fetch retry failed")
                    state.remove_failure(target.id, record.url, item_type=record.item_type)
                else:
                    raise ValueError(f"Unknown repair action: {job.action}")
                if manifest is not None:
                    store.save(manifest)
                result.completed += 1
            except Exception as exc:
                result.failed += 1
                error = clean_error(exc)
                result.errors.append(f"{job.kind}:{job.item_id} {job.action}: {error}")
                if _is_circuit_error(exc) or (client is not None and getattr(client, "circuit_open", False)):
                    result.circuit_open = True
                if manifest is not None:
                    if job.action == "index":
                        manifest.index_status, manifest.index_error = "failed", error
                    else:
                        artifact_kind = {"subtitles": "subtitle", "metadata": "metadata", "images": "image"}.get(job.action)
                        for artifact in manifest.artifacts:
                            if artifact.kind == artifact_kind and artifact.status in _PENDING:
                                artifact.status, artifact.error = "failed", error
                    try:
                        store.save(manifest)
                    except Exception as save_exc:
                        result.errors.append(f"Manifest save: {clean_error(save_exc)}")
    return result
