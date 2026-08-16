"""Neo4j sync & subgraph query (Phase 2 P2-T-003 / P2-T-006)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from src.core.config import get_settings
from src.industry.ontology.loader import ontology_version

logger = logging.getLogger(__name__)

_MAX_NODES = 200


class Neo4jGraphService:
    def __init__(self) -> None:
        self._driver = None
        self._init_attempted = False

    @property
    def configured(self) -> bool:
        settings = get_settings()
        return bool(settings.neo4j_uri and settings.neo4j_password)

    def _get_driver(self):
        if self._init_attempted:
            return self._driver
        self._init_attempted = True
        if not self.configured:
            return None
        try:
            from neo4j import GraphDatabase

            settings = get_settings()
            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            self._driver.verify_connectivity()
        except Exception:
            logger.warning("Neo4j driver unavailable", exc_info=True)
            self._driver = None
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def merge_company(
        self,
        *,
        company_id: UUID | str,
        canonical_name: str,
        country_iso: str = "CN",
        **props: Any,
    ) -> None:
        driver = self._get_driver()
        if not driver:
            return
        cypher = """
        MERGE (c:Company {company_id: $company_id})
        SET c.canonical_name = $canonical_name,
            c.country_iso = $country_iso,
            c._ontology_version = $version
        """
        with driver.session() as session:
            session.run(
                cypher,
                company_id=str(company_id),
                canonical_name=canonical_name,
                country_iso=country_iso,
                version=ontology_version(),
                **props,
            )

    def merge_industry(
        self,
        *,
        code: str,
        taxonomy: str,
        name: str,
        level: int = 1,
    ) -> None:
        driver = self._get_driver()
        if not driver:
            return
        cypher = """
        MERGE (i:Industry {code: $code, taxonomy: $taxonomy})
        SET i.name = $name, i.level = $level, i._ontology_version = $version
        """
        with driver.session() as session:
            session.run(
                cypher,
                code=code,
                taxonomy=taxonomy,
                name=name,
                level=level,
                version=ontology_version(),
            )

    def merge_awarded_to(
        self,
        *,
        contract_id: str,
        winner_company_id: str,
        issuer_company_id: str | None = None,
        notice_url: str | None = None,
    ) -> None:
        driver = self._get_driver()
        if not driver:
            return
        cypher = """
        MERGE (c:Contract {contract_id: $contract_id})
        SET c.notice_url = coalesce($notice_url, c.notice_url),
            c._ontology_version = $version
        WITH c
        MATCH (w:Company {company_id: $winner_id})
        MERGE (c)-[:AWARDED_TO {role: 'winner'}]->(w)
        """
        with driver.session() as session:
            session.run(
                cypher,
                contract_id=contract_id,
                winner_id=winner_company_id,
                notice_url=notice_url,
                version=ontology_version(),
            )
            if issuer_company_id:
                session.run(
                    """
                    MATCH (c:Contract {contract_id: $contract_id})
                    MATCH (i:Company {company_id: $issuer_id})
                    MERGE (c)-[:ISSUED_BY {role: 'issuer'}]->(i)
                    """,
                    contract_id=contract_id,
                    issuer_id=issuer_company_id,
                )

    def merge_classified_as(
        self,
        *,
        company_id: str,
        industry_code: str,
        taxonomy: str = "GB2017",
    ) -> None:
        driver = self._get_driver()
        if not driver:
            return
        cypher = """
        MATCH (c:Company {company_id: $company_id})
        MATCH (i:Industry {code: $code, taxonomy: $taxonomy})
        MERGE (c)-[:CLASSIFIED_AS]->(i)
        """
        with driver.session() as session:
            session.run(
                cypher,
                company_id=company_id,
                code=industry_code,
                taxonomy=taxonomy,
            )

    def merge_companies(
        self,
        *,
        source_ids: list[str],
        target_id: str,
    ) -> dict[str, Any]:
        """Neo4j 企业合并：重定向边并删除源节点（P2-T-008）。"""
        driver = self._get_driver()
        if not driver:
            return {"merged": [], "source": "unconfigured"}
        merged: list[str] = []
        with driver.session() as session:
            for sid in source_ids:
                if sid == target_id:
                    continue
                session.run(
                    """
                    MATCH (src:Company {company_id: $source})
                    MATCH (tgt:Company {company_id: $target})
                    WITH src, tgt
                    OPTIONAL MATCH (src)-[r]-(n)
                    WHERE NOT (n:Company AND n.company_id = $target)
                    WITH src, tgt, collect(distinct r) AS rels
                    FOREACH (rel IN rels |
                        FOREACH (_ IN CASE WHEN startNode(rel) = src THEN [1] ELSE [] END |
                            SET rel._tmp_redirect = tgt
                        )
                    )
                    DETACH DELETE src
                    """,
                    source=sid,
                    target=target_id,
                )
                merged.append(sid)
        return {"merged": merged, "target_id": target_id, "source": "neo4j"}

    def query_subgraph(
        self,
        *,
        center: str | None = None,
        depth: int = 2,
        industry_code: str | None = None,
        limit: int = _MAX_NODES,
    ) -> dict[str, Any]:
        """Return Cytoscape.js elements JSON."""
        driver = self._get_driver()
        if not driver:
            return {"elements": {"nodes": [], "edges": []}, "source": "unconfigured"}

        depth = max(1, min(depth, 4))
        limit = max(1, min(limit, _MAX_NODES))

        if center:
            cypher = f"""
            MATCH path = (start:Company {{company_id: $center}})-[*1..{depth}]-(n)
            WITH collect(distinct start) + collect(distinct n) AS nodes, relationships(path) AS rels
            UNWIND nodes AS node
            WITH collect(distinct node) AS uniq_nodes, rels
            UNWIND rels AS r
            WITH uniq_nodes, collect(distinct r) AS uniq_rels
            RETURN uniq_nodes[0..{limit}] AS nodes, uniq_rels[0..{limit}] AS rels
            """
            params: dict[str, Any] = {"center": center}
        elif industry_code:
            cypher = f"""
            MATCH (i:Industry {{code: $code}})<-[:CLASSIFIED_AS]-(c:Company)
            OPTIONAL MATCH (c)-[r]-(m)
            WITH collect(distinct c) + collect(distinct m) AS nodes, collect(distinct r) AS rels
            RETURN nodes[0..{limit}] AS nodes, rels[0..{limit}] AS rels
            """
            params = {"code": industry_code}
        else:
            cypher = f"""
            MATCH (c:Company)-[r]-(m)
            RETURN collect(distinct c)[0..{limit}] AS nodes, collect(distinct r)[0..{limit}] AS rels
            """
            params = {}

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        seen_edges: set[str] = set()

        with driver.session() as session:
            record = session.run(cypher, **params).single()
            if not record:
                return {"elements": {"nodes": [], "edges": []}, "source": "neo4j"}
            for node in record.get("nodes") or []:
                if node is None:
                    continue
                labels = list(node.labels)
                label = labels[0] if labels else "Node"
                node_id = _node_element_id(node, label)
                if node_id in seen_nodes:
                    continue
                seen_nodes.add(node_id)
                title = (
                    node.get("canonical_name")
                    or node.get("name")
                    or node.get("title")
                    or node_id
                )
                nodes.append(
                    {
                        "data": {
                            "id": node_id,
                            "label": title,
                            "type": label,
                        }
                    }
                )
            for rel in record.get("rels") or []:
                if rel is None:
                    continue
                start_labels = list(rel.start_node.labels)
                end_labels = list(rel.end_node.labels)
                src = _node_element_id(rel.start_node, start_labels[0] if start_labels else "Node")
                tgt = _node_element_id(rel.end_node, end_labels[0] if end_labels else "Node")
                edge_id = f"{src}-{rel.type}-{tgt}"
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
                edges.append(
                    {
                        "data": {
                            "id": edge_id,
                            "source": src,
                            "target": tgt,
                            "type": rel.type,
                        }
                    }
                )

        return {
            "elements": {"nodes": nodes, "edges": edges},
            "source": "neo4j",
            "center": center,
            "depth": depth,
            "industry_code": industry_code,
        }


def _node_element_id(node: Any, label: str) -> str:
    if label == "Company" and node.get("company_id"):
        return str(node["company_id"])
    if label == "Industry" and node.get("code"):
        return f"{node.get('taxonomy', 'GB2017')}:{node['code']}"
    if label == "Region" and node.get("region_code"):
        return str(node["region_code"])
    if label == "Contract" and node.get("contract_id"):
        return str(node["contract_id"])
    return str(node.element_id)


_service: Neo4jGraphService | None = None


def get_neo4j_graph_service() -> Neo4jGraphService:
    global _service
    if _service is None:
        _service = Neo4jGraphService()
    return _service
