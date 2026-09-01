from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.financial_sources.eastmoney_operator import (
    EastmoneyOperatorReadingError,
    EastmoneyOperatorReadingSource,
    OperatorDataset,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
RETRIEVED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=SHANGHAI)


def _body(rows: list[dict[str, object]], *, pages: int = 1, count: int | None = None) -> bytes:
    return json.dumps(
        {
            "success": True,
            "code": 0,
            "message": "ok",
            "result": {
                "pages": pages,
                "count": len(rows) if count is None else count,
                "data": rows,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_latest_indicator_rows_preserve_direct_values_nulls_and_wire_hash() -> None:
    raw = _body(
        [
            {
                "SECUCODE": "300059.SZ",
                "REPORT_DATE": "2026-06-30 00:00:00",
                "TOTAL_OPERATE_INCOME": 10_505_339_283.68,
                "TOI_YOY_RATIO": 53.22,
                "ROE": 8.46,
                "NET_CAPITAL": None,
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == OperatorDataset.LATEST_INDICATORS.value
        assert request.url.params["filter"] == '(SECUCODE="300059.SZ")'
        return httpx.Response(200, content=raw, request=request)

    with EastmoneyOperatorReadingSource(transport=httpx.MockTransport(handler)) as source:
        batch = source.fetch_latest_indicators("300059.SZ", retrieved_at=RETRIEVED_AT)

    assert batch.dataset is OperatorDataset.LATEST_INDICATORS
    assert batch.rows[0]["TOI_YOY_RATIO"] == 53.22
    assert batch.rows[0]["NET_CAPITAL"] is None
    assert batch.requests[0].raw_response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_main_financial_rows_keep_direct_report_and_publication_fields() -> None:
    """The PIT binder needs raw report/notice/update fields, not derived values."""

    raw = _body(
        [
            {
                "SECUCODE": "300059.SZ",
                "REPORT_DATE": "2025-06-30 00:00:00",
                "REPORT_TYPE": "中报",
                "NOTICE_DATE": "2025-08-15 00:00:00",
                "UPDATE_DATE": "2025-08-15 00:00:00",
                "TOTALOPERATEREVE": 7_000_000_000.0,
                "PARENTNETPROFIT": 5_000_000_000.0,
                "ROEJQ": 8.46,
                "XSMLL": None,
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == OperatorDataset.MAIN_FINANCIAL_DATA.value
        assert request.url.params["source"] == "HSF10"
        assert request.url.params["sty"] == "ALL"
        assert request.url.params["filter"] == '(SECUCODE="300059.SZ")'
        return httpx.Response(200, content=raw, request=request)

    with EastmoneyOperatorReadingSource(transport=httpx.MockTransport(handler)) as source:
        batch = source.fetch_main_financial_data("300059.SZ", retrieved_at=RETRIEVED_AT)

    assert batch.dataset is OperatorDataset.MAIN_FINANCIAL_DATA
    assert batch.rows[0]["REPORT_DATE"] == "2025-06-30 00:00:00"
    assert batch.rows[0]["NOTICE_DATE"] == "2025-08-15 00:00:00"
    assert batch.rows[0]["UPDATE_DATE"] == "2025-08-15 00:00:00"
    assert batch.rows[0]["XSMLL"] is None


@pytest.mark.parametrize("symbol", ["300059.SZ", "000001.SZ", "600519.SH", "688981.SH"])
def test_canonical_mainland_stock_symbols_are_accepted(symbol: str) -> None:
    raw = _body([{"SECUCODE": symbol, "REPORT_DATE": "2026-06-30 00:00:00"}])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw, request=request)
    )
    with EastmoneyOperatorReadingSource(transport=transport) as source:
        result = source.fetch_latest_indicators(symbol, retrieved_at=RETRIEVED_AT)
    assert result.instrument_id == symbol


def test_wrong_security_identity_fails_closed() -> None:
    raw = _body([{"SECUCODE": "600519.SH", "REPORT_DATE": "2026-06-30 00:00:00"}])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw, request=request)
    )

    with (
        EastmoneyOperatorReadingSource(transport=transport) as source,
        pytest.raises(EastmoneyOperatorReadingError, match="identity"),
    ):
        source.fetch_latest_indicators("300059.SZ", retrieved_at=RETRIEVED_AT)


def test_valuation_trend_fetches_all_pages_and_keeps_provider_indicator_type() -> None:
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        indicator_type = int(request.url.params["filter"].split("INDICATORTYPE=")[1][0])
        page = int(request.url.params["p"])
        calls.append((indicator_type, page))
        row = {
            "SECUCODE": "300059.SZ",
            "SECURITY_CODE": "300059",
            "TRADE_DATE": f"2026-0{page}-28 00:00:00",
            "INDICATORTYPE": str(indicator_type),
            "INDICATOR_VALUE": float(indicator_type * 10 + page),
        }
        return httpx.Response(
            200,
            content=_body([row], pages=2, count=2),
            request=request,
        )

    with EastmoneyOperatorReadingSource(transport=httpx.MockTransport(handler)) as source:
        batches = source.fetch_valuation_trends(
            "300059.SZ",
            retrieved_at=RETRIEVED_AT,
            statistics_cycle=4,
        )

    assert calls == [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2), (4, 1), (4, 2)]
    assert [batch.indicator_type for batch in batches] == [1, 2, 3, 4]
    assert all(len(batch.rows) == 2 for batch in batches)
    assert batches[3].rows[-1]["INDICATOR_VALUE"] == 42.0


@pytest.mark.parametrize("symbol", ["300059", "300059.sh", "000300.SH", "AAPL.US"])
def test_only_canonical_a_share_stock_symbols_are_accepted(symbol: str) -> None:
    with (
        EastmoneyOperatorReadingSource(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
        ) as source,
        pytest.raises(ValueError, match="canonical A-share stock"),
    ):
        source.fetch_latest_indicators(symbol, retrieved_at=RETRIEVED_AT)


def test_provider_failure_is_not_converted_to_empty_data() -> None:
    raw = json.dumps(
        {"success": False, "code": 9201, "message": "返回数据为空"},
        ensure_ascii=False,
    ).encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw, request=request)
    )

    with (
        EastmoneyOperatorReadingSource(transport=transport) as source,
        pytest.raises(EastmoneyOperatorReadingError, match="9201"),
    ):
        source.fetch_latest_indicators("300059.SZ", retrieved_at=RETRIEVED_AT)
