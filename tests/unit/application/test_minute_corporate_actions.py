from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.fixed_grid_orders import FixedGridOrders, GridCell
from ashare_lab.application.minute_grid_replay import Protection, replay_grid
from ashare_lab.application.minute_price_rebase import MinutePriceRebase
from ashare_lab.application.scheduled_execution import ScheduledOrder
from ashare_lab.domain.market_data import CorporateActionKind
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import Money
from tests.unit.application.test_minute_grid_replay import bar, FEES, SEC
from tests.unit.portfolio.test_corporate_actions import _action

ZONE = ZoneInfo("Asia/Shanghai")


def clock(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


def dividend():
    return replace(_action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=D(".5"), pay_date=date(2025, 1, 6)),
                   record_date=date(2026, 9, 9), ex_date=date(2026, 9, 10), cash_pay_date=date(2026, 9, 11))


def test_imported_inventory_receives_dividend_without_invented_acquisition_cost():
    from ashare_lab.domain.portfolio import PositionLot
    from ashare_lab.domain.shared import FillId, Quantity
    lot = PositionLot(FillId("opening-import:declared-position"), SEC,
                      clock(8, 15), date(2026, 9, 8), date(2026, 9, 9),
                      Quantity(100), Money(D(1000)), None)
    bars = [bar(9, 31, 10), replace(bar(9, 32, 10), ended_at=clock(9, 15)),
            bar(10, 31, "9.5"), replace(bar(10, 32, "9.5"), ended_at=clock(10, 15)),
            bar(11, 31, "9.5"), replace(bar(11, 32, "9.5"), ended_at=clock(11, 15))]
    result = replay_grid(FixedGridOrders([]), bars,
        PortfolioState(Money(D(0)), lots=(lot,)), FEES, initial_equity_cny=D(1000),
        corporate_actions=TimelineCorporateActionApplier((dividend(),)),
        price_rebases=(MinutePriceRebase(SEC, date(2026, 9, 10), D(".95"), clock(10, 9), "a" * 64),))
    assert not result.portfolio.fills
    assert result.portfolio.lots[0].acquisition_principal is None
    assert result.portfolio.cash.amount == 50
    assert all(point.equity == D(1000) for point in result.equity)


def test_cash_dividend_timeline_rebases_before_observation_and_settles_after_close():
    policy = FixedGridOrders([GridCell("a", D("9.8"), D(11), 100)])
    bars = [bar(9, 31, 10), replace(bar(9, 32, 10), ended_at=clock(9, 15)),
            bar(10, 31, "9.5"), replace(bar(10, 32, "9.5"), ended_at=clock(10, 15)),
            bar(11, 31, "9.5"), replace(bar(11, 32, "9.5"), ended_at=clock(11, 15))]
    rebase = MinutePriceRebase(SEC, date(2026, 9, 10), D(".95"), clock(10, 9), "a" * 64)
    result = replay_grid(policy, bars, PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=(ScheduledOrder("seed", date(2026, 9, 9), clock(9, 9), quantity=100),),
                         protection=Protection(stop_loss=D(".03")),
                         corporate_actions=TimelineCorporateActionApplier((dividend(),)), price_rebases=(rebase,))
    assert len(result.portfolio.fills) == 1
    assert policy.cells["a"].buy_price == D("9.31")
    assert result.portfolio.lots[0].acquisition_principal.amount == D(950)
    assert [e.phase.value for e in result.portfolio.corporate_action_entries] == ["entitlement", "accrual", "settlement"]
    assert result.equity[2].dividend_receivable == 50
    assert result.equity[4].cash == result.equity[1].cash
    assert result.equity[5].cash == result.equity[4].cash + 50
    assert result.equity[5].dividend_receivable == 0
    assert all(point.equity == result.equity[0].equity for point in result.equity)
    assert all(point.benchmark_equity == point.equity for point in result.equity)
    assert result.benchmark_portfolio.cash == result.portfolio.cash


