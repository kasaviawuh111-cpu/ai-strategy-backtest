"""Read-only execution explanations; never grant execution or reject parsing."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.domain.strategy import HybridExecutionPolicy, StrategySpec, canonical_hash
from ashare_lab.domain.strategy.price_plans import ConditionalPlan, GridPlan, ScheduledPlan


class ExecutionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal[
        "executable", "research_degraded", "understood_not_executable", "temporarily_unavailable",
    ]
    message: str
    missing: tuple[str, ...] = ()
    interpreted_strategy: StrategySpec | None = None
    strategy_hash: str | None = None


def assess_execution(outcome: CompileOutcome) -> ExecutionAssessment | None:
    """Only expose retained rules for a known data/preparation failure.

    A revision base in an unrelated edit/chat may be an older strategy. It must
    never be presented as a successful interpretation of the latest message.
    """
    code = outcome.diagnostic_code or ""
    if code in {"minute_execution_unavailable", "grid_execution_unavailable",
                "backtest_data_not_yet_available", "backtest_date_range_invalid"}:
        date_issue = code in {"backtest_data_not_yet_available", "backtest_date_range_invalid"}
        strategy = outcome.strategy or (outcome.revision_base_strategy if date_issue else None)
        return ExecutionAssessment(
            status=("temporarily_unavailable" if code == "minute_execution_unavailable"
                    else "understood_not_executable"),
            message=outcome.clarification or "这套策略暂时无法回测，买卖规则已保留。",
            missing=("所选日期的行情" if date_issue else
                     "回测服务" if code == "minute_execution_unavailable" else "该交易方式的回测支持",),
            interpreted_strategy=strategy,
            strategy_hash=canonical_hash(strategy) if strategy is not None else None,
        )
    transient_services = {
        "instrument_resolution_unavailable": "证券身份查询服务",
        "dialogue_model_unavailable": "策略理解服务",
    }
    if code in transient_services:
        # No current interpretation is proven when a required service fails.
        # In particular, an old revision base must not become this input's plan.
        dependency = transient_services[code]
        return ExecutionAssessment(
            status="temporarily_unavailable",
            message=outcome.clarification or (
                dependency + "暂时不可用，原输入已保留，请稍后重试；本次尚未回测。"
            ),
            missing=(dependency,),
        )
    if code == "execution_prerequisite_required":
        # Only this response's validated preview represents the latest input.
        # A revision base may belong to an older, unrelated strategy.
        strategy = outcome.suggested_strategy
        return ExecutionAssessment(
            status="understood_not_executable",
            message=outcome.clarification or (
                "卖出规则已识别并保留；补充买入规则或期初可卖持仓后，"
                "再检查数据与执行条件。本次尚未回测。"
            ),
            missing=("买入规则或期初可卖持仓",),
            interpreted_strategy=strategy,
            strategy_hash=canonical_hash(strategy) if strategy is not None else None,
        )
    if outcome.status is CompileStatus.READY and outcome.strategy is not None:
        plan = outcome.strategy.trading_plan
        if isinstance(outcome.strategy.execution, HybridExecutionPolicy):
            execution = "日线信号收盘确认、下一交易日开盘起委托；持仓保护按分钟检查，触发后下一根分钟起委托"
        elif isinstance(plan, (GridPlan, ConditionalPlan)) and plan.parameters.observation == "minute_bar":
            execution = "已识别1分钟价格计划，新委托从下一根分钟起生效"
        elif isinstance(plan, ScheduledPlan):
            execution = "已识别定时计划，按计划交易日及开盘或收盘时点模拟"
            if plan.parameters.exit_rules:
                execution += "；退出条件按分钟及实际持有批次检查，卖出后继续后续定投"
        elif isinstance(plan, GridPlan):
            execution = ("已识别网格限价计划，触发不代表成交"
                         if plan.parameters.price_mode != "next_open" else "已识别网格市价计划")
        elif isinstance(plan, ConditionalPlan):
            execution = "已识别日线条件计划，按所设触发条件与委托类型模拟"
        else:
            execution = "按日线收盘确认信号，下一交易日开盘起模拟委托"
        return ExecutionAssessment(
            status="research_degraded",
            message=execution + "；启动回测时确认数据与执行能力，实际口径以报告为准。",
        )
    missing: tuple[str, ...] = ()
    temporary = False
    # These codes describe understood requirements, not malformed language.
    capability_gaps = {
        "non_daily_timeframe_not_supported": ("所需频率的行情与撮合支持",),
        "same_session_execution_not_supported": ("日内行情与成交时序支持",),
        "execution_price_time_not_supported": ("指定时点行情与撮合支持",),
        "document_text_not_supported": ("可回放的历史正文数据",),
        "previous_session_limit_up_capability_unavailable": ("前一交易日涨停状态",),
    }
    if code == "grid_latest_quote_unavailable":
        temporary = True
        missing = ("该股票的行情最新价",)
    elif code in {"backtest_data_temporarily_unavailable", "backtest_data_preparation_timeout"}:
        temporary = True
        missing = ("本次数据查询结果",)
    elif code.startswith("skill_numeric_"):
        missing = ("条件对应的历史指标或单位口径",)
        temporary = "query_failed" in code
    elif code in {"skill_history_fields_missing", "skill_history_incomplete"}:
        missing = ("完整历史行情字段",)
    elif code in {"skill_indicator_history_not_ready", "skill_indicator_unavailable"}:
        missing = ("有效历史指标",)
    elif code in {"backtest_condition_data_unavailable", "capability_research_fallback"}:
        missing = ("原条件对应的历史数据",)
    elif code in capability_gaps:
        missing = capability_gaps[code]
    else:
        return None
    # Capability rejects can carry an OLD edit base. It is not a new parsed plan.
    strategy = None if code in capability_gaps else outcome.revision_base_strategy
    return ExecutionAssessment(
        status="temporarily_unavailable" if temporary else "understood_not_executable",
        message=outcome.clarification or (
            "已理解这项要求，暂缺" + "、".join(missing) + "。原要求已保留，尚未开始回测。"
        ),
        missing=missing, interpreted_strategy=strategy,
        strategy_hash=canonical_hash(strategy) if strategy is not None else None,
    )
