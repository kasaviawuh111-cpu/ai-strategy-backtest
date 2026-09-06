"""Validated current A-share identities for bounded, entirely offline lookup.

This directory proves only what its acquisition metadata and rows contain.
The minimum row count is a sanity check, not evidence of full-market coverage;
the producer must obtain ``reported_total`` from its complete acquisition.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from ashare_lab.domain.market_data.instruments import normalize_a_share_instrument

_SOURCE = "eastmoney_instrument_directory"
_MIN_ITEMS = 4_000
_MAX_ITEMS = 15_000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_ASCII_PINYIN = re.compile(r"[A-Za-z0-9]+")
_CANONICAL_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")


def _search_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _query_key(query: str) -> str | None:
    if len(query) > 256:
        raise ValueError("directory search query must be a string of at most 256 characters")
    key = _search_key(query)
    if not key:
        raise ValueError("directory search query cannot be empty")
    if "." in key:
        try:
            return normalize_a_share_instrument(key).value.casefold()
        except ValueError:
            return None
    return key


@dataclass(frozen=True, slots=True)
class DirectoryInstrument:
    symbol: str
    name: str
    exchange: Literal["SH", "SZ", "BJ"]
    pinyin: str
    initials: str


@dataclass(frozen=True, slots=True)
class InstrumentDirectory:
    items: tuple[DirectoryInstrument, ...]
    retrieved_at: datetime
    reported_total: int
    source_url: str
    source: Literal["eastmoney_instrument_directory"] = _SOURCE
    _keys: tuple[tuple[str, ...], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_keys", tuple(
            tuple(_search_key(value) for value in (
                item.name, item.symbol, item.symbol.split(".")[0],
                item.pinyin, item.initials,
            ))
            for item in self.items
        ))

    def search(self, query: str) -> tuple[DirectoryInstrument, ...]:
        """Return all matches ranked exact, prefix, then contains; never fetch."""
        key = _query_key(query)
        if key is None:
            return ()
        ranked: list[tuple[int, str, DirectoryInstrument]] = []
        for item, fields in zip(self.items, self._keys, strict=True):
            rank = min((
                0 if key == candidate else 1 if candidate.startswith(key) else 2
                for candidate in fields if key in candidate
            ), default=3)
            if rank < 3:
                ranked.append((rank, item.symbol, item))
        ranked.sort(key=lambda match: (match[0], match[1]))
        return tuple(item for _, _, item in ranked)

    def exact_matches(self, query: str) -> tuple[DirectoryInstrument, ...]:
        """Match complete names, codes, pinyin or initials, before any UI limit.

        Search prefixes remain suggestions; even one partial result cannot
        supply an executable identity. An alias shared by two stocks is ambiguous.
        """
        key = _query_key(query)
        if key is None:
            return ()
        return tuple(sorted(
            (item for item, fields in zip(self.items, self._keys, strict=True) if key in fields),
            key=lambda item: item.symbol,
        ))


def _text(row: dict[str, object], key: str, maximum: int) -> str:
    value = row.get(key)
    if (not isinstance(value, str) or not value or len(value) > maximum
            or value != value.strip() or any(unicodedata.category(c) == "Cc" for c in value)):
        raise ValueError(f"invalid directory field: {key}")
    return value


def load(path: Path, *, min_items: int = _MIN_ITEMS) -> InstrumentDirectory:
    """Read and validate a directory; ``min_items`` only supports isolated tests.

    Application callers must retain the production default. I/O errors,
    malformed JSON and invalid metadata all raise ValueError for the caller's
    explicit fallback handling. No directory is changed and no network is used.
    """
    if type(min_items) is not int or not 1 <= min_items <= _MAX_ITEMS:
        raise ValueError("invalid minimum directory size")
    try:
        with path.open("rb") as stream:
            encoded = stream.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError("instrument directory is unreadable") from exc
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError("instrument directory exceeds the file size limit")
    try:
        payload: object = json.loads(encoded)
    except (ValueError, RecursionError) as exc:
        raise ValueError("instrument directory JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("instrument directory must be an object")
    data = cast(dict[str, object], payload)
    if type(data.get("version")) is not int or data.get("version") != 1:
        raise ValueError("unsupported instrument directory version")
    if data.get("source") != _SOURCE:
        raise ValueError("unexpected instrument directory source")
    source_url = _text(data, "source_url", 2048)
    try:
        url = urlsplit(source_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or not (url.hostname == "eastmoney.com"
                        or url.hostname.endswith(".eastmoney.com"))):
            raise ValueError("invalid instrument directory source URL")
    except ValueError as exc:
        raise ValueError("invalid instrument directory source URL") from exc
    timestamp = _text(data, "retrieved_at", 64)
    try:
        retrieved_at = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError("invalid instrument directory retrieval time") from exc
    if (retrieved_at.tzinfo is None or retrieved_at.utcoffset() != timedelta(0)
            or retrieved_at > datetime.now(UTC)):
        raise ValueError("instrument directory retrieval time must be UTC and not in the future")
    retrieved_at = retrieved_at.astimezone(UTC)
    reported_total, raw_items = data.get("reported_total"), data.get("items")
    if type(reported_total) is not int or not isinstance(raw_items, list):
        raise ValueError("invalid instrument directory count or items")
    rows = cast(list[object], raw_items)
    if not min_items <= reported_total <= _MAX_ITEMS or len(rows) != reported_total:
        raise ValueError("instrument directory count is inconsistent or outside the size limits")
    instruments: list[DirectoryInstrument] = []
    seen: set[str] = set()
    exchanges: set[str] = set()
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise ValueError("invalid instrument directory row")
        row = cast(dict[str, object], raw_row)
        symbol, name = _text(row, "symbol", 9), _text(row, "name", 64)
        exchange = _text(row, "exchange", 2)
        pinyin, initials = _text(row, "pinyin", 256), _text(row, "initials", 64)
        if (not _CANONICAL_SYMBOL.fullmatch(symbol)
                or normalize_a_share_instrument(symbol).value != symbol
                or exchange not in {"SH", "SZ", "BJ"}
                or not symbol.endswith(f".{exchange}")):
            raise ValueError("invalid canonical instrument directory identity")
        if not _ASCII_PINYIN.fullmatch(pinyin) or not _ASCII_PINYIN.fullmatch(initials):
            raise ValueError(
                "instrument directory pinyin and initials must be ASCII letters or digits"
            )
        if symbol in seen:
            raise ValueError("duplicate instrument directory symbol")
        seen.add(symbol)
        exchanges.add(exchange)
        market = cast(Literal["SH", "SZ", "BJ"], exchange)
        instruments.append(DirectoryInstrument(symbol, name, market, pinyin, initials))
    if exchanges != {"SH", "SZ", "BJ"}:
        raise ValueError("instrument directory must cover Shanghai, Shenzhen and Beijing")
    return InstrumentDirectory(tuple(instruments), retrieved_at, reported_total, source_url)
