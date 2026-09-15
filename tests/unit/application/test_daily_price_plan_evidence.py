"""Daily plan report identities; deterministic fixtures, not live acceptance."""
import json
from dataclasses import replace
from datetime import date
from hashlib import sha256

import pytest

from ashare_lab.application.price_plan_result import execute_price_plan
from ashare_lab.application.result_views import calculate_result_bundle_hash
from ashare_lab.domain.strategy import canonical_hash
from ashare_lab.domain.strategy.price_plans import ConditionalPlan, GridPlan
from tests.unit.application.test_conditional_orders import params
from tests.unit.application.test_grid_strategy import DATES, parameters
from tests.unit.application.test_skill_backtest import _history, _row, _strategy

RUNTIME = dict(catalog_hash="sha256:" + "a" * 64,
               code_revision="fixture-unreleased", engine_version="fixture-engine")


@pytest.mark.parametrize("kind", ["grid", "buy_first", "sell_first"])
def test_daily_report_pins_inputs_without_changing_trades(kind):
    plan = GridPlan(parameters=parameters(observation="daily_close")) if kind == "grid" else (
        ConditionalPlan(parameters=params([
            dict(kind="price", side="sell" if kind == "sell_first" else "buy",
                 direction="up" if kind == "sell_first" else "down",
                 target_price=11 if kind == "sell_first" else 9, quantity=100),
        ], observation="daily_close", opening_shares=100 if kind == "sell_first" else 0)))
    strategy = _strategy(DATES).model_copy(update={"trading_plan": plan})
    history = _history((_row(date(2024, 12, 31), raw_open="10"), *(
        _row(day, raw_open=price, raw_close=price)
        for day, price in zip(DATES, ("10", "9", "11", "10"), strict=True))))
    def run(source=history, **extra):
        return execute_price_plan("daily-evidence", strategy, source, **extra)
    baseline = run()
    pinned = run(runtime_evidence=RUNTIME)
    evidence = pinned.summary.run_evidence
    assert baseline.summary.run_evidence is None
    assert evidence.code_revision == RUNTIME["code_revision"]
    assert evidence.strategy_hash == canonical_hash(strategy.model_dump(mode="json"))
    assert pinned.activities == baseline.activities
    assert pinned.series == baseline.series
    assert pinned.summary.total_return == baseline.summary.total_return
    ledger = json.loads(pinned.audit.price_plan_ledger)
    source = ledger["provenance"]["input_snapshot"]
    assert evidence.data_snapshot_checksum == "sha256:" + sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert pinned.audit.result_hash == calculate_result_bundle_hash(
        pinned.model_dump(mode="json", by_alias=True))
    assert run(runtime_evidence=RUNTIME).summary.run_evidence == evidence
    changed = replace(history, rows=(
        replace(history.rows[0], amount=history.rows[0].amount + 1), *history.rows[1:]))
    changed_report = run(changed, runtime_evidence=RUNTIME)
    calendar_report = run(runtime_evidence=RUNTIME,
                          calendar_evidence={"sourceSha256": "b" * 64})
    for report in (changed_report, calendar_report):
        assert report.summary.run_evidence.data_snapshot_checksum != evidence.data_snapshot_checksum
