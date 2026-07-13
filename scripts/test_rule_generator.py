#!/usr/bin/env python3
"""测试 AI 规则生成：mock LLM 或真实 API。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rules import CrawlRule
from src.core.rule_generator import RuleGenerator, validate_yaml, _strip_yaml_fences

MOCK_ZYCG_YAML = """version: 1
site_id: zycg_national
name: 中央政府采购网
enabled: true
entry_url: https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/cggg/index.html
list_page:
  strategy: dom_after_ajax
  container: "#noticeShow"
  item: "li"
  title: "a.titleHiding span"
  link: "a.titleHiding"
  wait_network:
    url_pattern: "selectInfoMore.do"
    timeout_ms: 15000
  api:
    url: https://www.zycg.gov.cn/freecms/rest/v1/notice/selectInfoMore.do
    method: GET
    params:
      siteId: "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d"
      channel: "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5"
      pageSize: 15
    page_param: currPage
    items_path: data
    total_path: total
    fields:
      title: title
      url: pageurl
      id: id
      date: addtimeStr
    url_template: "{url}?id={id}"
pagination:
  type: ajax_param
  page_param: currPage
  page_size: 15
  max_pages: 20
  stop_when_empty: true
detail:
  fetch_detail: false
  url_pattern: "/ggxx/info/|/tzgg/info/"
limits:
  max_pages: 20
  max_items: 100
  max_depth: 2
  rate_limit_seconds: 1.0"""


def test_validate_existing_zycg() -> None:
    path = ROOT / "config" / "crawl_rules" / "zycg_national.yaml"
    text = path.read_text(encoding="utf-8")
    result = validate_yaml(text, site_id="zycg_national")
    assert result["valid"], result["errors"]
    assert result["preview"]["site_id"] == "zycg_national"
    print("✓ 现有 zycg_national.yaml 通过 Pydantic 校验")


def test_strip_yaml_fences() -> None:
    wrapped = "```yaml\n" + MOCK_ZYCG_YAML + "\n```"
    assert _strip_yaml_fences(wrapped).startswith("version: 1")
    print("✓ YAML 围栏剥离正常")


async def test_mock_generate() -> None:
    mock_llm = MagicMock()
    mock_llm.available = True
    mock_llm.chat.return_value = (MOCK_ZYCG_YAML, None)

    gen = RuleGenerator(llm=mock_llm)
    with patch("src.core.rule_generator.fetch_page_hints", return_value="页面标题: 测试"):
        result = await gen.generate(
            site_id="zycg_national",
            url="https://www.zycg.gov.cn/",
            messages=[{"role": "user", "content": "生成中央政府采购网 AJAX 分页规则"}],
        )
    assert result["valid"], result.get("errors")
    assert "zycg_national" in result["yaml"]
    CrawlRule.model_validate(result["preview"])
    print("✓ Mock LLM 生成并通过校验")


async def test_real_llm() -> None:
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.openai_api_key:
        print("⊘ 跳过真实 LLM 测试（未配置 OPENAI_API_KEY）")
        return

    gen = RuleGenerator()
    result = await gen.generate(
        site_id="zycg_national",
        url="https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/cggg/index.html",
        messages=[
            {
                "role": "user",
                "content": "为中央政府采购网生成爬取规则：采购公告列表 AJAX 加载，REST API selectInfoMore.do 分页，每页15条，max_pages 20",
            }
        ],
        fetch_hints=True,
    )
    if result.get("llm_error"):
        print(f"⊘ 真实 LLM 测试失败: {result['llm_error']}")
        return
    print(f"  valid={result['valid']}, yaml 长度={len(result.get('yaml', ''))}")
    if result["valid"]:
        print("✓ 真实 LLM 生成并通过 Pydantic 校验")
    else:
        print(f"✗ 校验失败: {result.get('errors')}")


def main() -> None:
    test_validate_existing_zycg()
    test_strip_yaml_fences()
    asyncio.run(test_mock_generate())
    asyncio.run(test_real_llm())
    print("\n全部测试完成")


if __name__ == "__main__":
    main()
