from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateSourceSpan,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    HybridCandidateGenerator,
    VibeBoundedCandidateGenerator,
    _deterministic_plan_semantic_issues,
    _candidate_schema_feedback,
    _leading_instrument_name,
    _normalize_implied_condition_fields,
    _safe_validation_reason,
    _validate_join_grounding,  # pyright: ignore[reportPrivateUsage]
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderNoDataError
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous, InstrumentNameCandidate


from ashare_lab.api import create_app
from ashare_lab.api.container import ApiContainer
from ashare_lab.api.routes.strategy_drafts import _offer_missing_instrument
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import (
    AllCondition,
    EventCondition,
    HoldingPeriodExit,
    HybridExecutionPolicy,
    IndicatorCondition,
    MinuteProtectionExit,
    PositionReturnExit,
    TrailingDrawdownExit,
)
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
    ResolvedCompileInstrument,
)
from ashare_lab.ports.live_market_data import LiveMarketDataResult

ROOT = Path(__file__).parents[4]
CATALOG = load_catalog_directory(ROOT / "catalogs")
CATALOG_RELEASE = next(
    item.release_version for item in CATALOG.manifests if item.catalog_id == "cn_a.signals"
)
CAPABILITY_MATRIX = build_candidate_capability_matrix(
    CATALOG,
    load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
)
DIRECT_UTTERANCE = "MACD金叉买入，MACD死叉卖出"
FALLBACK_UTTERANCE = "指数平滑异同移动平均线快线上穿时买入，指数平滑异同移动平均线快线下穿时卖出"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_model_calendar_and_indicator_legs_survive_grounding_and_compilation(side):
    from ashare_lab.domain.strategy import ComposedExecutionPolicy

    plan_text = "每月月初买入100股" if side == "buy" else "每月月初卖出100股"
    signal_text = "MACD死叉卖出" if side == "buy" else "MACD金叉买入"
    utterance = f"{plan_text}，{signal_text}"
    signal_side = "exit" if side == "buy" else "entry"
    payload = {"candidates": [{
        "entry": [], "exit": [], "entry_spans": [], "exit_spans": [],
        signal_side: [{"kind": "indicator", "indicator_id": "technical.macd",
            "definition_version": "1.0.0",
            "trigger": "death_cross" if side == "buy" else "golden_cross",
            "params": {"fast": 12, "slow": 26, "signal": 9}}],
        f"{signal_side}_spans": [_source_span(utterance, signal_text)],
        "trading_plan": {"kind": "scheduled", "parameters": {
            "side": side, "frequency": "monthly", "day": 1,
            "sizing_mode": "shares", "quantity": 100}},
        "plan_span": _source_span(utterance, plan_text),
        "confidence": .95,
        "defaulted_fields": [f"/{signal_side}/0/params/{key}"
                             for key in ("fast", "slow", "signal")],
    }]}
    generator = _bounded(_FakeTransport(payload))
    from ashare_lab.adapters.language.vibe_candidates import _validate_transport_payload
    normalized = _validate_transport_payload(payload, utterance=utterance)
    assert set(normalized.candidates[0].defaulted_fields) == set(payload["candidates"][0]["defaulted_fields"])
    assert _validate_transport_payload(normalized.model_dump(mode="json"),
                                       utterance=utterance) == normalized
    request = CompileInput(utterance=utterance, instrument_context="300059.SZ",
                           as_of_date=date(2026, 9, 14))
    candidate, = await generator.generate(request)
    assert candidate.unsupported_code is None
    compiler = StrategyCompiler(generator=generator, catalog=CATALOG,
        catalog_id="cn_a.signals", release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 9, 14))
    outcome = await compiler.compile(request)
    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    strategy = outcome.strategy
    assert strategy is not None
    assert isinstance(strategy.execution, ComposedExecutionPolicy)
    assert strategy.trading_plan.parameters.side == side
    condition = strategy.exit.children[0] if side == "buy" else strategy.entry
    assert condition.indicator_id == "technical.macd"
    assert condition.trigger == ("death_cross" if side == "buy" else "golden_cross")


@pytest.mark.asyncio
@pytest.mark.parametrize(("utterance", "observation", "hybrid"), [
    ("MACD金叉买入，盈利5%或亏损3%卖出", "minute_bar", True),
    ("MACD金叉买入，收盘盈利5%或亏损3%卖出", "daily_close", False),
])
async def test_indicator_entry_preserves_explicit_position_protection_clock(
    utterance: str, observation: str, hybrid: bool,
) -> None:
    entry_text, exit_text = utterance.split("，", 1)
    payload = {"candidates": [{
        "instrument_symbol": None,
        "entry": [{"kind": "indicator", "indicator_id": "technical.macd",
                   "definition_version": "1.0.0", "trigger": "golden_cross",
                   "params": {"fast": 12, "slow": 26, "signal": 9}}],
        "exit": [
            {"kind": "position_return", "trigger": "take_profit", "threshold_pct": 5,
             "observation": observation},
            {"kind": "position_return", "trigger": "stop_loss", "threshold_pct": 3,
             "observation": observation},
        ],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text), _source_span(utterance, exit_text)],
        "confidence": .98,
        "defaulted_fields": ["/entry/0/params/fast", "/entry/0/params/slow",
                             "/entry/0/params/signal"],
    }]}
    generator = _bounded(_FakeTransport(payload))
    outcome = await StrategyCompiler(generator=generator, catalog=CATALOG,
        catalog_id="cn_a.signals", release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30)).compile(
            CompileInput(utterance=utterance, instrument_context="300059.SZ",
                         as_of_date=date(2026, 8, 30)))
    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    if hybrid:
        assert isinstance(outcome.strategy.execution, HybridExecutionPolicy)
        assert outcome.strategy.exit.children == (MinuteProtectionExit(
            take_profit_pct=Decimal(5), stop_loss_pct=Decimal(3)),)
    else:
        assert not isinstance(outcome.strategy.execution, HybridExecutionPolicy)
        assert [type(item) for item in outcome.strategy.exit.children] == [
            PositionReturnExit, PositionReturnExit]


@pytest.mark.asyncio
async def test_explicit_daily_protection_cannot_be_silently_labeled_minute() -> None:
    utterance = "MACD金叉买入，收盘止损3%卖出"
    entry_text, exit_text = utterance.split("，", 1)
    payload = {"candidates": [{
        "instrument_symbol": None,
        "entry": [{"kind": "indicator", "indicator_id": "technical.macd",
                   "definition_version": "1.0.0", "trigger": "golden_cross",
                   "params": {"fast": 12, "slow": 26, "signal": 9}}],
        "exit": [{"kind": "position_return", "trigger": "stop_loss", "threshold_pct": 3,
                  "observation": "minute_bar"}],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)], "confidence": .98,
        "defaulted_fields": ["/entry/0/params/fast", "/entry/0/params/slow",
                             "/entry/0/params/signal"],
    }]}
    result = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 8, 30)))
    assert result[0].unsupported_code == "candidate_provider_invalid_output"


def test_condition_schema_feedback_explains_known_rule_without_echoing_input():
    from ashare_lab.domain.strategy.price_plans import ConditionRule

    with pytest.raises(ValidationError) as caught:
        ConditionRule.model_validate({"kind": "price", "side": "sell"})
    assert "到价条件须填写触发价" in _candidate_schema_feedback(caught.value)
    with pytest.raises(ValidationError) as private_error:
        ConditionRule.model_validate({"kind": "private-secret-value", "side": "sell"})
    assert "private-secret-value" not in _candidate_schema_feedback(private_error.value)


def test_default_provenance_feedback_explains_paths_without_waiving_grounding():
    from ashare_lab.adapters.language.vibe_candidates import _candidate_repair_hints, BoundedCandidate
    code = _candidate_schema_feedback(ValueError("candidate claimed an unknown or unconsumed defaulted field"))
    assert code == 'defaulted_field_unconsumed'
    hints = ' '.join(_candidate_repair_hints([code]))
    assert '/entry/序号/params/参数名' in hints
    assert '不得覆盖明确数值' in hints
    assert '不要删除规则' in hints
    description = BoundedCandidate.model_json_schema()['properties']['defaulted_fields']['description']
    assert 'Do not list /value' in description


@pytest.mark.parametrize('indicator,trigger,value,text', [
    ('technical.kdj', 'j_below', 20, 'KDJ超卖买入'),
    ('technical.rsi', 'below', 30, 'RSI低就买入'),
    ('valuation.pe_ttm', 'below', 10, 'PE便宜就买入'),
])
def test_implicit_threshold_remains_confirmation_not_catalog_default(indicator, trigger, value, text):
    from ashare_lab.adapters.language.vibe_candidates import BoundedCandidate, _unspoken_threshold_issues
    def candidate(source):
        return BoundedCandidate.model_validate({
            'entry': [{'kind': 'indicator', 'indicator_id': indicator, 'trigger': trigger, 'params': {}, 'value': value}],
            'entry_spans': [{'start': 0, 'end': len(source), 'text': source}], 'confidence': .99,
        })
    implicit = candidate(text)
    assert len(_unspoken_threshold_issues(implicit)) == 1
    assert implicit.entry[0].value == value  # preserve the proposal for confirmation
    assert _unspoken_threshold_issues(candidate(f'{text}，阈值{value}')) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('indicator,trigger,value,text', [
    ('technical.kdj', 'j_below', 20, 'KDJ超卖买入'),
    ('technical.rsi', 'below', 30, 'RSI低就买入'),
    ('technical.cci', 'below', -100, 'CCI低就买入'),
])
async def test_suggested_threshold_compiles_editable_with_disclosure(indicator, trigger, value, text):
    utterance = text + '，MACD死叉卖出'
    payload = {'candidates': [{
        'entry': [{'kind': 'indicator', 'indicator_id': indicator,
                   'trigger': trigger, 'params': {}, 'value': value}],
        'exit': [{'kind': 'indicator', 'indicator_id': 'technical.macd',
                  'trigger': 'death_cross', 'params': {}}],
        'entry_spans': [_source_span(utterance, text)],
        'exit_spans': [_source_span(utterance, 'MACD死叉卖出')],
        'confidence': .99,
    }]}
    review = {'instrument': 'equivalent', 'requested_bar_interval': '1d',
        'differences': [], 'requirements': [{'status': 'represented',
        'candidate_path': '/entry', 'source_quote': utterance,
        'requested_meaning': utterance, 'candidate_meaning': utterance}]}
    generator = VibeBoundedCandidateGenerator(_SequenceTransport((payload, review)),
        capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True)
    compiler = StrategyCompiler(generator=generator, catalog=CATALOG,
        catalog_id='cn_a.signals', release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 9, 11))
    result = await compiler.compile(CompileInput(utterance=utterance,
        instrument_context='000001.SZ', as_of_date=date(2026, 9, 11)))
    assert result.status is CompileStatus.READY, (result.diagnostic_code, result.clarification, result.candidate_rejections)
    assert result.strategy.entry.value == value
    assert '系统建议' in result.clarification and '可在策略设置中修改' in result.clarification
    assert not result.run_requested
    assert '系统建议' in await compiler.compose_ready_response(answer=utterance, outcome=result)
    legacy = replace(result, status=CompileStatus.NEEDS_CLARIFICATION,
        strategy=None, strategy_hash=None, diagnostic_code='semantic_confirmation_required',
        suggested_strategy=result.strategy, suggested_strategy_hash=result.strategy_hash,
        semantic_review_issues=(f'买入条件「{text}」的数值阈值尚未明确；当前候选值{value}是建议，'
            '不是指标目录默认值，请确认指标口径及阈值。其他已明确条件保留。',))
    for answer in ('可以，按你说的来', str(value), '是'):
        turn = await compiler.answer_clarification(original_input=CompileInput(
            utterance=utterance, instrument_context='000001.SZ', as_of_date=date(2026, 9, 11)),
            prior_outcome=legacy, answer=answer)
        assert turn.outcome.status is CompileStatus.READY
        assert turn.outcome.strategy == result.strategy
        assert not turn.outcome.run_requested


@pytest.mark.parametrize('side', ['entry', 'exit'])
@pytest.mark.parametrize('path,removed', [
    ('/{side}/0/value', True), ('/{side}/1/value', False),
    ('/{side}/0/params/unknown', False), ('/{side}/00/value', False),
])
def test_threshold_default_annotation_is_separated_without_waiving_unknown_paths(side, path, removed):
    from ashare_lab.adapters.language.vibe_candidates import BoundedCandidate, _materialize_catalog_defaults, _unspoken_threshold_issues
    path = path.format(side=side)
    text = 'KDJ超卖买入' if side == 'entry' else 'KDJ超买卖出'
    candidate = BoundedCandidate.model_validate({
        side: [{'kind': 'indicator', 'indicator_id': 'technical.kdj',
                'trigger': 'j_below' if side == 'entry' else 'j_above', 'value': 20, 'params': {}}],
        side + '_spans': [{'start': 0, 'end': len(text), 'text': text}],
        'confidence': .99, 'defaulted_fields': [path],
    })
    result = _materialize_catalog_defaults(candidate, CAPABILITY_MATRIX)
    assert (path not in result.defaulted_fields) is removed
    assert getattr(result, side)[0].value == 20
    assert len(_unspoken_threshold_issues(result)) == 1
    assert candidate.defaulted_fields == (path,)


def test_condition_kind_canonicalizes_only_implied_execution_mechanics() -> None:
    candidate = {"trading_plan": {"kind": "conditional", "parameters": {"rules": [{
        "kind": "rebound", "side": "sell", "direction": "down", "gap": "2",
        "gap_unit": "percent", "quantity": 100, "reference_mode": "first_observation",
    }, {
        "kind": "stop_loss", "side": "sell", "direction": "up", "gap": "3",
        "gap_unit": "percent", "quantity": 200,
    }]}}}
    _normalize_implied_condition_fields(candidate)
    rules = candidate["trading_plan"]["parameters"]["rules"]
    assert rules[0] == {
        "kind": "rebound", "side": "buy", "direction": "up", "gap": "2",
        "gap_unit": "percent", "quantity": 100, "reference_mode": "previous_fill",
    }
    assert rules[1]["direction"] == "down"
    assert rules[1]["quantity"] == 200


