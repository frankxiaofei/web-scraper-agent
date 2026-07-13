"""定时任务 REST API 单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.core import chat_scheduled_tasks as cst

    jobs_path = tmp_path / "chat_scheduled_tasks.json"
    monkeypatch.setattr(cst, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(cst, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cst, "migrate_system_notification_tasks", lambda jobs: (jobs, False))
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.get_site_by_id",
        lambda sid: {"id": sid, "name": "演示站"},
    )

    from src.web.app import app

    return TestClient(app)


def test_api_list_and_toggle_scheduled_tasks(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    task = cst.create_chat_scheduled_task(site_id="demo_site", cron="0 8 * * *", enabled=True)

    list_resp = client.get("/api/chat/scheduled-tasks")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["tasks"][0]["id"] == task["id"]
    assert "***" in (body["tasks"][0].get("feishu_webhook_url") or "") or body["tasks"][0].get("feishu_webhook_url") is None

    toggle_resp = client.post(f"/api/chat/scheduled-tasks/{task['id']}/toggle")
    assert toggle_resp.status_code == 200
    toggled = toggle_resp.json()
    assert toggled["ok"] is True
    assert toggled["task"]["enabled"] is False

    patch_resp = client.patch(
        f"/api/chat/scheduled-tasks/{task['id']}",
        json={"enabled": True, "name": "重命名任务"},
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()
    assert updated["task"]["enabled"] is True
    assert updated["task"]["name"] == "重命名任务"

    delete_resp = client.delete(f"/api/chat/scheduled-tasks/{task['id']}")
    assert delete_resp.status_code == 200
    assert client.get("/api/chat/scheduled-tasks").json()["count"] == 0


def test_api_create_bim_brief_and_run(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from src.core import chat_scheduled_tasks as cst

    create_resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "bim_daily_brief",
            "name": "测试 BIM 日报",
            "cron": "0 8 * * *",
            "top_n": 8,
            "enabled": True,
        },
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    assert body["ok"] is True
    task_id = body["task"]["id"]
    assert body["task"]["task_type"] == cst.TASK_TYPE_BIM_DAILY_BRIEF

    run_resp = client.post(
        f"/api/chat/scheduled-tasks/{task_id}/run",
        json={"dry_run": True},
    )
    assert run_resp.status_code in (200, 409)
    if run_resp.status_code == 200:
        run_body = run_resp.json()
        assert run_body["ok"] is False
        assert "废弃" in (run_body.get("message") or run_body.get("error") or "")


def test_api_scheduled_tasks_meta(client: TestClient):
    resp = client.get("/api/chat/scheduled-tasks/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body.get("primary_task_type") == "hermes_summary"
    assert any(t["value"] == "hermes_summary" for t in body["task_types"])
    assert body.get("prompt_presets")


def test_api_create_hermes_summary_and_dry_run(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    mock_run = AsyncMock(
        return_value={"ok": True, "success": True, "output": "摘要 ok", "tool_count": 0}
    )
    monkeypatch.setattr("src.core.chat_scheduled_tasks.run_hermes_scheduled_prompt", mock_run)
    monkeypatch.setattr(
        "src.core.chat_scheduled_tasks.push_hermes_task_feishu_report",
        lambda *a, **k: {"ok": True, "skipped": True},
    )

    create_resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "hermes_summary",
            "name": "测试 Hermes 总结",
            "cron": "0 9 * * *",
            "hermes_prompt": "统计今日 BIM 公告，输出 ## 飞书摘要",
            "push_mode": "excerpt",
            "push_excerpt_max": 1200,
            "enabled": True,
        },
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    assert body["ok"] is True
    assert body["task"]["task_type"] == "hermes_summary"
    task_id = body["task"]["id"]

    run_resp = client.post(
        f"/api/chat/scheduled-tasks/{task_id}/run",
        json={"dry_run": True},
    )
    assert run_resp.status_code == 200
    assert run_resp.json()["ok"] is True


def test_api_create_engineering_llm_report_and_run(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from src.core import chat_scheduled_tasks as cst

    mock_report = {
        "ok": True,
        "skipped": True,
        "dry_run": True,
        "brief": {"count": 4},
        "pushed_count": 0,
    }
    monkeypatch.setattr(
        "src.core.engineering_llm_report_service.push_engineering_llm_report",
        lambda **kwargs: mock_report,
    )

    create_resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "engineering_llm_report",
            "name": "工程大模型应用日报",
            "cron": "0 8 * * *",
            "feishu_user_ids": "yuan_jp, li_xf10",
            "days": 1,
            "top_n": 8,
            "enabled": True,
        },
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    assert body["ok"] is True
    task_id = body["task"]["id"]
    assert body["task"]["task_type"] == cst.TASK_TYPE_ENGINEERING_LLM_REPORT
    assert body["task"]["feishu_user_ids"] == ["yuan_jp", "li_xf10"]

    run_resp = client.post(
        f"/api/chat/scheduled-tasks/{task_id}/run",
        json={"dry_run": True},
    )
    assert run_resp.status_code == 200
    run_body = run_resp.json()
    assert run_body["ok"] is True
    assert "4 条" in run_body.get("message", "")


def test_scheduled_tasks_page(client: TestClient):
    resp = client.get("/scheduled-tasks")
    assert resp.status_code == 200
    assert "通知任务" in resp.text
    assert "新建通知任务" in resp.text
    assert "create-task-modal" in resp.text
    assert "open-create-modal-btn" in resp.text
    assert "提示词" in resp.text
    assert "工程大模型" in resp.text
    assert "编辑" in resp.text
    assert "edit-task-modal" in resp.text
    assert "执行时间（Cron）" in resp.text
    assert 'id="cron-expression"' in resp.text
    assert 'id="edit-cron-expression"' in resp.text
    assert "cron-preset-buttons" in resp.text
    assert "通知渠道（多选）" in resp.text
    assert "create-channel-config-panel" in resp.text
    assert "create-cfg-feishu" in resp.text
    assert "channel_config" in resp.text
    assert "系统内置任务（只读）" not in resp.text
    assert "renderSystemTaskRow" not in resp.text
    assert "system_tasks" not in resp.text


def test_api_list_excludes_system_tasks(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEISHU_RECEIVE_USERS", raising=False)
    resp = client.get("/api/chat/scheduled-tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "system_tasks" not in body
    assert "system_count" not in body
    assert "tasks" in body


def test_api_get_scheduled_task(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    task = cst.create_chat_scheduled_task(
        task_type="hermes_summary",
        cron="0 9 * * *",
        name="GET 测试",
        hermes_prompt="测试提示词",
        enabled=True,
    )
    resp = client.get(f"/api/chat/scheduled-tasks/{task['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["task"]["id"] == task["id"]
    assert body["task"]["hermes_prompt"] == "测试提示词"

    missing = client.get("/api/chat/scheduled-tasks/nonexistent-id")
    assert missing.status_code == 404


def test_api_patch_hermes_summary_task(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    task = cst.create_chat_scheduled_task(
        task_type="hermes_summary",
        cron="0 9 * * *",
        name="Hermes 原始",
        hermes_prompt="原始提示词",
        push_mode="excerpt",
        push_excerpt_max=1200,
        enabled=True,
    )
    resp = client.patch(
        f"/api/chat/scheduled-tasks/{task['id']}",
        json={
            "name": "Hermes 已更新",
            "cron_expression": "0 10 * * *",
            "hermes_prompt": "更新后的提示词",
            "push_mode": "full",
            "push_excerpt_max": 2000,
            "enabled": False,
        },
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["ok"] is True
    assert updated["task"]["name"] == "Hermes 已更新"
    assert updated["task"]["cron"] == "0 10 * * *"
    assert updated["task"]["cron_description"] == "每天 10:00"
    assert updated["task"]["hermes_prompt"] == "更新后的提示词"
    assert updated["task"]["push_mode"] == "full"
    assert updated["task"]["push_excerpt_max"] == 2000
    assert updated["task"]["enabled"] is False


def test_api_create_with_cron_expression_alias(client: TestClient):
    create_resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "hermes_summary",
            "name": "Cron 表达式别名测试",
            "cron_expression": "0 9 * * 1",
            "hermes_prompt": "每周一执行",
            "enabled": True,
        },
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    assert body["ok"] is True
    assert body["task"]["cron"] == "0 9 * * 1"
    assert body["task"]["cron_description"] == "每周一 09:00"


def test_api_invalid_cron_returns_400(client: TestClient):
    resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "hermes_summary",
            "name": "无效 Cron",
            "cron_expression": "not-a-cron",
            "hermes_prompt": "测试",
            "enabled": True,
        },
    )
    assert resp.status_code == 400


def test_api_patch_engineering_llm_report_task(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    task = cst.create_chat_scheduled_task(
        task_type="engineering_llm_report",
        cron="0 8 * * *",
        name="工程报告",
        feishu_user_ids="yuan_jp",
        days=1,
        top_n=10,
        enabled=True,
    )
    resp = client.patch(
        f"/api/chat/scheduled-tasks/{task['id']}",
        json={
            "feishu_user_ids": "yuan_jp, li_xf10",
            "days": 3,
            "top_n": 15,
            "cron": "0 18 * * *",
        },
    )
    assert resp.status_code == 200
    updated = resp.json()["task"]
    assert updated["feishu_user_ids"] == ["yuan_jp", "li_xf10"]
    assert updated["days"] == 3
    assert updated["top_n"] == 15
    assert updated["cron"] == "0 18 * * *"


def test_api_patch_bim_brief_task(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    task = cst.create_chat_scheduled_task(
        task_type="bim_weekly_brief",
        cron="0 8 * * 5",
        name="BIM 周报",
        top_n=10,
        enabled=True,
    )
    resp = client.patch(
        f"/api/chat/scheduled-tasks/{task['id']}",
        json={"name": "BIM 周报（改）", "top_n": 20},
    )
    assert resp.status_code == 200
    updated = resp.json()["task"]
    assert updated["name"] == "BIM 周报（改）"
    assert updated["top_n"] == 20


def test_api_patch_validation_errors(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    hermes = cst.create_chat_scheduled_task(
        task_type="hermes_summary",
        cron="0 9 * * *",
        name="校验测试",
        hermes_prompt="有效提示词",
        enabled=True,
    )
    empty_prompt = client.patch(
        f"/api/chat/scheduled-tasks/{hermes['id']}",
        json={"hermes_prompt": "   "},
    )
    assert empty_prompt.status_code == 400

    engineering = cst.create_chat_scheduled_task(
        task_type="engineering_llm_report",
        cron="0 8 * * *",
        name="工程",
        feishu_user_ids="yuan_jp",
        enabled=True,
    )
    empty_users = client.patch(
        f"/api/chat/scheduled-tasks/{engineering['id']}",
        json={"feishu_user_ids": ""},
    )
    assert empty_users.status_code == 400

    empty_body = client.patch(f"/api/chat/scheduled-tasks/{hermes['id']}", json={})
    assert empty_body.status_code == 400

    missing = client.patch("/api/chat/scheduled-tasks/missing-id", json={"name": "x"})
    assert missing.status_code == 404


def test_api_migrate_backfills_push_channels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.core import chat_scheduled_tasks as cst

    jobs_path = tmp_path / "chat_scheduled_tasks.json"
    monkeypatch.setattr(cst, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(cst, "DATA_DIR", tmp_path)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/testhook12")
    monkeypatch.setenv("FEISHU_RECEIVE_USERS", "yuan_jp,li_xf10")

    cst.create_chat_scheduled_task(
        task_type="bim_daily_brief",
        cron="0 8 * * *",
        name="BIM 日报",
        enabled=True,
    )
    jobs = cst.load_chat_scheduled_tasks(migrate=True)
    task = next(j for j in jobs if j.get("task_type") == "bim_daily_brief")
    assert task.get("feishu_webhooks")
    assert task.get("feishu_user_ids") == ["yuan_jp", "li_xf10"]


def test_api_create_with_dual_push_channels(client: TestClient):
    from src.core import chat_scheduled_tasks as cst

    webhook1 = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123456789"
    webhook2 = "https://open.feishu.cn/open-apis/bot/v2/hook/def987654321"
    create_resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "bim_daily_brief",
            "name": "双通道 BIM 日报",
            "cron": "0 8 * * *",
            "feishu_webhooks": [webhook1, webhook2],
            "feishu_user_ids": "yuan_jp, li_xf10",
            "enabled": True,
        },
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    task = body["task"]
    assert len(task.get("feishu_webhooks") or []) == 2
    assert task.get("feishu_user_ids") == ["yuan_jp", "li_xf10"]
    assert task.get("push_channels", {}).get("webhook_count") == 2
    assert task.get("push_channels", {}).get("user_count") == 2

    stored = cst.get_chat_scheduled_task(task["id"])
    assert len(stored.get("feishu_webhooks") or []) == 2


def test_api_create_with_notification_channels(client: TestClient):
    create_resp = client.post(
        "/api/chat/scheduled-tasks",
        json={
            "task_type": "hermes_summary",
            "name": "多渠道测试",
            "cron": "0 9 * * *",
            "hermes_prompt": "统计今日农业公告",
            "notification_channels": ["feishu", "dingtalk", "email"],
            "channel_config": {
                "feishu": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test"},
                "dingtalk": {"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x"},
                "email": {"to": ["ops@example.com"]},
            },
            "enabled": True,
        },
    )
    assert create_resp.status_code == 200
    body = create_resp.json()
    assert body["ok"] is True
    assert body["task"]["notification_channels"] == ["feishu", "dingtalk", "email"]
    assert body["task"]["channel_config"]["email"]["to"] == ["ops@example.com"]

    meta_resp = client.get("/api/chat/scheduled-tasks/meta")
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta.get("notification_channels")
    assert any(ch["value"] == "dingtalk" for ch in meta["notification_channels"])
