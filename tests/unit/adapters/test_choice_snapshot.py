from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from ashare_lab.adapters.market_data import (
    LocalParquetMarketDataRepository,
    MarketDataCapabilityError,
    ParquetInstrumentSessionProvider,
    SnapshotIntegrityError,
    SnapshotScopeError,
)
from ashare_lab.adapters.market_data.choice_snapshot import (
    CHOICE_DATA_INTEGRITY_EXIT_CODE,
    CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
    MIXED_CORPORATE_ACTION_COVERAGE_SCOPE,
    MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES,
    MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES,
    STRICT_CORPORATE_ACTION_CATEGORIES,
    STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotError,
    ChoiceSnapshotSpec,
    DailySnapshotSource,
    build_choice_snapshot,
)
from ashare_lab.domain.market_data import Board, PriceBasis, TradingStatus
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange
from scripts import prepare_choice_snapshot
from scripts.prepare_choice_snapshot import (
    CALENDAR_OPTIONS,
    ChoiceProviderUnavailableError,
    ChoiceResponseValidationError,
    _annual_intervals,
    _calendar_requests,
    _read_with_retry,
    _start_with_retry,
)


class _FakeEmQuantData:
    def __init__(self, error_code: int, error_message: str = "") -> None:
        self.ErrorCode = error_code
        self.ErrorMsg = error_message
        self.Codes: list[object] = []
        self.Indicators: list[object] = []
        self.Dates: list[object] = []
        self.Data: object = {}


class _FakeChoiceClient:
    EmQuantData = _FakeEmQuantData


class _SuccessfulChoiceClient:
    EmQuantData = _FakeEmQuantData

    def start(self, _options: str, _callback: object) -> _FakeEmQuantData:
        return _FakeEmQuantData(0)

    def tradedates(self, _start: str, _end: str, _options: str) -> object:
        result = _FakeEmQuantData(0)
        result.Data = {"dates": ["2025-01-02"]}
        return result

    def csd(
        self,
        symbol: str,
        indicators: str,
        _start: str,
        _end: str,
        _options: str,
    ) -> object:
        result = _FakeEmQuantData(0)
        result.Codes = [symbol]
        result.Indicators = indicators.split(",")
        result.Dates = ["2025-01-02"]
        result.Data = {symbol: [[1] for _ in result.Indicators]}
        return result

    def stop(self) -> None:
        return None


class _UnavailableChoiceClient(_SuccessfulChoiceClient):
    def tradedates(self, _start: str, _end: str, _options: str) -> object:
        raise OSError("native SDK network socket closed")


def _install_choice_cli_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: object,
) -> None:
    args = SimpleNamespace(
        symbol="300059.SZ",
        start=date(2025, 1, 2),
        end=date(2025, 1, 4),
        prefix_end=date(2025, 1, 3),
        listing_date=date(2010, 3, 19),
        board=Board.CHINEXT.value,
        output_root=tmp_path / "choice",
        session_reference_json=tmp_path / "sessions.json",
        corporate_actions_json=tmp_path / "actions.json",
        sdk_archive=tmp_path / "sdk.zip",
    )
    monkeypatch.setattr(prepare_choice_snapshot, "_parse_args", lambda: args)
    monkeypatch.setattr(
        prepare_choice_snapshot,
        "_load_session_reference_source",
        lambda *_args, **_kwargs: (
            (),
            {"provider": "fixture", "normalizedResponseSha256": "a" * 64},
            {},
        ),
    )
    monkeypatch.setattr(
        prepare_choice_snapshot,
        "_load_corporate_action_source",
        lambda *_args, **_kwargs: ((), {"provider": "fixture"}, {}),
    )
    sdk_module = ModuleType("EmQuantAPI")
    sdk_module.c = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "EmQuantAPI", sdk_module)


def _execution_rows() -> list[dict[str, object]]:
    return [
        {
            "date": "2025/1/2",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "preclose": 9.8,
            "volume": 1000,
            "amount": 10_500,
            "highlimit": "否",
            "lowlimit": "否",
            "tradestatus": "正常交易",
        },
        {
            "date": "2025-01-03",
            "open": 10.6,
            "high": 11.2,
            "low": 10.1,
            "close": 11,
            "preclose": 10.5,
            "volume": 1200,
            "amount": 13_200,
            "highlimit": "否",
            "lowlimit": "否",
            "tradestatus": "正常交易",
        },
    ]


def test_choice_calendar_requests_are_bounded_by_calendar_year() -> None:
    intervals = _annual_intervals(date(2021, 8, 6), date(2026, 8, 6))

    assert len(intervals) == 6
    assert intervals[:2] == (
        (date(2021, 8, 6), date(2021, 12, 31)),
        (date(2022, 1, 1), date(2022, 12, 31)),
    )
    assert intervals[-1] == (date(2026, 1, 1), date(2026, 8, 6))
    assert all((end - start).days + 1 <= 366 for start, end in intervals)
    assert all(
        right_start == left_end.replace(year=left_end.year + 1, month=1, day=1)
        for (_, left_end), (right_start, _) in pairwise(intervals)
    )


