"""Strict public daily acquisition for server-resolved mainland stock ETFs.

The Tencent request and normalization shape is a thin adaptation of AKShare
``stock_zh_a_hist_tx`` at the immutable commit recorded below.  Tencent is the
price/volume authority, Sohu supplies a separately acquired turnover amount,
and BaoStock is an independent overlapping check.  None of these acquisition
clients are called by a replay worker.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.market_data import (
    Board,
    CorporateAction,
    CorporateActionKind,
    TimeQuality,
)
from ashare_lab.domain.shared import InstrumentId, StrongId

TENCENT_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SOHU_KLINE_URL = "https://q.stock.sohu.com/hisHq"
AKSHARE_SOURCE_REPOSITORY = "https://github.com/akfamily/akshare"
AKSHARE_SOURCE_COMMIT = "8e95744b79ae22326308ccd2b4e62650c5b53c55"
AKSHARE_SOURCE_PATH = "akshare/stock_feature/stock_hist_tx.py:stock_zh_a_hist_tx"
AKSHARE_SOURCE_LICENSE = "MIT"
ETF_SESSION_REFERENCE_SCHEMA_VERSION = "ashare-lab.stock-etf-session-reference.v1"
ETF_CORPORATE_ACTION_COVERAGE_SCOPE = "stock_etf_cash_and_unit_change_reconciliation"
PROVIDER = "Tencent Finance public + Sohu Finance public + BaoStock overlap"
DATASET = "stock_etf_daily_kline"
_LOT_SIZE = 100
_SHARE_VOLUME_TOLERANCE = 99
_PRICE_TOLERANCE = Decimal("0.001")
_AMOUNT_RELATIVE_TOLERANCE = Decimal("0.000001")
_AMOUNT_ABSOLUTE_TOLERANCE = Decimal("1000")
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://gu.qq.com/sh510300/gp",
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; etf-daily-research)",
}
_SOHU_CALLBACK = "historySearchHandler"
_DATE_RE = re.compile(r"(?P<year>20\d{2})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日")
_SHANGHAI = ZoneInfo("Asia/Shanghai")

SSE_DISTRIBUTION_NOTICE_URLS = (
    "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2022-01-12/"
    "510300_20220112_1_pWPNE0aG.pdf",
    "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2023-01-09/510300_20230109_0ED5.pdf",
    "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-01-11/510300_20240111_QQFV.pdf",
    "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-06-11/510300_20250611_ZAU4.pdf",
    "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-01-12/510300_20260112_VTCZ.pdf",
)


class EtfDailySourceError(RuntimeError):
    """The ETF sources cannot be reconciled without inventing data."""


@dataclass(frozen=True, slots=True)
class BaoStockEtfRow:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    previous_close: Decimal
    volume_shares: int
    amount_cny: Decimal
    trading_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "date": self.session_date.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "preclose": str(self.previous_close),
            "volume": self.volume_shares,
            "amount": str(self.amount_cny),
            "tradestatus": self.trading_status,
        }


@dataclass(frozen=True, slots=True)
class BaoStockEtfEvidence:
    provider: str
    request_params: Mapping[str, str]
    queried_at: datetime
    canonical_response_sha256: str
    rows: tuple[BaoStockEtfRow, ...]

    def __post_init__(self) -> None:
        _require_aware(self.queried_at, "BaoStock queried_at")
        if not self.provider.strip():
            raise ValueError("BaoStock provider cannot be blank")
        _require_sha256(self.canonical_response_sha256, "BaoStock canonical response")
        expected = _canonical_sha256([row.as_dict() for row in self.rows])
        if self.canonical_response_sha256 != expected:
            raise ValueError("BaoStock canonical response hash does not match rows")


@dataclass(frozen=True, slots=True)
class _DailyRow:
    session_date: date
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    volume_lots: Decimal
    amount_wan_cny: Decimal

    @property
    def volume_shares(self) -> int:
        shares = self.volume_lots * _LOT_SIZE
        if shares != shares.to_integral_value():
            raise EtfDailySourceError("ETF provider volume cannot be normalized to whole shares")
        return int(shares)

    @property
    def amount_cny(self) -> Decimal:
        return self.amount_wan_cny * Decimal("10000")

    def identity(self) -> dict[str, object]:
        return {
            "date": self.session_date.isoformat(),
            "open": str(self.open),
            "close": str(self.close),
            "high": str(self.high),
            "low": str(self.low),
            "volumeLots": str(self.volume_lots),
            "amountWanCny": str(self.amount_wan_cny),
        }


@dataclass(frozen=True, slots=True)
class _HttpAudit:
    purpose: str
    provider: str
    url: str
    params: Mapping[str, str]
    requested_at: datetime
    received_at: datetime
    raw_wire_sha256: str
    canonical_sha256: str
    row_count: int
    returned_start: date | None
    returned_end: date | None

    def as_dict(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "provider": self.provider,
            "url": self.url,
            "params": dict(sorted(self.params.items())),
            "requestedAt": self.requested_at.astimezone(UTC).isoformat(),
            "receivedAt": self.received_at.astimezone(UTC).isoformat(),
            "rawWireSha256": self.raw_wire_sha256,
            "canonicalSha256": self.canonical_sha256,
            "rowCount": self.row_count,
            "returnedStart": self.returned_start.isoformat() if self.returned_start else None,
            "returnedEnd": self.returned_end.isoformat() if self.returned_end else None,
        }


@dataclass(frozen=True, slots=True)
class EtfDailyBundle:
    instrument_id: InstrumentId
    board: Board
    execution_rows: tuple[Mapping[str, object], ...]
    signal_rows: tuple[Mapping[str, object], ...]
    session_reference_rows: tuple[Mapping[str, object], ...]
    market_calendar: tuple[date, ...]
    raw_audit_payload: Mapping[str, object]
    request_audit: Mapping[str, object]
    session_reference_coverage: Mapping[str, object]
    prefix_stability: Mapping[str, object]
    corporate_actions: tuple[CorporateAction, ...]
    corporate_action_coverage: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SseDistributionTerms:
    announced_date: date
    record_date: date
    ex_date: date
    pay_date: date
    gross_cash_per_unit: Decimal


@dataclass(frozen=True, slots=True)
class SseDistributionNoticeEvidence:
    instrument_id: InstrumentId
    fund_name: str
    source_url: str
    requested_at: datetime
    received_at: datetime
    raw_pdf_sha256: str
    raw_pdf_base64: str
    extracted_text_sha256: str
    terms: SseDistributionTerms

    def __post_init__(self) -> None:
        if not str(self.instrument_id).endswith(".SH"):
            raise ValueError("SSE notice evidence is valid only for Shanghai ETFs")
        _require_aware(self.requested_at, "SSE requested_at")
        _require_aware(self.received_at, "SSE received_at")
        if self.received_at < self.requested_at:
            raise ValueError("SSE received_at cannot precede requested_at")
        parsed = httpx.URL(self.source_url)
        if parsed.scheme != "https" or parsed.host != "www.sse.com.cn":
            raise ValueError("SSE notice must use the official HTTPS host")
        code = str(self.instrument_id).split(".", maxsplit=1)[0]
        if f"{code}_" not in parsed.path or not parsed.path.endswith(".pdf"):
            raise ValueError("SSE notice URL does not identify the ETF")
        if not self.fund_name.strip():
            raise ValueError("SSE notice fund name cannot be blank")
        _require_sha256(self.raw_pdf_sha256, "SSE raw PDF")
        _require_sha256(self.extracted_text_sha256, "SSE extracted text")
        try:
            raw_pdf = b64decode(self.raw_pdf_base64, validate=True)
        except (ValueError, BinasciiError) as exc:
            raise ValueError("SSE raw PDF must be valid base64") from exc
        if hashlib.sha256(raw_pdf).hexdigest() != self.raw_pdf_sha256:
            raise ValueError("SSE raw PDF hash does not match archived bytes")

    def audit(self) -> dict[str, object]:
        return {
            "provider": "Shanghai Stock Exchange official fund announcement",
            "instrumentId": str(self.instrument_id),
            "fundName": self.fund_name,
            "url": self.source_url,
            "requestedAt": self.requested_at.astimezone(UTC).isoformat(),
            "receivedAt": self.received_at.astimezone(UTC).isoformat(),
            "rawPdfSha256": self.raw_pdf_sha256,
            "rawPdfBase64": self.raw_pdf_base64,
            "extractedTextSha256": self.extracted_text_sha256,
            "announcedDate": self.terms.announced_date.isoformat(),
            "recordDate": self.terms.record_date.isoformat(),
            "exDate": self.terms.ex_date.isoformat(),
            "payDate": self.terms.pay_date.isoformat(),
            "grossCashPerUnit": str(self.terms.gross_cash_per_unit),
        }


class SseDistributionNoticeSource:
    """Acquire and archive one official SSE ETF distribution PDF."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        clock: Callable[[], datetime] | None = None,
        text_extractor: Callable[[bytes], str] | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers=_HEADERS,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._text_extractor = text_extractor or _extract_pdf_text

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SseDistributionNoticeSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        source_url: str,
        *,
        instrument: InstrumentRef,
    ) -> SseDistributionNoticeEvidence:
        _require_etf_instrument(instrument, as_of=instrument.listing_date)
        if instrument.exchange is not Exchange.SH:
            raise EtfDailySourceError("SSE distribution source supports Shanghai ETFs only")
        parsed = httpx.URL(source_url)
        if parsed.scheme != "https" or parsed.host != "www.sse.com.cn":
            raise EtfDailySourceError("distribution notice must use official SSE HTTPS")
        requested_at = _clock(self._clock)
        response = self._client.get(source_url)
        received_at = _clock(self._clock)
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EtfDailySourceError("official SSE distribution PDF request failed") from exc
        if not response.content.startswith(b"%PDF"):
            raise EtfDailySourceError("official SSE distribution response is not a PDF")
        text = self._text_extractor(response.content)
        terms = parse_sse_distribution_notice_text(
            text,
            instrument_id=InstrumentId(instrument.symbol),
            fund_name=instrument.name,
        )
        return SseDistributionNoticeEvidence(
            instrument_id=InstrumentId(instrument.symbol),
            fund_name=instrument.name,
            source_url=source_url,
            requested_at=requested_at,
            received_at=received_at,
            raw_pdf_sha256=hashlib.sha256(response.content).hexdigest(),
            raw_pdf_base64=b64encode(response.content).decode("ascii"),
            extracted_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            terms=terms,
        )


