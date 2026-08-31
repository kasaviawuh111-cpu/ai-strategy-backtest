"""Immutable portfolio, fill, fee, and position-lot values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import CorporateActionKind
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import (
    FillId,
    InstrumentId,
    Money,
    OrderId,
    Price,
    Quantity,
    StrongId,
    require_aware,
)

from .exceptions import PortfolioInvariantError
from .journal import LedgerEntry, LedgerPosting, PostingSide

_SHANGHAI_TIME = ZoneInfo("Asia/Shanghai")


class FeeKind(StrEnum):
    """Fee dimensions retained independently for audit and reporting."""

    COMMISSION = "commission"
    STAMP_TAX = "stamp_tax"
    TRANSFER_FEE = "transfer_fee"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    """Caller-calculated fees; this domain never embeds broker fee rates."""

    commission: Money
    stamp_tax: Money
    transfer_fee: Money
    other: Money

    def __post_init__(self) -> None:
        values = tuple(amount for _, amount in self.items())
        currencies = {amount.currency for amount in values}
        if len(currencies) != 1:
            raise PortfolioInvariantError("all fee components must use one currency")
        if any(amount.amount < 0 for amount in values):
            raise PortfolioInvariantError("fee components cannot be negative")

    @classmethod
    def zero(cls, currency: str = "CNY") -> FeeBreakdown:
        zero = Money.zero(currency)
        return cls(zero, zero, zero, zero)

    @property
    def currency(self) -> str:
        return self.commission.currency

    @property
    def total(self) -> Money:
        return self.commission + self.stamp_tax + self.transfer_fee + self.other

    def items(self) -> tuple[tuple[FeeKind, Money], ...]:
        return (
            (FeeKind.COMMISSION, self.commission),
            (FeeKind.STAMP_TAX, self.stamp_tax),
            (FeeKind.TRANSFER_FEE, self.transfer_fee),
            (FeeKind.OTHER, self.other),
        )


@dataclass(frozen=True, slots=True)
class FillRecord:
    """One executed fill, including externally calculated fee components."""

    fill_id: FillId
    order_id: OrderId
    instrument_id: InstrumentId
    side: OrderSide
    quantity: Quantity
    price: Price
    filled_at: datetime
    fees: FeeBreakdown

    def __post_init__(self) -> None:
        if self.quantity.value <= 0:
            raise PortfolioInvariantError("fill quantity must be greater than zero")
        require_aware(self.filled_at, "filled_at")
        if self.fees.currency != self.price.currency:
            raise PortfolioInvariantError("fill price and fees must use one currency")

    @property
    def trading_date(self) -> date:
        """Exchange-local date, independent of the timestamp's input offset."""

        return self.filled_at.astimezone(_SHANGHAI_TIME).date()

    @property
    def gross_amount(self) -> Money:
        return self.price * self.quantity.value


@dataclass(frozen=True, slots=True)
class PositionLot:
    """One FIFO acquisition lot with its own A-share T+1 availability date."""

    opened_by_fill_id: FillId
    instrument_id: InstrumentId
    acquired_at: datetime
    acquired_on: date
    sellable_on: date
    remaining_quantity: Quantity
    cost_basis: Money

    def __post_init__(self) -> None:
        require_aware(self.acquired_at, "acquired_at")
        if type(self.acquired_on) is not date:  # datetime is also a date subclass
            raise PortfolioInvariantError("acquired_on must be a date")
        if type(self.sellable_on) is not date:
            raise PortfolioInvariantError("sellable_on must be a date")
        local_acquisition_date = self.acquired_at.astimezone(_SHANGHAI_TIME).date()
        if self.acquired_on != local_acquisition_date:
            raise PortfolioInvariantError("acquired_on must match acquired_at in Asia/Shanghai")
        if self.sellable_on < self.acquired_on:
            raise PortfolioInvariantError("sellable_on cannot precede the acquisition date")
        if self.remaining_quantity.value <= 0:
            raise PortfolioInvariantError("position lot quantity must be positive")
        if self.cost_basis.amount < 0:
            raise PortfolioInvariantError("position lot cost basis cannot be negative")


class CorporateActionPhase(StrEnum):
    ENTITLEMENT = "entitlement"
    ACCRUAL = "accrual"
    SETTLEMENT = "settlement"
    DECLINED = "declined"


