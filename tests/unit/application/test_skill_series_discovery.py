import copy
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ashare_lab.adapters.market_data.mx_finance_history_format import MxFinanceHistoryDecoder
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
    observe_mx_retries,
)
from ashare_lab.application.skill_series_discovery import (
    SkillSeriesDiscovery,
    SkillSeriesDiscoveryNoHistoryError,
)
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataProvenance

START, END = date(2026, 9, 1), date(2026, 9, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["recover", "both_invalid", "missing"])
async def test_authoritative_sessions_bound_both_channels_without_trimming(mode: str) -> None:
    first = table()
    clean = copy.deepcopy(first)
    clean["rawTable"] = {"headName": ["2026-09-01", "2026-09-03"],
                         "actual_pe_ttm": [18.25, 19]}
    _, fixture = service(first)
    base = fixture.query_finance.return_value

    class Provider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def query_finance(self, *, query: str, indicators: str | None):
            self.calls.append("finance")
            return replace(base, query=query, tables=(clean if mode == "missing" else first,))

        async def query_finance_via_screen(self, *, query: str, indicators: str | None):
            self.calls.append("screen")
            return replace(base, query=query, provider="eastmoney_mx_screener",
                           tables=(first if mode == "both_invalid" else clean,))

    provider = Provider()
    result = await SkillSeriesDiscovery(provider, decoder=MxFinanceHistoryDecoder()).discover(
        instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
        expected_session_dates=(START, date(2026, 9, 3), END),
    )
    assert provider.calls == (["finance"] if mode == "missing" else ["finance", "screen"])
    if mode == "both_invalid":
        assert result.status == "unavailable" and result.candidate_table_indices == ()
        assert "dates_outside_authoritative_sessions" in result.issues
        assert date(2026, 9, 2) in result.tables[0].dates  # Retain rejected evidence, don't trim.
    else:
        assert result.status == "discovered"
        assert result.tables[0].dates == (START, date(2026, 9, 3))  # Don't fill missing END.
    if mode != "missing":
        assert "dates_outside_authoritative_sessions" in result.attempts[0].issues


@pytest.mark.asyncio
async def test_requery_failure_preserves_actual_report_period_fields_without_binding() -> None:
    first = table()
    first["rawTable"]["headName"] = ["2026Q1", "2026Q2", "2026Q3"]
    discovery, provider = service(first)
    provider.query_finance.side_effect = [provider.query_finance.return_value,
        MxSaasProviderDataError("private provider payload")]
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="ROE(TTM)", start=START, end=END,
    )
    assert result.status == "unavailable"
    assert result.candidate_table_indices == ()
    assert result.tables[0].fields[0].unit is not None
    assert "historical_requery_failed" in result.issues
    assert "field_length_mismatch" not in str(result.issues)
    assert len(result.attempts) == 2
    assert result.attempts[0].response_hash == result.response_hash
    assert result.attempts[1].response_hash is None


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_alternate", [True, False])
async def test_alternate_skill_reuses_actual_dated_values_without_broadcast(
    valid_alternate: bool,
) -> None:
    current = table()
    current["rawTable"] = {"headName": ["2026-09-03"], "actual_pe_ttm": [19]}
    provenance = LiveMarketDataProvenance(
        response_sha256="sha256:" + "b" * 64,
        retrieved_at=datetime(2026, 9, 7, tzinfo=UTC), schema_version="test.screen.v1",
    )

    class DualProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            self.calls.append(("finance", query))
            return LiveFinanceDataResult(
                provider="eastmoney_mx_finance_data", query=query, indicators=indicators,
                tables=(current,), provenance=replace(
                    provenance, response_sha256="sha256:" + "a" * 64,
                ),
            )

        async def query_finance_via_screen(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            self.calls.append(("screen", query))
            return LiveFinanceDataResult(
                provider="eastmoney_mx_screener", query=query, indicators=indicators,
                tables=(table() if valid_alternate else current,), provenance=provenance,
            )

    provider = DualProvider()
    result = await SkillSeriesDiscovery(provider, decoder=MxFinanceHistoryDecoder()).discover(
        instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
    )
    assert [kind for kind, _ in provider.calls] == ["finance", "screen"]
    assert all(all(token in query for token in ("600519.SH", "PE(TTM)", str(START), str(END)))
               for _, query in provider.calls)
    assert result.provider == "eastmoney_mx_screener" and len(result.attempts) == 2
    assert result.response_hash == provenance.response_sha256
    assert result.status == ("discovered" if valid_alternate else "unavailable")
    assert len(result.tables[0].dates) == (3 if valid_alternate else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "first_auth", "second_auth", "second_unavailable"])
