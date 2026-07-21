"""crawl_agent prompt 调试日志单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.core.crawl_agent_prompt_log import (
    is_prompt_logging_enabled,
    log_hermes_run_payload,
)


def test_is_prompt_logging_enabled_default_true():
    with patch.dict("os.environ", {}, clear=True):
        assert is_prompt_logging_enabled() is True
    with patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "1"}):
        assert is_prompt_logging_enabled() is True


def test_is_prompt_logging_disabled():
    with patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "0"}):
        assert is_prompt_logging_enabled() is False
    with patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "false"}):
        assert is_prompt_logging_enabled() is False


def test_log_hermes_run_payload_writes_json_line(tmp_path: Path):
    log_file = tmp_path / "crawl_agent_prompt.log"
    with (
        patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "1"}),
        patch("src.core.crawl_agent_prompt_log._LOG_PATH", log_file),
        patch("src.core.crawl_agent_prompt_log._INSTRUCTIONS_FULL_DIR", tmp_path / "instr"),
    ):
        log_hermes_run_payload(
            layer="test",
            input_text="【当前问题】\n新问题",
            conversation_history=[{"role": "user", "content": "旧问题"}],
            instructions="system prompt",
            session_id="sess-1",
            hermes_session_id="hermes-1",
        )

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["layer"] == "test"
    assert "新问题" in record["input"]
    assert record["conversation_history"][0]["content"] == "旧问题"
    assert record["instructions"] == "system prompt"
    assert record["hermes_session_id"] == "hermes-1"


def test_log_hermes_run_payload_skips_when_disabled(tmp_path: Path):
    log_file = tmp_path / "crawl_agent_prompt.log"
    with (
        patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "0"}),
        patch("src.core.crawl_agent_prompt_log._LOG_PATH", log_file),
    ):
        log_hermes_run_payload(layer="test", input_text="x")
    assert not log_file.exists()


def test_log_hermes_run_payload_redacts_bearer(tmp_path: Path):
    log_file = tmp_path / "crawl_agent_prompt.log"
    with (
        patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "1"}),
        patch("src.core.crawl_agent_prompt_log._LOG_PATH", log_file),
        patch("src.core.crawl_agent_prompt_log._INSTRUCTIONS_FULL_DIR", tmp_path / "instr"),
    ):
        log_hermes_run_payload(
            layer="test",
            input_text="token Bearer sk-secret123 here",
            instructions="api_key=abc",
        )
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert "sk-secret" not in record["input"]
    assert "[REDACTED]" in record["input"]


def test_log_hermes_run_payload_multi_turn_history(tmp_path: Path):
    """验证多轮历史（≥2 条）中每条消息在日志 JSON 和终端日志中都被正确记录。"""
    log_file = tmp_path / "crawl_agent_prompt.log"
    with (
        patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "1"}),
        patch("src.core.crawl_agent_prompt_log._LOG_PATH", log_file),
        patch("src.core.crawl_agent_prompt_log._INSTRUCTIONS_FULL_DIR", tmp_path / "instr"),
        patch("src.core.crawl_agent_prompt_log._PROMPT_LOGGER") as mock_logger,
    ):
        log_hermes_run_payload(
            layer="test",
            input_text="【当前问题】\n帮我爬取铁建采购网",
            conversation_history=[
                {"role": "user", "content": "第一条：查 ggzy 上海市"},
                {"role": "user", "content": "第二条：查 tjbid 状态"},
            ],
            instructions="system prompt here",
            session_id="sess-multi",
        )

    # 验证 JSON 日志文件
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    # conversation_history 应包含两条
    hist = record["conversation_history"]
    assert len(hist) == 2
    assert hist[0]["role"] == "user"
    assert "ggzy 上海市" in hist[0]["content"]
    assert hist[1]["role"] == "user"
    assert "tjbid" in hist[1]["content"]

    # 终端日志应打印两条 history_preview
    info_calls = [c[0] for c in mock_logger.info.call_args_list]
    history_preview_line = [line for line in info_calls if "history_preview" in str(line)]
    assert len(history_preview_line) == 1
    preview_str = str(history_preview_line[0])
    assert "0:user:" in preview_str
    assert "1:user:" in preview_str


def test_log_hermes_run_payload_second_message_index_is_one(tmp_path: Path):
    """第二条消息的 index 必须为 1，不受 role 或其他字段影响。"""
    log_file = tmp_path / "crawl_agent_prompt.log"
    with (
        patch.dict("os.environ", {"CRAWL_AGENT_LOG_PROMPT": "1"}),
        patch("src.core.crawl_agent_prompt_log._LOG_PATH", log_file),
        patch("src.core.crawl_agent_prompt_log._INSTRUCTIONS_FULL_DIR", tmp_path / "instr"),
        patch("src.core.crawl_agent_prompt_log._PROMPT_LOGGER") as mock_logger,
    ):
        log_hermes_run_payload(
            layer="test",
            input_text="当前轮次问题",
            conversation_history=[
                {"role": "user", "content": "历史消息 A"},
                {"role": "user", "content": "历史消息 B —— 这是第二条"},
            ],
        )

    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert len(record["conversation_history"]) == 2

    # 通过终端日志验证 _history_summary 输出的 index 正确
    info_calls = [c[0] for c in mock_logger.info.call_args_list]
    preview_line = [line for line in info_calls if "history_preview" in str(line)]
    assert len(preview_line) == 1
    preview_text = str(preview_line[0])
    # 索引 1 对应"第二条"
    assert "1:user:" in preview_text
    assert "第二条" in preview_text
