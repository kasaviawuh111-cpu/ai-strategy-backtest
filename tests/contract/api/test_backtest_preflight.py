"""HTTP boundaries with controlled data failures, not live-data acceptance."""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistoryBeforeListingError,
    MxDailyHistoryError,
    MxDailyHistoryFieldsMissingError,
)
from ashare_lab.adapters.market_data.mx_indicator_contract import UnsupportedSkillIndicatorError
from ashare_lab.adapters.market_data.mx_saas import (
    MxFailureReason,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
    MxTool,
)
from ashare_lab.api import backtest_preflight, create_app
from ashare_lab.api.container import ApiContainer
from ashare_lab.api.persistent_store import SQLAlchemyDraftStore
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.compile_strategy import (
    ClarificationTurnOutcome,
    CompileOutcome,
    CompileStatus,
)
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator, DialogueTurnPlan
from ashare_lab.application.skill_backtest_service import (
    SkillBacktestService,
    SkillCandidatePreparation,
    SkillCandidatePreparationError,
)
from ashare_lab.application.skill_numeric_catalog import extend_skill_numeric_catalogs
from ashare_lab.application.skill_numeric_history import SkillNumericHistoryError
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import (
    CatalogRef,
    EventCondition,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    StrategySpec,
    canonical_hash,
)
from ashare_lab.ports.backtest_runs import CreateRunResult
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchResult,
    ResearchFact,
    ResearchPurpose,
    ResearchSource,
)
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataProvenance

from .backtest_fakes import FakeRunStore, FakeSubmitter

_ROOT = Path(__file__).parents[3]
_TEXT = "东方财富收盘价上穿20日均线买入，持有10个交易日卖出"
_PRIVATE = "secret-token-never-log https://private.invalid/?key=secret-token-never-log"


@pytest.mark.asyncio
async def test_stale_end_date_keeps_selected_strategy_editable_without_shortening(monkeypatch):
    from ashare_lab.api.errors import ApiProblem
    outcome = CompileOutcome(status=CompileStatus.READY, strategy=_strategy(), run_requested=True)
    monkeypatch.setattr(backtest_preflight, "preflight_backtest_strategy", AsyncMock(
        side_effect=ApiProblem(status_code=422, code="backtest_data_not_yet_available",
                              message="当前行情已更新至2026-09-11，请调整结束日期。"),
    ))
    result = await backtest_preflight.preflight_ready_outcome(
        outcome=outcome, container=SimpleNamespace(),
        request=CompileInput(utterance=_TEXT, as_of_date=date(2026, 9, 14)),
    )
    assert result.status is CompileStatus.READY
    assert result.strategy is outcome.strategy
    assert not result.run_requested
    assert result.diagnostic_code == "backtest_data_not_yet_available"


@pytest.mark.asyncio
@pytest.mark.parametrize('verified', [True, False])
@pytest.mark.parametrize('too_early', [True, False])
async def test_range_proposal_requires_full_preflight_and_keeps_original(monkeypatch, verified, too_early):
    from datetime import timedelta
    from ashare_lab.api.errors import ApiProblem
    from ashare_lab.api.schemas import StrategyDraftResponse
    strategy = _strategy()
    latest = strategy.backtest.end - timedelta(days=1)
    proposed_start = strategy.backtest.start + timedelta(days=5) if too_early else strategy.backtest.start
    original_error = ApiProblem(status_code=422,
        code='skill_history_before_listing' if too_early else 'backtest_data_not_yet_available',
        message='范围不足', available_start=proposed_start if too_early else None)
    smaller_error = ApiProblem(status_code=503, code='backtest_data_temporarily_unavailable', message='接口失败')
    prepare = AsyncMock(side_effect=[original_error, None if verified else smaller_error])
    monkeypatch.setattr(backtest_preflight, 'preflight_backtest_strategy', prepare)
    outcome = CompileOutcome(status=CompileStatus.READY, strategy=strategy,
        strategy_hash=canonical_hash(strategy), run_requested=True)
    container = SimpleNamespace(backtest_submission=SimpleNamespace(available_data_end=lambda: latest))
    if too_early and not verified:
        with pytest.raises(ApiProblem, match='范围不足'):
            await backtest_preflight.preflight_ready_outcome(outcome=outcome, container=container)
        assert outcome.strategy == strategy
        assert outcome.suggested_strategy is None
        return
    result = await backtest_preflight.preflight_ready_outcome(outcome=outcome, container=container)
    assert prepare.await_count == 2
    assert not result.run_requested
    assert strategy.backtest.end != latest
    if verified:
        assert result.status is CompileStatus.NEEDS_CLARIFICATION
        assert result.strategy is None and result.revision_base_strategy == strategy
        assert result.suggested_strategy.backtest.end == latest
        assert result.suggested_strategy.backtest.start == proposed_start
        assert result.suggested_strategy.entry == strategy.entry
        assert result.suggested_strategy.exit == strategy.exit
        assert result.diagnostic_code == 'backtest_range_confirmation_required'
        assert '是否接受' in result.clarification
        from tests.contract.api.test_candidate_preflight import _response
        from ashare_lab.adapters.language import RuleBasedCandidateGenerator
        from ashare_lab.application.compile_strategy import StrategyCompiler
        assert _response(result).suggested_strategy == result.suggested_strategy
        catalog = load_catalog_directory(_ROOT / 'catalogs')
        manifest = next(m for m in catalog.manifests if m.catalog_id == 'cn_a.signals')
        compiler = StrategyCompiler(generator=RuleBasedCandidateGenerator(), catalog=catalog,
            catalog_id=manifest.catalog_id, release_version=manifest.release_version)
        accepted = await compiler.answer_clarification(
            original_input=CompileInput(utterance=_TEXT, as_of_date=strategy.backtest.end),
            prior_outcome=result, answer='接受建议范围')
        assert accepted.outcome.status is CompileStatus.READY
        assert accepted.outcome.strategy.backtest.end == latest
        assert accepted.outcome.strategy.entry == strategy.entry
        assert not accepted.outcome.run_requested
    else:
        assert result.strategy == strategy and result.suggested_strategy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", [
    "skill_numeric_history_unavailable_after_query_retry",
    "skill_numeric_invalid_history", "skill_numeric_non_daily_history",
])
async def test_numeric_history_gap_preserves_rules_without_irrelevant_search(monkeypatch, failure_code):
    from ashare_lab.api.errors import ApiProblem

    outcome = CompileOutcome(status=CompileStatus.READY, strategy=_strategy())
    request = CompileInput(utterance=_TEXT, as_of_date=date(2026, 9, 7))
    fallback = replace(outcome, status=CompileStatus.NEEDS_CLARIFICATION,
                       diagnostic_code="capability_research_fallback")
    researcher = AsyncMock(return_value=fallback)
    container = SimpleNamespace(compiler=SimpleNamespace(research_data_gap=researcher))
    monkeypatch.setattr(backtest_preflight, "preflight_backtest_strategy", AsyncMock(
        side_effect=ApiProblem(status_code=503,
            code=failure_code, message="无历史数据"),
    ))
    result = await backtest_preflight.preflight_ready_outcome(
        outcome=outcome, container=container, request=request,
    )
    assert result.status is CompileStatus.NEEDS_CLARIFICATION
    assert result.diagnostic_code == failure_code
    assert result.revision_base_strategy == outcome.strategy
    assert result.strategy is None and not result.run_requested
    assert "无历史数据" in result.clarification
    researcher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data_state", ["events", "network", "irrelevant"])
