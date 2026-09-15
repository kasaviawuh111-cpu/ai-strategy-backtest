from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import json
import pytest
from pathlib import Path

from ashare_lab.adapters.market_data.minute_availability import MinuteAvailability


def test_import_watermark_follows_files_not_wall_clock(tmp_path):
    path = tmp_path / '000001.SZ.parquet'
    pq.write_table(pa.table({'trade_time': [datetime(2026, 9, 11, 15)]}), path)
    source = MinuteAvailability(tmp_path)
    assert source.latest_date() == date(2026, 9, 11)
    pq.write_table(pa.table({'trade_time': [datetime(2026, 9, 18, 15)]}), path)
    source._checked = float('-inf')
    assert source.latest_date() == date(2026, 9, 18)
    (tmp_path / '._000001.SZ.parquet').write_bytes(b'not parquet')
    source._checked = float('-inf')
    assert source.latest_date() == date(2026, 9, 18)


def test_no_import_does_not_invent_a_date(tmp_path):
    assert MinuteAvailability(tmp_path).latest_date() is None


def test_remote_import_index_never_enumerates_mount(tmp_path, monkeypatch):
    (tmp_path / 'minute-availability.json').write_text(json.dumps({
        'schemaVersion': 'ashare-minute-availability.v1', 'fileCount': 5839,
        'dataVersion': 'sha256:' + 'a' * 64, 'latestDate': '2026-09-11'}))
    def forbidden(*args, **kwargs):
        raise AssertionError('must not enumerate remote files')
    monkeypatch.setattr(Path, 'glob', forbidden)
    monkeypatch.setattr(Path, 'iterdir', forbidden)
    assert MinuteAvailability(tmp_path, require_index=True).latest_date() == date(2026, 9, 11)


def test_missing_remote_index_fails_without_scan(tmp_path):
    with pytest.raises(FileNotFoundError):
        MinuteAvailability(tmp_path, require_index=True).latest_date()
