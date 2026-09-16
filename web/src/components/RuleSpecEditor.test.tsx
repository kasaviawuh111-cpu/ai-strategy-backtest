import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { beforeAll, afterAll, expect, it, vi } from 'vitest'
import { RuleSpecEditor } from './RuleSpecEditor'
import { metricApi } from '../shared/api/client'
import { StrategyCard } from './SummaryCards'
import { toStrategySummary } from '../view-model'
import { toLiveRevisionBody, withEditedStrategySpec } from '../shared/api/contract'
import { mockApi, enableImmediateMockWaitForTests, resetMockWaitForTests } from '../shared/api/mock'
import type { CapabilitiesResponse, StrategyDraft, StrategySpecIndicatorCondition } from '../shared/api/types'

let base: StrategyDraft
beforeAll(async () => {
  enableImmediateMockWaitForTests()
  const result = await mockApi.compile({ utterance: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
  if (result.status !== 'compiled') throw new Error('fixture unavailable')
  base = result.draft
})
afterAll(resetMockWaitForTests)

it('qualifies repeated period labels with their indicator name', async () => {
  const result = await mockApi.compile({
    utterance: '平安银行，KDJ金叉且RSI低于50买入，KDJ死叉卖出',
    instrument: { name: '平安银行', symbol: '000001.SZ', market: 'CN_A', exchange: 'SZSE' },
  })
  if (result.status !== 'compiled') throw new Error('fixture unavailable')
  render(<RuleSpecEditor draft={result.draft} side="entry" onChange={() => undefined} />)
  expect(screen.getByText('KDJ 周期')).toBeInTheDocument()
  expect(screen.getByText('RSI 周期')).toBeInTheDocument()
})

const seriesCondition: StrategySpecIndicatorCondition = {
  type: 'indicator_condition', indicator_id: 'provider.series_compare', definition_version: '1.0.0',
  params: { left_metric_query: '5日均线', right_metric_query: '20日均线', unit: '元' },
  timeframe: '1d', evaluation_mode: 'bar_close_confirmed', trigger: 'crosses_above', value: null,
}

const providerCapabilities: CapabilitiesResponse = {
  markets: ['CN_A'], input_modes: ['natural_language_zh'], strategy_scopes: ['single_instrument', 'long_only'],
  events: [], execution_policies: ['next_tradable_session_open'], event_catalog_status: 'published',
  event_backtest_available: false, event_preparation_available: false, event_availability_scope: 'unavailable',
  backtest_execution_available: true, limits: { max_body_bytes: 16384, max_utterance_characters: 2000, max_instrument_context_characters: 32 },
  indicators: ['provider.numeric', 'provider.series_compare'].map((indicator_id) => ({
    indicator_id, definition_version: '1.0.0', status: 'stable', display_name: indicator_id === 'provider.numeric' ? '数值比较' : '双指标比较',
    description: '测试能力', warmup_bars: 1, timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
    triggers: ['above', 'below', 'crosses_above', 'crosses_below'],
    parameters: (indicator_id === 'provider.numeric' ? ['metric_query', 'unit'] : ['left_metric_query', 'right_metric_query', 'unit']).map((name) => ({
      name, value_type: 'string', required: true, default: null, minimum: null, maximum: null, choices: [], display_name: null, unit: null,
    })),
    trigger_definitions: ['above', 'below', 'crosses_above', 'crosses_below'].map((id) => ({
      id, display_name: null, description: null, value_requirement: indicator_id === 'provider.series_compare' ? 'forbidden' : 'required',
      minimum: null, maximum: null, unit: null, exclusive_minimum: false, exclusive_maximum: false,
    })),
  })),
}

it('preserves discovered units and unrelated rules in the saved revision payload', async () => {
  const discover = vi.spyOn(metricApi, 'discover').mockResolvedValue([
    {name:'市盈率TTM', unit:'倍', note:'已返回历史数据，执行前仍需检查'},
  ])
  const capture = vi.fn()
  const initial = withEditedStrategySpec(base, {...base.strategySpec, entry:seriesCondition})
  function Harness() {
    const [draft, setDraft] = useState(initial)
    return <RuleSpecEditor draft={draft} side="entry" capabilities={providerCapabilities}
      onChange={next => {setDraft(next); capture(next)}} />
  }
  try {
    render(<Harness />)
    fireEvent.change(screen.getByRole('searchbox'), {target:{value:'市盈率TTM'}})
    fireEvent.click(screen.getByRole('button',{name:'查询更多'}))
    fireEvent.click(await screen.findByRole('button',{name:/市盈率TTM（倍）/}))
    fireEvent.change(screen.getByRole('combobox',{name:'entry触发方式'}), {target:{value:'below'}})
    fireEvent.change(screen.getByRole('spinbutton',{name:'阈值'}), {target:{value:'40'}})
    const next: StrategyDraft = capture.mock.calls.at(-1)![0]
    expect(toLiveRevisionBody(next).strategy.entry).toMatchObject({
      indicator_id:'provider.numeric', trigger:'below', value:40,
      params:{metric_query:'市盈率TTM',unit:'倍'},
    })
    expect(next.strategySpec.exit).toEqual(initial.strategySpec.exit)
    expect(next.strategySpec.instrument).toEqual(initial.strategySpec.instrument)
    expect(next.strategySpec.backtest).toEqual(initial.strategySpec.backtest)
    expect(next.execution).toEqual(initial.execution)
  } finally { discover.mockRestore() }
})

it('edits both series queries, their common unit and comparator without a scalar threshold', () => {
  const capture = vi.fn()
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, entry: { ...seriesCondition, value: 99 } })
  function Harness() {
    const [draft, setDraft] = useState(initial)
    return <RuleSpecEditor draft={draft} side="entry" capabilities={providerCapabilities}
      onChange={(next) => { setDraft(next); capture(next) }} />
  }
  render(<Harness />)
  expect(screen.queryByRole('spinbutton', { name: '阈值' })).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '5日均线 上穿 20日均线（共同单位：元）' })).toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: 'entry左侧指标与口径' }), { target: { value: '主力净流入金额' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'entry右侧指标与口径' }), { target: { value: '前5日平均主力净流入金额' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'entry共同单位' }), { target: { value: '亿元' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'entry触发方式' }), { target: { value: 'below' } })
  const next: StrategyDraft = capture.mock.calls.at(-1)![0]
  expect(toLiveRevisionBody(next).strategy.entry).toMatchObject({
    indicator_id: 'provider.series_compare', trigger: 'below', value: null,
    params: { left_metric_query: '主力净流入金额', right_metric_query: '前5日平均主力净流入金额', unit: '亿元' },
  })
  expect(screen.getByRole('heading', { name: '主力净流入金额 低于 前5日平均主力净流入金额（共同单位：亿元）' })).toBeInTheDocument()
  expect(next.strategySpec.exit).toEqual(base.strategySpec.exit)
  expect(next.execution).toEqual(base.execution)
})