class CorporateActionEntitlementStatus(StrEnum):
    CAPTURED = "captured"
    ACCRUED = "accrued"
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class CorporateActionEntitlement:
    """Record-date entitlement carried independently of later holdings."""

    action_id: StrongId
    revision_no: int
    instrument_id: InstrumentId
    action_type: CorporateActionKind
    record_date: date
    ex_date: date
    settlement_date: date
    entitled_quantity: Quantity
    cash_amount: Money
    share_delta: int
    share_sellable_date: date | None
    status: CorporateActionEntitlementStatus
    captured_at: datetime
    accrued_at: datetime | None = None
    settled_at: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.captured_at, "captured_at")
        for field_name in ("accrued_at", "settled_at"):
            value = getattr(self, field_name)
            if value is not None:
                require_aware(value, field_name)
        if self.revision_no < 0:
            raise PortfolioInvariantError("corporate-action revision cannot be negative")
        if not self.record_date <= self.ex_date <= self.settlement_date:
            raise PortfolioInvariantError("corporate-action entitlement dates are inconsistent")
        if self.cash_amount.amount < 0:
            raise PortfolioInvariantError("corporate-action cash entitlement cannot be negative")
        if self.status is CorporateActionEntitlementStatus.CAPTURED:
            if self.accrued_at is not None or self.settled_at is not None:
                raise PortfolioInvariantError("captured entitlement cannot have later clocks")
        elif self.status is CorporateActionEntitlementStatus.ACCRUED:
            if self.accrued_at is None or self.settled_at is not None:
                raise PortfolioInvariantError("accrued entitlement clocks are inconsistent")
        elif self.accrued_at is None or self.settled_at is None:
            raise PortfolioInvariantError(
                "settled entitlement requires accrual and settlement clocks"
            )


