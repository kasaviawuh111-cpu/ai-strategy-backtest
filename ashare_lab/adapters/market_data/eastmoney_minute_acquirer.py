"""Bounded on-demand acquisition using the existing public minute channel."""
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from ashare_lab.adapters.market_data.eastmoney_minute import EastmoneyMinuteResearchSource, EastmoneyMinuteSourceError
from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import (
    EastmoneyMinuteSnapshotSpec, EastmoneyMinuteSnapshotError, build_eastmoney_minute_snapshot,
)
from ashare_lab.application.minute_replay_input import MinuteReplayDataError


@dataclass(frozen=True)
class EastmoneyMinuteAcquirer:
    output_root: Path
    source_factory: object = EastmoneyMinuteResearchSource

    def __call__(self, symbol: str, missing: tuple[date, ...]) -> tuple[Path, ...]:
        if not missing:
            return ()
        # trends2 returns a retained window, not a pageable historical range.
        # Fetch once per run; repeating once per missing day cannot extend it.
        try:
            with self.source_factory() as source:
                collection = source.fetch(instrument_id=symbol, start=min(missing), end=max(missing), period=1)
        except EastmoneyMinuteSourceError as exc:
            if exc.code in {"range_unavailable", "no_data"}:
                return ()
            raise MinuteReplayDataError(f"minute_source_{exc.code}") from exc
        paths = []
        for day in sorted(set(missing)):
            rows = tuple(row for row in collection.rows if row.timestamp.date() == day)
            if not rows:
                continue
            daily = replace(collection, requested_start=day, requested_end=day, rows=rows)
            try:
                built = build_eastmoney_minute_snapshot(collection=daily,
                    spec=EastmoneyMinuteSnapshotSpec(symbol=symbol, start=day, end=day),
                    output_root=self.output_root, captured_at=collection.retrieved_at)
            except EastmoneyMinuteSnapshotError as exc:
                # A forming session must not discard other complete days.
                if "is incomplete:" in str(exc):
                    continue
                raise MinuteReplayDataError("minute_source_invalid_market_data") from exc
            paths.append(built.path)
        return tuple(paths)