it('clears the scalar threshold when switching to series comparison and restores scalar editing on return', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, entry: {
    ...seriesCondition, indicator_id: 'provider.numeric', params: { metric_query: '市盈率TTM', unit: '倍' }, trigger: 'above', value: 20,
  } }, providerCapabilities)
  const capture = vi.fn()
  function Harness() {
    const [draft, setDraft] = useState(initial)
    return <RuleSpecEditor draft={draft} side="entry" capabilities={providerCapabilities}
      onChange={(next) => { setDraft(next); capture(toLiveRevisionBody(next)) }} />
  }
  render(<Harness />)
  expect(screen.getByRole('spinbutton', { name: '阈值' })).toHaveValue(20)
  fireEvent.change(screen.getByRole('searchbox', { name: 'entry指标搜索' }), { target: { value: '双指标' } })
  fireEvent.click(screen.getByRole('button', { name: '双指标比较' }))
  expect(capture.mock.calls.at(-1)![0].strategy.entry).toMatchObject({ value: null, params: {} })
  expect(screen.queryByRole('spinbutton', { name: '阈值' })).not.toBeInTheDocument()
  expect(screen.getByRole('textbox', { name: 'entry左侧指标与口径' })).toHaveValue('')
  fireEvent.change(screen.getByRole('searchbox', { name: 'entry指标搜索' }), { target: { value: '数值比较' } })
  fireEvent.click(screen.getByRole('button', { name: '数值比较' }))
  expect(screen.getByRole('textbox', { name: 'entry指标名称与口径' })).toHaveValue('')
  expect(screen.getByRole('spinbutton', { name: '阈值' })).toHaveValue(0)
  expect(capture.mock.calls.at(-1)![0].strategy.entry).toMatchObject({ indicator_id: 'provider.numeric', value: 0, params: {} })
})

