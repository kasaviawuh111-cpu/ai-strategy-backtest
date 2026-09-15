from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ashare_lab.adapters.language.skill_metric_binding_review import (
    SkillMetricBindingReviewer,
    SkillMetricBindingVerdict,
)
from ashare_lab.adapters.market_data.mx_finance_history_format import MxFinanceHistoryDecoder
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderNoDataError, MxSaasProviderDataError, MxSaasProviderUnavailableError,
    MxSaasProviderAuthError,
    MxSaasProviderSqlError,
)
from ashare_lab.application.skill_backtest_service import (
    _skill_numeric_timeline,  # pyright: ignore[reportPrivateUsage]
)
from ashare_lab.application.skill_numeric_history import (
    SkillNumericHistoryError,
    prepare_skill_numeric_series,
)
from ashare_lab.application.skill_series_discovery import SkillSeriesDiscovery
from ashare_lab.domain.signals.provider_runtime import evaluate_provider_indicator_aligned
from ashare_lab.domain.signals.skill_numeric import skill_numeric_binding
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataProvenance

START, END = date(2026, 9, 1), date(2026, 9, 4)


def _table(unit: str = "倍") -> dict[str, Any]:
    return {
        "entityCode": "600519", "dateGranularity": "DAY",
        "fieldSet": [{
            "returnCode": "328773", "returnSourceCode": "PETTM", "returnName": "市盈率TTM",
            "unit": "1", "unitName": unit, "unitDesc": unit, "fixedParamValue": "Period=1",
            "dateGranularity": "DAY",
        }],
        "rawTable": {
            "headName": ["2026-09-03", "2026-09-02", "2026-09-01"],
            "328773": ["19", None, "18.25"],
        },
    }


def _discovery(*tables: dict[str, Any]) -> SkillSeriesDiscovery:
    return SkillSeriesDiscovery(Mock(query_finance=AsyncMock(return_value=LiveFinanceDataResult(
        provider="eastmoney_mx_finance_data", query="真实供应商查询", indicators=None,
        tables=tuple(tables), provenance=LiveMarketDataProvenance(
            response_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 9, 7, tzinfo=UTC), schema_version="test.v1",
        ),
    ))), decoder=MxFinanceHistoryDecoder())


def _condition(unit: str = "倍") -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="provider.numeric", definition_version="1.0.0", trigger="above", value=18.5,
        params={"metric_query": "任意供应商真实指标（原始参数）", "unit": unit},
    )


async def _prepare(*tables: dict[str, Any], unit: str = "倍"):
    return await prepare_skill_numeric_series(
        _discovery(*tables), condition=_condition(unit), instrument_id="600519.SH",
        start=START, end=END,
    )


