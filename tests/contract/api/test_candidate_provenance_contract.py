from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ashare_lab.api import create_app
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGenerator,
    CandidateGroundingEvidence,
    CandidateProvenance,
    CompileInput,
    IndicatorIntent,
)

ROOT = Path(__file__).parents[3]


class _GroundedCandidateGenerator(CandidateGenerator):
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        entry_text = "MACD金叉买入"
        exit_text = "死叉卖出"
        params = (("fast", 12), ("signal", 9), ("slow", 26))
        return (
            CandidateAst(
                instrument_symbol=request.instrument_context,
                entry=(
                    IndicatorIntent(
                        indicator_id="technical.macd",
                        definition_version="1.0.0",
                        trigger="golden_cross",
                        params=params,
                    ),
                ),
                exit=(
                    IndicatorIntent(
                        indicator_id="technical.macd",
                        definition_version="1.0.0",
                        trigger="death_cross",
                        params=params,
                    ),
                ),
                confidence=0.93,
                provenance=CandidateProvenance(
                    source="bounded_provider",
                    provider="test-provider",
                    model="test-model",
                    prompt_version="zh-bounded.v1",
                    schema_version="bounded-candidate.v1",
                    capability_projection_version="candidate-capabilities.v1",
                    capability_projection_hash="sha256:" + "b" * 64,
                    upstream_pattern_commit=("e90b6c6cd9fea23067a85667e7fbf74f9d73ea48"),
                    candidate_rank=1,
                ),
                grounding_evidence=(
                    CandidateGroundingEvidence(
                        path="/entry/0",
                        start=request.utterance.index(entry_text),
                        end=request.utterance.index(entry_text) + len(entry_text),
                        text=entry_text,
                    ),
                    CandidateGroundingEvidence(
                        path="/exit/0",
                        start=request.utterance.index(exit_text),
                        end=request.utterance.index(exit_text) + len(exit_text),
                        text=exit_text,
                    ),
                ),
            ),
        )


def test_candidate_identity_and_grounding_survive_storage_and_idempotent_replay() -> None:
    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = catalog.manifests[0]
    compiler = StrategyCompiler(
        generator=_GroundedCandidateGenerator(),
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
    )
    app = create_app(compiler=compiler, catalog=catalog)
    request = {
        "utterance": "MACD金叉买入，死叉卖出",
        "instrument_context": "300059.SZ",
        "as_of_date": date(2026, 8, 30).isoformat(),
    }
    headers = {"Idempotency-Key": "grounding-replay-001"}

    with TestClient(app) as client:
        first = client.post("/api/v1/strategy-drafts", json=request, headers=headers)
        replay = client.post("/api/v1/strategy-drafts", json=request, headers=headers)

    assert first.status_code == replay.status_code == 201
    payload: dict[str, Any] = first.json()
    assert payload["status"] == "ready"
    assert payload["candidate_provenance"] == {
        "source": "bounded_provider",
        "provider": "test-provider",
        "model": "test-model",
        "prompt_version": "zh-bounded.v1",
        "schema_version": "bounded-candidate.v1",
        "capability_projection_version": "candidate-capabilities.v1",
        "capability_projection_hash": "sha256:" + "b" * 64,
        "upstream_pattern_commit": "e90b6c6cd9fea23067a85667e7fbf74f9d73ea48",
        "candidate_rank": 1,
    }
    assert payload["candidate_grounding"] == {
        "matched_spans": ["MACD金叉买入", "死叉卖出"],
        "spans": [
            {"path": "/entry/0", "start": 0, "end": 8, "text": "MACD金叉买入"},
            {"path": "/exit/0", "start": 9, "end": 13, "text": "死叉卖出"},
        ],
    }
    assert replay.json() == payload
    assert replay.headers["Idempotency-Replayed"] == "true"
