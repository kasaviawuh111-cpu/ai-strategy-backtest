from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ashare_lab.adapters.market_data.mx_grid_anchor import bind_latest_grid_anchor
from ashare_lab.domain.strategy.price_plans import GridParameters
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult, LiveMarketDataProvenance, LiveMarketDataResult,
)


def response(code="300803.SZ", field="ZXJ_f2_3", label="最新价", value="79.32"):
    return LiveFinanceDataResult(provider="eastmoney_mx_finance_data", query="latest", indicators=None,
        tables=({"code": code, "nameMap": {field: label},
                 "rawTable": {field: [value], "headName": ["2026-09-11 18:49"]}},),
        provenance=LiveMarketDataProvenance("sha256:" + "a" * 64,
            datetime(2026, 9, 11, 10, 49, tzinfo=UTC), "test"))


def params():
    return GridParameters(anchor_mode="latest_price", lower_price=1, upper_price=200,
                          spacing_mode="cny", spacing=1)


def test_current_quote_pins_price_and_provenance_without_scaling_yuan_spacing():
    bound = bind_latest_grid_anchor(params(), "300803.SZ", response())
    assert bound.anchor_price == Decimal("79.32")
    assert bound.anchor_mode == "latest_price"
    assert bound.spacing == 1 and bound.lower_price == 1 and bound.upper_price == 200
    assert bound.anchor_quote_response_sha256 == "sha256:" + "a" * 64
    assert bound.anchor_quote_time_label == "2026-09-11 18:49"


@pytest.mark.parametrize("changes", [
    {"code": "300059.SZ"}, {"field": "CLOSE", "label": "收盘价"},
    {"field": "ZCW_219_4", "label": "支撑位"}, {"value": "-"}, {"value": "0"},
])
def test_does_not_substitute_another_stock_or_field(changes):
    with pytest.raises(ValueError):
        bind_latest_grid_anchor(params(), "300803.SZ", response(**changes))


def screen_response(code="300803", column="最新价(元) 2026.09.11", value="79.32"):
    return LiveMarketDataResult(provider="eastmoney_mx_screener", query="300803.SZ行情最新价",
        asset_type="A股", columns=("代码", column, "收盘价(日线不复权)(元) 2026.09.11"),
        rows=({"代码": code, column: value,
               "收盘价(日线不复权)(元) 2026.09.11": "100"},),
        provenance=response().provenance)


def test_screen_quote_uses_identified_yuan_column_and_its_own_source():
    result = screen_response()
    bound = bind_latest_grid_anchor(params(), "300803.SZ", result)
    assert bound.anchor_price == Decimal("79.32")
    assert bound.anchor_quote_time_label == "2026.09.11"
    assert bound.anchor_quote_source == "eastmoney_mx_screener"
    assert bound.anchor_quote_response_sha256 == result.provenance.response_sha256
    assert bound.spacing == 1 and bound.lower_price == 1 and bound.upper_price == 200


@pytest.mark.parametrize("changes", [
    {"code": "300059"}, {"code": "300803.SH"},
    {"column": "最新价(%) 2026.09.11"}, {"column": "最新价(元)"},
    {"column": "收盘价(元) 2026.09.11"}, {"column": "最高价(元) 2026.09.11"},
    {"column": "最新价(元) 2026.09.12"}, {"value": "0"}, {"value": "NaN"},
])
def test_screen_quote_rejects_wrong_identity_field_unit_time_or_value(changes):
    with pytest.raises(ValueError):
        bind_latest_grid_anchor(params(), "300803.SZ", screen_response(**changes))


def test_screen_quote_does_not_choose_arbitrarily_between_conflicting_rows():
    from dataclasses import replace
    first = screen_response()
    conflict = replace(first, rows=first.rows + screen_response(value="80").rows)
    with pytest.raises(ValueError):
        bind_latest_grid_anchor(params(), "300803.SZ", conflict)
