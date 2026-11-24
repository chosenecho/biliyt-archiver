from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from biliyt.bilibili.archiver import BilibiliArchiver
from biliyt.bilibili.client import BilibiliRiskControlError
from biliyt.config import (
    AppConfig,
    BilibiliArchiveConfig,
    CommentArchiveConfig,
    PlatformConfig,
    TargetConfig,
    resolve_target,
)
from biliyt.db import ArchiveDB
from biliyt.platform import Platform
from biliyt.state import FailedRecord, StateStore


class _FakeBilibiliClient:
    """In-memory stub of BilibiliWebClient for offline archiver unit tests."""

    def __init__(self) -> None:
        self.dynamic_pages_called = 0
        self.sub_reply_calls: list[int | str] = []
        self.fail_on_comments = False

    def __enter__(self) -> _FakeBilibiliClient:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def get_user_info(self, uid: int) -> dict[str, Any]:
        return {
            "mid": uid,
            "name": "测试UP",
            "sign": "测试简介",
            "level": 6,
            "face": "https://i0.hdslb.com/bfs/face/avatar.jpg",
            "top_photo": "https://i0.hdslb.com/bfs/space/banner.jpg",
        }

    def get_relation_stat(self, uid: int) -> dict[str, Any]:
        return {"follower": 12345, "following": 10}

    def get_upstat(self, uid: int) -> dict[str, Any]:
        return {"archive": {"view": 99999}, "likes": 8888}

    def get_collections(self, uid: int) -> dict[str, Any]:
        return {
            "items_lists": {
                "seasons_list": [{"meta": {"name": "合集一", "total": 5, "season_id": 10}}]
            }
        }

    def get_dynamics_page(self, uid: int, *, offset: str = "") -> dict[str, Any]:
        self.dynamic_pages_called += 1
        if offset == "":
            return {
                "has_more": True,
                "offset": "page2",
                "items": [
                    {
                        "id_str": "dyn_pinned",
                        "type": "DYNAMIC_TYPE_WORD",
                        "basic": {"comment_id_str": "dyn_pinned", "comment_type": 17},
                        "modules": {
                            "module_tag": {"text": "置顶"},
                            "module_author": {"name": "测试UP", "pub_ts": 1700000000},
                            "module_stat": {"like": {"count": 10}, "comment": {"count": 2}, "forward": {"count": 1}},
                            "module_dynamic": {"desc": {"text": "置顶动态"}},
                        },
                    },
                    {
                        "id_str": "dyn_2",
                        "type": "DYNAMIC_TYPE_DRAW",
                        "basic": {"comment_id_str": "2002", "comment_type": 11},
                        "modules": {
                            "module_author": {"name": "测试UP", "pub_ts": 1700000100},
                            "module_stat": {"like": {"count": 20}, "comment": {"count": 3}, "forward": {"count": 0}},
                            "module_dynamic": {
                                "desc": {"text": "最新动态"},
                                "major": {"draw": {"items": [{"src": "https://i0.hdslb.com/bfs/dyn/img1.jpg"}]}},
                            },
                        },
                    },
                    {
                        "id_str": "dyn_1",
                        "type": "DYNAMIC_TYPE_WORD",
                        "basic": {"comment_id_str": "dyn_1", "comment_type": 17},
                        "modules": {
                            "module_author": {"name": "测试UP", "pub_ts": 1700000050},
                            "module_stat": {"like": {"count": 5}, "comment": {"count": 1}, "forward": {"count": 0}},
                            "module_dynamic": {"desc": {"text": "旧动态1"}},
                        },
                    },
                ],
            }
        return {
            "has_more": False,
            "offset": "",
            "items": [
                {
                    "id_str": "dyn_0",
                    "type": "DYNAMIC_TYPE_WORD",
                    "basic": {"comment_id_str": "dyn_0", "comment_type": 17},
                    "modules": {
                        "module_author": {"name": "测试UP", "pub_ts": 1699999000},
                        "module_stat": {"like": {"count": 1}, "comment": {"count": 0}, "forward": {"count": 0}},
                        "module_dynamic": {"desc": {"text": "更早动态"}},
                    },
                }
            ],
        }

    def get_articles_page(self, uid: int, *, pn: int = 1, ps: int = 30) -> dict[str, Any]:
        return {
            "count": 1,
            "articles": [
                {
                    "id": 987654,
                    "title": "深度长文",
                    "author": {"name": "测试UP"},
                    "publish_time": 1700000000,
                    "stats": {"view": 1000, "like": 80, "reply": 15},
                }
            ],
        }

    def get_article_html(self, cv_id: int | str) -> str:
        return '<div id="read-article-holder"><p>专栏正文内容</p><img data-src="//i0.hdslb.com/bfs/art/p1.png" /></div>'

    def get_created_favorites(self, uid: int) -> dict[str, Any]:
        return {"list": [{"id": 777, "title": "参考资料", "media_count": 1}]}

    def get_favorite_resources_page(self, media_id: int, *, pn: int = 1, ps: int = 20) -> dict[str, Any]:
        return {
            "has_more": False,
            "medias": [
                {
                    "bvid": "BV17x411w7KC",
                    "title": "收藏的视频",
                    "upper": {"name": "别人UP"},
                    "duration": 120,
                    "fav_time": 1700000000,
                }
            ],
        }

    def get_replies_main_page(
        self, *, oid: str | int, rtype: int, next_offset: str = "", mode: int = 3
    ) -> dict[str, Any]:
        if self.fail_on_comments:
            raise BilibiliRiskControlError("风控拦截 -352", code=-352)
        return {
            "cursor": {"all_count": 3, "is_end": True, "pagination_reply": {"next_offset": ""}},
            "top": {
                "upper": {
                    "rpid": 100,
                    "ctime": 1700000000,
                    "like": 88,
                    "rcount": 0,
                    "member": {"mid": 23191782, "uname": "测试UP"},
                    "content": {"message": "置顶勘误"},
                    "replies": [],
                }
            },
            "replies": [
                {
                    "rpid": 100,  # duplicate of pinned -> must be deduped from top
                    "ctime": 1700000000,
                    "like": 88,
                    "member": {"mid": 23191782, "uname": "测试UP"},
                    "content": {"message": "置顶勘误"},
                    "replies": [],
                },
                {
                    "rpid": 200,
                    "ctime": 1700000100,
                    "like": 55,
                    "rcount": 2,
                    "up_action": {"reply": True},
                    "member": {"mid": 88888, "uname": "粉丝甲"},
                    "content": {"message": "这里有个问题"},
                    "replies": [],
                },
                {
                    "rpid": 300,
                    "ctime": 1700000200,
                    "like": 20,
                    "rcount": 0,
                    "up_action": {"reply": False},
                    "member": {"mid": 99999, "uname": "粉丝乙"},
                    "content": {"message": "普通热评"},
                    "replies": [],
                },
            ],
        }

    def get_sub_replies_page(
        self, *, oid: str | int, rtype: int, root_rpid: int | str, pn: int = 1, ps: int = 20
    ) -> dict[str, Any]:
        self.sub_reply_calls.append(root_rpid)
        return {
            "replies": [
                {
                    "rpid": 201,
                    "ctime": 1700000150,
                    "like": 30,
                    "member": {"mid": 23191782, "uname": "测试UP"},
                    "content": {"message": "UP主亲自回复"},
                }
            ]
        }

    def download_binary(self, url: str, *, referer: str | None = None) -> tuple[bytes, str]:
        return b"FAKEIMAGEBYTES", "image/jpeg"


