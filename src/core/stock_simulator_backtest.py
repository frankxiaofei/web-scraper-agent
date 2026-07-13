"""股票模拟交易历史回测：规则策略与 Agent 策略。"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.stock_market_utils import resolve_stock
from src.core.stock_simulator_service import DEFAULT_RULES, DISCLAIMER, _position_key

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
BACKTEST_DIR = ROOT / "data" / "stock_simulator" / "backtests"
BACKTEST_RUNS_COLLECTION = "stock_simulator_backtest_runs"
WARMUP_CALENDAR_DAYS = 90
DEFAULT_REBALANCE_INTERVAL = 20

BACKTEST_DISCLAIMER = (
    "回测结果基于历史数据模拟，不代表未来收益，不构成投资建议。"
    + DISCLAIMER
)

# Agent 回测 / 选股 prompt 上限
AGENT_MAX_KLINE_CODES = 10
AGENT_MAX_KLINE_DAYS = 30
AGENT_PROMPT_JSON_MAX_CHARS = 12_000

DEFAULT_INITIAL_CASH = 1_000_000.0


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s[:10], "%Y-%m-%d")


def _normalize_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_RULES)
    if not rules:
        return merged
    alias = {"ma_cross": "ma_cross_enabled", "momentum": "momentum_enabled"}
    for key, val in rules.items():
        if val is None:
            continue
        target = alias.get(key, key)
        if target in merged or target.endswith("_enabled") or target.endswith("_pct") or target.endswith("_trigger"):
            merged[target] = val
    return merged


def _compute_ma(values: list[float], n: int) -> list[float]:
    out: list[float] = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(float("nan"))
        else:
            out.append(sum(values[i + 1 - n : i + 1]) / n)
    return out


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


class StockSimulatorBacktestService:
    """按日 K 时间轴逐步模拟买卖，输出净值曲线与绩效指标。"""

    def __init__(self) -> None:
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, dict[str, Any]] = {}

    def _run_path(self, run_id: str) -> Path:
        return BACKTEST_DIR / f"{run_id}.json"

    def _mongo_coll(self):
        uri = get_settings().mongodb_uri
        if not uri:
            return None
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            coll = client[get_settings().mongodb_db][BACKTEST_RUNS_COLLECTION]
            coll.create_index([("run_id", 1)], unique=True)
            coll.create_index([("created_at", -1)])
            return coll
        except Exception as exc:
            logger.warning("MongoDB %s 不可用: %s", BACKTEST_RUNS_COLLECTION, exc)
            return None

    def save_run(self, run_id: str, result: dict[str, Any]) -> None:
        self._runs[run_id] = result
        path = self._run_path(run_id)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        coll = self._mongo_coll()
        if coll is not None:
            doc = dict(result)
            doc["run_id"] = run_id
            coll.replace_one({"run_id": run_id}, doc, upsert=True)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if run_id in self._runs:
            return self._runs[run_id]
        path = self._run_path(run_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._runs[run_id] = data
                return data
            except Exception as exc:
                logger.warning("读取回测结果失败 %s: %s", run_id, exc)
        return None

    def _load_klines(
        self,
        code: str,
        *,
        market: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        from src.core.stock_kline_service import get_stock_kline_service

        svc = get_stock_kline_service()
        start_dt = _parse_date(start_date)
        end_dt = _parse_date(end_date)
        buffer_days = max(WARMUP_CALENDAR_DAYS, (end_dt - start_dt).days + 60)
        limit = max(120, min(buffer_days + 30, 2000))
        fetch_start = (start_dt - timedelta(days=WARMUP_CALENDAR_DAYS)).strftime("%Y-%m-%d")

        rows = svc.get_kline_history(code, market=market, period="daily", limit=limit)
        if len(rows) < 30:
            svc.fetch_and_save_kline(code, market=market, period="daily", start_date=fetch_start, end_date=end_date)
            rows = svc.get_kline_history(code, market=market, period="daily", limit=limit)

        # 保留 end_date 之前全部 K 线（含 start 前 warmup），信号计算只用 idx 截断，禁止前视
        filtered = [
            r
            for r in rows
            if str(r.get("date", ""))[:10] <= end_date and _safe_float(r.get("close")) > 0
        ]
        return filtered

    def _build_timeline(self, series: dict[str, list[dict[str, Any]]], *, start_date: str, end_date: str) -> list[str]:
        dates: set[str] = set()
        for items in series.values():
            for row in items:
                d = str(row.get("date", ""))[:10]
                if d and start_date <= d <= end_date:
                    dates.add(d)
        return sorted(dates)

    def _positions_snapshot(
        self,
        positions: dict[str, dict],
        prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        snap: list[dict[str, Any]] = []
        for key, pos in positions.items():
            qty = int(pos.get("quantity") or 0)
            price = prices.get(key) or float(pos.get("avg_cost") or 0)
            snap.append(
                {
                    "code": pos.get("code") or "",
                    "name": pos.get("name") or "",
                    "market": pos.get("market") or "",
                    "quantity": qty,
                    "avg_cost": float(pos.get("avg_cost") or 0),
                    "price": round(price, 4),
                    "market_value": round(qty * price, 2),
                }
            )
        return snap

    def _finalize_daily_log(
        self,
        daily_log: list[dict[str, Any]],
        *,
        date: str,
        day_actions: list[dict[str, Any]],
        cash: float,
        equity: float,
        positions: dict[str, dict],
        prices: dict[str, float],
    ) -> None:
        if not day_actions:
            day_actions = [{"type": "hold", "code": "", "qty": 0, "price": 0.0, "reason": "无操作"}]
        daily_log.append(
            {
                "date": date,
                "actions": day_actions,
                "cash": round(cash, 2),
                "equity": round(equity, 2),
                "positions_snapshot": self._positions_snapshot(positions, prices),
            }
        )

    def _ensure_series_for_codes(
        self,
        series: dict[str, list[dict[str, Any]]],
        meta: dict[str, dict[str, str]],
        codes: list[str],
        *,
        start_date: str,
        end_date: str,
        markets: dict[str, str] | None,
    ) -> None:
        for raw_code in codes:
            code, market = resolve_stock(raw_code, (markets or {}).get(raw_code))
            key = _position_key(code, market)
            if key in series:
                continue
            items = self._load_klines(code, market=market, start_date=start_date, end_date=end_date)
            if items:
                series[key] = items
                meta[key] = {"code": code, "market": market, "name": items[-1].get("name") or code}

    def _rebalance_pick(
        self,
        *,
        date: str,
        pick_kwargs: dict[str, Any],
        day_actions: list[dict[str, Any]],
    ) -> list[str]:
        from src.core.stock_simulator_stock_picker import pick_stocks

        pick_kwargs = dict(pick_kwargs)
        pick_kwargs["as_of_date"] = date
        result = pick_stocks(**pick_kwargs)
        codes: list[str] = []
        for item in result.get("items") or []:
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            codes.append(code)
            day_actions.append(
                {
                    "type": "pick",
                    "code": code,
                    "qty": 0,
                    "price": 0.0,
                    "reason": item.get("reason") or result.get("strategy") or "auto_pick",
                }
            )
        return codes

    def _price_on(self, items: list[dict[str, Any]], date: str) -> float | None:
        for row in items:
            if str(row.get("date", ""))[:10] == date:
                close = _safe_float(row.get("close"))
                return close if close > 0 else None
        return None

    def _change_pct_on(self, items: list[dict[str, Any]], date: str, idx: int) -> float | None:
        row = items[idx] if 0 <= idx < len(items) else None
        if not row or str(row.get("date", ""))[:10] != date:
            return None
        chg = row.get("change_pct")
        if chg is not None:
            return _safe_float(chg, default=float("nan"))
        if idx > 0:
            prev = _safe_float(items[idx - 1].get("close"))
            curr = _safe_float(row.get("close"))
            if prev > 0:
                return (curr - prev) / prev * 100
        return None

    def _ma_cross_signal_at(self, closes: list[float], idx: int) -> str | None:
        if idx < 20 or len(closes) <= idx:
            return None
        window = closes[: idx + 1]
        ma5 = _compute_ma(window, 5)
        ma20 = _compute_ma(window, 20)
        if idx < 1:
            return None
        prev5, curr5 = ma5[idx - 1], ma5[idx]
        prev20, curr20 = ma20[idx - 1], ma20[idx]
        if math.isnan(prev5) or math.isnan(curr5) or math.isnan(prev20) or math.isnan(curr20):
            return None
        if prev5 <= prev20 and curr5 > curr20:
            return "buy"
        if prev5 >= prev20 and curr5 < curr20:
            return "sell"
        return None

    def _momentum_signal_at(self, closes: list[float], idx: int, *, lookback: int = 20, threshold: float = 0.0) -> str | None:
        if idx < lookback:
            return None
        start_price = closes[idx - lookback]
        curr = closes[idx]
        if start_price <= 0:
            return None
        ret = (curr - start_price) / start_price * 100
        if ret >= threshold:
            return "buy"
        if ret <= -threshold:
            return "sell"
        return None

    def _portfolio_value(self, cash: float, positions: dict[str, dict], prices: dict[str, float]) -> float:
        total = cash
        for key, pos in positions.items():
            qty = int(pos.get("quantity") or 0)
            price = prices.get(key) or float(pos.get("avg_cost") or 0)
            total += qty * price
        return total

    def _execute_buy(
        self,
        *,
        cash: float,
        positions: dict[str, dict],
        key: str,
        code: str,
        market: str,
        name: str,
        price: float,
        date: str,
        max_pct: float,
        total_assets: float,
        trades: list[dict],
        reason: str,
        day_actions: list[dict[str, Any]] | None = None,
    ) -> float:
        if price <= 0 or cash <= 0:
            return cash
        existing = positions.get(key) or {"quantity": 0, "cost_basis": 0.0, "avg_cost": 0.0}
        existing_mv = int(existing.get("quantity") or 0) * price
        max_mv = total_assets * max_pct / 100 if max_pct < 100 else cash + existing_mv
        budget = max(0.0, min(cash, max_mv - existing_mv))
        if budget < price * 100:
            return cash
        qty = int(budget / price / 100) * 100
        if qty <= 0:
            return cash
        amount = round(price * qty, 2)
        if amount > cash:
            return cash

        old_qty = int(existing.get("quantity") or 0)
        old_basis = float(existing.get("cost_basis") or 0)
        new_qty = old_qty + qty
        new_basis = old_basis + amount
        positions[key] = {
            "code": code,
            "name": name,
            "market": market,
            "quantity": new_qty,
            "cost_basis": round(new_basis, 2),
            "avg_cost": round(new_basis / new_qty, 4),
        }
        cash = round(cash - amount, 2)
        trades.append(
            {
                "date": date,
                "side": "buy",
                "code": code,
                "name": name,
                "market": market,
                "quantity": qty,
                "price": round(price, 4),
                "amount": amount,
                "reason": reason,
            }
        )
        if day_actions is not None:
            day_actions.append(
                {
                    "type": "buy",
                    "code": code,
                    "qty": qty,
                    "price": round(price, 4),
                    "reason": reason,
                }
            )
        return cash

    def _execute_sell(
        self,
        *,
        cash: float,
        positions: dict[str, dict],
        key: str,
        price: float,
        date: str,
        quantity: int | None,
        trades: list[dict],
        reason: str,
        day_actions: list[dict[str, Any]] | None = None,
    ) -> float:
        pos = positions.get(key)
        if not pos:
            return cash
        held = int(pos.get("quantity") or 0)
        if held <= 0:
            return cash
        qty = min(held, quantity if quantity else held)
        avg_cost = float(pos.get("avg_cost") or 0)
        amount = round(price * qty, 2)
        realized = round((price - avg_cost) * qty, 2)
        cost_removed = round(avg_cost * qty, 2)
        new_qty = held - qty
        if new_qty <= 0:
            positions.pop(key, None)
        else:
            old_basis = float(pos.get("cost_basis") or 0)
            pos["quantity"] = new_qty
            pos["cost_basis"] = round(max(old_basis - cost_removed, 0), 2)
            pos["avg_cost"] = round(pos["cost_basis"] / new_qty, 4) if new_qty else 0.0
            positions[key] = pos
        cash = round(cash + amount, 2)
        sell_code = pos.get("code") or ""
        trades.append(
            {
                "date": date,
                "side": "sell",
                "code": sell_code,
                "name": pos.get("name") or "",
                "market": pos.get("market") or "",
                "quantity": qty,
                "price": round(price, 4),
                "amount": amount,
                "realized_pnl": realized,
                "reason": reason,
            }
        )
        if day_actions is not None:
            day_actions.append(
                {
                    "type": "sell",
                    "code": sell_code,
                    "qty": qty,
                    "price": round(price, 4),
                    "reason": reason,
                }
            )
        return cash

    def _calc_summary(
        self,
        *,
        initial_cash: float,
        equity_curve: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        if not equity_curve:
            return {
                "initial_cash": initial_cash,
                "final_equity": initial_cash,
                "total_return_pct": 0.0,
                "annualized_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "sharpe_ratio": None,
                "trade_count": len(trades),
                "win_rate_pct": None,
                "start_date": start_date,
                "end_date": end_date,
                "trading_days": 0,
            }

        equities = [float(p["equity"]) for p in equity_curve]
        final = equities[-1]
        total_return = (final - initial_cash) / initial_cash * 100 if initial_cash > 0 else 0.0

        days = max(1, (_parse_date(end_date) - _parse_date(start_date)).days)
        years = days / 365.25
        ann = ((final / initial_cash) ** (1 / years) - 1) * 100 if initial_cash > 0 and years > 0 and final > 0 else total_return

        peak = equities[0]
        max_dd = 0.0
        for eq in equities:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)

        daily_returns: list[float] = []
        for i in range(1, len(equities)):
            if equities[i - 1] > 0:
                daily_returns.append((equities[i] - equities[i - 1]) / equities[i - 1])

        sharpe = None
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / len(daily_returns)
            var = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std = math.sqrt(var) if var > 0 else 0
            if std > 0:
                sharpe = round(mean_r / std * math.sqrt(252), 3)

        sell_trades = [t for t in trades if t.get("side") == "sell" and t.get("realized_pnl") is not None]
        wins = sum(1 for t in sell_trades if float(t.get("realized_pnl") or 0) > 0)
        win_rate = round(wins / len(sell_trades) * 100, 2) if sell_trades else None

        return {
            "initial_cash": round(initial_cash, 2),
            "final_equity": round(final, 2),
            "total_return_pct": round(total_return, 2),
            "annualized_return_pct": round(ann, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio": sharpe,
            "trade_count": len(trades),
            "win_rate_pct": win_rate,
            "start_date": start_date,
            "end_date": end_date,
            "trading_days": len(equity_curve),
        }

    def run_rules_backtest(
        self,
        *,
        codes: list[str],
        start_date: str,
        end_date: str,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        rules: dict[str, Any] | None = None,
        markets: dict[str, str] | None = None,
        auto_pick: bool = False,
        pick_kwargs: dict[str, Any] | None = None,
        rebalance_interval: int = DEFAULT_REBALANCE_INTERVAL,
    ) -> dict[str, Any]:
        rules = _normalize_rules(rules)
        stop_loss = float(rules.get("stop_loss_pct") or 0)
        take_profit = float(rules.get("take_profit_pct") or 0)
        max_pct = float(rules.get("max_position_pct") or 100)
        ma_enabled = bool(rules.get("ma_cross_enabled"))
        change_enabled = bool(rules.get("change_pct_enabled"))
        change_trigger = float(rules.get("change_pct_trigger") or 0)
        momentum_enabled = bool(rules.get("momentum_enabled", not ma_enabled))

        if not codes and not auto_pick:
            return {"ok": False, "error": "codes 不能为空"}

        start_date = start_date[:10]
        end_date = end_date[:10]
        series: dict[str, list[dict[str, Any]]] = {}
        meta: dict[str, dict[str, str]] = {}
        if codes:
            self._ensure_series_for_codes(
                series, meta, codes, start_date=start_date, end_date=end_date, markets=markets
            )
            if not series:
                return {"ok": False, "error": f"无 K 线数据: {codes} ({start_date} ~ {end_date})"}

        cash = float(initial_cash)
        positions: dict[str, dict] = {}
        trades: list[dict] = []
        equity_curve: list[dict] = []
        daily_log: list[dict] = []
        active_keys: set[str] | None = None
        pick_history: list[dict[str, Any]] = []
        rebalance = max(1, rebalance_interval)

        closes_by_key: dict[str, list[float]] = {}
        date_index: dict[str, dict[str, int]] = {}

        def _refresh_indices() -> None:
            closes_by_key.clear()
            date_index.clear()
            for key, items in series.items():
                closes_by_key[key] = [_safe_float(r.get("close")) for r in items]
                date_index[key] = {str(r.get("date", ""))[:10]: i for i, r in enumerate(items)}

        if auto_pick and pick_kwargs:
            init_actions: list[dict[str, Any]] = []
            picked_codes = self._rebalance_pick(
                date=start_date, pick_kwargs=pick_kwargs, day_actions=init_actions
            )
            pick_history.append({"date": start_date, "codes": picked_codes, "as_of_date": start_date})
            self._ensure_series_for_codes(
                series, meta, picked_codes, start_date=start_date, end_date=end_date, markets=markets
            )
            active_keys = set()
            for c in picked_codes:
                code, market = resolve_stock(c, (markets or {}).get(c))
                active_keys.add(_position_key(code, market))

        timeline = self._build_timeline(series, start_date=start_date, end_date=end_date)
        if not timeline:
            return {"ok": False, "error": "回测区间无交易日"}

        _refresh_indices()

        for day_i, date in enumerate(timeline):
            day_actions: list[dict[str, Any]] = []

            if auto_pick and pick_kwargs and day_i > 0 and day_i % rebalance == 0:
                picked_codes = self._rebalance_pick(date=date, pick_kwargs=pick_kwargs, day_actions=day_actions)
                pick_history.append({"date": date, "codes": picked_codes, "as_of_date": date})
                self._ensure_series_for_codes(
                    series, meta, picked_codes, start_date=start_date, end_date=end_date, markets=markets
                )
                _refresh_indices()
                active_keys = set()
                for c in picked_codes:
                    code, market = resolve_stock(c, (markets or {}).get(c))
                    active_keys.add(_position_key(code, market))

            prices: dict[str, float] = {}
            for key, items in series.items():
                p = self._price_on(items, date)
                if p is not None:
                    prices[key] = p

            total_assets = self._portfolio_value(cash, positions, prices)

            for key, price in prices.items():
                if active_keys is not None and key not in active_keys:
                    continue
                info = meta[key]
                pos = positions.get(key)
                idx = date_index[key].get(date)
                if idx is None:
                    continue
                closes = closes_by_key[key]

                if pos:
                    avg_cost = float(pos.get("avg_cost") or 0)
                    if avg_cost > 0:
                        pnl_pct = (price - avg_cost) / avg_cost * 100
                        if stop_loss > 0 and pnl_pct <= -stop_loss:
                            cash = self._execute_sell(
                                cash=cash,
                                positions=positions,
                                key=key,
                                price=price,
                                date=date,
                                quantity=None,
                                trades=trades,
                                reason=f"止损 {pnl_pct:.2f}%",
                                day_actions=day_actions,
                            )
                            total_assets = self._portfolio_value(cash, positions, prices)
                            continue
                        if take_profit > 0 and pnl_pct >= take_profit:
                            cash = self._execute_sell(
                                cash=cash,
                                positions=positions,
                                key=key,
                                price=price,
                                date=date,
                                quantity=None,
                                trades=trades,
                                reason=f"止盈 {pnl_pct:.2f}%",
                                day_actions=day_actions,
                            )
                            total_assets = self._portfolio_value(cash, positions, prices)
                            continue

                    if change_enabled:
                        chg = self._change_pct_on(series[key], date, idx)
                        if chg is not None and not math.isnan(chg) and chg <= -change_trigger:
                            cash = self._execute_sell(
                                cash=cash,
                                positions=positions,
                                key=key,
                                price=price,
                                date=date,
                                quantity=None,
                                trades=trades,
                                reason=f"涨跌幅触发 {chg:.2f}%",
                                day_actions=day_actions,
                            )
                            total_assets = self._portfolio_value(cash, positions, prices)
                            continue

                    if ma_enabled:
                        sig = self._ma_cross_signal_at(closes, idx)
                        if sig == "sell":
                            cash = self._execute_sell(
                                cash=cash,
                                positions=positions,
                                key=key,
                                price=price,
                                date=date,
                                quantity=None,
                                trades=trades,
                                reason="MA5 下穿 MA20",
                                day_actions=day_actions,
                            )
                            total_assets = self._portfolio_value(cash, positions, prices)

                if key not in positions:
                    buy_reason = None
                    if ma_enabled:
                        sig = self._ma_cross_signal_at(closes, idx)
                        if sig == "buy":
                            buy_reason = "MA5 上穿 MA20"
                    elif momentum_enabled:
                        sig = self._momentum_signal_at(closes, idx, lookback=20, threshold=3.0)
                        if sig == "buy":
                            buy_reason = "20日动量 > 3%"

                    if buy_reason:
                        cash = self._execute_buy(
                            cash=cash,
                            positions=positions,
                            key=key,
                            code=info["code"],
                            market=info["market"],
                            name=info["name"],
                            price=price,
                            date=date,
                            max_pct=max_pct,
                            total_assets=total_assets,
                            trades=trades,
                            reason=buy_reason,
                            day_actions=day_actions,
                        )
                        total_assets = self._portfolio_value(cash, positions, prices)

            equity = self._portfolio_value(cash, positions, prices)
            equity_curve.append(
                {
                    "date": date,
                    "equity": round(equity, 2),
                    "cash": round(cash, 2),
                    "position_value": round(equity - cash, 2),
                }
            )
            self._finalize_daily_log(
                daily_log,
                date=date,
                day_actions=day_actions,
                cash=cash,
                equity=equity,
                positions=positions,
                prices=prices,
            )

        if not equity_curve:
            return {"ok": False, "error": "回测区间无交易日"}

        summary = self._calc_summary(
            initial_cash=initial_cash,
            equity_curve=equity_curve,
            trades=trades,
            start_date=equity_curve[0]["date"],
            end_date=equity_curve[-1]["date"],
        )

        return {
            "ok": True,
            "strategy": "rules",
            "codes": [meta[k]["code"] for k in series],
            "rules": rules,
            "trades": trades,
            "equity_curve": equity_curve,
            "daily_log": daily_log,
            "pick_history": pick_history if auto_pick else [],
            "summary": summary,
            "disclaimer": BACKTEST_DISCLAIMER,
            "as_of_date": end_date,
        }

    def run_agent_backtest(
        self,
        *,
        codes: list[str],
        start_date: str,
        end_date: str,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        rules: dict[str, Any] | None = None,
        markets: dict[str, str] | None = None,
        agent_interval: int = 5,
        lookback_days: int = 30,
        timeout: int = 30,
        auto_pick: bool = False,
        pick_kwargs: dict[str, Any] | None = None,
        rebalance_interval: int = DEFAULT_REBALANCE_INTERVAL,
    ) -> dict[str, Any]:
        from src.core.llm_chat import create_chat_client

        llm = create_chat_client()
        if not llm.available:
            result = self.run_rules_backtest(
                codes=codes,
                start_date=start_date,
                end_date=end_date,
                initial_cash=initial_cash,
                rules=rules,
                markets=markets,
                auto_pick=auto_pick,
                pick_kwargs=pick_kwargs,
                rebalance_interval=rebalance_interval,
            )
            result["strategy"] = "rules"
            result["agent_fallback"] = True
            result["agent_message"] = "LLM 未配置，已降级为规则策略"
            return result

        rules = _normalize_rules(rules)
        max_pct = float(rules.get("max_position_pct") or 30)
        start_date = start_date[:10]
        end_date = end_date[:10]

        if not codes and not auto_pick:
            return {"ok": False, "error": "codes 不能为空"}

        series: dict[str, list[dict[str, Any]]] = {}
        meta: dict[str, dict[str, str]] = {}
        if codes:
            self._ensure_series_for_codes(
                series, meta, codes, start_date=start_date, end_date=end_date, markets=markets
            )

        pick_history: list[dict[str, Any]] = []
        rebalance = max(1, rebalance_interval)
        if auto_pick and pick_kwargs:
            init_actions: list[dict[str, Any]] = []
            picked_codes = self._rebalance_pick(
                date=start_date, pick_kwargs=pick_kwargs, day_actions=init_actions
            )
            pick_history.append({"date": start_date, "codes": picked_codes, "as_of_date": start_date})
            self._ensure_series_for_codes(
                series, meta, picked_codes, start_date=start_date, end_date=end_date, markets=markets
            )

        timeline = self._build_timeline(series, start_date=start_date, end_date=end_date)
        if not timeline:
            return {"ok": False, "error": "回测区间无交易日"}

        cash = float(initial_cash)
        positions: dict[str, dict] = {}
        trades: list[dict] = []
        equity_curve: list[dict] = []
        daily_log: list[dict] = []
        agent_decisions: list[dict] = []
        interval = max(1, agent_interval)

        for day_i, date in enumerate(timeline):
            day_actions: list[dict[str, Any]] = []

            if auto_pick and pick_kwargs and day_i > 0 and day_i % rebalance == 0:
                picked_codes = self._rebalance_pick(date=date, pick_kwargs=pick_kwargs, day_actions=day_actions)
                pick_history.append({"date": date, "codes": picked_codes, "as_of_date": date})
                self._ensure_series_for_codes(
                    series, meta, picked_codes, start_date=start_date, end_date=end_date, markets=markets
                )

            prices: dict[str, float] = {}
            for key, items in series.items():
                p = self._price_on(items, date)
                if p is not None:
                    prices[key] = p

            total_assets = self._portfolio_value(cash, positions, prices)

            if day_i % interval == 0:
                window_data: dict[str, Any] = {}
                for key, items in list(series.items())[:AGENT_MAX_KLINE_CODES]:
                    recent = [r for r in items if str(r.get("date", ""))[:10] <= date][-AGENT_MAX_KLINE_DAYS:]
                    window_data[meta[key]["code"]] = [
                        {"date": r.get("date"), "close": r.get("close"), "change_pct": r.get("change_pct")}
                        for r in recent
                    ]

                portfolio = [
                    {
                        "code": pos.get("code"),
                        "quantity": pos.get("quantity"),
                        "avg_cost": pos.get("avg_cost"),
                        "market_value": round(int(pos.get("quantity") or 0) * prices.get(k, 0), 2),
                    }
                    for k, pos in list(positions.items())[:AGENT_MAX_KLINE_CODES]
                ]

                kline_json = json.dumps(window_data, ensure_ascii=False)
                if len(kline_json) > AGENT_PROMPT_JSON_MAX_CHARS:
                    kline_json = kline_json[: AGENT_PROMPT_JSON_MAX_CHARS - 40] + "…[K线数据已截断]"

                prompt = (
                    "你是 A 股模拟交易助手。根据截至决策日的历史 K 线窗口与当前持仓，输出 JSON 决策。\n"
                    "禁止引用决策日之后的任何数据。\n"
                    "格式: {\"decisions\":[{\"code\":\"000001\",\"action\":\"buy|sell|hold\",\"weight_pct\":0-100}]}\n"
                    "weight_pct 表示可用资金或持仓的百分比。仅返回 JSON，不要其他文字。\n"
                    f"决策日(as_of_date): {date}\n现金: {cash:.2f}\n总资产: {total_assets:.2f}\n"
                    f"持仓: {json.dumps(portfolio, ensure_ascii=False)}\n"
                    f"K线(仅 date<={date}): {kline_json}"
                )
                content, err = llm.chat([{"role": "user", "content": prompt}], timeout=timeout)
                decisions: list[dict] = []
                if content and not err:
                    try:
                        text = content.strip()
                        if "```" in text:
                            text = text.split("```")[1]
                            if text.startswith("json"):
                                text = text[4:]
                        parsed = json.loads(text.strip())
                        decisions = parsed.get("decisions") or []
                    except json.JSONDecodeError:
                        logger.debug("Agent 决策 JSON 解析失败: %s", content[:200])

                agent_decisions.append({"date": date, "as_of_date": date, "decisions": decisions, "error": err})

                for dec in decisions:
                    raw_code = str(dec.get("code") or "")
                    action = str(dec.get("action") or "hold").lower()
                    weight = min(100.0, max(0.0, _safe_float(dec.get("weight_pct"), 0)))
                    code, market = resolve_stock(raw_code, None)
                    key = _position_key(code, market)
                    if key not in prices:
                        continue
                    price = prices[key]
                    info = meta.get(key) or {"code": code, "market": market, "name": code}

                    if action == "sell" and key in positions:
                        held = int(positions[key].get("quantity") or 0)
                        qty = int(held * weight / 100 / 100) * 100 or held
                        cash = self._execute_sell(
                            cash=cash,
                            positions=positions,
                            key=key,
                            price=price,
                            date=date,
                            quantity=qty,
                            trades=trades,
                            reason=f"Agent 卖出 {weight:.0f}%",
                            day_actions=day_actions,
                        )
                    elif action == "buy" and weight > 0:
                        cash = self._execute_buy(
                            cash=cash,
                            positions=positions,
                            key=key,
                            code=info["code"],
                            market=info["market"],
                            name=info["name"],
                            price=price,
                            date=date,
                            max_pct=max_pct,
                            total_assets=total_assets,
                            trades=trades,
                            reason=f"Agent 买入 {weight:.0f}%",
                            day_actions=day_actions,
                        )
                    elif action == "hold":
                        day_actions.append(
                            {
                                "type": "hold",
                                "code": code,
                                "qty": 0,
                                "price": round(price, 4),
                                "reason": f"Agent hold {weight:.0f}%",
                            }
                        )
                    total_assets = self._portfolio_value(cash, positions, prices)

            equity = self._portfolio_value(cash, positions, prices)
            equity_curve.append(
                {
                    "date": date,
                    "equity": round(equity, 2),
                    "cash": round(cash, 2),
                    "position_value": round(equity - cash, 2),
                }
            )
            self._finalize_daily_log(
                daily_log,
                date=date,
                day_actions=day_actions,
                cash=cash,
                equity=equity,
                positions=positions,
                prices=prices,
            )

        summary = self._calc_summary(
            initial_cash=initial_cash,
            equity_curve=equity_curve,
            trades=trades,
            start_date=timeline[0],
            end_date=timeline[-1],
        )

        return {
            "ok": True,
            "strategy": "agent",
            "codes": [meta[k]["code"] for k in series],
            "rules": rules,
            "trades": trades,
            "equity_curve": equity_curve,
            "daily_log": daily_log,
            "pick_history": pick_history if auto_pick else [],
            "summary": summary,
            "agent_decisions": agent_decisions,
            "agent_interval": interval,
            "disclaimer": BACKTEST_DISCLAIMER,
            "as_of_date": end_date,
        }

    def run_backtest(
        self,
        *,
        codes: list[str],
        start_date: str,
        end_date: str,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        strategy: str = "rules",
        rules: dict[str, Any] | None = None,
        markets: dict[str, str] | None = None,
        run_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        start_date = start_date[:10]
        end_date = end_date[:10]
        if _parse_date(start_date) > _parse_date(end_date):
            return {"ok": False, "error": "start_date 不能晚于 end_date", "run_id": run_id}

        auto_pick = bool(kwargs.pop("auto_pick", False))
        pick_kwargs = kwargs.pop("pick_kwargs", None)
        rebalance_interval = int(kwargs.pop("rebalance_interval", DEFAULT_REBALANCE_INTERVAL))
        bt_common = {
            "codes": codes,
            "start_date": start_date,
            "end_date": end_date,
            "initial_cash": initial_cash,
            "rules": rules,
            "markets": markets,
            "auto_pick": auto_pick,
            "pick_kwargs": pick_kwargs,
            "rebalance_interval": rebalance_interval,
        }

        if strategy == "agent":
            result = self.run_agent_backtest(**bt_common, **kwargs)
        else:
            result = self.run_rules_backtest(**bt_common)

        result["run_id"] = run_id
        result["created_at"] = _now_iso()
        if result.get("ok"):
            self.save_run(run_id, result)
        return result


_service: Optional[StockSimulatorBacktestService] = None


def get_stock_simulator_backtest_service() -> StockSimulatorBacktestService:
    global _service
    if _service is None:
        _service = StockSimulatorBacktestService()
    return _service
