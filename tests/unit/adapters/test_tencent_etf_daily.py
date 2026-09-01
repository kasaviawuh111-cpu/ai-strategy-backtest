from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from ashare_lab.adapters.market_data.choice_snapshot import (
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotError,
    DailySnapshotSource,
    DailySnapshotSpec,
    build_daily_research_snapshot,
)
from ashare_lab.adapters.market_data.tencent_etf_daily import (
    AKSHARE_SOURCE_COMMIT,
    AKSHARE_SOURCE_LICENSE,
    BaoStockEtfEvidence,
    BaoStockEtfRow,
    EtfDailySourceError,
    SseDistributionNoticeEvidence,
    SseDistributionTerms,
    TencentSohuEtfDailySource,
    parse_sse_distribution_notice_text,
)
from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.market_data import Board
from ashare_lab.domain.shared import InstrumentId


def _instrument(
    symbol: str = "510300.SH",
    *,
    name: str = "华泰柏瑞沪深300ETF",
    exchange: Exchange = Exchange.SH,
) -> InstrumentRef:
    return InstrumentRef(
        symbol=symbol,
        name=name,
        exchange=exchange,
        asset_type=AssetType.ETF,
        currency="CNY",
        listing_date=date(2012, 5, 28),
        delisting_date=None,
        tradable=True,
        data_source="security-master:test",
    )


def _tencent_payload(
    *,
    key: str,
    code: str = "sh510300",
    with_cash_adjustment: bool = True,
) -> bytes:
    rows = [
        [
            "2024-01-16",
            "3.340",
            "3.367",
            "3.368",
            "3.326",
            "17689524.00",
            {},
            "4.94",
            "592509.98",
        ],
        [
            "2024-01-17",
            "3.357",
            "3.292",
            "3.357",
            "3.290",
            "12862152.14",
            {},
            "3.57",
            "427863.31",
        ],
        [
            "2024-01-18",
            "3.218",
            "3.268",
            "3.275",
            "3.166",
            "47543511.64",
            {},
            "13.15",
            "1525702.17",
        ],
    ]
    if key == "qfqday" and with_cash_adjustment:
        rows = [
            [
                "2024-01-16",
                "3.271",
                "3.298",
                "3.299",
                "3.257",
                "17689524.00",
                {},
                "4.94",
                "592509.98",
            ],
            [
                "2024-01-17",
                "3.288",
                "3.223",
                "3.288",
                "3.221",
                "12862152.14",
                {},
                "3.57",
                "427863.31",
            ],
            [
                "2024-01-18",
                "3.218",
                "3.268",
                "3.275",
                "3.166",
                "47543511.64",
                {},
                "13.15",
                "1525702.17",
            ],
        ]
    if key == "hfqday" and with_cash_adjustment:
        rows = [
            [
                "2024-01-16",
                "3.340",
                "3.367",
                "3.368",
                "3.326",
                "17689524.00",
                {},
                "4.94",
                "592509.98",
            ],
            [
                "2024-01-17",
                "3.357",
                "3.292",
                "3.357",
                "3.290",
                "12862152.14",
                {},
                "3.57",
                "427863.31",
            ],
            [
                "2024-01-18",
                "3.287",
                "3.337",
                "3.344",
                "3.235",
                "47543511.64",
                {},
                "13.15",
                "1525702.17",
            ],
        ]
    payload = {
        "code": 0,
        "msg": "",
        "data": {
            code: {
                key: rows,
                "qt": {code: ["1", "ETF", code[2:]]},
            }
        },
    }
    return ("kline_fixture=" + json.dumps(payload, ensure_ascii=False)).encode()


def _sohu_payload(*, code: str = "cn_510300") -> bytes:
    payload = [
        {
            "status": 0,
            "hq": [
                [
                    "2024-01-17",
                    "3.357",
                    "3.292",
                    "-0.075",
                    "-2.23%",
                    "3.290",
                    "3.357",
                    "12862152.14",
                    "427863.312",
                ],
                [
                    "2024-01-18",
                    "3.218",
                    "3.268",
                    "-0.024",
                    "-0.73%",
                    "3.166",
                    "3.275",
                    "47543511.64",
                    "1525702.168",
                ],
            ],
            "code": code,
        }
    ]
    return ("historySearchHandler(" + json.dumps(payload) + ")").encode()