def test_choice_calendar_request_has_explicit_receive_timeout() -> None:
    requests = _calendar_requests(date(2021, 8, 6), date(2026, 8, 6))

    assert len(requests) == 6
    assert {options for _, _, options in requests} == {"Market=CNSESH,RECVtimeout=30"}
    assert requests[0][2] == CALENDAR_OPTIONS


def test_choice_idempotent_read_retries_explicit_network_disconnect_then_succeeds() -> None:
    results = [
        _FakeEmQuantData(10002004, "network connection closed when recv"),
        _FakeEmQuantData(0),
    ]
    calls = 0
    sleeps: list[float] = []

    def read() -> _FakeEmQuantData:
        nonlocal calls
        result = results[calls]
        calls += 1
        return result

    result, attempts = _read_with_retry(
        read,
        "market calendar 2021-03-01..2021-03-31",
        _FakeChoiceClient,
        sleeper=sleeps.append,
    )

    assert result.ErrorCode == 0
    assert attempts == 2
    assert calls == 2
    assert sleeps == [0.25]


def test_choice_idempotent_read_classifies_permission_as_provider_unavailable() -> None:
    calls = 0
    sleeps: list[float] = []

    def read() -> _FakeEmQuantData:
        nonlocal calls
        calls += 1
        return _FakeEmQuantData(10000017, "overseas ip is restricted")

    with pytest.raises(
        ChoiceProviderUnavailableError,
        match="10000017 overseas ip is restricted",
    ):
        _read_with_retry(
            read,
            "unadjusted daily series",
            _FakeChoiceClient,
            sleeper=sleeps.append,
        )

    assert calls == 1
    assert sleeps == []


def test_choice_idempotent_read_stops_after_three_transient_attempts() -> None:
    calls = 0
    sleeps: list[float] = []

    def read() -> _FakeEmQuantData:
        nonlocal calls
        calls += 1
        return _FakeEmQuantData(10002004, "network connection closed when recv")

    with pytest.raises(
        ChoiceProviderUnavailableError,
        match="10002004 network connection closed when recv",
    ):
        _read_with_retry(
            read,
            "back-adjusted prefix series",
            _FakeChoiceClient,
            sleeper=sleeps.append,
        )

    assert calls == 3
    assert sleeps == [0.25, 0.75]


def test_choice_idempotent_read_rejects_non_provider_results_without_retry() -> None:
    calls = 0

    def read() -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(ChoiceResponseValidationError, match="returned object"):
        _read_with_retry(
            read,
            "market calendar",
            _FakeChoiceClient,
            sleeper=lambda _: pytest.fail("non-provider result must not be retried"),
        )

    assert calls == 1


def test_choice_idempotent_read_does_not_retry_sdk_exceptions() -> None:
    calls = 0

    def read() -> _FakeEmQuantData:
        nonlocal calls
        calls += 1
        raise ConnectionError("native SDK connection failed")

    with pytest.raises(ConnectionError, match="native SDK connection failed"):
        _read_with_retry(
            read,
            "market calendar",
            _FakeChoiceClient,
            sleeper=lambda _: pytest.fail("SDK exceptions must fail fast"),
        )

    assert calls == 1


@pytest.mark.parametrize("error_code", [10002002, 10002004])
def test_choice_login_retries_only_explicit_transient_network_codes(error_code: int) -> None:
    results = [
        _FakeEmQuantData(error_code, "network unavailable"),
        _FakeEmQuantData(0),
    ]
    calls = 0
    sleeps: list[float] = []

    def start() -> _FakeEmQuantData:
        nonlocal calls
        result = results[calls]
        calls += 1
        return result

    result, attempts = _start_with_retry(
        start,
        _FakeChoiceClient,
        sleeper=sleeps.append,
    )

    assert result.ErrorCode == 0
    assert attempts == 2
    assert calls == 2
    assert sleeps == [0.25]


def test_choice_login_does_not_retry_permission_errors() -> None:
    calls = 0

    def start() -> _FakeEmQuantData:
        nonlocal calls
        calls += 1
        return _FakeEmQuantData(10000017, "overseas ip is restricted")

    with pytest.raises(
        ChoiceProviderUnavailableError,
        match="10000017 overseas ip is restricted",
    ):
        _start_with_retry(
            start,
            _FakeChoiceClient,
            sleeper=lambda _: pytest.fail("permission errors must fail fast"),
        )

    assert calls == 1


@pytest.mark.parametrize("error_code", [10001014, 10001020])
def test_choice_login_activation_errors_allow_the_server_owned_fallback(
    error_code: int,
) -> None:
    """A local Choice activation gap is availability, never bad market data."""

    with pytest.raises(ChoiceProviderUnavailableError, match=str(error_code)):
        _start_with_retry(
            lambda: _FakeEmQuantData(error_code, "activation required"),
            _FakeChoiceClient,
            sleeper=lambda _: pytest.fail("activation errors must not be retried"),
        )


