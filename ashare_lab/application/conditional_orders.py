"""Official price-condition primitives connected to the existing A-share ledger.

Only completed daily closes are observations in this release. A minute source
does not automatically authorize intrabar path, quote, or queue simulation.
"""
from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from decimal import Decimal

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistory
from ashare_lab.adapters.strategies.vnpy_conditions import price_reached, trailing_price
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.grid_strategy import (
    GridOrder,
    GridParameters,
    GridSpecificationError,
    PricePolicyContext,
    _buy_quantity,
    run_grid_backtest,
    rebase_price_order,
)
from ashare_lab.domain.market_data import Board, standard_buy_quantity_rule
from ashare_lab.domain.strategy.price_plans import ConditionParameters, ConditionRule

D = Decimal


class ConditionalOrderPolicy:
    """Signal-only state machine; settlement and money live in the shared ledger.

    Adjacent grouped conditions are OCO on trigger, not on fill. Exact
    simultaneous triggers use submitted order. A partial fill cannot advance
    a stage or reset its extreme. Cycles restart only after actual completion.
    """

    def __init__(self, rules: list[ConditionRule], repeat_cycles: int = 1, *, minimum_shares: int = 0) -> None:
        self.rules = rules
        self.minimum_shares = minimum_shares
        self.repeat_cycles = repeat_cycles
        self.cycles = 0
        self.index = 0
        self.remaining = 0
        self.pending: GridOrder | None = None
        self.extremes: dict[int, Decimal] = {}
        self.observations: list[dict[str, object]] = []
        self.cancelled: list[dict[str, int]] = []
        self.last_fill_price: Decimal | None = None
        self.first_observation_price: Decimal | None = None
        self.stage_notional = D(0)
        self.stage_quantity = 0
        self.holding_target_remaining = 0
        self.amount_remaining: Decimal | None = None
        self.initial_notional = D(0)
        self.initial_quantity = 0
        self.minimum_quantity = 100
        self.budget_residuals: list[dict[str, object]] = []
        self.selected_rule: int | None = None
        self.selected_stage_end: int | None = None
        self.terminal: dict[str, object] | None = None

    def _stage(self) -> range:
        end = self.index + 1
        group = self.rules[self.index].group
        while group is not None and end < len(self.rules) and self.rules[end].group == group:
            end += 1
        return range(self.index, end)

    def rebase_prices(self, rebase) -> None:
        self.rules = [rule.model_copy(update={
            "target_price": rebase.price(rule.target_price, rule.side),
            "limit_price": rebase.price(rule.limit_price, rule.side),
            "activation_price": rebase.price(rule.activation_price, rule.side),
            "gap": rebase.price(rule.gap, rule.side) if rule.gap_unit == "cny" else rule.gap,
        }) for rule in self.rules]
        self.pending = rebase_price_order(self.pending, rebase)
        self.extremes = {key: value * rebase.factor for key, value in self.extremes.items()}
        for key in ("last_fill_price", "first_observation_price", "stage_notional", "initial_notional"):
            value = getattr(self, key)
            if value is not None:
                setattr(self, key, value * rebase.factor)

    def _emit(self) -> GridOrder | None:
        if self.pending is None:
            return None
        return replace(self.pending, quantity=self.remaining,
                       amount_budget_cny=self.amount_remaining)

    def _arm(self, index: int, *, price: Decimal | None, target: Decimal | None,
             reference: Decimal | None, session: date, board: Board,
             quantity_override: int | None = None) -> GridOrder | None:
        rule = self.rules[index]
        quantity = rule.quantity
        if rule.sizing_mode == "amount":
            assert price is not None and rule.amount_cny is not None
            quantity = _buy_quantity(int(rule.amount_cny / price), board)
        if quantity_override is not None:
            quantity = quantity_override if rule.sizing_mode == "all_position" else min(quantity, quantity_override)
            if quantity <= 0:
                return None
        if self.selected_stage_end is None:
            self.selected_stage_end = self._stage().stop
        self.selected_rule = index
        self.remaining = quantity
        self.minimum_quantity = standard_buy_quantity_rule(board)[0]
        self.amount_remaining = rule.amount_cny if rule.sizing_mode == "amount" else None
        self.pending = GridOrder(
            rule.side, quantity, D(0), session, rule.limit_price,
            rule_id=f"cycle-{self.cycles + 1}:rule-{index + 1}",
            trigger_price=target, reference_price=reference,
            amount_budget_cny=self.amount_remaining,
            signal_at=datetime.combine(session, time(9, 30) if rule.kind == "holding_period" else time(15),
                                       ZoneInfo("Asia/Shanghai")),
        )
        self.observations.append({
            "rule_index": index, "cycle": self.cycles + 1, "kind": rule.kind,
            "date": session.isoformat(), "price": price, "trigger_price": target,
            "reference_price": reference, "extreme": self.extremes.get(index),
            "rule_id": self.pending.rule_id,
        })
        self.cancelled.extend({"cycle": self.cycles + 1, "rule_index": other}
                              for other in self._stage() if other != index)
        return self._emit()

    def on_open(self, *, session: date, board: Board,
                context: PricePolicyContext) -> GridOrder | None:
        self.holding_target_remaining = max(0, context.shares - self.minimum_shares)
        if self.terminal is not None:
            return None
        if self.pending is not None:
            if self.selected_rule is not None and self.rules[self.selected_rule].kind == "holding_period":
                target = self.rules[self.selected_rule].sessions
                due = sum(shares for age, shares in context.holding_batches if age >= target)
                if self.rules[self.selected_rule].sizing_mode == "all_position":
                    due = min(due, self.holding_target_remaining)
                self.remaining = min(self.remaining, due)
                if self.remaining == 0:
                    self.pending = None
                    return None
            return self._emit()
        if self.index >= len(self.rules):
            return None
        for index in ([self.selected_rule] if self.selected_rule is not None else self._stage()):
            rule = self.rules[index]
            due = sum(shares for age, shares in context.holding_batches if rule.sessions is not None and age >= rule.sessions)
            if rule.kind == "holding_period" and due > 0:
                # Calendar deadline known from actual entry, before seeing the
                # target day's opening price. Entry day is day zero.
                return self._arm(index, price=None, target=None,
                                 reference=context.average_entry_price,
                                 session=session, board=board,
                                 quantity_override=min(due, self.holding_target_remaining
                                     if rule.sizing_mode == "all_position"
                                     else rule.quantity - self.stage_quantity))
        return None

    def on_close(self, *, price: Decimal, session: date, board: Board,
                 context: PricePolicyContext) -> GridOrder | None:
        if self.first_observation_price is None:
            self.first_observation_price = price
        if self.terminal is not None:
            return None
        if self.pending is not None:
            return self._emit()
        if self.index >= len(self.rules):
            return None
        for index in ([self.selected_rule] if self.selected_rule is not None else self._stage()):
            rule = self.rules[index]
            if rule.kind == "holding_period":
                continue
            direction = rule.direction
            target = rule.target_price
            reference: Decimal | None = None
            if rule.kind in {"rebound", "pullback"}:
                if index not in self.extremes:
                    if rule.activation_price is not None and not price_reached(
                        price, rule.activation_price, "down" if rule.kind == "rebound" else "up",
                    ):
                        continue
                    self.extremes[index] = price
                self.extremes[index] = (min if rule.kind == "rebound" else max)(
                    self.extremes[index], price,
                )
                reference = self.extremes[index]
                direction = "up" if rule.kind == "rebound" else "down"
            elif rule.kind in {"take_profit", "stop_loss"}:
                if context.shares == 0 or context.average_entry_price is None:
                    continue
                reference = context.average_entry_price
                direction = "up" if rule.kind == "take_profit" else "down"
            elif rule.kind == "relative_price":
                reference = (
                    self.first_observation_price
                    if rule.reference_mode == "first_observation"
                    else self.last_fill_price
                )
                if reference is None:
                    continue
            if reference is not None:
                assert rule.gap is not None
                target = trailing_price(reference, rule.gap, unit=rule.gap_unit,
                                        direction=direction)
            assert target is not None
            if target <= 0 or not price_reached(price, target, direction,
                    inclusive=rule.kind != "price" or rule.price_comparison == "inclusive"):
                continue
            return self._arm(index, price=price, target=target, reference=reference,
                             session=session, board=board,
                             quantity_override=max(0, context.shares - self.minimum_shares)
                             if rule.sizing_mode == "all_position" else None)
        return None

    def on_session_end(self, *, order: GridOrder, session: date, reason: str) -> str | None:
        if self.pending is None or self.selected_rule is None:
            return None
        rule = self.rules[self.selected_rule]
        if self.remaining > 0 and rule.kind in {"take_profit", "stop_loss", "holding_period"}:
            # The daily research path retries at the next market open.
            # Preserve original trigger, remaining quantity, type and limit.
            return "retry_next_session"
        disposition = "expired_day" if order.limit_price is not None else "ended"
        self.terminal = dict(rule_id=order.rule_id, date=session.isoformat(),
                             reason=reason, disposition=disposition,
                             unfilled_quantity=self.remaining)
        self.pending = None
        self.remaining = 0
        return disposition

    def on_fill(self, *, order: GridOrder, quantity: int, price: Decimal) -> None:
        if order.initial:
            self.initial_notional += quantity * price
            self.initial_quantity += quantity
            self.last_fill_price = self.initial_notional / self.initial_quantity
            return
        self.remaining -= quantity
        self.stage_notional += quantity * price
        self.stage_quantity += quantity
        self.holding_target_remaining = max(0, self.holding_target_remaining - quantity)
        if self.amount_remaining is not None:
            self.amount_remaining -= quantity * price
            if self.remaining > 0 and self.amount_remaining < self.minimum_quantity * price:
                # A money-sized order ends at a fill when the balance cannot
                # fund another minimum declaration at that fill price. Record
                # the cancelled balance; never pretend the whole quantity filled.
                self.budget_residuals.append({
                    "rule_id": order.rule_id, "cancelled_quantity": self.remaining,
                    "unspent_amount_cny": self.amount_remaining,
                    "reference_fill_price": price,
                    "reason": "remaining_budget_below_minimum_declaration",
                })
                self.remaining = 0
        if self.remaining == 0:
            if (self.selected_rule is not None and self.rules[self.selected_rule].kind == "holding_period"
                    and (self.holding_target_remaining > 0
                         if self.rules[self.selected_rule].sizing_mode == "all_position"
                         else self.stage_quantity < self.rules[self.selected_rule].quantity)):
                self.pending = None
                return
            self.last_fill_price = self.stage_notional / self.stage_quantity
            self.index = self.selected_stage_end or self._stage().stop
            self.selected_rule = self.selected_stage_end = None
            self.pending = None
            self.extremes.clear()
            self.stage_notional = D(0)
            self.stage_quantity = 0
            if self.index == len(self.rules):
                self.cycles += 1
                if self.cycles < self.repeat_cycles:
                    self.index = 0


