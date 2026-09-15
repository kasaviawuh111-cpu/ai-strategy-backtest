"""Canonical market records exposed to the backtest domain.

Provider-specific columns stop at an adapter.  Every fact crossing this boundary
states when it became knowable so a replay cannot accidentally see the future.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from ashare_lab.domain.shared import (
    DomainValidationError,
    InstrumentId,
    Money,
    Price,
    Quantity,
    StrongId,
    require_aware,
)

_PRODUCER_SNAPSHOT_ID = re.compile(r"^[a-z][a-z0-9_-]*:[0-9a-f]{64}$")


class BarInterval(StrEnum):
    DAY_1 = "1d"
    MINUTE_1 = "1m"


class PriceBasis(StrEnum):
    """Price transformation applied to a canonical bar series."""

    UNADJUSTED = "unadjusted"
    BACK_ADJUSTED = "back_adjusted"
    DYNAMIC_FRONT_ADJUSTED = "dynamic_front_adjusted"


class Board(StrEnum):
    MAIN = "main"
    STOCK_ETF = "stock_etf"
    CHINEXT = "chinext"
    STAR = "star"
    BSE = "bse"


_BOARD_BUY_QUANTITY_RULES: dict[Board, tuple[int, int]] = {
    Board.MAIN: (100, 100),
    Board.STOCK_ETF: (100, 100),
    Board.CHINEXT: (100, 100),
    Board.STAR: (200, 1),
    Board.BSE: (100, 1),
}


def standard_buy_quantity_rule(board: Board) -> tuple[int, int]:
    """Return the v1 A-share buy declaration minimum and increment.

    These values govern submitted buy quantities, not individual execution
    fragments. A valid order may be partially filled into an odd-lot position,
    and that actual integer-share remainder remains sellable in full.
    """

    if type(board) is not Board:
        raise DomainValidationError("board must be a Board")
    return _BOARD_BUY_QUANTITY_RULES[board]


class TradingStatus(StrEnum):
    TRADING = "trading"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


class TimeQuality(StrEnum):
    EXACT = "exact"
    VENDOR_OBSERVED = "vendor_observed"
    DATE_ONLY_CONSERVATIVE = "date_only_conservative"
    ESTIMATED_RESEARCH_ONLY = "estimated_research_only"


class CorporateActionKind(StrEnum):
    """Economic actions that can change a cash-equity holding."""

    CASH_DIVIDEND = "cash_dividend"
    SHARE_DISTRIBUTION = "share_distribution"
    STOCK_SPLIT = "stock_split"
    REVERSE_SPLIT = "reverse_split"
    RIGHTS_ISSUE = "rights_issue"


@dataclass(frozen=True, slots=True)
class DataSnapshotRef:
    """Immutable identity and checksum of a canonical dataset selection.

    ``schema_version`` describes the loader's pinned selection manifest.  A
    producer may wrap the physical files in a different, independently
    versioned snapshot format; when present, ``producer_schema_version`` keeps
    that provenance separate instead of overloading the loader schema.
    """

    snapshot_id: StrongId
    checksum: str
    schema_version: str
    created_at: datetime
    producer_schema_version: str | None = None
    producer_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")
        if not self.checksum.startswith("sha256:") or len(self.checksum) != 71:
            raise DomainValidationError("checksum must be sha256:<64 lowercase hex digits>")
        digest = self.checksum.removeprefix("sha256:")
        if any(character not in "0123456789abcdef" for character in digest):
            raise DomainValidationError("checksum must contain lowercase hexadecimal digits")
        if not self.schema_version:
            raise DomainValidationError("schema_version cannot be empty")
        if self.producer_schema_version is not None and not self.producer_schema_version:
            raise DomainValidationError("producer_schema_version cannot be empty")
        if self.producer_snapshot_id is not None and (
            _PRODUCER_SNAPSHOT_ID.fullmatch(self.producer_snapshot_id) is None
        ):
            raise DomainValidationError(
                "producer_snapshot_id must be <provider>:<64 lowercase hex digits>"
            )


@dataclass(frozen=True, slots=True)
class InstrumentSession:
    """Rules and tradability for one security on one exchange session."""

    instrument_id: InstrumentId
    session_date: date
    board: Board
    status: TradingStatus
    previous_close: Price
    upper_limit: Price | None
    lower_limit: Price | None
    minimum_buy_quantity: int
    buy_quantity_increment: int
    price_tick: Decimal = Decimal("0.01")
    t_plus_one: bool = True
    is_st: bool = False

    def __post_init__(self) -> None:
        limits = [value for value in (self.upper_limit, self.lower_limit) if value is not None]
        if (self.upper_limit is None) != (self.lower_limit is None):
            raise DomainValidationError(
                "upper_limit and lower_limit must both be set or both be absent"
            )
        currencies = {self.previous_close.currency, *(value.currency for value in limits)}
        if len(currencies) != 1:
            raise DomainValidationError("session prices must use one currency")
        if (
            self.lower_limit is not None
            and self.upper_limit is not None
            and self.lower_limit.amount >= self.upper_limit.amount
        ):
            raise DomainValidationError("lower_limit must be below upper_limit")
        if type(self.minimum_buy_quantity) is not int or self.minimum_buy_quantity <= 0:
            raise DomainValidationError("minimum_buy_quantity must be a positive integer")
        if type(self.buy_quantity_increment) is not int or self.buy_quantity_increment <= 0:
            raise DomainValidationError("buy_quantity_increment must be a positive integer")
        expected_minimum, expected_increment = standard_buy_quantity_rule(self.board)
        if (
            self.minimum_buy_quantity != expected_minimum
            or self.buy_quantity_increment != expected_increment
        ):
            raise DomainValidationError(
                "buy quantity rule does not match board: "
                f"{self.board.value} requires minimum={expected_minimum}, "
                f"increment={expected_increment}"
            )
        if self.price_tick <= 0:
            raise DomainValidationError("price_tick must be positive")
        if self.board is Board.STOCK_ETF and self.price_tick != Decimal("0.001"):
            raise DomainValidationError("stock_etf requires price_tick=0.001")
        if self.board is Board.STOCK_ETF and not self.t_plus_one:
            raise DomainValidationError("stock_etf requires T+1 settlement")
        if self.board is Board.STOCK_ETF and self.is_st:
            raise DomainValidationError("stock_etf cannot use stock ST rules")

    def is_valid_buy_quantity(self, quantity: int) -> bool:
        """Whether a submitted buy order satisfies this session's rule."""

        return (
            type(quantity) is int
            and quantity >= self.minimum_buy_quantity
            and (quantity - self.minimum_buy_quantity) % self.buy_quantity_increment == 0
        )

    def floor_buy_quantity(self, maximum: int) -> int:
        """Largest valid buy declaration no greater than ``maximum``."""

        if type(maximum) is not int or maximum < 0:
            raise DomainValidationError("maximum buy quantity must be a non-negative integer")
        if maximum < self.minimum_buy_quantity:
            return 0
        return (
            self.minimum_buy_quantity
            + ((maximum - self.minimum_buy_quantity) // self.buy_quantity_increment)
            * self.buy_quantity_increment
        )

    def previous_buy_quantity(self, quantity: int) -> int:
        """Previous valid declaration below ``quantity``, or zero."""

        if not self.is_valid_buy_quantity(quantity):
            raise DomainValidationError("quantity must be a valid buy declaration")
        return self.floor_buy_quantity(quantity - 1)


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One canonical daily OHLCV bar with an explicit price basis.

    Order matching and accounting must receive ``UNADJUSTED`` bars. Relative
    technical indicators may receive ``BACK_ADJUSTED`` bars so corporate-action
    discontinuities do not become false trading signals.
    """

    instrument_id: InstrumentId
    session_date: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    turnover: Decimal
    available_at: datetime
    price_basis: PriceBasis = PriceBasis.UNADJUSTED
    # This is deliberately separate from ``turnover`` (CNY amount).  A
    # provider-reported turnover rate must never be reconstructed from volume
    # and a contemporaneous share-capital value during replay.
    turnover_rate_pct: Decimal | None = None
    turnover_rate_provider: str | None = None
    turnover_rate_methodology: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")
        amounts = [self.open.amount, self.high.amount, self.low.amount, self.close.amount]
        currencies = {
            self.open.currency,
            self.high.currency,
            self.low.currency,
            self.close.currency,
        }
        if len(currencies) != 1:
            raise DomainValidationError("OHLC prices must use one currency")
        if self.low.amount > min(amounts) or self.high.amount < max(amounts):
            raise DomainValidationError("OHLC values are inconsistent with high/low")
        if self.turnover < 0 or not self.turnover.is_finite():
            raise DomainValidationError("turnover must be a finite non-negative Decimal")
        turnover_rate_provenance = (
            self.turnover_rate_provider,
            self.turnover_rate_methodology,
        )
        if self.turnover_rate_pct is None:
            if any(value is not None for value in turnover_rate_provenance):
                raise DomainValidationError("turnover-rate provenance requires turnover_rate_pct")
            return
        if self.turnover_rate_pct < 0 or not self.turnover_rate_pct.is_finite():
            raise DomainValidationError("turnover_rate_pct must be a finite non-negative Decimal")
        if any(
            not isinstance(value, str) or not value.strip() for value in turnover_rate_provenance
        ):
            raise DomainValidationError(
                "turnover-rate provenance requires non-empty provider and methodology"
            )


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """One completed one-minute OHLCV bar with an explicit information clock.

    ``available_at`` is deliberately separate from the provider's timestamp
    label.  A replay may evaluate this bar only after the complete interval is
    known, and matching must use a later bar rather than the signal bar itself.
    """

    instrument_id: InstrumentId
    bar_start_at: datetime
    bar_end_at: datetime
    available_at: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    turnover: Decimal
    price_basis: PriceBasis = PriceBasis.UNADJUSTED

    def __post_init__(self) -> None:
        require_aware(self.bar_start_at, "bar_start_at")
        require_aware(self.bar_end_at, "bar_end_at")
        require_aware(self.available_at, "available_at")
        if self.bar_end_at <= self.bar_start_at:
            raise DomainValidationError("minute bar end must follow its start")
        if (self.bar_end_at - self.bar_start_at).total_seconds() != 60:
            raise DomainValidationError("minute bar interval must be exactly 60 seconds")
        if self.available_at < self.bar_end_at:
            raise DomainValidationError("minute bar cannot be available before it is complete")
        if self.price_basis is not PriceBasis.UNADJUSTED:
            raise DomainValidationError("minute execution bar must be unadjusted")
        amounts = [self.open.amount, self.high.amount, self.low.amount, self.close.amount]
        currencies = {
            self.open.currency,
            self.high.currency,
            self.low.currency,
            self.close.currency,
        }
        if len(currencies) != 1:
            raise DomainValidationError("minute OHLC prices must use one currency")
        if self.low.amount > min(amounts) or self.high.amount < max(amounts):
            raise DomainValidationError("minute OHLC values are inconsistent with high/low")
        if self.turnover < 0 or not self.turnover.is_finite():
            raise DomainValidationError("minute turnover must be a finite non-negative Decimal")


@dataclass(frozen=True, slots=True)
class MinuteClose:
    """Completed one-minute close projected onto a signal price basis."""

    instrument_id: InstrumentId
    bar_start_at: datetime
    bar_end_at: datetime
    available_at: datetime
    close: Price
    price_basis: PriceBasis = PriceBasis.BACK_ADJUSTED

    def __post_init__(self) -> None:
        require_aware(self.bar_start_at, "bar_start_at")
        require_aware(self.bar_end_at, "bar_end_at")
        require_aware(self.available_at, "available_at")
        if (self.bar_end_at - self.bar_start_at).total_seconds() != 60:
            raise DomainValidationError("minute close interval must be exactly 60 seconds")
        if self.available_at < self.bar_end_at:
            raise DomainValidationError("minute close cannot be available before completion")
        if self.price_basis is not PriceBasis.BACK_ADJUSTED:
            raise DomainValidationError("minute signal close must be back-adjusted")


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One versioned, point-in-time corporate-action fact.

    ``replay_available_at`` is the validated historical clock used by a replay.
    It is deliberately separate from ``ingested_at`` so downloading an old
    announcement later cannot move the action's information clock. Economic
    terms are explicit and must never be inferred from adjusted prices.
    """

    action_id: StrongId
    source_action_id: str
    instrument_id: InstrumentId
    action_type: CorporateActionKind
    record_date: date
    ex_date: date
    source_released_at: datetime | None
    vendor_first_available_at: datetime | None
    ingested_at: datetime
    replay_available_at: datetime
    revision_no: int
    time_quality: TimeQuality
    provider: str
    source_url: str
    raw_response_sha256: str
    validation_status: str
    currency: str = "CNY"
    gross_cash_per_share: Decimal | None = None
    cash_pay_date: date | None = None
    share_multiplier: Decimal | None = None
    share_credit_date: date | None = None
    share_sellable_date: date | None = None
    rights_ratio: Decimal | None = None
    rights_subscription_price: Decimal | None = None
    rights_payment_deadline: date | None = None
    rights_listing_date: date | None = None

    def __post_init__(self) -> None:
        if type(self.record_date) is not date or type(self.ex_date) is not date:
            raise DomainValidationError("corporate-action record_date and ex_date must be dates")
        for field_name in (
            "cash_pay_date",
            "share_credit_date",
            "share_sellable_date",
            "rights_payment_deadline",
            "rights_listing_date",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not date:
                raise DomainValidationError(f"corporate-action {field_name} must be a date")
        if self.record_date >= self.ex_date:
            raise DomainValidationError("corporate-action record_date must precede ex_date")
        require_aware(self.ingested_at, "ingested_at")
        require_aware(self.replay_available_at, "replay_available_at")
        for field_name in ("source_released_at", "vendor_first_available_at"):
            value = getattr(self, field_name)
            if value is not None:
                require_aware(value, field_name)
                if self.replay_available_at < value:
                    raise DomainValidationError(
                        "replay_available_at cannot precede source or vendor availability"
                    )
        if self.revision_no < 0:
            raise DomainValidationError("corporate-action revision_no cannot be negative")
        for field_name in ("source_action_id", "provider", "source_url"):
            value = getattr(self, field_name)
            if not value.strip():
                raise DomainValidationError(f"corporate-action {field_name} cannot be blank")
        digest = self.raw_response_sha256.removeprefix("sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise DomainValidationError(
                "corporate-action raw_response_sha256 must contain 64 lowercase hex digits"
            )
        if self.validation_status not in {
            "validated",
            "unverified",
            "blocked_time_quality",
            "rejected",
        }:
            raise DomainValidationError("invalid corporate-action validation_status")
        Money.zero(self.currency)
        self._validate_terms()

    @property
    def available_at(self) -> datetime | None:
        """Earliest validated clock at which this revision was knowable."""

        if self.validation_status != "validated":
            return None
        if self.time_quality is TimeQuality.ESTIMATED_RESEARCH_ONLY:
            return None
        return self.replay_available_at

    def _validate_terms(self) -> None:
        numeric_values = {
            "gross_cash_per_share": self.gross_cash_per_share,
            "share_multiplier": self.share_multiplier,
            "rights_ratio": self.rights_ratio,
            "rights_subscription_price": self.rights_subscription_price,
        }
        for field_name, value in numeric_values.items():
            if value is not None and not value.is_finite():
                raise DomainValidationError(
                    f"corporate-action {field_name} must be a finite Decimal"
                )

        if self.action_type is CorporateActionKind.CASH_DIVIDEND:
            if self.gross_cash_per_share is None or self.gross_cash_per_share <= 0:
                raise DomainValidationError("cash dividend requires positive gross_cash_per_share")
            if any(
                value is not None
                for value in (
                    self.share_multiplier,
                    self.share_credit_date,
                    self.share_sellable_date,
                    self.rights_ratio,
                    self.rights_subscription_price,
                    self.rights_payment_deadline,
                    self.rights_listing_date,
                )
            ):
                raise DomainValidationError("cash dividend contains incompatible economic terms")
            if self.cash_pay_date is None or self.cash_pay_date < self.ex_date:
                raise DomainValidationError("cash dividend requires pay date on or after ex_date")
            return

        if self.action_type in {
            CorporateActionKind.SHARE_DISTRIBUTION,
            CorporateActionKind.STOCK_SPLIT,
            CorporateActionKind.REVERSE_SPLIT,
        }:
            multiplier = self.share_multiplier
            if multiplier is None or multiplier <= 0 or multiplier == 1:
                raise DomainValidationError("share action requires a positive non-unit multiplier")
            if self.action_type is CorporateActionKind.REVERSE_SPLIT and multiplier >= 1:
                raise DomainValidationError("reverse split multiplier must be below one")
            if self.action_type is not CorporateActionKind.REVERSE_SPLIT and multiplier <= 1:
                raise DomainValidationError(
                    "share distribution or split multiplier must exceed one"
                )
            if self.share_credit_date is None or self.share_credit_date < self.ex_date:
                raise DomainValidationError(
                    "share action requires a credit date on or after ex_date"
                )
            if (
                self.share_sellable_date is None
                or self.share_sellable_date < self.share_credit_date
            ):
                raise DomainValidationError(
                    "share action requires sellable date on or after credit date"
                )
            if any(
                value is not None
                for value in (
                    self.gross_cash_per_share,
                    self.cash_pay_date,
                    self.rights_ratio,
                    self.rights_subscription_price,
                    self.rights_payment_deadline,
                    self.rights_listing_date,
                )
            ):
                raise DomainValidationError("share action contains incompatible economic terms")
            return

        if self.action_type is CorporateActionKind.RIGHTS_ISSUE:
            if self.rights_ratio is None or self.rights_ratio <= 0:
                raise DomainValidationError("rights issue requires a positive rights_ratio")
            if self.rights_subscription_price is None or self.rights_subscription_price <= 0:
                raise DomainValidationError(
                    "rights issue requires a positive rights_subscription_price"
                )
            if self.rights_payment_deadline is None or self.rights_listing_date is None:
                raise DomainValidationError(
                    "rights issue requires payment deadline and listing date"
                )
            if self.rights_payment_deadline < self.ex_date:
                raise DomainValidationError("rights payment deadline cannot precede ex_date")
            if self.rights_listing_date < self.ex_date:
                raise DomainValidationError("rights listing date cannot precede ex_date")
            if self.gross_cash_per_share is not None or self.share_multiplier is not None:
                raise DomainValidationError("rights issue contains incompatible economic terms")
            if any(
                value is not None
                for value in (
                    self.cash_pay_date,
                    self.share_credit_date,
                    self.share_sellable_date,
                )
            ):
                raise DomainValidationError("rights issue contains incompatible settlement dates")


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """A catalog-coded event payload without provider-specific executable data."""

    event_id: StrongId
    event_code: str
    instrument_id: InstrumentId
    attributes: Mapping[str, str | int | Decimal | bool | None]

    def __post_init__(self) -> None:
        if not self.event_code or len(self.event_code) > 128:
            raise DomainValidationError("event_code must contain 1-128 characters")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Point-in-time lifecycle for a market event and its revision."""

    event: MarketEvent
    occurred_at: datetime | None
    source_released_at: datetime | None
    vendor_first_available_at: datetime | None
    ingested_at: datetime
    revision_no: int
    time_quality: TimeQuality
    replay_available_at: datetime | None = None
    source_event_id: str | None = None
    provider: str | None = None
    source_url: str | None = None
    raw_response_sha256: str | None = None
    validation_status: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.ingested_at, "ingested_at")
        for field_name in (
            "occurred_at",
            "source_released_at",
            "vendor_first_available_at",
            "replay_available_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_aware(value, field_name)
        if self.revision_no < 0:
            raise DomainValidationError("revision_no cannot be negative")
        if self.replay_available_at is not None and any(
            value is not None and self.replay_available_at < value
            for value in (self.source_released_at, self.vendor_first_available_at)
        ):
            raise DomainValidationError(
                "replay_available_at cannot precede source or vendor availability"
            )
        for field_name in ("source_event_id", "provider", "source_url"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise DomainValidationError(f"{field_name} cannot be blank")
        if self.raw_response_sha256 is not None:
            digest = self.raw_response_sha256.removeprefix("sha256:")
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise DomainValidationError(
                    "raw_response_sha256 must contain 64 lowercase hexadecimal digits"
                )
        if self.validation_status not in {
            None,
            "validated",
            "unverified",
            "blocked_time_quality",
            "rejected",
        }:
            raise DomainValidationError("invalid event validation_status")

    @property
    def available_at(self) -> datetime | None:
        """Earliest defensible time at which a trading strategy may use the fact."""

        if self.time_quality is TimeQuality.ESTIMATED_RESEARCH_ONLY:
            return None
        if self.validation_status in {"unverified", "blocked_time_quality", "rejected"}:
            return None
        # Historical event snapshots are usually downloaded long after the
        # announcement.  Their real ``ingested_at`` must remain auditable, but
        # it must not move an archived event into the download date.  A
        # validated snapshot therefore pins an explicit historical replay
        # clock.  Legacy/live envelopes without that field retain the original
        # max(source, vendor, ingestion) behavior below.
        if self.replay_available_at is not None:
            return self.replay_available_at
        candidates = [
            value
            for value in (self.source_released_at, self.vendor_first_available_at, self.ingested_at)
            if value is not None
        ]
        return max(candidates)

    def is_visible_at(self, clock: datetime) -> bool:
        require_aware(clock, "clock")
        available_at = self.available_at
        return available_at is not None and available_at <= clock
