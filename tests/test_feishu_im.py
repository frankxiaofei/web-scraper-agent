"""飞书 IM / 推送通道测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.feishu_im_client import FeishuImClient
from src.core.feishu_push import resolve_feishu_channel, send_feishu_interactive_card


def test_feishu_im_client_configured():
    client = FeishuImClient(
        app_id="cli_test",
        app_secret="secret",
        receive_user="li_xf10",
    )
    assert client.configured is True


def test_feishu_im_client_not_configured():
    client = FeishuImClient(app_id="", app_secret="", receive_user="")
    assert client.configured is False


def test_resolve_feishu_channel_auto_prefers_im():
    with patch("src.core.feishu_push.FeishuImClient") as mock_im_cls, patch(
        "src.core.feishu_push.FeishuWebhookClient"
    ) as mock_wh_cls:
        mock_im_cls.return_value.configured = True
        mock_wh_cls.return_value.configured = True
        assert resolve_feishu_channel("auto") == "im"


def test_resolve_feishu_channel_auto_fallback_webhook():
    with patch("src.core.feishu_push.FeishuImClient") as mock_im_cls, patch(
        "src.core.feishu_push.FeishuWebhookClient"
    ) as mock_wh_cls:
        mock_im_cls.return_value.configured = False
        mock_wh_cls.return_value.configured = True
        assert resolve_feishu_channel("auto") == "webhook"


def test_feishu_im_send_interactive_mocked():
    client = FeishuImClient(
        app_id="cli_test",
        app_secret="secret",
        receive_open_id="ou_test_open_id",
    )
    client._tenant_token = "test_token"
    client._token_expire_at = 9999999999.0
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"code": 0, "data": {"message_id": "om_test"}}
    mock_resp.raise_for_status = MagicMock()

    with patch("src.core.feishu_im_client.httpx.post", return_value=mock_resp) as mock_post:
        result = client.send_interactive_card(
            {"header": {"title": {"tag": "plain_text", "content": "test"}}}
        )

    assert result["ok"] is True
    assert result["channel"] == "im"
    mock_post.assert_called_once()
    body = mock_post.call_args.kwargs["json"]
    assert body["msg_type"] == "interactive"
    assert body["receive_id"] == "ou_test_open_id"


def test_send_feishu_interactive_card_im_path():
    card = {"elements": []}
    mock_im = MagicMock()
    mock_im.configured = True
    mock_im.send_interactive_card.return_value = {"ok": True, "channel": "im"}

    with patch("src.core.feishu_push.resolve_feishu_channel", return_value="im"), patch(
        "src.core.feishu_push.FeishuImClient", return_value=mock_im
    ):
        result = send_feishu_interactive_card(card, channel="im")

    assert result["ok"] is True
    mock_im.send_interactive_card.assert_called_once_with(card)
