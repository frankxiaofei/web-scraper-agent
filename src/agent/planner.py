"""AgentPlanner: 分析站点结构并生成 CrawlPlan。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlparse

from src.agent.events import ANALYZE, PAGE_LOAD, PLAN_START, make_event
from src.agent.models import AgentEvent, CrawlPlan, ListStrategy
from src.core.browser import BrowserPool
from src.core.scheduler import ADAPTER_REGISTRY

logger = logging.getLogger(__name__)

_EXTRACT_SUMMARY_JS = """() => {
    const links = [];
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href;
        if (!href || href.startsWith('javascript:')) continue;
        links.push({ url: href, text: (a.innerText || '').trim().slice(0, 100) });
    }
    const bodyText = document.body ? document.body.innerText.slice(0, 4000) : '';
    const html = document.documentElement ? document.documentElement.outerHTML.slice(0, 20000) : '';
    const tables = document.querySelectorAll('table').length;
    const listItems = document.querySelectorAll('ul li, .list-item, .news-list li').length;
    const pagination = links.filter(l =>
        /下一页|下页|next|»|>>|后页|more/i.test(l.text) || /page=|index_\\d|list_\\d|PageNo=/i.test(l.url)
    );
    return { links: links.slice(0, 60), bodyText, html, tables, listItems, pagination, title: document.title };
}"""

_PLAN_PROMPT = """你是招标/采购网站爬取专家。分析以下页面信息，输出 JSON 爬取计划。

站点: {site_name} ({site_id})
首页 URL: {base_url}
页面标题: {title}
正文摘要: {body_preview}
链接样本: {links_sample}
XHR/API 请求: {xhr_sample}
已有适配器: {adapter_info}

