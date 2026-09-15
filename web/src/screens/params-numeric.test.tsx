import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterAll, beforeAll, expect, it, vi } from 'vitest'
import { mockApi, enableImmediateMockWaitForTests, resetMockWaitForTests } from '../shared/api/mock'
import { toLiveRevisionBody, withEditedStrategySpec } from '../shared/api/contract'
import type { StrategyDraft } from '../shared/api/types'
import { ParamsScreen } from './index'

let base: StrategyDraft
beforeAll(async () => {
  vi.stubEnv('VITE_DATA_AS_OF_DATE', '2026-09-05')
  enableImmediateMockWaitForTests()
  const result = await mockApi.compile({ utterance: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
  if (result.status !== 'compiled') throw new Error('fixture unavailable')
  base = { ...result.draft, backtest: { ...result.draft.backtest, start: '2025-09-05', end: '2026-09-05' } }
})
afterAll(() => { vi.unstubAllEnvs(); resetMockWaitForTests() })

function harness(initial: StrategyDraft, focus: 'exit' | 'more' = 'more', withStock = false) {
  const changed = vi.fn()
  const back = vi.fn()
  const saveStock = vi.fn()
  function Harness() {
    const [draft, setDraft] = useState(initial)
    return <ParamsScreen open draft={draft} onChange={(next) => { changed(next); setDraft(next) }}
      onBack={back} onReset={() => setDraft(initial)} isLocked={false} focus={focus}
      stockEditor={withStock ? { onSearch: vi.fn(), onSave: saveStock } : undefined} />
  }
  render(<Harness />)
  return { changed, back, saveStock }
}

it('opens and closes parameter explanations without changing the draft', () => {
  const { changed, back } = harness(base)
  const info = screen.getByRole('button', { name: '了解单边固定价差' })
  fireEvent.click(info)
  expect(info).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByText(/每股的固定不利价差/)).toBeVisible()
  expect(screen.getByRole('dialog', { name: '单边固定价差说明' }).closest('.setting-row')).toBeNull()
  // Smooth section navigation may still emit scroll events after opening help.
  fireEvent.scroll(info.closest('.scroll')!)
  fireEvent.resize(window)
  expect(screen.getByRole('dialog', { name: '单边固定价差说明' })).toBeVisible()
  fireEvent.keyDown(info, { key: 'Escape' })
  expect(info).toHaveAttribute('aria-expanded', 'false')
  expect(changed).not.toHaveBeenCalled()
  expect(back).not.toHaveBeenCalled()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(back).toHaveBeenCalledOnce()
})

it('keeps one explanation open and dismisses on outside interaction', () => {
  const { changed } = harness(base)
  fireEvent.click(screen.getByRole('button', { name: '了解单边固定价差' }))
  fireEvent.click(screen.getByRole('button', { name: '了解佣金率' }))
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  expect(screen.getByRole('dialog', { name: '佣金率说明' })).toBeVisible()
  fireEvent.pointerDown(screen.getByRole('spinbutton', { name: '佣金率' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(changed).not.toHaveBeenCalled()
})

it('allows valid price-plan share counts to leave settings without native step mismatch', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, entry: null, exit: null,
    trading_plan: { kind: 'grid' as const, parameters: {
      anchor_mode: 'manual', anchor_price: '20', spacing: '1', spacing_mode: 'cny',
      sizing_mode: 'shares',
      order_shares: 100, initial_shares: 1000, min_shares: 500, max_shares: 2000,
    } } })
  const { back } = harness(initial)
  for (const label of ['每格股数', '最大持仓（股）']) {
    const input = screen.getByRole('spinbutton', { name: label }) as HTMLInputElement
    expect(input.checkValidity()).toBe(true)
    expect(input.min).toBe('1')
  }
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).toHaveBeenCalledOnce()
})

