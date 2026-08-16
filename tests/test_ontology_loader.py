"""Ontology loader tests (P2-T-002)."""

from __future__ import annotations

from src.industry.ontology.loader import load_ontology, neo4j_label, ontology_version


def test_load_ontology_has_version():
    data = load_ontology()
    assert "ontology" in data
    assert ontology_version() == "1.0.0"


def test_neo4j_label_company():
    assert neo4j_label("Company") == "Company"
    assert neo4j_label("Unknown") is None
