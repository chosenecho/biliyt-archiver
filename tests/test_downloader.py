"""Tests for SyncEngine two-stage pipeline, retry_failed, and CLI smoke tests."""

from pathlib import Path
from typing import Any

import pytest
import yt_dlp
from typer.testing import CliRunner

from biliyt.cli import app
from biliyt.config import AppConfig, resolve_target
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


def test_cli_init_check_and_status(tmp_path: Path) -> None:
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
    assert len(warnings) == 1
    assert "429" in warnings[0]
    assert state.load_failed("yt_sub429") == []


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
