#!/usr/bin/env python3
"""同步个股公司资料到本地 MongoDB / JSONL。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.stock_company_service import get_stock_company_service


def main() -> None:
    parser = argparse.ArgumentParser(description="同步个股公司资料（AKShare → 本地存储）")
    parser.add_argument("codes", nargs="+", help="股票代码，如 000001 600519")
    parser.add_argument("--periods", type=int, default=4, help="资产负债表期数（默认 4）")
    args = parser.parse_args()

    result = get_stock_company_service().sync_company_batch(
        args.codes,
        balance_periods=args.periods,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
