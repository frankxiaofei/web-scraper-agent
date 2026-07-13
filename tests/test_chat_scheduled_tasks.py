"""对话定时任务（增量爬取 + 飞书 / Hermes + 飞书）单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import chat_scheduled_tasks as cst
from src.core.chat_scheduled_tasks import TASK_TYPE_BIM_DAILY_ANALYSIS, TASK_TYPE_HERMES, TASK_TYPE_HERMES_SUMMARY, TASK_TYPE_INCREMENTAL
from src.core.feishu_webhook import FeishuWebhookClient
from src.web.chat_schedule_service import ChatScheduleService


@pytest.fixture
def tasks_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs_path = tmp_path / "chat_scheduled_tasks.json"
    monkeypatch.setattr(cst, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(cst, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cst, "migrate_system_notification_tasks", lambda jobs: (jobs, False))
    return jobs_path


def test_detect_scheduled_feishu_intent():
    assert cst.detect_scheduled_feishu_intent("帮我设置每天上午8点爬取电建BIM增量，完成后发飞书报告") is True
    assert cst.detect_scheduled_feishu_intent("创建一个通知任务：每天18点增量同步并推送飞书") is True
    assert cst.detect_scheduled_feishu_intent("爬取 tjbid") is False
    assert cst.detect_scheduled_feishu_intent("每天8点") is False


def test_detect_scheduled_hermes_feishu_intent():
    assert cst.detect_scheduled_hermes_feishu_intent(
        "每天上午9点执行 Hermes 任务：统计今日 BIM 公告，完成后推飞书"
    ) is True
    assert cst.detect_scheduled_hermes_feishu_intent(
        "帮我设置每天上午8点爬取电建BIM增量，完成后发飞书报告"
    ) is False
    assert cst.detect_scheduled_hermes_feishu_intent("每天9点") is False


def test_detect_scheduled_bim_analysis_feishu_intent():
    assert cst.detect_scheduled_bim_analysis_feishu_intent(
        "每天上午8点分析今日新增 BIM 招标公告并推飞书"
    ) is True
    assert cst.detect_scheduled_bim_analysis_feishu_intent(
        "帮我设置每天上午8点爬取电建BIM增量，完成后发飞书报告"
    ) is False
    assert cst.detect_scheduled_bim_analysis_feishu_intent("每天8点") is False


def test_detect_bim_analysis_schedule_ui_intent():
    assert cst.detect_bim_analysis_schedule_ui_intent("创建 BIM 每日分析通知任务") is True
    assert cst.detect_bim_analysis_schedule_ui_intent("配置 BIM 分析飞书推送") is True
    assert cst.detect_bim_analysis_schedule_ui_intent("爬取 tjbid") is False


def test_build_bim_analysis_schedule_ui_payload():
    payload = cst.build_bim_analysis_schedule_ui_payload()
    assert payload["component"] == "bim_analysis_schedule_form"
    assert payload["submit_action"] == "create_bim_analysis_schedule"
    field_names = {f["name"] for f in payload["fields"]}
    assert "name" in field_names
    assert "cron_preset" in field_names
    assert "feishu_webhook_url" in field_names


def test_detect_scheduled_tasks_panel_ui_intent():
    assert cst.detect_scheduled_tasks_panel_ui_intent("查看通知任务") is True
    assert cst.detect_scheduled_tasks_panel_ui_intent("管理通知任务") is True
    assert cst.detect_scheduled_tasks_panel_ui_intent("我的通知任务列表") is True
    assert cst.detect_scheduled_tasks_panel_ui_intent("爬取 tjbid") is False


def test_build_scheduled_tasks_panel_ui_payload():
    payload = cst.build_scheduled_tasks_panel_ui_payload()
    assert payload["component"] == "scheduled_tasks_panel"
    assert "incremental_sync" in payload["task_type_labels"]


def test_toggle_chat_scheduled_task(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.get_site_by_id",
        lambda sid: {"id": sid, "name": "演示站"},
    )
    task = cst.create_chat_scheduled_task(site_id="demo_site", cron="0 8 * * *", enabled=True)
    toggled = cst.toggle_chat_scheduled_task(task["id"])
    assert toggled["enabled"] is False
    assert toggled.get("next_run_time") is None
    restored = cst.toggle_chat_scheduled_task(task["id"], enabled=True)
    assert restored["enabled"] is True
    assert restored.get("next_run_time")


def test_load_tasks_default_enabled(tasks_file: Path):
    tasks_file.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "t1",
                        "cron": "0 8 * * *",
                        "task_type": "hermes_prompt",
                        "hermes_prompt": "hi",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    jobs = cst.load_chat_scheduled_tasks()
    assert jobs[0]["enabled"] is True


def test_parse_bim_analysis_schedule_intent():
    parsed = cst.parse_bim_analysis_schedule_intent(
        "每天下午6点分析今日新增 BIM 招标公告，完成后推飞书"
    )
    assert parsed["ok"] is True
    assert parsed["task_type"] == TASK_TYPE_BIM_DAILY_ANALYSIS
    assert parsed["cron"] == "0 18 * * *"


def test_create_bim_analysis_task(tasks_file: Path):
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_BIM_DAILY_ANALYSIS,
        cron="0 8 * * *",
        name="测试 BIM 分析",
        top_n=5,
    )
    assert task["task_type"] == TASK_TYPE_BIM_DAILY_ANALYSIS
    assert task["top_n"] == 5
    assert task.get("next_run_time")


@pytest.mark.asyncio
async def test_run_chat_scheduled_task_async_bim_daily_brief(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    task = cst.create_chat_scheduled_task(
        task_type=cst.TASK_TYPE_BIM_DAILY_BRIEF,
        cron="0 8 * * *",
    )
    result = await cst.run_chat_scheduled_task_async(task["id"], dry_run=True)
    assert result["ok"] is False
    assert result["task_type"] == cst.TASK_TYPE_BIM_DAILY_BRIEF
    assert "废弃" in result.get("message", "") or "废弃" in result.get("error", "")


@pytest.mark.asyncio
async def test_run_chat_scheduled_task_async_bim_analysis(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_BIM_DAILY_ANALYSIS,
        cron="0 8 * * *",
        hermes_prompt=None,
    )
    result = await cst.run_chat_scheduled_task_async(task["id"], dry_run=True)
    assert result["ok"] is False
    assert result["task_type"] == TASK_TYPE_BIM_DAILY_ANALYSIS
    assert "废弃" in result.get("message", "") or "废弃" in result.get("error", "")


def test_crawl_schedule_bim_analysis_feishu_tool(tasks_file: Path):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "crawl_schedule_bim_analysis_feishu",
        {
            "action": "create",
            "cron": "0 8 * * *",
            "name": "BIM 每日分析",
        },
    )
    assert result["ok"] is True
    assert result["task"]["task_type"] == TASK_TYPE_BIM_DAILY_ANALYSIS
    assert "8" in result["task"]["cron"] or "8" in summary


def test_mask_webhook_url():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/abcd1234-5678-90ef-ghij-klmnopqrstuv"
    masked = cst.mask_webhook_url(url)
    assert "abcd" in masked
    assert "***" in masked
    assert "stuv" in masked
    assert masked != url


def test_extract_feishu_webhook_from_text():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id"
    text = f"每天8点推飞书 webhook 为 {url} secret abcDEF123456"
    parsed_url, secret = cst.extract_feishu_webhook_from_text(text)
    assert parsed_url == url
    assert secret == "abcDEF123456"


def test_resolve_task_feishu_client_prefers_task_webhook(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/global")
    task = {"feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/task-level"}
    client = cst.resolve_task_feishu_client(task)
    assert client.webhook_url == task["feishu_webhook_url"]


def test_resolve_task_feishu_client_fallback_global(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/global")
    client = cst.resolve_task_feishu_client({})
    assert client.webhook_url == "https://open.feishu.cn/open-apis/bot/v2/hook/global"


def test_detect_scheduled_feishu_intent():
    assert cst.detect_scheduled_feishu_intent("帮我设置每天上午8点爬取电建BIM增量，完成后发飞书报告") is True
    assert cst.detect_scheduled_feishu_intent("创建一个通知任务：每天18点增量同步并推送飞书") is True
    assert cst.detect_scheduled_feishu_intent("爬取 tjbid") is False
    assert cst.detect_scheduled_feishu_intent("每天8点") is False


def test_normalize_cron_am_pm():
    assert cst._normalize_cron_with_extras("上午8点") == "0 8 * * *"
    assert cst._normalize_cron_with_extras("下午6点") == "0 18 * * *"


def test_describe_cron_expression():
    from src.core.script_cron import describe_cron_expression

    assert describe_cron_expression("0 8 * * *") == "每天 08:00"
    assert describe_cron_expression("0 9 * * 1") == "每周一 09:00"
    assert describe_cron_expression("0 8 * * 5") == "每周五 08:00"


def test_build_cron_trigger_for_task():
    from apscheduler.triggers.cron import CronTrigger

    trigger = cst.build_cron_trigger_for_task("0 8 * * *")
    assert isinstance(trigger, CronTrigger)


def test_parse_schedule_intent_powerchina(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.web.crawl_agent_tools.resolve_site_id",
        lambda q: "中国电力建设集团有限公司_公共资源交易服务平台" if "电建" in q else None,
    )
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.get_site_by_id",
        lambda sid: {"id": sid, "name": "电建招采"},
    )
    parsed = cst.parse_schedule_intent("每天上午8点爬取电建BIM增量，完成后发飞书报告")
    assert parsed["ok"] is True
    assert parsed["cron"] == "0 8 * * *"
    assert parsed["report_type"] == "bim"
    assert "电建" in parsed["site_name"] or "电力" in parsed["site_id"]


def test_parse_schedule_intent_with_webhook():
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc-def-ghi"
    with patch("src.web.crawl_agent_tools.resolve_site_id", return_value="demo_site"), patch(
        "src.core.chat_scheduled_tasks.get_site_by_id",
        return_value={"id": "demo_site", "name": "演示站"},
    ):
        parsed = cst.parse_schedule_intent(
            f"每天上午8点爬取演示站增量并推飞书 webhook {url}"
        )
    assert parsed["ok"] is True
    assert parsed["feishu_webhook_url"] == url


def test_parse_hermes_schedule_intent():
    parsed = cst.parse_hermes_schedule_intent(
        "每天上午9点执行 Hermes 任务：统计今日 BIM 公告数量，完成后推飞书"
    )
    assert parsed["ok"] is True
    assert parsed["task_type"] == TASK_TYPE_HERMES_SUMMARY
    assert parsed["cron"] == "0 9 * * *"
    assert "BIM" in parsed["hermes_prompt"]


def test_create_hermes_task_list_masks_webhook(tasks_file: Path):
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/abcd1234-5678-90ef-ghij-klmnopqrstuv"
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_HERMES,
        cron="0 9 * * *",
        hermes_prompt="统计今日公告",
        feishu_webhook_url=url,
        feishu_webhook_secret="secret123",
    )
    assert task["task_type"] == TASK_TYPE_HERMES
    assert task["feishu_webhook_url"] != url
    assert task["feishu_webhook_secret"] == "***"

    stored = cst.get_chat_scheduled_task(task["id"])
    assert stored is not None
    assert stored["feishu_webhook_url"] == url
    assert stored["feishu_webhook_secret"] == "secret123"


def test_build_hermes_run_feishu_card():
    card = cst.build_hermes_run_feishu_card(
        task_name="测试 Hermes 任务",
        run_result={
            "ok": True,
            "success": True,
            "output": "今日 BIM 公告 12 条",
            "tool_count": 3,
            "run_id": "run_test",
        },
    )
    body = json.dumps(card, ensure_ascii=False)
    assert "测试 Hermes 任务" in body
    assert "12 条" in body
    assert "3 次" in body


def test_push_hermes_task_feishu_report_mocked(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_HERMES,
        cron="0 9 * * *",
        hermes_prompt="测试",
    )
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.push_task_feishu_card",
        lambda *a, **k: {"ok": True, "pushed_count": 1},
    )
    result = cst.push_hermes_task_feishu_report(
        cst.get_chat_scheduled_task(task["id"]) or task,
        {"success": True, "output": "完成", "tool_count": 1},
    )
    assert result.get("ok") is True


def test_create_list_delete_task(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    site_id = "demo_site"
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.get_site_by_id",
        lambda sid: {"id": sid, "name": "演示站"} if sid == site_id else None,
    )

    task = cst.create_chat_scheduled_task(
        site_id=site_id,
        cron="每天 9 点",
        report_type="site",
    )
    assert task["cron"] == "0 9 * * *"
    assert task.get("next_run_time")

    tasks = cst.list_chat_scheduled_tasks()
    assert len(tasks) == 1
    assert tasks_file.exists()

    deleted = cst.delete_chat_scheduled_task(task["id"])
    assert deleted["ok"] is True
    assert cst.list_chat_scheduled_tasks() == []


def test_build_site_sync_feishu_card():
    card = cst.build_site_sync_feishu_card(
        site_id="demo",
        site_name="演示站",
        sync_result={"success": True, "new_count": 3},
        top_items=[{"title": "测试公告", "url": "https://example.com/a"}],
    )
    body = json.dumps(card, ensure_ascii=False)
    assert "演示站" in body
    assert "测试公告" in body


def test_push_task_feishu_report_skipped_without_webhook(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_RECEIVE_USERS", raising=False)
    monkeypatch.delenv("FEISHU_RECEIVE_USER", raising=False)
    monkeypatch.setattr("src.core.chat_scheduled_tasks.get_site_by_id", lambda sid: {"id": sid, "name": "演示站"})
    monkeypatch.setattr("src.core.chat_scheduled_tasks.resolve_env_feishu_webhooks", lambda: [])
    monkeypatch.setattr("src.core.chat_scheduled_tasks.resolve_env_feishu_receive_user_ids", lambda: [])
    task = cst.create_chat_scheduled_task(site_id="demo_site", cron="0 8 * * *")
    result = cst.push_task_feishu_report(task, {"success": True, "new_count": 1})
    assert result.get("skipped") is True


def test_push_task_feishu_report_bim_mocked(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.core.chat_scheduled_tasks.get_site_by_id", lambda sid: {"id": sid, "name": "电建"})
    task = cst.create_chat_scheduled_task(
        site_id="demo_site",
        cron="0 8 * * *",
        report_type="bim",
    )
    with patch("src.web.agri_insights_service.get_agri_insights_service") as mock_svc, patch(
        "src.core.chat_scheduled_tasks.push_task_feishu_card",
        return_value={"ok": True, "pushed_count": 1},
    ) as mock_push:
        mock_svc.return_value.get_summary.return_value = {
            "agri_count_in_window": 2,
            "hot_keywords": [{"keyword": "智慧农业", "count": 1}],
        }
        result = cst.push_task_feishu_report(task, {"success": True})
    assert result.get("ok") is True
    assert result.get("report_type") == "agri"
    mock_push.assert_called_once()


@pytest.mark.asyncio
async def test_run_hermes_scheduled_prompt_dry_run():
    result = await cst.run_hermes_scheduled_prompt("测试 {date}", task_id="t1", dry_run=True)
    assert result["ok"] is True
    assert result["success"] is True
    assert result.get("rendered_prompt")


@pytest.mark.asyncio
async def test_run_hermes_scheduled_prompt_mocked():
    mock_client = MagicMock()
    mock_client.available = True
    mock_client.start_run = AsyncMock(return_value=("run_1", None))

    async def _events(_run_id):
        yield {"event": "tool.completed", "tool": "crawl_list_sites"}
        yield {"event": "run.completed", "output": "共 5 条公告"}

    mock_client.stream_run_events = _events

    with patch("src.core.hermes_agent_chat_client.get_hermes_agent_chat_client", return_value=mock_client):
        result = await cst.run_hermes_scheduled_prompt("列出站点", task_id="t2")

    assert result["success"] is True
    assert result["output"] == "共 5 条公告"
    assert result["tool_count"] == 1


@pytest.mark.asyncio
async def test_run_chat_scheduled_task_async_hermes(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_HERMES,
        cron="0 9 * * *",
        hermes_prompt="统计公告",
    )
    mock_run = AsyncMock(
        return_value={"ok": True, "success": True, "output": "ok", "tool_count": 0}
    )
    monkeypatch.setattr(cst, "run_hermes_scheduled_prompt", mock_run)
    with patch.object(cst, "push_hermes_task_feishu_report", return_value={"ok": True, "skipped": True}):
        result = await cst.run_chat_scheduled_task_async(task["id"], dry_run=True)
    assert result["ok"] is True
    assert result["task_type"] == TASK_TYPE_HERMES
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_chat_scheduled_task_async(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    site_id = "demo_site"
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.get_site_by_id",
        lambda sid: {"id": sid, "name": "演示站"} if sid == site_id else None,
    )
    task = cst.create_chat_scheduled_task(site_id=site_id, cron="0 8 * * *")

    mock_sync = AsyncMock(return_value={"success": True, "new_count": 5, "ok": True})
    monkeypatch.setattr("src.core.hermes_agent_runner.run_scheduler_site_sync", mock_sync)

    with patch.object(cst, "push_task_feishu_report", return_value={"ok": True, "skipped": True}):
        result = await cst.run_chat_scheduled_task_async(task["id"], dry_run=True)

    assert result["ok"] is True
    mock_sync.assert_awaited_once()
    assert "新增 5 条" in result.get("message", "")


def test_chat_schedule_service_create_from_intent(monkeypatch: pytest.MonkeyPatch):
    svc = ChatScheduleService()
    monkeypatch.setattr(
        "src.web.chat_schedule_service.parse_schedule_intent",
        lambda msg: {
            "ok": True,
            "site_id": "demo_site",
            "site_name": "演示站",
            "cron": "0 8 * * *",
            "report_type": "site",
            "feishu_push": True,
            "max_items": 10,
            "name": "演示站 增量+飞书",
        },
    )
    monkeypatch.setattr("src.core.chat_scheduled_tasks.get_site_by_id", lambda sid: {"id": sid, "name": "演示站"})

    with patch.object(cst, "save_chat_scheduled_tasks"):
        with patch.object(cst, "load_chat_scheduled_tasks", return_value=[]):
            result = svc.create_from_intent("每天8点爬取演示站增量并推飞书")
    assert result["ok"] is True
    assert result["task"]["cron"] == "0 8 * * *"


def test_crawl_schedule_incremental_feishu_tool(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    monkeypatch.setattr("src.core.chat_scheduled_tasks.get_site_by_id", lambda sid: {"id": sid, "name": "演示站"})
    monkeypatch.setattr("src.web.crawl_agent_tools.resolve_site_id", lambda q: q if q == "demo_site" else None)
    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "crawl_schedule_incremental_feishu",
        {
            "action": "create",
            "site_id": "demo_site",
            "cron": "0 18 * * *",
            "report_type": "site",
        },
    )
    assert result["ok"] is True
    assert "18" in summary or result["task"]["cron"] == "0 18 * * *"


def test_crawl_schedule_hermes_feishu_tool(tasks_file: Path):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    executor = CrawlAgentToolExecutor()
    result, summary = executor.execute(
        "crawl_schedule_hermes_feishu",
        {
            "action": "create",
            "cron": "0 9 * * *",
            "hermes_prompt": "统计今日 BIM 公告",
        },
    )
    assert result["ok"] is True
    assert result["task"]["task_type"] in (TASK_TYPE_HERMES, TASK_TYPE_HERMES_SUMMARY)
    assert "9" in result["task"]["cron"] or "9" in summary


def test_render_prompt_template_replaces_placeholders():
    rendered = cst.render_prompt_template(
        "今日 {date}，站点：{site_list}",
        context={"date": "2026-07-07", "site_list": "电建、云筑"},
    )
    assert "2026-07-07" in rendered
    assert "电建" in rendered


def test_extract_hermes_push_content_prefers_summary_section():
    output = "完整分析内容\n\n## 飞书摘要\n今日 BIM 12 条，重点关注 3 条。"
    excerpt = cst.extract_hermes_push_content(output, push_mode=cst.PUSH_MODE_EXCERPT, excerpt_max=500)
    assert "12 条" in excerpt
    assert "完整分析" not in excerpt


def test_create_hermes_summary_task(tasks_file: Path):
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_HERMES_SUMMARY,
        cron="0 9 * * *",
        name="测试总结",
        hermes_prompt="分析 {date} BIM 公告",
        push_mode=cst.PUSH_MODE_EXCERPT,
        push_excerpt_max=800,
    )
    assert task["task_type"] == TASK_TYPE_HERMES_SUMMARY
    assert task["push_mode"] == cst.PUSH_MODE_EXCERPT
    assert task["push_excerpt_max"] == 800


@pytest.mark.asyncio
async def test_run_chat_scheduled_task_async_hermes_summary(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    task = cst.create_chat_scheduled_task(
        task_type=TASK_TYPE_HERMES_SUMMARY,
        cron="0 9 * * *",
        hermes_prompt="统计公告",
    )
    mock_run = AsyncMock(
        return_value={"ok": True, "success": True, "output": "## 飞书摘要\nok", "tool_count": 0}
    )
    monkeypatch.setattr(cst, "run_hermes_scheduled_prompt", mock_run)
    with patch.object(cst, "push_hermes_task_feishu_report", return_value={"ok": True, "skipped": True}):
        result = await cst.run_chat_scheduled_task_async(task["id"], dry_run=True)
    assert result["ok"] is True
    assert result["task_type"] == TASK_TYPE_HERMES_SUMMARY
    mock_run.assert_awaited_once()


def test_push_task_feishu_report_uses_task_webhook(tasks_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.core.chat_scheduled_tasks.get_site_by_id", lambda sid: {"id": sid, "name": "演示站"})
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/task-only"
    task = cst.create_chat_scheduled_task(
        site_id="demo_site",
        cron="0 8 * * *",
        feishu_webhook_url=url,
    )
    raw_task = cst.get_chat_scheduled_task(task["id"]) or {}
    with patch.object(
        cst,
        "push_task_feishu_card",
        return_value={"ok": True, "pushed_count": 1},
    ) as mock_push:
        cst.push_task_feishu_report(raw_task, {"success": True, "new_count": 0})
    mock_push.assert_called_once()


def test_parse_feishu_webhooks():
    url1 = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123456789"
    url2 = "https://open.feishu.cn/open-apis/bot/v2/hook/def987654321"
    parsed = cst.parse_feishu_webhooks([url1, {"url": url2, "name": "群B"}])
    assert len(parsed) == 2
    assert parsed[0]["url"] == url1
    assert parsed[1]["name"] == "群B"


def test_parse_feishu_user_ids():
    assert cst.parse_feishu_user_ids("yuan_jp, li_xf10") == ["yuan_jp", "li_xf10"]
    assert cst.parse_feishu_user_ids("yuan_jp\nli_xf10") == ["yuan_jp", "li_xf10"]
    assert cst.parse_feishu_user_ids(["a_b", "c_d"]) == ["a_b", "c_d"]
    with pytest.raises(ValueError, match="无效"):
        cst.parse_feishu_user_ids("bad user!")


def test_create_engineering_llm_report_task(tasks_file: Path):
    task = cst.create_chat_scheduled_task(
        task_type=cst.TASK_TYPE_ENGINEERING_LLM_REPORT,
        cron="0 8 * * *",
        name="工程大模型日报",
        feishu_user_ids="yuan_jp, li_xf10",
        days=1,
        top_n=5,
    )
    assert task["task_type"] == cst.TASK_TYPE_ENGINEERING_LLM_REPORT
    assert task["feishu_user_ids"] == ["yuan_jp", "li_xf10"]
    assert task["push_target"] == cst.PUSH_TARGET_INDIVIDUAL
    assert task["report_kind"] == cst.REPORT_KIND_ENGINEERING_LLM
    assert task["days"] == 1
    assert task["top_n"] == 5


@pytest.mark.asyncio
async def test_run_chat_scheduled_task_async_engineering_llm_report(
    tasks_file: Path, monkeypatch: pytest.MonkeyPatch
):
    task = cst.create_chat_scheduled_task(
        task_type=cst.TASK_TYPE_ENGINEERING_LLM_REPORT,
        cron="0 8 * * *",
        feishu_user_ids=["li_xf10"],
    )
    mock_report = {
        "ok": True,
        "skipped": True,
        "dry_run": True,
        "brief": {"count": 3},
        "pushed_count": 0,
    }
    monkeypatch.setattr(
        "src.core.engineering_llm_report_service.push_engineering_llm_report",
        lambda **kwargs: mock_report,
    )
    result = await cst.run_chat_scheduled_task_async(task["id"], dry_run=True)
    assert result["ok"] is True
    assert result["task_type"] == cst.TASK_TYPE_ENGINEERING_LLM_REPORT
    assert "3 条" in result.get("message", "")


def test_migrate_consolidates_legacy_bim_daily_to_stable_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    jobs_path = tmp_path / "chat_scheduled_tasks.json"
    monkeypatch.setattr(cst, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(cst, "DATA_DIR", tmp_path)
    legacy_id = "0276af4d-792c-4e57-991f-4c65f9d38265"
    jobs_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": legacy_id,
                        "task_type": "bim_daily_brief",
                        "cron": "0 8 * * *",
                        "name": "测试 BIM 日报推送",
                        "enabled": True,
                        "feishu_push": True,
                        "top_n": 5,
                    },
                    {
                        "id": "bim_weekly_brief_feishu",
                        "task_type": "bim_weekly_brief",
                        "cron": "0 8 * * 5",
                        "name": "BIM 周报",
                        "enabled": True,
                        "feishu_push": True,
                        "top_n": 10,
                        "days": 30,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    jobs = cst.load_chat_scheduled_tasks()
    daily = next(j for j in jobs if j.get("task_type") == "bim_daily_brief")
    assert daily["id"] == "bim_daily_brief_feishu"
    assert daily["name"] == "测试 BIM 日报推送"
    assert all(j.get("id") != legacy_id for j in jobs)


def test_list_system_notification_tasks_always_includes_system_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    jobs_path = tmp_path / "chat_scheduled_tasks.json"
    monkeypatch.setattr(cst, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(cst, "DATA_DIR", tmp_path)
    monkeypatch.delenv("BIM_WEEKLY_BRIEF_CRON", raising=False)
    monkeypatch.delenv("FEISHU_RECEIVE_USERS", raising=False)
    jobs_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "bim_daily_brief_feishu",
                        "task_type": "bim_daily_brief",
                        "cron": "0 8 * * 0-4",
                        "name": "BIM 日报",
                        "enabled": True,
                        "feishu_push": True,
                        "top_n": 10,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    chat_tasks = cst.list_chat_scheduled_tasks()
    system_tasks = cst.list_system_notification_tasks(chat_tasks=chat_tasks)
    ids = {t["id"] for t in system_tasks}
    assert "bim_daily_brief_feishu" in ids
    assert "bim_weekly_brief_feishu" in ids
    daily = next(t for t in system_tasks if t["id"] == "bim_daily_brief_feishu")
    assert daily.get("source") == "system"
    assert daily.get("read_only") is True
    assert daily.get("has_editable_task") is True
    assert daily.get("config_matches_editable") is True


def test_migrate_syncs_bim_cron_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    jobs_path = tmp_path / "chat_scheduled_tasks.json"
    monkeypatch.setattr(cst, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(cst, "DATA_DIR", tmp_path)
    monkeypatch.setenv("BIM_BRIEF_CRON", "0 8 * * 0-4")
    monkeypatch.setenv("BIM_WEEKLY_BRIEF_CRON", "0 18 * * 4")
    jobs_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "bim_daily_brief_feishu",
                        "task_type": "bim_daily_brief",
                        "cron": "0 8 * * *",
                        "enabled": True,
                        "feishu_push": True,
                        "top_n": 10,
                    },
                    {
                        "id": "bim_weekly_brief_feishu",
                        "task_type": "bim_weekly_brief",
                        "cron": "0 8 * * 5",
                        "enabled": True,
                        "feishu_push": True,
                        "top_n": 10,
                        "days": 30,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    jobs = cst.load_chat_scheduled_tasks()
    daily = next(j for j in jobs if j["id"] == "bim_daily_brief_feishu")
    weekly = next(j for j in jobs if j["id"] == "bim_weekly_brief_feishu")
    assert daily["cron"] == "0 8 * * 0-4"
    assert weekly["cron"] == "0 18 * * 4"


def test_bim_cron_next_run_weekday_only():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.core.script_cron import compute_next_run_time

    friday = datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert compute_next_run_time("0 18 * * 4", after=friday).isoformat() == "2026-07-10T18:00:00+08:00"
    assert compute_next_run_time("0 8 * * 0-4", after=friday).isoformat() == "2026-07-13T08:00:00+08:00"
    saturday = datetime(2026, 7, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert compute_next_run_time("0 8 * * 0-4", after=saturday).isoformat() == "2026-07-13T08:00:00+08:00"
