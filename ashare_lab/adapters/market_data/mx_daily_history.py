"""Lightweight Eastmoney MX daily history for research-return backtests.

This module deliberately does not implement ``MarketDataRepository``.  It keeps
the provider's unadjusted execution reference and provider-declared
back-adjusted research prices side by side, without creating corporate actions
or routing through another market-data source.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from ashare_lab.domain.market_data import (
    AshareInstrumentCodeError,
    Board,
    TradingStatus,
    normalize_a_share_instrument,
)
from ashare_lab.domain.shared import DomainValidationError
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataResult

from .mx_saas import MxSaasMarketDataClient, MxSaasProviderNoDataError

MX_DAILY_HISTORY_PROVIDER = "eastmoney_mx_finance_data"
MX_BACK_ADJUSTMENT = "provider_declared_back_adjusted"
MX_DAILY_HISTORY_CACHE_SCHEMA = "ashare-lab.mx-daily-history-cache.v1"
_MAX_HISTORY_CHUNK_DAYS = 730

PERSISTENT_SKILL_INSTRUMENTS = frozenset(
    {
        "300059.SZ",
        "600519.SH",
        "000001.SZ",
        "688981.SH",
        "002594.SZ",
    }
)
_RAW_FIELDS = ("开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额")
_SESSION_FIELDS = ("前收盘价", "交易状态", "是否为ST股票")
_LIMIT_FIELDS = ("涨停价", "跌停价")
_PROVIDER_FIELD_ALIASES = {
    "收盘价(不前推)": "收盘价",
    "收盘价（不前推）": "收盘价",
}
_TRADING_STATUSES = {
    "正常交易": TradingStatus.TRADING,
    "复牌": TradingStatus.TRADING,
    "连续停牌": TradingStatus.SUSPENDED,
}
_ST_STATUSES = {"否": False, "是": True}
_MISSING_VALUES = {"", "-", "--", "null", "none"}
_EXCHANGE_NAMES = {"SH": "上海证券交易所", "SZ": "深圳证券交易所"}


class MxDailyHistoryError(RuntimeError):
    """MX did not satisfy the strict daily-history contract."""


class MxDailyHistoryCacheMissError(MxDailyHistoryError):
    """No persisted history exists and no live MX client is configured."""


class MxDailyHistoryFieldsMissingError(MxDailyHistoryError):
    """Only requested, canonical field names are safe to expose to callers."""

    def __init__(
        self, fields: tuple[str, ...], *, start: date | None = None, end: date | None = None,
    ) -> None:
        if (start is None) != (end is None) or (
            start is not None and end is not None
            and (type(start) is not date or type(end) is not date or start > end)
        ):
            raise ValueError("missing-field context requires an ordered date pair")
        self.fields = fields
        # The failed fetch range may include warmup or be one chunk of a long
        # request. It is not a substitute for the user's requested backtest range.
        self.start = start
        self.end = end
        super().__init__("MX history response omitted fields: " + ", ".join(fields))


@dataclass(frozen=True, slots=True)
class MxQueryEvidence:
    purpose: str
    provider: str
    query: str
    retrieved_at: datetime
    response_sha256: str
    row_count: int

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.purpose, self.provider, self.query)):
            raise MxDailyHistoryError("MX query evidence text must not be blank")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise MxDailyHistoryError("MX query evidence time must be timezone-aware")
        if not self.response_sha256.startswith("sha256:"):
            raise MxDailyHistoryError("MX query evidence must retain the provider response hash")
        if type(self.row_count) is not int or self.row_count < 0:
            raise MxDailyHistoryError("MX query evidence row count must be non-negative")


@dataclass(frozen=True, slots=True)
class MxDailyRow:
    session_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    raw_preclose: Decimal
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    volume: int
    amount: Decimal
    trading_status: TradingStatus
    is_st: bool
    upper_limit: Decimal | None
    lower_limit: Decimal | None
    limit_source: Literal["eastmoney_mx_finance_data", "not_applicable_suspended"]

    def __post_init__(self) -> None:
        raw = (self.raw_open, self.raw_high, self.raw_low, self.raw_close)
        adjusted = (
            self.adjusted_open,
            self.adjusted_high,
            self.adjusted_low,
            self.adjusted_close,
        )
        if any(not value.is_finite() or value <= 0 for value in (*raw, *adjusted)):
            raise MxDailyHistoryError("MX daily OHLC values must be finite and positive")
        if self.raw_low > min(raw) or self.raw_high < max(raw):
            raise MxDailyHistoryError("MX raw OHLC values are inconsistent")
        if self.adjusted_low > min(adjusted) or self.adjusted_high < max(adjusted):
            raise MxDailyHistoryError("MX adjusted OHLC values are inconsistent")
        if not self.raw_preclose.is_finite() or self.raw_preclose <= 0:
            raise MxDailyHistoryError("MX previous close must be finite and positive")
        if type(self.volume) is not int or self.volume < 0:
            raise MxDailyHistoryError("MX volume must be a non-negative integer")
        if not self.amount.is_finite() or self.amount < 0:
            raise MxDailyHistoryError("MX amount must be finite and non-negative")
        if type(self.trading_status) is not TradingStatus:
            raise MxDailyHistoryError("MX trading status must be canonical")
        if type(self.is_st) is not bool:
            raise MxDailyHistoryError("MX ST flag must be boolean")
        if (self.upper_limit is None) != (self.lower_limit is None):
            raise MxDailyHistoryError("MX upper and lower limits must be present together")
        if self.upper_limit is None:
            if self.trading_status is not TradingStatus.SUSPENDED:
                raise MxDailyHistoryError("trading sessions require provider limit prices")
            if self.limit_source != "not_applicable_suspended":
                raise MxDailyHistoryError("missing limits are allowed only for suspended sessions")
        else:
            assert self.lower_limit is not None
            if (
                not self.upper_limit.is_finite()
                or not self.lower_limit.is_finite()
                or self.lower_limit <= 0
                or self.lower_limit >= self.upper_limit
            ):
                raise MxDailyHistoryError("MX provider limit prices are invalid")
            if self.limit_source != MX_DAILY_HISTORY_PROVIDER:
                raise MxDailyHistoryError("provider limit prices require MX provenance")
        if self.trading_status is TradingStatus.SUSPENDED and (
            self.volume != 0 or self.amount != 0
        ):
            raise MxDailyHistoryError("suspended MX sessions must have zero normalized liquidity")


@dataclass(frozen=True, slots=True)
class MxDailyHistory:
    instrument_id: str
    board: Board
    listing_date: date
    start: date
    end: date
    retrieved_at: datetime
    provider: Literal["eastmoney_mx_finance_data"]
    rows: tuple[MxDailyRow, ...]
    query_evidence: tuple[MxQueryEvidence, ...]
    adjustment: Literal["provider_declared_back_adjusted"] = MX_BACK_ADJUSTMENT
    cache_status: Literal["memory", "disk", "live", "forced"] | None = None

    def __post_init__(self) -> None:
        canonical = _canonical_symbol(self.instrument_id)
        if canonical != self.instrument_id:
            raise MxDailyHistoryError("MX daily history instrument must be canonical")
        if type(self.board) is not Board:
            raise MxDailyHistoryError("MX daily history board must be canonical")
        if self.start > self.end or self.listing_date > self.start:
            raise MxDailyHistoryError("MX daily history dates are invalid")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise MxDailyHistoryError("MX daily history retrieval time must be timezone-aware")
        if self.provider != MX_DAILY_HISTORY_PROVIDER:
            raise MxDailyHistoryError("MX daily history provider is invalid")
        if self.adjustment != MX_BACK_ADJUSTMENT:
            raise MxDailyHistoryError("MX daily history adjustment declaration is invalid")
        if not self.rows:
            raise MxDailyHistoryError("MX daily history must contain rows")
        dates = tuple(row.session_date for row in self.rows)
        if dates != tuple(sorted(set(dates))):
            raise MxDailyHistoryError("MX daily rows must be unique and strictly ordered")
        if any(not self.start <= value <= self.end for value in dates):
            raise MxDailyHistoryError("MX daily row is outside the requested range")
        if not self.query_evidence:
            raise MxDailyHistoryError("MX daily history requires query evidence")
        if self.cache_status not in (None, "memory", "disk", "live", "forced"):
            raise MxDailyHistoryError("MX daily history cache status is invalid")

    @property
    def returned_start(self) -> date:
        return self.rows[0].session_date

    @property
    def returned_end(self) -> date:
        return self.rows[-1].session_date


class _MxClient(Protocol):
    async def screen(self, *, query: str, asset_type: str) -> Any: ...

    async def query_finance(
        self, *, query: str, indicators: str | None
    ) -> LiveFinanceDataResult: ...


class MxDailyHistoryClient:
    """Load strict MX-only history, persisting only the five MVP securities."""

    def __init__(
        self,
        client: MxSaasMarketDataClient | _MxClient | None,
        cache_root: Path,
        *,
        memory_max_entries: int = 128,
    ) -> None:
        if type(memory_max_entries) is not int or memory_max_entries < 1:
            raise ValueError("MX daily history memory cache size must be positive")
        self._client = client
        self._cache_root = cache_root.expanduser().resolve()
        self._memory_max_entries = memory_max_entries
        self._memory: OrderedDict[tuple[str, date, date], MxDailyHistory] = OrderedDict()
        self._inflight: dict[tuple[str, date, date], Future[MxDailyHistory]] = {}
        self._gate = threading.RLock()

    async def load(
        self,
        instrument_id: str,
        start: date,
        end: date,
        *,
        force_refresh: bool = False,
    ) -> MxDailyHistory:
        canonical = _canonical_symbol(instrument_id)
        if type(start) is not date or type(end) is not date or start > end:
            raise MxDailyHistoryError("MX daily history range must contain ordered dates")
        key = (canonical, start, end)
        with self._gate:
            if not force_refresh:
                in_memory = self._memory.get(key)
                if in_memory is not None:
                    self._memory.move_to_end(key)
                    return replace(in_memory, cache_status="memory")
                if canonical in PERSISTENT_SKILL_INSTRUMENTS:
                    cached = self._read_cache(canonical, start, end)
                    if cached is not None:
                        self._remember(key, cached)
                        return replace(cached, cache_status="disk")
            # A refresh owns a separate provider request. Replacing ownership
            # also prevents an older in-flight result from overwriting it.
            future = None if force_refresh else self._inflight.get(key)
            owner = future is None
            if future is None:
                if self._client is None:
                    raise MxDailyHistoryCacheMissError(
                        "MX daily history cache miss and no live provider is configured"
                    )
                future = Future[MxDailyHistory]()
                self._inflight[key] = future
        if not owner:
            history = await asyncio.wrap_future(future)
            return replace(history, cache_status="live")
        try:
            if (end - start).days > _MAX_HISTORY_CHUNK_DAYS:
                history = await self._fetch(
                    canonical, start, end, force_refresh=force_refresh, owner=future,
                )
            else:
                history = await self._fetch(canonical, start, end)
            history = replace(history, cache_status=None)
            with self._gate:
                if self._inflight.get(key) is future:
                    if canonical in PERSISTENT_SKILL_INSTRUMENTS:
                        self._write_cache(history)
                    self._remember(key, history)
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()
            raise
        else:
            future.set_result(history)
            return replace(history, cache_status="forced" if force_refresh else "live")
        finally:
            with self._gate:
                if self._inflight.get(key) is future:
                    self._inflight.pop(key, None)

    def _remember(self, key: tuple[str, date, date], history: MxDailyHistory) -> None:
        self._memory[key] = history
        self._memory.move_to_end(key)
        while len(self._memory) > self._memory_max_entries:
            self._memory.popitem(last=False)

    async def _fetch(
        self, symbol: str, start: date, end: date, *,
        force_refresh: bool = False, owner: Future[MxDailyHistory] | None = None,
    ) -> MxDailyHistory:
        assert self._client is not None
        identity_query = (
            f"证券代码等于{symbol}；获取证券代码、证券简称、是否上市、证券类型、交易市场"
        )
        identity_response = await self._client.screen(query=identity_query, asset_type="A股")
        identity_name = _identity_name(identity_response, symbol)

        listing_query = f"查询A股{symbol}的首发上市日、股票简称、是否上市"
        listing_response = await self._client.query_finance(
            query=listing_query,
            indicators=None,
        )
        listing_date, listing_name = _listing_metadata(listing_response, symbol)
        board = _board_for_symbol(symbol)
        if listing_name != identity_name:
            raise MxDailyHistoryError("MX identity responses disagree on the security name")
        if start < listing_date:
            raise MxDailyHistoryError("MX daily history cannot start before listing date")

        identity_evidence = (
            _screen_evidence("security_master", identity_response),
            _finance_evidence("listing_identity", listing_response, row_count=1),
        )
        if (end - start).days <= _MAX_HISTORY_CHUNK_DAYS:
            return await self._fetch_range(
                symbol, start, end, board, listing_date, identity_evidence,
            )

        merged: dict[date, MxDailyRow] = {}
        evidence = list(identity_evidence)
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=_MAX_HISTORY_CHUNK_DAYS), end)
            chunk_key = (symbol, cursor, chunk_end)
            chunk: MxDailyHistory | None = None
            with self._gate:
                if not force_refresh:
                    chunk = self._memory.get(chunk_key)
                    if chunk is None and symbol in PERSISTENT_SKILL_INSTRUMENTS:
                        chunk = self._read_cache(symbol, cursor, chunk_end)
            if chunk is None:
                chunk = await self._fetch_range(
                    symbol, cursor, chunk_end, board, listing_date, identity_evidence,
                )
            if chunk.board != board or chunk.listing_date != listing_date:
                raise MxDailyHistoryError("MX cached chunk security identity disagrees")
            # This is a truncation guard, not a manufactured exchange calendar.
            # Longer unexplained boundary gaps remain unavailable, never filled.
            if (
                (chunk.returned_start - cursor).days > 14
                or (chunk_end - chunk.returned_end).days > 14
            ):
                raise MxDailyHistoryError("MX history chunk boundary coverage is unconfirmed")
            if merged and chunk.returned_start != cursor:
                raise MxDailyHistoryError("MX history chunks omitted the overlap session")
            for row in chunk.rows:
                previous = merged.get(row.session_date)
                if previous is not None and previous != row:
                    raise MxDailyHistoryError("MX history chunks disagree on an overlap session")
                merged[row.session_date] = row
            if chunk.returned_end <= cursor:
                raise MxDailyHistoryError("MX history chunk did not advance")
            evidence.extend(
                item for item in chunk.query_evidence
                if item.purpose not in {"security_master", "listing_identity"}
            )
            with self._gate:
                # A superseded long refresh must not overwrite its successor's
                # chunk cache, matching the existing whole-request ownership rule.
                if owner is None or self._inflight.get((symbol, start, end)) is owner:
                    if symbol in PERSISTENT_SKILL_INSTRUMENTS:
                        self._write_cache(chunk)
                    self._remember(chunk_key, chunk)
            if chunk_end == end:
                break
            cursor = chunk.returned_end
        return MxDailyHistory(
            instrument_id=symbol, board=board, listing_date=listing_date,
            start=start, end=end,
            retrieved_at=max(item.retrieved_at for item in evidence),
            provider=MX_DAILY_HISTORY_PROVIDER,
            rows=tuple(merged[day] for day in sorted(merged)),
            query_evidence=tuple(evidence),
        )

    async def _fetch_range(
        self, symbol: str, start: date, end: date, board: Board,
        listing_date: date, identity_evidence: tuple[MxQueryEvidence, ...],
    ) -> MxDailyHistory:
        assert self._client is not None

        async def query_fields(
            *, query: str, indicators: str, required: tuple[str, ...],
        ) -> LiveFinanceDataResult:
            assert self._client is not None
            try:
                return await self._client.query_finance(query=query, indicators=indicators)
            except MxSaasProviderNoDataError as exc:
                raise MxDailyHistoryFieldsMissingError(required, start=start, end=end) from exc

        range_text = f"{start.isoformat()}至{end.isoformat()}"
        session_indicators = "前收盘价、交易状态、是否ST"
        session_response = await query_fields(
            query=f"查询{symbol} {range_text}每个交易日的{session_indicators}",
            indicators=session_indicators,
            required=_SESSION_FIELDS,
        )
        session_dates, session_fields = _history_fields(
            session_response,
            symbol=symbol,
            required=_SESSION_FIELDS,
            start=start,
            end=end,
        )

        limit_indicators = "涨停价、跌停价"
        raw_indicators = "不复权开盘价、不复权最高价、不复权最低价、不复权收盘价、成交量、成交额"
        adjusted_indicators = "后复权开盘价、后复权最高价、后复权最低价、后复权收盘价"
        limit_response, raw_response, adjusted_response = await asyncio.gather(
            query_fields(
                query=f"查询{symbol} {range_text}每个交易日的{limit_indicators}",
                indicators=limit_indicators,
                required=_LIMIT_FIELDS,
            ),
            query_fields(
                query=f"查询{symbol}{range_text}每个交易日数据",
                indicators=raw_indicators,
                required=_RAW_FIELDS,
            ),
            query_fields(
                query=f"查询{symbol}{range_text}每个交易日数据",
                indicators=adjusted_indicators,
                required=_RAW_FIELDS[:4],
            ),
        )
        limit_dates, limit_fields = _history_fields(
            limit_response,
            symbol=symbol,
            required=_LIMIT_FIELDS,
            start=start,
            end=end,
        )
        raw_dates, raw_fields = _history_fields(
            raw_response,
            symbol=symbol,
            required=_RAW_FIELDS,
            start=start,
            end=end,
        )
        adjusted_dates, adjusted_fields = _history_fields(
            adjusted_response,
            symbol=symbol,
            required=_RAW_FIELDS[:4],
            start=start,
            end=end,
        )
        if set(session_dates) != set(raw_dates) or set(session_dates) != set(adjusted_dates):
            raise MxDailyHistoryError(
                "MX session, raw and adjusted daily dates must align one-to-one"
            )
        trading_dates = {
            day
            for index, day in enumerate(session_dates)
            if _provider_trading_status(session_fields["交易状态"][index])
            is TradingStatus.TRADING
        }
        if set(limit_dates) != trading_dates:
            raise MxDailyHistoryError(
                "MX provider limits must cover every trading session and no suspended session"
            )
        session_index = {value: index for index, value in enumerate(session_dates)}
        limit_index = {value: index for index, value in enumerate(limit_dates)}
        session_fields = {
            **session_fields,
            **{
                name: [
                    limit_fields[name][limit_index[day]] if day in limit_index else None
                    for day in session_dates
                ]
                for name in _LIMIT_FIELDS
            },
        }
        raw_index = {value: index for index, value in enumerate(raw_dates)}
        adjusted_index = {value: index for index, value in enumerate(adjusted_dates)}
        rows = tuple(
            _daily_row(
                day,
                session_fields=session_fields,
                session_index=session_index[day],
                raw_fields=raw_fields,
                raw_index=raw_index[day],
                adjusted_fields=adjusted_fields,
                adjusted_index=adjusted_index[day],
            )
            for day in sorted(session_dates)
        )
        evidence = (
            *identity_evidence,
            _finance_evidence("sessions", session_response, row_count=len(session_dates)),
            _finance_evidence("limits", limit_response, row_count=len(limit_dates)),
            _finance_evidence("raw_prices", raw_response, row_count=len(raw_dates)),
            _finance_evidence("adjusted_prices", adjusted_response, row_count=len(adjusted_dates)),
        )
        return MxDailyHistory(
            instrument_id=symbol,
            board=board,
            listing_date=listing_date,
            start=start,
            end=end,
            retrieved_at=max(item.retrieved_at for item in evidence),
            provider=MX_DAILY_HISTORY_PROVIDER,
            rows=rows,
            query_evidence=evidence,
        )

    def _cache_path(self, symbol: str, start: date, end: date) -> Path:
        return self._cache_root / symbol / f"{start.isoformat()}_{end.isoformat()}.json"

    def _read_cache(self, symbol: str, start: date, end: date) -> MxDailyHistory | None:
        exact = self._cache_path(symbol, start, end)
        candidates = (exact, *sorted(exact.parent.glob("*.json")))
        seen: set[Path] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            history = self._read_cache_file(path)
            if history is None or history.instrument_id != symbol:
                continue
            if history.start > start or history.end < end:
                continue
            rows = tuple(row for row in history.rows if start <= row.session_date <= end)
            if not rows:
                continue
            return replace(history, start=start, end=end, rows=rows)
        return None

    def _read_cache_file(self, path: Path) -> MxDailyHistory | None:
        try:
            decoded: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(decoded, Mapping):
                return None
            payload = cast(Mapping[str, Any], decoded)
            if payload.get("schemaVersion") != MX_DAILY_HISTORY_CACHE_SCHEMA:
                return None
            request_value = payload.get("request")
            if not isinstance(request_value, Mapping):
                return None
            request = cast(Mapping[str, Any], request_value)
            source_symbol = str(request.get("instrumentId", ""))
            source_start = date.fromisoformat(str(request.get("start", "")))
            source_end = date.fromisoformat(str(request.get("end", "")))
            history = _history_from_payload(cast(Mapping[str, Any], payload["history"]))
            if (
                history.instrument_id != source_symbol
                or history.start != source_start
                or history.end != source_end
            ):
                return None
            return history
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            DomainValidationError,
        ):
            return None

    def _write_cache(self, history: MxDailyHistory) -> None:
        path = self._cache_path(history.instrument_id, history.start, history.end)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schemaVersion": MX_DAILY_HISTORY_CACHE_SCHEMA,
            "request": {
                "instrumentId": history.instrument_id,
                "start": history.start.isoformat(),
                "end": history.end.isoformat(),
            },
            "history": _history_payload(history),
        }
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)


def _history_fields(
    response: LiveFinanceDataResult,
    *,
    symbol: str,
    required: Sequence[str],
    start: date,
    end: date,
) -> tuple[tuple[date, ...], dict[str, list[object]]]:
    if response.provider != MX_DAILY_HISTORY_PROVIDER:
        raise MxDailyHistoryError("MX history response has the wrong provider")
    grouped: dict[tuple[date, ...], list[Mapping[str, Any]]] = {}
    for table in response.tables:
        if not _table_belongs_to(table, symbol):
            continue
        raw_value = table.get("rawTable")
        if not isinstance(raw_value, Mapping):
            continue
        raw = cast(Mapping[str, object], raw_value)
        raw_dates = raw.get("headName")
        if not isinstance(raw_dates, list) or not raw_dates:
            continue
        typed_dates = cast(list[object], raw_dates)
        try:
            dates = tuple(date.fromisoformat(str(value).strip()) for value in typed_dates)
        except ValueError:
            continue
        if len(dates) != len(set(dates)):
            raise MxDailyHistoryError("MX history response contains duplicate dates")
        if any(not start <= value <= end for value in dates):
            raise MxDailyHistoryError("MX history response contains dates outside the request")
        grouped.setdefault(dates, []).append(table)
    if not grouped:
        raise MxDailyHistoryError("MX history response omitted an exact-symbol historical table")
    longest = max(len(axis) for axis in grouped)
    winners = [(axis, tables) for axis, tables in grouped.items() if len(axis) == longest]
    if len(winners) != 1:
        raise MxDailyHistoryError("MX history response has ambiguous historical date axes")
    dates, tables = winners[0]
    fields: dict[str, list[object]] = {}
    wanted = set(required)
    for table in tables:
        raw = cast(Mapping[str, object], table["rawTable"])
        names = table.get("nameMap")
        if not isinstance(names, Mapping):
            continue
        for code, raw_name in cast(Mapping[object, object], names).items():
            if not isinstance(code, str) or code not in raw:
                continue
            name = _provider_field_name(raw_name)
            if name not in wanted:
                continue
            values_value = raw[code]
            if not isinstance(values_value, list):
                raise MxDailyHistoryError(f"MX {name} values do not align with dates")
            values = cast(list[object], values_value)
            if len(values) != len(dates):
                raise MxDailyHistoryError(f"MX {name} values do not align with dates")
            typed_values = list(values)
            existing = fields.get(name)
            if existing is not None and existing != typed_values:
                raise MxDailyHistoryError(f"MX history has conflicting {name} fields")
            fields[name] = typed_values
    missing = sorted(wanted - set(fields))
    if missing:
        raise MxDailyHistoryFieldsMissingError(tuple(missing), start=start, end=end)
    return dates, fields


def _canonical_symbol(value: str) -> str:
    try:
        canonical = str(normalize_a_share_instrument(value))
    except AshareInstrumentCodeError as exc:
        raise MxDailyHistoryError("instrument_id must be a canonical A-share stock") from exc
    digits, market = canonical.split(".", maxsplit=1)
    if market not in _EXCHANGE_NAMES or digits.startswith(("5", "1")):
        raise MxDailyHistoryError("MX daily history currently covers SH/SZ stocks only")
    return canonical


def _identity_name(response: LiveMarketDataResult, symbol: str) -> str:
    if response.provider != "eastmoney_mx_screener" or response.asset_type != "A股":
        raise MxDailyHistoryError("MX security-master response has the wrong provider scope")
    if len(response.rows) != 1:
        raise MxDailyHistoryError("MX security-master query must return exactly one A-share")
    row = response.rows[0]
    digits, market = symbol.split(".", maxsplit=1)
    code = _one_text(row, ("证券代码", "股票代码", "代码"), "security code")
    if code.upper() not in {digits, symbol}:
        raise MxDailyHistoryError("MX security-master response belongs to another security")
    if _one_text(row, ("证券类型",), "security type") != "A股":
        raise MxDailyHistoryError("MX security-master response is not an A-share stock")
    if _one_text(row, ("上市状态",), "listing status") != "正常上市":
        raise MxDailyHistoryError("MX security-master response is not currently listed")
    if _one_text(row, ("市场类型",), "exchange") != _EXCHANGE_NAMES[market]:
        raise MxDailyHistoryError("MX security-master exchange conflicts with the symbol")
    return _one_text(row, ("证券简称", "股票简称", "名称"), "security name")


def _listing_metadata(response: LiveFinanceDataResult, symbol: str) -> tuple[date, str]:
    candidates: list[dict[str, list[object]]] = []
    required = {"首发上市日", "股票简称", "是否上市"}
    for table in response.tables:
        if not _table_belongs_to(table, symbol):
            continue
        fields = _named_fields(table)
        if required <= set(fields):
            candidates.append(fields)
    if len(candidates) != 1:
        raise MxDailyHistoryError(
            "MX listing query must return one exact security metadata table"
        )
    fields = candidates[0]
    listing_text = _one_provider_value(fields["首发上市日"], "listing date")
    name = _one_provider_value(fields["股票简称"], "security name")
    if _one_provider_value(fields["是否上市"], "listing flag") != "是":
        raise MxDailyHistoryError("MX listing identity is not currently listed")
    try:
        return date.fromisoformat(listing_text), name
    except ValueError as exc:
        raise MxDailyHistoryError("MX listing date must be ISO text") from exc


def _board_for_symbol(symbol: str) -> Board:
    digits, market = symbol.split(".", maxsplit=1)
    if market == "SH" and digits.startswith(("688", "689")):
        return Board.STAR
    if market == "SZ" and (digits.startswith(("300", "301")) or digits == "302132"):
        return Board.CHINEXT
    if (market == "SH" and digits.startswith(("600", "601", "603", "605"))) or (
        market == "SZ" and digits.startswith(("000", "001", "002", "003"))
    ):
        return Board.MAIN
    raise MxDailyHistoryError("security code is outside supported A-share stock boards")


def _named_fields(table: Mapping[str, Any]) -> dict[str, list[object]]:
    raw_value = table.get("rawTable")
    names_value = table.get("nameMap")
    if not isinstance(raw_value, Mapping) or not isinstance(names_value, Mapping):
        return {}
    raw = cast(Mapping[str, object], raw_value)
    output: dict[str, list[object]] = {}
    for code, raw_name in cast(Mapping[object, object], names_value).items():
        if not isinstance(code, str) or code == "headNameSub" or code not in raw:
            continue
        values = raw[code]
        if not isinstance(values, list):
            continue
        name = _provider_field_name(raw_name)
        typed_values = list(cast(list[object], values))
        if name in output and output[name] != typed_values:
            raise MxDailyHistoryError("MX response contains conflicting duplicate labels")
        output[name] = typed_values
    return output


def _provider_field_name(value: object) -> str:
    name = str(value).strip()
    return _PROVIDER_FIELD_ALIASES.get(name, name)


def _one_text(row: Mapping[str, Any], aliases: Sequence[str], label: str) -> str:
    values = {
        str(row[alias]).strip()
        for alias in aliases
        if alias in row and not _is_missing(row[alias])
    }
    if len(values) != 1:
        raise MxDailyHistoryError(f"MX {label} is missing or ambiguous")
    return next(iter(values))


def _one_provider_value(values: Sequence[object], label: str) -> str:
    cleaned = {str(value).strip() for value in values if not _is_missing(value)}
    if len(cleaned) != 1:
        raise MxDailyHistoryError(f"MX {label} is missing or ambiguous")
    return next(iter(cleaned))


def _daily_row(
    day: date,
    *,
    session_fields: Mapping[str, Sequence[object]],
    session_index: int,
    raw_fields: Mapping[str, Sequence[object]],
    raw_index: int,
    adjusted_fields: Mapping[str, Sequence[object]],
    adjusted_index: int,
) -> MxDailyRow:
    trading_status = _provider_trading_status(session_fields["交易状态"][session_index])
    raw_st = _required_text(session_fields["是否为ST股票"][session_index], "是否为ST股票")
    if raw_st not in _ST_STATUSES:
        raise MxDailyHistoryError(f"unsupported MX ST status: {raw_st}")
    volume = _liquidity_integer(raw_fields["成交量"][raw_index], trading_status, "成交量")
    amount = _liquidity_decimal(raw_fields["成交额"][raw_index], trading_status, "成交额")
    raw_upper = session_fields["涨停价"][session_index]
    raw_lower = session_fields["跌停价"][session_index]
    if trading_status is TradingStatus.SUSPENDED and _is_missing(raw_upper) and _is_missing(
        raw_lower
    ):
        upper_limit = None
        lower_limit = None
        limit_source: Literal[
            "eastmoney_mx_finance_data", "not_applicable_suspended"
        ] = "not_applicable_suspended"
    else:
        upper_limit = _positive_decimal(raw_upper, "涨停价")
        lower_limit = _positive_decimal(raw_lower, "跌停价")
        limit_source = MX_DAILY_HISTORY_PROVIDER
    return MxDailyRow(
        session_date=day,
        raw_open=_positive_decimal(raw_fields["开盘价"][raw_index], "不复权开盘价"),
        raw_high=_positive_decimal(raw_fields["最高价"][raw_index], "不复权最高价"),
        raw_low=_positive_decimal(raw_fields["最低价"][raw_index], "不复权最低价"),
        raw_close=_positive_decimal(raw_fields["收盘价"][raw_index], "不复权收盘价"),
        raw_preclose=_positive_decimal(
            session_fields["前收盘价"][session_index], "前收盘价"
        ),
        adjusted_open=_positive_decimal(
            adjusted_fields["开盘价"][adjusted_index], "后复权开盘价"
        ),
        adjusted_high=_positive_decimal(
            adjusted_fields["最高价"][adjusted_index], "后复权最高价"
        ),
        adjusted_low=_positive_decimal(
            adjusted_fields["最低价"][adjusted_index], "后复权最低价"
        ),
        adjusted_close=_positive_decimal(
            adjusted_fields["收盘价"][adjusted_index], "后复权收盘价"
        ),
        volume=volume,
        amount=amount,
        trading_status=trading_status,
        is_st=_ST_STATUSES[raw_st],
        upper_limit=upper_limit,
        lower_limit=lower_limit,
        limit_source=limit_source,
    )


def _provider_trading_status(value: object) -> TradingStatus:
    raw_status = _required_text(value, "交易状态")
    if raw_status not in _TRADING_STATUSES:
        raise MxDailyHistoryError(f"unsupported MX trading status: {raw_status}")
    return _TRADING_STATUSES[raw_status]


def _table_belongs_to(table: Mapping[str, Any], symbol: str) -> bool:
    code = table.get("code")
    codes = table.get("entityCodes")
    return (isinstance(code, str) and code.strip().upper() == symbol) or (
        isinstance(codes, list)
        and symbol in {str(value).strip().upper() for value in cast(list[object], codes)}
    )


def _screen_evidence(purpose: str, response: Any) -> MxQueryEvidence:
    return MxQueryEvidence(
        purpose=purpose,
        provider=str(response.provider),
        query=str(response.query),
        retrieved_at=response.provenance.retrieved_at,
        response_sha256=str(response.provenance.response_sha256),
        row_count=len(response.rows),
    )


def _finance_evidence(
    purpose: str,
    response: LiveFinanceDataResult,
    *,
    row_count: int,
) -> MxQueryEvidence:
    return MxQueryEvidence(
        purpose=purpose,
        provider=response.provider,
        query=response.query,
        retrieved_at=response.provenance.retrieved_at,
        response_sha256=response.provenance.response_sha256,
        row_count=row_count,
    )


def _positive_decimal(value: object, label: str) -> Decimal:
    result = _non_negative_decimal(value, label)
    if result <= 0:
        raise MxDailyHistoryError(f"MX {label} must be positive")
    return result


def _non_negative_decimal(value: object, label: str) -> Decimal:
    if _is_missing(value):
        raise MxDailyHistoryError(f"MX {label} is missing")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MxDailyHistoryError(f"MX {label} is not numeric") from exc
    if not result.is_finite() or result < 0:
        raise MxDailyHistoryError(f"MX {label} is invalid")
    return result


def _liquidity_decimal(value: object, status: TradingStatus, label: str) -> Decimal:
    if status is TradingStatus.SUSPENDED and _is_missing(value):
        return Decimal(0)
    result = _non_negative_decimal(value, label)
    if status is TradingStatus.SUSPENDED and result != 0:
        raise MxDailyHistoryError(f"MX suspended {label} must be empty or zero")
    return result


def _liquidity_integer(value: object, status: TradingStatus, label: str) -> int:
    result = _liquidity_decimal(value, status, label)
    if result != result.to_integral_value():
        raise MxDailyHistoryError(f"MX {label} must be an integer")
    return int(result)


def _required_text(value: object, label: str) -> str:
    if _is_missing(value):
        raise MxDailyHistoryError(f"MX {label} is missing")
    return str(value).strip()


def _is_missing(value: object) -> bool:
    return value is None or str(value).strip().casefold() in _MISSING_VALUES


def _history_payload(history: MxDailyHistory) -> dict[str, object]:
    return {
        "adjustment": history.adjustment,
        "board": history.board.value,
        "end": history.end.isoformat(),
        "instrumentId": history.instrument_id,
        "listingDate": history.listing_date.isoformat(),
        "provider": history.provider,
        "queryEvidence": [
            {
                "provider": item.provider,
                "purpose": item.purpose,
                "query": item.query,
                "responseSha256": item.response_sha256,
                "retrievedAt": item.retrieved_at.isoformat(),
                "rowCount": item.row_count,
            }
            for item in history.query_evidence
        ],
        "retrievedAt": history.retrieved_at.isoformat(),
        "rows": [
            {
                "adjustedClose": str(row.adjusted_close),
                "adjustedHigh": str(row.adjusted_high),
                "adjustedLow": str(row.adjusted_low),
                "adjustedOpen": str(row.adjusted_open),
                "amount": str(row.amount),
                "isSt": row.is_st,
                "limitSource": row.limit_source,
                "lowerLimit": None if row.lower_limit is None else str(row.lower_limit),
                "rawClose": str(row.raw_close),
                "rawHigh": str(row.raw_high),
                "rawLow": str(row.raw_low),
                "rawOpen": str(row.raw_open),
                "rawPreclose": str(row.raw_preclose),
                "sessionDate": row.session_date.isoformat(),
                "tradingStatus": row.trading_status.value,
                "upperLimit": None if row.upper_limit is None else str(row.upper_limit),
                "volume": row.volume,
            }
            for row in history.rows
        ],
        "start": history.start.isoformat(),
    }


def _history_from_payload(payload: Mapping[str, Any]) -> MxDailyHistory:
    return MxDailyHistory(
        instrument_id=str(payload["instrumentId"]),
        board=Board(str(payload["board"])),
        listing_date=date.fromisoformat(str(payload["listingDate"])),
        start=date.fromisoformat(str(payload["start"])),
        end=date.fromisoformat(str(payload["end"])),
        retrieved_at=datetime.fromisoformat(str(payload["retrievedAt"])),
        provider=cast(Literal["eastmoney_mx_finance_data"], str(payload["provider"])),
        adjustment=cast(
            Literal["provider_declared_back_adjusted"], str(payload["adjustment"])
        ),
        rows=tuple(
            MxDailyRow(
                session_date=date.fromisoformat(str(row["sessionDate"])),
                raw_open=Decimal(str(row["rawOpen"])),
                raw_high=Decimal(str(row["rawHigh"])),
                raw_low=Decimal(str(row["rawLow"])),
                raw_close=Decimal(str(row["rawClose"])),
                raw_preclose=Decimal(str(row["rawPreclose"])),
                adjusted_open=Decimal(str(row["adjustedOpen"])),
                adjusted_high=Decimal(str(row["adjustedHigh"])),
                adjusted_low=Decimal(str(row["adjustedLow"])),
                adjusted_close=Decimal(str(row["adjustedClose"])),
                volume=int(row["volume"]),
                amount=Decimal(str(row["amount"])),
                trading_status=TradingStatus(str(row["tradingStatus"])),
                is_st=cast(bool, row["isSt"]),
                upper_limit=(
                    None if row["upperLimit"] is None else Decimal(str(row["upperLimit"]))
                ),
                lower_limit=(
                    None if row["lowerLimit"] is None else Decimal(str(row["lowerLimit"]))
                ),
                limit_source=cast(
                    Literal["eastmoney_mx_finance_data", "not_applicable_suspended"],
                    str(row["limitSource"]),
                ),
            )
            for row in cast(Sequence[Mapping[str, Any]], payload["rows"])
        ),
        query_evidence=tuple(
            MxQueryEvidence(
                purpose=str(item["purpose"]),
                provider=str(item["provider"]),
                query=str(item["query"]),
                retrieved_at=datetime.fromisoformat(str(item["retrievedAt"])),
                response_sha256=str(item["responseSha256"]),
                row_count=int(item["rowCount"]),
            )
            for item in cast(Sequence[Mapping[str, Any]], payload["queryEvidence"])
        ),
    )


__all__ = [
    "MX_BACK_ADJUSTMENT",
    "MX_DAILY_HISTORY_PROVIDER",
    "MxDailyHistory",
    "MxDailyHistoryCacheMissError",
    "MxDailyHistoryClient",
    "MxDailyHistoryError",
    "MxDailyRow",
    "MxQueryEvidence",
]
