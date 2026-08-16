"""EntitlementService 单元测试。"""

from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.billing.entitlements import EntitlementService


def test_feature_gate_free_heatmap_denied():
    session = MagicMock()
    tenant_id = uuid.uuid4()
    svc = EntitlementService(session)
    with patch.object(svc, "get_plan_id", return_value="free"):
        with patch.object(svc.usage, "sum_metric", return_value=Decimal("0")):
            result = svc.check(tenant_id, "feature.industry_heatmap")
    assert result.allowed is False
    assert result.plan_id == "free"


def test_feature_gate_pro_heatmap_allowed():
    session = MagicMock()
    tenant_id = uuid.uuid4()
    svc = EntitlementService(session)
    with patch.object(svc, "get_plan_id", return_value="pro"):
        with patch.object(svc.usage, "sum_metric", return_value=Decimal("0")):
            result = svc.check(tenant_id, "feature.industry_heatmap")
    assert result.allowed is True


def test_quota_unlimited_enterprise():
    session = MagicMock()
    tenant_id = uuid.uuid4()
    svc = EntitlementService(session)
    with patch.object(svc, "get_plan_id", return_value="enterprise"):
        with patch.object(svc.usage, "sum_metric", return_value=Decimal("999")):
            result = svc.check(tenant_id, "quota.crawl_runs", delta=1)
    assert result.allowed is True
    assert result.limit is None
