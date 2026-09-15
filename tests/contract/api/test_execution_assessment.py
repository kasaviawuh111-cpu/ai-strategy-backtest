from dataclasses import replace

from ashare_lab.api.execution_assessment import assess_execution
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.domain.strategy import DailyExecutionPolicy, StrategySpec
from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters, ScheduledPlan, ScheduledParameters
from tests.contract.api.test_backtest_preflight import _strategy


def outcome(plan):
    strategy = StrategySpec.model_validate({**_strategy().model_dump(), "entry": None, "exit": None,
        "trading_plan": plan, "execution": DailyExecutionPolicy(position_policy="bounded_inventory")})
    return CompileOutcome(status=CompileStatus.READY, strategy=strategy)


def test_transient_service_failure_does_not_reuse_old_strategy_or_claim_parse_failure():
    for code, dependency in (
        ("instrument_resolution_unavailable", "证券身份查询服务"),
        ("dialogue_model_unavailable", "策略理解服务"),
    ):
        result = CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code=code,
            revision_base_strategy=_strategy(),
        )
        assessment = assess_execution(result)
        assert assessment.status == "temporarily_unavailable"
        assert assessment.missing == (dependency,)
        assert assessment.interpreted_strategy is None
        assert assessment.strategy_hash is None
        assert "请稍后重试" in assessment.message
        assert result.status is CompileStatus.NEEDS_CLARIFICATION
    assert assess_execution(replace(result, diagnostic_code="instrument_unconfirmed")) is None


def test_minute_and_limit_grid_are_not_described_as_daily_open_market_orders():
    p = GridParameters(anchor_price=10, lower_price=5, upper_price=15,
                       observation="minute_bar", price_mode="grid_limit")
    assessment = assess_execution(outcome(GridPlan(parameters=p)))
    assert "1分钟" in assessment.message and "下一根分钟" in assessment.message
    assert "日线开盘价" not in assessment.message
    daily = assess_execution(outcome(GridPlan(parameters=p.model_copy(update={"observation": "daily_close"}))))
    assert "限价" in daily.message and "触发不代表成交" in daily.message


def test_close_schedule_does_not_claim_open_fill():
    assessment = assess_execution(outcome(ScheduledPlan(parameters=ScheduledParameters(at="close"))))
    assert "收盘" in assessment.message
    assert "日线开盘价模拟" not in assessment.message


def test_query_failure_and_verified_missing_history_keep_distinct_capabilities():
    ready = outcome(GridPlan(parameters=GridParameters(anchor_price=10, lower_price=5, upper_price=15)))
    for code, expected in (
        ("skill_numeric_query_failed", "temporarily_unavailable"),
        ("backtest_data_preparation_timeout", "temporarily_unavailable"),
        ("skill_numeric_history_unavailable_after_query_retry", "understood_not_executable"),
        ("skill_numeric_unit_unconfirmed", "understood_not_executable"),
        ("skill_history_incomplete", "understood_not_executable"),
    ):
        pending = replace(ready, status=CompileStatus.NEEDS_CLARIFICATION,
                          strategy=None, revision_base_strategy=ready.strategy,
                          diagnostic_code=code, clarification="原规则已保留，本次未启动回测。")
        assessment = assess_execution(pending)
        assert assessment.status == expected
        assert assessment.interpreted_strategy == ready.strategy
        assert assessment.message == pending.clarification
        assert pending.strategy is None


def test_hybrid_assessment_keeps_daily_signal_and_minute_protection_clocks():
    from ashare_lab.domain.strategy import FirstOfExit, HybridExecutionPolicy, MinuteProtectionExit

    strategy = StrategySpec.model_validate({**_strategy().model_dump(),
        "exit": FirstOfExit(children=(MinuteProtectionExit(stop_loss_pct=3),)),
        "execution": HybridExecutionPolicy()})
    assessment = assess_execution(CompileOutcome(status=CompileStatus.READY, strategy=strategy))
    assert "日线信号收盘确认" in assessment.message
    assert "持仓保护按分钟检查" in assessment.message
    assert "触发后下一根分钟" in assessment.message
    assert "启动回测时确认数据与执行能力" in assessment.message
    assert assessment.status != "executable"


def test_quote_gap_retains_interpreted_latest_anchor_and_names_actual_missing_data():
    ready = outcome(GridPlan(parameters=GridParameters(anchor_mode="latest_price", lower_price=1, upper_price=1000)))
    missing = replace(ready, status=CompileStatus.NEEDS_CLARIFICATION, strategy=None,
                      revision_base_strategy=ready.strategy, diagnostic_code="grid_latest_quote_unavailable")
    assessment = assess_execution(missing)
    assert assessment.status == "temporarily_unavailable"
    assert assessment.interpreted_strategy == ready.strategy
    assert assessment.missing == ("该股票的行情最新价",)


def test_missing_inventory_is_understood_and_uses_current_preview_not_old_edit_base():
    from ashare_lab.domain.strategy import canonical_hash
    from ashare_lab.domain.strategy.price_plans import ConditionalPlan

    current = outcome(ConditionalPlan(parameters={"rules": [
        {"kind": "take_profit", "side": "sell", "gap": 5, "sizing_mode": "all_position"},
    ]})).strategy
    old = _strategy()
    result = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code="execution_prerequisite_required",
        suggested_strategy=current,
        revision_base_strategy=old,
    )
    assessment = assess_execution(result)
    assert assessment.status == "understood_not_executable"
    assert assessment.missing == ("买入规则或期初可卖持仓",)
    assert assessment.interpreted_strategy == current
    assert assessment.strategy_hash == canonical_hash(current)
    assert result.strategy is None and result.status is CompileStatus.NEEDS_CLARIFICATION

    without_preview = assess_execution(replace(result, suggested_strategy=None))
    assert without_preview.interpreted_strategy is None
    assert without_preview.strategy_hash is None
