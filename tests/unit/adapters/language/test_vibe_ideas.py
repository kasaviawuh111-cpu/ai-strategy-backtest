from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from ashare_lab.adapters.language.inspiration_stock_selection import (
    InspirationStockSelector,
    is_self_contained_perishable_food_inspiration,
)
from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.adapters.language.vibe_ideas import (
    VibeIdeaRouter,
    _repair_idea_explanation,
    _with_internal_grid_guardrails,
    _safe_schema_feedback,
    _parse_provider_route,
    _idea_response_schema,
    _validate_explicit_comparisons,
)


@pytest.mark.parametrize("metric,query", [("PE", "市盈率PE(TTM)"), ("PB", "市净率"), ("ROE", "净资产收益率"), ("每股收益", "每股收益")])
@pytest.mark.parametrize("trigger,valid", [("below", True), ("crosses_above", False), ("above", False)])
def test_idea_preserves_explicit_scalar_direction(metric, query, trigger, valid):
    strategy = SimpleNamespace(entry=IndicatorCondition(
        indicator_id="provider.numeric", definition_version="1.0.0",
        params={"metric_query": query, "unit": "倍"}, trigger=trigger, value=20,
    ), exit=None)
    text = f"东方财富{metric}低于20买，高了卖"
    if valid:
        _validate_explicit_comparisons(strategy, text)
    else:
        with pytest.raises(ValueError, match="候选遗漏或改写"):
            _validate_explicit_comparisons(strategy, text)
from ashare_lab.api.app import build_hybrid_candidate_compiler
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchPurpose,
    ResearchSource,
)
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import (
    IdeaGenerationError,
    IdeaResearchUnavailableError,
    IdeaStockSelection,
    IdeaStockSelectionUnavailableError,
)
from ashare_lab.ports.live_market_data import LiveMarketDataProvenance, LiveMarketDataResult


def _selection_evidence():
    return LiveMarketDataResult(
        provider="fixture", query="fixture", asset_type="A股",
        columns=("代码", "名称", "主营业务"),
        rows=({"代码": "300059", "名称": "身份测试样本", "主营业务": "受控业务"},),
        provenance=LiveMarketDataProvenance(
            response_sha256="fixture", retrieved_at=datetime(2026, 9, 13, tzinfo=UTC),
            schema_version="fixture",
        ),
    )

ROOT = Path(__file__).parents[4]


@pytest.mark.parametrize("unbound", [False, True])
def test_new_idea_schema_uses_new_signal_policy_without_migrating_saved_strategies(unbound):
    from ashare_lab.domain.strategy import HybridExecutionPolicy

    definitions = _idea_response_schema(require_strategy=True, unbound=unbound)["$defs"]
    for name in ("DailyExecutionPolicy", "HybridExecutionPolicy"):
        schema = definitions[name]
        policy = schema["properties"]["position_policy"]
        assert policy["default"] == "accumulate_on_new_entry_signal"
        assert "position_policy" in schema["required"]
        assert "single_position_no_pyramiding" in policy["enum"]
    for name in ("PricePlanExecutionPolicy", "ComposedExecutionPolicy"):
        assert definitions[name]["properties"]["position_policy"]["default"] == "bounded_inventory"
    # Durable defaults still decode previously saved plans with their old meaning.
    for policy_type in (DailyExecutionPolicy, HybridExecutionPolicy):
        assert policy_type.model_validate({}).position_policy == "single_position_no_pyramiding"
        assert policy_type.model_json_schema()["properties"]["position_policy"]["default"] == (
            "single_position_no_pyramiding"
        )


def test_requested_profit_loss_keeps_only_complete_structured_suggestions():
    from ashare_lab.domain.strategy import execution_for_price_plan
    from ashare_lab.domain.strategy.price_plans import ConditionalPlan, ConditionParameters, ConditionRule
    payload = _provider_payload()
    for proposal, kinds in zip(payload['proposals'],
                              [('take_profit', 'stop_loss'), ('stop_loss',), ('take_profit',)], strict=True):
        plan = ConditionalPlan(parameters=ConditionParameters(observation='minute_bar', rules=[
            ConditionRule(kind='rebound', side='buy', gap=2, gap_unit='percent'),
            *(ConditionRule(kind=kind, side='sell', direction='down' if kind == 'stop_loss' else 'up',
                            gap=3, gap_unit='percent', group='exit')
              for kind in kinds),
        ]))
        proposal['strategy'] = StrategySpec(
            catalog=CatalogRef(catalog_id='cn_a.signals', release_version='2026.09.01'),
            instrument=Instrument(symbol='300059.SZ'), trading_plan=plan,
            execution=execution_for_price_plan(plan),
            backtest=BacktestConfig(start=date(2025,9,1),end=date(2026,9,1),initial_cash_cny=1000000),
        ).model_dump(mode='json')
    route = _parse_provider_route(payload, utterance='帮我做一个赚了落袋、亏了及时退出的策略')
    assert len(route.proposals) == 1
    assert route.proposals[0].strategy.model_dump(mode='json') == payload['proposals'][0]['strategy']
    assert '1个' in route.understanding
    payload['proposals'] = payload['proposals'][1:]
    with pytest.raises(ValueError, match='每个候选'):
        _parse_provider_route(payload, utterance='止盈止损策略')
    # An explicit cancellation must not force protection back into a proposal.
    assert len(_parse_provider_route(payload, utterance='不要止盈止损').proposals) == 2


def test_generation_schema_keeps_minute_protection_without_legacy_daily_alternatives():
    import json
    schema = _idea_response_schema(require_strategy=True, unbound=True, minute_protection=True)
    serialized = json.dumps(schema)
    assert "#/$defs/PositionReturnExit" not in serialized
    assert "#/$defs/TrailingDrawdownExit" not in serialized
    assert "take_profit" in serialized and "pullback" in serialized
    assert "HoldingPeriodExit" in serialized and "IndicatorCondition" in serialized
    daily = _idea_response_schema(require_strategy=True, minute_protection=False)
    assert "PositionReturnExit" in daily["$defs"]


def test_protection_period_conflict_is_reported_before_extra_exit_wrapper():
    raw = {"proposals": [{"strategy_template": {"exit": {
        "type": "first_of", "op": "first_of", "children": [
            {"type": "trailing_drawdown_exit", "threshold_pct": 3},
        ],
    }}}]}
    with pytest.raises(ValueError, match="默认按分钟检查"):
        _parse_provider_route(raw, utterance="利润跑一会儿，掉头退出")
    # Explicit daily observation is not overridden by the early check.
    with pytest.raises(ValidationError):
        _parse_provider_route(raw, utterance="日线收盘回落3%退出")


@pytest.mark.parametrize("indicator,description", [
    ("price.return_pct", "持仓盈利8%止盈、持仓亏损5%止损"),
    ("technical.bias", "持仓后从最高点回落3%卖出"),
])
def test_protection_reference_mismatch_requests_semantic_repair(indicator, description):
    raw = {"proposals": [{"exit_summary": description, "strategy_template": {
        "exit": {"children": [{"type": "indicator_condition", "indicator_id": indicator}]},
    }}]}
    with pytest.raises(ValueError, match="不能用") as caught:
        _parse_provider_route(raw, utterance="赚了落袋，亏了退出")
    feedback = _safe_schema_feedback(caught.value, {})
    assert feedback[0]["loc"] == ("proposals",)
    assert "conditional" in feedback[0]["msg"]
    raw["proposals"][0]["exit_summary"] = "单日涨跌幅或均线乖离率满足条件卖出"
    with pytest.raises(ValidationError):
        _parse_provider_route(raw, utterance="按日线指标退出")


def test_idea_repair_includes_known_condition_requirement_without_private_input():
    from ashare_lab.domain.strategy.price_plans import ConditionRule
    with pytest.raises(ValidationError) as caught:
        ConditionRule.model_validate({"kind": "price", "side": "buy"})
    feedback = _safe_schema_feedback(caught.value, ConditionRule.model_json_schema())
    assert feedback[0]["msg"] == "Value error, 到价条件须填写触发价"
    with pytest.raises(ValidationError) as caught:
        ConditionRule.model_validate({"kind": "private-provider-value", "side": "buy"})
    assert "private-provider-value" not in str(_safe_schema_feedback(caught.value, ConditionRule.model_json_schema()))


@pytest.mark.parametrize("branch", ["strategy", "strategy_template"])
def test_conditional_ideas_reuse_canonical_mechanics_without_changing_values(branch):
    rule = {"kind": "rebound", "side": "buy", "direction": "up",
            "reference_mode": "first_observation", "gap": "2", "gap_unit": "percent",
            "quantity": 200}
    raw = {"proposals": [{branch: {"trading_plan": {"kind": "conditional",
        "parameters": {"observation": "minute_bar", "rules": [rule]},
    }}}]}
    normalized = _with_internal_grid_guardrails(raw)
    actual = normalized["proposals"][0][branch]["trading_plan"]["parameters"]["rules"][0]
    assert actual == {**rule, "reference_mode": "previous_fill"}
    assert rule["reference_mode"] == "first_observation"
    execution = normalized["proposals"][0][branch]["execution"]
    assert execution["execution_resolution"] == "1m"
    assert execution["entry_policy"] == execution["exit_policy"] == "next_bar_order_activation"
    assert execution["t_plus_one"] is True


@pytest.mark.parametrize("branch", ["strategy", "strategy_template"])
def test_minute_protection_idea_derives_hybrid_execution_with_new_signal_accumulation(branch):
    raw = {"proposals": [{branch: {
        "entry": {"type": "indicator_condition", "indicator_id": "technical.ma",
                  "trigger": "crosses_above", "value": "20"},
        "exit": {"children": [{"type": "minute_protection_exit", "stop_loss_pct": "5"}]},
        "execution": {"position_policy": "single_position_no_pyramiding"},
    }}]}

    normalized = _with_internal_grid_guardrails(raw)

    execution = normalized["proposals"][0][branch]["execution"]
    assert execution["execution_resolution"] == "1m"
    assert execution["evaluation_frequency"] == "daily_close_and_minute_bar"
    assert execution["position_policy"] == "accumulate_on_new_entry_signal"
    assert raw["proposals"][0][branch]["execution"] == {
        "position_policy": "single_position_no_pyramiding",
    }


