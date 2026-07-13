"""对话创建的定时任务：增量 sync + 飞书报告，或 Hermes prompt + 飞书摘要（持久化 JSON + APScheduler）。"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from src.core.feishu_webhook import FeishuWebhookClient, build_feishu_card_header, format_feishu_card_title
from src.core.notification.task_push import (
    normalize_notification_channels,
    push_task_notifications,
    resolve_task_notification_channels,
)
from src.core.script_cron import (
    _default_timezone,
    build_cron_trigger,
    compute_next_run_time,
    normalize_cron_expression,
)
from src.core.timezone_utils import app_now, format_display_dt
from src.core.site_sync import get_site_by_id

logger = logging.getLogger(__name__)

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
JOBS_PATH = DATA_DIR / "chat_scheduled_tasks.json"

# 口语 cron 扩展（在 script_cron 标准解析之前尝试）
_EXTRA_NATURAL_CRON: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"上午\s*(\d{1,2})\s*点"), lambda m: f"0 {int(m.group(1))} * * *"),
    (re.compile(r"早上\s*(\d{1,2})\s*点"), lambda m: f"0 {int(m.group(1))} * * *"),
    (
        re.compile(r"下午\s*(\d{1,2})\s*点"),
        lambda m: f"0 {int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1))} * * *",
    ),
    (
        re.compile(r"傍晚\s*(\d{1,2})\s*点"),
        lambda m: f"0 {int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1))} * * *",
    ),
    (re.compile(r"晚上\s*(\d{1,2})\s*点"), lambda m: f"0 {int(m.group(1))} * * *"),
]

_SCHEDULE_INTENT_KEYWORDS = (
    "定时",
    "每天",
    "每日",
    "cron",
    "通知任务",
    "创建任务",
    "设置任务",
)
_FEISHU_KEYWORDS = ("飞书", "推送", "报告", "简报", "通知群")
_INCREMENTAL_KEYWORDS = ("增量", "同步", "爬取", "抓取")
_BIM_KEYWORDS = ("bim", "BIM", "电建")
_BIM_ANALYSIS_KEYWORDS = ("综合分析", "BIM 分析", "bim 分析", "BIM分析", "每日分析", "今日新增", "分析报告")
_HERMES_SCHEDULE_KEYWORDS = ("hermes", "Hermes", "代理任务", "助手任务", "执行以下", "运行以下")

TASK_TYPE_INCREMENTAL = "incremental_sync"
TASK_TYPE_HERMES = "hermes_prompt"
TASK_TYPE_HERMES_SUMMARY = "hermes_summary"
TASK_TYPE_BIM_DAILY_ANALYSIS = "bim_daily_analysis"
TASK_TYPE_BIM_DAILY_BRIEF = "bim_daily_brief"
TASK_TYPE_BIM_WEEKLY_BRIEF = "bim_weekly_brief"
TASK_TYPE_ENGINEERING_LLM_REPORT = "engineering_llm_report"
HERMES_TASK_TYPES = frozenset({TASK_TYPE_HERMES, TASK_TYPE_HERMES_SUMMARY})
VALID_TASK_TYPES = (
    TASK_TYPE_INCREMENTAL,
    TASK_TYPE_HERMES,
    TASK_TYPE_HERMES_SUMMARY,
    TASK_TYPE_BIM_DAILY_ANALYSIS,
    TASK_TYPE_BIM_DAILY_BRIEF,
    TASK_TYPE_BIM_WEEKLY_BRIEF,
    TASK_TYPE_ENGINEERING_LLM_REPORT,
)

PUSH_TARGET_WEBHOOK = "webhook"
PUSH_TARGET_INDIVIDUAL = "individual"
VALID_PUSH_TARGETS = (PUSH_TARGET_WEBHOOK, PUSH_TARGET_INDIVIDUAL)

REPORT_KIND_ENGINEERING_LLM = "engineering_llm"
REPORT_KINDS: list[dict[str, str]] = [
    {"value": REPORT_KIND_ENGINEERING_LLM, "label": "工程大模型应用"},
]

TASK_TYPE_LABELS: dict[str, str] = {
    TASK_TYPE_INCREMENTAL: "增量同步 + 飞书",
    TASK_TYPE_HERMES: "Hermes 提示词 + 飞书（旧）",
    TASK_TYPE_HERMES_SUMMARY: "Hermes 提示词 + 飞书",
    TASK_TYPE_BIM_DAILY_ANALYSIS: "商机每日综合分析",
    TASK_TYPE_BIM_DAILY_BRIEF: "商机日报推送",
    TASK_TYPE_BIM_WEEKLY_BRIEF: "商机周报推送",
    TASK_TYPE_ENGINEERING_LLM_REPORT: "工程大模型应用 · 个人私信",
}

PUSH_MODE_EXCERPT = "excerpt"
PUSH_MODE_FULL = "full"
PUSH_MODES: list[dict[str, str]] = [
    {"value": PUSH_MODE_EXCERPT, "label": "摘要截断（推荐）"},
    {"value": PUSH_MODE_FULL, "label": "完整输出"},
]
DEFAULT_PUSH_EXCERPT_MAX = 1500
MAX_PUSH_EXCERPT = 4000

PROMPT_PRESETS: list[dict[str, str]] = [
    {
        "id": "bim_daily_summary",
        "label": "商机日报总结",
        "description": "汇总今日招标公告与商机线索并给出重点结论",
        "default_name": "商机日报总结",
        "default_cron": "0 8 * * *",
        "prompt": (
            "请分析今日（{date}，{weekday}）新增的招标公告与商机线索。\n\n"
            "要求：\n"
            "1. 统计今日标讯总量，按站点分布列出 Top 5\n"
            "2. 提炼 3–5 条重点标讯（项目名称、预算/金额、站点）\n"
            "3. 给出 2–3 句综合结论与关注建议\n\n"
            "输出格式：先写完整分析，最后用「## 飞书摘要」标题给出 200 字以内的群推送摘要。\n"
            "已接入站点：{site_list}"
        ),
    },
    {
        "id": "bim_weekly_summary",
        "label": "商机周报总结",
        "description": "汇总近 30 天招标趋势与重点标讯",
        "default_name": "商机周报总结（近30天）",
        "default_cron": "0 8 * * 5",
        "prompt": (
            "请分析近 30 天（截至 {date}）的招标公告与商机线索。\n\n"
            "要求：\n"
            "1. 统计近 30 天标讯总量与日均，按站点分布\n"
            "2. 对比前 30 天（如能查询）说明增减趋势\n"
            "3. 列出近 30 天 Top 5 重点标讯\n"
            "4. 给出下周关注建议\n\n"
            "输出格式：先写完整分析，最后用「## 飞书摘要」标题给出 300 字以内的群推送摘要。\n"
            "已接入站点：{site_list}"
        ),
    },
]

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_FEISHU_SUMMARY_SECTION_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:#{1,3}\s*飞书摘要|【飞书摘要】|###\s*推送摘要)\s*\n([\s\S]*)$",
    re.I,
)

CRON_PRESETS: list[dict[str, str]] = [
    {"value": "0 8 * * *", "label": "每天 08:00"},
    {"value": "0 9 * * 1", "label": "每周一 09:00"},
    {"value": "0 18 * * *", "label": "每天 18:00"},
    {"value": "0 8 * * 5", "label": "每周五 08:00"},
    {"value": "custom", "label": "自定义 Cron"},
]

_FEISHU_WEBHOOK_URL_PATTERN = re.compile(
    r"https?://open\.feishu\.cn/open-apis/bot/v2/hook/[a-zA-Z0-9\-]+",
    re.I,
)
_FEISHU_WEBHOOK_SECRET_PATTERN = re.compile(
    r"(?:secret|签名|密钥)[：:\s]*([A-Za-z0-9+/=_\-]{8,128})",
    re.I,
)
_FEISHU_USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{2,64}$")


def parse_feishu_webhooks(raw: Any) -> list[dict[str, Any]]:
    """解析飞书群 Webhook 列表：支持 list[dict|str] 或换行/逗号分隔 URL 字符串。"""
    if raw is None:
        return []
    items: list[Any]
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).replace(";", "\n")
        items = [part.strip() for part in re.split(r"[\n,，]+", text) if part.strip()]

    seen_urls: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            url = item.strip()
            if not url:
                continue
            match = _FEISHU_WEBHOOK_URL_PATTERN.search(url)
            if not match:
                raise ValueError(f"无效的飞书 Webhook URL: {url!r}")
            url = match.group(0)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            result.append({"url": url})
            continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        match = _FEISHU_WEBHOOK_URL_PATTERN.search(url)
        if not match:
            raise ValueError(f"无效的飞书 Webhook URL: {url!r}")
        url = match.group(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        entry: dict[str, Any] = {"url": url}
        secret = item.get("secret")
        if secret is not None and str(secret).strip():
            entry["secret"] = str(secret).strip()
        name = (item.get("name") or "").strip()
        if name:
            entry["name"] = name
        result.append(entry)
    return result


def parse_feishu_user_ids(raw: Any) -> list[str]:
    """解析飞书 user_id 列表：支持 list 或逗号/空格/换行分隔字符串。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw]
    else:
        text = str(raw).replace("\n", ",").replace(";", ",")
        parts = re.split(r"[\s,，]+", text)
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        uid = part.strip()
        if not uid or uid in seen:
            continue
        if not _FEISHU_USER_ID_PATTERN.match(uid):
            raise ValueError(f"无效的飞书 user_id: {uid!r}（示例: yuan_jp, li_xf10）")
        seen.add(uid)
        result.append(uid)
    return result


