"""Hermes Crawl Agent 分步 skill — 路径规划 + 单页抓取（不走 site_sync）。"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

from src.adapters.generic import GenericRuleAdapter
from src.core.crawl_rules import CrawlRule, build_page_number_url, load_crawl_rule
from src.core.models import BidNotice, ScrapeResult
from src.core.pipeline import Pipeline
from src.core.publish_date_utils import parse_publish_datetime
from src.core.rule_executor import RuleExecutor
from src.core.rule_generator import load_rule_yaml, validate_yaml
from src.core.site_aliases import resolve_canonical_site_id, site_not_registered_error
from src.core.site_credentials import reset_site_auth_context, set_site_auth_context
from src.core.site_sync import get_site_by_id
from src.core.browser import BrowserPool
from src.web.crawl_agent_cancel import (
    CrawlAgentCancelledError,
    raise_if_chat_cancelled,
    track_skill_session,
)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_MAX_PAGES = 3
ABSOLUTE_MAX_PAGES = 500
SKILL_ASYNC_TIMEOUT = 120.0
SKILL_SESSION_MAX_AGE = 600.0
NAVIGATE_TIMEOUT_MS = 60_000
CLICK_TIMEOUT_MS = 10_000

_pipeline: Optional[Pipeline] = None
_skill_loop: Optional[asyncio.AbstractEventLoop] = None
_skill_loop_thread: Optional[threading.Thread] = None
_skill_loop_init = threading.Lock()


def _get_skill_loop() -> asyncio.AbstractEventLoop:
    """Playwright 会话绑定单一事件循环，避免 run_async 每次 asyncio.run 导致跨 loop 挂死。"""
    global _skill_loop, _skill_loop_thread
    with _skill_loop_init:
        if _skill_loop is None:
            loop = asyncio.new_event_loop()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            _skill_loop_thread = threading.Thread(
                target=_run_loop, name="skill-async-loop", daemon=True
            )
            _skill_loop_thread.start()
            _skill_loop = loop
        return _skill_loop


def run_async(coro: Any, *, timeout: float = SKILL_ASYNC_TIMEOUT) -> Any:
    """在持久 skill 事件循环中运行协程（线程安全）。"""
    loop = _get_skill_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _get_pipeline() -> Pipeline:
    """复用 Pipeline 实例，避免每条 save 重复连 Redis/PG 并全量读 JSONL。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


def _is_pure_api_list_rule(rule: CrawlRule) -> bool:
    """与 RuleExecutor._is_pure_api_list 一致。"""
    list_page = rule.list_page
    if list_page is None or list_page.api is None:
        return False
    if list_page.strategy != "api":
        return False
    if list_page.wait_network is not None:
        return False
    if rule.entry_steps:
        return False
    return True


