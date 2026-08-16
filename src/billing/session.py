"""Billing DB session management."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import lru_cache
from typing import Generator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.billing.models import BillingBase
from src.core.config import get_settings

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker[Session]] = None


def billing_database_configured() -> bool:
    return bool(get_settings().database_url)


@lru_cache(maxsize=1)
def get_billing_engine(database_url: str) -> Engine:
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


def get_billing_session() -> Session:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for billing")
    get_billing_engine(settings.database_url)
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def billing_session_scope() -> Generator[Session, None, None]:
    session = get_billing_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_billing_schema(database_url: str | None = None) -> None:
    """Create billing schema tables (PostgreSQL). Prefer SQL migration in production."""
    url = database_url or get_settings().database_url
    if not url:
        logger.warning("DATABASE_URL not set; skipping billing schema init")
        return
    engine = get_billing_engine(url)
    if engine.dialect.name != "postgresql":
        BillingBase.metadata.create_all(engine)
        logger.info("billing tables created (non-postgresql)")
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS billing"))
    BillingBase.metadata.create_all(engine)
    logger.info("billing schema ready")
