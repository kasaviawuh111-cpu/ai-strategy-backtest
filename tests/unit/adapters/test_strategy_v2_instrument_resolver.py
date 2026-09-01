from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ashare_lab.adapters.market_data.trusted_snapshots import (
    ServerOwnedTrustedInstrumentResolver,
    TrustedSecurityMasterSnapshotLoader,
    build_trusted_security_master_snapshot,
)
from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentAmbiguousError,
    InstrumentCapabilityUnavailableError,
    InstrumentUnconfirmedError,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.ports.market_data import DateRange

NOW = datetime(2026, 8, 31, 8, tzinfo=UTC)


def _record(
    symbol: str,
    *,
    name: str,
    asset_type: SecurityMasterAssetType,
    tradable: bool = True,
) -> SecurityMasterRecord:
    return SecurityMasterRecord(
        symbol=symbol,
        name=name,
        exchange=Exchange(symbol.rsplit(".", maxsplit=1)[1]),
        asset_type=asset_type,
        listing_date=date(2012, 5, 28),
        tradable=tradable,
        data_source="BaoStock query_stock_basic",
    )


def _baseline(
    root: Path,
    *,
    records: tuple[SecurityMasterRecord, ...] | None = None,
) -> tuple[TrustedSecurityMasterSnapshotLoader, str]:
    built = build_trusted_security_master_snapshot(
        snapshot=SecurityMasterSnapshot(
            snapshot_id="logical:baseline",
            records=records
            or (
                _record(
                    "300059.SZ",
                    name="东方财富",
                    asset_type=SecurityMasterAssetType.STOCK,
                ),
            ),
        ),
        provider="fixture-baseline",
        coverage=DateRange(date(2012, 5, 28), date(2026, 8, 31)),
        generated_at=NOW,
        output_root=root,
    )
    return (
        TrustedSecurityMasterSnapshotLoader(
            root,
            max_age=timedelta(days=1),
            clock=lambda: NOW,
        ),
        built.metadata.snapshot_id,
    )


def test_dynamic_exact_etf_is_published_and_reused_without_provider_recall(
    tmp_path: Path,
) -> None:
    loader, baseline_id = _baseline(tmp_path)
    calls: list[str] = []

    def provider(symbol: str) -> SecurityMasterRecord:
        calls.append(symbol)
        return _record(
            "510300.SH",
            name="沪深300ETF",
            asset_type=SecurityMasterAssetType.ETF,
        )

    first = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=provider,
        clock=lambda: NOW,
    ).resolve_or_prepare("510300.SH", as_of=date(2026, 8, 31))

    assert first.instrument.asset_type is AssetType.ETF
    assert first.security_master.metadata.snapshot_id != baseline_id
    assert calls == ["510300.SH"]

    def forbidden_provider(symbol: str) -> SecurityMasterRecord:
        raise AssertionError(f"provider recalled after restart: {symbol}")

    restarted = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=forbidden_provider,
        clock=lambda: NOW,
    ).resolve_or_prepare("510300.SH", as_of=date(2026, 8, 31))

    assert restarted.instrument == first.instrument
    assert restarted.security_master.metadata.snapshot_id == (
        first.security_master.metadata.snapshot_id
    )


def test_baseline_name_resolves_but_unknown_name_never_invokes_provider(tmp_path: Path) -> None:
    loader, baseline_id = _baseline(tmp_path)
    calls: list[str] = []

    def provider(symbol: str) -> SecurityMasterRecord:
        calls.append(symbol)
        raise AssertionError("free-form name must not reach the exact-symbol provider")

    resolver = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=provider,
        clock=lambda: NOW,
    )

    assert (
        resolver.resolve_or_prepare(
            "东方财富",
            as_of=date(2026, 8, 31),
        ).instrument.symbol
        == "300059.SZ"
    )
    with pytest.raises(InstrumentUnconfirmedError):
        resolver.resolve_or_prepare("不存在的证券", as_of=date(2026, 8, 31))
    assert calls == []


