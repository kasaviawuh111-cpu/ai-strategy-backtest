"""Funded buy-and-hold benchmark using the production execution primitives."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Protocol
from zoneinfo import ZoneInfo

from ashare_lab.application.pre_open_sizing import (
    PRE_OPEN_BUY_SIZING_UPPER_LIMIT_UNAVAILABLE,
    size_pre_open_buy,
)
from ashare_lab.domain.execution import (
    CapacityMode,
    DailyBarMatchingModel,
    DailyBarMatchRequest,
    LimitHandling,
    MatchOutcome,
    MatchResult,
    previous_session_volume_proxy,
)
from ashare_lab.domain.market_data import DailyBar, InstrumentSession, PriceBasis
from ashare_lab.domain.orders import Order, OrderSide, OrderStateMachine, OrderStatus
from ashare_lab.domain.portfolio import FeeBreakdown, FillRecord, PortfolioState, apply_buy
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
from ashare_lab.ports.corporate_actions import (
    AppliedCorporateAction,
    CorporateActionApplier,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
FUNDED_BENCHMARK_POLICY = "funded_buy_and_hold.same_execution_ledger.v1"


class FundedBenchmarkInputError(DomainValidationError):
    """A funded benchmark cannot be replayed without inventing input facts."""


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


@dataclass(frozen=True, slots=True)
class FundedBuyAndHoldConfig:
    initial_cash: Money
    participation_rate: Decimal = Decimal("0.05")
    slippage_bps: Decimal = Decimal("5")
    slippage_cny: Decimal = Decimal("0")
    limit_handling: LimitHandling = LimitHandling.WAIT_FOR_UNLOCK
    allocation_ratio: Decimal = Decimal("1")
    capacity_mode: CapacityMode = CapacityMode.POINT_IN_TIME_VOLUME

    def __post_init__(self) -> None:
        if self.initial_cash.amount <= 0:
            raise FundedBenchmarkInputError("initial_cash must be positive")
        if not Decimal("0") < self.participation_rate <= Decimal("1"):
            raise FundedBenchmarkInputError("participation_rate must be in (0, 1]")
        if not Decimal("0") <= self.slippage_bps <= Decimal("1000"):
            raise FundedBenchmarkInputError("slippage_bps must be in [0, 1000]")
        if not self.slippage_cny.is_finite() or self.slippage_cny < 0:
            raise FundedBenchmarkInputError("slippage_cny must be finite and non-negative")
        if not Decimal("0") < self.allocation_ratio <= Decimal("1"):
            raise FundedBenchmarkInputError("allocation_ratio must be in (0, 1]")
        if type(self.limit_handling) is not LimitHandling:
            raise FundedBenchmarkInputError("limit_handling must be LimitHandling")
        if type(self.capacity_mode) is not CapacityMode:
            raise FundedBenchmarkInputError("capacity_mode must be CapacityMode")


@dataclass(frozen=True, slots=True)
class FundedBenchmarkInput:
    run_key: str
    period_start: date
    period_end: date
    bars: tuple[DailyBar, ...]
    sessions: tuple[InstrumentSession, ...]
    calendar: SessionCalendar
    fee_calculator: FeeQuoteProvider
    corporate_actions: CorporateActionApplier
    config: FundedBuyAndHoldConfig


@dataclass(frozen=True, slots=True)
class FundedBenchmarkPoint:
    session_date: date
    equity: Decimal
    cash: Decimal
    shares: int


@dataclass(frozen=True, slots=True)
class CorporateActionAudit:
    session_date: date
    action: AppliedCorporateAction


@dataclass(frozen=True, slots=True)
class BenchmarkEntryAttempt:
    session_date: date
    match: MatchResult
    order: Order | None = None
    fill: FillRecord | None = None


@dataclass(frozen=True, slots=True)
class FundedBuyAndHoldResult:
    initial_cash: Money
    points: tuple[FundedBenchmarkPoint, ...]
    entry_attempts: tuple[BenchmarkEntryAttempt, ...]
    final_portfolio: PortfolioState
    corporate_action_policy_id: str
    corporate_actions: tuple[CorporateActionAudit, ...] = field(default_factory=tuple)

    @property
    def final_equity(self) -> Decimal:
        return self.points[-1].equity

    @property
    def total_return(self) -> Decimal:
        return self.final_equity / self.initial_cash.amount - Decimal("1")

    @property
    def funded_equity_path(self) -> tuple[tuple[date, Decimal], ...]:
        return tuple((point.session_date, point.equity) for point in self.points)

    @property
    def entry_fill(self) -> FillRecord | None:
        return next((item.fill for item in self.entry_attempts if item.fill is not None), None)


def run_funded_buy_and_hold(request: FundedBenchmarkInput) -> FundedBuyAndHoldResult:
    """Buy at the first executable session open, then hold funded equity.

    ``bars`` must include the completed session before ``period_start`` when the
    default point-in-time capacity mode is used.  That prior bar supplies the
    only daily-volume value knowable before the first benchmark order matches.
    """

    instrument_id, eligible_indices = _validate_input(request)
    portfolio = PortfolioState(cash=request.config.initial_cash)
    attempts: list[BenchmarkEntryAttempt] = []
    points: list[FundedBenchmarkPoint] = []
    action_audit: list[CorporateActionAudit] = []
    applied_action_keys: set[tuple[str, str]] = set()
    has_entry_fill = False
    id_prefix = hashlib.sha256(request.run_key.encode("utf-8")).hexdigest()[:16]

    for index in eligible_indices:
        bar = request.bars[index]
        session = request.sessions[index]
        open_at = datetime.combine(bar.session_date, time(9, 30), tzinfo=SHANGHAI)
        application = request.corporate_actions.apply_before_session(
            portfolio=portfolio,
            instrument_id=instrument_id,
            session=session,
            as_of=open_at,
        )
        portfolio = application.portfolio
        _validate_corporate_action_state(portfolio, instrument_id, request.config.initial_cash)
        _append_action_audit(
            action_audit,
            applied_action_keys,
            session_date=bar.session_date,
            actions=application.applied_actions,
        )

        if not has_entry_fill:
            previous_bar = request.bars[index - 1] if index > 0 else None
            attempt, portfolio = _attempt_entry(
                request=request,
                bar=bar,
                session=session,
                previous_bar=previous_bar,
                portfolio=portfolio,
                id_prefix=id_prefix,
                attempt_number=len(attempts) + 1,
            )
            attempts.append(attempt)
            has_entry_fill = attempt.fill is not None

        shares = portfolio.position_quantity(instrument_id).value + portfolio.pending_share_delta(
            instrument_id
        )
        points.append(
            FundedBenchmarkPoint(
                session_date=bar.session_date,
                equity=(
                    portfolio.cash.amount
                    + portfolio.dividend_receivable(instrument_id).amount
                    + Decimal(shares) * bar.close.amount
                ),
                cash=portfolio.cash.amount,
                shares=shares,
            )
        )

        after_session = getattr(request.corporate_actions, "apply_after_session", None)
        if after_session is not None:
            close_at = datetime.combine(bar.session_date, time(15), tzinfo=SHANGHAI)
            application = after_session(
                portfolio=portfolio,
                instrument_id=instrument_id,
                session=session,
                as_of=close_at,
            )
            portfolio = application.portfolio
            _validate_corporate_action_state(
                portfolio,
                instrument_id,
                request.config.initial_cash,
            )
            _append_action_audit(
                action_audit,
                applied_action_keys,
                session_date=bar.session_date,
                actions=application.applied_actions,
            )

    policy_id = request.corporate_actions.policy_id
    if not policy_id.strip():
        raise FundedBenchmarkInputError("corporate action policy_id cannot be blank")
    return FundedBuyAndHoldResult(
        initial_cash=request.config.initial_cash,
        points=tuple(points),
        entry_attempts=tuple(attempts),
        final_portfolio=portfolio,
        corporate_action_policy_id=policy_id,
        corporate_actions=tuple(action_audit),
    )


def _attempt_entry(
    *,
    request: FundedBenchmarkInput,
    bar: DailyBar,
    session: InstrumentSession,
    previous_bar: DailyBar | None,
    portfolio: PortfolioState,
    id_prefix: str,
    attempt_number: int,
) -> tuple[BenchmarkEntryAttempt, PortfolioState]:
    open_at = datetime.combine(bar.session_date, time(9, 30), tzinfo=SHANGHAI)
    close_at = datetime.combine(bar.session_date, time(15), tzinfo=SHANGHAI)
    sizing = size_pre_open_buy(
        cash=portfolio.cash,
        session=session,
        allocation_ratio=request.config.allocation_ratio,
        fee_calculator=request.fee_calculator,
        trading_date=bar.session_date,
    )
    if sizing is None:
        match = MatchResult(
            outcome=MatchOutcome.NO_FILL,
            reason_code=PRE_OPEN_BUY_SIZING_UPPER_LIMIT_UNAVAILABLE,
        )
        return BenchmarkEntryAttempt(session_date=bar.session_date, match=match), portfolio
    quantity = sizing.quantity
    if quantity.value == 0:
        match = MatchResult(
            outcome=MatchOutcome.NO_FILL,
            reason_code="insufficient_cash_for_minimum_buy_quantity",
        )
        return BenchmarkEntryAttempt(session_date=bar.session_date, match=match), portfolio

    order_id = OrderId(f"benchmark:{id_prefix}:order:{attempt_number}")
    accepted = _accepted_order(
        order_id=order_id,
        id_prefix=id_prefix,
        attempt_number=attempt_number,
        instrument_id=bar.instrument_id,
        quantity=quantity,
        limit_price=sizing.affordability_price,
        open_at=open_at,
        close_at=close_at,
    )
    point_in_time_volume = (
        previous_session_volume_proxy(previous_bar)
        if request.config.capacity_mode is CapacityMode.POINT_IN_TIME_VOLUME
        and previous_bar is not None
        else None
    )
    match = DailyBarMatchingModel.match(
        DailyBarMatchRequest(
            order=accepted,
            bar=bar,
            session=session,
            opening_price_proxy_at=open_at,
            participation_rate=request.config.participation_rate,
            slippage_bps=request.config.slippage_bps,
            slippage_cny=request.config.slippage_cny,
            limit_handling=request.config.limit_handling,
            capacity_mode=request.config.capacity_mode,
            point_in_time_volume=point_in_time_volume,
        )
    )
    final_order = accepted
    fill: FillRecord | None = None
    if match.outcome is not MatchOutcome.NO_FILL:
        assert match.price is not None and match.filled_at is not None
        fill_id = FillId(f"benchmark:{id_prefix}:fill:{attempt_number}")
        transition = OrderStateMachine.record_fill(
            final_order,
            event_id=_event_id(id_prefix, attempt_number, 4),
            fill_id=fill_id,
            filled_at=match.filled_at,
            quantity=match.quantity,
            price=match.price,
        )
        final_order = transition.order
        fill = FillRecord(
            fill_id=fill_id,
            order_id=order_id,
            instrument_id=bar.instrument_id,
            side=OrderSide.BUY,
            quantity=match.quantity,
            price=match.price,
            filled_at=match.filled_at,
            fees=request.fee_calculator.calculate(
                side=OrderSide.BUY,
                price=match.price,
                quantity=match.quantity,
                trade_date=bar.session_date,
            ),
        )
        try:
            sellable_on = request.calendar.next_session(bar.session_date)
        except (KeyError, ValueError) as exc:
            raise FundedBenchmarkInputError(
                "calendar must include the first session after a benchmark buy"
            ) from exc
        portfolio = apply_buy(portfolio, fill, sellable_on=sellable_on)

    if final_order.status is not OrderStatus.FILLED:
        transition = OrderStateMachine.expire(
            final_order,
            event_id=_event_id(id_prefix, attempt_number, final_order.version + 1),
            expired_at=close_at,
            reason_code=(
                "day_remainder_expired"
                if match.outcome is not MatchOutcome.NO_FILL
                else match.reason_code
            ),
        )
        final_order = transition.order
    return (
        BenchmarkEntryAttempt(
            session_date=bar.session_date,
            match=match,
            order=final_order,
            fill=fill,
        ),
        portfolio,
    )


def _accepted_order(
    *,
    order_id: OrderId,
    id_prefix: str,
    attempt_number: int,
    instrument_id: InstrumentId,
    quantity: Quantity,
    limit_price: Price,
    open_at: datetime,
    close_at: datetime,
) -> Order:
    created_at = open_at - timedelta(minutes=2)
    created = OrderStateMachine.create(
        order_id=order_id,
        event_id=_event_id(id_prefix, attempt_number, 1),
        decision_id=DecisionId(f"benchmark:{id_prefix}:entry"),
        instrument_id=instrument_id,
        side=OrderSide.BUY,
        quantity=quantity,
        limit_price=limit_price,
        created_at=created_at,
        valid_from=open_at,
        valid_until=close_at,
    )
    submitted = OrderStateMachine.submit(
        created.order,
        event_id=_event_id(id_prefix, attempt_number, 2),
        submitted_at=open_at - timedelta(minutes=1),
    )
    return OrderStateMachine.accept(
        submitted.order,
        event_id=_event_id(id_prefix, attempt_number, 3),
        accepted_at=open_at,
        fill_eligible_at=open_at,
    ).order


def _validate_input(request: FundedBenchmarkInput) -> tuple[InstrumentId, tuple[int, ...]]:
    if not request.run_key.strip():
        raise FundedBenchmarkInputError("run_key cannot be blank")
    if type(request.period_start) is not date or type(request.period_end) is not date:
        raise FundedBenchmarkInputError("benchmark period bounds must be dates")
    if request.period_start > request.period_end:
        raise FundedBenchmarkInputError("period_start must not be after period_end")
    if not request.bars:
        raise FundedBenchmarkInputError("benchmark requires daily bars")
    if len(request.bars) != len(request.sessions):
        raise FundedBenchmarkInputError("bars and sessions must align one-to-one")
    dates = tuple(bar.session_date for bar in request.bars)
    if any(left >= right for left, right in pairwise(dates)):
        raise FundedBenchmarkInputError("bars must be strictly ordered by session_date")
    if dates != tuple(session.session_date for session in request.sessions):
        raise FundedBenchmarkInputError("bar and session dates must align one-to-one")
    instrument_ids = {
        *(bar.instrument_id for bar in request.bars),
        *(session.instrument_id for session in request.sessions),
    }
    if len(instrument_ids) != 1:
        raise FundedBenchmarkInputError("benchmark supports exactly one aligned instrument")
    if any(bar.price_basis is not PriceBasis.UNADJUSTED for bar in request.bars):
        raise FundedBenchmarkInputError("funded benchmark accounting requires unadjusted bars")
    instrument_id = next(iter(instrument_ids))
    if any(bar.close.currency != request.config.initial_cash.currency for bar in request.bars):
        raise FundedBenchmarkInputError("bars and initial_cash must use one currency")
    eligible_indices = tuple(
        index
        for index, day in enumerate(dates)
        if request.period_start <= day <= request.period_end
    )
    if not eligible_indices:
        raise FundedBenchmarkInputError("bars do not cover the requested benchmark period")
    return instrument_id, eligible_indices


def _validate_corporate_action_state(
    portfolio: PortfolioState,
    instrument_id: InstrumentId,
    initial_cash: Money,
) -> None:
    if portfolio.cash.currency != initial_cash.currency:
        raise FundedBenchmarkInputError("corporate actions cannot change portfolio currency")
    if any(lot.instrument_id != instrument_id for lot in portfolio.lots):
        raise FundedBenchmarkInputError("corporate actions introduced another instrument")
    if (
        portfolio.position_quantity(instrument_id).value
        + portfolio.pending_share_delta(instrument_id)
        < 0
    ):
        raise FundedBenchmarkInputError("corporate actions produced negative economic shares")


def _append_action_audit(
    audit: list[CorporateActionAudit],
    applied_keys: set[tuple[str, str]],
    *,
    session_date: date,
    actions: tuple[AppliedCorporateAction, ...],
) -> None:
    for action in actions:
        key = (action.action_id, action.phase)
        if key in applied_keys:
            raise FundedBenchmarkInputError(
                f"corporate action stage {key!r} was applied more than once"
            )
        applied_keys.add(key)
        audit.append(CorporateActionAudit(session_date, action))


def _event_id(id_prefix: str, attempt_number: int, sequence: int) -> OrderEventId:
    return OrderEventId(f"benchmark:{id_prefix}:event:{attempt_number}:{sequence}")
