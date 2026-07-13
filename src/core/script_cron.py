"""scripts/ 批量入库 Cron 任务：持久化、白名单校验、subprocess 执行。"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import yaml
from apscheduler.triggers.cron import CronTrigger

from src.core.timezone_utils import APP_TZ

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = ROOT / "data"
JOBS_PATH = DATA_DIR / "script_cron_jobs.json"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"

# 允许通过 cron 调度的 scripts/ 入口（批量入库 / 同步）
SCRIPT_WHITELIST: frozenset[str] = frozenset(
    {
        "crawl_powerchina_http.py",
        "run_daily_sync.py",
        "run_daily_bim_sync.py",
        "run_daily_biz_clue_sync.py",
        "run_bim_crawl_all.py",
        "run_all_enabled.py",
        "run_once.py",
        "run_generated_crawl.py",
        "run_intelligent_crawl.py",
        "run_agent_crawl.py",
    }
)

# 禁止出现在参数中的 shell 元字符
_UNSAFE_ARG_RE = re.compile(r"[;&|`$<>(){}\\]")

# 口语 → 5 段 cron（分 时 日 月 周）
_NATURAL_CRON_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"每天\s*凌晨?\s*(\d{1,2})\s*点"), lambda m: f"0 {int(m.group(1))} * * *"),
    (re.compile(r"每天\s*(\d{1,2})\s*点"), lambda m: f"0 {int(m.group(1))} * * *"),
    (re.compile(r"每日\s*(\d{1,2})\s*点"), lambda m: f"0 {int(m.group(1))} * * *"),
    (re.compile(r"每\s*(\d{1,2})\s*小时"), lambda m: f"0 */{int(m.group(1))} * * *"),
]


def _default_timezone() -> ZoneInfo:
    if SCHEDULE_PATH.exists():
        with open(SCHEDULE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        tz_name = (data.get("scheduler") or {}).get("timezone", "Asia/Shanghai")
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return APP_TZ


def normalize_cron_expression(raw: str) -> str:
    """解析标准 5 段 cron 或常见中文口语。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("cron 表达式不能为空")

    parts = text.split()
    if len(parts) == 5 and all(p for p in parts):
        validate_cron_expression(text)
        return text

    for pattern, builder in _NATURAL_CRON_PATTERNS:
        match = pattern.search(text)
        if match:
            expr = builder(match)
            validate_cron_expression(expr)
            return expr

    raise ValueError(
        f"无法解析 cron: {raw!r}；请使用 5 段格式（如 0 2 * * *）或「每天 2 点」"
    )


def validate_cron_expression(cron_expr: str) -> None:
    """校验 5 段 cron 可被 APScheduler 解析。"""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron 须为 5 段（分 时 日 月 周），当前: {cron_expr!r}")
    minute, hour, day, month, day_of_week = parts
    CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=_default_timezone(),
    )


_CRON_DOW_LABELS = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")


def describe_cron_expression(cron_expr: str) -> str:
    """将常见 5 段 cron 转为简短中文描述。"""
    text = (cron_expr or "").strip()
    if not text:
        return ""
    parts = text.split()
    if len(parts) != 5:
        return text
    minute, hour, day, month, dow = parts
    if not (minute.isdigit() and hour.isdigit() and day == "*" and month == "*"):
        return text
    time_label = f"{int(hour):02d}:{int(minute):02d}"
    if dow == "*":
        return f"每天 {time_label}"
    if dow.isdigit():
        return f"每{_CRON_DOW_LABELS[int(dow) % 7]} {time_label}"
    return text


def build_cron_trigger(cron_expr: str, *, tz: Optional[ZoneInfo] = None) -> CronTrigger:
    parts = cron_expr.strip().split()
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=tz or _default_timezone(),
    )


def compute_next_run_time(
    cron_expr: str,
    *,
    tz: Optional[ZoneInfo] = None,
    after: Optional[datetime] = None,
) -> Optional[datetime]:
    zone = tz or _default_timezone()
    trigger = build_cron_trigger(cron_expr, tz=zone)
    ref = after or datetime.now(zone)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=zone)
    return trigger.get_next_fire_time(None, ref)


def validate_script_name(script: str) -> str:
    name = (script or "").strip()
    if not name:
        raise ValueError("script 必填")
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"script 须为 scripts/ 下文件名，禁止路径: {script!r}")
    if not name.endswith(".py"):
        name = f"{name}.py"
    if name not in SCRIPT_WHITELIST:
        allowed = ", ".join(sorted(SCRIPT_WHITELIST))
        raise ValueError(f"脚本不在白名单: {name}；允许: {allowed}")
    script_path = SCRIPTS_DIR / name
    if not script_path.is_file():
        raise ValueError(f"脚本文件不存在: scripts/{name}")
    return name


def validate_script_args(args: Any) -> list[str]:
    """校验并规范化 CLI 参数列表。"""
    if args is None:
        return []
    if isinstance(args, dict):
        flat: list[str] = []
        for key, value in args.items():
            flag = key if str(key).startswith("--") else f"--{key}".replace("_", "-")
            if isinstance(value, bool):
                if value:
                    flat.append(flag)
            elif value is not None:
                flat.extend([flag, str(value)])
        args = flat
    if not isinstance(args, list):
        raise ValueError("args 须为字符串列表或键值对象")

    normalized: list[str] = []
    for item in args:
        text = str(item).strip()
        if not text:
            continue
        if _UNSAFE_ARG_RE.search(text):
            raise ValueError(f"参数含非法字符: {text!r}")
        normalized.append(text)
    return normalized


