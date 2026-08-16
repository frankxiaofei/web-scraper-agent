"""NAICS / crosswalk taxonomy 视图（Phase 3 P3-T-006）。"""

from __future__ import annotations

from typing import Any

from src.industry.gb2017_taxonomy import build_gb2017_cascader


_NAICS_SEED: list[dict[str, str]] = [
    {"code": "11", "name": "Agriculture, Forestry, Fishing and Hunting", "level": "1"},
    {"code": "111", "name": "Crop Production", "level": "2", "parent_code": "11"},
    {"code": "1111", "name": "Oilseed and Grain Farming", "level": "3", "parent_code": "111"},
    {"code": "54", "name": "Professional, Scientific, and Technical Services", "level": "1"},
]


def build_taxonomy_view(taxonomy: str) -> dict[str, Any]:
    taxonomy = (taxonomy or "GB2017").upper()
    if taxonomy == "GB2017":
        return {"taxonomy": taxonomy, "tree": build_gb2017_cascader()}
    if taxonomy in {"NAICS2022", "NAICS"}:
        return {"taxonomy": "NAICS2022", "items": _NAICS_SEED}
    if taxonomy == "ISIC4":
        return {
            "taxonomy": "ISIC4",
            "items": [
                {"code": "A", "name": "Agriculture, forestry and fishing", "level": "1"},
                {"code": "01", "name": "Crop and animal production", "level": "2", "parent_code": "A"},
            ],
        }
    raise ValueError(f"unsupported taxonomy: {taxonomy}")