def _make_target(tmp_path: Path) -> Any:
    cfg = AppConfig(
        platforms={
            Platform.BILIBILI: PlatformConfig(
                js_runtime=None,
                archive=BilibiliArchiveConfig(
                    profile=True,
                    videos=True,
                    dynamics=True,
                    articles=True,
                    favorites=True,
                    dynamics_limit=50,
                    articles_limit=50,
                    api_sleep_min=0.0,
                    api_sleep_max=0.0,
                    comments=CommentArchiveConfig(enabled=True, top_n=50),
                ),
            )
        },
        targets=[
            TargetConfig(
                id="bili_test",
                name="测试UP",
                platform=Platform.BILIBILI,
                source="https://space.bilibili.com/23191782",
                enabled=True,
            )
        ],
    )
    return resolve_target(cfg, cfg.targets[0], tmp_path)


def test_archive_config_three_layer_merge(tmp_path: Path) -> None:
    cfg = AppConfig(
        platforms={
            Platform.BILIBILI: PlatformConfig(
                js_runtime=None,
                archive=BilibiliArchiveConfig(
                    profile=True,
                    dynamics=True,
                    comments=CommentArchiveConfig(enabled=True, top_n=50),
                ),
            )
        },
        targets=[
            TargetConfig(
                id="bili_override",
                source="23191782",
                archive=BilibiliArchiveConfig(
                    articles=True,
                    comments=CommentArchiveConfig(top_n=25),
                ),
            )
        ],
    )
    resolved = resolve_target(cfg, cfg.targets[0], tmp_path)
    assert resolved.archive.profile is True
    assert resolved.archive.dynamics is True
    assert resolved.archive.articles is True
    # Nested comments.enabled preserved from platform while top_n overridden by target
    assert resolved.archive.comments.enabled is True
    assert resolved.archive.comments.top_n == 25


