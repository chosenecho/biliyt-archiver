"""Markdown renderers and local image downloader for Bilibili extended archive content."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..artifacts import Artifact, clean_error, public_url
from ..atomic_io import atomic_write_bytes
from .client import BilibiliAuthError, BilibiliRiskControlError, BilibiliWebClient

_CN_TZ = timezone(timedelta(hours=8))
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_MULTISPACE_RE = re.compile(r"\s+")
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def sanitize_filename(name: str, *, max_len: int = 80, fallback: str = "untitled") -> str:
    """Make a Windows-safe filename segment."""
    cleaned = _INVALID_FILENAME_RE.sub("_", (name or "").strip())
    cleaned = _MULTISPACE_RE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or fallback


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw


def format_unix_ts(ts: int | float | str | None) -> str:
    """Format a Unix timestamp into China Standard Time `YYYY-MM-DD HH:MM:SS`."""
    if ts is None or ts == "":
        return "unknown"
    try:
        val = int(float(ts))
    except (ValueError, TypeError):
        return str(ts)
    if val <= 0:
        return "unknown"
    return datetime.fromtimestamp(val, tz=_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def format_unix_date(ts: int | float | str | None) -> str:
    """Format a Unix timestamp into `YYYY-MM-DD`."""
    formatted = format_unix_ts(ts)
    return formatted[:10] if formatted != "unknown" else "unknown"


def _guess_image_ext(url: str, content_type: str = "") -> str:
    if content_type in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[content_type]
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


class ImageLocalizer:
    """Download remote Bilibili images into a local folder and return `{orig_url: rel_path}`."""

    def __init__(
        self,
        client: BilibiliWebClient,
        *,
        enabled: bool = True,
        referer: str = "https://www.bilibili.com/",
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._referer = referer
        self.last_artifacts: list[Artifact] = []

    def localize(self, urls: list[str], dest_dir: Path, *, rel_prefix: str = "images") -> dict[str, str]:
        mapping: dict[str, str] = {}
        self.last_artifacts = []
        unique_urls = list(dict.fromkeys(normalize_url(u) for u in urls if normalize_url(u)))
        for idx, url in enumerate(unique_urls, start=1):
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
            filename = f"{idx:02d}_{digest}{_guess_image_ext(url)}"
            artifact = Artifact("image", "pending", f"{rel_prefix}/{filename}", public_url(url))
            self.last_artifacts.append(artifact)
            if not self._enabled:
                artifact.status = "disabled"
                continue
            existing = sorted(p for p in dest_dir.glob(f"*_{digest}.*") if p.is_file() and p.suffix != ".tmp")
            if existing:
                artifact.path = f"{rel_prefix}/{existing[0].name}"
                artifact.status = "complete"
                mapping[url] = artifact.path
                continue
            try:
                data, ctype = self._client.download_binary(url, referer=self._referer)
                filename = f"{idx:02d}_{digest}{_guess_image_ext(url, ctype)}"
                artifact.path = f"{rel_prefix}/{filename}"
                atomic_write_bytes(dest_dir / filename, data)
            except (BilibiliRiskControlError, BilibiliAuthError):
                raise
            except Exception as exc:
                artifact.status = "failed"
                artifact.error = clean_error(exc)
                continue
            artifact.status = "complete"
            mapping[url] = artifact.path
        return mapping


def _img_ref(url: str, image_map: dict[str, str]) -> str:
    norm = normalize_url(url)
    return image_map.get(norm, norm)


# ---------------------------------------------------------------------------
# Profile rendering
# ---------------------------------------------------------------------------


def render_profile_markdown(
    *,
    uid: int,
    user_info: dict[str, Any],
    relation_stat: dict[str, Any] | None,
    upstat: dict[str, Any] | None,
    collections: list[dict[str, Any]],
    avatar_rel: str | None = None,
    banner_rel: str | None = None,
    fetched_at: str = "",
) -> str:
    name = str(user_info.get("name") or f"UID {uid}")
    sign = str(user_info.get("sign") or "").strip()
    level = user_info.get("level", "-")
    official = user_info.get("official") or {}
    official_title = str(official.get("title") or "").strip()
    follower = (relation_stat or {}).get("follower", "-")
    following = (relation_stat or {}).get("following", "-")
    archive_views = ((upstat or {}).get("archive") or {}).get("view", "-")
    likes = (upstat or {}).get("likes", "-")

    lines: list[str] = [f"# {name} (UID: {uid})", ""]
    if avatar_rel:
        lines.append(f"![头像]({avatar_rel})")
        lines.append("")
    if banner_rel:
        lines.append(f"![主页横幅]({banner_rel})")
        lines.append("")

    lines.extend(
        [
            "## 基本信息",
            "",
            f"- **空间主页**: https://space.bilibili.com/{uid}",
            f"- **UID**: `{uid}`",
            f"- **等级**: LV{level}",
            f"- **粉丝数**: {follower} | **关注数**: {following}",
            f"- **累计播放**: {archive_views} | **累计获赞**: {likes}",
        ]
    )
    if official_title:
        lines.append(f"- **认证**: {official_title}")
    if fetched_at:
        lines.append(f"- **快照时间**: {fetched_at}")
    lines.append("")

    if sign:
        lines.extend(["## 个人简介", "", sign, ""])

    if collections:
        lines.extend(["## 公开合集与系列", ""])
        for col in collections:
            meta = col.get("meta") if isinstance(col.get("meta"), dict) else col
            title = str(meta.get("name") or meta.get("title") or "未命名合集")
            total = meta.get("total", "-")
            desc = str(meta.get("description") or "").strip()
            cid = meta.get("season_id") or meta.get("series_id") or ""
            suffix = f" (`id={cid}`)" if cid else ""
            lines.append(f"- **{title}**{suffix} — 共 {total} 项")
            if desc:
                lines.append(f"  - {desc}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Dynamic rendering
# ---------------------------------------------------------------------------


def _render_rich_text_nodes(nodes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "")
        text = str(node.get("text") or node.get("orig_text") or "")
        if ntype == "RICH_TEXT_NODE_TYPE_WEB" and node.get("jump_url"):
            parts.append(f"[{text or node['jump_url']}]({normalize_url(str(node['jump_url']))})")
        elif ntype == "RICH_TEXT_NODE_TYPE_TOPIC" and node.get("jump_url"):
            parts.append(f"[{text}]({normalize_url(str(node['jump_url']))})")
        else:
            parts.append(text)
    return "".join(parts)


def extract_dynamic_image_urls(item: dict[str, Any]) -> list[str]:
    """Collect all image URLs referenced by a dynamic item (including forwarded `orig`)."""
    urls: list[str] = []
    modules = item.get("modules") or {}
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}

    opus = major.get("opus") or {}
    for pic in opus.get("pics") or []:
        if isinstance(pic, dict) and pic.get("url"):
            urls.append(normalize_url(str(pic["url"])))

    draw = major.get("draw") or {}
    for pic in draw.get("items") or []:
        if isinstance(pic, dict) and pic.get("src"):
            urls.append(normalize_url(str(pic["src"])))

    archive = major.get("archive") or {}
    if archive.get("cover"):
        urls.append(normalize_url(str(archive["cover"])))

    article = major.get("article") or {}
    for cover in article.get("covers") or []:
        if isinstance(cover, str) and cover:
            urls.append(normalize_url(cover))

    common = major.get("common") or {}
    if common.get("cover"):
        urls.append(normalize_url(str(common["cover"])))

    orig = item.get("orig")
    if isinstance(orig, dict):
        urls.extend(extract_dynamic_image_urls(orig))

    return [u for u in urls if u]


def _render_dynamic_body(item: dict[str, Any], image_map: dict[str, str]) -> list[str]:
    lines: list[str] = []
    modules = item.get("modules") or {}
    dyn = modules.get("module_dynamic") or {}

    topic = dyn.get("topic") or {}
    if topic.get("name"):
        lines.append(f"**话题**: #{topic['name']}#")
        lines.append("")

    desc = dyn.get("desc") or {}
    if isinstance(desc, dict):
        nodes = desc.get("rich_text_nodes")
        if isinstance(nodes, list) and nodes:
            desc_text = _render_rich_text_nodes(nodes).strip()
        else:
            desc_text = str(desc.get("text") or "").strip()
        if desc_text:
            lines.append(desc_text)
            lines.append("")

    major = dyn.get("major") or {}
    if isinstance(major, dict):
        opus = major.get("opus")
        if isinstance(opus, dict):
            opus_title = str(opus.get("title") or "").strip()
            if opus_title:
                lines.append(f"## {opus_title}")
                lines.append("")
            summary = opus.get("summary") or {}
            if isinstance(summary, dict):
                nodes = summary.get("rich_text_nodes")
                text = (
                    _render_rich_text_nodes(nodes).strip()
                    if isinstance(nodes, list) and nodes
                    else str(summary.get("text") or "").strip()
                )
                if text:
                    lines.append(text)
                    lines.append("")
            for idx, pic in enumerate(opus.get("pics") or [], start=1):
                if isinstance(pic, dict) and pic.get("url"):
                    lines.append(f"![图片 {idx}]({_img_ref(str(pic['url']), image_map)})")
            if opus.get("pics"):
                lines.append("")

        draw = major.get("draw")
        if isinstance(draw, dict):
            for idx, pic in enumerate(draw.get("items") or [], start=1):
                if isinstance(pic, dict) and pic.get("src"):
                    lines.append(f"![图片 {idx}]({_img_ref(str(pic['src']), image_map)})")
            if draw.get("items"):
                lines.append("")

        archive = major.get("archive")
        if isinstance(archive, dict):
            title = str(archive.get("title") or "投稿视频")
            bvid = str(archive.get("bvid") or "")
            jump = normalize_url(str(archive.get("jump_url") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")))
            lines.append(f"### 关联视频：[{title}]({jump})")
            if archive.get("desc"):
                lines.append("")
                lines.append(str(archive["desc"]).strip())
            if archive.get("cover"):
                lines.append("")
                lines.append(f"![视频封面]({_img_ref(str(archive['cover']), image_map)})")
            lines.append("")

        article = major.get("article")
        if isinstance(article, dict):
            title = str(article.get("title") or "专栏文章")
            jump = normalize_url(str(article.get("jump_url") or ""))
            lines.append(f"### 关联专栏：[{title}]({jump})")
            if article.get("desc"):
                lines.append("")
                lines.append(str(article["desc"]).strip())
            for idx, cover in enumerate(article.get("covers") or [], start=1):
                if isinstance(cover, str) and cover:
                    lines.append("")
                    lines.append(f"![专栏封面 {idx}]({_img_ref(cover, image_map)})")
            lines.append("")

        common = major.get("common")
        if isinstance(common, dict):
            title = str(common.get("title") or "卡片")
            jump = normalize_url(str(common.get("jump_url") or ""))
            lines.append(f"### 关联卡片：[{title}]({jump})")
            if common.get("desc"):
                lines.append("")
                lines.append(str(common["desc"]).strip())
            if common.get("cover"):
                lines.append("")
                lines.append(f"![卡片封面]({_img_ref(str(common['cover']), image_map)})")
            lines.append("")

        none_block = major.get("none")
        if isinstance(none_block, dict) and none_block.get("tips"):
            lines.append(f"*（{none_block['tips']}）*")
            lines.append("")

    return lines


def render_dynamic_markdown(item: dict[str, Any], image_map: dict[str, str] | None = None) -> str:
    """Render a Bilibili dynamic item (including forwarded `orig`) to Markdown."""
    img_map = image_map or {}
    dyn_id = str(item.get("id_str") or "")
    dyn_type = str(item.get("type") or "")
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    stat = modules.get("module_stat") or {}

    author_name = str(author.get("name") or "未知作者")
    pub_ts = author.get("pub_ts")
    pub_time = format_unix_ts(pub_ts) if pub_ts else str(author.get("pub_time") or "unknown")
    like_cnt = (stat.get("like") or {}).get("count", 0)
    comment_cnt = (stat.get("comment") or {}).get("count", 0)
    forward_cnt = (stat.get("forward") or {}).get("count", 0)

    lines: list[str] = [
        f"# 动态 {dyn_id}",
        "",
        f"- **作者**: {author_name}",
        f"- **发布时间**: {pub_time}",
        f"- **类型**: `{dyn_type}`",
        f"- **原链接**: https://t.bilibili.com/{dyn_id}",
        f"- **互动**: 点赞 {like_cnt} | 评论 {comment_cnt} | 转发 {forward_cnt}",
        "",
        "---",
        "",
    ]

    lines.extend(_render_dynamic_body(item, img_map))

    orig = item.get("orig")
    if isinstance(orig, dict):
        orig_id = str(orig.get("id_str") or "")
        orig_author = ((orig.get("modules") or {}).get("module_author") or {}).get("name") or "原动态"
        lines.append("## 转发原动态")
        lines.append("")
        if orig_id:
            lines.append(f"> **原作者**: {orig_author} | 原链接: https://t.bilibili.com/{orig_id}")
            lines.append(">")
        orig_body = _render_dynamic_body(orig, img_map)
        if not orig_body:
            lines.append("> *（原动态内容不可见或已删除）*")
        else:
            for b_line in orig_body:
                lines.append(f"> {b_line}" if b_line else ">")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Article rendering (Opus JSON + legacy cv HTML)
# ---------------------------------------------------------------------------


def _iter_opus_paragraphs(opus_detail: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(top_pics, paragraphs)` from either opus/detail or /x/article/view `opus` payloads."""
    top_pics: list[dict[str, Any]] = []
    paragraphs: list[dict[str, Any]] = []

    # Format A: /x/article/view -> opus.content.paragraphs
    direct_content = opus_detail.get("content")
    if isinstance(direct_content, dict) and isinstance(direct_content.get("paragraphs"), list):
        paragraphs.extend(p for p in direct_content["paragraphs"] if isinstance(p, dict))
        return top_pics, paragraphs

    # Format B: /x/polymer/web-dynamic/v1/opus/detail -> item.modules[]
    item = opus_detail.get("item") if isinstance(opus_detail.get("item"), dict) else opus_detail
    modules = item.get("modules") or []
    if not isinstance(modules, list):
        return top_pics, paragraphs

    for mod in modules:
        if not isinstance(mod, dict):
            continue
        mod_top = mod.get("module_top") or {}
        album = (mod_top.get("display") or {}).get("album") or {}
        for pic in album.get("pics") or []:
            if isinstance(pic, dict):
                top_pics.append(pic)
        mod_content = mod.get("module_content") or {}
        for para in mod_content.get("paragraphs") or []:
            if isinstance(para, dict):
                paragraphs.append(para)

    return top_pics, paragraphs


