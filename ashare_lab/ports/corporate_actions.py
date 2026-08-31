"""Point-in-time corporate-action boundary shared by funded replays."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ashare_lab.domain.market_data import InstrumentSession
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, require_aware


@dataclass(frozen=True, slots=True)
class AppliedCorporateAction:
    """One auditable adjustment applied at a deterministic session boundary."""

    action_id: str
    reason_code: str
    phase: str = "application"

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise DomainValidationError("corporate action_id cannot be blank")
        if not self.reason_code.strip():
            raise DomainValidationError("corporate action reason_code cannot be blank")
        if not self.phase.strip():
            raise DomainValidationError("corporate action phase cannot be blank")


@dataclass(frozen=True, slots=True)
class CorporateActionApplication:
    """Adjusted state plus the immutable facts responsible for the change."""

    portfolio: PortfolioState
    applied_actions: tuple[AppliedCorporateAction, ...] = ()

    def __post_init__(self) -> None:
        action_keys = tuple((item.action_id, item.phase) for item in self.applied_actions)
        if len(action_keys) != len(set(action_keys)):
            raise DomainValidationError(
                "corporate action id and phase must be unique per application"
            )


class CorporateActionApplier(Protocol):
    """Apply actions knowable by ``as_of`` immediately before a session opens."""

    @property
    def policy_id(self) -> str: ...

    def apply_before_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication: ...

    def apply_after_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication: ...


@dataclass(frozen=True, slots=True)
class ExplicitNoCorporateActions:
    """An explicit research assumption, never an implicit production default."""

    policy_id: str = "corporate_actions.explicit_none.v1"

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise DomainValidationError("corporate action policy_id cannot be blank")

    def apply_before_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication:
        require_aware(as_of, "as_of")
        if instrument_id != session.instrument_id:
            raise DomainValidationError("corporate action instrument and session must match")
        return CorporateActionApplication(portfolio=portfolio)

    def apply_after_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication:
        require_aware(as_of, "as_of")
        if instrument_id != session.instrument_id:
            raise DomainValidationError("corporate action instrument and session must match")
        return CorporateActionApplication(portfolio=portfolio)