def test_full_archiver_pipeline_and_idempotency(tmp_path: Path) -> None:
    target = _make_target(tmp_path)
    state = StateStore(target.state_dir)

    # Pre-seed one downloaded video .info.json so comment discovery picks up video + dynamic + article
    target.download_dir.mkdir(parents=True, exist_ok=True)
    (target.download_dir / "2026-08-02 - 示例视频 [BV17x411w7KC].info.json").write_text(
        json.dumps({"id": "BV17x411w7KC", "title": "示例视频"}),
        encoding="utf-8",
    )

    fake_client = _FakeBilibiliClient()
    archiver = BilibiliArchiver(state, client_factory=lambda _: fake_client)

    # First full sync
    res1 = archiver.sync_all(target, full=True)
    assert res1["profile"].saved == 1
    assert res1["favorites"].saved == 1
    assert res1["articles"].saved == 1
    assert res1["dynamics"].saved == 4
    # 1 video + 4 dynamics + 1 article = 6 comment carriers
    assert res1["comments"].saved == 6

    # Check output files on disk
    assert (target.download_dir / "profile" / "profile.md").exists()
    assert (target.download_dir / "profile" / "collections.json").exists()
    assert (target.download_dir / "favorites" / "index.md").exists()
    assert list((target.download_dir / "favorites").glob("*.mp4")) == []
    assert list((target.download_dir / "articles").glob("*/article.md"))
    assert list((target.download_dir / "dynamics").glob("*/dynamic.md"))

    # Verify video comment bundle has pinned + UP reply enriched + deduped top
    v_cmt_path = target.download_dir / "2026-08-02 - 示例视频 [BV17x411w7KC].comments.json"
    assert v_cmt_path.exists()
    bundle = json.loads(v_cmt_path.read_text(encoding="utf-8"))
    assert len(bundle["pinned"]) == 1
    assert len(bundle["up_replied"]) == 1
    assert bundle["up_replied"][0]["replies"][0]["content"]["message"] == "UP主亲自回复"
    # rpid 100 was deduped from top -> 200 and 300 remain
    assert [r["rpid"] for r in bundle["top"]] == [200, 300]
    assert 200 in fake_client.sub_reply_calls

    # Second incremental sync -> must save 0 new dynamics/articles/comments and early-stop page 1
    fake_client.dynamic_pages_called = 0
    res2 = archiver.sync_all(target, only={"dynamics", "articles", "comments"}, full=False)
    assert res2["dynamics"].saved == 0
    assert fake_client.dynamic_pages_called == 1  # early stop before page 2!
    assert res2["articles"].saved == 0
    assert res2["comments"].saved == 0


def test_archiver_handles_risk_control_gracefully(tmp_path: Path) -> None:
    target = _make_target(tmp_path)
    state = StateStore(target.state_dir)
    target.download_dir.mkdir(parents=True, exist_ok=True)
    (target.download_dir / "2026-08-02 - 示例视频 [BV17x411w7KC].info.json").write_text(
        json.dumps({"id": "BV17x411w7KC", "title": "示例视频"}),
        encoding="utf-8",
    )

    fake_client = _FakeBilibiliClient()
    fake_client.fail_on_comments = True
    archiver = BilibiliArchiver(state, client_factory=lambda _: fake_client)

    res = archiver.sync_all(target, only={"profile", "comments"})
    assert res["profile"].saved == 1
    assert "circuit_breaker" in res
    assert res["circuit_breaker"].failed == 1


