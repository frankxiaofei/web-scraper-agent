"""商机配置 SOP 向导服务 — 领域→关键词→站点→定时爬取→推送。"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.biz_clue_config import SOURCES_PATH, clear_config_cache, load_biz_clue_sources
from src.core.chat_scheduled_tasks import (
    TASK_TYPE_HERMES_SUMMARY,
    TASK_TYPE_INCREMENTAL,
    create_chat_scheduled_task,
)
from src.core.site_sync import load_sites_config
from src.core.timezone_utils import app_now

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SOP_DATA_DIR = ROOT / "data" / "biz_clue_sop"
SOP_SESSIONS_DIR = SOP_DATA_DIR / "sessions"
DOMAINS_PATH = ROOT / "config" / "biz_clue_sop_domains.yaml"

SOP_STEPS = ("domain", "keywords", "sites", "tasks", "notify", "done")
DEFAULT_SYNC_CRON = "30 6 * * *"
DEFAULT_NOTIFY_CRON = "0 8 * * *"
MAX_CRAWL_TASKS_PER_SOP = 10

_service: Optional["BizClueSopService"] = None


def _now_iso() -> str:
    return app_now().isoformat()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("加载 YAML 失败 %s: %s", path, exc)
        return {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def load_domain_templates() -> dict[str, dict[str, Any]]:
    """加载领域模板。"""
    cfg = _read_yaml(DOMAINS_PATH)
    domains = cfg.get("domains") or {}
    return {str(k): v for k, v in domains.items() if isinstance(v, dict)}


def _session_path(session_id: str) -> Path:
    return SOP_SESSIONS_DIR / f"{session_id}.json"


def _save_session(session: dict[str, Any]) -> dict[str, Any]:
    session["updated_at"] = _now_iso()
    path = _session_path(session["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return session


def _load_session(session_id: str) -> Optional[dict[str, Any]]:
    path = _session_path(session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_keywords(keywords: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in keywords:
        kw = str(raw).strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def generate_keywords_for_domain(
    domain: str,
    *,
    custom_keywords: Optional[list[str]] = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    """基于领域模板生成关键词清单（规则优先，可选 LLM 增强）。"""
    templates = load_domain_templates()
    template = templates.get(domain) or {}
    base_keywords = list(template.get("keywords") or [])
    stage_keywords = list(template.get("stage_keywords") or [])
    exclude_keywords = list(template.get("exclude_keywords") or [])

    if not base_keywords and domain:
        base_keywords = [domain]
        for part in re.split(r"[/、,，\s]+", domain):
            part = part.strip()
            if part and part not in base_keywords:
                base_keywords.append(part)

    if custom_keywords:
        base_keywords = _normalize_keywords(custom_keywords + base_keywords)

    search_keywords = _normalize_keywords(base_keywords + stage_keywords[:6])
    source = "template"
    llm_note = ""

    if use_llm and os.environ.get("OPENAI_API_KEY"):
        llm_kws = _llm_enhance_keywords(domain, base_keywords)
        if llm_kws:
            search_keywords = _normalize_keywords(search_keywords + llm_kws)
            source = "template+llm"
            llm_note = f"LLM 补充 {len(llm_kws)} 个关键词"

    return {
        "domain": domain,
        "search_keywords": search_keywords,
        "product_keywords": base_keywords,
        "stage_keywords": stage_keywords,
        "exclude_keywords": exclude_keywords,
        "source": source,
        "llm_note": llm_note,
    }


def _llm_enhance_keywords(domain: str, existing: list[str]) -> list[str]:
    """可选 LLM 增强关键词（无 key 时跳过）。"""
    try:
        from openai import OpenAI

        client = OpenAI()
        prompt = (
            f"领域：{domain}\n"
            f"已有词：{', '.join(existing[:12])}\n"
            "请补充 5-8 个中文招标检索关键词，每行一个，不要编号。"
        )
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
        return _normalize_keywords([ln.strip() for ln in text.splitlines() if ln.strip()][:10])
    except Exception as exc:
        logger.info("SOP LLM 关键词增强跳过: %s", exc)
        return []


def score_site_for_keywords(
    site: dict[str, Any],
    keywords: list[str],
    *,
    domain_template: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """站点与关键词匹配打分。"""
    site_id = str(site.get("id") or "")
    name = str(site.get("name") or "")
    notes = str(site.get("notes") or "")
    category = str(site.get("category") or "")
    region = str(site.get("region") or "")
    text = f"{name} {notes} {category} {region}".lower()

    score = 0
    matched: list[str] = []
    for kw in keywords:
        if kw.lower() in text or kw in name or kw in notes:
            score += 12
            matched.append(kw)

    template = domain_template or {}
    for hint in template.get("site_name_hints") or []:
        if hint in name or hint in notes:
            score += 8
            matched.append(hint)

    preferred_cats = set(template.get("site_categories") or [])
    if category in preferred_cats:
        score += 10

    if site.get("enabled"):
        score += 3
    if site.get("mvp"):
        score += 2
    if site.get("industry"):
        score += 1

    if "gov" in name or "政府采购" in name or "公共资源" in name:
        score += 6

    return {
        "site_id": site_id,
        "name": name,
        "category": category,
        "region": region or "—",
        "enabled": bool(site.get("enabled")),
        "score": score,
        "matched_keywords": _normalize_keywords(matched)[:8],
        "url": site.get("url") or "",
    }


def match_sites_for_keywords(
    keywords: list[str],
    *,
    domain: str = "",
    limit: int = 30,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    """从站点库中匹配相关站点。"""
    templates = load_domain_templates()
    template = templates.get(domain) or {}
    config = load_sites_config()
    sites = config.get("sites") or []

    scored: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        if enabled_only and not site.get("enabled"):
            continue
        item = score_site_for_keywords(site, keywords, domain_template=template)
        if item["score"] > 0:
            scored.append(item)

    scored.sort(key=lambda x: (x["score"], x.get("name") or ""), reverse=True)
    return scored[:limit]


def _build_notify_prompt(domain: str, keywords: list[str], site_names: list[str]) -> str:
    kw_text = "、".join(keywords[:10])
    site_text = "、".join(site_names[:8]) or "已选站点"
    return (
        f"请分析「{domain}」领域近 1 天新增招标公告与商机线索。\n\n"
        f"检索关键词：{kw_text}\n"
        f"已接入站点：{site_text}\n\n"
        "要求：\n"
        "1. 统计今日标讯总量，按站点分布列出 Top 5\n"
        "2. 提炼 3–5 条与领域最相关的重点标讯（项目名称、预算/金额、站点）\n"
        "3. 给出 2–3 句综合结论与关注建议\n\n"
        "输出格式：先写完整分析，最后用「## 飞书摘要」标题给出 200 字以内的群推送摘要。"
    )


def apply_sop_to_biz_clue_sources(
    *,
    search_keywords: list[str],
    site_ids: list[str],
) -> dict[str, Any]:
    """将 SOP 结果写入 biz_clue_sources.yaml。"""
    sources = _read_yaml(SOURCES_PATH)
    if not sources:
        sources = dict(load_biz_clue_sources())

    existing_kws = list(sources.get("search_keywords") or [])
    merged_kws = _normalize_keywords(search_keywords + existing_kws)
    sources["search_keywords"] = merged_kws[:24]

    existing_sites = list(sources.get("default_sync_site_ids") or [])
    merged_sites: list[str] = []
    seen: set[str] = set()
    for sid in site_ids + existing_sites:
        sid = str(sid).strip()
        if sid and sid not in seen:
            seen.add(sid)
            merged_sites.append(sid)
    sources["default_sync_site_ids"] = merged_sites

    _write_yaml(SOURCES_PATH, sources)
    clear_config_cache()

    return {
        "search_keywords_count": len(sources["search_keywords"]),
        "sync_site_count": len(merged_sites),
        "sources_path": str(SOURCES_PATH),
    }


class BizClueSopService:
    """商机配置 SOP 会话管理。"""

    def list_domains(self) -> dict[str, Any]:
        templates = load_domain_templates()
        items = [
            {
                "name": name,
                "description": tpl.get("description") or "",
                "keyword_count": len(tpl.get("keywords") or []),
            }
            for name, tpl in templates.items()
        ]
        return {"domains": items, "custom_allowed": True}

    def start_session(
        self,
        *,
        domain: str,
        custom_domain: str = "",
        use_llm: bool = False,
    ) -> dict[str, Any]:
        domain = (domain or custom_domain or "").strip()
        if not domain:
            return {"ok": False, "error": "请提供领域名称"}

        templates = load_domain_templates()
        effective_domain = domain if domain in templates else domain

        kw_result = generate_keywords_for_domain(
            effective_domain,
            use_llm=use_llm and bool(os.environ.get("OPENAI_API_KEY")),
        )

        session_id = str(uuid.uuid4())
        session: dict[str, Any] = {
            "id": session_id,
            "domain": effective_domain,
            "custom_domain": custom_domain.strip(),
            "step": 1,
            "current_step": "domain",
            "status": "in_progress",
            "keywords": kw_result,
            "matched_sites": [],
            "selected_site_ids": [],
            "tasks": {
                "cron": DEFAULT_SYNC_CRON,
                "created_task_ids": [],
                "applied_sources": None,
            },
            "notify": {
                "cron": DEFAULT_NOTIFY_CRON,
                "task_id": None,
                "feishu_push": True,
                "task_name": f"{effective_domain} 商机日报",
            },
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "completed_at": None,
        }
        _save_session(session)

        return {
            "ok": True,
            "session": self.serialize_session(session),
            "keywords_preview": kw_result,
            "next_step": "keywords",
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = _load_session(session_id)
        if not session:
            return {"ok": False, "error": f"SOP 会话不存在: {session_id}"}
        return {"ok": True, "session": self.serialize_session(session)}

    def save_keywords(
        self,
        session_id: str,
        *,
        search_keywords: Optional[list[str]] = None,
        exclude_keywords: Optional[list[str]] = None,
        regenerate: bool = False,
        use_llm: bool = False,
    ) -> dict[str, Any]:
        session = _load_session(session_id)
        if not session:
            return {"ok": False, "error": f"SOP 会话不存在: {session_id}"}

        if regenerate:
            kw_result = generate_keywords_for_domain(
                session["domain"],
                custom_keywords=search_keywords,
                use_llm=use_llm,
            )
            session["keywords"] = kw_result
        else:
            kw = session.setdefault("keywords", {})
            if search_keywords is not None:
                kw["search_keywords"] = _normalize_keywords(search_keywords)
            if exclude_keywords is not None:
                kw["exclude_keywords"] = _normalize_keywords(exclude_keywords)

        session["step"] = max(session.get("step", 1), 2)
        session["current_step"] = "keywords"
        _save_session(session)

        return {
            "ok": True,
            "session": self.serialize_session(session),
            "next_step": "sites",
        }

    def match_and_save_sites(
        self,
        session_id: str,
        *,
        selected_site_ids: Optional[list[str]] = None,
        rematch: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        session = _load_session(session_id)
        if not session:
            return {"ok": False, "error": f"SOP 会话不存在: {session_id}"}

        keywords = list((session.get("keywords") or {}).get("search_keywords") or [])
        if rematch or not session.get("matched_sites"):
            matched = match_sites_for_keywords(
                keywords,
                domain=session.get("domain") or "",
                limit=limit,
            )
            session["matched_sites"] = matched

        if selected_site_ids is not None:
            allowed = {s["site_id"] for s in session.get("matched_sites") or []}
            all_sites = {s.get("id") for s in load_sites_config().get("sites") or []}
            selected = [
                sid for sid in selected_site_ids
                if sid in allowed or sid in all_sites
            ]
            session["selected_site_ids"] = selected
        elif not session.get("selected_site_ids"):
            auto = [s["site_id"] for s in (session.get("matched_sites") or [])[:12]]
            session["selected_site_ids"] = auto

        session["step"] = max(session.get("step", 1), 3)
        session["current_step"] = "sites"
        _save_session(session)

        return {
            "ok": True,
            "session": self.serialize_session(session),
            "matched_count": len(session.get("matched_sites") or []),
            "selected_count": len(session.get("selected_site_ids") or []),
            "next_step": "tasks",
        }

    def create_crawl_tasks(
        self,
        session_id: str,
        *,
        cron: Optional[str] = None,
        apply_sources: bool = True,
        create_incremental_tasks: bool = True,
    ) -> dict[str, Any]:
        session = _load_session(session_id)
        if not session:
            return {"ok": False, "error": f"SOP 会话不存在: {session_id}"}

        site_ids = list(session.get("selected_site_ids") or [])
        if not site_ids:
            return {"ok": False, "error": "请先选择至少一个站点"}

        cron_expr = (cron or session.get("tasks", {}).get("cron") or DEFAULT_SYNC_CRON).strip()
        keywords = list((session.get("keywords") or {}).get("search_keywords") or [])
        created_ids: list[str] = []
        errors: list[str] = []

        applied = None
        if apply_sources:
            try:
                applied = apply_sop_to_biz_clue_sources(
                    search_keywords=keywords,
                    site_ids=site_ids,
                )
                session.setdefault("tasks", {})["applied_sources"] = applied
            except OSError as exc:
                errors.append(f"写入 biz_clue_sources 失败: {exc}")

        if create_incremental_tasks:
            for sid in site_ids[:MAX_CRAWL_TASKS_PER_SOP]:
                try:
                    task = create_chat_scheduled_task(
                        task_type=TASK_TYPE_INCREMENTAL,
                        site_id=sid,
                        cron=cron_expr,
                        name=f"{session['domain']}·{sid} 增量爬取",
                        report_type="bim",
                        feishu_push=False,
                        max_items=20,
                        enabled=True,
                        created_by=f"biz_clue_sop:{session_id}",
                    )
                    created_ids.append(task["id"])
                except ValueError as exc:
                    errors.append(f"{sid}: {exc}")

        session.setdefault("tasks", {})["cron"] = cron_expr
        session["tasks"]["created_task_ids"] = list(
            set((session["tasks"].get("created_task_ids") or []) + created_ids)
        )
        session["step"] = max(session.get("step", 1), 4)
        session["current_step"] = "tasks"
        _save_session(session)

        return {
            "ok": True,
            "session": self.serialize_session(session),
            "created_task_ids": created_ids,
            "applied_sources": applied,
            "errors": errors,
            "next_step": "notify",
        }

    def configure_notify(
        self,
        session_id: str,
        *,
        cron: Optional[str] = None,
        task_name: Optional[str] = None,
        feishu_push: bool = True,
        feishu_webhook_url: Optional[str] = None,
    ) -> dict[str, Any]:
        session = _load_session(session_id)
        if not session:
            return {"ok": False, "error": f"SOP 会话不存在: {session_id}"}

        cron_expr = (cron or session.get("notify", {}).get("cron") or DEFAULT_NOTIFY_CRON).strip()
        name = (task_name or session.get("notify", {}).get("task_name") or f"{session['domain']} 商机日报").strip()
        keywords = list((session.get("keywords") or {}).get("search_keywords") or [])
        site_ids = list(session.get("selected_site_ids") or [])
        site_names = [
            (s.get("name") or s["site_id"])
            for s in (session.get("matched_sites") or [])
            if s.get("site_id") in site_ids
        ]
        prompt = _build_notify_prompt(session["domain"], keywords, site_names)

        try:
            task = create_chat_scheduled_task(
                task_type=TASK_TYPE_HERMES_SUMMARY,
                cron=cron_expr,
                name=name,
                hermes_prompt=prompt,
                feishu_push=feishu_push,
                feishu_webhook_url=feishu_webhook_url,
                enabled=True,
                created_by=f"biz_clue_sop:{session_id}",
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        session.setdefault("notify", {})
        session["notify"]["cron"] = cron_expr
        session["notify"]["task_name"] = name
        session["notify"]["feishu_push"] = feishu_push
        session["notify"]["task_id"] = task["id"]
        session["step"] = 5
        session["current_step"] = "notify"
        session["status"] = "completed"
        session["completed_at"] = _now_iso()
        _save_session(session)

        return {
            "ok": True,
            "session": self.serialize_session(session),
            "notify_task": task,
            "next_step": "done",
        }

    def serialize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        step = int(session.get("step") or 1)
        return {
            "id": session.get("id"),
            "domain": session.get("domain"),
            "custom_domain": session.get("custom_domain"),
            "step": step,
            "current_step": session.get("current_step") or SOP_STEPS[min(step, len(SOP_STEPS) - 1)],
            "status": session.get("status") or "in_progress",
            "keywords": session.get("keywords") or {},
            "matched_sites": session.get("matched_sites") or [],
            "selected_site_ids": session.get("selected_site_ids") or [],
            "tasks": session.get("tasks") or {},
            "notify": session.get("notify") or {},
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "completed_at": session.get("completed_at"),
        }


def get_biz_clue_sop_service() -> BizClueSopService:
    global _service
    if _service is None:
        _service = BizClueSopService()
    return _service


__all__ = [
    "BizClueSopService",
    "apply_sop_to_biz_clue_sources",
    "generate_keywords_for_domain",
    "get_biz_clue_sop_service",
    "load_domain_templates",
    "match_sites_for_keywords",
    "score_site_for_keywords",
]
