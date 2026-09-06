from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from ashare_lab.adapters.market_data.tencent_stock_daily import (
    AKSHARE_SOURCE_COMMIT,
    TENCENT_KLINE_URL,
    TencentAdjustment,
    TencentStockDailyIntegrityError,
    TencentStockDailyProviderUnavailableError,
    TencentStockDailySource,
)
from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef

NOW = datetime(2026, 9, 2, 8, tzinfo=UTC)


def _instrument(
    symbol: str = "300059.SZ",
    *,
    name: str = "东方财富",
    exchange: Exchange = Exchange.SZ,
    asset_type: AssetType = AssetType.STOCK,
    listing_date: date = date(2010, 3, 19),
) -> InstrumentRef:
    return InstrumentRef(
        symbol=symbol,
        name=name,
        exchange=exchange,
        asset_type=asset_type,
        currency="CNY",
        listing_date=listing_date,
        delisting_date=None,
        tradable=True,
        data_source="security-master:test",
    )


def _payload(
    *,
    symbol: str = "sz300059",
    name: str = "东方财富",
    key: str = "day",
    rows: list[list[object]] | None = None,
    variable: str = "kline_day2024",
) -> bytes:
    body = {
        "code": 0,
        "msg": "",
        "data": {
            symbol: {
                key: rows
                or [
                    [
                        "2024-01-02",
                        "14.00",
                        "14.20",
                        "14.30",
                        "13.90",
                        "12345.67",
                        {},
                        "1.50",
                        "1753.148",
                    ]
                ],
                "qt": {symbol: ["51", name, symbol[2:]]},
            }
        },
    }
    return f"{variable}=".encode() + json.dumps(body, ensure_ascii=False).encode()


def _source(
    body: bytes,
    requests: list[httpx.Request] | None = None,
    *,
    status_code: int = 200,
) -> TencentStockDailySource:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(status_code, content=body, request=request)

    return TencentStockDailySource(
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
        max_attempts=1,
    )


def test_sz_unadjusted_preserves_wire_hash_and_normalizes_provider_units() -> None:
    body = _payload()
    requests: list[httpx.Request] = []
    with _source(body, requests) as source:
        result = source.fetch(
            instrument=_instrument(),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )

    assert len(requests) == 1
    assert str(requests[0].url).startswith(TENCENT_KLINE_URL)
    assert requests[0].url.params["param"] == (
        "sz300059,day,2024-01-01,2024-12-31,640,"
    )
    assert result.tencent_symbol == "sz300059"
    assert result.raw_wire_sha256s == (hashlib.sha256(body).hexdigest(),)
    assert result.adjustment is TencentAdjustment.UNADJUSTED
    row = result.rows[0]
    assert row.open == Decimal("14.00")
    assert row.close == Decimal("14.20")
    assert row.volume_source_unit == "lot"
    assert row.volume_shares == 1_234_567
    assert row.amount_cny == Decimal("17531480.000")
    assert row.turnover_rate_pct == Decimal("1.50")
    assert row.as_snapshot_row(result.instrument_id) == {
        "stock_code": "300059.SZ",
        "date": "2024-01-02",
        "open": Decimal("14.00"),
        "high": Decimal("14.30"),
        "low": Decimal("13.90"),
        "close": Decimal("14.20"),
        "volume": 1_234_567,
        "amount": Decimal("17531480.000"),
        "turnover_rate_pct": Decimal("1.50"),
        "turnover_rate_provider": "tencent_finance_public",
        "turnover_rate_methodology": (
            "tencent.newfqkline.row7.provider_reported_turnover_rate_pct.v1"
        ),
    }
    assert AKSHARE_SOURCE_COMMIT == "8e95744b79ae22326308ccd2b4e62650c5b53c55"


