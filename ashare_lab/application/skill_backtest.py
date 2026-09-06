"""Research-only backtest over provider-declared adjusted daily history.

This module deliberately does not reuse the legacy share/corporate-action
ledger.  Economic returns come from the provider-adjusted series while raw
prices are retained only as order references and for point-in-time market
reachability checks.  The resulting exposure units are *not* exchange shares.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistory, MxDailyRow
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.domain.analytics import BacktestMetrics, EquityPoint, RoundTrip, calculate_metrics
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.market_data import TradingStatus
from ashare_lab.domain.shared import DomainValidationError, InstrumentId
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import (
    EventCondition,
    FinancialConditionV1,
    HoldingPeriodExit,
    IndicatorCondition,
    PositionReturnExit,
    StrategySpec,
    TrailingDrawdownExit,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
MX_HISTORY_PROVIDER = "eastmoney_mx_finance_data"
MX_ADJUSTMENT_POLICY = "provider_declared_back_adjusted"
_BPS_DENOMINATOR = Decimal("10000")
_EDGE_TRIGGER_IDS = frozenset(
    {"bearish", "bullish", "new_high", "surge_down", "surge_up"}
)


class SkillBacktestInputError(DomainValidationError):
    """Raised when the skill-backed simulator would have to invent a fact."""


@dataclass(frozen=True, slots=True)
class SkillBacktestActivity:
    """One research-simulation event suitable for the existing API projection.

    ``raw_reference_price`` is an unadjusted daily-open reference, never a
    claimed tick fill.  ``quantity`` is intentionally always ``None`` because
    provider-adjusted return units are not exchange shares.
    """

    id: str
    kind: Literal["signal", "order", "fill", "partial_fill", "unfilled"]
    occurred_at: datetime
    side: Literal["buy", "sell"]
    status: Literal["confirmed", "submitted", "filled", "partially_filled", "expired"]
    reason: str
    raw_reference_price: Decimal | None = None
    notional_cny: Decimal | None = None
    quantity: None = None
    signal: SignalFact | None = None
    attempt_no: int | None = None
    chain_id: str | None = None
    decision_id: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    parent_id: str | None = None
    origin_signal_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillBacktestResult:
    """Immutable result of the provider-adjusted research simulation."""

    equity_curve: tuple[EquityPoint, ...]
    round_trips: tuple[RoundTrip, ...]
    metrics: BacktestMetrics
    activities: tuple[SkillBacktestActivity, ...]
    has_open_position: bool
    final_cash_cny: Decimal
    open_position_notional_cny: Decimal
    benchmark_entry_filled: bool
    unknown_entry_sessions: tuple[date, ...]
    unknown_exit_sessions: tuple[date, ...]
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(slots=True)
class _Position:
    return_units: Decimal
    entry_date: date
    adjusted_entry_price: Decimal
    total_cost_cny: Decimal
    realized_net_proceeds_cny: Decimal
    peak_adjusted_close: Decimal
    holding_exit_index: int | None


@dataclass(slots=True)
class _PendingDecision:
    side: Literal["buy", "sell"]
    reason: str
    available_at: datetime
    signal: SignalFact | None
    max_sessions: int
    decision_id: str
    origin_signal_id: str
    sessions_seen: int = 0
    attempts: int = 0
    last_order_id: str | None = None


@dataclass(slots=True)
class _FundedState:
    cash: Decimal
    return_units: Decimal = Decimal("0")
    entry_filled: bool = False


def run_skill_backtest(
    *,
    strategy: StrategySpec,
    history: MxDailyHistory,
    entry_timeline: tuple[SignalFact | None, ...],
    exit_timeline: tuple[SignalFact | None, ...],
    config: BacktestRunConfig,
) -> SkillBacktestResult:
    """Simulate one long-only strategy using MX provider-adjusted returns.

    Signals confirmed on a session close become eligible at the next session
    open.  Holding-period exits execute at the Nth subsequent session open;
    close-confirmed stop/take-profit/trailing exits execute no earlier than the
    next open.  A sell decision always wins over an already-pending buy and a
    completed sell cannot be followed by a same-session buy.
    """

    rows, eligible_indices = _validate_input(
        strategy=strategy,
        history=history,
        entry_timeline=entry_timeline,
        exit_timeline=exit_timeline,
    )
    initial_cash = Decimal(strategy.backtest.initial_cash_cny)
    cash = initial_cash
    position: _Position | None = None
    pending_entry: _PendingDecision | None = None
    pending_exit: _PendingDecision | None = None
    activities: list[SkillBacktestActivity] = []
    round_trips: list[RoundTrip] = []
    curve: list[EquityPoint] = []
    unknown_entry_sessions: list[date] = []
    unknown_exit_sessions: list[date] = []
    activity_sequence = 0

    benchmark = _FundedState(cash=initial_cash)
    first_row = rows[eligible_indices[0]]
    curve.append(
        EquityPoint(
            session_date=first_row.session_date - timedelta(days=1),
            equity=initial_cash,
            benchmark=initial_cash,
        )
    )

    def append_activity(
        *,
        kind: Literal["signal", "order", "fill", "partial_fill", "unfilled"],
        occurred_at: datetime,
        side: Literal["buy", "sell"],
        status: Literal["confirmed", "submitted", "filled", "partially_filled", "expired"],
        reason: str,
        raw_reference_price: Decimal | None = None,
        notional_cny: Decimal | None = None,
        signal: SignalFact | None = None,
        attempt_no: int | None = None,
        chain_id: str | None = None,
        decision_id: str | None = None,
        order_id: str | None = None,
        fill_id: str | None = None,
        parent_id: str | None = None,
        origin_signal_id: str | None = None,
    ) -> str:
        nonlocal activity_sequence
        activity_sequence += 1
        activity_id = f"skill-sim:{activity_sequence:06d}"
        if kind == "signal":
            chain_id = chain_id or activity_id
            decision_id = decision_id or activity_id
            origin_signal_id = origin_signal_id or activity_id
        elif kind == "order":
            order_id = order_id or activity_id
        elif kind in {"fill", "partial_fill"}:
            fill_id = fill_id or activity_id
        activities.append(
            SkillBacktestActivity(
                id=activity_id,
                kind=kind,
                occurred_at=occurred_at,
                side=side,
                status=status,
                reason=reason,
                raw_reference_price=raw_reference_price,
                notional_cny=notional_cny,
                signal=signal,
                attempt_no=attempt_no,
                chain_id=chain_id,
                decision_id=decision_id,
                order_id=order_id,
                fill_id=fill_id,
                parent_id=parent_id,
                origin_signal_id=origin_signal_id,
            )
        )
        return activity_id

    for index in eligible_indices:
        row = rows[index]
        previous_row = rows[index - 1] if index > 0 else None
        open_at = datetime.combine(row.session_date, time(9, 30), tzinfo=SHANGHAI)
        close_at = datetime.combine(row.session_date, time(15), tzinfo=SHANGHAI)

        if not benchmark.entry_filled:
            benchmark_fill = _buy_exposure(
                cash=benchmark.cash,
                row=row,
                previous_row=previous_row,
                config=config,
            )
            if benchmark_fill.filled_notional > 0:
                benchmark.cash -= benchmark_fill.cash_debit
                benchmark.return_units += benchmark_fill.return_units
                benchmark.entry_filled = True

        sold_today = False
        if (
            position is not None
            and pending_exit is None
            and position.holding_exit_index is not None
            and index >= position.holding_exit_index
        ):
            reason = "holding_period_target_session_open"
            origin_signal_id = append_activity(
                kind="signal",
                occurred_at=open_at,
                side="sell",
                status="confirmed",
                reason=reason,
            )
            pending_exit = _PendingDecision(
                side="sell",
                reason=reason,
                available_at=open_at,
                signal=None,
                max_sessions=config.max_exit_attempts if config.retry_unfilled_exits else 1,
                decision_id=origin_signal_id,
                origin_signal_id=origin_signal_id,
            )
            position.holding_exit_index = None

        if position is not None and pending_exit is not None:
            exit_decision = pending_exit
            if exit_decision.available_at <= open_at:
                exit_decision.sessions_seen += 1
                exit_decision.attempts += 1
                order_activity_id = append_activity(
                    kind="order",
                    occurred_at=open_at,
                    side="sell",
                    status="submitted",
                    reason=exit_decision.reason,
                    raw_reference_price=row.raw_open,
                    notional_cny=position.return_units * row.adjusted_open,
                    signal=exit_decision.signal,
                    attempt_no=exit_decision.attempts,
                    chain_id=exit_decision.decision_id,
                    decision_id=exit_decision.decision_id,
                    parent_id=exit_decision.origin_signal_id,
                    origin_signal_id=exit_decision.origin_signal_id,
                )
                exit_decision.last_order_id = order_activity_id
                sell_fill = _sell_exposure(
                    return_units=position.return_units,
                    row=row,
                    previous_row=previous_row,
                    config=config,
                )
                if sell_fill.filled_notional > 0:
                    cash += sell_fill.cash_credit
                    position.return_units -= sell_fill.return_units
                    position.realized_net_proceeds_cny += sell_fill.cash_credit
                    is_complete = position.return_units == 0
                    append_activity(
                        kind="fill" if is_complete else "partial_fill",
                        occurred_at=open_at,
                        side="sell",
                        status="filled" if is_complete else "partially_filled",
                        reason=sell_fill.reason,
                        raw_reference_price=row.raw_open,
                        notional_cny=sell_fill.filled_notional,
                        signal=exit_decision.signal,
                        attempt_no=exit_decision.attempts,
                        chain_id=exit_decision.decision_id,
                        decision_id=exit_decision.decision_id,
                        order_id=order_activity_id,
                        parent_id=order_activity_id,
                        origin_signal_id=exit_decision.origin_signal_id,
                    )
                    sold_today = True
                    pending_entry = None
                    if is_complete:
                        round_trips.append(
                            RoundTrip(
                                entry_date=position.entry_date,
                                exit_date=row.session_date,
                                net_pnl=(
                                    position.realized_net_proceeds_cny
                                    - position.total_cost_cny
                                ),
                            )
                        )
                        position = None
                        pending_exit = None
                else:
                    append_activity(
                        kind="unfilled",
                        occurred_at=open_at,
                        side="sell",
                        status="expired",
                        reason=sell_fill.reason,
                        raw_reference_price=row.raw_open,
                        signal=exit_decision.signal,
                        attempt_no=exit_decision.attempts,
                        chain_id=exit_decision.decision_id,
                        decision_id=exit_decision.decision_id,
                        order_id=order_activity_id,
                        parent_id=order_activity_id,
                        origin_signal_id=exit_decision.origin_signal_id,
                    )
                if position is not None and (
                    not config.retry_unfilled_exits
                    or exit_decision.attempts >= exit_decision.max_sessions
                ):
                    # This closes the existing decision, not a new order or fill.
                    # Link the final real order as for end-of-period expiry.
                    append_activity(
                        kind="unfilled",
                        occurred_at=open_at,
                        side="sell",
                        status="expired",
                        reason=(
                            "exit_retry_budget_exhausted"
                            if config.retry_unfilled_exits
                            else "exit_retry_disabled"
                        ),
                        signal=exit_decision.signal,
                        attempt_no=exit_decision.attempts,
                        chain_id=exit_decision.decision_id,
                        decision_id=exit_decision.decision_id,
                        order_id=order_activity_id,
                        parent_id=order_activity_id,
                        origin_signal_id=exit_decision.origin_signal_id,
                    )
                    pending_exit = None

        if position is None and pending_entry is not None and not sold_today:
            entry_decision = pending_entry
            if entry_decision.available_at <= open_at:
                entry_decision.sessions_seen += 1
                entry_decision.attempts += 1
                order_activity_id = append_activity(
                    kind="order",
                    occurred_at=open_at,
                    side="buy",
                    status="submitted",
                    reason=entry_decision.reason,
                    raw_reference_price=row.raw_open,
                    notional_cny=cash * config.allocation_ratio,
                    signal=entry_decision.signal,
                    attempt_no=entry_decision.attempts,
                    chain_id=entry_decision.decision_id,
                    decision_id=entry_decision.decision_id,
                    parent_id=entry_decision.origin_signal_id,
                    origin_signal_id=entry_decision.origin_signal_id,
                )
                entry_decision.last_order_id = order_activity_id
                buy_fill = _buy_exposure(
                    cash=cash,
                    row=row,
                    previous_row=previous_row,
                    config=config,
                )
                if buy_fill.filled_notional > 0:
                    cash -= buy_fill.cash_debit
                    holding_sessions = _holding_sessions(strategy)
                    position = _Position(
                        return_units=buy_fill.return_units,
                        entry_date=row.session_date,
                        adjusted_entry_price=buy_fill.adjusted_execution_price,
                        total_cost_cny=buy_fill.cash_debit,
                        realized_net_proceeds_cny=Decimal("0"),
                        peak_adjusted_close=max(
                            buy_fill.adjusted_execution_price,
                            row.adjusted_close,
                        ),
                        holding_exit_index=(
                            None if holding_sessions is None else index + holding_sessions
                        ),
                    )
                    append_activity(
                        kind=(
                            "partial_fill"
                            if buy_fill.filled_notional < buy_fill.requested_notional
                            else "fill"
                        ),
                        occurred_at=open_at,
                        side="buy",
                        status=(
                            "partially_filled"
                            if buy_fill.filled_notional < buy_fill.requested_notional
                            else "filled"
                        ),
                        reason=buy_fill.reason,
                        raw_reference_price=row.raw_open,
                        notional_cny=buy_fill.filled_notional,
                        signal=entry_decision.signal,
                        attempt_no=entry_decision.attempts,
                        chain_id=entry_decision.decision_id,
                        decision_id=entry_decision.decision_id,
                        order_id=order_activity_id,
                        parent_id=order_activity_id,
                        origin_signal_id=entry_decision.origin_signal_id,
                    )
                    pending_entry = None
                else:
                    append_activity(
                        kind="unfilled",
                        occurred_at=open_at,
                        side="buy",
                        status="expired",
                        reason=buy_fill.reason,
                        raw_reference_price=row.raw_open,
                        signal=entry_decision.signal,
                        attempt_no=entry_decision.attempts,
                        chain_id=entry_decision.decision_id,
                        decision_id=entry_decision.decision_id,
                        order_id=order_activity_id,
                        parent_id=order_activity_id,
                        origin_signal_id=entry_decision.origin_signal_id,
                    )
                    if entry_decision.sessions_seen >= entry_decision.max_sessions:
                        pending_entry = None

        strategy_equity = cash + (
            Decimal("0")
            if position is None
            else position.return_units * row.adjusted_close
        )
        benchmark_equity = benchmark.cash + benchmark.return_units * row.adjusted_close
        if strategy_equity <= 0 or benchmark_equity <= 0:
            raise SkillBacktestInputError("simulation equity must remain positive")
        curve.append(
            EquityPoint(
                session_date=row.session_date,
                equity=strategy_equity,
                benchmark=benchmark_equity,
            )
        )

        entry_fact = entry_timeline[index]
        exit_fact = exit_timeline[index]
        if entry_fact is None:
            unknown_entry_sessions.append(row.session_date)
        if exit_fact is None and _has_market_exit(strategy):
            unknown_exit_sessions.append(row.session_date)

        if position is None:
            if exit_fact is not None and exit_fact.triggered and pending_entry is not None:
                if pending_entry.last_order_id is not None:
                    append_activity(
                        kind="unfilled",
                        occurred_at=exit_fact.available_at,
                        side="buy",
                        status="expired",
                        reason="opposite_exit_signal_invalidated_pending_entry",
                        signal=pending_entry.signal,
                        attempt_no=pending_entry.attempts,
                        chain_id=pending_entry.decision_id,
                        decision_id=pending_entry.decision_id,
                        order_id=pending_entry.last_order_id,
                        parent_id=pending_entry.last_order_id,
                        origin_signal_id=pending_entry.origin_signal_id,
                    )
                pending_entry = None
            if entry_fact is not None and entry_fact.triggered and pending_entry is None:
                available_at = max(entry_fact.available_at, close_at)
                origin_signal_id = append_activity(
                    kind="signal",
                    occurred_at=available_at,
                    side="buy",
                    status="confirmed",
                    reason=entry_fact.reason,
                    signal=entry_fact,
                )
                pending_entry = _PendingDecision(
                    side="buy",
                    reason=entry_fact.reason,
                    available_at=available_at,
                    signal=entry_fact,
                    max_sessions=_entry_validity_sessions(strategy, config),
                    decision_id=origin_signal_id,
                    origin_signal_id=origin_signal_id,
                )
        else:
            position.peak_adjusted_close = max(
                position.peak_adjusted_close,
                row.adjusted_close,
            )
            if pending_exit is None:
                exit_reason = _close_exit_reason(
                    strategy=strategy,
                    position=position,
                    adjusted_close=row.adjusted_close,
                    exit_fact=exit_fact,
                )
                if exit_reason is not None:
                    reason, signal = exit_reason
                    available_at = (
                        close_at if signal is None else max(signal.available_at, close_at)
                    )
                    origin_signal_id = append_activity(
                        kind="signal",
                        occurred_at=available_at,
                        side="sell",
                        status="confirmed",
                        reason=reason,
                        signal=signal,
                    )
                    pending_exit = _PendingDecision(
                        side="sell",
                        reason=reason,
                        available_at=available_at,
                        signal=signal,
                        max_sessions=(
                            config.max_exit_attempts if config.retry_unfilled_exits else 1
                        ),
                        decision_id=origin_signal_id,
                        origin_signal_id=origin_signal_id,
                    )

    final_row = rows[eligible_indices[-1]]
    no_future_at = datetime.combine(final_row.session_date, time(15), tzinfo=SHANGHAI)
    final_pending: tuple[_PendingDecision | None, ...] = (
        pending_exit,
        pending_entry,
    )
    for pending in final_pending:
        if pending is not None and pending.attempts > 0 and pending.last_order_id is not None:
            append_activity(
                kind="unfilled",
                occurred_at=no_future_at,
                side=pending.side,
                status="expired",
                reason="no_future_session_in_backtest_period",
                signal=pending.signal,
                attempt_no=pending.attempts,
                chain_id=pending.decision_id,
                decision_id=pending.decision_id,
                order_id=pending.last_order_id,
                parent_id=pending.last_order_id,
                origin_signal_id=pending.origin_signal_id,
            )

    open_notional = (
        Decimal("0")
        if position is None
        else position.return_units * final_row.adjusted_close
    )
    limitations = [
        (
            "研究模拟使用东方财富声明的后复权价格计算资金暴露收益；"
            "它不是证券账户逐笔股份账本，不代表现金分红、送转股或税费实际到账。"
        ),
        (
            "原始价格仅用于每日开盘参考与停牌、涨跌停、流动性检查；"
            "quantity 恒为空，不推断真实股数、排队位置或逐笔成交。"
        ),
        "日线开盘价是模拟成交代理，不是 09:30 逐笔撮合证据。",
        (
            "费用仅包含 BacktestRunConfig 提供的佣金率与最低佣金；"
            "配置未提供的印花税等费用不作猜测。"
        ),
    ]
    if unknown_entry_sessions or unknown_exit_sessions:
        limitations.append(
            "SignalFact=None 保留为未知而非 false；未知日期已在结果中单独列出。"
        )

    result_curve = tuple(curve)
    result_round_trips = tuple(round_trips)
    return SkillBacktestResult(
        equity_curve=result_curve,
        round_trips=result_round_trips,
        metrics=calculate_metrics(result_curve, result_round_trips),
        activities=tuple(activities),
        has_open_position=position is not None,
        final_cash_cny=cash,
        open_position_notional_cny=open_notional,
        benchmark_entry_filled=benchmark.entry_filled,
        unknown_entry_sessions=tuple(unknown_entry_sessions),
        unknown_exit_sessions=tuple(unknown_exit_sessions),
        assumptions=(
            "provider_adjusted_return_units_no_share_ledger",
            "close_signal_next_session_open_proxy",
            "a_share_t_plus_one",
            "sell_priority_no_same_session_reentry",
            "fees_and_slippage_from_backtest_run_config",
            (
                "point_in_time_liquidity_previous_completed_session_volume"
                if config.capacity_mode is CapacityMode.POINT_IN_TIME_VOLUME
                else "liquidity_capacity_explicitly_unlimited"
            ),
            "funded_benchmark_same_execution_cost_basis",
            "end_of_period_position_marked_without_forced_liquidation",
        ),
        limitations=tuple(limitations),
    )


@dataclass(frozen=True, slots=True)
class _ExposureFill:
    requested_notional: Decimal
    filled_notional: Decimal
    return_units: Decimal
    adjusted_execution_price: Decimal
    fee_cny: Decimal
    cash_debit: Decimal
    cash_credit: Decimal
    reason: str


def _buy_exposure(
    *,
    cash: Decimal,
    row: MxDailyRow,
    previous_row: MxDailyRow | None,
    config: BacktestRunConfig,
) -> _ExposureFill:
    reason, capacity = _execution_capacity(
        row=row,
        previous_row=previous_row,
        side="buy",
        config=config,
    )
    budget = cash * config.allocation_ratio
    requested = _affordable_notional(
        budget,
        rate=config.commission_rate,
        minimum=config.minimum_commission_cny,
    )
    if reason is not None or requested <= 0:
        return _empty_fill(reason or "insufficient_cash_after_fee", requested=requested)
    filled = requested if capacity is None else min(requested, capacity)
    fee = _commission(filled, config)
    if filled <= 0 or filled + fee > budget:
        return _empty_fill("insufficient_executable_notional_after_fee", requested=requested)
    adjusted_price = row.adjusted_open * (
        Decimal("1") + config.slippage_bps / _BPS_DENOMINATOR
    )
    if adjusted_price <= 0:
        return _empty_fill("invalid_adjusted_open", requested=requested)
    return _ExposureFill(
        requested_notional=requested,
        filled_notional=filled,
        return_units=filled / adjusted_price,
        adjusted_execution_price=adjusted_price,
        fee_cny=fee,
        cash_debit=filled + fee,
        cash_credit=Decimal("0"),
        reason=("filled_at_open_proxy" if filled == requested else "partial_liquidity_fill"),
    )


def _sell_exposure(
    *,
    return_units: Decimal,
    row: MxDailyRow,
    previous_row: MxDailyRow | None,
    config: BacktestRunConfig,
) -> _ExposureFill:
    reason, capacity = _execution_capacity(
        row=row,
        previous_row=previous_row,
        side="sell",
        config=config,
    )
    adjusted_price = row.adjusted_open * (
        Decimal("1") - config.slippage_bps / _BPS_DENOMINATOR
    )
    requested = return_units * adjusted_price
    if reason is not None or requested <= 0 or adjusted_price <= 0:
        return _empty_fill(reason or "invalid_adjusted_open", requested=requested)
    filled = requested if capacity is None else min(requested, capacity)
    fee = _commission(filled, config)
    if filled <= fee:
        return _empty_fill("sell_proceeds_do_not_cover_fee", requested=requested)
    sold_units = return_units if filled == requested else filled / adjusted_price
    return _ExposureFill(
        requested_notional=requested,
        filled_notional=filled,
        return_units=sold_units,
        adjusted_execution_price=adjusted_price,
        fee_cny=fee,
        cash_debit=Decimal("0"),
        cash_credit=filled - fee,
        reason=("filled_at_open_proxy" if filled == requested else "partial_liquidity_fill"),
    )


def _execution_capacity(
    *,
    row: MxDailyRow,
    previous_row: MxDailyRow | None,
    side: Literal["buy", "sell"],
    config: BacktestRunConfig,
) -> tuple[str | None, Decimal | None]:
    if row.trading_status is not TradingStatus.TRADING:
        return f"security_{row.trading_status.value}", Decimal("0")
    if row.raw_open <= 0 or row.adjusted_open <= 0:
        return "invalid_open_price", Decimal("0")
    adverse_limit = (
        row.upper_limit is not None
        and side == "buy"
        and row.raw_open >= row.upper_limit
    ) or (
        row.lower_limit is not None
        and side == "sell"
        and row.raw_open <= row.lower_limit
    )
    if adverse_limit and config.limit_handling is not LimitHandling.ALLOW_LIMIT_VOLUME:
        return (
            "adverse_price_limit"
            if config.limit_handling is LimitHandling.STRICT_NO_FILL_AT_LIMIT
            else f"daily_{side}_limit_open_unlock_unknown"
        ), Decimal("0")
    if config.capacity_mode is CapacityMode.UNLIMITED:
        return None, None
    if previous_row is None or previous_row.volume <= 0:
        return "previous_session_liquidity_unavailable", Decimal("0")
    capacity = previous_row.volume * row.raw_open * config.participation_rate
    if capacity <= 0:
        return "participation_capacity_zero", Decimal("0")
    return None, capacity


def _commission(notional: Decimal, config: BacktestRunConfig) -> Decimal:
    if notional <= 0:
        return Decimal("0")
    return max(config.minimum_commission_cny, notional * config.commission_rate)


def _affordable_notional(
    budget: Decimal,
    *,
    rate: Decimal,
    minimum: Decimal,
) -> Decimal:
    if budget <= minimum:
        return Decimal("0")
    variable_candidate = budget / (Decimal("1") + rate)
    if variable_candidate * rate >= minimum:
        return variable_candidate
    return budget - minimum


def _empty_fill(reason: str, *, requested: Decimal) -> _ExposureFill:
    return _ExposureFill(
        requested_notional=max(Decimal("0"), requested),
        filled_notional=Decimal("0"),
        return_units=Decimal("0"),
        adjusted_execution_price=Decimal("0"),
        fee_cny=Decimal("0"),
        cash_debit=Decimal("0"),
        cash_credit=Decimal("0"),
        reason=reason,
    )


def _entry_validity_sessions(
    strategy: StrategySpec,
    config: BacktestRunConfig,
) -> int:
    condition = strategy.entry
    if isinstance(condition, EventCondition):
        return config.event_entry_validity_sessions
    if isinstance(condition, FinancialConditionV1):
        return config.state_entry_validity_sessions
    if isinstance(condition, IndicatorCondition):
        if "cross" in condition.trigger or condition.trigger in _EDGE_TRIGGER_IDS:
            return config.edge_entry_validity_sessions
        return config.state_entry_validity_sessions
    return 1


def _holding_sessions(strategy: StrategySpec) -> int | None:
    rule = next(
        (item for item in strategy.exit.children if isinstance(item, HoldingPeriodExit)),
        None,
    )
    return None if rule is None else rule.sessions


def _has_market_exit(strategy: StrategySpec) -> bool:
    return any(
        not isinstance(
            item,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
        )
        for item in strategy.exit.children
    )


def _close_exit_reason(
    *,
    strategy: StrategySpec,
    position: _Position,
    adjusted_close: Decimal,
    exit_fact: SignalFact | None,
) -> tuple[str, SignalFact | None] | None:
    if exit_fact is not None and exit_fact.triggered:
        return exit_fact.reason, exit_fact
    position_return = adjusted_close / position.adjusted_entry_price - Decimal("1")
    trailing_drawdown = adjusted_close / position.peak_adjusted_close - Decimal("1")
    for rule in strategy.exit.children:
        if isinstance(rule, PositionReturnExit):
            threshold = Decimal(str(rule.threshold_pct)) / Decimal("100")
            if rule.trigger == "take_profit" and position_return >= threshold:
                return "take_profit_close_confirmed", None
            if rule.trigger == "stop_loss" and position_return <= -threshold:
                return "stop_loss_close_confirmed", None
        elif isinstance(rule, TrailingDrawdownExit):
            threshold = Decimal(str(rule.threshold_pct)) / Decimal("100")
            if trailing_drawdown <= -threshold:
                return "trailing_drawdown_close_confirmed", None
    return None


def _validate_input(
    *,
    strategy: StrategySpec,
    history: MxDailyHistory,
    entry_timeline: tuple[SignalFact | None, ...],
    exit_timeline: tuple[SignalFact | None, ...],
) -> tuple[tuple[MxDailyRow, ...], tuple[int, ...]]:
    if history.provider != MX_HISTORY_PROVIDER:
        raise SkillBacktestInputError("history provider must be eastmoney_mx_finance_data")
    if history.adjustment != MX_ADJUSTMENT_POLICY:
        raise SkillBacktestInputError(
            "history must declare provider_declared_back_adjusted prices"
        )
    if str(history.instrument_id) != strategy.instrument.symbol:
        raise SkillBacktestInputError("history instrument does not match strategy")
    rows = tuple(history.rows)
    if not rows:
        raise SkillBacktestInputError("history rows cannot be empty")
    if len(entry_timeline) != len(rows) or len(exit_timeline) != len(rows):
        raise SkillBacktestInputError("signal timelines must align one-to-one with history rows")
    dates = tuple(item.session_date for item in rows)
    if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise SkillBacktestInputError("history dates must be strictly increasing")
    instrument_id = InstrumentId(strategy.instrument.symbol)
    for row, entry_fact, exit_fact in zip(rows, entry_timeline, exit_timeline, strict=True):
        if (row.upper_limit is None) != (row.lower_limit is None):
            raise SkillBacktestInputError("upper and lower limits must both be present or absent")
        for value_name in (
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "raw_preclose",
            "adjusted_open",
            "adjusted_high",
            "adjusted_low",
            "adjusted_close",
        ):
            value = getattr(row, value_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise SkillBacktestInputError(f"{value_name} must be a finite positive Decimal")
        if type(row.volume) is not int or row.volume < 0:
            raise SkillBacktestInputError("volume must be a non-negative integer")
        for fact in (entry_fact, exit_fact):
            if fact is None:
                continue
            if fact.instrument_id != instrument_id or fact.session_date != row.session_date:
                raise SkillBacktestInputError(
                    "signal facts must match the aligned history instrument and session"
                )
    eligible_indices = tuple(
        index
        for index, row in enumerate(rows)
        if strategy.backtest.start <= row.session_date <= strategy.backtest.end
    )
    if not eligible_indices:
        raise SkillBacktestInputError("history has no rows inside the strategy period")
    return rows, eligible_indices