async def test_condition_gap_reads_skill_before_optional_web(monkeypatch, data_state):
    from ashare_lab.api.errors import ApiProblem

    app = create_app()
    compiler = app.state.container.compiler
    outcome = replace(_ready(), run_requested=True)
    outcome = replace(outcome, strategy=outcome.strategy.model_copy(update={
        "entry": EventCondition(
            event_code="event.shareholder_holdings.major_holder_increase_plan",
            definition_version="v1",
        ),
    }))
    request = CompileInput(utterance="东方财富大股东增持公告后买入", as_of_date=date(2026, 9, 7))
    result = LiveFinanceDataResult(
        provider="eastmoney_mx_screener", query=request.utterance, indicators=None,
        tables=({"event_tables": [{"full": False, "data": [{
            "公告日期": "2026-09-01", "股东名称": "测试股东", "方向": "减持",
        }]}]},),
        provenance=LiveMarketDataProvenance(
            response_sha256="sha256:fixture", retrieved_at=datetime.now(UTC), schema_version="test",
        ),
    )
    if data_state == "irrelevant":
        result = replace(result, tables=({"title": "区间行情", "rows": [{"最高价": 28.36}]},))
    lookup = AsyncMock(return_value=result)
    if data_state == "network":
        lookup.side_effect = TimeoutError("private-provider-detail")
    search = AsyncMock(side_effect=RuntimeError("no web fixture"))
    monkeypatch.setattr(compiler, "_current_fact_researcher", SimpleNamespace(research=search))
    reply = AsyncMock(return_value="已查到减持记录，不能作为原增持条件；原规则保留。")
    monkeypatch.setattr(compiler, "compose_dialogue_response", reply)
    container = SimpleNamespace(
        compiler=compiler,
        coverage_catalog=app.state.container.coverage_catalog,
        live_finance_data=SimpleNamespace(query_current_finance=lookup, query_finance=lookup),
    )
    monkeypatch.setattr(backtest_preflight, "preflight_backtest_strategy", AsyncMock(
        side_effect=ApiProblem(status_code=503, code="backtest_condition_data_unavailable",
                              message="事件历史尚未接入"),
    ))
    recovered = await backtest_preflight.preflight_ready_outcome(
        outcome=outcome, request=request, container=container,
    )
    lookup.assert_awaited_once()
    assert outcome.strategy.instrument.symbol in lookup.call_args.kwargs["query"]
    assert "公告" in lookup.call_args.kwargs["indicators"]
    assert "不要股价行情" in lookup.call_args.kwargs["query"]
    assert recovered.revision_base_strategy == outcome.strategy
    assert recovered.strategy is None and not recovered.run_requested
    if data_state == "events":
        search.assert_not_awaited()
        assert "减持" in reply.call_args.kwargs["context"]
        assert "full" in reply.call_args.kwargs["context"]
        assert "身份可能重叠" in reply.call_args.kwargs["context"]
        assert "公告事件历史尚未接入回测执行链路" in reply.call_args.kwargs["context"]
        assert "公告事件历史尚未接入回测执行链路" in reply.call_args.kwargs["fallback_reply"]
        assert str(outcome.strategy.backtest.start) in reply.call_args.kwargs["context"]
        assert str(outcome.strategy.backtest.end) in reply.call_args.kwargs["context"]
        assert "减持" in recovered.clarification
    else:
        search.assert_awaited_once()
        reply.assert_not_awaited()
        assert "private-provider-detail" not in recovered.clarification


