"""Versioned, exact A-share cash-equity transaction fees.

The policy deliberately records the settlement venue and the calculator
receives the trade date for every fill.  It does not infer venue from a
security code and never applies the latest fee schedule retroactively.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import FeeBreakdown
from ashare_lab.domain.shared import (
    CurrencyMismatchError,
    DomainValidationError,
    Money,
    Price,
    Quantity,
)

_CENT = Decimal("0.01")
_SH_SZ_TRANSFER_FEE_START = date(2015, 8, 1)
_UNIFIED_TRANSFER_FEE_START = date(2022, 4, 29)
_STAMP_TAX_REDUCTION_START = date(2023, 8, 28)


class AshareExchange(StrEnum):
    """Settlement venue required by the historical transfer-fee schedule."""

    SHANGHAI = "SH"
    SHENZHEN = "SZ"
    BEIJING = "BJ"


class FeePolicyVersion(StrEnum):
    """Stable identity of the implemented historical rule set."""

    CASH_EQUITY_2015_V1 = "cn.a_share.cash_equity_fees.2015_present.v1"


class UnsupportedFeePolicyError(DomainValidationError):
    """Raised when the selected policy has no defensible historical rate."""


@dataclass(frozen=True, slots=True)
class FeePolicy:
    """Broker-specific commission terms plus a versioned statutory schedule."""

    exchange: AshareExchange
    commission_rate: Decimal
    minimum_commission: Money
    version: FeePolicyVersion = FeePolicyVersion.CASH_EQUITY_2015_V1

    def __post_init__(self) -> None:
        if type(self.version) is not FeePolicyVersion:
            raise DomainValidationError("version must be a supported FeePolicyVersion")
        if type(self.exchange) is not AshareExchange:
            raise DomainValidationError("exchange must be AshareExchange")
        if type(self.commission_rate) is not Decimal:
            raise DomainValidationError("commission_rate must be Decimal")
        if not self.commission_rate.is_finite() or self.commission_rate < 0:
            raise DomainValidationError("commission_rate must be a finite non-negative Decimal")
        if self.minimum_commission.amount < 0:
            raise DomainValidationError("minimum_commission cannot be negative")


class FeeCalculator:
    """Calculate one fill's fees, retaining every component independently."""

    def __init__(self, policy: FeePolicy) -> None:
        if type(policy) is not FeePolicy:
            raise DomainValidationError("policy must be FeePolicy")
        self._policy = policy

    @property
    def policy_version(self) -> FeePolicyVersion:
        return self._policy.version

    def calculate(
        self,
        *,
        side: OrderSide,
        price: Price,
        quantity: Quantity,
        trade_date: date,
    ) -> FeeBreakdown:
        """Return cent-rounded fees for a positive gross transaction amount.

        Each component is rounded independently with ``ROUND_HALF_UP``.  This
        is important for auditability: rounding only the combined total can
        shift cents between commission, tax, and transfer fee.
        """

        self._validate_request(side, price, quantity, trade_date)
        gross_amount = price.amount * Decimal(quantity.value)
        currency = price.currency

        raw_commission = gross_amount * self._policy.commission_rate
        commission = self._money(
            max(raw_commission, self._policy.minimum_commission.amount), currency
        )
        stamp_tax_rate = self._stamp_tax_rate(side, trade_date)
        stamp_tax = self._money(gross_amount * stamp_tax_rate, currency)
        transfer_fee_rate = self._transfer_fee_rate(self._policy.exchange, trade_date)
        transfer_fee = self._money(gross_amount * transfer_fee_rate, currency)

        return FeeBreakdown(
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            other=self._money(Decimal("0"), currency),
        )

    def _validate_request(
        self,
        side: OrderSide,
        price: Price,
        quantity: Quantity,
        trade_date: date,
    ) -> None:
        if type(side) is not OrderSide:
            raise DomainValidationError("side must be OrderSide")
        if type(price) is not Price:
            raise DomainValidationError("price must be Price")
        if type(quantity) is not Quantity or quantity.value <= 0:
            raise DomainValidationError("quantity must be a positive Quantity")
        if type(trade_date) is not date:
            raise DomainValidationError("trade_date must be a date")
        if price.currency != self._policy.minimum_commission.currency:
            raise CurrencyMismatchError("price and minimum_commission must use one currency")

    @staticmethod
    def _stamp_tax_rate(side: OrderSide, trade_date: date) -> Decimal:
        if side is OrderSide.BUY:
            return Decimal("0")
        if trade_date >= _STAMP_TAX_REDUCTION_START:
            return Decimal("0.0005")
        return Decimal("0.001")

    @staticmethod
    def _transfer_fee_rate(exchange: AshareExchange, trade_date: date) -> Decimal:
        if trade_date >= _UNIFIED_TRANSFER_FEE_START:
            return Decimal("0.00001")

        if exchange is AshareExchange.BEIJING:
            raise UnsupportedFeePolicyError(
                "Beijing Stock Exchange transfer fees before 2022-04-29 are "
                "not modeled by policy cn.a_share.cash_equity_fees.2015_present.v1"
            )
        if trade_date < _SH_SZ_TRANSFER_FEE_START:
            raise UnsupportedFeePolicyError(
                "Shanghai/Shenzhen transfer fees before 2015-08-01 are not "
                "modeled by policy cn.a_share.cash_equity_fees.2015_present.v1"
            )
        return Decimal("0.00002")

    @staticmethod
    def _money(amount: Decimal, currency: str) -> Money:
        return Money(amount.quantize(_CENT, rounding=ROUND_HALF_UP), currency)
