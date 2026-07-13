#!/usr/bin/env python3
"""验证录制 JSON → YAML（stub + mock LLM）。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rules import CrawlRule
from src.core.recording_converter import format_recording_for_llm, recording_to_yaml_stub
from src.core.rule_generator import RuleGenerator, validate_yaml

FIXTURE = ROOT / "tests" / "fixtures" / "mock_recording_zycg.json"
EXT_DIR = ROOT / "chrome-extension" / "crawl-recorder"
REQUIRED_EXT = [
    "manifest.json",
    "background.js",
    "content.js",
    "selector.js",
    "popup.html",
    "popup.js",
    "popup.css",
]


def test_extension_structure() -> None:
    missing = [f for f in REQUIRED_EXT if not (EXT_DIR / f).is_file()]
    assert not missing, f"扩展缺少文件: {missing}"
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("manifest_version") == 3
    assert "content_scripts" in manifest
    print("✓ Chrome 扩展文件结构完整")


def test_stub_from_recording() -> None:
    recording = json.loads(FIXTURE.read_text(encoding="utf-8"))
    yaml_text = recording_to_yaml_stub(recording, site_id="zycg_national")
    result = validate_yaml(yaml_text, site_id="zycg_national")
    assert result["valid"], result["errors"]
    assert "#noticeShow" in yaml_text
    assert "selectInfoMore.do" in yaml_text
    CrawlRule.model_validate(result["preview"])
    print("✓ stub 直转 mock recording 通过 Pydantic 校验")


def test_format_recording_context() -> None:
    recording = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ctx = format_recording_for_llm(recording)
    assert "zycg_national" in ctx
    assert "wait_network" in ctx
    print("✓ 录制上下文格式化正常")


async def test_generate_from_recording_mock_llm() -> None:
    recording = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mock_yaml = recording_to_yaml_stub(recording, site_id="zycg_national")

    mock_llm = MagicMock()
    mock_llm.available = True
    mock_llm.chat.return_value = (mock_yaml, None)

    gen = RuleGenerator(llm=mock_llm)
    with patch("src.core.rule_generator.fetch_page_hints", return_value=""):
        result = await gen.generate_from_recording(
            site_id="zycg_national",
            url=recording["entry_url"],
            recording=recording,
        )
    assert result["valid"], result.get("errors")
    assert result["source"] == "llm"
    print("✓ generate_from_recording mock LLM 通过")


async def test_generate_from_recording_stub_fallback() -> None:
    recording = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mock_llm = MagicMock()
    mock_llm.available = False

    gen = RuleGenerator(llm=mock_llm)
    result = await gen.generate_from_recording(
        site_id="zycg_national",
        url=recording["entry_url"],
        recording=recording,
    )
    assert result["valid"], result.get("errors")
    assert result["source"] == "stub"
    print("✓ generate_from_recording stub 回退通过")


def main() -> None:
    test_extension_structure()
    test_stub_from_recording()
    test_format_recording_context()
    asyncio.run(test_generate_from_recording_mock_llm())
    asyncio.run(test_generate_from_recording_stub_fallback())
    print("\n全部录制相关测试通过")


if __name__ == "__main__":
    main()
