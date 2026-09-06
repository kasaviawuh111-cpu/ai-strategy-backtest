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
            {"相对成交量": "1.6", "当日涨跌幅": "2.9"},
            {"相对成交量": "1.7", "当日涨跌幅": "3.1"},
        ),
    )

    timeline = evaluate_provider_indicator_aligned(
        condition, series, tuple(point.session_date for point in series.points)
    )

    assert [item.triggered for item in timeline if item is not None] == [False, True]
    assert timeline[1] is not None and " AND " in timeline[1].reason


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
            {"量比": "1.6"},
            {"量比": "1.7"},
            {"量比": "1.4"},
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
