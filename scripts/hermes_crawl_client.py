#!/usr/bin/env python3
"""Hermes Crawl Agent 守护进程 — 巡检 web_scraper 失败/超时任务。

P0：HTTP 轮询 + 打印/构造 Hermes prompt，不强制依赖 hermes CLI。
P1：--once / --spawn-hermes 调用 ``hermes`` 执行 Agent turn（不可用时输出手动调用说明）。
P2：--loop / --daemon 间隔轮询（默认 15min），有 attention 站点时 spawn hermes turn；
    stale 站建议 reset，可选 --auto-reset 调用 POST /api/sync/{id}/reset。
    日志写入 data/agent-crawl.log。

用法:
    python scripts/hermes_crawl_client.py --dry-run
    python scripts/hermes_crawl_client.py --once
    python scripts/hermes_crawl_client.py --spawn-hermes
    python scripts/hermes_crawl_client.py --loop
    python scripts/hermes_crawl_client.py --daemon --interval-minutes 15 --auto-reset
    WEB_SCRAPER_BASE_URL=http://127.0.0.1:8080 python scripts/hermes_crawl_client.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_BASE = "http://127.0.0.1:8080"
DEFAULT_STALE_HOURS = 24.0
DEFAULT_INTERVAL_MINUTES = 15.0
LOG_FILENAME = "agent-crawl.log"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_log_path() -> Path:
    return _project_root() / "data" / LOG_FILENAME


def _base_url() -> str:
    return os.getenv("WEB_SCRAPER_BASE_URL", DEFAULT_BASE).rstrip("/")


def _get_json(path: str, timeout: float = 30.0) -> Any:
    url = f"{_base_url()}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(path: str, *, body: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    url = f"{_base_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {"status": resp.status}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
        except json.JSONDecodeError:
            parsed = {"detail": err_body}
        return {
            "ok": False,
            "http_status": exc.code,
            "error": parsed.get("detail") if isinstance(parsed, dict) else parsed,
            "path": path,
        }


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _site_id(site: dict[str, Any]) -> str:
    return str(site.get("site_id") or site.get("id") or "")


def _site_new_count(site: dict[str, Any]) -> int:
    last_run = site.get("last_run") or {}
    if isinstance(last_run, dict) and last_run.get("new_count") is not None:
        return int(last_run.get("new_count") or 0)
    if site.get("new_count_display") is not None:
        return int(site.get("new_count_display") or 0)
    return int(site.get("new_count") or 0)


def is_healthy_zero_new(site: dict[str, Any]) -> bool:
    """HC-P2-7 轻量版：completed + new_count=0 视为正常增量，不列入巡检。"""
    if site.get("is_running") or site.get("sync_stale"):
        return False

    sync_status = (site.get("status") or site.get("sync_status") or "").lower()
    display = (site.get("display_status") or "").lower()
    last_run_status = (site.get("last_run_status") or "").lower()
    new_count = _site_new_count(site)

    if sync_status == "failed" or display == "failed" or last_run_status == "failed":
        return False
    if new_count != 0:
        return False

    if sync_status == "completed" and last_run_status in ("success", "pending", ""):
        return True
    return display == "success"


def is_failed_site(site: dict[str, Any]) -> bool:
    if is_healthy_zero_new(site):
        return False
    if site.get("sync_stale"):
        return True

    sync_status = (site.get("status") or site.get("sync_status") or "").lower()
    display = (site.get("display_status") or "").lower()
    last_run = site.get("last_run") or {}
    run_status = (last_run.get("status") or "").lower() if isinstance(last_run, dict) else ""
    last_run_status = (site.get("last_run_status") or "").lower()

    return (
        sync_status == "failed"
        or display == "failed"
        or run_status == "failed"
        or last_run_status == "failed"
    )


def is_overdue_no_success(
    site: dict[str, Any],
    *,
    hours: float = DEFAULT_STALE_HOURS,
    now: datetime | None = None,
) -> bool:
    """enabled 站点超过 hours 无 last_success_at（且非 healthy 0-new）。"""
    if not site.get("enabled", True):
        return False
    if site.get("is_running") or is_healthy_zero_new(site):
        return False

    now = now or datetime.now(timezone.utc)
    last_success = _parse_iso_dt(site.get("last_success_at"))
    if last_success is None:
        sync_status = (site.get("status") or site.get("sync_status") or "").lower()
        if sync_status == "failed":
            return True
        last_sync = _parse_iso_dt(site.get("last_sync_at"))
        if last_sync is None:
            return False
        return (now - last_sync).total_seconds() > hours * 3600

    return (now - last_success).total_seconds() > hours * 3600


def classify_site_attention(
    site: dict[str, Any],
    *,
    stale_hours: float = DEFAULT_STALE_HOURS,
    now: datetime | None = None,
) -> list[str]:
    reasons: list[str] = []
    if is_failed_site(site):
        if site.get("sync_stale"):
            reasons.append("stale_sync")
        else:
            reasons.append("failed")
    if is_overdue_no_success(site, hours=stale_hours, now=now):
        reasons.append("overdue_24h")
    return reasons


def fetch_all_sites() -> list[dict[str, Any]]:
    data = _get_json("/api/tasks/sites")
    sites = data if isinstance(data, list) else data.get("sites", data.get("items", []))
    return [s for s in sites if isinstance(s, dict)]


def fetch_attention_sites(
    *,
    stale_hours: float = DEFAULT_STALE_HOURS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """返回需 Agent 介入的 enabled 站点（failed / stale / 24h 未成功）。"""
    attention: list[dict[str, Any]] = []
    for site in fetch_all_sites():
        reasons = classify_site_attention(site, stale_hours=stale_hours, now=now)
        if not reasons:
            continue
        enriched = dict(site)
        enriched["attention_reasons"] = reasons
        attention.append(enriched)
    return attention


def build_agent_prompt(attention_sites: list[dict[str, Any]]) -> str:
    lines = [
        "你是招标爬虫运维 Agent。WEB_SCRAPER_BASE_URL=" + _base_url(),
        f"当前有 {len(attention_sites)} 个站点需要处理（failed / stale / 24h 未成功）：",
    ]
    for s in attention_sites:
        sid = _site_id(s) or "?"
        reasons = ",".join(s.get("attention_reasons") or [])
        err = s.get("last_error") or s.get("error") or ""
        detail = err or reasons or "needs_attention"
        lines.append(f"- {sid} [{reasons}]: {detail}")
    lines.extend(
        [
            "",
            "healthy 判定：completed + new_count=0 且无 failed/stale 的站点已自动排除。",
            "对每个站点：crawl_get_task_status → crawl_get_run_logs → 判断缺规则/登录/选择器。",
            "stale syncing（sync_stale=true）：POST /api/sync/{site_id}/reset 或 Web UI「重置状态」后再 trigger；",
            "  也可用 hermes_crawl_client.py --auto-reset。API 站（如电建）优先 scripts/crawl_powerchina_http.py。",
            "调用相应 crawl tool；需人工时用 crawl_request_user_input 和 crawl_notify_user。",
        ]
    )
    return "\n".join(lines)


def build_hermes_invoke_instructions(prompt: str) -> str:
    escaped = prompt.replace("'", "'\"'\"'")
    return "\n".join(
        [
            "--- Hermes 调用说明 ---",
            "hermes CLI 不在 PATH。可手动执行：",
            "",
            f"  export WEB_SCRAPER_BASE_URL={_base_url()}",
            f"  hermes -p '{escaped}'",
            "",
            "或在 Hermes Gateway Cron 中注册上述 prompt。",
        ]
    )


def run_hermes_once(prompt: str) -> int:
    hermes = shutil.which("hermes")
    if not hermes:
        print(build_hermes_invoke_instructions(prompt))
        return 1
    proc = subprocess.run([hermes, "-p", prompt], check=False)
    return proc.returncode


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_action(site_id: str, action: str, detail: str = "") -> None:
    """POST 审计日志到 web_scraper（失败时静默）。"""
    try:
        _post_json(
            "/api/crawl-agent/actions",
            body={
                "site_id": site_id,
                "action": action,
                "detail": detail,
                "source": "daemon",
            },
        )
    except (urllib.error.URLError, OSError):
        pass


def append_log(message: str, log_path: Path | None = None, *, also_stdout: bool = True) -> None:
    """追加一行带时间戳的日志到 data/agent-crawl.log。"""
    path = log_path or _default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{_utc_now_iso()}] {message}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    if also_stdout:
        print(line.rstrip())


def reset_stale_site(site_id: str) -> dict[str, Any]:
    """POST /api/sync/{site_id}/reset — 仅 stale syncing 时成功。"""
    from urllib.parse import quote

    encoded = quote(site_id, safe="")
    result = _post_json(f"/api/sync/{encoded}/reset")
    if isinstance(result, dict) and result.get("ok") is not False and "http_status" not in result:
        return {"ok": True, "site_id": site_id, "result": result}
    return {
        "ok": False,
        "site_id": site_id,
        "result": result,
    }


def handle_stale_resets(
    attention_sites: list[dict[str, Any]],
    *,
    auto_reset: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """对 stale_sync 站点建议 reset；--auto-reset 时自动调用 API。"""
    _log = log_fn or (lambda msg: None)
    outcomes: list[dict[str, Any]] = []
    for site in attention_sites:
        reasons = site.get("attention_reasons") or []
        if "stale_sync" not in reasons:
            continue
        sid = _site_id(site)
        if not sid:
            continue
        _log(f"stale_sync 建议 reset: {sid} (POST /api/sync/{sid}/reset 或 Web UI「重置状态」)")
        if not auto_reset:
            outcomes.append({"site_id": sid, "action": "suggested", "ok": None})
            continue
        result = reset_stale_site(sid)
        if result.get("ok"):
            _log(f"auto-reset 成功: {sid}")
            record_action(sid, "reset_stale", "auto-reset 成功")
        else:
            detail = result.get("result")
            _log(f"auto-reset 失败: {sid} -> {detail}")
            record_action(sid, "reset_stale", f"auto-reset 失败: {detail}")
        outcomes.append({"site_id": sid, "action": "reset", **result})
    return outcomes


def run_poll_cycle(
    *,
    stale_hours: float = DEFAULT_STALE_HOURS,
    auto_reset: bool = False,
    spawn_hermes: bool = True,
    log_path: Path | None = None,
    now: datetime | None = None,
    attention_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
) -> int:
    """单次巡检：拉 attention → 处理 stale reset → spawn hermes（若有待处理站）。"""
    fetcher = attention_fetcher or fetch_attention_sites

    def _log(msg: str) -> None:
        append_log(msg, log_path, also_stdout=True)

    _log(f"poll cycle start base_url={_base_url()}")

    try:
        attention = fetcher(stale_hours=stale_hours, now=now)
    except urllib.error.URLError as exc:
        _log(f"无法连接 web_scraper: {exc}")
        return 1

    failed_n = sum(1 for s in attention if "failed" in s.get("attention_reasons", []))
    stale_n = sum(1 for s in attention if "stale_sync" in s.get("attention_reasons", []))
    overdue_n = sum(1 for s in attention if "overdue_24h" in s.get("attention_reasons", []))
    _log(
        f"attention_count={len(attention)} failed={failed_n} stale={stale_n} overdue={overdue_n}"
    )

    if stale_n:
        handle_stale_resets(attention, auto_reset=auto_reset, log_fn=_log)

    if not attention:
        _log("无 attention 站点，跳过 Hermes turn")
        return 0

    prompt = build_agent_prompt(attention)
    _log(f"spawn Hermes turn attention_count={len(attention)}")
    for site in attention:
        sid = _site_id(site)
        if not sid:
            continue
        reasons = ",".join(site.get("attention_reasons") or [])
        record_action(sid, "spawn_hermes", reasons or "needs_attention")

    if not spawn_hermes:
        print("--- Agent prompt ---\n")
        print(prompt)
        _log("spawn_hermes=False，仅输出 prompt")
        return 0

    print("--- Agent prompt ---\n")
    print(prompt)
    print()
    rc = run_hermes_once(prompt)
    _log(f"hermes exit_code={rc}")
    return rc


def run_daemon_loop(
    *,
    interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
    stale_hours: float = DEFAULT_STALE_HOURS,
    auto_reset: bool = False,
    spawn_hermes: bool = True,
    log_path: Path | None = None,
) -> None:
    """间隔轮询守护进程（--loop / --daemon）。"""
    path = log_path or _default_log_path()
    append_log(
        f"daemon start interval_minutes={interval_minutes} auto_reset={auto_reset}",
        path,
    )
    while True:
        try:
            run_poll_cycle(
                stale_hours=stale_hours,
                auto_reset=auto_reset,
                spawn_hermes=spawn_hermes,
                log_path=path,
            )
        except Exception as exc:  # noqa: BLE001 — 守护进程不因单次异常退出
            append_log(f"poll cycle exception: {exc}", path)
        sleep_s = max(1.0, interval_minutes * 60.0)
        append_log(f"sleep {interval_minutes}min until next poll", path)
        time.sleep(sleep_s)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Crawl Agent 巡检守护进程")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅列出待处理站点与 prompt")
    mode.add_argument("--once", action="store_true", help="单次巡检并尝试调用 hermes CLI")
    mode.add_argument(
        "--spawn-hermes",
        action="store_true",
        help="同 --once：单次巡检并 spawn hermes CLI turn",
    )
    mode.add_argument(
        "--loop",
        "--daemon",
        action="store_true",
        dest="loop",
        help=f"守护进程：每 {DEFAULT_INTERVAL_MINUTES:.0f}min 轮询（可用 --interval-minutes 调整）",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"守护进程轮询间隔（分钟，默认 {DEFAULT_INTERVAL_MINUTES:g}）",
    )
    parser.add_argument(
        "--auto-reset",
        action="store_true",
        help="stale_sync 站点自动 POST /api/sync/{id}/reset（默认仅日志建议）",
    )
    parser.add_argument(
        "--no-spawn",
        action="store_true",
        help="有 attention 站点时不调用 hermes，仅写日志与 prompt",
    )
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=DEFAULT_STALE_HOURS,
        help="超过该小时数无 last_success_at 视为 overdue（默认 24）",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=f"日志路径（默认 {_default_log_path()}）",
    )
    args = parser.parse_args()

    log_path = args.log_file or _default_log_path()

    if args.loop:
        run_daemon_loop(
            interval_minutes=args.interval_minutes,
            stale_hours=args.stale_hours,
            auto_reset=args.auto_reset,
            spawn_hermes=not args.no_spawn,
            log_path=log_path,
        )
        return 0

    if args.dry_run:
        try:
            attention = fetch_attention_sites(stale_hours=args.stale_hours)
        except urllib.error.URLError as exc:
            print(f"无法连接 web_scraper ({_base_url()}): {exc}", file=sys.stderr)
            return 1

        payload = {
            "base_url": _base_url(),
            "attention_count": len(attention),
            "failed_count": sum(1 for s in attention if "failed" in s.get("attention_reasons", [])),
            "stale_count": sum(1 for s in attention if "stale_sync" in s.get("attention_reasons", [])),
            "overdue_count": sum(1 for s in attention if "overdue_24h" in s.get("attention_reasons", [])),
            "sites": attention,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("\n--- Agent prompt ---\n")
        print(build_agent_prompt(attention))
        return 0

    if args.once or args.spawn_hermes:
        return run_poll_cycle(
            stale_hours=args.stale_hours,
            auto_reset=args.auto_reset,
            spawn_hermes=not args.no_spawn,
            log_path=log_path,
        )

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
