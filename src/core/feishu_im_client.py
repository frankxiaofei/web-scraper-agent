"""飞书开放平台应用机器人 — 单聊/私信（Open API IM）。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
MESSAGES_URL = f"{FEISHU_API_BASE}/im/v1/messages"


class FeishuImClient:
    """飞书应用机器人 IM 客户端（tenant_access_token + 单聊消息）。"""

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        receive_user: Optional[str] = None,
        receive_open_id: Optional[str] = None,
        receive_email: Optional[str] = None,
    ) -> None:
        if any(v is None for v in (app_id, app_secret, receive_user, receive_open_id, receive_email)):
            from src.core.config import get_settings

            settings = get_settings()
            if app_id is None:
                app_id = settings.feishu_app_id
            if app_secret is None:
                app_secret = settings.feishu_app_secret
            if receive_user is None:
                receive_user = settings.feishu_receive_user
            if receive_open_id is None:
                receive_open_id = settings.feishu_user_open_id
            if receive_email is None:
                receive_email = settings.feishu_receive_email

        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.receive_user = (receive_user or "").strip() or None
        self.receive_open_id = (receive_open_id or "").strip() or None
        self.receive_email = (receive_email or "").strip() or None
        self._tenant_token: Optional[str] = None
        self._token_expire_at: float = 0.0
        self._resolved_open_id: Optional[str] = None

    @property
    def configured(self) -> bool:
        if not self.app_id or not self.app_secret:
            return False
        return bool(self.receive_open_id or self.receive_user or self.receive_email)

    def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        now = time.time()
        if (
            not force_refresh
            and self._tenant_token
            and now < self._token_expire_at - 60
        ):
            return self._tenant_token

        if not self.app_id or not self.app_secret:
            raise ValueError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")

        resp = httpx.post(
            TOKEN_URL,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")

        self._tenant_token = data["tenant_access_token"]
        expire = int(data.get("expire") or 7200)
        self._token_expire_at = now + expire
        return self._tenant_token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_tenant_access_token()}"}

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = FEISHU_API_BASE + path
        resp = httpx.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=self._auth_headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"飞书 API 错误: {data}")
        return data

    def resolve_receive_open_id(self) -> str:
        """解析收件人 open_id（优先 FEISHU_USER_OPEN_ID，否则 user_id / email 查询）。"""
        if self._resolved_open_id:
            return self._resolved_open_id
        if self.receive_open_id:
            self._resolved_open_id = self.receive_open_id
            return self._resolved_open_id

        if self.receive_user:
            data = self._api_request(
                "GET",
                f"/contact/v3/users/{self.receive_user}",
                params={"user_id_type": "user_id"},
            )
            user = data.get("data", {}).get("user") or {}
            open_id = (user.get("open_id") or "").strip()
            if open_id:
                self._resolved_open_id = open_id
                return open_id
            raise RuntimeError(
                f"无法解析用户 {self.receive_user!r} 的 open_id: {data}"
            )

        if self.receive_email:
            data = self._api_request(
                "POST",
                "/contact/v3/users/batch_get_id",
                params={"user_id_type": "open_id"},
                json_body={"emails": [self.receive_email]},
            )
            user_list = data.get("data", {}).get("user_list") or []
            for item in user_list:
                open_id = (item.get("user_id") or "").strip()
                if open_id:
                    self._resolved_open_id = open_id
                    return open_id
            raise RuntimeError(
                f"无法通过邮箱 {self.receive_email!r} 解析 open_id: {data}"
            )

        raise ValueError(
            "未配置收件人：请设置 FEISHU_USER_OPEN_ID、FEISHU_RECEIVE_USER 或 FEISHU_RECEIVE_EMAIL"
        )

    def send_message(
        self,
        msg_type: str,
        content: dict[str, Any],
        *,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        """发送 IM 消息（content 会被序列化为 JSON 字符串）。"""
        if not self.configured:
            return {"ok": False, "skipped": True, "reason": "im_not_configured"}

        receive_id = self.resolve_receive_open_id()
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        try:
            resp = httpx.post(
                MESSAGES_URL,
                params={"receive_id_type": receive_id_type},
                json=body,
                headers=self._auth_headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                logger.warning("飞书 IM 返回错误: %s", data)
                return {"ok": False, "response": data, "channel": "im"}
            logger.info("飞书私信推送成功 (receive_id=%s)", receive_id[:8] + "...")
            return {"ok": True, "response": data, "channel": "im", "receive_id": receive_id}
        except Exception as exc:
            logger.error("飞书 IM 发送失败: %s", exc)
            return {"ok": False, "error": str(exc), "channel": "im"}

    def send_text(self, text: str, *, bot_name: Optional[str] = None) -> dict[str, Any]:
        from src.core.feishu_webhook import format_feishu_text_message

        return self.send_message(
            "text",
            {"text": format_feishu_text_message(text, bot_name=bot_name)},
        )

    def send_text_to_user(
        self,
        user_id: str,
        text: str,
        *,
        bot_name: Optional[str] = None,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        """向指定飞书 user_id 发送纯文本私信。"""
        from src.core.feishu_webhook import format_feishu_text_message

        if not self.app_id or not self.app_secret:
            return {"ok": False, "skipped": True, "reason": "im_not_configured"}

        uid = (user_id or "").strip()
        if not uid:
            return {"ok": False, "error": "user_id 为空", "channel": "im"}

        prev_user = self.receive_user
        prev_open = self.receive_open_id
        prev_email = self.receive_email
        prev_resolved = self._resolved_open_id
        try:
            self.receive_user = uid
            self.receive_open_id = None
            self.receive_email = None
            self._resolved_open_id = None
            receive_id = self.resolve_receive_open_id()
            body = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": format_feishu_text_message(text, bot_name=bot_name)},
                    ensure_ascii=False,
                ),
            }
            resp = httpx.post(
                MESSAGES_URL,
                params={"receive_id_type": receive_id_type},
                json=body,
                headers=self._auth_headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                logger.warning("飞书 IM 文本返回错误 user=%s: %s", uid, data)
                return {"ok": False, "response": data, "channel": "im", "user_id": uid}
            logger.info("飞书私信文本推送成功 user_id=%s", uid)
            return {
                "ok": True,
                "response": data,
                "channel": "im",
                "receive_id": receive_id,
                "user_id": uid,
            }
        except Exception as exc:
            logger.error("飞书 IM 文本发送失败 user=%s: %s", uid, exc)
            return {"ok": False, "error": str(exc), "channel": "im", "user_id": uid}
        finally:
            self.receive_user = prev_user
            self.receive_open_id = prev_open
            self.receive_email = prev_email
            self._resolved_open_id = prev_resolved

    def send_interactive_card(self, card: dict[str, Any]) -> dict[str, Any]:
        return self.send_message("interactive", card)

    def send_interactive_card_to_user(
        self,
        user_id: str,
        card: dict[str, Any],
        *,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        """向指定飞书 user_id 发送 interactive 卡片（不复用实例级收件人缓存）。"""
        if not self.configured and not ((self.app_id and self.app_secret)):
            return {"ok": False, "skipped": True, "reason": "im_not_configured"}

        uid = (user_id or "").strip()
        if not uid:
            return {"ok": False, "error": "user_id 为空", "channel": "im"}

        prev_user = self.receive_user
        prev_open = self.receive_open_id
        prev_resolved = self._resolved_open_id
        try:
            self.receive_user = uid
            self.receive_open_id = None
            self.receive_email = None
            self._resolved_open_id = None
            receive_id = self.resolve_receive_open_id()
            body = {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
            resp = httpx.post(
                MESSAGES_URL,
                params={"receive_id_type": receive_id_type},
                json=body,
                headers=self._auth_headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                logger.warning("飞书 IM 返回错误 user=%s: %s", uid, data)
                return {"ok": False, "response": data, "channel": "im", "user_id": uid}
            logger.info("飞书私信推送成功 user_id=%s", uid)
            return {
                "ok": True,
                "response": data,
                "channel": "im",
                "receive_id": receive_id,
                "user_id": uid,
            }
        except Exception as exc:
            logger.error("飞书 IM 发送失败 user=%s: %s", uid, exc)
            return {"ok": False, "error": str(exc), "channel": "im", "user_id": uid}
        finally:
            self.receive_user = prev_user
            self.receive_open_id = prev_open
            self._resolved_open_id = prev_resolved
