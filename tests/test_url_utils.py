"""公告 URL 工具函数测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.url_utils import (
    is_browsable_http_url,
    is_likely_detail_url,
    resolve_notice_detail_url,
)


def test_resolve_chdtp_javascript_url():
    raw = "javascript:toGetContent('zhaobiaogg/2026/07/06/zhaobiaogg_3884137_38293.html')"
    resolved = resolve_notice_detail_url(raw)
    assert resolved == (
        "https://www.chdtp.com/staticPage/"
        "zhaobiaogg/2026/07/06/zhaobiaogg_3884137_38293.html"
    )
    assert is_browsable_http_url(resolved)
    assert is_likely_detail_url(resolved)


def test_resolve_keeps_http_url():
    url = "https://www.dlzb.com/d-zb-67447122.html"
    assert resolve_notice_detail_url(url) == url
