"""Thin, server-owned adapter for 东方财富妙想实时选股.

The provider owns its indicator aliases and query grammar.  This adapter does
not recreate that catalogue locally: it forwards the user's screening text and
returns the provider-labelled columns with an auditable response hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import ssl
import unicodedata
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous, InstrumentNameCandidate
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
    LiveScreenedFinanceDataResult,
    LiveSecurityEntity,
)
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)

from .mx_indicator_contract import MxIndicatorFieldContract, build_indicator_contract

_SCHEMA_VERSION = "eastmoney-mx.select-security.v1"
_FINANCE_SCHEMA_VERSION = "eastmoney-mx.search-data.v1"
_INDICATOR_HISTORY_SCHEMA_VERSION = "eastmoney-mx.provider-indicator-history.v1"
_DEFAULT_BASE_URL = "https://ai-saas.eastmoney.com"
_DEFAULT_MAX_ATTEMPTS = 3
# Give a transient provider/connection failure time to recover. Keep retries
# bounded and at this HTTP layer only, reusing the original read request.
_RETRY_BASE_BACKOFF_SECONDS = 1.0
_RETRY_MAX_BACKOFF_SECONDS = 4.0
_LOGGER = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429})
_DIRECT_ENTITY_LIMIT = 5
_MAX_SCREENED_ENTITIES = 500
_SECURITY_CODE_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_MARKDOWN_DELIMITER_RE = re.compile(r"^:?-{3,}:?$")
_SUCCESS_STATUS_VALUES = (None, 0, 200, "0", "200")
_AUTH_STATUS_VALUES = (401, 403, "401", "403")
_PARTIAL_RESULT_MARKERS = (
    "截断",
    "精简后的部分数据",
    "检测到您的数据范围较大",
    "权限不足",
    "数据量已达到上限",
)
_ENTITY_CODE_KEYS = (
    "code",
    "symbol",
    "securityCode",
    "secuCode",
    "entityCode",
    "stockCode",
    "fundCode",
)
_ENTITY_CODE_LIST_KEYS = (
    "entityCodes",
    "securityCodes",
    "secuCodes",
    "stockCodes",
    "fundCodes",
)
_ENTITY_CODE_HEADERS = frozenset(
    {
        "代码",
        "证券代码",
        "股票代码",
        "基金代码",
        "code",
        "symbol",
        "securitycode",
        "secucode",
        "entitycode",
        "stockcode",
        "fundcode",
    }
)
_INDICATOR_SEPARATOR_RE = re.compile(r"\s*(?:、|,|，|和|与|及|/)\s*")
_DISPLAY_UNIT_SUFFIXES = (
    "(元)",
    "（元）",
    "(%)",
    "（%）",
    "(％)",
    "（％）",
    "(倍)",
    "（倍）",
    "(股)",
    "（股）",
    "(手)",
    "（手）",
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
MxTool = Literal["selectSecurity", "searchData", "unknown"]
MxFailureReason = Literal["read_timeout", "connect_timeout", "transport_error", "http_error"]


@dataclass(frozen=True)
class MxRetryProgress:
    tool: MxTool
    call_id: str
    retry_number: int
    max_retries: int
    recovered: bool = False
    data_incomplete: bool = False


_RETRY_OBSERVER: ContextVar[Callable[[MxRetryProgress], None] | None] = ContextVar(
    "mx_retry_observer", default=None,
)


@contextmanager
def observe_mx_retries(callback: Callable[[MxRetryProgress], None]) -> Generator[None]:
    """Scope progress to the current run, including its concurrent async reads."""
    token = _RETRY_OBSERVER.set(callback)
    try:
        yield
    finally:
        _RETRY_OBSERVER.reset(token)


def _notify_retry_progress(event: MxRetryProgress) -> None:
    observer = _RETRY_OBSERVER.get()
    if observer is not None:
        observer(event)


def notify_mx_data_retry(call_id: str, *, recovered: bool = False) -> None:
    """Reuse the run-scoped progress channel for one missing-field requery."""
    _notify_retry_progress(MxRetryProgress(
        tool="searchData", call_id=call_id, retry_number=1, max_retries=1,
        recovered=recovered, data_incomplete=True,
    ))


class MxSaasProviderError(RuntimeError):
    """Base error that never exposes provider credentials."""

    def __init__(
        self,
        message: str,
        *,
        tool: MxTool = "unknown",
        reason: MxFailureReason | None = None,
        http_status: int | None = None,
        transport_kind: str | None = None,
        attempts: int | None = None,
        call_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tool = tool
        self.reason = reason
        self.http_status = http_status
        self.transport_kind = transport_kind
        self.attempts = attempts
        self.call_id = call_id


class MxSaasProviderAuthError(MxSaasProviderError):
    """The configured provider credential was rejected."""


class MxSaasProviderUnavailableError(MxSaasProviderError):
    """The provider cannot be reached for this request."""


class MxSaasProviderDataError(MxSaasProviderError):
    """The provider response cannot safely be interpreted."""


class MxSaasProviderNoDataError(MxSaasProviderDataError):
    """The provider answered successfully but selected no usable entity."""


class MxSaasMarketDataClient:
    """Minimal adapter for the documented ``selectSecurity`` endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        strict_indicator_contracts: bool = False,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._strict_indicator_contracts = strict_indicator_contracts

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        cleaned_query = query.strip()
        cleaned_asset_type = asset_type.strip()
        if not cleaned_query or not cleaned_asset_type:
            raise ValueError("query and asset_type must not be blank")
        payload = {
            "query": cleaned_query,
            "selectType": cleaned_asset_type,
            "toolContext": {
                "callId": f"screen_{uuid4().hex}",
                "userInfo": {"userId": "ashare-backtest-service"},
            },
        }
        response = await self._post(
            path="/proxy/b/mcp/tool/selectSecurity",
            payload=payload,
        )
        raw = response.content
        decoded = _decode_provider_response(response)
        _raise_for_provider_status(response, decoded, tool="selectSecurity")
        result = _result_node(decoded)
        columns: tuple[str, ...] = ()
        rows: tuple[Mapping[str, Any], ...] = ()
        if result is not None:
            columns, column_labels = _column_metadata(result.get("columns"))
            rows = _normalise_rows(result.get("dataList"), columns, column_labels)
        if not rows:
            partial_rows = _partial_result_rows(decoded)
            if partial_rows:
                columns = tuple(partial_rows[0])
                rows = partial_rows
        if not rows:
            raise MxSaasProviderNoDataError(
                "real-time screening provider returned no matching data"
            )
        return LiveMarketDataResult(
            provider="eastmoney_mx_screener",
            query=cleaned_query,
            asset_type=cleaned_asset_type,
            columns=columns,
            rows=rows,
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                retrieved_at=self._validated_retrieved_at(),
                schema_version=_SCHEMA_VERSION,
            ),
            provider_metadata=_screen_provider_metadata(decoded, result),
        )

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        """Query current values through the provider's documented data skill.

        The provider owns recognition, metric aliases, units and table shape.
        This thin adapter neither derives financial fields nor turns a current
        reply into point-in-time historical data.
        """

        cleaned_query = query.strip()
        cleaned_indicators = indicators.strip() if indicators is not None else None
        if not cleaned_query:
            raise ValueError("query must not be blank")
        provider_query = _query_with_indicator_hint(cleaned_query, cleaned_indicators)
        payload = {
            "query": provider_query,
            "toolContext": {
                "callId": f"finance_{uuid4().hex}",
                "userInfo": {"userId": "ashare-backtest-service"},
            },
        }
        response = await self._post(
            path="/proxy/b/mcp/tool/searchData",
            payload=payload,
        )
        raw = response.content
        decoded = _decode_provider_response(response)
        _raise_for_provider_status(response, decoded, tool="searchData")
        tables = _finance_tables(decoded)
        if not tables:
            raise MxSaasProviderNoDataError(
                "real-time financial provider returned no matching data"
            )
        retrieved_at = self._validated_retrieved_at()
        return LiveFinanceDataResult(
            provider="eastmoney_mx_finance_data",
            query=provider_query,
            indicators=cleaned_indicators or None,
            tables=tables,
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                retrieved_at=retrieved_at,
                schema_version=_FINANCE_SCHEMA_VERSION,
            ),
        )

    async def query_indicator_history(
        self,
        *,
        instrument_id: str,
        indicator_id: str,
        provider_indicator_name: str,
        value_names: tuple[str, ...],
        start: date,
        end: date,
    ) -> ProviderIndicatorSeries:
        """Fetch provider-calculated daily indicator values for one security.

        This method intentionally never requests OHLCV and never derives the
        indicator locally.  A response is accepted only when ``rawTable``
        binds the exact security, requested dates and every requested value.
        """

        if start > end:
            raise ValueError("indicator history range is inverted")
        try:
            canonical_instrument = str(normalize_a_share_instrument(instrument_id))
        except AshareInstrumentCodeError as exc:
            raise ValueError("instrument_id must be a canonical A-share symbol") from exc
        cleaned_indicator_id = indicator_id.strip()
        cleaned_provider_name = provider_indicator_name.strip()
        cleaned_value_names = tuple(name.strip() for name in value_names)
        if (
            not cleaned_indicator_id
            or not cleaned_provider_name
            or not cleaned_value_names
            or any(not name for name in cleaned_value_names)
            or len(set(name.casefold() for name in cleaned_value_names)) != len(cleaned_value_names)
        ):
            raise ValueError("indicator history identity and value names cannot be blank")
        range_text = f"{start.isoformat()}至{end.isoformat()}"
        value_text = "、".join(cleaned_value_names)
        query = (
            f"查询{canonical_instrument}{range_text}每个交易日的"
            f"{cleaned_provider_name}指标{value_text}"
        )
        contract = (
            build_indicator_contract(
                cleaned_indicator_id, cleaned_provider_name, cleaned_value_names
            )
            if self._strict_indicator_contracts
            else None
        )
        if contract is not None:
            query_fields = contract.query_fields.replace("、", "和")
            adjusted = "AdjustFlag=2" in query_fields
            if adjusted and not query_fields.startswith("后复权"):
                query_fields = "后复权" + query_fields
            query = (
                f"查询{canonical_instrument}在{range_text}每个交易日的"
                f"{query_fields}。"
                + (
                    "复权参数AdjustFlag必须为2，不能用1或3。"
                    if adjusted else "只返回逐日数据，不返回区间汇总。"
                )
            )
        response = await self._post(
            path="/proxy/b/mcp/tool/searchData",
            payload={
                "query": query,
                "toolContext": {
                    "callId": f"indicator_history_{uuid4().hex}",
                    "userInfo": {"userId": "ashare-backtest-service"},
                },
            },
        )
        raw = response.content
        decoded = _decode_provider_response(response)
        _raise_for_provider_status(response, decoded, tool="searchData")
        tables = _finance_tables(decoded)
        points = _provider_indicator_points(
            tables=tables,
            instrument_id=canonical_instrument,
            provider_indicator_name=cleaned_provider_name,
            value_names=cleaned_value_names,
            start=start,
            end=end,
            contract=contract,
        )
        return ProviderIndicatorSeries(
            provider="eastmoney_mx_finance_data",
            instrument_id=canonical_instrument,
            indicator_id=cleaned_indicator_id,
            requested_start=start,
            requested_end=end,
            points=points,
            response_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
            retrieved_at=self._validated_retrieved_at(),
            schema_version=_INDICATOR_HISTORY_SCHEMA_VERSION,
            query=query,
        )

    async def query_condition_history(
        self,
        *,
        instrument_id: str,
        condition: object,
        start: date,
        end: date,
    ) -> ProviderIndicatorSeries:
        """Fetch the provider values required by one validated DSL condition.

        The catalog is the only translation table between DSL identifiers and
        provider-owned names.  Keeping this wrapper here avoids per-security
        or per-strategy request branches in the application layer.
        """

        from ashare_lab.domain.signals.provider_catalog import (
            provider_binding_for_condition,
        )
        from ashare_lab.domain.strategy import IndicatorCondition

        if not isinstance(condition, IndicatorCondition):
            raise TypeError("condition must be an IndicatorCondition")
        binding = provider_binding_for_condition(condition)
        return await self.query_indicator_history(
            instrument_id=instrument_id,
            indicator_id=condition.indicator_id,
            provider_indicator_name=binding.provider_indicator_name,
            value_names=binding.value_names,
            start=start,
            end=end,
        )

    async def screen_then_query_finance(
        self,
        *,
        screening_query: str,
        asset_type: str,
        indicators: str,
    ) -> LiveScreenedFinanceDataResult:
        """Screen current instruments, then query their current data in batches.

        This mirrors the installed screener/data Skills while keeping the
        provider calls auditable.  It does not create a historical snapshot and
        cannot be used as point-in-time evidence by the backtest engine.
        """

        cleaned_indicators = indicators.strip()
        if not cleaned_indicators:
            raise ValueError("indicators must not be blank")
        screen = await self.screen(query=screening_query, asset_type=asset_type)
        entities = screen_security_entities(screen)
        if not entities:
            raise MxSaasProviderNoDataError(
                "real-time screening provider selected no usable entities"
            )
        # The screener often returns current quote columns together with the
        # selected universe.  Reuse those provider-owned values when every
        # requested indicator is already present instead of issuing hundreds
        # of redundant searchData requests.  An empty ``batches`` tuple means
        # that the auditable ``screen`` response is the complete answer.
        if _screen_covers_indicators(screen.columns, cleaned_indicators):
            return LiveScreenedFinanceDataResult(
                screen=screen,
                entities=entities,
                batches=(),
            )
        if len(entities) > _MAX_SCREENED_ENTITIES:
            raise MxSaasProviderDataError(
                f"real-time screening result exceeds {_MAX_SCREENED_ENTITIES} entities"
            )

        entity_batches = tuple(
            entities[offset : offset + _DIRECT_ENTITY_LIMIT]
            for offset in range(0, len(entities), _DIRECT_ENTITY_LIMIT)
        )
        semaphore = asyncio.Semaphore(4)

        async def fetch_batch(
            entity_batch: tuple[LiveSecurityEntity, ...],
        ) -> LiveFinanceDataResult:
            query = _screened_finance_query(entity_batch, cleaned_indicators)
            async with semaphore:
                result = await self.query_finance(
                    query=query,
                    indicators=cleaned_indicators,
                )
            _verify_finance_entity_coverage(result.tables, entity_batch)
            return result

        # ``gather`` preserves input order, so provider batches remain
        # deterministic while avoiding one full network round trip per five
        # entities.  Four-way concurrency is deliberately bounded.
        batches = await asyncio.gather(*(fetch_batch(batch) for batch in entity_batches))
        return LiveScreenedFinanceDataResult(
            screen=screen,
            entities=entities,
            batches=tuple(batches),
        )

    def resolve_instrument_name(self, name: str) -> str:
        """Resolve an exact name, or retain provider identities for confirmation.

        A partial match never binds a strategy, even if only one row is found.
        Asking for containing names also gives an abbreviation its choices in
        the first lookup; a unique exact match still takes precedence.
        """

        cleaned_name = "".join(name.split())
        if not cleaned_name:
            raise LookupError("empty instrument name")
        try:
            result = asyncio.run(
                self.screen(
                    query=f"A股证券简称包含{cleaned_name}；获取证券代码和证券简称",
                    asset_type="A股",
                )
            )
        except MxSaasProviderError as exc:
            raise TimeoutError("instrument resolver is unavailable") from exc
        matches: set[str] = set()
        candidates: dict[str, InstrumentNameCandidate] = {}
        for row in result.rows:
            row_name = _first_text(row, ("证券简称", "证券名称", "股票简称", "名称"))
            if row_name is None:
                continue
            normalized_name = "".join(row_name.split())
            if (cleaned_name.casefold() not in normalized_name.casefold()
                    or len(normalized_name) > 64):
                continue
            raw_code = _first_text(row, ("证券代码", "股票代码", "代码"))
            if raw_code is None:
                continue
            try:
                symbol = str(normalize_a_share_instrument(raw_code))
            except AshareInstrumentCodeError:
                continue
            candidates[symbol] = InstrumentNameCandidate(
                symbol=symbol, name=normalized_name, source=result.provider,
                retrieved_at=result.provenance.retrieved_at,
            )
            if normalized_name.casefold() == cleaned_name.casefold():
                matches.add(symbol)
        if len(matches) != 1:
            if candidates:
                choices = tuple(sorted(candidates.values(), key=lambda item: (
                    item.symbol not in matches,
                    not item.name.casefold().startswith(cleaned_name.casefold()),
                    len(item.name), item.name, item.symbol,
                )))[:3]
                raise InstrumentNameAmbiguous(choices)
            raise LookupError("instrument name is unconfirmed or ambiguous")
        return next(iter(matches))

    async def _post(self, *, path: str, payload: Mapping[str, Any]) -> httpx.Response:
        tool: MxTool = (
            "selectSecurity" if path == "/proxy/b/mcp/tool/selectSecurity"
            else "searchData" if path == "/proxy/b/mcp/tool/searchData"
            else "unknown"
        )
        context = payload.get("toolContext")
        raw_call_id = (
            cast(Mapping[str, object], context).get("callId")
            if isinstance(context, Mapping) else None
        )
        call_id = (
            raw_call_id if isinstance(raw_call_id, str)
            and re.fullmatch(r"(?:finance|screen)_[0-9a-f]{32}", raw_call_id)
            else "unknown"
        )
        # This provider currently advertises both address families, while its
        # IPv6 edge and TLS 1.3 edge close the handshake on the supported
        # Python/OpenSSL runtime.  Keep this compatibility transport scoped to
        # MX; other application traffic retains its normal network policy.
        transport = self._transport
        if transport is None:
            tls = ssl.create_default_context()
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.maximum_version = ssl.TLSVersion.TLSv1_2
            transport = httpx.AsyncHTTPTransport(
                local_address="0.0.0.0",
                verify=tls,
            )
        async with httpx.AsyncClient(
            # Match the finance Skill's 120-second read budget by default;
            # connecting to an unavailable endpoint must still fail promptly.
            timeout=httpx.Timeout(
                self._timeout_seconds, connect=min(10.0, self._timeout_seconds)
            ),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    response = await client.post(
                        f"{self._base_url}{path}",
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "em_api_key": self._api_key,
                        },
                    )
                except httpx.TransportError as exc:
                    transport_kind = _safe_transport_kind(exc)
                    _LOGGER.warning(
                        "MX request interrupted: tool=%s call_id=%s attempt=%s/%s kind=%s retry=%s",
                        tool, call_id, attempt, self._max_attempts, transport_kind,
                        attempt < self._max_attempts,
                    )
                    if attempt < self._max_attempts:
                        _notify_retry_progress(MxRetryProgress(
                            tool, call_id, attempt, self._max_attempts - 1,
                        ))
                        await self._sleep_before_retry(attempt)
                        continue
                    raise MxSaasProviderUnavailableError(
                        "real-time market-data provider is unavailable",
                        tool=tool,
                        reason=(
                            "read_timeout" if isinstance(exc, httpx.ReadTimeout)
                            else "connect_timeout" if isinstance(exc, httpx.ConnectTimeout)
                            else "transport_error"
                        ),
                        transport_kind=transport_kind, attempts=attempt, call_id=call_id,
                    ) from exc
                if _is_retryable_http_status(response.status_code):
                    _LOGGER.warning(
                        "MX request unavailable: tool=%s call_id=%s attempt=%s/%s "
                        "status=%s retry=%s",
                        tool, call_id, attempt, self._max_attempts, response.status_code,
                        attempt < self._max_attempts,
                    )
                    if attempt < self._max_attempts:
                        _notify_retry_progress(MxRetryProgress(
                            tool, call_id, attempt, self._max_attempts - 1,
                        ))
                        await self._sleep_before_retry(attempt)
                        continue
                    raise MxSaasProviderUnavailableError(
                        "real-time market-data provider is unavailable",
                        tool=tool, reason="http_error", http_status=response.status_code,
                        attempts=attempt, call_id=call_id,
                    )
                # Classify the HTTP status before parsing a body: gateways may
                # return HTML/plain text for authorization and service errors.
                if response.status_code in {401, 403}:
                    raise MxSaasProviderAuthError(
                        "real-time market-data provider rejected its credential",
                        tool=tool, reason="http_error", http_status=response.status_code,
                        attempts=attempt, call_id=call_id,
                    )
                if response.is_error:
                    raise MxSaasProviderUnavailableError(
                        "real-time market-data provider returned an error",
                        tool=tool, reason="http_error", http_status=response.status_code,
                        attempts=attempt, call_id=call_id,
                    )
                if attempt > 1:
                    _notify_retry_progress(MxRetryProgress(
                        tool, call_id, attempt - 1, self._max_attempts - 1, recovered=True,
                    ))
                    _LOGGER.info(
                        "MX request recovered: tool=%s call_id=%s attempt=%s/%s",
                        tool, call_id, attempt, self._max_attempts,
                    )
                return response
        raise AssertionError("network retry loop exited unexpectedly")

    async def _sleep_before_retry(self, attempt: int) -> None:
        delay = min(
            _RETRY_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
            _RETRY_MAX_BACKOFF_SECONDS,
        )
        await self._sleeper(delay)

    def _validated_retrieved_at(self) -> datetime:
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise MxSaasProviderDataError("real-time screening clock must include a timezone")
        return retrieved_at.astimezone(UTC)


