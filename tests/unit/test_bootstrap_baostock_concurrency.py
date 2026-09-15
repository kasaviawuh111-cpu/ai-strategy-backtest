"""The compiler's current identity chain must not open shared BaoStock sessions."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ashare_lab import bootstrap
from tests.unit.test_bootstrap_instrument_name_chain import _settings_with_master, _stock


def test_compiler_name_chain_resolves_concurrently_without_global_baostock_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoBaoStock:
        def __getattr__(self, name: str) -> object:
            pytest.fail(f"compiler identity lookup must not use BaoStock.{name}")

    monkeypatch.setitem(sys.modules, "baostock", NoBaoStock())
    settings = _settings_with_master(tmp_path, _stock("300059.SZ", "东方财富"))
    remote_ready = threading.Barrier(2)
    calls: list[str] = []
    calls_lock = threading.Lock()
    expected = {"汤姆猫": "300459.SZ", "同花顺": "300033.SZ"}

    def mx(name: str) -> str:
        with calls_lock:
            calls.append(name)
        # Both independent MX requests may run concurrently; no process-global
        # login/logout session or a replacement BaoStock resolver is needed.
        remote_ready.wait(timeout=5)
        return expected[name]

    resolver = bootstrap._build_compiler_instrument_name_resolver(  # pyright: ignore[reportPrivateUsage]
        settings, mx_resolver=mx,
    )

    def resolve(name: str) -> str:
        assert resolver("东方财富") == "300059.SZ"  # Trusted cache never calls MX.
        return resolver(name)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {name: executor.submit(resolve, name) for name in expected}
        assert {name: future.result(timeout=10) for name, future in futures.items()} == expected
    assert sorted(calls) == sorted(expected)