async def test_transport_failure_reaches_alternate_without_bypassing_auth(
    failure: str | None,
) -> None:
    class DualProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            self.calls.append(("finance", query, indicators))
            if failure == "first_auth":
                raise MxSaasProviderAuthError("denied")
            raise MxSaasProviderUnavailableError("connection failed")

        async def query_finance_via_screen(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            self.calls.append(("screen", query, indicators))
            if failure == "second_auth":
                raise MxSaasProviderAuthError("denied")
            if failure == "second_unavailable":
                raise MxSaasProviderUnavailableError("connection still failed")
            return LiveFinanceDataResult(
                provider="eastmoney_mx_screener", query=query, indicators=indicators,
                tables=(table(),), provenance=LiveMarketDataProvenance(
                    response_sha256="sha256:" + "b" * 64,
                    retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
                    schema_version="test.screen.v1",
                ),
            )

    provider = DualProvider()
    discovery = SkillSeriesDiscovery(provider, decoder=MxFinanceHistoryDecoder())
    if failure:
        expected = (MxSaasProviderUnavailableError if failure == "second_unavailable"
                    else MxSaasProviderAuthError)
        with pytest.raises(expected):
            await discovery.discover(
                instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
            )
    else:
        result = await discovery.discover(
            instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
        )
        assert result.status == "discovered" and result.provider == "eastmoney_mx_screener"
        assert result.attempts[0].response_hash is None
        assert result.attempts[0].issues == ("provider_temporarily_unavailable",)
        assert len(result.attempts) == 2
    assert [kind for kind, _, _ in provider.calls] == (
        ["finance"] if failure == "first_auth" else ["finance", "screen"]
    )
    for _, query, indicators in provider.calls:
        assert all(value in query for value in ("600519.SH", "PE(TTM)", str(START), str(END)))
        assert indicators == f"PE(TTM)，{START}至{END}，逐日历史数值"


def table() -> dict[str, Any]:
    return {
        "entityCode": "600519", "title": "供应商返回历史指标",
        "fieldSet": [{
            "returnCode": "actual_pe_ttm", "returnName": "市盈率(TTM)",
            "returnSourceCode": "actual_vendor_source", "unitName": "倍",
            "fixedParamValue": '{"basis":"TTM"}',
        }],
        "rawTable": {
            "headName": ["2026-09-01", "2026-09-02", "2026-09-03"],
            "actual_pe_ttm": ["18.25", None, 19],
        },
    }


def service(*tables: dict[str, Any]) -> tuple[SkillSeriesDiscovery, Mock]:
    provider = Mock(query_finance=AsyncMock(return_value=LiveFinanceDataResult(
        provider="eastmoney_mx_finance_data", query="actual query", indicators=None,
        tables=tuple(tables), provenance=LiveMarketDataProvenance(
            response_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 9, 7, tzinfo=UTC), schema_version="test.v1",
        ),
    )))
    return SkillSeriesDiscovery(provider, decoder=MxFinanceHistoryDecoder()), provider


