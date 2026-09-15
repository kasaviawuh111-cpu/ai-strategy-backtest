"""Local import watermark; individual-symbol completeness is checked separately."""
from datetime import date
from pathlib import Path
import re
import time
import json

import pyarrow.parquet as pq


class MinuteAvailability:
    def __init__(self, root: Path, *, require_index: bool = False):
        self.root = root
        self.require_index = require_index
        self._files = {}
        self._checked = float('-inf')
        self._latest = None

    def latest_date(self) -> date | None:
        if time.monotonic() - self._checked < 60:
            return self._latest
        index = self.root / 'minute-availability.json'
        if self.require_index or index.is_file():
            data = json.loads(index.read_text())
            if (data.get('schemaVersion') != 'ashare-minute-availability.v1'
                    or not isinstance(data.get('fileCount'), int) or data['fileCount'] < 1
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', data.get('dataVersion', ''))):
                raise ValueError('Invalid minute availability index')
            self._latest = date.fromisoformat(data['latestDate'])
            self._checked = time.monotonic()
            return self._latest
        dates = []
        for path in self.root.glob('*.parquet'):
            if not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)\.parquet', path.name):
                continue
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            cached = self._files.get(path.name)
            if cached is None or cached[0] != signature:
                metadata = pq.read_metadata(path)
                values = []
                for group in range(metadata.num_row_groups):
                    row = metadata.row_group(group)
                    for index in range(row.num_columns):
                        column = row.column(index)
                        if column.path_in_schema == 'trade_time' and column.statistics and column.statistics.has_min_max:
                            value = column.statistics.max
                            values.append(value.date())
                cached = (signature, max(values) if values else None)
                self._files[path.name] = cached
            if cached[1] is not None:
                dates.append(cached[1])
        self._latest = max(dates) if dates else None
        self._checked = time.monotonic()
        return self._latest
