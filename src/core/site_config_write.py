"""站点配置写回（sites.yaml / crawl_rules enabled）。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from src.core.crawl_rules import get_rule_loader
from src.core.rule_generator import load_rule_yaml, save_rule_yaml, validate_yaml
from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.site_aliases import resolve_canonical_site_id
from src.core.site_sync import SCHEDULE_PATH, SITES_PATH, get_site_by_id, load_sites_config

ROOT = Path(__file__).resolve().parent.parent.parent
CRAWL_SCRIPTS_DIR = ROOT / "data" / "crawl_scripts"


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """备份后原子写入 YAML 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp.replace(path)


def _write_sites_yaml(config: dict[str, Any]) -> None:
    _atomic_write_yaml(SITES_PATH, config)


def _extract_site_id_from_args(args: Any) -> str | None:
    if not isinstance(args, list):
        return None
    for i, arg in enumerate(args):
        text = str(arg).strip()
        if text in ("--site-id", "--site_id") and i + 1 < len(args):
            return str(args[i + 1]).strip() or None
    return None


def _remove_schedule_jobs(site_id: str) -> bool:
    if not SCHEDULE_PATH.is_file():
        return False
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    jobs = data.get("jobs") or []
    kept = [j for j in jobs if j.get("site_id") != site_id]
    if len(kept) == len(jobs):
        return False
    data["jobs"] = kept
    _atomic_write_yaml(SCHEDULE_PATH, data)
    return True


def _remove_script_cron_jobs_for_site(site_id: str) -> bool:
    from src.core.script_cron import load_script_cron_jobs, save_script_cron_jobs

    jobs = load_script_cron_jobs()
    kept = [j for j in jobs if _extract_site_id_from_args(j.get("args")) != site_id]
    if len(kept) == len(jobs):
        return False
    save_script_cron_jobs(kept)
    return True


def _is_protected_site(site: dict[str, Any]) -> bool:
    return bool(site.get("protected") or site.get("system"))


def delete_site(site_id: str) -> dict[str, Any]:
    """删除站点配置：sites.yaml、crawl_rules、调度引用；不删除已爬取数据。"""
    canonical = resolve_canonical_site_id(site_id)
    config = load_sites_config()
    sites = config.get("sites") or []

    target: dict[str, Any] | None = None
    for site in sites:
        if site.get("id") == canonical:
            target = site
            break
    if target is None:
        return {"ok": False, "error": f"站点不存在: {canonical}"}
    if _is_protected_site(target):
        return {"ok": False, "error": f"站点受保护，不可删除: {canonical}"}

    deleted: list[str] = []

    config["sites"] = [s for s in sites if s.get("id") != canonical]
    _write_sites_yaml(config)
    deleted.append("sites.yaml")

    loader = get_rule_loader()
    rule_path = loader.path_for(canonical)
    if rule_path.is_file():
        rule_path.unlink()
        deleted.append(f"crawl_rules/{canonical}.yaml")

    if _remove_schedule_jobs(canonical):
        deleted.append("schedule.yaml")

    if _remove_script_cron_jobs_for_site(canonical):
        deleted.append("script_cron_jobs.json")

    crawl_script_path = CRAWL_SCRIPTS_DIR / f"{canonical}.json"
    if crawl_script_path.is_file():
        crawl_script_path.unlink()
        deleted.append(f"crawl_scripts/{canonical}.json")

    from src.core.site_credentials import delete_site_credentials
    from src.core.site_onboarding import DRAFTS_DIR, ONBOARDING_DIR

    cred_result = delete_site_credentials(canonical)
    if cred_result.get("ok"):
        deleted.append("site_credentials")

    draft_path = DRAFTS_DIR / f"{canonical}.yaml"
    if draft_path.is_file():
        draft_path.unlink()
        deleted.append(f"sites_drafts/{canonical}.yaml")

    onboarding_path = ONBOARDING_DIR / f"{canonical}.json"
    if onboarding_path.is_file():
        onboarding_path.unlink()
        deleted.append(f"site_onboarding/{canonical}.json")

    return {
        "ok": True,
        "site_id": canonical,
        "site_name": target.get("name") or canonical,
        "deleted": deleted,
        "data_preserved": True,
    }


