"""Normalize Tushare ``anns_d`` rows without importing Tushare Pro."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import cast

from ashare_lab.domain.events.observations import EventObservation

from .ifind import (
    VendorEventRowError,
    VendorRowsCallable,
    vendor_row_observation_quality,
    vendor_row_optional_sha256,
    vendor_row_optional_text,
    vendor_row_raw_attributes,
    vendor_row_raw_response_sha256,
    vendor_row_required_event_code,
    vendor_row_required_identifier,
    vendor_row_required_instrument,
    vendor_row_required_retrieved_at,
    vendor_row_required_text,
    vendor_row_required_timestamp,
    vendor_row_revision_no,
)


class TushareEventRowError(VendorEventRowError):
    """Raised when a Tushare announcement row violates the adapter contract."""


class TushareEventSourceAdapter:
    """Inject a callable returning ``anns_d`` mappings, recorded or live."""

    def __init__(self, fetch_rows: VendorRowsCallable) -> None:
        self._fetch_rows = fetch_rows

    def normalize(
        self,
        row: Mapping[str, object],
        *,
        event_code: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> EventObservation:
        return normalize_tushare_row(
            row,
            event_code=event_code,
            retrieved_at=retrieved_at,
        )

    def fetch(
        self,
        *args: object,
        event_code: str,
        retrieved_at: datetime,
        **kwargs: object,
    ) -> tuple[EventObservation, ...]:
        rows: Iterable[object] = self._fetch_rows(*args, **kwargs)
        observations: list[EventObservation] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise TushareEventRowError(
                    f"Tushare row {index} must be a mapping, got {type(row).__name__}"
                )
            typed_row = cast(Mapping[str, object], row)
            observations.append(
                normalize_tushare_row(
                    typed_row,
                    event_code=event_code,
                    retrieved_at=retrieved_at,
                )
            )
        return tuple(observations)


def normalize_tushare_row(
    row: Mapping[str, object],
    *,
    event_code: str | None = None,
    retrieved_at: datetime | None = None,
) -> EventObservation:
    """Convert one Tushare ``anns_d`` row using ``rec_time`` as source time."""

    document_url = vendor_row_optional_text(row, ("url", "document_url"))
    provider_event_id = vendor_row_required_identifier(
        row,
        "Tushare",
        ("provider_event_id", "announcement_id", "id", "seq"),
        fallback_names=("url",),
    )
    instrument_id = vendor_row_required_instrument(
        row,
        "Tushare",
        ("ts_code", "instrument_id"),
    )
    canonical_event_code = vendor_row_required_event_code(row, event_code, "Tushare")
    title = vendor_row_required_text(row, "Tushare", ("title", "reportTitle"))
    released = vendor_row_required_timestamp(row, "Tushare", ("rec_time",))
    ingested_at = vendor_row_required_retrieved_at(row, retrieved_at, "Tushare")
    quality, validation_status = vendor_row_observation_quality(released)

    return EventObservation(
        provider="tushare",
        provider_event_id=provider_event_id,
        instrument_id=instrument_id,
        event_code=canonical_event_code,
        title=title,
        occurred_at=None,
        source_released_at=released.value,
        vendor_first_available_at=None,
        retrieved_at=ingested_at,
        time_quality=quality,
        document_url=document_url,
        document_sha256=vendor_row_optional_sha256(row, "document_sha256", "Tushare"),
        raw_response_sha256=vendor_row_raw_response_sha256(row, "Tushare"),
        validation_status=validation_status,
        attributes=vendor_row_raw_attributes(row, "Tushare"),
        revision_no=vendor_row_revision_no(row, "Tushare"),
    )


__all__ = [
    "TushareEventRowError",
    "TushareEventSourceAdapter",
    "normalize_tushare_row",
]