class _PreparingService(SkillBacktestService):
    """No worker/provider; only emulate the service boundary used by HTTP."""

    def __init__(self) -> None:
        self.fake = FakeSubmitter(FakeRunStore())
        self.failure: Exception | None = None
        self.prepared_configs: list[BacktestRunConfig] = []
        self.prepared_submissions: list[bool] = []
        self.delay = 0.0

    async def prepare_candidate(
        self, strategy: StrategySpec, config: BacktestRunConfig,
    ) -> SkillCandidatePreparation:
        self.prepared_configs.append(config)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return cast(SkillCandidatePreparation, object())

    def submit(
        self, strategy: StrategySpec, config: BacktestRunConfig, *, prepared_inputs: bool = False,
    ) -> CreateRunResult:
        self.prepared_submissions.append(prepared_inputs)
        return self.fake.submit(strategy, config)


def _strategy() -> StrategySpec:
    return StrategySpec.model_validate_json(
        (_ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )


def _ready(strategy: StrategySpec | None = None) -> CompileOutcome:
    spec = strategy or _strategy()
    return CompileOutcome(status=CompileStatus.READY, strategy=spec,
                          strategy_hash=canonical_hash(spec))


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_available", [True, False])
async def test_latest_grid_preflight_pins_quote_or_preserves_unresolved_plan(monkeypatch, quote_available):
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    from tests.unit.adapters.test_mx_grid_anchor import response

    strategy = _strategy().model_copy(update={"entry": None, "exit": None,
        "trading_plan": GridPlan(parameters=GridParameters(
            anchor_mode="latest_price", lower_price=1, upper_price=200,
            spacing_mode="anchor_percent", spacing=1))})
    lookup = AsyncMock(return_value=response(code=strategy.instrument.symbol),
                       side_effect=None if quote_available else ValueError("no quote"))
    prepare = AsyncMock()
    monkeypatch.setattr(backtest_preflight, "preflight_backtest_strategy", prepare)
    container = SimpleNamespace(live_finance_data=SimpleNamespace(query_finance=lookup))
    outcome = await backtest_preflight.preflight_ready_outcome(
        outcome=_ready(strategy), container=container,
        request=CompileInput(utterance="指南针网格基准价按行情最新价", as_of_date=date(2026, 9, 11)))
    if quote_available:
        assert outcome.status is CompileStatus.READY
        assert str(outcome.strategy.trading_plan.parameters.anchor_price) == "79.32"
        assert outcome.strategy_hash == canonical_hash(outcome.strategy)
        assert outcome.strategy_hash != canonical_hash(strategy)
        assert prepare.await_args.kwargs["strategy"] == outcome.strategy
    else:
        assert outcome.diagnostic_code == "grid_latest_quote_unavailable"
        assert outcome.strategy is None
        assert outcome.revision_base_strategy == strategy
        assert "不会改用起始开盘价" in outcome.clarification
        prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("screen_available", [True, False])
async def test_latest_grid_requeries_wrong_metric_without_substituting_close(monkeypatch, screen_available):
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    from tests.unit.adapters.test_mx_grid_anchor import response, screen_response

    strategy = _strategy().model_copy(update={"entry": None, "exit": None,
        "trading_plan": GridPlan(parameters=GridParameters(
            anchor_mode="latest_price", lower_price=1, upper_price=200,
            spacing_mode="anchor_percent", spacing=1))})
    symbol = strategy.instrument.symbol
    lookup = AsyncMock(return_value=response(code=symbol, field="CLOSE", label="收盘价", value="100"))
    screen = AsyncMock(return_value=screen_response(code=symbol),
                       side_effect=None if screen_available else ValueError("no quote"))
    prepare = AsyncMock()
    monkeypatch.setattr(backtest_preflight, "preflight_backtest_strategy", prepare)
    container = SimpleNamespace(live_finance_data=SimpleNamespace(query_finance=lookup, screen=screen))
    outcome = await backtest_preflight.preflight_ready_outcome(
        outcome=_ready(strategy), container=container,
        request=CompileInput(utterance="网格基准价按行情最新价", as_of_date=date(2026, 9, 11)))
    lookup.assert_awaited_once()
    screen.assert_awaited_once_with(query=f"{symbol}行情最新价", asset_type="A股")
    if screen_available:
        assert outcome.status is CompileStatus.READY
        assert str(outcome.strategy.trading_plan.parameters.anchor_price) == "79.32"
        assert outcome.strategy.trading_plan.parameters.anchor_quote_source == "eastmoney_mx_screener"
        assert outcome.strategy_hash == canonical_hash(outcome.strategy)
        assert prepare.await_args.kwargs["strategy"] == outcome.strategy
    else:
        assert outcome.diagnostic_code == "grid_latest_quote_unavailable"
        assert outcome.revision_base_strategy == strategy
        prepare.assert_not_awaited()


@pytest.mark.parametrize("entry", ["create", "clarification"])
@pytest.mark.parametrize("code", ["unit_unconfirmed", "query_failed", "field_unconfirmed"])
def test_preflight_data_failure_persists_accepted_rules_not_old_question(
    entry, code, monkeypatch, tmp_path, caplog,
):
    service = _PreparingService()
    service.numeric_series_enabled = True
    engine = create_engine(f"sqlite:///{tmp_path / 'drafts.db'}")
    store = SQLAlchemyDraftStore(engine, initialize_schema=True)
    catalog, coverage = extend_skill_numeric_catalogs(
        load_catalog_directory(_ROOT / "catalogs"),
        load_coverage_catalog_directory(_ROOT / "catalogs/coverage"),
    )
    app = create_app(backtest_submission=service, run_store=service.fake.store, draft_store=store,
                     catalog=catalog, coverage_catalog=coverage)
    container = app.state.container
    compiler = container.compiler
    numeric = IndicatorCondition(
        indicator_id="provider.numeric", definition_version="1.0.0", trigger="above", value=0,
        params={"metric_query": "炸板次数", "unit": "次"},
    )
    manifest = next(item for item in container.catalog.manifests
                    if item.catalog_id == "cn_a.signals")
    strategy = _strategy().model_copy(update={
        "entry": numeric,
        "catalog": CatalogRef(catalog_id=manifest.catalog_id,
                              release_version=manifest.release_version),
    })
    original = replace(_ready(strategy), run_requested=True, refresh_data=True,
                       pending_edit_inputs=("旧条件问题",), pending_edit_run_requested=True)
    current_input = CompileInput(utterance=_TEXT, as_of_date=date(2026, 9, 7))
    compiler.compile = AsyncMock(return_value=original)
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    compiler.compose_ready_response = AsyncMock(return_value="规则已准备好。")
    compiler.research_data_gap = AsyncMock(side_effect=AssertionError("not a missing-data search"))
    failure = SkillNumericHistoryError(code, _PRIVATE)
    failure.indicator_id = numeric.indicator_id
    failure.condition_path = "entry.$"
    failure.metric_query = "炸板次数"
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    try:
        with TestClient(app) as client:
            if entry == "clarification":
                compiler.compile.return_value = CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    diagnostic_code="strategy_edit_clarification",
                    clarification="旧条件问题", revision_base_strategy=_strategy(),
                    pending_edit_inputs=("旧条件问题",),
                )
                first = client.post("/api/v1/strategy-drafts", json={
                    "utterance": "原来的规则", "instrument_context": "300059.SZ",
                    "as_of_date": "2026-09-07",
                })
                assert first.status_code == 201, first.text
                first = first.json()
                monkeypatch.setattr(DialogueTurnOrchestrator, "plan", AsyncMock(
                    return_value=DialogueTurnPlan(
                        intent=TurnIntent.SUPPLEMENT, clarification_turn=ClarificationTurnOutcome(
                            reply_kind="accepted", assistant_message="旧的准备好回复",
                            outcome=original, compile_input=current_input, revision_changed=True,
                        ),
                    ),
                ))
                url = (f"/api/v1/strategy-drafts/{first['draft_id']}/revisions/1"
                       "/clarification-answers")
                payload = {"answer": "收盘价是否达到当日涨停价"}
            else:
                url = "/api/v1/strategy-drafts"
                payload = {"utterance": _TEXT, "as_of_date": "2026-09-07"}
            service.failure = failure
            response = client.post(url, json=payload)
            assert response.status_code == (200 if entry == "clarification" else 201), response.text
            draft = response.json()["draft"] if entry == "clarification" else response.json()
            assert draft["status"] == "needs_clarification"
            assert draft["diagnostic_code"] == f"skill_numeric_{code}"
            assert draft["strategy"] is None and draft["strategy_hash"] is None
            assessment = draft["execution_assessment"]
            assert assessment["status"] == ("temporarily_unavailable" if code == "query_failed"
                                            else "understood_not_executable")
            assert assessment["interpreted_strategy"] == strategy.model_dump(mode="json")
            assert assessment["strategy_hash"] == canonical_hash(strategy)
            assert assessment["missing"]
            assert not draft.get("run_requested") and not draft.get("refresh_data")
            message = response.json()["assistant_message"]
            assert "买入条件「炸板次数」" in message
            assert "旧条件问题" not in message and "旧的准备好回复" not in message
            if code == "unit_unconfirmed":
                assert "比较单位是「次」" in message and "尚未确认" in message
                assert "未取得可用数据" not in message
            assert draft["verified_instrument"]["symbol"] == "300059.SZ"
            saved = asyncio.run(store.load_latest_dialogue_state(draft_id=UUID(draft["draft_id"])))
            assert saved.outcome.revision_base_strategy == strategy
            assert saved.compile_input.utterance == _TEXT
            assert saved.compile_input.instrument_context == "300059.SZ"
            assert saved.outcome.pending_edit_inputs == ()
            assert saved.outcome.edit_clarification_options == ()
            assert not saved.outcome.pending_edit_run_requested
            if entry == "clarification":
                assert saved.revision == 2
                assert saved.recent_turns[-1].user_text == payload["answer"]
            compiler.research_data_gap.assert_not_awaited()
            for path in ("/api/v1/backtest-runs/prepare", "/api/v1/backtest-runs"):
                blocked = client.post(path, json={"strategy": strategy.model_dump(mode="json")})
                assert blocked.status_code == 503, blocked.text
                assert blocked.json()["error"]["details"][0]["location"] == "/entry"
            assert service.fake.configs == [] and service.fake.store.records == {}
            log = next(record.getMessage() for record in caplog.records
                       if record.getMessage().startswith("backtest_preflight "))
            metadata = json.loads(log.split(" failure=", 1)[1])
            assert metadata["metric_query"] == "炸板次数"
            assert metadata["condition_path"] == "/entry"
            assert metadata["comparison_unit"] == "次"
            assert _PRIVATE not in caplog.text + response.text
    finally:
        engine.dispose()


