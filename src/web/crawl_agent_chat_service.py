"""Hermes 爬虫运维对话 Agent — 经 hermes-agent API Server（/v1/runs）驱动，SSE 事件流。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from src.core.config import get_settings
from src.core.crawl_rules import load_crawl_rule
from src.core.chat_scheduled_tasks import (
    build_bim_analysis_schedule_ui_payload,
    build_scheduled_tasks_panel_ui_payload,
    detect_bim_analysis_schedule_ui_intent,
    detect_scheduled_bim_analysis_feishu_intent,
    detect_scheduled_feishu_intent,
    detect_scheduled_hermes_feishu_intent,
    detect_scheduled_tasks_panel_ui_intent,
)
from src.core.hermes_agent_chat_client import (
    HermesAgentChatClient,
    get_hermes_agent_chat_client,
    resolve_hermes_agent_chat_url,
)
from src.core.rule_generator import load_rule_yaml
from src.core.site_sync import get_site_by_id
from src.core.llm_chat import truncate_chat_history
from src.web.crawl_agent_tools import build_executed_command, format_tool_command, resolve_site_id
from src.web.crawl_agent_cancel import is_chat_cancelled

import httpx

logger = logging.getLogger(__name__)

MAX_AGENT_TURNS = 100
DEFAULT_HERMES_MAX_ITERATIONS = 180

_FULL_CRAWL_KEYWORDS = ("全部", "全量", "所有", "完整", "500页", "10000")
_CRAWL_ACTION_PATTERN = re.compile(r"[爬抓]")
_ONBOARDING_KEYWORDS = ("添加站点", "接入新", "接入站点", "注册站点", "新站点", "站点接入", "onboard")
_ONBOARDING_PATTERN = re.compile(r"接入.*(url|URL|网站|站点|域名)", re.I)


def _message_has_full_crawl_keywords(text: str) -> bool:
    return any(kw in text for kw in _FULL_CRAWL_KEYWORDS)


def _message_has_crawl_action(text: str) -> bool:
    return bool(_CRAWL_ACTION_PATTERN.search(text))


def _get_site_effective_max_pages(site_id: str) -> Optional[int]:
    try:
        if load_rule_yaml(site_id) is None:
            return None
        site = get_site_by_id(site_id)
        if not site:
            return None
        rule = load_crawl_rule(site)
        if not rule:
            return None
        max_pages = rule.limits.max_pages
        if rule.pagination and rule.pagination.max_pages:
            max_pages = min(max_pages, rule.pagination.max_pages)
        return max_pages
    except Exception:
        logger.debug("读取站点 %s 规则 max_pages 失败", site_id, exc_info=True)
        return None


def detect_onboarding_intent(user_message: str) -> bool:
    """启发式检测用户是否要添加/接入新站点。"""
    text = (user_message or "").strip()
    if not text:
        return False
    lower = text.lower()
    if any(kw in text or kw in lower for kw in _ONBOARDING_KEYWORDS):
        return True
    return bool(_ONBOARDING_PATTERN.search(text))


def build_site_register_ui_event() -> ChatStreamEvent:
    from src.core.site_onboarding import ONBOARDING_STAGES, STAGE_LABELS

    return ChatStreamEvent(
        type="ui",
        content="请在下方表单填写站点信息，注册后将进入探查阶段。",
        data={
            "component": "site_register_form",
            "title": "录入新站点",
            "fields": [
                {
                    "name": "name",
                    "label": "站点名称",
                    "type": "text",
                    "placeholder": "例如：电建公共资源交易服务平台",
                    "required": True,
                },
                {
                    "name": "url",
                    "label": "入口 URL",
                    "type": "url",
                    "placeholder": "https://bid.powerchina.cn",
                    "required": True,
                },
            ],
            "submit_action": "register_site",
            "stages": [{"key": k, "label": STAGE_LABELS[k]} for k in ONBOARDING_STAGES],
        },
    )


def build_bim_analysis_schedule_ui_event(
    *,
    defaults: Optional[dict[str, Any]] = None,
) -> ChatStreamEvent:
    payload = build_bim_analysis_schedule_ui_payload(defaults=defaults)
    return ChatStreamEvent(
        type="ui",
        content="请配置 BIM 每日分析通知任务：查询今日新增标讯、LLM 综合分析后推送飞书。",
        data=payload,
    )


def build_scheduled_tasks_panel_ui_event() -> ChatStreamEvent:
    payload = build_scheduled_tasks_panel_ui_payload()
    return ChatStreamEvent(
        type="ui",
        content="以下为已配置的通知任务，可直接启用/禁用或删除。",
        data=payload,
    )


def build_onboarding_progress_ui(status: dict[str, Any]) -> ChatStreamEvent:
    from src.core.site_onboarding import ONBOARDING_STAGES, STAGE_LABELS

    stages = status.get("stages") or {}
    return ChatStreamEvent(
        type="ui",
        data={
            "component": "onboarding_stepper",
            "site_id": status.get("site_id"),
            "name": status.get("name"),
            "url": status.get("url"),
            "current_stage": status.get("current_stage"),
            "stages": [{"key": k, "label": STAGE_LABELS[k], "status": stages.get(k, "pending")} for k in ONBOARDING_STAGES],
        },
    )


def detect_full_crawl_intent(user_message: str) -> bool:
    """启发式检测用户是否要求全量爬取。"""
    text = (user_message or "").strip()
    if not text:
        return False
    if _message_has_full_crawl_keywords(text):
        return True
    if _message_has_crawl_action(text):
        site_id = resolve_site_id(text)
        if site_id:
            max_pages = _get_site_effective_max_pages(site_id)
            if max_pages is not None and max_pages >= 100:
                return True
    return False


def resolve_max_turns_for_message(
    user_message: str,
    *,
    default_max: int,
    full_max: int,
) -> tuple[int, bool]:
    if detect_full_crawl_intent(user_message):
        return full_max, True
    return default_max, False


def resolve_hermes_max_iterations() -> int:
    """读取 hermes-agent 实际推理预算（HERMES_MAX_ITERATIONS > config.yaml > 默认）。"""
    raw = (os.getenv("HERMES_MAX_ITERATIONS") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.debug("无效的 HERMES_MAX_ITERATIONS=%s", raw)

    config_path = Path(__file__).resolve().parents[2] / "data" / "hermes-agent" / "config.yaml"
    if config_path.is_file():
        try:
            import yaml

            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            agent_cfg = payload.get("agent") or {}
            max_turns = agent_cfg.get("max_turns")
            if max_turns is not None:
                return max(1, int(max_turns))
        except Exception:
            logger.debug("读取 %s agent.max_turns 失败", config_path, exc_info=True)

    return DEFAULT_HERMES_MAX_ITERATIONS


def detect_max_iterations_reached(
    event: dict[str, Any],
    *,
    tool_turns: int,
    max_iterations: int,
) -> bool:
    """判断 Hermes run 是否因推理轮次耗尽而可继续。"""
    if event.get("partial") is True:
        return True
    if event.get("completed") is False:
        return True
    reason = str(
        event.get("reason")
        or event.get("stop_reason")
        or event.get("finish_reason")
        or ""
    ).lower()
    if "max_iterations" in reason or "max_turns" in reason:
        return True
    if max_iterations > 0 and tool_turns >= max_iterations:
        return True
    return False


SYSTEM_PROMPT = """你是 Hermes 爬虫运维 Agent，通过 **skill 分步工具** 完成站点爬取（不触发整站 sync）。