async def http_fetch_list_page_async(
    site_id: str,
    *,
    page_num: int = 1,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """纯 HTTP 抓取 API 列表页（零 browser，适用于 list_page.strategy=api）。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(site_id)}
    rule = load_crawl_rule(site)
    if not rule or not rule.list_page or not rule.list_page.api:
        return {"ok": False, "error": f"站点 {site_id} 缺少 list_page.api 配置"}

    list_page = rule.list_page
    api = list_page.api
    pag = rule.pagination
    page_param = (pag.page_param if pag else None) or api.page_param
    auth_token = set_site_auth_context(site_id)

    try:
        raise_if_chat_cancelled()
        from src.core.http_api_client import fetch_api_json

        body = await fetch_api_json(
            api,
            page_param=page_param,
            page_num=page_num,
            site_url=site.get("url") or "",
            entry_url=rule.entry_url or site.get("url") or "",
        )
        from src.core.rule_executor import RuleExecutor, _get_nested

        items = _get_nested(body, api.items_path)
        if isinstance(items, dict):
            for key in ("records", "list", "rows", "items"):
                nested = items.get(key)
                if isinstance(nested, list):
                    items = nested
                    break
        if not isinstance(items, list):
            items = []

        executor = RuleExecutor(MagicMock(), site)
        notices = executor._api_items_to_notices(items, api, rule)  # noqa: SLF001
        batch_items = [_notice_item(n) for n in notices]

        max_p = min(rule.limits.max_pages, (pag.max_pages if pag else rule.limits.max_pages))
        has_next = bool(pag and pag.type == "ajax_param" and page_num < max_p and batch_items)
        if pag and pag.stop_when_empty and not batch_items:
            has_next = False

        return {
            "ok": True,
            "site_id": site_id,
            "page_num": page_num,
            "list_url": api.url,
            "items": batch_items,
            "item_count": len(batch_items),
            "has_next": has_next,
            "next_page_url": None,
            "session_id": session_id or "default",
            "transport": "http",
        }
    except Exception as exc:
        logger.exception("skill_http_fetch_list 失败 site=%s page=%s", site_id, page_num)
        from src.core.http_api_client import format_http_api_error, resolve_http_proxy

        hint = format_http_api_error(exc)
        payload: dict[str, Any] = {
            "ok": False,
            "site_id": site_id,
            "page_num": page_num,
            "error": hint,
            "transport": "http",
        }
        proxy = resolve_http_proxy()
        if proxy:
            payload["proxy_configured"] = True
        else:
            payload["proxy_configured"] = False
            payload["hint"] = (
                "未配置 HTTP_PROXY/HTTPS_PROXY；IP 封锁时可写入 .env 或在可访问网络运行 "
                "scripts/crawl_powerchina_http.py"
            )
        return payload
    finally:
        reset_site_auth_context(auth_token)


def _pagination_url(
    rule: CrawlRule,
    page_num: int,
    *,
    base_url: Optional[str] = None,
) -> str:
    pag = rule.pagination
    entry = base_url or rule.entry_url or ""
    if pag is None:
        return entry
    return build_page_number_url(
        entry,
        page_num,
        page_param=pag.page_param,
        url_style=pag.url_style,
    )


async def _skill_goto_list(
    page: Any,
    url: str,
    executor: RuleExecutor,
    rule: CrawlRule,
) -> str:
    """带超时的列表页导航 + DOM 就绪等待。"""
    raise_if_chat_cancelled()

    async def _goto() -> None:
        raise_if_chat_cancelled()
        await page.goto(url, wait_until="networkidle", timeout=NAVIGATE_TIMEOUT_MS)

    await asyncio.wait_for(_goto(), timeout=NAVIGATE_TIMEOUT_MS / 1000 + 5)
    raise_if_chat_cancelled()
    list_page = rule.list_page
    if list_page is not None:
        await asyncio.wait_for(
            executor._prepare_dom_list(page, list_page),  # noqa: SLF001
            timeout=20,
        )
    return page.url


def _notice_item(notice: BidNotice) -> dict[str, Any]:
    pub = notice.publish_date.isoformat() if notice.publish_date else None
    return {
        "title": notice.title,
        "url": notice.url,
        "date": pub,
        "category": notice.category.value if notice.category else None,
    }


def _parse_requested_pages(user_intent: str, rule_max: int) -> int:
    text = (user_intent or "").lower()
    m = re.search(r"(\d+)\s*页", text)
    if m:
        return min(int(m.group(1)), ABSOLUTE_MAX_PAGES, rule_max)
    if any(k in text for k in ("全部", "全量", "all", "500")):
        return min(rule_max, ABSOLUTE_MAX_PAGES)
    return min(DEFAULT_AGENT_MAX_PAGES, rule_max)


def plan_crawl_path(
    site_id: str,
    *,
    user_intent: str = "",
    max_pages_override: Optional[int] = None,
) -> dict[str, Any]:
    """读 crawl_rules + 用户意图 → 结构化爬取计划。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(site_id)}

    rule = load_crawl_rule(site)
    if not rule or not rule.list_page:
        yaml_text = load_rule_yaml(site_id)
        valid = False
        errors: list[str] = []
        if yaml_text:
            v = validate_yaml(yaml_text, site_id=site_id)
            valid = bool(v.get("valid"))
            errors = v.get("errors") or []
        entry = site.get("url") or ""
        return {
            "ok": False,
            "site_id": site_id,
            "error": "缺少可用 crawl_rules 或 list_page 配置",
            "has_rule": yaml_text is not None,
            "valid": valid,
            "errors": errors,
            "recommended_flow": "webbridge_probe_then_generate_rules",
            "preferred_skills": ["webbridge-crawl", "crawl-rule-generation"],
            "recommended_first_tool": "skill_webbridge_check",
            "hint": (
                "新站/无规则：先 skill_webbridge_check → navigate → skill_analyze_list_dom "
                "→ crawl_generate_rule(page_hints) → crawl_test → crawl_enable_site；"
                "不要直接 skill_fetch_list_page"
            ),
            "entry_url": entry,
        }

    entry = rule.entry_url or site.get("url") or ""
    pag = rule.pagination
    strategy = pag.type if pag else "none"
    rule_max_pages = min(rule.limits.max_pages, ABSOLUTE_MAX_PAGES)
    if pag and pag.max_pages:
        rule_max_pages = min(rule_max_pages, pag.max_pages)

    agent_pages = max_pages_override if max_pages_override is not None else _parse_requested_pages(
        user_intent, rule_max_pages
    )
    agent_pages = max(1, min(agent_pages, rule_max_pages))

    path_tree = {
        "entry": entry,
        "pages": [
            {
                "page_num": n,
                "label": f"列表第 {n} 页",
                "children": "items[] → detail? → save",
            }
            for n in range(1, agent_pages + 1)
        ],
    }

    fallback_tool = (
        "skill_http_fetch_list" if _is_pure_api_list_rule(rule) else "skill_fetch_list_page"
    )

    return {
        "ok": True,
        "site_id": site_id,
        "site_name": site.get("name") or rule.name,
        "entry_urls": [entry] if entry else [],
        "list_page_strategy": rule.list_page.strategy,
        "transport": "http" if _is_pure_api_list_rule(rule) else "browser",
        "recommended_list_tool": "skill_webbridge_extract_list",
        "fallback_list_tool": fallback_tool,
        "preferred_probe_flow": "webbridge_first",
        "pagination_strategy": strategy,
        "pagination_selector": (pag.selector if pag else None),
        "max_pages": rule_max_pages,
        "agent_max_pages": agent_pages,
        "max_items": rule.limits.max_items,
        "fetch_detail": bool(rule.detail and rule.detail.fetch_detail),
        "rate_limit_seconds": rule.limits.rate_limit_seconds,
        "path_tree": path_tree,
        "summary": (
            f"{site_id}: entry={entry}, strategy={strategy}, "
            f"计划 {agent_pages}/{rule_max_pages} 页"
        ),
    }


