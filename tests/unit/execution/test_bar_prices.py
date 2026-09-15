from decimal import Decimal as D

from ashare_lab.domain.execution.bar_prices import BarPrices, match_bar_price, protective_exit
from ashare_lab.domain.orders import OrderSide


def test_limit_improvement_touch_and_close_only():
    bar = BarPrices(D(10), D(12), D(8), D(11))
    assert match_bar_price(bar, side=OrderSide.BUY, limit=D(11), slippage_bps=D(0)) == 10
    assert match_bar_price(bar, side=OrderSide.BUY, limit=D(9)) == 9
    assert match_bar_price(bar, side=OrderSide.SELL, limit=D(11)) == 11
    assert match_bar_price(bar, side=OrderSide.BUY, limit=D(9), observation="close") is None
    assert match_bar_price(bar, side=OrderSide.BUY, limit=D(7)) is None


def test_friction_cannot_escape_bar_or_limit():
    bar = BarPrices(D(10), D("10.01"), D("9.99"), D(10))
    assert match_bar_price(bar, side=OrderSide.BUY, slippage_bps=D(100)) == D("10.01")
    assert match_bar_price(bar, side=OrderSide.SELL, slippage_bps=D(100)) == D("9.99")
    assert match_bar_price(bar, side=OrderSide.BUY, limit=D(10)) == 10
    assert match_bar_price(bar, side=OrderSide.SELL, slippage_cny=D(100)) == D("9.99")


def test_protection_open_precedes_unknown_intrabar_order():
    assert protective_exit(BarPrices(D(10), D(12), D(8), D(10)),
        take_profit=D(11), stop_loss=D(9)) == ("stop_loss", True)
    assert protective_exit(BarPrices(D(12), D(12), D(8), D(10)),
        take_profit=D(11), stop_loss=D(9)) == ("take_profit", False)
