from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput

ROOT = Path(__file__).parents[3]


@pytest.mark.asyncio
@pytest.mark.parametrize('end,accepted', [('2026-09-16', True), ('2026-09-17', False)])
async def test_explicit_range_after_import_watermark_reaches_data_preflight(end, accepted):
    compiler = StrategyCompiler(generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / 'catalogs'),
        catalog_id='cn_a.signals', release_version='2026.09.01',
        trusted_date_provider=lambda: date(2026, 9, 16),
        backtest_anchor_date=date(2026, 9, 11))
    outcome = await compiler.compile(CompileInput(
        utterance=f'MACD金叉买入，死叉卖出，回测2025-09-11至{end}',
        instrument_context='300308.SZ', as_of_date=date(2026, 9, 11)))
    assert (outcome.status is CompileStatus.READY) is accepted
    if accepted:
        assert outcome.strategy.backtest.end == date.fromisoformat(end)
    else:
        assert outcome.diagnostic_code == 'backtest_end_after_as_of_date'


class _CapturingGenerator:
    def __init__(self) -> None:
        self.requests: list[CompileInput] = []
        self._delegate = RuleBasedCandidateGenerator()

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.requests.append(request)
        return await self._delegate.generate(request)


@pytest.fixture
def compiler() -> StrategyCompiler:
    return StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )


@pytest.mark.asyncio
async def test_weekly_import_anchor_does_not_follow_monday_clock():
    imported = [date(2026, 9, 11)]
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / 'catalogs'),
        catalog_id='cn_a.signals', release_version='2026.09.01',
        trusted_date_provider=lambda: date(2026, 9, 21),
        backtest_anchor_date=lambda: imported[0],
    )
    request = CompileInput(utterance='MACD金叉买入，死叉卖出，近一年',
                           instrument_context='300059.SZ', as_of_date=date(2026, 9, 21))
    first = await compiler.compile(request)
    assert first.strategy is not None
    assert first.strategy.backtest.end == date(2026, 9, 11)
    assert first.strategy.backtest.start == date(2025, 9, 11)
    imported[0] = date(2026, 9, 18)
    second = await compiler.compile(request)
    assert second.strategy is not None
    assert second.strategy.backtest.end == date(2026, 9, 18)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "东方财富 MACD 金叉买入，死叉卖出，回测 2021-08-06 至 2026-08-06",
        "东方财富 MACD 金叉买入，死叉卖出，回测 2021年8月6日至2026年8月6日",
        "东方财富年度报告发布后买入，MACD死叉卖出，回测2021/08/06到2026/08/06",
    ],
)
async def test_explicit_backtest_date_range_is_preserved(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2021, 8, 6)
    assert outcome.strategy.backtest.end == date(2026, 8, 6)
    provenance = {item.path: item.source for item in outcome.provenance}
    assert provenance["/backtest/start"] == "utterance/explicit_date_range"
    assert provenance["/backtest/end"] == "utterance/explicit_date_range"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "period",
    ("回测近5年", "回测近五年", "近五年", "最近五年"),
)
async def test_relative_backtest_years_are_anchored_to_as_of_date(
    compiler: StrategyCompiler,
    period: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"MACD金叉买入，死叉卖出，{period}",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2021, 8, 20)
    assert outcome.strategy.backtest.end == date(2026, 8, 20)
    provenance = {item.path: item.source for item in outcome.provenance}
    assert provenance["/backtest/start"] == "utterance/relative_lookback"
    assert provenance["/backtest/end"] == "request/as_of_date"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "diagnostic_code"),
    [
        ("回测2021-02-30至2026-08-06", "backtest_date_invalid"),
        ("回测2021-08-06至", "backtest_date_range_incomplete"),
        ("回测2021-08-06", "backtest_date_range_incomplete"),
        ("回测2026-08-06至2021-08-06", "backtest_date_range_reversed"),
        (
            "回测2021-08-06到2022-08-06至2026-08-06",
            "backtest_date_range_ambiguous",
        ),
        (
            "回测近5年，回测2021-08-06至2026-08-06",
            "backtest_date_range_ambiguous",
        ),
        ("回测2021年至2026年", "backtest_date_range_unsupported"),
        ("回测2021-08至2026-08", "backtest_date_range_unsupported"),
        ("回测近0年", "backtest_lookback_invalid"),
        ("回测近101年", "backtest_lookback_invalid"),
    ],
)
async def test_incomplete_invalid_or_ambiguous_period_fails_closed(
    compiler: StrategyCompiler,
    period: str,
    diagnostic_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"MACD金叉买入，死叉卖出，{period}",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.strategy is None
    assert outcome.diagnostic_code == diagnostic_code


@pytest.mark.asyncio
async def test_explicit_end_after_as_of_date_fails_closed(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出，回测2021-08-06至2026-08-30",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.strategy is None
    assert outcome.diagnostic_code == "backtest_end_after_as_of_date"


@pytest.mark.asyncio
async def test_snapshot_anchor_replaces_client_date_for_relative_period() -> None:
    generator = _CapturingGenerator()
    anchored = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        trusted_date_provider=lambda: date(2026, 8, 31),
        backtest_anchor_date=date(2026, 8, 20),
    )

    outcome = await anchored.compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出，近五年",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 6),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2021, 8, 20)
    assert outcome.strategy.backtest.end == date(2026, 8, 20)
    assert generator.requests[0].as_of_date == date(2026, 8, 20)
    provenance = {item.path: item.source for item in outcome.provenance}
    assert provenance["/backtest/end"] == "data_snapshot/end"
