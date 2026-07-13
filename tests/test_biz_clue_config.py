"""商机配置加载单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.biz_clue_config import (
    clear_config_cache,
    compute_next_check_date,
    get_biz_clue_sync_site_ids,
    get_config_for_api,
    get_config_version,
    get_product_lines,
    get_scoring_levels,
    get_stages,
    load_biz_clue_config,
    load_biz_clue_keywords,
)


def setup_function() -> None:
    clear_config_cache()


def test_load_biz_clue_config_has_required_sections():
    clear_config_cache()
    cfg = load_biz_clue_config()
    assert cfg.get("version")
    assert cfg.get("product_lines")
    assert cfg.get("stages")
    assert cfg.get("scoring")
    assert cfg.get("validity_rules")
    assert cfg.get("dedup_rules")


def test_product_lines_three_categories():
    lines = get_product_lines()
    assert "农业平台" in lines
    assert "田间作业" in lines
    assert "AI能力" in lines


def test_stages_have_check_days():
    stages = get_stages()
    assert len(stages) >= 6
    names = {s["name"] for s in stages}
    assert "采购前置" in names
    assert "正式采购" in names
    for stage in stages:
        assert int(stage.get("check_days") or 0) > 0


def test_scoring_levels_from_config():
    levels = get_scoring_levels()
    assert levels["S"] == 85
    assert levels["A"] == 70
    assert levels["B"] == 50


def test_compute_next_check_date():
    date_str = compute_next_check_date("采购前置")
    assert len(date_str) == 10
    assert date_str[4] == "-"


def test_biz_clue_sync_site_ids():
    ids = get_biz_clue_sync_site_ids()
    assert "ccgp_national" in ids
    assert "ggzy_national" in ids


def test_load_biz_clue_keywords_compat():
    kw = load_biz_clue_keywords()
    assert kw.get("product_lines")
    assert kw.get("stages")
    assert kw.get("exclude_keywords")


def test_get_config_for_api():
    api_cfg = get_config_for_api()
    assert api_cfg["version"] == get_config_version()
    assert "schedule" in api_cfg
    assert "sources" in api_cfg
    assert "prompt_preview" in api_cfg
