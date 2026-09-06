from __future__ import annotations

from datetime import date

import pytest

from ashare_lab.adapters.market_data.instrument_name_chain import (
    ChainedInstrumentNameResolver,
    InstrumentNameAmbiguousError,
    InstrumentNameProviderUnavailableError,
    InstrumentNameResolverIntegrityError,
    SecurityMasterInstrumentNameResolver,
)
from ashare_lab.domain.instruments import (
    Exchange,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)


def _record(
    symbol: str,
    name: str,
    *,
    asset_type: SecurityMasterAssetType = SecurityMasterAssetType.STOCK,
) -> SecurityMasterRecord:
    return SecurityMasterRecord(
        symbol=symbol,
        name=name,
        exchange=Exchange(symbol.rsplit(".", maxsplit=1)[1]),
        asset_type=asset_type,
        currency="CNY",
        listing_date=date(2010, 1, 1),
        delisting_date=None,
        tradable=True,
        data_source="fixed security-master test snapshot",
    )


def _snapshot(*records: SecurityMasterRecord) -> SecurityMasterSnapshot:
    return SecurityMasterSnapshot(
        snapshot_id="security-master:test",
        records=records,
    )


def test_fixed_security_master_wins_without_calling_network() -> None:
    network_calls: list[str] = []
    fixed = SecurityMasterInstrumentNameResolver(_snapshot(_record("300033.SZ", "同花顺")))

    def network(name: str) -> str:
        network_calls.append(name)
        return "300059.SZ"

    resolver = ChainedInstrumentNameResolver((fixed, network))

    assert resolver(" 同 花 顺 ") == "300033.SZ"
    assert network_calls == []


def test_unconfirmed_fixed_name_falls_back_to_exact_remote_resolver() -> None:
    fixed = SecurityMasterInstrumentNameResolver(_snapshot(_record("300059.SZ", "东方财富")))
    resolver = ChainedInstrumentNameResolver((fixed, lambda _name: "300033.SZ"))

    assert resolver("同花顺") == "300033.SZ"


def test_network_timeout_falls_back_to_existing_baostock_resolver() -> None:
    calls: list[str] = []

    def unavailable(_name: str) -> str:
        raise TimeoutError("remote provider unavailable")

    def baostock(name: str) -> str:
        calls.append(name)
        return "300033.sz"

    resolver = ChainedInstrumentNameResolver((unavailable, baostock))

    assert resolver("同花顺") == "300033.SZ"
    assert calls == ["同花顺"]


def test_explicit_provider_unavailable_error_can_be_mapped_without_hiding_other_bugs() -> None:
    class ProviderUnavailableError(RuntimeError):
        pass

    def unavailable(_name: str) -> str:
        raise ProviderUnavailableError("provider offline")

    resolver = ChainedInstrumentNameResolver(
        (unavailable, lambda _name: "300033.SZ"),
        unavailable_errors=(ProviderUnavailableError,),
    )

    assert resolver("同花顺") == "300033.SZ"


def test_all_unavailable_resolvers_are_not_reported_as_an_unconfirmed_name() -> None:
    def unavailable(_name: str) -> str:
        raise TimeoutError("provider unavailable")

    resolver = ChainedInstrumentNameResolver((unavailable, unavailable))

    with pytest.raises(InstrumentNameProviderUnavailableError):
        resolver("同花顺")


def test_confirmed_misses_remain_unconfirmed_when_no_provider_was_unavailable() -> None:
    fixed = SecurityMasterInstrumentNameResolver(_snapshot(_record("300059.SZ", "东方财富")))

    with pytest.raises(LookupError, match="unconfirmed") as captured:
        ChainedInstrumentNameResolver((fixed,))("同花顺")

    assert not isinstance(captured.value, InstrumentNameProviderUnavailableError)


def test_wrapped_non_network_timeout_is_terminal_when_cause_policy_is_configured() -> None:
    class ProviderUnavailableError(RuntimeError):
        pass

    fallback_calls: list[str] = []

    def malformed(_name: str) -> str:
        try:
            raise ValueError("malformed provider data")
        except ValueError as error:
            raise TimeoutError("legacy provider wrapper") from error

    def fallback(name: str) -> str:
        fallback_calls.append(name)
        return "300033.SZ"

    resolver = ChainedInstrumentNameResolver(
        (malformed, fallback),
        wrapped_unavailable_causes=(ProviderUnavailableError,),
    )

    with pytest.raises(InstrumentNameResolverIntegrityError):
        resolver("同花顺")
    assert fallback_calls == []


def test_wrapped_network_timeout_may_fall_back_when_cause_is_allowlisted() -> None:
    class ProviderUnavailableError(RuntimeError):
        pass

    def unavailable(_name: str) -> str:
        try:
            raise ProviderUnavailableError("network unavailable")
        except ProviderUnavailableError as error:
            raise TimeoutError("legacy provider wrapper") from error

    resolver = ChainedInstrumentNameResolver(
        (unavailable, lambda _name: "300033.SZ"),
        wrapped_unavailable_causes=(ProviderUnavailableError,),
    )

    assert resolver("同花顺") == "300033.SZ"


def test_generic_provider_lookup_failure_is_terminal_not_a_fallback_signal() -> None:
    fallback_calls: list[str] = []

    def ambiguous_or_unconfirmed(_name: str) -> str:
        raise LookupError("instrument name is unconfirmed or ambiguous")

    def fallback(name: str) -> str:
        fallback_calls.append(name)
        return "300033.SZ"

    resolver = ChainedInstrumentNameResolver((ambiguous_or_unconfirmed, fallback))

    with pytest.raises(LookupError, match="unconfirmed or ambiguous"):
        resolver("同花顺")
    assert fallback_calls == []


def test_ambiguous_fixed_master_fails_closed_without_remote_override() -> None:
    remote_calls: list[str] = []
    fixed = SecurityMasterInstrumentNameResolver(
        _snapshot(
            _record("600001.SH", "重名公司"),
            _record("000001.SZ", "重名公司"),
        )
    )

    def remote(name: str) -> str:
        remote_calls.append(name)
        return "600001.SH"

    resolver = ChainedInstrumentNameResolver((fixed, remote))

    with pytest.raises(InstrumentNameAmbiguousError):
        resolver("重名公司")
    assert remote_calls == []


def test_invalid_provider_symbol_fails_closed_without_trying_next_source() -> None:
    fallback_calls: list[str] = []

    def invalid(_name: str) -> str:
        return "not-a-security"

    def fallback(name: str) -> str:
        fallback_calls.append(name)
        return "300033.SZ"

    resolver = ChainedInstrumentNameResolver((invalid, fallback))

    with pytest.raises(InstrumentNameResolverIntegrityError):
        resolver("同花顺")
    assert fallback_calls == []


def test_index_name_is_not_projected_as_a_tradeable_stock() -> None:
    fixed = SecurityMasterInstrumentNameResolver(
        _snapshot(
            _record(
                "000300.SH",
                "沪深300",
                asset_type=SecurityMasterAssetType.INDEX,
            )
        )
    )

    with pytest.raises(LookupError, match="not executable"):
        fixed("沪深300")
