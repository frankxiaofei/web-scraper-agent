"""OpenAI 兼容多轮对话客户端（规则生成、分类等共用）。"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 保守上限：约 400k 字符 ≈ 100k–200k tokens（视中英文比例）
DEFAULT_MAX_LLM_MESSAGES = 40
DEFAULT_MAX_LLM_TOTAL_CHARS = 400_000
DEFAULT_MAX_CONTENT_CHARS = 12_000
DEFAULT_MAX_TOOL_CONTENT_CHARS = 8_000
DEFAULT_MAX_HISTORY_MESSAGES = 30
DEFAULT_MAX_HISTORY_CONTENT_CHARS = 8_000

# 工具结果中常见的大字段，优先裁剪
_TOOL_RESULT_HEAVY_KEYS = frozenset(
    {
        "content_text",
        "content_html",
        "html",
        "stdout",
        "stderr",
        "tree",
        "yaml",
        "code",
        "raw",
        "body",
    }
)


def truncate_text(text: str, max_chars: int) -> str:
    """截断文本并附加省略标记。"""
    if max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = f"\n…[已截断，原长 {len(text)} 字符]"
    keep = max(1, max_chars - len(suffix))
    return text[:keep] + suffix


def estimate_messages_chars(messages: list[dict[str, Any]]) -> int:
    """估算 messages 序列化后的字符总量。"""
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content, ensure_ascii=False, default=str))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return total


def _trim_tool_result_payload(result: Any, *, max_items: int = 30) -> Any:
    """递归裁剪工具结果中的大字段与长列表。"""
    if isinstance(result, dict):
        trimmed: dict[str, Any] = {}
        for key, val in result.items():
            if key in _TOOL_RESULT_HEAVY_KEYS and isinstance(val, str):
                trimmed[key] = truncate_text(val, 500)
            elif key == "items" and isinstance(val, list):
                trimmed[key] = [_trim_tool_result_payload(item, max_items=max_items) for item in val[:max_items]]
                if len(val) > max_items:
                    trimmed["items_truncated"] = True
                    trimmed["items_total"] = len(val)
            else:
                trimmed[key] = _trim_tool_result_payload(val, max_items=max_items)
        return trimmed
    if isinstance(result, list):
        if len(result) > max_items:
            return [_trim_tool_result_payload(item, max_items=max_items) for item in result[:max_items]]
        return [_trim_tool_result_payload(item, max_items=max_items) for item in result]
    if isinstance(result, str) and len(result) > 2000:
        return truncate_text(result, 2000)
    return result


def serialize_tool_result_for_llm(result: Any, *, max_chars: int = DEFAULT_MAX_TOOL_CONTENT_CHARS) -> str:
    """将工具结果序列化为 LLM 可接受的 JSON 字符串（带截断）。"""
    for max_items in (30, 15, 8, 3, 0):
        trimmed = _trim_tool_result_payload(result, max_items=max_items)
        text = json.dumps(trimmed, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return text
    # 最后兜底：仅保留 ok/error/summary 等短字段
    slim = {
        k: v
        for k, v in _trim_tool_result_payload(result, max_items=0).items()
        if k in ("ok", "error", "summary", "count", "saved", "duplicate", "site_id", "hint")
    }
    text = json.dumps(slim, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return truncate_text(text, max_chars)


def _drop_leading_orphan_tools(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃开头无前置 tool_calls 的 tool 消息（截断后可能破坏配对）。"""
    while messages and messages[0].get("role") == "tool":
        messages.pop(0)
    return messages


def _pop_tool_followups(messages: list[dict[str, Any]], start_idx: int) -> None:
    """从 start_idx 起移除连续的 tool 消息（配合丢弃含 tool_calls 的 assistant）。"""
    while start_idx < len(messages) and messages[start_idx].get("role") == "tool":
        messages.pop(start_idx)


