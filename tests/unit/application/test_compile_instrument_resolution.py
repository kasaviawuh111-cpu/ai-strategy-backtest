from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGenerator,
    CandidateProvenance,
    CompileInput,
    IndicatorIntent,
)

ROOT = Path(__file__).parents[3]


class _StaticCandidateGenerator(CandidateGenerator):
    def __init__(self, candidates: tuple[CandidateAst, ...]) -> None:
        self._candidates = candidates

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        del request
        return self._candidates


def _candidate(symbol: str, *, rank: int) -> CandidateAst:
    parameters = (("fast", 12), ("signal", 9), ("slow", 26))
    return CandidateAst(
        instrument_symbol=symbol,
        entry=(
            IndicatorIntent(
                indicator_id="technical.macd",
                definition_version="1.0.0",
                trigger="golden_cross",
                params=parameters,
            ),
        ),
        exit=(
            IndicatorIntent(
                indicator_id="technical.macd",
                definition_version="1.0.0",
                trigger="death_cross",
                params=parameters,
            ),
        ),
        confidence=0.9,
        provenance=CandidateProvenance(
            source="bounded_provider",
            provider="test-provider",
            model="test-model",
            prompt_version="zh-bounded.v1",
            schema_version="bounded-candidate.v1",
            capability_projection_version="candidate-capabilities.v1",
            capability_projection_hash="sha256:" + "a" * 64,
            upstream_pattern_commit="e90b6c6cd9fea23067a85667e7fbf74f9d73ea48",
            candidate_rank=rank,
        ),
    )


def _compiler(candidates: tuple[CandidateAst, ...]) -> StrategyCompiler:
    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = catalog.manifests[0]
    return StrategyCompiler(
        generator=_StaticCandidateGenerator(candidates),
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
        trusted_date_provider=lambda: date(2026, 8, 30),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    ["300059.SH", "600519.SZ", "430047.SZ", "920000.SH", "399001.SZ"],
)
async def test_compiler_rejects_code_suffix_conflicts_before_data_acquisition(
    symbol: str,
) -> None:
    outcome = await _compiler((_candidate(symbol, rank=1),)).compile(
        CompileInput(
            utterance=f"{symbol} MACD金叉买入，死叉卖出",
            instrument_context=symbol,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "invalid_a_share_instrument"
    assert [item.diagnostic_code for item in outcome.candidate_rejections] == [
        "invalid_a_share_instrument"
    ]


@pytest.mark.asyncio
async def test_invalid_first_candidate_cannot_block_a_later_resolved_a_share() -> None:
    invalid = _candidate("300059.SH", rank=1)
    valid = _candidate("600519.SH", rank=2)

    outcome = await _compiler((invalid, valid)).compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出",
            instrument_context="600519.SH",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "600519.SH"
    assert outcome.candidate_provenance == valid.provenance
    assert outcome.candidate_rejections[0].diagnostic_code == "invalid_a_share_instrument"
