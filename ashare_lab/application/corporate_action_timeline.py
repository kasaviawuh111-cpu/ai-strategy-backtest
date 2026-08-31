"""Point-in-time corporate-action orchestration shared by funded accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import (
    CorporateAction,
    CorporateActionKind,
    InstrumentSession,
)
from ashare_lab.domain.portfolio import (
    CorporateActionEntitlement,
    CorporateActionEntitlementStatus,
    CorporateActionPhase,
    PortfolioState,
    accrue_corporate_action,
    capture_corporate_action_entitlement,
    decline_rights_issue,
    settle_corporate_action,
)
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, require_aware
from ashare_lab.ports.corporate_actions import (
    AppliedCorporateAction,
    CorporateActionApplication,
)

# Source feeds currently provide cash settlement as a calendar date, not an
# intraday credit timestamp.  v2 therefore keeps the receivable through that
# session and moves it to available cash only after the close.  Credited shares
# continue to use their explicit source credit/sellable dates.
CORPORATE_ACTION_POLICY = (
    "cn.a_share.timeline_entitlement_receivable_settlement."
    "date_only_cash_after_close_conservative.v2"
)
RIGHTS_ISSUE_POLICY = "decline_no_external_cash.v1"
DIVIDEND_TAX_POLICY = "gross_research_no_withholding.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class TimelineCorporateActionApplier:
    """Apply a pinned action tuple through the authoritative staged ledger."""

    actions: tuple[CorporateAction, ...]
    policy_id: str = CORPORATE_ACTION_POLICY

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise DomainValidationError("corporate action policy_id cannot be blank")
        keys = tuple((item.action_id, item.revision_no) for item in self.actions)
        if len(keys) != len(set(keys)):
            raise DomainValidationError("corporate actions must have unique revision keys")
        source_leg_keys = tuple((item.source_action_id, item.action_type) for item in self.actions)
        if len(source_leg_keys) != len(set(source_leg_keys)):
            raise DomainValidationError(
                "corporate actions must contain one selected revision per source action leg"
            )

    def apply_before_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication:
        _validate_boundary(instrument_id, session, as_of)
        state = portfolio
        applied: list[AppliedCorporateAction] = []
        actions = self._for_instrument(instrument_id)

        for action in actions:
            if action.ex_date != session.session_date or not _has_entitlement(state, action):
                continue
            key = (action.action_id, action.revision_no, CorporateActionPhase.ACCRUAL)
            if key in state.applied_corporate_action_phases:
                continue
            state = accrue_corporate_action(state, action, accrued_at=as_of)
            applied.append(
                AppliedCorporateAction(
                    action_id=action.action_id.value,
                    phase=CorporateActionPhase.ACCRUAL.value,
                    reason_code="ex_date_receivable_accrued",
                )
            )

        for action in actions:
            entitlement = _find_entitlement(state, action)
            if (
                action.action_type is CorporateActionKind.CASH_DIVIDEND
                or entitlement is None
                or entitlement.status is not CorporateActionEntitlementStatus.ACCRUED
                or entitlement.settlement_date > session.session_date
            ):
                continue
            key = (action.action_id, action.revision_no, CorporateActionPhase.SETTLEMENT)
            if key in state.applied_corporate_action_phases:
                continue
            state = settle_corporate_action(state, action, settled_at=as_of)
            applied.append(
                AppliedCorporateAction(
                    action_id=action.action_id.value,
                    phase=CorporateActionPhase.SETTLEMENT.value,
                    reason_code="shares_credited_before_session_per_source_dates",
                )
            )
        return CorporateActionApplication(state, tuple(applied))

    def apply_after_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication:
        _validate_boundary(instrument_id, session, as_of)
        state = portfolio
        applied: list[AppliedCorporateAction] = []
        for action in self._for_instrument(instrument_id):
            if action.record_date != session.session_date:
                continue
            if action.action_type is CorporateActionKind.RIGHTS_ISSUE:
                key = (action.action_id, action.revision_no, CorporateActionPhase.DECLINED)
                if key in state.applied_corporate_action_phases:
                    continue
                state = decline_rights_issue(state, action, declined_at=as_of)
                applied.append(
                    AppliedCorporateAction(
                        action_id=action.action_id.value,
                        phase=CorporateActionPhase.DECLINED.value,
                        reason_code="rights_issue_declined_no_external_cash",
                    )
                )
                continue
            key = (action.action_id, action.revision_no, CorporateActionPhase.ENTITLEMENT)
            if key in state.applied_corporate_action_phases:
                continue
            state = capture_corporate_action_entitlement(state, action, captured_at=as_of)
            applied.append(
                AppliedCorporateAction(
                    action_id=action.action_id.value,
                    phase=CorporateActionPhase.ENTITLEMENT.value,
                    reason_code="record_date_entitlement_captured",
                )
            )

        for action in self._for_instrument(instrument_id):
            entitlement = _find_entitlement(state, action)
            if (
                action.action_type is not CorporateActionKind.CASH_DIVIDEND
                or entitlement is None
                or entitlement.status is not CorporateActionEntitlementStatus.ACCRUED
                or entitlement.settlement_date > session.session_date
            ):
                continue
            key = (action.action_id, action.revision_no, CorporateActionPhase.SETTLEMENT)
            if key in state.applied_corporate_action_phases:
                continue
            state = settle_corporate_action(state, action, settled_at=as_of)
            applied.append(
                AppliedCorporateAction(
                    action_id=action.action_id.value,
                    phase=CorporateActionPhase.SETTLEMENT.value,
                    reason_code="date_only_cash_settled_after_close_conservative",
                )
            )
        return CorporateActionApplication(state, tuple(applied))

    def _for_instrument(self, instrument_id: InstrumentId) -> tuple[CorporateAction, ...]:
        return tuple(
            sorted(
                (item for item in self.actions if item.instrument_id == instrument_id),
                key=lambda item: (
                    item.record_date,
                    item.ex_date,
                    item.action_id.value,
                    item.revision_no,
                ),
            )
        )


def _validate_boundary(
    instrument_id: InstrumentId,
    session: InstrumentSession,
    as_of: datetime,
) -> None:
    require_aware(as_of, "as_of")
    if instrument_id != session.instrument_id:
        raise DomainValidationError("corporate action instrument and session must match")
    if as_of.astimezone(SHANGHAI).date() != session.session_date:
        raise DomainValidationError("corporate action boundary date must match session")


def _find_entitlement(
    portfolio: PortfolioState,
    action: CorporateAction,
) -> CorporateActionEntitlement | None:
    return next(
        (
            item
            for item in portfolio.corporate_action_entitlements
            if item.action_id == action.action_id and item.revision_no == action.revision_no
        ),
        None,
    )


def _has_entitlement(portfolio: PortfolioState, action: CorporateAction) -> bool:
    return _find_entitlement(portfolio, action) is not None
