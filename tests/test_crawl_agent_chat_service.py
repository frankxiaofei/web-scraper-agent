"""Crawl Agent 对话服务单元测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.chat_scheduled_tasks import (
    detect_bim_analysis_schedule_ui_intent,
    detect_scheduled_tasks_panel_ui_intent,
)
from src.web.crawl_agent_chat_service import (
    ChatStreamEvent,
    CrawlAgentChatService,
    MAX_AGENT_TURNS,
    _build_partial_summary,
    _collect_tool_stats,
    build_site_register_ui_event,
    build_bim_analysis_schedule_ui_event,
    build_scheduled_tasks_panel_ui_event,
    detect_full_crawl_intent,
    detect_max_iterations_reached,
    detect_onboarding_intent,
    map_hermes_run_event,
    resolve_max_turns_for_message,
)
from src.web.crawl_agent_tools import (
    CrawlAgentToolExecutor,
    build_executed_command,
    format_http_request_command,
    format_tool_command,
    resolve_site_id,
)

TJ_SITE = "中国铁道建筑集团有限公司_物资采购网"


def test_format_tool_command_and_http():
    assert format_tool_command("skill_save_notice", {}) == "skill_save_notice()"
    cmd = format_tool_command(
        "skill_fetch_notice_content",
        {"notice_id": "n1", "detail_url": "https://example.com/a"},
    )
    assert "skill_fetch_notice_content" in cmd
    assert "notice_id=" in cmd
    http_cmd = format_http_request_command(
        "POST",
        "https://bid.example.com/api/list",
        {"page_num": 1, "site_id": "demo"},
    )
    assert http_cmd.startswith("POST https://bid.example.com/api/list")
    assert "page_num" in http_cmd
    assert build_executed_command(
        "skill_webbridge_navigate",
        {"url": "https://example.com", "session_id": "crawl-x"},
    ).startswith("webbridge.navigate")


def test_resolve_site_id_tjbid_alias():
    assert resolve_site_id("帮我把 tjbid 全部爬下来") == TJ_SITE
    assert resolve_site_id("铁建物资采购网") == TJ_SITE


def test_resolve_site_id_unknown():
    with patch("src.web.crawl_agent_tools.get_task_service") as mock_svc:
        mock_svc.return_value.list_sites.return_value = {"sites": []}
        assert resolve_site_id("完全不存在的站点xyz") is None


def test_tool_executor_resolve_site():
    with patch("src.web.crawl_agent_tools.get_site_by_id") as mock_site:
        mock_site.return_value = {"name": "物资采购网", "url": "https://tjbid.dlzb.com/"}
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("crawl_resolve_site", {"query": "tjbid"})
        assert result["ok"] is True
        assert result["site_id"] == TJ_SITE
        assert result["arguments"] == {"query": "tjbid"}
        assert "crawl_resolve_site" in result["executed_command"]
        assert "tjbid" in result["executed_command"]
        assert "tjbid" in summary or TJ_SITE in summary


def test_tool_executor_list_sites_mock():
    with patch("src.web.crawl_agent_tools.get_task_service") as mock_svc:
        mock_svc.return_value.list_sites.return_value = {
            "sites": [
                {"site_id": TJ_SITE, "site_name": "物资采购网", "status": "completed"},
            ]
        }
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("crawl_list_sites", {})
        assert result["ok"] is True
        assert result["count"] == 1
        assert "1 个站点" in summary


def test_tool_poll_until_done_completes_immediately():
    with patch("src.web.crawl_agent_tools.get_sync_service") as mock_sync, patch(
        "src.web.crawl_agent_tools.get_task_service"
    ) as mock_task:
        sync_svc = mock_sync.return_value
        sync_svc.get_sync_status.return_value = {
            "ok": True,
            "is_running": False,
            "status": "completed",
        }
        mock_task.return_value.get_site.return_value = {
            "site": {
                "site_id": TJ_SITE,
                "status": "completed",
                "last_run_status": "success",
                "new_count_display": 5,
            }
        }
        executor = CrawlAgentToolExecutor(sleep_fn=lambda _: None)
        result, summary = executor.execute(
            "crawl_poll_until_done",
            {"site_id": TJ_SITE, "timeout_seconds": 10},
        )
        assert result["ok"] is True
        assert "polls=1" in summary


async def _collect_events(service: CrawlAgentChatService, message: str) -> list[ChatStreamEvent]:
    events: list[ChatStreamEvent] = []
    async for ev in service.stream_chat(message):
        events.append(ev)
    return events


def test_max_agent_turns_default():
    assert MAX_AGENT_TURNS == 100


def test_detect_onboarding_intent():
    assert detect_onboarding_intent("添加站点") is True
    assert detect_onboarding_intent("接入新 URL") is True
    assert detect_onboarding_intent("站点接入") is True
    assert detect_onboarding_intent("爬取 tjbid") is False
    assert detect_onboarding_intent("继续") is False


def test_detect_bim_analysis_schedule_ui_intent():
    assert detect_bim_analysis_schedule_ui_intent("创建 BIM 每日分析通知任务") is True
    assert detect_bim_analysis_schedule_ui_intent("配置 BIM 分析飞书推送") is True
    assert detect_bim_analysis_schedule_ui_intent("爬取 tjbid") is False


def test_build_bim_analysis_schedule_ui_event():
    ev = build_bim_analysis_schedule_ui_event()
    assert ev.type == "ui"
    assert ev.data.get("component") == "bim_analysis_schedule_form"
    assert ev.data.get("submit_action") == "create_bim_analysis_schedule"


def test_detect_scheduled_tasks_panel_ui_intent():
    assert detect_scheduled_tasks_panel_ui_intent("查看通知任务") is True
    assert detect_scheduled_tasks_panel_ui_intent("管理通知任务") is True
    assert detect_scheduled_tasks_panel_ui_intent("爬取 tjbid") is False


def test_build_scheduled_tasks_panel_ui_event():
    ev = build_scheduled_tasks_panel_ui_event()
    assert ev.type == "ui"
    assert ev.data.get("component") == "scheduled_tasks_panel"


@pytest.mark.asyncio
async def test_stream_chat_scheduled_tasks_panel_yields_ui():
    mock_hermes = MagicMock()
    mock_hermes.available = True
    service = CrawlAgentChatService(hermes_client=mock_hermes)
    events = await _collect_events(service, "查看通知任务")
    assert any(e.type == "ui" for e in events)
    ui_ev = next(e for e in events if e.type == "ui")
    assert ui_ev.data.get("component") == "scheduled_tasks_panel"
    assert events[-1].type == "done"
    mock_hermes.start_run.assert_not_called()


@pytest.mark.asyncio
async def test_stream_chat_bim_analysis_ui_yields_form():
    mock_hermes = MagicMock()
    mock_hermes.available = True
    service = CrawlAgentChatService(hermes_client=mock_hermes)
    events = await _collect_events(service, "创建 BIM 每日分析通知任务")
    assert any(e.type == "ui" for e in events)
    ui_ev = next(e for e in events if e.type == "ui")
    assert ui_ev.data.get("component") == "bim_analysis_schedule_form"
    assert events[-1].type == "done"
    mock_hermes.start_run.assert_not_called()


def test_build_site_register_ui_event():
    ev = build_site_register_ui_event()
    assert ev.type == "ui"
    assert ev.data.get("component") == "site_register_form"
    assert len(ev.data.get("fields") or []) >= 2


@pytest.mark.asyncio
async def test_stream_chat_onboarding_yields_ui():
    mock_hermes = MagicMock()
    mock_hermes.available = True

    service = CrawlAgentChatService(hermes_client=mock_hermes)
    events = await _collect_events(service, "添加站点")

    assert any(e.type == "ui" for e in events)
    ui_ev = next(e for e in events if e.type == "ui")
    assert ui_ev.data.get("component") == "site_register_form"
    assert any(e.type == "message" for e in events)
    assert events[-1].type == "done"
    mock_hermes.start_run.assert_not_called()


@pytest.mark.asyncio
async def test_stream_chat_scheduled_feishu_intent_creates_task():
    mock_hermes = MagicMock()
    mock_hermes.available = True

    service = CrawlAgentChatService(hermes_client=mock_hermes)
    with patch(
        "src.web.chat_schedule_service.get_chat_schedule_service"
    ) as mock_svc_factory:
        mock_svc = MagicMock()
        mock_svc.create_from_intent.return_value = {
            "ok": True,
            "task": {
                "id": "task-1",
                "name": "电建 增量+飞书",
                "site_id": "powerchina",
                "cron": "0 8 * * *",
                "report_type": "bim",
                "next_run_time": "2026-06-30T00:00:00+08:00",
            },
        }
        mock_svc_factory.return_value = mock_svc
        events = await _collect_events(
            service,
            "帮我设置每天上午8点爬取电建BIM增量，完成后发飞书报告",
        )

    assert any(e.type == "message" for e in events)
    assert events[-1].type == "done"
    mock_hermes.start_run.assert_not_called()
    mock_svc.create_from_intent.assert_called_once()


@pytest.mark.parametrize(
    "message,expected",
    [
        ("帮我把 tjbid 全部爬下来", True),
        ("全量抓取铁建物资", True),
        ("爬取所有公告", True),
        ("完整同步 500页", True),
        ("目标 10000 条", True),
        ("查一下 tjbid 状态", False),
        ("继续", False),
    ],
)
def test_detect_full_crawl_intent_keywords(message, expected):
    assert detect_full_crawl_intent(message) is expected


def test_detect_full_crawl_intent_site_rule_max_pages():
    with patch(
        "src.web.crawl_agent_chat_service._get_site_effective_max_pages",
        return_value=500,
    ):
        assert detect_full_crawl_intent("爬取 tjbid") is True
    with patch(
        "src.web.crawl_agent_chat_service._get_site_effective_max_pages",
        return_value=50,
    ):
        assert detect_full_crawl_intent("爬取 tjbid") is False


def test_resolve_max_turns_for_message():
    assert resolve_max_turns_for_message("继续", default_max=20, full_max=100) == (20, False)
    assert resolve_max_turns_for_message("全部爬取", default_max=20, full_max=100) == (100, True)


def test_build_partial_summary_full_crawl_heading():
    text = _build_partial_summary([], turn=100, max_turns=100, is_full_crawl=True)
    assert "全量爬取摘要" in text
    assert "全量任务 100 轮" in text
    assert "100/100" in text


def test_collect_tool_stats_from_messages():
    messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "ok": True,
                    "site_id": TJ_SITE,
                    "agent_max_pages": 50,
                    "pagination_strategy": "page_number",
                }
            ),
        },
        {
            "role": "tool",
            "content": json.dumps({"ok": True, "site_id": TJ_SITE, "page_num": 1, "item_count": 20}),
        },
        {
            "role": "tool",
            "content": json.dumps({"ok": True, "saved": True, "site_id": TJ_SITE}),
        },
        {
            "role": "tool",
            "content": json.dumps({"ok": True, "duplicate": True, "site_id": TJ_SITE}),
        },
    ]
    stats = _collect_tool_stats(messages)
    assert stats["site_id"] == TJ_SITE
    assert stats["pages_fetched"] == {1}
    assert stats["saved"] == 1
    assert stats["skipped"] == 1
    assert stats["agent_max_pages"] == 50


def test_build_partial_summary_includes_continue_hint():
    messages = [
        {
            "role": "tool",
            "content": json.dumps({"ok": True, "site_id": TJ_SITE, "page_num": 3}),
        },
        {
            "role": "tool",
            "content": json.dumps({"ok": True, "saved": True}),
        },
    ]
    text = _build_partial_summary(messages, turn=14, max_turns=14)
    assert "部分爬取摘要" in text
    assert TJ_SITE in text
    assert "继续" in text
    assert "14/14" in text


def test_map_hermes_run_event_message_delta():
    events = map_hermes_run_event({"event": "message.delta", "delta": "hello"})
    assert len(events) == 1
    assert events[0].type == "message_delta"
    assert events[0].content == "hello"


def test_map_hermes_run_event_tool_error_false_positive():
    """Hermes 有时对成功 skill 响应误标 error=true。"""
    completed_ev = map_hermes_run_event(
        {
            "event": "tool.completed",
            "tool": "skill_webbridge_check",
            "error": True,
            "duration": 0.03,
            "result": {"ok": True, "available": True},
        }
    )
    assert completed_ev[0].type == "result"
    assert "失败" not in completed_ev[0].summary


def test_map_hermes_run_event_tool_and_completed():
    tool_ev = map_hermes_run_event({"event": "tool.started", "tool": "crawl_list_sites"})
    assert tool_ev[0].type == "tool"
    assert tool_ev[0].name == "crawl_list_sites"
    assert tool_ev[0].content == "crawl_list_sites()"
    assert tool_ev[0].data.get("executed_command") == "crawl_list_sites()"

    tool_args_ev = map_hermes_run_event(
        {
            "event": "tool.started",
            "tool": "skill_fetch_notice_content",
            "arguments": {"notice_id": "abc123", "detail_url": "https://example.com/n/1"},
        }
    )
    assert tool_args_ev[0].args["notice_id"] == "abc123"
    assert "skill_fetch_notice_content" in tool_args_ev[0].content
    assert "notice_id=" in tool_args_ev[0].content

    completed_ev = map_hermes_run_event(
        {
            "event": "tool.completed",
            "tool": "skill_http_fetch_list",
            "error": False,
            "duration": 1.2,
            "arguments": {"site_id": TJ_SITE, "page_num": 2},
            "result": {
                "ok": True,
                "list_url": "https://bid.example.com/api/list",
                "executed_command": "POST https://bid.example.com/api/list {\"page_num\": 2}",
            },
        }
    )
    assert completed_ev[0].type == "result"
    assert completed_ev[0].data.get("executed_command", "").startswith("POST https://")

    done_ev = map_hermes_run_event({"event": "run.completed", "output": "完成"})
    assert done_ev[0].type == "message"
    assert done_ev[0].content == "完成"


def test_detect_max_iterations_reached():
    assert detect_max_iterations_reached({"partial": True}, tool_turns=0, max_iterations=90)
    assert detect_max_iterations_reached(
        {"completed": False}, tool_turns=10, max_iterations=90
    )
    assert detect_max_iterations_reached(
        {"stop_reason": "max_iterations_reached"}, tool_turns=0, max_iterations=90
    )
    assert detect_max_iterations_reached({}, tool_turns=90, max_iterations=90)
    assert not detect_max_iterations_reached({}, tool_turns=12, max_iterations=90)


def test_map_hermes_run_completed_sets_can_continue():
    with patch(
        "src.web.crawl_agent_chat_service.resolve_hermes_max_iterations",
        return_value=90,
    ):
        events = map_hermes_run_event(
            {
                "event": "run.completed",
                "output": "已爬取部分页面",
                "_tool_turns": 90,
            }
        )
    assert len(events) == 1
    assert events[0].type == "message"
    assert events[0].data.get("can_continue") is True
    assert events[0].data.get("turns_used") == 90
    assert events[0].data.get("max_turns") == 90
    assert events[0].data.get("reason") == "max_iterations_reached"


def test_map_hermes_run_completed_no_can_continue_when_under_budget():
    with patch(
        "src.web.crawl_agent_chat_service.resolve_hermes_max_iterations",
        return_value=90,
    ):
        events = map_hermes_run_event(
            {
                "event": "run.completed",
                "output": "完成",
                "_tool_turns": 5,
            }
        )
    assert events[0].data.get("can_continue") is not True


@pytest.mark.asyncio
async def test_stream_chat_yields_hermes_full_crawl_thinking():
    mock_hermes = MagicMock()
    mock_hermes.available = True
    mock_hermes.start_run = AsyncMock(return_value=("run_x", None))

    async def _events(*_a, **_k):
        yield {"event": "run.completed", "output": "ok"}

    mock_hermes.stream_run_events = _events

    service = CrawlAgentChatService(hermes_client=mock_hermes, max_turns=20)
    with patch("src.web.crawl_agent_chat_service.get_settings") as mock_settings:
        mock_settings.return_value.crawl_agent_max_rounds_full = 100
        mock_settings.return_value.hermes_agent_url = "http://hermes:8080"
        mock_settings.return_value.hermes_agent_chat_url = None
        events = await _collect_events(service, "帮我把 tjbid 全部爬下来")

    thinking = [e for e in events if e.type == "thinking"]
    assert any("Hermes Agent" in (e.content or "") for e in thinking)
    assert any(e.data.get("is_full_crawl") is True for e in thinking if e.data)
    assert any(e.type == "message" for e in events)
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_stream_chat_hermes_tool_events():
    mock_hermes = MagicMock()
    mock_hermes.available = True
    mock_hermes.start_run = AsyncMock(return_value=("run_x", None))

    async def _events(*_a, **_k):
        yield {"event": "tool.started", "tool": "crawl_resolve_site", "preview": "resolve"}
        yield {"event": "tool.completed", "tool": "crawl_resolve_site", "error": False, "duration": 0.5}
        yield {"event": "run.completed", "output": "站点已解析"}

    mock_hermes.stream_run_events = _events

    service = CrawlAgentChatService(hermes_client=mock_hermes)
    with patch("src.web.crawl_agent_chat_service.get_settings") as mock_settings:
        mock_settings.return_value.crawl_agent_max_rounds_full = 100
        mock_settings.return_value.hermes_agent_url = "http://hermes:8642"
        mock_settings.return_value.hermes_agent_chat_url = "http://hermes:8642"
        events = await _collect_events(service, "爬取 tjbid")

    assert any(e.type == "tool" and e.name == "crawl_resolve_site" for e in events)
    assert any(e.type == "result" for e in events)
    assert any(e.type == "message" for e in events)


def test_get_service_uses_settings_max_rounds():
    with patch("src.web.crawl_agent_chat_service.get_settings") as mock_settings:
        mock_settings.return_value.crawl_agent_max_rounds = 33
        import src.web.crawl_agent_chat_service as mod

        mod._service = None
        svc = mod.get_crawl_agent_chat_service()
        assert svc.max_turns == 33
        mod._service = None


@pytest.mark.asyncio
async def test_hermes_unavailable_yields_error():
    mock_hermes = MagicMock()
    mock_hermes.available = False

    service = CrawlAgentChatService(hermes_client=mock_hermes)
    events = await _collect_events(service, "hello")

    assert any(e.type == "error" for e in events)
    assert "HERMES_AGENT" in (next(e for e in events if e.type == "error").content or "")
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_hermes_start_error_yields_error_event():
    mock_hermes = MagicMock()
    mock_hermes.available = True
    mock_hermes.start_run = AsyncMock(return_value=(None, "连接失败"))

    service = CrawlAgentChatService(hermes_client=mock_hermes)
    with patch("src.web.crawl_agent_chat_service.get_settings") as mock_settings:
        mock_settings.return_value.crawl_agent_max_rounds_full = 100
        mock_settings.return_value.hermes_agent_url = "http://hermes:8642"
        mock_settings.return_value.hermes_agent_chat_url = "http://hermes:8642"
        events = await _collect_events(service, "hello")

    assert any(e.type == "error" for e in events)
    assert events[-1].type == "done"


def test_chat_stream_event_to_sse_dict():
    ev = ChatStreamEvent(
        type="tool",
        name="crawl_trigger",
        args={"site_id": TJ_SITE},
        summary="ok",
        data={"executed_command": f'crawl_trigger(site_id="{TJ_SITE}")'},
    )
    d = ev.to_sse_dict()
    assert d["type"] == "tool"
    assert d["name"] == "crawl_trigger"
    assert d["args"]["site_id"] == TJ_SITE
    assert "executed_command" in d["data"]
