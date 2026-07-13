"""智慧农业专用站点范围测试。"""

from __future__ import annotations

from src.core.agri_sites import (
    DEFAULT_AGRI_SITE_IDS,
    get_agri_scope_label,
    get_agri_site_ids,
    load_agri_sites,
    resolve_agri_search_url,
)


def test_agri_site_ids_count():
    ids = get_agri_site_ids()
    assert len(ids) == 7
    assert "ggzy_上海市" not in ids
    assert "dlzb_power" in ids


def test_agri_scope_label():
    label = get_agri_scope_label()
    assert "数据源" in label or "央企" in label


def test_resolve_agri_search_url_optional():
    assert resolve_agri_search_url("dlzb_power", "智慧农业") is None
    assert resolve_agri_search_url("crec_bidding", "智慧农业") is None


def test_load_agri_sites_excludes_shanghai_ggzy():
    sites = load_agri_sites()
    ids = {s["id"] for s in sites}
    assert "ggzy_上海市" not in ids
    assert ids.issubset(set(DEFAULT_AGRI_SITE_IDS))