it('persists edited logical relation into the revision payload', () => {
  const capture = vi.fn()
  function Harness() {
    const [draft, setDraft] = useState(base)
    return <RuleSpecEditor draft={draft} side="entry" onChange={(next) => { setDraft(next); capture(toLiveRevisionBody(next)) }} />
  }
  render(<Harness />)
  fireEvent.change(screen.getByRole('combobox', { name: 'entry条件关系' }), { target: { value: 'any' } })
  expect(capture.mock.calls.at(-1)?.[0].strategy.entry.type).toBe('any')
})

it('edits holding sessions without changing entry, fees or independent exit semantics', () => {
  const draft = withEditedStrategySpec(base, { ...base.strategySpec, exit: { op: 'first_of', children: [{
    type: 'holding_period_exit', sessions: 10, anchor: 'first_entry_fill', count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy',
  }] } })
  const change = vi.fn()
  render(<RuleSpecEditor draft={draft} side="exit" path="exit-0" onChange={change} />)
  expect(screen.getByRole('combobox', { name: '卖出条件关系' })).toBeDisabled()
  fireEvent.change(screen.getByRole('spinbutton', { name: '持有交易日' }), { target: { value: '20' } })
  const next = change.mock.calls[0]?.[0]
  expect(toLiveRevisionBody(next).strategy.exit!.children[0]).toMatchObject({ sessions: 20 })
  expect(next.execution).toEqual(draft.execution)
  expect(next.strategySpec.entry).toEqual(draft.strategySpec.entry)
})

it('shows separate condition buttons, relation and unboxed range with correct edit target', () => {
  const edit = vi.fn()
  const { container } = render(<StrategyCard instrument={{ name: '东方财富', code: '300059.SZ' }} strategy={toStrategySummary(base)}
    onEditRow={edit} onOpenMore={vi.fn()} onRun={vi.fn()} />)
  expect(screen.getByRole('button', { name: '全部满足才买入（且）' })).toBeInTheDocument()
  fireEvent.click(container.querySelector('.condition-section .erow')!)
  expect(edit).toHaveBeenCalledWith('entry', 'entry-0')
  expect(container.querySelector('.condition-range')).not.toHaveClass('erow')
})

it('persists ALL across indicator and holding exits and labels the real relation', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, exit: { op: 'first_of', children: [
    base.strategySpec.exit!.children[0]!, { type: 'holding_period_exit', sessions: 10, anchor: 'first_entry_fill',
      count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy' },
  ] } })
  const change = vi.fn()
  render(<RuleSpecEditor draft={initial} side="exit" onChange={change} />)
  fireEvent.change(screen.getByRole('combobox', { name: '卖出条件关系' }), { target: { value: 'all' } })
  const next = change.mock.calls[0]?.[0]
  expect(toLiveRevisionBody(next).strategy.exit?.op).toBe('all')
  expect(next.exit.conditions[1].label).toBe('持有满 10 个交易日')
  expect(toStrategySummary(next).exitRule).toMatchObject({ operator: 'all' })
})

it('edits verified financial and event values in the executable payload', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, entry: {
    type: 'financial_condition', metric_id: 'financial.test', definition_version: '1.0.0', report_type: 'annual',
    period_basis: 'full_year', statement_scope: 'consolidated', revision_policy: 'as_known_at_signal', comparator: 'gt', value: 10, unit: 'PERCENT',
  } })
  const change = vi.fn()
  const { unmount } = render(<RuleSpecEditor draft={initial} side="entry" onChange={change} />)
  fireEvent.change(screen.getByRole('spinbutton', { name: '财务阈值（PERCENT）' }), { target: { value: '15' } })
  expect(toLiveRevisionBody(change.mock.calls[0]?.[0]).strategy.entry).toMatchObject({ value: 15, revision_policy: 'as_known_at_signal' })
  unmount()
  const event = withEditedStrategySpec(base, { ...base.strategySpec, entry: {
    type: 'event_condition', event_code: 'event.test', definition_version: '1.0.0', trigger: 'published', attributes: { count: 2 },
  } })
  render(<RuleSpecEditor draft={event} side="entry" onChange={change} />)
  fireEvent.change(screen.getByRole('spinbutton', { name: '事件参数 count' }), { target: { value: '3' } })
  expect(toLiveRevisionBody(change.mock.calls.at(-1)?.[0]).strategy.entry).toMatchObject({ attributes: { count: 3 } })
})