class _FakeTransport:
    def __init__(self, response: CandidateTransportResponse) -> None:
        self.response = response
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        return self.response


class _SequenceTransport(_FakeTransport):
    def __init__(self, responses: tuple[CandidateTransportResponse, ...]) -> None:
        if not responses:
            raise ValueError("responses must not be empty")
        super().__init__(responses[0])
        self.responses = responses

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        response_index = len(self.requests)
        if response_index >= len(self.responses):
            raise AssertionError("candidate provider must not make a third attempt")
        self.requests.append(request)
        return self.responses[response_index]


class _FailingTransport:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        del request
        raise self.error


class _UnexpectedDeterministicGenerator:
    async def generate(self, _request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError("deterministic parser must not run in model-first mode")


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_name", [False, True])
async def test_failed_lexical_identity_hint_keeps_unbound_grid_but_explicit_name_error(explicit_name):
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    plan = GridPlan(parameters=GridParameters(anchor_price=Decimal(10),
        lower_price=Decimal(8), upper_price=Decimal(12), spacing=Decimal(1), max_shares=10000))
    candidate = CandidateAst(instrument_symbol=None, entry=(), exit=(), confidence=1,
        trading_plan=plan, instrument_name="东方财富" if explicit_name else None)

    class Provider:
        async def generate(self, request):
            return (candidate,)

    names = []
    def unavailable(name):
        names.append(name)
        raise TimeoutError("security lookup unavailable")

    generator = HybridCandidateGenerator(deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=Provider(), model_first=True, instrument_name_resolver=unavailable)
    result = (await generator.generate(CompileInput(
        utterance="东方财富，价格下跌1元买入" if explicit_name else
            "网格策略，价格每下跌1元买入100股，每上涨1元卖出100股，最多持有10000股",
        as_of_date=date(2026, 9, 11))))[0]
    assert names == (["东方财富"] if explicit_name else ["网格策略"])
    assert result.trading_plan == plan and result.instrument_symbol is None
    assert result.unsupported_code == ("instrument_resolution_unavailable" if explicit_name else None)


@dataclass(frozen=True)
class _FakeIdentity:
    provider: str = "fixture-provider"
    model: str = "fixture-model"
    prompt_version: str = "fixture-prompt.v1"
    schema_version: str = "fixture-schema.v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["1m", "intraday", "other_intraday"])
async def test_supported_minute_plan_survives_intraday_review_label(interval):
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    utterance = "基准10元，每下跌1元买100股，每上涨1元卖100股"
    plan = GridPlan(parameters=GridParameters(
        anchor_price=10, lower_price=1, upper_price=100,
        spacing=1, observation="minute_bar",
    ))
    payload = {"candidates": [{
        "entry": [], "exit": [], "entry_spans": [], "exit_spans": [],
        "trading_plan": plan.model_dump(mode="json"),
        "plan_span": _source_span(utterance, utterance),
        "confidence": 0.95, "defaulted_fields": [],
    }]}
    transport = _SequenceTransport((payload, {
        "instrument": "equivalent", "requested_bar_interval": interval,
        "differences": [], "requirements": [{
            "status": "represented", "candidate_path": "/trading_plan",
            "source_quote": utterance, "requested_meaning": utterance,
            "candidate_meaning": utterance,
        }],
    }))
    result = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True,
    ).generate(CompileInput(utterance=utterance, instrument_context="300059.SZ",
                           as_of_date=date(2026, 9, 11)))
    if interval == "other_intraday":
        assert result[0].unsupported_code == "non_daily_timeframe_not_supported"
    else:
        assert result[0].unsupported_code is None
        assert result[0].trading_plan == plan


def _bounded(transport: _FakeTransport) -> VibeBoundedCandidateGenerator:
    return VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITY_MATRIX,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("daily", [False, True])
@pytest.mark.parametrize("semantic_review", [False, True])
async def test_isolated_profit_and_loss_exits_keep_thresholds_without_inventing_entry(daily, semantic_review):
    utterance = ("日线收盘" if daily else "") + "买入后止盈5%或止损3%"
    payload = {"candidates": [{
        "entry": [], "entry_spans": [],
        "exit": [
            {"kind": "position_return", "trigger": "take_profit", "threshold_pct": 5},
            {"kind": "position_return", "trigger": "stop_loss", "threshold_pct": 3},
        ],
        "exit_spans": [_source_span(utterance, "止盈5%"), _source_span(utterance, "止损3%")],
        "confidence": .95, "defaulted_fields": [],
    }]}
    transport = _SequenceTransport((payload, {
        "instrument": "equivalent", "requested_bar_interval": "1d" if daily else "unspecified",
        "differences": [], "requirements": [{
            "status": "represented", "candidate_path": "/exit" if daily else "/trading_plan",
            "source_quote": utterance, "requested_meaning": utterance,
            "candidate_meaning": utterance,
        }],
    })) if semantic_review else _FakeTransport(payload)
    result = (await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, model_semantic_review=semantic_review,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 11),
    )))[0]
    if daily:
        assert result.trading_plan is None
        assert len(result.exit) == 2
    else:
        assert result.trading_plan.parameters.observation == "minute_bar"
        assert [(r.kind, r.gap) for r in result.trading_plan.parameters.rules] == [
            ("take_profit", Decimal(5)), ("stop_loss", Decimal(3)),
        ]
        assert result.trading_plan.parameters.initial_shares == 0
        assert result.trading_plan.parameters.opening_shares == 0
        assert result.unsupported_code == "execution_prerequisite_required"


def test_minute_interval_scoped_to_protection_does_not_authorize_minute_entry():
    from types import SimpleNamespace
    from ashare_lab.adapters.language.vibe_candidates import _minute_interval_is_exit_only, PositionReturnCandidate
    text = "按1分钟K线盈利5%止盈"
    candidate = SimpleNamespace(entry=(object(),),
        exit=(PositionReturnCandidate(trigger="take_profit", threshold_pct=5),),
        exit_spans=(SimpleNamespace(text=text),))
    assert _minute_interval_is_exit_only(candidate, "5日均线上穿20日均线买入，" + text)
    assert not _minute_interval_is_exit_only(candidate, "按1分钟K线均线上穿买入，" + text)


def test_fixed_share_plan_cannot_claim_sell_all_or_sell_without_inventory():
    from ashare_lab.domain.strategy.price_plans import ConditionalPlan, ConditionParameters
    plan = ConditionalPlan(parameters=ConditionParameters.model_validate({
        "opening_shares": 1000,
        "rules": [{"kind": "pullback", "side": "sell", "gap": 2, "quantity": 100}],
    }))
    candidate = CandidateAst(None, (), (), .9, trading_plan=plan)
    all_exit = ConditionalPlan(parameters=ConditionParameters.model_validate({
        "rules": [
            {"kind": "price", "side": "buy", "target_price": 20, "sizing_mode": "amount", "amount_cny": 5000},
            {"kind": "take_profit", "side": "sell", "gap": 1, "group": "exit", "sizing_mode": "all_position"},
            {"kind": "stop_loss", "side": "sell", "gap": 1, "group": "exit", "sizing_mode": "all_position"},
        ],
    }))
    assert _deterministic_plan_semantic_issues(
        CandidateAst(None, (), (), .9, trading_plan=all_exit), "买入后盈利1%或亏损1%卖出全部",
    ) == ()
    assert _deterministic_plan_semantic_issues(
        candidate, "涨起来先拿着，回落2%全部卖出",
    ) == (
        "原意为卖出全部可卖持仓；当前条件单只表达了固定股数，"
        "已保留原要求，暂不按错误数量执行回测。",
    )

    exact = ConditionalPlan(parameters=ConditionParameters.model_validate({
        "rules": [
            {"kind": "price", "side": "buy", "target_price": 18, "quantity": 100},
            {"kind": "pullback", "side": "sell", "gap": 2, "quantity": 100},
        ],
    }))
    assert _deterministic_plan_semantic_issues(
        CandidateAst(None, (), (), .9, trading_plan=exact),
        "到18元买100股，回落2%全部卖出",
    ) == ()

    no_inventory = ConditionalPlan(parameters=ConditionParameters.model_validate({
        "rules": [{"kind": "pullback", "side": "sell", "gap": 2}],
    }))
    assert _deterministic_plan_semantic_issues(
        CandidateAst(None, (), (), .9, trading_plan=no_inventory), "回落2%再卖",
    ) == (
        "已识别卖出条件，但当前新策略没有买入规则或期初可卖持仓；"
        "卖出规则仍保留，补充买入规则或期初持仓后，将继续检查数据与执行条件。",
    )


@pytest.mark.parametrize(("utterance", "expected_name"), [
    ("我选放量创高突破：收盘价创20日新高且成交量达到前20日均量1.5倍买入，"
     "跌破20日均线卖出，回测近一年。帮我选三只股票试试。", None),
    ("我选东方财富MACD金叉买入，MACD死叉卖出", "东方财富"),
    ("请帮我回测东方财富MACD金叉买入，MACD死叉卖出", "东方财富"),
])
def test_leading_instrument_name_excludes_selection_prefix(
    utterance: str, expected_name: str | None,
) -> None:
    mention = _leading_instrument_name(utterance)
    if expected_name is None:
        assert mention is None
    else:
        assert mention is not None
        assert mention.text == expected_name
        assert utterance[mention.start:mention.end] == expected_name


@pytest.mark.asyncio
async def test_declared_transport_failure_degrades_to_provider_unavailable() -> None:
    generator = VibeBoundedCandidateGenerator(
        _FailingTransport(CandidateTransportError("sanitized unavailable")),
        capability_matrix=CAPABILITY_MATRIX,
    )

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_unavailable"


@pytest.mark.asyncio
async def test_unexpected_transport_bug_is_not_disguised_as_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_message = "programming bug with provider-secret"
    generator = VibeBoundedCandidateGenerator(
        _FailingTransport(AssertionError(secret_message)),
        capability_matrix=CAPABILITY_MATRIX,
    )

    with pytest.raises(AssertionError, match="programming bug"):
        await generator.generate(
            CompileInput(
                utterance=DIRECT_UTTERANCE,
                instrument_context="300059.SZ",
                as_of_date=date(2026, 8, 30),
            )
        )

    assert "AssertionError" in caplog.text
    assert secret_message not in caplog.text


def _source_span(utterance: str, text: str) -> dict[str, object]:
    start = utterance.index(text)
    return {"start": start, "end": start + len(text), "text": text}


def _macd_batch(
    *,
    symbol: str | None = None,
    utterance: str = DIRECT_UTTERANCE,
) -> dict[str, object]:
    entry_text, exit_text = utterance.split("，", 1)
    return {
        "candidates": [
            {
                "instrument_symbol": symbol,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "death_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [_source_span(utterance, exit_text)],
                "confidence": 0.91,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/signal",
                    "/entry/0/params/slow",
                    "/exit/0/params/fast",
                    "/exit/0/params/signal",
                    "/exit/0/params/slow",
                ],
            }
        ]
    }


def _macd_batch_with_ungrounded_entry(*, utterance: str) -> dict[str, object]:
    payload = deepcopy(_macd_batch(utterance=utterance))
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    exit_text = utterance.split("，", 1)[1]
    candidate["entry_spans"] = [_source_span(utterance, exit_text)]
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "stock", "utterance"])
async def test_recovery_identity_payload_is_bound_to_current_stock_and_exact_source(change) -> None:
    utterance = "东方财富MACD金叉买入，MACD死叉卖出"
    request = CompileInput(
        utterance, date(2026, 9, 6), "300059.SZ",
        resolved_instrument=ResolvedCompileInstrument(
            "300059.SZ", CandidateGroundingEvidence("/instrument/symbol", 0, 4, "东方财富"),
        ),
    )
    if change == "stock":
        request = replace(request, instrument_context="600519.SH")
    elif change == "utterance":
        request = replace(request, utterance=utterance.replace("东方财富", "贵州茅台"))
    transport = _FakeTransport(_macd_batch(utterance=request.utterance))
    await _bounded(transport).generate(request)
    assert len(transport.requests) == 1
    payload = transport.requests[0].user_payload
    assert payload["utterance"] == request.utterance
    if change is None:
        assert payload["verifiedInstrument"] == {
            "symbol": "300059.SZ", "matchedUserText": "东方财富",
            "sourceSpan": {"start": 0, "end": 4},
        }
    else:
        assert "verifiedInstrument" not in payload


def _compiler(generator: HybridCandidateGenerator) -> StrategyCompiler:
    return StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    )


@pytest.mark.asyncio
async def test_unrecognized_phrase_uses_bounded_json_and_compiles_current_dsl() -> None:
    transport = _FakeTransport(json.dumps(_macd_batch(utterance=FALLBACK_UTTERANCE)))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=FALLBACK_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "technical.macd"
    assert outcome.strategy.entry.trigger == "golden_cross"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.max_candidates == 1
    assert "不得生成 Python" in request.system_contract
    assert request.capability_projection_version == "candidate-capabilities.v1"
    assert request.capability_projection_hash == CAPABILITY_MATRIX.content_hash
    assert request.upstream_pattern_commit.startswith("e90b6c6")


@pytest.mark.asyncio
async def test_explicit_code_survives_deterministic_to_bounded_fallback() -> None:
    utterance = (
        "300059.SZ 指数平滑异同移动平均线快线上穿时买入，指数平滑异同移动平均线快线下穿时卖出"
    )
    transport = _FakeTransport(_macd_batch(utterance=utterance))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert len(transport.requests) == 1
    assert transport.requests[0].instrument_context == "300059.SZ"
    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
async def test_grounding_invalid_payload_fails_closed_without_hidden_retry() -> None:
    transport = _SequenceTransport(
        (
            _macd_batch_with_ungrounded_entry(utterance=FALLBACK_UTTERANCE),
            _macd_batch(utterance=FALLBACK_UTTERANCE),
        )
    )
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=FALLBACK_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "candidate_provider_invalid_output"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_known_expression_stays_on_deterministic_fast_path() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="MACD 金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert transport.requests == []


@pytest.mark.asyncio
async def test_model_first_mode_sends_a_known_complete_strategy_to_the_provider() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=_bounded(transport),
        model_first=True,
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert len(transport.requests) == 1
    assert transport.requests[0].utterance == DIRECT_UTTERANCE


