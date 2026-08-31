"""Normalize RQData announcement rows without importing ``rqdatac``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import cast

from ashare_lab.domain.events.observations import EventObservation
from ashare_lab.domain.market_data import TimeQuality

from .ifind import (
    VALIDATED,
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


class RQDataEventRowError(VendorEventRowError):
    """Raised when an RQData announcement row violates the adapter contract."""


class RQDataEventSourceAdapter:
    """Inject a callable returning RQData rows, recorded or live."""

    def __init__(self, fetch_rows: VendorRowsCallable) -> None:
        self._fetch_rows = fetch_rows

    def normalize(
        self,
        row: Mapping[str, object],
        *,
        event_code: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> EventObservation:
        return normalize_rqdata_row(
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
                raise RQDataEventRowError(
                    f"RQData row {index} must be a mapping, got {type(row).__name__}"
                )
            typed_row = cast(Mapping[str, object], row)
            observations.append(
                normalize_rqdata_row(
                    typed_row,
                    event_code=event_code,
                    retrieved_at=retrieved_at,
                )
            )
        return tuple(observations)


def normalize_rqdata_row(
    row: Mapping[str, object],
    *,
    event_code: str | None = None,
    retrieved_at: datetime | None = None,
) -> EventObservation:
    """Convert one ``rqdatac.get_announcement`` row.

    ``info_date`` is the source publication field while ``create_tm`` is the
    vendor's first recorded database availability.  Keeping both prevents a
    backtest from treating the earlier date as a precise vendor observation.
    """

    document_url = vendor_row_optional_text(
        row,
        ("announcement_link", "url", "document_url"),
    )
    provider_event_id = vendor_row_required_identifier(
        row,
        "RQData",
        ("provider_event_id", "announcement_id", "id", "seq"),
        fallback_names=("announcement_link", "url"),
    )
    instrument_id = vendor_row_required_instrument(
        row,
        "RQData",
        ("order_book_id", "instrument_id", "ts_code"),
    )
    canonical_event_code = vendor_row_required_event_code(row, event_code, "RQData")
    title = vendor_row_required_text(row, "RQData", ("title", "reportTitle"))
    released = vendor_row_required_timestamp(row, "RQData", ("info_date",))
    vendor_available = vendor_row_required_timestamp(row, "RQData", ("create_tm",))
    ingested_at = vendor_row_required_retrieved_at(row, retrieved_at, "RQData")
    if (
        vendor_available.second_precision
        and vendor_available.time_quality is not TimeQuality.DATE_ONLY_CONSERVATIVE
        and vendor_available.value >= released.value
    ):
        # ``info_date`` is often only a date, while ``create_tm`` is RQData's
        # precise first database time.  The latter can safely dominate the
        # conservative 15:00 source-date placeholder without pretending the
        # placeholder itself was exact.
        quality, validation_status = TimeQuality.VENDOR_OBSERVED, VALIDATED
    else:
        quality, validation_status = vendor_row_observation_quality(
            released,
            vendor_available,
        )

    return EventObservation(
        provider="rqdata",
        provider_event_id=provider_event_id,
        instrument_id=instrument_id,
        event_code=canonical_event_code,
        title=title,
        occurred_at=None,
        source_released_at=released.value,
        vendor_first_available_at=vendor_available.value,
        retrieved_at=ingested_at,
        time_quality=quality,
        document_url=document_url,
        document_sha256=vendor_row_optional_sha256(row, "document_sha256", "RQData"),
        raw_response_sha256=vendor_row_raw_response_sha256(row, "RQData"),
        validation_status=validation_status,
        attributes=vendor_row_raw_attributes(row, "RQData"),
        revision_no=vendor_row_revision_no(row, "RQData"),
    )


__all__ = [
    "RQDataEventRowError",
    "RQDataEventSourceAdapter",
    "normalize_rqdata_row",
]
