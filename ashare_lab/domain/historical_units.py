"""Pure, evidence-backed numeric unit normalization shared by data bindings.

No metric name interpretation, external I/O, or indicator calculation belongs here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast


class HistoricalUnitError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UnitTableEvidence:
    metadata_json: str
    dates: tuple[date, ...]


@dataclass(frozen=True)
class UnitFieldEvidence:
    metadata_json: str
    unit: str | None
    return_code: str
    values: tuple[Decimal | None, ...]


_UNITS: dict[str, tuple[str, Decimal]] = {
    "元": ("cny", Decimal(1)), "人民币元": ("cny", Decimal(1)),
    "cny": ("cny", Decimal(1)), "rmb": ("cny", Decimal(1)),
    "万元": ("cny", Decimal(10_000)), "亿元": ("cny", Decimal(100_000_000)),
    "股": ("shares", Decimal(1)), "手": ("shares", Decimal(100)),
    "万股": ("shares", Decimal(10_000)),
    "%": ("percentage_points", Decimal(1)), "百分点": ("percentage_points", Decimal(1)),
    "百分比": ("percentage_points", Decimal(1)),
    "倍": ("multiple", Decimal(1)),
    "1": ("dimensionless", Decimal(1)), "无单位": ("dimensionless", Decimal(1)),
}


def _unit_scale(
    table: UnitTableEvidence, field: UnitFieldEvidence, target: str,
) -> Decimal:
    metadata = _field_definition(field)
    labels = tuple(
        _unit_text(value) for value in (
            metadata.get("unitName"), metadata.get("unitDesc"), field.unit,
        ) if isinstance(value, str) and value.strip()
    )
    raw_unit = metadata.get("unit")
    numeric_unit: Decimal | None = None
    if isinstance(raw_unit, bool):
        raise HistoricalUnitError("unit_unconfirmed")
    if raw_unit is not None and not isinstance(raw_unit, bool):
        try:
            numeric_unit = Decimal(str(raw_unit))
        except ArithmeticError:
            if isinstance(raw_unit, str) and raw_unit.strip():
                labels += (_unit_text(raw_unit),)
    if not labels:
        raise HistoricalUnitError("unit_unconfirmed")
    percentage_ambiguous = "100%" in labels
    if percentage_ambiguous:
        if any(label not in {"100%", "%", "百分点", "百分比"} for label in labels):
            raise HistoricalUnitError("unit_unconfirmed")
        source_dimension = "percentage_points"
        source_scale, _, _ = _percent_display_scale(table, field)
    else:
        definitions = {_unit_definition(label) for label in labels}
        if len(definitions) != 1:
            raise HistoricalUnitError("unit_unconfirmed")
        definition = next(iter(definitions))
        source_dimension, source_scale = definition
        if numeric_unit is not None and numeric_unit != 1:
            # MX can return numeric unit metadata (e.g. unit="2", unitName="元")
            # while rawTable and its formatted table both contain yuan values.
            # Do not assume the numeric code is a multiplier or ignore it:
            # establish the raw scale from matching dated values and explicit units.
            source_scale, _, _ = _numeric_display_scale(table, field, source_dimension)
    target_label = _unit_text(target)
    if source_dimension == "cny":
        target_label = {"万": "万元", "亿": "亿元"}.get(target_label, target_label)
    requested = _unit_definition(target_label)
    if requested[0] != source_dimension:
        raise HistoricalUnitError("unit_mismatch")
    return source_scale / requested[1]


def _unit_definition(label: str) -> tuple[str, Decimal]:
    # The conversion table is not a whitelist of all financial metrics. An
    # explicit unit outside it is usable unchanged only against that same unit.
    # Never infer a conversion between unknown units or interpret their names.
    return _UNITS.get(label, ("exact:" + label, Decimal(1)))


def _numeric_unit_code(metadata: Mapping[str, object]) -> Decimal | None:
    try:
        return Decimal(str(metadata["unit"])) if "unit" in metadata else None
    except ArithmeticError:
        return None


def _numeric_display_scale(
    table: UnitTableEvidence, field: UnitFieldEvidence, dimension: str,
) -> tuple[Decimal, tuple[dict[str, str], ...], int]:
    """Resolve coded metadata from actual dated raw/display pairs, not value size."""
    metadata = cast(dict[str, Any], json.loads(table.metadata_json))
    displayed = metadata.get("table")
    if not isinstance(displayed, Mapping):
        raise HistoricalUnitError("unit_unconfirmed")
    displayed = cast(Mapping[str, Any], displayed)
    raw_dates, raw_values = displayed.get("headName"), displayed.get(field.return_code)
    if not isinstance(raw_dates, list) or not isinstance(raw_values, list):
        raise HistoricalUnitError("unit_unconfirmed")
    dates = tuple(_display_date(value) for value in cast(list[object], raw_dates))
    values = cast(list[object], raw_values)
    if len(dates) != len(values) or len(set(dates)) != len(dates):
        raise HistoricalUnitError("unit_unconfirmed")
    by_date = dict(zip(dates, values, strict=True))
    scales = {scale for unit_dimension, scale in _UNITS.values() if unit_dimension == dimension}
    if dimension.startswith("exact:"):
        scales = {Decimal(1)}
    samples: list[dict[str, str]] = []
    count = 0
    for day, raw in zip(table.dates, field.values, strict=True):
        if raw is None:
            continue
        display = by_date.get(day.isoformat())
        if not isinstance(display, str):
            raise HistoricalUnitError("unit_unconfirmed")
        match = re.fullmatch(
            r"\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(\S+)\s*", display,
        )
        if match is None:
            raise HistoricalUnitError("unit_unconfirmed")
        shown = Decimal(match[1].replace(",", ""))
        definition = _unit_definition(_unit_text(match[2]))
        if definition[0] != dimension:
            raise HistoricalUnitError("unit_unconfirmed")
        display_scale = definition[1]
        tolerance = Decimal("0.5") * Decimal(10) ** cast(int, shown.as_tuple().exponent)
        scales = {scale for scale in scales
                  if abs(raw * scale - shown * display_scale) <= tolerance * display_scale}
        if raw != 0 and shown != 0:
            count += 1
            if len(samples) < 3:
                samples.append({"date": day.isoformat(), "raw": str(raw), "display": display})
    if count < 2 or len(scales) != 1:
        raise HistoricalUnitError("unit_unconfirmed")
    return next(iter(scales)), tuple(samples), count


def _percent_display_scale(
    table: UnitTableEvidence, field: UnitFieldEvidence,
) -> tuple[Decimal, tuple[dict[str, str], ...], int]:
    metadata = cast(dict[str, Any], json.loads(table.metadata_json))
    displayed = metadata.get("table")
    if not isinstance(displayed, Mapping):
        raise HistoricalUnitError("unit_unconfirmed")
    displayed = cast(Mapping[str, Any], displayed)
    raw_dates, raw_values = displayed.get("headName"), displayed.get(field.return_code)
    if not isinstance(raw_dates, list) or not isinstance(raw_values, list):
        raise HistoricalUnitError("unit_unconfirmed")
    dates = tuple(_display_date(value) for value in cast(list[object], raw_dates))
    values = cast(list[object], raw_values)
    if len(dates) != len(values) or len(set(dates)) != len(dates):
        raise HistoricalUnitError("unit_unconfirmed")
    by_date = dict(zip(dates, values, strict=True))
    scales = {Decimal(1), Decimal(100)}
    evidence_count = 0
    samples: list[dict[str, str]] = []
    for day, raw in zip(table.dates, field.values, strict=True):
        if raw is None:
            continue
        display = by_date.get(day.isoformat())
        if not isinstance(display, str) or re.fullmatch(
            r"\s*[+-]?\d+(?:\.\d+)?\s*[%％]\s*", display,
        ) is None:
            raise HistoricalUnitError("unit_unconfirmed")
        shown = Decimal(display.strip().rstrip("%％").strip())
        tolerance = Decimal("0.5") * Decimal(10) ** cast(int, shown.as_tuple().exponent)
        scales = {scale for scale in scales if abs(raw * scale - shown) <= tolerance}
        if raw != 0 and shown != 0:
            evidence_count += 1
            if len(samples) < 3:
                samples.append({"date": day.isoformat(), "raw": str(raw), "display": display})
    if evidence_count < 2 or len(scales) != 1:
        raise HistoricalUnitError("unit_unconfirmed")
    return next(iter(scales)), tuple(samples), evidence_count


def _display_date(value: object) -> str:
    if not isinstance(value, str):
        raise HistoricalUnitError("unit_unconfirmed")
    # MX's formatted table may append a literal daily-granularity marker.
    # Strip only this documented suffix, not arbitrary text or weekday names.
    normalized = re.sub(r"(?:\(日\)|（日）)$", "", value.strip())
    try:
        if date.fromisoformat(normalized).isoformat() != normalized:
            raise ValueError("not an ISO date")
    except ValueError as exc:
        raise HistoricalUnitError("unit_unconfirmed") from exc
    return normalized



def _field_definition(field: UnitFieldEvidence) -> dict[str, Any]:
    definitions: object = json.loads(field.metadata_json)
    if not isinstance(definitions, list):
        raise HistoricalUnitError("unit_unconfirmed")
    definitions = cast(list[object], definitions)
    if len(definitions) != 1:
        raise HistoricalUnitError("unit_unconfirmed")
    if not isinstance(definitions[0], dict):
        raise HistoricalUnitError("unit_unconfirmed")
    return cast(dict[str, Any], definitions[0])


def _unit_text(value: str) -> str:
    return value.strip().replace("％", "%").casefold()


def canonical_unit(field: UnitFieldEvidence) -> str:
    """Choose the canonical unit only from actual source unit metadata."""
    metadata = _field_definition(field)
    labels = [
        _unit_text(value) for value in (
            metadata.get("unitName"), metadata.get("unitDesc"), field.unit,
        ) if isinstance(value, str) and value.strip()
    ]
    raw_unit = metadata.get("unit")
    if isinstance(raw_unit, str) and _numeric_unit_code(metadata) is None:
        labels.append(_unit_text(raw_unit))
    if not labels:
        raise HistoricalUnitError("unit_unconfirmed")
    dimensions = {
        "percentage_points" if label == "100%" else _unit_definition(label)[0]
        for label in labels
    }
    if len(dimensions) != 1:
        raise HistoricalUnitError("unit_unconfirmed")
    dimension = next(iter(dimensions))
    canonical = {
        "cny": "元", "shares": "股", "percentage_points": "%",
        "multiple": "倍", "dimensionless": "1",
    }
    if dimension not in canonical:
        raise HistoricalUnitError("unit_unconfirmed")
    return canonical[dimension]


unit_scale = _unit_scale
unit_definition = _unit_definition
numeric_unit_code = _numeric_unit_code
numeric_display_scale = _numeric_display_scale
percent_display_scale = _percent_display_scale
unit_text = _unit_text