def load_rules(site_id: str) -> dict[str, Any]:
    """加载站点 YAML 规则摘要。"""
    canonical_id = resolve_canonical_site_id(site_id)
    yaml_text = load_rule_yaml(canonical_id)
    if yaml_text is None:
        return {"ok": True, "site_id": canonical_id, "has_rule": False, "yaml": None}
    validation = validate_yaml(yaml_text, site_id=canonical_id)
    site = get_site_by_id(canonical_id)
    rule = load_crawl_rule(site) if site else None
    selectors: dict[str, Any] = {}
    if rule and rule.list_page:
        lp = rule.list_page
        selectors = {
            "container": lp.container,
            "item": lp.item,
            "title": lp.title,
            "link": lp.link,
            "date": lp.date,
            "wait_for": lp.wait_for,
        }
    return {
        "ok": True,
        "site_id": canonical_id,
        "has_rule": True,
        "valid": validation.get("valid"),
        "errors": validation.get("errors"),
        "selectors": selectors,
        "entry_url": rule.entry_url if rule else None,
        "pagination": rule.pagination.model_dump() if rule and rule.pagination else None,
        "limits": rule.limits.model_dump() if rule else None,
    }


@dataclass
class _SkillSession:
    site_id: str
    site: dict[str, Any]
    rule: CrawlRule
    pool: BrowserPool
    context: Any
    page: Any
    executor: RuleExecutor
    page_num: int = 1
    auth_token: Any = None
    created_at: float = field(default_factory=time.monotonic)


