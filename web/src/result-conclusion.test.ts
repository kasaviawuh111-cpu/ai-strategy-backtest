import { describe, expect, it } from 'vitest'

import { numericResultConclusion } from './result-conclusion'
import type { BacktestMetrics } from './types'

const metrics = (changes: Partial<BacktestMetrics>): BacktestMetrics => ({
  total: -2.54,
  bench: -39.23,
  excess: 36.69,
  benchmarkComparisonStatus: 'comparable',
  mdd: -58.19,
  trips: 37,
  win: null,
  sharpe: null,
  ann: null,
  initialCashCny: null,
  finalEquityCny: 0,
  interpretation: '',
  dataRange: { start: '2021-08-06', end: '2026-08-06', sessions: 0 },
  warnings: [],
  openShares: null,
  ...changes,
})

describe('numericResultConclusion', () => {
  it('states both losses and the exact number of percentage points saved', () => {
    expect(numericResultConclusion(metrics({}))).toBe(
      '策略亏损 2.54%，同样的钱买入后一直持有亏损 39.23%，相对少亏 36.69 个百分点。',
    )
  })

  it.each([
    [{ total: 12, bench: 8, excess: 4 }, '策略盈利 12.00%，同样的钱买入后一直持有盈利 8.00%，相对领先 4.00 个百分点。'],
    [{ total: 5, bench: 8, excess: -3 }, '策略盈利 5.00%，同样的钱买入后一直持有盈利 8.00%，相对落后 3.00 个百分点。'],
    [{ total: -10, bench: -3, excess: -7 }, '策略亏损 10.00%，同样的钱买入后一直持有亏损 3.00%，相对多亏 7.00 个百分点。'],
  ])('keeps comparisons numeric for %#', (changes, expected) => {
    expect(numericResultConclusion(metrics(changes))).toBe(expected)
  })

  it('does not infer a missing benchmark and makes zero completed trades explicit', () => {
    expect(numericResultConclusion(metrics({
      total: 0,
      bench: null,
      excess: null,
      benchmarkComparisonStatus: 'benchmark_unavailable',
      trips: 0,
    }))).toBe(
      '策略收益 0.00%，没有可审计的买入持有基准，无法比较超额收益；完整买卖 0 回合。',
    )
  })

  it('does not call an all-cash strategy outperformance when no buy filled', () => {
    expect(numericResultConclusion(metrics({
      total: 0,
      bench: 8.7,
      excess: null,
      trips: 0,
      benchmarkComparisonStatus: 'strategy_entry_not_filled',
    }))).toBe(
      '策略收益 0.00%，本次没有已成交买入，无法比较超额收益（同样的钱买入后一直持有盈利 8.70%，仅作参考）；完整买卖 0 回合。',
    )
  })
})
