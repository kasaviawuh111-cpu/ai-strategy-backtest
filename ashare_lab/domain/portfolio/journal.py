"""Balanced double-entry journal values for portfolio fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ashare_lab.domain.shared import (
    FillId,
    InstrumentId,
    Money,
    OrderId,
    require_aware,
)

from .exceptions import PortfolioInvariantError


class PostingSide(StrEnum):
    """The side of a double-entry posting."""

    DEBIT = "debit"
    CREDIT = "credit"


class LedgerAccount(StrEnum):
    """Small chart of accounts used by the backtest portfolio."""

    CASH = "cash"
    POSITION_ASSET = "position_asset"
    COMMISSION_EXPENSE = "commission_expense"
    STAMP_TAX_EXPENSE = "stamp_tax_expense"
    TRANSFER_FEE_EXPENSE = "transfer_fee_expense"
    OTHER_FEE_EXPENSE = "other_fee_expense"
    REALIZED_PNL = "realized_pnl"
    DIVIDEND_INCOME = "dividend_income"
    DIVIDEND_RECEIVABLE = "dividend_receivable"


class LedgerComponent(StrEnum):
    """Economic component retained independently from its ledger account."""

    CASH = "cash"
    PRINCIPAL = "principal"
    COST_BASIS = "cost_basis"
    COMMISSION = "commission"
    STAMP_TAX = "stamp_tax"
    TRANSFER_FEE = "transfer_fee"
    OTHER = "other"
    REALIZED_PNL = "realized_pnl"
    CASH_DIVIDEND = "cash_dividend"


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """One positive debit or credit within a journal entry."""

    account: LedgerAccount
    side: PostingSide
    amount: Money
    component: LedgerComponent
    instrument_id: InstrumentId | None = None

    def __post_init__(self) -> None:
        if self.amount.amount <= 0:
            raise PortfolioInvariantError("posting amount must be greater than zero")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable, self-balancing journal entry produced by a fill."""

    fill_id: FillId
    order_id: OrderId
    occurred_at: datetime
    postings: tuple[LedgerPosting, ...]

    def __post_init__(self) -> None:
        require_aware(self.occurred_at, "occurred_at")
        if len(self.postings) < 2:
            raise PortfolioInvariantError("ledger entry requires at least two postings")

        currencies = {posting.amount.currency for posting in self.postings}
        if len(currencies) != 1:
            raise PortfolioInvariantError("all postings in a ledger entry must use one currency")
        if self.debit_total != self.credit_total:
            raise PortfolioInvariantError(
                "ledger entry is not balanced: total debits must equal total credits"
            )

    @property
    def currency(self) -> str:
        return self.postings[0].amount.currency

    @property
    def debit_total(self) -> Money:
        return self._total_for(PostingSide.DEBIT)

    @property
    def credit_total(self) -> Money:
        return self._total_for(PostingSide.CREDIT)

    def _total_for(self, side: PostingSide) -> Money:
        currency = self.postings[0].amount.currency
        total = Money.zero(currency)
        for posting in self.postings:
            if posting.side is side:
                total += posting.amount
        return total
