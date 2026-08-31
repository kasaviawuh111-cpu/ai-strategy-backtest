from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentAmbiguousError,
    InstrumentCapabilityUnavailableError,
    InstrumentNotTradableError,
    InstrumentRef,
    InstrumentResolver,
    InstrumentUnconfirmedError,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)


def _record(
    *,
    symbol: str,
    name: str,
    exchange: Exchange,
    asset_type: SecurityMasterAssetType,
    listing_date: date,
    delisting_date: date | None = None,
    tradable: bool = True,
    data_source: str = "choice.security_master",
) -> SecurityMasterRecord:
    return SecurityMasterRecord(
        symbol=symbol,
        name=name,
        exchange=exchange,
        asset_type=asset_type,
        currency="CNY",
        listing_date=listing_date,
        delisting_date=delisting_date,
        tradable=tradable,
        data_source=data_source,
    )


@pytest.fixture
def security_master() -> SecurityMasterSnapshot:
    return SecurityMasterSnapshot(
        snapshot_id="security-master:2026-08-29",
        records=(
            _record(
                symbol="600519.SH",
                name="贵州茅台",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.STOCK,
                listing_date=date(2001, 8, 27),
            ),
            _record(
                symbol="510300.SH",
                name="沪深300ETF",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.ETF,
                listing_date=date(2012, 5, 28),
            ),
            _record(
                symbol="000300.SH",
                name="沪深300",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.INDEX,
                listing_date=date(2005, 4, 8),
                tradable=False,
            ),
            _record(
                symbol="600001.SH",
                name="已退市样例",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.STOCK,
                listing_date=date(1998, 1, 1),
                delisting_date=date(2020, 12, 31),
                tradable=True,
            ),
            _record(
                symbol="159919.SZ",
                name="暂停交易ETF样例",
                exchange=Exchange.SZ,
                asset_type=SecurityMasterAssetType.ETF,
                listing_date=date(2012, 5, 28),
                tradable=False,
            ),
        ),
    )


def test_explicit_security_master_stock_resolves_as_stock(
    security_master: SecurityMasterSnapshot,
) -> None:
    instrument = InstrumentResolver(security_master).resolve(" 600519.sh ", as_of=date(2024, 1, 2))

    assert instrument == InstrumentRef(
        symbol="600519.SH",
        name="贵州茅台",
        exchange=Exchange.SH,
        asset_type=AssetType.STOCK,
        currency="CNY",
        listing_date=date(2001, 8, 27),
        delisting_date=None,
        tradable=True,
        data_source="choice.security_master",
    )


def test_bare_local_code_resolves_only_by_exact_unique_master_record(
    security_master: SecurityMasterSnapshot,
) -> None:
    instrument = InstrumentResolver(security_master).resolve("600519", as_of=date(2024, 1, 2))

    assert instrument.symbol == "600519.SH"
    assert instrument.asset_type is AssetType.STOCK


def test_explicit_security_master_etf_resolves_and_passes_as_of_validation(
    security_master: SecurityMasterSnapshot,
) -> None:
    resolver = InstrumentResolver(security_master)

    instrument = resolver.resolve("510300.SH", as_of=date(2024, 1, 2))

    assert instrument.asset_type is AssetType.ETF
    assert instrument.symbol == "510300.SH"
    assert instrument.require_tradable_on(date(2024, 1, 2)) is instrument


@pytest.mark.parametrize("identifier", ["000300.SH", "沪深300"])
def test_index_code_or_name_is_explicitly_capability_unavailable(
    security_master: SecurityMasterSnapshot,
    identifier: str,
) -> None:
    resolver = InstrumentResolver(security_master)

    with pytest.raises(InstrumentCapabilityUnavailableError) as captured:
        resolver.resolve(identifier, as_of=date(2024, 1, 2))

    assert captured.value.code == "capability_unavailable"
    assert captured.value.asset_type is SecurityMasterAssetType.INDEX
    assert "具体 ETF 代码" in str(captured.value)


def test_index_is_never_automatically_mapped_to_etf(
    security_master: SecurityMasterSnapshot,
) -> None:
    resolver = InstrumentResolver(security_master)

    with pytest.raises(InstrumentCapabilityUnavailableError) as captured:
        resolver.resolve("沪深300", as_of=date(2024, 1, 2))

    assert captured.value.identifier == "沪深300"
    assert "510300" not in str(captured.value)


@pytest.mark.parametrize("identifier", ["600000.SH", "510500.SH", "不存在的证券"])
def test_unknown_identifier_cannot_masquerade_as_stock_or_etf(
    security_master: SecurityMasterSnapshot,
    identifier: str,
) -> None:
    with pytest.raises(InstrumentUnconfirmedError) as captured:
        InstrumentResolver(security_master).resolve(identifier, as_of=date(2024, 1, 2))

    assert captured.value.code == "instrument_unconfirmed"
    assert captured.value.identifier == identifier


