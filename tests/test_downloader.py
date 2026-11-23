"""Tests for SyncEngine two-stage pipeline, retry_failed, and CLI smoke tests."""

from pathlib import Path
from typing import Any

import yt_dlp
from typer.testing import CliRunner

from biliyt.cli import app
from biliyt.config import AppConfig, resolve_target
from biliyt.downloader import SyncEngine
from biliyt.state import StateStore


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
