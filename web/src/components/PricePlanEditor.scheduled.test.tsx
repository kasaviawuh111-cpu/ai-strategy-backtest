import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { PricePlanEditor } from './PricePlanEditor'
import { pricePlanSides, pricePlanTitle } from '../shared/price-plan'
import type { PricePlan, StrategyDraft } from '../shared/api/types'

const draftFor = (plan: PricePlan) => ({ strategySpec: { trading_plan: plan },
  execution: { slippageBps: 5, slippageCny: 0, commissionRate: 0.00025, minimumCommissionCny: 5 },
}) as StrategyDraft

it('edits scheduled holding exits in their original field without changing the calendar entry', () => {
  const plan: PricePlan = { kind: 'scheduled', parameters: {
    frequency: 'monthly', day: 1, at: 'open', side: 'buy', sizing_mode: 'shares', quantity: 100,
    exit_rules: [{ kind: 'holding_period', side: 'sell', sessions: 10, sizing_mode: 'all_position', quantity: 100 }],
  } }
  const onChange = vi.fn()
  render(<PricePlanEditor draft={draftFor(plan)} onChange={onChange} />)
  expect(screen.getByLabelText('条件1 持有交易日数')).toHaveValue(10)
  expect(screen.getByLabelText('条件1 委托数量方式')).toHaveValue('all_position')
  expect(screen.queryByLabelText('条件1 交易方向')).toBeNull()
  fireEvent.change(screen.getByLabelText('条件1 持有交易日数'), { target: { value: '12' } })
  const updated = onChange.mock.lastCall?.[0].strategySpec.trading_plan
  expect(updated.parameters).toMatchObject({ frequency: 'monthly', day: 1, quantity: 100,
    exit_rules: [{ sessions: 12, side: 'sell', sizing_mode: 'all_position' }] })
  expect(updated.parameters.rules).toBeUndefined()
  expect(plan.parameters.exit_rules).toEqual([{ kind: 'holding_period', side: 'sell', sessions: 10, sizing_mode: 'all_position', quantity: 100 }])
})

it.each(['stop_loss', 'holding_period'])('preserves %s all-position sizing without displaying a fixed quantity', (kind) => {
  const plan: PricePlan = { kind: 'conditional', parameters: {
    observation: 'minute_bar', rules: [
      { kind, side: 'sell', gap: 1, gap_unit: 'percent', sessions: 2, quantity: 100, sizing_mode: 'all_position' },
    ],
  } }
  const onChange = vi.fn()
  render(<PricePlanEditor draft={draftFor(plan)} onChange={onChange} />)
  expect(screen.getByLabelText('条件1 委托数量方式')).toHaveValue('all_position')
  expect(screen.queryByLabelText('条件1 委托股数')).toBeNull()
  fireEvent.change(screen.getByLabelText('条件1 委托数量方式'), { target: { value: 'shares' } })
  expect(onChange.mock.lastCall?.[0].strategySpec.trading_plan.parameters.rules[0].sizing_mode).toBe('shares')
})

it('keeps strict price comparison editable without moving the threshold', () => {
  const plan: PricePlan = { kind: 'conditional', parameters: { rules: [
    { kind: 'price', side: 'buy', direction: 'down', target_price: 20, price_comparison: 'strict' },
  ] } }
  const onChange = vi.fn()
  render(<PricePlanEditor draft={draftFor(plan)} onChange={onChange} />)
  expect(screen.getByLabelText('条件1 价格比较边界')).toHaveValue('strict')
  fireEvent.change(screen.getByLabelText('条件1 价格比较边界'), { target: { value: 'inclusive' } })
  expect(onChange.mock.lastCall?.[0].strategySpec.trading_plan.parameters.rules[0]).toMatchObject({ target_price: 20, price_comparison: 'inclusive' })
})

it('shows scheduled parameters and saves a sell as shares without grid defaults', () => {
  const plan: PricePlan = { kind: 'scheduled', parameters: {
    frequency: 'monthly', day: 31, at: 'open', side: 'buy', sizing_mode: 'amount',
    quantity: 100, budget_cny: 2000,
  } }
  const draft = draftFor(plan)
  const onChange = vi.fn()
  render(<PricePlanEditor draft={draft} onChange={onChange} />)
  expect(pricePlanTitle(plan)).toBe('每月计划买入')
  expect(pricePlanSides(plan).entry[0]).toContain('2000元（含费用预算）')
  expect(screen.queryByLabelText('买入间距')).toBeNull()
  fireEvent.change(screen.getByLabelText('交易方向'), { target: { value: 'sell' } })
  expect(onChange.mock.lastCall?.[0].strategySpec.trading_plan.parameters).toMatchObject({ side: 'sell', sizing_mode: 'shares' })
  fireEvent.change(screen.getByLabelText('执行周期'), { target: { value: 'weekly' } })
  expect(onChange.mock.lastCall?.[0].strategySpec.trading_plan.parameters).toMatchObject({ frequency: 'weekly', day: 1 })
  fireEvent.change(screen.getByLabelText('执行时点'), { target: { value: 'close' } })
  expect(onChange.mock.lastCall?.[0].strategySpec.execution).toMatchObject({
    entry_policy: 'scheduled_session_close', exit_policy: 'scheduled_session_close',
    execution_resolution: '1d', evaluation_frequency: 'pre_session_schedule',
  })
  expect(onChange.mock.lastCall?.[0].execution).toMatchObject({
    entryPolicy: 'scheduled_session_close', exitPolicy: 'scheduled_session_close',
    dataCapability: 'daily_ohlcv', evaluationFrequency: 'pre_session_schedule',
  })
})
