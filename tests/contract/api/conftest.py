from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ashare_lab.api import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(service_version="test-version")) as value:
        yield value


@pytest.fixture
def ready_request() -> dict[str, str]:
    return {
        "utterance": "MACD金叉买入，死叉卖出",
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-27",
    }
