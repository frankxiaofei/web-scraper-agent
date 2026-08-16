"""region_normalizer 市级测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.industry.region_normalizer import (
    normalize_region_to_city,
    region_name_by_code,
)


def test_normalize_city_guangzhou():
    result = normalize_region_to_city("广东省广州市天河区", parent_province_code="CN-44")
    assert result == ("CN-44::广州市", "广州市")


def test_normalize_city_nanjing():
    result = normalize_region_to_city("江苏省南京市", parent_province_code="CN-32")
    assert result == ("CN-32::南京市", "南京市")


def test_normalize_city_beijing_municipality():
    result = normalize_region_to_city("北京市朝阳区")
    assert result == ("CN-11::北京市", "北京市")


def test_normalize_city_wrong_parent():
    assert normalize_region_to_city("广东省广州市", parent_province_code="CN-32") is None


def test_region_name_by_code_city():
    assert region_name_by_code("CN-44::广州市") == "广州市"
    assert region_name_by_code("CN-44") == "广东省"
