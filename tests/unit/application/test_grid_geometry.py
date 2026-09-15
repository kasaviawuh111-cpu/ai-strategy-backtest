from decimal import Decimal as D

import pytest

from ashare_lab.domain.strategy.price_plans import GridParameters, GridSpecificationError
from ashare_lab.application.minute_grid_plan import compile_minute_grid
from tests.unit.application.test_grid_strategy import run


def parameters(**updates):
    return GridParameters(anchor_price=85, lower_price=1, upper_price=1000,
                          spacing_mode="anchor_percent", spacing=D("0.5"), **updates)


def test_explicit_per_side_five_percent_ten_cells_compiles_instead_of_being_metadata():
    p = parameters(range_percent=5, levels_below=10, levels_above=10)
    resolved = p.resolve_geometry()
    assert (resolved.lower_price, resolved.upper_price) == (D("80.75"), D("89.25"))
    compiled = compile_minute_grid(p, first_open=D(90))
    assert len([c for c in compiled.cells if c.first_side == "buy"]) == 10
    assert len([c for c in compiled.cells if c.first_side == "sell"]) == 10
    daily = run([("85", "84"), ("84", "86")],
                anchor_price=85, lower_price=1, upper_price=1000,
                spacing_mode="anchor_percent", spacing=D("0.5"),
                range_percent=5, levels_below=10, levels_above=10)
    assert daily["parameters"]["lower_price"] == "80.75"
    assert daily["parameters"]["upper_price"] == "89.25"


def test_levels_use_directional_units_without_overwriting_them():
    p = parameters(levels_below=3, levels_above=2, buy_spacing_mode="cny",
                   buy_spacing=1, sell_spacing_mode="anchor_percent", sell_spacing=1)
    resolved = p.resolve_geometry()
    assert (resolved.lower_price, resolved.upper_price) == (D(82), D("86.70"))
    assert resolved.spacing_for("buy") == ("cny", 1)
    assert resolved.spacing_for("sell") == ("anchor_percent", 1)


def test_conflicting_range_and_cell_count_are_not_silently_ignored():
    with pytest.raises(GridSpecificationError, match="不一致"):
        parameters(range_percent=5, levels_below=20).resolve_geometry()
