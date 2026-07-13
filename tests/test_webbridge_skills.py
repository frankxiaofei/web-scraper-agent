"""Kimi WebBridge skill 工具单元测试（mock subprocess / HTTP）。"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
import subprocess
import urllib.request

from pathlib import Path

from src.web.crawl_agent_tools import CrawlAgentToolExecutor, DEFAULT_TOOL_SCHEMAS
from src.web import webbridge_skills as wb

_ORIGINAL_SUBPROCESS_RUN = subprocess.run
_ORIGINAL_URLOPEN = urllib.request.urlopen


@pytest.fixture(autouse=True)
def _reset_webbridge_hooks():
    """每个测试后恢复可注入的 subprocess/urlopen 钩子。"""
    yield
    wb._subprocess_run = _ORIGINAL_SUBPROCESS_RUN
    wb._urlopen = _ORIGINAL_URLOPEN


def test_default_tool_schemas_include_webbridge():
    names = {s["function"]["name"] for s in DEFAULT_TOOL_SCHEMAS}
    for tool in (
        "skill_webbridge_check",
        "skill_webbridge_navigate",
        "skill_webbridge_extract_list",
        "skill_webbridge_get_html",
        "skill_webbridge_close",
        "skill_webbridge_snapshot",
        "skill_webbridge_click",
        "skill_webbridge_fill",
        "skill_webbridge_screenshot",
        "skill_webbridge_evaluate",
        "skill_webbridge_scroll",
        "skill_webbridge_wait",
        "skill_webbridge_go",
        "skill_webbridge_select",
        "skill_webbridge_auto_crawl",
    ):
        assert tool in names
    assert len(names) == 37


def test_check_webbridge_healthy():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = json.dumps(
        {"running": True, "extension_connected": True, "version": "v1.9.7", "port": 10086}
    )
    proc.stderr = ""

    with patch.object(wb, "resolve_webbridge_binary", return_value="/bin/kimi-webbridge"), patch.object(
        wb, "_subprocess_run", return_value=proc
    ):
        result = wb.check_webbridge()

    assert result["ok"] is True
    assert result["available"] is True
    assert result["version"] == "v1.9.7"


def test_check_webbridge_binary_not_found():
    with patch.object(wb, "resolve_webbridge_binary", return_value=None):
        result = wb.check_webbridge()
    assert result["ok"] is False
    assert "未找到" in result["error"]
    assert "install_hint" in result


def test_check_webbridge_extension_disconnected():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = json.dumps({"running": True, "extension_connected": False})
    proc.stderr = ""

    with patch.object(wb, "resolve_webbridge_binary", return_value="/bin/kimi-webbridge"), patch.object(
        wb, "_subprocess_run", return_value=proc
    ):
        result = wb.check_webbridge()

    assert result["ok"] is False
    assert result["running"] is True
    assert "扩展" in result["error"]


def _mock_urlopen_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        return BytesIO(body)

    return _fake_urlopen


def test_webbridge_navigate_success():
    healthy = {"ok": True, "available": True, "running": True, "extension_connected": True}
    calls: list[str] = []

    def _urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body["action"])
        if body["action"] == "navigate":
            return BytesIO(json.dumps({"success": True, "url": "https://x.com", "tabId": 1}).encode())
        if body["action"] == "snapshot":
            return BytesIO(
                json.dumps(
                    {"url": "https://x.com", "title": "列表", "tree": {"role": "document"}}
                ).encode()
            )
        raise AssertionError(f"unexpected action {body['action']}")

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_navigate("https://x.com", session_id="test-session")

    assert result["ok"] is True
    assert result["title"] == "列表"
    assert result["session_id"] == "test-session"
    assert calls == ["navigate", "snapshot"]


def test_webbridge_extract_list_with_selector():
    healthy = {"ok": True, "available": True}
    items_json = json.dumps({"items": [{"title": "公告A", "url": "https://x.com/a"}], "count": 1})

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps({"value": items_json}).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_extract_list(selector=".list a", max_items=10)

    assert result["ok"] is True
    assert result["item_count"] == 1
    assert result["items"][0]["title"] == "公告A"


def test_webbridge_extract_list_daemon_v2_nested_data():
    """daemon v1.9+ 将 evaluate 结果包在 {ok, data:{type, value}} 内。"""
    healthy = {"ok": True, "available": True}
    items_json = json.dumps(
        {
            "items": [{"title": "公告B", "url": "https://x.com/b"}],
            "count": 1,
            "selector": ".list a",
            "hint": None,
        }
    )
    daemon_body = {"ok": True, "data": {"type": "string", "value": items_json}}

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps(daemon_body).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_extract_list(selector=".list a", hint="招标", max_items=10)

    assert result["ok"] is True
    assert result["item_count"] == 1
    assert result["items"][0]["url"] == "https://x.com/b"


def test_webbridge_extract_list_daemon_v2_object_value():
    healthy = {"ok": True, "available": True}
    daemon_body = {
        "ok": True,
        "data": {
            "type": "object",
            "value": {
                "items": [{"title": "公告C", "url": "https://x.com/c"}],
                "count": 1,
            },
        },
    }

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps(daemon_body).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_extract_list(hint="公告", max_items=10)

    assert result["ok"] is True
    assert result["item_count"] == 1


def test_daemon_command_error_object_message():
    healthy = {"ok": True, "available": True}
    err_body = {
        "ok": False,
        "error": {"code": "tool_error", "message": 'session "x" has no tab'},
    }

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps(err_body).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.daemon_command("evaluate", {"code": "1"}, session="x")

    assert result["ok"] is False
    assert "no tab" in result["error"]


def test_webbridge_navigate_daemon_v2_nested_data():
    healthy = {"ok": True, "available": True, "running": True, "extension_connected": True}
    calls: list[str] = []

    def _urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body["action"])
        if body["action"] == "navigate":
            return BytesIO(
                json.dumps(
                    {"ok": True, "data": {"success": True, "url": "https://x.com", "tabId": 1}}
                ).encode()
            )
        if body["action"] == "snapshot":
            return BytesIO(
                json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "url": "https://x.com",
                            "title": "列表",
                            "tree": {"role": "document"},
                        },
                    }
                ).encode()
            )
        raise AssertionError(f"unexpected action {body['action']}")

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_navigate("https://x.com", session_id="test-session")

    assert result["ok"] is True
    assert result["title"] == "列表"
    assert result["url"] == "https://x.com"


def test_webbridge_get_html_truncated():
    healthy = {"ok": True, "available": True}
    html = "x" * 100
    payload = json.dumps({"html": html, "length": len(html)})

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps({"value": payload}).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_get_html(max_chars=50)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["html"]) == 50


def test_webbridge_close_tab():
    healthy = {"ok": True, "available": True}

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps({"success": True, "closed": True}).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_close(close_session=False)

    assert result["ok"] is True
    assert result["action"] == "close_tab"


def test_webbridge_wait_success():
    healthy = {"ok": True, "available": True}
    with patch.object(wb, "check_webbridge", return_value=healthy), patch(
        "src.web.webbridge_skills.time.sleep"
    ) as mock_sleep:
        result = wb.webbridge_wait(1.5, session_id="wait-test")
    assert result["ok"] is True
    assert result["waited_seconds"] == 1.5
    mock_sleep.assert_called_once_with(1.5)


def test_webbridge_wait_too_long():
    result = wb.webbridge_wait(60)
    assert result["ok"] is False
    assert "30" in result["error"]


def test_webbridge_screenshot_saves_file(tmp_path):
    healthy = {"ok": True, "available": True}
    fake_png = b"\x89PNG fake"
    b64 = __import__("base64").b64encode(fake_png).decode()

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps({"data": b64, "format": "png"}).encode())

    with patch.object(wb, "SCREENSHOT_DIR", tmp_path), patch.object(
        wb, "check_webbridge", return_value=healthy
    ), patch.object(wb, "_urlopen", side_effect=_urlopen):
        result = wb.webbridge_screenshot(session_id="shot-test")

    assert result["ok"] is True
    assert Path(result["image_path"]).is_file()
    assert result["image_url"].startswith("/api/browser/screenshot/")


def test_webbridge_evaluate_truncates():
    healthy = {"ok": True, "available": True}
    long_val = "x" * 100

    def _urlopen(req, timeout=None):
        return BytesIO(json.dumps({"value": long_val, "type": "string"}).encode())

    with patch.object(wb, "check_webbridge", return_value=healthy), patch.object(
        wb, "_urlopen", side_effect=_urlopen
    ):
        result = wb.webbridge_evaluate("1+1", max_result_chars=20)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["value"]) == 20


def test_executor_webbridge_check_unavailable():
    with patch("src.web.crawl_agent_tools.check_webbridge") as mock_check:
        mock_check.return_value = {
            "ok": False,
            "available": False,
            "error": "daemon 未运行",
            "install_hint": "安装指引",
        }
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("skill_webbridge_check", {})
    assert result["ok"] is False
    assert "daemon" in summary


def test_executor_webbridge_extract_requires_selector_or_hint():
    executor = CrawlAgentToolExecutor()
    result, _ = executor.execute("skill_webbridge_extract_list", {})
    assert result["ok"] is False
    assert "selector" in result["error"]


def test_executor_webbridge_navigate_mock():
    mock_nav = {
        "ok": True,
        "url": "https://example.com",
        "title": "Example",
        "session_id": "crawl-agent",
        "snapshot": {"tree": "...", "truncated": False},
    }
    with patch("src.web.crawl_agent_tools.webbridge_navigate", return_value=mock_nav):
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute(
            "skill_webbridge_navigate",
            {"url": "https://example.com"},
        )
    assert result["ok"] is True
    assert "example.com" in summary
