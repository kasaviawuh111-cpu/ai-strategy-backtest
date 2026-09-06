from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ashare_lab import bootstrap
from ashare_lab.adapters.market_data.instrument_name_chain import (
    InstrumentNameProviderUnavailableError,
    InstrumentNameResolverIntegrityError,
)
from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderDataError
from ashare_lab.adapters.market_data.trusted_snapshots import (
    build_trusted_security_master_snapshot,
)
from ashare_lab.domain.instruments import (
    Exchange,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.ports.market_data import DateRange
from ashare_lab.settings import AppSettings

NameResolver = Callable[[str], str]


def _build_resolver(
    settings: AppSettings,
    *,
    mx_resolver: NameResolver | None,
) -> NameResolver:
    return bootstrap._build_compiler_instrument_name_resolver(  # pyright: ignore[reportPrivateUsage]
        settings,
        mx_resolver=mx_resolver,
    )


def _settings_with_master(
    tmp_path: Path,
    *records: SecurityMasterRecord,
) -> AppSettings:
    root = tmp_path / "master"
    built = build_trusted_security_master_snapshot(
        snapshot=SecurityMasterSnapshot(
            snapshot_id="security-master:logical-source",
            records=records,
        ),
        provider="fixed-master-test",
        coverage=DateRange(date(2010, 1, 1), date(2026, 9, 1)),
        generated_at=datetime.now(UTC),
        output_root=root,
    )
    return AppSettings(
        app_env="test",
        strategy_v2_security_master_root=root,
        strategy_v2_security_master_snapshot_id=built.metadata.snapshot_id,
    )


def _stock(symbol: str, name: str) -> SecurityMasterRecord:
    return SecurityMasterRecord(
        symbol=symbol,
        name=name,
        exchange=Exchange(symbol.rsplit(".", maxsplit=1)[1]),
        asset_type=SecurityMasterAssetType.STOCK,
        listing_date=date(2010, 1, 1),
        tradable=True,
        data_source="fixed security master",
    )


def test_bootstrap_name_chain_prefers_fixed_master_without_remote_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    settings = _settings_with_master(tmp_path, _stock("300033.SZ", "同花顺"))

    def mx(name: str) -> str:
        calls.append(("mx", name))
        return "300059.SZ"

    def baostock(name: str) -> str:
        calls.append(("baostock", name))
        return "300059.SZ"

    monkeypatch.setattr(bootstrap, "_search_strategy_v2_baostock_candidates", baostock)
    resolver = _build_resolver(
        settings,
        mx_resolver=mx,
    )

    assert resolver("同花顺") == "300033.SZ"
    assert calls == []


def test_bootstrap_name_chain_does_not_switch_vendor_when_mx_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    settings = _settings_with_master(tmp_path, _stock("300059.SZ", "东方财富"))

    def mx(name: str) -> str:
        calls.append(("mx", name))
        raise TimeoutError("mx unavailable")

    def baostock(name: str) -> str:
        calls.append(("baostock", name))
        return "300033.SZ"

    monkeypatch.setattr(bootstrap, "_search_strategy_v2_baostock_candidates", baostock)
    resolver = _build_resolver(
        settings,
        mx_resolver=mx,
    )

    with pytest.raises(InstrumentNameProviderUnavailableError):
        resolver("同花顺")
    assert calls == [("mx", "同花顺")]


def test_bootstrap_name_chain_does_not_override_mx_ambiguity_with_baostock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    settings = AppSettings(app_env="test")

    def mx(name: str) -> str:
        calls.append(("mx", name))
        raise LookupError("unconfirmed")

    def baostock(name: str) -> str:
        calls.append(("baostock", name))
        return "300033.SZ"

    monkeypatch.setattr(bootstrap, "_search_strategy_v2_baostock_candidates", baostock)
    resolver = _build_resolver(
        settings,
        mx_resolver=mx,
    )

    with pytest.raises(LookupError, match="unconfirmed"):
        resolver("同花顺")
    assert calls == [("mx", "同花顺")]


def test_bootstrap_name_chain_without_mx_does_not_call_baostock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def baostock(name: str) -> str:
        calls.append(name)
        return "300033.SZ"

    monkeypatch.setattr(bootstrap, "_search_strategy_v2_baostock_candidates", baostock)
    resolver = _build_resolver(
        AppSettings(app_env="test"),
        mx_resolver=None,
    )

    with pytest.raises(TimeoutError, match="东方财富"):
        resolver("同花顺")
    assert calls == []


def test_bootstrap_name_chain_does_not_treat_mx_data_error_as_network_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baostock_calls: list[str] = []

    def mx(_name: str) -> str:
        try:
            raise MxSaasProviderDataError("unsafe response")
        except MxSaasProviderDataError as error:
            raise TimeoutError("legacy MX wrapper") from error

    def baostock(name: str) -> str:
        baostock_calls.append(name)
        return "300033.SZ"

    monkeypatch.setattr(bootstrap, "_search_strategy_v2_baostock_candidates", baostock)
    resolver = _build_resolver(
        AppSettings(app_env="test"),
        mx_resolver=mx,
    )

    with pytest.raises(InstrumentNameResolverIntegrityError):
        resolver("同花顺")
    assert baostock_calls == []
