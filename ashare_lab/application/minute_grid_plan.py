"""Translate persisted grid fields into independent, bounded fixed cells."""

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

from ashare_lab.application.fixed_grid_orders import GridCell, FixedGridOrders
from ashare_lab.domain.strategy.price_plans import GridParameters


class MinuteGridCapabilityError(ValueError):
    """The strategy was understood but its requested execution is not connected."""


@dataclass(frozen=True)
class CompiledMinuteGrid:
    parameters: GridParameters
    cells: tuple[GridCell, ...]


def _resolve_parameters(params: GridParameters, first_open: Decimal) -> GridParameters:
    if params.anchor_update not in {"fixed", "last_trigger"}:
        raise MinuteGridCapabilityError("moving_anchor_execution_not_connected")
    if params.anchor_mode in {"first_open", "latest_price"}:
        anchor = first_open if params.anchor_mode == "first_open" else params.anchor_price
        if anchor is None:
            raise MinuteGridCapabilityError("latest_price_unavailable")
        params = GridParameters.model_validate({**params.model_dump(), "anchor_price": anchor})
    params = params.resolve_geometry()
    anchor = params.resolved_anchor
    if not params.lower_price <= anchor <= params.upper_price:
        raise MinuteGridCapabilityError("historical_anchor_outside_grid_bounds")
    return params


def _grid_cells(params: GridParameters, side: str):
    anchor = params.resolved_anchor
    mode, spacing = params.spacing_for(side)
    reverse_mode, reverse_spacing = params.spacing_for("sell" if side == "buy" else "buy")

    def move(price: Decimal, gap_mode: str, gap: Decimal, direction: int) -> Decimal:
        if gap_mode == "percent":
            return price * (1 + gap / 100) if direction > 0 else price / (1 + gap / 100)
        step = gap if gap_mode == "cny" else anchor * gap / 100
        return price + direction * step

    level = anchor
    previous = None
    for index in range(1, 10001):
        level = move(level, mode, spacing, -1 if side == "buy" else 1)
        if level < params.lower_price or level > params.upper_price:
            break
        reverse = move(level, reverse_mode, reverse_spacing, 1 if side == "buy" else -1)
        buy, sell = (level, reverse) if side == "buy" else (reverse, level)
        buy = buy.quantize(Decimal(".01"), rounding=ROUND_FLOOR)
        sell = sell.quantize(Decimal(".01"), rounding=ROUND_CEILING)
        if buy < params.lower_price or sell > params.upper_price:
            continue
        order_type = "market" if params.price_mode == "next_open" else "fixed_limit"
        buy_limit = (
            params.buy_limit
            if params.price_mode == "fixed_limit"
            else buy + params.limit_offset_cny
        )
        sell_limit = (
            params.sell_limit
            if params.price_mode == "fixed_limit"
            else sell - params.limit_offset_cny
        )
        if (buy, sell) == previous:
            raise MinuteGridCapabilityError("grid_spacing_below_price_tick")
        previous = (buy, sell)
        yield GridCell(
            f"{side}:{index}",
            buy,
            sell,
            params.order_shares,
            side,
            order_type,
            buy_limit,
            sell_limit,
            params.order_amount_cny if params.sizing_mode == "amount" else None,
        )
    else:
        raise MinuteGridCapabilityError("grid_cell_count_exceeds_10000_per_side")


def compile_minute_grid(params: GridParameters, *, first_open: Decimal) -> CompiledMinuteGrid:
    if params.anchor_update == "last_trigger":
        raise MinuteGridCapabilityError("trigger_grid_requires_dynamic_policy")
    params = _resolve_parameters(params, first_open)
    cells = tuple(cell for side in ("buy", "sell") for cell in _grid_cells(params, side))
    if not cells:
        raise MinuteGridCapabilityError("grid_bounds_contain_no_complete_cell")
    if len({(c.first_side, c.buy_price, c.sell_price) for c in cells}) != len(cells):
        raise MinuteGridCapabilityError("grid_spacing_below_price_tick")
    return CompiledMinuteGrid(params, tuple(cells))


