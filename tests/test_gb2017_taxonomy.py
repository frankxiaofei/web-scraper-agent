"""GB2017 Cascader 树测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.industry.gb2017_taxonomy import build_gb2017_cascader, flatten_gb2017_items


def test_gb2017_cascader_has_roots():
    tree = build_gb2017_cascader()
    assert len(tree) >= 2
    codes = {n["code"] for n in tree}
    assert "A" in codes
    assert "I" in codes


def test_gb2017_cascader_nested_children():
    tree = build_gb2017_cascader()
    agri = next(n for n in tree if n["code"] == "A")
    child_codes = {c["code"] for c in agri.get("children") or []}
    assert "A01" in child_codes


def test_flatten_gb2017_items():
    items = flatten_gb2017_items()
    assert any(it["code"] == "A01" for it in items)
