"""大模型欠费/配额异常时向管理员飞书私信告警。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USER_ID = "li_xf10"
_ALERT_COOLDOWN_SECONDS = 30 * 60

_BILLING_KEYWORDS: tuple[str, ...] = (
    "quota exceeded",
    "insufficient balance",
    "insufficient_quota",
    "insufficient quota",
    "out of credits",
    "credit balance",
    "billing",
    "欠费",
    "余额不足",
    "账户余额",
    "充值",
    "payment required",
    "exceeded your current quota",
    "rate limit",
    "too many requests",
    "exceeded quota",
    "quota is exhausted",
    "billing hard limit",
    "billing_not_active",
    "insufficient_funds",
)

_BILLING_HTTP_RE = re.compile(r"\bHTTP\s*(402|429)\b", re.IGNORECASE)
_BILLING_CODE_RE = re.compile(r"\b(402|429)\b")

_last_alert_at: dict[str, float] = {}


def _normalize_error_text(error_detail: Any) -> str:
    if error_detail is None:
        return ""
    if isinstance(error_detail, BaseException):
        parts = [str(error_detail)]
        code = getattr(error_detail, "code", None)
        if code is not None:
            parts.append(f"HTTP {code}")
        return " ".join(parts)
    return str(error_detail).strip()


def is_llm_billing_error(error_detail: Any) -> bool:
    """判断是否为 LLM 欠费、配额或计费相关异常。"""
    text = _normalize_error_text(error_detail)
    if not text:
        return False

    lowered = text.lower()
    if _BILLING_HTTP_RE.search(text):
        return True
    if "http 402" in lowered or "http 429" in lowered:
        return True
    if "余额不足" in text or "欠费" in text:
        return True
    if any(keyword in lowered for keyword in _BILLING_KEYWORDS):
        return True

    # 独立状态码（避免误匹配普通数字）
    if _BILLING_CODE_RE.search(text) and any(
        token in lowered
        for token in ("http", "llm", "api", "quota", "billing", "balance", "credit", "rate")
    ):
        return True
    return False


def _in_cooldown(source: str) -> bool:
    last = _last_alert_at.get(source)
    if last is None:
        return False
    return (time.time() - last) < _ALERT_COOLDOWN_SECONDS


def _mark_alert_sent(source: str) -> None:
    _last_alert_at[source] = time.time()


def reset_llm_alert_cooldown(*, source: Optional[str] = None) -> None:
    """测试用：清除告警冷却。"""
    if source is None:
        _last_alert_at.clear()
        return
    _last_alert_at.pop(source, None)


def build_llm_billing_alert_text(
    error_detail: Any,
    *,
    source: str = "unknown",
) -> str:
    detail = _normalize_error_text(error_detail)[:500]
    prefix = f"【{source}】" if source else ""
    return f"{prefix}大模型服务异常/欠费，请及时充值。详情：{detail}"


def send_llm_billing_alert_to_admin(
    error_detail: Any,
    *,
    user_id: str = DEFAULT_ADMIN_USER_ID,
    source: str = "unknown",
    dry_run: bool = False,
) -> dict[str, Any]:
    """向管理员飞书私信发送 LLM 欠费/异常告警。"""
    text = build_llm_billing_alert_text(error_detail, source=source)
    uid = (user_id or DEFAULT_ADMIN_USER_ID).strip()

    if dry_run:
        logger.info("LLM 告警 dry-run → %s: %s", uid, text[:120])
        return {
            "ok": True,
            "skipped": True,
            "dry_run": True,
            "user_id": uid,
            "text": text,
            "source": source,
        }

    from src.core.feishu_im_client import FeishuImClient

    client = FeishuImClient()
    if not client.app_id or not client.app_secret:
        logger.warning("LLM 告警跳过：未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
        return {
            "ok": False,
            "skipped": True,
            "reason": "im_not_configured",
            "user_id": uid,
            "text": text,
            "source": source,
        }

    result = client.send_text_to_user(uid, text)
    ok = bool(result.get("ok"))
    if ok:
        logger.info("LLM 欠费告警已推送 user_id=%s source=%s", uid, source)
    else:
        logger.warning(
            "LLM 欠费告警推送失败 user_id=%s source=%s err=%s",
            uid,
            source,
            result.get("error") or result.get("response"),
        )
    return {
        "ok": ok,
        "user_id": uid,
        "text": text,
        "source": source,
        **result,
    }


def maybe_alert_on_llm_error(
    error_detail: Any,
    *,
    source: str = "unknown",
    user_id: str = DEFAULT_ADMIN_USER_ID,
    dry_run: bool = False,
    force: bool = False,
) -> Optional[dict[str, Any]]:
    """若错误为欠费/配额类，则向管理员发送飞书告警（带冷却）。"""
    if not error_detail:
        return None
    if not is_llm_billing_error(error_detail):
        return None
    if not force and _in_cooldown(source):
        logger.debug("LLM 告警冷却中，跳过 source=%s", source)
        return {"ok": True, "skipped": True, "reason": "cooldown", "source": source}

    result = send_llm_billing_alert_to_admin(
        error_detail,
        user_id=user_id,
        source=source,
        dry_run=dry_run,
    )
    if result.get("ok") and not result.get("dry_run"):
        _mark_alert_sent(source)
    return result