@pytest.mark.parametrize("observation", [None, "daily_close"])
def test_cost_protection_plan_uses_minute_default_unless_user_requests_daily(observation):
    parameters = {"rules": [
        {"kind": "rebound", "side": "buy", "gap": "2", "quantity": 100},
        {"kind": "stop_loss", "side": "sell", "direction": "down", "gap": "5",
         "quantity": 100, "sizing_mode": "all_position"},
    ]}
    if observation is not None:
        parameters["observation"] = observation
    raw = {"proposals": [{"strategy": {
        "trading_plan": {"kind": "conditional", "parameters": parameters},
    }}]}

    normalized = _with_internal_grid_guardrails(raw, utterance="东方财富买一点，亏5%就走")

    strategy = normalized["proposals"][0]["strategy"]
    assert strategy["trading_plan"]["parameters"]["observation"] == "minute_bar"
    assert strategy["execution"]["evaluation_frequency"] == "1m_bar"

    daily = _with_internal_grid_guardrails(raw, utterance="按日线收盘亏5%就走")
    daily_parameters = daily["proposals"][0]["strategy"]["trading_plan"]["parameters"]
    assert daily_parameters.get("observation", "daily_close") == (observation or "daily_close")


@pytest.mark.parametrize("branch", ["strategy", "strategy_template"])
def test_scheduled_buy_with_independent_indicator_exit_derives_composed_execution(branch):
    raw = {"proposals": [{branch: {
        "trading_plan": {"kind": "scheduled", "parameters": {
            "frequency": "monthly", "day": 1, "at": "open",
        }},
        "exit": {"op": "first_of", "children": [{
            "type": "indicator_condition", "indicator_id": "technical.macd",
            "definition_version": "1.0.0", "params": {"fast": 12, "slow": 26, "signal": 9},
            "timeframe": "1d", "evaluation_mode": "bar_close_confirmed",
            "trigger": "death_cross",
        }]},
        "execution": {"position_policy": "bounded_inventory"},
    }}]}

    normalized = _with_internal_grid_guardrails(raw, utterance="每个月买一次，MACD死叉卖")

    execution = normalized["proposals"][0][branch]["execution"]
    assert execution["entry_policy"] == "composed_entry_leg"
    assert execution["exit_policy"] == "composed_exit_leg"
    assert execution["evaluation_frequency"] == "daily_close_and_minute_bar"
    assert execution["position_policy"] == "bounded_inventory"


def test_provider_route_does_not_overwrite_composed_template_execution():
    payload = _provider_payload()
    payload["proposals"] = [{
        "title": "定投与动量退出",
        "hypothesis": "每月分批买入，动量转弱时退出。",
        "entry_summary": "每月1日定投",
        "exit_summary": "MACD死叉卖出",
        "suggested_utterance": "每月1日定投买入，MACD死叉卖出，回测近1年",
        "strategy_template": {
            "catalog": {"catalog_id": "cn_a.signals", "release_version": "2026.09.01"},
            "trading_plan": {"kind": "scheduled", "parameters": {
                "frequency": "monthly", "day": 1, "at": "open",
            }},
            "exit": {"op": "first_of", "children": [{
                "type": "indicator_condition", "indicator_id": "technical.macd",
                "definition_version": "1.0.0", "params": {"fast": 12, "slow": 26, "signal": 9},
                "timeframe": "1d", "evaluation_mode": "bar_close_confirmed",
                "trigger": "death_cross",
            }]},
            # Provider execution metadata is untrusted and mechanically derived.
            "execution": {"position_policy": "bounded_inventory"},
            "backtest": {"start": "2025-09-15", "end": "2026-09-15",
                         "initial_cash_cny": 1_000_000},
        },
    }]

    route = _parse_provider_route(
        payload,
        utterance="东财每个月买一次，死叉卖",
    )

    execution = route.proposals[0].strategy_template.execution
    assert execution.entry_policy == "composed_entry_leg"
    assert execution.exit_policy == "composed_exit_leg"


def test_unbound_grid_omitted_absolute_bounds_gets_only_hidden_guardrails() -> None:
    raw = {"proposals": [{"strategy_template": {"trading_plan": {"kind": "grid",
        "parameters": {"anchor_mode": "latest_price", "range_percent": "10"},
    }}}]}
    normalized = cast(dict[str, object], _with_internal_grid_guardrails(raw))
    params = normalized["proposals"][0]["strategy_template"]["trading_plan"]["parameters"]
    assert params["lower_price"] == "0.01"
    assert params["upper_price"] == "1000000"
    assert params["range_percent"] == "10"
    assert "lower_price" not in raw["proposals"][0]["strategy_template"]["trading_plan"]["parameters"]


def test_idea_normalization_never_turns_promised_buy_into_sell():
    raw = {"proposals": [{"title": "回落买入", "strategy_template": {"trading_plan": {
        "kind": "conditional", "parameters": {"rules": [
            {"kind": "pullback", "side": "buy", "gap": "3"},
        ]},
    }}}]}
    normalized = _with_internal_grid_guardrails(raw)
    assert normalized["proposals"][0]["strategy_template"]["trading_plan"]["parameters"]["rules"][0]["side"] == "buy"
    # Preserve the conflict for a specific model repair; do not execute it.
    from ashare_lab.domain.strategy.price_plans import ConditionalPlan
    with pytest.raises(ValidationError, match="回落条件用于卖出"):
        ConditionalPlan.model_validate(normalized["proposals"][0]["strategy_template"]["trading_plan"])


@pytest.mark.asyncio
@pytest.mark.parametrize("declined", [True, False])
async def test_inspiration_selector_does_not_invent_stock_when_declined_or_unverified(
    capability_matrix, declined: bool,
) -> None:
    transport = _RecordingTransport([{
        "declined": declined, "framing": "把观察作为研究假设",
        "screen_query": None if declined else "A股蛋鸡养殖主营业务相关公司",
    }])
    provider = Mock(screen=AsyncMock(return_value=object()))
    advisor = Mock(recommend_stocks=AsyncMock(return_value=()))
    selector = InspirationStockSelector(
        transport=transport, matrix=capability_matrix, provider=provider, advisor=advisor,
        sleeper=AsyncMock(),
    )
    request = CompileInput(utterance="鸡蛋涨价", as_of_date=date(2026, 8, 30))
    if declined:
        assert await selector.select(request, None) is None
        provider.screen.assert_not_awaited()
        advisor.recommend_stocks.assert_not_awaited()
    else:
        with pytest.raises(IdeaStockSelectionUnavailableError):
            await selector.select(request, None)
        provider.screen.assert_awaited_with(
            query="A股蛋鸡养殖主营业务相关公司；仅沪深当前上市A股，排除已退市股票。", asset_type="A股",
        )
        assert provider.screen.await_count == 3
        assert advisor.recommend_stocks.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", [
    "我想吃荔枝，我是杨玉环，给我对应的交易策略",
    "我想吃樱桃，假如我是小红帽，给我对应的交易策略",
])
async def test_perishable_food_inspiration_uses_bounded_evidenced_cold_chain_screen(
    capability_matrix, utterance: str,
) -> None:
    from dataclasses import replace

    evidence = replace(
        _selection_evidence(),
        rows=(
            {"代码": "002639", "名称": "雪人集团", "主营业务": "受控冷链装备测试字段"},
            {"代码": "603187", "名称": "海容冷链", "主营业务": "受控商用冷链测试字段"},
            {"代码": "603339", "名称": "四方科技", "主营业务": "受控食品速冻测试字段"},
        ),
    )
    stocks = tuple(
        SimpleNamespace(symbol=symbol, name=name, reason="只据筛选返回的受控业务字段")
        for symbol, name in (
            ("002639.SZ", "雪人集团"),
            ("603187.SH", "海容冷链"),
            ("603339.SH", "四方科技"),
        )
    )
    transport = _RecordingTransport([])
    provider = Mock(screen=AsyncMock(return_value=evidence))
    selector = InspirationStockSelector(
        transport=transport,
        matrix=capability_matrix,
        provider=provider,
        advisor=Mock(recommend_stocks=AsyncMock(return_value=stocks)),
    )

    selected = await selector.select(
        CompileInput(
            utterance=utterance,
            idea_inspiration=utterance,
            semantic_intent="viewpoint",
            as_of_date=date(2026, 9, 15),
        ),
        None,
    )

    assert selected is not None
    assert [selected.symbol, *(item.symbol for item in selected.alternatives)] == [
        "002639.SZ", "603187.SH", "603339.SH",
    ]
    assert transport.requests == []
    query = provider.screen.await_args.kwargs["query"]
    assert "冷链物流设备" in query and "食品速冻设备" in query
    assert not any(term in query for term in ("杨玉环", "小红帽", "荔枝", "樱桃"))
    assert "仅沪深当前上市A股，排除已退市股票" in query
    assert "不代表股价因果" in selected.framing


@pytest.mark.asyncio
async def test_self_contained_lychee_inspiration_skips_current_affairs_research(
    capability_matrix,
) -> None:
    researcher = _StaticResearcher()
    selector = Mock(select=AsyncMock(return_value=IdeaStockSelection(
        "002639.SZ", "受控候选", "来自受控选股证据", "只作创作联想", _selection_evidence(),
    )))
    route = await VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        researcher=researcher,
        stock_selector=selector,
    ).route(CompileInput(
        utterance="我想吃荔枝，我是杨贵妃，给我对应的交易策略",
        idea_inspiration="杨贵妃与荔枝的创作型研究灵感",
        semantic_intent="viewpoint",
        as_of_date=date(2026, 9, 15),
    ))

    assert route is not None
    assert researcher.requests == []
    assert selector.select.await_args.args[1] is None
    assert route.proposals and all(item.instrument_symbol == "002639.SZ" for item in route.proposals)


def test_perishable_food_shortcut_never_overrides_explicit_producer_scope() -> None:
    request = CompileInput(
        utterance="我想吃荔枝，帮我选荔枝种植公司",
        semantic_intent="viewpoint",
        as_of_date=date(2026, 9, 15),
    )
    assert not is_self_contained_perishable_food_inspiration(request)