## 新爬虫默认流程（最高优先级）
用户说「帮我爬取 XXX 网站」「接入新站」「配置爬取规则」且站点 **无有效 crawl_rules**（generic / 占位 / `skill_plan_crawl_path` 报缺规则）时，**不要**直接 `skill_fetch_list_page` / Playwright / 写一次性脚本，按以下 SOP：

1. `crawl_resolve_site` 或注册新站（`POST /api/sites/register`）
2. `skill_webbridge_check` → `skill_webbridge_navigate(entry_url, new_tab=true)`
3. 必要时 click/wait 进入公告列表 → `skill_analyze_list_dom(session_id=…)` 得 `page_hints`
4. `crawl_generate_rule(page_hints=…)` 或 `crawl_generate_workflow` → `crawl_validate_rule` → `crawl_save_rule`
5. `crawl_test(site_id, max_pages=1)` 试跑 ≥1 条合理公告
6. 通过后 `crawl_enable_site` → `crawl_schedule_status` 确认 `schedule_eligible=true`
7. 再按已有规则执行爬取（WebBridge 优先，失败回退 rules 驱动的 HTTP/Playwright）

**Skill 加载顺序**：`webbridge-crawl` → `crawl-rule-generation` → `crawl-path`（仅已有规则时分步爬取）。
**例外（保持原流程）**：微信公众号（chain-only 脚本）、BIM 专用 HTTP 脚本（如 `sync_powerchina_bim_http.py`）、用户明确要求「只用 API/HTTP/Playwright」。

## 编排架构
- **定时爬取 / Tasks 页手动 sync**：经 **Hermes Agent Runner**（`src/core/hermes_agent_runner.py`）headless 执行整站 sync，不经 LLM、不占用对话轮次
- **本对话页**：skill 分步工具（plan → fetch → save），用于探索、Remediation、规则调试
- 关闭 Runner：`CRAWL_VIA_HERMES=false` 时 scheduler / trigger 回退直连 `sync_site`

## 爬取范围
- 默认 **国央企 + 行业平台**（`schedule.yaml` → `crawl_scope: soe_and_industry`）
- `sites.yaml` 中 `enabled: true` 且满足以下之一：`soe: true`（国央企）或 `industry: true`（行业扩展，如 dlzb_power、ggzy_上海市）
- 整站 sync / BIM sync / 定时爬取只处理上述 enabled 站；其余站点仍 `enabled: false`
- 统计与 BIM 洞察页面数据范围与 crawl_scope 一致

## 能力
- 解析用户口语（如 tjbid、铁建物资采购网）为 site_id
- 读取 crawl_rules，**自动规划**爬取路径（入口 → 翻页 → 列表项 → 详情）
- 逐页抓取列表、逐条保存公告，SSE 推送 url_crawl 进度
- 查询已爬公告、检查任务状态（诊断用）

## SOP（用户要求爬取某站时）
1. `crawl_resolve_site` 或已知 site_id
2. `crawl_get_rule` — 若无有效 `list_page` → 走上方 **新爬虫默认流程**，**跳过**本 SOP 第 3–4 步的直接抓取
3. `skill_plan_crawl_path`（仅已有规则）— 输出 entry_urls、pagination_strategy、agent_max_pages、path_tree；`recommended_list_tool` 为 WebBridge 首选，`fallback_list_tool` 为回退
4. **WebBridge 优先**（除非用户明确「只用 API/HTTP/Playwright」）：
   - **第一步** `skill_webbridge_check`
   - 可用 → `skill_webbridge_navigate(url, new_tab=true)` → `skill_webbridge_extract_list(selector 或 hint)` → 逐条 `skill_save_notice` → 任务结束 `skill_webbridge_close(close_session=true)`
   - 规则调试：`skill_webbridge_get_html` 辅助 selector
   - check 失败 → **graceful degrade**：`crawl_notify_user` / `crawl_request_user_input`，**不要 crash**；向用户说明 WebBridge 不可用
5. **WebBridge 不可用或抓取失败时回退**（使用 `fallback_list_tool`）：
   - **若 list_page.strategy=api**（如 bid.powerchina.cn）— 用户未特别声明时仍先步骤 4；回退时用 `skill_http_fetch_list(page_num=N)`（零 browser）
   - 分页 type=ajax_param：**禁止** `skill_paginate`；连续 `skill_http_fetch_list(page_num=2/3/…)`
   - 详情若 `detail.strategy=api` → `skill_fetch_detail_page`（HTTP getInfo）或 `skill_fetch_notice_content(notice_id=…)`（PDF 正文）
   - HTTP 500/403/超时：检查 `HTTP_PROXY`/`HTTPS_PROXY`、VPN，或 `scripts/crawl_powerchina_http.py`
   - **DOM 站回退**：对 page_num = 1 .. agent_max_pages：
     - **page_number 分页**（如 tjbid）：`skill_fetch_list_page(page_num=N)`，**禁止** `skill_paginate`
     - **next_button 分页**：page_num > 1 时先 `skill_paginate`，再 `skill_fetch_list_page`
     - 每条 item：若规则 fetch_detail → `skill_fetch_detail_page` 再 `skill_save_notice`；否则直接 `skill_save_notice`
