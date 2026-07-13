"""股票模拟回测引擎与 API 单元测试。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.stock_simulator_backtest import (
    StockSimulatorBacktestService,
    _compute_ma,
    _normalize_rules,
    get_stock_simulator_backtest_service,
)
from src.core.stock_simulator_stock_picker import (
    _return_over_n_days,
    pick_stocks,
)
from src.web.stock_app import app as stock_app


def _mock_kline_items(n: int = 130, start_price: float = 10.0) -> list[dict]:
    start = datetime(2025, 1, 2)
    items = []
    price = start_price
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        price *= 1 + (0.002 if i % 7 < 4 else -0.001)
        items.append(
            {
                "code": "000001",
                "market": "SZ",
                "name": "平安银行",
                "date": d.strftime("%Y-%m-%d"),
                "close": round(price, 4),
                "open": round(price * 0.99, 4),
                "high": round(price * 1.01, 4),
                "low": round(price * 0.98, 4),
                "change_pct": 0.2,
            }
        )
    return items


@pytest.fixture
def bt_dir(tmp_path: Path, monkeypatch):
    bt = tmp_path / "backtests"
    bt.mkdir()
    monkeypatch.setattr("src.core.stock_simulator_backtest.BACKTEST_DIR", bt)
    monkeypatch.setattr("src.core.stock_simulator_backtest._service", None)
    return bt


@pytest.fixture
def bt_svc(bt_dir):
    return StockSimulatorBacktestService()


def test_normalize_rules_ma_cross_alias():
    rules = _normalize_rules({"ma_cross": True, "stop_loss_pct": 8})
    assert rules["ma_cross_enabled"] is True
    assert rules["stop_loss_pct"] == 8


def test_compute_ma():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    ma5 = _compute_ma(vals, 3)
    assert ma5[0] != ma5[0]  # nan
    assert ma5[-1] == pytest.approx(4.0)


def test_rules_backtest_equity_curve(bt_svc: StockSimulatorBacktestService):
    items = _mock_kline_items(130)
    start = items[20]["date"]
    end = items[-1]["date"]

    with patch.object(bt_svc, "_load_klines", return_value=items):
        result = bt_svc.run_rules_backtest(
            codes=["000001"],
            start_date=start,
            end_date=end,
            initial_cash=1_000_000,
            rules={"ma_cross_enabled": True, "stop_loss_pct": 5, "take_profit_pct": 10, "max_position_pct": 30},
        )

    assert result["ok"] is True
    assert len(result["equity_curve"]) > 0
    assert "summary" in result
    assert result["summary"]["trading_days"] == len(result["equity_curve"])
    assert result["equity_curve"][0]["equity"] == pytest.approx(1_000_000, rel=0.01)


def test_backtest_api_000001(bt_dir):
    client = TestClient(stock_app)
    items = _mock_kline_items(120)
    start = items[0]["date"]
    end = items[-1]["date"]

    svc = get_stock_simulator_backtest_service()

    with patch.object(svc, "_load_klines", return_value=items):
        with patch(
            "src.core.stock_simulator_backtest.get_stock_simulator_backtest_service",
            return_value=svc,
        ):
            r = client.post(
                "/api/stock/simulator/backtest",
                json={
                    "codes": ["000001"],
                    "start_date": start,
                    "end_date": end,
                    "initial_cash": 1000000,
                    "strategy": "rules",
                    "rules": {"ma_cross_enabled": True, "stop_loss_pct": 5, "take_profit_pct": 10},
                },
            )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "equity_curve" in data
    assert "daily_log" in data
    assert len(data["daily_log"]) == len(data["equity_curve"])
    assert len(data["equity_curve"]) > 0
    assert "run_id" in data

    r2 = client.get(f"/api/stock/simulator/backtest/{data['run_id']}")
    assert r2.status_code == 200
    assert r2.json()["run_id"] == data["run_id"]


def test_agent_backtest_fallback_to_rules(bt_svc: StockSimulatorBacktestService):
    items = _mock_kline_items(60)
    start = items[0]["date"]
    end = items[-1]["date"]

    with patch.object(bt_svc, "_load_klines", return_value=items):
        with patch("src.core.llm_chat.create_chat_client") as mock_llm:
            mock_llm.return_value.available = False
            result = bt_svc.run_agent_backtest(
                codes=["000001"],
                start_date=start,
                end_date=end,
                initial_cash=500_000,
            )

    assert result["ok"] is True
    assert result.get("agent_fallback") is True
    assert len(result["equity_curve"]) > 0
    assert "daily_log" in result
    assert len(result["daily_log"]) == len(result["equity_curve"])


def test_daily_log_matches_trading_days(bt_svc: StockSimulatorBacktestService):
    items = _mock_kline_items(130)
    start = items[25]["date"]
    end = items[-1]["date"]

    with patch.object(bt_svc, "_load_klines", return_value=items):
        result = bt_svc.run_rules_backtest(
            codes=["000001"],
            start_date=start,
            end_date=end,
            initial_cash=1_000_000,
            rules={"ma_cross_enabled": True, "max_position_pct": 30},
        )

    assert result["ok"] is True
    assert len(result["daily_log"]) == len(result["equity_curve"])
    assert result["summary"]["trading_days"] == len(result["daily_log"])
    for day in result["daily_log"]:
        assert "date" in day
        assert "actions" in day
        assert "cash" in day
        assert "equity" in day
        assert "positions_snapshot" in day


def test_no_lookahead_momentum_signal(bt_svc: StockSimulatorBacktestService):
    """T 日动量信号不得使用 T+1 收盘价。"""
    closes = [10.0] * 25
    closes.append(20.0)
    sig_at_t = bt_svc._momentum_signal_at(closes, 24, lookback=20, threshold=3.0)
    assert sig_at_t is None

    sig_with_future = bt_svc._momentum_signal_at(closes, 25, lookback=20, threshold=3.0)
    assert sig_with_future == "buy"


def test_return_over_n_days_excludes_future():
    klines = [
        {"date": f"2025-01-{d:02d}", "close": c}
        for d, c in zip(range(2, 12), [10, 10, 10, 10, 10, 10, 10, 10, 10, 20], strict=False)
    ]
    ret_t = _return_over_n_days(klines, "2025-01-09", n=3)
    assert ret_t == pytest.approx(0.0)
    ret_future = _return_over_n_days(klines, "2025-01-11", n=3)
    assert ret_future is not None
    assert ret_future > 50


def test_pick_stocks_pit_uses_as_of_date():
    watchlist_items = [
        {"code": "000001", "name": "A", "market": "SZ"},
        {"code": "600519", "name": "B", "market": "SH"},
    ]

    def fake_klines(code, *, market, as_of_date, lookback_days=60):
        rows = []
        for i in range(1, 8):
            d = f"2025-06-{i:02d}"
            if code == "000001":
                close = 10.0 + i * 0.5
            else:
                close = 20.0 if i < 7 else 100.0
            if d <= as_of_date[:10]:
                rows.append({"date": d, "close": close})
        return rows

    with patch("src.core.stock_simulator_stock_picker._load_watchlist", return_value=watchlist_items):
        with patch("src.core.stock_simulator_stock_picker._klines_as_of", side_effect=fake_klines):
            result = pick_stocks(
                universe="all",
                limit=1,
                strategy="rules",
                as_of_date="2025-06-06",
            )

    assert result["ok"] is True
    assert result["as_of_date"] == "2025-06-06"
    assert result["items"][0]["code"] == "000001"
