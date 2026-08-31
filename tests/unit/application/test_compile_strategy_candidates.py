from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import combinations
from pathlib import Path

import pytest

from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import AllCondition, AnyCondition
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGenerator,
    CandidateProvenance,
    CompileInput,
    ConditionJoin,
    EventIntent,
    ExitIntent,
    HoldingPeriodIntent,
    IndicatorIntent,
    PositionReturnIntent,
    TrailingDrawdownIntent,
)

ROOT = Path(__file__).parents[3]


class _StaticCandidates(CandidateGenerator):
    def __init__(self, candidates: tuple[CandidateAst, ...]) -> None:
        self._candidates = candidates

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        del request
        return self._candidates


def _provenance(rank: int) -> CandidateProvenance:
    return CandidateProvenance(
        source="bounded_provider",
        provider="test-provider",
        model="test-model",
        prompt_version="zh-bounded.v1",
        schema_version="bounded-candidate.v1",
        capability_projection_version="candidate-capabilities.v1",
        capability_projection_hash="sha256:" + "a" * 64,
        upstream_pattern_commit="e90b6c6cd9fea23067a85667e7fbf74f9d73ea48",
        candidate_rank=rank,
    )


def _macd_candidate(*, rank: int) -> CandidateAst:
    params = (("fast", 12), ("signal", 9), ("slow", 26))
    return CandidateAst(
        instrument_symbol="300059.SZ",
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
        confidence=0.9,
        provenance=_provenance(rank),
    )


def _macd_intent(trigger: str) -> IndicatorIntent:
    return IndicatorIntent(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        trigger=trigger,
        params=(("fast", 12), ("signal", 9), ("slow", 26)),
    )


_ANNUAL_REPORT = EventIntent(
    event_code="event.financial_results.annual_report",
    definition_version="1.0.0",
)
_EXIT_INTENT_CASES: tuple[tuple[str, ExitIntent], ...] = (
    ("indicator", _macd_intent("death_cross")),
    ("event", _ANNUAL_REPORT),
    ("holding_period", HoldingPeriodIntent(sessions=3)),
    ("take_profit", PositionReturnIntent(trigger="take_profit", threshold_pct=20)),
    ("stop_loss", PositionReturnIntent(trigger="stop_loss", threshold_pct=5)),
    ("trailing_drawdown", TrailingDrawdownIntent(threshold_pct=8)),
)
_EXIT_INTENT_PAIRS = tuple(combinations(_EXIT_INTENT_CASES, 2))


def _compiler(
    candidates: tuple[CandidateAst, ...],
    *,
    trusted_today: date = date(2026, 8, 30),
) -> StrategyCompiler:
    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = catalog.manifests[0]
    return StrategyCompiler(
        generator=_StaticCandidates(candidates),
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
        trusted_date_provider=lambda: trusted_today,
    )


@pytest.mark.asyncio
async def test_compiler_rejects_first_candidate_and_selects_first_catalog_valid_one() -> None:
    invalid = _macd_candidate(rank=1)
    invalid = replace(
        invalid,
        entry=(replace(invalid.entry[0], indicator_id="technical.invented"),),
    )
    outcome = await _compiler((invalid, _macd_candidate(rank=2))).compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.candidate_provenance == _provenance(2)
    assert [item.candidate_rank for item in outcome.candidate_rejections] == [1]
    assert outcome.candidate_rejections[0].diagnostic_code.startswith("strategy_validation_failed:")


@pytest.mark.asyncio
async def test_future_dated_candidate_cannot_block_a_later_valid_candidate() -> None:
    future = replace(
        _macd_candidate(rank=1),
        backtest_start=date(2026, 1, 1),
        backtest_end=date(2027, 1, 1),
    )
    outcome = await _compiler((future, _macd_candidate(rank=2))).compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出，回测五年",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.candidate_provenance == _provenance(2)
    assert outcome.candidate_rejections[0].diagnostic_code == "backtest_end_after_as_of_date"


