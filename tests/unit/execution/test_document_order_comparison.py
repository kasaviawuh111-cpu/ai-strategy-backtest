"""Doc-derived examples, NOT exports from a Tonghuashun account."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from ashare_lab.domain.execution import CapacityMode
from ashare_lab.domain.execution.fees import AshareExchange, FeeCalculator, FeePolicy
from ashare_lab.domain.market_data import TradingStatus
from ashare_lab.domain.orders import OrderSide, OrderType
from ashare_lab.domain.shared import Money
from scripts.compare_backtest_records import compare_records
from tests.unit.execution.test_daily_matching import (
    DAY,
    dt,
    make_bar,
    make_order,
    match,
    session,
)


def export(price, quantity, *, status="ACTIVE", source="document_fixture"):
    return {
        "evidence_kind": source,
        "context": {
            "instrument": "300059.SZ", "start": str(DAY), "end": str(DAY),
            "frequency": "1d", "initial_cash_cny": "20000", "data_sha256": "fixture-ohlcv-v1",
            "settings": {"slippage_bps": "0", "slippage_cny": "0",
                         "commission_rate": "0.0003", "minimum_commission_cny": "5",
                         "participation_rate": "0.25", "volume_basis": "explicit_point_in_time",
                         "execution_price_basis": "raw_open_proxy"},
        },
        "orders": [{"key": "buy-1", "instrument": "300059.SZ", "side": "buy",
                    "requested_quantity": 1000, "filled_quantity": quantity,
                    "status": status, "submitted_at": dt(9, 21).isoformat(),
                    "terminated_at": (dt(15, 0).isoformat()
                                      if status in {"EXPIRED", "CANCELLED"} else None),
                    "limit_price": "12", "average_fill_price": price if quantity else None}],
        "fills": [{"key": "buy-1-fill-1", "order_key": "buy-1", "instrument": "300059.SZ",
                   "side": "buy", "at": dt(9, 30).isoformat(), "quantity": quantity,
                   "price": price, "commission_cny": "5", "stamp_tax_cny": "0",
                   "transfer_fee_cny": "0.03"}] if quantity else [],
        "account": {"cash_cny": "17494.97" if quantity else "20000", "shares": quantity},
    }


def test_actual_matching_and_fee_components_against_document_fixture():
    # Only price/quantity/fee mechanics are compared. This explicit pre-open
    # observation is NOT THS's current-day-volume convention.
    from ashare_lab.domain.execution import PointInTimeVolume, VolumeSource
    from ashare_lab.domain.shared import Quantity
    result = match(make_bar("10", "10.5", "9.5", "10"), participation_rate=D("0.25"),
                   point_in_time_volume=PointInTimeVolume(
                       quantity=Quantity(1000), known_at=dt(9, 0),
                       source=VolumeSource.EXPLICIT_POINT_IN_TIME_OBSERVATION))
    assert result.quantity.value == 250
    fees = FeeCalculator(FeePolicy(exchange=AshareExchange.SHENZHEN,
                                   commission_rate=D("0.0003"), minimum_commission=Money(D(5))))
    costs = fees.calculate(side=OrderSide.BUY, price=result.price,
                           quantity=result.quantity, trade_date=DAY)
    reference = export("10", 250)
    actual = export(result.price.amount, result.quantity.value,
                    status="PARTIALLY_FILLED", source="local_run")
    actual["fills"][0].update(commission_cny=costs.commission.amount,
                               stamp_tax_cny=costs.stamp_tax.amount,
                               transfer_fee_cny=costs.transfer_fee.amount)
    assert compare_records(reference, actual)["status"] == "matched_document_fixture"


@pytest.mark.parametrize(("side", "limit", "expected", "hour"), [
    (OrderSide.BUY, "10.2", "10.01", 9), (OrderSide.SELL, "9.8", "9.99", 9),
    (OrderSide.BUY, "9.8", "9.8", 15), (OrderSide.SELL, "10.2", "10.2", 15),
    (OrderSide.BUY, "8.9", None, None), (OrderSide.SELL, "11.1", None, None),
])
def test_explicit_daily_limit_obeys_phase_one_ohlc_and_timestamp(side, limit, expected, hour):
    from ashare_lab.domain.shared import Price
    order = replace(make_order(side), order_type=OrderType.LIMIT, limit_price=Price(D(limit)))
    result = match(make_bar("10", "11", "9", "10"), order=order, slippage_bps=D(5))
    assert (result.price.amount if result.price else None) == (D(expected) if expected else None)
    assert (result.filled_at.hour if result.filled_at else None) == hour
    if hour == 15:
        assert result.time_quality.value == "daily_bar_available_at_proxy"
        assert result.reason_code == "matched_intrabar_limit"


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_intrabar_limit_cannot_use_prices_after_early_expiry(side):
    from ashare_lab.domain.shared import Price
    order = replace(make_order(side), order_type=OrderType.LIMIT,
                    limit_price=Price(D("9.8" if side is OrderSide.BUY else "10.2")),
                    valid_until=dt(12, 0))
    result = match(make_bar("10", "11", "9", "10"), order=order)
    assert result.price is None
    assert result.reason_code == "bar_available_after_order_expiry"


@pytest.mark.parametrize(("side", "expected"), [(OrderSide.BUY, "11"), (OrderSide.SELL, "9")])
def test_fixed_yuan_and_proportional_slippage_remain_distinct(side, expected):
    result = match(make_bar("10", "11", "9", "10"), order=make_order(side),
                   slippage_cny=D(1), slippage_bps=D(10))
    assert result.price.amount == D(expected)


def test_extreme_fixed_slippage_clips_to_bar_instead_of_parse_failure_or_crash():
    result = match(make_bar("10", "11", "9", "10"), order=make_order(OrderSide.SELL),
                   slippage_cny=D(100))
    assert result.quantity.value == 1000 and result.price.amount == D(9)


@pytest.mark.parametrize(("bar", "state", "reason"), [
    (make_bar("10", "10", "10", "10"), TradingStatus.SUSPENDED, "security_suspended"),
    (make_bar("12", "12", "12", "12"), TradingStatus.TRADING, "one_price_limit_up"),
])
def test_untradable_document_cases(bar, state, reason):
    result = match(bar, session=session(state), capacity_mode=CapacityMode.UNLIMITED,
                   point_in_time_volume=None)
    assert result.quantity.value == 0 and result.reason_code == reason


def test_expired_partial_order_is_not_rejected_or_lost_fill():
    reference, actual = export("10", 250, status="CANCELLED"), export("10", 250, status="EXPIRED")
    assert compare_records(reference, actual)["status"] == "matched_document_fixture"
    actual["orders"][0]["filled_quantity"] = 0
    assert compare_records(reference, actual)["status"] == "incomplete"


def test_price_and_fee_differences_are_located_per_fill():
    reference = export("10", 250)
    actual = deepcopy(reference)
    actual["fills"][0]["commission_cny"] = "5.01"
    result = compare_records(reference, actual)
    assert result["status"] == "different"
    assert result["differences"][0]["path"] == "fills.buy-1-fill-1.commission_cny"


def test_missing_qty_empty_exports_and_fake_platform_proof_cannot_pass():
    reference = export("10", 250)
    missing = deepcopy(reference)
    missing["orders"][0]["filled_quantity"] = None
    assert compare_records(reference, missing)["status"] == "incomplete"
    assert compare_records({}, {})["status"] == "incomplete"
    reference["evidence_kind"] = "platform_export"
    assert compare_records(reference, reference)["status"] == "incomplete"
