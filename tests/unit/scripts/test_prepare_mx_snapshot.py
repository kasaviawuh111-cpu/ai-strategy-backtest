from __future__ import annotations

import pytest

from scripts.prepare_mx_snapshot import _one_table


def test_one_table_selects_unique_longest_complete_table_for_same_symbol() -> None:
    selected = _one_table(
        [
            {
                "code": "300059.SZ",
                "entityCodes": ["300059.SZ"],
                "rawTable": {"headName": ["2026-09-04"]},
            },
            {
                "code": "300059.SZ",
                "entityCodes": ["300059.SZ"],
                "rawTable": {"headName": ["2026-09-03", "2026-09-04"]},
            },
        ],
        "300059.SZ",
    )

    assert selected["rawTable"]["headName"] == ["2026-09-03", "2026-09-04"]


def test_one_table_keeps_equal_length_matches_fail_closed() -> None:
    with pytest.raises(ValueError, match="exactly one complete"):
        _one_table(
            [
                {
                    "code": "300059.SZ",
                    "entityCodes": ["300059.SZ"],
                    "rawTable": {"headName": ["2026-09-04"]},
                },
                {
                    "code": "300059.SZ",
                    "entityCodes": ["300059.SZ"],
                    "rawTable": {"headName": ["2026-09-04"]},
                },
            ],
            "300059.SZ",
        )
