from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.signals import (
    ProviderSignalRuntimeError,
    evaluate_provider_condition_tree_aligned,
    evaluate_provider_indicator_aligned,
    provider_condition_leaves,
)
from ashare_lab.domain.strategy import AllCondition, AnyCondition, IndicatorCondition, NotCondition
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)

TZ = ZoneInfo("Asia/Shanghai")
START = date(2025, 9, 1)


def _condition(trigger: str) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="technical.kdj",
        definition_version="1.0.0",
        params={"period": 9, "k_smoothing": 3, "d_smoothing": 3},
        trigger=trigger,
    )


def _series(values: tuple[tuple[str, str, str], ...]) -> ProviderIndicatorSeries:
    return ProviderIndicatorSeries(
        provider="eastmoney_mx_finance_data",
        instrument_id="300033.SZ",
        indicator_id="technical.kdj",
        requested_start=START,
        requested_end=START + timedelta(days=len(values) - 1),
        points=tuple(
            ProviderIndicatorPoint(
                session_date=START + timedelta(days=index),
                observed_at=datetime.combine(START + timedelta(days=index), time(15), tzinfo=TZ),
                first_available_at=datetime.combine(
                    START + timedelta(days=index), time(15), tzinfo=TZ
                ),
                values=(
                    ProviderIndicatorValue("KDJ_K", "K值", Decimal(k)),
                    ProviderIndicatorValue("KDJ_D", "D值", Decimal(d)),
                    ProviderIndicatorValue("KDJ_J", "J值", Decimal(j)),
                ),
            )
            for index, (k, d, j) in enumerate(values)
        ),
        response_sha256="sha256:" + "a" * 64,
        retrieved_at=datetime(2026, 9, 3, tzinfo=TZ),
        schema_version="eastmoney-mx.provider-indicator-history.v1",
        query="同花顺近一年每个交易日 KDJ K、D、J 值",
    )


def test_kdj_cross_uses_exact_provider_values() -> None:
    series = _series((("20", "25", "10"), ("30", "25", "40"), ("22", "26", "14")))
    sessions = tuple(point.session_date for point in series.points)

    golden = evaluate_provider_indicator_aligned(_condition("golden_cross"), series, sessions)
    death = evaluate_provider_indicator_aligned(_condition("death_cross"), series, sessions)

    assert golden[0] is None
    assert golden[1] is not None and golden[1].triggered is True
    assert golden[1].left_value == Decimal("30")
    assert golden[1].right_value == Decimal("25")
    assert golden[1].evidence[0].provider == "eastmoney_mx_finance_data"
    assert death[2] is not None and death[2].triggered is True


def test_missing_provider_session_is_no_signal_without_fill_or_carry_forward() -> None:
    series = _series((("20", "25", "10"), ("30", "25", "40")))
    sessions = (
        START,
        START + timedelta(days=1),
        START + timedelta(days=2),
    )

    timeline = evaluate_provider_indicator_aligned(_condition("golden_cross"), series, sessions)

    assert timeline[0] is None
    assert timeline[1] is not None and timeline[1].triggered is True
    assert timeline[2] is None


def test_provider_session_outside_trusted_axis_fails_closed() -> None:
    series = _series((("20", "25", "10"), ("30", "25", "40")))

    with pytest.raises(ProviderSignalRuntimeError, match="outside the trusted axis"):
        evaluate_provider_indicator_aligned(_condition("golden_cross"), series, (START,))


def test_unverified_indicator_never_falls_back_to_ohlcv() -> None:
    series = _series((("20", "25", "10"), ("30", "25", "40")))
    unknown = IndicatorCondition(
        indicator_id="technical.supertrend",
        definition_version="1.0.0",
        params={"period": 14},
        trigger="above",
        value=1,
    )

    with pytest.raises(ProviderSignalRuntimeError, match="not registered"):
        evaluate_provider_indicator_aligned(
            unknown,
            series,
            tuple(point.session_date for point in series.points),
        )


