"""Controlled routing contracts only; these are not real-data acceptance evidence."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryBeforeListingError
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderDataError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.api import create_app
from ashare_lab.api.container import ApiContainer, BacktestSubmitter
from ashare_lab.api.routes.strategy_drafts import (
    _preflight_idea_choices,  # pyright: ignore[reportPrivateUsage]
    _to_response,  # pyright: ignore[reportPrivateUsage]
)
from ashare_lab.api.schemas import StrategyDraftResponse
from ashare_lab.api.store import StoredDraftRevision
from ashare_lab.application.backtest_submission import BacktestDataNotYetAvailableError, BacktestRunConfig
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.application.dialogue_state import DialogueState, VerifiedInstrumentMemory
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator
from ashare_lab.application.skill_numeric_history import SkillNumericHistoryError
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
    canonical_hash,
)
from ashare_lab.ports.candidate_generation import CandidateGroundingEvidence, CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
    UnboundIdeaStrategy,
)
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult, LiveMarketDataProvenance, LiveMarketDataResult,
)
from ashare_lab.ports.strategy_advice import (
    StockRecommendation,
    StockStrategyPair,
    StockStrategyPairing,
    VerifiedFactStrategyAdvisor,
)

_START, _END = date(2025, 9, 5), date(2026, 9, 4)
_NOW = datetime(2026, 9, 7, tzinfo=UTC)
_SYMBOLS = ("688825.SH", "600183.SH", "000977.SZ")


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_available", [True, False])
@pytest.mark.parametrize("relative_range", [True, False])
async def test_explicit_latest_price_grid_interval_is_calibrated_to_quote(quote_available, relative_range):
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    original = _outcome()
    proposal = original.idea_route.proposals[0]
    strategy = proposal.strategy.model_copy(update={
        "entry": None, "exit": None,
        "trading_plan": GridPlan(parameters=GridParameters(
            anchor_mode="latest_price",
            lower_price=Decimal("0.01") if relative_range else Decimal(40),
            upper_price=Decimal(1000000) if relative_range else Decimal(80),
            range_percent=Decimal(20) if relative_range else None,
            spacing_mode="cny", spacing=Decimal(1),
            buy_spacing_mode="cny", buy_spacing=Decimal(2),
            sell_spacing_mode="cny", sell_spacing=Decimal(3),
            price_mode="fixed_limit", buy_limit=Decimal(59), sell_limit=Decimal(61),
            limit_offset_cny=Decimal("0.02"),
        )),
    })
    original = replace(original, idea_route=replace(original.idea_route,
        proposals=(replace(proposal, strategy=strategy),)))

    async def prepare(*args):
        return SimpleNamespace(history=SimpleNamespace(rows=(SimpleNamespace(
            session_date=_END, raw_close=Decimal("215.80"),
        ),)))

    # The prepared period ends before the quote and has a different close.
    # Only the actual current quote can become the latest-price anchor.
    quote = LiveFinanceDataResult(provider="eastmoney_mx_finance_data", query="latest",
        indicators="最新价", tables=({"code": strategy.instrument.symbol,
            "nameMap": {"ZXJ_f2_3" if quote_available else "CLOSE":
                        "最新价" if quote_available else "收盘价"},
            "rawTable": {"ZXJ_f2_3" if quote_available else "CLOSE": ["300.00"],
                         "headName": ["2026-09-07 09:35"]}},),
        provenance=LiveMarketDataProvenance("sha256:" + "b" * 64, _NOW, "fixture"))
    container = _container(prepare)
    container.live_finance_data = SimpleNamespace(query_finance=AsyncMock(return_value=quote))
    result = await _preflight_idea_choices(outcome=original, container=container)
    if not quote_available:
        assert result.diagnostic_code == "candidate_data_incomplete"
        assert result.idea_route.proposals[0].strategy == strategy
        assert result.idea_route.proposals[0].strategy.trading_plan.parameters.anchor_price is None
        assert "215.80" not in result.idea_route.proposals[0].entry_summary
        return
    assert result.diagnostic_code != "candidate_data_not_ready"
    updated = result.idea_route.proposals[0]
    params = updated.strategy.trading_plan.parameters
    assert params.anchor_mode == "latest_price"
    assert params.anchor_price == Decimal("300.00")
    assert params.lower_price == (Decimal("0.01") if relative_range else Decimal("200.00"))
    assert params.upper_price == (Decimal(1000000) if relative_range else Decimal("400.00"))
    assert (params.spacing, params.buy_spacing, params.sell_spacing) == (Decimal(1), Decimal(2), Decimal(3))
    assert (params.buy_limit, params.sell_limit, params.limit_offset_cny) == (Decimal(59), Decimal(61), Decimal("0.02"))
    assert params.initial_shares == 0 and params.opening_shares == 0
    if relative_range:
        resolved = params.resolve_geometry()
        assert (resolved.lower_price, resolved.upper_price) == (Decimal(240), Decimal(360))
    assert params.anchor_quote_response_sha256 == quote.provenance.response_sha256
    assert params.anchor_quote_source == quote.provider
    assert "2026-09-07 09:35" in updated.entry_summary
    assert "215.80" not in updated.entry_summary
    assert updated.strategy_hash == canonical_hash(updated.strategy)
    assert strategy.trading_plan.parameters.anchor_price is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["grounded", "selected"])
async def test_preflight_never_rescales_user_grid_parameters(protection):
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    original = _outcome()
    proposal = original.idea_route.proposals[0]
    params = GridParameters(anchor_mode="manual", anchor_price=Decimal("60"),
        lower_price=Decimal("40"), upper_price=Decimal("80"),
        spacing_mode="cny", spacing=Decimal("1"), order_shares=100,
        initial_shares=0, max_shares=10000)
    strategy = proposal.strategy.model_copy(update={
        "entry": None, "exit": None, "trading_plan": GridPlan(parameters=params),
    })
    proposal = replace(proposal, strategy=strategy)
    original = replace(original, idea_route=replace(original.idea_route, proposals=(proposal,)),
        selected_idea_proposal=proposal if protection == "selected" else None,
        candidate_grounding=(CandidateGroundingEvidence(
            path="/trading_plan", start=0, end=2, text="网格"),)
            if protection == "grounded" else ())
    calls = []

    async def prepare(current, _config):
        calls.append(current)
        return SimpleNamespace(history=SimpleNamespace(rows=(SimpleNamespace(
            session_date=_END, raw_close=Decimal("215.80"),
        ),)))

    result = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    assert calls == ([] if protection == "selected" else [strategy])
    assert result is original
    assert result.idea_route.proposals[0].strategy.trading_plan.parameters == params


def _outcome() -> CompileOutcome:
    proposals: list[IdeaProposal] = []
    for index, symbol in enumerate(_SYMBOLS):
        strategy = StrategySpec(
            catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01"),
            instrument=Instrument(symbol=symbol),
            entry=IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": 10 + index, "price_field": "close"},
                trigger="price_crosses_above",
            ),
            exit=FirstOfExit(children=(HoldingPeriodExit(sessions=5 + index),)),
            backtest=BacktestConfig(start=_START, end=_END, initial_cash_cny=100_000),
        )
        proposals.append(IdeaProposal(
            id=f"idea_{index:012x}", title=f"均线方案{index}", hypothesis="受控候选方案",
            entry_summary=f"收盘价上穿{10 + index}日均线时买入",
            exit_summary=f"持有满{5 + index}个交易日卖出",
            suggested_utterance=f"选择均线方案{index}", capability_ids=("technical.ma",),
            assumptions=(), confidence=1.0, instrument_symbol=symbol,
            instrument_name=f"样本{index}", strategy=strategy,
            strategy_hash=canonical_hash(strategy),
            strategy_template=UnboundIdeaStrategy.model_validate(
                strategy.model_dump(exclude={"instrument", "schema_version"}),
            ),
        ))
    route = IdeaRoute(
        understanding="先看这几个可修改的方向。", hypothesis="受控回测候选",
        asset_mapping=IdeaAssetMapping(
            instrument_symbol=None, relation="unbound", rationale="等待用户选择股票与策略。",
            evidence_status="instrument_required",
        ),
        proposals=tuple(proposals),
    )
    first = proposals[0]
    return CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        clarification=route.understanding, idea_route=route,
        suggested_strategy=first.strategy, suggested_strategy_hash=first.strategy_hash,
        suggested_strategy_choice_id=first.id, suggested_strategy_note="待用户确认的可修改方案。",
        stock_recommendations=tuple(StockRecommendation(
            symbol, f"样本{index}", "受控筛选依据", source="fixture", retrieved_at=_NOW,
        ) for index, symbol in enumerate(_SYMBOLS)),
    )


def _container(
    prepare: Callable[[StrategySpec, BacktestRunConfig], Awaitable[object]],
) -> ApiContainer:
    return cast(ApiContainer, SimpleNamespace(
        backtest_submission=SimpleNamespace(prepare_candidate=prepare),
    ))


def _response(
    outcome: CompileOutcome, *, override: IdeaRoute | None = None,
) -> StrategyDraftResponse:
    return _to_response(StoredDraftRevision(
        draft_id=uuid4(), revision=1, outcome=outcome,
        compile_input=CompileInput(utterance="给我几个可修改的方案", as_of_date=_END),
        created_at=_NOW,
    ), idea_route_override=override)


def test_unbound_internal_templates_serialize_as_recoverable_pairing_pending():
    original = _outcome()
    route = replace(original.idea_route, proposals=tuple(
        replace(p, instrument_symbol=None) for p in original.idea_route.proposals
    ))
    pending = replace(original, idea_route=route, diagnostic_code="idea_guidance_required",
                      clarification="正在核实相关股票，你的规则已保留。")
    response = _response(pending)
    assert response.idea_route is None
    assert response.diagnostic_code == "stock_pairing_pending"
    assert response.clarification == pending.clarification
    assert pending.idea_route.proposals


@pytest.mark.parametrize("code", ["backtest_data_not_yet_available", "backtest_date_range_invalid"])
def test_selected_date_problem_serializes_rules_and_alternatives_for_editing(code):
    original = _outcome()
    selected = original.idea_route.proposals[0]
    pending = replace(original, selected_idea_proposal=selected,
                      revision_base_strategy=selected.strategy, diagnostic_code=code,
                      clarification="行情可选至2026-09-11，请调整结束日期后再回测。")
    response = _response(pending)
    assert response.status is CompileStatus.NEEDS_CLARIFICATION
    assert response.idea_route is not None
    assert response.strategy is None and not response.run_requested
    assert response.execution_assessment.interpreted_strategy == selected.strategy
    assert response.execution_assessment.strategy_hash == canonical_hash(selected.strategy)
    assert response.execution_assessment.message == pending.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_count", [1, 2])
async def test_prelisting_choices_are_retained_with_data_gaps(
    failed_count: int, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="uvicorn.error")
    original = _outcome()
    assert original.idea_route is not None
    original = replace(
        original, clarification="以下三种方案都可以选择。",
        idea_route=replace(original.idea_route, understanding="以下三种方案都可以选择。"),
    )
    failed_symbols = set(_SYMBOLS[:failed_count])
    seen: list[StrategySpec] = []

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        seen.append(strategy)
        if strategy.instrument.symbol in failed_symbols:
            raise MxDailyHistoryBeforeListingError(start=_START, listing_date=date(2026, 5, 1))
        return object()

    prepared = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    assert prepared.idea_route is not None
    assert prepared.clarification == prepared.idea_route.understanding
    assert "请将开始日期改到上市之后" in prepared.clarification
    assert prepared.clarification.startswith(original.idea_route.understanding)
    survivors = original.idea_route.proposals[failed_count:]
    assert len(prepared.idea_route.proposals) == 3
    assert prepared.idea_route.proposals[failed_count:] == survivors
    assert all(any(note.startswith("回测设置：") for note in item.assumptions)
               for item in prepared.idea_route.proposals[:failed_count])
    assert len(seen) == 3
    records = [record.getMessage() for record in caplog.records
               if record.getMessage().startswith("candidate_preflight ")]
    assert len(records) == 3
    assert sum("result=ready " in message for message in records) == 3 - failed_count
    assert sum("result=history_before_listing " in message for message in records) == failed_count
    assert all("elapsed_ms=" in message and "exception_class=" in message for message in records)
    assert all(item.backtest.start == _START and item.backtest.end == _END for item in seen)
    assert prepared.suggested_strategy is None
    assert prepared.suggested_strategy_choice_id is None
    response = _response(prepared)
    assert response.idea_route is not None
    assert [item.id for item in response.idea_route.proposals] == [item.id for item in original.idea_route.proposals]
    assert [item.symbol for item in response.instrument_suggestions] == list(
        _SYMBOLS[failed_count:],
    )
    # This is the payload consumed by selectable frontend cards, including the
    # one-survivor boundary; rejected choices cannot remain in a second list.
    payload = response.model_dump(mode="json", by_alias=True)
    assert len(payload["idea_route"]["proposals"]) == 3
    serialized = response.model_dump_json(by_alias=True)
    for rejected in original.idea_route.proposals[:failed_count]:
        assert rejected.id in serialized
        assert rejected.instrument_symbol is not None
        assert rejected.instrument_symbol in serialized


@pytest.mark.asyncio
async def test_composed_inspiration_reasons_survive_partial_preflight_response() -> None:
    original = _outcome()
    assert original.idea_route is not None
    proposals = tuple(replace(
        item, pairing_reason="研究联想，仅作为待验证假设。" * 18 + "已核实的选股依据。" * 8,
    ) for item in original.idea_route.proposals)
    original = replace(original, idea_route=replace(original.idea_route, proposals=proposals))

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        if strategy.instrument.symbol == _SYMBOLS[-1]:
            raise MxSaasProviderUnavailableError("fixture", reason="read_timeout")
        return object()

    prepared = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    response = _response(prepared)
    assert response.idea_route is not None
    assert len(response.idea_route.proposals) == 3
    assert response.idea_route.understanding.startswith(original.idea_route.understanding)
    assert len(response.idea_route.understanding) < 240
    assert response.idea_route.proposals[0].pairing_reason == proposals[0].pairing_reason
    assert len(response.idea_route.proposals[0].pairing_reason) > 160
    assert response.idea_route.understanding == original.idea_route.understanding
    assert not prepared.run_requested


@pytest.mark.asyncio
async def test_numeric_failure_preserves_strategy_intro_without_operational_diagnostics() -> None:
    original = _outcome()

    async def prepare(_strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        raise SkillNumericHistoryError("session_mismatch", "secret raw provider response")

    blocked = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    assert [p.strategy for p in blocked.idea_route.proposals] == [p.strategy for p in original.idea_route.proposals]
    assert blocked.clarification == original.idea_route.understanding
    assert "secret" not in blocked.clarification
    assert "解析" not in blocked.clarification
    assert not blocked.run_requested


@pytest.mark.asyncio
async def test_partial_numeric_failure_keeps_original_answer_and_choices() -> None:
    original = _outcome()

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        if strategy.instrument.symbol == _SYMBOLS[-1]:
            raise SkillNumericHistoryError("session_mismatch", "private upstream detail")
        return object()

    prepared = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    response = _response(prepared)
    assert response.idea_route is not None and len(response.idea_route.proposals) == 3
    assert response.clarification == original.idea_route.understanding
    assert "private upstream" not in response.clarification
    assert not prepared.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [
    (BacktestDataNotYetAvailableError("secret stale date detail"), "secret stale date detail"),
    (MxSaasProviderDataError("historical indicator unit is unconfirmed"), "单位或换算口径"),
    (MxSaasProviderDataError("historical indicator response contains duplicate dates"),
     "交易日不一致"),
    (MxSaasProviderNoDataError("secret raw provider response"), "所需的历史数据"),
    (MxSaasProviderDataError("secret raw provider response"), "格式或口径校验"),
])
async def test_provider_failure_does_not_replace_proposals_with_service_report(
    error: Exception, expected: str,
) -> None:
    original = _outcome()

    async def prepare(_strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        raise error

    blocked = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    assert [p.strategy for p in blocked.idea_route.proposals] == [p.strategy for p in original.idea_route.proposals]
    assert expected not in blocked.clarification
    assert blocked.clarification == original.idea_route.understanding
    assert "secret" not in blocked.clarification
    assert "解析" not in blocked.clarification
    assert not blocked.run_requested


@pytest.mark.asyncio
async def test_selected_strategy_does_not_recheck_unselected_siblings():
    original = _outcome()
    selected = original.idea_route.proposals[0]
    ready = replace(original, status=CompileStatus.READY, strategy=selected.strategy,
                    selected_idea_proposal=selected)
    prepare = AsyncMock(side_effect=BacktestDataNotYetAvailableError("stale sibling dates"))
    result = await _preflight_idea_choices(outcome=ready, container=_container(prepare))
    assert result is ready
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_failed_choices_remain_visible_without_automatic_execution() -> None:
    original = replace(_outcome(), run_requested=True, refresh_data=True)

    async def prepare(_strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        raise MxSaasProviderUnavailableError("fixture-private-detail", reason="read_timeout")

    blocked = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    assert [p.strategy for p in blocked.idea_route.proposals] == [p.strategy for p in original.idea_route.proposals]
    assert blocked.diagnostic_code == "candidate_data_incomplete"
    assert not blocked.run_requested and not blocked.refresh_data
    assert blocked.stock_recommendations == ()
    assert blocked.suggested_strategy is None
    assert blocked.execution_settings == original.execution_settings
    # A stale override must not restore optimistic execution suggestions.
    response = _response(blocked, override=original.idea_route)
    assert response.idea_route is not None
    assert response.idea_route.understanding == original.idea_route.understanding
    assert [item.assumptions for item in response.idea_route.proposals] == [
        item.assumptions for item in original.idea_route.proposals
    ]
    assert response.instrument_suggestions == ()
    assert response.suggested_strategy is None
    assert response.suggested_strategy_choice_id is None
    assert not response.run_requested
    assert original.idea_route is not None
    serialized = response.model_dump_json(by_alias=True)
    for proposal in original.idea_route.proposals:
        assert proposal.id in serialized
        assert proposal.instrument_symbol is not None
        assert proposal.instrument_symbol in serialized
        assert proposal.strategy_template is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered_count", [1, 3])
async def test_retry_after_temporary_failure_restores_selectable_prepared_choices(
    recovered_count: int,
) -> None:
    original = _outcome()
    recovered_symbols: set[str] = set()
    calls: list[StrategySpec] = []

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        calls.append(strategy)
        if strategy.instrument.symbol not in recovered_symbols:
            raise MxSaasProviderUnavailableError("fixture unavailable", reason="read_timeout")
        return object()

    container = _container(prepare)
    blocked = await _preflight_idea_choices(outcome=original, container=container)
    assert _response(blocked).idea_route is not None
    recovered_symbols.update(_SYMBOLS[:recovered_count])
    recovered = await _preflight_idea_choices(outcome=blocked, container=container)
    assert recovered.diagnostic_code == "idea_guidance_required"
    response = _response(recovered)
    assert response.idea_route is not None
    assert [item.instrument_symbol for item in response.idea_route.proposals] == list(
        _SYMBOLS,
    )
    assert len(calls) == 6
    assert not response.run_requested
    assert original.idea_route is not None and recovered.idea_route is not None
    assert recovered.idea_route.proposals[:recovered_count] == original.idea_route.proposals[:recovered_count]


@pytest.mark.asyncio
async def test_preflight_uses_existing_execution_settings_including_zero_and_false() -> None:
    settings = ExecutionSettingsPatch(
        slippage_bps=Decimal("0"), commission_rate=Decimal("0"),
        minimum_commission_cny=Decimal("0"), participation_rate=Decimal("0.03"),
        allocation_ratio=Decimal("0.6"), retry_unfilled_exits=False,
        max_exit_attempts=7, warmup_calendar_days=0, settlement_extension_days=3,
        run_robustness=False,
    )
    original = replace(_outcome(), execution_settings=settings)
    seen: list[BacktestRunConfig] = []

    async def prepare(_strategy: StrategySpec, config: BacktestRunConfig) -> object:
        seen.append(config)
        return object()

    prepared = await _preflight_idea_choices(outcome=original, container=_container(prepare))
    assert prepared is original
    assert len(seen) == 3
    for config in seen:
        for key, value in settings.model_dump(exclude_none=True).items():
            assert getattr(config, key) == value
        assert config.limit_handling == BacktestRunConfig().limit_handling
        assert config.capacity_mode == BacktestRunConfig().capacity_mode
    assert _response(prepared).execution_settings.slippage_bps == Decimal("0")


def _http_app(
    outcome: CompileOutcome, assessment: ClarificationDialogueAssessment,
    prepare: Callable[[StrategySpec, BacktestRunConfig], Awaitable[object]],
) -> FastAPI:
    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            return assessment

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(Path(__file__).parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(),
    )
    compiler.compile = AsyncMock(return_value=outcome)
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.VAGUE_STRATEGY)
    compiler.classify_dialogue_intent = AsyncMock(return_value=TurnIntent.SUPPLEMENT)
    compiler.compose_ready_response = AsyncMock(return_value="规则已准备好，可以核对。")
    app = create_app()
    app.state.container = replace(
        app.state.container, compiler=compiler,
        backtest_submission=cast(BacktestSubmitter, SimpleNamespace(prepare_candidate=prepare)),
    )
    return app


def test_hidden_candidates_accept_new_stock_without_reusing_old_symbols() -> None:
    calls: list[str] = []
    target = "300059.SZ"

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        calls.append(strategy.instrument.symbol)
        if strategy.instrument.symbol != target:
            raise MxSaasProviderUnavailableError("fixture unavailable", reason="read_timeout")
        return object()

    app = _http_app(_outcome(), ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="使用你指定的股票，保留原规则。",
        instrument_name="东方财富", instrument_selected=True,
    ), prepare)
    app.state.container.compiler.resolve_instrument_context = AsyncMock(return_value=target)
    with TestClient(app) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": "给我几个短线方案", "as_of_date": _END.isoformat(),
        }).json()
        assert created["diagnostic_code"] == "candidate_data_incomplete"
        calls.clear()
        response = client.post(
            f"/api/v1/strategy-drafts/{created['draft_id']}/revisions/"
            f"{created['revision']}/clarification-answers",
            json={"answer": "东方财富300059.SZ"},
        )
        assert response.status_code == 200, response.text
        draft = response.json()["draft"]
        assert calls and set(calls) == {target}
        assert draft["diagnostic_code"] == "idea_guidance_required"
        assert not draft.get("run_requested")
        assert all(item["instrument_symbol"] == target
                   for item in draft["idea_route"]["proposals"])


@pytest.mark.parametrize("entry", ["create", "clarification"])
@pytest.mark.parametrize("recovered_count", [1, 3])
def test_http_choice_checks_only_selected_data_and_recovery_keeps_cards(
    entry: str, recovered_count: int,
) -> None:
    calls: list[str] = []
    recovered: set[str] = set()

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        calls.append(strategy.instrument.symbol)
        if strategy.instrument.symbol not in recovered:
            raise MxSaasProviderUnavailableError("fixture unavailable", reason="read_timeout")
        return object()

    app = _http_app(_outcome(), ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="继续检查原候选的数据。",
    ), prepare)
    with TestClient(app) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": "给我几个短线方案", "as_of_date": _END.isoformat(),
        })
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["diagnostic_code"] == "candidate_data_incomplete"
        assert len(payload["idea_route"]["proposals"]) == 3 and len(calls) == 3

        def followup(answer: str) -> dict[str, object]:
            if entry == "create":
                result = client.post("/api/v1/strategy-drafts", headers={
                    "X-Conversation-Parent-Draft-ID": str(payload["draft_id"]),
                }, json={"utterance": answer, "as_of_date": _END.isoformat()})
                assert result.status_code == 201, result.text
                return result.json()
            result = client.post(
                f"/api/v1/strategy-drafts/{payload['draft_id']}/revisions/"
                f"{payload['revision']}/clarification-answers", json={"answer": answer},
            )
            assert result.status_code == 200, result.text
            assert all(item["id"] in {p["id"] for p in (result.json()["draft"].get("idea_route") or {}).get("proposals", [])}
                       for item in result.json()["suggestions"])
            return result.json()["draft"]

        payload = followup("第一个")
        assert payload["status"] == "needs_clarification"
        assert payload["strategy"] is None and payload["idea_route"] is None
        assert len(calls) == 4
        recovered.update(_SYMBOLS[:recovered_count])
        payload = followup("重试")
        assert payload["diagnostic_code"] == "idea_guidance_required"
        route = cast(dict[str, object], payload["idea_route"])
        assert len(cast(list[object], route["proposals"])) == 3
        assert len(calls) == 7 and not payload.get("run_requested")

        # Only the recovered card can now become a ready strategy.
        if entry == "create":
            result = client.post("/api/v1/strategy-drafts", headers={
                "X-Conversation-Parent-Draft-ID": str(payload["draft_id"]),
            }, json={"utterance": "第一个", "as_of_date": _END.isoformat()})
            assert result.status_code == 201, result.text
            ready = result.json()
        else:
            result = client.post(
                f"/api/v1/strategy-drafts/{payload['draft_id']}/revisions/"
                f"{payload['revision']}/clarification-answers", json={"answer": "第一个"},
            )
            assert result.status_code == 200, result.text
            ready = result.json()["draft"]
        assert ready["status"] == "ready"
        assert ready["strategy"]["instrument"]["symbol"] == _SYMBOLS[0]
        # The selected READY card also passes the final save boundary. In the
        # real service this reuses the already-prepared five-minute cache.
        assert len(calls) == 8


@pytest.mark.asyncio
async def test_hidden_choices_are_not_options_even_for_direct_compiler_calls() -> None:
    original = replace(_outcome(), diagnostic_code="candidate_data_not_ready")
    app = _http_app(original, ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="继续检查数据。",
    ), AsyncMock(return_value=object()))
    state = DialogueState.project(
        draft_id=uuid4(), revision=1,
        compile_input=CompileInput(utterance="短线策略", as_of_date=_END), outcome=original,
        created_at=_NOW, recent_turns=(),
    )
    assert state.available_option_ids == ()
    turn = await app.state.container.compiler.answer_clarification(
        original_input=state.compile_input, prior_outcome=original, answer="第一个",
    )
    assert turn.outcome is original and not turn.revision_changed and turn.suggestions == ()
    response = _to_response(StoredDraftRevision(
        draft_id=state.draft_id, revision=1, outcome=replace(
            original, stock_recommendations=(), suggested_strategy=None,
            suggested_strategy_hash=None, suggested_strategy_choice_id=None,
            suggested_strategy_note=None,
        ), compile_input=state.compile_input, created_at=_NOW,
        pending_instrument_reuse=VerifiedInstrumentMemory(
            symbol=_SYMBOLS[0], name="旧候选", source="fixture", verified_at=_NOW,
        ),
    ))
    assert response.instrument_suggestion is None


@pytest.mark.parametrize("proposal_count", [1, 3])
def test_http_explicit_reselection_uses_new_screen_and_retains_original_templates(
    proposal_count: int,
) -> None:
    original = _outcome()
    assert original.idea_route is not None
    original = replace(original, idea_route=replace(
        original.idea_route, proposals=original.idea_route.proposals[:proposal_count],
    ))
    new_symbols = ("300059.SZ", "600519.SH", "600030.SH")[:proposal_count]
    checked: list[StrategySpec] = []
    screened: list[str] = []

    async def prepare(strategy: StrategySpec, _config: BacktestRunConfig) -> object:
        checked.append(strategy)
        if strategy.instrument.symbol in _SYMBOLS:
            raise MxDailyHistoryBeforeListingError(start=_START, listing_date=date(2026, 5, 1))
        return object()

    class Data:
        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            screened.append(query)
            return LiveMarketDataResult(
                provider="fixture", query=query, asset_type=asset_type,
                columns=("代码", "名称"), rows=tuple(
                    {"代码": symbol, "名称": f"新样本{index}"}
                    for index, symbol in enumerate(new_symbols)
                ), provenance=LiveMarketDataProvenance("sha256:" + "1" * 64, _NOW, "fixture"),
            )

    class Advisor:
        async def pair_stock_strategies(
            self, utterance: str, result: LiveMarketDataResult,
            proposals: tuple[IdeaProposal, ...], understanding: str = "", **_kwargs: object,
        ) -> StockStrategyPairing:
            assert original.idea_route is not None
            assert [item.strategy_template for item in proposals] == [
                item.strategy_template for item in original.idea_route.proposals
            ]
            return StockStrategyPairing(understanding, tuple(
                StockStrategyPair(item.id, symbol, f"新样本{index}", "受控配对依据。")
                for index, (item, symbol) in enumerate(zip(proposals, new_symbols, strict=True))
            ))

    app = _http_app(original, ClarificationDialogueAssessment(
        reply_kind="question", acknowledgement_id="answer_question",
        natural_reply="保留原规则和区间，重新挑选股票。", instrument_recommendation_requested=True,
    ), prepare)
    app.state.container = replace(
        app.state.container, live_market_data=Data(),
        strategy_advisor=cast(VerifiedFactStrategyAdvisor, Advisor()),
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": "给我几个短线方案", "as_of_date": _END.isoformat(),
        }).json()
        assert created["diagnostic_code"] == "candidate_data_incomplete"
        app.state.container.compiler.classify_dialogue_intent = AsyncMock(
            return_value=TurnIntent.DATA_QUERY,
        )
        response = client.post(
            f"/api/v1/strategy-drafts/{created['draft_id']}/revisions/"
            f"{created['revision']}/clarification-answers", json={"answer": "重新选股"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()["draft"]
        assert len(screened) == 1 and len(checked) == proposal_count * 2
        assert [item["instrument_symbol"] for item in payload["idea_route"]["proposals"]] == list(
            new_symbols,
        )
        assert payload["diagnostic_code"] == "idea_guidance_required"
        for old, new in zip(checked[:proposal_count], checked[proposal_count:], strict=True):
            assert old.entry == new.entry and old.exit == new.exit and old.backtest == new.backtest
        assert not payload.get("run_requested")


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [
    "instrument_unconfirmed", "instrument_resolution_unavailable",
    "idea_instrument_extraction_unavailable",
])
async def test_identity_failure_explicit_recommendation_reopens_ideas_without_old_stock(
    diagnostic: str,
) -> None:
    failed = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code=diagnostic,
        clarification="股票身份暂未核对完成。",
    )
    app = _http_app(failed, ClarificationDialogueAssessment(
        reply_kind="question", acknowledgement_id="answer_question",
        natural_reply="保留短线风格，重新推荐股票。", instrument_recommendation_requested=True,
    ), AsyncMock(return_value=object()))
    compiler = app.state.container.compiler
    compiler.classify_dialogue_intent = AsyncMock(return_value=TurnIntent.DATA_QUERY)
    original = _outcome()
    assert original.idea_route is not None
    unbound = replace(original, stock_recommendations=(), idea_route=replace(
        original.idea_route, proposals=tuple(replace(
            item, instrument_symbol=None, instrument_name=None, strategy=None, strategy_hash=None,
        ) for item in original.idea_route.proposals),
    ))
    compiler.compile = AsyncMock(return_value=unbound)
    state = DialogueState.project(
        draft_id=uuid4(), revision=1,
        compile_input=CompileInput(utterance="东方财富短线策略", as_of_date=_END), outcome=failed,
        created_at=_NOW, recent_turns=(),
    )
    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state, answer="那帮我重新推荐股票",
    )
    turn = plan.clarification_turn
    assert turn is not None and turn.revision_changed
    assert turn.outcome is unbound and not turn.outcome.instrument_suggestion_declined
    assert turn.compile_input.instrument_context is None
    assert turn.compile_input.utterance == "那帮我重新推荐股票"
    assert turn.compile_input.idea_inspiration == state.compile_input.utterance
    compiler.compile.assert_awaited_once()
@pytest.mark.asyncio
async def test_minute_data_failure_is_not_mislabeled_as_bad_grid_parameters():
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError
    async def prepare(*args):
        raise MinuteReplayDataError('corporate_action_acquisition_unavailable')
    result = await _preflight_idea_choices(outcome=_outcome(), container=_container(prepare))
    reasons = str([p.assumptions for p in result.idea_route.proposals])
    assert result.diagnostic_code == 'candidate_data_incomplete'
    assert '数据准备' not in reasons
    assert '行情基准尚未对齐' not in reasons
