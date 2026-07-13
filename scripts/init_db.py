#!/usr/bin/env python3
"""初始化 PostgreSQL 表结构（SQLAlchemy create_all 或 SQL 迁移）。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings
from src.db.session import check_connection, get_engine, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("init_db")

MIGRATIONS_DIR = ROOT / "docker" / "migrations"


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    for part in sql.split(";"):
        lines = [line for line in part.splitlines() if not line.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


def apply_migrations(database_url: str) -> None:
    engine = get_engine(database_url)
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        logger.info("应用迁移: %s", path.name)
        sql = path.read_text(encoding="utf-8")
        with engine.begin() as conn:
            for statement in _split_sql_statements(sql):
                conn.execute(text(statement))


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 PostgreSQL 表")
    parser.add_argument(
        "--database-url",
        help="覆盖 DATABASE_URL 环境变量",
    )
    args = parser.parse_args()

    settings = get_settings()
    database_url = args.database_url or settings.database_url
    if not database_url:
        logger.error("请设置 DATABASE_URL 或传入 --database-url")
        sys.exit(1)

    if not check_connection(database_url):
        logger.error("无法连接数据库，请确认 docker compose up 已启动")
        sys.exit(1)

    init_db(database_url)
    apply_migrations(database_url)
    logger.info("表 bid_notices / scrape_jobs 已就绪")


if __name__ == "__main__":
    main()
