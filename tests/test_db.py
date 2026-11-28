from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from biliyt.cli import app
from biliyt.config import AppConfig, TargetConfig, resolve_target
from biliyt.db import (
    ArchiveDB,
    RebuildError,
    extract_srt_plain_text,
    index_video_info_file,
    rebuild_database,
    rebuild_target_from_disk,
)
from biliyt.platform import Platform
from biliyt.state import StateStore


def test_extract_srt_plain_text(tmp_path: Path) -> None:
    srt = tmp_path / "demo.ai-zh.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n大家好\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\n大家好\n\n"
        "3\n00:00:05,000 --> 00:00:08,000\n<b>今天聊斯宾诺莎的人格</b>\n",
        encoding="utf-8",
    )
    plain = extract_srt_plain_text(srt)
    assert plain == "大家好\n今天聊斯宾诺莎的人格"


def test_archive_db_upserts_idempotency_and_chinese_search(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "biliyt.db"
    with ArchiveDB(db_path) as db:
        db.upsert_profile(
            target_id="bili_u1",
            platform="bilibili",
            uid=23191782,
            user_info={"name": "未明子", "sign": "简介", "level": 6},
            relation_stat={"follower": 600000, "following": 70},
            upstat={"archive": {"view": 1000000}, "likes": 50000},
            collections=[{"meta": {"name": "现实观察", "total": 10}}],
        )
        db.upsert_video(
            target_id="bili_u1",
            platform="bilibili",
            info={
                "id": "BV17x411w7KC",
                "title": "【实事求是】地下工运讨论",
                "description": "视频简介测试",
                "upload_date": "20260802",
                "duration": 1800,
                "view_count": 50000,
                "webpage_url": "https://www.bilibili.com/video/BV17x411w7KC",
            },
            subtitle_text="我们在字幕中重点讨论了斯宾诺莎与工运闭环要件。",
        )
        # Re-upsert same video -> must stay 1 row and 2 FTS entries (video + subtitle)
        db.upsert_video(
            target_id="bili_u1",
            platform="bilibili",
            info={
                "id": "BV17x411w7KC",
                "title": "【实事求是】地下工运讨论",
                "description": "视频简介测试",
                "upload_date": "20260802",
            },
            subtitle_text="我们在字幕中重点讨论了斯宾诺莎与工运闭环要件。",
        )

        db.upsert_dynamic(
            target_id="bili_u1",
            item={
                "id_str": "1273763089596022809",
                "type": "DYNAMIC_TYPE_WORD",
                "modules": {
                    "module_author": {"name": "未明子", "pub_ts": 1700000000},
                    "module_stat": {"like": {"count": 100}, "comment": {"count": 10}, "forward": {"count": 1}},
                    "module_dynamic": {"desc": {"text": "敌言风变，我即为敌。"}},
                },
            },
        )
        db.upsert_article(
            target_id="bili_u1",
            payload={
                "cv_id": "50661719",
                "meta": {
                    "id": 50661719,
                    "title": "【旧作】喧闹",
                    "publish_time": 1700000000,
                    "words": 162,
                    "stats": {"view": 69712, "like": 236, "reply": 39},
                },
            },
            markdown_content="# 【旧作】喧闹\n\n因为达到斯宾诺莎的人格是不可能的",
        )
        cmt_n = db.upsert_comments(
            target_id="bili_u1",
            carrier_kind="video",
            oid="170001",
            rtype=1,
            carrier_title="【实事求是】地下工运讨论",
            source_url="https://www.bilibili.com/video/BV17x411w7KC",
            bundle={
                "pinned": [
                    {
                        "rpid": 1,
                        "ctime": 1700000000,
                        "like": 99,
                        "member": {"mid": 23191782, "uname": "未明子"},
                        "content": {"message": "置顶勘误：补充哲学参考书目"},
                        "replies": [
                            {
                                "rpid": 2,
                                "ctime": 1700000100,
                                "like": 12,
                                "member": {"mid": 999, "uname": "观众"},
                                "content": {"message": "感谢补充斯宾诺莎书单"},
                            }
                        ],
                    }
                ],
                "up_replied": [],
                "top": [],
            },
            up_uid=23191782,
        )
        assert cmt_n == 2

        db.upsert_favorite_folder(
            target_id="bili_u1",
            folder={"id": 777, "title": "书单"},
            medias=[{"bvid": "BV17x411w7KC", "title": "视频A", "upper": {"name": "作者"}, "duration": 60}],
        )

        st = db.stats()
        assert st["counts"]["profiles"] == 1
        assert st["counts"]["videos"] == 1
        assert st["counts"]["dynamics"] == 1
        assert st["counts"]["articles"] == 1
        assert st["counts"]["comments"] == 2
        assert st["counts"]["favorites"] == 1
        assert st["counts"]["favorite_items"] == 1

        # >= 3 char Chinese trigram FTS5 search hits subtitle, article, and comment
        hits_long = db.search("斯宾诺莎", target_id="bili_u1")
        assert {h.kind for h in hits_long} == {"subtitle", "article", "comment"}

        # < 3 char Chinese LIKE fallback hits video, subtitle, and comment
        hits_short = db.search("工运", target_id="bili_u1")
        assert {"video", "subtitle"}.issubset({h.kind for h in hits_short})

        hits_phil = db.search("哲学", target_id="bili_u1", kinds={"comment"})
        assert len(hits_phil) == 1
        assert "哲学" in hits_phil[0].snippet


def test_rebuild_from_disk_and_cli_commands(tmp_path: Path) -> None:
    cfg = AppConfig(
        targets=[
            TargetConfig(
                id="bili_demo",
                name="Demo UP",
                platform=Platform.BILIBILI,
                source="https://space.bilibili.com/23191782",
                enabled=True,
            )
        ]
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    root = target.download_dir
    root.mkdir(parents=True, exist_ok=True)

    # Seed profile, video+srt+comments, dynamic, article
    (root / "profile").mkdir()
    (root / "profile" / "profile.json").write_text(
        json.dumps({"uid": 23191782, "user_info": {"name": "未明子", "level": 6}}),
        encoding="utf-8",
    )
    (root / "2026-08-02 - 测试视频 [BV17x411w7KC].info.json").write_text(
        json.dumps({"id": "BV17x411w7KC", "title": "测试视频", "upload_date": "20260802"}),
        encoding="utf-8",
    )
    (root / "2026-08-02 - 测试视频 [BV17x411w7KC].ai-zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n闭环要件核心观点\n",
        encoding="utf-8",
    )
    (root / "2026-08-02 - 测试视频 [BV17x411w7KC].comments.json").write_text(
        json.dumps(
            {
                "oid": "170001",
                "type": 1,
                "pinned": [],
                "up_replied": [],
                "top": [
                    {
                        "rpid": 55,
                        "ctime": 1700000000,
                        "like": 5,
                        "member": {"mid": 1, "uname": "用户"},
                        "content": {"message": "非常认同闭环要件"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "targets:\n"
        "  - id: bili_demo\n"
        "    platform: bilibili\n"
        "    source: https://space.bilibili.com/23191782\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    with ArchiveDB(target.state_dir / "biliyt.db") as db:
        rebuilt = rebuild_target_from_disk(db, target)
        assert rebuilt["profiles"] == 1
        assert rebuilt["videos"] == 1
        assert rebuilt["comments"] == 1

    runner = CliRunner()
    res_rebuild = runner.invoke(app, ["db", "rebuild", "-c", str(cfg_file), "--reset"])
    assert res_rebuild.exit_code == 0, res_rebuild.output

    res_stats = runner.invoke(app, ["db", "stats", "-c", str(cfg_file)])
    assert res_stats.exit_code == 0, res_stats.output
    assert "videos" in res_stats.output

    res_search = runner.invoke(app, ["search", "闭环要件", "-c", str(cfg_file)])
    assert res_search.exit_code == 0, res_search.output
    assert "闭环要件" in res_search.output


@pytest.fixture
def multi_target_archive(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "targets:\n"
        "  - id: alpha\n    platform: youtube\n    source: https://www.youtube.com/@alpha\n"
        "  - id: beta\n    platform: youtube\n    source: https://www.youtube.com/@beta\n",
        encoding="utf-8",
    )
    from biliyt.config import load_config

    cfg, base = load_config(cfg_file)
    targets = [resolve_target(cfg, target, base) for target in cfg.targets]
    for target in targets:
        target.download_dir.mkdir(parents=True)
        (target.download_dir / "sample.info.json").write_text(
            json.dumps({"id": target.id, "title": f"searchable {target.id}"}), encoding="utf-8"
        )
    return cfg_file, targets, targets[0].state_dir / "biliyt.db"


def test_target_reset_rejected_before_database_open(multi_target_archive, monkeypatch) -> None:
    cfg_file, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)

    def unexpected_open(*args, **kwargs):
        raise AssertionError("database was opened")

    with monkeypatch.context() as patch:
        patch.setattr("biliyt.cli.ArchiveDB", unexpected_open)
        patch.setattr("biliyt.cli.rebuild_database", unexpected_open)
        result = CliRunner().invoke(
            app, ["db", "rebuild", "-c", str(cfg_file), "--target", "alpha", "--reset"]
        )
    assert result.exit_code == 1, result.output
    assert "--reset" in result.output
    with ArchiveDB(db_path) as db:
        assert {hit.target_id for hit in db.search("searchable")} == {"alpha", "beta"}


def test_single_target_rebuild_preserves_other_targets(multi_target_archive) -> None:
    cfg_file, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)
    (targets[0].download_dir / "second.info.json").write_text(
        json.dumps({"id": "second", "title": "second alpha"}), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["db", "rebuild", "-c", str(cfg_file), "-t", "alpha"])
    assert result.exit_code == 0, result.output
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["videos"] == 3
        assert len(db.search("searchable", target_id="beta")) == 1


@pytest.mark.parametrize("reset", [False, True])
def test_all_rebuilds_share_sync_writer_lock(multi_target_archive, reset) -> None:
    cfg_file, targets, db_path = multi_target_archive
    with StateStore(targets[0].state_dir).acquire_sync_lock():
        args = ["db", "rebuild", "-c", str(cfg_file)] + (["--reset"] if reset else [])
        result = CliRunner().invoke(app, args)
    assert result.exit_code == 3, result.output
    assert "另一个写入操作正在进行" in result.output
    assert not db_path.exists()


@pytest.mark.parametrize("bad_data", ["{broken", "[]", '{"title": "missing id"}'])
def test_bad_metadata_collects_errors_and_preserves_original(multi_target_archive, bad_data) -> None:
    _, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)
    bad_file = targets[0].download_dir / "broken.info.json"
    bad_file.write_text(bad_data, encoding="utf-8")
    errors = []
    with ArchiveDB(db_path) as db:
        counts = rebuild_target_from_disk(db, targets[0], errors=errors)
        assert counts["videos"] == 1
    assert len(errors) == 1 and str(bad_file) in errors[0]
    with pytest.raises(RebuildError, match="broken.info.json"):
        rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert {hit.target_id for hit in db.search("searchable")} == {"alpha", "beta"}
    assert not list(db_path.parent.glob(f".{db_path.name}.rebuild-*"))


def test_reset_replacement_failure_keeps_original_and_backup(multi_target_archive, monkeypatch) -> None:
    _, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)
    (targets[0].download_dir / "sample.info.json").unlink()

    def fail_replace(source, destination):
        assert Path(source).parent == db_path.parent
        assert Path(destination) == db_path
        raise PermissionError("simulated occupied database")

    monkeypatch.setattr("biliyt.db.os.replace", fail_replace)
    with pytest.raises(RebuildError, match="Backup:"):
        rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert len(db.search("searchable", target_id="alpha")) == 1
    backups = list(db_path.parent.glob("biliyt.db.backup-*.db"))
    assert len(backups) == 1
    with ArchiveDB(backups[0]) as backup:
        assert backup.stats()["counts"]["videos"] == 2
    assert not list(db_path.parent.glob(f".{db_path.name}.rebuild-*"))


def test_reset_import_exception_keeps_original(multi_target_archive, monkeypatch) -> None:
    _, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)

    def fail_import(*args, **kwargs):
        raise RuntimeError("simulated import interruption")

    monkeypatch.setattr("biliyt.db.rebuild_target_from_disk", fail_import)
    with pytest.raises(RebuildError, match="interruption"):
        rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["videos"] == 2
    assert not list(db_path.parent.glob(f".{db_path.name}.rebuild-*"))


def test_reset_successfully_replaces_and_retains_backup(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO sync_runs(mode, started_at, finished_at, summary_json) VALUES ('old', 'a', 'b', '{}')")
    connection.commit()
    (targets[0].download_dir / "sample.info.json").unlink()
    # An idle open reader prevents journal_mode=DELETE: release it before replacement.
    connection.close()
    rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["videos"] == 1
        assert not db.search("searchable", target_id="alpha")
    backups = list(db_path.parent.glob("biliyt.db.backup-*.db"))
    with ArchiveDB(backups[0]) as backup:
        assert backup.stats()["counts"]["videos"] == 2
        assert backup.stats()["counts"]["sync_runs"] == 1
    assert not list(db_path.parent.glob(f".{db_path.name}.rebuild-*"))



def test_reset_busy_database_preserves_committed_wal_backup(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    rebuild_database(db_path, targets)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO sync_runs(mode, started_at, finished_at, summary_json) "
            "VALUES ('wal-only', 'a', 'b', '{}')"
        )
        connection.commit()
        assert Path(str(db_path) + "-wal").stat().st_size > 0
        with pytest.raises(RebuildError, match="Backup:"):
            rebuild_database(db_path, targets, reset=True)
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
    finally:
        connection.close()
    backups = list(db_path.parent.glob("biliyt.db.backup-*.db"))
    with ArchiveDB(backups[0]) as backup:
        assert backup.stats()["counts"]["sync_runs"] == 1
    with ArchiveDB(db_path) as original:
        assert original.stats()["counts"]["videos"] == 2
    assert not list(db_path.parent.glob(f".{db_path.name}.rebuild-*"))



def test_v1_migration_is_versioned_and_deduplicates_exact_history(tmp_path: Path) -> None:
    from biliyt.db import _DDL_STATEMENTS

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    for statement in _DDL_STATEMENTS:
        connection.execute(statement)
    connection.execute("INSERT INTO schema_migrations VALUES (1, '2026-01-01', 'legacy')")
    for follower in (10, 10, 11):
        connection.execute(
            "INSERT INTO profile_snapshots(target_id, fetched_at, follower) VALUES ('old', '2026-01-01', ?)",
            (follower,),
        )
    for summary in ('{"targets": []}', '{"targets": []}', '{"targets": [], "different": true}'):
        connection.execute(
            "INSERT INTO sync_runs(mode, started_at, finished_at, summary_json) VALUES ('sync', 'a', 'b', ?)",
            (summary,),
        )
    connection.commit()
    connection.close()
    for _ in range(2):
        with ArchiveDB(db_path) as db:
            assert [row[0] for row in db._conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2]
            assert db.stats()["counts"]["profile_snapshots"] == 2
            assert db.stats()["counts"]["sync_runs"] == 2
            assert len({row[0] for row in db._conn.execute("SELECT snapshot_id FROM profile_snapshots")}) == 2
            assert len({row[0] for row in db._conn.execute("SELECT run_id FROM sync_runs")}) == 2


def test_profile_and_run_default_identities_preserve_same_second_changes(tmp_path: Path) -> None:
    with ArchiveDB(tmp_path / "archive.db") as db:
        for name in ("one", "one", "two"):
            db.upsert_profile(
                target_id="demo", platform="bilibili", uid=1, user_info={"name": name},
                fetched_at="2026-01-01T00:00:00+00:00", relation_stat={"follower": 10},
            )
        assert db.stats()["counts"]["profile_snapshots"] == 2
        for summary in ({"value": 1}, {"value": 1}, {"value": 2}):
            db.record_sync_run(mode="sync", started_at="a", finished_at="b", summary_dict=summary)
        assert db.stats()["counts"]["sync_runs"] == 2
        db.record_sync_run(mode="sync", started_at="a", finished_at="b", summary_dict={"run_id": "explicit"})
        db.record_sync_run(mode="sync", started_at="a", finished_at="b", summary_dict={}, run_id="explicit")
        assert db.stats()["counts"]["sync_runs"] == 3


def test_snapshot_rebuild_is_idempotent_and_preserves_latest_assets(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    target = targets[0]
    directory = target.download_dir / "profile"
    (directory / "snapshots").mkdir(parents=True)
    newer = {
        "snapshot_id": "newer", "uid": 42, "fetched_at": "2026-02-01T00:00:00+00:00",
        "user_info": {"name": "new name"}, "relation_stat": {"follower": 30},
        "collections": [{"title": "current"}], "avatar_relpath": "profile/avatar.png",
        "banner_relpath": "profile/banner.png",
    }
    older = {**newer, "snapshot_id": "older", "fetched_at": "2026-01-01T00:00:00+00:00",
             "user_info": {"name": "old name"}, "avatar_relpath": "profile/old-avatar.png"}
    (directory / "snapshots" / "new.json").write_text(json.dumps(newer), encoding="utf-8")
    (directory / "profile.json").write_text(json.dumps(older), encoding="utf-8")
    for _ in range(2):
        rebuild_database(db_path, targets)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["profile_snapshots"] == 2
        profile = db._conn.execute("SELECT * FROM profiles WHERE target_id=?", (target.id,)).fetchone()
        assert profile["name"] == "new name"
        assert profile["avatar_relpath"] == "profile/avatar.png"
        assert profile["banner_relpath"] == "profile/banner.png"
        assert json.loads(profile["collections_json"]) == newer["collections"]
    rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["profile_snapshots"] == 2
        assert db._conn.execute("SELECT name FROM profiles").fetchone()[0] == "new name"


def test_reset_preserves_database_only_history_and_removed_targets(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    with ArchiveDB(db_path) as db:
        for target_id in ("alpha", "removed"):
            for day in (1, 2):
                db.upsert_profile(
                    target_id=target_id, platform="youtube", uid="42", user_info={"name": target_id},
                    fetched_at=f"2026-01-0{day}T00:00:00+00:00", relation_stat={"follower": day},
                    avatar_relpath="profile/avatar.webp", banner_relpath="profile/banner.webp",
                    snapshot_id=f"{target_id}-{day}",
                )
        db.record_sync_run(
            mode="sync", started_at="a", finished_at="b", run_id="old-run",
            summary_dict={"targets": [{"target_id": "removed"}, {"target_id": "alpha"}], "custom": 9},
        )
    for _ in range(2):
        rebuild_database(db_path, targets, reset=True)
        with ArchiveDB(db_path) as db:
            assert db.stats()["counts"]["profile_snapshots"] == 4
            assert db.stats()["counts"]["sync_runs"] == 1
            profile = db._conn.execute("SELECT * FROM profiles WHERE target_id='removed'").fetchone()
            assert profile["follower"] == 2
            assert profile["avatar_relpath"] == "profile/avatar.webp"
            assert profile["banner_relpath"] == "profile/banner.webp"
            summary = json.loads(db._conn.execute("SELECT summary_json FROM sync_runs").fetchone()[0])
            assert summary["custom"] == 9
            assert len(summary["targets"]) == 2
    assert list((targets[0].download_dir / "profile" / "snapshots").glob("*.json"))
    assert list((db_path.parent / "profile-history").glob("*/*.json"))
    assert list((db_path.parent / "reports").glob("*.json"))


def test_report_import_keeps_whole_selected_runs_and_rejects_bad_json(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    reports = db_path.parent / "reports"
    reports.mkdir(parents=True)
    for name, target_ids in (("mixed", ["alpha", "beta"]), ("other", ["beta"])):
        report = {
            "mode": "sync", "started_at": "2026-01-01", "finished_at": "2026-01-02",
            "targets": [{"target_id": target_id} for target_id in target_ids],
        }
        (reports / f"{name}.json").write_text(json.dumps(report), encoding="utf-8")
    for _ in range(2):
        rebuild_database(db_path, [targets[0]])
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["sync_runs"] == 1
        assert len(json.loads(db._conn.execute("SELECT summary_json FROM sync_runs").fetchone()[0])["targets"]) == 2
    rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["sync_runs"] == 2
    (reports / "broken.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(RebuildError, match="broken.json"):
        rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["sync_runs"] == 2


def test_nested_video_rebuild_and_actual_path_overrides(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    target = targets[0]
    nested = target.download_dir / "custom" / "deep"
    nested.mkdir(parents=True)
    info = nested / "arbitrary.info.json"
    info.write_text(json.dumps({"id": "nested-id", "title": "nested item"}), encoding="utf-8")
    media = nested / "converted.mov"
    media.write_bytes(b"offline fixture")
    subtitle = nested / "different-name.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nactual subtitle content\n", encoding="utf-8")
    thumb = nested / "cover.avif"
    thumb.write_bytes(b"offline fixture")
    with ArchiveDB(db_path) as db:
        assert index_video_info_file(db, target, info, paths={
            "media": media, "subtitle": subtitle, "thumbnail": thumb,
        })
        row = db._conn.execute("SELECT * FROM videos WHERE video_id='nested-id'").fetchone()
        assert row["video_relpath"] == "custom/deep/converted.mov"
        assert row["subtitle_relpath"] == "custom/deep/different-name.srt"
        assert row["thumbnail_relpath"] == "custom/deep/cover.avif"
        assert row["subtitle_text"] == "actual subtitle content"
        with pytest.raises(ValueError):
            index_video_info_file(db, target, info, paths={"media": "../outside.mov"})
    rebuild_database(db_path, targets)
    with ArchiveDB(db_path) as db:
        assert db.stats()["counts"]["videos"] == 3
        assert db._conn.execute("SELECT video_relpath FROM videos WHERE video_id='nested-id'").fetchone()[0] == "custom/deep/converted.mov"



def test_profile_import_normalizes_offsets_and_keeps_unknown_time_older(multi_target_archive) -> None:
    _, targets, db_path = multi_target_archive
    target = targets[0]
    directory = target.download_dir / "profile"
    (directory / "snapshots").mkdir(parents=True)
    for name, timestamp in (("older", "2026-01-01T08:00:00+08:00"), ("newer", "2026-01-01T01:00:00Z")):
        (directory / "snapshots" / f"{name}.json").write_text(json.dumps({
            "snapshot_id": name, "fetched_at": timestamp, "user_info": {"name": name},
        }), encoding="utf-8")
    (directory / "profile.json").write_text(json.dumps({"user_info": {"name": "legacy no timestamp"}}), encoding="utf-8")
    rebuild_database(db_path, targets)
    with ArchiveDB(db_path) as db:
        row = db._conn.execute("SELECT name, updated_at FROM profiles").fetchone()
        assert tuple(row) == ("newer", "2026-01-01T01:00:00+00:00")
        assert db.stats()["counts"]["profile_snapshots"] == 3


def test_reset_history_export_error_retains_original(multi_target_archive, monkeypatch) -> None:
    _, targets, db_path = multi_target_archive
    with ArchiveDB(db_path) as db:
        db.upsert_profile(target_id="alpha", platform="youtube", uid=1, user_info={"name": "retained"})

    def fail_history_write(*args, **kwargs):
        raise OSError("simulated historical file write failure")

    monkeypatch.setattr("biliyt.db.atomic_write_json", fail_history_write)
    with pytest.raises(RebuildError, match="History export errors"):
        rebuild_database(db_path, targets, reset=True)
    with ArchiveDB(db_path) as db:
        assert db._conn.execute("SELECT name FROM profiles").fetchone()[0] == "retained"
        assert db.stats()["counts"]["profile_snapshots"] == 1
    assert not list(db_path.parent.glob(f".{db_path.name}.rebuild-*"))