@pytest.mark.asyncio
async def test_model_first_provider_failure_never_falls_back_to_deterministic_rules() -> None:
    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=VibeBoundedCandidateGenerator(
            _FailingTransport(CandidateTransportError("provider unavailable")),
            capability_matrix=CAPABILITY_MATRIX,
        ),
        model_first=True,
    )

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert len(candidates) == 1
    assert candidates[0].unsupported_code == "candidate_provider_unavailable"


def test_mixed_plan_schema_repair_explains_holding_exit_placement():
    from ashare_lab.adapters.language.vibe_candidates import _candidate_repair_hints
    hints = _candidate_repair_hints(['schema_invalid:candidates/0:value_error:Value error, 交易计划与指标条件不能混装'])
    assert any('BOTH rules inside trading_plan.parameters.rules' in hint for hint in hints)
    assert any('Preserve quantity' in hint for hint in hints)
    assert _candidate_repair_hints(['unrelated failure']) == []


@pytest.mark.asyncio
async def test_bounded_event_candidate_compiles_without_generated_code() -> None:
    utterance = "年度报告发布后买入，持有3个交易日卖出"
    entry_text, exit_text = utterance.split("，", 1)
    transport = _FakeTransport(
        {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [
                        {
                            "kind": "event",
                            "event_code": "event.financial_results.annual_report",
                            "definition_version": "1.0.0",
                            "trigger": "published",
                            "attributes": {},
                        }
                    ],
                    "exit": [{"kind": "holding_period", "sessions": 3}],
                    "entry_spans": [_source_span(utterance, entry_text)],
                    "exit_spans": [_source_span(utterance, exit_text)],
                    "confidence": 0.88,
                }
            ]
        }
    )
    generator = _bounded(transport)

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == "event.financial_results.annual_report"
    assert isinstance(outcome.strategy.exit.children[0], HoldingPeriodExit)
    assert outcome.strategy.exit.children[0].sessions == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["高点", "买入后最高收盘价", "建仓以来最高收盘价"])
async def test_bounded_position_risk_exits_require_exact_user_grounding(reference: str) -> None:
    utterance = f"MACD金叉买入，止盈20%或从{reference}回撤8%卖出"
    entry_text, _exit_text = utterance.split("，", 1)
    exit_text = f"止盈20%或从{reference}回撤8%卖出"
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "position_return",
                        "trigger": "take_profit",
                        "threshold_pct": 20,
                    },
                    {"kind": "trailing_drawdown", "threshold_pct": 8},
                ],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [
                    _source_span(utterance, exit_text),
                    _source_span(utterance, exit_text),
                ],
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/signal",
                    "/entry/0/params/slow",
                ],
            }
        ]
    }
    generator = VibeBoundedCandidateGenerator(
        _FakeTransport(payload),
        capability_matrix=CAPABILITY_MATRIX,
        provider_identity=_FakeIdentity(),
    )

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    if "收盘价" in reference:
        assert isinstance(outcome.strategy.exit.children[0], TrailingDrawdownExit)
        assert isinstance(outcome.strategy.exit.children[1], MinuteProtectionExit)
    else:
        protection, = outcome.strategy.exit.children
        assert isinstance(protection, MinuteProtectionExit)
        assert protection.take_profit_pct == Decimal(20)
        assert protection.trailing_drawdown_pct == Decimal(8)
    assert outcome.candidate_provenance is not None
    assert outcome.candidate_provenance.provider == "fixture-provider"
    assert outcome.candidate_provenance.capability_projection_hash == (
        CAPABILITY_MATRIX.content_hash
    )


@pytest.mark.asyncio
async def test_bounded_provider_exit_and_cannot_be_rewritten_as_first_of() -> None:
    utterance = "MACD金叉买入，止盈20%且止损5%卖出"
    entry_text, exit_text = utterance.split("，", 1)
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "position_return",
                        "trigger": "take_profit",
                        "threshold_pct": 20,
                    },
                    {
                        "kind": "position_return",
                        "trigger": "stop_loss",
                        "threshold_pct": 5,
                    },
                ],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [
                    _source_span(utterance, exit_text),
                    _source_span(utterance, exit_text),
                ],
                "exit_join": "all",
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/signal",
                    "/entry/0/params/slow",
                ],
            }
        ]
    }
    generator = VibeBoundedCandidateGenerator(
        _FakeTransport(payload),
        capability_matrix=CAPABILITY_MATRIX,
        provider_identity=_FakeIdentity(),
    )

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    # ALL is supported, but the same position cannot simultaneously have a
    # positive take-profit return and a negative stop-loss return.
    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.strategy is None and outcome.strategy_hash is None
    assert not outcome.run_requested
    assert outcome.diagnostic_code == "candidate_batch_no_valid_strategy"
    assert outcome.candidate_provenance is not None
    assert outcome.candidate_provenance.provider == "fixture-provider"
    assert [item.diagnostic_code for item in outcome.candidate_rejections] == [
        "compound_all_exit_not_executable"
    ]


@pytest.mark.asyncio
async def test_provider_cannot_replace_host_instrument() -> None:
    transport = _FakeTransport(_macd_batch(symbol="600519.SH"))
    generator = _bounded(transport)

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_arbitrary_generated_code_fails_closed() -> None:
    payload = _macd_batch()
    candidate = payload["candidates"][0]  # type: ignore[index]
    assert isinstance(candidate, dict)
    candidate["python"] = "import os; os.system('echo unsafe')"
    generator = _bounded(_FakeTransport(payload))

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_side", ["entry", "exit"])
@pytest.mark.parametrize("semantic_review", [False, True])
async def test_partial_model_rules_keep_verified_stock_and_explicit_side(
    missing_side: str, semantic_review: bool,
) -> None:
    side = "exit" if missing_side == "entry" else "entry"
    clause = "MACD死叉卖出" if side == "exit" else "MACD金叉买入"
    utterance = f"我想测东方财富，{clause}"
    payload = _macd_batch()
    item = cast(list[dict[str, object]], payload["candidates"])[0]
    item.update({
        "instrument_name": "东方财富", "instrument_span": _source_span(utterance, "东方财富"),
        missing_side: [], f"{missing_side}_spans": [],
        f"{side}_spans": [_source_span(utterance, clause)],
        "defaulted_fields": [f"/{side}/0/params/{name}" for name in ("fast", "slow", "signal")],
    })
    transport = _SequenceTransport((payload, {
        "instrument": "equivalent", "requested_bar_interval": "unspecified",
        "differences": [], "requirements": [{
            "status": "represented", "candidate_path": f"/{side}[0]",
            "source_quote": clause, "requested_meaning": clause, "candidate_meaning": clause,
        }],
    }))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=VibeBoundedCandidateGenerator(
            transport, capability_matrix=CAPABILITY_MATRIX,
            model_semantic_review=semantic_review,
        ),
        model_first=True, instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )
    candidate = (await generator.generate(CompileInput(
        utterance=utterance, as_of_date=date(2026, 8, 30),
    )))[0]
    assert candidate.unsupported_code == f"{missing_side}_rule_not_recognized"
    assert candidate.instrument_symbol == "300059.SZ"
    assert len(getattr(candidate, side)) == 1
    assert getattr(candidate, missing_side) == ()
    assert any(e.path == "/instrument/symbol" and e.text == "东方财富"
               for e in candidate.grounding_evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_name", [True, False])
async def test_model_evidence_does_not_count_stock_buying_intent_as_an_entry_rule(
    include_name: bool,
) -> None:
    utterance = "我想买东方财富，MACD金叉买入，MACD死叉卖出"
    payload = _macd_batch()
    item = cast(list[dict[str, object]], payload["candidates"])[0]
    item.update({
        "instrument_name": "东方财富" if include_name else None,
        "instrument_span": _source_span(utterance, "东方财富") if include_name else None,
        "entry_spans": [_source_span(utterance, "MACD金叉买入")],
        "exit_spans": [_source_span(utterance, "MACD死叉卖出")],
    })
    transport = _FakeTransport(payload)
    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=VibeBoundedCandidateGenerator(
            transport, capability_matrix=CAPABILITY_MATRIX,
            provider_identity=CandidateProviderIdentityView("fixture", "fixture", "v1", "v1"),
        ), model_first=True,
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )
    outcome = await _compiler(generator).compile(CompileInput(
        utterance=utterance, as_of_date=date(2026, 8, 30),
    ))
    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_missing_exit_does_not_bypass_existing_clarification() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="MACD 金叉买入",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "exit_rule_not_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_missing_entry_does_not_bypass_existing_clarification() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="MACD 死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "entry_rule_not_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    (
        "MACD金叉就上车，MACD死叉就走",
        "MACD金叉买入，MACD死叉就收手",
    ),
)
async def test_source_complete_colloquial_actions_use_bounded_fallback(
    utterance: str,
) -> None:
    transport = _FakeTransport(_macd_batch(utterance=utterance))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert len(transport.requests) == 1


@dataclass(frozen=True)
class _ExactFiveCase:
    utterance: str
    local_diagnostic: str
    payload: dict[str, object]
    expected_entry: tuple[object, ...]
    expected_exit: tuple[tuple[object, ...], ...]


def _indicator_payload(
    indicator_id: str,
    trigger: str,
    params: Mapping[str, object],
    *,
    value: float | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "indicator",
        "indicator_id": indicator_id,
        "definition_version": "1.0.0",
        "trigger": trigger,
        "params": dict(params),
    }
    if value is not None:
        payload["value"] = value
    return payload


def _five_case_payload(
    *,
    utterance: str,
    entry: list[dict[str, object]],
    exit: list[dict[str, object]],
    entry_texts: list[str],
    exit_texts: list[str],
    backtest_text: str,
    defaulted_fields: list[str] | None = None,
) -> dict[str, object]:
    return {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": entry,
                "exit": exit,
                "entry_spans": [_source_span(utterance, text) for text in entry_texts],
                "exit_spans": [_source_span(utterance, text) for text in exit_texts],
                "backtest_lookback_years": 5,
                "backtest_span": _source_span(utterance, backtest_text),
                "confidence": 0.91,
                "defaulted_fields": defaulted_fields or [],
            }
        ]
    }


def _condition_signature(condition: object) -> tuple[object, ...]:
    if isinstance(condition, IndicatorCondition):
        return (
            "indicator",
            condition.indicator_id,
            condition.trigger,
            tuple(sorted(condition.params.items())),
            condition.value,
        )
    if isinstance(condition, EventCondition):
        return ("event", condition.event_code, condition.trigger)
    if isinstance(condition, HoldingPeriodExit):
        return ("holding_period", condition.sessions)
    if isinstance(condition, AllCondition):
        return ("all", tuple(_condition_signature(child) for child in condition.children))
    raise AssertionError(f"unexpected strategy node: {type(condition).__name__}")