def test_articles_early_stop_and_full_batch_limit(tmp_path: Path) -> None:
    target = _make_target(tmp_path)
    state = StateStore(target.state_dir)

    class _PagedArticlesClient(_FakeBilibiliClient):
        def __init__(self) -> None:
            super().__init__()
            self.article_pages_called: list[int] = []

        def get_articles_page(self, uid: int, *, pn: int = 1, ps: int = 30) -> dict[str, Any]:
            self.article_pages_called.append(pn)
            all_items = [
                {
                    "id": 1000 + i,
                    "title": f"专栏 {i}",
                    "author": {"name": "测试UP"},
                    "publish_time": 1700000000 - i * 100,
                    "stats": {"view": 100, "like": 10, "reply": 1},
                }
                for i in range(4)
            ]
            # Simulate 2 items per page even though ps=30 by returning 30-like page boundary or full 2-page mock
            if pn == 1:
                return {"count": 32, "articles": all_items[:2] + [{} for _ in range(28)]}
            return {"count": 32, "articles": all_items[2:]}

    client = _PagedArticlesClient()
    archiver = BilibiliArchiver(state, client_factory=lambda _: client)

    # Batch 1: --full --limit 2 saves first 2 articles
    r1 = archiver.sync_all(target, only={"articles"}, full=True, limit_override=2)
    assert r1["articles"].saved == 2
    assert state.read_namespace_archive(target.id, "articles") == {"cv1000", "cv1001"}

    # Incremental sync now hits 2 consecutive archived items on page 1 -> early stops before page 2!
    client.article_pages_called.clear()
    r_inc = archiver.sync_all(target, only={"articles"}, full=False)
    assert r_inc["articles"].saved == 0
    assert client.article_pages_called == [1]

    # Batch 2: --full --limit 2 skips page 1's 2 archived items and saves page 2's 2 items
    client.article_pages_called.clear()
    r2 = archiver.sync_all(target, only={"articles"}, full=True, limit_override=2)
    assert r2["articles"].already_archived == 2
    assert r2["articles"].saved == 2
    assert state.read_namespace_archive(target.id, "articles") == {"cv1000", "cv1001", "cv1002", "cv1003"}


def test_dynamics_full_batch_limit(tmp_path: Path) -> None:
    target = _make_target(tmp_path)
    state = StateStore(target.state_dir)
    client = _FakeBilibiliClient()
    archiver = BilibiliArchiver(state, client_factory=lambda _: client)

    # 4 dynamics total across 2 pages -> backfill 2 at a time with full=True, limit_override=2
    r1 = archiver.sync_all(target, only={"dynamics"}, full=True, limit_override=2)
    assert r1["dynamics"].saved == 2

    r2 = archiver.sync_all(target, only={"dynamics"}, full=True, limit_override=2)
    assert r2["dynamics"].saved == 2
    assert state.count_namespace_archive(target.id, "dynamics") == 4


def test_favorites_fingerprint_skips_unchanged_and_resyncs_on_change(tmp_path: Path) -> None:
    target = _make_target(tmp_path)
    state = StateStore(target.state_dir)

    class _FavClient(_FakeBilibiliClient):
        def __init__(self) -> None:
            super().__init__()
            self.media_count = 1
            self.resource_calls = 0

        def get_created_favorites(self, uid: int) -> dict[str, Any]:
            return {"list": [{"id": 777, "title": "参考资料", "media_count": self.media_count, "mtime": 1700000000}]}

        def get_favorite_resources_page(self, media_id: int, *, pn: int = 1, ps: int = 20) -> dict[str, Any]:
            self.resource_calls += 1
            return super().get_favorite_resources_page(media_id, pn=pn, ps=ps)

    client = _FavClient()
    archiver = BilibiliArchiver(state, client_factory=lambda _: client)

    # First sync fetches folder pages
    r1 = archiver.sync_all(target, only={"favorites"}, full=False)
    assert r1["favorites"].saved == 1
    assert client.resource_calls == 1

    # Second incremental sync with unchanged count/mtime skips pagination
    r2 = archiver.sync_all(target, only={"favorites"}, full=False)
    assert r2["favorites"].saved == 0
    assert r2["favorites"].already_archived == 1
    assert client.resource_calls == 1

    # Full sync forces re-fetch even if unchanged
    r_full = archiver.sync_all(target, only={"favorites"}, full=True)
    assert r_full["favorites"].saved == 1
    assert client.resource_calls == 2

    # Changing media_count triggers incremental re-sync
    client.media_count = 2
    r3 = archiver.sync_all(target, only={"favorites"}, full=False)
    assert r3["favorites"].saved == 1
    assert client.resource_calls == 3