def test_choice_login_stops_after_three_transient_attempts() -> None:
    calls = 0
    sleeps: list[float] = []

    def start() -> _FakeEmQuantData:
        nonlocal calls
        calls += 1
        return _FakeEmQuantData(10002002, "network connect failure")

    with pytest.raises(
        ChoiceProviderUnavailableError,
        match="10002002 network connect failure",
    ):
        _start_with_retry(
            start,
            _FakeChoiceClient,
            sleeper=sleeps.append,
        )

    assert calls == 3
    assert sleeps == [0.25, 0.75]


def test_choice_non_availability_provider_error_is_integrity_failure() -> None:
    with pytest.raises(
        ChoiceResponseValidationError,
        match="10004006 invalid security code",
    ):
        _read_with_retry(
            lambda: _FakeEmQuantData(10004006, "invalid security code"),
            "unadjusted daily series",
            _FakeChoiceClient,
            sleeper=lambda _: pytest.fail("data errors must not retry or fall back"),
        )


def test_choice_login_does_not_retry_sdk_exceptions() -> None:
    calls = 0

    def start() -> _FakeEmQuantData:
        nonlocal calls
        calls += 1
        raise ConnectionError("native SDK login failed")

    with pytest.raises(ConnectionError, match="native SDK login failed"):
        _start_with_retry(
            start,
            _FakeChoiceClient,
            sleeper=lambda _: pytest.fail("SDK exceptions must fail fast"),
        )

    assert calls == 1


