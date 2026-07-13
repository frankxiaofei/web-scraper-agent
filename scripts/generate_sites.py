#!/usr/bin/env python3
"""从 Excel 生成 config/sites.yaml。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# 将项目根目录加入 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.excel_loader import load_sites_from_excel, sites_to_yaml_dict

EXCEL_PATH = ROOT / "docs" / "招标网址 副本.xlsx"
OUTPUT_PATH = ROOT / "config" / "sites.yaml"


def main() -> None:
    if not EXCEL_PATH.exists():
        print(f"错误: Excel 文件不存在: {EXCEL_PATH}")
        sys.exit(1)

    sites = load_sites_from_excel(EXCEL_PATH)
    data = sites_to_yaml_dict(sites)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        yaml.dump(
            data,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )

    print(f"已生成 {OUTPUT_PATH}")
    print(f"  总站点数: {data['total']}")
    print(f"  MVP 站点: {data['mvp_count']}")
    enabled = sum(1 for s in sites if s.get("enabled"))
    print(f"  已启用: {enabled}")


if __name__ == "__main__":
    main()
