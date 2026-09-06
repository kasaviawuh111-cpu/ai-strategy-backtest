from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_lab.adapters.market_data.mx_daily_history import (
    MX_BACK_ADJUSTMENT,
    MX_DAILY_HISTORY_PROVIDER,
    MxDailyHistory,
    MxDailyHistoryCacheMissError,
    MxDailyHistoryClient,
    MxDailyHistoryError,
    MxDailyHistoryFieldsMissingError,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderNoDataError,
)
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
)

_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
_START = date(2026, 9, 3)
_END = date(2026, 9, 4)


def _provenance(marker: str) -> LiveMarketDataProvenance:
    return LiveMarketDataProvenance(
        response_sha256="sha256:" + marker * 64,
        retrieved_at=_NOW + timedelta(seconds=len(marker)),
        schema_version="eastmoney-mx.search-data.v1",
    )


def _table(
    symbol: str,
    dates: list[str],
    fields: dict[str, list[object]],
) -> dict[str, object]:
    raw: dict[str, object] = {"headName": dates}
    names: dict[str, str] = {}
    for index, (name, values) in enumerate(fields.items()):
        code = f"f{index}"
        raw[code] = values
        names[code] = name
    return {
        "code": symbol,
        "entityCodes": [symbol],
        "rawTable": raw,
        "nameMap": names,
    }


class _FakeMxClient:
    def __init__(self, symbol: str, *, adjusted_dates: list[str] | None = None) -> None:
        self.symbol = symbol
        self.name = {"688981.SH": "中芯国际", "302132.SZ": "中航成飞"}.get(symbol, "中国平安")
        self.calls: list[str] = []
        self.adjusted_dates = adjusted_dates
        self.raw_close = "52"
        self.revision = 0
        self.fail = False

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        self.calls.append("identity")
        if self.fail:
            raise MxDailyHistoryError("MX refresh failed")
        await asyncio.sleep(0)
        return LiveMarketDataResult(
            provider="eastmoney_mx_screener",
            query=query,
            asset_type=asset_type,
            columns=("证券代码", "证券简称", "证券类型", "上市状态", "市场类型"),
            rows=(
                {
                    "证券代码": self.symbol.split(".", maxsplit=1)[0],
                    "证券简称": self.name,
                    "证券类型": "A股",
                    "上市状态": "正常上市",
                    "市场类型": (
                        "深圳证券交易所" if self.symbol.endswith(".SZ") else "上海证券交易所"
                    ),
                },
            ),
            provenance=_provenance("a"),
        )

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        dates = ["2026-09-04", "2026-09-03"]
        if "首发上市日" in query:
            self.calls.append("listing")
            tables = (
                _table(
                    self.symbol,
                    ["value"],
                    {
                        "首发上市日": ["2020-07-16"],
                        "股票简称": [self.name],
                        "是否上市": ["是"],
                    },
                ),
            )
            marker = "b"
        elif indicators == "前收盘价、交易状态、是否ST":
            self.calls.append("sessions")
            # MX can split fields into several exact-symbol tables on one date axis.
            tables = (
                _table(
                    self.symbol,
                    dates,
                    {
                        "交易状态": ["正常交易", "连续停牌"],
                        "是否为ST股票": ["否", "否"],
                    },
                ),
                _table(
                    self.symbol,
                    dates,
                    {
                        "前收盘价": ["50", "49"],
                    },
                ),
            )
            marker = "c"
        elif indicators == "涨停价、跌停价":
            self.calls.append("limits")
            # MX omits suspended sessions from the provider limit-price table.
            tables = (
                _table(
                    self.symbol,
                    dates[:1],
                    {
                        "涨停价": ["60"],
                        "跌停价": ["40"],
                    },
                ),
            )
            marker = "f"
        elif indicators is not None and "不复权" in indicators:
            self.calls.append("raw")
            tables = (
                _table(
                    self.symbol,
                    dates,
                    {
                        "开盘价": ["51", "49"],
                        "最高价": ["53", "49"],
                        "最低价": ["50", "49"],
                        # Real MX responses label unadjusted close this way.
                        "收盘价(不前推)": [self.raw_close, "49"],
                        "成交量": ["1000", "--"],
                        "成交额": ["52000", "--"],
                    },
                ),
            )
            marker = "d"
        else:
            self.calls.append("adjusted")
            adjusted_dates = self.adjusted_dates or dates
            adjusted_values = {
                "开盘价": ["101", "98"],
                "最高价": ["105", "98"],
                "最低价": ["100", "98"],
                "收盘价": ["104", "98"],
            }
            tables = (
                _table(
                    self.symbol,
                    adjusted_dates,
                    {
                        name: values[: len(adjusted_dates)]
                        for name, values in adjusted_values.items()
                    },
                ),
            )
            marker = "e"
        return LiveFinanceDataResult(
            provider=MX_DAILY_HISTORY_PROVIDER,
            query=query,
            indicators=indicators,
            tables=tables,
            provenance=replace(
                _provenance(marker),
                response_sha256=f"sha256:{marker}{self.revision:063x}",
                retrieved_at=_NOW + timedelta(seconds=self.revision),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["302132", "302132.SZ"])
async def test_verified_replacement_code_uses_chinext_and_preserves_provider_limits(
    tmp_path: Path, symbol: str,
) -> None:
    live = _FakeMxClient("302132.SZ")

    history = await MxDailyHistoryClient(live, tmp_path).load(symbol, _START, _END)

    assert history.instrument_id == "302132.SZ"
    assert history.board is Board.CHINEXT
    # These are synthetic provider values, not inferred from a board percentage.
    assert (history.rows[-1].upper_limit, history.rows[-1].lower_limit) == (60, 40)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    ["302131", "302133", "302131.SZ", "302133.SZ", "302132.SH", "302132.BJ"],
)
async def test_replacement_code_exception_rejects_neighbors_and_wrong_exchange_before_query(
    tmp_path: Path, symbol: str,
) -> None:
    live = _FakeMxClient("302132.SZ")

    with pytest.raises(MxDailyHistoryError, match="canonical A-share stock"):
        await MxDailyHistoryClient(live, tmp_path).load(symbol, _START, _END)

    assert live.calls == []


