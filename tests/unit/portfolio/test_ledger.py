from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import (
    DuplicateFillError,
    FeeBreakdown,
    FillRecord,
    InsufficientCashError,
    InsufficientSellableQuantityError,
    LedgerAccount,
    LedgerComponent,
    LedgerEntry,
    LedgerPosting,
    PortfolioInvariantError,
    PortfolioState,
    PostingSide,
    apply_buy,
    apply_sell,
)
from ashare_lab.domain.shared import (
    FillId,
    InstrumentId,
    Money,
    OrderId,
    Price,
    Quantity,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")


def _money(value: str | int, currency: str = "CNY") -> Money:
    return Money(Decimal(str(value)), currency)


def _fees(
    *,
    commission: str = "0",
    stamp_tax: str = "0",
    transfer_fee: str = "0",
    other: str = "0",
) -> FeeBreakdown:
    return FeeBreakdown(
        commission=_money(commission),
        stamp_tax=_money(stamp_tax),
        transfer_fee=_money(transfer_fee),
        other=_money(other),
    )


def _fill(
    fill_number: int,
    *,
    side: OrderSide,
    quantity: int,
    price: str,
    day: date,
    fees: FeeBreakdown | None = None,
) -> FillRecord:
    return FillRecord(
        fill_id=FillId(f"fill-{fill_number}"),
        order_id=OrderId(f"order-{fill_number}"),
        instrument_id=INSTRUMENT,
        side=side,
        quantity=Quantity(quantity),
        price=Price(Decimal(price), "CNY"),
        filled_at=datetime(day.year, day.month, day.day, 10, tzinfo=SHANGHAI),
        fees=fees or FeeBreakdown.zero(),
    )


def _posting(
    entry: LedgerEntry,
    account: LedgerAccount,
    component: LedgerComponent,
) -> LedgerPosting:
    return next(
        posting
        for posting in entry.postings
        if posting.account is account and posting.component is component
    )


def test_buy_creates_t1_lot_and_capitalizes_separate_fees() -> None:
    initial = PortfolioState(cash=_money("10000"))
    fill = _fill(
        1,
        side=OrderSide.BUY,
        quantity=100,
        price="10",
        day=date(2026, 8, 27),
        fees=_fees(commission="5", transfer_fee="1"),
    )

    updated = apply_buy(initial, fill, sellable_on=date(2026, 8, 28))

    assert initial.cash == _money("10000")
    assert initial.lots == ()
    assert updated.cash == _money("8994")
    assert updated.position_quantity(INSTRUMENT) == Quantity(100)
    lot = updated.lots[0]
    assert lot.acquired_on == date(2026, 8, 27)
    assert lot.sellable_on == date(2026, 8, 28)
    assert lot.cost_basis == _money("1006")

    entry = updated.ledger_entries[0]
    assert entry.debit_total == entry.credit_total == _money("1006")
    assert _posting(
        entry, LedgerAccount.POSITION_ASSET, LedgerComponent.COMMISSION
    ).amount == _money("5")
    assert _posting(
        entry, LedgerAccount.POSITION_ASSET, LedgerComponent.TRANSFER_FEE
    ).amount == _money("1")


def test_buy_rejects_insufficient_cash_without_mutating_input() -> None:
    initial = PortfolioState(cash=_money("999"))
    fill = _fill(
        1,
        side=OrderSide.BUY,
        quantity=100,
        price="10",
        day=date(2026, 8, 27),
    )

    with pytest.raises(InsufficientCashError):
        apply_buy(initial, fill, sellable_on=date(2026, 8, 28))

    assert initial == PortfolioState(cash=_money("999"))


def test_t1_is_enforced_from_each_lots_sellable_date() -> None:
    day = date(2026, 8, 27)
    bought = apply_buy(
        PortfolioState(cash=_money("10000")),
        _fill(1, side=OrderSide.BUY, quantity=100, price="10", day=day),
        sellable_on=day + timedelta(days=1),
    )
    same_day_sale = _fill(2, side=OrderSide.SELL, quantity=1, price="11", day=day)

    with pytest.raises(InsufficientSellableQuantityError):
        apply_sell(bought, same_day_sale)

    next_day_sale = _fill(
        3, side=OrderSide.SELL, quantity=100, price="11", day=day + timedelta(days=1)
    )
    sold = apply_sell(bought, next_day_sale)
    assert sold.position_quantity(INSTRUMENT) == Quantity.zero()


def test_sell_rejects_quantity_above_eligible_inventory() -> None:
    bought = apply_buy(
        PortfolioState(cash=_money("10000")),
        _fill(
            1,
            side=OrderSide.BUY,
            quantity=100,
            price="10",
            day=date(2026, 8, 25),
        ),
        sellable_on=date(2026, 8, 26),
    )

    with pytest.raises(InsufficientSellableQuantityError):
        apply_sell(
            bought,
            _fill(
                2,
                side=OrderSide.SELL,
                quantity=101,
                price="11",
                day=date(2026, 8, 26),
            ),
        )

    assert bought.position_quantity(INSTRUMENT) == Quantity(100)


def test_sell_consumes_eligible_lots_fifo_and_books_realized_pnl() -> None:
    initial = PortfolioState(cash=_money("10000"))
    first = apply_buy(
        initial,
        _fill(
            1,
            side=OrderSide.BUY,
            quantity=100,
            price="10",
            day=date(2026, 8, 25),
        ),
        sellable_on=date(2026, 8, 26),
    )
    second = apply_buy(
        first,
        _fill(
            2,
            side=OrderSide.BUY,
            quantity=100,
            price="12",
            day=date(2026, 8, 26),
        ),
        sellable_on=date(2026, 8, 27),
    )

    sold = apply_sell(
        second,
        _fill(
            3,
            side=OrderSide.SELL,
            quantity=150,
            price="14",
            day=date(2026, 8, 27),
        ),
    )

    assert len(sold.lots) == 1
    remaining = sold.lots[0]
    assert remaining.opened_by_fill_id == FillId("fill-2")
    assert remaining.remaining_quantity == Quantity(50)
    assert remaining.cost_basis == _money("600")

    entry = sold.ledger_entries[-1]
    assert _posting(
        entry, LedgerAccount.POSITION_ASSET, LedgerComponent.COST_BASIS
    ).amount == _money("1600")
    pnl = _posting(entry, LedgerAccount.REALIZED_PNL, LedgerComponent.REALIZED_PNL)
    assert pnl.side is PostingSide.CREDIT
    assert pnl.amount == _money("500")
    assert entry.debit_total == entry.credit_total


def test_sell_keeps_each_fee_in_its_own_expense_account() -> None:
    bought = apply_buy(
        PortfolioState(cash=_money("10000")),
        _fill(
            1,
            side=OrderSide.BUY,
            quantity=100,
            price="10",
            day=date(2026, 8, 25),
        ),
        sellable_on=date(2026, 8, 26),
    )
    sold = apply_sell(
        bought,
        _fill(
            2,
            side=OrderSide.SELL,
            quantity=100,
            price="11",
            day=date(2026, 8, 26),
            fees=_fees(commission="5", stamp_tax="1", transfer_fee="0.2", other="0.3"),
        ),
    )
    entry = sold.ledger_entries[-1]

    assert _posting(
        entry, LedgerAccount.COMMISSION_EXPENSE, LedgerComponent.COMMISSION
    ).amount == _money("5")
    assert _posting(
        entry, LedgerAccount.STAMP_TAX_EXPENSE, LedgerComponent.STAMP_TAX
    ).amount == _money("1")
    assert _posting(
        entry, LedgerAccount.TRANSFER_FEE_EXPENSE, LedgerComponent.TRANSFER_FEE
    ).amount == _money("0.2")
    assert _posting(entry, LedgerAccount.OTHER_FEE_EXPENSE, LedgerComponent.OTHER).amount == _money(
        "0.3"
    )
    assert entry.debit_total == entry.credit_total


def test_partial_fill_and_non_round_lot_integer_remainder_can_be_sold_in_full() -> None:
    """The ledger accepts fill sizes; the execution layer validates order lots."""

    bought = apply_buy(
        PortfolioState(cash=_money("1000")),
        _fill(
            1,
            side=OrderSide.BUY,
            quantity=50,
            price="10",
            day=date(2026, 8, 25),
        ),
        sellable_on=date(2026, 8, 26),
    )
    sold = apply_sell(
        bought,
        _fill(
            2,
            side=OrderSide.SELL,
            quantity=50,
            price="10",
            day=date(2026, 8, 26),
        ),
    )

    assert sold.lots == ()
    assert sold.cash == _money("1000")


def test_duplicate_fill_is_rejected_for_buys_and_sells() -> None:
    initial = PortfolioState(cash=_money("10000"))
    buy_fill = _fill(
        1,
        side=OrderSide.BUY,
        quantity=100,
        price="10",
        day=date(2026, 8, 25),
    )
    bought = apply_buy(initial, buy_fill, sellable_on=date(2026, 8, 26))

    with pytest.raises(DuplicateFillError):
        apply_buy(bought, buy_fill, sellable_on=date(2026, 8, 26))

    sell_fill = _fill(
        2,
        side=OrderSide.SELL,
        quantity=100,
        price="10",
        day=date(2026, 8, 26),
    )
    sold = apply_sell(bought, sell_fill)
    with pytest.raises(DuplicateFillError):
        apply_sell(sold, sell_fill)


def test_ledger_entry_rejects_unbalanced_postings() -> None:
    occurred_at = datetime(2026, 8, 27, 10, tzinfo=SHANGHAI)
    debit = LedgerPosting(
        account=LedgerAccount.CASH,
        side=PostingSide.DEBIT,
        amount=_money("10"),
        component=LedgerComponent.CASH,
    )
    credit = LedgerPosting(
        account=LedgerAccount.POSITION_ASSET,
        side=PostingSide.CREDIT,
        amount=_money("9"),
        component=LedgerComponent.COST_BASIS,
        instrument_id=INSTRUMENT,
    )

    with pytest.raises(PortfolioInvariantError, match="not balanced"):
        LedgerEntry(
            fill_id=FillId("unbalanced-fill"),
            order_id=OrderId("unbalanced-order"),
            occurred_at=occurred_at,
            postings=(debit, credit),
        )


def test_portfolio_snapshots_are_frozen() -> None:
    state = PortfolioState(cash=_money("10000"))
    with pytest.raises(FrozenInstanceError):
        state.cash = _money("0")  # type: ignore[misc]


@given(
    quantity=st.integers(min_value=1, max_value=10_000),
    price_cents=st.integers(min_value=1, max_value=100_000),
    commission_cents=st.integers(min_value=0, max_value=10_000),
    transfer_cents=st.integers(min_value=0, max_value=1_000),
)
def test_property_buy_preserves_cash_plus_capitalized_cost_and_balances_entry(
    quantity: int,
    price_cents: int,
    commission_cents: int,
    transfer_cents: int,
) -> None:
    price = Decimal(price_cents) / Decimal(100)
    commission = Decimal(commission_cents) / Decimal(100)
    transfer = Decimal(transfer_cents) / Decimal(100)
    total = price * quantity + commission + transfer
    initial_cash = total + Decimal("1000")
    initial = PortfolioState(cash=Money(initial_cash, "CNY"))
    fill = _fill(
        1,
        side=OrderSide.BUY,
        quantity=quantity,
        price=str(price),
        day=date(2026, 8, 27),
        fees=_fees(commission=str(commission), transfer_fee=str(transfer)),
    )

    updated = apply_buy(initial, fill, sellable_on=date(2026, 8, 28))

    assert updated.cash + updated.position_cost(INSTRUMENT) == initial.cash
    assert updated.ledger_entries[-1].debit_total == updated.ledger_entries[-1].credit_total


@given(days_until_sellable=st.integers(min_value=1, max_value=30))
def test_property_lot_cannot_sell_before_its_calendar_supplied_t1_boundary(
    days_until_sellable: int,
) -> None:
    bought_on = date(2026, 1, 1)
    sellable_on = bought_on + timedelta(days=days_until_sellable)
    bought = apply_buy(
        PortfolioState(cash=_money("1000")),
        _fill(1, side=OrderSide.BUY, quantity=1, price="10", day=bought_on),
        sellable_on=sellable_on,
    )
    attempt_on = sellable_on - timedelta(days=1)
    attempted_sale = _fill(2, side=OrderSide.SELL, quantity=1, price="10", day=attempt_on)

    with pytest.raises(InsufficientSellableQuantityError):
        apply_sell(bought, attempted_sale)


@given(quantity=st.integers(min_value=1, max_value=1_000))
def test_property_fill_ids_are_idempotency_keys(quantity: int) -> None:
    fill = _fill(
        1,
        side=OrderSide.BUY,
        quantity=quantity,
        price="1",
        day=date(2026, 8, 27),
    )
    bought = apply_buy(
        PortfolioState(cash=_money("100000")),
        fill,
        sellable_on=date(2026, 8, 28),
    )

    with pytest.raises(DuplicateFillError):
        apply_buy(bought, fill, sellable_on=date(2026, 8, 28))
