#!/usr/bin/env python3
"""Create an immutable daily snapshot from 东方财富妙想查数."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from ashare_lab.adapters.market_data.choice_snapshot import (
    CHOICE_DATA_INTEGRITY_EXIT_CODE,
    CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotSpec,
    DailySnapshotSource,
    build_daily_research_snapshot,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.domain.market_data import Board

try:
    from scripts.prepare_choice_snapshot import (
        compare_price_prefix,
        load_corporate_action_source,
        load_session_reference_source,
    )
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from prepare_choice_snapshot import (  # type: ignore[import-not-found]
        compare_price_prefix,
        load_corporate_action_source,
        load_session_reference_source,
    )

MX_DAILY_SOURCE = DailySnapshotSource(
    schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    snapshot_prefix="technical",
    provider="东方财富妙想查数 searchData",
    dataset="daily OHLCV and provider-adjusted daily prices",
    account_scope="server_owned_mx_skill_credential",
    adjustment_field="provider natural-language adjustment qualifier",
    execution_adjustment=0,
    signal_adjustment=2,
    acquisition_implementation="thin MxSaasMarketDataClient searchData adapter",
    limit_event_flags_available=False,
    status_cross_check="validated session reference supplies historical tradestatus/isST",
    previous_close_cross_check=(
        "independent MX daily response agrees with the session-reference preclose within one cent"
    ),
    limitations=(
        "provider response precision and historical coverage are accepted only from rawTable",
        "runtime replay is offline and never calls the provider",
        "daily price limits are derived from the versioned A-share rulebook",
    ),
)
_TICK = Decimal("0.01")
_PRICE_NAMES = ("开盘价", "最高价", "最低价", "收盘价")


def main() -> int:
    args = _parse_args()
    try:
        key = _credential()
        session_rows, session_coverage, session_payload = load_session_reference_source(
            args.session_reference_json, symbol=args.symbol
        )
        actions, action_coverage, action_payload = load_corporate_action_source(
            args.corporate_actions_json, symbol=args.symbol
        )
        result = asyncio.run(_acquire(args, key))
        execution_rows = _execution_rows(
            result["execution"], symbol=args.symbol, session_rows=session_rows
        )
        signal_rows = _price_rows(result["signal"], symbol=args.symbol)
        prefix_rows = _price_rows(result["prefix"], symbol=args.symbol)
        prefix_stability = compare_price_prefix(signal_rows, prefix_rows, args.prefix_end)
        captured_at = datetime.now(UTC)
        snapshot = build_daily_research_snapshot(
            spec=ChoiceSnapshotSpec(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                listing_date=args.listing_date,
                board=Board(args.board),
            ),
            execution_rows=execution_rows,
            signal_rows=signal_rows,
            market_calendar=[date.fromisoformat(str(row["date"])) for row in session_rows],
            raw_audit_payload={
                "mxExecutionRawTable": result["execution"],
                "mxSignalRawTable": result["signal"],
                "mxSignalPrefixRawTable": result["prefix"],
                "sessionReference": session_payload,
                "eastmoneyCorporateActionReference": action_payload,
            },
            request_audit={
                "sourcePolicy": "one provider for the complete requested price series",
                "requests": result["audit"],
                "historicalSessions": {
                    "inputSha256": _sha256_file(args.session_reference_json),
                    "provider": session_coverage["provider"],
                },
                "corporateActions": {
                    "inputSha256": _sha256_file(args.corporate_actions_json),
                    "provider": action_coverage["provider"],
                },
            },
            prefix_stability=prefix_stability,
            output_root=args.output_root,
            captured_at=captured_at,
            sdk_archive_sha256=None,
            session_reference_rows=session_rows,
            session_reference_coverage=session_coverage,
            corporate_actions=actions,
            corporate_action_coverage=action_coverage,
            source=MX_DAILY_SOURCE,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "snapshotId": snapshot.snapshot_id,
                    "path": str(snapshot.path),
                    "provider": snapshot.manifest["provider"],
                    "rows": snapshot.manifest["rowCounts"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (MxSaasProviderUnavailableError, MxSaasProviderAuthError, OSError, TimeoutError) as exc:
        print(f"MX provider unavailable: {type(exc).__name__}")
        return CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE
    except (MxSaasProviderDataError, TypeError, ValueError, KeyError) as exc:
        print(f"MX daily snapshot failed integrity validation: {exc}")
        return CHOICE_DATA_INTEGRITY_EXIT_CODE


async def _acquire(args: argparse.Namespace, key: str) -> dict[str, Any]:
    client = MxSaasMarketDataClient(api_key=key, timeout_seconds=args.timeout_seconds)
    requests = (
        (
            "execution",
            args.start,
            args.end,
            "不复权开盘价、不复权最高价、不复权最低价、不复权收盘价、成交量、成交额、前收盘价、换手率",
        ),
        ("signal", args.start, args.end, "后复权开盘价、后复权最高价、后复权最低价、后复权收盘价"),
        (
            "prefix",
            args.start,
            args.prefix_end,
            "后复权开盘价、后复权最高价、后复权最低价、后复权收盘价",
        ),
    )
    output: dict[str, Any] = {"audit": []}
    for purpose, start, end, indicators in requests:
        query = f"查询{args.symbol}{start.isoformat()}至{end.isoformat()}每个交易日数据"
        response = await client.query_finance(query=query, indicators=indicators)
        table = _one_table(response.tables, args.symbol)
        raw_table = _raw_table(table)
        output[purpose] = {"rawTable": raw_table, "nameMap": _string_map(table.get("nameMap"))}
        output["audit"].append(
            {
                "purpose": purpose,
                "query": response.query,
                "provider": response.provider,
                "schemaVersion": response.provenance.schema_version,
                "responseSha256": response.provenance.response_sha256,
                "retrievedAt": response.provenance.retrieved_at.isoformat(),
                "rows": len(_dates(raw_table)),
            }
        )
    return output


def _price_rows(payload: Mapping[str, object], *, symbol: str) -> list[dict[str, object]]:
    raw = cast(Mapping[str, object], payload["rawTable"])
    names = cast(Mapping[str, str], payload["nameMap"])
    dates = _dates(raw)
    by_name = {name: raw[code] for code, name in names.items() if code in raw}
    missing = set(_PRICE_NAMES) - set(by_name)
    if missing:
        raise ValueError("MX daily response omitted price fields: " + ", ".join(sorted(missing)))
    rows: list[dict[str, object]] = []
    for index, session_date in enumerate(dates):
        rows.append(
            {
                "stock_code": symbol,
                "date": session_date,
                "open": _decimal(_at(by_name["开盘价"], index), "开盘价"),
                "high": _decimal(_at(by_name["最高价"], index), "最高价"),
                "low": _decimal(_at(by_name["最低价"], index), "最低价"),
                "close": _decimal(_at(by_name["收盘价"], index), "收盘价"),
            }
        )
    return sorted(rows, key=lambda row: str(row["date"]))


def _execution_rows(
    payload: Mapping[str, object], *, symbol: str, session_rows: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    rows = _price_rows(payload, symbol=symbol)
    raw = cast(Mapping[str, object], payload["rawTable"])
    names = cast(Mapping[str, str], payload["nameMap"])
    by_name = {name: raw[code] for code, name in names.items() if code in raw}
    required = {"成交量", "成交额", "前收盘价"}
    if required - set(by_name):
        raise ValueError("MX daily response omitted execution fields")
    source_dates = _dates(raw)
    source_index = {item: index for index, item in enumerate(source_dates)}
    sessions = {str(row["date"]): row for row in session_rows}
    if set(source_index) != set(sessions):
        raise ValueError("MX daily rows must align one-to-one with session-reference rows")
    for row in rows:
        day = str(row["date"])
        index = source_index[day]
        session = sessions[day]
        provider_preclose = _decimal(_at(by_name["前收盘价"], index), "前收盘价")
        reference_preclose = _decimal(session["preclose"], "session-reference preclose")
        if abs(provider_preclose - reference_preclose) > _TICK:
            raise ValueError(f"MX preclose conflicts with session reference on {day}")
        row.update(
            {
                "volume": _integer(_at(by_name["成交量"], index), "成交量"),
                "amount": _decimal(_at(by_name["成交额"], index), "成交额"),
                "preclose": reference_preclose,
                "tradestatus": "正常交易" if session["tradestatus"] == "1" else "连续停牌",
            }
        )
        if "换手率" in by_name:
            row.update(
                {
                    "turnover_rate_pct": _decimal(_at(by_name["换手率"], index), "换手率"),
                    "turnover_rate_provider": "eastmoney_mx_finance_data",
                    "turnover_rate_methodology": "provider_reported_daily_turnover_rate_pct",
                }
            )
    return rows


def _one_table(tables: Sequence[Mapping[str, Any]], symbol: str) -> Mapping[str, Any]:
    matches = [
        table
        for table in tables
        if str(table.get("code", "")).upper() == symbol.upper()
        or symbol.upper() in _entity_codes(table)
    ]
    if len(matches) != 1:
        matches = _longest_complete_security_tables(matches)
    if len(matches) != 1:
        raise ValueError(
            "MX daily response must contain exactly one complete requested security table"
        )
    return matches[0]


def _longest_complete_security_tables(
    tables: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Prefer the only complete historical table when MX also returns a short stub.

    妙想查数 can return two tables for the same security: a one-day auxiliary
    table plus the requested historical table.  Selecting by security identity
    alone is therefore too strict, but picking the first table would be unsafe.
    Keep only tables with a non-empty ``rawTable.headName`` and choose the
    unique longest one; equal-length candidates remain ambiguous and fail.
    """

    dated: list[tuple[int, Mapping[str, Any]]] = []
    for table in tables:
        raw_table = table.get("rawTable")
        dates = raw_table.get("headName") if isinstance(raw_table, Mapping) else None
        if isinstance(dates, list) and dates:
            dated.append((len(dates), table))
    if not dated:
        return []
    longest = max(length for length, _table in dated)
    return [table for length, table in dated if length == longest]


