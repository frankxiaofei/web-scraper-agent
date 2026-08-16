"""Ontology YAML loader (Phase 2 P2-T-002)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.core.config import ROOT

ONTOLOGY_FILE = ROOT / "config" / "ontology" / "industry_supply_chain.yaml"


@lru_cache(maxsize=1)
def load_ontology() -> dict[str, Any]:
    if not ONTOLOGY_FILE.is_file():
        return {"ontology": {"version": "0.0.0", "object_types": {}, "link_types": {}}}
    with ONTOLOGY_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


def ontology_version() -> str:
    return str((load_ontology().get("ontology") or {}).get("version") or "0.0.0")


def neo4j_label(object_type: str) -> str | None:
    obj = (load_ontology().get("ontology") or {}).get("object_types") or {}
    spec = obj.get(object_type) or {}
    ds = spec.get("datasource") or {}
    return ds.get("neo4j_label")
