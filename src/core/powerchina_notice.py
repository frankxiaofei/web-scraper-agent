"""bid.powerchina.cn 公告 ID 解析、详情 URL 构建与标题匹配。"""

from __future__ import annotations

import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from src.core.crawl_rules import load_crawl_rule
from src.core.publish_date_utils import parse_publish_datetime
from src.core.http_api_client import fetch_json_url
from src.core.site_sync import get_site_by_id

logger = logging.getLogger(__name__)

POWERCHINA_SITE_ID = "中国电力建设集团有限公司_公共资源交易服务平台"
ALL_LIST_URL = (
    "https://bid.powerchina.cn/newcbs/recpro-newmember/BidAnnouncementSummary/allList"
)
API_BASE_URL = "https://bid.powerchina.cn/newcbs/recpro-newmember"
DEFAULT_DETAIL_URL_TEMPLATE = (
    "https://bid.powerchina.cn/notice/detail?id={id}"
    "&type=招采公告&typeName=招采公告&index=0-3&path=/consult/notice"
    "&companyType=3&bidType=1"
)
DEFAULT_INDEX_CACHE = Path("data/powerchina_bim_title_index.json")
DEFAULT_BIM_CACHE = Path("data/generated_scripts/powerchina_bim_all.json")
LIST_URL = (
    "https://bid.powerchina.cn/newcbs/recpro-newmember/BidAnnouncementSummary/list"
)
POWERCHINA_PDF_DOWNLOAD_BASE = (
    "https://bid-zb.powerchina.cn/bidprocurement/common-tools/tools/commonUpload/"
    "bulletinDownLoadFile"
)
DEFAULT_PDF_FILE_TYPE = "4"
_MIN_MEANINGFUL_TEXT_LEN = 30

_SUFFIX_RE = re.compile(
    r"(公开招标|公开询比|询比采购|竞争性谈判|成交结果公示|中标结果公示|"
    r"中标候选人公示|采购项目|招标公告|采购公告|公示|公告)$"
)
_NOTICE_SUFFIX_RE = re.compile(
    r"(变更公告(?:\([一二三四五六七八九十\d]+\))?|终止公告|成交结果公示|"
    r"中标结果公示|中标候选人公示|公开(?:询比|谈判|竞价)采购公告|"
    r"采购公示|实施前公示|招标公告|采购公告|公示)$"
)
_PROJECT_CODE_RE = re.compile(
    r"KY\d{4}[-_][A-Za-z]{2}[-_][A-Za-z0-9]+|"
    r"XARD\d{4}-\d{4}(?:-\d+)?|"
    r"PC-\d{4,}-\d+",
    re.I,
)
_BRACKET_CONTENT_RE = re.compile(r"[（(][^）)]*[）)]")


def _collapse_repeated_phrases(text: str, *, min_len: int = 10) -> str:
    """去除标题中任意位置相邻重复的子串（allList 常重复项目名）。"""
    if len(text) < min_len * 2:
        return text
    changed = True
    while changed and len(text) >= min_len * 2:
        changed = False
        for start in range(len(text) - min_len * 2 + 1):
            max_len = (len(text) - start) // 2
            for length in range(max_len, min_len - 1, -1):
                chunk = text[start : start + length]
                if text[start + length : start + 2 * length] == chunk:
                    text = text[: start + length] + text[start + 2 * length :]
                    changed = True
                    break
            if changed:
                break
    return text


def normalize_title(title: str) -> str:
    """标题规范化：去空格/括号、合并重复词、去项目编号前缀，便于索引匹配。"""
    text = (title or "").strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[（()）\[\]【】]", "", text)
    text = re.sub(r"采购采购+", "采购", text)
    text = re.sub(r"项目项目+", "项目", text)
    text = re.sub(_PROJECT_CODE_RE, "", text)
    text = re.sub(r"220KV", "220kV", text, flags=re.I)
    text = re.sub(r"BIM\s*\+", "BIM+", text, flags=re.I)
    text = _collapse_repeated_phrases(text)
    return text.strip()