def _safe_transport_kind(exc: httpx.TransportError) -> str:
    # Only known library labels reach diagnostics; never provider exception
    # text, URLs, request bodies or credentials (including in custom subclasses).
    for error_type in (
        httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
        httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.CloseError,
        httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ProxyError,
        httpx.UnsupportedProtocol,
    ):
        if type(exc) is error_type:
            return error_type.__name__
    return "TransportError"


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_HTTP_STATUS_CODES or 500 <= status_code <= 599


def _result_node(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = payload.get("data")
    containers = (
        (cast(Mapping[str, Any], data), payload) if isinstance(data, Mapping) else (payload,)
    )
    for container in containers:
        if "allResults" not in container:
            continue
        all_results = container.get("allResults")
        if all_results is None:
            return None
        if not isinstance(all_results, Mapping):
            raise MxSaasProviderDataError(
                "real-time screening provider returned an invalid result set"
            )
        result = cast(Mapping[str, Any], all_results).get("result")
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise MxSaasProviderDataError(
                "real-time screening provider returned an invalid result set"
            )
        return cast(Mapping[str, Any], result)
    return None


def _screen_provider_metadata(
    payload: Mapping[str, Any], result: Mapping[str, Any] | None,
) -> Mapping[str, object]:
    """Keep provider-reported scope and column semantics, never the input/echoed query."""
    raw_data = payload.get("data")
    data = cast(Mapping[str, Any], raw_data) if isinstance(raw_data, Mapping) else payload
    metadata = _screen_condition_metadata(data)
    all_results = data.get("allResults")
    if isinstance(all_results, Mapping):
        conditions = _screen_condition_metadata(cast(Mapping[str, Any], all_results))
        if conditions:
            metadata["allResults"] = conditions
    if result is not None:
        raw_columns = result.get("columns")
        if isinstance(raw_columns, list):
            metadata["columns"] = [
                _metadata_scalars(cast(Mapping[str, Any], column), (
                    "displayName", "title", "label", "field", "name", "key", "indexName",
                    "dateMsg", "sortWay", "unit", "sortable", "userNeed",
                ))
                for column in cast(list[Any], raw_columns) if isinstance(column, Mapping)
            ]
    return metadata


def _screen_condition_metadata(source: Mapping[str, Any]) -> dict[str, object]:
    metadata = _metadata_scalars(source, ("selectType", "market"))
    conditions = source.get("responseConditionList")
    if isinstance(conditions, list):
        metadata["responseConditionList"] = [
            _metadata_scalars(cast(Mapping[str, Any], item), ("describe", "stockCount"))
            for item in cast(list[Any], conditions) if isinstance(item, Mapping)
        ]
    total = source.get("totalCondition")
    if isinstance(total, Mapping):
        metadata["totalCondition"] = _metadata_scalars(
            cast(Mapping[str, Any], total), ("describe", "stockCount"),
        )
    elif isinstance(total, str):
        metadata["totalCondition"] = total
    return metadata


def _metadata_scalars(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, object]:
    return {
        key: source[key] for key in fields if key in source
        and (source[key] is None or isinstance(source[key], str | int | float | bool))
    }


def _decode_provider_response(response: httpx.Response) -> Mapping[str, Any]:
    try:
        decoded = response.json()
    except json.JSONDecodeError as exc:
        raise MxSaasProviderDataError("real-time market-data provider returned non-JSON") from exc
    if not isinstance(decoded, Mapping):
        raise MxSaasProviderDataError("real-time market-data provider returned an invalid payload")
    return cast(Mapping[str, Any], decoded)


def _raise_for_provider_status(
    response: httpx.Response, decoded: Mapping[str, Any], *, tool: MxTool = "unknown"
) -> None:
    code = decoded.get("code")
    status = decoded.get("status")
    if (
        response.status_code in {401, 403}
        or code in _AUTH_STATUS_VALUES
        or status in _AUTH_STATUS_VALUES
    ):
        raise MxSaasProviderAuthError(
            "real-time market-data provider rejected its credential", tool=tool,
        )
    if response.is_error:
        raise MxSaasProviderUnavailableError("real-time market-data provider returned an error")
    if code not in _SUCCESS_STATUS_VALUES or status not in _SUCCESS_STATUS_VALUES:
        raise MxSaasProviderDataError("real-time market-data provider rejected the request")
    if decoded.get("success") is False:
        raise MxSaasProviderDataError("real-time market-data provider rejected the request")
    data = decoded.get("data")
    if isinstance(data, Mapping) and cast(Mapping[str, Any], data).get("success") is False:
        raise MxSaasProviderDataError("real-time market-data provider rejected the request")
    message = _provider_message(decoded)
    if message is not None and any(marker in message for marker in _PARTIAL_RESULT_MARKERS):
        raise MxSaasProviderDataError(
            "real-time market-data provider returned a partial business result"
        )


def _finance_tables(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw_tables: object = payload.get("dataTableDTOList")
    data = payload.get("data")
    if not isinstance(raw_tables, list) and isinstance(data, Mapping):
        data = cast(Mapping[str, Any], data)
        result = data.get("searchDataResultDTO")
        if isinstance(result, Mapping):
            raw_tables = cast(Mapping[str, Any], result).get("dataTableDTOList")
        if not isinstance(raw_tables, list):
            raw_tables = data.get("dataTableDTOList")
    if not isinstance(raw_tables, list):
        raise MxSaasProviderDataError("real-time financial provider omitted its tables")
    raw_tables = cast(list[Any], raw_tables)
    tables: list[Mapping[str, Any]] = []
    for table in raw_tables:
        if not isinstance(table, Mapping):
            raise MxSaasProviderDataError("real-time financial provider returned an invalid table")
        tables.append(cast(Mapping[str, Any], table))
    return tuple(tables)


def _provider_indicator_points(
    *,
    tables: tuple[Mapping[str, Any], ...],
    instrument_id: str,
    provider_indicator_name: str,
    value_names: tuple[str, ...],
    start: date,
    end: date,
    contract: MxIndicatorFieldContract | None = None,
) -> tuple[ProviderIndicatorPoint, ...]:
    matching_tables = tuple(
        table
        for table in tables
        if instrument_id[:6] in _explicit_entity_codes(table)
        or instrument_id[:6] in _table_entity_code_values(table)
    )
    table = _unique_longest_historical_table(matching_tables)
    if table is None:
        raise MxSaasProviderDataError(
            "historical indicator response does not bind exactly one requested security"
        )
    raw_table = table.get("rawTable")
    if not isinstance(raw_table, Mapping):
        raise MxSaasProviderDataError("historical indicator response omitted rawTable")
    raw_table = cast(Mapping[str, Any], raw_table)
    raw_dates = raw_table.get("headName")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise MxSaasProviderDataError("historical indicator response omitted session dates")
    dates = tuple(_provider_session_date(value) for value in cast(list[Any], raw_dates))
    if len(dates) != len(set(dates)):
        raise MxSaasProviderDataError("historical indicator response contains duplicate dates")
    if contract is not None and any(value < start or value > end for value in dates):
        raise MxSaasProviderDataError("historical indicator response has a different date window")

    field_metadata = _provider_field_metadata(table)
    # Provider field labels normally omit the parameter tuple even when the
    # request includes it (for example ``KDJ(9,3,3)`` -> ``KDJ K值``).
    indicator_token = _provider_field_token(provider_indicator_name.split("(", 1)[0])
    field_definitions: dict[str, Mapping[str, Any]] = {}
    for raw_field in table.get("fieldSet", []):
        if isinstance(raw_field, Mapping):
            definition = cast(Mapping[str, Any], raw_field)
            code = definition.get("returnCode")
            if isinstance(code, str):
                field_definitions[code] = definition
    bindings: list[tuple[str, str, str, str | None]] = []
    used_codes: set[str] = set()
    for requested_name in value_names:
        requested_token = _provider_field_token(requested_name)
        candidates = tuple(
            (field_code, field_name, unit)
            for field_code, (field_name, aliases, unit) in field_metadata.items()
            if field_code in raw_table
            and field_code not in used_codes
            and (
                contract.bind_field(requested_name, field_definitions.get(field_code, {}))
                if contract is not None
                else _provider_field_matches(
                    requested_token=requested_token,
                    indicator_token=indicator_token,
                    aliases=aliases,
                )
            )
        )
        if len(candidates) != 1:
            raise MxSaasProviderDataError(
                f"historical indicator field {requested_name!r} is missing or ambiguous"
            )
        binding = candidates[0]
        used_codes.add(binding[0])
        bindings.append((requested_name, *binding))

    points_by_date: dict[date, ProviderIndicatorPoint] = {}
    for row_index, session_date in enumerate(dates):
        if session_date < start or session_date > end:
            continue
        values: list[ProviderIndicatorValue] = []
        for requested_name, field_code, source_field_name, source_unit in bindings:
            raw_values = raw_table.get(field_code)
            if not isinstance(raw_values, list):
                raise MxSaasProviderDataError(
                    "historical indicator value count does not match session dates"
                )
            typed_raw_values = cast(list[object], raw_values)
            if len(typed_raw_values) != len(dates):
                raise MxSaasProviderDataError(
                    "historical indicator value count does not match session dates"
                )
            value = _provider_decimal(typed_raw_values[row_index])
            source_parameters = field_definitions[field_code].get("fixedParamValue")
            values.append(
                ProviderIndicatorValue(
                    field_code=field_code,
                    field_name=requested_name,
                    value=value,
                    unit="%" if source_unit == "100%" else source_unit,
                    source_field_name=source_field_name,
                    source_unit=source_unit,
                    source_parameters=(
                        source_parameters if isinstance(source_parameters, str) else None
                    ),
                )
            )
        observed_at = datetime.combine(session_date, time(15, 0), tzinfo=_SHANGHAI)
        points_by_date[session_date] = ProviderIndicatorPoint(
            session_date=session_date,
            observed_at=observed_at,
            first_available_at=observed_at,
            values=tuple(values),
        )
    if not points_by_date:
        raise MxSaasProviderNoDataError(
            "historical indicator provider returned no values in requested range"
        )
    return tuple(points_by_date[item] for item in sorted(points_by_date))


def _unique_longest_historical_table(
    tables: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any] | None:
    """Select/merge the single full historical response for one security.

    MX often returns a short stub plus the real historical table.  It can also
    return the requested provider values split across several tables with the
    same date axis, for example close price in one table and MA20 in another.
    Merging is allowed only when the longest same-security tables share one
    identical date axis; ambiguous equal-length date axes still fail closed.
    """

    if len(tables) == 1:
        return tables[0]
    dated: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for table in tables:
        raw_table = table.get("rawTable")
        raw_dates = (
            cast(Mapping[str, object], raw_table).get("headName")
            if isinstance(raw_table, Mapping)
            else None
        )
        if isinstance(raw_dates, list) and raw_dates:
            date_axis = tuple(str(value) for value in cast(list[object], raw_dates))
            dated.setdefault(date_axis, []).append(table)
    if not dated:
        return None
    longest = max(len(date_axis) for date_axis in dated)
    winners = [
        (date_axis, group)
        for date_axis, group in dated.items()
        if len(date_axis) == longest
    ]
    if len(winners) != 1:
        return None
    date_axis, group = winners[0]
    if len(group) == 1:
        return group[0]
    return _merge_historical_tables(group, date_axis)


def _merge_historical_tables(
    tables: list[Mapping[str, Any]],
    date_axis: tuple[str, ...],
) -> Mapping[str, Any] | None:
    merged: dict[str, Any] = dict(tables[0])
    merged_raw: dict[str, Any] = {"headName": list(date_axis)}
    merged_names: dict[str, Any] = {}
    merged_fields: list[Any] = []
    seen_field_codes: set[str] = set()

    for table in tables:
        raw_table = table.get("rawTable")
        if not isinstance(raw_table, Mapping):
            return None
        raw_table = cast(Mapping[str, Any], raw_table)
        if tuple(str(value) for value in raw_table.get("headName", ())) != date_axis:
            return None
        for key, value in raw_table.items():
            if key == "headName":
                continue
            if key in merged_raw and merged_raw[key] != value:
                return None
            merged_raw[key] = value

        name_map = table.get("nameMap")
        if isinstance(name_map, Mapping):
            for key, value in cast(Mapping[str, Any], name_map).items():
                if key in merged_names and merged_names[key] != value:
                    return None
                merged_names[key] = value

        field_set = table.get("fieldSet")
        if isinstance(field_set, list):
            for raw_field in cast(list[Any], field_set):
                if not isinstance(raw_field, Mapping):
                    continue
                raw_field = cast(Mapping[str, Any], raw_field)
                code = raw_field.get("returnCode")
                if not isinstance(code, str) or not code.strip():
                    continue
                if code in seen_field_codes:
                    continue
                seen_field_codes.add(code)
                merged_fields.append(raw_field)

    merged["rawTable"] = merged_raw
    merged["nameMap"] = merged_names
    merged["fieldSet"] = merged_fields
    return merged


def _provider_field_metadata(
    table: Mapping[str, Any],
) -> Mapping[str, tuple[str, frozenset[str], str | None]]:
    name_map = table.get("nameMap")
    names: Mapping[str, object] = (
        cast(Mapping[str, object], name_map) if isinstance(name_map, Mapping) else {}
    )
    raw_field_set = table.get("fieldSet")
    field_set = cast(list[Any], raw_field_set) if isinstance(raw_field_set, list) else []
    metadata: dict[str, tuple[str, frozenset[str], str | None]] = {}
    for raw_field in field_set:
        if not isinstance(raw_field, Mapping):
            continue
        field = cast(Mapping[str, Any], raw_field)
        code = field.get("returnCode")
        if not isinstance(code, str) or not code.strip():
            continue
        raw_labels = (
            names.get(code),
            field.get("returnName"),
            field.get("returnSourceName"),
            field.get("returnSourceCode"),
        )
        labels = tuple(
            value.strip() for value in raw_labels if isinstance(value, str) and value.strip()
        )
        if not labels:
            continue
        unit_value = field.get("unitName") or field.get("unitDesc")
        unit = unit_value.strip() if isinstance(unit_value, str) and unit_value.strip() else None
        metadata[code] = (
            labels[0],
            frozenset(_provider_field_token(label) for label in labels),
            unit,
        )
    return metadata


def _provider_field_matches(
    *,
    requested_token: str,
    indicator_token: str,
    aliases: frozenset[str],
) -> bool:
    accepted = {
        requested_token,
        indicator_token + requested_token,
        requested_token + indicator_token,
    }
    return bool(accepted & aliases)


def _provider_field_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if character.isalnum() or character in "+-"
    )


def _provider_session_date(value: object) -> date:
    if not isinstance(value, str):
        raise MxSaasProviderDataError("historical indicator session date is invalid")
    value = value.strip()
    # A range summary such as '2026-08-24至2026-09-04' is not a daily observation.
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?",
        value,
    ) is None:
        raise MxSaasProviderDataError("historical indicator response is not a daily date axis")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise MxSaasProviderDataError("historical indicator session date is invalid") from exc


