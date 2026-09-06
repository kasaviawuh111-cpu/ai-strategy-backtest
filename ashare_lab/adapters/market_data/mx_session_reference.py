"""Provider-neutral historical session reference built from Eastmoney MX.

The daily engine needs exact per-instrument ``preclose``, trading-status and
ST facts before it can derive historical price limits.  This adapter keeps
those facts separate from the daily OHLCV request, binds every response to an
exact canonical A-share symbol, and preserves both provider wire hashes and
canonical normalized hashes in a versioned artifact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from ashare_lab.domain.market_data import (
    AshareInstrumentCodeError,
    Board,
    normalize_a_share_instrument,
)
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataResult

from .mx_saas import MxSaasMarketDataClient

REFERENCE_SCHEMA_VERSION = "ashare-lab.instrument-session-reference.v3"
REFERENCE_PROVIDER = "eastmoney_mx_finance_data"
REFERENCE_FIELDS = ("date", "preclose", "tradestatus", "isST")
REFERENCE_FREQUENCY = "1d"
REFERENCE_PRICE_BASIS = "unadjusted"
REFERENCE_ADJUSTMENT = "provider_unadjusted_preclose"
REFERENCE_PAGINATION_POLICY = "annual_searchData_queries_bounded_to_at_most_366_calendar_days"
REFERENCE_HASH_SEMANTICS = (
    "provider_raw_wire_sha256_plus_canonical_normalized_rows_sha256"
)

_REQUIRED_PROVIDER_FIELDS = ("前收盘价", "交易状态", "是否为ST股票")
_TRADING_STATUS = {"正常交易": "1", "复牌": "1", "连续停牌": "0"}
_ST_STATUS = {"否": "0", "是": "1"}
_EXCHANGE_NAMES = {"SH": "上海证券交易所", "SZ": "深圳证券交易所"}


class MxSessionReferenceError(RuntimeError):
    """MX data was unavailable or could not satisfy the strict reference contract."""


class MxSessionReferenceAdapter:
    """Acquire an exact A-share identity and annual historical session facts."""

    def __init__(self, client: MxSaasMarketDataClient) -> None:
        self._client = client

    async def prepare(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        captured_at: datetime,
    ) -> dict[str, object]:
        canonical = _canonical_stock_symbol(symbol)
        if type(start) is not date or type(end) is not date or start > end:
            raise MxSessionReferenceError("reference period must contain ordered dates")
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise MxSessionReferenceError("captured_at must include an explicit timezone")

        identity_query = (
            f"证券代码等于{canonical}；获取证券代码、证券简称、是否上市、"
            "证券类型、交易市场"
        )
        identity_response = await self._client.screen(
            query=identity_query,
            asset_type="A股",
        )
        identity = _identity_from_screen(identity_response, canonical)

        listing_query = f"查询A股{canonical}的首发上市日、股票简称、是否上市"
        listing_response = await self._client.query_finance(
            query=listing_query,
            indicators=None,
        )
        listing_date, listing_name = _listing_identity(listing_response, canonical)
        if listing_name != identity["name"]:
            raise MxSessionReferenceError("MX identity responses disagree on the security name")
        if start < listing_date:
            raise MxSessionReferenceError("reference period cannot start before listing date")

        rows: list[dict[str, str]] = []
        session_audits: list[dict[str, object]] = []
        raw_session_tables: list[dict[str, object]] = []
        for interval_start, interval_end in _annual_intervals(start, end):
            query = (
                f"查询{canonical} {interval_start.isoformat()}至{interval_end.isoformat()}"
                "每个交易日的前收盘价、交易状态、是否ST、证券简称"
            )
            response = await self._client.query_finance(query=query, indicators=None)
            interval_rows, selected_tables = _session_rows(response, canonical)
            if not interval_rows:
                raise MxSessionReferenceError("MX returned no historical session rows")
            if not all(
                interval_start <= date.fromisoformat(row["date"]) <= interval_end
                for row in interval_rows
            ):
                raise MxSessionReferenceError("MX returned session dates outside the request")
            rows.extend(interval_rows)
            session_audits.append(
                _session_audit(
                    response,
                    query=query,
                    start=interval_start,
                    end=interval_end,
                    rows=interval_rows,
                )
            )
            raw_session_tables.append(
                {
                    "requestedStart": interval_start.isoformat(),
                    "requestedEnd": interval_end.isoformat(),
                    "tables": [dict(table) for table in selected_tables],
                }
            )

        rows.sort(key=lambda row: row["date"])
        dates = [row["date"] for row in rows]
        if len(dates) != len(set(dates)):
            raise MxSessionReferenceError("MX returned duplicate historical session dates")
        if not rows:
            raise MxSessionReferenceError("MX returned no historical session rows")

        identity_audits = [
            _screen_audit(identity_response, purpose="security_master"),
            _finance_audit(
                listing_response,
                purpose="listing_identity",
                provider_fields=("首发上市日", "股票简称", "是否上市"),
            ),
        ]
        canonical_rows_sha256 = _canonical_sha256(rows)
        coverage = {
            "schemaVersion": REFERENCE_SCHEMA_VERSION,
            "status": "complete",
            "querySucceeded": True,
            "provider": REFERENCE_PROVIDER,
            "instrumentId": canonical,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fields": list(REFERENCE_FIELDS),
            "providerFields": list(_REQUIRED_PROVIDER_FIELDS),
            "frequency": REFERENCE_FREQUENCY,
            "adjustFlag": REFERENCE_ADJUSTMENT,
            "priceBasis": REFERENCE_PRICE_BASIS,
            "rowCount": len(rows),
            "zeroResult": False,
            "returnedStart": rows[0]["date"],
            "returnedEnd": rows[-1]["date"],
            "canonicalRowsSha256": canonical_rows_sha256,
            "dateAxisSha256": _canonical_sha256(dates),
            "aggregateAuditSha256": _canonical_sha256(session_audits),
            "hashSemantics": REFERENCE_HASH_SEMANTICS,
            "paginationPolicy": REFERENCE_PAGINATION_POLICY,
            "queryMethod": "Eastmoney MX searchData annual exact-symbol daily facts",
            "queryAudits": session_audits,
        }
        board = _board_for_stock(canonical)
        return {
            "schemaVersion": REFERENCE_SCHEMA_VERSION,
            "instrument": {
                "instrument_id": canonical,
                "provider_code": canonical,
                "name": identity["name"],
                "listing_date": listing_date.isoformat(),
                "delisting_date": None,
                "status": "listed",
                "board": board.value,
                "boardSource": "canonical A-share exchange code-space rule v1",
                "asset_type": "STOCK",
                "currency": "CNY",
            },
            "historicalSessions": {"rows": rows, "coverage": coverage},
            "queryAudits": [*identity_audits, *session_audits],
            "rawProviderResponses": {
                "securityMasterRows": [dict(row) for row in identity_response.rows],
                "listingTables": [dict(table) for table in listing_response.tables],
                "historicalSessionTables": raw_session_tables,
            },
            "capturedAt": captured_at.astimezone(UTC).isoformat(),
        }


def _canonical_stock_symbol(symbol: str) -> str:
    try:
        canonical = str(normalize_a_share_instrument(symbol))
    except AshareInstrumentCodeError as exc:
        raise MxSessionReferenceError("symbol must be a canonical A-share code") from exc
    if canonical.endswith(".BJ"):
        raise MxSessionReferenceError("MX session reference currently covers SH/SZ stocks only")
    digits, market = canonical.split(".", maxsplit=1)
    if digits.startswith(("5", "1")):
        raise MxSessionReferenceError("MX session reference currently excludes stock ETFs")
    if market not in _EXCHANGE_NAMES:
        raise MxSessionReferenceError("MX session reference currently covers SH/SZ stocks only")
    return canonical


def _identity_from_screen(
    response: LiveMarketDataResult,
    symbol: str,
) -> dict[str, str]:
    if response.provider != "eastmoney_mx_screener" or response.asset_type != "A股":
        raise MxSessionReferenceError("MX security-master response has the wrong provider scope")
    if len(response.rows) != 1:
        raise MxSessionReferenceError("MX security-master query must return exactly one A-share")
    row = response.rows[0]
    digits, market = symbol.split(".", maxsplit=1)
    raw_code = _one_text(row, ("证券代码", "股票代码", "代码"), "security code")
    if raw_code.upper() not in {digits, symbol}:
        raise MxSessionReferenceError("MX security-master response belongs to another security")
    name = _one_text(row, ("证券简称", "股票简称", "名称"), "security name")
    if _one_text(row, ("证券类型",), "security type") != "A股":
        raise MxSessionReferenceError("MX security-master response is not an A-share stock")
    if _one_text(row, ("上市状态",), "listing status") != "正常上市":
        raise MxSessionReferenceError("MX security-master response is not currently listed")
    if _one_text(row, ("市场类型",), "exchange") != _EXCHANGE_NAMES[market]:
        raise MxSessionReferenceError("MX security-master exchange conflicts with the symbol")
    return {"name": name}


def _listing_identity(response: LiveFinanceDataResult, symbol: str) -> tuple[date, str]:
    candidates: list[Mapping[str, Any]] = []
    for table in response.tables:
        if not _table_belongs_to(table, symbol):
            continue
        fields = _table_fields(table)
        if {"首发上市日", "股票简称", "是否上市"}.issubset(fields):
            candidates.append(table)
    if len(candidates) != 1:
        raise MxSessionReferenceError(
            "MX listing query must return one exact security metadata table"
        )
    fields = _table_fields(candidates[0])
    values = {
        name: _one_provider_value(fields[name], name)
        for name in ("首发上市日", "股票简称", "是否上市")
    }
    if values["是否上市"] != "是":
        raise MxSessionReferenceError("MX listing identity is not currently listed")
    try:
        listing_date = date.fromisoformat(values["首发上市日"])
    except ValueError as exc:
        raise MxSessionReferenceError("MX listing date must be an ISO date") from exc
    return listing_date, values["股票简称"]


def _session_rows(
    response: LiveFinanceDataResult,
    symbol: str,
) -> tuple[list[dict[str, str]], tuple[Mapping[str, Any], ...]]:
    if response.provider != REFERENCE_PROVIDER:
        raise MxSessionReferenceError("MX session response has the wrong provider")
    grouped: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for table in response.tables:
        if not _table_belongs_to(table, symbol):
            continue
        raw: object = table.get("rawTable")
        if not isinstance(raw, Mapping):
            continue
        typed_raw = cast(Mapping[str, object], raw)
        dates: object = typed_raw.get("headName")
        if not isinstance(dates, list) or not dates:
            continue
        axis = tuple(str(value).strip() for value in cast(list[object], dates))
        try:
            parsed = tuple(date.fromisoformat(value) for value in axis)
        except ValueError:
            continue
        if len(parsed) != len(set(parsed)):
            raise MxSessionReferenceError("MX session response contains duplicate dates")
        grouped.setdefault(axis, []).append(table)
    if not grouped:
        raise MxSessionReferenceError("MX session response omitted an exact historical table")
    longest = max(len(axis) for axis in grouped)
    winners = [(axis, tables) for axis, tables in grouped.items() if len(axis) == longest]
    if len(winners) != 1:
        raise MxSessionReferenceError("MX session response has ambiguous historical date axes")
    axis, tables = winners[0]
    fields: dict[str, list[object]] = {}
    for table in tables:
        for name, values in _table_fields(table).items():
            if len(values) != len(axis):
                raise MxSessionReferenceError("MX session field count does not match its dates")
            existing = fields.get(name)
            if existing is not None and existing != values:
                raise MxSessionReferenceError(
                    "MX session response has conflicting duplicate fields"
                )
            fields[name] = values
    missing = sorted(set(_REQUIRED_PROVIDER_FIELDS) - set(fields))
    if missing:
        raise MxSessionReferenceError("MX session response omitted fields: " + ", ".join(missing))

    rows: list[dict[str, str]] = []
    for index, raw_date in enumerate(axis):
        raw_status = _clean_value(fields["交易状态"][index], "交易状态")
        raw_st = _clean_value(fields["是否为ST股票"][index], "是否为ST股票")
        if raw_status not in _TRADING_STATUS:
            raise MxSessionReferenceError(f"unsupported MX trading status: {raw_status}")
        if raw_st not in _ST_STATUS:
            raise MxSessionReferenceError(f"unsupported MX ST status: {raw_st}")
        rows.append(
            {
                "date": date.fromisoformat(raw_date).isoformat(),
                "preclose": _positive_decimal_text(fields["前收盘价"][index]),
                "tradestatus": _TRADING_STATUS[raw_status],
                "isST": _ST_STATUS[raw_st],
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows, tuple(tables)


def _session_audit(
    response: LiveFinanceDataResult,
    *,
    query: str,
    start: date,
    end: date,
    rows: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    return {
        "purpose": "historical_sessions",
        "query": query,
        "provider": response.provider,
        "schemaVersion": response.provenance.schema_version,
        "responseSha256": response.provenance.response_sha256,
        "retrievedAt": response.provenance.retrieved_at.astimezone(UTC).isoformat(),
        "requestedStart": start.isoformat(),
        "requestedEnd": end.isoformat(),
        "returnedStart": rows[0]["date"],
        "returnedEnd": rows[-1]["date"],
        "rowCount": len(rows),
        "providerFields": list(_REQUIRED_PROVIDER_FIELDS),
        "canonicalRowsSha256": _canonical_sha256(list(rows)),
        "dateAxisSha256": _canonical_sha256([row["date"] for row in rows]),
    }


def _screen_audit(response: LiveMarketDataResult, *, purpose: str) -> dict[str, object]:
    return {
        "purpose": purpose,
        "query": response.query,
        "assetType": response.asset_type,
        "provider": response.provider,
        "schemaVersion": response.provenance.schema_version,
        "responseSha256": response.provenance.response_sha256,
        "retrievedAt": response.provenance.retrieved_at.astimezone(UTC).isoformat(),
        "rowCount": len(response.rows),
        "providerFields": list(response.columns),
    }


def _finance_audit(
    response: LiveFinanceDataResult,
    *,
    purpose: str,
    provider_fields: Sequence[str],
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "query": response.query,
        "provider": response.provider,
        "schemaVersion": response.provenance.schema_version,
        "responseSha256": response.provenance.response_sha256,
        "retrievedAt": response.provenance.retrieved_at.astimezone(UTC).isoformat(),
        "providerFields": list(provider_fields),
    }


def _annual_intervals(start: date, end: date) -> tuple[tuple[date, date], ...]:
    return tuple(
        (max(start, date(year, 1, 1)), min(end, date(year, 12, 31)))
        for year in range(start.year, end.year + 1)
    )


def _table_belongs_to(table: Mapping[str, Any], symbol: str) -> bool:
    code = table.get("code")
    codes = table.get("entityCodes")
    return (isinstance(code, str) and code.strip().upper() == symbol) or (
        isinstance(codes, list)
        and symbol in {str(value).strip().upper() for value in cast(list[Any], codes)}
    )


def _table_fields(table: Mapping[str, Any]) -> dict[str, list[object]]:
    raw = table.get("rawTable")
    names = table.get("nameMap")
    if not isinstance(raw, Mapping) or not isinstance(names, Mapping):
        return {}
    raw = cast(Mapping[str, object], raw)
    output: dict[str, list[object]] = {}
    for code, raw_name in cast(Mapping[object, object], names).items():
        if not isinstance(code, str) or code == "headNameSub" or code not in raw:
            continue
        values = raw[code]
        name = str(raw_name).strip()
        if not name or not isinstance(values, list):
            continue
        if name in output and output[name] != values:
            raise MxSessionReferenceError("MX response contains conflicting duplicate labels")
        output[name] = list(cast(list[object], values))
    return output


def _one_text(row: Mapping[str, Any], aliases: Sequence[str], label: str) -> str:
    values = {
        str(row[alias]).strip()
        for alias in aliases
        if alias in row and str(row[alias]).strip() not in {"", "-", "--"}
    }
    if len(values) != 1:
        raise MxSessionReferenceError(f"MX {label} is missing or ambiguous")
    return next(iter(values))


def _one_provider_value(values: Sequence[object], label: str) -> str:
    cleaned = {
        str(value).strip()
        for value in values
        if value is not None and str(value).strip() not in {"", "-", "--"}
    }
    if len(cleaned) != 1:
        raise MxSessionReferenceError(f"MX {label} is missing or ambiguous")
    return next(iter(cleaned))


def _clean_value(value: object, label: str) -> str:
    cleaned = str(value).strip() if value is not None else ""
    if cleaned in {"", "-", "--"}:
        raise MxSessionReferenceError(f"MX {label} contains a missing value")
    return cleaned


def _positive_decimal_text(value: object) -> str:
    try:
        parsed = Decimal(_clean_value(value, "前收盘价"))
    except (InvalidOperation, ValueError) as exc:
        raise MxSessionReferenceError("MX previous close is not numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise MxSessionReferenceError("MX previous close must be positive")
    text = format(parsed, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _board_for_stock(symbol: str) -> Board:
    digits, market = symbol.split(".", maxsplit=1)
    if market == "SH" and digits.startswith(("688", "689")):
        return Board.STAR
    if market == "SZ" and digits.startswith(("300", "301")):
        return Board.CHINEXT
    if (market == "SH" and digits.startswith(("600", "601", "603", "605"))) or (
        market == "SZ" and digits.startswith(("000", "001", "002", "003"))
    ):
        return Board.MAIN
    raise MxSessionReferenceError("security code is outside supported A-share stock boards")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "REFERENCE_SCHEMA_VERSION",
    "MxSessionReferenceAdapter",
    "MxSessionReferenceError",
]
