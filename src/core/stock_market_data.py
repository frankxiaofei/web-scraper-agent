"""A 股行情数据封装：AKShare 板块/指数/个股基本信息，网络失败时优雅降级。"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 主要宽基指数名称（用于过滤 AKShare 返回）
_MAIN_INDEX_NAMES = frozenset(
    {
        "上证指数",
        "深证成指",
        "创业板指",
        "科创50",
        "沪深300",
        "中证500",
        "上证50",
        "北证50",
    }
)

_AKSHARE_RETRIES = 3
_AKSHARE_RETRY_DELAY = 2.0
_AKSHARE_CALL_TIMEOUT = 30.0
_SECTOR_AMOUNT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SECTOR_AMOUNT_CACHE_TTL = 120.0
_SECTOR_TRADING_CACHE: dict[str, tuple[float, tuple[list[dict[str, Any]], bool, Optional[str]]]] = {}
_SECTOR_TRADING_CACHE_TTL = 120.0
_SECTOR_INTRO_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "sector_intro_cache.json"
_SECTOR_INTRO_TTL = 86400.0

# 板块/工业分类体系（classify 参数）
SECTOR_CLASSIFICATIONS: dict[str, dict[str, str]] = {
    "industry": {"label": "行业板块", "source": "东方财富", "kind": "em_board"},
    "concept": {"label": "概念板块", "source": "东方财富", "kind": "em_board"},
    "sw_l1": {"label": "申万一级", "source": "申万宏源", "kind": "sw_index"},
    "sw_l2": {"label": "申万二级", "source": "申万宏源", "kind": "sw_index"},
    "sw_l3": {"label": "申万三级", "source": "乐咕乐股", "kind": "sw_static"},
}

_SW_REALTIME_LEVEL = {
    "sw_l1": "一级行业",
    "sw_l2": "二级行业",
}


def _akshare_call(
    fn: Callable[[], Any],
    *,
    label: str,
    timeout: float = _AKSHARE_CALL_TIMEOUT,
) -> Any:
    """AKShare 网络调用：失败重试 + 超时，不静默降级 mock。"""
    last_exc: Optional[Exception] = None
    for attempt in range(1, _AKSHARE_RETRIES + 1):
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(fn)
                return fut.result(timeout=timeout)
        except Exception as exc:
            last_exc = exc
            logger.warning("AKShare %s 第 %d/%d 次失败: %s", label, attempt, _AKSHARE_RETRIES, exc)
            if attempt < _AKSHARE_RETRIES:
                time.sleep(_AKSHARE_RETRY_DELAY * attempt)
    assert last_exc is not None
    raise last_exc


def get_data_provider() -> str:
    raw = os.getenv("STOCK_DATA_PROVIDER", "akshare").strip().lower()
    return raw or "akshare"


def _akshare_import():
    try:
        import akshare as ak  # type: ignore

        return ak
    except ImportError:
        return None


def is_akshare_available() -> bool:
    return _akshare_import() is not None


class StockMarketData:
    """AKShare 行情数据封装，失败时返回 degraded 标记与空列表。"""

    _SNAPSHOT_TTL_SECONDS = 60.0

    def __init__(self) -> None:
        self.provider = get_data_provider()
        self._snapshot_cache: Optional[dict[str, Any]] = None
        self._snapshot_cache_at: float = 0.0

    @property
    def akshare_enabled(self) -> bool:
        return self.provider in ("akshare", "ak") and is_akshare_available()

    def _parse_sector_row(self, row: Any) -> dict[str, Any]:
        return {
            "rank": _safe_int(row.get("排名")),
            "name": str(row.get("板块名称") or row.get("name") or ""),
            "code": str(row.get("板块代码") or row.get("code") or ""),
            "price": _safe_float(row.get("最新价")),
            "change_amount": _safe_float(row.get("涨跌额")),
            "change_pct": _safe_float(row.get("涨跌幅")),
            "market_cap": _safe_float(row.get("总市值")),
            "turnover_rate": _safe_float(row.get("换手率")),
            "up_count": _safe_int(row.get("上涨家数")),
            "down_count": _safe_int(row.get("下跌家数")),
            "leading_stock": str(row.get("领涨股票") or ""),
            "leading_stock_change_pct": _safe_float(row.get("领涨股票-涨跌幅")),
            "amount": None,
        }

    def get_sector_list(self, *, limit: int = 30) -> dict[str, Any]:
        """行业板块列表（东方财富）。"""
        result = self.get_sectors_trading(limit=limit)
        if not result.get("ok"):
            return {**result, "kind": "sector_list"}
        items = result.get("items") or []
        slim = [
            {"name": i["name"], "code": i["code"], "change_pct": i.get("change_pct")}
            for i in items
        ]
        return {
            "ok": True,
            "provider": result.get("provider"),
            "items": slim,
            "total": len(slim),
            "degraded": result.get("degraded", False),
            "fetched_at": result.get("fetched_at"),
        }

    @staticmethod
    def normalize_classify(classify: str) -> str:
        key = (classify or "industry").strip().lower()
        if key in ("sw", "sw1", "sw_1"):
            return "sw_l1"
        if key in ("sw2", "sw_2"):
            return "sw_l2"
        if key in ("sw3", "sw_3"):
            return "sw_l3"
        return key if key in SECTOR_CLASSIFICATIONS else "industry"

    def list_classifications(self) -> dict[str, Any]:
        """返回支持的板块/工业分类体系。"""
        items = [
            {"classify": k, **v}
            for k, v in SECTOR_CLASSIFICATIONS.items()
        ]
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "default": "industry",
        }

    def get_sectors_trading(
        self,
        *,
        classify: str = "industry",
        limit: int = 100,
        sort_by: str = "change_pct",
        order: str = "desc",
        enrich_amount: bool = False,
    ) -> dict[str, Any]:
        """板块/工业分类交易概览（涨跌幅、规模、涨跌家数、领涨股等）。"""
        limit = max(1, min(limit, 200))
        classify = self.normalize_classify(classify)
        meta = SECTOR_CLASSIFICATIONS.get(classify, SECTOR_CLASSIFICATIONS["industry"])
        if not self.akshare_enabled:
            return self._empty("sectors_trading", reason="akshare 未安装或未启用")

        ak = _akshare_import()
        assert ak is not None
        try:
            if classify == "industry":
                rows, degraded, fallback = self._fetch_em_board_rows(ak, board="industry")
            elif classify == "concept":
                rows, degraded, fallback = self._fetch_em_board_rows(ak, board="concept")
            elif classify in _SW_REALTIME_LEVEL:
                rows, degraded, fallback = self._fetch_sw_realtime_rows(
                    ak, level=_SW_REALTIME_LEVEL[classify]
                )
            elif classify == "sw_l3":
                rows, degraded, fallback = self._fetch_sw_l3_rows(ak)
            else:
                rows, degraded, fallback = self._fetch_em_board_rows(ak, board="industry")

            if enrich_amount or sort_by == "amount":
                self._enrich_sector_amounts(rows, ak, classify=classify)
            rows = self._sort_sector_rows(rows, sort_by=sort_by, order=order, classify=classify)
            rows = rows[:limit]
            classify_source = "同花顺" if fallback == "ths" else meta["source"]
            return {
                "ok": True,
                "provider": "akshare",
                "classify": classify,
                "classify_label": meta["label"],
                "classify_source": classify_source,
                "items": rows,
                "total": len(rows),
                "sort_by": sort_by,
                "order": order,
                "degraded": degraded,
                "fallback": fallback,
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            if classify in ("industry", "concept"):
                logger.warning(
                    "AKShare %s 板块列表主接口失败，尝试同花顺备用: %s",
                    classify,
                    exc,
                )
                try:
                    if classify == "industry":
                        rows = self._fetch_ths_industry_fallback_rows(ak)
                    else:
                        rows = self._fetch_ths_concept_fallback_rows(ak)
                    if enrich_amount or sort_by == "amount":
                        self._enrich_sector_amounts(rows, ak, classify=classify)
                    rows = self._sort_sector_rows(rows, sort_by=sort_by, order=order, classify=classify)
                    rows = rows[:limit]
                    self._cache_sector_trading_rows(classify, rows, degraded=True, fallback="ths")
                    return {
                        "ok": True,
                        "provider": "akshare",
                        "classify": classify,
                        "classify_label": meta["label"],
                        "classify_source": "同花顺" if classify == "concept" else meta["source"],
                        "items": rows,
                        "total": len(rows),
                        "sort_by": sort_by,
                        "order": order,
                        "degraded": True,
                        "fallback": "ths",
                        "fetched_at": datetime.now().isoformat(),
                    }
                except Exception as fallback_exc:
                    logger.warning("AKShare %s 板块列表备用接口失败: %s", classify, fallback_exc)
                    return self._empty("sectors_trading", reason=str(fallback_exc), degraded=True)
            logger.warning("AKShare 分类 %s 拉取失败: %s", classify, exc)
            return self._empty("sectors_trading", reason=str(exc), degraded=True)

    @staticmethod
    def _sector_trading_cache_key(classify: str) -> str:
        return f"sectors_trading:{classify}"

    def _get_cached_sector_trading_rows(
        self,
        classify: str,
    ) -> Optional[tuple[list[dict[str, Any]], bool, Optional[str]]]:
        cached = _SECTOR_TRADING_CACHE.get(self._sector_trading_cache_key(classify))
        if not cached:
            return None
        if time.time() - cached[0] >= _SECTOR_TRADING_CACHE_TTL:
            return None
        rows, degraded, fallback = cached[1]
        return [dict(row) for row in rows], degraded, fallback

    def _cache_sector_trading_rows(
        self,
        classify: str,
        rows: list[dict[str, Any]],
        *,
        degraded: bool,
        fallback: Optional[str],
    ) -> None:
        _SECTOR_TRADING_CACHE[self._sector_trading_cache_key(classify)] = (
            time.time(),
            ([dict(row) for row in rows], degraded, fallback),
        )

    def _fetch_em_board_rows(
        self,
        ak: Any,
        *,
        board: str,
    ) -> tuple[list[dict[str, Any]], bool, Optional[str]]:
        cached = self._get_cached_sector_trading_rows(board)
        if cached is not None:
            return cached

        if board == "concept":
            df = _akshare_call(ak.stock_board_concept_name_em, label="概念板块列表")
        else:
            df = _akshare_call(ak.stock_board_industry_name_em, label="行业板块列表")
        rows = [self._parse_sector_row(row) for _, row in df.iterrows()]
        result = (rows, False, None)
        self._cache_sector_trading_rows(board, rows, degraded=False, fallback=None)
        return result

    def _fetch_ths_industry_fallback_rows(self, ak: Any) -> list[dict[str, Any]]:
        df = _akshare_call(ak.stock_board_industry_summary_ths, label="行业板块列表(同花顺)")
        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "rank": None,
                    "name": str(row.get("板块") or row.get("板块名称") or ""),
                    "code": "",
                    "price": None,
                    "change_amount": None,
                    "change_pct": _safe_float(row.get("涨跌幅")),
                    "market_cap": None,
                    "turnover_rate": None,
                    "up_count": _safe_int(row.get("上涨家数")),
                    "down_count": _safe_int(row.get("下跌家数")),
                    "leading_stock": str(row.get("领涨股") or ""),
                    "leading_stock_change_pct": _safe_float(row.get("领涨股-涨跌幅")),
                    "amount": _safe_float(row.get("总成交额")),
                }
            )
        return rows

    def _fetch_ths_concept_fallback_rows(self, ak: Any) -> list[dict[str, Any]]:
        """概念板块备用：优先同花顺概念资金流（含涨跌幅），失败则仅名称列表。"""
        try:
            df = _akshare_call(
                lambda: ak.stock_fund_flow_concept(symbol="即时"),
                label="概念板块列表(同花顺资金流)",
            )
            rows: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                rows.append(
                    {
                        "rank": _safe_int(row.get("序号")),
                        "name": str(row.get("行业") or row.get("概念") or ""),
                        "code": "",
                        "price": _safe_float(row.get("当前价")),
                        "change_amount": None,
                        "change_pct": _safe_float(row.get("行业-涨跌幅")),
                        "market_cap": None,
                        "turnover_rate": None,
                        "up_count": _safe_int(row.get("公司家数")),
                        "down_count": None,
                        "leading_stock": str(row.get("领涨股") or ""),
                        "leading_stock_change_pct": _safe_float(row.get("领涨股-涨跌幅")),
                        "amount": None,
                    }
                )
            if rows:
                return rows
        except Exception as exc:
            logger.warning("同花顺概念资金流备用失败，尝试概念名称列表: %s", exc)

        df = _akshare_call(ak.stock_board_concept_name_ths, label="概念板块名称(同花顺)")
        return [
            {
                "rank": None,
                "name": str(row.get("name") or ""),
                "code": str(row.get("code") or ""),
                "price": None,
                "change_amount": None,
                "change_pct": None,
                "market_cap": None,
                "turnover_rate": None,
                "up_count": None,
                "down_count": None,
                "leading_stock": "",
                "leading_stock_change_pct": None,
                "amount": None,
            }
            for _, row in df.iterrows()
        ]

    def _fetch_sw_realtime_rows(
        self,
        ak: Any,
        *,
        level: str,
    ) -> tuple[list[dict[str, Any]], bool, Optional[str]]:
        df = _akshare_call(
            lambda: ak.index_realtime_sw(symbol=level),
            label=f"申万{level}实时",
        )
        rows = [self._parse_sw_realtime_row(row) for _, row in df.iterrows()]
        return rows, False, None

    def _fetch_sw_l3_rows(self, ak: Any) -> tuple[list[dict[str, Any]], bool, Optional[str]]:
        df = _akshare_call(ak.sw_index_third_info, label="申万三级分类")
        rows = [self._parse_sw_l3_row(row) for _, row in df.iterrows()]
        return rows, False, None

    def _parse_sw_realtime_row(self, row: Any) -> dict[str, Any]:
        prev_close = _safe_float(row.get("昨收盘"))
        latest = _safe_float(row.get("最新价"))
        change_pct = None
        if prev_close and latest is not None and prev_close != 0:
            change_pct = (latest - prev_close) / prev_close * 100
        change_amount = (latest - prev_close) if latest is not None and prev_close is not None else None
        return {
            "rank": None,
            "name": str(row.get("指数名称") or ""),
            "code": str(row.get("指数代码") or ""),
            "price": latest,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "market_cap": None,
            "turnover_rate": None,
            "up_count": None,
            "down_count": None,
            "leading_stock": "",
            "leading_stock_change_pct": None,
            "amount": _safe_float(row.get("成交额")),
            "volume": _safe_float(row.get("成交量")),
            "parent": "",
        }

    def _parse_sw_l3_row(self, row: Any) -> dict[str, Any]:
        raw_code = str(row.get("行业代码") or "")
        code = raw_code.replace(".SI", "").strip()
        return {
            "rank": None,
            "name": str(row.get("行业名称") or ""),
            "code": code,
            "price": None,
            "change_amount": None,
            "change_pct": None,
            "market_cap": None,
            "turnover_rate": None,
            "up_count": None,
            "down_count": None,
            "leading_stock": "",
            "leading_stock_change_pct": None,
            "amount": None,
            "parent": str(row.get("上级行业") or ""),
            "constituent_count": _safe_int(row.get("成份个数")),
            "pe_static": _safe_float(row.get("静态市盈率")),
            "pe_ttm": _safe_float(row.get("TTM(滚动)市盈率")),
            "pb": _safe_float(row.get("市净率")),
            "dividend_yield": _safe_float(row.get("静态股息率")),
        }

    def get_sector_detail(
        self,
        symbol: str,
        *,
        classify: str = "industry",
        code: str | None = None,
        cons_limit: int = 30,
    ) -> dict[str, Any]:
        """单个板块/分类实时快照 + 成分股。"""
        name = (symbol or "").strip()
        if not name and not (code or "").strip():
            return {"ok": False, "error": "symbol 或 code 必填"}
        classify = self.normalize_classify(classify)
        meta = SECTOR_CLASSIFICATIONS.get(classify, SECTOR_CLASSIFICATIONS["industry"])
        if not self.akshare_enabled:
            return {"ok": False, "error": "akshare 未安装或未启用", "degraded": True}

        ak = _akshare_import()
        assert ak is not None
        cons_limit = max(1, min(cons_limit, 100))
        spot: dict[str, Any] = {}
        constituents: list[dict[str, Any]] = []
        index_code: str | None = None

        if classify == "concept":
            spot, constituents = self._fetch_em_board_detail(ak, name, cons_limit, board="concept")
        elif classify == "industry":
            spot, constituents = self._fetch_em_board_detail(ak, name, cons_limit, board="industry")
        elif classify in _SW_REALTIME_LEVEL or classify == "sw_l3":
            index_code = (code or name).strip().replace(".SI", "")
            spot, constituents = self._fetch_sw_index_detail(ak, index_code, name, cons_limit)
        else:
            spot, constituents = self._fetch_em_board_detail(ak, name, cons_limit, board="industry")

        ok = bool(spot or constituents)
        resolved_code = index_code if classify.startswith("sw") else ((code or "").strip().replace(".SI", "") or None)
        resolved_name = name or (resolved_code or "")
        introduction = self.get_sector_intro(
            resolved_name,
            classify=classify,
            code=resolved_code,
            constituents=constituents,
            spot=spot,
        )
        return {
            "ok": ok,
            "provider": "akshare",
            "classify": classify,
            "classify_label": meta["label"],
            "symbol": resolved_name,
            "code": resolved_code,
            "spot": spot,
            "constituents": constituents,
            "constituents_total": len(constituents),
            "introduction": introduction,
            "degraded": not ok,
            "fetched_at": datetime.now().isoformat(),
        }

    def get_sector_intro(
        self,
        symbol: str,
        *,
        classify: str = "industry",
        code: str | None = None,
        constituents: list[dict[str, Any]] | None = None,
        spot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """板块/分类简介（24h 本地缓存，基于行情与成份股聚合）。"""
        name = (symbol or "").strip()
        classify = self.normalize_classify(classify)
        cache_key = _sector_intro_cache_key(classify, name, code)
        cached = _get_cached_sector_intro(cache_key)
        if cached is not None:
            return cached

        empty = _empty_sector_intro(name or (code or ""))
        if not name and not (code or "").strip():
            return empty
        if not self.akshare_enabled:
            return empty

        ak = _akshare_import()
        assert ak is not None
        try:
            intro = self._build_sector_intro(
                ak,
                name or (code or ""),
                classify=classify,
                code=code,
                constituents=constituents or [],
                spot=spot or {},
            )
        except Exception as exc:
            logger.warning("板块简介生成失败 %s/%s: %s", classify, name or code, exc)
            return empty

        _set_cached_sector_intro(cache_key, intro)
        return intro

    def _build_sector_intro(
        self,
        ak: Any,
        name: str,
        *,
        classify: str,
        code: str | None,
        constituents: list[dict[str, Any]],
        spot: dict[str, Any],
    ) -> dict[str, Any]:
        meta = SECTOR_CLASSIFICATIONS.get(classify, SECTOR_CLASSIFICATIONS["industry"])
        cons_names = [str(c.get("name") or "").strip() for c in constituents if c.get("name")]
        cons_names = [n for n in cons_names if n][:8]
        scope = f"代表{'概念' if classify == 'concept' else '成份'}股：{'、'.join(cons_names)}" if cons_names else ""

        if classify in ("industry", "concept"):
            return self._build_em_board_intro(
                ak, name, classify=classify, meta=meta,
                constituents=constituents, spot=spot, scope=scope, cons_names=cons_names,
            )
        if classify.startswith("sw"):
            return self._build_sw_sector_intro(
                ak, name, classify=classify, meta=meta, code=code,
                constituents=constituents, spot=spot, scope=scope, cons_names=cons_names,
            )
        return _empty_sector_intro(name)

    def _build_em_board_intro(
        self,
        ak: Any,
        name: str,
        *,
        classify: str,
        meta: dict[str, str],
        constituents: list[dict[str, Any]],
        spot: dict[str, Any],
        scope: str,
        cons_names: list[str],
    ) -> dict[str, Any]:
        board = "concept" if classify == "concept" else "industry"
        board_row = self._fetch_em_board_row(ak, name, board=board)
        resolved_name = str((board_row or {}).get("name") or name)
        board_code = str((board_row or {}).get("code") or "")

        parts: list[str] = []
        if classify == "concept":
            parts.append(f"{resolved_name}是 A 股市场{meta['label']}（{meta['source']}）。")
        else:
            parts.append(f"{resolved_name}属于{meta['label']}（{meta['source']}）。")
        if board_code:
            parts.append(f"板块代码 {board_code}。")

        change_pct = _safe_float(spot.get("涨跌幅"))
        if change_pct is None and board_row:
            change_pct = board_row.get("change_pct")
        if change_pct is not None:
            parts.append(f"最新涨跌幅 {change_pct:+.2f}%。")

        up_count = (board_row or {}).get("up_count")
        down_count = (board_row or {}).get("down_count")
        if up_count is not None and down_count is not None:
            parts.append(f"成份股涨跌家数 {up_count} / {down_count}。")

        market_cap = (board_row or {}).get("market_cap")
        if market_cap is not None:
            parts.append(f"板块总市值约 {_fmt_yi(market_cap)}。")

        leading = str((board_row or {}).get("leading_stock") or "").strip()
        leading_pct = (board_row or {}).get("leading_stock_change_pct")
        hotspots = ""
        if leading:
            hotspots = f"今日领涨 {leading}"
            if leading_pct is not None:
                hotspots += f"（{leading_pct:+.2f}%）"
            parts.append(f"{hotspots}。")

        if cons_names:
            parts.append(f"主要{'概念' if classify == 'concept' else '行业'}标的包括{'、'.join(cons_names[:5])}等。")

        description = "".join(parts) if parts else "暂无简介"
        return {
            "name": resolved_name,
            "description": description,
            "scope": scope,
            "industry_chain": "",
            "hotspots": hotspots,
            "data_source": meta["source"],
            "cached": False,
        }

    def _build_sw_sector_intro(
        self,
        ak: Any,
        name: str,
        *,
        classify: str,
        meta: dict[str, str],
        code: str | None,
        constituents: list[dict[str, Any]],
        spot: dict[str, Any],
        scope: str,
        cons_names: list[str],
    ) -> dict[str, Any]:
        index_code = (code or name).strip().replace(".SI", "")
        sw_meta = self._fetch_sw_sector_meta(ak, index_code, name, classify)
        resolved_name = str(sw_meta.get("name") or name)
        parent = str(sw_meta.get("parent") or "").strip()
        level_label = meta["label"]

        if classify == "sw_l1":
            industry_chain = resolved_name
        elif parent:
            industry_chain = f"{parent} > {resolved_name}"
        else:
            industry_chain = resolved_name

        parts: list[str] = [f"{resolved_name}是申万宏源{level_label}（指数代码 {index_code}）。"]
        if parent and classify != "sw_l1":
            parts.append(f"隶属于申万「{parent}」分类。")

        count = sw_meta.get("constituent_count")
        if count is not None:
            parts.append(f"共 {count} 只成份股。")
        elif cons_names:
            parts.append(f"样本成份股 {len(cons_names)} 只。")

        pe_ttm = sw_meta.get("pe_ttm")
        pb = sw_meta.get("pb")
        metrics: list[str] = []
        if pe_ttm is not None:
            metrics.append(f"TTM 市盈率 {pe_ttm:.2f}")
        if pb is not None:
            metrics.append(f"市净率 {pb:.2f}")
        if metrics:
            parts.append("，".join(metrics) + "。")

        change_pct = _safe_float(spot.get("涨跌幅"))
        if change_pct is not None:
            parts.append(f"指数最新涨跌幅 {change_pct:+.2f}%。")

        weighted: list[str] = []
        for c in constituents[:3]:
            w = _safe_float(c.get("weight"))
            n = str(c.get("name") or "").strip()
            if n and w is not None:
                weighted.append(f"{n}（权重 {w:.2f}%）")
        if weighted:
            parts.append(f"权重靠前：{'、'.join(weighted)}。")
        elif cons_names:
            parts.append(f"代表成份股包括{'、'.join(cons_names[:5])}等。")

        description = "".join(parts) if parts else "暂无简介"
        return {
            "name": resolved_name,
            "description": description,
            "scope": scope,
            "industry_chain": industry_chain,
            "hotspots": "",
            "data_source": meta["source"],
            "cached": False,
        }

    def _fetch_em_board_row(self, ak: Any, name: str, *, board: str) -> Optional[dict[str, Any]]:
        if board == "concept":
            df = _akshare_call(ak.stock_board_concept_name_em, label="概念板块列表(简介)")
        else:
            df = _akshare_call(ak.stock_board_industry_name_em, label="行业板块列表(简介)")
        col_name = "板块名称"
        matched = df[df[col_name] == name]
        if matched.empty:
            matched = df[df[col_name].str.contains(name, na=False, regex=False)]
        if matched.empty:
            return None
        return self._parse_sector_row(matched.iloc[0])

    def _fetch_sw_sector_meta(
        self,
        ak: Any,
        index_code: str,
        name: str,
        classify: str,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {"name": name, "parent": "", "constituent_count": None}
        code_key = index_code.replace(".SI", "").strip()
        try:
            if classify == "sw_l3":
                df = _akshare_call(ak.sw_index_third_info, label="申万三级(简介)")
                row = df[df["行业代码"].astype(str).str.replace(".SI", "", regex=False) == code_key]
                if row.empty:
                    row = df[df["行业名称"] == name]
                if not row.empty:
                    r = row.iloc[0]
                    meta.update(
                        {
                            "name": str(r.get("行业名称") or name),
                            "parent": str(r.get("上级行业") or ""),
                            "constituent_count": _safe_int(r.get("成份个数")),
                            "pe_ttm": _safe_float(r.get("TTM(滚动)市盈率")),
                            "pb": _safe_float(r.get("市净率")),
                        }
                    )
            elif classify == "sw_l2":
                df = _akshare_call(ak.sw_index_second_info, label="申万二级(简介)")
                row = df[df["行业代码"].astype(str).str.replace(".SI", "", regex=False) == code_key]
                if row.empty:
                    row = df[df["行业名称"] == name]
                if not row.empty:
                    r = row.iloc[0]
                    meta.update(
                        {
                            "name": str(r.get("行业名称") or name),
                            "parent": str(r.get("上级行业") or ""),
                            "constituent_count": _safe_int(r.get("成份个数")),
                            "pe_ttm": _safe_float(r.get("TTM(滚动)市盈率")),
                            "pb": _safe_float(r.get("市净率")),
                        }
                    )
            elif classify == "sw_l1":
                df = _akshare_call(ak.sw_index_first_info, label="申万一级(简介)")
                row = df[df["行业代码"].astype(str).str.replace(".SI", "", regex=False) == code_key]
                if row.empty:
                    row = df[df["行业名称"] == name]
                if not row.empty:
                    r = row.iloc[0]
                    meta.update(
                        {
                            "name": str(r.get("行业名称") or name),
                            "constituent_count": _safe_int(r.get("成份个数")),
                            "pe_ttm": _safe_float(r.get("TTM(滚动)市盈率")),
                            "pb": _safe_float(r.get("市净率")),
                        }
                    )
        except Exception as exc:
            logger.debug("申万元数据 %s: %s", index_code, exc)
        return meta

    def _fetch_em_board_detail(
        self,
        ak: Any,
        name: str,
        cons_limit: int,
        *,
        board: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        spot: dict[str, Any] = {}
        constituents: list[dict[str, Any]] = []
        spot_fn = (
            ak.stock_board_concept_spot_em if board == "concept" else ak.stock_board_industry_spot_em
        )
        cons_fn = (
            ak.stock_board_concept_cons_em if board == "concept" else ak.stock_board_industry_cons_em
        )
        label = "概念" if board == "concept" else "行业"
        try:
            spot_df = _akshare_call(lambda: spot_fn(symbol=name), label=f"{label}快照({name})")
            for _, row in spot_df.iterrows():
                key = str(row.get("item") or "")
                val = _safe_float(row.get("value"))
                if key:
                    spot[key] = val
        except Exception as exc:
            logger.warning("%s快照失败 %s: %s", label, name, exc)

        try:
            cons_df = _akshare_call(lambda: cons_fn(symbol=name), label=f"{label}成分({name})")
            for _, row in cons_df.head(cons_limit).iterrows():
                constituents.append(
                    {
                        "code": str(row.get("代码") or ""),
                        "name": str(row.get("名称") or ""),
                        "price": _safe_float(row.get("最新价")),
                        "change_pct": _safe_float(row.get("涨跌幅")),
                        "change_amount": _safe_float(row.get("涨跌额")),
                        "amount": _safe_float(row.get("成交额")),
                        "turnover_rate": _safe_float(row.get("换手率")),
                    }
                )
        except Exception as exc:
            logger.warning("%s成分失败 %s: %s", label, name, exc)
        return spot, constituents

    def _fetch_sw_index_detail(
        self,
        ak: Any,
        index_code: str,
        name: str,
        cons_limit: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        spot: dict[str, Any] = {}
        constituents: list[dict[str, Any]] = []
        try:
            hist_df = _akshare_call(
                lambda: ak.index_hist_sw(symbol=index_code, period="day"),
                label=f"申万指数历史({index_code})",
            )
            if not hist_df.empty:
                last = hist_df.iloc[-1]
                prev = hist_df.iloc[-2] if len(hist_df) > 1 else None
                close = _safe_float(last.get("收盘"))
                prev_close = _safe_float(prev.get("收盘")) if prev is not None else None
                change_pct = None
                if close is not None and prev_close and prev_close != 0:
                    change_pct = (close - prev_close) / prev_close * 100
                spot = {
                    "最新": close,
                    "涨跌幅": change_pct,
                    "成交额": _safe_float(last.get("成交额")),
                    "成交量": _safe_float(last.get("成交量")),
                    "最高": _safe_float(last.get("最高")),
                    "最低": _safe_float(last.get("最低")),
                    "开盘": _safe_float(last.get("开盘")),
                    "日期": str(last.get("日期") or ""),
                }
        except Exception as exc:
            logger.warning("申万指数历史失败 %s: %s", index_code, exc)

        try:
            cons_df = _akshare_call(
                lambda: ak.index_component_sw(symbol=index_code),
                label=f"申万成分({index_code})",
            )
            for _, row in cons_df.head(cons_limit).iterrows():
                constituents.append(
                    {
                        "code": str(row.get("证券代码") or ""),
                        "name": str(row.get("证券名称") or ""),
                        "weight": _safe_float(row.get("最新权重")),
                        "inclusion_date": str(row.get("计入日期") or ""),
                    }
                )
        except Exception as exc:
            logger.warning("申万成分失败 %s: %s", index_code, exc)
        return spot, constituents

    def _enrich_sector_amounts(
        self,
        rows: list[dict[str, Any]],
        ak: Any,
        *,
        classify: str = "industry",
    ) -> None:
        now = time.time()
        pending: list[dict[str, Any]] = []
        for row in rows:
            if row.get("amount") is not None:
                continue
            cache_key = f"{classify}:{row.get('code') or row.get('name') or ''}"
            cached = _SECTOR_AMOUNT_CACHE.get(cache_key)
            if cached and now - cached[0] < _SECTOR_AMOUNT_CACHE_TTL:
                row["amount"] = cached[1].get("amount")
                continue
            pending.append(row)

        if not pending:
            return

        def _fetch_amount(row: dict[str, Any]) -> tuple[str, Optional[float]]:
            name = row["name"]
            cache_key = f"{classify}:{row.get('code') or name}"
            try:
                if classify == "concept":
                    spot_df = ak.stock_board_concept_spot_em(symbol=name)
                elif classify.startswith("sw"):
                    return cache_key, row.get("amount")
                else:
                    spot_df = ak.stock_board_industry_spot_em(symbol=name)
                amount_row = spot_df.loc[spot_df["item"] == "成交额"]
                amount = _safe_float(amount_row["value"].iloc[0]) if not amount_row.empty else None
                _SECTOR_AMOUNT_CACHE[cache_key] = (time.time(), {"amount": amount})
                return cache_key, amount
            except Exception as exc:
                logger.debug("板块成交额 %s: %s", name, exc)
                return cache_key, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_amount, row): row for row in pending}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    _, amount = fut.result()
                    if amount is not None:
                        row["amount"] = amount
                except Exception:
                    row["amount"] = None

    @staticmethod
    def _sort_sector_rows(
        rows: list[dict[str, Any]],
        *,
        sort_by: str,
        order: str,
        classify: str = "industry",
    ) -> list[dict[str, Any]]:
        key_map = {
            "change_pct": lambda r: r.get("change_pct"),
            "amount": lambda r: r.get("amount") if r.get("amount") is not None else r.get("market_cap"),
            "market_cap": lambda r: r.get("market_cap"),
            "turnover_rate": lambda r: r.get("turnover_rate"),
            "rank": lambda r: r.get("rank"),
            "name": lambda r: r.get("name") or "",
            "constituent_count": lambda r: r.get("constituent_count"),
            "pe_ttm": lambda r: r.get("pe_ttm"),
            "pb": lambda r: r.get("pb"),
        }
        if classify == "sw_l3" and sort_by == "change_pct":
            sort_by = "constituent_count"
        key_fn = key_map.get(sort_by, key_map["change_pct"])
        reverse = order.lower() != "asc"
        return sorted(
            rows,
            key=lambda r: (
                key_fn(r) is None,
                key_fn(r) if key_fn(r) is not None else (float("-inf") if reverse else float("inf")),
            ),
            reverse=reverse,
        )

    def get_index_overview(self) -> dict[str, Any]:
        """主要宽基指数实时概览。"""
        if not self.akshare_enabled:
            return self._empty("index_overview", reason="akshare 未安装或未启用")

        ak = _akshare_import()
        assert ak is not None
        try:
            df = _akshare_call(ak.stock_zh_index_spot_em, label="指数概览")
            items: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                name = str(row.get("名称") or row.get("name") or "")
                if name not in _MAIN_INDEX_NAMES:
                    continue
                items.append(
                    {
                        "name": name,
                        "code": str(row.get("代码") or row.get("code") or ""),
                        "price": _safe_float(row.get("最新价")),
                        "change_pct": _safe_float(row.get("涨跌幅")),
                        "volume": _safe_float(row.get("成交量")),
                    }
                )
            items.sort(key=lambda x: x.get("name") or "")
            return {
                "ok": True,
                "provider": "akshare",
                "items": items,
                "total": len(items),
                "degraded": len(items) == 0,
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            logger.warning("AKShare 指数概览失败: %s", exc)
            return self._empty("index_overview", reason=str(exc), degraded=True)

    def get_stock_basic(self, symbol: str) -> dict[str, Any]:
        """个股基本信息（可选）。"""
        code = (symbol or "").strip()
        if not code:
            return {"ok": False, "error": "symbol 必填"}
        if not self.akshare_enabled:
            return {"ok": False, "error": "akshare 未安装或未启用", "degraded": True}

        ak = _akshare_import()
        assert ak is not None
        try:
            df = _akshare_call(lambda: ak.stock_individual_info_em(symbol=code), label=f"个股信息({code})")
            info: dict[str, str] = {}
            for _, row in df.iterrows():
                key = str(row.get("item") or row.get("项目") or "")
                val = str(row.get("value") or row.get("值") or "")
                if key:
                    info[key] = val
            return {
                "ok": True,
                "provider": "akshare",
                "symbol": code,
                "info": info,
                "degraded": False,
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            logger.warning("AKShare 个股信息失败 %s: %s", code, exc)
            return {"ok": False, "symbol": code, "error": str(exc), "degraded": True}

    def get_hk_index_overview(self) -> dict[str, Any]:
        """港股主要指数概览（恒生指数等）。"""
        from src.core.stock_market_utils import is_hk_enabled

        if not is_hk_enabled() or not self.akshare_enabled:
            return self._empty("hk_indices", reason="港股未启用或 AKShare 不可用", degraded=True)
        ak = _akshare_import()
        if ak is None:
            return self._empty("hk_indices", reason="akshare 未安装", degraded=True)
        try:
            df = _akshare_call(lambda: ak.stock_hk_index_spot_em(), label="stock_hk_index_spot_em")
            if df is None or df.empty:
                return self._empty("hk_indices", reason="无港股指数数据")
            focus = ("恒生指数", "恒生科技指数", "国企指数", "红筹指数")
            items: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                name = str(row.get("名称") or "")
                if name not in focus and len(items) >= 6:
                    continue
                if name in focus or len(items) < 4:
                    items.append(
                        {
                            "code": str(row.get("代码") or ""),
                            "name": name,
                            "price": _safe_float(row.get("最新价")),
                            "change_pct": _safe_float(row.get("涨跌幅")),
                            "change_amount": _safe_float(row.get("涨跌额")),
                        }
                    )
            return {
                "ok": bool(items),
                "provider": self.provider,
                "kind": "hk_indices",
                "items": items[:6],
                "total": len(items),
                "degraded": False,
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            logger.warning("港股指数概览失败: %s", exc)
            return self._empty("hk_indices", reason=str(exc), degraded=True)

    def get_us_index_overview(self) -> dict[str, Any]:
        """美股主要指数概览（道琼斯、标普500、纳斯达克等）。"""
        from src.core.stock_market_utils import is_us_enabled

        if not is_us_enabled() or not self.akshare_enabled:
            return self._empty("us_indices", reason="美股未启用或 AKShare 不可用", degraded=True)
        ak = _akshare_import()
        if ak is None:
            return self._empty("us_indices", reason="akshare 未安装", degraded=True)
        try:
            df = _akshare_call(lambda: ak.index_global_spot_em(), label="index_global_spot_em")
            if df is None or df.empty:
                return self._empty("us_indices", reason="无美股指数数据")
            focus = ("道琼斯", "标普500", "纳斯达克", "纳斯达克100")
            items: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                name = str(row.get("名称") or "")
                code = str(row.get("代码") or "")
                if name not in focus and code not in ("DJIA", "SPX", "NDX"):
                    continue
                items.append(
                    {
                        "code": code,
                        "name": name,
                        "price": _safe_float(row.get("最新价")),
                        "change_pct": _safe_float(row.get("涨跌幅")),
                        "change_amount": _safe_float(row.get("涨跌额")),
                    }
                )
            if not items:
                for _, row in df.iterrows():
                    code = str(row.get("代码") or "")
                    if code in ("DJIA", "SPX", "NDX"):
                        items.append(
                            {
                                "code": code,
                                "name": str(row.get("名称") or code),
                                "price": _safe_float(row.get("最新价")),
                                "change_pct": _safe_float(row.get("涨跌幅")),
                                "change_amount": _safe_float(row.get("涨跌额")),
                            }
                        )
            return {
                "ok": bool(items),
                "provider": self.provider,
                "kind": "us_indices",
                "items": items[:6],
                "total": len(items),
                "degraded": False,
                "fetched_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            logger.warning("美股指数概览失败: %s", exc)
            return self._empty("us_indices", reason=str(exc), degraded=True)

    def get_market_snapshot(self) -> dict[str, Any]:
        """板块 + 指数合并快照，供页面/API 一次拉取（短 TTL 缓存，避免阻塞 UI）。"""
        now = time.time()
        if (
            self._snapshot_cache is not None
            and (now - self._snapshot_cache_at) < self._SNAPSHOT_TTL_SECONDS
        ):
            return self._snapshot_cache

        sectors = self.get_sector_list(limit=20)
        indices = self.get_index_overview()
        degraded = bool(sectors.get("degraded") or indices.get("degraded"))
        result = {
            "ok": sectors.get("ok") or indices.get("ok"),
            "provider": self.provider,
            "sectors": sectors,
            "indices": indices,
            "degraded": degraded,
            "disclaimer": "行情数据仅供参考，不构成投资建议。",
            "fetched_at": datetime.now().isoformat(),
        }
        self._snapshot_cache = result
        self._snapshot_cache_at = now
        return result

    def _empty(
        self,
        kind: str,
        *,
        reason: str = "",
        degraded: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.provider,
            "kind": kind,
            "items": [],
            "total": 0,
            "degraded": degraded or not self.akshare_enabled,
            "error": reason or None,
            "fetched_at": datetime.now().isoformat(),
        }


def _fmt_yi(value: float) -> str:
    yi = value / 1e8 if value >= 1e4 else value
    if yi >= 10000:
        return f"{yi / 10000:.2f} 万亿"
    return f"{yi:.2f} 亿"


def _sector_intro_cache_key(classify: str, symbol: str, code: str | None) -> str:
    key = (code or symbol or "").strip().replace(".SI", "")
    return f"{classify}:{key or symbol}"


def _read_sector_intro_cache() -> dict[str, Any]:
    if not _SECTOR_INTRO_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_SECTOR_INTRO_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_sector_intro_cache(store: dict[str, Any]) -> None:
    _SECTOR_INTRO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SECTOR_INTRO_CACHE_PATH.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_cached_sector_intro(cache_key: str) -> Optional[dict[str, Any]]:
    store = _read_sector_intro_cache()
    entry = store.get(cache_key)
    if not entry:
        return None
    cached_at = entry.get("cached_at", 0)
    if time.time() - cached_at > _SECTOR_INTRO_TTL:
        return None
    payload = dict(entry.get("payload") or {})
    payload["cached"] = True
    return payload


def _set_cached_sector_intro(cache_key: str, payload: dict[str, Any]) -> None:
    store = _read_sector_intro_cache()
    store[cache_key] = {"cached_at": time.time(), "payload": payload}
    _write_sector_intro_cache(store)


def _empty_sector_intro(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "暂无简介",
        "scope": "",
        "industry_chain": "",
        "hotspots": "",
        "data_source": "",
        "cached": False,
    }


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


_service: Optional[StockMarketData] = None


def get_stock_market_data() -> StockMarketData:
    global _service
    if _service is None:
        _service = StockMarketData()
    return _service
