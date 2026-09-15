"""Price-condition primitives adapted from official VeighNa code (MIT).

Copyright (c) 2015-present, Xiaoyou Chen.
StopAlgo.on_tick: vnpy/vnpy_algotrading@4133987530eb28f3538d1983545d81c4f83d7d59
AtrRsiStrategy.on_bar: vnpy/vnpy_ctastrategy@6ef76981624bf55b2ea978f8587f74d633aafc72
Full license: third_party_notices/vnpy-LICENSE.txt.

Keep the inclusive trigger default and extreme-price percentage calculation. Explicit
strict price comparison is a local extension. Decimal,
CNY offsets, and explicit upward/downward conditions are local adaptations.
These functions emit no fills, shorts, or orders to a broker.
"""
from decimal import Decimal
from typing import Literal


def price_reached(price: Decimal, target: Decimal, direction: Literal["up", "down"], *, inclusive: bool = True) -> bool:
    if not inclusive:
        return price > target if direction == "up" else price < target
    return price >= target if direction == "up" else price <= target


def trailing_price(
    extreme: Decimal, gap: Decimal, *, unit: Literal["cny", "percent"],
    direction: Literal["up", "down"],
) -> Decimal:
    offset = extreme * gap / 100 if unit == "percent" else gap
    return extreme + offset if direction == "up" else extreme - offset