def _field_series(
    *,
    indicator_id: str,
    rows: tuple[dict[str, str], ...],
) -> ProviderIndicatorSeries:
    return ProviderIndicatorSeries(
        provider="eastmoney_mx_finance_data",
        instrument_id="600519.SH",
        indicator_id=indicator_id,
        requested_start=START,
        requested_end=START + timedelta(days=len(rows) - 1),
        points=tuple(
            ProviderIndicatorPoint(
                session_date=START + timedelta(days=index),
                observed_at=datetime.combine(START + timedelta(days=index), time(15), tzinfo=TZ),
                first_available_at=datetime.combine(
                    START + timedelta(days=index), time(15), tzinfo=TZ
                ),
                values=tuple(
                    ProviderIndicatorValue(field_name.upper(), field_name, Decimal(value))
                    for field_name, value in row.items()
                ),
            )
            for index, row in enumerate(rows)
        ),
        response_sha256="sha256:" + "b" * 64,
        retrieved_at=datetime(2026, 9, 3, tzinfo=TZ),
        schema_version="eastmoney-mx.provider-indicator-history.v1",
        query="provider exact values",
    )


def test_generic_threshold_condition_uses_provider_value() -> None:
    condition = IndicatorCondition(
        indicator_id="technical.atr",
        definition_version="1.0.0",
        params={"period": 14},
        trigger="above",
        value=2.5,
    )
    series = _field_series(
        indicator_id="technical.atr",
        rows=({"ATR值": "2.4"}, {"ATR值": "2.6"}),
    )

    timeline = evaluate_provider_indicator_aligned(
        condition, series, tuple(point.session_date for point in series.points)
    )

    assert [item.triggered for item in timeline if item is not None] == [False, True]
    assert timeline[1] is not None and timeline[1].left_value == Decimal("2.6")


def _with_units(
    series: ProviderIndicatorSeries, units: dict[str, str | None],
) -> ProviderIndicatorSeries:
    return replace(series, points=tuple(
        replace(point, values=tuple(replace(value, unit=units[value.field_name])
                                    for value in point.values))
        for point in series.points
    ))


@pytest.mark.parametrize("right_unit", ["元", "万股", "%"])
def test_field_comparison_rejects_incompatible_dimension_or_unconverted_scale(
    right_unit: str,
) -> None:
    condition = IndicatorCondition(
        indicator_id="market.volume", definition_version="1.0.0",
        params={"baseline_period": 20}, trigger="gte_multiple", value=1.5,
    )
    series = _with_units(_field_series(
        indicator_id="market.volume", rows=({"成交量": "200", "20日平均成交量": "100"},),
    ), {"成交量": "股", "20日平均成交量": right_unit})
    with pytest.raises(ProviderSignalRuntimeError, match="incompatible units"):
        evaluate_provider_indicator_aligned(condition, series, (START,))


@pytest.mark.parametrize("right_unit", ["股", None])
def test_field_comparison_accepts_equal_canonical_units_without_inventing_legacy_units(
    right_unit: str | None,
) -> None:
    condition = IndicatorCondition(
        indicator_id="market.volume", definition_version="1.0.0",
        params={"baseline_period": 20}, trigger="gte_multiple", value=1.5,
    )
    series = _with_units(_field_series(
        indicator_id="market.volume", rows=({"成交量": "200", "20日平均成交量": "100"},),
    ), {"成交量": "股", "20日平均成交量": right_unit})
    (fact,) = evaluate_provider_indicator_aligned(condition, series, (START,))
    assert fact is not None and fact.triggered
    assert (fact.left_value, fact.right_value) == (Decimal(200), Decimal(150))
    assert series.points[0].values[1].unit == right_unit


def test_previous_field_comparison_rejects_a_unit_change_between_sessions() -> None:
    series = _field_series(indicator_id="technical.obv", rows=({"OBV值": "100"}, {"OBV值": "200"}))
    series = replace(series, points=tuple(
        replace(point, values=(replace(point.values[0], unit=unit),))
        for point, unit in zip(series.points, ("股", "元"), strict=True)
    ))
    condition = IndicatorCondition(
        indicator_id="technical.obv", definition_version="1.0.0", trigger="rising",
    )
    with pytest.raises(ProviderSignalRuntimeError, match="incompatible units"):
        evaluate_provider_indicator_aligned(
            condition, series, tuple(p.session_date for p in series.points),
        )


