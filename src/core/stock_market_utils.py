"""A 股 / 港股 / 美股代码与市场识别工具。"""

from __future__ import annotations

import re

from src.core.config import get_settings

MARKETS_A = frozenset({"SH", "SZ", "BJ"})
MARKET_HK = "HK"
MARKET_US = "US"

_us_secid_map: dict[str, str] = {}


def is_hk_enabled() -> bool:
    return bool(get_settings().stock_hk_enabled)


def is_us_enabled() -> bool:
    return bool(get_settings().stock_us_enabled)


def _digits_only(code: str) -> str:
    return re.sub(r"\D", "", (code or "").strip())


def _strip_market_prefix(raw: str) -> str:
    lower = raw.lower()
    for prefix in ("hk", "sh", "sz", "bj", "us"):
        if lower.startswith(prefix) and len(lower) > len(prefix):
            return raw[len(prefix) :]
    return raw


def normalize_hk_code(code: str) -> str:
    raw = _strip_market_prefix((code or "").strip())
    digits = _digits_only(raw)
    return digits.zfill(5) if digits else raw


def normalize_a_code(code: str) -> str:
    raw = _strip_market_prefix((code or "").strip())
    digits = _digits_only(raw)
    return digits.zfill(6) if digits else raw


def normalize_us_symbol(code: str) -> str:
    """美股 ticker，如 AAPL；支持 105.AAPL 形态。"""
    raw = _strip_market_prefix((code or "").strip())
    if not raw:
        return ""
    if "." in raw:
        parts = raw.split(".")
        ticker = parts[-1]
        if ticker and not ticker.isdigit():
            return ticker.upper()
        if len(parts) == 2 and parts[0].isdigit():
            register_us_secid(parts[1].upper(), raw)
            return parts[1].upper()
    return raw.upper()


def register_us_secid(ticker: str, secid: str) -> None:
    """登记 ticker -> AKShare secid（如 105.AAPL）。"""
    t = normalize_us_symbol(ticker)
    s = (secid or "").strip()
    if t and s:
        _us_secid_map[t] = s


def resolve_us_secid(ticker: str) -> str:
    """解析美股 K 线 secid；未命中时返回 ticker 本身。"""
    t = normalize_us_symbol(ticker)
    if not t:
        return ""
    cached = _us_secid_map.get(t)
    if cached:
        return cached
    raw = (ticker or "").strip()
    if re.match(r"^\d+\.\w+", raw):
        register_us_secid(t, raw)
        return raw
    return t


def is_us_ticker(code: str) -> bool:
    raw = _strip_market_prefix((code or "").strip())
    if not raw:
        return False
    if "." in raw:
        tail = raw.split(".")[-1]
        if tail and not tail.isdigit() and re.search(r"[A-Za-z]", tail):
            return True
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,9}", raw))


def market_from_a_code(code: str) -> str:
    code = normalize_a_code(code)
    if code.startswith("6"):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def detect_market_from_code(code: str) -> str:
    """根据代码形态推断市场：字母为美股，5 位数字为港股，6 位为 A 股。"""
    raw = _strip_market_prefix((code or "").strip())
    lower = raw.lower()
    if lower.startswith("hk"):
        return MARKET_HK
    if lower.startswith("us") or is_us_ticker(raw):
        if is_us_enabled():
            return MARKET_US
    digits = _digits_only(raw)
    if not digits:
        return "SZ"
    if len(digits) <= 5:
        return MARKET_HK
    return market_from_a_code(digits)


def resolve_stock(code: str, market: str | None = None) -> tuple[str, str]:
    """解析 (规范化代码, 市场)。"""
    explicit = (market or "").strip().upper()
    if explicit == MARKET_HK:
        return normalize_hk_code(code), MARKET_HK
    if explicit == MARKET_US:
        return normalize_us_symbol(code), MARKET_US
    if explicit in MARKETS_A:
        return normalize_a_code(code), explicit

    detected = detect_market_from_code(code)
    if detected == MARKET_HK:
        if not is_hk_enabled():
            return normalize_a_code(code), market_from_a_code(code)
        return normalize_hk_code(code), MARKET_HK
    if detected == MARKET_US:
        return normalize_us_symbol(code), MARKET_US
    normalized = normalize_a_code(code)
    return normalized, market_from_a_code(normalized)


def is_hk_market(market: str | None) -> bool:
    return (market or "").strip().upper() == MARKET_HK


def is_us_market(market: str | None) -> bool:
    return (market or "").strip().upper() == MARKET_US


def is_overseas_market(market: str | None) -> bool:
    m = (market or "").strip().upper()
    return m in (MARKET_HK, MARKET_US)
