"""Shared report explanations. Never changes strategy parameters or fills."""
from collections import Counter
from collections.abc import Mapping, Sequence


def execution_capability_feedback(reason: str) -> tuple[str, str]:
    """Safe application-owned explanations shared by submission and execution."""
    explanations = {
        "trigger_grid_requires_minute_execution": (
            "grid_execution_unavailable", "触发后更新基准的网格需要分钟执行；未改用日线旧算法。",
        ),
        "moving_anchor_execution_not_connected": (
            "grid_execution_unavailable",
            "当前分钟引擎尚不支持成交后移动基准；未改为固定基准。",
        ),
        "latest_price_unavailable": (
            "grid_latest_quote_unavailable", "行情最新价尚未取得；未改用起始开盘价。",
        ),
        "historical_anchor_outside_grid_bounds": (
            "grid_parameters_unavailable", "本次确定的基准价不在设定网格上下限内。",
        ),
        "grid_spacing_below_price_tick": (
            "grid_parameters_unavailable", "网格间距按最小价格单位取整后出现重复价位。",
        ),
        "grid_cell_count_exceeds_10000_per_side": (
            "grid_parameters_unavailable", "网格数量超过当前引擎单侧10000格的上限。",
        ),
        "grid_bounds_contain_no_complete_cell": (
            "grid_parameters_unavailable", "当前上下限与间距无法形成完整的买卖网格。",
        ),
        "holding_period_market_calendar_missing": (
            "market_calendar_unavailable", "持有期限计算所需的交易日历尚未取得。",
        ),
        "opening_holding_prior_session_missing": (
            "opening_holdings_data_unavailable", "期初持仓缺少前一交易日依据，无法确认可卖状态。",
        ),
        "opening_holding_exceeds_initial_equity": (
            "opening_holdings_exceed_equity",
            "期初持仓市值超过设定的初始总资产，无法分配剩余现金。",
        ),
        "conditional_initial_reference_missing": (
            "condition_reference_unavailable",
            "第一笔缺少起始基准价；尚不存在上一笔成交价，请设置第一笔触发价。",
        ),
        "conditional_cost_provenance_missing": (
            "condition_cost_unavailable", "持仓成交成本依据缺失，暂不能计算成本止盈止损。",
        ),
    }
    disabled = {
        "hybrid_execution_disabled", "minute_grid_execution_disabled",
        "minute_conditional_execution_disabled",
    }
    if reason in disabled:
        return (
            "minute_execution_unavailable",
            "这套策略的回测暂时不可用。你的股票和买卖规则已保留，稍后可以继续。",
        )
    else:
        code, detail = explanations.get(reason, (
            "execution_capability_unavailable", "所需的执行方式尚未接通。",
        ))
    return code, "策略已理解，" + detail + "原规则与设置已保留。"


