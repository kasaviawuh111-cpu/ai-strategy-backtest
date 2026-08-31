"""Immutable order aggregate, statuses, and audit events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ashare_lab.domain.shared import (
    DecisionId,
    FillId,
    InstrumentId,
    OrderEventId,
    OrderId,
    Price,
    Quantity,
    require_aware,
)

from .exceptions import OrderInvariantError


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKETABLE_LIMIT = "marketable_limit"


class TimeInForce(StrEnum):
    DAY = "day"


class OrderStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }


class OrderEventKind(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_EVENT_TARGET_STATUS = {
    OrderEventKind.CREATED: OrderStatus.CREATED,
    OrderEventKind.SUBMITTED: OrderStatus.SUBMITTED,
    OrderEventKind.ACCEPTED: OrderStatus.ACCEPTED,
    OrderEventKind.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    OrderEventKind.FILLED: OrderStatus.FILLED,
    OrderEventKind.REJECTED: OrderStatus.REJECTED,
    OrderEventKind.CANCELLED: OrderStatus.CANCELLED,
    OrderEventKind.EXPIRED: OrderStatus.EXPIRED,
}

_FILL_EVENT_KINDS = {
    OrderEventKind.PARTIALLY_FILLED,
    OrderEventKind.FILLED,
}

_REASON_EVENT_KINDS = {
    OrderEventKind.REJECTED,
    OrderEventKind.CANCELLED,
    OrderEventKind.EXPIRED,
}


def _require_reason(reason: str | None, field_name: str = "reason_code") -> None:
    if not isinstance(reason, str) or not reason.strip():
        raise OrderInvariantError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class Order:
    """Current immutable snapshot of one order aggregate."""

    order_id: OrderId
    decision_id: DecisionId
    instrument_id: InstrumentId
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Quantity
    limit_price: Price
    created_at: datetime
    valid_from: datetime
    valid_until: datetime
    updated_at: datetime
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: Quantity = field(default_factory=Quantity.zero)
    average_fill_price: Price | None = None
    submitted_at: datetime | None = None
    accepted_at: datetime | None = None
    fill_eligible_at: datetime | None = None
    terminal_at: datetime | None = None
    terminal_reason: str | None = None
    applied_fill_ids: tuple[FillId, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if self.quantity.value <= 0:
            raise OrderInvariantError("order quantity must be greater than zero")
        if self.filled_quantity.value > self.quantity.value:
            raise OrderInvariantError("filled quantity cannot exceed order quantity")
        if type(self.version) is not int:
            raise OrderInvariantError("order version must be an integer")
        if self.version < 1:
            raise OrderInvariantError("order version must be at least one")

        require_aware(self.created_at, "created_at")
        require_aware(self.valid_from, "valid_from")
        require_aware(self.valid_until, "valid_until")
        require_aware(self.updated_at, "updated_at")
        if self.valid_from > self.valid_until:
            raise OrderInvariantError("valid_from cannot be after valid_until")
        if self.created_at > self.valid_until:
            raise OrderInvariantError("order cannot be created after valid_until")
        if self.updated_at < self.created_at:
            raise OrderInvariantError("updated_at cannot be before created_at")

        optional_times = {
            "submitted_at": self.submitted_at,
            "accepted_at": self.accepted_at,
            "fill_eligible_at": self.fill_eligible_at,
            "terminal_at": self.terminal_at,
        }
        for name, value in optional_times.items():
            if value is not None:
                require_aware(value, name)
                if value < self.created_at:
                    raise OrderInvariantError(f"{name} cannot be before created_at")
                # Eligibility is a scheduled future boundary established when
                # the order is accepted; the other timestamps describe events
                # that have already happened to the current snapshot.
                if name != "fill_eligible_at" and value > self.updated_at:
                    raise OrderInvariantError(f"{name} cannot be after updated_at")

        if self.submitted_at is not None and self.submitted_at > self.valid_until:
            raise OrderInvariantError("submitted_at cannot be after valid_until")
        if self.accepted_at is not None:
            if self.submitted_at is None:
                raise OrderInvariantError("accepted order must have submitted_at")
            if self.accepted_at < self.submitted_at:
                raise OrderInvariantError("accepted_at cannot be before submitted_at")
            if self.accepted_at > self.valid_until:
                raise OrderInvariantError("accepted_at cannot be after valid_until")
            if self.fill_eligible_at is None:
                raise OrderInvariantError("accepted order must have fill_eligible_at")
        elif self.fill_eligible_at is not None:
            raise OrderInvariantError("fill_eligible_at requires accepted_at")

        if self.fill_eligible_at is not None:
            if self.fill_eligible_at < self.accepted_at:  # type: ignore[operator]
                raise OrderInvariantError("fill_eligible_at cannot be before accepted_at")
            if self.fill_eligible_at < self.valid_from:
                raise OrderInvariantError("fill_eligible_at cannot be before valid_from")
            if self.fill_eligible_at > self.valid_until:
                raise OrderInvariantError("fill_eligible_at cannot be after valid_until")

        if self.filled_quantity.value == 0:
            if self.average_fill_price is not None:
                raise OrderInvariantError("average_fill_price requires a positive filled quantity")
            if self.applied_fill_ids:
                raise OrderInvariantError("fill ids require a positive filled quantity")
        else:
            if self.accepted_at is None:
                raise OrderInvariantError("a filled order must have been accepted")
            if self.average_fill_price is None:
                raise OrderInvariantError("positive filled quantity requires average_fill_price")
            if self.average_fill_price.currency != self.limit_price.currency:
                raise OrderInvariantError("average fill and limit price currencies must match")
            if not self.applied_fill_ids:
                raise OrderInvariantError("positive filled quantity requires at least one fill id")
        if len(set(self.applied_fill_ids)) != len(self.applied_fill_ids):
            raise OrderInvariantError("applied fill ids must be unique")

        if self.status is OrderStatus.CREATED:
            if self.submitted_at is not None or self.accepted_at is not None:
                raise OrderInvariantError("created order cannot be submitted or accepted")
            if self.version != 1:
                raise OrderInvariantError("created order must have version one")
        elif self.status is OrderStatus.SUBMITTED:
            if self.submitted_at is None or self.accepted_at is not None:
                raise OrderInvariantError(
                    "submitted order requires submitted_at and cannot be accepted"
                )
        elif self.status is OrderStatus.ACCEPTED:
            if self.accepted_at is None or self.filled_quantity.value != 0:
                raise OrderInvariantError("accepted order requires acceptance and no fills")
        elif self.status is OrderStatus.PARTIALLY_FILLED:
            if not 0 < self.filled_quantity.value < self.quantity.value:
                raise OrderInvariantError("partially filled status requires a partial quantity")
        elif self.status is OrderStatus.FILLED:
            if self.filled_quantity != self.quantity:
                raise OrderInvariantError("filled status requires the full requested quantity")
        elif self.status is OrderStatus.REJECTED:
            if self.submitted_at is None or self.accepted_at is not None:
                raise OrderInvariantError(
                    "rejected order must have been submitted but not accepted"
                )
            if self.filled_quantity.value != 0:
                raise OrderInvariantError("rejected order cannot contain fills")
        elif self.status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
            if self.filled_quantity == self.quantity:
                raise OrderInvariantError("fully filled order cannot be cancelled or expired")

        if self.status.is_terminal:
            if self.terminal_at is None:
                raise OrderInvariantError("terminal order requires terminal_at")
            if self.terminal_at != self.updated_at:
                raise OrderInvariantError("terminal_at must equal the final updated_at")
            if self.status is OrderStatus.FILLED:
                if self.terminal_reason is not None:
                    raise OrderInvariantError("filled order cannot have terminal_reason")
            else:
                _require_reason(self.terminal_reason, "terminal_reason")
        elif self.terminal_at is not None or self.terminal_reason is not None:
            raise OrderInvariantError("non-terminal order cannot have terminal metadata")

    @property
    def remaining_quantity(self) -> Quantity:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """One immutable, sequence-numbered transition in an order's audit trail."""

    event_id: OrderEventId
    order_id: OrderId
    sequence: int
    kind: OrderEventKind
    occurred_at: datetime
    previous_status: OrderStatus | None
    status: OrderStatus
    fill_id: FillId | None = None
    fill_quantity: Quantity | None = None
    fill_price: Price | None = None
    fill_eligible_at: datetime | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int:
            raise OrderInvariantError("event sequence must be an integer")
        if self.sequence < 1:
            raise OrderInvariantError("event sequence must be at least one")
        require_aware(self.occurred_at, "occurred_at")

        if self.status is not _EVENT_TARGET_STATUS[self.kind]:
            raise OrderInvariantError("event kind does not match target status")
        if self.kind is OrderEventKind.CREATED:
            if self.previous_status is not None or self.sequence != 1:
                raise OrderInvariantError(
                    "created event must have no previous status and sequence one"
                )
        elif self.previous_status is None:
            raise OrderInvariantError("non-created event must include its previous status")

        if self.kind in _FILL_EVENT_KINDS:
            if self.fill_id is None or self.fill_quantity is None or self.fill_price is None:
                raise OrderInvariantError("fill event requires fill id, quantity, and price")
            if self.fill_quantity.value <= 0:
                raise OrderInvariantError("fill event quantity must be positive")
        elif (
            self.fill_id is not None
            or self.fill_quantity is not None
            or self.fill_price is not None
        ):
            raise OrderInvariantError("non-fill event cannot contain fill data")

        if self.kind is OrderEventKind.ACCEPTED:
            if self.fill_eligible_at is None:
                raise OrderInvariantError("accepted event requires fill_eligible_at")
            require_aware(self.fill_eligible_at, "fill_eligible_at")
            if self.fill_eligible_at < self.occurred_at:
                raise OrderInvariantError("fill_eligible_at cannot be before acceptance")
        elif self.fill_eligible_at is not None:
            raise OrderInvariantError("only an accepted event can carry fill_eligible_at")

        if self.kind in _REASON_EVENT_KINDS:
            _require_reason(self.reason_code)
        elif self.reason_code is not None:
            raise OrderInvariantError("only rejected, cancelled, or expired events carry a reason")


@dataclass(frozen=True, slots=True)
class OrderTransition:
    """The new aggregate snapshot and the single event that produced it."""

    order: Order
    event: OrderEvent

    def __post_init__(self) -> None:
        if self.order.order_id != self.event.order_id:
            raise OrderInvariantError("transition order and event ids must match")
        if self.order.status is not self.event.status:
            raise OrderInvariantError("transition order and event statuses must match")
        if self.order.version != self.event.sequence:
            raise OrderInvariantError("order version must equal event sequence")
        if self.order.updated_at != self.event.occurred_at:
            raise OrderInvariantError("order updated_at must equal event occurred_at")
