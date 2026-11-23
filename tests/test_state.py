"""Tests for archive reading, failure queue persistence, and run reports."""

from pathlib import Path

from biliyt.state import RunReport, StateStore, TargetRunSummary


def test_archive_and_failed_queue_lifecycle(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    arch = store.archive_path("bili_1")
    arch.write_text("bilibili BV1aaa\nbilibili BV1bbb_p1\n", encoding="utf-8")

    assert store.read_archive_video_ids("bili_1") == {"BV1aaa", "BV1bbb_p1"}

    rec1 = store.record_failure(
        target_id="bili_1",
        platform="bilibili",
        url="https://www.bilibili.com/video/BV1ccc",
        video_id="BV1ccc",
        title="Video C",
        error="HTTP 412",
    )
    assert rec1.attempts == 1

    rec2 = store.record_failure(
        target_id="bili_1",
        platform="bilibili",
        url="https://www.bilibili.com/video/BV1ccc",
        error="HTTP 412 again",
    )
    assert rec2.attempts == 2
    assert len(store.load_failed("bili_1")) == 1
    assert store.load_failed("bili_1")[0].title == "Video C"

    store.remove_failure("bili_1", "https://www.bilibili.com/video/BV1ccc")
    assert store.load_failed("bili_1") == []


def test_save_and_load_report(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    report = RunReport(
        started_at="2026-11-23T10:00:00+00:00",
        finished_at="2026-11-23T10:01:00+00:00",
        mode="sync",
        targets=[
            TargetRunSummary(
                target_id="yt_1",
                target_name="YT 1",
                platform="youtube",
                url="https://www.youtube.com/@demo/videos",
                discovered=3,
                downloaded=2,
            )
        ],
    )
    saved = store.save_report(report)
    assert saved.exists()
    loaded = store.load_last_report()
    assert loaded is not None
    assert loaded["mode"] == "sync"
    assert loaded["targets"][0]["downloaded"] == 2
