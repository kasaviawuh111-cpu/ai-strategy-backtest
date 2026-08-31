from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.market_data import (
    CorporateAction,
    CorporateActionKind,
    TimeQuality,
)
from ashare_lab.domain.portfolio import (
    CorporateActionEntitlementStatus,
    CorporateActionPhase,
    PortfolioState,
    PositionLot,
    UnsupportedCorporateActionError,
    accrue_corporate_action,
    capture_corporate_action_entitlement,
    decline_rights_issue,
    settle_corporate_action,
)
from ashare_lab.domain.shared import FillId, InstrumentId, Money, Quantity, StrongId

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
RECORD_DATE = date(2025, 1, 2)
EX_DATE = date(2025, 1, 3)


def _clock(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)


def _portfolio() -> PortfolioState:
    return PortfolioState(
        cash=Money(Decimal("500")),
        lots=(
            PositionLot(
                opened_by_fill_id=FillId("fill:seed"),
                instrument_id=INSTRUMENT,
                acquired_at=_clock(date(2024, 12, 31), 10),
                acquired_on=date(2024, 12, 31),
                sellable_on=RECORD_DATE,
                remaining_quantity=Quantity(100),
                cost_basis=Money(Decimal("1000")),
            ),
        ),
    )


def _action(
    action_type: CorporateActionKind,
    *,
    cash_per_share: Decimal | None = None,
    pay_date: date | None = None,
    multiplier: Decimal | None = None,
    credit_date: date | None = None,
    sellable_date: date | None = None,
) -> CorporateAction:
    return CorporateAction(
        action_id=StrongId(f"action:{action_type.value}"),
        source_action_id=f"source:{action_type.value}",
        instrument_id=INSTRUMENT,
        action_type=action_type,
        record_date=RECORD_DATE,
        ex_date=EX_DATE,
        source_released_at=_clock(RECORD_DATE, 9),
        vendor_first_available_at=_clock(RECORD_DATE, 9, 1),
        ingested_at=_clock(RECORD_DATE, 9, 3),
        replay_available_at=_clock(RECORD_DATE, 9, 2),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/action",
        raw_response_sha256="a" * 64,
        validation_status="validated",
        gross_cash_per_share=cash_per_share,
        cash_pay_date=pay_date,
        share_multiplier=multiplier,
        share_credit_date=credit_date,
        share_sellable_date=sellable_date,
    )


def test_cash_dividend_locks_then_accrues_then_settles_without_early_spendability() -> None:
    action = _action(
        CorporateActionKind.CASH_DIVIDEND,
        cash_per_share=Decimal("0.5"),
        pay_date=date(2025, 1, 6),
    )
    captured = capture_corporate_action_entitlement(
        _portfolio(),
        action,
        captured_at=_clock(RECORD_DATE, 15),
    )
    accrued = accrue_corporate_action(
        captured,
        action,
        accrued_at=_clock(EX_DATE, 9, 29),
    )

    assert captured.corporate_action_entitlements[0].entitled_quantity == Quantity(100)
    assert accrued.cash == Money(Decimal("500"))
    assert accrued.dividend_receivable(INSTRUMENT) == Money(Decimal("50"))
    assert accrued.corporate_action_entitlements[0].status is (
        CorporateActionEntitlementStatus.ACCRUED
    )

    settled = settle_corporate_action(
        accrued,
        action,
        settled_at=_clock(date(2025, 1, 6), 9, 29),
    )
    assert settled.cash == Money(Decimal("550"))
    assert settled.dividend_receivable(INSTRUMENT) == Money.zero()
    assert [item.phase for item in settled.corporate_action_entries] == [
        CorporateActionPhase.ENTITLEMENT,
        CorporateActionPhase.ACCRUAL,
        CorporateActionPhase.SETTLEMENT,
    ]
    assert (
        settle_corporate_action(
            settled,
            action,
            settled_at=_clock(date(2025, 1, 6), 9, 29),
        )
        is settled
    )


def test_share_distribution_credits_zero_cost_shares_on_explicit_sellable_date() -> None:
    action = _action(
        CorporateActionKind.SHARE_DISTRIBUTION,
        multiplier=Decimal("1.5"),
        credit_date=date(2025, 1, 6),
        sellable_date=date(2025, 1, 7),
    )
    captured = capture_corporate_action_entitlement(
        _portfolio(),
        action,
        captured_at=_clock(RECORD_DATE, 15),
    )
    accrued = accrue_corporate_action(
        captured,
        action,
        accrued_at=_clock(EX_DATE, 9, 29),
    )
    assert accrued.pending_share_delta(INSTRUMENT) == 50
    assert accrued.position_quantity(INSTRUMENT) == Quantity(100)

    settled = settle_corporate_action(
        accrued,
        action,
        settled_at=_clock(date(2025, 1, 6), 9, 29),
    )
    assert settled.pending_share_delta(INSTRUMENT) == 0
    assert settled.position_quantity(INSTRUMENT) == Quantity(150)
    assert settled.position_cost(INSTRUMENT) == Money(Decimal("1000"))
    assert settled.sellable_quantity(INSTRUMENT, date(2025, 1, 6)) == Quantity(100)
    assert settled.sellable_quantity(INSTRUMENT, date(2025, 1, 7)) == Quantity(150)