def _exact_five_cases() -> tuple[_ExactFiveCase, ...]:
    ma_utterance = "东方财富最近五年，价格强势站上20日均线时上车，跌回这条线下就走。"
    ma_params = {"period": 20, "price_field": "close"}
    macd_utterance = "东方财富的 MACD 快线往上穿过慢线就买，往下穿回去就卖，回测近五年。"
    macd_params = {"fast": 12, "slow": 26, "signal": 9}
    rsi_utterance = "东方财富跌得很猛后，RSI 从30以下重新回到30上方就买，到70上方就收手，回测五年。"
    rsi_params = {"period": 14}
    volume_utterance = (
        "东方财富收盘创20日新高、成交量是过去20日平均的1.5倍才买；MACD 转弱卖，回测五年。"
    )
    event_utterance = "东方财富的年报公开以后就买，持有三个交易日后卖，近五年。"
    return (
        _ExactFiveCase(
            utterance=ma_utterance,
            local_diagnostic="strategy_rule_incomplete",
            payload=_five_case_payload(
                utterance=ma_utterance,
                entry=[_indicator_payload("technical.ma", "price_crosses_above", ma_params)],
                exit=[_indicator_payload("technical.ma", "price_crosses_below", ma_params)],
                entry_texts=["价格强势站上20日均线时上车"],
                exit_texts=["跌回这条线下就走"],
                backtest_text="最近五年",
                defaulted_fields=[
                    "/entry/0/params/price_field",
                    "/exit/0/params/price_field",
                ],
            ),
            expected_entry=(
                "indicator",
                "technical.ma",
                "price_crosses_above",
                (("period", 20), ("price_field", "close")),
                None,
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.ma",
                    "price_crosses_below",
                    (("period", 20), ("price_field", "close")),
                    None,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=macd_utterance,
            local_diagnostic="ambiguous_macd_trigger",
            payload=_five_case_payload(
                utterance=macd_utterance,
                entry=[_indicator_payload("technical.macd", "golden_cross", macd_params)],
                exit=[_indicator_payload("technical.macd", "death_cross", macd_params)],
                entry_texts=["MACD 快线往上穿过慢线就买"],
                exit_texts=["往下穿回去就卖"],
                backtest_text="回测近五年",
                defaulted_fields=[
                    f"/{side}/0/params/{name}"
                    for side in ("entry", "exit")
                    for name in ("fast", "signal", "slow")
                ],
            ),
            expected_entry=(
                "indicator",
                "technical.macd",
                "golden_cross",
                (("fast", 12), ("signal", 9), ("slow", 26)),
                None,
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.macd",
                    "death_cross",
                    (("fast", 12), ("signal", 9), ("slow", 26)),
                    None,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=rsi_utterance,
            local_diagnostic="exit_rule_not_recognized",
            payload=_five_case_payload(
                utterance=rsi_utterance,
                entry=[_indicator_payload("technical.rsi", "crosses_above", rsi_params, value=30)],
                exit=[_indicator_payload("technical.rsi", "crosses_above", rsi_params, value=70)],
                entry_texts=["RSI 从30以下重新回到30上方就买"],
                exit_texts=["到70上方就收手"],
                backtest_text="回测五年",
                defaulted_fields=[
                    "/entry/0/params/period",
                    "/exit/0/params/period",
                ],
            ),
            expected_entry=(
                "indicator",
                "technical.rsi",
                "crosses_above",
                (("period", 14),),
                30.0,
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.rsi",
                    "crosses_above",
                    (("period", 14),),
                    70.0,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=volume_utterance,
            local_diagnostic="ambiguous_boolean_expression",
            payload=_five_case_payload(
                utterance=volume_utterance,
                entry=[
                    _indicator_payload(
                        "price.rolling_high",
                        "new_high",
                        {"period": 20, "price_field": "close"},
                    ),
                    _indicator_payload(
                        "volume.relative",
                        "gte_multiple",
                        {"baseline_period": 20, "consecutive_days": 3},
                        value=1.5,
                    ),
                ],
                exit=[_indicator_payload("technical.macd", "death_cross", macd_params)],
                entry_texts=[
                    "东方财富收盘创20日新高、成交量是过去20日平均的1.5倍才买",
                    "东方财富收盘创20日新高、成交量是过去20日平均的1.5倍才买",
                ],
                exit_texts=["MACD 转弱卖"],
                backtest_text="回测五年",
                defaulted_fields=[
                    "/entry/1/params/consecutive_days",
                    "/exit/0/params/fast",
                    "/exit/0/params/signal",
                    "/exit/0/params/slow",
                ],
            ),
            expected_entry=(
                "all",
                (
                    (
                        "indicator",
                        "price.rolling_high",
                        "new_high",
                        (("period", 20), ("price_field", "close")),
                        None,
                    ),
                    (
                        "indicator",
                        "volume.relative",
                        "gte_multiple",
                        (("baseline_period", 20),),
                        1.5,
                    ),
                ),
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.macd",
                    "death_cross",
                    (("fast", 12), ("signal", 9), ("slow", 26)),
                    None,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=event_utterance,
            local_diagnostic="exit_rule_not_recognized",
            payload=_five_case_payload(
                utterance=event_utterance,
                entry=[
                    {
                        "kind": "event",
                        "event_code": "event.financial_results.annual_report",
                        "definition_version": "1.0.0",
                        "trigger": "published",
                        "attributes": {},
                    }
                ],
                exit=[{"kind": "holding_period", "sessions": 3}],
                entry_texts=["东方财富的年报公开以后就买"],
                exit_texts=["持有三个交易日后卖"],
                backtest_text="近五年",
            ),
            expected_entry=(
                "event",
                "event.financial_results.annual_report",
                "published",
            ),
            expected_exit=(("holding_period", 3),),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _exact_five_cases())
async def test_exact_original_five_route_through_bounded_provider(
    case: _ExactFiveCase,
) -> None:
    snapshot_end = date(2026, 8, 20)
    input_request = CompileInput(
        utterance=case.utterance,
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 30),
    )
    local = await RuleBasedCandidateGenerator().generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=snapshot_end,
        )
    )
    assert local[0].unsupported_code == case.local_diagnostic

    transport = _FakeTransport(case.payload)
    bounded = VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITY_MATRIX,
        provider_identity=CandidateProviderIdentityView(
            provider="fixture-provider",
            model="fixture-model",
            prompt_version="fixture-prompt.v1",
            schema_version="fixture-schema.v1",
        ),
    )
    outcome = await StrategyCompiler(
        generator=HybridCandidateGenerator(
            deterministic=RuleBasedCandidateGenerator(),
            bounded_fallback=bounded,
        ),
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
        backtest_anchor_date=snapshot_end,
    ).compile(input_request)

    assert len(transport.requests) == 1
    assert transport.requests[0].as_of_date == snapshot_end
    assert outcome.status is CompileStatus.READY
    assert outcome.candidate_provenance is not None
    assert outcome.candidate_provenance.source == "bounded_provider"
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2021, 8, 20)
    assert outcome.strategy.backtest.end == snapshot_end
    assert _condition_signature(outcome.strategy.entry) == case.expected_entry
    assert tuple(_condition_signature(item) for item in outcome.strategy.exit.children) == (
        case.expected_exit
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deviation",
    (
        "catalog_alias_instead_of_id",
        "chinese_alias_instead_of_trigger",
        "missing_exact_span",
        "paraphrased_span",
        "extra_reasoning_field",
        "numeric_parameter_as_string",
        "unclaimed_catalog_defaults",
        "noncanonical_join",
    ),
)
async def test_common_json_object_semantic_deviations_fail_closed(
    deviation: str,
) -> None:
    payload = deepcopy(_macd_batch())
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    entry = cast(list[object], candidate["entry"])
    entry_leaf = cast(dict[str, object], entry[0])
    if deviation == "catalog_alias_instead_of_id":
        entry_leaf["indicator_id"] = "MACD"
    elif deviation == "chinese_alias_instead_of_trigger":
        entry_leaf["trigger"] = "金叉"
    elif deviation == "missing_exact_span":
        candidate.pop("entry_spans")
    elif deviation == "paraphrased_span":
        entry_spans = cast(list[object], candidate["entry_spans"])
        entry_span = cast(dict[str, object], entry_spans[0])
        entry_span["text"] = "MACD向上交叉买入"
    elif deviation == "extra_reasoning_field":
        candidate["reasoning"] = "模型的解释不属于受限契约"
    elif deviation == "numeric_parameter_as_string":
        params = cast(dict[str, object], entry_leaf["params"])
        params["fast"] = "12"
    elif deviation == "unclaimed_catalog_defaults":
        candidate["defaulted_fields"] = []
    elif deviation == "noncanonical_join":
        candidate["entry_join"] = "AND"
    else:  # pragma: no cover - the parameter table is closed above
        raise AssertionError(f"unknown deviation: {deviation}")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == (
        None if deviation == "unclaimed_catalog_defaults" else "candidate_provider_invalid_output"
    )


@pytest.mark.asyncio
async def test_json_object_repairs_only_exact_unique_offsets_and_explicit_default_claims() -> None:
    case = _exact_five_cases()[0]
    payload = deepcopy(case.payload)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    for key in ("entry_spans", "exit_spans"):
        spans = cast(list[object], candidate[key])
        for raw_span in spans:
            span = cast(dict[str, object], raw_span)
            span["start"] = 0
            span["end"] = len(cast(str, span["text"]))
    candidate["instrument_symbol"] = "300059.SZ"
    # Name evidence is resolved as a name, not treated as literal code evidence.
    candidate["instrument_name"] = "东方财富"
    candidate["instrument_span"] = {
        "start": 9,
        "end": 13,
        "text": "东方财富",
    }
    backtest_span = cast(dict[str, object], candidate["backtest_span"])
    backtest_span.update(start=0, end=4)
    defaulted_fields = cast(list[object], candidate["defaulted_fields"])
    defaulted_fields.extend(
        (
            "/entry/0/params/period",
            "/exit/0/params/period",
        )
    )

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code is None
    assert generated[0].instrument_name == "东方财富"
    assert generated[0].instrument_symbol is None


@pytest.mark.asyncio
async def test_json_object_expands_narrow_action_quote_to_exact_source_clause() -> None:
    utterance = "收盘价上穿20日均线买入，下穿20日均线卖出"
    params = {"period": 20, "price_field": "close"}
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [_indicator_payload("technical.ma", "price_crosses_above", params)],
                "exit": [_indicator_payload("technical.ma", "price_crosses_below", params)],
                # The model chose the right leaf but quoted only its action.
                "entry_spans": [_source_span(utterance, "买入")],
                "exit_spans": [_source_span(utterance, "下穿20日均线卖出")],
                "confidence": 0.91,
                "defaulted_fields": [],
            }
        ]
    }

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code is None
    entry_evidence = next(
        item for item in generated[0].grounding_evidence if item.path == "/entry/0"
    )
    assert entry_evidence.text == "收盘价上穿20日均线买入"


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "average"), [
    ("", "20日均线"), ("收盘价", "20日移动平均线"), ("当日收盘价", "MA20"),
])
@pytest.mark.parametrize("crossing", [False, True])
async def test_price_subject_and_comparator_are_grounded_independently(
    subject: str, average: str, crossing: bool,
) -> None:
    entry_text = f"{subject}{'从上方下穿至' if crossing else '低于'}{average}下方买入"
    exit_text = f"{subject}高于{average}卖出"
    utterance = f"{entry_text}，{exit_text}"
    params = {"period": 20, "price_field": "close"}
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.ma", "price_below", params)],
        "exit": [_indicator_payload("technical.ma", "price_above", params)],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.91,
        "defaulted_fields": ["/entry/0/params/price_field", "/exit/0/params/price_field"]
        if not subject else [],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="600519.SH", as_of_date=date(2026, 9, 5),
    ))
    expected_code = "candidate_provider_invalid_output" if crossing else None
    assert generated[0].unsupported_code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "entry_trigger", "exit_trigger", "fast", "expected_code"),
    (
        (5, 20, "golden_cross", "death_cross", 5, None),
        (20, 5, "death_cross", "golden_cross", 5, None),
        (5, 20, "death_cross", "golden_cross", 5, "candidate_provider_invalid_output"),
        (5, 20, "golden_cross", "death_cross", 10, "candidate_provider_invalid_output"),
    ),
)
async def test_model_keeps_full_catalog_and_exact_ma_pair_grounding(
    first: int,
    second: int,
    entry_trigger: str,
    exit_trigger: str,
    fast: int,
    expected_code: str | None,
) -> None:
    entry_text = f"{first}日均线上穿{second}日均线买入"
    exit_text = f"{first}日均线下穿{second}日均线卖出"
    utterance = f"{entry_text}，{exit_text}"
    params = {"fast_period": fast, "slow_period": 20, "price_field": "close"}
    transport = _FakeTransport({"candidates": [{
        "instrument_symbol": None,
        "entry": [_indicator_payload("technical.ma_cross", entry_trigger, params)],
        "exit": [_indicator_payload("technical.ma_cross", exit_trigger, params)],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.95,
        "defaulted_fields": ["/entry/0/params/price_field", "/exit/0/params/price_field"],
    }]})
    generated = await _bounded(transport).generate(CompileInput(
        utterance=utterance, instrument_context="601318.SH", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == expected_code
    assert transport.requests[0].capability_matrix == CAPABILITY_MATRIX.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second", "exit_text", "exit_indicator", "exit_trigger", "exit_fast", "valid"),
    [
        (5, 20, "下穿20日均线时卖出", "technical.ma_cross", "death_cross", 5, True),
        (20, 5, "下穿5日均线时卖出", "technical.ma_cross", "golden_cross", 5, True),
        (5, 20, "下穿20日均线时卖出", "technical.ma_cross", "golden_cross", 5, False),
        (5, 20, "下穿20日均线时卖出", "technical.ma_cross", "death_cross", 10, False),
        (5, 20, "下穿20日均线时卖出", "technical.ma", "price_crosses_below", 5, False),
        (5, 20, "收盘价下穿20日均线时卖出", "technical.ma", "price_crosses_below", 5, True),
        (5, 20, "收盘价下穿20日均线时卖出", "technical.ma_cross", "death_cross", 5, False),
        (5, 20, "10日均线下穿20日均线时卖出", "technical.ma_cross", "death_cross", 10, True),
    ],
)
async def test_ma_exit_inherits_only_unambiguous_source_subject(
    first: int, second: int, exit_text: str, exit_indicator: str,
    exit_trigger: str, exit_fast: int, valid: bool,
) -> None:
    entry_text = f"贵州茅台{first}日均线上穿{second}日均线时买入"
    utterance = f"{entry_text}，{exit_text}"
    entry_params = {"fast_period": min(first, second), "slow_period": max(first, second),
                    "price_field": "close"}
    exit_params = ({"period": 20, "price_field": "close"}
                   if exit_indicator == "technical.ma" else
                   {"fast_period": exit_fast, "slow_period": 20, "price_field": "close"})
    defaults = ["/entry/0/params/price_field"]
    if "收盘价" not in exit_text:
        defaults.append("/exit/0/params/price_field")
    transport = _FakeTransport({"candidates": [{
        "entry": [_indicator_payload("technical.ma_cross", "golden_cross"
                                     if first < second else "death_cross", entry_params)],
        "exit": [_indicator_payload(exit_indicator, exit_trigger, exit_params)],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.95, "defaulted_fields": defaults,
    }]})
    generated = await _bounded(transport).generate(CompileInput(
        utterance=utterance, instrument_context="600519.SH", as_of_date=date(2026, 9, 6),
    ))
    assert (generated[0].unsupported_code is None) is valid
    if valid:
        evidence = {item.path: item for item in generated[0].grounding_evidence}
        assert evidence["/exit/0"].text == exit_text
        assert generated[0].exit[0].indicator_id == exit_indicator


@pytest.mark.asyncio
async def test_explicit_exit_price_subject_overrides_entry_ma_subject_in_compound_clause() -> None:
    entry_text = "贵州茅台5日均线上穿20日均线时买入"
    exit_text = "收盘价高于30元且下穿20日均线时卖出"
    utterance = f"{entry_text}，{exit_text}"
    transport = _FakeTransport({"candidates": [{
        "entry": [_indicator_payload("technical.ma_cross", "golden_cross", {
            "fast_period": 5, "slow_period": 20, "price_field": "close",
        })],
        "exit": [_indicator_payload("price.close", "above", {}, value=30),
                 _indicator_payload("technical.ma", "price_crosses_below", {
                     "period": 20, "price_field": "close",
                 })],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)] * 2,
        "exit_join": "all", "confidence": 0.95,
        "defaulted_fields": ["/entry/0/params/price_field"],
    }]})
    generated = await _bounded(transport).generate(CompileInput(
        utterance=utterance, instrument_context="600519.SH", as_of_date=date(2026, 9, 6),
    ))
    assert generated[0].unsupported_code is None
    assert generated[0].exit[1].indicator_id == "technical.ma"


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["20日", "前20日", "近20日"])
async def test_rolling_high_close_subject_is_not_an_extra_price_condition(window: str) -> None:
    entry_text = f"东方财富收盘价创{window}新高买入"
    exit_text = "价格下穿20日均线卖出"
    utterance = f"{entry_text}，{exit_text}"
    payload = {"candidates": [{
        "instrument_symbol": None,
        "entry": [_indicator_payload("price.rolling_high", "new_high", {
            "period": 20, "price_field": "close",
        })],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.95,
        "defaulted_fields": ["/exit/0/params/price_field"],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("window,verb,field,trigger", [
    ("20日", "突破", "最高价", "price_crosses_above_upper"),
    ("前60日", "突破", "最高价", "price_crosses_above_upper"),
    ("过去20个交易日", "高于", "最高价", "price_above_upper"),
    ("近20日", "跌破", "最低价", "price_crosses_below_lower"),
])
async def test_explicit_historical_high_low_uses_close_vs_prior_extreme(
    window: str, verb: str, field: str, trigger: str,
) -> None:
    entry_text = f"东方财富收盘价{verb}{window}{field}时买入"
    exit_text = "跌破20日均线卖出"
    utterance = f"{entry_text}，{exit_text}"
    period = 60 if "60" in window else 20
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.donchian", trigger, {"period": period})],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.95, "defaulted_fields": [],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 7),
    ))
    assert generated[0].unsupported_code is None
    assert generated[0].entry[0].indicator_id == "technical.donchian"
    assert dict(generated[0].entry[0].params) == {"period": period}


@pytest.mark.asyncio
@pytest.mark.parametrize("review_result", ["equivalent", "mismatch", "uncertain"])
@pytest.mark.parametrize("corruption", [
    None, "period", "source", "stock", "default", "interval", "missing_default", "review_stock",
])
async def test_model_semantic_mode_accepts_meaning_not_trade_keywords(
    review_result: str, corruption: str | None,
) -> None:
    entry = "MACD快线从慢线下面穿上去就进场"
    exit_text = "两线反方向交叉就离场"
    utterance = f"{entry}，{exit_text}"
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.macd", "golden_cross", {
            "fast": 12, "slow": 26, "signal": 9,
        })],
        "exit": [_indicator_payload("technical.macd", "death_cross", {
            "fast": 12, "slow": 26, "signal": 9,
        })],
        "entry_spans": [_source_span(utterance, entry)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.95, "defaulted_fields": [
            f"/{side}/0/params/{field}" for side in ("entry", "exit")
            for field in ("fast", "slow", "signal")
        ],
    }]}

    candidate = payload["candidates"][0]
    if corruption == "period":
        candidate["entry"][0]["params"]["fast"] = -1
    elif corruption == "source":
        candidate["entry_spans"][0]["text"] = "模型虚构的引用"
    elif corruption == "stock":
        candidate["instrument_symbol"] = "600519.SH"
    elif corruption == "default":
        candidate["defaulted_fields"].append("/entry/0/params/unknown")
    elif corruption == "missing_default":
        del candidate["entry"][0]["params"]["fast"]

    class Transport:
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                return {
                    "instrument": "uncertain" if corruption == "review_stock" else "equivalent",
                    "requirements": [{"status": "represented", "candidate_path": "/entry",
                        "source_quote": request.utterance,
                        "requested_meaning": "测试条件", "candidate_meaning": "测试条件"}],
                    "requested_bar_interval": (
                        "intraday" if corruption == "interval" else "unspecified"
                    ),
                    "differences": [{"candidate_path": "/entry/0/trigger",
                        "source_quote": entry, "requested_meaning": "上穿进场",
                        "candidate_meaning": "不同方向进场"}]
                        if review_result == "mismatch" else [],
                }
            return payload

    generated = await VibeBoundedCandidateGenerator(
        Transport(), capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 7),
    ))
    assert (generated[0].unsupported_code is None) == (
        review_result != "mismatch" and corruption in {None, "missing_default"}
    )
    if corruption == "interval":
        assert generated[0].unsupported_code == "non_daily_timeframe_not_supported"
    elif review_result == "mismatch" and corruption in {None, "missing_default"}:
        assert generated[0].unsupported_code == "semantic_confirmation_required"
        assert generated[0].entry and generated[0].exit
        assert generated[0].semantic_review_issues
    elif corruption == "review_stock":
        assert generated[0].unsupported_code == "candidate_provider_invalid_output"
        assert not generated[0].entry and not generated[0].exit


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_failure", [
    "review", "schema", "transport", "connection_failed", "timeout", "rate_limited",
])
async def test_semantic_disagreement_survives_failed_repair_and_resolves_stock(repair_failure):
    utterance = "东方财富MACD金叉买入，MACD死叉卖出"
    payload = {"candidates": [{
        "instrument_name": "东方财富",
        "instrument_span": _source_span(utterance, "东方财富"),
        "entry": [_indicator_payload("technical.macd", "golden_cross", {
            "fast": 12, "slow": 26, "signal": 9,
        })],
        "exit": [_indicator_payload("technical.macd", "death_cross", {
            "fast": 12, "slow": 26, "signal": 9,
        })],
        "entry_spans": [_source_span(utterance, "MACD金叉买入")],
        "exit_spans": [_source_span(utterance, "MACD死叉卖出")],
        "confidence": 0.95,
    }]}
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "equivalent",
                        "requested_bar_interval": "unspecified",
                        "requirements": [{"status": "represented", "candidate_path": "/entry",
                            "source_quote": request.utterance,
                            "requested_meaning": "测试条件", "candidate_meaning": "测试条件"}],
                        "differences": [{"candidate_path": "/exit/0/trigger",
                            "source_quote": "MACD死叉卖出", "requested_meaning": "死叉卖出",
                            "candidate_meaning": "另一信号卖出"}]}
            if len(requests) > 1:
                if repair_failure == "schema":
                    return {"broken": True}
                if repair_failure == "transport":
                    raise CandidateTransportError("provider unavailable", timed_out=True)
                if repair_failure in {"connection_failed", "timeout", "rate_limited"}:
                    raise CandidateTransportError(
                        "provider unavailable", failure_kind=repair_failure,
                    )
            return payload

    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=VibeBoundedCandidateGenerator(
            Transport(), capability_matrix=CAPABILITY_MATRIX,
            model_semantic_review=True, repair_invalid_output=True,
        ),
        model_first=True, instrument_name_resolver=lambda name: "300059.SZ",
    )
    request = CompileInput(
        utterance=utterance, as_of_date=date(2026, 9, 7),
    )
    candidates = await generator.generate(request)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.unsupported_code == "semantic_confirmation_required"
    assert len(candidate.semantic_review_issues) == 1
    assert candidate.semantic_review_issues[0] == "原意为死叉卖出；当前为另一信号卖出"
    assert candidate.instrument_symbol == "300059.SZ" and candidate.instrument_name is None
    assert candidate.entry[0].trigger == "golden_cross"
    assert candidate.exit[0].trigger == "death_cross"
    assert len(requests) == 3  # An unchanged repaired candidate reuses the first review.