UNFILLED_REASONS = {
    "grid_price_trigger_anchor_updated": "价格到达网格条件，按此次触发价更新基准；委托等待下一分钟撮合。",
    "grid_price_outside_range": "当前方向的触发价或委托价超出设定价格区间。",
    "no_position_to_sell": "当前持仓为0股，没有可卖股票；现金充足也不能直接卖出股票。",
    "insufficient_position": "可卖持仓不足。",
    "minimum_inventory_or_t_plus_one": "底仓限制或T+1可卖数量限制；旧记录未分别记录具体约束。",
    "minimum_inventory_reached": "持仓已达到保留底仓，继续卖出会低于设定的最小持仓。",
    "minimum_inventory_breached": "本次卖出将低于设定底仓，未执行。",
    "maximum_inventory_exceeded": "本次买入将超过最大持仓股数。",
    "maximum_position_value_exceeded": "本次买入将超过最大持仓金额。",
    "t_plus_one_no_sellable_shares": "持仓尚未满足T+1可卖条件，本次没有可卖股票。",
    "t_plus_one_locked": "持仓尚未满足T+1可卖条件。",
    "sell_quantity_below_lot": "本次可卖数量未满足申报数量规则。",
    "invalid_buy_quantity": "委托股数未满足该股票的最低申报股数或递增单位。",
    "order_amount_below_minimum": "单次委托金额不足以买入该股票的最低申报股数。",
    "amount_below_minimum_order": "单次委托金额不足以买入该股票的最低申报股数。",
    "budget_below_minimum_order": "单次定投预算不足以买入最低申报股数并支付费用，本期跳过，不自动追加预算或补投。",
    "insufficient_cash_including_fees": "可用资金不足以支付本次委托及费用。",
    "insufficient_cash_after_fees": "计入费用后可用资金不足。",
    "participation_capacity_zero": "当前容量约束下没有可成交数量。",
    "participation_partial_fill": "达到本次成交量参与率上限，委托仅部分成交。",
    "inventory_limit_partial_fill": "按最大持仓股数约束买入数量，委托仅部分成交。",
    "position_value_limit_partial_fill": "按最大持仓金额约束买入数量，委托仅部分成交。",
    "security_not_trading": "该证券当前未处于可交易状态。",
    "no_market_trades": "本根行情无市场成交，不模拟成交；是否继续等待以订单状态为准。",
    "adverse_price_limit": "买入触及涨停价或卖出触及跌停价，按本次成交设置未成交。",
    "buy_at_upper_limit": "买入触及涨停价，按本次成交设置未成交。",
    "sell_at_lower_limit": "卖出触及跌停价，按本次成交设置未成交。",
    "limit_not_reached": "本根价格未达到委托限价，尚未成交。",
    "bar_available_after_order_expiry": "委托在本根行情结束前已到期，无法确认触价发生在有效期内，不模拟成交。",
    "buy_limit_not_marketable": "买入限价未满足本次撮合条件。",
    "sell_limit_not_marketable": "卖出限价未满足本次撮合条件。",
    "cash_position_or_lot_limit": "资金、持仓额度或申报股数限制导致无法委托；旧记录未分别记录具体约束。",
    "proceeds_do_not_cover_fees": "卖出金额不足以支付交易费用。",
    "day_expired": "当日委托有效期结束，未成交余量已撤销。",
    "day_order_expired": "当日委托有效期结束，未成交余量已撤销；已成交部分保留。",
    "backtest_ended": "回测区间结束，未成交余量不再撮合。",
}


def zero_fill_execution_note(activities: Sequence[Mapping[str, object]]) -> str | None:
    """Describe no fills, not zero closed cycles; count each order only once."""
    if any(a.get("kind") in {"fill", "partial_fill"} for a in activities):
        return None
    orders: dict[str, Mapping[str, object]] = {}
    priority = {"order": 0, "expired": 1, "unfilled": 2}
    for activity in activities:
        if activity.get("kind") not in {"order", "unfilled", "expired"}:
            continue
        identity = str(activity.get("orderId") or activity.get("chainId") or activity.get("id"))
        # Keep the concrete rejection instead of replacing it with a later DAY cancellation.
        if (identity not in orders
                or priority[str(activity["kind"])] >= priority[str(orders[identity]["kind"])]):
            orders[identity] = activity
    if not orders:
        return "本次未产生可执行委托，未发生买卖；可核对条件是否触发及委托生效日期。"
    reasons = Counter(str(a.get("outcomeReason")
                          or (a.get("executionDetails") or {}).get("reason")
                          or a.get("reason") or "") for a in orders.values())
    details = "；".join(f"{count}笔：{UNFILLED_REASONS.get(reason, '具体原因见逐笔委托记录。')}"
                       for reason, count in reasons.most_common(3))
    return f"本次{len(orders)}笔委托均未成交。{details}"


def untriggered_combination_note(signals, expected_dates, activities) -> str | None:
    """Explain only fully observed AND conditions; missing facts are not false."""
    if any(a.get("kind") in {"order", "fill", "partial_fill"} for a in activities):
        return None
    rows = {str(s["session_date"]): s for s in signals if s is not None}
    selected = [rows.get(str(day)) for day in expected_dates]
    if not selected or any(s is None for s in selected):
        return None
    references = None
    counts = []
    for row in selected:
        children = row.get("children") or []
        if row.get("condition_ref") != "all" or row.get("triggered") is not False or len(children) < 2:
            return None
        refs = tuple(c.get("condition_ref") for c in children)
        if references is None:
            references, counts = refs, [0] * len(children)
        if refs != references or any(type(c.get("triggered")) is not bool for c in children):
            return None
        if all(c["triggered"] for c in children):
            return None
        for i, child in enumerate(children):
            counts[i] += int(child["triggered"])
    detail = "各项条件曾分别满足，但没有在同一天同时满足" if all(counts) else "买入条件没有在同一天全部满足"
    return f"本次已检查{len(selected)}个交易日，{detail}，因此未触发买入、没有产生委托。"