def build_subprocess_command(script: str, args: Optional[list[str]] = None) -> list[str]:
    script_name = validate_script_name(script)
    safe_args = validate_script_args(args)
    script_path = SCRIPTS_DIR / script_name
    return [sys.executable, str(script_path), *safe_args]


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_script_cron_jobs() -> list[dict[str, Any]]:
    if not JOBS_PATH.exists():
        return []
    with open(JOBS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    return [j for j in jobs if isinstance(j, dict)]


def save_script_cron_jobs(jobs: list[dict[str, Any]]) -> None:
    _ensure_data_dir()
    payload = {"version": 1, "jobs": jobs}
    tmp = JOBS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(JOBS_PATH)


def get_script_cron_job(job_id: str) -> Optional[dict[str, Any]]:
    for job in load_script_cron_jobs():
        if job.get("id") == job_id:
            return job
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    cron = job.get("cron") or ""
    next_run = compute_next_run_time(cron) if cron and job.get("enabled", True) else None
    out = dict(job)
    out["next_run_time"] = next_run.isoformat() if next_run else None
    return out


def list_script_cron_jobs(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    jobs = load_script_cron_jobs()
    if enabled_only:
        jobs = [j for j in jobs if j.get("enabled", True)]
    return [_serialize_job(j) for j in jobs]


def create_script_cron_job(
    *,
    script: str,
    cron: str,
    args: Any = None,
    name: Optional[str] = None,
    enabled: bool = True,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    script_name = validate_script_name(script)
    cron_expr = normalize_cron_expression(cron)
    safe_args = validate_script_args(args)

    jobs = load_script_cron_jobs()
    new_id = (job_id or "").strip() or str(uuid.uuid4())
    if any(j.get("id") == new_id for j in jobs):
        raise ValueError(f"job_id 已存在: {new_id}")

    now = _now_iso()
    job: dict[str, Any] = {
        "id": new_id,
        "name": (name or "").strip() or f"{script_name} @ {cron_expr}",
        "script": script_name,
        "args": safe_args,
        "cron": cron_expr,
        "enabled": bool(enabled),
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_run_status": None,
        "last_run_message": None,
    }
    jobs.append(job)
    save_script_cron_jobs(jobs)
    return _serialize_job(job)


def update_script_cron_job(
    job_id: str,
    *,
    script: Optional[str] = None,
    cron: Optional[str] = None,
    args: Any = None,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict[str, Any]:
    jobs = load_script_cron_jobs()
    target = None
    for job in jobs:
        if job.get("id") == job_id:
            target = job
            break
    if target is None:
        raise ValueError(f"任务不存在: {job_id}")

    if script is not None:
        target["script"] = validate_script_name(script)
    if cron is not None:
        target["cron"] = normalize_cron_expression(cron)
    if args is not None:
        target["args"] = validate_script_args(args)
    if name is not None:
        target["name"] = name.strip() or target.get("name")
    if enabled is not None:
        target["enabled"] = bool(enabled)
    target["updated_at"] = _now_iso()
    save_script_cron_jobs(jobs)
    return _serialize_job(target)


def delete_script_cron_job(job_id: str) -> dict[str, Any]:
    jobs = load_script_cron_jobs()
    kept = [j for j in jobs if j.get("id") != job_id]
    if len(kept) == len(jobs):
        raise ValueError(f"任务不存在: {job_id}")
    save_script_cron_jobs(kept)
    return {"ok": True, "deleted_id": job_id}


def mark_job_run(job_id: str, result: dict[str, Any]) -> None:
    jobs = load_script_cron_jobs()
    for job in jobs:
        if job.get("id") == job_id:
            job["last_run_at"] = _now_iso()
            job["last_run_status"] = "success" if result.get("ok") else "failed"
            job["last_run_message"] = (result.get("message") or result.get("error") or "")[:500]
            job["updated_at"] = _now_iso()
            break
    else:
        return
    save_script_cron_jobs(jobs)


def run_script_job(
    job_id: str,
    *,
    subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """执行持久化 cron 任务（subprocess，禁止 shell）。"""
    job = get_script_cron_job(job_id)
    if not job:
        return {"ok": False, "error": f"任务不存在: {job_id}"}
    if not job.get("enabled", True):
        return {"ok": False, "error": f"任务已禁用: {job_id}"}

    cmd = build_subprocess_command(job["script"], job.get("args"))
    log_path = DATA_DIR / f"script_cron_{job_id}.log"
    logger.info("执行 script cron: %s -> %s", job_id, " ".join(cmd))

    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- {_now_iso()} job={job_id} ---\n")
            log_file.write(f"cmd: {' '.join(cmd)}\n")
            log_file.flush()
            proc = subprocess_run(
                cmd,
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=7200,
            )
        ok = proc.returncode == 0
        result = {
            "ok": ok,
            "job_id": job_id,
            "returncode": proc.returncode,
            "log_path": str(log_path),
            "message": "执行成功" if ok else f"退出码 {proc.returncode}",
        }
    except subprocess.TimeoutExpired:
        result = {"ok": False, "job_id": job_id, "error": "执行超时 (7200s)"}
    except Exception as exc:
        result = {"ok": False, "job_id": job_id, "error": str(exc)}

    mark_job_run(job_id, result)
    return result