_sessions: dict[str, _SkillSession] = {}
_session_locks: dict[str, asyncio.Lock] = {}
_session_create_lock = asyncio.Lock()


def _session_key(site_id: str, session_id: Optional[str]) -> str:
    return f"{site_id}:{session_id or 'default'}"


def _get_session_lock(key: str) -> asyncio.Lock:
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


async def _open_persistent_page(pool: BrowserPool) -> tuple[Any, Any]:
    """创建可跨 skill 调用复用的 context + page。"""
    from src.core.browser import random_user_agent
    from src.core.site_credentials import get_current_browser_auth

    if pool._browser is None:  # noqa: SLF001
        raise RuntimeError("BrowserPool 未启动")
    auth = get_current_browser_auth()
    context_kwargs: dict[str, Any] = {
        "user_agent": random_user_agent(),
        "locale": "zh-CN",
    }
    headers = auth.get("extra_http_headers") or {}
    if headers:
        context_kwargs["extra_http_headers"] = headers
    context = await pool._browser.new_context(**context_kwargs)  # noqa: SLF001
    cookies = auth.get("cookies") or []
    if cookies:
        await context.add_cookies(cookies)
    context.set_default_timeout(pool.timeout_ms)
    page = await context.new_page()
    return context, page


async def _cleanup_skill_session(sess: _SkillSession) -> None:
    try:
        await asyncio.wait_for(sess.page.close(), timeout=10)
    except Exception:
        pass
    try:
        await asyncio.wait_for(sess.context.close(), timeout=10)
    except Exception:
        pass
    reset_site_auth_context(sess.auth_token)
    try:
        await asyncio.wait_for(sess.pool.stop(), timeout=15)
    except Exception:
        pass


async def close_skill_session(site_id: str, session_id: Optional[str] = None) -> None:
    key = _session_key(site_id, session_id)
    lock = _get_session_lock(key)
    async with lock:
        sess = _sessions.pop(key, None)
        _session_locks.pop(key, None)
    if sess is None:
        return
    await _cleanup_skill_session(sess)


async def _get_or_create_session(site_id: str, session_id: Optional[str] = None) -> _SkillSession:
    key = _session_key(site_id, session_id)
    lock = _get_session_lock(key)
    stale_sess: Optional[_SkillSession] = None

    async with lock:
        existing = _sessions.get(key)
        if existing is not None:
            if time.monotonic() - existing.created_at <= SKILL_SESSION_MAX_AGE:
                return existing
            logger.warning("skill session 已超时，重建: %s", key)
            stale_sess = _sessions.pop(key, None)
            _session_locks.pop(key, None)

    if stale_sess is not None:
        await _cleanup_skill_session(stale_sess)

    async with lock:
        async with _session_create_lock:
            existing = _sessions.get(key)
            if existing is not None:
                return existing

            site = get_site_by_id(site_id)
            if not site:
                raise ValueError(site_not_registered_error(site_id))
            rule = load_crawl_rule(site)
            if not rule or not rule.list_page:
                raise ValueError(f"站点 {site_id} 缺少 list_page 规则")

            pool = BrowserPool(headless=True)
            await asyncio.wait_for(pool.start(), timeout=30)
            auth_token = set_site_auth_context(site_id)
            context, page = await _open_persistent_page(pool)
            executor = RuleExecutor(pool, site)
            sess = _SkillSession(
                site_id=site_id,
                site=site,
                rule=rule,
                pool=pool,
                context=context,
                page=page,
                executor=executor,
                auth_token=auth_token,
            )
            _sessions[key] = sess
            track_skill_session(site_id, session_id)
            return sess