@pytest.mark.asyncio
async def test_unique_real_field_sorted_without_filling_null_or_claiming_pit() -> None:
    series = await _prepare(_table())
    assert series.schema_version == "skill-numeric-history.v1"
    assert series.query == "真实供应商查询"
    assert [point.session_date for point in series.points] == [date(2026, 9, 1), date(2026, 9, 3)]
    assert [point.values[0].value for point in series.points] == [Decimal("18.25"), Decimal(19)]
    first = series.points[0]
    assert first.observed_at.isoformat() == "2026-09-01T15:00:00+08:00"
    assert first.first_available_at == first.observed_at
    assert first.values[0].field_code == "328773" and first.values[0].field_name == "value"
    parameters = json.loads(first.values[0].source_parameters or "{}")
    assert parameters["assumedTime"] and parameters["pitVerified"] is False
    assert parameters["bindingHash"].startswith("sha256:")
    assert parameters["fixedParamValue"] == "Period=1"
    assert parameters["fieldMetadata"]["returnSourceCode"] == "PETTM"
    assert parameters["scale"] == "1"
    assert len(parameters["attempts"]) == 1
    assert parameters["attempts"][0]["responseHash"] == series.response_sha256
    timeline = evaluate_provider_indicator_aligned(
        _condition(), series, (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)),
        binding=skill_numeric_binding(_condition()),
    )
    assert timeline[0] is not None and not timeline[0].triggered
    assert timeline[1] is None and timeline[2] is not None and timeline[2].triggered


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [True, False])
async def test_binding_uses_model_verdict_over_actual_field_metadata(matched: bool) -> None:
    table = _table()
    if not matched:
        table["fieldSet"][0].update(returnSourceCode="PB", returnName="市净率")
    result = SkillMetricBindingVerdict(
        verdict="matched" if matched else "mismatch",
        reason_code="same_metric" if matched else "different_metric", reason="固定诊断",
        binding_hash="sha256:" + "b" * 64, provider="test", model="test-model",
        prompt_version="test.v1",
    )
    reviewer = Mock(spec=SkillMetricBindingReviewer, verify=AsyncMock(return_value=(result,)))
    condition = _condition().model_copy(update={
        "params": {"metric_query": "市盈率TTM", "unit": "倍"},
    })
    task = prepare_skill_numeric_series(
        _discovery(table), condition=condition, instrument_id="600519.SH", start=START, end=END,
        binding_reviewer=reviewer,
    )
    if matched:
        series = await task
        assert series.points[0].values[0].value == Decimal("18.25")
        metadata = json.loads(series.points[0].values[0].source_parameters or "{}")
        assert metadata["fieldBindingReview"]["bindingHash"] == result.binding_hash
    else:
        with pytest.raises(SkillNumericHistoryError) as caught:
            await task
        assert caught.value.code == "field_mismatch"
    reviewer.verify.assert_awaited_once()
    bindings = reviewer.verify.await_args.args[0]
    assert bindings[0][0] == "市盈率TTM"
    assert bindings[0][1]["returnSourceCode"] == ("PETTM" if matched else "PB")
    assert "rawTable" not in bindings[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target,scale", [
    ("元", "万元", "0.0001"), ("万元", "元", "10000"),
    ("亿元", "万", "10000"), ("元", "亿", "0.00000001"),
    ("股", "手", "0.01"), ("手", "股", "100"), ("万股", "股", "10000"),
    ("%", "百分点", "1"), ("倍", "倍", "1"),
])
async def test_only_dimension_compatible_units_are_converted(
    source: str, target: str, scale: str,
) -> None:
    series = await _prepare(_table(source), unit=target)
    assert series.points[0].values[0].value == Decimal("18.25") * Decimal(scale)
    assert series.points[0].values[0].unit == target
    assert series.points[0].values[0].source_unit == source


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["户", "吨", "美元/股"])
async def test_explicit_identical_unit_is_not_limited_by_conversion_catalog(unit: str) -> None:
    series = await _prepare(_table(unit), unit=unit)
    assert series.points[0].values[0].value == Decimal("18.25")
    assert series.points[0].values[0].unit == unit


@pytest.mark.asyncio
async def test_unknown_units_do_not_get_unverified_scale_conversions() -> None:
    with pytest.raises(SkillNumericHistoryError, match="单位不兼容"):
        await _prepare(_table("吨"), unit="万吨")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["元", "万元", "万"])
async def test_coded_unit_two_uses_dated_formatted_values_not_numeric_multiplier(
    target: str,
) -> None:
    # Actual MX CLOSE response: unit=2, unitName=元, raw 19.15, display 19.15元.
    table = _table("元")
    table["fieldSet"][0]["unit"] = "2"
    table["rawTable"]["328773"] = ["19.15", None, "19.01"]
    table["table"] = {
        "headName": [day + "(日)" for day in table["rawTable"]["headName"]],
        "328773": ["19.15元", None, "19.01元"],
    }
    series = await _prepare(table, unit=target)
    divisor = Decimal(1) if target == "元" else Decimal(10000)
    assert series.points[0].values[0].value == Decimal("19.01") / divisor
    params = json.loads(series.points[0].values[0].source_parameters or "{}")
    assert params["unitProof"]["sourceMetadata"]["unit"] == "2"
    assert params["unitProof"]["verifiedDisplayCount"] == 2