def test_rebase_requires_a_source_clock_and_preserves_pending_order_identity():
    with pytest.raises(ValueError, match="not known"):
        MinutePriceRebase(SEC, date(2026, 9, 10), D(".95"), clock(10, 10), "a" * 64)
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100)])
    ticket, = policy.observe(bar(9, 31, 9).prices, 0)
    rebase = MinutePriceRebase(SEC, date(2026, 9, 10), D(".95"), clock(10, 9), "a" * 64)
    policy.rebase_prices(rebase)
    updated = policy.pending["a"]
    assert updated.order_id == ticket.order_id and updated.quantity == ticket.quantity
    assert updated.signal_bar == 0 and updated.effective_bar == 1
    assert updated.limit_price == D("8.55")


def test_suspended_record_ex_and_payment_days_advance_both_account_ledgers():
    from ashare_lab.domain.market_data import DailyBar, TradingStatus
    from ashare_lab.domain.shared import Price, Quantity
    action = replace(dividend(), record_date=date(2026, 9, 10),
                     ex_date=date(2026, 9, 11), cash_pay_date=date(2026, 9, 14))
    closes = []
    for day, value in ((10, "10"), (11, "9.5"), (14, "9.5")):
        session = replace(bar(day, 31, value).session, status=TradingStatus.SUSPENDED)
        daily = DailyBar(SEC, session.session_date, *(Price(D(value)) for _ in range(4)),
                         Quantity(0), D(0), clock(day, 15))
        closes.append((session, daily))
    policy = FixedGridOrders([GridCell("a", D("9.8"), D(11), 100)])
    result = replay_grid(policy,
        [bar(9, 31, 10), replace(bar(9, 32, 10), ended_at=clock(9, 15))],
        PortfolioState(Money(D(1000000))), FEES,
        scheduled_orders=(ScheduledOrder("seed", date(2026, 9, 9), clock(9, 9), quantity=100),),
        corporate_actions=TimelineCorporateActionApplier((action,)),
        price_rebases=(MinutePriceRebase(SEC, action.ex_date, D(".95"), clock(11, 9), "a" * 64),),
        nontrading_closes=tuple(closes))
    assert len(result.portfolio.fills) == 1
    assert [e.phase.value for e in result.portfolio.corporate_action_entries] == ["entitlement", "accrual", "settlement"]
    assert result.equity[-3].dividend_receivable == 0
    assert result.equity[-2].dividend_receivable == 50
    assert result.equity[-1].dividend_receivable == 0
    assert result.equity[-1].cash == result.equity[-3].cash + 50
    assert result.equity[-1].observed_at == clock(14, 15)
    assert result.portfolio.lots[0].acquisition_principal.amount == 950
    assert policy.cells["a"].buy_price == D("9.31")
    assert all(p.equity == result.equity[0].equity == p.benchmark_equity for p in result.equity)
    assert result.benchmark_portfolio.cash == result.portfolio.cash


def test_holding_benchmark_uses_its_own_entitlement_not_strategy_added_inventory():
    from ashare_lab.application.minute_result import minute_result_bundle
    action = replace(dividend(), record_date=date(2026, 9, 10), ex_date=date(2026, 9, 11))
    bars = [bar(9, 31, 10), replace(bar(9, 32, 10), ended_at=clock(9, 15)),
            bar(10, 31, 10), replace(bar(10, 32, 10), ended_at=clock(10, 15)),
            bar(11, 31, "9.5"), replace(bar(11, 32, "9.5"), ended_at=clock(11, 15))]
    rebase = MinutePriceRebase(SEC, date(2026, 9, 11), D(".95"), clock(11, 9), "a" * 64)
    result = replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=tuple(ScheduledOrder(f"buy:{day}", date(2026, 9, day), clock(9, 9), quantity=100)
                                                for day in (9, 10)),
                         corporate_actions=TimelineCorporateActionApplier((action,)), price_rebases=(rebase,))
    assert result.portfolio.corporate_action_entitlements[0].cash_amount.amount == 100
    assert result.benchmark_portfolio.corporate_action_entitlements[0].cash_amount.amount == 50
    assert result.benchmark_portfolio.position_quantity(SEC).value == 100
    assert result.portfolio.position_quantity(SEC).value == 200
    bundle = minute_result_bundle(run_id="dividend-benchmark", result=result,
                                  initial_cash=D(1000000), snapshot_id="fixture:minute")
    assert bundle.summary.benchmark_return == float(result.equity[-1].benchmark_equity / D(1000000) - 1)


