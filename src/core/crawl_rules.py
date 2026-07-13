"""深度爬取规则加载与校验（L2 declarative crawl rules）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_DIR = ROOT / "config" / "crawl_rules"


class EntryStep(BaseModel):
    """入口导航单步。"""

    action: Literal[
        "navigate",
        "click_text",
        "click_selector",
        "follow_link",
        "wait",
        "wait_network",
    ]
    url: Optional[str] = None
    text: Optional[str] = None
    exact: bool = False
    selector: Optional[str] = None
    href_contains: Optional[str] = None
    timeout_ms: int = 15000
    url_pattern: Optional[str] = None


class ListPageApi(BaseModel):
    """REST 列表 API 配置。"""

    url: str
    method: Literal["GET", "POST"] = "GET"
    body_format: Literal["form", "json"] = "form"
    params: dict[str, Any] = Field(default_factory=dict)
    page_param: str = "currPage"
    items_path: str = "data"
    total_path: str = "total"
    fields: dict[str, str] = Field(default_factory=dict)
    url_template: Optional[str] = None
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="可选：覆盖/补充 Referer、Origin 等（如 IP 封锁站需固定 Origin）",
    )


class ListPageWaitNetwork(BaseModel):
    url_pattern: str
    timeout_ms: int = 15000


class ListPage(BaseModel):
    strategy: Literal["dom", "api", "dom_after_ajax"] = "dom"
    container: Optional[str] = None
    item: Optional[str] = None
    title: Optional[str] = None
    link: Optional[str] = None
    date: Optional[str] = None
    wait_for: Optional[str] = None
    wait_network: Optional[ListPageWaitNetwork] = None
    api: Optional[ListPageApi] = None


class PaginationFallback(BaseModel):
    type: Literal["next_button", "page_number", "load_more"] = "next_button"
    selector: str
    disabled_selector: Optional[str] = None
    wait_after_ms: int = 500


class Pagination(BaseModel):
    type: Literal["next_button", "page_number", "load_more", "ajax_param"] = "next_button"
    selector: Optional[str] = None
    disabled_selector: Optional[str] = None
    page_param: Optional[str] = None
    url_style: Literal["query", "path_suffix"] = "query"
    page_size: int = 15
    max_pages: int = 20
    stop_when_empty: bool = True
    wait_after_ms: int = 500
    fallback: Optional[PaginationFallback] = None


class DetailRule(BaseModel):
    fetch_detail: bool = False
    strategy: Literal["dom", "api"] = "dom"
    url_pattern: Optional[str] = None
    content_selector: Optional[str] = None
    title_selector: Optional[str] = None
    wait_for: Optional[str] = None
    meta_selector: Optional[str] = None
    iframe_selector: Optional[str] = None
    api_base_url: Optional[str] = None
    api_path_template: Optional[str] = None
    api_content_field: Optional[str] = None
    api_title_field: Optional[str] = "title"
    api_id_param: Optional[str] = "id"


class CrawlLimits(BaseModel):
    max_pages: int = 20
    max_items: int = 100
    max_depth: int = 2
    rate_limit_seconds: float = 0.0


class CrawlRule(BaseModel):
    """站点深度爬取规则（config/crawl_rules/{site_id}.yaml）。"""

    version: int = 1
    site_id: str
    name: str = ""
    enabled: bool = True
    entry_url: Optional[str] = None
    entry_steps: list[EntryStep] = Field(default_factory=list)
    list_page: Optional[ListPage] = None
    pagination: Optional[Pagination] = None
    detail: Optional[DetailRule] = None
    limits: CrawlLimits = Field(default_factory=CrawlLimits)


class RuleLoader:
    """从 config/crawl_rules/ 或 sites.yaml 内嵌加载规则。"""

    def __init__(self, rules_dir: Path | None = None) -> None:
        self.rules_dir = rules_dir or DEFAULT_RULES_DIR

    def path_for(self, site_id: str) -> Path:
        return self.rules_dir / f"{site_id}.yaml"

    def load_file(self, site_id: str) -> Optional[CrawlRule]:
        path = self.path_for(site_id)
        if not path.is_file():
            return None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            rule = CrawlRule.model_validate(raw)
            if rule.site_id != site_id:
                logger.warning("规则 site_id 不匹配: 文件 %s, 期望 %s", rule.site_id, site_id)
            return rule if rule.enabled else None
        except Exception as e:
            logger.error("加载爬取规则失败 %s: %s", path, e)
            return None

    def load_for_site(self, site_config: dict[str, Any]) -> Optional[CrawlRule]:
        """合并 sites.yaml 单站配置与外部规则文件。"""
        site_id = site_config.get("id", "")
        if not site_id:
            return None

        rule: Optional[CrawlRule] = None

        # sites.yaml 内嵌 crawl_rules 对象
        embedded = site_config.get("crawl_rules")
        if isinstance(embedded, dict):
            try:
                rule = CrawlRule.model_validate(embedded)
            except Exception as e:
                logger.warning("站点 %s 内嵌 crawl_rules 无效: %s", site_id, e)
        elif isinstance(embedded, str):
            # crawl_rules: zycg_national → 加载同名文件
            rule = self.load_file(embedded)
        else:
            rule = self.load_file(site_id)

        if rule is None:
            return None

        return self._merge_site_config(rule, site_config)

    @staticmethod
    def _merge_site_config(rule: CrawlRule, site_config: dict[str, Any]) -> CrawlRule:
        """L1 sites.yaml 字段覆盖 limits / detail。"""
        limits = rule.limits.model_copy()
        if "max_items" in site_config:
            limits.max_items = int(site_config["max_items"])
        if "max_crawl_depth" in site_config:
            limits.max_depth = int(site_config["max_crawl_depth"])
        # min_delay_seconds 仅用于 BaseAdapter 请求前等待，不覆盖 YAML rate_limit_seconds

        detail = rule.detail.model_copy() if rule.detail else DetailRule()
        if "fetch_detail" in site_config:
            detail.fetch_detail = bool(site_config["fetch_detail"])

        entry_url = rule.entry_url
        override = site_config.get("agri_entry_url_override")
        if override:
            entry_url = str(override)
        elif not entry_url and site_config.get("url"):
            entry_url = site_config["url"]

        return rule.model_copy(update={"limits": limits, "detail": detail, "entry_url": entry_url})


_default_loader: Optional[RuleLoader] = None


def get_rule_loader() -> RuleLoader:
    global _default_loader
    if _default_loader is None:
        _default_loader = RuleLoader()
    return _default_loader


def build_page_number_url(
    base_url: str,
    page_num: int,
    *,
    page_param: Optional[str] = "page",
    url_style: str = "query",
) -> str:
    """根据分页策略构造列表页 URL（query 或 path 后缀，如 dlzb /v1/2/）。"""
    parsed = urlparse(base_url)
    if url_style == "path_suffix":
        path = parsed.path or "/"
        path = path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if parts and parts[-1].isdigit():
            parts = parts[:-1]
        base_path = "/" + "/".join(parts) if parts else "/"
        if not base_path.endswith("/"):
            base_path += "/"
        if page_num <= 1:
            return urlunparse(parsed._replace(path=base_path, query=""))
        new_path = base_path.rstrip("/") + f"/{page_num}/"
        return urlunparse(parsed._replace(path=new_path, query=""))

    param = page_param or "page"
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs[param] = [str(page_num)]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))


def load_crawl_rule(site_config: dict[str, Any]) -> Optional[CrawlRule]:
    """便捷函数：为 adapter 加载有效爬取规则。"""
    return get_rule_loader().load_for_site(site_config)
