"""HTTP contracts for confirmed portfolio imports and evidence-bounded reviews."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator

from ashare_lab.application.portfolio_review import (
    CashFlowRecord,
    DailyEquityRecord,
    HoldingRecord,
    MarketTickRecord,
    PortfolioReviewInput,
    ReviewAccount,
    TradeRecord,
    infer_broker_ledger_import,
)
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument

from .schemas import ApiModel

PROFILE = "CN_A_HK_CASH_V1"
Currency = Literal["CNY", "HKD"]
Market = Literal["CN_A", "HK"]
ImportMethod = Literal["screenshot", "csv_xlsx", "manual"]
TradeSide = Literal["buy", "sell"]
CashFlowKind = Literal[
    "deposit",
    "withdrawal",
    "in_kind_transfer_in",
    "in_kind_transfer_out",
    "dividend",
    "interest",
    "fee",
    "tax",
    "other",
]
TickGranularity = Literal["tick", "1m", "5m", "15m", "30m", "60m", "1d"]
CapabilityGrade = Literal["available", "partial", "unavailable"]

_EXTERNAL_KINDS = {
    "deposit",
    "withdrawal",
    "in_kind_transfer_in",
    "in_kind_transfer_out",
}
_CASH_FLOW_ALIASES = {
    "asset_transfer_in": "in_kind_transfer_in",
    "asset_transfer_out": "in_kind_transfer_out",
}
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HK_SYMBOL_RE = re.compile(r"^(\d{1,5})(?:\.HK)?$", re.IGNORECASE)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value


def _optional_aware(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _optional_upper(value: object) -> object:
    return None if value is None else _upper(value)


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _cash_flow_kind(value: object) -> object:
    lowered = _lower(value)
    return _CASH_FLOW_ALIASES.get(lowered, lowered) if isinstance(lowered, str) else lowered


def _canonical_symbol(market: Market, value: str) -> str:
    if market == "CN_A":
        try:
            return normalize_a_share_instrument(value).value
        except AshareInstrumentCodeError as exc:
            raise ValueError(str(exc)) from exc
    match = _HK_SYMBOL_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("HK symbol must contain one to five digits with an optional .HK suffix")
    return f"{int(match.group(1)):05d}.HK"


class ImportedRecordRequest(ApiModel):
    """Optional source identity retained across every import adapter."""

    source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    content_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class PortfolioImportMetadataRequest(ApiModel):
    method: ImportMethod = "csv_xlsx"
    confirmed: Literal[True] = True
    confirmed_at: datetime | None = None

    _validate_confirmed_at = field_validator("confirmed_at")(_optional_aware)


class PortfolioAccountRequest(ApiModel):
    period_start: date
    period_end: date
    base_currency: Currency
    timezone: str = Field(min_length=1, max_length=64)

    _normalize_base_currency = field_validator("base_currency", mode="before")(_upper)

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        return value

    @model_validator(mode="after")
    def period_is_ordered(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        return self


class PortfolioHoldingRequest(ImportedRecordRequest):
    as_of: datetime
    market: Market
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0)
    market_price: Decimal = Field(gt=0)
    average_cost: Decimal | None = Field(default=None, ge=0)
    currency: Currency
    fx_to_base: Decimal | None = Field(default=None, gt=0)

    _validate_as_of = field_validator("as_of")(_aware)
    _normalize_market = field_validator("market", mode="before")(_upper)
    _normalize_currency = field_validator("currency", mode="before")(_upper)

    @model_validator(mode="after")
    def normalize_security_symbol(self) -> Self:
        self.symbol = _canonical_symbol(self.market, self.symbol)
        return self


class PortfolioTradeRequest(ImportedRecordRequest):
    executed_at: datetime
    timestamp_precision: Literal["exact", "date_only"] = "exact"
    market: Market
    symbol: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    side: TradeSide
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    currency: Currency
    fx_to_base: Decimal | None = Field(default=None, gt=0)
    realized_pnl: Decimal | None = None

    _validate_executed_at = field_validator("executed_at")(_aware)
    _normalize_market = field_validator("market", mode="before")(_upper)
    _normalize_side = field_validator("side", mode="before")(_lower)
    _normalize_currency = field_validator("currency", mode="before")(_upper)

    @model_validator(mode="after")
    def normalize_security_symbol(self) -> Self:
        self.symbol = _canonical_symbol(self.market, self.symbol)
        return self


class PortfolioCashFlowRequest(ImportedRecordRequest):
    occurred_at: datetime
    kind: CashFlowKind
    amount: Decimal = Field(gt=0)
    currency: Currency
    fx_to_base: Decimal | None = Field(default=None, gt=0)
    external: bool | None = None

    _validate_occurred_at = field_validator("occurred_at")(_aware)
    _normalize_kind = field_validator("kind", mode="before")(_cash_flow_kind)
    _normalize_currency = field_validator("currency", mode="before")(_upper)

    @model_validator(mode="after")
    def external_flag_matches_kind(self) -> Self:
        expected = self.kind in _EXTERNAL_KINDS
        if self.external is not None and self.external is not expected:
            raise ValueError(
                "external must be true only for deposits, withdrawals, and in-kind transfers"
            )
        return self

    @property
    def normalized_external(self) -> bool:
        return self.kind in _EXTERNAL_KINDS


class PortfolioDailyEquityRequest(ImportedRecordRequest):
    at: date
    equity_base: Decimal = Field(gt=0)
    external_inflow_base: Decimal | None = Field(default=None, ge=0)
    external_outflow_base: Decimal | None = Field(default=None, ge=0)
    # Compatibility for the current H5 CSV mapper.  Positive means inflow;
    # negative means outflow.  New adapters should send the split fields above.
    external_flow_base: Decimal | None = None

    @field_validator("at", mode="before")
    @classmethod
    def normalize_account_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str) and "T" in value:
            return value.split("T", maxsplit=1)[0]
        return value

    @model_validator(mode="after")
    def flow_fields_are_unambiguous(self) -> Self:
        if self.external_flow_base is not None and (
            self.external_inflow_base is not None or self.external_outflow_base is not None
        ):
            raise ValueError(
                "external_flow_base cannot be combined with split inflow/outflow fields"
            )
        return self

    @property
    def normalized_inflow(self) -> Decimal | None:
        if self.external_flow_base is None:
            return self.external_inflow_base
        return max(self.external_flow_base, Decimal("0"))

    @property
    def normalized_outflow(self) -> Decimal | None:
        if self.external_flow_base is None:
            return self.external_outflow_base
        return max(-self.external_flow_base, Decimal("0"))


class PortfolioMarketTickRequest(ImportedRecordRequest):
    at: datetime
    # Missing granularity in the current CSV template is conservatively a daily
    # observation.  It is never upgraded to a true tick.
    granularity: TickGranularity = "1d"
    market: Market | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    last_price: Decimal | None = Field(default=None, gt=0)
    account_equity_base: Decimal | None = Field(default=None, gt=0)
    currency: Currency | None = None
    fx_to_base: Decimal | None = Field(default=None, gt=0)

    _validate_at = field_validator("at")(_aware)
    _normalize_market = field_validator("market", mode="before")(_optional_upper)
    _normalize_currency = field_validator("currency", mode="before")(_optional_upper)
    _normalize_granularity = field_validator("granularity", mode="before")(_lower)

    @model_validator(mode="after")
    def tick_has_one_supported_observation(self) -> Self:
        if self.last_price is None and self.account_equity_base is None:
            raise ValueError("market tick requires last_price or account_equity_base")
        if self.last_price is not None and (
            self.market is None or self.symbol is None or self.currency is None
        ):
            raise ValueError("last_price requires market, symbol, and currency")
        if self.last_price is None and (self.currency is not None or self.fx_to_base is not None):
            raise ValueError("currency and fx_to_base are only valid with last_price")
        if self.symbol is not None:
            if self.market is None:
                raise ValueError("symbol requires market")
            self.symbol = _canonical_symbol(self.market, self.symbol)
        return self


class PortfolioReviewRequest(ApiModel):
    profile: Literal["CN_A_HK_CASH_V1"] = PROFILE
    import_metadata: PortfolioImportMetadataRequest = Field(
        default_factory=PortfolioImportMetadataRequest
    )
    account: PortfolioAccountRequest
    holdings: tuple[PortfolioHoldingRequest, ...] = Field(default=(), max_length=500)
    trades: tuple[PortfolioTradeRequest, ...] = Field(default=(), max_length=5_000)
    cash_flows: tuple[PortfolioCashFlowRequest, ...] = Field(default=(), max_length=5_000)
    daily_equity: tuple[PortfolioDailyEquityRequest, ...] = Field(default=(), max_length=5_000)
    market_ticks: tuple[PortfolioMarketTickRequest, ...] = Field(default=(), max_length=10_000)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_h5_envelope(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalized: dict[str, object] = dict(cast(Mapping[str, object], value))
        if "import_metadata" not in normalized:
            normalized["import_metadata"] = {
                "method": normalized.pop("import_method", "csv_xlsx"),
                "confirmed": normalized.pop("confirmed", True),
            }
        account = normalized.get("account")
        holdings = normalized.get("holdings")
        if isinstance(account, Mapping) and isinstance(holdings, (list, tuple)):
            account_mapping = cast(Mapping[str, object], account)
            holding_rows = cast(Sequence[object], holdings)
            timezone = account_mapping.get("timezone")
            try:
                zone = ZoneInfo(str(timezone))
            except (ZoneInfoNotFoundError, ValueError):
                return normalized
            adjusted_holdings: list[object] = []
            for row in holding_rows:
                if not isinstance(row, Mapping):
                    adjusted_holdings.append(row)
                    continue
                adjusted: dict[str, object] = dict(cast(Mapping[str, object], row))
                raw_as_of = adjusted.get("as_of")
                if isinstance(raw_as_of, str) and _DATE_ONLY_RE.fullmatch(raw_as_of):
                    adjusted["as_of"] = datetime.combine(
                        date.fromisoformat(raw_as_of),
                        time(16),
                        tzinfo=zone,
                    ).isoformat()
                adjusted_holdings.append(adjusted)
            normalized["holdings"] = adjusted_holdings
        return normalized

    @model_validator(mode="after")
    def records_match_account_contract(self) -> Self:
        zone = ZoneInfo(self.account.timezone)
        base = self.account.base_currency
        seen_holdings: set[tuple[str, str]] = set()
        holding_times: set[datetime] = set()
        for item in self.holdings:
            _validate_market_currency(item.market, item.currency)
            _validate_fx(item.currency, item.fx_to_base, base)
            _validate_datetime_period(item.as_of, zone, self.account)
            key = (item.market, item.symbol)
            if key in seen_holdings:
                raise ValueError("holdings must contain one aggregated row per market and symbol")
            seen_holdings.add(key)
            holding_times.add(item.as_of)
        if holding_times and len(holding_times) != 1:
            raise ValueError("all holdings must share the same as_of timestamp")

        for item in self.trades:
            _validate_market_currency(item.market, item.currency)
            _validate_fx(item.currency, item.fx_to_base, base)
            _validate_datetime_period(item.executed_at, zone, self.account)
        for item in self.cash_flows:
            _validate_fx(item.currency, item.fx_to_base, base)
            _validate_datetime_period(item.occurred_at, zone, self.account)
        for item in self.market_ticks:
            if item.currency is not None:
                if item.market is None:
                    raise ValueError("market tick currency requires market")
                _validate_market_currency(item.market, item.currency)
                _validate_fx(item.currency, item.fx_to_base, base)
            _validate_datetime_period(item.at, zone, self.account)

        daily_dates: set[date] = set()
        for item in self.daily_equity:
            _validate_date_period(item.at, self.account)
            if item.at in daily_dates:
                raise ValueError("daily_equity.at values must be unique")
            daily_dates.add(item.at)
        if not any(
            (
                self.holdings,
                self.trades,
                self.cash_flows,
                self.daily_equity,
                self.market_ticks,
            )
        ):
            raise ValueError("portfolio review requires at least one confirmed record")
        _reject_duplicate_import_records(self)
        return self

    def to_application_input(self) -> PortfolioReviewInput:
        return PortfolioReviewInput(
            profile=self.profile,
            import_method=self.import_metadata.method,
            confirmed=self.import_metadata.confirmed,
            account=ReviewAccount(
                period_start=self.account.period_start,
                period_end=self.account.period_end,
                base_currency=self.account.base_currency,
                timezone=self.account.timezone,
            ),
            holdings=tuple(
                HoldingRecord(
                    source_id=item.source_id,
                    content_sha256=item.content_sha256,
                    as_of=item.as_of,
                    market=item.market,
                    symbol=item.symbol,
                    name=item.name,
                    quantity=item.quantity,
                    market_price=item.market_price,
                    average_cost=item.average_cost,
                    currency=item.currency,
                    fx_to_base=item.fx_to_base,
                )
                for item in self.holdings
            ),
            trades=tuple(
                TradeRecord(
                    source_id=item.source_id,
                    content_sha256=item.content_sha256,
                    executed_at=item.executed_at,
                    timestamp_precision=item.timestamp_precision,
                    market=item.market,
                    symbol=item.symbol,
                    name=item.name,
                    side=item.side,
                    quantity=item.quantity,
                    price=item.price,
                    fees=item.fees,
                    currency=item.currency,
                    fx_to_base=item.fx_to_base,
                    realized_pnl=item.realized_pnl,
                )
                for item in self.trades
            ),
            cash_flows=tuple(
                CashFlowRecord(
                    source_id=item.source_id,
                    content_sha256=item.content_sha256,
                    occurred_at=item.occurred_at,
                    kind=item.kind,
                    amount=item.amount,
                    currency=item.currency,
                    fx_to_base=item.fx_to_base,
                    external=item.normalized_external,
                )
                for item in self.cash_flows
            ),
            daily_equity=tuple(
                DailyEquityRecord(
                    source_id=item.source_id,
                    content_sha256=item.content_sha256,
                    at=item.at,
                    equity_base=item.equity_base,
                    external_inflow_base=item.normalized_inflow,
                    external_outflow_base=item.normalized_outflow,
                )
                for item in self.daily_equity
            ),
            market_ticks=tuple(
                MarketTickRecord(
                    source_id=item.source_id,
                    content_sha256=item.content_sha256,
                    at=item.at,
                    granularity=item.granularity,
                    market=item.market,
                    symbol=item.symbol,
                    last_price=item.last_price,
                    account_equity_base=item.account_equity_base,
                    currency=item.currency,
                    fx_to_base=item.fx_to_base,
                )
                for item in self.market_ticks
            ),
        )


def _validate_market_currency(market: Market, currency: Currency) -> None:
    expected = "CNY" if market == "CN_A" else "HKD"
    if currency != expected:
        raise ValueError(f"{market} records must use {expected} before base-currency conversion")


def _validate_fx(currency: str, fx: Decimal | None, base_currency: str) -> None:
    if currency != base_currency and fx is None:
        raise ValueError("fx_to_base is required when record currency differs from base_currency")
    if currency == base_currency and fx is not None and fx != Decimal("1"):
        raise ValueError("fx_to_base must equal 1 when record currency equals base_currency")


def _validate_datetime_period(
    value: datetime,
    zone: ZoneInfo,
    account: PortfolioAccountRequest,
) -> None:
    _validate_date_period(value.astimezone(zone).date(), account)


def _validate_date_period(value: date, account: PortfolioAccountRequest) -> None:
    if not account.period_start <= value <= account.period_end:
        raise ValueError("record timestamp must fall within the account review period")


def _reject_duplicate_import_records(review: PortfolioReviewRequest) -> None:
    seen_source_ids: set[str] = set()
    groups = (
        ("holdings", review.holdings),
        ("trades", review.trades),
        ("cash_flows", review.cash_flows),
        ("daily_equity", review.daily_equity),
        ("market_ticks", review.market_ticks),
    )
    for _group_name, records in groups:
        for item in records:
            if item.source_id is not None:
                if item.source_id in seen_source_ids:
                    raise ValueError(f"duplicate import source_id: {item.source_id}")
                seen_source_ids.add(item.source_id)


class DataCapabilityPayload(ApiModel):
    grade: CapabilityGrade
    available: bool
    reason: str
    granularity: str | None


class DataCapabilitiesPayload(ApiModel):
    snapshot: DataCapabilityPayload
    trade_replay: DataCapabilityPayload
    performance: DataCapabilityPayload
    attribution: DataCapabilityPayload
    market_tick_replay: DataCapabilityPayload


class CapabilityProjectionPayload(ApiModel):
    snapshot: bool
    trade_replay: bool
    performance: bool
    attribution: bool
    market_tick_replay: bool


class ImportMetadataPayload(ApiModel):
    method: ImportMethod
    confirmed: Literal[True]


class ValidityPayload(ApiModel):
    valid: Literal[True]
    confirmed: Literal[True]
    record_count: int = Field(ge=1)
    security_identity_scope: Literal["syntactic_a_h_normalization_only"]


class DataQualityPayload(ApiModel):
    level: Literal["confirmed_import"]
    warnings: tuple[str, ...]


class NarrativeStatusPayload(ApiModel):
    available: bool
    mode: Literal["on_demand"]
    reason: str


class PeriodPayload(ApiModel):
    start: date
    end: date


class SummaryPayload(ApiModel):
    total_market_value_base: Decimal | None
    ending_equity_base: Decimal | None
    net_external_flow_base: Decimal
    external_flow_base: Decimal
    period_return_pct: Decimal | None
    twr_pct: Decimal | None
    max_drawdown_pct: Decimal | None


class SnapshotPositionPayload(ApiModel):
    source_id: str | None
    content_sha256: str | None
    as_of: datetime
    market: Market
    symbol: str
    name: str
    quantity: Decimal
    market_price: Decimal
    average_cost: Decimal | None
    currency: Currency
    fx_to_base: Decimal
    market_value: Decimal
    market_value_base: Decimal
    unrealized_pnl_base: Decimal | None


class HoldingProjectionPayload(SnapshotPositionPayload):
    weight_pct: Decimal


class SnapshotPayload(ApiModel):
    as_of: datetime
    total_market_value_base: Decimal
    positions: tuple[SnapshotPositionPayload, ...]


class TradeReplayEventPayload(ApiModel):
    source_index: int = Field(ge=0)
    source_id: str | None
    content_sha256: str | None
    executed_at: datetime
    timestamp_precision: Literal["exact", "date_only"]
    market: Market
    symbol: str
    name: str | None
    side: TradeSide
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: Currency
    fx_to_base: Decimal
    notional_base: Decimal
    fees_base: Decimal
    realized_pnl_base: Decimal | None
    realized_pnl_source: Literal["broker_reported", "fifo_derived"] | None


class TradeReplayPayload(ApiModel):
    granularity: Literal["executed_at", "date"]
    events: tuple[TradeReplayEventPayload, ...]


class CashFlowReplayEventPayload(ApiModel):
    source_index: int = Field(ge=0)
    source_id: str | None
    content_sha256: str | None
    occurred_at: datetime
    kind: CashFlowKind
    amount: Decimal
    currency: Currency
    fx_to_base: Decimal
    external: bool
    amount_base: Decimal
    external_flow_base: Decimal


class CashFlowReplayPayload(ApiModel):
    granularity: Literal["occurred_at"]
    events: tuple[CashFlowReplayEventPayload, ...]


class PerformancePointPayload(ApiModel):
    source_id: str | None
    content_sha256: str | None
    at: date
    equity_base: Decimal
    external_inflow_base: Decimal
    external_outflow_base: Decimal
    external_flow_base: Decimal
    period_return: Decimal | None
    linked_index: Decimal
    drawdown: Decimal


class PerformancePayload(ApiModel):
    method: Literal["linked_daily_ttwror.external_flows.v1"]
    granularity: Literal["1d"]
    twr: Decimal
    max_drawdown: Decimal
    start_equity_base: Decimal
    end_equity_base: Decimal
    points: tuple[PerformancePointPayload, ...]


class SeriesPointPayload(ApiModel):
    source_id: str | None
    content_sha256: str | None
    at: date
    equity_base: Decimal
    return_pct: Decimal | None
    drawdown_pct: Decimal


AttributionKind = Literal["trade_realized_pnl"]


class AttributionEntryPayload(ApiModel):
    kind: AttributionKind
    source_index: int = Field(ge=0)
    source_id: str | None
    content_sha256: str | None
    at: datetime
    market: Market
    symbol: str
    name: str
    amount_base: Decimal
    calculation_method: Literal["broker_reported", "fifo_derived"]
    evidence_fields: tuple[str, ...]
    share_of_verified_pnl: Decimal | None


class AttributionPayload(ApiModel):
    method: Literal["broker_reported_or_fifo_derived_realized_pnl.v1"]
    coverage: Literal["available", "partial"]
    verified_pnl_base: Decimal
    entries: tuple[AttributionEntryPayload, ...]


class MarketTickEventPayload(ApiModel):
    source_index: int = Field(ge=0)
    source_id: str | None
    content_sha256: str | None
    at: datetime
    granularity: TickGranularity
    market: Market | None
    symbol: str | None
    last_price: Decimal | None
    account_equity_base: Decimal | None
    currency: Currency | None
    fx_to_base: Decimal | None
    last_price_base: Decimal | None


class MarketTickReplayPayload(ApiModel):
    timestamp_origin: Literal["imported"]
    granularities: tuple[TickGranularity, ...]
    interpolation: Literal["none"]
    events: tuple[MarketTickEventPayload, ...]


ReplayKind = Literal[
    "buy",
    "sell",
    "deposit",
    "withdrawal",
    "in_kind_transfer_in",
    "in_kind_transfer_out",
    "dividend",
    "interest",
    "fee",
    "tax",
    "other",
    "account_equity",
    "market_observation",
]


class ReplayFramePayload(ApiModel):
    at: datetime
    kind: ReplayKind
    title: str
    market: Market | None
    symbol: str | None
    account_equity_base: Decimal | None
    last_price: Decimal | None
    return_pct: Decimal | None
    caption: str
    evidence: str
    source_kind: Literal["trade", "cash_flow", "daily_equity", "market_tick"]
    source_index: int = Field(ge=0)
    source_id: str | None
    content_sha256: str | None
    is_highlight: bool
    battle_cry: str | None


class HighlightPayload(ApiModel):
    kind: AttributionKind
    type: Literal["已实现盈利"]
    at: datetime
    market: Market
    symbol: str
    name: str
    amount_base: Decimal = Field(gt=0)
    impact_base: Decimal = Field(gt=0)
    impact_pct: Decimal | None
    title: str
    subtitle: str
    evidence: str
    calculation_method: Literal["broker_reported", "fifo_derived"]
    evidence_fields: tuple[str, ...]
    verification: Literal["derived_from_imported_records"]
    trade_source_index: int = Field(ge=0)
    source_id: str | None
    content_sha256: str | None
    start_tick_index: int | None = Field(default=None, ge=0)
    end_tick_index: int | None = Field(default=None, ge=0)


class PortfolioAccountResponse(ApiModel):
    period_start: date
    period_end: date
    base_currency: Currency
    timezone: str


class PortfolioReviewResponse(ApiModel):
    profile: Literal["CN_A_HK_CASH_V1"]
    import_metadata: ImportMetadataPayload
    validity: ValidityPayload
    account: PortfolioAccountResponse
    period: PeriodPayload
    summary: SummaryPayload
    capabilities: CapabilityProjectionPayload
    data_capabilities: DataCapabilitiesPayload
    data_quality: DataQualityPayload
    narrative_status: NarrativeStatusPayload
    snapshot: SnapshotPayload | None
    holdings: tuple[HoldingProjectionPayload, ...]
    trade_replay: TradeReplayPayload
    cash_flow_replay: CashFlowReplayPayload
    performance: PerformancePayload | None
    series: tuple[SeriesPointPayload, ...]
    attribution: AttributionPayload | None
    market_tick_replay: MarketTickReplayPayload
    replay_ticks: tuple[ReplayFramePayload, ...]
    highlights: tuple[HighlightPayload, ...] = Field(max_length=5)
    warnings: tuple[str, ...]


class PortfolioHighlightNarrationRequest(ApiModel):
    review: PortfolioReviewRequest
    highlight_index: int = Field(ge=0, le=4)


class NarrativeLikelyDriverPayload(ApiModel):
    reason: str
    confidence: Literal["high", "medium", "low"]
    source_ids: tuple[str, ...]


class NarrativeSourcePayload(ApiModel):
    source_id: str
    title: str
    url: str
    publisher: str
    published_at: str | None


class PortfolioHighlightNarrationResponse(ApiModel):
    highlight_index: int = Field(ge=0, le=4)
    headline: str
    empathetic_summary: str
    likely_drivers: tuple[NarrativeLikelyDriverPayload, ...]
    sources: tuple[NarrativeSourcePayload, ...]
    unresolved: tuple[str, ...]
    historical_market_evidence_count: int = Field(ge=0)


TabularCell = str | int | float | bool | None


class BrokerLedgerParseRequest(ApiModel):
    """Rows decoded from CSV/XLSX; account metadata is deliberately absent."""

    import_method: ImportMethod = "csv_xlsx"
    file_name: str | None = Field(default=None, min_length=1, max_length=255)
    rows: tuple[dict[str, TabularCell], ...] = Field(default=(), max_length=200)
    current_holding_rows: tuple[dict[str, TabularCell], ...] = Field(
        default=(),
        max_length=100,
    )

    @model_validator(mode="after")
    def has_ledger_or_current_holdings(self) -> BrokerLedgerParseRequest:
        if not self.rows and not self.current_holding_rows:
            raise ValueError("rows or current_holding_rows must contain at least one row")
        return self

    def infer_draft(self) -> dict[str, object]:
        inferred = infer_broker_ledger_import(
            self.rows,
            current_holding_rows=self.current_holding_rows,
            import_method=self.import_method,
        )
        reconciliation = inferred.get("reconciliation")
        draft = inferred.get("draft")
        if not isinstance(reconciliation, dict) or not isinstance(draft, dict):
            return inferred
        reconciliation_mapping = cast(dict[str, object], reconciliation)
        draft_mapping = cast(dict[str, object], draft)
        if reconciliation_mapping.get("can_analyze_after_confirmation") is not True:
            return inferred
        try:
            PortfolioReviewRequest.model_validate(
                {
                    "profile": inferred.get("profile"),
                    "import_metadata": {
                        "method": self.import_method,
                        "confirmed": True,
                    },
                    **draft_mapping,
                }
            )
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            reconciliation_mapping["can_analyze_after_confirmation"] = False
            reconciliation_mapping["status"] = "needs_supplement"
            reasons = reconciliation_mapping.get("reasons")
            existing_reasons = (
                tuple(item for item in cast(Sequence[object], reasons) if isinstance(item, str))
                if isinstance(reasons, (tuple, list))
                else ()
            )
            reconciliation_mapping["reasons"] = (
                *existing_reasons,
                "导入草稿未通过确认契约校验：" + str(first_error.get("msg", "invalid draft")),
            )
        return inferred


class ImportDraftMetadataPayload(ApiModel):
    method: ImportMethod
    confirmed: Literal[False]


class InferredAccountPayload(ApiModel):
    period_start: date
    period_end: date
    base_currency: Currency | None
    timezone: str
    markets: tuple[Market, ...]


class BrokerImportDraftPayload(ApiModel):
    account: PortfolioAccountRequest | None
    holdings: tuple[PortfolioHoldingRequest, ...]
    trades: tuple[PortfolioTradeRequest, ...]
    cash_flows: tuple[PortfolioCashFlowRequest, ...]
    daily_equity: tuple[PortfolioDailyEquityRequest, ...]
    market_ticks: tuple[PortfolioMarketTickRequest, ...]


class PositionBalanceDraftPayload(ApiModel):
    source_id: str
    content_sha256: str
    as_of: datetime
    market: Market
    symbol: str
    name: str
    quantity: Decimal
    market_price: Decimal | None
    average_cost: Decimal | None
    currency: Currency
    fx_to_base: Decimal | None
    reconstructable_holding: bool


class BrokerImportReconciliationPayload(ApiModel):
    status: Literal["ready_for_confirmation", "needs_base_currency", "needs_supplement"]
    can_analyze_after_confirmation: bool
    needs_current_holdings: bool
    needs_opening_positions: bool
    holding_source: (
        Literal[
            "supplemental_current_holdings",
            "broker_reported_closing_balance",
        ]
        | None
    )
    reasons: tuple[str, ...]


class UnresolvedBrokerRowPayload(ApiModel):
    source_index: int = Field(ge=0)
    reason: str


class BrokerLedgerParseResponse(ApiModel):
    profile: Literal["CN_A_HK_CASH_V1"]
    security_identity_scope: Literal["syntactic_a_h_normalization_only"]
    import_metadata: ImportDraftMetadataPayload
    inferred_account: InferredAccountPayload
    draft: BrokerImportDraftPayload
    position_balances: tuple[PositionBalanceDraftPayload, ...]
    reconciliation: BrokerImportReconciliationPayload
    unresolved_rows: tuple[UnresolvedBrokerRowPayload, ...]
    warnings: tuple[str, ...]


class CsvTypeContract(ApiModel):
    name: Literal["holdings", "trades", "cash_flows", "daily_equity", "market_ticks"]
    required: bool
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    time_semantics: str


class ImportMethodContract(ApiModel):
    method: ImportMethod
    label: str
    produces: Literal["confirmed_structured_json"]
    requires_preview_and_confirmation: Literal[True]
    adapter_boundary: str


class ParseLimitsPayload(ApiModel):
    max_request_bytes: Literal[2097152]
    max_ledger_rows: Literal[200]
    max_current_holding_rows: Literal[100]
    large_file_strategy: Literal["oversized_files_require_a_separate_streaming_import"]


class PortfolioImportContractResponse(ApiModel):
    profile: Literal["CN_A_HK_CASH_V1"]
    format_version: Literal["1.1"]
    transport: Literal["structured_json"]
    file_upload_supported: Literal[False]
    parse_limits: ParseLimitsPayload
    import_methods: tuple[ImportMethodContract, ImportMethodContract, ImportMethodContract]
    required_csv_types: tuple[Literal["holdings"], ...]
    csv_types: tuple[CsvTypeContract, ...]
    rules: tuple[str, ...]


def portfolio_import_contract() -> PortfolioImportContractResponse:
    return PortfolioImportContractResponse(
        profile=PROFILE,
        format_version="1.1",
        transport="structured_json",
        file_upload_supported=False,
        parse_limits=ParseLimitsPayload(
            max_request_bytes=2 * 1024 * 1024,
            max_ledger_rows=200,
            max_current_holding_rows=100,
            large_file_strategy="oversized_files_require_a_separate_streaming_import",
        ),
        import_methods=(
            ImportMethodContract(
                method="screenshot",
                label="截图识别",
                produces="confirmed_structured_json",
                requires_preview_and_confirmation=True,
                adapter_boundary=(
                    "vision/OCR only creates an editable draft; raw images are not "
                    "accepted by /analyze"
                ),
            ),
            ImportMethodContract(
                method="csv_xlsx",
                label="CSV / XLSX",
                produces="confirmed_structured_json",
                requires_preview_and_confirmation=True,
                adapter_boundary="browser parser maps tabular rows into the common draft",
            ),
            ImportMethodContract(
                method="manual",
                label="手工录入",
                produces="confirmed_structured_json",
                requires_preview_and_confirmation=True,
                adapter_boundary="form fields map directly into the common draft",
            ),
        ),
        required_csv_types=(),
        csv_types=(
            CsvTypeContract(
                name="holdings",
                required=False,
                required_fields=(
                    "as_of",
                    "market",
                    "symbol",
                    "name",
                    "quantity",
                    "market_price",
                    "currency",
                ),
                optional_fields=(
                    "average_cost",
                    "fx_to_base",
                    "source_id",
                    "content_sha256",
                ),
                time_semantics="all rows form one snapshot and share one offset-aware as_of",
            ),
            CsvTypeContract(
                name="trades",
                required=False,
                required_fields=(
                    "executed_at",
                    "market",
                    "symbol",
                    "side",
                    "quantity",
                    "price",
                    "fees",
                    "currency",
                ),
                optional_fields=(
                    "name",
                    "fx_to_base",
                    "realized_pnl",
                    "source_id",
                    "content_sha256",
                ),
                time_semantics="executed_at is the imported offset-aware execution timestamp",
            ),
            CsvTypeContract(
                name="cash_flows",
                required=False,
                required_fields=("occurred_at", "kind", "amount", "currency"),
                optional_fields=(
                    "fx_to_base",
                    "external",
                    "source_id",
                    "content_sha256",
                ),
                time_semantics="occurred_at is offset-aware; external direction follows kind",
            ),
            CsvTypeContract(
                name="daily_equity",
                required=False,
                required_fields=("at", "equity_base"),
                optional_fields=(
                    "external_inflow_base",
                    "external_outflow_base",
                    "external_flow_base",
                    "source_id",
                    "content_sha256",
                ),
                time_semantics=(
                    "at is the local account date; inflow is placed at interval start and "
                    "outflow at interval end"
                ),
            ),
            CsvTypeContract(
                name="market_ticks",
                required=False,
                required_fields=("at",),
                optional_fields=(
                    "granularity",
                    "market",
                    "symbol",
                    "last_price",
                    "account_equity_base",
                    "currency",
                    "fx_to_base",
                    "source_id",
                    "content_sha256",
                ),
                time_semantics="at is imported; omitted granularity degrades to 1d, never tick",
            ),
        ),
        rules=(
            "All three import methods produce the same confirmed JSON contract for /analyze.",
            (
                "Broker trade/delivery rows are the primary CSV/XLSX input; current holdings "
                "are supplemental when opening positions, corporate actions, or closing "
                "balances cannot be reconciled."
            ),
            (
                "The current H5 POST itself is the confirmation boundary when "
                "import_metadata is omitted."
            ),
            (
                "Send decimal values as base-10 strings; binary floating-point is not "
                "used in arithmetic."
            ),
            "Every non-base-currency monetary record requires a positive fx_to_base.",
            (
                "A-share symbols are normalized by the existing mainland identity gate; "
                "HK symbols become five-digit .HK ids."
            ),
            (
                "TWR and observed drawdown require at least two daily_equity rows; full "
                "performance capability requires at least three observations including "
                "both account-period endpoints."
            ),
            (
                "Broker-reported realized P&L takes precedence; otherwise FIFO is derived "
                "only when earlier imported buys fully cover that sell, including fees and FX."
            ),
            (
                "FIFO is disabled after a same-symbol day that mixes buys and sells when any "
                "record has date-only timestamp precision."
            ),
            (
                "An imported tick label remains customer-reported until provider granularity "
                "and rights are verified; bars are never relabelled as ticks."
            ),
        ),
    )


__all__ = [
    "BrokerLedgerParseRequest",
    "BrokerLedgerParseResponse",
    "PortfolioHighlightNarrationRequest",
    "PortfolioHighlightNarrationResponse",
    "PortfolioImportContractResponse",
    "PortfolioReviewRequest",
    "PortfolioReviewResponse",
    "portfolio_import_contract",
]
