from __future__ import annotations

import pytest
from pydantic import ValidationError

from ashare_lab.settings import AppSettings


def test_on_demand_daily_source_defaults_to_existing_choice_first_policy() -> None:
    assert AppSettings(_env_file=None).on_demand_daily_source == "choice_then_eastmoney"


def test_on_demand_data_refreshes_for_each_fresh_submission_by_default() -> None:
    assert AppSettings(_env_file=None).on_demand_refresh_each_submission is True


def test_on_demand_reference_source_is_server_owned_and_defaults_to_baostock() -> None:
    assert AppSettings(_env_file=None).on_demand_reference_source == "baostock"
    assert (
        AppSettings(
            _env_file=None,
            on_demand_reference_source="eastmoney_mx",
        ).on_demand_reference_source
        == "eastmoney_mx"
    )
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, on_demand_reference_source="browser_selected")


def test_on_demand_daily_source_accepts_only_server_owned_policies() -> None:
    assert (
        AppSettings(
            _env_file=None, on_demand_daily_source="baostock_stock_only"
        ).on_demand_daily_source
        == "baostock_stock_only"
    )
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, on_demand_daily_source="browser_declared_provider")
