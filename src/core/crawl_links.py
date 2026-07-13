"""爬取链接序列化工具。"""

from __future__ import annotations

from typing import Any, Optional

from src.core.models import BidNotice


def notice_to_link(notice: BidNotice) -> dict[str, Any]:
    publish_date: Optional[str] = None
    if notice.publish_date:
        publish_date = notice.publish_date.isoformat()
    return {
        "title": notice.title,
        "url": notice.url,
        "publish_date": publish_date,
    }
