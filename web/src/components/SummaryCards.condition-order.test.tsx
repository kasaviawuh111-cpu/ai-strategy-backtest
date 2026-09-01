import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { StrategyRuleNode, StrategySummary } from '../types'
import { StrategyCard } from './SummaryCards'

const leaf = (id: string, label: string): StrategyRuleNode => ({
  kind: 'leaf',
  id,
  label,
  detail: `日线收盘确认：${label}`,
  tone: 'indicator',
  parameters: [],
})

describe('condition-order strategy card', () => {
  it('shows the Chinese comparison and threshold in the buy and sell rows', () => {
    const entryRule = leaf('entry', '当日涨幅 ≥ 5%')
    const exitRule = leaf('exit', '当日跌幅 ≥ 5%')
    const strategy: StrategySummary = {
      title: '当日涨跌幅规则',
      rows: [
        { key: 'entry', label: '买入', value: entryRule.label, kind: 'buy' },
        { key: 'exit', label: '卖出', value: exitRule.label, kind: 'sell' },
        { key: 'range', label: '区间', value: '近一年' },
      ],
      strategyHash: 'sha256:test',
      entryRule,
      exitRule,
      confirmation: '日线收盘确认',
      earliestExecution: '下一可交易日开盘',
      conditionCount: 2,
      eventFacts: [],
    }

    render(
      <StrategyCard
        instrument={{ name: '东方财富', code: '300059.SZ' }}
        strategy={strategy}
        onEditRow={vi.fn()}
        onOpenMore={vi.fn()}
        onRun={vi.fn()}
        canStart
      />,
    )

    expect(screen.getByText('当日涨幅 ≥ 5%')).toBeInTheDocument()
    expect(screen.getByText('当日跌幅 ≥ 5%')).toBeInTheDocument()
    expect(screen.queryByText(/at_least|at_most|\bat least\b|\bat most\b/i)).not.toBeInTheDocument()
  })

  it('keeps generic indicator thresholds visible in the strategy card', () => {
    const entryRule = leaf('entry-turnover', '换手率 高于 5%')
    const exitRule = leaf('exit-rsi', 'RSI 低于 30')
    const strategy: StrategySummary = {
      title: '指标阈值规则',
      rows: [
        { key: 'entry', label: '买入', value: entryRule.label, kind: 'buy' },
        { key: 'exit', label: '卖出', value: exitRule.label, kind: 'sell' },
        { key: 'range', label: '区间', value: '近一年' },
      ],
      strategyHash: 'sha256:thresholds',
      entryRule,
      exitRule,
      confirmation: '日线收盘确认',
      earliestExecution: '下一可交易日开盘',
      conditionCount: 2,
      eventFacts: [],
    }

    render(
      <StrategyCard
        instrument={{ name: '东方财富', code: '300059.SZ' }}
        strategy={strategy}
        onEditRow={vi.fn()}
        onOpenMore={vi.fn()}
        onRun={vi.fn()}
        canStart
      />,
    )

    expect(screen.getByText('换手率 高于 5%')).toBeInTheDocument()
    expect(screen.getByText('RSI 低于 30')).toBeInTheDocument()
  })
})