def test_choice_cli_local_snapshot_oserror_is_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_choice_cli_fakes(monkeypatch, tmp_path, _SuccessfulChoiceClient())

    def fail_local_snapshot_write(**_kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(
        prepare_choice_snapshot,
        "build_choice_snapshot",
        fail_local_snapshot_write,
    )

    exit_code = prepare_choice_snapshot.main()

    assert exit_code == CHOICE_DATA_INTEGRITY_EXIT_CODE
    assert "failed integrity validation: OSError: disk full" in capsys.readouterr().out


def test_choice_cli_sdk_oserror_is_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_choice_cli_fakes(monkeypatch, tmp_path, _UnavailableChoiceClient())
    monkeypatch.setattr(
        prepare_choice_snapshot,
        "build_choice_snapshot",
        lambda **_kwargs: pytest.fail("local snapshot build must not run"),
    )

    exit_code = prepare_choice_snapshot.main()

    assert exit_code == CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE
    assert "provider became unavailable" in capsys.readouterr().out


def _signal_rows() -> list[dict[str, object]]:
    return [
        {"date": "2025/1/2", "open": 20, "high": 22, "low": 18, "close": 21},
        {
            "date": "2025-01-03",
            "open": 21.2,
            "high": 22.4,
            "low": 20.2,
            "close": 22,
        },
    ]


def _spec() -> ChoiceSnapshotSpec:
    return ChoiceSnapshotSpec(
        symbol="300059.SZ",
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        listing_date=date(2010, 3, 19),
        board=Board.CHINEXT,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _session_reference_rows() -> list[dict[str, object]]:
    return [
        {
            "date": "2025-01-02",
            "preclose": "9.8",
            "tradestatus": "1",
            "isST": "0",
        },
        {
            "date": "2025-01-03",
            "preclose": "10.5",
            "tradestatus": "1",
            "isST": "0",
        },
    ]


def _session_reference_coverage(
    rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_rows = rows or _session_reference_rows()
    params = {
        "code": "sz.300059",
        "fields": "date,preclose,tradestatus,isST",
        "start_date": "2025-01-02",
        "end_date": "2025-01-03",
        "frequency": "d",
        "adjustflag": "3",
    }
    canonical_rows = sorted(
        (dict(row) for row in source_rows),
        key=lambda row: json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    audit_body = {
        "method": "query_history_k_data_plus",
        "params": params,
        "fields": sorted(("date", "preclose", "tradestatus", "isST")),
        "rows": canonical_rows,
        "errorCode": "0",
        "errorMessage": "success",
    }
    audit = {
        **audit_body,
        "rowCount": len(canonical_rows),
        "zeroResult": not canonical_rows,
        "normalizedResponseSha256": _canonical_sha256(audit_body),
    }
    return {
        "status": "complete",
        "querySucceeded": True,
        "provider": "BaoStock Python API",
        "instrumentId": "300059.SZ",
        "start": "2025-01-02",
        "end": "2025-01-03",
        "fields": ["date", "preclose", "tradestatus", "isST"],
        "frequency": "d",
        "adjustFlag": "3",
        "priceBasis": "unadjusted",
        "rowCount": len(source_rows),
        "zeroResult": not source_rows,
        "returnedStart": source_rows[0]["date"] if source_rows else None,
        "returnedEnd": source_rows[-1]["date"] if source_rows else None,
        "normalizedResponseSha256": _canonical_sha256([audit]),
        "canonicalRowsSha256": _canonical_sha256(source_rows),
        "hashSemantics": "sha256_of_canonical_normalized_results_not_raw_wire_bytes",
        "paginationPolicy": "annual_queries_bounded_to_at_most_366_calendar_days",
        "intervals": [
            {
                "start": "2025-01-02",
                "end": "2025-01-03",
                "rowCount": len(source_rows),
                "zeroResult": not source_rows,
                "normalizedResponseSha256": audit["normalizedResponseSha256"],
            }
        ],
        "queryAudits": [audit],
    }


def _provider_neutral_session_reference_coverage() -> dict[str, object]:
    rows = _session_reference_rows()
    audit = {
        "purpose": "historical_sessions",
        "query": (
            "查询300059.SZ 2025-01-02至2025-01-03"
            "每个交易日的前收盘价、交易状态、是否ST、证券简称"
        ),
        "provider": "eastmoney_mx_finance_data",
        "schemaVersion": "eastmoney-mx.search-data.v1",
        "responseSha256": "sha256:" + "c" * 64,
        "retrievedAt": "2025-01-04T00:00:00+00:00",
        "requestedStart": "2025-01-02",
        "requestedEnd": "2025-01-03",
        "returnedStart": "2025-01-02",
        "returnedEnd": "2025-01-03",
        "rowCount": 2,
        "providerFields": ["前收盘价", "交易状态", "是否为ST股票"],
        "canonicalRowsSha256": _canonical_sha256(rows),
        "dateAxisSha256": _canonical_sha256([row["date"] for row in rows]),
    }
    return {
        "schemaVersion": "ashare-lab.instrument-session-reference.v3",
        "status": "complete",
        "querySucceeded": True,
        "provider": "eastmoney_mx_finance_data",
        "instrumentId": "300059.SZ",
        "start": "2025-01-02",
        "end": "2025-01-03",
        "fields": ["date", "preclose", "tradestatus", "isST"],
        "providerFields": ["前收盘价", "交易状态", "是否为ST股票"],
        "frequency": "1d",
        "adjustFlag": "provider_unadjusted_preclose",
        "priceBasis": "unadjusted",
        "rowCount": 2,
        "zeroResult": False,
        "returnedStart": "2025-01-02",
        "returnedEnd": "2025-01-03",
        "canonicalRowsSha256": _canonical_sha256(rows),
        "dateAxisSha256": _canonical_sha256([row["date"] for row in rows]),
        "aggregateAuditSha256": _canonical_sha256([audit]),
        "hashSemantics": (
            "provider_raw_wire_sha256_plus_canonical_normalized_rows_sha256"
        ),
        "paginationPolicy": (
            "annual_searchData_queries_bounded_to_at_most_366_calendar_days"
        ),
        "queryMethod": "Eastmoney MX searchData annual exact-symbol daily facts",
        "queryAudits": [audit],
    }


def _build(tmp_path: Path, **overrides):
    values = {
        "spec": _spec(),
        "execution_rows": _execution_rows(),
        "signal_rows": _signal_rows(),
        "market_calendar": [date(2025, 1, 2), date(2025, 1, 3)],
        "raw_audit_payload": {"responses": ["fixture"]},
        "request_audit": {"AdjustFlag": [1, 2]},
        "prefix_stability": {"status": "passed", "overlapRows": 1},
        "output_root": tmp_path,
        "captured_at": datetime(2025, 1, 4, tzinfo=UTC),
        "sdk_archive_sha256": "a" * 64,
        "session_reference_rows": _session_reference_rows(),
        "session_reference_coverage": _session_reference_coverage(),
        "corporate_action_coverage": {
            "status": "complete",
            "querySucceeded": True,
            "provider": "fixture-source",
            "start": "2025-01-02",
            "end": "2025-01-03",
            "rawResponseSha256": "b" * 64,
            "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
            "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
            "unsupportedCategories": [],
        },
    }
    values.update(overrides)
    return build_choice_snapshot(**values)


def _mixed_action_coverage() -> dict[str, object]:
    counts = {category: 0 for category in STRICT_CORPORATE_ACTION_CATEGORIES}
    return {
        "status": "complete_mixed_mode",
        "querySucceeded": True,
        "provider": "eastmoney_public_corporate_action_reference",
        "start": "2025-01-02",
        "end": "2025-01-03",
        "rowCount": 0,
        "zeroResult": True,
        "rawResponseSha256": "b" * 64,
        "coverageScope": MIXED_CORPORATE_ACTION_COVERAGE_SCOPE,
        "positiveCapableCategories": list(MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES),
        "negativeProofCategories": list(MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES),
        "unsupportedCategories": [],
        "strictEligibleUnderCurrentChoiceValidator": True,
        "categoryActionCounts": counts,
        "categoryCoverage": {
            **{
                category: {
                    "status": "complete_for_filtered_dataset",
                    "dataset": "fixture-positive-dataset",
                    "zeroResult": True,
                }
                for category in MIXED_POSITIVE_CORPORATE_ACTION_CATEGORIES
            },
            **{
                category: {
                    "status": "complete",
                    "categoryMode": "complete_negative_proof",
                    "dataset": "RPT_F10_EH_EQUITY full instrument history",
                    "candidateCount": 0,
                    "zeroResult": True,
                }
                for category in MIXED_NEGATIVE_PROOF_CORPORATE_ACTION_CATEGORIES
            },
        },
        "negativeSplitProof": {
            "categoryMode": "complete_negative_proof",
            "queryScope": "full_instrument_history_filtered_locally_to_requested_interval",
            "start": "2025-01-02",
            "end": "2025-01-03",
            "scannedRows": 2,
            "recognizedChangeReasons": ["高管股份变动"],
            "stockSplitCandidates": 0,
            "reverseSplitCandidates": 0,
            "sourceDataset": "RPT_F10_EH_EQUITY",
            "sourceDeclaredCount": 2,
            "sourceTotalPages": 1,
            "sourceRawResponseSha256": "c" * 64,
        },
        "timeQuality": "date_only_conservative",
        "dateAvailabilityPolicy": "implementation notice date @ 15:00:00 Asia/Shanghai",
        "hashSemantics": "fixture",
    }


def test_builds_content_addressed_dual_price_snapshot(tmp_path: Path) -> None:
    result = _build(tmp_path)

    assert result.snapshot_id.startswith("choice:")
    assert (result.path / "daily_ohlcv.parquet").is_file()
    assert (result.path / "signal_daily_ohlcv.parquet").is_file()
    assert (result.path / "instrument_sessions.parquet").is_file()
    assert (result.path / "corporate_actions.parquet").is_file()
    assert (result.path / "raw/provider_response.json").is_file()
    manifest = json.loads((result.path / "snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["accountScope"] == "personal_research_demo"
    assert manifest["capabilities"]["events"] == "unavailable_current_account"
    assert manifest["corporateActionCoverage"]["querySucceeded"] is True
    assert manifest["corporateActionCoverage"]["coverageScope"] == (
        STRICT_CORPORATE_ACTION_COVERAGE_SCOPE
    )
    assert manifest["corporateActionCoverage"]["supportedCategories"] == list(
        STRICT_CORPORATE_ACTION_CATEGORIES
    )
    assert manifest["corporateActionCoverage"]["unsupportedCategories"] == []
    assert manifest["sessionReference"]["kind"] == (
        "baostock-historical-facts-plus-versioned-rulebook"
    )
    assert manifest["sessionReference"]["coverage"]["rowCount"] == 2
    assert "historicalStAssumption" not in manifest["sessionReference"]
    assert all("never-ST" not in item for item in manifest["limitations"])

    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(InstrumentId("300059.SZ"),),
            datasets=("daily_ohlcv",),
        ),
        period,
    )
    execution = repository.load_daily_bars(snapshot, InstrumentId("300059.SZ"), period)
    signal = repository.load_signal_bars(snapshot, InstrumentId("300059.SZ"), period)

    assert execution[0].price_basis is PriceBasis.UNADJUSTED
    assert signal[0].price_basis is PriceBasis.BACK_ADJUSTED
    assert execution[0].close.amount != signal[0].close.amount
    assert execution[0].volume == signal[0].volume


def test_provider_neutral_v3_session_evidence_is_preserved_in_manifest(
    tmp_path: Path,
) -> None:
    result = _build(
        tmp_path,
        session_reference_coverage=_provider_neutral_session_reference_coverage(),
    )

    manifest = json.loads((result.path / "snapshot_manifest.json").read_text(encoding="utf-8"))
    reference = manifest["sessionReference"]
    assert reference["kind"] == (
        "provider-neutral-historical-facts-plus-versioned-rulebook"
    )
    assert reference["provider"] == "eastmoney_mx_finance_data"
    assert reference["adjustFlag"] == "provider_unadjusted_preclose"
    assert reference["coverage"]["queryAudits"][0]["responseSha256"] == (
        "sha256:" + "c" * 64
    )
    assert reference["priceLimitSource"] == (
        "derived from ruleVersion using provider-neutral isST/preclose"
    )


def test_snapshot_preserves_provider_reported_turnover_rate_for_both_price_bases(
    tmp_path: Path,
) -> None:
    execution_rows = _execution_rows()
    for row, rate in zip(
        execution_rows,
        (Decimal("2.500000000000000001"), Decimal("3.25")),
        strict=True,
    ):
        row["turnover_rate_pct"] = rate
        row["turnover_rate_provider"] = "eastmoney_push2his_public"
        row["turnover_rate_methodology"] = (
            "eastmoney_push2his.f61.provider_reported_turnover_rate_pct.v1"
        )

    result = _build(tmp_path, execution_rows=execution_rows)
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(InstrumentId("300059.SZ"),),
            datasets=("daily_ohlcv",),
        ),
        period,
    )

    execution = repository.load_daily_bars(snapshot, InstrumentId("300059.SZ"), period)
    signal = repository.load_signal_bars(snapshot, InstrumentId("300059.SZ"), period)

    assert [bar.turnover_rate_pct for bar in execution] == [
        Decimal("2.500000000000000001"),
        Decimal("3.25"),
    ]
    assert [bar.turnover_rate_pct for bar in signal] == [
        Decimal("2.500000000000000001"),
        Decimal("3.25"),
    ]
    assert all(bar.turnover_rate_provider == "eastmoney_push2his_public" for bar in signal)


def test_provider_neutral_snapshot_declares_real_source_and_omits_choice_flags(
    tmp_path: Path,
) -> None:
    execution = [
        {key: value for key, value in row.items() if key not in {"highlimit", "lowlimit"}}
        for row in _execution_rows()
    ]
    source = DailySnapshotSource(
        schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
        snapshot_prefix="technical",
        provider="Eastmoney Push2His public endpoint",
        dataset="stock_kline_day",
        account_scope="public_undocumented_research_demo",
        adjustment_field="eastmoneyFqt",
        execution_adjustment=0,
        signal_adjustment=2,
        acquisition_implementation="AKShare-compatible Push2His thin adapter",
        limit_event_flags_available=False,
        status_cross_check="BaoStock supplies and validates historical trading status",
        previous_close_cross_check="Push2 implied preclose equals BaoStock per date",
        limitations=("public undocumented endpoint; not authorized for production use",),
    )

    result = _build(
        tmp_path,
        execution_rows=execution,
        source=source,
        sdk_archive_sha256=None,
    )

    assert result.snapshot_id.startswith("technical:")
    assert result.manifest["schemaVersion"] == TECHNICAL_SNAPSHOT_SCHEMA_VERSION
    assert result.manifest["provider"] == "Eastmoney Push2His public endpoint"
    assert result.manifest["sourceDataset"] == "stock_kline_day"
    price_bases = result.manifest["priceBases"]
    assert isinstance(price_bases, dict)
    assert price_bases["daily_ohlcv.parquet"]["eastmoneyFqt"] == 0
    assert price_bases["signal_daily_ohlcv.parquet"]["eastmoneyFqt"] == 2


def test_same_snapshot_inputs_are_idempotent(tmp_path: Path) -> None:
    first = _build(tmp_path)
    second = _build(tmp_path)

    assert second.snapshot_id == first.snapshot_id
    assert second.path == first.path


def test_accepts_eastmoney_mixed_positive_and_complete_negative_proof(
    tmp_path: Path,
) -> None:
    result = _build(tmp_path, corporate_action_coverage=_mixed_action_coverage())

    manifest = json.loads((result.path / "snapshot_manifest.json").read_text(encoding="utf-8"))
    coverage = manifest["corporateActionCoverage"]
    assert coverage["status"] == "complete_mixed_mode"
    assert coverage["coverageScope"] == MIXED_CORPORATE_ACTION_COVERAGE_SCOPE
    assert coverage["negativeSplitProof"]["sourceTotalPages"] == 1

    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(InstrumentId("300059.SZ"),),
            datasets=("daily_ohlcv", "corporate_actions"),
        ),
        period,
    )
    assert (
        repository.load_corporate_actions(
            snapshot,
            InstrumentId("300059.SZ"),
            period,
        )
        == ()
    )


