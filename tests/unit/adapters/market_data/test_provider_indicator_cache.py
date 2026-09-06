from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
    ProviderIndicatorCacheMissError,
)
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)


class _Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.revision = 0
        self.fail = False

    async def query_indicator_history(self, **request: object) -> ProviderIndicatorSeries:
        self.calls += 1
        assert "force_refresh" not in request
        if self.fail:
            raise RuntimeError("MX indicator refresh failed")
        await asyncio.sleep(0.01)
        session = date.fromisoformat(str(request["start"]))
        return ProviderIndicatorSeries(
            provider="eastmoney_mx_finance_data",
            instrument_id=str(request["instrument_id"]),
            indicator_id=str(request["indicator_id"]),
            requested_start=session,
            requested_end=date.fromisoformat(str(request["end"])),
            points=(
                ProviderIndicatorPoint(
                    session_date=session,
                    observed_at=datetime(2026, 9, 1, 15, tzinfo=UTC),
                    first_available_at=datetime(2026, 9, 1, 15, tzinfo=UTC),
                    values=(
                        ProviderIndicatorValue(
                            field_code="KDJ_K",
                            field_name="K值",
                            value=Decimal("21.5") + self.revision,
                        ),
                    ),
                ),
            ),
            response_sha256=f"sha256:{self.revision:064x}",
            retrieved_at=datetime(2026, 9, 2, tzinfo=UTC) + timedelta(seconds=self.revision),
            schema_version="eastmoney-mx.provider-indicator-history.v1",
            query="provider query",
        )


async def _query(
    provider: FileCachedHistoricalIndicatorData,
    *,
    force_refresh: bool = False,
) -> ProviderIndicatorSeries:
    return await provider.query_indicator_history(
        instrument_id="300033.SZ",
        indicator_id="technical.kdj",
        provider_indicator_name="KDJ(9,3,3)",
        value_names=("K值",),
        start=date(2026, 9, 1),
        end=date(2026, 9, 1),
        force_refresh=force_refresh,
    )


def test_cache_reuses_validated_provider_series_across_instances(tmp_path: Path) -> None:
    provider = _Provider()
    first = FileCachedHistoricalIndicatorData(provider, root=tmp_path)
    second = FileCachedHistoricalIndicatorData(provider, root=tmp_path)

    initial = asyncio.run(_query(first))
    cached = asyncio.run(_query(second))

    assert initial.cache_status == "live"
    assert cached == replace(initial, cache_status="disk")
    assert provider.calls == 1
    assert len(tuple(tmp_path.glob("*.json"))) == 1


def test_cache_only_instance_reuses_validated_file_without_live_delegate(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    online = FileCachedHistoricalIndicatorData(provider, root=tmp_path)
    cached_only = FileCachedHistoricalIndicatorData(None, root=tmp_path)

    initial = asyncio.run(_query(online))
    cached = asyncio.run(_query(cached_only))

    assert initial.cache_status == "live"
    assert cached == replace(initial, cache_status="disk")
    assert provider.calls == 1


def test_cache_only_miss_fails_closed(tmp_path: Path) -> None:
    cache = FileCachedHistoricalIndicatorData(None, root=tmp_path)

    with pytest.raises(
        ProviderIndicatorCacheMissError,
        match="cache miss and no live provider",
    ):
        asyncio.run(_query(cache))


def test_identical_concurrent_misses_are_coalesced(tmp_path: Path) -> None:
    provider = _Provider()
    cache = FileCachedHistoricalIndicatorData(provider, root=tmp_path)

    async def query_twice() -> tuple[ProviderIndicatorSeries, ProviderIndicatorSeries]:
        first, second = await asyncio.gather(
            _query(cache),
            _query(cache),
        )
        return first, second

    first, second = asyncio.run(query_twice())

    assert first == second
    assert first.cache_status == "live"
    assert provider.calls == 1


def test_indicator_refresh_bypasses_disk_updates_cache_and_preserves_old_series(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    cache = FileCachedHistoricalIndicatorData(provider, root=tmp_path)
    old = asyncio.run(_query(cache))
    provider.revision = 1

    refreshed = asyncio.run(_query(cache, force_refresh=True))
    cached = asyncio.run(_query(cache))
    cache_only = asyncio.run(_query(FileCachedHistoricalIndicatorData(None, root=tmp_path)))

    assert provider.calls == 2
    assert refreshed.cache_status == "forced"
    assert refreshed.provider == old.provider == "eastmoney_mx_finance_data"
    assert refreshed.points[0].values[0].value == Decimal("22.5")
    assert refreshed.response_sha256 != old.response_sha256
    assert cached == cache_only == replace(refreshed, cache_status="disk")
    assert old.cache_status == "live"
    assert old.points[0].values[0].value == Decimal("21.5")
    stored = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert "cache_status" not in stored["series"]
    assert "cacheStatus" not in stored["series"]


def test_failed_indicator_refresh_does_not_return_cached_values(tmp_path: Path) -> None:
    provider = _Provider()
    cache = FileCachedHistoricalIndicatorData(provider, root=tmp_path)
    old = asyncio.run(_query(cache))
    provider.fail = True

    with pytest.raises(RuntimeError, match="MX indicator refresh failed"):
        asyncio.run(_query(cache, force_refresh=True))
    with pytest.raises(ProviderIndicatorCacheMissError):
        asyncio.run(_query(
            FileCachedHistoricalIndicatorData(None, root=tmp_path), force_refresh=True,
        ))

    assert provider.calls == 2
    assert asyncio.run(_query(cache)) == replace(old, cache_status="disk")


@pytest.mark.asyncio
async def test_indicator_refresh_bypasses_inflight_and_late_old_request_cannot_overwrite_it(
    tmp_path: Path,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(_Provider):
        async def query_indicator_history(self, **request: object) -> ProviderIndicatorSeries:
            result = await super().query_indicator_history(**request)
            if self.calls == 1:
                started.set()
                await release.wait()
            return result

    provider = BlockingProvider()
    cache = FileCachedHistoricalIndicatorData(provider, root=tmp_path)
    ordinary = asyncio.create_task(_query(cache))
    await started.wait()
    provider.revision = 1
    try:
        refreshed = await asyncio.wait_for(_query(cache, force_refresh=True), timeout=1)
    finally:
        release.set()
        original_result = await ordinary

    assert provider.calls == 2
    assert original_result.cache_status == "live"
    assert original_result.points[0].values[0].value == Decimal("21.5")
    assert refreshed.cache_status == "forced"
    assert refreshed.points[0].values[0].value == Decimal("22.5")
    assert await _query(cache) == replace(refreshed, cache_status="disk")
