"""试跑 crawl rules：抓取列表预览，不写 notices。"""

from __future__ import annotations

import logging
from typing import Any, Optional

import yaml

from src.core.browser import BrowserPool
from src.core.crawl_rules import CrawlRule, RuleLoader, load_crawl_rule
from src.core.models import BidNotice
from src.core.rule_executor import RuleExecutor
from src.core.rule_generator import validate_yaml
from src.core.site_sync import get_site_by_id

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 20
DEFAULT_MAX_PAGES = 1


def _notice_preview(notice: BidNotice) -> dict[str, Any]:
    pub = notice.publish_date.isoformat() if notice.publish_date else None
    cat = notice.category.value if notice.category else None
    return {
        "title": notice.title,
        "url": notice.url,
        "publish_date": pub,
        "category": cat,
    }


def _prepare_rule_for_dry_run(
    rule: CrawlRule,
    *,
    max_items: int,
    max_pages: int,
) -> CrawlRule:
    limits = rule.limits.model_copy(
        update={
            "max_pages": min(max_pages, rule.limits.max_pages),
            "max_items": min(max_items, rule.limits.max_items),
        }
    )
    pagination = rule.pagination
    if pagination is not None:
        pagination = pagination.model_copy(
            update={"max_pages": min(max_pages, pagination.max_pages)}
        )
    return rule.model_copy(update={"limits": limits, "pagination": pagination})


def _resolve_rule(
    site: dict[str, Any],
    yaml_text: Optional[str],
    site_id: str,
) -> tuple[Optional[CrawlRule], list[str]]:
    if yaml_text is not None:
        result = validate_yaml(yaml_text, site_id=site_id)
        if not result["valid"]:
            return None, result["errors"]
        raw = yaml.safe_load(yaml_text) or {}
        try:
            rule = CrawlRule.model_validate(raw)
        except Exception as e:
            return None, [str(e)]
        rule = RuleLoader._merge_site_config(rule, site)
        if not rule.list_page:
            return None, ["规则缺少 list_page 配置"]
        return rule, []

    rule = load_crawl_rule(site)
    if not rule or not rule.list_page:
        return None, ["未找到可用的 crawl_rules 或缺少 list_page"]
    return rule, []


async def dry_run_crawl_rule(
    site_id: str,
    *,
    yaml_text: Optional[str] = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """试跑 1 页列表，返回预览 items，不写入 MongoDB。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"success": False, "items": [], "errors": ["站点不存在"]}

    rule, errors = _resolve_rule(site, yaml_text, site_id)
    if errors:
        return {"success": False, "items": [], "errors": errors}

    rule = _prepare_rule_for_dry_run(rule, max_items=max_items, max_pages=max_pages)
    cap = min(max_items, rule.limits.max_items)

    from unittest.mock import MagicMock

    from src.core.site_credentials import reset_site_auth_context, set_site_auth_context

    auth_token = set_site_auth_context(site_id)
    try:
        probe = RuleExecutor(MagicMock(), site)
        if probe._is_pure_api_list(rule):  # noqa: SLF001
            notices = await probe.execute(rule, cap)
            items = [_notice_preview(n) for n in notices]
            return {"success": True, "items": items, "errors": [], "transport": "http"}

        pool = BrowserPool(headless=True)
        await pool.start()
        try:
            executor = RuleExecutor(pool, site)
            notices = await executor.execute(rule, cap)
            items = [_notice_preview(n) for n in notices]
            return {"success": True, "items": items, "errors": [], "transport": "browser"}
        finally:
            await pool.stop()
    except Exception as e:
        logger.exception("dry-run 失败 site=%s", site_id)
        return {"success": False, "items": [], "errors": [str(e)]}
    finally:
        reset_site_auth_context(auth_token)
