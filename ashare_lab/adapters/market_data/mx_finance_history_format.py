"""Thin MX-format adapter; reuse the existing parsing and retry observer."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from ashare_lab.ports.finance_history_format import FinanceFieldMetadata, FinanceHistoryDecoder

from .mx_saas import (
    MxSaasProviderDataError,
    MxSaasProviderError,
    MxSaasProviderNoDataError,
    MxSaasProviderSqlError,
    MxSaasProviderUnavailableError,
    _explicit_entity_codes,  # pyright: ignore[reportPrivateUsage]
    _provider_field_metadata,  # pyright: ignore[reportPrivateUsage]
    _provider_session_date,  # pyright: ignore[reportPrivateUsage]
    _table_entity_code_values,  # pyright: ignore[reportPrivateUsage]
    notify_mx_data_retry,
)


class MxFinanceHistoryDecoder(FinanceHistoryDecoder):
    provider_errors: tuple[type[Exception], ...] = (MxSaasProviderError,)
    no_data_errors: tuple[type[Exception], ...] = (MxSaasProviderNoDataError,)
    unavailable_errors: tuple[type[Exception], ...] = (
        MxSaasProviderUnavailableError, MxSaasProviderSqlError,
    )
    invalid_data_errors: tuple[type[Exception], ...] = (MxSaasProviderDataError,)

    def entity_codes(self, table: Mapping[str, Any]) -> set[str]:
        return _explicit_entity_codes(table) | _table_entity_code_values(table)

    def field_metadata(self, table: Mapping[str, Any]) -> FinanceFieldMetadata:
        return _provider_field_metadata(table)

    def session_date(self, value: object) -> date:
        return _provider_session_date(value)

    def notify_data_retry(self, call_id: str, *, recovered: bool = False) -> None:
        notify_mx_data_retry(call_id, recovered=recovered)