@pytest.mark.asyncio
async def test_perishable_food_selection_failure_returns_unbound_choices_without_stock_facts(
    capability_matrix,
) -> None:
    selector = Mock(select=AsyncMock(side_effect=IdeaStockSelectionUnavailableError(
        "private provider detail", reason="data_no_results", attempts=3,
    )))
    transport = _RecordingTransport([_provider_payload()])
    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        stock_selector=selector,
    ).route(CompileInput(
        utterance="我想吃荔枝，假如我是古代人，给我对应的交易策略",
        idea_inspiration="从荔枝的保鲜运输得到的创作灵感",
        semantic_intent="viewpoint",
        as_of_date=date(2026, 9, 15),
    ))

    assert route is not None and route.proposals
    assert route.asset_mapping.instrument_symbol is None
    assert all(item.instrument_symbol is None for item in route.proposals)
    payload = transport.requests[0].user_payload
    assert payload is not None
    assert "不得输出具体股票或代码" in "".join(payload["recentIdeaTurns"])


def test_idea_normalization_consumes_safe_provider_redundancy_without_model_repair() -> None:
    raw = {
        "proposals": [{
            "suggested_utterance": "以前收盘价为基准做等比网格，每格3%，首日建仓，回测近1年",
            "strategy_template": {
                "exit": {
                    "type": "first_of",
                    "op": "first_of",
                    "children": [{"type": "holding_period_exit", "sessions": 20}],
                },
                "trading_plan": {
                    "kind": "grid",
                    "parameters": {
                        "anchor_mode": "previous_close",
                        "spacing_mode": "percent",
                        "spacing": 3,
                        "initial_cash_cny": 100_000,
                    },
                },
            },
        }],
    }

    normalized = cast(dict[str, object], _with_internal_grid_guardrails(raw))
    proposal = normalized["proposals"][0]
    branch = proposal["strategy_template"]
    assert "type" not in branch["exit"] and branch["exit"]["op"] == "first_of"
    assert "每下跌3%买入" in proposal["suggested_utterance"]
    assert "每上涨3%卖出" in proposal["suggested_utterance"]
    assert raw["proposals"][0]["strategy_template"]["exit"]["type"] == "first_of"


@pytest.mark.asyncio
async def test_inspiration_failed_recommendation_is_not_a_provider_retry(capability_matrix):
    provider = Mock(screen=AsyncMock(return_value=object()))
    selector = InspirationStockSelector(
        transport=_RecordingTransport([{
            "declined": False, "framing": "创作联想", "screen_query": "沪深A股文旅业务",
        }]), matrix=capability_matrix, provider=provider,
        advisor=Mock(recommend_stocks=AsyncMock(return_value=None)), sleeper=AsyncMock(),
    )
    with pytest.raises(IdeaStockSelectionUnavailableError) as caught:
        await selector.select(CompileInput(utterance="我是秦始皇", as_of_date=date(2026, 9, 13)), None)
    assert caught.value.reason == "recommendation_unavailable"
    provider.screen.assert_awaited_once()


@pytest.mark.asyncio
async def test_inspiration_no_data_is_a_domain_failure_not_an_http_500(capability_matrix) -> None:
    from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderNoDataError
    transport = _RecordingTransport([{
        "declined": False, "framing": "仅作为研究联想", "screen_query": "A股相关主营业务",
    }])
    selector = InspirationStockSelector(
        transport=transport, matrix=capability_matrix,
        provider=Mock(screen=AsyncMock(side_effect=MxSaasProviderNoDataError("no data"))),
        advisor=Mock(recommend_stocks=AsyncMock()),
        sleeper=AsyncMock(),
    )
    with pytest.raises(IdeaStockSelectionUnavailableError):
        await selector.select(CompileInput(utterance="一个灵感", as_of_date=date(2026, 9, 13)), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_plan", [
    "not json",
    {"declined": False, "framing": "用户观察，仅作为待验证的研究假设", "screen_query": None},
])
async def test_inspiration_plan_repairs_once_before_screening(capability_matrix, bad_plan) -> None:
    query = "A股宠物食品主营业务相关公司，返回代码、简称和主营业务依据"
    transport = _RecordingTransport([bad_plan, {
        "declined": False, "framing": "把养宠物的观察转成宠物消费研究假设",
        "screen_query": query,
    }])
    provider = Mock(screen=AsyncMock(return_value=_selection_evidence()))
    advisor = Mock(recommend_stocks=AsyncMock(return_value=(SimpleNamespace(
        symbol="300059.SZ", name="身份测试样本", reason="受控测试数据，不是真实业务归属",
    ),)))
    selector = InspirationStockSelector(
        transport=transport, matrix=capability_matrix, provider=provider, advisor=advisor,
    )
    selected = await selector.select(CompileInput(
        utterance="最近大家都在养宠物", as_of_date=date(2026, 9, 8),
    ), None)
    assert selected is not None
    assert selected.evidence is provider.screen.return_value
    assert len(transport.requests) == 2
    repair = transport.requests[1].user_payload
    assert repair is not None and repair["utterance"] == "最近大家都在养宠物"
    assert repair["formatIssues"]
    assert provider.screen.await_args_list[0].kwargs == {
        "query": query + "；仅沪深当前上市A股，排除已退市股票。", "asset_type": "A股",
    }
    assert provider.screen.await_count == 2  # One evidenced stock triggers supplementation.


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_inspiration_repairs_query_and_retries_without_multiplying_http_budget(
    capability_matrix, recover,
):
    import httpx
    import json
    from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient
    from ashare_lab.ports.dialogue_progress import progress_sink
    queries, events = [], []
    original, repaired = "A股历史文化相关业务", "沪深A股，文旅或历史题材影视主营业务，返回代码、简称及业务依据"
    transport = _RecordingTransport([
        {"declined": False, "framing": "仅作创作联想", "screen_query": original},
        {"declined": False, "framing": "仅作创作联想", "screen_query": repaired},
    ])

    def handler(request):
        queries.append(json.loads(request.content)["query"])
        if len(queries) < 3 or not recover:
            return httpx.Response(200, json={"code": 500, "message": "SQL execution failed: private"})
        return httpx.Response(200, json={"data": {"allResults": {"result": {
            "columns": [{"field": "code", "displayName": "证券代码"}], "dataList": [{"code": "300059"}],
        }}}})

    advisor = Mock(recommend_stocks=AsyncMock(return_value=(SimpleNamespace(
        symbol="300059.SZ", name="受控样本", reason="仅测试依据，不是真实业务关联",
    ),)))
    selector = InspirationStockSelector(
        transport=transport, matrix=capability_matrix, advisor=advisor, sleeper=AsyncMock(),
        provider=MxSaasMarketDataClient(api_key="test", transport=httpx.MockTransport(handler), sleeper=AsyncMock()),
    )
    token = progress_sink.set(lambda stage, message: events.append((stage, message)))
    try:
        request = CompileInput(utterance="我是秦始皇", as_of_date=date(2026, 9, 13))
        if recover:
            result = await selector.select(request, None)
            assert result is not None
            advisor.recommend_stocks.assert_awaited_once()
        else:
            with pytest.raises(IdeaStockSelectionUnavailableError) as caught:
                await selector.select(request, None)
            assert caught.value.reason == "provider_sql_error" and caught.value.attempts == 3
            advisor.recommend_stocks.assert_not_awaited()
    finally:
        progress_sink.reset(token)
    assert len(queries) == 3 and len(transport.requests) == 2
    assert queries[0].startswith(original) and all(q.startswith(repaired) for q in queries[1:])
    assert all("排除已退市股票" in q and "ST" not in q for q in queries)
    assert transport.requests[1].user_payload["utterance"] == "我是秦始皇"
    assert "不得改变用户明确" in transport.requests[1].user_payload["repairInstruction"]
    assert any("2/2" in message for _, message in events)
    assert "private" not in str(events)


@pytest.mark.asyncio
async def test_inspiration_keeps_st_candidates_and_excludes_other_markets(capability_matrix):
    transport = _RecordingTransport([{
        "declined": False, "framing": "研究联想", "screen_query": "内容审核相关A股",
    }])
    candidates = tuple(SimpleNamespace(symbol=symbol, name=name, reason="受控依据")
                       for symbol, name in (("002122.SZ", "ST汇洲"),
                                            ("600001.SH", "*ST样本"),
                                            ("830001.BJ", "北交样本"),
                                            ("300229.SZ", "拓尔思")))
    selector = InspirationStockSelector(
        transport=transport, matrix=capability_matrix,
        provider=Mock(screen=AsyncMock(return_value=_selection_evidence())),
        advisor=Mock(recommend_stocks=AsyncMock(return_value=candidates)),
    )
    selected = await selector.select(CompileInput(
        utterance="内容审核", as_of_date=date(2026, 9, 13)), None)
    assert selected.symbol == "002122.SZ"
    assert [item.symbol for item in selected.alternatives] == ["600001.SH", "300229.SZ"]


@pytest.mark.asyncio
async def test_inspiration_projects_only_selected_evidence_without_mutating_source(capability_matrix):
    from dataclasses import replace
    evidence = _selection_evidence()
    source = replace(evidence, rows=evidence.rows + (
        {"代码": "600000", "名称": "未选样本", "主营业务": "不相关长资料" * 500},
    ))
    advisor = Mock(recommend_stocks=AsyncMock(return_value=(SimpleNamespace(
        symbol="300059.SZ", name="身份测试样本", reason="受控业务",
    ),)))
    selector = InspirationStockSelector(
        transport=_RecordingTransport([{
            "declined": False, "framing": "研究方向", "screen_query": "相关主营业务",
        }]), matrix=capability_matrix, provider=Mock(screen=AsyncMock(return_value=source)),
        advisor=advisor,
    )
    selected = await selector.select(CompileInput(utterance="研究灵感", as_of_date=date(2026, 9, 13)), None)
    assert selected.evidence.rows == evidence.rows
    assert selected.evidence.provenance == source.provenance
    assert selected.evidence.provider_metadata["sourceRowCount"] == 2
    assert len(source.rows) == 2
    assert advisor.recommend_stocks.await_args.args[1] is source


