"""Typed, immutable models for the first executable Strategy DSL v1 subset."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from math import isfinite
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ashare_lab.domain.financials import (
    FIRST_FINANCIAL_METRIC_CATALOG,
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialReportType,
    FinancialStatementScope,
    FinancialUnit,
)

from .price_plans import PricePlan
from .independent_plans import IndependentPlanPair

type JsonScalar = str | int | float | bool


class FrozenModel(BaseModel):
    """Strict-shaped value object shared by the DSL models."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CatalogRef(FrozenModel):
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    release_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Instrument(FrozenModel):
    market: Literal["CN_A"] = "CN_A"
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    position_mode: Literal["long_only"] = "long_only"

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class IndicatorCondition(FrozenModel):
    type: Literal["indicator_condition"] = "indicator_condition"
    indicator_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    definition_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    params: dict[str, JsonScalar] = Field(default_factory=dict, max_length=16)
    timeframe: Literal["1d"] = "1d"
    evaluation_mode: Literal["bar_close_confirmed"] = "bar_close_confirmed"
    trigger: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: float | None = None

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("indicator comparison value must be finite")
        return value

    @field_validator("params")
    @classmethod
    def parameter_names_are_stable(cls, params: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for name, value in params.items():
            if not name or not name.replace("_", "a").isalnum() or not name[0].isalpha():
                raise ValueError(f"invalid parameter name: {name!r}")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError(f"parameter {name!r} must be finite")
        return params


class FinancialCondition(FrozenModel):
    """A direct provider financial fact evaluated as known at each bar close."""

    type: Literal["financial_condition"] = "financial_condition"
    metric_id: FinancialMetricId
    definition_version: Literal["1.0.0"] = "1.0.0"
    report_type: FinancialReportType | None = None
    period_basis: FinancialPeriodBasis | None = None
    statement_scope: FinancialStatementScope | None = None
    revision_policy: Literal["as_known_at_signal"] = "as_known_at_signal"
    comparator: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: Decimal
    unit: FinancialUnit

    @field_validator("value", mode="before")
    @classmethod
    def value_is_numeric_and_finite(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("financial comparison value must be numeric")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("financial comparison value must be finite")
        return value

    @field_validator("value")
    @classmethod
    def decimal_value_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("financial comparison value must be finite")
        return value

    @model_validator(mode="after")
    def shape_matches_metric_catalog(self) -> FinancialCondition:
        definition = FIRST_FINANCIAL_METRIC_CATALOG.definition_for(self.metric_id)
        if (
            self.period_basis is not None
            and self.period_basis not in definition.allowed_period_bases
        ):
            raise ValueError("period_basis is not allowed for metric")
        if self.unit is not definition.unit:
            raise ValueError("unit does not match metric catalog")
        if definition.data_kind.value == "statement":
            if self.statement_scope is None:
                raise ValueError("statement condition requires statement_scope")
            if self.statement_scope not in definition.allowed_statement_scopes:
                raise ValueError("statement_scope is not allowed for metric")
        elif self.report_type is not None or self.statement_scope is not None:
            raise ValueError("valuation condition cannot declare report shape")
        return self


class EventCondition(FrozenModel):
    """A catalog-coded event publication with bounded deterministic filters."""

    type: Literal["event_condition"] = "event_condition"
    event_code: str = Field(pattern=r"^event\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    definition_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    trigger: Literal["published"] = "published"
    attributes: dict[str, JsonScalar] = Field(default_factory=dict, max_length=8)
    document_text: EventDocumentTextPredicate | None = None

    @field_validator("attributes")
    @classmethod
    def attribute_filters_are_bounded(
        cls,
        attributes: dict[str, JsonScalar],
    ) -> dict[str, JsonScalar]:
        for name, value in attributes.items():
            if not name or not name.replace("_", "a").isalnum() or not name[0].isalpha():
                raise ValueError(f"invalid event attribute name: {name!r}")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError(f"event attribute {name!r} must be finite")
        return attributes


class EventDocumentTextPredicate(FrozenModel):
    """A reproducible term-count predicate bound to the same event document.

    The document extractor and normalization algorithm are part of the frozen
    event snapshot.  The strategy stores only the deterministic comparison; an
    LLM is never asked to count or summarize the report.
    """

    metric_id: Literal["document.literal_mention_count"] = "document.literal_mention_count"
    metric_version: Literal["1.0.0"] = "1.0.0"
    term: str = Field(min_length=1, max_length=64)
    normalization: Literal["nfkc"] = "nfkc"
    match_mode: Literal["ascii_token", "literal"]
    case_sensitive: bool = False
    # The current embedded-text evidence can under-count a damaged PDF text
    # layer.  Lower-bound predicates remain fail-safe under that failure mode:
    # they may miss a trade, but cannot create one merely because text is
    # missing. Equality and upper-bound predicates are therefore not executable.
    comparator: Literal["gt", "gte"]
    value: int = Field(ge=0, le=1_000_000)

    @field_validator("term")
    @classmethod
    def term_is_plain_text(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("document term cannot contain control characters")
        if not value.strip():
            raise ValueError("document term cannot be blank")
        return value

    @model_validator(mode="after")
    def ascii_mode_requires_ascii_term(self) -> EventDocumentTextPredicate:
        if self.match_mode == "ascii_token" and (
            not self.term.isascii() or not any(character.isalnum() for character in self.term)
        ):
            raise ValueError("ascii_token match mode requires an ASCII alphanumeric term")
        return self


class AllCondition(FrozenModel):
    type: Literal["all"] = "all"
    children: tuple[Condition, ...] = Field(min_length=2, max_length=16)


class AnyCondition(FrozenModel):
    type: Literal["any"] = "any"
    children: tuple[Condition, ...] = Field(min_length=2, max_length=16)


class NotCondition(FrozenModel):
    type: Literal["not"] = "not"
    child: Condition


type Condition = Annotated[
    IndicatorCondition
    | FinancialCondition
    | EventCondition
    | AllCondition
    | AnyCondition
    | NotCondition,
    Field(discriminator="type"),
]


class FirstOfExit(FrozenModel):
    # ALL exits are evaluated together at daily close; holding maturity remains true afterwards.
    op: Literal["first_of", "all"] = "first_of"
    children: tuple[ExitRule, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def normalize_equivalent_exit_shape(cls, value: object) -> object:
        # A single rule is an OR group of one. Do not change its parameters,
        # and leave conflicting or unknown fields for normal validation.
        if not isinstance(value, dict) or "op" in value:
            return value
        kind = value.get("type")
        if not isinstance(kind, str):
            return value
        if kind == "first_of":
            return {"op": "first_of", **{key: item for key, item in value.items() if key != "type"}}
        if kind in {"indicator_condition", "financial_condition", "event_condition",
                    "holding_period_exit", "position_return_exit", "trailing_drawdown_exit", "minute_protection_exit"}:
            return {"op": "first_of", "children": [value]}
        return value

    @model_validator(mode="after")
    def position_aware_rules_are_unique(self) -> FirstOfExit:
        minute_rules = [child for child in self.children if isinstance(child, MinuteProtectionExit)]
        if minute_rules:
            if len(minute_rules) != 1 or self.op != "first_of":
                raise ValueError("分钟保护须为一个first_of退出节点，不能与日线条件同时满足")
        count = sum(isinstance(child, HoldingPeriodExit) for child in self.children)
        if count > 1:
            raise ValueError("exit may contain at most one holding-period rule")
        return_keys = [
            child.trigger for child in self.children if isinstance(child, PositionReturnExit)
        ]
        if len(return_keys) != len(set(return_keys)):
            raise ValueError("exit may contain at most one take-profit and one stop-loss rule")
        # PositionReturnExit fixes the same first-entry anchor and daily-close
        # observation, with strictly positive thresholds. One return cannot be
        # both >= a profit threshold and <= a negative loss threshold at once.
        if self.op == "all" and set(return_keys) == {"take_profit", "stop_loss"}:
            raise ValueError(
                "all exit cannot require both take-profit and stop-loss "
                "on the same entry-anchored closing return"
            )
        trailing_count = sum(isinstance(child, TrailingDrawdownExit) for child in self.children)
        if trailing_count > 1:
            raise ValueError("exit may contain at most one trailing-drawdown rule")
        return self


class HoldingPeriodExit(FrozenModel):
    """Exit at the open of the Nth A-share session after the first entry fill."""

    type: Literal["holding_period_exit"] = "holding_period_exit"
    sessions: int = Field(ge=1, le=10_000)
    anchor: Literal["first_entry_fill"] = "first_entry_fill"
    count_mode: Literal["subsequent_trading_sessions"] = "subsequent_trading_sessions"
    execution: Literal["target_session_open_proxy"] = "target_session_open_proxy"


class PositionReturnExit(FrozenModel):
    """Exit after a close-confirmed return from the first actual BUY fill."""

    type: Literal["position_return_exit"] = "position_return_exit"
    trigger: Literal["take_profit", "stop_loss"]
    threshold_pct: float = Field(gt=0, le=10_000)
    anchor: Literal["first_entry_fill"] = "first_entry_fill"
    observation: Literal["back_adjusted_daily_close"] = "back_adjusted_daily_close"
    evaluation_mode: Literal["bar_close_confirmed"] = "bar_close_confirmed"
    execution: Literal["next_tradable_session_open"] = "next_tradable_session_open"

    @model_validator(mode="after")
    def loss_threshold_is_bounded(self) -> PositionReturnExit:
        if not isfinite(self.threshold_pct):
            raise ValueError("position return threshold must be finite")
        if self.trigger == "stop_loss" and self.threshold_pct > 100:
            raise ValueError("stop-loss threshold cannot exceed 100 percent")
        return self


class TrailingDrawdownExit(FrozenModel):
    """Exit after a close-confirmed drawdown from the post-entry close peak."""

    type: Literal["trailing_drawdown_exit"] = "trailing_drawdown_exit"
    threshold_pct: float = Field(gt=0, le=100)
    anchor: Literal["first_entry_fill"] = "first_entry_fill"
    peak_basis: Literal["back_adjusted_daily_close"] = "back_adjusted_daily_close"
    evaluation_mode: Literal["bar_close_confirmed"] = "bar_close_confirmed"
    execution: Literal["next_tradable_session_open"] = "next_tradable_session_open"

    @field_validator("threshold_pct")
    @classmethod
    def threshold_is_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("trailing drawdown threshold must be finite")
        return value


class MinuteProtectionExit(FrozenModel):
    """Observe raw minute H/L against fee-exclusive weighted acquisition cost."""

    type: Literal["minute_protection_exit"] = "minute_protection_exit"
    take_profit_pct: Decimal | None = Field(default=None, gt=0, le=10_000, allow_inf_nan=False)
    stop_loss_pct: Decimal | None = Field(default=None, gt=0, lt=100, allow_inf_nan=False)
    trailing_drawdown_pct: Decimal | None = Field(default=None, gt=0, lt=100, allow_inf_nan=False)
    limit_price_cny: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    anchor: Literal["fee_exclusive_weighted_acquisition_cost"] = "fee_exclusive_weighted_acquisition_cost"
    observation: Literal["raw_minute_high_low"] = "raw_minute_high_low"
    execution: Literal["next_bar_order_activation"] = "next_bar_order_activation"

    @model_validator(mode="after")
    def has_protection(self) -> MinuteProtectionExit:
        if (self.take_profit_pct is None and self.stop_loss_pct is None
                and self.trailing_drawdown_pct is None):
            raise ValueError("分钟保护至少需要止盈或止损阈值")
        return self


type ExitRule = Annotated[
    IndicatorCondition
    | FinancialCondition
    | EventCondition
    | AllCondition
    | AnyCondition
    | NotCondition
    | HoldingPeriodExit
    | PositionReturnExit
    | MinuteProtectionExit
    | TrailingDrawdownExit,
    Field(discriminator="type"),
]


class DailyExecutionPolicy(FrozenModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    entry_policy: Literal["next_tradable_session_open"] = "next_tradable_session_open"
    exit_policy: Literal["next_tradable_session_open"] = "next_tradable_session_open"
    data_capability: Literal[
        "daily_ohlcv",
        "daily_ohlcv_events",
        "daily_ohlcv_financials",
        "daily_ohlcv_events_financials",
    ] = "daily_ohlcv"
    execution_resolution: Literal["1d"] = "1d"
    evaluation_frequency: Literal[
        "1d_close",
        "event_available_plus_1d_close",
        "financial_available_plus_1d_close",
        "event_financial_available_plus_1d_close",
    ] = "1d_close"
    position_policy: Literal[
        "single_position_no_pyramiding", "bounded_inventory", "accumulate_on_new_entry_signal",
    ] = "single_position_no_pyramiding"
    t_plus_one: Literal[True] = True


class PricePlanExecutionPolicy(FrozenModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    entry_policy: Literal["next_bar_order_activation", "scheduled_session_open", "scheduled_session_close", "server_selected_grid"]
    exit_policy: Literal["next_bar_order_activation", "scheduled_session_open", "scheduled_session_close", "server_selected_grid"]
    data_capability: Literal["minute_ohlcv", "daily_ohlcv", "server_selected"]
    execution_resolution: Literal["1m", "1d", "server_selected"]
    evaluation_frequency: Literal["1m_bar", "1d_close", "pre_session_schedule", "server_selected"]
    position_policy: Literal["bounded_inventory"] = "bounded_inventory"
    t_plus_one: Literal[True] = True


class HybridExecutionPolicy(FrozenModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    entry_policy: Literal["next_market_session_open"] = "next_market_session_open"
    exit_policy: Literal["daily_signal_open_or_next_minute_activation"] = "daily_signal_open_or_next_minute_activation"
    data_capability: Literal[
        "daily_and_minute_ohlcv", "daily_and_minute_ohlcv_events",
        "daily_and_minute_ohlcv_financials", "daily_and_minute_ohlcv_events_financials",
    ] = "daily_and_minute_ohlcv"
    execution_resolution: Literal["1m"] = "1m"
    evaluation_frequency: Literal["daily_close_and_minute_bar"] = "daily_close_and_minute_bar"
    position_policy: Literal["single_position_no_pyramiding", "accumulate_on_new_entry_signal"] = "single_position_no_pyramiding"
    t_plus_one: Literal[True] = True


class ComposedExecutionPolicy(FrozenModel):
    """Explicit declaration for independent signal and inventory-plan legs."""
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    entry_policy: Literal["composed_entry_leg"] = "composed_entry_leg"
    exit_policy: Literal["composed_exit_leg"] = "composed_exit_leg"
    data_capability: Literal[
        "daily_and_minute_ohlcv", "daily_and_minute_ohlcv_events",
        "daily_and_minute_ohlcv_financials", "daily_and_minute_ohlcv_events_financials",
    ] = "daily_and_minute_ohlcv"
    execution_resolution: Literal["1m"] = "1m"
    evaluation_frequency: Literal["daily_close_and_minute_bar"] = "daily_close_and_minute_bar"
    position_policy: Literal["bounded_inventory"] = "bounded_inventory"
    t_plus_one: Literal[True] = True


def execution_for_price_plan(plan: PricePlan) -> DailyExecutionPolicy | PricePlanExecutionPolicy:
    params = plan.parameters
    if plan.kind == "scheduled":
        policy = "scheduled_session_close" if params.at == "close" else "scheduled_session_open"
        if params.exit_rules:
            return PricePlanExecutionPolicy(entry_policy=policy, exit_policy="next_bar_order_activation",
                data_capability="minute_ohlcv", execution_resolution="1m", evaluation_frequency="1m_bar")
        return PricePlanExecutionPolicy(entry_policy=policy, exit_policy=policy,
            data_capability="daily_ohlcv", execution_resolution="1d", evaluation_frequency="pre_session_schedule")
    if params.observation == "minute_bar":
        return PricePlanExecutionPolicy(entry_policy="next_bar_order_activation", exit_policy="next_bar_order_activation",
            data_capability="minute_ohlcv", execution_resolution="1m", evaluation_frequency="1m_bar")
    if plan.kind == "grid" and params.observation is None:
        return PricePlanExecutionPolicy(entry_policy="server_selected_grid", exit_policy="server_selected_grid",
            data_capability="server_selected", execution_resolution="server_selected", evaluation_frequency="server_selected")
    return DailyExecutionPolicy(position_policy="bounded_inventory")


class BacktestConfig(FrozenModel):
    start: date
    end: date
    initial_cash_cny: int = Field(gt=0, le=1_000_000_000)

    @model_validator(mode="after")
    def period_is_ordered(self) -> BacktestConfig:
        if self.start > self.end:
            raise ValueError("backtest start must be on or before end")
        return self


class StrategySpec(FrozenModel):
    schema_version: Literal["strategy.v1"] = "strategy.v1"
    catalog: CatalogRef
    instrument: Instrument
    entry: Condition | None = None
    exit: FirstOfExit | None = None
    trading_plan: PricePlan | None = Field(default=None, exclude_if=lambda value: value is None)
    independent_plans: IndependentPlanPair | None = Field(default=None, exclude_if=lambda value: value is None)
    execution: DailyExecutionPolicy | PricePlanExecutionPolicy | HybridExecutionPolicy | ComposedExecutionPolicy = Field(default_factory=DailyExecutionPolicy)
    backtest: BacktestConfig

    @model_validator(mode="after")
    def expression_is_bounded(self) -> StrategySpec:
        if self.independent_plans is not None:
            if self.trading_plan is not None or self.entry is not None or self.exit is not None:
                raise ValueError('双计划与其他买卖规则不能重复声明所有权')
            if not isinstance(self.execution, ComposedExecutionPolicy):
                raise ValueError('双计划须显式声明组合执行')
            if self.backtest.initial_cash_cny != self.independent_plans.entry_plan.parameters.initial_cash_cny:
                raise ValueError('双计划与回测初始资金必须一致')
            return self
        plan = getattr(self, "trading_plan", None)
        composed = plan is not None and (self.entry is not None or self.exit is not None)
        if plan is not None:
            if self.backtest.initial_cash_cny != plan.parameters.initial_cash_cny:
                raise ValueError("交易计划与回测初始资金必须一致")
            # Read existing saved v1 records without changing their hashes;
            # newly compiled plans use the exact plan-derived declaration.
            if not composed and self.execution not in (DailyExecutionPolicy(position_policy="bounded_inventory"), execution_for_price_plan(plan)):
                raise ValueError("交易计划执行声明与实际计划不一致")
            if not composed:
                return self
        if composed and not isinstance(self.execution, ComposedExecutionPolicy):
            raise ValueError("独立买卖组合须显式声明组合执行，不能沿用整套计划的执行声明")
        if not composed and self.execution.position_policy not in {
            "single_position_no_pyramiding", "accumulate_on_new_entry_signal",
        }:
            raise ValueError("指标策略须声明不加仓或按新信号加仓")
        if not composed and (self.entry is None or self.exit is None):
            raise ValueError("指标策略须有完整买卖条件")
        exit_children = self.exit.children if self.exit is not None else ()
        exit_conditions = tuple(
            child
            for child in exit_children
            if not isinstance(
                child,
                (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit, MinuteProtectionExit),
            )
        )
        roots: tuple[Condition, ...] = (
            *((self.entry,) if self.entry is not None else ()), *exit_conditions,
        )
        node_count = (
            sum(_condition_size(root) for root in roots)
            + len(exit_children)
            - len(exit_conditions)
        )
        max_depth = max((_condition_depth(root) for root in roots), default=0)
        if node_count > 64:
            raise ValueError("strategy condition tree exceeds 64 nodes")
        if max_depth > 8:
            raise ValueError("strategy condition tree exceeds depth 8")
        has_events = any(_contains_event(root) for root in roots)
        has_financials = any(_contains_financial(root) for root in roots)
        expected_execution = (
            ("daily_ohlcv_events_financials", "event_financial_available_plus_1d_close")
            if has_events and has_financials
            else ("daily_ohlcv_events", "event_available_plus_1d_close")
            if has_events
            else ("daily_ohlcv_financials", "financial_available_plus_1d_close")
            if has_financials
            else ("daily_ohlcv", "1d_close")
        )
        has_minute = any(isinstance(child, MinuteProtectionExit) for child in exit_children)
        if composed:
            expected_execution = (expected_execution[0].replace("daily_ohlcv", "daily_and_minute_ohlcv"),
                                  "daily_close_and_minute_bar")
        elif has_minute:
            expected_execution = (expected_execution[0].replace("daily_ohlcv", "daily_and_minute_ohlcv"),
                                  "daily_close_and_minute_bar")
            if not isinstance(self.execution, HybridExecutionPolicy):
                raise ValueError("分钟保护须声明日线信号与分钟执行，不能回退日线")
        elif isinstance(self.execution, HybridExecutionPolicy):
            raise ValueError("混合执行声明须包含分钟保护规则")
        if (
            self.execution.data_capability,
            self.execution.evaluation_frequency,
        ) != expected_execution:
            raise ValueError(
                "strategy execution data declaration does not match its condition types; "
                f"expected {expected_execution[0]} / {expected_execution[1]}"
            )
        return self


def _children(condition: Condition) -> tuple[Condition, ...]:
    if isinstance(condition, (AllCondition, AnyCondition)):
        return condition.children
    if isinstance(condition, NotCondition):
        return (condition.child,)
    return ()


def _condition_size(condition: Condition) -> int:
    return 1 + sum(_condition_size(child) for child in _children(condition))


def _condition_depth(condition: Condition) -> int:
    children = _children(condition)
    return 1 if not children else 1 + max(_condition_depth(child) for child in children)


def _contains_event(condition: Condition) -> bool:
    return isinstance(condition, EventCondition) or any(
        _contains_event(child) for child in _children(condition)
    )


def _contains_financial(condition: Condition) -> bool:
    return isinstance(condition, FinancialCondition) or any(
        _contains_financial(child) for child in _children(condition)
    )


def iter_indicator_conditions(spec: StrategySpec) -> Iterator[IndicatorCondition]:
    """Yield indicator leaves in deterministic, document order."""

    def walk(condition: Condition) -> Iterator[IndicatorCondition]:
        if isinstance(condition, IndicatorCondition):
            yield condition
            return
        for child in _children(condition):
            yield from walk(child)

    if spec.entry is not None:
        yield from walk(spec.entry)
    for exit_condition in spec.exit.children if spec.exit is not None else ():
        if not isinstance(
            exit_condition,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit, MinuteProtectionExit),
        ):
            yield from walk(exit_condition)


def iter_event_conditions(spec: StrategySpec) -> Iterator[EventCondition]:
    """Yield event leaves in deterministic, document order."""

    def walk(condition: Condition) -> Iterator[EventCondition]:
        if isinstance(condition, EventCondition):
            yield condition
            return
        for child in _children(condition):
            yield from walk(child)

    if spec.entry is not None:
        yield from walk(spec.entry)
    for exit_condition in spec.exit.children if spec.exit is not None else ():
        if not isinstance(
            exit_condition,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit, MinuteProtectionExit),
        ):
            yield from walk(exit_condition)


def iter_financial_conditions(spec: StrategySpec) -> Iterator[FinancialCondition]:
    """Yield direct-provider financial leaves in deterministic document order."""

    def walk(condition: Condition) -> Iterator[FinancialCondition]:
        if isinstance(condition, FinancialCondition):
            yield condition
            return
        for child in _children(condition):
            yield from walk(child)

    if spec.entry is not None:
        yield from walk(spec.entry)
    for exit_condition in spec.exit.children if spec.exit is not None else ():
        if not isinstance(
            exit_condition,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit, MinuteProtectionExit),
        ):
            yield from walk(exit_condition)


def iter_holding_period_exits(spec: StrategySpec) -> Iterator[HoldingPeriodExit]:
    """Yield the bounded position-aware exit rules in document order."""

    for exit_rule in spec.exit.children if spec.exit else ():
        if isinstance(exit_rule, HoldingPeriodExit):
            yield exit_rule


def iter_position_return_exits(spec: StrategySpec) -> Iterator[PositionReturnExit]:
    for exit_rule in spec.exit.children if spec.exit else ():
        if isinstance(exit_rule, PositionReturnExit):
            yield exit_rule


def iter_trailing_drawdown_exits(spec: StrategySpec) -> Iterator[TrailingDrawdownExit]:
    for exit_rule in spec.exit.children if spec.exit else ():
        if isinstance(exit_rule, TrailingDrawdownExit):
            yield exit_rule


def strategy_requires_events(spec: StrategySpec) -> bool:
    return next(iter_event_conditions(spec), None) is not None


def strategy_requires_financials(spec: StrategySpec) -> bool:
    return next(iter_financial_conditions(spec), None) is not None


AllCondition.model_rebuild()
AnyCondition.model_rebuild()
NotCondition.model_rebuild()
FirstOfExit.model_rebuild()
StrategySpec.model_rebuild()