def test_independent_and_conditions_can_use_different_units() -> None:
    price = IndicatorCondition(
        indicator_id="technical.ma", definition_version="1.0.0", trigger="price_above",
        params={"period": 20, "price_field": "close"},
    )
    volume = IndicatorCondition(
        indicator_id="market.volume", definition_version="1.0.0", trigger="gte_multiple",
        params={"baseline_period": 20}, value=1.5,
    )
    prices = _with_units(_field_series(
        indicator_id="technical.ma", rows=({"收盘价": "20", "20日MA简单移动平均": "18"},),
    ), {"收盘价": "元", "20日MA简单移动平均": "人民币元"})
    volumes = _with_units(_field_series(
        indicator_id="market.volume", rows=({"成交量": "200", "20日平均成交量": "100"},),
    ), {"成交量": "股", "20日平均成交量": "股"})
    (fact,) = evaluate_provider_condition_tree_aligned(
        AllCondition(children=(price, volume)),
        {"$.children[0]": prices, "$.children[1]": volumes}, (START,),
    )
    assert fact is not None and fact.triggered


def test_provider_runtime_combines_multiple_provider_fields() -> None:
    condition = IndicatorCondition(
        indicator_id="volume.price_confirmation",
        definition_version="1.0.0",
        params={
            "baseline_period": 20,
            "volume_multiple": 1.5,
            "return_threshold_pct": 3,
        },
        trigger="surge_up",
    )
    series = _field_series(
        indicator_id="volume.price_confirmation",
        rows=(
            {"成交量": "160", "前20日平均成交量": "100", "当日涨跌幅": "2.9"},
            {"成交量": "170", "前20日平均成交量": "100", "当日涨跌幅": "3.1"},
        ),
    )

    timeline = evaluate_provider_indicator_aligned(
        condition, series, tuple(point.session_date for point in series.points)
    )

    assert [item.triggered for item in timeline if item is not None] == [False, True]
    assert timeline[1] is not None and " AND " in timeline[1].reason


@pytest.mark.parametrize("trigger,change", [("surge_up", "3"), ("surge_down", "-3")])
def test_volume_price_confirmation_uses_exact_average_and_preserves_price_direction(
    trigger: str, change: str,
) -> None:
    condition = IndicatorCondition(
        indicator_id="volume.price_confirmation", definition_version="1.0.0",
        params={"baseline_period": 15, "volume_multiple": 2, "return_threshold_pct": 3},
        trigger=trigger,
    )
    series = _field_series(indicator_id="volume.price_confirmation", rows=(
        {"成交量": "200", "前15日平均成交量": "100", "当日涨跌幅": change},
        {"成交量": "199", "前15日平均成交量": "100", "当日涨跌幅": change},
        {"成交量": "200", "前15日平均成交量": "100", "当日涨跌幅": "0"},
        {"成交量": "200", "前15日平均成交量": "0", "当日涨跌幅": change},
        {"成交量": "0", "前15日平均成交量": "100", "当日涨跌幅": change},
    ))
    timeline = evaluate_provider_indicator_aligned(
        condition, series, tuple(point.session_date for point in series.points),
    )
    assert [None if fact is None else fact.triggered for fact in timeline] == [
        True, False, False, None, None,
    ]


def test_previous_provider_value_and_consecutive_sessions_are_supported() -> None:
    obv_condition = IndicatorCondition(
        indicator_id="technical.obv",
        definition_version="1.0.0",
        params={},
        trigger="rising",
    )
    obv_series = _field_series(
        indicator_id="technical.obv",
        rows=({"OBV值": "10"}, {"OBV值": "12"}, {"OBV值": "11"}),
    )
    obv = evaluate_provider_indicator_aligned(
        obv_condition,
        obv_series,
        tuple(point.session_date for point in obv_series.points),
    )
    assert obv[0] is None
    assert obv[1] is not None and obv[1].triggered is True
    assert obv[2] is not None and obv[2].triggered is False

    volume_condition = IndicatorCondition(
        indicator_id="volume.relative",
        definition_version="1.0.0",
        params={"baseline_period": 20, "consecutive_days": 2},
        trigger="consecutive_gte_multiple",
        value=1.5,
    )
    volume_series = _field_series(
        indicator_id="volume.relative",
        rows=(
            {"成交量": "160", "前20日平均成交量": "100"},
            {"成交量": "170", "前20日平均成交量": "100"},
            {"成交量": "140", "前20日平均成交量": "100"},
        ),
    )
    volume = evaluate_provider_indicator_aligned(
        volume_condition,
        volume_series,
        tuple(point.session_date for point in volume_series.points),
    )
    assert volume[0] is None
    assert volume[1] is not None and volume[1].triggered is True
    assert volume[2] is not None and volume[2].triggered is False


