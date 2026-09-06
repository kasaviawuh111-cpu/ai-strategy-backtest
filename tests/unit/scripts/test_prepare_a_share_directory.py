"""Generator regressions using synthetic complete responses, with no live calls."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from ashare_lab.adapters.market_data.a_share_directory import load
from scripts.prepare_a_share_directory import QUERY, build_payload, write_directory

pytest.importorskip(
    "pypinyin", reason="directory regeneration requires the instrument-directory extra",
)


def _complete_source() -> dict[str, Any]:
    # Synthetic identities satisfy supported code syntax and all three markets.
    # They are test rows, not an asserted real-world security universe.
    codes = [
        f"{prefix}{number:03d}"
        for prefix in ("600", "601", "603", "000", "001")
        for number in range(1000)
    ] + [f"920{number:03d}" for number in range(567)]
    rows = [{"代码": code, "名称": "测试股份"} for code in codes]
    total = len(rows)
    return {
        "query": QUERY,
        "collected_at_utc": "2025-01-01T00:00:00+00:00",
        "rows": rows,
        "provider_metadata": {
            "selectType": "A_STOCK",
            "responseConditionList": [{"stockCount": total}],
            "allResults": {
                "market": "HSJ",
                "totalCondition": {"stockCount": total, "describe": "全部A股"},
                "responseConditionList": [{"stockCount": total}],
            },
        },
        "raw_provider_response": {"data": {"allResults": {"result": {
            "total": total, "totalRecordCount": total, "dataList": rows,
        }}}},
    }


def test_complete_source_generates_a_production_valid_directory(tmp_path: Path) -> None:
    payload = build_payload(_complete_source())
    output = tmp_path / "directory.json"
    write_directory(payload, output)
    directory = load(output)
    assert len(directory.items) == directory.reported_total == 5567
    assert {item.exchange for item in directory.items} == {"SH", "SZ", "BJ"}
    assert directory.source == "eastmoney_instrument_directory"
    assert directory.retrieved_at.isoformat() == "2025-01-01T00:00:00+00:00"
    assert directory.items[0].pinyin == "ceshigufen"
    assert directory.items[0].initials == "csgf"
    assert len(directory.search("csgf")) == 5567


@pytest.mark.parametrize("narrowing", ["actual_rows", "provider_condition"])
def test_filtered_208_results_cannot_claim_the_5567_stock_universe(narrowing: str) -> None:
    source = _complete_source()
    if narrowing == "actual_rows":
        source["rows"] = source["rows"][:208]
    else:
        source["provider_metadata"]["responseConditionList"][0]["stockCount"] = 208
    with pytest.raises(ValueError, match=r"do not agree|narrowed or truncated"):
        build_payload(source)


def test_invalid_replacement_preserves_the_previous_directory(tmp_path: Path) -> None:
    payload = build_payload(_complete_source())
    output = tmp_path / "directory.json"
    write_directory(payload, output)
    previous = output.read_bytes()
    bad_payload = deepcopy(payload)
    bad_payload["items"][-1] = dict(bad_payload["items"][0])
    with pytest.raises(ValueError, match="duplicate"):
        write_directory(bad_payload, output)
    assert output.read_bytes() == previous
    assert load(output).reported_total == 5567
    assert set(tmp_path.iterdir()) == {output}
