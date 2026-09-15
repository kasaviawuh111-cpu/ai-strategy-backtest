"""Close-confirmed indicator intents for the shared minute account executor.

These are signals, not assumed fills. Position/cash sizing happens only at
the effective market opening, after corporate actions have updated the ledger.
"""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.application.scheduled_execution import ScheduledOrder
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import InstrumentId, require_aware


@dataclass(frozen=True)
class DailySignalIntent:
    intent_id: str
    instrument_id: InstrumentId
    confirmed_at: datetime
    session_date: date
    enter: bool = False
    exit: bool = False
    cash_fraction: Decimal = Decimal(1)
    available_at: datetime | None = None
    position_policy: str = "single_position_no_pyramiding"

    def __post_init__(self):
        if self.position_policy not in {
            "single_position_no_pyramiding", "accumulate_on_new_entry_signal",
        }:
            raise ValueError("unsupported daily signal position policy")
        require_aware(self.confirmed_at, "confirmed_at")
        local = self.confirmed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if (not self.intent_id or local.time() < time(15)
                or local.date() >= self.session_date):
            raise ValueError("daily signal must be confirmed after close before its target session")
        if type(self.enter) is not bool or type(self.exit) is not bool or not (self.enter or self.exit):
            raise ValueError("daily intent requires a boolean entry or exit signal")
        if not self.cash_fraction.is_finite() or not 0 < self.cash_fraction <= 1:
            raise ValueError("daily entry cash fraction must be in (0, 1]")
        require_aware(self.known_at, "available_at")
        opening = datetime.combine(self.session_date, time(9, 30), ZoneInfo("Asia/Shanghai"))
        if not self.confirmed_at <= self.known_at < opening:
            raise ValueError("daily signal evidence must be known before target opening")

    @property
    def known_at(self):
        return self.available_at or self.confirmed_at

    def validate_calendar(self, sessions: tuple[date, ...]):
        signal_day = self.confirmed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if any(a >= b for a, b in zip(sessions, sessions[1:])):
            raise ValueError("daily signal calendar must be strictly ordered")
        if signal_day not in sessions:
            raise ValueError("daily signal session missing from market calendar")
        index = sessions.index(signal_day)
        if index + 1 >= len(sessions) or sessions[index + 1] != self.session_date:
            raise ValueError("daily signal must activate at the next market session")

    def order_for(self, portfolio, instrument, *, minimum_shares=0):
        if type(minimum_shares) is not int or minimum_shares < 0:
            raise ValueError("minimum shares must be a nonnegative integer")
        shares = portfolio.position_quantity(instrument).value
        economic_shares = shares + portfolio.pending_share_delta(instrument)
        if economic_shares > 0:
            if self.exit and shares > minimum_shares:
                return ScheduledOrder(self.intent_id, self.session_date, self.known_at,
                                      side=OrderSide.SELL, quantity=shares - minimum_shares), "daily_exit_signal"
            if self.exit and minimum_shares and shares <= minimum_shares:
                return None, "minimum_inventory_reached"
            if self.exit or not self.enter:
                return None, "no_delivered_inventory"
            if self.position_policy == "single_position_no_pyramiding":
                return None, "position_already_open"
        if self.enter:
            budget = portfolio.cash.amount * self.cash_fraction
            if budget > 0:
                return ScheduledOrder(self.intent_id, self.session_date, self.known_at,
                                      budget=budget), "daily_entry_signal"
            return None, "insufficient_cash_including_fees"
        return None, "no_position"


