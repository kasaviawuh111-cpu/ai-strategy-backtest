"""A-share market rules and deterministic matching models."""

from .calendar import NoNextSessionError, TradingCalendar
from .fees import (
    AshareExchange,
    FeeCalculator,
    FeePolicy,
    FeePolicyVersion,
    UnsupportedFeePolicyError,
)
from .matching import (
    CapacityMode,
    DailyBarMatchingModel,
    DailyBarMatchRequest,
    ExecutionTimeQuality,
    LimitHandling,
    MatchOutcome,
    MatchResult,
    PointInTimeVolume,
    VolumeSource,
    previous_session_volume_proxy,
)
from .rules import HistoricalAshareRuleBook, PriceLimitRuleInput

__all__ = [
    "AshareExchange",
    "CapacityMode",
    "DailyBarMatchRequest",
    "DailyBarMatchingModel",
    "ExecutionTimeQuality",
    "FeeCalculator",
    "FeePolicy",
    "FeePolicyVersion",
    "HistoricalAshareRuleBook",
    "LimitHandling",
    "MatchOutcome",
    "MatchResult",
    "NoNextSessionError",
    "PointInTimeVolume",
    "PriceLimitRuleInput",
    "TradingCalendar",
    "UnsupportedFeePolicyError",
    "VolumeSource",
    "previous_session_volume_proxy",
]
