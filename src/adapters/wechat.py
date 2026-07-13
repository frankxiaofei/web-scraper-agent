"""微信公众号适配器 — 搜狗搜索 + 链式发现。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.adapters.base import BaseAdapter
from src.core.models import BidNotice, NoticeCategory
from src.core.notice_tags import WECHAT_TAG, apply_notice_tags
from src.core.wechat_crawl import (
    crawl_new_articles,
    parse_publish_datetime,
    resolve_wechat_biz,
    resolve_wechat_nickname,
)

logger = logging.getLogger(__name__)


class WechatAdapter(BaseAdapter):
    """微信公众号文章爬取适配器。"""

    async def fetch_list(self, max_items: int = 20) -> list[BidNotice]:
        nickname = resolve_wechat_nickname(self.config)
        seed_url = self.base_url
        sogou_max_pages = int(self.config.get("wechat_sogou_max_pages", 2))
        chain_max_seed = int(self.config.get("wechat_chain_max_seed", 5))
        article_delay = float(self.config.get("min_delay_seconds", 3))

        force_refresh = bool(self.config.get("wechat_force_refresh", False))

        use_webbridge = bool(self.config.get("wechat_use_webbridge", True))

        articles = await asyncio.to_thread(
            crawl_new_articles,
            site_id=self.site_id,
            nickname=nickname,
            seed_url=seed_url,
            max_articles=max_items,
            sogou_max_pages=sogou_max_pages,
            chain_max_seed=chain_max_seed,
            article_delay_seconds=article_delay,
            force_refresh=force_refresh,
            use_sogou=False,
            use_webbridge=use_webbridge,
            wechat_biz=resolve_wechat_biz(self.config),
        )

        notices: list[BidNotice] = []
        for article in articles:
            title = (article.get("title") or "").strip()
            url = article.get("url") or ""
            content = article.get("content") or ""
            if not url or (not title and not content):
                continue
            notices.append(
                apply_notice_tags(
                    BidNotice(
                        title=title or url,
                        url=url,
                        source_site_id=self.site_id,
                        source_site_name=self.site_name,
                        source_url=seed_url,
                        publish_date=parse_publish_datetime(article.get("publish_time", "")),
                        region=self.config.get("region"),
                        category=NoticeCategory.OTHER,
                        content_text=content or None,
                        content_html=article.get("content_html") or None,
                        attachments=article.get("attachments") or [],
                        tags=[WECHAT_TAG],
                    ),
                    site=self.config,
                )
            )

        logger.info("站点 %s 微信公众号解析 %d 条", self.site_id, len(notices))
        return notices

    async def fetch_detail(self, notice: BidNotice) -> BidNotice:
        """正文已在 fetch_list 中提取，直接返回。"""
        return notice