@pytest.mark.asyncio
async def test_inspiration_plan_repair_is_bounded_and_does_not_query_data(
    capability_matrix,
) -> None:
    transport = _RecordingTransport(["not json"])
    provider = Mock(screen=AsyncMock())
    advisor = Mock(recommend_stocks=AsyncMock())
    selector = InspirationStockSelector(
        transport=transport, matrix=capability_matrix, provider=provider, advisor=advisor,
    )
    with pytest.raises(IdeaStockSelectionUnavailableError) as caught:
        await selector.select(CompileInput(
            utterance="我是狮子座", as_of_date=date(2026, 9, 8),
        ), None)
    assert caught.value.stage == "planning"
    assert len(transport.requests) == 2
    provider.screen.assert_not_awaited()
    advisor.recommend_stocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspiration_stock_is_selected_before_rules_without_claiming_user_choice(
    capability_matrix, monkeypatch,
) -> None:
    order: list[str] = []
    evidence = LiveMarketDataResult(
        provider="test", query="测试业务范围，不是归属证据", asset_type="A股",
        columns=("代码", "主营业务"),
        rows=({"代码": "300059.SZ", "主营业务": "受控业务字段"},),
        provenance=LiveMarketDataProvenance(
            response_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 8, 30, tzinfo=UTC), schema_version="test.v1",
        ),
    )
    review = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "ashare_lab.adapters.language.vibe_ideas.review_display_semantics", review,
    )

    class Selector:
        async def select(self, request, research):
            order.append("select")
            assert request.utterance == "鸡蛋涨价"
            assert research is not None
            return IdeaStockSelection(
                "300059.SZ", "身份校验测试样本", "测试数据依据", "待验证假设", evidence,
            )

    class Transport(_RecordingTransport):
        async def generate_json(self, request):
            order.append("rules")
            assert request.instrument_context == "300059.SZ"
            return await super().generate_json(request)

    router = VibeIdeaRouter(
        Transport([_provider_payload()]), capability_matrix=capability_matrix,
        researcher=_StaticResearcher(order), stock_selector=Selector(),
        model_semantic_review=True,
    )
    route = await router.route(CompileInput(
        utterance="鸡蛋涨价", semantic_intent="viewpoint", as_of_date=date(2026, 8, 30),
    ))
    assert route is not None
    assert order == ["research", "select", "rules"]
    checked = review.call_args.kwargs["verified_context"]["stockSelectionEvidence"]
    assert checked["rows"] == list(evidence.rows)
    assert checked["query"] == evidence.query
    assert checked["responseSha256"] == evidence.provenance.response_sha256
    assert "不是独立事实证据" in review.call_args.kwargs["response_scope"]
    assert route.asset_mapping.instrument_symbol is None
    assert route.understanding == _provider_payload()["understanding"]
    assert all(item.instrument_symbol == "300059.SZ" for item in route.proposals)
    assert all(item.instrument_name == "身份校验测试样本" for item in route.proposals)
    assert all("尚待用户选择" in item.assumptions[1] for item in route.proposals)
    assert all("模型没有选择或替换股票" not in "".join(item.assumptions)
               for item in route.proposals)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_rules", [False, True])