def test_provider_confirmed_index_is_rejected_before_publication(tmp_path: Path) -> None:
    loader, baseline_id = _baseline(tmp_path)
    resolver = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=lambda symbol: _record(
            symbol,
            name="沪深300",
            asset_type=SecurityMasterAssetType.INDEX,
            tradable=False,
        ),
        clock=lambda: NOW,
    )
    before = {path.name for path in tmp_path.iterdir() if path.is_dir()}

    with pytest.raises(InstrumentCapabilityUnavailableError):
        resolver.resolve_or_prepare("000300.SH", as_of=date(2026, 8, 31))

    assert {path.name for path in tmp_path.iterdir() if path.is_dir()} == before


@pytest.mark.parametrize("identifier", ["沪深300ETF", "510300"])
def test_name_or_suffixless_code_uses_choice_candidates_then_authoritative_master(
    tmp_path: Path,
    identifier: str,
) -> None:
    loader, baseline_id = _baseline(tmp_path)
    searches: list[str] = []
    providers: list[str] = []

    def search(value: str) -> tuple[str, ...]:
        searches.append(value)
        return ("510300.SH",)

    def provider(symbol: str) -> SecurityMasterRecord:
        providers.append(symbol)
        return _record(
            symbol,
            name="沪深300ETF",
            asset_type=SecurityMasterAssetType.ETF,
        )

    selection = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=provider,
        candidate_search=search,
        clock=lambda: NOW,
    ).resolve_or_prepare(identifier, as_of=date(2026, 8, 31))

    assert selection.instrument.symbol == "510300.SH"
    assert selection.instrument.asset_type is AssetType.ETF
    assert searches == [identifier]
    assert providers == ["510300.SH"]


def test_search_candidates_are_revalidated_and_ambiguity_requires_clarification(
    tmp_path: Path,
) -> None:
    loader, baseline_id = _baseline(tmp_path)

    def provider(symbol: str) -> SecurityMasterRecord:
        return _record(
            symbol,
            name="同名基金",
            asset_type=SecurityMasterAssetType.ETF,
        )

    resolver = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=provider,
        candidate_search=lambda identifier: ("510300.SH", "159919.SZ"),
        clock=lambda: NOW,
    )

    with pytest.raises(InstrumentAmbiguousError) as captured:
        resolver.resolve_or_prepare("同名基金", as_of=date(2026, 8, 31))

    assert captured.value.candidate_symbols == ("159919.SZ", "510300.SH")


def test_fuzzy_etf_name_stays_ambiguous_and_never_defaults_to_510300(
    tmp_path: Path,
) -> None:
    loader, baseline_id = _baseline(tmp_path)
    names = {
        "510300.SH": "华泰柏瑞沪深300ETF",
        "159919.SZ": "嘉实沪深300ETF",
        "510330.SH": "华夏沪深300ETF",
    }
    resolver = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=lambda symbol: _record(
            symbol,
            name=names[symbol],
            asset_type=SecurityMasterAssetType.ETF,
        ),
        candidate_search=lambda identifier: tuple(names),
        clock=lambda: NOW,
    )

    with pytest.raises(InstrumentAmbiguousError) as captured:
        resolver.resolve_or_prepare("沪深300ETF", as_of=date(2026, 8, 31))

    assert captured.value.candidate_symbols == (
        "159919.SZ",
        "510300.SH",
        "510330.SH",
    )


def test_provider_discovered_exact_company_name_resolves_uniquely(tmp_path: Path) -> None:
    loader, baseline_id = _baseline(
        tmp_path,
        records=(
            _record(
                "600519.SH",
                name="贵州茅台",
                asset_type=SecurityMasterAssetType.STOCK,
            ),
        ),
    )
    resolver = ServerOwnedTrustedInstrumentResolver(
        baseline_loader=loader,
        baseline_snapshot_id=baseline_id,
        output_root=tmp_path,
        record_provider=lambda symbol: _record(
            symbol,
            name="东方财富",
            asset_type=SecurityMasterAssetType.STOCK,
        ),
        candidate_search=lambda identifier: ("300059.SZ",),
        clock=lambda: NOW,
    )

    selection = resolver.resolve_or_prepare("东方财富", as_of=date(2026, 8, 31))

    assert selection.instrument.symbol == "300059.SZ"
