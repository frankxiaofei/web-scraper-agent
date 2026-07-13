"""AI 对话生成 crawl rules YAML（L2 声明式规则）。"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx
import yaml
from pydantic import ValidationError

from src.core.crawl_rules import CrawlRule, get_rule_loader
from src.core.llm_chat import LLMChatClient, create_chat_client
from src.core.recording_converter import format_recording_for_llm, recording_to_yaml_stub
from src.core.site_aliases import resolve_canonical_site_id
from src.core.site_sync import get_site_by_id

logger = logging.getLogger(__name__)

_ZYCG_EXAMPLE = """# 中央政府采购网示例片段
version: 1
site_id: zycg_national
name: 中央政府采购网
enabled: true
entry_url: https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/cggg/index.html
list_page:
  strategy: dom_after_ajax
  container: "#noticeShow"
  item: "li"
  title: "a.titleHiding span"
  link: "a.titleHiding"
  wait_network:
    url_pattern: "selectInfoMore.do"
    timeout_ms: 15000
  api:
    url: https://www.zycg.gov.cn/freecms/rest/v1/notice/selectInfoMore.do
    method: GET
    params:
      siteId: "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d"
      channel: "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5"
      pageSize: 15
    page_param: currPage
    items_path: data
    total_path: total
    fields:
      title: title
      url: pageurl
      id: id
      date: addtimeStr
    url_template: "{url}?id={id}"
pagination:
  type: ajax_param
  page_param: currPage
  page_size: 15
  max_pages: 20
  stop_when_empty: true
detail:
  fetch_detail: false
  url_pattern: "/ggxx/info/|/tzgg/info/"
limits:
  max_pages: 20
  max_items: 100
  max_depth: 2
  rate_limit_seconds: 1.0"""

_SYSTEM_PROMPT = f"""你是招标/采购网站深度爬取规则专家。根据用户描述和页面结构提示，生成符合 CrawlRule v1 schema 的 YAML 规则文件。

## Schema 说明

顶层字段：
- version: 1（固定）
- site_id: 站点 ID（必须与用户指定一致）
- name: 站点中文名
- enabled: true/false
- entry_url: 列表入口 URL
- entry_steps: 从首页到列表的导航步骤（可选数组）
- list_page: 列表页提取配置
- pagination: 分页配置
- detail: 详情页配置
- limits: 抓取限制

### entry_steps action 类型
- navigate: url
- click_text: text, exact?
- click_selector: selector
- follow_link: href_contains / text
- wait: selector, timeout_ms
- wait_network: url_pattern, timeout_ms

### list_page
- strategy: dom | api | dom_after_ajax
- container, item, title, link, date: CSS 选择器
- wait_for: 等待选择器
- wait_network: url_pattern, timeout_ms
- api: url, method, params, page_param, items_path, total_path, fields, url_template

### pagination type
- next_button: selector, disabled_selector?, wait_after_ms
- page_number: page_param
- load_more: selector
- ajax_param: page_param, page_size, max_pages, stop_when_empty, fallback

### detail
- fetch_detail: bool
- url_pattern: 正则
- content_selector: CSS

### limits
- max_pages, max_items, max_depth, rate_limit_seconds

## 参考示例（zycg_national）

```yaml
{_ZYCG_EXAMPLE}
```