def test_comments_recent_refresh_window_and_retry_writes_sqlite(tmp_path: Path) -> None:
    cfg = AppConfig(
        paths={"download_dir": str(tmp_path / "dl"), "state_dir": str(tmp_path / "state")},
        targets=[
            TargetConfig(
                id="bili_cmt",
                source="23191782",
                archive=BilibiliArchiveConfig(
                    comments=CommentArchiveConfig(
                        enabled=True,
                        targets=["videos"],
                        top_n=10,
                        refresh_recent_days=7,
                    )
                ),
            )
        ],
    )
    target = resolve_target(cfg, cfg.targets[0], tmp_path)
    state = StateStore(target.state_dir)
    target.download_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    recent_ts = int((now - timedelta(days=2)).timestamp())
    old_ts = int((now - timedelta(days=30)).timestamp())

    # Two videos: BV17x411w7KC (recent) and BV1xx411c7mD (old)
    (target.download_dir / "2026-11-22 - 近期视频 [BV17x411w7KC].info.json").write_text(
        json.dumps({"id": "BV17x411w7KC", "title": "近期视频", "timestamp": recent_ts}),
        encoding="utf-8",
    )
    (target.download_dir / "2026-10-20 - 历史视频 [BV1xx411c7mD].info.json").write_text(
        json.dumps({"id": "BV1xx411c7mD", "title": "历史视频", "timestamp": old_ts}),
        encoding="utf-8",
    )

    client = _FakeBilibiliClient()
    archiver = BilibiliArchiver(state, client_factory=lambda _: client)

    # Test --full --limit 1 fetches only 1 carrier
    r_batch = archiver.sync_all(target, only={"comments"}, full=True, limit_override=1)
    assert r_batch["comments"].saved == 1

    # Fetch remaining carrier
    r_rest = archiver.sync_all(target, only={"comments"}, full=True)
    assert r_rest["comments"].saved == 1

    # Immediate re-sync: both inside 12h cooldown -> 0 saved
    r_cool = archiver.sync_all(target, only={"comments"}, full=False)
    assert r_cool["comments"].saved == 0
    assert r_cool["comments"].already_archived == 2

    # Backdate archive timestamps to 24h ago: only the recent video (2d old <= 7d) refreshes
    stale_iso = (now - timedelta(hours=24)).replace(microsecond=0).isoformat()
    for key in list(state.read_namespace_archive(target.id, "comments")):
        state.append_namespace_archive(target.id, "comments", key, meta=stale_iso, update_meta=True)

    r_win = archiver.sync_all(target, only={"comments"}, full=False)
    assert r_win["comments"].saved == 1
    assert r_win["comments"].already_archived == 1

    # Verify retry_failed_item for comment writes to SQLite as well
    with ArchiveDB(state.state_dir / "biliyt.db") as db:
        db.reset()
        assert db.stats()["counts"]["comments"] == 0

    retry_rec = FailedRecord(
        target_id=target.id,
        platform="bilibili",
        url="https://www.bilibili.com/video/BV17x411w7KC",
        video_id="video:170001",
        title="评论: 近期视频",
        error="timeout",
        item_type="comment",
        extra={
            "carrier_kind": "videos",
            "oid": "170001",
            "rtype": 1,
            "title": "近期视频",
            "json_path": str(target.download_dir / "retry.comments.json"),
            "md_path": str(target.download_dir / "retry.comments.md"),
            "archive_key": "video:170001",
        },
    )
    outcome, detail = archiver.retry_failed_item(target, retry_rec)
    assert outcome == "downloaded", detail
    with ArchiveDB(state.state_dir / "biliyt.db") as db:
        assert db.stats()["counts"]["comments"] >= 3
