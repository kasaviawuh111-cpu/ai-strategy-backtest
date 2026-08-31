"""Fail-closed resolution from a fixed security master to executable assets."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

from .models import (
    AssetType,
    InstrumentRef,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)


class InstrumentResolutionError(ValueError):
    """Base class for stable, typed identity-resolution failures."""

    code: ClassVar[str] = "instrument_resolution_failed"

    def __init__(self, *, identifier: str, message: str) -> None:
        self.identifier = identifier
        super().__init__(message)


class InstrumentCapabilityUnavailableError(InstrumentResolutionError):
    """The master identified an asset class the product cannot execute."""

    code: ClassVar[str] = "capability_unavailable"

    def __init__(
        self,
        *,
        identifier: str,
        asset_type: SecurityMasterAssetType,
    ) -> None:
        self.asset_type = asset_type
        super().__init__(
            identifier=identifier,
            message=(
                f"{identifier!r} 是指数，当前暂不支持指数回测或下单；请提供希望交易的具体 ETF 代码"
            ),
        )


class InstrumentUnconfirmedError(InstrumentResolutionError):
    """No exact record exists, so no executable asset type may be guessed."""

    code: ClassVar[str] = "instrument_unconfirmed"

    def __init__(self, *, identifier: str) -> None:
        super().__init__(
            identifier=identifier,
            message=f"无法从固定证券主数据确认标的 {identifier!r}",
        )


class InstrumentAmbiguousError(InstrumentResolutionError):
    """A name or local code matches multiple exact security-master records."""

    code: ClassVar[str] = "instrument_ambiguous"

    def __init__(
        self,
        *,
        identifier: str,
        candidate_symbols: tuple[str, ...],
    ) -> None:
        self.candidate_symbols = tuple(sorted(candidate_symbols))
        super().__init__(
            identifier=identifier,
            message=(
                f"证券主数据中的标的 {identifier!r} 不唯一；请提供包含交易所后缀的完整证券代码"
            ),
        )


class InstrumentNotTradableError(InstrumentResolutionError):
    """A confirmed STOCK or ETF is unavailable on the requested date."""

    code: ClassVar[str] = "instrument_not_tradable"

    def __init__(self, *, identifier: str, as_of: object, reason: str) -> None:
        self.as_of = as_of
        self.reason = reason
        super().__init__(
            identifier=identifier,
            message=f"标的 {identifier!r} 在 {as_of!s} 不可交易：{reason}",
        )


class InstrumentResolver:
    """Resolve exact symbols or names without code-prefix heuristics."""

    def __init__(self, security_master: SecurityMasterSnapshot) -> None:
        self._security_master = security_master
        self._by_symbol: dict[str, SecurityMasterRecord] = {
            record.symbol: record for record in security_master.records
        }
        records_by_name: dict[str, list[SecurityMasterRecord]] = {}
        records_by_local_code: dict[str, list[SecurityMasterRecord]] = {}
        for record in security_master.records:
            records_by_name.setdefault(record.name, []).append(record)
            local_code = record.symbol.split(".", maxsplit=1)[0]
            records_by_local_code.setdefault(local_code, []).append(record)
        self._by_name: dict[str, tuple[SecurityMasterRecord, ...]] = {
            name: tuple(records) for name, records in records_by_name.items()
        }
        self._by_local_code: dict[str, tuple[SecurityMasterRecord, ...]] = {
            code: tuple(records) for code, records in records_by_local_code.items()
        }

    @property
    def snapshot_id(self) -> str:
        return self._security_master.snapshot_id

    def resolve(self, identifier: str, *, as_of: date) -> InstrumentRef:
        """Return an exact STOCK/ETF reference or a typed fail-closed error."""

        if type(identifier) is not str or not identifier.strip():
            normalized = "" if type(identifier) is not str else identifier.strip()
            raise InstrumentUnconfirmedError(identifier=normalized)

        raw = identifier.strip()
        record = self._by_symbol.get(raw.upper())
        if record is None:
            candidates = (
                self._by_local_code.get(raw)
                if len(raw) == 6 and raw.isdigit()
                else self._by_name.get(raw)
            )
            if not candidates:
                raise InstrumentUnconfirmedError(identifier=raw)
            if len(candidates) > 1:
                raise InstrumentAmbiguousError(
                    identifier=raw,
                    candidate_symbols=tuple(candidate.symbol for candidate in candidates),
                )
            record = next(iter(candidates))
        if record.asset_type is SecurityMasterAssetType.INDEX:
            raise InstrumentCapabilityUnavailableError(
                identifier=raw,
                asset_type=record.asset_type,
            )

        instrument = _project_executable_record(record)
        return instrument.require_tradable_on(as_of)


def _project_executable_record(record: SecurityMasterRecord) -> InstrumentRef:
    asset_type = {
        SecurityMasterAssetType.STOCK: AssetType.STOCK,
        SecurityMasterAssetType.ETF: AssetType.ETF,
    }.get(record.asset_type)
    if asset_type is None:
        raise InstrumentCapabilityUnavailableError(
            identifier=record.symbol,
            asset_type=record.asset_type,
        )
    return InstrumentRef(
        symbol=record.symbol,
        name=record.name,
        exchange=record.exchange,
        asset_type=asset_type,
        currency=record.currency,
        listing_date=record.listing_date,
        delisting_date=record.delisting_date,
        tradable=record.tradable,
        data_source=record.data_source,
    )


__all__ = [
    "InstrumentAmbiguousError",
    "InstrumentCapabilityUnavailableError",
    "InstrumentNotTradableError",
    "InstrumentResolutionError",
    "InstrumentResolver",
    "InstrumentUnconfirmedError",
]