def mask_feishu_webhooks(
    webhooks: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """脱敏展示任务级飞书 Webhook 列表。"""
    masked: list[dict[str, Any]] = []
    for item in webhooks or []:
        if not isinstance(item, dict):
            continue
        url = mask_webhook_url(str(item.get("url") or ""))
        entry: dict[str, Any] = {"url": url}
        if item.get("name"):
            entry["name"] = item["name"]
        if item.get("secret"):
            entry["secret"] = "***"
        masked.append(entry)
    return masked


def mask_webhook_url(url: Optional[str]) -> Optional[str]:
    """脱敏展示飞书 Webhook URL。"""
    if not url:
        return url
    match = _FEISHU_WEBHOOK_URL_PATTERN.search(url.strip())
    if match:
        full = match.group(0)
        prefix = full.rsplit("/", 1)[0] + "/"
        token = full.rsplit("/", 1)[-1]
        if len(token) <= 8:
            return prefix + "***"
        return f"{prefix}{token[:4]}***{token[-4:]}"
    trimmed = url.strip()
    if len(trimmed) <= 12:
        return "***"
    return f"{trimmed[:12]}***{trimmed[-4:]}"


def extract_feishu_webhook_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """从用户消息解析飞书 Webhook URL 与可选 secret。"""
    msg = text or ""
    webhook_url = None
    match = _FEISHU_WEBHOOK_URL_PATTERN.search(msg)
    if match:
        webhook_url = match.group(0)
    secret = None
    secret_match = _FEISHU_WEBHOOK_SECRET_PATTERN.search(msg)
    if secret_match:
        secret = secret_match.group(1).strip()
    return webhook_url, secret


def is_hermes_task_type(task_type: Optional[str]) -> bool:
    return (task_type or "").strip() in HERMES_TASK_TYPES


def build_prompt_context() -> dict[str, str]:
    """构建提示词模板占位符上下文。"""
    now = app_now()
    site_names: list[str] = []
    try:
        from src.core.site_sync import load_sites_config

        for site in load_sites_config().get("sites") or []:
            if site.get("enabled", True) is False:
                continue
            name = (site.get("name") or site.get("id") or "").strip()
            if name:
                site_names.append(name)
    except Exception as exc:
        logger.debug("加载站点列表失败: %s", exc)

    site_list = "、".join(site_names[:25])
    if len(site_names) > 25:
        site_list += f" 等共 {len(site_names)} 个站点"

    return {
        "date": now.strftime("%Y-%m-%d"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": _WEEKDAY_LABELS[now.weekday()],
        "site_list": site_list or "（暂无已启用站点）",
    }


def render_prompt_template(prompt: str, *, context: Optional[dict[str, str]] = None) -> str:
    """渲染提示词模板，支持 {date}、{site_list} 等占位符。"""
    template = (prompt or "").strip()
    if not template:
        return ""
    ctx = context or build_prompt_context()
    try:
        return template.format_map(ctx)
    except KeyError as exc:
        logger.warning("提示词模板占位符缺失: %s", exc)
        return template


def extract_hermes_push_content(
    output: str,
    *,
    push_mode: str = PUSH_MODE_EXCERPT,
    excerpt_max: int = DEFAULT_PUSH_EXCERPT_MAX,
) -> str:
    """从 Hermes 输出提取飞书推送正文：优先「飞书摘要」段落，否则按 push_mode 截断。"""
    text = (output or "").strip()
    if not text:
        return "（无文本输出）"

    match = _FEISHU_SUMMARY_SECTION_PATTERN.search(text)
    if match:
        text = match.group(1).strip()

    if push_mode == PUSH_MODE_FULL:
        max_len = MAX_PUSH_EXCERPT
    else:
        max_len = max(200, min(int(excerpt_max or DEFAULT_PUSH_EXCERPT_MAX), MAX_PUSH_EXCERPT))

    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n…（已截断）"


def resolve_env_feishu_webhooks() -> list[dict[str, Any]]:
    """从 FEISHU_WEBHOOK_URL / FEISHU_WEBHOOK_SECRET 解析默认群机器人。"""
    import os

    from src.core.config import get_settings

    settings = get_settings()
    url = (os.environ.get("FEISHU_WEBHOOK_URL") or settings.feishu_webhook_url or "").strip()
    if not url:
        return []
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET") or settings.feishu_webhook_secret
    if secret is not None and not str(secret).strip():
        secret = None
    entry: dict[str, Any] = {"url": url, "source": "env"}
    if secret:
        entry["secret"] = str(secret)
    return [entry]


def _legacy_task_webhook_entry(task: dict[str, Any]) -> Optional[dict[str, Any]]:
    """兼容单字段 feishu_webhook_url / feishu_webhook_secret。"""
    url = (task.get("feishu_webhook_url") or "").strip()
    if not url:
        return None
    secret = task.get("feishu_webhook_secret")
    if secret is not None and not str(secret).strip():
        secret = None
    entry: dict[str, Any] = {"url": url}
    if secret:
        entry["secret"] = str(secret)
    return entry


def resolve_task_feishu_webhooks(
    task: dict[str, Any],
    *,
    include_env_fallback: bool = True,
) -> list[dict[str, Any]]:
    """解析任务飞书群 Webhook：任务列表 → 单字段 legacy → 全局 env。"""
    if isinstance(task.get("feishu_webhooks"), list) and task["feishu_webhooks"]:
        return [
            {k: v for k, v in item.items() if k != "source"}
            for item in parse_feishu_webhooks(task["feishu_webhooks"])
        ]
    legacy = _legacy_task_webhook_entry(task)
    if legacy:
        return [legacy]
    if include_env_fallback:
        return [
            {k: v for k, v in item.items() if k != "source"}
            for item in resolve_env_feishu_webhooks()
        ]
    return []


def resolve_task_feishu_user_ids(
    task: dict[str, Any],
    *,
    include_env_fallback: bool = True,
) -> list[str]:
    """解析任务飞书私信收件人：任务列表 → env。"""
    if isinstance(task.get("feishu_user_ids"), list) and task["feishu_user_ids"]:
        return list(task["feishu_user_ids"])
    if include_env_fallback:
        return resolve_env_feishu_receive_user_ids()
    return []


def resolve_task_push_channels(
    task: dict[str, Any],
    *,
    include_env_fallback: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (webhooks, user_ids)，可同时配置两种推送通道。"""
    return (
        resolve_task_feishu_webhooks(task, include_env_fallback=include_env_fallback),
        resolve_task_feishu_user_ids(task, include_env_fallback=include_env_fallback),
    )


def is_task_feishu_push_configured(
    task: dict[str, Any],
    *,
    include_env_fallback: bool = True,
) -> bool:
    webhooks, user_ids = resolve_task_push_channels(
        task,
        include_env_fallback=include_env_fallback,
    )
    return bool(webhooks) or bool(user_ids)


def resolve_task_feishu_client(task: dict[str, Any]) -> FeishuWebhookClient:
    """优先任务级 webhook，否则 fallback 全局 FEISHU_WEBHOOK_URL。"""
    webhooks = resolve_task_feishu_webhooks(task)
    if webhooks:
        first = webhooks[0]
        return FeishuWebhookClient(
            webhook_url=first.get("url"),
            secret=first.get("secret"),
        )
    return FeishuWebhookClient()


def push_task_feishu_card(
    task: dict[str, Any],
    card: dict[str, Any],
    *,
    dry_run: bool = False,
    include_env_fallback: bool = True,
) -> dict[str, Any]:
    """按任务配置同时推送到群 Webhook 与人员列表。"""
    if not task.get("feishu_push", True):
        return {"ok": True, "skipped": True, "reason": "feishu_push_disabled"}

    webhooks, user_ids = resolve_task_push_channels(
        task,
        include_env_fallback=include_env_fallback,
    )
    if dry_run or (not webhooks and not user_ids):
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_push_channels",
            "dry_run": dry_run,
            "payload": {"msg_type": "interactive", "card": card},
        }

    from src.core.feishu_push import push_feishu_interactive_card_multi

    return push_feishu_interactive_card_multi(
        card,
        webhooks=webhooks,
        user_ids=user_ids,
        dry_run=dry_run,
    )


def detect_scheduled_feishu_intent(text: str) -> bool:
    """启发式：用户是否要创建「增量爬取 + 飞书报告」定时任务。"""
    msg = (text or "").strip()
    if not msg:
        return False
    has_schedule = any(kw in msg for kw in _SCHEDULE_INTENT_KEYWORDS) or bool(
        re.search(r"每\s*\d+\s*点|:\d{2}|点.*爬|点.*同步", msg)
    )
    has_crawl = any(kw in msg for kw in _INCREMENTAL_KEYWORDS)
    has_feishu = any(kw in msg for kw in _FEISHU_KEYWORDS)
    return has_schedule and has_crawl and has_feishu


def detect_scheduled_hermes_feishu_intent(text: str) -> bool:
    """启发式：用户是否要创建「Hermes 任务 + 飞书报告」定时任务（非增量 sync）。"""
    if detect_scheduled_feishu_intent(text):
        return False
    if detect_scheduled_bim_analysis_feishu_intent(text):
        return False
    msg = (text or "").strip()
    if not msg:
        return False
    has_schedule = any(kw in msg for kw in _SCHEDULE_INTENT_KEYWORDS) or bool(
        re.search(r"每\s*\d+\s*点|:\d{2}|点.*执行|点.*运行", msg)
    )
    has_feishu = any(kw in msg for kw in _FEISHU_KEYWORDS) or bool(
        _FEISHU_WEBHOOK_URL_PATTERN.search(msg)
    )
    has_hermes = any(kw in msg for kw in _HERMES_SCHEDULE_KEYWORDS) or bool(
        re.search(r"执行.{2,40}任务|运行.{2,40}任务", msg)
    )
    return has_schedule and has_feishu and has_hermes


def detect_scheduled_bim_analysis_feishu_intent(text: str) -> bool:
    """启发式：用户是否要创建「BIM 每日综合分析 + 飞书」定时任务。"""
    if detect_scheduled_feishu_intent(text):
        return False
    msg = (text or "").strip()
    if not msg:
        return False
    has_schedule = any(kw in msg for kw in _SCHEDULE_INTENT_KEYWORDS) or bool(
        re.search(r"每\s*\d+\s*点|:\d{2}|点.*分析|点.*报告", msg)
    )
    has_feishu = any(kw in msg for kw in _FEISHU_KEYWORDS) or bool(
        _FEISHU_WEBHOOK_URL_PATTERN.search(msg)
    )
    has_bim = any(kw in msg for kw in _BIM_KEYWORDS) or any(kw in msg for kw in _BIM_ANALYSIS_KEYWORDS)
    has_analysis = any(kw in msg for kw in _BIM_ANALYSIS_KEYWORDS) or bool(
        re.search(r"分析.*(BIM|bim|招标|标讯)|BIM.*(分析|报告|简报)", msg)
    )
    return has_schedule and has_feishu and has_bim and has_analysis


def detect_bim_analysis_schedule_ui_intent(text: str) -> bool:
    """启发式：用户是否要打开 BIM 分析定时任务配置表单（生成式 UI）。"""
    msg = (text or "").strip()
    if not msg:
        return False
    ui_patterns = (
        r"创建.*BIM.*(分析|报告).*(?:通知任务|定时|任务)",
        r"BIM.*(每日|定时).*(分析|报告).*(?:通知任务|任务|配置)",
        r"配置.*(BIM|bim).*(飞书|推送|定时|通知任务)",
        r"BIM.*分析.*(?:通知任务|定时(?:任务|推送))",
    )
    if any(re.search(p, msg, re.I) for p in ui_patterns):
        return True
    if "BIM 每日分析" in msg and any(kw in msg for kw in ("创建", "配置", "设置", "定时")):
        return True
    return False


def build_bim_analysis_schedule_ui_payload(
    *,
    defaults: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """生成式 UI：BIM 每日分析定时任务配置表单 payload。"""
    defaults = defaults or {}
    return {
        "component": "bim_analysis_schedule_form",
        "title": "创建商机每日分析通知任务",
        "description": "分析今日新增招标公告与商机线索，LLM 综合分析后推送到飞书群机器人。",
        "fields": [
            {
                "name": "name",
                "label": "任务名称",
                "type": "text",
                "placeholder": "商机每日综合分析",
                "default": defaults.get("name") or "商机每日综合分析",
                "required": True,
            },
            {
                "name": "cron_preset",
                "label": "执行时间",
                "type": "select",
                "options": [
                    {"value": "0 8 * * *", "label": "每天 08:00"},
                    {"value": "0 18 * * *", "label": "每天 18:00"},
                    {"value": "custom", "label": "自定义 Cron"},
                ],
                "default": defaults.get("cron") or "0 8 * * *",
                "required": True,
            },
            {
                "name": "cron_custom",
                "label": "自定义 Cron",
                "type": "text",
                "placeholder": "0 8 * * * 或「每天上午8点」",
                "default": defaults.get("cron_custom") or "",
            },
            {
                "name": "feishu_webhook_url",
                "label": "飞书 Webhook URL",
                "type": "url",
                "placeholder": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
                "required": False,
                "hint": "留空则使用全局 FEISHU_WEBHOOK_URL",
            },
            {
                "name": "top_n",
                "label": "Top 标讯条数",
                "type": "number",
                "default": defaults.get("top_n") or 10,
                "min": 1,
                "max": 30,
            },
            {
                "name": "enabled",
                "label": "启用任务",
                "type": "checkbox",
                "default": defaults.get("enabled", True),
            },
        ],
        "submit_action": "create_bim_analysis_schedule",
        "cancel_label": "取消",
        "submit_label": "创建任务",
    }


def detect_scheduled_tasks_panel_ui_intent(text: str) -> bool:
    """启发式：用户是否要打开定时任务列表面板（生成式 UI）。"""
    msg = (text or "").strip()
    if not msg:
        return False
    ui_patterns = (
        r"查看.*(通知任务|定时(?:任务|推送))",
        r"管理.*(通知任务|定时(?:任务|推送))",
        r"(?:通知任务|定时(?:任务|推送)).*(列表|管理|查看|面板)",
        r"我的(?:通知任务|定时(?:任务|推送))",
        r"已配置.*(?:通知任务|定时(?:任务|推送))",
        r"(?:通知任务|定时(?:任务|推送)).*(开关|启停|启用|禁用)",
    )
    if any(re.search(p, msg, re.I) for p in ui_patterns):
        return True
    return msg in (
        "查看通知任务",
        "管理通知任务",
        "通知任务列表",
        "通知任务管理",
    )


def build_scheduled_tasks_panel_ui_payload() -> dict[str, Any]:
    """生成式 UI：定时任务列表面板 payload（任务数据由前端 GET API 拉取）。"""
    return {
        "component": "scheduled_tasks_panel",
        "title": "通知任务管理",
        "description": "查看已配置的通知任务，可启用/禁用或删除。禁用后 run_scheduler 约 60 秒内自动卸载该 job。",
        "task_type_labels": TASK_TYPE_LABELS,
        "empty_hint": "暂无通知任务。可说「每天9点执行 Hermes 任务：统计今日招标公告并推飞书」或在通知任务页创建提示词任务。",
    }


def parse_bim_analysis_schedule_intent(user_message: str) -> dict[str, Any]:
    """从用户口语解析 BIM 每日分析定时任务参数。"""
    text = (user_message or "").strip()
    if not text:
        return {"ok": False, "error": "消息为空"}

    cron = _extract_cron_from_message(text)
    if not cron:
        return {"ok": False, "error": "未能识别执行时间，请说明如「每天上午8点」或 cron「0 8 * * *」"}

    webhook_url, webhook_secret = extract_feishu_webhook_from_text(text)
    name = "商机每日综合分析"
    name_match = re.search(r"任务名[称]?[：:\s]+(.+?)(?:[,，。]|cron|飞书|$)", text, re.I)
    if name_match:
        name = name_match.group(1).strip()[:80] or name
    else:
        name = f"商机每日综合分析 @ {cron}"

    top_n = 10
    top_match = re.search(r"top\s*(\d+)|前\s*(\d+)\s*条", text, re.I)
    if top_match:
        top_n = int(top_match.group(1) or top_match.group(2))

    return {
        "ok": True,
        "task_type": TASK_TYPE_BIM_DAILY_ANALYSIS,
        "cron": cron,
        "name": name,
        "top_n": max(1, min(top_n, 30)),
        "feishu_push": True,
        "feishu_webhook_url": webhook_url,
        "feishu_webhook_secret": webhook_secret,
    }


def _normalize_cron_with_extras(raw: str) -> str:
    text = (raw or "").strip()
    for pattern, builder in _EXTRA_NATURAL_CRON:
        match = pattern.search(text)
        if match:
            expr = builder(match)
            normalize_cron_expression(expr)
            return expr
    return normalize_cron_expression(text)


def _extract_cron_from_message(text: str) -> Optional[str]:
    """从整句用户消息中提取 cron。"""
    for pattern, builder in _EXTRA_NATURAL_CRON:
        match = pattern.search(text)
        if match:
            try:
                return _normalize_cron_with_extras(match.group(0))
            except ValueError:
                pass
    from src.core.script_cron import _NATURAL_CRON_PATTERNS

    for pattern, builder in _NATURAL_CRON_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return _normalize_cron_with_extras(match.group(0))
            except ValueError:
                pass
    cron_match = re.search(r"(\d+\s+\d+\s+\*\s+\*\s+\*)", text)
    if cron_match:
        try:
            return normalize_cron_expression(cron_match.group(1))
        except ValueError:
            pass
    return None


def _infer_report_type(text: str, site_id: Optional[str]) -> str:
    lower = text.lower()
    if any(kw in text or kw.lower() in lower for kw in _BIM_KEYWORDS):
        return "bim"
    if site_id and "电建" in (get_site_by_id(site_id) or {}).get("name", ""):
        return "bim"
    return "site"


def parse_schedule_intent(user_message: str) -> dict[str, Any]:
    """
    从用户口语解析定时任务参数。
    返回 ok、site_id、cron、report_type、feishu_push 等；解析失败时 ok=False。
    """
    text = (user_message or "").strip()
    if not text:
        return {"ok": False, "error": "消息为空"}

    from src.web.crawl_agent_tools import resolve_site_id

    site_id = resolve_site_id(text)
    if not site_id:
        return {"ok": False, "error": "未能识别站点，请说明站点名称或别名（如电建、tjbid）"}

    if not get_site_by_id(site_id):
        return {"ok": False, "error": f"站点未注册: {site_id}"}

    cron = _extract_cron_from_message(text)
    if not cron:
        return {"ok": False, "error": "未能识别执行时间，请说明如「每天上午8点」或 cron「0 8 * * *」"}

    report_type = _infer_report_type(text, site_id)
    site = get_site_by_id(site_id) or {}
    name = f"{site.get('name') or site_id} 增量+飞书 @ {cron}"
    webhook_url, webhook_secret = extract_feishu_webhook_from_text(text)

    return {
        "ok": True,
        "task_type": TASK_TYPE_INCREMENTAL,
        "site_id": site_id,
        "site_name": site.get("name") or site_id,
        "cron": cron,
        "report_type": report_type,
        "feishu_push": True,
        "max_items": 10,
        "name": name,
        "feishu_webhook_url": webhook_url,
        "feishu_webhook_secret": webhook_secret,
    }


def _extract_hermes_prompt(text: str) -> str:
    """从口语消息中提取 Hermes 任务指令。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    for pattern in (
        r"任务[：:]\s*(.+)",
        r"执行[：:]\s*(.+)",
        r"prompt[：:]\s*(.+)",
        r"指令[：:]\s*(.+)",
    ):
        match = re.search(pattern, raw, re.I | re.S)
        if match:
            candidate = match.group(1).strip()
            candidate = _FEISHU_WEBHOOK_URL_PATTERN.sub("", candidate).strip()
            if len(candidate) >= 4:
                return candidate[:2000]

    work = _FEISHU_WEBHOOK_URL_PATTERN.sub("", raw)
    for pattern, _builder in _EXTRA_NATURAL_CRON:
        work = pattern.sub(" ", work)
    from src.core.script_cron import _NATURAL_CRON_PATTERNS

    for pattern, _builder in _NATURAL_CRON_PATTERNS:
        work = pattern.sub(" ", work)
    work = re.sub(r"(\d+\s+\d+\s+\*\s+\*\s+\*)", " ", work)
    strip_tokens = (
        "飞书",
        "推送",
        "报告",
        "简报",
        "通知群",
        "webhook",
        "Webhook",
        "定时",
        "每天",
        "每日",
        "cron",
        "通知任务",
        "创建任务",
        "设置任务",
        "Hermes",
        "hermes",
        "代理",
        "助手",
        "为",
        "是",
    )
    for token in strip_tokens:
        work = work.replace(token, " ")
    work = re.sub(r"\s+", " ", work).strip(" ，,。.;；")
    return work[:2000] if len(work) >= 4 else ""


def parse_hermes_schedule_intent(user_message: str) -> dict[str, Any]:
    """从用户口语解析 Hermes 定时任务参数。"""
    text = (user_message or "").strip()
    if not text:
        return {"ok": False, "error": "消息为空"}

    cron = _extract_cron_from_message(text)
    if not cron:
        return {"ok": False, "error": "未能识别执行时间，请说明如「每天上午9点」或 cron「0 9 * * *」"}

    hermes_prompt = _extract_hermes_prompt(text)
    if not hermes_prompt:
        return {
            "ok": False,
            "error": "未能识别 Hermes 任务指令，请说明要执行的内容，如「任务：统计今日招标公告数量」",
        }

    webhook_url, webhook_secret = extract_feishu_webhook_from_text(text)
    short_prompt = hermes_prompt.replace("\n", " ")[:40]
    name = f"Hermes+飞书 {short_prompt} @ {cron}"

    return {
        "ok": True,
        "task_type": TASK_TYPE_HERMES_SUMMARY,
        "hermes_prompt": hermes_prompt,
        "prompt": hermes_prompt,
        "cron": cron,
        "feishu_push": True,
        "push_mode": PUSH_MODE_EXCERPT,
        "name": name,
        "feishu_webhook_url": webhook_url,
        "feishu_webhook_secret": webhook_secret,
    }


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_chat_scheduled_tasks(*, migrate: bool = True) -> list[dict[str, Any]]:
    if not JOBS_PATH.exists():
        if migrate:
            jobs, changed = migrate_system_notification_tasks([])
            if changed:
                save_chat_scheduled_tasks(jobs)
            return jobs
        return []
    with open(JOBS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    normalized: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job.setdefault("enabled", True)
        normalized.append(job)
    if migrate:
        normalized, changed = migrate_system_notification_tasks(normalized)
        if changed:
            save_chat_scheduled_tasks(normalized)
    return normalized


def save_chat_scheduled_tasks(jobs: list[dict[str, Any]]) -> None:
    _ensure_data_dir()
    payload = {"version": 1, "jobs": jobs}
    tmp = JOBS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(JOBS_PATH)


def get_chat_scheduled_task(task_id: str) -> Optional[dict[str, Any]]:
    for job in load_chat_scheduled_tasks():
        if job.get("id") == task_id:
            return job
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_env_feishu_receive_user_ids() -> list[str]:
    """从 FEISHU_RECEIVE_USERS / FEISHU_RECEIVE_USER 解析默认私信收件人。"""
    import os

    from src.core.config import get_settings

    raw = os.environ.get("FEISHU_RECEIVE_USERS") or get_settings().feishu_receive_users
    if raw and str(raw).strip():
        return parse_feishu_user_ids(raw)
    single = os.environ.get("FEISHU_RECEIVE_USER") or get_settings().feishu_receive_user
    if single and str(single).strip():
        return [str(single).strip()]
    return []


MIGRATED_SYSTEM_TASK_IDS: dict[str, str] = {
    TASK_TYPE_BIM_DAILY_BRIEF: "bim_daily_brief_feishu",
    TASK_TYPE_BIM_WEEKLY_BRIEF: "bim_weekly_brief_feishu",
}


def _find_task_by_type(
    jobs: list[dict[str, Any]],
    task_type: str,
) -> Optional[dict[str, Any]]:
    for job in jobs:
        if job.get("task_type") == task_type:
            return job
    return None


def _backfill_task_push_channels_from_env(task: dict[str, Any]) -> bool:
    """为缺少推送配置的任务回填 .env 中的 Webhook / 收件人；env 源任务与 .env 变更时同步。"""
    changed = False
    has_webhooks = bool(task.get("feishu_webhooks")) or bool(
        (task.get("feishu_webhook_url") or "").strip()
    )
    has_users = bool(task.get("feishu_user_ids"))
    env_webhooks = resolve_env_feishu_webhooks()
    if env_webhooks and (
        not has_webhooks or task.get("feishu_webhooks_source") == "env"
    ):
        normalized = [
            {k: v for k, v in item.items() if k != "source"} for item in env_webhooks
        ]
        if normalized != task.get("feishu_webhooks"):
            task["feishu_webhooks"] = normalized
            task["feishu_webhooks_source"] = "env"
            changed = True
    env_users = resolve_env_feishu_receive_user_ids()
    if env_users and (not has_users or task.get("feishu_user_ids_source") == "env"):
        if sorted(task.get("feishu_user_ids") or []) != sorted(env_users):
            task["feishu_user_ids"] = env_users
            task["feishu_user_ids_source"] = "env"
            changed = True
    return changed


def _build_system_bim_notification_specs() -> list[dict[str, Any]]:
    """从 schedule.yaml / .env 构建 BIM 日报/周报系统任务规格。"""
    import os

    from src.core.config import get_settings

    settings = get_settings()
    scheduler_cfg = _load_scheduler_cfg()
    return [
        {
            "task_type": TASK_TYPE_BIM_DAILY_BRIEF,
            "task_id": MIGRATED_SYSTEM_TASK_IDS[TASK_TYPE_BIM_DAILY_BRIEF],
            "name": "商机日报飞书推送",
            "enabled": bool(settings.bim_brief_enabled and scheduler_cfg.get("bim_brief_enabled", True)),
            "cron": str(
                os.environ.get("BIM_BRIEF_CRON")
                or settings.bim_brief_cron
                or scheduler_cfg.get("bim_brief_cron")
                or "0 8 * * 0-4"
            ).strip(),
            "top_n": int(scheduler_cfg.get("bim_brief_top_n", 10)),
            "days": 1,
        },
        {
            "task_type": TASK_TYPE_BIM_WEEKLY_BRIEF,
            "task_id": MIGRATED_SYSTEM_TASK_IDS[TASK_TYPE_BIM_WEEKLY_BRIEF],
            "name": "商机周报飞书推送",
            "enabled": bool(
                settings.bim_weekly_brief_enabled
                and scheduler_cfg.get("bim_weekly_brief_enabled", True)
            ),
            "cron": str(
                os.environ.get("BIM_WEEKLY_BRIEF_CRON")
                or settings.bim_weekly_brief_cron
                or scheduler_cfg.get("bim_weekly_brief_cron")
                or "0 18 * * 4"
            ).strip(),
            "top_n": int(scheduler_cfg.get("bim_weekly_brief_top_n", 10)),
            "days": int(scheduler_cfg.get("bim_weekly_brief_days", 30)),
        },
    ]


def _new_migrated_system_task(spec: dict[str, Any], *, now: str) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": spec["task_id"],
        "task_type": spec["task_type"],
        "cron": spec["cron"],
        "feishu_push": True,
        "enabled": spec["enabled"],
        "created_by": "system_migration",
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_run_status": None,
        "last_run_message": None,
        "name": spec["name"],
        "top_n": spec["top_n"],
        "days": spec["days"],
        "migrated_from": "schedule.yaml/.env",
    }
    _backfill_task_push_channels_from_env(task)
    return task


def _consolidate_bim_notification_tasks(
    jobs: list[dict[str, Any]],
    spec: dict[str, Any],
    *,
    now: str,
) -> tuple[list[dict[str, Any]], bool]:
    """合并同类型 BIM 任务为稳定 ID，去除 UUID 重复项。"""
    task_type = spec["task_type"]
    stable_id = spec["task_id"]
    changed = False
    typed = [j for j in jobs if j.get("task_type") == task_type]
    stable = next((j for j in typed if j.get("id") == stable_id), None)
    legacy = [j for j in typed if j.get("id") != stable_id]

    if not typed:
        jobs.append(_new_migrated_system_task(spec, now=now))
        return jobs, True

    if stable is None and legacy:
        primary = max(
            legacy,
            key=lambda j: (j.get("updated_at") or "", j.get("created_at") or "", j.get("id") or ""),
        )
        primary["id"] = stable_id
        primary.setdefault("migrated_from", "legacy_chat_task")
        primary["updated_at"] = now
        stable = primary
        changed = True
        legacy = [j for j in legacy if j is not primary]
        remove_ids = {j.get("id") for j in legacy if j.get("id")}
        if remove_ids:
            jobs = [j for j in jobs if j.get("id") not in remove_ids]
            changed = True
            stable = next((j for j in jobs if j.get("id") == stable_id), stable)

    if stable is not None:
        expected_cron = (spec.get("cron") or "").strip()
        if expected_cron and (stable.get("cron") or "").strip() != expected_cron:
            stable["cron"] = expected_cron
            stable["updated_at"] = now
            changed = True
        if _backfill_task_push_channels_from_env(stable):
            stable["updated_at"] = now
            changed = True
        return jobs, changed

    if stable is None:
        jobs.append(_new_migrated_system_task(spec, now=now))
        return jobs, True

    return jobs, changed


def _chat_task_matches_system_config(
    chat_task: Optional[dict[str, Any]],
    system_spec: dict[str, Any],
) -> bool:
    """已配置任务是否与系统 cron / 推送通道一致（用于标注系统行与可编辑行是否同步）。"""
    if not chat_task:
        return False
    if (chat_task.get("cron") or "").strip() != (system_spec.get("cron") or "").strip():
        return False
    env_users = resolve_env_feishu_receive_user_ids()
    if env_users:
        chat_users = resolve_task_feishu_user_ids(chat_task, include_env_fallback=False)
        if sorted(chat_users) != sorted(env_users):
            return False
    env_webhooks = resolve_env_feishu_webhooks()
    if env_webhooks:
        chat_webhooks = resolve_task_feishu_webhooks(chat_task, include_env_fallback=False)
        if not chat_webhooks:
            return False
    return True


def _serialize_system_notification_task(spec: dict[str, Any]) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": spec["task_id"],
        "task_type": spec["task_type"],
        "name": spec["name"],
        "cron": spec["cron"],
        "enabled": spec["enabled"],
        "top_n": spec["top_n"],
        "days": spec.get("days", 1),
        "feishu_push": True,
    }
    env_users = resolve_env_feishu_receive_user_ids()
    if env_users:
        task["feishu_user_ids"] = env_users
        task["feishu_user_ids_source"] = "env"
    env_webhooks = resolve_env_feishu_webhooks()
    if env_webhooks:
        task["feishu_webhooks"] = [
            {k: v for k, v in item.items() if k != "source"} for item in env_webhooks
        ]
        task["feishu_webhooks_source"] = "env"
    out = _serialize_task(task, mask_secrets=True)
    out["source"] = "system"
    out["read_only"] = True
    return out


def list_system_notification_tasks(
    *,
    chat_tasks: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """只读系统内置任务：始终展示 schedule.yaml / .env 中的 BIM 日报/周报配置。"""
    if chat_tasks is None:
        chat_tasks = list_chat_scheduled_tasks()
    by_type = {
        task.get("task_type"): task
        for task in chat_tasks
        if task.get("task_type") in MIGRATED_SYSTEM_TASK_IDS
    }
    by_id = {task.get("id"): task for task in chat_tasks if task.get("id")}
    results: list[dict[str, Any]] = []
    for spec in _build_system_bim_notification_specs():
        out = _serialize_system_notification_task(spec)
        chat_task = by_type.get(spec["task_type"]) or by_id.get(spec["task_id"])
        if chat_task:
            out["has_editable_task"] = True
            out["config_matches_editable"] = _chat_task_matches_system_config(
                chat_task, spec
            )
        results.append(out)
    return results


def migrate_system_notification_tasks(
    jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """将 schedule.yaml / .env 内置 BIM 日报/周报迁移为普通 JSON 通知任务。"""
    changed = False
    now = _now_iso()
    for spec in _build_system_bim_notification_specs():
        jobs, spec_changed = _consolidate_bim_notification_tasks(jobs, spec, now=now)
        changed = changed or spec_changed
    return jobs, changed


def has_enabled_chat_bim_brief_task(task_type: str) -> bool:
    """是否存在已启用的同类型 BIM 简报 JSON 任务（用于禁用内置 scheduler job）。"""
    if task_type not in MIGRATED_SYSTEM_TASK_IDS:
        return False
    for job in load_chat_scheduled_tasks(migrate=False):
        if job.get("task_type") == task_type and job.get("enabled", True):
            return True
    return False


def _serialize_task(task: dict[str, Any], *, mask_secrets: bool = True) -> dict[str, Any]:
    from src.core.script_cron import describe_cron_expression

    cron = task.get("cron") or ""
    next_run = compute_next_run_time(cron) if cron and task.get("enabled", True) else None
    out = dict(task)
    out.setdefault("task_type", TASK_TYPE_INCREMENTAL)
    if is_hermes_task_type(out.get("task_type")):
        prompt = out.get("hermes_prompt") or out.get("prompt")
        if prompt and not out.get("hermes_prompt"):
            out["hermes_prompt"] = prompt
        out.setdefault("push_mode", PUSH_MODE_EXCERPT)
        out.setdefault("push_excerpt_max", DEFAULT_PUSH_EXCERPT_MAX)
    if out.get("task_type") == TASK_TYPE_ENGINEERING_LLM_REPORT:
        out.setdefault("report_kind", REPORT_KIND_ENGINEERING_LLM)
        out.setdefault("push_target", PUSH_TARGET_INDIVIDUAL)
        if isinstance(out.get("feishu_user_ids"), list):
            out["feishu_user_ids"] = list(out["feishu_user_ids"])
    webhooks = resolve_task_feishu_webhooks(out, include_env_fallback=False)
    user_ids = resolve_task_feishu_user_ids(out, include_env_fallback=False)
    if not webhooks:
        env_webhooks = resolve_env_feishu_webhooks()
        if env_webhooks and not out.get("feishu_webhooks"):
            out["feishu_webhooks"] = [
                {k: v for k, v in item.items() if k != "source"} for item in env_webhooks
            ]
            out["feishu_webhooks_source"] = "env"
    if not user_ids and not out.get("feishu_user_ids"):
        env_users = resolve_env_feishu_receive_user_ids()
        if env_users:
            out["feishu_user_ids"] = env_users
            out["feishu_user_ids_source"] = "env"
    out["push_channels"] = {
        "webhook_count": len(resolve_task_feishu_webhooks(out)),
        "user_count": len(resolve_task_feishu_user_ids(out)),
    }
    out["notification_channels"] = resolve_task_notification_channels(out, include_default=False) or (
        resolve_task_notification_channels(out, include_default=True)
    )
    if mask_secrets and isinstance(out.get("channel_config"), dict):
        masked_cfg: dict[str, Any] = {}
        for key, val in out["channel_config"].items():
            if not isinstance(val, dict):
                masked_cfg[key] = val
                continue
            entry = dict(val)
            for secret_key in ("secret", "password", "api_key"):
                if entry.get(secret_key):
                    entry[secret_key] = "***"
            masked_cfg[key] = entry
        out["channel_config"] = masked_cfg
    if mask_secrets:
        if out.get("feishu_webhooks"):
            out["feishu_webhooks"] = mask_feishu_webhooks(out.get("feishu_webhooks"))
        if out.get("feishu_webhook_url"):
            out["feishu_webhook_url"] = mask_webhook_url(str(out["feishu_webhook_url"]))
        if out.get("feishu_webhook_secret"):
            out["feishu_webhook_secret"] = "***"
    out["next_run_time"] = next_run.isoformat() if next_run else None
    out["next_run_time_fmt"] = format_display_dt(next_run) if next_run else None
    out["last_run_at_fmt"] = format_display_dt(out.get("last_run_at"))
    out["cron_description"] = describe_cron_expression(cron) if cron else None
    return out


def list_chat_scheduled_tasks(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    jobs = load_chat_scheduled_tasks()
    if enabled_only:
        jobs = [j for j in jobs if j.get("enabled", True)]
    return [_serialize_task(j) for j in jobs]


SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


def _load_scheduler_cfg() -> dict[str, Any]:
    import yaml

    if not SCHEDULE_PATH.exists():
        return {}
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    scheduler_cfg = data.get("scheduler")
    return scheduler_cfg if isinstance(scheduler_cfg, dict) else {}


def _merge_channel_config(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """合并任务 channel_config，保留未提交的敏感字段。"""
    merged = dict(existing)
    for key, val in incoming.items():
        if not isinstance(val, dict):
            merged[key] = val
            continue
        prev = merged.get(key) if isinstance(merged.get(key), dict) else {}
        entry = dict(prev)
        entry.update(val)
        for secret_key in ("secret", "password", "api_key"):
            new_val = val.get(secret_key)
            if new_val in (None, "", "***"):
                if prev.get(secret_key):
                    entry[secret_key] = prev[secret_key]
                else:
                    entry.pop(secret_key, None)
        merged[key] = entry
    return merged


def _apply_notification_channel_fields(
    task: dict[str, Any],
    *,
    notification_channels: Any = None,
    channel_config: Any = None,
    clear_missing: bool = False,
) -> None:
    """写入任务多渠道通知配置。"""
    if notification_channels is not None:
        if notification_channels == [] or (
            isinstance(notification_channels, str) and not str(notification_channels).strip()
        ):
            task.pop("notification_channels", None)
        else:
            task["notification_channels"] = normalize_notification_channels(notification_channels)
    elif clear_missing:
        task.pop("notification_channels", None)

    if channel_config is not None:
        if channel_config == {}:
            task.pop("channel_config", None)
        elif isinstance(channel_config, dict):
            if isinstance(task.get("channel_config"), dict):
                task["channel_config"] = _merge_channel_config(task["channel_config"], channel_config)
            else:
                task["channel_config"] = channel_config
    elif clear_missing:
        task.pop("channel_config", None)


def _apply_push_channel_fields(
    task: dict[str, Any],
    *,
    feishu_webhooks: Any = None,
    feishu_webhook_url: Any = None,
    feishu_webhook_secret: Any = None,
    feishu_user_ids: Any = None,
    clear_missing: bool = False,
) -> None:
    """写入任务飞书推送通道：群 Webhook 列表 + 人员列表（可同时配置）。"""
    if feishu_webhooks is not None:
        if feishu_webhooks == [] or (
            isinstance(feishu_webhooks, str) and not str(feishu_webhooks).strip()
        ):
            task.pop("feishu_webhooks", None)
            task.pop("feishu_webhooks_source", None)
        else:
            task["feishu_webhooks"] = parse_feishu_webhooks(feishu_webhooks)
            task.pop("feishu_webhooks_source", None)
    elif clear_missing:
        task.pop("feishu_webhooks", None)
        task.pop("feishu_webhooks_source", None)

    if feishu_webhook_url is not None:
        url = str(feishu_webhook_url).strip()
        if url:
            task["feishu_webhook_url"] = url
        else:
            task.pop("feishu_webhook_url", None)
    elif clear_missing:
        task.pop("feishu_webhook_url", None)

    if feishu_webhook_secret is not None:
        secret = str(feishu_webhook_secret).strip()
        if secret:
            task["feishu_webhook_secret"] = secret
        else:
            task.pop("feishu_webhook_secret", None)
    elif clear_missing:
        task.pop("feishu_webhook_secret", None)

    if feishu_user_ids is not None:
        if feishu_user_ids == [] or (
            isinstance(feishu_user_ids, str) and not str(feishu_user_ids).strip()
        ):
            task.pop("feishu_user_ids", None)
            task.pop("feishu_user_ids_source", None)
        else:
            task["feishu_user_ids"] = parse_feishu_user_ids(feishu_user_ids)
            task.pop("feishu_user_ids_source", None)
    elif clear_missing:
        task.pop("feishu_user_ids", None)
        task.pop("feishu_user_ids_source", None)


def create_chat_scheduled_task(
    *,
    task_type: str = TASK_TYPE_INCREMENTAL,
    site_id: Optional[str] = None,
    cron: str,
    name: Optional[str] = None,
    report_type: str = "site",
    feishu_push: bool = True,
    max_items: int = 10,
    hermes_prompt: Optional[str] = None,
    prompt: Optional[str] = None,
    push_mode: str = PUSH_MODE_EXCERPT,
    push_excerpt_max: int = DEFAULT_PUSH_EXCERPT_MAX,
    feishu_webhook_url: Optional[str] = None,
    feishu_webhook_secret: Optional[str] = None,
    feishu_webhooks: Optional[list[dict[str, Any]] | str] = None,
    top_n: int = 10,
    days: Optional[int] = None,
    report_kind: str = REPORT_KIND_ENGINEERING_LLM,
    push_target: str = PUSH_TARGET_INDIVIDUAL,
    feishu_user_ids: Optional[list[str] | str] = None,
    notification_channels: Optional[list[str] | str] = None,
    channel_config: Optional[dict[str, Any]] = None,
    enabled: bool = True,
    task_id: Optional[str] = None,
    created_by: str = "chat",
) -> dict[str, Any]:
    task_type = (task_type or TASK_TYPE_INCREMENTAL).strip()
    if task_type not in VALID_TASK_TYPES:
        raise ValueError(f"task_type 须为 {VALID_TASK_TYPES}，当前: {task_type}")

    cron_expr = _normalize_cron_with_extras(cron)
    jobs = load_chat_scheduled_tasks()
    new_id = (task_id or "").strip() or str(uuid.uuid4())
    if any(j.get("id") == new_id for j in jobs):
        raise ValueError(f"task_id 已存在: {new_id}")

    now = _now_iso()
    task: dict[str, Any] = {
        "id": new_id,
        "task_type": task_type,
        "cron": cron_expr,
        "feishu_push": bool(feishu_push),
        "enabled": bool(enabled),
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_run_status": None,
        "last_run_message": None,
    }
    _apply_push_channel_fields(
        task,
        feishu_webhooks=feishu_webhooks,
        feishu_webhook_url=feishu_webhook_url,
        feishu_webhook_secret=feishu_webhook_secret,
        feishu_user_ids=feishu_user_ids,
    )
    _apply_notification_channel_fields(
        task,
        notification_channels=notification_channels,
        channel_config=channel_config,
    )

    if task_type == TASK_TYPE_INCREMENTAL:
        if not site_id:
            raise ValueError("incremental_sync 需要 site_id")
        site = get_site_by_id(site_id)
        if not site:
            raise ValueError(f"站点不存在: {site_id}")
        if report_type not in ("site", "bim"):
            raise ValueError(f"report_type 须为 site 或 bim，当前: {report_type}")
        site_name = site.get("name") or site_id
        task.update(
            {
                "name": (name or "").strip() or f"{site_name} 增量+飞书 @ {cron_expr}",
                "site_id": site_id,
                "site_name": site_name,
                "report_type": report_type,
                "max_items": max(1, min(int(max_items), 200)),
            }
        )
    elif task_type in (TASK_TYPE_BIM_DAILY_ANALYSIS, TASK_TYPE_BIM_DAILY_BRIEF, TASK_TYPE_BIM_WEEKLY_BRIEF):
        default_names = {
            TASK_TYPE_BIM_DAILY_ANALYSIS: f"商机每日综合分析 @ {cron_expr}",
            TASK_TYPE_BIM_DAILY_BRIEF: f"商机日报推送 @ {cron_expr}",
            TASK_TYPE_BIM_WEEKLY_BRIEF: f"商机周报推送 @ {cron_expr}",
        }
        task_days = days
        if task_days is None:
            task_days = 30 if task_type == TASK_TYPE_BIM_WEEKLY_BRIEF else 1
        task.update(
            {
                "name": (name or "").strip() or default_names[task_type],
                "top_n": max(1, min(int(top_n), 30)),
                "days": max(1, min(int(task_days), 31)),
            }
        )
    elif is_hermes_task_type(task_type):
        effective_prompt = (hermes_prompt or prompt or "").strip()
        if not effective_prompt:
            raise ValueError("Hermes 任务需要 prompt / hermes_prompt")
        if push_mode not in (PUSH_MODE_EXCERPT, PUSH_MODE_FULL):
            raise ValueError(f"push_mode 须为 {PUSH_MODE_EXCERPT} 或 {PUSH_MODE_FULL}")
        short = effective_prompt.replace("\n", " ")[:40]
        task.update(
            {
                "name": (name or "").strip() or f"Hermes 总结 {short} @ {cron_expr}",
                "hermes_prompt": effective_prompt,
                "push_mode": push_mode,
                "push_excerpt_max": max(200, min(int(push_excerpt_max), MAX_PUSH_EXCERPT)),
            }
        )
    elif task_type == TASK_TYPE_ENGINEERING_LLM_REPORT:
        if report_kind != REPORT_KIND_ENGINEERING_LLM:
            raise ValueError(f"report_kind 须为 {REPORT_KIND_ENGINEERING_LLM}")
        if push_target not in VALID_PUSH_TARGETS:
            raise ValueError(f"push_target 须为 {VALID_PUSH_TARGETS}")
        if push_target != PUSH_TARGET_INDIVIDUAL:
            raise ValueError("engineering_llm_report 当前仅支持 push_target=individual")
        user_ids = task.get("feishu_user_ids") or parse_feishu_user_ids(feishu_user_ids)
        if not user_ids:
            raise ValueError("engineering_llm_report 需要至少一个 feishu_user_id")
        task["feishu_user_ids"] = user_ids
        task_days = max(1, min(int(days or 1), 31))
        task.update(
            {
                "name": (name or "").strip() or f"工程大模型应用报告 @ {cron_expr}",
                "report_kind": report_kind,
                "push_target": push_target,
                "feishu_user_ids": user_ids,
                "top_n": max(1, min(int(top_n), 30)),
                "days": task_days,
            }
        )
    else:
        raise ValueError(f"未知 task_type: {task_type}")

    jobs.append(task)
    save_chat_scheduled_tasks(jobs)
    return _serialize_task(task)


def update_chat_scheduled_task(task_id: str, **fields: Any) -> dict[str, Any]:
    jobs = load_chat_scheduled_tasks()
    target = None
    for job in jobs:
        if job.get("id") == task_id:
            target = job
            break
    if target is None:
        raise ValueError(f"任务不存在: {task_id}")

    if "site_id" in fields and fields["site_id"] is not None:
        site = get_site_by_id(fields["site_id"])
        if not site:
            raise ValueError(f"站点不存在: {fields['site_id']}")
        target["site_id"] = fields["site_id"]
        target["site_name"] = site.get("name") or fields["site_id"]
    if "cron" in fields and fields["cron"] is not None:
        target["cron"] = _normalize_cron_with_extras(fields["cron"])
    if "name" in fields and fields["name"] is not None:
        target["name"] = fields["name"].strip() or target.get("name")
    if "report_type" in fields and fields["report_type"] is not None:
        rt = fields["report_type"]
        if rt not in ("site", "bim"):
            raise ValueError(f"report_type 须为 site 或 bim")
        target["report_type"] = rt
    if "feishu_push" in fields and fields["feishu_push"] is not None:
        target["feishu_push"] = bool(fields["feishu_push"])
    if "max_items" in fields and fields["max_items"] is not None:
        target["max_items"] = max(1, min(int(fields["max_items"]), 200))
    if "top_n" in fields and fields["top_n"] is not None:
        target["top_n"] = max(1, min(int(fields["top_n"]), 30))
    if "days" in fields and fields["days"] is not None:
        target["days"] = max(1, min(int(fields["days"]), 31))
    if "enabled" in fields and fields["enabled"] is not None:
        target["enabled"] = bool(fields["enabled"])
    if "task_type" in fields and fields["task_type"] is not None:
        tt = fields["task_type"]
        if tt not in VALID_TASK_TYPES:
            raise ValueError(f"task_type 须为 {VALID_TASK_TYPES}")
        target["task_type"] = tt
    if "hermes_prompt" in fields and fields["hermes_prompt"] is not None:
        prompt = str(fields["hermes_prompt"]).strip()
        if not prompt:
            raise ValueError("hermes_prompt 不能为空")
        target["hermes_prompt"] = prompt
    if "prompt" in fields and fields["prompt"] is not None:
        prompt = str(fields["prompt"]).strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        target["hermes_prompt"] = prompt
    if "push_mode" in fields and fields["push_mode"] is not None:
        mode = str(fields["push_mode"]).strip()
        if mode not in (PUSH_MODE_EXCERPT, PUSH_MODE_FULL):
            raise ValueError(f"push_mode 须为 {PUSH_MODE_EXCERPT} 或 {PUSH_MODE_FULL}")
        target["push_mode"] = mode
    if "push_excerpt_max" in fields and fields["push_excerpt_max"] is not None:
        target["push_excerpt_max"] = max(
            200, min(int(fields["push_excerpt_max"]), MAX_PUSH_EXCERPT)
        )
    push_keys = (
        "feishu_webhooks",
        "feishu_webhook_url",
        "feishu_webhook_secret",
        "feishu_user_ids",
    )
    if any(key in fields for key in push_keys):
        _apply_push_channel_fields(
            target,
            feishu_webhooks=fields.get("feishu_webhooks"),
            feishu_webhook_url=fields.get("feishu_webhook_url"),
            feishu_webhook_secret=fields.get("feishu_webhook_secret"),
            feishu_user_ids=fields.get("feishu_user_ids"),
            clear_missing=any(
                key in fields and fields[key] is None for key in push_keys
            ),
        )
        if (
            target.get("task_type") == TASK_TYPE_ENGINEERING_LLM_REPORT
            and "feishu_user_ids" in fields
            and not target.get("feishu_user_ids")
        ):
            raise ValueError("feishu_user_ids 不能为空")
    notify_keys = ("notification_channels", "channel_config")
    if any(key in fields for key in notify_keys):
        _apply_notification_channel_fields(
            target,
            notification_channels=fields.get("notification_channels"),
            channel_config=fields.get("channel_config"),
            clear_missing=any(
                key in fields and fields[key] is None for key in notify_keys
            ),
        )
    if "report_kind" in fields and fields["report_kind"] is not None:
        rk = str(fields["report_kind"]).strip()
        if rk != REPORT_KIND_ENGINEERING_LLM:
            raise ValueError(f"report_kind 须为 {REPORT_KIND_ENGINEERING_LLM}")
        target["report_kind"] = rk
    if "push_target" in fields and fields["push_target"] is not None:
        pt = str(fields["push_target"]).strip()
        if pt not in VALID_PUSH_TARGETS:
            raise ValueError(f"push_target 须为 {VALID_PUSH_TARGETS}")
        target["push_target"] = pt
    target["updated_at"] = _now_iso()
    save_chat_scheduled_tasks(jobs)
    return _serialize_task(target)


def delete_chat_scheduled_task(task_id: str) -> dict[str, Any]:
    jobs = load_chat_scheduled_tasks()
    kept = [j for j in jobs if j.get("id") != task_id]
    if len(kept) == len(jobs):
        raise ValueError(f"任务不存在: {task_id}")
    save_chat_scheduled_tasks(kept)
    return {"ok": True, "deleted_id": task_id}


def toggle_chat_scheduled_task(task_id: str, *, enabled: Optional[bool] = None) -> dict[str, Any]:
    """切换或设置任务 enabled 状态。"""
    task = get_chat_scheduled_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    new_enabled = not task.get("enabled", True) if enabled is None else bool(enabled)
    return update_chat_scheduled_task(task_id, enabled=new_enabled)


def mark_task_run(task_id: str, result: dict[str, Any]) -> None:
    jobs = load_chat_scheduled_tasks()
    for job in jobs:
        if job.get("id") == task_id:
            job["last_run_at"] = _now_iso()
            job["last_run_status"] = "success" if result.get("ok") else "failed"
            job["last_run_message"] = (result.get("message") or result.get("error") or "")[:500]
            job["updated_at"] = _now_iso()
            break
    else:
        return
    save_chat_scheduled_tasks(jobs)


def build_site_sync_feishu_card(
    *,
    site_id: str,
    site_name: str,
    sync_result: dict[str, Any],
    top_items: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """将增量同步结果转为飞书 interactive card。"""
    success = sync_result.get("success", sync_result.get("ok"))
    new_count = sync_result.get("new_count", 0)
    error = sync_result.get("error") or ""
    status_text = "成功" if success else f"失败：{error[:120]}"
    template = "green" if success else "red"

    item_lines = []
    for idx, item in enumerate(top_items or [], start=1):
        title = (item.get("title") or "无标题").replace("\n", " ")[:80]
        url = item.get("url") or ""
        if url:
            item_lines.append(f"{idx}. [{title}]({url})")
        else:
            item_lines.append(f"{idx}. {title}")

    from src.core.config import get_settings

    detail_url = f"{get_settings().public_base_url.rstrip('/')}/tasks"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**站点**：{site_name}\n"
                    f"**同步状态**：{status_text}\n"
                    f"**新增/更新**：{new_count} 条"
                ),
            },
        },
    ]
    if item_lines:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**最新条目**\n" + "\n".join(item_lines[:10]),
                },
            }
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看任务页"},
                    "type": "primary",
                    "url": detail_url,
                }
            ],
        }
    )

    now_label = app_now().strftime("%Y-%m-%d %H:%M")
    return {
        "header": build_feishu_card_header(f"增量爬取报告 {now_label}", template=template),
        "elements": elements,
    }


