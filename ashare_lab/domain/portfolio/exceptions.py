"""Portfolio-ledger failures with stable, domain-specific meanings."""

from ashare_lab.domain.shared import DomainValidationError


class PortfolioInvariantError(DomainValidationError):
    """Raised when an account, lot, fill, or journal invariant is violated."""


class DuplicateFillError(PortfolioInvariantError):
    """Raised when the same execution fill is applied more than once."""


class InsufficientCashError(PortfolioInvariantError):
    """Raised when applying a fill would make portfolio cash negative."""


class InsufficientSellableQuantityError(PortfolioInvariantError):
    """Raised when a sale exceeds the instrument's T+1-eligible quantity."""


class InvalidFillSideError(PortfolioInvariantError):
    """Raised when a buy fill is sent to sell logic, or conversely."""


class DuplicateCorporateActionError(PortfolioInvariantError):
    """Raised when one corporate-action identity is applied more than once."""


class UnsupportedCorporateActionError(PortfolioInvariantError):
    """Raised when applying an entitlement would require an unpinned policy."""
