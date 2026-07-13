"""股票模拟交易：账户、持仓、规则与收益统计（MongoDB + 本地 JSON 降级）。"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.stock_market_utils import resolve_stock

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SIMULATOR_DIR = ROOT / "data" / "stock_simulator"
ACCOUNT_COLLECTION = "stock_simulator_accounts"
TRADES_COLLECTION = "stock_simulator_trades"
DEFAULT_ACCOUNT_ID = "default"
DEFAULT_INITIAL_CASH = 1_000_000.0

DISCLAIMER = "本功能为模拟交易，不构成投资建议，实盘交易请自行承担风险。"

DEFAULT_RULES: dict[str, Any] = {
    "stop_loss_pct": 5.0,
    "take_profit_pct": 10.0,
    "max_position_pct": 30.0,
    "ma_cross_enabled": False,
    "change_pct_enabled": False,
    "change_pct_trigger": 3.0,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _position_key(code: str, market: str) -> str:
    return f"{market}:{code}"


class StockSimulatorService:
    """单用户模拟交易账户管理。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        SIMULATOR_DIR.mkdir(parents=True, exist_ok=True)

    def _mongo_coll(self, name: str):
        uri = self._settings.mongodb_uri
        if not uri:
            return None
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            coll = client[self._settings.mongodb_db][name]
            if name == ACCOUNT_COLLECTION:
                coll.create_index([("account_id", 1)], unique=True)
            elif name == TRADES_COLLECTION:
                coll.create_index([("account_id", 1), ("created_at", -1)])
                coll.create_index([("trade_id", 1)], unique=True)
            return coll
        except Exception as exc:
            logger.warning("MongoDB %s 不可用: %s", name, exc)
            return None

    def _account_json_path(self, account_id: str) -> Path:
        return SIMULATOR_DIR / f"account_{account_id}.json"

    def _trades_json_path(self, account_id: str) -> Path:
        return SIMULATOR_DIR / f"trades_{account_id}.jsonl"

    def _load_account(self, account_id: str) -> dict[str, Any] | None:
        coll = self._mongo_coll(ACCOUNT_COLLECTION)
        if coll is not None:
            doc = coll.find_one({"account_id": account_id})
            if doc:
                doc.pop("_id", None)
                return doc
        path = self._account_json_path(account_id)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("读取模拟账户 JSON 失败: %s", exc)
        return None

    def _save_account(self, account: dict[str, Any]) -> None:
        account_id = account["account_id"]
        account["updated_at"] = _now_iso()
        coll = self._mongo_coll(ACCOUNT_COLLECTION)
        if coll is not None:
            coll.replace_one({"account_id": account_id}, account, upsert=True)
        path = self._account_json_path(account_id)
        path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_trade(self, trade: dict[str, Any]) -> None:
        account_id = trade["account_id"]
        coll = self._mongo_coll(TRADES_COLLECTION)
        if coll is not None:
            coll.insert_one(dict(trade))
        path = self._trades_json_path(account_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trade, ensure_ascii=False) + "\n")

    def _load_trades(self, account_id: str, limit: int = 100) -> list[dict[str, Any]]:
        coll = self._mongo_coll(TRADES_COLLECTION)
        if coll is not None:
            cursor = coll.find({"account_id": account_id}).sort("created_at", -1).limit(limit)
            return [{k: v for k, v in doc.items() if k != "_id"} for doc in cursor]
        path = self._trades_json_path(account_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        trades: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(trades) >= limit:
                break
        return trades

    def _ensure_account(self, account_id: str, initial_cash: float | None = None) -> dict[str, Any]:
        account = self._load_account(account_id)
        if account:
            return account
        cash = float(initial_cash if initial_cash is not None else DEFAULT_INITIAL_CASH)
        now = _now_iso()
        account = {
            "account_id": account_id,
            "initial_cash": cash,
            "cash": cash,
            "rules": dict(DEFAULT_RULES),
            "positions": {},
            "stats": {"realized_pnl": 0.0, "trade_count": 0, "buy_count": 0, "sell_count": 0},
            "created_at": now,
            "updated_at": now,
        }
        self._save_account(account)
        return account

    def _get_price(
        self,
        code: str,
        *,
        market: str | None = None,
        price_override: float | None = None,
    ) -> tuple[float | None, str | None, float | None]:
        """返回 (price, source, change_pct)。"""
        if price_override is not None and price_override > 0:
            return float(price_override), "manual", None
        code, market = resolve_stock(code, market)
        try:
            from src.core.stock_company_service import get_stock_company_service

            detail = get_stock_company_service().get_stock_detail(code, market=market, balance_periods=1)
            quote = detail.get("quote") or {}
            price = quote.get("price")
            if price is not None and float(price) > 0:
                return float(price), quote.get("source") or "quote", quote.get("change_pct")
        except Exception as exc:
            logger.debug("模拟交易取价 quote 失败 %s: %s", code, exc)
        try:
            from src.core.stock_kline_service import get_stock_kline_service

            kline = get_stock_kline_service().get_kline(code, market=market, period="daily", limit=1, auto_fetch=True)
            items = kline.get("items") or []
            if items:
                last = items[-1]
                close = last.get("close")
                if close is not None and float(close) > 0:
                    return float(close), "kline", last.get("change_pct")
        except Exception as exc:
            logger.debug("模拟交易取价 K线失败 %s: %s", code, exc)
        return None, None, None

    def _resolve_name(self, code: str, market: str, name: str | None) -> str:
        if name and name.strip():
            return name.strip()
        try:
            from src.core.stock_search_service import get_stock_search_service

            result = get_stock_search_service().search(code, limit=5)
            for item in result.get("items") or []:
                if item.get("code") == code and (not market or item.get("market") == market):
                    return item.get("name") or code
        except Exception:
            pass
        return code

    def _portfolio_value(self, account: dict[str, Any], prices: dict[str, float]) -> float:
        total = float(account.get("cash") or 0)
        for key, pos in (account.get("positions") or {}).items():
            qty = float(pos.get("quantity") or 0)
            price = prices.get(key)
            if price is None:
                price = float(pos.get("avg_cost") or 0)
            total += qty * price
        return total

    def get_account(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        *,
        initial_cash: float | None = None,
        refresh_prices: bool = True,
    ) -> dict[str, Any]:
        account = self._ensure_account(account_id, initial_cash)
        positions_out: list[dict[str, Any]] = []
        prices: dict[str, float] = {}
        market_values: dict[str, float] = {}

        for key, pos in (account.get("positions") or {}).items():
            code = pos.get("code") or ""
            market = pos.get("market") or "SZ"
            qty = int(pos.get("quantity") or 0)
            avg_cost = float(pos.get("avg_cost") or 0)
            cost_basis = float(pos.get("cost_basis") or avg_cost * qty)
            current_price = avg_cost
            change_pct = None
            price_source = None
            if refresh_prices and qty > 0:
                p, src, chg = self._get_price(code, market=market)
                if p is not None:
                    current_price = p
                    price_source = src
                    change_pct = chg
            prices[key] = current_price
            market_value = current_price * qty
            market_values[key] = market_value
            unrealized = market_value - cost_basis
            unrealized_pct = (unrealized / cost_basis * 100) if cost_basis > 0 else 0.0
            positions_out.append(
                {
                    "key": key,
                    "code": code,
                    "name": pos.get("name") or code,
                    "market": market,
                    "quantity": qty,
                    "avg_cost": round(avg_cost, 4),
                    "cost_basis": round(cost_basis, 2),
                    "current_price": round(current_price, 4),
                    "price_source": price_source,
                    "change_pct": change_pct,
                    "market_value": round(market_value, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "unrealized_pnl_pct": round(unrealized_pct, 2),
                }
            )

        positions_out.sort(key=lambda x: x.get("market_value") or 0, reverse=True)
        total_assets = self._portfolio_value(account, prices)
        initial = float(account.get("initial_cash") or DEFAULT_INITIAL_CASH)
        cash = float(account.get("cash") or 0)
        position_value = total_assets - cash
        unrealized_total = sum(p["unrealized_pnl"] for p in positions_out)
        realized = float((account.get("stats") or {}).get("realized_pnl") or 0)
        total_pnl = realized + unrealized_total
        return_rate = (total_pnl / initial * 100) if initial > 0 else 0.0

        return {
            "ok": True,
            "account_id": account_id,
            "initial_cash": initial,
            "cash": round(cash, 2),
            "position_value": round(position_value, 2),
            "total_assets": round(total_assets, 2),
            "rules": account.get("rules") or dict(DEFAULT_RULES),
            "positions": positions_out,
            "stats": {
                **(account.get("stats") or {}),
                "unrealized_pnl": round(unrealized_total, 2),
                "total_pnl": round(total_pnl, 2),
                "return_rate_pct": round(return_rate, 2),
            },
            "created_at": account.get("created_at"),
            "updated_at": account.get("updated_at"),
            "disclaimer": DISCLAIMER,
        }

    def init_account(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        *,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        reset: bool = False,
    ) -> dict[str, Any]:
        if reset or self._load_account(account_id) is None:
            now = _now_iso()
            account = {
                "account_id": account_id,
                "initial_cash": float(initial_cash),
                "cash": float(initial_cash),
                "rules": dict(DEFAULT_RULES),
                "positions": {},
                "stats": {"realized_pnl": 0.0, "trade_count": 0, "buy_count": 0, "sell_count": 0},
                "created_at": now,
                "updated_at": now,
            }
            self._save_account(account)
            trades_path = self._trades_json_path(account_id)
            if reset and trades_path.exists():
                trades_path.unlink(missing_ok=True)
            coll = self._mongo_coll(TRADES_COLLECTION)
            if coll is not None and reset:
                coll.delete_many({"account_id": account_id})
        return self.get_account(account_id, initial_cash=initial_cash)

    def buy(
        self,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        code: str,
        market: str | None = None,
        name: str | None = None,
        quantity: int | None = None,
        amount: float | None = None,
        price: float | None = None,
    ) -> dict[str, Any]:
        code, market = resolve_stock(code, market)
        if not code:
            return {"ok": False, "error": "股票代码无效"}
        account = self._ensure_account(account_id)
        trade_price, price_source, _ = self._get_price(code, market=market, price_override=price)
        if trade_price is None or trade_price <= 0:
            return {"ok": False, "error": "无法获取有效成交价，请指定 price"}

        if quantity is None and amount is None:
            return {"ok": False, "error": "quantity 或 amount 至少填一项"}
        if quantity is None:
            quantity = int(float(amount) / trade_price)
        quantity = int(quantity)
        if quantity <= 0:
            return {"ok": False, "error": "买入数量须大于 0"}

        trade_amount = round(trade_price * quantity, 2)
        cash = float(account.get("cash") or 0)
        if trade_amount > cash + 1e-6:
            return {"ok": False, "error": f"可用资金不足（需要 {trade_amount:.2f}，可用 {cash:.2f}）"}

        rules = account.get("rules") or dict(DEFAULT_RULES)
        max_pct = float(rules.get("max_position_pct") or 100)
        if max_pct < 100:
            summary = self.get_account(account_id, refresh_prices=True)
            total_assets = float(summary.get("total_assets") or 0)
            if total_assets <= 0:
                total_assets = cash
            key = _position_key(code, market)
            existing = (account.get("positions") or {}).get(key) or {}
            existing_mv = float(existing.get("quantity") or 0) * trade_price
            new_mv = existing_mv + trade_amount
            if total_assets > 0 and new_mv / total_assets * 100 > max_pct + 1e-6:
                return {
                    "ok": False,
                    "error": f"超出最大单票仓位 {max_pct}%（新仓位占比 {new_mv / total_assets * 100:.1f}%）",
                }

        stock_name = self._resolve_name(code, market, name)
        key = _position_key(code, market)
        positions = dict(account.get("positions") or {})
        pos = positions.get(key) or {
            "code": code,
            "name": stock_name,
            "market": market,
            "quantity": 0,
            "avg_cost": 0.0,
            "cost_basis": 0.0,
        }
        old_qty = int(pos.get("quantity") or 0)
        old_basis = float(pos.get("cost_basis") or 0)
        new_qty = old_qty + quantity
        new_basis = old_basis + trade_amount
        pos["quantity"] = new_qty
        pos["cost_basis"] = round(new_basis, 2)
        pos["avg_cost"] = round(new_basis / new_qty, 4) if new_qty else 0.0
        pos["name"] = stock_name
        positions[key] = pos

        account["cash"] = round(cash - trade_amount, 2)
        account["positions"] = positions
        stats = dict(account.get("stats") or {})
        stats["trade_count"] = int(stats.get("trade_count") or 0) + 1
        stats["buy_count"] = int(stats.get("buy_count") or 0) + 1
        account["stats"] = stats
        self._save_account(account)

        trade = {
            "trade_id": str(uuid.uuid4()),
            "account_id": account_id,
            "side": "buy",
            "code": code,
            "name": stock_name,
            "market": market,
            "quantity": quantity,
            "price": round(trade_price, 4),
            "amount": trade_amount,
            "price_source": price_source,
            "realized_pnl": None,
            "created_at": _now_iso(),
        }
        self._append_trade(trade)

        result = self.get_account(account_id)
        result["trade"] = trade
        return result

    def sell(
        self,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        code: str,
        market: str | None = None,
        quantity: int,
        price: float | None = None,
    ) -> dict[str, Any]:
        code, market = resolve_stock(code, market)
        if not code:
            return {"ok": False, "error": "股票代码无效"}
        quantity = int(quantity)
        if quantity <= 0:
            return {"ok": False, "error": "卖出数量须大于 0"}

        account = self._ensure_account(account_id)
        key = _position_key(code, market)
        positions = dict(account.get("positions") or {})
        pos = positions.get(key)
        if not pos:
            return {"ok": False, "error": "无该股票持仓"}
        held = int(pos.get("quantity") or 0)
        if quantity > held:
            return {"ok": False, "error": f"持仓不足（持有 {held} 股，请求卖出 {quantity} 股）"}

        trade_price, price_source, _ = self._get_price(code, market=market, price_override=price)
        if trade_price is None or trade_price <= 0:
            return {"ok": False, "error": "无法获取有效成交价，请指定 price"}

        avg_cost = float(pos.get("avg_cost") or 0)
        trade_amount = round(trade_price * quantity, 2)
        realized = round((trade_price - avg_cost) * quantity, 2)

        cost_removed = round(avg_cost * quantity, 2)
        old_basis = float(pos.get("cost_basis") or 0)
        new_qty = held - quantity
        new_basis = round(max(old_basis - cost_removed, 0), 2)
        if new_qty <= 0:
            positions.pop(key, None)
        else:
            pos["quantity"] = new_qty
            pos["cost_basis"] = new_basis
            pos["avg_cost"] = round(new_basis / new_qty, 4) if new_qty else 0.0
            positions[key] = pos

        account["cash"] = round(float(account.get("cash") or 0) + trade_amount, 2)
        account["positions"] = positions
        stats = dict(account.get("stats") or {})
        stats["realized_pnl"] = round(float(stats.get("realized_pnl") or 0) + realized, 2)
        stats["trade_count"] = int(stats.get("trade_count") or 0) + 1
        stats["sell_count"] = int(stats.get("sell_count") or 0) + 1
        account["stats"] = stats
        self._save_account(account)

        trade = {
            "trade_id": str(uuid.uuid4()),
            "account_id": account_id,
            "side": "sell",
            "code": code,
            "name": pos.get("name") if pos else code,
            "market": market,
            "quantity": quantity,
            "price": round(trade_price, 4),
            "amount": trade_amount,
            "price_source": price_source,
            "realized_pnl": realized,
            "created_at": _now_iso(),
        }
        self._append_trade(trade)

        result = self.get_account(account_id)
        result["trade"] = trade
        return result

    def update_rules(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = self._ensure_account(account_id)
        current = dict(account.get("rules") or DEFAULT_RULES)
        if rules:
            for key in DEFAULT_RULES:
                if key in rules and rules[key] is not None:
                    current[key] = rules[key]
        account["rules"] = current
        self._save_account(account)
        result = self.get_account(account_id)
        result["message"] = "规则已更新"
        return result

    def list_trades(self, account_id: str = DEFAULT_ACCOUNT_ID, limit: int = 50) -> dict[str, Any]:
        self._ensure_account(account_id)
        trades = self._load_trades(account_id, limit=limit)
        return {"ok": True, "account_id": account_id, "items": trades, "count": len(trades), "disclaimer": DISCLAIMER}

    def evaluate(self, account_id: str = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
        account = self._ensure_account(account_id)
        rules = account.get("rules") or dict(DEFAULT_RULES)
        stop_loss = float(rules.get("stop_loss_pct") or 0)
        take_profit = float(rules.get("take_profit_pct") or 0)
        ma_enabled = bool(rules.get("ma_cross_enabled"))
        change_enabled = bool(rules.get("change_pct_enabled"))
        change_trigger = float(rules.get("change_pct_trigger") or 0)

        signals: list[dict[str, Any]] = []
        for key, pos in (account.get("positions") or {}).items():
            code = pos.get("code") or ""
            market = pos.get("market") or "SZ"
            qty = int(pos.get("quantity") or 0)
            if qty <= 0:
                continue
            avg_cost = float(pos.get("avg_cost") or 0)
            price, _, change_pct = self._get_price(code, market=market)
            if price is None or avg_cost <= 0:
                continue
            pnl_pct = (price - avg_cost) / avg_cost * 100
            base = {
                "code": code,
                "name": pos.get("name") or code,
                "market": market,
                "quantity": qty,
                "avg_cost": avg_cost,
                "current_price": price,
                "unrealized_pnl_pct": round(pnl_pct, 2),
            }
            if stop_loss > 0 and pnl_pct <= -stop_loss:
                signals.append({**base, "action": "sell", "reason": f"触发止损（浮动 {pnl_pct:.2f}% ≤ -{stop_loss}%）"})
            elif take_profit > 0 and pnl_pct >= take_profit:
                signals.append({**base, "action": "sell", "reason": f"触发止盈（浮动 {pnl_pct:.2f}% ≥ {take_profit}%）"})

            if change_enabled and change_pct is not None and abs(float(change_pct)) >= change_trigger:
                action = "sell" if float(change_pct) < 0 else "hold"
                signals.append(
                    {
                        **base,
                        "action": action,
                        "reason": f"涨跌幅触发（当日 {float(change_pct):.2f}%）",
                    }
                )

            if ma_enabled:
                ma_signal = self._ma_cross_signal(code, market)
                if ma_signal:
                    signals.append({**base, **ma_signal})

        return {
            "ok": True,
            "account_id": account_id,
            "rules": rules,
            "signals": signals,
            "signal_count": len(signals),
            "disclaimer": DISCLAIMER,
        }

    def _ma_cross_signal(self, code: str, market: str) -> dict[str, Any] | None:
        try:
            from src.core.stock_kline_service import get_stock_kline_service

            kline = get_stock_kline_service().get_kline(code, market=market, period="daily", limit=30, auto_fetch=True)
            items = kline.get("items") or []
            closes = [float(x.get("close") or 0) for x in items if x.get("close") is not None]
            if len(closes) < 21:
                return None

            def ma(data: list[float], n: int) -> list[float]:
                out: list[float] = []
                for i in range(len(data)):
                    if i + 1 < n:
                        out.append(0.0)
                    else:
                        out.append(sum(data[i + 1 - n : i + 1]) / n)
                return out

            ma5 = ma(closes, 5)
            ma20 = ma(closes, 20)
            if len(ma5) < 2 or len(ma20) < 2:
                return None
            prev5, curr5 = ma5[-2], ma5[-1]
            prev20, curr20 = ma20[-2], ma20[-1]
            if prev5 <= prev20 and curr5 > curr20:
                return {"action": "hold", "reason": "MA5 上穿 MA20（金叉），可关注加仓时机"}
            if prev5 >= prev20 and curr5 < curr20:
                return {"action": "sell", "reason": "MA5 下穿 MA20（死叉），建议减仓"}
        except Exception as exc:
            logger.debug("MA 信号计算失败 %s: %s", code, exc)
        return None


_service: Optional[StockSimulatorService] = None


def get_stock_simulator_service() -> StockSimulatorService:
    global _service
    if _service is None:
        _service = StockSimulatorService()
    return _service
