from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute

ROOT = Path(__file__).parents[3]


class _UnsupportedGenerator:
    def __init__(self, code: str, *, count: int = 1) -> None:
        self.code = code
        self.count = count
        self.requests: list[CompileInput] = []

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.requests.append(request)
        return tuple(
            CandidateAst(
                instrument_symbol=request.instrument_context,
                entry=(),
                exit=(),
                confidence=0.0,
                unsupported_code=self.code,
            )
            for _ in range(self.count)
        )


class _RecordingIdeaRouter:
    def __init__(self, result: IdeaRoute | None) -> None:
        self.result = result
        self.requests: list[CompileInput] = []

    async def route(self, request: CompileInput) -> IdeaRoute | None:
        self.requests.append(request)
        return self.result


def _idea_route(
    *,
    proposal_count: int = 2,
    instrument_symbol: str | None = "300059.SZ",
) -> IdeaRoute:
    proposals = tuple(
        IdeaProposal(
            id=f"idea_{index:012x}",
            title=f"候选 {index}",
            hypothesis="只用价格代理检验该观点。",
            entry_summary="MACD 金叉",
            exit_summary="MACD 死叉",
            suggested_utterance="MACD金叉买入，死叉卖出，回测近5年",
            capability_ids=("technical.macd",),
            assumptions=("不证明因果关系。",),
            confidence=0.75,
        )
        for index in range(1, proposal_count + 1)
    )
    return IdeaRoute(
        understanding="用户表达了一个政治态度。",
        hypothesis="相关不确定性可能与当前股票的价格行为同期出现。",
        asset_mapping=IdeaAssetMapping(
            instrument_symbol=instrument_symbol,
            relation="current_page_proxy" if instrument_symbol is not None else "unbound",
            rationale=(
                "只使用当前股票页作为价格代理。"
                if instrument_symbol is not None
                else "尚未绑定证券；选择方向后仍需补充具体 A 股。"
            ),
            evidence_status=(
                "host_context_only" if instrument_symbol is not None else "instrument_required"
            ),
        ),
        proposals=proposals,
    )


def _compiler(*, generator: object, idea_router: _RecordingIdeaRouter) -> StrategyCompiler:
    return StrategyCompiler(
        generator=generator,  # type: ignore[arg-type]
        idea_router=idea_router,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    (
        "我讨厌特朗普",
        "我看好国产算力",
        "降息可能让成长股更受欢迎吗",
        "这家公司管理层让我不放心",
        "AI会不会是泡沫",
    ),
)
async def test_broad_everyday_viewpoints_are_guided_before_strict_translation(
    utterance: str,
) -> None:
    generator = _UnsupportedGenerator("candidate_provider_invalid_output")
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(generator=generator, idea_router=idea_router)

    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert generator.requests == []
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic_code", "candidate_count"),
    [
        ("no_supported_signal_recognized", 1),
        ("no_supported_signal_recognized", 3),
    ],
)
async def test_literal_miss_batch_routes_to_non_executable_guidance(
    diagnostic_code: str,
    candidate_count: int,
) -> None:
    generator = _UnsupportedGenerator(diagnostic_code, count=candidate_count)
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(generator=generator, idea_router=idea_router)

    outcome = await compiler.compile(
        CompileInput(
            utterance="我想买入这只股票，但你帮我决定什么时候卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert len(generator.requests) == 1
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_invalid_literal_candidate_can_fall_back_to_non_executable_guidance() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=_UnsupportedGenerator("candidate_provider_invalid_output"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="这个观点成立时买入，不成立时卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_invalid_literal_candidate_stays_unsupported_when_guidance_also_fails() -> None:
    idea_router = _RecordingIdeaRouter(None)
    compiler = _compiler(
        generator=_UnsupportedGenerator("candidate_provider_invalid_output"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="这个观点成立时买入，不成立时卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "candidate_provider_invalid_output"
    assert outcome.idea_route is None
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_complete_strategy_keeps_the_existing_fast_path() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.idea_route is None
    assert idea_router.requests == []


@pytest.mark.asyncio
async def test_explicit_unsupported_semantics_never_enter_idea_routing() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="5分钟MACD金叉买入，5分钟死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "non_daily_timeframe_not_supported"
    assert outcome.idea_route is None
    assert idea_router.requests == []


@pytest.mark.asyncio
async def test_view_without_stock_is_guided_first_and_remains_non_executable() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol is None
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_selected_viewpoint_direction_keeps_the_resolved_stock_context() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )
    original = CompileInput(
        utterance="我看好东方财富",
        instrument_context=None,
        as_of_date=date(2026, 8, 30),
    )
    prior = await compiler.compile(original)

    turn = await compiler.answer_clarification(
        original_input=original,
        prior_outcome=prior,
        answer="1",
    )

    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
async def test_negative_theme_viewpoint_is_not_misread_as_a_company_name() -> None:
    resolved_names: list[str] = []

    def resolver(name: str) -> str:
        resolved_names.append(name)
        raise LookupError(name)

    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
        instrument_name_resolver=resolver,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我不喜欢新能源",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol is None
    assert resolved_names == []


@pytest.mark.asyncio
async def test_invalid_guidance_batch_is_not_exposed() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route(proposal_count=1))
    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "no_supported_signal_recognized"
    assert outcome.idea_route is None
