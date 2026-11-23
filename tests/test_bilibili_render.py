from __future__ import annotations

from biliyt.bilibili.render import (
    extract_dynamic_image_urls,
    extract_legacy_html_images,
    render_article_markdown,
    render_comments_markdown,
    render_dynamic_markdown,
    render_favorite_folder_markdown,
    render_favorites_index_markdown,
    render_profile_markdown,
)


def test_render_dynamic_markdown_with_forward_and_local_images() -> None:
    item = {
        "id_str": "999888777",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"name": "未明子", "pub_ts": 1700000000},
            "module_stat": {"like": {"count": 100}, "comment": {"count": 20}, "forward": {"count": 5}},
            "module_dynamic": {
                "desc": {"text": "转发补充说明"},
                "major": None,
            },
        },
        "orig": {
            "id_str": "111222333",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"name": "原作者"},
                "module_dynamic": {
                    "desc": {"text": "原动态图文"},
                    "major": {
                        "draw": {
                            "items": [{"src": "https://i0.hdslb.com/bfs/new_dyn/pic1.jpg"}]
                        }
                    },
                },
            },
        },
    }
    urls = extract_dynamic_image_urls(item)
    assert urls == ["https://i0.hdslb.com/bfs/new_dyn/pic1.jpg"]

    md = render_dynamic_markdown(item, {"https://i0.hdslb.com/bfs/new_dyn/pic1.jpg": "images/01_abc.jpg"})
    assert "转发补充说明" in md
    assert "## 转发原动态" in md
    assert "![图片 1](images/01_abc.jpg)" in md


def test_render_article_markdown_legacy_html_and_opus() -> None:
    meta = {
        "id": 123456,
        "title": "测试专栏标题",
        "author": {"name": "测试UP"},
        "publish_time": 1700000000,
        "stats": {"view": 500, "like": 30, "reply": 10},
    }
    html_text = """
    <html><body>
      <div id="read-article-holder">
        <h2>第一节</h2>
        <p>正文段落与<strong>重点</strong></p>
        <img data-src="//i0.hdslb.com/bfs/article/a.png" alt="配图" />
      </div>
    </body></html>
    """
    imgs = extract_legacy_html_images(html_text)
    assert imgs == ["https://i0.hdslb.com/bfs/article/a.png"]

    md = render_article_markdown(
        meta=meta,
        html_text=html_text,
        image_map={"https://i0.hdslb.com/bfs/article/a.png": "images/01_a.png"},
    )
    assert "# 测试专栏标题" in md
    assert "## 第一节" in md
    assert "**重点**" in md
    assert "![配图](images/01_a.png)" in md

    # Also test /x/article/view `opus.content.paragraphs` with numeric node_type=1
    opus_view = {
        "opus_id": 1214983026184814595,
        "content": {
            "paragraphs": [
                {"para_type": 1, "text": {"nodes": [{"node_type": 1, "word": {"words": "第一段\r"}}]}},
                {"para_type": 1, "text": {"nodes": [{"node_type": 1, "word": {"words": "第二段", "style": {"bold": True}}}]}},
            ]
        },
    }
    md_opus = render_article_markdown(meta=meta, opus_detail=opus_view)
    assert "第一段\n\n**第二段**" in md_opus


def test_render_favorites_markdown_lists_metadata_only() -> None:
    folder = {"id": 555, "title": "推荐书单", "media_count": 2, "_md_filename": "fav_555_推荐书单.md"}
    medias = [
        {
            "bvid": "BV1xx411c7mD",
            "title": "经典视频",
            "upper": {"name": "其他UP"},
            "duration": 600,
            "fav_time": 1700000000,
        }
    ]
    idx_md = render_favorites_index_markdown(23191782, [folder])
    assert "推荐书单" in idx_md
    assert "不下载他人视频" in idx_md

    folder_md = render_favorite_folder_markdown(folder, medias)
    assert "BV1xx411c7mD" in folder_md
    assert "经典视频" in folder_md
    assert "其他UP" in folder_md


def test_render_comments_markdown_sections() -> None:
    bundle = {
        "fetched_at": "2026-11-23T12:00:00+00:00",
        "total_count": 185,
        "pinned": [
            {
                "rpid": 1,
                "ctime": 1700000000,
                "like": 99,
                "member": {"mid": 23191782, "uname": "未明子"},
                "content": {"message": "勘误说明"},
                "replies": [],
            }
        ],
        "up_replied": [
            {
                "rpid": 2,
                "ctime": 1700000100,
                "like": 50,
                "member": {"mid": 10001, "uname": "观众A"},
                "content": {"message": "请问参考文献？"},
                "replies": [
                    {
                        "rpid": 3,
                        "ctime": 1700000200,
                        "like": 40,
                        "member": {"mid": 23191782, "uname": "未明子"},
                        "content": {"message": "见专栏第二篇"},
                    }
                ],
            }
        ],
        "top": [
            {
                "rpid": 2,
                "ctime": 1700000100,
                "like": 50,
                "member": {"mid": 10001, "uname": "观众A"},
                "content": {"message": "请问参考文献？"},
                "replies": [],
            }
        ],
    }
    md = render_comments_markdown(
        title="示例视频",
        oid="12345",
        rtype=1,
        bundle=bundle,
        up_uid=23191782,
    )
    assert "## 置顶评论" in md
    assert "勘误说明" in md
    assert "## UP 主参与回复" in md
    assert "见专栏第二篇" in md
    assert "**[UP主]**" in md
    assert "## 热门评论（1 条）" in md


def test_render_profile_markdown() -> None:
    md = render_profile_markdown(
        uid=23191782,
        user_info={"name": "未明子", "sign": "简介内容", "level": 6},
        relation_stat={"follower": 100000, "following": 20},
        upstat={"archive": {"view": 5000000}, "likes": 300000},
        collections=[{"meta": {"name": "现实观察", "total": 12, "season_id": 88}}],
        avatar_rel="assets/01_a.jpg",
    )
    assert "# 未明子 (UID: 23191782)" in md
    assert "现实观察" in md
    assert "![头像](assets/01_a.jpg)" in md