@pytest.mark.parametrize("reply_failure", ["timeout", "connection_failed", "empty"])
def test_optional_ready_reply_failure_preserves_verified_strategy_and_http201(
    monkeypatch: pytest.MonkeyPatch, reply_failure: str,
) -> None:
    from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError

    service = _PreparingService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    container = cast(ApiContainer, app.state.container)
    compiler = container.compiler
    original = _ready()
    compiler.compile = AsyncMock(return_value=original)
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    dialogue = SimpleNamespace(assess=AsyncMock(return_value=None))
    if reply_failure != "empty":
        dialogue.assess.side_effect = CandidateTransportError(
            "private-provider-detail", failure_kind=reply_failure,
        )
    monkeypatch.setattr(compiler, "_clarification_dialogue_router", dialogue)
    with TestClient(app) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": _TEXT, "as_of_date": "2026-09-07",
        })
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["status"] == "ready"
    assert draft["strategy"] == original.strategy.model_dump(mode="json")
    assert draft["strategy_hash"] == original.strategy_hash
    assert draft["assistant_message"] == "买卖规则已准备好，可以核对；本次尚未执行回测。"
    assert not draft.get("run_requested")
    assert service.prepared_configs and service.fake.configs == []
    saved = asyncio.run(container.drafts.load_latest_dialogue_state(
        draft_id=UUID(draft["draft_id"]),
    ))
    assert saved.outcome.strategy == original.strategy
    assert saved.outcome.strategy_hash == original.strategy_hash
    assert "private-provider-detail" not in response.text


