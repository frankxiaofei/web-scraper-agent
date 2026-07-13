"""L2 声明式爬取规则执行器。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin

from src.core.browser import BrowserPool
from src.core.crawl_rules import CrawlRule, EntryStep, ListPage, ListPageApi, Pagination
from src.core.crawl_run import emit_crawl_event
from src.core.http_api_client import fetch_api_json
from src.core.models import BidNotice, NoticeCategory
from src.core.publish_date_utils import parse_date_from_page_text
from src.core.sync_cancel import raise_if_sync_cancelled
from src.core.url_utils import resolve_notice_detail_url

logger = logging.getLogger(__name__)


def _get_nested(data: Any, path: str) -> Any:
    """按点分路径读取嵌套字段，如 data.records。"""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _get_field(row: dict[str, Any], spec: str) -> Any:
    """支持 field|alt 形式的多键回退。"""
    for key in spec.split("|"):
        val = _get_nested(row, key.strip())
        if val not in (None, ""):
            return val
    return None


def _parse_date(text: str) -> Optional[datetime]:
    return parse_date_from_page_text(text)


def _guess_category(title: str) -> NoticeCategory:
    if "中标" in title or "成交" in title or "候选人" in title:
        return NoticeCategory.WIN
    if "变更" in title or "补遗" in title:
        return NoticeCategory.CHANGE
    return NoticeCategory.TENDER


class RuleExecutor:
    """解释 crawl_rules YAML，驱动浏览器或纯 HTTP 完成 entry → 列表 → 分页。"""

    def __init__(self, browser: BrowserPool, site_config: dict[str, Any]) -> None:
        self.browser = browser
        self.site_config = site_config
        self.site_id = site_config.get("id", "")
        self.site_name = site_config.get("name", "")

    def _is_pure_api_list(self, rule: CrawlRule) -> bool:
        """list_page.strategy=api 且无 entry_steps / wait_network 时可零 browser。"""
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

    async def execute(self, rule: CrawlRule, max_items: int) -> list[BidNotice]:
        cap = min(max_items, rule.limits.max_items)
        entry_url = rule.entry_url or self.site_config.get("url", "")

        if self._is_pure_api_list(rule):
            emit_crawl_event(
                "fetch_list",
                f"纯 HTTP API 抓取列表 (max_items={cap})",
                data={"url": rule.list_page.api.url if rule.list_page and rule.list_page.api else entry_url},
            )
            return await self._execute_pure_api(rule, cap)

        emit_crawl_event(
            "fetch_list",
            f"规则驱动抓取列表 (max_items={cap})",
            data={"url": entry_url},
        )

        notices: list[BidNotice] = []
        async with self.browser.new_page() as page:
            list_page = rule.list_page
            if list_page is None:
                return []

            pagination = rule.pagination
            use_network_capture = (
                list_page.strategy == "api"
                and list_page.api is not None
                and list_page.wait_network is not None
            )
            use_api = list_page.strategy == "api" or (
                pagination is not None and pagination.type == "ajax_param" and list_page.api
            )

            captured: list[Any] = []
            if use_network_capture and list_page.wait_network:
                pattern = list_page.wait_network.url_pattern

                async def _on_response(response: Any) -> None:
                    if response.status != 200 or pattern not in response.url:
                        return
                    try:
                        captured.append(await response.json())
                    except Exception:
                        pass

                page.on("response", _on_response)

            await self._run_entry_steps(page, rule, captured if use_network_capture else None)

            if use_network_capture:
                notices = await self._collect_network_capture(
                    page, rule, list_page, cap, captured
                )
            elif use_api and list_page.api:
                notices = await self._collect_api_pages(page, rule, list_page, pagination, cap)
            else:
                notices = await self._collect_dom_pages(page, rule, list_page, pagination, cap)

        final_notices = notices[:cap]
        if final_notices:
            from src.core.crawl_links import notice_to_link

            emit_crawl_event(
                "parse",
                f"规则抓取完成: {len(final_notices)} 条",
                data={
                    "count": len(final_notices),
                    "urls": [notice_to_link(n) for n in final_notices],
                },
            )
        logger.info("站点 %s 规则执行完成: %d 条", self.site_id, len(final_notices))
        return final_notices

    async def _execute_pure_api(self, rule: CrawlRule, cap: int) -> list[BidNotice]:
        list_page = rule.list_page
        if list_page is None or list_page.api is None:
            return []
        pagination = rule.pagination
        notices = await self._collect_api_pages_http(rule, list_page, pagination, cap)
        final_notices = notices[:cap]
        if final_notices:
            from src.core.crawl_links import notice_to_link

            emit_crawl_event(
                "parse",
                f"HTTP API 抓取完成: {len(final_notices)} 条",
                data={
                    "count": len(final_notices),
                    "urls": [notice_to_link(n) for n in final_notices],
                },
            )
        logger.info("站点 %s HTTP API 规则执行完成: %d 条", self.site_id, len(final_notices))
        return final_notices

    def _effective_max_pages(self, rule: CrawlRule) -> int:
        pag = rule.pagination
        pag_max = pag.max_pages if pag else rule.limits.max_pages
        return min(pag_max, rule.limits.max_pages)

    async def _rate_limit(self, rule: CrawlRule) -> None:
        delay = rule.limits.rate_limit_seconds
        if delay > 0:
            raise_if_sync_cancelled()
            await asyncio.sleep(delay)
            raise_if_sync_cancelled()

    async def _run_entry_steps(
        self, page: Any, rule: CrawlRule, captured: Optional[list[Any]] = None
    ) -> None:
        steps = list(rule.entry_steps)
        if not steps and rule.entry_url:
            steps = [EntryStep(action="navigate", url=rule.entry_url)]

        for idx, step in enumerate(steps, start=1):
            raise_if_sync_cancelled()
            emit_crawl_event(
                "rule_step",
                f"入口步骤 {idx}/{len(steps)}: {step.action}",
                data={"action": step.action, "url": step.url or page.url},
            )
            await self._execute_entry_step(page, step, rule, captured)

    async def _execute_entry_step(
        self,
        page: Any,
        step: EntryStep,
        rule: CrawlRule,
        captured: Optional[list[Any]] = None,
    ) -> None:
        if step.action == "navigate":
            url = step.url or rule.entry_url or self.site_config.get("url", "")
            emit_crawl_event("navigate", f"打开 {url}", data={"url": url})
            await page.goto(url, wait_until="networkidle", timeout=max(step.timeout_ms, 60000))
            return

        if step.action == "click_text":
            locator = page.get_by_text(step.text or "", exact=step.exact)
            await locator.first.click(timeout=step.timeout_ms)
            return

        if step.action == "click_selector":
            await page.click(step.selector or "", timeout=step.timeout_ms)
            return

        if step.action == "follow_link":
            if step.href_contains:
                link = page.locator(f"a[href*='{step.href_contains}']").first
            elif step.text:
                link = page.get_by_role("link", name=step.text, exact=step.exact)
            else:
                raise ValueError("follow_link 需要 href_contains 或 text")
            await link.click(timeout=step.timeout_ms)
            return

        if step.action == "wait":
            if step.selector:
                await page.wait_for_selector(step.selector, timeout=step.timeout_ms)
            else:
                await page.wait_for_timeout(step.timeout_ms)
            return

        if step.action == "wait_network":
            pattern = step.url_pattern or ""
            emit_crawl_event(
                "rule_step",
                f"等待网络请求: {pattern}",
                data={"url_pattern": pattern},
            )
            await self._wait_network_pattern(page, pattern, step.timeout_ms, captured)
            return

    async def _wait_network_pattern(
        self,
        page: Any,
        pattern: str,
        timeout_ms: int,
        captured: Optional[list[Any]] = None,
    ) -> None:
        """等待匹配 url_pattern 的 XHR；若已有 captured 缓冲则轮询。"""
        if captured is not None:
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                if captured:
                    return
                await asyncio.sleep(0.2)
            return

        try:
            async with page.expect_response(
                lambda r: pattern in r.url and r.status == 200,
                timeout=timeout_ms,
            ) as resp_info:
                await resp_info.value
        except Exception:
            logger.debug("wait_network 超时或未匹配: %s", pattern)

    async def _prepare_dom_list(self, page: Any, list_page: ListPage) -> None:
        if list_page.wait_network:
            wn = list_page.wait_network

            emit_crawl_event(
                "rule_step",
                f"等待 AJAX 列表: {wn.url_pattern}",
                data={"url_pattern": wn.url_pattern},
            )
            await self._wait_network_pattern(page, wn.url_pattern, wn.timeout_ms, None)

        if list_page.wait_for:
            await page.wait_for_selector(list_page.wait_for, timeout=15000)

    async def _collect_dom_pages(
        self,
        page: Any,
        rule: CrawlRule,
        list_page: ListPage,
        pagination: Optional[Pagination],
        cap: int,
    ) -> list[BidNotice]:
        max_pages = self._effective_max_pages(rule)
        notices: list[BidNotice] = []
        seen: set[str] = set()
        page_num = 1

        while page_num <= max_pages and len(notices) < cap:
            raise_if_sync_cancelled()
            if page_num == 1:
                await self._prepare_dom_list(page, list_page)
            elif pagination and pagination.type == "next_button":
                if await self._is_next_disabled(page, pagination):
                    break
                emit_crawl_event(
                    "paginate",
                    f"翻页 {page_num}/{max_pages}",
                    data={"page": page_num, "url": page.url},
                )
                await page.click(pagination.selector or "", timeout=10000)
                wait_ms = pagination.wait_after_ms
                if wait_ms > 0:
                    await page.wait_for_timeout(wait_ms)
                if list_page.wait_for:
                    try:
                        await page.wait_for_selector(list_page.wait_for, timeout=15000)
                    except Exception:
                        pass
            else:
                break

            batch = await self._parse_dom_list(page, list_page, rule)
            emit_crawl_event(
                "parse",
                f"解析第 {page_num} 页: {len(batch)} 条",
                data={"page": page_num, "count": len(batch), "url": page.url},
            )

            added = 0
            for notice in batch:
                raise_if_sync_cancelled()
                if notice.url in seen:
                    continue
                seen.add(notice.url)
                notices.append(notice)
                added += 1
                if len(notices) >= cap:
                    break

            if pagination is None or pagination.type != "next_button":
                break
            if added == 0 and pagination.stop_when_empty:
                break

            page_num += 1
            await self._rate_limit(rule)

        return notices

    async def _is_next_disabled(self, page: Any, pagination: Pagination) -> bool:
        if pagination.disabled_selector:
            disabled = await page.query_selector(pagination.disabled_selector)
            if disabled:
                return True
        if not pagination.selector:
            return True
        next_el = await page.query_selector(pagination.selector)
        if next_el is None:
            return True
        aria = await next_el.get_attribute("aria-disabled")
        if aria == "true":
            return True
        cls = await next_el.get_attribute("class") or ""
        return "disabled" in cls

    async def _parse_dom_list(
        self, page: Any, list_page: ListPage, rule: CrawlRule
    ) -> list[BidNotice]:
        """在浏览器内按 container/item 选择器解析列表。"""
        rows: list[dict[str, str]] = await page.evaluate(
            """([containerSel, itemSel, titleSel, linkSel, dateSel]) => {
                const root = containerSel
                    ? document.querySelector(containerSel)
                    : document;
                if (!root) return [];
                const items = itemSel ? root.querySelectorAll(itemSel) : [root];
                const out = [];
                for (const item of items) {
                    const titleEl = titleSel ? item.querySelector(titleSel) : null;
                    const linkEl = linkSel ? item.querySelector(linkSel) : null;
                    const dateEl = dateSel ? item.querySelector(dateSel) : null;
                    const title = (titleEl?.innerText || linkEl?.innerText || '').trim();
                    let href = linkEl?.getAttribute('href') || linkEl?.href || '';
                    if (!href && linkEl?.closest('a')) {
                        href = linkEl.closest('a').getAttribute('href') || '';
                    }
                    const dateText = (dateEl?.innerText || '').trim();
                    if (title && href) {
                        out.push({ title, href, dateText });
                    }
                }
                return out;
            }""",
            [
                list_page.container or "",
                list_page.item or "",
                list_page.title or "",
                list_page.link or "",
                list_page.date or "",
            ],
        )

        source_url = rule.entry_url or page.url
        notices: list[BidNotice] = []
        for row in rows:
            full_url = resolve_notice_detail_url(
                urljoin(page.url, row["href"]),
                base_url=source_url,
            )
            notices.append(
                BidNotice(
                    title=row["title"],
                    url=full_url,
                    source_site_id=self.site_id,
                    source_site_name=self.site_name,
                    source_url=source_url,
                    publish_date=_parse_date(row.get("dateText", "")),
                    category=_guess_category(row["title"]),
                )
            )
        return notices

    async def _collect_network_capture(
        self,
        page: Any,
        rule: CrawlRule,
        list_page: ListPage,
        cap: int,
        captured: list[Any],
    ) -> list[BidNotice]:
        """解析 entry 阶段已捕获的 XHR 响应体。"""
        api = list_page.api
        if api is None or list_page.wait_network is None:
            return []

        pattern = list_page.wait_network.url_pattern

        if not captured:
            emit_crawl_event(
                "rule_step",
                f"等待网络捕获: {pattern}",
                data={"url_pattern": pattern},
            )
            await self._wait_network_pattern(
                page, pattern, list_page.wait_network.timeout_ms, captured
            )
            await page.wait_for_timeout(1000)

        all_items: list[Any] = []
        for body in captured:
            items = _get_nested(body, api.items_path)
            if isinstance(items, list):
                all_items.extend(items)
            elif isinstance(items, dict):
                for key in ("records", "list", "rows", "items"):
                    nested = items.get(key)
                    if isinstance(nested, list):
                        all_items.extend(nested)

        emit_crawl_event(
            "parse",
            f"网络捕获解析: {len(all_items)} 条原始记录",
            data={"count": len(all_items), "url": page.url},
        )

        notices = self._api_items_to_notices(all_items, api, rule)
        return notices[:cap]

    async def _collect_api_pages(
        self,
        page: Any,
        rule: CrawlRule,
        list_page: ListPage,
        pagination: Optional[Pagination],
        cap: int,
    ) -> list[BidNotice]:
        return await self._collect_api_pages_impl(
            rule, list_page, pagination, cap, page=page
        )

    async def _collect_api_pages_http(
        self,
        rule: CrawlRule,
        list_page: ListPage,
        pagination: Optional[Pagination],
        cap: int,
    ) -> list[BidNotice]:
        return await self._collect_api_pages_impl(
            rule, list_page, pagination, cap, page=None
        )

    async def _collect_api_pages_impl(
        self,
        rule: CrawlRule,
        list_page: ListPage,
        pagination: Optional[Pagination],
        cap: int,
        *,
        page: Any | None,
    ) -> list[BidNotice]:
        api = list_page.api
        if api is None:
            return []

        max_pages = self._effective_max_pages(rule)
        page_param = (pagination.page_param if pagination else None) or api.page_param
        notices: list[BidNotice] = []
        seen: set[str] = set()
        transport = "HTTP" if page is None else "browser"

        for page_num in range(1, max_pages + 1):
            raise_if_sync_cancelled()
            if len(notices) >= cap:
                break

            emit_crawl_event(
                "paginate",
                f"API 分页 {page_num}/{max_pages} ({transport})",
                data={"page": page_num, "url": api.url},
            )

            items = await self._fetch_api_items(page, api, page_param, page_num, rule)
            batch = self._api_items_to_notices(items, api, rule)

            emit_crawl_event(
                "parse",
                f"解析 API 第 {page_num} 页: {len(batch)} 条",
                data={"page": page_num, "count": len(batch), "url": api.url},
            )

            added = 0
            for notice in batch:
                raise_if_sync_cancelled()
                if notice.url in seen:
                    continue
                seen.add(notice.url)
                notices.append(notice)
                added += 1
                if len(notices) >= cap:
                    break

            stop_empty = pagination.stop_when_empty if pagination else True
            if added == 0 and stop_empty:
                break

            # 纯 HTTP API 模式也支持 page_number / ajax_param 翻页（多页循环）
            if page is None:
                if pagination is None or pagination.type not in ("ajax_param", "page_number"):
                    break
            elif pagination is None or pagination.type != "ajax_param":
                break

            if len(notices) >= cap:
                break

            await self._rate_limit(rule)

        return notices

    async def _fetch_api_items(
        self,
        page: Any | None,
        api: ListPageApi,
        page_param: str,
        page_num: int,
        rule: Optional[CrawlRule] = None,
    ) -> list[Any]:
        entry_url = (rule.entry_url if rule else None) or self.site_config.get("url", "")
        site_url = self.site_config.get("url", "")

        try:
            if page is None:
                body = await fetch_api_json(
                    api,
                    page_param=page_param,
                    page_num=page_num,
                    site_url=site_url,
                    entry_url=entry_url,
                )
            else:
                params = {**api.params, page_param: page_num}
                if api.method == "POST" and "time" not in params:
                    params["time"] = int(time.time() * 1000)
                api_url = api.url if api.url.startswith("http") else urljoin(page.url, api.url)

                if api.method == "GET":
                    response = await page.request.get(api_url, params=params)
                elif api.body_format == "json":
                    import json

                    response = await page.request.post(
                        api_url,
                        data=json.dumps(params, ensure_ascii=False),
                        headers={"Content-Type": "application/json;charset=utf-8"},
                    )
                else:
                    response = await page.request.post(api_url, data=params)

                if not response.ok:
                    logger.warning("API 请求失败 %s status=%s", api_url, response.status)
                    return []
                try:
                    body = await response.json()
                except Exception as e:
                    logger.warning("API 响应非 JSON: %s", e)
                    return []
        except Exception as exc:
            logger.warning("API 请求异常 page=%s url=%s: %s", page_num, api.url, exc)
            return []

        items = _get_nested(body, api.items_path)
        if isinstance(items, list):
            return items
        if isinstance(items, dict):
            for key in ("records", "list", "rows", "items"):
                nested = items.get(key)
                if isinstance(nested, list):
                    return nested
        return []

    def _api_items_to_notices(
        self, items: list[Any], api: ListPageApi, rule: CrawlRule
    ) -> list[BidNotice]:
        fields = api.fields or {}
        title_key = fields.get("title", "title")
        url_key = fields.get("url", "url")
        id_key = fields.get("id", "id")
        date_key = fields.get("date", "date")
        source_url = rule.entry_url or self.site_config.get("url", "")

        notices: list[BidNotice] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            title = str(_get_field(row, title_key) or "").strip()
            if not title:
                continue

            url_val = _get_field(row, url_key) if url_key in fields else None
            row_id = _get_field(row, id_key)
            if api.url_template:
                ctx: dict[str, Any] = {"id": row_id}
                for alias, spec in fields.items():
                    ctx[alias] = _get_field(row, spec)
                try:
                    url = api.url_template.format(**ctx)
                except KeyError:
                    url = str(url_val or row_id or "")
            elif url_val:
                url = str(url_val)
                if url.startswith("/") or not url.startswith("http"):
                    url = urljoin(source_url, url)
            elif row_id:
                url = f"{source_url}?id={row_id}"
            else:
                continue

            date_text = str(_get_field(row, date_key) or "")
            notices.append(
                BidNotice(
                    title=title,
                    url=url,
                    source_site_id=self.site_id,
                    source_site_name=self.site_name,
                    source_url=source_url,
                    publish_date=_parse_date(date_text),
                    category=_guess_category(title),
                )
            )
        return notices

    # ── Agent skill 单步能力（不跑整站 sync）────────────────────────

    async def skill_navigate_entry(self, page: Any, rule: CrawlRule) -> str:
        """打开入口并等待列表就绪，返回当前 URL。"""
        await self._run_entry_steps(page, rule, None)
        list_page = rule.list_page
        if list_page is not None:
            await self._prepare_dom_list(page, list_page)
        return page.url

    async def skill_parse_list(self, page: Any, rule: CrawlRule) -> list[BidNotice]:
        """解析当前页 DOM 列表项。"""
        list_page = rule.list_page
        if list_page is None:
            return []
        return await self._parse_dom_list(page, list_page, rule)

    async def skill_has_next_page(self, page: Any, rule: CrawlRule) -> bool:
        """是否还有下一页（next_button 策略）。"""
        pagination = rule.pagination
        if pagination is None or pagination.type != "next_button":
            return False
        return not await self._is_next_disabled(page, pagination)

    async def skill_click_next_page(self, page: Any, rule: CrawlRule) -> tuple[str, bool]:
        """点击下一页，返回 (当前 URL, 是否仍有下一页)。"""
        pagination = rule.pagination
        if pagination is None or pagination.type != "next_button":
            return page.url, False
        if await self._is_next_disabled(page, pagination):
            return page.url, False
        emit_crawl_event(
            "paginate",
            "Agent skill 翻页",
            data={"url": page.url},
        )
        await page.click(pagination.selector or "", timeout=10000)
        wait_ms = pagination.wait_after_ms
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        list_page = rule.list_page
        if list_page and list_page.wait_for:
            try:
                await page.wait_for_selector(list_page.wait_for, timeout=15000)
            except Exception:
                pass
        await self._rate_limit(rule)
        has_next = await self.skill_has_next_page(page, rule)
        return page.url, has_next
