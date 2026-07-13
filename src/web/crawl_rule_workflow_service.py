"""爬取规则工作流 Web 服务。"""

from __future__ import annotations

from typing import Any, Optional

import yaml

from src.core.crawl_rule_workflow import (
    NODE_FIELD_SCHEMAS,
    NODE_LABELS,
    PALETTE_NODE_TYPES,
    parse_workflow_payload,
    rule_to_workflow,
    workflow_to_yaml,
)
from src.core.rule_generator import get_rule_generator
from src.core.crawl_rules import CrawlRule
from src.core.rule_generator import load_rule_yaml, save_rule_yaml, validate_yaml
from src.core.site_config_write import get_site_schedule_status
from src.core.site_sync import get_site_by_id


def _default_rule(site_id: str, site: dict[str, Any]) -> CrawlRule:
    return CrawlRule(
        site_id=site_id,
        name=site.get("name") or site_id,
        enabled=True,
        entry_url=site.get("url"),
        list_page={"strategy": "dom"},
        pagination={"type": "next_button", "max_pages": 20},
        detail={"fetch_detail": False},
        limits={"max_pages": 20, "max_items": 100, "max_depth": 2, "rate_limit_seconds": 0.0},
    )


class CrawlRuleWorkflowService:
    def get_workflow(self, site_id: str) -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "error": f"站点不存在: {site_id}"}

        yaml_text = load_rule_yaml(site_id)
        schedule_status = get_site_schedule_status(site_id)

        if yaml_text:
            validation = validate_yaml(yaml_text, site_id=site_id)
            if validation.get("preview"):
                rule = CrawlRule.model_validate(validation["preview"])
            else:
                rule = _default_rule(site_id, site)
        else:
            rule = _default_rule(site_id, site)
            validation = {"valid": False, "errors": ["规则文件不存在，使用默认模板"]}

        workflow = rule_to_workflow(rule)
        return {
            "ok": True,
            "site_id": site_id,
            "site_name": site.get("name") or site_id,
            "site_url": site.get("url") or "",
            "site_enabled": bool(site.get("enabled")),
            "has_rule_file": yaml_text is not None,
            "yaml": yaml_text,
            "validation": {
                "valid": bool(validation.get("valid")),
                "errors": validation.get("errors") or [],
            },
            "schedule_status": schedule_status,
            "workflow": workflow,
            "node_labels": NODE_LABELS,
            "field_schemas": NODE_FIELD_SCHEMAS,
            "palette_types": PALETTE_NODE_TYPES,
        }

    def save_workflow(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "error": f"站点不存在: {site_id}"}

        rule, errors = parse_workflow_payload(payload, site_id=site_id)
        if rule is None:
            return {"ok": False, "valid": False, "errors": errors}

        yaml_text = workflow_to_yaml(payload, site_id=site_id)
        save_result = save_rule_yaml(site_id, yaml_text)
        if not save_result.get("ok"):
            return {
                "ok": False,
                "valid": False,
                "errors": save_result.get("errors") or errors,
            }

        schedule_status = get_site_schedule_status(site_id)
        return {
            "ok": True,
            "valid": True,
            "path": save_result.get("path"),
            "yaml": yaml_text,
            "workflow": rule_to_workflow(rule),
            "schedule_status": schedule_status,
            "errors": [],
        }

    async def generate_workflow(
        self,
        site_id: str,
        *,
        url: str = "",
        prompt: str = "",
        page_hints: str = "",
        save: bool = False,
    ) -> dict[str, Any]:
        site = get_site_by_id(site_id)
        if not site:
            return {"ok": False, "error": f"站点不存在: {site_id}"}

        entry_url = url or site.get("url") or ""
        messages: list[dict[str, str]] = []
        if prompt.strip():
            messages.append({"role": "user", "content": prompt.strip()})

        result = await get_rule_generator().generate(
            site_id=site_id,
            url=entry_url,
            messages=messages,
            page_hints=page_hints.strip(),
            fetch_hints=not bool(page_hints.strip()),
        )
        if not result.get("valid") or not result.get("preview"):
            return {
                "ok": False,
                "error": "AI 生成失败",
                "errors": result.get("errors") or ["规则校验未通过"],
                "yaml": result.get("yaml"),
            }

        rule = CrawlRule.model_validate(result["preview"])
        workflow = rule_to_workflow(rule)

        saved = False
        path: Optional[str] = None
        if save and result.get("yaml"):
            save_result = save_rule_yaml(site_id, result["yaml"])
            saved = bool(save_result.get("ok"))
            path = save_result.get("path")

        return {
            "ok": True,
            "site_id": site_id,
            "workflow": workflow,
            "yaml": result.get("yaml"),
            "saved": saved,
            "path": path,
            "node_labels": NODE_LABELS,
            "field_schemas": NODE_FIELD_SCHEMAS,
            "palette_types": PALETTE_NODE_TYPES,
        }


_service: Optional[CrawlRuleWorkflowService] = None


def get_crawl_rule_workflow_service() -> CrawlRuleWorkflowService:
    global _service
    if _service is None:
        _service = CrawlRuleWorkflowService()
    return _service