@pytest.mark.asyncio
async def test_load_preserves_mx_price_lanes_status_limits_and_persistent_cache(
    tmp_path: Path,
) -> None:
    live = _FakeMxClient("688981.SH")
    client = MxDailyHistoryClient(live, tmp_path)

    history = await client.load("688981.SH", _START, _END)

    assert history.instrument_id == "688981.SH"
    assert history.board is Board.STAR
    assert history.provider == MX_DAILY_HISTORY_PROVIDER
    assert history.cache_status == "live"
    assert history.adjustment == MX_BACK_ADJUSTMENT
    assert tuple(row.session_date for row in history.rows) == (_START, _END)
    suspended, trading = history.rows
    assert suspended.trading_status is TradingStatus.SUSPENDED
    assert (suspended.volume, suspended.amount) == (0, 0)
    assert (suspended.upper_limit, suspended.lower_limit) == (None, None)
    assert trading.raw_open == 51
    assert trading.adjusted_close == 104
    assert (trading.upper_limit, trading.lower_limit) == (60, 40)
    assert live.calls[:3] == ["identity", "listing", "sessions"]
    assert {item.purpose for item in history.query_evidence} == {
        "security_master",
        "listing_identity",
        "sessions",
        "limits",
        "raw_prices",
        "adjusted_prices",
    }
    assert len(tuple(tmp_path.rglob("*.json"))) == 1

    cached = await MxDailyHistoryClient(None, tmp_path).load("688981.SH", _START, _END)
    assert cached == replace(history, cache_status="disk")

    sliced = await MxDailyHistoryClient(None, tmp_path).load("688981.SH", _END, _END)
    assert (sliced.start, sliced.end) == (_END, _END)
    assert tuple(row.session_date for row in sliced.rows) == (_END,)
    assert sliced.query_evidence == history.query_evidence
    assert sliced.cache_status == "disk"


