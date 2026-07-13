"""板块产业链分析：关联板块成分股聚合 + 预设模板 + 可选 LLM 综述。"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.core.stock_market_utils import market_from_a_code

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_PATH = ROOT / "data" / "industry_chain_templates.json"
CACHE_JSON_PATH = ROOT / "data" / "sector_industry_chain_cache.json"
COLLECTION = "sector_industry_chain"
CACHE_TTL = 48 * 3600  # 48h

DISCLAIMER = (
    "产业链分析基于公开板块成分股与预设/申万层级关联关系聚合生成，"
    "不代表完整产业图谱，仅供参考，不构成任何投资建议。"
)

_service: Optional["StockIndustryChainService"] = None


def get_stock_industry_chain_service() -> "StockIndustryChainService":
    global _service
    if _service is None:
        _service = StockIndustryChainService()
    return _service


class StockIndustryChainService:
    """板块上下游产业链分析。"""

    def __init__(self) -> None:
        self._templates = self._load_templates()
        self._settings = None

    def _settings_obj(self):
        if self._settings is None:
            from src.core.config import get_settings

            self._settings = get_settings()
        return self._settings

    @staticmethod
    def _load_templates() -> dict[str, Any]:
        if not TEMPLATES_PATH.exists():
            return {}
        try:
            return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取产业链模板失败: %s", exc)
            return {}

    def get_sector_industry_chain(
        self,
        classify: str,
        symbol: str,
        code: str | None = None,
        *,
        cons_limit: int = 5,
    ) -> dict[str, Any]:
        """返回板块产业链结构化数据。"""
        from src.core.stock_market_data import SECTOR_CLASSIFICATIONS, get_stock_market_data

        classify = (classify or "industry").strip()
        name = (symbol or "").strip()
        index_code = (code or "").strip().replace(".SI", "") or None
        cache_key = f"{classify}:{index_code or name}"

        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        meta = SECTOR_CLASSIFICATIONS.get(classify, SECTOR_CLASSIFICATIONS["industry"])
        market_data = get_stock_market_data()

        self_constituents = self._fetch_constituents(
            market_data, classify, name or (index_code or ""), index_code, max(cons_limit, 10)
        )
        resolved_name = name or (index_code or "")
        if self_constituents and not resolved_name:
            resolved_name = index_code or ""

        # 若仅有 code，尝试从申万/板块列表解析名称
        if index_code and classify.startswith("sw") and market_data.akshare_enabled:
            try:
                import akshare as ak

                meta = market_data._fetch_sw_sector_meta(ak, index_code, name, classify)
                resolved_name = str(meta.get("name") or resolved_name)
            except Exception:
                pass

        resolved_code = index_code

        plan = self._resolve_chain_plan(
            classify=classify,
            name=resolved_name,
            code=resolved_code,
            self_constituents=self_constituents,
        )

        intro: dict[str, Any] = {}
        if market_data.akshare_enabled:
            try:
                intro = market_data.get_sector_intro(
                    resolved_name,
                    classify=classify,
                    code=index_code,
                    constituents=self_constituents,
                )
            except Exception:
                intro = {}

        upstream = self._build_segments(
            plan.get("upstream") or [],
            self_constituents=self_constituents,
            cons_limit=cons_limit,
            market_data=market_data,
        )
        midstream = self._build_segments(
            plan.get("midstream") or [{"name": resolved_name, "role": "核心环节", "self": True}],
            self_constituents=self_constituents,
            cons_limit=cons_limit,
            market_data=market_data,
        )
        downstream = self._build_segments(
            plan.get("downstream") or [],
            self_constituents=self_constituents,
            cons_limit=cons_limit,
            market_data=market_data,
        )

        data_sources: set[str] = set()
        if any(s.get("companies") for s in upstream + midstream + downstream):
            data_sources.add("akshare")
        if plan.get("_from_template"):
            data_sources.add("aggregated")
        if plan.get("_from_sw"):
            data_sources.add("aggregated")

        analysis, llm_used = self._build_analysis(
            sector_name=resolved_name,
            classify=classify,
            meta=meta,
            upstream=upstream,
            midstream=midstream,
            downstream=downstream,
            intro=intro,
        )
        if llm_used:
            data_sources.add("llm")

        data_source = "+".join(sorted(data_sources)) if data_sources else "aggregated"
        if not upstream and not downstream and not midstream:
            data_source = "none"

        result = {
            "ok": bool(upstream or midstream or downstream),
            "sector": {"name": resolved_name, "classify": classify, "code": resolved_code},
            "upstream": upstream,
            "midstream": midstream,
            "downstream": downstream,
            "analysis": analysis,
            "data_source": data_source,
            "disclaimer": DISCLAIMER,
            "cached": False,
            "fetched_at": datetime.now().isoformat(),
        }
        if not result["ok"]:
            result["analysis"] = "暂无产业链数据，该板块尚未配置关联模板或申万层级信息不足。"

        self._set_cache(cache_key, result)
        return result

    def _resolve_chain_plan(
        self,
        *,
        classify: str,
        name: str,
        code: str | None,
        self_constituents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """解析产业链环节规划：模板 > 申万层级 > 空。"""
        tpl = self._templates.get(name)
        if tpl:
            return {**tpl, "_from_template": True}

        if classify.startswith("sw"):
            sw_plan = self._build_sw_plan(classify, name, code)
            if sw_plan:
                return {**sw_plan, "_from_sw": True}

        # 模糊匹配模板（如「其他小金属」含「小金属」）
        for key, val in self._templates.items():
            if key in name or name in key:
                return {**val, "_from_template": True}

        return {
            "upstream": [],
            "midstream": [{"name": name, "role": "当前板块", "self": True}],
            "downstream": [],
        }

    def _build_sw_plan(
        self,
        classify: str,
        name: str,
        code: str | None,
    ) -> dict[str, Any] | None:
        from src.core.stock_market_data import get_stock_market_data, is_akshare_available

        if not is_akshare_available():
            return None

        import akshare as ak

        market_data = get_stock_market_data()
        index_code = (code or name).replace(".SI", "")

        try:
            if classify == "sw_l3":
                meta = market_data._fetch_sw_sector_meta(ak, index_code, name, classify)
                parent = str(meta.get("parent") or "").strip()
                upstream: list[dict[str, Any]] = []
                if parent:
                    upstream.append(
                        {
                            "name": parent,
                            "role": "申万上级行业",
                            "sector": {"classify": "sw_l2", "symbol": parent},
                        }
                    )
                return {
                    "upstream": upstream,
                    "midstream": [{"name": name, "role": "申万三级细分", "self": True}],
                    "downstream": [],
                }
            if classify == "sw_l2":
                df2 = ak.sw_index_second_info()
                row = df2[df2["行业名称"] == name]
                if row.empty and code:
                    row = df2[
                        df2["行业代码"].astype(str).str.replace(".SI", "", regex=False)
                        == index_code
                    ]
                parent = str(row.iloc[0]["上级行业"]) if not row.empty else ""
                upstream = (
                    [{"name": parent, "role": "申万一级", "sector": {"classify": "sw_l1", "symbol": parent}}]
                    if parent
                    else []
                )
                downstream: list[dict[str, Any]] = []
                try:
                    df3 = ak.sw_index_third_info()
                    children = df3[df3["上级行业"] == name]["行业名称"].tolist()[:3]
                    for child in children:
                        downstream.append(
                            {"name": child, "role": "申万三级细分", "sector": {"classify": "sw_l3", "symbol": child}}
                        )
                except Exception as exc:
                    logger.debug("申万三级子行业查询失败: %s", exc)
                return {
                    "upstream": upstream,
                    "midstream": [{"name": name, "role": "申万二级行业", "self": True}],
                    "downstream": downstream,
                }
            if classify == "sw_l1":
                downstream = []
                try:
                    df2 = ak.sw_index_second_info()
                    children = df2[df2["上级行业"] == name]["行业名称"].tolist()[:4]
                    for child in children:
                        downstream.append(
                            {"name": child, "role": "申万二级子行业", "sector": {"classify": "sw_l2", "symbol": child}}
                        )
                except Exception as exc:
                    logger.debug("申万二级子行业查询失败: %s", exc)
                return {
                    "upstream": [],
                    "midstream": [{"name": name, "role": "申万一级行业", "self": True}],
                    "downstream": downstream,
                }
        except Exception as exc:
            logger.warning("申万产业链规划失败 %s: %s", name, exc)
        return None

    def _build_segments(
        self,
        segments: list[dict[str, Any]],
        *,
        self_constituents: list[dict[str, Any]],
        cons_limit: int,
        market_data: Any,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for seg in segments:
            companies: list[dict[str, Any]] = []
            if seg.get("self"):
                companies = self._format_companies(self_constituents[:cons_limit])
            elif seg.get("sector"):
                companies = self._fetch_sector_companies(
                    market_data,
                    seg["sector"],
                    cons_limit=cons_limit,
                )
            out.append(
                {
                    "name": seg.get("name") or "",
                    "role": seg.get("role") or "",
                    "companies": companies,
                }
            )
        return out

    def _fetch_constituents(
        self,
        market_data: Any,
        classify: str,
        symbol: str,
        code: str | None,
        cons_limit: int,
    ) -> list[dict[str, Any]]:
        if not market_data.akshare_enabled:
            return []
        from src.core.stock_market_data import _akshare_call, _safe_float, _akshare_import

        ak = _akshare_import()
        if ak is None:
            return []
        index_code = (code or symbol).replace(".SI", "")
        try:
            if classify == "concept":
                return self._fetch_em_cons(ak, symbol, "concept", cons_limit)
            if classify == "industry":
                return self._fetch_em_cons(ak, symbol, "industry", cons_limit)
            if classify.startswith("sw"):
                cons_df = _akshare_call(
                    lambda: ak.index_component_sw(symbol=index_code),
                    label=f"申万成分({index_code})",
                )
                out: list[dict[str, Any]] = []
                for _, row in cons_df.head(cons_limit).iterrows():
                    out.append(
                        {
                            "code": str(row.get("证券代码") or ""),
                            "name": str(row.get("证券名称") or ""),
                            "weight": _safe_float(row.get("最新权重")),
                        }
                    )
                return out
        except Exception as exc:
            logger.debug("成分股拉取失败 %s/%s: %s", classify, symbol, exc)
        return []

    @staticmethod
    def _fetch_em_cons(ak: Any, name: str, board: str, cons_limit: int) -> list[dict[str, Any]]:
        from src.core.stock_market_data import _akshare_call, _safe_float

        cons_fn = (
            ak.stock_board_concept_cons_em if board == "concept" else ak.stock_board_industry_cons_em
        )
        label = "概念" if board == "concept" else "行业"
        cons_df = _akshare_call(lambda: cons_fn(symbol=name), label=f"{label}成分({name})")
        out: list[dict[str, Any]] = []
        for _, row in cons_df.head(cons_limit).iterrows():
            out.append(
                {
                    "code": str(row.get("代码") or ""),
                    "name": str(row.get("名称") or ""),
                    "change_pct": _safe_float(row.get("涨跌幅")),
                }
            )
        return out

    def _fetch_sector_companies(
        self,
        market_data: Any,
        sector: dict[str, Any],
        *,
        cons_limit: int,
    ) -> list[dict[str, Any]]:
        classify = str(sector.get("classify") or "industry")
        symbol = str(sector.get("symbol") or "")
        code = sector.get("code")
        if not symbol and not code:
            return []
        try:
            cons = self._fetch_constituents(market_data, classify, symbol, code, cons_limit)
            return self._format_companies(cons[:cons_limit])
        except Exception as exc:
            logger.debug("关联板块成分获取失败 %s/%s: %s", classify, symbol, exc)
            return []

    @staticmethod
    def _format_companies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            code = str(row.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(
                {
                    "code": code,
                    "name": str(row.get("name") or ""),
                    "market": market_from_a_code(code),
                }
            )
        return out

    def _build_analysis(
        self,
        *,
        sector_name: str,
        classify: str,
        meta: dict[str, str],
        upstream: list[dict[str, Any]],
        midstream: list[dict[str, Any]],
        downstream: list[dict[str, Any]],
        intro: dict[str, Any],
    ) -> tuple[str, bool]:
        parts: list[str] = []
        parts.append(f"{sector_name}（{meta.get('label', classify)}）产业链概览。")

        if upstream:
            up_names = "、".join(s["name"] for s in upstream if s.get("name"))
            parts.append(f"上游主要包括{up_names}等环节，提供原材料、基础资源或核心器件支撑。")
        if midstream:
            mid = midstream[0]
            cos = mid.get("companies") or []
            if cos:
                rep = "、".join(c["name"] for c in cos[:3] if c.get("name"))
                parts.append(f"中游{mid.get('name') or sector_name}代表企业有{rep}等。")
        if downstream:
            down_names = "、".join(s["name"] for s in downstream if s.get("name"))
            parts.append(f"下游延伸至{down_names}等应用领域，承接终端需求与价值转化。")

        chain_hint = str(intro.get("industry_chain") or "").strip()
        if chain_hint:
            parts.append(f"分类路径：{chain_hint}。")

        rule_text = "".join(parts)
        llm_text = self._try_llm_analysis(
            sector_name=sector_name,
            classify=classify,
            upstream=upstream,
            midstream=midstream,
            downstream=downstream,
            rule_text=rule_text,
        )
        if llm_text:
            return llm_text, True
        return rule_text, False

    def _try_llm_analysis(
        self,
        *,
        sector_name: str,
        classify: str,
        upstream: list[dict[str, Any]],
        midstream: list[dict[str, Any]],
        downstream: list[dict[str, Any]],
        rule_text: str,
    ) -> Optional[str]:
        from src.core.llm_chat import LLMChatClient

        client = LLMChatClient()
        if not client.available:
            return None

        def _seg_summary(segs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "name": s.get("name"),
                    "role": s.get("role"),
                    "companies": [c.get("name") for c in (s.get("companies") or [])[:5]],
                }
                for s in segs
            ]

        payload = {
            "sector": sector_name,
            "classify": classify,
            "upstream": _seg_summary(upstream),
            "midstream": _seg_summary(midstream),
            "downstream": _seg_summary(downstream),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 A 股产业链分析助手。仅基于给定真实板块与公司名称撰写 150-250 字中文综述，"
                    "说明上下游关系与价值传导，不要编造未提供的公司或数据。"
                ),
            },
            {
                "role": "user",
                "content": f"数据：{json.dumps(payload, ensure_ascii=False)}\n参考：{rule_text}",
            },
        ]
        try:
            content, err = client.chat(messages, temperature=0.2, timeout=45)
            if content and not err:
                return content.strip()
        except Exception as exc:
            logger.debug("LLM 产业链综述失败: %s", exc)
        return None

    # --- cache: Mongo 优先，JSON 降级 ---

    def _mongo_coll(self):
        settings = self._settings_obj()
        uri = settings.mongodb_uri
        if not uri:
            return None
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            client.admin.command("ping")
            return client[settings.mongodb_db][COLLECTION]
        except Exception as exc:
            logger.debug("MongoDB %s 不可用: %s", COLLECTION, exc)
            return None

    def _cache_key_doc(self, cache_key: str) -> dict[str, str]:
        return {"_id": cache_key}

    def _get_cache(self, cache_key: str) -> Optional[dict[str, Any]]:
        coll = self._mongo_coll()
        if coll is not None:
            try:
                doc = coll.find_one(self._cache_key_doc(cache_key))
                if doc and time.time() - doc.get("cached_at", 0) < CACHE_TTL:
                    payload = dict(doc.get("payload") or {})
                    payload["cached"] = True
                    payload["storage"] = "mongodb"
                    return payload
            except Exception as exc:
                logger.debug("Mongo 产业链缓存读取失败: %s", exc)

        store = self._read_json_cache()
        entry = store.get(cache_key)
        if entry and time.time() - entry.get("cached_at", 0) < CACHE_TTL:
            payload = dict(entry.get("payload") or {})
            payload["cached"] = True
            payload["storage"] = "json"
            return payload
        return None

    def _set_cache(self, cache_key: str, payload: dict[str, Any]) -> None:
        record = {"cached_at": time.time(), "payload": payload}
        coll = self._mongo_coll()
        if coll is not None:
            try:
                coll.replace_one(
                    self._cache_key_doc(cache_key),
                    {"_id": cache_key, **record},
                    upsert=True,
                )
                return
            except Exception as exc:
                logger.warning("Mongo 产业链缓存写入失败: %s", exc)

        store = self._read_json_cache()
        store[cache_key] = record
        self._write_json_cache(store)

    @staticmethod
    def _read_json_cache() -> dict[str, Any]:
        if not CACHE_JSON_PATH.exists():
            return {}
        try:
            return json.loads(CACHE_JSON_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _write_json_cache(store: dict[str, Any]) -> None:
        CACHE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_JSON_PATH.write_text(
            json.dumps(store, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