@pytest.mark.asyncio
async def test_coded_unit_display_can_use_different_currency_scale() -> None:
    table = _table("万元")
    table["fieldSet"][0]["unit"] = "4"
    table["rawTable"]["328773"] = ["191500", None, "190100"]
    table["table"] = {
        "headName": table["rawTable"]["headName"], "328773": ["19.15万元", None, "19.01万元"],
    }
    series = await _prepare(table, unit="元")
    assert series.points[0].values[0].value == Decimal("190100")


@pytest.mark.asyncio
@pytest.mark.parametrize("display", [["19.15元", None, "19.01万元"], ["19.15", None, "19.01"]])
async def test_coded_unit_does_not_admit_conflicting_or_unitless_display(
    display: list[str | None],
) -> None:
    table = _table("元")
    table["fieldSet"][0]["unit"] = "2"
    table["rawTable"]["328773"] = ["19.15", None, "19.01"]
    table["table"] = {"headName": table["rawTable"]["headName"], "328773": display}
    with pytest.raises(SkillNumericHistoryError, match="单位尚未确认"):
        await _prepare(table, unit="元")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,display,scale", [
    (["0.05", None, "0.06"], ["5.00%", None, "6.00%"], "100"),
    (["5", None, "6"], ["5.00%", None, "6.00%"], "1"),
])
async def test_ambiguous_percent_label_requires_same_date_display_evidence(
    raw: list[str | None], display: list[str | None], scale: str,
) -> None:
    table = _table("100%")
    table["rawTable"]["328773"] = raw
    table["table"] = {"headName": table["rawTable"]["headName"], "328773": display}
    table["jumpUrl"] = "https://example.invalid/should-not-be-copied-per-day"
    series = await _prepare(table, unit="%")
    assert series.points[0].values[0].value == Decimal("6")
    params = json.loads(series.points[0].values[0].source_parameters or "{}")
    assert params["scale"] == scale
    assert len(params["unitProof"]["displaySamples"]) == 2
    assert params["unitProof"]["verifiedDisplayCount"] == 2
    assert "table" not in params["tableMetadata"][0]
    assert "jumpUrl" not in params["tableMetadata"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["(日)", "（日）"])
async def test_percent_display_matches_exact_daily_granularity_suffix(suffix: str) -> None:
    table = _table("100%")
    table["rawTable"]["328773"] = ["0.05", None, "0.06"]
    table["table"] = {
        "headName": [day + suffix for day in table["rawTable"]["headName"]],
        "328773": ["5.00%", None, "6.00%"],
    }
    series = await _prepare(table, unit="%")
    assert series.points[0].values[0].value == Decimal("6")
    parameters = json.loads(series.points[0].values[0].source_parameters or "{}")
    assert parameters["scale"] == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem,code", [
    ("multiple_numeric", "ambiguous_field"), ("second_metric_missing", "ambiguous_field"),
    ("unknown_unit", "unit_unconfirmed"), ("wrong_dimension", "unit_mismatch"),
    ("percent_no_evidence", "unit_unconfirmed"), ("percent_conflict", "unit_unconfirmed"),
    ("percent_only_one_evidence", "unit_unconfirmed"),
    ("unit_numeric_multiplier", "unit_unconfirmed"), ("weekly", "non_daily_history"),
    ("security", "security_mismatch"), ("missing_security", "security_missing"),
    ("duplicate_dates", "invalid_history"), ("wrong_length", "invalid_history"),
    ("non_finite", "invalid_history"),
])
async def test_binding_rejects_ambiguity_and_unit_guesses(problem: str, code: str) -> None:
    table, unit = _table(), "倍"
    if problem in {"multiple_numeric", "second_metric_missing"}:
        table["fieldSet"].append({
            "returnCode": "other", "returnName": "其他真实指标", "unitName": "倍",
        })
        table["rawTable"]["other"] = (
            [2, 3, 4] if problem == "multiple_numeric" else [None, None, None]
        )
    elif problem == "unknown_unit":
        table["fieldSet"][0]["unitName"] = "供应商自定义尺度"
    elif problem == "wrong_dimension":
        unit = "元"
    elif problem.startswith("percent_"):
        table, unit = _table("100%"), "%"
        if problem != "percent_no_evidence":
            table["rawTable"]["328773"] = ["0.05", None, "0.06"]
            table["table"] = {
                "headName": table["rawTable"]["headName"],
                "328773": ["5.00%", None, "0.06%"],
            }
            if problem == "percent_only_one_evidence":
                table["rawTable"]["328773"][2] = None
                table["table"]["328773"][2] = None
    elif problem == "unit_numeric_multiplier":
        table["fieldSet"][0]["unit"] = "10000"
    elif problem == "weekly":
        table["fieldSet"][0]["dateGranularity"] = "WEEK"
    elif problem == "security":
        table["entityCode"] = "300059"
    elif problem == "missing_security":
        del table["entityCode"]
    elif problem == "duplicate_dates":
        table["rawTable"]["headName"][1] = table["rawTable"]["headName"][0]
    elif problem == "wrong_length":
        table["rawTable"]["328773"].pop()
    elif problem == "non_finite":
        table["rawTable"]["328773"][0] = "NaN"
    with pytest.raises(SkillNumericHistoryError) as caught:
        await _prepare(table, unit=unit)
    assert caught.value.code == code
    assert caught.value.message == str(caught.value)
    assert caught.value.metric_query == _condition(unit).params["metric_query"]
    if problem in {"security", "missing_security", "duplicate_dates", "wrong_length", "non_finite"}:
        assert len(caught.value.discovery_attempts) == (
            2 if problem in {"security", "missing_security"} else 1
        )


