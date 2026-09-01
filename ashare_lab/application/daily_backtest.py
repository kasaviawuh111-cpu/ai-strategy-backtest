"""The single authoritative daily strategy-to-ledger execution path."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from math import isfinite
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from ashare_lab.application.pre_open_sizing import (
    PRE_OPEN_BUY_SIZING_UPPER_LIMIT_UNAVAILABLE,
    size_pre_open_buy,
)
from ashare_lab.domain.analytics import (
    BacktestMetrics,
    EquityPoint,
    RoundTrip,
    calculate_metrics,
)
from ashare_lab.domain.execution import (
    CapacityMode,
    DailyBarMatchingModel,
    DailyBarMatchRequest,
    LimitHandling,
    MatchOutcome,
    MatchResult,
    PointInTimeVolume,
    previous_session_volume_proxy,
)
from ashare_lab.domain.financials import FinancialFactRecord
from ashare_lab.domain.market_data import (
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    EventEnvelope,
    InstrumentSession,
    PriceBasis,
    TradingStatus,
)
from ashare_lab.domain.orders import (
    Order,
    OrderEvent,
    OrderSide,
    OrderStateMachine,
    OrderStatus,
)
from ashare_lab.domain.portfolio import (
    FeeBreakdown,
    FillRecord,
    PortfolioState,
    UnsupportedCorporateActionError,
    accrue_corporate_action,
    apply_buy,
    apply_sell,
    capture_corporate_action_entitlement,
    decline_rights_issue,
    settle_corporate_action,
)
from ashare_lab.domain.runs import result_hash
from ashare_lab.domain.shared import (
    DecisionId,
    DomainValidationError,
    FillId,
    InstrumentId,
    Money,
    OrderEventId,
    OrderId,
    Price,
    Quantity,
)
from ashare_lab.domain.signals import SignalEvidence, SignalFact, SignalRuntime
from ashare_lab.domain.strategy.models import (
    AllCondition,
    AnyCondition,
    Condition,
    EventCondition,
    FinancialCondition,
    HoldingPeriodExit,
    IndicatorCondition,
    PositionReturnExit,
    StrategySpec,
    TrailingDrawdownExit,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DECISION_PROCESSING_LATENCY = timedelta(seconds=1)
OPENING_AUCTION_ACCEPT_AT = time(9, 15)
DAILY_OPENING_PRICE_PROXY_AT = time(9, 30)
OPENING_AUCTION_POLICY = (
    "cn.a_share.daily.published_open_proxy.latency_1s.cutoff_0915."
    "recorded_0930.not_exact.day_order.v2"
)
ENTRY_SIGNAL_VALIDITY_POLICY = (
    "cn.a_share.daily.entry_signal_validity.edge_event_state.composite_fail_closed."
    "event_revision_unavailable_one_attempt.retryable_day_orders.v3"
)
MAX_ENTRY_VALIDITY_SESSIONS = 20

_RETRYABLE_ENTRY_NO_FILL_REASONS = frozenset(
    {
        "adverse_price_limit",
        "daily_unlock_timing_unknown",
        "one_price_limit_up",
        "participation_capacity_zero",
        "point_in_time_volume_zero",
    }
)
_EDGE_TRIGGER_IDS = frozenset(
    {
        "bearish",
        "bullish",
        "new_high",
        "surge_down",
        "surge_up",
    }
)


class DailyBacktestInputError(DomainValidationError):
    """Raised when a run cannot be executed without inventing input facts."""


class FeeQuoteProvider(Protocol):
    def calculate(
        self,
        *,
        side: OrderSide,
        price: Price,
        quantity: Quantity,
        trade_date: date,
    ) -> FeeBreakdown: ...


class SessionCalendar(Protocol):
    def next_session(self, after: date) -> date: ...


class DecisionStatus(StrEnum):
    PENDING = "pending"
    RETRYING = "retrying"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    UNFILLED = "unfilled"
    NO_FUTURE_SESSION = "no_future_session"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EntrySignalSemantics(StrEnum):
    EDGE = "edge"
    EVENT = "event"
    STATE = "state"
    COMPOSITE = "composite"


@dataclass(frozen=True, slots=True)
class DailyBacktestConfig:
    participation_rate: Decimal = Decimal("0.05")
    slippage_bps: Decimal = Decimal("5")
    limit_handling: LimitHandling = LimitHandling.WAIT_FOR_UNLOCK
    allocation_ratio: Decimal = Decimal("1")
    retry_unfilled_exits: bool = True
    max_exit_attempts: int = 20
    capacity_mode: CapacityMode = CapacityMode.POINT_IN_TIME_VOLUME
    edge_entry_validity_sessions: int = 3
    event_entry_validity_sessions: int = 1
    state_entry_validity_sessions: int = 1

    def __post_init__(self) -> None:
        if not Decimal("0") < self.participation_rate <= Decimal("1"):
            raise DailyBacktestInputError("participation_rate must be in (0, 1]")
        if not Decimal("0") < self.allocation_ratio <= Decimal("1"):
            raise DailyBacktestInputError("allocation_ratio must be in (0, 1]")
        if self.slippage_bps < 0:
            raise DailyBacktestInputError("slippage_bps cannot be negative")
        if self.max_exit_attempts < 1:
            raise DailyBacktestInputError("max_exit_attempts must be positive")
        entry_validities = (
            self.edge_entry_validity_sessions,
            self.event_entry_validity_sessions,
            self.state_entry_validity_sessions,
        )
        if any(
            type(value) is not int or not 1 <= value <= MAX_ENTRY_VALIDITY_SESSIONS
            for value in entry_validities
        ):
            raise DailyBacktestInputError(
                f"entry signal validity sessions must be integers in [1, "
                f"{MAX_ENTRY_VALIDITY_SESSIONS}]"
            )
        if self.event_entry_validity_sessions != 1:
            raise DailyBacktestInputError(
                "event entry validity sessions must be 1 until point-in-time "
                "revision invalidation is supported"
            )
        if self.state_entry_validity_sessions != 1:
            raise DailyBacktestInputError(
                "state entry validity sessions must be 1 until point-in-time "
                "state revalidation is supported"
            )
        if type(self.capacity_mode) is not CapacityMode:
            raise DailyBacktestInputError("capacity_mode must be a CapacityMode")


@dataclass(frozen=True, slots=True)
class DailyBacktestInput:
    run_key: str
    strategy: StrategySpec
    bars: tuple[DailyBar, ...]
    sessions: tuple[InstrumentSession, ...]
    calendar: SessionCalendar
    fee_calculator: FeeQuoteProvider
    signal_bars: tuple[DailyBar, ...] | None = None
    events: tuple[EventEnvelope, ...] = ()
    financial_facts: tuple[FinancialFactRecord, ...] = ()
    corporate_actions: tuple[CorporateAction, ...] = ()
    config: DailyBacktestConfig = field(default_factory=DailyBacktestConfig)
    benchmark_equity: tuple[tuple[date, Decimal], ...] = ()
    benchmark_initial_equity: Decimal | None = None
    # A funded benchmark is only a buy-and-hold account when its own entry
    # order filled.  Its equity path cannot prove that fact (a flat path could
    # result from either a fill or no fill), so retain it as a run input.
    benchmark_entry_filled: bool | None = None
    # Compatibility-only input for callers that have not migrated to a funded
    # equity path. New production runs must use ``benchmark_equity`` plus the
    # explicit pre-entry ``benchmark_initial_equity``.
    benchmark_close: tuple[tuple[date, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class DailyStrategyDecision:
    decision_id: DecisionId
    side: OrderSide
    signal: SignalFact
    created_at: datetime
    status: DecisionStatus
    order_ids: tuple[OrderId, ...] = ()
    attempts: int = 0
    outcome_reason: str | None = None
    signal_semantics: EntrySignalSemantics | None = None
    signal_validity_sessions: int | None = None


@dataclass(frozen=True, slots=True)
class OrderTrace:
    order: Order
    events: tuple[OrderEvent, ...]
    match: MatchResult


@dataclass(frozen=True, slots=True)
class DailyBacktestResult:
    decisions: tuple[DailyStrategyDecision, ...]
    orders: tuple[OrderTrace, ...]
    fills: tuple[FillRecord, ...]
    signals: tuple[SignalFact, ...]
    equity_curve: tuple[EquityPoint, ...]
    round_trips: tuple[RoundTrip, ...]
    metrics: BacktestMetrics
    final_portfolio: PortfolioState
    benchmark_entry_filled: bool | None
    content_hash: str


@dataclass(slots=True)
class _PendingDecision:
    decision_index: int


@dataclass(slots=True)
class _IdSequence:
    run_key: str
    decision: int = 0
    order: int = 0
    fill: int = 0

    def next_decision(self) -> DecisionId:
        self.decision += 1
        return DecisionId(f"decision:{self.run_key}:{self.decision:06d}")

    def next_order(self) -> OrderId:
        self.order += 1
        return OrderId(f"order:{self.run_key}:{self.order:06d}")

    def next_fill(self) -> FillId:
        self.fill += 1
        return FillId(f"fill:{self.run_key}:{self.fill:06d}")

    def event(self, order_id: OrderId, sequence: int) -> OrderEventId:
        return OrderEventId(f"event:{order_id.value}:{sequence:02d}")


def run_daily_backtest(request: DailyBacktestInput) -> DailyBacktestResult:
    """Run Strategy DSL v1 through signals, orders, matching, lots and ledger."""

    bars, signal_bars, sessions, first_trade_index = _validate_and_select_inputs(request)
    instrument_id = InstrumentId(request.strategy.instrument.symbol)
    entry_timeline = SignalRuntime().evaluate_aligned(
        request.strategy.entry,
        signal_bars,
        request.events,
        request.financial_facts,
        execution_bars=bars,
    )
    market_exit_condition = _exit_condition(request.strategy)
    exit_timeline: tuple[SignalFact | None, ...] = (
        SignalRuntime().evaluate_aligned(
            market_exit_condition,
            signal_bars,
            request.events,
            request.financial_facts,
            execution_bars=bars,
        )
        if market_exit_condition is not None
        else (None,) * len(signal_bars)
    )
    holding_exit = _holding_period_exit(request.strategy)
    position_risk_exits = _position_risk_exits(request.strategy)
    session_by_date = {item.session_date: item for item in sessions}
    if request.benchmark_equity and request.benchmark_close:
        raise DailyBacktestInputError(
            "benchmark_equity and legacy benchmark_close cannot both be supplied"
        )
    benchmark_values = request.benchmark_equity or request.benchmark_close
    benchmark_by_date = _validate_benchmark_equity(
        benchmark_values,
        initial_equity=request.benchmark_initial_equity,
        funded=bool(request.benchmark_equity),
    )
    active_actions = _validate_corporate_actions(
        request.corporate_actions,
        bars=bars,
        first_trade_index=first_trade_index,
    )
    actions_by_record_date = _actions_by_date(active_actions, "record_date")
    actions_by_ex_date = _actions_by_date(active_actions, "ex_date")
    actions_by_settlement_date = _actions_by_settlement_session(active_actions, bars)

    portfolio: PortfolioState = PortfolioState(
        cash=Money(Decimal(request.strategy.backtest.initial_cash_cny), "CNY")
    )
    decisions: list[DailyStrategyDecision] = []
    order_traces: list[OrderTrace] = []
    triggered_signals: list[SignalFact] = []
    curve: list[EquityPoint] = []
    round_trips: list[RoundTrip] = []
    pending: _PendingDecision | None = None
    handled_entry_indices: set[int] = set()
    handled_exit_indices: set[int] = set()
    ids = _IdSequence(_safe_run_key(request.run_key))
    open_trade_cost = Money.zero("CNY")
    open_trade_proceeds = Money.zero("CNY")
    open_trade_action_income = Money.zero("CNY")
    open_trade_date: date | None = None
    holding_anchor_fill: FillRecord | None = None
    holding_exit_target_index: int | None = None
    holding_exit_emitted = False
    risk_anchor_fill: FillRecord | None = None
    risk_anchor_adjusted_price: Decimal | None = None
    risk_peak_adjusted_close: Decimal | None = None
    queued_risk_exit: SignalFact | None = None

    first_trade_bar = bars[first_trade_index]
    curve.append(
        EquityPoint(
            session_date=first_trade_bar.session_date - timedelta(days=1),
            equity=portfolio.cash.amount,
            benchmark=(
                request.benchmark_initial_equity
                if request.benchmark_initial_equity is not None
                else benchmark_by_date.get(first_trade_bar.session_date)
            ),
        )
    )

    for index in range(first_trade_index, len(bars)):
        bar = bars[index]
        session = session_by_date[bar.session_date]
        auction_accept_at = datetime.combine(
            bar.session_date,
            OPENING_AUCTION_ACCEPT_AT,
            tzinfo=SHANGHAI,
        )
        opening_price_proxy_at = datetime.combine(
            bar.session_date,
            DAILY_OPENING_PRICE_PROXY_AT,
            tzinfo=SHANGHAI,
        )
        decision_cutoff = auction_accept_at - DECISION_PROCESSING_LATENCY
        preopen = opening_price_proxy_at - timedelta(microseconds=1)
        entry_fact = entry_timeline[index]
        exit_fact = exit_timeline[index]
        holding_exit_fact: SignalFact | None = None
        if (
            holding_exit is not None
            and holding_anchor_fill is not None
            and holding_exit_target_index == index
            and not holding_exit_emitted
        ):
            holding_exit_fact = _holding_period_signal(
                rule=holding_exit,
                anchor_fill=holding_anchor_fill,
                target_session_date=bar.session_date,
                available_at=decision_cutoff,
            )
            holding_exit_emitted = True
            triggered_signals.append(holding_exit_fact)

        try:
            for action in actions_by_ex_date.get(bar.session_date, ()):
                if action.action_type is CorporateActionKind.RIGHTS_ISSUE:
                    continue
                receivable_before = portfolio.dividend_receivable(instrument_id)
                portfolio = accrue_corporate_action(
                    portfolio,
                    action,
                    accrued_at=preopen,
                )
                receivable_delta = portfolio.dividend_receivable(instrument_id) - receivable_before
                if open_trade_date is not None:
                    open_trade_action_income += receivable_delta
            for action in actions_by_settlement_date.get(bar.session_date, ()):
                if action.action_type is CorporateActionKind.CASH_DIVIDEND:
                    continue
                portfolio = settle_corporate_action(
                    portfolio,
                    action,
                    settled_at=preopen,
                )
        except UnsupportedCorporateActionError as exc:
            raise DailyBacktestInputError(str(exc)) from exc

        available_exit_for_cancellation: SignalFact | None = None
        if pending is None:
            position_at_open = _economic_position_quantity(portfolio, instrument_id)
            available_entry = _consume_latest_available_signal(
                entry_timeline,
                handled_entry_indices,
                start_index=first_trade_index,
                up_to_index=index,
                cutoff=decision_cutoff,
                inclusive=True,
            )
            available_exit = _consume_latest_available_signal(
                exit_timeline,
                handled_exit_indices,
                start_index=first_trade_index,
                up_to_index=index,
                cutoff=decision_cutoff,
                inclusive=True,
            )
            open_fact = (
                available_entry
                if position_at_open == 0
                else _first_available_exit(
                    available_exit,
                    holding_exit_fact,
                    queued_risk_exit,
                )
            )
            if open_fact is not None:
                side = OrderSide.BUY if position_at_open == 0 else OrderSide.SELL
                pending = _new_decision(
                    decisions,
                    ids,
                    side,
                    open_fact,
                    config=request.config,
                    entry_condition=request.strategy.entry,
                )
                if open_fact is queued_risk_exit:
                    queued_risk_exit = None
                if side is OrderSide.BUY:
                    available_exit_for_cancellation = available_exit

        if pending is not None:
            assert isinstance(pending, _PendingDecision)
            decision = decisions[pending.decision_index]
            if decision.side is OrderSide.BUY and available_exit_for_cancellation is None:
                available_exit_for_cancellation = _consume_latest_available_signal(
                    exit_timeline,
                    handled_exit_indices,
                    start_index=first_trade_index,
                    up_to_index=index,
                    cutoff=decision_cutoff,
                    inclusive=True,
                )
            if (
                decision.side is OrderSide.BUY
                and available_exit_for_cancellation is not None
                and _signal_is_not_older(
                    available_exit_for_cancellation,
                    than=decision.signal,
                )
            ):
                decisions[pending.decision_index] = replace(
                    decision,
                    status=DecisionStatus.CANCELLED,
                    outcome_reason=(
                        "opposite_exit_signal_invalidated_entry:"
                        f"{available_exit_for_cancellation.condition_ref}"
                    ),
                )
                pending = None

        if pending is not None:
            assert isinstance(pending, _PendingDecision)
            decision = decisions[pending.decision_index]
            if (
                decision.signal.available_at + DECISION_PROCESSING_LATENCY <= auction_accept_at
                and session.status is TradingStatus.TRADING
            ):
                attempt_result: tuple[
                    PortfolioState,
                    DailyStrategyDecision,
                    OrderTrace | None,
                    FillRecord | None,
                    bool,
                ] = _attempt_decision(
                    request=request,
                    decision=decision,
                    bar=bar,
                    session=session,
                    portfolio=portfolio,
                    ids=ids,
                    instrument_id=instrument_id,
                    point_in_time_volume=(
                        previous_session_volume_proxy(bars[index - 1])
                        if request.config.capacity_mode is CapacityMode.POINT_IN_TIME_VOLUME
                        and index > 0
                        else None
                    ),
                )
                portfolio = attempt_result[0]
                decision = attempt_result[1]
                trace = attempt_result[2]
                fill = attempt_result[3]
                should_retry = attempt_result[4]
                decisions[pending.decision_index] = decision
                if trace is not None:
                    order_traces.append(trace)
                if fill is not None:
                    if fill.side is OrderSide.BUY:
                        if open_trade_date is None:
                            open_trade_date = fill.trading_date
                        if holding_exit is not None and holding_anchor_fill is None:
                            holding_anchor_fill = fill
                            holding_exit_target_index = index + holding_exit.sessions
                            holding_exit_emitted = False
                        if position_risk_exits and risk_anchor_fill is None:
                            risk_anchor_fill = fill
                            execution_open = bar.open.amount
                            signal_open = signal_bars[index].open.amount
                            if execution_open <= 0 or signal_open <= 0:
                                raise DailyBacktestInputError(
                                    "risk exit requires positive aligned execution and signal opens"
                                )
                            risk_anchor_adjusted_price = (
                                fill.price.amount * signal_open / execution_open
                            )
                            risk_peak_adjusted_close = max(
                                risk_anchor_adjusted_price,
                                signal_bars[index].close.amount,
                            )
                        open_trade_cost += fill.gross_amount + fill.fees.total
                    else:
                        open_trade_proceeds += fill.gross_amount - fill.fees.total
                        if _economic_position_quantity(portfolio, instrument_id) == 0:
                            if open_trade_date is not None:
                                round_trips.append(
                                    RoundTrip(
                                        entry_date=open_trade_date,
                                        exit_date=fill.trading_date,
                                        net_pnl=(
                                            open_trade_proceeds
                                            + open_trade_action_income
                                            - open_trade_cost
                                        ).amount,
                                    )
                                )
                            open_trade_cost = Money.zero("CNY")
                            open_trade_proceeds = Money.zero("CNY")
                            open_trade_action_income = Money.zero("CNY")
                            open_trade_date = None
                            holding_anchor_fill = None
                            holding_exit_target_index = None
                            holding_exit_emitted = False
                            risk_anchor_fill = None
                            risk_anchor_adjusted_price = None
                            risk_peak_adjusted_close = None
                            queued_risk_exit = None
                if not should_retry:
                    pending = None

        position_quantity = portfolio.position_quantity(instrument_id).value
        pending_share_delta = portfolio.pending_share_delta(instrument_id)
        dividend_receivable = portfolio.dividend_receivable(instrument_id).amount
        curve.append(
            EquityPoint(
                session_date=bar.session_date,
                equity=(
                    portfolio.cash.amount
                    + dividend_receivable
                    + Decimal(position_quantity + pending_share_delta) * bar.close.amount
                ),
                benchmark=benchmark_by_date.get(bar.session_date),
            )
        )

        if entry_fact is not None and entry_fact.triggered:
            triggered_signals.append(entry_fact)
        if exit_fact is not None and exit_fact.triggered:
            triggered_signals.append(exit_fact)

        try:
            for action in actions_by_record_date.get(bar.session_date, ()):
                if action.action_type is CorporateActionKind.RIGHTS_ISSUE:
                    portfolio = decline_rights_issue(
                        portfolio,
                        action,
                        declined_at=_session_close(bar.session_date),
                    )
                    continue
                portfolio = capture_corporate_action_entitlement(
                    portfolio,
                    action,
                    captured_at=_session_close(bar.session_date),
                )
            for action in actions_by_settlement_date.get(bar.session_date, ()):
                if action.action_type is not CorporateActionKind.CASH_DIVIDEND:
                    continue
                portfolio = settle_corporate_action(
                    portfolio,
                    action,
                    settled_at=_session_close(bar.session_date),
                )
        except UnsupportedCorporateActionError as exc:
            raise DailyBacktestInputError(str(exc)) from exc

        if (
            position_risk_exits
            and queued_risk_exit is None
            and pending is None
            and _economic_position_quantity(portfolio, instrument_id) > 0
            and risk_anchor_fill is not None
            and risk_anchor_adjusted_price is not None
            and risk_peak_adjusted_close is not None
        ):
            assert isinstance(risk_anchor_fill, FillRecord)
            assert isinstance(risk_anchor_adjusted_price, Decimal)
            assert isinstance(risk_peak_adjusted_close, Decimal)
            queued_risk_exit, risk_peak_adjusted_close = _position_risk_signal(
                rules=position_risk_exits,
                anchor_fill=risk_anchor_fill,
                anchor_adjusted_price=risk_anchor_adjusted_price,
                previous_peak_adjusted_close=risk_peak_adjusted_close,
                signal_bar=signal_bars[index],
            )
            if queued_risk_exit is not None:
                triggered_signals.append(queued_risk_exit)

    if pending is not None:
        assert isinstance(pending, _PendingDecision)
        item = decisions[pending.decision_index]
        decisions[pending.decision_index] = replace(
            item,
            status=DecisionStatus.NO_FUTURE_SESSION,
            outcome_reason="no_future_session_in_backtest_period",
        )
    else:
        final_quantity = _economic_position_quantity(portfolio, instrument_id)
        final_cutoff = max(bars[-1].available_at, _session_close(bars[-1].session_date))
        final_entry = _consume_latest_available_signal(
            entry_timeline,
            handled_entry_indices,
            start_index=first_trade_index,
            up_to_index=len(bars) - 1,
            cutoff=final_cutoff,
            inclusive=True,
        )
        final_exit = _consume_latest_available_signal(
            exit_timeline,
            handled_exit_indices,
            start_index=first_trade_index,
            up_to_index=len(bars) - 1,
            cutoff=final_cutoff,
            inclusive=True,
        )
        final_fact = (
            final_entry
            if final_quantity == 0
            else _first_available_exit(final_exit, queued_risk_exit)
        )
        if final_fact is not None:
            side = OrderSide.BUY if final_quantity == 0 else OrderSide.SELL
            pending = _new_decision(
                decisions,
                ids,
                side,
                final_fact,
                config=request.config,
                entry_condition=request.strategy.entry,
            )
            item = decisions[pending.decision_index]
            decisions[pending.decision_index] = replace(
                item,
                status=DecisionStatus.NO_FUTURE_SESSION,
                outcome_reason="no_future_session_in_backtest_period",
            )

    metrics = calculate_metrics(tuple(curve), tuple(round_trips))
    payload = _result_payload(
        decisions=decisions,
        order_traces=order_traces,
        fills=portfolio.fills,
        signals=triggered_signals,
        curve=curve,
        round_trips=round_trips,
        metrics=metrics,
        final_portfolio=portfolio,
        benchmark_entry_filled=request.benchmark_entry_filled,
    )
    return DailyBacktestResult(
        decisions=tuple(decisions),
        orders=tuple(order_traces),
        fills=portfolio.fills,
        signals=tuple(triggered_signals),
        equity_curve=tuple(curve),
        round_trips=tuple(round_trips),
        metrics=metrics,
        final_portfolio=portfolio,
        benchmark_entry_filled=request.benchmark_entry_filled,
        content_hash=result_hash(payload),
    )


def _attempt_decision(
    *,
    request: DailyBacktestInput,
    decision: DailyStrategyDecision,
    bar: DailyBar,
    session: InstrumentSession,
    portfolio: PortfolioState,
    ids: _IdSequence,
    instrument_id: InstrumentId,
    point_in_time_volume: PointInTimeVolume | None,
) -> tuple[
    PortfolioState,
    DailyStrategyDecision,
    OrderTrace | None,
    FillRecord | None,
    bool,
]:
    auction_accept_at = datetime.combine(
        bar.session_date,
        OPENING_AUCTION_ACCEPT_AT,
        tzinfo=SHANGHAI,
    )
    open_at = datetime.combine(
        bar.session_date,
        DAILY_OPENING_PRICE_PROXY_AT,
        tzinfo=SHANGHAI,
    )
    close_at = datetime.combine(bar.session_date, time(15), tzinfo=SHANGHAI)
    submitted_at = max(
        decision.created_at + DECISION_PROCESSING_LATENCY,
        auction_accept_at,
    )
    # Every attempt is a distinct DAY order created for that session.  The
    # decision retains the original signal time; a retry must not pretend that
    # its order existed continuously since the signal was confirmed.
    created_at = submitted_at
    if decision.side is OrderSide.BUY:
        sizing = size_pre_open_buy(
            cash=portfolio.cash,
            session=session,
            allocation_ratio=request.config.allocation_ratio,
            fee_calculator=request.fee_calculator,
            trading_date=bar.session_date,
        )
        if sizing is None:
            return (
                portfolio,
                replace(
                    decision,
                    status=DecisionStatus.REJECTED,
                    outcome_reason=PRE_OPEN_BUY_SIZING_UPPER_LIMIT_UNAVAILABLE,
                ),
                None,
                None,
                False,
            )
        quantity = sizing.quantity
        limit_price = sizing.affordability_price
    else:
        expected_price = DailyBarMatchingModel.price_with_slippage(
            bar.open,
            side=decision.side,
            slippage_bps=request.config.slippage_bps,
            tick=session.price_tick,
        )
        quantity = portfolio.sellable_quantity(instrument_id, bar.session_date)
        if quantity.value == 0:
            return portfolio, decision, None, None, True
        limit_price = session.lower_limit or expected_price
    if quantity.value == 0:
        return (
            portfolio,
            replace(
                decision,
                status=DecisionStatus.REJECTED,
                outcome_reason="insufficient_cash_for_minimum_buy_quantity",
            ),
            None,
            None,
            False,
        )

    order_id = ids.next_order()
    events: list[OrderEvent] = []
    created = OrderStateMachine.create(
        order_id=order_id,
        event_id=ids.event(order_id, 1),
        decision_id=decision.decision_id,
        instrument_id=instrument_id,
        side=decision.side,
        quantity=quantity,
        limit_price=limit_price,
        created_at=created_at,
        valid_from=open_at,
        valid_until=close_at,
    )
    events.append(created.event)
    submitted = OrderStateMachine.submit(
        created.order,
        event_id=ids.event(order_id, 2),
        submitted_at=submitted_at,
    )
    events.append(submitted.event)
    accepted = OrderStateMachine.accept(
        submitted.order,
        event_id=ids.event(order_id, 3),
        accepted_at=submitted_at,
        fill_eligible_at=open_at,
    )
    events.append(accepted.event)
    match = DailyBarMatchingModel.match(
        DailyBarMatchRequest(
            order=accepted.order,
            bar=bar,
            session=session,
            opening_price_proxy_at=open_at,
            participation_rate=request.config.participation_rate,
            slippage_bps=request.config.slippage_bps,
            limit_handling=request.config.limit_handling,
            capacity_mode=request.config.capacity_mode,
            point_in_time_volume=point_in_time_volume,
        )
    )
    final_order = accepted.order
    fill_record = None
    if match.outcome is not MatchOutcome.NO_FILL:
        assert match.price is not None and match.filled_at is not None
        fill_id = ids.next_fill()
        filled = OrderStateMachine.record_fill(
            final_order,
            event_id=ids.event(order_id, 4),
            fill_id=fill_id,
            filled_at=match.filled_at,
            quantity=match.quantity,
            price=match.price,
        )
        events.append(filled.event)
        final_order = filled.order
        fees = request.fee_calculator.calculate(
            side=decision.side,
            price=match.price,
            quantity=match.quantity,
            trade_date=bar.session_date,
        )
        fill_record = FillRecord(
            fill_id=fill_id,
            order_id=order_id,
            instrument_id=instrument_id,
            side=decision.side,
            quantity=match.quantity,
            price=match.price,
            filled_at=match.filled_at,
            fees=fees,
        )
        if decision.side is OrderSide.BUY:
            try:
                sellable_on = request.calendar.next_session(fill_record.trading_date)
            except (KeyError, ValueError) as exc:
                raise DailyBacktestInputError(
                    "calendar must include the first session after every possible buy fill"
                ) from exc
            portfolio = apply_buy(portfolio, fill_record, sellable_on=sellable_on)
        else:
            portfolio = apply_sell(portfolio, fill_record)

    if final_order.status is not OrderStatus.FILLED:
        expired = OrderStateMachine.expire(
            final_order,
            event_id=ids.event(order_id, final_order.version + 1),
            expired_at=close_at,
            reason_code=(
                "day_remainder_expired"
                if match.outcome is not MatchOutcome.NO_FILL
                else match.reason_code
            ),
        )
        events.append(expired.event)
        final_order = expired.order

    attempts = decision.attempts + 1
    still_positioned = _economic_position_quantity(portfolio, instrument_id) > 0
    should_retry_exit = (
        decision.side is OrderSide.SELL
        and still_positioned
        and request.config.retry_unfilled_exits
        and attempts < request.config.max_exit_attempts
    )
    should_retry_entry = (
        decision.side is OrderSide.BUY
        and fill_record is None
        and match.outcome is MatchOutcome.NO_FILL
        and match.reason_code in _RETRYABLE_ENTRY_NO_FILL_REASONS
        and decision.signal_validity_sessions is not None
        and attempts < decision.signal_validity_sessions
    )
    should_retry = should_retry_exit or should_retry_entry
    if should_retry:
        status = (
            DecisionStatus.PARTIALLY_FILLED if fill_record is not None else DecisionStatus.RETRYING
        )
    elif fill_record is None:
        status = DecisionStatus.UNFILLED
    elif final_order.filled_quantity == final_order.quantity:
        status = DecisionStatus.FILLED
    else:
        status = DecisionStatus.PARTIALLY_FILLED
    updated_decision = replace(
        decision,
        status=status,
        order_ids=(*decision.order_ids, order_id),
        attempts=attempts,
        outcome_reason=(
            "retry_exit_remaining_position"
            if should_retry_exit
            else (
                f"retry_entry_after:{match.reason_code}"
                if should_retry_entry
                else match.reason_code
            )
        ),
    )
    return (
        portfolio,
        updated_decision,
        OrderTrace(order=final_order, events=tuple(events), match=match),
        fill_record,
        should_retry,
    )


def _new_decision(
    decisions: list[DailyStrategyDecision],
    ids: _IdSequence,
    side: OrderSide,
    signal: SignalFact,
    *,
    config: DailyBacktestConfig,
    entry_condition: Condition,
) -> _PendingDecision:
    semantics = _entry_signal_semantics(entry_condition) if side is OrderSide.BUY else None
    item = DailyStrategyDecision(
        decision_id=ids.next_decision(),
        side=side,
        signal=signal,
        created_at=signal.available_at,
        status=DecisionStatus.PENDING,
        signal_semantics=semantics,
        signal_validity_sessions=(
            _entry_validity_sessions(config, semantics) if semantics is not None else None
        ),
    )
    decisions.append(item)
    return _PendingDecision(decision_index=len(decisions) - 1)


def _entry_signal_semantics(condition: Condition) -> EntrySignalSemantics:
    """Classify entry persistence conservatively from the executable DSL tree."""

    if isinstance(condition, EventCondition):
        return EntrySignalSemantics.EVENT
    if isinstance(condition, FinancialCondition):
        return EntrySignalSemantics.STATE
    if isinstance(condition, IndicatorCondition):
        if "cross" in condition.trigger or condition.trigger in _EDGE_TRIGGER_IDS:
            return EntrySignalSemantics.EDGE
        return EntrySignalSemantics.STATE
    # A composite fact can mix one-shot events or edges with a state that may
    # stop being true on the following session.  The daily runtime currently
    # retains only the combined fact, so it cannot safely prove every child
    # again before creating a later DAY order.  Fail closed by allowing only
    # the first attempt until child-level revalidation is part of the replay.
    if isinstance(condition, (AllCondition, AnyCondition)) or hasattr(condition, "child"):
        return EntrySignalSemantics.COMPOSITE
    raise DailyBacktestInputError("unsupported entry condition semantics")


def _entry_validity_sessions(
    config: DailyBacktestConfig,
    semantics: EntrySignalSemantics,
) -> int:
    return {
        EntrySignalSemantics.EDGE: config.edge_entry_validity_sessions,
        EntrySignalSemantics.EVENT: 1,
        EntrySignalSemantics.STATE: config.state_entry_validity_sessions,
        EntrySignalSemantics.COMPOSITE: 1,
    }[semantics]


def _signal_is_not_older(signal: SignalFact, *, than: SignalFact) -> bool:
    # A delayed observation about an older session must not override a newer
    # market state merely because the old record arrived later.
    return (
        signal.session_date,
        signal.observed_at,
        signal.available_at,
    ) >= (
        than.session_date,
        than.observed_at,
        than.available_at,
    )


def _consume_latest_available_signal(
    timeline: tuple[SignalFact | None, ...],
    handled_indices: set[int],
    *,
    start_index: int,
    up_to_index: int,
    cutoff: datetime,
    inclusive: bool = False,
) -> SignalFact | None:
    """Consume facts in availability order so a late old bar cannot block newer facts."""

    candidates: list[tuple[int, SignalFact]] = []
    for index in range(start_index, up_to_index + 1):
        if index in handled_indices:
            continue
        fact = timeline[index]
        if fact is None or not fact.triggered:
            continue
        is_available = fact.available_at <= cutoff if inclusive else fact.available_at < cutoff
        if not is_available:
            continue
        handled_indices.add(index)
        candidates.append((index, fact))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item[1].session_date,
            item[1].available_at,
            item[1].observed_at,
            item[0],
        ),
    )[1]


def _session_close(day: date) -> datetime:
    return datetime.combine(day, time(15), tzinfo=SHANGHAI)


def _validate_benchmark_equity(
    values: tuple[tuple[date, Decimal], ...],
    *,
    initial_equity: Decimal | None,
    funded: bool,
) -> dict[date, Decimal]:
    if funded and initial_equity is None:
        raise DailyBacktestInputError(
            "funded benchmark requires explicit pre-entry benchmark_initial_equity"
        )
    if initial_equity is not None and (not initial_equity.is_finite() or initial_equity <= 0):
        raise DailyBacktestInputError("benchmark_initial_equity must be finite and positive")
    dates = tuple(day for day, _ in values)
    if len(dates) != len(set(dates)) or any(left >= right for left, right in pairwise(dates)):
        raise DailyBacktestInputError("benchmark equity dates must be unique and ordered")
    if any(not value.is_finite() or value < 0 for _, value in values):
        raise DailyBacktestInputError("benchmark equity values must be finite and non-negative")
    return dict(values)


def _validate_corporate_actions(
    actions: tuple[CorporateAction, ...],
    *,
    bars: tuple[DailyBar, ...],
    first_trade_index: int,
) -> tuple[CorporateAction, ...]:
    instrument_id = bars[0].instrument_id
    first_trade_date = bars[first_trade_index].session_date
    final_date = bars[-1].session_date
    bar_index = {bar.session_date: index for index, bar in enumerate(bars)}
    selected: list[CorporateAction] = []
    keys: set[tuple[str, int]] = set()
    source_leg_keys: set[tuple[str, CorporateActionKind]] = set()
    for action in actions:
        if action.instrument_id != instrument_id:
            raise DailyBacktestInputError(
                "all corporate actions must match the strategy instrument"
            )
        key = (action.action_id.value, action.revision_no)
        source_leg_key = (action.source_action_id, action.action_type)
        if key in keys or source_leg_key in source_leg_keys:
            raise DailyBacktestInputError(
                "corporate actions must contain one selected revision per source action leg"
            )
        keys.add(key)
        source_leg_keys.add(source_leg_key)
        if action.record_date < first_trade_date or action.record_date > final_date:
            # The portfolio starts in cash at first_trade_date, so an earlier
            # record date cannot create an entitlement for this run.
            continue
        if action.available_at is None:
            raise DailyBacktestInputError(
                "corporate action revision is not validated for point-in-time replay"
            )
        if action.record_date not in bar_index:
            raise DailyBacktestInputError("corporate-action record date has no pinned session")
        if action.available_at > _session_close(action.record_date):
            raise DailyBacktestInputError(
                "corporate-action terms were not validated by record-date close"
            )
        if action.ex_date <= final_date:
            if action.ex_date not in bar_index:
                raise DailyBacktestInputError("corporate-action ex date has no pinned session")
            if bar_index[action.ex_date] != bar_index[action.record_date] + 1:
                raise DailyBacktestInputError(
                    "corporate-action record date must be the session immediately before ex date"
                )
        selected.append(action)
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.record_date,
                item.ex_date,
                item.action_id.value,
                item.revision_no,
            ),
        )
    )


def _actions_by_date(
    actions: tuple[CorporateAction, ...],
    field_name: Literal["record_date", "ex_date"],
) -> dict[date, tuple[CorporateAction, ...]]:
    grouped: dict[date, list[CorporateAction]] = {}
    for action in actions:
        day = action.record_date if field_name == "record_date" else action.ex_date
        grouped.setdefault(day, []).append(action)
    return {day: tuple(values) for day, values in grouped.items()}


def _actions_by_settlement_session(
    actions: tuple[CorporateAction, ...],
    bars: tuple[DailyBar, ...],
) -> dict[date, tuple[CorporateAction, ...]]:
    grouped: dict[date, list[CorporateAction]] = {}
    session_dates = tuple(bar.session_date for bar in bars)
    for action in actions:
        if action.action_type is CorporateActionKind.RIGHTS_ISSUE:
            continue
        settlement_date = _corporate_action_settlement_date(action)
        session_date = next((day for day in session_dates if day >= settlement_date), None)
        if session_date is not None:
            grouped.setdefault(session_date, []).append(action)
    return {day: tuple(values) for day, values in grouped.items()}


def _corporate_action_settlement_date(action: CorporateAction) -> date:
    if action.action_type is CorporateActionKind.CASH_DIVIDEND:
        assert action.cash_pay_date is not None
        return action.cash_pay_date
    if action.action_type in {
        CorporateActionKind.SHARE_DISTRIBUTION,
        CorporateActionKind.STOCK_SPLIT,
        CorporateActionKind.REVERSE_SPLIT,
    }:
        assert action.share_credit_date is not None
        return action.share_credit_date
    raise DailyBacktestInputError("rights issues do not create a settlement under decline policy")


def _economic_position_quantity(
    portfolio: PortfolioState,
    instrument_id: InstrumentId,
) -> int:
    value = portfolio.position_quantity(instrument_id).value + portfolio.pending_share_delta(
        instrument_id
    )
    if value < 0:
        raise DailyBacktestInputError("corporate action produced negative economic shares")
    return value


def _exit_condition(strategy: StrategySpec) -> Condition | None:
    """Return only market/event rules that the pure signal runtime can evaluate."""

    children = tuple(
        child
        for child in strategy.exit.children
        if not isinstance(
            child,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
        )
    )
    if not children:
        return None
    return children[0] if len(children) == 1 else AnyCondition(children=children)


def _holding_period_exit(strategy: StrategySpec) -> HoldingPeriodExit | None:
    return next(
        (child for child in strategy.exit.children if isinstance(child, HoldingPeriodExit)),
        None,
    )


def _position_risk_exits(
    strategy: StrategySpec,
) -> tuple[PositionReturnExit | TrailingDrawdownExit, ...]:
    return tuple(
        child
        for child in strategy.exit.children
        if isinstance(child, (PositionReturnExit, TrailingDrawdownExit))
    )


def _position_risk_signal(
    *,
    rules: tuple[PositionReturnExit | TrailingDrawdownExit, ...],
    anchor_fill: FillRecord,
    anchor_adjusted_price: Decimal,
    previous_peak_adjusted_close: Decimal,
    signal_bar: DailyBar,
) -> tuple[SignalFact | None, Decimal]:
    """Evaluate close-confirmed position exits without using the signal-bar close to fill."""

    if signal_bar.price_basis is not PriceBasis.BACK_ADJUSTED:
        raise DailyBacktestInputError("position risk exits require back-adjusted signal bars")
    if signal_bar.volume.value == 0:
        return None, previous_peak_adjusted_close
    close = signal_bar.close.amount
    if anchor_adjusted_price <= 0 or close <= 0:
        raise DailyBacktestInputError("position risk exit prices must be positive")
    peak = max(previous_peak_adjusted_close, close)
    return_pct = (close / anchor_adjusted_price - Decimal("1")) * Decimal("100")
    drawdown_pct = (close / peak - Decimal("1")) * Decimal("100")
    observed_at = _session_close(signal_bar.session_date)
    available_at = max(signal_bar.available_at, observed_at)
    for rule in rules:
        threshold = Decimal(str(rule.threshold_pct))
        if isinstance(rule, PositionReturnExit):
            triggered = (
                return_pct >= threshold
                if rule.trigger == "take_profit"
                else return_pct <= -threshold
            )
            if not triggered:
                continue
            condition_ref = f"position_return_exit:{rule.trigger}:{threshold}pct"
            reason = (
                f"{rule.trigger} confirmed at daily close: adjusted return "
                f"{return_pct}% versus threshold {threshold}% from first BUY fill "
                f"{anchor_fill.fill_id.value}; earliest execution is next tradable open"
            )
            left_value = return_pct
            right_value = threshold if rule.trigger == "take_profit" else -threshold
        else:
            if drawdown_pct > -threshold:
                continue
            condition_ref = f"trailing_drawdown_exit:{threshold}pct"
            reason = (
                f"trailing drawdown confirmed at daily close: {drawdown_pct}% versus "
                f"-{threshold}% from post-entry adjusted close peak; earliest execution "
                "is next tradable open"
            )
            left_value = drawdown_pct
            right_value = -threshold
        return (
            SignalFact(
                instrument_id=anchor_fill.instrument_id,
                session_date=signal_bar.session_date,
                condition_ref=condition_ref,
                triggered=True,
                observed_at=observed_at,
                available_at=available_at,
                reason=reason,
                left_value=left_value,
                right_value=right_value,
                evidence=(
                    SignalEvidence(
                        evidence_type="entry_fill_anchor",
                        evidence_id=anchor_fill.fill_id.value,
                        available_at=anchor_fill.filled_at,
                        provider="backtest_ledger",
                        validation_status="validated",
                    ),
                ),
            ),
            peak,
        )
    return None, peak


def _holding_period_signal(
    *,
    rule: HoldingPeriodExit,
    anchor_fill: FillRecord,
    target_session_date: date,
    available_at: datetime,
) -> SignalFact:
    """Materialize the auditable fact when the scheduled target session begins.

    The target is anchored to the first actual BUY fill.  It is not emitted at
    the fill itself because that would make the rule executable before the Nth
    subsequent pinned session.  ``available_at`` is the target session's
    decision cutoff; execution remains the published daily open proxy.
    """

    return SignalFact(
        instrument_id=anchor_fill.instrument_id,
        session_date=target_session_date,
        condition_ref=(
            f"holding_period_exit:{rule.sessions}:first_entry_fill:subsequent_trading_sessions"
        ),
        triggered=True,
        observed_at=available_at,
        available_at=available_at,
        reason=(
            f"first BUY fill {anchor_fill.fill_id.value} on "
            f"{anchor_fill.trading_date.isoformat()}; target is subsequent "
            f"pinned A-share session {rule.sessions} on "
            f"{target_session_date.isoformat()}, using daily open proxy"
        ),
        left_value=Decimal(rule.sessions),
        right_value=Decimal(rule.sessions),
        evidence=(
            SignalEvidence(
                evidence_type="entry_fill_anchor",
                evidence_id=anchor_fill.fill_id.value,
                available_at=anchor_fill.filled_at,
                provider="backtest_ledger",
                validation_status="validated",
            ),
        ),
    )


def _first_available_exit(*facts: SignalFact | None) -> SignalFact | None:
    """Choose the chronologically first eligible rule for ``first_of``."""

    candidates = tuple(item for item in facts if item is not None)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            item.available_at,
            item.observed_at,
            item.session_date,
            item.condition_ref,
        ),
    )


def _validate_and_select_inputs(
    request: DailyBacktestInput,
) -> tuple[
    tuple[DailyBar, ...],
    tuple[DailyBar, ...],
    tuple[InstrumentSession, ...],
    int,
]:
    if (
        request.benchmark_entry_filled is not None
        and type(request.benchmark_entry_filled) is not bool
    ):
        raise DailyBacktestInputError("benchmark_entry_filled must be a boolean or None")
    if request.benchmark_entry_filled is not None and not request.benchmark_equity:
        raise DailyBacktestInputError(
            "benchmark_entry_filled requires a funded benchmark equity path"
        )
    if not request.bars:
        raise DailyBacktestInputError("daily backtest requires bars")
    instrument_id = InstrumentId(request.strategy.instrument.symbol)
    all_bars = tuple(
        bar for bar in request.bars if bar.session_date <= request.strategy.backtest.end
    )
    if any(bar.instrument_id != instrument_id for bar in all_bars):
        raise DailyBacktestInputError("all bars must match the strategy instrument")
    if any(bar.price_basis is not PriceBasis.UNADJUSTED for bar in all_bars):
        raise DailyBacktestInputError("execution bars must be unadjusted")
    if any(
        all_bars[index].session_date >= all_bars[index + 1].session_date
        for index in range(len(all_bars) - 1)
    ):
        raise DailyBacktestInputError("bars must be strictly date ordered")
    trade_indices = [
        index
        for index, bar in enumerate(all_bars)
        if request.strategy.backtest.start <= bar.session_date <= request.strategy.backtest.end
    ]
    if not trade_indices:
        raise DailyBacktestInputError("no bars fall inside the strategy backtest period")

    if _position_risk_exits(request.strategy) and request.signal_bars is None:
        raise DailyBacktestInputError(
            "position risk exits require explicit back-adjusted signal bars"
        )
    source_signal_bars = request.signal_bars or request.bars
    signal_bars = tuple(
        bar for bar in source_signal_bars if bar.session_date <= request.strategy.backtest.end
    )
    if any(bar.instrument_id != instrument_id for bar in signal_bars):
        raise DailyBacktestInputError("all signal bars must match the strategy instrument")
    if any(
        signal_bars[index].session_date >= signal_bars[index + 1].session_date
        for index in range(len(signal_bars) - 1)
    ):
        raise DailyBacktestInputError("signal bars must be strictly date ordered")
    if request.signal_bars is not None and any(
        bar.price_basis is not PriceBasis.BACK_ADJUSTED for bar in signal_bars
    ):
        raise DailyBacktestInputError("explicit signal bars must be back-adjusted")
    execution_keys = tuple((bar.instrument_id, bar.session_date) for bar in all_bars)
    signal_keys = tuple((bar.instrument_id, bar.session_date) for bar in signal_bars)
    if execution_keys != signal_keys:
        raise DailyBacktestInputError("signal and execution bars must align one-to-one by date")
    if any(
        signal.available_at != execution.available_at
        for execution, signal in zip(all_bars, signal_bars, strict=True)
    ):
        raise DailyBacktestInputError(
            "signal and execution bars must have identical availability times"
        )

    session_by_date = {item.session_date: item for item in request.sessions}
    if len(session_by_date) != len(request.sessions):
        raise DailyBacktestInputError("sessions must have unique dates")
    if any(item.instrument_id != instrument_id for item in request.sessions):
        raise DailyBacktestInputError("all sessions must match the strategy instrument")
    if any(item.event.instrument_id != instrument_id for item in request.events):
        raise DailyBacktestInputError("all events must match the strategy instrument")
    if any(item.instrument_id != str(instrument_id) for item in request.financial_facts):
        raise DailyBacktestInputError("all financial facts must match the strategy instrument")
    event_revision_keys = [(item.event.event_id.value, item.revision_no) for item in request.events]
    if len(event_revision_keys) != len(set(event_revision_keys)):
        raise DailyBacktestInputError("events must have unique event_id and revision_no pairs")
    missing = [bar.session_date for bar in all_bars if bar.session_date not in session_by_date]
    if missing:
        raise DailyBacktestInputError(f"missing sessions for {len(missing)} bars")
    selected_sessions = tuple(session_by_date[bar.session_date] for bar in all_bars)
    return all_bars, signal_bars, selected_sessions, trade_indices[0]


def _safe_run_key(value: str) -> str:
    safe = "".join(
        character
        for character in value
        if character.isascii() and (character.isalnum() or character in "_-.")
    )
    if not safe:
        raise DailyBacktestInputError("run_key must contain a safe identifier character")
    return safe[:40]


def _result_payload(
    *,
    decisions: Sequence[DailyStrategyDecision],
    order_traces: Sequence[OrderTrace],
    fills: Sequence[FillRecord],
    signals: Sequence[SignalFact],
    curve: Sequence[EquityPoint],
    round_trips: Sequence[RoundTrip],
    metrics: BacktestMetrics,
    final_portfolio: PortfolioState,
    benchmark_entry_filled: bool | None,
) -> dict[str, object]:
    return {
        "benchmark_entry_filled": benchmark_entry_filled,
        "decisions": [
            {
                "attempts": item.attempts,
                "created_at": item.created_at.isoformat(),
                "id": item.decision_id.value,
                "orders": [value.value for value in item.order_ids],
                "reason": item.outcome_reason,
                "side": item.side.value,
                "signal": _signal_payload(item.signal),
                "signal_semantics": (
                    item.signal_semantics.value if item.signal_semantics is not None else None
                ),
                "signal_validity_sessions": item.signal_validity_sessions,
                "status": item.status.value,
            }
            for item in decisions
        ],
        "orders": [
            {
                "id": item.order.order_id.value,
                "decision_id": item.order.decision_id.value,
                "instrument_id": item.order.instrument_id.value,
                "side": item.order.side.value,
                "order_type": item.order.order_type.value,
                "time_in_force": item.order.time_in_force.value,
                "quantity": item.order.quantity.value,
                "filled_quantity": item.order.filled_quantity.value,
                "limit_price": str(item.order.limit_price.amount),
                "limit_price_currency": item.order.limit_price.currency,
                "created_at": item.order.created_at.isoformat(),
                "valid_from": item.order.valid_from.isoformat(),
                "valid_until": item.order.valid_until.isoformat(),
                "updated_at": item.order.updated_at.isoformat(),
                "submitted_at": (
                    None if item.order.submitted_at is None else item.order.submitted_at.isoformat()
                ),
                "accepted_at": (
                    None if item.order.accepted_at is None else item.order.accepted_at.isoformat()
                ),
                "fill_eligible_at": (
                    None
                    if item.order.fill_eligible_at is None
                    else item.order.fill_eligible_at.isoformat()
                ),
                "terminal_at": (
                    None if item.order.terminal_at is None else item.order.terminal_at.isoformat()
                ),
                "terminal_reason": item.order.terminal_reason,
                "average_fill_price": (
                    None
                    if item.order.average_fill_price is None
                    else str(item.order.average_fill_price.amount)
                ),
                "average_fill_price_currency": (
                    None
                    if item.order.average_fill_price is None
                    else item.order.average_fill_price.currency
                ),
                "applied_fill_ids": [value.value for value in item.order.applied_fill_ids],
                "version": item.order.version,
                "events": [
                    {
                        "id": event.event_id.value,
                        "order_id": event.order_id.value,
                        "sequence": event.sequence,
                        "kind": event.kind.value,
                        "occurred_at": event.occurred_at.isoformat(),
                        "previous_status": (
                            None if event.previous_status is None else event.previous_status.value
                        ),
                        "status": event.status.value,
                        "fill_id": None if event.fill_id is None else event.fill_id.value,
                        "fill_quantity": (
                            None if event.fill_quantity is None else event.fill_quantity.value
                        ),
                        "fill_price": (
                            None if event.fill_price is None else str(event.fill_price.amount)
                        ),
                        "fill_price_currency": (
                            None if event.fill_price is None else event.fill_price.currency
                        ),
                        "fill_eligible_at": (
                            None
                            if event.fill_eligible_at is None
                            else event.fill_eligible_at.isoformat()
                        ),
                        "reason": event.reason_code,
                    }
                    for event in item.events
                ],
                "match": {
                    "outcome": item.match.outcome.value,
                    "reason": item.match.reason_code,
                    "quantity": item.match.quantity.value,
                    "price": None if item.match.price is None else str(item.match.price.amount),
                    "price_currency": (
                        None if item.match.price is None else item.match.price.currency
                    ),
                    "filled_at": (
                        None if item.match.filled_at is None else item.match.filled_at.isoformat()
                    ),
                    "capacity_reason_code": item.match.capacity_reason_code,
                    "time_quality": (
                        None if item.match.time_quality is None else item.match.time_quality.value
                    ),
                },
                "status": item.order.status.value,
            }
            for item in order_traces
        ],
        "fills": [
            {
                "fees": str(item.fees.total.amount),
                "fees_currency": item.fees.currency,
                "id": item.fill_id.value,
                "order_id": item.order_id.value,
                "instrument_id": item.instrument_id.value,
                "price": str(item.price.amount),
                "price_currency": item.price.currency,
                "quantity": item.quantity.value,
                "side": item.side.value,
                "time": item.filled_at.isoformat(),
                "fee_components": {
                    kind.value: str(amount.amount) for kind, amount in item.fees.items()
                },
            }
            for item in fills
        ],
        "curve": [
            {
                "date": item.session_date.isoformat(),
                "equity": str(item.equity),
                "benchmark": None if item.benchmark is None else str(item.benchmark),
            }
            for item in curve
        ],
        "signals": [_signal_payload(item) for item in signals],
        "round_trips": [
            {
                "entry_date": item.entry_date.isoformat(),
                "exit_date": item.exit_date.isoformat(),
                "net_pnl": str(item.net_pnl),
            }
            for item in round_trips
        ],
        "final_portfolio": {
            "cash": str(final_portfolio.cash.amount),
            "currency": final_portfolio.cash.currency,
            "dividend_receivable": str(final_portfolio.dividend_receivable().amount),
            "lots": [
                {
                    "opened_by_fill_id": lot.opened_by_fill_id.value,
                    "instrument_id": lot.instrument_id.value,
                    "acquired_at": lot.acquired_at.isoformat(),
                    "acquired_on": lot.acquired_on.isoformat(),
                    "sellable_on": lot.sellable_on.isoformat(),
                    "remaining_quantity": lot.remaining_quantity.value,
                    "cost_basis": str(lot.cost_basis.amount),
                }
                for lot in final_portfolio.lots
            ],
            "ledger": [
                {
                    "fill_id": entry.fill_id.value,
                    "order_id": entry.order_id.value,
                    "occurred_at": entry.occurred_at.isoformat(),
                    "postings": [
                        {
                            "account": posting.account.value,
                            "side": posting.side.value,
                            "amount": str(posting.amount.amount),
                            "currency": posting.amount.currency,
                            "component": posting.component.value,
                            "instrument_id": (
                                None
                                if posting.instrument_id is None
                                else posting.instrument_id.value
                            ),
                        }
                        for posting in entry.postings
                    ],
                }
                for entry in final_portfolio.ledger_entries
            ],
            "corporate_action_entitlements": [
                {
                    "action_id": item.action_id.value,
                    "revision_no": item.revision_no,
                    "instrument_id": item.instrument_id.value,
                    "action_type": item.action_type.value,
                    "record_date": item.record_date.isoformat(),
                    "ex_date": item.ex_date.isoformat(),
                    "settlement_date": item.settlement_date.isoformat(),
                    "entitled_quantity": item.entitled_quantity.value,
                    "cash_amount": str(item.cash_amount.amount),
                    "cash_currency": item.cash_amount.currency,
                    "share_delta": item.share_delta,
                    "share_sellable_date": (
                        None
                        if item.share_sellable_date is None
                        else item.share_sellable_date.isoformat()
                    ),
                    "status": item.status.value,
                    "captured_at": item.captured_at.isoformat(),
                    "accrued_at": (
                        None if item.accrued_at is None else item.accrued_at.isoformat()
                    ),
                    "settled_at": (
                        None if item.settled_at is None else item.settled_at.isoformat()
                    ),
                }
                for item in final_portfolio.corporate_action_entitlements
            ],
            "corporate_action_ledger": [
                {
                    "action_id": entry.action_id.value,
                    "revision_no": entry.revision_no,
                    "instrument_id": entry.instrument_id.value,
                    "action_type": entry.action_type.value,
                    "phase": entry.phase.value,
                    "occurred_at": entry.occurred_at.isoformat(),
                    "cash_before": str(entry.cash_before.amount),
                    "cash_after": str(entry.cash_after.amount),
                    "quantity_before": entry.quantity_before.value,
                    "quantity_after": entry.quantity_after.value,
                    "position_cost_before": str(entry.position_cost_before.amount),
                    "position_cost_after": str(entry.position_cost_after.amount),
                    "receivable_before": str(entry.receivable_before.amount),
                    "receivable_after": str(entry.receivable_after.amount),
                    "share_entitlement_delta_before": entry.share_entitlement_delta_before,
                    "share_entitlement_delta_after": entry.share_entitlement_delta_after,
                    "postings": [
                        {
                            "account": posting.account.value,
                            "side": posting.side.value,
                            "amount": str(posting.amount.amount),
                            "currency": posting.amount.currency,
                            "component": posting.component.value,
                            "instrument_id": (
                                None
                                if posting.instrument_id is None
                                else posting.instrument_id.value
                            ),
                        }
                        for posting in entry.postings
                    ],
                }
                for entry in final_portfolio.corporate_action_entries
            ],
        },
        "metrics": {
            item.name: _json_metric(getattr(metrics, item.name)) for item in fields(metrics)
        },
    }


def _signal_payload(item: SignalFact) -> dict[str, object]:
    return {
        "instrument_id": item.instrument_id.value,
        "session_date": item.session_date.isoformat(),
        "condition_ref": item.condition_ref,
        "triggered": item.triggered,
        "observed_at": item.observed_at.isoformat(),
        "available_at": item.available_at.isoformat(),
        "reason": item.reason,
        "left_value": None if item.left_value is None else str(item.left_value),
        "right_value": None if item.right_value is None else str(item.right_value),
        "evidence": [
            {
                "type": evidence.evidence_type,
                "id": evidence.evidence_id,
                "available_at": evidence.available_at.isoformat(),
                "source_event_id": evidence.source_event_id,
                "provider": evidence.provider,
                "source_url": evidence.source_url,
                "time_quality": evidence.time_quality,
                "validation_status": evidence.validation_status,
                "raw_response_sha256": evidence.raw_response_sha256,
            }
            for evidence in item.evidence
        ],
        "children": [_signal_payload(child) for child in item.children],
    }


def _json_metric(value: object) -> object:
    if isinstance(value, float) and not isfinite(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value