def _extract_notice_suffix(title: str) -> str:
    normalized = normalize_title(title)
    match = _NOTICE_SUFFIX_RE.search(normalized)
    return match.group(1) if match else ""


def _significant_tokens(title: str) -> list[str]:
    text = normalize_title(title)
    tokens: list[str] = []
    for token in re.split(r"[\s\-—_#、，,.+]+", text):
        token = token.strip()
        if len(token) >= 6 or "BIM" in token.upper():
            tokens.append(token)
    for match in re.finditer(
        r"[\u4e00-\u9fff\d]{4,24}(?:片区|地块|工程|项目|电站|线路|通道|变电站|隧洞|酒店|标段|局|院|公司)",
        text,
    ):
        tokens.append(match.group(0))
    if "BIM" in text.upper():
        idx = text.upper().index("BIM")
        tokens.append(text[max(0, idx - 24) : min(len(text), idx + 36)])
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _longest_common_substring_size(left: str, right: str, *, min_size: int = 14) -> int:
    match = SequenceMatcher(None, left, right).find_longest_match(0, len(left), 0, len(right))
    return match.size if match.size >= min_size else 0


def _query_matches_candidate(query: str, candidate: str) -> bool:
    """判断候选标题是否可能对应查询标题（用于过滤 fuzzy 误匹配）。"""
    q_norm = normalize_title(query)
    c_norm = normalize_title(candidate)
    if not q_norm or not c_norm:
        return False
    if q_norm == c_norm or q_norm in c_norm or c_norm in q_norm:
        return True

    q_suffix = _extract_notice_suffix(q_norm)
    c_suffix = _extract_notice_suffix(c_norm)
    if q_suffix and c_suffix and q_suffix != c_suffix:
        return False

    if _longest_common_substring_size(q_norm, c_norm, min_size=14) >= 14:
        return True

    q_tokens = set(_significant_tokens(q_norm))
    c_tokens = set(_significant_tokens(c_norm))
    if q_tokens:
        overlap_ratio = len(q_tokens & c_tokens) / len(q_tokens)
        if overlap_ratio >= 0.55:
            return True

    if _title_match_score(query, candidate) >= 0.38:
        long_q = {t for t in q_tokens if len(t) >= 10}
        if not long_q or (long_q & c_tokens):
            return True
    return False


def _title_match_score(left: str, right: str) -> float:
    """综合相似度：整体序列 + 关键词重叠 + 公告类型后缀，避免仅凭 BIM 尾缀误匹配。"""
    l_norm = normalize_title(left)
    r_norm = normalize_title(right)
    if not l_norm or not r_norm:
        return 0.0
    if l_norm == r_norm:
        return 1.0

    base = SequenceMatcher(None, l_norm, r_norm).ratio()
    if len(l_norm) >= 16 and (l_norm in r_norm or r_norm in l_norm):
        return max(base, 0.95)

    l_tokens = set(_significant_tokens(l_norm))
    r_tokens = set(_significant_tokens(r_norm))
    overlap_ratio = len(l_tokens & r_tokens) / len(l_tokens) if l_tokens else 0.0

    l_distinct = {t for t in l_tokens if len(t) >= 8 or "BIM" in t.upper()}
    distinct_overlap = (
        len(l_distinct & r_tokens) / len(l_distinct) if l_distinct else overlap_ratio
    )

    score = base * 0.45 + overlap_ratio * 0.4 + distinct_overlap * 0.15

    l_suffix = _extract_notice_suffix(l_norm)
    r_suffix = _extract_notice_suffix(r_norm)
    if l_suffix and r_suffix:
        if l_suffix == r_suffix:
            score = min(1.0, score + 0.05)
        else:
            score *= 0.35

    return score


def is_search_placeholder_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return "/search" in lower and "q=" in lower


def is_valid_detail_url(url: str) -> bool:
    """真实详情页须含 notice/detail?id= 数字 ID，拒绝 /search?q= 占位 URL。"""
    if not url or is_search_placeholder_url(url):
        return False
    return extract_notice_id(url) is not None


