"""Hermes Crawl Agent 工具 — 直接调用 web_scraper 服务（无需 HTTP 自环）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Optional

from src.core.crawl_agent_actions import append_action
from src.core.rule_generator import get_rule_generator, load_rule_yaml, save_rule_yaml, validate_yaml
from src.core.site_aliases import resolve_canonical_site_id, site_not_registered_error
from src.core.site_sync import get_site_by_id
from src.core.crawl_run_store import get_crawl_run_store
from src.web.crawl_url_progress import UrlCrawlTracker
from src.web.crawl_agent_skills import (
    close_skill_session as _close_skill_session_async,
    fetch_detail_page_async,
    fetch_list_page_async,
    fetch_notice_content_async,
    http_fetch_list_page_async,
    load_rules,
    paginate_async,
    plan_crawl_path,
    run_async,
    save_notice,
)
from src.web.notice_extract_skills import extract_notice_fields
from src.web.tagged_document_skills import save_tagged_document, search_tagged_documents
from src.web.webbridge_skills import (
    check_webbridge,
    webbridge_click,
    webbridge_close,
    webbridge_evaluate,
    webbridge_extract_list,
    webbridge_fill,
    webbridge_get_html,
    webbridge_go,
    webbridge_navigate,
    webbridge_scroll,
    webbridge_screenshot,
    webbridge_select,
    webbridge_snapshot,
    webbridge_wait,
)
from src.web.data_service import get_data_service
from src.web.schedule_service import get_schedule_service
from src.web.script_cron_service import get_script_cron_service
from src.web.sync_service import get_sync_service
from src.web.task_service import get_task_service

logger = logging.getLogger(__name__)

# 用户口语 → site_id 别名（MVP）
SITE_ALIASES: dict[str, str] = {
    "dlzb_power": "dlzb_power",
    "电力招标网": "dlzb_power",
    "www.dlzb.com": "dlzb_power",
    "www.dlzb.com/search": "dlzb_power",
    "zhfdc_dlzb": "crec_bidding",
    "zhfdc.dlzb": "crec_bidding",
    "zhfdc": "crec_bidding",
    "鲁班商务网": "crec_bidding",
    "鲁班": "crec_bidding",
    "crec": "crec_bidding",
    "tjbid": "中国铁道建筑集团有限公司_物资采购网",
    "铁建物资": "中国铁道建筑集团有限公司_物资采购网",
    "铁建物资采购": "中国铁道建筑集团有限公司_物资采购网",
    "物资采购网": "中国铁道建筑集团有限公司_物资采购网",
    "dlzb": "中国铁道建筑集团有限公司_物资采购网",
    "powerchina": "中国电力建设集团有限公司_公共资源交易服务平台",
    "电建": "中国电力建设集团有限公司_公共资源交易服务平台",
    "bid.powerchina": "中国电力建设集团有限公司_公共资源交易服务平台",
    "公共资源交易服务平台": "中国电力建设集团有限公司_公共资源交易服务平台",
    "zgjtjs": "中国交通建设集团有限公司_供应链管理信息系统",
    "中交": "中国交通建设集团有限公司_供应链管理信息系统",
    "gov_cg": "gov_cg_national",
    "gov-cg": "gov_cg_national",
    "政采网": "gov_cg_national",
    "中国政府采购招标网": "gov_cg_national",
}

SKILL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "skill_plan_crawl_path",
            "description": (
                "读取已有 crawl_rules 与用户意图，输出结构化爬取计划。"
                "新站或无有效 list_page 时不要调用，改走 WebBridge 探查 + crawl_generate_rule。"
                "recommended_list_tool 为 WebBridge 首选，fallback_list_tool 为回退。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "user_intent": {"type": "string", "description": "用户原始请求，用于解析页数"},
                    "max_pages": {"type": "integer", "description": "可选，覆盖默认批次页数"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_load_rules",
            "description": "加载站点 crawl_rules YAML 摘要（selectors、分页、limits）。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_analyze_list_dom",
            "description": (
                "分析列表页 HTML，推断 container/item/title/link 选择器，"
                "输出 page_hints 供 crawl_generate_rule 使用。"
                "可传 html 或 session_id（经 WebBridge get_html 获取）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "页面 HTML 片段"},
                    "url": {"type": "string", "description": "当前页 URL，用于补全相对链接"},
                    "session_id": {"type": "string", "description": "WebBridge 会话，自动拉取 HTML"},
                    "notes": {"type": "string", "description": "探查备注，合并进 page_hints"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_http_fetch_list",
            "description": "纯 HTTP POST/GET 列表 API（零 browser）。list_page.strategy=api 时优先于 skill_fetch_list_page。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "page_num": {"type": "integer", "description": "默认 1"},
                    "session_id": {"type": "string"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_fetch_list_page",
            "description": "抓取单页列表，返回 items[{title,url,date}] 与 has_next。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "page_num": {"type": "integer", "description": "默认 1"},
                    "list_url": {"type": "string", "description": "可选，指定列表 URL"},
                    "session_id": {"type": "string"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_paginate",
            "description": "按规则翻页或计算下一页 URL（不触发整站 sync）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "current_url": {"type": "string"},
                    "page_num": {"type": "integer"},
                    "session_id": {"type": "string"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_fetch_detail_page",
            "description": "抓取单条详情 URL，返回 title/content/attachments。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["site_id", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_fetch_notice_content",
            "description": (
                "电建 bid.powerchina.cn 公告正文：getInfo HTML 为空时自动下载 pictureUrl PDF 并提取文本。"
                "支持 notice_id、detail_url 或 pdf_url；返回 content_source(html|pdf|empty)、"
                "content_text、picture_url、system_id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "notice_id": {"type": "string", "description": "公告数字 ID"},
                    "detail_url": {
                        "type": "string",
                        "description": "notice/detail?id= 详情 URL，可替代 notice_id",
                    },
                    "pdf_url": {
                        "type": "string",
                        "description": "直接指定 bulletinDownLoadFile PDF URL",
                    },
                    "expected_title": {
                        "type": "string",
                        "description": "可选，校验 getInfo 标题是否匹配",
                    },
                    "verify_title": {
                        "type": "boolean",
                        "description": "是否校验标题，默认 true",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_save_notice",
            "description": "单条公告写入 pipeline/Mongo（自动去重）。site_id 须在 config/sites.yaml 注册（非 MongoDB sites 集合）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "publish_date": {"type": "string"},
                    "content_text": {"type": "string"},
                    "content_html": {"type": "string"},
                    "attachments": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["site_id", "title", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_extract_notice_fields",
            "description": (
                "从公告正文/摘要抽取结构化字段：合同金额、招标方、项目周期、招标关键内容。"
                "支持传入 doc、site_id+notice_id、url 或 url+content_text；"
                "persist=true 时写回 MongoDB。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc": {
                        "type": "object",
                        "description": "完整公告文档 dict（含 title/content_text/key_summary 等）",
                    },
                    "site_id": {"type": "string", "description": "站点 ID（可选）"},
                    "notice_id": {
                        "type": "string",
                        "description": "公告 ID（电建为 notice/detail?id= 数字）",
                    },
                    "url": {"type": "string", "description": "公告详情 URL"},
                    "content_text": {"type": "string", "description": "正文纯文本"},
                    "title": {"type": "string", "description": "公告标题"},
                    "persist": {
                        "type": "boolean",
                        "description": "是否将抽取结果写回 MongoDB，默认 false",
                    },
                    "use_llm": {
                        "type": "boolean",
                        "description": "是否启用 LLM 补全（默认跟随配置 key_info_llm）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_save_tagged_document",
            "description": (
                "将爬取内容写入 MongoDB tagged_documents 集合并打标签（url 或 content_hash 去重）。"
                "适用于非标准公告、自定义分类或 Agent 提取的结构化片段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文档标题"},
                    "content": {"type": "string", "description": "正文纯文本"},
                    "url": {"type": "string", "description": "来源 URL（可选，用于去重）"},
                    "site_id": {"type": "string", "description": "关联站点 ID（可选）"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表，如 [\"BIM\", \"招标\"]",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "附加元数据（可选 dict）",
                    },
                    "source": {
                        "type": "string",
                        "description": "写入来源标识，如 webbridge、agent",
                    },
                },
                "required": ["title", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_search_by_tags",
            "description": (
                "从 MongoDB tagged_documents 按标签检索文档摘要。"
                "match_mode=any 匹配任一标签，all 须同时包含全部标签。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要匹配的标签",
                    },
                    "match_mode": {
                        "type": "string",
                        "enum": ["any", "all"],
                        "description": "默认 any",
                    },
                    "site_id": {"type": "string", "description": "可选，限定站点"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 200"},
                },
                "required": ["tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_close_session",
            "description": "关闭 skill 浏览器会话，释放 BrowserPool 资源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "session_id": {"type": "string", "description": "可选，指定 session"},
                },
                "required": ["site_id"],
            },
        },
    },
]

WEBBRIDGE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_check",
            "description": (
                "新爬虫/新站接入第一步必调：检测 Kimi WebBridge daemon 是否可用"
                "（需本机安装 + 浏览器扩展连接）。"
                "DOM/API 失败、需登录或反爬时先调用此工具。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_navigate",
            "description": (
                "通过 WebBridge 在用户真实浏览器中打开 URL，返回 accessibility tree 摘要。"
                "首次打开请 new_tab=true；可复用 session_id 保持标签组隔离。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "session_id": {"type": "string", "description": "WebBridge session 名，默认 crawl-agent"},
                    "new_tab": {"type": "boolean", "description": "默认 true，首次导航建议 true"},
                    "group_title": {"type": "string", "description": "浏览器标签组标题"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_extract_list",
            "description": (
                "从当前 WebBridge 页面提取列表项 {title,url}。"
                "提供 CSS selector（如 .list a）或 hint 关键词过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS 选择器，匹配列表项或链接"},
                    "hint": {"type": "string", "description": "AI 提示关键词，用于过滤链接标题/URL"},
                    "session_id": {"type": "string"},
                    "max_items": {"type": "integer", "description": "默认 50，最大 200"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_get_html",
            "description": "获取当前 WebBridge 页面 HTML（截断），供 crawl_rules 选择器调试。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "默认 50000"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_close",
            "description": "关闭 WebBridge 当前 tab 或整个 session（任务结束时应调用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "close_session": {
                        "type": "boolean",
                        "description": "true 时关闭 session 内全部 tab",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_snapshot",
            "description": "获取当前 WebBridge 页面 URL、标题与 accessibility tree 摘要（不导航）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_click",
            "description": "在 WebBridge 页面点击元素（@e ref 或 CSS selector）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "@e ref 或 CSS"},
                    "session_id": {"type": "string"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_fill",
            "description": "在 WebBridge 页面填写输入框或可编辑区域（clear-and-insert）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_screenshot",
            "description": "WebBridge 页面截图，保存到服务器并返回 image_path / image_url（非 base64）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["png", "jpeg"]},
                    "quality": {"type": "integer", "description": "jpeg 质量 1-100"},
                    "selector": {"type": "string", "description": "可选，仅截取元素"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_evaluate",
            "description": "在 WebBridge 当前页面执行 JavaScript（支持 async/await）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "session_id": {"type": "string"},
                    "max_result_chars": {"type": "integer", "description": "默认 8000"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_scroll",
            "description": "滚动 WebBridge 当前页面（默认向下 500px）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "x": {"type": "integer", "description": "水平滚动像素，默认 0"},
                    "y": {"type": "integer", "description": "垂直滚动像素，默认 500"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_wait",
            "description": "等待指定秒数（最多 30s），用于页面加载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number"},
                    "session_id": {"type": "string"},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_go",
            "description": "WebBridge 浏览器历史后退或前进，并返回新页面 snapshot。",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["back", "forward"]},
                    "session_id": {"type": "string"},
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_select",
            "description": "设置 WebBridge 页面 <select> 下拉选项（按 value 或可见文本）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_webbridge_auto_crawl",
            "description": (
                "WebBridge 一键自动爬取当前页：snapshot → extract_list → 可选翻页 → save_notice。"
                "site_id 须在 sites.yaml 注册；无 crawl_rules 时用通用链接提取（需 confirm_generic）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string", "description": "站点 ID；省略时从当前页 URL 推断"},
                    "session_id": {"type": "string"},
                    "max_pages": {"type": "integer", "description": "最多翻页数，上限 20"},
                    "save": {"type": "boolean", "description": "是否入库，默认 true"},
                    "confirm_generic": {
                        "type": "boolean",
                        "description": "通用 DOM 提取时确认入库",
                    },
                },
                "required": ["site_id"],
            },
        },
    },
]

RULE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_generate_rule",
            "description": "AI 生成 crawl_rules YAML（可选 save 落盘）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "url": {"type": "string"},
                    "page_hints": {
                        "type": "string",
                        "description": "WebBridge/DOM 探查结论，优先于 HTTP 自动抓取",
                    },
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "save": {"type": "boolean", "description": "校验通过后写入 config/crawl_rules"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_generate_workflow",
            "description": (
                "AI 生成爬取工作流（浏览器动作节点 JSON + crawl_rules YAML）。"
                "生成后可在 /crawl-rules/{site_id}/workflow 画布上手动微调。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "url": {"type": "string", "description": "入口 URL"},
                    "page_hints": {
                        "type": "string",
                        "description": "WebBridge/DOM 探查结论",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "用户对列表/分页/选择器的补充说明",
                    },
                    "save": {"type": "boolean", "description": "校验通过后写入 crawl_rules"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_save_rule",
            "description": "保存 crawl_rules YAML 到 config/crawl_rules/{site_id}.yaml。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "yaml": {"type": "string"},
                },
                "required": ["site_id", "yaml"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_validate_rule",
            "description": "Pydantic 校验 crawl_rules YAML。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "yaml": {"type": "string"},
                },
                "required": ["yaml"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_test",
            "description": (
                "试跑 crawl_rules（1 页列表预览，不入库）。"
                "映射 POST /api/crawl-rules/dry-run；可传 yaml 试跑未保存草稿。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "yaml": {"type": "string", "description": "可选，试跑未保存的规则文本"},
                    "max_items": {"type": "integer", "description": "预览条数上限，默认 5"},
                    "max_pages": {"type": "integer", "description": "试跑页数，默认 1"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_enable_site",
            "description": (
                "试跑通过后启用站点：写 sites.yaml enabled=true，"
                "并可选启用 crawl_rules；返回 schedule_eligible 状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "enable_site": {"type": "boolean", "description": "默认 true"},
                    "enable_rule": {"type": "boolean", "description": "默认 true"},
                    "require_schedule_eligible": {
                        "type": "boolean",
                        "description": "默认 true，generic 需有效 list_page",
                    },
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_schedule_status",
            "description": "查询站点 crawl_rules 与 per-site 调度资格（site_eligible_for_schedule）。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_generate_script",
            "description": (
                "分析页面并生成可持久化爬取脚本（YAML 规则为主）。"
                "可选 save 落盘、dry_run 试跑；执行阶段无需 LLM。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "url": {"type": "string", "description": "目标 URL，默认站点 url"},
                    "page_hints": {
                        "type": "string",
                        "description": "WebBridge/DOM 探查结论",
                    },
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                        "description": "补充需求描述",
                    },
                    "save": {"type": "boolean", "description": "校验通过后保存规则与 metadata"},
                    "dry_run": {
                        "type": "boolean",
                        "description": "保存或生成后试跑 1 页（不入库）",
                    },
                    "max_items": {"type": "integer", "description": "试跑条数上限，默认 5"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_run_script",
            "description": "执行已生成的爬取脚本/规则，全程不调用 LLM。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "dry_run": {"type": "boolean", "description": "试跑不入库"},
                    "max_items": {"type": "integer", "description": "最多抓取条数"},
                    "persist": {
                        "type": "boolean",
                        "description": "是否入库 Mongo，默认 true；dry_run 时忽略",
                    },
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_get_script_status",
            "description": "读取已生成爬取脚本的 metadata 与规则摘要。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
]

HITL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_request_user_input",
            "description": "创建 HITL 待办（Cookie/验证码/规则确认），写入 pending 队列。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "input_type": {
                        "type": "string",
                        "enum": ["login_cookie", "captcha", "rule_confirm", "generic"],
                    },
                    "prompt": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "object"},
                },
                "required": ["site_id", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_poll_pending",
            "description": "单次读取 pending 队列（不阻塞）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "resolved", "all"],
                        "description": "默认 pending",
                    },
                    "site_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_wait_pending",
            "description": "轮询 pending 直至 resolved 或超时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pending_id": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "description": "默认 120"},
                    "poll_interval": {"type": "number", "description": "默认 2.0"},
                },
                "required": ["pending_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_notify_user",
            "description": "通知运维（MVP：日志 + stdout；可扩展飞书/桌面）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error"],
                        "description": "默认 info",
                    },
                },
                "required": ["title", "body"],
            },
        },
    },
]

INDUSTRY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "skill_supply_chain_graph",
            "description": (
                "查询行业供应链 Neo4j 子图或采购链表格。"
                "可传 center（企业 UUID）或 industry_code 展开关系。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "center": {"type": "string", "description": "中心企业 company_id (UUID)"},
                    "industry_code": {"type": "string", "description": "行业 code，如 A01"},
                    "depth": {"type": "integer", "description": "子图深度 1-4，默认 2"},
                    "domain": {"type": "string", "description": "业务域，默认数字农业"},
                    "days": {"type": "integer", "description": "采购链表格天数窗口"},
                    "mode": {
                        "type": "string",
                        "enum": ["graph", "table"],
                        "description": "graph=Neo4j 子图；table=采购链表格",
                    },
                },
            },
        },
    },
]

DIAGNOSTIC_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_list_sites",
            "description": "列出已启用站点及同步状态、最近 run。用于查找 site_id 或巡检失败站。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "description": "可选：failed, syncing, completed, pending",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_get_task_status",
            "description": "获取单站任务详情；可选拉取实时爬取进度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "include_live_progress": {"type": "boolean"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_get_rule",
            "description": "读取站点 L2 crawl_rules YAML 及校验结果。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_query_notices",
            "description": "查询已爬取公告列表；agri_only=true 仅返回数字农业相关。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "keyword": {"type": "string"},
                    "agri_only": {"type": "boolean", "description": "仅数字农业相关公告"},
                    "bim_only": {"type": "boolean", "description": "已废弃，等同 agri_only"},
                    "page": {"type": "integer"},
                    "per_page": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_classify_agri",
            "description": "对已有公告补做数字农业分类（关键词 + 可选 LLM），写入 is_agri_related 等字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string", "description": "可选，限定站点"},
                    "limit": {"type": "integer", "description": "最多处理条数，默认 100"},
                    "force": {"type": "boolean", "description": "强制重新分类"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_agri_insights",
            "description": "聚合近 N 天商机洞察，返回主站 /insights 页 view_url。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "统计窗口天数，默认 7"},
                    "site_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_agri_sync_all",
            "description": "触发 7 站农业关键词同步（等同 daily_agri_sync）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "max_items": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_agri_tenders",
            "description": "招标商机列表（按 opportunity_score 排序）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "min_score": {"type": "integer"},
                    "site_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_stock_insights",
            "description": "聚合近 N 天股票领域洞察，返回 8092 洞察页 view_url。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer"},
                    "use_llm": {"type": "boolean"},
                    "refresh_llm": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_stock_analysis",
            "description": "结合新闻+公告生成产业发展分析。",
            "parameters": {
                "type": "object",
                "properties": {
                    "industry": {"type": "string"},
                    "sector": {"type": "string"},
                    "days": {"type": "integer"},
                    "use_llm": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_stock_investment_plan",
            "description": "生成 6-24 月中长线投资思路框架（非买卖建议）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "industries": {"type": "array", "items": {"type": "string"}},
                    "risk_profile": {"type": "string"},
                    "horizon_months": {"type": "integer"},
                    "days": {"type": "integer"},
                    "use_llm": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_resolve_site",
            "description": "从用户描述（如 tjbid、铁建物资）解析 site_id。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]

EXECUTE_CODE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_execute_code",
            "description": (
                "执行 Python 代码并返回 stdout/stderr/exit code。"
                "mode=script：运行 scripts/ 白名单脚本（可传 args）；"
                "mode=code：运行 Agent 生成的临时代码（写入 data/generated_scripts/）。"
                "subprocess 执行，禁止 shell；默认超时 120s。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["script", "code"],
                        "description": "script=白名单脚本；code=临时代码",
                    },
                    "script": {
                        "type": "string",
                        "description": "mode=script 时 scripts/ 下文件名，如 run_once.py",
                    },
                    "args": {
                        "description": "mode=script 的 CLI 参数：字符串数组或键值对象",
                    },
                    "code": {
                        "type": "string",
                        "description": "mode=code 的 Python 源码",
                    },
                    "label": {
                        "type": "string",
                        "description": "mode=code 时可选，用于生成脚本文件名前缀",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数，默认 120，最大 7200",
                    },
                },
                "required": ["mode"],
            },
        },
    },
]

SCRIPT_CRON_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_schedule_script",
            "description": (
                "管理 scripts/ 批量入库 Cron 任务（创建/更新/列出/删除/立即执行）。"
                "仅白名单脚本；cron 支持 5 段表达式或「每天 2 点」等口语。"
                "任务由 run_scheduler.py 的 APScheduler 在指定时间 subprocess 执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "delete", "list", "get", "run"],
                        "description": "create/update/delete/list/get/run",
                    },
                    "job_id": {"type": "string", "description": "任务 ID；update/delete/get/run 必填"},
                    "script": {
                        "type": "string",
                        "description": "scripts/ 下脚本名，如 crawl_powerchina_http.py",
                    },
                    "cron": {
                        "type": "string",
                        "description": "cron 表达式（0 2 * * *）或口语（每天 2 点）",
                    },
                    "args": {
                        "description": "CLI 参数：字符串数组或键值对象，如 [\"--max-pages\",\"6\"] 或 {\"max_pages\":6}",
                    },
                    "name": {"type": "string", "description": "任务显示名"},
                    "enabled": {"type": "boolean", "description": "是否启用，默认 true"},
                },
                "required": ["action"],
            },
        },
    },
]

CHAT_SCHEDULE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_schedule_incremental_feishu",
            "description": (
                "管理「增量爬取 + 飞书报告」对话通知任务。"
                "用户说「每天 X 点爬取某站增量并推飞书」时使用。"
                "create_from_intent 可解析整句口语；create 需 site_id + cron。"
                "飞书 Webhook 优先任务级 feishu_webhook_url，否则读 FEISHU_WEBHOOK_URL。"
                "BIM 站可设 report_type=bim。任务持久化于 data/chat_scheduled_tasks.json。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create",
                            "create_from_intent",
                            "update",
                            "delete",
                            "list",
                            "get",
                            "parse",
                        ],
                        "description": "create/create_from_intent/update/delete/list/get/parse",
                    },
                    "task_id": {"type": "string", "description": "任务 ID；update/delete/get 必填"},
                    "user_message": {
                        "type": "string",
                        "description": "用户原话；create_from_intent/parse 时使用",
                    },
                    "site_id": {"type": "string", "description": "站点 ID 或别名"},
                    "cron": {
                        "type": "string",
                        "description": "cron 五段式或口语（每天上午8点）",
                    },
                    "report_type": {
                        "type": "string",
                        "enum": ["site", "bim"],
                        "description": "报告类型：site=增量摘要，bim=BIM 日报卡片",
                    },
                    "feishu_push": {"type": "boolean", "description": "完成后是否推飞书，默认 true"},
                    "feishu_webhook_url": {
                        "type": "string",
                        "description": "可选；任务级飞书群机器人 Webhook，覆盖全局 FEISHU_WEBHOOK_URL",
                    },
                    "max_items": {"type": "integer", "description": "增量 sync 条数上限，默认 10"},
                    "name": {"type": "string", "description": "任务显示名"},
                    "enabled": {"type": "boolean", "description": "是否启用"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_schedule_hermes_feishu",
            "description": (
                "管理「Hermes Agent 任务 + 飞书摘要」对话通知任务。"
                "用户说「每天 X 点执行 Hermes 任务并推飞书」时使用。"
                "create_from_intent 可解析 cron、任务指令、可选 webhook URL。"
                "到点调用 Hermes /v1/runs 执行 hermes_prompt，完成后推送飞书卡片摘要。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create",
                            "create_from_intent",
                            "update",
                            "delete",
                            "list",
                            "get",
                            "parse",
                        ],
                    },
                    "task_id": {"type": "string"},
                    "user_message": {"type": "string"},
                    "cron": {"type": "string"},
                    "hermes_prompt": {"type": "string"},
                    "feishu_push": {"type": "boolean"},
                    "feishu_webhook_url": {"type": "string"},
                    "name": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_schedule_bim_analysis_feishu",
            "description": (
                "管理「BIM 每日综合分析 + 飞书」对话通知任务。"
                "用户说「每天 X 点分析今日 BIM 公告并推飞书」时使用。"
                "到点查询今日新增 BIM 标讯，LLM 综合分析后推送飞书卡片。"
                "不爬取站点（与 incremental_sync 不同）；非自由 Hermes prompt。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create",
                            "create_from_intent",
                            "update",
                            "delete",
                            "list",
                            "get",
                            "parse",
                        ],
                    },
                    "task_id": {"type": "string"},
                    "user_message": {"type": "string"},
                    "cron": {"type": "string"},
                    "name": {"type": "string"},
                    "top_n": {"type": "integer", "description": "Top 标讯条数，默认 10"},
                    "feishu_push": {"type": "boolean"},
                    "feishu_webhook_url": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["action"],
            },
        },
    },
]

LEGACY_SYNC_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "crawl_trigger",
            "description": "【旧】触发站点后台整站 sync。对话 Agent 默认勿用，请用 skill_* 分步爬取。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_poll_until_done",
            "description": "【旧】轮询整站 sync 直到完成。对话 Agent 默认勿用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                    "poll_interval": {"type": "number"},
                },
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_reset",
            "description": "重置 stale syncing 孤儿状态。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_cancel",
            "description": "中止正在进行的站点同步。",
            "parameters": {
                "type": "object",
                "properties": {"site_id": {"type": "string"}},
                "required": ["site_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_get_run_logs",
            "description": "按 run_id 查 run 详情，或按 site_id 列最近 runs。",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "site_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
]

# 对话 Agent 默认工具集（不含 crawl_trigger / crawl_poll_until_done）
# WebBridge 工具排在最前，引导 LLM 新站优先走真实浏览器探查
DEFAULT_TOOL_SCHEMAS: list[dict[str, Any]] = (
    WEBBRIDGE_TOOL_SCHEMAS
    + SKILL_TOOL_SCHEMAS
    + INDUSTRY_TOOL_SCHEMAS
    + DIAGNOSTIC_TOOL_SCHEMAS
    + RULE_TOOL_SCHEMAS
    + HITL_TOOL_SCHEMAS
    + EXECUTE_CODE_TOOL_SCHEMAS
    + SCRIPT_CRON_TOOL_SCHEMAS
    + CHAT_SCHEDULE_TOOL_SCHEMAS
)

def resolve_tool_schemas(agent_profile: str = "default") -> list[dict[str, Any]]:
    """按 agent profile 过滤对话 Agent 可用工具。"""
    return DEFAULT_TOOL_SCHEMAS


# 全量工具（含 legacy sync，供 Cron / 外部 Hermes 使用）
TOOL_SCHEMAS: list[dict[str, Any]] = DEFAULT_TOOL_SCHEMAS + LEGACY_SYNC_TOOL_SCHEMAS


def resolve_site_id(query: str) -> Optional[str]:
    """从别名或模糊匹配解析 site_id。"""
    raw = (query or "").strip()
    if not raw:
        return None
    canonical = resolve_canonical_site_id(raw)
    if canonical != raw and get_site_by_id(canonical):
        return canonical
    text = raw.lower()
    for alias, site_id in SITE_ALIASES.items():
        if alias.lower() in text:
            return site_id
    task_svc = get_task_service()
    data = task_svc.list_sites()
    for site in data.get("sites") or []:
        sid = site.get("site_id") or ""
        name = (site.get("site_name") or "").lower()
        if text in sid.lower() or text in name:
            return sid
        if sid and any(part in text for part in sid.split("_") if len(part) > 2):
            return sid
    from src.web.browser_crawl import infer_site_id_from_url

    for candidate in (raw, raw if raw.startswith(("http://", "https://")) else f"https://{raw.lstrip('/')}"):
        inferred = infer_site_id_from_url(candidate)
        if inferred and get_site_by_id(inferred):
            return inferred
    return None


def _truncate_display(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def format_tool_command(name: str, args: Optional[dict[str, Any]] = None) -> str:
    """将工具名与参数格式化为可读命令串，如 skill_save_notice(site_id=..., title=...)."""
    args = args or {}
    parts: list[str] = []
    for key, value in args.items():
        if value is None or value == "":
            continue
        if isinstance(value, str):
            parts.append(f"{key}={_truncate_display(repr(value), 84)}")
        elif isinstance(value, (dict, list)):
            parts.append(f"{key}={_truncate_display(json.dumps(value, ensure_ascii=False), 64)}")
        else:
            parts.append(f"{key}={value!r}")
    if not parts:
        return f"{name}()"
    return f"{name}({', '.join(parts)})"


def format_http_request_command(
    method: str,
    url: str,
    body: Optional[dict[str, Any]] = None,
) -> str:
    """HTTP skill 可读命令，如 POST https://.../api/list {\"page_num\": 1}."""
    url = (url or "").strip()
    if not url:
        return ""
    display_url = _truncate_display(url, 120)
    line = f"{method.upper()} {display_url}"
    if body:
        line += " " + _truncate_display(json.dumps(body, ensure_ascii=False), 100)
    return line


