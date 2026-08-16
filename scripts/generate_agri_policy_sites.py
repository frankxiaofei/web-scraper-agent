#!/usr/bin/env python3
"""从 docs/数字农业政策信息源台账.xlsx 合并数字农业政策政府站点到 sites.yaml / schedule.yaml。"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXCEL_PATH = ROOT / "docs" / "数字农业政策信息源台账.xlsx"
SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"
RULES_DIR = ROOT / "config" / "crawl_rules"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

REGION_SLUG = {
    "北京": "beijing",
    "天津": "tianjin",
    "河北": "hebei",
    "山西": "shanxi",
    "内蒙古": "neimenggu",
    "辽宁": "liaoning",
    "吉林": "jilin",
    "黑龙江": "heilongjiang",
    "上海": "shanghai",
    "江苏": "jiangsu",
    "浙江": "zhejiang",
    "安徽": "anhui",
    "福建": "fujian",
    "江西": "jiangxi",
    "山东": "shandong",
    "河南": "henan",
    "湖北": "hubei",
    "湖南": "hunan",
    "广东": "guangdong",
    "广西": "guangxi",
    "海南": "hainan",
    "重庆": "chongqing",
    "四川": "sichuan",
    "贵州": "guizhou",
    "云南": "yunnan",
    "西藏": "xizang",
    "陕西": "shaanxi",
    "甘肃": "gansu",
    "青海": "qinghai",
    "宁夏": "ningxia",
    "新疆": "xinjiang",
    "新疆兵团": "xinjiang_bingtuan",
}

CRAWL_RULE_TEMPLATE = """version: 1
site_id: {site_id}
name: {name}
enabled: true
entry_url: {entry_url}
list_page:
  strategy: dom
  wait_for: "body"
  container: "body"
  item: "a[href]"
  title: "a"
  link: "a[href]"
detail:
  fetch_detail: true
  strategy: dom
  content_selector: ".article, .content, #content, .TRS_Editor, .pages_content, .detail"
limits:
  max_pages: 5
  max_items: 30
  max_depth: 2
  rate_limit_seconds: 3.0
