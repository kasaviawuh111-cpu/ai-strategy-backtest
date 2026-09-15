from datetime import date
from unittest.mock import MagicMock

import pytest

from ashare_lab.adapters.market_data.eastmoney_minute import EastmoneyMinuteSourceError
from ashare_lab.adapters.market_data.eastmoney_minute_acquirer import EastmoneyMinuteAcquirer
from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import load_eastmoney_minute_snapshot
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from tests.unit.adapters.test_eastmoney_minute_snapshot import _make_collection, _make_rows


def test_acquirer_publishes_only_returned_complete_days_with_one_fetch(tmp_path):
    source = MagicMock()
    source.__enter__.return_value = source
    source.fetch.return_value = _make_collection(_make_rows())
    paths = EastmoneyMinuteAcquirer(tmp_path, lambda: source)("300059.SZ", (date(2025, 1, 2), date(2025, 1, 3)))
    assert len(paths) == 1
    assert len(load_eastmoney_minute_snapshot(paths[0])) == 240
    assert source.fetch.call_count == 1
    source.fetch.return_value = _make_collection(_make_rows()[:-1])
    assert EastmoneyMinuteAcquirer(tmp_path, lambda: source)("300059.SZ", (date(2025, 1, 2),)) == ()


@pytest.mark.parametrize("code", ["network_error", "range_unavailable"])
def test_acquirer_distinguishes_transport_and_absent_history(tmp_path, code):
    source = MagicMock()
    source.__enter__.return_value = source
    source.fetch.side_effect = EastmoneyMinuteSourceError(code, "fixture")
    acquire = EastmoneyMinuteAcquirer(tmp_path, lambda: source)
    if code == "network_error":
        with pytest.raises(MinuteReplayDataError, match="minute_source_network_error"):
            acquire("300059.SZ", (date(2025, 1, 2),))
    else:
        assert acquire("300059.SZ", (date(2025, 1, 2),)) == ()