6. `crawl_query_notices` 汇总结果

## 效率（减少推理轮次）
- 每轮可发起**多个** tool_calls；优先在一轮内完成「抓一页 + 保存该页全部条目」
- page_number 站点：连续页用 `fetch_list(page_num=2/3/…)`，不要 paginate
- 用户说「全部/全量」时，`skill_plan_crawl_path` 传较大 max_pages（如 50–100），分批执行；轮次不够时用户会发「继续」

## 禁止
- **不要**调用 `crawl_trigger` / `crawl_poll_until_done`（整站 sync 旧流程）
- 同站已在跑 legacy sync 时勿重复 trigger
- page_number 策略**不要**调用 `skill_paginate`
- **list_page.strategy=api 且 WebBridge 已失败**：回退 `skill_http_fetch_list`，**不要** Playwright goto entry_url（会 timeout / IP 封）

## HTTP API 失败时
- 向用户说明：本机 curl 也 500 时多为 IP 封锁，需 `HTTP_PROXY`/`HTTPS_PROXY`、VPN 或在内网 cron 跑 `scripts/crawl_powerchina_http.py`
- sync_stale（任务页「同步中（异常）」）：先 `POST /api/sync/{site_id}/reset` 或点「重置状态」，再重试

## 批次策略
- 全量可达 500 页；用户未说「全部」时先按 plan 的 agent_max_pages（通常 3–5 页）验证
- 遵守 crawl_rules limits.rate_limit_seconds

## 站点注册
- 站点元数据在 **config/sites.yaml**（`get_site_by_id` 只读此文件，**不读 MongoDB sites 集合**）
- **添加新站点在对话内完成**：用户说「添加站点」「接入新 URL」时，系统会展示注册表单；也可引导用户填写 URL → `POST /api/sites/register` → 继续规则生成或 `skill_plan_crawl_path` 探查
- Onboarding 五阶段：录入 → 探查 → 分析 → 工具链 → 调度；注册后当前阶段为「探查」
- `skill_save_notice` 若报「站点未注册」，在 sites.yaml 添加条目或在 `src/core/site_aliases.py` 添加别名；**勿** mongosh 插入 sites 集合
- 用户回复「已激活」表示 sites.yaml/别名已就绪，可继续 `skill_save_notice`

## 自动生成 crawl_rules（generic / 新站 / 占位规则）
当用户要求「配置爬取规则」「生成 crawl_rules」「接入新站并爬取」且站点为 **generic** 或 **无有效 list_page** 时，走规则生成 SOP（**不要**直接整站 sync）：

1. `crawl_resolve_site` 或已知 `site_id`；`crawl_get_rule` 查看现有规则/占位
2. **WebBridge 探查列表页**（优先）：
   - `skill_webbridge_check` → `skill_webbridge_navigate(entry_url, new_tab=true)`
   - 必要时 `skill_webbridge_click` / `skill_webbridge_wait` 进入公告列表
   - `skill_webbridge_get_html` 或 `skill_analyze_list_dom(session_id=…)` → 得到 `page_hints` 与候选 selectors
   - WebBridge 不可用 → `crawl_generate_rule` 仅靠 HTTP 页面提示，或 `crawl_request_user_input` 请用户提供列表 URL
3. **生成规则**：`crawl_generate_rule(site_id, url, page_hints=…)` 或 `crawl_generate_workflow`（输出可视化工作流节点，可在 `/crawl-rules/{site_id}/workflow` 手动微调）或一步式 `crawl_generate_script(save=true, dry_run=true, page_hints=…)`
4. **校验落盘**：`crawl_validate_rule` → `crawl_save_rule`（若 generate 未 save）
5. **试跑**：`crawl_test(site_id, max_pages=1)` — 必须 ≥1 条合理列表项才算通过
6. **启用调度**：试跑通过后 `crawl_enable_site(site_id)` → `crawl_schedule_status` 确认 `schedule_eligible=true`
7. 试跑失败：根据 errors 调整 selectors，用 `page_hints` + messages 重新 `crawl_generate_rule`，重复 4–6

**快捷一步式**（探查信息已齐全时）：`crawl_generate_script(site_id, page_hints=…, save=true, dry_run=true)` → 通过则 `crawl_enable_site`

**资格**：`generic` 站配置有效 `crawl_rules.list_page` 后 `site_eligible_for_schedule` 为 true，可纳入 per-site interval 调度。

**限制**：登录墙/验证码需 `crawl_request_user_input`；重度 SPA 可能需 `list_page.strategy=api` 或 `dom_after_ajax`；占位规则（container=body）试跑通常会失败，必须重新生成。

## 执行 Python 代码
- 用户要求「运行这段代码」「执行脚本看结果」「验证生成的 Python」时，使用 `crawl_execute_code`
- **mode=script**：白名单 scripts/ 脚本 + args，如 `run_once.py --site-id xxx`；与 `crawl_schedule_script(action=run)` 不同，**立即同步执行**并返回 stdout/stderr
- **mode=code**：传入完整 Python 源码；写入 `data/generated_scripts/` 后 subprocess 运行，默认超时 120s
- 执行后向用户展示 stdout/stderr 与 exit code；失败时根据 stderr 修正代码可再次调用
- 安全限制：禁止 shell、禁止 subprocess/eval/exec 等危险 import；路径限定在项目内；输出过长会截断

## 定时批量入库（scripts/ Cron）
- 用户要求「每天 X 点跑某脚本批量入库」时，使用 `crawl_schedule_script`（**不要**用 crawl_trigger）
- 常用脚本：`crawl_powerchina_http.py`（电建 HTTP）、`run_daily_sync.py`、`run_daily_bim_sync.py`、`run_bim_crawl_all.py`
- **BIM 每日任务**：调度器 03:00 经 Hermes Runner 跑 `daily_bim_sync`；对话内可用 `crawl_bim_sync_all` 手动触发（同样走 Runner）
- cron 示例：`0 3 * * *` 或口语「每天 3 点」；args 如 `["--max-items","10"]`
- 创建后返回 job_id、cron、next_run_time；列出/删除/更新用 action=list|get|update|delete
- 任务由 **run_scheduler.py** 执行；新建后约 60s 自动加载，或重启 scheduler 立即生效

