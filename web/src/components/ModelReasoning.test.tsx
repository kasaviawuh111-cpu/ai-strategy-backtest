import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ModelReasoning } from './ModelReasoning'

describe('provider reasoning display', () => {
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
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('正在分析')
    expect(screen.getByRole('status').closest('summary')).toBeNull()
    expect(screen.getAllByText('正在分析')).toHaveLength(1)

    rerender(<ModelReasoning events={[model, search]} active />)
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('正在检索')
    expect(screen.getByLabelText('处理过程内容')).toContainElement(screen.getByRole('status'))
    expect(screen.getAllByText('正在检索')).toHaveLength(1)
    expect(screen.queryByText('正在分析')).not.toBeInTheDocument()
    expect(screen.queryByText(/暂无|没有返回/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('处理过程'))
    await waitFor(() => {
      expect(container.querySelector('details')).not.toHaveAttribute('open')
      expect(screen.getByRole('status').closest('summary')).not.toBeNull()
    })
    expect(screen.getByRole('status')).toHaveTextContent('正在检索')
    expect(screen.getByRole('status')).toBeVisible()
    expect(screen.getAllByText('正在检索')).toHaveLength(1)
  })

  it('does not keep an empty content box or placeholder when finished without text', () => {
    const event = { stage: 'model', message: '模型处理中', elapsedMs: 10 }
    const { rerender, container } = render(<ModelReasoning events={[event]} active />)
    rerender(<ModelReasoning events={[event]} />)
    expect(container.querySelector('details')).not.toHaveAttribute('open')
    expect(screen.getByText('已完成')).toBeVisible()
    expect(screen.queryByLabelText('处理过程内容')).not.toBeInTheDocument()
    expect(screen.queryByText(/暂无|没有返回/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('处理过程'))
    expect(screen.queryByLabelText('处理过程内容')).not.toBeInTheDocument()
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
})
