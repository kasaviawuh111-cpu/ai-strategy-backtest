"""Strict, thin BaoStock daily acquisition for SH/SZ A-share research.

The adapter accepts an already configured BaoStock-compatible client.  It does
not import the SDK, log in, log out, retry, publish a snapshot, or perform any
network operation of its own.  Requests are split at calendar-year boundaries
so every ``query_history_k_data_plus`` result is bounded to at most 366 days.

BaoStock exposes parsed SDK rows rather than the HTTP/socket bytes that produced
them.  Consequently, the audit digest in this module is explicitly a SHA-256 of
the canonical normalized SDK result.  It is not, and must never be described as,
a raw-wire response hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, cast

from ashare_lab.domain.market_data import PriceBasis, normalize_a_share_instrument
from ashare_lab.domain.shared import InstrumentId

PROVIDER = "baostock_python_api"
DATASET = "history_k_data_plus_daily"
DAILY_FIELD_ORDER = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "tradestatus",
    "isST",
)
DAILY_FIELDS = ",".join(DAILY_FIELD_ORDER)
FREQUENCY = "d"
MAX_QUERY_CALENDAR_DAYS = 366
VOLUME_SOURCE_UNIT = "share"
TURNOVER_RATE_PROVIDER = PROVIDER
TURNOVER_RATE_METHODOLOGY = (
    "baostock.history_k_data_plus.turn.provider_reported_turnover_rate_pct.v1"
)
NORMALIZED_RESPONSE_HASH_SEMANTICS = "sha256_of_canonical_normalized_sdk_result_not_raw_wire_bytes"

_REQUIRED_FIELDS = frozenset(DAILY_FIELD_ORDER)
_ADJUST_FLAG_BY_PRICE_BASIS = {
    PriceBasis.UNADJUSTED: "3",
    PriceBasis.BACK_ADJUSTED: "1",
}

type NormalizedRow = tuple[tuple[str, str], ...]


class BaoStockResult(Protocol):
    """Narrow result surface used from the BaoStock SDK."""

    fields: Sequence[object]
    error_code: object
    error_msg: object

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[object]: ...


class BaoStockDailyClient(Protocol):
    """Injectable client protocol; a logged-in ``baostock`` module satisfies it."""

    def query_history_k_data_plus(
        self,
        *,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockResult: ...


class BaoStockDailySourceError(RuntimeError):
    """A BaoStock result cannot be safely normalized for research replay."""

    def __init__(
        self,
        message: str,
        *,
        query_audits: tuple[BaoStockDailyQueryAudit, ...] = (),
    ) -> None:
        super().__init__(message)
        self.query_audits = query_audits


@dataclass(frozen=True, slots=True)
class BaoStockDailyQueryAudit:
    """One annual request and a digest of its normalized SDK result."""

    method: str
    request_params: tuple[tuple[str, str], ...]
    response_fields: tuple[str, ...]
    normalized_rows: tuple[NormalizedRow, ...]
    error_code: str
    error_message: str
    normalized_response_sha256: str
    hash_semantics: str = NORMALIZED_RESPONSE_HASH_SEMANTICS

    @property
    def row_count(self) -> int:
        return len(self.normalized_rows)

    @property
    def zero_result(self) -> bool:
        return not self.normalized_rows

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "requestParams": dict(self.request_params),
            "responseFields": list(self.response_fields),
            "normalizedRows": [dict(row) for row in self.normalized_rows],
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "rowCount": self.row_count,
            "zeroResult": self.zero_result,
            "normalizedResponseSha256": self.normalized_response_sha256,
            "hashSemantics": self.hash_semantics,
            "rawWireCaptured": False,
        }


@dataclass(frozen=True, slots=True)
class BaoStockDailyRow:
    """One validated BaoStock daily row in provider units."""

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    preclose: Decimal
    volume: int
    amount: Decimal
    turnover_rate_pct: Decimal
    tradestatus: Literal["0", "1"]
    is_st: bool

    @property
    def trade_status(self) -> Literal["0", "1"]:
        return self.tradestatus

    def as_snapshot_row(self, instrument_id: InstrumentId) -> dict[str, object]:
        """Project the provider row to the common daily snapshot column names."""

        return {
            "stock_code": str(instrument_id),
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "turnover_rate_pct": self.turnover_rate_pct,
            "turnover_rate_provider": TURNOVER_RATE_PROVIDER,
            "turnover_rate_methodology": TURNOVER_RATE_METHODOLOGY,
        }


@dataclass(frozen=True, slots=True)
class BaoStockDailyCollection:
    """Validated rows and complete annual-query evidence for one price basis."""

    instrument_id: InstrumentId
    provider_code: str
    price_basis: PriceBasis
    provider_name: str
    dataset_name: str
    requested_start: date
    requested_end: date
    rows: tuple[BaoStockDailyRow, ...]
    query_audits: tuple[BaoStockDailyQueryAudit, ...]
    source_volume_unit: str = VOLUME_SOURCE_UNIT
    normalized_response_hash_semantics: str = NORMALIZED_RESPONSE_HASH_SEMANTICS

    @property
    def adjustflag(self) -> str:
        return _ADJUST_FLAG_BY_PRICE_BASIS[self.price_basis]

    @property
    def turnover_price_range_checked(self) -> bool:
        """Whether amount/volume was checked against the returned OHLC range.

        BaoStock keeps amount and volume on their original economic scale while
        back-adjusting prices.  Applying the unadjusted amount/volume identity to
        those adjusted prices would reject valid data, so it is intentionally
        performed only for unadjusted rows.
        """

        return self.price_basis is PriceBasis.UNADJUSTED


class BaoStockDailyResearchSource:
    """Collect one unadjusted or back-adjusted SH/SZ daily series."""

    def __init__(self, client: BaoStockDailyClient) -> None:
        self._client = client

    def fetch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        price_basis: PriceBasis,
    ) -> BaoStockDailyCollection:
        """Run bounded annual queries and return validated, date-ordered rows."""

        _validate_date_range(start, end)
        if type(price_basis) is not PriceBasis or price_basis not in _ADJUST_FLAG_BY_PRICE_BASIS:
            raise ValueError("price_basis must be UNADJUSTED or BACK_ADJUSTED")
        canonical_instrument, provider_code = _identity(instrument_id)
        adjustflag = _ADJUST_FLAG_BY_PRICE_BASIS[price_basis]

        audits: list[BaoStockDailyQueryAudit] = []
        rows: list[BaoStockDailyRow] = []
        for interval_start, interval_end in _annual_intervals(start, end):
            params = (
                ("code", provider_code),
                ("fields", DAILY_FIELDS),
                ("start_date", interval_start.isoformat()),
                ("end_date", interval_end.isoformat()),
                ("frequency", FREQUENCY),
                ("adjustflag", adjustflag),
            )
            audit = self._query(
                params=params,
                request=lambda interval_start=interval_start, interval_end=interval_end: (
                    self._client.query_history_k_data_plus(
                        code=provider_code,
                        fields=DAILY_FIELDS,
                        start_date=interval_start.isoformat(),
                        end_date=interval_end.isoformat(),
                        frequency=FREQUENCY,
                        adjustflag=adjustflag,
                    )
                ),
            )
            audits.append(audit)
            if audit.error_code != "0":
                raise BaoStockDailySourceError(
                    "query_history_k_data_plus failed with provider/adapter code "
                    f"{audit.error_code}",
                    query_audits=tuple(audits),
                )
            try:
                rows.extend(
                    _rows_from_audit(
                        audit,
                        interval_start=interval_start,
                        interval_end=interval_end,
                        price_basis=price_basis,
                    )
                )
            except BaoStockDailySourceError as exc:
                raise BaoStockDailySourceError(
                    str(exc),
                    query_audits=tuple(audits),
                ) from exc

        ordered = tuple(sorted(rows, key=lambda row: row.date))
        dates = tuple(row.date for row in ordered)
        if len(dates) != len(set(dates)):
            raise BaoStockDailySourceError(
                "annual BaoStock query results contain duplicate trade dates",
                query_audits=tuple(audits),
            )
        return BaoStockDailyCollection(
            instrument_id=canonical_instrument,
            provider_code=provider_code,
            price_basis=price_basis,
            provider_name=PROVIDER,
            dataset_name=DATASET,
            requested_start=start,
            requested_end=end,
            rows=ordered,
            query_audits=tuple(audits),
        )

    @staticmethod
    def _query(
        *,
        params: tuple[tuple[str, str], ...],
        request: Callable[[], BaoStockResult],
    ) -> BaoStockDailyQueryAudit:
        try:
            return _capture_result(params=params, result=request())
        except Exception as exc:  # pragma: no cover - exact SDK failures vary by release
            return _make_audit(
                params=params,
                fields=(),
                rows=(),
                error_code="client_exception",
                error_message=type(exc).__name__,
            )


def _identity(instrument_id: InstrumentId) -> tuple[InstrumentId, str]:
    try:
        canonical = normalize_a_share_instrument(instrument_id)
    except ValueError as exc:
        raise ValueError("instrument_id must be a canonical mainland A-share identifier") from exc
    if str(canonical) != str(instrument_id):
        raise ValueError(f"instrument_id must be canonical: {canonical}")
    code, exchange = str(canonical).split(".", maxsplit=1)
    if exchange not in {"SH", "SZ"}:
        raise ValueError("BaoStock daily fallback supports SH and SZ A-shares only")
    return canonical, f"{exchange.lower()}.{code}"


def _validate_date_range(start: date, end: date) -> None:
    if type(start) is not date or type(end) is not date:
        raise TypeError("start and end must be date values")
    if start > end:
        raise ValueError("start must not be later than end")


def _annual_intervals(start: date, end: date) -> tuple[tuple[date, date], ...]:
    return tuple(
        (max(start, date(year, 1, 1)), min(end, date(year, 12, 31)))
        for year in range(start.year, end.year + 1)
    )


def _capture_result(
    *,
    params: tuple[tuple[str, str], ...],
    result: BaoStockResult,
) -> BaoStockDailyQueryAudit:
    error_code = _result_text(result.error_code)
    error_message = _result_text(result.error_msg)

    fields: list[str] = []
    for field in tuple(result.fields):
        if not isinstance(field, str) or not field.strip():
            return _make_audit(
                params=params,
                fields=(),
                rows=(),
                error_code="adapter_schema_error",
                error_message="invalid result fields",
            )
        fields.append(field.strip())
    if len(fields) != len(set(fields)):
        return _make_audit(
            params=params,
            fields=tuple(fields),
            rows=(),
            error_code="adapter_schema_error",
            error_message="duplicate result fields",
        )

    rows: list[NormalizedRow] = []
    if error_code == "0":
        try:
            while result.next():
                raw_row = tuple(result.get_row_data())
                if len(raw_row) != len(fields) or any(
                    not isinstance(value, str) for value in raw_row
                ):
                    return _make_audit(
                        params=params,
                        fields=tuple(fields),
                        rows=tuple(rows),
                        error_code="adapter_schema_error",
                        error_message="result row does not match string fields",
                    )
                values = cast(tuple[str, ...], raw_row)
                rows.append(
                    tuple(
                        sorted(
                            (field, value.strip())
                            for field, value in zip(fields, values, strict=True)
                        )
                    )
                )
        except Exception as exc:  # pragma: no cover - exact SDK failures vary by release
            return _make_audit(
                params=params,
                fields=tuple(fields),
                rows=tuple(rows),
                error_code="client_iteration_exception",
                error_message=type(exc).__name__,
            )
        error_code = _result_text(result.error_code)
        error_message = _result_text(result.error_msg)
    return _make_audit(
        params=params,
        fields=tuple(fields),
        rows=tuple(rows),
        error_code=error_code,
        error_message=error_message,
    )


def _make_audit(
    *,
    params: tuple[tuple[str, str], ...],
    fields: tuple[str, ...],
    rows: tuple[NormalizedRow, ...],
    error_code: str,
    error_message: str,
) -> BaoStockDailyQueryAudit:
    canonical_fields = tuple(sorted(fields))
    canonical_rows = tuple(sorted(rows, key=lambda row: _canonical_json_bytes(dict(row))))
    normalized_response = {
        "errorCode": error_code,
        "errorMessage": error_message,
        "fields": list(canonical_fields),
        "rows": [dict(row) for row in canonical_rows],
    }
    digest = hashlib.sha256(_canonical_json_bytes(normalized_response)).hexdigest()
    return BaoStockDailyQueryAudit(
        method="query_history_k_data_plus",
        request_params=tuple(sorted(params)),
        response_fields=canonical_fields,
        normalized_rows=canonical_rows,
        error_code=error_code,
        error_message=error_message,
        normalized_response_sha256=digest,
    )


def _rows_from_audit(
    audit: BaoStockDailyQueryAudit,
    *,
    interval_start: date,
    interval_end: date,
    price_basis: PriceBasis,
) -> tuple[BaoStockDailyRow, ...]:
    if frozenset(audit.response_fields) != _REQUIRED_FIELDS or len(audit.response_fields) != len(
        DAILY_FIELD_ORDER
    ):
        raise BaoStockDailySourceError(
            "query_history_k_data_plus fields must exactly match " + DAILY_FIELDS
        )
    interval_days = (interval_end - interval_start).days + 1
    if interval_days > MAX_QUERY_CALENDAR_DAYS:
        raise BaoStockDailySourceError("BaoStock daily query exceeds the annual safety bound")
    trade_dates: list[date] = []
    seen_dates: set[date] = set()
    for index, raw_row in enumerate(audit.normalized_rows):
        row = dict(raw_row)
        trade_date = _iso_date(row["date"], field_name="date", index=index)
        if not interval_start <= trade_date <= interval_end:
            raise BaoStockDailySourceError(f"row {index} date is outside its annual query interval")
        if trade_date in seen_dates:
            raise BaoStockDailySourceError("BaoStock daily result contains duplicate trade dates")
        seen_dates.add(trade_date)
        trade_dates.append(trade_date)
    if audit.row_count > interval_days:
        raise BaoStockDailySourceError(
            "BaoStock daily query returned too many rows for its interval"
        )

    parsed: list[BaoStockDailyRow] = []
    for index, (raw_row, trade_date) in enumerate(
        zip(audit.normalized_rows, trade_dates, strict=True)
    ):
        row = dict(raw_row)
        open_cny = _positive_decimal(row["open"], field_name="open", index=index)
        high_cny = _positive_decimal(row["high"], field_name="high", index=index)
        low_cny = _positive_decimal(row["low"], field_name="low", index=index)
        close_cny = _positive_decimal(row["close"], field_name="close", index=index)
        preclose_cny = _positive_decimal(row["preclose"], field_name="preclose", index=index)
        if high_cny < max(open_cny, close_cny, low_cny) or low_cny > min(
            open_cny, close_cny, high_cny
        ):
            raise BaoStockDailySourceError(f"row {index} OHLC values are inconsistent")

        volume = _non_negative_whole_number(row["volume"], field_name="volume", index=index)
        amount = _non_negative_decimal(row["amount"], field_name="amount", index=index)
        turnover_rate_pct = _non_negative_decimal(row["turn"], field_name="turn", index=index)
        tradestatus = row["tradestatus"]
        if tradestatus not in {"0", "1"}:
            raise BaoStockDailySourceError(f"row {index} tradestatus must be BaoStock 0 or 1")
        typed_tradestatus = cast(Literal["0", "1"], tradestatus)
        raw_is_st = row["isST"]
        if raw_is_st not in {"0", "1"}:
            raise BaoStockDailySourceError(f"row {index} isST must be BaoStock 0 or 1")

        if (volume == 0) != (amount == 0):
            raise BaoStockDailySourceError(
                f"row {index} volume and amount must both be zero or both be positive"
            )
        if typed_tradestatus == "0" and (volume != 0 or amount != 0):
            raise BaoStockDailySourceError(
                f"row {index} suspended status cannot carry positive volume or amount"
            )
        if price_basis is PriceBasis.UNADJUSTED and volume > 0:
            share_volume = Decimal(volume)
            if not low_cny * share_volume <= amount <= high_cny * share_volume:
                raise BaoStockDailySourceError(
                    f"row {index} unadjusted volume and amount are inconsistent with OHLC"
                )

        parsed.append(
            BaoStockDailyRow(
                date=trade_date,
                open=open_cny,
                high=high_cny,
                low=low_cny,
                close=close_cny,
                preclose=preclose_cny,
                volume=volume,
                amount=amount,
                turnover_rate_pct=turnover_rate_pct,
                tradestatus=typed_tradestatus,
                is_st=raw_is_st == "1",
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.date))


def _iso_date(value: str, *, field_name: str, index: int) -> date:
    if not value:
        raise BaoStockDailySourceError(f"row {index} {field_name} is missing")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BaoStockDailySourceError(f"row {index} {field_name} is not an ISO date") from exc


def _decimal(value: str, *, field_name: str, index: int) -> Decimal:
    if not value:
        raise BaoStockDailySourceError(f"row {index} {field_name} is missing")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BaoStockDailySourceError(f"row {index} {field_name} is not numeric") from exc
    if not parsed.is_finite():
        raise BaoStockDailySourceError(f"row {index} {field_name} must be finite")
    return parsed


def _positive_decimal(value: str, *, field_name: str, index: int) -> Decimal:
    parsed = _decimal(value, field_name=field_name, index=index)
    if parsed <= 0:
        raise BaoStockDailySourceError(f"row {index} {field_name} must be positive")
    return parsed


def _non_negative_decimal(value: str, *, field_name: str, index: int) -> Decimal:
    parsed = _decimal(value, field_name=field_name, index=index)
    if parsed < 0:
        raise BaoStockDailySourceError(f"row {index} {field_name} must be non-negative")
    return parsed


def _non_negative_whole_number(value: str, *, field_name: str, index: int) -> int:
    parsed = _non_negative_decimal(value, field_name=field_name, index=index)
    if parsed != parsed.to_integral_value():
        raise BaoStockDailySourceError(
            f"row {index} {field_name} must be a non-negative whole number of shares"
        )
    return int(parsed)


def _result_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else "adapter_schema_error"


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