## 输出要求
1. 只输出纯 YAML，不要 markdown 代码块、不要 JSON、不要解释文字
2. site_id 必须与用户指定一致
3. 根据用户描述合理推断选择器、API、分页方式
4. 用户要求修改时，在原有 YAML 基础上调整并输出完整文件"""


async def fetch_page_hints(url: str, *, timeout: float = 15.0) -> str:
    """轻量 HTTP 抓取页面结构提示，失败返回空字符串。"""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; web_scraper/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text[:50000]
    except Exception as e:
        logger.info("页面抓取失败 %s: %s", url, e)
        return ""

    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    title = title_m.group(1).strip() if title_m else ""

    links: list[str] = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{0,80})', html, re.I):
        href, text = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        if href.startswith("javascript:"):
            continue
        links.append(f"{text[:40]} → {href[:120]}")
        if len(links) >= 25:
            break

    ajax_hints = list(dict.fromkeys(re.findall(r"[\w/]+\.(?:do|json|api)[\w/?=&.-]*", html)))[:15]
    ids = list(dict.fromkeys(re.findall(r'id=["\']([^"\']+)["\']', html)))[:20]
    classes = list(dict.fromkeys(re.findall(r'class=["\']([^"\']+)["\']', html)))[:15]

    parts = [f"页面标题: {title}", f"链接样本 ({len(links)} 条):"]
    parts.extend(f"  - {ln}" for ln in links[:15])
    if ajax_hints:
        parts.append(f"疑似 API/XHR 路径: {', '.join(ajax_hints[:10])}")
    if ids:
        parts.append(f"常见 id: {', '.join(ids[:12])}")
    if classes:
        parts.append(f"常见 class: {', '.join(c[:60] for c in classes[:8])}")
    return "\n".join(parts)


def _strip_yaml_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def validate_yaml(yaml_text: str, *, site_id: Optional[str] = None) -> dict[str, Any]:
    """解析并 Pydantic 校验 YAML，返回 {valid, errors, preview}。"""
    errors: list[str] = []
    preview: Optional[dict[str, Any]] = None

    try:
        raw = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        return {"valid": False, "errors": [f"YAML 语法错误: {e}"], "preview": None}

    if not isinstance(raw, dict):
        return {"valid": False, "errors": ["根节点必须是 YAML 对象"], "preview": None}

    try:
        rule = CrawlRule.model_validate(raw)
        if site_id and rule.site_id != site_id:
            errors.append(f"site_id 不匹配: YAML 为 {rule.site_id}，期望 {site_id}")
        preview = rule.model_dump(mode="json")
        return {"valid": len(errors) == 0, "errors": errors, "preview": preview}
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            errors.append(f"{loc}: {err.get('msg', '校验失败')}")
        return {"valid": False, "errors": errors, "preview": None}


def save_rule_yaml(site_id: str, yaml_text: str) -> dict[str, Any]:
    """校验后写入 config/crawl_rules/{site_id}.yaml。"""
    result = validate_yaml(yaml_text, site_id=site_id)
    if not result["valid"]:
        return {"ok": False, "errors": result["errors"]}

    loader = get_rule_loader()
    path = loader.path_for(site_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def load_rule_yaml(site_id: str) -> Optional[str]:
    loader = get_rule_loader()
    path = loader.path_for(resolve_canonical_site_id(site_id))
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


class RuleGenerator:
    """多轮对话生成 crawl rules YAML。"""

    def __init__(self, llm: Optional[LLMChatClient] = None) -> None:
        self.llm = llm or create_chat_client()

    async def generate(
        self,
        *,
        site_id: str,
        url: str,
        messages: list[dict[str, str]],
        fetch_hints: bool = True,
        page_hints: str = "",
    ) -> dict[str, Any]:
        """生成 YAML 并校验，失败时自修正一次。"""
        if not self.llm.available:
            return {
                "yaml": "",
                "valid": False,
                "errors": ["未配置 OPENAI_API_KEY，无法调用 LLM"],
                "preview": None,
                "llm_error": "未配置 OPENAI_API_KEY",
            }

        site = get_site_by_id(site_id)
        site_name = (site or {}).get("name", site_id)

        hints_text = (page_hints or "").strip()
        if not hints_text and fetch_hints and url:
            hints_text = await fetch_page_hints(url)

        context_block = (
            f"站点 ID: {site_id}\n"
            f"站点名称: {site_name}\n"
            f"目标 URL: {url}\n"
        )
        if hints_text:
            context_block += f"\n页面结构提示:\n{hints_text}\n"

        llm_messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": context_block + "\n请根据以上信息生成初始爬取规则 YAML。"},
        ]
        for msg in messages:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            llm_messages.append({"role": role, "content": msg.get("content", "")})

        yaml_text, llm_error = self._call_llm(llm_messages)
        if llm_error:
            return {
                "yaml": "",
                "valid": False,
                "errors": [llm_error],
                "preview": None,
                "llm_error": llm_error,
            }

        yaml_text = _strip_yaml_fences(yaml_text or "")
        result = validate_yaml(yaml_text, site_id=site_id)

        if not result["valid"]:
            fix_messages = llm_messages + [
                {"role": "assistant", "content": yaml_text},
                {
                    "role": "user",
                    "content": (
                        "上述 YAML 校验失败，请修正后重新输出完整 YAML（仅 YAML，无 markdown）：\n"
                        + "\n".join(result["errors"])
                    ),
                },
            ]
            fixed_yaml, fix_error = self._call_llm(fix_messages)
            if fix_error:
                return {
                    "yaml": yaml_text,
                    "valid": False,
                    "errors": result["errors"] + [f"自修正失败: {fix_error}"],
                    "preview": result.get("preview"),
                    "llm_error": fix_error,
                }
            yaml_text = _strip_yaml_fences(fixed_yaml or yaml_text)
            result = validate_yaml(yaml_text, site_id=site_id)

        return {
            "yaml": yaml_text,
            "valid": result["valid"],
            "errors": result["errors"] if not result["valid"] else [],
            "preview": result.get("preview"),
        }

    async def generate_from_recording(
        self,
        *,
        site_id: str,
        url: str,
        recording: dict[str, Any],
        fetch_hints: bool = True,
        prefer_llm: bool = True,
    ) -> dict[str, Any]:
        """从 Chrome 扩展录制的步骤 JSON 生成 YAML；无 LLM 时回退 stub 直转。"""
        recording_context = format_recording_for_llm(recording)
        entry_url = url or recording.get("entry_url") or ""

        if prefer_llm and self.llm.available:
            user_content = (
                "以下为用户在浏览器中录制的爬取操作步骤（JSON 摘要）。"
                "请据此生成完整的 CrawlRule v1 YAML，合理推断 list_page、pagination、entry_steps。\n\n"
                + recording_context
            )
            result = await self.generate(
                site_id=site_id,
                url=entry_url,
                messages=[{"role": "user", "content": user_content}],
                fetch_hints=fetch_hints,
            )
            result["source"] = "llm"
            return result

        yaml_text = recording_to_yaml_stub(recording, site_id=site_id)
        result = validate_yaml(yaml_text, site_id=site_id)
        return {
            "yaml": yaml_text,
            "valid": result["valid"],
            "errors": result["errors"] if not result["valid"] else [],
            "preview": result.get("preview"),
            "source": "stub",
            "llm_error": None if self.llm.available else "未配置 OPENAI_API_KEY，已使用 stub 直转",
        }

    def _call_llm(self, messages: list[dict[str, str]]) -> tuple[Optional[str], Optional[str]]:
        return self.llm.chat(messages, temperature=0.1, timeout=90)


_default_generator: Optional[RuleGenerator] = None


def get_rule_generator() -> RuleGenerator:
    global _default_generator
    if _default_generator is None:
        _default_generator = RuleGenerator()
    return _default_generator
