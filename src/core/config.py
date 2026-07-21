"""应用配置（pydantic-settings，支持 .env）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """从环境变量 / .env 加载的全局配置。"""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: Optional[str] = Field(
        default=None,
        description="PostgreSQL 连接串，如 postgresql://scraper:scraper_pass@localhost:5432/bid_scraper",
    )
    redis_url: Optional[str] = Field(
        default=None,
        description="Redis 连接串，如 redis://localhost:6379/0",
    )
    redis_dedup_ttl_seconds: int = Field(
        default=0,
        description="Redis 去重 SET 的 TTL（秒），0 表示不过期",
    )
    data_dir: Path = Field(default=ROOT / "data", description="JSONL 落盘目录")
    mongodb_uri: Optional[str] = Field(
        default=None,
        description="MongoDB 连接串，如 mongodb://localhost:27018/bid_scraper",
    )
    mongodb_db: str = Field(default="bid_scraper", description="MongoDB 数据库名")
    mongodb_collection: str = Field(
        default="bid_notices",
        description="MongoDB 集合名",
    )
    recursive_crawl_llm: bool = Field(
        default=False,
        description="[已废弃] 请使用 INTELLIGENT_CRAWL_LLM",
    )
    intelligent_crawl_llm: bool = Field(
        default=False,
        description="是否启用 LLM 语义决策（默认规则引擎）",
    )
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI API Key")
    llm_base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL"),
        description="LLM API Base URL（不含 /v1，客户端自动拼接）",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL"),
        description="LLM 模型名",
    )

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def _normalize_llm_base_url(cls, value: object) -> object:
        if not value or not isinstance(value, str):
            return value
        url = value.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        return url
    key_info_llm: bool = Field(
        default=True,
        description="是否启用 LLM 从正文提取关键项目信息（有 OPENAI_API_KEY 时默认开启）",
    )
    bim_classify_llm: bool = Field(
        default=True,
        description="是否启用 LLM 判断 BIM 相关招标（需 OPENAI_API_KEY）",
    )
    agri_classify_llm: bool = Field(
        default=False,
        description="是否启用 LLM 判断智慧农业相关招标（默认关键词；需 OPENAI_API_KEY）",
    )
    site_credentials_key: Optional[str] = Field(
        default=None,
        description="站点凭据加密密钥（SITE_CREDENTIALS_KEY）；未设置时使用开发默认值",
    )
    crawl_via_hermes: bool = Field(
        default=True,
        description="定时/手动 sync 经 Hermes Agent Runner 编排（CRAWL_VIA_HERMES，默认 true）",
    )
    hermes_agent_url: Optional[str] = Field(
        default=None,
        description="外部 hermes-agent Gateway crawl dispatch 根 URL（HERMES_AGENT_URL，如 http://127.0.0.1:8080）",
    )
    hermes_agent_timeout: float = Field(
        default=300.0,
        ge=1.0,
        description="Hermes Gateway HTTP 超时秒数（HERMES_AGENT_TIMEOUT，默认 300）",
    )
    hermes_agent_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "HERMES_AGENT_API_KEY",
            "API_SERVER_KEY",
            "HERMES_CRAWL_DISPATCH_KEY",
        ),
        description="Hermes Gateway / crawl dispatch Bearer Token（可选）",
    )
    hermes_agent_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Gateway HTTP 失败重试次数（HERMES_AGENT_RETRIES，默认 3）",
    )
    hermes_agent_chat_url: Optional[str] = Field(
        default=None,
        description=(
            "Hermes Agent API Server 根 URL（HERMES_AGENT_CHAT_URL，对话 /v1/runs；"
            "未设置时由 HERMES_AGENT_URL 推导 :8642）"
        ),
    )
    crawl_agent_max_rounds: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Hermes 对话 Agent 单次请求最大 LLM 推理轮次（CRAWL_AGENT_MAX_ROUNDS）",
    )
    crawl_agent_max_rounds_full: int = Field(
        default=100,
        ge=1,
        le=200,
        description="Hermes 全量爬取任务最大 LLM 推理轮次（CRAWL_AGENT_MAX_ROUNDS_FULL）",
    )
    sse_reconnect_max: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Hermes 对话 SSE 断线自动重连次数（SSE_RECONNECT_MAX，默认 10）",
    )
    crawl_agent_stream_stale_minutes: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="streaming 会话无活跃 run 超过此分钟数则自动 finalize（CRAWL_AGENT_STREAM_STALE_MINUTES）",
    )
    crawl_agent_max_history_turns: int = Field(
        default=6,
        ge=0,
        le=30,
        description="Hermes 对话传入的历史 user 轮数上限（CRAWL_AGENT_MAX_HISTORY_TURNS）",
    )
    crawl_agent_max_history_content_chars: int = Field(
        default=2000,
        ge=256,
        le=32_000,
        description="历史 user 消息单条字符上限（CRAWL_AGENT_MAX_HISTORY_CONTENT_CHARS）",
    )
    http_proxy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("HTTP_PROXY", "http_proxy"),
        description="HTTP 代理（如 http://127.0.0.1:7890），供 httpx API 抓取使用",
    )
    https_proxy: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("HTTPS_PROXY", "https_proxy"),
        description="HTTPS 代理（未设置时回退 HTTP_PROXY）",
    )
    stock_news_sources: Optional[str] = Field(
        default=None,
        description="股票新闻源列表，逗号分隔：akshare,mock,bid_notices,rss 等（STOCK_NEWS_SOURCES）",
    )
    stock_data_provider: str = Field(
        default="akshare",
        description="A 股行情数据提供方：akshare | tushare（STOCK_DATA_PROVIDER）",
    )
    tushare_token: Optional[str] = Field(
        default=None,
        description="Tushare Pro Token（TUSHARE_TOKEN，可选）",
    )
    stock_news_rss: Optional[str] = Field(
        default=None,
        description="额外 RSS 源 URL，逗号分隔（STOCK_NEWS_RSS）",
    )
    influxdb_url: Optional[str] = Field(
        default=None,
        description="InfluxDB 2.x URL，如 http://127.0.0.1:8086",
    )
    influxdb_token: Optional[str] = Field(
        default=None,
        description="InfluxDB API Token",
    )
    influxdb_org: str = Field(
        default="web_scraper",
        description="InfluxDB 组织名",
    )
    influxdb_bucket: str = Field(
        default="stock_ts",
        description="InfluxDB bucket 名（K 线时序）",
    )
    kline_storage: str = Field(
        default="influxdb",
        description="K 线存储后端，逗号分隔：influxdb,mongodb,jsonl（KLINE_STORAGE）",
    )
    stock_company_storage: str = Field(
        default="mongodb",
        description="个股公司资料存储：mongodb | jsonl | mongodb,jsonl（STOCK_COMPANY_STORAGE）",
    )
    stock_company_cache_hours: float = Field(
        default=24.0,
        ge=0.0,
        description="本地公司资料缓存有效期（小时），过期后重新拉取 AKShare（STOCK_COMPANY_CACHE_HOURS）",
    )
    stock_search_cache_hours: float = Field(
        default=24.0,
        ge=0.0,
        description="A 股检索索引缓存有效期（小时）（STOCK_SEARCH_CACHE_HOURS）",
    )
    stock_hk_enabled: bool = Field(
        default=True,
        description="是否启用港股检索、K 线与公司详情（STOCK_HK_ENABLED）",
    )
    stock_us_enabled: bool = Field(
        default=True,
        description="是否启用美股检索、K 线与公司详情（STOCK_US_ENABLED）",
    )
    feishu_webhook_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FEISHU_WEBHOOK_URL", "LARK_WEBHOOK_URL"),
        description="飞书群机器人 Webhook URL",
    )
    feishu_webhook_secret: Optional[str] = Field(
        default=None,
        description="飞书 Webhook 签名校验密钥（FEISHU_WEBHOOK_SECRET）",
    )
    feishu_bot_name: str = Field(
        default="工程大模型平台机器人",
        description="飞书消息卡片/文本中展示的机器人名称（FEISHU_BOT_NAME）",
    )
    feishu_app_id: Optional[str] = Field(
        default=None,
        description="飞书开放平台 App ID（FEISHU_APP_ID），用于应用机器人私信",
    )
    feishu_app_secret: Optional[str] = Field(
        default=None,
        description="飞书开放平台 App Secret（FEISHU_APP_SECRET）",
    )
    feishu_receive_user: Optional[str] = Field(
        default=None,
        description="私信收件人 user_id，如 li_xf10（FEISHU_RECEIVE_USER）",
    )
    feishu_receive_users: Optional[str] = Field(
        default=None,
        description="私信收件人 user_id 列表，逗号分隔（FEISHU_RECEIVE_USERS）",
    )
    feishu_user_open_id: Optional[str] = Field(
        default=None,
        description="私信收件人 open_id，可跳过通讯录查询（FEISHU_USER_OPEN_ID）",
    )
    feishu_receive_email: Optional[str] = Field(
        default=None,
        description="私信收件人邮箱，用于 batch_get_id 解析 open_id（FEISHU_RECEIVE_EMAIL）",
    )
    feishu_bot_display_name: str = Field(
        default="工程大模型平台机器人",
        description="文档用：飞书管理后台应用机器人显示名（FEISHU_BOT_DISPLAY_NAME）",
    )
    bim_brief_enabled: bool = Field(
        default=True,
        description="是否启用 BIM 日报飞书推送（BIM_BRIEF_ENABLED）",
    )
    bim_brief_cron: str = Field(
        default="0 8 * * 0-4",
        description="BIM 日报推送 cron 五段式，默认周一至周五 08:00（BIM_BRIEF_CRON）",
    )
    bim_weekly_brief_enabled: bool = Field(
        default=True,
        description="是否启用 BIM 周报飞书推送（BIM_WEEKLY_BRIEF_ENABLED）",
    )
    bim_weekly_brief_cron: str = Field(
        default="0 18 * * 4",
        description="BIM 周报推送 cron 五段式，默认周五 18:00（APScheduler 周字段 4=周五）",
    )
    dingtalk_webhook_url: Optional[str] = Field(
        default=None,
        description="钉钉群机器人 Webhook URL（DINGTALK_WEBHOOK_URL）",
    )
    dingtalk_secret: Optional[str] = Field(
        default=None,
        description="钉钉 Webhook 加签密钥（DINGTALK_SECRET）",
    )
    wechat_webhook_url: Optional[str] = Field(
        default=None,
        description="企业微信群机器人 Webhook URL（WECHAT_WEBHOOK_URL）",
    )
    smtp_host: Optional[str] = Field(default=None, description="SMTP 服务器（SMTP_HOST）")
    smtp_port: int = Field(default=587, description="SMTP 端口（SMTP_PORT）")
    smtp_user: Optional[str] = Field(default=None, description="SMTP 用户名（SMTP_USER）")
    smtp_password: Optional[str] = Field(default=None, description="SMTP 密码（SMTP_PASSWORD）")
    smtp_from: Optional[str] = Field(default=None, description="发件人地址（SMTP_FROM）")
    smtp_use_tls: bool = Field(default=True, description="SMTP 是否启用 STARTTLS（SMTP_USE_TLS）")
    notification_email_to: Optional[str] = Field(
        default=None,
        description="默认邮件收件人，逗号分隔（NOTIFICATION_EMAIL_TO）",
    )
    sms_provider: str = Field(
        default="http",
        description="短信提供方标识（SMS_PROVIDER），当前为通用 HTTP API",
    )
    sms_api_url: Optional[str] = Field(
        default=None,
        description="短信 HTTP API 地址（SMS_API_URL）",
    )
    sms_api_key: Optional[str] = Field(
        default=None,
        description="短信 API 密钥（SMS_API_KEY）",
    )
    notification_sms_to: Optional[str] = Field(
        default=None,
        description="默认短信收件人手机号，逗号分隔（NOTIFICATION_SMS_TO）",
    )
    public_base_url: str = Field(
        default="http://127.0.0.1:8090",
        description="Web 对外访问基址，用于简报详情链接（PUBLIC_BASE_URL）",
    )
    agri_ui_url: str = Field(
        default="http://127.0.0.1:8091",
        validation_alias=AliasChoices("AGRI_UI_URL", "BIM_UI_URL"),
        description="商机洞察独立前端基址（AGRI_UI_URL，兼容旧 BIM_UI_URL）",
    )


def get_settings() -> Settings:
    return Settings()
