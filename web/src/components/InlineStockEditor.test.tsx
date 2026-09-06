import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { InlineStockEditor, type StockSearchResult } from './InlineStockEditor'
import type { Instrument } from '../shared/api/types'
import { instrumentApi } from '../shared/api/client'

afterEach(() => { vi.unstubAllGlobals() })

// Component fixtures only. Real provider coverage is verified separately.
const candidates: [Instrument, Instrument] = [
  { name: '中国平安', symbol: '601318.SH', exchange: 'SSE', market: 'CN_A' },
  { name: '中国银行', symbol: '601988.SH', exchange: 'SSE', market: 'CN_A' },
]
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
const start = (overrides: Partial<Parameters<typeof InlineStockEditor>[0]> = {}) => {
  const props = { instrument: { name: '东方财富', code: '300059.SZ' },
    onSearch: vi.fn(async (): Promise<StockSearchResult> => ({ items: candidates, hasMore: true })),
    onSave: vi.fn(async () => {}), onEditingChange: vi.fn(), ...overrides }
  const rendered = render(<InlineStockEditor {...props} />)
  fireEvent.click(screen.getByRole('button', { name: /修改股票/ }))
  return { ...rendered, props }
}

describe('B28 inline stock editing', () => {
  it('labels a server stale-cache result without claiming a frontend refresh', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      query: '中国', source: 'eastmoney_security_search', retrieved_at: '2026-09-05T08:00:00Z',
      cache_status: 'stale_cache', has_more: false,
      items: [{ name: '中国平安', symbol: '601318.SH', exchange: 'SH' }],
    }), { status: 200 })))
    start({ onSearch: instrumentApi.search })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '中国' } })
    expect(await screen.findByRole('option', { name: '中国平安 601318.SH' })).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent('显示最近缓存的匹配，请选择股票')
    expect(screen.queryByText(/正在实时更新|2026-09-05/)).not.toBeInTheDocument()
  })

  it('selects the original name, handles empty/cancel, and saves a real candidate with the keyboard', async () => {
    const { props } = start()
    const input = screen.getByRole('combobox') as HTMLInputElement
    expect(input).toHaveFocus()
    expect(input.selectionStart).toBe(0)
    expect(input.selectionEnd).toBe(input.value.length)
    fireEvent.click(screen.getByRole('button', { name: '清空股票输入' }))
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('')
    expect(props.onSave).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.getByRole('button', { name: /修改股票：东方财富/ })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /修改股票/ }))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '中国' } })
    await screen.findByRole('option', { name: '中国平安 601318.SH' })
    expect(screen.getByText('继续输入可缩小范围')).toBeVisible()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(props.onSave).toHaveBeenCalledWith(candidates[1], expect.any(AbortSignal)))
    await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument())
  })

  it('does not search or submit composition text before the IME selection is complete', async () => {
    const { props } = start()
    const input = screen.getByRole('combobox')
    fireEvent.compositionStart(input)
    fireEvent.change(input, { target: { value: '中国' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true, keyCode: 229 })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 270)) })
    expect(props.onSearch).not.toHaveBeenCalled()
    expect(props.onSave).not.toHaveBeenCalled()
    fireEvent.compositionEnd(input, { data: '中国' })
    await screen.findByRole('option', { name: '中国平安 601318.SH' })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(props.onSave).toHaveBeenCalledTimes(1))
  })

  it('ignores stale searches and aborts a saving selection when Escape cancels', async () => {
    const first = deferred<StockSearchResult>()
    const saving = deferred<void>()
    const search = vi.fn().mockReturnValueOnce(first.promise)
      .mockResolvedValue({ items: [candidates[1]], hasMore: false })
    const save = vi.fn().mockReturnValue(saving.promise)
    start({ onSearch: search, onSave: save })
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '中国平' } })
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    fireEvent.change(input, { target: { value: '中国银' } })
    await screen.findByRole('option', { name: '中国银行 601988.SH' })
    await act(async () => first.resolve({ items: [candidates[0]], hasMore: false }))
    expect(screen.queryByRole('option', { name: /中国平安/ })).not.toBeInTheDocument()
    expect(search.mock.calls[0]?.[1].aborted).toBe(true)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('正在保存股票，原策略将保留')).toBeVisible()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(save.mock.calls[0]?.[1].aborted).toBe(true)
    await act(async () => saving.resolve())
    expect(screen.getByRole('button', { name: /修改股票：东方财富/ })).toBeVisible()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('keeps the original stock and typed query across lookup/save failures and permits retry', async () => {
    const search = vi.fn().mockRejectedValueOnce(new Error('搜索服务暂不可用'))
      .mockResolvedValue({ items: candidates, hasMore: false })
    const save = vi.fn().mockRejectedValueOnce(new Error('草稿保存连接中断')).mockResolvedValue(undefined)
    start({ onSearch: search, onSave: save })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '中国' } })
    expect(await screen.findByRole('alert')).toHaveTextContent('核对股票失败：搜索服务暂不可用')
    expect(screen.getByRole('combobox')).toHaveValue('中国')
    fireEvent.click(screen.getByRole('button', { name: '重新查找' }))
    fireEvent.click(await screen.findByRole('option', { name: '中国平安 601318.SH' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('保存股票失败：草稿保存连接中断')
    expect(screen.getByRole('combobox')).toHaveValue('中国')
    expect(screen.getByRole('combobox')).not.toHaveAttribute('readonly')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2))
  })

  it('keeps the field read-only during an existing run', () => {
    const { props } = start({ disabled: true })
    expect(screen.getByRole('button', { name: /修改股票/ })).toBeDisabled()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(props.onSearch).not.toHaveBeenCalled()
  })
})
