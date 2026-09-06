"""Offline directory integration with the existing bounded query-cache service."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ashare_lab.adapters.market_data import eastmoney_instrument_search as module
from ashare_lab.adapters.market_data.a_share_directory import load
from ashare_lab.adapters.market_data.eastmoney_instrument_search import EastmoneyInstrumentSearch

_NOW = datetime(2025, 1, 15, 12, tzinfo=UTC)


def _write_directory(
    path: Path, retrieved_at: datetime, *, name: str = "东方财富",
) -> None:
    path.write_text(json.dumps({
        "version": 1,
        "source": "eastmoney_instrument_directory",
        "source_url": "https://search-codetable.eastmoney.com/codetable/search/web",
        "retrieved_at": retrieved_at.isoformat(),
        "reported_total": 3,
        "items": [
            {"symbol": "300059.SZ", "name": name, "exchange": "SZ",
             "pinyin": "dongfangcaifu", "initials": "dfcf"},
            {"symbol": "600958.SH", "name": "东方证券", "exchange": "SH",
             "pinyin": "dongfangzhengquan", "initials": "dfzq"},
            {"symbol": "920799.BJ", "name": "艾融软件", "exchange": "BJ",
             "pinyin": "airongruanjian", "initials": "arrj"},
        ],
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def directory_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Only the explicit loader used by this test is relaxed; production's
    # default size threshold and loader implementation remain intact.
    monkeypatch.setattr(module, "load_directory", lambda path: load(path, min_items=3))
    path = tmp_path / "directory.json"
    _write_directory(path, _NOW - timedelta(hours=1))
    return path


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"fresh directory lookup unexpectedly requested {request.url}")


def _live_identity_response() -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "result": [
        {"code": "300059", "shortName": "东方财富新名", "market": 0,
         "securityTypeName": "深A"},
    ]})


@pytest.mark.asyncio
async def test_new_instances_find_previously_unqueried_words_without_network(
    directory_path: Path, tmp_path: Path,
) -> None:
    transport = httpx.MockTransport(_unexpected_request)
    cache_dir = tmp_path / "query-cache"
    first = EastmoneyInstrumentSearch(
        transport=transport, cache_dir=cache_dir, directory_path=directory_path,
        clock=lambda: _NOW,
    )
    initial = await first.search("dfcf")
    assert initial.items[0].symbol == "300059.SZ"
    restarted = EastmoneyInstrumentSearch(
        transport=transport, cache_dir=cache_dir, directory_path=directory_path,
        clock=lambda: _NOW,
    )
    for query, expected in (
        ("财富", "300059.SZ"), ("dongfangzhengquan", "600958.SH"),
        (" ＡＲ ＲＪ　", "920799.BJ"), ("艾 融", "920799.BJ"),
    ):
        result = await restarted.search(query)
        assert result.items[0].symbol == expected
        assert result.source == "eastmoney_instrument_directory"
        assert result.cache_status == "fresh_cache"
        assert result.retrieved_at == _NOW - timedelta(hours=1)
    assert not cache_dir.exists()


@pytest.mark.asyncio
async def test_directory_limit_and_has_more_use_all_local_matches(directory_path: Path) -> None:
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(_unexpected_request), directory_path=directory_path,
        clock=lambda: _NOW,
    )
    limited = await search.search("东方", limit=1)
    complete = await search.search("东方", limit=8)
    assert len(limited.items) == 1 and limited.has_more
    assert [item.symbol for item in complete.items] == ["300059.SZ", "600958.SH"]
    assert not complete.has_more
    assert limited.items == complete.items[:1]
    assert limited.retrieved_at == complete.retrieved_at


@pytest.mark.asyncio
async def test_directory_exact_codes_preserve_the_verified_market(directory_path: Path) -> None:
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(_unexpected_request), directory_path=directory_path,
        clock=lambda: _NOW,
    )
    for query, expected in (
        ("300059", "300059.SZ"), ("600958.sh", "600958.SH"),
        ("920799.BJ", "920799.BJ"), ("３０００５９．ＳＺ", "300059.SZ"),
    ):
        result = await search.search(query)
        assert [item.symbol for item in result.items] == [expected]
        assert not result.has_more
    assert not (await search.search("300059.SH")).items
    assert not (await search.search("920799.SZ")).items


@pytest.mark.asyncio
async def test_old_directory_returns_stale_identity_and_survives_failed_refresh(
    directory_path: Path,
) -> None:
    retrieved_at = _NOW - timedelta(days=10)
    _write_directory(directory_path, retrieved_at)
    saved = directory_path.read_bytes()
    calls: list[str] = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["keyword"])
        raise httpx.ConnectTimeout("isolated refresh failure", request=request)

    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(fail), directory_path=directory_path, clock=lambda: _NOW,
    )
    for _ in range(2):
        result = await search.search("dfcf")
        assert result.cache_status == "stale_cache"
        assert result.source == "eastmoney_instrument_directory"
        assert result.retrieved_at == retrieved_at
        assert result.items[0].name == "东方财富"
        tasks = tuple(search._refresh_tasks.values())
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        assert not search._refresh_tasks
    assert calls == ["dfcf", "dfcf"]
    assert directory_path.read_bytes() == saved


@pytest.mark.asyncio
async def test_corrupt_initial_directory_falls_back_to_existing_live_search(
    directory_path: Path, tmp_path: Path,
) -> None:
    directory_path.write_text("{broken directory", encoding="utf-8")
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["keyword"])
        return _live_identity_response()

    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(respond), directory_path=directory_path,
        cache_dir=tmp_path / "query-cache", clock=lambda: _NOW,
    )
    live = await search.search("dfcf")
    assert live.source == "eastmoney_security_search" and live.cache_status == "live"
    assert live.items[0].symbol == "300059.SZ" and live.items[0].name == "东方财富新名"
    cached = await search.search("dfcf")
    assert cached.items == live.items and cached.cache_status == "fresh_cache"
    assert calls == ["dfcf"]


@pytest.mark.asyncio
async def test_atomic_directory_replacement_hot_loads_without_restarting_service(
    directory_path: Path, tmp_path: Path,
) -> None:
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(_unexpected_request), directory_path=directory_path,
        clock=lambda: _NOW,
    )
    original = await search.search("300059")
    assert original.items[0].name == "东方财富"
    # A malformed update must leave the already validated directory usable.
    directory_path.write_text("{broken update", encoding="utf-8")
    retained = await search.search("300059")
    assert retained.items == original.items and retained.retrieved_at == original.retrieved_at

    replacement = tmp_path / "replacement.json"
    _write_directory(replacement, _NOW, name="东方财富新名称")
    os.replace(replacement, directory_path)
    updated = await search.search("新名称")
    assert updated.items[0].name == "东方财富新名称"
    assert updated.items[0].symbol == "300059.SZ"
    assert updated.source == "eastmoney_instrument_directory"
    assert updated.retrieved_at == _NOW
    assert original.items[0].name == "东方财富"


@pytest.mark.asyncio
async def test_expired_newer_query_cache_falls_back_to_the_older_directory(
    directory_path: Path, tmp_path: Path,
) -> None:
    directory_time = _NOW - timedelta(days=11)
    _write_directory(directory_path, directory_time)
    now = _NOW - timedelta(days=9)
    calls: list[str] = []
    fail_refresh = False

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["keyword"])
        if fail_refresh:
            raise httpx.ConnectTimeout("isolated refresh failure", request=request)
        return _live_identity_response()

    cache_dir = tmp_path / "query-cache"
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(respond), cache_dir=cache_dir,
        directory_path=directory_path, clock=lambda: now,
    )
    initial = await search.search("dfcf")
    assert initial.source == "eastmoney_instrument_directory"
    await asyncio.gather(*tuple(search._refresh_tasks.values()))
    newer = await search.search("dfcf")
    assert newer.source == "eastmoney_security_search"
    assert newer.cache_status == "fresh_cache" and newer.items[0].name == "东方财富新名"
    assert newer.retrieved_at > directory_time
    assert calls == ["dfcf"]

    now = _NOW
    fail_refresh = True
    restarted = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(respond), cache_dir=cache_dir,
        directory_path=directory_path, clock=lambda: now,
    )
    for instance in (search, restarted):
        fallback = await instance.search("dfcf")
        assert fallback.source == "eastmoney_instrument_directory"
        assert fallback.cache_status == "stale_cache"
        assert fallback.retrieved_at == directory_time
        assert fallback.items[0].name == "东方财富"
        await asyncio.gather(*tuple(instance._refresh_tasks.values()))
    assert calls == ["dfcf", "dfcf", "dfcf"]