async def fetch_list_page_async(
    site_id: str,
    *,
    page_num: int = 1,
    list_url: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """打开/复用列表页，返回 items + next_page_url 提示。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(site_id)}
    rule = load_crawl_rule(site)
    if rule and _is_pure_api_list_rule(rule) and not list_url:
        return await http_fetch_list_page_async(
            site_id, page_num=page_num, session_id=session_id
        )

    key = _session_key(site_id, session_id)
    sess = await _get_or_create_session(site_id, session_id)
    lock = _get_session_lock(key)

    async with lock:
        track_skill_session(site_id, session_id)
        rule = sess.rule
        pag = rule.pagination
        page = sess.page
        executor = sess.executor

        try:
            raise_if_chat_cancelled()
            if page_num <= 1:
                url = list_url or rule.entry_url or sess.site.get("url") or ""
                if list_url:
                    current_url = await _skill_goto_list(page, url, executor, rule)
                else:
                    current_url = await asyncio.wait_for(
                        executor.skill_navigate_entry(page, rule),
                        timeout=NAVIGATE_TIMEOUT_MS / 1000 + 10,
                    )
                sess.page_num = 1
            else:
                if pag and pag.type == "page_number":
                    target_url = list_url or _pagination_url(rule, page_num)
                    current_url = await _skill_goto_list(page, target_url, executor, rule)
                    sess.page_num = page_num
                elif list_url:
                    current_url = await _skill_goto_list(page, list_url, executor, rule)
                    sess.page_num = page_num
                elif sess.page_num == page_num - 1:
                    current_url = page.url
                    sess.page_num = page_num
                else:
                    return {
                        "ok": False,
                        "error": (
                            f"页码不连续：当前 session 在第 {sess.page_num} 页，"
                            f"请传 list_url 或先 skill_paginate"
                        ),
                        "current_page_num": sess.page_num,
                    }

            notices = await asyncio.wait_for(
                executor.skill_parse_list(page, rule),
                timeout=30,
            )
            items = [_notice_item(n) for n in notices]

            if pag and pag.type == "page_number":
                max_p = min(rule.limits.max_pages, pag.max_pages or rule.limits.max_pages)
                has_next = page_num < max_p
                next_url = _pagination_url(rule, page_num + 1) if has_next else None
            else:
                has_next = await asyncio.wait_for(
                    executor.skill_has_next_page(page, rule),
                    timeout=15,
                )
                next_url = page.url if has_next else None

            return {
                "ok": True,
                "site_id": site_id,
                "page_num": page_num,
                "list_url": current_url,
                "items": items,
                "item_count": len(items),
                "has_next": has_next,
                "next_page_url": next_url,
                "session_id": session_id or "default",
            }
        except CrawlAgentCancelledError:
            return {
                "ok": False,
                "site_id": site_id,
                "page_num": page_num,
                "cancelled": True,
                "error": "用户已中止爬取",
            }
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "site_id": site_id,
                "page_num": page_num,
                "error": f"抓取第 {page_num} 页超时（>{SKILL_ASYNC_TIMEOUT}s）",
            }
        except Exception as exc:
            logger.exception("skill_fetch_list_page 失败 site=%s page=%s", site_id, page_num)
            return {
                "ok": False,
                "site_id": site_id,
                "page_num": page_num,
                "error": str(exc),
            }


async def paginate_async(
    site_id: str,
    *,
    current_url: Optional[str] = None,
    page_num: Optional[int] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """根据规则翻页或计算下一页 URL（不整站跑）。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(site_id)}
    rule = load_crawl_rule(site)
    if not rule:
        return {"ok": False, "error": f"站点 {site_id} 缺少 crawl_rules"}

    pag = rule.pagination
    if pag is None:
        return {
            "ok": True,
            "site_id": site_id,
            "has_next": False,
            "next_page_url": None,
            "message": "规则无分页配置",
        }

    key = _session_key(site_id, session_id)

    if pag.type == "page_number":
        base = current_url or rule.entry_url or site.get("url") or ""
        sess = _sessions.get(key)
        cur = sess.page_num if sess else 1
        target_num = page_num if page_num is not None else cur + 1
        next_url = _pagination_url(rule, target_num, base_url=base)
        max_p = min(rule.limits.max_pages, pag.max_pages or rule.limits.max_pages)
        has_next = target_num < max_p

        if sess is not None:
            lock = _get_session_lock(key)
            async with lock:
                try:
                    current_url = await _skill_goto_list(
                        sess.page, next_url, sess.executor, rule
                    )
                    sess.page_num = target_num
                except asyncio.TimeoutError:
                    return {
                        "ok": False,
                        "site_id": site_id,
                        "error": f"翻页导航超时: {next_url}",
                    }
                except Exception as exc:
                    return {"ok": False, "site_id": site_id, "error": str(exc)}
        else:
            current_url = next_url

        return {
            "ok": True,
            "site_id": site_id,
            "has_next": has_next,
            "next_page_url": next_url if has_next else None,
            "page_num": target_num,
            "list_url": current_url,
            "pagination_strategy": "page_number",
        }

    sess = await _get_or_create_session(site_id, session_id)
    lock = _get_session_lock(key)

    async with lock:
        track_skill_session(site_id, session_id)
        try:
            raise_if_chat_cancelled()
            if not await asyncio.wait_for(
                sess.executor.skill_has_next_page(sess.page, rule),
                timeout=15,
            ):
                return {
                    "ok": True,
                    "site_id": site_id,
                    "has_next": False,
                    "next_page_url": None,
                    "page_num": sess.page_num,
                }
            new_url, has_next = await asyncio.wait_for(
                sess.executor.skill_click_next_page(sess.page, rule),
                timeout=30,
            )
            sess.page_num += 1
            return {
                "ok": True,
                "site_id": site_id,
                "has_next": has_next,
                "next_page_url": new_url,
                "page_num": sess.page_num,
                "list_url": new_url,
                "pagination_strategy": "next_button",
            }
        except CrawlAgentCancelledError:
            return {
                "ok": False,
                "site_id": site_id,
                "cancelled": True,
                "error": "用户已中止爬取",
            }
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "site_id": site_id,
                "error": "翻页操作超时",
            }
        except Exception as exc:
            logger.exception("skill_paginate 失败 site=%s", site_id)
            return {"ok": False, "site_id": site_id, "error": str(exc)}

    return {
        "ok": False,
        "site_id": site_id,
        "error": f"暂不支持的分页策略: {pag.type}（请用 skill_fetch_list_page + list_url）",
    }


