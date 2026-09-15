"""Protocol fixtures only; reshaping dates does not prove requested coverage."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any

import pytest

from ashare_lab.adapters.market_data.mx_finance_history_format import MxFinanceHistoryDecoder
from ashare_lab.adapters.market_data.mx_screen_history_format import (
    MxScreenHistoryFormatError,
    screen_history_tables,
)
from ashare_lab.ports.live_market_data import LiveMarketDataProvenance, LiveMarketDataResult


def _column(title: str, index_name: str, day: str | None, unit: str | None = None):
    return {"title": title, "indexName": index_name,
            "key": f"{index_name}{{{day}}}" if day else index_name,
            "dateMsg": day, "unit": unit}


def _result(columns: list[dict[str, Any]], *rows: dict[str, Any]) -> LiveMarketDataResult:
    columns = [_column("代码", "SECURITY_CODE", None), *columns]
    return LiveMarketDataResult(
        provider="eastmoney_mx_stocks_screener", query="查询2025年全年指标；日期不得从此补齐",
        asset_type="A_STOCK", columns=tuple(str(column["title"]) for column in columns),
        rows=tuple(rows), provider_metadata={"columns": columns},
        provenance=LiveMarketDataProvenance(
            response_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 9, 7, tzinfo=UTC), schema_version="fixture.v1",
        ),
    )


def test_actual_dated_columns_become_separate_metric_tables_without_query_date_expansion() -> None:
    columns = [
        _column("最新价(元)", "NEWEST_PRICE<70>", "2026.09.07", "元"),
        _column("收盘价(日线不复权)(元)", "NEWEST_PRICE<140>", "2026.09.04", "元"),
        _column("收盘价(日线不复权)(元)", "NEWEST_PRICE<140>", "2026.09.03", "元"),
        _column("成交量(股)", "010000_VOLUME<70>", "2026.09.04", "股"),
        _column("成交量(股)", "010000_VOLUME<70>", "2026.09.03", "股"),
        _column("区间最高收盘价", "HIGHCLOSE", "2026.08.11 - 2026.09.07", "元"),
        _column("20日均量线", "20日010000_JLX<70>", "2026.09.04", ""),
    ]
    result = _result(columns, {
        "代码": "600519", "最新价(元) 2026.09.07": "1316.01",
        "收盘价(日线不复权)(元) 2026.09.04": "1330.00",
        "收盘价(日线不复权)(元) 2026.09.03": "1298.88",
        "成交量(股) 2026.09.04": "454.16万", "成交量(股) 2026.09.03": "177.48万",
        "区间最高收盘价 2026.08.11 - 2026.09.07": "1355.29",
        "20日均量线 2026.09.04": "336.04万",
    })
    original = deepcopy(result)
    tables = screen_history_tables(result)
    by_source = {table["fieldSet"][0]["returnSourceCode"]: table for table in tables}
    assert result == original and len(tables) == 4
    assert "HIGHCLOSE" not in by_source
    assert by_source["NEWEST_PRICE<70>"]["rawTable"]["headName"] == ["2026-09-07"]
    close = by_source["NEWEST_PRICE<140>"]
    assert close["rawTable"] == {
        "headName": ["2026-09-03", "2026-09-04"], "NEWEST_PRICE<140>": ["1298.88", "1330.00"],
    }
    assert close["fieldSet"][0]["returnName"] == "收盘价(日线不复权)(元)"
    assert "fixedParamValue" not in close["fieldSet"][0]
    volume = by_source["010000_VOLUME<70>"]
    assert volume["rawTable"]["010000_VOLUME<70>"] == ["1774800.00", "4541600.00"]
    assert volume["table"]["010000_VOLUME<70>"] == ["177.48万", "454.16万"]
    assert volume["screenHistoryEvidence"]["columns"] == [columns[4], columns[3]]
    unknown_unit = by_source["20日010000_JLX<70>"]
    assert unknown_unit["rawTable"]["20日010000_JLX<70>"] == ["336.04万"]
    assert "unitName" not in unknown_unit["fieldSet"][0]
    decoder = MxFinanceHistoryDecoder()
    assert decoder.entity_codes(close) == {"600519"}
    assert decoder.session_date(close["rawTable"]["headName"][0]) == date(2026, 9, 3)
    assert decoder.field_metadata(close)["NEWEST_PRICE<140>"][2] == "元"


@pytest.mark.parametrize(("unit", "display", "expected"), [
    ("元", "1.65万亿", "1650000000000.00"),
    ("元", "33.36亿", "3336000000.00"),
    ("股", "2.50万股", "25000.00"),
    ("股", "2亿股", "200000000"),
    ("股", "1手", "100"),
    ("万元", "2万元", "2"),
    ("万元", "20000元", "2"),
    ("%", "3.5%", "3.5"),
    ("元", "1,234.50", "1234.50"),
    (None, "12.3", "12.3"),
    (None, "3万", "3万"),
])
def test_display_scale_requires_explicit_units_and_reuses_domain_unit_definitions(
    unit: str | None, display: str, expected: str,
) -> None:
    tables = screen_history_tables(_result(
        [_column("供应商指标", "VENDOR_METRIC", "2026-09-04", unit)],
        {"代码": "300059.SZ", "供应商指标 2026-09-04": display},
    ))
    assert tables[0]["rawTable"]["VENDOR_METRIC"] == [expected]
    assert tables[0]["table"]["VENDOR_METRIC"] == [display]
    assert tables[0]["entityCode"] == "300059.SZ"


@pytest.mark.parametrize("value", [None, "--", "NaN", "无法计算", True])
def test_missing_and_non_numeric_cells_are_preserved_for_existing_validators(value: object) -> None:
    tables = screen_history_tables(_result(
        [_column("供应商指标", "METRIC", "2026/09/04", "元")],
        {"代码": "600519", "供应商指标 2026/09/04": value},
    ))
    assert tables[0]["rawTable"]["METRIC"] == [value]


def test_undated_scalar_is_never_broadcast_to_an_advertised_dated_column() -> None:
    result = _result([_column("指标", "METRIC", "2026.09.04", "元")],
                     {"代码": "600519", "指标": "99"})
    assert screen_history_tables(result)[0]["rawTable"]["METRIC"] == [None]


def test_entities_remain_separate_and_raw_keys_are_supported() -> None:
    columns = [_column("成交量", "VOLUME", "2026.09.04", "股")]
    result = _result(columns, {"SECURITY_CODE": "600519", "VOLUME{2026.09.04}": "1万"},
                     {"SECURITY_CODE": "300059", "VOLUME{2026.09.04}": "2万"})
    tables = screen_history_tables(result)
    assert [table["entityCode"] for table in tables] == ["600519", "300059"]
    assert [table["rawTable"]["VOLUME"] for table in tables] == [["10000"], ["20000"]]


def test_display_label_collision_cannot_assign_one_value_to_two_provider_metrics() -> None:
    result = _result([
        _column("指标", "METRIC_A", "2026.09.04", "元"),
        _column("指标", "METRIC_B", "2026.09.04", "元"),
    ], {"代码": "600519", "指标 2026.09.04": "12"})
    with pytest.raises(MxScreenHistoryFormatError, match="column label is ambiguous"):
        screen_history_tables(result)


@pytest.mark.parametrize("day", [None, "最新", "2026年9月", "2026.09.01 - 2026.09.04"])
def test_no_daily_history_is_invented_from_current_or_range_columns(day: str | None) -> None:
    result = _result([_column("指标", "METRIC", day)], {"代码": "600519"})
    assert screen_history_tables(result) == ()


def test_conflicting_units_and_duplicate_dates_raise_explicit_errors() -> None:
    column = _column("指标", "METRIC", "2026.09.04", "股")
    with pytest.raises(MxScreenHistoryFormatError, match="unit conflicts"):
        screen_history_tables(_result([column], {"代码": "600519", "指标 2026.09.04": "3元"}))
    with pytest.raises(MxScreenHistoryFormatError, match="duplicate metric dates"):
        screen_history_tables(_result([column, column], {"代码": "600519"}))
    with pytest.raises(MxScreenHistoryFormatError, match="date is invalid"):
        result = _result([_column("指标", "METRIC", "2026.02.30")], {"代码": "600519"})
        screen_history_tables(result)