@pytest.mark.asyncio
async def test_discovers_real_fields_without_catalog_and_preserves_missing_and_units() -> None:
    raw = table()
    raw["fieldSet"][0]["unitName"] = "100%"
    raw["fieldSet"].append({"returnCode": "invented_without_values", "returnName": "不存在"})
    discovery, provider = service(raw)
    query = "供应商任意新指标（用户参数=23，不在37个目录内）"
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query=query, start=START, end=END,
    )
    assert result.status == "discovered" and result.instrument_verified
    assert result.candidate_table_indices == (0,)
    assert not result.daily_frequency_verified and not result.pit_verified
    assert result.response_hash == "sha256:" + "a" * 64
    assert result.request_hash.startswith("sha256:")
    assert len(result.tables[0].fields) == 1
    field = result.tables[0].fields[0]
    assert field.return_code == "actual_pe_ttm" and field.return_name == "市盈率(TTM)"
    assert field.return_source_code == "actual_vendor_source"
    assert field.fixed_param_value == '{"basis":"TTM"}'
    assert field.unit == "100%"  # No label rewrite, scaling or inferred meaning.
    assert field.values == (Decimal("18.25"), None, Decimal("19"))
    assert field.missing_indices == (1,)
    assert json.loads(field.raw_values_json) == ["18.25", None, 19]
    assert "invented_without_values" in result.tables[0].metadata_json
    actual = provider.query_finance.await_args.kwargs
    assert actual["indicators"] == f"{query}，{START}至{END}，逐日历史数值"
    assert "600519" not in actual["indicators"]
    assert query in actual["query"]
    request = {
        "schema_version": "skill-series-discovery.v1", "instrument_id": "600519.SH",
        "metric_query": query, "start": START.isoformat(), "end": END.isoformat(),
        "query": actual["query"], "indicators": actual["indicators"],
    }
    assert result.request_hash == "sha256:" + hashlib.sha256(json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        result.status = "unavailable"  # pyright: ignore[reportAttributeAccessIssue]
    raw["rawTable"]["actual_pe_ttm"][0] = "999"
    assert field.values[0] == Decimal("18.25")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    ("wrong_stock", "security_mismatch_or_ambiguous"),
    ("missing_stock", "security_missing"),
    ("extra_stock", "security_mismatch_or_ambiguous"),
    ("duplicate_dates", "duplicate_dates"),
    ("out_of_range", "dates_outside_requested_range"),
    ("quarter_axis", "invalid_or_non_daily_date_axis"),
    ("timestamp_axis", "timestamp_axis_requires_frequency_verification"),
    ("one_row", "insufficient_history_or_current_only"),
    ("wrong_length", "field_length_mismatch"),
    ("nan", "non_finite_or_non_numeric_value[0]"),
    ("boolean", "non_finite_or_non_numeric_value[0]"),
    ("yes", "non_finite_or_non_numeric_value[0]"),
    ("all_missing", "no_numeric_observations"),
    ("duplicate_field", "field_metadata_missing_or_ambiguous"),
    ("no_raw", "raw_table_missing"),
])
async def test_invalid_history_is_not_discovered_but_keeps_metadata(
    failure: str, expected: str,
) -> None:
    raw = table()
    axis, values = raw["rawTable"]["headName"], raw["rawTable"]["actual_pe_ttm"]
    if failure == "wrong_stock":
        raw["entityCode"] = "300059"
    elif failure == "missing_stock":
        del raw["entityCode"]
    elif failure == "extra_stock":
        raw["stockCode"] = "300059"
    elif failure == "duplicate_dates":
        axis[1] = axis[0]
    elif failure == "out_of_range":
        axis[0] = "2026-08-31"
    elif failure == "quarter_axis":
        axis[0] = "2026Q3"
    elif failure == "timestamp_axis":
        axis[0] = "2026-09-01 09:35:00"
    elif failure == "one_row":
        del axis[1:]
        del values[1:]
    elif failure == "wrong_length":
        values.pop()
    elif failure in {"nan", "boolean", "yes"}:
        values[0] = {"nan": "NaN", "boolean": True, "yes": "是"}[failure]
    elif failure == "all_missing":
        raw["rawTable"]["actual_pe_ttm"] = [None, None, None]
    elif failure == "duplicate_field":
        raw["fieldSet"].append(copy.deepcopy(raw["fieldSet"][0]))
    elif failure == "no_raw":
        del raw["rawTable"]
    discovery, provider = service(raw)
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="每日PE(TTM)", start=START, end=END,
    )
    assert result.status == "unavailable" and not result.candidate_table_indices
    assert any(expected in issue for issue in result.issues)
    assert "actual_pe_ttm" in result.tables[0].metadata_json
    if failure in {
            "nan", "boolean", "duplicate_field",
        "duplicate_dates", "wrong_length", "timestamp_axis",
    }:
        provider.query_finance.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["date_axis", "values", "definition"])