@pytest.mark.asyncio
async def test_non_allowlisted_symbol_merges_inflight_and_reuses_memory_without_persisting(
    tmp_path: Path,
) -> None:
    live = _FakeMxClient("600183.SH")
    client = MxDailyHistoryClient(live, tmp_path)

    first, concurrent = await asyncio.gather(
        client.load("600183.SH", _START, _END),
        client.load("600183.SH", _START, _END),
    )
    calls_after_first = tuple(live.calls)
    second = await client.load("600183.SH", _START, _END)

    assert concurrent == first
    assert first.cache_status == "live"
    assert second == replace(first, cache_status="memory")
    assert calls_after_first.count("identity") == 1
    assert tuple(live.calls).count("identity") == 1
    assert not tuple(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_all_symbol_memory_cache_is_bounded_and_uses_recent_access(
    tmp_path: Path,
) -> None:
    live = _FakeMxClient("600183.SH")
    client = MxDailyHistoryClient(live, tmp_path, memory_max_entries=2)
    first_start, second_start = date(2026, 9, 1), date(2026, 9, 2)

    await client.load("600183.SH", first_start, _END)
    await client.load("600183.SH", second_start, _END)
    first_hit = await client.load("600183.SH", first_start, _END)
    await client.load("600183.SH", _START, _END)
    recent_hit = await client.load("600183.SH", first_start, _END)
    evicted = await client.load("600183.SH", second_start, _END)

    assert first_hit.cache_status == recent_hit.cache_status == "memory"
    assert evicted.cache_status == "live"
    assert live.calls.count("identity") == 4
    assert len(client._memory) == 2
    assert not tuple(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("warm_memory", [False, True])
async def test_force_refresh_bypasses_history_cache_and_updates_without_mutating_old_result(
    tmp_path: Path, warm_memory: bool,
) -> None:
    live = _FakeMxClient("688981.SH")
    client = MxDailyHistoryClient(live, tmp_path)
    old = await client.load("688981.SH", _START, _END)
    if not warm_memory:
        client = MxDailyHistoryClient(live, tmp_path)
    live.raw_close = "51.5"
    live.revision = 1

    refreshed = await client.load("688981.SH", _START, _END, force_refresh=True)
    memory = await client.load("688981.SH", _START, _END)
    disk = await MxDailyHistoryClient(None, tmp_path).load("688981.SH", _START, _END)

    assert live.calls.count("identity") == 2
    assert refreshed.cache_status == "forced"
    assert refreshed.rows[-1].raw_close == Decimal("51.5")
    assert refreshed.provider == old.provider == MX_DAILY_HISTORY_PROVIDER
    assert refreshed.query_evidence != old.query_evidence
    assert memory == replace(refreshed, cache_status="memory")
    assert disk == replace(refreshed, cache_status="disk")
    assert old.cache_status == "live"
    assert old.rows[-1].raw_close == 52
    stored = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert "cache_status" not in stored["history"]
    assert "cacheStatus" not in stored["history"]


@pytest.mark.asyncio
async def test_failed_history_refresh_never_falls_back_to_warm_values(tmp_path: Path) -> None:
    live = _FakeMxClient("688981.SH")
    client = MxDailyHistoryClient(live, tmp_path)
    old = await client.load("688981.SH", _START, _END)
    live.fail = True

    with pytest.raises(MxDailyHistoryError, match="MX refresh failed"):
        await client.load("688981.SH", _START, _END, force_refresh=True)
    with pytest.raises(MxDailyHistoryCacheMissError):
        await MxDailyHistoryClient(None, tmp_path).load(
            "688981.SH", _START, _END, force_refresh=True,
        )

    assert live.calls.count("identity") == 2
    assert await client.load("688981.SH", _START, _END) == replace(old, cache_status="memory")
    assert await MxDailyHistoryClient(None, tmp_path).load(
        "688981.SH", _START, _END,
    ) == replace(old, cache_status="disk")


@pytest.mark.asyncio
async def test_history_refresh_bypasses_inflight_and_late_old_request_cannot_overwrite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await MxDailyHistoryClient(_FakeMxClient("688981.SH"), tmp_path / "fixture").load(
        "688981.SH", _START, _END,
    )
    fresh = replace(old, rows=(*old.rows[:-1], replace(old.rows[-1], raw_close=Decimal("51.5"))))
    client = MxDailyHistoryClient(_FakeMxClient("688981.SH"), tmp_path / "cache")
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(symbol: str, start: date, end: date) -> MxDailyHistory:
        nonlocal calls
        assert (symbol, start, end) == ("688981.SH", _START, _END)
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return old
        return fresh

    monkeypatch.setattr(client, "_fetch", fetch)
    ordinary = asyncio.create_task(client.load("688981.SH", _START, _END))
    await started.wait()
    try:
        refreshed = await asyncio.wait_for(
            client.load("688981.SH", _START, _END, force_refresh=True), timeout=1,
        )
    finally:
        release.set()
        original_result = await ordinary

    assert calls == 2
    assert original_result == replace(old, cache_status="live")
    assert refreshed == replace(fresh, cache_status="forced")
    assert await client.load("688981.SH", _START, _END) == replace(fresh, cache_status="memory")
    assert await MxDailyHistoryClient(None, tmp_path / "cache").load(
        "688981.SH", _START, _END,
    ) == replace(fresh, cache_status="disk")


@pytest.mark.asyncio
async def test_cache_miss_without_live_mx_and_misaligned_provider_dates_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(MxDailyHistoryCacheMissError):
        await MxDailyHistoryClient(None, tmp_path).load("688981.SH", _START, _END)

    live = _FakeMxClient("688981.SH", adjusted_dates=["2026-09-04"])
    with pytest.raises(MxDailyHistoryError, match="align one-to-one"):
        await MxDailyHistoryClient(live, tmp_path).load("688981.SH", _START, _END)


_LONG_START = date(2024, 1, 2)
_LONG_END = date(2026, 9, 4)


class _LongRangeMxClient(_FakeMxClient):
    def __init__(self) -> None:
        super().__init__("688981.SH")
        self.ranges: list[tuple[str, date, date]] = []
        self.fail_later = False
        self.conflict_overlap = False

    async def query_finance(
        self, *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        if "首发上市日" in query:
            return await super().query_finance(query=query, indicators=indicators)
        bounds = re.findall(r"\d{4}-\d{2}-\d{2}", query)
        start, end = (date.fromisoformat(value) for value in bounds[:2])
        days = [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        ]
        if indicators == "前收盘价、交易状态、是否ST":
            purpose = "sessions"
            fields = {"前收盘价": "50", "交易状态": "正常交易", "是否为ST股票": "否"}
        elif indicators == "涨停价、跌停价":
            purpose = "limits"
            fields = {"涨停价": "60", "跌停价": "40"}
        elif indicators is not None and "不复权" in indicators:
            purpose = "raw"
            fields = {
                "开盘价": "51", "最高价": "53", "最低价": "50",
                "收盘价": "52", "成交量": "1000", "成交额": "52000",
            }
        else:
            purpose = "adjusted"
            fields = {"开盘价": "101", "最高价": "105", "最低价": "100", "收盘价": "104"}
        self.ranges.append((purpose, start, end))
        if purpose == "sessions" and start > _LONG_START and self.fail_later:
            raise MxDailyHistoryError("later chunk temporarily unavailable")
        values: dict[str, list[object]] = {
            name: [value] * len(days) for name, value in fields.items()
        }
        if purpose == "adjusted" and start > _LONG_START and self.conflict_overlap:
            values["收盘价"][0] = "104.5"
        return LiveFinanceDataResult(
            provider=MX_DAILY_HISTORY_PROVIDER, query=query, indicators=indicators,
            tables=(_table(self.symbol, [day.isoformat() for day in days], values),),
            provenance=_provenance("b"),
        )


@pytest.mark.asyncio
async def test_long_history_merges_chunks_resumes_cache_and_force_refreshes(tmp_path: Path) -> None:
    live = _LongRangeMxClient()
    live.fail_later = True
    with pytest.raises(MxDailyHistoryError, match="later chunk temporarily unavailable"):
        await MxDailyHistoryClient(live, tmp_path).load(live.symbol, _LONG_START, _LONG_END)
    parent_path = tmp_path / live.symbol / f"{_LONG_START}_{_LONG_END}.json"
    assert not parent_path.exists()
    assert len(tuple(tmp_path.rglob("*.json"))) == 1

    live.fail_later = False
    client = MxDailyHistoryClient(live, tmp_path)
    history = await client.load(live.symbol, _LONG_START, _LONG_END)
    assert (history.start, history.end) == (_LONG_START, _LONG_END)
    assert (history.returned_start, history.returned_end) == (_LONG_START, _LONG_END)
    assert len({row.session_date for row in history.rows}) == len(history.rows)
    assert all((end - start).days <= 730 for _, start, end in live.ranges)
    assert sum(
        purpose == "sessions" and start == _LONG_START for purpose, start, _ in live.ranges
    ) == 1
    assert live.calls.count("identity") == live.calls.count("listing") == 2
    assert len(history.query_evidence) == 10
    assert parent_path.exists()

    refreshed = await client.load(live.symbol, _LONG_START, _LONG_END, force_refresh=True)
    assert refreshed.cache_status == "forced"
    assert refreshed.rows == history.rows
    assert sum(
        purpose == "sessions" and start == _LONG_START for purpose, start, _ in live.ranges
    ) == 2
    assert live.calls.count("identity") == live.calls.count("listing") == 3


@pytest.mark.asyncio
async def test_long_history_rejects_conflicting_overlap_without_parent_cache(
    tmp_path: Path,
) -> None:
    live = _LongRangeMxClient()
    live.conflict_overlap = True
    with pytest.raises(MxDailyHistoryError, match="disagree on an overlap session"):
        await MxDailyHistoryClient(live, tmp_path).load(live.symbol, _LONG_START, _LONG_END)
    assert not (tmp_path / live.symbol / f"{_LONG_START}_{_LONG_END}.json").exists()
    assert len(tuple(tmp_path.rglob("*.json"))) == 1
    assert live.calls.count("identity") == live.calls.count("listing") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_error", [MxSaasProviderNoDataError, MxSaasProviderAuthError, TimeoutError],
)
async def test_history_maps_only_no_data_to_requested_missing_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_error: type[Exception],
) -> None:
    live = _FakeMxClient("688981.SH")
    original = live.query_finance

    async def query_finance(
        *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        if indicators == "涨停价、跌停价":
            raise provider_error("provider diagnostic is not a display field")
        return await original(query=query, indicators=indicators)

    monkeypatch.setattr(live, "query_finance", query_finance)
    expected = (
        MxDailyHistoryFieldsMissingError
        if provider_error is MxSaasProviderNoDataError else provider_error
    )
    with pytest.raises(expected) as captured:
        await MxDailyHistoryClient(live, tmp_path).load(live.symbol, _START, _END)
    if isinstance(captured.value, MxDailyHistoryFieldsMissingError):
        assert captured.value.fields == ("涨停价", "跌停价")
        assert (captured.value.start, captured.value.end) == (_START, _END)
        assert "provider diagnostic" not in str(captured.value)
    assert not tuple(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_history_missing_column_retains_exact_fetch_range_not_claimed_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _FakeMxClient("688981.SH")
    original = live.query_finance

    async def query_finance(
        *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        response = await original(query=query, indicators=indicators)
        if indicators == "涨停价、跌停价":
            return replace(response, tables=(
                _table(live.symbol, [_END.isoformat()], {"涨停价": ["60"]}),
            ))
        return response

    monkeypatch.setattr(live, "query_finance", query_finance)
    with pytest.raises(MxDailyHistoryFieldsMissingError) as captured:
        await MxDailyHistoryClient(live, tmp_path).load(live.symbol, _START, _END)
    assert captured.value.fields == ("跌停价",)
    assert (captured.value.start, captured.value.end) == (_START, _END)
    assert not tuple(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(("start", "end"), [(_START, None), (None, _END), (_END, _START)])
def test_history_missing_field_context_requires_paired_ordered_dates(
    start: date | None, end: date | None,
) -> None:
    with pytest.raises(ValueError, match="ordered date pair"):
        MxDailyHistoryFieldsMissingError(("涨停价",), start=start, end=end)


@pytest.mark.asyncio
async def test_later_missing_fields_report_only_failed_chunk_and_keep_completed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _LongRangeMxClient()
    original = live.query_finance
    failed_bounds: list[date] = []

    async def query_finance(
        *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        bounds = re.findall(r"\d{4}-\d{2}-\d{2}", query)
        if indicators == "涨停价、跌停价" and date.fromisoformat(bounds[0]) > _LONG_START:
            failed_bounds.extend(date.fromisoformat(value) for value in bounds[:2])
            raise MxSaasProviderNoDataError("private provider diagnostic")
        return await original(query=query, indicators=indicators)

    monkeypatch.setattr(live, "query_finance", query_finance)
    with pytest.raises(MxDailyHistoryFieldsMissingError) as captured:
        await MxDailyHistoryClient(live, tmp_path).load(live.symbol, _LONG_START, _LONG_END)
    assert [captured.value.start, captured.value.end] == failed_bounds
    assert failed_bounds[0] > _LONG_START and failed_bounds[1] == _LONG_END
    assert len(tuple(tmp_path.rglob("*.json"))) == 1
    assert "private provider diagnostic" not in str(captured.value)
