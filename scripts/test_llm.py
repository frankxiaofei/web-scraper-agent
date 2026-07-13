#!/usr/bin/env python3
"""验证 LLM 连接（DeepSeek / OpenAI 兼容接口）并确认返回 JSON。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings
from src.intelligent_crawl.llm_client import create_llm_client


def main() -> None:
    settings = get_settings()
    if not settings.intelligent_crawl_llm:
        print("请设置 INTELLIGENT_CRAWL_LLM=true（可复制 .env.example → .env）")
        sys.exit(1)
    if not settings.openai_api_key:
        print("请设置 OPENAI_API_KEY")
        sys.exit(1)

    client = create_llm_client()
    if not client:
        print("无法创建 LLM 客户端，请检查 OPENAI_API_KEY")
        sys.exit(1)

    print(f"模型: {client.model}")
    print(f"Base URL: {client.base_url or '(OpenAI 默认)'}")
    print("发送测试请求…")

    import urllib.error
    import urllib.request

    body = json.dumps(
        {
            "model": client.model,
            "messages": [
                {"role": "system", "content": "你是招标网站爬取助手，只输出合法 JSON。"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "返回示例决策",
                            "hint": "仅返回 JSON: {page_type, should_recurse, next_urls}",
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    url = (
        f"{client.base_url}/v1/chat/completions"
        if client.base_url
        else "https://api.openai.com/v1/chat/completions"
    )
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"LLM HTTP 错误 {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}")
        sys.exit(1)
    except Exception as e:
        print(f"请求失败: {e}")
        sys.exit(1)

    content = payload["choices"][0]["message"]["content"].strip()
    if "```" in content:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            content = content[start:end]

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"响应非合法 JSON: {e}")
        print(f"原始内容: {content[:300]}")
        sys.exit(1)

    required = {"page_type", "should_recurse", "next_urls"}
    missing = required - set(data.keys())
    if missing:
        print(f"JSON 缺少字段: {missing}")
        print(f"收到: {data}")
        sys.exit(1)

    print(f"LLM 返回 JSON: {json.dumps(data, ensure_ascii=False)}")
    print("LLM 连接验证通过 ✓")


if __name__ == "__main__":
    main()
