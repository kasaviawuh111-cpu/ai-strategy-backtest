"""Adapted from vn.py v2.1.0 GridAlgo.on_timer (MIT).

Copyright (c) 2015-present, Xiaoyou Chen.
Source: vnpy/vnpy@3fdabe8007801b871e4932ed02515e2461a51aff
Path: vnpy/app/algo_trading/algos/grid_algo.py
Full license: third_party_notices/vnpy-LICENSE.txt.

Retains the floor/ceil target-position algorithm; Decimal replaces float.
The caller supplies CNY or geometric-percent distance in grid units.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def target_grid_change(distance: Decimal, filled_grid_units: Decimal) -> Decimal:
    """The floor/ceil gap prevents a trade on a sub-grid move.

    Only confirmed fills may change ``filled_grid_units``.
    """
    target_buy_position = distance.to_integral_value(rounding=ROUND_FLOOR)
    target_buy_volume = target_buy_position - filled_grid_units
    target_sell_position = distance.to_integral_value(rounding=ROUND_CEILING)
    target_sell_volume = filled_grid_units - target_sell_position
    if target_buy_volume > 0:
        return target_buy_volume
    if target_sell_volume > 0:
        return -target_sell_volume
    return Decimal(0)
