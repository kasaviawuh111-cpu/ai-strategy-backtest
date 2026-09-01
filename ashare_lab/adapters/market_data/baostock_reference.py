"""Auditable BaoStock reference data for an internal SH/SZ A-share Demo.

This adapter deliberately stops before snapshot publication.  It validates one
ordinary CNY stock, collects annual ``query_dividend_data(...,
yearType="operate")`` responses, and captures bounded annual
``query_history_k_data_plus`` session-reference queries.  Annual history
queries remain below BaoStock's implicit 2,000-row pagination boundary, whose
socket-empty failure mode cannot otherwise be distinguished from success.

Corporate-action coverage remains separate from historical-session coverage:
the former is complete only for explicitly supported action lanes, while the
latter carries daily ``preclose``, ``tradestatus`` and ``isST`` facts for exact
alignment with the Choice security/date axis.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    SecurityMasterAssetType,
    SecurityMasterRecord,
)
from ashare_lab.domain.market_data import (
    Board,
    CorporateAction,
    CorporateActionKind,
    TimeQuality,
)
from ashare_lab.domain.shared import InstrumentId, StrongId

PROVIDER = "BaoStock Python API"
SUPPORTED_CATEGORIES = ("cash_dividend", "share_distribution")
UNSUPPORTED_CATEGORIES = ("rights_issue", "stock_split", "reverse_split")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_ONLY_AVAILABLE_TIME = time(hour=15)
_CANONICAL_RE = re.compile(r"^(\d{6})\.(SH|SZ)$", re.IGNORECASE)
_PROVIDER_RE = re.compile(r"^(sh|sz)\.(\d{6})$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d{6}$")

_BASIC_FIELDS = frozenset({"code", "code_name", "ipoDate", "outDate", "type", "status"})
_DIVIDEND_FIELDS = frozenset(
    {
        "code",
        "dividPlanDate",
        "dividRegistDate",
        "dividOperateDate",
        "dividPayDate",
        "dividStockMarketDate",
        "dividCashPsBeforeTax",
        "dividCashPsAfterTax",
        "dividStocksPs",
        "dividReserveToStockPs",
    }
)
_HISTORY_FIELD_ORDER = ("date", "preclose", "tradestatus", "isST")
_HISTORY_FIELDS = frozenset(_HISTORY_FIELD_ORDER)
_HISTORY_FIELDS_TEXT = ",".join(_HISTORY_FIELD_ORDER)
_HISTORY_FREQUENCY = "d"
_HISTORY_ADJUST_FLAG = "3"
_MAX_HISTORY_CALENDAR_DAYS = 366


class BaoStockResult(Protocol):
    fields: Sequence[object]
    error_code: object
    error_msg: object

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[object]: ...


class BaoStockClient(Protocol):
    def query_stock_basic(self, *, code: str) -> BaoStockResult: ...

    def query_dividend_data(
        self,
        *,
        code: str,
        year: str,
        yearType: str,
    ) -> BaoStockResult: ...

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


class BaoStockListingStatus(StrEnum):
    LISTED = "listed"
    DELISTED = "delisted"


@dataclass(frozen=True, slots=True)
class BaoStockQueryAudit:
    method: str
    params: tuple[tuple[str, str], ...]
    fields: tuple[str, ...]
    rows: tuple[tuple[tuple[str, str], ...], ...]
    error_code: str
    error_message: str
    normalized_response_sha256: str

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def zero_result(self) -> bool:
        return not self.rows

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "params": dict(self.params),
            "fields": list(self.fields),
            "rows": [dict(row) for row in self.rows],
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "rowCount": self.row_count,
            "zeroResult": self.zero_result,
            "normalizedResponseSha256": self.normalized_response_sha256,
        }


class BaoStockReferenceError(RuntimeError):
    """No reference result may be used after a query or validation failure."""

    def __init__(
        self,
        message: str,
        *,
        query_audits: tuple[BaoStockQueryAudit, ...] = (),
    ) -> None:
        super().__init__(message)
        self.query_audits = query_audits


@dataclass(frozen=True, slots=True)
class BaoStockInstrumentReference:
    instrument_id: InstrumentId
    provider_code: str
    name: str
    listing_date: date
    delisting_date: date | None
    status: BaoStockListingStatus
    board: Board
    asset_type: AssetType
    currency: Literal["CNY"] = "CNY"


@dataclass(frozen=True, slots=True)
class BaoStockSecurityMasterResult:
    """One provider-classified identity plus its immutable query evidence."""

    record: SecurityMasterRecord
    query_audit: BaoStockQueryAudit


@dataclass(frozen=True, slots=True)
class BaoStockHistoricalSessionFact:
    session_date: date
    previous_close: Decimal
    trade_status: Literal["0", "1"]
    is_st: bool


@dataclass(frozen=True, slots=True)
class BaoStockReferenceResult:
    instrument: BaoStockInstrumentReference
    corporate_actions: tuple[CorporateAction, ...]
    historical_sessions: tuple[BaoStockHistoricalSessionFact, ...]
    query_audits: tuple[BaoStockQueryAudit, ...]
    coverage: Mapping[str, object]
    historical_session_coverage: Mapping[str, object]


class BaoStockReferenceAdapter:
    """Collect validated reference facts through an injected BaoStock client."""

    def __init__(self, client: BaoStockClient) -> None:
        self._client = client

    def resolve_security_master(self, *, symbol: str) -> BaoStockSecurityMasterResult:
        """Classify one exact symbol without acquiring history or distributions."""

        instrument_id, provider_code = _normalize_symbol_identity(symbol)
        audit = self._query(
            method="query_stock_basic",
            params=(("code", provider_code),),
            request=lambda: self._client.query_stock_basic(code=provider_code),
        )
        _require_query_success(audit, (audit,))
        try:
            record = _security_master_from_basic(
                audit,
                expected_instrument=instrument_id,
                expected_provider_code=provider_code,
            )
        except BaoStockReferenceError as exc:
            raise BaoStockReferenceError(str(exc), query_audits=(audit,)) from exc
        return BaoStockSecurityMasterResult(record=record, query_audit=audit)

    def prepare(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        captured_at: datetime,
    ) -> BaoStockReferenceResult:
        if type(start) is not date or type(end) is not date or start > end:
            raise BaoStockReferenceError("reference period must contain ordered dates")
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise BaoStockReferenceError("captured_at must include an explicit timezone")

        instrument_id, provider_code = _normalize_symbol_identity(symbol)
        audits: list[BaoStockQueryAudit] = []

        basic_audit = self._query(
            method="query_stock_basic",
            params=(("code", provider_code),),
            request=lambda: self._client.query_stock_basic(code=provider_code),
        )
        audits.append(basic_audit)
        _require_query_success(basic_audit, audits)
        try:
            record = _security_master_from_basic(
                basic_audit,
                expected_instrument=instrument_id,
                expected_provider_code=provider_code,
            )
            if record.asset_type is SecurityMasterAssetType.INDEX:
                raise BaoStockReferenceError("security is an index and cannot enter backtest")
            board = _board_for_security(record)
            instrument = _instrument_from_security_master(record, provider_code, board)
        except BaoStockReferenceError as exc:
            raise BaoStockReferenceError(
                str(exc),
                query_audits=tuple(audits),
            ) from exc
        if start < instrument.listing_date:
            raise BaoStockReferenceError(
                "reference period cannot start before the stock listing date",
                query_audits=tuple(audits),
            )

        years = tuple(range(start.year, end.year + 1))
        if instrument.asset_type is AssetType.STOCK:
            canonical_actions, coverage = self._collect_stock_actions(
                instrument=instrument,
                start=start,
                end=end,
                years=years,
                captured_at=captured_at,
                audits=audits,
            )
        else:
            canonical_actions = ()
            coverage = _etf_reference_only_action_coverage(
                instrument=instrument,
                start=start,
                end=end,
                basic_audit=basic_audit,
            )

        historical_sessions: list[BaoStockHistoricalSessionFact] = []
        history_audits: list[BaoStockQueryAudit] = []
        history_intervals: list[tuple[date, date]] = []
        for year in years:
            interval_start = max(start, date(year, 1, 1))
            interval_end = min(end, date(year, 12, 31))
            history_intervals.append((interval_start, interval_end))
            audit = self._query(
                method="query_history_k_data_plus",
                params=(
                    ("code", provider_code),
                    ("fields", _HISTORY_FIELDS_TEXT),
                    ("start_date", interval_start.isoformat()),
                    ("end_date", interval_end.isoformat()),
                    ("frequency", _HISTORY_FREQUENCY),
                    ("adjustflag", _HISTORY_ADJUST_FLAG),
                ),
                request=lambda interval_start=interval_start, interval_end=interval_end: (
                    self._client.query_history_k_data_plus(
                        code=provider_code,
                        fields=_HISTORY_FIELDS_TEXT,
                        start_date=interval_start.isoformat(),
                        end_date=interval_end.isoformat(),
                        frequency=_HISTORY_FREQUENCY,
                        adjustflag=_HISTORY_ADJUST_FLAG,
                    )
                ),
            )
            audits.append(audit)
            history_audits.append(audit)
            _require_query_success(audit, audits)
            try:
                historical_sessions.extend(
                    _historical_sessions_from_audit(
                        audit,
                        interval_start=interval_start,
                        interval_end=interval_end,
                    )
                )
            except BaoStockReferenceError as exc:
                raise BaoStockReferenceError(
                    str(exc),
                    query_audits=tuple(audits),
                ) from exc

        canonical_sessions = _validate_historical_session_identity(
            historical_sessions,
            audits,
        )
        historical_session_coverage = _build_historical_session_coverage(
            instrument=instrument,
            start=start,
            end=end,
            sessions=canonical_sessions,
            intervals=tuple(history_intervals),
            audits=tuple(history_audits),
        )
        query_audits = tuple(audits)
        return BaoStockReferenceResult(
            instrument=instrument,
            corporate_actions=canonical_actions,
            historical_sessions=canonical_sessions,
            query_audits=query_audits,
            coverage=coverage,
            historical_session_coverage=historical_session_coverage,
        )

    def _collect_stock_actions(
        self,
        *,
        instrument: BaoStockInstrumentReference,
        start: date,
        end: date,
        years: tuple[int, ...],
        captured_at: datetime,
        audits: list[BaoStockQueryAudit],
    ) -> tuple[tuple[CorporateAction, ...], Mapping[str, object]]:
        actions: list[CorporateAction] = []
        in_range_source_rows = 0
        for year in years:
            audit = self._query(
                method="query_dividend_data",
                params=(
                    ("code", instrument.provider_code),
                    ("year", str(year)),
                    ("yearType", "operate"),
                ),
                request=lambda year=year: self._client.query_dividend_data(
                    code=instrument.provider_code,
                    year=str(year),
                    yearType="operate",
                ),
            )
            audits.append(audit)
            _require_query_success(audit, audits)
            try:
                _require_fields(audit, _DIVIDEND_FIELDS)
                for raw_row in audit.rows:
                    row_actions, in_range = _actions_from_dividend_row(
                        dict(raw_row),
                        instrument=instrument,
                        requested_year=year,
                        start=start,
                        end=end,
                        captured_at=captured_at,
                        response_sha256=audit.normalized_response_sha256,
                    )
                    in_range_source_rows += int(in_range)
                    actions.extend(row_actions)
            except BaoStockReferenceError as exc:
                raise BaoStockReferenceError(str(exc), query_audits=tuple(audits)) from exc
        canonical = _validate_action_identity(actions, audits)
        return canonical, _build_coverage(
            instrument=instrument,
            start=start,
            end=end,
            years=years,
            actions=canonical,
            in_range_source_rows=in_range_source_rows,
            audits=tuple(audits),
        )

    @staticmethod
    def _query(
        *,
        method: str,
        params: tuple[tuple[str, str], ...],
        request: Callable[[], BaoStockResult],
    ) -> BaoStockQueryAudit:
        try:
            result = request()
            return _capture_result(method=method, params=params, result=result)
        except Exception as exc:  # pragma: no cover - exact SDK failures vary by version
            return _make_audit(
                method=method,
                params=params,
                fields=(),
                rows=(),
                error_code="client_exception",
                error_message=type(exc).__name__,
            )


def to_choice_snapshot_payload(result: BaoStockReferenceResult) -> dict[str, object]:
    """Return JSON-safe input accepted by ``prepare_choice_snapshot.py``.

    The mapping keeps the richer BaoStock query evidence alongside the two
    keys consumed by that script (``actions`` and ``coverage``).  Callers must
    inspect ``coverage.unsupportedCategories`` before deciding whether the
    requested strategy can use this source.
    """

    return {
        "schemaVersion": "baostock.internal-demo-reference.v2",
        "instrument": {
            "instrument_id": result.instrument.instrument_id.value,
            "provider_code": result.instrument.provider_code,
            "name": result.instrument.name,
            "listing_date": result.instrument.listing_date.isoformat(),
            "delisting_date": _optional_iso_date(result.instrument.delisting_date),
            "status": result.instrument.status.value,
            "board": result.instrument.board.value,
            "asset_type": result.instrument.asset_type.value,
            "currency": result.instrument.currency,
        },
        "actions": [_action_to_json(action) for action in result.corporate_actions],
        "coverage": dict(result.coverage),
        "historicalSessions": {
            "rows": [_historical_session_to_json(item) for item in result.historical_sessions],
            "coverage": dict(result.historical_session_coverage),
        },
        "queryAudits": [audit.as_dict() for audit in result.query_audits],
    }


def _normalize_symbol_identity(value: str) -> tuple[InstrumentId, str]:
    raw = value.strip()
    canonical_match = _CANONICAL_RE.fullmatch(raw)
    provider_match = _PROVIDER_RE.fullmatch(raw)
    if canonical_match is not None:
        digits, market = canonical_match.group(1), canonical_match.group(2).upper()
    elif provider_match is not None:
        market, digits = provider_match.group(1).upper(), provider_match.group(2)
    elif _DIGITS_RE.fullmatch(raw) is not None:
        digits = raw
        market = _market_for_digits(digits)
    else:
        raise BaoStockReferenceError("symbol must be a supported six-digit SH/SZ A-share code")
    _validate_candidate_exchange(digits, market)
    return InstrumentId(f"{digits}.{market}"), f"{market.lower()}.{digits}"


def _validate_candidate_exchange(digits: str, market: str) -> None:
    sh_candidate = digits.startswith(("5", "600", "601", "603", "605", "688", "689", "000"))
    sz_candidate = digits.startswith(("1", "000", "001", "002", "003", "300", "301", "399"))
    if (market == "SH" and sh_candidate) or (market == "SZ" and sz_candidate):
        return
    raise BaoStockReferenceError("security code prefix does not match a supported exchange space")


def _market_for_digits(digits: str) -> str:
    if digits.startswith(("5", "600", "601", "603", "605", "688", "689")):
        return "SH"
    if digits.startswith(("1", "000", "001", "002", "003", "300", "301")):
        return "SZ"
    raise BaoStockReferenceError("code is not a supported SH/SZ stock or ETF code")


def _stock_board_for_digits(digits: str, market: str) -> Board:
    if digits.startswith(("5", "1")):
        raise BaoStockReferenceError("BaoStock type=1 stock cannot use an ETF code space")
    inferred_market = _market_for_digits(digits)
    if market != inferred_market:
        raise BaoStockReferenceError("stock code prefix does not match its exchange suffix")
    if digits.startswith(("688", "689")):
        return Board.STAR
    if digits.startswith(("300", "301")):
        return Board.CHINEXT
    return Board.MAIN


def _capture_result(
    *,
    method: str,
    params: tuple[tuple[str, str], ...],
    result: BaoStockResult,
) -> BaoStockQueryAudit:
    error_code = _result_text(result.error_code)
    error_message = _result_text(result.error_msg)
    raw_fields = tuple(result.fields)
    normalized_fields: list[str] = []
    for field in raw_fields:
        if not isinstance(field, str) or not field.strip():
            return _make_audit(
                method=method,
                params=params,
                fields=(),
                rows=(),
                error_code="adapter_schema_error",
                error_message="invalid result fields",
            )
        normalized_fields.append(field.strip())
    fields = tuple(normalized_fields)
    if len(set(fields)) != len(fields):
        return _make_audit(
            method=method,
            params=params,
            fields=fields,
            rows=(),
            error_code="adapter_schema_error",
            error_message="duplicate result fields",
        )

    rows: list[tuple[tuple[str, str], ...]] = []
    if error_code == "0":
        try:
            while result.next():
                raw_row = tuple(result.get_row_data())
                normalized_values: list[str] = []
                for value in raw_row:
                    if not isinstance(value, str):
                        return _make_audit(
                            method=method,
                            params=params,
                            fields=fields,
                            rows=tuple(rows),
                            error_code="adapter_schema_error",
                            error_message="result row does not match string fields",
                        )
                    normalized_values.append(value.strip())
                if len(normalized_values) != len(fields):
                    return _make_audit(
                        method=method,
                        params=params,
                        fields=fields,
                        rows=tuple(rows),
                        error_code="adapter_schema_error",
                        error_message="result row does not match string fields",
                    )
                rows.append(
                    tuple(
                        sorted(
                            (field, value)
                            for field, value in zip(fields, normalized_values, strict=True)
                        )
                    )
                )
        except Exception as exc:  # pragma: no cover - exact SDK failures vary by version
            return _make_audit(
                method=method,
                params=params,
                fields=fields,
                rows=tuple(rows),
                error_code="client_iteration_exception",
                error_message=type(exc).__name__,
            )
        error_code = _result_text(result.error_code)
        error_message = _result_text(result.error_msg)
    return _make_audit(
        method=method,
        params=params,
        fields=fields,
        rows=tuple(rows),
        error_code=error_code,
        error_message=error_message,
    )


def _make_audit(
    *,
    method: str,
    params: tuple[tuple[str, str], ...],
    fields: tuple[str, ...],
    rows: tuple[tuple[tuple[str, str], ...], ...],
    error_code: str,
    error_message: str,
) -> BaoStockQueryAudit:
    canonical_rows = tuple(sorted(rows, key=lambda row: _canonical_json_bytes(dict(row))))
    canonical_fields = tuple(sorted(fields))
    body = {
        "method": method,
        "params": dict(sorted(params)),
        "fields": list(canonical_fields),
        "rows": [dict(row) for row in canonical_rows],
        "errorCode": error_code,
        "errorMessage": error_message,
    }
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return BaoStockQueryAudit(
        method=method,
        params=tuple(sorted(params)),
        fields=canonical_fields,
        rows=canonical_rows,
        error_code=error_code,
        error_message=error_message,
        normalized_response_sha256=digest,
    )


def _result_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else "adapter_schema_error"


def _require_query_success(
    audit: BaoStockQueryAudit,
    audits: Sequence[BaoStockQueryAudit],
) -> None:
    if audit.error_code != "0":
        raise BaoStockReferenceError(
            f"{audit.method} failed with provider code {audit.error_code}",
            query_audits=tuple(audits),
        )


def _require_fields(audit: BaoStockQueryAudit, required: frozenset[str]) -> None:
    missing = sorted(required.difference(audit.fields))
    if missing:
        raise BaoStockReferenceError(
            f"{audit.method} is missing required fields: {', '.join(missing)}"
        )


def _security_master_from_basic(
    audit: BaoStockQueryAudit,
    *,
    expected_instrument: InstrumentId,
    expected_provider_code: str,
) -> SecurityMasterRecord:
    _require_fields(audit, _BASIC_FIELDS)
    if audit.row_count != 1:
        raise BaoStockReferenceError("query_stock_basic must return exactly one security")
    row = dict(audit.rows[0])
    if row["code"].casefold() != expected_provider_code:
        raise BaoStockReferenceError("query_stock_basic returned a different security")
    asset_type = {
        "1": SecurityMasterAssetType.STOCK,
        "2": SecurityMasterAssetType.INDEX,
        "5": SecurityMasterAssetType.ETF,
    }.get(row["type"])
    if asset_type is None:
        raise BaoStockReferenceError("BaoStock security type is not STOCK, ETF, or INDEX")
    name = row["code_name"].strip()
    if not name:
        raise BaoStockReferenceError("security name cannot be blank")
    listing_date = _required_date(row["ipoDate"], "ipoDate")
    delisting_date = _optional_date(row["outDate"], "outDate")
    raw_status = row["status"]
    if raw_status == "1":
        status = BaoStockListingStatus.LISTED
        if delisting_date is not None:
            raise BaoStockReferenceError("listed security cannot carry a delisting date")
    elif raw_status == "0":
        status = BaoStockListingStatus.DELISTED
        if delisting_date is None:
            raise BaoStockReferenceError("delisted security requires outDate")
    else:
        raise BaoStockReferenceError("security status must be BaoStock 0 or 1")
    if delisting_date is not None and delisting_date < listing_date:
        raise BaoStockReferenceError("outDate cannot precede ipoDate")
    market = str(expected_instrument).rsplit(".", maxsplit=1)[1]
    return SecurityMasterRecord(
        symbol=str(expected_instrument),
        name=name,
        exchange=Exchange(market),
        asset_type=asset_type,
        currency="CNY",
        listing_date=listing_date,
        delisting_date=delisting_date,
        tradable=status is BaoStockListingStatus.LISTED,
        data_source=(
            "BaoStock query_stock_basic normalized sha256:" + audit.normalized_response_sha256
        ),
    )


def _board_for_security(record: SecurityMasterRecord) -> Board:
    digits, market = record.symbol.split(".", maxsplit=1)
    if record.asset_type is SecurityMasterAssetType.ETF:
        if not (
            (market == "SH" and digits.startswith("5"))
            or (market == "SZ" and digits.startswith("1"))
        ):
            raise BaoStockReferenceError("BaoStock type=5 ETF code space is inconsistent")
        return Board.STOCK_ETF
    if record.asset_type is not SecurityMasterAssetType.STOCK:
        raise BaoStockReferenceError("security is not an executable STOCK or ETF")
    return _stock_board_for_digits(digits, market)


def _instrument_from_security_master(
    record: SecurityMasterRecord,
    provider_code: str,
    board: Board,
) -> BaoStockInstrumentReference:
    asset_type = {
        SecurityMasterAssetType.STOCK: AssetType.STOCK,
        SecurityMasterAssetType.ETF: AssetType.ETF,
    }.get(record.asset_type)
    if asset_type is None:
        raise BaoStockReferenceError("security is not an executable STOCK or ETF")
    return BaoStockInstrumentReference(
        instrument_id=InstrumentId(record.symbol),
        provider_code=provider_code,
        name=record.name,
        listing_date=record.listing_date,
        delisting_date=record.delisting_date,
        status=(
            BaoStockListingStatus.LISTED if record.tradable else BaoStockListingStatus.DELISTED
        ),
        board=board,
        asset_type=asset_type,
    )


def _historical_sessions_from_audit(
    audit: BaoStockQueryAudit,
    *,
    interval_start: date,
    interval_end: date,
) -> tuple[BaoStockHistoricalSessionFact, ...]:
    if frozenset(audit.fields) != _HISTORY_FIELDS or len(audit.fields) != len(_HISTORY_FIELDS):
        raise BaoStockReferenceError(
            "query_history_k_data_plus fields must exactly match " + _HISTORY_FIELDS_TEXT
        )
    if (interval_end - interval_start).days + 1 > _MAX_HISTORY_CALENDAR_DAYS:
        raise BaoStockReferenceError("historical session query exceeds the annual safety bound")
    if audit.row_count > _MAX_HISTORY_CALENDAR_DAYS:
        raise BaoStockReferenceError("historical session query returned too many daily rows")

    sessions: list[BaoStockHistoricalSessionFact] = []
    seen_dates: set[date] = set()
    for raw_row in audit.rows:
        row = dict(raw_row)
        session_date = _required_date(row["date"], "historical session date")
        if not interval_start <= session_date <= interval_end:
            raise BaoStockReferenceError("historical session date is outside its query interval")
        if session_date in seen_dates:
            raise BaoStockReferenceError("query_history_k_data_plus returned duplicate dates")
        seen_dates.add(session_date)
        previous_close = _positive_decimal(row["preclose"], "historical preclose")
        trade_status = row["tradestatus"]
        if trade_status not in {"0", "1"}:
            raise BaoStockReferenceError("historical tradestatus must be BaoStock 0 or 1")
        typed_trade_status = cast(Literal["0", "1"], trade_status)
        raw_is_st = row["isST"]
        if raw_is_st not in {"0", "1"}:
            raise BaoStockReferenceError("historical isST must be BaoStock 0 or 1")
        sessions.append(
            BaoStockHistoricalSessionFact(
                session_date=session_date,
                previous_close=previous_close,
                trade_status=typed_trade_status,
                is_st=raw_is_st == "1",
            )
        )
    return tuple(sorted(sessions, key=lambda item: item.session_date))


def _validate_historical_session_identity(
    sessions: Sequence[BaoStockHistoricalSessionFact],
    audits: Sequence[BaoStockQueryAudit],
) -> tuple[BaoStockHistoricalSessionFact, ...]:
    ordered = tuple(sorted(sessions, key=lambda item: item.session_date))
    dates = tuple(item.session_date for item in ordered)
    if len(dates) != len(set(dates)):
        raise BaoStockReferenceError(
            "historical session intervals returned duplicate dates",
            query_audits=tuple(audits),
        )
    return ordered


def _build_historical_session_coverage(
    *,
    instrument: BaoStockInstrumentReference,
    start: date,
    end: date,
    sessions: tuple[BaoStockHistoricalSessionFact, ...],
    intervals: tuple[tuple[date, date], ...],
    audits: tuple[BaoStockQueryAudit, ...],
) -> dict[str, object]:
    if len(intervals) != len(audits):
        raise BaoStockReferenceError("historical query intervals and audits must align")
    audit_payload = [audit.as_dict() for audit in audits]
    aggregate_digest = hashlib.sha256(_canonical_json_bytes(audit_payload)).hexdigest()
    row_payload = [_historical_session_to_json(item) for item in sessions]
    row_digest = hashlib.sha256(_canonical_json_bytes(row_payload)).hexdigest()
    interval_payload: list[dict[str, object]] = []
    for (interval_start, interval_end), audit in zip(intervals, audits, strict=True):
        interval_payload.append(
            {
                "start": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "rowCount": audit.row_count,
                "zeroResult": audit.zero_result,
                "normalizedResponseSha256": audit.normalized_response_sha256,
            }
        )
    return {
        "status": "complete",
        "querySucceeded": True,
        "provider": PROVIDER,
        "instrumentId": instrument.instrument_id.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fields": list(_HISTORY_FIELD_ORDER),
        "frequency": _HISTORY_FREQUENCY,
        "adjustFlag": _HISTORY_ADJUST_FLAG,
        "priceBasis": "unadjusted",
        "rowCount": len(sessions),
        "zeroResult": not sessions,
        "returnedStart": sessions[0].session_date.isoformat() if sessions else None,
        "returnedEnd": sessions[-1].session_date.isoformat() if sessions else None,
        "normalizedResponseSha256": aggregate_digest,
        "canonicalRowsSha256": row_digest,
        "hashSemantics": "sha256_of_canonical_normalized_results_not_raw_wire_bytes",
        "paginationPolicy": "annual_queries_bounded_to_at_most_366_calendar_days",
        "intervals": interval_payload,
        "queryAudits": audit_payload,
    }


def _actions_from_dividend_row(
    row: Mapping[str, str],
    *,
    instrument: BaoStockInstrumentReference,
    requested_year: int,
    start: date,
    end: date,
    captured_at: datetime,
    response_sha256: str,
) -> tuple[tuple[CorporateAction, ...], bool]:
    if row["code"].casefold() != instrument.provider_code:
        raise BaoStockReferenceError("query_dividend_data returned a different security")
    ex_date = _required_date(row["dividOperateDate"], "dividOperateDate")
    if ex_date.year != requested_year:
        raise BaoStockReferenceError("operate-year query returned an ex-date in another year")
    if not start <= ex_date <= end:
        return (), False

    cash_before_tax = _nonnegative_decimal(
        row["dividCashPsBeforeTax"],
        "dividCashPsBeforeTax",
    )
    # BaoStock sometimes returns multiple holding-period tax outcomes in this
    # display field (for example comma-separated values).  The engine books the
    # explicit before-tax amount under its versioned gross-dividend policy, so
    # after-tax text is evidence only and must not be misparsed as one number.
    cash_after_tax_reported = row["dividCashPsAfterTax"].strip() not in {
        "",
        "0",
        "0.0",
        "0.00",
    }
    stock_per_share = _nonnegative_decimal(row["dividStocksPs"], "dividStocksPs")
    reserve_per_share = _nonnegative_decimal(
        row["dividReserveToStockPs"],
        "dividReserveToStockPs",
    )
    cash_value = cash_before_tax or Decimal(0)
    share_increment = (stock_per_share or Decimal(0)) + (reserve_per_share or Decimal(0))
    if cash_value == 0 and cash_after_tax_reported:
        raise BaoStockReferenceError("cash dividend lacks the required before-tax amount")
    if cash_value == 0 and share_increment == 0:
        return (), True

    implementation_date = _required_date(row["dividPlanDate"], "dividPlanDate")
    record_date = _required_date(row["dividRegistDate"], "dividRegistDate")
    if implementation_date > record_date:
        raise BaoStockReferenceError("implementation announcement date is after record date")
    if record_date >= ex_date:
        raise BaoStockReferenceError("record date must precede ex-date")
    released_at = datetime.combine(
        implementation_date,
        _DATE_ONLY_AVAILABLE_TIME,
        tzinfo=_SHANGHAI,
    )
    if captured_at < released_at:
        raise BaoStockReferenceError("captured_at cannot precede the implementation announcement")

    row_digest = hashlib.sha256(_canonical_json_bytes(dict(sorted(row.items())))).hexdigest()
    source_action_id = (
        f"baostock:{instrument.instrument_id.value.split('.', maxsplit=1)[0]}:"
        f"{ex_date.strftime('%Y%m%d')}:{row_digest[:16]}"
    )
    source_url = (
        f"baostock://query_dividend_data/{instrument.provider_code}/{requested_year}/operate"
    )
    common: dict[str, object] = {
        "source_action_id": source_action_id,
        "instrument_id": instrument.instrument_id,
        "record_date": record_date,
        "ex_date": ex_date,
        "source_released_at": released_at,
        "vendor_first_available_at": None,
        "ingested_at": captured_at,
        "replay_available_at": released_at,
        "revision_no": 0,
        "time_quality": TimeQuality.DATE_ONLY_CONSERVATIVE,
        "provider": PROVIDER,
        "source_url": source_url,
        "raw_response_sha256": response_sha256,
        "validation_status": "validated",
        "currency": "CNY",
    }
    actions: list[CorporateAction] = []
    if cash_value > 0:
        pay_date = _required_date(row["dividPayDate"], "dividPayDate")
        if pay_date < ex_date:
            raise BaoStockReferenceError("cash pay date cannot precede ex-date")
        actions.append(
            CorporateAction(
                action_id=StrongId(f"ca:{source_action_id}:cash"),
                action_type=CorporateActionKind.CASH_DIVIDEND,
                gross_cash_per_share=cash_value,
                cash_pay_date=pay_date,
                **common,  # type: ignore[arg-type]
            )
        )
    if share_increment > 0:
        market_date = _required_date(
            row["dividStockMarketDate"],
            "dividStockMarketDate",
        )
        if market_date < ex_date:
            raise BaoStockReferenceError("share settlement date cannot precede ex-date")
        actions.append(
            CorporateAction(
                action_id=StrongId(f"ca:{source_action_id}:shares"),
                action_type=CorporateActionKind.SHARE_DISTRIBUTION,
                share_multiplier=Decimal(1) + share_increment,
                # BaoStock exposes only the red-share market date.  Using it for
                # both clocks is conservative: shares are not credited or sold
                # before the one settlement date the provider actually proves.
                share_credit_date=market_date,
                share_sellable_date=market_date,
                **common,  # type: ignore[arg-type]
            )
        )
    return tuple(actions), True


def _validate_action_identity(
    actions: Sequence[CorporateAction],
    audits: Sequence[BaoStockQueryAudit],
) -> tuple[CorporateAction, ...]:
    identities: set[tuple[str, CorporateActionKind]] = set()
    for action in actions:
        key = (action.source_action_id, action.action_type)
        if key in identities:
            raise BaoStockReferenceError(
                "duplicate BaoStock corporate-action leg",
                query_audits=tuple(audits),
            )
        identities.add(key)
    return tuple(
        sorted(
            actions,
            key=lambda item: (item.ex_date, item.source_action_id, item.action_type.value),
        )
    )


def _build_coverage(
    *,
    instrument: BaoStockInstrumentReference,
    start: date,
    end: date,
    years: tuple[int, ...],
    actions: tuple[CorporateAction, ...],
    in_range_source_rows: int,
    audits: tuple[BaoStockQueryAudit, ...],
) -> dict[str, object]:
    audit_payload = [audit.as_dict() for audit in audits]
    aggregate_digest = hashlib.sha256(_canonical_json_bytes(audit_payload)).hexdigest()
    dividend_audits = tuple(audit for audit in audits if audit.method == "query_dividend_data")
    return {
        "status": "complete",
        "querySucceeded": True,
        "provider": PROVIDER,
        "instrumentId": instrument.instrument_id.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "requestedYears": list(years),
        "yearType": "operate",
        "rowCount": len(actions),
        "sourceRowCount": sum(audit.row_count for audit in dividend_audits),
        "inRangeSourceRowCount": in_range_source_rows,
        "zeroResult": not actions,
        "rawResponseSha256": aggregate_digest,
        "normalizedResponseSha256": aggregate_digest,
        "hashSemantics": "sha256_of_canonical_normalized_results_not_raw_wire_bytes",
        "queryAudits": audit_payload,
        "supportedCategories": list(SUPPORTED_CATEGORIES),
        "unsupportedCategories": list(UNSUPPORTED_CATEGORIES),
        "coverageScope": "complete_for_supported_categories_only",
        "timeQuality": TimeQuality.DATE_ONLY_CONSERVATIVE.value,
        "dateAvailabilityPolicy": "dividPlanDate@15:00:00 Asia/Shanghai",
        "settlementPolicy": (
            "cash requires dividPayDate; share distribution requires "
            "dividStockMarketDate, conservatively used as credit and sellable date"
        ),
        "revisionSemantics": "latest_observed_row_without_historical_revision_stream",
        "normalization": "trimmed strings, sorted fields and rows, canonical JSON SHA-256",
    }


def _etf_reference_only_action_coverage(
    *,
    instrument: BaoStockInstrumentReference,
    start: date,
    end: date,
    basic_audit: BaoStockQueryAudit,
) -> dict[str, object]:
    """Disclose that BaoStock identity/session proof is not ETF action proof."""

    return {
        "status": "reference_only",
        "querySucceeded": True,
        "provider": PROVIDER,
        "instrumentId": instrument.instrument_id.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rowCount": 0,
        "zeroResult": True,
        "rawResponseSha256": basic_audit.normalized_response_sha256,
        "normalizedResponseSha256": basic_audit.normalized_response_sha256,
        "hashSemantics": "sha256_of_canonical_normalized_query_stock_basic",
        "queryAudits": [basic_audit.as_dict()],
        "supportedCategories": [],
        "unsupportedCategories": [kind.value for kind in CorporateActionKind],
        "coverageScope": "identity_and_sessions_only_not_etf_corporate_actions",
        "timeQuality": None,
        "dateAvailabilityPolicy": None,
        "settlementPolicy": None,
        "revisionSemantics": None,
        "normalization": "trimmed strings, sorted fields and rows, canonical JSON SHA-256",
    }


def _action_to_json(action: CorporateAction) -> dict[str, object]:
    return {
        "action_id": action.action_id.value,
        "source_action_id": action.source_action_id,
        "action_type": action.action_type.value,
        "record_date": action.record_date.isoformat(),
        "ex_date": action.ex_date.isoformat(),
        "source_released_at": _optional_iso_datetime(action.source_released_at),
        "vendor_first_available_at": _optional_iso_datetime(action.vendor_first_available_at),
        "ingested_at": action.ingested_at.isoformat(),
        "replay_available_at": action.replay_available_at.isoformat(),
        "revision_no": action.revision_no,
        "time_quality": action.time_quality.value,
        "provider": action.provider,
        "source_url": action.source_url,
        "raw_response_sha256": action.raw_response_sha256.removeprefix("sha256:"),
        "validation_status": action.validation_status,
        "currency": action.currency,
        "gross_cash_per_share": _optional_decimal_text(action.gross_cash_per_share),
        "cash_pay_date": _optional_iso_date(action.cash_pay_date),
        "share_multiplier": _optional_decimal_text(action.share_multiplier),
        "share_credit_date": _optional_iso_date(action.share_credit_date),
        "share_sellable_date": _optional_iso_date(action.share_sellable_date),
        "rights_ratio": _optional_decimal_text(action.rights_ratio),
        "rights_subscription_price": _optional_decimal_text(action.rights_subscription_price),
        "rights_payment_deadline": _optional_iso_date(action.rights_payment_deadline),
        "rights_listing_date": _optional_iso_date(action.rights_listing_date),
    }


def _historical_session_to_json(
    item: BaoStockHistoricalSessionFact,
) -> dict[str, object]:
    return {
        "date": item.session_date.isoformat(),
        "preclose": str(item.previous_close),
        "tradestatus": item.trade_status,
        "isST": "1" if item.is_st else "0",
    }


def _optional_iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _required_date(value: str, field_name: str) -> date:
    parsed = _optional_date(value, field_name)
    if parsed is None:
        raise BaoStockReferenceError(f"{field_name} is required")
    return parsed


def _optional_date(value: str, field_name: str) -> date | None:
    raw = value.strip()
    if raw in {"", "0", "0000-00-00"}:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BaoStockReferenceError(f"{field_name} must be an ISO date") from exc


def _nonnegative_decimal(value: str, field_name: str) -> Decimal | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise BaoStockReferenceError(f"{field_name} must be a Decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise BaoStockReferenceError(f"{field_name} must be finite and non-negative")
    return parsed


def _positive_decimal(value: str, field_name: str) -> Decimal:
    parsed = _nonnegative_decimal(value, field_name)
    if parsed is None or parsed <= 0:
        raise BaoStockReferenceError(f"{field_name} must be positive")
    return parsed


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
