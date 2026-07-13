"""Crawl Agent 人机协同待办队列：JSON 文件持久化（data/crawl_agent_pending.json）。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from src.core.config import get_settings

logger = logging.getLogger(__name__)

PendingInputType = Literal["login_cookie", "captcha", "rule_confirm", "generic"]
PendingStatus = Literal["pending", "resolved"]

INPUT_TYPE_LABELS: dict[str, str] = {
    "login_cookie": "登录 Cookie",
    "captcha": "验证码",
    "rule_confirm": "规则确认",
    "generic": "其他输入",
}

VALID_INPUT_TYPES: frozenset[str] = frozenset(INPUT_TYPE_LABELS)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path() -> Path:
    return get_settings().data_dir / "crawl_agent_pending.json"


def _load_store() -> dict[str, Any]:
    path = _store_path()
    if not path.is_file():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取 pending 队列失败: %s", exc)
    return {"items": []}


def _save_store(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "site_id": item["site_id"],
        "input_type": item["input_type"],
        "input_type_label": INPUT_TYPE_LABELS.get(item["input_type"], item["input_type"]),
        "prompt": item["prompt"],
        "choices": item.get("choices"),
        "context": item.get("context") or {},
        "status": item["status"],
        "created_at": item["created_at"],
        "resolved_at": item.get("resolved_at"),
        "user_response": item.get("user_response"),
    }


def create_pending_item(
    *,
    site_id: str,
    input_type: str,
    prompt: str,
    choices: Optional[list[str]] = None,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if input_type not in VALID_INPUT_TYPES:
        raise ValueError(f"不支持的 input_type: {input_type}")
    if not prompt.strip():
        raise ValueError("prompt 不能为空")

    item = {
        "id": uuid.uuid4().hex[:12],
        "site_id": site_id,
        "input_type": input_type,
        "prompt": prompt.strip(),
        "choices": choices,
        "context": context or {},
        "status": "pending",
        "created_at": _utcnow_iso(),
        "resolved_at": None,
        "user_response": None,
    }
    store = _load_store()
    store["items"].append(item)
    _save_store(store)
    return _public_item(item)


def list_pending_items(
    *,
    status: Optional[str] = "pending",
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    items = _load_store().get("items", [])
    if status and status != "all":
        items = [i for i in items if i.get("status") == status]
    if site_id:
        items = [i for i in items if i.get("site_id") == site_id]
    items = sorted(items, key=lambda i: i.get("created_at", ""), reverse=True)
    return [_public_item(i) for i in items]


def get_pending_item(item_id: str) -> Optional[dict[str, Any]]:
    for item in _load_store().get("items", []):
        if item.get("id") == item_id:
            return _public_item(item)
    return None


def _apply_login_cookie(site_id: str, user_response: str) -> dict[str, Any]:
    from src.core.site_credentials import save_site_credentials
    from src.core.site_sync import get_site_by_id

    site = get_site_by_id(site_id)
    if not site:
        raise ValueError("站点不存在")
    cookie_header = user_response.strip()
    if not cookie_header:
        raise ValueError("Cookie 不能为空")
    return save_site_credentials(
        site_id,
        site_url=site.get("url", ""),
        credential_type="cookie",
        cookie_header=cookie_header,
        notes="via crawl-agent pending resolve",
    )


def resolve_pending_item(item_id: str, user_response: str) -> dict[str, Any]:
    response = user_response.strip()
    if not response:
        raise ValueError("user_response 不能为空")

    store = _load_store()
    target: Optional[dict[str, Any]] = None
    for item in store.get("items", []):
        if item.get("id") == item_id:
            target = item
            break
    if target is None:
        raise LookupError("待办项不存在")
    if target.get("status") != "pending":
        raise ValueError("待办项已处理")

    credentials_result: Optional[dict[str, Any]] = None
    if target.get("input_type") == "login_cookie":
        credentials_result = _apply_login_cookie(target["site_id"], response)

    target["status"] = "resolved"
    target["resolved_at"] = _utcnow_iso()
    target["user_response"] = response
    _save_store(store)

    result: dict[str, Any] = {"ok": True, "item": _public_item(target)}
    if credentials_result is not None:
        cred_ok = credentials_result.get("ok", False)
        result["credentials_saved"] = cred_ok
        result["credentials"] = {
            k: credentials_result[k]
            for k in ("ok", "cookie_count", "site_id")
            if k in credentials_result
        }
        if cred_ok:
            from src.core.crawl_agent_actions import append_action_if_absent

            cookie_count = credentials_result.get("cookie_count")
            detail_parts = [f"pending_id={item_id}", f"input_type={target.get('input_type')}"]
            if cookie_count is not None:
                detail_parts.append(f"cookie_count={cookie_count}")
            action_rec = append_action_if_absent(
                site_id=target["site_id"],
                action="pending_resolve_credentials",
                detail="; ".join(detail_parts),
                source="web",
                dedup_detail_contains=f"pending_id={item_id}",
            )
            if action_rec is not None:
                result["action_recorded"] = True
            else:
                result["action_recorded"] = False
                result["action_skipped_duplicate"] = True
    return result
