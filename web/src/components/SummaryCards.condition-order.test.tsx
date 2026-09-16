import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { StrategyRuleNode, StrategySummary } from '../types'
import { StrategyCard } from './SummaryCards'

const leaf = (id: string, label: string, parameters: string[] = []): StrategyRuleNode => ({
  kind: 'leaf',
  id,
  label,
  detail: `日线收盘确认：${label}`,
  tone: 'indicator',
  parameters,
})

describe('condition-order strategy card', () => {
  it('shows the trigger note only in the buy section without changing the condition editor', () => {
    const entryRule = leaf('entry', 'MACD 金叉')
    const exitRule = leaf('exit', 'MACD 死叉')
    const strategy: StrategySummary = {
      title: 'MACD 规则', strategyHash: 'sha256:entry-note', entryRule, exitRule,
      rows: [
        { key: 'entry', label: '买入', value: entryRule.label, kind: 'buy' },
        { key: 'exit', label: '卖出', value: exitRule.label, kind: 'sell' },
      ],
      entryTriggerNote: '金叉触发，持续在上方不重复买入',
      confirmation: '日线收盘确认', earliestExecution: '下一可交易日开盘', conditionCount: 2, eventFacts: [],
    }
    const edit = vi.fn()
    const props = { instrument: { name: '东方财富', code: '300059.SZ' },
      onEditRow: edit, onOpenMore: vi.fn(), onRun: vi.fn() }
    const { rerender } = render(<StrategyCard {...props} strategy={strategy} />)
    expect(screen.getByRole('region', { name: '买入条件' })).toHaveTextContent(strategy.entryTriggerNote!)
    expect(screen.getByRole('region', { name: '卖出条件' })).not.toHaveTextContent(strategy.entryTriggerNote!)
    expect(screen.getAllByText(strategy.entryTriggerNote!)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'MACD 金叉' }))
    expect(edit).toHaveBeenCalledWith('entry', 'entry')

    rerender(<StrategyCard {...props} strategy={{ ...strategy, entryTriggerNote: undefined }} />)
    expect(screen.queryByText(strategy.entryTriggerNote!)).not.toBeInTheDocument()
  })

  it('does not call a single conditional rule stage 1', () => {
    const entryRule = leaf('entry', '建仓：区间首个交易日开盘提交买入10000股')
    const exitRule = leaf('exit', '卖出条件：较持仓成交均价上涨20%，卖出全部可卖持仓')
    const strategy: StrategySummary = {
      title: '止盈', pricePlanKind: 'conditional', hasSequentialPricePlanStages: false,
      rows: [
        { key: 'entry', label: '买入', value: entryRule.label, kind: 'buy' },
        { key: 'exit', label: '卖出', value: exitRule.label, kind: 'sell' },
      ],
      strategyHash: 'sha256:single-condition', entryRule, exitRule,
      confirmation: '开盘建仓后监测', earliestExecution: '区间首日开盘', conditionCount: 1, eventFacts: [],
    }
    render(<StrategyCard instrument={{ name: '平安银行', code: '000001.SZ' }} strategy={strategy}
      onEditRow={vi.fn()} onOpenMore={vi.fn()} onRun={vi.fn()} />)

    expect(screen.getByRole('button', { name: /区间开始时建仓/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /满足条件时触发/ })).toBeInTheDocument()
    expect(screen.queryByText(/阶段1/)).not.toBeInTheDocument()
  })

  it('renders independent MACD exit instead of an empty grid exit panel', () => {
    const strategy: StrategySummary = {
      title: '网格买入与MACD卖出', pricePlanKind: 'grid', strategyHash: 'sha256:mixed',
      rows: [{ key: 'exit', label: '卖出', value: 'MACD 死叉', kind: 'sell' }],
      entryRule: leaf('entry', '下跨买入'), exitRule: leaf('exit', 'MACD 死叉'),
      confirmation: '日线确认', earliestExecution: '下一开盘', conditionCount: 2, eventFacts: [],
      gridReview: { anchor: '首日开盘', initialization: '', buy: [], sell: [] },
    }
    render(<StrategyCard instrument={{ name: '东方财富', code: '300059.SZ' }} strategy={strategy}
      onEditRow={vi.fn()} onOpenMore={vi.fn()} onRun={vi.fn()} />)
    expect(screen.getByRole('region', { name: '卖出条件' })).toHaveTextContent('MACD 死叉')
    expect(screen.queryByText('卖出间距')).not.toBeInTheDocument()
  })
  it('opens grid parameter editing without submitting a backtest', () => {
    const edit = vi.fn(), run = vi.fn()
    const strategy: StrategySummary = {
      title: '网格交易', rows: [], strategyHash: 'sha256:grid',
      entryRule: leaf('entry', '下跨买入'), exitRule: leaf('exit', '上跨卖出'),
      confirmation: '分钟触发', earliestExecution: '下一分钟', conditionCount: 2, eventFacts: [],
      gridReview: { anchor: '起始日昨收26.95元', initialization: '空仓启动，等待买点', buy: [], sell: [] },
    }
    render(<StrategyCard instrument={{ name: '东方财富', code: '300059.SZ' }} strategy={strategy}
      onEditRow={edit} onOpenMore={vi.fn()} onRun={run} />)
    fireEvent.click(screen.getByRole('button', { name: '修改网格参数' }))
    expect(edit).toHaveBeenCalledWith('entry')
    expect(run).not.toHaveBeenCalled()
  })
  it.each([
    { blockedLabel: '暂不支持回测', checkingSettings: false, expectedLabel: '暂不支持回测' },
    { blockedLabel: '等待连接恢复', checkingSettings: false, expectedLabel: '等待连接恢复' },
    { blockedLabel: '暂不支持回测', checkingSettings: true, expectedLabel: '检查设置中' },
  ])('shows $expectedLabel with the specific reason below the action', ({ blockedLabel, checkingSettings, expectedLabel }) => {
    const entryRule = leaf('entry', 'MACD 金叉')
    const exitRule = leaf('exit', 'MACD 死叉')
    const strategy: StrategySummary = {
      title: 'MACD 规则', rows: [], strategyHash: 'sha256:blocked', entryRule, exitRule,
      confirmation: '日线收盘确认', earliestExecution: '下一可交易日开盘', conditionCount: 2, eventFacts: [],
    }
    const reason = '买入条件需要逐笔成交数据，当前仅有日线数据。\n策略和参数已保留。'
    render(<StrategyCard
      instrument={{ name: '东方财富', code: '300059.SZ' }} strategy={strategy}
      onEditRow={vi.fn()} onOpenMore={vi.fn()} onRun={vi.fn()}
      canStart={false} blockedLabel={blockedLabel} checkingSettings={checkingSettings}
      disabledReason={reason} error="原有错误详情"
    />)

    const action = screen.getByRole('button', { name: expectedLabel })
    const explanation = screen.getByRole('status')
    expect(action).toBeDisabled()
    expect(explanation).toHaveAttribute('aria-live', 'polite')
    expect(explanation.textContent).toBe(reason)
    expect(action.compareDocumentPosition(explanation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByRole('alert')).toHaveTextContent('原有错误详情')
  })

  it('keeps the core threshold in the first-level review', () => {
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

  it('keeps exact values on the rule node but hides them from the strategy card', () => {
    const entryRule = leaf('entry-turnover', '换手率 高于 5%', ['阈值 5%'])
    const exitRule = leaf('exit-rsi', 'RSI 低于 30', ['周期 14', '阈值 30'])
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
    expect(screen.queryByText(/参数：/)).not.toBeInTheDocument()
    expect(screen.queryByText(/阈值 30/)).not.toBeInTheDocument()
  })
})
