"""政府政策与十五五投资方向分析。"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.core.policy_news_fetcher import DISCLAIMER, get_policy_news_fetcher
from src.core.policy_sources import (
    get_policy_scope_label,
    get_policy_themes,
    load_policy_sources_config,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_INSIGHTS_CACHE = ROOT / "data" / "policy_insights_cache.json"


class PolicyInsightsService:
    """聚合政府政策资讯，生成十五五投资方向报告。"""

    def get_latest(
        self,
        *,
        limit: int = 30,
        days: int = 30,
        theme: Optional[str] = None,
    ) -> dict[str, Any]:
        data = get_policy_news_fetcher().list_news(limit=limit, days=days, theme=theme)
        return {
            **data,
            "scope_label": get_policy_scope_label(),
            "generated_at": datetime.now().isoformat(),
        }

    def get_summary(self, *, days: int = 30) -> dict[str, Any]:
        days = max(1, min(days, 90))
        news = get_policy_news_fetcher().list_news(limit=100, days=days)
        items = news.get("items") or []

        source_counter: Counter[str] = Counter()
        theme_counter: Counter[str] = Counter()
        keyword_counter: Counter[str] = Counter()
        source_names: dict[str, str] = {}

        for item in items:
            sid = str(item.get("source_id") or "unknown")
            source_counter[sid] += 1
            source_names[sid] = str(item.get("source_name") or sid)
            for th in item.get("themes") or []:
                theme_counter[str(th)] += 1
            for kw in item.get("keywords") or []:
                keyword_counter[str(kw)] += 1

        by_source = [
            {"id": sid, "name": source_names.get(sid) or sid, "count": cnt}
            for sid, cnt in source_counter.most_common()
        ]
        by_theme = [
            {"name": name, "count": cnt} for name, cnt in theme_counter.most_common()
        ]
        hot_keywords = [
            {"keyword": kw, "count": cnt} for kw, cnt in keyword_counter.most_common(12)
        ]

        return {
            "days": days,
            "policy_count": len(items),
            "by_source": by_source,
            "by_theme": by_theme,
            "hot_keywords": hot_keywords,
            "storage": news.get("storage"),
            "degraded": news.get("storage") == "mock"
            or not (news.get("items") or []),
            "scope_label": get_policy_scope_label(),
            "enabled_sources": news.get("enabled_sources"),
            "generated_at": datetime.now().isoformat(),
            "disclaimer": DISCLAIMER,
        }

    def _llm_available(self) -> bool:
        from src.core.llm_chat import LLMChatClient

        return LLMChatClient().available

    def _generate_investment_direction_llm(
        self,
        summary: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        from src.core.llm_chat import LLMChatClient

        client = LLMChatClient()
        if not client.available:
            return None

        payload = {
            "context": "第十五个五年规划（2026-2030）编制与发布阶段",
            "window_days": summary.get("days"),
            "policy_count": summary.get("policy_count"),
            "by_theme": summary.get("by_theme", [])[:10],
            "by_source": summary.get("by_source", [])[:8],
            "hot_keywords": summary.get("hot_keywords", [])[:12],
            "sample_policies": [
                {
                    "title": i.get("title"),
                    "source": i.get("source_name"),
                    "themes": i.get("themes"),
                    "summary": (i.get("summary") or "")[:200],
                }
                for i in items[:15]
            ],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是宏观政策与产业投资研究分析师，结合国内政府最新政策与「十五五」"
                    "（第十五个五年规划，2026-2030）编制背景，输出 JSON："
                    "summary(150字内总述), sectors(重点产业数组,含name/logic/horizon), "
                    "themes(政策主题数组,含name/signal/strength 1-10), "
                    "policy_signals(政策信号数组,3-6条), "
                    "investment_horizon(投资时间维度,如2026-2030分阶段), "
                    "regional_focus(区域重点数组), risks(风险数组,3-5条), "
                    "markdown(完整 markdown 投资方向报告,含二级标题), "
                    f"disclaimer(固定为: {DISCLAIMER})。"
                    "只基于给定政策样本归纳，禁止编造未出现的具体政策文件编号或买卖建议。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        content, err = client.chat(messages, temperature=0.25, timeout=90)
        if err or not content:
            logger.warning("投资方向 LLM 失败: %s", err)
            return None
        text = content.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"summary": content[:600], "markdown": content[:2500], "disclaimer": DISCLAIMER}
        data.setdefault("disclaimer", DISCLAIMER)
        return data

    def _fallback_investment_direction(
        self,
        summary: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        themes = summary.get("by_theme") or []
        top_themes = [t.get("name") for t in themes[:5] if t.get("name")]
        sectors = top_themes or ["新质生产力", "数字经济", "双碳绿色"]
        md = (
            "## 十五五投资方向摘要（统计 fallback）\n\n"
            f"- 近 {summary.get('days')} 天政策相关条目 {summary.get('policy_count', 0)} 条\n"
            f"- 热点主题：{', '.join(top_themes) or '—'}\n\n"
            "### 关注方向\n"
            + "\n".join(f"- **{s}**：与十五五产业政策导向相关" for s in sectors[:5])
            + f"\n\n> {DISCLAIMER}\n"
        )
        return {
            "summary": (
                f"基于近{summary.get('days')}天{summary.get('policy_count', 0)}条政府政策信息，"
                f"十五五阶段可关注{','.join(sectors[:3])}等主题方向。"
            ),
            "sectors": [
                {"name": s, "logic": "政策频次与十五五主题匹配", "horizon": "2026-2030"}
                for s in sectors[:5]
            ],
            "themes": [
                {"name": t.get("name"), "signal": "政策提及", "strength": min(10, t.get("count", 1))}
                for t in themes[:6]
            ],
            "policy_signals": [
                i.get("title", "")[:80] for i in items[:5] if i.get("title")
            ] or ["十五五规划纲要编制工作持续推进"],
            "investment_horizon": {
                "near": "2026-2027：政策密集发布期，关注规划细则落地",
                "mid": "2028-2029：产业投资兑现期",
                "long": "2030：阶段性目标评估与调整",
            },
            "regional_focus": ["京津冀", "长三角", "粤港澳大湾区"],
            "risks": [
                "政策解读存在滞后与偏差",
                "网络抓取失败时数据为演示/mock",
                "产业政策落地节奏不确定",
            ],
            "data_note": "LLM 未启用或失败，当前为统计 fallback。",
            "markdown": md,
            "disclaimer": DISCLAIMER,
        }

    def generate_investment_direction(
        self,
        *,
        days: int = 30,
        use_llm: bool = True,
        refresh_llm: bool = False,
    ) -> dict[str, Any]:
        days = max(1, min(days, 90))
        summary = self.get_summary(days=days)
        news = get_policy_news_fetcher().list_news(limit=30, days=days)
        items = news.get("items") or []
        cache_key = f"investment:{days}"

        cached = _read_cache()
        if (
            use_llm
            and cached
            and cached.get("investment_cache_key") == cache_key
            and cached.get("investment_direction")
            and not refresh_llm
        ):
            return {
                "summary_stats": summary,
                "policy_items": items,
                "llm_available": self._llm_available(),
                "report": cached.get("investment_direction"),
                "cached": True,
                "degraded": summary.get("degraded"),
                "disclaimer": DISCLAIMER,
                "generated_at": cached.get("generated_at"),
            }

        report = None
        if use_llm:
            report = self._generate_investment_direction_llm(summary, items)
            if report:
                _write_cache(
                    {
                        "investment_cache_key": cache_key,
                        "investment_direction": report,
                        "generated_at": datetime.now().isoformat(),
                    }
                )
        if not report:
            report = self._fallback_investment_direction(summary, items)

        return {
            "summary_stats": summary,
            "policy_items": items,
            "llm_available": self._llm_available(),
            "report": report,
            "cached": False,
            "degraded": summary.get("degraded"),
            "disclaimer": DISCLAIMER,
            "generated_at": datetime.now().isoformat(),
        }

    def get_insights(
        self,
        *,
        days: int = 30,
        use_llm: bool = True,
        refresh_llm: bool = False,
    ) -> dict[str, Any]:
        summary = self.get_summary(days=days)
        latest = self.get_latest(limit=20, days=days)
        investment = self.generate_investment_direction(
            days=days,
            use_llm=use_llm,
            refresh_llm=refresh_llm,
        )
        cfg = load_policy_sources_config()
        return {
            **summary,
            "latest_items": latest.get("items") or [],
            "investment_direction": investment.get("report"),
            "themes_config": get_policy_themes(),
            "llm_available": self._llm_available(),
            "fifteenth_fyp_note": (
                "「十五五」指第十五个五年规划（2026-2030），"
                "当前处于规划编制与政策密集发布阶段。"
            ),
            "sources_config": [
                s for s in (cfg.get("sources") or []) if isinstance(s, dict) and s.get("enabled") is not False
            ],
            "disclaimer": DISCLAIMER,
        }

    def refresh_cache(self) -> dict[str, Any]:
        summary = self.get_summary(days=30)
        _write_cache(
            {
                "summary_cache_key": "30",
                "generated_at": datetime.now().isoformat(),
                "summary": summary,
            }
        )
        return {"ok": True, "policy_count": summary.get("policy_count", 0)}


def _read_cache() -> Optional[dict[str, Any]]:
    if not POLICY_INSIGHTS_CACHE.exists():
        return None
    try:
        return json.loads(POLICY_INSIGHTS_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: dict[str, Any]) -> None:
    existing = _read_cache() or {}
    existing.update(data)
    POLICY_INSIGHTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    POLICY_INSIGHTS_CACHE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


_service: Optional[PolicyInsightsService] = None


def get_policy_insights_service() -> PolicyInsightsService:
    global _service
    if _service is None:
        _service = PolicyInsightsService()
    return _service