def extract_row_notice_id(row: dict[str, Any]) -> str | None:
    """从 allList/list 行解析公告 ID（id / noticeId / rowid）。"""
    for key in ("id", "noticeId", "rowid", "notice_id"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def extract_notice_id(url: str, *, id_param: str = "id") -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get(id_param, [])
    if values and str(values[0]).strip():
        return str(values[0]).strip()
    return None


def load_detail_url_template(site_id: str = POWERCHINA_SITE_ID) -> str:
    site = get_site_by_id(site_id) or {}
    rule = load_crawl_rule(site)
    if rule and rule.list_page and rule.list_page.api and rule.list_page.api.url_template:
        return rule.list_page.api.url_template
    return DEFAULT_DETAIL_URL_TEMPLATE


def build_detail_url(notice_id: str, *, url_template: str | None = None) -> str:
    template = url_template or load_detail_url_template()
    return template.format(id=notice_id)


def titles_match(expected: str, actual: str, *, threshold: float = 0.88) -> bool:
    if not expected or not actual:
        return False
    return _title_match_score(expected, actual) >= threshold


def title_search_keywords(title: str) -> list[str]:
    """从标题提取可用于内存匹配的短关键词。"""
    text = (title or "").strip()
    text = _SUFFIX_RE.sub("", text)
    text = re.sub(r"^中国电建[^，,]{0,20}(公司|院|局|分公司)?", "", text)
    keywords: list[str] = []
    for token in re.split(r"[\s\-—_（）()#]+", text):
        token = token.strip()
        if len(token) >= 6:
            keywords.append(token)
    if "BIM" in title.upper():
        for token in keywords:
            if "BIM" in token.upper() and token not in keywords[:1]:
                keywords.insert(0, token)
    deduped: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(kw)
    return deduped[:5]


def _html_to_text(html: str) -> str:
    from html import unescape

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _get_nested_field(data: Any, spec: str) -> Any:
    for key in spec.split("|"):
        if isinstance(data, dict):
            val = data.get(key.strip())
            if val not in (None, ""):
                return val
    return None


def _api_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://bid.powerchina.cn/search",
        "Origin": "https://bid.powerchina.cn",
    }


async def _post_json_api(
    url: str,
    payload: dict[str, Any],
    *,
    page_label: str,
    max_retries: int = 3,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    import httpx

    from src.core.http_api_client import create_http_client

    body_payload = {**payload, "time": int(time.time() * 1000)}
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with create_http_client() as client:
                client.timeout = httpx.Timeout(timeout_seconds, connect=20.0)
                response = await client.post(
                    url,
                    content=json.dumps(body_payload, ensure_ascii=False),
                    headers=_api_headers(),
                )
                response.raise_for_status()
                body = response.json()
            return body if isinstance(body, dict) else {}
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "%s 失败 (attempt %d/%d): %s",
                page_label,
                attempt + 1,
                max_retries,
                exc,
            )
            await _sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return {}


def _rows_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("rows", "list", "records"):
        rows = body.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


