"""模拟交易自动选股：Agent / 板块动量 / 全市场动量 / watchlist 多层 fallback。

时点正确性（Point-in-Time）：
- 传入 as_of_date 时，仅使用 date <= as_of_date 的 K 线计算动量/排名；
- 禁止用实时 spot、当日板块涨幅或回测 end_date 之后的数据做决策。
- 未传 as_of_date 时为「实时预览」，响应会标明 as_of_date=null。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.stock_market_utils import resolve_stock

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
WATCHLIST_PATH = ROOT / "data" / "stock_simulator" / "watchlist.yaml"

PICK_DISCLAIMER = "自动选股结果基于历史行情与规则/模型推断，不构成投资建议。"
CLASSIC_STRATEGY_DISCLAIMER = "经典策略为简化规则演示，非专业量化产品，不构成投资建议。"
DEFAULT_MOMENTUM_LOOKBACK = 5
DEFAULT_PE_MAX = 25.0
DEFAULT_PB_MAX = 3.0
DEFAULT_ROE_MIN = 10.0
DEFAULT_PEG_MAX = 1.5

# 可选选股策略（rules 与 sector_momentum 等价展示）
STRATEGY_CATALOG: list[dict[str, Any]] = [
    {
        "id": "rules",
        "name": "板块动量（规则）",
        "desc": "在指定板块内按 N 日涨幅排序，取 Top 标的",
        "group": "basic",
        "params": ["momentum_lookback"],
    },
    {
        "id": "sector_momentum",
        "name": "板块涨幅 Top",
        "desc": "同板块动量规则，强调板块内涨幅排名",
        "group": "basic",
        "params": ["momentum_lookback"],
    },
    {
        "id": "agent",
        "name": "Agent 智能选股",
        "desc": "LLM 结合板块与 K 线推断；不可用时降级板块动量",
        "group": "basic",
        "params": ["momentum_lookback"],
    },
    {
        "id": "value",
        "name": "价值投资",
        "desc": "低 PE/PB 筛选，优先估值更低的标的",
        "group": "classic",
        "params": ["pe_max", "pb_max"],
    },
    {
        "id": "growth",
        "name": "成长策略",
        "desc": "按营收/净利润同比增速排序，偏好高成长",
        "group": "classic",
        "params": [],
    },
    {
        "id": "garp",
        "name": "GARP",
        "desc": "PEG 合理区间（PE/增速），兼顾估值与成长",
        "group": "classic",
        "params": ["pe_max", "peg_max"],
    },
    {
        "id": "momentum",
        "name": "动量策略",
        "desc": "全市场或候选池按 N 日动量排序（可配 5/20/60 日）",
        "group": "classic",
        "params": ["momentum_lookback"],
    },
    {
        "id": "dual_ma",
        "name": "双均线",
        "desc": "收盘价 > MA20 且 MA5 > MA20，再按动量排序",
        "group": "classic",
        "params": ["momentum_lookback"],
    },
    {
        "id": "quality",
        "name": "质量策略",
        "desc": "ROE 达标过滤，按 ROE 从高到低选取",
        "group": "classic",
        "params": ["roe_min"],
    },
    {
        "id": "sector_rotation",
        "name": "行业轮动",
        "desc": "选取动量 Top 板块，合并成分股后再排序",
        "group": "classic",
        "params": ["momentum_lookback"],
    },
    {
        "id": "low_volatility",
        "name": "低波动",
        "desc": "历史波动率升序，偏好波动更低的标的",
        "group": "classic",
        "params": ["momentum_lookback"],
    },
]

VALID_PICK_STRATEGIES = frozenset(s["id"] for s in STRATEGY_CATALOG)
CLASSIC_FUNDAMENTAL_STRATEGIES = frozenset({"value", "growth", "garp", "quality"})
CLASSIC_TECHNICAL_STRATEGIES = frozenset({"momentum", "dual_ma", "sector_rotation", "low_volatility"})
CLASSIC_STRATEGIES = CLASSIC_FUNDAMENTAL_STRATEGIES | CLASSIC_TECHNICAL_STRATEGIES


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s[:10], "%Y-%m-%d")


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _normalize_classify(classify: str, universe: str) -> str:
    u = (universe or "sector").strip().lower()
    c = (classify or "").strip().lower()
    if u in ("concept", "industry", "sw_l1", "sw_l2", "sw_l3"):
        return u
    if c:
        return c
    if u == "all":
        return "concept"
    return "concept"


def _item(code: str, name: str, *, market: str | None = None, reason: str = "") -> dict[str, Any]:
    code, mkt = resolve_stock(code, market)
    return {"code": code, "name": name or code, "market": mkt, "reason": reason}


def _load_watchlist(limit: int) -> list[dict[str, Any]]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = yaml.safe_load(WATCHLIST_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("读取 watchlist 失败: %s", exc)
        return []
    items: list[dict[str, Any]] = []
    for row in data.get("stocks") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        items.append(
            _item(
                code,
                str(row.get("name") or code),
                market=row.get("market"),
                reason=str(row.get("reason") or "watchlist 固定列表"),
            )
        )
        if len(items) >= limit:
            break
    return items


def _klines_as_of(
    code: str,
    *,
    market: str | None,
    as_of_date: str,
    lookback_days: int = 60,
) -> list[dict[str, Any]]:
    """读取 as_of_date 及之前 lookback 窗口内的 K 线（不含未来）。"""
    from src.core.stock_kline_service import get_stock_kline_service

    code, mkt = resolve_stock(code, market)
    svc = get_stock_kline_service()
    as_of = as_of_date[:10]
    as_of_dt = _parse_date(as_of)
    fetch_start = (as_of_dt - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")

    limit = max(120, lookback_days + 40)
    rows = svc.get_kline_history(code, market=mkt, period="daily", limit=limit)
    if len(rows) < 10:
        svc.fetch_and_save_kline(
            code,
            market=mkt,
            period="daily",
            start_date=fetch_start,
            end_date=as_of,
        )
        rows = svc.get_kline_history(code, market=mkt, period="daily", limit=limit)

    return [
        r
        for r in rows
        if str(r.get("date", ""))[:10] <= as_of and _safe_float(r.get("close")) > 0
    ]


def _historical_volatility(
    klines: list[dict[str, Any]],
    as_of_date: str,
    n: int = 20,
) -> float | None:
    """截至 as_of_date 的 N 日收盘价日收益率标准差（年化近似）。"""
    as_of = as_of_date[:10]
    closes = [
        _safe_float(r.get("close"))
        for r in klines
        if str(r.get("date", ""))[:10] <= as_of and _safe_float(r.get("close")) > 0
    ]
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1) :]
    rets: list[float] = []
    for i in range(1, len(window)):
        prev, curr = window[i - 1], window[i]
        if prev > 0:
            rets.append((curr - prev) / prev)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return var**0.5 * (252**0.5) * 100


def _return_over_n_days(
    klines: list[dict[str, Any]],
    as_of_date: str,
    n: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> float | None:
    """计算截至 as_of_date 的 N 日收益率（仅使用 date <= as_of_date 的收盘价）。"""
    as_of = as_of_date[:10]
    dated = [(str(r.get("date", ""))[:10], _safe_float(r.get("close"))) for r in klines]
    dated = [(d, c) for d, c in dated if d <= as_of and c > 0]
    if len(dated) < n + 1:
        return None
    end_close = dated[-1][1]
    start_close = dated[-1 - n][1]
    if start_close <= 0:
        return None
    return (end_close - start_close) / start_close * 100


def _rank_candidates_by_momentum_pit(
    candidates: list[dict[str, Any]],
    *,
    as_of_date: str,
    limit: int,
    lookback: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        klines = _klines_as_of(code, market=cand.get("market"), as_of_date=as_of_date, lookback_days=lookback + 30)
        ret = _return_over_n_days(klines, as_of_date, n=lookback)
        if ret is None:
            continue
        ranked.append((ret, cand))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for ret, cand in ranked[:limit]:
        code = str(cand.get("code") or "")
        name = str(cand.get("name") or code)
        reason = f"{lookback}日动量 Top（截至 {as_of_date[:10]} {ret:+.2f}%）"
        out.append(_item(code, name, market=cand.get("market"), reason=reason))
    return out


def _sort_constituents_by_momentum(constituents: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """实时模式：按当日涨跌幅排序（仅用于无 as_of_date 的预览）。"""
    ranked = sorted(
        constituents,
        key=lambda c: _safe_float(c.get("change_pct"), default=-999.0),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for row in ranked[:limit]:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        chg = row.get("change_pct")
        reason = f"板块成分涨幅 Top（{chg:+.2f}%）" if chg is not None else "板块成分股"
        out.append(_item(code, str(row.get("name") or code), reason=reason))
    return out


def _sector_constituents(
    *,
    classify: str,
    symbol: str,
    code: str | None,
    cons_limit: int,
) -> tuple[str, str | None, list[dict[str, Any]], str]:
    """获取板块成分股列表（名称/代码用于候选池，动量由 PIT K 线计算）。"""
    from src.core.stock_market_data import get_stock_market_data

    mkt = get_stock_market_data()
    sector_name = (symbol or "").strip()
    sector_code = (code or "").strip() or None
    auto_reason = ""

    if not sector_name and not sector_code:
        sectors = mkt.get_sectors_trading(classify=classify, limit=5, sort_by="change_pct", order="desc")
        items = sectors.get("items") or []
        if not items:
            return "", None, [], f"无法获取 {classify} 板块列表"
        lead = items[0]
        sector_name = str(lead.get("name") or "")
        sector_code = lead.get("code")
        auto_reason = f"自动选取涨幅首位板块「{sector_name}」"
    else:
        auto_reason = f"板块「{sector_name or sector_code}」"

    detail = mkt.get_sector_detail(
        sector_name,
        classify=classify,
        code=sector_code,
        cons_limit=min(cons_limit, 100),
    )
    if not detail.get("ok"):
        return sector_name, sector_code, [], detail.get("error") or "板块详情不可用"

    resolved_name = str(detail.get("symbol") or sector_name)
    resolved_code = detail.get("code") or sector_code
    constituents = detail.get("constituents") or []
    return resolved_name, resolved_code, constituents, auto_reason


def _pick_from_sector(
    *,
    classify: str,
    symbol: str,
    code: str | None,
    limit: int,
    as_of_date: str | None = None,
    momentum_lookback: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> dict[str, Any]:
    cons_limit = max(limit * 3, 30)
    sector_name, sector_code, constituents, auto_reason = _sector_constituents(
        classify=classify,
        symbol=symbol,
        code=code,
        cons_limit=cons_limit,
    )
    if not constituents and ("无法" in auto_reason or "不可用" in auto_reason):
        return {"ok": False, "error": auto_reason or "板块不可用", "classify": classify, "symbol": symbol}

    if as_of_date:
        candidates = [
            {"code": str(r.get("code") or ""), "name": str(r.get("name") or ""), "market": r.get("market")}
            for r in constituents
            if str(r.get("code") or "").strip()
        ]
        if not candidates:
            candidates = [{"code": c["code"], "name": c["name"], "market": c.get("market")} for c in _load_watchlist(cons_limit)]
        picked = _rank_candidates_by_momentum_pit(
            candidates,
            as_of_date=as_of_date,
            limit=limit,
            lookback=momentum_lookback,
        )
        source = "kline_momentum_pit"
        pit_note = f"截至 {as_of_date[:10]} 的历史 K 线动量"
    else:
        picked = _sort_constituents_by_momentum(constituents, limit)
        source = "sector_constituents_live"
        pit_note = "实时板块成分涨幅（预览，非回测时点）"

    if not picked and constituents:
        for row in constituents[:limit]:
            code_s = str(row.get("code") or "").strip()
            if code_s:
                picked.append(_item(code_s, str(row.get("name") or code_s), reason=f"{auto_reason} 成分股"))

    if not picked:
        return {"ok": False, "error": "板块无可用成分股", "classify": classify, "symbol": sector_name}

    for p in picked:
        if auto_reason and auto_reason not in p["reason"]:
            p["reason"] = f"{auto_reason}；{p['reason']}"

    return {
        "ok": True,
        "strategy": "sector_momentum",
        "classify": classify,
        "symbol": sector_name,
        "code": sector_code,
        "items": picked,
        "source": source,
        "as_of_date": as_of_date[:10] if as_of_date else None,
        "pit_note": pit_note,
    }


def _pick_market_momentum(
    limit: int,
    *,
    as_of_date: str | None = None,
    momentum_lookback: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> dict[str, Any]:
    if as_of_date:
        candidates = _load_watchlist(max(limit * 3, 20))
        if not candidates:
            return {"ok": False, "error": "watchlist 为空，无法进行历史动量选股", "degraded": True}
        picked = _rank_candidates_by_momentum_pit(
            candidates,
            as_of_date=as_of_date,
            limit=limit,
            lookback=momentum_lookback,
        )
        if picked:
            return {
                "ok": True,
                "strategy": "sector_momentum",
                "items": picked,
                "source": "watchlist_kline_momentum_pit",
                "as_of_date": as_of_date[:10],
                "pit_note": f"watchlist {momentum_lookback}日动量（截至 {as_of_date[:10]}）",
            }
        return {"ok": False, "error": "历史 K 线不足，无法计算动量", "as_of_date": as_of_date[:10]}

    try:
        from src.core.stock_market_data import _akshare_call, _akshare_import

        ak = _akshare_import()
        if ak is None:
            raise RuntimeError("akshare 不可用")
        df = _akshare_call(ak.stock_zh_a_spot_em, label="A股实时行情")
        rows = []
        for _, row in df.iterrows():
            code = str(row.get("代码") or "").strip()
            if not code or len(code) != 6:
                continue
            chg = _safe_float(row.get("涨跌幅"), default=-999.0)
            if chg <= -900:
                continue
            rows.append(
                {
                    "code": code,
                    "name": str(row.get("名称") or code),
                    "change_pct": chg,
                }
            )
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        picked = [
            _item(r["code"], r["name"], reason=f"全市场动量 Top（{r['change_pct']:+.2f}%）")
            for r in rows[:limit]
        ]
        if picked:
            return {
                "ok": True,
                "strategy": "sector_momentum",
                "items": picked,
                "source": "market_spot",
                "as_of_date": None,
                "pit_note": "实时全市场 spot（预览，非回测时点）",
            }
    except Exception as exc:
        logger.warning("全市场动量选股失败: %s", exc)

    watchlist = _load_watchlist(limit)
    if watchlist:
        return {
            "ok": True,
            "strategy": "rules",
            "items": watchlist,
            "source": "watchlist",
            "fallback": True,
            "fallback_reason": "全市场 spot 不可用，已降级 watchlist",
            "as_of_date": None,
        }
    return {"ok": False, "error": "全市场动量与 watchlist 均不可用", "degraded": True}


def _build_agent_kline_context(
    candidates: list[dict[str, Any]],
    *,
    as_of_date: str,
    lookback_days: int = 30,
    max_codes: int = 8,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    for cand in candidates[:max_codes]:
        code = str(cand.get("code") or "")
        if not code:
            continue
        klines = _klines_as_of(code, market=cand.get("market"), as_of_date=as_of_date, lookback_days=lookback_days + 10)
        recent = klines[-lookback_days:]
        ctx[code] = [
            {"date": r.get("date"), "close": r.get("close"), "change_pct": r.get("change_pct")}
            for r in recent
        ]
    return ctx


def _pick_agent(
    *,
    classify: str,
    symbol: str,
    code: str | None,
    limit: int,
    start_date: str | None,
    end_date: str | None,
    constraints: dict[str, Any] | None,
    as_of_date: str | None = None,
    momentum_lookback: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> dict[str, Any]:
    from src.core.llm_chat import create_chat_client

    llm = create_chat_client()
    if not llm.available:
        result = _pick_from_sector(
            classify=classify,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=as_of_date,
            momentum_lookback=momentum_lookback,
        )
        result["strategy"] = "rules"
        result["fallback"] = True
        result["fallback_reason"] = "LLM 未配置，已降级板块动量选股"
        return result

    pit_date = (as_of_date or start_date or "")[:10] or None

    if pit_date:
        candidates = _load_watchlist(max(limit * 3, 15))
        _, _, constituents, _ = _sector_constituents(
            classify=classify,
            symbol=symbol,
            code=code,
            cons_limit=max(limit * 2, 20),
        )
        for row in constituents:
            code_s = str(row.get("code") or "").strip()
            if code_s and not any(c["code"] == code_s for c in candidates):
                candidates.append({"code": code_s, "name": row.get("name"), "market": row.get("market")})
        kline_ctx = _build_agent_kline_context(candidates, as_of_date=pit_date, lookback_days=30)
        momentum_preview = _rank_candidates_by_momentum_pit(
            candidates,
            as_of_date=pit_date,
            limit=min(8, len(candidates)),
            lookback=momentum_lookback,
        )
        kline_json = json.dumps(kline_ctx, ensure_ascii=False)
        if len(kline_json) > 3000:
            kline_json = kline_json[:2960] + "…[K线已截断]"
        prompt = (
            f"你是 A 股选股助手。决策时点为 {pit_date}，只能依据该日及之前的历史 K 线。\n"
            f"选出 {limit} 只 A 股（6位代码）。\n"
            "仅返回 JSON：{\"picks\":[{\"code\":\"000001\",\"name\":\"平安银行\",\"reason\":\"...\"}]}\n"
            f"约束: {json.dumps(constraints or {}, ensure_ascii=False)}\n"
            f"板块分类: {classify}\n"
            f"动量参考（截至 {pit_date}）: {json.dumps(momentum_preview, ensure_ascii=False)}\n"
            f"历史K线窗口: {kline_json}\n"
            "禁止引用该时点之后的任何数据；优先流动性好、动量合理的标的；不要重复代码。"
        )
    else:
        from src.core.stock_market_data import get_stock_market_data

        mkt = get_stock_market_data()
        hot_sectors = mkt.get_sectors_trading(classify=classify, limit=10, sort_by="change_pct", order="desc")
        sector_ctx: dict[str, Any] = {}
        if symbol or code:
            detail = mkt.get_sector_detail(symbol, classify=classify, code=code, cons_limit=20)
            sector_ctx = {
                "symbol": detail.get("symbol") or symbol,
                "change_pct": (detail.get("spot") or {}).get("涨跌幅"),
                "constituents": [
                    {"code": c.get("code"), "name": c.get("name"), "change_pct": c.get("change_pct")}
                    for c in (detail.get("constituents") or [])[:15]
                ],
            }
        prompt = (
            f"你是 A 股选股助手。根据板块热点与动量，选出 {limit} 只 A 股（6位代码）。\n"
            "仅返回 JSON：{\"picks\":[{\"code\":\"000001\",\"name\":\"平安银行\",\"reason\":\"...\"}]}\n"
            f"约束: {json.dumps(constraints or {}, ensure_ascii=False)}\n"
            f"板块分类: {classify}\n"
            f"热点板块（实时预览）: {json.dumps(hot_sectors.get('items') or [], ensure_ascii=False)[:2000]}\n"
            f"指定板块: {json.dumps(sector_ctx, ensure_ascii=False)}\n"
            "优先流动性好、与热点板块相关的标的；不要重复代码。"
        )

    content, err = llm.chat([{"role": "user", "content": prompt}], timeout=45)
    if not content or err:
        result = _pick_from_sector(
            classify=classify,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=as_of_date,
            momentum_lookback=momentum_lookback,
        )
        result["strategy"] = "rules"
        result["fallback"] = True
        result["fallback_reason"] = f"Agent 选股失败({err or 'empty'})，已降级板块动量"
        return result

    try:
        text = content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        picks_raw = parsed.get("picks") or parsed.get("items") or []
    except json.JSONDecodeError:
        result = _pick_from_sector(
            classify=classify,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=as_of_date,
            momentum_lookback=momentum_lookback,
        )
        result["strategy"] = "rules"
        result["fallback"] = True
        result["fallback_reason"] = "Agent JSON 解析失败，已降级板块动量"
        return result

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in picks_raw:
        if not isinstance(row, dict):
            continue
        code_s = str(row.get("code") or "").strip()
        if not code_s or code_s in seen:
            continue
        seen.add(code_s)
        items.append(
            _item(
                code_s,
                str(row.get("name") or code_s),
                reason=str(row.get("reason") or "Agent 智能选股"),
            )
        )
        if len(items) >= limit:
            break

    if not items:
        result = _pick_from_sector(
            classify=classify,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=as_of_date,
            momentum_lookback=momentum_lookback,
        )
        result["strategy"] = "rules"
        result["fallback"] = True
        result["fallback_reason"] = "Agent 未返回有效代码，已降级板块动量"
        return result

    return {
        "ok": True,
        "strategy": "agent",
        "items": items,
        "source": "llm",
        "classify": classify,
        "symbol": symbol or None,
        "as_of_date": pit_date,
    }


def list_pick_strategies() -> dict[str, Any]:
    """返回可选选股策略及说明。"""
    return {
        "ok": True,
        "strategies": STRATEGY_CATALOG,
        "disclaimer": CLASSIC_STRATEGY_DISCLAIMER,
    }


def _constraint_float(constraints: dict[str, Any] | None, key: str, default: float) -> float:
    if not constraints:
        return default
    val = constraints.get(key)
    if val is None:
        return default
    return _safe_float(val, default=default)


def _secucode(code: str, market: str | None = None) -> str:
    code, mkt = resolve_stock(code, market)
    suffix = "SH" if mkt == "SH" or code.startswith("6") else ("BJ" if mkt == "BJ" or code.startswith(("4", "8")) else "SZ")
    return f"{code}.{suffix}"


def _report_date_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:10]


def _fetch_fundamentals_pit(
    code: str,
    *,
    market: str | None,
    as_of_date: str | None,
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    """截至 as_of_date 可用的最近财报指标 + PIT 收盘价推算 PE/PB。"""
    code, mkt = resolve_stock(code, market)
    cache_key = f"{mkt}:{code}:{as_of_date or 'live'}"
    if cache_key in cache:
        return cache[cache_key]

    from src.core.stock_market_data import _akshare_call, _akshare_import, is_akshare_available

    result: dict[str, Any] | None = None
    pit = (as_of_date or "")[:10] or None

    if is_akshare_available():
        ak = _akshare_import()
        if ak is not None:
            try:
                secu = _secucode(code, mkt)
                df = _akshare_call(
                    lambda: ak.stock_financial_analysis_indicator_em(symbol=secu, indicator="按报告期"),
                    label=f"财务指标({code})",
                )
                if df is not None and not df.empty:
                    row = None
                    for _, r in df.iterrows():
                        rd = _report_date_str(r.get("REPORT_DATE"))
                        if pit and rd and rd > pit:
                            continue
                        row = r
                        break
                    if row is not None:
                        eps = _safe_float(row.get("EPSJB"))
                        bps = _safe_float(row.get("BPS"))
                        roe = _safe_float(row.get("ROEJQ"))
                        rev_growth = _safe_float(row.get("TOTALOPERATEREVETZ"))
                        profit_growth = _safe_float(row.get("PARENTNETPROFITTZ"))
                        growth = profit_growth if profit_growth > 0 else rev_growth
                        close = None
                        pe = pb = None
                        if pit:
                            klines = _klines_as_of(code, market=mkt, as_of_date=pit, lookback_days=30)
                            if klines:
                                close = _safe_float(klines[-1].get("close"))
                        if close and close > 0:
                            if eps > 0:
                                pe = close / eps
                            if bps > 0:
                                pb = close / bps
                        peg = (pe / growth) if pe and pe > 0 and growth > 0 else None
                        result = {
                            "code": code,
                            "market": mkt,
                            "report_date": _report_date_str(row.get("REPORT_DATE")),
                            "eps": eps,
                            "bps": bps,
                            "roe": roe,
                            "rev_growth": rev_growth,
                            "profit_growth": profit_growth,
                            "growth": growth,
                            "close": close,
                            "pe": pe,
                            "pb": pb,
                            "peg": peg,
                        }
            except Exception as exc:
                logger.debug("财务指标获取失败 %s: %s", code, exc)

    if result is None and is_akshare_available():
        ak = _akshare_import()
        if ak is not None:
            try:
                df = _akshare_call(
                    lambda: ak.stock_individual_info_em(symbol=code),
                    label=f"个股信息({code})",
                )
                if df is not None and not df.empty:
                    info = {str(r.get("item") or ""): str(r.get("value") or "") for _, r in df.iterrows()}
                    pe = _safe_float(info.get("市盈率-动态") or info.get("市盈率"))
                    pb = _safe_float(info.get("市净率"))
                    result = {
                        "code": code,
                        "market": mkt,
                        "report_date": "",
                        "eps": 0.0,
                        "bps": 0.0,
                        "roe": 0.0,
                        "rev_growth": 0.0,
                        "profit_growth": 0.0,
                        "growth": 0.0,
                        "close": _safe_float(info.get("最新")),
                        "pe": pe if pe > 0 else None,
                        "pb": pb if pb > 0 else None,
                        "peg": None,
                        "live_preview": True,
                    }
            except Exception as exc:
                logger.debug("个股信息降级失败 %s: %s", code, exc)

    cache[cache_key] = result
    return result


def _close_on(klines: list[dict[str, Any]], as_of_date: str) -> float | None:
    as_of = as_of_date[:10]
    for row in reversed(klines):
        d = str(row.get("date", ""))[:10]
        if d <= as_of:
            close = _safe_float(row.get("close"))
            return close if close > 0 else None
    return None


def _ma_on(klines: list[dict[str, Any]], as_of_date: str, n: int = 20) -> float | None:
    as_of = as_of_date[:10]
    closes: list[float] = []
    for row in klines:
        d = str(row.get("date", ""))[:10]
        if d > as_of:
            continue
        close = _safe_float(row.get("close"))
        if close > 0:
            closes.append(close)
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _gather_candidates(
    *,
    universe: str,
    classify: str,
    symbol: str,
    code: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    """收集候选股票池。"""
    u = (universe or "sector").strip().lower()
    cons_limit = max(limit * 5, 30)

    if u == "all" or u == "watchlist":
        candidates = _load_watchlist(cons_limit)
        return candidates, "watchlist"

    sector_name, _, constituents, auto_reason = _sector_constituents(
        classify=classify,
        symbol=symbol,
        code=code,
        cons_limit=cons_limit,
    )
    candidates = [
        {"code": str(r.get("code") or ""), "name": str(r.get("name") or ""), "market": r.get("market")}
        for r in constituents
        if str(r.get("code") or "").strip()
    ]
    if not candidates:
        candidates = _load_watchlist(cons_limit)
        auto_reason = auto_reason or "watchlist"
    return candidates, auto_reason or f"板块「{sector_name or symbol}」"


def _finalize_pick_result(
    result: dict[str, Any],
    *,
    strategy: str,
    universe: str,
    limit: int,
    pit: str | None,
) -> dict[str, Any]:
    result["strategy"] = strategy
    result["universe"] = universe
    result["limit"] = limit
    result["disclaimer"] = PICK_DISCLAIMER
    result["classic_disclaimer"] = CLASSIC_STRATEGY_DISCLAIMER
    if "as_of_date" not in result:
        result["as_of_date"] = pit
    return result


def _pick_with_fallback(
    result: dict[str, Any],
    *,
    limit: int,
    universe: str,
    pit: str | None,
) -> dict[str, Any]:
    if result.get("ok"):
        return result
    watchlist = _load_watchlist(limit)
    if watchlist:
        return {
            "ok": True,
            "strategy": result.get("strategy") or "rules",
            "items": watchlist,
            "source": "watchlist",
            "fallback": True,
            "degraded": True,
            "fallback_reason": result.get("error") or "选股失败",
            "degraded_reason": result.get("degraded_reason") or result.get("error") or "选股失败，已降级 watchlist",
            "universe": universe,
            "as_of_date": pit,
        }
    result["disclaimer"] = PICK_DISCLAIMER
    result["as_of_date"] = pit
    result.setdefault("degraded", True)
    return result


def _pick_with_momentum_degraded_fallback(
    result: dict[str, Any],
    *,
    strat: str,
    candidates: list[dict[str, Any]],
    limit: int,
    as_of_date: str | None,
    momentum_lookback: int,
    pool_note: str,
) -> dict[str, Any]:
    """基本面/经典策略失败时，优先降级为 PIT 动量，再交给 watchlist fallback。"""
    if result.get("ok"):
        return result
    err = result.get("error") or "策略无可用标的"
    if candidates and as_of_date:
        picked = _rank_candidates_by_momentum_pit(
            candidates,
            as_of_date=as_of_date,
            limit=limit,
            lookback=momentum_lookback,
        )
        if picked:
            return {
                "ok": True,
                "strategy": strat,
                "items": picked,
                "source": "momentum_degraded",
                "pool_note": pool_note,
                "pit_note": f"降级 {momentum_lookback}日动量（截至 {as_of_date[:10]}）",
                "as_of_date": as_of_date[:10],
                "degraded": True,
                "degraded_reason": f"{err}；已降级为动量选股",
                "fallback": True,
                "fallback_reason": err,
            }
    result["degraded"] = True
    result.setdefault("degraded_reason", err)
    return result


def _pick_value(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str | None,
    constraints: dict[str, Any] | None,
    pool_note: str,
) -> dict[str, Any]:
    pe_max = _constraint_float(constraints, "pe_max", DEFAULT_PE_MAX)
    pb_max = _constraint_float(constraints, "pb_max", DEFAULT_PB_MAX)
    cache: dict[str, dict[str, Any] | None] = {}
    scored: list[tuple[float, dict[str, Any], str]] = []

    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        fin = _fetch_fundamentals_pit(code, market=cand.get("market"), as_of_date=as_of_date, cache=cache)
        if not fin:
            continue
        pe, pb = fin.get("pe"), fin.get("pb")
        if pe is None or pe <= 0 or pe > pe_max:
            continue
        if pb is not None and pb > 0 and pb > pb_max:
            continue
        score = pe + (pb or 0) * 0.5
        reason = f"价值筛选 PE={pe:.1f} PB={(pb or 0):.1f}（≤{pe_max}/{pb_max}）"
        if fin.get("report_date"):
            reason += f"，财报截至 {fin['report_date']}"
        scored.append((score, cand, reason))

    scored.sort(key=lambda x: x[0])
    items = [
        _item(str(c["code"]), str(c.get("name") or c["code"]), market=c.get("market"), reason=r)
        for _, c, r in scored[:limit]
    ]
    if not items:
        return {"ok": False, "error": f"无满足 PE≤{pe_max} PB≤{pb_max} 的标的", "strategy": "value", "degraded": True}
    pit_note = f"PIT 财报+K线（截至 {as_of_date[:10]}）" if as_of_date else "实时估值预览（非回测时点）"
    return {
        "ok": True,
        "strategy": "value",
        "items": items,
        "source": "fundamental_pe_pb",
        "pool_note": pool_note,
        "pit_note": pit_note,
        "as_of_date": as_of_date[:10] if as_of_date else None,
    }


def _pick_growth(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str | None,
    pool_note: str,
) -> dict[str, Any]:
    cache: dict[str, dict[str, Any] | None] = {}
    scored: list[tuple[float, dict[str, Any], str]] = []

    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        fin = _fetch_fundamentals_pit(code, market=cand.get("market"), as_of_date=as_of_date, cache=cache)
        if not fin:
            continue
        rev_g = _safe_float(fin.get("rev_growth"))
        profit_g = _safe_float(fin.get("profit_growth"))
        score = max(rev_g, profit_g)
        if score <= 0:
            continue
        reason = f"成长筛选 营收增速 {rev_g:+.1f}% 净利增速 {profit_g:+.1f}%"
        if fin.get("report_date"):
            reason += f"（财报 {fin['report_date']}）"
        scored.append((score, cand, reason))

    scored.sort(key=lambda x: x[0], reverse=True)
    items = [
        _item(str(c["code"]), str(c.get("name") or c["code"]), market=c.get("market"), reason=r)
        for _, c, r in scored[:limit]
    ]
    if not items:
        return {"ok": False, "error": "无正增长标的", "strategy": "growth", "degraded": True}
    pit_note = f"PIT 财报（截至 {as_of_date[:10]}）" if as_of_date else "最新财报预览"
    return {
        "ok": True,
        "strategy": "growth",
        "items": items,
        "source": "fundamental_growth",
        "pool_note": pool_note,
        "pit_note": pit_note,
        "as_of_date": as_of_date[:10] if as_of_date else None,
    }


def _pick_garp(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str | None,
    constraints: dict[str, Any] | None,
    pool_note: str,
) -> dict[str, Any]:
    pe_max = _constraint_float(constraints, "pe_max", DEFAULT_PE_MAX)
    peg_max = _constraint_float(constraints, "peg_max", DEFAULT_PEG_MAX)
    cache: dict[str, dict[str, Any] | None] = {}
    scored: list[tuple[float, dict[str, Any], str]] = []

    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        fin = _fetch_fundamentals_pit(code, market=cand.get("market"), as_of_date=as_of_date, cache=cache)
        if not fin:
            continue
        pe = fin.get("pe")
        growth = _safe_float(fin.get("growth"))
        if pe is None or pe <= 0 or pe > pe_max or growth <= 0:
            continue
        peg = pe / growth
        if peg <= 0 or peg > peg_max:
            continue
        scored.append((peg, cand, f"GARP PEG={peg:.2f} PE={pe:.1f} 增速={growth:.1f}%"))

    scored.sort(key=lambda x: x[0])
    items = [
        _item(str(c["code"]), str(c.get("name") or c["code"]), market=c.get("market"), reason=r)
        for _, c, r in scored[:limit]
    ]
    if not items:
        return {"ok": False, "error": f"无满足 PEG≤{peg_max} 的标的", "strategy": "garp", "degraded": True}
    return {
        "ok": True,
        "strategy": "garp",
        "items": items,
        "source": "fundamental_garp",
        "pool_note": pool_note,
        "pit_note": f"PIT 截至 {as_of_date[:10]}" if as_of_date else "最新财报预览",
        "as_of_date": as_of_date[:10] if as_of_date else None,
    }


def _pick_quality(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str | None,
    constraints: dict[str, Any] | None,
    pool_note: str,
) -> dict[str, Any]:
    roe_min = _constraint_float(constraints, "roe_min", DEFAULT_ROE_MIN)
    cache: dict[str, dict[str, Any] | None] = {}
    scored: list[tuple[float, dict[str, Any], str]] = []

    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        fin = _fetch_fundamentals_pit(code, market=cand.get("market"), as_of_date=as_of_date, cache=cache)
        if not fin:
            continue
        roe = _safe_float(fin.get("roe"))
        if roe < roe_min:
            continue
        scored.append((roe, cand, f"质量筛选 ROE={roe:.1f}%（≥{roe_min}%）"))

    scored.sort(key=lambda x: x[0], reverse=True)
    items = [
        _item(str(c["code"]), str(c.get("name") or c["code"]), market=c.get("market"), reason=r)
        for _, c, r in scored[:limit]
    ]
    if not items:
        return {"ok": False, "error": f"无 ROE≥{roe_min}% 的标的", "strategy": "quality", "degraded": True}
    return {
        "ok": True,
        "strategy": "quality",
        "items": items,
        "source": "fundamental_roe",
        "pool_note": pool_note,
        "pit_note": f"PIT 截至 {as_of_date[:10]}" if as_of_date else "最新财报预览",
        "as_of_date": as_of_date[:10] if as_of_date else None,
    }


def _pick_dual_ma(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str,
    momentum_lookback: int,
    pool_note: str,
) -> dict[str, Any]:
    filtered: list[dict[str, Any]] = []
    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        klines = _klines_as_of(code, market=cand.get("market"), as_of_date=as_of_date, lookback_days=60)
        close = _close_on(klines, as_of_date)
        ma5 = _ma_on(klines, as_of_date, 5)
        ma20 = _ma_on(klines, as_of_date, 20)
        if close and ma5 and ma20 and close > ma20 and ma5 > ma20:
            filtered.append(cand)

    if not filtered:
        return {"ok": False, "error": "无 close>MA20 且 MA5>MA20 的标的", "strategy": "dual_ma", "degraded": True}

    picked = _rank_candidates_by_momentum_pit(
        filtered,
        as_of_date=as_of_date,
        limit=limit,
        lookback=momentum_lookback,
    )
    for p in picked:
        p["reason"] = f"双均线 MA5>MA20 且 close>MA20；{p['reason']}"
    return {
        "ok": True,
        "strategy": "dual_ma",
        "items": picked,
        "source": "dual_ma_momentum_pit",
        "pool_note": pool_note,
        "pit_note": f"截至 {as_of_date[:10]} K线",
        "as_of_date": as_of_date[:10],
    }


def _pick_momentum_strategy(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str | None,
    momentum_lookback: int,
    pool_note: str,
) -> dict[str, Any]:
    if as_of_date:
        picked = _rank_candidates_by_momentum_pit(
            candidates,
            as_of_date=as_of_date,
            limit=limit,
            lookback=momentum_lookback,
        )
        if picked:
            return {
                "ok": True,
                "strategy": "momentum",
                "items": picked,
                "source": "kline_momentum_pit",
                "pool_note": pool_note,
                "pit_note": f"{momentum_lookback}日动量（截至 {as_of_date[:10]}）",
                "as_of_date": as_of_date[:10],
            }
        return {"ok": False, "error": "历史 K 线不足", "strategy": "momentum", "as_of_date": as_of_date[:10]}

    result = _pick_market_momentum(limit, as_of_date=None, momentum_lookback=momentum_lookback)
    result["strategy"] = "momentum"
    result["pool_note"] = pool_note
    return result


def _pick_sector_rotation(
    *,
    classify: str,
    symbol: str,
    code: str | None,
    limit: int,
    as_of_date: str | None,
    momentum_lookback: int,
) -> dict[str, Any]:
    from src.core.stock_market_data import get_stock_market_data

    mkt = get_stock_market_data()
    top_sectors: list[dict[str, Any]] = []

    if as_of_date:
        sectors_resp = mkt.get_sectors_trading(classify=classify, limit=8, sort_by="name", order="asc")
        sector_items = (sectors_resp.get("items") or [])[:5]
        sector_scores: list[tuple[float, str, str | None, list[dict]]] = []
        for sec in sector_items:
            sname = str(sec.get("name") or "")
            scode = sec.get("code")
            detail = mkt.get_sector_detail(sname, classify=classify, code=scode, cons_limit=8)
            cons = detail.get("constituents") or []
            if not cons:
                continue
            cands = [
                {"code": str(r.get("code") or ""), "name": str(r.get("name") or ""), "market": r.get("market")}
                for r in cons[:5]
                if str(r.get("code") or "").strip()
            ]
            rets = []
            for c in cands:
                klines = _klines_as_of(str(c["code"]), market=c.get("market"), as_of_date=as_of_date, lookback_days=momentum_lookback + 30)
                ret = _return_over_n_days(klines, as_of_date, n=momentum_lookback)
                if ret is not None:
                    rets.append(ret)
            if rets:
                sector_scores.append((sum(rets) / len(rets), sname, scode, cons))

        sector_scores.sort(key=lambda x: x[0], reverse=True)
        for avg_ret, sname, scode, cons in sector_scores[:3]:
            top_sectors.append({"name": sname, "code": scode, "avg_ret": avg_ret, "constituents": cons})
    else:
        sectors_resp = mkt.get_sectors_trading(classify=classify, limit=3, sort_by="change_pct", order="desc")
        for sec in sectors_resp.get("items") or []:
            sname = str(sec.get("name") or "")
            detail = mkt.get_sector_detail(sname, classify=classify, code=sec.get("code"), cons_limit=30)
            top_sectors.append(
                {
                    "name": sname,
                    "code": sec.get("code"),
                    "avg_ret": _safe_float(sec.get("change_pct")),
                    "constituents": detail.get("constituents") or [],
                }
            )

    if not top_sectors:
        return {"ok": False, "error": "无法获取轮动板块", "strategy": "sector_rotation", "degraded": True}

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    sector_names: list[str] = []
    for sec in top_sectors:
        sector_names.append(str(sec.get("name") or ""))
        for row in sec.get("constituents") or []:
            code_s = str(row.get("code") or "").strip()
            if code_s and code_s not in seen:
                seen.add(code_s)
                merged.append({"code": code_s, "name": row.get("name"), "market": row.get("market")})

    if not merged:
        return {"ok": False, "error": "轮动板块无成分股", "strategy": "sector_rotation"}

    if as_of_date:
        picked = _rank_candidates_by_momentum_pit(
            merged,
            as_of_date=as_of_date,
            limit=limit,
            lookback=momentum_lookback,
        )
        source = "sector_rotation_pit"
        pit_note = f"Top 板块 {', '.join(sector_names[:3])}（截至 {as_of_date[:10]}）"
    else:
        picked = _sort_constituents_by_momentum(merged, limit)
        source = "sector_rotation_live"
        pit_note = f"Top 板块 {', '.join(sector_names[:3])}（实时预览）"

    for p in picked:
        p["reason"] = f"行业轮动 · {p['reason']}"

    return {
        "ok": True,
        "strategy": "sector_rotation",
        "items": picked,
        "source": source,
        "classify": classify,
        "sectors": sector_names[:3],
        "pit_note": pit_note,
        "as_of_date": as_of_date[:10] if as_of_date else None,
    }


def _pick_low_volatility(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    as_of_date: str | None,
    lookback: int,
    pool_note: str,
) -> dict[str, Any]:
    if not as_of_date:
        return {"ok": False, "error": "低波动策略需指定 as_of_date", "strategy": "low_volatility", "degraded": True}

    scored: list[tuple[float, dict[str, Any], str]] = []
    for cand in candidates:
        code = str(cand.get("code") or "").strip()
        if not code:
            continue
        klines = _klines_as_of(code, market=cand.get("market"), as_of_date=as_of_date, lookback_days=lookback + 40)
        vol = _historical_volatility(klines, as_of_date, n=lookback)
        if vol is None:
            continue
        scored.append((vol, cand, f"低波动 {lookback}日年化波动 {vol:.2f}%"))

    if not scored:
        return {"ok": False, "error": "无法计算历史波动率", "strategy": "low_volatility", "degraded": True}

    scored.sort(key=lambda x: x[0])
    items = [
        _item(str(c["code"]), str(c.get("name") or c["code"]), market=c.get("market"), reason=r)
        for _, c, r in scored[:limit]
    ]
    return {
        "ok": True,
        "strategy": "low_volatility",
        "items": items,
        "source": "low_volatility_pit",
        "pool_note": pool_note,
        "pit_note": f"{lookback}日波动率升序（截至 {as_of_date[:10]}）",
        "as_of_date": as_of_date[:10],
    }


def _pick_classic_strategy(
    strat: str,
    *,
    universe: str,
    classify: str,
    symbol: str,
    code: str | None,
    limit: int,
    as_of_date: str | None,
    constraints: dict[str, Any] | None,
    momentum_lookback: int,
) -> dict[str, Any]:
    """经典策略路由。"""
    if strat == "sector_rotation":
        return _pick_sector_rotation(
            classify=classify,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=as_of_date,
            momentum_lookback=momentum_lookback,
        )

    candidates, pool_note = _gather_candidates(
        universe=universe,
        classify=classify,
        symbol=symbol,
        code=code,
        limit=limit,
    )
    if not candidates:
        return {"ok": False, "error": "候选池为空", "strategy": strat, "degraded": True}

    if strat == "value":
        return _pick_value(candidates, limit=limit, as_of_date=as_of_date, constraints=constraints, pool_note=pool_note)
    if strat == "growth":
        return _pick_growth(candidates, limit=limit, as_of_date=as_of_date, pool_note=pool_note)
    if strat == "garp":
        return _pick_garp(candidates, limit=limit, as_of_date=as_of_date, constraints=constraints, pool_note=pool_note)
    if strat == "quality":
        return _pick_quality(candidates, limit=limit, as_of_date=as_of_date, constraints=constraints, pool_note=pool_note)
    if strat == "momentum":
        return _pick_momentum_strategy(
            candidates,
            limit=limit,
            as_of_date=as_of_date,
            momentum_lookback=momentum_lookback,
            pool_note=pool_note,
        )
    if strat == "dual_ma":
        effective_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
        return _pick_dual_ma(
            candidates,
            limit=limit,
            as_of_date=effective_date,
            momentum_lookback=momentum_lookback,
            pool_note=pool_note,
        )
    if strat == "low_volatility":
        effective_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
        return _pick_low_volatility(
            candidates,
            limit=limit,
            as_of_date=effective_date,
            lookback=momentum_lookback,
            pool_note=pool_note,
        )

    return {"ok": False, "error": f"未知经典策略: {strat}", "strategy": strat}


def pick_stocks(
    *,
    universe: str = "sector",
    classify: str = "concept",
    symbol: str = "",
    code: str | None = None,
    limit: int = 5,
    strategy: str = "rules",
    start_date: str | None = None,
    end_date: str | None = None,
    as_of_date: str | None = None,
    constraints: dict[str, Any] | None = None,
    momentum_lookback: int = DEFAULT_MOMENTUM_LOOKBACK,
) -> dict[str, Any]:
    """
    自动选股入口。

    strategy: agent | rules | sector_momentum | value | growth | garp | momentum | dual_ma | quality | sector_rotation | low_volatility
    universe: all | sector | concept | sw_l1 | watchlist
    as_of_date: 决策时点 YYYY-MM-DD；回测/历史模式必传，仅使用 date <= as_of_date 的数据。
    constraints: 策略参数 pe_max / pb_max / roe_min / peg_max 等
    momentum_lookback: 动量/波动窗口，常用 5 / 20 / 60
    """
    limit = max(1, min(int(limit or 5), 20))
    strat = (strategy or "rules").strip().lower()
    if strat not in VALID_PICK_STRATEGIES:
        strat = "rules"
    u = (universe or "sector").strip().lower()
    clf = _normalize_classify(classify, u)
    pit = (as_of_date or start_date or "")[:10] or None
    lookback = max(5, min(int(momentum_lookback or DEFAULT_MOMENTUM_LOOKBACK), 60))

    if u == "watchlist" and strat not in CLASSIC_STRATEGIES:
        items = _load_watchlist(limit)
        if items:
            return _finalize_pick_result(
                {
                    "ok": True,
                    "items": items,
                    "source": "watchlist",
                },
                strategy=strat if strat in ("rules", "sector_momentum", "momentum") else "rules",
                universe=u,
                limit=limit,
                pit=pit,
            )
        return {"ok": False, "error": "watchlist 为空或不存在", "universe": u, "as_of_date": pit}

    if strat in CLASSIC_STRATEGIES:
        candidates, pool_note = _gather_candidates(
            universe=u,
            classify=clf,
            symbol=symbol,
            code=code,
            limit=limit,
        )
        result = _pick_classic_strategy(
            strat,
            universe=u,
            classify=clf,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=pit,
            constraints=constraints,
            momentum_lookback=lookback,
        )
        if not result.get("ok") and strat in CLASSIC_FUNDAMENTAL_STRATEGIES | {"dual_ma", "low_volatility", "momentum"}:
            result = _pick_with_momentum_degraded_fallback(
                result,
                strat=strat,
                candidates=candidates,
                limit=limit,
                as_of_date=pit,
                momentum_lookback=lookback,
                pool_note=pool_note,
            )
        elif not result.get("ok") and strat == "sector_rotation" and candidates and pit:
            result = _pick_with_momentum_degraded_fallback(
                result,
                strat=strat,
                candidates=candidates,
                limit=limit,
                as_of_date=pit,
                momentum_lookback=lookback,
                pool_note=pool_note,
            )
        result = _pick_with_fallback(result, limit=limit, universe=u, pit=pit)
        return _finalize_pick_result(result, strategy=strat, universe=u, limit=limit, pit=pit)

    if u == "all":
        if strat == "agent":
            agent_result = _pick_agent(
                classify=clf,
                symbol=symbol,
                code=code,
                limit=limit,
                start_date=start_date,
                end_date=end_date,
                constraints=constraints,
                as_of_date=pit,
                momentum_lookback=lookback,
            )
            if agent_result.get("ok"):
                return _finalize_pick_result(agent_result, strategy="agent", universe=u, limit=limit, pit=pit)
        result = _pick_market_momentum(limit, as_of_date=pit, momentum_lookback=lookback)
        result["strategy"] = "momentum" if strat == "momentum" else "sector_momentum"
        return _finalize_pick_result(result, strategy=result["strategy"], universe=u, limit=limit, pit=pit)

    if strat == "agent":
        result = _pick_agent(
            classify=clf,
            symbol=symbol,
            code=code,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
            constraints=constraints,
            as_of_date=pit,
            momentum_lookback=lookback,
        )
    else:
        result = _pick_from_sector(
            classify=clf,
            symbol=symbol,
            code=code,
            limit=limit,
            as_of_date=pit,
            momentum_lookback=lookback,
        )
        result["strategy"] = "rules" if strat == "rules" else "sector_momentum"

    result = _pick_with_fallback(result, limit=limit, universe=u, pit=pit)
    return _finalize_pick_result(result, strategy=result.get("strategy") or strat, universe=u, limit=limit, pit=pit)


def get_stock_simulator_stock_picker():
    return pick_stocks
