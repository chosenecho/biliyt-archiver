"""Offline recovery through the public plan/execute interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegSubtitlesConvertorPP

from biliyt.artifacts import Artifact, ArtifactManifest, ArtifactStore
from biliyt.bilibili.client import BilibiliAuthError, BilibiliRiskControlError
from biliyt.config import AppConfig, resolve_target
from biliyt.db import ArchiveDB
from biliyt.recovery import build_repair_plan, execute_repair_plan
from biliyt.state import FailedRecord, StateStore


@pytest.fixture
def archive(tmp_path: Path) -> tuple[Any, StateStore, ArtifactStore]:
    cfg = AppConfig.model_validate({
        "paths": {"download_dir": str(tmp_path / "downloads"), "state_dir": str(tmp_path / "state")},
        "targets": [{"id": "creator", "source": "946974"}],
    })
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    return target, StateStore(target.state_dir), ArtifactStore(target.download_dir, target.id)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def video(store: ArtifactStore, item_id: str = "BV1sample", *, subtitle: str | None = None) -> ArtifactManifest:
    info = store.root / "arbitrary" / f"title-{item_id}.info.json"
    write_json(info, {"id": item_id, "title": "Searchable recovery sample", "description": "offline archive"})
    media = info.with_name(f"title-{item_id}.mp4")
    media.write_bytes(b"original-media")
    artifacts = [Artifact("metadata", "complete", store.relative(info)),
                 Artifact("media", "complete", store.relative(media))]
    if subtitle:
        artifacts.append(Artifact("subtitle", subtitle, language="zh-Hans"))
    manifest = ArtifactManifest(store.target_id, "video", item_id,
                                f"https://www.bilibili.com/video/{item_id}", artifacts,
                                context={"metadata_path": store.relative(info)})
    store.save(manifest)
    return manifest


def dynamic(store: ArtifactStore, item_id: str, *, index_status: str = "complete") -> ArtifactManifest:
    folder = store.root / "dynamics" / item_id
    write_json(folder / "dynamic.json", {
        "id_str": item_id,
        "modules": {"module_dynamic": {"desc": {"text": "Recovered dynamic text"},
                                          "major": {"type": "MAJOR_TYPE_DRAW", "draw": {"items": [
                                              {"src": "https://i.example/a.jpg"},
                                              {"src": "https://i.example/b.jpg"},
                                          ]}}}},
    })
    (folder / "dynamic.md").write_text("old markdown", encoding="utf-8")
    images = folder / "images"
    images.mkdir()
    (images / "old.jpg").write_bytes(b"existing")
    manifest = ArtifactManifest(store.target_id, "dynamic", item_id, f"https://t.bilibili.com/{item_id}", [
        Artifact("metadata", "complete", store.relative(folder / "dynamic.json")),
        Artifact("image", "complete", store.relative(images / "old.jpg"), "https://i.example/a.jpg"),
        Artifact("image", "failed", source_url="https://i.example/b.jpg"),
    ], index_status=index_status,
        context={"metadata_path": store.relative(folder / "dynamic.json"),
                 "markdown_path": store.relative(folder / "dynamic.md"), "image_dir": store.relative(images)})
    store.save(manifest)
    return manifest


def no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Offline repair created a network client")


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_preview_never_creates_state_manifests_or_clients(archive: tuple[Any, StateStore, ArtifactStore],
                                                         tmp_path: Path) -> None:
    target, state, store = archive
    info = store.root / "custom" / "name-without-id.info.json"
    write_json(info, {"id": "BVlegacy", "title": "legacy"})
    before = snapshot(tmp_path)
    plan = build_repair_plan(target, state)
    assert [(j.action, j.item_id, j.requires_network) for j in plan.jobs] == [("index", "BVlegacy", False)]
    assert plan.manifests[0].artifacts[1].status == "unknown"
    assert snapshot(tmp_path) == before
    assert not store.directory.exists()
    assert not state.state_dir.exists()


def test_index_legacy_is_offline_and_commits_manifest(archive: tuple[Any, StateStore, ArtifactStore]) -> None:
    target, state, store = archive
    write_json(store.root / "nested" / "custom-title.info.json", {"id": "BVlegacy", "title": "Legacy searchable"})
    # Unrelated broken content must not participate in the single-item index operation.
    broken = store.root / "articles" / "unrelated" / "article.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("broken", encoding="utf-8")
    plan = build_repair_plan(target, state, only="index")
    with ArchiveDB(state.state_dir / "biliyt.db") as db:
        result = execute_repair_plan(plan, target, state, db=db,
                                     client_factory=no_network, ydl_factory=no_network)
        assert result.failed == 0
        assert result.completed == 1
        assert db.search("Legacy")[0].item_id == "BVlegacy"
    manifest = store.load("video", "BVlegacy")
    assert manifest is not None and manifest.index_status == "complete"
    assert manifest.artifacts[1].status == "unknown"


@pytest.mark.parametrize("use_move_map", [True, False])
def test_subtitles_use_skip_download_and_final_converted_paths(
    archive: tuple[Any, StateStore, ArtifactStore], use_move_map: bool,
) -> None:
    target, state, store = archive
    original = video(store, subtitle="failed")
    media = store.resolve(original.artifacts[1].path)
    called = []

    class YDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            called.append(opts)
            assert opts["skip_download"] is True
            assert "download_archive" not in opts
            assert opts["writethumbnail"] is False
            assert opts["writeinfojson"] is False
            assert all(p["key"] == "FFmpegSubtitlesConvertor" for p in opts["postprocessors"])
            self.pp = None

        def __enter__(self) -> YDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def add_post_processor(self, pp: Any, when: str) -> None:
            assert when == "after_move"
            self.pp = pp

        def extract_info(self, url: str, download: bool) -> dict[str, Any]:
            assert download is True
            final = media.with_suffix(".zh-Hans.srt")
            final.write_text("1\n00:00:00,000 --> 00:00:01,000\nRecovered subtitle words\n", encoding="utf-8")
            temporary = str(store.root / "temporary" / ("subtitle.vtt" if use_move_map else final.name))
            info = {"id": original.item_id, "filepath": str(media),
                    "requested_subtitles": {"zh-Hans": {"filepath": temporary}},
                    "__files_to_move": {temporary: str(final)} if use_move_map else {}}
            self.pp.run(info)
            return info

    plan = build_repair_plan(target, state, only="subtitles")
    assert [j.action for j in plan.jobs] == ["subtitles", "index"]
    result = execute_repair_plan(plan, target, state, ydl_factory=YDL, client_factory=no_network)
    assert result.failed == 0 and result.completed == 2
    assert len(called) == 1
    assert media.read_bytes() == b"original-media"
    repaired = store.load("video", original.item_id)
    subtitle = next(a for a in repaired.artifacts if a.kind == "subtitle")
    assert subtitle.status == "complete"
    assert subtitle.path.endswith(".zh-Hans.srt")
    assert repaired.index_status == "complete"
    with ArchiveDB(state.state_dir / "biliyt.db") as db:
        assert db.search("Recovered subtitle")[0].kind == "subtitle"


def test_real_ytdlp_skip_download_lifecycle_converts_subtitles_before_move(
    archive: tuple[Any, StateStore, ArtifactStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, state, store = archive
    manifest = video(store, subtitle="failed")
    media = store.resolve(manifest.artifacts[1].path)
    temporary = store.root / "temporary-subtitles"
    temporary.mkdir()
    raw = temporary / "captions.zh-Hans.vtt"
    final = media.with_suffix(".zh-Hans.srt")
    converted = []

    def write_subtitles(self: Any, info: dict[str, Any], filename: str) -> list[tuple[str, str]]:
        raw.write_text("WEBVTT\n", encoding="utf-8")
        info["requested_subtitles"]["zh-Hans"]["filepath"] = str(raw)
        return [(str(raw), str(final.with_suffix(".vtt")))]

    def simulate_ffmpeg(self: Any, source: str, destination: str, options: Any, **kwargs: Any) -> None:
        assert Path(source) == raw and self.format == "srt"
        assert options == ["-f", "srt"]
        converted.append(source)
        Path(destination).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nLifecycle subtitle conversion\n", encoding="utf-8")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "_write_subtitles", write_subtitles)
    # Keep the real converter's run(), subtitle filepath updates and move-map logic.
    monkeypatch.setattr(FFmpegSubtitlesConvertorPP, "run_ffmpeg", simulate_ffmpeg)

    class OfflineYoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self, opts: dict[str, Any]) -> None:
            assert opts["skip_download"] is True and "download_archive" not in opts
            assert opts["postprocessors"]
            assert all(pp["when"] == "before_dl" for pp in opts["postprocessors"])
            super().__init__(opts)

        def extract_info(self, url: str, download: bool = True, **kwargs: Any) -> dict[str, Any]:
            assert download is True
            info = {"id": manifest.item_id, "title": "Lifecycle example", "ext": "mp4", "format": "mp4",
                    "url": "https://example.invalid/media", "webpage_url": manifest.source_url,
                    "extractor": "bilibili", "extractor_key": "BiliBili",
                    "requested_subtitles": {"zh-Hans": {"ext": "vtt", "url": "https://example.invalid/subtitle"}}}
            # Exercise yt-dlp's real before_dl -> skip_download -> MoveFiles path.
            self.process_info(info)
            return info

    result = execute_repair_plan(build_repair_plan(target, state, only="subtitles"), target, state,
                                 ydl_factory=OfflineYoutubeDL, client_factory=no_network)
    assert result.failed == 0 and result.completed == 2, result.errors
    assert converted == [str(raw)]
    assert final.is_file() and not raw.exists() and not raw.with_suffix(".srt").exists()
    assert media.read_bytes() == b"original-media"
    repaired = store.load("video", manifest.item_id)
    subtitle = next(a for a in repaired.artifacts if a.kind == "subtitle")
    assert subtitle.path == store.relative(final) and subtitle.status == "complete"
    with ArchiveDB(state.state_dir / "biliyt.db") as db:
        assert db.search("Lifecycle subtitle conversion")[0].kind == "subtitle"


def test_images_preserve_complete_mapping_and_rerender_markdown(archive: tuple[Any, StateStore, ArtifactStore]) -> None:
    target, state, store = archive
    manifest = dynamic(store, "123")
    # Raw payload keeps an expiring query, while persisted image sources intentionally omit it.
    metadata = store.resolve(manifest.context["metadata_path"])
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["modules"]["module_dynamic"]["major"]["draw"]["items"][1]["src"] += "?token=transient"
    write_json(metadata, data)
    requests = []

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def download_binary(self, url: str, *, referer: str) -> tuple[bytes, str]:
            requests.append(url)
            return b"new-image", "image/jpeg"

    plan = build_repair_plan(target, state, only="images")
    assert [j.action for j in plan.jobs] == ["images", "index"]
    result = execute_repair_plan(plan, target, state, client_factory=lambda _: Client(), ydl_factory=no_network)
    assert result.failed == 0 and result.completed == 2
    assert requests == ["https://i.example/b.jpg"]
    repaired = store.load("dynamic", manifest.item_id)
    md = store.resolve(repaired.context["markdown_path"]).read_text(encoding="utf-8")
    assert "images/old.jpg" in md
    assert "https://i.example/b.jpg" not in md
    assert "transient" not in md
    assert all(a.status == "complete" for a in repaired.artifacts)
    assert repaired.index_status == "complete"
    with ArchiveDB(state.state_dir / "biliyt.db") as db:
        assert db.search("Recovered dynamic")[0].kind == "dynamic"


@pytest.mark.parametrize("kind", ["article", "profile"])
def test_article_and_profile_rerender_offline_payloads(archive: tuple[Any, StateStore, ArtifactStore], kind: str) -> None:
    target, state, store = archive
    folder = store.root / kind
    url = "https://i.example/new.jpg"
    if kind == "article":
        data = {"cv_id": "7", "meta": {"id": "7", "title": "Local article"},
                "html_text": f'<article><p>Preserved text</p><img src="{url}"></article>'}
    else:
        data = {"uid": 946974, "snapshot_id": "profile-snapshot", "user_info": {"name": "Creator", "face": url}}
    write_json(folder / f"{kind}.json", data)
    manifest = ArtifactManifest(target.id, kind, "7" if kind == "article" else "profile-snapshot",
                                artifacts=[Artifact("image", "failed", source_url=url)],
                                context={"metadata_path": f"{kind}/{kind}.json",
                                         "markdown_path": f"{kind}/{kind}.md", "image_dir": f"{kind}/assets"})
    store.save(manifest)

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def download_binary(self, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            return b"image", "image/jpeg"

    result = execute_repair_plan(build_repair_plan(target, state, only="images"), target, state,
                                 client_factory=lambda _: Client())
    assert result.failed == 0, result.errors
    markdown = store.resolve(manifest.context["markdown_path"]).read_text(encoding="utf-8")
    assert "assets/" in markdown
    assert url not in markdown
    if kind == "article":
        assert "Preserved text" in markdown


def test_failure_queue_deduplicates_saved_items_and_retries_selected_records(
    archive: tuple[Any, StateStore, ArtifactStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, state, store = archive
    saved = video(store)
    records = [FailedRecord(target.id, "bilibili", saved.source_url, saved.item_id),
               FailedRecord(target.id, "bilibili", "https://example.com/missing", "BVmissing"),
               FailedRecord(target.id, "bilibili", "https://example.com/alternate", "BVmissing")]
    monkeypatch.setattr(state, "load_failed", lambda *args: records)
    plan = build_repair_plan(target, state)
    assert [(j.action, j.item_id) for j in plan.jobs] == [("index", saved.item_id), ("retry", "BVmissing")]
    assert not any(j.action == "retry" for j in build_repair_plan(target, state, only="index").jobs)
    calls = []

    class Engine:
        def _download_single(self, target: Any, url: str, **kwargs: Any) -> tuple[str, str]:
            calls.append(url)
            return "downloaded", "ok"

    monkeypatch.setattr(state, "remove_failure", lambda *args, **kwargs: None)
    result = execute_repair_plan(plan, target, state, engine=Engine(), client_factory=no_network)
    assert result.failed == 0 and calls == ["https://example.com/missing"]


@pytest.mark.parametrize("exception", [BilibiliRiskControlError("risk"), BilibiliAuthError("expired")])
def test_circuit_skips_remaining_network_but_continues_offline_index(
    archive: tuple[Any, StateStore, ArtifactStore], exception: Exception,
) -> None:
    target, state, store = archive
    dynamic(store, "first", index_status="pending")
    dynamic(store, "second", index_status="pending")
    video(store)
    calls = []

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def download_binary(self, url: str, **kwargs: Any) -> tuple[bytes, str]:
            calls.append(url)
            raise exception

    result = execute_repair_plan(build_repair_plan(target, state), target, state,
                                 client_factory=lambda _: Client())
    assert result.circuit_open is True
    assert result.failed == 1 and result.skipped == 1 and result.completed == 3
    assert len(calls) == 1
    assert store.load("video", "BV1sample").index_status == "complete"
    assert store.load("dynamic", "second").index_status == "complete"


def test_error_redaction_and_manifest_failure_saved(archive: tuple[Any, StateStore, ArtifactStore]) -> None:
    target, state, store = archive
    video(store, subtitle="failed")

    def fail(opts: Any) -> Any:
        raise RuntimeError("SESSDATA=top-secret https://user:password@example.com/path?token=secret")

    result = execute_repair_plan(build_repair_plan(target, state, only="subtitles"), target, state, ydl_factory=fail)
    assert result.failed == 1
    text = " ".join(result.errors) + " ".join(p.read_text(encoding="utf-8") for p in store.directory.rglob("*.json"))
    assert "top-secret" not in text and "password@" not in text and "token=secret" not in text
    assert store.load("video", "BV1sample").artifacts[-1].status == "failed"


def test_limit_counts_items_including_all_jobs_and_rejects_bad_options(
    archive: tuple[Any, StateStore, ArtifactStore],
) -> None:
    target, state, store = archive
    video(store, "first", subtitle="pending")
    video(store, "second", subtitle="failed")
    plan = build_repair_plan(target, state, limit=1)
    assert plan.item_count == 1 and len(plan.jobs) == 2
    with pytest.raises(ValueError):
        build_repair_plan(target, state, only="video")
    with pytest.raises(ValueError):
        build_repair_plan(target, state, limit=0)


def test_disabled_unavailable_unknown_attachments_do_not_schedule_network(
    archive: tuple[Any, StateStore, ArtifactStore],
) -> None:
    target, state, store = archive
    manifest = video(store)
    manifest.artifacts += [Artifact("subtitle", "disabled"), Artifact("subtitle", "unavailable"),
                           Artifact("image", "unknown")]
    store.save(manifest)
    assert all(not j.requires_network for j in build_repair_plan(target, state).jobs)


@pytest.mark.parametrize("latest_matches", [False, True])
def test_profile_snapshot_repair_updates_only_matching_latest(
    archive: tuple[Any, StateStore, ArtifactStore], latest_matches: bool,
) -> None:
    target, state, store = archive
    payload = {"uid": 946974, "snapshot_id": "old-snapshot", "fetched_at": "2026-01-01T00:00:00+00:00",
               "user_info": {"name": "Old name", "face": "https://i.example/avatar.jpg?token=ephemeral"},
               "collections": []}
    write_json(store.root / "profile/snapshots/old-snapshot.json", payload)
    latest = dict(payload) if latest_matches else {**payload, "snapshot_id": "new-snapshot",
                                                   "fetched_at": "2026-02-01T00:00:00+00:00",
                                                   "user_info": {"name": "New name"}}
    latest_path = store.root / "profile/profile.json"
    write_json(latest_path, latest)
    latest_markdown = latest_path.with_suffix(".md")
    latest_markdown.write_text("latest draft must survive", encoding="utf-8")
    before_json = latest_path.read_bytes()
    manifest = ArtifactManifest(target.id, "profile", "old-snapshot", artifacts=[
        Artifact("metadata", "complete", "profile/snapshots/old-snapshot.json"),
        Artifact("image", "failed", source_url="https://i.example/avatar.jpg?token=ephemeral"),
    ], context={"metadata_path": "profile/snapshots/old-snapshot.json",
                "markdown_path": "profile/snapshots/old-snapshot.md", "image_dir": "profile/assets"})
    store.save(manifest)

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def download_binary(self, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            return b"avatar", "image/jpeg"

    result = execute_repair_plan(build_repair_plan(target, state, only="images"), target, state,
                                 client_factory=lambda _: Client())
    assert result.failed == 0, result.errors
    repaired = json.loads(store.resolve(manifest.context["metadata_path"]).read_text(encoding="utf-8"))
    assert repaired["avatar_relpath"].startswith("profile/assets/")
    historical_md = store.resolve(manifest.context["markdown_path"]).read_text(encoding="utf-8")
    assert "../assets/" in historical_md and "ephemeral" not in historical_md
    if latest_matches:
        assert json.loads(latest_path.read_text(encoding="utf-8"))["avatar_relpath"] == repaired["avatar_relpath"]
        latest_text = latest_markdown.read_text(encoding="utf-8")
        assert "assets/" in latest_text and "../assets/" not in latest_text
    else:
        assert latest_path.read_bytes() == before_json
        assert latest_markdown.read_text(encoding="utf-8") == "latest draft must survive"


def test_missing_markdown_is_rebuilt_offline(archive: tuple[Any, StateStore, ArtifactStore]) -> None:
    target, state, store = archive
    manifest = dynamic(store, "local")
    for artifact in manifest.artifacts:
        if artifact.kind == "image" and artifact.status == "failed":
            artifact.status = "unknown"
    markdown = str(manifest.context["markdown_path"])
    manifest.artifacts.append(Artifact("markdown", "complete", markdown))
    store.save(manifest)
    store.resolve(markdown).unlink()
    plan = build_repair_plan(target, state, only="metadata")
    assert [j.action for j in plan.jobs] == ["metadata", "index"]
    assert not any(j.requires_network for j in plan.jobs)
    result = execute_repair_plan(plan, target, state, client_factory=no_network, ydl_factory=no_network)
    assert result.failed == 0 and result.completed == 2
    assert "Recovered dynamic text" in store.resolve(markdown).read_text(encoding="utf-8")
    assert store.load("dynamic", "local").artifacts[-1].status == "complete"


def test_metadata_repair_skips_media_and_atomically_saves_info(archive: tuple[Any, StateStore, ArtifactStore]) -> None:
    target, state, store = archive
    manifest = video(store)
    store.resolve(manifest.artifacts[0].path).unlink()

    class YDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            assert opts["skip_download"] is True and opts["writesubtitles"] is False
            assert not opts["postprocessors"]

        def __enter__(self) -> YDL:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def extract_info(self, url: str, download: bool) -> dict[str, Any]:
            return {"id": manifest.item_id, "title": "Recovered metadata"}

    result = execute_repair_plan(build_repair_plan(target, state, only="metadata"), target, state, ydl_factory=YDL)
    assert result.failed == 0 and result.completed == 2
    restored = store.load("video", manifest.item_id)
    assert restored.artifacts[0].status == "complete"
    assert store.resolve(restored.artifacts[1].path).read_bytes() == b"original-media"


def test_bad_legacy_json_is_reported_without_hiding_good_files(
    archive: tuple[Any, StateStore, ArtifactStore], tmp_path: Path,
) -> None:
    target, state, store = archive
    write_json(store.root / "z-good.info.json", {"id": "BVgood", "title": "Good legacy"})
    (store.root / "a-bad.info.json").write_text("invalid json", encoding="utf-8")
    before = snapshot(tmp_path)
    plan = build_repair_plan(target, state)
    assert [job.item_id for job in plan.jobs] == ["BVgood"]
    assert len(plan.errors) == 1 and "a-bad.info.json" in plan.errors[0]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["favorite", "comment"])
@pytest.mark.parametrize("missing_raw", [False, True])
def test_favorite_and_comment_restore_metadata_or_markdown(
    archive: tuple[Any, StateStore, ArtifactStore], kind: str, missing_raw: bool,
) -> None:
    target, state, store = archive
    reply = {"rpid_str": "7", "member": {"mid": "946974", "uname": "Creator"},
             "content": {"message": "Restored creator comment"}, "ctime": 1700000000}
    folder = {"id": 123, "title": "Restored favorites"}
    media = {"bvid": "BVfavorite", "title": "Favorite video"}
    payload = ({"folder": folder, "medias": [media]} if kind == "favorite" else
               {"oid": "987", "type": 17, "top": [reply]})
    metadata = store.root / kind / "item.json"
    markdown = metadata.with_suffix(".md")
    write_json(metadata, payload)
    relative = store.relative(metadata)
    manifest = ArtifactManifest(target.id, kind, "123" if kind == "favorite" else "dynamic:987",
                                artifacts=[Artifact("metadata", "complete", relative),
                                           Artifact("markdown", "pending", store.relative(markdown))],
                                context={"metadata_path": relative, "markdown_path": store.relative(markdown),
                                         **({"title": "Carrier title", "oid": "987", "rtype": 17,
                                             "carrier_kind": "dynamic"} if kind == "comment" else {})})
    store.save(manifest)
    if missing_raw:
        metadata.unlink()
    calls = []

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def get_created_favorites(self, uid: int) -> dict[str, Any]:
            assert uid == 946974
            calls.append("folders")
            return {"list": [folder]}

        def get_favorite_resources_page(self, media_id: int, *, pn: int, ps: int) -> dict[str, Any]:
            assert media_id == 123 and ps == 20
            calls.append(pn)
            return {"medias": [media] if pn == 1 else [{"bvid": "BVsecond", "title": "Second page"}],
                    "has_more": pn == 1}

        def get_replies_main_page(self, *, oid: str, rtype: int, next_offset: str, mode: int) -> dict[str, Any]:
            assert oid == "987" and rtype == 17 and mode == 3
            calls.append("comments")
            return {"replies": [reply], "cursor": {"is_end": True, "all_count": 1}}

    plan = build_repair_plan(target, state, only="metadata")
    assert plan.jobs[0].requires_network is missing_raw
    result = execute_repair_plan(plan, target, state,
                                 client_factory=(lambda _: Client()) if missing_raw else no_network,
                                 ydl_factory=no_network)
    assert result.failed == 0 and result.completed == 2, result.errors
    assert all(a.status == "complete" for a in store.load(kind, manifest.item_id).artifacts)
    text = markdown.read_text(encoding="utf-8")
    if kind == "favorite":
        assert "Restored favorites" in text and "Favorite video" in text
        if missing_raw:
            assert calls == ["folders", 1, 2]
            assert "Second page" in text
    else:
        assert "Carrier title" in text and "[UP主]" in text and "Restored creator comment" in text
        with ArchiveDB(state.state_dir / "biliyt.db") as db:
            assert "[UP主]" in db.search("Restored creator comment")[0].title
        if missing_raw:
            assert calls == ["comments"]
    if not missing_raw:
        assert calls == []