class TencentSohuEtfDailySource:
    """Acquire one strict bundle for an ETF resolved by trusted master data."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers=_HEADERS,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TencentSohuEtfDailySource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        *,
        instrument: InstrumentRef,
        start: date,
        end: date,
        baostock_overlap: BaoStockEtfEvidence,
        distribution_notices: Sequence[SseDistributionNoticeEvidence],
        prefix_end: date,
    ) -> EtfDailyBundle:
        _require_etf_instrument(instrument, as_of=end)
        instrument_id = InstrumentId(instrument.symbol)
        code = instrument.symbol.split(".", maxsplit=1)[0]
        tencent_symbol = f"{instrument.exchange.value.lower()}{code}"
        sohu_symbol = f"cn_{code}"
        if type(start) is not date or type(end) is not date or start > end:
            raise EtfDailySourceError("ETF daily range must contain ordered dates")
        if type(prefix_end) is not date or not start <= prefix_end < end:
            raise EtfDailySourceError("ETF prefix_end must be inside and before the full range")
        acquisition_start = start - timedelta(days=31)
        expected_bao_code = f"{instrument.exchange.value.lower()}.{code}"
        if baostock_overlap.request_params.get("code") != expected_bao_code:
            raise EtfDailySourceError("BaoStock overlap belongs to another ETF")
        raw_rows, raw_audits, raw_payloads = self._fetch_tencent(
            tencent_symbol=tencent_symbol,
            start=acquisition_start,
            end=end,
            adjustment="",
            purpose="tencent_unadjusted",
        )
        signal_rows_all, signal_audits, signal_payloads = self._fetch_tencent(
            tencent_symbol=tencent_symbol,
            start=acquisition_start,
            end=end,
            adjustment="hfq",
            purpose="tencent_hfq_signal",
        )
        qfq_rows, qfq_audits, qfq_payloads = self._fetch_tencent(
            tencent_symbol=tencent_symbol,
            start=acquisition_start,
            end=end,
            adjustment="qfq",
            purpose="tencent_qfq_action_reconciliation",
        )
        prefix_rows, prefix_audits, prefix_payloads = self._fetch_tencent(
            tencent_symbol=tencent_symbol,
            start=acquisition_start,
            end=prefix_end,
            adjustment="hfq",
            purpose="tencent_hfq_prefix",
        )
        sohu_rows, sohu_audits, sohu_payloads = self._fetch_sohu(
            sohu_symbol=sohu_symbol,
            start=start,
            end=end,
        )
        raw_by_date = {row.session_date: row for row in raw_rows}
        signal_by_date = {row.session_date: row for row in signal_rows_all}
        qfq_by_date = {row.session_date: row for row in qfq_rows}
        prefix_by_date = {row.session_date: row for row in prefix_rows}
        sohu_by_date = {row.session_date: row for row in sohu_rows}
        expected_prefix_dates = tuple(
            sorted(day for day in signal_by_date if acquisition_start <= day <= prefix_end)
        )
        if tuple(sorted(prefix_by_date)) != expected_prefix_dates or _rows_identity(
            [signal_by_date[day] for day in expected_prefix_dates]
        ) != _rows_identity([prefix_by_date[day] for day in expected_prefix_dates]):
            raise EtfDailySourceError(
                "Tencent hfq history changes when the query end date advances"
            )
        selected_dates = tuple(sorted(day for day in raw_by_date if start <= day <= end))
        if not selected_dates:
            raise EtfDailySourceError("Tencent returned no ETF rows in the requested range")
        if set(selected_dates) != {day for day in signal_by_date if start <= day <= end}:
            raise EtfDailySourceError("Tencent raw and hfq date axes do not align")
        if set(selected_dates) != {day for day in qfq_by_date if start <= day <= end}:
            raise EtfDailySourceError("Tencent raw and qfq date axes do not align")
        if set(selected_dates) != set(sohu_by_date):
            raise EtfDailySourceError("Tencent and Sohu date axes do not align")

        raw_order = tuple(sorted(raw_by_date))
        first_index = raw_order.index(selected_dates[0])
        if first_index == 0:
            raise EtfDailySourceError("Tencent acquisition lacks the previous close for start")
        previous_by_date = {
            raw_order[index]: raw_by_date[raw_order[index - 1]].close
            for index in range(1, len(raw_order))
        }
        self._validate_cross_sources(
            dates=selected_dates,
            raw_by_date=raw_by_date,
            sohu_by_date=sohu_by_date,
            baostock=baostock_overlap,
        )
        actions, action_coverage, adjustment_reconciliation = _reconcile_corporate_actions(
            start=start,
            end=end,
            raw_by_date=raw_by_date,
            signal_by_date=qfq_by_date,
            notices=distribution_notices,
            ingested_at=_clock(self._clock),
            instrument_id=instrument_id,
        )

        execution: list[Mapping[str, object]] = []
        signal: list[Mapping[str, object]] = []
        sessions: list[Mapping[str, object]] = []
        for day in selected_dates:
            raw = raw_by_date[day]
            adjusted = signal_by_date[day]
            sohu = sohu_by_date[day]
            execution.append(
                {
                    "stock_code": str(instrument_id),
                    "date": day.isoformat(),
                    "open": raw.open,
                    "high": raw.high,
                    "low": raw.low,
                    "close": raw.close,
                    "volume": raw.volume_shares,
                    "amount": sohu.amount_cny,
                    "preclose": previous_by_date[day],
                    "tradestatus": "正常交易",
                }
            )
            signal.append(
                {
                    "stock_code": str(instrument_id),
                    "date": day.isoformat(),
                    "open": adjusted.open,
                    "high": adjusted.high,
                    "low": adjusted.low,
                    "close": adjusted.close,
                    "volume": raw.volume_shares,
                    "amount": sohu.amount_cny,
                }
            )
            sessions.append(
                {
                    "date": day.isoformat(),
                    "preclose": str(previous_by_date[day]),
                    "tradestatus": "1",
                    "isST": "0",
                }
            )

        bao_audit = _bao_audit(baostock_overlap)
        audits = (
            *raw_audits,
            *signal_audits,
            *qfq_audits,
            *prefix_audits,
            *sohu_audits,
        )
        request_items = [audit.as_dict() for audit in audits]
        request_items.append(bao_audit)
        dates_hash = _canonical_sha256([day.isoformat() for day in selected_dates])
        coverage: dict[str, object] = {
            "schemaVersion": ETF_SESSION_REFERENCE_SCHEMA_VERSION,
            "status": "complete",
            "querySucceeded": True,
            "provider": PROVIDER,
            "instrumentId": str(instrument_id),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fields": ["date", "preclose", "tradestatus", "isST"],
            "frequency": "d",
            "adjustFlag": "0",
            "priceBasis": "unadjusted",
            "rowCount": len(sessions),
            "zeroResult": False,
            "returnedStart": selected_dates[0].isoformat(),
            "returnedEnd": selected_dates[-1].isoformat(),
            "dateAxisSha256": dates_hash,
            "calendarPolicy": (
                "exact Tencent/Sohu trading-row intersection; BaoStock overlap independently "
                "checks provider identity and values"
            ),
            "sourceAudits": request_items,
            "baostockOverlap": {
                "start": baostock_overlap.rows[0].session_date.isoformat(),
                "end": baostock_overlap.rows[-1].session_date.isoformat(),
                "rowCount": len(baostock_overlap.rows),
                "canonicalSha256": baostock_overlap.canonical_response_sha256,
                "wireBytesCaptured": False,
                "wireCaptureReason": "BaoStock SDK exposes normalized rows, not HTTP bytes",
            },
        }
        return EtfDailyBundle(
            instrument_id=instrument_id,
            board=Board.STOCK_ETF,
            execution_rows=tuple(execution),
            signal_rows=tuple(signal),
            session_reference_rows=tuple(sessions),
            market_calendar=selected_dates,
            raw_audit_payload={
                "tencentUnadjusted": raw_payloads,
                "tencentHfqSignal": signal_payloads,
                "tencentQfqActionReconciliation": qfq_payloads,
                "tencentHfqPrefix": prefix_payloads,
                "sohuCrossCheck": sohu_payloads,
                "baostockOverlap": [row.as_dict() for row in baostock_overlap.rows],
                "officialSseDistributionNotices": [item.audit() for item in distribution_notices],
                "adjustmentReconciliation": adjustment_reconciliation,
            },
            request_audit={
                "sourcePolicy": (
                    "Tencent raw OHLCV primary; Tencent hfq signal prices; Sohu turnover; "
                    "BaoStock overlapping independent check"
                ),
                "volumeNormalization": {
                    "sourceUnit": "lot",
                    "lotSizeShares": _LOT_SIZE,
                    "resolutionShares": _LOT_SIZE,
                    "policy": (
                        "provider lots multiplied by 100; decimal lots are retained when present, "
                        "but cross-source comparison never invents sub-lot precision"
                    ),
                },
                "openSourceReuse": {
                    "repository": AKSHARE_SOURCE_REPOSITORY,
                    "commit": AKSHARE_SOURCE_COMMIT,
                    "path": AKSHARE_SOURCE_PATH,
                    "license": AKSHARE_SOURCE_LICENSE,
                },
                "requests": request_items,
            },
            session_reference_coverage=coverage,
            prefix_stability={
                "status": "passed",
                "comparison": "same Tencent hfq prefix under earlier and later query end dates",
                "prefixEnd": prefix_end.isoformat(),
                "overlapRows": len(expected_prefix_dates),
                "canonicalSha256": _canonical_sha256(
                    _rows_identity([prefix_by_date[day] for day in expected_prefix_dates])
                ),
            },
            corporate_actions=actions,
            corporate_action_coverage=action_coverage,
        )

    def _fetch_tencent(
        self,
        *,
        tencent_symbol: str,
        start: date,
        end: date,
        adjustment: str,
        purpose: str,
    ) -> tuple[tuple[_DailyRow, ...], tuple[_HttpAudit, ...], tuple[object, ...]]:
        if adjustment not in {"", "qfq", "hfq"}:
            raise ValueError("Tencent adjustment must be raw, qfq, or hfq")
        rows: dict[date, _DailyRow] = {}
        audits: list[_HttpAudit] = []
        payloads: list[object] = []
        for year in range(start.year, end.year + 1):
            params = {
                "_var": f"kline_day{adjustment}{year}",
                "param": (f"{tencent_symbol},day,{year}-01-01,{year + 1}-12-31,640,{adjustment}"),
                "r": "0.8205512681390605",
            }
            requested_at = _clock(self._clock)
            response = self._client.get(TENCENT_KLINE_URL, params=params)
            received_at = _clock(self._clock)
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise EtfDailySourceError("Tencent ETF request failed") from exc
            decoded, parsed = _parse_tencent(
                response.content,
                adjustment=adjustment,
                tencent_symbol=tencent_symbol,
            )
            payloads.append(decoded)
            for row in parsed:
                existing = rows.get(row.session_date)
                if existing is not None and existing != row:
                    raise EtfDailySourceError("Tencent overlapping requests disagree")
                rows[row.session_date] = row
            audits.append(
                _http_audit(
                    purpose=purpose,
                    provider="Tencent Finance public endpoint",
                    response=response,
                    params=params,
                    requested_at=requested_at,
                    received_at=received_at,
                    rows=parsed,
                )
            )
        selected = tuple(row for day, row in sorted(rows.items()) if start <= day <= end)
        if not selected:
            raise EtfDailySourceError("Tencent returned no ETF rows")
        return selected, tuple(audits), tuple(payloads)

    def _fetch_sohu(
        self,
        *,
        sohu_symbol: str,
        start: date,
        end: date,
    ) -> tuple[tuple[_DailyRow, ...], tuple[_HttpAudit, ...], tuple[object, ...]]:
        rows: dict[date, _DailyRow] = {}
        audits: list[_HttpAudit] = []
        payloads: list[object] = []
        for year in range(start.year, end.year + 1):
            interval_start = max(start, date(year, 1, 1))
            interval_end = min(end, date(year, 12, 31))
            params = {
                "code": sohu_symbol,
                "start": interval_start.strftime("%Y%m%d"),
                "end": interval_end.strftime("%Y%m%d"),
                "stat": "1",
                "order": "A",
                "period": "d",
                "callback": _SOHU_CALLBACK,
                "rt": "jsonp",
            }
            requested_at = _clock(self._clock)
            response = self._client.get(SOHU_KLINE_URL, params=params)
            received_at = _clock(self._clock)
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise EtfDailySourceError("Sohu ETF request failed") from exc
            decoded, parsed = _parse_sohu(response.content, sohu_symbol=sohu_symbol)
            payloads.append(decoded)
            for row in parsed:
                if not interval_start <= row.session_date <= interval_end:
                    continue
                existing = rows.get(row.session_date)
                if existing is not None and existing != row:
                    raise EtfDailySourceError("Sohu overlapping requests disagree")
                rows[row.session_date] = row
            audits.append(
                _http_audit(
                    purpose="sohu_unadjusted_cross_check",
                    provider="Sohu Finance public endpoint",
                    response=response,
                    params=params,
                    requested_at=requested_at,
                    received_at=received_at,
                    rows=parsed,
                )
            )
        selected = tuple(row for day, row in sorted(rows.items()) if start <= day <= end)
        if not selected:
            raise EtfDailySourceError("Sohu returned no ETF rows")
        return selected, tuple(audits), tuple(payloads)

    @staticmethod
    def _validate_cross_sources(
        *,
        dates: tuple[date, ...],
        raw_by_date: Mapping[date, _DailyRow],
        sohu_by_date: Mapping[date, _DailyRow],
        baostock: BaoStockEtfEvidence,
    ) -> None:
        for day in dates:
            primary = raw_by_date[day]
            cross = sohu_by_date[day]
            _require_ohlc_close(primary, cross, day, "Sohu")
            if abs(primary.volume_shares - cross.volume_shares) > _SHARE_VOLUME_TOLERANCE:
                raise EtfDailySourceError(f"Tencent/Sohu volume mismatch on {day.isoformat()}")
            if (
                not primary.low * Decimal(primary.volume_shares)
                <= cross.amount_cny
                <= (primary.high * Decimal(primary.volume_shares))
            ):
                raise EtfDailySourceError(f"Sohu amount is outside OHLC bounds on {day}")
            tolerance = max(
                _AMOUNT_ABSOLUTE_TOLERANCE,
                primary.amount_cny * _AMOUNT_RELATIVE_TOLERANCE,
            )
            if abs(primary.amount_cny - cross.amount_cny) > tolerance:
                raise EtfDailySourceError(f"Tencent/Sohu amount mismatch on {day.isoformat()}")

        if not baostock.rows:
            raise EtfDailySourceError("BaoStock overlap evidence is empty")
        overlap_dates = {row.session_date for row in baostock.rows}
        if not overlap_dates.issubset(set(dates)):
            raise EtfDailySourceError("BaoStock overlap is outside Tencent/Sohu coverage")
        for row in baostock.rows:
            if row.trading_status != "1":
                raise EtfDailySourceError("BaoStock overlap contains a non-trading ETF row")
            primary = raw_by_date[row.session_date]
            reference = _DailyRow(
                session_date=row.session_date,
                open=row.open,
                close=row.close,
                high=row.high,
                low=row.low,
                volume_lots=Decimal(row.volume_shares) / Decimal(_LOT_SIZE),
                amount_wan_cny=row.amount_cny / Decimal("10000"),
            )
            _require_ohlc_close(primary, reference, row.session_date, "BaoStock")
            if abs(primary.volume_shares - row.volume_shares) > _SHARE_VOLUME_TOLERANCE:
                raise EtfDailySourceError(
                    f"Tencent/BaoStock share volume mismatch on {row.session_date}"
                )
            tolerance = max(
                _AMOUNT_ABSOLUTE_TOLERANCE,
                row.amount_cny * _AMOUNT_RELATIVE_TOLERANCE,
            )
            if abs(primary.amount_cny - row.amount_cny) > tolerance:
                raise EtfDailySourceError(f"Tencent/BaoStock amount mismatch on {row.session_date}")


def _fund_identity_token(name: str) -> str:
    normalized = re.sub(r"\s+", "", name)
    for suffix in (
        "交易型开放式指数证券投资基金",
        "交易型开放式证券投资基金",
        "交易型开放式指数基金",
        "交易型开放式基金",
        "ETF",
    ):
        normalized = normalized.replace(suffix, "")
    if len(normalized) < 4:
        raise EtfDailySourceError("security-master ETF name is too weak for notice identity")
    return normalized


def parse_sse_distribution_notice_text(
    text: str,
    *,
    instrument_id: InstrumentId | None = None,
    fund_name: str | None = None,
) -> SseDistributionTerms:
    """Extract one ETF cash distribution from an official SSE PDF text."""

    instrument_id = instrument_id or InstrumentId("510300.SH")
    fund_name = fund_name or "华泰柏瑞沪深300ETF"
    normalized = re.sub(r"\s+", "", text)
    expected_code = str(instrument_id).split(".", maxsplit=1)[0]
    normalized_name = _fund_identity_token(fund_name)
    if expected_code not in normalized or normalized_name not in normalized:
        raise EtfDailySourceError("SSE notice does not identify the requested ETF")
    amount_match = re.search(
        r"本次分红方案（单位：元/10份基金份额）(?P<amount>\d+(?:\.\d+)?)",
        normalized,
    )
    announced = _label_date(normalized, "公告送出日期：")
    record = _label_date(normalized, "权益登记日")
    ex_date = _label_date(normalized, "除息日")
    pay = _label_date(normalized, "现金红利发放日")
    if amount_match is None or any(item is None for item in (announced, record, ex_date, pay)):
        raise EtfDailySourceError("SSE cash distribution terms are incomplete")
    amount_per_ten = _decimal(amount_match.group("amount"), "SSE distribution amount")
    if amount_per_ten <= 0:
        raise EtfDailySourceError("SSE cash distribution terms are incomplete")
    assert announced is not None and record is not None and ex_date is not None and pay is not None
    if not announced < record < ex_date <= pay:
        raise EtfDailySourceError("SSE cash distribution dates are inconsistent")
    return SseDistributionTerms(
        announced_date=announced,
        record_date=record,
        ex_date=ex_date,
        pay_date=pay,
        gross_cash_per_unit=amount_per_ten / Decimal("10"),
    )


def _reconcile_corporate_actions(
    *,
    start: date,
    end: date,
    raw_by_date: Mapping[date, _DailyRow],
    signal_by_date: Mapping[date, _DailyRow],
    notices: Sequence[SseDistributionNoticeEvidence],
    ingested_at: datetime,
    instrument_id: InstrumentId,
) -> tuple[tuple[CorporateAction, ...], dict[str, object], dict[str, object]]:
    _require_aware(ingested_at, "corporate-action ingested_at")
    selected_notices = tuple(
        sorted(
            (item for item in notices if start <= item.terms.ex_date <= end),
            key=lambda item: item.terms.ex_date,
        )
    )
    if len(selected_notices) != len(notices):
        raise EtfDailySourceError("SSE distribution evidence is outside the snapshot range")
    if any(item.instrument_id != instrument_id for item in selected_notices):
        raise EtfDailySourceError("official SSE distribution evidence belongs to another ETF")
    ex_dates = tuple(item.terms.ex_date for item in selected_notices)
    if len(ex_dates) != len(set(ex_dates)):
        raise EtfDailySourceError("official SSE distribution evidence has duplicate ex-dates")

    dates = tuple(sorted(day for day in raw_by_date if start <= day <= end))
    if tuple(sorted(day for day in signal_by_date if start <= day <= end)) != dates:
        raise EtfDailySourceError("raw and qfq dates do not align for action reconciliation")
    if not dates:
        raise EtfDailySourceError("corporate-action reconciliation has no daily rows")

    offsets: list[dict[str, object]] = []
    for day in dates:
        raw = raw_by_date[day]
        adjusted = signal_by_date[day]
        expected_offset = sum(
            (
                item.terms.gross_cash_per_unit
                for item in selected_notices
                if item.terms.ex_date > day
            ),
            start=Decimal("0"),
        )
        observed_offset = raw.close - adjusted.close
        if abs(observed_offset - expected_offset) > _PRICE_TOLERANCE:
            raise EtfDailySourceError(
                "Tencent raw/qfq adjustment is not fully explained by official cash distributions "
                f"on {day.isoformat()}"
            )
        for field in ("open", "high", "low", "close"):
            field_offset = getattr(raw, field) - getattr(adjusted, field)
            if abs(field_offset - expected_offset) > _PRICE_TOLERANCE:
                raise EtfDailySourceError(
                    f"Tencent raw/qfq OHLC adjustment is not additive-only on {day.isoformat()}"
                )
        offsets.append(
            {
                "date": day.isoformat(),
                "expectedCashOffset": str(expected_offset),
                "observedCloseOffset": str(observed_offset),
            }
        )

    actions = tuple(
        _cash_action(item, ingested_at=ingested_at, instrument_id=instrument_id)
        for item in selected_notices
    )
    notice_audits = [
        {key: value for key, value in item.audit().items() if key != "rawPdfBase64"}
        for item in selected_notices
    ]
    reconciliation: dict[str, object] = {
        "status": "passed",
        "method": (
            "for every session and OHLC field, Tencent unadjusted minus qfq equals the sum "
            "of official future SSE cash distributions; no unexplained scale/unit transition"
        ),
        "rowCount": len(offsets),
        "offsetRowsSha256": _canonical_sha256(offsets),
        "officialExDates": [item.terms.ex_date.isoformat() for item in selected_notices],
        "unmatchedAdjustmentTransitions": 0,
        "unitSplitCandidates": 0,
        "unitConsolidationCandidates": 0,
    }
    counts = {kind.value: 0 for kind in CorporateActionKind}
    counts[CorporateActionKind.CASH_DIVIDEND.value] = len(actions)
    identity: dict[str, object] = {
        "notices": notice_audits,
        "adjustmentReconciliation": reconciliation,
    }
    coverage: dict[str, object] = {
        "status": "complete",
        "querySucceeded": True,
        "provider": "Shanghai Stock Exchange official PDFs + Tencent raw/qfq reconciliation",
        "instrumentId": str(instrument_id),
        "instrumentType": "stock_etf",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rowCount": len(actions),
        "zeroResult": not actions,
        "rawResponseSha256": _canonical_sha256(identity),
        "coverageScope": ETF_CORPORATE_ACTION_COVERAGE_SCOPE,
        "categoryActionCounts": counts,
        "officialNoticeCount": len(notice_audits),
        "officialNotices": notice_audits,
        "adjustmentReconciliation": reconciliation,
        "notApplicableCategories": [
            CorporateActionKind.RIGHTS_ISSUE.value,
            CorporateActionKind.SHARE_DISTRIBUTION.value,
        ],
        "timeQuality": TimeQuality.DATE_ONLY_CONSERVATIVE.value,
        "dateAvailabilityPolicy": "official notice date @ 15:00:00 Asia/Shanghai",
        "hashSemantics": "raw official PDF hashes plus canonical Tencent adjustment proof",
    }
    return actions, coverage, reconciliation


def _cash_action(
    notice: SseDistributionNoticeEvidence,
    *,
    ingested_at: datetime,
    instrument_id: InstrumentId,
) -> CorporateAction:
    terms = notice.terms
    replay_available_at = datetime.combine(
        terms.announced_date,
        datetime.min.time().replace(hour=15),
        tzinfo=_SHANGHAI,
    )
    code = str(instrument_id).split(".", maxsplit=1)[0]
    return CorporateAction(
        action_id=StrongId(f"sse:{code}:cash:{terms.ex_date.isoformat()}"),
        source_action_id=f"sse:{code}:{terms.announced_date.isoformat()}",
        instrument_id=instrument_id,
        action_type=CorporateActionKind.CASH_DIVIDEND,
        record_date=terms.record_date,
        ex_date=terms.ex_date,
        source_released_at=None,
        vendor_first_available_at=None,
        ingested_at=ingested_at,
        replay_available_at=replay_available_at,
        revision_no=0,
        time_quality=TimeQuality.DATE_ONLY_CONSERVATIVE,
        provider="Shanghai Stock Exchange official fund announcement",
        source_url=notice.source_url,
        raw_response_sha256=notice.raw_pdf_sha256,
        validation_status="validated",
        gross_cash_per_share=terms.gross_cash_per_unit,
        cash_pay_date=terms.pay_date,
    )


def _extract_pdf_text(raw_pdf: bytes) -> str:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - exercised by packaging gate
        raise EtfDailySourceError("pypdfium2 is required to verify official SSE PDFs") from exc
    try:
        document = pdfium.PdfDocument(raw_pdf)
        text = "\n".join(page.get_textpage().get_text_range() for page in document)
    except Exception as exc:  # PDFium normalizes provider-specific parser errors.
        raise EtfDailySourceError("official SSE distribution PDF cannot be extracted") from exc
    if not text.strip():
        raise EtfDailySourceError("official SSE distribution PDF contains no extractable text")
    return text


def _parse_tencent(
    raw: bytes,
    *,
    adjustment: str,
    tencent_symbol: str,
) -> tuple[object, tuple[_DailyRow, ...]]:
    marker = raw.find(b"={")
    encoded = raw[marker + 1 :] if marker >= 0 else raw
    try:
        decoded: object = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EtfDailySourceError("Tencent response is not valid JSON/JSONP") from exc
    if not isinstance(decoded, Mapping):
        raise EtfDailySourceError("Tencent response status is not successful")
    decoded_map = cast(Mapping[str, object], decoded)
    if decoded_map.get("code") != 0:
        raise EtfDailySourceError("Tencent response status is not successful")
    data = decoded_map.get("data")
    if not isinstance(data, Mapping):
        raise EtfDailySourceError("Tencent response identity is malformed")
    data_map = cast(Mapping[str, object], data)
    if set(data_map) != {tencent_symbol}:
        raise EtfDailySourceError("Tencent response identity does not match the requested ETF")
    item = data_map.get(tencent_symbol)
    if not isinstance(item, Mapping):
        raise EtfDailySourceError("Tencent response identity is malformed")
    item_map = cast(Mapping[str, object], item)
    qt = item_map.get("qt")
    if not isinstance(qt, Mapping):
        raise EtfDailySourceError("Tencent response identity metadata is missing")
    qt_map = cast(Mapping[str, object], qt)
    identity = qt_map.get(tencent_symbol)
    if not isinstance(identity, Sequence) or isinstance(identity, str | bytes | bytearray):
        raise EtfDailySourceError("Tencent response identity metadata is inconsistent")
    identity_values = cast(Sequence[object], identity)
    expected_code = tencent_symbol[2:]
    if len(identity_values) < 3 or identity_values[2] != expected_code:
        raise EtfDailySourceError("Tencent response identity metadata is inconsistent")
    key = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}.get(adjustment)
    if key is None:
        raise EtfDailySourceError("Tencent response uses an unsupported adjustment")
    raw_rows = item_map.get(key)
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, str | bytes | bytearray):
        raise EtfDailySourceError(f"Tencent response does not contain {key}")
    return dict(decoded_map), _parse_tencent_rows(cast(Sequence[object], raw_rows))


def _parse_tencent_rows(raw_rows: Sequence[object]) -> tuple[_DailyRow, ...]:
    rows: list[_DailyRow] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
            raise EtfDailySourceError(f"Tencent row {index} is malformed")
        values = list(cast(Sequence[object], raw))
        if len(values) < 9:
            raise EtfDailySourceError(f"Tencent row {index} is incomplete")
        rows.append(
            _row(
                raw_date=values[0],
                open_value=values[1],
                close_value=values[2],
                high_value=values[3],
                low_value=values[4],
                volume_value=values[5],
                amount_value=values[8],
                label=f"Tencent row {index}",
            )
        )
    return _deduplicate_rows(rows, "Tencent")


def _parse_sohu(
    raw: bytes,
    *,
    sohu_symbol: str,
) -> tuple[object, tuple[_DailyRow, ...]]:
    text: str | None = None
    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise EtfDailySourceError("Sohu response is neither UTF-8 nor GB18030")
    text = text.strip()
    prefix = f"{_SOHU_CALLBACK}("
    if not text.startswith(prefix) or not text.endswith(")"):
        raise EtfDailySourceError("Sohu response is not the expected JSONP callback")
    try:
        decoded: object = json.loads(text[len(prefix) : -1])
    except json.JSONDecodeError as exc:
        raise EtfDailySourceError("Sohu response is not valid JSONP") from exc
    if not isinstance(decoded, Sequence):
        raise EtfDailySourceError("Sohu response envelope is malformed")
    decoded_values = cast(Sequence[object], decoded)
    if len(decoded_values) != 1:
        raise EtfDailySourceError("Sohu response envelope is malformed")
    item = decoded_values[0]
    if not isinstance(item, Mapping):
        raise EtfDailySourceError("Sohu response identity does not match the requested ETF")
    item_map = cast(Mapping[str, object], item)
    if item_map.get("status") != 0 or item_map.get("code") != sohu_symbol:
        raise EtfDailySourceError("Sohu response identity does not match the requested ETF")
    raw_rows = item_map.get("hq")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, str | bytes | bytearray):
        raise EtfDailySourceError("Sohu response rows are malformed")
    rows: list[_DailyRow] = []
    for index, raw_row in enumerate(cast(Sequence[object], raw_rows)):
        if not isinstance(raw_row, Sequence) or isinstance(raw_row, str | bytes | bytearray):
            raise EtfDailySourceError(f"Sohu row {index} is malformed")
        values = list(cast(Sequence[object], raw_row))
        if len(values) < 9:
            raise EtfDailySourceError(f"Sohu row {index} is incomplete")
        rows.append(
            _row(
                raw_date=values[0],
                open_value=values[1],
                close_value=values[2],
                high_value=values[6],
                low_value=values[5],
                volume_value=values[7],
                amount_value=values[8],
                label=f"Sohu row {index}",
            )
        )
    return list(decoded_values), _deduplicate_rows(rows, "Sohu")


def _row(
    *,
    raw_date: object,
    open_value: object,
    close_value: object,
    high_value: object,
    low_value: object,
    volume_value: object,
    amount_value: object,
    label: str,
) -> _DailyRow:
    if not isinstance(raw_date, str):
        raise EtfDailySourceError(f"{label} date is invalid")
    try:
        session_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise EtfDailySourceError(f"{label} date is invalid") from exc
    prices = tuple(
        _decimal(value, f"{label} price")
        for value in (open_value, close_value, high_value, low_value)
    )
    if any(value <= 0 for value in prices):
        raise EtfDailySourceError(f"{label} prices must be positive")
    open_price, close_price, high, low = prices
    if low > min(prices) or high < max(prices):
        raise EtfDailySourceError(f"{label} OHLC values are inconsistent")
    volume_decimal = _decimal(volume_value, f"{label} volume")
    if (
        volume_decimal < 0
        or (volume_decimal * _LOT_SIZE) != (volume_decimal * _LOT_SIZE).to_integral_value()
    ):
        raise EtfDailySourceError(f"{label} volume must resolve to whole ETF shares")
    amount = _decimal(amount_value, f"{label} amount")
    if amount < 0:
        raise EtfDailySourceError(f"{label} amount must be non-negative")
    return _DailyRow(
        session_date=session_date,
        open=open_price,
        close=close_price,
        high=high,
        low=low,
        volume_lots=volume_decimal,
        amount_wan_cny=amount,
    )


def _deduplicate_rows(rows: Sequence[_DailyRow], label: str) -> tuple[_DailyRow, ...]:
    by_date: dict[date, _DailyRow] = {}
    for row in rows:
        existing = by_date.get(row.session_date)
        if existing is not None and existing != row:
            raise EtfDailySourceError(f"{label} response contains conflicting duplicate dates")
        by_date[row.session_date] = row
    return tuple(by_date[day] for day in sorted(by_date))


def _require_ohlc_close(primary: _DailyRow, other: _DailyRow, day: date, label: str) -> None:
    for field in ("open", "close", "high", "low"):
        if abs(getattr(primary, field) - getattr(other, field)) > _PRICE_TOLERANCE:
            raise EtfDailySourceError(f"Tencent/{label} {field} mismatch on {day.isoformat()}")


def _http_audit(
    *,
    purpose: str,
    provider: str,
    response: httpx.Response,
    params: Mapping[str, str],
    requested_at: datetime,
    received_at: datetime,
    rows: Sequence[_DailyRow],
) -> _HttpAudit:
    return _HttpAudit(
        purpose=purpose,
        provider=provider,
        url=str(response.request.url.copy_with(query=None)),
        params=dict(params),
        requested_at=requested_at,
        received_at=received_at,
        raw_wire_sha256=hashlib.sha256(response.content).hexdigest(),
        canonical_sha256=_canonical_sha256(_rows_identity(rows)),
        row_count=len(rows),
        returned_start=rows[0].session_date if rows else None,
        returned_end=rows[-1].session_date if rows else None,
    )


def _bao_audit(evidence: BaoStockEtfEvidence) -> dict[str, object]:
    return {
        "purpose": "baostock_overlap_cross_check",
        "provider": evidence.provider,
        "url": "BaoStock Python SDK:query_history_k_data_plus",
        "params": dict(sorted(evidence.request_params.items())),
        "requestedAt": evidence.queried_at.astimezone(UTC).isoformat(),
        "receivedAt": evidence.queried_at.astimezone(UTC).isoformat(),
        "rawWireSha256": None,
        "wireBytesCaptured": False,
        "wireCaptureReason": "BaoStock SDK exposes normalized rows, not HTTP bytes",
        "canonicalSha256": evidence.canonical_response_sha256,
        "rowCount": len(evidence.rows),
        "returnedStart": evidence.rows[0].session_date.isoformat() if evidence.rows else None,
        "returnedEnd": evidence.rows[-1].session_date.isoformat() if evidence.rows else None,
    }


def _rows_identity(rows: Sequence[_DailyRow]) -> list[dict[str, object]]:
    return [row.identity() for row in rows]


def _label_date(normalized: str, label: str) -> date | None:
    index = normalized.find(label)
    if index < 0:
        return None
    match = _DATE_RE.search(normalized[index + len(label) : index + len(label) + 40])
    if match is None:
        return None
    return date(int(match["year"]), int(match["month"]), int(match["day"]))


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise EtfDailySourceError(f"{label} must be numeric")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise EtfDailySourceError(f"{label} must be numeric") from exc
    if not result.is_finite():
        raise EtfDailySourceError(f"{label} must be finite")
    return result


def _require_etf_instrument(instrument: InstrumentRef, *, as_of: date) -> None:
    if instrument.asset_type is not AssetType.ETF:
        raise EtfDailySourceError("ETF daily acquisition requires security-master asset_type=ETF")
    if instrument.exchange not in {Exchange.SH, Exchange.SZ}:
        raise EtfDailySourceError("ETF daily acquisition currently supports SH/SZ only")
    try:
        instrument.require_tradable_on(as_of)
    except ValueError as exc:
        raise EtfDailySourceError("ETF is not tradable on the requested date") from exc


def _clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    _require_aware(value, "source clock")
    return value


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include an explicit timezone")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "AKSHARE_SOURCE_COMMIT",
    "AKSHARE_SOURCE_LICENSE",
    "AKSHARE_SOURCE_PATH",
    "AKSHARE_SOURCE_REPOSITORY",
    "DATASET",
    "ETF_SESSION_REFERENCE_SCHEMA_VERSION",
    "PROVIDER",
    "BaoStockEtfEvidence",
    "BaoStockEtfRow",
    "EtfDailyBundle",
    "EtfDailySourceError",
    "SseDistributionTerms",
    "TencentSohuEtfDailySource",
    "parse_sse_distribution_notice_text",
]
