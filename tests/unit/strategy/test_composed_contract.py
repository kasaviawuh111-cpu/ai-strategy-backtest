"""Composition schema checks, not runtime capability or public acceptance."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ashare_lab.domain.strategy import ComposedExecutionPolicy, StrategySpec
from ashare_lab.ports.idea_routing import UnboundIdeaStrategy


def test_independent_plans_survive_stock_binding_and_json_roundtrip():
    from ashare_lab.application.composed_execution import validate_composed_route
    payload = json.loads((Path(__file__).resolve().parents[3] /
        'contracts/examples/strategy.macd-volume.daily.v1.json').read_text())
    cash = payload['backtest']['initial_cash_cny']
    payload.update(entry=None, exit=None, execution=ComposedExecutionPolicy().model_dump(),
        independent_plans={key: dict(kind='scheduled', parameters=dict(
            initial_cash_cny=cash, frequency='monthly', day=day, side=side,
            sizing_mode='shares', quantity=100))
            for key, side, day in [('entry_plan', 'buy', 1), ('exit_plan', 'sell', 15)]})
    bound = StrategySpec.model_validate(payload)
    unbound = UnboundIdeaStrategy.model_validate(bound.model_dump(exclude={'schema_version', 'instrument'}))
    assert unbound.bind(bound.instrument.symbol) == bound
    assert StrategySpec.model_validate_json(bound.model_dump_json()) == bound
    validate_composed_route(bound)
    from ashare_lab.application.skill_backtest_service import SkillBacktestService
    service = object.__new__(SkillBacktestService)
    service.minute_grid = None
    service.signal_corporate_loader = None
    with pytest.raises(ValueError, match='composed_execution_inputs_not_connected'):
        service.validate_execution_capability(bound)
    payload['entry'] = json.loads((Path(__file__).resolve().parents[3] /
        'contracts/examples/strategy.macd-volume.daily.v1.json').read_text())['entry']
    with pytest.raises(ValidationError, match='所有权'):
        StrategySpec.model_validate(payload)


@pytest.mark.parametrize('signal_side', ['entry', 'exit'])
def test_daily_grid_signal_composition_passes_route_without_rewriting_clock(signal_side):
    from ashare_lab.application.composed_execution import validate_composed_route
    payload = json.loads((Path(__file__).resolve().parents[3] /
        'contracts/examples/strategy.macd-volume.daily.v1.json').read_text())
    original_signal = getattr(StrategySpec.model_validate(payload), signal_side)
    payload['entry' if signal_side == 'exit' else 'exit'] = None
    payload['trading_plan'] = dict(kind='grid', parameters=dict(anchor_price=10,
        lower_price=5, upper_price=15, observation='daily_close',
        initial_cash_cny=payload['backtest']['initial_cash_cny']))
    payload['execution'] = ComposedExecutionPolicy().model_dump()
    strategy = StrategySpec.model_validate(payload)
    validate_composed_route(strategy)
    assert strategy.trading_plan.parameters.observation == 'daily_close'
    assert getattr(strategy, signal_side) == original_signal


@pytest.mark.parametrize("kind,parameters", [
    ("scheduled", {"frequency": "monthly", "day": 1}),
    ("conditional", {"observation": "minute_bar", "rules": [
        {"kind": "price", "side": "buy", "target_price": 10}]}),
])
def test_independent_minute_protection_routes_without_changing_its_anchor(kind, parameters):
    from decimal import Decimal
    from ashare_lab.application.composed_execution import validate_composed_route, composed_protection
    payload = json.loads((Path(__file__).resolve().parents[3] /
        "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    payload.update(entry=None, exit={"op": "first_of", "children": [{
        "type": "minute_protection_exit", "take_profit_pct": 5, "stop_loss_pct": 3}]},
        trading_plan={"kind": kind, "parameters": {**parameters,
            "initial_cash_cny": payload["backtest"]["initial_cash_cny"]}},
        execution=ComposedExecutionPolicy().model_dump())
    parsed = StrategySpec.model_validate(payload)
    validate_composed_route(parsed)
    protection = composed_protection(parsed)
    assert protection.take_profit == Decimal('.05')
    assert protection.stop_loss == Decimal('.03')
    assert parsed.exit.children[0].anchor == "fee_exclusive_weighted_acquisition_cost"


@pytest.mark.parametrize("kind,parameters", [
    ("scheduled", {"frequency": "monthly", "day": 1}),
    ("conditional", {"observation": "minute_bar", "rules": [
        {"kind": "price", "side": "buy", "target_price": 10}]}),
    ("grid", {"anchor_price": 10, "lower_price": 5, "upper_price": 15}),
])
@pytest.mark.parametrize("leg", ["entry", "exit", "both"])
def test_composed_leg_contract_roundtrip_requires_explicit_execution(kind, parameters, leg):
    payload = json.loads((Path(__file__).resolve().parents[3] /
        "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    payload["trading_plan"] = {"kind": kind, "parameters": {
        **parameters, "initial_cash_cny": payload["backtest"]["initial_cash_cny"]}}
    if leg == "entry":
        payload["exit"] = None
    elif leg == "exit":
        payload["entry"] = None
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(payload)
    payload["execution"] = ComposedExecutionPolicy().model_dump()
    parsed = StrategySpec.model_validate(payload)
    assert StrategySpec.model_validate_json(parsed.model_dump_json()) == parsed
    unbound = UnboundIdeaStrategy.model_validate(parsed.model_dump(exclude={"schema_version", "instrument"}))
    assert unbound.bind(parsed.instrument.symbol) == parsed
    assert parsed.entry is not None if leg != "exit" else parsed.entry is None
    assert parsed.exit is not None if leg != "entry" else parsed.exit is None
    # A schema-valid draft still cannot run without the composed data adapters.
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    from ashare_lab.application.skill_backtest_service import SkillBacktestService
    service = object.__new__(SkillBacktestService)
    service.minute_grid = None
    service.signal_corporate_loader = None
    with pytest.raises(MinuteGridCapabilityError, match="composed_"):
        service.validate_execution_capability(parsed)
    from ashare_lab.application.price_plan_result import execute_price_plan
    from ashare_lab.application.grid_strategy import GridSpecificationError
    # Missing composed evidence must fail before any legacy engine runs,
    # including callers bypassing the queue's capability preflight.
    with pytest.raises(GridSpecificationError, match="不能回退"):
        execute_price_plan("fixture", parsed, None, None)
