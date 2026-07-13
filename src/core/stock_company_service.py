"""个股详情：公司介绍、主营构成、资产负债表 — AKShare 拉取 + 本地持久化。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

from src.core.stock_company_store import get_stock_company_store
from src.core.stock_market_data import (
    _akshare_call,
    _akshare_import,
    _safe_float,
    is_akshare_available,
)
from src.core.stock_market_utils import MARKET_HK, MARKET_US, market_from_a_code, resolve_stock

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 3600.0

# 资产负债表关键科目（东方财富英文字段 -> 中文标签）
BALANCE_SHEET_ITEMS: list[tuple[str, str]] = [
    ("TOTAL_ASSETS", "资产总计"),
    ("TOTAL_CURRENT_ASSETS", "流动资产合计"),
    ("MONETARYFUNDS", "货币资金"),
    ("ACCOUNTS_RECE", "应收账款"),
    ("INVENTORIES", "存货"),
    ("TOTAL_NONCURRENT_ASSETS", "非流动资产合计"),
    ("FIXED_ASSETS", "固定资产"),
    ("GOODWILL", "商誉"),
    ("TOTAL_LIABILITIES", "负债合计"),
    ("TOTAL_CURRENT_LIAB", "流动负债合计"),
    ("SHORT_LOAN", "短期借款"),
    ("ACCOUNTS_PAYABLE", "应付账款"),
    ("TOTAL_NONCURRENT_LIAB", "非流动负债合计"),
    ("TOTAL_EQUITY", "所有者权益合计"),
    ("SHARE_CAPITAL", "股本"),
    ("RETAINED_EARNINGS", "未分配利润"),
]


def _normalize_code(code: str, *, market: str | None = None) -> str:
    normalized, _ = resolve_stock(code, market)
    return normalized


def _em_symbol(code: str) -> str:
    """6 位代码 -> SH600519 / SZ000001 / BJ430047。"""
    code = _normalize_code(code)
    if code.startswith("6"):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def _format_report_date(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if " " in text:
        text = text.split(" ")[0]
    return text[:10]


def _json_safe(value: Any) -> Any:
    import math

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _df_row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "to_dict"):
        import math

        out: dict[str, Any] = {}
        for k, v in row.to_dict().items():
            if isinstance(v, float) and math.isnan(v):
                out[str(k)] = None
            else:
                out[str(k)] = v
        return out
    return {}


class StockCompanyService:
    """个股公司资料与财务摘要。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._balance_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _cache_get(self, store: dict[str, tuple[float, dict[str, Any]]], key: str) -> Optional[dict[str, Any]]:
        cached = store.get(key)
        if not cached:
            return None
        ts, payload = cached
        if time.time() - ts < _CACHE_TTL_SECONDS:
            out = dict(payload)
            out["cached"] = True
            return out
        store.pop(key, None)
        return None

    def _cache_set(self, store: dict[str, tuple[float, dict[str, Any]]], key: str, payload: dict[str, Any]) -> None:
        store[key] = (time.time(), payload)

    def _fetch_individual_info(self, code: str) -> dict[str, str]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(
                lambda: ak.stock_individual_info_em(symbol=code),
                label=f"个股信息({code})",
            )
            info: dict[str, str] = {}
            for _, row in df.iterrows():
                key = str(row.get("item") or row.get("项目") or "")
                val = str(row.get("value") or row.get("值") or "")
                if key:
                    info[key] = val
            return info
        except Exception as exc:
            logger.debug("stock_individual_info_em 失败 %s: %s", code, exc)
            return {}

    def _fetch_profile_cninfo(self, code: str) -> dict[str, Any]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(
                lambda: ak.stock_profile_cninfo(symbol=code),
                label=f"公司概况({code})",
            )
            if df is None or df.empty:
                return {}
            return _df_row_to_dict(df.iloc[0])
        except Exception as exc:
            logger.debug("stock_profile_cninfo 失败 %s: %s", code, exc)
            return {}

    def _fetch_main_business(self, code: str, *, limit: int = 12) -> list[dict[str, Any]]:
        ak = _akshare_import()
        if ak is None:
            return []
        em_sym = _em_symbol(code)
        try:
            df = _akshare_call(
                lambda: ak.stock_zygc_em(symbol=em_sym),
                label=f"主营构成({code})",
            )
            if df is None or df.empty:
                return []
            latest_date = str(df["报告日期"].max()) if "报告日期" in df.columns else ""
            subset = df
            if latest_date:
                subset = df[df["报告日期"].astype(str) == latest_date]
            product_rows = subset
            if "分类类型" in subset.columns:
                by_product = subset[subset["分类类型"] == "按产品分类"]
                if not by_product.empty:
                    product_rows = by_product
            items: list[dict[str, Any]] = []
            for _, row in product_rows.head(limit).iterrows():
                items.append(
                    {
                        "name": str(row.get("主营构成") or ""),
                        "report_date": _format_report_date(row.get("报告日期")),
                        "category": str(row.get("分类类型") or ""),
                        "income": _safe_float(row.get("主营收入")),
                        "income_ratio": _safe_float(row.get("收入比例")),
                        "gross_margin": _safe_float(row.get("毛利率")),
                    }
                )
            return items
        except Exception as exc:
            logger.warning("主营构成拉取失败 %s: %s", code, exc)
            return []

    def _fetch_balance_sheet_raw(self, code: str) -> list[dict[str, Any]]:
        ak = _akshare_import()
        if ak is None:
            return []
        em_sym = _em_symbol(code)
        try:
            df = _akshare_call(
                lambda: ak.stock_balance_sheet_by_report_em(symbol=em_sym),
                label=f"资产负债表({code})",
            )
            if df is None or df.empty:
                return []
            periods: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                period = {
                    "report_date": _format_report_date(row.get("REPORT_DATE")),
                    "report_label": str(row.get("REPORT_DATE_NAME") or ""),
                    "items": {},
                }
                for key, label in BALANCE_SHEET_ITEMS:
                    if key in df.columns:
                        val = _safe_float(row.get(key))
                        if val is not None:
                            period["items"][key] = {"label": label, "value": val}
                periods.append(period)
            return periods
        except Exception as exc:
            logger.warning("资产负债表拉取失败 %s: %s", code, exc)
            return []

    def _build_balance_sheet_table(
        self,
        periods: list[dict[str, Any]],
        *,
        max_periods: int = 4,
    ) -> dict[str, Any]:
        if not periods:
            return {"periods": [], "rows": [], "count": 0}

        selected = periods[:max_periods]
        period_labels = [
            p.get("report_label") or p.get("report_date") or ""
            for p in selected
        ]
        rows: list[dict[str, Any]] = []
        for key, label in BALANCE_SHEET_ITEMS:
            values: list[Optional[float]] = []
            has_value = False
            for period in selected:
                item = (period.get("items") or {}).get(key)
                val = item.get("value") if item else None
                values.append(val)
                if val is not None:
                    has_value = True
            if has_value:
                rows.append({"key": key, "label": label, "values": values})
        return {
            "periods": period_labels,
            "report_dates": [p.get("report_date") for p in selected],
            "rows": rows,
            "count": len(selected),
        }

    def _fetch_quote(self, code: str, info: dict[str, str], profile: dict[str, Any]) -> dict[str, Any]:
        quote: dict[str, Any] = {
            "price": _safe_float(info.get("最新")),
            "change_pct": _safe_float(info.get("涨跌幅")),
            "change_amount": _safe_float(info.get("涨跌额")),
            "volume": _safe_float(info.get("成交量")),
            "amount": _safe_float(info.get("成交额")),
            "market_cap": _safe_float(info.get("总市值")),
            "pe_ratio": _safe_float(info.get("市盈率-动态") or info.get("市盈率")),
            "pb_ratio": _safe_float(info.get("市净率")),
            "source": None,
        }
        if quote["price"] is not None:
            quote["source"] = "individual_info_em"
            return quote

        ak = _akshare_import()
        if ak is None:
            return quote
        try:
            from src.core.stock_kline_service import get_stock_kline_service

            kline = get_stock_kline_service().get_kline(code, period="daily", limit=2, auto_fetch=True)
            items = kline.get("items") or []
            if items:
                last = items[-1]
                quote["price"] = _safe_float(last.get("close"))
                quote["change_pct"] = _safe_float(last.get("change_pct"))
                quote["change_amount"] = _safe_float(last.get("change_amount"))
                quote["volume"] = _safe_float(last.get("volume"))
                quote["amount"] = _safe_float(last.get("amount"))
                quote["source"] = "kline"
        except Exception as exc:
            logger.debug("行情降级 K线失败 %s: %s", code, exc)

        if quote["price"] is None and profile:
            quote["source"] = quote.get("source") or "none"
        return quote

    def _fetch_hk_company_profile(self, code: str) -> dict[str, Any]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(
                lambda: ak.stock_hk_company_profile_em(symbol=code),
                label=f"港股公司资料({code})",
            )
            if df is None or df.empty:
                return {}
            return _df_row_to_dict(df.iloc[0])
        except Exception as exc:
            logger.debug("stock_hk_company_profile_em 失败 %s: %s", code, exc)
            return {}

    def _fetch_hk_security_profile(self, code: str) -> dict[str, Any]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(
                lambda: ak.stock_hk_security_profile_em(symbol=code),
                label=f"港股证券资料({code})",
            )
            if df is None or df.empty:
                return {}
            return _df_row_to_dict(df.iloc[0])
        except Exception as exc:
            logger.debug("stock_hk_security_profile_em 失败 %s: %s", code, exc)
            return {}

    def _fetch_hk_financial_indicator(self, code: str) -> dict[str, Any]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(
                lambda: ak.stock_hk_financial_indicator_em(symbol=code),
                label=f"港股财务指标({code})",
            )
            if df is None or df.empty:
                return {}
            return _df_row_to_dict(df.iloc[0])
        except Exception as exc:
            logger.debug("stock_hk_financial_indicator_em 失败 %s: %s", code, exc)
            return {}

    def _fetch_detail_from_akshare_hk(self, code: str) -> dict[str, Any]:
        degraded_parts: list[str] = []
        profile = self._fetch_hk_company_profile(code)
        security = self._fetch_hk_security_profile(code)
        fin = self._fetch_hk_financial_indicator(code)

        if not profile:
            degraded_parts.append("profile")
        if not security:
            degraded_parts.append("security")
        if not fin:
            degraded_parts.append("financial_indicator")

        name = (
            str(profile.get("公司名称") or "")
            or str(security.get("证券简称") or "")
            or code
        )
        industry = str(profile.get("所属行业") or "")
        introduction = str(profile.get("公司介绍") or "").strip()

        quote: dict[str, Any] = {
            "price": None,
            "change_pct": None,
            "change_amount": None,
            "volume": None,
            "amount": None,
            "market_cap": _safe_float(fin.get("总市值(港元)")),
            "pe_ratio": _safe_float(fin.get("市盈率")),
            "pb_ratio": _safe_float(fin.get("市净率")),
            "dividend_yield": _safe_float(fin.get("股息率TTM(%)")),
            "source": "hk_financial_indicator" if fin else None,
        }

        financial_summary = {
            "operating_income": _safe_float(fin.get("营业总收入")),
            "net_profit": _safe_float(fin.get("净利润")),
            "roe": _safe_float(fin.get("股东权益回报率(%)")),
            "roa": _safe_float(fin.get("总资产回报率(%)")),
            "eps": _safe_float(fin.get("基本每股收益(元)")),
            "bps": _safe_float(fin.get("每股净资产(元)")),
        }

        return {
            "ok": bool(name or profile or security or fin),
            "code": code,
            "market": MARKET_HK,
            "em_symbol": f"{code}.HK",
            "name": name,
            "industry": industry,
            "quote": quote,
            "profile": {
                "introduction": introduction,
                "main_business": introduction,
                "scope": "",
                "listed_date": str(security.get("上市日期") or ""),
                "established_date": str(profile.get("公司成立日期") or ""),
                "website": str(profile.get("公司网址") or ""),
                "office_address": str(profile.get("办公地址") or ""),
                "legal_representative": str(profile.get("董事长") or ""),
                "english_name": str(profile.get("英文名称") or ""),
                "exchange": str(security.get("交易所") or ""),
                "board": str(security.get("板块") or ""),
            },
            "main_products": [],
            "financial_summary": financial_summary,
            "balance_sheet": {"periods": [], "rows": [], "count": 0},
            "_raw_balance_periods": [],
            "sources": {
                "profile": "eastmoney_hk_profile" if profile else None,
                "quote": quote.get("source"),
                "main_products": None,
                "balance_sheet": None,
                "financial_summary": "eastmoney_hk_indicator" if fin else None,
            },
            "degraded": bool(degraded_parts),
            "degraded_parts": degraded_parts,
            "cached": False,
            "fetched_at": datetime.now().isoformat(),
            "disclaimer": "数据来源于 AKShare 公开接口，仅供参考，不构成投资建议。",
        }

    def _fetch_us_org_profile(self, code: str) -> dict[str, Any]:
        try:
            import requests

            url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
            params = {
                "reportName": "RPT_USF10_INFO_ORGPROFILE",
                "columns": "SECUCODE,SECURITY_CODE,ORG_NAME,ORG_EN_ABBR,BELONG_INDUSTRY,"
                "FOUND_DATE,CHAIRMAN,REG_PLACE,ADDRESS,ORG_WEB,ORG_PROFILE",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": "5",
                "source": "SECURITIES",
                "client": "PC",
            }

            def _fetch() -> dict[str, Any]:
                r = requests.get(url, params=params, timeout=15)
                data = r.json()
                rows = (data.get("result") or {}).get("data") or []
                return rows[0] if rows else {}

            return _akshare_call(_fetch, label=f"美股公司简介({code})")
        except Exception as exc:
            logger.debug("美股公司简介失败 %s: %s", code, exc)
            return {}

    def _fetch_us_spot_quote(self, code: str) -> dict[str, Any]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(lambda: ak.stock_us_spot_em(), label="美股行情查询")
            target = code.upper()
            for _, row in df.iterrows():
                short = str(row.get("简称", "")).strip().upper()
                if short == target:
                    return _df_row_to_dict(row)
        except Exception as exc:
            logger.debug("美股行情查询失败 %s: %s", code, exc)
        return {}

    def _fetch_us_financial_indicator(self, code: str) -> dict[str, Any]:
        ak = _akshare_import()
        if ak is None:
            return {}
        try:
            df = _akshare_call(
                lambda: ak.stock_financial_us_analysis_indicator_em(symbol=code, indicator="年报"),
                label=f"美股财务指标({code})",
            )
            if df is None or df.empty:
                return {}
            return _df_row_to_dict(df.iloc[0])
        except Exception as exc:
            logger.debug("stock_financial_us_analysis_indicator_em 失败 %s: %s", code, exc)
            return {}

    def _fetch_detail_from_akshare_us(self, code: str) -> dict[str, Any]:
        degraded_parts: list[str] = []
        org = self._fetch_us_org_profile(code)
        spot = self._fetch_us_spot_quote(code)
        fin = self._fetch_us_financial_indicator(code)

        if not org and not spot:
            degraded_parts.append("profile")
        if not fin:
            degraded_parts.append("financial_indicator")

        name = (
            str(org.get("ORG_NAME") or org.get("SECURITY_NAME_ABBR") or "")
            or str(spot.get("名称") or "")
            or code
        )
        industry = str(org.get("BELONG_INDUSTRY") or "")
        introduction = str(org.get("ORG_PROFILE") or "").strip()

        quote: dict[str, Any] = {
            "price": _safe_float(spot.get("最新价")),
            "change_pct": _safe_float(spot.get("涨跌幅")),
            "change_amount": _safe_float(spot.get("涨跌额")),
            "volume": _safe_float(spot.get("成交量")),
            "amount": _safe_float(spot.get("成交额")),
            "market_cap": _safe_float(spot.get("总市值")),
            "pe_ratio": _safe_float(spot.get("市盈率")),
            "pb_ratio": None,
            "dividend_yield": None,
            "source": "us_spot_em" if spot else None,
        }

        financial_summary = {
            "operating_income": _safe_float(fin.get("TOTAL_INCOME")),
            "net_profit": _safe_float(fin.get("PARENT_HOLDER_NETPROFIT")),
            "roe": _safe_float(fin.get("ROE")),
            "roa": _safe_float(fin.get("ROA")),
            "eps": _safe_float(fin.get("BASIC_EPS_CS")),
            "bps": None,
        }

        return {
            "ok": bool(name or org or spot or fin),
            "code": code,
            "market": MARKET_US,
            "em_symbol": str(org.get("SECUCODE") or code),
            "name": name,
            "industry": industry,
            "quote": quote,
            "profile": {
                "introduction": introduction,
                "main_business": introduction,
                "scope": "",
                "listed_date": "",
                "established_date": str(org.get("FOUND_DATE") or ""),
                "website": str(org.get("ORG_WEB") or ""),
                "office_address": str(org.get("ADDRESS") or ""),
                "legal_representative": str(org.get("CHAIRMAN") or ""),
                "english_name": str(org.get("ORG_EN_ABBR") or ""),
                "exchange": "US",
                "board": "",
            },
            "main_products": [],
            "financial_summary": financial_summary,
            "balance_sheet": {"periods": [], "rows": [], "count": 0},
            "_raw_balance_periods": [],
            "sources": {
                "profile": "eastmoney_us_profile" if org else None,
                "quote": quote.get("source"),
                "main_products": None,
                "balance_sheet": None,
                "financial_summary": "eastmoney_us_indicator" if fin else None,
            },
            "degraded": bool(degraded_parts),
            "degraded_parts": degraded_parts,
            "cached": False,
            "fetched_at": datetime.now().isoformat(),
            "disclaimer": "数据来源于 AKShare 公开接口，仅供参考，不构成投资建议。",
        }

    def _fetch_detail_from_akshare(
        self,
        code: str,
        *,
        market: str,
        balance_periods: int = 4,
    ) -> dict[str, Any]:
        """从 AKShare 拉取并组装个股详情（不写缓存/存储）。"""
        if market == MARKET_HK:
            return self._fetch_detail_from_akshare_hk(code)
        if market == MARKET_US:
            return self._fetch_detail_from_akshare_us(code)

        degraded_parts: list[str] = []
        info = self._fetch_individual_info(code)
        if not info:
            degraded_parts.append("individual_info")

        profile = self._fetch_profile_cninfo(code)
        if not profile:
            degraded_parts.append("profile")

        name = (
            info.get("股票简称")
            or info.get("名称")
            or str(profile.get("A股简称") or "")
            or code
        )
        industry = info.get("行业") or str(profile.get("所属行业") or "")
        introduction = str(profile.get("机构简介") or "").strip()
        main_business_text = str(profile.get("主营业务") or profile.get("经营范围") or "").strip()
        if not introduction and main_business_text:
            introduction = main_business_text

        main_products = self._fetch_main_business(code)
        if not main_products and not main_business_text:
            degraded_parts.append("main_business")

        raw_balance = self._fetch_balance_sheet_raw(code)
        balance_sheet = self._build_balance_sheet_table(raw_balance, max_periods=balance_periods)
        if not balance_sheet.get("rows"):
            degraded_parts.append("balance_sheet")

        quote = self._fetch_quote(code, info, profile)

        return {
            "ok": bool(name or profile or main_products or balance_sheet.get("rows")),
            "code": code,
            "market": market_from_a_code(code),
            "em_symbol": _em_symbol(code),
            "name": name,
            "industry": industry,
            "quote": quote,
            "profile": {
                "introduction": introduction,
                "main_business": main_business_text,
                "scope": str(profile.get("经营范围") or ""),
                "listed_date": str(profile.get("上市日期") or info.get("上市时间") or ""),
                "established_date": str(profile.get("成立日期") or ""),
                "website": str(profile.get("官方网站") or ""),
                "office_address": str(profile.get("办公地址") or ""),
                "legal_representative": str(profile.get("法人代表") or ""),
            },
            "main_products": main_products,
            "balance_sheet": balance_sheet,
            "_raw_balance_periods": raw_balance,
            "sources": {
                "profile": "cninfo" if profile else None,
                "quote": quote.get("source"),
                "main_products": "eastmoney_zygc" if main_products else None,
                "balance_sheet": "eastmoney" if balance_sheet.get("rows") else None,
            },
            "degraded": bool(degraded_parts),
            "degraded_parts": degraded_parts,
            "cached": False,
            "fetched_at": datetime.now().isoformat(),
            "disclaimer": "数据来源于 AKShare 公开接口，仅供参考，不构成投资建议。",
        }

    def _attach_storage_meta(self, result: dict[str, Any], store_meta: dict[str, Any]) -> dict[str, Any]:
        out = dict(result)
        out["storage"] = store_meta.get("storage") or out.get("storage")
        out["last_sync"] = store_meta.get("last_sync") or out.get("last_sync")
        persisted = out.get("persisted")
        if persisted is None:
            persisted = bool(store_meta.get("ok")) or bool(out.get("persisted"))
        out["persisted"] = bool(persisted)
        out["meta"] = {
            "storage": out.get("storage"),
            "last_sync": out.get("last_sync"),
            "persisted": out["persisted"],
        }
        out.pop("_raw_balance_periods", None)
        return out

    def sync_company_detail(
        self,
        code: str,
        *,
        market: str | None = None,
        balance_periods: int = 4,
    ) -> dict[str, Any]:
        """强制从 AKShare 拉取并写入本地存储。"""
        code, market = resolve_stock(code, market)
        if not code:
            return {"ok": False, "error": "code 必填"}
        if not is_akshare_available():
            return {"ok": False, "code": code, "market": market, "error": "akshare 未安装或未启用", "degraded": True}

        result = self._fetch_detail_from_akshare(code, market=market, balance_periods=balance_periods)
        store_meta = get_stock_company_store().save_company_detail(code, result, market=market)
        cache_key = f"detail:{market}:{code}:{balance_periods}"
        self._cache.pop(cache_key, None)
        out = self._attach_storage_meta(result, store_meta)
        out["synced"] = True
        out["persisted"] = store_meta.get("ok", False)
        return _json_safe(out)

    def sync_company_batch(
        self,
        codes: list[str],
        *,
        market: str | None = None,
        balance_periods: int = 4,
    ) -> dict[str, Any]:
        """批量同步个股公司资料。"""
        results: list[dict[str, Any]] = []
        ok_count = 0
        for raw in codes:
            code, m = resolve_stock(raw, market)
            if not code:
                continue
            item = self.sync_company_detail(code, market=m, balance_periods=balance_periods)
            results.append({"code": code, **item})
            if item.get("ok"):
                ok_count += 1
        return {
            "ok": ok_count > 0,
            "total": len(results),
            "success": ok_count,
            "results": results,
            "synced_at": datetime.now().isoformat(),
        }

    def get_balance_sheet(
        self,
        code: str,
        *,
        market: str | None = None,
        periods: int = 4,
        refresh: bool = False,
    ) -> dict[str, Any]:
        code, market = resolve_stock(code, market)
        if not code:
            return {"ok": False, "error": "code 必填"}
        if market == MARKET_HK:
            return {
                "ok": False,
                "code": code,
                "market": market,
                "balance_sheet": {"periods": [], "rows": [], "count": 0},
                "degraded": True,
                "message": "港股暂无 A 股式资产负债表接口",
                "disclaimer": "财务数据来源于公开披露，仅供参考。",
            }
        if market == MARKET_US:
            return {
                "ok": False,
                "code": code,
                "market": market,
                "balance_sheet": {"periods": [], "rows": [], "count": 0},
                "degraded": True,
                "message": "美股资产负债表需单独报表接口，当前仅展示核心财务指标",
                "disclaimer": "财务数据来源于公开披露，仅供参考。",
            }
        cache_key = f"{market}:{code}:{periods}"
        if refresh:
            self._balance_cache.pop(cache_key, None)
        cached = self._cache_get(self._balance_cache, cache_key)
        if cached is not None:
            return _json_safe(cached)

        store = get_stock_company_store()
        if not refresh:
            stored = store.load_company_detail(code, market=market)
            if stored and stored.get("balance_sheet"):
                table = stored["balance_sheet"]
                max_p = max(1, min(periods, 8))
                if table.get("rows") and len(table.get("periods") or []) > max_p:
                    table = {
                        **table,
                        "periods": (table.get("periods") or [])[:max_p],
                        "report_dates": (table.get("report_dates") or [])[:max_p],
                        "rows": [
                            {**r, "values": (r.get("values") or [])[:max_p]}
                            for r in table.get("rows") or []
                        ],
                        "count": max_p,
                    }
                result = {
                    "ok": bool(table.get("rows")),
                    "code": code,
                    "em_symbol": stored.get("em_symbol") or _em_symbol(code),
                    "balance_sheet": table,
                    "degraded": stored.get("degraded", False),
                    "cached": False,
                    "persisted": True,
                    "storage": stored.get("storage"),
                    "last_sync": stored.get("last_sync"),
                    "fetched_at": stored.get("fetched_at"),
                    "disclaimer": "财务数据来源于公开披露，仅供参考。",
                }
                self._cache_set(self._balance_cache, cache_key, result)
                return _json_safe(result)

        if not is_akshare_available():
            stale = store.load_company_detail(code, market=market, ignore_ttl=True)
            if stale and stale.get("balance_sheet"):
                table = stale["balance_sheet"]
                return _json_safe({
                    "ok": bool(table.get("rows")),
                    "code": code,
                    "em_symbol": stale.get("em_symbol") or _em_symbol(code),
                    "balance_sheet": table,
                    "degraded": True,
                    "offline": True,
                    "storage": stale.get("storage"),
                    "last_sync": stale.get("last_sync"),
                    "disclaimer": "财务数据来源于公开披露，仅供参考。",
                })
            return {"ok": False, "code": code, "error": "akshare 未安装或未启用", "degraded": True}

        raw = self._fetch_balance_sheet_raw(code)
        table = self._build_balance_sheet_table(raw, max_periods=max(1, min(periods, 8)))
        result = {
            "ok": bool(table.get("rows")),
            "code": code,
            "em_symbol": _em_symbol(code),
            "balance_sheet": table,
            "degraded": not bool(table.get("rows")),
            "cached": False,
            "fetched_at": datetime.now().isoformat(),
            "disclaimer": "财务数据来源于公开披露，仅供参考。",
        }
        self._cache_set(self._balance_cache, cache_key, result)
        return _json_safe(result)

    def get_stock_detail(
        self,
        code: str,
        *,
        market: str | None = None,
        balance_periods: int = 4,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """统一个股详情 JSON：本地 DB 优先，过期或缺失则 AKShare 拉取并写入。"""
        code, market = resolve_stock(code, market)
        if not code:
            return {"ok": False, "error": "code 必填"}

        cache_key = f"detail:{market}:{code}:{balance_periods}"
        if refresh:
            self._cache.pop(cache_key, None)
        else:
            cached = self._cache_get(self._cache, cache_key)
            if cached is not None:
                return _json_safe(cached)

        store = get_stock_company_store()
        if not refresh:
            stored = store.load_company_detail(code, market=market)
            if stored is not None:
                meta = store.get_meta(code, market=market)
                out = self._attach_storage_meta(stored, meta)
                self._cache_set(self._cache, cache_key, out)
                return _json_safe(out)

        if not is_akshare_available():
            stale = store.load_company_detail(code, market=market, ignore_ttl=True)
            if stale is not None:
                meta = store.get_meta(code, market=market)
                out = self._attach_storage_meta(stale, meta)
                out["degraded"] = True
                out["offline"] = True
                return _json_safe(out)
            return {"ok": False, "code": code, "error": "akshare 未安装或未启用", "degraded": True}

        result = self._fetch_detail_from_akshare(code, market=market, balance_periods=balance_periods)
        store_meta = store.save_company_detail(code, result, market=market)
        out = self._attach_storage_meta(result, store_meta)
        out["persisted"] = store_meta.get("ok", False)
        self._cache_set(self._cache, cache_key, out)
        return _json_safe(out)


_service: Optional[StockCompanyService] = None


def get_stock_company_service() -> StockCompanyService:
    global _service
    if _service is None:
        _service = StockCompanyService()
    return _service
