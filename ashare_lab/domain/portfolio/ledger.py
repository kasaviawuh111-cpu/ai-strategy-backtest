"""Pure portfolio transitions for applying externally matched fills."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import InstrumentId, Money, Quantity

from .exceptions import (
    DuplicateFillError,
    InsufficientCashError,
    InsufficientSellableQuantityError,
    InvalidFillSideError,
    PortfolioInvariantError,
)
from .journal import (
    LedgerAccount,
    LedgerComponent,
    LedgerEntry,
    LedgerPosting,
    PostingSide,
)
from .models import FeeBreakdown, FeeKind, FillRecord, PortfolioState, PositionLot

_FEE_COMPONENT = {
    FeeKind.COMMISSION: LedgerComponent.COMMISSION,
    FeeKind.STAMP_TAX: LedgerComponent.STAMP_TAX,
    FeeKind.TRANSFER_FEE: LedgerComponent.TRANSFER_FEE,
    FeeKind.OTHER: LedgerComponent.OTHER,
}

_FEE_EXPENSE_ACCOUNT = {
    FeeKind.COMMISSION: LedgerAccount.COMMISSION_EXPENSE,
    FeeKind.STAMP_TAX: LedgerAccount.STAMP_TAX_EXPENSE,
    FeeKind.TRANSFER_FEE: LedgerAccount.TRANSFER_FEE_EXPENSE,
    FeeKind.OTHER: LedgerAccount.OTHER_FEE_EXPENSE,
}


def apply_buy(
    state: PortfolioState,
    fill: FillRecord,
    *,
    sellable_on: date,
) -> PortfolioState:
    """Apply one buy fill and create a new T+1 lot without mutating ``state``.

    ``sellable_on`` is supplied by the trading-calendar/execution layer. This
    keeps holidays and exchange calendar versions out of the accounting model.
    Fill quantities are deliberately not checked for board lots: a valid
    round-lot order can receive partial fills, while order-size validation
    belongs to execution.
    """

    _require_fill(state, fill, OrderSide.BUY)
    if type(sellable_on) is not date:
        raise PortfolioInvariantError("sellable_on must be a date")
    if sellable_on <= fill.trading_date:
        raise PortfolioInvariantError("A-share buy lot must become sellable after its trading date")

    acquisition_cost = fill.gross_amount + fill.fees.total
    if state.cash.amount < acquisition_cost.amount:
        raise InsufficientCashError(
            f"buy requires {acquisition_cost.amount} {acquisition_cost.currency}, "
            f"but only {state.cash.amount} is available"
        )

    lot = PositionLot(
        opened_by_fill_id=fill.fill_id,
        instrument_id=fill.instrument_id,
        acquired_at=fill.filled_at,
        acquired_on=fill.trading_date,
        sellable_on=sellable_on,
        remaining_quantity=fill.quantity,
        cost_basis=acquisition_cost,
        acquisition_principal=fill.gross_amount,
    )
    entry = _buy_entry(fill)
    return PortfolioState(
        cash=state.cash - acquisition_cost,
        lots=(*state.lots, lot),
        fills=(*state.fills, fill),
        ledger_entries=(*state.ledger_entries, entry),
        corporate_action_entries=state.corporate_action_entries,
        corporate_action_entitlements=state.corporate_action_entitlements,
    )


def apply_sell(state: PortfolioState, fill: FillRecord) -> PortfolioState:
    """Apply one sell fill against eligible lots in deterministic FIFO order."""

    _require_fill(state, fill, OrderSide.SELL)
    eligible = [
        (index, lot)
        for index, lot in enumerate(state.lots)
        if lot.instrument_id == fill.instrument_id and lot.sellable_on <= fill.trading_date
    ]
    eligible.sort(
        key=lambda item: (
            item[1].acquired_at,
            item[1].opened_by_fill_id.value,
            item[0],
        )
    )
    sellable = sum(lot.remaining_quantity.value for _, lot in eligible)
    if fill.quantity.value > sellable:
        raise InsufficientSellableQuantityError(
            f"sell requests {fill.quantity.value} shares, but only {sellable} are T+1 eligible"
        )

    remaining_to_sell = fill.quantity.value
    replacements: dict[int, PositionLot | None] = {}
    sold_cost = Money.zero(state.cash.currency)
    for index, lot in eligible:
        if remaining_to_sell == 0:
            break
        consumed = min(remaining_to_sell, lot.remaining_quantity.value)
        if consumed == lot.remaining_quantity.value:
            allocated_cost = lot.cost_basis
            replacements[index] = None
        else:
            allocated_cost = Money(
                lot.cost_basis.amount * Decimal(consumed) / Decimal(lot.remaining_quantity.value),
                lot.cost_basis.currency,
            )
            replacements[index] = replace(
                lot,
                remaining_quantity=Quantity(lot.remaining_quantity.value - consumed),
                cost_basis=lot.cost_basis - allocated_cost,
                acquisition_principal=(Money(
                    lot.acquisition_principal.amount * Decimal(lot.remaining_quantity.value - consumed)
                    / Decimal(lot.remaining_quantity.value), lot.acquisition_principal.currency
                ) if lot.acquisition_principal is not None else None),
            )
        sold_cost += allocated_cost
        remaining_to_sell -= consumed

    surviving_lots_list: list[PositionLot] = []
    for index, lot in enumerate(state.lots):
        replacement = replacements.get(index, lot)
        if replacement is not None:
            surviving_lots_list.append(replacement)
    surviving_lots = tuple(surviving_lots_list)

    cash_after = state.cash + fill.gross_amount - fill.fees.total
    if cash_after.amount < 0:
        raise InsufficientCashError(
            "sale proceeds plus available cash do not cover caller-supplied fees"
        )
    entry = _sell_entry(fill, sold_cost)
    return PortfolioState(
        cash=cash_after,
        lots=surviving_lots,
        fills=(*state.fills, fill),
        ledger_entries=(*state.ledger_entries, entry),
        corporate_action_entries=state.corporate_action_entries,
        corporate_action_entitlements=state.corporate_action_entitlements,
    )


def _require_fill(state: PortfolioState, fill: FillRecord, expected_side: OrderSide) -> None:
    if fill.side is not expected_side:
        raise InvalidFillSideError(f"expected a {expected_side.value} fill, got {fill.side.value}")
    if fill.fill_id in state.applied_fill_ids:
        raise DuplicateFillError(f"fill {fill.fill_id} has already been applied")
    if fill.price.currency != state.cash.currency:
        raise PortfolioInvariantError("fill and portfolio cash currencies must match")


def _buy_entry(fill: FillRecord) -> LedgerEntry:
    postings = [
        LedgerPosting(
            account=LedgerAccount.POSITION_ASSET,
            side=PostingSide.DEBIT,
            amount=fill.gross_amount,
            component=LedgerComponent.PRINCIPAL,
            instrument_id=fill.instrument_id,
        )
    ]
    postings.extend(_capitalized_fee_postings(fill))
    postings.append(
        LedgerPosting(
            account=LedgerAccount.CASH,
            side=PostingSide.CREDIT,
            amount=fill.gross_amount + fill.fees.total,
            component=LedgerComponent.CASH,
        )
    )
    return LedgerEntry(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        occurred_at=fill.filled_at,
        postings=tuple(postings),
    )


def _sell_entry(fill: FillRecord, sold_cost: Money) -> LedgerEntry:
    postings: list[LedgerPosting] = []
    cash_delta = fill.gross_amount - fill.fees.total
    _append_signed_posting(
        postings,
        amount=cash_delta,
        positive_side=PostingSide.DEBIT,
        account=LedgerAccount.CASH,
        component=LedgerComponent.CASH,
    )
    postings.extend(_expense_fee_postings(fill.fees, fill.instrument_id))
    postings.append(
        LedgerPosting(
            account=LedgerAccount.POSITION_ASSET,
            side=PostingSide.CREDIT,
            amount=sold_cost,
            component=LedgerComponent.COST_BASIS,
            instrument_id=fill.instrument_id,
        )
    )
    realized = fill.gross_amount - sold_cost
    _append_signed_posting(
        postings,
        amount=realized,
        positive_side=PostingSide.CREDIT,
        account=LedgerAccount.REALIZED_PNL,
        component=LedgerComponent.REALIZED_PNL,
        instrument_id=fill.instrument_id,
    )
    return LedgerEntry(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        occurred_at=fill.filled_at,
        postings=tuple(postings),
    )


def _capitalized_fee_postings(fill: FillRecord) -> list[LedgerPosting]:
    return [
        LedgerPosting(
            account=LedgerAccount.POSITION_ASSET,
            side=PostingSide.DEBIT,
            amount=amount,
            component=_FEE_COMPONENT[kind],
            instrument_id=fill.instrument_id,
        )
        for kind, amount in fill.fees.items()
        if amount.amount > 0
    ]


def _expense_fee_postings(fees: FeeBreakdown, instrument_id: InstrumentId) -> list[LedgerPosting]:
    return [
        LedgerPosting(
            account=_FEE_EXPENSE_ACCOUNT[kind],
            side=PostingSide.DEBIT,
            amount=amount,
            component=_FEE_COMPONENT[kind],
            instrument_id=instrument_id,
        )
        for kind, amount in fees.items()
        if amount.amount > 0
    ]


def _append_signed_posting(
    postings: list[LedgerPosting],
    *,
    amount: Money,
    positive_side: PostingSide,
    account: LedgerAccount,
    component: LedgerComponent,
    instrument_id: InstrumentId | None = None,
) -> None:
    if amount.amount == 0:
        return
    side = positive_side
    posting_amount = amount
    if amount.amount < 0:
        side = PostingSide.CREDIT if positive_side is PostingSide.DEBIT else PostingSide.DEBIT
        posting_amount = Money(-amount.amount, amount.currency)
    postings.append(
        LedgerPosting(
            account=account,
            side=side,
            amount=posting_amount,
            component=component,
            instrument_id=instrument_id,
        )
    )
