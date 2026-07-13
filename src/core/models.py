"""招标公告数据模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class NoticeCategory(str, Enum):
    """公告类型。"""

    TENDER = "招标公告"
    WIN = "中标公告"
    CHANGE = "变更公告"
    OTHER = "其他"


class PageType(str, Enum):
    """页面类型（递归爬取用）。"""

    LIST = "list"
    DETAIL = "detail"
    ATTACHMENT = "attachment"
    INDEX = "index"
    UNKNOWN = "unknown"


class BidNotice(BaseModel):
    """单条招标/采购公告。"""

    title: str = Field(..., description="公告标题")
    url: str = Field(..., description="详情页 URL")
    source_site_id: str = Field(..., description="来源站点 ID（sites.yaml 中的 id）")
    source_site_name: str = Field(..., description="来源站点名称")
    source_url: str = Field(..., description="列表页或站点首页 URL")

    publish_date: Optional[datetime] = Field(None, description="发布时间")
    region: Optional[str] = Field(None, description="地区")
    category: NoticeCategory = Field(default=NoticeCategory.TENDER, description="公告类型")
    purchaser: Optional[str] = Field(None, description="采购人/招标人")
    agency: Optional[str] = Field(None, description="代理机构")
    budget: Optional[str] = Field(None, description="预算金额")
    contract_amount: Optional[str] = Field(None, description="合同金额")
    budget_amount: Optional[str] = Field(None, description="预算/最高限价")
    tender_party: Optional[str] = Field(None, description="招标方/采购人")
    project_period: Optional[str] = Field(None, description="项目周期/工期/服务期限")
    project_location: Optional[str] = Field(None, description="建设/项目地点")
    qualification_requirements: Optional[str] = Field(None, description="资质/招标要求摘要")
    key_summary: Optional[str] = Field(None, description="招标关键内容摘要")
    bid_deadline: Optional[str] = Field(None, description="投标截止时间")
    open_time: Optional[str] = Field(None, description="开标时间")
    contact: Optional[str] = Field(None, description="联系人/电话")
    content_text: Optional[str] = Field(None, description="正文纯文本")
    content_html: Optional[str] = Field(None, description="正文 HTML")
    attachments: list[dict[str, Any]] = Field(
        default_factory=list, description="附件链接列表 [{url, name}]"
    )
    content_hash: Optional[str] = Field(None, description="内容指纹（标题+URL 等）")

    is_bim_related: Optional[bool] = Field(None, description="是否与 BIM 相关（True/False/None 未分类）")
    bim_confidence: Optional[float] = Field(None, description="BIM 判定置信度 0-1")
    bim_reason: Optional[str] = Field(None, description="BIM 判定理由")
    bim_tags: Optional[list[str]] = Field(None, description="BIM 相关标签，如 BIM建模、数字孪生")
    bim_classified_at: Optional[datetime] = Field(None, description="BIM 分类完成时间")

    is_agri_related: Optional[bool] = Field(None, description="是否与智慧农业相关（True/False/None 未分类）")
    agri_confidence: Optional[float] = Field(None, description="农业判定置信度 0-1")
    agri_reason: Optional[str] = Field(None, description="农业判定理由")
    agri_tags: Optional[list[str]] = Field(None, description="农业相关标签，如 农业物联网、智慧大棚")
    agri_classified_at: Optional[datetime] = Field(None, description="农业分类完成时间")

    tags: Optional[list[str]] = Field(None, description="通用检索标签，如 微信公众号")

    # 递归爬取元数据
    parent_url: Optional[str] = Field(None, description="父页面 URL")
    depth: int = Field(default=0, description="爬取深度（0=列表项，1=详情，2=附件/子链接）")
    page_type: Optional[PageType] = Field(None, description="页面类型")
    needs_recursion: bool = Field(default=False, description="是否仍需继续递归")

    scraped_at: datetime = Field(default_factory=datetime.now, description="抓取时间")

    model_config = {"str_strip_whitespace": True}


class ScrapeResult(BaseModel):
    """单次抓取结果。"""

    site_id: str
    success: bool
    notices: list[BidNotice] = Field(default_factory=list)
    error: Optional[str] = None
    duration_seconds: float = 0.0