class DailyPositionRiskObserver:
    """Stateful close observer; fills and cash remain owned by the replay ledger."""

    def __init__(self, *, rules, bars, market_sessions):
        self.rules = tuple(rules)
        self.bars = {bar.session_date: bar for bar in bars}
        if len(self.bars) != len(bars):
            raise ValueError("position risk daily bars must have unique dates")
        self.sessions = tuple(market_sessions)
        if any(a >= b for a, b in zip(self.sessions, self.sessions[1:])):
            raise ValueError("position risk calendar must be strictly ordered")
        self.anchor = None
        self.anchor_price = self.peak = None
        self.pending = None

    def after_fill(self, fill, *, before, after):
        if after == 0:
            self.anchor = self.anchor_price = self.peak = self.pending = None
        elif before == 0:
            if fill.side is not OrderSide.BUY:
                raise ValueError("position risk cycle requires an actual buy fill")
            self.anchor = fill
            self.anchor_price = self.peak = fill.price.amount

    def rebase(self, factor):
        if self.anchor is not None:
            self.anchor_price *= factor
            self.peak *= factor

    def observe_close(self, day):
        if self.anchor is None or self.pending is not None:
            return
        from ashare_lab.application.daily_backtest import _position_risk_signal
        bar = self.bars.get(day)
        if bar is None or bar.instrument_id != self.anchor.instrument_id:
            raise ValueError("position risk requires a sourced daily bar for this security")
        self.pending, self.peak = _position_risk_signal(
            rules=self.rules, anchor_fill=self.anchor,
            anchor_adjusted_price=self.anchor_price,
            previous_peak_adjusted_close=self.peak,
            signal_bar=bar, raw_rebased=True)

    def intent_for(self, day):
        fact = self.pending
        if fact is None or day <= fact.session_date:
            return None
        if day not in self.sessions or fact.session_date not in self.sessions:
            raise ValueError("position risk signal requires sourced market sessions")
        opening = datetime.combine(day, time(9, 30), ZoneInfo("Asia/Shanghai"))
        if fact.available_at >= opening:
            return None
        # A triggered liquidation persists across suspension, T+1 and capacity
        # failures until the shared ledger actually becomes flat.
        return DailySignalIntent(f"risk:{self.anchor.fill_id.value}:{day}",
            fact.instrument_id, fact.observed_at, day, exit=True,
            available_at=fact.available_at)


def prepare_daily_signal_intents(*, strategy, signal_input, market_sessions, cash_fraction):
    """Validate daily facts independently of the entry/exit execution adapters.

    A missing leg is represented by an aligned timeline of None, not by
    manufacturing a signal. Both legacy hybrid and composed plans must obey
    the same identity, evidence-availability and next-session checks.
    """
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError
    history = signal_input.history
    instrument = InstrumentId(strategy.instrument.symbol)
    if history.instrument_id != strategy.instrument.symbol:
        raise MinuteReplayDataError("market_data_instrument_mismatch")
    if len(signal_input.entry_timeline) != len(history.rows) or len(signal_input.exit_timeline) != len(history.rows):
        raise MinuteReplayDataError("daily_signal_timeline_misaligned")
    dates = tuple(market_sessions)
    if any(a >= b for a, b in zip(dates, dates[1:])):
        raise MinuteReplayDataError("market_calendar_not_strictly_ordered")
    next_day = dict(zip(dates, dates[1:]))
    intents = []
    from ashare_lab.application.entry_occurrences import ACCUMULATE_ON_NEW_ENTRY, entry_occurrences
    accumulate = strategy.execution.position_policy == ACCUMULATE_ON_NEW_ENTRY
    if accumulate:
        # Validate the raw state observations, including warm-up and suppressed
        # repeats. A malformed or late observation cannot silently rearm (or
        # suppress) an entry just because it produces no order itself.
        for row, entry, exit in zip(
            history.rows, signal_input.entry_timeline, signal_input.exit_timeline, strict=True,
        ):
            facts = [fact for fact in (entry, exit) if fact is not None]
            closing = datetime.combine(row.session_date, time(15), ZoneInfo("Asia/Shanghai"))
            if any(fact.instrument_id != instrument or fact.session_date != row.session_date
                   or fact.observed_at != closing for fact in facts):
                raise MinuteReplayDataError("daily_signal_identity_or_clock_mismatch")
            target = next_day.get(row.session_date)
            if target is not None and target <= strategy.backtest.end:
                opening = datetime.combine(target, time(9, 30), ZoneInfo("Asia/Shanghai"))
                if any(fact.available_at >= opening for fact in facts):
                    raise MinuteReplayDataError("daily_signal_evidence_not_available_before_open")
    entries = (entry_occurrences(signal_input.entry_timeline, condition=strategy.entry)
               if accumulate else signal_input.entry_timeline)
    for row, entry, exit in zip(history.rows, entries, signal_input.exit_timeline, strict=True):
        if not strategy.backtest.start <= row.session_date <= strategy.backtest.end:
            continue
        facts = [fact for fact in (entry, exit) if fact is not None]
        closing = datetime.combine(row.session_date, time(15), ZoneInfo("Asia/Shanghai"))
        if any(fact.instrument_id != instrument or fact.session_date != row.session_date
               or fact.observed_at != closing for fact in facts):
            raise MinuteReplayDataError("daily_signal_identity_or_clock_mismatch")
        enter, leave = bool(entry and entry.triggered), bool(exit and exit.triggered)
        if not (enter or leave):
            continue
        target = next_day.get(row.session_date)
        if target is None:
            raise MinuteReplayDataError("market_calendar_missing_next_settlement_session")
        if target > strategy.backtest.end:
            continue  # No order beyond the requested backtest interval.
        known_at = max(fact.available_at for fact in facts)
        if known_at >= datetime.combine(target, time(9, 30), ZoneInfo("Asia/Shanghai")):
            raise MinuteReplayDataError("daily_signal_evidence_not_available_before_open")
        intents.append(DailySignalIntent(f"daily:{row.session_date}", instrument, closing, target,
                                        enter, leave, cash_fraction, known_at,
                                        ACCUMULATE_ON_NEW_ENTRY if accumulate
                                        else "single_position_no_pyramiding"))
    return tuple(intents)


