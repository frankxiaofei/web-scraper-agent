"""Phase 3 global API 测试。"""

from __future__ import annotations

from src.industry.global_data.taxonomy_views import build_taxonomy_view


def test_naics_taxonomy_view():
    data = build_taxonomy_view("NAICS2022")
    assert data["taxonomy"] == "NAICS2022"
    assert len(data["items"]) >= 1


def test_isic_taxonomy_view():
    data = build_taxonomy_view("ISIC4")
    assert data["taxonomy"] == "ISIC4"
