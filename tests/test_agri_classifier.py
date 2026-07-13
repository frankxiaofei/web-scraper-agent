"""智慧农业分类与 Pipeline 集成单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.core.agri_classifier import (
    agri_relevance_score,
    agri_tags_for_tagged_documents,
    apply_agri_classification_to_notice,
    classify_agri_by_keywords,
    doc_is_agri_related,
    infer_agri_application_scenes,
    infer_agri_tech_types,
    is_agri_related_by_keywords,
    matched_agri_keywords,
)
from src.core.models import BidNotice
from src.core.pipeline import Pipeline


def _notice(**kwargs) -> BidNotice:
    base = {
        "title": "测试公告",
        "url": "https://example.com/n1",
        "source_site_id": "crec_bidding",
        "source_site_name": "测试站",
        "source_url": "https://example.com/",
    }
    base.update(kwargs)
    return BidNotice(**base)


def test_matched_agri_keywords():
    kws = matched_agri_keywords({"title": "智慧农业物联网平台采购"})
    assert "智慧农业" in kws
    assert "农业物联网" in kws


def test_is_agri_related_by_keywords():
    assert is_agri_related_by_keywords({"title": "数字农业大数据平台"})
    assert not is_agri_related_by_keywords({"title": "普通钢材采购"})


def test_doc_is_agri_related_prefers_field():
    assert doc_is_agri_related({"title": "钢材", "is_agri_related": True})
    assert not doc_is_agri_related({"title": "智慧农业", "is_agri_related": False})


def test_classify_agri_by_keywords_positive():
    result = classify_agri_by_keywords("智慧大棚控制系统", "采购农业传感器")
    assert result.is_agri_related is True
    assert result.tags
    assert result.confidence is not None


def test_classify_agri_by_keywords_negative():
    result = classify_agri_by_keywords("办公桌椅采购", "普通家具")
    assert result.is_agri_related is False


def test_apply_agri_classification_to_notice():
    notice = _notice(title="乡村振兴数字化项目", content_text="农机智能化升级")
    apply_agri_classification_to_notice(notice)
    assert notice.is_agri_related is True
    assert notice.agri_tags
    assert notice.agri_classified_at is not None


def test_apply_agri_skips_when_already_classified():
    notice = _notice(title="智慧农业", is_agri_related=False)
    apply_agri_classification_to_notice(notice)
    assert notice.is_agri_related is False
    assert notice.agri_tags is None


def test_agri_tags_for_tagged_documents_dedupes():
    notice = _notice(agri_tags=["智慧农业", "智慧农业", "农机"])
    tags = agri_tags_for_tagged_documents(notice)
    assert tags[0] == "agri"
    assert tags.count("智慧农业") == 1
    assert "农机" in tags


def test_pipeline_syncs_agri_notice_to_tagged_documents(tmp_path):
    notice = _notice(title="农业物联网监测平台", content_text="传感器部署")
    mock_repo = MagicMock()
    mock_repo.available = True
    mock_repo.upsert_tagged_document.return_value = {"ok": True, "saved": True}

    with patch("src.core.pipeline.create_mongo_repository", return_value=mock_repo):
        pipeline = Pipeline(data_dir=tmp_path)
    pipeline._mongo_repo = mock_repo

    apply_agri_classification_to_notice(notice)
    pipeline._sync_agri_notice_to_tagged_documents(notice, agri_tags_for_tagged_documents)

    mock_repo.upsert_tagged_document.assert_called_once()
    call_kwargs = mock_repo.upsert_tagged_document.call_args.kwargs
    assert "agri" in call_kwargs["tags"]
    assert call_kwargs["source"] == "pipeline_agri_classify"


def test_pipeline_process_applies_agri_classification(tmp_path):
    notice = _notice(title="智慧农场管理平台", content_text="数字农业")

    with patch("src.core.pipeline.create_mongo_repository", return_value=None):
        pipeline = Pipeline(data_dir=tmp_path)
    pipeline._mongo_repo = None

    from src.core.models import ScrapeResult

    result = ScrapeResult(site_id="crec_bidding", success=True, notices=[notice])
    processed = pipeline.process(result)
    stored = processed.all_for_storage[0]
    assert stored.is_agri_related is True
    assert stored.agri_tags


def test_infer_agri_tech_and_scenes():
    doc = {"title": "农业物联网与遥感监测平台", "content_text": "智慧种植、无人机巡检"}
    tech = infer_agri_tech_types(doc)
    scenes = infer_agri_application_scenes(doc)
    assert "物联网" in tech
    assert "遥感" in tech
    assert "种植" in scenes


def test_agri_relevance_score():
    assert agri_relevance_score({"title": "智慧农业", "is_agri_related": True}) >= 0.5
    assert agri_relevance_score({"title": "钢材", "is_agri_related": False}) == 0.0


def test_pipeline_skips_tagged_sync_for_non_agri(tmp_path):
    notice = _notice(title="普通钢材采购", content_text="无农业内容")
    mock_repo = MagicMock()
    mock_repo.available = True

    with patch("src.core.pipeline.create_mongo_repository", return_value=mock_repo):
        pipeline = Pipeline(data_dir=tmp_path)
    pipeline._mongo_repo = mock_repo

    apply_agri_classification_to_notice(notice)
    pipeline._sync_agri_notice_to_tagged_documents(notice, agri_tags_for_tagged_documents)
    mock_repo.upsert_tagged_document.assert_not_called()
