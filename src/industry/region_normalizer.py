"""省名 → CN-XX 标准代码映射。"""

from __future__ import annotations

import re
from typing import Optional

# (code, 标准全称) — 34 省级行政区
PROVINCE_ALIASES: dict[str, tuple[str, str]] = {
    "北京": ("CN-11", "北京市"),
    "天津": ("CN-12", "天津市"),
    "河北": ("CN-13", "河北省"),
    "山西": ("CN-14", "山西省"),
    "内蒙古": ("CN-15", "内蒙古自治区"),
    "辽宁": ("CN-21", "辽宁省"),
    "吉林": ("CN-22", "吉林省"),
    "黑龙江": ("CN-23", "黑龙江省"),
    "上海": ("CN-31", "上海市"),
    "江苏": ("CN-32", "江苏省"),
    "浙江": ("CN-33", "浙江省"),
    "安徽": ("CN-34", "安徽省"),
    "福建": ("CN-35", "福建省"),
    "江西": ("CN-36", "江西省"),
    "山东": ("CN-37", "山东省"),
    "河南": ("CN-41", "河南省"),
    "湖北": ("CN-42", "湖北省"),
    "湖南": ("CN-43", "湖南省"),
    "广东": ("CN-44", "广东省"),
    "广西": ("CN-45", "广西壮族自治区"),
    "海南": ("CN-46", "海南省"),
    "重庆": ("CN-50", "重庆市"),
    "四川": ("CN-51", "四川省"),
    "贵州": ("CN-52", "贵州省"),
    "云南": ("CN-53", "云南省"),
    "西藏": ("CN-54", "西藏自治区"),
    "陕西": ("CN-61", "陕西省"),
    "甘肃": ("CN-62", "甘肃省"),
    "青海": ("CN-63", "青海省"),
    "宁夏": ("CN-64", "宁夏回族自治区"),
    "新疆": ("CN-65", "新疆维吾尔自治区"),
    "台湾": ("CN-71", "台湾省"),
    "香港": ("CN-81", "香港特别行政区"),
    "澳门": ("CN-82", "澳门特别行政区"),
}

PROVINCE_ALIASES["新疆维吾尔"] = ("CN-65", "新疆维吾尔自治区")
PROVINCE_ALIASES["宁夏回族"] = ("CN-64", "宁夏回族自治区")

# 按 key 长度降序，优先匹配长别名
_SORTED_KEYS = sorted(PROVINCE_ALIASES.keys(), key=len, reverse=True)


def normalize_region_to_province(location: str | None) -> tuple[str, str] | None:
    """从 project_location/region 自由文本提取省级 code + 标准名。"""
    if not location:
        return None
    text = str(location).strip()
    if not text:
        return None
    for key in _SORTED_KEYS:
        code, name = PROVINCE_ALIASES[key]
        if key in text or name in text:
            return code, name
    m = re.match(r"([\u4e00-\u9fa5]{2,}(?:省|自治区|市))", text)
    if m:
        fragment = m.group(1)
        for key in _SORTED_KEYS:
            code, name = PROVINCE_ALIASES[key]
            if key in fragment or name.startswith(fragment[:2]):
                return code, name
    return None


def province_name_by_code(code: str) -> Optional[str]:
    for _key, (c, name) in PROVINCE_ALIASES.items():
        if c == code:
            return name
    return None


def all_provinces() -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for _key, (code, name) in PROVINCE_ALIASES.items():
        if code in seen:
            continue
        seen.add(code)
        result.append((code, name))
    return sorted(result, key=lambda x: x[0])


_DIRECT_MUNICIPALITIES = frozenset({"CN-11", "CN-12", "CN-31", "CN-50"})
_CITY_PATTERN = re.compile(r"([\u4e00-\u9fa5]{2,}(?:市|州|盟|地区|自治州))")


def normalize_region_to_city(
    location: str | None,
    *,
    parent_province_code: str | None = None,
) -> tuple[str, str] | None:
    """从 project_location 提取市级 code + 名称（Phase 1 drill-down）。"""
    if not location:
        return None
    prov = normalize_region_to_province(location)
    if not prov:
        return None
    prov_code, prov_name = prov
    if parent_province_code and prov_code != parent_province_code:
        return None

    if prov_code in _DIRECT_MUNICIPALITIES:
        return f"{prov_code}::{prov_name}", prov_name

    text = str(location).strip()
    search_text = text
    for key in _SORTED_KEYS:
        code, name = PROVINCE_ALIASES[key]
        if code != prov_code:
            continue
        for fragment in (name, key):
            idx = text.find(fragment)
            if idx >= 0:
                search_text = text[idx + len(fragment) :]
                break

    match = _CITY_PATTERN.search(search_text)
    if not match:
        return None
    city_name = match.group(1)
    if city_name.endswith("省") or city_name == prov_name:
        return None
    return f"{prov_code}::{city_name}", city_name


def region_name_by_code(code: str) -> Optional[str]:
    """省级 CN-XX 或市级 CN-XX::城市名 → 显示名。"""
    if not code:
        return None
    if "::" in code:
        return code.split("::", 1)[1]
    return province_name_by_code(code)
