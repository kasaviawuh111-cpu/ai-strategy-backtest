from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
    ProviderIndicatorCacheMissError,
)
from ashare_lab.application.backtest_submission import (
    BacktestRunConfig,
    BacktestSubmissionService,
    SubmissionVersions,
    _effective_warmup_calendar_days,
)
from ashare_lab.domain.market_data import DataSnapshotRef
from ashare_lab.domain.shared import StrongId
from ashare_lab.domain.signals import binding_for_provider_condition
from ashare_lab.domain.strategy import IndicatorCondition, StrategySpec
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestRunRecord,
    CreateRunResult,
)
from ashare_lab.ports.market_data import DataRequirements, DateRange
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)

_HASH = "sha256:" + "a" * 64
_NOW = datetime(2026, 9, 5, tzinfo=UTC)


class _SnapshotOnlyMarketData:
    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        del requirements, period
        return DataSnapshotRef(
            snapshot_id=StrongId("snapshot:provider-cache-test"),
            checksum=_HASH,
            schema_version="test.snapshot.v1",
            created_at=_NOW,
        )


class _MemoryStore:
    def __init__(self) -> None:
        self.by_fingerprint: dict[str, BacktestRunRecord] = {}

    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult:
        existing = self.by_fingerprint.get(record.fingerprint)
        if existing is not None:
            return CreateRunResult(existing, True)
        self.by_fingerprint[record.fingerprint] = record
        return CreateRunResult(record, False)


class _Queue:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    def enqueue(self, run_id: object) -> str:
        self.run_ids.append(str(run_id))
        return "job-provider-cache-test"


class _SeedProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def query_indicator_history(
        self,
        *,
        instrument_id: str,
        indicator_id: str,
        provider_indicator_name: str,
        value_names: tuple[str, ...],
        start: date,
        end: date,
    ) -> ProviderIndicatorSeries:
        self.calls += 1
        observed_at = datetime(start.year, start.month, start.day, 15, tzinfo=UTC)
        return ProviderIndicatorSeries(
            provider="eastmoney_mx_finance_data",
            instrument_id=instrument_id,
            indicator_id=indicator_id,
            requested_start=start,
            requested_end=end,
            points=(
                ProviderIndicatorPoint(
                    session_date=start,
                    observed_at=observed_at,
                    first_available_at=observed_at,
                    values=tuple(
                        ProviderIndicatorValue(
                            field_code=f"FIELD_{index}",
                            field_name=name,
                            value=Decimal(index),
                        )
                        for index, name in enumerate(value_names, start=1)
                    ),
                ),
            ),
            response_sha256=_HASH,
            retrieved_at=_NOW,
            schema_version="eastmoney-mx.provider-indicator-history.v1",
            query=provider_indicator_name,
        )


def _pure_macd_strategy() -> StrategySpec:
    path = Path(__file__).parents[3] / "contracts/examples/strategy.macd-volume.daily.v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entry"] = payload["entry"]["children"][0]
    return StrategySpec.model_validate(payload)


def _service(
    *,
    provider: FileCachedHistoricalIndicatorData,
    store: _MemoryStore,
    queue: _Queue,
) -> BacktestSubmissionService:
    return BacktestSubmissionService(
        market_data=_SnapshotOnlyMarketData(),
        run_store=store,  # type: ignore[arg-type]
        job_queue=queue,  # type: ignore[arg-type]
        versions=SubmissionVersions(
            catalog_hash=_HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        provider_indicator_data=provider,
        clock=lambda: _NOW,
    )


def _seed_exact_request(
    *,
    cache_root: Path,
    strategy: StrategySpec,
    config: BacktestRunConfig,
) -> _SeedProvider:
    assert isinstance(strategy.entry, IndicatorCondition)
    binding = binding_for_provider_condition(strategy.entry)
    period = DateRange(
        start=strategy.backtest.start
        - timedelta(days=_effective_warmup_calendar_days(strategy, config)),
        end=strategy.backtest.end + timedelta(days=config.settlement_extension_days),
    )
    delegate = _SeedProvider()
    cache = FileCachedHistoricalIndicatorData(
        delegate,
        root=cache_root,
        clock=lambda: _NOW,
    )
    asyncio.run(
        cache.query_indicator_history(
            instrument_id=strategy.instrument.symbol,
            indicator_id=strategy.entry.indicator_id,
            provider_indicator_name=binding.provider_indicator_name,
            value_names=binding.value_names,
            start=period.start,
            end=period.end,
        )
    )
    return delegate


def test_cache_only_provider_hit_creates_and_enqueues_new_technical_backtest(
    tmp_path: Path,
) -> None:
    strategy = _pure_macd_strategy()
    config = BacktestRunConfig()
    cache_root = tmp_path / "provider-indicators"
    delegate = _seed_exact_request(
        cache_root=cache_root,
        strategy=strategy,
        config=config,
    )
    store = _MemoryStore()
    queue = _Queue()

    created = _service(
        provider=FileCachedHistoricalIndicatorData(
            None,
            root=cache_root,
            clock=lambda: _NOW,
        ),
        store=store,
        queue=queue,
    ).submit(strategy, config)

    pinned_config = json.loads(created.record.config_json)
    pinned_manifest = json.loads(created.record.manifest_json)
    assert created.record.state is BacktestJobState.QUEUED
    assert queue.run_ids == [str(created.record.run_id)]
    assert "provider_indicator_series" in pinned_config
    assert (
        pinned_manifest["provider_indicator_series"]
        == pinned_config["provider_indicator_series"]
    )
    assert delegate.calls == 1


def test_cache_only_provider_miss_fails_before_run_creation_or_enqueue(
    tmp_path: Path,
) -> None:
    store = _MemoryStore()
    queue = _Queue()
    service = _service(
        provider=FileCachedHistoricalIndicatorData(
            None,
            root=tmp_path / "empty-provider-indicators",
            clock=lambda: _NOW,
        ),
        store=store,
        queue=queue,
    )

    with pytest.raises(ProviderIndicatorCacheMissError, match="cache miss"):
        service.submit(_pure_macd_strategy(), BacktestRunConfig())

    assert store.by_fingerprint == {}
    assert queue.run_ids == []
