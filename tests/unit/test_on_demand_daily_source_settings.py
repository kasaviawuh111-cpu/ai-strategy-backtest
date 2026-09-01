from __future__ import annotations

import pytest
from pydantic import ValidationError

from ashare_lab.settings import AppSettings


def test_on_demand_daily_source_defaults_to_existing_choice_first_policy() -> None:
    assert AppSettings(_env_file=None).on_demand_daily_source == "choice_then_eastmoney"


def test_on_demand_daily_source_accepts_only_server_owned_policies() -> None:
    assert (
        AppSettings(
            _env_file=None, on_demand_daily_source="baostock_stock_only"
        ).on_demand_daily_source
        == "baostock_stock_only"
    )
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, on_demand_daily_source="browser_declared_provider")
