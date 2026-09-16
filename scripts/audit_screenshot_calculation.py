"""Read-only independent arithmetic audit of the user's eight-fill screenshot."""
import json
import sqlite3
from decimal import Decimal as D, ROUND_UP, ROUND_DOWN, ROUND_HALF_UP

RUN = 'run:22305d94eefe48728a9c39b2831d7033'


def money(value):
    return value.quantize(D('.01'), rounding=ROUND_HALF_UP)


def fees(price, quantity, buy):
    gross = price * quantity
    return dict(commission=money(max(D(5), gross * D('.00025'))),
                stamp_tax=money(D(0) if buy else gross * D('.0005')),
                transfer_fee=money(gross * D('.00001')), other=D(0))


def main():
    db = sqlite3.connect('file:var/ashare.db?mode=ro', uri=True)
    result = json.loads(db.execute('select result_json from backtest_runs where run_id=?', (RUN,)).fetchone()[0])
    ledger = json.loads(result['audit']['pricePlanLedger'])
    history = {r['session_date']: r for r in ledger['sourceEvidence']['history']}
    fills = ledger['portfolio']['fills']
    cash, quantity, fee_total = D(1000000), 0, D(0)
    rows, cycles = [], []
    cycle_start = cash
    for fill in fills:
        date = fill['filled_at'][:10]
        bar = history[date]
        buy = fill['side'] == 'buy'
        price, shares = D(fill['price']['amount']), fill['quantity']['value']
        expected = (D(bar['raw_open']) * (D('1.0005') if buy else D('.9995'))).quantize(
            D('.01'), rounding=ROUND_UP if buy else ROUND_DOWN)
        expected = min(D(bar['raw_high']), max(D(bar['raw_low']), expected))
        assert price == expected
        charges = fees(price, shares, buy)
        assert charges == {k: D(v['amount']) for k, v in fill['fees'].items()}
        order = next(a for a in result['activities'] if a['kind'] == 'order' and a['orderId'] == fill['order_id']['value'])
        limit = D(bar['upper_limit'] if buy else bar['lower_limit'])
        assert D(str(order['price'])) == limit
        if buy:
            if quantity == 0:
                cycle_start = cash
            affordable = int(cash / limit) // 100 * 100
            while affordable * limit + sum(fees(limit, affordable, True).values()) > cash:
                affordable -= 100
            assert shares == affordable
        else:
            assert shares == quantity
        cost = sum(charges.values())
        cash += (-price * shares if buy else price * shares) - cost
        quantity += shares if buy else -shares
        fee_total += cost
        assert cash >= 0 and quantity >= 0
        if quantity == 0:
            cycles.append(cash - cycle_start)
        rows.append(dict(date=date, side=fill['side'], open=bar['raw_open'], limit=limit,
                         fill=price, shares=shares, fees=cost, cashAfter=cash))
    assert quantity == 0 and cash == D(str(result['summary']['finalEquityCny']))
    # No entitlement was held by the strategy; its cash ledger needs no dividend additions.
    assert all(D(a['cash_after']['amount']) == D(a['cash_before']['amount']) for a in ledger['portfolio']['corporate_action_entries'])
    daily_cash, daily_quantity, peak, worst = D(1000000), 0, D(1000000), D(0)
    for point in result['series']:
        date = point['date']
        for row in (r for r in rows if r['date'] == date):
            buy = row['side'] == 'buy'
            daily_cash += (-1 if buy else 1) * row['fill'] * row['shares'] - row['fees']
            daily_quantity += row['shares'] if buy else -row['shares']
        equity = daily_cash + daily_quantity * D(history[date]['raw_close'])
        assert abs(equity / D(10000) - D(str(point['equity']))) < D('.00000001')
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
    assert abs(worst - D(str(result['summary']['maxDrawdown']))) < D('.000000000001')
    # Baseline buys at the period's first open, retains cash, and receives the recorded dividend.
    first = history['2025-09-11']
    base_limit = D(first['upper_limit'])
    base_qty = int(D(1000000) / base_limit) // 100 * 100
    while base_qty * base_limit + sum(fees(base_limit, base_qty, True).values()) > D(1000000):
        base_qty -= 100
    base_price = (D(first['raw_open']) * D('1.0005')).quantize(D('.01'), rounding=ROUND_UP)
    base_cash = D(1000000) - base_qty * base_price - sum(fees(base_price, base_qty, True).values())
    actions = json.loads(ledger['sourceEvidence']['signalAdjustmentSource'])['actions']
    dividends = sum(D(a['gross_cash_per_share']) * base_qty for a in actions
                    if '2025-09-11' <= a['cash_pay_date'] <= '2026-09-11')
    base_end = base_cash + base_qty * D(history['2026-09-11']['raw_close']) + dividends
    base_return = base_end / D(1000000) - 1
    assert abs(base_return - D(str(result['summary']['benchmarkReturn']))) < D('.000000000001')
    win = D(sum(x > 0 for x in cycles)) / len(cycles)
    assert len(cycles) == result['summary']['tradeCount']
    assert abs(win - D(str(result['summary']['winRate']))) < D('.000000000001')
    ret = cash / D(1000000) - 1
    print(json.dumps(dict(runId=RUN, instrument='300059.SZ', rows=rows, totalFees=fee_total,
        finalCash=cash, returnPct=ret*100, maxDrawdownPct=worst*100,
        closedCyclePnl=cycles, winRatePct=win*100, baselineShares=base_qty,
        baselineBuy=base_price, baselineDividend=dividends, baselineFinal=base_end,
        baselineReturnPct=base_return*100, excessPercentagePoints=(ret-base_return)*100,
        verifiedDailyPoints=len(result['series']), scope='Arithmetic against persisted source history; not independent market-provider verification.'),
        ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