async def test_ambiguous_tables_do_not_select_a_guess(conflict: str) -> None:
    first, second = table(), table()
    if conflict == "date_axis":
        second["rawTable"]["headName"][2] = "2026-09-04"
    elif conflict == "values":
        second["rawTable"]["actual_pe_ttm"][0] = 99
    else:
        second["fieldSet"][0]["fixedParamValue"] = '{"basis":"LYR"}'
    discovery, _ = service(first, second)
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="每日PE", start=START, end=END,
    )
    assert result.status == "unavailable" and len(result.tables) == 2
    assert result.issues == (
        "ambiguous_historical_date_axes" if conflict == "date_axis"
        else "conflicting_historical_fields",
    )


@pytest.mark.asyncio
async def test_request_identity_and_invalid_input_before_query() -> None:
    discovery, provider = service(table())
    first = await discovery.discover(
        instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
    )
    second = await discovery.discover(
        instrument_id="600519.SH", metric_query="PE(LYR)", start=START, end=END,
    )
    assert first.request_hash != second.request_hash
    with pytest.raises(ValueError, match="canonical"):
        await discovery.discover(
            instrument_id="600519", metric_query="PE", start=START, end=END,
        )
    assert provider.query_finance.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["current_only", "all_null", "missing_numeric", "quarter_axis"])
async def test_incomplete_history_retries_a_simpler_historical_query_once(shape: str) -> None:
    discovery, provider = service(table())
    full = provider.query_finance.return_value
    current = copy.deepcopy(table())
    if shape == "current_only":
        current["rawTable"] = {"headName": ["2026-09-03"], "actual_pe_ttm": [19]}
    elif shape == "all_null":
        current["rawTable"]["actual_pe_ttm"] = [None, None, None]
    elif shape == "quarter_axis":
        current["rawTable"]["headName"] = ["2026Q1", "2026Q2", "2026Q3"]
    else:
        del current["rawTable"]["actual_pe_ttm"]
    provider.query_finance.side_effect = [replace(full, tables=(current,)), full]
    progress = Mock()
    with observe_mx_retries(progress):
        result = await discovery.discover(
            instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
        )
    assert progress.call_count == 2
    retry, recovered = (call.args[0] for call in progress.call_args_list)
    assert retry.data_incomplete and not retry.recovered
    assert recovered.recovered and recovered.call_id == retry.call_id
    assert result.status == "discovered"
    assert provider.query_finance.await_count == 2
    queries = [call.kwargs["query"] for call in provider.query_finance.await_args_list]
    assert queries[0] != queries[1]
    for query in queries:
        assert all(value in query for value in ("600519.SH", "2026-09-01", "2026-09-04", "PE(TTM)"))
    assert all(
        call.kwargs["indicators"] == f"PE(TTM)，{START}至{END}，逐日历史数值"
        for call in provider.query_finance.await_args_list
    )
    assert len(result.attempts) == 2
    assert "no_valid_numeric_history" in result.attempts[0].issues
    assert result.attempts[1].issues == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["current_only", "all_null", "missing_numeric"])
async def test_repeated_missing_history_does_not_loop_or_create_history(shape: str) -> None:
    current = table()
    if shape == "current_only":
        current["rawTable"] = {"headName": ["2026-09-03"], "actual_pe_ttm": [19]}
    elif shape == "all_null":
        current["rawTable"]["actual_pe_ttm"] = [None, None, None]
    else:
        del current["rawTable"]["actual_pe_ttm"]
    discovery, provider = service(current)
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
    )
    assert result.status == "unavailable" and len(result.attempts) == 2
    assert provider.query_finance.await_count == 2
    assert result.tables[0].dates == (
        (date(2026, 9, 3),) if shape == "current_only"
        else (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3))
    )
    assert not result.candidate_table_indices


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "wrong_stock", "duplicate_dates", "wrong_length", "non_finite",
])
async def test_empty_values_retry_wrong_stock_only(invalid: str) -> None:
    raw = table()
    raw["rawTable"]["actual_pe_ttm"] = [None, None, None]
    if invalid == "wrong_stock":
        raw["entityCode"] = "300059"
    elif invalid == "duplicate_dates":
        raw["rawTable"]["headName"][1] = raw["rawTable"]["headName"][0]
    elif invalid == "wrong_length":
        raw["rawTable"]["actual_pe_ttm"].pop()
    else:
        raw["rawTable"]["actual_pe_ttm"][0] = "NaN"
    discovery, provider = service(raw)
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
    )
    assert result.status == "unavailable" and not result.candidate_table_indices
    assert provider.query_finance.await_count == (2 if invalid == "wrong_stock" else 1)


