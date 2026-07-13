"""站点 onboarding 五阶段：录入 → 探查 → 分析 → 工具链 → 调度。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
DRAFTS_DIR = ROOT / "config" / "sites_drafts"
ONBOARDING_DIR = ROOT / "data" / "site_onboarding"

ONBOARDING_STAGES = ["register", "probe", "analyze", "toolchain", "schedule"]
STAGE_LABELS: dict[str, str] = {
    "register": "录入",
    "probe": "探查",
    "analyze": "分析",
    "toolchain": "工具链",
    "schedule": "调度",
}
StageStatus = Literal["pending", "in_progress", "completed", "skipped"]


class ProbeReport(BaseModel):
    """探查结果 stub，后续可接 WebBridge / HTTP 探针。"""

    site_id: str
    url: str
    status: StageStatus = "pending"
    probed_at: Optional[str] = None
    summary: Optional[str] = None
    page_title: Optional[str] = None
    hints: dict[str, Any] = Field(default_factory=dict)


class OnboardingStatus(BaseModel):
    site_id: str
    name: str
    url: str
    current_stage: str
    stages: dict[str, StageStatus]
    registered_at: str
    draft: bool = True
    probe_report: Optional[ProbeReport] = None


def normalize_page_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    return raw


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ONBOARDING_DIR.mkdir(parents=True, exist_ok=True)


def _onboarding_path(site_id: str) -> Path:
    return ONBOARDING_DIR / f"{site_id}.json"


def _draft_path(site_id: str) -> Path:
    return DRAFTS_DIR / f"{site_id}.yaml"


def site_id_exists(site_id: str) -> bool:
    from src.core.site_sync import get_site_by_id

    if get_site_by_id(site_id):
        return True
    return _draft_path(site_id).is_file()


def _slug_site_id(name: str, url: str) -> str:
    host = urlparse(normalize_page_url(url)).netloc.lower()
    if host:
        slug = re.sub(r"[^a-z0-9]+", "_", host.replace(".", "_"))
    else:
        slug = re.sub(r"[^\w]+", "_", name.strip(), flags=re.UNICODE)
    slug = slug.strip("_")[:48] or "site"
    candidate = slug
    suffix = 2
    while site_id_exists(candidate):
        candidate = f"{slug}_{suffix}"
        suffix += 1
    return candidate


def _initial_stages() -> dict[str, StageStatus]:
    stages: dict[str, StageStatus] = {stage: "pending" for stage in ONBOARDING_STAGES}
    stages["register"] = "completed"
    stages["probe"] = "in_progress"
    return stages


def _save_onboarding(status: OnboardingStatus) -> None:
    _ensure_dirs()
    with open(_onboarding_path(status.site_id), "w", encoding="utf-8") as f:
        json.dump(status.model_dump(mode="json"), f, ensure_ascii=False, indent=2)


def register_site(name: str, url: str) -> dict[str, Any]:
    """录入新站点：写 draft YAML + onboarding JSON，返回 site_id。"""
    _ensure_dirs()
    clean_name = (name or "").strip()
    clean_url = normalize_page_url(url)
    if not clean_name or not clean_url:
        raise ValueError("name 与 url 不能为空")

    site_id = _slug_site_id(clean_name, clean_url)
    draft = {
        "id": site_id,
        "name": clean_name,
        "url": clean_url,
        "category": "draft",
        "adapter": "generic",
        "enabled": False,
        "mvp": False,
        "onboarding": True,
        "notes": f"onboarding draft created {_now_iso()}",
    }
    with open(_draft_path(site_id), "w", encoding="utf-8") as f:
        yaml.safe_dump(draft, f, allow_unicode=True, sort_keys=False)

    status = OnboardingStatus(
        site_id=site_id,
        name=clean_name,
        url=clean_url,
        current_stage="probe",
        stages=_initial_stages(),
        registered_at=_now_iso(),
        draft=True,
        probe_report=ProbeReport(site_id=site_id, url=clean_url, status="pending"),
    )
    _save_onboarding(status)
    return status.model_dump(mode="json")


def _status_from_draft(site_id: str, draft: dict[str, Any]) -> OnboardingStatus:
    stages = _initial_stages()
    return OnboardingStatus(
        site_id=site_id,
        name=str(draft.get("name") or site_id),
        url=str(draft.get("url") or ""),
        current_stage="probe",
        stages=stages,
        registered_at=_now_iso(),
        draft=True,
        probe_report=ProbeReport(
            site_id=site_id,
            url=str(draft.get("url") or ""),
            status="pending",
        ),
    )


def _status_from_registered_site(site_id: str, site: dict[str, Any]) -> OnboardingStatus:
    return OnboardingStatus(
        site_id=site_id,
        name=str(site.get("name") or site_id),
        url=str(site.get("url") or ""),
        current_stage="schedule",
        stages={stage: "completed" for stage in ONBOARDING_STAGES},
        registered_at=_now_iso(),
        draft=False,
    )


def get_onboarding_status(site_id: str) -> Optional[OnboardingStatus]:
    path = _onboarding_path(site_id)
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            return OnboardingStatus.model_validate(json.load(f))

    from src.core.site_sync import get_site_by_id

    site = get_site_by_id(site_id)
    if site:
        return _status_from_registered_site(site_id, site)

    draft_path = _draft_path(site_id)
    if draft_path.is_file():
        with open(draft_path, encoding="utf-8") as f:
            draft = yaml.safe_load(f) or {}
        return _status_from_draft(site_id, draft)
    return None


def save_probe_report(site_id: str, report: ProbeReport) -> OnboardingStatus:
    status = get_onboarding_status(site_id)
    if status is None:
        raise KeyError(f"站点 onboarding 不存在: {site_id}")
    status.probe_report = report
    if report.status == "completed":
        status.stages["probe"] = "completed"
        status.stages["analyze"] = "in_progress"
        status.current_stage = "analyze"
    _save_onboarding(status)
    return status


def mark_onboarding_stage_completed(
    site_id: str,
    stage: str,
    *,
    skipped: bool = False,
) -> Optional[OnboardingStatus]:
    """标记 onboarding 某阶段完成，并推进 current_stage。"""
    if stage not in ONBOARDING_STAGES:
        raise ValueError(f"未知阶段: {stage}")
    status = get_onboarding_status(site_id)
    if status is None:
        return None

    status.stages[stage] = "skipped" if skipped else "completed"
    try:
        idx = ONBOARDING_STAGES.index(stage)
    except ValueError:
        idx = -1

    next_stage: Optional[str] = None
    if idx >= 0 and idx + 1 < len(ONBOARDING_STAGES):
        next_stage = ONBOARDING_STAGES[idx + 1]
        if status.stages.get(next_stage) == "pending":
            status.stages[next_stage] = "in_progress"
        status.current_stage = next_stage
    else:
        status.current_stage = stage

    _save_onboarding(status)
    return status