@pytest.mark.parametrize("research_state", ["sources", "empty", "unavailable", "unconfigured"])
def test_history_gap_research_preserves_draft_without_execution(
    monkeypatch: pytest.MonkeyPatch, research_state: str,
) -> None:
    service = _PreparingService()
    service.failure = SkillNumericHistoryError("invalid_history", "private-provider-detail")
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    container = cast(ApiContainer, app.state.container)
    compiler = container.compiler
    original = replace(_ready(), run_requested=True, refresh_data=True)
    compiler.compile = AsyncMock(return_value=original)
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    summary = "公告分析。" * 100  # A valid 500-character research summary.
    now = datetime(2026, 9, 7, tzinfo=UTC)
    research = CurrentFactResearchResult(
        provider="fixture", model="fixture", provider_response_id="research-long-summary",
        query=_TEXT, purpose=ResearchPurpose.CURRENT_FACT, as_of=now,
        summary=summary,
        facts=(ResearchFact("保留已取得的公告事实。", "reported_fact", ("source_1",), None),),
        sources=(ResearchSource("source_1", "公司公告", "https://example.com/notice",
                                "公告来源", "2026-09-07"),),
        unresolved_questions=(), retrieved_at=now, response_sha256="1" * 64,
        search_call_count=1,
    )
    if research_state == "empty":
        research = replace(research, sources=())
    researcher = SimpleNamespace(research=AsyncMock(return_value=research))
    if research_state == "unavailable":
        researcher.research.side_effect = RuntimeError("private-provider-detail")
    monkeypatch.setattr(compiler, "_current_fact_researcher",
                        None if research_state == "unconfigured" else researcher)
    with TestClient(app) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": _TEXT, "as_of_date": "2026-09-07",
        })
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["status"] == "needs_clarification"
    assert draft["diagnostic_code"] == "skill_numeric_invalid_history"
    assert draft["strategy"] is None and draft["strategy_hash"] is None
    assert not draft.get("run_requested") and not draft.get("refresh_data")
    assert draft["idea_route"] is None
    assert "已识别并保留" in draft["assistant_message"]
    assert summary not in response.text
    assert "private-provider-detail" not in response.text
    researcher.research.assert_not_awaited()
    saved = asyncio.run(container.drafts.load_latest_dialogue_state(
        draft_id=UUID(draft["draft_id"]),
    ))
    assert saved.outcome.revision_base_strategy == original.strategy
    assert saved.outcome.strategy is None
    assert service.fake.configs == [] and service.fake.store.records == {}
    researcher.research.assert_not_awaited()


@pytest.mark.parametrize(("failure", "status", "code", "text"), [
    (MxDailyHistoryBeforeListingError(start=date(2025, 1, 1), listing_date=date(2026, 3, 1)),
     422, "skill_history_before_listing", "2026-03-01"),
    (SkillCandidatePreparationError("skill_indicator_history_not_ready", "private",
                                    condition_path="entry.root"),
     422, "skill_indicator_history_not_ready", "买入条件"),
    (SkillCandidatePreparationError("skill_history_no_execution_session", "private"),
     422, "skill_history_no_execution_session", "足够交易日"),
    (MxDailyHistoryFieldsMissingError(("private-token",)),
     503, "skill_history_fields_missing", "完整历史行情字段"),
    (MxDailyHistoryError("private-token"), 503, "skill_history_incomplete", "日期或数据不完整"),
    (MxSaasProviderUnavailableError("private-token", reason="read_timeout"),
     503, "backtest_data_temporarily_unavailable", "这次没能取到回测所需的行情"),
    (MxSaasProviderUnavailableError("private-token", reason="connect_timeout", tool="searchData"),
     503, "backtest_data_temporarily_unavailable", "这次没能取到回测所需的行情"),
    (MxSaasProviderUnavailableError("private-token", reason="transport_error",
                                    tool="selectSecurity"),
     503, "backtest_data_temporarily_unavailable", "这次没能取到回测所需的行情"),
    (MxSaasProviderAuthError("private-token", tool="searchData"),
     503, "backtest_data_temporarily_unavailable", "这次没能取到回测所需的行情"),
    (MxSaasProviderDataError("historical indicator unit is unconfirmed or incompatible",
                             tool="searchData"),
     503, "backtest_data_temporarily_unavailable", "有些指标的单位还无法确认"),
    (MxSaasProviderNoDataError("private-token", tool="selectSecurity"),
     503, "backtest_data_temporarily_unavailable", "暂时没有这段时间的完整行情"),
    (ValueError("private-token"),
     503, "backtest_data_temporarily_unavailable", "这次没能完成回测"),
    (MinuteReplayDataError("corporate_action_coverage_insufficient"),
     503, "backtest_data_temporarily_unavailable", "分红送转数据尚未覆盖指标预热期"),
        (TimeoutError("private-token"), 503, "backtest_data_preparation_timeout", "这次没能取到回测所需的行情"),
    (SkillNumericHistoryError("history_unavailable_after_query_retry", "private-token"),
     503, "skill_numeric_history_unavailable_after_query_retry", "逐日历史数据"),
])
def test_prepare_and_submit_fail_before_run_creation_then_recover(
    failure: Exception, status: int, code: str, text: str,
) -> None:
    service = _PreparingService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    request = {"strategy": _strategy().model_dump(mode="json")}
    with TestClient(app) as client:
        service.failure = failure
        for url in ("/api/v1/backtest-runs/prepare", "/api/v1/backtest-runs"):
            failed = client.post(url, json=request)
            assert failed.status_code == status, failed.text
            assert failed.json()["error"]["code"] == code
            assert text in failed.json()["error"]["message"]
            assert "private" not in failed.text
            if isinstance(failure, (MxSaasProviderDataError, ValueError)):
                assert "网络异常" not in failed.json()["error"]["message"]
            if isinstance(failure, MxDailyHistoryBeforeListingError):
                # The adapter may report its earlier warmup request date.
                # Explain the user's selected range, not that internal date.
                assert _strategy().backtest.start.isoformat() in failed.json()["error"]["message"]
        assert service.fake.configs == [] and service.fake.store.records == {}
        service.failure = None
        prepared = client.post("/api/v1/backtest-runs/prepare", json=request)
        assert prepared.status_code == 200 and prepared.json() == {"ready": True}
        assert service.fake.configs == [] and service.fake.store.records == {}
        submitted = client.post("/api/v1/backtest-runs", json=request)
        assert submitted.status_code == 202, submitted.text
        assert submitted.json()["state"] == "queued"
        assert len(service.fake.configs) == 1 and len(service.fake.store.records) == 1