def run_conditional_backtest(
    *, params: ConditionParameters, history: MxDailyHistory, start: date, end: date,
    execution_config: BacktestRunConfig | None = None,
    market_sessions: tuple[date, ...] | None = None,
    corporate_actions=None, price_rebases=(),
) -> dict[str, object]:
    if params.observation != "daily_close":
        raise GridSpecificationError("分钟条件须使用分钟执行链路，不能回退日线。")
    # Reuse execution, T+1, capacity, cash, lot sizing and fees. Grid coordinates
    # are inert internal values: the explicit order policy bypasses grid signals.
    execution = GridParameters.model_validate({
        **params.model_dump(exclude={"rules", "observation", "repeat_cycles"}),
        "anchor_mode": "first_open", "startup_mode": "wait_for_crossing",
        "spacing": 1, "lower_price": "0.01", "upper_price": 1_000_000,
        "order_shares": 200 if history.board is Board.STAR else 100,
    })
    for rule in params.rules:
        if rule.side == "buy" and rule.sizing_mode == "shares" and (
            _buy_quantity(rule.quantity, history.board) != rule.quantity
        ):
            raise GridSpecificationError("条件单数量不满足该板块的申报数量规则")
    policy = ConditionalOrderPolicy(params.rules, params.repeat_cycles, minimum_shares=params.min_shares)
    result = run_grid_backtest(params=execution, history=history, start=start, end=end,
                               order_policy=policy, execution_config=execution_config,
                               market_sessions=market_sessions, corporate_actions=corporate_actions,
                               price_rebases=price_rebases)
    result["parameters"] = params.model_dump(mode="json")
    result.pop("initialization")
    end_order = policy._emit()
    unfinished_exit = (min(policy.remaining, result["summary"]["final_shares"])
                       if end_order is not None and end_order.side == "sell" else 0)
    end_cancellation = (dict(rule_id=end_order.rule_id, side=end_order.side,
                             signal_date=end_order.signal_date.isoformat(),
                             date=result["series"][-1]["date"], quantity=policy.remaining,
                             limit_price=end_order.limit_price, reason="backtest_ended")
                        if end_order is not None else None)
    # Capture the unfinished intent separately before cancelling the live order.
    policy.pending = None
    policy.remaining = 0
    result["condition_state"] = {
        "completed_rules": policy.index, "total_rules": len(params.rules),
        "pending_quantity": policy.remaining if policy.index < len(params.rules) else 0,
        "observations": policy.observations,
        "completed_cycles": policy.cycles, "requested_cycles": params.repeat_cycles,
        "cancelled_rules": policy.cancelled,
        "budget_residuals": policy.budget_residuals,
        "terminal_order": policy.terminal,
        "end_cancellation": end_cancellation,
        "unfinished_exit_quantity": unfinished_exit,
    }
    for order in result["orders"]:
        order.pop("grid_units")
        order.pop("baseline_units")
    result["warnings"] = [*result["warnings"],
        "按阶段顺序执行；同组条件先触发者锁定，其余取消；同时触发按填写顺序。前一阶段全部实际成交后才进入下一阶段。",
        "反弹/回落极值来自该条件开始观察后的已完成日线收盘价，不是盘中最高/最低价。",
        "普通单校验失败则结束，限价未成交余量收盘失效；止盈止损和持有期退出保留余量、类型及限价，日线研究按下一交易日开盘重试。",
        "这是日线条件单研究，不能作为分钟做T、盘口排队或券商真实条件单成交的证据。",
        "止盈止损基于持仓不含费用的加权成本；新买批次采用实际成交价，期初导入批次采用首日开盘估值。期限按各批次D0分别计时，到期开盘尝试卖出；期初批次日期依导入说明。",
        "价差接续以上一阶段实际成交的加权均价为基准；按金额委托在触发时确定股数，成交金额不超过预算，费用另计。",
        "按金额委托部分成交后，余额若不足以按该次成交价再申报最小数量，则取消余量并结束该阶段；账本保留未花金额和取消数量。",
    ]
    result["provenance"]["algorithm"] = "vnpy-price-conditions-adapter.v1"
    result["provenance"]["sources"] = {
        "stop": "vnpy/vnpy_algotrading@4133987530eb28f3538d1983545d81c4f83d7d59",
        "trailing": "vnpy/vnpy_ctastrategy@6ef76981624bf55b2ea978f8587f74d633aafc72",
    }
    return result
