"""Explicitly labelled fallback session facts for the runnable research demo.

This adapter is intentionally small: v1 only publishes the product's first
instrument, 东方财富 (300059.SZ).  It derives daily bands with the versioned
fallback rule book because the bundled Parquet file has no ST, board or limit
columns.  Production deployments must replace it with an exchange/vendor
reference-data adapter rather than silently extending the code-prefix guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from ashare_lab.domain.execution import HistoricalAshareRuleBook, PriceLimitRuleInput
from ashare_lab.domain.market_data import (
    Board,
    DailyBar,
    InstrumentSession,
    TradingStatus,
)
from ashare_lab.domain.shared import DomainValidationError, InstrumentId


@dataclass(frozen=True, slots=True)
class ResearchInstrumentProfile:
    instrument_id: InstrumentId
    listing_date: date
    board: Board
    st_periods: tuple[tuple[date, date], ...] = ()

    def is_st_on(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.st_periods)


class ResearchFallbackSessionProvider:
    """Build reproducible, research-only sessions for explicitly profiled stocks."""

    version = HistoricalAshareRuleBook.version

    def __init__(
        self,
        profiles: Mapping[InstrumentId, ResearchInstrumentProfile] | None = None,
    ) -> None:
        default = ResearchInstrumentProfile(
            instrument_id=InstrumentId("300059.SZ"),
            listing_date=date(2010, 3, 19),
            board=Board.CHINEXT,
        )
        self._profiles = dict(profiles or {default.instrument_id: default})
        self._rules = HistoricalAshareRuleBook()

    def sessions_for(self, bars: Sequence[DailyBar]) -> Sequence[InstrumentSession]:
        if not bars:
            return ()
        instrument_id = bars[0].instrument_id
        if any(bar.instrument_id != instrument_id for bar in bars):
            raise DomainValidationError("session input bars must contain one instrument")
        profile = self._profiles.get(instrument_id)
        if profile is None:
            raise DomainValidationError(
                f"research session profile is not published for {instrument_id}"
            )

        sessions: list[InstrumentSession] = []
        previous_close = bars[0].open
        for index, bar in enumerate(bars):
            if bar.session_date < profile.listing_date:
                raise DomainValidationError("bar predates the instrument listing date")
            if index > 0:
                previous_close = bars[index - 1].close
            # The only published profile is many years past its first five
            # sessions.  Keeping the boundary explicit avoids pretending a
            # calendar-day count is a listing-session count.
            listing_session_number = (
                index + 1 if bars[0].session_date == profile.listing_date else 6
            )
            sessions.append(
                self._rules.build_session(
                    PriceLimitRuleInput(
                        instrument_id=instrument_id,
                        session_date=bar.session_date,
                        board=profile.board,
                        status=(
                            TradingStatus.SUSPENDED
                            if bar.volume.value == 0
                            else TradingStatus.TRADING
                        ),
                        previous_close=previous_close,
                        listing_date=profile.listing_date,
                        listing_session_number=listing_session_number,
                        is_st=profile.is_st_on(bar.session_date),
                    )
                )
            )
        return tuple(sessions)
