"""Eastmoney's public security autocomplete: identities, never strategy advice.

Endpoint and taxonomy verified against the stock search on quote.eastmoney.com
and its stocksuggest2017 script. No model, private key or historical price call.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import httpx

from ashare_lab.domain.market_data.instruments import (
    AshareInstrumentCodeError,
    normalize_a_share_instrument,
)
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous, InstrumentNameCandidate

from .a_share_directory import InstrumentDirectory
from .a_share_directory import load as load_directory

_URL = "https://search-codetable.eastmoney.com/codetable/search/web"
_PAGE_SIZE = 100
_FRESH_AGE = timedelta(days=1)
_MAX_AGE = timedelta(days=7)
_MAX_CACHE_ENTRIES = 128
_MAX_CACHE_BYTES = 4 * 1024 * 1024
_MAX_REFRESH_TASKS = 4
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / "var/cache/instrument-search"
_CACHE_FILENAME = "identities-v1.json"
_DIRECTORY_PATH = Path(__file__).resolve().parents[2] / "resources/a_share_directory.json"
_A_SHARE_TYPES: dict[str, tuple[int, Literal["SH", "SZ", "BJ"]]] = {
    "沪A": (1, "SH"), "科创板": (1, "SH"), "深A": (0, "SZ"), "京A": (0, "BJ"),
}


class InstrumentSearchUnavailable(RuntimeError):
    """The upstream could not complete a security search."""


class InstrumentSearchInvalid(RuntimeError):
    """The upstream returned an unrecognised or inconsistent identity payload."""


@dataclass(frozen=True, slots=True)
class SearchInstrument:
    symbol: str
    name: str
    exchange: Literal["SH", "SZ", "BJ"]


@dataclass(frozen=True, slots=True)
class InstrumentSearchResult:
    query: str
    items: tuple[SearchInstrument, ...]
    retrieved_at: datetime
    has_more: bool
    source: Literal["eastmoney_security_search", "eastmoney_instrument_directory"] = (
        "eastmoney_security_search"
    )
    cache_status: Literal["live", "fresh_cache", "stale_cache"] = "live"


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    retrieved_at: datetime
    items: tuple[SearchInstrument, ...]
    has_more: bool


def parse_a_share_identities(payload: object) -> tuple[tuple[SearchInstrument, ...], bool]:
    if not isinstance(payload, Mapping):
        raise InstrumentSearchInvalid("search envelope missing")
    data = cast(Mapping[str, object], payload)
    rows = data.get("result")
    if str(data.get("code")) != "0" or not isinstance(rows, list):
        raise InstrumentSearchInvalid("search result invalid")
    typed_rows = cast(list[object], rows)
    if len(typed_rows) > 1000:
        raise InstrumentSearchInvalid("search result too large")
    identities: dict[str, SearchInstrument] = {}
    for raw in typed_rows:
        if not isinstance(raw, Mapping):
            raise InstrumentSearchInvalid("search row invalid")
        row = cast(Mapping[str, object], raw)
        kind = row.get("securityTypeName")
        if not isinstance(kind, str) or kind not in _A_SHARE_TYPES:
            continue  # Index/ETF/HK/US identities may share a six-digit code.
        market, exchange = _A_SHARE_TYPES[kind]
        code, name = row.get("code"), row.get("shortName")
        if (type(row.get("market")) is not int or row.get("market") != market
                or not isinstance(code, str)
                or not isinstance(name, str) or not name.strip() or len(name) > 64):
            raise InstrumentSearchInvalid("A-share identity incomplete")
        try:
            symbol = normalize_a_share_instrument(f"{code}.{exchange}").value
        except AshareInstrumentCodeError as exc:
            raise InstrumentSearchInvalid("A-share code/exchange conflict") from exc
        item = SearchInstrument(symbol=symbol, name=name.strip(), exchange=exchange)
        if symbol in identities and identities[symbol] != item:
            raise InstrumentSearchInvalid("conflicting security names")
        identities[symbol] = item
    return tuple(identities.values()), len(typed_rows) >= _PAGE_SIZE


class EastmoneyInstrumentSearch:
    """Bounded identity lookup with fresh and stale persistent query results."""

    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None,
        cache_dir: Path | None = None, clock: Callable[[], datetime] | None = None,
        directory_path: Path | None = None,
    ) -> None:
        self._transport = transport
        # Injected test transports never read or write the application's cache
        # unless the test also explicitly supplies its own directory.
        self._cache_dir = cache_dir if cache_dir is not None else (
            _DEFAULT_CACHE_DIR if transport is None else None
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._directory: InstrumentDirectory | None = None
        # Isolated transports must opt into a directory explicitly, just as
        # they opt into the disk query cache. Runtime loads only packaged identities.
        self._directory_path = (directory_path or _DIRECTORY_PATH) if (
            directory_path is not None or transport is None
        ) else None
        self._directory_stamp: tuple[int, int] | None = None
        self._read_directory()

    def _read_directory(self) -> None:
        if self._directory_path is None:
            return
        try:
            stat = self._directory_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._directory_stamp:
                self._directory_stamp = stamp
                self._directory = load_directory(self._directory_path)
        except (OSError, ValueError):
            # A missing/bad update does not remove the last verified directory.
            # The acquisition command validates before atomically replacing it.
            pass

    def _read_cache(self) -> None:
        if self._cache_dir is None:
            return
        try:
            with (self._cache_dir / _CACHE_FILENAME).open("rb") as handle:
                raw = handle.read(_MAX_CACHE_BYTES + 1)
            if len(raw) > _MAX_CACHE_BYTES:
                return
            payload: object = json.loads(raw)
            if not isinstance(payload, Mapping):
                return
            data = cast(Mapping[str, object], payload)
            records = data.get("entries")
            if (data.get("version") != 1 or not isinstance(records, list)
                    or len(cast(list[object], records)) > _MAX_CACHE_ENTRIES):
                return
            now = self._clock()
            for raw_record in cast(list[object], records):
                try:
                    if not isinstance(raw_record, Mapping):
                        continue
                    record = cast(Mapping[str, object], raw_record)
                    key, timestamp = record.get("query"), record.get("retrieved_at")
                    if (not isinstance(key, str) or not key or len(key) > 32
                            or key != key.strip().casefold() or not isinstance(timestamp, str)):
                        continue
                    retrieved_at = datetime.fromisoformat(timestamp)
                    if (retrieved_at.tzinfo is None
                            or not timedelta(0) <= now - retrieved_at <= _MAX_AGE):
                        continue
                    rows, more = record.get("items"), record.get("has_more")
                    if (not isinstance(rows, list) or len(cast(list[object], rows)) > 1000
                            or not isinstance(more, bool)):
                        continue
                    items: dict[str, SearchInstrument] = {}
                    for raw_row in cast(list[object], rows):
                        if not isinstance(raw_row, Mapping):
                            raise ValueError("invalid cached identity")
                        row = cast(Mapping[str, object], raw_row)
                        symbol, name = row.get("symbol"), row.get("name")
                        exchange = row.get("exchange")
                        if (not isinstance(symbol, str)
                                or not isinstance(name, str) or not name.strip() or len(name) > 64
                                or not isinstance(exchange, str)
                                or exchange not in {"SH", "SZ", "BJ"}
                                or normalize_a_share_instrument(symbol).value != symbol
                                or not symbol.endswith(f".{exchange}")):
                            raise ValueError("invalid cached identity")
                        item = SearchInstrument(symbol, name.strip(),
                                                cast(Literal["SH", "SZ", "BJ"], exchange))
                        if symbol in items and items[symbol] != item:
                            raise ValueError("conflicting cached identity")
                        items[symbol] = item
                    entry = _CacheEntry(retrieved_at, tuple(items.values()), more)
                    previous = self._cache.get(key)
                    if previous is None or previous.retrieved_at < retrieved_at:
                        self._cache[key] = entry
                except (KeyError, TypeError, ValueError):
                    continue  # A corrupt record cannot block a fresh provider lookup.
            while len(self._cache) > _MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        except (OSError, TypeError, ValueError):
            return

    def resolve_local_name(self, query: str) -> str | None:
        """Resolve a unique complete directory alias without network or refresh.

        Reuse the validated in-memory directory used by autocomplete. Its
        acquisition time is identity evidence, not historical tradability.
        ``None`` means the existing provider resolver may try an unlisted name.
        Ambiguous aliases and partial matches must be confirmed by the user.
        """
        self._read_directory()
        directory = self._directory
        if directory is None:
            return None
        exact = directory.exact_matches(query)
        if len(exact) == 1:
            return exact[0].symbol
        matches = exact or directory.search(query)
        if matches:
            raise InstrumentNameAmbiguous(tuple(
                InstrumentNameCandidate(
                    symbol=item.symbol, name=item.name, source=directory.source,
                    retrieved_at=directory.retrieved_at,
                ) for item in matches[:3]
            ))
        return None

    def _write_cache(self) -> None:
        if self._cache_dir is None:
            return
        temporary: Path | None = None
        try:
            # One fixed filename prevents query text from becoming a path.
            # Trim both entry count and encoded bytes before the atomic write.
            while True:
                payload = json.dumps({"version": 1, "entries": [
                    {"query": key, "retrieved_at": entry.retrieved_at.isoformat(),
                     "items": [asdict(item) for item in entry.items], "has_more": entry.has_more}
                    for key, entry in self._cache.items()
                ]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(payload) <= _MAX_CACHE_BYTES:
                    break
                if not self._cache:
                    return
                self._cache.popitem(last=False)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self._cache_dir, prefix=".identities-",
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._cache_dir / _CACHE_FILENAME)
        except (OSError, TypeError, ValueError):
            pass  # Disk availability never changes a successful identity response.
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    async def _fetch_and_store(self, keyword: str, key: str) -> _CacheEntry:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=10), transport=self._transport,
                # This endpoint routes by Accept; application/json returns
                # HTTP 200 with a business-level 404 "Path mismatch".
                headers={"Accept": "*/*"},
            ) as client:
                response = await client.get(_URL, params={
                    "client": "web", "clientType": "webSuggest", "clientVersion": "lastest",
                    "keyword": keyword, "pageIndex": 1, "pageSize": _PAGE_SIZE,
                })
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise InstrumentSearchUnavailable("security search unavailable") from exc
        try:
            items, more = parse_a_share_identities(response.json())
        except ValueError as exc:
            raise InstrumentSearchInvalid("search JSON invalid") from exc
        entry = _CacheEntry(self._clock(), items, more)
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)
        self._write_cache()
        return entry

    async def _refresh(self, keyword: str, key: str) -> None:
        try:
            await self._fetch_and_store(keyword, key)
        except Exception:
            # Refreshes are best-effort: retain the verified timestamp and consume
            # task failures without affecting the already-returned stale response.
            pass
        finally:
            self._refresh_tasks.pop(key, None)

    def _queue_refresh(self, keyword: str, key: str) -> None:
        if key not in self._refresh_tasks and len(self._refresh_tasks) < _MAX_REFRESH_TASKS:
            self._refresh_tasks[key] = asyncio.create_task(self._refresh(keyword, key))

    def _exact_cached_identity(self, query: str) -> tuple[str, _CacheEntry] | None:
        """Resolve only an exact verified identity, without creating query aliases on disk."""
        canonical: str | None = None
        if "." in query or (len(query) == 6 and query.isdigit()):
            try:
                canonical = normalize_a_share_instrument(query).value
            except AshareInstrumentCodeError:
                return None
        latest: dict[str, tuple[str, _CacheEntry, SearchInstrument]] = {}
        conflicts: set[str] = set()
        now = self._clock()
        for source_key, entry in self._cache.items():
            if not timedelta(0) <= now - entry.retrieved_at <= _MAX_AGE:
                continue
            for item in entry.items:
                previous = latest.get(item.symbol)
                if previous is None or previous[1].retrieved_at < entry.retrieved_at:
                    latest[item.symbol] = (source_key, entry, item)
                    conflicts.discard(item.symbol)
                elif previous[1].retrieved_at == entry.retrieved_at and previous[2] != item:
                    conflicts.add(item.symbol)
        # Select the newest name before comparing: an older matching name must
        # not revive an identity that another cached response has already renamed.
        matches = [
            (source_key, _CacheEntry(entry.retrieved_at, (item,), False))
            for source_key, entry, item in latest.values()
            if item.symbol not in conflicts and (
                item.symbol == canonical if canonical is not None
                else item.name.casefold() == query.casefold()
            )
        ]
        return matches[0] if len(matches) == 1 else None

    async def search(self, query: str, *, limit: int = 8) -> InstrumentSearchResult:
        query = unicodedata.normalize("NFKC", query).strip()
        if not query or len(query) > 32 or not 1 <= limit <= 20:
            raise ValueError("invalid security search query or limit")
        self._read_directory()
        # A supplied canonical suffix must agree with its code before lookup.
        keyword = query
        if "." in query:
            try:
                keyword = normalize_a_share_instrument(query).value.split(".")[0]
            except AshareInstrumentCodeError:
                return InstrumentSearchResult(query, (), self._clock(), False)
        key = keyword.casefold()
        cached = self._cache.get(key)
        if cached is None or self._clock() - cached.retrieved_at > _FRESH_AGE:
            self._read_cache()
            cached = self._cache.get(key)
        age = self._clock() - cached.retrieved_at if cached else None
        # A newer full directory supersedes older keyword/alias results.
        # Its date describes identity lookup only, never historical tradability.
        directory = self._directory
        if directory is not None and (
            cached is None or cached.retrieved_at <= directory.retrieved_at
            or (age is not None and age > _MAX_AGE)
        ):
            matches = directory.search(query)
            if matches:
                stale = self._clock() - directory.retrieved_at > _FRESH_AGE
                if stale:
                    self._queue_refresh(keyword, key)
                return InstrumentSearchResult(
                    query, tuple(SearchInstrument(item.symbol, item.name, item.exchange)
                                 for item in matches[:limit]),
                    directory.retrieved_at, len(matches) > limit,
                    source="eastmoney_instrument_directory",
                    cache_status="stale_cache" if stale else "fresh_cache",
                )
        if cached is not None and age is not None and timedelta(0) <= age <= _MAX_AGE:
            self._cache.move_to_end(key)
            stale = age > _FRESH_AGE
            if stale:
                self._queue_refresh(keyword, key)
            return InstrumentSearchResult(
                query, cached.items[:limit], cached.retrieved_at,
                cached.has_more or len(cached.items) > limit,
                cache_status="stale_cache" if stale else "fresh_cache",
            )
        matched = self._exact_cached_identity(query)
        if matched is not None:
            source_key, identity = matched
            self._cache.move_to_end(source_key)
            stale = self._clock() - identity.retrieved_at > _FRESH_AGE
            if stale:
                # Name, bare code and canonical code share one refresh task.
                code = identity.items[0].symbol.split(".")[0]
                self._queue_refresh(code, code)
            return InstrumentSearchResult(
                query, identity.items, identity.retrieved_at, False,
                cache_status="stale_cache" if stale else "fresh_cache",
            )
        entry = await self._fetch_and_store(keyword, key)
        return InstrumentSearchResult(
            query, entry.items[:limit], entry.retrieved_at,
            entry.has_more or len(entry.items) > limit,
        )
