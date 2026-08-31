"""Canonical, point-in-time market-data contracts."""

from .instruments import AshareInstrumentCodeError, normalize_a_share_instrument
from .models import (
    BarInterval,
    Board,
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    DataSnapshotRef,
    EventEnvelope,
    InstrumentSession,
    MarketEvent,
    MinuteBar,
    MinuteClose,
    PriceBasis,
    TimeQuality,
    TradingStatus,
    standard_buy_quantity_rule,
)

__all__ = [
    "AshareInstrumentCodeError",
    "BarInterval",
    "Board",
    "CorporateAction",
    "CorporateActionKind",
    "DailyBar",
    "DataSnapshotRef",
    "EventEnvelope",
    "InstrumentSession",
    "MarketEvent",
    "MinuteBar",
    "MinuteClose",
    "PriceBasis",
    "TimeQuality",
    "TradingStatus",
    "normalize_a_share_instrument",
    "standard_buy_quantity_rule",
]
