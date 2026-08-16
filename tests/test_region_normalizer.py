"""region_normalizer 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.industry.region_normalizer import normalize_region_to_province


def test_normalize_guangdong():
    result = normalize_region_to_province("广东省广州市天河区")
    assert result == ("CN-44", "广东省")


def test_normalize_xinjiang():
    result = normalize_region_to_province("新疆乌鲁木齐市")
    assert result == ("CN-65", "新疆维吾尔自治区")


def test_normalize_empty():
    assert normalize_region_to_province("") is None
    assert normalize_region_to_province(None) is None
