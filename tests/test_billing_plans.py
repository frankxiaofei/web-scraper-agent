"""Billing plans config tests."""

from __future__ import annotations

from src.billing.plans import get_entitlement_limit, list_plans, load_plans_config


def test_load_plans_config_has_three_tiers():
    data = load_plans_config()
    plans = data.get("plans") or {}
    assert "free" in plans
    assert "pro" in plans
    assert "enterprise" in plans


def test_list_plans_response():
    resp = list_plans()
    ids = {p.id for p in resp.plans}
    assert ids == {"free", "pro", "enterprise"}
    free = next(p for p in resp.plans if p.id == "free")
    assert free.price_monthly_cny == 0
    assert "sop_wizard" in free.entitlements.features


def test_entitlement_limit_unlimited():
    assert get_entitlement_limit("enterprise", "crawl_runs") is None
    assert get_entitlement_limit("free", "crawl_runs") == 5
