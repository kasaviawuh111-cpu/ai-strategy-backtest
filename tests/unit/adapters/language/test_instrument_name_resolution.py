from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import HybridCandidateGenerator
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput

ROOT = Path(__file__).resolve().parents[4]


class _UnexpectedFallback:
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError(f"bounded fallback must not run: {request!r}")


@pytest.mark.asyncio
async def test_standalone_company_name_is_resolved_before_rule_parsing() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"同花顺": "300033.SZ"}[name],
    )

    candidates = await generator.generate(
        CompileInput(
            utterance="同花顺ROE高于0%买入，MACD死叉卖出，回测近1年",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.unsupported_code is None
    assert candidate.instrument_symbol == "300033.SZ"
    assert candidate.grounding_evidence[0].path == "/instrument/symbol"
    assert candidate.grounding_evidence[0].text == "同花顺"


@pytest.mark.asyncio
async def test_bare_cross_trigger_still_leaves_the_leading_company_name_resolvable() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"汤姆猫": "300459.SZ"}[name],
    )

    candidates = await generator.generate(
        CompileInput(
            utterance="汤姆猫金叉买，MACD死叉卖出，回测近1年",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.unsupported_code is None
    assert candidate.instrument_symbol == "300459.SZ"
    assert candidate.entry[0].indicator_id == "technical.macd"
    assert candidate.entry[0].trigger == "golden_cross"
    assert candidate.grounding_evidence[0].text == "汤姆猫"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "entry_indicator", "entry_trigger", "exit_trigger"),
    [
        (
            "东方财富19元买，17.8元卖，回测近1年",
            "price.close",
            "crosses_above",
            "crosses_below",
        ),
        (
            "东方财富上涨5%买入，下跌5%卖出，回测近1年",
            "price.return_pct",
            "at_least",
            "at_most",
        ),
        (
            "东方财富当日上涨5%买入，当日下跌5%卖出，回测近1年",
            "price.return_pct",
            "at_least",
            "at_most",
        ),
        (
            "东方财富当日下跌5%买入，当日上涨5%卖出，回测近1年",
            "price.return_pct",
            "at_most",
            "at_least",
        ),
    ],
)
async def test_condition_order_markers_leave_the_company_name_resolvable(
    utterance: str,
    entry_indicator: str,
    entry_trigger: str,
    exit_trigger: str,
) -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )

    candidates = await generator.generate(
        CompileInput(
            utterance=utterance,
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.unsupported_code is None
    assert candidate.instrument_symbol == "300059.SZ"
    assert candidate.entry[0].indicator_id == entry_indicator
    assert candidate.entry[0].trigger == entry_trigger
    assert candidate.exit[0].trigger == exit_trigger
    assert candidate.grounding_evidence[0].text == "东方财富"

    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )
    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
async def test_resolved_standalone_company_name_enables_validated_clarification_choices() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="东方财富MACD金叉买入",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "exit_rule_not_recognized"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol == "300059.SZ"
    assert {proposal.title for proposal in outcome.idea_route.proposals} == {
        "动能转弱",
        "持有 5 日",
        "亏损 8%",
    }


@pytest.mark.asyncio
async def test_resolved_company_bare_crosses_offer_three_catalog_validated_families() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"汤姆猫": "300459.SZ"}[name],
    )
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="汤姆猫金叉买死叉卖",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "ambiguous_cross_indicator"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol == "300459.SZ"
    assert len(outcome.idea_route.proposals) == 3
    assert {proposal.title for proposal in outcome.idea_route.proposals} == {
        "MACD",
        "KDJ",
        "5/20 日均线",
    }
    assert [(item.path, item.text) for item in outcome.candidate_grounding] == [
        ("/instrument/symbol", "汤姆猫"),
        ("/clarification", "汤姆猫金叉"),
    ]

    for proposal in outcome.idea_route.proposals:
        compiled = await compiler.compile(
            CompileInput(
                utterance=proposal.suggested_utterance,
                instrument_context="300459.SZ",
                as_of_date=date(2026, 8, 20),
            )
        )
        assert compiled.status is CompileStatus.READY, proposal.suggested_utterance
        assert compiled.strategy is not None
        assert compiled.strategy.instrument.symbol == "300459.SZ"


@pytest.mark.asyncio
async def test_dual_moving_average_period_is_not_absorbed_into_company_name() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"汤姆猫": "300459.SZ"}[name],
    )

    candidates = await generator.generate(
        CompileInput(
            utterance="汤姆猫5日均线上穿20日均线买入，5日均线下穿20日均线卖出",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.unsupported_code is None
    assert candidate.instrument_symbol == "300459.SZ"
    assert candidate.entry[0].indicator_id == "technical.ma_cross"
    assert candidate.exit[0].indicator_id == "technical.ma_cross"


@pytest.mark.asyncio
async def test_named_company_cannot_silently_replace_a_real_stock_page_context() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda _name: "300033.SZ",
    )

    candidates = await generator.generate(
        CompileInput(
            utterance="同花顺ROE高于0%买入，MACD死叉卖出，回测近1年",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].instrument_symbol == "300059.SZ"
    assert candidates[0].unsupported_code == "instrument_context_mismatch"


@pytest.mark.asyncio
async def test_unconfirmed_company_name_fails_closed_without_default_stock() -> None:
    def unresolved(_name: str) -> str:
        raise LookupError("not found")

    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=unresolved,
    )

    candidates = await generator.generate(
        CompileInput(
            utterance="不存在公司ROE高于0%买入，MACD死叉卖出",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].instrument_symbol is None
    assert candidates[0].unsupported_code == "instrument_unconfirmed"


@pytest.mark.asyncio
async def test_explicit_code_keeps_the_existing_deterministic_path() -> None:
    calls: list[str] = []

    def resolver(name: str) -> str:
        calls.append(name)
        return "300033.SZ"

    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=resolver,
    )

    candidates = await generator.generate(
        CompileInput(
            utterance="回测300033的ROE高于0%买入，MACD死叉卖出",
            instrument_context=None,
            as_of_date=date(2026, 8, 20),
        )
    )

    assert candidates[0].instrument_symbol == "300033.SZ"
    assert calls == []