def _provider_decimal(value: object) -> Decimal:
    if value is None:
        raise MxSaasProviderDataError("historical indicator value is missing")
    if isinstance(value, bool):
        return Decimal(1 if value else 0)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        boolean_values = {
            "是": Decimal(1),
            "否": Decimal(0),
            "true": Decimal(1),
            "false": Decimal(0),
            "yes": Decimal(1),
            "no": Decimal(0),
        }
        if normalized in boolean_values:
            return boolean_values[normalized]
    try:
        numeric = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MxSaasProviderDataError("historical indicator value is not numeric") from exc
    if not numeric.is_finite():
        raise MxSaasProviderDataError("historical indicator value is not finite")
    return numeric


def _column_metadata(raw_columns: object) -> tuple[tuple[str, ...], Mapping[str, str]]:
    if not isinstance(raw_columns, list):
        raise MxSaasProviderDataError("real-time screening provider omitted its columns")
    raw_columns = cast(list[Any], raw_columns)
    labels: list[str] = []
    field_to_label: dict[str, str] = {}
    for item in raw_columns:
        if not isinstance(item, Mapping):
            raise MxSaasProviderDataError("real-time screening provider has an invalid column")
        item = cast(Mapping[str, Any], item)
        raw_label = item.get("displayName") or item.get("title") or item.get("label")
        if not isinstance(raw_label, str) or not raw_label.strip():
            raise MxSaasProviderDataError("real-time screening provider has an unnamed column")
        source_key = item.get("field") or item.get("name") or item.get("key")
        if not isinstance(source_key, str) or not source_key.strip():
            raise MxSaasProviderDataError("real-time screening provider has an unbound column")
        label = raw_label.strip()
        date_message = item.get("dateMsg")
        if isinstance(date_message, str) and date_message.strip():
            label = f"{label} {date_message.strip()}"
        labels.append(label)
        field_to_label[source_key] = label
    return tuple(labels), field_to_label