@dataclass(frozen=True, slots=True)
class CorporateActionLedgerEntry:
    """Auditable before/after state for one corporate-action application."""

    action_id: StrongId
    revision_no: int
    instrument_id: InstrumentId
    action_type: CorporateActionKind
    phase: CorporateActionPhase
    occurred_at: datetime
    cash_before: Money
    cash_after: Money
    quantity_before: Quantity
    quantity_after: Quantity
    position_cost_before: Money
    position_cost_after: Money
    receivable_before: Money
    receivable_after: Money
    share_entitlement_delta_before: int
    share_entitlement_delta_after: int
    postings: tuple[LedgerPosting, ...] = ()

    def __post_init__(self) -> None:
        require_aware(self.occurred_at, "occurred_at")
        if self.revision_no < 0:
            raise PortfolioInvariantError("corporate-action revision cannot be negative")
        currencies = {
            self.cash_before.currency,
            self.cash_after.currency,
            self.position_cost_before.currency,
            self.position_cost_after.currency,
            self.receivable_before.currency,
            self.receivable_after.currency,
            *(posting.amount.currency for posting in self.postings),
        }
        if len(currencies) != 1:
            raise PortfolioInvariantError("corporate-action ledger currencies must match")
        if self.cash_before.amount < 0 or self.cash_after.amount < 0:
            raise PortfolioInvariantError("corporate-action ledger cash cannot be negative")
        if self.position_cost_before != self.position_cost_after:
            raise PortfolioInvariantError(
                "supported corporate actions must preserve aggregate position cost"
            )
        if self.receivable_before.amount < 0 or self.receivable_after.amount < 0:
            raise PortfolioInvariantError("corporate-action receivable cannot be negative")
        if (
            type(self.share_entitlement_delta_before) is not int
            or type(self.share_entitlement_delta_after) is not int
        ):
            raise PortfolioInvariantError("corporate-action share entitlement deltas must be ints")
        if self.postings:
            debit = Money.zero(self.cash_before.currency)
            credit = Money.zero(self.cash_before.currency)
            for posting in self.postings:
                if posting.side is PostingSide.DEBIT:
                    debit += posting.amount
                else:
                    credit += posting.amount
            if debit != credit:
                raise PortfolioInvariantError("corporate-action postings must balance")

    @property
    def cash_delta(self) -> Money:
        return self.cash_after - self.cash_before


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Complete immutable portfolio snapshot and its applied-fill audit trail."""

    cash: Money
    lots: tuple[PositionLot, ...] = ()
    fills: tuple[FillRecord, ...] = ()
    ledger_entries: tuple[LedgerEntry, ...] = ()
    corporate_action_entries: tuple[CorporateActionLedgerEntry, ...] = ()
    corporate_action_entitlements: tuple[CorporateActionEntitlement, ...] = ()

    def __post_init__(self) -> None:
        if self.cash.amount < 0:
            raise PortfolioInvariantError("portfolio cash cannot be negative")

        opened_ids = tuple(lot.opened_by_fill_id for lot in self.lots)
        if len(set(opened_ids)) != len(opened_ids):
            raise PortfolioInvariantError("open position lot ids must be unique")

        fill_ids = tuple(fill.fill_id for fill in self.fills)
        if len(set(fill_ids)) != len(fill_ids):
            raise PortfolioInvariantError("applied fill ids must be unique")
        entry_ids = tuple(entry.fill_id for entry in self.ledger_entries)
        if entry_ids != fill_ids:
            raise PortfolioInvariantError(
                "each applied fill must have one ledger entry in the same order"
            )

        currency = self.cash.currency
        if any(lot.cost_basis.currency != currency for lot in self.lots):
            raise PortfolioInvariantError("cash and position lots must use one currency")
        if any(fill.price.currency != currency for fill in self.fills):
            raise PortfolioInvariantError("cash and fills must use one currency")
        if any(entry.currency != currency for entry in self.ledger_entries):
            raise PortfolioInvariantError("cash and ledger must use one currency")
        action_keys = tuple(
            (entry.action_id, entry.revision_no, entry.phase)
            for entry in self.corporate_action_entries
        )
        if len(action_keys) != len(set(action_keys)):
            raise PortfolioInvariantError("corporate-action ledger keys must be unique")
        if any(entry.cash_before.currency != currency for entry in self.corporate_action_entries):
            raise PortfolioInvariantError("cash and corporate-action ledger must use one currency")
        entitlement_keys = tuple(
            (item.action_id, item.revision_no) for item in self.corporate_action_entitlements
        )
        if len(entitlement_keys) != len(set(entitlement_keys)):
            raise PortfolioInvariantError("corporate-action entitlements must be unique")
        if any(
            item.cash_amount.currency != currency for item in self.corporate_action_entitlements
        ):
            raise PortfolioInvariantError("cash and corporate-action entitlements must match")

    @property
    def applied_fill_ids(self) -> frozenset[FillId]:
        ids = {fill.fill_id for fill in self.fills}
        ids.update(lot.opened_by_fill_id for lot in self.lots)
        ids.update(entry.fill_id for entry in self.ledger_entries)
        return frozenset(ids)

    @property
    def applied_corporate_action_phases(
        self,
    ) -> frozenset[tuple[StrongId, int, CorporateActionPhase]]:
        return frozenset(
            (entry.action_id, entry.revision_no, entry.phase)
            for entry in self.corporate_action_entries
        )

    def dividend_receivable(self, instrument_id: InstrumentId | None = None) -> Money:
        total = Money.zero(self.cash.currency)
        for item in self.corporate_action_entitlements:
            if instrument_id is not None and item.instrument_id != instrument_id:
                continue
            if item.status is CorporateActionEntitlementStatus.ACCRUED:
                total += item.cash_amount
        return total

    def share_receivable_quantity(self, instrument_id: InstrumentId) -> Quantity:
        return Quantity(max(0, self.pending_share_delta(instrument_id)))

    def pending_share_delta(self, instrument_id: InstrumentId) -> int:
        """Signed economic share change recognized but not yet settled."""

        total = 0
        for item in self.corporate_action_entitlements:
            if (
                item.instrument_id == instrument_id
                and item.status is CorporateActionEntitlementStatus.ACCRUED
            ):
                total += item.share_delta
        return total

    def position_quantity(self, instrument_id: InstrumentId) -> Quantity:
        return Quantity(
            sum(
                lot.remaining_quantity.value
                for lot in self.lots
                if lot.instrument_id == instrument_id
            )
        )

    def sellable_quantity(self, instrument_id: InstrumentId, trading_date: date) -> Quantity:
        if type(trading_date) is not date:
            raise PortfolioInvariantError("trading_date must be a date")
        return Quantity(
            sum(
                lot.remaining_quantity.value
                for lot in self.lots
                if lot.instrument_id == instrument_id and lot.sellable_on <= trading_date
            )
        )

    def position_cost(self, instrument_id: InstrumentId) -> Money:
        total = Money.zero(self.cash.currency)
        for lot in self.lots:
            if lot.instrument_id == instrument_id:
                total += lot.cost_basis
        return total