@pytest.mark.asyncio
async def test_real_clock_is_used_and_missing_clock_uses_internal_assumption() -> None:
    table = _table()
    table["fieldSet"].append({
        "returnCode": "pub", "returnSourceCode": "first_available_at", "returnName": "可得时间",
    })
    table["rawTable"]["pub"] = [None, None, "2026-09-01T18:00:00+08:00"]
    series = await _prepare(table)
    assert series.points[0].first_available_at.isoformat() == "2026-09-01T18:00:00+08:00"
    first_params = json.loads(series.points[0].values[0].source_parameters or "{}")
    last_params = json.loads(series.points[1].values[0].source_parameters or "{}")
    assert first_params["assumedTime"] is False and last_params["assumedTime"] is True
    assert first_params["bindingHash"] == last_params["bindingHash"]
    assert first_params["sourceAvailableAt"] == ["2026-09-01T18:00:00+08:00"]


@pytest.mark.asyncio
async def test_same_binding_is_deduplicated_and_changed_field_parameter_changes_hash() -> None:
    table = _table()
    series = await _prepare(table, copy.deepcopy(table))
    assert len(series.points) == 2
    old_hash = json.loads(series.points[0].values[0].source_parameters or "{}")["bindingHash"]
    table["fieldSet"][0]["fixedParamValue"] = "Period=2"
    changed = await _prepare(table, copy.deepcopy(table))
    new_hash = json.loads(changed.points[0].values[0].source_parameters or "{}")["bindingHash"]
    assert old_hash != new_hash


def test_error_never_echoes_provider_or_caller_message() -> None:
    error = SkillNumericHistoryError("unknown", "https://private.invalid?token=secret")
    assert error.code == "invalid_history" and "secret" not in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["current_only", "all_null", "missing_numeric"])
async def test_requery_message_only_follows_two_actual_discovery_attempts(shape: str) -> None:
    table = _table()
    if shape == "current_only":
        table["rawTable"] = {"headName": ["2026-09-03"], "328773": [19]}
    elif shape == "all_null":
        table["rawTable"]["328773"] = [None, None, None]
    else:
        del table["rawTable"]["328773"]
    with pytest.raises(SkillNumericHistoryError) as caught:
        await _prepare(table)
    assert caught.value.code == "history_unavailable_after_query_retry"
    assert "重新查询" in caught.value.message
    assert len(caught.value.discovery_attempts) == 2


@pytest.mark.asyncio
async def test_two_real_no_data_failures_preserve_internal_attempts_without_fake_hash() -> None:
    provider = Mock(query_finance=AsyncMock(side_effect=MxSaasProviderNoDataError("no data")))
    with pytest.raises(SkillNumericHistoryError) as caught:
        await prepare_skill_numeric_series(
            SkillSeriesDiscovery(provider, decoder=MxFinanceHistoryDecoder()),
            condition=_condition(), instrument_id="600519.SH",
            start=START, end=END,
        )
    assert provider.query_finance.await_count == 2
    assert caught.value.code == "history_unavailable_after_query_retry"
    assert len(caught.value.discovery_attempts) == 2
    assert all(attempt.response_hash is None for attempt in caught.value.discovery_attempts)
    assert caught.value.__cause__ is not None