@pytest.mark.parametrize(("case", "indicator_id", "path", "source_path"), [
    ("entry", "technical.macd", "entry.$.children[0]", "/entry/children/0"),
    ("single_exit", "technical.macd", "exit.$", "/exit/children/1"),
    ("multiple_exit", "market.volume", "exit.$.children[1]", "/exit/children/2"),
])
def test_prepare_warmup_details_map_runtime_leaf_to_source_pointer(
    case, indicator_id, path, source_path,
):
    strategy = _strategy()
    if case != "entry":
        children = [HoldingPeriodExit(sessions=30), *strategy.exit.children]
        if case == "multiple_exit":
            children.append(strategy.entry.children[1])
        strategy = strategy.model_copy(update={"exit": FirstOfExit(children=tuple(children))})
    service = _PreparingService()
    service.failure = SkillCandidatePreparationError(
        "skill_indicator_history_not_ready", _PRIVATE,
        indicator_id=indicator_id, condition_path=path,
    )
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        for endpoint in ("/api/v1/backtest-runs/prepare", "/api/v1/backtest-runs"):
            response = client.post(endpoint, json={"strategy": strategy.model_dump(mode="json")})
            assert response.status_code == 422, response.text
            assert response.json()["error"]["details"] == [{
                "location": source_path, "type": "backtest_condition_unavailable",
                "message": "这条条件在本次区间内尚无可用于后续交易日执行的有效历史指标值，"
                           "可能尚未完成预热。",
            }]
            assert _PRIVATE not in response.text
    assert service.fake.configs == [] and service.fake.store.records == {}


def test_prepare_unsupported_indicator_lists_all_matching_rule_locations():
    service = _PreparingService()
    service.failure = UnsupportedSkillIndicatorError("technical.macd", _PRIVATE)
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs/prepare", json={
            "strategy": _strategy().model_dump(mode="json"),
        })
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [{
        "location": path, "type": "backtest_condition_unavailable",
        "message": "这条条件目前没有可执行的历史指标数据链路。",
    } for path in ("/entry/children/0", "/exit/children/0")]
    assert _PRIVATE not in response.text


def test_shared_runtime_leaf_without_unique_source_location_falls_back_to_leg():
    strategy = _strategy()
    leaf = strategy.exit.children[0]
    strategy = strategy.model_copy(update={"exit": FirstOfExit(children=(leaf, leaf))})
    details = backtest_preflight._condition_failure_details(
        strategy, indicator_id="technical.macd", condition_path="exit.$.children[0]", warmup=True,
    )
    assert [detail.location for detail in details] == ["exit"]


@pytest.mark.parametrize(("failure", "location"), [
    (UnsupportedSkillIndicatorError(_PRIVATE, _PRIVATE), "strategy"),
    (SkillCandidatePreparationError(
        "skill_indicator_history_not_ready", _PRIVATE,
        indicator_id="technical.macd", condition_path=_PRIVATE,
    ), "strategy"),
    (SkillCandidatePreparationError(
        "skill_indicator_history_not_ready", _PRIVATE,
        indicator_id="market.volume", condition_path="entry.$.children[0]",
    ), "entry"),
])
def test_prepare_unmatched_or_conflicting_rule_context_never_invents_location(failure, location):
    service = _PreparingService()
    service.failure = failure
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs/prepare", json={
            "strategy": _strategy().model_dump(mode="json"),
        })
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [{
        "location": location, "type": "backtest_condition_unavailable",
        "message": "当前策略的历史指标数据尚不满足本次回测要求。",
    }]
    assert _PRIVATE not in response.text


def test_missing_field_details_whitelist_fields_and_name_fetch_range_not_backtest_range():
    service = _PreparingService()
    service.failure = MxDailyHistoryFieldsMissingError(
        ("跌停价", _PRIVATE, "成交量", "跌停价"),
        start=date(2026, 1, 1), end=date(2026, 3, 1),
    )
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs/prepare", json={
            "strategy": _strategy().model_dump(mode="json"),
        })
    assert response.status_code == 503
    assert response.json()["error"]["details"] == [{
        "location": "market_history", "type": "backtest_data_missing",
        "message": "取数区间 2026-01-01 至 2026-03-01：未取得跌停价、成交量的完整历史数据。",
    }]
    assert _PRIVATE not in response.text


