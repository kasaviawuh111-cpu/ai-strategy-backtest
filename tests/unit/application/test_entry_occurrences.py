import pytest

from ashare_lab.application.entry_occurrences import entry_occurrences
from tests.unit.application.test_daily_backtest import _provider_fact, bars, strategy


@pytest.mark.parametrize("indicator,trigger", [
    ("technical.ma", "price_above"), ("technical.rsi", "below"),
    ("volume.relative", "gte_multiple"), ("provider.numeric", "below"),
    ("price.return_pct", "above"), ("price.rolling_high", "new_high"),
])
def test_state_episodes_do_not_rearm_on_missing_data_and_use_warmup(indicator, trigger):
    condition = strategy().entry.model_copy(update={"indicator_id": indicator, "trigger": trigger})
    source = bars(("10",) * 8)
    values = [None, False, True, True, None, True, False, True]
    facts = tuple(
        None if value is None else _provider_fact(bar, condition_ref="state", triggered=value)
        for bar, value in zip(source, values, strict=True)
    )
    projected = entry_occurrences(facts, condition=condition)
    assert [i for i, fact in enumerate(projected) if fact and fact.triggered] == [2, 7]
    for end in range(1, len(facts) + 1):
        assert entry_occurrences(facts[:end], condition=condition) == projected[:end]
    # Filtering the trading range happens after projection: warm-up true stays active.
    assert not any(fact and fact.triggered for fact in projected[3:7])


def test_independent_cross_pulses_are_preserved():
    source = bars(("10",) * 4)
    facts = tuple(_provider_fact(bar, condition_ref="cross", triggered=i in (1, 3))
                  for i, bar in enumerate(source))
    assert entry_occurrences(facts, condition=strategy().entry) == facts


def test_single_policy_missing_field_keeps_saved_strategy_semantics():
    from ashare_lab.domain.strategy import StrategySpec
    original = strategy()
    payload = original.model_dump()
    payload["execution"].pop("position_policy")
    assert StrategySpec.model_validate(payload) == original
