#!/usr/bin/env python3
"""一次性补丁：飞书四 Tab 站点扩展（industry/soe 标记 + schedule jobs + 占位 crawl_rules）。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"
RULES_DIR = ROOT / "config" / "crawl_rules"

# generic 全国站需 WebBridge 编写规则前的占位
GENERIC_NATIONAL_STUBS = (
    "zcygov_national",
    "bidnews_national",
    "okcis_national",
    "bidcenter_national",
)

STUB_RULE = """version: 1
site_id: {site_id}
name: {name}
enabled: false
# 占位规则：选择器待 WebBridge 可视化配置向导填写
entry_url: {entry_url}
list_page:
  strategy: dom
  wait_for: "body"
  container: "body"
  item: "a[href]"
  title: "a"
  link: "a[href]"
pagination:
  type: none
detail:
  fetch_detail: false
limits:
  max_pages: 1
  max_items: 5
  max_depth: 1
  rate_limit_seconds: 2.0
"""


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def patch_sites(sites_data: dict) -> tuple[int, int]:
    """返回 (industry_added, notes_updated)。"""
    industry_added = 0
    notes_updated = 0
    for site in sites_data.get("sites", []):
        cat = site.get("category", "")
        if cat in ("provincial_gov", "provincial_ggzy"):
            if not site.get("industry"):
                site["industry"] = True
                industry_added += 1
            note = site.get("notes") or ""
            if "BIM" not in note and "行业扩展" not in note:
                prefix = "行业扩展：省级政采；" if cat == "provincial_gov" else "行业扩展：省级公共资源；"
                site["notes"] = (prefix + note).strip("；")
                notes_updated += 1
        elif cat == "national" and site.get("industry") is None:
            site["industry"] = True
            industry_added += 1
        elif cat == "enterprise" and site.get("id") != "dlzb_power":
            # 央企：显式 soe 标记（parent 非空时 is_soe_site 已生效，便于 UI 展示）
            if "soe" not in site and site.get("parent"):
                site["soe"] = False
    sites_data["total"] = len(sites_data.get("sites", []))
    return industry_added, notes_updated


def build_schedule_jobs(sites_data: dict, existing_jobs: list[dict]) -> list[dict]:
    """按 Tab 分组重建 jobs 列表，保留已有 minutes 覆盖。"""
    existing = {j["site_id"]: j for j in existing_jobs if j.get("site_id")}
    sites = sites_data.get("sites", [])

    def job_for(site: dict, default_minutes: int = 60) -> dict:
        sid = site["id"]
        prev = existing.get(sid, {})
        minutes = prev.get("minutes", default_minutes)
        return {
            "site_id": sid,
            "trigger": "interval",
            "minutes": minutes,
            "enabled": bool(site.get("enabled")),
        }

    sections: list[tuple[str, list[dict]]] = []

    national = [s for s in sites if s.get("category") == "national"]
    sections.append(("全国级招标采购平台（Tab1）", national))

    prov_gov = sorted(
        [s for s in sites if s.get("category") == "provincial_gov"],
        key=lambda s: s.get("region", ""),
    )
    prov_ggzy = sorted(
        [s for s in sites if s.get("category") == "provincial_ggzy"],
        key=lambda s: s.get("region", ""),
    )
    sections.append(("省级政府采购网（Tab2）", prov_gov))
    sections.append(("省级公共资源交易平台（Tab2）", prov_ggzy))

    enterprise = [s for s in sites if s.get("category") == "enterprise"]
    sections.append(("中央企业采购平台（Tab3）", enterprise))

    jobs: list[dict] = []
    for _title, group in sections:
        for site in group:
            minutes = 90 if site.get("category") == "enterprise" else 60
            jobs.append(job_for(site, default_minutes=minutes))

    return jobs


def write_stub_rules(sites_data: dict) -> list[str]:
    created: list[str] = []
    site_map = {s["id"]: s for s in sites_data.get("sites", [])}
    for sid in GENERIC_NATIONAL_STUBS:
        path = RULES_DIR / f"{sid}.yaml"
        if path.exists():
            continue
        site = site_map.get(sid)
        if not site:
            continue
        content = STUB_RULE.format(
            site_id=sid,
            name=site.get("name", sid),
            entry_url=site.get("url", ""),
        )
        path.write_text(content, encoding="utf-8")
        created.append(sid)
    return created


def main() -> None:
    sites_data = load_yaml(SITES_PATH)
    schedule_data = load_yaml(SCHEDULE_PATH)

    industry_added, notes_updated = patch_sites(sites_data)
    save_yaml(SITES_PATH, sites_data)

    old_jobs = schedule_data.get("jobs", [])
    new_jobs = build_schedule_jobs(sites_data, old_jobs)
    schedule_data["jobs"] = new_jobs
    save_yaml(SCHEDULE_PATH, schedule_data)

    created_rules = write_stub_rules(sites_data)

    enabled_sites = [s["id"] for s in sites_data["sites"] if s.get("enabled")]
    print(f"sites.yaml: industry 标记 +{industry_added}, notes 更新 {notes_updated}")
    print(f"schedule.yaml: {len(new_jobs)} 条 job（原 {len(old_jobs)}）")
    print(f"crawl_rules 占位新建: {created_rules or '无'}")
    print(f"当前 enabled 站点 ({len(enabled_sites)}): {', '.join(enabled_sites)}")


if __name__ == "__main__":
    main()
