from __future__ import annotations

import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

import pytest

from ashare_lab import bootstrap

_FIELDS = ("code", "code_name", "ipoDate", "outDate", "type", "status")
_RECORDS = {
    "sz.300059": ("sz.300059", "东方财富", "2010-03-19", "", "1", "1"),
    "sz.300459": ("sz.300459", "汤姆猫", "2015-05-15", "", "1", "1"),
}


class _Result:
    fields: Sequence[object] = _FIELDS
    error_code: object = "0"
    error_msg: object = "ok"

    def __init__(self, rows: Sequence[Sequence[object]]) -> None:
        self._rows = tuple(tuple(row) for row in rows)
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> Sequence[object]:
        row = self._rows[self._index]
        self._index += 1
        return row


class _LoginResult:
    error_code: object = "0"
    error_msg: object = "ok"


class _GlobalSessionBaoStock:
    """Fake the process-global connection owned by the real BaoStock module."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._owner: int | None = None
        self._first_login = True
        self._overlap_attempted = threading.Event()
        self.overlap_detected = False
        self.session_events: list[tuple[str, int]] = []

    def login(self) -> _LoginResult:
        owner = threading.get_ident()
        with self._state_lock:
            if self._owner is not None:
                self.overlap_detected = True
                self._overlap_attempted.set()
            self._owner = owner
            self.session_events.append(("login", owner))
            first_login = self._first_login
            self._first_login = False
        if first_login:
            # Give the other already-started worker a deterministic chance to
            # enter login.  With bootstrap's process lock it cannot do so.
            self._overlap_attempted.wait(timeout=0.2)
        return _LoginResult()

    def logout(self) -> _LoginResult:
        owner = threading.get_ident()
        with self._state_lock:
            if self._owner != owner:
                raise OSError(9, "Bad file descriptor")
            self.session_events.append(("logout", owner))
            self._owner = None
        return _LoginResult()

    def query_stock_basic(self, *, code: str = "", code_name: str = "") -> _Result:
        owner = threading.get_ident()
        with self._state_lock:
            if self._owner != owner:
                raise OSError(9, "Bad file descriptor")
        if code:
            record = _RECORDS.get(code.casefold())
            return _Result((record,) if record is not None else ())
        normalized = "".join(code_name.split()).casefold()
        return _Result(
            tuple(record for record in _RECORDS.values() if normalized in record[1].casefold())
        )


def test_compiler_name_resolutions_serialize_global_baostock_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _GlobalSessionBaoStock()
    monkeypatch.setitem(sys.modules, "baostock", client)
    bootstrap._resolve_compiler_instrument_name.cache_clear()
    workers_ready = threading.Barrier(2)

    def resolve(name: str) -> str:
        workers_ready.wait(timeout=1)
        return bootstrap._resolve_compiler_instrument_name(name)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {name: executor.submit(resolve, name) for name in ("东方财富", "汤姆猫")}
            assert {name: future.result(timeout=2) for name, future in futures.items()} == {
                "东方财富": "300059.SZ",
                "汤姆猫": "300459.SZ",
            }
    finally:
        bootstrap._resolve_compiler_instrument_name.cache_clear()

    assert client.overlap_detected is False
    assert [event for event, _ in client.session_events] == ["login", "logout"] * 4
    session_owners = [
        login_owner
        for (_, login_owner), (_, logout_owner) in zip(
            client.session_events[::2],
            client.session_events[1::2],
            strict=True,
        )
        if login_owner == logout_owner
    ]
    assert len(session_owners) == 4
    assert session_owners[0] == session_owners[1]
    assert session_owners[2] == session_owners[3]
    assert session_owners[0] != session_owners[2]
