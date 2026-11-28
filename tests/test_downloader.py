"""Tests for SyncEngine two-stage pipeline, retry_failed, and CLI smoke tests."""

import io
from pathlib import Path
from typing import Any

import pytest
import yt_dlp
from typer.testing import CliRunner

from biliyt.cli import _configure_utf8_stdio, app
from biliyt.config import AppConfig, load_config, resolve_target
from biliyt.downloader import SyncEngine
from biliyt.state import StateStore, SyncLockError


class _FakeYDL:
    """In-memory mock for yt_dlp.YoutubeDL."""

    def __init__(self, opts: dict[str, Any], feed: list[dict[str, Any]], fail_urls: set[str]) -> None:
        self.opts = opts
        self.feed = feed
        self.fail_urls = fail_urls

    def __enter__(self) -> "_FakeYDL":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        limit = self.opts.get("playlistend")
        entries = self.feed[:limit] if limit else self.feed
        return {"entries": entries}

    def download(self, urls: list[str]) -> int:
        url = urls[0]
        if url in self.fail_urls:
            raise yt_dlp.utils.DownloadError("simulated network error")

        archive = self.opts.get("download_archive")
        if archive:
            arch_path = Path(archive)
            arch_path.parent.mkdir(parents=True, exist_ok=True)
            vid = url.rsplit("/", 1)[-1].replace("watch?v=", "")
            with arch_path.open("a", encoding="utf-8") as fh:
                fh.write(f"mock {vid}\n")

        for hook in self.opts.get("progress_hooks", []):
            hook({"status": "finished", "filename": "demo.mp4"})
        return 0


def test_sync_and_retry_failed_flow(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
            "filters": {"min_duration": 60},
            "targets": [{"id": "bili_demo", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    # Pre-seed archive with BV1old
    state.archive_path(target.id).write_text("bilibili BV1old\n", encoding="utf-8")

    feed = [
        {"id": "BV1old", "title": "已下载视频", "duration": 200, "url": "https://www.bilibili.com/video/BV1old"},
        {"id": "BV1short", "title": "太短跳过", "duration": 20, "url": "https://www.bilibili.com/video/BV1short"},
        {"id": "BV1ok", "title": "正常下载", "duration": 300, "url": "https://www.bilibili.com/video/BV1ok"},
        {"id": "BV1bad", "title": "网络失败", "duration": 300, "url": "https://www.bilibili.com/video/BV1bad"},
    ]
    fail_urls = {"https://www.bilibili.com/video/BV1bad"}

    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _FakeYDL(opts, feed, fail_urls),
        sleeper=lambda _: None,
    )

    # Dry-run should not download or touch failed queue
    dry_summary = engine.sync_target(target, dry_run=True)
    assert dry_summary.discovered == 4
    assert dry_summary.already_archived == 1
    assert dry_summary.filtered_out == 1
    assert dry_summary.downloaded == 0
    assert state.load_failed() == []

    # Real sync: BV1ok succeeds, BV1bad recorded in failed.jsonl
    summary = engine.sync_target(target, dry_run=False)
    assert summary.discovered == 4
    assert summary.already_archived == 1
    assert summary.filtered_out == 1
    assert summary.downloaded == 1
    assert summary.failed == 1
    failed_records = state.load_failed("bili_demo")
    assert len(failed_records) == 1
    assert failed_records[0].video_id == "BV1bad"

    # Retry after transient issue resolves
    fail_urls.clear()
    retry_summaries = engine.retry_failed({target.id: target})
    assert len(retry_summaries) == 1
    assert retry_summaries[0].downloaded == 1
    assert retry_summaries[0].failed == 0
    assert state.load_failed("bili_demo") == []


def test_cli_init_check_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "biliyt.doctor._cmd_version",
        lambda exe, args=None: f"{exe} mock-1.0 (/usr/bin/{exe})",
    )
    runner = CliRunner()
    cfg_path = tmp_path / "config.yaml"

    init_res = runner.invoke(app, ["init", "--config", str(cfg_path)])
    assert init_res.exit_code == 0, init_res.output
    assert cfg_path.exists()

    check_res = runner.invoke(app, ["check", "--config", str(cfg_path)])
    assert check_res.exit_code == 0, check_res.output
    assert "yt-dlp" in check_res.output

    status_res = runner.invoke(app, ["status", "--config", str(cfg_path)])
    assert status_res.exit_code == 0, status_res.output
    assert "bili_example" in status_res.output


def test_cli_check_fails_when_ffmpeg_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "biliyt.doctor._cmd_version",
        lambda exe, args=None: None if exe in {"ffmpeg", "ffprobe"} else f"{exe} mock-1.0",
    )
    runner = CliRunner()
    cfg_path = tmp_path / "config.yaml"
    init_res = runner.invoke(app, ["init", "--config", str(cfg_path)])
    assert init_res.exit_code == 0, init_res.output

    check_res = runner.invoke(app, ["check", "--config", str(cfg_path)])
    assert check_res.exit_code == 1
    assert "FFmpeg" in check_res.output


