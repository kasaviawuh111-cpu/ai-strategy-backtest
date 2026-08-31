"""The sole authority for changing an order aggregate's state."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

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

from .exceptions import InvalidOrderTransition, OrderInvariantError
from .models import (
    Order,
    OrderEvent,
    OrderEventKind,
    OrderSide,
    OrderStatus,
    OrderTransition,
    OrderType,
    TimeInForce,
)


class OrderStateMachine:
    """Pure transition functions for immutable orders.

    Event and aggregate IDs are supplied by the caller. The domain never calls
    ``uuid4`` or the wall clock, which keeps replays deterministic.
    """

    @staticmethod
    def create(
        *,
        order_id: OrderId,
        event_id: OrderEventId,
        decision_id: DecisionId,
        instrument_id: InstrumentId,
        side: OrderSide,
        quantity: Quantity,
        limit_price: Price,
        created_at: datetime,
        valid_from: datetime,
        valid_until: datetime,
        order_type: OrderType = OrderType.MARKETABLE_LIMIT,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> OrderTransition:
        order = Order(
            order_id=order_id,
            decision_id=decision_id,
            instrument_id=instrument_id,
            side=side,
            order_type=order_type,
            time_in_force=time_in_force,
            quantity=quantity,
            limit_price=limit_price,
            created_at=created_at,
            valid_from=valid_from,
            valid_until=valid_until,
            updated_at=created_at,
        )
        event = OrderEvent(
            event_id=event_id,
            order_id=order_id,
            sequence=1,
            kind=OrderEventKind.CREATED,
            occurred_at=created_at,
            previous_status=None,
            status=OrderStatus.CREATED,
        )
        return OrderTransition(order=order, event=event)

    @staticmethod
    def submit(
        order: Order,
        *,
        event_id: OrderEventId,
        submitted_at: datetime,
    ) -> OrderTransition:
        OrderStateMachine._require_status(order, {OrderStatus.CREATED}, "submit")
        OrderStateMachine._require_transition_time(order, submitted_at)
        if submitted_at > order.valid_until:
            raise OrderInvariantError("cannot submit an order after valid_until")
        updated = replace(
            order,
            status=OrderStatus.SUBMITTED,
            submitted_at=submitted_at,
            updated_at=submitted_at,
            version=order.version + 1,
        )
        return OrderStateMachine._transition(
            order,
            updated,
            event_id=event_id,
            kind=OrderEventKind.SUBMITTED,
            occurred_at=submitted_at,
        )

    @staticmethod
    def accept(
        order: Order,
        *,
        event_id: OrderEventId,
        accepted_at: datetime,
        fill_eligible_at: datetime,
    ) -> OrderTransition:
        OrderStateMachine._require_status(order, {OrderStatus.SUBMITTED}, "accept")
        OrderStateMachine._require_transition_time(order, accepted_at)
        require_aware(fill_eligible_at, "fill_eligible_at")
        if accepted_at > order.valid_until:
            raise OrderInvariantError("cannot accept an order after valid_until")
        if fill_eligible_at < accepted_at:
            raise OrderInvariantError("fill_eligible_at cannot be before accepted_at")
        if fill_eligible_at < order.valid_from:
            raise OrderInvariantError("fill_eligible_at cannot be before valid_from")
        if fill_eligible_at > order.valid_until:
            raise OrderInvariantError("fill_eligible_at cannot be after valid_until")
        updated = replace(
            order,
            status=OrderStatus.ACCEPTED,
            accepted_at=accepted_at,
            fill_eligible_at=fill_eligible_at,
            updated_at=accepted_at,
            version=order.version + 1,
        )
        return OrderStateMachine._transition(
            order,
            updated,
            event_id=event_id,
            kind=OrderEventKind.ACCEPTED,
            occurred_at=accepted_at,
            fill_eligible_at=fill_eligible_at,
        )

    @staticmethod
    def reject(
        order: Order,
        *,
        event_id: OrderEventId,
        rejected_at: datetime,
        reason_code: str,
    ) -> OrderTransition:
        OrderStateMachine._require_status(order, {OrderStatus.SUBMITTED}, "reject")
        OrderStateMachine._require_transition_time(order, rejected_at)
        updated = replace(
            order,
            status=OrderStatus.REJECTED,
            terminal_at=rejected_at,
            terminal_reason=reason_code,
            updated_at=rejected_at,
            version=order.version + 1,
        )
        return OrderStateMachine._transition(
            order,
            updated,
            event_id=event_id,
            kind=OrderEventKind.REJECTED,
            occurred_at=rejected_at,
            reason_code=reason_code,
        )

    @staticmethod
    def record_fill(
        order: Order,
        *,
        event_id: OrderEventId,
        fill_id: FillId,
        filled_at: datetime,
        quantity: Quantity,
        price: Price,
    ) -> OrderTransition:
        OrderStateMachine._require_status(
            order,
            {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED},
            "record fill for",
        )
        OrderStateMachine._require_transition_time(order, filled_at)
        if order.fill_eligible_at is None:
            raise OrderInvariantError("accepted order is missing fill_eligible_at")
        if filled_at < order.fill_eligible_at:
            raise OrderInvariantError("fill cannot occur before fill_eligible_at")
        if filled_at > order.valid_until:
            raise OrderInvariantError("fill cannot occur after valid_until")
        if quantity.value <= 0:
            raise OrderInvariantError("fill quantity must be greater than zero")
        if quantity.value > order.remaining_quantity.value:
            raise OrderInvariantError("fill quantity exceeds remaining order quantity")
        if fill_id in order.applied_fill_ids:
            raise OrderInvariantError(f"fill {fill_id} has already been applied")
        if price.currency != order.limit_price.currency:
            raise OrderInvariantError("fill and limit price currencies must match")
        if order.side is OrderSide.BUY and price.amount > order.limit_price.amount:
            raise OrderInvariantError("buy fill price cannot exceed its limit price")
        if order.side is OrderSide.SELL and price.amount < order.limit_price.amount:
            raise OrderInvariantError("sell fill price cannot be below its limit price")

        old_quantity = order.filled_quantity.value
        added_quantity = quantity.value
        new_quantity = old_quantity + added_quantity
        if order.average_fill_price is None:
            average_amount = price.amount
        else:
            average_amount = (
                order.average_fill_price.amount * Decimal(old_quantity)
                + price.amount * Decimal(added_quantity)
            ) / Decimal(new_quantity)
        average_price = Price(average_amount, price.currency)
        new_filled_quantity = Quantity(new_quantity)
        is_complete = new_filled_quantity == order.quantity
        new_status = OrderStatus.FILLED if is_complete else OrderStatus.PARTIALLY_FILLED
        kind = OrderEventKind.FILLED if is_complete else OrderEventKind.PARTIALLY_FILLED
        updated = replace(
            order,
            status=new_status,
            filled_quantity=new_filled_quantity,
            average_fill_price=average_price,
            terminal_at=filled_at if is_complete else None,
            updated_at=filled_at,
            applied_fill_ids=(*order.applied_fill_ids, fill_id),
            version=order.version + 1,
        )
        return OrderStateMachine._transition(
            order,
            updated,
            event_id=event_id,
            kind=kind,
            occurred_at=filled_at,
            fill_id=fill_id,
            fill_quantity=quantity,
            fill_price=price,
        )

    @staticmethod
    def cancel(
        order: Order,
        *,
        event_id: OrderEventId,
        cancelled_at: datetime,
        reason_code: str,
    ) -> OrderTransition:
        OrderStateMachine._require_status(
            order,
            {
                OrderStatus.CREATED,
                OrderStatus.SUBMITTED,
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
            },
            "cancel",
        )
        OrderStateMachine._require_transition_time(order, cancelled_at)
        if cancelled_at > order.valid_until:
            raise OrderInvariantError(
                "order past valid_until must expire instead of being cancelled"
            )
        updated = replace(
            order,
            status=OrderStatus.CANCELLED,
            terminal_at=cancelled_at,
            terminal_reason=reason_code,
            updated_at=cancelled_at,
            version=order.version + 1,
        )
        return OrderStateMachine._transition(
            order,
            updated,
            event_id=event_id,
            kind=OrderEventKind.CANCELLED,
            occurred_at=cancelled_at,
            reason_code=reason_code,
        )

    @staticmethod
    def expire(
        order: Order,
        *,
        event_id: OrderEventId,
        expired_at: datetime,
        reason_code: str = "validity_elapsed",
    ) -> OrderTransition:
        OrderStateMachine._require_status(
            order,
            {
                OrderStatus.CREATED,
                OrderStatus.SUBMITTED,
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
            },
            "expire",
        )
        OrderStateMachine._require_transition_time(order, expired_at)
        if expired_at < order.valid_until:
            raise OrderInvariantError("cannot expire an order before valid_until")
        updated = replace(
            order,
            status=OrderStatus.EXPIRED,
            terminal_at=expired_at,
            terminal_reason=reason_code,
            updated_at=expired_at,
            version=order.version + 1,
        )
        return OrderStateMachine._transition(
            order,
            updated,
            event_id=event_id,
            kind=OrderEventKind.EXPIRED,
            occurred_at=expired_at,
            reason_code=reason_code,
        )

    @staticmethod
    def _require_status(
        order: Order,
        allowed: set[OrderStatus],
        action: str,
    ) -> None:
        if order.status not in allowed:
            allowed_text = ", ".join(sorted(status.value for status in allowed))
            raise InvalidOrderTransition(
                f"cannot {action} order in {order.status.value}; allowed statuses: {allowed_text}"
            )

    @staticmethod
    def _require_transition_time(order: Order, occurred_at: datetime) -> None:
        require_aware(occurred_at, "transition time")
        if occurred_at < order.updated_at:
            raise OrderInvariantError("transition time cannot be before the previous order update")

    @staticmethod
    def _transition(
        previous: Order,
        updated: Order,
        *,
        event_id: OrderEventId,
        kind: OrderEventKind,
        occurred_at: datetime,
        fill_id: FillId | None = None,
        fill_quantity: Quantity | None = None,
        fill_price: Price | None = None,
        fill_eligible_at: datetime | None = None,
        reason_code: str | None = None,
    ) -> OrderTransition:
        event = OrderEvent(
            event_id=event_id,
            order_id=updated.order_id,
            sequence=updated.version,
            kind=kind,
            occurred_at=occurred_at,
            previous_status=previous.status,
            status=updated.status,
            fill_id=fill_id,
            fill_quantity=fill_quantity,
            fill_price=fill_price,
            fill_eligible_at=fill_eligible_at,
            reason_code=reason_code,
        )
        return OrderTransition(order=updated, event=event)