def _query_recent_notices(site_id: str, limit: int = 10) -> list[dict[str, Any]]:
    try:
        from src.web.agri_insights_service import get_agri_insights_service

        latest = get_agri_insights_service().get_latest(limit=limit, days=7, site_id=site_id)
        return latest.get("items") or []
    except Exception as exc:
        logger.debug("查询最近公告失败: %s", exc)
        return []


def push_task_feishu_report(
    task: dict[str, Any],
    sync_result: dict[str, Any],
    *,
    webhook_client: Optional[FeishuWebhookClient] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """爬取完成后推送飞书报告。"""
    if not task.get("feishu_push", True):
        return {"ok": True, "skipped": True, "reason": "feishu_push_disabled"}

    report_type = task.get("report_type") or "site"
    site_id = task.get("site_id") or ""
    site_name = task.get("site_name") or site_id

    if report_type in ("bim", "agri"):
        from src.core.config import get_settings
        from src.web.agri_insights_service import get_agri_insights_service

        settings = get_settings()
        summary = get_agri_insights_service().get_summary(days=1, site_id=site_id or None)
        agri_count = summary.get("agri_count_in_window", 0)
        hot = summary.get("hot_keywords") or []
        hot_line = "、".join(f"{h['keyword']}({h['count']})" for h in hot[:5]) or "—"
        card = {
            "header": build_feishu_card_header(
                format_feishu_card_title(f"{site_name} · 数字农业招标简报"),
                template="green",
            ),
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**今日农业相关**：{agri_count} 条\n"
                            f"**热词**：{hot_line}\n"
                            f"**专题**：{settings.public_base_url.rstrip('/')}/insights"
                        ),
                    },
                }
            ],
        }
        result = push_task_feishu_card(task, card, dry_run=dry_run)
        return {**result, "report_type": "agri", "summary": summary}

    top_items = _query_recent_notices(site_id, limit=10)
    card = build_site_sync_feishu_card(
        site_id=site_id,
        site_name=site_name,
        sync_result=sync_result,
        top_items=top_items,
    )
    result = push_task_feishu_card(task, card, dry_run=dry_run)
    return {**result, "report_type": "site", "card": card}


