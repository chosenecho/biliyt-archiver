"""Tests for archive reading, failure queue persistence, and run reports."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from biliyt import atomic_io
from biliyt.state import RunReport, StateStore, SyncLockError, TargetRunSummary


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
    assert loaded["run_id"] == report.run_id
    assert str(UUID(report.run_id)) == report.run_id
    assert loaded["targets"][0]["partial"] == 0
    assert loaded["targets"][0]["index_failed"] == 0
    assert loaded["targets"][0]["pending_repair"] == 0
    assert loaded["warnings"] == []


def test_reports_from_same_second_keep_distinct_history(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    first = RunReport("start", "finish", "sync")
    second = RunReport("start", "finish", "repair", warnings=["index retry pending"])
    first_path = store.save_report(first)
    second_path = store.save_report(second)
    assert first.run_id in first_path.name
    assert second.run_id in second_path.name
    assert first_path != second_path
    assert len(list(store.reports_dir.glob("*.json"))) == 2
    assert store.load_last_report()["warnings"] == ["index retry pending"]


@pytest.mark.parametrize("operation", ["failure", "namespace", "report", "last_run"])
def test_rewrite_failure_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store = StateStore(tmp_path)
    store.record_failure(target_id="t", platform="youtube", url="url", error="old")
    store.append_namespace_archive("t", "profile", "key", meta="old")
    report = RunReport("start", "finish", "sync")
    report_path = store.save_report(report)
    paths = {
        "failure": store.failed_file,
        "namespace": store.namespace_archive_path("t", "profile"),
        "report": report_path,
        "last_run": store.last_run_file,
    }
    destination = paths[operation]
    original = destination.read_bytes()
    real_replace = atomic_io.os.replace

    def fail_replace(source, target):
        if Path(target) == destination:
            raise OSError("injected replacement failure")
        real_replace(source, target)

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replacement"):
        if operation == "failure":
            store.remove_failure("t", "url")
        elif operation == "namespace":
            store.append_namespace_archive("t", "profile", "key", meta="new", update_meta=True)
        else:
            report.warnings.append("new")
            store.save_report(report)
    assert destination.read_bytes() == original
    assert list(tmp_path.rglob("*.tmp")) == []
    if operation == "last_run":
        assert b"new" in report_path.read_bytes()


def test_summary_warning_lists_are_independent() -> None:
    first = TargetRunSummary("a", "A", "youtube", "url", partial=1, index_failed=1, pending_repair=1)
    second = TargetRunSummary("b", "B", "youtube", "url")
    first.warnings.append("subtitle missing")
    assert second.warnings == []
    assert (first.partial, first.index_failed, first.pending_repair) == (1, 1, 1)


def test_lock_conflict_mentions_general_write_operation(tmp_path: Path) -> None:
    first = StateStore(tmp_path)
    second = StateStore(tmp_path)
    with first.acquire_sync_lock():
        with pytest.raises(SyncLockError, match="另一个写入操作正在进行"):
            with second.acquire_sync_lock():
                pytest.fail("competing writer acquired lock")


def test_failure_credentials_are_redacted_and_public_urls_match(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    signed_url = "https://user:proxysecret@example.test/watch?v=video&p=2&sign=signedsecret"
    public = "https://example.test/watch?v=video&p=2"
    error = "SESSDATA=cookiesecret proxy https://user:proxysecret@proxy.test:8080"
    title = "SESSDATA=title-is-content"
    extra = {"json_path": "profile/item.json", "md_path": "profile/item.md"}
    first = store.record_failure(
        target_id="t", platform="youtube", url=signed_url, error=error, title=title, extra=extra
    )
    assert first.url == public
    assert first.title == title
    assert first.extra == extra
    stored = store.failed_file.read_text(encoding="utf-8")
    for secret in ("cookiesecret", "proxysecret", "signedsecret"):
        assert secret not in stored
    assert "title-is-content" in stored
    second = store.record_failure(
        target_id="t", platform="youtube", url=public + "&token=anothersecret", error=error
    )
    assert second.attempts == 2
    assert len(store.load_failed()) == 1
    store.remove_failure("t", signed_url)
    assert store.load_failed() == []

    # Old files are sanitized on read and use the same normalized removal key.
    store.failed_file.write_text(
        json.dumps({"target_id": "t", "platform": "youtube", "url": signed_url, "error": error}),
        encoding="utf-8",
    )
    legacy = store.load_failed()[0]
    assert legacy.url == public
    assert "cookiesecret" not in legacy.error
    assert "proxysecret" not in legacy.error
    store.remove_failure("t", public)
    assert store.load_failed() == []


def test_report_errors_and_warnings_are_redacted_without_mutating_input(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    message = (
        "SESSDATA=cookiesecret\n"
        "Cookie: bili_jct=csrfsecret; DedeUserID=idsecret\n"
        "proxy https://user:proxysecret@proxy.test:8080\n"
        "Authorization: Bearer bearer_secret"
    )
    target = TargetRunSummary(
        "t", "SESSDATA=normal-title", "youtube", "https://example.test/watch?v=video",
        errors=[message], warnings=[message],
    )
    report = RunReport("start", "finish", "sync", targets=[target], warnings=[message])
    saved = store.save_report(report)
    assert saved.read_bytes() == store.last_run_file.read_bytes()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["targets"][0]["target_name"] == target.target_name
    assert payload["targets"][0]["url"] == target.url
    for messages in (payload["warnings"], payload["targets"][0]["errors"], payload["targets"][0]["warnings"]):
        for secret in ("cookiesecret", "csrfsecret", "idsecret", "proxysecret", "bearer_secret"):
            assert secret not in messages[0]
        assert "[REDACTED]" in messages[0]
    assert report.warnings == [message]
    assert target.errors == [message]
    assert target.warnings == [message]
