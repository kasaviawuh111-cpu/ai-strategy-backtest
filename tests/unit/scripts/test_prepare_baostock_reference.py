from __future__ import annotations

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prepare_baostock_reference


def test_failed_login_uses_bounded_socket_timeout_and_restores_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float | None] = []

    fake_baostock = SimpleNamespace(
        login=lambda: (
            observed.append(socket.getdefaulttimeout())
            or SimpleNamespace(error_code="10001001", error_msg="network unavailable")
        ),
        logout=lambda: pytest.fail("logout must not run after a failed login"),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_baostock_reference.py",
            "--symbol",
            "688981.SH",
            "--start",
            "2025-03-08",
            "--end",
            "2026-09-04",
            "--output",
            str(tmp_path / "reference.json"),
            "--socket-timeout-seconds",
            "7.5",
        ],
    )
    before = socket.getdefaulttimeout()

    assert prepare_baostock_reference.main() == 1

    assert observed == [7.5]
    assert socket.getdefaulttimeout() == before