@pytest.mark.asyncio
async def test_all_bounded_candidates_fail_with_one_directional_clarification_and_audit() -> None:
    first = _macd_candidate(rank=1)
    first = replace(
        first,
        entry=(replace(first.entry[0], indicator_id="technical.invented"),),
    )
    second = replace(
        _macd_candidate(rank=2),
        backtest_start=date(2026, 1, 1),
        backtest_end=date(2027, 1, 1),
    )
    outcome = await _compiler((first, second)).compile(
        CompileInput(
            utterance="把快线慢线交叉翻译成交易规则",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "candidate_batch_no_valid_strategy"
    assert "买入条件、卖出条件和回测区间" in (outcome.clarification or "")
    assert [item.candidate_rank for item in outcome.candidate_rejections] == [1, 2]


@pytest.mark.asyncio
async def test_semantically_distinct_valid_candidates_require_one_clarification() -> None:
    second = _macd_candidate(rank=2)
    different_params = (("fast", 8), ("signal", 5), ("slow", 21))
    second = replace(
        second,
        entry=(replace(second.entry[0], params=different_params),),
        exit=(replace(second.exit[0], params=different_params),),
    )

    outcome = await _compiler((_macd_candidate(rank=1), second)).compile(
        CompileInput(
            utterance="快慢线交叉买卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "candidate_set_ambiguous"
    assert "不会按模型置信度" in (outcome.clarification or "")
    assert len(outcome.candidate_alternatives) == 2
    assert len({item.strategy_hash for item in outcome.candidate_alternatives}) == 2


@pytest.mark.asyncio
async def test_duplicate_valid_candidates_are_deduplicated_by_canonical_strategy_hash() -> None:
    outcome = await _compiler((_macd_candidate(rank=1), _macd_candidate(rank=2))).compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.candidate_provenance == _provenance(1)
    assert len(outcome.candidate_alternatives) == 1


@pytest.mark.asyncio
async def test_request_as_of_date_cannot_move_beyond_trusted_server_date() -> None:
    outcome = await _compiler((_macd_candidate(rank=1),)).compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 31),
        )
    )

    assert outcome.status is CompileStatus.INVALID
    assert outcome.diagnostic_code == "request_as_of_date_in_future"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("join", "expected_type"),
    (("all", AllCondition), ("any", AnyCondition)),
)
async def test_entry_join_is_preserved_for_indicator_event_candidates(
    join: ConditionJoin,
    expected_type: type[AllCondition] | type[AnyCondition],
) -> None:
    base = _macd_candidate(rank=1)
    candidate = replace(
        base,
        entry=(base.entry[0], _ANNUAL_REPORT),
        entry_join=join,
    )

    outcome = await _compiler((candidate,)).compile(
        CompileInput(
            utterance="受限模型候选",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, expected_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("join", ("all", "any"))
@pytest.mark.parametrize(
    ("left_case", "right_case"),
    _EXIT_INTENT_PAIRS,
    ids=(f"{left[0]}-{right[0]}" for left, right in _EXIT_INTENT_PAIRS),
)
async def test_exit_join_matrix_never_rewrites_position_aware_and_as_first_of(
    join: ConditionJoin,
    left_case: tuple[str, ExitIntent],
    right_case: tuple[str, ExitIntent],
) -> None:
    left_name, left = left_case
    right_name, right = right_case
    candidate = replace(
        _macd_candidate(rank=1),
        exit=(left, right),
        exit_join=join,
    )

    outcome = await _compiler((candidate,)).compile(
        CompileInput(
            utterance=f"{left_name} {join} {right_name}",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    both_are_market_conditions = all(
        isinstance(item, (IndicatorIntent, EventIntent)) for item in (left, right)
    )
    if join == "all" and not both_are_market_conditions:
        assert outcome.status is CompileStatus.UNSUPPORTED
        assert outcome.strategy is None
        assert outcome.diagnostic_code == "position_aware_exit_and_not_supported"
        return

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    if join == "all":
        assert len(outcome.strategy.exit.children) == 1
        assert isinstance(outcome.strategy.exit.children[0], AllCondition)
    else:
        assert len(outcome.strategy.exit.children) == 2
