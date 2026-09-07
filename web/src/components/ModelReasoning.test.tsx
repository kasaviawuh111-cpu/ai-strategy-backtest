import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ModelReasoning } from './ModelReasoning'

describe('provider reasoning display', () => {
  it('preview recovery stays truthful and keeps resume available when collapsed', async () => {
    const resume = vi.fn()
    const retrying = {
      status: 'retrying' as const, attempt: 1,
      message: '结果查询暂时中断，正在恢复连接；不会重复提交。',
    }
    const paused = {
      status: 'paused' as const, attempt: 3,
      message: '暂时无法取得结果，后台任务可能仍在处理。', resume,
    }
    const { container, rerender } = render(<ModelReasoning events={[]} active recovery={retrying} />)
    expect(screen.getByText(retrying.message)).toBeVisible()
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('正在重新连接')
    expect(screen.queryByText('已完成')).not.toBeInTheDocument()
    expect(screen.queryByText('正在分析')).not.toBeInTheDocument()
    expect(container.querySelector('.dots')).toBeNull()

    rerender(<ModelReasoning events={[]} active recovery={paused} />)
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('等待恢复连接')
    expect(container.querySelector('.dots')).toBeNull()
    fireEvent.click(screen.getByText('处理过程'))
    await waitFor(() => expect(container.querySelector('details')).not.toHaveAttribute('open'))
    expect(screen.getByRole('button', { name: '继续查询结果' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '继续查询结果' }))
    expect(resume).toHaveBeenCalledTimes(1)

    rerender(<ModelReasoning events={[{
      stage: 'model', message: '已收到的阶段文字', elapsedMs: 10,
    }]} failed />)
    expect(screen.getByText('未完成')).toBeVisible()
    expect(screen.queryByText('已完成')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续查询结果' })).not.toBeInTheDocument()
  })

  it('updates received text in place, keeps user collapse, and escapes markup', () => {
    const { rerender, container } = render(<ModelReasoning events={[]} active />)
    expect(container.querySelector('details')).toHaveAttribute('open')
    const initialStatus = screen.getByRole('status', { name: '处理进度' })
    expect(initialStatus).toHaveTextContent('正在理解你的想法')
    expect(initialStatus).toBeVisible()
    expect(screen.getByLabelText('处理过程内容')).toContainElement(initialStatus)
    expect(initialStatus.closest('summary')).toBeNull()
    expect(screen.getAllByText('正在理解你的想法')).toHaveLength(1)
    expect(screen.queryByText(/暂无|没有返回/)).not.toBeInTheDocument()
    const event = { stage: 'model_reasoning', message: '收到推理流', elapsedMs: 100,
      reasoning: '真实接口片段 <script>alert(1)</script>' }
    rerender(<ModelReasoning events={[event]} active />)
    expect(screen.getByText(event.reasoning)).toBeVisible()
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('正在分析')
    expect(screen.getByRole('status').closest('summary')).not.toBeNull()
    expect(screen.getByLabelText('处理过程内容')).not.toHaveTextContent('正在分析')
    expect(screen.queryByText('正在理解你的想法')).not.toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    fireEvent.click(screen.getByText('处理过程'))
    rerender(<ModelReasoning events={[{ ...event, reasoning: `${event.reasoning}，下一段` }]} active />)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
    expect(screen.getByText(/下一段/)).not.toBeVisible()
  })

  it('moves the single live stage from the empty body to the summary when collapsed', async () => {
    const model = { stage: 'model', message: '模型处理中', elapsedMs: 10 }
    const search = { stage: 'web_search', message: '检索公开资料', elapsedMs: 20 }
    const { rerender, container } = render(<ModelReasoning events={[model]} active />)
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent(model.message)
    expect(screen.getByRole('status').closest('summary')).toBeNull()
    expect(screen.getAllByText(model.message)).toHaveLength(1)

    rerender(<ModelReasoning events={[model, search]} active />)
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent(search.message)
    expect(screen.getByLabelText('处理过程内容')).toContainElement(screen.getByRole('status'))
    expect(screen.getAllByText(search.message)).toHaveLength(1)
    expect(screen.queryByText('正在分析')).not.toBeInTheDocument()
    expect(screen.queryByText(/暂无|没有返回/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('处理过程'))
    await waitFor(() => {
      expect(container.querySelector('details')).not.toHaveAttribute('open')
      expect(screen.getByRole('status').closest('summary')).not.toBeNull()
    })
    expect(screen.getByRole('status')).toHaveTextContent(search.message)
    expect(screen.getByRole('status')).toBeVisible()
    expect(screen.getByLabelText('处理过程内容')).not.toBeVisible()
  })

  it('collapses on completion and lets users reopen the received status history', () => {
    const event = { stage: 'model', message: '模型处理中', elapsedMs: 10 }
    const { rerender, container } = render(<ModelReasoning events={[event]} active />)
    rerender(<ModelReasoning events={[event]} />)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
    expect(screen.getByText('已完成')).toBeVisible()
    expect(screen.getByLabelText('处理过程内容')).not.toBeVisible()
    expect(screen.queryByText(/暂无|没有返回/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('处理过程'))
    expect(screen.getByLabelText('处理过程内容')).toBeVisible()
    expect(screen.getByLabelText('处理过程内容')).toHaveTextContent(event.message)
    expect(screen.queryByText(/暂无|没有返回/)).not.toBeInTheDocument()
  })

  it('collapses when finished and starts the next request open', () => {
    const event = { stage: 'model_reasoning', message: '收到推理流', elapsedMs: 100,
      reasoning: '接口返回内容' }
    const { rerender, container } = render(<ModelReasoning events={[event]} active />)
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('正在分析')
    rerender(<ModelReasoning events={[event]} />)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
    expect(screen.getByText('已完成')).toBeVisible()
    fireEvent.click(screen.getByText('处理过程'))
    expect(screen.getByText(event.reasoning)).toBeVisible()
    rerender(<ModelReasoning events={[]} active />)
    expect(container.querySelector('details')).toHaveAttribute('open')
    expect(screen.getAllByText('处理过程')).toHaveLength(1)
    expect(screen.queryByText('模型思考')).not.toBeInTheDocument()
  })

  it('keeps reasoning and the latest real phrase in one scroll region without an outside list', () => {
    const reasoning = { stage: 'model_reasoning', message: '收到推理流', elapsedMs: 10,
      reasoning: '接口实际返回的文本' }
    const pairing = { stage: 'stock_strategy_pairing', message: '正在把股票与策略匹配成可选方案。', elapsedMs: 20 }
    const { container, rerender } = render(<ModelReasoning events={[reasoning]} active />)
    rerender(<ModelReasoning events={[reasoning, pairing]} active />)
    const box = screen.getByLabelText('处理过程内容')
    expect(box).toContainElement(screen.getByText(reasoning.reasoning))
    expect(box).toContainElement(screen.getByRole('status'))
    expect(screen.getByRole('status')).toHaveTextContent(pairing.message)
    expect(screen.getAllByText(pairing.message)).toHaveLength(1)
    expect(container.querySelector('.model-reasoning__events')).toBeNull()
    expect(screen.queryByText('正在匹配方案')).not.toBeInTheDocument()
  })

  it('follows new status phrases unless the user has scrolled up', () => {
    const first = { stage: 'web_search', message: '正在检索资料', elapsedMs: 10 }
    const { rerender } = render(<ModelReasoning events={[first]} active />)
    const box = screen.getByLabelText('处理过程内容')
    Object.defineProperties(box, {
      scrollHeight: { configurable: true, value: 600 },
      clientHeight: { configurable: true, value: 160 },
    })
    const second = { stage: 'stock_screening', message: '正在筛选符合条件的股票', elapsedMs: 20 }
    rerender(<ModelReasoning events={[first, second]} active />)
    expect(box.scrollTop).toBe(600)
    fireEvent.scroll(box, { target: { scrollTop: 40 } })
    const third = { stage: 'stock_data_enrichment', message: '正在补查候选股票的成交额', elapsedMs: 30 }
    rerender(<ModelReasoning events={[first, second, third]} active />)
    expect(box.scrollTop).toBe(40)
    fireEvent.scroll(box, { target: { scrollTop: 440 } })
    rerender(<ModelReasoning events={[first, second, third, { ...third, message: '补查已经完成', elapsedMs: 40 }]} active />)
    expect(box.scrollTop).toBe(600)
  })
})
