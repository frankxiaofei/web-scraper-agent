"""policy_insights_service 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.policy_insights_service import PolicyInsightsService


def test_generate_investment_direction_fallback():
    svc = PolicyInsightsService()
    with patch.object(svc, "_llm_available", return_value=False):
        result = svc.generate_investment_direction(days=30, use_llm=True)
    report = result.get("report") or {}
    assert report.get("summary")
    assert report.get("sectors")
    assert report.get("disclaimer")
    assert "不构成" in report.get("disclaimer", "")


def test_get_insights_structure():
    svc = PolicyInsightsService()
    with patch.object(svc, "_llm_available", return_value=False):
        result = svc.get_insights(days=30, use_llm=False)
    assert "policy_count" in result
    assert "fifteenth_fyp_note" in result
    assert result.get("disclaimer")