def _compare_condition(trigger: str = "above") -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="provider.series_compare", definition_version="1.0.0", trigger=trigger,
        params={
            "left_metric_query": "历史指标甲", "right_metric_query": "历史指标乙", "unit": "元",
        },
    )


def _compare_tables() -> tuple[dict[str, Any], dict[str, Any]]:
    left, right = _table("万元"), _table("元")
    for table in (left, right):
        table["rawTable"]["headName"] = [f"2026-09-0{i}" for i in range(1, 5)]
    left["rawTable"]["328773"] = [10, 12, 11, 8]
    right["rawTable"]["328773"] = [110000, 110000, 110000, 90000]
    right["fieldSet"][0].update({"returnSourceCode": "RIGHT", "returnName": "历史指标乙"})
    return left, right


def _comparison_discovery(*tables: dict[str, Any]) -> tuple[SkillSeriesDiscovery, AsyncMock]:
    provider = AsyncMock(side_effect=[
        LiveFinanceDataResult(
            provider="eastmoney_mx_finance_data", query=f"真实查询{index}", indicators=None,
            tables=(table,), provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + str(index + 1) * 64,
                retrieved_at=datetime(2026, 9, 7, index, tzinfo=UTC), schema_version="test.v1",
            ),
        ) for index, table in enumerate(tables)
    ])
    return SkillSeriesDiscovery(Mock(query_finance=provider),
                                decoder=MxFinanceHistoryDecoder()), provider


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_side", [0, 1])
async def test_failed_comparison_identifies_actual_operand_without_guessing_unit(failed_side):
    tables = _compare_tables()
    tables[failed_side]["fieldSet"][0].pop("unitName")
    tables[failed_side]["fieldSet"][0].pop("unitDesc")
    tables[failed_side]["fieldSet"][0].pop("unit")
    responses = [*tables[:failed_side], tables[failed_side], tables[failed_side]]
    discovery, calls = _comparison_discovery(*responses)
    with pytest.raises(SkillNumericHistoryError) as caught:
        await prepare_skill_numeric_series(
            discovery, condition=_compare_condition(), instrument_id="600519.SH",
            start=START, end=END,
        )
    assert caught.value.code == "unit_unconfirmed"
    assert caught.value.metric_query == ("历史指标甲", "历史指标乙")[failed_side]
    assert calls.await_count == failed_side + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unit_unknown", "unit_mismatch", "security"])
