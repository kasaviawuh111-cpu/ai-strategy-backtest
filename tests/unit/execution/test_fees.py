from datetime import date
from decimal import Decimal

import pytest

from ashare_lab.domain.execution import (
    AshareExchange,
    FeeCalculator,
    FeePolicy,
    FeePolicyVersion,
    UnsupportedFeePolicyError,
)
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import Money, Price, Quantity


def calculator(
    *,
    rate: str = "0.0003",
    minimum: str = "5",
    exchange: AshareExchange = AshareExchange.SHANGHAI,
) -> FeeCalculator:
    return FeeCalculator(
        FeePolicy(
            exchange=exchange,
            commission_rate=Decimal(rate),
            minimum_commission=Money(Decimal(minimum)),
        )
    )


def calculate(
    *,
    side: OrderSide,
    day: date,
    gross: str = "100000",
    exchange: AshareExchange = AshareExchange.SHANGHAI,
):
    return calculator(exchange=exchange).calculate(
        side=side,
        price=Price(Decimal(gross)),
        quantity=Quantity(1),
        trade_date=day,
    )


def test_policy_version_is_exposed_for_run_manifest_audit() -> None:
    fees = calculator()

    assert fees.policy_version is FeePolicyVersion.CASH_EQUITY_2015_V1


def test_commission_uses_configurable_minimum_on_both_sides() -> None:
    fees = calculator(rate="0.0003", minimum="5")

    buy = fees.calculate(
        side=OrderSide.BUY,
        price=Price(Decimal("10")),
        quantity=Quantity(100),
        trade_date=date(2025, 1, 2),
    )
    sell = fees.calculate(
        side=OrderSide.SELL,
        price=Price(Decimal("10")),
        quantity=Quantity(100),
        trade_date=date(2025, 1, 2),
    )

    assert buy.commission == Money(Decimal("5.00"))
    assert sell.commission == Money(Decimal("5.00"))


def test_commission_uses_configured_rate_when_it_exceeds_minimum() -> None:
    fees = calculator(rate="0.00025", minimum="5").calculate(
        side=OrderSide.BUY,
        price=Price(Decimal("20")),
        quantity=Quantity(2000),
        trade_date=date(2025, 1, 2),
    )

    assert fees.commission == Money(Decimal("10.00"))


def test_stamp_tax_is_sell_only_and_reduces_on_2023_08_28() -> None:
    buy_before = calculate(side=OrderSide.BUY, day=date(2023, 8, 27))
    sell_before = calculate(side=OrderSide.SELL, day=date(2023, 8, 27))
    sell_after = calculate(side=OrderSide.SELL, day=date(2023, 8, 28))

    assert buy_before.stamp_tax == Money(Decimal("0.00"))
    assert sell_before.stamp_tax == Money(Decimal("100.00"))
    assert sell_after.stamp_tax == Money(Decimal("50.00"))


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2015, 8, 1), "2.00"),
        (date(2022, 4, 28), "2.00"),
        (date(2022, 4, 29), "1.00"),
    ],
)
def test_sh_sz_transfer_fee_is_bilateral_across_historical_cutoff(
    side: OrderSide, day: date, expected: str
) -> None:
    fees = calculate(side=side, day=day)

    assert fees.transfer_fee == Money(Decimal(expected))


def test_bse_old_transfer_fee_is_explicitly_unsupported() -> None:
    with pytest.raises(UnsupportedFeePolicyError, match="before 2022-04-29"):
        calculate(
            side=OrderSide.BUY,
            day=date(2022, 4, 28),
            exchange=AshareExchange.BEIJING,
        )


def test_sh_sz_transfer_fee_before_policy_start_is_explicitly_unsupported() -> None:
    with pytest.raises(UnsupportedFeePolicyError, match="before 2015-08-01"):
        calculate(side=OrderSide.SELL, day=date(2015, 7, 31))


def test_bse_uses_unified_transfer_fee_from_2022_04_29() -> None:
    fees = calculate(
        side=OrderSide.BUY,
        day=date(2022, 4, 29),
        exchange=AshareExchange.BEIJING,
    )

    assert fees.transfer_fee == Money(Decimal("1.00"))


def test_each_fee_component_rounds_half_up_to_cents() -> None:
    fees = calculator(rate="0.0005", minimum="0").calculate(
        side=OrderSide.SELL,
        price=Price(Decimal("10.10")),
        quantity=Quantity(100),
        trade_date=date(2025, 1, 2),
    )

    assert fees.commission == Money(Decimal("0.51"))
    assert fees.stamp_tax == Money(Decimal("0.51"))
    assert fees.transfer_fee == Money(Decimal("0.01"))
    assert fees.other == Money(Decimal("0.00"))
