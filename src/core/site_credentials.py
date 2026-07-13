"""站点登录凭据：加密落盘 data/site_credentials/，供 sync / RuleExecutor 注入 Cookie。"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from src.core.config import get_settings

logger = logging.getLogger(__name__)

_FORMAT_VERSION = 1
_current_auth: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "site_credentials_auth", default=None
)


def _credentials_dir() -> Path:
    path = get_settings().data_dir / "site_credentials"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _credential_path(site_id: str) -> Path:
    safe_id = re.sub(r"[^\w\-.]", "_", site_id)
    return _credentials_dir() / f"{safe_id}.json"


def _derive_key() -> bytes:
    settings = get_settings()
    secret = settings.site_credentials_key or "dev-insecure-key-change-me"
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _encrypt_payload(data: dict[str, Any]) -> str:
    plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = _derive_key()
    encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(plaintext))
    return base64.urlsafe_b64encode(encrypted).decode("ascii")


def _decrypt_payload(blob: str) -> dict[str, Any]:
    key = _derive_key()
    encrypted = base64.urlsafe_b64decode(blob.encode("ascii"))
    plaintext = bytes(c ^ key[i % len(key)] for i, c in enumerate(encrypted))
    return json.loads(plaintext.decode("utf-8"))


def _default_domain(site_url: str) -> str:
    host = urlparse(site_url).hostname or ""
    if host and not host.startswith("."):
        return f".{host}"
    return host


def _parse_cookie_header(cookie_header: str, default_domain: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": default_domain,
                "path": "/",
            }
        )
    return cookies


def _normalize_cookies(
    cookies: list[dict[str, Any]], default_domain: str
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in cookies:
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", "")).strip()
        if not name:
            continue
        normalized.append(
            {
                "name": name,
                "value": value,
                "domain": str(item.get("domain") or default_domain).strip() or default_domain,
                "path": str(item.get("path") or "/"),
            }
        )
    return normalized


def save_site_credentials(
    site_id: str,
    *,
    site_url: str = "",
    credential_type: str = "cookie",
    cookie_header: Optional[str] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    headers: Optional[dict[str, str]] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """保存站点凭据（加密写入 data/site_credentials/）。"""
    default_domain = _default_domain(site_url)
    stored_cookies: list[dict[str, Any]] = []
    if cookie_header:
        stored_cookies.extend(_parse_cookie_header(cookie_header, default_domain))
    if cookies:
        stored_cookies.extend(_normalize_cookies(cookies, default_domain))

    stored_headers = {str(k): str(v) for k, v in (headers or {}).items() if str(k).strip()}
    if not stored_cookies and not stored_headers:
        raise ValueError("需提供 cookie_header、cookies 或 headers 至少一项")

    updated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "site_id": site_id,
        "credential_type": credential_type,
        "cookies": stored_cookies,
        "headers": stored_headers,
        "notes": notes or "",
        "updated_at": updated_at,
    }
    envelope = {
        "v": _FORMAT_VERSION,
        "encrypted": _encrypt_payload(payload),
    }
    path = _credential_path(site_id)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("站点 %s 凭据已保存: %s", site_id, path)
    return {
        "ok": True,
        "site_id": site_id,
        "credential_type": credential_type,
        "cookie_count": len(stored_cookies),
        "header_keys": sorted(stored_headers.keys()),
        "updated_at": updated_at,
        "path": str(path),
    }


def load_site_credentials(site_id: str) -> Optional[dict[str, Any]]:
    """读取并解密站点凭据；不存在时返回 None。"""
    path = _credential_path(site_id)
    if not path.is_file():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取站点凭据失败 %s: %s", site_id, exc)
        return None
    encrypted = envelope.get("encrypted")
    if not encrypted:
        return None
    try:
        return _decrypt_payload(str(encrypted))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("解密站点凭据失败 %s: %s", site_id, exc)
        return None


def delete_site_credentials(site_id: str) -> dict[str, Any]:
    path = _credential_path(site_id)
    if not path.is_file():
        return {"ok": False, "site_id": site_id, "message": "凭据不存在"}
    path.unlink()
    logger.info("站点 %s 凭据已删除", site_id)
    return {"ok": True, "site_id": site_id, "message": "凭据已删除"}


def get_credentials_metadata(site_id: str) -> dict[str, Any]:
    """返回凭据元数据（不含 Cookie 值）。"""
    data = load_site_credentials(site_id)
    if not data:
        return {
            "ok": True,
            "site_id": site_id,
            "has_credentials": False,
        }
    cookies = data.get("cookies") or []
    headers = data.get("headers") or {}
    return {
        "ok": True,
        "site_id": site_id,
        "has_credentials": True,
        "credential_type": data.get("credential_type", "cookie"),
        "cookie_names": [c.get("name") for c in cookies if c.get("name")],
        "cookie_count": len(cookies),
        "header_keys": sorted(headers.keys()),
        "notes": data.get("notes") or "",
        "updated_at": data.get("updated_at"),
    }


def get_browser_auth(site_id: str) -> dict[str, Any]:
    """转为 Playwright context 选项：cookies + extra_http_headers。"""
    data = load_site_credentials(site_id)
    if not data:
        return {"cookies": [], "extra_http_headers": {}}

    cookies = list(data.get("cookies") or [])
    headers = dict(data.get("headers") or {})
    cookie_header = data.get("cookie_header")
    if cookie_header and not cookies:
        cookies = _parse_cookie_header(str(cookie_header), "")

    extra_http_headers = dict(headers)
    if cookies and "Cookie" not in extra_http_headers:
        extra_http_headers["Cookie"] = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies if c.get("name")
        )
    return {"cookies": cookies, "extra_http_headers": extra_http_headers}


def set_site_auth_context(site_id: str) -> contextvars.Token:
    """在 sync / dry-run 调用链中注入当前站点凭据。"""
    return _current_auth.set(get_browser_auth(site_id))


def reset_site_auth_context(token: contextvars.Token) -> None:
    try:
        _current_auth.reset(token)
    except ValueError:
        # 跨 asyncio Task / 线程复用 token 时无法 reset，退化为清空
        _current_auth.set(None)


def get_current_browser_auth() -> dict[str, Any]:
    """BrowserPool.new_page 读取当前 context 中的凭据。"""
    auth = _current_auth.get()
    if not auth:
        return {"cookies": [], "extra_http_headers": {}}
    return auth
