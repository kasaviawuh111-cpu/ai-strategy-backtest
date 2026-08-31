"""Staged, idempotent portfolio transitions for corporate actions."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import CorporateAction, CorporateActionKind
from ashare_lab.domain.shared import FillId, InstrumentId, Money, Quantity, require_aware

from .exceptions import PortfolioInvariantError, UnsupportedCorporateActionError
from .journal import LedgerAccount, LedgerComponent, LedgerPosting, PostingSide
from .models import (
    CorporateActionEntitlement,
    CorporateActionEntitlementStatus,
    CorporateActionLedgerEntry,
    CorporateActionPhase,
    PortfolioState,
    PositionLot,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHARE_ACTIONS = {
    CorporateActionKind.SHARE_DISTRIBUTION,
    CorporateActionKind.STOCK_SPLIT,
    CorporateActionKind.REVERSE_SPLIT,
}


def capture_corporate_action_entitlement(
    state: PortfolioState,
    action: CorporateAction,
    *,
    captured_at: datetime,
) -> PortfolioState:
    """Lock the record-date close holding before any later sale can change it."""

    _require_stage_clock(captured_at, action.record_date, "entitlement")
    key = (action.action_id, action.revision_no, CorporateActionPhase.ENTITLEMENT)
    if key in state.applied_corporate_action_phases:
        return state
    if action.available_at is None or action.available_at > captured_at:
        raise PortfolioInvariantError(
            "corporate-action terms were not validated by record-date close"
        )
    if action.currency != state.cash.currency:
        raise PortfolioInvariantError("corporate-action and portfolio currencies must match")
    if any(
        item.action_id == action.action_id and item.revision_no == action.revision_no
        for item in state.corporate_action_entitlements
    ):
        raise PortfolioInvariantError("corporate-action entitlement state is inconsistent")

    entitled_quantity = state.position_quantity(action.instrument_id)
    if action.action_type is CorporateActionKind.RIGHTS_ISSUE:
        raise UnsupportedCorporateActionError(
            "rights_issue_requires_explicit_participation_and_settlement_policy"
        )

    cash_amount = Money.zero(action.currency)
    share_delta = 0
    share_sellable_date = None
    if action.action_type is CorporateActionKind.CASH_DIVIDEND:
        assert action.gross_cash_per_share is not None and action.cash_pay_date is not None
        cash_amount = Money(
            action.gross_cash_per_share * Decimal(entitled_quantity.value),
            action.currency,
        )
        settlement_date = action.cash_pay_date
    elif action.action_type in _SHARE_ACTIONS:
        assert action.share_multiplier is not None
        assert action.share_credit_date is not None and action.share_sellable_date is not None
        target = Decimal(entitled_quantity.value) * action.share_multiplier
        integral = target.to_integral_value()
        if target != integral:
            raise UnsupportedCorporateActionError(
                "share_action_requires_integer_registration_allocation_evidence"
            )
        share_delta = int(integral) - entitled_quantity.value
        settlement_date = action.share_credit_date
        share_sellable_date = action.share_sellable_date
    else:
        assert action.rights_listing_date is not None
        settlement_date = action.rights_listing_date

    entitlement = CorporateActionEntitlement(
        action_id=action.action_id,
        revision_no=action.revision_no,
        instrument_id=action.instrument_id,
        action_type=action.action_type,
        record_date=action.record_date,
        ex_date=action.ex_date,
        settlement_date=settlement_date,
        entitled_quantity=entitled_quantity,
        cash_amount=cash_amount,
        share_delta=share_delta,
        share_sellable_date=share_sellable_date,
        status=CorporateActionEntitlementStatus.CAPTURED,
        captured_at=captured_at,
    )
    entry = _entry(
        state,
        action,
        phase=CorporateActionPhase.ENTITLEMENT,
        occurred_at=captured_at,
        cash_after=state.cash,
        lots_after=state.lots,
        entitlements_after=(*state.corporate_action_entitlements, entitlement),
    )
    return _state(
        state,
        entitlements=(*state.corporate_action_entitlements, entitlement),
        entry=entry,
    )


def decline_rights_issue(
    state: PortfolioState,
    action: CorporateAction,
    *,
    declined_at: datetime,
) -> PortfolioState:
    """Record a no-subscription decision without adding cash or shares.

    The internal Demo never injects external cash.  A rights issue is therefore
    handled as an explicit record-date decision to let the rights lapse.  The
    unchanged before/after ledger entry makes that choice auditable and keeps
    retries idempotent; it must never be mistaken for missing source data.
    """

    _require_stage_clock(declined_at, action.record_date, "rights decline")
    if action.action_type is not CorporateActionKind.RIGHTS_ISSUE:
        raise UnsupportedCorporateActionError("only a rights issue can be declined")
    key = (action.action_id, action.revision_no, CorporateActionPhase.DECLINED)
    if key in state.applied_corporate_action_phases:
        return state
    if action.available_at is None or action.available_at > declined_at:
        raise PortfolioInvariantError(
            "corporate-action terms were not validated by record-date close"
        )
    if action.currency != state.cash.currency:
        raise PortfolioInvariantError("corporate-action and portfolio currencies must match")
    if any(
        item.action_id == action.action_id and item.revision_no == action.revision_no
        for item in state.corporate_action_entitlements
    ):
        raise PortfolioInvariantError("declined rights issue cannot have an entitlement")

    entry = _entry(
        state,
        action,
        phase=CorporateActionPhase.DECLINED,
        occurred_at=declined_at,
        cash_after=state.cash,
        lots_after=state.lots,
        entitlements_after=state.corporate_action_entitlements,
    )
    return _state(
        state,
        entitlements=state.corporate_action_entitlements,
        entry=entry,
    )


def accrue_corporate_action(
    state: PortfolioState,
    action: CorporateAction,
    *,
    accrued_at: datetime,
) -> PortfolioState:
    """Recognize the entitlement at ex-date without making cash spendable."""

    _require_stage_clock(accrued_at, action.ex_date, "accrual")
    key = (action.action_id, action.revision_no, CorporateActionPhase.ACCRUAL)
    if key in state.applied_corporate_action_phases:
        return state
    index, entitlement = _entitlement(state, action)
    if entitlement.status is not CorporateActionEntitlementStatus.CAPTURED:
        raise PortfolioInvariantError("corporate action must be captured before accrual")
    updated = replace(
        entitlement,
        status=CorporateActionEntitlementStatus.ACCRUED,
        accrued_at=accrued_at,
    )
    entitlements = _replace_entitlement(state, index, updated)
    postings: tuple[LedgerPosting, ...] = ()
    if entitlement.cash_amount.amount > 0:
        postings = (
            LedgerPosting(
                account=LedgerAccount.DIVIDEND_RECEIVABLE,
                side=PostingSide.DEBIT,
                amount=entitlement.cash_amount,
                component=LedgerComponent.CASH_DIVIDEND,
                instrument_id=action.instrument_id,
            ),
            LedgerPosting(
                account=LedgerAccount.DIVIDEND_INCOME,
                side=PostingSide.CREDIT,
                amount=entitlement.cash_amount,
                component=LedgerComponent.CASH_DIVIDEND,
                instrument_id=action.instrument_id,
            ),
        )
    entry = _entry(
        state,
        action,
        phase=CorporateActionPhase.ACCRUAL,
        occurred_at=accrued_at,
        cash_after=state.cash,
        lots_after=state.lots,
        entitlements_after=entitlements,
        postings=postings,
    )
    return _state(state, entitlements=entitlements, entry=entry)


def settle_corporate_action(
    state: PortfolioState,
    action: CorporateAction,
    *,
    settled_at: datetime,
) -> PortfolioState:
    """Move an accrued receivable into available cash or credited shares."""

    require_aware(settled_at, "settled_at")
    key = (action.action_id, action.revision_no, CorporateActionPhase.SETTLEMENT)
    if key in state.applied_corporate_action_phases:
        return state
    index, entitlement = _entitlement(state, action)
    if entitlement.status is not CorporateActionEntitlementStatus.ACCRUED:
        raise PortfolioInvariantError("corporate action must accrue before settlement")
    if settled_at.astimezone(_SHANGHAI).date() < entitlement.settlement_date:
        raise PortfolioInvariantError("corporate action cannot settle before its source date")

    cash_after = state.cash
    lots_after = state.lots
    postings: tuple[LedgerPosting, ...] = ()
    if action.action_type is CorporateActionKind.CASH_DIVIDEND:
        cash_after += entitlement.cash_amount
        if entitlement.cash_amount.amount > 0:
            postings = (
                LedgerPosting(
                    account=LedgerAccount.CASH,
                    side=PostingSide.DEBIT,
                    amount=entitlement.cash_amount,
                    component=LedgerComponent.CASH_DIVIDEND,
                ),
                LedgerPosting(
                    account=LedgerAccount.DIVIDEND_RECEIVABLE,
                    side=PostingSide.CREDIT,
                    amount=entitlement.cash_amount,
                    component=LedgerComponent.CASH_DIVIDEND,
                    instrument_id=action.instrument_id,
                ),
            )
    elif action.action_type is CorporateActionKind.SHARE_DISTRIBUTION:
        if entitlement.share_delta > 0:
            assert entitlement.share_sellable_date is not None
            lots_after = (
                *state.lots,
                PositionLot(
                    opened_by_fill_id=_corporate_lot_id(action),
                    instrument_id=action.instrument_id,
                    acquired_at=settled_at,
                    acquired_on=settled_at.astimezone(_SHANGHAI).date(),
                    sellable_on=entitlement.share_sellable_date,
                    remaining_quantity=Quantity(entitlement.share_delta),
                    cost_basis=Money.zero(state.cash.currency),
                ),
            )
    elif action.action_type in {
        CorporateActionKind.STOCK_SPLIT,
        CorporateActionKind.REVERSE_SPLIT,
    }:
        current = state.position_quantity(action.instrument_id)
        if current != entitlement.entitled_quantity:
            raise UnsupportedCorporateActionError(
                "split_settlement_requires_unchanged_record_date_inventory"
            )
        target = current.value + entitlement.share_delta
        if target < 0 or (current.value > 0 and target == 0):
            raise UnsupportedCorporateActionError(
                "split_settlement_requires_positive_integer_registered_quantity"
            )
        other_lots = tuple(lot for lot in state.lots if lot.instrument_id != action.instrument_id)
        if target > 0:
            assert entitlement.share_sellable_date is not None
            transformed = PositionLot(
                opened_by_fill_id=_corporate_lot_id(action),
                instrument_id=action.instrument_id,
                acquired_at=settled_at,
                acquired_on=settled_at.astimezone(_SHANGHAI).date(),
                sellable_on=entitlement.share_sellable_date,
                remaining_quantity=Quantity(target),
                cost_basis=state.position_cost(action.instrument_id),
            )
            lots_after = (*other_lots, transformed)

    updated = replace(
        entitlement,
        status=CorporateActionEntitlementStatus.SETTLED,
        settled_at=settled_at,
    )
    entitlements = _replace_entitlement(state, index, updated)
    entry = _entry(
        state,
        action,
        phase=CorporateActionPhase.SETTLEMENT,
        occurred_at=settled_at,
        cash_after=cash_after,
        lots_after=lots_after,
        entitlements_after=entitlements,
        postings=postings,
    )
    return _state(
        state,
        cash=cash_after,
        lots=lots_after,
        entitlements=entitlements,
        entry=entry,
    )


def _state(
    state: PortfolioState,
    *,
    cash: Money | None = None,
    lots: tuple[PositionLot, ...] | None = None,
    entitlements: tuple[CorporateActionEntitlement, ...],
    entry: CorporateActionLedgerEntry,
) -> PortfolioState:
    return PortfolioState(
        cash=state.cash if cash is None else cash,
        lots=state.lots if lots is None else lots,
        fills=state.fills,
        ledger_entries=state.ledger_entries,
        corporate_action_entries=(*state.corporate_action_entries, entry),
        corporate_action_entitlements=entitlements,
    )


def _entry(
    state: PortfolioState,
    action: CorporateAction,
    *,
    phase: CorporateActionPhase,
    occurred_at: datetime,
    cash_after: Money,
    lots_after: tuple[PositionLot, ...],
    entitlements_after: tuple[CorporateActionEntitlement, ...],
    postings: tuple[LedgerPosting, ...] = (),
) -> CorporateActionLedgerEntry:
    return CorporateActionLedgerEntry(
        action_id=action.action_id,
        revision_no=action.revision_no,
        instrument_id=action.instrument_id,
        action_type=action.action_type,
        phase=phase,
        occurred_at=occurred_at,
        cash_before=state.cash,
        cash_after=cash_after,
        quantity_before=state.position_quantity(action.instrument_id),
        quantity_after=_position_quantity(lots_after, action.instrument_id),
        position_cost_before=state.position_cost(action.instrument_id),
        position_cost_after=_position_cost(lots_after, action.instrument_id, state.cash.currency),
        receivable_before=state.dividend_receivable(action.instrument_id),
        receivable_after=_dividend_receivable(
            entitlements_after,
            action.instrument_id,
            state.cash.currency,
        ),
        share_entitlement_delta_before=state.pending_share_delta(action.instrument_id),
        share_entitlement_delta_after=_pending_share_delta(
            entitlements_after,
            action.instrument_id,
        ),
        postings=postings,
    )


def _entitlement(
    state: PortfolioState,
    action: CorporateAction,
) -> tuple[int, CorporateActionEntitlement]:
    matches = tuple(
        (index, item)
        for index, item in enumerate(state.corporate_action_entitlements)
        if item.action_id == action.action_id and item.revision_no == action.revision_no
    )
    if len(matches) != 1:
        raise PortfolioInvariantError("corporate-action entitlement is missing or ambiguous")
    return matches[0]


def _replace_entitlement(
    state: PortfolioState,
    index: int,
    replacement: CorporateActionEntitlement,
) -> tuple[CorporateActionEntitlement, ...]:
    values = list(state.corporate_action_entitlements)
    values[index] = replacement
    return tuple(values)


def _require_stage_clock(value: datetime, expected_date: date, phase: str) -> None:
    require_aware(value, f"{phase}_at")
    if value.astimezone(_SHANGHAI).date() != expected_date:
        raise PortfolioInvariantError(f"corporate-action {phase} used the wrong local date")


def _corporate_lot_id(action: CorporateAction) -> FillId:
    digest = hashlib.sha256(f"{action.action_id.value}:{action.revision_no}".encode()).hexdigest()[
        :24
    ]
    return FillId(f"corporate:{digest}")


def _position_quantity(
    lots: tuple[PositionLot, ...],
    instrument_id: InstrumentId,
) -> Quantity:
    return Quantity(
        sum(lot.remaining_quantity.value for lot in lots if lot.instrument_id == instrument_id)
    )


def _position_cost(
    lots: tuple[PositionLot, ...],
    instrument_id: InstrumentId,
    currency: str,
) -> Money:
    total = Money.zero(currency)
    for lot in lots:
        if lot.instrument_id == instrument_id:
            total += lot.cost_basis
    return total


def _dividend_receivable(
    entitlements: tuple[CorporateActionEntitlement, ...],
    instrument_id: InstrumentId,
    currency: str,
) -> Money:
    total = Money.zero(currency)
    for item in entitlements:
        if (
            item.instrument_id == instrument_id
            and item.status is CorporateActionEntitlementStatus.ACCRUED
        ):
            total += item.cash_amount
    return total


def _pending_share_delta(
    entitlements: tuple[CorporateActionEntitlement, ...],
    instrument_id: InstrumentId,
) -> int:
    return sum(
        item.share_delta
        for item in entitlements
        if item.instrument_id == instrument_id
        and item.status is CorporateActionEntitlementStatus.ACCRUED
    )
