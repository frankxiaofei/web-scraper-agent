"""公告正文 HTML 展示预处理单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.notice_html import prepare_notice_html_for_display


def test_unwraps_full_html_document():
    raw = (
        '<html><head><style>.x{color:red}</style></head>'
        '<body style="background:#fff"><p>正文</p></body></html>'
    )
    out = prepare_notice_html_for_display(raw)
    assert "<html" not in out.lower()
    assert "<head" not in out.lower()
    assert "<body" not in out.lower()
    assert "<style" not in out.lower()
    assert "<p>正文</p>" in out


def test_strips_script_and_iframe():
    raw = '<div>前<script>alert(1)</script><iframe src="/x"></iframe>后</div>'
    out = prepare_notice_html_for_display(raw)
    assert "script" not in out.lower()
    assert "iframe" not in out.lower()
    assert "前" in out and "后" in out


def test_preserves_table_markup():
    raw = "<table><tr><td style='width:800px'>单元格</td></tr></table>"
    out = prepare_notice_html_for_display(raw)
    assert "<table>" in out
    assert "单元格" in out
    assert "width:800px" in out


def test_wechat_rewrites_inline_images_from_attachments():
    raw = (
        '<div id="js_content" style="visibility: hidden;">'
        '<img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/x/640?wx_fmt=jpeg&amp;from=appmsg" />'
        "</div>"
    )
    attachments = [
        {
            "url": "https://mmbiz.qpic.cn/mmbiz_jpg/x/640?wx_fmt=jpeg&from=appmsg",
            "type": "image",
            "local_path": "data/wechat_lpfb/images/slug/1.jpg",
        }
    ]
    out = prepare_notice_html_for_display(raw, attachments=attachments, is_wechat=True)
    assert 'src="/api/wechat-assets/wechat_lpfb/slug/1.jpg"' in out
    assert "visibility: hidden" not in out
