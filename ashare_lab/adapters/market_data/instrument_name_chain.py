"""Small fail-closed adapters for exact instrument-name resolution.

The compiler consumes the deliberately narrow ``name -> canonical symbol``
callable contract.  This module lets composition roots reuse a fixed security
master first and fall back to existing provider resolvers without teaching the
compiler about providers or adding code/name heuristics.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ashare_lab.domain.instruments import (
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import (
    AshareInstrumentCodeError,
    normalize_a_share_instrument,
)

InstrumentNameResolver = Callable[[str], str]


class InstrumentNameResolutionError(LookupError):
    """Base error intentionally compatible with the existing compiler."""


class InstrumentNameUnconfirmedError(InstrumentNameResolutionError):
    """No exact identity was found by a resolver."""


class InstrumentNameProviderUnavailableError(InstrumentNameResolutionError):
    """No authoritative resolver was reachable for this lookup."""


class InstrumentNameAmbiguousError(InstrumentNameResolutionError):
    """A trusted source contains more than one exact identity."""


class InstrumentNameCapabilityUnavailableError(InstrumentNameResolutionError):
    """The identity is known but its asset class is not executable."""


class InstrumentNameResolverIntegrityError(InstrumentNameResolutionError):
    """A resolver returned an unsafe or malformed identity."""


class SecurityMasterInstrumentNameResolver:
    """Resolve exact names from one immutable security-master snapshot.

    Whitespace and case are normalised only for equality.  No substring,
    similarity, prefix or asset-type inference is performed.
    """

    def __init__(self, snapshot: SecurityMasterSnapshot) -> None:
        by_name: dict[str, list[SecurityMasterRecord]] = {}
        for record in snapshot.records:
            by_name.setdefault(_normalise_name(record.name), []).append(record)
        self._by_name = {name: tuple(records) for name, records in by_name.items()}

    def __call__(self, name: str) -> str:
        normalised = _normalise_name(name)
        if not normalised:
            raise InstrumentNameUnconfirmedError("instrument name is blank")
        matches = self._by_name.get(normalised, ())
        if not matches:
            raise InstrumentNameUnconfirmedError("instrument name is unconfirmed")
        if len(matches) != 1:
            raise InstrumentNameAmbiguousError("instrument name is ambiguous")
        record = matches[0]
        if record.asset_type is SecurityMasterAssetType.INDEX:
            raise InstrumentNameCapabilityUnavailableError(
                "known instrument type is not executable"
            )
        if not record.tradable:
            raise InstrumentNameCapabilityUnavailableError(
                "known instrument is not marked tradable"
            )
        return record.symbol


class ChainedInstrumentNameResolver:
    """Try exact resolvers in order and accept only a canonical A-share code.

    A typed fixed-master miss and explicit provider-unavailable errors may try
    the next source.  A provider's generic ``LookupError`` is terminal because
    existing adapters combine "unconfirmed" and "ambiguous" in that error;
    treating it as a miss could silently let another provider choose one side
    of an ambiguity.  Typed ambiguity/capability/integrity failures are also
    terminal.
    """

    def __init__(
        self,
        resolvers: Sequence[InstrumentNameResolver],
        *,
        unavailable_errors: tuple[type[Exception], ...] = (),
        wrapped_unavailable_causes: tuple[type[Exception], ...] = (),
    ) -> None:
        if not resolvers:
            raise ValueError("at least one instrument-name resolver is required")
        if Exception in unavailable_errors:
            raise TypeError("unavailable_errors must contain specific Exception subclasses")
        if Exception in wrapped_unavailable_causes:
            raise TypeError("wrapped_unavailable_causes must contain specific Exception subclasses")
        self._resolvers = tuple(resolvers)
        self._unavailable_errors = (
            TimeoutError,
            OSError,
            InstrumentNameProviderUnavailableError,
            *unavailable_errors,
        )
        self._wrapped_unavailable_causes = wrapped_unavailable_causes

    def __call__(self, name: str) -> str:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise InstrumentNameUnconfirmedError("instrument name is blank")

        saw_unavailable = False
        for resolver in self._resolvers:
            try:
                value = resolver(cleaned_name)
            except (
                InstrumentNameAmbiguousError,
                InstrumentNameCapabilityUnavailableError,
                InstrumentNameResolverIntegrityError,
            ):
                raise
            except InstrumentNameUnconfirmedError:
                continue
            except self._unavailable_errors as error:
                cause = error.__cause__
                if (
                    self._wrapped_unavailable_causes
                    and isinstance(error, (TimeoutError, OSError))
                    and cause is not None
                    and not isinstance(cause, self._wrapped_unavailable_causes)
                ):
                    raise InstrumentNameResolverIntegrityError(
                        "provider wrapped a non-network failure as unavailable"
                    ) from error
                saw_unavailable = True
                continue
            except LookupError:
                raise

            return _validated_symbol(value)

        if saw_unavailable:
            raise InstrumentNameProviderUnavailableError(
                "all instrument resolvers are unavailable"
            )
        raise InstrumentNameUnconfirmedError("instrument name is unconfirmed")


def _normalise_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(value.split()).casefold()


def _validated_symbol(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstrumentNameResolverIntegrityError("instrument resolver returned a blank symbol")
    try:
        return normalize_a_share_instrument(value).value
    except AshareInstrumentCodeError as error:
        raise InstrumentNameResolverIntegrityError(
            "instrument resolver returned a non-canonical A-share symbol"
        ) from error


__all__ = [
    "ChainedInstrumentNameResolver",
    "InstrumentNameAmbiguousError",
    "InstrumentNameCapabilityUnavailableError",
    "InstrumentNameProviderUnavailableError",
    "InstrumentNameResolutionError",
    "InstrumentNameResolver",
    "InstrumentNameResolverIntegrityError",
    "InstrumentNameUnconfirmedError",
    "SecurityMasterInstrumentNameResolver",
]
