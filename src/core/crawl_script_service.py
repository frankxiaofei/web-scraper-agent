"""生成与执行爬取脚本：分析阶段可用 LLM，执行阶段无 LLM。"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from src.core.crawl_rules import get_rule_loader
from src.core.rule_generator import (
    RuleGenerator,
    get_rule_generator,
    save_rule_yaml,
    validate_yaml,
)
from src.core.site_sync import get_site_by_id, load_sites_config, sync_site

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
METADATA_DIR = ROOT / "data" / "crawl_scripts"
DRAFTS_DIR = ROOT / "config" / "sites_drafts"
GENERATED_PY_DIR = ROOT / "scripts" / "generated"

CrawlScriptType = Literal["yaml_rule", "python_script"]


class CrawlScriptMetadata(BaseModel):
    """已生成爬取脚本的持久化元数据。"""

    site_id: str
    version: int = 1
    created_at: str
    updated_at: str
    source_url: str = ""
    script_type: CrawlScriptType = "yaml_rule"
    script_path: str
    valid: bool = True
    rule_summary: dict[str, Any] = Field(default_factory=dict)
    generation_source: str = "llm"
    notes: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_PY_DIR.mkdir(parents=True, exist_ok=True)


def _metadata_path(site_id: str) -> Path:
    return METADATA_DIR / f"{site_id}.json"


def _resolve_under_root(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    raise ValueError(f"路径必须在项目目录内: {path}")


def resolve_site_config(site_id: str) -> Optional[dict[str, Any]]:
    """从 sites.yaml 或 onboarding draft 解析站点配置。"""
    site = get_site_by_id(site_id)
    if site:
        return site

    draft_path = DRAFTS_DIR / f"{site_id}.yaml"
    if not draft_path.is_file():
        return None

    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    with open(draft_path, encoding="utf-8") as f:
        draft = yaml.safe_load(f) or {}
    return {**crawl_defaults, **draft}


def load_crawl_script_metadata(site_id: str) -> Optional[CrawlScriptMetadata]:
    path = _metadata_path(site_id)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return CrawlScriptMetadata.model_validate(json.load(f))


def save_crawl_script_metadata(metadata: CrawlScriptMetadata) -> CrawlScriptMetadata:
    _ensure_dirs()
    metadata.updated_at = _now_iso()
    with open(_metadata_path(metadata.site_id), "w", encoding="utf-8") as f:
        json.dump(metadata.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    return metadata


def build_rule_summary(preview: Optional[dict[str, Any]]) -> dict[str, Any]:
    """从 CrawlRule preview 生成 UI 摘要。"""
    if not preview:
        return {}
    list_page = preview.get("list_page") or {}
    pagination = preview.get("pagination") or {}
    detail = preview.get("detail") or {}
    limits = preview.get("limits") or {}
    return {
        "site_id": preview.get("site_id"),
        "name": preview.get("name"),
        "entry_url": preview.get("entry_url"),
        "list_strategy": list_page.get("strategy"),
        "pagination_type": pagination.get("type"),
        "max_items": limits.get("max_items"),
        "max_pages": limits.get("max_pages"),
        "fetch_detail": detail.get("fetch_detail"),
        "enabled": preview.get("enabled", True),
    }


def _rule_relative_path(site_id: str) -> str:
    path = get_rule_loader().path_for(site_id)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return f"config/crawl_rules/{site_id}.yaml"


def _python_script_path(site_id: str) -> Path:
    safe_id = re.sub(r"[^\w.-]+", "_", site_id).strip("_") or "site"
    return GENERATED_PY_DIR / f"{safe_id}_crawl.py"


def validate_generated_python_script(site_id: str) -> Path:
    """校验 scripts/generated/{site_id}_crawl.py 存在且在白名单目录内。"""
    path = _resolve_under_root(_python_script_path(site_id))
    gen_root = _resolve_under_root(GENERATED_PY_DIR)
    if gen_root not in path.parents and path.parent != gen_root:
        raise ValueError(f"生成脚本必须在 scripts/generated/ 下: {path}")
    if not path.is_file():
        raise ValueError(f"生成脚本不存在: {path.relative_to(ROOT)}")
    return path


def save_python_crawl_script(site_id: str, code: str, *, source_url: str = "") -> dict[str, Any]:
    """写入白名单目录下的 Python 爬取脚本并更新 metadata。"""
    from src.core.code_executor import validate_generated_code

    validate_generated_code(code)
    _ensure_dirs()
    path = _python_script_path(site_id)
    _resolve_under_root(path)
    path.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
    rel_path = str(path.relative_to(ROOT))

    now = _now_iso()
    existing = load_crawl_script_metadata(site_id)
    metadata = CrawlScriptMetadata(
        site_id=site_id,
        version=(existing.version + 1) if existing else 1,
        created_at=existing.created_at if existing else now,
        updated_at=now,
        source_url=source_url or (existing.source_url if existing else ""),
        script_type="python_script",
        script_path=rel_path,
        valid=True,
        generation_source="llm",
    )
    save_crawl_script_metadata(metadata)
    return {"ok": True, "path": rel_path, "metadata": metadata.model_dump(mode="json")}


def save_yaml_crawl_script(
    site_id: str,
    yaml_text: str,
    *,
    source_url: str = "",
    generation_source: str = "llm",
) -> dict[str, Any]:
    """校验并保存 YAML 规则，同时写入 metadata。"""
    save_result = save_rule_yaml(site_id, yaml_text)
    if not save_result.get("ok"):
        return {"ok": False, "errors": save_result.get("errors", [])}

    validation = validate_yaml(yaml_text, site_id=site_id)
    summary = build_rule_summary(validation.get("preview"))
    now = _now_iso()
    existing = load_crawl_script_metadata(site_id)
    rel_path = _rule_relative_path(site_id)
    metadata = CrawlScriptMetadata(
        site_id=site_id,
        version=(existing.version + 1) if existing else 1,
        created_at=existing.created_at if existing else now,
        updated_at=now,
        source_url=source_url or (existing.source_url if existing else ""),
        script_type="yaml_rule",
        script_path=rel_path,
        valid=True,
        rule_summary=summary,
        generation_source=generation_source,
    )
    save_crawl_script_metadata(metadata)
    return {
        "ok": True,
        "path": save_result.get("path") or rel_path,
        "metadata": metadata.model_dump(mode="json"),
        "rule_summary": summary,
    }


async def generate_crawl_script(
    *,
    site_id: str,
    url: str = "",
    messages: Optional[list[dict[str, str]]] = None,
    page_hints: str = "",
    save: bool = False,
    dry_run: bool = False,
    max_items: int = 5,
    max_pages: int = 1,
    generator: Optional[RuleGenerator] = None,
) -> dict[str, Any]:
    """分析 + 生成爬取规则（一次性 LLM），可选保存与试跑。"""
    site = resolve_site_config(site_id)
    if not site:
        return {"ok": False, "error": f"站点不存在: {site_id}"}

    source_url = url or str(site.get("url") or "")
    gen = generator or get_rule_generator()
    gen_result = await gen.generate(
        site_id=site_id,
        url=source_url,
        messages=messages or [],
        page_hints=page_hints,
    )

    payload: dict[str, Any] = {
        "ok": bool(gen_result.get("valid")),
        "site_id": site_id,
        "source_url": source_url,
        "yaml": gen_result.get("yaml") or "",
        "valid": gen_result.get("valid"),
        "errors": gen_result.get("errors") or [],
        "preview": gen_result.get("preview"),
        "rule_summary": build_rule_summary(gen_result.get("preview")),
        "script_type": "yaml_rule",
        "saved": False,
        "dry_run": None,
    }
    if gen_result.get("llm_error"):
        payload["llm_error"] = gen_result["llm_error"]

    if save and gen_result.get("valid") and gen_result.get("yaml"):
        save_result = save_yaml_crawl_script(
            site_id,
            gen_result["yaml"],
            source_url=source_url,
            generation_source="llm",
        )
        payload["saved"] = save_result.get("ok", False)
        if save_result.get("path"):
            payload["path"] = save_result["path"]
        if save_result.get("metadata"):
            payload["metadata"] = save_result["metadata"]
        if not save_result.get("ok"):
            payload["save_errors"] = save_result.get("errors", [])
            payload["ok"] = False

        try:
            from src.core.site_onboarding import mark_onboarding_stage_completed

            mark_onboarding_stage_completed(site_id, "analyze")
            mark_onboarding_stage_completed(site_id, "toolchain")
        except Exception:
            logger.debug("更新 onboarding 阶段失败（可忽略）", exc_info=True)

    if dry_run and gen_result.get("valid"):
        from src.core.crawl_rule_dry_run import dry_run_crawl_rule

        yaml_for_run = gen_result.get("yaml") if not save else None
        dry_result = await dry_run_crawl_rule(
            site_id,
            yaml_text=yaml_for_run,
            max_items=max_items,
            max_pages=max_pages,
        )
        payload["dry_run"] = dry_result
        payload["dry_run_ok"] = dry_result.get("success")

    return payload


async def run_generated_crawl(
    site_id: str,
    *,
    dry_run: bool = False,
    max_items: Optional[int] = None,
    persist: bool = True,
    trigger: str = "generated_script",
) -> dict[str, Any]:
    """读取已生成规则/脚本并执行，全程不调用 LLM。"""
    metadata = load_crawl_script_metadata(site_id)
    site = resolve_site_config(site_id)
    if not site:
        return {"ok": False, "site_id": site_id, "error": "站点不存在"}

    if metadata is None:
        loader = get_rule_loader()
        rule_path = loader.path_for(site_id)
        if rule_path.is_file():
            metadata = CrawlScriptMetadata(
                site_id=site_id,
                version=1,
                created_at=_now_iso(),
                updated_at=_now_iso(),
                source_url=str(site.get("url") or ""),
                script_type="yaml_rule",
                script_path=str(rule_path.relative_to(ROOT)),
                valid=True,
            )
        else:
            return {
                "ok": False,
                "site_id": site_id,
                "error": "未找到已生成的爬取脚本或规则，请先调用 generate",
            }

    if metadata.script_type == "python_script":
        return _run_python_script(site_id, metadata, dry_run=dry_run)

    if dry_run or not persist:
        from src.core.crawl_rule_dry_run import dry_run_crawl_rule

        cap = max_items if max_items is not None else 20
        dry_result = await dry_run_crawl_rule(
            site_id,
            max_items=cap,
            max_pages=1,
        )
        return {
            "ok": dry_result.get("success", False),
            "site_id": site_id,
            "mode": "dry_run",
            "script_type": metadata.script_type,
            "script_path": metadata.script_path,
            "llm_used": False,
            **dry_result,
        }

    effective_max = max_items
    if effective_max is None:
        from src.core.site_sync import resolve_max_items

        effective_max = resolve_max_items(site)

    result = await sync_site(
        site,
        max_items=effective_max,
        update_status=True,
        trigger=trigger,
    )
    return {
        "ok": result.get("success", False),
        "site_id": site_id,
        "mode": "sync",
        "script_type": metadata.script_type,
        "script_path": metadata.script_path,
        "llm_used": False,
        **result,
    }


def _run_python_script(
    site_id: str,
    metadata: CrawlScriptMetadata,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    try:
        script_path = validate_generated_python_script(site_id)
    except ValueError as exc:
        return {"ok": False, "site_id": site_id, "error": str(exc)}

    cmd = [sys.executable, str(script_path)]
    if dry_run and "--dry-run" not in cmd:
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=7200,
            shell=False,
        )
        ok = proc.returncode == 0
        return {
            "ok": ok,
            "site_id": site_id,
            "mode": "subprocess",
            "script_type": "python_script",
            "script_path": metadata.script_path,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-2000:],
            "llm_used": False,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "site_id": site_id, "error": "执行超时 (7200s)"}
    except Exception as exc:
        return {"ok": False, "site_id": site_id, "error": str(exc)}


def get_crawl_script_status(site_id: str) -> dict[str, Any]:
    """返回 metadata + 规则摘要，供 UI / Hermes 展示。"""
    metadata = load_crawl_script_metadata(site_id)
    site = resolve_site_config(site_id)
    yaml_text = None
    loader = get_rule_loader()
    rule_path = loader.path_for(site_id)
    if rule_path.is_file():
        yaml_text = rule_path.read_text(encoding="utf-8")
        validation = validate_yaml(yaml_text, site_id=site_id)
    else:
        validation = {"valid": False, "errors": ["规则文件不存在"], "preview": None}

    return {
        "ok": True,
        "site_id": site_id,
        "site": {"id": site_id, "name": (site or {}).get("name"), "url": (site or {}).get("url")}
        if site
        else None,
        "metadata": metadata.model_dump(mode="json") if metadata else None,
        "has_rule_file": rule_path.is_file(),
        "rule_valid": validation.get("valid"),
        "rule_summary": metadata.rule_summary if metadata else build_rule_summary(validation.get("preview")),
        "rule_errors": validation.get("errors") or [],
    }