def extract_opus_image_urls(opus_detail: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    top_pics, paragraphs = _iter_opus_paragraphs(opus_detail)

    for pic in top_pics:
        if pic.get("url"):
            urls.append(normalize_url(str(pic["url"])))

    for para in paragraphs:
        pic_obj = para.get("pic") or {}
        for pic in pic_obj.get("pics") or []:
            if isinstance(pic, dict) and pic.get("url"):
                urls.append(normalize_url(str(pic["url"])))

    return [u for u in urls if u]


def _render_opus_text_nodes(nodes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "")
        nnum = node.get("node_type")
        if ntype == "TEXT_NODE_TYPE_WORD" or nnum == 1:
            word = node.get("word") or {}
            text = str(word.get("words") or "").replace("\r\n", "\n").replace("\r", "")
            style = word.get("style") or {}
            if text:
                if style.get("bold"):
                    text = f"**{text}**"
                if style.get("italic"):
                    text = f"*{text}*"
                if style.get("strikethrough"):
                    text = f"~~{text}~~"
                parts.append(text)
        elif ntype == "TEXT_NODE_TYPE_RICH" or nnum == 2:
            rich = node.get("rich") or {}
            text = str(rich.get("text") or rich.get("orig_text") or "")
            jump = normalize_url(str(rich.get("jump_url") or ""))
            if jump and text:
                parts.append(f"[{text}]({jump})")
            elif text:
                parts.append(text)
        elif ntype == "TEXT_NODE_TYPE_FORMULA" or nnum == 3:
            formula = node.get("formula") or {}
            latex = str(formula.get("latex_content") or "").strip()
            if latex:
                parts.append(f"${latex}$")
        elif ntype == "TEXT_NODE_TYPE_EMOJI" or nnum == 4:
            emoji = node.get("emoji") or {}
            parts.append(str(emoji.get("text") or ""))
    return "".join(parts)


def render_opus_body_markdown(opus_detail: dict[str, Any], image_map: dict[str, str] | None = None) -> str:
    img_map = image_map or {}
    top_pics, paragraphs = _iter_opus_paragraphs(opus_detail)
    lines: list[str] = []

    pic_idx = 0
    for pic in top_pics:
        if pic.get("url"):
            pic_idx += 1
            lines.append(f"![图 {pic_idx}]({_img_ref(str(pic['url']), img_map)})")
            lines.append("")

    for para in paragraphs:
        ptype = para.get("para_type")
        if ptype in (1, "PARA_TYPE_TEXT"):
            nodes = (para.get("text") or {}).get("nodes") or []
            text = _render_opus_text_nodes(nodes).strip()
            if text:
                lines.append(text)
                lines.append("")
        elif ptype in (2, "PARA_TYPE_PIC"):
            for pic in (para.get("pic") or {}).get("pics") or []:
                if isinstance(pic, dict) and pic.get("url"):
                    pic_idx += 1
                    lines.append(f"![图 {pic_idx}]({_img_ref(str(pic['url']), img_map)})")
            lines.append("")
        elif ptype in (3, "PARA_TYPE_LINE"):
            lines.extend(["---", ""])
        elif ptype in (4, "PARA_TYPE_QUOTE"):
            nodes = (para.get("text") or {}).get("nodes") or []
            text = _render_opus_text_nodes(nodes).strip()
            if text:
                for qline in text.splitlines():
                    lines.append(f"> {qline}")
                lines.append("")
        elif ptype in (5, "PARA_TYPE_LIST"):
            list_obj = para.get("list") or {}
            style = list_obj.get("style", 2)
            for lidx, litem in enumerate(list_obj.get("items") or [], start=1):
                nodes = litem.get("nodes") or [] if isinstance(litem, dict) else []
                text = _render_opus_text_nodes(nodes).strip()
                if text:
                    prefix = f"{lidx}. " if style == 1 else "- "
                    lines.append(f"{prefix}{text}")
            lines.append("")
        elif ptype in (6, "PARA_TYPE_LINK_CARD"):
            card = (para.get("link_card") or {}).get("card") or {}
            title = str(card.get("title") or "链接卡片")
            jump = normalize_url(str(card.get("jump_url") or ""))
            lines.append(f"- 链接卡片：[{title}]({jump})" if jump else f"- 链接卡片：{title}")
            lines.append("")
        elif ptype in (7, "PARA_TYPE_CODE"):
            code = para.get("code") or {}
            lang = str(code.get("lang") or "")
            content = str(code.get("content") or "")
            lines.extend([f"```{lang}", content, "```", ""])

    return "\n".join(lines).strip()


class _LegacyArticleHtmlParser(HTMLParser):
    """Extract clean Markdown and image URLs from Bilibili legacy cv article HTML."""

    def __init__(self, image_map: dict[str, str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._image_map = image_map or {}
        self._in_holder = False
        self._holder_depth = 0
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.image_urls: list[str] = []
        self._link_href: str | None = None
        self._in_blockquote = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        t = tag.lower()

        if not self._in_holder:
            if t == "div" and (
                attr_map.get("id") == "read-article-holder"
                or "article-holder" in attr_map.get("class", "")
            ):
                self._in_holder = True
                self._holder_depth = 1
            return

        if t in ("div", "section", "article"):
            self._holder_depth += 1

        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(t[1])
            self._chunks.append(f"\n\n{'#' * level} ")
        elif t == "p":
            self._chunks.append("\n\n> " if self._in_blockquote else "\n\n")
        elif t == "br":
            self._chunks.append("  \n")
        elif t == "hr":
            self._chunks.append("\n\n---\n\n")
        elif t == "blockquote":
            self._in_blockquote = True
            self._chunks.append("\n\n> ")
        elif t == "li":
            self._chunks.append("\n- ")
        elif t in ("strong", "b"):
            self._chunks.append("**")
        elif t in ("em", "i"):
            self._chunks.append("*")
        elif t == "code":
            self._chunks.append("`")
        elif t == "a":
            href = normalize_url(attr_map.get("href", ""))
            self._link_href = href or None
            if self._link_href:
                self._chunks.append("[")
        elif t == "img":
            raw_src = attr_map.get("data-src") or attr_map.get("src") or ""
            norm = normalize_url(raw_src)
            if norm:
                self.image_urls.append(norm)
                alt = attr_map.get("alt") or "图片"
                ref = self._image_map.get(norm, norm)
                self._chunks.append(f"\n\n![{alt}]({ref})\n\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_holder:
            return
        t = tag.lower()
        if t in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if t in ("div", "section", "article"):
            self._holder_depth -= 1
            if self._holder_depth <= 0:
                self._in_holder = False
            return
        if self._skip_depth > 0:
            return

        if t in ("strong", "b"):
            self._chunks.append("**")
        elif t in ("em", "i"):
            self._chunks.append("*")
        elif t == "code":
            self._chunks.append("`")
        elif t == "blockquote":
            self._in_blockquote = False
            self._chunks.append("\n")
        elif t == "a" and self._link_href:
            self._chunks.append(f"]({self._link_href})")
            self._link_href = None

    def handle_data(self, data: str) -> None:
        if not self._in_holder or self._skip_depth > 0:
            return
        text = html.unescape(data)
        if text:
            self._chunks.append(text)

    def get_markdown(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def extract_legacy_html_images(html_text: str) -> list[str]:
    text = html_text or ""
    if "read-article-holder" not in text and "article-holder" not in text and "<html" not in text.lower():
        text = f'<div id="read-article-holder">{text}</div>'
    parser = _LegacyArticleHtmlParser()
    parser.feed(text)
    return parser.image_urls


def render_legacy_html_markdown(html_text: str, image_map: dict[str, str] | None = None) -> str:
    text = html_text or ""
    if "read-article-holder" not in text and "article-holder" not in text and "<html" not in text.lower():
        text = f'<div id="read-article-holder">{text}</div>'
    parser = _LegacyArticleHtmlParser(image_map=image_map)
    parser.feed(text)
    return parser.get_markdown()


def render_article_markdown(
    *,
    meta: dict[str, Any],
    opus_detail: dict[str, Any] | None = None,
    html_text: str | None = None,
    image_map: dict[str, str] | None = None,
) -> str:
    """Render a Bilibili article (Opus or legacy cv HTML) to Markdown."""
    img_map = image_map or {}
    cv_id = meta.get("id") or ""
    title = str(meta.get("title") or f"专栏 cv{cv_id}")
    author = (meta.get("author") or {}).get("name") or "未知作者"
    pub_time = format_unix_ts(meta.get("publish_time"))
    summary = str(meta.get("summary") or "").strip()
    stats = meta.get("stats") or {}
    views = stats.get("view", "-")
    likes = stats.get("like", "-")
    replies = stats.get("reply", "-")
    category = (meta.get("category") or {}).get("name") or ""
    url = f"https://www.bilibili.com/read/cv{cv_id}" if cv_id else ""

    lines: list[str] = [
        f"# {title}",
        "",
        f"- **作者**: {author}",
        f"- **发布时间**: {pub_time}",
        f"- **专栏 ID**: `cv{cv_id}`",
    ]
    if category:
        lines.append(f"- **分类**: {category}")
    if url:
        lines.append(f"- **原链接**: {url}")
    lines.append(f"- **统计**: 阅读 {views} | 点赞 {likes} | 评论 {replies}")
    lines.extend(["", "---", ""])

    body = ""
    if opus_detail:
        body = render_opus_body_markdown(opus_detail, img_map)
    if not body and html_text:
        body = render_legacy_html_markdown(html_text, img_map)
    if not body and summary:
        body = summary

    if body:
        lines.append(body)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Favorites清单 rendering (metadata list ONLY, no media download)
# ---------------------------------------------------------------------------


def render_favorites_index_markdown(uid: int, folders: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        f"# 公开收藏夹总览 (UID: {uid})",
        "",
        f"共发现 **{len(folders)}** 个公开收藏夹（仅保存视频清单，不下载他人视频）。",
        "",
        "| 收藏夹名称 | Media ID | 条目数 | 清单文件 |",
        "| :--- | :---: | :---: | :--- |",
    ]
    for f in folders:
        title = str(f.get("title") or "未命名收藏夹").replace("|", "\\|")
        mid = f.get("id") or ""
        count = f.get("media_count", 0)
        fname = str(f.get("_md_filename") or "")
        link = f"[{fname}]({fname})" if fname else "-"
        lines.append(f"| {title} | `{mid}` | {count} | {link} |")
    lines.append("")
    return "\n".join(lines)


def render_favorite_folder_markdown(folder: dict[str, Any], medias: list[dict[str, Any]]) -> str:
    title = str(folder.get("title") or "未命名收藏夹")
    media_id = folder.get("id") or ""
    lines: list[str] = [
        f"# 收藏夹：{title}",
        "",
        f"- **Media ID**: `{media_id}`",
        f"- **收录条目数**: {len(medias)}",
        "",
        "| # | BV / ID | 标题 | 原作者 | 时长(秒) | 收藏时间 | 链接 |",
        "| :---: | :--- | :--- | :--- | :---: | :--- | :--- |",
    ]
    for idx, item in enumerate(medias, start=1):
        bvid = str(item.get("bvid") or item.get("id") or "-")
        item_title = str(item.get("title") or "已失效视频").replace("|", "\\|")
        upper = ((item.get("upper") or {}).get("name") or "-").replace("|", "\\|")
        duration = item.get("duration", "-")
        fav_time = format_unix_date(item.get("fav_time"))
        url = f"https://www.bilibili.com/video/{bvid}" if bvid.startswith("BV") else "-"
        link = f"[链接]({url})" if url != "-" else "-"
        lines.append(f"| {idx} | `{bvid}` | {item_title} | {upper} | {duration} | {fav_time} | {link} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Comments rendering (Pinned + UP Replies + Top N)
# ---------------------------------------------------------------------------


def _format_comment_block(reply: dict[str, Any], up_uid: int | None) -> list[str]:
    member = reply.get("member") or {}
    uname = str(member.get("uname") or "未知用户")
    mid = member.get("mid")
    is_up = up_uid is not None and str(mid) == str(up_uid)
    badge = " **[UP主]**" if is_up else ""
    ctime = format_unix_ts(reply.get("ctime"))
    likes = reply.get("like", 0)
    content = str((reply.get("content") or {}).get("message") or "").strip()
    rpid = reply.get("rpid_str") or reply.get("rpid") or ""

    lines: list[str] = [
        f"### {uname}{badge}  `{ctime}` · 👍 {likes} · `rpid:{rpid}`",
        "",
        content or "*（空内容）*",
        "",
    ]

    sub_replies = reply.get("replies") or []
    if isinstance(sub_replies, list) and sub_replies:
        for sub in sub_replies:
            if not isinstance(sub, dict):
                continue
            sm = sub.get("member") or {}
            suname = str(sm.get("uname") or "未知用户")
            smid = sm.get("mid")
            s_is_up = up_uid is not None and str(smid) == str(up_uid)
            s_badge = " **[UP主]**" if s_is_up else ""
            s_time = format_unix_ts(sub.get("ctime"))
            s_likes = sub.get("like", 0)
            s_msg = str((sub.get("content") or {}).get("message") or "").strip().replace("\n", " ")
            lines.append(f"- **{suname}**{s_badge} (`{s_time}` · 👍 {s_likes}): {s_msg}")
        lines.append("")

    return lines


def render_comments_markdown(
    *,
    title: str,
    oid: str | int,
    rtype: int,
    bundle: dict[str, Any],
    up_uid: int | None = None,
) -> str:
    """Render structured comment archive bundle (`pinned`, `up_replied`, `top`) to Markdown."""
    pinned: list[dict[str, Any]] = list(bundle.get("pinned") or [])
    up_replied: list[dict[str, Any]] = list(bundle.get("up_replied") or [])
    top: list[dict[str, Any]] = list(bundle.get("top") or [])
    fetched_at = str(bundle.get("fetched_at") or "")
    total_count = bundle.get("total_count", "-")

    lines: list[str] = [
        f"# 精选评论归档：{title}",
        "",
        f"- **载体**: `oid={oid}` / `type={rtype}`",
        f"- **评论总数（快照）**: {total_count}",
        f"- **抓取策略**: 置顶 ({len(pinned)}) + UP 主回复相关 ({len(up_replied)}) + 热门 ({len(top)})",
    ]
    if fetched_at:
        lines.append(f"- **抓取时间**: {fetched_at}")
    lines.extend(["", "---", ""])

    if pinned:
        lines.extend(["## 置顶评论", ""])
        for r in pinned:
            lines.extend(_format_comment_block(r, up_uid))

    if up_replied:
        lines.extend(["## UP 主参与回复", ""])
        for r in up_replied:
            lines.extend(_format_comment_block(r, up_uid))

    lines.extend([f"## 热门评论（{len(top)} 条）", ""])
    if not top:
        lines.append("*（暂无热门评论）*")
        lines.append("")
    else:
        for r in top:
            lines.extend(_format_comment_block(r, up_uid))

    return "\n".join(lines).rstrip() + "\n"
