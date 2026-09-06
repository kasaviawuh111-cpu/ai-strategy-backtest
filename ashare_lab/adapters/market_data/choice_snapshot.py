"""Build immutable, validated daily research snapshots for the daily engine.

The original producer was Choice, so the compatibility module name and public
aliases remain.  New producers must declare their real identity through
``DailySnapshotSource`` instead of masquerading as Choice.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.domain.execution import HistoricalAshareRuleBook, PriceLimitRuleInput
from ashare_lab.domain.market_data import (
    Board,
    CorporateAction,
    CorporateActionKind,
    TradingStatus,
)
from ashare_lab.domain.shared import InstrumentId, Price

from .local_parquet import normalize_instrument_id

SNAPSHOT_SCHEMA_VERSION = "choice.daily-research-snapshot.v1"
TECHNICAL_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.daily-research-snapshot.v1"
CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE = 75
CHOICE_DATA_INTEGRITY_EXIT_CODE = 65
ACCOUNT_SCOPE = "personal_research_demo"
EXECUTION_FILENAME = "daily_ohlcv.parquet"
SIGNAL_FILENAME = "signal_daily_ohlcv.parquet"
SESSION_FILENAME = "instrument_sessions.parquet"
CORPORATE_ACTION_FILENAME = "corporate_actions.parquet"
MANIFEST_FILENAME = "snapshot_manifest.json"
_TURNOVER_RATE_FIELDS = (
    "turnover_rate_pct",
    "turnover_rate_provider",
    "turnover_rate_methodology",
)
_HASH_CHUNK_BYTES = 1024 * 1024
_SECRET_KEY_FRAGMENTS = ("password", "passwd", "token", "userinfo", "mobile", "phone")
STRICT_CORPORATE_ACTION_COVERAGE_SCOPE = "all_corporate_action_categories"
STRICT_CORPORATE_ACTION_CATEGORIES = tuple(sorted(item.value for item in CorporateActionKind))
MIXED_CORPORATE_ACTION_COVERAGE_SCOPE = "all_categories_mixed_positive_and_negative_proof"
MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES = tuple(
    sorted(
        (
            CorporateActionKind.CASH_DIVIDEND.value,
            CorporateActionKind.RIGHTS_ISSUE.value,
            CorporateActionKind.SHARE_DISTRIBUTION.value,
        )
    )
)
MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES = tuple(
    sorted(
        (
            CorporateActionKind.REVERSE_SPLIT.value,
            CorporateActionKind.STOCK_SPLIT.value,
        )
    )
)
SESSION_REFERENCE_FIELDS = ("date", "preclose", "tradestatus", "isST")
SESSION_REFERENCE_METHOD = "query_history_k_data_plus"
SESSION_REFERENCE_FREQUENCY = "d"
SESSION_REFERENCE_ADJUST_FLAG = "3"
ETF_SESSION_REFERENCE_SCHEMA_VERSION = "ashare-lab.stock-etf-session-reference.v1"
PROVIDER_NEUTRAL_SESSION_REFERENCE_SCHEMA_VERSION = (
    "ashare-lab.instrument-session-reference.v3"
)
ETF_CORPORATE_ACTION_COVERAGE_SCOPE = "stock_etf_cash_and_unit_change_reconciliation"
_CHOICE_TRADING_STATUS = "正常交易"
_CHOICE_SUSPENDED_STATUSES = frozenset({"连续停牌"})
_ARROW: Any = pa
_PARQUET: Any = pq


class ChoiceSnapshotError(RuntimeError):
    """Choice data cannot be promoted into a replayable research snapshot."""


@dataclass(frozen=True, slots=True)
class DailySnapshotSource:
    """Identity and evidence semantics for one validated daily-price producer."""

    schema_version: str
    snapshot_prefix: str
    provider: str
    dataset: str
    account_scope: str
    adjustment_field: str
    execution_adjustment: int
    signal_adjustment: int
    acquisition_implementation: str
    limit_event_flags_available: bool
    status_cross_check: str
    previous_close_cross_check: str
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        required_text = (
            self.schema_version,
            self.snapshot_prefix,
            self.provider,
            self.dataset,
            self.account_scope,
            self.adjustment_field,
            self.acquisition_implementation,
            self.status_cross_check,
            self.previous_close_cross_check,
        )
        if any(not value.strip() for value in required_text):
            raise ValueError("daily snapshot source text fields must be non-empty")
        if self.snapshot_prefix not in {"choice", "technical"}:
            raise ValueError("daily snapshot prefix must be choice or technical")
        if not self.limitations or any(not value.strip() for value in self.limitations):
            raise ValueError("daily snapshot source limitations must be non-empty text")


CHOICE_DAILY_SOURCE = DailySnapshotSource(
    schema_version=SNAPSHOT_SCHEMA_VERSION,
    snapshot_prefix="choice",
    provider="Choice Quant API",
    dataset="Choice csd daily series",
    account_scope=ACCOUNT_SCOPE,
    adjustment_field="choiceAdjustFlag",
    execution_adjustment=1,
    signal_adjustment=2,
    acquisition_implementation="Choice EmQuantAPI Python SDK",
    limit_event_flags_available=True,
    status_cross_check="Choice TRADESTATUS equals BaoStock tradestatus per date",
    previous_close_cross_check="exact decimal equality across providers",
    limitations=(
        "personal research account; not authorized for production use",
        "daily price-limit prices are derived from versioned exchange rules using "
        "BaoStock historical isST/preclose; Choice HIGHLIMIT/LOWLIMIT are event flags, "
        "not vendor limit prices",
        "BaoStock evidence hashes canonical normalized responses, not raw wire bytes",
        "event data is not included because the current account lacks regularreport access",
    ),
)


@dataclass(frozen=True, slots=True)
class ChoiceSnapshotSpec:
    symbol: str
    start: date
    end: date
    listing_date: date
    board: Board

    def __post_init__(self) -> None:
        canonical = normalize_instrument_id(self.symbol)
        if str(canonical) != self.symbol.upper():
            raise ChoiceSnapshotError(f"symbol must be canonical: {canonical}")
        if self.start > self.end:
            raise ChoiceSnapshotError("snapshot start must not exceed end")
        if self.listing_date > self.start:
            raise ChoiceSnapshotError(
                "research session derivation requires a listing date before the snapshot start"
            )


@dataclass(frozen=True, slots=True)
class ChoiceSnapshotResult:
    snapshot_id: str
    path: Path
    manifest: Mapping[str, object]


DailySnapshotSpec = ChoiceSnapshotSpec
DailySnapshotResult = ChoiceSnapshotResult


def build_daily_research_snapshot(
    *,
    spec: ChoiceSnapshotSpec,
    execution_rows: Sequence[Mapping[str, object]],
    signal_rows: Sequence[Mapping[str, object]],
    market_calendar: Sequence[date],
    raw_audit_payload: Mapping[str, object],
    request_audit: Mapping[str, object],
    prefix_stability: Mapping[str, object],
    output_root: Path,
    captured_at: datetime,
    sdk_archive_sha256: str | None,
    session_reference_rows: Sequence[Mapping[str, object]],
    session_reference_coverage: Mapping[str, object],
    corporate_actions: Sequence[CorporateAction] = (),
    corporate_action_coverage: Mapping[str, object] | None = None,
    source: DailySnapshotSource = CHOICE_DAILY_SOURCE,
) -> ChoiceSnapshotResult:
    """Validate both price bases, write canonical files, and publish atomically."""

    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ChoiceSnapshotError("captured_at must include an explicit timezone")
    _reject_secret_keys(raw_audit_payload)
    _reject_secret_keys(request_audit)
    _reject_secret_keys(session_reference_coverage)

    canonical_execution = _canonicalize_rows(
        execution_rows,
        spec=spec,
        required_extra=("preclose", "tradestatus"),
        require_limit_event_flags=source.limit_event_flags_available,
    )
    canonical_signal = _canonicalize_rows(signal_rows, spec=spec)
    _validate_alignment(canonical_execution, canonical_signal)
    _validate_calendar(canonical_execution, market_calendar, spec)
    _validate_prefix_stability(prefix_stability)
    validated_session_reference, validated_session_coverage = _validate_session_reference(
        session_reference_rows,
        coverage=session_reference_coverage,
        execution_rows=canonical_execution,
        spec=spec,
    )
    validated_actions = _validate_corporate_actions(
        corporate_actions,
        spec=spec,
    )
    validated_action_coverage = _validate_corporate_action_coverage(
        corporate_action_coverage,
        spec=spec,
        actions=validated_actions,
    )

    signal_output = _signal_rows_with_raw_liquidity(canonical_execution, canonical_signal)
    sessions = _derive_research_sessions(
        spec,
        canonical_execution,
        validated_session_reference,
    )

    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{source.snapshot_prefix}-snapshot-", dir=destination_root)
    )
    try:
        raw_root = temporary / "raw"
        raw_root.mkdir(parents=True)
        _write_json(raw_root / "provider_response.json", raw_audit_payload)
        _write_json(raw_root / "requests.json", request_audit)
        _write_daily(temporary / EXECUTION_FILENAME, canonical_execution)
        _write_daily(temporary / SIGNAL_FILENAME, signal_output)
        _write_sessions(temporary / SESSION_FILENAME, sessions)
        _write_corporate_actions(
            temporary / CORPORATE_ACTION_FILENAME,
            validated_actions,
        )

        data_paths = (
            temporary / EXECUTION_FILENAME,
            temporary / SIGNAL_FILENAME,
            temporary / SESSION_FILENAME,
            temporary / CORPORATE_ACTION_FILENAME,
            raw_root / "provider_response.json",
            raw_root / "requests.json",
        )
        file_manifest = {
            str(path.relative_to(temporary)): {
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in data_paths
        }
        manifest_body: dict[str, object] = {
            "schemaVersion": source.schema_version,
            "provider": source.provider,
            "sourceDataset": source.dataset,
            "accountScope": source.account_scope,
            "acquisitionImplementation": source.acquisition_implementation,
            "symbol": spec.symbol,
            "requestedRange": [spec.start.isoformat(), spec.end.isoformat()],
            "capturedAt": captured_at.astimezone(UTC).isoformat(),
            "rowCounts": {
                "execution": len(canonical_execution),
                "signal": len(signal_output),
                "sessions": len(sessions),
                "corporateActions": len(validated_actions),
            },
            "priceBases": {
                EXECUTION_FILENAME: {
                    "basis": "unadjusted",
                    source.adjustment_field: source.execution_adjustment,
                    "uses": ["matching", "fees", "position_sizing", "ledger", "valuation"],
                },
                SIGNAL_FILENAME: {
                    "basis": "back_adjusted",
                    source.adjustment_field: source.signal_adjustment,
                    "uses": ["relative_price_technical_signals"],
                    "volumeAndAmountSource": EXECUTION_FILENAME,
                },
            },
            "sessionReference": {
                "kind": (
                    "stock-etf-multi-source-daily-facts-plus-versioned-rulebook"
                    if validated_session_coverage.get("schemaVersion")
                    == ETF_SESSION_REFERENCE_SCHEMA_VERSION
                    else (
                        "provider-neutral-historical-facts-plus-versioned-rulebook"
                        if validated_session_coverage.get("schemaVersion")
                        == PROVIDER_NEUTRAL_SESSION_REFERENCE_SCHEMA_VERSION
                        else "baostock-historical-facts-plus-versioned-rulebook"
                    )
                ),
                "provider": validated_session_coverage["provider"],
                "queryMethod": validated_session_coverage.get(
                    "queryMethod",
                    SESSION_REFERENCE_METHOD,
                ),
                "fields": list(SESSION_REFERENCE_FIELDS),
                "frequency": SESSION_REFERENCE_FREQUENCY,
                "adjustFlag": validated_session_coverage.get(
                    "adjustFlag",
                    SESSION_REFERENCE_ADJUST_FLAG,
                ),
                "coverage": validated_session_coverage,
                "ruleVersion": HistoricalAshareRuleBook.version,
                "board": spec.board.value,
                "listingDate": spec.listing_date.isoformat(),
                "statusCrossCheck": source.status_cross_check,
                "previousCloseCrossCheck": source.previous_close_cross_check,
                "priceLimitSource": (
                    "stock ETF rule derived from ruleVersion using Tencent prior raw close"
                    if spec.board is Board.STOCK_ETF
                    else (
                        "derived from ruleVersion using provider-neutral isST/preclose"
                        if validated_session_coverage.get("schemaVersion")
                        == PROVIDER_NEUTRAL_SESSION_REFERENCE_SCHEMA_VERSION
                        else "derived from ruleVersion using BaoStock isST/preclose"
                    )
                ),
            },
            "adjustmentPrefixStability": dict(prefix_stability),
            "corporateActionCoverage": validated_action_coverage,
            "sdkArchiveSha256": sdk_archive_sha256,
            "files": file_manifest,
            "capabilities": {
                "technicalDaily": "validated_for_demo",
                "events": "unavailable_current_account",
                "corporateActions": "validated_for_demo",
                "corporateActionLedger": "point_in_time.v1",
            },
            "limitations": list(source.limitations),
        }
        snapshot_digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"{source.snapshot_prefix}:{snapshot_digest}"
        manifest = {"snapshotId": snapshot_id, **manifest_body}
        _write_json(temporary / MANIFEST_FILENAME, manifest)

        final_path = destination_root / snapshot_digest
        if final_path.exists():
            existing_manifest = final_path / MANIFEST_FILENAME
            if (
                not existing_manifest.is_file()
                or json.loads(existing_manifest.read_text(encoding="utf-8")) != manifest
            ):
                raise ChoiceSnapshotError(
                    f"snapshot destination already exists with different content: {final_path}"
                )
            _validate_published_snapshot(
                final_path,
                manifest,
                snapshot_prefix=source.snapshot_prefix,
            )
            shutil.rmtree(temporary)
        else:
            temporary.replace(final_path)
        return ChoiceSnapshotResult(snapshot_id=snapshot_id, path=final_path, manifest=manifest)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


# Backwards-compatible API for existing Choice acquisition code.
build_choice_snapshot = build_daily_research_snapshot


def _canonicalize_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    spec: ChoiceSnapshotSpec,
    required_extra: Sequence[str] = (),
    require_limit_event_flags: bool = False,
) -> list[dict[str, object]]:
    limit_fields = ("highlimit", "lowlimit") if require_limit_event_flags else ()
    required = ("date", "open", "high", "low", "close", *required_extra, *limit_fields)
    normalized: list[dict[str, object]] = []
    for index, raw in enumerate(rows):
        lowered = {str(key).lower(): value for key, value in raw.items()}
        missing = [name for name in required if name not in lowered]
        if missing:
            raise ChoiceSnapshotError(
                f"Choice row {index} is missing required fields: {', '.join(missing)}"
            )
        session_date = _as_date(lowered["date"], f"row {index} date")
        if not spec.start <= session_date <= spec.end:
            raise ChoiceSnapshotError(
                f"Choice row date is outside the requested range: {session_date}"
            )
        prices = {
            name: _positive_decimal(lowered[name], f"row {index} {name}")
            for name in ("open", "high", "low", "close")
        }
        if prices["low"] > min(prices.values()) or prices["high"] < max(prices.values()):
            raise ChoiceSnapshotError(f"Choice row {index} has inconsistent OHLC values")
        item: dict[str, object] = {
            "stock_code": spec.symbol.split(".", maxsplit=1)[0],
            "date": session_date,
            **prices,
        }
        if required_extra:
            choice_status = _choice_trading_status(lowered["tradestatus"], index)
            if choice_status is TradingStatus.SUSPENDED:
                item["volume"] = _suspended_liquidity(
                    lowered.get("volume"),
                    index=index,
                    field_name="volume",
                    integer=True,
                )
                item["amount"] = _suspended_liquidity(
                    lowered.get("amount"),
                    index=index,
                    field_name="amount",
                    integer=False,
                )
            else:
                item["volume"] = _positive_integer(
                    lowered.get("volume"),
                    f"row {index} trading volume",
                )
                item["amount"] = _positive_decimal(
                    lowered.get("amount"),
                    f"row {index} trading amount",
                )
            item["preclose"] = _positive_decimal(lowered["preclose"], f"row {index} preclose")
            item["choice_trading_status"] = choice_status
            item["choice_tradestatus_raw"] = cast(str, lowered["tradestatus"]).strip()
            if require_limit_event_flags:
                item["highlimit"] = _limit_flag(lowered["highlimit"], index, "highlimit")
                item["lowlimit"] = _limit_flag(lowered["lowlimit"], index, "lowlimit")
        else:
            item["volume"] = _non_negative_integer(
                lowered.get("volume", 0),
                f"row {index} volume",
            )
            item["amount"] = _non_negative_decimal(
                lowered.get("amount", 0),
                f"row {index} amount",
            )
        turnover_rate_fields_present = tuple(
            field_name in lowered for field_name in _TURNOVER_RATE_FIELDS
        )
        if any(turnover_rate_fields_present):
            if not all(turnover_rate_fields_present):
                raise ChoiceSnapshotError(
                    f"row {index} has partial provider turnover-rate provenance"
                )
            item["turnover_rate_pct"] = _non_negative_decimal(
                lowered["turnover_rate_pct"],
                f"row {index} turnover_rate_pct",
            )
            item["turnover_rate_provider"] = _non_empty_text(
                lowered["turnover_rate_provider"],
                f"row {index} turnover_rate_provider",
            )
            item["turnover_rate_methodology"] = _non_empty_text(
                lowered["turnover_rate_methodology"],
                f"row {index} turnover_rate_methodology",
            )
        normalized.append(item)
    normalized.sort(key=lambda item: cast(date, item["date"]))
    dates = [cast(date, item["date"]) for item in normalized]
    if not dates:
        raise ChoiceSnapshotError("Choice returned no daily rows")
    if len(dates) != len(set(dates)):
        raise ChoiceSnapshotError("Choice returned duplicate daily rows")
    return normalized


def _validate_alignment(
    execution_rows: Sequence[Mapping[str, object]],
    signal_rows: Sequence[Mapping[str, object]],
) -> None:
    execution_dates = tuple(item["date"] for item in execution_rows)
    signal_dates = tuple(item["date"] for item in signal_rows)
    if execution_dates != signal_dates:
        raise ChoiceSnapshotError(
            "unadjusted execution rows and adjusted signal rows must align one-to-one"
        )


def _validate_calendar(
    execution_rows: Sequence[Mapping[str, object]],
    market_calendar: Sequence[date],
    spec: ChoiceSnapshotSpec,
) -> None:
    calendar = tuple(sorted({item for item in market_calendar if spec.start <= item <= spec.end}))
    row_dates = tuple(cast(date, item["date"]) for item in execution_rows)
    if calendar != row_dates:
        missing = sorted(set(calendar) - set(row_dates))
        unexpected = sorted(set(row_dates) - set(calendar))
        raise ChoiceSnapshotError(
            "security rows must exactly match the market calendar for this research snapshot; "
            f"missing={len(missing)}, unexpected={len(unexpected)}"
        )


def _validate_prefix_stability(result: Mapping[str, object]) -> None:
    if result.get("status") != "passed":
        raise ChoiceSnapshotError(
            "back-adjusted series did not pass the no-future prefix-stability check"
        )


def _validate_session_reference(
    rows: Sequence[Mapping[str, object]],
    *,
    coverage: Mapping[str, object],
    execution_rows: Sequence[Mapping[str, object]],
    spec: ChoiceSnapshotSpec,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    normalized: list[dict[str, object]] = []
    provider_rows: list[dict[str, str]] = []
    expected_keys = set(SESSION_REFERENCE_FIELDS)
    for index, raw in enumerate(rows):
        if set(raw) != expected_keys:
            raise ChoiceSnapshotError(
                f"session-reference row {index} fields must exactly match "
                + ",".join(SESSION_REFERENCE_FIELDS)
            )
        raw_date = raw.get("date")
        if not isinstance(raw_date, str):
            raise ChoiceSnapshotError(f"session-reference row {index} date must be ISO text")
        try:
            session_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ChoiceSnapshotError(
                f"session-reference row {index} date must be ISO text"
            ) from exc
        raw_preclose = raw.get("preclose")
        raw_trade_status = raw.get("tradestatus")
        raw_is_st = raw.get("isST")
        if not isinstance(raw_preclose, str):
            raise ChoiceSnapshotError(
                f"session-reference row {index} preclose must be decimal text"
            )
        if raw_trade_status not in {"0", "1"}:
            raise ChoiceSnapshotError(
                f"session-reference row {index} tradestatus must be 0 or 1"
            )
        if raw_is_st not in {"0", "1"}:
            raise ChoiceSnapshotError(f"session-reference row {index} isST must be 0 or 1")
        normalized.append(
            {
                "date": session_date,
                "preclose": _positive_decimal(
                    raw_preclose,
                    f"session-reference row {index} preclose",
                ),
                "trading_status": (
                    TradingStatus.TRADING if raw_trade_status == "1" else TradingStatus.SUSPENDED
                ),
                "is_st": raw_is_st == "1",
            }
        )
        provider_rows.append(
            {
                "date": raw_date,
                "preclose": raw_preclose.strip(),
                "tradestatus": cast(str, raw_trade_status),
                "isST": cast(str, raw_is_st),
            }
        )

    normalized.sort(key=lambda item: cast(date, item["date"]))
    provider_rows.sort(key=lambda item: item["date"])
    reference_dates = tuple(cast(date, item["date"]) for item in normalized)
    if len(reference_dates) != len(set(reference_dates)):
        raise ChoiceSnapshotError("session reference contains duplicate dates")
    execution_dates = tuple(cast(date, item["date"]) for item in execution_rows)
    if reference_dates != execution_dates:
        missing = sorted(set(execution_dates) - set(reference_dates))
        unexpected = sorted(set(reference_dates) - set(execution_dates))
        raise ChoiceSnapshotError(
            "session-reference dates must exactly match Choice/daily rows and the market calendar; "
            f"missing={len(missing)}, unexpected={len(unexpected)}"
        )
    for execution, reference in zip(execution_rows, normalized, strict=True):
        session_date = cast(date, execution["date"])
        if cast(Decimal, execution["preclose"]) != cast(Decimal, reference["preclose"]):
            raise ChoiceSnapshotError(
                f"daily data and session-reference preclose disagree on {session_date.isoformat()}"
            )
        if execution["choice_trading_status"] is not reference["trading_status"]:
            raise ChoiceSnapshotError(
                "daily data and session-reference trading status disagree on "
                f"{session_date.isoformat()}"
            )

    validated_coverage = _validate_session_reference_coverage(
        coverage,
        provider_rows=provider_rows,
        spec=spec,
    )
    return normalized, validated_coverage


def _validate_session_reference_coverage(
    coverage: Mapping[str, object],
    *,
    provider_rows: Sequence[Mapping[str, str]],
    spec: ChoiceSnapshotSpec,
) -> dict[str, object]:
    if coverage.get("schemaVersion") == PROVIDER_NEUTRAL_SESSION_REFERENCE_SCHEMA_VERSION:
        return _validate_provider_neutral_session_reference_coverage(
            coverage,
            provider_rows=provider_rows,
            spec=spec,
        )
    if coverage.get("schemaVersion") == ETF_SESSION_REFERENCE_SCHEMA_VERSION:
        return _validate_etf_session_reference_coverage(
            coverage,
            provider_rows=provider_rows,
            spec=spec,
        )
    if coverage.get("status") != "complete" or coverage.get("querySucceeded") is not True:
        raise ChoiceSnapshotError("BaoStock session-reference query must complete successfully")
    if coverage.get("instrumentId") != spec.symbol:
        raise ChoiceSnapshotError("BaoStock session reference belongs to another instrument")
    provider = coverage.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ChoiceSnapshotError("BaoStock session-reference provider is required")
    if (
        coverage.get("start") != spec.start.isoformat()
        or coverage.get("end") != spec.end.isoformat()
    ):
        raise ChoiceSnapshotError("BaoStock session-reference range must exactly match Choice")
    if coverage.get("fields") != list(SESSION_REFERENCE_FIELDS):
        raise ChoiceSnapshotError("BaoStock session-reference fields are not the strict field set")
    if (
        coverage.get("frequency") != SESSION_REFERENCE_FREQUENCY
        or coverage.get("adjustFlag") != SESSION_REFERENCE_ADJUST_FLAG
        or coverage.get("priceBasis") != "unadjusted"
    ):
        raise ChoiceSnapshotError("BaoStock session reference must be unadjusted daily data")
    if coverage.get("rowCount") != len(provider_rows):
        raise ChoiceSnapshotError("BaoStock session-reference row count does not match its rows")
    if coverage.get("zeroResult") is not (not provider_rows):
        raise ChoiceSnapshotError("BaoStock session-reference zero-result flag is inconsistent")
    returned_start = provider_rows[0]["date"] if provider_rows else None
    returned_end = provider_rows[-1]["date"] if provider_rows else None
    if (
        coverage.get("returnedStart") != returned_start
        or coverage.get("returnedEnd") != returned_end
    ):
        raise ChoiceSnapshotError("BaoStock session-reference returned range is inconsistent")

    raw_audits = coverage.get("queryAudits")
    if not isinstance(raw_audits, Sequence) or isinstance(raw_audits, str | bytes | bytearray):
        raise ChoiceSnapshotError("BaoStock session-reference query audits must be a list")
    audits = list(cast(Sequence[object], raw_audits))
    expected_intervals = [
        (max(spec.start, date(year, 1, 1)), min(spec.end, date(year, 12, 31)))
        for year in range(spec.start.year, spec.end.year + 1)
    ]
    if len(audits) != len(expected_intervals):
        raise ChoiceSnapshotError("BaoStock session-reference annual query coverage is incomplete")
    expected_code = (
        f"sh.{spec.symbol[:6]}" if spec.symbol.endswith(".SH") else f"sz.{spec.symbol[:6]}"
    )
    audited_rows: list[dict[str, str]] = []
    audited_intervals: list[dict[str, object]] = []
    for index, (raw_audit, (interval_start, interval_end)) in enumerate(
        zip(audits, expected_intervals, strict=True)
    ):
        if not isinstance(raw_audit, Mapping):
            raise ChoiceSnapshotError(f"BaoStock session audit {index} must be an object")
        audit = cast(Mapping[str, object], raw_audit)
        params = audit.get("params")
        if not isinstance(params, Mapping):
            raise ChoiceSnapshotError(f"BaoStock session audit {index} params must be an object")
        raw_params = cast(Mapping[object, object], params)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_params.items()
        ):
            raise ChoiceSnapshotError(f"BaoStock session audit {index} params must contain text")
        normalized_params = {cast(str, key): cast(str, value) for key, value in raw_params.items()}
        expected_params = {
            "code": expected_code,
            "fields": ",".join(SESSION_REFERENCE_FIELDS),
            "start_date": interval_start.isoformat(),
            "end_date": interval_end.isoformat(),
            "frequency": SESSION_REFERENCE_FREQUENCY,
            "adjustflag": SESSION_REFERENCE_ADJUST_FLAG,
        }
        if audit.get("method") != SESSION_REFERENCE_METHOD or normalized_params != expected_params:
            raise ChoiceSnapshotError(
                f"BaoStock session audit {index} request does not match the strict query"
            )
        raw_fields = audit.get("fields")
        if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, str | bytes | bytearray):
            raise ChoiceSnapshotError(f"BaoStock session audit {index} fields are invalid")
        fields = tuple(cast(Sequence[object], raw_fields))
        if len(fields) != len(SESSION_REFERENCE_FIELDS) or set(fields) != set(
            SESSION_REFERENCE_FIELDS
        ):
            raise ChoiceSnapshotError(f"BaoStock session audit {index} fields are invalid")
        if audit.get("errorCode") != "0":
            raise ChoiceSnapshotError(f"BaoStock session audit {index} did not succeed")
        raw_rows = audit.get("rows")
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, str | bytes | bytearray):
            raise ChoiceSnapshotError(f"BaoStock session audit {index} rows must be a list")
        audit_rows: list[dict[str, str]] = []
        for raw_row in cast(Sequence[object], raw_rows):
            if not isinstance(raw_row, Mapping):
                raise ChoiceSnapshotError(f"BaoStock session audit {index} contains malformed rows")
            row_mapping = cast(Mapping[object, object], raw_row)
            if set(row_mapping) != set(SESSION_REFERENCE_FIELDS):
                raise ChoiceSnapshotError(f"BaoStock session audit {index} contains malformed rows")
            if any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in row_mapping.items()
            ):
                raise ChoiceSnapshotError(f"BaoStock session audit {index} row values must be text")
            audit_rows.append(
                {cast(str, key): cast(str, value) for key, value in row_mapping.items()}
            )
        if audit.get("rowCount") != len(audit_rows) or audit.get("zeroResult") is not (
            not audit_rows
        ):
            raise ChoiceSnapshotError(
                f"BaoStock session audit {index} row metadata is inconsistent"
            )
        digest = _normalized_baostock_audit_sha256(
            method=SESSION_REFERENCE_METHOD,
            params=expected_params,
            fields=fields,
            rows=audit_rows,
            error_code="0",
            error_message=audit.get("errorMessage"),
        )
        if audit.get("normalizedResponseSha256") != digest:
            raise ChoiceSnapshotError(
                f"BaoStock session audit {index} normalized response hash is invalid"
            )
        audited_rows.extend(audit_rows)
        audited_intervals.append(
            {
                "start": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "rowCount": len(audit_rows),
                "zeroResult": not audit_rows,
                "normalizedResponseSha256": digest,
            }
        )

    if _sorted_canonical_rows(audited_rows) != _sorted_canonical_rows(provider_rows):
        raise ChoiceSnapshotError(
            "BaoStock session-reference rows do not match the audited provider responses"
        )
    if coverage.get("intervals") != audited_intervals:
        raise ChoiceSnapshotError("BaoStock session-reference interval coverage is inconsistent")
    aggregate_digest = _canonical_sha256(audits)
    if coverage.get("normalizedResponseSha256") != aggregate_digest:
        raise ChoiceSnapshotError("BaoStock session-reference aggregate query hash is invalid")
    row_digest = _canonical_sha256(list(provider_rows))
    if coverage.get("canonicalRowsSha256") != row_digest:
        raise ChoiceSnapshotError("BaoStock session-reference canonical row hash is invalid")
    if coverage.get("hashSemantics") != (
        "sha256_of_canonical_normalized_results_not_raw_wire_bytes"
    ):
        raise ChoiceSnapshotError("BaoStock session-reference hash semantics are missing")
    if coverage.get("paginationPolicy") != ("annual_queries_bounded_to_at_most_366_calendar_days"):
        raise ChoiceSnapshotError("BaoStock session-reference pagination policy is unsafe")
    return {
        "status": "complete",
        "querySucceeded": True,
        "provider": provider.strip(),
        "instrumentId": spec.symbol,
        "start": spec.start.isoformat(),
        "end": spec.end.isoformat(),
        "rowCount": len(provider_rows),
        "returnedStart": returned_start,
        "returnedEnd": returned_end,
        "normalizedResponseSha256": aggregate_digest,
        "canonicalRowsSha256": row_digest,
        "hashSemantics": coverage["hashSemantics"],
        "paginationPolicy": coverage["paginationPolicy"],
        "intervals": audited_intervals,
    }


def _validate_provider_neutral_session_reference_coverage(
    coverage: Mapping[str, object],
    *,
    provider_rows: Sequence[Mapping[str, str]],
    spec: ChoiceSnapshotSpec,
) -> dict[str, object]:
    """Validate v3 evidence without inheriting BaoStock-specific semantics."""

    if coverage.get("status") != "complete" or coverage.get("querySucceeded") is not True:
        raise ChoiceSnapshotError("provider-neutral session acquisition must be complete")
    if coverage.get("instrumentId") != spec.symbol:
        raise ChoiceSnapshotError("provider-neutral session evidence belongs to another instrument")
    provider = _coverage_text(coverage.get("provider"), "session-reference provider")
    if provider != "eastmoney_mx_finance_data":
        raise ChoiceSnapshotError("v3 session evidence has an unsupported provider")
    if (
        coverage.get("start") != spec.start.isoformat()
        or coverage.get("end") != spec.end.isoformat()
    ):
        raise ChoiceSnapshotError("provider-neutral session range must exactly match the snapshot")
    if coverage.get("fields") != list(SESSION_REFERENCE_FIELDS):
        raise ChoiceSnapshotError("provider-neutral session fields are not the strict field set")
    if coverage.get("providerFields") != ["前收盘价", "交易状态", "是否为ST股票"]:
        raise ChoiceSnapshotError("provider-neutral source fields are not the proven MX field set")
    if (
        coverage.get("frequency") != "1d"
        or coverage.get("adjustFlag") != "provider_unadjusted_preclose"
        or coverage.get("priceBasis") != "unadjusted"
    ):
        raise ChoiceSnapshotError("provider-neutral session evidence must be unadjusted daily data")
    if coverage.get("rowCount") != len(provider_rows) or coverage.get("zeroResult") is not (
        not provider_rows
    ):
        raise ChoiceSnapshotError("provider-neutral session row metadata is inconsistent")
    returned_start = provider_rows[0]["date"] if provider_rows else None
    returned_end = provider_rows[-1]["date"] if provider_rows else None
    if (
        coverage.get("returnedStart") != returned_start
        or coverage.get("returnedEnd") != returned_end
    ):
        raise ChoiceSnapshotError("provider-neutral returned range is inconsistent")
    if coverage.get("canonicalRowsSha256") != _canonical_sha256(list(provider_rows)):
        raise ChoiceSnapshotError("provider-neutral canonical row hash is invalid")
    if coverage.get("dateAxisSha256") != _canonical_sha256(
        [row["date"] for row in provider_rows]
    ):
        raise ChoiceSnapshotError("provider-neutral date-axis hash is invalid")
    if coverage.get("hashSemantics") != (
        "provider_raw_wire_sha256_plus_canonical_normalized_rows_sha256"
    ):
        raise ChoiceSnapshotError("provider-neutral hash semantics are missing")
    if coverage.get("paginationPolicy") != (
        "annual_searchData_queries_bounded_to_at_most_366_calendar_days"
    ):
        raise ChoiceSnapshotError("provider-neutral pagination policy is unsafe")
    if coverage.get("queryMethod") != (
        "Eastmoney MX searchData annual exact-symbol daily facts"
    ):
        raise ChoiceSnapshotError("provider-neutral query method is invalid")

    raw_audits = coverage.get("queryAudits")
    if not isinstance(raw_audits, Sequence) or isinstance(
        raw_audits,
        str | bytes | bytearray,
    ):
        raise ChoiceSnapshotError("provider-neutral query audits must be a list")
    audits = list(cast(Sequence[object], raw_audits))
    expected_intervals = [
        (max(spec.start, date(year, 1, 1)), min(spec.end, date(year, 12, 31)))
        for year in range(spec.start.year, spec.end.year + 1)
    ]
    if len(audits) != len(expected_intervals):
        raise ChoiceSnapshotError("provider-neutral annual query coverage is incomplete")
    validated_audits: list[dict[str, object]] = []
    for index, (raw_audit, (interval_start, interval_end)) in enumerate(
        zip(audits, expected_intervals, strict=True)
    ):
        if not isinstance(raw_audit, Mapping):
            raise ChoiceSnapshotError(f"provider-neutral query audit {index} must be an object")
        audit = dict(cast(Mapping[str, object], raw_audit))
        expected_query = (
            f"查询{spec.symbol} {interval_start.isoformat()}至{interval_end.isoformat()}"
            "每个交易日的前收盘价、交易状态、是否ST、证券简称"
        )
        if (
            audit.get("purpose") != "historical_sessions"
            or audit.get("query") != expected_query
            or audit.get("provider") != provider
            or audit.get("schemaVersion") != "eastmoney-mx.search-data.v1"
            or audit.get("requestedStart") != interval_start.isoformat()
            or audit.get("requestedEnd") != interval_end.isoformat()
            or audit.get("providerFields")
            != ["前收盘价", "交易状态", "是否为ST股票"]
            or not _is_sha256(audit.get("responseSha256"))
        ):
            raise ChoiceSnapshotError(
                f"provider-neutral query audit {index} does not match the strict request"
            )
        retrieved_at = audit.get("retrievedAt")
        if not isinstance(retrieved_at, str):
            raise ChoiceSnapshotError(f"provider-neutral query audit {index} lacks retrieval time")
        try:
            parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
        except ValueError as exc:
            raise ChoiceSnapshotError(
                f"provider-neutral query audit {index} retrieval time is invalid"
            ) from exc
        if parsed_retrieved_at.tzinfo is None or parsed_retrieved_at.utcoffset() is None:
            raise ChoiceSnapshotError(
                f"provider-neutral query audit {index} retrieval time lacks timezone"
            )
        interval_rows = [
            row
            for row in provider_rows
            if interval_start <= date.fromisoformat(row["date"]) <= interval_end
        ]
        if not interval_rows:
            raise ChoiceSnapshotError(
                f"provider-neutral query audit {index} contains no historical rows"
            )
        if (
            audit.get("rowCount") != len(interval_rows)
            or audit.get("returnedStart") != interval_rows[0]["date"]
            or audit.get("returnedEnd") != interval_rows[-1]["date"]
            or audit.get("canonicalRowsSha256") != _canonical_sha256(interval_rows)
            or audit.get("dateAxisSha256")
            != _canonical_sha256([row["date"] for row in interval_rows])
        ):
            raise ChoiceSnapshotError(
                f"provider-neutral query audit {index} row evidence is inconsistent"
            )
        validated_audits.append(audit)
    if coverage.get("aggregateAuditSha256") != _canonical_sha256(validated_audits):
        raise ChoiceSnapshotError("provider-neutral aggregate audit hash is invalid")
    return {
        "schemaVersion": PROVIDER_NEUTRAL_SESSION_REFERENCE_SCHEMA_VERSION,
        "status": "complete",
        "querySucceeded": True,
        "provider": provider,
        "instrumentId": spec.symbol,
        "start": spec.start.isoformat(),
        "end": spec.end.isoformat(),
        "fields": list(SESSION_REFERENCE_FIELDS),
        "providerFields": ["前收盘价", "交易状态", "是否为ST股票"],
        "frequency": "1d",
        "adjustFlag": "provider_unadjusted_preclose",
        "priceBasis": "unadjusted",
        "rowCount": len(provider_rows),
        "zeroResult": not provider_rows,
        "returnedStart": returned_start,
        "returnedEnd": returned_end,
        "canonicalRowsSha256": coverage["canonicalRowsSha256"],
        "dateAxisSha256": coverage["dateAxisSha256"],
        "aggregateAuditSha256": coverage["aggregateAuditSha256"],
        "hashSemantics": coverage["hashSemantics"],
        "paginationPolicy": coverage["paginationPolicy"],
        "queryMethod": coverage["queryMethod"],
        "queryAudits": validated_audits,
    }


def _validate_etf_session_reference_coverage(
    coverage: Mapping[str, object],
    *,
    provider_rows: Sequence[Mapping[str, str]],
    spec: ChoiceSnapshotSpec,
) -> dict[str, object]:
    if spec.board is not Board.STOCK_ETF or not _is_mainland_etf_symbol(spec.symbol):
        raise ChoiceSnapshotError("stock-ETF session evidence does not match the snapshot identity")
    if coverage.get("status") != "complete" or coverage.get("querySucceeded") is not True:
        raise ChoiceSnapshotError("stock-ETF session acquisition must complete successfully")
    if coverage.get("instrumentId") != spec.symbol:
        raise ChoiceSnapshotError("stock-ETF session evidence belongs to another instrument")
    provider = _coverage_text(coverage.get("provider"), "stock-ETF session provider")
    if (
        coverage.get("start") != spec.start.isoformat()
        or coverage.get("end") != spec.end.isoformat()
    ):
        raise ChoiceSnapshotError("stock-ETF session range must exactly match the snapshot")
    if coverage.get("fields") != list(SESSION_REFERENCE_FIELDS):
        raise ChoiceSnapshotError("stock-ETF session fields are not the strict field set")
    if (
        coverage.get("frequency") != "d"
        or coverage.get("adjustFlag") != "0"
        or coverage.get("priceBasis") != "unadjusted"
    ):
        raise ChoiceSnapshotError("stock-ETF session evidence must use unadjusted daily data")
    if coverage.get("rowCount") != len(provider_rows) or coverage.get("zeroResult") is not (
        not provider_rows
    ):
        raise ChoiceSnapshotError("stock-ETF session row metadata is inconsistent")
    returned_start = provider_rows[0]["date"] if provider_rows else None
    returned_end = provider_rows[-1]["date"] if provider_rows else None
    if (
        coverage.get("returnedStart") != returned_start
        or coverage.get("returnedEnd") != returned_end
    ):
        raise ChoiceSnapshotError("stock-ETF session returned range is inconsistent")
    date_axis_digest = _canonical_sha256([row["date"] for row in provider_rows])
    if coverage.get("dateAxisSha256") != date_axis_digest:
        raise ChoiceSnapshotError("stock-ETF session date-axis hash is invalid")
    if coverage.get("calendarPolicy") != (
        "exact Tencent/Sohu trading-row intersection; BaoStock overlap independently "
        "checks provider identity and values"
    ):
        raise ChoiceSnapshotError("stock-ETF calendar policy is missing")

    raw_audits = coverage.get("sourceAudits")
    if not isinstance(raw_audits, Sequence) or isinstance(
        raw_audits,
        str | bytes | bytearray,
    ):
        raise ChoiceSnapshotError("stock-ETF source audits must be a list")
    audits = list(cast(Sequence[object], raw_audits))
    required_purposes = {
        "tencent_unadjusted",
        "tencent_hfq_signal",
        "tencent_qfq_action_reconciliation",
        "tencent_hfq_prefix",
        "sohu_unadjusted_cross_check",
        "baostock_overlap_cross_check",
    }
    observed_purposes: set[str] = set()
    for index, raw_audit in enumerate(audits):
        if not isinstance(raw_audit, Mapping):
            raise ChoiceSnapshotError(f"stock-ETF source audit {index} must be an object")
        audit = cast(Mapping[str, object], raw_audit)
        purpose = _coverage_text(audit.get("purpose"), f"stock-ETF source audit {index} purpose")
        observed_purposes.add(purpose)
        _coverage_text(audit.get("provider"), f"stock-ETF source audit {index} provider")
        _coverage_text(audit.get("url"), f"stock-ETF source audit {index} URL")
        params = audit.get("params")
        if not isinstance(params, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in cast(Mapping[object, object], params).items()
        ):
            raise ChoiceSnapshotError(f"stock-ETF source audit {index} params are invalid")
        for timestamp_field in ("requestedAt", "receivedAt"):
            raw_timestamp = audit.get(timestamp_field)
            if not isinstance(raw_timestamp, str):
                raise ChoiceSnapshotError(
                    f"stock-ETF source audit {index} {timestamp_field} is invalid"
                )
            try:
                parsed_timestamp = datetime.fromisoformat(raw_timestamp)
            except ValueError as exc:
                raise ChoiceSnapshotError(
                    f"stock-ETF source audit {index} {timestamp_field} is invalid"
                ) from exc
            if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                raise ChoiceSnapshotError(
                    f"stock-ETF source audit {index} {timestamp_field} lacks timezone"
                )
        canonical_digest = audit.get("canonicalSha256")
        if not isinstance(canonical_digest, str) or not _is_sha256(canonical_digest):
            raise ChoiceSnapshotError(f"stock-ETF source audit {index} canonical hash is invalid")
        if purpose != "baostock_overlap_cross_check":
            wire_digest = audit.get("rawWireSha256")
            if not isinstance(wire_digest, str) or not _is_sha256(wire_digest):
                raise ChoiceSnapshotError(
                    f"stock-ETF source audit {index} raw wire hash is invalid"
                )
        elif audit.get("wireBytesCaptured") is not False or not isinstance(
            audit.get("wireCaptureReason"), str
        ):
            raise ChoiceSnapshotError("BaoStock SDK audit must disclose its wire-byte boundary")
        row_count = audit.get("rowCount")
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
            raise ChoiceSnapshotError(f"stock-ETF source audit {index} has no evidence rows")
    if observed_purposes != required_purposes:
        raise ChoiceSnapshotError("stock-ETF source audit coverage is incomplete")

    overlap = _coverage_mapping(coverage.get("baostockOverlap"), "baostockOverlap")
    overlap_count = overlap.get("rowCount")
    if isinstance(overlap_count, bool) or not isinstance(overlap_count, int) or overlap_count < 1:
        raise ChoiceSnapshotError("BaoStock overlap must contain real rows")
    if not _is_sha256(overlap.get("canonicalSha256")):
        raise ChoiceSnapshotError("BaoStock overlap canonical hash is invalid")
    return {
        "schemaVersion": ETF_SESSION_REFERENCE_SCHEMA_VERSION,
        "status": "complete",
        "querySucceeded": True,
        "provider": provider,
        "instrumentId": spec.symbol,
        "start": spec.start.isoformat(),
        "end": spec.end.isoformat(),
        "rowCount": len(provider_rows),
        "returnedStart": returned_start,
        "returnedEnd": returned_end,
        "dateAxisSha256": date_axis_digest,
        "queryMethod": "Tencent/Sohu HTTPS plus BaoStock SDK overlap",
        "adjustFlag": "0",
        "calendarPolicy": coverage["calendarPolicy"],
        "sourceAudits": [dict(cast(Mapping[str, object], item)) for item in audits],
        "baostockOverlap": dict(overlap),
    }


def _normalized_baostock_audit_sha256(
    *,
    method: str,
    params: Mapping[str, str],
    fields: Sequence[object],
    rows: Sequence[Mapping[str, str]],
    error_code: str,
    error_message: object,
) -> str:
    if not isinstance(error_message, str):
        raise ChoiceSnapshotError("BaoStock session audit errorMessage must be text")
    body = {
        "method": method,
        "params": dict(sorted(params.items())),
        "fields": sorted(cast(str, item) for item in fields),
        "rows": [dict(row) for row in _sorted_canonical_rows(rows)],
        "errorCode": error_code,
        "errorMessage": error_message,
    }
    return _canonical_sha256(body)


def _sorted_canonical_rows(
    rows: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value.removeprefix("sha256:")) == 64
        and all(character in "0123456789abcdef" for character in value.removeprefix("sha256:"))
    )


def _signal_rows_with_raw_liquidity(
    execution_rows: Sequence[Mapping[str, object]],
    signal_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for execution, signal in zip(execution_rows, signal_rows, strict=True):
        item: dict[str, object] = {
            "stock_code": signal["stock_code"],
            "date": signal["date"],
            "open": signal["open"],
            "high": signal["high"],
            "low": signal["low"],
            "close": signal["close"],
            "volume": execution["volume"],
            "amount": execution["amount"],
        }
        if all(field_name in execution for field_name in _TURNOVER_RATE_FIELDS):
            item.update({field_name: execution[field_name] for field_name in _TURNOVER_RATE_FIELDS})
        output.append(item)
    return output


def _derive_research_sessions(
    spec: ChoiceSnapshotSpec,
    execution_rows: Sequence[Mapping[str, object]],
    session_reference_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    instrument_id = InstrumentId(spec.symbol)
    rule_book = HistoricalAshareRuleBook()
    sessions: list[dict[str, object]] = []
    for index, (row, reference) in enumerate(
        zip(execution_rows, session_reference_rows, strict=True),
        start=1,
    ):
        session_date = cast(date, row["date"])
        previous_close = cast(Decimal, reference["preclose"])
        session = rule_book.build_session(
            PriceLimitRuleInput(
                instrument_id=instrument_id,
                session_date=session_date,
                board=spec.board,
                status=cast(TradingStatus, reference["trading_status"]),
                previous_close=Price(previous_close),
                listing_date=spec.listing_date,
                listing_session_number=max(6, index),
                is_st=cast(bool, reference["is_st"]),
            )
        )
        sessions.append(
            {
                "stock_code": spec.symbol.split(".", maxsplit=1)[0],
                "date": session_date,
                "board": session.board.value,
                "trading_status": session.status.value,
                "previous_close": session.previous_close.amount,
                "upper_limit": (
                    None if session.upper_limit is None else session.upper_limit.amount
                ),
                "lower_limit": (
                    None if session.lower_limit is None else session.lower_limit.amount
                ),
                "minimum_buy_quantity": session.minimum_buy_quantity,
                "buy_quantity_increment": session.buy_quantity_increment,
                "price_tick": session.price_tick,
                "t_plus_one": session.t_plus_one,
                "is_st": session.is_st,
            }
        )
    return sessions


def _write_daily(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    turnover_rate_presence = tuple(
        all(field_name in item for field_name in _TURNOVER_RATE_FIELDS) for item in rows
    )
    if any(turnover_rate_presence) and not all(turnover_rate_presence):
        raise ChoiceSnapshotError("daily snapshot has partial provider turnover-rate provenance")
    includes_turnover_rate = bool(turnover_rate_presence and all(turnover_rate_presence))
    fields = [
        ("stock_code", _ARROW.string()),
        ("date", _ARROW.date32()),
        ("open", _ARROW.float64()),
        ("high", _ARROW.float64()),
        ("low", _ARROW.float64()),
        ("close", _ARROW.float64()),
        ("volume", _ARROW.int64()),
        ("amount", _ARROW.float64()),
    ]
    if includes_turnover_rate:
        fields.extend(
            [
                ("turnover_rate_pct", _ARROW.decimal128(38, 18)),
                ("turnover_rate_provider", _ARROW.string()),
                ("turnover_rate_methodology", _ARROW.string()),
            ]
        )
    schema = _ARROW.schema(fields)
    projected = [
        {
            "stock_code": item["stock_code"],
            "date": item["date"],
            "open": float(cast(Decimal, item["open"])),
            "high": float(cast(Decimal, item["high"])),
            "low": float(cast(Decimal, item["low"])),
            "close": float(cast(Decimal, item["close"])),
            "volume": item["volume"],
            "amount": float(cast(Decimal, item["amount"])),
            **(
                {
                    "turnover_rate_pct": cast(Decimal, item["turnover_rate_pct"]),
                    "turnover_rate_provider": item["turnover_rate_provider"],
                    "turnover_rate_methodology": item["turnover_rate_methodology"],
                }
                if includes_turnover_rate
                else {}
            ),
        }
        for item in rows
    ]
    _PARQUET.write_table(_ARROW.Table.from_pylist(projected, schema=schema), path)


def _write_sessions(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    schema = _ARROW.schema(
        [
            ("stock_code", _ARROW.string()),
            ("date", _ARROW.date32()),
            ("board", _ARROW.string()),
            ("trading_status", _ARROW.string()),
            ("previous_close", _ARROW.decimal128(20, 6)),
            ("upper_limit", _ARROW.decimal128(20, 6)),
            ("lower_limit", _ARROW.decimal128(20, 6)),
            ("minimum_buy_quantity", _ARROW.int32()),
            ("buy_quantity_increment", _ARROW.int32()),
            ("price_tick", _ARROW.decimal128(10, 4)),
            ("t_plus_one", _ARROW.bool_()),
            ("is_st", _ARROW.bool_()),
        ]
    )
    _PARQUET.write_table(_ARROW.Table.from_pylist(list(rows), schema=schema), path)


def _validate_corporate_actions(
    actions: Sequence[CorporateAction],
    *,
    spec: ChoiceSnapshotSpec,
) -> tuple[CorporateAction, ...]:
    canonical = InstrumentId(spec.symbol)
    revision_keys: set[tuple[str, int]] = set()
    source_leg_keys: set[tuple[str, CorporateActionKind]] = set()
    values: list[CorporateAction] = []
    for action in actions:
        if action.instrument_id != canonical:
            raise ChoiceSnapshotError("corporate action belongs to a different instrument")
        if not spec.start <= action.ex_date <= spec.end:
            raise ChoiceSnapshotError("corporate-action ex_date is outside snapshot coverage")
        available_at = action.available_at
        record_close = datetime.combine(
            action.record_date,
            datetime.min.time().replace(hour=15),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        if available_at is None or available_at > record_close:
            raise ChoiceSnapshotError(
                "corporate-action terms must be validated by record-date close"
            )
        key = (action.action_id.value, action.revision_no)
        if key in revision_keys:
            raise ChoiceSnapshotError("duplicate corporate-action revision")
        revision_keys.add(key)
        source_leg_key = (action.source_action_id, action.action_type)
        if source_leg_key in source_leg_keys:
            raise ChoiceSnapshotError("duplicate selected corporate-action source leg")
        source_leg_keys.add(source_leg_key)
        values.append(action)
    return tuple(
        sorted(
            values,
            key=lambda item: (item.ex_date, item.action_id.value, item.revision_no),
        )
    )


def _validate_corporate_action_coverage(
    coverage: Mapping[str, object] | None,
    *,
    spec: ChoiceSnapshotSpec,
    actions: Sequence[CorporateAction],
) -> dict[str, object]:
    action_type_counts = {
        category: sum(action.action_type.value == category for action in actions)
        for category in STRICT_CORPORATE_ACTION_CATEGORIES
    }
    return validate_corporate_action_coverage_evidence(
        coverage,
        start=spec.start,
        end=spec.end,
        action_type_counts=action_type_counts,
        instrument_id=spec.symbol,
    )


def validate_corporate_action_coverage_evidence(
    coverage: Mapping[str, object] | None,
    *,
    start: date,
    end: date,
    action_type_counts: Mapping[str, int],
    instrument_id: str | None = None,
) -> dict[str, object]:
    """Validate the exact corporate-action proof used by producer and loaders.

    ``action_type_counts`` must come from the actual canonical action rows.  A
    consumer therefore cannot promote a manifest merely because two manifest
    fields agree with each other; it must reconcile the immutable Parquet file
    through this same boundary.
    """

    if start > end:
        raise ChoiceSnapshotError("corporate-action validation range is invalid")
    if set(action_type_counts) != set(STRICT_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError(
            "corporate-action row counts must account for every domain category"
        )
    normalized_counts: dict[str, int] = {}
    for category in STRICT_CORPORATE_ACTION_CATEGORIES:
        value = action_type_counts.get(category)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ChoiceSnapshotError("corporate-action row counts must be non-negative integers")
        normalized_counts[category] = value
    if coverage is None:
        raise ChoiceSnapshotError(
            "corporate-action coverage evidence is required; an empty file is not proof"
        )
    if coverage.get("querySucceeded") is not True:
        raise ChoiceSnapshotError("corporate-action source query must complete successfully")
    scope = coverage.get("coverageScope")
    if scope == ETF_CORPORATE_ACTION_COVERAGE_SCOPE:
        return _validate_etf_corporate_action_coverage(
            coverage,
            start=start,
            end=end,
            action_type_counts=normalized_counts,
            instrument_id=instrument_id,
        )
    if scope == MIXED_CORPORATE_ACTION_COVERAGE_SCOPE:
        return _validate_mixed_corporate_action_coverage(
            coverage,
            start=start,
            end=end,
            action_type_counts=normalized_counts,
        )
    if coverage.get("status") != "complete":
        raise ChoiceSnapshotError("corporate-action source query must complete successfully")
    if scope != STRICT_CORPORATE_ACTION_COVERAGE_SCOPE:
        raise ChoiceSnapshotError(
            "strict corporate-action coverage must prove all categories; "
            "supported-categories-only coverage is insufficient"
        )
    supported_categories = _coverage_category_list(
        coverage.get("supportedCategories"),
        "supportedCategories",
    )
    unsupported_categories = _coverage_category_list(
        coverage.get("unsupportedCategories"),
        "unsupportedCategories",
    )
    if unsupported_categories:
        raise ChoiceSnapshotError(
            "strict corporate-action coverage cannot contain unsupported categories"
        )
    if set(supported_categories) != set(STRICT_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError(
            "strict corporate-action coverage must include every domain category"
        )
    provider = coverage.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ChoiceSnapshotError("corporate-action coverage provider is required")
    coverage_start = _as_date(coverage.get("start"), "corporate-action coverage start")
    coverage_end = _as_date(coverage.get("end"), "corporate-action coverage end")
    if coverage_start > start or coverage_end < end:
        raise ChoiceSnapshotError("corporate-action coverage does not span the snapshot range")
    digest = coverage.get("rawResponseSha256")
    if (
        not isinstance(digest, str)
        or len(digest.removeprefix("sha256:")) != 64
        or any(character not in "0123456789abcdef" for character in digest.removeprefix("sha256:"))
    ):
        raise ChoiceSnapshotError("corporate-action coverage requires a raw response hash")
    return {
        "status": "complete",
        "querySucceeded": True,
        "provider": provider.strip(),
        "start": coverage_start.isoformat(),
        "end": coverage_end.isoformat(),
        "rawResponseSha256": digest.removeprefix("sha256:"),
        "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
        "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
        "unsupportedCategories": [],
    }


def _validate_etf_corporate_action_coverage(
    coverage: Mapping[str, object],
    *,
    start: date,
    end: date,
    action_type_counts: Mapping[str, int],
    instrument_id: str | None,
) -> dict[str, object]:
    coverage_instrument = coverage.get("instrumentId")
    if (
        not isinstance(coverage_instrument, str)
        or not _is_mainland_etf_symbol(coverage_instrument)
        or (instrument_id is not None and coverage_instrument != instrument_id)
    ):
        raise ChoiceSnapshotError("stock-ETF action evidence belongs to another instrument")
    if coverage.get("status") != "complete" or coverage.get("instrumentType") != "stock_etf":
        raise ChoiceSnapshotError("stock-ETF corporate-action coverage is incomplete")
    provider, coverage_start, coverage_end, digest = _corporate_action_coverage_identity(
        coverage,
        start=start,
        end=end,
    )
    cash_category = CorporateActionKind.CASH_DIVIDEND.value
    raw_counts = coverage.get("categoryActionCounts")
    if not isinstance(raw_counts, Mapping):
        raise ChoiceSnapshotError("stock-ETF action category counts must be an object")
    expected_counts = cast(Mapping[object, object], raw_counts)
    for category in STRICT_CORPORATE_ACTION_CATEGORIES:
        if expected_counts.get(category) != action_type_counts[category]:
            raise ChoiceSnapshotError("stock-ETF action category count does not match actions")
        if category != cash_category and action_type_counts[category] != 0:
            raise ChoiceSnapshotError("stock-ETF proof only permits official cash distributions")
    row_count = action_type_counts[cash_category]
    if coverage.get("rowCount") != row_count or coverage.get("zeroResult") is not (row_count == 0):
        raise ChoiceSnapshotError("stock-ETF action row metadata is inconsistent")
    if coverage.get("officialNoticeCount") != row_count:
        raise ChoiceSnapshotError("stock-ETF official distribution coverage is incomplete")

    raw_notices = coverage.get("officialNotices")
    if not isinstance(raw_notices, Sequence) or isinstance(
        raw_notices,
        str | bytes | bytearray,
    ):
        raise ChoiceSnapshotError("stock-ETF official notices must be a list")
    notices: list[dict[str, object]] = []
    ex_dates: list[str] = []
    for index, raw_notice in enumerate(cast(Sequence[object], raw_notices)):
        if not isinstance(raw_notice, Mapping):
            raise ChoiceSnapshotError(f"stock-ETF official notice {index} must be an object")
        notice = dict(cast(Mapping[str, object], raw_notice))
        url = notice.get("url")
        if not isinstance(url, str):
            raise ChoiceSnapshotError(f"stock-ETF official notice {index} URL is invalid")
        parsed = urlparse(url)
        parsed_url = PurePosixPath(parsed.path)
        expected_code = coverage_instrument.split(".", maxsplit=1)[0]
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.sse.com.cn"
            or not coverage_instrument.endswith(".SH")
            or f"{expected_code}_" not in parsed_url.name
            or parsed_url.suffix != ".pdf"
        ):
            raise ChoiceSnapshotError(f"stock-ETF official notice {index} is not an SSE PDF")
        for digest_field in ("rawPdfSha256", "extractedTextSha256"):
            if not _is_sha256(notice.get(digest_field)):
                raise ChoiceSnapshotError(
                    f"stock-ETF official notice {index} {digest_field} is invalid"
                )
        announced = _as_date(notice.get("announcedDate"), "ETF notice announcement date")
        record = _as_date(notice.get("recordDate"), "ETF notice record date")
        ex_date = _as_date(notice.get("exDate"), "ETF notice ex-date")
        pay = _as_date(notice.get("payDate"), "ETF notice pay date")
        if not announced < record < ex_date <= pay or not start <= ex_date <= end:
            raise ChoiceSnapshotError("stock-ETF official notice dates are inconsistent")
        amount = _positive_decimal(
            notice.get("grossCashPerUnit"),
            "ETF notice gross cash per unit",
        )
        notice["grossCashPerUnit"] = str(amount)
        ex_dates.append(ex_date.isoformat())
        notices.append(notice)
    if len(ex_dates) != len(set(ex_dates)) or len(notices) != row_count:
        raise ChoiceSnapshotError("stock-ETF official notice coverage has duplicate ex-dates")

    reconciliation = _coverage_mapping(
        coverage.get("adjustmentReconciliation"),
        "adjustmentReconciliation",
    )
    reconciliation_rows = reconciliation.get("rowCount")
    if (
        reconciliation.get("status") != "passed"
        or isinstance(reconciliation_rows, bool)
        or not isinstance(reconciliation_rows, int)
        or reconciliation_rows < row_count
        or reconciliation.get("officialExDates") != ex_dates
        or reconciliation.get("unmatchedAdjustmentTransitions") != 0
        or reconciliation.get("unitSplitCandidates") != 0
        or reconciliation.get("unitConsolidationCandidates") != 0
        or not _is_sha256(reconciliation.get("offsetRowsSha256"))
    ):
        raise ChoiceSnapshotError("stock-ETF adjustment reconciliation is incomplete")
    identity = {
        "notices": notices,
        "adjustmentReconciliation": dict(reconciliation),
    }
    if digest != _canonical_sha256(identity):
        raise ChoiceSnapshotError("stock-ETF corporate-action aggregate hash is invalid")
    if coverage.get("notApplicableCategories") != [
        CorporateActionKind.RIGHTS_ISSUE.value,
        CorporateActionKind.SHARE_DISTRIBUTION.value,
    ]:
        raise ChoiceSnapshotError("stock-ETF inapplicable action categories are not explicit")
    return {
        "status": "complete",
        "querySucceeded": True,
        "provider": provider,
        "instrumentId": coverage_instrument,
        "instrumentType": "stock_etf",
        "start": coverage_start.isoformat(),
        "end": coverage_end.isoformat(),
        "rowCount": row_count,
        "zeroResult": row_count == 0,
        "rawResponseSha256": digest,
        "coverageScope": ETF_CORPORATE_ACTION_COVERAGE_SCOPE,
        "categoryActionCounts": dict(action_type_counts),
        "officialNoticeCount": row_count,
        "officialNotices": notices,
        "adjustmentReconciliation": dict(reconciliation),
        "notApplicableCategories": list(cast(Sequence[str], coverage["notApplicableCategories"])),
        "timeQuality": coverage.get("timeQuality"),
        "dateAvailabilityPolicy": coverage.get("dateAvailabilityPolicy"),
        "hashSemantics": coverage.get("hashSemantics"),
    }


def _is_mainland_etf_symbol(symbol: str) -> bool:
    """Recognize an explicit ETF code space without conferring executability."""

    return re.fullmatch(r"(?:5\d{5}\.SH|1\d{5}\.SZ)", symbol) is not None


def _validate_mixed_corporate_action_coverage(
    coverage: Mapping[str, object],
    *,
    start: date,
    end: date,
    action_type_counts: Mapping[str, int],
) -> dict[str, object]:
    if coverage.get("status") != "complete_mixed_mode":
        raise ChoiceSnapshotError("mixed corporate-action coverage status must be complete")
    positive = _coverage_category_list(
        coverage.get("positiveCapableCategories"),
        "positiveCapableCategories",
    )
    negative = _coverage_category_list(
        coverage.get("negativeProofCategories"),
        "negativeProofCategories",
    )
    unsupported = _coverage_category_list(
        coverage.get("unsupportedCategories"),
        "unsupportedCategories",
    )
    if set(positive) != set(MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError(
            "mixed corporate-action positive-capable categories are incomplete"
        )
    if set(negative) != set(MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError("mixed corporate-action negative-proof categories are incomplete")
    if unsupported or set(positive) | set(negative) != set(STRICT_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError("mixed corporate-action coverage must account for every category")
    if coverage.get("strictEligibleUnderCurrentChoiceValidator") is not True:
        raise ChoiceSnapshotError(
            "mixed corporate-action evidence was not promoted by its producer"
        )

    provider, coverage_start, coverage_end, digest = _corporate_action_coverage_identity(
        coverage,
        start=start,
        end=end,
    )
    row_count = sum(action_type_counts.values())
    if coverage.get("rowCount") != row_count:
        raise ChoiceSnapshotError("corporate-action coverage row count does not match actions")
    if coverage.get("zeroResult") is not (row_count == 0):
        raise ChoiceSnapshotError("corporate-action coverage zero-result flag is inconsistent")

    raw_counts = _coverage_mapping(
        coverage.get("categoryActionCounts"),
        "categoryActionCounts",
    )
    if set(raw_counts) != set(STRICT_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError("corporate-action category counts must cover every category")
    counts: dict[str, int] = {}
    for category in STRICT_CORPORATE_ACTION_CATEGORIES:
        value = raw_counts.get(category)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ChoiceSnapshotError(
                "corporate-action category counts must be non-negative integers"
            )
        if value != action_type_counts[category]:
            raise ChoiceSnapshotError("corporate-action category count does not match actions")
        counts[category] = value
    if any(counts[category] for category in MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError(
            "split actions cannot be promoted from a complete-negative-proof source"
        )

    raw_category_coverage = _coverage_mapping(
        coverage.get("categoryCoverage"),
        "categoryCoverage",
    )
    if set(raw_category_coverage) != set(STRICT_CORPORATE_ACTION_CATEGORIES):
        raise ChoiceSnapshotError("corporate-action category evidence must cover every category")
    category_coverage: dict[str, dict[str, object]] = {}
    for category in MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES:
        item = _coverage_mapping(raw_category_coverage.get(category), category)
        dataset = _coverage_text(item.get("dataset"), f"{category} dataset")
        if item.get("status") != "complete_for_filtered_dataset":
            raise ChoiceSnapshotError(f"{category} positive coverage is incomplete")
        if item.get("zeroResult") is not (counts[category] == 0):
            raise ChoiceSnapshotError(f"{category} zero-result evidence is inconsistent")
        category_coverage[category] = {
            "status": "complete_for_filtered_dataset",
            "dataset": dataset,
            "zeroResult": counts[category] == 0,
        }
    for category in MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES:
        item = _coverage_mapping(raw_category_coverage.get(category), category)
        dataset = _coverage_text(item.get("dataset"), f"{category} dataset")
        if (
            item.get("status") != "complete"
            or item.get("categoryMode") != "complete_negative_proof"
            or item.get("candidateCount") != 0
            or item.get("zeroResult") is not True
        ):
            raise ChoiceSnapshotError(f"{category} negative proof is incomplete")
        category_coverage[category] = {
            "status": "complete",
            "categoryMode": "complete_negative_proof",
            "dataset": dataset,
            "candidateCount": 0,
            "zeroResult": True,
        }

    negative_proof = _validate_negative_split_coverage(
        coverage,
        start=start,
        end=end,
    )
    return {
        "status": "complete_mixed_mode",
        "querySucceeded": True,
        "provider": provider,
        "start": coverage_start.isoformat(),
        "end": coverage_end.isoformat(),
        "rowCount": row_count,
        "zeroResult": row_count == 0,
        "rawResponseSha256": digest,
        "coverageScope": MIXED_CORPORATE_ACTION_COVERAGE_SCOPE,
        "positiveCapableCategories": list(MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES),
        "negativeProofCategories": list(MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES),
        "unsupportedCategories": [],
        "strictEligibleUnderCurrentChoiceValidator": True,
        "categoryActionCounts": counts,
        "categoryCoverage": category_coverage,
        "negativeSplitProof": negative_proof,
        "timeQuality": coverage.get("timeQuality"),
        "dateAvailabilityPolicy": coverage.get("dateAvailabilityPolicy"),
        "hashSemantics": coverage.get("hashSemantics"),
    }


def _corporate_action_coverage_identity(
    coverage: Mapping[str, object],
    *,
    start: date,
    end: date,
) -> tuple[str, date, date, str]:
    provider = _coverage_text(coverage.get("provider"), "provider")
    coverage_start = _as_date(coverage.get("start"), "corporate-action coverage start")
    coverage_end = _as_date(coverage.get("end"), "corporate-action coverage end")
    if coverage_start > start or coverage_end < end:
        raise ChoiceSnapshotError("corporate-action coverage does not span the snapshot range")
    digest = coverage.get("rawResponseSha256")
    normalized = digest.removeprefix("sha256:") if isinstance(digest, str) else ""
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ChoiceSnapshotError("corporate-action coverage requires a raw response hash")
    return provider, coverage_start, coverage_end, normalized


def _validate_negative_split_coverage(
    coverage: Mapping[str, object],
    *,
    start: date,
    end: date,
) -> dict[str, object]:
    proof = _coverage_mapping(coverage.get("negativeSplitProof"), "negativeSplitProof")
    proof_start = _as_date(proof.get("start"), "negative split proof start")
    proof_end = _as_date(proof.get("end"), "negative split proof end")
    if proof_start > start or proof_end < end:
        raise ChoiceSnapshotError("negative split proof does not span the snapshot range")
    pages = proof.get("sourceTotalPages")
    declared_count = proof.get("sourceDeclaredCount")
    scanned_rows = proof.get("scannedRows")
    if (
        proof.get("categoryMode") != "complete_negative_proof"
        or proof.get("queryScope")
        != "full_instrument_history_filtered_locally_to_requested_interval"
        or isinstance(pages, bool)
        or not isinstance(pages, int)
        or pages < 1
        or isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count < 1
        or isinstance(scanned_rows, bool)
        or not isinstance(scanned_rows, int)
        or scanned_rows < 0
        or proof.get("stockSplitCandidates") != 0
        or proof.get("reverseSplitCandidates") != 0
    ):
        raise ChoiceSnapshotError("negative split proof is incomplete")
    dataset = _coverage_text(proof.get("sourceDataset"), "negative split source dataset")
    digest = proof.get("sourceRawResponseSha256")
    normalized = digest.removeprefix("sha256:") if isinstance(digest, str) else ""
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ChoiceSnapshotError("negative split proof requires a raw response hash")
    reasons = _coverage_category_list(
        proof.get("recognizedChangeReasons"),
        "negativeSplitProof.recognizedChangeReasons",
    )
    return {
        "categoryMode": "complete_negative_proof",
        "queryScope": "full_instrument_history_filtered_locally_to_requested_interval",
        "start": proof_start.isoformat(),
        "end": proof_end.isoformat(),
        "scannedRows": scanned_rows,
        "recognizedChangeReasons": list(reasons),
        "stockSplitCandidates": 0,
        "reverseSplitCandidates": 0,
        "sourceDataset": dataset,
        "sourceDeclaredCount": declared_count,
        "sourceTotalPages": pages,
        "sourceRawResponseSha256": normalized,
    }


def _coverage_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChoiceSnapshotError(f"corporate-action coverage {field_name} must be an object")
    return cast(Mapping[str, object], value)


def _coverage_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChoiceSnapshotError(f"corporate-action coverage {field_name} must contain text")
    return value.strip()


def _coverage_category_list(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ChoiceSnapshotError(f"corporate-action coverage {field_name} must be a list")
    categories: list[str] = []
    for item in cast(Sequence[object], value):
        if not isinstance(item, str) or not item.strip():
            raise ChoiceSnapshotError(f"corporate-action coverage {field_name} must contain text")
        categories.append(item.strip())
    if len(categories) != len(set(categories)):
        raise ChoiceSnapshotError(f"corporate-action coverage {field_name} contains duplicates")
    return tuple(categories)


def _write_corporate_actions(
    path: Path,
    actions: Sequence[CorporateAction],
) -> None:
    schema = _ARROW.schema(
        [
            ("stock_code", _ARROW.string()),
            ("action_id", _ARROW.string()),
            ("source_action_id", _ARROW.string()),
            ("action_type", _ARROW.string()),
            ("record_date", _ARROW.date32()),
            ("ex_date", _ARROW.date32()),
            ("source_released_at", _ARROW.string()),
            ("vendor_first_available_at", _ARROW.string()),
            ("ingested_at", _ARROW.string()),
            ("replay_available_at", _ARROW.string()),
            ("revision_no", _ARROW.int32()),
            ("time_quality", _ARROW.string()),
            ("provider", _ARROW.string()),
            ("source_url", _ARROW.string()),
            ("raw_response_sha256", _ARROW.string()),
            ("validation_status", _ARROW.string()),
            ("currency", _ARROW.string()),
            ("gross_cash_per_share", _ARROW.decimal128(20, 8)),
            ("cash_pay_date", _ARROW.date32()),
            ("share_multiplier", _ARROW.decimal128(20, 8)),
            ("share_credit_date", _ARROW.date32()),
            ("share_sellable_date", _ARROW.date32()),
            ("rights_ratio", _ARROW.decimal128(20, 8)),
            ("rights_subscription_price", _ARROW.decimal128(20, 8)),
            ("rights_payment_deadline", _ARROW.date32()),
            ("rights_listing_date", _ARROW.date32()),
        ]
    )
    rows = [
        {
            "stock_code": action.instrument_id.value.split(".", maxsplit=1)[0],
            "action_id": action.action_id.value,
            "source_action_id": action.source_action_id,
            "action_type": action.action_type.value,
            "record_date": action.record_date,
            "ex_date": action.ex_date,
            "source_released_at": _optional_iso(action.source_released_at),
            "vendor_first_available_at": _optional_iso(action.vendor_first_available_at),
            "ingested_at": action.ingested_at.isoformat(),
            "replay_available_at": action.replay_available_at.isoformat(),
            "revision_no": action.revision_no,
            "time_quality": action.time_quality.value,
            "provider": action.provider,
            "source_url": action.source_url,
            "raw_response_sha256": action.raw_response_sha256.removeprefix("sha256:"),
            "validation_status": action.validation_status,
            "currency": action.currency,
            "gross_cash_per_share": action.gross_cash_per_share,
            "cash_pay_date": action.cash_pay_date,
            "share_multiplier": action.share_multiplier,
            "share_credit_date": action.share_credit_date,
            "share_sellable_date": action.share_sellable_date,
            "rights_ratio": action.rights_ratio,
            "rights_subscription_price": action.rights_subscription_price,
            "rights_payment_deadline": action.rights_payment_deadline,
            "rights_listing_date": action.rights_listing_date,
        }
        for action in actions
    ]
    _PARQUET.write_table(_ARROW.Table.from_pylist(rows, schema=schema), path)


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )


def _json_default(value: object) -> str:
    if isinstance(value, date | datetime | Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")


def _validate_published_snapshot(
    snapshot_path: Path,
    manifest: Mapping[str, object],
    *,
    snapshot_prefix: str = "choice",
) -> None:
    snapshot_id = manifest.get("snapshotId")
    manifest_body = {key: value for key, value in manifest.items() if key != "snapshotId"}
    expected_digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
    if (
        snapshot_id != f"{snapshot_prefix}:{expected_digest}"
        or snapshot_path.name != expected_digest
    ):
        raise ChoiceSnapshotError("published snapshot identity does not match its manifest")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, Mapping):
        raise ChoiceSnapshotError("published snapshot manifest files must be an object")
    root = snapshot_path.resolve()
    for raw_relative, raw_metadata in cast(Mapping[object, object], raw_files).items():
        if not isinstance(raw_relative, str) or not raw_relative:
            raise ChoiceSnapshotError("published snapshot manifest contains an invalid file path")
        relative = Path(raw_relative)
        declared_path = snapshot_path / relative
        candidate = declared_path.resolve()
        if (
            relative.is_absolute()
            or not candidate.is_relative_to(root)
            or declared_path.is_symlink()
        ):
            raise ChoiceSnapshotError("published snapshot manifest contains an unsafe file path")
        if not isinstance(raw_metadata, Mapping):
            raise ChoiceSnapshotError("published snapshot file metadata must be an object")
        metadata = cast(Mapping[object, object], raw_metadata)
        expected_size = metadata.get("bytes")
        expected_sha256 = metadata.get("sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
        ):
            raise ChoiceSnapshotError("published snapshot file metadata is invalid")
        if not candidate.is_file():
            raise ChoiceSnapshotError(f"published snapshot file is missing: {raw_relative}")
        if candidate.stat().st_size != expected_size or _sha256_file(candidate) != expected_sha256:
            raise ChoiceSnapshotError(f"published snapshot file hash mismatch: {raw_relative}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_secret_keys(value: object, *, path: str = "root") -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            name = str(key)
            lowered = name.lower()
            if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
                raise ChoiceSnapshotError(
                    f"audit payload contains a forbidden secret key: {path}.{name}"
                )
            _reject_secret_keys(item, path=f"{path}.{name}")
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence = cast(Sequence[object], value)
        for index, item in enumerate(sequence):
            _reject_secret_keys(item, path=f"{path}[{index}]")


def _as_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parts = value[:10].replace("/", "-").split("-")
            if len(parts) != 3:
                raise ValueError
            return date(*(int(part) for part in parts))
        except (TypeError, ValueError) as exc:
            raise ChoiceSnapshotError(f"{field_name} is not an ISO date") from exc
    raise ChoiceSnapshotError(f"{field_name} is not a date")


def _decimal(value: object, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ChoiceSnapshotError(f"{field_name} must be numeric")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChoiceSnapshotError(f"{field_name} must be numeric") from exc
    if not converted.is_finite():
        raise ChoiceSnapshotError(f"{field_name} must be finite")
    return converted


def _positive_decimal(value: object, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted <= 0:
        raise ChoiceSnapshotError(f"{field_name} must be positive")
    return converted


def _non_negative_decimal(value: object, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted < 0:
        raise ChoiceSnapshotError(f"{field_name} must be non-negative")
    return converted


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChoiceSnapshotError(f"{field_name} must be non-empty text")
    return value.strip()


def _non_negative_integer(value: object, field_name: str) -> int:
    converted = _non_negative_decimal(value, field_name)
    integral = converted.to_integral_value()
    if converted != integral:
        raise ChoiceSnapshotError(f"{field_name} must be a whole number")
    return int(integral)


def _positive_integer(value: object, field_name: str) -> int:
    converted = _non_negative_integer(value, field_name)
    if converted <= 0:
        raise ChoiceSnapshotError(f"{field_name} must be positive")
    return converted


def _choice_trading_status(value: object, index: int) -> TradingStatus:
    if not isinstance(value, str) or not value.strip():
        raise ChoiceSnapshotError(f"row {index} tradestatus must be non-empty text")
    normalized = value.strip()
    if normalized == _CHOICE_TRADING_STATUS:
        return TradingStatus.TRADING
    if normalized in _CHOICE_SUSPENDED_STATUSES:
        return TradingStatus.SUSPENDED
    raise ChoiceSnapshotError(f"row {index} has an unverified Choice tradestatus: {normalized}")


def _suspended_liquidity(
    value: object,
    *,
    index: int,
    field_name: str,
    integer: bool,
) -> int | Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0 if integer else Decimal(0)
    converted = _non_negative_decimal(value, f"row {index} suspended {field_name}")
    if converted != 0:
        raise ChoiceSnapshotError(f"row {index} suspended {field_name} must be null or zero")
    if integer:
        if converted != converted.to_integral_value():
            raise ChoiceSnapshotError(f"row {index} suspended {field_name} must be a whole number")
        return int(converted)
    return converted


def _limit_flag(value: object, index: int, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() not in {"是", "否"}:
        raise ChoiceSnapshotError(f"row {index} {field_name} must be 是 or 否")
    return value.strip()