def _entity_codes(table: Mapping[str, Any]) -> set[str]:
    values = table.get("entityCodes")
    if not isinstance(values, list):
        return set()
    return {str(code).upper() for code in values}


def _raw_table(table: Mapping[str, Any]) -> Mapping[str, object]:
    value = table.get("rawTable")
    if not isinstance(value, Mapping) or not value:
        raise ValueError("MX daily response omitted rawTable")
    return cast(Mapping[str, object], value)


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("MX daily response omitted nameMap")
    return {str(k): str(v) for k, v in value.items() if str(k) != "headNameSub"}


def _dates(raw: Mapping[str, object]) -> list[str]:
    values = raw.get("headName")
    if not isinstance(values, list) or not values:
        raise ValueError("MX daily response omitted dates")
    dates = [date.fromisoformat(str(value)).isoformat() for value in values]
    if len(dates) != len(set(dates)):
        raise ValueError("MX daily response contains duplicate dates")
    return dates


def _at(values: object, index: int) -> object:
    if not isinstance(values, list) or index >= len(values):
        raise ValueError("MX daily response columns are not aligned")
    return values[index]


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"MX {name} is not numeric") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"MX {name} is invalid")
    return result


def _integer(value: object, name: str) -> int:
    result = _decimal(value, name)
    if result != result.to_integral_value():
        raise ValueError(f"MX {name} is not an integer")
    return int(result)


def _credential() -> str:
    path = Path.home() / ".mx-skills" / "em_api_key"
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise OSError("MX credential is empty")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--prefix-end", type=date.fromisoformat)
    parser.add_argument("--listing-date", type=date.fromisoformat, required=True)
    parser.add_argument("--board", choices=tuple(item.value for item in Board), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--session-reference-json", type=Path, required=True)
    parser.add_argument("--corporate-actions-json", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.prefix_end is None:
        args.prefix_end = args.end - timedelta(days=365)
    if not args.start < args.prefix_end < args.end:
        parser.error("--prefix-end must be strictly inside the requested range")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
