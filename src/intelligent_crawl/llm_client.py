"""可选 LLM 语义决策客户端（失败时回退规则层）。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from src.core.models import PageType
from src.intelligent_crawl.models import CrawlDecision, NextUrl, PageSemantics, SiteContext

logger = logging.getLogger(__name__)

_PAGE_TYPE_MAP = {
    "list": PageType.LIST,
    "detail": PageType.DETAIL,
    "attachment": PageType.ATTACHMENT,
    "index": PageType.INDEX,
    "unknown": PageType.UNKNOWN,
}


class LLMCrawlClient:
    """OpenAI 兼容 API 客户端，用于页面递归决策。"""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def decide(
        self,
        semantics: PageSemantics,
        *,
        depth: int,
        site_context: SiteContext,
    ) -> Optional[CrawlDecision]:
        """调用 LLM 返回 CrawlDecision；失败返回 None 以触发规则回退。"""
        if not self.available:
            return None
        try:
            return self._call_api(semantics, depth=depth, site_context=site_context)
        except Exception as e:
            logger.warning("LLM 语义决策失败，回退规则层: %s", e)
            return None

    def _call_api(
        self,
        semantics: PageSemantics,
        *,
        depth: int,
        site_context: SiteContext,
    ) -> Optional[CrawlDecision]:
        import urllib.error
        import urllib.request

        link_sample = [
            {"url": l.get("url", ""), "text": l.get("text", "")[:80]}
            for l in semantics.links[:40]
        ]
        prompt = {
            "url": semantics.url,
            "title": semantics.title[:200],
            "body_preview": semantics.body_text[:1500],
            "depth": depth,
            "site_id": site_context.site_id,
            "links": link_sample,
            "task": (
                "判断页面类型(list/detail/attachment/index/unknown)，"
                "是否继续递归(should_recurse)，以及 next_urls 列表。"
                "next_urls 每项含 url, reason, priority(1-10，越小越优先)。"
                "仅返回 JSON: {page_type, should_recurse, next_urls}"
            ),
        }
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你是招标网站爬取助手，只输出合法 JSON。"},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "temperature": 0.1,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        url = f"{self.base_url}/v1/chat/completions" if self.base_url else "https://api.openai.com/v1/chat/completions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning("LLM HTTP 错误 %s: %s", e.code, e.read().decode("utf-8", errors="replace")[:200])
            return None

        content = payload["choices"][0]["message"]["content"]
        # 提取 JSON 块
        text = content.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        data: dict[str, Any] = json.loads(text)
        page_type = _PAGE_TYPE_MAP.get(str(data.get("page_type", "unknown")).lower(), PageType.UNKNOWN)
        next_urls = [
            NextUrl(
                url=item["url"],
                reason=item.get("reason", "llm"),
                priority=int(item.get("priority", 5)),
            )
            for item in data.get("next_urls", [])
            if item.get("url")
        ]
        return CrawlDecision(
            page_type=page_type,
            should_recurse=bool(data.get("should_recurse")),
            next_urls=next_urls,
        )


def create_llm_client() -> Optional[LLMCrawlClient]:
    """根据配置（.env / 环境变量）创建 LLM 客户端。"""
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.intelligent_crawl_llm:
        return None
    client = LLMCrawlClient(
        api_key=settings.openai_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    return client if client.available else None