def test_configure_utf8_stdio_reconfigures_legacy_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    fake_err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("biliyt.cli.sys.stdout", fake_out)
    monkeypatch.setattr("biliyt.cli.sys.stderr", fake_err)

    _configure_utf8_stdio()
    assert fake_out.encoding.lower() == "utf-8"
    assert fake_err.encoding.lower() == "utf-8"
    fake_out.write("中文环境测试")
    fake_out.flush()


def test_sync_concurrent_downloads(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {
                "download_dir": str(tmp_path / "dl"),
                "state_dir": str(tmp_path / "state"),
                "temp_dir": str(tmp_path / "tmp"),
            },
            "rate_limit": {"concurrency": 3, "sleep_interval": 0.0, "max_sleep_interval": 0.0},
            "targets": [{"id": "bili_conc", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    feed = [
        {"id": f"BV1conc{i}", "title": f"视频 {i}", "duration": 180, "url": f"https://www.bilibili.com/video/BV1conc{i}"}
        for i in range(6)
    ]
    fail_urls = {"https://www.bilibili.com/video/BV1conc2"}

    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _FakeYDL(opts, feed, fail_urls),
        sleeper=lambda _: None,
    )
    summary = engine.sync_target(target, dry_run=False)
    assert summary.discovered == 6
    assert summary.downloaded == 5
    assert summary.failed == 1
    assert len(state.load_failed("bili_conc")) == 1
    assert state.load_failed("bili_conc")[0].video_id == "BV1conc2"
    assert (tmp_path / "tmp").is_dir()


def test_sync_falls_back_without_subtitles_on_subtitle_429(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "media": {"embed_subtitles": True},
            "paths": {"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
            "rate_limit": {"sleep_interval": 0.0, "max_sleep_interval": 0.0},
            "targets": [{"id": "yt_sub429", "source": "@demo"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    feed = [{"id": "abc12345678", "title": "YouTube 视频", "duration": 180, "url": "https://www.youtube.com/watch?v=abc12345678"}]

    seen_opts: list[dict[str, Any]] = []
    warnings: list[str] = []

    class _Sub429YDL(_FakeYDL):
        def download(self, urls: list[str]) -> int:
            seen_opts.append(dict(self.opts))
            if self.opts.get("writesubtitles"):
                raise yt_dlp.utils.DownloadError(
                    "ERROR: Unable to download video subtitles for 'zh-Hans': HTTP Error 429: Too Many Requests"
                )
            return super().download(urls)

    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _Sub429YDL(opts, feed, set()),
        on_event=lambda ev, payload: warnings.append(payload["message"]) if ev == "item_warning" else None,
        sleeper=lambda _: None,
    )

    summary = engine.sync_target(target, dry_run=False)
    assert summary.downloaded == 1
    assert summary.failed == 0
    assert len(seen_opts) == 2
    assert seen_opts[0]["writesubtitles"] is True
    assert seen_opts[1]["writesubtitles"] is False
    assert seen_opts[1]["writeautomaticsub"] is False
    assert all(
        pp.get("key") not in {"FFmpegSubtitlesConvertor", "FFmpegEmbedSubtitle"}
        for pp in seen_opts[1].get("postprocessors", [])
    )
    assert any("429" in warning for warning in warnings)
    assert state.load_failed("yt_sub429") == []
    from biliyt.artifacts import ArtifactStore

    manifest = ArtifactStore(target.download_dir, target.id).load("video", "abc12345678")
    assert any(a.kind == "subtitle" and a.status == "failed" for a in manifest.artifacts)
    assert any(a.kind == "subtitle_embed" and a.status == "pending" for a in manifest.artifacts)
    assert summary.partial == 1
    assert summary.pending_repair == 1


def test_probe_lazy_early_stop_does_not_consume_tail(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
            "filters": {"early_stop_after": 3},
            "targets": [{"id": "bili_lazy", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    # Pre-seed items 1..5 as archived; item 0 is new
    state.append_archive_entries(target.id, [f"bilibili BV1item{i}" for i in range(1, 6)])

    yielded_ids: list[str] = []

    class _LazyFeedYDL(_FakeYDL):
        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            def _gen():
                for i in range(10):
                    vid = f"BV1item{i}"
                    yielded_ids.append(vid)
                    yield {"id": vid, "title": f"视频 {i}", "url": f"https://www.bilibili.com/video/{vid}"}

            return {"entries": _gen()}

    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _LazyFeedYDL(opts, [], set()),
        sleeper=lambda _: None,
    )

    # Incremental probe must stop after 1 pending + 3 consecutive archived -> only 4 items consumed
    res = engine.probe_target(target, full=False)
    assert res.early_stopped is True
    assert len(res.pending) == 1
    assert res.archived_count == 3
    assert yielded_ids == ["BV1item0", "BV1item1", "BV1item2", "BV1item3"]

    # Full probe ignores early_stop_after and scans the complete feed
    yielded_ids.clear()
    full_res = engine.probe_target(target, full=True)
    assert full_res.early_stopped is False
    assert len(yielded_ids) == 10
    assert len(full_res.pending) == 5


def test_sync_full_with_limit_backfills_in_batches(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
            "filters": {"recent_limit": 2, "early_stop_after": 2},
            "rate_limit": {"sleep_interval": 0.0, "max_sleep_interval": 0.0},
            "targets": [{"id": "bili_batch", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    feed = [
        {"id": f"BV1hist{i}", "title": f"历史视频 {i}", "duration": 180, "url": f"https://www.bilibili.com/video/BV1hist{i}"}
        for i in range(6)
    ]

    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _FakeYDL(opts, feed, set()),
        sleeper=lambda _: None,
    )

    # Batch 1: downloads hist0, hist1
    s1 = engine.sync_target(target, full=True, limit_override=2)
    assert s1.downloaded == 2
    assert state.read_archive_video_ids(target.id) == {"BV1hist0", "BV1hist1"}

    # Batch 2: must NOT get stuck on the first 2 items; downloads hist2, hist3
    s2 = engine.sync_target(target, full=True, limit_override=2)
    assert s2.already_archived == 2
    assert s2.downloaded == 2
    assert state.read_archive_video_ids(target.id) == {"BV1hist0", "BV1hist1", "BV1hist2", "BV1hist3"}

    # Batch 3: downloads remaining hist4, hist5
    s3 = engine.sync_target(target, full=True, limit_override=2)
    assert s3.already_archived == 4
    assert s3.downloaded == 2
    assert len(state.read_archive_video_ids(target.id)) == 6


def test_probe_detects_appended_multipart_video(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
            "targets": [{"id": "bili_mp", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    state.append_archive_entries(target.id, ["bilibili BV1mpA_p1", "bilibili BV1mpB_p1", "bilibili BV1mpB_p2"])

    feed = [
        # Only p1 archived, but feed now reports 2 parts -> must be pending to backfill p2
        {"id": "BV1mpA", "title": "追加分P视频", "playlist_count": 2, "url": "https://www.bilibili.com/video/BV1mpA"},
        # Both p1 & p2 archived and count is 2 -> archived
        {"id": "BV1mpB", "title": "完整多P视频", "playlist_count": 2, "url": "https://www.bilibili.com/video/BV1mpB"},
    ]
    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _FakeYDL(opts, feed, set()),
        sleeper=lambda _: None,
    )
    res = engine.probe_target(target, full=True)
    by_id = {e.video_id: e.status for e in res.entries}
    assert by_id == {"BV1mpA": "pending", "BV1mpB": "archived"}


def test_concurrent_download_merges_archive_cleanly_and_sync_lock(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate(
        {
            "paths": {"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
            "rate_limit": {"concurrency": 4, "sleep_interval": 0.0, "max_sleep_interval": 0.0},
            "targets": [{"id": "bili_lock", "source": "946974"}],
        }
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    feed = [
        {"id": f"BV1c{i}", "title": f"并发 {i}", "url": f"https://www.bilibili.com/video/BV1c{i}"}
        for i in range(8)
    ]

    engine = SyncEngine(
        state,
        ydl_factory=lambda opts: _FakeYDL(opts, feed, set()),
        sleeper=lambda _: None,
    )
    summary = engine.sync_target(target)
    assert summary.downloaded == 8
    assert state.read_archive_video_ids(target.id) == {f"BV1c{i}" for i in range(8)}
    # Ensure no per-download temp archive files leaked
    assert list(state.archives_dir.glob(".tmp_*.txt")) == []

    # Process sync lock must reject overlapping acquisition and recover after release
    with state.acquire_sync_lock():
        with pytest.raises(SyncLockError):
            with state.acquire_sync_lock():
                pass
    with state.acquire_sync_lock():
        assert state.lock_file.exists()


def test_final_paths_index_custom_template_and_multipart(tmp_path: Path) -> None:
    import json

    from biliyt.artifacts import ArtifactStore
    from biliyt.db import ArchiveDB

    cfg = AppConfig.model_validate({
        "paths": {"filename_template": "%(title)s.%(ext)s"},
        "targets": [{"id": "demo", "source": "946974", "archive": {"layout": "grouped"}}],
    })
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    feed = [{"id": "BVdemo", "url": "https://www.bilibili.com/video/BVdemo"}]

    class FinalYDL(_FakeYDL):
        def add_post_processor(self, pp, when):
            assert when == "after_move"
            self.collector = pp

        def download(self, urls):
            super().download(urls)
            final = target.download_dir / "videos"
            final.mkdir(parents=True, exist_ok=True)
            for part in (1, 2):
                item_id = f"BVdemo_p{part}"
                media = final / f"part {part}.mkv"
                metadata = final / f"part {part}.info.json"
                sub = final / f"part {part}.zh-Hans.srt"
                media.write_bytes(b"media")
                metadata.write_text(json.dumps({"id": item_id, "title": f"Part {part}"}), encoding="utf-8")
                sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8")
                self.collector.run({
                    "id": item_id, "filepath": str(media),
                    "infojson_filename": str(tmp_path / "old" / metadata.name),
                    "requested_subtitles": {"zh-Hans": {"filepath": str(tmp_path / "old" / sub.name)}},
                })
            return 0

    engine = SyncEngine(state, ydl_factory=lambda opts: FinalYDL(opts, feed, set()), sleeper=lambda _: None)
    summary = engine.sync_target(target)
    assert summary.downloaded == 1
    assert summary.index_failed == 0
    store = ArtifactStore(target.download_dir, target.id)
    assert {m.item_id for m in store.manifests()} == {"BVdemo_p1", "BVdemo_p2"}
    assert all(m.index_status == "complete" for m in store.manifests())
    with ArchiveDB(target.state_dir / "biliyt.db") as db:
        assert db.stats()["counts"]["videos"] == 2
        assert db.search("测试字幕")


def test_index_failure_is_persisted_without_losing_download(tmp_path: Path, monkeypatch) -> None:
    import json

    from biliyt.artifacts import ArtifactStore

    cfg = AppConfig.model_validate({"targets": [{"id": "demo", "source": "@demo"}]})
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    target.download_dir.mkdir(parents=True)
    info = target.download_dir / "custom.info.json"
    info.write_text(json.dumps({"id": "abc123", "title": "custom"}), encoding="utf-8")
    messages = []
    engine = SyncEngine(StateStore(target.state_dir), on_event=lambda event, payload: messages.append(event))
    monkeypatch.setattr("biliyt.db.index_video_info_file", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk failure")))
    engine._index_downloaded_video(target, "abc123")
    manifest = ArtifactStore(target.download_dir, target.id).load("video", "abc123")
    assert manifest.index_status == "failed"
    assert "disk failure" in manifest.index_error
    assert "item_warning" in messages
    assert info.exists()


def test_repair_cli_preview_is_read_only_and_apply_offline(tmp_path: Path) -> None:
    import json

    from biliyt.db import ArchiveDB

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("targets:\n  - id: demo\n    source: '@demo'\n", encoding="utf-8")
    cfg, _ = load_config(cfg_path)
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    target.download_dir.mkdir(parents=True)
    (target.download_dir / "custom.info.json").write_text(json.dumps({"id": "abc123", "title": "离线修复"}), encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    runner = CliRunner()
    preview = runner.invoke(app, ["repair", "-c", str(cfg_path), "--only", "index"])
    assert preview.exit_code == 0, preview.output
    assert "index" in preview.output
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    assert not target.state_dir.exists()
    applied = runner.invoke(app, ["repair", "-c", str(cfg_path), "--only", "index", "--apply"])
    assert applied.exit_code == 0, applied.output
    with ArchiveDB(target.state_dir / "biliyt.db") as db:
        assert db.search("离线修复")
    assert list((target.state_dir / "reports").glob("run_*.json"))
    with StateStore(target.state_dir).acquire_sync_lock():
        locked = runner.invoke(app, ["repair", "-c", str(cfg_path), "--apply"])
    assert locked.exit_code == 3, locked.output
    (target.download_dir / "broken.info.json").write_text("{broken", encoding="utf-8")
    bad_preview = runner.invoke(app, ["repair", "-c", str(cfg_path), "--only", "index"])
    assert bad_preview.exit_code == 2, bad_preview.output
    assert "broken.info.json" in bad_preview.output


def test_corrupt_manifests_are_visible_in_sync_summary(tmp_path: Path) -> None:
    from biliyt.artifacts import ArtifactStore
    from biliyt.state import TargetRunSummary

    cfg = AppConfig.model_validate({"targets": [{"id": "demo", "source": "@demo"}]})
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    store = ArtifactStore(target.download_dir, target.id)
    corrupt = store.path_for("video", "bad")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{broken", encoding="utf-8")
    summary = TargetRunSummary(target.id, target.name, target.platform.value, target.url)
    SyncEngine(StateStore(target.state_dir))._summarize_artifacts(target, summary)
    assert summary.failed == 1
    assert summary.errors


def test_deferred_embedded_subtitle_stays_visible_in_repair_cli(tmp_path: Path) -> None:
    from biliyt.artifacts import Artifact, ArtifactManifest, ArtifactStore

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("targets:\n  - id: demo\n    source: '@demo'\n", encoding="utf-8")
    cfg, _ = load_config(cfg_path)
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    store = ArtifactStore(target.download_dir, target.id)
    store.save(ArtifactManifest(target.id, "video", "abc", artifacts=[
        Artifact("subtitle_embed", "pending", error="待重封装"),
    ], index_status="complete"))
    runner = CliRunner()
    preview = runner.invoke(app, ["repair", "-c", str(cfg_path)])
    assert preview.exit_code == 0, preview.output
    assert "重封装" in preview.output
    apply = runner.invoke(app, ["repair", "-c", str(cfg_path), "--apply"])
    assert apply.exit_code == 2, apply.output
    assert store.stats()["pending_repair"] == 1


def test_independent_stores_can_commit_same_item_concurrently(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from biliyt.artifacts import ArtifactManifest, ArtifactStore

    def write_one(index: int) -> None:
        ArtifactStore(tmp_path / "archive", "demo").save(ArtifactManifest(
            "demo", "video", "same-id", index_status="complete", context={"title": str(index)},
        ))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_one, range(24)))
    manifest = ArtifactStore(tmp_path / "archive", "demo").load("video", "same-id")
    assert manifest.index_status == "complete"
    assert manifest.context["title"].isdigit()
    assert not list((tmp_path / "archive").rglob("*.tmp"))
