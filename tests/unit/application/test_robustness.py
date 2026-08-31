from dataclasses import replace
from decimal import Decimal

from ashare_lab.application.daily_backtest import DailyBacktestConfig, DailyBacktestInput
from ashare_lab.application.robustness import run_execution_robustness
from ashare_lab.domain.execution import TradingCalendar

from .test_daily_backtest import bars, fees, sessions, strategy


def test_execution_stress_scenarios_are_fixed_and_do_not_select_a_winner() -> None:
    source_bars = bars()
    report = run_execution_robustness(
        DailyBacktestInput(
            run_key="stress-test",
            strategy=strategy(),
            bars=source_bars,
            sessions=sessions(source_bars),
            calendar=TradingCalendar(
                version="test",
                sessions=tuple(item.session_date for item in source_bars),
            ),
            fee_calculator=fees(),
        )
    )

    payload = report.as_dict()
    assert [item.scenario_id for item in report.scenarios] == [
        "base",
        "slippage_1_5x",
        "slippage_2x",
        "participation_2_5pct",
        "participation_10pct",
    ]
    hashes = [item.config_hash for item in report.scenarios]
    assert len(hashes) == len(set(hashes))
    assert all(item.startswith("sha256:") for item in hashes)
    assert payload["selectionPolicy"] == "predeclared_scenarios_no_optimization"
    assert "winner" not in payload


def test_execution_stress_scenarios_drop_any_duplicate_configs() -> None:
    source_bars = bars()
    request = DailyBacktestInput(
        run_key="stress-dedup-test",
        strategy=strategy(),
        bars=source_bars,
        sessions=sessions(source_bars),
        calendar=TradingCalendar(
            version="test",
            sessions=tuple(item.session_date for item in source_bars),
        ),
        fee_calculator=fees(),
        config=DailyBacktestConfig(
            participation_rate=Decimal("0.025"),
            slippage_bps=Decimal("0"),
        ),
    )

    report = run_execution_robustness(request)

    assert [item.scenario_id for item in report.scenarios] == [
        "base",
        "participation_5pct",
        "participation_10pct",
    ]
    assert len({item.config_hash for item in report.scenarios}) == len(report.scenarios)
    assert (
        report.scenarios[0].config_hash
        != run_execution_robustness(
            replace(request, config=replace(request.config, allocation_ratio=Decimal("0.5")))
        )
        .scenarios[0]
        .config_hash
    )
