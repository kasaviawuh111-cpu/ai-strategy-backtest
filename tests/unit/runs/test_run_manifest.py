from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import DataSnapshotRef
from ashare_lab.domain.runs import (
    ExecutionAssumptions,
    RunManifest,
    result_hash,
)
from ashare_lab.domain.shared import RunId, StrongId

TZ = ZoneInfo("Asia/Shanghai")
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def manifest(*, run_id: str = "run-1", created_minute: int = 0) -> RunManifest:
    return RunManifest(
        run_id=RunId(run_id),
        strategy_hash=HASH_A,
        catalog_hash=HASH_B,
        config_hash="sha256:" + "d" * 64,
        data_snapshot=DataSnapshotRef(
            snapshot_id=StrongId("snapshot-1"),
            checksum="sha256:" + "c" * 64,
            schema_version="market-data.v1",
            created_at=datetime(2025, 1, 1, 0, 0, tzinfo=TZ),
            producer_schema_version="producer-snapshot.v2",
            producer_snapshot_id="composite:" + "1" * 64,
        ),
        strategy_schema_version="strategy.v1",
        engine_version="2.0.0a0",
        code_revision="git:abc123",
        period_start=date(2021, 1, 1),
        period_end=date(2025, 1, 1),
        initial_cash_cny="100000.00",
        assumptions=ExecutionAssumptions(
            resolution="1d",
            price_limit_mode="wait_for_unlock",
            participation_rate="0.05",
            slippage_bps="5",
            commission_rate="0.0003",
            minimum_commission_cny="5.00",
            fee_schedule_version="cn_a.fees.v1",
            market_rule_version="cn_a.daily_market_rules.fallback.v2",
        ),
        created_at=datetime(2025, 1, 1, 0, created_minute, tzinfo=TZ),
    )


def test_fingerprint_ignores_run_identity_and_creation_clock() -> None:
    first = manifest(run_id="run-1", created_minute=0)
    retry = manifest(run_id="run-2", created_minute=1)

    assert first.fingerprint == retry.fingerprint


def test_fingerprint_changes_when_an_execution_assumption_changes() -> None:
    first = manifest()
    changed = replace(
        first,
        assumptions=ExecutionAssumptions(**{**first.assumptions.as_dict(), "slippage_bps": "10"}),
    )

    assert first.fingerprint != changed.fingerprint


def test_fingerprint_changes_when_capacity_mode_changes() -> None:
    first = manifest()
    changed = replace(
        first,
        assumptions=ExecutionAssumptions(
            **{**first.assumptions.as_dict(), "capacity_mode": "unlimited"}
        ),
    )

    assert first.fingerprint != changed.fingerprint


def test_fingerprint_changes_when_producer_snapshot_schema_changes() -> None:
    first = manifest()
    changed = replace(
        first,
        data_snapshot=replace(
            first.data_snapshot,
            producer_schema_version="producer-snapshot.v3",
        ),
    )

    assert first.fingerprint != changed.fingerprint


def test_fingerprint_changes_when_producer_snapshot_content_id_changes() -> None:
    first = manifest()
    changed = replace(
        first,
        data_snapshot=replace(
            first.data_snapshot,
            producer_snapshot_id="composite:" + "2" * 64,
        ),
    )

    assert first.fingerprint != changed.fingerprint


def test_fingerprint_changes_when_the_full_run_config_changes() -> None:
    first = manifest()
    changed = replace(first, config_hash="sha256:" + "e" * 64)

    assert first.fingerprint != changed.fingerprint


def test_result_hash_is_canonical() -> None:
    assert result_hash({"return": "0.123", "trades": 2}) == result_hash(
        {"trades": 2, "return": "0.123"}
    )
