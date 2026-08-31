#!/usr/bin/env python3
"""Acquire, validate and freeze multi-source A-share events."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from ashare_lab.adapters.event_sources import (
    EastmoneyAnnouncementSource,
    EventCollectionRequest,
    build_event_acquisition_coverage,
    collect_event_observations,
    normalize_ifind_row,
    normalize_rqdata_row,
    normalize_tushare_row,
    normalize_web_evidence_row,
)
from ashare_lab.adapters.event_sources.document_text import (
    DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE,
    DocumentTextIncompleteError,
)
from ashare_lab.adapters.market_data import (
    build_event_snapshot,
    build_no_event_required_snapshot,
    compose_choice_event_snapshot,
    normalize_instrument_id,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
STRICT_EASTMONEY_TIMESTAMP_FLOOR = date(2017, 1, 1)
DEFAULT_EVENT_CODES = (
    "event.financial_results.earnings_forecast_published",
    "event.financial_results.earnings_flash_report",
    "event.financial_results.annual_report",
    "event.financial_results.semiannual_report",
    "event.financial_results.quarterly_report",
    "event.repurchase_capital.repurchase_change",
)
_SECRET_FRAGMENTS = ("password", "passwd", "token", "userinfo", "mobile", "phone")
type RowNormalizer = Callable[..., EventObservation]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="300059.SZ")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--event-code", action="append", dest="event_codes")
    parser.add_argument(
        "--no-event-required",
        action="store_true",
        help=(
            "publish an explicit empty Event v2 input for a technical-only strategy; "
            "no event provider is queried and event capability is not advertised"
        ),
    )
    parser.add_argument("--ifind-json", type=Path)
    parser.add_argument("--rqdata-json", type=Path)
    parser.add_argument("--tushare-json", type=Path)
    parser.add_argument(
        "--web-evidence-json",
        action="append",
        type=Path,
        default=[],
        help="normalized authoritative-page evidence JSON; may be repeated",
    )
    parser.add_argument("--skip-eastmoney", action="store_true")
    parser.add_argument(
        "--extract-document-text",
        action="store_true",
        help=(
            "extract complete embedded text from periodic-report documents; "
            "scanned, incomplete, or unreadable documents fail closed"
        ),
    )
    parser.add_argument(
        "--research-snapshot",
        action="store_true",
        help="retain quarantined evidence; cannot be composed into a Demo snapshot",
    )
    parser.add_argument("--event-output", type=Path, default=Path("var/snapshots/events"))
    parser.add_argument("--choice-snapshot", type=Path)
    parser.add_argument(
        "--composite-output",
        type=Path,
        default=Path("var/snapshots/composite"),
    )
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not exceed --end")
    if args.research_snapshot and args.choice_snapshot is not None:
        parser.error("--research-snapshot cannot be combined with --choice-snapshot")
    if args.no_event_required:
        incompatible = (
            args.event_codes
            or args.ifind_json is not None
            or args.rqdata_json is not None
            or args.tushare_json is not None
            or bool(args.web_evidence_json)
            or args.skip_eastmoney
            or args.extract_document_text
            or args.research_snapshot
        )
        if incompatible:
            parser.error("--no-event-required cannot be combined with event acquisition options")
        if args.choice_snapshot is None:
            parser.error("--no-event-required requires --choice-snapshot")
    if (
        not args.no_event_required
        and not args.research_snapshot
        and args.start < STRICT_EASTMONEY_TIMESTAMP_FLOOR
    ):
        parser.error(
            "strict Demo event snapshots cannot start before 2017-01-01: "
            "older Eastmoney eiTime values do not meet the historical first-availability gate"
        )

    instrument_id = normalize_instrument_id(args.symbol)
    captured_at = datetime.now(SHANGHAI).replace(microsecond=0)
    if args.no_event_required:
        event_snapshot = build_no_event_required_snapshot(
            instrument_id=instrument_id,
            start=args.start,
            end=args.end,
            output_root=args.event_output,
            captured_at=captured_at,
        )
        composite = compose_choice_event_snapshot(
            choice_snapshot_path=cast(Path, args.choice_snapshot),
            event_snapshot_path=event_snapshot.path,
            output_root=args.composite_output,
            composed_at=captured_at,
        )
        print(
            json.dumps(
                {
                    "eventSnapshotId": event_snapshot.snapshot_id,
                    "eventSnapshotPath": str(event_snapshot.path),
                    "compositeSnapshotId": composite.snapshot_id,
                    "compositeSnapshotPath": str(composite.path),
                    "mode": "no_event_required",
                    "acquisitionCoverage": event_snapshot.manifest["acquisitionCoverage"],
                    "sources": [],
                    "webEvidence": {
                        "files": 0,
                        "inputRows": 0,
                        "acceptedRows": 0,
                        "byProvider": {},
                        "byValidationStatus": {},
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    allowed_codes = frozenset(args.event_codes or DEFAULT_EVENT_CODES)
    request = EventCollectionRequest(
        instrument_id=instrument_id,
        start=args.start,
        end=args.end,
        retrieved_at=captured_at,
    )
    fetchers = _build_fetchers(
        allowed_codes=allowed_codes,
        skip_eastmoney=args.skip_eastmoney,
        ifind_json=args.ifind_json,
        rqdata_json=args.rqdata_json,
        tushare_json=args.tushare_json,
        extract_document_text=args.extract_document_text,
    )
    collection = collect_event_observations(
        request,
        fetchers,
        require_primary=not args.skip_eastmoney,
    )
    web_observations, web_evidence_report = _load_web_evidence(
        tuple(args.web_evidence_json),
        allowed_codes=allowed_codes,
        instrument_id=instrument_id,
        retrieved_at=captured_at,
    )
    filtered = tuple(
        item
        for item in (*collection.observations, *web_observations)
        if item.event_code in allowed_codes
    )
    acquisition_coverage = build_event_acquisition_coverage(
        request,
        collection,
        requested_event_codes=tuple(sorted(allowed_codes)),
        supplemental_observations=web_observations,
    )
    event_snapshot = build_event_snapshot(
        observations=filtered,
        acquisition_coverage=acquisition_coverage,
        output_root=args.event_output,
        captured_at=captured_at,
        strict_demo=not args.research_snapshot,
    )

    payload: dict[str, object] = {
        "eventSnapshotId": event_snapshot.snapshot_id,
        "eventSnapshotPath": str(event_snapshot.path),
        "mode": "research" if args.research_snapshot else "strict_demo",
        "acquisitionCoverage": acquisition_coverage,
        "webEvidence": web_evidence_report,
        "sources": [
            {
                "provider": item.provider,
                "status": item.status,
                "rows": len(item.observations),
                "errorType": item.error_type,
                "errorMessage": item.error_message,
            }
            for item in collection.sources
        ],
    }
    if args.choice_snapshot is not None:
        composite = compose_choice_event_snapshot(
            choice_snapshot_path=args.choice_snapshot,
            event_snapshot_path=event_snapshot.path,
            output_root=args.composite_output,
            composed_at=captured_at,
        )
        payload.update(
            {
                "compositeSnapshotId": composite.snapshot_id,
                "compositeSnapshotPath": str(composite.path),
            }
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _build_fetchers(
    *,
    allowed_codes: frozenset[str],
    skip_eastmoney: bool,
    ifind_json: Path | None,
    rqdata_json: Path | None,
    tushare_json: Path | None,
    extract_document_text: bool,
):
    fetchers = {}
    if not skip_eastmoney:

        def fetch_eastmoney(request: EventCollectionRequest):
            with EastmoneyAnnouncementSource(
                extract_document_text=extract_document_text,
            ) as source:
                batch = source.fetch_batch(
                    instrument_id=request.instrument_id,
                    start=request.start,
                    end=request.end,
                    retrieved_at=request.retrieved_at,
                    requested_event_codes=tuple(sorted(allowed_codes)),
                )
            return batch

        fetchers["eastmoney"] = fetch_eastmoney

    vendor_specs: tuple[tuple[str, Path | None, RowNormalizer], ...] = (
        ("ifind", ifind_json, normalize_ifind_row),
        ("rqdata", rqdata_json, normalize_rqdata_row),
        ("tushare", tushare_json, normalize_tushare_row),
    )
    for provider, path, normalizer in vendor_specs:
        if path is None:
            continue
        rows = _load_rows(path)

        def fetch_vendor(
            request: EventCollectionRequest,
            *,
            captured_rows: tuple[Mapping[str, object], ...] = rows,
            captured_normalizer: RowNormalizer = normalizer,
        ) -> tuple[EventObservation, ...]:
            observations: list[EventObservation] = []
            for row in captured_rows:
                event_code = row.get("event_code")
                if not isinstance(event_code, str):
                    if len(allowed_codes) != 1:
                        raise ValueError(
                            "vendor JSON rows need event_code when multiple event codes are enabled"
                        )
                    event_code = next(iter(allowed_codes))
                if event_code not in allowed_codes:
                    continue
                observations.append(
                    captured_normalizer(
                        row,
                        event_code=event_code,
                        retrieved_at=request.retrieved_at,
                    )
                )
            return tuple(observations)

        fetchers[provider] = fetch_vendor
    return fetchers


def _load_web_evidence(
    paths: tuple[Path, ...],
    *,
    allowed_codes: frozenset[str],
    instrument_id: InstrumentId,
    retrieved_at: datetime,
) -> tuple[tuple[EventObservation, ...], Mapping[str, object]]:
    observations: list[EventObservation] = []
    input_rows = 0
    for path in paths:
        rows = _load_rows(path)
        input_rows += len(rows)
        for index, row in enumerate(rows):
            event_code = row.get("event_code")
            if isinstance(event_code, str) and event_code not in allowed_codes:
                continue
            try:
                observation = normalize_web_evidence_row(row, retrieved_at=retrieved_at)
            except ValueError as exc:
                raise ValueError(f"invalid web evidence {path} row {index}: {exc}") from exc
            if observation.instrument_id != instrument_id:
                raise ValueError(
                    f"web evidence {path} row {index} targets "
                    f"{observation.instrument_id}, expected {instrument_id}"
                )
            observations.append(observation)

    observations.sort(
        key=lambda item: (
            item.instrument_id.value,
            item.event_code,
            item.provider,
            item.provider_event_id,
            item.revision_no,
        )
    )
    provider_counts = Counter(item.provider for item in observations)
    status_counts = Counter(item.validation_status for item in observations)
    report: Mapping[str, object] = {
        "files": len(paths),
        "inputRows": input_rows,
        "acceptedRows": len(observations),
        "byProvider": dict(sorted(provider_counts.items())),
        "byValidationStatus": dict(sorted(status_counts.items())),
    }
    return tuple(observations), report


def _load_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    try:
        decoded: object = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read event JSON rows: {path}") from exc
    if isinstance(decoded, dict) and isinstance(decoded.get("data"), list):
        decoded = decoded["data"]
    if not isinstance(decoded, list):
        raise ValueError(f"event JSON must be an array or a data array: {path}")
    _reject_secret_keys(decoded)
    rows: list[Mapping[str, object]] = []
    for index, item in enumerate(cast(list[object], decoded)):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise ValueError(f"event JSON row {index} must be an object with string keys")
        rows.append(cast(Mapping[str, object], item))
    return tuple(rows)


def _reject_secret_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in cast(dict[object, object], value).items():
            normalized = str(key).replace("_", "").lower()
            if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
                raise ValueError(f"event JSON contains forbidden secret-like key: {key}")
            _reject_secret_keys(nested)
    elif isinstance(value, list):
        for nested in cast(Sequence[object], value):
            _reject_secret_keys(nested)


def _has_document_text_incomplete_error(error: BaseException) -> bool:
    """Recognize typed document-quality failure across provider wrappers."""

    pending: list[BaseException] = [error]
    observed: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in observed:
            continue
        observed.add(identity)
        if isinstance(current, DocumentTextIncompleteError):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as error:
        if not _has_document_text_incomplete_error(error):
            raise
        print(
            "event document text could not be extracted completely",
            file=sys.stderr,
        )
        exit_code = DOCUMENT_TEXT_INCOMPLETE_EXIT_CODE
    raise SystemExit(exit_code)
