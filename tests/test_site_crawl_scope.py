"""爬取范围过滤测试。"""

from __future__ import annotations

from unittest.mock import patch

from src.core.site_sync import (
    get_crawl_scope,
    get_crawl_scope_label,
    is_industry_site,
    is_soe_site,
    load_enabled_sites,
    site_in_crawl_scope,
)


def test_is_soe_site_explicit_flag():
    assert is_soe_site({"soe": True, "category": "enterprise", "parent": None}) is True
    assert is_soe_site({"soe": False, "category": "enterprise", "parent": "某集团"}) is False


def test_is_soe_site_fallback_parent():
    assert is_soe_site({"category": "enterprise", "parent": "中国铁建"}) is True
    assert is_soe_site({"category": "enterprise", "parent": None}) is False
    assert is_soe_site({"category": "provincial_ggzy", "parent": None}) is False


def test_is_industry_site_flag():
    assert is_industry_site({"industry": True}) is True
    assert is_industry_site({"industry": False}) is False
    assert is_industry_site({}) is False


def test_site_in_crawl_scope_soe_and_industry():
    soe = {"id": "soe_a", "soe": True}
    industry = {"id": "dlzb_power", "industry": True, "soe": False}
    other = {"id": "other", "enabled": True}
    assert site_in_crawl_scope(soe, "soe_and_industry") is True
    assert site_in_crawl_scope(industry, "soe_and_industry") is True
    assert site_in_crawl_scope(other, "soe_and_industry") is False
    assert site_in_crawl_scope(industry, "soe_only") is False


def test_load_enabled_sites_soe_only_excludes_non_soe():
    sites_yaml = {
        "crawl_defaults": {},
        "sites": [
            {
                "id": "soe_a",
                "enabled": True,
                "soe": True,
                "adapter": "ccgp",
            },
            {
                "id": "dlzb_power",
                "enabled": True,
                "soe": False,
                "industry": True,
                "adapter": "generic",
            },
            {
                "id": "ggzy_上海市",
                "enabled": True,
                "category": "provincial_ggzy",
                "adapter": "ggzy_provincial",
            },
            {
                "id": "disabled_soe",
                "enabled": False,
                "soe": True,
                "adapter": "ccgp",
            },
        ],
    }
    with patch("src.core.site_sync.get_crawl_scope", return_value="soe_only"):
        with patch("src.core.site_sync.load_sites_config", return_value=sites_yaml):
            ids = [s["id"] for s in load_enabled_sites()]
    assert ids == ["soe_a"]


def test_load_enabled_sites_soe_and_industry_includes_industry():
    sites_yaml = {
        "crawl_defaults": {},
        "sites": [
            {"id": "soe_a", "enabled": True, "soe": True},
            {"id": "dlzb_power", "enabled": True, "soe": False, "industry": True},
            {"id": "ggzy_上海市", "enabled": True, "industry": True},
            {"id": "other", "enabled": True},
        ],
    }
    with patch("src.core.site_sync.get_crawl_scope", return_value="soe_and_industry"):
        with patch("src.core.site_sync.load_sites_config", return_value=sites_yaml):
            ids = [s["id"] for s in load_enabled_sites()]
    assert set(ids) == {"soe_a", "dlzb_power", "ggzy_上海市"}


def test_load_enabled_sites_all_scope():
    sites_yaml = {
        "crawl_defaults": {},
        "sites": [
            {"id": "soe_a", "enabled": True, "soe": True},
            {"id": "dlzb_power", "enabled": True, "soe": False},
        ],
    }
    with patch("src.core.site_sync.get_crawl_scope", return_value="all"):
        with patch("src.core.site_sync.load_sites_config", return_value=sites_yaml):
            ids = [s["id"] for s in load_enabled_sites()]
    assert set(ids) == {"soe_a", "dlzb_power"}


def test_get_crawl_scope_default_soe_only():
    with patch("src.core.site_sync.SCHEDULE_PATH") as mock_path:
        mock_path.exists.return_value = False
        assert get_crawl_scope() == "soe_only"


def test_get_crawl_scope_label():
    assert get_crawl_scope_label("soe_and_industry") == "国央企 + 行业平台"
    assert get_crawl_scope_label("soe_only") == "仅国央企"
