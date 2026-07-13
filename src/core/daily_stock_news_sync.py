"""每日股票/财经新闻同步 + 洞察缓存刷新。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent


def setup_daily_stock_log_file() -> Path:
    log_dir = ROOT / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"daily_stock_{datetime.now().strftime('%Y%m%d')}.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    return path


def run_daily_stock_news_sync(
    *,
    schedule_config: Optional[dict[str, Any]] = None,
    days: int = 7,
    refresh_insights: bool = True,
) -> dict[str, Any]:
    """拉取新闻、刷新 stock_insights_cache。"""
    from src.core.stock_news_fetcher import get_stock_news_fetcher
    from src.web.stock_insights_service import get_stock_insights_service

    scheduler_cfg = (schedule_config or {}).get("scheduler", {})
    cron = scheduler_cfg.get("daily_stock_news_sync_cron", "0 5 * * *")

    fetcher = get_stock_news_fetcher()
    fetch_result = fetcher.fetch_all(days=days)

    policy_result: dict[str, Any] = {}
    if scheduler_cfg.get("daily_policy_sync_enabled", True):
        from src.core.policy_news_fetcher import get_policy_news_fetcher
        from src.web.policy_insights_service import get_policy_insights_service

        policy_result = get_policy_news_fetcher().fetch_all(days=30)
        get_policy_insights_service().refresh_cache()

    insights_result: dict[str, Any] = {}
    if refresh_insights and scheduler_cfg.get("daily_stock_news_sync_insights_enabled", True):
        svc = get_stock_insights_service()
        svc.refresh_cache()
        insights_result = {"cache_refreshed": True}

    summary = {
        "success": True,
        "trigger": "daily_stock_news_sync",
        "cron": cron,
        "fetch": fetch_result,
        "policy_fetch": policy_result,
        **insights_result,
        "completed_at": datetime.now().isoformat(),
    }
    logger.info(
        "股票/政策同步完成: news_saved=%s policy_saved=%s sources=%s",
        fetch_result.get("saved_new"),
        policy_result.get("saved_new"),
        fetch_result.get("enabled_sources"),
    )
    return summary
