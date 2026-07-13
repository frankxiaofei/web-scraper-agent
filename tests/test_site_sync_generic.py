"""site_sync 对 generic + crawl_rules 的放行逻辑。"""

from __future__ import annotations

from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.site_sync import _generic_has_crawl_rules, should_fetch_detail


def test_generic_without_rules_rejected():
    site = {
        "id": "中国建筑集团有限公司_云筑网",
        "adapter": "generic",
        "url": "https://www.yzw.cn",
    }
    assert _generic_has_crawl_rules(site) is False


def test_generic_with_rules_allowed():
    site = {
        "id": "中国电力建设集团有限公司_公共资源交易服务平台",
        "adapter": "generic",
        "url": "https://bid.powerchina.cn",
    }
    assert _generic_has_crawl_rules(site) is True


def test_generic_fetch_detail_when_rule_enabled():
    site = {
        "id": "中国电力建设集团有限公司_公共资源交易服务平台",
        "adapter": "generic",
        "url": "https://bid.powerchina.cn",
        "fetch_detail": True,
    }
    assert should_fetch_detail(site) is True


def test_ceec_rules_enabled_after_platform_migration():
    site = {
        "id": "中国能源建设集团有限公司_电子采购平台",
        "adapter": "generic",
        "url": "https://ceec.dnezb.com/",
    }
    assert _generic_has_crawl_rules(site) is True


def test_crec_rules_enabled_after_platform_migration():
    site = {
        "id": "crec_bidding",
        "adapter": "generic",
        "url": "https://zhfdc.dlzb.com/",
    }
    assert _generic_has_crawl_rules(site) is True


def test_generic_adapter_registered():
    from src.core.scheduler import get_adapter_class

    cls = get_adapter_class("generic")
    assert cls.__name__ == "GenericRuleAdapter"


def test_site_eligible_for_schedule_non_generic():
    site = {"id": "ccgp_national", "adapter": "ccgp", "enabled": True}
    assert site_eligible_for_schedule(site) is True


def test_site_eligible_for_schedule_generic_without_rules():
    site = {
        "id": "中国建筑集团有限公司_云筑网",
        "adapter": "generic",
        "url": "https://www.yzw.cn",
    }
    assert site_eligible_for_schedule(site) is False


def test_site_eligible_for_schedule_generic_with_rules():
    site = {
        "id": "中国电力建设集团有限公司_公共资源交易服务平台",
        "adapter": "generic",
        "url": "https://bid.powerchina.cn",
    }
    assert site_eligible_for_schedule(site) is True