## 增量爬取 + 飞书报告（对话通知任务）
- 用户说「每天 X 点爬取某站增量并推飞书」时，使用 `crawl_schedule_incremental_feishu`
- **快捷**：action=create_from_intent + user_message=用户原话，可解析站点、cron、BIM 报告类型、可选 webhook
- 或 action=create，显式传 site_id、cron、report_type（site|bim）、max_items、feishu_webhook_url
- 飞书 Webhook **优先任务级 feishu_webhook_url**，否则读 **FEISHU_WEBHOOK_URL**；BIM 报告需 **BIM_BRIEF_ENABLED=true**
- 任务持久化 `data/chat_scheduled_tasks.json`（task_type=incremental_sync）；到点经 Hermes Runner 增量 sync，完成后推飞书卡片

## Hermes 任务 + 飞书摘要（对话通知任务）
- 用户说「每天 X 点执行 Hermes 任务并推飞书」时使用 `crawl_schedule_hermes_feishu`
- create_from_intent 解析 cron、hermes_prompt、可选 feishu_webhook_url
- 到点调用 Hermes Agent `/v1/runs` 执行指令，完成后推送执行摘要卡片（task_type=hermes_prompt）
- 示例：「每天上午9点执行 Hermes 任务：统计今日 BIM 公告，推飞书，webhook 为 https://open.feishu.cn/...」

## BIM 每日综合分析 + 飞书（对话通知任务）
- 用户说「每天 X 点分析今日 BIM 公告并推飞书」时使用 `crawl_schedule_bim_analysis_feishu`
- **快捷 UI**：用户说「创建 BIM 每日分析通知任务」时展示配置表单（生成式 UI）
- create_from_intent 解析 cron、可选 webhook；到点查询今日新增 BIM 标讯，LLM 综合分析后推飞书卡片（task_type=bim_daily_analysis）
- 与 incremental_sync（先爬取再简报）不同：本任务**不爬取**，仅分析已有数据
- 与 hermes_prompt 不同：使用内置 BIM 分析流水线，非自由 Hermes prompt

## 输出要求
- 每轮先简要说明思考与计划（中文）
- 最终 Markdown 汇总：site_id、路径、保存条数、skipped 重复数
"""

AGRI_SYSTEM_PROMPT = """你是 **商机洞察** Hermes 爬虫 Agent，从七大央企招采数据源筛选、爬取商机相关公告并生成洞察。

## 重要概念
- 下列 7 个网站是 **央企通用招采平台/监测数据源**，**不是**农业专用招标网
- 8091 专题 = 从这 7 个数据源爬取公告后，按关键词（智慧农业、数字农业、乡村振兴等）筛选商机相关项目

## 监测数据源（仅此 7 站）
- 云筑网（中国建筑集团有限公司_云筑网）
- 鲁班商务网（crec_bidding）
- 电力招标网（dlzb_power）
- 铁建物资采购网（中国铁道建筑集团有限公司_物资采购网）
- 中交供应链（中国交通建设集团有限公司_供应链管理信息系统）
- 电建招采（中国电力建设集团有限公司_公共资源交易服务平台）
- 能建电子采购（中国能源建设集团有限公司_电子采购平台）
- 配置见 `config/agri_sites.yaml`；**不要**爬取 ggzy_上海市 等 industry 扩展站

## 商机洞察能力
- `crawl_agri_sync_all` — 触发 7 站监测源 sync（后台 daily_agri_sync）
- `crawl_agri_insights` — 聚合近 N 天商机洞察，返回 8091 洞察页 view_url
- 爬取后公告经 Pipeline 关键词/LLM 判定 `agri_tags`（智慧农业、数字农业、农机等）

## 爬取 SOP（与主站相同 skill 分步，范围限定 7 站监测源）
1. `crawl_resolve_site` 确认 site_id 属于上述 7 站之一
2. `skill_plan_crawl_path` 规划路径
3. **WebBridge 优先**：先 `skill_webbridge_check` → navigate → extract → save；不可用或失败时回退 `skill_http_fetch_list`（API 站）或 `skill_fetch_list_page`（DOM 站）
4. 用户要「同步全部监测站」→ `crawl_agri_sync_all`；要「看洞察/统计」→ `crawl_agri_insights`
5. 禁止 `crawl_trigger` 整站 sync（非监测源范围）

## 禁止
- 爬取 7 站以外的 enabled 站点
- 整站 legacy sync（`crawl_trigger` / `crawl_poll_until_done`）

## 输出要求
- 中文简要说明；汇总 site_id、保存条数、关键词命中情况
- 洞察/同步完成后给出 8091 商机洞察专题页链接（`view_url`）
"""

STOCK_SYSTEM_PROMPT = """你是 **股票领域分析** Hermes Agent，结合 AKShare 财经快讯、招采公告与国内政府政策（十五五），生成产业发展分析与投资方向框架。

## 重要概念
- 8092 专题 = 独立股票分析平台，与招标（8090）、商机洞察（8091）分离
- 数据来源：AKShare 东方财富全球快讯 + bid_notices 关键词筛选 + 政府部委官网政策（config/policy_sources.yaml）
- **十五五** = 第十五个五年规划（2026-2030），当前处于规划编制/政策密集发布阶段
- 行情：AKShare 板块/指数概览（`GET /api/stock/market`）；网络失败时降级为 bid_notices/mock
- 政策：国务院/发改委/工信部/科技部/农业农村部/生态环境部/国资委/商务部；抓取失败时降级 mock 演示条目
- **免责声明**：所有输出须注明「政策解读仅供参考，不构成投资建议」，禁止编造股价/涨跌幅/具体买卖建议

