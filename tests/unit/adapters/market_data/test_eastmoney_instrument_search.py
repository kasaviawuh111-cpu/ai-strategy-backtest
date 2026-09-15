"""Provider identity taxonomy and same-field autocomplete API boundaries."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.a_share_directory import (
    DirectoryInstrument,
    InstrumentDirectory,
)
from ashare_lab.adapters.market_data.eastmoney_instrument_search import (
    EastmoneyInstrumentSearch,
    InstrumentSearchInvalid,
    InstrumentSearchUnavailable,
    parse_a_share_identities,
)
from ashare_lab.api.app import create_app
from ashare_lab.api.routes.instruments import get_instrument_search
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous


def _identity_response() -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "result": [
        {"code": "300059", "shortName": "东方财富", "market": 0, "securityTypeName": "深A"},
    ]})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unique", "later_ambiguity", "still_truncated"])
@pytest.mark.parametrize("old_cache", [False, True])
async def test_mixed_asset_pages_do_not_make_one_a_share_ambiguous_or_hide_later_stocks(
    mode: str, old_cache: bool,
) -> None:
    from ashare_lab.adapters.market_data.eastmoney_instrument_search import (
        SearchInstrument, _CacheEntry,
    )

    pages = []
    fund = {"securityTypeName": "基金"}
    stock = {"code": "300059", "shortName": "东方财富", "market": 0, "securityTypeName": "深A"}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageIndex"])
        pages.append(page)
        rows = [stock] + [fund] * 99 if page == 1 else [fund] * 100
        if page == 2 and mode == "later_ambiguity":
            rows = [{"code": "601318", "shortName": "中国平安", "market": 1, "securityTypeName": "沪A"}]
        elif page == 3 and mode == "unique":
            rows = [fund] * 4
        return httpx.Response(200, json={"code": "0", "pageIndex": page, "result": rows})

    search = EastmoneyInstrumentSearch(transport=httpx.MockTransport(handler))
    if old_cache:
        search._cache["东财"] = _CacheEntry(
            datetime.now(UTC), (SearchInstrument("300059.SZ", "东方财富", "SZ"),), True,
        )
    result = await search.search("东财", limit=3)
    assert pages == list(range(1, {"unique": 3, "later_ambiguity": 2, "still_truncated": 5}[mode] + 1))
    assert [item.symbol for item in result.items] == (
        ["300059.SZ", "601318.SH"] if mode == "later_ambiguity" else ["300059.SZ"]
    )
    assert result.has_more is (mode == "still_truncated")
    count = len(pages)
    await search.search("东财", limit=3)
    assert len(pages) == count  # Cached bounded results do not refetch on every keystroke.


@pytest.mark.asyncio
async def test_failed_later_identity_page_cannot_claim_a_unique_complete_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["pageIndex"] == "2":
            raise httpx.ConnectError("page unavailable")
        return httpx.Response(200, json={"code": "0", "result": [
            {"code": "300059", "shortName": "东方财富", "market": 0, "securityTypeName": "深A"},
            *[{"securityTypeName": "基金"}] * 99,
        ]})

    search = EastmoneyInstrumentSearch(transport=httpx.MockTransport(handler))
    with pytest.raises(InstrumentSearchUnavailable):
        await search.search("东财", limit=3)
    assert "东财" not in search._cache


def test_local_resolver_keeps_shared_initials_ambiguous_without_refresh() -> None:
    def no_network(_request: httpx.Request) -> httpx.Response:
        pytest.fail("local resolution must never issue or schedule a network request")

    search = EastmoneyInstrumentSearch(transport=httpx.MockTransport(no_network))
    retrieved_at = datetime(2024, 1, 1, tzinfo=UTC)
    # Deliberately stale identity data still uses its original acquisition date;
    # resolution must not start autocomplete's async stale-refresh task.
    search._directory = InstrumentDirectory(
        items=(
            DirectoryInstrument("300059.SZ", "甲公司", "SZ", "jiagongsi", "jgs"),
            DirectoryInstrument("600519.SH", "嘉公司", "SH", "jiagongsi", "jgs"),
            DirectoryInstrument("920799.BJ", "甲公司软件", "BJ", "jiagongsiruanjian", "jgsrj"),
        ),
        retrieved_at=retrieved_at, reported_total=3,
        source_url="https://search-codetable.eastmoney.com/codetable/search/web",
    )
    with pytest.raises(InstrumentNameAmbiguous) as caught:
        search.resolve_local_name("JGS")
    assert [item.symbol for item in caught.value.candidates] == ["300059.SZ", "600519.SH"]
    assert all(item.retrieved_at == retrieved_at for item in caught.value.candidates)
    # Unique exact equality takes precedence over longer containing matches.
    assert search.resolve_local_name("甲公司") == "300059.SZ"
    assert search.resolve_local_name("unknown_alias") is None
    assert search.resolve_local_name("300059.SH") is None
    assert not search._refresh_tasks


@pytest.mark.parametrize("query,days_old,total,ambiguous", [
    ("中免", 0, 1, False),
    ("中免", 8, 1, False),
    ("中免", 0, 2, True),
    ("中", 0, 1, True),
    ("6018", 0, 1, True),
])
def test_unique_name_fragment_requires_complete_directory_not_recent_timestamp(
    query: str, days_old: int, total: int, ambiguous: bool,
) -> None:
    search = EastmoneyInstrumentSearch()
    search._directory = InstrumentDirectory(
        items=(DirectoryInstrument("601888.SH", "中国中免", "SH", "zhongguozhongmian", "zgzm"),),
        retrieved_at=datetime.now(UTC) - timedelta(days=days_old), reported_total=total,
        source_url="https://search-codetable.eastmoney.com/codetable/search/web",
    )
    if ambiguous:
        with pytest.raises(InstrumentNameAmbiguous):
            search.resolve_local_name(query)
    else:
        assert search.resolve_local_name(query) == "601888.SH"


def test_local_resolver_without_validated_directory_leaves_provider_fallback_available(
    tmp_path: Path,
) -> None:
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(lambda _: _identity_response()),
        directory_path=tmp_path / "missing.json",
    )
    assert search.resolve_local_name("DFCF") is None


def test_a_share_types_exclude_index_fund_hk_and_keep_beijing_star_st() -> None:
    items, more = parse_a_share_identities({"code": "0", "result": [
        {"code": "000001", "shortName": "平安银行", "market": 0, "securityTypeName": "深A"},
        {"code": "000001", "shortName": "上证指数", "market": 1, "securityTypeName": "指数"},
        {"code": "000001", "shortName": "华夏成长混合", "market": 150, "securityTypeName": "基金"},
        {"code": "601995", "shortName": "中金公司", "market": 1, "securityTypeName": "沪A"},
        {"code": "03908", "shortName": "中金公司", "market": 116, "securityTypeName": "港股"},
        {"code": "689009", "shortName": "九号公司-WD", "market": 1, "securityTypeName": "科创板"},
        {"code": "920799", "shortName": "艾融软件", "market": 0, "securityTypeName": "京A"},
        {"code": "600053", "shortName": "*ST九鼎", "market": 1, "securityTypeName": "沪A"},
    ]})
    assert [item.symbol for item in items] == [
        "000001.SZ", "601995.SH", "689009.SH", "920799.BJ", "600053.SH",
    ]
    assert items[-1].name == "*ST九鼎"
    assert not more


@pytest.mark.parametrize("payload", [
    {"code": "0", "result": None},
    {"code": "1001", "result": []},
    {"code": "0", "result": [
        {"code": "300059", "shortName": "东方财富", "market": 1, "securityTypeName": "沪A"},
    ]},
])
def test_malformed_or_conflicting_identity_fails_closed(payload: object) -> None:
    with pytest.raises(InstrumentSearchInvalid):
        parse_a_share_identities(payload)


@pytest.mark.asyncio
async def test_query_cache_limit_and_suffix_use_provider_identity_only() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": "0", "result": [
            {"code": "600489", "shortName": "中金黄金", "market": 1, "securityTypeName": "沪A"},
            {"code": "601995", "shortName": "中金公司", "market": 1, "securityTypeName": "沪A"},
        ]})

    search = EastmoneyInstrumentSearch(transport=httpx.MockTransport(respond))
    limited = await search.search(" 中金 ", limit=1)
    full = await search.search("中金", limit=8)
    assert len(requests) == 1 and requests[0].url.params["keyword"] == "中金"
    assert "token" not in requests[0].url.params
    assert requests[0].headers["Accept"] == "*/*"
    assert limited.has_more and len(limited.items) == 1
    assert len(full.items) == 2 and not full.has_more
    assert full.retrieved_at == limited.retrieved_at
    # A mismatching suffix is never silently repaired to a different stock.
    assert not (await search.search("300059.SH")).items
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_network_failure_is_not_an_empty_result_or_cached() -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("test timeout", request=request)

    search = EastmoneyInstrumentSearch(transport=httpx.MockTransport(fail))
    for _ in range(2):
        with pytest.raises(InstrumentSearchUnavailable):
            await search.search("中国")
    assert calls == 2


@pytest.mark.asyncio
async def test_disk_cache_restart_fresh_boundary_and_deduplicated_stale_refresh(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    calls = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await release.wait()
        return _identity_response()

    transport = httpx.MockTransport(respond)
    first = EastmoneyInstrumentSearch(transport=transport, cache_dir=tmp_path, clock=lambda: now)
    live = await first.search("dfcf")
    assert live.cache_status == "live"
    assert (tmp_path / "identities-v1.json").exists()
    restarted = EastmoneyInstrumentSearch(
        transport=transport, cache_dir=tmp_path, clock=lambda: now,
    )
    now += timedelta(days=1)
    fresh = await restarted.search("DFCF")
    assert fresh.cache_status == "fresh_cache" and fresh.retrieved_at == live.retrieved_at
    assert calls == 1

    now += timedelta(seconds=1)
    stale = await restarted.search("dfcf")
    assert stale.cache_status == "stale_cache" and stale.items == live.items
    assert stale.retrieved_at == live.retrieved_at
    await started.wait()
    assert (await restarted.search("dfcf")).cache_status == "stale_cache"
    assert calls == 2 and len(restarted._refresh_tasks) == 1
    tasks = tuple(restarted._refresh_tasks.values())
    release.set()
    await asyncio.gather(*tasks)
    assert not restarted._refresh_tasks
    refreshed = await EastmoneyInstrumentSearch(
        transport=transport, cache_dir=tmp_path, clock=lambda: now,
    ).search("dfcf")
    assert refreshed.cache_status == "fresh_cache" and refreshed.retrieved_at == now
    assert calls == 2


@pytest.mark.asyncio
async def test_expired_cache_does_not_hide_failure_or_change_persisted_timestamp(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    failed = False

    def respond(request: httpx.Request) -> httpx.Response:
        if failed:
            raise httpx.ConnectTimeout("test timeout", request=request)
        return _identity_response()

    transport = httpx.MockTransport(respond)
    original = EastmoneyInstrumentSearch(transport=transport, cache_dir=tmp_path, clock=lambda: now)
    await original.search("dfcf")
    path = tmp_path / "identities-v1.json"
    saved = path.read_bytes()
    failed = True
    now += timedelta(days=7, seconds=1)
    restarted = EastmoneyInstrumentSearch(
        transport=transport, cache_dir=tmp_path, clock=lambda: now,
    )
    with pytest.raises(InstrumentSearchUnavailable):
        await restarted.search("dfcf")
    assert path.read_bytes() == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["json", "identity", "oversized"])
async def test_corrupt_cache_falls_back_to_provider(tmp_path: Path, corruption: str) -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    path = tmp_path / "identities-v1.json"
    if corruption == "json":
        path.write_text("{broken json", encoding="utf-8")
    elif corruption == "oversized":
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    else:
        path.write_text(json.dumps({"version": 1, "entries": [{
            "query": "dfcf", "retrieved_at": now.isoformat(), "has_more": False,
            "items": [{"symbol": "300059.SH", "exchange": "SH", "name": "错误身份"}],
        }]}), encoding="utf-8")
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(lambda _: _identity_response()),
        cache_dir=tmp_path, clock=lambda: now,
    )
    result = await search.search("dfcf")
    assert result.cache_status == "live" and result.items[0].symbol == "300059.SZ"
    assert json.loads(path.read_bytes())["entries"][0]["items"][0]["name"] == "东方财富"


@pytest.mark.asyncio
async def test_cache_bounds_fixed_path_and_failed_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_lab.adapters.market_data import eastmoney_instrument_search as module

    monkeypatch.setattr(module, "_MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(module, "_MAX_CACHE_BYTES", 420)
    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(lambda _: _identity_response()), cache_dir=tmp_path,
    )
    for query in ("first", "second", "branch/name"):
        await search.search(query)
    path = tmp_path / "identities-v1.json"
    saved = path.read_bytes()
    assert len(saved) <= 420
    assert len(json.loads(saved)["entries"]) <= 2
    assert set(tmp_path.iterdir()) == {path}

    def fail_replace(source: object, target: object) -> None:
        raise OSError("test unavailable disk")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    assert (await search.search("another")).cache_status == "live"
    assert path.read_bytes() == saved
    assert set(tmp_path.iterdir()) == {path}


def test_api_registered_and_search_errors_are_recoverable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["keyword"] == "失败":
            return httpx.Response(503)
        return httpx.Response(200, json={"code": "0", "result": [
            {"code": "300059", "shortName": "东方财富", "market": 0, "securityTypeName": "深A"},
        ]})

    app = create_app()
    service = EastmoneyInstrumentSearch(transport=httpx.MockTransport(respond))
    app.dependency_overrides[get_instrument_search] = lambda: service
    with TestClient(app) as client:
        result = client.get("/api/v1/market/instruments", params={"query": "dfcf", "limit": 8})
        assert result.status_code == 200
        assert result.json()["source"] == "eastmoney_security_search"
        assert result.json()["items"] == [
            {"symbol": "300059.SZ", "name": "东方财富", "exchange": "SZ"},
        ]
        assert result.json()["retrieved_at"]
        assert client.get("/api/v1/market/instruments?query=%20").status_code == 422
        assert client.get("/api/v1/market/instruments?query=中&limit=1000").status_code == 422
        failed = client.get("/api/v1/market/instruments?query=失败")
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "instrument_search_unavailable"


@pytest.mark.asyncio
async def test_exact_identity_aliases_reuse_persisted_queries_but_new_fuzzy_words_do_not(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        keyword = request.url.params["keyword"]
        requested.append(keyword)
        if keyword == "dfcf":
            return _identity_response()
        rows = [
            {"code": "600028", "shortName": "中国石化", "market": 1, "securityTypeName": "沪A"},
            {"code": "600007", "shortName": "中国国贸", "market": 1, "securityTypeName": "沪A"},
        ] if keyword == "中国" else []
        return httpx.Response(200, json={"code": "0", "result": rows})

    transport = httpx.MockTransport(respond)
    original = EastmoneyInstrumentSearch(transport=transport, cache_dir=tmp_path, clock=lambda: now)
    source_time = (await original.search("dfcf")).retrieved_at
    await original.search("中国")
    path = tmp_path / "identities-v1.json"
    saved = path.read_bytes()
    now += timedelta(days=1)
    restarted = EastmoneyInstrumentSearch(
        transport=transport, cache_dir=tmp_path, clock=lambda: now,
    )
    for alias, symbol in (
        ("300059", "300059.SZ"), ("300059.SZ", "300059.SZ"), ("东方财富", "300059.SZ"),
        ("中国石化", "600028.SH"), ("600028", "600028.SH"), ("600028.SH", "600028.SH"),
    ):
        result = await restarted.search(alias)
        assert result.cache_status == "fresh_cache" and result.retrieved_at == source_time
        assert [item.symbol for item in result.items] == [symbol]
        assert not result.has_more
    assert requested == ["dfcf", "中国"]
    assert path.read_bytes() == saved  # Alias reads neither persist nor reset the source TTL.
    assert not (await restarted.search("300059.SH")).items
    assert requested == ["dfcf", "中国"]
    for fuzzy in ("东方", "中金"):
        assert (await restarted.search(fuzzy)).cache_status == "live"
    assert requested == ["dfcf", "中国", "东方", "中金"]


@pytest.mark.asyncio
async def test_stale_aliases_share_code_refresh_and_expired_aliases_never_mask_failure(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    started, release = asyncio.Event(), asyncio.Event()
    requested: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        keyword = request.url.params["keyword"]
        requested.append(keyword)
        if keyword == "dfcf":
            return _identity_response()
        started.set()
        await release.wait()
        raise httpx.ConnectTimeout("test unavailable provider", request=request)

    search = EastmoneyInstrumentSearch(
        transport=httpx.MockTransport(respond), cache_dir=tmp_path, clock=lambda: now,
    )
    live = await search.search("dfcf")
    path = tmp_path / "identities-v1.json"
    saved = path.read_bytes()
    now += timedelta(hours=25)
    for alias in ("东方财富", "300059", "300059.SZ"):
        result = await search.search(alias)
        assert result.cache_status == "stale_cache" and result.retrieved_at == live.retrieved_at
    await started.wait()
    assert requested == ["dfcf", "300059"] and set(search._refresh_tasks) == {"300059"}
    tasks = tuple(search._refresh_tasks.values())
    release.set()
    await asyncio.gather(*tasks)
    assert not search._refresh_tasks and path.read_bytes() == saved
    now = live.retrieved_at + timedelta(days=7, seconds=1)
    with pytest.raises(InstrumentSearchUnavailable):
        await search.search("300059")
    assert requested == ["dfcf", "300059", "300059"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["renamed", "same_name", "same_timestamp_conflict"])
async def test_aliases_use_latest_name_and_reject_ambiguous_cached_identities(case: str) -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    requested: list[str] = []
    old_name = "同名候选" if case == "same_name" else "旧名称"
    new_name = "同名候选" if case == "same_name" else "新名称"

    def respond(request: httpx.Request) -> httpx.Response:
        keyword = request.url.params["keyword"]
        requested.append(keyword)
        if keyword == "候选一":
            rows = [{"code": "300059", "shortName": old_name,
                     "market": 0, "securityTypeName": "深A"}]
        elif keyword == "候选二":
            rows = [{"code": "600028" if case == "same_name" else "300059", "shortName": new_name,
                     "market": 1 if case == "same_name" else 0,
                     "securityTypeName": "沪A" if case == "same_name" else "深A"}]
        else:
            rows = []
        return httpx.Response(200, json={"code": "0", "result": rows})

    search = EastmoneyInstrumentSearch(transport=httpx.MockTransport(respond), clock=lambda: now)
    await search.search("候选一")
    if case != "same_timestamp_conflict":
        now += timedelta(hours=1)
    await search.search("候选二")
    if case == "renamed":
        latest = await search.search(new_name)
        assert latest.cache_status == "fresh_cache" and latest.items[0].name == new_name
        assert latest.retrieved_at == now and requested == ["候选一", "候选二"]
        result = await search.search(old_name)
        assert requested[-1] == old_name
    else:
        result = await search.search(new_name)
        assert requested[-1] == new_name
    assert result.cache_status == "live" and result.items == ()
    assert len(requested) == 3
