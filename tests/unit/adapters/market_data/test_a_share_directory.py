from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ashare_lab.adapters.market_data.a_share_directory import load


def _payload() -> dict[str, Any]:
    return {
        "version": 1,
        "source": "eastmoney_instrument_directory",
        "source_url": "https://search-codetable.eastmoney.com/codetable/search/web",
        "retrieved_at": "2024-01-01T00:00:00Z",
        "reported_total": 3,
        "items": [
            {"symbol": "300059.SZ", "name": "东方财富", "exchange": "SZ",
             "pinyin": "dongfangcaifu", "initials": "dfcf"},
            {"symbol": "600519.SH", "name": "贵州茅台", "exchange": "SH",
             "pinyin": "guizhoumaotai", "initials": "gzmt"},
            {"symbol": "920799.BJ", "name": "艾融软件", "exchange": "BJ",
             "pinyin": "airongruanjian", "initials": "arrj"},
        ],
    }


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "directory.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("query", [
    "东方", "财富", "300059", "300059.SZ", "300059.sz", "ｄｆｃｆ",
    " ＤＦ ＣＦ　", "dongfangcaifu", "DONGFANG", "东 方财 富", "３０００５９．ｓｚ",
])
def test_offline_search_supports_names_codes_and_pinyin(tmp_path: Path, query: str) -> None:
    directory = load(_write(tmp_path, _payload()), min_items=3)
    assert [item.symbol for item in directory.search(query)] == ["300059.SZ"]
    assert directory.reported_total == 3
    assert directory.retrieved_at.isoformat() == "2024-01-01T00:00:00+00:00"


def test_matching_suffix_never_repairs_a_conflicting_exchange(tmp_path: Path) -> None:
    directory = load(_write(tmp_path, _payload()), min_items=3)
    assert directory.search("300059.SH") == ()
    assert directory.search("920799.BJ")[0].name == "艾融软件"
    assert directory.search("没有这只股票") == ()


def test_ranks_exact_prefix_contains_and_keeps_all_matches(tmp_path: Path) -> None:
    payload = _payload()
    payload["items"][0].update(name="东方", pinyin="dongfang", initials="df")
    payload["items"][1].update(name="东方机械", pinyin="dongfangjixie", initials="dfjx")
    payload["items"][2].update(name="新东方科技", pinyin="xindongfangkeji", initials="xdfkj")
    directory = load(_write(tmp_path, payload), min_items=3)
    assert [item.symbol for item in directory.search("东方")] == [
        "300059.SZ", "600519.SH", "920799.BJ",
    ]
    assert [item.symbol for item in directory.search("df")] == [
        "300059.SZ", "600519.SH", "920799.BJ",
    ]


@pytest.mark.parametrize("query", ["dfcf", "DFCF", " ＤＦ ＣＦ ", "dongfangcaifu",
                                  "东方财富", "300059", "300059.sz"])
def test_exact_aliases_support_name_code_full_pinyin_and_initials(
    tmp_path: Path, query: str,
) -> None:
    directory = load(_write(tmp_path, _payload()), min_items=3)
    assert [item.symbol for item in directory.exact_matches(query)] == ["300059.SZ"]


def test_exact_aliases_never_bind_a_unique_partial_or_wrong_suffix(tmp_path: Path) -> None:
    directory = load(_write(tmp_path, _payload()), min_items=3)
    for query in ("dfc", "dongfang", "东方", "30005", "300059.SH"):
        assert directory.exact_matches(query) == ()


def test_shared_initials_remain_ambiguous_before_result_limiting(tmp_path: Path) -> None:
    payload = _payload()
    payload["items"][1]["initials"] = "dfcf"
    directory = load(_write(tmp_path, payload), min_items=3)
    assert [item.symbol for item in directory.exact_matches("DFCF")] == [
        "300059.SZ", "600519.SH",
    ]


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("source", "model"),
    ("reported_total", True), ("reported_total", 4), ("reported_total", 15001),
    ("items", {}), ("retrieved_at", "2999-01-01T00:00:00Z"),
    ("retrieved_at", "2024-01-01T08:00:00+08:00"),
    ("retrieved_at", "2024-01-01T00:00:00"), ("retrieved_at", "invalid"),
    ("source_url", "file:///tmp/directory.json"),
    ("source_url", "https://user:password@example.com/list"),
])
def test_rejects_invalid_metadata(tmp_path: Path, field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(ValueError):
        load(_write(tmp_path, payload), min_items=3)


@pytest.mark.parametrize("field,value", [
    ("symbol", "300059.SH"), ("symbol", "300059.sz"),
    ("symbol", "３０００５９.SZ"), ("symbol", "510300.SH"),
    ("exchange", "SH"), ("name", ""), ("name", "x" * 65),
    ("name", "东方\n财富"), ("pinyin", "dōngfang"), ("pinyin", "df/cf"),
    ("pinyin", "x" * 257), ("initials", ""), ("initials", "x" * 65),
    ("initials", "东方财富"),
])
def test_rejects_invalid_identity_fields(tmp_path: Path, field: str, value: object) -> None:
    payload = _payload()
    payload["items"][0][field] = value
    with pytest.raises(ValueError):
        load(_write(tmp_path, payload), min_items=3)


@pytest.mark.parametrize("conflicting", [False, True])
def test_rejects_duplicate_symbols_even_if_rows_agree(tmp_path: Path, conflicting: bool) -> None:
    payload = _payload()
    duplicate = dict(payload["items"][0])
    if conflicting:
        duplicate["name"] = "其他名称"
    payload["items"].append(duplicate)
    payload["reported_total"] = 4
    with pytest.raises(ValueError, match="duplicate"):
        load(_write(tmp_path, payload), min_items=3)


def test_rejects_missing_market_and_small_production_directory(tmp_path: Path) -> None:
    path = _write(tmp_path, _payload())
    with pytest.raises(ValueError, match="size limits"):
        load(path)
    payload = _payload()
    payload["items"][2].update(symbol="600036.SH", exchange="SH")
    with pytest.raises(ValueError, match="cover Shanghai"):
        load(_write(tmp_path, payload), min_items=3)


def test_unreadable_broken_or_oversized_directory_has_explicit_error(tmp_path: Path) -> None:
    path = tmp_path / "directory.json"
    with pytest.raises(ValueError, match="unreadable"):
        load(path, min_items=3)
    path.write_bytes(b"{invalid")
    with pytest.raises(ValueError, match="JSON is invalid"):
        load(path, min_items=3)
    path.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="file size limit"):
        load(path, min_items=3)


@pytest.mark.parametrize("query", ["", "　 ", "x" * 257])
def test_rejects_empty_or_unbounded_query(tmp_path: Path, query: str) -> None:
    directory = load(_write(tmp_path, _payload()), min_items=3)
    with pytest.raises(ValueError):
        directory.search(query)
