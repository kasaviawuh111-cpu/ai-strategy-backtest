from decimal import Decimal as D

import pytest

from ashare_lab.application.grid_strategy import grid_order
from ashare_lab.domain.market_data import Board
from tests.unit.application.test_grid_strategy import DATES, parameters, run


def settings(**extra):
    return dict(anchor_price=100, lower_price=1, upper_price=200,
                buy_spacing=3, sell_spacing=1, buy_spacing_mode="anchor_percent",
                sell_spacing_mode="anchor_percent", **extra)


def test_three_percent_buy_one_percent_sell_and_no_repeat_at_same_price():
    p = parameters(**settings())
    assert grid_order(p, price=D(99), session=DATES[0], filled_units=D(0),
                      board=Board.MAIN) is None
    result = run([("100", "99"), ("99", "97"), ("97", "98"), ("98", "98")], **settings())
    buy, sell = result["orders"]
    assert (buy["side"], buy["trigger_price"], buy["filled_quantity"]) == ("buy", 97, 100)
    assert (sell["side"], sell["trigger_price"], sell["filled_quantity"]) == ("sell", 98, 100)
    assert sell["reference_price"] == 97
    assert result["summary"]["final_shares"] == 0
    # One million initial cash + 100 gross P&L - .10/.10 transfer - 4.90 stamp.
    assert result["summary"]["final_equity_cny"] == D("1000094.90")


def test_mixed_units_remain_independent():
    result = run([("20", "19"), ("19", "19.2"), ("19.2", "19.2")],
                 anchor_price=20, lower_price=1, upper_price=100,
                 buy_spacing=1, buy_spacing_mode="cny",
                 sell_spacing=1, sell_spacing_mode="anchor_percent")
    assert [o["trigger_price"] for o in result["orders"]] == [D(19), D("19.2")]


def test_failed_order_keeps_reference_and_retries_original_buy_threshold():
    # _row sets low=min(open, close)-1, so a 99/99 bar has low=98 > 97.
    # A low at 97 is reachable and must not be called an unfilled limit order.
    result = run([("100", "97"), ("99", "99"), ("98", "97"), ("97", "97")],
                 **settings(price_mode="grid_limit"))
    failed, filled = result["orders"]
    assert failed["filled_quantity"] == 0
    assert filled["filled_quantity"] == 100
    assert failed["reference_price"] == filled["reference_price"] == 100
    assert failed["trigger_price"] == filled["trigger_price"] == 97


def test_partial_fill_advances_only_executed_grids():
    result = run([("100", "94"), ("94", "95"), ("98", "98"), ("98", "98")],
                 **settings(max_shares=100))
    buy, sell = result["orders"]
    assert buy["requested_quantity"] == 200 and buy["filled_quantity"] == 100
    assert sell["reference_price"] == 97  # One executed 3% grid, not both requested grids.
    assert sell["trigger_price"] == 98


def test_last_fill_updates_from_actual_price_and_fixed_mode_does_not():
    prices = [("100", "97"), ("96", "97"), ("97", "97")]
    fixed = run(prices, **settings())
    moving = run(prices, **settings(anchor_update="last_fill"))
    assert len(fixed["orders"]) == 1
    assert moving["orders"][1]["reference_price"] == 96
    assert moving["orders"][1]["trigger_price"] == D("96.96")


def test_equal_directional_values_preserve_existing_symmetric_execution():
    prices = [("10", "8"), ("8", "9"), ("9", "9")]
    legacy = run(prices)
    explicit = run(prices, buy_spacing=1, sell_spacing=1,
                   buy_spacing_mode="cny", sell_spacing_mode="cny")
    assert legacy["orders"] == explicit["orders"]
    assert legacy["series"] == explicit["series"]


def test_amount_budget_t_plus_one_and_inventory_floor_still_apply():
    result = run([("100", "101"), ("101", "101"), ("101", "101")],
                 **settings(initial_shares=200, min_shares=100,
                            sizing_mode="amount", order_amount_cny=20000))
    initial, sell = result["orders"]
    assert initial["date"] < sell["date"]
    assert sell["filled_quantity"] == 100
    assert sell["price"] * sell["filled_quantity"] <= 20000
    assert result["summary"]["final_shares"] == 100


def test_geometric_directional_threshold_keeps_price_ratio_definition():
    p = parameters(anchor_price=100, lower_price=1, upper_price=200,
                   buy_spacing=3, sell_spacing=1, spacing_mode="percent")
    first = grid_order(p, price=D(100)/D("1.03"), session=DATES[0],
                       filled_units=D(0), board=Board.MAIN)
    assert first.side == "buy" and first.quantity == 100
    next_price = first.trigger_price * D("1.01")
    second = grid_order(p, price=next_price, reference_price=first.trigger_price,
                        session=DATES[1], filled_units=D(1), board=Board.MAIN)
    assert second.side == "sell" and second.quantity == 100
    assert abs(second.trigger_price - next_price) < D("1e-20")


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_invalid_percent_on_either_side_is_not_hidden_by_legacy_spacing(side):
    with pytest.raises(ValueError, match="百分比"):
        parameters(**{f"{side}_spacing": 100, f"{side}_spacing_mode": "anchor_percent"})