async def fetch_keyword_list_page(
    keyword: str,
    page_num: int,
    *,
    page_size: int = 20,
    max_retries: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """allList 关键词搜索（keyWords + pageNum，非 curpage/search 全量参数）。"""
    body = await _post_json_api(
        ALL_LIST_URL,
        {
            "keyWords": keyword,
            "pageNum": page_num,
            "pageSize": page_size,
            "publishStartTime": "",
            "publishEndTime": "",
        },
        page_label=f"allList keyWords={keyword!r} page={page_num}",
        max_retries=max_retries,
        timeout_seconds=30.0,
    )
    total = int(body.get("total") or 0)
    return _rows_from_body(body), total


async def fetch_all_list_page(
    curpage: int,
    *,
    page_size: int = 20,
    company_type: str = "3",
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """全量 allList（curpage），仅用于无关键词时的分页扫描。"""
    body = await _post_json_api(
        ALL_LIST_URL,
        {
            "curpage": curpage,
            "pageSize": page_size,
            "companyType": company_type,
        },
        page_label=f"allList curpage={curpage}",
        max_retries=max_retries,
        timeout_seconds=120.0,
    )
    return _rows_from_body(body)


async def fetch_consult_list_page(
    page_num: int,
    *,
    page_size: int = 20,
    announcement_type: str = "招采公告",
    company_type: str = "3",
    bid_type: str = "1",
    max_retries: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """consult/notice 列表 API（pageNum，较快，用于日常增量同步）。"""
    body = await _post_json_api(
        LIST_URL,
        {
            "pageNum": page_num,
            "pageSize": page_size,
            "announcementType": announcement_type,
            "companyType": company_type,
            "bidType": bid_type,
        },
        page_label=f"list page={page_num}",
        max_retries=max_retries,
        timeout_seconds=30.0,
    )
    total = int(body.get("total") or 0)
    return _rows_from_body(body), total


def load_bim_cache_index(cache_path: Path | None = DEFAULT_BIM_CACHE) -> dict[str, str]:
    """从 powerchina_bim_all.json 加载 normalized_title -> id。"""
    path = cache_path or DEFAULT_BIM_CACHE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        logger.debug("忽略损坏的 BIM 缓存: %s", path)
        return {}
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return {}
    index: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        notice_id = extract_row_notice_id(item)
        if title and notice_id:
            index[normalize_title(title)] = notice_id
    return index


def collect_search_keywords(target_titles: set[str]) -> list[str]:
    """从待匹配标题提取 allList keyWords 候选（优先 BIM）。"""
    keywords: list[str] = []
    seen: set[str] = set()
    if any("BIM" in (t or "").upper() for t in target_titles):
        keywords.append("BIM")
        seen.add("bim")
    for title in target_titles:
        for kw in title_search_keywords(title):
            key = kw.lower()
            if key not in seen and len(kw) >= 4:
                seen.add(key)
                keywords.append(kw)
    return keywords[:8]


async def fetch_get_info(notice_id: str) -> dict[str, Any] | None:
    api_url = (
        f"{API_BASE_URL.rstrip('/')}/BidAnnouncementSummary/getInfo/{notice_id}"
        f"?time={int(time.time() * 1000)}"
    )
    try:
        body = await fetch_json_url(
            api_url,
            site_url="https://bid.powerchina.cn",
            entry_url="https://bid.powerchina.cn/consult/notice",
        )
    except Exception as exc:
        logger.warning("powerchina getInfo 失败 id=%s: %s", notice_id, exc)
        return None
    data = body.get("data") if isinstance(body, dict) else body
    return data if isinstance(data, dict) else None


def parse_get_info_content(data: dict[str, Any]) -> tuple[str, str, str]:
    title = str(_get_nested_field(data, "title") or "").strip()
    content_html = str(
        _get_nested_field(data, "announcementContent|content|contentHtml|content_html") or ""
    )
    content_text = _html_to_text(content_html) if content_html else ""
    return title, content_text, content_html


def extract_publish_time_from_row(row: dict[str, Any]) -> datetime | None:
    """从 allList/list 行解析 publishTime / publishDate。"""
    return parse_publish_datetime(
        _get_nested_field(row, "publishTime|publishDate|publish_time|publish_date")
    )


def extract_publish_time_from_get_info(data: dict[str, Any]) -> datetime | None:
    """从 getInfo 响应解析 publishTime。"""
    return parse_publish_datetime(
        _get_nested_field(data, "publishTime|publishDate|publish_time|publish_date")
    )


async def build_id_publish_index(
    *,
    keywords: list[str] | None = None,
    max_pages: int = 20,
    page_size: int = 100,
    rate_limit_seconds: float = 0.2,
) -> dict[str, datetime]:
    """构建 notice_id -> publishTime 索引（allList keyWords 扫描）。"""
    index: dict[str, datetime] = {}
    for keyword in keywords or ["BIM"]:
        page_num = 1
        while page_num <= max(1, max_pages):
            try:
                rows, total = await fetch_keyword_list_page(
                    keyword, page_num, page_size=page_size
                )
            except Exception as exc:
                logger.warning("build_id_publish_index keyWords=%r page=%d: %s", keyword, page_num, exc)
                break
            if not rows:
                break
            for row in rows:
                notice_id = extract_row_notice_id(row)
                pub = extract_publish_time_from_row(row)
                if notice_id and pub:
                    index[notice_id] = pub
            if page_num * page_size >= total:
                break
            page_num += 1
            if rate_limit_seconds > 0:
                await _sleep(rate_limit_seconds)
    return index


def _meaningful_text(text: str) -> bool:
    return len((text or "").strip()) >= _MIN_MEANINGFUL_TEXT_LEN


def _extract_biz_id_from_url(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("bizId", [])
    if values and str(values[0]).strip():
        return str(values[0]).strip()
    match = re.search(r"bizId(?:%3D|=)([a-f0-9]+)", url, re.I)
    if match:
        return match.group(1)
    return None


def _extract_biz_id(data: dict[str, Any], content_html: str = "") -> str | None:
    for key in ("systemId", "bizId", "fileId", "pdfId"):
        val = _get_nested_field(data, key)
        if val and str(val).strip() and not str(val).startswith("http"):
            return str(val).strip()

    for key in ("pictureUrl", "pdfId", "fileUrl", "publicUrl"):
        url = str(_get_nested_field(data, key) or "").strip()
        biz_id = _extract_biz_id_from_url(url)
        if biz_id:
            return biz_id

    if content_html:
        biz_id = _extract_biz_id_from_url(content_html)
        if biz_id:
            return biz_id
    return None


def _extract_file_type(data: dict[str, Any], picture_url: str | None = None) -> str:
    for candidate in (picture_url, str(_get_nested_field(data, "pictureUrl|pdfId") or "")):
        if not candidate:
            continue
        values = parse_qs(urlparse(candidate).query).get("fileType", [])
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return DEFAULT_PDF_FILE_TYPE


def build_pdf_download_url(biz_id: str, *, file_type: str = DEFAULT_PDF_FILE_TYPE) -> str:
    query = urlencode({"bizId": biz_id, "fileType": file_type})
    return f"{POWERCHINA_PDF_DOWNLOAD_BASE}?{query}"


def resolve_picture_url(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """从 getInfo 响应解析 PDF 下载 URL 与 systemId/bizId。"""
    for key in ("pictureUrl", "pdfId", "fileUrl"):
        url = str(_get_nested_field(data, key) or "").strip()
        if url.startswith("http") and "bulletinDownLoadFile" in url:
            return url, _extract_biz_id_from_url(url) or _extract_biz_id(data)

    biz_id = _extract_biz_id(data)
    if biz_id:
        file_type = _extract_file_type(data)
        return build_pdf_download_url(biz_id, file_type=file_type), biz_id
    return None, biz_id


async def fetch_notice_content(
    *,
    notice_id: str | None = None,
    detail_url: str | None = None,
    pdf_url: str | None = None,
    expected_title: str | None = None,
    verify_title: bool = True,
) -> dict[str, Any]:
    """拉取电建公告正文：优先 HTML，空则下载 pictureUrl PDF 并提取文本。"""
    resolved_id = (notice_id or "").strip() or None
    if not resolved_id and detail_url:
        resolved_id = extract_notice_id(detail_url)

    if pdf_url and not resolved_id:
        pdf_result = await _fetch_pdf_content(pdf_url)
        return {
            "ok": pdf_result.get("ok", False),
            "notice_id": None,
            "detail_url": detail_url,
            "title": "",
            "content_source": "pdf" if pdf_result.get("ok") else "empty",
            "content_text": pdf_result.get("content_text") or "",
            "content_html": "",
            "picture_url": pdf_url,
            "system_id": _extract_biz_id_from_url(pdf_url),
            "error": pdf_result.get("error"),
        }

    if not resolved_id:
        return {"ok": False, "error": "notice_id、detail_url 或 pdf_url 至少提供一个"}

    detail = build_detail_url(resolved_id)
    if pdf_url:
        pdf_result = await _fetch_pdf_content(pdf_url)
        if pdf_result.get("ok"):
            return _build_notice_content_result(
                notice_id=resolved_id,
                detail_url=detail,
                title=expected_title or "",
                content_source="pdf",
                content_text=pdf_result.get("content_text") or "",
                content_html="",
                picture_url=pdf_url,
                system_id=_extract_biz_id_from_url(pdf_url),
            )

    data = await fetch_get_info(resolved_id)
    if not data:
        return {
            "ok": False,
            "notice_id": resolved_id,
            "detail_url": detail,
            "error": "getInfo 无响应",
        }

    api_title, content_text, content_html = parse_get_info_content(data)
    if verify_title and expected_title and not titles_match(expected_title, api_title):
        return {
            "ok": False,
            "notice_id": resolved_id,
            "detail_url": detail,
            "title": api_title,
            "error": "getInfo 标题与预期不匹配",
        }

    picture_url, system_id = resolve_picture_url(data)
    if _meaningful_text(content_text):
        return _build_notice_content_result(
            notice_id=resolved_id,
            detail_url=detail,
            title=api_title,
            content_source="html",
            content_text=content_text,
            content_html=content_html,
            picture_url=picture_url,
            system_id=system_id,
        )

    if picture_url:
        pdf_result = await _fetch_pdf_content(picture_url)
        if pdf_result.get("ok"):
            return _build_notice_content_result(
                notice_id=resolved_id,
                detail_url=detail,
                title=api_title,
                content_source="pdf",
                content_text=pdf_result.get("content_text") or "",
                content_html=content_html,
                picture_url=picture_url,
                system_id=system_id,
            )

    return _build_notice_content_result(
        notice_id=resolved_id,
        detail_url=detail,
        title=api_title,
        content_source="empty",
        content_text=content_text,
        content_html=content_html,
        picture_url=picture_url,
        system_id=system_id,
    )


def _build_notice_content_result(
    *,
    notice_id: str,
    detail_url: str,
    title: str,
    content_source: str,
    content_text: str,
    content_html: str,
    picture_url: str | None,
    system_id: str | None,
) -> dict[str, Any]:
    has_content = _meaningful_text(content_text)
    return {
        "ok": True,
        "notice_id": notice_id,
        "detail_url": detail_url,
        "title": title,
        "content_source": content_source,
        "content_text": content_text,
        "content_html": content_html,
        "picture_url": picture_url,
        "system_id": system_id,
        "has_content": has_content,
    }


async def _fetch_pdf_content(pdf_url: str) -> dict[str, Any]:
    from src.core.pdf_content import download_pdf_text

    return await download_pdf_text(
        pdf_url,
        referer="https://bid.powerchina.cn/consult/notice",
    )


async def verify_notice_id(notice_id: str, expected_title: str) -> dict[str, Any] | None:
    result = await fetch_notice_content(
        notice_id=notice_id,
        expected_title=expected_title,
        verify_title=True,
    )
    if not result.get("ok"):
        return None
    return {
        "id": notice_id,
        "title": result.get("title") or "",
        "content_text": result.get("content_text") or "",
        "content_html": result.get("content_html") or "",
        "content_source": result.get("content_source"),
        "picture_url": result.get("picture_url"),
        "system_id": result.get("system_id"),
    }


def resolve_id_from_index(
    title: str,
    index: dict[str, str],
    *,
    threshold: float = 0.48,
) -> str | None:
    key = normalize_title(title)
    if key in index:
        return index[key]

    best_id: str | None = None
    best_score = 0.0
    for candidate_key, notice_id in index.items():
        if not _query_matches_candidate(title, candidate_key):
            continue
        score = _title_match_score(title, candidate_key)
        if score > best_score:
            best_score = score
            best_id = notice_id
    if best_id and best_score >= threshold:
        return best_id
    return None


async def build_title_index(
    target_titles: set[str],
    *,
    max_pages: int = 500,
    page_size: int = 20,
    rate_limit_seconds: float = 0.2,
    cache_path: Path | None = DEFAULT_INDEX_CACHE,
    bim_cache_path: Path | None = DEFAULT_BIM_CACHE,
    force_refresh: bool = False,
) -> dict[str, str]:
    """构建 normalized_title -> notice_id 索引。

    优先级：BIM 缓存 JSON → 本地 title 索引缓存 → allList keyWords 搜索 → 全量 curpage 扫描。
    """
    normalized_targets = {normalize_title(t) for t in target_titles if t}
    index: dict[str, str] = {}

    if not force_refresh:
        index.update(load_bim_cache_index(bim_cache_path))
        if cache_path and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                by_title = cached.get("by_title") or {}
                if isinstance(by_title, dict):
                    index.update({str(k): str(v) for k, v in by_title.items()})
            except (OSError, json.JSONDecodeError, TypeError):
                logger.debug("忽略损坏的 powerchina 索引缓存: %s", cache_path)

    matched = {k for k in normalized_targets if k in index}
    if normalized_targets and matched >= normalized_targets:
        logger.info("缓存已覆盖全部目标标题 (%d 条)", len(index))
        return index

    search_keywords = collect_search_keywords(target_titles)
    for keyword in search_keywords:
        page_num = 1
        while page_num <= max(1, max_pages):
            try:
                rows, total = await fetch_keyword_list_page(
                    keyword, page_num, page_size=page_size
                )
            except Exception as exc:
                logger.warning("keyWords=%r 第 %d 页放弃: %s", keyword, page_num, exc)
                break
            if not rows:
                break
            for row in rows:
                row_title = str(row.get("title") or "").strip()
                row_id = extract_row_notice_id(row)
                if not row_title or not row_id:
                    continue
                key = normalize_title(row_title)
                index[key] = row_id
                if key in normalized_targets:
                    matched.add(key)
            if normalized_targets and matched >= normalized_targets:
                logger.info("keyWords=%r 已匹配全部目标 (page=%d)", keyword, page_num)
                break
            if page_num * page_size >= total:
                break
            page_num += 1
            if rate_limit_seconds > 0:
                await _sleep(rate_limit_seconds)
        if normalized_targets and matched >= normalized_targets:
            break

    if normalized_targets and matched >= normalized_targets:
        _write_title_index_cache(index, cache_path, max_pages)
        return index

    for curpage in range(1, max(1, max_pages) + 1):
        try:
            rows = await fetch_all_list_page(curpage, page_size=page_size)
        except Exception as exc:
            logger.warning("allList curpage=%d 放弃: %s", curpage, exc)
            break
        if not rows:
            logger.info("allList curpage=%d 无数据，停止扫描", curpage)
            break

        for row in rows:
            row_title = str(row.get("title") or "").strip()
            row_id = extract_row_notice_id(row)
            if not row_title or not row_id:
                continue
            key = normalize_title(row_title)
            index[key] = row_id
            if key in normalized_targets:
                matched.add(key)

        if curpage % 25 == 0:
            logger.info(
                "allList curpage 扫描: page=%d index=%d matched=%d/%d",
                curpage,
                len(index),
                len(matched),
                len(normalized_targets),
            )
        if normalized_targets and matched >= normalized_targets:
            logger.info("allList curpage 已匹配全部目标 (page=%d)", curpage)
            break
        if rate_limit_seconds > 0:
            await _sleep(rate_limit_seconds)

    _write_title_index_cache(index, cache_path, max_pages)
    return index


def _write_title_index_cache(
    index: dict[str, str],
    cache_path: Path | None,
    max_pages: int,
) -> None:
    if not cache_path:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "max_pages": max_pages,
                "by_title": index,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def resolve_notice_id(
    title: str,
    url: str | None,
    index: dict[str, str],
) -> tuple[str | None, str]:
    """返回 (notice_id, reason)。"""
    is_placeholder = is_search_placeholder_url(url or "")
    existing_id = None if is_placeholder else extract_notice_id(url or "")

    if existing_id and not is_placeholder:
        verified = await verify_notice_id(existing_id, title)
        if verified and (verified["content_text"] or verified["content_html"]):
            return existing_id, "verified_existing_id"

    resolved = resolve_id_from_index(title, index)
    if resolved:
        reason = "search_index_match" if is_placeholder else "index_match"
        return resolved, reason

    if existing_id:
        verified = await verify_notice_id(existing_id, title)
        if verified:
            return existing_id, "verified_existing_id_empty_content"

    return None, "unresolved"


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
