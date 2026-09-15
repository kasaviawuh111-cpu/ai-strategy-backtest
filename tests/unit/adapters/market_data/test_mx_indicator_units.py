"""Provider unit evidence and deterministic comparison, not live verification."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_indicator_contract import build_indicator_contract
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderDataError,
    _provider_indicator_points,  # pyright: ignore[reportPrivateUsage]
)
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
)
from ashare_lab.domain.signals.provider_runtime import evaluate_provider_indicator_aligned
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.provider_indicator_data import ProviderIndicatorSeries

_DATES = (date(2026, 9, 2), date(2026, 9, 3))
_PARAMS = "CurType=1,Period=1"


def _amount_table(
    unit_metadata: dict[str, object], *, raw: tuple[str, str] = ("1", "2"),
    display: tuple[str, str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "entityCodes": ["300059.SZ"],
        "rawTable": {"headName": [str(day) for day in _DATES], "amount": list(raw)},
        "fieldSet": [{
            "returnCode": "amount", "returnName": "成交额", "returnSourceCode": "AMOUNT",
            "fixedParamValue": _PARAMS, **unit_metadata,
        }],
    }
    if display is not None:
        result["table"] = {
            "headName": [f"{day}(日)" for day in _DATES], "amount": list(display),
        }
    return result


def _parse_amount(table: dict[str, object]):
    return _provider_indicator_points(
        tables=(table,), instrument_id="300059.SZ", provider_indicator_name="成交额",
        value_names=("成交额",), start=_DATES[0], end=_DATES[-1],
        contract=build_indicator_contract("market.amount", "成交额", ("成交额",)),
    )


@pytest.mark.parametrize(("source_unit", "scale"), [
    ("元", "1"), ("万元", "10000"), ("亿元", "100000000"),
])
def test_amount_units_are_normalized_before_dsl_threshold_comparison(
    source_unit: str, scale: str,
) -> None:
    points = _parse_amount(_amount_table({"unitName": source_unit}))
    assert tuple(point.values[0].value for point in points) == (
        Decimal(scale), Decimal(scale) * 2,
    )
    value = points[0].values[0]
    assert value.unit == "元" and value.source_unit == source_unit
    assert value.source_parameters == _PARAMS
    assert json.loads(value.unit_normalization or "{}")["scale"] == scale
    series = ProviderIndicatorSeries(
        provider="fixture", instrument_id="300059.SZ", indicator_id="market.amount",
        requested_start=_DATES[0], requested_end=_DATES[-1], points=points,
        response_sha256="sha256:" + "b" * 64, retrieved_at=datetime.now(UTC),
        schema_version="fixture", query="fixture-only",
    )
    condition = IndicatorCondition(
        indicator_id="market.amount", definition_version="1.0.0", trigger="crosses_above",
        value=float(Decimal(scale) * Decimal("1.5")),
    )
    facts = evaluate_provider_indicator_aligned(condition, series, _DATES)
    assert facts[0] is None
    assert facts[1] is not None and facts[1].triggered is True


@pytest.mark.parametrize(("unit", "expected"), [("万股", "10000"), ("手", "100")])
def test_volume_operands_normalize_independently_before_comparison(
    unit: str, expected: str,
) -> None:
    fields = [{
        "returnCode": "volume", "returnName": "成交量", "returnSourceCode": "VOLUME",
        "fixedParamValue": "Period=1", "unitName": unit,
    }, {
        "returnCode": "average", "returnName": "20日平均成交量",
        "fixedParamValue": "N=20,Period=1", "unitName": "股",
    }]
    table = {"entityCodes": ["300059.SZ"], "fieldSet": fields, "rawTable": {
        "headName": [str(day) for day in _DATES], "volume": ["1", "2"],
        "average": [expected, expected],
    }}
    points = _provider_indicator_points(
        tables=(table,), instrument_id="300059.SZ", provider_indicator_name="成交量",
        value_names=("成交量", "20日平均成交量"), start=_DATES[0], end=_DATES[-1],
        contract=build_indicator_contract("market.volume", "成交量", ("成交量", "20日平均成交量"),
                                          {"baseline_period": 20}),
    )
    assert tuple(value.value for value in points[1].values) == (
        Decimal(expected) * 2, Decimal(expected),
    )
    assert all(value.unit == "股" for point in points for value in point.values)


def test_numeric_unit_code_is_not_a_multiplier_and_requires_actual_display_proof() -> None:
    table = _amount_table({"unit": "2", "unitName": "元"}, raw=("19.15", "19.30"),
                          display=("19.15元", "19.30元"))
    values = [point.values[0] for point in _parse_amount(table)]
    assert [value.value for value in values] == [Decimal("19.15"), Decimal("19.30")]
    proof = json.loads(values[0].unit_normalization or "{}")
    assert proof["scale"] == "1" and proof["verifiedDisplayCount"] == 2
    assert proof["sourceMetadata"] == {"unit": "2", "unitName": "元"}
    table.pop("table")
    with pytest.raises(MxSaasProviderDataError, match="unit is unconfirmed"):
        _parse_amount(table)


@pytest.mark.parametrize("metadata", [{}, {"unitName": "未知单位"},
    {"unitName": "元", "unitDesc": "股"}, {"unit": "2"}, {"unitName": "港元"},
    {"unitName": "股"}, {"unitName": "%"},
])
def test_unknown_or_incompatible_named_units_cannot_silently_reach_comparison(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(MxSaasProviderDataError, match="unit is unconfirmed"):
        _parse_amount(_amount_table(metadata))


@pytest.mark.parametrize(("raw", "expected_scale"), [(("0.2", "0.3"), "100"),
                                                         (("20", "30"), "1")])
def test_percentage_format_code_requires_raw_display_scale_proof(
    raw: tuple[str, str], expected_scale: str,
) -> None:
    table = {
        "entityCodes": ["300059.SZ"],
        "rawTable": {"headName": [str(day) for day in _DATES], "rsi": list(raw)},
        "table": {"headName": [str(day) for day in _DATES], "rsi": ["20.00%", "30.00%"]},
        "fieldSet": [{
            "returnCode": "rsi", "returnName": "RSI值", "returnSourceCode": "RSIXDQRZB",
            "fixedParamValue": "N=14,AdjustFlag=2,period=1", "unitName": "100%",
        }],
    }
    points = _provider_indicator_points(
        tables=(table,), instrument_id="300059.SZ", provider_indicator_name="RSI(14)",
        value_names=("RSI值",), start=_DATES[0], end=_DATES[-1],
        contract=build_indicator_contract("technical.rsi", "RSI(14)", ("RSI值",)),
    )
    assert tuple(point.values[0].value for point in points) == (Decimal(20), Decimal(30))
    assert all(point.values[0].unit == "%" for point in points)
    assert json.loads(points[0].values[0].unit_normalization or "{}")["scale"] == expected_scale


def test_existing_exact_amount_raw_unit_one_contract_remains_auditable() -> None:
    (first, _) = _parse_amount(_amount_table({"unit": "1"}))
    assert first.values[0].value == 1 and first.values[0].unit == "元"
    assert first.values[0].source_unit is None
    assert json.loads(first.values[0].unit_normalization or "{}")["basis"] == (
        "verified_field_raw_unit_1_contract"
    )


@pytest.mark.parametrize("failure", [None, "wrong_period", "unknown_code", "missing_unit"])
def test_bound_dimensionless_field_uses_contract_without_guessing_unknown_units(
    failure: str | None,
) -> None:
    field = {
        "returnCode": "rsi", "returnName": "RSI相对强弱指标",
        "returnSourceCode": "RSIXDQRZB", "unit": "1",
        "fixedParamValue": "period=1,N=14,AdjustFlag=2,NewOldType=0",
    }
    if failure == "wrong_period":
        field["fixedParamValue"] = "period=1,N=6,AdjustFlag=2,NewOldType=0"
    elif failure == "unknown_code":
        field["unit"] = "9"
    elif failure == "missing_unit":
        field.pop("unit")
    table = {"entityCodes": ["000333.SZ"], "fieldSet": [field], "rawTable": {
        "headName": [str(day) for day in _DATES], "rsi": ["29.5", "30.5"],
    }}
    contract = build_indicator_contract("technical.rsi", "RSI(14)", ("RSI值",))
    def parse():
        return _provider_indicator_points(
            tables=(table,), instrument_id="000333.SZ", provider_indicator_name="RSI(14)",
            value_names=("RSI值",), start=_DATES[0], end=_DATES[-1], contract=contract,
        )
    if failure:
        with pytest.raises(MxSaasProviderDataError):
            parse()
    else:
        points = parse()
        assert tuple(point.values[0].value for point in points) == (
            Decimal("29.5"), Decimal("30.5"),
        )
        assert all(point.values[0].unit == "1" for point in points)
        proof = json.loads(points[0].values[0].unit_normalization or "{}")
        assert proof["basis"] == "verified_field_raw_unit_1_contract" and proof["scale"] == "1"
        assert proof["sourceMetadata"] == {"unit": "1"}


def test_verified_ma_price_contract_and_each_tables_display_evidence_are_preserved() -> None:
    close = {
        "entityCodes": ["300059.SZ"],
        "rawTable": {"headName": [str(day) for day in _DATES], "close": ["19.15", "19.30"]},
        "table": {"headName": [str(day) for day in _DATES], "close": ["19.15元", "19.30元"]},
        "fieldSet": [{
            "returnCode": "close", "returnName": "收盘价", "returnSourceCode": "CLOSE",
            "fixedParamValue": "AdjustFlag=2,CurType=1,Period=1", "unit": "2", "unitName": "元",
        }],
    }
    ma = {
        "entityCodes": ["300059.SZ"],
        "rawTable": {"headName": [str(day) for day in _DATES], "ma": ["18", "18.10"]},
        "fieldSet": [{
            "returnCode": "ma", "returnName": "20日MA简单移动平均",
            "returnSourceCode": "MAJDYDPJ20", "unit": "1",
            "fixedParamValue": "N=20,AdjustFlag=2,period=1,NewOldType=0",
        }],
    }
    points = _provider_indicator_points(
        tables=(close, ma), instrument_id="300059.SZ", provider_indicator_name="20日移动平均线",
        value_names=("收盘价", "20日MA简单移动平均"), start=_DATES[0], end=_DATES[-1],
        contract=build_indicator_contract("technical.ma", "20日移动平均线",
                                          ("收盘价", "20日MA简单移动平均")),
    )
    assert tuple(value.value for value in points[1].values) == (Decimal("19.30"), Decimal("18.10"))
    assert all(value.unit == "元" for point in points for value in point.values)
    close_proof, ma_proof = (json.loads(value.unit_normalization or "{}")
                            for value in points[1].values)
    assert close_proof["verifiedDisplayCount"] == 2
    assert close_proof["scale"] == "1"
    assert ma_proof["basis"] == "verified_field_raw_unit_1_contract"
    assert ma_proof["sourceMetadata"] == {"unit": "1"}


def test_named_unit_evidence_and_converted_values_survive_persistent_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [
            _amount_table({"unitName": "亿元"}),
        ]})

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    async def query(cache: FileCachedHistoricalIndicatorData) -> ProviderIndicatorSeries:
        return await cache.query_indicator_history(
            instrument_id="300059.SZ", indicator_id="market.amount",
            provider_indicator_name="成交额", value_names=("成交额",),
            start=_DATES[0], end=_DATES[-1],
        )

    live = asyncio.run(query(FileCachedHistoricalIndicatorData(client, root=tmp_path)))
    cached = asyncio.run(query(FileCachedHistoricalIndicatorData(None, root=tmp_path)))
    assert len(calls) == 1 and cached.cache_status == "disk"
    assert cached.points == live.points
    assert cached.points[1].values[0].value == Decimal("200000000")
    assert cached.points[1].values[0].unit_normalization is not None
    assert cached.response_sha256 == live.response_sha256