def _bao_evidence(*, code: str = "sh.510300") -> BaoStockEtfEvidence:
    rows = (
        BaoStockEtfRow(
            session_date=date(2024, 1, 17),
            open=Decimal("3.357"),
            high=Decimal("3.357"),
            low=Decimal("3.290"),
            close=Decimal("3.292"),
            previous_close=Decimal("3.367"),
            volume_shares=1_286_215_214,
            amount_cny=Decimal("4278633100"),
            trading_status="1",
        ),
        BaoStockEtfRow(
            session_date=date(2024, 1, 18),
            open=Decimal("3.218"),
            high=Decimal("3.275"),
            low=Decimal("3.166"),
            close=Decimal("3.268"),
            previous_close=Decimal("3.292"),
            volume_shares=4_754_351_164,
            amount_cny=Decimal("15257021700"),
            trading_status="1",
        ),
    )
    payload = [item.as_dict() for item in rows]
    return BaoStockEtfEvidence(
        provider="BaoStock Python API",
        request_params={
            "code": code,
            "start_date": "2024-01-17",
            "end_date": "2024-01-18",
            "frequency": "d",
            "adjustflag": "3",
        },
        queried_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        canonical_response_sha256=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        rows=rows,
    )


def _distribution_notice() -> SseDistributionNoticeEvidence:
    raw_pdf = b"%PDF-fixture-510300"
    return SseDistributionNoticeEvidence(
        instrument_id=InstrumentId("510300.SH"),
        fund_name="华泰柏瑞沪深300ETF",
        source_url=(
            "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-01-11/"
            "510300_20240111_QQFV.pdf"
        ),
        requested_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        raw_pdf_sha256=hashlib.sha256(raw_pdf).hexdigest(),
        raw_pdf_base64=b64encode(raw_pdf).decode("ascii"),
        extracted_text_sha256="a" * 64,
        terms=SseDistributionTerms(
            announced_date=date(2024, 1, 11),
            record_date=date(2024, 1, 17),
            ex_date=date(2024, 1, 18),
            pay_date=date(2024, 1, 23),
            gross_cash_per_unit=Decimal("0.069"),
        ),
    )


def _transport(request: httpx.Request) -> httpx.Response:
    if "newfqkline" in request.url.path:
        raw_param = request.url.params["param"]
        key = (
            "qfqday"
            if raw_param.endswith(",qfq")
            else "hfqday"
            if raw_param.endswith(",hfq")
            else "day"
        )
        return httpx.Response(200, content=_tencent_payload(key=key), request=request)
    if "hisHq" in request.url.path:
        return httpx.Response(200, content=_sohu_payload(), request=request)
    raise AssertionError(request.url)


