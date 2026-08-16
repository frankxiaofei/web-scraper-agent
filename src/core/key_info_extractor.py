"""从招标公告正文提取关键项目信息（规则层 + 可选 LLM）。"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from src.core.models import BidNotice

logger = logging.getLogger(__name__)

_KEY_FIELDS = (
    "contract_amount",
    "budget_amount",
    "tender_party",
    "project_period",
    "project_location",
    "qualification_requirements",
    "key_summary",
    "bid_deadline",
    "open_time",
    "contact",
    "agency",
)

_NOTICE_EXTRACT_FIELDS = (
    "contract_amount",
    "budget_amount",
    "tender_party",
    "project_period",
    "key_summary",
)

_LLM_CONTENT_MAX_CHARS = 12_000
_METADATA_HINT_KEYS = (
    "source_site_id",
    "source_site_name",
    "site_id",
    "category",
    "publish_date",
    "region",
    "budget_amount",
    "budget",
    "contract_amount",
    "tender_party",
    "purchaser",
    "agency",
    "project_period",
    "key_summary",
    "url",
)

_LABEL_PATTERNS: dict[str, list[str]] = {
    "contract_amount": ["合同金额", "中标金额", "成交金额", "合同价"],
    "budget_amount": [
        "预算金额及最高限价",
        "预算金额与最高限价",
        "预算金额",
        "项目预算",
        "采购预算",
        "最高限价",
        "控制价",
        "合同估算价",
        "项目估算价",
    ],
    "tender_party": ["采购人", "招标人", "采购单位", "采购人名称", "项目业主", "建设单位"],
    "agency": ["代理机构", "采购代理机构", "招标代理机构"],
    "project_period": [
        "项目周期",
        "工期",
        "服务期限",
        "建设工期",
        "合同履行期限",
        "交付期限",
    ],
    "bid_deadline": [
        "投标截止时间",
        "递交投标文件截止时间",
        "响应文件提交截止时间",
        "投标文件递交截止",
        "报名截止时间",
    ],
    "open_time": ["开标时间", "开标日期", "开启时间"],
    "contact": ["联系人", "项目联系人", "采购人联系人", "联系电话", "联系方式"],
    "project_location": [
        "建设地点",
        "项目地点",
        "工程地点",
        "采购项目实施地点",
        "项目实施地点",
        "建设地点为",
        "工程建设地点",
    ],
    "qualification_requirements": [
        "资质要求",
        "投标人资格",
        "供应商资格",
        "资格条件",
        "投标人资质",
        "资格要求",
    ],
}

_SUMMARY_SECTION_LABELS = [
    "项目概况",
    "采购需求",
    "招标内容",
    "项目简介",
    "采购内容",
    "项目说明",
]


def _looks_valid_amount_value(value: str) -> bool:
    """判断金额字段是否完整（非标签碎片、非千分位截断）。"""
    if not value or len(value) > 300:
        return False
    junk_starts = ("和", "的", "通过", "单位", "统筹", "在", "对", "将", "及", "与", "均", "为")
    if value.startswith(junk_starts):
        return False
    if not re.search(r"\d", value):
        return False
    if re.search(r"(?:元|万|万元|亿|亿元)", value):
        return True
    if re.search(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", value):
        return True
    stripped = value.strip()
    if re.fullmatch(r"[\d,，.]+", stripped):
        num_str = stripped.replace(",", "").replace("，", "")
        try:
            return float(num_str) >= 10000
        except ValueError:
            return False
    return True


def looks_valid_stored_amount(value: str | None) -> bool:
    """已落库金额是否可信；用于触发从正文重新抽取。"""
    if not value or not str(value).strip():
        return False
    return _looks_valid_amount_value(str(value).strip())


def _looks_valid_value(field: str, value: str) -> bool:
    if not value or len(value) > 300:
        return False
    junk_starts = ("和", "的", "通过", "单位", "统筹", "在", "对", "将")
    if value.startswith(junk_starts) and field in ("budget_amount", "contract_amount", "tender_party"):
        return False
    if field in ("budget_amount", "contract_amount"):
        return _looks_valid_amount_value(value)
    if field == "tender_party" and len(value) < 4:
        return False
    return True


def _extract_labeled_value(text: str, labels: list[str], field: str = "") -> Optional[str]:
    is_amount = field in ("budget_amount", "contract_amount")
    for label in labels:
        if is_amount:
            m = re.search(
                rf"{re.escape(label)}[^：:\n\r]*[：:]\s*"
                r"([\d,，.]+(?:\s*(?:万元|万|亿元|亿|元))?(?:\s*（[^）]*）)?)",
                text,
            )
        else:
            m = re.search(
                rf"{re.escape(label)}\s*[：:]\s*([^\n\r；;]+)",
                text,
            )
        if m:
            value = m.group(1).strip()
            if value and _looks_valid_value(field or "generic", value):
                return value
    return None


def _extract_summary(text: str, max_len: int = 400) -> Optional[str]:
    for label in _SUMMARY_SECTION_LABELS:
        m = re.search(
            rf"{re.escape(label)}[：:\s]*([\s\S]{{1,800}}?)(?=\n\s*[\u4e00-\u9fff]{{2,8}}[：:]|$)",
            text,
        )
        if m:
            chunk = re.sub(r"\s+", " ", m.group(1)).strip()
            if len(chunk) >= 20:
                if len(chunk) <= max_len:
                    return chunk
                return chunk[:max_len].rstrip() + "…"
    return None


def _extract_by_rules(content_text: str, title: str = "") -> dict[str, Optional[str]]:
    text = (content_text or "").strip()
    if not text:
        return {k: None for k in _KEY_FIELDS}

    result: dict[str, Optional[str]] = {}
    for field, labels in _LABEL_PATTERNS.items():
        result[field] = _extract_labeled_value(text, labels, field)

    result["key_summary"] = _extract_summary(text)
    if not result["key_summary"] and title:
        result["key_summary"] = title[:200] if len(title) > 200 else title

    return result


def _llm_configured() -> bool:
    from src.core.config import get_settings

    settings = get_settings()
    return bool(settings.openai_api_key and settings.key_info_llm)


def _resolve_use_llm(use_llm: bool | None, *, has_content: bool) -> bool:
    """None 表示有正文且 LLM 已配置时默认启用。"""
    if use_llm is False:
        return False
    if use_llm is True:
        return True
    return has_content and _llm_configured()


def _build_notice_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    hints: dict[str, Any] = {}
    for key in _METADATA_HINT_KEYS:
        val = metadata.get(key)
        if val is not None and str(val).strip() and str(val).strip() not in ("—", "-"):
            hints[key] = str(val).strip()[:500]
    return hints


def _llm_extract(
    content_text: str,
    title: str = "",
    *,
    metadata: dict[str, Any] | None = None,
) -> Optional[dict[str, str]]:
    from src.core.config import get_extraction_model, get_settings

    if not _llm_configured():
        return None

    import urllib.error
    import urllib.request

    settings = get_settings()
    text = (content_text or "").strip()
    preview = text[:_LLM_CONTENT_MAX_CHARS] if len(text) > _LLM_CONTENT_MAX_CHARS else text
    meta = _build_notice_metadata(metadata)
    prompt = {
        "title": (title or "")[:300],
        "content_text": preview,
        "metadata": meta or None,
        "fields": {
            "contract_amount": "合同金额、中标金额或预算/最高限价（保留原文单位，如 866万元）",
            "budget_amount": "采购预算（若与 contract_amount 相同可重复）",
            "tender_party": "招标方/采购人/建设单位全称",
            "project_period": "项目周期、工期、服务期限或合同履行期限",
            "key_summary": "招标关键内容：项目概况、采购/招标范围，100字内，不含资格要求",
        },
        "task": (
            "根据完整标题、正文与 metadata 提示，抽取上述字段。"
            "仅返回 JSON 对象，键名与 fields 一致；缺失用 null，不得编造。"
        ),
    }
    base_url = (settings.llm_base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    model = get_extraction_model(settings)
    url = f"{base_url}/v1/chat/completions" if base_url else "https://api.openai.com/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是招标文档信息抽取助手，只输出合法 JSON。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:200]
        err_msg = f"关键信息 LLM HTTP 错误 {e.code}: {err_body}"
        logger.warning("%s", err_msg)
        from src.core.llm_alert_service import maybe_alert_on_llm_error

        maybe_alert_on_llm_error(err_msg, source="key_info_extractor")
        return None
    except Exception as e:
        err_msg = f"关键信息 LLM 调用失败: {e}"
        logger.warning("%s", err_msg)
        from src.core.llm_alert_service import maybe_alert_on_llm_error

        maybe_alert_on_llm_error(err_msg, source="key_info_extractor")
        return None

    content = payload["choices"][0]["message"]["content"].strip()
    if "```" in content:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            content = content[start:end]
    try:
        data: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("关键信息 LLM 返回非 JSON")
        return None

    out: dict[str, str] = {}
    for key in _KEY_FIELDS:
        val = data.get(key)
        if val is not None and str(val).strip():
            out[key] = str(val).strip()
    return out or None


def extract_key_info(
    content_text: str,
    title: str = "",
    *,
    use_llm: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Optional[str]]:
    """从正文提取关键字段；LLM 优先，规则层作回退。"""
    text = (content_text or "").strip()
    rules = _extract_by_rules(text, title)
    if not _resolve_use_llm(use_llm, has_content=bool(text)):
        return rules

    llm_data = _llm_extract(text, title, metadata=metadata)
    if not llm_data:
        return rules

    merged: dict[str, Optional[str]] = dict(rules)
    for key in _KEY_FIELDS:
        val = llm_data.get(key)
        if val:
            merged[key] = val
        elif not merged.get(key):
            merged[key] = rules.get(key)
    return merged


def notice_key_info_dict(notice: BidNotice) -> dict[str, Optional[str]]:
    """将 BidNotice 上的关键字段组装为 dict（供 API / 模板）。"""
    return {
        "contract_amount": notice.contract_amount,
        "budget_amount": notice.budget_amount or notice.budget,
        "tender_party": notice.tender_party or notice.purchaser,
        "agency": notice.agency,
        "project_period": notice.project_period,
        "key_summary": notice.key_summary,
        "bid_deadline": notice.bid_deadline,
        "open_time": notice.open_time,
        "contact": notice.contact,
        "project_location": getattr(notice, "project_location", None),
        "qualification_requirements": getattr(notice, "qualification_requirements", None),
    }


def apply_key_info_to_notice(notice: BidNotice) -> BidNotice:
    """根据正文填充 BidNotice 关键信息字段，并同步 purchaser/budget/agency。"""
    text = (notice.content_text or "").strip()
    if not text and notice.content_html:
        text = re.sub(r"<[^>]+>", " ", notice.content_html)
        text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return notice

    info = extract_key_info(text, notice.title)
    for field in (
        "contract_amount",
        "budget_amount",
        "tender_party",
        "project_period",
        "project_location",
        "qualification_requirements",
        "key_summary",
        "bid_deadline",
        "open_time",
        "contact",
    ):
        val = info.get(field)
        if val:
            setattr(notice, field, val)

    if info.get("agency") and not notice.agency:
        notice.agency = info["agency"]
    if info.get("tender_party") and not notice.purchaser:
        notice.purchaser = info["tender_party"]
    if not notice.tender_party and notice.purchaser:
        notice.tender_party = notice.purchaser
    if info.get("budget_amount") and not notice.budget:
        notice.budget = info["budget_amount"]
    if not notice.budget_amount and notice.budget:
        notice.budget_amount = notice.budget

    return notice