@pytest.mark.asyncio
@pytest.mark.parametrize("review_error", ["enum", "json", "source"])
async def test_invalid_advisory_review_does_not_erase_validated_candidate(review_error):
    utterance = "东方财富MACD金叉买入，MACD死叉卖出"
    payload = {"candidates": [{
        "instrument_name": "东方财富",
        "instrument_span": _source_span(utterance, "东方财富"),
        "entry": [_indicator_payload("technical.macd", "golden_cross", {
            "fast": 12, "slow": 26, "signal": 9,
        })],
        "exit": [_indicator_payload("technical.macd", "death_cross", {
            "fast": 12, "slow": 26, "signal": 9,
        })],
        "entry_spans": [_source_span(utterance, "MACD金叉买入")],
        "exit_spans": [_source_span(utterance, "MACD死叉卖出")],
        "confidence": 0.95,
    }]}
    requests = []

    class Transport:
        async def generate_json(self, request):
            requests.append(request)
            if request.response_schema_name != "strategy_semantic_review":
                return payload
            if review_error == "json":
                return "{invalid-json"
            return {
                "instrument": "represented" if review_error == "enum" else "equivalent",
                "requested_bar_interval": "unspecified", "differences": [],
                "requirements": [{"status": "represented", "candidate_path": "/entry",
                    "source_quote": "虚构引用" if review_error == "source" else utterance,
                    "requested_meaning": "测试规则", "candidate_meaning": "测试规则"}],
            }

    generator = VibeBoundedCandidateGenerator(
        Transport(), capability_matrix=CAPABILITY_MATRIX,
        model_semantic_review=True, repair_invalid_output=True,
    )
    generated = await generator.generate(CompileInput(
        utterance=utterance, as_of_date=date(2026, 9, 8),
    ))
    assert generated[0].unsupported_code is None
    assert generated[0].entry and generated[0].exit
    assert generated[0].semantic_review_issues == ()
    assert [r.response_schema_name for r in requests] == [
        "ashare_bounded_strategy_candidates",
        "strategy_semantic_review", "strategy_semantic_review",
    ]
    assert requests[1].user_payload["candidate"] == requests[2].user_payload["candidate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("connector", ["且", "并要求"])
async def test_rolling_high_cannot_hide_a_separate_fixed_price_condition(
    connector: str, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="ashare_lab.adapters.language.vibe_candidates")
    entry_text = f"收盘价创前20日新高{connector}收盘价低于30元买入"
    exit_text = "持有5个交易日后卖出"
    utterance = f"{entry_text}，{exit_text}"
    payload = {"candidates": [{
        "instrument_symbol": None,
        "entry": [_indicator_payload("price.rolling_high", "new_high", {
            "period": 20, "price_field": "close",
        })],
        "exit": [{"kind": "holding_period", "sessions": 5}],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.95,
        "defaulted_fields": [],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == "candidate_provider_invalid_output"
    assert "source_condition_omitted" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("title", "entry_text", "include_volume", "accepted"), [
    ("我选放量创高突破：", "收盘价创20日新高且成交量达到前20日均量1.5倍买入", True, True),
    ("", "收盘价创20日新高且成交量达到前20日均量1.5倍买入", True, True),
    ("我选放量创高突破：", "收盘价创20日新高买入", False, False),
    ("我选放量创高突破：", "收盘价创20日新高且成交量达到前20日均量1.5倍买入", False, False),
    ("我选放量3倍：", "收盘价创20日新高且成交量达到前20日均量1.5倍买入", True, False),
    ("我选放量３倍：", "收盘价创20日新高且成交量达到前20日均量1.5倍买入", True, False),
    ("我选放量买入：", "收盘价创20日新高且成交量达到前20日均量1.5倍买入", True, False),
    ("我选放量创高突破：",
     "收盘价创20日新高且成交量达到前20日均量1.5倍且收盘价低于30元买入", True, False),
])
async def test_selected_strategy_title_only_deduplicates_conditions_repeated_in_explicit_rules(
    title: str, entry_text: str, include_volume: bool, accepted: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exit_text = "跌破20日均线卖出"
    utterance = f"{title}{entry_text}，{exit_text}，回测近一年。帮我选三只股票试试。"
    entries = [_indicator_payload("price.rolling_high", "new_high", {
        "period": 20, "price_field": "close",
    })]
    if include_volume:
        entries.append(_indicator_payload("volume.relative", "gte_multiple", {
            "baseline_period": 20, "consecutive_days": 3,
        }, value=1.5))
    payload = {"candidates": [{
        "entry": entries,
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })],
        "entry_spans": [_source_span(utterance, entry_text)] * len(entries),
        "exit_spans": [_source_span(utterance, exit_text)],
        "backtest_lookback_years": 1,
        "backtest_span": _source_span(utterance, "回测近一年"),
        "confidence": 0.95,
        "defaulted_fields": ["/exit/0/params/price_field"] + (
            ["/entry/1/params/consecutive_days"] if include_volume else []
        ),
    }]}
    request = CompileInput(utterance=utterance, as_of_date=date(2026, 9, 5))
    transport = _FakeTransport(payload)

    generated = await _bounded(transport).generate(request)

    assert (generated[0].unsupported_code is None) is accepted
    assert request.utterance == utterance and transport.requests[0].utterance == utterance
    assert len(transport.requests) == 1
    if accepted:
        assert len(generated[0].entry) == len(entries)
        assert all(item.text == entry_text for item in generated[0].grounding_evidence
                   if item.path.startswith("/entry/"))
    else:
        assert "source_condition_omitted" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_text", "expected_code"),
    (
        ("创20日新高就买", None),
        ("最高价创20日新高就买", "candidate_provider_invalid_output"),
    ),
)
async def test_rolling_high_only_defaults_unspoken_price_field(
    entry_text: str,
    expected_code: str | None,
) -> None:
    utterance = f"{entry_text}，持有5个交易日后卖"
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [
                    _indicator_payload(
                        "price.rolling_high",
                        "new_high",
                        {"period": 20, "price_field": "close"},
                    )
                ],
                "exit": [{"kind": "holding_period", "sessions": 5}],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [_source_span(utterance, "持有5个交易日后卖")],
                "confidence": 0.91,
                "defaulted_fields": (
                    []
                    if expected_code is None
                    else ["/entry/0/params/price_field"]
                ),
            }
        ]
    }

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("multiple", "repair"), [(1.5, False), (2.0, False), (1.5, True), (2.0, True)],
)
async def test_homepage_volume_wording_is_grounded_without_changing_model_threshold(
    multiple: float,
    repair: bool,
) -> None:
    entry_text = "东方财富创20日新高且放量1.5倍买入"
    exit_text = "跌破20日线卖出"
    utterance = f"{entry_text}，{exit_text}"
    payload = {"candidates": [{
        "instrument_symbol": None,
        "entry": [
            _indicator_payload("price.rolling_high", "new_high", {
                "period": 20, "price_field": "close",
            }),
            _indicator_payload("volume.relative", "gte_multiple", {
                "baseline_period": 20, "consecutive_days": 3,
            }, value=multiple),
        ],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })],
        "entry_spans": [_source_span(utterance, entry_text)] * 2,
        "exit_spans": [_source_span(utterance, exit_text)],
        "entry_join": "all",
        "confidence": 0.95,
        "defaulted_fields": [
            "/entry/0/params/price_field", "/entry/1/params/baseline_period",
            "/entry/1/params/consecutive_days", "/exit/0/params/price_field",
        ],
    }]}
    incomplete = deepcopy(payload)
    incomplete["candidates"][0]["defaulted_fields"] = []
    transport = _SequenceTransport((incomplete, payload)) if repair else _FakeTransport(payload)
    generated = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, repair_invalid_output=repair,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == (
        None if multiple == 1.5 else "candidate_provider_invalid_output"
    )
    assert len(transport.requests) == (2 if repair else 1)
    if repair:
        correction = transport.requests[1].user_payload
        assert correction is not None and correction["utterance"] == utterance
        assert correction["validationFeedback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewed", [False, True])
