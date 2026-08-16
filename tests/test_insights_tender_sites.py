"""商机洞察招标源站点与 CCGP 关键词搜索。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.adapters.ccgp import build_ccgp_search_url
from src.core.agri_sites import get_insights_tender_site_ids


def test_insights_tender_site_ids_includes_ccgp_and_soe():
    ids = get_insights_tender_site_ids()
    assert "ccgp_national" in ids
    assert "crec_bidding" in ids
    assert ids.index("ccgp_national") < ids.index("crec_bidding")


def test_build_ccgp_search_url_encodes_keyword():
    url = build_ccgp_search_url("数字农业")
    assert "search.ccgp.gov.cn/bxsearch" in url
    assert "kw=" in url
    assert "timeType=6" in url
