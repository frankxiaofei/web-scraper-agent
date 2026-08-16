#!/usr/bin/env python3
"""导入 GB/T 4754 行业分类到 mdm.industries。

Usage:
  python scripts/import_gb_industry.py
  python scripts/import_gb_industry.py postgresql://scraper:scraper_pass@localhost:5433/bid_scraper
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEED_FILE = ROOT / "data" / "industry" / "gb2017_seed.yaml"
MIGRATION_FILE = ROOT / "migrations" / "versions" / "20260816_0001_mdm_schema.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mdm_schema_migration", MIGRATION_FILE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if len(sys.argv) > 1:
        database_url = sys.argv[1]
    else:
        from src.core.config import get_settings

        database_url = get_settings().database_url
        if not database_url:
            print("DATABASE_URL required", file=sys.stderr)
            return 1

    _load_migration().upgrade(database_url)

    from src.industry.mdm.repository import MdmRepository

    repo = MdmRepository(database_url)
    data = yaml.safe_load(SEED_FILE.read_text(encoding="utf-8")) or {}
    taxonomy = data.get("taxonomy", "GB2017")
    rows = data.get("industries") or []
    for row in rows:
        repo.upsert_industry(
            row["code"],
            taxonomy,
            row["name"],
            level=int(row["level"]),
            parent_code=row.get("parent_code"),
        )
    count = repo.count_industries(taxonomy)
    print(f"imported GB/T industries: {count} rows ({taxonomy})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
