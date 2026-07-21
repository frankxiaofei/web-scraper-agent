"""系统设置 API 与 settings_service 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.web.settings_service import (
    get_public_settings,
    is_masked_or_empty,
    mask_secret,
    parse_env_file,
    save_settings,
    write_env_updates,
)


@pytest.fixture
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# demo env",
                "HERMES_AGENT_URL=http://127.0.0.1:8080",
                "HERMES_AGENT_CHAT_URL=http://127.0.0.1:8642",
                "HERMES_AGENT_API_KEY=e138abcdefghijklmnop93e3",
                "LLM_MODEL=kimi-k2",
                "OPENAI_API_KEY=sk-testkey12345678",
                "DATABASE_URL=postgresql://localhost/test",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.core.config.ROOT", tmp_path)
    monkeypatch.setattr("src.web.settings_service.ENV_FILE", env_file)
    monkeypatch.delenv("HERMES_AGENT_URL", raising=False)
    monkeypatch.delenv("HERMES_AGENT_CHAT_URL", raising=False)
    monkeypatch.delenv("HERMES_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return {"env_file": env_file}


def test_mask_secret_short_and_long():
    assert mask_secret("") == ""
    assert mask_secret("abc") == "***"
    assert mask_secret("e138abcdefghijklmnop93e3") == "e138****93e3"
    assert mask_secret("sk-testkey12345678") == "sk-t****5678"


def test_is_masked_or_empty():
    assert is_masked_or_empty(None) is True
    assert is_masked_or_empty("") is True
    assert is_masked_or_empty("   ") is True
    assert is_masked_or_empty("e138****93e3") is True
    assert is_masked_or_empty("sk-new-key-value") is False


def test_write_env_updates_preserves_other_lines(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FOO=bar\nHERMES_AGENT_URL=http://old:8080\n# comment\nBAZ=1\n",
        encoding="utf-8",
    )
    write_env_updates(env_file, {"HERMES_AGENT_URL": "http://new:8080"})
    text = env_file.read_text(encoding="utf-8")
    assert "FOO=bar" in text
    assert "HERMES_AGENT_URL=http://new:8080" in text
    assert "# comment" in text
    assert "BAZ=1" in text


def test_get_public_settings_masks_keys(settings_env):
    data = get_public_settings()
    assert data["hermes_agent_url"] == "http://127.0.0.1:8080"
    assert data["hermes_agent_chat_url"] == "http://127.0.0.1:8642"
    assert data["llm_model"] == "kimi-k2"
    assert "****" in data["hermes_agent_api_key"]
    assert "****" in data["llm_api_key"]
    assert "e138" in data["hermes_agent_api_key"]
    assert "93e3" in data["hermes_agent_api_key"]


def test_save_settings_updates_urls_and_model(settings_env):
    env_file = settings_env["env_file"]
    save_settings(
        {
            "hermes_agent_url": "http://10.0.0.1:8080",
            "hermes_agent_chat_url": "http://10.0.0.1:8642",
            "llm_model": "gpt-4o",
        },
        env_path=env_file,
    )
    parsed = parse_env_file(env_file)
    assert parsed["HERMES_AGENT_URL"] == "http://10.0.0.1:8080"
    assert parsed["HERMES_AGENT_CHAT_URL"] == "http://10.0.0.1:8642"
    assert parsed["LLM_MODEL"] == "gpt-4o"
    assert parsed["HERMES_AGENT_API_KEY"] == "e138abcdefghijklmnop93e3"
    assert parsed["OPENAI_API_KEY"] == "sk-testkey12345678"
    assert parsed["DATABASE_URL"] == "postgresql://localhost/test"


def test_save_settings_masked_key_does_not_overwrite(settings_env):
    env_file = settings_env["env_file"]
    save_settings(
        {
            "hermes_agent_api_key": "e138****93e3",
            "llm_api_key": "sk-t****5678",
        },
        env_path=env_file,
    )
    parsed = parse_env_file(env_file)
    assert parsed["HERMES_AGENT_API_KEY"] == "e138abcdefghijklmnop93e3"
    assert parsed["OPENAI_API_KEY"] == "sk-testkey12345678"


def test_save_settings_empty_key_does_not_overwrite(settings_env):
    env_file = settings_env["env_file"]
    save_settings(
        {
            "hermes_agent_api_key": "",
            "llm_api_key": "   ",
        },
        env_path=env_file,
    )
    parsed = parse_env_file(env_file)
    assert parsed["HERMES_AGENT_API_KEY"] == "e138abcdefghijklmnop93e3"
    assert parsed["OPENAI_API_KEY"] == "sk-testkey12345678"


def test_save_settings_new_key_overwrites(settings_env):
    env_file = settings_env["env_file"]
    save_settings(
        {
            "hermes_agent_api_key": "new-hermes-secret-key",
            "llm_api_key": "sk-brand-new-api-key",
        },
        env_path=env_file,
    )
    parsed = parse_env_file(env_file)
    assert parsed["HERMES_AGENT_API_KEY"] == "new-hermes-secret-key"
    assert parsed["OPENAI_API_KEY"] == "sk-brand-new-api-key"


def test_api_get_settings_masks_keys(settings_env):
    from src.web.app import app

    client = TestClient(app)
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hermes_agent_url"] == "http://127.0.0.1:8080"
    assert "****" in data["llm_api_key"]


def test_api_put_settings(settings_env):
    from src.web.app import app

    client = TestClient(app)
    resp = client.put(
        "/api/settings",
        json={
            "hermes_agent_url": "http://192.168.1.10:8080",
            "hermes_agent_chat_url": "http://192.168.1.10:8642",
            "llm_model": "deepseek-v4-pro",
            "hermes_agent_api_key": "e138****93e3",
            "llm_api_key": "",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    parsed = parse_env_file(settings_env["env_file"])
    assert parsed["HERMES_AGENT_URL"] == "http://192.168.1.10:8080"
    assert parsed["HERMES_AGENT_CHAT_URL"] == "http://192.168.1.10:8642"
    assert parsed["LLM_MODEL"] == "deepseek-v4-pro"
    assert parsed["HERMES_AGENT_API_KEY"] == "e138abcdefghijklmnop93e3"
    assert parsed["OPENAI_API_KEY"] == "sk-testkey12345678"


def test_base_page_has_settings_button():
    from src.web.app import app

    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/hermes"
    html = client.get("/hermes").text
    assert 'id="btn-settings"' in html
    assert 'aria-label="设置"' in html
    assert 'id="settings-modal"' in html
    app_modal_pos = html.find("window.AppModal")
    settings_init_pos = html.find("initSettingsUI")
    assert app_modal_pos >= 0
    assert settings_init_pos >= 0
    assert app_modal_pos < settings_init_pos