## 股票专题能力
- `crawl_stock_insights` — 聚合近 N 天股票领域洞察，返回 8092 洞察页 view_url
- `crawl_stock_analysis` — 结合新闻+公告生成产业发展分析（趋势/驱动/风险/产业链）；参数 industry 或 sector
- `crawl_stock_investment_plan` — 生成 6-24 月中长线投资思路框架（分阶段、主题配置、非买卖建议）
- `crawl_policy_insights` — 聚合政府政策资讯，对齐十五五主题；可选 sync_first 先抓取
- `crawl_investment_direction` — 结合十五五背景生成整体投资方向报告（产业/主题/政策信号/时间维度/风险）
- `stock_query_market` — 查询 A 股大盘/指数概览（AKShare）
- `stock_query_sectors` — 板块涨跌排行（industry/concept/sw_l1 等，可 sort_by change_pct）
- `stock_query_company` — 单股公司资料（code 必填）
- `stock_query_industry_chain` — 板块产业链解读（symbol 或 code）
- 配置见 `config/stock_watch.yaml`、`config/policy_sources.yaml`；`.env` 设 `STOCK_DATA_PROVIDER=akshare`、`POLICY_NEWS_SOURCES`

## 工作方式
1. 用户要「大盘/指数/今日涨跌」→ `stock_query_market`
2. 用户要「板块热点/申万一级/概念涨幅」→ `stock_query_sectors`（classify=sw_l1 或 concept）
3. 用户要「某股资料/财报/主营」→ `stock_query_company`
4. 用户要「产业链/上下游」→ `stock_query_industry_chain`
5. 用户要「十五五/政策/投资方向/政府最新政策」→ `crawl_policy_insights` 或 `crawl_investment_direction`
6. 用户要「产业发展分析/某产业链分析」→ `crawl_stock_analysis`（指定 industry/sector，如 新能源）
7. 用户要「12 个月投资思路/中长线计划」→ `crawl_stock_investment_plan`
8. 用户要「看股票洞察/统计/主题分布」→ `crawl_stock_insights`
9. 回复时用 Markdown 表格展示排行数据；可选 ```echarts JSON``` 代码块绘制简单柱状/折线图
10. 说明当前数据来源与降级状态（AKShare/政府网站失败 → bid_notices/mock）
11. 完成后给出 8092 专题页链接（`view_url`）

## 禁止
- 编造未在数据中出现的股价、涨跌幅
- 给出具体个股买卖建议或目标价
- 声称已接入行情 API 而实际仅为新闻/关键词筛选