@pytest.mark.parametrize("binding_failure", [False, True])
async def test_inspiration_binds_each_template_to_its_verified_stock(
    capability_matrix, duplicate_rules, binding_failure, monkeypatch,
):
    from ashare_lab.ports.idea_routing import UnboundIdeaStrategy
    original_bind = UnboundIdeaStrategy.bind
    failures = []
    def bind(self, symbol):
        if binding_failure and not failures:
            failures.append(symbol)
            raise ValidationError.from_exception_data("StrategySpec", [{
                "type": "value_error", "loc": (), "input": {},
                "ctx": {"error": ValueError("分钟保护须声明日线信号与分钟执行，不能回退日线")},
            }])
        return original_bind(self, symbol)
    monkeypatch.setattr(UnboundIdeaStrategy, "bind", bind)
    catalog = CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01")
    payload = _provider_payload()
    for period, proposal in zip((20, 30, 60), payload["proposals"], strict=True):
        strategy = StrategySpec(
            catalog=catalog, instrument=Instrument(symbol="300059.SZ"),
            execution=DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
            entry=IndicatorCondition(indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"}, trigger="price_crosses_above"),
            exit=FirstOfExit(children=(IndicatorCondition(indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"}, trigger="price_crosses_below"),)),
            backtest=BacktestConfig(start=date(2025, 9, 4), end=date(2026, 9, 4), initial_cash_cny=1_000_000),
        ).model_dump(mode="json")
        strategy.pop("instrument")
        strategy.pop("schema_version")
        proposal["strategy_template"] = strategy
        proposal["pairing_explanation"] = f"样本的业务是线索，先看价格能否站上{period}日均线，再考虑买入。"
    if duplicate_rules:
        payload["proposals"][2]["strategy_template"] = deepcopy(payload["proposals"][0]["strategy_template"])
    selected = IdeaStockSelection("300059.SZ", "样本甲", "依据甲", "联想", alternatives=(
        IdeaStockSelection("000001.SZ", "样本乙", "依据乙", "联想"),
        IdeaStockSelection("600519.SH", "样本丙", "依据丙", "联想"),
    ))
    transport = _RecordingTransport([payload, payload])
    router = VibeIdeaRouter(transport, repair_transport=transport, capability_matrix=capability_matrix,
        researcher=_StaticResearcher(), strategy_catalog=catalog,
        stock_selector=Mock(select=AsyncMock(return_value=selected)))
    request = CompileInput(utterance="我讨厌特朗普", semantic_intent="viewpoint",
                           as_of_date=date(2026, 9, 4))
    if duplicate_rules:
        from ashare_lab.ports.idea_routing import IdeaGenerationError
        with pytest.raises(IdeaGenerationError):
            await router.route(request)
        return
    route = await router.route(request)
    if binding_failure:
        assert len(transport.requests) == 2
        assert "分钟保护须声明" in transport.requests[1].user_payload["validationFeedback"][0]["msg"]
    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert [p.instrument_symbol for p in route.proposals] == ["300059.SZ", "000001.SZ", "600519.SH"]
    assert [p.instrument_name for p in route.proposals] == ["样本甲", "样本乙", "样本丙"]
    assert "20日均线" in route.proposals[0].pairing_reason
    assert [p.strategy.entry.params["period"] for p in route.proposals] == [20, 30, 60]
    assert all(p.strategy_template is None and p.strategy_hash for p in route.proposals)
    assert transport.requests[0].instrument_context is None
    assert len(transport.requests[0].user_payload["verifiedStockSelections"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("structure", ["strategy", "strategy_template", None])
@pytest.mark.parametrize("accepted", [True, False])
async def test_repair_generated_sentence_only_with_immutable_structured_rules(capability_matrix, structure, accepted):
    from dataclasses import replace
    from ashare_lab.ports.idea_routing import UnboundIdeaStrategy
    from tests.unit.application.test_daily_backtest import strategy

    source = _RecordingTransport([_provider_payload()])
    route = await VibeIdeaRouter(source, capability_matrix=capability_matrix).route(CompileInput(
        utterance="给几个简单策略", semantic_intent="new_strategy", instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 30)))
    assert route is not None
    cross = IndicatorCondition(indicator_id="technical.ma_cross", definition_version="1.0.0",
        params={"fast_period": 5, "slow_period": 20, "price_field": "close"}, trigger="golden_cross")
    bound = strategy().model_copy(update={"entry": cross,
        "exit": FirstOfExit(children=(cross.model_copy(update={"trigger": "death_cross"}),))})
    template = UnboundIdeaStrategy(catalog=bound.catalog, entry=bound.entry, exit=bound.exit,
                                  execution=bound.execution, backtest=bound.backtest)
    if structure:
        route = replace(route, proposals=(replace(route.proposals[0], **{
            structure: bound if structure == "strategy" else template,
        }), *route.proposals[1:]))
    first = route.proposals[0]
    sentence = "5日均线上穿20日均线买入，5日均线下穿20日均线卖出，回测近1年"
    transport = _RecordingTransport([{
        "understanding": route.understanding, "hypothesis": route.hypothesis,
        "pairing_reasons": {item.id: item.pairing_reason for item in route.proposals},
        "proposal_explanations": {item.id: {
            "hypothesis": item.hypothesis, "entry_summary": item.entry_summary,
            "exit_summary": item.exit_summary,
            **({"suggested_utterance": sentence} if item.id == first.id else {}),
        } for item in route.proposals},
    }, {"facts": "supported" if accepted else "unsupported",
        "state_and_authority": "supported", "user_intent_and_tone": "supported"}])
    result = await _repair_idea_explanation(transport, source.requests[0], route, {
        "display_payload": {"proposals": [{"title": item.title, "suggested_utterance": item.suggested_utterance}
                                           for item in route.proposals]},
        "verified_context": {"user": "给几个简单策略"}, "response_scope": "核对交叉两条序列与周期",
    })
    assert transport.requests[0].user_payload["verified_context"]["user"] == "给几个简单策略"
    if structure is None:
        assert result is None and len(transport.requests) == 1
        return
    reviewed = transport.requests[1].user_payload["reply"]["proposals"][0]
    assert reviewed["suggested_utterance"] == sentence
    if not accepted:
        assert result is None
        return
    assert result is not None
    assert result.proposals[0].suggested_utterance == sentence
    for before, after in zip(route.proposals, result.proposals, strict=True):
        assert before.id == after.id
        assert before.instrument_symbol == after.instrument_symbol
        assert before.strategy == after.strategy
        assert before.strategy_template == after.strategy_template


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("repair_titles", [True, False])
@pytest.mark.parametrize("repair_explanations", [True, False])
async def test_explanation_repair_preserves_rules_and_requires_second_review(
    capability_matrix, accepted: bool, repair_titles: bool, repair_explanations: bool,
) -> None:
    original_transport = _RecordingTransport([_provider_payload()])
    route = await VibeIdeaRouter(
        original_transport, capability_matrix=capability_matrix,
    ).route(CompileInput(utterance="均线交易方向", semantic_intent="new_strategy",
                        instrument_context="300059.SZ", as_of_date=date(2026, 8, 30)))
    assert route is not None
    repaired_text = "把这个表达当作创作起点，以下研究方案的参数可以修改。"
    transport = _RecordingTransport([{
        "understanding": repaired_text, "hypothesis": "未经回测的研究方案",
        "pairing_reasons": {item.id: "已提供的股票研究样本" for item in route.proposals},
        "proposal_titles": {item.id: "均线交叉" for item in route.proposals} if repair_titles else {},
        "proposal_explanations": {item.id: {
            "hypothesis": "检验均线交易假设", "entry_summary": "按所列入场规则买入",
            "exit_summary": "按所列退出规则卖出",
        } for item in route.proposals} if repair_explanations else {},
    }, {"facts": "supported" if accepted else "unsupported",
        "state_and_authority": "supported", "user_intent_and_tone": "supported"}])
    result = await _repair_idea_explanation(
        transport, original_transport.requests[0], route,
        {"display_payload": {"understanding": route.understanding,
                             "proposals": [{"title": item.title} for item in route.proposals]},
         "verified_context": {"user": "我是狮子座"}, "response_scope": "禁止推断用户性格"},
    )
    assert len(transport.requests) == 2
    assert transport.requests[1].user_payload["reply"]["understanding"] == repaired_text
    if repair_explanations:
        assert all(item["exit_summary"] == "按所列退出规则卖出"
                   for item in transport.requests[1].user_payload["reply"]["proposals"])
    if repair_titles:
        assert all(item["title"] == "均线交叉"
                   for item in transport.requests[1].user_payload["reply"]["proposals"])
    if not accepted:
        assert result is None
        return
    assert result is not None and result.understanding == repaired_text
    assert result.asset_mapping == route.asset_mapping
    for before, after in zip(route.proposals, result.proposals, strict=True):
        assert before.id == after.id
        assert after.title == ("均线交叉" if repair_titles else before.title)
        assert before.strategy == after.strategy
        assert before.instrument_symbol == after.instrument_symbol
        assert before.suggested_utterance == after.suggested_utterance
        assert after.entry_summary == ("按所列入场规则买入" if repair_explanations else before.entry_summary)
        assert after.exit_summary == ("按所列退出规则卖出" if repair_explanations else before.exit_summary)
        assert before.strategy_template == after.strategy_template


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_router_repairs_rejected_explanation_without_regenerating_rules(
    capability_matrix, accepted,
):
    class ReviewTransport:
        def __init__(self):
            self.requests = []

        async def generate_json(self, request):
            self.requests.append(request)
            if request.response_schema_name == "idea_explanation_repair":
                return {
                    "understanding": "把这个表达作为创作起点，以下是待验证的研究方案。",
                    "hypothesis": "以趋势信号研究交易时机，不预示收益。",
                    "pairing_reasons": {
                        key: None for key in request.user_payload["pairing_reasons"]
                    },
                }
            verdict = "unsupported" if len(self.requests) == 1 or not accepted else "supported"
            return {"facts": verdict,
                    "state_and_authority": "supported", "user_intent_and_tone": "supported"}

    payload = _provider_payload()
    for period, proposal in zip(
        (10, 20, 30), cast(list[dict[str, object]], payload["proposals"]), strict=True,
    ):
        proposal["strategy"] = StrategySpec(
            catalog=CatalogRef(
                catalog_id="cn_a.signals", release_version="2026.09.01",
            ),
            instrument=Instrument(symbol="300059.SZ"),
            entry=IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"},
                trigger="price_crosses_above",
            ),
            exit=FirstOfExit(children=(IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"},
                trigger="price_crosses_below",
            ),)),
            backtest=BacktestConfig(
                start=date(2025, 8, 30), end=date(2026, 8, 30),
                initial_cash_cny=1_000_000,
            ),
        ).model_dump(mode="json")
    generation = _RecordingTransport([payload])
    review = ReviewTransport()
    router = VibeIdeaRouter(
        generation, capability_matrix=capability_matrix,
        model_semantic_review=True, review_transport=review,
        repair_transport=generation,
    )
    request = CompileInput(utterance="均线交易方向", semantic_intent="new_strategy",
                           instrument_context="300059.SZ", as_of_date=date(2026, 8, 30))
    if not accepted:
        route = await router.route(request)
        assert route is not None
        assert route.understanding.startswith("给你 3 个交易方案作比较")
        assert all(item.title.startswith("可修改策略方向 ") for item in route.proposals)
        assert all(item.strategy is not None for item in route.proposals)
        assert all(item.pairing_reason is None for item in route.proposals)
        assert len(generation.requests) == 1
        assert len(review.requests) == 3
        return
    route = await router.route(request)
    assert route is not None
    assert len(generation.requests) == 1
    assert [item.response_schema_name for item in review.requests] == [
        "dialogue_reply_semantic_review", "idea_explanation_repair",
        "dialogue_reply_semantic_review",
    ]
    assert route.understanding == "把这个表达作为创作起点，以下是待验证的研究方案。"
    assert (review.requests[0].user_payload["reply"]["proposals"]
            == review.requests[-1].user_payload["reply"]["proposals"])


class _RecordingTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return cast(CandidateTransportResponse, response)


class _StaticResearcher:
    def __init__(
        self,
        order: list[str] | None = None,
        *,
        include_sources: bool = True,
        summary: str = "检索结果提到同花顺。",
    ) -> None:
        self.requests: list[CurrentFactResearchRequest] = []
        self.order = order
        self.include_sources = include_sources
        self.summary = summary

    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult:
        if self.order is not None:
            self.order.append("research")
        self.requests.append(request)
        observed_at = datetime(2026, 8, 30, tzinfo=UTC)
        return CurrentFactResearchResult(
            provider="test-research",
            model="test-model",
            provider_response_id="research-1",
            query=request.query,
            purpose=ResearchPurpose.VIEWPOINT,
            as_of=request.as_of,
            summary=self.summary,
            facts=(
                ResearchFact(
                    statement="同花顺",
                    fact_kind="related_instrument",
                    source_ids=("source-1",) if self.include_sources else (),
                    time_scope=None,
                ),
            ),
            sources=(
                (
                    ResearchSource(
                        source_id="source-1",
                        title="测试公开来源",
                        url="https://example.com/source-1",
                        publisher="test-publisher",
                        published_at="2026-08-30",
                    ),
                )
                if self.include_sources
                else ()
            ),
            unresolved_questions=(),
            retrieved_at=observed_at,
            response_sha256="sha256:" + "1" * 64,
            search_call_count=1,
        )


class _OrderedTransport(_RecordingTransport):
    def __init__(self, responses: list[object], order: list[str]) -> None:
        super().__init__(responses)
        self.order = order

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.order.append("model")
        return await super().generate_json(request)


@pytest.fixture
def capability_matrix() -> CandidateCapabilityMatrix:
    return build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


@pytest.mark.asyncio
async def test_composed_idea_generation_uses_explicit_repair_profile(capability_matrix):
    class IdentifiedTransport(_RecordingTransport):
        def __init__(self, model, responses):
            super().__init__(responses)
            self.identity = CandidateProviderIdentityView(
                provider="test", model=model, prompt_version="test", schema_version="test",
            )

    fast = IdentifiedTransport("fast", ["invalid json"])
    deep = IdentifiedTransport("deep", [_provider_payload()])
    compiler = build_hybrid_candidate_compiler(
        load_catalog_directory(ROOT / "catalogs"), candidate_transport=fast,
        idea_transport=fast, idea_repair_transport=deep,
        capability_matrix=capability_matrix, candidate_repair_invalid_output=True,
    )
    route = await compiler._idea_router.route(CompileInput(
        utterance="均线交易方向", instrument_context="300059.SZ", as_of_date=date(2026, 8, 30),
    ))
    assert route is not None
    assert len(fast.requests) == len(deep.requests) == 1
    assert route.provenance.model == "deep"
    assert deep.requests[0].user_payload["previousResponse"] == "invalid json"


def _provider_payload() -> dict[str, object]:
    return {
        "understanding": "用户对特朗普表达了负面态度。",
        "hypothesis": "相关不确定性可能与当前股票的价格行为同期出现。",
        "proposals": [
            {
                "title": "趋势确认",
                "hypothesis": "用均线突破检验趋势延续，而不是假定观点必然影响股价。",
                "entry_summary": "股价上穿 20 日均线",
                "exit_summary": "股价跌破 20 日均线",
                "suggested_utterance": "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
            },
            {
                "title": "超跌反转",
                "hypothesis": "用 RSI 区间检验价格是否存在均值回归。",
                "entry_summary": "RSI 低于 30",
                "exit_summary": "RSI 高于 70",
                "suggested_utterance": "RSI低于30买入，RSI高于70卖出，回测近1年",
            },
            {
                "title": "动量转强",
                "hypothesis": "用 MACD 交叉检验动量变化。",
                "entry_summary": "MACD 金叉",
                "exit_summary": "MACD 死叉",
                "suggested_utterance": "MACD金叉买入，MACD死叉卖出，回测近1年",
            },
        ],
    }


@pytest.mark.asyncio
async def test_suggested_parameters_and_unavailable_valuation_remain_visible_as_suggestions(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    payload = _provider_payload()
    payload["understanding"] = "低估值保留为选股偏好，先给出可编辑的日线反转建议。"
    proposals = cast(list[dict[str, object]], payload["proposals"])
    for proposal in proposals:
        proposal["title"] = "模型建议" + str(proposal["title"])
        proposal["hypothesis"] = "周期和阈值为模型建议，当前只回测日线条件，没有历史估值过滤。"
        proposal["entry_summary"] = (
            str(proposal["entry_summary"]) + "（模型建议，未纳入历史估值过滤）"
        )
    transport = _RecordingTransport([payload])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)
    route = await router.route(CompileInput(
        utterance="估值过低的股票反转买", as_of_date=date(2026, 9, 4),
        idea_inspiration="估值过低的股票反转买",
    ))
    assert route is not None
    assert route.understanding == payload["understanding"]
    assert all("模型建议" in proposal.title for proposal in route.proposals)
    assert all("没有历史估值过滤" in proposal.hypothesis for proposal in route.proposals)
    assert all("未纳入历史估值过滤" in proposal.entry_summary for proposal in route.proposals)
    assert all(any("不是用户给定条件" in item for item in proposal.assumptions)
               for proposal in route.proposals)
    assert len(transport.requests) == 1
    contract = transport.requests[0].system_contract
    assert "不再追问用户或要求重写" in contract
    assert "不能编造历史PE、PB" in contract
    assert "不得静默移除" in contract


@pytest.mark.asyncio
async def test_provider_authors_complete_strategies_but_server_owns_asset_and_capabilities(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-chat",
            prompt_version="candidate.prompt.v1",
            schema_version="candidate.schema.v1",
        ),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert len(transport.requests) == 1
    assert route.asset_mapping.instrument_symbol == "300059.SZ"
    assert route.asset_mapping.relation == "current_page_proxy"
    assert route.asset_mapping.evidence_status == "host_context_only"
    assert len(route.proposals) == 3
    assert {item.suggested_utterance for item in route.proposals} == {
        "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
        "RSI低于30买入，RSI高于70卖出，回测近1年",
        "MACD金叉买入，MACD死叉卖出，回测近1年",
    }
    assert all("300059.SZ" not in item.suggested_utterance for item in route.proposals)
    assert all("绕过系统" not in item.suggested_utterance for item in route.proposals)
    assert all(item.instrument_symbol == "300059.SZ" for item in route.proposals)
    assert all(item.capability_ids == () for item in route.proposals)
    assert route.provenance is not None
    assert route.provenance.provider == "deepseek"
    assert route.provenance.prompt_version == "idea-route.prompt.v27"
    assert route.provenance.schema_version == "idea-route-provider.v6"
    assert route.execution_settings.model_dump(exclude_none=True) == {}
    assert transport.requests[0].response_schema["additionalProperties"] is False
    properties = cast(dict[str, object], transport.requests[0].response_schema["properties"])
    assert "proposals" in properties
    assert "template_ids" not in properties
    assert "mapping_rationale" not in properties
    assert "不得写股票名称" in transport.requests[0].system_contract
    assert transport.requests[0].response_schema_name == "strategy_ideas"
    assert transport.requests[0].system_footer is not None
    assert transport.requests[0].json_object_contract is not None
    assert "not extraction" in transport.requests[0].json_object_contract
    user_payload = transport.requests[0].user_payload
    assert user_payload is not None
    assert user_payload["capabilityMatrix"] == capability_matrix.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "settings", "evidence"),
    [
        (
            "低买高卖，滑点0，佣金0，最低佣金0，不做稳健性分析",
            {"slippage_bps": 0, "commission_rate": 0, "minimum_commission_cny": 0,
             "run_robustness": False},
            {"slippage_bps": "滑点0", "commission_rate": "佣金0",
             "minimum_commission_cny": "最低佣金0", "run_robustness": "不做稳健性分析"},
        ),
        (
            "低买高卖，滑点0.05%，佣金万三，最低佣金5元",
            {"slippage_bps": 5, "commission_rate": "0.0003", "minimum_commission_cny": 5},
            {"slippage_bps": "滑点0.05%", "commission_rate": "佣金万三",
             "minimum_commission_cny": "最低佣金5元"},
        ),
    ],
)
async def test_idea_execution_settings_preserve_provider_values_with_exact_current_quotes(
    capability_matrix: CandidateCapabilityMatrix,
    utterance: str,
    settings: dict[str, object],
    evidence: dict[str, str],
) -> None:
    # Adapter contract only, not real-model/data acceptance.
    payload = {**_provider_payload(), "execution_settings": settings,
               "execution_setting_evidence": evidence}
    transport = _RecordingTransport([payload])
    route = await VibeIdeaRouter(
        transport, capability_matrix=capability_matrix, researcher=_StaticResearcher(),
    ).route(
        CompileInput(utterance=utterance, as_of_date=date(2026, 9, 6)),
    )
    assert route is not None
    assert route.execution_settings == ExecutionSettingsPatch.model_validate(settings)
    assert len(transport.requests) == 1
    schema = transport.requests[0].response_schema
    properties = cast(dict[str, object], schema["properties"])
    settings_schema = cast(dict[str, object], properties["execution_settings"])
    assert set(cast(dict[str, object], settings_schema["properties"])) == set(
        ExecutionSettingsPatch.model_fields
    )
    assert "execution_setting_evidence" in properties
    for key in ExecutionSettingsPatch.model_fields:
        assert key in transport.requests[0].system_contract


@pytest.mark.asyncio
async def test_idea_model_preserves_waiting_for_users_own_stock(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([{
        **_provider_payload(), "instrument_suggestion_declined": True,
    }])
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        idea_router=VibeIdeaRouter(transport, capability_matrix=capability_matrix),
        backtest_anchor_date=date(2026, 9, 5),
    )
    outcome = await compiler._compile_idea_guidance(CompileInput(
        utterance="我想低买高卖，股票等我补充，先别跑", as_of_date=date(2026, 9, 5),
    ))
    assert outcome is not None and outcome.idea_route is not None
    assert outcome.idea_route.instrument_suggestion_declined is True
    assert outcome.instrument_suggestion_declined is True
    assert outcome.strategy is None and not outcome.run_requested
    properties = cast(dict[str, object], transport.requests[0].response_schema["properties"])
    assert properties["instrument_suggestion_declined"] == {"type": "boolean", "default": False}
    assert "缺少股票本身、或只说先别跑，不能据此拒绝推荐" in transport.requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_repair", [True, False])
async def test_idea_execution_settings_repair_revalidates_quotes_and_field_coverage(
    capability_matrix: CandidateCapabilityMatrix,
    valid_repair: bool,
) -> None:
    malformed = {**_provider_payload(), "execution_settings": {"slippage_bps": 5},
                 "execution_setting_evidence": {}}
    correction = {
        **_provider_payload(), "execution_settings": {"slippage_bps": 5},
        "execution_setting_evidence": {
            "slippage_bps": "滑点0.05%" if valid_repair else "滑点5基点",
        },
    }
    primary, repair = _RecordingTransport([malformed]), _RecordingTransport([correction])
    router = VibeIdeaRouter(primary, capability_matrix=capability_matrix, repair_transport=repair)
    request = CompileInput(utterance="低买高卖，滑点0.05%", as_of_date=date(2026, 9, 6))
    if valid_repair:
        route = await router.route(request)
        assert route is not None
        assert route.execution_settings.slippage_bps == 5
    else:
        with pytest.raises(IdeaGenerationError) as caught:
            await router.route(request)
        assert caught.value.stage == "schema"
    assert len(primary.requests) == len(repair.requests) == 1
    repaired_payload = repair.requests[0].user_payload
    assert repaired_payload is not None
    assert repaired_payload["utterance"] == request.utterance
    assert "execution setting evidence must match changed fields" in str(
        repaired_payload["validationFeedback"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unbound", [False, True])
@pytest.mark.parametrize("plan_kind", [None, "grid", "scheduled"])
async def test_bound_idea_direct_dsl_preserves_server_cash_period_and_instrument(
    capability_matrix: CandidateCapabilityMatrix,
    unbound: bool,
    plan_kind: str | None,
) -> None:
    payload = _provider_payload()
    payload["execution_settings"] = {"slippage_bps": 0, "commission_rate": 0}
    payload["execution_setting_evidence"] = {"slippage_bps": "滑点0", "commission_rate": "佣金0"}
    proposals = cast(list[dict[str, object]], payload["proposals"])
    for period, proposal in zip((20, 30, 60), proposals, strict=True):
        proposal["strategy"] = StrategySpec(
            catalog=CatalogRef(
                catalog_id="cn_a.signals",
                release_version="2026.09.01",
            ),
            execution=DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
            instrument=Instrument(symbol="300059.SZ"),
            entry=IndicatorCondition(
                indicator_id="technical.ma",
                definition_version="1.0.0",
                params={"period": period, "price_field": "close"},
                trigger="price_crosses_above",
            ),
            exit=FirstOfExit(
                children=(
                    IndicatorCondition(
                        indicator_id="technical.ma",
                        definition_version="1.0.0",
                        params={"period": period, "price_field": "close"},
                        trigger="price_crosses_below",
                    ),
                )
            ),
            backtest=BacktestConfig(
                start=date(2025, 9, 4),
                end=date(2026, 9, 4),
                initial_cash_cny=100_000,
            ),
        ).model_dump(mode="json")
        if plan_kind is not None:
            from ashare_lab.domain.strategy import execution_for_price_plan
            from ashare_lab.domain.strategy.price_plans import (
                GridPlan, GridParameters, ScheduledPlan, ScheduledParameters,
            )
            plan = (GridPlan(parameters=GridParameters(
                anchor_mode="first_open", spacing=period, initial_cash_cny=100_000,
                lower_price=1, upper_price=1000,
            )) if plan_kind == "grid" else ScheduledPlan(parameters=ScheduledParameters(
                budget_cny=period * 100, initial_cash_cny=100_000,
            )))
            strategy_data = cast(dict[str, object], proposal["strategy"])
            strategy_data.update(entry=None, exit=None, trading_plan=plan.model_dump(mode="json"),
                                 execution=execution_for_price_plan(plan).model_dump(mode="json"))
            if plan_kind == "grid":
                # Exercise omitted internal bounds through the real discriminated
                # plan schema and router, not a hand-written nested approximation.
                strategy_data["trading_plan"]["parameters"].pop("lower_price")
                strategy_data["trading_plan"]["parameters"].pop("upper_price")
        if unbound:
            strategy = cast(dict[str, object], proposal.pop("strategy"))
            strategy.pop("instrument")
            strategy.pop("schema_version")
            if plan_kind is not None:
                strategy["execution"] = DailyExecutionPolicy().model_dump(mode="json")
            proposal["strategy_template"] = strategy
    transport = _RecordingTransport([payload])
    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
        strategy_catalog=CatalogRef(
            catalog_id="cn_a.signals",
            release_version="2026.09.01",
        ),
    ).route(
        CompileInput(
            utterance="我讨厌特朗普的关税政策，给我三个近一年策略，本金10万元，滑点0，佣金0",
            instrument_context=None if unbound else "300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert len(route.proposals) == 3
    assert route.execution_settings.slippage_bps == route.execution_settings.commission_rate == 0
    strategies = [
        item.strategy_template if unbound else item.strategy for item in route.proposals
    ]
    assert all(strategy is not None for strategy in strategies)
    if unbound:
        assert all(item.strategy is None for item in route.proposals)
        assert all(item.instrument_symbol is None for item in route.proposals)
    observed_cash = {
        strategy.backtest.initial_cash_cny
        for strategy in strategies
        if strategy is not None
    }
    assert observed_cash == {
        100_000
    }
    request = transport.requests[0]
    definitions = cast(dict[str, object], request.response_schema["$defs"])
    proposal_schema = cast(dict[str, object], definitions["_ProviderIdeaProposal"])
    expected_field = "strategy_template" if unbound else "strategy"
    assert expected_field in cast(list[str], proposal_schema["required"])
    if unbound:
        assert "strategy" not in cast(dict[str, object], proposal_schema["properties"])


@pytest.mark.asyncio
async def test_bound_instrument_is_web_researched_and_returned_on_each_card(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    researcher = _StaticResearcher()
    route = await VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        researcher=researcher,
    ).route(
        CompileInput(
            utterance="分析东方财富并给我几个可回测策略",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert route.research is not None
    assert route.research.provider_response_id == "research-1"
    assert len(researcher.requests) == 1
    assert researcher.requests[0].instrument_context == "300059.SZ"
    assert all(item.instrument_symbol == "300059.SZ" for item in route.proposals)
    assert route.understanding == _provider_payload()["understanding"]


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", [
    "东方财富涨3个点就卖，亏2个点就割", "东方财富跌到我买入价的九成就离场",
    "站上昨天的最高价再进去", "赚够三百块就出来",
])
async def test_model_classified_partial_rules_do_not_require_keyword_matched_news_research(
    capability_matrix: CandidateCapabilityMatrix, utterance: str,
) -> None:
    researcher = _StaticResearcher(include_sources=False)
    route = await VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]), capability_matrix=capability_matrix,
        researcher=researcher,
    ).route(CompileInput(
        utterance=utterance, instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 7), semantic_intent="new_strategy",
    ))
    assert route is not None
    assert route.research is None
    assert researcher.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model_viewpoint", [False, True])