def build_executed_command(
    name: str,
    args: dict[str, Any],
    result: Optional[dict[str, Any]] = None,
) -> str:
    """结合工具参数与执行结果生成 UI 展示用命令串。"""
    result = result or {}
    if name == "crawl_execute_code":
        cmd = result.get("command")
        if isinstance(cmd, list):
            return " ".join(str(part) for part in cmd)
        mode = args.get("mode")
        if mode == "script":
            script = args.get("script") or "?"
            script_args = args.get("args") or []
            suffix = " ".join(str(part) for part in script_args)
            return f"python scripts/{script}" + (f" {suffix}" if suffix else "")
    if name.startswith("skill_webbridge_"):
        action = name.removeprefix("skill_webbridge_")
        return format_tool_command(f"webbridge.{action}", args)
    list_url = (result.get("list_url") or result.get("detail_url") or "").strip()
    if list_url and name in (
        "skill_http_fetch_list",
        "skill_fetch_list_page",
        "skill_fetch_detail_page",
    ):
        method = "POST" if name == "skill_http_fetch_list" else "GET"
        body: dict[str, Any] = {}
        for key in ("page_num", "site_id", "notice_id", "url"):
            if args.get(key) is not None and args.get(key) != "":
                body[key] = args[key]
        return format_http_request_command(method, list_url, body or None)
    return format_tool_command(name, args)