def push_bim_analysis_task_feishu_report(
    task: dict[str, Any],
    report_result: dict[str, Any],
    *,
    webhook_client: Optional[FeishuWebhookClient] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """BIM 每日分析飞书推送已废弃。"""
    _ = task, report_result, webhook_client, dry_run
    return {
        "ok": False,
        "skipped": True,
        "reason": "bim_daily_analysis_deprecated",
        "report_type": "bim_daily_analysis",
        "error": "商机每日分析已废弃，请使用商机洞察专题",
    }


def build_hermes_run_feishu_card(
    *,
    task_name: str,
    run_result: dict[str, Any],
    push_mode: str = PUSH_MODE_EXCERPT,
    excerpt_max: int = DEFAULT_PUSH_EXCERPT_MAX,
) -> dict[str, Any]:
    """将 Hermes run 结果转为飞书 interactive card。"""
    success = bool(run_result.get("success") or run_result.get("ok"))
    output = (run_result.get("output") or "").strip()
    error = (run_result.get("error") or "").strip()
    tool_count = int(run_result.get("tool_count") or 0)
    run_id = (run_result.get("run_id") or "").strip()

    status_text = "成功" if success else f"失败：{error[:200] or '未知错误'}"
    template = "green" if success else "red"

    summary = extract_hermes_push_content(
        output,
        push_mode=push_mode,
        excerpt_max=excerpt_max,
    )

    from src.core.config import get_settings

    detail_url = f"{get_settings().public_base_url.rstrip('/')}/tasks"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**任务**：{task_name}\n"
                    f"**状态**：{status_text}\n"
                    f"**工具调用**：{tool_count} 次\n"
                    + (f"**Run ID**：`{run_id}`\n" if run_id else "")
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**执行摘要**\n{summary}"},
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看任务页"},
                    "type": "primary",
                    "url": detail_url,
                }
            ],
        },
    ]

    return {
        "header": build_feishu_card_header(
            format_feishu_card_title(f"{task_name} · {app_now().strftime('%Y-%m-%d')}"),
            template=template,
        ),
        "elements": elements,
    }


