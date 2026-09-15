import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StrategyExamples } from './StrategyExamples'
import { DEFAULT_STRATEGY_EXAMPLES } from '../shared/default-strategy-examples'

afterEach(() => vi.unstubAllGlobals())

describe('scrolling strategy ideas', () => {
  it('moves both lanes forward and wraps without reversing at the seam', () => {
    let tick: FrameRequestCallback = () => {}
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { tick = callback; return 1 })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const { container } = render(<StrategyExamples inputHasText={false} onChoose={vi.fn()} />)
    const lanes = [...container.querySelectorAll<HTMLElement>('.home-example-lane')]
    for (const lane of lanes) {
      const groups = lane.querySelectorAll('.home-example-group')
      Object.defineProperty(groups[0], 'offsetLeft', { value: 0 })
      Object.defineProperty(groups[1], 'offsetLeft', { value: 100 })
    }
    tick(100)
    tick(150)
    expect(lanes.map(lane => lane.scrollLeft)).toEqual([0.5, 0.5])
    for (let time = 200; time <= 10100; time += 50) tick(time)
    expect(lanes.map(lane => lane.scrollLeft)).toEqual([0, 0])
    tick(10150)
    expect(lanes.map(lane => lane.scrollLeft)).toEqual([0.5, 0.5])
  })

  it('has one accessible button per example and fills the exact selected sentence', async () => {
    const choose = vi.fn()
    const { container } = render(<StrategyExamples inputHasText={false} onChoose={choose} />)
    expect(container.querySelectorAll('.home-example-lane')).toHaveLength(2)
    expect(screen.getAllByRole('button')).toHaveLength(DEFAULT_STRATEGY_EXAMPLES.length)
    const first = DEFAULT_STRATEGY_EXAMPLES[0]!
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: first.utterance }))
    expect(choose).toHaveBeenCalledExactlyOnceWith(first)
    for (const item of DEFAULT_STRATEGY_EXAMPLES) {
      expect(screen.getAllByRole('button', { name: item.utterance })).toHaveLength(1)
    }
  })

  it('pauses during hover, keyboard focus, touch and typed input, then resumes automatically', () => {
    const { container, rerender } = render(<StrategyExamples inputHasText={false} onChoose={vi.fn()} />)
    const region = screen.getByLabelText('策略示例')
    const lanes = container.querySelector('.home-example-lanes')!
    expect(region).toHaveAttribute('data-paused', 'false')
    fireEvent.mouseEnter(lanes)
    expect(region).toHaveAttribute('data-paused', 'true')
    fireEvent.mouseLeave(lanes)
    const first = within(region).getByRole('button', { name: DEFAULT_STRATEGY_EXAMPLES[0]!.utterance })
    fireEvent.focus(first)
    expect(region).toHaveAttribute('data-paused', 'true')
    fireEvent.blur(first, { relatedTarget: document.body })
    expect(region).toHaveAttribute('data-paused', 'false')
    fireEvent.touchStart(lanes)
    expect(region).toHaveAttribute('data-paused', 'true')
    fireEvent.touchEnd(lanes)
    expect(region).toHaveAttribute('data-paused', 'false')
    fireEvent.touchStart(lanes)
    expect(region).toHaveAttribute('data-paused', 'true')
    fireEvent.touchCancel(lanes)
    expect(region).toHaveAttribute('data-paused', 'false')
    rerender(<StrategyExamples inputHasText onChoose={vi.fn()} />)
    expect(region).toHaveAttribute('data-paused', 'true')
    rerender(<StrategyExamples inputHasText={false} onChoose={vi.fn()} />)
    expect(region).toHaveAttribute('data-paused', 'false')
    expect(screen.queryByText(/试试这些想法|暂停滚动|继续滚动/)).not.toBeInTheDocument()
  })

  it('keeps the focused example reachable inside its horizontal lane', () => {
    const scrollIntoView = vi.fn()
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
    const { container } = render(<StrategyExamples inputHasText={false} onChoose={vi.fn()} />)
    const region = screen.getByLabelText('策略示例')
    const second = within(region).getByRole('button', { name: DEFAULT_STRATEGY_EXAMPLES[1]!.utterance })
    fireEvent.focus(second)
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', inline: 'nearest' })
    expect(container.querySelector('.home-example-lanes')).toContainElement(second)
    if (descriptor) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', descriptor)
    else delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
  })

  it('does not start motion when the browser requests reduced motion', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    const animation = vi.fn()
    vi.stubGlobal('requestAnimationFrame', animation)
    render(<StrategyExamples inputHasText={false} onChoose={vi.fn()} />)
    expect(screen.getByLabelText('策略示例')).toHaveAttribute('data-reduced-motion', 'true')
    expect(animation).not.toHaveBeenCalled()
    expect(screen.getAllByRole('button')).toHaveLength(DEFAULT_STRATEGY_EXAMPLES.length)
    expect(screen.getByRole('button', { name: DEFAULT_STRATEGY_EXAMPLES[0]!.utterance })).toBeEnabled()
  })
})
