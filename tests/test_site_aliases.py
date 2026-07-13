"""站点 ID 别名解析测试。"""

from __future__ import annotations

from src.core.site_aliases import resolve_canonical_site_id
from src.core.site_sync import get_site_by_id
from src.web.crawl_agent_tools import resolve_site_id


def test_resolve_canonical_site_id_zhfdc_dlzb():
    assert resolve_canonical_site_id("zhfdc_dlzb") == "crec_bidding"
    assert resolve_canonical_site_id("crec_bidding") == "crec_bidding"


def test_get_site_by_id_zhfdc_dlzb_alias():
    site = get_site_by_id("zhfdc_dlzb")
    assert site is not None
    assert site["id"] == "crec_bidding"
    assert site["name"] == "鲁班商务网"
    assert "zhfdc.dlzb.com" in site["url"]


def test_resolve_site_id_zhfdc_natural_language():
    assert resolve_site_id("zhfdc") == "crec_bidding"
    assert resolve_site_id("zhfdc_dlzb") == "crec_bidding"
    assert resolve_site_id("鲁班商务网") == "crec_bidding"
    assert resolve_site_id("https://zhfdc.dlzb.com 爬取") == "crec_bidding"


def test_load_rules_zhfdc_dlzb_alias():
    from src.web.crawl_agent_skills import load_rules

    rules = load_rules("zhfdc_dlzb")
    assert rules["has_rule"] is True
    assert rules["valid"] is True
    assert rules["site_id"] == "crec_bidding"


def test_get_site_by_id_dlzb_power():
    site = get_site_by_id("dlzb_power")
    assert site is not None
    assert site["id"] == "dlzb_power"
    assert site["name"] == "电力招标网"
    assert "www.dlzb.com/search" in site["url"]


def test_resolve_site_id_dlzb_power_natural_language():
    assert resolve_site_id("电力招标网") == "dlzb_power"
    assert resolve_site_id("https://www.dlzb.com/search/") == "dlzb_power"
    assert resolve_site_id("dlzb 电力招标网") == "dlzb_power"


def test_resolve_site_id_bare_domain_powerchina():
    assert resolve_site_id("bid.powerchina.cn") == "中国电力建设集团有限公司_公共资源交易服务平台"


def test_get_site_by_id_tjbid_alias():
    site = get_site_by_id("tjbid")
    assert site is not None
    assert site["id"] == "中国铁道建筑集团有限公司_物资采购网"
    assert "tjbid.dlzb.com" in site["url"]


def test_get_site_by_id_powerchina_alias():
    site = get_site_by_id("powerchina")
    assert site is not None
    assert site["id"] == "中国电力建设集团有限公司_公共资源交易服务平台"
    assert "bid.powerchina.cn" in site["url"]


def test_get_site_by_id_powerchina_bid_alias():
    site = get_site_by_id("powerchina_bid")
    assert site is not None
    assert site["id"] == "中国电力建设集团有限公司_公共资源交易服务平台"


def test_load_rules_tjbid_alias():
    from src.web.crawl_agent_skills import load_rules

    rules = load_rules("tjbid")
    assert rules["has_rule"] is True
    assert rules["valid"] is True
    assert rules["site_id"] == "中国铁道建筑集团有限公司_物资采购网"


def test_site_not_registered_error_mentions_sites_yaml():
    from src.core.site_aliases import site_not_registered_error

    msg = site_not_registered_error("unknown_site_xyz")
    assert "sites.yaml" in msg
    assert "MongoDB" in msg
    assert "unknown_site_xyz" in msg


def test_load_rules_dlzb_power():
    from src.web.crawl_agent_skills import load_rules

    rules = load_rules("dlzb_power")
    assert rules["has_rule"] is True
    assert rules["valid"] is True
    assert rules["site_id"] == "dlzb_power"