def push_hermes_task_feishu_report(
    task: dict[str, Any],
    run_result: dict[str, Any],
    *,
    webhook_client: Optional[FeishuWebhookClient] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Hermes 任务完成后推送飞书摘要。"""
    if not task.get("feishu_push", True):
        return {"ok": True, "skipped": True, "reason": "feishu_push_disabled"}

    card = build_hermes_run_feishu_card(
        task_name=task.get("name") or task.get("id") or "Hermes 任务",
        run_result=run_result,
        push_mode=str(task.get("push_mode") or PUSH_MODE_EXCERPT),
        excerpt_max=int(task.get("push_excerpt_max") or DEFAULT_PUSH_EXCERPT_MAX),
    )
    result = push_task_feishu_card(task, card, dry_run=dry_run)
    return {**result, "report_type": "hermes", "card": card}


async def run_hermes_scheduled_prompt(
    prompt: str,
    *,
    task_id: str,
    dry_run: bool = False,
    prompt_context: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """调用 Hermes Agent API 执行 prompt 并收集 run 结果。"""
    rendered_prompt = render_prompt_template(prompt, context=prompt_context)
    if dry_run:
        preview = extract_hermes_push_content(
            f"[dry-run 预览]\n{rendered_prompt[:400]}",
            push_mode=PUSH_MODE_EXCERPT,
            excerpt_max=400,
        )
        return {
            "ok": True,
            "success": True,
            "output": preview,
            "rendered_prompt": rendered_prompt,
            "tool_count": 0,
            "run_id": None,
        }

    from src.core.hermes_agent_chat_client import get_hermes_agent_chat_client

    client = get_hermes_agent_chat_client()
    if not client.available:
        return {
            "ok": False,
            "success": False,
            "error": "HERMES_AGENT_CHAT_URL / HERMES_AGENT_URL 未配置",
            "tool_count": 0,
        }

    trigger = f"chat_scheduled:{task_id}"
    instructions = (
        "你是 web_scraper 通知任务执行器。请完成用户指令并给出简洁中文结论。"
        "若任务要求飞书摘要，请在文末用「## 飞书摘要」标题单独给出推送段落。"
        f" trigger={trigger}"
    )
    run_id, err = await client.start_run(user_message=rendered_prompt, instructions=instructions)
    if err:
        return {"ok": False, "success": False, "error": err, "tool_count": 0}

    output = ""
    error = ""
    tool_count = 0
    terminal = False
    success = False

    async for event in client.stream_run_events(str(run_id)):
        ev_type = event.get("event") or ""
        if ev_type == "tool.completed":
            tool_count += 1
        elif ev_type == "run.completed":
            output = (event.get("output") or "").strip()
            success = True
            terminal = True
            break
        elif ev_type == "run.failed":
            error = (event.get("error") or "Hermes Agent 运行失败").strip()
            terminal = True
            break
        elif ev_type == "run.cancelled":
            error = "运行已取消"
            terminal = True
            break

    if not terminal:
        error = error or "Hermes 流结束但未收到终态事件"

    return {
        "ok": success,
        "success": success,
        "output": output,
        "rendered_prompt": rendered_prompt,
        "error": error or None,
        "tool_count": tool_count,
        "run_id": run_id,
    }


async def _run_incremental_scheduled_task(
    task: dict[str, Any],
    task_id: str,
    *,
    browser_pool: Any = None,
    pipeline: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行 incremental_sync 类型任务。"""
    site_id = task.get("site_id") or ""
    max_items = int(task.get("max_items") or 10)
    trigger = f"chat_scheduled:{task_id}"

    from src.core.hermes_agent_runner import run_scheduler_site_sync

    logger.info("执行增量定时任务 %s site=%s max_items=%d", task_id, site_id, max_items)
    try:
        sync_result = await run_scheduler_site_sync(
            site_id,
            trigger=trigger,
            browser_pool=browser_pool,
            pipeline=pipeline,
            max_items=max_items,
        )
    except Exception as exc:
        logger.exception("增量定时任务 sync 异常: %s", exc)
        sync_result = {"success": False, "ok": False, "error": str(exc), "site_id": site_id}

    success = bool(sync_result.get("success") or sync_result.get("ok"))
    feishu_result: dict[str, Any] = {"skipped": True}
    if task.get("feishu_push", True):
        feishu_result = push_task_feishu_report(task, sync_result, dry_run=dry_run)

    ok = success and (feishu_result.get("ok") or feishu_result.get("skipped"))
    message_parts = [
        f"sync={'成功' if success else '失败'}",
        f"新增 {sync_result.get('new_count', 0)} 条",
    ]
    if feishu_result.get("skipped"):
        message_parts.append(f"飞书跳过({feishu_result.get('reason')})")
    elif feishu_result.get("ok"):
        message_parts.append("飞书已推送")
    else:
        message_parts.append(f"飞书失败({feishu_result.get('error')})")

    return {
        "ok": ok,
        "task_id": task_id,
        "task_type": TASK_TYPE_INCREMENTAL,
        "site_id": site_id,
        "sync": sync_result,
        "feishu": feishu_result,
        "message": "；".join(message_parts),
    }


async def _run_hermes_scheduled_task(
    task: dict[str, Any],
    task_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行 hermes_prompt 类型任务。"""
    prompt = (task.get("hermes_prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "hermes_prompt 为空", "task_id": task_id}

    logger.info("执行 Hermes 定时任务 %s prompt_len=%d", task_id, len(prompt))
    run_result = await run_hermes_scheduled_prompt(
        prompt,
        task_id=task_id,
        dry_run=dry_run,
        prompt_context=build_prompt_context(),
    )

    feishu_result: dict[str, Any] = {"skipped": True}
    if task.get("feishu_push", True):
        feishu_result = push_hermes_task_feishu_report(task, run_result, dry_run=dry_run)

    success = bool(run_result.get("success") or run_result.get("ok"))
    if not success:
        from src.core.llm_alert_service import maybe_alert_on_llm_error

        hermes_err = run_result.get("error") or run_result.get("output") or ""
        maybe_alert_on_llm_error(hermes_err, source=f"hermes_scheduled:{task_id}")

    ok = success and (feishu_result.get("ok") or feishu_result.get("skipped"))
    message_parts = [
        f"hermes={'成功' if success else '失败'}",
        f"工具 {run_result.get('tool_count', 0)} 次",
    ]
    if feishu_result.get("skipped"):
        message_parts.append(f"飞书跳过({feishu_result.get('reason')})")
    elif feishu_result.get("ok"):
        message_parts.append("飞书已推送")
    else:
        message_parts.append(f"飞书失败({feishu_result.get('error')})")

    return {
        "ok": ok,
        "task_id": task_id,
        "task_type": task.get("task_type") or TASK_TYPE_HERMES_SUMMARY,
        "hermes": run_result,
        "feishu": feishu_result,
        "message": "；".join(message_parts),
    }


async def _run_bim_brief_scheduled_task(
    task: dict[str, Any],
    task_id: str,
    *,
    weekly: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """BIM 日报/周报任务已废弃，请使用商机洞察专题（8091）。"""
    _ = task, weekly, dry_run
    label = "周报" if weekly else "日报"
    task_type = TASK_TYPE_BIM_WEEKLY_BRIEF if weekly else TASK_TYPE_BIM_DAILY_BRIEF
    return {
        "ok": False,
        "task_id": task_id,
        "task_type": task_type,
        "error": f"商机{label}任务已废弃，请使用商机洞察（8091）与 daily_agri_sync",
        "message": f"商机{label}任务已废弃",
    }


async def _run_bim_daily_analysis_scheduled_task(
    task: dict[str, Any],
    task_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """BIM 每日分析任务已废弃，请使用商机洞察。"""
    _ = task, dry_run
    return {
        "ok": False,
        "task_id": task_id,
        "task_type": TASK_TYPE_BIM_DAILY_ANALYSIS,
        "error": "商机每日分析任务已废弃，请使用 crawl_agri_insights 或 8091 商机洞察专题",
        "message": "商机每日分析任务已废弃",
    }


async def _run_engineering_llm_report_scheduled_task(
    task: dict[str, Any],
    task_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行 engineering_llm_report：筛选工程大模型应用标讯并私信推送。"""
    top_n = int(task.get("top_n") or 10)
    days = int(task.get("days") or 1)
    user_ids = task.get("feishu_user_ids") or []
    logger.info(
        "执行工程大模型应用报告任务 %s days=%d top_n=%d users=%s",
        task_id,
        days,
        top_n,
        ",".join(user_ids),
    )

    from src.core.engineering_llm_report_service import push_engineering_llm_report

    force_no_push = dry_run or not task.get("feishu_push", True)
    try:
        report_result = push_engineering_llm_report(
            feishu_user_ids=user_ids,
            days=days,
            top_n=top_n,
            dry_run=force_no_push,
        )
    except Exception as exc:
        logger.exception("工程大模型应用报告任务异常: %s", exc)
        return {"ok": False, "error": str(exc), "task_id": task_id}

    feishu_result: dict[str, Any] = {
        "ok": report_result.get("ok"),
        "skipped": report_result.get("skipped", False),
        "reason": report_result.get("reason"),
        "error": report_result.get("error"),
        "pushed_count": report_result.get("pushed_count", 0),
    }
    count = (report_result.get("brief") or {}).get("count", 0)
    ok = bool(report_result.get("ok")) and (
        feishu_result.get("ok") or feishu_result.get("skipped")
    )
    message_parts = [
        f"简报={'成功' if report_result.get('ok') else '失败'}",
        f"近{days}天 {count} 条",
        f"收件人 {len(user_ids)} 人",
    ]
    if feishu_result.get("skipped"):
        message_parts.append(f"飞书跳过({feishu_result.get('reason') or 'dry_run'})")
    elif feishu_result.get("ok"):
        message_parts.append(f"已推送 {feishu_result.get('pushed_count', 0)} 人")
    else:
        message_parts.append(f"飞书失败({feishu_result.get('error')})")

    return {
        "ok": ok,
        "task_id": task_id,
        "task_type": TASK_TYPE_ENGINEERING_LLM_REPORT,
        "report": report_result,
        "feishu": feishu_result,
        "message": "；".join(message_parts),
    }


async def run_chat_scheduled_task_async(
    task_id: str,
    *,
    browser_pool: Any = None,
    pipeline: Any = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """执行单次定时任务：incremental sync 或 Hermes prompt，完成后可选推飞书。"""
    task = get_chat_scheduled_task(task_id)
    if not task:
        return {"ok": False, "error": f"任务不存在: {task_id}"}
    if not task.get("enabled", True) and not force:
        return {"ok": False, "error": f"任务已禁用: {task_id}"}

    task_type = task.get("task_type") or TASK_TYPE_INCREMENTAL
    if is_hermes_task_type(task_type):
        result = await _run_hermes_scheduled_task(task, task_id, dry_run=dry_run)
    elif task_type == TASK_TYPE_BIM_DAILY_ANALYSIS:
        result = await _run_bim_daily_analysis_scheduled_task(task, task_id, dry_run=dry_run)
    elif task_type == TASK_TYPE_BIM_DAILY_BRIEF:
        result = await _run_bim_brief_scheduled_task(task, task_id, weekly=False, dry_run=dry_run)
    elif task_type == TASK_TYPE_BIM_WEEKLY_BRIEF:
        result = await _run_bim_brief_scheduled_task(task, task_id, weekly=True, dry_run=dry_run)
    elif task_type == TASK_TYPE_ENGINEERING_LLM_REPORT:
        result = await _run_engineering_llm_report_scheduled_task(task, task_id, dry_run=dry_run)
    else:
        result = await _run_incremental_scheduled_task(
            task,
            task_id,
            browser_pool=browser_pool,
            pipeline=pipeline,
            dry_run=dry_run,
        )

    mark_task_run(task_id, result)

    # 非飞书渠道（或 feishu_push 关闭时的飞书文本）统一推送
    try:
        notify_result = push_task_notifications(
            task,
            result,
            dry_run=dry_run,
            skip_feishu=bool(task.get("feishu_push", True)),
        )
        result["notifications"] = notify_result
        if notify_result.get("pushed_count"):
            extra = f"；多渠道推送 {notify_result.get('pushed_count')} 个"
            result["message"] = (result.get("message") or "") + extra
    except Exception as exc:
        logger.warning("多渠道通知推送异常: %s", exc)
        result["notifications"] = {"ok": False, "error": str(exc)}

    if not result.get("ok"):
        from src.core.llm_alert_service import maybe_alert_on_llm_error

        err = result.get("error") or result.get("message") or ""
        maybe_alert_on_llm_error(
            err,
            source=f"scheduled_task:{task_type}:{task_id}",
        )
    return result


def build_cron_trigger_for_task(cron_expr: str, *, tz: Optional[ZoneInfo] = None):
    return build_cron_trigger(cron_expr, tz=tz or _default_timezone())