def test_glued_etf_source_keeps_real_identity_units_and_audits() -> None:
    source = TencentSohuEtfDailySource(
        transport=httpx.MockTransport(_transport),
        clock=lambda: datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    result = source.fetch(
        instrument=_instrument(),
        start=date(2024, 1, 17),
        end=date(2024, 1, 18),
        baostock_overlap=_bao_evidence(),
        distribution_notices=(_distribution_notice(),),
        prefix_end=date(2024, 1, 17),
    )

    assert result.board is Board.STOCK_ETF
    assert result.execution_rows[0]["volume"] == 1_286_215_214
    assert result.execution_rows[0]["amount"] == Decimal("4278633120.000")
    assert result.execution_rows[0]["preclose"] == Decimal("3.367")
    assert result.signal_rows[0]["close"] == Decimal("3.292")
    assert result.market_calendar == (date(2024, 1, 17), date(2024, 1, 18))
    assert result.session_reference_coverage["schemaVersion"] == (
        "ashare-lab.stock-etf-session-reference.v1"
    )
    assert {item["purpose"] for item in result.request_audit["requests"]} == {
        "tencent_unadjusted",
        "tencent_hfq_signal",
        "tencent_qfq_action_reconciliation",
        "tencent_hfq_prefix",
        "sohu_unadjusted_cross_check",
        "baostock_overlap_cross_check",
    }
    assert all(item["canonicalSha256"] for item in result.request_audit["requests"])
    assert AKSHARE_SOURCE_COMMIT == "8e95744b79ae22326308ccd2b4e62650c5b53c55"
    assert AKSHARE_SOURCE_LICENSE == "MIT"
    assert result.corporate_actions[0].gross_cash_per_share == Decimal("0.069")
    assert result.corporate_action_coverage["officialNoticeCount"] == 1


def test_etf_source_rejects_identity_or_repeated_qfq_drift() -> None:
    def wrong_identity(request: httpx.Request) -> httpx.Response:
        if "newfqkline" in request.url.path:
            return httpx.Response(
                200,
                content=_tencent_payload(key="day", code="sh510050"),
                request=request,
            )
        return httpx.Response(200, content=_sohu_payload(), request=request)

    source = TencentSohuEtfDailySource(transport=httpx.MockTransport(wrong_identity))
    with pytest.raises(EtfDailySourceError, match="identity"):
        source.fetch(
            instrument=_instrument(),
            start=date(2024, 1, 17),
            end=date(2024, 1, 18),
            baostock_overlap=_bao_evidence(),
            distribution_notices=(_distribution_notice(),),
            prefix_end=date(2024, 1, 17),
        )

    with pytest.raises(EtfDailySourceError, match="BaoStock overlap belongs"):
        TencentSohuEtfDailySource(transport=httpx.MockTransport(_transport)).fetch(
            instrument=_instrument(symbol="510050.SH", name="上证50ETF"),
            start=date(2024, 1, 17),
            end=date(2024, 1, 18),
            baostock_overlap=_bao_evidence(),
            distribution_notices=(_distribution_notice(),),
            prefix_end=date(2024, 1, 17),
        )


def test_etf_source_is_symbol_driven_for_a_trusted_shenzhen_etf() -> None:
    def shenzhen_transport(request: httpx.Request) -> httpx.Response:
        if "newfqkline" in request.url.path:
            raw_param = request.url.params["param"]
            key = (
                "qfqday"
                if raw_param.endswith(",qfq")
                else "hfqday"
                if raw_param.endswith(",hfq")
                else "day"
            )
            return httpx.Response(
                200,
                content=_tencent_payload(
                    key=key,
                    code="sz159919",
                    with_cash_adjustment=False,
                ),
                request=request,
            )
        if "hisHq" in request.url.path:
            return httpx.Response(
                200,
                content=_sohu_payload(code="cn_159919"),
                request=request,
            )
        raise AssertionError(request.url)

    result = TencentSohuEtfDailySource(
        transport=httpx.MockTransport(shenzhen_transport),
        clock=lambda: datetime(2026, 8, 31, 12, tzinfo=UTC),
    ).fetch(
        instrument=_instrument(
            symbol="159919.SZ",
            name="嘉实沪深300ETF",
            exchange=Exchange.SZ,
        ),
        start=date(2024, 1, 17),
        end=date(2024, 1, 18),
        baostock_overlap=_bao_evidence(code="sz.159919"),
        distribution_notices=(),
        prefix_end=date(2024, 1, 17),
    )

    assert result.instrument_id == InstrumentId("159919.SZ")
    assert result.corporate_actions == ()
    assert result.corporate_action_coverage["zeroResult"] is True
    assert result.corporate_action_coverage["officialNoticeCount"] == 0


def test_etf_source_rejects_a_security_master_stock_record() -> None:
    stock = _instrument().model_copy(update={"asset_type": AssetType.STOCK})

    with pytest.raises(EtfDailySourceError, match="asset_type=ETF"):
        TencentSohuEtfDailySource(transport=httpx.MockTransport(_transport)).fetch(
            instrument=stock,
            start=date(2024, 1, 17),
            end=date(2024, 1, 18),
            baostock_overlap=_bao_evidence(),
            distribution_notices=(_distribution_notice(),),
            prefix_end=date(2024, 1, 17),
        )


def test_sse_distribution_text_parser_is_fail_closed() -> None:
    text = """
    华泰柏瑞沪深 300 交易型开放式指数证券投资基金分红公告
    基金主代码 510300
    公告送出日期：2024 年 1 月 11 日
    本次分红方案（单位：元/10 份基金份额） 0.6900
    权益登记日 2024 年 1 月 17 日
    除息日 2024 年 1 月 18 日
    现金红利发放日 2024 年 1 月 23 日
    """

    parsed = parse_sse_distribution_notice_text(text)

    assert parsed.announced_date == date(2024, 1, 11)
    assert parsed.record_date == date(2024, 1, 17)
    assert parsed.ex_date == date(2024, 1, 18)
    assert parsed.pay_date == date(2024, 1, 23)
    assert parsed.gross_cash_per_unit == Decimal("0.06900")

    with pytest.raises(EtfDailySourceError, match="cash distribution terms"):
        parse_sse_distribution_notice_text(text.replace("0.6900", ""))


def test_etf_bundle_publishes_without_baostock_full_history_masquerade(
    tmp_path: Path,
) -> None:
    bundle = TencentSohuEtfDailySource(
        transport=httpx.MockTransport(_transport),
        clock=lambda: datetime(2026, 8, 31, 12, tzinfo=UTC),
    ).fetch(
        instrument=_instrument(),
        start=date(2024, 1, 17),
        end=date(2024, 1, 18),
        baostock_overlap=_bao_evidence(),
        distribution_notices=(_distribution_notice(),),
        prefix_end=date(2024, 1, 17),
    )
    source = DailySnapshotSource(
        schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
        snapshot_prefix="technical",
        provider="Tencent/Sohu public with BaoStock overlap and SSE official actions",
        dataset="510300.SH stock ETF daily",
        account_scope="public_research",
        adjustment_field="tencentAdjustment",
        execution_adjustment=0,
        signal_adjustment=2,
        acquisition_implementation=f"AKShare-compatible adapter@{AKSHARE_SOURCE_COMMIT}",
        limit_event_flags_available=False,
        status_cross_check="Tencent/Sohu exact date axis plus BaoStock overlap",
        previous_close_cross_check="Tencent prior unadjusted close",
        limitations=("public endpoint terms require production legal review",),
    )

    result = build_daily_research_snapshot(
        spec=DailySnapshotSpec(
            symbol="510300.SH",
            start=date(2024, 1, 17),
            end=date(2024, 1, 18),
            listing_date=date(2012, 5, 28),
            board=Board.STOCK_ETF,
        ),
        execution_rows=bundle.execution_rows,
        signal_rows=bundle.signal_rows,
        market_calendar=bundle.market_calendar,
        raw_audit_payload=bundle.raw_audit_payload,
        request_audit=bundle.request_audit,
        prefix_stability=bundle.prefix_stability,
        output_root=tmp_path,
        captured_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
        sdk_archive_sha256=None,
        session_reference_rows=bundle.session_reference_rows,
        session_reference_coverage=bundle.session_reference_coverage,
        corporate_actions=bundle.corporate_actions,
        corporate_action_coverage=bundle.corporate_action_coverage,
        source=source,
    )

    assert result.snapshot_id.startswith("technical:")
    assert result.manifest["sessionReference"]["board"] == "stock_etf"
    assert result.manifest["corporateActionCoverage"]["coverageScope"] == (
        "stock_etf_cash_and_unit_change_reconciliation"
    )

    tampered = dict(bundle.session_reference_coverage)
    tampered["dateAxisSha256"] = "0" * 64
    with pytest.raises(ChoiceSnapshotError, match="date-axis hash"):
        build_daily_research_snapshot(
            spec=DailySnapshotSpec(
                symbol="510300.SH",
                start=date(2024, 1, 17),
                end=date(2024, 1, 18),
                listing_date=date(2012, 5, 28),
                board=Board.STOCK_ETF,
            ),
            execution_rows=bundle.execution_rows,
            signal_rows=bundle.signal_rows,
            market_calendar=bundle.market_calendar,
            raw_audit_payload=bundle.raw_audit_payload,
            request_audit=bundle.request_audit,
            prefix_stability=bundle.prefix_stability,
            output_root=tmp_path,
            captured_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
            sdk_archive_sha256=None,
            session_reference_rows=bundle.session_reference_rows,
            session_reference_coverage=tampered,
            corporate_actions=bundle.corporate_actions,
            corporate_action_coverage=bundle.corporate_action_coverage,
            source=source,
        )
