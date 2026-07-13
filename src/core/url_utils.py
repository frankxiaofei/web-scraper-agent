"""公告 URL 分类：区分详情页与列表/导航页。"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urljoin

_CHDTP_TOGETCONTENT_RE = re.compile(
    r"^javascript:toGetContent\(['\"]([^'\"]+)['\"]\)\s*$",
    re.I,
)

# 列表/导航/栏目首页路径
_LIST_PATH_RE = re.compile(
    r"(?:"
    r"/index(?:_\d+)?\.(?:shtml|html|htm|jsp|aspx|php)(?:/|$)"
    r"|/(?:index|list)(?:/|$|\?)"
    r"|/jyxx/?$"
    r"|/jyxx/index\."
    r")",
    re.I,
)

# 详情页特征：路径段或查询参数
_DETAIL_MARKER_RE = re.compile(
    r"(?:"
    r"/(?:detail|article|notice)(?:/|[?])"
    r"|[?&](?:id|articleid|noticeid|infoid)="
    r"|/t\d{6,8}_\d+\."
    r"|/\d{4}/\d{2}/\d+\."
    r"|_\d{5,}\."
    r")",
    re.I,
)

# 中国政府采购网等：日期+序号文件名
_CCGP_ARTICLE_RE = re.compile(r"t\d{6,8}_\d+\.(?:htm|shtml|html)$", re.I)

# dlzb 统一招标平台详情页
_DLZB_DETAIL_RE = re.compile(r"/d-zb-\d+\.html", re.I)

# 栏目首页（无正文，仅导航）
_CATEGORY_INDEX_RE = re.compile(
    r"/(?:xxgg|zcfg|gpsr|jdjc|wtogpa|cggg|news)/index\.", re.I
)


def is_browsable_http_url(url: str) -> bool:
    """是否为可在浏览器中直接打开的 http(s) 链接。"""
    if not url or not str(url).strip():
        return False
    return str(url).strip().lower().startswith(("http://", "https://"))


def resolve_notice_detail_url(url: str, *, base_url: str = "") -> str:
    """将 javascript:onclick 占位链接解析为可访问的 HTTP URL。"""
    if not url or not str(url).strip():
        return ""

    raw = str(url).strip()
    match = _CHDTP_TOGETCONTENT_RE.match(raw)
    if match:
        return f"https://www.chdtp.com/staticPage/{match.group(1)}"

    if is_browsable_http_url(raw):
        return raw

    if raw.startswith("/") and base_url:
        return urljoin(base_url.rstrip("/") + "/", raw.lstrip("/"))

    return raw


def is_likely_list_url(url: str) -> bool:
    """判断 URL 是否像列表页、栏目首页或导航链接。"""
    if not url or not url.strip():
        return True
    path = urlparse(url.lower()).path
    if _LIST_PATH_RE.search(path):
        return True
    if _CATEGORY_INDEX_RE.search(path):
        return True
    if "/jyxx/" in path and not _DETAIL_MARKER_RE.search(url):
        return True
    return False


def is_likely_detail_url(url: str) -> bool:
    """判断 URL 是否像公告详情页。"""
    return url_backfill_skip_reason(url) is None


def url_backfill_skip_reason(url: str) -> str | None:
    """返回补抓跳过原因；None 表示可尝试补抓。"""
    if not url or not str(url).strip():
        return "empty_url"

    resolved = resolve_notice_detail_url(url)
    if resolved != url and is_browsable_http_url(resolved):
        url = resolved

    lower = str(url).lower()
    if lower.startswith("javascript:"):
        return "javascript_url"

    path = urlparse(lower).path

    if _LIST_PATH_RE.search(path):
        return "list_or_index_path"
    if _CATEGORY_INDEX_RE.search(path):
        return "category_index"
    if "/jyxx/" in path and not _DETAIL_MARKER_RE.search(url):
        return "jyxx_category"
    if _DETAIL_MARKER_RE.search(url):
        return None
    if _CCGP_ARTICLE_RE.search(path):
        return None
    if _DLZB_DETAIL_RE.search(path):
        return None
    return "not_likely_detail"
