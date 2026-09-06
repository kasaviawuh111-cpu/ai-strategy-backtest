"""Small persistent cache for already-calculated provider indicator series.

This cache is an availability/performance layer only.  Every backtest still
copies the exact provider series and response hash into its immutable work
item, so replacing or expiring a cache entry cannot rewrite an old run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)

_CACHE_SCHEMA = "ashare-lab.provider-indicator-cache.v2"


class ProviderIndicatorCacheError(ValueError):
    """A cache entry cannot be trusted and must not be used."""


class ProviderIndicatorCacheMissError(RuntimeError):
    """No validated cached series exists and no live provider may fill it."""


class FileCachedHistoricalIndicatorData:
    """Reuse validated provider values and optionally fill misses from a provider.

    A ``None`` delegate makes this a cache-only, fail-closed data source.  That
    mode is used when the service has durable provider evidence but deliberately
    has no live credential or network access.
    """

    def __init__(
        self,
        delegate: HistoricalIndicatorData | None,
        *,
        root: Path,
        ttl: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        persistent_instruments: frozenset[str] | None = None,
        contract_namespace: str | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("provider indicator cache ttl must be positive")
        self._delegate = delegate
        self._root = root.expanduser().resolve()
        self._ttl = ttl
        self._clock = clock
        self._persistent_instruments = persistent_instruments
        self._contract_namespace = contract_namespace
        self._gate = threading.RLock()
        self._inflight: dict[str, Future[ProviderIndicatorSeries]] = {}

    async def query_indicator_history(
        self,
        *,
        instrument_id: str,
        indicator_id: str,
        provider_indicator_name: str,
        value_names: tuple[str, ...],
        start: date,
        end: date,
        force_refresh: bool = False,
    ) -> ProviderIndicatorSeries:
        request = {
            "end": end.isoformat(),
            "indicatorId": indicator_id,
            "instrumentId": instrument_id,
            "providerIndicatorName": provider_indicator_name,
            "start": start.isoformat(),
            "valueNames": list(value_names),
        }
        if self._contract_namespace is not None:
            request["contractNamespace"] = self._contract_namespace
        key = _sha256(request)
        persist = (
            self._persistent_instruments is None
            or instrument_id in self._persistent_instruments
        )
        with self._gate:
            cached = self._read(key=key, request=request) if persist and not force_refresh else None
            if cached is not None:
                return replace(cached, cache_status="disk")
            delegate = self._delegate
            if delegate is None:
                raise ProviderIndicatorCacheMissError(
                    "provider indicator cache miss and no live provider is configured"
                )
            # Refresh never joins an ordinary request, and older owners may
            # finish for their callers without replacing the refreshed cache.
            future = None if force_refresh else self._inflight.get(key)
            owner = future is None
            if future is None:
                future = Future[ProviderIndicatorSeries]()
                self._inflight[key] = future

        if not owner:
            series = await asyncio.wrap_future(future)
            return replace(series, cache_status="live")

        try:
            series = await delegate.query_indicator_history(
                instrument_id=instrument_id,
                indicator_id=indicator_id,
                provider_indicator_name=provider_indicator_name,
                value_names=value_names,
                start=start,
                end=end,
            )
            series = replace(series, cache_status=None)
            with self._gate:
                if persist and self._inflight.get(key) is future:
                    self._write(key=key, request=request, series=series)
        except BaseException as exc:
            future.set_exception(exc)
            # The owner observes the original exception directly.  Mark the
            # shared Future as observed when there were no waiting callers.
            future.exception()
            raise
        else:
            future.set_result(series)
            return replace(series, cache_status="forced" if force_refresh else "live")
        finally:
            with self._gate:
                if self._inflight.get(key) is future:
                    self._inflight.pop(key, None)

    def _read(
        self,
        *,
        key: str,
        request: Mapping[str, object],
    ) -> ProviderIndicatorSeries | None:
        path = self._root / f"{key}.json"
        try:
            raw_payload: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw_payload, dict):
                return None
            payload = cast(dict[str, object], raw_payload)
            if payload.get("schemaVersion") != _CACHE_SCHEMA:
                return None
            if payload.get("request") != request:
                return None
            stored_at = datetime.fromisoformat(str(payload.get("storedAt")))
            if stored_at.tzinfo is None or stored_at.utcoffset() is None:
                return None
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("provider indicator cache clock must be timezone-aware")
            if now - stored_at > self._ttl:
                return None
            raw_series_payload = payload.get("series")
            if not isinstance(raw_series_payload, dict):
                return None
            series_payload = cast(dict[str, Any], raw_series_payload)
            expected = _sha256({"request": request, "series": series_payload})
            if payload.get("contentHash") != expected:
                return None
            return _series_from_payload(series_payload)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _write(
        self,
        *,
        key: str,
        request: Mapping[str, object],
        series: ProviderIndicatorSeries,
    ) -> None:
        series_payload = _series_payload(series)
        payload = {
            "contentHash": _sha256({"request": request, "series": series_payload}),
            "request": dict(request),
            "schemaVersion": _CACHE_SCHEMA,
            "series": series_payload,
            "storedAt": self._clock().isoformat(),
        }
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._root / f"{key}.json"
        temporary = self._root / f".{key}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)


def _series_payload(series: ProviderIndicatorSeries) -> dict[str, object]:
    return {
        "end": series.requested_end.isoformat(),
        "indicatorId": series.indicator_id,
        "instrumentId": series.instrument_id,
        "points": [
            {
                "firstAvailableAt": point.first_available_at.isoformat(),
                "observedAt": point.observed_at.isoformat(),
                "sessionDate": point.session_date.isoformat(),
                "values": [
                    {
                        "fieldCode": value.field_code,
                        "fieldName": value.field_name,
                        "sourceFieldName": value.source_field_name,
                        "sourceUnit": value.source_unit,
                        "sourceParameters": value.source_parameters,
                        "unit": value.unit,
                        "value": str(value.value),
                    }
                    for value in point.values
                ],
            }
            for point in series.points
        ],
        "provider": series.provider,
        "query": series.query,
        "responseSha256": series.response_sha256,
        "retrievedAt": series.retrieved_at.isoformat(),
        "schemaVersion": series.schema_version,
        "start": series.requested_start.isoformat(),
    }


def _series_from_payload(payload: Mapping[str, Any]) -> ProviderIndicatorSeries:
    return ProviderIndicatorSeries(
        provider=str(payload["provider"]),
        instrument_id=str(payload["instrumentId"]),
        indicator_id=str(payload["indicatorId"]),
        requested_start=date.fromisoformat(str(payload["start"])),
        requested_end=date.fromisoformat(str(payload["end"])),
        points=tuple(
            ProviderIndicatorPoint(
                session_date=date.fromisoformat(str(point["sessionDate"])),
                observed_at=datetime.fromisoformat(str(point["observedAt"])),
                first_available_at=datetime.fromisoformat(str(point["firstAvailableAt"])),
                values=tuple(
                    ProviderIndicatorValue(
                        field_code=str(value["fieldCode"]),
                        field_name=str(value["fieldName"]),
                        value=Decimal(str(value["value"])),
                        unit=None if value.get("unit") is None else str(value["unit"]),
                        source_field_name=value.get("sourceFieldName"),
                        source_unit=value.get("sourceUnit"),
                        source_parameters=value.get("sourceParameters"),
                    )
                    for value in cast(list[dict[str, Any]], point["values"])
                ),
            )
            for point in cast(list[dict[str, Any]], payload["points"])
        ),
        response_sha256=str(payload["responseSha256"]),
        retrieved_at=datetime.fromisoformat(str(payload["retrievedAt"])),
        schema_version=str(payload["schemaVersion"]),
        query=str(payload["query"]),
    )


def _sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
