import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'

import { Proposals, type ProposalItem } from './Proposals'
import { gridProposalPresentation, pricePlanSides } from '../shared/price-plan'

it('shows independent exit and omits replaced grid selling in a mixed candidate', () => {
  const plan = { kind: 'grid' as const, parameters: { spacing: 1, order_shares: 100 } }
  const presentation = gridProposalPresentation(plan, undefined, { entry: null,
    exit: { op: 'first_of', children: [] } })
  render(<Proposals items={[{ id: 'mixed', ...presentation, entry: '下跨买入100股',
    exit: 'MACD 死叉卖出全部可卖持仓' }]} onPick={vi.fn()} />)
  expect(screen.getByText('MACD 死叉卖出全部可卖持仓')).toBeInTheDocument()
  expect(screen.queryByText('上涨多少卖出')).not.toBeInTheDocument()
  expect(screen.getByText('下跌多少买入')).toBeInTheDocument()
})

it('keeps unresolved and pinned latest quotes distinct from the first open', () => {
  const plan = { kind: 'grid' as const, parameters: {
    anchor_mode: 'latest_price', anchor_price: null as number | null,
    spacing_mode: 'anchor_percent', spacing: 1, order_shares: 100, max_shares: 10000,
  } }
  expect(pricePlanSides(plan).entry[0]).toContain('行情最新价（待获取）')
  plan.parameters.anchor_price = 79.32
  expect(pricePlanSides(plan).entry[0]).toContain('行情最新价 79.32元')
  expect(gridProposalPresentation(plan).gridFacts[2]?.value).toBe('行情最新价 79.32元')
  expect(pricePlanSides(plan).entry[0]).not.toContain('开盘价')
})

it('derives distinct grid cards from actual units and directional spacing', () => {
  const parameters = { anchor_mode: 'manual', anchor_price: 85, lower_price: 60, upper_price: 100,
    order_shares: 100, initial_shares: 1000, min_shares: 500, max_shares: 10000, spacing: 1 }
  const cards = [
    { ...parameters, spacing_mode: 'cny' },
    { ...parameters, spacing_mode: 'anchor_percent', buy_spacing: 3, sell_spacing: 1 },
    { ...parameters, spacing_mode: 'percent', spacing: 2 },
  ].map((p, i) => ({ id: String(i), ...gridProposalPresentation({ kind: 'grid', parameters: p }, ['窄幅震荡网格', '均衡网格', '宽幅波段网格'][i]) }))
  expect(new Set(cards.map(card => card.title)).size).toBe(3)
  render(<Proposals items={cards} onPick={() => {}} />)
  expect(screen.getByRole('button', { name: '窄幅震荡网格' })).toHaveTextContent('60–100元')
  expect(screen.getByRole('button', { name: '均衡网格' })).toHaveTextContent('100股')
  expect(screen.getByRole('button', { name: '均衡网格' })).toHaveAttribute('aria-description', expect.stringContaining('500 / 10000股'))
  expect(screen.getByRole('button', { name: '宽幅波段网格' })).toHaveTextContent('约1.96%')
  expect(gridProposalPresentation({ kind: 'grid', parameters }).title).toBe('跌1元买，涨1元卖')
})

it('does not present unresolved grid geometry sentinels as a real price interval', () => {
  const card = gridProposalPresentation({ kind: 'grid', parameters: {
    anchor_mode: 'latest_price', anchor_price: null, lower_price: 0.01, upper_price: 1000000,
    range_percent: 20, levels_below: 4, levels_above: 4, spacing_mode: 'anchor_percent', spacing: 5,
  } })
  const range = card.gridFacts.find(fact => fact.label === '价格区间')!.value
  expect(range).toContain('基准上下20%')
  expect(range).toContain('下方4格；上方4格')
  expect(range).toContain('价格待基准确定后计算')
  expect(range).not.toContain('1000000元')
})

it('shows at most three complete paired cards and selects from anywhere in a card', async () => {
  const items: ProposalItem[] = ['东方财富', '贵州茅台', '比亚迪', '平安银行'].map((name, index) => ({
    id: `pair-${index}`,
    title: `${name} · 方案${index + 1}`,
    paired: true,
    detail: `组合${index + 1}的匹配理由。`,
    entry: '收盘价创20日新高',
    exit: '跌破20日均线',
  }))
  const onPick = vi.fn()
  const user = userEvent.setup()
  render(<Proposals items={items} onPick={onPick} />)

  const group = screen.getByRole('group', { name: '股票与策略组合' })
  const buttons = within(group).getAllByRole('button')
  const second = within(group).getByRole('button', { name: '贵州茅台 · 方案2' })
  expect(buttons).toHaveLength(3)
  expect(second).toHaveClass('proposal')
  expect(second).toHaveTextContent('贵州茅台 · 方案2')
  expect(screen.queryByRole('button', { name: '平安银行 · 方案4' })).not.toBeInTheDocument()
  expect(group.querySelector('details')).toBeNull()
  expect(within(second).getByText('组合2的匹配理由。')).toBeVisible()
  expect(second).toHaveAttribute('aria-description', expect.stringContaining('匹配理由'))
  expect(within(second).getByText('收盘价创20日新高')).toBeVisible()
  expect(within(second).getByText('跌破20日均线')).toBeVisible()
  expect(onPick).not.toHaveBeenCalled()
  await user.click(within(second).getByText('收盘价创20日新高'))
  expect(onPick).toHaveBeenCalledExactlyOnceWith('pair-1')
})

it('keeps the original unpaired strategy card and selection behavior', async () => {
  const onPick = vi.fn()
  const user = userEvent.setup()
  const { container } = render(<Proposals items={[{
    id: 'legacy', title: '趋势确认', instrument: '东方财富 · 300059.SZ',
    entry: '上穿20日均线', exit: '下穿20日均线',
  }]} onPick={onPick} />)

  const choice = screen.getByRole('button', { name: '趋势确认' })
  expect(choice).toHaveClass('proposal')
  expect(choice).toHaveTextContent('东方财富 · 300059.SZ')
  expect(choice).toHaveTextContent('买入上穿20日均线')
  expect(choice).toHaveTextContent('卖出下穿20日均线')
  expect(container.querySelector('details')).toBeNull()
  await user.click(choice)
  expect(onPick).toHaveBeenCalledExactlyOnceWith('legacy')
})
