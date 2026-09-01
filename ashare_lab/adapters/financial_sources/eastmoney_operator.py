"""Eastmoney F10 ``操盘必读`` direct-value adapter.

Only provider-returned values cross this boundary.  The adapter does not
derive quarterly values, scale ratios, calculate TTM figures, or use the
collection clock as historical availability.  Point-in-time publication
matching is a separate application-layer gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

import httpx

from ashare_lab.adapters.host_http import HostThrottle, HostThrottledHttpClient
from ashare_lab.domain.shared import require_aware

OPERATOR_READING_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
OPERATOR_READING_PROVIDER = "eastmoney_f10_operator_reading"
OPERATOR_READING_SCHEMA_VERSION = "eastmoney-f10-operator-reading.v1"

_TIMEOUT_SECONDS = 15.0
_MIN_REQUEST_INTERVAL_SECONDS = 0.5
_MAX_PAGES = 100
_PAGE_SIZE = 100
_A_SHARE_STOCK = re.compile(
    r"(?:(?:600|601|603|605|688|689)[0-9]{3})\.SH|"
    r"(?:(?:000|001|002|003|300|301)[0-9]{3})\.SZ"
)


class OperatorDataset(StrEnum):
    """The narrowed, documented datasets used by the financial MVP."""

    LATEST_INDICATORS = "RPT_F10_FN_LATESTINDIC"
    QUARTERLY_TRENDS = "RPT_F10_FN_QUARTER"
    VALUATION_TREND = "RPT_CUSTOM_DMSK_TREND"
    VALUATION_PERCENTILES = "RPT_STOCKVALUATIONTANTILE"


_DATASET_FIELDS: Mapping[OperatorDataset, str] = {
    OperatorDataset.LATEST_INDICATORS: "ALL",
    OperatorDataset.QUARTERLY_TRENDS: "ALL",
    OperatorDataset.VALUATION_TREND: (
        "SECURITY_CODE,TRADE_DATE,INDICATORTYPE,INDICATOR_VALUE,SECUCODE"
    ),
    OperatorDataset.VALUATION_PERCENTILES: (
        "SECUCODE,STATISTICS_CYCLE,INDEX_TYPE,PERCENTILE_THIRTY,PERCENTILE_FIFTY,PERCENTILE_SEVENTY"
    ),
}


class EastmoneyOperatorReadingError(RuntimeError):
    """The provider response cannot be used as trusted financial input."""


@dataclass(frozen=True, slots=True)
class OperatorRequestAudit:
    dataset: OperatorDataset
    page: int
    raw_response_sha256: str
    retrieved_at: datetime
    row_count: int


@dataclass(frozen=True, slots=True)
class OperatorReadingBatch:
    dataset: OperatorDataset
    instrument_id: str
    rows: tuple[Mapping[str, object], ...]
    requests: tuple[OperatorRequestAudit, ...]
    canonical_rows_sha256: str
    indicator_type: int | None = None
    statistics_cycle: int | None = None


class EastmoneyOperatorReadingSource:
    """Fetch documented direct financial/valuation values without derivation."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        throttle: HostThrottle | None = None,
    ) -> None:
        self._http = HostThrottledHttpClient(
            client=client,
            transport=transport,
            timeout=_TIMEOUT_SECONDS,
            throttle=throttle,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EastmoneyOperatorReadingSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch_latest_indicators(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
    ) -> OperatorReadingBatch:
        return self._fetch_paged(
            OperatorDataset.LATEST_INDICATORS,
            instrument_id,
            filter_expression=f'(SECUCODE="{_instrument(instrument_id)}")',
            retrieved_at=retrieved_at,
            sort_field="REPORT_DATE",
            sort_order="-1",
        )

    def fetch_quarterly_trends(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
    ) -> OperatorReadingBatch:
        return self._fetch_paged(
            OperatorDataset.QUARTERLY_TRENDS,
            instrument_id,
            filter_expression=f'(SECUCODE="{_instrument(instrument_id)}")',
            retrieved_at=retrieved_at,
            sort_field="REPORT_DATE",
            sort_order="1",
        )

    def fetch_valuation_trends(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
        statistics_cycle: int = 4,
    ) -> tuple[OperatorReadingBatch, ...]:
        symbol = _instrument(instrument_id)
        if statistics_cycle not in {1, 2, 3, 4}:
            raise ValueError("statistics_cycle must be 1, 2, 3, or 4")
        return tuple(
            self._fetch_paged(
                OperatorDataset.VALUATION_TREND,
                symbol,
                filter_expression=(
                    f'(SECUCODE="{symbol}")(DATETYPE={statistics_cycle})'
                    f"(INDICATORTYPE={indicator_type})"
                ),
                retrieved_at=retrieved_at,
                sort_field="TRADE_DATE",
                sort_order="1",
                indicator_type=indicator_type,
                statistics_cycle=statistics_cycle,
            )
            for indicator_type in range(1, 5)
        )

    def fetch_valuation_percentiles(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
        statistics_cycle: int = 4,
    ) -> tuple[OperatorReadingBatch, ...]:
        symbol = _instrument(instrument_id)
        if statistics_cycle not in {1, 2, 3, 4}:
            raise ValueError("statistics_cycle must be 1, 2, 3, or 4")
        return tuple(
            self._fetch_paged(
                OperatorDataset.VALUATION_PERCENTILES,
                symbol,
                filter_expression=(
                    f'(SECUCODE="{symbol}")(INDEX_TYPE="{indicator_type}")'
                    f'(STATISTICS_CYCLE="{statistics_cycle}")'
                ),
                retrieved_at=retrieved_at,
                indicator_type=indicator_type,
                statistics_cycle=statistics_cycle,
            )
            for indicator_type in range(1, 5)
        )

    def _fetch_paged(
        self,
        dataset: OperatorDataset,
        instrument_id: str,
        *,
        filter_expression: str,
        retrieved_at: datetime,
        sort_field: str | None = None,
        sort_order: str | None = None,
        indicator_type: int | None = None,
        statistics_cycle: int | None = None,
    ) -> OperatorReadingBatch:
        symbol = _instrument(instrument_id)
        require_aware(retrieved_at, "retrieved_at")
        rows: list[Mapping[str, object]] = []
        audits: list[OperatorRequestAudit] = []
        expected_pages: int | None = None
        seen_rows: set[str] = set()
        page = 1
        while expected_pages is None or page <= expected_pages:
            params = {
                "type": dataset.value,
                "sty": _DATASET_FIELDS[dataset],
                "filter": filter_expression,
                "p": str(page),
                "ps": str(_PAGE_SIZE),
                "source": "SECURITIES",
                "client": "APP",
            }
            if sort_field is not None:
                params["st"] = sort_field
            if sort_order is not None:
                params["sr"] = sort_order
            response = self._http.get(
                OPERATOR_READING_URL,
                params=params,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://data.eastmoney.com/",
                    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; financial-backfill)",
                },
                timeout=_TIMEOUT_SECONDS,
                min_interval=_MIN_REQUEST_INTERVAL_SECONDS,
            )
            if response.status_code != 200:
                raise EastmoneyOperatorReadingError(
                    f"{dataset.value} HTTP status {response.status_code}"
                )
            raw_hash = "sha256:" + hashlib.sha256(response.content).hexdigest()
            payload = _response_payload(response.content, dataset)
            result_value = payload.get("result")
            if not isinstance(result_value, Mapping):
                raise EastmoneyOperatorReadingError(f"{dataset.value} result is missing")
            result = _string_keyed_mapping(
                cast(Mapping[object, object], result_value), f"{dataset.value} result"
            )
            raw_pages = result.get("pages", 1)
            if type(raw_pages) is not int or raw_pages < 1 or raw_pages > _MAX_PAGES:
                raise EastmoneyOperatorReadingError(f"{dataset.value} pages are invalid")
            if expected_pages is None:
                expected_pages = raw_pages
            elif expected_pages != raw_pages:
                raise EastmoneyOperatorReadingError(f"{dataset.value} pagination changed")
            raw_rows = result.get("data")
            if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
                raise EastmoneyOperatorReadingError(f"{dataset.value} data is invalid")
            page_rows = tuple(
                _validated_row(item, symbol, dataset) for item in cast(Sequence[object], raw_rows)
            )
            for item in page_rows:
                identity = _canonical_json(item)
                if identity in seen_rows:
                    raise EastmoneyOperatorReadingError(f"{dataset.value} contains duplicate rows")
                seen_rows.add(identity)
                rows.append(item)
            audits.append(
                OperatorRequestAudit(
                    dataset=dataset,
                    page=page,
                    raw_response_sha256=raw_hash,
                    retrieved_at=retrieved_at,
                    row_count=len(page_rows),
                )
            )
            page += 1

        canonical_rows_sha256 = (
            "sha256:" + hashlib.sha256(_canonical_json(rows).encode()).hexdigest()
        )
        return OperatorReadingBatch(
            dataset=dataset,
            instrument_id=symbol,
            rows=tuple(rows),
            requests=tuple(audits),
            canonical_rows_sha256=canonical_rows_sha256,
            indicator_type=indicator_type,
            statistics_cycle=statistics_cycle,
        )


