"""Load billing plans from config/billing_plans.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.billing.schemas import PlanEntitlements, PlanResponse, PlansListResponse
from src.core.config import ROOT

PLANS_FILE = ROOT / "config" / "billing_plans.yaml"


@lru_cache(maxsize=1)
def load_plans_config() -> dict[str, Any]:
    if not PLANS_FILE.is_file():
        return {"plans": {}}
    with PLANS_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def get_plan(plan_id: str) -> dict[str, Any] | None:
    plans = load_plans_config().get("plans") or {}
    return plans.get(plan_id)


def list_plans() -> PlansListResponse:
    raw = load_plans_config().get("plans") or {}
    items: list[PlanResponse] = []
    for plan_id, cfg in raw.items():
        ent = cfg.get("entitlements") or {}
        items.append(
            PlanResponse(
                id=cfg.get("id", plan_id),
                name=cfg.get("name", plan_id),
                price_monthly_cny=int(cfg.get("price_monthly_cny", 0)),
                price_yearly_cny=int(cfg.get("price_yearly_cny", 0)),
                entitlements=PlanEntitlements(**ent),
            )
        )
    return PlansListResponse(plans=items)


def get_entitlement_limit(plan_id: str, metric_key: str) -> int | None:
    """Return quota limit for a metric; -1 means unlimited (None in API)."""
    plan = get_plan(plan_id) or get_plan("free") or {}
    ent = plan.get("entitlements") or {}
    mapping = {
        "crawl_runs": ent.get("crawl_runs_per_day"),
        "llm_extraction_tokens": ent.get("llm_tokens_per_month"),
        "llm_chat_tokens": ent.get("llm_tokens_per_month"),
        "storage_bytes": ent.get("storage_gb"),
        "storage_gb": ent.get("storage_gb"),
        "hermes_messages": ent.get("hermes_messages_per_month"),
        "webbridge_minutes": ent.get("webbridge_minutes_per_month"),
        "gov_bid_api_calls": ent.get("gov_bid_api_calls"),
        "api_calls": None,
    }
    limit = mapping.get(metric_key)
    if limit is None:
        return None
    if int(limit) < 0:
        return None
    return int(limit)
