from __future__ import annotations

from email.message import Message
from typing import Self

import pytest

from scripts import container_healthcheck


class _Response:
    status = 200
    headers = Message()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_api_health_prefers_cloudbase_port(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, int]] = []

    def fake_urlopen(url: str, timeout: int) -> _Response:
        observed.append((url, timeout))
        return _Response()

    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("APP_PORT", "8000")
    monkeypatch.setattr(
        container_healthcheck.urllib.request,
        "urlopen",
        fake_urlopen,
    )

    container_healthcheck.check_api()

    assert observed == [("http://127.0.0.1:8080/api/v1/ready", 3)]


@pytest.mark.parametrize("value", ("not-a-port", "0", "65536"))
def test_api_health_rejects_invalid_port(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("PORT", value)

    with pytest.raises(RuntimeError, match="port"):
        container_healthcheck.check_api()
