"""bootstrap_hermes_agent LLM provider 选择逻辑测试。"""

from __future__ import annotations

from scripts.bootstrap_hermes_agent import (
    has_real_llm_key,
    is_real_api_key,
    resolve_llm_model_config,
)


def test_is_real_api_key_rejects_placeholder():
    assert not is_real_api_key("sk-xxx")
    assert not is_real_api_key("")
    assert is_real_api_key("sk-1234567890abcdef")


def test_resolve_provider_openrouter_when_key_present():
    cfg = resolve_llm_model_config({"OPENROUTER_API_KEY": "or-key-12345678"})
    assert cfg["provider"] == "openrouter"


def test_resolve_provider_openai_when_only_openai_key():
    cfg = resolve_llm_model_config(
        {
            "OPENAI_API_KEY": "sk-real-key-123456",
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "OPENAI_MODEL": "deepseek-v4-flash",
        }
    )
    assert cfg["provider"] == "openai-api"
    assert cfg["base_url"] == "https://api.deepseek.com"
    assert cfg["default"] == "deepseek-v4-flash"


def test_resolve_provider_deepseek_default_model():
    cfg = resolve_llm_model_config({"DEEPSEEK_API_KEY": "sk-real-key-123456"})
    assert cfg["provider"] == "deepseek"
    assert cfg["default"] == "deepseek-v4-flash"


def test_resolve_provider_openai_api_not_openrouter_when_placeholder():
    cfg = resolve_llm_model_config({"OPENAI_API_KEY": "sk-xxx"})
    assert cfg["provider"] == "openai-api"
    assert not has_real_llm_key({"OPENAI_API_KEY": "sk-xxx"})