async def fetch_detail_page_async(
    site_id: str,
    url: str,
    *,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """抓取单条详情页正文与附件。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(site_id)}

    notice = BidNotice(
        title=title or url,
        url=url,
        source_site_id=site_id,
        source_site_name=site.get("name") or site_id,
        source_url=site.get("url") or url,
    )

    rule = load_crawl_rule(site)
    detail_api = rule and rule.detail and rule.detail.strategy == "api"

    auth_token = set_site_auth_context(site_id)
    pool: BrowserPool | None = None
    try:
        if detail_api:
            adapter = GenericRuleAdapter(site, MagicMock())
        else:
            pool = BrowserPool(headless=True)
            await pool.start()
            adapter = GenericRuleAdapter(site, pool)
        enriched = await adapter.fetch_detail(notice)
        attachments = enriched.attachments or []
        return {
            "ok": True,
            "site_id": site_id,
            "url": url,
            "title": enriched.title,
            "content_text": (enriched.content_text or "")[:2000],
            "content_html_len": len(enriched.content_html or ""),
            "attachments": attachments,
            "has_content": bool(enriched.content_text or enriched.content_html),
            "transport": "http" if detail_api else "browser",
        }
    except Exception as exc:
        logger.exception("skill_fetch_detail_page 失败 site=%s url=%s", site_id, url)
        return {"ok": False, "site_id": site_id, "url": url, "error": str(exc)}
    finally:
        reset_site_auth_context(auth_token)
        if pool is not None:
            await pool.stop()


async def fetch_notice_content_async(
    *,
    notice_id: Optional[str] = None,
    detail_url: Optional[str] = None,
    pdf_url: Optional[str] = None,
    expected_title: Optional[str] = None,
    verify_title: bool = True,
) -> dict[str, Any]:
    """电建 getInfo + PDF 正文抓取（HTML 空时自动下载 pictureUrl）。"""
    from src.core.powerchina_notice import fetch_notice_content

    try:
        result = await fetch_notice_content(
            notice_id=(notice_id or "").strip() or None,
            detail_url=(detail_url or "").strip() or None,
            pdf_url=(pdf_url or "").strip() or None,
            expected_title=(expected_title or "").strip() or None,
            verify_title=bool(verify_title),
        )
        if result.get("ok") and result.get("content_text"):
            preview = str(result["content_text"])[:2000]
            result = {**result, "content_text_preview": preview}
        return result
    except Exception as exc:
        logger.exception(
            "skill_fetch_notice_content 失败 notice_id=%s detail_url=%s",
            notice_id,
            detail_url,
        )
        return {"ok": False, "error": str(exc)}


def save_notice(
    site_id: str,
    *,
    title: str,
    url: str,
    publish_date: Optional[str] = None,
    content_text: Optional[str] = None,
    content_html: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    on_url_crawl: Optional[Callable[[dict[str, Any]], None]] = None,
    index: Optional[int] = None,
) -> dict[str, Any]:
    """单条公告写入 pipeline（去重）。"""
    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(site_id)}

    canonical_id = site["id"]

    try:
        from src.core.powerchina_notice import (
            POWERCHINA_SITE_ID,
            is_search_placeholder_url,
            is_valid_detail_url,
        )

        if canonical_id == POWERCHINA_SITE_ID or "powerchina.cn" in (url or ""):
            if is_search_placeholder_url(url):
                return {
                    "ok": False,
                    "site_id": canonical_id,
                    "url": url,
                    "saved": False,
                    "error": (
                        "拒绝入库 search 占位 URL；请用 allList keyWords 或 list API "
                        "解析真实 notice/detail?id= 后再 save_notice"
                    ),
                }
            if url and not is_valid_detail_url(url):
                return {
                    "ok": False,
                    "site_id": canonical_id,
                    "url": url,
                    "saved": False,
                    "error": "电建公告 URL 须为 notice/detail?id={数字ID}",
                }
    except ImportError:
        pass

    pub_dt: Optional[datetime] = None
    if publish_date:
        pub_dt = parse_publish_datetime(publish_date, reject_future=True)

    notice = BidNotice(
        title=title,
        url=url,
        source_site_id=canonical_id,
        source_site_name=site.get("name") or canonical_id,
        source_url=site.get("url") or url,
        publish_date=pub_dt,
        content_text=content_text,
        content_html=content_html,
        attachments=attachments or [],
    )
    from src.core.notice_tags import apply_notice_tags

    notice = apply_notice_tags(notice, site=site)

    pipeline = _get_pipeline()
    notice = pipeline.clean(notice)
    is_dup = pipeline._is_duplicate(notice)  # noqa: SLF001 — skill 单条保存
    if is_dup and not (content_text or content_html):
        payload = {
            "url": url,
            "title": title,
            "status": "skipped",
            "index": index,
            "message": "重复，已跳过",
        }
        if on_url_crawl:
            on_url_crawl(payload)
        return {"ok": True, "site_id": canonical_id, "url": url, "saved": False, "duplicate": True}

    if on_url_crawl:
        on_url_crawl(
            {
                "url": url,
                "title": title,
                "status": "fetching",
                "index": index,
            }
        )

    result = pipeline.process(
        ScrapeResult(site_id=canonical_id, success=True, notices=[notice])
    )
    saved_count = pipeline.save(result)
    saved = saved_count > 0 or bool(result.enriched)

    if on_url_crawl:
        on_url_crawl(
            {
                "url": url,
                "title": title,
                "status": "saved" if saved else "skipped",
                "index": index,
            }
        )

    return {
        "ok": True,
        "site_id": canonical_id,
        "url": url,
        "saved": saved,
        "duplicate": is_dup and not saved,
        "new_count": saved_count,
    }