def test_missing_rebase_is_not_inferred_from_a_price_gap():
    with pytest.raises(ValueError, match="requires source price rebase"):
        replay_grid(FixedGridOrders([]), [bar(9, 31, 10), bar(10, 31, 5)],
                    PortfolioState(Money(D(1000000))), FEES,
                    corporate_actions=TimelineCorporateActionApplier((dividend(),)))


def test_split_keeps_total_principal_and_rebases_protection_before_first_ex_bar():
    action = replace(_action(CorporateActionKind.STOCK_SPLIT, multiplier=D(2),
                             credit_date=date(2025, 1, 3), sellable_date=date(2025, 1, 3)),
                     record_date=date(2026, 9, 9), ex_date=date(2026, 9, 10),
                     share_credit_date=date(2026, 9, 10), share_sellable_date=date(2026, 9, 10))
    bars = [bar(9, 31, 10), replace(bar(9, 32, 10), ended_at=clock(9, 15)),
            bar(10, 31, 5), replace(bar(10, 32, 5), ended_at=clock(10, 15))]
    result = replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=(ScheduledOrder("seed", date(2026, 9, 9), clock(9, 9), quantity=100),),
                         protection=Protection(stop_loss=D(".03")),
                         corporate_actions=TimelineCorporateActionApplier((action,)),
                         price_rebases=(MinutePriceRebase(SEC, date(2026, 9, 10), D(".5"), clock(10, 9), "a" * 64),))
    assert result.portfolio.position_quantity(SEC).value == 200
    assert result.portfolio.lots[0].acquisition_principal.amount == 1000
    assert result.portfolio.lots[0].acquired_on == date(2026, 9, 9)
    assert len(result.portfolio.fills) == 1
    assert result.unfinished_exit_quantity == 0
    assert result.equity[-1].equity == result.equity[0].equity == result.equity[-1].benchmark_equity


@pytest.mark.parametrize("sold,expected", [(100, 0), (200, 1)])
def test_closed_cycles_follow_split_adjusted_inventory_not_net_traded_shares(sold, expected):
    from ashare_lab.domain.orders import OrderSide
    from ashare_lab.domain.shared import Price
    from ashare_lab.application.minute_result import minute_result_bundle
    action = replace(_action(CorporateActionKind.STOCK_SPLIT, multiplier=D(2),
                             credit_date=date(2025, 1, 3), sellable_date=date(2025, 1, 3)),
                     record_date=date(2026, 9, 9), ex_date=date(2026, 9, 10),
                     share_credit_date=date(2026, 9, 10), share_sellable_date=date(2026, 9, 10))
    bars = [bar(9, 31, 10), replace(bar(9, 32, 10), ended_at=clock(9, 15)),
            bar(10, 31, 5), replace(bar(10, 32, 5), ended_at=clock(10, 15))]
    bars = [replace(b, session=replace(b.session, lower_limit=Price(D(4)), upper_limit=Price(D(6))))
            if b.session.session_date == date(2026, 9, 10) else b for b in bars]
    result = replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=(ScheduledOrder("seed", date(2026, 9, 9), clock(9, 9), quantity=100),
                                           ScheduledOrder("exit", date(2026, 9, 10), clock(9, 9),
                                                          side=OrderSide.SELL, quantity=sold)),
                         corporate_actions=TimelineCorporateActionApplier((action,)),
                         price_rebases=(MinutePriceRebase(SEC, date(2026, 9, 10), D(".5"), clock(10, 9), "a" * 64),))
    assert result.portfolio.position_quantity(SEC).value == 200 - sold
    assert result.closed_position_cycles == expected
    bundle = minute_result_bundle(run_id="split-exit", result=result, initial_cash=D(1000000), snapshot_id="fixture")
    assert bundle.summary.trade_count == expected
