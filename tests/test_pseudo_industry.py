"""pseudo_industry 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.industry.pseudo_industry import filter_docs_by_code, parse_industry_code


def test_parse_product_line():
    code = parse_industry_code("product_line:田间作业")
    assert code.type == "product_line"
    assert code.name == "田间作业"


def test_parse_invalid_empty():
    with pytest.raises(ValueError):
        parse_industry_code("")


def test_filter_product_line():
    code = parse_industry_code("product_line:田间作业")
    docs = [
        {"title": "巡田管理系统", "content_text": "巡田采集与农事记录"},
        {"title": "普通钢材采购", "content_text": "钢材"},
    ]
    filtered = filter_docs_by_code(docs, code)
    assert len(filtered) == 1
    assert "巡田" in filtered[0]["title"]