@pytest.mark.parametrize("recovered", [True, False])
async def test_numeric_binding_retries_rejected_response_once(failure, recovered):
    bad = _table()
    if failure == "security":
        bad["entityCode"] = "300059"
        code = "security_mismatch"
    elif failure == "unit_mismatch":
        bad["fieldSet"][0].update(unitName="元", unitDesc="元")
        code = "unit_mismatch"
    else:
        for name in ("unit", "unitName", "unitDesc"):
            bad["fieldSet"][0].pop(name)
        code = "unit_unconfirmed"
    bad["rawTable"]["328773"] = [999, None, 888]
    discovery, calls = _comparison_discovery(bad, _table() if recovered else bad)
    if recovered:
        series = await prepare_skill_numeric_series(
            discovery, condition=_condition(), instrument_id="600519.SH", start=START, end=END,
        )
        assert [p.values[0].value for p in series.points] == [Decimal("18.25"), Decimal(19)]
        assert series.response_sha256 == "sha256:" + "2" * 64
    else:
        with pytest.raises(SkillNumericHistoryError) as caught:
            await prepare_skill_numeric_series(
                discovery, condition=_condition(), instrument_id="600519.SH", start=START, end=END,
            )
        assert caught.value.code == code
    assert calls.await_count == 2
    for call in calls.await_args_list:
        assert "600519.SH" in call.kwargs["query"]
        assert _condition().params["metric_query"] in call.kwargs["query"]
        assert str(START) in call.kwargs["query"] and str(END) in call.kwargs["query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [
    MxSaasProviderNoDataError, MxSaasProviderDataError, MxSaasProviderUnavailableError, None,
])
async def test_failed_alternate_cannot_erase_real_history_unit_rejection(error_type):
    bad = _table("%")
    # Actual ROETTM response metadata: a coded unit descriptor is unresolved.
    bad["fieldSet"][0]["unitDesc"] = "108:%:%"
    discovery = _discovery(bad)
    calls = discovery._provider.query_finance
    empty = copy.deepcopy(calls.return_value)
    empty.tables[0]["rawTable"] = {}
    calls.side_effect = [calls.return_value,
                         error_type("private provider detail") if error_type else empty]
    with pytest.raises(SkillNumericHistoryError) as caught:
        await prepare_skill_numeric_series(
            discovery, condition=_condition("%"), instrument_id="600519.SH",
            start=START, end=END,
        )
    assert caught.value.code == "unit_unconfirmed"
    assert len(caught.value.discovery_attempts) == 2
    assert caught.value.discovery_attempts[0].response_hash is not None
    assert calls.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,temporary", [
    (MxSaasProviderUnavailableError, True),
    (MxSaasProviderSqlError, True),
    (MxSaasProviderAuthError, False),
    (MxSaasProviderDataError, False),
])
async def test_only_temporary_history_errors_invite_waiting_and_retry(error_type, temporary):
    discovery = _discovery(_table("%"))
    calls = discovery._provider.query_finance
    calls.side_effect = error_type("private provider detail")
    with pytest.raises(SkillNumericHistoryError) as caught:
        await prepare_skill_numeric_series(
            discovery, condition=_condition("%"), instrument_id="600519.SH",
            start=START, end=END,
        )
    assert caught.value.code == ("query_temporarily_unavailable" if temporary else "query_failed")
    assert ("暂时不太稳定" in caught.value.message) is temporary
    assert "private provider detail" not in caught.value.message
    assert calls.await_count == (2 if temporary else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger,expected", [
    ("above", [False, True, False, False]), ("below", [True, False, False, True]),
    ("at_least", [False, True, True, False]), ("at_most", [True, False, True, True]),
    ("crosses_above", [None, True, False, False]),
    ("crosses_below", [None, False, False, True]),
])
async def test_comparison_converts_both_sides_and_uses_dynamic_operands(
    trigger: str, expected: list[bool | None],
) -> None:
    discovery, calls = _comparison_discovery(*_compare_tables())
    condition = _compare_condition(trigger)
    series = await prepare_skill_numeric_series(
        discovery, condition=condition, instrument_id="600519.SH", start=START, end=END,
    )
    dates = tuple(date(2026, 9, i) for i in range(1, 5))
    timeline = _skill_numeric_timeline(condition, series, dates)
    assert [None if fact is None else fact.triggered for fact in timeline] == expected
    assert series.indicator_id == "provider.series_compare"
    assert calls.await_count == 2
    assert series.points[0].values[0].value == Decimal(100000)
    assert series.points[0].values[1].value == Decimal(110000)
    for index, value in enumerate(series.points[0].values):
        side = ("left", "right")[index]
        evidence = json.loads(value.source_parameters or "{}")
        assert value.field_name == f"{side}_value"
        assert evidence["operand"] == side
        assert evidence["sourceQuery"] == f"真实查询{index}"
        assert evidence["sourceSeriesResponseHash"] == "sha256:" + str(index + 1) * 64
        assert evidence["responseHash"] == evidence["sourceSeriesResponseHash"]
        assert evidence["comparisonUnit"] == "元"
        assert evidence["scale"] == ("10000", "1")[index]
        assert evidence["bindingHash"].startswith("sha256:")
    assert series.retrieved_at == datetime(2026, 9, 7, 1, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("left_basis,right_basis,accepted", [
    ("1", "1", True), ("2", "2", True), ("1", "2", False), ("2", "3", False),
    (None, None, True),
])
async def test_comparison_requires_consistent_declared_adjustment(
    left_basis: str | None, right_basis: str | None, accepted: bool,
) -> None:
    left, right = _compare_tables()
    for table, basis, period in ((left, left_basis, 5), (right, right_basis, 20)):
        table["fieldSet"][0]["fixedParamValue"] = (
            f"N={period},Period=1" + (f",AdjustFlag={basis}" if basis else "")
        )
    discovery, _ = _comparison_discovery(left, right)
    pending = prepare_skill_numeric_series(
        discovery, condition=_compare_condition("above"),
        instrument_id="600519.SH", start=START, end=END,
    )
    if accepted:
        assert len((await pending).points) == 4
    else:
        with pytest.raises(SkillNumericHistoryError, match="复权口径不一致") as caught:
            await pending
        assert caught.value.code == "adjustment_mismatch"


@pytest.mark.asyncio
async def test_comparison_pairs_by_exact_date_and_never_carries_missing_values() -> None:
    left, right = _compare_tables()
    right["rawTable"]["headName"] = ["2026-09-04", "2026-09-03", "2026-09-01"]
    right["rawTable"]["328773"] = [90000, 110000, 110000]
    discovery, _ = _comparison_discovery(left, right)
    condition = _compare_condition("crosses_above")
    series = await prepare_skill_numeric_series(
        discovery, condition=condition, instrument_id="600519.SH", start=START, end=END,
    )
    dates = tuple(date(2026, 9, i) for i in range(1, 5))
    assert tuple(point.session_date for point in series.points) == (dates[0], dates[2], dates[3])
    timeline = _skill_numeric_timeline(condition, series, dates)
    assert timeline[:3] == (None, None, None)
    assert timeline[3] is not None and not timeline[3].triggered


@pytest.mark.asyncio
async def test_comparison_respects_both_current_and_previous_operand_availability() -> None:
    left, right = _compare_tables()
    left["fieldSet"][0]["firstAvailableAt"] = ["2026-09-04T18:00:00+08:00", None, None, None]
    right["fieldSet"][0]["firstAvailableAt"] = [None, "2026-09-02T19:00:00+08:00", None, None]
    discovery, _ = _comparison_discovery(left, right)
    condition = _compare_condition("crosses_above")
    series = await prepare_skill_numeric_series(
        discovery, condition=condition, instrument_id="600519.SH", start=START, end=END,
    )
    assert series.points[1].first_available_at.isoformat() == "2026-09-02T19:00:00+08:00"
    timeline = _skill_numeric_timeline(
        condition, series, tuple(point.session_date for point in series.points),
    )
    assert timeline[1] is not None and timeline[1].triggered
    assert timeline[1].available_at.isoformat() == "2026-09-04T18:00:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [
    ("disjoint", "unaligned_history"), ("snapshot", "history_unavailable_after_query_retry"),
    ("null", "history_unavailable_after_query_retry"), ("unit", "unit_mismatch"),
    ("security", "security_mismatch"),
])
async def test_comparison_refuses_unusable_operand_without_inventing_history(
    failure: str, code: str,
) -> None:
    left, right = _compare_tables()
    if failure == "disjoint":
        left["rawTable"]["headName"] = ["2026-09-01", "2026-09-02"]
        left["rawTable"]["328773"] = [10, 12]
        right["rawTable"]["headName"] = ["2026-09-03", "2026-09-04"]
        right["rawTable"]["328773"] = [110000, 90000]
    elif failure == "snapshot":
        right["rawTable"] = {"headName": ["2026-09-04"], "328773": [90000]}
    elif failure == "null":
        right["rawTable"]["328773"] = [None] * 4
    elif failure == "unit":
        right["fieldSet"][0].update({"unitName": "股", "unitDesc": "股"})
    else:
        right["entityCode"] = "300059"
    discovery, calls = _comparison_discovery(left, right, right)
    with pytest.raises(SkillNumericHistoryError) as caught:
        await prepare_skill_numeric_series(
            discovery, condition=_compare_condition(), instrument_id="600519.SH",
            start=START, end=END,
        )
    assert caught.value.code == code
    assert calls.await_count == (2 if failure == "disjoint" else 3)