@pytest.mark.parametrize("entry_text,price_id,trigger,join,include_price,accepted", [
    ("东方财富放量突破买", "technical.donchian", "price_crosses_above_upper", "all", True, True),
    ("东方财富放量跌破买", "technical.donchian", "price_crosses_below_lower", "all", True, True),
    ("东方财富放量突破买", "technical.donchian", "price_crosses_above_upper", "all", False, False),
    ("东方财富放量突破买", "technical.donchian", "price_crosses_above_upper", "any", True, False),
    ("东方财富放量突破20日均线买", "technical.donchian", "price_crosses_above_upper", "all", True, False),
    ("怡 亚 通放量突破买", "price.rolling_high", "new_high", "all", True, True),
    ("东方财富放量突破买", "price.rolling_high", "new_high", "all", True, True),
    ("东方财富放量跌破买", "price.rolling_high", "new_high", "all", True, False),
    ("东方财富放量突破买", "price.rolling_high", "new_high", "any", True, False),
    ("东方财富放量突破20日均线买", "price.rolling_high", "new_high", "all", True, False),
])
async def test_volume_price_compound_keeps_both_predicates(
    entry_text, price_id, trigger, join, include_price, accepted, reviewed,
):
    # Use the same-side action for a downside entry as well: direction is
    # independent of buy/sell, and may not be silently reversed.
    entry_text = entry_text.replace("卖", "买")
    exit_text = "跌破20日线卖出"
    utterance = f"{entry_text}，{exit_text}"
    entries = [_indicator_payload("volume.relative", "gt_multiple", {
        "baseline_period": 20, "consecutive_days": 3,
    }, value=1)]
    defaults = ["/entry/0/params/baseline_period", "/entry/0/params/consecutive_days",
                "/exit/0/params/price_field"]
    if include_price:
        params = {"period": 20}
        if price_id == "price.rolling_high":
            params["price_field"] = "close"
            defaults.append("/entry/1/params/price_field")
        entries.append(_indicator_payload(price_id, trigger, params))
        defaults.append("/entry/1/params/period")
    payload = {"candidates": [{
        "instrument_symbol": None, "entry": entries,
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })], "entry_spans": [_source_span(utterance, entry_text)] * len(entries),
        "exit_spans": [_source_span(utterance, exit_text)],
        "entry_join": join, "confidence": 0.95, "defaulted_fields": defaults,
    }]}
    class Transport:
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                        "requirements": [{"status": "represented", "candidate_path": "/entry",
                            "source_quote": entry_text, "requested_meaning": "量价条件",
                            "candidate_meaning": "量价条件"}], "differences": []}
            return payload
    generated = await VibeBoundedCandidateGenerator(
        Transport(), capability_matrix=CAPABILITY_MATRIX,
        repair_invalid_output=False, model_semantic_review=reviewed,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 11),
    ))
    # A model's false approval cannot waive missing legs/AND. Explicit boundary
    # meaning still belongs to the semantic reviewer, exercised separately.
    expected = accepted or (reviewed and "20日均线" in entry_text)
    assert (generated[0].unsupported_code is None) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("patch", "safe_code"), [
    ({"indicator_id": "private.provider_indicator"}, "catalog_indicator_unknown"),
    ({"trigger": "private_provider_trigger"}, "catalog_trigger_unknown"),
    ({"params": {"private/provider/key": "private-provider-value"}}, "catalog_parameter_unknown"),
    ({"params": {"fast": "private-provider-value", "slow": 26, "signal": 9}},
     "catalog_parameter_type"),
])
async def test_catalog_matrix_repair_reports_safe_reason_without_provider_content(
    patch: dict[str, object], safe_code: str, caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _macd_batch()
    candidate = cast(list[dict[str, object]], payload["candidates"])[0]
    cast(list[dict[str, object]], candidate["entry"])[0].update(patch)
    transport = _SequenceTransport((payload, payload))

    generated = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, repair_invalid_output=True,
    ).generate(CompileInput(
        utterance=DIRECT_UTTERANCE, instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 5),
    ))

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"
    assert len(transport.requests) == 2
    correction = transport.requests[1].user_payload
    assert correction is not None
    assert correction["validationFeedback"][0] == f"candidate/1:{safe_code}"
    assert f"detail={safe_code}" in caplog.text
    assert "private" not in caplog.text
    assert "private" not in str(correction["validationFeedback"])
    assert _safe_validation_reason(ValueError("private-provider-value")) \
        == "unclassified_validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_supplies_missing_parameter", [True, False])
@pytest.mark.parametrize("semantic_mode", [False, True])
async def test_missing_explicit_volume_baseline_requires_model_repair(
    model_supplies_missing_parameter: bool, semantic_mode: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    baseline = 30 if semantic_mode else 20
    entry = f"当日成交量达到前{baseline}日均量1.5倍买入"
    exit_text = "跌破20日均线卖出"
    utterance = f"生益科技，{entry}，{exit_text}，回测近一年。"
    valid = {"candidates": [{
        "entry": [_indicator_payload("volume.relative", "gte_multiple", {
            "baseline_period": baseline, "consecutive_days": 3,
        }, value=1.5)],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })],
        "entry_spans": [_source_span(utterance, entry)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "backtest_lookback_years": 1,
        "backtest_span": _source_span(utterance, "回测近一年"),
        "confidence": 0.95,
        "defaulted_fields": ["/entry/0/params/consecutive_days", "/exit/0/params/price_field"],
    }]}
    incomplete = deepcopy(valid)
    # A Catalog default must not override the explicitly requested baseline.
    del incomplete["candidates"][0]["entry"][0]["params"]["baseline_period"]
    class Transport(_SequenceTransport):
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                actual = request.user_payload["candidate"]["entry"][0]["params"]["baseline_period"]
                correct = actual == baseline
                return {"instrument": "equivalent",
                        "requested_bar_interval": "unspecified",
                        "requirements": [{"status": "represented", "candidate_path": "/entry",
                            "source_quote": request.utterance,
                            "requested_meaning": "测试条件", "candidate_meaning": "测试条件"}],
                        "differences": [] if correct else [{
                            "candidate_path": "/entry/0/params/baseline_period",
                            "source_quote": entry, "requested_meaning": "均量周期30",
                            "candidate_meaning": "均量周期20",
                        }]}
            return await super().generate_json(request)

    transport = Transport((
        incomplete, valid if model_supplies_missing_parameter else incomplete,
    ))

    generated = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, repair_invalid_output=True,
        model_semantic_review=semantic_mode,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="600183.SH", as_of_date=date(2026, 9, 5),
    ))

    expected_failure = (
        "semantic_confirmation_required" if semantic_mode else "candidate_provider_invalid_output"
    )
    assert generated[0].unsupported_code == (
        None if model_supplies_missing_parameter else expected_failure
    )
    assert len(transport.requests) == 2
    correction = transport.requests[1].user_payload
    assert correction is not None and correction["utterance"] == utterance
    feedback = str(correction["validationFeedback"])
    if semantic_mode:
        assert "baseline_period" in feedback and "均量周期30" in feedback
        assert "均量周期20" in feedback
    else:
        assert "catalog_parameter_required" in feedback
        assert "/entry/0/params/baseline_period" in feedback
        assert "目录默认值为 20" in feedback and "defaulted_fields" in feedback
        assert "原文已指定的参数必须忠实保留" in feedback
        assert "detail=catalog_parameter_required" in caplog.text
    assert "baseline_period" not in incomplete["candidates"][0]["entry"][0]["params"]
    if model_supplies_missing_parameter:
        assert dict(generated[0].entry[0].params) == {
            "baseline_period": baseline,
        }
        assert generated[0].entry[0].value == 1.5
        assert "/entry/0/params/baseline_period" not in generated[0].defaulted_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("model_corrects_quote", [True, False])
async def test_shared_exit_action_is_repaired_by_model_with_exact_quote_feedback(
    model_corrects_quote: bool,
) -> None:
    entry = "贵州茅台收盘价低于25日均线买入"
    exit_text = "高于20日均线或从持仓最高价回撤6%卖出"
    utterance = f"{entry}，{exit_text}"
    valid = {"candidates": [{
        "instrument_symbol": None,
        "entry": [_indicator_payload("technical.ma", "price_below", {
            "period": 25, "price_field": "close",
        })],
        "exit": [
            _indicator_payload("technical.ma", "price_above", {
                "period": 20, "price_field": "close",
            }),
            {"kind": "trailing_drawdown", "threshold_pct": 6},
        ],
        "entry_spans": [_source_span(utterance, entry)],
        "exit_spans": [_source_span(utterance, exit_text)] * 2,
        "exit_join": "any", "confidence": 0.95,
        "defaulted_fields": ["/exit/0/params/price_field"],
    }]}
    invalid = deepcopy(valid)
    invalid["candidates"][0]["exit_spans"][0] = {
        **_source_span(utterance, exit_text), "text": "收盘价高于20日均线卖出",
    }
    transport = _SequenceTransport((invalid, valid if model_corrects_quote else invalid))
    generated = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, repair_invalid_output=True,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="600519.SH", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == (
        None if model_corrects_quote else "candidate_provider_invalid_output"
    )
    assert len(transport.requests) == 2
    correction = transport.requests[1].user_payload
    assert correction is not None and correction["utterance"] == utterance
    feedback = str(correction["validationFeedback"])
    assert "收盘价高于20日均线卖出" in feedback
    assert "共用买卖动作" in feedback


@pytest.mark.asyncio
@pytest.mark.parametrize(("movement", "trigger"), [("下跌", "below"), ("上涨", "above")])
async def test_daily_movement_can_reselect_relative_operator_without_inventing_price(movement, trigger):
    entry = f"买入规则：20日均线高于60日均线，当天{movement}但收盘仍在20日线上方"
    exit_text = "卖出规则：跌破20日线，或持有10个交易日"
    utterance = f"东方财富（300059.SZ），趋势回踩。{entry}；{exit_text}。"
    ma_params = {"period": 20, "price_field": "close"}
    valid = {"candidates": [{
        "entry": [
            _indicator_payload("technical.ma_cross", "fast_above_slow",
                               {"fast_period": 20, "slow_period": 60, "price_field": "close"}),
            _indicator_payload("price.return_pct", trigger, {"period": 1, "price_field": "close"}, value=0),
            _indicator_payload("technical.ma", "price_above", ma_params),
        ],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", ma_params),
                 {"kind": "holding_period", "sessions": 10}],
        "entry_join": "all", "exit_join": "any",
        "entry_spans": [_source_span(utterance, entry)] * 3,
        "exit_spans": [_source_span(utterance, exit_text)] * 2,
        "confidence": .95, "defaulted_fields": ["/entry/0/params/price_field", "/exit/0/params/price_field"],
    }]}
    invalid = deepcopy(valid)
    invalid["candidates"][0]["entry"][1] = _indicator_payload("price.close", trigger, {}, value=None)
    class Transport:
        def __init__(self):
            self.requests = []

        async def generate_json(self, request):
            self.requests.append(request)
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                        "requirements": [{"status": "represented", "candidate_path": "/entry",
                            "source_quote": entry, "requested_meaning": "均线位置与当日涨跌同时满足",
                            "candidate_meaning": "均线位置与当日涨跌同时满足"}], "differences": []}
            return invalid if len(self.requests) == 1 else valid

    transport = Transport()
    # Match the live route: structure is strict; source meaning is reviewed,
    # not rejected for omitting literal digits "1" and "0" in "当天下跌".
    result = await VibeBoundedCandidateGenerator(transport, capability_matrix=CAPABILITY_MATRIX,
        repair_invalid_output=True, model_semantic_review=True).generate(CompileInput(utterance=utterance,
            instrument_context="300059.SZ", as_of_date=date(2026, 9, 11)))
    assert result[0].unsupported_code is None
    assert len(result[0].entry) == 3 and len(result[0].exit) == 2
    assert result[0].entry[1].indicator_id == "price.return_pct"
    assert result[0].entry[1].value == 0
    assert dict(result[0].entry[1].params)["period"] == 1
    assert len(transport.requests) == 3
    assert transport.requests[1].user_payload["utterance"] == utterance


@pytest.mark.asyncio
@pytest.mark.parametrize("cash_quote", [None, "10万元", "本金10万元"])
async def test_model_grounded_initial_cash_flows_into_compiled_strategy(
    cash_quote: str | None,
) -> None:
    rule = "MACD金叉买入，MACD死叉卖出"
    utterance = f"{rule}，本金10万元"
    payload = _macd_batch(utterance=rule)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    candidate["initial_cash_cny"] = 100_000
    candidate["initial_cash_span"] = (
        None if cash_quote is None else _source_span(utterance, cash_quote)
    )
    transport = _FakeTransport(payload)
    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=_bounded(transport),
        model_first=True,
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.initial_cash_cny == 100_000
    assert {
        item.path: item.source for item in outcome.provenance
    }["/backtest/initial_cash_cny"] == "utterance/explicit_initial_cash"
    assert any(
        item.path == "/backtest/initial_cash_cny" and item.text == "本金10万元"
        for item in outcome.candidate_grounding
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("settings", "evidence"), [
    (
        {"slippage_bps": "0", "commission_rate": "0", "minimum_commission_cny": "0"},
        {"slippage_bps": "滑点0", "commission_rate": "佣金0",
         "minimum_commission_cny": "最低佣金0"},
    ),
    (
        {"slippage_bps": "5", "commission_rate": "0.0003", "minimum_commission_cny": "5"},
        {"slippage_bps": "滑点0.05%", "commission_rate": "佣金万分之三",
         "minimum_commission_cny": "最低佣金5元"},
    ),
    (
        {"slippage_bps": "5", "commission_rate": "0.0003"},
        {"slippage_bps": "滑点5个基点", "commission_rate": "佣金率0.03%"},
    ),
])
async def test_model_execution_settings_preserve_zero_and_normalized_units(
    settings: dict[str, str], evidence: dict[str, str],
) -> None:
    # The model supplies the conversion; the adapter must not substitute defaults.
    utterance = f"{DIRECT_UTTERANCE}，{'，'.join(evidence.values())}"
    payload = _macd_batch()
    candidate = cast(list[dict[str, object]], payload["candidates"])[0]
    candidate.update(execution_settings=settings, execution_setting_evidence=evidence)
    transport = _FakeTransport(payload)
    generated = await _bounded(transport).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))

    assert generated[0].unsupported_code is None
    assert generated[0].execution_settings.model_dump(exclude_none=True) == {
        name: Decimal(value) for name, value in settings.items()
    }
    contract = transport.requests[0].system_contract
    assert "0 和 false 是明确设置" in contract
    assert "滑点0.05%写5" in contract and "佣金万分之三或万三写0.0003" in contract
    assert "不能遗漏费用" in contract