def _summarize_json(data: Any, max_len: int = 800) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


class CrawlAgentToolExecutor:
    """执行 crawl 工具并返回 (result_dict, summary_str)。"""

    def __init__(
        self,
        *,
        on_progress: Optional[Callable[[str, dict[str, Any]], None]] = None,
        on_url_crawl: Optional[Callable[[dict[str, Any]], None]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._on_progress = on_progress
        self._on_url_crawl = on_url_crawl
        self._sleep = sleep_fn
        self._url_index = 0

    def reset_url_index(self) -> None:
        self._url_index = 0

    def _emit_url_crawl(self, payload: dict[str, Any]) -> None:
        if not self._on_url_crawl:
            return
        if payload.get("index") is None:
            self._url_index += 1
            payload = {**payload, "index": self._url_index}
        self._on_url_crawl(payload)

    def execute(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        from src.web.crawl_agent_cancel import is_chat_cancelled

        if is_chat_cancelled():
            payload = {
                "ok": False,
                "cancelled": True,
                "error": "用户已中止爬取",
                "arguments": dict(args or {}),
                "executed_command": build_executed_command(name, args or {}),
            }
            return payload, "已中止"
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            payload = {
                "ok": False,
                "error": f"未知工具: {name}",
                "arguments": dict(args or {}),
                "executed_command": build_executed_command(name, args or {}),
            }
            return payload, f"未知工具 {name}"
        try:
            result = handler(args or {})
            if isinstance(result, dict):
                result.setdefault("arguments", dict(args or {}))
                result.setdefault(
                    "executed_command",
                    build_executed_command(name, args or {}, result),
                )
            summary = self._auto_summary(name, result)
            if result.get("ok") is not False:
                site_id = (args or {}).get("site_id") or result.get("site_id")
                if site_id and name in ("crawl_trigger", "crawl_reset", "crawl_cancel"):
                    try:
                        append_action(
                            site_id=str(site_id),
                            action=name,
                            detail=summary[:200],
                            source="web",
                        )
                    except Exception:
                        pass
            return result, summary
        except Exception as exc:
            payload = {
                "ok": False,
                "error": str(exc),
                "arguments": dict(args or {}),
                "executed_command": build_executed_command(name, args or {}),
            }
            return payload, f"工具 {name} 异常: {exc}"

    def _auto_summary(self, name: str, result: dict[str, Any]) -> str:
        if not result.get("ok", True) and result.get("error"):
            return str(result.get("error") or result.get("message") or "失败")[:300]
        if name == "crawl_list_sites":
            sites = result.get("sites") or []
            return f"共 {len(sites)} 个站点"
        if name == "crawl_get_task_status":
            site = result.get("site") or {}
            return (
                f"{site.get('site_id', '?')}: sync={site.get('status')}, "
                f"running={site.get('is_running')}, last_run={site.get('last_run_status')}"
            )
        if name == "crawl_trigger":
            return f"已触发 run_id={result.get('run_id', '—')}"
        if name == "crawl_poll_until_done":
            return result.get("summary") or _summarize_json(result, 400)
        if name == "crawl_query_notices":
            return f"共 {result.get('total', 0)} 条公告，本页 {len(result.get('items') or [])} 条"
        if name in ("crawl_classify_agri", "crawl_classify_bim"):
            bf = result.get("backfill") or result
            return f"农业分类: 处理 {bf.get('attempted', 0)} 条，更新 {bf.get('updated', 0)} 条"
        if name in ("crawl_agri_insights", "crawl_bim_insights"):
            st = result.get("structured") or {}
            price_hint = ""
            if st.get("with_price"):
                price_hint = f"，{st['with_price']} 条含金额"
            return (
                f"近 {result.get('days', 7)} 天商机 {result.get('agri_count_in_window', 0)} 条"
                + price_hint
            )

        if name in ("crawl_agri_sync_all", "crawl_bim_sync_all"):
            return (
                f"商机洞察同步完成: 成功 {result.get('sites_ok', 0)} 站, "
                f"商机相关 {result.get('total_agri', result.get('agri_count', 0))} 条"
            )
        if name == "crawl_agri_tenders":
            return f"招标商机 {result.get('total', 0)} 条，主站商机洞察 /insights"
        if name == "crawl_stock_insights":
            return f"近 {result.get('days', 7)} 天股票 {result.get('stock_count_in_window', 0)} 条"
        if name == "crawl_stock_analysis":
            ind = result.get("industry") or result.get("industry_analysis", {}).get("industry") or "综合"
            return f"{ind} 产业发展分析已生成"
        if name == "crawl_stock_investment_plan":
            return f"{result.get('horizon_months', 12)} 个月投资思路框架已生成"

        if name == "crawl_resolve_site":
            sid = result.get("site_id")
            return f"解析为 {sid}" if sid else "未匹配到站点"
        if name == "skill_plan_crawl_path":
            return result.get("summary") or f"计划 {result.get('agent_max_pages')} 页"
        if name == "skill_http_fetch_list":
            return f"HTTP 第 {result.get('page_num')} 页 {result.get('item_count', 0)} 条"
        if name == "skill_fetch_list_page":
            return f"第 {result.get('page_num')} 页 {result.get('item_count', 0)} 条"
        if name == "skill_paginate":
            if result.get("has_next"):
                return f"已翻至第 {result.get('page_num')} 页"
            return "无更多页"
        if name == "skill_save_notice":
            if result.get("saved"):
                return f"已保存 {result.get('url', '')[:60]}"
            if result.get("duplicate"):
                return "重复跳过"
            return "未写入"
        if name == "skill_save_tagged_document":
            if result.get("inserted"):
                return f"已写入 tagged_documents id={result.get('document_id', '—')}"
            if result.get("updated"):
                return f"已更新 id={result.get('document_id', '—')}"
            if result.get("duplicate"):
                return "重复未变更"
            return "未写入"
        if name == "skill_search_by_tags":
            return f"匹配 {result.get('total', 0)} 条（{result.get('match_mode', 'any')}）"
        if name == "skill_fetch_detail_page":
            return f"详情 {'有正文' if result.get('has_content') else '无正文'}"
        if name == "skill_fetch_notice_content":
            source = result.get("content_source") or "empty"
            text_len = len(result.get("content_text") or "")
            return f"正文来源 {source}，{text_len} 字符"
        if name == "skill_extract_notice_fields":
            parts = []
            if result.get("contract_amount"):
                parts.append(f"金额 {result['contract_amount']}")
            if result.get("tender_party"):
                parts.append(f"招标方 {str(result['tender_party'])[:20]}")
            if result.get("project_period"):
                parts.append(f"周期 {str(result['project_period'])[:20]}")
            if result.get("key_content"):
                parts.append(f"关键内容 {str(result['key_content'])[:30]}")
            hint = "；".join(parts) if parts else "未抽取到字段"
            if result.get("persisted"):
                hint += "（已写回）"
            return hint
        if name == "skill_load_rules":
            return "规则已加载" if result.get("has_rule") else "无规则"
        if name == "skill_close_session":
            return "会话已关闭" if result.get("closed") else "关闭失败"
        if name == "skill_webbridge_check":
            if result.get("available"):
                return f"WebBridge 可用 v{result.get('version', '?')}"
            return result.get("error") or "WebBridge 不可用"
        if name == "skill_webbridge_navigate":
            title = (result.get("title") or "")[:40]
            return f"已打开 {result.get('url', '')[:60]} {title}".strip()
        if name == "skill_webbridge_extract_list":
            return f"提取 {result.get('item_count', 0)} 条链接"
        if name == "skill_webbridge_get_html":
            return f"HTML {result.get('html_length', 0)} 字符"
        if name == "skill_webbridge_close":
            return "WebBridge 已关闭" if result.get("closed") is not False else "关闭完成"
        if name == "skill_webbridge_snapshot":
            title = (result.get("title") or "")[:40]
            return f"{result.get('url', '')[:60]} {title}".strip()
        if name == "skill_webbridge_click":
            return f"已点击 {result.get('selector', '')[:40]}"
        if name == "skill_webbridge_fill":
            return f"已填写 {result.get('selector', '')[:40]}"
        if name == "skill_webbridge_screenshot":
            return f"截图 {result.get('bytes', 0)} bytes"
        if name == "skill_webbridge_evaluate":
            return f"evaluate {result.get('type', '?')}"
        if name == "skill_webbridge_scroll":
            return f"滚动 y={result.get('scroll_y', 0)}"
        if name == "skill_webbridge_wait":
            return f"等待 {result.get('waited_seconds', 0)}s"
        if name == "skill_webbridge_go":
            return f"{result.get('direction', '?')} → {result.get('url', '')[:50]}"
        if name == "skill_webbridge_select":
            return f"已选择 {result.get('text', result.get('value', ''))[:40]}"
        if name == "skill_webbridge_auto_crawl":
            saved = result.get("saved_count", 0)
            items = result.get("item_count", 0)
            if result.get("needs_confirmation"):
                return f"提取 {items} 条，待确认入库"
            return f"提取 {items} 条，入库 {saved} 条"
        if name == "crawl_generate_rule":
            if result.get("valid"):
                return "规则生成成功" + (" 已保存" if result.get("saved") else "")
            return f"规则无效: {result.get('errors', [])[:1]}"
        if name == "crawl_generate_workflow":
            if result.get("ok"):
                nodes = len((result.get("workflow") or {}).get("nodes") or [])
                return f"工作流生成成功 {nodes} 个节点" + (" 已保存" if result.get("saved") else "")
            return f"工作流生成失败: {(result.get('errors') or [result.get('error')])[:1]}"
        if name == "crawl_save_rule":
            return f"已保存 {result.get('path', '')}" if result.get("ok") else "保存失败"
        if name == "crawl_validate_rule":
            return "校验通过" if result.get("valid") else f"校验失败: {len(result.get('errors') or [])} 项"
        if name == "crawl_test":
            if result.get("success"):
                return f"试跑通过 {result.get('item_count', 0)} 条预览"
            errs = result.get("errors") or []
            return f"试跑失败: {errs[0] if errs else '未知错误'}"
        if name == "crawl_enable_site":
            if result.get("ok"):
                st = result.get("schedule_status") or {}
                return (
                    f"已启用 site={st.get('site_enabled')} "
                    f"eligible={st.get('schedule_eligible')}"
                )
            return result.get("error") or "启用失败"
        if name == "crawl_schedule_status":
            if result.get("schedule_eligible"):
                return f"可调度 eligible=True rule_valid={result.get('rule_valid')}"
            return f"不可调度 eligible=False rule_valid={result.get('rule_valid')}"
        if name == "skill_analyze_list_dom":
            sel = result.get("selectors") or {}
            container = sel.get("container") or "?"
            return f"推断 container={container} 样本 {len(result.get('sample_items') or [])} 条"
        if name == "crawl_generate_script":
            if result.get("valid"):
                parts = ["脚本生成成功"]
                if result.get("saved"):
                    parts.append("已保存")
                if result.get("dry_run_ok") is True:
                    parts.append("试跑通过")
                elif result.get("dry_run_ok") is False:
                    parts.append("试跑失败")
                summary = result.get("rule_summary") or {}
                if summary.get("list_strategy"):
                    parts.append(f"策略={summary.get('list_strategy')}")
                return "，".join(parts)
            return f"生成失败: {(result.get('errors') or result.get('error') or ['未知'])[:1]}"
        if name == "crawl_run_script":
            if result.get("ok"):
                mode = result.get("mode", "")
                if mode == "sync":
                    return (
                        f"执行完成 抓取 {result.get('notices_count', 0)} 条 "
                        f"新增 {result.get('new_count', 0)} 条"
                    )
                if mode == "dry_run":
                    return f"试跑完成 {len(result.get('items') or [])} 条预览"
                return "执行完成"
            return result.get("error") or "执行失败"
        if name == "crawl_get_script_status":
            meta = result.get("metadata") or {}
            if meta:
                return f"v{meta.get('version', 1)} {meta.get('script_type')} @ {meta.get('script_path', '')}"
            return "尚未生成脚本"
            return f"待办 {result.get('pending_id', '—')}"
        if name == "crawl_poll_pending":
            return f"pending {result.get('count', 0)} 条"
        if name == "crawl_wait_pending":
            if result.get("ok"):
                return f"已 resolved polls={result.get('polls')}"
            return result.get("error") or "等待超时"
        if name == "crawl_notify_user":
            return f"[{result.get('severity', 'info')}] {result.get('title', '')}"
        if name == "crawl_schedule_script":
            if result.get("job"):
                job = result["job"]
                return (
                    f"{job.get('id', '?')[:8]}… cron={job.get('cron')} "
                    f"下次={job.get('next_run_time', '—')}"
                )
            if result.get("jobs") is not None:
                return f"共 {result.get('count', 0)} 个 script cron 任务"
            return result.get("message") or _summarize_json(result, 300)
        if name == "crawl_schedule_incremental_feishu":
            if result.get("task"):
                task = result["task"]
                return (
                    f"{task.get('site_name', task.get('name', '?'))} "
                    f"type={task.get('task_type', 'incremental_sync')} "
                    f"cron={task.get('cron')} 下次={task.get('next_run_time', '—')}"
                )
            if result.get("tasks") is not None:
                return f"共 {result.get('count', 0)} 个通知任务"
            return result.get("message") or _summarize_json(result, 300)
        if name == "crawl_schedule_hermes_feishu":
            if result.get("task"):
                task = result["task"]
                return (
                    f"Hermes {task.get('name', '?')[:24]} "
                    f"cron={task.get('cron')} 下次={task.get('next_run_time', '—')}"
                )
            if result.get("tasks") is not None:
                return f"共 {result.get('count', 0)} 个通知任务"
            return result.get("message") or _summarize_json(result, 300)
        if name == "crawl_schedule_bim_analysis_feishu":
            if result.get("task"):
                task = result["task"]
                return (
                    f"BIM分析 {task.get('name', '?')[:24]} "
                    f"cron={task.get('cron')} 下次={task.get('next_run_time', '—')}"
                )
            if result.get("tasks") is not None:
                return f"共 {result.get('count', 0)} 个通知任务"
            return result.get("message") or _summarize_json(result, 300)
        if name == "crawl_execute_code":
            if result.get("timed_out"):
                return result.get("error") or "执行超时"
            rc = result.get("returncode")
            mode = result.get("mode", "?")
            out_len = len(result.get("stdout") or "")
            if result.get("ok"):
                return f"{mode} exit=0 stdout={out_len} 字符"
            err = (result.get("stderr") or result.get("error") or "")[:120]
            return f"{mode} exit={rc} {err}".strip()
        return _summarize_json(result, 300)

    def _tool_crawl_resolve_site(self, args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        site_id = resolve_site_id(query)
        if not site_id:
            return {"ok": False, "query": query, "error": f"无法从「{query}」解析 site_id"}
        site = get_site_by_id(site_id)
        return {
            "ok": True,
            "query": query,
            "site_id": site_id,
            "site_name": (site or {}).get("name"),
            "url": (site or {}).get("url"),
        }

    def _tool_crawl_list_sites(self, args: dict[str, Any]) -> dict[str, Any]:
        data = get_task_service().list_sites()
        sites = data.get("sites") or []
        status_filter = (args.get("status_filter") or "").strip().lower()
        if status_filter:
            sites = [
                s
                for s in sites
                if (s.get("status") or s.get("sync_status") or "").lower() == status_filter
            ]
        slim = [
            {
                "site_id": s.get("site_id"),
                "site_name": s.get("site_name"),
                "status": s.get("status"),
                "is_running": s.get("is_running"),
                "sync_stale": s.get("sync_stale"),
                "last_run_status": s.get("last_run_status"),
                "new_count_display": s.get("new_count_display"),
                "last_error": s.get("last_error"),
            }
            for s in sites
        ]
        return {"ok": True, "sites": slim, "count": len(slim)}

    def _tool_crawl_get_task_status(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        detail = get_task_service().get_site(site_id)
        if not detail:
            return {"ok": False, "error": site_not_registered_error(site_id)}
        out: dict[str, Any] = {"ok": True, **detail}
        if args.get("include_live_progress"):
            live = get_sync_service().get_sync_status(site_id)
            out["live_status"] = live
        return out

    def _run_async_coro(self, coro: Any) -> dict[str, Any]:
        """在同步工具 handler 中运行 async 协程（兼容 FastAPI 事件循环）。"""
        import concurrent.futures

        def _runner() -> dict[str, Any]:
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result(timeout=180)

    def _tool_crawl_trigger(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        result = self._run_async_coro(get_sync_service().trigger_sync(site_id))
        if not result.get("ok"):
            return {"ok": False, "site_id": site_id, **result}
        return {"ok": True, "site_id": site_id, **result}

    def _tool_crawl_reset(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        result = get_sync_service().reset_sync_status(site_id)
        return {"ok": bool(result.get("ok")), "site_id": site_id, **result}

    def _tool_crawl_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        result = self._run_async_coro(get_sync_service().cancel_sync(site_id))
        return {"ok": bool(result.get("ok")), "site_id": site_id, **result}

    def _tool_crawl_get_run_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        sched = get_schedule_service()
        run_id = (args.get("run_id") or "").strip()
        if run_id:
            detail = sched.get_run(run_id)
            if not detail:
                return {"ok": False, "error": f"run 不存在: {run_id}"}
            return {"ok": True, **detail}
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "run_id 或 site_id 必填"}
        limit = int(args.get("limit") or 5)
        runs = sched.list_runs(site_id=site_id, limit=limit)
        return {"ok": True, "site_id": site_id, "runs": runs.get("runs") or runs}

    def _tool_crawl_get_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = resolve_canonical_site_id((args.get("site_id") or "").strip())
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        yaml_text = load_rule_yaml(site_id)
        if yaml_text is None:
            return {"ok": True, "site_id": site_id, "has_rule": False, "yaml": None}
        validation = validate_yaml(yaml_text, site_id=site_id)
        max_items = None
        m = re.search(r"max_items:\s*(\d+)", yaml_text)
        if m:
            max_items = int(m.group(1))
        return {
            "ok": True,
            "site_id": site_id,
            "has_rule": True,
            "valid": validation.get("valid"),
            "max_items": max_items,
            "errors": validation.get("errors"),
        }

    def _tool_crawl_query_notices(self, args: dict[str, Any]) -> dict[str, Any]:
        svc = get_data_service()
        site_id = (args.get("site_id") or "").strip() or None
        keyword = (args.get("keyword") or "").strip() or None
        agri_only = bool(args.get("agri_only") or args.get("bim_only"))
        page = max(1, int(args.get("page") or 1))
        per_page = min(100, max(1, int(args.get("per_page") or 10)))
        result = svc.list_notices(
            page=page,
            per_page=per_page,
            source=site_id,
            keyword=keyword,
            has_content=not agri_only,
            agri_only=agri_only,
        )
        return {"ok": True, **result}

    def _tool_crawl_classify_agri(self, args: dict[str, Any]) -> dict[str, Any]:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts.backfill_agri_classify import run_backfill

        site_id = (args.get("site_id") or "").strip() or None
        limit = max(1, min(int(args.get("limit") or 100), 500))
        force = bool(args.get("force"))
        backfill = run_backfill(source=site_id, limit=limit, force=force)
        return {"ok": True, "backfill": backfill, **backfill}

    def _tool_crawl_agri_insights(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.agri_insights_service import get_agri_insights_service, get_main_ui_port

        days = max(1, min(int(args.get("days") or 7), 90))
        result = get_agri_insights_service().get_insights(
            days=days,
            site_id=(args.get("site_id") or "").strip() or None,
        )
        view_url = f"http://127.0.0.1:{get_main_ui_port()}/insights"
        return {"ok": True, "view_url": view_url, **result}

    def _tool_crawl_agri_sync_all(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.daily_agri_sync import run_daily_agri_sync
        from src.core.scheduler import load_yaml
        from src.web.agri_insights_service import get_main_ui_port

        schedule_path = Path(__file__).resolve().parent.parent.parent / "config" / "schedule.yaml"
        schedule_config = load_yaml(schedule_path) if schedule_path.exists() else {}
        summary = self._run_async_coro(
            run_daily_agri_sync(
                schedule_config=schedule_config,
                site_id=(args.get("site_id") or "").strip() or None,
                max_items=args.get("max_items"),
                update_status=True,
            )
        )
        view_url = f"http://127.0.0.1:{get_main_ui_port()}/insights"
        return {"ok": bool(summary.get("success")), "view_url": view_url, **summary}

    _tool_crawl_classify_bim = _tool_crawl_classify_agri
    _tool_crawl_bim_insights = _tool_crawl_agri_insights
    _tool_crawl_bim_sync_all = _tool_crawl_agri_sync_all

    def _tool_crawl_agri_tenders(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.agri_insights_service import get_agri_insights_service, get_main_ui_port

        days = max(1, min(int(args.get("days") or 30), 90))
        result = get_agri_insights_service().get_tenders(
            days=days,
            limit=int(args.get("limit") or 50),
            min_score=int(args.get("min_score") or 0),
            site_id=(args.get("site_id") or "").strip() or None,
        )
        view_url = f"http://127.0.0.1:{get_main_ui_port()}/insights/tenders"
        return {"ok": True, "view_url": view_url, **result}

    def _tool_crawl_stock_insights(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.stock_insights_service import get_stock_insights_service, get_stock_ui_port

        days = max(1, min(int(args.get("days") or 7), 90))
        result = get_stock_insights_service().get_insights(
            days=days,
            use_llm=bool(args.get("use_llm")),
            refresh_llm=bool(args.get("refresh_llm")),
        )
        view_url = f"http://127.0.0.1:{get_stock_ui_port()}/"
        return {"ok": True, "view_url": view_url, **result}

    def _tool_crawl_stock_analysis(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.stock_insights_service import get_stock_insights_service, get_stock_ui_port

        industry = (args.get("industry") or args.get("sector") or "综合").strip()
        days = max(1, min(int(args.get("days") or 7), 90))
        result = get_stock_insights_service().generate_industry_analysis(
            industry=industry,
            days=days,
            use_llm=bool(args.get("use_llm", False)),
        )
        view_url = f"http://127.0.0.1:{get_stock_ui_port()}/?industry={industry}"
        return {"ok": True, "view_url": view_url, "industry": industry, **result}

    def _tool_crawl_stock_investment_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.stock_insights_service import get_stock_insights_service, get_stock_ui_port

        industries = args.get("industries")
        if isinstance(industries, str):
            industries = [industries]
        result = get_stock_insights_service().generate_investment_plan(
            industries=industries if isinstance(industries, list) else None,
            risk_profile=str(args.get("risk_profile") or "balanced"),
            horizon_months=int(args.get("horizon_months") or 12),
            days=int(args.get("days") or 30),
            use_llm=bool(args.get("use_llm", False)),
        )
        view_url = f"http://127.0.0.1:{get_stock_ui_port()}/"
        return {"ok": True, "view_url": view_url, **result}

    def _tool_crawl_poll_until_done(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        timeout = max(5, int(args.get("timeout_seconds") or 120))
        interval = max(1.0, float(args.get("poll_interval") or 3.0))
        sync_svc = get_sync_service()
        url_tracker = UrlCrawlTracker() if self._on_url_crawl else None
        seen_event_count = 0
        deadline = time.monotonic() + timeout
        polls = 0
        last_progress: dict[str, Any] = {}

        def _emit_run_events(run_id: str) -> None:
            nonlocal seen_event_count
            if not run_id or not self._on_url_crawl or url_tracker is None:
                return
            try:
                events = get_crawl_run_store().list_events(run_id)
            except Exception:
                return
            if len(events) <= seen_event_count:
                return
            new_slice = events[seen_event_count:]
            seen_event_count = len(events)
            for item in url_tracker.process_events(new_slice):
                self._on_url_crawl(item)

        while time.monotonic() < deadline:
            polls += 1
            status = sync_svc.get_sync_status(site_id)
            if not status.get("ok"):
                return {"ok": False, "site_id": site_id, "error": status.get("message")}
            is_running = bool(
                status.get("is_running")
                or status.get("sync_status") == "syncing"
                or status.get("status") == "syncing"
            )
            progress = status.get("progress") or {}
            run_id = status.get("run_id") or progress.get("run_id") or ""
            last_progress = {
                "status": status.get("sync_status") or status.get("status") or progress.get("status"),
                "run_id": run_id,
                "progress_percent": progress.get("progress_percent"),
                "progress_items": progress.get("progress_items"),
                "max_items": progress.get("max_items"),
            }
            _emit_run_events(str(run_id) if run_id else "")
            if self._on_progress:
                self._on_progress(site_id, last_progress)
            if not is_running:
                detail = get_task_service().get_site(site_id) or {}
                site = detail.get("site") or {}
                url_summary = url_tracker.summary() if url_tracker else {"total": 0, "by_status": {}, "recent": []}
                summary = (
                    f"完成 polls={polls}: status={site.get('status')}, "
                    f"last_run={site.get('last_run_status')}, "
                    f"new={site.get('new_count_display', 0)}"
                )
                if url_summary.get("total"):
                    summary += f", urls={url_summary['total']}"
                return {
                    "ok": True,
                    "site_id": site_id,
                    "polls": polls,
                    "summary": summary,
                    "final_status": last_progress,
                    "site": site,
                    "url_summary": url_summary,
                }
            self._sleep(interval)
        return {
            "ok": False,
            "site_id": site_id,
            "polls": polls,
            "timeout_seconds": timeout,
            "last_progress": last_progress,
            "url_summary": url_tracker.summary() if url_tracker else {"total": 0, "by_status": {}, "recent": []},
            "error": f"轮询超时 ({timeout}s)，任务可能仍在运行",
        }

    def _tool_skill_plan_crawl_path(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        max_pages = args.get("max_pages")
        return plan_crawl_path(
            site_id,
            user_intent=(args.get("user_intent") or ""),
            max_pages_override=int(max_pages) if max_pages is not None else None,
        )

    def _tool_skill_load_rules(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        return load_rules(site_id)

    def _tool_skill_analyze_list_dom(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.dom_selector_hints import analyze_list_dom, format_page_hints_for_llm

        html = (args.get("html") or "").strip()
        url = (args.get("url") or "").strip()
        session_id = (args.get("session_id") or "").strip() or None
        notes = (args.get("notes") or "").strip()

        if not html and session_id:
            snap = webbridge_snapshot(session_id=session_id)
            if snap.get("ok") and not url:
                url = (snap.get("url") or "").strip()
            html_result = webbridge_get_html(session_id=session_id)
            if not html_result.get("ok"):
                return {"ok": False, "error": html_result.get("error") or "无法获取 HTML"}
            html = (html_result.get("html") or "").strip()

        if not html:
            return {"ok": False, "error": "html 或 session_id 必填"}

        analysis = analyze_list_dom(html, url=url)
        page_hints = format_page_hints_for_llm(analysis, extra_notes=notes)
        return {
            "ok": analysis.get("ok", False),
            "url": url or None,
            "selectors": analysis.get("selectors") or {},
            "sample_items": analysis.get("sample_items") or [],
            "page_hints": page_hints,
            "page_title": analysis.get("page_title"),
            "api_hints": analysis.get("api_hints") or [],
        }

    def _tool_skill_http_fetch_list(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        page_num = max(1, int(args.get("page_num") or 1))
        result = run_async(
            http_fetch_list_page_async(
                site_id,
                page_num=page_num,
                session_id=(args.get("session_id") or "").strip() or None,
            )
        )
        if result.get("ok") and self._on_url_crawl:
            for item in result.get("items") or []:
                self._emit_url_crawl(
                    {
                        "url": item.get("url"),
                        "title": item.get("title"),
                        "status": "fetching",
                        "message": f"HTTP 列表第 {page_num} 页",
                    }
                )
        return result

    def _tool_skill_fetch_list_page(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        page_num = max(1, int(args.get("page_num") or 1))
        result = run_async(
            fetch_list_page_async(
                site_id,
                page_num=page_num,
                list_url=(args.get("list_url") or "").strip() or None,
                session_id=(args.get("session_id") or "").strip() or None,
            )
        )
        if result.get("ok") and self._on_url_crawl:
            for item in result.get("items") or []:
                self._emit_url_crawl(
                    {
                        "url": item.get("url"),
                        "title": item.get("title"),
                        "status": "fetching",
                        "message": f"列表第 {page_num} 页",
                    }
                )
        return result

    def _tool_skill_paginate(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        page_num = args.get("page_num")
        session_id = (args.get("session_id") or "").strip() or None
        target_page = int(page_num) if page_num is not None else None
        if self._on_url_crawl:
            self._emit_url_crawl(
                {
                    "url": (args.get("current_url") or "").strip() or site_id,
                    "title": f"翻页至第 {target_page or '下一'} 页",
                    "status": "paginating",
                    "message": "skill_paginate 进行中",
                }
            )
        result = run_async(
            paginate_async(
                site_id,
                current_url=(args.get("current_url") or "").strip() or None,
                page_num=target_page,
                session_id=session_id,
            )
        )
        if result.get("ok") and self._on_url_crawl:
            self._emit_url_crawl(
                {
                    "url": result.get("next_page_url") or result.get("list_url") or site_id,
                    "title": f"第 {result.get('page_num')} 页",
                    "status": "fetching" if result.get("has_next") else "done",
                    "message": result.get("next_page_url") or "无更多页",
                }
            )
        return result

    def _tool_skill_fetch_detail_page(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        url = (args.get("url") or "").strip()
        if not site_id or not url:
            return {"ok": False, "error": "site_id 与 url 必填"}
        if self._on_url_crawl:
            self._emit_url_crawl(
                {
                    "url": url,
                    "title": args.get("title") or url,
                    "status": "fetching",
                    "message": "抓取详情",
                }
            )
        result = run_async(
            fetch_detail_page_async(
                site_id,
                url,
                title=(args.get("title") or "").strip() or None,
            )
        )
        if result.get("ok") and self._on_url_crawl:
            self._emit_url_crawl(
                {
                    "url": url,
                    "title": result.get("title") or url,
                    "status": "saved" if result.get("has_content") else "fetching",
                    "message": "详情已抓取",
                }
            )
        return result

    def _tool_skill_fetch_notice_content(self, args: dict[str, Any]) -> dict[str, Any]:
        notice_id = (args.get("notice_id") or "").strip() or None
        detail_url = (args.get("detail_url") or "").strip() or None
        pdf_url = (args.get("pdf_url") or "").strip() or None
        if not notice_id and not detail_url and not pdf_url:
            return {"ok": False, "error": "notice_id、detail_url 或 pdf_url 至少填一个"}
        verify_title = args.get("verify_title")
        if verify_title is None:
            verify_title = True
        if self._on_url_crawl and (detail_url or notice_id):
            self._emit_url_crawl(
                {
                    "url": detail_url or f"powerchina:getInfo/{notice_id}",
                    "title": args.get("expected_title") or notice_id or detail_url,
                    "status": "fetching",
                    "message": "抓取 getInfo/PDF 正文",
                }
            )
        result = run_async(
            fetch_notice_content_async(
                notice_id=notice_id,
                detail_url=detail_url,
                pdf_url=pdf_url,
                expected_title=(args.get("expected_title") or "").strip() or None,
                verify_title=bool(verify_title),
            )
        )
        if result.get("ok") and self._on_url_crawl:
            self._emit_url_crawl(
                {
                    "url": result.get("detail_url") or detail_url or pdf_url,
                    "title": result.get("title") or args.get("expected_title") or "",
                    "status": "saved" if result.get("has_content") else "fetching",
                    "message": f"正文来源 {result.get('content_source')}",
                }
            )
        return result

    def _tool_skill_extract_notice_fields(self, args: dict[str, Any]) -> dict[str, Any]:
        doc = args.get("doc") if isinstance(args.get("doc"), dict) else None
        return extract_notice_fields(
            doc=doc,
            site_id=(args.get("site_id") or "").strip() or None,
            notice_id=(args.get("notice_id") or "").strip() or None,
            url=(args.get("url") or "").strip() or None,
            content_text=(args.get("content_text") or "").strip() or None,
            title=(args.get("title") or "").strip() or None,
            persist=bool(args.get("persist")),
            use_llm=args.get("use_llm"),
        )

    def _tool_skill_save_notice(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        title = (args.get("title") or "").strip()
        url = (args.get("url") or "").strip()
        if not site_id or not title or not url:
            return {"ok": False, "error": "site_id、title、url 必填"}
        return save_notice(
            site_id,
            title=title,
            url=url,
            publish_date=(args.get("publish_date") or "").strip() or None,
            content_text=(args.get("content_text") or "").strip() or None,
            content_html=(args.get("content_html") or "").strip() or None,
            attachments=args.get("attachments"),
            on_url_crawl=self._emit_url_crawl if self._on_url_crawl else None,
        )

    def _tool_skill_supply_chain_graph(self, args: dict[str, Any]) -> dict[str, Any]:
        mode = (args.get("mode") or "graph").strip().lower()
        if mode == "table":
            from src.web.industry_insights_service import get_industry_insights_service

            code = (args.get("industry_code") or args.get("domain") or "domain:数字农业").strip()
            if not code.startswith("domain:") and args.get("domain"):
                code = f"domain:{args.get('domain')}"
            days = int(args.get("days") or 30)
            svc = get_industry_insights_service()
            try:
                table = svc.supply_chain_table(code, days=days, limit=int(args.get("limit") or 20))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "mode": "table", "industry_code": code, **table}
        from src.industry.graph.neo4j_sync import get_neo4j_graph_service

        depth = max(1, min(int(args.get("depth") or 2), 4))
        graph = get_neo4j_graph_service().query_subgraph(
            center=(args.get("center") or "").strip() or None,
            industry_code=(args.get("industry_code") or "").strip() or None,
            depth=depth,
            limit=int(args.get("limit") or 100),
        )
        nodes = graph.get("elements", {}).get("nodes") or []
        edges = graph.get("elements", {}).get("edges") or []
        return {
            "ok": True,
            "mode": "graph",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "view_url": "/insights/supply-chain",
            **graph,
        }

    def _tool_skill_save_tagged_document(self, args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "").strip()
        tags = args.get("tags")
        if not isinstance(tags, list):
            tags = [tags] if tags else []
        return save_tagged_document(
            title=title,
            tags=tags,
            url=(args.get("url") or "").strip() or None,
            content=(args.get("content") or "").strip() or None,
            site_id=(args.get("site_id") or "").strip() or None,
            metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else None,
            source=(args.get("source") or "").strip() or None,
        )

    def _tool_skill_search_by_tags(self, args: dict[str, Any]) -> dict[str, Any]:
        tags = args.get("tags")
        if not isinstance(tags, list):
            tags = [tags] if tags else []
        limit = args.get("limit")
        try:
            limit_val = int(limit) if limit is not None else 20
        except (TypeError, ValueError):
            limit_val = 20
        return search_tagged_documents(
            tags,
            match_mode=(args.get("match_mode") or "any").strip() or "any",
            site_id=(args.get("site_id") or "").strip() or None,
            limit=limit_val,
        )

    def _tool_skill_close_session(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        session_id = (args.get("session_id") or "").strip() or None
        self.close_skill_session(site_id, session_id)
        return {"ok": True, "site_id": site_id, "session_id": session_id, "closed": True}

    def _tool_skill_webbridge_check(self, args: dict[str, Any]) -> dict[str, Any]:
        return check_webbridge()

    def _tool_skill_webbridge_navigate(self, args: dict[str, Any]) -> dict[str, Any]:
        url = (args.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url 必填"}
        new_tab = args.get("new_tab")
        if new_tab is None:
            new_tab = True
        return webbridge_navigate(
            url,
            session_id=(args.get("session_id") or "").strip() or None,
            new_tab=bool(new_tab),
            group_title=(args.get("group_title") or "").strip() or None,
        )

    def _tool_skill_webbridge_extract_list(self, args: dict[str, Any]) -> dict[str, Any]:
        selector = (args.get("selector") or "").strip() or None
        hint = (args.get("hint") or "").strip() or None
        if not selector and not hint:
            return {"ok": False, "error": "selector 或 hint 至少提供一个"}
        max_items = args.get("max_items")
        return webbridge_extract_list(
            selector=selector,
            hint=hint,
            session_id=(args.get("session_id") or "").strip() or None,
            max_items=int(max_items) if max_items is not None else 50,
        )

    def _tool_skill_webbridge_get_html(self, args: dict[str, Any]) -> dict[str, Any]:
        max_chars = args.get("max_chars")
        return webbridge_get_html(
            session_id=(args.get("session_id") or "").strip() or None,
            max_chars=int(max_chars) if max_chars is not None else 50_000,
        )

    def _tool_skill_webbridge_close(self, args: dict[str, Any]) -> dict[str, Any]:
        close_session = bool(args.get("close_session"))
        return webbridge_close(
            session_id=(args.get("session_id") or "").strip() or None,
            close_session=close_session,
        )

    def _tool_skill_webbridge_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        return webbridge_snapshot(
            session_id=(args.get("session_id") or "").strip() or None,
        )

    def _tool_skill_webbridge_click(self, args: dict[str, Any]) -> dict[str, Any]:
        selector = (args.get("selector") or "").strip()
        if not selector:
            return {"ok": False, "error": "selector 必填"}
        return webbridge_click(
            selector,
            session_id=(args.get("session_id") or "").strip() or None,
        )

    def _tool_skill_webbridge_fill(self, args: dict[str, Any]) -> dict[str, Any]:
        selector = (args.get("selector") or "").strip()
        if not selector:
            return {"ok": False, "error": "selector 必填"}
        return webbridge_fill(
            selector,
            str(args.get("value") if args.get("value") is not None else ""),
            session_id=(args.get("session_id") or "").strip() or None,
        )

    def _tool_skill_webbridge_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        fmt = (args.get("format") or "png").strip().lower()
        if fmt not in ("png", "jpeg"):
            fmt = "png"
        quality = args.get("quality")
        return webbridge_screenshot(
            session_id=(args.get("session_id") or "").strip() or None,
            format=fmt,
            quality=int(quality) if quality is not None else 80,
            selector=(args.get("selector") or "").strip() or None,
        )

    def _tool_skill_webbridge_evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        code = (args.get("code") or "").strip()
        if not code:
            return {"ok": False, "error": "code 必填"}
        max_chars = args.get("max_result_chars")
        return webbridge_evaluate(
            code,
            session_id=(args.get("session_id") or "").strip() or None,
            max_result_chars=int(max_chars) if max_chars is not None else 8000,
        )

    def _tool_skill_webbridge_scroll(self, args: dict[str, Any]) -> dict[str, Any]:
        x = args.get("x")
        y = args.get("y")
        return webbridge_scroll(
            session_id=(args.get("session_id") or "").strip() or None,
            x=int(x) if x is not None else 0,
            y=int(y) if y is not None else 500,
        )

    def _tool_skill_webbridge_wait(self, args: dict[str, Any]) -> dict[str, Any]:
        seconds = args.get("seconds")
        if seconds is None:
            return {"ok": False, "error": "seconds 必填"}
        return webbridge_wait(
            float(seconds),
            session_id=(args.get("session_id") or "").strip() or None,
        )

    def _tool_skill_webbridge_go(self, args: dict[str, Any]) -> dict[str, Any]:
        direction = (args.get("direction") or "").strip().lower()
        if direction not in ("back", "forward"):
            return {"ok": False, "error": "direction 须为 back 或 forward"}
        return webbridge_go(
            direction,
            session_id=(args.get("session_id") or "").strip() or None,
        )

    def _tool_skill_webbridge_select(self, args: dict[str, Any]) -> dict[str, Any]:
        selector = (args.get("selector") or "").strip()
        value = (args.get("value") or "").strip()
        if not selector:
            return {"ok": False, "error": "selector 必填"}
        if not value:
            return {"ok": False, "error": "value 必填"}
        return webbridge_select(
            selector,
            value,
            session_id=(args.get("session_id") or "").strip() or None,
        )

    def _tool_skill_webbridge_auto_crawl(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.browser_crawl import browser_auto_crawl, infer_site_id_from_url
        from src.web.webbridge_skills import webbridge_snapshot

        site_id = (args.get("site_id") or "").strip()
        session_id = (args.get("session_id") or "").strip() or None
        if not site_id:
            snap = webbridge_snapshot(session_id=session_id)
            if not snap.get("ok"):
                return {"ok": False, "error": snap.get("error") or "无法 snapshot 推断 site_id"}
            site_id = infer_site_id_from_url(snap.get("url") or "") or ""
        if not site_id:
            return {"ok": False, "error": "无法推断 site_id，请显式传入"}
        max_pages = args.get("max_pages")
        save = args.get("save")
        confirm_generic = args.get("confirm_generic")
        return browser_auto_crawl(
            session_id=session_id or "crawl-agent",
            site_id=site_id,
            max_pages=int(max_pages) if max_pages is not None else None,
            save=False if save is False else True,
            confirm_generic=bool(confirm_generic),
        )

    def _tool_crawl_generate_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        url = (args.get("url") or "").strip()
        messages = args.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        page_hints = (args.get("page_hints") or "").strip()
        result = self._run_async_coro(
            get_rule_generator().generate(
                site_id=site_id,
                url=url,
                messages=messages,
                page_hints=page_hints,
                fetch_hints=not bool(page_hints),
            )
        )
        if args.get("save") and result.get("valid") and result.get("yaml"):
            save_result = save_rule_yaml(site_id, result["yaml"])
            result["saved"] = save_result.get("ok", False)
            if save_result.get("path"):
                result["path"] = save_result["path"]
        return {"ok": bool(result.get("valid")), "site_id": site_id, **result}

    def _tool_crawl_generate_workflow(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.web.crawl_rule_workflow_service import get_crawl_rule_workflow_service

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        result = self._run_async_coro(
            get_crawl_rule_workflow_service().generate_workflow(
                site_id,
                url=(args.get("url") or "").strip(),
                prompt=(args.get("prompt") or "").strip(),
                page_hints=(args.get("page_hints") or "").strip(),
                save=bool(args.get("save")),
            )
        )
        if result.get("ok"):
            result["workflow_url"] = f"/crawl-rules/{site_id}/workflow"
        return result

    def _tool_crawl_save_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip()
        yaml_text = (args.get("yaml") or "").strip()
        if not site_id or not yaml_text:
            return {"ok": False, "error": "site_id 与 yaml 必填"}
        result = save_rule_yaml(site_id, yaml_text)
        return {"ok": bool(result.get("ok")), "site_id": site_id, **result}

    def _tool_crawl_validate_rule(self, args: dict[str, Any]) -> dict[str, Any]:
        site_id = (args.get("site_id") or "").strip() or None
        yaml_text = (args.get("yaml") or "").strip()
        if not yaml_text:
            return {"ok": False, "error": "yaml 必填"}
        result = validate_yaml(yaml_text, site_id=site_id)
        return {"ok": bool(result.get("valid")), **result}

    def _tool_crawl_test(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_rule_dry_run import dry_run_crawl_rule
        from src.core.site_config_write import get_site_schedule_status

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        yaml_text = (args.get("yaml") or "").strip() or None
        max_items = int(args.get("max_items") or 5)
        max_pages = int(args.get("max_pages") or 1)
        dry_result = self._run_async_coro(
            dry_run_crawl_rule(
                site_id,
                yaml_text=yaml_text,
                max_items=max_items,
                max_pages=max_pages,
            )
        )
        success = bool(dry_result.get("success"))
        payload: dict[str, Any] = {
            "ok": success,
            "site_id": site_id,
            "success": success,
            "items": dry_result.get("items") or [],
            "errors": dry_result.get("errors") or [],
            "transport": dry_result.get("transport"),
            "item_count": len(dry_result.get("items") or []),
        }
        if success and not yaml_text:
            payload["schedule_status"] = get_site_schedule_status(site_id)
        return payload

    def _tool_crawl_enable_site(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.site_config_write import enable_site_after_probe

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        return enable_site_after_probe(
            site_id,
            enable_site=args.get("enable_site", True) is not False,
            enable_rule=args.get("enable_rule", True) is not False,
            require_schedule_eligible=args.get("require_schedule_eligible", True) is not False,
        )

    def _tool_crawl_schedule_status(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.site_config_write import get_site_schedule_status

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        return get_site_schedule_status(site_id)

    def _tool_crawl_generate_script(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_script_service import generate_crawl_script

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        url = (args.get("url") or "").strip()
        messages = args.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        max_items = int(args.get("max_items") or 5)
        page_hints = (args.get("page_hints") or "").strip()
        result = self._run_async_coro(
            generate_crawl_script(
                site_id=site_id,
                url=url,
                messages=messages,
                page_hints=page_hints,
                save=bool(args.get("save")),
                dry_run=bool(args.get("dry_run")),
                max_items=max_items,
            )
        )
        return result

    def _tool_crawl_run_script(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_script_service import run_generated_crawl

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        dry_run = bool(args.get("dry_run"))
        persist = False if dry_run else args.get("persist", True) is not False
        max_items = args.get("max_items")
        result = self._run_async_coro(
            run_generated_crawl(
                site_id,
                dry_run=dry_run,
                max_items=int(max_items) if max_items is not None else None,
                persist=persist,
            )
        )
        return result

    def _tool_crawl_get_script_status(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_script_service import get_crawl_script_status

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        return get_crawl_script_status(site_id)

    def _tool_crawl_request_user_input(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_agent_pending import create_pending_item

        site_id = (args.get("site_id") or "").strip()
        if not site_id:
            return {"ok": False, "error": "site_id 必填"}
        if not get_site_by_id(site_id):
            return {"ok": False, "error": site_not_registered_error(site_id)}
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "prompt 必填"}
        input_type = (args.get("input_type") or "generic").strip()
        try:
            item = create_pending_item(
                site_id=site_id,
                input_type=input_type,
                prompt=prompt,
                choices=args.get("choices"),
                context=args.get("context"),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "pending_id": item["id"], "item": item}

    def _tool_crawl_poll_pending(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_agent_pending import list_pending_items

        status = (args.get("status") or "pending").strip() or "pending"
        site_id = (args.get("site_id") or "").strip() or None
        items = list_pending_items(status=status, site_id=site_id)
        return {"ok": True, "items": items, "count": len(items)}

    def _tool_crawl_wait_pending(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.crawl_agent_pending import get_pending_item

        pending_id = (args.get("pending_id") or "").strip()
        if not pending_id:
            return {"ok": False, "error": "pending_id 必填"}
        timeout = max(1, int(args.get("timeout_seconds") or 120))
        interval = max(0.5, float(args.get("poll_interval") or 2.0))
        deadline = time.monotonic() + timeout
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            item = get_pending_item(pending_id)
            if item is None:
                return {"ok": False, "error": f"待办项不存在: {pending_id}"}
            if item.get("status") == "resolved":
                return {"ok": True, "pending_id": pending_id, "polls": polls, "item": item}
            self._sleep(interval)
        item = get_pending_item(pending_id)
        return {
            "ok": False,
            "pending_id": pending_id,
            "polls": polls,
            "timeout_seconds": timeout,
            "last_status": (item or {}).get("status"),
            "error": f"等待超时 ({timeout}s)",
        }

    def _tool_crawl_notify_user(self, args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        severity = (args.get("severity") or "info").strip().lower()
        if not title or not body:
            return {"ok": False, "error": "title 与 body 必填"}
        message = f"[{severity.upper()}] {title}: {body}"
        logger.info("crawl_notify_user: %s", message)
        print(message, flush=True)
        return {
            "ok": True,
            "title": title,
            "body": body,
            "severity": severity,
            "delivered": True,
            "channel": "cli",
        }

    def _tool_crawl_execute_code(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.code_executor import execute_code

        mode = (args.get("mode") or "").strip()
        timeout = args.get("timeout_seconds")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                return {"ok": False, "error": "timeout_seconds 须为整数"}
        return execute_code(
            mode,
            script=(args.get("script") or "").strip() or None,
            args=args.get("args"),
            code=args.get("code"),
            label=(args.get("label") or "").strip() or None,
            timeout_seconds=timeout,
        )

    def _tool_crawl_schedule_script(self, args: dict[str, Any]) -> dict[str, Any]:
        action = (args.get("action") or "").strip()
        if not action:
            return {"ok": False, "error": "action 必填"}
        svc = get_script_cron_service()
        enabled = args.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            enabled = str(enabled).lower() in ("1", "true", "yes")
        return svc.manage(
            action,
            job_id=(args.get("job_id") or "").strip() or None,
            script=(args.get("script") or "").strip() or None,
            cron=(args.get("cron") or "").strip() or None,
            args=args.get("args"),
            name=(args.get("name") or "").strip() or None,
            enabled=enabled,
        )

    def _tool_crawl_schedule_incremental_feishu(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.chat_scheduled_tasks import TASK_TYPE_INCREMENTAL

        return self._schedule_feishu_manage(args, task_type=TASK_TYPE_INCREMENTAL)

    def _tool_crawl_schedule_hermes_feishu(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.chat_scheduled_tasks import TASK_TYPE_HERMES

        return self._schedule_feishu_manage(args, task_type=TASK_TYPE_HERMES)

    def _tool_crawl_schedule_bim_analysis_feishu(self, args: dict[str, Any]) -> dict[str, Any]:
        from src.core.chat_scheduled_tasks import TASK_TYPE_BIM_DAILY_ANALYSIS

        return self._schedule_feishu_manage(args, task_type=TASK_TYPE_BIM_DAILY_ANALYSIS)

    def _schedule_feishu_manage(self, args: dict[str, Any], *, task_type: str) -> dict[str, Any]:
        from src.web.chat_schedule_service import get_chat_schedule_service

        action = (args.get("action") or "").strip()
        if not action:
            return {"ok": False, "error": "action 必填"}
        svc = get_chat_schedule_service()
        enabled = args.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            enabled = str(enabled).lower() in ("1", "true", "yes")
        feishu_push = args.get("feishu_push")
        if feishu_push is not None and not isinstance(feishu_push, bool):
            feishu_push = str(feishu_push).lower() in ("1", "true", "yes")
        site_id = (args.get("site_id") or "").strip() or None
        if site_id:
            site_id = resolve_site_id(site_id) or site_id
        return svc.manage(
            action,
            task_type=task_type,
            task_id=(args.get("task_id") or "").strip() or None,
            user_message=(args.get("user_message") or "").strip() or None,
            site_id=site_id,
            cron=(args.get("cron") or "").strip() or None,
            name=(args.get("name") or "").strip() or None,
            report_type=(args.get("report_type") or "").strip() or None,
            feishu_push=feishu_push,
            max_items=args.get("max_items"),
            hermes_prompt=(args.get("hermes_prompt") or "").strip() or None,
            feishu_webhook_url=(args.get("feishu_webhook_url") or "").strip() or None,
            top_n=args.get("top_n"),
            enabled=enabled,
        )

    def close_skill_session(self, site_id: str, session_id: Optional[str] = None) -> None:
        run_async(_close_skill_session_async(site_id, session_id))