it('edits independent buy and sell spacing without replacing the other direction', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, entry: null, exit: null,
    trading_plan: { kind: 'grid', parameters: {
      anchor_mode: 'first_open', spacing: '1', spacing_mode: 'anchor_percent',
      buy_spacing: '3', sell_spacing: '1', sizing_mode: 'shares', order_shares: 100,
      initial_shares: 0, min_shares: 0, max_shares: 10000,
    } } })
  const { changed, back } = harness(initial)
  expect(screen.queryByLabelText('每格间距')).not.toBeInTheDocument()
  expect(screen.getByLabelText('买入间距')).toHaveValue(3)
  expect(screen.getByLabelText('卖出间距')).toHaveValue(1)
  fireEvent.change(screen.getByLabelText('买入间距'), { target: { value: '4' } })
  fireEvent.change(screen.getByLabelText('卖出间距单位'), { target: { value: 'cny' } })
  const plan = toLiveRevisionBody(changed.mock.calls.at(-1)?.[0]).strategy.trading_plan!
  expect(plan.parameters.buy_spacing).toBe(4)
  expect(plan.parameters.sell_spacing).toBe('1')
  expect(plan.parameters.sell_spacing_mode).toBe('cny')
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).toHaveBeenCalledOnce()
})

it.each(['entry', 'exit'] as const)('preserves independent %s when editing the opposite grid side', (side) => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec,
    execution: { timezone: 'Asia/Shanghai', entry_policy: 'composed_entry_leg',
      exit_policy: 'composed_exit_leg', data_capability: 'daily_and_minute_ohlcv_events_financials',
      execution_resolution: '1m', evaluation_frequency: 'daily_close_and_minute_bar',
      position_policy: 'bounded_inventory', t_plus_one: true },
    entry: side === 'entry' ? base.strategySpec.entry : null,
    exit: side === 'exit' ? base.strategySpec.exit : null,
    trading_plan: { kind: 'grid', parameters: {
      anchor_mode: 'first_open', observation: 'minute_bar', spacing: 1,
      spacing_mode: 'cny', order_shares: 100, sizing_mode: 'shares',
      initial_shares: 0, min_shares: 0, max_shares: 10000,
    } } })
  const { changed } = harness(initial)
  const visible = side === 'exit' ? '买入间距' : '卖出间距'
  expect(screen.queryByLabelText(side === 'exit' ? '卖出间距' : '买入间距')).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText(visible), { target: { value: '2' } })
  const saved = toLiveRevisionBody(changed.mock.calls.at(-1)?.[0]).strategy
  expect(saved[side]).toEqual(initial.strategySpec[side])
  expect(saved.execution).toEqual(initial.strategySpec.execution)
  expect(saved.trading_plan?.parameters[side === 'exit' ? 'buy_spacing' : 'sell_spacing']).toBe(2)
})

it('edits fixed CNY slippage without changing the proportional setting', () => {
  const { changed } = harness(base)
  fireEvent.change(screen.getByLabelText('单边固定价差'), { target: { value: '0.02' } })
  const updated = changed.mock.calls.at(-1)?.[0]
  expect(updated.execution.slippageCny).toBe(0.02)
  expect(updated.execution.slippageBps).toBe(base.execution.slippageBps)
  expect(toLiveRevisionBody(updated).execution_settings?.slippage_cny).toBe(0.02)
})

