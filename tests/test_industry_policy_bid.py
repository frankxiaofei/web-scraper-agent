"""industry policy-bid 服务单元测试。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.industry_policy_bid_service import IndustryPolicyBidService


def test_policy_bid_lag_structure():
    mock_insights = MagicMock()
    mock_insights.load_domain_docs.return_value = [
        {
            "title": "数字农业平台招标",
            "content_text": "数字农业",
            "scraped_at": datetime.now().isoformat(),
            "budget_amount": "100万元",
            "region": "广东省",
        }
    ]
    svc = IndustryPolicyBidService(mock_insights)
    with patch("src.web.industry_policy_bid_service.get_policy_news_fetcher") as mock_fetcher:
        mock_fetcher.return_value.list_news.return_value = {
            "items": [
                {
                    "title": "数字农业政策",
                    "themes": ["数字农业"],
                    "published_at": (datetime.now() - timedelta(days=10)).isoformat(),
                }
            ]
        }
        result = svc.policy_bid_lag(domain="数字农业", lag_days=90, days=180)

    assert "themes" in result
    assert result["themes"][0]["theme"] == "数字农业"
    assert "lift_pct" in result["themes"][0]


def test_policy_bid_trend_weekly():
    mock_insights = MagicMock()
    mock_insights.load_domain_docs.return_value = []
    svc = IndustryPolicyBidService(mock_insights)
    with patch("src.web.industry_policy_bid_service.get_policy_news_fetcher") as mock_fetcher:
        mock_fetcher.return_value.list_news.return_value = {"items": []}
        result = svc.policy_bid_trend("数字农业", days=90, bucket="week")

    assert result["bucket"] == "week"
    assert "series" in result
