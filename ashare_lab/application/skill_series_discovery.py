"""Response-backed historical numeric discovery, not executable data binding.

No indicator list is consulted. Dates describe observations only: discovering
a numeric history does not establish its frequency, publication time or PIT
semantics, and must not itself authorize a backtest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import uuid4

from ashare_lab.domain.market_data import normalize_a_share_instrument
from ashare_lab.ports.finance_history_format import FinanceHistoryDecoder
from ashare_lab.ports.live_market_data import LiveFinanceData, LiveFinanceDataResult


@dataclass(frozen=True, slots=True)
class SkillSeriesFieldEvidence:
    return_code: str
    return_name: str | None
    display_name: str | None
    return_source_code: str | None
    return_source_name: str | None
    unit: str | None
    fixed_param_value: str | None
    metadata_json: str
    raw_values_json: str
    values: tuple[Decimal | None, ...]
    missing_indices: tuple[int, ...]
    issues: tuple[str, ...]

    @property
    def has_numeric_history(self) -> bool:
        return not self.issues and any(value is not None for value in self.values)


@dataclass(frozen=True, slots=True)
class SkillSeriesTableEvidence:
    table_index: int
    entity_codes: tuple[str, ...]
    instrument_verified: bool
    metadata_json: str
    raw_dates_json: str
    dates: tuple[date, ...]
    fields: tuple[SkillSeriesFieldEvidence, ...]
    issues: tuple[str, ...]

    @property
    def has_numeric_history(self) -> bool:
        return not self.issues and any(field.has_numeric_history for field in self.fields)


@dataclass(frozen=True, slots=True)
class SkillSeriesDiscoveryAttempt:
    query: str
    response_hash: str | None
    issues: tuple[str, ...]


class SkillSeriesDiscoveryNoHistoryError(RuntimeError):
    """Two genuine attempts found no history; no response identity is invented."""

    def __init__(self, attempts: tuple[SkillSeriesDiscoveryAttempt, ...]) -> None:
        super().__init__("历史数据查询及一次补查均未返回可用历史数据。")
        self.attempts = len(attempts)
        self.discovery_attempts = attempts


@dataclass(frozen=True, slots=True)
class SkillSeriesDiscoveryResult:
    status: Literal["discovered", "unavailable"]
    instrument_id: str
    metric_query: str
    start: date
    end: date
    request_hash: str
    response_hash: str
    provider: str
    query: str
    retrieved_at: datetime
    response_schema_version: str
    instrument_verified: bool
    tables: tuple[SkillSeriesTableEvidence, ...]
    candidate_table_indices: tuple[int, ...]
    issues: tuple[str, ...]
    attempts: tuple[SkillSeriesDiscoveryAttempt, ...] = ()

    @property
    def daily_frequency_verified(self) -> bool:
        return False

    @property
    def pit_verified(self) -> bool:
        return False


class SkillSeriesDiscovery:
    """Discover provider evidence without interpreting metric names in Python."""

    def __init__(self, provider: LiveFinanceData, *, decoder: FinanceHistoryDecoder) -> None:
        self._provider = provider
        self._decoder = decoder

    @property
    def provider_errors(self) -> tuple[type[Exception], ...]:
        return self._decoder.provider_errors

    @property
    def unavailable_errors(self) -> tuple[type[Exception], ...]:
        return self._decoder.unavailable_errors

    async def discover(
        self, *, instrument_id: str, metric_query: str, start: date, end: date,
        expected_session_dates: tuple[date, ...] | None = None,
        validation_issue: Callable[[SkillSeriesDiscoveryResult], str | None] | None = None,
    ) -> SkillSeriesDiscoveryResult:
        """Check acquisition wording once before blaming unavailable history.

        Retry only a wrong/missing historical shape, never a strategy run or
        the model interpretation. The same stock, metric, parameters and date
        range are kept. Both actual queries/responses remain auditable.
        """
        if expected_session_dates is not None and (
            not expected_session_dates
            or tuple(sorted(set(expected_session_dates))) != expected_session_dates
        ):
            raise ValueError("expected sessions must be nonempty, unique and ascending")
        attempts: list[SkillSeriesDiscoveryAttempt] = []
        initial_result: SkillSeriesDiscoveryResult | None = None
        try:
            result = await self._discover_once(
                instrument_id=instrument_id, metric_query=metric_query, start=start, end=end,
                expected_session_dates=expected_session_dates,
            )
            result = _check_authoritative_sessions(result, expected_session_dates)
            issue = validation_issue(result) if validation_issue is not None else None
            attempts.append(SkillSeriesDiscoveryAttempt(
                result.query, result.response_hash, (*result.issues, *((issue,) if issue else ())),
            ))
            # Retain the actual rejection, not merely the raw discovery status.
            # An empty/failed alternate channel cannot erase a unit conflict
            # found in a real historical response.
            initial_result = replace(
                result, issues=(*result.issues, *((issue,) if issue else ())),
            )
            needs_retry = result.status == "unavailable" and (
                not result.tables or any(any(reason in issue for reason in (
                    "date_axis_missing", "insufficient_history_or_current_only",
                    "raw_table_missing", "dates_outside_requested_range",
                    "invalid_or_non_daily_date_axis",
                    "dates_outside_authoritative_sessions",
                    "no_numeric_observations", "raw_numeric_fields_missing",
                )) for issue in result.issues)
            ) and not any(
                table.entity_codes and not table.instrument_verified for table in result.tables
            ) and not any(any(reason in issue for reason in (
                "security_mismatch_or_ambiguous", "duplicate_dates",
                "field_metadata_missing_or_ambiguous", "non_finite_or_non_numeric_value",
                "field_length_mismatch", "field_values_not_array",
                "timestamp_axis_requires_frequency_verification",
            )) for issue in result.issues)
            # Identity and downstream unit checks share the same one-requery
            # budget. No rejected values are merged with the second response.
            needs_retry = needs_retry or issue is not None or any(
                reason in result.issues
                for reason in ("security_missing", "security_mismatch_or_ambiguous")
            )
            if not needs_retry:
                return replace(result, attempts=tuple(attempts))
        except self._decoder.no_data_errors:
            attempts.append(SkillSeriesDiscoveryAttempt(
                _query_text(instrument_id, metric_query, start, end, compact=False),
                None, ("provider_no_results",),
            ))
        except self._decoder.unavailable_errors:
            attempts.append(SkillSeriesDiscoveryAttempt(
                _query_text(instrument_id, metric_query, start, end, compact=False),
                None, ("provider_temporarily_unavailable",),
            ))
        retry_progress_id = f"skill-history:{uuid4().hex}"
        self._decoder.notify_data_retry(retry_progress_id)
        try:
            result = await self._discover_once(
                instrument_id=instrument_id, metric_query=metric_query, start=start, end=end,
                compact=True,
            )
            result = _check_authoritative_sessions(result, expected_session_dates)
            issue = validation_issue(result) if validation_issue is not None else None
        except self._decoder.no_data_errors as exc:
            attempts.append(SkillSeriesDiscoveryAttempt(
                _query_text(instrument_id, metric_query, start, end, compact=True),
                None, ("provider_no_results",),
            ))
            if initial_result is not None and any(
                reason in initial_result.issues for reason in ("unit_unconfirmed", "unit_mismatch")
            ):
                return replace(initial_result, status="unavailable", attempts=tuple(attempts))
            raise SkillSeriesDiscoveryNoHistoryError(tuple(attempts)) from exc
        except (*self._decoder.invalid_data_errors, *self._decoder.unavailable_errors):
            # Field discovery is useful even when the historical requery fails.
            # Preserve only an actual response; never promote it to executable.
            if initial_result is None:
                raise
            attempts.append(SkillSeriesDiscoveryAttempt(
                _query_text(instrument_id, metric_query, start, end, compact=True),
                None, ("historical_requery_failed",),
            ))
            return replace(initial_result, status="unavailable",
                           issues=(*initial_result.issues, "historical_requery_failed"),
                           attempts=tuple(attempts))
        attempts.append(SkillSeriesDiscoveryAttempt(
            result.query, result.response_hash, (*result.issues, *((issue,) if issue else ())),
        ))
        if result.status == "unavailable" and initial_result is not None and any(
            reason in initial_result.issues for reason in ("unit_unconfirmed", "unit_mismatch")
        ):
            return replace(initial_result, status="unavailable", attempts=tuple(attempts))
        if result.status == "discovered" and issue is None:
            self._decoder.notify_data_retry(retry_progress_id, recovered=True)
        return replace(result, attempts=tuple(attempts))

    async def _discover_once(
        self, *, instrument_id: str, metric_query: str, start: date, end: date,
        compact: bool = False,
        expected_session_dates: tuple[date, ...] | None = None,
    ) -> SkillSeriesDiscoveryResult:
        if str(normalize_a_share_instrument(instrument_id)) != instrument_id:
            raise ValueError("instrument_id must be a canonical A-share symbol")
        if not metric_query.strip():
            raise ValueError("metric_query must not be blank")
        if start > end:
            raise ValueError("historical discovery range is inverted")
        query = _query_text(instrument_id, metric_query, start, end, compact=compact)
        indicators = f"{metric_query}，{start.isoformat()}至{end.isoformat()}，逐日历史数值"
        request = {
            "schema_version": "skill-series-discovery.v1", "instrument_id": instrument_id,
            "metric_query": metric_query, "start": start.isoformat(), "end": end.isoformat(),
            "query": query, "indicators": indicators,
        }
        # Only a declared adapter method enables another channel. Dynamic
        # proxies/mocks must not fabricate a capability on attribute access.
        supports_alternate = compact and callable(
            getattr(type(self._provider), "query_finance_via_screen", None)
        )
        alternate = (
            getattr(self._provider, "query_finance_via_screen", None)
            if supports_alternate else None
        )
        operation = (
            cast(Callable[..., Awaitable[LiveFinanceDataResult]], alternate)
            if callable(alternate) else self._provider.query_finance
        )
        if not compact and callable(getattr(type(self._provider), "query_finance_history", None)):
            history_operation = cast(Callable[..., Awaitable[LiveFinanceDataResult]],
                                     getattr(self._provider, "query_finance_history"))
            result = await history_operation(
                query=query, indicators=indicators, instrument_id=instrument_id, start=start, end=end,
                **({"expected_session_dates": expected_session_dates}
                   if expected_session_dates is not None else {}),
            )
        else:
            result = await operation(query=query, indicators=indicators)
        tables = tuple(
            _table_evidence(table, index=index, instrument_id=instrument_id,
                            start=start, end=end, decoder=self._decoder)
            for index, table in enumerate(result.tables)
        )
        returned_codes = {code for table in tables for code in table.entity_codes}
        instrument_verified = returned_codes == {instrument_id[:6]}
        issues: list[str] = []
        if not instrument_verified:
            issues.append(
                "security_missing" if not returned_codes else "security_mismatch_or_ambiguous"
            )
        eligible = tuple(table for table in tables if table.has_numeric_history)
        indices: tuple[int, ...] = ()
        if eligible:
            longest = max(len(table.dates) for table in eligible)
            winners = tuple(table for table in eligible if len(table.dates) == longest)
            if len({tuple(sorted(table.dates)) for table in winners}) != 1:
                issues.append("ambiguous_historical_date_axes")
            else:
                indices = tuple(table.table_index for table in winners)
                if _conflicting_fields(winners):
                    issues.append("conflicting_historical_fields")
        else:
            issues.append("no_valid_numeric_history")
            issues.extend(
                f"table[{table.table_index}]:{issue}"
                for table in tables for issue in table.issues
            )
            issues.extend(
                f"table[{table.table_index}].{field.return_code}:{issue}"
                for table in tables for field in table.fields for issue in field.issues
            )
        if issues:
            indices = ()
        return SkillSeriesDiscoveryResult(
            status="unavailable" if issues else "discovered",
            instrument_id=instrument_id, metric_query=metric_query, start=start, end=end,
            request_hash="sha256:" + hashlib.sha256(_json(request).encode()).hexdigest(),
            response_hash=result.provenance.response_sha256, provider=result.provider,
            query=result.query, retrieved_at=result.provenance.retrieved_at,
            response_schema_version=result.provenance.schema_version,
            instrument_verified=instrument_verified, tables=tables,
            candidate_table_indices=indices, issues=tuple(issues),
        )


def _query_text(
    instrument_id: str, metric_query: str, start: date, end: date, *, compact: bool,
) -> str:
    if compact:
        return (
            f"{instrument_id}，{start.isoformat()}到{end.isoformat()}，"
            f"{metric_query}，每日历史数据，按交易日期逐行列出。"
        )
    return (
        f"查询{instrument_id}在{start.isoformat()}至{end.isoformat()}每个交易日的"
        f"{metric_query}历史数值。保留原始口径、参数、单位和日期，不返回区间汇总。"
    )


def _table_evidence(
    table: Mapping[str, Any], *, index: int, instrument_id: str, start: date, end: date,
    decoder: FinanceHistoryDecoder,
) -> SkillSeriesTableEvidence:
    codes = decoder.entity_codes(table)
    verified = codes == {instrument_id[:6]}
    issues: list[str] = [] if verified else ["table_security_missing_or_ambiguous"]
    raw = table.get("rawTable")
    raw_table: Mapping[str, Any] = cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
    if not raw_table:
        issues.append("raw_table_missing")
    raw_dates: object = raw_table.get("headName")
    raw_dates_json = _json(raw_dates)
    dates: tuple[date, ...] = ()
    if not isinstance(raw_dates, list) or not raw_dates:
        issues.append("date_axis_missing")
    else:
        date_values = cast(list[object], raw_dates)
        try:
            dates = tuple(decoder.session_date(value) for value in date_values)
        except (*decoder.invalid_data_errors, ValueError):
            issues.append("invalid_or_non_daily_date_axis")
        if dates:
            if any(
                not isinstance(raw_day, str) or raw_day.strip() != day.isoformat()
                for raw_day, day in zip(date_values, dates, strict=True)
            ):
                issues.append("timestamp_axis_requires_frequency_verification")
            if len(dates) != len(set(dates)):
                issues.append("duplicate_dates")
            if any(day < start or day > end for day in dates):
                issues.append("dates_outside_requested_range")
            if len(dates) < 2:
                issues.append("insufficient_history_or_current_only")
    metadata = decoder.field_metadata(table)
    definitions: dict[str, list[Mapping[str, Any]]] = {}
    raw_fields = table.get("fieldSet")
    if isinstance(raw_fields, list):
        for item in cast(list[object], raw_fields):
            if isinstance(item, Mapping):
                definition = cast(Mapping[str, Any], item)
                code = definition.get("returnCode")
                if isinstance(code, str) and code:
                    definitions.setdefault(code, []).append(definition)
    fields = tuple(
        _field_evidence(
            code=code, raw_values=values, definitions=definitions.get(code, []),
            # Shape and date semantics are independent: a quarterly/date-format
            # response may need requery without having corrupted column lengths.
            metadata=metadata.get(code),
            length=len(raw_dates) if isinstance(raw_dates, list) else 0,
        )
        for code, values in raw_table.items() if code != "headName"
    )
    if not fields:
        issues.append("raw_numeric_fields_missing")
    return SkillSeriesTableEvidence(
        table_index=index, entity_codes=tuple(sorted(codes)), instrument_verified=verified,
        metadata_json=_json({key: value for key, value in table.items() if key != "rawTable"}),
        raw_dates_json=raw_dates_json, dates=dates, fields=fields, issues=tuple(issues),
    )


def _field_evidence(
    *, code: str, raw_values: object, definitions: list[Mapping[str, Any]],
    metadata: tuple[str, frozenset[str], str | None] | None, length: int,
) -> SkillSeriesFieldEvidence:
    definition: Mapping[str, Any] = definitions[0] if definitions else {}
    raw_values_json = _json(raw_values)
    issues: list[str] = []
    if len(definitions) != 1:
        issues.append("field_metadata_missing_or_ambiguous")
    values: list[Decimal | None] = []
    missing: list[int] = []
    if not isinstance(raw_values, list):
        issues.append("field_values_not_array")
    else:
        raw_items = cast(list[object], raw_values)
        if len(raw_items) != length:
            issues.append("field_length_mismatch")
        for index, raw_value in enumerate(raw_items):
            if raw_value is None:
                missing.append(index)
                values.append(None)
                continue
            try:
                # The existing helper also maps yes/no to 1/0. Numeric
                # discovery must not silently make that semantic conversion.
                if isinstance(raw_value, bool) or not isinstance(
                    raw_value, str | int | float | Decimal,
                ):
                    raise ValueError("not a number")
                numeric = Decimal(str(raw_value).strip())
                if not numeric.is_finite():
                    raise ValueError("not a finite number")
                values.append(numeric)
            except (ArithmeticError, ValueError):
                values.append(None)
                issues.append(f"non_finite_or_non_numeric_value[{index}]")
        if values and all(value is None for value in values):
            issues.append("no_numeric_observations")
    return SkillSeriesFieldEvidence(
        return_code=code, return_name=_text(definition.get("returnName")),
        display_name=metadata[0] if metadata else None,
        return_source_code=_text(definition.get("returnSourceCode")),
        return_source_name=_text(definition.get("returnSourceName")),
        unit=metadata[2] if metadata else None,
        fixed_param_value=_text(definition.get("fixedParamValue")),
        metadata_json=_json(definitions), raw_values_json=raw_values_json,
        values=tuple(values), missing_indices=tuple(missing), issues=tuple(issues),
    )


def _conflicting_fields(tables: tuple[SkillSeriesTableEvidence, ...]) -> bool:
    seen: dict[str, tuple[str, tuple[tuple[date, Decimal | None], ...]]] = {}
    for table in tables:
        for field in table.fields:
            if not field.has_numeric_history:
                continue
            identity = (
                field.metadata_json, tuple(sorted(zip(table.dates, field.values, strict=True))),
            )
            if field.return_code in seen and seen[field.return_code] != identity:
                return True
            seen[field.return_code] = identity
    return False


def _check_authoritative_sessions(
    result: SkillSeriesDiscoveryResult, sessions: tuple[date, ...] | None,
) -> SkillSeriesDiscoveryResult:
    """Reject the response, never trim extra observations or fill missing ones."""
    if sessions is None:
        return result
    allowed = set(sessions)
    if any(day not in allowed for index in result.candidate_table_indices
           for day in result.tables[index].dates):
        return replace(
            result, status="unavailable", candidate_table_indices=(),
            issues=(*result.issues, "dates_outside_authoritative_sessions"),
        )
    return result


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
