"""hermes_crawl_client 巡检逻辑单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.hermes_crawl_client import (
    append_log,
    build_agent_prompt,
    classify_site_attention,
    handle_stale_resets,
    is_failed_site,
    is_healthy_zero_new,
    is_overdue_no_success,
    reset_stale_site,
    run_poll_cycle,
)


def _now() -> datetime:
    return datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_healthy_zero_new_completed_not_flagged():
    site = {
        "enabled": True,
        "status": "completed",
        "display_status": "success",
        "last_run_status": "success",
        "new_count_display": 0,
        "last_success_at": "2026-06-17T08:00:00+00:00",
    }
    assert is_healthy_zero_new(site) is True
    assert is_failed_site(site) is False
    assert classify_site_attention(site, now=_now()) == []


def test_failed_site_detected():
    site = {
        "enabled": True,
        "status": "failed",
        "display_status": "failed",
        "last_run_status": "failed",
        "last_error": "selector timeout",
    }
    assert is_failed_site(site) is True
    assert "failed" in classify_site_attention(site, now=_now())


def test_stale_sync_flagged_as_failed():
    site = {
        "enabled": True,
        "status": "syncing",
        "sync_stale": True,
        "is_running": False,
    }
    assert is_failed_site(site) is True
    assert classify_site_attention(site, now=_now()) == ["stale_sync"]


def test_overdue_24h_without_success():
    site = {
        "enabled": True,
        "status": "pending",
        "last_success_at": None,
        "last_sync_at": "2026-06-15T08:00:00+00:00",
    }
    assert is_overdue_no_success(site, now=_now()) is True
    assert classify_site_attention(site, now=_now()) == ["overdue_24h"]


def test_recent_success_not_overdue():
    site = {
        "enabled": True,
        "status": "completed",
        "display_status": "success",
        "last_run_status": "success",
        "new_count_display": 3,
        "last_success_at": "2026-06-17T08:00:00+00:00",
    }
    assert is_overdue_no_success(site, now=_now()) is False


def test_never_synced_not_overdue():
    site = {
        "enabled": True,
        "status": "pending",
        "last_success_at": None,
        "last_sync_at": None,
    }
    assert is_overdue_no_success(site, now=_now()) is False


def test_disabled_site_ignored_for_overdue():
    site = {
        "enabled": False,
        "status": "pending",
        "last_success_at": None,
        "last_sync_at": "2026-06-15T08:00:00+00:00",
    }
    assert is_overdue_no_success(site, now=_now()) is False


def test_build_agent_prompt_mentions_crawl_reset():
    sites = [
        {
            "site_id": "foo",
            "attention_reasons": ["stale_sync"],
            "last_error": "orphan syncing",
        }
    ]
    prompt = build_agent_prompt(sites)
    assert "/api/sync/" in prompt and "reset" in prompt
    assert "foo" in prompt


@patch("scripts.hermes_crawl_client._post_json")
def test_reset_stale_site_success(mock_post: MagicMock):
    mock_post.return_value = {"ok": True, "site_id": "foo", "previous_status": "syncing"}
    result = reset_stale_site("foo")
    assert result["ok"] is True
    mock_post.assert_called_once()
    assert "/api/sync/foo/reset" in mock_post.call_args[0][0]


@patch("scripts.hermes_crawl_client._post_json")
def test_reset_stale_site_http_error(mock_post: MagicMock):
    mock_post.return_value = {"ok": False, "http_status": 409, "error": "not stale"}
    result = reset_stale_site("bar")
    assert result["ok"] is False


def test_handle_stale_resets_suggest_only():
    attention = [
        {"site_id": "stale_a", "attention_reasons": ["stale_sync"]},
        {"site_id": "failed_b", "attention_reasons": ["failed"]},
    ]
    logs: list[str] = []
    outcomes = handle_stale_resets(attention, auto_reset=False, log_fn=logs.append)
    assert len(outcomes) == 1
    assert outcomes[0]["action"] == "suggested"
    assert any("stale_a" in line for line in logs)
    assert not any("failed_b" in line for line in logs)


@patch("scripts.hermes_crawl_client.reset_stale_site")
def test_handle_stale_resets_auto(mock_reset: MagicMock):
    mock_reset.return_value = {"ok": True, "site_id": "stale_a"}
    attention = [{"site_id": "stale_a", "attention_reasons": ["stale_sync"]}]
    outcomes = handle_stale_resets(attention, auto_reset=True)
    mock_reset.assert_called_once_with("stale_a")
    assert outcomes[0]["action"] == "reset"
    assert outcomes[0]["ok"] is True


def test_append_log_writes_timestamped_line(tmp_path: Path):
    log_file = tmp_path / "agent-crawl.log"
    append_log("hello test", log_file, also_stdout=False)
    text = log_file.read_text(encoding="utf-8")
    assert "hello test" in text
    assert text.startswith("[")


@patch("scripts.hermes_crawl_client.run_hermes_once")
def test_run_poll_cycle_no_attention(mock_hermes: MagicMock, tmp_path: Path):
    mock_hermes.return_value = 0

    def _empty(**kwargs):
        return []

    rc = run_poll_cycle(
        log_path=tmp_path / "log.txt",
        attention_fetcher=_empty,
        spawn_hermes=True,
    )
    assert rc == 0
    mock_hermes.assert_not_called()
    log_text = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "无 attention 站点" in log_text


@patch("scripts.hermes_crawl_client.run_hermes_once")
@patch("scripts.hermes_crawl_client.reset_stale_site")
def test_run_poll_cycle_with_attention_and_auto_reset(
    mock_reset: MagicMock,
    mock_hermes: MagicMock,
    tmp_path: Path,
):
    mock_hermes.return_value = 0
    mock_reset.return_value = {"ok": True, "site_id": "stale_x"}

    def _fetch(**kwargs):
        return [
            {"site_id": "stale_x", "attention_reasons": ["stale_sync"], "last_error": ""},
        ]

    rc = run_poll_cycle(
        log_path=tmp_path / "log.txt",
        attention_fetcher=_fetch,
        auto_reset=True,
        spawn_hermes=True,
    )
    assert rc == 0
    mock_reset.assert_called_once_with("stale_x")
    mock_hermes.assert_called_once()
    log_text = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "auto-reset 成功" in log_text


@patch("scripts.hermes_crawl_client.run_hermes_once")
def test_run_poll_cycle_no_spawn(mock_hermes: MagicMock, tmp_path: Path):
    def _fetch(**kwargs):
        return [{"site_id": "fail_y", "attention_reasons": ["failed"]}]

    rc = run_poll_cycle(
        log_path=tmp_path / "log.txt",
        attention_fetcher=_fetch,
        spawn_hermes=False,
    )
    assert rc == 0
    mock_hermes.assert_not_called()
    log_text = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "spawn_hermes=False" in log_text


@patch("scripts.hermes_crawl_client.run_poll_cycle", return_value=0)
def test_main_spawn_hermes_flag(mock_poll: MagicMock):
    import sys

    from scripts import hermes_crawl_client

    with patch.object(sys, "argv", ["hermes_crawl_client.py", "--spawn-hermes"]):
        rc = hermes_crawl_client.main()
    assert rc == 0
    mock_poll.assert_called_once()
    assert mock_poll.call_args.kwargs.get("spawn_hermes") is True