def execute_hybrid_signals(*, strategy, signal_input, prepared, config, exchange,
                           corporate_actions=None, price_rebases=()):
    """Bridge already evaluated daily facts; never evaluate indicators on minute bars."""
    from ashare_lab.application.fixed_grid_orders import FixedGridOrders
    from ashare_lab.application.minute_grid_replay import Protection, replay_grid
    from ashare_lab.domain.execution.fees import FeeCalculator, FeePolicy
    from ashare_lab.domain.portfolio import PortfolioState
    from ashare_lab.domain.shared import Money
    from ashare_lab.domain.strategy import HoldingPeriodExit, HybridExecutionPolicy, MinuteProtectionExit

    if not isinstance(strategy.execution, HybridExecutionPolicy):
        raise ValueError("hybrid executor requires an explicit hybrid strategy")
    rules = [rule for rule in strategy.exit.children if isinstance(rule, MinuteProtectionExit)]
    if len(rules) != 1:
        raise ValueError("hybrid executor requires exactly one minute protection")
    protection = rules[0]
    dates = tuple(prepared.market_sessions)
    intents = prepare_daily_signal_intents(
        strategy=strategy, signal_input=signal_input, market_sessions=dates,
        cash_fraction=config.allocation_ratio,
    )
    holding = next((rule.sessions for rule in strategy.exit.children if isinstance(rule, HoldingPeriodExit)), None)
    return replay_grid(FixedGridOrders([]), list(prepared.bars),
        PortfolioState(Money(Decimal(strategy.backtest.initial_cash_cny))),
        FeeCalculator(FeePolicy(exchange, config.commission_rate, Money(config.minimum_commission_cny))),
        slippage_bps=config.slippage_bps, slippage_cny=config.slippage_cny,
        daily_signals=tuple(intents), market_sessions=dates, holding_sessions=holding,
        protection=Protection(
            take_profit=protection.take_profit_pct / 100 if protection.take_profit_pct is not None else None,
            stop_loss=protection.stop_loss_pct / 100 if protection.stop_loss_pct is not None else None,
            trailing_drawdown=(protection.trailing_drawdown_pct / 100
                               if protection.trailing_drawdown_pct is not None else None),
            limit_price=protection.limit_price_cny),
        corporate_actions=corporate_actions, price_rebases=price_rebases,
        nontrading_closes=prepared.nontrading_closes,
        capacity_mode=config.capacity_mode, participation_rate=config.participation_rate,
        limit_handling=config.limit_handling)