def truncate_messages_for_llm(
    messages: list[dict[str, Any]],
    *,
    max_messages: int = DEFAULT_MAX_LLM_MESSAGES,
    max_total_chars: int = DEFAULT_MAX_LLM_TOTAL_CHARS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    max_tool_content_chars: int = DEFAULT_MAX_TOOL_CONTENT_CHARS,
) -> list[dict[str, Any]]:
    """截断消息历史，防止 prompt 超出模型上下文窗口。

    - 始终保留首条 system 消息
    - 保留最近 max_messages 条非 system 消息
    - 单条 content 按 role 截断（tool 更短）
    - 若总字符仍超限，从最早非 system 消息起丢弃
    - 截断后修复 assistant.tool_calls 与 tool 消息的配对
    """
    if not messages:
        return []

    system_msgs: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system" and not rest and not system_msgs:
            system_msgs.append(msg)
        else:
            rest.append(msg)

    if max_messages > 0 and len(rest) > max_messages:
        dropped = len(rest) - max_messages
        rest = rest[-max_messages:]
        rest = _drop_leading_orphan_tools(rest)
        logger.info("LLM 消息历史截断：丢弃最早 %d 条非 system 消息", dropped)

    def _cap(msg: dict[str, Any]) -> dict[str, Any]:
        out = dict(msg)
        role = out.get("role")
        limit = max_tool_content_chars if role == "tool" else max_content_chars
        content = out.get("content")
        if isinstance(content, str) and len(content) > limit:
            out["content"] = truncate_text(content, limit)
        return out

    capped = [_cap(m) for m in rest]
    combined = system_msgs + capped

    while len(combined) > len(system_msgs) and estimate_messages_chars(combined) > max_total_chars:
        drop_idx = len(system_msgs)
        dropped_msg = combined[drop_idx]
        logger.info(
            "LLM 消息总字符超限（%d > %d），丢弃第 %d 条",
            estimate_messages_chars(combined),
            max_total_chars,
            drop_idx,
        )
        combined.pop(drop_idx)
        if dropped_msg.get("role") == "assistant" and dropped_msg.get("tool_calls"):
            _pop_tool_followups(combined, drop_idx)

    rest_out = _drop_leading_orphan_tools(combined[len(system_msgs) :])
    return system_msgs + rest_out


def truncate_chat_history(
    history: list[dict[str, str]],
    *,
    max_messages: int = DEFAULT_MAX_HISTORY_MESSAGES,
    max_content_chars: int = DEFAULT_MAX_HISTORY_CONTENT_CHARS,
) -> list[dict[str, str]]:
    """截断 user/assistant 对话历史（Hermes 会话加载用）。"""
    if not history:
        return []
    trimmed = history[-max_messages:] if max_messages > 0 else []
    return [
        {
            "role": msg.get("role", "user"),
            "content": truncate_text(str(msg.get("content") or ""), max_content_chars),
        }
        for msg in trimmed
        if msg.get("role") in ("user", "assistant") and (msg.get("content") or "").strip()
    ]


class LLMChatClient:
    """轻量 chat/completions 封装，不依赖 intelligent_crawl_llm 开关。"""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        from src.core.config import get_settings

        settings = get_settings()
        self.api_key = api_key or settings.openai_api_key or ""
        self.base_url = (base_url or settings.llm_base_url or "https://api.openai.com").rstrip("/")
        self.model = model or settings.llm_model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        timeout: int = 60,
    ) -> tuple[Optional[str], Optional[str]]:
        """调用 chat/completions，返回 (content, error_message)。"""
        message, err = self.chat_completion(messages, temperature=temperature, timeout=timeout)
        if err:
            return None, err
        content = (message or {}).get("content") or ""
        return content.strip() if content else None, None

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str | dict[str, Any]] = "auto",
        temperature: float = 0.1,
        timeout: int = 120,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """调用 chat/completions，返回 (assistant_message_dict, error_message)。"""
        if not self.available:
            return None, "未配置 OPENAI_API_KEY"

        import urllib.error
        import urllib.request

        safe_messages = truncate_messages_for_llm(messages)

        payload_body: dict[str, Any] = {
            "model": self.model,
            "messages": safe_messages,
            "temperature": temperature,
        }
        if tools:
            payload_body["tools"] = tools
            if tool_choice is not None:
                payload_body["tool_choice"] = tool_choice

        body = json.dumps(payload_body, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/v1/chat/completions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            message = payload["choices"][0]["message"]
            return message, None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            logger.warning("LLM HTTP 错误 %s: %s", e.code, err_body)
            if e.code == 402:
                err = "LLM 账户余额不足 (HTTP 402)，请充值后重试"
            elif e.code == 401:
                err = "LLM API Key 无效 (HTTP 401)"
            else:
                err = f"LLM HTTP 错误 {e.code}: {err_body}"
            from src.core.llm_alert_service import maybe_alert_on_llm_error

            maybe_alert_on_llm_error(err, source="llm_chat")
            return None, err
        except Exception as e:
            logger.warning("LLM 请求失败: %s", e)
            err = f"LLM 请求失败: {e}"
            from src.core.llm_alert_service import maybe_alert_on_llm_error

            maybe_alert_on_llm_error(err, source="llm_chat")
            return None, err


def create_chat_client() -> LLMChatClient:
    return LLMChatClient()
