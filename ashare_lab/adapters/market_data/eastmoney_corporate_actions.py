"""Acquire free Eastmoney corporate-action reference evidence.

The public Eastmoney products expose three useful histories that AKShare also
uses: ``RPT_SHAREBONUS_DET`` for dividends/bonus shares,
``RPT_IPO_ALLOTMENT`` for rights issues, and ``RPT_F10_EH_EQUITY`` for share
capital changes.  The first two can prove their own filtered, paginated result
sets.  The equity history can corroborate a share-listing date and surface
split-like candidates, but it is not an explicit exhaustive stock-split or
reverse-split category query.  It can nevertheless provide a bounded negative
proof when every capital change in the requested interval has a recognized,
non-split reason or a matching issuer issuance announcement and share-class
ledger. Unresolved reasons and split-like positive candidates fail closed.
This mixed positive/negative contract is intentionally distinct from
the current strict Choice contract and cannot promote itself into that profile.

Every response page, including Eastmoney's explicit ``9201`` empty-result
shape, is retained with its raw wire SHA-256.  This module performs acquisition
only; the backtest runtime never calls the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import time as time_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.adapters.event_sources.collector import EventFetchBatch
from ashare_lab.adapters.event_sources.eastmoney import (
    ANNOUNCEMENT_LIST_URL,
    EastmoneyAnnouncementError,
    EastmoneyAnnouncementSource,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.market_data import CorporateAction, CorporateActionKind, TimeQuality
from ashare_lab.domain.shared import InstrumentId, StrongId

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
F10_DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
BONUS_FINANCING_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BonusFinancing/PageAjax"
DIVIDEND_MAIN_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
PROVIDER = "eastmoney_public_corporate_action_reference"
SCHEMA_VERSION = "eastmoney.corporate-action-reference.v1"
COVERAGE_SCOPE = "all_categories_mixed_positive_and_negative_proof"
SUPPORTED_CATEGORIES = (
    CorporateActionKind.CASH_DIVIDEND.value,
    CorporateActionKind.RIGHTS_ISSUE.value,
    CorporateActionKind.SHARE_DISTRIBUTION.value,
)
NEGATIVE_PROOF_CATEGORIES = (
    CorporateActionKind.REVERSE_SPLIT.value,
    CorporateActionKind.STOCK_SPLIT.value,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_ONLY_AVAILABLE_TIME = time(hour=15)
_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_PAGE_SIZE = 100
_MAX_PAGES = 1_000
_MAX_REQUEST_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.25, 0.75)
_ZERO_RESULT_CODE = 9201
_ZERO_RESULT_MESSAGE = "返回数据为空"
_CODE_RE = re.compile(r"^(?P<digits>[0-9]{6})(?:\.(?P<market>SH|SZ|BJ))?$")
_PREFIX_RE = re.compile(r"^(?P<market>SH|SZ|BJ)(?P<digits>[0-9]{6})$")
_SHARE_LISTING_MARKERS = ("送股上市", "转增股上市")
_STOCK_SPLIT_MARKERS = ("拆股", "股份拆细", "股票拆细")
_REVERSE_SPLIT_MARKERS = ("缩股", "合股", "股份合并", "股票合并")
_KNOWN_NON_SPLIT_CHANGE_MARKERS = (
    "债转股上市",
    "可转债转股",
    "高管股份变动",
    "限制性股票",
    "期权行权",
    "自主行权",
    "股份性质变更",
    "股权激励",
    "回购",
    "注销",
    "送股上市",
    "转增股上市",
    "配股上市",
    "增发",
    "非公开发行",
    "公开发行",
    "定向发行",
    "首次公开发行",
    "首发上市",
    "首发限售股份上市",
    "限售股上市",
    "限售股份上市",
    "网下配售股份上市",
    "战略配售上市",
    "股权分置改革",
)
_CHANGE_REASON_SEPARATOR = re.compile(r"[,，、;/；]+")
_IMPLEMENTATION_ANNOUNCEMENT_COLUMN = "001002002001005"
_IMPLEMENTATION_TITLE_RE = re.compile(r"(?:权益分派|利润分配(?:方案)?|分红派息)(?:方案)?实施公告")
_IMPLEMENTATION_TITLE_EXCLUSIONS = ("更正", "补充", "取消", "终止", "撤回")
_CASH_SETTLEMENT_PARSER_CONTRACT_VERSION = "eastmoney.cash-settlement-implementation.v1"
_CHINESE_DATE_FRAGMENT = r"(?P<year>[0-9]{4})年(?P<month>[0-9]{1,2})月(?P<day>[0-9]{1,2})日"
_RECORD_DATE_RE = re.compile(rf"(?:A股)?股权登记日(?:为|是)?[:：]?{_CHINESE_DATE_FRAGMENT}")
_EX_DATE_RE = re.compile(rf"(?:A股)?除权(?:除息)?日(?:为|是)?[:：]?{_CHINESE_DATE_FRAGMENT}")
_CASH_PAY_DATE_PATTERNS = (
    re.compile(rf"现金红利发放日(?:为|是)?[:：]?{_CHINESE_DATE_FRAGMENT}"),
    re.compile(
        rf"现金红利(?:将)?于{_CHINESE_DATE_FRAGMENT}[^。；]{{0,120}}?"
        r"(?:直接)?(?:划入|发放|到账)"
    ),
)
_CASH_PER_TEN_RE = re.compile(r"每10股[^。；\n]{0,60}?(?P<amount>[0-9]+(?:\.[0-9]+)?)元")
_IMPLEMENTED_ASSIGN_PROGRESS = frozenset(
    {
        "实施分配",
        "实施方案",
        "已实施",
    }
)
_NON_FINAL_ASSIGN_PROGRESS_MARKERS = (
    "董事会预案",
    "股东大会预案",
    "预案",
    "未实施",
    "取消",
    "终止",
)
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://data.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; corporate-action-reference)",
}

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class EastmoneyCorporateActionError(RuntimeError):
    """The public response cannot support the promised reference coverage."""


class EastmoneyCorporateActionValidationError(EastmoneyCorporateActionError):
    """Data was acquired, but its economic meaning is not yet verified."""


@dataclass(frozen=True, slots=True)
class EastmoneyPageEvidence:
    lane: str
    dataset: str
    url: str
    params: tuple[tuple[str, str], ...]
    page_number: int
    total_pages: int
    declared_count: int
    row_count: int
    zero_result: bool
    raw_wire_sha256: str
    canonical_payload_sha256: str
    payload: JsonObject

    def as_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "dataset": self.dataset,
            "url": self.url,
            "params": dict(self.params),
            "pageNumber": self.page_number,
            "totalPages": self.total_pages,
            "declaredCount": self.declared_count,
            "rowCount": self.row_count,
            "zeroResult": self.zero_result,
            "rawWireSha256": self.raw_wire_sha256,
            "canonicalPayloadSha256": self.canonical_payload_sha256,
            "rawPayload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class EastmoneySourceRow:
    value: JsonObject
    page_number: int
    raw_wire_sha256: str


@dataclass(frozen=True, slots=True)
class EastmoneyLaneCollection:
    lane: str
    dataset: str
    url: str
    rows: tuple[EastmoneySourceRow, ...]
    pages: tuple[EastmoneyPageEvidence, ...]
    declared_count: int
    zero_result: bool

    @property
    def raw_response_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json_bytes(
                [
                    {
                        "pageNumber": page.page_number,
                        "rawWireSha256": page.raw_wire_sha256,
                    }
                    for page in self.pages
                ]
            )
        ).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "dataset": self.dataset,
            "url": self.url,
            "querySucceeded": True,
            "declaredCount": self.declared_count,
            "rowCount": len(self.rows),
            "totalPages": len(self.pages),
            "zeroResult": self.zero_result,
            "rawResponseSha256": self.raw_response_sha256,
            "pages": [page.as_dict() for page in self.pages],
        }


@dataclass(frozen=True, slots=True)
class EastmoneyCorporateActionReferenceResult:
    instrument_id: InstrumentId
    start: date
    end: date
    captured_at: datetime
    corporate_actions: tuple[CorporateAction, ...]
    coverage: Mapping[str, object]
    query_collections: tuple[CorporateActionEvidenceCollection, ...]


@dataclass(frozen=True, slots=True)
class EastmoneyCashSettlementCollection:
    """Frozen issuer-announcement evidence used only for cash settlement dates."""

    notice_date: date
    observations: tuple[EventObservation, ...]
    acquisition_evidence: Mapping[str, object]

    @property
    def raw_response_sha256(self) -> str:
        return _canonical_sha256(
            {
                "acquisitionEvidence": dict(self.acquisition_evidence),
                "candidateDocuments": [
                    _cash_settlement_observation_evidence(item) for item in self.observations
                ],
            }
        )

    def as_dict(self) -> dict[str, object]:
        pagination = self.acquisition_evidence.get("pagination")
        total_hits = 0
        if isinstance(pagination, Mapping):
            raw_total = cast(Mapping[object, object], pagination).get("totalHits")
            if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0:
                total_hits = raw_total
        return {
            "lane": "cash_settlement_implementation_announcement",
            "dataset": "Eastmoney complete announcement interval + content document",
            "url": ANNOUNCEMENT_LIST_URL,
            "querySucceeded": self.acquisition_evidence.get("querySucceeded") is True,
            "parserContractVersion": _CASH_SETTLEMENT_PARSER_CONTRACT_VERSION,
            "noticeDate": self.notice_date.isoformat(),
            "declaredCount": total_hits,
            "rowCount": len(self.observations),
            "zeroResult": not self.observations,
            "rawResponseSha256": self.raw_response_sha256,
            "acquisitionEvidence": dict(self.acquisition_evidence),
            "candidateDocuments": [
                _cash_settlement_observation_evidence(item) for item in self.observations
            ],
        }


type CorporateActionEvidenceCollection = EastmoneyLaneCollection | EastmoneyCashSettlementCollection


class EastmoneyCorporateActionReferenceAdapter:
    """Collect immutable reference evidence from free Eastmoney web products."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT_SECONDS,
        page_size: int = _DEFAULT_PAGE_SIZE,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time_module.sleep,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        self._timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._page_size = page_size
        self._clock = clock or _now_shanghai
        self._sleeper = sleeper
        self._owns_client = client is None
        if client is None and transport is None:
            tls = ssl.create_default_context()
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.maximum_version = ssl.TLSVersion.TLSv1_2
            transport = httpx.HTTPTransport(
                local_address="0.0.0.0",
                verify=tls,
            )
        self._client = client or httpx.Client(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> EastmoneyCorporateActionReferenceAdapter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def prepare(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        captured_at: datetime | None = None,
    ) -> EastmoneyCorporateActionReferenceResult:
        if type(start) is not date or type(end) is not date:
            raise TypeError("start and end must be dates")
        if start > end:
            raise EastmoneyCorporateActionError("start must not exceed end")
        instrument_id, digits, prefixed_code = _normalize_symbol(symbol)
        capture_time = captured_at or self._clock()
        if capture_time.tzinfo is None or capture_time.utcoffset() is None:
            raise EastmoneyCorporateActionError("captured_at must be timezone-aware")
        if end > capture_time.astimezone(_SHANGHAI).date():
            raise EastmoneyCorporateActionError("cannot prove a future corporate-action interval")

        dividend = self._fetch_paginated(
            lane="dividend_and_share_distribution",
            dataset="RPT_SHAREBONUS_DET",
            url=DATACENTER_URL,
            params={
                "client": "WEB",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{digits}")',
                "pageSize": str(self._page_size),
                "reportName": "RPT_SHAREBONUS_DET",
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "source": "WEB",
            },
        )
        rights = self._fetch_paginated(
            lane="rights_issue",
            dataset="RPT_IPO_ALLOTMENT",
            url=DATACENTER_URL,
            params={
                "client": "WEB",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{digits}")',
                "pageSize": str(self._page_size),
                "reportName": "RPT_IPO_ALLOTMENT",
                "sortColumns": "EQUITY_RECORD_DATE",
                "sortTypes": "-1",
                "source": "WEB",
            },
        )
        equity = self._fetch_paginated(
            lane="capital_structure_candidate_scan",
            dataset="RPT_F10_EH_EQUITY",
            url=F10_DATACENTER_URL,
            params={
                "client": "PC",
                "columns": "ALL",
                "filter": f'(SECUCODE="{instrument_id.value}")',
                "pageSize": str(self._page_size),
                "reportName": "RPT_F10_EH_EQUITY",
                "sortColumns": "END_DATE",
                "sortTypes": "-1",
                "source": "HSF10",
            },
        )
        try:
            bonus_detail = self._fetch_bonus_financing(prefixed_code)
        except EastmoneyCorporateActionError:
            bonus_detail = self._fetch_dividend_main(instrument_id)

        _validate_lane_identity(dividend, digits=digits, instrument_id=instrument_id)
        _validate_lane_identity(rights, digits=digits, instrument_id=instrument_id)
        _validate_lane_identity(equity, digits=digits, instrument_id=instrument_id)
        settlement_collections = tuple(
            self._fetch_cash_settlement_announcements(
                instrument_id=instrument_id,
                notice_date=notice_date,
                captured_at=capture_time,
            )
            for notice_date in _cash_settlement_fallback_dates(
                dividend.rows,
                bonus_detail.rows,
                digits=digits,
                start=start,
                end=end,
            )
        )
        actions = _build_actions(
            instrument_id=instrument_id,
            digits=digits,
            start=start,
            end=end,
            captured_at=capture_time,
            dividend=dividend,
            rights=rights,
            equity=equity,
            bonus_detail=bonus_detail,
            settlement_announcements=settlement_collections,
        )
        negative_split_proof = _validate_negative_split_proof(
            equity,
            start=start,
            end=end,
            resolved_changes=self._resolve_ambiguous_equity(
                equity, instrument_id=instrument_id, start=start, end=end,
                captured_at=capture_time,
            ),
        )
        collections: tuple[CorporateActionEvidenceCollection, ...] = (
            dividend,
            rights,
            equity,
            bonus_detail,
            *settlement_collections,
        )
        coverage = _build_coverage(
            instrument_id=instrument_id,
            start=start,
            end=end,
            actions=actions,
            negative_split_proof=negative_split_proof,
            collections=collections,
        )
        return EastmoneyCorporateActionReferenceResult(
            instrument_id=instrument_id,
            start=start,
            end=end,
            captured_at=capture_time,
            corporate_actions=actions,
            coverage=coverage,
            query_collections=collections,
        )

    def _resolve_ambiguous_equity(
        self, equity: EastmoneyLaneCollection, *, instrument_id: InstrumentId,
        start: date, end: date, captured_at: datetime,
    ) -> dict[str, object]:
        """Corroborate ambiguous A-share incentive issuance, never infer a split factor."""
        resolved: dict[str, object] = {}
        batches: dict[date, EventFetchBatch] = {}
        for source_row in equity.rows:
            row = source_row.value
            changed = _optional_date(row.get("END_DATE"), "END_DATE")
            reason = str(row.get("CHANGE_REASON", ""))
            if changed is None or not start <= changed <= end or "其他变动原因" not in reason:
                continue
            if _limited_only_capital_increase(row):
                resolved[f"{changed.isoformat()}|{reason}"] = {
                    "basis": "capital_ledger_limited_only_increase",
                    "totalSharesChange": str(row["TOTAL_SHARES_CHANGE"]),
                    "listedASharesChange": "0",
                    "limitedASharesChange": str(row["LIMITED_ASHARES_CHANGE"]),
                    "meaning": "仅限售A股增加，现有流通A股数量不变；不生成送股或价格复权因子",
                }
                continue
            notice = _optional_date(row.get("NOTICE_DATE"), "NOTICE_DATE")
            if notice is None or notice > captured_at.astimezone(_SHANGHAI).date():
                continue
            if notice not in batches:
                try:
                    with EastmoneyAnnouncementSource(client=self._client, extract_document_text=True) as source:
                        batches[notice] = source.fetch_batch(
                            instrument_id=instrument_id, start=notice, end=notice,
                            retrieved_at=captured_at,
                            requested_provider_column_codes=("001002007001007001",),
                        )
                except EastmoneyAnnouncementError as exc:
                    raise EastmoneyCorporateActionError(
                        f"ambiguous equity event evidence acquisition failed: {exc}"
                    ) from exc
            batch = batches[notice]
            if batch.acquisition_evidence.get("querySucceeded") is not True:
                continue
            matches = [o for o in batch.observations if _matches_incentive_issuance(row, o)]
            if len(matches) == 1:
                resolved[f"{changed.isoformat()}|{reason}"] = {
                    "classification": "directed_incentive_issuance_no_holder_entitlement",
                    "accountSharesMultiplier": "1", "accountCashDelta": "0",
                    "acquisitionEvidence": dict(batch.acquisition_evidence),
                    "document": _cash_settlement_observation_evidence(matches[0]),
                }
        return resolved

    def _fetch_cash_settlement_announcements(
        self,
        *,
        instrument_id: InstrumentId,
        notice_date: date,
        captured_at: datetime,
    ) -> EastmoneyCashSettlementCollection:
        try:
            with EastmoneyAnnouncementSource(
                client=self._client,
                extract_document_text=True,
            ) as source:
                batch = source.fetch_batch(
                    instrument_id=instrument_id,
                    start=notice_date,
                    end=notice_date,
                    retrieved_at=captured_at,
                    requested_provider_column_codes=(_IMPLEMENTATION_ANNOUNCEMENT_COLUMN,),
                )
        except EastmoneyAnnouncementError as exc:
            raise EastmoneyCorporateActionError(
                f"cash-settlement implementation announcement acquisition failed: {exc}"
            ) from exc
        candidates = tuple(
            item for item in batch.observations if _is_cash_settlement_announcement(item)
        )
        return _cash_settlement_collection(
            notice_date=notice_date,
            batch=batch,
            candidates=candidates,
        )

    def _fetch_paginated(
        self,
        *,
        lane: str,
        dataset: str,
        url: str,
        params: Mapping[str, str],
    ) -> EastmoneyLaneCollection:
        first_params = {**params, "pageNumber": "1"}
        first_payload, first_raw = self._request_json(url, first_params)
        if _is_explicit_zero_result(first_payload):
            evidence = _page_evidence(
                lane=lane,
                dataset=dataset,
                url=url,
                params=first_params,
                page_number=1,
                total_pages=1,
                declared_count=0,
                rows=(),
                zero_result=True,
                raw=first_raw,
                payload=first_payload,
            )
            return EastmoneyLaneCollection(
                lane=lane,
                dataset=dataset,
                url=url,
                rows=(),
                pages=(evidence,),
                declared_count=0,
                zero_result=True,
            )

        first_result = _validated_result(first_payload, dataset=dataset)
        total_pages = _required_positive_int(first_result.get("pages"), f"{dataset}.pages")
        declared_count = _required_nonnegative_int(
            first_result.get("count"),
            f"{dataset}.count",
        )
        if total_pages > _MAX_PAGES:
            raise EastmoneyCorporateActionError(f"{dataset} exceeds the page safety limit")
        all_rows: list[EastmoneySourceRow] = []
        pages: list[EastmoneyPageEvidence] = []
        for page_number in range(1, total_pages + 1):
            page_params = {**params, "pageNumber": str(page_number)}
            if page_number == 1:
                payload, raw = first_payload, first_raw
                result = first_result
            else:
                payload, raw = self._request_json(url, page_params)
                if _is_explicit_zero_result(payload):
                    raise EastmoneyCorporateActionError(
                        f"{dataset} returned an empty-result sentinel inside pagination"
                    )
                result = _validated_result(payload, dataset=dataset)
            page_total = _required_positive_int(result.get("pages"), f"{dataset}.pages")
            page_count = _required_nonnegative_int(result.get("count"), f"{dataset}.count")
            if page_total != total_pages or page_count != declared_count:
                raise EastmoneyCorporateActionError(
                    f"{dataset} pagination metadata changed between pages"
                )
            row_values = _required_object_list(result.get("data"), f"{dataset}.data")
            raw_sha = hashlib.sha256(raw).hexdigest()
            pages.append(
                _page_evidence(
                    lane=lane,
                    dataset=dataset,
                    url=url,
                    params=page_params,
                    page_number=page_number,
                    total_pages=total_pages,
                    declared_count=declared_count,
                    rows=row_values,
                    zero_result=False,
                    raw=raw,
                    payload=payload,
                )
            )
            all_rows.extend(
                EastmoneySourceRow(
                    value=value,
                    page_number=page_number,
                    raw_wire_sha256=raw_sha,
                )
                for value in row_values
            )
        if len(all_rows) != declared_count:
            raise EastmoneyCorporateActionError(
                f"{dataset} returned {len(all_rows)} rows but declared {declared_count}"
            )
        row_hashes = [_canonical_sha256(row.value) for row in all_rows]
        if len(row_hashes) != len(set(row_hashes)):
            raise EastmoneyCorporateActionError(f"{dataset} pagination contains duplicate rows")
        return EastmoneyLaneCollection(
            lane=lane,
            dataset=dataset,
            url=url,
            rows=tuple(all_rows),
            pages=tuple(pages),
            declared_count=declared_count,
            zero_result=not all_rows,
        )

    def _fetch_dividend_main(self, instrument_id: InstrumentId) -> EastmoneyLaneCollection:
        """Alternative settlement dates; never infer amounts from annual totals."""
        dataset = "RPT_F10_DIVIDEND_MAIN"
        pages: list[EastmoneyPageEvidence] = []
        rows: list[EastmoneySourceRow] = []
        count = total_pages = None
        for number in range(1, _MAX_PAGES + 1):
            params = {
                "type": dataset, "sty": "ALL", "p": str(number),
                "ps": str(self._page_size), "sr": "-1", "st": "NOTICE_DATE",
                "filter": f'(SECUCODE="{instrument_id.value}")',
                "source": "SECURITIES", "client": "APP",
            }
            payload, raw = self._request_json(DIVIDEND_MAIN_URL, params)
            result = _validated_result(payload, dataset=dataset)
            page_count = _required_nonnegative_int(result.get("count"), "count")
            page_total = max(1, _required_nonnegative_int(result.get("pages"), "pages"))
            values = _required_object_list(result.get("data"), "data")
            if count is not None and (count != page_count or total_pages != page_total):
                raise EastmoneyCorporateActionError("dividend pagination changed during acquisition")
            count, total_pages = page_count, page_total
            if total_pages > _MAX_PAGES:
                raise EastmoneyCorporateActionError("dividend pagination exceeds acquisition limit")
            for value in values:
                if value.get("SECUCODE") != instrument_id.value:
                    raise EastmoneyCorporateActionError("dividend returned a different security")
            page = _page_evidence(
                lane="settlement_enrichment", dataset=dataset, url=DIVIDEND_MAIN_URL,
                params=params, page_number=number, total_pages=total_pages,
                declared_count=count, rows=values, zero_result=count == 0,
                raw=raw, payload=payload,
            )
            pages.append(page)
            rows.extend(EastmoneySourceRow(value, number, page.raw_wire_sha256) for value in values)
            if number == total_pages:
                break
        if len(rows) != count:
            raise EastmoneyCorporateActionError("dividend pagination row count mismatch")
        return EastmoneyLaneCollection(
            lane="settlement_enrichment", dataset=dataset, url=DIVIDEND_MAIN_URL,
            rows=tuple(rows), pages=tuple(pages), declared_count=count, zero_result=count == 0,
        )

    def _fetch_bonus_financing(self, prefixed_code: str) -> EastmoneyLaneCollection:
        params = {"code": prefixed_code}
        payload, raw = self._request_json(BONUS_FINANCING_URL, params)
        fhyx = _required_object_list(payload.get("fhyx"), "BonusFinancing.fhyx")
        _required_object_list(payload.get("pgmx"), "BonusFinancing.pgmx")
        raw_sha = hashlib.sha256(raw).hexdigest()
        rows = tuple(
            EastmoneySourceRow(value=value, page_number=1, raw_wire_sha256=raw_sha)
            for value in fhyx
        )
        page = _page_evidence(
            lane="settlement_enrichment",
            dataset="BonusFinancing.PageAjax",
            url=BONUS_FINANCING_URL,
            params=params,
            page_number=1,
            total_pages=1,
            declared_count=len(rows),
            rows=tuple(value.value for value in rows),
            zero_result=not rows,
            raw=raw,
            payload=payload,
        )
        return EastmoneyLaneCollection(
            lane="settlement_enrichment",
            dataset="BonusFinancing.PageAjax",
            url=BONUS_FINANCING_URL,
            rows=rows,
            pages=(page,),
            declared_count=len(rows),
            zero_result=not rows,
        )

    def _request_json(
        self,
        url: str,
        params: Mapping[str, str],
    ) -> tuple[JsonObject, bytes]:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, _MAX_REQUEST_ATTEMPTS + 1):
            try:
                response = self._client.get(
                    url,
                    params=dict(params),
                    headers=_HEADERS,
                    timeout=self._timeout,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                retryable_status = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and (exc.response.status_code == 429 or exc.response.status_code >= 500)
                )
                if attempt < _MAX_REQUEST_ATTEMPTS and (
                    isinstance(exc, httpx.TransportError) or retryable_status
                ):
                    self._sleeper(_RETRY_BACKOFF_SECONDS[attempt - 1])
                    continue
                raise EastmoneyCorporateActionError(
                    f"Eastmoney request failed: {exc}"
                ) from exc
            break
        else:  # pragma: no cover - the loop either returns a response or raises
            assert last_error is not None
            raise EastmoneyCorporateActionError(
                f"Eastmoney request failed: {last_error}"
            ) from last_error
        raw = response.content
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EastmoneyCorporateActionError("Eastmoney response is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise EastmoneyCorporateActionError("Eastmoney response must be a JSON object")
        return cast(JsonObject, decoded), raw


def to_choice_snapshot_payload(
    result: EastmoneyCorporateActionReferenceResult,
) -> dict[str, object]:
    """Return JSON-safe mixed-mode evidence for a future explicit promotion gate."""

    return {
        "schemaVersion": SCHEMA_VERSION,
        "instrumentId": result.instrument_id.value,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "capturedAt": result.captured_at.isoformat(),
        "actions": [_action_to_json(action) for action in result.corporate_actions],
        "coverage": dict(result.coverage),
        "queryCollections": [collection.as_dict() for collection in result.query_collections],
    }




def _build_actions(
    *,
    instrument_id: InstrumentId,
    digits: str,
    start: date,
    end: date,
    captured_at: datetime,
    dividend: EastmoneyLaneCollection,
    rights: EastmoneyLaneCollection,
    equity: EastmoneyLaneCollection,
    bonus_detail: EastmoneyLaneCollection,
    settlement_announcements: Sequence[EastmoneyCashSettlementCollection],
) -> tuple[CorporateAction, ...]:
    actions: list[CorporateAction] = []
    for source_row in dividend.rows:
        row = source_row.value
        ex_date = _optional_date(row.get("EX_DIVIDEND_DATE"), "EX_DIVIDEND_DATE")
        if ex_date is None or not start <= ex_date <= end:
            continue
        progress = _required_text(row.get("ASSIGN_PROGRESS"), "ASSIGN_PROGRESS")
        if progress not in _IMPLEMENTED_ASSIGN_PROGRESS:
            if any(marker in progress for marker in _NON_FINAL_ASSIGN_PROGRESS_MARKERS):
                continue
            raise EastmoneyCorporateActionError(
                f"dividend row has an unclassified implementation status: {progress}"
            )
        record_date = _required_date(row.get("EQUITY_RECORD_DATE"), "EQUITY_RECORD_DATE")
        notice_date = _required_date(row.get("NOTICE_DATE"), "NOTICE_DATE")
        if notice_date > record_date:
            raise EastmoneyCorporateActionError(
                "dividend implementation notice is later than the record date"
            )
        released_at = _date_only_available_at(notice_date)
        if captured_at < released_at:
            raise EastmoneyCorporateActionError("captured_at precedes a dividend source release")

        cash_per_ten = _optional_decimal(row.get("PRETAX_BONUS_RMB"), "PRETAX_BONUS_RMB")
        cash_per_share = (cash_per_ten or Decimal(0)) / Decimal(10)
        share_per_ten = _share_increment_per_ten(row)
        row_sha = _canonical_sha256(row)
        source_action_id = f"eastmoney:{digits}:{ex_date.strftime('%Y%m%d')}:{row_sha[:16]}"
        common: dict[str, object] = {
            "source_action_id": source_action_id,
            "instrument_id": instrument_id,
            "record_date": record_date,
            "ex_date": ex_date,
            "source_released_at": released_at,
            "vendor_first_available_at": None,
            "ingested_at": captured_at,
            "replay_available_at": released_at,
            "revision_no": 0,
            "time_quality": TimeQuality.DATE_ONLY_CONSERVATIVE,
            "provider": PROVIDER,
            "source_url": f"https://data.eastmoney.com/yjfp/detail/{digits}.html",
            "validation_status": "validated",
            "currency": "CNY",
        }
        if cash_per_share > 0:
            pay_date, enrichment_sha, settlement_source_url = _cash_pay_date(
                bonus_detail.rows,
                digits=digits,
                record_date=record_date,
                ex_date=ex_date,
                report_date=_required_date(row.get("REPORT_DATE"), "REPORT_DATE"),
                cash_per_ten=cash_per_share * Decimal(10),
                notice_date=notice_date,
                settlement_announcements=settlement_announcements,
            )
            raw_sha = _combined_evidence_sha(source_row.raw_wire_sha256, enrichment_sha)
            cash_common = {**common, "source_url": settlement_source_url}
            actions.append(
                CorporateAction(
                    action_id=StrongId(f"ca:{source_action_id}:cash"),
                    action_type=CorporateActionKind.CASH_DIVIDEND,
                    gross_cash_per_share=cash_per_share,
                    cash_pay_date=pay_date,
                    raw_response_sha256=raw_sha,
                    **cash_common,  # type: ignore[arg-type]
                )
            )
        if share_per_ten > 0:
            settlement_date, equity_sha = _share_settlement_date(
                equity.rows,
                ex_date=ex_date,
            )
            raw_sha = _combined_evidence_sha(source_row.raw_wire_sha256, equity_sha)
            actions.append(
                CorporateAction(
                    action_id=StrongId(f"ca:{source_action_id}:shares"),
                    action_type=CorporateActionKind.SHARE_DISTRIBUTION,
                    share_multiplier=Decimal(1) + share_per_ten / Decimal(10),
                    share_credit_date=settlement_date,
                    share_sellable_date=settlement_date,
                    raw_response_sha256=raw_sha,
                    **common,  # type: ignore[arg-type]
                )
            )

    for source_row in rights.rows:
        row = source_row.value
        ex_date = _optional_date(row.get("EX_DIVIDEND_DATE"), "EX_DIVIDEND_DATE")
        if ex_date is None or not start <= ex_date <= end:
            continue
        record_date = _required_date(row.get("EQUITY_RECORD_DATE"), "EQUITY_RECORD_DATE")
        notice_date = _required_date(row.get("FIRST_NOTICE_DATE"), "FIRST_NOTICE_DATE")
        if notice_date > record_date:
            raise EastmoneyCorporateActionError(
                "rights-issue first notice is later than the record date"
            )
        payment_deadline = _required_date(row.get("PAY_END_DATE"), "PAY_END_DATE")
        listing_date = _required_date(row.get("LISTING_DATE"), "LISTING_DATE")
        ratio_per_ten = _required_positive_decimal(row.get("PLACING_RATIO"), "PLACING_RATIO")
        price = _required_positive_decimal(row.get("ISSUE_PRICE"), "ISSUE_PRICE")
        released_at = _date_only_available_at(notice_date)
        if captured_at < released_at:
            raise EastmoneyCorporateActionError("captured_at precedes a rights source release")
        row_sha = _canonical_sha256(row)
        source_action_id = f"eastmoney:{digits}:{ex_date.strftime('%Y%m%d')}:rights:{row_sha[:12]}"
        actions.append(
            CorporateAction(
                action_id=StrongId(f"ca:{source_action_id}"),
                source_action_id=source_action_id,
                instrument_id=instrument_id,
                action_type=CorporateActionKind.RIGHTS_ISSUE,
                record_date=record_date,
                ex_date=ex_date,
                source_released_at=released_at,
                vendor_first_available_at=None,
                ingested_at=captured_at,
                replay_available_at=released_at,
                revision_no=0,
                time_quality=TimeQuality.DATE_ONLY_CONSERVATIVE,
                provider=PROVIDER,
                source_url="https://data.eastmoney.com/xg/pg/",
                raw_response_sha256=source_row.raw_wire_sha256,
                validation_status="validated",
                currency="CNY",
                rights_ratio=ratio_per_ten / Decimal(10),
                rights_subscription_price=price,
                rights_payment_deadline=payment_deadline,
                rights_listing_date=listing_date,
            )
        )
    identities: set[tuple[str, CorporateActionKind]] = set()
    for action in actions:
        identity = (action.source_action_id, action.action_type)
        if identity in identities:
            raise EastmoneyCorporateActionError("duplicate normalized corporate-action leg")
        identities.add(identity)
    return tuple(
        sorted(
            actions, key=lambda item: (item.ex_date, item.action_type.value, item.action_id.value)
        )
    )


def _build_coverage(
    *,
    instrument_id: InstrumentId,
    start: date,
    end: date,
    actions: Sequence[CorporateAction],
    negative_split_proof: Mapping[str, object],
    collections: Sequence[CorporateActionEvidenceCollection],
) -> dict[str, object]:
    query_payload = [collection.as_dict() for collection in collections]
    aggregate_sha = _canonical_sha256(query_payload)
    counts = {
        category: sum(action.action_type.value == category for action in actions)
        for category in (*SUPPORTED_CATEGORIES, *NEGATIVE_PROOF_CATEGORIES)
    }
    return {
        "status": "complete_mixed_mode",
        "querySucceeded": True,
        "provider": PROVIDER,
        "instrumentId": instrument_id.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rowCount": len(actions),
        "zeroResult": not actions,
        "rawResponseSha256": aggregate_sha,
        "normalizedResponseSha256": aggregate_sha,
        "coverageScope": COVERAGE_SCOPE,
        "positiveCapableCategories": list(SUPPORTED_CATEGORIES),
        "negativeProofCategories": list(NEGATIVE_PROOF_CATEGORIES),
        "unsupportedCategories": [],
        "strictEligibleUnderCurrentChoiceValidator": True,
        "categoryActionCounts": counts,
        "categoryCoverage": {
            CorporateActionKind.CASH_DIVIDEND.value: {
                "status": "complete_for_filtered_dataset",
                "dataset": (
                    "RPT_SHAREBONUS_DET + BonusFinancing.PageAjax + "
                    "issuer implementation announcement fallback"
                ),
                "zeroResult": counts[CorporateActionKind.CASH_DIVIDEND.value] == 0,
            },
            CorporateActionKind.SHARE_DISTRIBUTION.value: {
                "status": "complete_for_filtered_dataset",
                "dataset": "RPT_SHAREBONUS_DET + RPT_F10_EH_EQUITY",
                "zeroResult": counts[CorporateActionKind.SHARE_DISTRIBUTION.value] == 0,
            },
            CorporateActionKind.RIGHTS_ISSUE.value: {
                "status": "complete_for_filtered_dataset",
                "dataset": "RPT_IPO_ALLOTMENT",
                "zeroResult": counts[CorporateActionKind.RIGHTS_ISSUE.value] == 0,
            },
            CorporateActionKind.STOCK_SPLIT.value: {
                "status": "complete",
                "categoryMode": "complete_negative_proof",
                "dataset": "RPT_F10_EH_EQUITY full instrument history",
                "candidateCount": 0,
                "zeroResult": True,
            },
            CorporateActionKind.REVERSE_SPLIT.value: {
                "status": "complete",
                "categoryMode": "complete_negative_proof",
                "dataset": "RPT_F10_EH_EQUITY full instrument history",
                "candidateCount": 0,
                "zeroResult": True,
            },
        },
        "negativeSplitProof": dict(negative_split_proof),
        "strictBlockers": [],
        "positiveCandidatePolicy": (
            "a future split or reverse-split positive candidate fails closed until a source "
            "provides record date, settlement date, multiplier, and historical first availability"
        ),
        "timeQuality": TimeQuality.DATE_ONLY_CONSERVATIVE.value,
        "dateAvailabilityPolicy": "implementation notice date @ 15:00:00 Asia/Shanghai",
        "hashSemantics": "SHA-256 of each raw HTTP body plus canonical aggregate query evidence",
        "queryCollections": query_payload,
    }


def _limited_only_capital_increase(row: Mapping[str, object]) -> bool:
    """Prove no allocation to circulating A shares from reconciled share counts."""
    try:
        def value(key: str, *, nullable: bool = False) -> Decimal:
            raw = row.get(key)
            if raw is None and nullable:
                return Decimal(0)
            if raw is None or isinstance(raw, bool):
                raise ValueError("missing share count")
            number = Decimal(str(raw))
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError("invalid share count")
            return number
        delta = value("TOTAL_SHARES_CHANGE")
        total, listed, limited = (value(k) for k in ("TOTAL_SHARES", "LISTED_A_SHARES", "LIMITED_A_SHARES"))
        other_changes = ("H_FREESHARE_CHANGE", "LIMITED_H_SHARES_CHANGE", "B_FREESHARE_CHANGE",
                         "LIMITED_BSHARES_CHANGE", "OTHERFREE_SHARES_CHANGE", "NONFREE_SHARES_CHANGE")
        return (delta > 0 and total > delta and listed > 0 and limited >= delta
                and total == value("TOTAL_A_SHARES") == listed + limited
                and value("LISTED_ASHARES_CHANGE") == 0
                and value("LIMITED_ASHARES_CHANGE") == delta
                and all(value(k, nullable=True) == 0 for k in other_changes))
    except (ValueError, InvalidOperation):
        return False


def _matches_incentive_issuance(row: Mapping[str, object], observation: EventObservation) -> bool:
    """Match a completed directed A-share issue to the capital ledger exactly.

    Mixed A/H capital is reconciled separately; a vague reason alone never passes.
    Other ambiguous categories remain unresolved until their own evidence exists.
    """
    if observation.validation_status != "validated":
        return False
    tokens = set(_CHANGE_REASON_SEPARATOR.split(str(row.get("CHANGE_REASON", ""))))
    if not tokens <= {"其他变动原因", "自主行权", "期权行权", "限制性股票", "股权激励"}:
        return False
    title = re.sub(r"\s+", "", observation.title)
    if "限制性股票" not in title or "归属结果" not in title:
        return False
    text = re.sub(r"\s+", "", str(observation.attributes.get("document_text", "")))
    if not re.search(r"向激励对象定向发行[^。；]{0,30}A股", text):
        return False
    def number(key: str) -> Decimal | None:
        value = row.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            result = Decimal(str(value))
            return result if result.is_finite() and result == result.to_integral_value() else None
        except InvalidOperation:
            return None
    limited, listed, total, after = (number(k) for k in (
        "LIMITED_ASHARES_CHANGE", "LISTED_ASHARES_CHANGE", "TOTAL_SHARES_CHANGE", "TOTAL_SHARES"))
    if limited is None or limited <= 0 or listed != 0 or total is None or after is None:
        return False
    # Missing foreign-share deltas mean no such share class in this provider schema.
    foreign = [number(k) if row.get(k) is not None else Decimal(0) for k in (
        "H_FREESHARE_CHANGE", "LIMITED_H_SHARES_CHANGE", "B_FREESHARE_CHANGE",
        "LIMITED_BSHARES_CHANGE", "OTHERFREE_SHARES_CHANGE", "NONFREE_SHARES_CHANGE")]
    if any(v is None or v < 0 for v in foreign):
        return False
    foreign_delta = sum(v for v in foreign if v is not None)
    if total != limited + foreign_delta:
        return False
    if foreign_delta and not tokens.intersection({"自主行权", "期权行权"}):
        return False
    changed = _optional_date(row.get("END_DATE"), "END_DATE")
    if changed is None:
        return False
    registration = rf"{changed.year}年0?{changed.month}月0?{changed.day}日[^。]{{0,100}}股份完成登记"
    if not re.search(registration, text):
        return False
    # Announcement's A-issue-only before/after may already include same-day H exercise.
    raw_text = str(observation.attributes.get("document_text", ""))
    table = re.search(r"股本总数\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)", raw_text)
    if table is None:
        return False
    before_doc, amount_doc, after_doc = (Decimal(v.replace(",", "")) for v in table.groups())
    return amount_doc == limited and after_doc == after and before_doc + amount_doc == after_doc


def _is_verified_h_share_issue(row: JsonObject, reason: str) -> bool:
    """Recognize H issuance only when the ledger proves no A-share change."""
    if reason not in {"H股超额配售", "首发H股上市"}:
        return False
    def number(key: str, *, optional: bool = False) -> Decimal | None:
        value = row.get(key)
        if value is None and optional:
            return Decimal(0)
        if value is None or isinstance(value, bool):
            return None
        try:
            n = Decimal(str(value))
            return n if n.is_finite() and n == n.to_integral_value() else None
        except InvalidOperation:
            return None
    if any(number(k) != 0 for k in ("LISTED_ASHARES_CHANGE", "LIMITED_ASHARES_CHANGE")):
        return False
    if any(number(k, optional=True) != 0 for k in (
            "B_FREESHARE_CHANGE", "LIMITED_BSHARES_CHANGE", "NONFREE_SHARES_CHANGE",
            "OTHERFREE_SHARES_CHANGE", "LIMITED_H_SHARES_CHANGE")):
        return False
    issued = number("H_FREESHARE_CHANGE")
    return issued is not None and issued > 0 and number("TOTAL_SHARES_CHANGE") == issued


def _validate_negative_split_proof(
    equity: EastmoneyLaneCollection,
    *,
    start: date,
    end: date,
    resolved_changes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if equity.zero_result or not equity.rows:
        raise EastmoneyCorporateActionError(
            "capital-structure history cannot be empty for negative split proof"
        )
    scanned_rows = 0
    recognized_reasons: set[str] = set()
    for source_row in equity.rows:
        row = source_row.value
        changed_at = _optional_date(row.get("END_DATE"), "END_DATE")
        if changed_at is None or not start <= changed_at <= end:
            continue
        scanned_rows += 1
        reason = _required_text(row.get("CHANGE_REASON"), "CHANGE_REASON")
        if any(marker in reason for marker in _STOCK_SPLIT_MARKERS):
            raise EastmoneyCorporateActionError(
                "stock-split candidate lacks executable economic terms"
            )
        if any(marker in reason for marker in _REVERSE_SPLIT_MARKERS):
            raise EastmoneyCorporateActionError(
                "reverse-split candidate lacks executable economic terms"
            )
        tokens = tuple(
            token.strip() for token in _CHANGE_REASON_SEPARATOR.split(reason) if token.strip()
        )
        if not tokens or any(
            not any(marker in token for marker in _KNOWN_NON_SPLIT_CHANGE_MARKERS)
            for token in tokens
        ):
            if (f"{changed_at.isoformat()}|{reason}" not in (resolved_changes or {})
                    and not _is_verified_h_share_issue(row, reason)):
                raise EastmoneyCorporateActionValidationError(
                    f"capital-structure ledger has an unclassified change reason: {reason}"
                )
        recognized_reasons.update(tokens)
    return {
        "categoryMode": "complete_negative_proof",
        "queryScope": "full_instrument_history_filtered_locally_to_requested_interval",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "scannedRows": scanned_rows,
        "recognizedChangeReasons": sorted(recognized_reasons),
        "resolvedAmbiguousChanges": dict(resolved_changes or {}),
        "stockSplitCandidates": 0,
        "reverseSplitCandidates": 0,
        "sourceDataset": equity.dataset,
        "sourceDeclaredCount": equity.declared_count,
        "sourceTotalPages": len(equity.pages),
        "sourceRawResponseSha256": equity.raw_response_sha256,
    }


def _validate_lane_identity(
    collection: EastmoneyLaneCollection,
    *,
    digits: str,
    instrument_id: InstrumentId,
) -> None:
    for source_row in collection.rows:
        row = source_row.value
        code = row.get("SECURITY_CODE")
        secucode = row.get("SECUCODE")
        if code is not None and code != digits:
            raise EastmoneyCorporateActionError(
                f"{collection.dataset} returned a different security code"
            )
        if secucode is not None and str(secucode).upper() != instrument_id.value:
            raise EastmoneyCorporateActionError(
                f"{collection.dataset} returned a different SECUCODE"
            )


def _cash_pay_date(
    rows: Sequence[EastmoneySourceRow],
    *,
    digits: str,
    record_date: date,
    ex_date: date,
    report_date: date,
    cash_per_ten: Decimal,
    notice_date: date,
    settlement_announcements: Sequence[EastmoneyCashSettlementCollection],
) -> tuple[date, str, str]:
    direct = _bonus_cash_pay_date(
        rows,
        digits=digits,
        record_date=record_date,
        ex_date=ex_date,
    )
    if direct is not None:
        return (
            direct[0],
            direct[1],
            f"https://data.eastmoney.com/yjfp/detail/{digits}.html",
        )

    return _cash_pay_date_from_announcements(
        settlement_announcements,
        report_date=report_date,
        record_date=record_date,
        ex_date=ex_date,
        cash_per_ten=cash_per_ten,
        notice_date=notice_date,
    )


def _bonus_cash_pay_date(
    rows: Sequence[EastmoneySourceRow],
    *,
    digits: str,
    record_date: date,
    ex_date: date,
) -> tuple[date, str] | None:
    matches: list[tuple[date, str]] = []
    for source_row in rows:
        row = source_row.value
        if row.get("SECURITY_CODE") != digits:
            continue
        if row.get("ASSIGN_PROGRESS") not in _IMPLEMENTED_ASSIGN_PROGRESS:
            continue
        candidate_record = _optional_date(row.get("EQUITY_RECORD_DATE"), "EQUITY_RECORD_DATE")
        candidate_ex = _optional_date(row.get("EX_DIVIDEND_DATE"), "EX_DIVIDEND_DATE")
        if candidate_record == record_date and candidate_ex == ex_date:
            pay_date = _optional_date(row.get("PAY_CASH_DATE"), "PAY_CASH_DATE")
            if pay_date is None:
                continue
            matches.append((pay_date, source_row.raw_wire_sha256))
    unique_dates = {item[0] for item in matches}
    if not matches:
        return None
    if len(unique_dates) != 1:
        raise EastmoneyCorporateActionError(
            "cash dividend has conflicting PAY_CASH_DATE enrichment rows"
        )
    return matches[0]


def _cash_settlement_fallback_dates(
    dividend_rows: Sequence[EastmoneySourceRow],
    bonus_rows: Sequence[EastmoneySourceRow],
    *,
    digits: str,
    start: date,
    end: date,
) -> tuple[date, ...]:
    required: set[date] = set()
    for source_row in dividend_rows:
        row = source_row.value
        ex_date = _optional_date(row.get("EX_DIVIDEND_DATE"), "EX_DIVIDEND_DATE")
        if ex_date is None or not start <= ex_date <= end:
            continue
        progress = _required_text(row.get("ASSIGN_PROGRESS"), "ASSIGN_PROGRESS")
        if progress not in _IMPLEMENTED_ASSIGN_PROGRESS:
            continue
        cash_per_ten = _optional_decimal(row.get("PRETAX_BONUS_RMB"), "PRETAX_BONUS_RMB")
        if cash_per_ten is None or cash_per_ten <= 0:
            continue
        record_date = _required_date(row.get("EQUITY_RECORD_DATE"), "EQUITY_RECORD_DATE")
        if (
            _bonus_cash_pay_date(
                bonus_rows,
                digits=digits,
                record_date=record_date,
                ex_date=ex_date,
            )
            is None
        ):
            required.add(_required_date(row.get("NOTICE_DATE"), "NOTICE_DATE"))
    return tuple(sorted(required))


def _cash_settlement_collection(
    *,
    notice_date: date,
    batch: EventFetchBatch,
    candidates: tuple[EventObservation, ...],
) -> EastmoneyCashSettlementCollection:
    evidence = dict(batch.acquisition_evidence)
    if evidence.get("querySucceeded") is not True:
        raise EastmoneyCorporateActionError(
            "cash-settlement announcement interval query did not complete"
        )
    if (
        evidence.get("start") != notice_date.isoformat()
        or evidence.get("end") != notice_date.isoformat()
    ):
        raise EastmoneyCorporateActionError(
            "cash-settlement announcement evidence does not match the requested date"
        )
    return EastmoneyCashSettlementCollection(
        notice_date=notice_date,
        observations=candidates,
        acquisition_evidence=evidence,
    )


def _is_cash_settlement_announcement(observation: EventObservation) -> bool:
    normalized_title = re.sub(r"\s+", "", observation.title)
    if any(marker in normalized_title for marker in _IMPLEMENTATION_TITLE_EXCLUSIONS):
        return False
    if _IMPLEMENTATION_TITLE_RE.search(normalized_title) is None:
        return False
    raw_columns = observation.attributes.get("raw_columns_json")
    if not isinstance(raw_columns, str):
        return False
    try:
        columns: object = json.loads(raw_columns)
    except json.JSONDecodeError:
        return False
    if not isinstance(columns, list):
        return False
    for raw_item in cast(list[object], columns):
        if not isinstance(raw_item, Mapping):
            continue
        item = cast(Mapping[object, object], raw_item)
        if item.get("column_code") == _IMPLEMENTATION_ANNOUNCEMENT_COLUMN:
            return True
    return False


def _cash_pay_date_from_announcements(
    collections: Sequence[EastmoneyCashSettlementCollection],
    *,
    report_date: date,
    record_date: date,
    ex_date: date,
    cash_per_ten: Decimal,
    notice_date: date,
) -> tuple[date, str, str]:
    matches: list[tuple[date, str, str]] = []
    for collection in collections:
        if collection.notice_date != notice_date:
            continue
        for observation in collection.observations:
            match = _parse_cash_settlement_announcement(
                observation,
                report_date=report_date,
                record_date=record_date,
                ex_date=ex_date,
                cash_per_ten=cash_per_ten,
            )
            if match is not None:
                matches.append(
                    (
                        match,
                        _combined_evidence_sha(
                            collection.raw_response_sha256,
                            observation.raw_response_sha256 or "",
                            observation.document_sha256 or "",
                            _required_observation_attribute(
                                observation,
                                "raw_content_response_sha256",
                            ),
                            _required_observation_attribute(
                                observation,
                                "document_text_sha256",
                            ),
                        ),
                        observation.document_url
                        or _required_observation_attribute(observation, "source_url"),
                    )
                )
    if len(matches) != 1:
        raise EastmoneyCorporateActionError(
            "cash dividend lacks one unambiguous issuer implementation announcement "
            "with an explicit cash settlement date"
        )
    return matches[0]


def _parse_cash_settlement_announcement(
    observation: EventObservation,
    *,
    report_date: date,
    record_date: date,
    ex_date: date,
    cash_per_ten: Decimal,
) -> date | None:
    if observation.validation_status != "validated":
        return None
    title = re.sub(r"\s+", "", observation.title)
    if not _report_period_matches(title, report_date):
        return None
    text = _required_observation_attribute(observation, "document_text")
    compact = re.sub(r"\s+", "", text)
    if _single_chinese_date(_RECORD_DATE_RE, compact) != record_date:
        return None
    if _single_chinese_date(_EX_DATE_RE, compact) != ex_date:
        return None
    stated_amounts = {
        _required_positive_decimal(
            match.group("amount"),
            "implementation announcement cash amount",
        )
        for match in _CASH_PER_TEN_RE.finditer(compact)
    }
    if cash_per_ten not in stated_amounts:
        return None
    pay_dates = {
        parsed
        for pattern in _CASH_PAY_DATE_PATTERNS
        for match in pattern.finditer(compact)
        if (parsed := _date_from_match(match)) is not None
    }
    if len(pay_dates) != 1:
        return None
    pay_date = next(iter(pay_dates))
    if pay_date < ex_date:
        return None
    return pay_date


def _report_period_matches(title: str, report_date: date) -> bool:
    year = str(report_date.year)
    month_day = (report_date.month, report_date.day)
    if month_day == (12, 31):
        return f"{year}年度" in title
    if month_day == (6, 30):
        return any(marker in title for marker in (f"{year}年半年度", f"{year}年中期"))
    if month_day == (3, 31):
        return any(marker in title for marker in (f"{year}年第一季度", f"{year}年一季度"))
    if month_day == (9, 30):
        return any(marker in title for marker in (f"{year}年第三季度", f"{year}年三季度"))
    return False


def _single_chinese_date(pattern: re.Pattern[str], text: str) -> date | None:
    dates = {
        parsed
        for match in pattern.finditer(text)
        if (parsed := _date_from_match(match)) is not None
    }
    return next(iter(dates)) if len(dates) == 1 else None


def _date_from_match(match: re.Match[str]) -> date | None:
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _required_observation_attribute(
    observation: EventObservation,
    field_name: str,
) -> str:
    value = observation.attributes.get(field_name)
    if not isinstance(value, str) or not value:
        raise EastmoneyCorporateActionError(f"cash-settlement announcement is missing {field_name}")
    return value


def _cash_settlement_observation_evidence(
    observation: EventObservation,
) -> dict[str, object]:
    attributes = observation.attributes
    retained_attribute_names = (
        "raw_notice_date",
        "raw_columns_json",
        "raw_codes_json",
        "raw_payload_json",
        "raw_content_payload_json",
        "raw_content_response_sha256",
        "raw_pdf_response_sha256",
        "document_text",
        "document_text_sha256",
        "document_text_source_sha256",
        "document_text_source_format",
        "document_text_extractor",
        "document_text_extractor_version",
        "document_text_extractor_library_version",
        "document_text_normalization",
        "document_text_quality",
        "document_text_page_count",
        "document_text_provider_page_count",
        "document_text_extracted_page_count",
        "document_text_empty_page_count",
        "document_text_character_count",
        "document_text_non_whitespace_character_count",
        "document_text_pages_json",
    )
    return {
        "providerEventId": observation.provider_event_id,
        "instrumentId": observation.instrument_id.value,
        "title": observation.title,
        "sourceReleasedAt": (
            observation.source_released_at.isoformat()
            if observation.source_released_at is not None
            else None
        ),
        "vendorFirstAvailableAt": (
            observation.vendor_first_available_at.isoformat()
            if observation.vendor_first_available_at is not None
            else None
        ),
        "retrievedAt": observation.retrieved_at.isoformat(),
        "documentUrl": observation.document_url,
        "documentSha256": observation.document_sha256,
        "rawListResponseSha256": observation.raw_response_sha256,
        "validationStatus": observation.validation_status,
        "attributes": {
            name: attributes.get(name) for name in retained_attribute_names if name in attributes
        },
    }


def _share_settlement_date(
    rows: Sequence[EastmoneySourceRow],
    *,
    ex_date: date,
) -> tuple[date, str]:
    matches: list[tuple[date, str]] = []
    for source_row in rows:
        row = source_row.value
        changed_at = _optional_date(row.get("END_DATE"), "END_DATE")
        reason = _required_text(row.get("CHANGE_REASON"), "CHANGE_REASON")
        if changed_at == ex_date and any(marker in reason for marker in _SHARE_LISTING_MARKERS):
            matches.append((ex_date, source_row.raw_wire_sha256))
    if not matches:
        raise EastmoneyCorporateActionError(
            "share distribution lacks a matching Eastmoney share-listing row"
        )
    return matches[0]


def _share_increment_per_ten(row: Mapping[str, JsonValue]) -> Decimal:
    bonus = _optional_decimal(row.get("BONUS_RATIO"), "BONUS_RATIO") or Decimal(0)
    capitalization = _optional_decimal(row.get("IT_RATIO"), "IT_RATIO") or Decimal(0)
    total = _optional_decimal(row.get("BONUS_IT_RATIO"), "BONUS_IT_RATIO")
    component_total = bonus + capitalization
    if total is not None and component_total not in {Decimal(0), total}:
        raise EastmoneyCorporateActionError("share-distribution ratio fields conflict")
    value = total if total is not None else component_total
    if value < 0:
        raise EastmoneyCorporateActionError("share-distribution ratio cannot be negative")
    return value


def _validated_result(payload: JsonObject, *, dataset: str) -> JsonObject:
    if payload.get("success") is not True or payload.get("code") != 0:
        raise EastmoneyCorporateActionError(f"{dataset} query did not succeed")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise EastmoneyCorporateActionError(f"{dataset}.result must be an object")
    return cast(JsonObject, result)


def _is_explicit_zero_result(payload: JsonObject) -> bool:
    return (
        payload.get("success") is False
        and payload.get("code") == _ZERO_RESULT_CODE
        and payload.get("message") == _ZERO_RESULT_MESSAGE
        and payload.get("result") is None
    )


def _page_evidence(
    *,
    lane: str,
    dataset: str,
    url: str,
    params: Mapping[str, str],
    page_number: int,
    total_pages: int,
    declared_count: int,
    rows: Sequence[JsonObject],
    zero_result: bool,
    raw: bytes,
    payload: JsonObject,
) -> EastmoneyPageEvidence:
    return EastmoneyPageEvidence(
        lane=lane,
        dataset=dataset,
        url=url,
        params=tuple(sorted(params.items())),
        page_number=page_number,
        total_pages=total_pages,
        declared_count=declared_count,
        row_count=len(rows),
        zero_result=zero_result,
        raw_wire_sha256=hashlib.sha256(raw).hexdigest(),
        canonical_payload_sha256=_canonical_sha256(payload),
        payload=payload,
    )


def _required_object_list(value: JsonValue, field_name: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, list):
        raise EastmoneyCorporateActionError(f"{field_name} must be an array")
    rows: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise EastmoneyCorporateActionError(f"{field_name} must contain objects")
        rows.append(cast(JsonObject, item))
    return tuple(rows)


def _required_positive_int(value: JsonValue, field_name: str) -> int:
    parsed = _required_nonnegative_int(value, field_name)
    if parsed < 1:
        raise EastmoneyCorporateActionError(f"{field_name} must be positive")
    return parsed


def _required_nonnegative_int(value: JsonValue, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EastmoneyCorporateActionError(f"{field_name} must be a nonnegative integer")
    return value


def _required_text(value: JsonValue, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EastmoneyCorporateActionError(f"{field_name} must be non-empty text")
    return value.strip()


def _required_date(value: JsonValue, field_name: str) -> date:
    parsed = _optional_date(value, field_name)
    if parsed is None:
        raise EastmoneyCorporateActionError(f"{field_name} is required")
    return parsed


def _optional_date(value: JsonValue, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise EastmoneyCorporateActionError(f"{field_name} must be datetime text")
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise EastmoneyCorporateActionError(f"{field_name} is not ISO datetime text") from exc


def _required_positive_decimal(value: JsonValue, field_name: str) -> Decimal:
    parsed = _optional_decimal(value, field_name)
    if parsed is None or parsed <= 0:
        raise EastmoneyCorporateActionError(f"{field_name} must be positive")
    return parsed


def _optional_decimal(value: JsonValue, field_name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise EastmoneyCorporateActionError(f"{field_name} must be numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise EastmoneyCorporateActionError(f"{field_name} must be numeric") from exc
    if not parsed.is_finite():
        raise EastmoneyCorporateActionError(f"{field_name} must be finite")
    return parsed


def _normalize_symbol(value: str) -> tuple[InstrumentId, str, str]:
    raw = value.strip().upper()
    match = _CODE_RE.fullmatch(raw)
    if match is not None:
        digits = match.group("digits")
        market = match.group("market") or _market_for_digits(digits)
    else:
        prefixed = _PREFIX_RE.fullmatch(raw)
        if prefixed is None:
            raise EastmoneyCorporateActionError(
                "symbol must be a supported six-digit SH/SZ/BJ A-share code"
            )
        digits = prefixed.group("digits")
        market = prefixed.group("market")
    if market != _market_for_digits(digits):
        raise EastmoneyCorporateActionError("stock code prefix does not match exchange suffix")
    return InstrumentId(f"{digits}.{market}"), digits, f"{market}{digits}"


def _market_for_digits(digits: str) -> str:
    if digits.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if digits.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    if digits.startswith("920"):
        return "BJ"
    raise EastmoneyCorporateActionError("code is not a supported SH/SZ/BJ CNY ordinary share")


def _date_only_available_at(value: date) -> datetime:
    return datetime.combine(value, _DATE_ONLY_AVAILABLE_TIME, tzinfo=_SHANGHAI)


def _combined_evidence_sha(*digests: str) -> str:
    return hashlib.sha256(_canonical_json_bytes(sorted(digests))).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _now_shanghai() -> datetime:
    return datetime.now(_SHANGHAI).replace(microsecond=0)


def _action_to_json(action: CorporateAction) -> dict[str, object]:
    return {
        "action_id": action.action_id.value,
        "source_action_id": action.source_action_id,
        "action_type": action.action_type.value,
        "record_date": action.record_date.isoformat(),
        "ex_date": action.ex_date.isoformat(),
        "source_released_at": _optional_iso_datetime(action.source_released_at),
        "vendor_first_available_at": _optional_iso_datetime(action.vendor_first_available_at),
        "ingested_at": action.ingested_at.isoformat(),
        "replay_available_at": action.replay_available_at.isoformat(),
        "revision_no": action.revision_no,
        "time_quality": action.time_quality.value,
        "provider": action.provider,
        "source_url": action.source_url,
        "raw_response_sha256": action.raw_response_sha256.removeprefix("sha256:"),
        "validation_status": action.validation_status,
        "currency": action.currency,
        "gross_cash_per_share": _optional_decimal_text(action.gross_cash_per_share),
        "cash_pay_date": _optional_iso_date(action.cash_pay_date),
        "share_multiplier": _optional_decimal_text(action.share_multiplier),
        "share_credit_date": _optional_iso_date(action.share_credit_date),
        "share_sellable_date": _optional_iso_date(action.share_sellable_date),
        "rights_ratio": _optional_decimal_text(action.rights_ratio),
        "rights_subscription_price": _optional_decimal_text(action.rights_subscription_price),
        "rights_payment_deadline": _optional_iso_date(action.rights_payment_deadline),
        "rights_listing_date": _optional_iso_date(action.rights_listing_date),
    }


def _optional_iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