it('deletes the last rule digit without rebound, blocks all exits, then commits a legal replacement', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, exit: { op: 'first_of', children: [{
    type: 'holding_period_exit', sessions: 5, anchor: 'first_entry_fill',
    count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy',
  }] } })
  const { changed, back } = harness(initial, 'exit')
  const input = screen.getByRole('spinbutton', { name: '持有交易日' })
  fireEvent.change(input, { target: { value: '' } })
  expect(input).toHaveDisplayValue('')
  expect(changed).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  fireEvent.blur(input)
  expect(screen.getByRole('alert')).toHaveTextContent('请填写数值')
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value: '0' } })
  fireEvent.change(input, { target: { value: '' } })
  expect(input).toHaveDisplayValue('')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(screen.getByRole('alert')).toHaveTextContent('请填写数值')
  fireEvent.click(screen.getByRole('button', { name: '返回' }))
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(back).not.toHaveBeenCalled()
  expect(input).toHaveFocus()
  expect(input).toHaveDisplayValue('')
  fireEvent.change(input, { target: { value: '7' } })
  expect(toLiveRevisionBody(changed.mock.calls.at(-1)?.[0]).strategy.exit!.children[0]).toMatchObject({ sessions: 7 })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).toHaveBeenCalledTimes(1)
})

it('keeps out-of-range and fractional integer edits visible without changing rules', () => {
  const { changed, back } = harness(base)
  const input = screen.getByRole('spinbutton', { name: '最多卖出尝试次数' })
  for (const [raw, error] of [['0', '不能小于 1'], ['1001', '不能大于 1000'], ['1.5', '请输入整数']] as const) {
    fireEvent.change(input, { target: { value: raw } })
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    expect(input).toHaveDisplayValue(raw)
    expect(screen.getByRole('alert')).toHaveTextContent(error)
  }
  expect(changed).not.toHaveBeenCalled()
  expect(back).not.toHaveBeenCalled()
  fireEvent.change(input, { target: { value: '8' } })
  expect(changed.mock.calls.at(-1)?.[0].execution.maxExitAttempts).toBe(8)
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).toHaveBeenCalledTimes(1)
})

it('buffers empty cash/fees, retains decimals, and cannot save a stock around an invalid number', () => {
  const { changed, back, saveStock } = harness(base, 'more', true)
  const fee = screen.getByRole('spinbutton', { name: '最低佣金' })
  fireEvent.change(fee, { target: { value: '' } })
  expect(screen.getByRole('button', { name: /修改股票/ })).toBeDisabled()
  expect(changed).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).not.toHaveBeenCalled()
  expect(saveStock).not.toHaveBeenCalled()
  fireEvent.change(fee, { target: { value: '2.5' } })
  expect(changed.mock.calls.at(-1)?.[0].execution.minimumCommissionCny).toBe(2.5)
  expect(screen.getByRole('button', { name: /修改股票/ })).toBeEnabled()
  fireEvent.change(screen.getByRole('spinbutton', { name: '佣金率' }), { target: { value: '0.025' } })
  expect(changed.mock.calls.at(-1)?.[0].execution.commissionRate).toBe(0.00025)
  const cash = screen.getByRole('spinbutton', { name: '初始资金' })
  changed.mockClear()
  fireEvent.change(cash, { target: { value: '' } })
  fireEvent.blur(cash)
  expect(cash).toHaveDisplayValue('')
  expect(changed).not.toHaveBeenCalled()
  fireEvent.change(cash, { target: { value: '10000.5' } })
  fireEvent.blur(cash)
  expect(screen.getByRole('alert')).toHaveTextContent('请输入整数')
  fireEvent.change(cash, { target: { value: '50000' } })
  expect(changed.mock.calls.at(-1)?.[0].backtest.initialCashCny).toBe(50000)
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).toHaveBeenCalledTimes(1)
})

it('only explicit reset discards incomplete numbers', () => {
  const { changed, back } = harness(base)
  const input = screen.getByRole('spinbutton', { name: '单边滑点' })
  fireEvent.change(input, { target: { value: '' } })
  fireEvent.blur(input)
  expect(screen.getByRole('alert')).toHaveTextContent('请填写数值')
  fireEvent.click(screen.getByRole('button', { name: '重置为识别结果' }))
  expect(screen.getByRole('spinbutton', { name: '单边滑点' })).toHaveValue(base.execution.slippageBps)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(changed).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '完成' }))
  expect(back).toHaveBeenCalledTimes(1)
})
