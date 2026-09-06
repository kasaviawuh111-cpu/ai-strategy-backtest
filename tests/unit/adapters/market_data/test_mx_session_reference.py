from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest

from ashare_lab.adapters.market_data.mx_session_reference import (
    REFERENCE_SCHEMA_VERSION,
    MxSessionReferenceAdapter,
    MxSessionReferenceError,
)
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
)


def _provenance(digit: str, schema: str) -> LiveMarketDataProvenance:
    return LiveMarketDataProvenance(
        response_sha256="sha256:" + digit * 64,
        retrieved_at=datetime(2025, 1, 4, tzinfo=UTC),
        schema_version=schema,
    )


class _Client:
    def __init__(self, *, status: tuple[str, ...] = ("正常交易", "复牌")) -> None:
        self.status = status

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        return LiveMarketDataResult(
            provider="eastmoney_mx_screener",
            query=query,
            asset_type=asset_type,
            columns=("代码", "名称", "市场类型", "股票简称", "上市状态", "证券类型"),
            rows=(
                {
                    "代码": "688981",
                    "名称": "中芯国际",
                    "市场类型": "上海证券交易所",
                    "股票简称": "中芯国际",
                    "上市状态": "正常上市",
                    "证券类型": "A股",
                },
            ),
            provenance=_provenance("a", "eastmoney-mx.select-security.v1"),
        )

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        del indicators
        if "首发上市日" in query:
            tables: tuple[dict[str, Any], ...] = (
                {
                    "code": "688981.SH",
                    "entityCodes": ["688981.SH"],
                    "rawTable": {
                        "listing": ["2020-07-16"],
                        "name": ["中芯国际"],
                        "listed": ["是"],
                        "headName": [" "],
                    },
                    "nameMap": {
                        "listing": "首发上市日",
                        "name": "股票简称",
                        "listed": "是否上市",
                    },
                },
            )
            digit = "b"
        else:
            dates = ["2025-01-03", "2025-01-02"]
            tables = (
                {
                    "code": "688981.SH",
                    "entityCodes": ["688981.SH"],
                    "rawTable": {
                        "status": list(self.status),
                        "st": ["否", "否"],
                        "headName": dates,
                    },
                    "nameMap": {"status": "交易状态", "st": "是否为ST股票"},
                },
                {
                    "code": "688981.SH",
                    "entityCodes": ["688981.SH"],
                    "rawTable": {"preclose": ["10.5", "9.8"], "headName": dates},
                    "nameMap": {"preclose": "前收盘价"},
                },
            )
            digit = "c"
        return LiveFinanceDataResult(
            provider="eastmoney_mx_finance_data",
            query=query,
            indicators=None,
            tables=tables,
            provenance=_provenance(digit, "eastmoney-mx.search-data.v1"),
        )


def test_builds_provider_neutral_reference_from_exact_split_mx_tables() -> None:
    payload = asyncio.run(
        MxSessionReferenceAdapter(_Client()).prepare(  # type: ignore[arg-type]
            symbol="688981.SH",
            start=date(2025, 1, 2),
            end=date(2025, 1, 3),
            captured_at=datetime(2025, 1, 4, tzinfo=UTC),
        )
    )

    assert payload["schemaVersion"] == REFERENCE_SCHEMA_VERSION
    assert payload["instrument"] == {
        "instrument_id": "688981.SH",
        "provider_code": "688981.SH",
        "name": "中芯国际",
        "listing_date": "2020-07-16",
        "delisting_date": None,
        "status": "listed",
        "board": "star",
        "boardSource": "canonical A-share exchange code-space rule v1",
        "asset_type": "STOCK",
        "currency": "CNY",
    }
    sessions = payload["historicalSessions"]
    assert isinstance(sessions, dict)
    assert sessions["rows"] == [
        {"date": "2025-01-02", "preclose": "9.8", "tradestatus": "1", "isST": "0"},
        {"date": "2025-01-03", "preclose": "10.5", "tradestatus": "1", "isST": "0"},
    ]
    coverage = sessions["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["rowCount"] == 2
    assert coverage["queryAudits"][0]["responseSha256"] == "sha256:" + "c" * 64


def test_unknown_provider_trading_status_fails_closed() -> None:
    with pytest.raises(MxSessionReferenceError, match="unsupported MX trading status"):
        asyncio.run(
            MxSessionReferenceAdapter(_Client(status=("未知状态", "正常交易"))).prepare(  # type: ignore[arg-type]
                symbol="688981.SH",
                start=date(2025, 1, 2),
                end=date(2025, 1, 3),
                captured_at=datetime(2025, 1, 4, tzinfo=UTC),
            )
        )
