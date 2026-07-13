"""微信公众号爬取与适配器测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.adapters.wechat import WechatAdapter
from src.core.models import BidNotice, NoticeCategory
from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.site_sync import should_fetch_detail
from src.core.wechat_crawl import (
    accounts_match,
    article_belongs_to_account,
    clean_url,
    download_wechat_images,
    extract_article,
    extract_article_account,
    extract_biz_from_url,
    extract_image_urls,
    parse_publish_datetime,
    resolve_wechat_nickname,
    rewrite_wechat_content_images,
)


SAMPLE_HTML = """
<html><head>
<meta property="og:title" content="测试文章标题" />
</head><body>
<script>var ct = "1704067200"; var biz = "MzkzNTMwNTQ0Mw=="; var nickname = "临平发布";</script>
<div id="js_name">临平发布</div>
<div id="js_content">
<p>这是正文内容段落。</p>
<img data-src="https://mmbiz.qpic.cn/mmbiz_png/test/640?wx_fmt=png" />
<img src="https://mmbiz.qpic.cn/mmbiz_jpg/test2/640?wx_fmt=jpeg" />
</div>
</body></html>
"""


def test_clean_url_strips_fragment():
    url = "https://mp.weixin.qq.com/s/abc123#wechat_redirect"
    assert clean_url(url) == "https://mp.weixin.qq.com/s/abc123"


def test_clean_url_preserves_biz_params():
    url = "https://mp.weixin.qq.com/s?__biz=MzkzNTMwNTQ0Mw==&mid=1&sn=abc"
    assert "__biz=" in clean_url(url)


def test_resolve_wechat_nickname_from_config():
    site = {"wechat_nickname": "临平发布", "name": "其他名称"}
    assert resolve_wechat_nickname(site) == "临平发布"


def test_resolve_wechat_nickname_from_name():
    site = {"name": "临平发布（微信公众号）"}
    assert resolve_wechat_nickname(site) == "临平发布"


def test_parse_publish_datetime():
    dt = parse_publish_datetime("2024-01-01 12:00:00")
    assert dt is not None
    assert dt.year == 2024
    assert dt.month == 1


@patch("src.core.wechat_crawl.requests.get")
def test_extract_article(mock_get):
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_HTML
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    article = extract_article("https://mp.weixin.qq.com/s/test123", download_images=False)
    assert article["title"] == "测试文章标题"
    assert "正文内容" in article["content"]
    assert article["publish_time"].startswith("2024")
    assert "js_content" in article["content_html"]
    assert len(article["image_urls"]) == 2
    assert "mmbiz.qpic.cn" in article["image_urls"][0]
    assert article["author"] == "临平发布"
    assert article["biz"] == "MzkzNTMwNTQ0Mw=="


def test_extract_article_account_from_dom():
    from bs4 import BeautifulSoup

    author, biz = extract_article_account(SAMPLE_HTML, BeautifulSoup(SAMPLE_HTML, "html.parser"))
    assert author == "临平发布"
    assert biz == "MzkzNTMwNTQ0Mw=="


def test_accounts_match_normalizes_spaces():
    assert accounts_match("临平发布", "临平发布") is True
    assert accounts_match("临 平 发布", "临平发布") is True
    assert accounts_match("中国电力报", "国家能源局") is False


def test_article_belongs_to_account_rejects_wrong_biz():
    assert article_belongs_to_account(
        author="临平发布",
        biz="MzkzNTMwNTQ0Mw==",
        url="https://mp.weixin.qq.com/s?__biz=MzkzNTMwNTQ0Mw==",
        expected_nickname="临平发布",
        expected_biz="MzkzNTMwNTQ0Mw==",
    )
    assert not article_belongs_to_account(
        author="其他公众号",
        biz="MzUzOTkzNDA0Ng==",
        url="https://mp.weixin.qq.com/s?__biz=MzUzOTkzNDA0Ng==",
        expected_nickname="临平发布",
        expected_biz="MzkzNTMwNTQ0Mw==",
    )


def test_extract_biz_from_url():
    url = "https://mp.weixin.qq.com/s?__biz=MzkzNTMwNTQ0Mw==&mid=1&sn=abc"
    assert extract_biz_from_url(url) == "MzkzNTMwNTQ0Mw=="


@patch("src.core.wechat_crawl.requests.get")
def test_extract_article_rewrites_img_src_inline(mock_get, tmp_path, monkeypatch):
    import src.core.wechat_crawl as wc

    monkeypatch.setattr(wc, "ROOT", tmp_path)
    mock_resp = MagicMock()
    mock_resp.text = SAMPLE_HTML
    mock_resp.raise_for_status = MagicMock()

    img_resp = MagicMock()
    img_resp.content = b"\x89PNG\r\n"
    img_resp.headers = {"Content-Type": "image/png"}
    img_resp.raise_for_status = MagicMock()

    def side_effect(url, **kwargs):
        if "mmbiz.qpic.cn" in url:
            return img_resp
        return mock_resp

    mock_get.side_effect = side_effect

    article = extract_article(
        "https://mp.weixin.qq.com/s/test123",
        site_id="wechat_lpfb",
        download_images=True,
    )
    html = article["content_html"]
    assert 'src="/api/wechat-assets/wechat_lpfb/' in html
    assert "data-src=" not in html or 'src="/api/wechat-assets/wechat_lpfb/' in html
    assert "visibility: hidden" not in html


def test_rewrite_wechat_content_images_uses_local_urls():
    html = (
        '<div id="js_content" style="visibility: hidden; opacity: 0;">'
        '<img data-src="https://mmbiz.qpic.cn/mmbiz_png/test/640?wx_fmt=png" />'
        "</div>"
    )
    attachments = [
        {
            "url": "https://mmbiz.qpic.cn/mmbiz_png/test/640?wx_fmt=png",
            "name": "image_1",
            "type": "image",
            "local_path": "data/wechat_lpfb/images/abc123/1.png",
        }
    ]
    out = rewrite_wechat_content_images(html, attachments)
    assert 'src="/api/wechat-assets/wechat_lpfb/abc123/1.png"' in out
    assert "visibility: hidden" not in out
    assert "opacity: 0" not in out


def test_extract_image_urls_prefers_data_src():
    from bs4 import BeautifulSoup

    html = '<img data-src="https://mmbiz.qpic.cn/a.png" src="https://placeholder.com/x.png" />'
    soup = BeautifulSoup(html, "html.parser")
    urls = extract_image_urls(soup)
    assert urls == ["https://mmbiz.qpic.cn/a.png"]


@patch("src.core.wechat_crawl.requests.get")
def test_download_wechat_images(mock_get, tmp_path, monkeypatch):
    import src.core.wechat_crawl as wc

    monkeypatch.setattr(wc, "ROOT", tmp_path)
    mock_resp = MagicMock()
    mock_resp.content = b"\x89PNG\r\n"
    mock_resp.headers = {"Content-Type": "image/png"}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    attachments = download_wechat_images(
        ["https://mmbiz.qpic.cn/mmbiz_png/test/640?wx_fmt=png"],
        site_id="wechat_lpfb",
        article_url="https://mp.weixin.qq.com/s/test123",
    )
    assert len(attachments) == 1
    assert attachments[0]["type"] == "image"
    assert attachments[0]["local_path"].startswith("data/wechat_lpfb/images/")
    assert (tmp_path / attachments[0]["local_path"]).exists()


def test_wechat_adapter_registered():
    from src.core.scheduler import get_adapter_class

    cls = get_adapter_class("wechat")
    assert cls.__name__ == "WechatAdapter"


def test_wechat_site_eligible_for_schedule():
    site = {"id": "wechat_lpfb", "adapter": "wechat", "enabled": True}
    assert site_eligible_for_schedule(site) is True


def test_wechat_should_not_fetch_detail_separately():
    site = {"id": "wechat_lpfb", "adapter": "wechat", "fetch_detail": True}
    assert should_fetch_detail(site) is False


@pytest.mark.asyncio
@patch("src.adapters.wechat.asyncio.to_thread")
async def test_wechat_adapter_fetch_list(mock_to_thread):
    mock_to_thread.return_value = [
        {
            "title": "临平新闻",
            "url": "https://mp.weixin.qq.com/s/abc",
            "publish_time": "2024-06-01 10:00:00",
            "content": "新闻正文",
            "content_html": "<p>新闻正文</p>",
            "attachments": [{"url": "https://mmbiz.qpic.cn/x.png", "name": "image_1", "type": "image"}],
        }
    ]
    site_config = {
        "id": "wechat_lpfb",
        "name": "临平发布（微信公众号）",
        "url": "https://mp.weixin.qq.com/s/seed",
        "wechat_nickname": "临平发布",
        "region": "浙江杭州",
    }
    adapter = WechatAdapter(site_config, MagicMock())
    notices = await adapter.fetch_list(max_items=5)

    assert len(notices) == 1
    assert notices[0].title == "临平新闻"
    assert notices[0].content_text == "新闻正文"
    assert notices[0].content_html == "<p>新闻正文</p>"
    assert len(notices[0].attachments) == 1
    assert notices[0].category == NoticeCategory.OTHER
    assert notices[0].source_site_id == "wechat_lpfb"


@pytest.mark.asyncio
async def test_wechat_adapter_fetch_detail_passthrough():
    site_config = {"id": "wechat_lpfb", "name": "临平发布", "url": "https://example.com"}
    adapter = WechatAdapter(site_config, MagicMock())
    notice = BidNotice(
        title="t",
        url="https://mp.weixin.qq.com/s/x",
        source_site_id="wechat_lpfb",
        source_site_name="临平发布",
        source_url="https://example.com",
        content_text="已有正文",
    )
    result = await adapter.fetch_detail(notice)
    assert result.content_text == "已有正文"