def test_listing_and_delisting_dates_are_enforced(
    security_master: SecurityMasterSnapshot,
) -> None:
    resolver = InstrumentResolver(security_master)

    with pytest.raises(InstrumentNotTradableError, match="尚未上市"):
        resolver.resolve("510300.SH", as_of=date(2012, 5, 27))
    assert resolver.resolve("600001.SH", as_of=date(2020, 12, 30)).symbol == "600001.SH"
    with pytest.raises(InstrumentNotTradableError, match="已经退市"):
        resolver.resolve("600001.SH", as_of=date(2021, 1, 1))


def test_security_master_tradable_flag_is_enforced(
    security_master: SecurityMasterSnapshot,
) -> None:
    with pytest.raises(InstrumentNotTradableError, match="不可交易"):
        InstrumentResolver(security_master).resolve("159919.SZ", as_of=date(2024, 1, 2))


def test_instrument_ref_rejects_index_and_exchange_suffix_conflicts() -> None:
    common = {
        "symbol": "000300.SH",
        "name": "沪深300",
        "exchange": Exchange.SH,
        "currency": "CNY",
        "listing_date": date(2005, 4, 8),
        "delisting_date": None,
        "tradable": False,
        "data_source": "choice.security_master",
    }

    with pytest.raises(ValidationError):
        InstrumentRef(asset_type="INDEX", **common)
    with pytest.raises(ValidationError, match="symbol suffix"):
        InstrumentRef(
            symbol="510300.SH",
            name="沪深300ETF",
            exchange=Exchange.SZ,
            asset_type=AssetType.ETF,
            currency="CNY",
            listing_date=date(2012, 5, 28),
            delisting_date=None,
            tradable=True,
            data_source="choice.security_master",
        )


def test_instrument_ref_is_frozen_extra_forbidden_and_serializes_deterministically() -> None:
    instrument = InstrumentRef(
        symbol="510300.SH",
        name="沪深300ETF",
        exchange=Exchange.SH,
        asset_type=AssetType.ETF,
        currency="CNY",
        listing_date=date(2012, 5, 28),
        delisting_date=None,
        tradable=True,
        data_source="choice.security_master",
    )

    assert instrument.model_dump(mode="json") == {
        "symbol": "510300.SH",
        "name": "沪深300ETF",
        "exchange": "SH",
        "asset_type": "ETF",
        "currency": "CNY",
        "listing_date": "2012-05-28",
        "delisting_date": None,
        "tradable": True,
        "data_source": "choice.security_master",
    }
    with pytest.raises(ValidationError, match="frozen"):
        instrument.name = "改名"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        InstrumentRef.model_validate({**instrument.model_dump(), "unexpected": True})


def test_security_master_snapshot_rejects_duplicate_symbol() -> None:
    stock = _record(
        symbol="600519.SH",
        name="贵州茅台",
        exchange=Exchange.SH,
        asset_type=SecurityMasterAssetType.STOCK,
        listing_date=date(2001, 8, 27),
    )

    with pytest.raises(ValidationError, match="duplicate security-master symbol"):
        SecurityMasterSnapshot(snapshot_id="security-master:duplicate", records=(stock, stock))


def test_duplicate_display_name_is_allowed_in_master_but_fails_closed_on_resolution() -> None:
    first = _record(
        symbol="123456.SH",
        name="历史重名证券",
        exchange=Exchange.SH,
        asset_type=SecurityMasterAssetType.STOCK,
        listing_date=date(2001, 1, 1),
    )
    second = _record(
        symbol="654321.SZ",
        name="历史重名证券",
        exchange=Exchange.SZ,
        asset_type=SecurityMasterAssetType.STOCK,
        listing_date=date(2002, 1, 1),
    )
    master = SecurityMasterSnapshot(
        snapshot_id="security-master:duplicate-name",
        records=(first, second),
    )

    with pytest.raises(InstrumentAmbiguousError) as captured:
        InstrumentResolver(master).resolve("历史重名证券", as_of=date(2024, 1, 2))

    assert captured.value.code == "instrument_ambiguous"
    assert captured.value.candidate_symbols == ("123456.SH", "654321.SZ")


def test_ambiguous_bare_local_code_fails_closed_without_exchange_inference() -> None:
    first = _record(
        symbol="123456.SH",
        name="沪市样例",
        exchange=Exchange.SH,
        asset_type=SecurityMasterAssetType.STOCK,
        listing_date=date(2001, 1, 1),
    )
    second = _record(
        symbol="123456.SZ",
        name="深市样例",
        exchange=Exchange.SZ,
        asset_type=SecurityMasterAssetType.STOCK,
        listing_date=date(2002, 1, 1),
    )
    master = SecurityMasterSnapshot(
        snapshot_id="security-master:duplicate-local-code",
        records=(first, second),
    )

    with pytest.raises(InstrumentAmbiguousError) as captured:
        InstrumentResolver(master).resolve("123456", as_of=date(2024, 1, 2))

    assert captured.value.candidate_symbols == ("123456.SH", "123456.SZ")


def test_security_master_dates_must_be_coherent() -> None:
    with pytest.raises(ValidationError, match="delisting_date cannot precede listing_date"):
        _record(
            symbol="600001.SH",
            name="日期错误",
            exchange=Exchange.SH,
            asset_type=SecurityMasterAssetType.STOCK,
            listing_date=date(2024, 1, 2),
            delisting_date=date(2024, 1, 1),
        )
