import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MetricSearch } from './MetricSearch'
import { metricApi } from '../shared/api/client'
import type { IndicatorCapability } from '../shared/api/types'

afterEach(() => vi.restoreAllMocks())
const indicators = [{ indicator_id: 'provider.numeric', status: 'stable', display_name: '数值比较',
  description: '数值', } as IndicatorCapability]
const props = { id:'entry', name:'均线', indicators, instrument:'002558.SZ',
  start:'2025-09-08', end:'2026-09-08', onSelect:vi.fn() }

it('queries an uncatalogued metric for the current stock and retains the returned unit', async () => {
  const discover = vi.spyOn(metricApi, 'discover').mockResolvedValue([{name:'净资产收益率TTM',unit:'%',note:'已返回历史数据'}])
  const onQuery = vi.fn()
  render(<MetricSearch {...props} onQuery={onQuery} />)
  fireEvent.change(screen.getByRole('searchbox'), {target:{value:'ROE'}})
  fireEvent.click(screen.getByRole('button',{name:'查询更多'}))
  fireEvent.click(await screen.findByRole('button',{name:/净资产收益率TTM（%）/}))
  expect(discover).toHaveBeenCalledWith({ instrument_id:'002558.SZ',metric_query:'ROE',
    start:'2025-09-08',end:'2026-09-08' }, expect.any(AbortSignal))
  expect(onQuery).toHaveBeenCalledWith('净资产收益率TTM','%')
})

it('ignores a late result after the user changes the query', async () => {
  let resolve!: (value: {name:string;unit:string;note:string}[]) => void
  const discover = vi.spyOn(metricApi,'discover').mockImplementation(() => new Promise(done => {resolve=done}))
  const onQuery = vi.fn()
  render(<MetricSearch {...props} onQuery={onQuery} />)
  fireEvent.change(screen.getByRole('searchbox'), {target:{value:'ROE'}})
  fireEvent.click(screen.getByRole('button',{name:'查询更多'}))
  fireEvent.change(screen.getByRole('searchbox'), {target:{value:'换手率'}})
  expect(discover.mock.calls[0]![1]!.aborted).toBe(true)
  resolve([{name:'旧ROE',unit:'%',note:'已返回历史数据'}])
  await waitFor(() => expect(screen.getByRole('button',{name:'查询更多'})).toBeEnabled())
  expect(screen.queryByRole('button',{name:/旧ROE/})).not.toBeInTheDocument()
  expect(onQuery).not.toHaveBeenCalled()
})

it.each([
  {name:'换手率'}, {instrument:'600519.SH'},
  {start:'2024-09-08'}, {end:'2026-09-07'},
])('discards pending results when the editing context changes: %j', async changed => {
  let resolve!: (value: {name:string;unit:string;note:string}[]) => void
  const discover = vi.spyOn(metricApi,'discover').mockImplementation(() => new Promise(done => {resolve=done}))
  const onQuery = vi.fn()
  const view = render(<MetricSearch {...props} onQuery={onQuery} />)
  fireEvent.click(screen.getByRole('button',{name:'查询更多'}))
  view.rerender(<MetricSearch {...props} {...changed} onQuery={onQuery} />)
  expect(discover.mock.calls[0]![1]!.aborted).toBe(true)
  await act(async () => { resolve([{name:'过期指标',unit:'%',note:'已返回历史数据'}]) })
  expect(screen.queryByRole('button',{name:/过期指标/})).not.toBeInTheDocument()
  expect(screen.getByRole('button',{name:'查询更多'})).toBeEnabled()
  expect(onQuery).not.toHaveBeenCalled()
})

it('keeps the original condition unchanged when discovery fails', async () => {
  vi.spyOn(metricApi,'discover').mockRejectedValue(new Error('connection failed'))
  const onQuery = vi.fn()
  render(<MetricSearch {...props} onQuery={onQuery} />)
  fireEvent.click(screen.getByRole('button',{name:'查询更多'}))
  expect(await screen.findByRole('status')).toHaveTextContent('原条件没有改变')
  expect(onQuery).not.toHaveBeenCalled()
  expect(screen.getByRole('searchbox')).toHaveValue('均线')
})

it('does not offer an unchecked custom condition when no usable history is returned', async () => {
  vi.spyOn(metricApi,'discover').mockResolvedValue([])
  const onQuery = vi.fn()
  render(<MetricSearch {...props} onQuery={onQuery} />)
  fireEvent.change(screen.getByRole('searchbox'), {target:{value:'未知指标'}})
  expect(screen.queryByRole('button',{name:/使用.*设置条件/})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'查询更多'}))
  expect(await screen.findByRole('status')).toHaveTextContent('暂不列入可选结果')
  expect(onQuery).not.toHaveBeenCalled()
})