@pytest.mark.asyncio
async def test_unmentioned_execution_settings_stay_empty_for_legacy_model_output() -> None:
    generated = await _bounded(_FakeTransport(_macd_batch())).generate(CompileInput(
        utterance=DIRECT_UTTERANCE, instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 5),
    ))

    assert generated[0].unsupported_code is None
    assert generated[0].execution_settings.model_dump(exclude_none=True) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("suffix", "declined"), [
    ("股票等我补充，先别跑", True),
    ("先别跑，请帮我推荐三只股票", False),
])
async def test_model_stock_selection_preference_controls_initial_screening(
    suffix: str, declined: bool,
) -> None:
    # Provider-output plumbing, not a rule-based interpretation or live acceptance.
    payload = _macd_batch()
    candidate = cast(list[dict[str, object]], payload["candidates"])[0]
    candidate["instrument_suggestion_declined"] = declined
    transport = _FakeTransport(payload)
    compiler = StrategyCompiler(
        generator=_bounded(transport), catalog=CATALOG,
        catalog_id="cn_a.signals", release_version=CATALOG_RELEASE,
        backtest_anchor_date=date(2026, 9, 5),
    )
    request = CompileInput(f"{DIRECT_UTTERANCE}，{suffix}", date(2026, 9, 5))
    outcome = await compiler.compile(request)
    assert outcome.diagnostic_code == "instrument_required"
    assert outcome.instrument_suggestion_declined is declined
    assert outcome.selected_idea_proposal is not None
    assert outcome.selected_idea_proposal.strategy_template is not None
    assert not outcome.run_requested

    class Screener:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            self.queries.append(query)
            raise MxSaasProviderNoDataError("fixture contains no stocks")

    screener = Screener()
    container = cast(ApiContainer, create_app(
        compiler=compiler, live_market_data=screener,
    ).state.container)
    offered, selected = await _offer_missing_instrument(
        outcome=outcome, compile_input=request, state=None, container=container,
    )
    assert selected is None and offered.selected_idea_proposal == outcome.selected_idea_proposal
    assert len(screener.queries) == (0 if declined else 2)
    contract = transport.requests[0].system_contract
    assert "instrument_suggestion_declined=true" in contract
    assert "缺少股票本身、或只说先别跑，不能据此拒绝推荐" in contract
    assert "用户本轮明确请求帮忙推荐股票时，此字段为false" in contract


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "candidate_data_not_ready", "preflight_timeout"])
async def test_optional_stock_offer_failure_restores_original_question_and_rules(monkeypatch, failure):
    from unittest.mock import AsyncMock
    from ashare_lab.api.routes import strategy_drafts
    compiler = StrategyCompiler(generator=_bounded(_FakeTransport(_macd_batch())),
        catalog=CATALOG, catalog_id="cn_a.signals", release_version=CATALOG_RELEASE,
        backtest_anchor_date=date(2026, 9, 5))
    request = CompileInput(DIRECT_UTTERANCE, date(2026, 9, 5))
    outcome = await compiler.compile(request)
    assert outcome.diagnostic_code == "instrument_required"
    assert outcome.selected_idea_proposal is not None
    offer = AsyncMock(side_effect=TimeoutError()) if failure == "timeout" else AsyncMock(
        return_value=(replace(outcome, diagnostic_code="candidate_data_not_ready",
                              clarification="不完整候选不提供"), None))
    monkeypatch.setattr(strategy_drafts, "_offer_missing_instrument_impl", offer)
    if failure == "preflight_timeout":
        offer.return_value = (outcome, None)
        preflight = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(strategy_drafts, "_preflight_idea_choices", preflight)
    container = create_app(compiler=compiler).state.container
    restored, memory = await _offer_missing_instrument(outcome=outcome,
        compile_input=request, state=None, container=container)
    assert restored is outcome and memory is None
    assert restored.selected_idea_proposal.strategy_template == outcome.selected_idea_proposal.strategy_template
    assert restored.clarification == "请告诉我想回测哪一只 A 股（股票名称或 6 位代码）。"
    if failure == "preflight_timeout":
        preflight.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("settings", "evidence"), [
    ({"slippage_bps": "5"}, {}),
    ({}, {"slippage_bps": "滑点0"}),
    ({"slippage_bps": "5"}, {"slippage_bps": "滑点5个基点"}),
    ({"slippage_bps": "0"}, {"slippage_bps": ""}),
])
async def test_execution_settings_require_matching_exact_quotes(
    settings: dict[str, str], evidence: dict[str, str],
) -> None:
    payload = _macd_batch()
    candidate = cast(list[dict[str, object]], payload["candidates"])[0]
    candidate.update(execution_settings=settings, execution_setting_evidence=evidence)
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=f"{DIRECT_UTTERANCE}，滑点0", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 5),
    ))

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [None, "600519.SH", "300059.SZ"])
async def test_model_name_anywhere_is_resolved_before_execution(context: str | None) -> None:
    utterance = f"{DIRECT_UTTERANCE}。这次测试贵州茅台"
    payload = _macd_batch(utterance=DIRECT_UTTERANCE)
    candidate = cast(dict[str, object], cast(list[object], payload["candidates"])[0])
    candidate["instrument_name"] = "贵州茅台"
    candidate["instrument_span"] = _source_span(utterance, "贵州茅台")
    calls: list[str] = []

    def resolver(name: str) -> str:
        calls.append(name)
        return "600519.SH"

    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=_bounded(_FakeTransport(payload)),
        instrument_name_resolver=resolver,
        model_first=True,
    )
    generated = await generator.generate(CompileInput(
        utterance=utterance, instrument_context=context, as_of_date=date(2026, 9, 5),
    ))
    assert calls == ["贵州茅台"]
    if context == "300059.SZ":
        assert generated[0].unsupported_code == "instrument_context_mismatch"
        assert generated[0].instrument_symbol is None
    else:
        assert generated[0].unsupported_code is None
        assert generated[0].instrument_symbol == "600519.SH"
        assert generated[0].instrument_name is None
        assert any(item.path == "/instrument/symbol" and item.text == "贵州茅台"
                   for item in generated[0].grounding_evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_review", "hybrid_model", "hybrid_hint", "deterministic"])
async def test_ambiguous_name_choices_survive_each_candidate_identity_boundary(stage: str) -> None:
    utterance = f"东财，{DIRECT_UTTERANCE}"
    payload = _macd_batch()
    item = cast(list[dict[str, object]], payload["candidates"])[0]
    item.update({
        "instrument_name": None if stage == "hybrid_hint" else "东财",
        "instrument_span": None if stage == "hybrid_hint" else _source_span(utterance, "东财"),
        "entry_spans": [_source_span(utterance, "MACD金叉买入")],
        "exit_spans": [_source_span(utterance, "MACD死叉卖出")],
    })
    choices = (InstrumentNameCandidate(
        "300059.SZ", "东方财富", "eastmoney_security_search",
        datetime(2026, 9, 15, tzinfo=UTC),
    ),)

    def resolver(name: str) -> str:
        assert name == "东财"
        raise InstrumentNameAmbiguous(choices)

    transport = _FakeTransport(payload)
    bounded = VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX,
        model_semantic_review=stage == "before_review",
        instrument_name_resolver=resolver if stage == "before_review" else None,
    )
    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(), bounded_fallback=bounded,
        instrument_name_resolver=resolver, model_first=stage != "deterministic",
    )
    candidate = (await generator.generate(CompileInput(
        utterance=utterance, as_of_date=date(2026, 9, 15),
    )))[0]
    assert candidate.unsupported_code == "instrument_name_ambiguous"
    assert candidate.instrument_symbol is None
    assert candidate.instrument_name == "东财"
    assert candidate.instrument_candidates == choices
    assert len(transport.requests) == (0 if stage == "deterministic" else 1)
    if stage != "deterministic":
        assert candidate.entry and candidate.exit


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["name_not_in_source", "invented_code"])
async def test_model_name_does_not_bypass_source_or_security_identity(defect: str) -> None:
    utterance = f"{DIRECT_UTTERANCE}。这次测试贵州茅台"
    payload = _macd_batch(utterance=DIRECT_UTTERANCE)
    candidate = cast(dict[str, object], cast(list[object], payload["candidates"])[0])
    candidate["instrument_name"] = "东方财富" if defect == "name_not_in_source" else "贵州茅台"
    candidate["instrument_span"] = _source_span(utterance, "贵州茅台")
    if defect == "invented_code":
        candidate["instrument_symbol"] = "600519.SH"
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context=None, as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_cash", (None, 1_000_000))
async def test_explicit_initial_cash_is_not_omitted_or_changed_by_model(
    returned_cash: int | None,
) -> None:
    rule = "MACD金叉买入，MACD死叉卖出"
    utterance = f"{rule}，本金10万元"
    payload = _macd_batch(utterance=rule)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    if returned_cash is not None:
        candidate["initial_cash_cny"] = returned_cash
        candidate["initial_cash_span"] = _source_span(utterance, "本金10万元")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_index", "deviation"),
    (
        (0, "pronoun_changes_indicator_parameters"),
        (3, "drops_one_and_condition"),
        (4, "adds_natural_day_unit"),
    ),
)
async def test_exact_five_provider_shortcuts_fail_closed(
    case_index: int,
    deviation: str,
) -> None:
    case = _exact_five_cases()[case_index]
    payload = deepcopy(case.payload)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    if deviation == "pronoun_changes_indicator_parameters":
        exit_leaves = cast(list[object], candidate["exit"])
        exit_leaf = cast(dict[str, object], exit_leaves[0])
        params = cast(dict[str, object], exit_leaf["params"])
        params["period"] = 10
    elif deviation == "drops_one_and_condition":
        entry_leaves = cast(list[object], candidate["entry"])
        entry_spans = cast(list[object], candidate["entry_spans"])
        entry_leaves.pop()
        entry_spans.pop()
        defaulted_fields = cast(list[object], candidate["defaulted_fields"])
        candidate["defaulted_fields"] = [
            item for item in defaulted_fields if not str(item).startswith("/entry/1/")
        ]
    elif deviation == "adds_natural_day_unit":
        exit_leaves = cast(list[object], candidate["exit"])
        exit_leaf = cast(dict[str, object], exit_leaves[0])
        exit_leaf["unit"] = "natural_days"
    else:  # pragma: no cover - the parameter table is closed above
        raise AssertionError(f"unknown deviation: {deviation}")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_rsi_recovery_cannot_be_downgraded_to_static_above() -> None:
    case = _exact_five_cases()[2]
    payload = deepcopy(case.payload)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    exit_leaves = cast(list[object], candidate["exit"])
    exit_leaf = cast(dict[str, object], exit_leaves[0])
    exit_leaf["trigger"] = "above"

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_context_cannot_silently_discard_unused_instrument_evidence() -> None:
    utterance = "600519.SH MACD金叉买入，MACD死叉卖出"
    payload = _macd_batch(utterance=utterance)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    candidate["instrument_symbol"] = None
    candidate["instrument_span"] = _source_span(utterance, "600519.SH")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "entry", "exit", "entry_text", "exit_text", "defaults"),
    (
        (
            "MACD金叉就买，跌回这条线下就走",
            _indicator_payload(
                "technical.macd",
                "golden_cross",
                {"fast": 12, "slow": 26, "signal": 9},
            ),
            _indicator_payload(
                "technical.ma",
                "price_crosses_below",
                {"period": 20, "price_field": "close"},
            ),
            "MACD金叉就买",
            "跌回这条线下就走",
            [
                "/entry/0/params/fast",
                "/entry/0/params/signal",
                "/entry/0/params/slow",
                "/exit/0/params/period",
                "/exit/0/params/price_field",
            ],
        ),
        (
            "RSI低于30就买，往下穿回去就卖",
            _indicator_payload(
                "technical.rsi",
                "below",
                {"period": 14},
                value=30,
            ),
            _indicator_payload(
                "technical.macd",
                "death_cross",
                {"fast": 12, "slow": 26, "signal": 9},
            ),
            "RSI低于30就买",
            "往下穿回去就卖",
            [
                "/entry/0/params/period",
                "/exit/0/params/fast",
                "/exit/0/params/signal",
                "/exit/0/params/slow",
            ],
        ),
        (
            "MACD金叉就买，到70上方就收手",
            _indicator_payload(
                "technical.macd",
                "golden_cross",
                {"fast": 12, "slow": 26, "signal": 9},
            ),
            _indicator_payload(
                "technical.rsi",
                "crosses_above",
                {"period": 14},
                value=70,
            ),
            "MACD金叉就买",
            "到70上方就收手",
            [
                "/entry/0/params/fast",
                "/entry/0/params/signal",
                "/entry/0/params/slow",
                "/exit/0/params/period",
            ],
        ),
    ),
)
async def test_contextual_reference_cannot_name_a_different_indicator(
    utterance: str,
    entry: dict[str, object],
    exit: dict[str, object],
    entry_text: str,
    exit_text: str,
    defaults: list[str],
) -> None:
    response: dict[str, object] = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [entry],
                "exit": [exit],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [_source_span(utterance, exit_text)],
                "confidence": 0.91,
                "defaulted_fields": defaults,
            }
        ]
    }
    candidates = await _bounded(_FakeTransport(response)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_bounded_grounding_accepts_safe_complete_buy_sell_phrases() -> None:
    utterance = "MACD金叉才买，MACD死叉转弱 卖"
    generator = _bounded(_FakeTransport(_macd_batch(utterance=utterance)))

    candidates = await generator.generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code is None


@pytest.mark.asyncio
async def test_generic_entry_placeholder_does_not_use_bounded_fallback() -> None:
    utterance = "随便上车，MACD死叉卖出"
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "entry_rule_not_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_generic_unrecognized_placeholder_does_not_use_bounded_fallback() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="你看着随便帮我交易",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "no_supported_signal_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_explicitly_unsupported_semantics_never_use_bounded_fallback() -> None:
    utterance = "业绩预告净利润增长超过30%买入，MACD死叉卖出"
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "event_attribute_filter_not_supported"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_candidates_are_ranked_and_low_confidence_requests_one_clarification() -> None:
    weak = _macd_batch()["candidates"][0]  # type: ignore[index]
    assert isinstance(weak, dict)
    weak["confidence"] = 0.41
    strong = _macd_batch()["candidates"][0]  # type: ignore[index]
    assert isinstance(strong, dict)
    strong["confidence"] = 0.93
    transport = _FakeTransport({"candidates": [weak, strong]})
    generator = _bounded(transport)

    ranked = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert [item.confidence for item in ranked] == [0.93, 0.0]
    assert ranked[1].unsupported_code == "candidate_provider_low_confidence"

    low_transport = _FakeTransport({"candidates": [weak]})
    outcome = await StrategyCompiler(
        generator=_bounded(low_transport),
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "candidate_provider_low_confidence"
    assert outcome.clarification is not None
    assert outcome.strategy is None
    assert outcome.strategy_hash is None


@pytest.mark.asyncio
async def test_catalog_invalid_first_candidate_does_not_hide_valid_second_candidate() -> None:
    invalid = _macd_batch()["candidates"][0]  # type: ignore[index]
    valid = _macd_batch()["candidates"][0]  # type: ignore[index]
    assert isinstance(invalid, dict)
    assert isinstance(valid, dict)
    invalid["confidence"] = 0.99
    invalid_entry = invalid["entry"]  # type: ignore[index]
    assert isinstance(invalid_entry, list)
    assert isinstance(invalid_entry[0], dict)
    invalid_entry[0]["trigger"] = "invented_trigger"
    valid["confidence"] = 0.91
    transport = _FakeTransport({"candidates": [invalid, valid]})
    generator = _bounded(transport)

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.candidate_rejections[0].candidate_rank == 1
    assert outcome.candidate_rejections[0].diagnostic_code == ("candidate_provider_invalid_output")



@pytest.mark.asyncio
@pytest.mark.parametrize(("entry_text", "exit_text", "narrow_quote"), [
    ("收盘价上穿20日均线买", "收盘价下穿20日均线卖", False),
    ("收盘价上穿7日均线时买", "收盘价下穿7日均线时卖", False),
    ("买：收盘价上穿20日均线", "卖：收盘价下穿20日均线", False),
    ("收盘价上穿20日均线买", "收盘价下穿20日均线卖", True),
])
async def test_model_short_trade_actions_keep_exact_condition_evidence(
    entry_text: str, exit_text: str, narrow_quote: bool,
) -> None:
    utterance = f"帮我测东方财富，{entry_text}，{exit_text}，最近一年。"
    period = 7 if "7日" in entry_text else 20
    params = {"period": period, "price_field": "close"}
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.ma", "price_crosses_above", params)],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", params)],
        "entry_spans": [_source_span(utterance, "买" if narrow_quote else entry_text)],
        "exit_spans": [_source_span(utterance, "卖" if narrow_quote else exit_text)],
        "backtest_lookback_years": 1,
        "backtest_span": _source_span(utterance, "最近一年"),
        "confidence": 0.91, "defaulted_fields": [],
    }]}
    transport = _FakeTransport(payload)
    generated = await _bounded(transport).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code is None
    assert len(transport.requests) == 1
    evidence = {item.path: item for item in generated[0].grounding_evidence}
    for path, expected in (("/entry/0", entry_text), ("/exit/0", exit_text)):
        item = evidence[path]
        assert item.text == expected
        assert utterance[item.start:item.end] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "noun"), [
    ("entry", "超买"), ("exit", "超卖"), ("entry", "买方"),
    ("exit", "卖盘"), ("entry", "不买"), ("exit", "卖出价格"),
])
async def test_short_trade_character_does_not_turn_nouns_or_references_into_orders(
    side: str, noun: str,
) -> None:
    entry_text = f"收盘价上穿20日均线{noun if side == 'entry' else '买'}"
    exit_text = f"收盘价下穿20日均线{noun if side == 'exit' else '卖'}"
    utterance = f"{entry_text}，{exit_text}"
    params = {"period": 20, "price_field": "close"}
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.ma", "price_crosses_above", params)],
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", params)],
        "entry_spans": [_source_span(utterance, entry_text)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.91, "defaulted_fields": [],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_span", ["mixed_actions", "invented_action"])
async def test_short_actions_reduce_mixed_span_but_reject_invented_quote(bad_span: str) -> None:
    utterance = "MACD金叉买，MACD死叉卖"
    payload = _macd_batch(utterance=utterance)
    candidate = cast(dict[str, object], cast(list[object], payload["candidates"])[0])
    candidate["entry_spans"] = [
        _source_span(utterance, utterance) if bad_span == "mixed_actions" else
        {"start": 0, "end": 8, "text": "MACD金叉买入"}
    ]
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert generated[0].unsupported_code == (
        None if bad_span == "mixed_actions" else "candidate_provider_invalid_output"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["whole", "action"])
async def test_unpunctuated_cross_spans_keep_exact_sides(quote: str) -> None:
    utterance = "MACD金叉买死叉卖"
    payload = _macd_batch(utterance="MACD金叉买，死叉卖")
    candidate = payload["candidates"][0]
    candidate["entry_spans"] = [_source_span(utterance, utterance if quote == "whole" else "买")]
    candidate["exit_spans"] = [_source_span(utterance, utterance if quote == "whole" else "卖")]
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 7),
    ))
    assert generated[0].unsupported_code is None
    evidence = {item.path: item.text for item in generated[0].grounding_evidence}
    assert evidence["/entry/0"] == "MACD金叉买"
    assert evidence["/exit/0"] == "死叉卖"