def test_listing_details_use_requested_start_not_warmup_start():
    service = _PreparingService()
    service.failure = MxDailyHistoryBeforeListingError(
        start=date(2020, 1, 1), listing_date=date(2022, 1, 1),
    )
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs/prepare", json={
            "strategy": _strategy().model_dump(mode="json"),
        })
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [{
        "location": "backtest.start", "type": "backtest_date_unavailable",
        "message": "所选回测起点 2021-01-01 早于已核实上市日期 2022-01-01，"
                   "该段没有上市后的交易历史。",
    }]
    assert "2020-01-01" not in response.text


def test_preflight_timeout_is_bounded_and_does_not_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest_preflight, "_TIMEOUT_SECONDS", 0.01)
    service = _PreparingService()
    service.delay = 0.1
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs", json={
            "strategy": _strategy().model_dump(mode="json"),
        })
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backtest_data_preparation_timeout"
    assert service.fake.configs == [] and service.fake.store.records == {}


def test_submission_recovers_connection_once_without_duplicate_run() -> None:
    class RecoveringService(_PreparingService):
        async def prepare_candidate(self, strategy, config):
            self.failure = (MxSaasProviderUnavailableError(
                "private-connection-details", reason="connect_timeout",
            ) if not self.prepared_configs else None)
            return await super().prepare_candidate(strategy, config)

    service = RecoveringService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post('/api/v1/backtest-runs', json={
            'strategy': _strategy().model_dump(mode='json'),
        })
    assert response.status_code == 202
    assert len(service.prepared_configs) == 2
    assert service.prepared_configs[0] == service.prepared_configs[1]
    assert service.prepared_submissions == [True]
    assert len(service.fake.store.records) == 1


def test_persistent_connection_failure_stops_after_one_recovery() -> None:
    service = _PreparingService()
    service.failure = MxSaasProviderUnavailableError("private", reason="transport_error")
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post('/api/v1/backtest-runs', json={
            'strategy': _strategy().model_dump(mode='json'),
        })
    assert response.status_code == 503
    assert len(service.prepared_configs) == 2
    assert service.prepared_submissions == []
    assert service.fake.store.records == {}
    assert "private" not in response.text


def test_explicit_refresh_retains_audit_config_and_marks_prepared_submission() -> None:
    service = _PreparingService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs", json={
            "strategy": _strategy().model_dump(mode="json"),
            "config": {"refreshData": True, "warmupCalendarDays": 365, "slippageBps": "8"},
        })
    assert response.status_code == 202, response.text
    prepared, submitted = service.prepared_configs[0], service.fake.configs[0]
    assert prepared.refresh_data and submitted == prepared
    assert service.prepared_submissions == [True]


def test_ready_check_preserves_refresh_intent_for_final_submission() -> None:
    service = _PreparingService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    compiler = cast(ApiContainer, app.state.container).compiler
    compiler.compile = AsyncMock(return_value=replace(_ready(), run_requested=True,
                                                     refresh_data=True))
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    compiler.compose_ready_response = AsyncMock(return_value="规则已准备好。")
    with TestClient(app) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": _TEXT, "as_of_date": "2026-09-04",
        })
        assert created.status_code == 201, created.text
        draft = created.json()
        assert draft["refresh_data"] is True
        assert service.prepared_configs[0].refresh_data is False
        assert service.fake.configs == []
        submitted = client.post("/api/v1/backtest-runs", json={
            "strategy": draft["strategy"], "config": {"refreshData": draft["refresh_data"]},
        })
        assert submitted.status_code == 202, submitted.text
    assert [config.refresh_data for config in service.prepared_configs] == [False, True]
    assert service.fake.configs[0].refresh_data is True
    assert service.prepared_submissions == [True]


