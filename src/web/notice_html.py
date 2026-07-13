"""Prepare scraped notice HTML for safe display on the detail page."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.core.wechat_crawl import rewrite_wechat_content_images

_UNSAFE_BLOCK_TAGS = (
    "script",
    "iframe",
    "object",
    "embed",
    "form",
    "frame",
    "frameset",
    "base",
)
_UNSAFE_BLOCK_RE = re.compile(
    rf"(?is)<({'|'.join(_UNSAFE_BLOCK_TAGS)})[^>]*>.*?</\1>"
)
_STYLE_TAG_RE = re.compile(r"(?is)<style[^>]*>.*?</style>")
_VOID_UNSAFE_RE = re.compile(
    r"(?is)<(?:meta|link|base)[^>]*>|"
    rf"<(?:{'|'.join(_UNSAFE_BLOCK_TAGS)})[^>]*/>"
)
_EVENT_ATTR_RE = re.compile(
    r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_DOCTYPE_RE = re.compile(r"(?is)<!DOCTYPE[^>]*>")
_HEAD_RE = re.compile(r"(?is)<head[^>]*>.*?</head>")
_BODY_RE = re.compile(r"(?is)<body[^>]*>(.*)</body>")
_WRAPPER_TAG_RE = re.compile(r"(?is)</?(?:html|head|body)[^>]*>")


def _extract_body_fragment(html: str) -> str:
    """Unwrap full HTML documents; keep fragment content for inline embedding."""
    text = _DOCTYPE_RE.sub("", html).strip()
    match = _BODY_RE.search(text)
    if match:
        return match.group(1).strip()
    text = _HEAD_RE.sub("", text)
    return _WRAPPER_TAG_RE.sub("", text).strip()


def _strip_unsafe_markup(html: str) -> str:
    cleaned = html
    for _ in range(3):
        next_cleaned = _UNSAFE_BLOCK_RE.sub("", cleaned)
        if next_cleaned == cleaned:
            break
        cleaned = next_cleaned
    cleaned = _STYLE_TAG_RE.sub("", cleaned)
    cleaned = _VOID_UNSAFE_RE.sub("", cleaned)
    return _EVENT_ATTR_RE.sub("", cleaned)


_VISIBILITY_HIDDEN_RE = re.compile(
    r"visibility\s*:\s*hidden\s*;?",
    re.IGNORECASE,
)
_OPACITY_ZERO_RE = re.compile(
    r"opacity\s*:\s*0\s*;?",
    re.IGNORECASE,
)


def _fix_wechat_root_visibility(html: str) -> str:
    """微信正文根节点常被设为 hidden，展示前强制可见。"""
    if not html or "js_content" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for root in soup.select("#js_content, .rich_media_content"):
        style = root.get("style") or ""
        next_style = _VISIBILITY_HIDDEN_RE.sub("", style)
        next_style = _OPACITY_ZERO_RE.sub("", next_style)
        next_style = next_style.strip().rstrip(";")
        if next_style != style.strip().rstrip(";"):
            changed = True
            if next_style:
                root["style"] = next_style
            elif root.has_attr("style"):
                del root["style"]
    return str(soup) if changed else html


def prepare_notice_html_for_display(
    html: str | None,
    *,
    attachments: list[dict] | None = None,
    is_wechat: bool = False,
) -> str:
    """Light sanitize + unwrap document shell for detail page rendering."""
    if not html or not str(html).strip():
        return ""
    prepared = _extract_body_fragment(str(html).strip())
    prepared = _strip_unsafe_markup(prepared).strip()
    if is_wechat or 'id="js_content"' in prepared or "rich_media_content" in prepared:
        prepared = _fix_wechat_root_visibility(prepared)
        if attachments:
            prepared = rewrite_wechat_content_images(prepared, attachments)
    return prepared