def _normalise_rows(
    raw_rows: object,
    columns: tuple[str, ...],
    field_to_label: Mapping[str, str],
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw_rows, list):
        raise MxSaasProviderDataError("real-time screening provider omitted its rows")
    raw_rows = cast(list[Any], raw_rows)
    normalised: list[Mapping[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, Mapping):
            raise MxSaasProviderDataError("real-time screening provider has an invalid row")
        item = cast(Mapping[str, Any], item)
        row: dict[str, Any] = {}
        for source_key, value in item.items():
            label = field_to_label.get(source_key)
            if label is not None:
                row[label] = value
        normalised.append(row)
    # A provider can legitimately return an empty result set, but non-empty
    # rows without declared columns would make their meaning unknowable.
    if normalised and not columns:
        raise MxSaasProviderDataError("real-time screening provider returned rows without columns")
    return tuple(normalised)


def _provider_message(payload: Mapping[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, Mapping):
        message = cast(Mapping[str, Any], data).get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = payload.get("message")
    if isinstance(message, str) and message.strip() not in {
        "成功",
        "ok",
        "OK",
        "success",
        "Success",
    }:
        return message.strip()
    return None


def _partial_result_rows(payload: Mapping[str, Any]) -> tuple[Mapping[str, str], ...]:
    data = payload.get("data")
    containers = (
        (cast(Mapping[str, Any], data), payload) if isinstance(data, Mapping) else (payload,)
    )
    raw_table: object = None
    for container in containers:
        candidate = container.get("partialResults")
        if isinstance(candidate, str) and candidate.strip():
            raw_table = candidate
            break
    if raw_table is None:
        return ()
    lines = [line.strip() for line in raw_table.splitlines() if line.strip()]
    if len(lines) < 2:
        raise MxSaasProviderDataError(
            "real-time screening provider returned an invalid partial result"
        )
    headers = _markdown_cells(lines[0])
    delimiters = _markdown_cells(lines[1])
    if (
        not headers
        or len(headers) != len(delimiters)
        or len(set(headers)) != len(headers)
        or any(not header for header in headers)
        or any(_MARKDOWN_DELIMITER_RE.fullmatch(cell) is None for cell in delimiters)
    ):
        raise MxSaasProviderDataError(
            "real-time screening provider returned an invalid partial result"
        )
    rows: list[Mapping[str, str]] = []
    for line in lines[2:]:
        cells = _markdown_cells(line)
        if len(cells) != len(headers):
            raise MxSaasProviderDataError(
                "real-time screening provider returned an invalid partial result"
            )
        rows.append(dict(zip(headers, cells, strict=True)))
    return tuple(rows)


def _markdown_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return tuple(cell.strip() for cell in stripped.split("|"))


def _verify_finance_entity_coverage(
    tables: tuple[Mapping[str, Any], ...],
    expected_entities: tuple[LiveSecurityEntity, ...],
) -> None:
    expected_codes = {
        code
        for entity in expected_entities
        if (code := _normalise_provider_entity_code(entity.code)) is not None
    }
    returned_codes: set[str] = set()
    for table in tables:
        returned_codes.update(_explicit_entity_codes(table))
        returned_codes.update(_table_entity_code_values(table))
    if returned_codes != expected_codes:
        raise MxSaasProviderDataError(
            "real-time financial provider entity coverage cannot be proven"
        )


def _explicit_entity_codes(table: Mapping[str, Any]) -> set[str]:
    codes: set[str] = set()
    casefolded: dict[str, object] = {
        str(key).casefold(): value for key, value in table.items()
    }
    for key in _ENTITY_CODE_KEYS:
        code = _normalise_provider_entity_code(casefolded.get(key.casefold()))
        if code is not None:
            codes.add(code)
    for key in _ENTITY_CODE_LIST_KEYS:
        values = casefolded.get(key.casefold())
        if isinstance(values, (list, tuple)):
            for value in cast(Sequence[object], values):
                code = _normalise_provider_entity_code(value)
                if code is not None:
                    codes.add(code)
    for key in (
        "entityName2TagMap",
        "entityTagDTO",
        "entityTagDTOList",
        "entityTags",
        "entityTagListMap",
    ):
        value = casefolded.get(key.casefold())
        if value is not None:
            codes.update(_entity_tag_codes(value))
    return codes


def _entity_tag_codes(node: object) -> set[str]:
    codes: set[str] = set()
    if isinstance(node, Mapping):
        mapping = cast(Mapping[object, object], node)
        for key, value in mapping.items():
            if str(key).casefold() in {item.casefold() for item in _ENTITY_CODE_KEYS}:
                code = _normalise_provider_entity_code(value)
                if code is not None:
                    codes.add(code)
            elif isinstance(value, (Mapping, list, tuple)):
                codes.update(_entity_tag_codes(cast(object, value)))
    elif isinstance(node, (list, tuple)):
        for value in cast(Sequence[object], node):
            codes.update(_entity_tag_codes(value))
    return codes


def _table_entity_code_values(table: Mapping[str, Any]) -> set[str]:
    codes: set[str] = set()
    for payload_key in ("rawTable", "table"):
        payload = table.get(payload_key)
        if not isinstance(payload, Mapping):
            continue
        payload = cast(Mapping[str, Any], payload)
        raw_columns = payload.get("headers") or payload.get("columns") or payload.get("fieldnames")
        raw_rows = payload.get("data") or payload.get("rows") or payload.get("dataList")
        if not isinstance(raw_columns, list) or not isinstance(raw_rows, list):
            continue
        column_bindings = _entity_code_column_bindings(cast(list[Any], raw_columns))
        for row in cast(list[object], raw_rows):
            if isinstance(row, Mapping):
                row = cast(Mapping[object, object], row)
                for _, keys in column_bindings:
                    for key in keys:
                        if key in row:
                            code = _normalise_provider_entity_code(row[key])
                            if code is not None:
                                codes.add(code)
                            break
            elif isinstance(row, (list, tuple)):
                sequence_row = cast(Sequence[object], row)
                for index, _ in column_bindings:
                    if index < len(sequence_row):
                        code = _normalise_provider_entity_code(sequence_row[index])
                        if code is not None:
                            codes.add(code)
    return codes


def _entity_code_column_bindings(
    columns: list[Any],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    bindings: list[tuple[int, tuple[str, ...]]] = []
    for index, column in enumerate(columns):
        if isinstance(column, str):
            if _is_entity_code_header(column):
                bindings.append((index, (column,)))
            continue
        if not isinstance(column, Mapping):
            continue
        column = cast(Mapping[str, Any], column)
        keys = tuple(
            value.strip()
            for value in (
                column.get("field"),
                column.get("name"),
                column.get("key"),
                column.get("displayName"),
                column.get("title"),
                column.get("label"),
            )
            if isinstance(value, str) and value.strip()
        )
        if any(_is_entity_code_header(value) for value in keys):
            bindings.append((index, keys))
    return tuple(bindings)


def _is_entity_code_header(value: str) -> bool:
    return "".join(value.split()).casefold() in _ENTITY_CODE_HEADERS


def _normalise_provider_entity_code(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0 or value > 999999:
            return None
        text = f"{value:06d}"
    elif isinstance(value, str):
        text = value.strip().upper()
    else:
        return None
    if _SECURITY_CODE_RE.fullmatch(text) is None:
        return None
    return text[:6]


def _first_text(row: Mapping[str, Any], labels: tuple[str, ...]) -> str | None:
    for label in labels:
        value = row.get(label)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return None


def _query_with_indicator_hint(query: str, indicators: str | None) -> str:
    if not indicators or indicators.casefold() in query.casefold():
        return query
    return f"{query.rstrip(' ，,。；;')}；获取{indicators}"


def _screen_covers_indicators(columns: tuple[str, ...], indicators: str) -> bool:
    requested = tuple(
        _normalise_indicator_label(term)
        for term in _INDICATOR_SEPARATOR_RE.split(indicators)
        if term.strip()
    )
    if not requested:
        return False
    available = {_normalise_indicator_label(column) for column in columns}
    return all(indicator in available for indicator in requested)


def _normalise_indicator_label(value: str) -> str:
    label = "".join(value.split()).casefold()
    for suffix in _DISPLAY_UNIT_SUFFIXES:
        if label.endswith(suffix.casefold()):
            return label[: -len(suffix)]
    return label


def screen_security_entities(result: LiveMarketDataResult) -> tuple[LiveSecurityEntity, ...]:
    entities: list[LiveSecurityEntity] = []
    seen_codes: set[str] = set()
    for row in result.rows:
        code = _first_text(row, ("证券代码", "股票代码", "基金代码", "代码"))
        if code is None:
            continue
        normalized_code = code.strip().upper()
        if _SECURITY_CODE_RE.fullmatch(normalized_code) is None:
            continue
        if normalized_code in seen_codes:
            continue
        name = _first_text(row, ("证券简称", "证券名称", "股票简称", "基金简称", "名称"))
        seen_codes.add(normalized_code)
        entities.append(
            LiveSecurityEntity(
                code=normalized_code,
                name=name,
                asset_type=result.asset_type,
            )
        )
    return tuple(entities)


def _screened_finance_query(
    entities: tuple[LiveSecurityEntity, ...],
    indicators: str,
) -> str:
    labels = [
        f"{entity.name}({entity.code})" if entity.name else entity.code for entity in entities
    ]
    return f"查询{'、'.join(labels)}；获取{indicators}"