@pytest.mark.parametrize("entry", ["create", "revision", "clarification"])
def test_ready_strategy_is_not_saved_until_inputs_are_ready(
    entry: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _PreparingService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    container = cast(ApiContainer, app.state.container)
    compiler = container.compiler
    compiler.compile = AsyncMock(return_value=_ready())
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    compiler.compose_ready_response = AsyncMock(return_value="规则已准备好。")
    create_spy = AsyncMock(wraps=container.drafts.create)
    revise_spy = AsyncMock(wraps=container.drafts.revise)
    monkeypatch.setattr(container.drafts, "create", create_spy)
    monkeypatch.setattr(container.drafts, "revise", revise_spy)
    with TestClient(app) as client:
        draft_id: str | None = None
        if entry != "create":
            created = client.post("/api/v1/strategy-drafts", json={
                "utterance": _TEXT, "as_of_date": "2026-09-04",
            })
            assert created.status_code == 201, created.text
            draft_id = created.json()["draft_id"]
        before = None if draft_id is None else asyncio.run(
            container.drafts.load_latest_dialogue_state(draft_id=UUID(draft_id)),
        )
        create_spy.reset_mock()
        revise_spy.reset_mock()
        service.failure = MxDailyHistoryBeforeListingError(
            start=date(2025, 1, 1), listing_date=date(2026, 3, 1),
        )
        if entry == "create":
            url = "/api/v1/strategy-drafts"
            payload = {"utterance": _TEXT, "as_of_date": "2026-09-04"}
        elif entry == "revision":
            url = f"/api/v1/strategy-drafts/{draft_id}/revisions"
            payload = {"strategy": _strategy().model_dump(mode="json"), "utterance": _TEXT}
        else:
            url = f"/api/v1/strategy-drafts/{draft_id}/revisions/1/clarification-answers"
            payload = {"answer": "持有满10天卖出"}
            monkeypatch.setattr(DialogueTurnOrchestrator, "plan", AsyncMock(
                return_value=DialogueTurnPlan(
                    intent=TurnIntent.SUPPLEMENT, clarification_turn=ClarificationTurnOutcome(
                        reply_kind="accepted", assistant_message="已补充退出条件。",
                        outcome=_ready(), compile_input=CompileInput(
                            utterance=_TEXT, as_of_date=date(2026, 9, 4),
                        ), revision_changed=True,
                    ),
                ),
            ))
        failed = client.post(url, json=payload)
        assert failed.status_code == 422, failed.text
        assert failed.json()["error"]["code"] == "skill_history_before_listing"
        create_spy.assert_not_awaited()
        revise_spy.assert_not_awaited()
        assert service.fake.configs == [] and service.fake.store.records == {}
        if draft_id is not None:
            unchanged = asyncio.run(container.drafts.load_latest_dialogue_state(
                draft_id=UUID(draft_id),
            ))
            assert unchanged == before  # Includes original utterance and all saved turns.
        service.failure = None
        recovered = client.post(url, json=payload)
        assert recovered.status_code == (200 if entry == "clarification" else 201), recovered.text
        draft = recovered.json()["draft"] if entry == "clarification" else recovered.json()
        assert draft["status"] == "ready"
        submitted = client.post("/api/v1/backtest-runs", json={"strategy": draft["strategy"]})
        assert submitted.status_code == 202 and len(service.fake.configs) == 1


def test_review_edits_are_rechecked_without_creating_a_draft_or_run() -> None:
    service = _PreparingService()
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    original = _strategy().model_dump(mode="json")
    invalid = {**original, "backtest": {**original["backtest"], "start": "2027-01-01"}}
    with TestClient(app) as client:
        failed = client.post("/api/v1/backtest-runs/prepare", json={"strategy": invalid})
        assert failed.status_code == 422
        valid = client.post("/api/v1/backtest-runs/prepare", json={"strategy": original})
        assert valid.status_code == 200 and valid.json() == {"ready": True}
    assert len(service.prepared_configs) == 1
    assert service.fake.configs == [] and service.fake.store.records == {}


@pytest.mark.parametrize(("failure", "status", "metadata"), [
    (MxSaasProviderUnavailableError(
        _PRIVATE, reason="read_timeout", tool="searchData", attempts=3,
        call_id="indicator_history_" + "a" * 32,
    ), 503, {"reason": "read_timeout", "tool": "searchData", "attempts": 3,
             "call_id": "indicator_history_" + "a" * 32}),
    (MxSaasProviderDataError(
        "historical indicator fields are all missing", tool="searchData",
        call_id="indicator_history_" + "b" * 32,
    ), 503, {"data_reason": "data_fields_missing", "tool": "searchData",
             "call_id": "indicator_history_" + "b" * 32}),
    (OSError(5, _PRIVATE, "/private/secret-token-never-log"), 503, {"errno": 5}),
    (ValueError(_PRIVATE), 503, {}),
    (SkillCandidatePreparationError(
        "skill_indicator_history_not_ready", _PRIVATE,
        indicator_id="technical.macd", condition_path="entry.root",
    ), 422, {"indicator_id": "technical.macd", "error_code": "skill_indicator_history_not_ready"}),
])
def test_preflight_failure_logs_safe_correlated_cause_without_private_details(
    failure: Exception, status: int, metadata: dict[str, str | int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    # These fields are deliberately not part of the allowed diagnostic projection.
    failure.__dict__.update(response_body=_PRIVATE, headers={"Authorization": _PRIVATE})
    service = _PreparingService()
    service.failure = failure
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    strategy = _strategy()
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs/prepare", headers={
            "X-Request-ID": "preflight-safe-cause",
        }, json={"strategy": strategy.model_dump(mode="json")})
    assert response.status_code == status
    records = [record for record in caplog.records
               if record.getMessage().startswith("backtest_preflight ")]
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert record.name == "uvicorn.error"
    assert record.exc_info is None and record.stack_info is None
    assert "request_id=preflight-safe-cause" in message
    assert f"symbol={strategy.instrument.symbol}" in message
    assert f"start={strategy.backtest.start}" in message
    assert f"end={strategy.backtest.end}" in message
    assert f"result={response.json()['error']['code']}" in message
    assert json.loads(message.split(" failure=", 1)[1]) == {
        "error_class": type(failure).__name__, **metadata,
    }
    assert "secret-token-never-log" not in caplog.text + response.text
    assert "private.invalid" not in caplog.text + response.text
    assert service.fake.configs == [] and service.fake.store.records == {}


def test_preflight_does_not_trust_arbitrary_exception_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UntrustedDataError(MxSaasProviderDataError):
        indicator_id = _PRIVATE
        code = _PRIVATE

        @property
        def data_reason(self) -> str:
            return _PRIVATE

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    failure = UntrustedDataError(
        _PRIVATE, reason=cast(MxFailureReason, _PRIVATE), tool=cast(MxTool, _PRIVATE),
        call_id=_PRIVATE,
    )
    service = _PreparingService()
    service.failure = failure
    app = create_app(backtest_submission=service, run_store=service.fake.store)
    with TestClient(app) as client:
        response = client.post("/api/v1/backtest-runs/prepare", json={
            "strategy": _strategy().model_dump(mode="json"),
        })
    assert response.status_code == 503
    records = [record for record in caplog.records
               if record.getMessage().startswith("backtest_preflight ")]
    assert len(records) == 1
    assert json.loads(records[0].getMessage().split(" failure=", 1)[1]) == {
        "error_class": "UntrustedDataError",
    }
    assert "secret-token-never-log" not in caplog.text + response.text
    assert "private.invalid" not in caplog.text + response.text
