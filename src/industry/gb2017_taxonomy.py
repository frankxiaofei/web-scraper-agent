"""GB/T 4754-2017 行业分类 Cascader 树（Phase 1）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SEED_FILE = ROOT / "data" / "industry" / "gb2017_seed.yaml"


@lru_cache(maxsize=1)
def load_gb2017_rows() -> list[dict[str, Any]]:
    if not SEED_FILE.is_file():
        return []
    data = yaml.safe_load(SEED_FILE.read_text(encoding="utf-8")) or {}
    return list(data.get("industries") or [])


def build_gb2017_cascader() -> list[dict[str, Any]]:
    """构建三级 Cascader 树：门类 → 大类 → 中类。"""
    rows = load_gb2017_rows()
    if not rows:
        return []

    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row["code"])
        by_code[code] = {
            "code": code,
            "name": row["name"],
            "level": int(row.get("level") or 1),
            "taxonomy": "GB2017",
            "parent_code": row.get("parent_code"),
            "children": [],
        }

    roots: list[dict[str, Any]] = []
    for node in by_code.values():
        parent = node.get("parent_code")
        if parent and parent in by_code:
            by_code[parent]["children"].append(node)
        elif not parent:
            roots.append(node)

    def _sort(nodes: list[dict[str, Any]]) -> None:
        nodes.sort(key=lambda n: n["code"])
        for n in nodes:
            if n["children"]:
                _sort(n["children"])

    _sort(roots)
    return roots


def flatten_gb2017_items() -> list[dict[str, str]]:
    """扁平列表，供 select / filter 使用。"""
    items: list[dict[str, str]] = []
    for row in load_gb2017_rows():
        items.append(
            {
                "code": str(row["code"]),
                "name": str(row["name"]),
                "type": "gb2017",
                "taxonomy": "GB2017",
            }
        )
    return items