@pytest.mark.parametrize(
    ("instrument", "adjustment", "provider_symbol", "key", "variable", "raw_volume", "shares"),
    [
        (
            _instrument(
                "688981.SH",
                name="中芯国际",
                exchange=Exchange.SH,
                listing_date=date(2020, 7, 16),
            ),
            TencentAdjustment.FRONT_ADJUSTED,
            "sh688981",
            "qfqday",
            "kline_dayqfq2024",
            "13459147.00",
            13_459_147,
        ),
        (
            _instrument(
                "920002.BJ",
                name="万达轴承",
                exchange=Exchange.BJ,
                listing_date=date(2024, 5, 30),
            ),
            TencentAdjustment.BACK_ADJUSTED,
            "bj920002",
            "hfqday",
            "kline_dayhfq2024",
            "46214.00",
            4_621_400,
        ),
    ],
)
def test_front_back_adjustment_and_sh_sz_bj_volume_rules(
    instrument: InstrumentRef,
    adjustment: TencentAdjustment,
    provider_symbol: str,
    key: str,
    variable: str,
    raw_volume: str,
    shares: int,
) -> None:
    trade_date = date(2024, 5, 30) if provider_symbol.startswith("bj") else date(2024, 1, 2)
    row = [
        trade_date.isoformat(),
        "50.00",
        "51.00",
        "52.00",
        "49.00",
        raw_volume,
        {},
        "0.68",
        str(
            Decimal(raw_volume)
            * (1 if provider_symbol.startswith("sh688") else 100)
            * 51
            / 10000
        ),
    ]
    body = _payload(
        symbol=provider_symbol,
        name=instrument.name,
        key=key,
        rows=[row],
        variable=variable,
    )
    with _source(body) as source:
        result = source.fetch(
            instrument=instrument,
            start=trade_date,
            end=trade_date,
            adjustment=adjustment,
        )

    assert result.rows[0].volume_shares == shares
    assert result.rows[0].volume_source_unit == (
        "share" if provider_symbol.startswith("sh688") else "lot"
    )


def test_rejects_etf_wrong_code_suffix_and_provider_identity_drift() -> None:
    etf = _instrument(
        "510300.SH",
        name="华泰柏瑞沪深300ETF",
        exchange=Exchange.SH,
        asset_type=AssetType.ETF,
        listing_date=date(2012, 5, 28),
    )
    with _source(_payload()) as source, pytest.raises(ValueError, match="STOCK"):
        source.fetch(
            instrument=etf,
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )

    wrong_suffix = _instrument("300059.SH", exchange=Exchange.SH)
    with _source(_payload()) as source, pytest.raises(ValueError, match="canonical"):
        source.fetch(
            instrument=wrong_suffix,
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )

    with _source(_payload(name="同花顺")) as source, pytest.raises(
        TencentStockDailyIntegrityError,
        match="name",
    ):
        source.fetch(
            instrument=_instrument(),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )


def test_network_and_integrity_failures_have_distinct_types() -> None:
    with _source(b"upstream down", status_code=503) as source, pytest.raises(
        TencentStockDailyProviderUnavailableError,
        match="HTTP 503",
    ):
        source.fetch(
            instrument=_instrument(),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )

    with _source(b"not-json") as source, pytest.raises(
        TencentStockDailyIntegrityError,
        match="JSONP",
    ):
        source.fetch(
            instrument=_instrument(),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )


def test_rejects_malformed_ohlcv_and_rows_outside_listing_window() -> None:
    impossible = [["2024-01-02", "14", "15", "14.5", "13", "1", {}, "1", "0.001"]]
    with _source(_payload(rows=impossible)) as source, pytest.raises(
        TencentStockDailyIntegrityError,
        match="OHLC",
    ):
        source.fetch(
            instrument=_instrument(),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )

    with _source(_payload()) as source, pytest.raises(ValueError, match="listing"):
        source.fetch(
            instrument=_instrument(listing_date=date(2024, 2, 1)),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            adjustment=TencentAdjustment.UNADJUSTED,
        )
