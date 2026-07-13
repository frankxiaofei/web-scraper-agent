"""SQLAlchemy 引擎与会话管理。"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import Base

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker[Session]] = None


@lru_cache(maxsize=1)
def get_engine(database_url: str) -> Engine:
    """按 DATABASE_URL 创建引擎（缓存单例）。"""
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            connect_args={"connect_timeout": 5},
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session(database_url: str) -> Session:
    get_engine(database_url)
    assert _SessionLocal is not None
    return _SessionLocal()


def init_db(database_url: str) -> None:
    """创建表结构（幂等）。"""
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    logger.info("数据库表结构已就绪")


def check_connection(database_url: str) -> bool:
    """探测数据库是否可用。"""
    try:
        engine = get_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.warning("数据库连接失败: %s", e)
        return False
