from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from biliyt.cli import app
from biliyt.config import AppConfig, TargetConfig, resolve_target
from biliyt.db import ArchiveDB, extract_srt_plain_text, rebuild_target_from_disk
from biliyt.platform import Platform


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
