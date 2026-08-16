"""Commercial C0: billing schema migration.

Run: python migrations/versions/20260816_0002_billing_schema.py
Or:  psql $DATABASE_URL -f docker/migrations/004_billing_schema.sql
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_FILE = PROJECT_ROOT / "docker" / "migrations" / "004_billing_schema.sql"


def upgrade(database_url: str) -> None:
    engine = create_engine(database_url)
    sql = SQL_FILE.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))
    print(f"billing schema applied: {database_url}")


def main() -> int:
    if len(sys.argv) < 2:
        from src.core.config import get_settings

        database_url = get_settings().database_url
        if not database_url:
            print("Usage: python 20260816_0002_billing_schema.py <DATABASE_URL>", file=sys.stderr)
            return 1
    else:
        database_url = sys.argv[1]
    upgrade(database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