## 输出要求
- 中文简要说明；汇总匹配条数、产业/政策热度、数据来源说明
- 投资/政策相关内容必须附带免责声明
- 给出 8092 专题页链接（`view_url`）
"""


def resolve_system_prompt(agent_profile: str = "default") -> str:
    profile = (agent_profile or "").strip().lower()
    if profile == "agri":
        return AGRI_SYSTEM_PROMPT
    if profile == "stock":
        return STOCK_SYSTEM_PROMPT
    return SYSTEM_PROMPT


@dataclass
class ChatStreamEvent:
    """SSE 事件 payload。"""

    type: str
    content: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    role: str = ""

    def to_sse_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.content:
            out["content"] = self.content
        if self.name:
            out["name"] = self.name
        if self.args:
            out["args"] = self.args
        if self.summary:
            out["summary"] = self.summary
        if self.data:
            out["data"] = self.data
        if self.role:
            out["role"] = self.role
        return out


def _url_crawl_event(data: dict[str, Any]) -> ChatStreamEvent:
    """构造 url_crawl SSE 事件。"""
    status = str(data.get("status") or "fetching")
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    if title:
        summary = f"{title[:40]} ({status})"
    elif url:
        summary = f"{url[:60]} ({status})"
    else:
        summary = status
    return ChatStreamEvent(type="url_crawl", summary=summary, data=dict(data))


def _format_path_tree(path_tree: dict[str, Any]) -> str:
    entry = path_tree.get("entry") or "?"
    lines = [f"📍 入口: {entry}"]
    for page in path_tree.get("pages") or []:
        num = page.get("page_num") or "?"
        label = page.get("label") or f"第 {num} 页"
        lines.append(f"  └─ {label} → items[] → detail? → save")
    return "\n".join(lines)


def _collect_tool_stats(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """从对话 messages 中的 tool 结果汇总爬取进度。"""
    stats: dict[str, Any] = {
        "site_id": None,
        "pages_fetched": set(),
        "saved": 0,
        "skipped": 0,
        "agent_max_pages": None,
        "pagination_strategy": None,
        "query_total": None,
    }
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        raw = msg.get("content") or ""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("site_id"):
            stats["site_id"] = data["site_id"]
        page_num = data.get("page_num")
        if page_num is not None and data.get("ok"):
            stats["pages_fetched"].add(int(page_num))
        if data.get("saved"):
            stats["saved"] += 1
        elif data.get("duplicate"):
            stats["skipped"] += 1
        if data.get("agent_max_pages") is not None:
            stats["agent_max_pages"] = data["agent_max_pages"]
        if data.get("pagination_strategy"):
            stats["pagination_strategy"] = data["pagination_strategy"]
        if data.get("total") is not None and "items" in data:
            stats["query_total"] = data["total"]
    return stats


def _build_partial_summary(
    messages: list[dict[str, Any]],
    *,
    turn: int,
    max_turns: int,
    is_full_crawl: bool = False,
) -> str:
    stats = _collect_tool_stats(messages)
    if is_full_crawl:
        heading = f"## 全量爬取摘要（全量任务 {max_turns} 轮推理上限，已达 {turn}/{max_turns}）"
    else:
        heading = f"## 部分爬取摘要（已达 {turn}/{max_turns} 轮推理上限）"
    lines = [
        heading,
        "",
    ]
    if stats["site_id"]:
        lines.append(f"- **站点**: `{stats['site_id']}`")
    if stats["pagination_strategy"]:
        lines.append(f"- **分页策略**: {stats['pagination_strategy']}")
    if stats["agent_max_pages"] is not None:
        lines.append(f"- **计划页数**: {stats['agent_max_pages']}")
    pages = sorted(stats["pages_fetched"])
    if pages:
        if len(pages) == 1:
            lines.append(f"- **已抓列表页**: 第 {pages[0]} 页")
        else:
            lines.append(f"- **已抓列表页**: {len(pages)} 页（第 {pages[0]}–{pages[-1]} 页）")
    lines.append(f"- **本次保存**: {stats['saved']} 条（跳过重复 {stats['skipped']}）")
    if stats["query_total"] is not None:
        lines.append(f"- **库内总计**: {stats['query_total']} 条")
    lines.extend(
        [
            "",
            "推理轮次已用尽，上方工具结果仍有效。",
            "请直接发送 **「继续」** 或说明从第几页接着爬；**同一会话**内上下文已保留。",
        ]
    )
    return "\n".join(lines)


def _cancelled_stream_events() -> list[ChatStreamEvent]:
    return [
        ChatStreamEvent(type="cancelled", content="用户已中止爬取"),
        ChatStreamEvent(type="done"),
    ]


async def _yield_if_cancelled(
    cancel_event: Optional[threading.Event],
    chat_session_id: Optional[str],
) -> Optional[list[ChatStreamEvent]]:
    if (cancel_event and cancel_event.is_set()) or is_chat_cancelled(chat_session_id):
        return _cancelled_stream_events()
    return None


def _extract_tool_arguments(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "args", "input", "parameters"):
        raw = event.get(key)
        if isinstance(raw, dict):
            return raw
    return {}


def _extract_tool_result_payload(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    for key in ("result", "output", "data"):
        raw = event.get(key)
        if isinstance(raw, dict):
            if key == "data" and "result" in raw and isinstance(raw["result"], dict):
                return raw["result"]
            if key in ("result", "output") or raw.get("ok") is not None or raw.get("executed_command"):
                return raw
    return None


def _tool_completed_is_error(event: dict[str, Any], result_payload: Optional[dict[str, Any]]) -> bool:
    """Hermes crawl toolset 对 skill_* 成功响应有时误标 error=true，按 payload 校正。"""
    is_error = bool(event.get("error"))
    if not is_error:
        return False
    if not isinstance(result_payload, dict):
        return True
    if result_payload.get("ok") is True:
        return False
    nested = result_payload.get("data")
    if isinstance(nested, dict) and nested.get("ok") is True:
        return False
    result = result_payload.get("result")
    if isinstance(result, dict) and result.get("ok") is True:
        return False
    return True


def map_hermes_run_event(event: dict[str, Any]) -> list[ChatStreamEvent]:
    """将 hermes-agent /v1/runs SSE 事件映射为 Web UI ChatStreamEvent。"""
    ev_type = (event.get("event") or "").strip()
    out: list[ChatStreamEvent] = []

    if ev_type == "message.delta":
        delta = event.get("delta") or ""
        if delta:
            out.append(ChatStreamEvent(type="message_delta", content=str(delta)))
    elif ev_type == "reasoning.available":
        text = (event.get("text") or "").strip()
        if text:
            out.append(
                ChatStreamEvent(
                    type="thinking",
                    content=text,
                    data={"reasoning_summary": True},
                )
            )
    elif ev_type == "tool.started":
        tool_name = event.get("tool") or ""
        args = _extract_tool_arguments(event)
        preview = (event.get("preview") or "").strip()
        executed_command = preview
        if args:
            executed_command = format_tool_command(tool_name, args)
        elif not executed_command:
            executed_command = format_tool_command(tool_name, {})
        out.append(
            ChatStreamEvent(
                type="tool",
                name=tool_name,
                args=args,
                content=executed_command,
                data={"executed_command": executed_command},
            )
        )
    elif ev_type == "tool.completed":
        tool_name = event.get("tool") or ""
        duration = event.get("duration")
        args = _extract_tool_arguments(event)
        result_payload = _extract_tool_result_payload(event)
        is_error = _tool_completed_is_error(event, result_payload)
        if bool(event.get("error")) and result_payload is None:
            # Hermes SSE 常不带 result body，crawl toolset 对 skill_* 成功响应也会误标 error
            is_error = False
        executed_command = ""
        if isinstance(result_payload, dict):
            executed_command = str(result_payload.get("executed_command") or "").strip()
            if not args:
                raw_args = result_payload.get("arguments")
                if isinstance(raw_args, dict):
                    args = raw_args
        if not executed_command:
            executed_command = build_executed_command(tool_name, args, result_payload or {})
        if is_error:
            summary = f"失败: {tool_name}"
        else:
            summary = f"完成: {tool_name}"
        if duration is not None:
            summary = f"{summary} ({duration}s)"
        result_data: dict[str, Any] = {
            "error": is_error,
            "duration": duration,
            "executed_command": executed_command,
        }
        if args:
            result_data["arguments"] = args
        if isinstance(result_payload, dict):
            result_data["result"] = result_payload
        out.append(
            ChatStreamEvent(
                type="result",
                name=tool_name,
                args=args,
                summary=summary,
                data=result_data,
            )
        )
    elif ev_type == "run.completed":
        output = (event.get("output") or "").strip()
        message_data: dict[str, Any] = {}
        max_iterations = resolve_hermes_max_iterations()
        tool_turns = int(event.get("_tool_turns") or 0)
        if detect_max_iterations_reached(
            event,
            tool_turns=tool_turns,
            max_iterations=max_iterations,
        ):
            message_data = {
                "can_continue": True,
                "turns_used": tool_turns or max_iterations,
                "max_turns": max_iterations,
                "reason": "max_iterations_reached",
            }
        if output or message_data.get("can_continue"):
            out.append(
                ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=output or "推理轮次已用尽，请发送「继续」接着爬取。",
                    data=message_data,
                )
            )
    elif ev_type == "run.failed":
        out.append(
            ChatStreamEvent(
                type="error",
                content=event.get("error") or "Hermes Agent 运行失败",
            )
        )
    elif ev_type == "run.cancelled":
        out.append(ChatStreamEvent(type="cancelled", content="用户已中止爬取"))
    return out


class CrawlAgentChatService:
    def __init__(
        self,
        *,
        hermes_client: Optional[HermesAgentChatClient] = None,
        max_turns: int = MAX_AGENT_TURNS,
    ) -> None:
        self._hermes = hermes_client or get_hermes_agent_chat_client()
        self.max_turns = max_turns

    async def stream_chat(
        self,
        user_message: str,
        history: Optional[list[dict[str, str]]] = None,
        *,
        session_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        agent_profile: str = "default",
    ) -> AsyncIterator[ChatStreamEvent]:
        """处理单轮用户消息，委托 hermes-agent /v1/runs，yield SSE 事件。"""
        history = truncate_chat_history(history or [])
        chat_session_id = session_id
        system_prompt = resolve_system_prompt(agent_profile)

        if agent_profile == "default" and detect_onboarding_intent(user_message):
            yield ChatStreamEvent(
                type="thinking",
                content="检测到站点接入意图，展示注册表单…",
                data={"onboarding_intent": True},
            )
            yield build_site_register_ui_event()
            yield ChatStreamEvent(
                type="message",
                role="assistant",
                content=(
                    "请填写站点名称与入口 URL 完成注册。"
                    "注册成功后说「探查该站点」或「帮我爬取这个网站」，"
                    "Agent 将默认走 WebBridge 探查 → 生成 crawl_rules → 试跑 → 启用。"
                ),
            )
            yield ChatStreamEvent(type="done")
            return

        if agent_profile == "default" and detect_scheduled_tasks_panel_ui_intent(user_message):
            yield ChatStreamEvent(
                type="thinking",
                content="检测到通知任务管理意图，展示任务列表…",
                data={"scheduled_tasks_panel_intent": True},
            )
            yield build_scheduled_tasks_panel_ui_event()
            yield ChatStreamEvent(
                type="message",
                role="assistant",
                content=(
                    "下方为当前已配置的通知任务。"
                    "关闭开关可暂停调度（run_scheduler 约 60 秒内生效）；"
                    "也可说「创建 BIM 每日分析通知任务」添加新任务。"
                ),
            )
            yield ChatStreamEvent(type="done")
            return

        if agent_profile == "default" and detect_bim_analysis_schedule_ui_intent(user_message):
            yield ChatStreamEvent(
                type="thinking",
                content="检测到 BIM 分析通知任务配置意图，展示表单…",
                data={"bim_analysis_schedule_ui_intent": True},
            )
            yield build_bim_analysis_schedule_ui_event()
            yield ChatStreamEvent(
                type="message",
                role="assistant",
                content=(
                    "请在下方表单配置任务名称、执行时间、飞书 Webhook 等信息。"
                    "Webhook 留空时将使用全局 `FEISHU_WEBHOOK_URL`。"
                ),
            )
            yield ChatStreamEvent(type="done")
            return

        if agent_profile == "default" and detect_scheduled_bim_analysis_feishu_intent(user_message):
            from src.web.chat_schedule_service import get_chat_schedule_service

            yield ChatStreamEvent(
                type="thinking",
                content="检测到「BIM 每日综合分析 + 飞书」通知任务意图，正在解析…",
                data={"scheduled_bim_analysis_intent": True},
            )
            result = get_chat_schedule_service().create_bim_analysis_from_intent(user_message)
            if result.get("ok"):
                task = result.get("task") or {}
                webhook_note = (
                    "已绑定任务级 Webhook"
                    if task.get("feishu_webhook_url")
                    else "飞书 fallback 全局 `FEISHU_WEBHOOK_URL`"
                )
                yield ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=(
                        f"已创建 BIM 分析通知任务：**{task.get('name')}**\n\n"
                        f"- 类型：`bim_daily_analysis`\n"
                        f"- Cron：`{task.get('cron')}`\n"
                        f"- Top 标讯：{task.get('top_n', 10)} 条\n"
                        f"- 下次执行：{task.get('next_run_time') or '—'}\n"
                        f"- 任务 ID：`{task.get('id')}`\n"
                        f"- 飞书：{webhook_note}\n\n"
                        "到点将分析今日新增 BIM 标讯并 LLM 综合分析后推送；run_scheduler 约 60 秒内自动加载。"
                    ),
                )
            else:
                yield ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=(
                        f"未能自动创建 BIM 分析通知任务：{result.get('error')}\n\n"
                        "请补充执行时间与 Webhook，或说「创建 BIM 每日分析通知任务」打开配置表单。"
                    ),
                )
            yield ChatStreamEvent(type="done")
            return

        if agent_profile == "default" and detect_scheduled_feishu_intent(user_message):
            from src.web.chat_schedule_service import get_chat_schedule_service

            yield ChatStreamEvent(
                type="thinking",
                content="检测到「增量爬取 + 飞书报告」通知任务意图，正在解析…",
                data={"scheduled_feishu_intent": True},
            )
            result = get_chat_schedule_service().create_from_intent(user_message)
            if result.get("ok"):
                task = result.get("task") or {}
                webhook_note = (
                    "已绑定任务级 Webhook"
                    if task.get("feishu_webhook_url")
                    else "飞书 fallback 全局 `FEISHU_WEBHOOK_URL`"
                )
                yield ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=(
                        f"已创建通知任务：**{task.get('name')}**\n\n"
                        f"- 类型：`{task.get('task_type', 'incremental_sync')}`\n"
                        f"- 站点：`{task.get('site_id')}`\n"
                        f"- Cron：`{task.get('cron')}`\n"
                        f"- 报告类型：{task.get('report_type')}\n"
                        f"- 下次执行：{task.get('next_run_time') or '—'}\n"
                        f"- 任务 ID：`{task.get('id')}`\n"
                        f"- 飞书：{webhook_note}\n\n"
                        "run_scheduler 约 60 秒内自动加载。"
                    ),
                )
            else:
                yield ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=(
                        f"未能自动创建通知任务：{result.get('error')}\n\n"
                        "请补充站点名称与执行时间，例如："
                        "「每天上午8点爬取电建BIM增量，完成后发飞书报告」。"
                    ),
                )
            yield ChatStreamEvent(type="done")
            return

        if agent_profile == "default" and detect_scheduled_hermes_feishu_intent(user_message):
            from src.web.chat_schedule_service import get_chat_schedule_service

            yield ChatStreamEvent(
                type="thinking",
                content="检测到「Hermes 任务 + 飞书摘要」通知任务意图，正在解析…",
                data={"scheduled_hermes_feishu_intent": True},
            )
            result = get_chat_schedule_service().create_hermes_from_intent(user_message)
            if result.get("ok"):
                task = result.get("task") or {}
                webhook_note = (
                    "已绑定任务级 Webhook"
                    if task.get("feishu_webhook_url")
                    else "飞书 fallback 全局 `FEISHU_WEBHOOK_URL`"
                )
                yield ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=(
                        f"已创建 Hermes 通知任务：**{task.get('name')}**\n\n"
                        f"- 类型：`hermes_prompt`\n"
                        f"- Cron：`{task.get('cron')}`\n"
                        f"- 指令：{(task.get('hermes_prompt') or '')[:120]}"
                        f"{'…' if len(task.get('hermes_prompt') or '') > 120 else ''}\n"
                        f"- 下次执行：{task.get('next_run_time') or '—'}\n"
                        f"- 任务 ID：`{task.get('id')}`\n"
                        f"- 飞书：{webhook_note}\n\n"
                        "到点将调用 Hermes Agent 执行并推送摘要；run_scheduler 约 60 秒内自动加载。"
                    ),
                )
            else:
                yield ChatStreamEvent(
                    type="message",
                    role="assistant",
                    content=(
                        f"未能自动创建 Hermes 通知任务：{result.get('error')}\n\n"
                        "请说明执行时间与任务内容，例如："
                        "「每天上午9点执行 Hermes 任务：统计今日 BIM 公告数量，完成后推飞书」。"
                    ),
                )
            yield ChatStreamEvent(type="done")
            return

        if not self._hermes.available:
            async for ev in self._hermes_unavailable():
                yield ev
            return

        # 只传 user 轮次：assistant 历史常含「好的我来操作…」等纯文字，会教 LLM 不调工具。
        conversation_history: list[dict[str, str]] = []
        for msg in history:
            role = msg.get("role", "user")
            content = (msg.get("content") or "").strip()
            if role == "user" and content:
                conversation_history.append({"role": role, "content": content})
        if len(conversation_history) > 6:
            conversation_history = conversation_history[-6:]

        settings = get_settings()
        _, is_full_crawl = resolve_max_turns_for_message(
            user_message,
            default_max=self.max_turns,
            full_max=settings.crawl_agent_max_rounds_full,
        )
        chat_url = resolve_hermes_agent_chat_url(settings)
        if is_full_crawl:
            yield ChatStreamEvent(
                type="thinking",
                content=(
                    f"检测到全量爬取意图，已委托 Hermes Agent（{chat_url}）执行；"
                    f"推理轮次由 hermes-agent HERMES_MAX_ITERATIONS 控制"
                ),
                data={"is_full_crawl": True, "hermes_agent_url": chat_url},
            )
        else:
            yield ChatStreamEvent(
                type="thinking",
                content=f"已连接 Hermes Agent（{chat_url}），开始 Agent turn…",
                data={"is_full_crawl": False, "hermes_agent_url": chat_url},
            )

        cancelled = await _yield_if_cancelled(cancel_event, chat_session_id)
        if cancelled:
            for ev in cancelled:
                yield ev
            return

        run_id, err = await self._hermes.start_run(
            user_message=user_message,
            conversation_history=conversation_history,
            session_id=chat_session_id,
            instructions=system_prompt,
        )
        if err:
            yield ChatStreamEvent(type="error", content=err)
            yield ChatStreamEvent(type="done")
            return

        yield ChatStreamEvent(
            type="thinking",
            content=f"Hermes run 已启动（run_id={run_id}）",
            data={"run_id": run_id},
        )

        terminal_types = {"run.completed", "run.failed", "run.cancelled"}
        saw_terminal = False
        tool_turns = 0

        try:
            async for raw in self._hermes.stream_run_events(
                run_id,
                cancel_event=cancel_event,
                is_cancelled=lambda: is_chat_cancelled(chat_session_id),
            ):
                cancelled = await _yield_if_cancelled(cancel_event, chat_session_id)
                if cancelled:
                    for ev in cancelled:
                        yield ev
                    return

                ev_type = raw.get("event") or ""
                if ev_type == "tool.completed":
                    tool_turns += 1
                if ev_type == "run.completed":
                    raw = {**raw, "_tool_turns": tool_turns}
                for chat_ev in map_hermes_run_event(raw):
                    yield chat_ev
                if ev_type in terminal_types:
                    saw_terminal = True
                    break
        except httpx.HTTPError as exc:
            yield ChatStreamEvent(type="error", content=f"Hermes Agent SSE 中断: {exc}")
            yield ChatStreamEvent(type="done")
            return

        if not saw_terminal:
            yield ChatStreamEvent(
                type="error",
                content="Hermes Agent 流结束但未收到终态事件，请检查 hermes-agent 日志",
            )

        yield ChatStreamEvent(type="done")

    async def _hermes_unavailable(self) -> AsyncIterator[ChatStreamEvent]:
        """hermes-agent 未配置或不可达时的提示。"""
        settings = get_settings()
        dispatch_url = (settings.hermes_agent_url or "").strip() or "（未设置）"
        chat_url = resolve_hermes_agent_chat_url(settings) or "（未设置）"
        yield ChatStreamEvent(
            type="thinking",
            content="Hermes 对话需外部 hermes-agent API Server，web_scraper 不再使用内置 LLM 循环。",
        )
        yield ChatStreamEvent(
            type="error",
            content=(
                "请配置 hermes-agent 并确保 API Server 可达：\n"
                f"- HERMES_AGENT_URL={dispatch_url}（scheduler crawl dispatch，:8080）\n"
                f"- HERMES_AGENT_CHAT_URL={chat_url}（对话 /v1/runs，默认 :8642）\n"
                "compose 示例：`docker compose up hermes-agent`，健康检查 8642/health。"
            ),
        )
        yield ChatStreamEvent(type="done")


_service: Optional[CrawlAgentChatService] = None


def get_crawl_agent_chat_service() -> CrawlAgentChatService:
    global _service
    if _service is None:
        settings = get_settings()
        _service = CrawlAgentChatService(max_turns=settings.crawl_agent_max_rounds)
    return _service
