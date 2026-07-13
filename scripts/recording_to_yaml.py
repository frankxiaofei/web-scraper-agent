#!/usr/bin/env python3
"""将 Chrome 扩展导出的录制 JSON 转为 crawl rules YAML。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.rule_generator import RuleGenerator, validate_yaml
from src.core.recording_converter import recording_to_yaml_stub


async def main() -> int:
    parser = argparse.ArgumentParser(description="录制 JSON → crawl rules YAML")
    parser.add_argument("recording_file", type=Path, help="录制 JSON 文件路径")
    parser.add_argument("--site-id", required=True, help="站点 ID")
    parser.add_argument("--url", default="", help="入口 URL（可选，默认取 recording.entry_url）")
    parser.add_argument("--stub", action="store_true", help="强制 stub 直转，不调用 LLM")
    parser.add_argument("-o", "--output", type=Path, help="输出 YAML 文件路径")
    args = parser.parse_args()

    recording = json.loads(args.recording_file.read_text(encoding="utf-8"))
    recording.setdefault("site_id", args.site_id)

    if args.stub:
        yaml_text = recording_to_yaml_stub(recording, site_id=args.site_id)
        result = validate_yaml(yaml_text, site_id=args.site_id)
        source = "stub"
    else:
        gen = RuleGenerator()
        result = await gen.generate_from_recording(
            site_id=args.site_id,
            url=args.url or recording.get("entry_url", ""),
            recording=recording,
            prefer_llm=not args.stub,
        )
        yaml_text = result.get("yaml", "")
        source = result.get("source", "unknown")

    print(f"来源: {source}, valid={result.get('valid', False)}")
    if result.get("errors"):
        for err in result["errors"]:
            print(f"  ✗ {err}", file=sys.stderr)
    if result.get("llm_error"):
        print(f"  LLM: {result['llm_error']}", file=sys.stderr)

    if not yaml_text:
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
        print(f"已写入 {args.output}")
    else:
        print("\n--- YAML ---\n")
        print(yaml_text)

    return 0 if result.get("valid") else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
