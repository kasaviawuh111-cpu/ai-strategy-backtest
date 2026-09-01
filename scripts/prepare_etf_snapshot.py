#!/usr/bin/env python3
"""Publish one strict SH/SZ stock-ETF daily snapshot after Choice is unavailable."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ashare_lab.adapters.market_data.choice_snapshot import (
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    DailySnapshotSource,
    DailySnapshotSpec,
    build_daily_research_snapshot,
)
from ashare_lab.adapters.market_data.tencent_etf_daily import (
    AKSHARE_SOURCE_COMMIT,
    SSE_DISTRIBUTION_NOTICE_URLS,
    BaoStockEtfEvidence,
    BaoStockEtfRow,
    SseDistributionNoticeEvidence,
    SseDistributionNoticeSource,
    TencentSohuEtfDailySource,
)
from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.market_data import Board


def main() -> int:
    args = _parse_args()
    try:
        reference = _json_object(args.reference_json)
        instrument = _instrument_from_reference(
            reference,
            expected_symbol=args.symbol,
            as_of=args.end,
        )
        overlap = _acquire_baostock_overlap(
            instrument=instrument,
            start=max(args.start, args.end - timedelta(days=45)),
            end=args.end,
        )
        notices = _acquire_official_distributions(instrument, start=args.start, end=args.end)
        with TencentSohuEtfDailySource(timeout=args.timeout_seconds) as source:
            bundle = source.fetch(
                instrument=instrument,
                start=args.start,
                end=args.end,
                baostock_overlap=overlap,
                distribution_notices=notices,
                prefix_end=args.prefix_end,
            )
        request_audit = dict(bundle.request_audit)
        request_audit["fallbackReason"] = args.fallback_reason
        request_audit["fallbackPolicy"] = (
            "public ETF sources may run only after Choice returned provider_unavailable"
        )
        result = build_daily_research_snapshot(
            spec=DailySnapshotSpec(
                symbol=instrument.symbol,
                start=args.start,
                end=args.end,
                listing_date=instrument.listing_date,
                board=Board.STOCK_ETF,
            ),
            execution_rows=bundle.execution_rows,
            signal_rows=bundle.signal_rows,
            market_calendar=bundle.market_calendar,
            raw_audit_payload=bundle.raw_audit_payload,
            request_audit=request_audit,
            prefix_stability=bundle.prefix_stability,
            output_root=args.output_root,
            captured_at=datetime.now(UTC),
            sdk_archive_sha256=None,
            session_reference_rows=bundle.session_reference_rows,
            session_reference_coverage=bundle.session_reference_coverage,
            corporate_actions=bundle.corporate_actions,
            corporate_action_coverage=bundle.corporate_action_coverage,
            source=DailySnapshotSource(
                schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
                snapshot_prefix="technical",
                provider=(
                    "Tencent Finance public + Sohu Finance public + BaoStock overlap + "
                    "official exchange distribution notices"
                ),
                dataset="SH/SZ stock ETF daily kline",
                account_scope="public_undocumented_research_demo",
                adjustment_field="tencentAdjustment",
                execution_adjustment=0,
                signal_adjustment=2,
                acquisition_implementation=(
                    f"AKShare-compatible Tencent adapter@{AKSHARE_SOURCE_COMMIT}"
                ),
                limit_event_flags_available=False,
                status_cross_check="Tencent/Sohu exact axis plus BaoStock overlap",
                previous_close_cross_check="Tencent raw prior close plus BaoStock overlap",
                limitations=(
                    "public endpoint terms require production legal review",
                    "ETF with an unexplained adjustment or missing official distribution proof "
                    "fails closed",
                    "Shenzhen distribution-document acquisition is not implemented",
                ),
            ),
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "snapshotId": result.snapshot_id,
                    "path": str(result.path),
                    "provider": result.manifest["provider"],
                    "rows": result.manifest["rowCounts"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"ETF technical snapshot failed: {type(exc).__name__}: {exc}")
        return 1


def _instrument_from_reference(
    payload: dict[str, object],
    *,
    expected_symbol: str,
    as_of: date,
) -> InstrumentRef:
    raw = payload.get("instrument")
    if not isinstance(raw, dict):
        raise ValueError("BaoStock reference lacks instrument metadata")
    if raw.get("instrument_id") != expected_symbol or raw.get("asset_type") != "ETF":
        raise ValueError("BaoStock reference does not prove the requested ETF identity")
    exchange = expected_symbol.rsplit(".", maxsplit=1)[1]
    status = raw.get("status")
    delisting = raw.get("delisting_date")
    return InstrumentRef(
        symbol=expected_symbol,
        name=_text(raw.get("name"), "ETF name"),
        exchange=Exchange(exchange),
        asset_type=AssetType.ETF,
        currency="CNY",
        listing_date=date.fromisoformat(_text(raw.get("listing_date"), "listing date")),
        delisting_date=(date.fromisoformat(delisting) if isinstance(delisting, str) else None),
        tradable=status == "listed",
        data_source="BaoStock query_stock_basic type=5",
    ).require_tradable_on(as_of)


def _acquire_baostock_overlap(
    *,
    instrument: InstrumentRef,
    start: date,
    end: date,
) -> BaoStockEtfEvidence:
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("BaoStock is required for ETF identity/session cross-check") from exc
    login = bs.login()
    if str(login.error_code) != "0":
        raise RuntimeError(f"BaoStock login failed with code {login.error_code}")
    provider_code = f"{instrument.exchange.value.lower()}.{instrument.symbol[:6]}"
    try:
        result = bs.query_history_k_data_plus(
            provider_code,
            "date,open,high,low,close,preclose,volume,amount,tradestatus",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag="3",
        )
        rows: list[BaoStockEtfRow] = []
        while result.next():
            row = dict(zip(result.fields, result.get_row_data(), strict=True))
            rows.append(
                BaoStockEtfRow(
                    session_date=date.fromisoformat(row["date"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    previous_close=Decimal(row["preclose"]),
                    volume_shares=int(Decimal(row["volume"])),
                    amount_cny=Decimal(row["amount"]),
                    trading_status=row["tradestatus"],
                )
            )
        if str(result.error_code) != "0" or not rows:
            raise RuntimeError("BaoStock returned no successful ETF overlap rows")
    finally:
        bs.logout()
    canonical = [row.as_dict() for row in rows]
    return BaoStockEtfEvidence(
        provider="BaoStock Python API",
        request_params={
            "code": provider_code,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "frequency": "d",
            "adjustflag": "3",
        },
        queried_at=datetime.now(UTC).replace(microsecond=0),
        canonical_response_sha256=hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        rows=tuple(rows),
    )


def _acquire_official_distributions(
    instrument: InstrumentRef,
    *,
    start: date,
    end: date,
) -> tuple[SseDistributionNoticeEvidence, ...]:
    if instrument.symbol != "510300.SH":
        return ()
    notices = []
    with SseDistributionNoticeSource(timeout=30) as source:
        for url in SSE_DISTRIBUTION_NOTICE_URLS:
            notice = source.fetch(url, instrument=instrument)
            if start <= notice.terms.ex_date <= end:
                notices.append(notice)
    return tuple(notices)


def _json_object(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("BaoStock reference artifact must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be text")
    return value.strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--prefix-end", type=date.fromisoformat, required=True)
    parser.add_argument("--reference-json", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--fallback-reason", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if not args.start < args.prefix_end < args.end:
        parser.error("--prefix-end must be strictly inside the requested range")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
