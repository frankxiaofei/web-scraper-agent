"""LLM 欠费/异常飞书告警测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.llm_alert_service import (
    build_llm_billing_alert_text,
    is_llm_billing_error,
    maybe_alert_on_llm_error,
    reset_llm_alert_cooldown,
    send_llm_billing_alert_to_admin,
)


@pytest.fixture(autouse=True)
def _clear_alert_cooldown():
    reset_llm_alert_cooldown()
    yield
    reset_llm_alert_cooldown()


@pytest.mark.parametrize(
    "error_text,expected",
    [
        ("LLM 账户余额不足 (HTTP 402)，请充值后重试", True),
        ("LLM HTTP 错误 429: rate limit exceeded", True),
        ("insufficient_quota: billing hard limit", True),
        ("账户欠费，请充值", True),
        ("LLM API Key 无效 (HTTP 401)", False),
        ("连接超时", False),
        ("", False),
    ],
)
def test_is_llm_billing_error(error_text, expected):
    assert is_llm_billing_error(error_text) is expected


def test_build_llm_billing_alert_text():
    text = build_llm_billing_alert_text("HTTP 402 balance", source="test_source")
    assert "大模型服务异常/欠费" in text
    assert "请及时充值" in text
    assert "test_source" in text
    assert "HTTP 402 balance" in text


def test_send_llm_billing_alert_dry_run():
    result = send_llm_billing_alert_to_admin(
        "quota exceeded",
        user_id="li_xf10",
        source="unit_test",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["user_id"] == "li_xf10"


def test_maybe_alert_on_llm_error_triggers_im_send():
    mock_client = MagicMock()
    mock_client.app_id = "cli_test"
    mock_client.app_secret = "secret"
    mock_client.send_text_to_user.return_value = {"ok": True, "channel": "im"}

    with patch("src.core.feishu_im_client.FeishuImClient", return_value=mock_client):
        result = maybe_alert_on_llm_error(
            "LLM HTTP 错误 402: insufficient balance",
            source="llm_chat",
            force=True,
        )

    assert result is not None
    assert result["ok"] is True
    mock_client.send_text_to_user.assert_called_once()
    args = mock_client.send_text_to_user.call_args[0]
    assert args[0] == "li_xf10"
    assert "大模型服务异常/欠费" in args[1]


def test_maybe_alert_on_llm_error_skips_non_billing():
    with patch("src.core.feishu_im_client.FeishuImClient") as mock_cls:
        result = maybe_alert_on_llm_error("连接超时", source="llm_chat", force=True)
    assert result is None
    mock_cls.assert_not_called()


def test_maybe_alert_on_llm_error_cooldown():
    mock_client = MagicMock()
    mock_client.app_id = "cli_test"
    mock_client.app_secret = "secret"
    mock_client.send_text_to_user.return_value = {"ok": True, "channel": "im"}

    with patch("src.core.feishu_im_client.FeishuImClient", return_value=mock_client):
        first = maybe_alert_on_llm_error("HTTP 429 rate limit", source="same_source")
        second = maybe_alert_on_llm_error("HTTP 429 rate limit", source="same_source")

    assert first is not None and first.get("ok") is True
    assert second == {"ok": True, "skipped": True, "reason": "cooldown", "source": "same_source"}
    assert mock_client.send_text_to_user.call_count == 1


def test_llm_chat_hooks_alert_on_http_402(monkeypatch):
    import urllib.error

    from src.core.llm_chat import LLMChatClient

    def fake_urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="http://test/v1/chat/completions",
            code=402,
            msg="Payment Required",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    mock_client = MagicMock()
    mock_client.app_id = "cli_test"
    mock_client.app_secret = "secret"
    mock_client.send_text_to_user.return_value = {"ok": True, "channel": "im"}

    with patch("src.core.feishu_im_client.FeishuImClient", return_value=mock_client):
        client = LLMChatClient(api_key="test-key")
        content, err = client.chat([{"role": "user", "content": "hi"}])

    assert content is None
    assert err and "402" in err
    mock_client.send_text_to_user.assert_called_once()
