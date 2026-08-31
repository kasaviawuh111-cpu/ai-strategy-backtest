import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { ChartMark, SeriesPoint } from '../types'
import { EquityChart } from './EquityChart'

const series: SeriesPoint[] = [
  { date: '2024-03-14', equity: 100, strategy: 0, benchmark: 0, drawdown: 0 },
  { date: '2024-03-15', equity: 90, strategy: -10, benchmark: -5, drawdown: -10 },
]

const mark: ChartMark = {
  index: 1, kind: 'buy', activityId: 'fill:1', side: 'buy', title: '买入成交',
  occurredAt: '2024-03-15T09:30:00+08:00', reason: '使用日线开盘价代理。',
  signalAt: '2024-03-14T20:57:33+08:00', signalReason: '年报首次可得。',
  orderAt: '2024-03-15T09:15:00+08:00', fillAt: '2024-03-15T09:30:00+08:00',
  price: 13.82, quantity: 3_800,
  priceLimitImpact: '活动记录未标记涨跌停影响',
  capacityImpact: '上一交易日已完成成交量 × 参与率',
  tPlusOneImpact: '活动记录未标记次日可卖限制',
  timeQuality: 'daily_bar_open_proxy',
  timeSemantics: '日线开盘、最高、最低、收盘与成交量数据中的开盘价代理记录边界，不代表真实逐笔成交',
  evidence: [],
}

const firstMark: ChartMark = {
  ...mark,
  index: 0,
  activityId: 'fill:0',
  occurredAt: '2024-03-14T09:30:00+08:00',
}

const sellMark: ChartMark = {
  ...mark,
  kind: 'sell',
  side: 'sell',
  activityId: 'fill:sell',
  title: '卖出成交',
}

describe('EquityChart', () => {
  it('renders a separate drawdown path and hands the clicked point to the order list', async () => {
    const user = userEvent.setup()
    const onSelectMark = vi.fn()
    render(<EquityChart series={series} marks={[mark]} onSelectMark={onSelectMark} />)

    expect(screen.getByRole('img', { name: /最大回撤 -10.00%/ })).toBeInTheDocument()
    const buyPoint = screen.getByRole('button', { name: /买入成交.*回车键/ })
    expect(buyPoint).toHaveTextContent('B')
    await user.click(buyPoint)

    expect(onSelectMark).toHaveBeenCalledWith(expect.objectContaining({ activityId: 'fill:1' }))
    expect(onSelectMark).toHaveBeenCalledTimes(1)
  })

  it('does not select a point merely because keyboard focus moved onto it', () => {
    const onSelectMark = vi.fn()
    render(<EquityChart series={series} marks={[mark]} onSelectMark={onSelectMark} />)

    fireEvent.focus(screen.getByRole('button', { name: /买入成交.*回车键/ }))

    expect(onSelectMark).not.toHaveBeenCalled()
  })

  it('no longer renders a second point navigator beside the order list', async () => {
    const user = userEvent.setup()
    render(<EquityChart series={series} marks={[mark]} />)
    await user.click(screen.getByRole('button', { name: /买入成交.*回车键/ }))

    expect(screen.queryByText('点位详情')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '上一点' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '下一点' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '选择点位' })).not.toBeInTheDocument()
  })

  it('operates a point with Enter', () => {
    const onSelectMark = vi.fn()
    render(<EquityChart series={series} marks={[mark]} onSelectMark={onSelectMark} />)
    fireEvent.keyDown(screen.getByRole('button', { name: /买入成交.*回车键/ }), { key: 'Enter' })
    expect(onSelectMark).toHaveBeenCalledTimes(1)
  })

  it('uses B and S points instead of geometric buy/sell symbols', () => {
    const { container } = render(<EquityChart series={series} marks={[mark, sellMark]} />)
    expect(screen.getByRole('button', { name: /买入成交/ })).toHaveTextContent('B')
    expect(screen.getByRole('button', { name: /卖出成交/ })).toHaveTextContent('S')
    expect(container.querySelector('svg > circle[pointer-events="none"]')).toBeInTheDocument()
  })

  it('places the tooltip opposite the selected point instead of pinning it over the y-axis', async () => {
    const user = userEvent.setup()
    const { container } = render(<EquityChart series={series} marks={[firstMark, mark]} />)

    await user.click(screen.getByRole('button', { name: /买入成交.*2024-03-14.*回车键/ }))
    expect(container.querySelector('.tip.on')).toHaveAttribute('data-placement', 'right')

    await user.click(screen.getByRole('button', { name: /买入成交.*2024-03-15.*回车键/ }))
    expect(container.querySelector('.tip.on')).toHaveAttribute('data-placement', 'left')
  })

  it('explains how to recover when the series is absent', () => {
    render(<EquityChart series={[]} marks={[]} />)
    expect(screen.getByText(/请重新读取结果/)).toBeInTheDocument()
  })

  it('keeps the buy-and-hold line but hides excess when the strategy never bought', () => {
    render(<EquityChart series={series} marks={[]} comparisonAvailable={false} />)

    expect(screen.getByText(/买入后一直持有/)).toBeInTheDocument()
    expect(screen.queryByText(/^超额/)).not.toBeInTheDocument()
  })
})
