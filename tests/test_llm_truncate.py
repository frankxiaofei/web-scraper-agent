"""LLM prompt 截断 helper 单元测试。"""

from __future__ import annotations

import json

from src.core.llm_chat import (
    DEFAULT_MAX_LLM_TOTAL_CHARS,
    DEFAULT_MAX_TOOL_CONTENT_CHARS,
    estimate_messages_chars,
    serialize_tool_result_for_llm,
    truncate_chat_history,
    truncate_messages_for_llm,
    truncate_text,
)


def test_truncate_text_adds_marker():
    result = truncate_text("a" * 100, 50)
    assert len(result) <= 50
    assert "已截断" in result


def test_truncate_chat_history_limits_count_and_length():
    history = [{"role": "user", "content": "x" * 20_000} for _ in range(50)]
    trimmed = truncate_chat_history(history, max_messages=10, max_content_chars=1000)
    assert len(trimmed) == 10
    assert all(len(m["content"]) <= 1000 for m in trimmed)


def test_truncate_messages_for_llm_keeps_system_and_recent():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(60):
        messages.append({"role": "user", "content": f"msg-{i}"})
    trimmed = truncate_messages_for_llm(messages, max_messages=20, max_total_chars=999_999)
    assert trimmed[0]["role"] == "system"
    assert len(trimmed) == 21  # system + 20
    assert trimmed[-1]["content"] == "msg-59"


def test_truncate_messages_for_llm_total_chars_cap():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(30):
        messages.append({"role": "user", "content": "z" * 20_000})
    trimmed = truncate_messages_for_llm(
        messages,
        max_messages=30,
        max_total_chars=50_000,
        max_content_chars=5_000,
    )
    assert estimate_messages_chars(trimmed) <= 50_000


def test_serialize_tool_result_strips_heavy_fields():
    result = {
        "ok": True,
        "content_text": "x" * 10_000,
        "items": [{"title": f"t{i}", "url": f"http://u{i}"} for i in range(100)],
    }
    text = serialize_tool_result_for_llm(result, max_chars=DEFAULT_MAX_TOOL_CONTENT_CHARS)
    assert len(text) <= DEFAULT_MAX_TOOL_CONTENT_CHARS
    parsed = json.loads(text)
    assert len(parsed.get("items", [])) <= 30
    assert len(parsed.get("content_text", "")) <= 500


def test_truncate_messages_never_exceeds_default_budget():
    """模拟 Hermes 长会话：截断后应远低于默认总字符上限。"""
    messages = [{"role": "system", "content": "Hermes"}]
    for turn in range(80):
        messages.append({"role": "assistant", "content": "plan " * 500, "tool_calls": [{"id": "1"}]})
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "1",
                "content": serialize_tool_result_for_llm(
                    {"items": [{"title": "公告", "url": "http://x", "content_text": "c" * 5000}] * 20}
                ),
            }
        )
    trimmed = truncate_messages_for_llm(messages)
    assert estimate_messages_chars(trimmed) <= DEFAULT_MAX_LLM_TOTAL_CHARS


def _assert_tool_pairing(messages: list[dict]) -> None:
    """每条 tool 消息前必须存在含 tool_calls 的 assistant（允许同一 assistant 后接多条 tool）。"""
    last_assistant_with_tools = False
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            last_assistant_with_tools = True
        elif role == "tool":
            assert last_assistant_with_tools, f"orphan tool message: {msg}"
        elif role in ("user", "assistant", "system"):
            last_assistant_with_tools = False


def test_truncate_messages_preserves_tool_pairing_on_odd_drop():
    """奇数条截断不能把 assistant 去掉而留下 orphan tool。"""
    messages = [{"role": "system", "content": "sys"}]
    for turn in range(30):
        tid = f"call_{turn}"
        messages.append({"role": "assistant", "content": "p", "tool_calls": [{"id": tid}]})
        messages.append({"role": "tool", "tool_call_id": tid, "content": "r"})
    trimmed = truncate_messages_for_llm(messages, max_messages=39)
    _assert_tool_pairing(trimmed)
    assert trimmed[1]["role"] != "tool"


def test_truncate_messages_preserves_tool_pairing_on_char_cap():
    """按字符上限逐条丢弃时，不能留下无 tool_calls 前置的 tool。"""
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    for turn in range(8):
        tid = f"c{turn}"
        messages.append({"role": "assistant", "content": "x", "tool_calls": [{"id": tid}]})
        messages.append({"role": "tool", "tool_call_id": tid, "content": "y" * 20_000})
    for budget in (100_000, 60_000, 40_000, 20_000):
        trimmed = truncate_messages_for_llm(messages, max_messages=100, max_total_chars=budget)
        _assert_tool_pairing(trimmed)
