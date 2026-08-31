"""Conservative daily-bar matching through the shared order aggregate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum

from ashare_lab.domain.market_data import DailyBar, InstrumentSession, TradingStatus
from ashare_lab.domain.orders import Order, OrderSide, OrderStatus
from ashare_lab.domain.shared import DomainValidationError, Price, Quantity, require_aware


class LimitHandling(StrEnum):
    """Daily assumptions are separate from minute/Tick/L2 data capability."""

    WAIT_FOR_UNLOCK = "wait_for_unlock"
    STRICT_NO_FILL_AT_LIMIT = "strict_no_fill_at_limit"
    ALLOW_LIMIT_VOLUME = "allow_limit_volume"


class CapacityMode(StrEnum):
    """How a replay bounds executable shares at the matching timestamp."""

    POINT_IN_TIME_VOLUME = "point_in_time_volume"
    UNLIMITED = "unlimited"


class VolumeSource(StrEnum):
    """Auditable sources whose values can be known before an order is matched."""

    PREVIOUS_SESSION_DAILY_BAR = "previous_session_daily_bar"
    EXPLICIT_POINT_IN_TIME_OBSERVATION = "explicit_point_in_time_observation"


@dataclass(frozen=True, slots=True)
class PointInTimeVolume:
    """A volume observation together with the time at which it was knowable.

    The default daily-replay policy uses the previous completed session's volume
    as a conservative capacity proxy.  Intraday engines may instead provide a
    genuinely observed point-in-time value, but must still prove that it was
    available no later than the matching timestamp.
    """

    quantity: Quantity
    known_at: datetime
    source: VolumeSource
    source_session_date: date | None = None

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        if type(self.source) is not VolumeSource:
            raise DomainValidationError("source must be a VolumeSource")
        if self.source is VolumeSource.PREVIOUS_SESSION_DAILY_BAR:
            if type(self.source_session_date) is not date:
                raise DomainValidationError("previous-session volume requires source_session_date")
        elif self.source_session_date is not None and type(self.source_session_date) is not date:
            raise DomainValidationError("source_session_date must be a date when supplied")

    @property
    def reason_code(self) -> str:
        if self.source is VolumeSource.PREVIOUS_SESSION_DAILY_BAR:
            return "capacity_previous_session_volume_proxy"
        return "capacity_explicit_point_in_time_observation"


def previous_session_volume_proxy(bar: DailyBar) -> PointInTimeVolume:
    """Promote one completed daily bar into a next-session volume proxy."""

    return PointInTimeVolume(
        quantity=bar.volume,
        known_at=bar.available_at,
        source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
        source_session_date=bar.session_date,
    )


class MatchOutcome(StrEnum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    NO_FILL = "no_fill"


class ExecutionTimeQuality(StrEnum):
    """What a daily-bar fill timestamp actually proves.

    A daily OHLCV row contains an opening price but no opening-auction print or
    order-book timestamp.  Recording the continuous-session boundary therefore
    remains an explicit proxy, never an assertion of an exact auction fill.
    """

    DAILY_BAR_OPEN_PROXY = "daily_bar_open_proxy"
    DAILY_BAR_AVAILABLE_AT_PROXY = "daily_bar_available_at_proxy"


@dataclass(frozen=True, slots=True)
class DailyBarMatchRequest:
    order: Order
    bar: DailyBar
    session: InstrumentSession
    opening_price_proxy_at: datetime
    participation_rate: Decimal = Decimal("0.05")
    slippage_bps: Decimal = Decimal("0")
    limit_handling: LimitHandling = LimitHandling.WAIT_FOR_UNLOCK
    capacity_mode: CapacityMode = CapacityMode.POINT_IN_TIME_VOLUME
    point_in_time_volume: PointInTimeVolume | None = None

    def __post_init__(self) -> None:
        require_aware(self.opening_price_proxy_at, "opening_price_proxy_at")
        if type(self.capacity_mode) is not CapacityMode:
            raise DomainValidationError("capacity_mode must be a CapacityMode")
        if not Decimal("0") < self.participation_rate <= Decimal("1"):
            raise DomainValidationError("participation_rate must be in (0, 1]")
        if not Decimal("0") <= self.slippage_bps <= Decimal("1000"):
            raise DomainValidationError("slippage_bps must be in [0, 1000]")
        if self.capacity_mode is CapacityMode.UNLIMITED:
            if self.point_in_time_volume is not None:
                raise DomainValidationError(
                    "unlimited capacity cannot also receive point_in_time_volume"
                )
        elif self.point_in_time_volume is not None:
            if self.point_in_time_volume.known_at > self.opening_price_proxy_at:
                raise DomainValidationError(
                    "point-in-time volume must be known by opening_price_proxy_at"
                )
            source_date = self.point_in_time_volume.source_session_date
            if self.point_in_time_volume.source is VolumeSource.PREVIOUS_SESSION_DAILY_BAR and (
                source_date is None or source_date >= self.bar.session_date
            ):
                raise DomainValidationError(
                    "previous-session volume must come from a date before the matched session"
                )
        instrument_ids = {
            self.order.instrument_id,
            self.bar.instrument_id,
            self.session.instrument_id,
        }
        if len(instrument_ids) != 1:
            raise DomainValidationError("order, bar, and session instruments must match")
        if self.bar.session_date != self.session.session_date:
            raise DomainValidationError("bar and session dates must match")
        if self.order.side is OrderSide.BUY and not self.session.is_valid_buy_quantity(
            self.order.quantity.value
        ):
            raise DomainValidationError("buy order quantity violates the session declaration rule")


@dataclass(frozen=True, slots=True)
class MatchResult:
    outcome: MatchOutcome
    reason_code: str
    quantity: Quantity = field(default_factory=Quantity.zero)
    price: Price | None = None
    filled_at: datetime | None = None
    capacity_reason_code: str | None = None
    time_quality: ExecutionTimeQuality | None = None

    def __post_init__(self) -> None:
        if self.outcome is MatchOutcome.NO_FILL:
            if (
                self.quantity.value != 0
                or self.price is not None
                or self.filled_at is not None
                or self.time_quality is not None
            ):
                raise DomainValidationError("no-fill result cannot contain fill fields")
            return
        if self.quantity.value <= 0 or self.price is None or self.filled_at is None:
            raise DomainValidationError("fill result requires quantity, price, and time")
        if not self.capacity_reason_code:
            raise DomainValidationError("fill result requires capacity_reason_code")
        if type(self.time_quality) is not ExecutionTimeQuality:
            raise DomainValidationError("fill result requires execution time quality")
        require_aware(self.filled_at, "filled_at")


class DailyBarMatchingModel:
    """Match an already accepted DAY order against the next session.

    At an adverse-limit open, a daily bar cannot establish the moment at which
    queue access became possible.  ``wait_for_unlock`` therefore leaves the
    order unfilled; users may explicitly select the optimistic volume mode.
    """

    @classmethod
    def match(cls, request: DailyBarMatchRequest) -> MatchResult:
        order = request.order
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            return cls._no_fill("order_not_working")
        if request.opening_price_proxy_at < order.valid_from:
            return cls._no_fill("order_not_yet_valid")
        if request.opening_price_proxy_at > order.valid_until:
            return cls._no_fill("order_validity_elapsed")
        if (
            order.fill_eligible_at is None
            or request.opening_price_proxy_at < order.fill_eligible_at
        ):
            return cls._no_fill("fill_not_yet_eligible")
        if request.session.status is not TradingStatus.TRADING:
            return cls._no_fill(f"security_{request.session.status.value}")
        adverse_limit = cls._adverse_limit(request)
        fill_time = request.opening_price_proxy_at
        time_quality = ExecutionTimeQuality.DAILY_BAR_OPEN_PROXY
        base_price = request.bar.open
        if adverse_limit is not None:
            if request.limit_handling is LimitHandling.ALLOW_LIMIT_VOLUME:
                if request.bar.available_at > order.valid_until:
                    return cls._no_fill("bar_available_after_order_expiry")
                base_price = adverse_limit
                fill_time = request.bar.available_at
                time_quality = ExecutionTimeQuality.DAILY_BAR_AVAILABLE_AT_PROXY
            elif request.limit_handling is LimitHandling.STRICT_NO_FILL_AT_LIMIT:
                return cls._no_fill("adverse_price_limit")
            else:
                return cls._no_fill(cls._daily_limit_reason(request, adverse_limit))

        fill_price = cls.price_with_slippage(
            base_price,
            side=order.side,
            slippage_bps=request.slippage_bps,
            tick=request.session.price_tick,
        )
        # A price-limit session cannot trade outside its exchange band.  In the
        # explicit optimistic mode, execution friction is already represented by
        # queue/volume participation; adverse price slippage is capped at the
        # legal band instead of making the order reject itself.
        if (
            request.session.upper_limit is not None
            and fill_price.amount > request.session.upper_limit.amount
        ):
            fill_price = request.session.upper_limit
        if (
            request.session.lower_limit is not None
            and fill_price.amount < request.session.lower_limit.amount
        ):
            fill_price = request.session.lower_limit
        if order.side is OrderSide.BUY and fill_price.amount > order.limit_price.amount:
            return cls._no_fill("buy_limit_not_marketable")
        if order.side is OrderSide.SELL and fill_price.amount < order.limit_price.amount:
            return cls._no_fill("sell_limit_not_marketable")

        capacity_result = cls._capacity(request)
        if isinstance(capacity_result, MatchResult):
            return capacity_result
        capacity, capacity_reason_code = capacity_result
        remaining = order.remaining_quantity.value
        # The submitted BUY quantity was validated against the session's
        # declaration rule above. Execution fragments are not new declarations:
        # a valid order may partially fill into an odd-lot position. SELL orders
        # may likewise liquidate the account's actual integer-share balance.
        quantity = min(remaining, capacity)
        if quantity <= 0:
            return cls._no_fill("participation_capacity_zero")

        outcome = MatchOutcome.FILLED if quantity == remaining else MatchOutcome.PARTIALLY_FILLED
        return MatchResult(
            outcome=outcome,
            reason_code="matched_at_limit" if adverse_limit is not None else "matched_at_open",
            quantity=Quantity(quantity),
            price=fill_price,
            filled_at=fill_time,
            capacity_reason_code=capacity_reason_code,
            time_quality=time_quality,
        )

    @classmethod
    def _capacity(cls, request: DailyBarMatchRequest) -> tuple[int, str] | MatchResult:
        if request.capacity_mode is CapacityMode.UNLIMITED:
            return (request.order.remaining_quantity.value, "capacity_unlimited_explicit")

        observation = request.point_in_time_volume
        if observation is None:
            return cls._no_fill("point_in_time_capacity_unknown")
        if observation.quantity.value == 0:
            return cls._no_fill("point_in_time_volume_zero")
        capacity = int(Decimal(observation.quantity.value) * request.participation_rate)
        return capacity, observation.reason_code

    @staticmethod
    def _adverse_limit(request: DailyBarMatchRequest) -> Price | None:
        if (
            request.order.side is OrderSide.BUY
            and request.session.upper_limit is not None
            and request.bar.open.amount >= request.session.upper_limit.amount
        ):
            return request.session.upper_limit
        if (
            request.order.side is OrderSide.SELL
            and request.session.lower_limit is not None
            and request.bar.open.amount <= request.session.lower_limit.amount
        ):
            return request.session.lower_limit
        return None

    @staticmethod
    def _daily_limit_reason(request: DailyBarMatchRequest, limit: Price) -> str:
        values = (
            request.bar.open.amount,
            request.bar.high.amount,
            request.bar.low.amount,
            request.bar.close.amount,
        )
        if all(value == limit.amount for value in values):
            suffix = "up" if request.order.side is OrderSide.BUY else "down"
            return f"one_price_limit_{suffix}"
        return "daily_unlock_timing_unknown"

    @staticmethod
    def price_with_slippage(
        price: Price,
        *,
        side: OrderSide,
        slippage_bps: Decimal,
        tick: Decimal,
    ) -> Price:
        direction = Decimal("1") if side is OrderSide.BUY else Decimal("-1")
        raw = price.amount * (Decimal("1") + direction * slippage_bps / Decimal("10000"))
        rounding = ROUND_UP if side is OrderSide.BUY else ROUND_DOWN
        ticks = (raw / tick).to_integral_value(rounding=rounding)
        return Price(ticks * tick, price.currency)

    @staticmethod
    def _no_fill(reason_code: str) -> MatchResult:
        return MatchResult(outcome=MatchOutcome.NO_FILL, reason_code=reason_code)