"""


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _cell_value(cell: ET.Element) -> str:
    if cell.get("t") == "inlineStr":
        is_el = cell.find(f"{NS}is")
        if is_el is not None:
            return "".join((t.text or "") for t in is_el.iter(f"{NS}t"))
        return ""
    v = cell.find(f"{NS}v")
    if v is None or v.text is None:
        return ""
    return v.text


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if not parsed.scheme:
        return url.lower()
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}".lower()


CENTRAL_URL_IDS = {
    _normalize_url("https://www.gov.cn/zhengce/"): "agri_gov_govcn",
    _normalize_url("https://www.moa.gov.cn/"): "agri_gov_moa",
    _normalize_url("https://www.agri.cn/"): "agri_gov_agricn",
    _normalize_url("https://zwfw.moa.gov.cn/"): "agri_gov_moa_zwfw",
}


def _slugify(text: str) -> str:
    text = re.sub(r"[\u200c\u200b\s]+", "", text.strip())
    slug = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    return slug.strip("_") or "unknown"


def _site_id_for_row(level: str, region: str, url: str) -> str:
    norm = _normalize_url(url)
    if norm in CENTRAL_URL_IDS:
        return CENTRAL_URL_IDS[norm]
    if level == "中央":
        host = urlparse(url).netloc.replace("www.", "").split(".")[0]
        return f"agri_gov_{_slugify(host)}"
    slug = REGION_SLUG.get(region) or _slugify(region)
    return f"agri_gov_{slug}"


def load_agri_policy_sources(excel_path: Path) -> list[dict[str, Any]]:
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel 不存在: {excel_path}")

    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(excel_path) as zf:
        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        for row in root.findall(f".//{NS}sheetData/{NS}row"):
            rnum = int(row.get("r", "0"))
            if rnum <= 1:
                continue
            cells: dict[str, str] = {}
            for c in row.findall(f"{NS}c"):
                ref = c.get("r", "")
                col = "".join(ch for ch in ref if ch.isalpha())
                cells[col] = _cell_value(c)
            name = cells.get("D", "").strip()
            url = cells.get("E", "").strip()
            if not name or not url.startswith("http"):
                continue
            level = cells.get("B", "").strip()
            region = cells.get("C", "").strip()
            notes = cells.get("F", "").strip()
            site_id = _site_id_for_row(level, region, url)
            rows.append(
                {
                    "seq": cells.get("A", "").strip(),
                    "level": level,
                    "region": region,
                    "name": name,
                    "url": url,
                    "notes": notes,
                    "site_id": site_id,
                    "category": "national" if level == "中央" else "provincial_agri",
                }
            )
    return rows


def _make_site(row: dict[str, Any]) -> dict[str, Any]:
    note = row.get("notes") or ""
    prefix = "数字农业政策信息源台账"
    full_note = f"{prefix}：{note}" if note else prefix
    site: dict[str, Any] = {
        "id": row["site_id"],
        "name": row["name"],
        "url": row["url"],
        "category": row["category"],
        "region": row["region"] if row["category"] == "provincial_agri" else None,
        "parent": None,
        "adapter": "generic",
        "enabled": True,
        "mvp": False,
        "industry": True,
        "fetch_detail": True,
        "max_items": 30,
        "min_delay_seconds": 3,
        "notes": full_note,
    }
    if row["category"] == "national" and row["region"]:
        site["parent"] = row["region"]
    return site


def _crawl_rule_needs_refresh(path: Path) -> bool:
    """占位规则含非法 pagination.type: none 时需重写。"""
    if not path.is_file():
        return True
    text = path.read_text(encoding="utf-8")
    return "pagination:" in text and "type: none" in text


def _ensure_crawl_rule(site: dict[str, Any]) -> tuple[bool, bool]:
    site_id = site["id"]
    path = RULES_DIR / f"{site_id}.yaml"
    existed = path.is_file()
    if existed and not _crawl_rule_needs_refresh(path):
        return False, False
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        CRAWL_RULE_TEMPLATE.format(
            site_id=site_id,
            name=site.get("name", site_id),
            entry_url=site.get("url", ""),
        ),
        encoding="utf-8",
    )
    return not existed, existed


def merge_configs(sources: list[dict[str, Any]]) -> dict[str, Any]:
    sites_data = load_yaml(SITES_PATH)
    schedule_data = load_yaml(SCHEDULE_PATH)
    sites: list[dict[str, Any]] = sites_data.get("sites", [])
    jobs: list[dict[str, Any]] = schedule_data.get("jobs", [])

    by_id = {s["id"]: s for s in sites if s.get("id")}
    by_url = {_normalize_url(s.get("url", "")): s for s in sites if s.get("url")}

    job_by_id = {j["site_id"]: j for j in jobs if j.get("site_id")}

    stats = {
        "excel_total": len(sources),
        "sites_created": 0,
        "sites_updated": 0,
        "jobs_created": 0,
        "jobs_updated": 0,
        "rules_created": 0,
        "rules_refreshed": 0,
        "skipped": [],
    }

    for row in sources:
        site_id = row["site_id"]
        norm_url = _normalize_url(row["url"])
        existing = by_id.get(site_id) or by_url.get(norm_url)

        if existing:
            existing["name"] = row["name"]
            existing["url"] = row["url"]
            if row["category"] == "provincial_agri":
                existing["region"] = row["region"]
            note = row.get("notes") or ""
            prefix = "数字农业政策信息源台账"
            existing["notes"] = f"{prefix}：{note}" if note else prefix
            existing["enabled"] = True
            if existing.get("adapter") in (None, "generic"):
                existing["adapter"] = "generic"
                existing["industry"] = True
                existing["fetch_detail"] = True
            site = existing
            stats["sites_updated"] += 1
        else:
            site = _make_site(row)
            sites.append(site)
            by_id[site_id] = site
            by_url[norm_url] = site
            stats["sites_created"] += 1

        created, refreshed = _ensure_crawl_rule(site)
        if created:
            stats["rules_created"] += 1
        elif refreshed:
            stats["rules_refreshed"] += 1

        minutes = 90 if row["category"] == "national" else 120
        sid = site["id"]
        if sid in job_by_id:
            job = job_by_id[sid]
            job["enabled"] = True
            if job.get("minutes") is None:
                job["minutes"] = minutes
            stats["jobs_updated"] += 1
        else:
            job = {
                "site_id": sid,
                "trigger": "interval",
                "minutes": minutes,
                "enabled": True,
            }
            jobs.append(job)
            job_by_id[sid] = job
            stats["jobs_created"] += 1

    sites_data["sites"] = sites
    sites_data["total"] = len(sites)
    meta = sites_data.get("generated_from") or ""
    if "数字农业政策信息源台账.xlsx" not in meta:
        sites_data["generated_from"] = (
            f"{meta}; docs/数字农业政策信息源台账.xlsx".strip("; ")
            if meta
            else "docs/数字农业政策信息源台账.xlsx"
        )

    schedule_data["jobs"] = jobs
    save_yaml(SITES_PATH, sites_data)
    save_yaml(SCHEDULE_PATH, schedule_data)
    return stats


def validate_yaml_files() -> None:
    for path in (SITES_PATH, SCHEDULE_PATH):
        data = load_yaml(path)
        if not isinstance(data, dict):
            raise ValueError(f"{path} 根节点不是 dict")
        if path == SITES_PATH:
            sites = data.get("sites", [])
            if not isinstance(sites, list):
                raise ValueError("sites 不是 list")
            ids = [s.get("id") for s in sites if isinstance(s, dict)]
            if len(ids) != len(set(ids)):
                raise ValueError("sites.yaml 存在重复 site id")
        if path == SCHEDULE_PATH:
            jobs = data.get("jobs", [])
            if not isinstance(jobs, list):
                raise ValueError("jobs 不是 list")


def main() -> None:
    sources = load_agri_policy_sources(EXCEL_PATH)
    stats = merge_configs(sources)
    validate_yaml_files()
    print("数字农业政策信息源台账 导入完成")
    print(f"  Excel 信息源: {stats['excel_total']}")
    print(f"  新建站点: {stats['sites_created']}")
    print(f"  更新站点: {stats['sites_updated']}")
    print(f"  新建调度: {stats['jobs_created']}")
    print(f"  更新调度: {stats['jobs_updated']}")
    print(f"  新建 crawl_rules: {stats['rules_created']}")
    print(f"  刷新 crawl_rules: {stats['rules_refreshed']}")
    if stats["skipped"]:
        print("  跳过条目:")
        for item in stats["skipped"]:
            print(f"    - {item}")


if __name__ == "__main__":
    main()