async def test_research_happens_before_model_and_verified_facts_reach_its_prompt(
    capability_matrix: CandidateCapabilityMatrix, model_viewpoint: bool,
) -> None:
    order: list[str] = []
    researcher = _StaticResearcher(order)
    transport = _OrderedTransport([_provider_payload()], order)

    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=researcher,
    ).route(
        CompileInput(
            utterance="这种走向让我挺不安的" if model_viewpoint else "我讨厌特朗普",
            semantic_intent="viewpoint" if model_viewpoint else None,
            idea_inspiration="谨慎观察的策略思路" if model_viewpoint else None,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert order == ["research", "model"]
    payload = transport.requests[0].user_payload
    assert payload is not None
    assert cast(dict[str, object], payload["research"])["summary"] == "检索结果提到同花顺。"
    assert payload["capabilityMatrix"] == capability_matrix.model_dump(mode="json")
    assert "research 是服务端先行检索" in transport.requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("with_empty_result", [False, True])
async def test_current_affairs_requires_successful_source_backed_research(
    capability_matrix: CandidateCapabilityMatrix,
    with_empty_result: bool,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    researcher = _StaticResearcher(include_sources=False) if with_empty_result else None
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=researcher,
    )

    with pytest.raises(IdeaResearchUnavailableError):
        await router.route(
            CompileInput(
                utterance="我讨厌特朗普",
                instrument_context="300059.SZ",
                as_of_date=date(2026, 9, 4),
            )
        )

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_available", [False, True])
async def test_model_viewpoint_research_failure_still_acknowledges_user(
    capability_matrix: CandidateCapabilityMatrix, reply_available: bool,
) -> None:
    natural_reply = "听起来这件事让你很不安。联网检索这次没成功，我们也可以先聊聊你的担心。"
    reply_transport = _RecordingTransport([{
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": natural_reply,
    }] if reply_available else [CandidateTransportError("unavailable")])
    idea_transport = _RecordingTransport([_provider_payload()])
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=VibeIdeaRouter(idea_transport, capability_matrix=capability_matrix),
        clarification_dialogue_router=VibeClarificationDialogueRouter(
            reply_transport, capability_matrix=capability_matrix,
        ),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
    )
    outcome = await compiler.compile(CompileInput(
        utterance="这种走向让我挺不安的", as_of_date=date(2026, 9, 4),
        semantic_intent="viewpoint", instrument_context="300059.SZ",
    ))
    assert outcome.diagnostic_code == "idea_research_unavailable"
    assert outcome.strategy is None and outcome.idea_route is None
    assert not outcome.run_requested
    assert idea_transport.requests == []
    assert len(reply_transport.requests) == 1
    if reply_available:
        assert outcome.clarification == natural_reply
    else:
        assert "服务侧的问题" in outcome.clarification


@pytest.mark.asyncio
async def test_compiler_reports_research_failure_without_generating_a_strategy(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)
    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=router,
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_research_unavailable"
    assert outcome.strategy is None
    assert outcome.idea_route is None
    assert "联网事实检索暂时不可用" in (outcome.clarification or "")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_pure_technical_vague_strategy_does_not_require_web_research(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert route.research is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_model_classified_vague_strategy_never_depends_on_web_research(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    """A named-stock strategy request stays executable when search is down."""

    transport = _RecordingTransport([_provider_payload()])
    researcher = _StaticResearcher()
    router = VibeIdeaRouter(
        transport, capability_matrix=capability_matrix, researcher=researcher,
    )

    route = await router.route(CompileInput(
        utterance="东方财富短线策略", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 8), semantic_intent="vague_strategy",
    ))

    assert route is not None
    assert route.research is None
    assert researcher.requests == []
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_unbound_vague_strategy_does_not_silently_expand_into_stock_selection(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    class Selector:
        async def select(self, request, research):
            raise AssertionError("a strategy-only request must not start stock selection")

    router = VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        stock_selector=Selector(),
    )
    route = await router.route(CompileInput(
        utterance="想做个宽一点的网格",
        as_of_date=date(2026, 9, 8),
        semantic_intent="vague_strategy",
        idea_inspiration="想做个宽一点的网格",
    ))
    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert all(item.instrument_symbol is None for item in route.proposals)


@pytest.mark.asyncio
async def test_casual_dca_inspiration_does_not_silently_select_a_stock(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    class Selector:
        async def select(self, request, research):
            raise AssertionError("a stock was not requested")

    router = VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        stock_selector=Selector(),
    )
    route = await router.route(CompileInput(
        utterance="定期投点钱进去，省得总盯盘",
        as_of_date=date(2026, 9, 8), semantic_intent="casual",
        idea_inspiration="定期投点钱进去，省得总盯盘",
    ))
    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert all(item.instrument_symbol is None for item in route.proposals)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_sources", [True, False])
async def test_unavailable_event_conditions_return_research_not_replacement_strategies(
    capability_matrix: CandidateCapabilityMatrix, include_sources: bool,
) -> None:
    payload = _provider_payload()
    payload["proposals"] = []
    payload["research_fallback_reason"] = "当前矩阵未提供公告历史事件条件"
    summary = "检索资料分析。" * 70
    researcher = _StaticResearcher(include_sources=include_sources, summary=summary)
    router = VibeIdeaRouter(
        _RecordingTransport([payload]), capability_matrix=capability_matrix,
        researcher=researcher,
    )
    request = CompileInput(
        utterance="东方财富发布大股东增持公告后次日买入，持有30个交易日卖出",
        instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
        semantic_intent="new_strategy",
    )
    if not include_sources:
        with pytest.raises(IdeaResearchUnavailableError):
            await router.route(request)
    else:
        route = await router.route(request)
        assert route is not None and route.proposals == ()
        assert route.research is not None and route.research.sources
        assert "已转用联网搜索" in route.understanding
        assert len(route.understanding) <= 240
        assert route.research.summary == summary
    assert len(researcher.requests) == 1
    assert researcher.requests[0].query == request.utterance


@pytest.mark.asyncio
async def test_viewpoint_without_instrument_returns_unbound_directions(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert route.asset_mapping.relation == "unbound"
    assert route.asset_mapping.evidence_status == "instrument_required"
    assert len(route.proposals) >= 2
    assert all(
        any("补充具体 A 股" in assumption for assumption in item.assumptions)
        for item in route.proposals
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal_count", [1, 2, 3])
async def test_direct_model_strategy_fixtures_recompile_through_the_existing_dsl(
    capability_matrix, proposal_count: int,
) -> None:
    payload = _provider_payload()
    payload["proposals"] = cast(list[object], payload["proposals"])[:proposal_count]
    transport = _RecordingTransport([payload])
    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    ).route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )
    assert route is not None

    assert len(route.proposals) == proposal_count

    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
    )
    for proposal in route.proposals:
        outcome = await compiler.compile(
            CompileInput(
                utterance=proposal.suggested_utterance,
                instrument_context="300059.SZ",
                as_of_date=date(2026, 8, 30),
            )
        )
        assert outcome.status is CompileStatus.READY, proposal.id
        assert outcome.strategy is not None
        assert outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
async def test_router_guides_without_instrument_but_keeps_asset_unbound(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert route.asset_mapping.relation == "unbound"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_router_does_not_allow_an_invalid_or_non_share_context(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="399001.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["success", "schema", "execution", "transport"])
async def test_one_format_repair_reuses_research_and_keeps_all_validation(
    capability_matrix: CandidateCapabilityMatrix, result: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    valid = _provider_payload()
    proposals = cast(list[dict[str, object]], valid["proposals"])
    catalog = CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01")
    for period, proposal in zip((20, 30, 60), proposals, strict=True):
        strategy = StrategySpec(
            catalog=catalog, instrument=Instrument(symbol="300059.SZ"),
            execution=DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
            entry=IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"}, trigger="price_crosses_above",
            ),
            exit=FirstOfExit(children=(IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"}, trigger="price_crosses_below",
            ),)),
            backtest=BacktestConfig(
                start=date(2025, 9, 5), end=date(2026, 9, 5), initial_cash_cny=1_000_000,
            ),
        ).model_dump(mode="json")
        strategy.pop("instrument")
        strategy.pop("schema_version")
        proposal["strategy_template"] = strategy
    malformed = {**valid, "private-provider-key": "private-provider-output"}
    correction: object = valid
    if result == "schema":
        correction = malformed
    elif result == "transport":
        correction = CandidateTransportError("private-provider-error", timed_out=True)
    elif result == "execution":
        changed = deepcopy(valid)
        for item in cast(list[dict[str, object]], changed["proposals"]):
            template = cast(dict[str, object], item["strategy_template"])
            cast(dict[str, object], template["backtest"])["initial_cash_cny"] = 123_456
        correction = changed
    primary = _RecordingTransport([malformed])
    repair = _RecordingTransport([correction])
    researcher = _StaticResearcher()
    router = VibeIdeaRouter(
        primary, capability_matrix=capability_matrix, researcher=researcher,
        strategy_catalog=catalog, repair_transport=repair,
        repair_provider_identity=CandidateProviderIdentityView(
            provider="test", model="format-repair-fixture",
            prompt_version="test", schema_version="test",
        ),
    )
    request = CompileInput(utterance="我讨厌特朗普", as_of_date=date(2026, 9, 5))
    if result == "success":
        route = await router.route(request)
        assert route is not None and len(route.proposals) == 3
        assert route.provenance is not None and route.provenance.model == "format-repair-fixture"
        assert all(item.strategy_template is not None for item in route.proposals)
    else:
        with pytest.raises(IdeaGenerationError) as caught:
            await router.route(request)
        assert caught.value.stage == result
    assert len(primary.requests) == len(repair.requests) == len(researcher.requests) == 1
    original = primary.requests[0]
    repaired = repair.requests[0]
    assert repaired.response_schema == original.response_schema
    assert original.user_payload is not None and repaired.user_payload is not None
    for key, value in original.user_payload.items():
        assert repaired.user_payload[key] == value
    assert repaired.user_payload["previousResponse"] == malformed
    assert "extra_forbidden" in str(repaired.user_payload["validationFeedback"])
    assert "private-provider" not in caplog.text
    assert "extra_forbidden" in caplog.text and "[field]" in caplog.text


@pytest.mark.asyncio
async def test_initial_transport_failure_is_not_retried_as_format_repair(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    primary = _RecordingTransport([
        CandidateTransportError("private transport detail", timed_out=True),
    ])
    repair = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(primary, capability_matrix=capability_matrix, repair_transport=repair)
    with pytest.raises(IdeaGenerationError) as caught:
        await router.route(CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 5)))
    assert caught.value.stage == "transport" and caught.value.timed_out
    assert len(primary.requests) == 1 and repair.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["invalid_response", "incomplete_response"])
