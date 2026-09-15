import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { ChartMark, SeriesPoint } from '../types'
import { EquityChart } from './EquityChart'
import { MiniEquity } from './MiniEquity'

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
  it('renders full-year-sized series without spreading points into function arguments', () => {
    const longSeries = Array.from({ length: 150_000 }, (_, index) => ({
      date: '2026-09-11', equity: 100, strategy: index === 80_000 ? -40 : 0,
      benchmark: 0, drawdown: index === 80_000 ? -40 : 0,
    }))
    const { container } = render(<>
      <EquityChart series={longSeries} marks={[]} />
      <MiniEquity series={longSeries} marks={[]} label="完整区间" />
    </>)
    expect(container.querySelector('svg')?.getAttribute('aria-label')).toContain('-40.00%')
    expect(screen.getByRole('img', { name: '完整区间' })).toBeInTheDocument()
  })
  it('shows days for a short range and aligns tick positions to the actual sample indices', () => {
    const { container } = render(<EquityChart series={series} marks={[]} />)
    const ticks = container.querySelectorAll('.axis-x > span')
    expect([...ticks].map(node => node.textContent)).toEqual(['03-14', '03-15'])
    expect(ticks[0]).toHaveStyle({ left: '0%' })
    expect(ticks[1]).toHaveStyle({ left: '100%' })
  })

  it('shows saved intraday timestamps in Beijing time rather than repeating the month', () => {
    const { container } = render(<EquityChart series={series.map((point, index) => ({
      ...point, date: index === 0 ? '2026-09-09T01:31:00Z' : '2026-09-09T07:00:00Z',
    }))} marks={[]} />)
    expect(container.querySelector('.axis-x')).toHaveTextContent('09:3115:00')
    expect(container.querySelector('.axis-context')).toHaveTextContent('2026-09-09 · 北京时间')
  })

  it('keeps the year visible across a short year boundary', () => {
    const { container } = render(<EquityChart series={series.map((point, index) => ({
      ...point, date: index === 0 ? '2025-12-31' : '2026-01-01',
    }))} marks={[]} />)
    expect(container.querySelector('.axis-x')).toHaveTextContent('12-31202501-012026')
  })

  it('retains both dates and times for minute data spanning two sessions', () => {
    const { container } = render(<EquityChart series={series.map((point, index) => ({
      ...point, date: index === 0 ? '2026-09-09T15:00:00+08:00' : '2026-09-10T09:31:00+08:00',
    }))} marks={[]} />)
    expect(container.querySelector('.axis-x')).toHaveTextContent('09-0915:0009-1009:31')
    expect(container.querySelector('.axis-context')).toHaveTextContent('2026-09-09 至 2026-09-10 · 北京时间')
  })

  it('breaks missing benchmark segments and never invents zero as the last value', () => {
    const data = [0, 2, null, 3, null].map((benchmark, index) => ({ ...series[0]!, benchmark, strategy: index }))
    const { container } = render(<EquityChart series={data} marks={[]} />)
    expect(screen.getByText('买入后一直持有 —')).toBeInTheDocument()
    expect(screen.getByText('超额 —')).toBeInTheDocument()
    const path = container.querySelector('path[stroke="var(--ink-3)"]')?.getAttribute('d')
    expect(path?.match(/M/g)).toHaveLength(2)
    expect(path?.match(/L/g)).toHaveLength(1)
    expect(container.querySelector('path[fill="var(--fill-excess)"]')).toBeNull()
    expect(container.querySelectorAll('circle[data-isolated-series="benchmark"]')).toHaveLength(1)
  })

  it('keeps the chart finite when the benchmark reaches zero NAV', () => {
    const { container } = render(<EquityChart series={[series[0]!, { ...series[1]!, benchmark: -100 }]} marks={[]} />)
    expect(screen.getByText('超额 —')).toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/)
    expect(container.querySelectorAll('svg text').length).toBeGreaterThan(2)
  })

  it('reserves space for fractional labels and retains precision while plotting excess', () => {
    const { container } = render(<EquityChart series={[series[0]!, { ...series[1]!, strategy: 0.0003, benchmark: 0, drawdown: 0 }]} marks={[]} />)
    expect(Number(container.querySelector('svg.chart')?.getAttribute('data-plot-left'))).toBeGreaterThanOrEqual(46)
    const path = container.querySelector('path[stroke="var(--color-info)"]')?.getAttribute('d')
    const coords = path?.split(' ')
    expect(coords?.[1]).not.toBe(coords?.[3])
  })

  it('does not let the hover guide intercept pointer input on a trade', () => {
    const { container } = render(<EquityChart series={series} marks={[mark]} selectedMarkId={mark.activityId} />)
    expect(container.querySelector('line[stroke="var(--ink-4)"]')).toHaveAttribute('pointer-events', 'none')
  })

  it('hides unfilled orders and expands clustered actual fills', async () => {
    const onSelectMark = vi.fn()
    render(<EquityChart series={series} marks={[mark, sellMark, { ...firstMark, kind: 'no_fill' }]} onSelectMark={onSelectMark} />)
    expect(screen.queryByRole('button', { name: /未成交/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: '2笔成交，展开明细' }))
    await userEvent.click(screen.getByRole('button', { name: /卖出成交.*定位委托/ }))
    expect(onSelectMark).toHaveBeenCalledWith(sellMark)
  })
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
    const { container } = render(<EquityChart series={series} marks={[firstMark, sellMark]} />)
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

  it('hides comparison lines consistently when the strategy never bought', () => {
    render(<EquityChart series={series} marks={[]} comparisonAvailable={false} />)

    expect(screen.queryByText(/买入后一直持有/)).not.toBeInTheDocument()
    expect(screen.queryByText(/^超额/)).not.toBeInTheDocument()
  })
})
