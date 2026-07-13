"""自动选股模块与 API 单元测试。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.stock_simulator_stock_picker import (
    _historical_volatility,
    _ma_on,
    list_pick_strategies,
    pick_stocks,
)
from src.web.stock_app import app as stock_app


MOCK_CONSTITUENTS = [
    {"code": "000001", "name": "平安银行", "change_pct": 1.2},
    {"code": "600036", "name": "招商银行", "change_pct": 3.5},
    {"code": "601318", "name": "中国平安", "change_pct": 2.1},
    {"code": "000002", "name": "万科A", "change_pct": -0.5},
    {"code": "600519", "name": "贵州茅台", "change_pct": 0.8},
]


def _mock_kline_series(
    code: str = "600036",
    *,
    start: datetime | None = None,
    n: int = 80,
    daily_return: float = 0.002,
    name: str = "招商银行",
) -> list[dict]:
    start = start or datetime(2025, 1, 2)
    items: list[dict] = []
    price = 10.0
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        price *= 1 + daily_return
        items.append(
            {
                "code": code,
                "market": "SH" if code.startswith("6") else "SZ",
                "name": name,
                "date": d.strftime("%Y-%m-%d"),
                "close": round(price, 4),
                "change_pct": daily_return * 100,
            }
        )
    return items


def test_pick_stocks_sector_momentum_rules():
    mock_detail = {
        "ok": True,
        "symbol": "人工智能",
        "classify": "concept",
        "constituents": MOCK_CONSTITUENTS,
    }

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        svc = mock_mkt.return_value
        svc.get_sector_detail.return_value = mock_detail
        result = pick_stocks(
            universe="sector",
            classify="concept",
            symbol="人工智能",
            limit=3,
            strategy="rules",
        )

    assert result["ok"] is True
    assert len(result["items"]) == 3
    codes = [i["code"] for i in result["items"]]
    assert codes[0] == "600036"
    assert all(i.get("market") in ("SH", "SZ", "BJ") for i in result["items"])
    assert result["items"][0].get("reason")


def test_pick_stocks_agent_fallback_to_sector():
    mock_detail = {
        "ok": True,
        "symbol": "人工智能",
        "constituents": MOCK_CONSTITUENTS,
    }

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        svc = mock_mkt.return_value
        svc.get_sectors_trading.return_value = {"ok": True, "items": [{"name": "人工智能", "change_pct": 2.0}]}
        svc.get_sector_detail.return_value = mock_detail
        with patch("src.core.llm_chat.create_chat_client") as mock_llm:
            mock_llm.return_value.available = False
            result = pick_stocks(
                universe="sector",
                classify="concept",
                symbol="人工智能",
                limit=2,
                strategy="agent",
            )

    assert result["ok"] is True
    assert result.get("fallback") is True
    assert len(result["items"]) == 2
    assert result["items"][0]["code"] == "600036"


def test_pick_stocks_watchlist_fallback(tmp_path, monkeypatch):
    wl = tmp_path / "watchlist.yaml"
    wl.write_text(
        "stocks:\n  - code: '000001'\n    name: 平安银行\n    market: SZ\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.core.stock_simulator_stock_picker.WATCHLIST_PATH", wl)

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        svc = mock_mkt.return_value
        svc.get_sector_detail.return_value = {"ok": False, "error": "mock fail"}
        result = pick_stocks(
            universe="sector",
            classify="concept",
            symbol="不存在板块",
            limit=1,
            strategy="rules",
        )

    assert result["ok"] is True
    assert result.get("fallback") is True
    assert result["items"][0]["code"] == "000001"


def _mock_kline_items(n: int = 80) -> list[dict]:
    return _mock_kline_series("600036", n=n)


def test_pick_stocks_momentum_lookback_pit():
    """动量策略：高涨幅标的应排在前面。"""
    fast = _mock_kline_series("600036", daily_return=0.01, n=40)
    slow = _mock_kline_series("000001", daily_return=0.001, n=40)
    as_of = fast[-1]["date"]

    def fake_klines(code, *, market=None, as_of_date, lookback_days=60):
        if code == "600036":
            return fast
        if code == "000001":
            return slow
        return []

    with patch("src.core.stock_simulator_stock_picker._sector_constituents") as mock_sec:
        mock_sec.return_value = ("测试", None, MOCK_CONSTITUENTS[:2], "测试板块")
        with patch("src.core.stock_simulator_stock_picker._klines_as_of", side_effect=fake_klines):
            result = pick_stocks(
                universe="sector",
                classify="concept",
                symbol="测试",
                limit=2,
                strategy="momentum",
                as_of_date=as_of,
                momentum_lookback=5,
            )

    assert result["ok"] is True
    assert result["strategy"] == "momentum"
    assert result["items"][0]["code"] == "600036"


def test_pick_stocks_dual_ma_filters_ma5_above_ma20():
    """双均线：仅 close>MA20 且 MA5>MA20 的标的入选。"""
    uptrend = _mock_kline_series("600036", daily_return=0.005, n=50)
    flat = _mock_kline_series("000001", daily_return=0.0, n=50)
    as_of = uptrend[-1]["date"]

    close_u = uptrend[-1]["close"]
    ma20_u = _ma_on(uptrend, as_of, 20)
    ma5_u = _ma_on(uptrend, as_of, 5)
    assert close_u > ma20_u and ma5_u > ma20_u

    close_f = flat[-1]["close"]
    ma20_f = _ma_on(flat, as_of, 20)
    ma5_f = _ma_on(flat, as_of, 5)
    assert not (close_f > ma20_f and ma5_f > ma20_f)

    def fake_klines(code, *, market=None, as_of_date, lookback_days=60):
        if code == "600036":
            return uptrend
        if code == "000001":
            return flat
        return []

    with patch("src.core.stock_simulator_stock_picker._sector_constituents") as mock_sec:
        mock_sec.return_value = ("测试", None, MOCK_CONSTITUENTS[:2], "测试")
        with patch("src.core.stock_simulator_stock_picker._klines_as_of", side_effect=fake_klines):
            result = pick_stocks(
                universe="sector",
                classify="concept",
                symbol="测试",
                limit=2,
                strategy="dual_ma",
                as_of_date=as_of,
                momentum_lookback=5,
            )

    assert result["ok"] is True
    assert result["strategy"] == "dual_ma"
    codes = [i["code"] for i in result["items"]]
    assert "600036" in codes
    assert "000001" not in codes


def test_pick_stocks_sector_rotation_merges_sectors():
    """行业轮动：合并 Top 板块成分并按动量排序。"""
    klines = _mock_kline_series("600036", daily_return=0.008, n=40)
    as_of = klines[-1]["date"]
    sectors = [
        {"name": "人工智能", "code": "BK001", "change_pct": 3.0},
        {"name": "新能源", "code": "BK002", "change_pct": 2.0},
    ]

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        svc = mock_mkt.return_value
        svc.get_sectors_trading.return_value = {"ok": True, "items": sectors}
        svc.get_sector_detail.side_effect = lambda name, **kw: {
            "ok": True,
            "symbol": name,
            "constituents": [{"code": "600036", "name": "招商银行", "change_pct": 2.0}],
        }
        with patch("src.core.stock_simulator_stock_picker._klines_as_of", return_value=klines):
            result = pick_stocks(
                universe="sector",
                classify="concept",
                symbol="",
                limit=1,
                strategy="sector_rotation",
                as_of_date=as_of,
                momentum_lookback=5,
            )

    assert result["ok"] is True
    assert result["strategy"] == "sector_rotation"
    assert result["items"][0]["code"] == "600036"
    assert "行业轮动" in result["items"][0]["reason"]


def test_pick_stocks_garp_degrades_to_momentum_when_no_fundamentals():
    """GARP 无基本面数据时降级为动量选股并标记 degraded。"""
    klines = _mock_kline_series("600036", n=40)
    as_of = klines[-1]["date"]

    with patch("src.core.stock_simulator_stock_picker._sector_constituents") as mock_sec:
        mock_sec.return_value = ("测试", None, MOCK_CONSTITUENTS[:2], "测试")
        with patch("src.core.stock_simulator_stock_picker._fetch_fundamentals_pit", return_value=None):
            with patch("src.core.stock_simulator_stock_picker._klines_as_of", return_value=klines):
                result = pick_stocks(
                    universe="sector",
                    classify="concept",
                    symbol="测试",
                    limit=1,
                    strategy="garp",
                    as_of_date=as_of,
                    momentum_lookback=5,
                )

    assert result["ok"] is True
    assert result.get("degraded") is True
    assert result["items"]


def test_historical_volatility_low_for_flat_series():
    flat = _mock_kline_series(daily_return=0.0, n=30)
    as_of = flat[-1]["date"]
    vol = _historical_volatility(flat, as_of, n=20)
    assert vol is not None
    assert vol < 1.0


def test_backtest_auto_pick_api(bt_dir, monkeypatch):
    bt = bt_dir
    monkeypatch.setattr("src.core.stock_simulator_backtest.BACKTEST_DIR", bt)
    monkeypatch.setattr("src.core.stock_simulator_backtest._service", None)

    client = TestClient(stock_app)
    items = _mock_kline_items(80)
    start = items[0]["date"]
    end = items[-1]["date"]

    pick_payload = {
        "ok": True,
        "items": [{"code": "600036", "name": "招商银行", "market": "SH", "reason": "test"}],
        "strategy": "rules",
    }

    from src.core.stock_simulator_backtest import get_stock_simulator_backtest_service

    svc = get_stock_simulator_backtest_service()

    with patch("src.core.stock_simulator_stock_picker.pick_stocks", return_value=pick_payload):
        with patch.object(svc, "_load_klines", return_value=items):
            with patch(
                "src.core.stock_simulator_backtest.get_stock_simulator_backtest_service",
                return_value=svc,
            ):
                r = client.post(
                    "/api/stock/simulator/backtest",
                    json={
                        "auto_pick": True,
                        "pick_strategy": "rules",
                        "universe": {"classify": "concept", "symbol": "人工智能", "limit": 1},
                        "start_date": start,
                        "end_date": end,
                        "strategy": "rules",
                    },
                )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data.get("picked_stocks")
    assert data["picked_stocks"][0]["code"] == "600036"
    assert len(data.get("equity_curve") or []) > 0


@pytest.fixture
def bt_dir(tmp_path: Path, monkeypatch):
    bt = tmp_path / "backtests"
    bt.mkdir()
    return bt


def test_list_pick_strategies_includes_classic():
    data = list_pick_strategies()
    assert data["ok"] is True
    ids = {s["id"] for s in data["strategies"]}
    assert "value" in ids
    assert "sector_rotation" in ids
    assert "low_volatility" in ids
    assert data.get("disclaimer")


def test_pick_stocks_value_with_mock_fundamentals():
    mock_detail = {
        "ok": True,
        "symbol": "银行",
        "constituents": [
            {"code": "000001", "name": "平安银行", "market": "SZ"},
            {"code": "600036", "name": "招商银行", "market": "SH"},
        ],
    }
    fin_map = {
        "000001": {"pe": 8.0, "pb": 0.9, "roe": 12.0, "rev_growth": 5.0, "profit_growth": 6.0, "growth": 6.0, "report_date": "2024-12-31"},
        "600036": {"pe": 30.0, "pb": 1.2, "roe": 15.0, "rev_growth": 8.0, "profit_growth": 10.0, "growth": 10.0, "report_date": "2024-12-31"},
    }

    def fake_fin(code, *, market=None, as_of_date=None, cache=None):
        return fin_map.get(code)

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        mock_mkt.return_value.get_sector_detail.return_value = mock_detail
        with patch("src.core.stock_simulator_stock_picker._fetch_fundamentals_pit", side_effect=fake_fin):
            result = pick_stocks(
                universe="sector",
                classify="industry",
                symbol="银行",
                limit=2,
                strategy="value",
                as_of_date="2025-06-01",
                constraints={"pe_max": 20, "pb_max": 3},
            )

    assert result["ok"] is True
    assert result["strategy"] == "value"
    assert result["items"][0]["code"] == "000001"
    assert "PE=" in result["items"][0]["reason"]


def test_pick_stocks_dual_ma_pit():
    mock_detail = {
        "ok": True,
        "symbol": "测试",
        "constituents": [{"code": "600036", "name": "招商银行", "market": "SH"}],
    }
    klines = _mock_kline_items(80)

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        mock_mkt.return_value.get_sector_detail.return_value = mock_detail
        with patch("src.core.stock_simulator_stock_picker._klines_as_of", return_value=klines):
            with patch("src.core.stock_simulator_stock_picker._return_over_n_days", return_value=3.5):
                result = pick_stocks(
                    universe="sector",
                    classify="concept",
                    symbol="测试",
                    limit=1,
                    strategy="dual_ma",
                    as_of_date=klines[-1]["date"],
                    momentum_lookback=5,
                )

    assert result["ok"] is True
    assert result["strategy"] == "dual_ma"
    assert result["items"][0]["code"] == "600036"


def test_api_simulator_strategies():
    client = TestClient(stock_app)
    r = client.get("/api/stock/simulator/strategies")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert any(s["id"] == "garp" for s in data["strategies"])


def test_pick_stocks_garp_fallback_watchlist(tmp_path, monkeypatch):
    wl = tmp_path / "watchlist.yaml"
    wl.write_text(
        "stocks:\n  - code: '600519'\n    name: 贵州茅台\n    market: SH\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.core.stock_simulator_stock_picker.WATCHLIST_PATH", wl)

    with patch("src.core.stock_market_data.get_stock_market_data") as mock_mkt:
        mock_mkt.return_value.get_sector_detail.return_value = {
            "ok": True,
            "symbol": "空",
            "constituents": [{"code": "999999", "name": "无效", "market": "SH"}],
        }
        with patch("src.core.stock_simulator_stock_picker._fetch_fundamentals_pit", return_value=None):
            result = pick_stocks(
                universe="sector",
                classify="concept",
                symbol="空",
                limit=1,
                strategy="garp",
                as_of_date="2025-01-15",
            )

    assert result["ok"] is True
    assert result.get("fallback") is True
    assert result["items"][0]["code"] == "600519"
