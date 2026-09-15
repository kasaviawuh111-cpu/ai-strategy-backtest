from datetime import date

import pytest

from scripts.prepare_market_calendar import calendar_payload


def test_export_preserves_provider_closed_weekday_and_open_day():
    payload = calendar_payload([["2025-01-01", "0"], ["2025-01-02", "1"]],
                               date(2025, 1, 1), date(2025, 1, 2))
    assert payload["sessions"] == ["2025-01-02"]
    assert payload["source"]["rows"][0] == ["2025-01-01", "0"]
    assert len(payload["sourceSha256"]) == 64


@pytest.mark.parametrize("rows", [[], [["2025-01-02", "1"]],
    [["2025-01-01", "unknown"]], [["2025-01-01", "0"], ["2025-01-01", "1"]]])
def test_export_rejects_gaps_duplicates_and_unknown_flags(rows):
    with pytest.raises(ValueError):
        calendar_payload(rows, date(2025, 1, 1), date(2025, 1, 2))