def test_share_distribution_rejects_unreconstructable_sub_share_registration_tail() -> None:
    action = _action(
        CorporateActionKind.SHARE_DISTRIBUTION,
        multiplier=Decimal("1.005"),
        credit_date=date(2025, 1, 6),
        sellable_date=date(2025, 1, 7),
    )

    with pytest.raises(
        UnsupportedCorporateActionError,
        match="integer_registration_allocation_evidence",
    ):
        capture_corporate_action_entitlement(
            _portfolio(),
            action,
            captured_at=_clock(RECORD_DATE, 15),
        )


def test_reverse_split_carries_a_signed_pending_delta_and_preserves_cost() -> None:
    action = _action(
        CorporateActionKind.REVERSE_SPLIT,
        multiplier=Decimal("0.5"),
        credit_date=EX_DATE,
        sellable_date=EX_DATE,
    )
    captured = capture_corporate_action_entitlement(
        _portfolio(),
        action,
        captured_at=_clock(RECORD_DATE, 15),
    )
    accrued = accrue_corporate_action(
        captured,
        action,
        accrued_at=_clock(EX_DATE, 9, 29),
    )
    assert accrued.pending_share_delta(INSTRUMENT) == -50

    settled = settle_corporate_action(
        accrued,
        action,
        settled_at=_clock(EX_DATE, 9, 29),
    )
    assert settled.position_quantity(INSTRUMENT) == Quantity(50)
    assert settled.position_cost(INSTRUMENT) == Money(Decimal("1000"))


def test_rights_issue_fails_closed_without_a_participation_policy() -> None:
    action = CorporateAction(
        action_id=StrongId("action:rights"),
        source_action_id="source:rights",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.RIGHTS_ISSUE,
        record_date=RECORD_DATE,
        ex_date=EX_DATE,
        source_released_at=_clock(RECORD_DATE, 9),
        vendor_first_available_at=_clock(RECORD_DATE, 9, 1),
        ingested_at=_clock(RECORD_DATE, 9, 3),
        replay_available_at=_clock(RECORD_DATE, 9, 2),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/rights",
        raw_response_sha256="b" * 64,
        validation_status="validated",
        rights_ratio=Decimal("0.2"),
        rights_subscription_price=Decimal("6"),
        rights_payment_deadline=date(2025, 1, 8),
        rights_listing_date=date(2025, 1, 15),
    )

    with pytest.raises(UnsupportedCorporateActionError, match="explicit_participation"):
        capture_corporate_action_entitlement(
            _portfolio(),
            action,
            captured_at=_clock(RECORD_DATE, 15),
        )


def test_rights_issue_can_be_explicitly_declined_without_external_cash() -> None:
    action = CorporateAction(
        action_id=StrongId("action:rights:declined"),
        source_action_id="source:rights:declined",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.RIGHTS_ISSUE,
        record_date=RECORD_DATE,
        ex_date=EX_DATE,
        source_released_at=_clock(RECORD_DATE, 9),
        vendor_first_available_at=_clock(RECORD_DATE, 9, 1),
        ingested_at=_clock(RECORD_DATE, 9, 3),
        replay_available_at=_clock(RECORD_DATE, 9, 2),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/rights-declined",
        raw_response_sha256="d" * 64,
        validation_status="validated",
        rights_ratio=Decimal("0.2"),
        rights_subscription_price=Decimal("6"),
        rights_payment_deadline=date(2025, 1, 8),
        rights_listing_date=date(2025, 1, 15),
    )
    portfolio = _portfolio()

    declined = decline_rights_issue(
        portfolio,
        action,
        declined_at=_clock(RECORD_DATE, 15),
    )
    replay = decline_rights_issue(
        declined,
        action,
        declined_at=_clock(RECORD_DATE, 15),
    )

    assert declined.cash == portfolio.cash
    assert declined.lots == portfolio.lots
    assert declined.corporate_action_entitlements == ()
    assert [item.phase for item in declined.corporate_action_entries] == [
        CorporateActionPhase.DECLINED
    ]
    assert replay == declined
