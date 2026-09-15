from datetime import date
from decimal import Decimal as D
import pytest

from ashare_lab.application.conditional_orders import run_conditional_backtest
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.grid_strategy import run_grid_backtest
from ashare_lab.application.minute_price_rebase import MinutePriceRebase
from ashare_lab.domain.market_data import CorporateActionKind
from tests.unit.application.test_grid_strategy import parameters
from tests.unit.application.test_conditional_orders import params
from tests.unit.application.test_skill_backtest import _row, _history
from tests.unit.portfolio.test_corporate_actions import _action, _clock, INSTRUMENT

DAYS = tuple(date(2025, 1, n) for n in (2, 3, 6, 7, 8))
CALENDAR = (date(2024, 12, 31), *DAYS)


def replay(kind, *, split=False, rules=None, **options):
    price = '5' if split else '9.5'
    history = _history(tuple(_row(day, raw_open='10' if day <= DAYS[0] else price)
                             for day in CALENDAR))
    common = dict(opening_shares=100, slippage_bps=0, commission_rate=0, minimum_commission_cny=0)
    arguments = dict(history=history, start=DAYS[0], end=DAYS[3], market_sessions=CALENDAR, **options)
    if kind == 'grid':
        return run_grid_backtest(params=parameters(spacing=D('.1'), **common), **arguments)
    return run_conditional_backtest(params=params(rules or [dict(kind='price', side='buy',
        direction='down', target_price=D('9.75'), quantity=100)], **common), **arguments)


def corporate(split=False):
    action = (_action(CorporateActionKind.SHARE_DISTRIBUTION, multiplier=D(2),
                      credit_date=DAYS[2], sellable_date=DAYS[3]) if split else
              _action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=D('.5'), pay_date=DAYS[2]))
    rebase = MinutePriceRebase(INSTRUMENT, DAYS[1], D('.5') if split else D('.95'),
                              _clock(DAYS[1], 9), 'a' * 64)
    return dict(corporate_actions=TimelineCorporateActionApplier((action,)), price_rebases=(rebase,))


@pytest.mark.parametrize('kind', ['grid', 'conditional'])
@pytest.mark.parametrize('split', [False, True])
def test_actions_keep_equity_and_rebase_thresholds_without_false_price_signals(kind, split):
    result = replay(kind, split=split, **corporate(split))
    assert not result['orders']
    assert all(p['equity_cny'] == D(1000000) for p in result['series'])
    assert all(p['benchmark_equity_cny'] == p['equity_cny'] for p in result['series'])
    ex_day, paid = result['series'][1:3]
    if split:
        assert ex_day['shares'] == 100 and ex_day['pending_share_delta'] == 100
        assert paid['shares'] == 200 and paid['pending_share_delta'] == 0
        lots = result['portfolio_ledger']['lots']
        assert sum(lot['principal_cny'] for lot in lots) == D(1000)
        assert any(lot['sellable_on'] == DAYS[3].isoformat() for lot in lots)
    else:
        assert ex_day['dividend_receivable_cny'] == 50
        assert paid['dividend_receivable_cny'] == 0
        assert paid['cash_cny'] == ex_day['cash_cny'] + 50
    assert result['corporate_action_policy']


def test_registered_cash_dividend_survives_ex_date_sale():
    result = replay('conditional', rules=[dict(kind='price', side='sell', direction='up',
        target_price=10, limit_price=10, quantity=100)], **corporate())
    sell, = result['orders']
    assert sell['date'] == DAYS[1].isoformat()
    assert sell['price'] == sell['limit_price'] == D('9.5')
    assert result['series'][1]['shares'] == 0
    assert result['series'][1]['dividend_receivable_cny'] == 50
    assert result['series'][2]['cash_cny'] == result['series'][1]['cash_cny'] + 50
    assert result['summary']['final_equity_cny'] == D(1000000) - sell['fees_cny']
    assert result['series'][-1]['benchmark_equity_cny'] == D(1000000)


def test_action_without_required_factor_is_not_silently_unadjusted():
    options = corporate()
    options['price_rebases'] = ()
    with pytest.raises(ValueError, match='price_rebase_missing'):
        replay('grid', **options)


def test_registered_bonus_survives_sale_and_is_not_sellable_before_source_date():
    result = replay('conditional', split=True, rules=[dict(kind='price', side='sell',
        direction='up', target_price=10, limit_price=10, quantity=100)], **corporate(True))
    filled = [order for order in result['orders'] if order['filled_quantity']]
    assert filled[0]['date'] == DAYS[1].isoformat()
    assert filled[0]['filled_quantity'] == 100
    ex_day = result['series'][1]
    assert ex_day['shares'] == 0 and ex_day['pending_share_delta'] == 100
    credited = result['series'][2]
    assert credited['shares'] == 100 and credited['pending_share_delta'] == 0
    assert not any(order['date'] == DAYS[2].isoformat() for order in filled)
    assert sum(lot['principal_cny'] for lot in result['portfolio_ledger']['lots']) == D(500)
    assert all(point['equity_cny'] >= D(1000000) - sum(
        order['fees_cny'] for order in filled) for point in result['series'])


def test_report_keeps_unpaid_cash_separate_from_stock_and_in_hold_benchmark():
    from types import SimpleNamespace
    from ashare_lab.application.price_plan_result import execute_price_plan
    from ashare_lab.domain.strategy.price_plans import GridPlan
    from ashare_lab.application.result_views import calculate_result_bundle_hash
    source = corporate()
    strategy = SimpleNamespace(
        trading_plan=GridPlan(parameters=parameters(spacing=D('.1'), opening_shares=100,
            slippage_bps=0, commission_rate=0, minimum_commission_cny=0)),
        backtest=SimpleNamespace(start=DAYS[0], end=DAYS[1], initial_cash_cny=1000000))
    history = _history(tuple(_row(day, raw_open='10' if day <= DAYS[0] else '9.5')
                             for day in CALENDAR))
    bundle = execute_price_plan('cash-action-report', strategy, history,
        market_sessions=CALENDAR, corporate=SimpleNamespace(
            applier=source['corporate_actions'], price_rebases=source['price_rebases'],
            evidence={'source': 'deterministic_test_fixture'}))
    assert bundle.audit.open_dividend_receivable_cny == 50
    assert bundle.audit.open_position_notional_cny == 950
    assert bundle.summary.final_equity_cny == 1000000
    assert bundle.summary.benchmark_return == 0
    assert bundle.audit.result_hash == calculate_result_bundle_hash(
        bundle.model_dump(mode='json', by_alias=True))