@pytest.mark.asyncio
@pytest.mark.parametrize(("comparison", "trigger", "valid"), [
    ("超过", "gt_multiple", True), ("不低于", "gte_multiple", True),
    ("超过", "gte_multiple", False), ("不低于", "gt_multiple", False),
    (">", "gt_multiple", True), (">", "gte_multiple", False),
    (">=", "gte_multiple", True), (">=", "gt_multiple", False),
])
@pytest.mark.parametrize("whole_unpunctuated_quote", [False, True])
@pytest.mark.parametrize("reviewed", [False, True])
async def test_model_relative_volume_preserves_baseline_and_strict_comparator(
    comparison: str, trigger: str, valid: bool, whole_unpunctuated_quote: bool, reviewed: bool,
) -> None:
    entry = f"收盘价创前20日新高且成交量{comparison}前20日均量1.5倍买入"
    exit_text = "跌破20日均线卖出"
    utterance = f"{entry}{'' if whole_unpunctuated_quote else '，'}{exit_text}"
    payload = {"candidates": [{
        "entry": [
            _indicator_payload("price.rolling_high", "new_high", {
                "period": 20, "price_field": "close",
            }),
            _indicator_payload("volume.relative", trigger, {
                "baseline_period": 20, "consecutive_days": 3,
            }, value=1.5),
        ],
        "entry_join": "all",
        "exit": [_indicator_payload("technical.ma", "price_crosses_below", {
            "period": 20, "price_field": "close",
        })],
        "entry_spans": [_source_span(
            utterance, utterance if whole_unpunctuated_quote else entry,
        )] * 2,
        "exit_spans": [_source_span(
            utterance, utterance if whole_unpunctuated_quote else exit_text,
        )],
        "confidence": 0.91,
        "defaulted_fields": ["/entry/1/params/consecutive_days", "/exit/0/params/price_field"],
    }]}
    class Transport:
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                        "requirements": [{"status": "represented", "candidate_path": "/entry",
                            "source_quote": entry, "requested_meaning": "量价条件",
                            "candidate_meaning": "量价条件"}], "differences": []}
            return payload

    generated = await VibeBoundedCandidateGenerator(
        Transport(), capability_matrix=CAPABILITY_MATRIX,
        repair_invalid_output=False, model_semantic_review=reviewed,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert (generated[0].unsupported_code is None) is valid


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_text", "exit_period", "valid"), [
    ("高于70卖", 14, True), ("高于70卖", 7, False), ("MACD高于70卖", 14, False),
])
async def test_model_omitted_repeated_indicator_keeps_unique_entry_subject(
    exit_text: str, exit_period: int, valid: bool,
) -> None:
    entry = "用14日RSI，低于30买"
    utterance = f"{entry}，{exit_text}"
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.rsi", "below", {"period": 14}, value=30)],
        "exit": [_indicator_payload("technical.rsi", "above", {"period": exit_period}, value=70)],
        "entry_spans": [_source_span(utterance, entry)],
        "exit_spans": [_source_span(utterance, exit_text)],
        "confidence": 0.91, "defaulted_fields": [],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="600519.SH", as_of_date=date(2026, 9, 5),
    ))
    assert (generated[0].unsupported_code is None) is valid


@pytest.mark.asyncio
@pytest.mark.parametrize(("amount_text", "amount_value", "price_rule", "include_price", "valid"), [
    ("超过5亿元", 500_000_000, False, False, True),
    ("高于50000万元", 500_000_000, False, False, True),
    ("大于500000000元", 500_000_000, False, False, True),
    ("超过5亿元", 5, False, False, False),
    ("不超过5亿元", 500_000_000, False, False, False),
    ("不高于5亿元", 500_000_000, False, False, False),
    ("超过5亿元", 500_000_000, True, False, False),
    ("超过5亿元", 500_000_000, True, True, True),
])
async def test_model_amount_threshold_preserves_cny_comparator_and_other_price_rule(
    amount_text: str, amount_value: int, price_rule: bool, include_price: bool, valid: bool,
) -> None:
    entry = f"14日RSI从30下方上穿30且当日成交额{amount_text}"
    if price_rule:
        entry += "且收盘价低于30元"
    entry += "买入"
    exit_text = "14日RSI高于55、持仓亏损5%或持有满10个交易日卖出"
    utterance = f"{entry}；{exit_text}"
    leaves = [
        _indicator_payload("technical.rsi", "crosses_above", {"period": 14}, value=30),
        _indicator_payload("market.amount", "above", {}, value=amount_value),
    ]
    if include_price:
        leaves.append(_indicator_payload("price.close", "below", {}, value=30))
    payload = {"candidates": [{
        "entry": leaves,
        "entry_join": "all",
        "exit": [
            _indicator_payload("technical.rsi", "above", {"period": 14}, value=55),
            {"kind": "position_return", "trigger": "stop_loss", "threshold_pct": 5},
            {"kind": "holding_period", "sessions": 10},
        ],
        "exit_join": "any",
        "entry_spans": [_source_span(utterance, entry)] * len(leaves),
        "exit_spans": [_source_span(utterance, exit_text)] * 3,
        "confidence": 0.95,
        "defaulted_fields": [],
    }]}
    generated = await _bounded(_FakeTransport(payload)).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    ))
    assert (generated[0].unsupported_code is None) is valid


@pytest.mark.parametrize(("separator", "valid"), [("、", True), ("、且", False), ("，", False)])
def test_exit_disjunction_list_does_not_accept_mixed_and_or_or_commas(
    separator: str, valid: bool,
) -> None:
    text = f"14日RSI高于55{separator}持仓亏损5%或持有满10个交易日卖出"
    span = CandidateSourceSpan(start=0, end=len(text), text=text)
    if valid:
        _validate_join_grounding("any", (span,) * 3, side="exit", leaf_count=3, utterance=text)
    else:
        with pytest.raises(ValueError):
            _validate_join_grounding("any", (span,) * 3, side="exit", leaf_count=3, utterance=text)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,side', [('rebound','buy'), ('pullback','sell'), ('price','buy')])
async def test_missing_plan_parameter_can_repair_to_non_executable_guidance(kind, side):
    utterance = '600236.SH反弹买入与回落卖出' if kind != 'price' else '600236.SH到价买入'
    partial = {'instrument_symbol': '600236.SH',
               'instrument_span': _source_span(utterance, '600236.SH'),
               'entry': [], 'exit': [], 'entry_spans': [], 'exit_spans': [], 'confidence': .99}
    bad = {**partial, 'trading_plan': {'kind': 'conditional', 'parameters': {
        'rules': [{'kind': kind, 'side': side, 'quantity': 100}],
    }}, 'plan_span': _source_span(utterance, utterance[9:])}
    transport = _SequenceTransport(({'candidates': [bad]}, {'candidates': [partial]}))
    result = await VibeBoundedCandidateGenerator(transport, capability_matrix=CAPABILITY_MATRIX,
        repair_invalid_output=True).generate(CompileInput(utterance=utterance,
        instrument_context='600236.SH', as_of_date=date(2026,9,11)))
    assert len(transport.requests) == 2
    assert result[0].unsupported_code == 'strategy_rule_incomplete'
    assert result[0].instrument_symbol == '600236.SH'
    assert result[0].trading_plan is None
    assert transport.requests[1].utterance == utterance
    assert 'do not invent a value' in str(transport.requests[1].user_payload)
@pytest.mark.parametrize("reference", ["买入成交价", "买入的成交均价", "买入实际成交价", "卖出成交价"])
def test_trade_fill_price_reference_is_not_a_new_action(reference):
    from ashare_lab.adapters.language.vibe_candidates import _has_trade_action, _ENTRY_ACTION_WORDS, _EXIT_ACTION_WORDS
    assert not _has_trade_action(reference, _ENTRY_ACTION_WORDS)
    assert not _has_trade_action(reference, _EXIT_ACTION_WORDS)
    assert _has_trade_action(reference + "下跌5%买入100股", _ENTRY_ACTION_WORDS)
    assert _has_trade_action(reference + "上涨5%卖出全部持仓", _EXIT_ACTION_WORDS)