请输出合法 JSON（不要 markdown）:
{{
  "list_url": "招标公告列表页 URL（若与首页相同则填首页）",
  "list_strategy": "dom|api|spa",
  "selectors": {{"list_item": "CSS选择器", "title": "...", "link": "a", "pagination": "..."}},
  "api_endpoint": "若列表通过 API 加载则填 URL，否则 null",
  "detail_pattern": "详情页 URL 正则模式，如 /detail/|/view\\.jsp",
  "pagination_hint": "翻页方式描述",
  "suggested_max_depth": 2,
  "reasoning": "简要分析说明（中文）"
}}"""


class AgentPlanner:
    """给定站点 URL，Playwright 打开首页，LLM 分析 HTML 结构，输出 CrawlPlan。"""

    def __init__(
        self,
        browser: BrowserPool,
        *,
        goto_timeout_ms: int = 30_000,
        emit: Optional[Callable[[AgentEvent], None]] = None,
    ) -> None:
        self.browser = browser
        self.goto_timeout_ms = goto_timeout_ms
        self._emit = emit or (lambda e: None)

    def _emit_event(self, event: AgentEvent) -> None:
        self._emit(event)

    async def plan(
        self,
        site_id: str,
        site_name: str,
        base_url: str,
        *,
        adapter_name: Optional[str] = None,
    ) -> CrawlPlan:
        """分析站点并生成爬取计划。"""
        self._emit_event(make_event(PLAN_START, f"开始分析站点: {site_name} ({site_id})", level="info"))

        has_adapter = adapter_name and adapter_name != "generic" and adapter_name in ADAPTER_REGISTRY
        if has_adapter:
            self._emit_event(
                make_event(
                    ANALYZE,
                    f"检测到已注册适配器「{adapter_name}」，将优先使用适配器爬取",
                    level="success",
                    data={"adapter": adapter_name},
                )
            )
            return CrawlPlan(
                site_id=site_id,
                site_name=site_name,
                base_url=base_url,
                list_url=base_url,
                list_strategy=ListStrategy.ADAPTER,
                use_existing_adapter=True,
                adapter_name=adapter_name,
                suggested_max_depth=3,
                reasoning=f"使用已有适配器 {adapter_name} 进行爬取",
            )

        summary, xhr_logs = await self._fetch_page_summary(base_url)
        self._emit_event(
            make_event(
                ANALYZE,
                f"首页分析完成: 链接 {len(summary.get('links', []))} 条, XHR {len(xhr_logs)} 个",
                level="info",
                data={"title": summary.get("title"), "xhr_count": len(xhr_logs)},
            )
        )

        llm_plan = self._call_llm_plan(
            site_id=site_id,
            site_name=site_name,
            base_url=base_url,
            summary=summary,
            xhr_logs=xhr_logs,
            adapter_info="无（将生成动态计划）",
        )
        if llm_plan:
            self._emit_event(
                make_event(
                    ANALYZE,
                    f"LLM 分析完成: 策略={llm_plan.list_strategy.value}, 列表={llm_plan.list_url}",
                    level="success",
                    data={"plan": llm_plan.model_dump(mode="json")},
                )
            )
            return llm_plan

        self._emit_event(make_event(ANALYZE, "LLM 不可用，使用规则回退计划", level="warn"))
        return self._fallback_plan(site_id, site_name, base_url, summary)

    async def _fetch_page_summary(self, url: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
        xhr_logs: list[dict[str, str]] = []

        async with self.browser.new_page() as page:
            def on_response(response) -> None:
                try:
                    req_url = response.url
                    ct = response.headers.get("content-type", "")
                    if response.request.resource_type in ("xhr", "fetch") or "json" in ct:
                        xhr_logs.append({
                            "url": req_url[:300],
                            "method": response.request.method,
                            "status": str(response.status),
                            "content_type": ct[:80],
                        })
                except Exception:
                    pass

            page.on("response", on_response)
            self._emit_event(make_event(PAGE_LOAD, f"正在加载首页: {url}", level="info"))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.goto_timeout_ms)
                await page.wait_for_timeout(1500)
                summary = await page.evaluate(_EXTRACT_SUMMARY_JS)
            except Exception as e:
                logger.warning("首页加载失败 %s: %s", url, e)
                self._emit_event(make_event(PAGE_LOAD, f"首页加载失败: {e}", level="error"))
                summary = {"links": [], "bodyText": "", "html": "", "title": "", "pagination": []}
            return summary, xhr_logs[:20]

    def _call_llm_plan(
        self,
        *,
        site_id: str,
        site_name: str,
        base_url: str,
        summary: dict[str, Any],
        xhr_logs: list[dict[str, str]],
        adapter_info: str,
    ) -> Optional[CrawlPlan]:
        from src.core.config import get_settings

        settings = get_settings()
        api_key = settings.openai_api_key
        if not api_key:
            return None

        base_llm_url = (settings.llm_base_url or "https://api.openai.com").rstrip("/")
        model = settings.llm_model

        links_sample = [
            {"url": l.get("url", ""), "text": l.get("text", "")[:60]}
            for l in summary.get("links", [])[:30]
        ]
        prompt = _PLAN_PROMPT.format(
            site_name=site_name,
            site_id=site_id,
            base_url=base_url,
            title=summary.get("title", "")[:200],
            body_preview=(summary.get("bodyText") or "")[:2000],
            links_sample=json.dumps(links_sample, ensure_ascii=False),
            xhr_sample=json.dumps(xhr_logs[:10], ensure_ascii=False),
            adapter_info=adapter_info,
        )

        try:
            import urllib.error
            import urllib.request

            body = json.dumps(
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是招标网站爬取助手，只输出合法 JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                },
                ensure_ascii=False,
            ).encode("utf-8")

            req = urllib.request.Request(
                f"{base_llm_url}/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"].strip()
            if "```" in content:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    content = content[start:end]
            data = json.loads(content)
            strategy_str = str(data.get("list_strategy", "dom")).lower()
            strategy = ListStrategy.DOM
            if strategy_str == "api":
                strategy = ListStrategy.API
            elif strategy_str == "spa":
                strategy = ListStrategy.SPA

            return CrawlPlan(
                site_id=site_id,
                site_name=site_name,
                base_url=base_url,
                list_url=data.get("list_url") or base_url,
                list_strategy=strategy,
                selectors=data.get("selectors"),
                api_endpoint=data.get("api_endpoint"),
                detail_pattern=data.get("detail_pattern"),
                pagination_hint=data.get("pagination_hint"),
                suggested_max_depth=int(data.get("suggested_max_depth", 2)),
                reasoning=data.get("reasoning"),
            )
        except Exception as e:
            logger.warning("LLM 计划生成失败: %s", e)
            self._emit_event(make_event(ANALYZE, f"LLM 分析失败: {e}", level="warn"))
            return None

    def _fallback_plan(
        self,
        site_id: str,
        site_name: str,
        base_url: str,
        summary: dict[str, Any],
    ) -> CrawlPlan:
        """规则回退：从链接中猜测列表页。"""
        list_url = base_url
        links = summary.get("links", [])
        list_keywords = re.compile(r"招标|采购|公告|中标|成交|通知|列表|信息", re.I)
        for link in links:
            text = link.get("text", "")
            href = link.get("url", "")
            if list_keywords.search(text) and href:
                list_url = href
                break

        pagination = summary.get("pagination", [])
        pagination_hint = "未检测到翻页" if not pagination else f"检测到 {len(pagination)} 个翻页链接"

        return CrawlPlan(
            site_id=site_id,
            site_name=site_name,
            base_url=base_url,
            list_url=list_url,
            list_strategy=ListStrategy.DOM,
            selectors={
                "list_item": "ul li a, table tr a, .list-item a, .news-list a",
                "title": "a",
                "link": "a",
                "pagination": "a",
            },
            detail_pattern=r"/detail/|/view\.|/info/|\.html",
            pagination_hint=pagination_hint,
            suggested_max_depth=2,
            reasoning="规则回退：根据链接关键词猜测列表页",
        )
