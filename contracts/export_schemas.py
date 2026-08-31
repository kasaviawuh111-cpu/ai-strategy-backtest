"""Generate deterministic JSON Schema snapshots from the authoritative models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ashare_lab.domain.catalog.models import CatalogManifest
from ashare_lab.domain.strategy.models import StrategySpec

CONTRACTS_DIR = Path(__file__).resolve().parent


def build_schemas() -> dict[str, dict[str, Any]]:
    schemas = {
        "strategy.v1.schema.json": StrategySpec.model_json_schema(),
        "indicator-catalog.v1.schema.json": CatalogManifest.model_json_schema(),
    }
    identifiers = {
        "strategy.v1.schema.json": "https://schemas.ashare-lab.local/strategy.v1.schema.json",
        "indicator-catalog.v1.schema.json": (
            "https://schemas.ashare-lab.local/indicator-catalog.v1.schema.json"
        ),
    }
    for filename, schema in schemas.items():
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = identifiers[filename]
    return schemas


def export_schemas(output_dir: Path = CONTRACTS_DIR) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, schema in build_schemas().items():
        payload = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (output_dir / filename).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    export_schemas()