def test_choice_profile_rejects_slice_outside_producer_requested_range(
    tmp_path: Path,
) -> None:
    result = _build(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")

    with pytest.raises(SnapshotScopeError, match="outside Choice snapshot coverage"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv", "corporate_actions"),
            ),
            DateRange(date(2025, 1, 1), date(2025, 1, 3)),
        )


def test_rejects_mixed_coverage_when_category_counts_do_not_match_actions(
    tmp_path: Path,
) -> None:
    coverage = _mixed_action_coverage()
    counts = coverage["categoryActionCounts"]
    assert isinstance(counts, dict)
    counts["cash_dividend"] = 1

    with pytest.raises(ChoiceSnapshotError, match="category count does not match"):
        _build(tmp_path, corporate_action_coverage=coverage)


def test_existing_snapshot_is_revalidated_before_reuse(tmp_path: Path) -> None:
    first = _build(tmp_path)
    (first.path / "daily_ohlcv.parquet").write_bytes(b"corrupt")

    with pytest.raises(ChoiceSnapshotError, match="file hash mismatch"):
        _build(tmp_path)


def test_engine_pin_detects_producer_manifest_change(tmp_path: Path) -> None:
    result = _build(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(InstrumentId("300059.SZ"),),
            datasets=("daily_ohlcv",),
        ),
        period,
    )
    manifest_path = result.path / "snapshot_manifest.json"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(SnapshotIntegrityError, match="changed"):
        repository.load_daily_bars(snapshot, InstrumentId("300059.SZ"), period)