def update_site_enabled(site_id: str, *, enabled: bool) -> dict[str, Any]:
    """更新 config/sites.yaml 中站点的 enabled 字段。"""
    canonical = resolve_canonical_site_id(site_id)
    config = load_sites_config()
    sites = config.get("sites") or []
    found = False
    for site in sites:
        if site.get("id") == canonical:
            if enabled and not site.get("enabled"):
                from src.billing.quota_middleware import check_entitlement, count_enabled_sites
                from src.billing.tenant_context import get_current_tenant_id

                result = check_entitlement(
                    get_current_tenant_id(),
                    "quota.enabled_sites",
                    delta=1,
                    enabled_sites_count=count_enabled_sites(),
                )
                if not result.allowed:
                    return {
                        "ok": False,
                        "error": "quota_exceeded",
                        "entitlement": "quota.enabled_sites",
                        "used": result.used,
                        "limit": result.limit,
                        "plan_id": result.plan_id,
                    }
            site["enabled"] = bool(enabled)
            found = True
            break
    if not found:
        return {"ok": False, "error": f"站点不在 sites.yaml: {canonical}"}
    _write_sites_yaml(config)
    return {"ok": True, "site_id": canonical, "enabled": bool(enabled)}


def update_rule_enabled(site_id: str, *, enabled: bool) -> dict[str, Any]:
    """更新 crawl_rules YAML 顶层 enabled 字段（保留其余内容）。"""
    canonical = resolve_canonical_site_id(site_id)
    yaml_text = load_rule_yaml(canonical)
    if yaml_text is None:
        return {"ok": False, "error": f"规则文件不存在: {canonical}"}
    flag = "true" if enabled else "false"
    if re.search(r"^enabled:\s*", yaml_text, re.M):
        new_text = re.sub(r"^enabled:\s*\S+", f"enabled: {flag}", yaml_text, count=1, flags=re.M)
    else:
        new_text = re.sub(
            r"(^(?:version|site_id|name):[^\n]+\n)",
            rf"\1enabled: {flag}\n",
            yaml_text,
            count=1,
            flags=re.M,
        )
        if new_text == yaml_text:
            new_text = f"enabled: {flag}\n{yaml_text}"
    result = save_rule_yaml(canonical, new_text)
    if not result.get("ok"):
        return {"ok": False, "errors": result.get("errors", [])}
    return {"ok": True, "site_id": canonical, "enabled": bool(enabled), "path": result.get("path")}


def get_site_schedule_status(site_id: str) -> dict[str, Any]:
    """返回站点规则与调度资格摘要。"""
    canonical = resolve_canonical_site_id(site_id)
    site = get_site_by_id(canonical)
    if not site:
        return {"ok": False, "error": f"站点不存在: {canonical}"}

    loader = get_rule_loader()
    rule_path = loader.path_for(canonical)
    has_rule = rule_path.is_file()
    rule_valid = False
    rule_errors: list[str] = []
    if has_rule:
        yaml_text = rule_path.read_text(encoding="utf-8")
        validation = validate_yaml(yaml_text, site_id=canonical)
        rule_valid = bool(validation.get("valid"))
        rule_errors = validation.get("errors") or []

    eligible = site_eligible_for_schedule(site)
    return {
        "ok": True,
        "site_id": canonical,
        "site_enabled": bool(site.get("enabled")),
        "adapter": site.get("adapter", "generic"),
        "has_rule_file": has_rule,
        "rule_valid": rule_valid,
        "rule_errors": rule_errors,
        "schedule_eligible": eligible,
        "rule_path": str(rule_path.relative_to(ROOT)) if has_rule else None,
    }


def enable_site_after_probe(
    site_id: str,
    *,
    enable_site: bool = True,
    enable_rule: bool = True,
    require_schedule_eligible: bool = True,
) -> dict[str, Any]:
    """试跑通过后启用站点与规则（可选校验 schedule_eligible）。"""
    canonical = resolve_canonical_site_id(site_id)
    status = get_site_schedule_status(canonical)
    if not status.get("ok"):
        return status

    if require_schedule_eligible and not status.get("schedule_eligible"):
        return {
            "ok": False,
            "site_id": canonical,
            "error": "规则未通过资格校验（generic 需有效 list_page）",
            "schedule_status": status,
        }

    results: dict[str, Any] = {"ok": True, "site_id": canonical, "updates": []}
    if enable_rule:
        rule_result = update_rule_enabled(canonical, enabled=True)
        results["updates"].append({"type": "rule_enabled", **rule_result})
        if not rule_result.get("ok"):
            results["ok"] = False
            results["error"] = rule_result.get("error") or rule_result.get("errors")
            return results

    if enable_site:
        site_result = update_site_enabled(canonical, enabled=True)
        results["updates"].append({"type": "site_enabled", **site_result})
        if not site_result.get("ok"):
            results["ok"] = False
            results["error"] = site_result.get("error")
            return results

    results["schedule_status"] = get_site_schedule_status(canonical)
    return results
