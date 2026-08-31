"""Content-addressed instrument-session reference facts from Parquet.

The file is independent from OHLCV data on purpose: board, ST state, trading
status and daily price bands are reference facts, not values that should be
guessed from bars.  A provider instance is pinned to one file digest.  Every
read verifies that digest before and after DuckDB scans the file.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from ashare_lab.domain.market_data import (
    Board,
    DailyBar,
    InstrumentSession,
    TradingStatus,
)
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, Price

from .local_parquet import normalize_instrument_id

_SCHEMA_VERSION = "parquet-instrument-sessions.v2"
_HASH_CHUNK_BYTES = 1024 * 1024


class SessionReferenceAdapterError(RuntimeError):
    """Base error for canonical instrument-session reference data."""


class SessionReferenceIntegrityError(SessionReferenceAdapterError):
    """The pinned Parquet file was removed or its content changed."""


class SessionReferenceSchemaError(SessionReferenceAdapterError):
    """The file cannot prove exactly one valid session for every input bar."""


class ParquetInstrumentSessionProvider:
    """Resolve canonical A-share session facts from an immutable Parquet file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        if not self._path.is_file():
            raise SessionReferenceIntegrityError(
                f"instrument-session reference file does not exist: {self._path}"
            )
        self._size = self._path.stat().st_size
        self._sha256 = _sha256_file(self._path)
        self._version = f"{_SCHEMA_VERSION}+sha256:{self._sha256}"
        self._validate_projection()
        self._verify_unchanged()

    @property
    def version(self) -> str:
        """Stable rule identity persisted into every run manifest."""

        return self._version

    def sessions_for(self, bars: Sequence[DailyBar]) -> Sequence[InstrumentSession]:
        if not bars:
            return ()
        instrument_id = bars[0].instrument_id
        if any(bar.instrument_id != instrument_id for bar in bars):
            raise SessionReferenceSchemaError("session input bars must contain one instrument")
        bar_dates = tuple(bar.session_date for bar in bars)
        if len(bar_dates) != len(set(bar_dates)):
            raise SessionReferenceSchemaError("session input bars contain duplicate dates")

        sessions = tuple(
            self.sessions_for_period(
                instrument_id,
                start=min(bar_dates),
                end=max(bar_dates),
            )
        )
        session_dates = tuple(item.session_date for item in sessions)
        expected_dates = set(bar_dates)
        actual_dates = set(session_dates)
        if actual_dates != expected_dates:
            missing = sorted(expected_dates - actual_dates)
            unexpected = sorted(actual_dates - expected_dates)
            details: list[str] = []
            if missing:
                details.append("missing=" + ",".join(item.isoformat() for item in missing))
            if unexpected:
                details.append("unexpected=" + ",".join(item.isoformat() for item in unexpected))
            raise SessionReferenceSchemaError(
                f"instrument-session rows must match bars exactly for {instrument_id}: "
                + "; ".join(details)
            )

        by_date = {item.session_date: item for item in sessions}
        return tuple(by_date[item] for item in bar_dates)

    def sessions_for_period(
        self,
        instrument_id: InstrumentId,
        *,
        start: date,
        end: date,
    ) -> Sequence[InstrumentSession]:
        """Read the immutable reference rows for one instrument and date range.

        Snapshot-backed repositories use this method before daily bars are
        handed to the engine.  The caller still compares the returned dates to
        its pinned bar axis, while this adapter proves file identity, schema,
        instrument scope and row uniqueness.
        """

        if start > end:
            raise SessionReferenceSchemaError("session period start must not exceed end")
        canonical_instrument = normalize_instrument_id(instrument_id)
        self._verify_unchanged()

        query = """
            SELECT
                stock_code,
                "date",
                board,
                trading_status,
                previous_close,
                upper_limit,
                lower_limit,
                minimum_buy_quantity,
                buy_quantity_increment,
                price_tick,
                t_plus_one,
                is_st
            FROM read_parquet(?)
            WHERE stock_code = ?
              AND "date" >= ?
              AND "date" <= ?
            ORDER BY "date" ASC
        """
        try:
            with duckdb.connect(database=":memory:") as connection:
                rows = connection.execute(
                    query,
                    [
                        str(self._path),
                        _provider_code(canonical_instrument),
                        start,
                        end,
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise SessionReferenceSchemaError(
                f"cannot read instrument-session reference v2 schema: {exc}"
            ) from exc

        sessions = tuple(self._row_to_session(row, canonical_instrument) for row in rows)
        session_dates = tuple(item.session_date for item in sessions)
        if len(session_dates) != len(set(session_dates)):
            raise SessionReferenceSchemaError(
                f"duplicate instrument-session rows found for {canonical_instrument}"
            )
        self._verify_unchanged()
        return sessions

    def _validate_projection(self) -> None:
        """Fail fast on a missing or incompatible required column."""

        query = """
            SELECT
                stock_code, "date", board, trading_status, previous_close,
                upper_limit, lower_limit, minimum_buy_quantity,
                buy_quantity_increment, price_tick, t_plus_one, is_st
            FROM read_parquet(?)
            LIMIT 0
        """
        try:
            with duckdb.connect(database=":memory:") as connection:
                connection.execute(query, [str(self._path)])
        except duckdb.Error as exc:
            raise SessionReferenceSchemaError(
                f"instrument-session reference does not match v2 schema: {exc}"
            ) from exc

    def _verify_unchanged(self) -> None:
        try:
            current_size = self._path.stat().st_size
        except FileNotFoundError as exc:
            raise SessionReferenceIntegrityError(
                f"instrument-session reference file was removed: {self._path}"
            ) from exc
        if current_size != self._size:
            raise SessionReferenceIntegrityError(
                f"instrument-session reference file size changed: {self._path}"
            )
        if _sha256_file(self._path) != self._sha256:
            raise SessionReferenceIntegrityError(
                f"instrument-session reference file content changed: {self._path}"
            )

    @staticmethod
    def _row_to_session(
        row: tuple[object, ...],
        expected_instrument: InstrumentId,
    ) -> InstrumentSession:
        if len(row) != 12:
            raise SessionReferenceSchemaError(
                "instrument-session projection returned 12-column mismatch"
            )
        (
            raw_code,
            raw_date,
            raw_board,
            raw_status,
            raw_previous_close,
            raw_upper_limit,
            raw_lower_limit,
            raw_minimum_buy_quantity,
            raw_buy_quantity_increment,
            raw_price_tick,
            raw_t_plus_one,
            raw_is_st,
        ) = row
        try:
            row_instrument = normalize_instrument_id(_required_text(raw_code, "stock_code"))
        except Exception as exc:
            raise SessionReferenceSchemaError("invalid session stock_code") from exc
        if row_instrument != expected_instrument:
            raise SessionReferenceSchemaError(
                "instrument-session query returned a different instrument"
            )

        session_date = _coerce_date(raw_date)
        try:
            board = Board(_required_text(raw_board, "board"))
        except ValueError as exc:
            raise SessionReferenceSchemaError(f"invalid board: {raw_board!r}") from exc
        try:
            status = TradingStatus(_required_text(raw_status, "trading_status"))
        except ValueError as exc:
            raise SessionReferenceSchemaError(f"invalid trading_status: {raw_status!r}") from exc

        previous_close = _positive_decimal(raw_previous_close, "previous_close")
        upper_limit = _optional_positive_decimal(raw_upper_limit, "upper_limit")
        lower_limit = _optional_positive_decimal(raw_lower_limit, "lower_limit")
        if (upper_limit is None) != (lower_limit is None):
            raise SessionReferenceSchemaError(
                "upper_limit and lower_limit must both be set or both be absent"
            )
        if (
            upper_limit is not None
            and lower_limit is not None
            and not lower_limit < previous_close < upper_limit
        ):
            raise SessionReferenceSchemaError("price limits must straddle previous_close")

        minimum_buy_quantity = _positive_whole_number(
            raw_minimum_buy_quantity,
            "minimum_buy_quantity",
        )
        buy_quantity_increment = _positive_whole_number(
            raw_buy_quantity_increment,
            "buy_quantity_increment",
        )
        price_tick = _positive_decimal(raw_price_tick, "price_tick")
        t_plus_one = _strict_bool(raw_t_plus_one, "t_plus_one")
        is_st = _strict_bool(raw_is_st, "is_st")
        try:
            return InstrumentSession(
                instrument_id=row_instrument,
                session_date=session_date,
                board=board,
                status=status,
                previous_close=Price(previous_close),
                upper_limit=Price(upper_limit) if upper_limit is not None else None,
                lower_limit=Price(lower_limit) if lower_limit is not None else None,
                minimum_buy_quantity=minimum_buy_quantity,
                buy_quantity_increment=buy_quantity_increment,
                price_tick=price_tick,
                t_plus_one=t_plus_one,
                is_st=is_st,
            )
        except DomainValidationError as exc:
            raise SessionReferenceSchemaError(f"invalid instrument-session row: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise SessionReferenceIntegrityError(
            f"cannot hash instrument-session reference file {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _provider_code(instrument_id: InstrumentId) -> str:
    return str(instrument_id).split(".", maxsplit=1)[0]


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionReferenceSchemaError(f"{field_name} must be a non-empty string")
    return value


def _coerce_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise SessionReferenceSchemaError(f"invalid session date: {value!r}") from exc
    raise SessionReferenceSchemaError(f"invalid session date type: {type(value).__name__}")


def _decimal(value: object, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise SessionReferenceSchemaError(f"{field_name} must be a finite number")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SessionReferenceSchemaError(f"{field_name} must be a finite number") from exc
    if not converted.is_finite():
        raise SessionReferenceSchemaError(f"{field_name} must be a finite number")
    return converted


def _positive_decimal(value: object, field_name: str) -> Decimal:
    converted = _decimal(value, field_name)
    if converted <= 0:
        raise SessionReferenceSchemaError(f"{field_name} must be positive")
    return converted


def _optional_positive_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _positive_decimal(value, field_name)


def _positive_whole_number(value: object, field_name: str) -> int:
    converted = _positive_decimal(value, field_name)
    integral = converted.to_integral_value()
    if converted != integral:
        raise SessionReferenceSchemaError(f"{field_name} must be a positive whole number")
    return int(integral)


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SessionReferenceSchemaError(f"{field_name} must be BOOLEAN")
    return value