@pytest.mark.parametrize(
    "filename",
    ["signal_daily_ohlcv.parquet", "snapshot_manifest.json"],
)
def test_choice_profile_rejects_missing_signal_or_manifest_before_pin(
    tmp_path: Path,
    filename: str,
) -> None:
    result = _build(tmp_path)
    (result.path / filename).unlink()
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))

    with pytest.raises(MarketDataCapabilityError, match=filename):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv",),
            ),
            period,
        )


def test_choice_profile_rejects_malformed_manifest_before_pin(tmp_path: Path) -> None:
    result = _build(tmp_path)
    (result.path / "snapshot_manifest.json").write_text("{", encoding="utf-8")
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")

    with pytest.raises(SnapshotIntegrityError, match="valid JSON"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv",),
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_choice_profile_rejects_manifest_snapshot_id_mismatch(tmp_path: Path) -> None:
    result = _build(tmp_path)
    manifest_path = result.path / "snapshot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["snapshotId"] = "choice:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")

    with pytest.raises(SnapshotIntegrityError, match="snapshotId"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv",),
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_choice_profile_rejects_manifest_schema_mismatch(tmp_path: Path) -> None:
    result = _build(tmp_path)
    manifest_path = result.path / "snapshot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schemaVersion"] = "choice.daily-research-snapshot.v999"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")

    with pytest.raises(SnapshotIntegrityError, match="schemaVersion"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv",),
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_choice_profile_rejects_manifest_digest_directory_mismatch(tmp_path: Path) -> None:
    result = _build(tmp_path)
    manifest_path = result.path / "snapshot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    body = {key: value for key, value in manifest.items() if key != "snapshotId"}
    body["accountScope"] = "different-research-scope"
    digest = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(
        json.dumps({"snapshotId": f"choice:{digest}", **body}),
        encoding="utf-8",
    )
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")

    with pytest.raises(SnapshotIntegrityError, match="directory"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv",),
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_choice_profile_rejects_manifest_file_hash_mismatch(tmp_path: Path) -> None:
    result = _build(tmp_path)
    (result.path / "raw/provider_response.json").write_text("{}\n", encoding="utf-8")
    repository = LocalParquetMarketDataRepository(result.path, profile="choice_snapshot")

    with pytest.raises(SnapshotIntegrityError, match="file hash mismatch"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("daily_ohlcv",),
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_rejects_misaligned_adjusted_series(tmp_path: Path) -> None:
    with pytest.raises(ChoiceSnapshotError, match="align one-to-one"):
        _build(tmp_path, signal_rows=_signal_rows()[:1])


def test_rejects_failed_adjustment_prefix_stability(tmp_path: Path) -> None:
    with pytest.raises(ChoiceSnapshotError, match="prefix-stability"):
        _build(tmp_path, prefix_stability={"status": "failed"})


def test_rejects_secret_material_in_audit_payload(tmp_path: Path) -> None:
    with pytest.raises(ChoiceSnapshotError, match="forbidden secret key"):
        _build(tmp_path, raw_audit_payload={"userInfo": "must-not-be-saved"})


def test_rejects_supported_categories_only_coverage_even_if_status_says_complete(
    tmp_path: Path,
) -> None:
    with pytest.raises(ChoiceSnapshotError, match="supported-categories-only"):
        _build(
            tmp_path,
            corporate_action_coverage={
                "status": "complete",
                "querySucceeded": True,
                "provider": "BaoStock Python API",
                "start": "2025-01-02",
                "end": "2025-01-03",
                "rawResponseSha256": "b" * 64,
                "coverageScope": "complete_for_supported_categories_only",
                "supportedCategories": ["cash_dividend", "share_distribution"],
                "unsupportedCategories": [
                    "rights_issue",
                    "stock_split",
                    "reverse_split",
                ],
            },
        )


def test_rejects_legacy_complete_coverage_without_category_proof(tmp_path: Path) -> None:
    with pytest.raises(ChoiceSnapshotError, match="prove all categories"):
        _build(
            tmp_path,
            corporate_action_coverage={
                "status": "complete",
                "querySucceeded": True,
                "provider": "legacy-fixture-source",
                "start": "2025-01-02",
                "end": "2025-01-03",
                "rawResponseSha256": "b" * 64,
            },
        )


def test_rejects_strict_scope_with_any_unsupported_category(tmp_path: Path) -> None:
    with pytest.raises(ChoiceSnapshotError, match="unsupported categories"):
        _build(
            tmp_path,
            corporate_action_coverage={
                "status": "complete",
                "querySucceeded": True,
                "provider": "fixture-source",
                "start": "2025-01-02",
                "end": "2025-01-03",
                "rawResponseSha256": "b" * 64,
                "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
                "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
                "unsupportedCategories": ["rights_issue"],
            },
        )


def test_rejects_strict_scope_missing_a_domain_category(tmp_path: Path) -> None:
    with pytest.raises(ChoiceSnapshotError, match="every domain category"):
        _build(
            tmp_path,
            corporate_action_coverage={
                "status": "complete",
                "querySucceeded": True,
                "provider": "fixture-source",
                "start": "2025-01-02",
                "end": "2025-01-03",
                "rawResponseSha256": "b" * 64,
                "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
                "supportedCategories": [
                    category
                    for category in STRICT_CORPORATE_ACTION_CATEGORIES
                    if category != "reverse_split"
                ],
                "unsupportedCategories": [],
            },
        )


def test_builds_suspended_st_session_from_cross_checked_provider_facts(
    tmp_path: Path,
) -> None:
    execution = _execution_rows()
    execution[1].update(
        {
            "open": 10.5,
            "high": 10.5,
            "low": 10.5,
            "close": 10.5,
            "volume": None,
            "amount": None,
            "tradestatus": "连续停牌",
        }
    )
    reference = _session_reference_rows()
    reference[1].update({"tradestatus": "0", "isST": "1"})

    result = _build(
        tmp_path,
        execution_rows=execution,
        session_reference_rows=reference,
        session_reference_coverage=_session_reference_coverage(reference),
    )

    sessions = ParquetInstrumentSessionProvider(
        result.path / "instrument_sessions.parquet"
    ).sessions_for_period(
        InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    assert sessions[0].status is TradingStatus.TRADING
    assert sessions[0].is_st is False
    assert sessions[1].status is TradingStatus.SUSPENDED
    assert sessions[1].is_st is True


def test_rejects_missing_session_reference_date(tmp_path: Path) -> None:
    reference = _session_reference_rows()[:1]

    with pytest.raises(ChoiceSnapshotError, match="must exactly match Choice"):
        _build(
            tmp_path,
            session_reference_rows=reference,
            session_reference_coverage=_session_reference_coverage(reference),
        )


def test_rejects_duplicate_session_reference_date(tmp_path: Path) -> None:
    reference = _session_reference_rows()
    reference[1]["date"] = reference[0]["date"]

    with pytest.raises(ChoiceSnapshotError, match="duplicate dates"):
        _build(
            tmp_path,
            session_reference_rows=reference,
            session_reference_coverage=_session_reference_coverage(reference),
        )


def test_rejects_cross_provider_preclose_mismatch(tmp_path: Path) -> None:
    reference = _session_reference_rows()
    reference[1]["preclose"] = "10.51"

    with pytest.raises(ChoiceSnapshotError, match="preclose disagree"):
        _build(
            tmp_path,
            session_reference_rows=reference,
            session_reference_coverage=_session_reference_coverage(reference),
        )


def test_rejects_cross_provider_trading_status_mismatch(tmp_path: Path) -> None:
    reference = _session_reference_rows()
    reference[1]["tradestatus"] = "0"

    with pytest.raises(ChoiceSnapshotError, match="trading status disagree"):
        _build(
            tmp_path,
            session_reference_rows=reference,
            session_reference_coverage=_session_reference_coverage(reference),
        )


@pytest.mark.parametrize(
    ("field_name", "bad_value", "message"),
    [
        ("preclose", "", "preclose must be numeric"),
        ("tradestatus", "2", "tradestatus must be 0 or 1"),
        ("isST", "", "isST must be 0 or 1"),
    ],
)
def test_rejects_malformed_session_reference_fields(
    tmp_path: Path,
    field_name: str,
    bad_value: object,
    message: str,
) -> None:
    reference = _session_reference_rows()
    reference[0][field_name] = bad_value

    with pytest.raises(ChoiceSnapshotError, match=message):
        _build(
            tmp_path,
            session_reference_rows=reference,
            session_reference_coverage=_session_reference_coverage(reference),
        )


def test_rejects_unverified_choice_trade_status(tmp_path: Path) -> None:
    execution = _execution_rows()
    execution[1]["tradestatus"] = "未知停牌状态"

    with pytest.raises(ChoiceSnapshotError, match="unverified Choice tradestatus"):
        _build(tmp_path, execution_rows=execution)


def test_rejects_trading_status_with_zero_volume(tmp_path: Path) -> None:
    execution = _execution_rows()
    execution[1]["volume"] = 0

    with pytest.raises(ChoiceSnapshotError, match="trading volume must be positive"):
        _build(tmp_path, execution_rows=execution)


def test_rejects_tampered_session_query_hash(tmp_path: Path) -> None:
    coverage = _session_reference_coverage()
    coverage["normalizedResponseSha256"] = "0" * 64

    with pytest.raises(ChoiceSnapshotError, match="aggregate query hash"):
        _build(tmp_path, session_reference_coverage=coverage)
