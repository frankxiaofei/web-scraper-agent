"""从 Excel（zip+xml）解析招标网址，生成站点配置字典。

不依赖 openpyxl，直接解析 xlsx 内部的 XML，避免部分文件 openpyxl 解析失败。
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# MVP 首批 15 站点 ID（用于标记 mvp: true）
MVP_SITE_IDS = frozenset(
    {
        "zycg_national",
        "ggzy_national",
        "cebpubservice_national",
        "ccgp_北京市",
        "ggzy_北京市",
        "ccgp_上海市",
        "ggzy_上海市",
        "ccgp_广东省",
        "ggzy_广东省",
        "ccgp_江苏省",
        "ccgp_浙江省",
        "ccgp_四川省",
        "ecp_sgcc",
        "csg_bidding",
        "sinopec_bidding",
        "chinamobile_bidding",
        "crec_bidding",
    }
)

# 全国级站点名称 → 固定 ID
NATIONAL_SITE_IDS = {
    "中国政府采购网": "ccgp_national",
    "中央政府采购网": "zycg_national",
    "全国公共资源交易平台": "ggzy_national",
    "中国招标投标公共服务平台": "cebpubservice_national",
    "政府采购云平台": "zcygov_national",
    "全国招标信息网": "bidnews_national",
    "中国招标网": "bidchance_national",
    "中国招标投标网": "cecbid_national",
    "中国通用招标网": "china_tender_national",
    "招标采购导航网": "okcis_national",
    "采招网": "bidcenter_national",
    "招投标资讯网": "chinazbbid_national",
}

# 站点 ID → 适配器名称
ADAPTER_MAP = {
    "ccgp_national": "ccgp",
    "zycg_national": "zycg",
    "ggzy_national": "ggzy",
    "cebpubservice_national": "cebpubservice",
    "cecbid_national": "cecbid",
    "china_tender_national": "china_tender",
    "bidchance_national": "bidchance",
    "chinazbbid_national": "chinazbbid",
    "ecp_sgcc": "sgcc_ecp",
    "csg_bidding": "csg",
    "sinopec_bidding": "sinopec",
    "chinamobile_bidding": "chinamobile",
    "crec_bidding": "crec",
}

# MVP 全国级 + 央企站点（始终启用）
MVP_ENABLED_SITE_IDS = frozenset(
    {
        "zycg_national",
        "ggzy_national",
        "cebpubservice_national",
        "ccgp_北京市",
        "ccgp_上海市",
        "ccgp_广东省",
        "ggzy_北京市",
        "ggzy_上海市",
        "ggzy_广东省",
        "ecp_sgcc",
        "csg_bidding",
        "sinopec_bidding",
        "chinamobile_bidding",
        "crec_bidding",
    }
)

# Phase 4 省级扩展（适配器已验证可抓取）
PHASE4_ENABLED_SITE_IDS = frozenset(
    {
        "ccgp_江苏省",
        "ccgp_浙江省",
        "ccgp_湖北省",
        "ccgp_河南省",
        "ccgp_四川省",
        "ggzy_江苏省",
        "ggzy_浙江省",
        "ggzy_四川省",
        "ggzy_湖北省",
        "ggzy_山东省",
        "ggzy_河南省",
        "ggzy_安徽省",
    }
)

ENABLED_SITE_IDS = MVP_ENABLED_SITE_IDS | PHASE4_ENABLED_SITE_IDS

# Excel 未收录但需纳入配置的省级站点
EXTRA_PROVINCIAL_SITES = [
    {
        "id": "ccgp_四川省",
        "name": "四川省政府采购网",
        "url": "http://www.ccgp-sichuan.gov.cn/",
        "category": "provincial_gov",
        "region": "四川省",
    },
]

# 站点 URL 覆盖（Excel 中域名过时或跳转；Tab4 重点关注 dlzb 迁移）
SITE_URL_OVERRIDES = {
    "ccgp_河南省": "https://zfcg.henan.gov.cn/",
    "crec_bidding": "https://zhfdc.dlzb.com/",
    "中国铁道建筑集团有限公司_物资采购网": "https://tjbid.dlzb.com/",
    "中国交通建设集团有限公司_供应链管理信息系统": "https://zgjtjs.dlzb.com/",
    "中国能源建设集团有限公司_电子采购平台": "https://ceec.dnezb.com/",
}

# 明确禁用站点及原因（适配器已注册但当前不可抓取）
DISABLED_SITE_NOTES = {
    "ccgp_山东省": "SPA 列表页（sdgp2017）无法在无登录下解析采购公告",
    "ggzy_河北省": "域名 ggzy.hebei.gov.cn 无法解析（ERR_NAME_NOT_RESOLVED）",
    "ggzy_福建省": "首页连接超时，列表页无公告数据",
    "ggzy_湖南省": "Vue SPA 列表链接为 javascript:void(0)，无法提取公告 URL",
}

# 央企名称 → 固定 ID
ENTERPRISE_SITE_IDS = {
    "国家电网有限公司": "ecp_sgcc",
    "中国南方电网有限责任公司": "csg_bidding",
    "中国石油化工集团有限公司": "sinopec_bidding",
    "中国移动通信集团有限公司": "chinamobile_bidding",
    "中国铁路工程集团有限公司": "crec_bidding",
}


def _slugify(text: str) -> str:
    """将文本转为可用作 ID 的 slug（中文地区名保留原文）。"""
    text = re.sub(r"[\u200c\u200b\s]+", "", text.strip())
    slug = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    return slug.strip("_") or "unknown"


def _national_site_id(name: str) -> str:
    return NATIONAL_SITE_IDS.get(name) or _slugify(name)


def _region_site_id(prefix: str, region: str) -> str:
    return f"{prefix}_{_slugify(region)}"


def _is_valid_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if url.startswith("建议") or "官网" in url and not url.startswith("http"):
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc
    except Exception:
        return False


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    data = zf.read("xl/sharedStrings.xml")
    root = ET.fromstring(data)
    strings: list[str] = []
    for si in root:
        t = si.find(f"{NS}t")
        if t is not None and t.text:
            strings.append(t.text)
        else:
            parts = [r.text or "" for r in si.iter(f"{NS}t")]
            strings.append("".join(parts))
    return strings


def _cell_value(cell: ET.Element, strings: list[str]) -> str:
    cell_type = cell.get("t")
    v = cell.find(f"{NS}v")
    if v is None or v.text is None:
        return ""
    if cell_type == "s":
        return strings[int(v.text)]
    return v.text


def _parse_col_row(ref: str) -> tuple[str, int]:
    m = re.match(r"([A-Z]+)(\d+)", ref)
    if not m:
        return "A", 0
    return m.group(1), int(m.group(2))


def _read_sheet(zf: zipfile.ZipFile, sheet_path: str, strings: list[str]) -> dict[int, dict[str, str]]:
    root = ET.fromstring(zf.read(sheet_path))
    rows: dict[int, dict[str, str]] = {}
    for row in root.findall(f".//{NS}row"):
        rnum = int(row.attrib["r"])
        cells: dict[str, str] = {}
        for c in row.findall(f"{NS}c"):
            col, _ = _parse_col_row(c.attrib["r"])
            cells[col] = _cell_value(c, strings)
        rows[rnum] = cells
    return rows


def _resolve_sheet_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target: dict[str, str] = {}
    for r in rels:
        rid_to_target[r.attrib["Id"]] = r.attrib["Target"]

    sheet_map: dict[str, str] = {}
    for sheet in wb.findall(f".//{NS}sheet"):
        name = sheet.attrib["name"]
        rid = sheet.attrib[REL_NS + "id"]
        target = rid_to_target[rid]
        if not target.startswith("xl/"):
            target = "xl/" + target.replace("worksheets/", "worksheets/")
        sheet_map[name] = target
    return sheet_map


def _resolve_adapter(site_id: str, category: str) -> str:
    if site_id in ADAPTER_MAP:
        return ADAPTER_MAP[site_id]
    if category == "provincial_gov" and site_id.startswith("ccgp_"):
        return "ccgp_provincial"
    if category == "provincial_ggzy" and site_id.startswith("ggzy_"):
        return "ggzy_provincial"
    return "generic"


def _default_fetch_detail(adapter: str, site_id: str) -> bool:
    """ccgp/ggzy 系列 MVP 站默认开启详情抓取。"""
    if adapter not in ("ccgp", "ccgp_provincial", "ggzy", "ggzy_provincial"):
        return False
    return site_id in MVP_SITE_IDS


def _is_enabled(site_id: str) -> bool:
    if site_id in DISABLED_SITE_NOTES:
        return False
    return site_id in ENABLED_SITE_IDS


def _make_site(
    site_id: str,
    name: str,
    url: str,
    category: str,
    region: str | None = None,
    parent: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    url = SITE_URL_OVERRIDES.get(site_id, url)
    adapter = _resolve_adapter(site_id, category)
    site_notes = notes or ""
    if site_id in DISABLED_SITE_NOTES:
        site_notes = DISABLED_SITE_NOTES[site_id]
    site: dict[str, Any] = {
        "id": site_id,
        "name": name,
        "url": url,
        "category": category,
        "region": region,
        "parent": parent,
        "adapter": adapter,
        "enabled": _is_enabled(site_id),
        "mvp": site_id in MVP_SITE_IDS,
        "notes": site_notes,
    }
    if category in ("national", "provincial_gov", "provincial_ggzy"):
        site["industry"] = True
    if category == "enterprise" and parent:
        site["soe"] = site_id in {
            "中国建筑集团有限公司_云筑网",
            "中国铁道建筑集团有限公司_物资采购网",
            "中国交通建设集团有限公司_供应链管理信息系统",
            "中国电力建设集团有限公司_公共资源交易服务平台",
            "中国能源建设集团有限公司_电子采购平台",
        }
    if _default_fetch_detail(adapter, site_id):
        site["fetch_detail"] = True
    return site


def load_sites_from_excel(excel_path: Path) -> list[dict[str, Any]]:
    """解析 Excel 并返回站点配置列表。"""
    sites: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    with zipfile.ZipFile(excel_path) as zf:
        strings = _load_shared_strings(zf)
        sheet_paths = _resolve_sheet_paths(zf)

        # 全国级招标采购平台
        if "全国级招标采购平台" in sheet_paths:
            rows = _read_sheet(zf, sheet_paths["全国级招标采购平台"], strings)
            for rnum in sorted(rows.keys()):
                if rnum == 1:
                    continue
                row = rows[rnum]
                name = row.get("B", "").strip()
                url = row.get("C", "").strip()
                if not name or not _is_valid_url(url):
                    continue
                site_id = _national_site_id(name)
                if site_id in seen_ids:
                    site_id = f"{site_id}_{rnum}"
                seen_ids.add(site_id)
                sites.append(_make_site(site_id, name, url, "national"))

        # 省级招标网站
        if "省级招标网站" in sheet_paths:
            rows = _read_sheet(zf, sheet_paths["省级招标网站"], strings)
            for rnum in sorted(rows.keys()):
                if rnum == 1:
                    continue
                row = rows[rnum]
                region = row.get("B", "").strip()
                gov_url = row.get("C", "").strip()
                ggzy_url = row.get("D", "").strip()

                if region and _is_valid_url(gov_url):
                    site_id = _region_site_id("ccgp", region)
                    if site_id in seen_ids:
                        site_id = f"{site_id}_{rnum}_gov"
                    seen_ids.add(site_id)
                    sites.append(
                        _make_site(
                            site_id,
                            f"{region}政府采购网",
                            gov_url,
                            "provincial_gov",
                            region=region,
                        )
                    )

                if region and _is_valid_url(ggzy_url):
                    site_id = _region_site_id("ggzy", region)
                    if site_id in seen_ids:
                        site_id = f"{site_id}_{rnum}_ggzy"
                    seen_ids.add(site_id)
                    sites.append(
                        _make_site(
                            site_id,
                            f"{region}公共资源交易平台",
                            ggzy_url,
                            "provincial_ggzy",
                            region=region,
                        )
                    )

        # 中央企业
        if "中央企业" in sheet_paths:
            rows = _read_sheet(zf, sheet_paths["中央企业"], strings)
            for rnum in sorted(rows.keys()):
                if rnum == 1:
                    continue
                row = rows[rnum]
                seq = row.get("A", "").strip()
                enterprise = row.get("B", "").strip()
                platform_name = row.get("C", "").strip()
                url = row.get("D", "").strip()
                notes = row.get("E", "").strip()

                # 跳过分类标题行
                if not enterprise or not _is_valid_url(url):
                    continue
                if not seq.isdigit():
                    continue

                if enterprise in ENTERPRISE_SITE_IDS:
                    site_id = ENTERPRISE_SITE_IDS[enterprise]
                else:
                    ent_slug = _slugify(enterprise)
                    plat_slug = _slugify(platform_name) if platform_name else "platform"
                    site_id = f"{ent_slug}_{plat_slug}"
                if site_id in seen_ids:
                    site_id = f"{site_id}_{seq}"
                seen_ids.add(site_id)

                display_name = platform_name or enterprise
                sites.append(
                    _make_site(
                        site_id,
                        display_name,
                        url,
                        "enterprise",
                        parent=enterprise,
                        notes=notes,
                    )
                )

        # Excel 未收录的额外省级站
        for extra in EXTRA_PROVINCIAL_SITES:
            site_id = extra["id"]
            if site_id in seen_ids:
                continue
            seen_ids.add(site_id)
            sites.append(
                _make_site(
                    site_id,
                    extra["name"],
                    extra["url"],
                    extra["category"],
                    region=extra.get("region"),
                )
            )

    return sites


def sites_to_yaml_dict(sites: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_from": "docs/招标网址 副本.xlsx",
        "total": len(sites),
        "mvp_count": sum(1 for s in sites if s.get("mvp")),
        "sites": sites,
    }
