from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data.choice_minute_snapshot import (
    ChoiceMinuteSnapshotError,
    ChoiceMinuteSnapshotSpec,
    build_choice_minute_snapshot,
)
from ashare_lab.adapters.market_data.local_parquet import (
    LocalParquetMarketDataRepository,
    SnapshotIntegrityError,
)
from ashare_lab.domain.market_data import PriceBasis
from ashare_lab.domain.shared import DomainValidationError, InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange

SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_DATE = date(2025, 1, 2)


def _session_starts() -> list[datetime]:
    starts: list[datetime] = []
    current = datetime.combine(SESSION_DATE, time(9, 30), tzinfo=SHANGHAI)
    while current < datetime.combine(SESSION_DATE, time(11, 30), tzinfo=SHANGHAI):
        starts.append(current)
        current += timedelta(minutes=1)
    current = datetime.combine(SESSION_DATE, time(13, 0), tzinfo=SHANGHAI)
    while current < datetime.combine(SESSION_DATE, time(15, 0), tzinfo=SHANGHAI):
        starts.append(current)
        current += timedelta(minutes=1)
    return starts


def _execution_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, start in enumerate(_session_starts()):
        price = 10 + index / 1000
        rows.append(
            {
                "timestamp": start + timedelta(minutes=1),
                "open": price,
                "high": price + 0.02,
                "low": price - 0.02,
                "close": price + 0.01,
                "volume": 100 + index,
                "amount": (100 + index) * (price + 0.01),
            }
        )
    return rows


def _signal_rows() -> list[dict[str, object]]:
    return [
        {
            "timestamp": start + timedelta(minutes=1),
            "close": 20 + index / 500,
        }
        for index, start in enumerate(_session_starts())
    ]


def _build(tmp_path: Path, **overrides):
    values = {
        "spec": ChoiceMinuteSnapshotSpec(
            symbol="300059.SZ",
            start=SESSION_DATE,
            end=SESSION_DATE,
            timestamp_semantics="bar_end",
        ),
        "execution_rows": _execution_rows(),
        "signal_rows": _signal_rows(),
        "market_calendar": [SESSION_DATE],
        "raw_response_chunks": [
            {
                "lane": "fixture",
                "decodedResponseSha256": "a" * 64,
                "result": {"errorCode": 0},
            }
        ],
        "request_audit": {"function": "cmc", "mode": "batch"},
        "daily_reconciliation": {"status": "passed", "sessionCount": 1},
        "prefix_stability": {"status": "passed", "overlapRows": 240},
        "output_root": tmp_path,
        "captured_at": datetime(2025, 1, 3, tzinfo=UTC),
        "sdk_archive_sha256": "b" * 64,
    }
    values.update(overrides)
    return build_choice_minute_snapshot(**values)


def test_builds_and_loads_content_addressed_minute_snapshot(tmp_path: Path) -> None:
    result = _build(tmp_path)
    assert result.snapshot_id.startswith("choice-minute:")
    assert (result.path / "minute_ohlcv.parquet").is_file()
    assert (result.path / "signal_minute_close.parquet").is_file()
    assert (result.path / "raw/responses/00000.json").is_file()

    repository = LocalParquetMarketDataRepository(
        result.path,
        profile="choice_minute_snapshot",
    )
    period = DateRange(SESSION_DATE, SESSION_DATE)
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(InstrumentId("300059.SZ"),),
            datasets=("minute_ohlcv",),
            needs_minute=True,
        ),
        period,
    )
    bars = repository.load_minute_bars(snapshot, InstrumentId("300059.SZ"), period)
    signals = repository.load_signal_minute_closes(
        snapshot,
        InstrumentId("300059.SZ"),
        period,
    )

    assert len(bars) == len(signals) == 240
    assert bars[0].bar_start_at == datetime(2025, 1, 2, 9, 30, tzinfo=SHANGHAI)
    assert bars[0].bar_end_at == bars[0].available_at
    assert bars[0].price_basis is PriceBasis.UNADJUSTED
    assert signals[0].price_basis is PriceBasis.BACK_ADJUSTED
    with pytest.raises(DomainValidationError, match="must be unadjusted"):
        replace(bars[0], price_basis=PriceBasis.BACK_ADJUSTED)


def test_same_minute_inputs_are_idempotent(tmp_path: Path) -> None:
    first = _build(tmp_path)
    second = _build(tmp_path)
    assert first.snapshot_id == second.snapshot_id
    assert first.path == second.path


def test_rejects_incomplete_or_misaligned_minute_axes(tmp_path: Path) -> None:
    with pytest.raises(ChoiceMinuteSnapshotError, match="align one-to-one"):
        _build(tmp_path, signal_rows=_signal_rows()[:-1])

    with pytest.raises(ChoiceMinuteSnapshotError, match="incomplete"):
        _build(
            tmp_path,
            execution_rows=_execution_rows()[:-1],
            signal_rows=_signal_rows()[:-1],
        )


def test_rejects_unproven_gates_and_secret_audit_material(tmp_path: Path) -> None:
    with pytest.raises(ChoiceMinuteSnapshotError, match="cannot precede"):
        _build(tmp_path, captured_at=datetime(2020, 1, 1, tzinfo=UTC))
    with pytest.raises(ChoiceMinuteSnapshotError, match="daily reconciliation"):
        _build(
            tmp_path,
            daily_reconciliation={"status": "failed", "sessionCount": 1},
        )
    with pytest.raises(ChoiceMinuteSnapshotError, match="complete session"):
        _build(tmp_path, prefix_stability={"status": "passed", "overlapRows": 1})
    with pytest.raises(ChoiceMinuteSnapshotError, match="forbidden secret key"):
        _build(tmp_path, raw_response_chunks=[{"userInfo": "must-not-be-saved"}])


def test_repository_rejects_tampered_registered_minute_file(tmp_path: Path) -> None:
    result = _build(tmp_path)
    manifest = json.loads((result.path / "snapshot_manifest.json").read_text())
    assert manifest["coverage"]["missingBars"] == 0
    (result.path / "minute_ohlcv.parquet").write_bytes(b"corrupt")

    repository = LocalParquetMarketDataRepository(
        result.path,
        profile="choice_minute_snapshot",
    )
    with pytest.raises(SnapshotIntegrityError, match="file hash mismatch"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId("300059.SZ"),),
                datasets=("minute_ohlcv",),
                needs_minute=True,
            ),
            DateRange(SESSION_DATE, SESSION_DATE),
        )


def test_rebuild_rejects_a_tampered_existing_manifest(tmp_path: Path) -> None:
    result = _build(tmp_path)
    manifest_path = result.path / "snapshot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"]["technicalMinute"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ChoiceMinuteSnapshotError, match="differs from this build"):
        _build(tmp_path)