def _instrument(value: str) -> str:
    if type(value) is not str or _A_SHARE_STOCK.fullmatch(value) is None:
        raise ValueError("instrument_id must be a canonical A-share stock symbol")
    return value


def _response_payload(content: bytes, dataset: OperatorDataset) -> Mapping[str, object]:
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EastmoneyOperatorReadingError(f"{dataset.value} response is not JSON") from error
    if not isinstance(decoded, Mapping):
        raise EastmoneyOperatorReadingError(f"{dataset.value} response is not an object")
    payload = _string_keyed_mapping(
        cast(Mapping[object, object], decoded), f"{dataset.value} response"
    )
    if payload.get("success") is not True or payload.get("code") != 0:
        raise EastmoneyOperatorReadingError(
            f"{dataset.value} provider error {payload.get('code')}: {payload.get('message')}"
        )
    return payload


def _validated_row(
    value: object,
    instrument_id: str,
    dataset: OperatorDataset,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EastmoneyOperatorReadingError(f"{dataset.value} row is invalid")
    row = _string_keyed_mapping(cast(Mapping[object, object], value), f"{dataset.value} row")
    if row.get("SECUCODE") != instrument_id:
        raise EastmoneyOperatorReadingError(f"{dataset.value} security identity mismatch")
    return dict(row)


def _string_keyed_mapping(value: Mapping[object, object], label: str) -> Mapping[str, object]:
    if any(type(key) is not str for key in value):
        raise EastmoneyOperatorReadingError(f"{label} has non-string keys")
    return cast(Mapping[str, object], value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "OPERATOR_READING_PROVIDER",
    "OPERATOR_READING_SCHEMA_VERSION",
    "EastmoneyOperatorReadingError",
    "EastmoneyOperatorReadingSource",
    "OperatorDataset",
    "OperatorReadingBatch",
    "OperatorRequestAudit",
]
