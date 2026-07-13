"""股票模拟交易服务与 API 单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.stock_simulator_service import StockSimulatorService, get_stock_simulator_service
from src.web.stock_app import app as stock_app


@pytest.fixture
def sim_dir(tmp_path: Path, monkeypatch):
    sim_dir = tmp_path / "stock_simulator"
    sim_dir.mkdir()
    monkeypatch.setattr("src.core.stock_simulator_service.SIMULATOR_DIR", sim_dir)
    monkeypatch.setattr("src.core.stock_simulator_service._service", None)
    return sim_dir


@pytest.fixture
def sim_svc(sim_dir):
    return StockSimulatorService()


def test_buy_sell_pnl(sim_svc: StockSimulatorService):
    account_id = "test_pnl"
    sim_svc.init_account(account_id, initial_cash=100_000, reset=True)

    with patch.object(sim_svc, "_get_price", return_value=(10.0, "mock", None)):
        buy = sim_svc.buy(account_id=account_id, code="000001", market="SZ", quantity=1000, price=10.0)
        assert buy["ok"] is True
        assert buy["cash"] == 90_000.0
        pos = buy["positions"]
        assert len(pos) == 1
        assert pos[0]["quantity"] == 1000
        assert pos[0]["avg_cost"] == 10.0

    with patch.object(sim_svc, "_get_price", return_value=(12.0, "mock", None)):
        sell = sim_svc.sell(account_id=account_id, code="000001", market="SZ", quantity=500, price=12.0)
        assert sell["ok"] is True
        assert sell["trade"]["realized_pnl"] == 1000.0  # (12-10)*500
        assert sell["stats"]["realized_pnl"] == 1000.0
        assert sell["cash"] == 96_000.0  # 90000 + 6000
        assert len(sell["positions"]) == 1
        assert sell["positions"][0]["quantity"] == 500


def test_rules_save_and_load(sim_svc: StockSimulatorService):
    account_id = "test_rules"
    sim_svc.init_account(account_id, reset=True)
    updated = sim_svc.update_rules(
        account_id,
        {"stop_loss_pct": 8.0, "take_profit_pct": 15.0, "max_position_pct": 25.0, "ma_cross_enabled": True},
    )
    assert updated["rules"]["stop_loss_pct"] == 8.0
    assert updated["rules"]["take_profit_pct"] == 15.0
    assert updated["rules"]["max_position_pct"] == 25.0
    assert updated["rules"]["ma_cross_enabled"] is True

    reloaded = sim_svc.get_account(account_id, refresh_prices=False)
    assert reloaded["rules"]["stop_loss_pct"] == 8.0
    assert reloaded["rules"]["ma_cross_enabled"] is True


def test_max_position_rule(sim_svc: StockSimulatorService):
    account_id = "test_max_pos"
    sim_svc.init_account(account_id, initial_cash=100_000, reset=True)
    sim_svc.update_rules(account_id, {"max_position_pct": 20.0})

    with patch.object(sim_svc, "_get_price", return_value=(10.0, "mock", None)):
        result = sim_svc.buy(account_id=account_id, code="000001", quantity=2500, price=10.0)
        assert result["ok"] is False
        assert "仓位" in result["error"]

        ok = sim_svc.buy(account_id=account_id, code="000001", quantity=1500, price=10.0)
        assert ok["ok"] is True


def test_simulator_api(sim_dir):
    client = TestClient(stock_app)

    def mock_price(self, code, *, market=None, price_override=None):
        if price_override is not None and price_override > 0:
            return float(price_override), "manual", None
        return 11.5, "mock", None

    with patch.object(StockSimulatorService, "_get_price", mock_price):
        r = client.post(
            "/api/stock/simulator/account",
            json={"account_id": "api_test", "initial_cash": 200_000, "reset": True},
        )
        assert r.status_code == 200
        assert r.json()["cash"] == 200_000

        r = client.post(
            "/api/stock/simulator/buy",
            json={"account_id": "api_test", "code": "000001", "market": "SZ", "quantity": 100, "price": 11.5},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["positions"][0]["code"] == "000001"

        r = client.put(
            "/api/stock/simulator/rules",
            json={"account_id": "api_test", "stop_loss_pct": 6.0, "take_profit_pct": 12.0},
        )
        assert r.status_code == 200
        assert r.json()["rules"]["stop_loss_pct"] == 6.0

        r = client.post(
            "/api/stock/simulator/sell",
            json={"account_id": "api_test", "code": "000001", "market": "SZ", "quantity": 50, "price": 12.0},
        )
        assert r.status_code == 200
        assert r.json()["trade"]["realized_pnl"] == 25.0  # (12-11.5)*50

        r = client.get("/api/stock/simulator/trades", params={"account_id": "api_test"})
        assert r.status_code == 200
        assert r.json()["count"] >= 2

    r = client.get("/simulator")
    assert r.status_code == 200
    assert "模拟交易" in r.text