class LazyFixedGridOrders(FixedGridOrders):
    """Materialize only cells whose first-side threshold has been observed.

    The lattice and order IDs are unchanged. No future bar is consulted and
    untouched cells have no order, cash reservation, or inventory to maintain.
    """

    def __init__(self, params: GridParameters, *, first_open: Decimal,
                 active_sides: tuple[str, ...] = ("buy", "sell")):
        self.parameters = _resolve_parameters(params, first_open)
        super().__init__([], wait_for_crossing=params.startup_mode == "wait_for_crossing",
                         active_sides=active_sides)
        self.streams = {side: iter(_grid_cells(self.parameters, side)) for side in ("buy", "sell")}
        self.next_cells = {side: next(stream, None) for side, stream in self.streams.items()}
        self.rebases = []
        if not any(self.next_cells.values()):
            raise MinuteGridCapabilityError("grid_bounds_contain_no_complete_cell")

    def _current_cell(self, cell):
        for rebase, tick in self.rebases:
            cell = replace(
                cell,
                buy_price=rebase.price(cell.buy_price, "buy", tick),
                sell_price=rebase.price(cell.sell_price, "sell", tick),
                buy_limit=rebase.price(cell.buy_limit, "buy", tick),
                sell_limit=rebase.price(cell.sell_limit, "sell", tick),
            )
        return cell

    def rebase_prices(self, rebase, *, tick=Decimal(".01")):
        super().rebase_prices(rebase, tick=tick)
        self.rebases.append((rebase, tick))

    def observe(self, bar, index, **kwargs):
        if not self.paused:
            for side in ("buy", "sell"):
                if side not in self.active_sides:
                    continue
                while self.next_cells[side] is not None:
                    cell = self._current_cell(self.next_cells[side])
                    touched = (
                        bar.low <= cell.buy_price if side == "buy" else bar.high >= cell.sell_price
                    )
                    if not touched:
                        break
                    self.cells[cell.cell_id] = cell
                    self.sides[cell.cell_id] = side
                    self.armed_from[cell.cell_id] = 0
                    self.next_cells[side] = next(self.streams[side], None)
            # Preserve eager lattice ordering for simultaneous signals/fills.
            self.cells = dict(
                sorted(
                    self.cells.items(),
                    key=lambda item: (item[1].first_side != "buy", int(item[0].split(":")[1])),
                )
            )
        return super().observe(bar, index, **kwargs)


def execute_minute_grid(
    params: GridParameters, *, prepared, exchange, corporate_actions=None, price_rebases=(),
    execution_config=None, active_sides: tuple[str, ...] = ("buy", "sell"),
    daily_signals=(), protection=None, holding_sessions=None,
    holding_anchor="each_entry_fill", daily_position_risk=None, scheduled_orders=(),
):
    """Wire all supported persisted grid settings into the shared execution path."""
    from ashare_lab.application.minute_replay_input import replay_start
    from ashare_lab.application.opening_portfolio import opening_portfolio
    from ashare_lab.application.minute_grid_replay import replay_grid
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    from ashare_lab.domain.execution.fees import FeeCalculator, FeePolicy
    from ashare_lab.domain.shared import Money

    first_day, first_price, first_at = replay_start(prepared)
    fees = FeeCalculator(
        FeePolicy(exchange, params.commission_rate, Money(params.minimum_commission_cny))
    )
    if params.observation == 'daily_close' and len(active_sides) == 1:
        from datetime import time
        from zoneinfo import ZoneInfo
        from ashare_lab.application.daily_grid_orders import DailyGridOrders
        from ashare_lab.application.independent_order_routing import IndependentOrderRouting
        dates = {bar.session.session_date for bar in prepared.bars}
        for clock in (time(9, 31), time(15)):
            if dates != {bar.session.session_date for bar in prepared.bars
                         if bar.ended_at.astimezone(ZoneInfo('Asia/Shanghai')).time() == clock}:
                raise MinuteGridCapabilityError('composed_daily_open_or_close_missing')
        side, = active_sides
        daily = DailyGridOrders(params, first_open=first_price, side=side)
        policy = IndependentOrderRouting(entry=daily if side == 'buy' else FixedGridOrders([]),
            exit=daily if side == 'sell' else FixedGridOrders([]),
            minimum_shares=params.min_shares,
            observations={'entry' if side == 'buy' else 'exit': 'daily_close'})
        policy.active_sides = frozenset(active_sides)
        policy.parameters = daily.parameters
        policy.daily_grid_clocks = True
    elif params.anchor_update == "last_trigger":
        from ashare_lab.application.trigger_grid_orders import TriggerGridOrders
        policy = TriggerGridOrders(_resolve_parameters(params, first_price), fees, active_sides=active_sides)
    else:
        policy = LazyFixedGridOrders(params, first_open=first_price, active_sides=active_sides)
    effective = policy.parameters
    initial = ()
    if effective.initial_shares and (effective.anchor_update != "last_trigger" or effective.observation == 'daily_close'):
        initial = (
            ScheduledOrder(
                "grid-initial-build", first_day, first_at, quantity=effective.initial_shares
            ),
        )
    portfolio, initial_equity = opening_portfolio(effective, prepared, first_price=first_price)
    return replay_grid(
        policy,
        list(prepared.bars),
        portfolio,
        fees,
        slippage_bps=effective.slippage_bps,
        slippage_cny=effective.slippage_cny,
        scheduled_orders=(*initial, *scheduled_orders),
        daily_signals=daily_signals, grid_composition=len(active_sides) == 1,
        protection=protection, holding_sessions=holding_sessions, holding_anchor=holding_anchor,
        daily_position_risk=daily_position_risk,
        market_sessions=prepared.market_sessions,
        minimum_shares=effective.min_shares,
        maximum_shares=effective.max_shares,
        maximum_position_cny=effective.max_position_cny,
        corporate_actions=corporate_actions,
        price_rebases=price_rebases,
        nontrading_closes=getattr(prepared, "nontrading_closes", ()),
        initial_equity_cny=initial_equity,
        **(dict(capacity_mode=execution_config.capacity_mode,
                participation_rate=execution_config.participation_rate)
           if execution_config is not None else {}),
    )
