"""信息抽取专用模型配置单元测试。"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import Settings, get_extraction_model
from src.core.key_info_extractor import _llm_extract


def test_get_extraction_model_fallback_to_llm_model():
    settings = Settings.model_construct(
        llm_model="deepseek-v4-pro",
        llm_extraction_model=None,
    )
    assert get_extraction_model(settings) == "deepseek-v4-pro"


def test_get_extraction_model_explicit():
    settings = Settings.model_construct(
        llm_model="deepseek-v4-pro",
        llm_extraction_model="deepseek-v4-flash",
    )
    assert get_extraction_model(settings) == "deepseek-v4-flash"


def test_get_extraction_model_accepts_extraction_model_alias(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXTRACTION_MODEL", "qwen-turbo")
    monkeypatch.delenv("LLM_EXTRACTION_MODEL", raising=False)
    settings = Settings(llm_model="deepseek-v4-pro")
    assert get_extraction_model(settings) == "qwen-turbo"


def test_llm_extract_uses_extraction_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("KEY_INFO_LLM", "true")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_EXTRACTION_MODEL", "deepseek-v4-flash")

    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=45):
        body = json.loads(req.data.decode("utf-8"))
        captured["model"] = body["model"]
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "contract_amount": "100万元",
                                "tender_party": "测试单位",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", fake_urlopen):
        result = _llm_extract("预算金额：100万元", title="测试公告")

    assert captured["model"] == "deepseek-v4-flash"
    assert result is not None
    assert result["contract_amount"] == "100万元"