@pytest.mark.parametrize("current,average", [("100", "0"), ("100", "-1"), ("0", "100")])
@pytest.mark.parametrize("trigger", ["gt_multiple", "lte_multiple"])
def test_relative_volume_invalid_operands_remain_unavailable_and_break_streak(
    current: str, average: str, trigger: str,
) -> None:
    condition = IndicatorCondition(
        indicator_id="volume.relative", definition_version="1.0.0",
        params={"baseline_period": 20, "consecutive_days": 2}, trigger=trigger, value=1.5,
    )
    series = _field_series(indicator_id="volume.relative", rows=(
        {"成交量": "160", "前20日平均成交量": "100"},
        {"成交量": current, "前20日平均成交量": average},
        {"成交量": "170", "前20日平均成交量": "100"},
    ))
    sessions = tuple(point.session_date for point in series.points)
    timeline = evaluate_provider_indicator_aligned(condition, series, sessions)
    assert timeline[1] is None
    assert timeline[0] is not None and timeline[2] is not None
    consecutive = evaluate_provider_indicator_aligned(
        condition.model_copy(update={"trigger": "consecutive_gte_multiple"}), series, sessions,
    )
    assert consecutive == (None, None, None)


def test_rolling_high_provider_threshold_uses_strict_comparison_not_boolean_flag() -> None:
    condition = IndicatorCondition(
        indicator_id="price.rolling_high", definition_version="1.0.0",
        params={"period": 20, "price_field": "close"}, trigger="new_high",
    )
    series = _field_series(indicator_id="price.rolling_high", rows=(
        {"收盘价": "100", "前20日最高收盘价": "100"},
        {"收盘价": "101", "前20日最高收盘价": "100"},
        {"收盘价": "99", "前20日最高收盘价": "100"},
    ))
    timeline = evaluate_provider_indicator_aligned(
        condition, series, tuple(point.session_date for point in series.points),
    )
    assert [fact.triggered for fact in timeline if fact is not None] == [False, True, False]


def test_provider_condition_tree_combines_all_any_and_not_without_formulas() -> None:
    rsi = IndicatorCondition(
        indicator_id="technical.rsi",
        definition_version="1.0.0",
        params={"period": 14},
        trigger="below",
        value=30,
    )
    turnover = IndicatorCondition(
        indicator_id="market.turnover_rate",
        definition_version="1.0.0",
        params={},
        trigger="above",
        value=2,
    )
    atr = IndicatorCondition(
        indicator_id="technical.atr",
        definition_version="1.0.0",
        params={"period": 14},
        trigger="above",
        value=3,
    )
    condition = AllCondition(
        children=(
            rsi,
            AnyCondition(children=(turnover, NotCondition(child=atr))),
        )
    )
    sessions = (START, START + timedelta(days=1))
    series = {
        "$.children[0]": _field_series(
            indicator_id="technical.rsi",
            rows=({"RSI值": "25"}, {"RSI值": "35"}),
        ),
        "$.children[1].children[0]": _field_series(
            indicator_id="market.turnover_rate",
            rows=({"换手率": "3"}, {"换手率": "3"}),
        ),
        "$.children[1].children[1].child": _field_series(
            indicator_id="technical.atr",
            rows=({"ATR值": "4"}, {"ATR值": "2"}),
        ),
    }

    timeline = evaluate_provider_condition_tree_aligned(condition, series, sessions)

    assert [fact.triggered for fact in timeline if fact is not None] == [True, False]
    assert timeline[0] is not None
    assert timeline[0].condition_ref == "all"
    assert len(timeline[0].children) == 2
    assert len(timeline[0].evidence) == 3


def test_provider_condition_tree_requires_exact_leaf_series() -> None:
    condition = AllCondition(children=(_condition("golden_cross"), _condition("death_cross")))
    sessions = tuple(point.session_date for point in _series((("20", "25", "10"),)).points)

    with pytest.raises(ProviderSignalRuntimeError, match="series paths do not match"):
        evaluate_provider_condition_tree_aligned(
            condition,
            {"$.children[0]": _series((("20", "25", "10"),))},
            sessions,
        )


def test_provider_condition_leaf_paths_are_stable_for_generic_acquisition() -> None:
    condition = AnyCondition(
        children=(
            _condition("golden_cross"),
            NotCondition(child=_condition("death_cross")),
        )
    )

    assert tuple(path for path, _ in provider_condition_leaves(condition)) == (
        "$.children[0]",
        "$.children[1].child",
    )
