"""Reshape actual dated screener columns, without certifying historical coverage.

No query parsing, date expansion, indicator calculation or semantic field mapping
belongs here. The existing history consumer must still bind the requested data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, cast

from ashare_lab.domain.historical_units import unit_definition
from ashare_lab.ports.live_market_data import LiveMarketDataResult

_DAY = re.compile(r"(\d{4})[-./](\d{2})[-./](\d{2})")
_NUMBER = re.compile(
    r"\s*([+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*(\S*)\s*",
)
_MULTIPLIERS = {"万亿": Decimal(10) ** 12, "亿": Decimal(10) ** 8, "万": Decimal(10) ** 4}
_CODE_KEYS = {"SECURITY_CODE", "SECUCODE", "SECU_CODE", "STOCK_CODE"}


class MxScreenHistoryFormatError(ValueError):
    """The returned screener shape is ambiguous or internally inconsistent."""


def screen_history_tables(result: LiveMarketDataResult) -> tuple[Mapping[str, Any], ...]:
    """Return one table per returned entity/metric, on only its actual date axis."""
    raw_columns = result.provider_metadata.get("columns")
    if raw_columns is None or not result.rows:
        return ()
    if not isinstance(raw_columns, (list, tuple)):
        raise MxScreenHistoryFormatError("screen history columns are invalid")
    columns: list[Mapping[str, Any]] = []
    for item in cast(list[object] | tuple[object, ...], raw_columns):
        if not isinstance(item, Mapping):
            raise MxScreenHistoryFormatError("screen history column is invalid")
        columns.append(cast(Mapping[str, Any], item))
    dated = [(column, day) for column in columns
             if (day := _single_day(column.get("dateMsg"))) is not None]
    if not dated:
        return ()
    label_keys: dict[str, set[str]] = {}
    for column, _ in dated:
        label_keys.setdefault(_label(column), set()).add(str(column.get("key")))
    code_columns = [column for column in columns
                    if column.get("key") in _CODE_KEYS or column.get("indexName") in _CODE_KEYS]
    tables: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in result.rows:
        if any(label in row and len(keys) > 1 for label, keys in label_keys.items()):
            raise MxScreenHistoryFormatError("screen history normalized column label is ambiguous")
        codes = {_entity_code(_cell(row, column)) for column in code_columns}
        if len(codes) != 1:
            raise MxScreenHistoryFormatError("screen history entity identity is ambiguous")
        code = next(iter(codes))
        groups: dict[str, list[tuple[Mapping[str, Any], date]]] = {}
        for column, day in dated:
            index_name = column.get("indexName")
            if not isinstance(index_name, str) or not index_name.strip():
                raise MxScreenHistoryFormatError("screen history metric identity is missing")
            groups.setdefault(index_name, []).append((column, day))
        for index_name, items in groups.items():
            if (code, index_name) in seen:
                raise MxScreenHistoryFormatError("screen history contains duplicate entity metrics")
            seen.add((code, index_name))
            items.sort(key=lambda item: item[1])
            dates = [day.isoformat() for _, day in items]
            if len(dates) != len(set(dates)):
                raise MxScreenHistoryFormatError("screen history contains duplicate metric dates")
            titles = {_title(column, source=True) for column, _ in items}
            units = {_unit(column) for column, _ in items}
            if len(titles) != 1 or len(units) != 1:
                raise MxScreenHistoryFormatError(
                    "screen history metric metadata changes across dates",
                )
            title, unit = next(iter(titles)), next(iter(units))
            displayed = [_cell(row, column) for column, _ in items]
            values = [_numeric_display(value, unit) for value in displayed]
            field: dict[str, Any] = {
                "returnCode": index_name, "returnSourceCode": index_name,
                "returnName": title, "dateGranularity": "DAY",
            }
            if unit:
                field["unitName"] = unit
            tables.append({
                "entityCode": code, "dateGranularity": "DAY", "fieldSet": [field],
                "nameMap": {index_name: title},
                "rawTable": {"headName": dates, index_name: values},
                "table": {"headName": dates, index_name: displayed},
                "screenHistoryEvidence": {
                    "provider": result.provider,
                    "responseSha256": result.provenance.response_sha256,
                    "retrievedAt": result.provenance.retrieved_at.isoformat(),
                    "columns": [dict(column) for column, _ in items],
                    "normalization": "dated_columns_only; explicit_display_unit_scale_only",
                },
            })
    return tuple(tables)


def _title(column: Mapping[str, Any], *, source: bool = False) -> str:
    value = ((column.get("title") if source else None)
             or column.get("displayName") or column.get("title") or column.get("label"))
    if not isinstance(value, str) or not value.strip():
        raise MxScreenHistoryFormatError("screen history column title is missing")
    return value.strip()


def _cell(row: Mapping[str, Any], column: Mapping[str, Any]) -> object:
    # The upstream adapter emits this exact title/date label. Never fall back
    # to an undated title for a dated column: that would broadcast a snapshot.
    label = _label(column)
    if label in row:
        return row[label]
    key = column.get("field") or column.get("name") or column.get("key")
    return row.get(key) if isinstance(key, str) else None


def _label(column: Mapping[str, Any]) -> str:
    title = _title(column)
    date_message = column.get("dateMsg")
    return f"{title} {date_message.strip()}" if isinstance(date_message, str) else title


def _single_day(value: object) -> date | None:
    if not isinstance(value, str) or (match := _DAY.fullmatch(value.strip())) is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError as exc:
        raise MxScreenHistoryFormatError("screen history date is invalid") from exc


def _entity_code(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999999:
        return f"{value:06d}"
    if isinstance(value, str) and re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", value.strip().upper()):
        return value.strip().upper()
    raise MxScreenHistoryFormatError("screen history entity code is invalid")


def _unit(column: Mapping[str, Any]) -> str | None:
    unit = column.get("unit")
    return unit.strip().replace("％", "%") if isinstance(unit, str) and unit.strip() else None


def _numeric_display(value: object, unit: str | None) -> object:
    # Keep missing/unknown/non-numeric content for the history validator. A
    # absent unit never becomes an inferred currency, share count or ratio.
    if not isinstance(value, str) or not unit:
        return value
    match = _NUMBER.fullmatch(value)
    if match is None:
        return value
    number, suffix = Decimal(match[1].replace(",", "")), match[2].replace("％", "%")
    if not suffix or suffix == unit:
        return str(number)
    if suffix in _MULTIPLIERS:
        return str(number * _MULTIPLIERS[suffix])
    source_dimension, source_scale = unit_definition(suffix)
    target_dimension, target_scale = unit_definition(unit)
    if source_dimension == target_dimension:
        return str(number * source_scale / target_scale)
    for prefix, multiplier in _MULTIPLIERS.items():
        if suffix.startswith(prefix):
            dimension, scale = unit_definition(suffix[len(prefix):])
            if dimension == target_dimension:
                return str(number * multiplier * scale / target_scale)
    raise MxScreenHistoryFormatError("screen history display unit conflicts with column unit")
