"""powerchina_notice 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.powerchina_notice import (
    build_detail_url,
    build_pdf_download_url,
    extract_notice_id,
    extract_row_notice_id,
    is_search_placeholder_url,
    is_valid_detail_url,
    load_bim_cache_index,
    normalize_title,
    parse_get_info_content,
    resolve_id_from_index,
    resolve_picture_url,
    title_search_keywords,
    titles_match,
)


def test_normalize_title():
    assert normalize_title("中国 电建  BIM") == "中国电建BIM"
    assert normalize_title("BIM技术服务采购采购项目") == "BIM技术服务采购项目"
    assert normalize_title("KY2024-ZD-08zx面向重大复杂工程BIM") == "面向重大复杂工程BIM"


def test_normalize_title_collapses_duplicate_phrase():
    raw = (
        "富阳区南北渠分洪隧洞工程一期建设期信息化项目"
        "富阳区南北渠分洪隧洞工程一期建设期信息化项目BIM技术服务"
    )
    assert "富阳区南北渠" in normalize_title(raw)
    assert normalize_title(raw).count("富阳区南北渠") == 1


def test_resolve_id_from_index_fuzzy():
    index = {
        normalize_title(
            "中国电建华东院富阳区南北渠分洪隧洞工程（一期）建设期信息化项目"
            "富阳区南北渠分洪隧洞工程（一期）建设期信息化项目BIM技术服务采购采购项目成交结果公示"
        ): "2409449609",
        normalize_title(
            "中国电建电建建筑公司容东片区1号地块项目XARD0040-0049宗地设计施工总承包三标"
            "-XARD0044-3#宗地酒店地块项目雄安智汇城三标段44-3酒店项目"
            "BIM咨询服务采购计划采购项目中标候选人公示"
        ): "2409435670",
    }
    query = (
        "中国电建华东院富阳区南北渠分洪隧洞工程（一期）建设期信息化项目"
        "BIM技术服务采购采购项目 成交结果公示"
    )
    assert resolve_id_from_index(query, index) == "2409449609"

    short_query = "中国电建电建建筑公司容东片区 1 号地块项目BIM咨询服务采购计划采购项目中标候选人公示"
    assert resolve_id_from_index(short_query, index) == "2409435670"


def test_is_search_placeholder_url():
    assert is_search_placeholder_url("https://bid.powerchina.cn/search?q=BIM#1") is True
    assert is_search_placeholder_url(
        "https://bid.powerchina.cn/notice/detail?id=2409467417"
    ) is False


def test_is_valid_detail_url():
    assert is_valid_detail_url("https://bid.powerchina.cn/search?q=BIM#1") is False
    assert is_valid_detail_url(
        "https://bid.powerchina.cn/notice/detail?id=2409467417"
    ) is True


def test_extract_notice_id():
    url = (
        "https://bid.powerchina.cn/notice/detail?id=2409467417"
        "&type=招采公告&typeName=招采公告"
    )
    assert extract_notice_id(url) == "2409467417"


def test_extract_row_notice_id():
    assert extract_row_notice_id({"id": 2409467417}) == "2409467417"
    assert extract_row_notice_id({"noticeId": "2409468004"}) == "2409468004"
    assert extract_row_notice_id({"rowid": 123}) == "123"
    assert extract_row_notice_id({"title": "x"}) is None


def test_build_detail_url():
    url = build_detail_url("2409467417")
    assert "id=2409467417" in url
    assert url.startswith("https://bid.powerchina.cn/notice/detail")


def test_extract_publish_time_helpers():
    from src.core.powerchina_notice import (
        extract_publish_time_from_get_info,
        extract_publish_time_from_row,
    )

    row = {"publishDate": "2026-06-10T14:50:01", "id": "1"}
    assert extract_publish_time_from_row(row) is not None
    info = {"publishTime": "2026-06-17 10:25:01"}
    assert extract_publish_time_from_get_info(info).day == 17


def test_parse_get_info_content():
    data = {
        "title": "测试公告",
        "announcementContent": "<p>正文<strong>内容</strong></p>",
    }
    title, text, html = parse_get_info_content(data)
    assert title == "测试公告"
    assert "正文" in text
    assert "<p>" in html


def test_resolve_picture_url_from_system_id():
    system_id = "c3ff94635c2c48ccaa50517bf193af97"
    data = {
        "title": "PDF 公示",
        "systemId": system_id,
        "announcementContent": "",
    }
    picture_url, biz_id = resolve_picture_url(data)
    assert biz_id == system_id
    assert picture_url is not None
    assert system_id in picture_url
    assert "fileType=4" in picture_url


def test_resolve_picture_url_prefers_direct_url():
    direct = (
        "https://bid-zb.powerchina.cn/bidprocurement/common-tools/tools/commonUpload/"
        "bulletinDownLoadFile?bizId=abc123&fileType=2"
    )
    data = {"pictureUrl": direct, "systemId": "other"}
    picture_url, biz_id = resolve_picture_url(data)
    assert picture_url == direct
    assert biz_id == "abc123"


def test_build_pdf_download_url():
    url = build_pdf_download_url("abc123", file_type="4")
    assert "bizId=abc123" in url
    assert "fileType=4" in url


def test_load_bim_cache_index():
    cache = ROOT / "data" / "generated_scripts" / "powerchina_bim_all.json"
    if not cache.is_file():
        return
    index = load_bim_cache_index(cache)
    assert len(index) >= 60
    sample = next(iter(index.values()))
    assert sample.isdigit()


def test_titles_match_exact():
    title = "中国电建贵州院基于数字模型的输电线路机械化施工方案智能决策分析研究项目招标公告"
    assert titles_match(title, title) is True


def test_titles_match_prefix():
    left = "中国电建贵州院基于数字模型的输电线路机械化施工方案智能决策分析研究项目"
    right = left + "招标公告"
    assert titles_match(left, right) is True


def test_titles_match_rejects_unrelated():
    assert titles_match(
        "中国电建水电七局苏州市BIM项目",
        "中国电建水电十二局太阳沟抽水蓄能电站项目",
    ) is False


def test_title_search_keywords():
    keywords = title_search_keywords(
        "中国电建中南院湖南辰溪抽水蓄能电站数智化电站项目枢纽布置BIM模型采购项目公开询比采购公告"
    )
    assert any("BIM" in kw.upper() for kw in keywords)
