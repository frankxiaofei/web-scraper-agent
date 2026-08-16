#!/usr/bin/env python3
"""导入省级区划到 mdm.regions（Phase 1 种子，无 GeoJSON 边界）。

Usage:
  python scripts/import_regions_postgis.py
  python scripts/import_regions_postgis.py postgresql://scraper:scraper_pass@localhost:5433/bid_scraper
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    from src.industry.region_normalizer import PROVINCE_ALIASES

    repo = MdmRepository(database_url)
    seen: set[str] = set()
    for _alias, (code, full_name) in PROVINCE_ALIASES.items():
        if code in seen:
            continue
        seen.add(code)
        adcode = code.replace("CN-", "") + "0000" if code.startswith("CN-") else None
        repo.upsert_region(code, full_name, level="province", adcode=adcode)

    count = repo.count_regions("province")
    print(f"imported regions: {count} province rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