async def test_invalid_structured_transport_response_regenerates_once_with_repair_model(
    capability_matrix: CandidateCapabilityMatrix,
    kind: str,
) -> None:
    primary = _RecordingTransport([
        CandidateTransportError("private malformed response", failure_kind=kind),
    ])
    repair = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        primary,
        capability_matrix=capability_matrix,
        repair_transport=repair,
        repair_provider_identity=CandidateProviderIdentityView(
            provider="test", model="strong-repair", prompt_version="test",
            schema_version="test",
        ),
    )

    route = await router.route(CompileInput(
        utterance="给我三个低买高卖的思路", as_of_date=date(2026, 9, 5),
    ))

    assert route is not None and len(route.proposals) == 3
    assert len(primary.requests) == len(repair.requests) == 1
    retry = repair.requests[0]
    assert retry.user_payload is not None
    assert retry.user_payload["regenerationReason"] == (
        "previous_structured_response_incomplete"
    )
    assert "完整 JSON" in (retry.system_footer or "")
    assert route.provenance is not None and route.provenance.model == "strong-repair"


@pytest.mark.asyncio
async def test_idea_provider_context_is_bounded_before_generation(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(summary="研究摘要" * 5_000),
    )

    route = await router.route(CompileInput(
        utterance="我讨厌特朗普",
        semantic_intent="viewpoint",
        instrument_context="300059.SZ",
        idea_context=tuple("历史对话" * 1_000 for _ in range(20)),
        as_of_date=date(2026, 9, 5),
    ))

    assert route is not None
    payload = transport.requests[0].user_payload
    assert payload is not None
    turns = cast(list[str], payload["recentIdeaTurns"])
    research = cast(dict[str, object], payload["research"])
    assert len(turns) == 6 and all(len(turn) <= 1_600 for turn in turns)
    assert len(cast(str, research["summary"])) <= 4_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_case",
    [
        "empty",
        "duplicate",
        "missing_entry",
        "missing_exit",
        "missing_backtest",
        "instrument_code",
        "unsafe_claim",
        "executable_code",
    ],
)
async def test_invalid_provider_strategy_batch_fails_closed_without_hidden_retry(
    capability_matrix: CandidateCapabilityMatrix,
    bad_case: str,
) -> None:
    payload = _provider_payload()
    proposals = cast(list[dict[str, str]], payload["proposals"])
    if bad_case == "empty":
        payload["proposals"] = []
    elif bad_case == "duplicate":
        proposals[1] = dict(proposals[0])
    elif bad_case == "missing_entry":
        proposals[0]["suggested_utterance"] = "RSI低于30时观察，高于70卖出，回测近1年"
    elif bad_case == "missing_exit":
        proposals[0]["suggested_utterance"] = "RSI低于30买入，高于70时观察，回测近1年"
    elif bad_case == "missing_backtest":
        proposals[0]["suggested_utterance"] = "RSI低于30买入，高于70卖出"
    elif bad_case == "instrument_code":
        proposals[0]["suggested_utterance"] = (
            "300033.SZ的RSI低于30买入，高于70卖出，回测近1年"
        )
    elif bad_case == "unsafe_claim":
        proposals[0]["hypothesis"] = "这个方案保证盈利"
    else:
        proposals[0]["suggested_utterance"] = (
            "用Python在RSI低于30买入，高于70卖出，回测近1年"
        )
    transport = _RecordingTransport([payload])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_display_review_omits_inactive_reference_without_rewriting_rule(capability_matrix):
    from copy import deepcopy
    from ashare_lab.adapters.language.reply_semantic_review import review_display_semantics

    source = _RecordingTransport([_provider_payload()])
    await VibeIdeaRouter(source, capability_matrix=capability_matrix).route(
        CompileInput(utterance="给几个简单策略", semantic_intent="new_strategy",
                     instrument_context="300059.SZ", as_of_date=date(2026, 8, 30)))
    rule = {"kind": "rebound", "side": "buy", "reference_mode": "previous_fill", "gap": 2}
    before = deepcopy(rule)
    transport = _RecordingTransport([{"facts": "supported", "state_and_authority": "supported",
                                     "user_intent_and_tone": "supported"}])
    assert await review_display_semantics(transport, source.requests[0],
        display_payload={"rule": rule}, verified_context={"rule": rule}, response_scope="参数说明")
    payload = transport.requests[0].user_payload
    assert "reference_mode" not in payload["rule"]
    assert "reference_mode" not in payload["reply"]["rule"]
    assert rule == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["insufficient_balance", "authentication_failed", "timeout"])
