"""Configurable daily A-share grid using vn.py's target-position algorithm."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal, Protocol

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistory
from ashare_lab.adapters.strategies.vnpy_grid import target_grid_change
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.opening_portfolio import initialize_opening_portfolio
from ashare_lab.application.skill_backtest import (
    _affordable_notional,
    _execution_capacity,
)
from ashare_lab.domain.execution import LimitHandling
from ashare_lab.domain.execution.bar_prices import BarPrices, match_bar_price
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.execution.fees import FeeCalculator, FeePolicy, AshareExchange
from ashare_lab.domain.portfolio import FeeBreakdown, FillRecord, PortfolioState, apply_buy, apply_sell
from ashare_lab.domain.shared import FillId, InstrumentId, Money, OrderId, Price, Quantity
from ashare_lab.domain.market_data import Board, InstrumentSession, TradingStatus, standard_buy_quantity_rule
from ashare_lab.domain.strategy.price_plans import GridParameters, GridSpecificationError

D = Decimal
ZERO = D(0)


@dataclass(frozen=True)
class _Lot:
    """Read-only projection for the existing signal policy, not a ledger."""
    shares: int
    bought: date
    price: Decimal
    sellable_on: date


def _policy_lots(portfolio: PortfolioState) -> list[_Lot]:
    return [_Lot(lot.remaining_quantity.value, lot.acquired_on,
                 (lot.acquisition_principal or lot.cost_basis).amount / lot.remaining_quantity.value,
                 lot.sellable_on) for lot in portfolio.lots]


@dataclass(frozen=True)
class GridOrder:
    side: Literal["buy", "sell"]
    quantity: int
    grid_units: Decimal
    signal_date: date
    limit_price: Decimal | None
    initial: bool = False
    baseline_units: Decimal = ZERO
    # Stable signal identity and evidence survive partial fills. They are not
    # permission to change inventory; only the execution ledger can do that.
    rule_id: str | None = None
    trigger_price: Decimal | None = None
    reference_price: Decimal | None = None
    amount_budget_cny: Decimal | None = None
    signal_at: datetime | None = None


@dataclass(frozen=True)
class PricePolicyContext:
    shares: int
    settled_shares: int
    average_entry_price: Decimal | None
    holding_sessions: int | None
    holding_batches: tuple[tuple[int, int], ...] = ()


class PriceOrderPolicy(Protocol):
    """A signal policy reuses the ledger; it never modifies cash or inventory."""

    def on_close(self, *, price: Decimal, session: date, board: Board,
                 context: PricePolicyContext) -> GridOrder | None: ...

    def on_open(self, *, session: date, board: Board,
                context: PricePolicyContext) -> GridOrder | None: ...

    def on_fill(self, *, order: GridOrder, quantity: int, price: Decimal) -> None: ...

    def on_session_end(self, *, order: GridOrder, session: date, reason: str) -> str | None: ...

    def rebase_prices(self, rebase) -> None: ...


def rebase_price_order(order: GridOrder | None, rebase) -> GridOrder | None:
    if order is None:
        return None
    return replace(order, limit_price=rebase.price(order.limit_price, order.side),
                   trigger_price=rebase.price(order.trigger_price, order.side),
                   reference_price=(order.reference_price * rebase.factor
                                    if order.reference_price is not None else None))


def _rebase_grid_parameters(params, rebase):
    updates = {key: getattr(params, key) * rebase.factor
               for key in ("anchor_price", "lower_price", "upper_price", "limit_offset_cny")}
    updates.update(buy_limit=rebase.price(params.buy_limit, "buy"),
                   sell_limit=rebase.price(params.sell_limit, "sell"))
    if params.spacing_mode == "cny":
        updates["spacing"] = params.spacing * rebase.factor
    for side in ("buy", "sell"):
        value = getattr(params, side + "_spacing")
        if value is not None and params.spacing_for(side)[0] == "cny":
            updates[side + "_spacing"] = value * rebase.factor
    return params.model_copy(update=updates)


def _buy_quantity(desired: int, board: Board) -> int:
    minimum, increment = standard_buy_quantity_rule(board)
    if desired < minimum:
        return 0
    return minimum + (desired - minimum) // increment * increment


def _sell_quantity(desired: int, total: int, board: Board) -> int:
    return total if desired >= total else _buy_quantity(desired, board)


def _limit(params: GridParameters, side: str, change: Decimal) -> Decimal | None:
    if params.price_mode == "next_open":
        return None
    if params.price_mode == "fixed_limit":
        return params.buy_limit if side == "buy" else params.sell_limit
    price = params.line(change)
    price += params.limit_offset_cny * (1 if side == "buy" else -1)
    return max(D("0.01"), price.quantize(
        D("0.01"), rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING,
    ))


def grid_order(
    params: GridParameters, *, price: Decimal, session: date,
    filled_units: Decimal, board: Board,
    baseline_units: Decimal = ZERO, startup_distance: Decimal | None = None,
    reference_price: Decimal | None = None,
) -> GridOrder | None:
    """Create an intent; cash, inventory and anchor change only on actual fills."""
    if price < params.lower_price or price > params.upper_price:
        return None
    if params.asymmetric:
        return _asymmetric_grid_order(params, price=price, session=session, board=board,
                                      reference=reference_price or params.resolved_anchor,
                                      startup=startup_distance is not None)
    distance = params.distance(price)
    if startup_distance is not None:
        # Before the first fill, wait for a new whole grid-line crossing.
        # A start between two lines must not create fractional grid orders.
        direction = target_grid_change(distance, startup_distance)
        if not direction:
            return None
        baseline_units = startup_distance.to_integral_value(
            rounding=ROUND_FLOOR if direction > 0 else ROUND_CEILING,
        )
    change = target_grid_change(distance, filled_units + baseline_units)
    if not change:
        return None
    side: Literal["buy", "sell"] = "buy" if change > 0 else "sell"
    quantity = (
        int(abs(change) * params.order_shares) if params.sizing_mode == "shares"
        else int(abs(change) * params.order_amount_cny / price)
    )
    quantity = _buy_quantity(quantity, board)
    # Keep a triggered but unaffordable/minimum-size intent for execution audit.
    # Zero quantity is rejected downstream, never turned into a fill.
    return GridOrder(side, quantity, change, session, _limit(
        params, side, baseline_units + filled_units + change,
    ), baseline_units=baseline_units,
        rule_id=f"grid:{session.isoformat()}:{side}",
        trigger_price=params.line(baseline_units + filled_units + change),
        reference_price=params.resolved_anchor,
        amount_budget_cny=(abs(change) * params.order_amount_cny
                           if params.sizing_mode == "amount" else None))


def _asymmetric_grid_order(params: GridParameters, *, price: Decimal, session: date,
                           board: Board, reference: Decimal, startup: bool) -> GridOrder | None:
    """Directional spacing; reuse vn.py whole-grid rounding and the same ledger.

    Fixed mode advances the reference along executed theoretical grid levels,
    not the next-open fill price. Percent-of-anchor gaps keep the original scale.
    """
    if price == reference:
        return None
    side: Literal["buy", "sell"] = "buy" if price < reference else "sell"
    directional = params.for_side(side)
    reference_distance = directional.distance(reference)
    if startup:
        reference_distance = reference_distance.to_integral_value(
            rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING,
        )
        reference = directional.line(reference_distance)
    delta = directional.distance(price) - reference_distance
    nearest = delta.to_integral_value()
    if abs(delta - nearest) < D("1e-20"):
        delta = nearest
    change = target_grid_change(delta, ZERO)
    if not change:
        return None
    quantity = _buy_quantity(int(abs(change) * (
        D(params.order_shares) if params.sizing_mode == "shares"
        else params.order_amount_cny / price
    )), board)
    target_distance = reference_distance + change
    return GridOrder(
        side, quantity, change, session, _limit(directional, side, target_distance),
        rule_id=f"grid:{session.isoformat()}:{side}",
        trigger_price=directional.line(target_distance), reference_price=reference,
        amount_budget_cny=(abs(change) * params.order_amount_cny
                           if params.sizing_mode == "amount" else None),
    )


def advance_grid_fill(params: GridParameters, order: GridOrder, *, quantity: int,
                      price: Decimal, filled_units: Decimal,
                      reference_price: Decimal) -> tuple[GridParameters, Decimal, Decimal, Decimal]:
    """Advance daily-grid geometry from an actual fill, never a signal.

    Shared by daily replay and independent-leg adapters; account mutations stay
    in the ledger. Partial fills advance only their executed grid-unit fraction.
    """
    if order.initial or not 0 < quantity <= order.quantity:
        raise ValueError("daily grid advancement requires an actual grid fill")
    baseline = order.baseline_units
    units = filled_units + order.grid_units * D(quantity) / order.quantity
    if params.asymmetric:
        directional = params.for_side(order.side)
        reference_price = directional.line(
            # A ticket can fill over multiple bars in a shared-ledger replay.
            # Continue from the last executed reference, not its signal origin.
            directional.distance(reference_price)
            + order.grid_units * D(quantity) / order.quantity,
        )
    if params.anchor_update == "last_fill":
        params = params.model_copy(update={"anchor_price": price})
        baseline, units, reference_price = ZERO, ZERO, price
    return params, baseline, units, reference_price


def run_grid_backtest(
    *, params: GridParameters, history: MxDailyHistory, start: date, end: date,
    order_policy: PriceOrderPolicy | None = None,
    execution_config: BacktestRunConfig | None = None,
    market_sessions: tuple[date, ...] | None = None,
    corporate_actions=None, price_rebases=(),
) -> dict[str, object]:
    """Daily close / next-open research sharing the application's market gate."""
    if params.anchor_update == "last_trigger":
        raise GridSpecificationError("触发后更新基准的网格需要分钟执行，不能改用日线旧算法")
    rows = history.rows
    indices = [i for i, row in enumerate(rows) if start <= row.session_date <= end]
    usable = [rows[i] for i in indices if (
        rows[i].trading_status is TradingStatus.TRADING and rows[i].raw_open > 0
    )]
    if not usable:
        raise GridSpecificationError("指定区间内没有可用交易日")
    first_row = usable[0]
    if params.anchor_mode == "latest_price" and params.anchor_price is None:
        raise GridSpecificationError("行情最新价尚未取得，请先加载最新行情基准价")
    if params.anchor_mode == "first_open":
        anchor = first_row.raw_open
        if not params.lower_price <= anchor <= params.upper_price:
            raise GridSpecificationError(
                f"起始交易日{first_row.session_date}开盘价{anchor}元不在网格范围"
                f"{params.lower_price}–{params.upper_price}元内，请调整上下界或改用手动基准价",
            )
        # Only the historical first-open mode derives its anchor from replay bars.
        params = params.model_copy(update={"anchor_price": anchor})
    params = params.resolve_geometry()
    startup_distance = params.distance(first_row.raw_open)
    initial_anchor = params.resolved_anchor
    directional_reference = (first_row.raw_open if params.startup_mode == "wait_for_crossing"
                             else initial_anchor)
    effective_parameters = params.model_dump(mode="json")
    baseline_units: Decimal | None = (
        None if params.startup_mode == "wait_for_crossing" else D(0)
    )
    for quantity, label in ((params.initial_shares, "初始建仓"), (
        params.order_shares if params.sizing_mode == "shares" else 0, "每格数量",
    )):
        if quantity and _buy_quantity(quantity, history.board) != quantity:
            minimum, increment = standard_buy_quantity_rule(history.board)
            raise GridSpecificationError(f"{label}须不少于{minimum}股，并按{increment}股递增")
    config = replace(
        execution_config or BacktestRunConfig(),
        commission_rate=params.commission_rate,
        minimum_commission_cny=params.minimum_commission_cny,
        slippage_bps=params.slippage_bps,
        slippage_cny=params.slippage_cny,
        limit_handling=LimitHandling.STRICT_NO_FILL_AT_LIMIT,
    )
    fee_calculator = FeeCalculator(FeePolicy(AshareExchange(history.instrument_id.rsplit(".", 1)[1]),
                                             params.commission_rate, Money(params.minimum_commission_cny)))
    filled_units = D(0)
    orders: list[dict[str, object]] = []
    curve: list[dict[str, object]] = []
    initial_remaining = params.initial_shares
    pending: GridOrder | None = None
    max_drawdown = D(0)
    total_fees = D(0)
    adjustment_changed = False
    session_indices = {day: i for i, day in enumerate(market_sessions)} if market_sessions is not None else {row.session_date: i for i, row in enumerate(rows)}
    if market_sessions is not None:
        from ashare_lab.application.minute_replay_input import MinuteReplayDataError
        if not market_sessions or any(a >= b for a, b in zip(market_sessions, market_sessions[1:])):
            raise MinuteReplayDataError("invalid_market_calendar")
        if any(rows[i].session_date not in session_indices for i in indices):
            raise MinuteReplayDataError("market_calendar_session_missing")
        available_days = {rows[i].session_date for i in indices}
        if any(start <= day <= end and day not in available_days for day in market_sessions):
            raise MinuteReplayDataError("security_session_missing")
    opening_row = rows[indices[0]]
    instrument = InstrumentId(history.instrument_id)
    rebases = {item.ex_date: item for item in price_rebases}
    if price_rebases and corporate_actions is None:
        raise GridSpecificationError("corporate_action_source_missing")
    if corporate_actions is not None:
        if market_sessions is None or market_sessions[-1] <= end:
            raise GridSpecificationError("corporate_action_market_calendar_required")
        if len(rebases) != len(price_rebases) or any(item.instrument_id != instrument for item in price_rebases):
            raise GridSpecificationError("invalid_daily_price_rebases")
        if any(action.instrument_id != instrument for action in corporate_actions.actions):
            raise GridSpecificationError("corporate_action_instrument_mismatch")
        action_days = {action.ex_date for action in corporate_actions.actions}
        if any(day not in action_days for day in rebases):
            raise GridSpecificationError("corporate_action_rebase_event_missing")
        if any(start <= day <= end and day not in rebases for day in action_days):
            raise GridSpecificationError("daily_corporate_action_price_rebase_missing")
    portfolio, opening_equity = initialize_opening_portfolio(
        params, first_price=opening_row.raw_open, first_day=opening_row.session_date,
        instrument_id=instrument, market_sessions=tuple(session_indices),
    )
    # Declared opening stock is valued at this known opening observation,
    # without inventing a historical acquisition price or a purchase fee.
    portfolio = replace(portfolio, lots=tuple(replace(lot,
        acquisition_principal=lot.cost_basis) for lot in portfolio.lots))
    benchmark = portfolio
    benchmark_entered = bool(params.opening_shares)
    corporate_trace = []

    def apply_boundary(state, session, clock, *, before, account):
        if corporate_actions is None:
            return state
        rebase = rebases.get(session.session_date) if before else None
        if rebase is not None:
            multiplier = D(1)
            for action in corporate_actions.actions:
                if action.ex_date == session.session_date:
                    if action.available_at is None or action.available_at > clock:
                        raise GridSpecificationError("corporate_action_not_known_before_open")
                    if action.share_multiplier is not None:
                        multiplier *= action.share_multiplier
            state = replace(state, lots=tuple(replace(lot, acquisition_principal=Money(
                lot.acquisition_principal.amount * rebase.factor * multiplier)) for lot in state.lots))
        applied = (corporate_actions.apply_before_session if before else corporate_actions.apply_after_session)(
            portfolio=state, instrument_id=instrument, session=session, as_of=clock)
        for item in applied.applied_actions:
            corporate_trace.append(dict(account=account, at=clock.isoformat(), action_id=item.action_id,
                                         phase=item.phase, reason=item.reason_code))
        return applied.portfolio
    cash = initial_cash = portfolio.cash.amount
    initial_equity = opening_equity if opening_equity is not None else initial_cash
    lots = _policy_lots(portfolio)
    average_entry_price = opening_row.raw_open if lots else None
    entry_index = session_indices[lots[0].bought] if lots else None
    peak = initial_equity
    for index in indices:
        row = rows[index]
        market_index = session_indices[row.session_date]
        previous = rows[index - 1] if index else None
        minimum, increment = standard_buy_quantity_rule(history.board)
        session = InstrumentSession(instrument, row.session_date, history.board, row.trading_status,
            Price(row.raw_preclose), Price(row.upper_limit) if row.upper_limit is not None else None,
            Price(row.lower_limit) if row.lower_limit is not None else None, minimum, increment, is_st=row.is_st)
        opening_clock = datetime.combine(row.session_date, time(9, 30), ZoneInfo("Asia/Shanghai"))
        closing_clock = datetime.combine(row.session_date, time(15), ZoneInfo("Asia/Shanghai"))
        portfolio = apply_boundary(portfolio, session, opening_clock, before=True, account="strategy")
        benchmark = apply_boundary(benchmark, session, opening_clock, before=True, account="benchmark")
        rebase = rebases.get(row.session_date)
        if rebase is not None:
            params = _rebase_grid_parameters(params, rebase)
            directional_reference *= rebase.factor
            pending = rebase_price_order(pending, rebase)
            if order_policy is not None:
                order_policy.rebase_prices(rebase)
        cash = portfolio.cash.amount
        lots = _policy_lots(portfolio)
        shares = sum(lot.shares for lot in lots)
        pending_shares = portfolio.pending_share_delta(instrument)
        average_entry_price = ((sum(lot.price * lot.shares for lot in lots) + portfolio.pending_share_principal(instrument))
                               / (shares + pending_shares)) if shares + pending_shares else None
        entry_index = session_indices[lots[0].bought] if lots else None
        context = PricePolicyContext(
            shares, portfolio.sellable_quantity(instrument, row.session_date).value,
            average_entry_price, None if entry_index is None else market_index - entry_index,
            tuple((market_index - session_indices[lot.bought], lot.shares) for lot in lots),
        )
        if order_policy is not None and not initial_remaining:
            due = order_policy.on_open(session=row.session_date, board=history.board,
                                       context=context)
            pending = due or pending
        if previous and abs(
            row.adjusted_open / row.raw_open / (previous.adjusted_close / previous.raw_close) - 1
        ) > D("0.0001"):
            adjustment_changed = True
        order = (
            GridOrder("buy", initial_remaining, D(0), row.session_date, None, True)
            if initial_remaining else pending
        )
        if order is not None:
            cash_before = cash
            side = order.side
            reason, capacity = _execution_capacity(
                row=row, previous_row=previous, side=side,
                config=replace(config, limit_handling=LimitHandling.ALLOW_LIMIT_VOLUME),
            )
            matched = match_bar_price(BarPrices(row.raw_open, row.raw_high, row.raw_low, row.raw_close),
                                      side=OrderSide(side), limit=order.limit_price,
                                      slippage_bps=params.slippage_bps, slippage_cny=params.slippage_cny)
            price = matched if matched is not None else row.raw_open
            if matched is None:
                reason = reason or "limit_not_reached"
            if row.volume <= 0:
                reason = reason or "no_market_trades"
            # Judge the matched price, not whether the opening happened to
            # touch a limit earlier in this session.
            if ((side == "buy" and row.upper_limit is not None and price >= row.upper_limit)
                    or (side == "sell" and row.lower_limit is not None and price <= row.lower_limit)):
                reason = reason or "adverse_price_limit"
            # Opening-price proxy is not an exchange market order. Use the
            # stricter STAR market-order ceiling even for this proxy (50k);
            # explicitly limited orders may use the 100k limit-order ceiling.
            maximum_order = (50_000 if order.limit_price is None else 100_000) if (
                history.board is Board.STAR
            ) else 1_000_000
            quantity = min(order.quantity, maximum_order)
            if order.amount_budget_cny is not None and price > 0:
                quantity = min(quantity, int(order.amount_budget_cny / price))
            if params.sizing_mode == "amount" and not order.initial and price > 0:
                quantity = min(
                    quantity, int(abs(order.grid_units) * params.order_amount_cny / price),
                )
            if side == "buy" and price > 0:
                affordable = _affordable_notional(
                    cash, rate=params.commission_rate,
                    minimum=params.minimum_commission_cny,
                )
                quantity = min(quantity, int(affordable / price), params.max_shares - shares - pending_shares)
                if params.max_position_cny is not None:
                    quantity = min(quantity, max(0, int(params.max_position_cny / price) - shares - pending_shares))
                quantity = _buy_quantity(quantity, history.board)
            elif side == "sell":
                settled = portfolio.sellable_quantity(instrument, row.session_date).value
                quantity = min(quantity, settled, max(0, shares - params.min_shares))
                quantity = _sell_quantity(quantity, shares, history.board)
                if quantity == 0:
                    reason = reason or (
                        "no_position_to_sell" if shares == 0 else
                        "minimum_inventory_reached" if shares <= params.min_shares else
                        "t_plus_one_no_sellable_shares" if settled == 0 else
                        "sell_quantity_below_lot"
                    )
            if capacity is not None:
                # Capacity is an execution fragment, not a fresh declaration.
                # The shared legacy helper expresses previous-session shares
                # as open-price notional; recover shares using that same open,
                # not the eventual limit/slippage price.
                available_shares = int(capacity / row.raw_open) if row.raw_open > 0 else 0
                quantity = min(quantity, available_shares)
            if quantity <= 0:
                if side == "buy" and price > 0 and not reason:
                    minimum = standard_buy_quantity_rule(history.board)[0]
                    minimum_fees = fee_calculator.calculate(side=OrderSide.BUY, price=Price(price),
                        quantity=Quantity(minimum), trade_date=row.session_date).total.amount
                    budget = order.amount_budget_cny
                    if budget is None and params.sizing_mode == "amount" and not order.initial:
                        budget = abs(order.grid_units) * params.order_amount_cny
                    reason = (
                        "order_amount_below_minimum" if budget is not None and budget < minimum * price else
                        "maximum_inventory_exceeded" if params.max_shares - shares < minimum else
                        "maximum_position_value_exceeded" if params.max_position_cny is not None
                            and params.max_position_cny < (shares + minimum) * price else
                        "insufficient_cash_including_fees" if cash < minimum * price + minimum_fees else
                        "participation_capacity_zero" if capacity is not None and capacity < minimum * price else
                        "invalid_buy_quantity"
                    )
                reason = reason or "cash_position_or_lot_limit"
            notional = max(0, quantity) * price
            breakdown = (fee_calculator.calculate(side=OrderSide(side), price=Price(price), quantity=Quantity(quantity),
                         trade_date=row.session_date) if quantity > 0 else FeeBreakdown.zero())
            fees = breakdown.total.amount
            if side == "buy" and notional + fees > cash:
                quantity = _buy_quantity(quantity - standard_buy_quantity_rule(history.board)[1],
                                         history.board)
                notional = max(0, quantity) * price
                breakdown = (fee_calculator.calculate(side=OrderSide(side), price=Price(price), quantity=Quantity(quantity),
                             trade_date=row.session_date) if quantity > 0 else FeeBreakdown.zero())
                fees = breakdown.total.amount
                if not quantity or notional + fees > cash:
                    reason = reason or "insufficient_cash_after_fees"
            if side == "sell" and fees >= notional:
                reason = reason or "proceeds_do_not_cover_fees"
            filled = 0 if reason else quantity
            fill_at = None
            if filled:
                intrabar = order.limit_price is not None and (
                    row.raw_open > order.limit_price if side == "buy" else row.raw_open < order.limit_price)
                fill_at = datetime.combine(row.session_date, time(15) if intrabar else time(9, 30),
                                           ZoneInfo("Asia/Shanghai"))
                identity = f"price-plan:{len(orders)}"
                fill = FillRecord(FillId(identity + ":fill"), OrderId(identity), instrument,
                                  OrderSide(side), Quantity(filled), Price(price), fill_at, breakdown)
                # Explicit calendars supply the next market session. Legacy
                # direct callers without one retain their prior T+1 calendar-
                # day floor; this never permits a same-day sale.
                next_day = next((day for day in session_indices if day > row.session_date),
                                row.session_date + timedelta(days=1))
                portfolio = (apply_buy(portfolio, fill, sellable_on=next_day)
                             if side == "buy" else apply_sell(portfolio, fill))
                if side == "buy" and not params.opening_shares and (order.initial or not benchmark_entered):
                    benchmark = apply_buy(benchmark, fill, sellable_on=next_day)
                    benchmark_entered = True
                cash = portfolio.cash.amount
                lots = _policy_lots(portfolio)
                average_entry_price = ((sum(lot.price * lot.shares for lot in lots) + portfolio.pending_share_principal(instrument))
                                       / (sum(lot.shares for lot in lots) + pending_shares)) if lots else None
                entry_index = session_indices[lots[0].bought] if lots else None
                total_fees += fees
                if order.initial:
                    initial_remaining -= filled
                else:
                    if order_policy is None:
                        params, baseline_units, filled_units, directional_reference = advance_grid_fill(
                            params, order, quantity=filled, price=price,
                            filled_units=filled_units, reference_price=directional_reference,
                        )
                    else:
                        baseline_units = order.baseline_units
                        filled_units += order.grid_units * D(filled) / order.quantity
                if order_policy is not None:
                    order_policy.on_fill(order=order, quantity=filled, price=price)
            orders.append({
                "signal_date": order.signal_date.isoformat(),
                "signal_at": (order.signal_at or datetime.combine(order.signal_date,
                    time(9, 30) if order.initial else time(15), ZoneInfo("Asia/Shanghai"))).isoformat(),
                "effective_at": datetime.combine(row.session_date, time(9, 30), ZoneInfo("Asia/Shanghai")).isoformat(),
                "filled_at": fill_at.isoformat() if fill_at is not None else None,
                "time_quality": ("daily_bar_available_at_proxy" if fill_at and fill_at.hour == 15
                                 else "daily_bar_open_proxy"),
                "date": row.session_date.isoformat(), "side": side,
                "requested_quantity": order.quantity, "filled_quantity": filled,
                "limit_price": order.limit_price, "price": price if filled else None,
                "fees_cny": fees if filled else D(0),
                "reason": reason or ("filled" if filled == order.quantity else "partial_fill"),
                "initial": order.initial, "cash_cny": cash,
                "shares": sum(lot.shares for lot in lots), "grid_units": filled_units,
                "pending_share_delta": portfolio.pending_share_delta(instrument),
                "dividend_receivable_cny": portfolio.dividend_receivable(instrument).amount,
                "baseline_units": order.baseline_units if not order.initial else None,
                "rule_id": order.rule_id, "trigger_price": order.trigger_price,
                "reference_price": order.reference_price,
                "cash_before_cny": cash_before,
                "shares_before": shares, "sellable_before": context.settled_shares,
                "average_entry_price": average_entry_price,
                "commission_cny": breakdown.commission.amount if filled else ZERO,
                "transfer_fee_cny": breakdown.transfer_fee.amount if filled else ZERO,
                "stamp_tax_cny": breakdown.stamp_tax.amount if filled else ZERO,
                "fee_policy": fee_calculator.policy_version.value,
            })
            if order_policy is not None and not order.initial:
                disposition = order_policy.on_session_end(
                    order=order, session=row.session_date, reason=str(orders[-1]["reason"]),
                )
                if disposition is not None:
                    orders[-1]["remainder_disposition"] = disposition
        portfolio = apply_boundary(portfolio, session, closing_clock, before=False, account="strategy")
        benchmark = apply_boundary(benchmark, session, closing_clock, before=False, account="benchmark")
        cash = portfolio.cash.amount
        shares = portfolio.position_quantity(instrument).value
        receivable = portfolio.dividend_receivable(instrument).amount
        pending_shares = portfolio.pending_share_delta(instrument)
        equity = cash + receivable + (shares + pending_shares) * row.raw_close
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        curve.append({"date": row.session_date.isoformat(), "equity_cny": equity,
                      "cash_cny": cash, "shares": shares,
                      "dividend_receivable_cny": receivable, "pending_share_delta": pending_shares,
                      "benchmark_equity_cny": benchmark.cash.amount + benchmark.dividend_receivable(instrument).amount
                          + (benchmark.position_quantity(instrument).value + benchmark.pending_share_delta(instrument)) * row.raw_close})
        if order_policy is not None:
            pending = order_policy.on_close(
                price=row.raw_close, session=row.session_date, board=history.board,
                context=PricePolicyContext(
                    shares, portfolio.sellable_quantity(instrument, row.session_date).value,
                    average_entry_price, None if entry_index is None else market_index - entry_index,
                    tuple((market_index - session_indices[lot.bought], lot.shares) for lot in lots),
                ),
            ) if not initial_remaining and row.trading_status is TradingStatus.TRADING else None
        else:
            pending = grid_order(params, price=row.raw_close, session=row.session_date,
                             filled_units=filled_units, board=history.board,
                             baseline_units=baseline_units or D(0),
                             startup_distance=(startup_distance if baseline_units is None
                                               else None),
                             reference_price=directional_reference) if (
            not initial_remaining and row.trading_status is TradingStatus.TRADING
            ) else None
    warnings = [
        "按收盘确认信号、下一交易日开始模拟委托；市价按开盘价，限价按当日开高低价检查，日线无法确定盘中成交时刻。",
        ("按不复权股价成交，共享账本按登记/除权/到账时点处理分红送转；现金分红为税前研究口径，配股不追加资金。"
         if corporate_actions is not None else "按不复权股价计算，不包含分红、送转股；非含分红总收益。"),
        "最小底仓为卖出下限；最大持仓市值限制买入，不因股价上涨强制卖出。",
    ]
    if order_policy is None:
        warnings.append("跳过多格时合并为一笔委托；受单笔数量、资金或持仓上限限制时仅成交允许部分。")
    if params.anchor_mode == "latest_price":
        warnings.append("以生成方案时取得并固定的行情最新价回看历史，属于当前参数研究；不是历史时点可获得信息的回测。")
    if adjustment_changed and corporate_actions is None:
        warnings.append("该区间检测到复权因子变化，分红送转会影响本结果，需结合公司行为复核。")
    warnings.append("印花税与过户费按成交市场及历史日期计算，旧参数中的固定税率不再用于本次执行；各费用分项按分舍入。")
    if params.opening_shares:
        warnings.append(
            "期初已有持仓直接导入且开盘可卖，不生成买入成交或买入费用；按区间首日开盘价估值。"
            "未提供历史买入成本和日期时，收益及成本型条件从该估值起算，持有时钟按前一交易日导入，"
            "不代表真实历史持仓成本和持有期限。"
        )
    if params.anchor_update == "last_fill":
        warnings.append(
            "成交后基准模式：每次实际网格成交（含部分成交）后按该次成交价重设基准；"
            "未成交余量在下次观察时重新规划。初始建仓不重设手动基准。"
        )
    if params.asymmetric:
        warnings.append(
            "买卖分别使用各自间距；固定模式按实际成交比例推进理论格线参考价，"
            "未成交不推进。基准百分比的元间距仍按固定基准计算；成交价重设模式另行重置。"
        )
    return {
        "state": "succeeded", "parameters": effective_parameters,
        "fee_policy": fee_calculator.policy_version.value,
        "initialization": {
            "anchor_mode": params.anchor_mode, "anchor_price": initial_anchor,
            "anchor_update": params.anchor_update, "final_anchor_price": params.resolved_anchor,
            "reference_date": first_row.session_date.isoformat(),
            "reference_open": first_row.raw_open, "startup_mode": params.startup_mode,
            "startup_distance": startup_distance,
            "final_directional_reference": directional_reference if params.asymmetric else None,
        },
        "summary": {
            "initial_cash_cny": initial_cash,
            "initial_equity_cny": initial_equity,
            "final_equity_cny": curve[-1]["equity_cny"],
            "total_return": D(str(curve[-1]["equity_cny"])) / initial_equity - 1,
            "max_drawdown": max_drawdown, "total_fees_cny": total_fees,
            "filled_orders": sum(bool(item["filled_quantity"]) for item in orders),
            "unfilled_orders": sum(not item["filled_quantity"] for item in orders),
            "final_shares": sum(lot.shares for lot in lots),
        },
        "opening_portfolio": {
            "shares": params.opening_shares, "cash_cny": initial_cash,
            "equity_cny": initial_equity, "capital_scope": params.initial_capital_scope,
            "valuation_date": opening_row.session_date.isoformat(),
            "valuation_price": opening_row.raw_open,
        },
        "series": curve, "orders": orders, "warnings": warnings,
        "corporate_action_policy": corporate_actions.policy_id if corporate_actions else None,
        "corporate_action_trace": corporate_trace,
        "benchmark_entered": benchmark_entered,
        "portfolio_ledger": {
            "engine": "shared_portfolio.v1", "cash_cny": portfolio.cash.amount,
            "fills": len(portfolio.fills), "journal_entries": len(portfolio.ledger_entries),
            "lots": [{"shares": lot.remaining_quantity.value, "acquired_on": lot.acquired_on.isoformat(),
                      "sellable_on": lot.sellable_on.isoformat(), "cost_basis_cny": lot.cost_basis.amount,
                      "principal_cny": lot.acquisition_principal.amount if lot.acquisition_principal else None}
                     for lot in portfolio.lots],
        },
        "provenance": {"instrument_id": history.instrument_id, "provider": history.provider,
                       "retrieved_at": history.retrieved_at.isoformat(),
                       "cache_status": history.cache_status,
                       "response_hashes": [e.response_sha256 for e in history.query_evidence],
                       "algorithm": "vnpy-grid-v2.1.0-adapter.v2",
                       "execution": "close_signal_next_session_open", "price_basis": "raw"},
    }
