"""公告发布时间解析与回填辅助。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from src.core.timezone_utils import app_today

_PUBLISH_DATETIME_SUFFIX = (
    r"(?:[日号])?"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)

_PUBLISH_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"公告发布时间[：:为]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})" + _PUBLISH_DATETIME_SUFFIX
    ),
    re.compile(
        r"采购公告时间[：:为]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})" + _PUBLISH_DATETIME_SUFFIX
    ),
    re.compile(
        r"公告时间[：:为]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})" + _PUBLISH_DATETIME_SUFFIX
    ),
    re.compile(
        r"发布日期[：:为]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})" + _PUBLISH_DATETIME_SUFFIX
    ),
    re.compile(
        r"发布时间[：:为]?\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})" + _PUBLISH_DATETIME_SUFFIX
    ),
    re.compile(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})[日号]?\s*[至~\-]\s*\d"),
)

_DATE_YMD_RE = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")

# 日期前的截止/开标类标签 — 不得当作发布时间
_DEADLINE_CONTEXT_RE = re.compile(
    r"(?:"
    r"签收登记\s*截止|登记\s*截止|投标\s*截止|报名\s*截止|"
    r"(?:递交|提交)(?:投标)?(?:文件)?\s*截止|响应文件(?:提交|递交)?\s*截止|"
    r"文件(?:获取|递交)\s*截止|报价\s*截止|"
    r"获取招标文件\s*截止|招标文件\s*获取\s*截止|"
    r"截标[/／]?开标|截标|"
    r"截止(?:时间|日期)?|"
    r"开标时间|开标日期|开启时间"
    r")"
)

_FUTURE_GRACE_DAYS = 1


def _groups_to_datetime(match: re.Match[str]) -> Optional[datetime]:
    try:
        hour = int(match.group(4)) if match.lastindex and match.lastindex >= 4 and match.group(4) else 0
        minute = int(match.group(5)) if match.lastindex and match.lastindex >= 5 and match.group(5) else 0
        second = int(match.group(6)) if match.lastindex and match.lastindex >= 6 and match.group(6) else 0
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            hour,
            minute,
            second,
        )
    except (ValueError, TypeError):
        return None


def _is_deadline_labeled(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 50) : match.start()]
    return bool(_DEADLINE_CONTEXT_RE.search(prefix))


def _is_unreasonable_future_publish(dt: datetime, *, grace_days: int = _FUTURE_GRACE_DAYS) -> bool:
    return dt.date() > app_today() + timedelta(days=grace_days)


def _accept_publish_datetime(
    dt: Optional[datetime],
    *,
    reject_future: bool,
) -> Optional[datetime]:
    if dt is None:
        return None
    if reject_future and _is_unreasonable_future_publish(dt):
        return None
    return dt


def parse_publish_datetime(
    value: Any,
    *,
    reject_deadline: bool = True,
    reject_future: bool = False,
) -> Optional[datetime]:
    """解析 API/文档中的发布时间字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _accept_publish_datetime(value, reject_future=reject_future)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized[:10]):
        try:
            return _accept_publish_datetime(
                datetime.fromisoformat(candidate),
                reject_future=reject_future,
            )
        except ValueError:
            continue

    for match in _DATE_YMD_RE.finditer(text):
        if reject_deadline and _is_deadline_labeled(text, match):
            continue
        dt = _groups_to_datetime(match)
        accepted = _accept_publish_datetime(dt, reject_future=reject_future)
        if accepted:
            return accepted
    return None


def extract_publish_date_from_content(text: str | None) -> Optional[datetime]:
    """从正文纯文本中提取公告发布时间（仅匹配发布类标签）。"""
    if not text:
        return None
    for pattern in _PUBLISH_LABEL_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        dt = _accept_publish_datetime(_groups_to_datetime(match), reject_future=True)
        if dt:
            return dt
    return None


def reconcile_publish_date(
    *,
    publish_date: datetime | None,
    content_text: str | None,
    scraped_at: datetime | None = None,
) -> Optional[datetime]:
    """详情入库前校正发布时间：正文发布标签优先，剔除列表页误入的截止/未来日期。"""
    content_pub = extract_publish_date_from_content(content_text)
    if content_pub:
        return content_pub

    if publish_date is None:
        return None

    validated = parse_date_from_page_text(publish_date.strftime("%Y-%m-%d"))
    if validated is None:
        return None

    if scraped_at is not None:
        scrape_day = scraped_at.date() if isinstance(scraped_at, datetime) else None
        if scrape_day and validated.date() > scrape_day:
            return None

    return validated


def parse_date_from_page_text(text: str | None) -> Optional[datetime]:
    """从列表页/页面片段解析发布时间，优先发布标签并排除截止类日期。"""
    if not text:
        return None
    stripped = str(text).strip()
    if not stripped:
        return None
    labeled = extract_publish_date_from_content(stripped)
    if labeled:
        return labeled
    return parse_publish_datetime(
        stripped,
        reject_deadline=True,
        reject_future=True,
    )


def publish_date_day(value: Any) -> Optional[str]:
    """归一化为 YYYY-MM-DD 便于比较。"""
    dt = parse_publish_datetime(value)
    return dt.strftime("%Y-%m-%d") if dt else None


def doc_publish_day(doc: dict[str, Any]) -> Optional[str]:
    for key in ("publish_date", "published_at"):
        day = publish_date_day(doc.get(key))
        if day:
            return day
    return None


def doc_scrape_day(doc: dict[str, Any]) -> Optional[str]:
    for key in ("scraped_at", "crawled_at", "created_at"):
        day = publish_date_day(doc.get(key))
        if day:
            return day
    return None


def publish_date_suspicious(doc: dict[str, Any]) -> bool:
    """发布时间缺失、与采集日相同、晚于采集日/今天，或与正文发布标签不一致。"""
    pub_day = doc_publish_day(doc)
    if not pub_day:
        return True
    scrape_day = doc_scrape_day(doc)
    if scrape_day and pub_day == scrape_day:
        return True

    pub_dt = parse_publish_datetime(doc.get("publish_date"))
    if pub_dt and _is_unreasonable_future_publish(pub_dt):
        return True

    if pub_dt and scrape_day:
        scrape_dt = parse_publish_datetime(
            doc.get("scraped_at") or doc.get("crawled_at") or doc.get("created_at")
        )
        if scrape_dt and pub_dt.date() > scrape_dt.date():
            return True

    content_pub = extract_publish_date_from_content(doc.get("content_text"))
    if content_pub and publish_date_day(content_pub) != pub_day:
        return True

    return False


def serialize_publish_date(value: datetime) -> str:
    """与 Mongo 存量字段保持一致（日期 + T00:00:00）。"""
    return value.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