async def test_explanation_provider_failure_keeps_classification(capability_matrix, kind):
    from ashare_lab.adapters.language.reply_semantic_review import review_display_semantics

    source = _RecordingTransport([_provider_payload()])
    route = await VibeIdeaRouter(source, capability_matrix=capability_matrix).route(
        CompileInput(utterance="给几个简单策略", semantic_intent="new_strategy",
                     instrument_context="300059.SZ", as_of_date=date(2026, 8, 30)))
    assert route is not None
    failure = CandidateTransportError("private detail", failure_kind=kind)
    arguments = {"display_payload": {}, "verified_context": {}, "response_scope": "核对说明"}
    transport = _RecordingTransport([failure])
    with pytest.raises(CandidateTransportError) as caught:
        await _repair_idea_explanation(transport, source.requests[0], route, arguments)
    assert caught.value is failure
    assert len(transport.requests) == 1
    transport = _RecordingTransport([failure])
    with pytest.raises(CandidateTransportError) as caught:
        await review_display_semantics(transport, source.requests[0], **arguments)
    assert caught.value is failure
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_declared_provider_failure_is_not_exposed_as_guidance(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([CandidateTransportError("secret provider detail")])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_researched_viewpoint_without_instrument_stays_unbound(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我看好同花顺",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert len(route.proposals) == 3
    assert route.asset_mapping.instrument_symbol is None
    assert route.asset_mapping.evidence_status == "instrument_required"
    assert all(item.instrument_symbol is None for item in route.proposals)
    assert len({item.id for item in route.proposals}) == 3
    assert route.understanding == _provider_payload()["understanding"]
    request_payload = transport.requests[0].user_payload
    assert request_payload is not None
    assert "resolvedAshareCandidates" not in request_payload
    assert "不得选择、推荐或编造股票" in transport.requests[0].system_contract
@pytest.mark.parametrize('prefix,verified,valid', [
    ('麒麟信安688152.SH，', ('688152.SH',), True),
    ('688152，', ('688152.SH',), True),
    ('688152.SZ，', ('688152.SH',), False),
    ('688152.SH，', (), False),
    ('本金600000元，', (), True),
    ('持仓300000股，', (), True),
])
def test_display_identity_uses_verified_context_not_six_digit_quantity(prefix, verified, valid):
    from ashare_lab.adapters.language.vibe_ideas import _parse_provider_route
    payload = _provider_payload()
    payload['proposals'][0]['suggested_utterance'] = prefix + payload['proposals'][0]['suggested_utterance']
    if valid:
        _parse_provider_route(payload, utterance='网格策略', verified_symbols=verified)
    else:
        with pytest.raises(ValueError, match='cannot choose an instrument code'):
            _parse_provider_route(payload, utterance='网格策略', verified_symbols=verified)
@pytest.mark.asyncio
async def test_verified_display_code_does_not_consume_model_repair(capability_matrix):
    payload = _provider_payload()
    for proposal in payload['proposals']:
        proposal['suggested_utterance'] = '300059.SZ，' + proposal['suggested_utterance']
    transport = _RecordingTransport([payload])
    repair = _RecordingTransport([])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix, repair_transport=repair)
    result = await router.route(CompileInput(utterance='东方财富低买高卖',
        instrument_context='300059.SZ', as_of_date=date(2026, 9, 4)))
    assert result is not None and len(result.proposals) == 3
    assert len(transport.requests) == 1
    assert not repair.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', [False, True])
async def test_short_selection_supplements_or_preserves_evidenced_batch(capability_matrix, unavailable):
    from dataclasses import replace
    from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderDataError
    first = _selection_evidence()
    complete = replace(first, rows=first.rows + (
        {'代码': '000001', '主营业务': '受控业务'},
        {'代码': '600519', '主营业务': '受控业务'},
    ))
    stocks = tuple(SimpleNamespace(symbol=s, name=s, reason='受控依据')
                   for s in ('300059.SZ', '000001.SZ', '600519.SH'))
    provider = Mock(screen=AsyncMock(side_effect=[first,
        MxSaasProviderDataError('unavailable') if unavailable else complete]))
    advisor = Mock(recommend_stocks=AsyncMock(side_effect=[stocks[:1], stocks]))
    selector = InspirationStockSelector(
        transport=_RecordingTransport([{'declined': False, 'framing': '可能的研究联系',
                                       'screen_query': '用户指定业务方向'}]),
        matrix=capability_matrix, provider=provider, advisor=advisor,
    )
    result = await selector.select(CompileInput(utterance='我讨厌特朗普',
                                   as_of_date=date(2026, 9, 14)), None)
    assert result is not None
    assert result.framing == '可能的研究联系'
    assert len(result.alternatives) == (0 if unavailable else 2)
    assert result.evidence is (first if unavailable else complete)
    assert provider.screen.await_count == 2
    assert provider.screen.await_args_list[1].kwargs['query'].startswith('用户指定业务方向')
