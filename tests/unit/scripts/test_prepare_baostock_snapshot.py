from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from scripts import prepare_baostock_snapshot


class _Result:
    def __init__(self, rows: list[list[str]]) -> None:
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "preclose",
            "volume",
            "amount",
            "turn",
            "tradestatus",
            "isST",
        ]
        self._rows = rows
        self._index = 0

    def next(self) -> bool:
        if self._index >= len(self._rows):
            return False
        self._index += 1
        return True

    def get_row_data(self) -> list[str]:
        return self._rows[self._index - 1]


class _BaoStock:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.logged_out = False

    @staticmethod
    def login() -> SimpleNamespace:
        # The real SDK writes connection notices to stdout.  The publishing
        # CLI has a machine-readable stdout contract, so those notices must
        # not contaminate its terminal JSON object.
        print("login success!")
        return SimpleNamespace(error_code="0", error_msg="success")

    def logout(self) -> None:
        print("logout success!")
        self.logged_out = True

    def query_history_k_data_plus(self, **kwargs: str) -> _Result:
        self.calls.append(kwargs)
        adjusted = kwargs["adjustflag"] == "1"
        values = {
            "2024-01-02": (
                ("2", "2.4", "1.8", "2.2", "2") if adjusted else ("10", "12", "9", "11", "10")
            ),
            "2024-01-03": (
                ("2.2", "2.5", "2.1", "2.4", "2.2")
                if adjusted
                else ("11", "12.5", "10.5", "12", "11")
            ),
            "2024-01-04": (
                ("2.4", "2.6", "2.3", "2.5", "2.4")
                if adjusted
                else ("12", "13", "11.5", "12.5", "12")
            ),
        }
        rows: list[list[str]] = []
        for trading_day, (open_, high, low, close, preclose) in values.items():
            if kwargs["start_date"] <= trading_day <= kwargs["end_date"]:
                rows.append(
                    [
                        trading_day,
                        open_,
                        high,
                        low,
                        close,
                        preclose,
                        "1000",
                        "12500" if trading_day == "2024-01-04" else "11000",
                        "1.100000",
                        "1",
                        "0",
                    ]
                )
        return _Result(rows)


def test_main_publishes_a_provider_neutral_baostock_technical_snapshot(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    client = _BaoStock()
    args = argparse.Namespace(
        symbol="300059.SZ",
        start=date(2024, 1, 2),
        end=date(2024, 1, 4),
        prefix_end=date(2024, 1, 3),
        listing_date=date(2010, 3, 19),
        board="chinext",
        output_root=tmp_path / "technical",
        session_reference_json=tmp_path / "reference.json",
        corporate_actions_json=tmp_path / "actions.json",
    )
    captured: dict[str, object] = {}
    args.session_reference_json.write_text("{}", encoding="utf-8")
    args.corporate_actions_json.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(prepare_baostock_snapshot, "_parse_args", lambda: args)
    monkeypatch.setitem(sys.modules, "baostock", client)
    monkeypatch.setattr(
        prepare_baostock_snapshot,
        "load_session_reference_source",
        lambda *_args, **_kwargs: (
            (
                {"date": "2024-01-02", "preclose": "10", "tradestatus": "1", "isST": "0"},
                {"date": "2024-01-03", "preclose": "11", "tradestatus": "1", "isST": "0"},
                {"date": "2024-01-04", "preclose": "12", "tradestatus": "1", "isST": "0"},
            ),
            {"provider": "BaoStock Python API", "normalizedResponseSha256": "a" * 64},
            {"audit": "reference"},
        ),
    )
    monkeypatch.setattr(
        prepare_baostock_snapshot,
        "load_corporate_action_source",
        lambda *_args, **_kwargs: ((), {"provider": "Eastmoney"}, {"audit": "actions"}),
    )

    def publish(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            snapshot_id=f"technical:{'f' * 64}",
            path=tmp_path / "technical" / ("f" * 64),
            manifest={"provider": "BaoStock Python API", "rowCounts": {"execution": 3}},
        )

    monkeypatch.setattr(prepare_baostock_snapshot, "build_daily_research_snapshot", publish)

    assert prepare_baostock_snapshot.main() == 0
    assert client.logged_out is True
    assert [call["adjustflag"] for call in client.calls] == ["3", "1", "1"]
    assert captured["source"] is prepare_baostock_snapshot.BAOSTOCK_DAILY_SOURCE
    assert captured["execution_rows"] == [
        {
            "stock_code": "300059.SZ",
            "date": "2024-01-02",
            "open": Decimal("10"),
            "high": Decimal("12"),
            "low": Decimal("9"),
            "close": Decimal("11"),
            "volume": 1000,
            "amount": Decimal("11000"),
            "turnover_rate_pct": Decimal("1.100000"),
            "turnover_rate_provider": "baostock_python_api",
            "turnover_rate_methodology": (
                "baostock.history_k_data_plus.turn.provider_reported_turnover_rate_pct.v1"
            ),
            "preclose": Decimal("10"),
            "tradestatus": "正常交易",
        },
        {
            "stock_code": "300059.SZ",
            "date": "2024-01-03",
            "open": Decimal("11"),
            "high": Decimal("12.5"),
            "low": Decimal("10.5"),
            "close": Decimal("12"),
            "volume": 1000,
            "amount": Decimal("11000"),
            "turnover_rate_pct": Decimal("1.100000"),
            "turnover_rate_provider": "baostock_python_api",
            "turnover_rate_methodology": (
                "baostock.history_k_data_plus.turn.provider_reported_turnover_rate_pct.v1"
            ),
            "preclose": Decimal("11"),
            "tradestatus": "正常交易",
        },
        {
            "stock_code": "300059.SZ",
            "date": "2024-01-04",
            "open": Decimal("12"),
            "high": Decimal("13"),
            "low": Decimal("11.5"),
            "close": Decimal("12.5"),
            "volume": 1000,
            "amount": Decimal("12500"),
            "turnover_rate_pct": Decimal("1.100000"),
            "turnover_rate_provider": "baostock_python_api",
            "turnover_rate_methodology": (
                "baostock.history_k_data_plus.turn.provider_reported_turnover_rate_pct.v1"
            ),
            "preclose": Decimal("12"),
            "tradestatus": "正常交易",
        },
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["snapshotId"] == f"technical:{'f' * 64}"
