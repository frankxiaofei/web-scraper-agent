"""Hermes supply_chain tool 测试（P2-T-009）。"""

from __future__ import annotations

from unittest.mock import patch

from src.web.crawl_agent_tools import CrawlAgentToolExecutor


def test_skill_supply_chain_graph_table_mode():
    executor = CrawlAgentToolExecutor()
    with patch("src.web.industry_insights_service.get_industry_insights_service") as mock_svc:
        mock_svc.return_value.supply_chain_table.return_value = {
            "rows": [{"purchaser": "A", "supplier": "B"}],
            "total": 1,
        }
        result, summary = executor.execute(
            "skill_supply_chain_graph",
            {"mode": "table", "industry_code": "domain:数字农业", "days": 30},
        )
    assert result["ok"] is True
    assert result["mode"] == "table"
    assert "rows" in result


def test_skill_supply_chain_graph_neo4j_mode():
    executor = CrawlAgentToolExecutor()
    with patch("src.industry.graph.neo4j_sync.get_neo4j_graph_service") as mock_graph:
        mock_graph.return_value.query_subgraph.return_value = {
            "elements": {"nodes": [], "edges": []},
            "source": "unconfigured",
        }
        result, _ = executor.execute(
            "skill_supply_chain_graph",
            {"mode": "graph", "depth": 2},
        )
    assert result["ok"] is True
    assert result["view_url"] == "/insights/supply-chain"
