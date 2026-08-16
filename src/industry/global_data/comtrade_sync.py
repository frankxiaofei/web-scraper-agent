"""UN Comtrade 贸易流同步 stub（Phase 3 P3-T-001）。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

COMTRADE_API = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"


def fetch_trade_flows(
    *,
    reporter: str = "156",
    partner: str = "0",
    period: str = "2024",
    commodity_code: str = "10",
) -> list[dict[str, Any]]:
    """拉取 UN Comtrade 预览数据并映射为 TradeFlow 节点属性。"""
    params = {
        "reporterCode": reporter,
        "partnerCode": partner,
        "period": period,
        "cmdCode": commodity_code,
    }
    try:
        resp = httpx.get(COMTRADE_API, params=params, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("Comtrade fetch failed: %s", exc)
        return []

    flows: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        flows.append(
            {
                "reporter": row.get("reporterDesc") or reporter,
                "partner": row.get("partnerDesc") or partner,
                "commodity_code": row.get("cmdCode") or commodity_code,
                "trade_value_usd": row.get("primaryValue"),
                "period": row.get("period") or period,
                "flow_type": row.get("flowDesc") or "Import",
            }
        )
    return flows
