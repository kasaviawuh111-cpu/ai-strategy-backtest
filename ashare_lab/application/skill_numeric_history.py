"""Bind verified Skill numeric histories to generic comparison operators.

Missing availability clocks use the authorised close-time research assumption.
The assumption and original metadata are internal provenance, never PIT proof.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from ashare_lab.application.skill_series_discovery import (
    SkillSeriesDiscovery,
    SkillSeriesDiscoveryAttempt,
    SkillSeriesDiscoveryNoHistoryError,
    SkillSeriesDiscoveryResult,
    SkillSeriesFieldEvidence,
    SkillSeriesTableEvidence,
)
from ashare_lab.domain.historical_units import (
    HistoricalUnitError,
    UnitFieldEvidence,
    UnitTableEvidence,
    numeric_display_scale,
    percent_display_scale,
    unit_scale,
)
from ashare_lab.domain.historical_units import (
    numeric_unit_code as _numeric_unit_code,
)
from ashare_lab.domain.historical_units import (
    unit_definition as _unit_definition,
)
from ashare_lab.domain.signals.provider_catalog import ProviderCatalogError
from ashare_lab.domain.signals.skill_numeric import (
    SKILL_NUMERIC_ID,
    SKILL_SERIES_COMPARE_ID,
    validate_skill_comparison_condition,
)
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)
from ashare_lab.ports.skill_metric_binding import MetricBindingReviewer

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ERRORS = {
    "invalid_condition": "通用指标条件或参数不完整，请检查设置。",
    "query_failed": "这次数据查询未完成，返回结果或服务配置需要检查。你的策略已保留，尚未开始回测。",
    "query_temporarily_unavailable": "抱歉，数据查询服务暂时不太稳定，这次没能取到数据。你的策略已保留，请稍后再试一次。",
    "history_unavailable": "本次未取得可用的逐日历史数值，请调整指标口径或区间。",
    "history_unavailable_after_query_retry": (
        "已按指定股票、指标和日期重新查询，仍未取得本次所需的逐日历史数据；规则已保留。"
    ),
    "security_mismatch": "历史指标返回的股票与本次请求不一致，尚未执行回测。",
    "security_missing": "历史指标缺少可核对的股票标识，尚未执行回测。",
    "ambiguous_field": "接口返回了多个数值字段，暂时无法唯一确定本次指标。",
    "unit_unconfirmed": "历史指标的数值单位尚未确认，暂时无法完成条件比较。",
    "unit_mismatch": "历史指标单位与设置的比较单位不兼容，请检查单位。",
    "adjustment_mismatch": "两条历史指标的复权口径不一致，暂时不能直接比较；尚未开始回测。",
    "invalid_history": "历史指标的数据结构或字段绑定不一致，尚未执行回测。",
    "session_mismatch": (
        "历史指标返回了行情交易日之外的日期，自动补查后仍未对齐；"
        "这些数值未参与计算，原规则已保留，本次未执行回测。"
    ),
    "non_daily_history": "接口返回的指标频率不是日线，不能用于当前日线回测。",
    "unaligned_history": "两条历史指标没有可按同一交易日配对的数值，暂时无法比较。",
    "field_mismatch": "查数返回的指标或参数与本次规则不一致，暂时不能开始回测；原规则已保留。",
    "field_unconfirmed": (
        "查数返回的字段尚不足以确认本次指标及参数，暂时不能开始回测；原规则已保留。"
    ),
    "field_review_unavailable": "本次指标字段核对暂时未完成，请稍后重新读取；尚未开始回测。",
}
_AVAILABILITY_KEYS = frozenset({
    "firstavailableat", "availableat", "publishedat", "publicationtime", "publishtime",
})


class SkillNumericHistoryError(ValueError):
    """Safe, fixed diagnostic without provider text or request secrets."""

    def __init__(
        self, code: str, message: str | None = None, *,
        discovery_attempts: tuple[SkillSeriesDiscoveryAttempt, ...] = (),
    ) -> None:
        # Do not echo a caller/provider message into the user-visible channel.
        self.code = code if code in _ERRORS else "invalid_history"
        self.message = _ERRORS[self.code]
        self.discovery_attempts = discovery_attempts
        # Filled by the preparation layers from the accepted condition, never
        # from provider prose. These make a failed comparison traceable.
        self.metric_query: str | None = None
        self.indicator_id: str | None = None
        self.condition_path: str | None = None
        super().__init__(self.message)


async def prepare_skill_numeric_series(
    discovery: SkillSeriesDiscovery, *, condition: IndicatorCondition,
    instrument_id: str, start: date, end: date,
    binding_reviewer: MetricBindingReviewer | None = None,
    expected_session_dates: tuple[date, ...] | None = None,
) -> ProviderIndicatorSeries:
    try:
        validate_skill_comparison_condition(condition)
    except (ProviderCatalogError, ValueError) as exc:
        raise SkillNumericHistoryError("invalid_condition") from exc
    comparison_unit = cast(str, condition.params["unit"])

    async def load_metric(query: str) -> ProviderIndicatorSeries:
        try:
            return await _prepare_skill_metric_series(
                discovery, metric_query=query, comparison_unit=comparison_unit,
                instrument_id=instrument_id, start=start, end=end,
                expected_session_dates=expected_session_dates,
            )
        except SkillNumericHistoryError as exc:
            exc.metric_query = query
            raise

    if condition.indicator_id == SKILL_SERIES_COMPARE_ID:
        left_query = cast(str, condition.params["left_metric_query"])
        right_query = cast(str, condition.params["right_metric_query"])
        left = await load_metric(left_query)
        right = left if right_query == left_query else await load_metric(right_query)
        if binding_reviewer is not None:
            left, right = await _review_metric_bindings(
                binding_reviewer, ((left_query, left), (right_query, right)),
            )
        return _pair_skill_series(left, right)
    query = cast(str, condition.params["metric_query"])
    series = await load_metric(query)
    if binding_reviewer is not None:
        (series,) = await _review_metric_bindings(binding_reviewer, ((query, series),))
    return series


async def _review_metric_bindings(
    reviewer: MetricBindingReviewer,
    sources: tuple[tuple[str, ProviderIndicatorSeries], ...],
) -> tuple[ProviderIndicatorSeries, ...]:
    """Model owns field meaning; no second Chinese parser or per-metric allowlist.

    Live composition always supplies the reviewer. Direct low-level fixtures
    may omit it to exercise unit/date binding independently of language calls.
    """
    bindings: list[tuple[str, Mapping[str, object]]] = []
    for query, series in sources:
        metadata = json.loads(series.points[0].values[0].source_parameters or "{}")
        bindings.append((query, metadata["fieldMetadata"]))
    verdicts = await reviewer.verify(tuple(bindings))
    if len(verdicts) != len(sources):
        raise SkillNumericHistoryError("field_review_unavailable")
    for (query, _), verdict in zip(sources, verdicts, strict=True):
        if not verdict.matched:
            error = SkillNumericHistoryError(
                "field_review_unavailable" if verdict.reason_code == "review_unavailable"
                else "field_mismatch" if verdict.verdict == "mismatch" else "field_unconfirmed"
            )
            error.metric_query = query
            raise error
    reviewed: list[ProviderIndicatorSeries] = []
    for (_, series), verdict in zip(sources, verdicts, strict=True):
        proof = {
            "verdict": verdict.verdict, "bindingHash": verdict.binding_hash,
            "provider": verdict.provider, "model": verdict.model,
            "promptVersion": verdict.prompt_version,
        }
        points: list[ProviderIndicatorPoint] = []
        for point in series.points:
            value = point.values[0]
            parameters = json.loads(value.source_parameters or "{}")
            parameters["fieldBindingReview"] = proof
            parameters["bindingHash"] = "sha256:" + hashlib.sha256(_json({
                "dataBinding": parameters["bindingHash"], "semanticBinding": proof,
            }).encode()).hexdigest()
            points.append(replace(point, values=(
                replace(value, source_parameters=_json(parameters)),
            )))
        reviewed.append(replace(series, points=tuple(points)))
    return tuple(reviewed)


async def _prepare_skill_metric_series(
    discovery: SkillSeriesDiscovery, *, metric_query: str, comparison_unit: str,
    instrument_id: str, start: date, end: date,
    expected_session_dates: tuple[date, ...] | None = None,
) -> ProviderIndicatorSeries:
    """One independently verified source; comparison syntax never alters its evidence."""
    def unit_issue(result: SkillSeriesDiscoveryResult) -> str | None:
        if result.status != "discovered" or not result.instrument_verified:
            return None
        try:
            scales = tuple(_unit_scale(table, field, comparison_unit)
                           for table, field in _unique_field(result))
            return "unit_unconfirmed" if len(set(scales)) != 1 else None
        except SkillNumericHistoryError as exc:
            return exc.code if exc.code in {"unit_unconfirmed", "unit_mismatch"} else None

    try:
        result = await discovery.discover(
            instrument_id=instrument_id, metric_query=metric_query, start=start, end=end,
            validation_issue=unit_issue,
            **({"expected_session_dates": expected_session_dates}
               if expected_session_dates is not None else {}),
        )
    except SkillSeriesDiscoveryNoHistoryError as exc:
        raise SkillNumericHistoryError(
            "history_unavailable_after_query_retry", discovery_attempts=exc.discovery_attempts,
        ) from exc
    except (*discovery.unavailable_errors, OSError, TimeoutError) as exc:
        raise SkillNumericHistoryError("query_temporarily_unavailable") from exc
    except discovery.provider_errors as exc:
        raise SkillNumericHistoryError("query_failed") from exc
    except ValueError as exc:
        raise SkillNumericHistoryError("invalid_condition") from exc
    if not result.instrument_verified or result.instrument_id != instrument_id:
        code = (
            "security_missing" if result.instrument_id == instrument_id
            and "security_missing" in result.issues else "security_mismatch"
        )
        raise SkillNumericHistoryError(code, discovery_attempts=result.attempts)
    if result.status != "discovered" or not result.candidate_table_indices:
        raise _history_unavailable(result)
    if result.metric_query != metric_query or (result.start, result.end) != (start, end):
        raise SkillNumericHistoryError("invalid_history")
    candidates = _unique_field(result)
    table, field = candidates[0]
    try:
        converted = tuple(_unit_scale(item, actual, comparison_unit) for item, actual in candidates)
    except SkillNumericHistoryError as exc:
        exc.discovery_attempts = result.attempts
        raise
    if len(set(converted)) != 1:
        raise SkillNumericHistoryError("unit_unconfirmed", discovery_attempts=result.attempts)
    scale = converted[0]
    field_metadata = _field_definition(field)
    source_units = {
        key: field_metadata[key] for key in ("unit", "unitName", "unitDesc")
        if key in field_metadata
    }
    unit_proof: dict[str, object] = {"sourceMetadata": source_units}
    if any(_unit_text(str(value)) == "100%" for value in source_units.values()):
        _, samples, count = _percent_display_scale(table, field)
        unit_proof.update({"displaySamples": samples, "verifiedDisplayCount": count})
    elif _numeric_unit_code(field_metadata) not in (None, Decimal(1)):
        target_label = _unit_text(comparison_unit)
        target_label = {"万": "万元", "亿": "亿元"}.get(target_label, target_label)
        dimension = _unit_definition(target_label)[0]
        _, samples, count = _numeric_display_scale(table, field, dimension)
        unit_proof.update({"displaySamples": samples, "verifiedDisplayCount": count})
    source_metadata = {
        "requestHash": result.request_hash, "responseHash": result.response_hash,
        "returnCode": field.return_code, "returnSourceCode": field.return_source_code,
        "fixedParamValue": field.fixed_param_value, "sourceUnit": field.unit,
        "comparisonUnit": comparison_unit, "scale": str(scale),
        "fieldMetadata": {
            **_compact_field_metadata(field_metadata),
            **({"display_name": field.display_name} if field.display_name else {}),
            **({"source_name": field.return_source_name} if field.return_source_name else {}),
        },
        "tableMetadata": [_compact_table_metadata(item) for item, _ in candidates],
        "unitProof": unit_proof,
        "attempts": [
            {
                "query": attempt.query, "responseHash": attempt.response_hash,
                "issues": attempt.issues,
            }
            for attempt in result.attempts
        ],
        "availabilityPolicy": "research_replay_close_when_missing.v1",
        "pitVerified": False,
    }
    binding_hash = "sha256:" + hashlib.sha256(_json(source_metadata).encode()).hexdigest()
    source_metadata["bindingHash"] = binding_hash
    base = ProviderIndicatorValue(
        field_code=field.return_code, field_name="value", value=Decimal(0),
        unit=comparison_unit, source_field_name=field.return_name or field.display_name,
        source_unit=field.unit,
    )
    points: list[ProviderIndicatorPoint] = []
    clock_sources = tuple(
        ({day: index for index, day in enumerate(item.dates)}, _availability_columns(item, actual))
        for item, actual in candidates
    )
    for day, value in zip(table.dates, field.values, strict=True):
        if value is None:
            continue
        observed_at = datetime.combine(day, time(15), tzinfo=_SHANGHAI)
        raw_clocks: list[object] = []
        for positions, columns in clock_sources:
            raw_clocks.extend(column[positions[day]] for column in columns)
        available_at, clock_evidence = _availability(raw_clocks, observed_at)
        parameters = _json({**source_metadata, **clock_evidence})
        points.append(ProviderIndicatorPoint(
            session_date=day, observed_at=observed_at, first_available_at=available_at,
            values=(replace(base, value=value * scale, source_parameters=parameters),),
        ))
    if not points:
        raise _history_unavailable(result)
    return ProviderIndicatorSeries(
        provider=result.provider, instrument_id=instrument_id, indicator_id=SKILL_NUMERIC_ID,
        requested_start=start, requested_end=end,
        points=tuple(sorted(points, key=lambda point: point.session_date)),
        response_sha256=result.response_hash, retrieved_at=result.retrieved_at,
        schema_version="skill-numeric-history.v1", query=result.query,
    )


def _pair_skill_series(
    left: ProviderIndicatorSeries, right: ProviderIndicatorSeries,
) -> ProviderIndicatorSeries:
    """Inner join actual observations by date, without fabricating missing sessions."""
    if (left.provider, left.instrument_id, left.requested_start, left.requested_end) != (
        right.provider, right.instrument_id, right.requested_start, right.requested_end,
    ):
        raise SkillNumericHistoryError("invalid_history")
    left_adjustment, right_adjustment = _declared_adjustment(left), _declared_adjustment(right)
    if left_adjustment and right_adjustment and left_adjustment != right_adjustment:
        raise SkillNumericHistoryError("adjustment_mismatch")
    right_by_date = {point.session_date: point for point in right.points}
    points: list[ProviderIndicatorPoint] = []
    for left_point in left.points:
        right_point = right_by_date.get(left_point.session_date)
        if right_point is None:
            continue
        if len(left_point.values) != 1 or len(right_point.values) != 1:
            raise SkillNumericHistoryError("invalid_history")
        if left_point.values[0].unit != right_point.values[0].unit:
            raise SkillNumericHistoryError("unit_mismatch")
        values = tuple(
            replace(point.values[0], field_name=f"{side}_value", source_parameters=_json({
                **json.loads(point.values[0].source_parameters or "{}"),
                "operand": side,
                "sourceQuery": series.query,
                "sourceRetrievedAt": series.retrieved_at.isoformat(),
                "sourceSeriesResponseHash": series.response_sha256,
                "sourceObservedAt": point.observed_at.isoformat(),
                "sourceFirstAvailableAt": point.first_available_at.isoformat(),
            }))
            for side, point, series in (("left", left_point, left), ("right", right_point, right))
        )
        points.append(ProviderIndicatorPoint(
            session_date=left_point.session_date,
            observed_at=max(left_point.observed_at, right_point.observed_at),
            first_available_at=max(left_point.first_available_at, right_point.first_available_at),
            values=values,
        ))
    if not points:
        raise SkillNumericHistoryError("unaligned_history")
    combined_hash = "sha256:" + hashlib.sha256(_json({
        "schema": "skill-series-comparison.v1", "leftResponseHash": left.response_sha256,
        "rightResponseHash": right.response_sha256,
        "leftQuery": left.query, "rightQuery": right.query,
    }).encode()).hexdigest()
    return ProviderIndicatorSeries(
        provider=left.provider, instrument_id=left.instrument_id,
        indicator_id=SKILL_SERIES_COMPARE_ID,
        requested_start=left.requested_start, requested_end=left.requested_end,
        points=tuple(points), response_sha256=combined_hash,
        retrieved_at=max(left.retrieved_at, right.retrieved_at),
        schema_version="skill-series-comparison.v1",
        query=_json({"left": left.query, "right": right.query}),
    )


def _declared_adjustment(series: ProviderIndicatorSeries) -> str | None:
    """Compare explicit provider parameters, never reinterpret the user's Chinese.

    Missing metadata is not invented. Contradictory declared bases, including
    within one series, cannot be combined even when each metric matches its name.
    """
    declared: set[str] = set()
    for point in series.points:
        for value in point.values:
            metadata = json.loads(value.source_parameters or "{}")
            fields = metadata.get("fieldMetadata", {})
            for key, raw in fields.items():
                if key.casefold() == "adjustflag":
                    declared.add(str(raw).strip())
            raw_params = metadata.get("fixedParamValue")
            if isinstance(raw_params, str):
                for item in raw_params.split(","):
                    key, separator, raw = item.partition("=")
                    if separator and key.strip().casefold() == "adjustflag":
                        declared.add(raw.strip())
    declared.discard("")
    if len(declared) > 1:
        raise SkillNumericHistoryError("adjustment_mismatch")
    return next(iter(declared), None)


def _unique_field(
    result: SkillSeriesDiscoveryResult,
) -> tuple[tuple[SkillSeriesTableEvidence, SkillSeriesFieldEvidence], ...]:
    groups: dict[str, list[tuple[SkillSeriesTableEvidence, SkillSeriesFieldEvidence]]] = {}
    for index in result.candidate_table_indices:
        if index < 0 or index >= len(result.tables):
            raise SkillNumericHistoryError("invalid_history")
        table = result.tables[index]
        if not table.instrument_verified or table.issues:
            raise SkillNumericHistoryError("invalid_history")
        for field in table.fields:
            all_missing = bool(field.values) and len(field.missing_indices) == len(field.values)
            partly_numeric = any(value is not None for value in field.values)
            if _is_availability_field(field) or not (
                field.has_numeric_history or all_missing or partly_numeric
            ):
                continue
            key = _json({
                "returnCode": field.return_code, "returnSourceCode": field.return_source_code,
                "metadata": json.loads(field.metadata_json),
            })
            groups.setdefault(key, []).append((table, field))
    if len(groups) != 1:
        raise SkillNumericHistoryError("ambiguous_field" if groups else "history_unavailable")
    candidates = tuple(next(iter(groups.values())))
    if any(not field.has_numeric_history for _, field in candidates):
        raise SkillNumericHistoryError("invalid_history")
    for table, field in candidates:
        for metadata in (_field_definition(field), json.loads(table.metadata_json)):
            for name in ("dateGranularity", "frequency", "timeframe"):
                frequency = metadata.get(name)
                if frequency is not None and str(frequency).upper() not in {
                    "DAY", "DAILY", "1D", "D",
                }:
                    raise SkillNumericHistoryError("non_daily_history")
    values = {
        tuple(sorted(zip(table.dates, field.values, strict=True))) for table, field in candidates
    }
    if len(values) != 1:
        raise SkillNumericHistoryError("invalid_history")
    return candidates


def _history_unavailable(result: SkillSeriesDiscoveryResult) -> SkillNumericHistoryError:
    # Missing observations can be re-queried; malformed evidence is a distinct
    # acquisition failure, not proof that the requested metric has no history.
    for reason in ("unit_mismatch", "unit_unconfirmed"):
        if reason in result.issues:
            return SkillNumericHistoryError(reason, discovery_attempts=result.attempts)
    if "dates_outside_authoritative_sessions" in result.issues:
        return SkillNumericHistoryError("session_mismatch", discovery_attempts=result.attempts)
    if any("table_security_missing_or_ambiguous" in issue for issue in result.issues):
        return SkillNumericHistoryError("security_missing", discovery_attempts=result.attempts)
    if any(any(reason in issue for reason in (
        "duplicate_dates", "field_length_mismatch", "field_values_not_array",
        "field_metadata_missing_or_ambiguous", "non_finite_or_non_numeric_value",
        "ambiguous_historical_date_axes", "conflicting_historical_fields",
        "invalid_or_non_daily_date_axis", "timestamp_axis_requires_frequency_verification",
        "dates_outside_requested_range",
    )) for issue in result.issues):
        return SkillNumericHistoryError("invalid_history", discovery_attempts=result.attempts)
    return SkillNumericHistoryError(
        "history_unavailable_after_query_retry" if len(result.attempts) > 1
        else "history_unavailable", discovery_attempts=result.attempts,
    )


def _unit_inputs(
    table: SkillSeriesTableEvidence, field: SkillSeriesFieldEvidence,
) -> tuple[UnitTableEvidence, UnitFieldEvidence]:
    return (
        UnitTableEvidence(metadata_json=table.metadata_json, dates=table.dates),
        UnitFieldEvidence(metadata_json=field.metadata_json, unit=field.unit,
                          return_code=field.return_code, values=field.values),
    )


def _unit_scale(
    table: SkillSeriesTableEvidence, field: SkillSeriesFieldEvidence, target: str,
) -> Decimal:
    try:
        return unit_scale(*_unit_inputs(table, field), target)
    except HistoricalUnitError as exc:
        raise SkillNumericHistoryError(exc.code) from exc


def _numeric_display_scale(
    table: SkillSeriesTableEvidence, field: SkillSeriesFieldEvidence, dimension: str,
) -> tuple[Decimal, tuple[dict[str, str], ...], int]:
    try:
        return numeric_display_scale(*_unit_inputs(table, field), dimension)
    except HistoricalUnitError as exc:
        raise SkillNumericHistoryError(exc.code) from exc


def _percent_display_scale(
    table: SkillSeriesTableEvidence, field: SkillSeriesFieldEvidence,
) -> tuple[Decimal, tuple[dict[str, str], ...], int]:
    try:
        return percent_display_scale(*_unit_inputs(table, field))
    except HistoricalUnitError as exc:
        raise SkillNumericHistoryError(exc.code) from exc


def _availability_columns(
    table: SkillSeriesTableEvidence, field: SkillSeriesFieldEvidence,
) -> tuple[tuple[object, ...], ...]:
    columns: list[tuple[object, ...]] = []
    table_metadata = cast(dict[str, Any], json.loads(table.metadata_json))
    for metadata in (_field_definition(field), table_metadata):
        for key, value in metadata.items():
            if _key(key) in _AVAILABILITY_KEYS:
                if isinstance(value, list):
                    items = cast(list[object], value)
                    if len(items) == len(table.dates):
                        columns.append(tuple(items))
                elif isinstance(value, Mapping):
                    by_date = cast(Mapping[str, object], value)
                    columns.append(tuple(by_date.get(day.isoformat()) for day in table.dates))
                else:
                    columns.append((value,) * len(table.dates))
    for candidate in table.fields:
        if _is_availability_field(candidate):
            raw: object = json.loads(candidate.raw_values_json)
            if isinstance(raw, list):
                items = cast(list[object], raw)
                if len(items) == len(table.dates):
                    columns.append(tuple(items))
    return tuple(columns)


def _compact_field_metadata(metadata: dict[str, Any]) -> dict[str, object]:
    return {
        key: value for key, value in metadata.items()
        if _key(key) not in {"jumpurl", "url"}
        and not (_key(key) in _AVAILABILITY_KEYS and isinstance(value, (list, Mapping)))
    }


def _compact_table_metadata(table: SkillSeriesTableEvidence) -> dict[str, object]:
    metadata = cast(dict[str, Any], json.loads(table.metadata_json))
    return {
        "entityCodes": table.entity_codes,
        "fieldSet": [_compact_field_metadata(_field_definition(field)) for field in table.fields
                     if len(cast(list[object], json.loads(field.metadata_json))) == 1],
        **{
            key: metadata[key] for key in ("dateGranularity", "frequency", "timeframe",
                                          "reportAsOfAudit", "sourceResponseHashes", "pitVerified")
            if key in metadata
        },
        "availabilityMetadataFields": tuple(
            key for key in metadata if _key(key) in _AVAILABILITY_KEYS
        ),
    }


def _availability(
    raw_values: list[object], observed_at: datetime,
) -> tuple[datetime, dict[str, object]]:
    parsed: list[datetime] = []
    assumed_precision = False
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
                parsed.append(
                    datetime.combine(date.fromisoformat(value.strip()), time(15), _SHANGHAI)
                )
                assumed_precision = True
                continue
            instant = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=_SHANGHAI)
                assumed_precision = True
            parsed.append(instant.astimezone(_SHANGHAI))
        except ValueError:
            continue
    return max([observed_at, *parsed]), {
        "assumedTime": not parsed or assumed_precision,
        "sourceAvailableAt": raw_values,
        "effectiveAvailableAt": max([observed_at, *parsed]).isoformat(),
        "availabilityTimeZone": "Asia/Shanghai",
    }


def _is_availability_field(field: SkillSeriesFieldEvidence) -> bool:
    return any(
        _key(value) in _AVAILABILITY_KEYS
        for value in (field.return_code, field.return_source_code or "")
    )


def _field_definition(field: SkillSeriesFieldEvidence) -> dict[str, Any]:
    definitions: object = json.loads(field.metadata_json)
    if not isinstance(definitions, list):
        raise SkillNumericHistoryError("invalid_history")
    items = cast(list[object], definitions)
    if len(items) != 1:
        raise SkillNumericHistoryError("invalid_history")
    definition = items[0]
    if not isinstance(definition, dict):
        raise SkillNumericHistoryError("invalid_history")
    return cast(dict[str, Any], definition)


def _key(value: str) -> str:
    return value.replace("_", "").casefold()


def _unit_text(value: str) -> str:
    return value.strip().replace("％", "%").casefold()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
