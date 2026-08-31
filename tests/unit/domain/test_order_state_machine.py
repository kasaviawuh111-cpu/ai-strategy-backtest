from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ashare_lab.domain.orders import (
    InvalidOrderTransition,
    OrderEventKind,
    OrderInvariantError,
    OrderSide,
    OrderStateMachine,
    OrderStatus,
)
from ashare_lab.domain.shared import (
    CurrencyMismatchError,
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

CN_TZ = timezone(timedelta(hours=8))


class OrderFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.created_at = datetime(2026, 8, 27, 9, 20, tzinfo=CN_TZ)
        self.valid_from = datetime(2026, 8, 27, 9, 30, tzinfo=CN_TZ)
        self.valid_until = datetime(2026, 8, 27, 15, 0, tzinfo=CN_TZ)

    def create_order(
        self,
        *,
        side: OrderSide = OrderSide.BUY,
        quantity: int = 100,
        limit: str = "10.00",
    ):
        return OrderStateMachine.create(
            order_id=OrderId("ord_001"),
            event_id=OrderEventId("oev_001"),
            decision_id=DecisionId("decision_001"),
            instrument_id=InstrumentId("300059.SZ"),
            side=side,
            quantity=Quantity(quantity),
            limit_price=Price(Decimal(limit)),
            created_at=self.created_at,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
        )

    def submit_order(self, order):
        return OrderStateMachine.submit(
            order,
            event_id=OrderEventId(f"oev_{order.version + 1:03d}"),
            submitted_at=self.created_at + timedelta(minutes=1),
        )

    def accept_order(self, order):
        return OrderStateMachine.accept(
            order,
            event_id=OrderEventId(f"oev_{order.version + 1:03d}"),
            accepted_at=self.created_at + timedelta(minutes=2),
            fill_eligible_at=self.valid_from,
        )

    def accepted_order(self, *, side: OrderSide = OrderSide.BUY):
        created = self.create_order(side=side)
        submitted = self.submit_order(created.order)
        return self.accept_order(submitted.order)

    def partially_filled_order(self, *, side: OrderSide = OrderSide.BUY):
        accepted = self.accepted_order(side=side)
        price = "9.90" if side is OrderSide.BUY else "10.10"
        return OrderStateMachine.record_fill(
            accepted.order,
            event_id=OrderEventId("oev_004"),
            fill_id=FillId("fill_001"),
            filled_at=self.valid_from + timedelta(minutes=1),
            quantity=Quantity(40),
            price=Price(Decimal(price)),
        )


class TestSharedValueObjects(unittest.TestCase):
    def test_ids_are_strong_immutable_and_not_cross_type_equal(self) -> None:
        order_id = OrderId("ord_123-ABC")
        self.assertEqual(str(order_id), "ord_123-ABC")
        self.assertNotEqual(order_id, DecisionId("ord_123-ABC"))
        with self.assertRaises(FrozenInstanceError):
            order_id.value = "changed"  # type: ignore[misc]

    def test_ids_reject_empty_whitespace_and_unsupported_characters(self) -> None:
        for value in ("", " ord_1", "ord 1", "订单一"):
            with self.subTest(value=value), self.assertRaises(DomainValidationError):
                OrderId(value)

    def test_money_requires_exact_finite_decimal_and_matching_currency(self) -> None:
        self.assertEqual(
            Money(Decimal("1.20")) + Money(Decimal("2.30")),
            Money(Decimal("3.50")),
        )
        with self.assertRaises(DomainValidationError):
            Money(1.2)  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            Money(Decimal("NaN"))
        with self.assertRaises(CurrencyMismatchError):
            Money(Decimal("1"), "CNY") + Money(Decimal("1"), "USD")

    def test_price_and_quantity_enforce_domain_ranges(self) -> None:
        with self.assertRaises(DomainValidationError):
            Price(Decimal("0"))
        with self.assertRaises(DomainValidationError):
            Quantity(-1)
        with self.assertRaises(DomainValidationError):
            Quantity(True)
        with self.assertRaises(DomainValidationError):
            Quantity(2) - Quantity(3)
        self.assertEqual(Quantity(2) + Quantity(3), Quantity(5))


class TestOrderCreation(OrderFixture):
    def test_create_produces_immutable_version_one_order_and_event(self) -> None:
        transition = self.create_order()
        order = transition.order
        event = transition.event

        self.assertEqual(order.status, OrderStatus.CREATED)
        self.assertEqual(order.version, 1)
        self.assertEqual(order.remaining_quantity, Quantity(100))
        self.assertFalse(order.is_terminal)
        self.assertEqual(event.kind, OrderEventKind.CREATED)
        self.assertIsNone(event.previous_status)
        self.assertEqual(event.sequence, 1)
        self.assertEqual(event.order_id, order.order_id)
        with self.assertRaises(FrozenInstanceError):
            order.status = OrderStatus.SUBMITTED  # type: ignore[misc]

    def test_create_rejects_zero_quantity_invalid_window_and_naive_time(self) -> None:
        with self.assertRaises(OrderInvariantError):
            self.create_order(quantity=0)

        with self.assertRaises(OrderInvariantError):
            OrderStateMachine.create(
                order_id=OrderId("ord_bad_window"),
                event_id=OrderEventId("oev_bad_window"),
                decision_id=DecisionId("decision_bad_window"),
                instrument_id=InstrumentId("300059.SZ"),
                side=OrderSide.BUY,
                quantity=Quantity(100),
                limit_price=Price(Decimal("10")),
                created_at=self.created_at,
                valid_from=self.valid_until,
                valid_until=self.valid_from,
            )

        with self.assertRaises(DomainValidationError):
            OrderStateMachine.create(
                order_id=OrderId("ord_naive"),
                event_id=OrderEventId("oev_naive"),
                decision_id=DecisionId("decision_naive"),
                instrument_id=InstrumentId("300059.SZ"),
                side=OrderSide.BUY,
                quantity=Quantity(100),
                limit_price=Price(Decimal("10")),
                created_at=datetime(2026, 8, 27, 9, 20),
                valid_from=self.valid_from,
                valid_until=self.valid_until,
            )


class TestHappyPathTransitions(OrderFixture):
    def test_submit_accept_partial_fill_and_full_fill(self) -> None:
        created = self.create_order()
        submitted = self.submit_order(created.order)
        accepted = self.accept_order(submitted.order)
        partial = OrderStateMachine.record_fill(
            accepted.order,
            event_id=OrderEventId("oev_004"),
            fill_id=FillId("fill_001"),
            filled_at=self.valid_from + timedelta(minutes=1),
            quantity=Quantity(40),
            price=Price(Decimal("9.90")),
        )
        filled = OrderStateMachine.record_fill(
            partial.order,
            event_id=OrderEventId("oev_005"),
            fill_id=FillId("fill_002"),
            filled_at=self.valid_from + timedelta(minutes=2),
            quantity=Quantity(60),
            price=Price(Decimal("9.80")),
        )

        transitions = [created, submitted, accepted, partial, filled]
        self.assertEqual([item.order.version for item in transitions], [1, 2, 3, 4, 5])
        self.assertEqual([item.event.sequence for item in transitions], [1, 2, 3, 4, 5])
        self.assertEqual(
            [item.order.status for item in transitions],
            [
                OrderStatus.CREATED,
                OrderStatus.SUBMITTED,
                OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            ],
        )
        self.assertEqual(partial.order.remaining_quantity, Quantity(60))
        self.assertEqual(filled.order.remaining_quantity, Quantity.zero())
        self.assertEqual(filled.order.average_fill_price, Price(Decimal("9.84")))
        self.assertEqual(
            filled.order.applied_fill_ids,
            (FillId("fill_001"), FillId("fill_002")),
        )
        self.assertTrue(filled.order.is_terminal)
        self.assertEqual(filled.order.terminal_at, filled.event.occurred_at)
        self.assertIsNone(filled.order.terminal_reason)

    def test_sell_fill_respects_limit_and_can_complete(self) -> None:
        accepted = self.accepted_order(side=OrderSide.SELL)
        filled = OrderStateMachine.record_fill(
            accepted.order,
            event_id=OrderEventId("oev_sell_fill"),
            fill_id=FillId("fill_sell"),
            filled_at=self.valid_from,
            quantity=Quantity(100),
            price=Price(Decimal("10.10")),
        )
        self.assertEqual(filled.order.status, OrderStatus.FILLED)
        self.assertEqual(filled.event.kind, OrderEventKind.FILLED)

    def test_reject_submitted_order(self) -> None:
        submitted = self.submit_order(self.create_order().order)
        rejected = OrderStateMachine.reject(
            submitted.order,
            event_id=OrderEventId("oev_reject"),
            rejected_at=self.created_at + timedelta(minutes=2),
            reason_code="exchange_rejected",
        )
        self.assertEqual(rejected.order.status, OrderStatus.REJECTED)
        self.assertEqual(rejected.order.terminal_reason, "exchange_rejected")
        self.assertEqual(rejected.event.previous_status, OrderStatus.SUBMITTED)
        self.assertEqual(rejected.event.reason_code, "exchange_rejected")

    def test_cancel_preserves_partial_fill(self) -> None:
        partial = self.partially_filled_order()
        cancelled = OrderStateMachine.cancel(
            partial.order,
            event_id=OrderEventId("oev_cancel"),
            cancelled_at=self.valid_from + timedelta(minutes=2),
            reason_code="user_cancelled",
        )
        self.assertEqual(cancelled.order.status, OrderStatus.CANCELLED)
        self.assertEqual(cancelled.order.filled_quantity, Quantity(40))
        self.assertEqual(cancelled.order.remaining_quantity, Quantity(60))
        self.assertEqual(cancelled.event.previous_status, OrderStatus.PARTIALLY_FILLED)

    def test_expire_preserves_partial_fill(self) -> None:
        partial = self.partially_filled_order()
        expired = OrderStateMachine.expire(
            partial.order,
            event_id=OrderEventId("oev_expire"),
            expired_at=self.valid_until,
        )
        self.assertEqual(expired.order.status, OrderStatus.EXPIRED)
        self.assertEqual(expired.order.filled_quantity, Quantity(40))
        self.assertEqual(expired.order.terminal_reason, "validity_elapsed")


class TestAllowedTerminalTransitions(OrderFixture):
    def test_cancel_is_allowed_from_every_working_status(self) -> None:
        created = self.create_order().order
        submitted = self.submit_order(self.create_order().order).order
        accepted = self.accepted_order().order
        partial = self.partially_filled_order().order

        for index, order in enumerate((created, submitted, accepted, partial), start=1):
            with self.subTest(status=order.status):
                transition = OrderStateMachine.cancel(
                    order,
                    event_id=OrderEventId(f"oev_cancel_{index}"),
                    cancelled_at=max(order.updated_at, self.valid_from + timedelta(minutes=2)),
                    reason_code="cancelled_by_policy",
                )
                self.assertEqual(transition.order.status, OrderStatus.CANCELLED)

    def test_expire_is_allowed_from_every_working_status(self) -> None:
        created = self.create_order().order
        submitted = self.submit_order(self.create_order().order).order
        accepted = self.accepted_order().order
        partial = self.partially_filled_order().order

        for index, order in enumerate((created, submitted, accepted, partial), start=1):
            with self.subTest(status=order.status):
                transition = OrderStateMachine.expire(
                    order,
                    event_id=OrderEventId(f"oev_expire_{index}"),
                    expired_at=self.valid_until,
                )
                self.assertEqual(transition.order.status, OrderStatus.EXPIRED)


class TestInvalidTransitions(OrderFixture):
    def test_commands_reject_wrong_source_status(self) -> None:
        created = self.create_order().order
        submitted = self.submit_order(created).order
        accepted = self.accept_order(submitted).order

        invalid_calls = (
            lambda: OrderStateMachine.accept(
                created,
                event_id=OrderEventId("bad_accept"),
                accepted_at=self.created_at + timedelta(minutes=1),
                fill_eligible_at=self.valid_from,
            ),
            lambda: OrderStateMachine.record_fill(
                submitted,
                event_id=OrderEventId("bad_fill"),
                fill_id=FillId("bad_fill"),
                filled_at=self.valid_from,
                quantity=Quantity(1),
                price=Price(Decimal("10")),
            ),
            lambda: OrderStateMachine.reject(
                accepted,
                event_id=OrderEventId("bad_reject"),
                rejected_at=self.valid_from,
                reason_code="too_late",
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(InvalidOrderTransition):
                call()

    def test_terminal_order_cannot_transition_again(self) -> None:
        submitted = self.submit_order(self.create_order().order)
        rejected = OrderStateMachine.reject(
            submitted.order,
            event_id=OrderEventId("oev_rejected"),
            rejected_at=self.created_at + timedelta(minutes=2),
            reason_code="exchange_rejected",
        )
        with self.assertRaises(InvalidOrderTransition):
            OrderStateMachine.expire(
                rejected.order,
                event_id=OrderEventId("oev_after_terminal"),
                expired_at=self.valid_until,
            )

    def test_empty_terminal_reason_is_rejected(self) -> None:
        submitted = self.submit_order(self.create_order().order)
        with self.assertRaises(OrderInvariantError):
            OrderStateMachine.reject(
                submitted.order,
                event_id=OrderEventId("oev_empty_reason"),
                rejected_at=self.created_at + timedelta(minutes=2),
                reason_code="  ",
            )


class TestTemporalInvariants(OrderFixture):
    def test_transition_time_cannot_move_backwards(self) -> None:
        submitted = self.submit_order(self.create_order().order)
        with self.assertRaises(OrderInvariantError):
            OrderStateMachine.accept(
                submitted.order,
                event_id=OrderEventId("oev_backwards"),
                accepted_at=self.created_at,
                fill_eligible_at=self.valid_from,
            )

    def test_acceptance_requires_eligible_time_inside_validity_window(self) -> None:
        submitted = self.submit_order(self.create_order().order)
        invalid_eligible_times = (
            self.created_at,
            self.valid_until + timedelta(seconds=1),
        )
        for index, eligible_at in enumerate(invalid_eligible_times):
            with self.subTest(eligible_at=eligible_at), self.assertRaises(OrderInvariantError):
                OrderStateMachine.accept(
                    submitted.order,
                    event_id=OrderEventId(f"oev_bad_eligible_{index}"),
                    accepted_at=self.created_at + timedelta(minutes=2),
                    fill_eligible_at=eligible_at,
                )

    def test_fill_cannot_precede_eligibility_or_follow_expiry(self) -> None:
        accepted = self.accepted_order()
        for index, filled_at in enumerate(
            (self.valid_from - timedelta(seconds=1), self.valid_until + timedelta(seconds=1))
        ):
            with self.subTest(filled_at=filled_at), self.assertRaises(OrderInvariantError):
                OrderStateMachine.record_fill(
                    accepted.order,
                    event_id=OrderEventId(f"oev_bad_time_{index}"),
                    fill_id=FillId(f"fill_bad_time_{index}"),
                    filled_at=filled_at,
                    quantity=Quantity(1),
                    price=Price(Decimal("10")),
                )

    def test_expire_not_before_valid_until_and_cancel_not_after(self) -> None:
        created = self.create_order().order
        with self.assertRaises(OrderInvariantError):
            OrderStateMachine.expire(
                created,
                event_id=OrderEventId("oev_early_expire"),
                expired_at=self.valid_until - timedelta(seconds=1),
            )
        with self.assertRaises(OrderInvariantError):
            OrderStateMachine.cancel(
                created,
                event_id=OrderEventId("oev_late_cancel"),
                cancelled_at=self.valid_until + timedelta(seconds=1),
                reason_code="late_cancel",
            )


class TestFillInvariants(OrderFixture):
    def test_fill_quantity_must_be_positive_and_not_overfill(self) -> None:
        accepted = self.accepted_order()
        for index, quantity in enumerate((Quantity.zero(), Quantity(101))):
            with self.subTest(quantity=quantity), self.assertRaises(OrderInvariantError):
                OrderStateMachine.record_fill(
                    accepted.order,
                    event_id=OrderEventId(f"oev_bad_qty_{index}"),
                    fill_id=FillId(f"fill_bad_qty_{index}"),
                    filled_at=self.valid_from,
                    quantity=quantity,
                    price=Price(Decimal("10")),
                )

    def test_duplicate_fill_id_is_rejected(self) -> None:
        partial = self.partially_filled_order()
        with self.assertRaises(OrderInvariantError):
            OrderStateMachine.record_fill(
                partial.order,
                event_id=OrderEventId("oev_duplicate_fill"),
                fill_id=FillId("fill_001"),
                filled_at=self.valid_from + timedelta(minutes=2),
                quantity=Quantity(1),
                price=Price(Decimal("9.90")),
            )

    def test_limit_price_and_currency_are_enforced(self) -> None:
        buy = self.accepted_order(side=OrderSide.BUY)
        sell = self.accepted_order(side=OrderSide.SELL)
        cases = (
            (buy.order, Price(Decimal("10.01")), "buy_above_limit"),
            (sell.order, Price(Decimal("9.99")), "sell_below_limit"),
            (buy.order, Price(Decimal("10.00"), "USD"), "wrong_currency"),
        )
        for index, (order, price, name) in enumerate(cases):
            with self.subTest(case=name), self.assertRaises(OrderInvariantError):
                OrderStateMachine.record_fill(
                    order,
                    event_id=OrderEventId(f"oev_bad_price_{index}"),
                    fill_id=FillId(f"fill_bad_price_{index}"),
                    filled_at=self.valid_from,
                    quantity=Quantity(1),
                    price=price,
                )


class TestSnapshotAndEventInvariants(OrderFixture):
    def test_directly_constructed_inconsistent_snapshot_is_rejected(self) -> None:
        created = self.create_order().order
        with self.assertRaises(OrderInvariantError):
            replace(created, filled_quantity=Quantity(1))
        with self.assertRaises(OrderInvariantError):
            replace(created, status=OrderStatus.FILLED)
        with self.assertRaises(OrderInvariantError):
            replace(created, version=2)

    def test_event_kind_status_sequence_and_payload_are_validated(self) -> None:
        event = self.create_order().event
        with self.assertRaises(OrderInvariantError):
            replace(event, status=OrderStatus.SUBMITTED)
        with self.assertRaises(OrderInvariantError):
            replace(event, sequence=2)
        with self.assertRaises(OrderInvariantError):
            replace(event, fill_quantity=Quantity(1))


if __name__ == "__main__":
    unittest.main()
