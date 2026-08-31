"""Strict v2 security-master value objects for executable A-share instruments.

These models intentionally do not infer a security type from a numeric code.
An executable :class:`InstrumentRef` can only be projected from an explicit,
versioned security-master record.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Exchange(StrEnum):
    """Mainland exchange suffixes used by canonical instrument symbols."""

    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class AssetType(StrEnum):
    """Asset types that the current product may actually trade."""

    STOCK = "STOCK"
    ETF = "ETF"


class SecurityMasterAssetType(StrEnum):
    """Classifications a security master may know about.

    ``INDEX`` is deliberately present only at the reference-data boundary so
    the resolver can return a precise capability error.  It is not accepted by
    :class:`InstrumentRef` and therefore cannot enter an executable strategy.
    """

    STOCK = "STOCK"
    ETF = "ETF"
    INDEX = "INDEX"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _SecurityIdentity(_FrozenModel):
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    name: str = Field(min_length=1, max_length=128)
    exchange: Exchange
    currency: Literal["CNY"] = "CNY"
    listing_date: date
    delisting_date: date | None = None
    tradable: bool
    data_source: str = Field(min_length=1, max_length=128)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def identity_is_coherent(self) -> Self:
        suffix = self.symbol.rsplit(".", maxsplit=1)[1]
        if suffix != self.exchange.value:
            raise ValueError(
                f"symbol suffix {suffix!r} does not match exchange {self.exchange.value!r}"
            )
        if self.delisting_date is not None and self.delisting_date < self.listing_date:
            raise ValueError("delisting_date cannot precede listing_date")
        return self


class SecurityMasterRecord(_SecurityIdentity):
    """One immutable classification supplied by an explicit security master."""

    asset_type: SecurityMasterAssetType


class SecurityMasterSnapshot(_FrozenModel):
    """A fixed, deterministic collection used for all identity resolution."""

    schema_version: Literal["security-master.v2"] = "security-master.v2"
    snapshot_id: str = Field(min_length=1, max_length=160)
    records: tuple[SecurityMasterRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> Self:
        symbols: set[str] = set()
        for record in self.records:
            if record.symbol in symbols:
                raise ValueError(f"duplicate security-master symbol: {record.symbol!r}")
            symbols.add(record.symbol)
        return self


class InstrumentRef(_SecurityIdentity):
    """A confirmed, executable STOCK or ETF identity.

    Creation alone proves structural validity.  ``require_tradable_on`` also
    applies listing-window and master-status gates for a requested date.
    """

    asset_type: AssetType

    def require_tradable_on(self, as_of: date) -> Self:
        """Return this reference after proving it may trade on ``as_of``."""

        # Imported lazily to keep the value model independent from resolution.
        from .resolver import InstrumentNotTradableError

        if type(as_of) is not date:
            raise InstrumentNotTradableError(
                identifier=self.symbol,
                as_of=as_of,
                reason="交易日期必须是明确的 date",
            )
        if as_of < self.listing_date:
            raise InstrumentNotTradableError(
                identifier=self.symbol,
                as_of=as_of,
                reason="指定日期尚未上市",
            )
        if self.delisting_date is not None and as_of > self.delisting_date:
            raise InstrumentNotTradableError(
                identifier=self.symbol,
                as_of=as_of,
                reason="指定日期已经退市",
            )
        if not self.tradable:
            raise InstrumentNotTradableError(
                identifier=self.symbol,
                as_of=as_of,
                reason="证券主数据标记为不可交易",
            )
        return self


__all__ = [
    "AssetType",
    "Exchange",
    "InstrumentRef",
    "SecurityMasterAssetType",
    "SecurityMasterRecord",
    "SecurityMasterSnapshot",
]