@pytest.mark.asyncio
async def test_real_availability_metadata_is_preserved_without_synthesizing_times() -> None:
    raw = table()
    raw["fieldSet"][0].update({"frequency": "1d", "availableAt": "provider-reported-policy"})
    raw["fieldSet"].append({
        "returnCode": "actual_available_at", "returnName": "首次可得时间",
        "returnSourceCode": "availability_source", "frequency": "1d",
    })
    times = ["2026-09-01T18:00:00+08:00", None, "2026-09-03T18:30:00+08:00"]
    raw["rawTable"]["actual_available_at"] = times
    discovery, provider = service(raw)
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="每日PE(TTM)", start=START, end=END,
    )
    assert result.status == "discovered" and not result.pit_verified
    metric, availability = result.tables[0].fields
    assert json.loads(metric.metadata_json)[0]["frequency"] == "1d"
    assert json.loads(metric.metadata_json)[0]["availableAt"] == "provider-reported-policy"
    assert json.loads(availability.raw_values_json) == times
    assert not availability.has_numeric_history
    assert "每个交易日" in provider.query_finance.await_args.kwargs["query"]
    assert provider.query_finance.await_count == 1


@pytest.mark.asyncio
async def test_short_current_stub_does_not_hide_real_history() -> None:
    stub, history = table(), table()
    stub["rawTable"]["headName"] = ["2026-09-03"]
    stub["rawTable"]["actual_pe_ttm"] = [19]
    discovery, _ = service(stub, history)
    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="每日PE(TTM)", start=START, end=END,
    )
    assert result.status == "discovered" and result.candidate_table_indices == (1,)
    assert result.tables[0].issues == ("insufficient_history_or_current_only",)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_response", ["no_results", "current_only"])
async def test_second_no_results_retains_both_attempts_without_invented_provenance(
    first_response: str,
) -> None:
    discovery, provider = service(table())
    no_data = MxSaasProviderNoDataError("private provider message", tool="searchData")
    if first_response == "no_results":
        first = no_data
    else:
        current = table()
        current["rawTable"] = {"headName": ["2026-09-03"], "actual_pe_ttm": [19]}
        first = replace(provider.query_finance.return_value, tables=(current,))
    provider.query_finance.side_effect = [first, no_data]

    with pytest.raises(SkillSeriesDiscoveryNoHistoryError) as caught:
        await discovery.discover(
            instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
        )

    error = caught.value
    assert error.attempts == provider.query_finance.await_count == 2
    assert len(error.discovery_attempts) == 2
    first_attempt, last_attempt = error.discovery_attempts
    assert first_attempt.response_hash == (
        None if first_response == "no_results" else "sha256:" + "a" * 64
    )
    assert last_attempt.response_hash is None and last_attempt.issues == ("provider_no_results",)
    assert last_attempt.query == provider.query_finance.await_args_list[1].kwargs["query"]
    assert not hasattr(error, "response_hash") and not hasattr(error, "retrieved_at")
    assert "历史数据" in str(error) and "private" not in str(error)
    assert error.__cause__ is no_data


@pytest.mark.asyncio
async def test_missing_entity_with_current_only_shape_can_retry_same_stock_once() -> None:
    discovery, provider = service(table())
    full = provider.query_finance.return_value
    current = table()
    del current["entityCode"]
    current["rawTable"] = {"headName": ["2026-09-03"], "actual_pe_ttm": [19]}
    provider.query_finance.side_effect = [replace(full, tables=(current,)), full]

    result = await discovery.discover(
        instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
    )

    assert result.status == "discovered" and result.instrument_verified
    assert len(result.attempts) == provider.query_finance.await_count == 2
    assert "security_missing" in result.attempts[0].issues
    assert all(
        "600519.SH" in call.kwargs["query"] for call in provider.query_finance.await_args_list
    )


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried_or_changed_to_no_history() -> None:
    discovery, provider = service(table())
    error = MxSaasProviderAuthError("authentication failed", tool="searchData")
    provider.query_finance.side_effect = error

    with pytest.raises(MxSaasProviderAuthError) as caught:
        await discovery.discover(
            instrument_id="600519.SH", metric_query="PE(TTM)", start=START, end=END,
        )

    assert caught.value is error
    provider.query_finance.assert_awaited_once()
