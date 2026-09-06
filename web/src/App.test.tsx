import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, vi } from 'vitest'

import App from './App'
import { backtestApi, instrumentApi, strategyApi, systemApi, type DialogueProgressObserver } from './shared/api/client'
import { enableImmediateMockWaitForTests, mockApi, resetMockWaitForTests } from './shared/api/mock'
import { DEFAULT_STRATEGY_EXAMPLES } from './shared/default-strategy-examples'
import { ApiError, type BacktestOptimizationCandidate, type BacktestReviewResponse, type BacktestRun, type CapabilitiesResponse, type Instrument } from './shared/api/types'
import { settleMockRunOnFirstPoll } from './test/mock-run'

const renderApp = (
  instrument?: Instrument,
  options?: {
    instrumentContextSource?: 'stock_page' | 'standalone_default'
    instrumentContextError?: string
    onReturnToStockPage?: () => void
    onUseStandaloneExample?: () => void
  },
) => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <App instrument={instrument} {...options} />
      </QueryClientProvider>,
    ),
  }
}

const expectMockRuntimeMarker = () => {
  expect(document.querySelector('.app')).toHaveAttribute('data-api-mode', 'mock')
}

const HOME_CAPITAL_PATTERN = /(?:本金|初始资金|起始本金|100\s*万|1,000,000|1000000)/
const VOLUME_EXAMPLE = '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出'
const MOVING_AVERAGE_EXAMPLE = '东方财富收盘价上穿20日均线买入，下穿20日均线卖出'

const expectHomeToHideDefaultCapital = (container: HTMLElement) => {
  const home = container.querySelector<HTMLElement>('#pg-chat')
  if (!home) throw new Error('missing #pg-chat')

  const formValues = Array.from(
    home.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      'input, textarea, select',
    ),
    (control) => control.value,
  )
  const visibleSurface = [home.textContent ?? '', ...formValues].join('\n')

  expect(home).toBeVisible()
  expect(visibleSurface).not.toMatch(HOME_CAPITAL_PATTERN)
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  resetMockWaitForTests()
})

describe('formal main.tsx App journey', () => {
  it('B28 saves only the selected stock while preserving current edits and the completed report', async () => {
    // Component wiring fixtures; provider and live backtest acceptance are separate.
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const selected: Instrument = { name: '中国平安', symbol: '601318.SH', market: 'CN_A', exchange: 'SSE' }
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValue(fixture)
    vi.spyOn(instrumentApi, 'search').mockResolvedValue({ items: [selected], hasMore: true })
    const revise = vi.spyOn(strategyApi, 'revise').mockImplementation(async edited => ({
      ...edited, revision: edited.revision + 1,
    }))
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    const { container } = renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: /成交设置/ }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始资金' }), { target: { value: '500000' } })
    fireEvent.change(screen.getByLabelText(/单边滑点/), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText(/佣金率/), { target: { value: '0.02' } })
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2022-01-04' } })
    const condition = fixture.draft.entry.conditions.find(item => item.kind === 'indicator')
    if (!condition || !condition.parameters[0]) throw new Error('expected an editable parameter')
    const parameter = screen.getByRole('spinbutton', {
      name: `${condition.label} ${condition.parameters[0].label}`,
    })
    fireEvent.change(parameter, { target: { value: '25' } })
    await user.click(screen.getByRole('button', { name: '完成' }))
    expect(revise).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /修改股票：东方财富/ }))
    expect(screen.getByRole('button', { name: '开始回测' })).toBeDisabled()
    fireEvent.change(screen.getByRole('combobox', { name: '股票名称或代码' }), { target: { value: '中国' } })
    await user.click(await screen.findByRole('option', { name: '中国平安 601318.SH' }))
    await screen.findByRole('button', { name: /修改股票：中国平安/ })
    expect(revise).toHaveBeenCalledTimes(1)
    const edited = revise.mock.calls[0]?.[0]
    if (!edited) throw new Error('expected the current draft to be saved')
    expect(edited.instrument).toEqual(selected)
    expect(edited.strategySpec.instrument.symbol).toBe(selected.symbol)
    expect(edited.exit).toEqual(fixture.draft.exit)
    expect(edited.backtest).toEqual({ ...fixture.draft.backtest, start: '2022-01-04', initialCashCny: 500000 })
    expect(edited.execution).toEqual({ ...fixture.draft.execution, slippageBps: 7, commissionRate: 0.0002 })
    const editedCondition = edited.entry.conditions.find(item => item.kind === 'indicator')
    expect(editedCondition?.parameters[0]?.value).toBe(25)
    expect(create).toHaveBeenCalledTimes(1)
    expect(compile).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('form', { name: '编辑回测条件' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('交易规则')).not.toHaveFocus()
    const historyCard = container.querySelector('.stream .mcard.is-settled')
    expect(historyCard).toHaveTextContent('东方财富')
    expect(historyCard?.querySelector('.inline-stock-entry')).toBeNull()
    await user.click(screen.getByRole('button', { name: '查看这次报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeVisible()
  }, 10_000)

  it('B28 ignores a late stock save after the conversation is reset', async () => {
    enableImmediateMockWaitForTests()
    const selected: Instrument = { name: '中国银行', symbol: '601988.SH', market: 'CN_A', exchange: 'SSE' }
    vi.spyOn(instrumentApi, 'search').mockResolvedValue({ items: [selected], hasMore: false })
    let finish!: () => void
    const revise = vi.spyOn(strategyApi, 'revise').mockImplementation(edited => new Promise(resolve => {
      finish = () => resolve({ ...edited, revision: edited.revision + 1 })
    }))
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: /修改股票/ }))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '中国' } })
    await user.click(await screen.findByRole('option', { name: '中国银行 601988.SH' }))
    await user.click(screen.getByRole('button', { name: '新建会话并清空上下文', hidden: true }))
    expect(revise.mock.calls[0]?.[2]?.aborted).toBe(true)
    await act(async () => finish())
    expect(screen.queryByRole('button', { name: /修改股票/ })).not.toBeInTheDocument()
    expect(screen.queryByText('中国银行')).not.toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('B13 shows the current ready reply once and does not reuse it for a legacy ready response', async () => {
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const reply = '东方财富的放量突破规则已经准备好了，你可以核对后开始回测。'
    vi.spyOn(strategyApi, 'compile')
      .mockImplementationOnce(async (input) => {
        input.dialogueProgress?.onProgress([
          { stage: 'model', message: '已调用策略生成模型', elapsedMs: 1_200 },
          { stage: 'complete', message: '策略已准备好', elapsedMs: 2_450 },
        ])
        return { ...fixture, assistantMessage: reply }
      })
      .mockResolvedValueOnce({ ...fixture, draft: { ...fixture.draft, id: 'legacy-ready' } })
    const user = userEvent.setup()
    renderApp()
    const input = screen.getByLabelText('交易规则')
    fireEvent.change(input, { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText(reply)).toBeVisible()
    expect(screen.getAllByText(reply)).toHaveLength(1)
    expect(screen.getByText('预览策略')).toBeVisible()
    const readyTurn = screen.getByText('预览策略').closest('.turn')
    const process = readyTurn?.querySelector('.model-reasoning')
    expect(readyTurn).toContainElement(screen.getByText(reply))
    expect(process).toBeInTheDocument()
    expect(process!.compareDocumentPosition(screen.getByText(reply))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText(reply).compareDocumentPosition(screen.getByText('预览策略'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText(/已经把这句话整理成买卖规则/)).not.toBeInTheDocument()

    fireEvent.change(input, { target: { value: MOVING_AVERAGE_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeVisible()
    expect(screen.queryByText(reply)).not.toBeInTheDocument()
    expect(screen.queryByText(/已经把这句话整理成买卖规则/)).not.toBeInTheDocument()
  })

  it('B13 archives each ready reply with its own completed version and clears them for a new conversation', async () => {
    // Component state/snapshot regression only; real model acceptance is separate.
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    const { container } = renderApp()
    const replies = ['这一版用放量突破入场。', '这一版改成均线确认入场。']
    for (const [index, reply] of replies.entries()) {
      compile.mockResolvedValueOnce({ ...fixture, assistantMessage: reply,
        draft: { ...fixture.draft, id: `ready-reply-${index}` } })
      fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
      await user.click(screen.getByRole('button', { name: '识别交易规则' }))
      expect(await screen.findByText(reply)).toBeVisible()
      await user.click(screen.getByRole('button', { name: '开始回测' }))
      await screen.findByRole('heading', { name: '回测报告' })
      await user.click(screen.getByRole('button', { name: '回到对话' }))
      await user.click(screen.getByRole('button', { name: '收起策略审阅' }))
    }

    compile.mockResolvedValueOnce({ ...fixture, draft: { ...fixture.draft, id: 'ready-no-reply' } })
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: MOVING_AVERAGE_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    const archivedReplies = Array.from(container.querySelectorAll('.stream > [id^="journey-"]'),
      turn => turn.nextElementSibling?.textContent)
    expect(archivedReplies).toEqual(replies)
    for (const reply of replies) expect(screen.getAllByText(reply)).toHaveLength(1)
    expect(screen.queryByText(/已经把这句话整理成买卖规则/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '新建会话并清空上下文', hidden: true }))
    for (const reply of replies) expect(screen.queryByText(reply)).not.toBeInTheDocument()
  }, 12_000)

  it('B13 preserves the whole archived prompt and keeps replies while editing an unrun draft', async () => {
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const prompt = '你想用哪只股票？\n选一个试试，或说说你想怎么改。'
    const reply = '股票已确认，刚才的买卖条件都保留了。'
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification', draftId: 'whole-prompt', revision: 1,
      assistantMessage: prompt,
      clarification: { id: 'instrument_required', question: prompt, reason: '', choices: [] },
    })
    vi.spyOn(strategyApi, 'answerClarification').mockImplementationOnce(async (input) => {
      input.dialogueProgress?.onProgress([
        { stage: 'complete', message: '股票已确认', elapsedMs: 1_000 },
      ])
      return { replyKind: 'accepted', assistantMessage: reply, suggestions: [], outcome: fixture }
    })
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    const input = screen.getByLabelText('交易规则')
    fireEvent.change(input, { target: { value: '放量突破买入，跌破20日线卖出' } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText(/你想用哪只股票/)).toHaveTextContent('选一个试试，或说说你想怎么改。')
    fireEvent.change(input, { target: { value: '东方财富' } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    expect(screen.getByText(/你想用哪只股票/).textContent).toBe(prompt)

    await user.click(screen.getByRole('button', { name: /区间/ }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始资金' }), { target: { value: '500000' } })
    await user.click(screen.getByRole('button', { name: '完成' }))
    expect(screen.getAllByText(reply)).toHaveLength(1)
    expect(screen.getByText(/你想用哪只股票/).textContent).toBe(prompt)
    expect(screen.getByText('预览策略').closest('.turn')).toContainElement(screen.getByText(reply))
    expect(screen.getByText('放量突破买入，跌破20日线卖出')).toBeVisible()
    expect(screen.getByText('预览策略').closest('.turn')?.querySelector('.model-reasoning')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('B13 leaves a completed reply only in history after editing its settings', async () => {
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const reply = '这一版按原本金设置回测放量突破。'
    vi.spyOn(strategyApi, 'compile').mockImplementationOnce(async (input) => {
      input.dialogueProgress?.onProgress([
        { stage: 'complete', message: '策略已准备好', elapsedMs: 1_000 },
      ])
      return { ...fixture, assistantMessage: reply }
    })
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    const { container } = renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: /区间/ }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始资金' }), { target: { value: '500000' } })
    await user.click(screen.getByRole('button', { name: '完成' }))

    expect(screen.getAllByText(reply)).toHaveLength(1)
    expect(container.querySelector('.stream > [id^="journey-"]')?.nextElementSibling)
      .toHaveTextContent(reply)
    expect(screen.getByText('预览策略').closest('.turn')).not.toContainElement(screen.getByText(reply))
    expect(screen.getAllByText(VOLUME_EXAMPLE)).toHaveLength(1)
    expect(container.querySelector('.stream .model-reasoning')).not.toBeInTheDocument()
    expect(screen.queryByText(/想怎么交易？|说出新的买卖规则，继续回测/)).not.toBeInTheDocument()
    expect(Array.from(container.querySelectorAll('.stream .bubble'))
      .every(bubble => Boolean(bubble.textContent?.trim()))).toBe(true)
    await user.click(within(screen.getByLabelText('策略审阅')).getByRole('button', { name: /区间/ }))
    expect(screen.getByRole('spinbutton', { name: '初始资金' })).toHaveValue(500000)
    await user.click(screen.getByRole('button', { name: '完成' }))
    await user.click(within(screen.getByLabelText('策略审阅')).getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    expect(create).toHaveBeenCalledTimes(2)
    expect(create.mock.calls[1]?.[0].backtest.initialCashCny).toBe(500000)
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '新建策略', hidden: true }))
    expect(screen.getAllByText(VOLUME_EXAMPLE)).toHaveLength(1)
    const reports = screen.getAllByRole('button', { name: '查看这次报告' })
    expect(reports).toHaveLength(2)
    await user.click(reports[1]!)
    expect(await screen.findByRole('heading', { name: '回测报告' })).toBeVisible()
  }, 12_000)

  it('B26 locks compilation and keeps a missing-parent edit without silently restoring or clearing it', async () => {
    const fixture = await mockApi.compile({
      utterance: MOVING_AVERAGE_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
    })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    let rejectCompile: ((error: ApiError) => void) | undefined
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce(fixture)
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectCompile = reject }))
      .mockResolvedValueOnce(fixture)
    const revise = vi.spyOn(strategyApi, 'revise')
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    const input = screen.getByLabelText('交易规则')
    fireEvent.change(input, { target: { value: MOVING_AVERAGE_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    const edit = '股票换成中金公司，其他不变，先别跑'
    fireEvent.change(input, { target: { value: edit } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(input).toBeDisabled()
    expect(screen.getByRole('button', { name: '新建策略', hidden: true })).toBeDisabled()
    const newConversation = screen.getByRole('button', { name: '新建会话并清空上下文', hidden: true })
    expect(newConversation).toBeDisabled()
    act(() => rejectCompile?.(new ApiError({
      type: 'about:blank', title: 'Not found', status: 404,
      detail: 'Conversation parent draft was not found', code: 'conversation_parent_draft_not_found',
    })))
    expect(await screen.findByText(/这轮策略的服务端记录已无法读取/)).toBeVisible()
    expect(input).toBeEnabled()
    expect(input).toHaveValue(edit)
    expect(screen.queryByText(/Conversation parent draft was not found/)).not.toBeInTheDocument()
    expect(compile).toHaveBeenCalledTimes(2)
    expect(compile.mock.calls[1]?.[1]).toBe(fixture.draft.id)
    expect(revise).not.toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
    // Only the user's explicit new conversation discards the old lineage.
    await user.click(newConversation)
    fireEvent.change(input, { target: { value: MOVING_AVERAGE_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(3))
    expect(compile.mock.calls[2]?.[1]).toBeUndefined()
    expect(create).not.toHaveBeenCalled()
  })

  it('B26 preserves the readable proposal selection when its clarification draft is missing', async () => {
    const proposal = {
      id: 'internal-proposal-2', title: '均线确认', instrument_symbol: '600519.SH',
      instrument_name: '贵州茅台', pairing_reason: '组件接线示例', hypothesis: '',
      entry_summary: '上穿20日均线', exit_summary: '下穿20日均线',
      suggested_utterance: '贵州茅台上穿20日均线买入，下穿20日均线卖出',
      capability_ids: [], assumptions: [], confidence: 1,
    }
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification', draftId: 'lost-clarification', revision: 2,
      clarification: {
        id: 'idea_guidance_required', question: '选一组试试。', reason: '', choices: [],
        ideaRoute: {
          schema_version: 'idea-route.v1', understanding: '', hypothesis: '',
          asset_mapping: { instrument_symbol: null, relation: 'unbound', rationale: '',
            evidence_status: 'instrument_required' }, proposals: [proposal],
        },
      },
    })
    const answer = vi.spyOn(strategyApi, 'answerClarification').mockRejectedValueOnce(new ApiError({
      type: 'about:blank', title: 'Not found', status: 404,
      detail: 'Strategy draft was not found', code: 'strategy_draft_not_found',
    }))
    const revise = vi.spyOn(strategyApi, 'revise')
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: '我想试试趋势策略' } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: '贵州茅台 · 均线确认' }))
    expect(await screen.findByText(/这轮策略的服务端记录已无法读取/)).toBeVisible()
    expect(answer).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      draftId: 'lost-clarification', revision: 2, answer: 'internal-proposal-2',
    }))
    expect(screen.getByLabelText('交易规则')).toHaveValue('贵州茅台 · 均线确认')
    expect(screen.queryByRole('button', { name: '贵州茅台 · 均线确认' })).not.toBeInTheDocument()
    expect(screen.queryByText('Strategy draft was not found')).not.toBeInTheDocument()
    expect(compile).toHaveBeenCalledTimes(1)
    expect(revise).not.toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
  })

  it('B26 preserves edited panel values on missing and stale revisions without creating a run', async () => {
    enableImmediateMockWaitForTests()
    const revise = vi.spyOn(strategyApi, 'revise')
      .mockRejectedValueOnce(new ApiError({ type: 'about:blank', title: 'Not found', status: 404,
        detail: 'Strategy draft was not found', code: 'strategy_draft_not_found' }))
      .mockRejectedValueOnce(new ApiError({ type: 'about:blank', title: 'Conflict', status: 409,
        detail: 'Strategy draft revision is stale', code: 'strategy_draft_revision_stale' }))
    const create = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: MOVING_AVERAGE_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: /区间/ }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始资金' }), { target: { value: '500000' } })
    await user.click(screen.getByRole('button', { name: '完成' }))
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText(/这轮策略的服务端记录已无法读取/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText(/这轮策略版本已更新/)).toBeVisible()
    expect(screen.queryByText(/Strategy draft/)).not.toBeInTheDocument()
    expect(revise).toHaveBeenCalledTimes(2)
    expect(revise.mock.calls[0]?.[0].backtest.initialCashCny).toBe(500000)
    expect(revise.mock.calls[1]?.[0]).toEqual(revise.mock.calls[0]?.[0])
    expect(revise.mock.calls.every(call => call[1] !== true)).toBe(true)
    expect(create).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /区间/ }))
    expect(screen.getByRole('spinbutton', { name: '初始资金' })).toHaveValue(500000)
  }, 8_000)

  it('B15 carries exposed review versions through follow-ups and the next completed run', async () => {
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const candidateFor = (id: 'model-opt-1' | 'model-opt-2'): BacktestOptimizationCandidate => ({
      id, title: id, diagnosis: '组件候选', changeDimension: 'confirmation', expectedEffect: '组件接线测试',
      tradeoff: '组件接线测试', suggestedUtterance: VOLUME_EXAMPLE, strategy: fixture.draft.strategySpec,
      strategyHash: `sha256:${id}`, modelSuggested: true,
    })
    const reviewFor = (runId: string, version: string): BacktestReviewResponse => ({
      runId, sourceResultHash: 'sha256:fixture-result', generatedAt: '2026-09-05T12:00:00Z',
      evidenceGrade: 'limited', evidenceReasons: ['组件接线测试'], analysis: `分析版本${version}。`,
      conclusion: '等待用户选择。',
      optimizationCandidates: [candidateFor('model-opt-1'), candidateFor('model-opt-2')],
      disclaimer: '历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令',
      modelProvenance: { provider: 'test', model: 'test',
        promptVersion: 'test', schemaVersion: 'backtest-review.v1', responseHash: `sha256:${version}` },
    })
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce(fixture)
    const answer = vi.spyOn(strategyApi, 'answerClarification')
    const create = vi.spyOn(backtestApi, 'create')
    const review = vi.spyOn(backtestApi, 'review').mockImplementation(async runId => reviewFor(runId, 'manual'))
    const user = userEvent.setup()
    renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    await screen.findByText('分析版本manual。')
    const firstRun = await create.mock.results[0]?.value
    expect(review.mock.calls[0]?.[2]).toEqual({ relatedRunIds: [firstRun.id], relatedReviews: [] })

    await user.click(screen.getByRole('button', { name: '回到对话' }))
    compile.mockResolvedValueOnce({ status: 'needs_clarification', draftId: 'more-review', revision: 2,
      assistantMessage: '这里是另一版建议。', clarification: {
        id: 'backtest_review', question: '这里是另一版建议。', reason: '', choices: [],
        backtestReview: reviewFor(firstRun.id, 'followup'),
      } })
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: '换一批优化建议，先别跑' } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('这里是另一版建议。')
    const firstReference = { runId: firstRun.id, responseHash: 'sha256:manual' }
    const secondReference = { runId: firstRun.id, responseHash: 'sha256:followup' }
    expect(compile.mock.calls[1]?.[0]).toMatchObject({
      relatedRunIds: [firstRun.id], relatedReview: firstReference, relatedReviews: [firstReference],
    })
    expect(create).toHaveBeenCalledTimes(1)

    answer.mockResolvedValueOnce({ replyKind: 'accepted', assistantMessage: '按新条件回测。', suggestions: [],
      outcome: { ...fixture, draft: { ...fixture.draft, id: 'reviewed-followup' }, runRequested: true } })
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: '按新条件再跑一次' } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByRole('heading', { name: '回测报告' })
    expect(answer.mock.calls[0]?.[0]).toMatchObject({ relatedRunIds: [firstRun.id],
      relatedReview: secondReference, relatedReviews: [firstReference, secondReference] })
    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    await screen.findByText('分析版本manual。')
    const secondRun = await create.mock.results[1]?.value
    expect(review.mock.calls[1]?.[2]).toEqual({ relatedRunIds: [firstRun.id, secondRun.id],
      relatedReviews: [firstReference, secondReference] })

    await user.click(screen.getByRole('button', { name: '新建会话并清空上下文', hidden: true }))
    compile.mockResolvedValueOnce(fixture)
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    expect(compile.mock.calls[2]?.[0]).toMatchObject({ relatedRunIds: [], relatedReviews: [] })
    expect(compile.mock.calls[2]?.[0].relatedReview).toBeUndefined()
  }, 12_000)

  it('B26 offers the existing analysis retry only for the lost review and clears it after success', async () => {
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({
      utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
    })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const candidate = (id: 'model-opt-1' | 'model-opt-2'): BacktestOptimizationCandidate => ({
      id, title: id, diagnosis: '组件候选', changeDimension: 'confirmation', expectedEffect: '组件接线测试',
      tradeoff: '组件接线测试', suggestedUtterance: VOLUME_EXAMPLE, strategy: fixture.draft.strategySpec,
      strategyHash: `sha256:${id}`, modelSuggested: true,
    })
    const compile = vi.spyOn(strategyApi, 'compile')
    const create = vi.spyOn(backtestApi, 'create')
    const optimize = vi.spyOn(backtestApi, 'createOptimization')
    const review = vi.spyOn(backtestApi, 'review').mockImplementation(async (runId): Promise<BacktestReviewResponse> => ({
      runId, sourceResultHash: 'sha256:fixture-result', generatedAt: '2026-09-05T12:00:00Z',
      evidenceGrade: 'limited', evidenceReasons: ['组件接线测试'], analysis: '组件中的分析结果。',
      conclusion: '组件中的优化结论。', optimizationCandidates: [candidate('model-opt-1'), candidate('model-opt-2')],
      disclaimer: '历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令',
      modelProvenance: { provider: 'test', model: 'test', promptVersion: 'test',
        schemaVersion: 'backtest-review.v1', responseHash: 'sha256:fixture-review' },
    }))
    const user = userEvent.setup()
    renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    await screen.findByText('组件中的分析结果。')
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    compile.mockRejectedValueOnce(new ApiError({ type: 'about:blank', title: 'Conflict', status: 409,
      detail: 'Review context is missing', code: 'backtest_review_context_unavailable' }))
    const edit = '用第二个优化方案再跑一次'
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: edit } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText(/这版优化建议的服务端记录已无法读取/)).toBeVisible()
    expect(screen.getByLabelText('交易规则')).toHaveValue(edit)
    expect(compile.mock.calls[1]?.[0].relatedReview).toEqual({
      runId: review.mock.calls[0]?.[0], responseHash: 'sha256:fixture-review',
    })
    expect(create).toHaveBeenCalledTimes(1)
    expect(optimize).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '查看这次报告' }))
    const report = within(document.querySelector('#pg-report') as HTMLElement)
    expect(report.getByText(/这版优化建议的服务端记录已无法读取/)).toBeVisible()
    await user.click(report.getByRole('button', { name: '重试 AI 分析' }))
    await waitFor(() => expect(review).toHaveBeenCalledTimes(2))
    expect(review.mock.calls[1]?.[0]).toBe(review.mock.calls[0]?.[0])
    await waitFor(() => expect(report.queryByRole('button', { name: '重试 AI 分析' })).not.toBeInTheDocument())
    expect(report.queryByText(/这版优化建议的服务端记录已无法读取/)).not.toBeInTheDocument()
    expect(create).toHaveBeenCalledTimes(1)
  }, 12_000)

  it('blocks a malformed saved date before an automatic backtest can start', async () => {
    const fixture = await mockApi.compile({
      utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
    })
    if (fixture.status !== 'compiled') throw new Error('Expected a component fixture')
    vi.spyOn(strategyApi, 'compile').mockResolvedValue({
      ...fixture, runRequested: true,
      draft: { ...fixture.draft, backtest: { ...fixture.draft.backtest, start: '0686-09-05' } },
    })
    const createRun = vi.spyOn(backtestApi, 'create')
    const revise = vi.spyOn(strategyApi, 'revise')
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByRole('button', { name: '请检查设置' })).toBeDisabled()
    expect(screen.getByText('开始日期不能早于 1990-01-01，请检查年份。')).toBeVisible()
    expect(createRun).not.toHaveBeenCalled()
    expect(revise).not.toHaveBeenCalled()
  })

  it.each(DEFAULT_STRATEGY_EXAMPLES)(
    'shows and submits the same complete rule without appended settings: $instrument.name',
    async (example) => {
      const compile = vi.spyOn(strategyApi, 'compile').mockImplementation(() => new Promise(() => {}))
      const user = userEvent.setup()
      renderApp({ name: '比亚迪', symbol: '002594.SZ', market: 'CN_A', exchange: 'SZSE' }, {
        instrumentContextSource: 'stock_page',
      })

      const examples = within(screen.getByLabelText('策略示例'))
      expect(examples.getAllByRole('button')).toHaveLength(2)
      const exampleButton = examples.getByRole('button', { name: example.utterance })
      expect(exampleButton).toBeVisible()
      expect(exampleButton).not.toHaveTextContent(/本金|回测20\d{2}/)
      await user.click(exampleButton)

      await waitFor(() => expect(compile).toHaveBeenCalledTimes(1))
      expect(compile).toHaveBeenCalledWith(expect.objectContaining({
        utterance: example.utterance,
        instrument: example.instrument,
        instrumentContextSource: 'stock_page',
      }))
      expect(screen.getByText(example.utterance)).toBeVisible()
      expect(example.utterance).not.toMatch(/本金|回测20\d{2}/)
    },
  )

  it.each([false, true])('shows three stock chips after evidence and preserves selection (bound=%s)', async (bound) => {
    const stocks = [
      { symbol: '300394.SZ', name: '天孚通信' },
      { symbol: '688825.SH', name: '长鑫科技' },
      { symbol: '600487.SH', name: '亨通光电' },
    ].map(item => ({ ...item, source: 'eastmoney_mx_screener',
      retrieved_at: '2026-09-05T12:00:00Z', evidence: `${item.name}的真实返回依据位置` }))
    vi.spyOn(strategyApi, 'compile').mockResolvedValue({
      status: 'needs_clarification', draftId: 'three-stocks', revision: 2,
      assistantMessage: '你选的放量突破可以用这三只试试，也可以输入自己的股票。',
      clarification: {
        id: bound ? 'idea_guidance_required' : 'instrument_reuse_confirmation',
        question: '试哪一只？', reason: '', choices: [],
        instrumentSuggestion: stocks[0], instrumentSuggestions: stocks,
        ...(bound ? { ideaRoute: {
          schema_version: 'idea-route.v1' as const, understanding: '', hypothesis: '',
          asset_mapping: { instrument_symbol: null, relation: 'unbound' as const,
            rationale: '', evidence_status: 'instrument_required' as const },
          proposals: stocks.map((item, index) => ({
            id: `stock-${index}`, title: '放量创高突破', instrument_symbol: item.symbol,
            instrument_name: item.name, pairing_reason: item.evidence,
            hypothesis: '界面接线测试', entry_summary: '创20日新高且放量1.5倍',
            exit_summary: '跌破20日均线',
            suggested_utterance: '创20日新高且放量1.5倍买入，跌破20日均线卖出',
            capability_ids: [], assumptions: [], confidence: 1,
          })),
        } } : {}),
      },
    })
    const answer = vi.spyOn(strategyApi, 'answerClarification').mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), '放量创高突破')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    const group = await screen.findByLabelText('可选股票')
    expect(screen.getByLabelText('交易规则')).toHaveAttribute('placeholder', '也可以输入你想用的股票')
    const chips = within(group).getAllByRole('button')
    const [first, , third] = chips
    if (!first || !third) throw new Error('Expected three stock choices')
    expect(chips.map(chip => chip.textContent)).toEqual(stocks.map(item => item.name))
    expect(chips.every(chip => chip.classList.contains('chip'))).toBe(true)
    expect(screen.queryByRole('button', { name: '我自己选股票' })).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: '股票与策略组合' })).not.toBeInTheDocument()
    const evidence = within(group).getByText('东方财富选股依据')
    expect(evidence.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(evidence.closest('details')).not.toHaveAttribute('open')
    await user.click(evidence)
    expect(within(group).getByText(/亨通光电的真实返回依据位置/)).toBeVisible()
    await user.click(third)
    expect(answer).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      draftId: 'three-stocks', revision: 2,
      answer: bound ? 'stock-2' : '用亨通光电（600487.SH）',
    }))
  })

  it('shows the model direction before completion and clears it for the next dialogue request', async () => {
    const firstDirection = '借这份果断劲儿，可以从突破与趋势两个方向开始研究。'
    const nextDirection = '这次更侧重趋势确认，正在保留原来的退出思路。'
    const finalReply = '组合已经准备好了，想试哪一种？'
    const outcome = {
      status: 'needs_clarification' as const, draftId: 'progressive-draft', revision: 1,
      assistantMessage: finalReply,
      clarification: { id: 'idea_guidance_required', question: finalReply, reason: '', choices: [] },
    }
    let firstProgress: DialogueProgressObserver | undefined
    let nextProgress: DialogueProgressObserver | undefined
    let finishCompile: (() => void) | undefined
    let finishAnswer: (() => void) | undefined
    const compile = vi.spyOn(strategyApi, 'compile').mockImplementation((input) => new Promise((resolve) => {
      firstProgress = input.dialogueProgress
      finishCompile = () => resolve(outcome)
    }))
    const answer = vi.spyOn(strategyApi, 'answerClarification').mockImplementation((input) => new Promise((resolve) => {
      nextProgress = input.dialogueProgress
      finishAnswer = () => resolve({
        replyKind: 'clarification', assistantMessage: '调整后的组合已经准备好了。',
        suggestions: [], outcome,
      })
    }))
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), '我是秦始皇')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(compile).toHaveBeenCalledTimes(1)
    act(() => firstProgress?.onProgress([
      { stage: 'strategy_direction', message: firstDirection, elapsedMs: 2_000 },
    ]))
    expect(await screen.findByText(firstDirection)).toBeVisible()
    expect(screen.getAllByText(firstDirection)).toHaveLength(1)
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('正在准备组合')
    expect(screen.queryByText(finalReply)).not.toBeInTheDocument()

    act(() => firstProgress?.onProgress([
      { stage: 'strategy_direction', message: firstDirection, elapsedMs: 2_000 },
      { stage: 'stock_data_enrichment', message: '正在补充候选股票的成交数据。', elapsedMs: 3_000 },
    ]))
    expect(screen.getByText(firstDirection)).toBeVisible()
    expect(screen.getByRole('status', { name: '处理进度' })).toHaveTextContent('继续补查数据')
    act(() => finishCompile?.())
    expect(await screen.findByText(finalReply)).toBeVisible()
    expect(screen.queryByText(firstDirection)).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('交易规则'), '再侧重趋势一点')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(answer).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(firstDirection)).not.toBeInTheDocument()
    act(() => nextProgress?.onProgress([
      { stage: 'strategy_direction', message: nextDirection, elapsedMs: 1_000 },
    ]))
    expect(screen.getByText(nextDirection)).toBeVisible()
    expect(screen.getAllByText(nextDirection)).toHaveLength(1)
    act(() => finishAnswer?.())
    expect(await screen.findByText('调整后的组合已经准备好了。')).toBeVisible()
    expect(screen.queryByText(nextDirection)).not.toBeInTheDocument()
  }, 15_000)

  it.each(['pick-second', 'pick-second-other-symbol', 'typed-own-stock'])(
    'uses complete stock-strategy cards for %s', async (action) => {
    const picksProposal = action !== 'typed-own-stock'
    const proposals = ([
      ['pair-first', '东方财富', '300059.SZ', '突破跟随'],
      ['pair-second', '贵州茅台', '600519.SH', '均线确认'],
      ['pair-third', '比亚迪', '002594.SZ', '量价趋势'],
    ] as const).map(([id, name, symbol, title]) => ({
      id, title, instrument_symbol: symbol, instrument_name: name,
      pairing_reason: `${name}成交活跃，可以尝试${title}。`,
      hypothesis: '界面交互测试', entry_summary: '收盘价创20日新高', exit_summary: '跌破20日均线',
      suggested_utterance: '收盘价创20日新高买入，跌破20日均线卖出',
      capability_ids: [], assumptions: [], confidence: 1,
    }))
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification', draftId: 'paired-draft', revision: 2,
      assistantMessage: '借这份果断劲儿，试试进退明确的交易方向？',
      clarification: {
        id: 'idea_guidance_required', question: '选一个组合。', reason: '', choices: [],
        instrumentSuggestion: {
          symbol: '300059.SZ', name: '东方财富', source: 'eastmoney_mx_screener',
          retrieved_at: '2026-09-05T12:00:00Z', evidence: 'legacy single suggestion',
        },
        ideaRoute: {
          schema_version: 'idea-route.v1', understanding: '人物是创作灵感。', hypothesis: '',
          asset_mapping: {
            instrument_symbol: null, relation: 'unbound', rationale: '',
            evidence_status: 'instrument_required',
          },
          proposals,
        },
      },
    })
    const answer = vi.spyOn(strategyApi, 'answerClarification').mockImplementation(async () => {
      if (!picksProposal) return new Promise(() => {})
      const symbol = action === 'pick-second' ? '600519.SH' : '300059.SZ'
      return {
        replyKind: 'accepted', assistantMessage: '组合已准备好。', suggestions: [],
        outcome: await mockApi.compile({
          instrument: { symbol, name: symbol, market: 'CN_A', exchange: symbol.endsWith('.SH') ? 'SSE' : 'SZSE' },
          utterance: MOVING_AVERAGE_EXAMPLE,
        }),
      }
    })
    const createRun = vi.spyOn(backtestApi, 'create').mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), '我是秦始皇')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const second = await screen.findByRole('button', { name: '贵州茅台 · 均线确认' })
    expect(screen.getByRole('button', { name: '东方财富 · 突破跟随' })).toBeVisible()
    expect(screen.getByRole('button', { name: '比亚迪 · 量价趋势' })).toBeVisible()
    expect(screen.queryByRole('button', { name: '用东方财富' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '我自己选股票' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始回测' })).not.toBeInTheDocument()
    expect(second).toHaveClass('proposal')
    expect(within(second).getByText('收盘价创20日新高')).toBeVisible()
    expect(within(second).getByText('跌破20日均线')).toBeVisible()
    expect(screen.queryByLabelText('贵州茅台 · 均线确认的详情')).not.toBeInTheDocument()
    expect(screen.queryByText('贵州茅台成交活跃，可以尝试均线确认。')).not.toBeInTheDocument()
    expect(answer).not.toHaveBeenCalled()
    if (picksProposal) {
      await user.click(second)
    } else {
      await user.type(screen.getByLabelText('交易规则'), '我自己选股票')
      await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    }
    await waitFor(() => expect(answer).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      draftId: 'paired-draft', revision: 2,
      answer: picksProposal ? 'pair-second' : '我自己选股票',
    })))
    expect(screen.getByText(
      picksProposal ? '贵州茅台 · 均线确认' : '我自己选股票',
      { selector: '.bubble.me p' },
    )).toBeVisible()
    expect(compile).toHaveBeenCalledTimes(1)
    if (picksProposal) {
      const expectedName = action === 'pick-second' ? '贵州茅台' : '300059.SZ'
      await screen.findByText('预览策略')
      expect(document.querySelector('.review-title')).toHaveTextContent(expectedName)
      if (action !== 'pick-second') {
        expect(document.querySelector('.review-title')).not.toHaveTextContent('贵州茅台')
      }
      await user.click(screen.getByRole('button', { name: '开始回测' }))
      await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))
      const saved = createRun.mock.calls[0]?.[0]
      expect(saved?.instrument.name).toBe(expectedName)
      expect(saved?.instrument.symbol).toBe(saved?.strategySpec.instrument.symbol)
    }
  }, 15_000)

  it.each([390, 720])('B31 keeps accepted-run feedback visible without closing desktop review (%s px)', async (width) => {
    enableImmediateMockWaitForTests()
    vi.stubGlobal('innerWidth', width)
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    const queued: BacktestRun = { id: `b31-${width}`, state: 'queued', progress: 0,
      progressLabel: '等待回测', createdAt: '2026-09-06T10:00:00Z', updatedAt: '2026-09-06T10:00:00Z',
      fingerprint: 'b31', error: null, resultAvailable: false }
    vi.spyOn(strategyApi, 'compile').mockResolvedValue(fixture)
    const revise = vi.spyOn(strategyApi, 'revise')
    const create = vi.spyOn(backtestApi, 'create').mockResolvedValue(queued)
    let finish!: (run: BacktestRun) => void
    const getRun = vi.spyOn(backtestApi, 'get').mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const user = userEvent.setup()
    const { container } = renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: '开始回测' }))
    await waitFor(() => expect(getRun).toHaveBeenCalled())
    expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', width <= 719 ? 'false' : 'true')
    expect(screen.getByRole('button', { name: '取消回测' })).toBeInTheDocument()
    expect(create).toHaveBeenCalledExactlyOnceWith(fixture.draft, { refreshData: false })
    expect(revise).not.toHaveBeenCalled()
    await act(async () => finish({ ...queued, state: 'failed', error: 'skill_mx_transport_error',
      progressLabel: '本次取数未完成' }))
    expect(await screen.findByText('回测失败')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新读取' })).toBeInTheDocument()
    expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', width <= 719 ? 'false' : 'true')
    if (width <= 719) {
      await user.click(screen.getByRole('button', { name: '修改规则' }))
      expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', 'true')
      // JSDOM 不执行移动媒体查询；此处验证点击接线，尺寸与可见性另走浏览器验收。
      await user.click(screen.getByLabelText('返回对话'))
      expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', 'false')
      expect(container.querySelector('.review-title')).toHaveTextContent('东方财富')
    }
  })

  it.each(['revision', 'create', 'closed-create'] as const)(
    'B31 exposes a mobile %s error and retries the same edited draft', async (failurePoint) => {
    enableImmediateMockWaitForTests()
    vi.stubGlobal('innerWidth', 390)
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected a component fixture')
    vi.spyOn(strategyApi, 'compile').mockResolvedValue(fixture)
    const message = '这次提交未完成，请重试。'
    let rejectRequest!: (error: Error) => void
    const revise = vi.spyOn(strategyApi, 'revise').mockImplementation(async draft => ({
      ...draft, revision: draft.revision + 1,
    }))
    const queued: BacktestRun = { id: `b31-retry-${failurePoint}`, state: 'queued', progress: 0,
      progressLabel: '等待回测', createdAt: '2026-09-06T10:00:00Z', updatedAt: '2026-09-06T10:00:00Z',
      fingerprint: 'b31-retry', error: null, resultAvailable: false }
    const create = vi.spyOn(backtestApi, 'create').mockResolvedValue(queued)
    vi.spyOn(backtestApi, 'get').mockImplementation(() => new Promise(() => {}))
    if (failurePoint === 'revision') revise.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRequest = reject }))
    else create.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRequest = reject }))
    const user = userEvent.setup()
    const { container } = renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: /成交设置/ }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始资金' }), { target: { value: '500000' } })
    fireEvent.change(screen.getByLabelText(/单边滑点/), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText(/佣金率/), { target: { value: '0.02' } })
    fireEvent.change(screen.getByLabelText('开始日期'), { target: { value: '2022-01-04' } })
    await user.click(screen.getByRole('button', { name: '完成' }))
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await waitFor(() => expect(rejectRequest).toBeDefined())
    if (failurePoint === 'closed-create') await user.click(screen.getByLabelText('返回对话'))
    await act(async () => rejectRequest(new Error(message)))
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', 'false')
    expect(screen.getAllByText(message)).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: '审阅策略' }))
    expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', 'true')
    expect(screen.getAllByText(message)).toHaveLength(1)
    await user.click(screen.getByLabelText('返回对话'))
    await user.click(screen.getByRole('button', { name: '重试回测' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(failurePoint === 'revision' ? 1 : 2))
    const submitted = create.mock.calls.at(-1)?.[0]
    expect(submitted).toMatchObject({ instrument: fixture.draft.instrument,
      entry: fixture.draft.entry, exit: fixture.draft.exit,
      backtest: { ...fixture.draft.backtest, initialCashCny: 500000, start: '2022-01-04' },
      execution: { ...fixture.draft.execution, slippageBps: 7, commissionRate: 0.0002 },
      revision: fixture.draft.revision + 1 })
    expect(create.mock.calls.at(-1)?.[1]).toEqual({ refreshData: false })
    expect(revise).toHaveBeenCalledTimes(failurePoint === 'revision' ? 2 : 1)
    if (failurePoint !== 'revision') expect(submitted).toEqual(create.mock.calls[0]?.[0])
    await waitFor(() => expect(container.querySelector('#pg-chat')).toHaveAttribute('data-review', 'false'))
    expect(screen.queryByText(message)).not.toBeInTheDocument()
  })

  it.each([
    ['换个条件再回测', '买入', '创30日新高且放量2倍', '买入条件改为：创30日新高且放量2倍'],
    ['换只股票试试', '股票', '贵州茅台', '股票换成贵州茅台'],
  ])('opens editable slots from %s while the review request is pending', async (chip, slot, replacement, edit) => {
    // UI wiring only. Real model/data acceptance is exercised separately in the live browser.
    settleMockRunOnFirstPoll()
    const compile = vi.spyOn(strategyApi, 'compile')
    const revise = vi.spyOn(strategyApi, 'revise')
    const createRun = vi.spyOn(backtestApi, 'create')
    vi.spyOn(backtestApi, 'review').mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    await user.click(screen.getByRole('button', { name: chip }))

    const field = await screen.findByRole('textbox', { name: slot }) as HTMLTextAreaElement
    expect(field).toHaveFocus()
    expect(field.selectionStart).toBe(0)
    expect(field.selectionEnd).toBe(field.value.length)
    expect(screen.getByRole('textbox', { name: '股票' })).toHaveValue('东方财富（300059.SZ）')
    expect(screen.getByRole('textbox', { name: '买入' })).toHaveValue('创 20 日新高 且 放量 1.5 倍')
    const exit = (screen.getByRole('textbox', { name: '卖出' }) as HTMLTextAreaElement).value
    expect(exit).toContain('20 日均线')

    // Opening a chip must not discard the in-flight review or submit a new request.
    expect(compile).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '查看这次报告' }))
    expect(screen.getByRole('button', { name: 'AI 正在分析' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(field)
    await user.keyboard('{Backspace}')
    expect(field).toHaveValue('')
    expect(screen.getByRole('button', { name: '识别交易规则' })).toBeDisabled()
    await user.keyboard(replacement)
    expect(screen.getByRole('textbox', { name: '卖出' })).toHaveValue(exit)
    const saved = await revise.mock.results[0]?.value
    const edited = { ...saved, id: 'edited-slot-draft' }
    compile.mockResolvedValueOnce({ status: 'compiled', draft: edited,
      isStrategyEdit: true, runRequested: true })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile.mock.calls[1]?.[0].utterance).toBe(`${edit}。其他条件和回测设置保持不变，按新条件重新回测。`)
    expect(compile.mock.calls[1]?.[1]).toBe(saved.id)
    expect(compile.mock.calls[1]?.[0].editCurrentStrategy).toBe(true)
    // Submitting the edited slots starts a NEW run without a second Start click.
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
    expect(createRun.mock.calls[1]?.[0].id).toBe('edited-slot-draft')
  })

  it.each([
    { reply: '候选点击', runRequested: true },
    { reply: '中金公司', runRequested: true },
    { reply: '中金公司，先别跑', runRequested: false },
    { reply: '取消这次换股', runRequested: false },
    { reply: '另起全新策略', runRequested: false },
  ])('continues the stock clarification in its saved revision: $reply', async ({ reply, runRequested }) => {
    // Component wiring only; the model and MX journeys are verified separately.
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({
      utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
    })
    if (fixture.status !== 'compiled') throw new Error('expected the component fixture')
    const configured = { ...fixture.draft, execution: {
      ...fixture.draft.execution, commissionRate: 0, minimumCommissionCny: 0, slippageBps: 2,
    } }
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'compiled', draft: configured,
    })
    const revise = vi.spyOn(strategyApi, 'revise')
    const createRun = vi.spyOn(backtestApi, 'create')
    vi.spyOn(backtestApi, 'review').mockImplementation(() => new Promise(() => {}))
    const answer = vi.spyOn(strategyApi, 'answerClarification')
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    await user.click(screen.getByRole('button', { name: '换只股票试试' }))
    const stock = await screen.findByRole('textbox', { name: '股票' })
    await user.clear(stock)
    await user.type(stock, '中金')
    const saved = await revise.mock.results[0]?.value
    if (!saved) throw new Error('expected the saved stock-edit draft')
    const question = '这个简称对应多只股票，你想用哪一只？'
    const pending = {
      status: 'needs_clarification' as const, draftId: 'ambiguous-stock', revision: 7,
      isStrategyEdit: true,
      assistantMessage: question,
      clarification: {
        id: 'strategy_edit_clarification', question, reason: '',
        choices: [{ id: '601995.SH', label: '中金公司', description: '601995.SH',
          action: 'submit_clarification' as const, suggestedUtterance: '中金公司',
          instrumentName: '中金公司', instrumentSymbol: '601995.SH' }],
      },
    }
    compile.mockResolvedValueOnce(pending)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText(question)).toBeVisible()
    expect(compile.mock.calls[1]?.[1]).toBe(saved.id)
    expect(compile.mock.calls[1]?.[0].editCurrentStrategy).toBe(true)
    expect(compile.mock.calls[1]?.[0].utterance).toContain('按新条件重新回测')
    expect(createRun).toHaveBeenCalledTimes(1)

    const changed = { ...saved, id: 'confirmed-stock', execution: fixture.draft.execution,
      instrument: { ...saved.instrument, name: '中金公司', symbol: '601995.SH', exchange: 'SSE' as const },
      strategySpec: { ...saved.strategySpec,
        instrument: { ...saved.strategySpec.instrument, symbol: '601995.SH' } },
    }
    answer.mockResolvedValueOnce({
      replyKind: 'accepted', assistantMessage: runRequested ? '按确认的股票继续回测。' : '暂不回测。',
      suggestions: [],
      outcome: { status: 'compiled', runRequested, isStrategyEdit: reply !== '另起全新策略',
        draft: reply.startsWith('取消') ? saved : changed },
    })
    if (reply === '候选点击') await user.click(screen.getByRole('button', { name: '中金公司' }))
    else {
      await user.type(screen.getByLabelText('交易规则'), reply)
      await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    }
    await waitFor(() => expect(answer).toHaveBeenCalledTimes(1))
    expect(answer.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      draftId: 'ambiguous-stock', revision: 7,
      answer: reply === '候选点击' ? '中金公司' : reply,
    }))
    expect(compile).toHaveBeenCalledTimes(2)
    if (runRequested) {
      await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
      expect(createRun.mock.calls[1]?.[0]).toEqual(expect.objectContaining({
        instrument: changed.instrument, entry: saved.entry, exit: saved.exit,
        backtest: saved.backtest, execution: saved.execution,
      }))
    } else {
      expect(await screen.findByRole('button', { name: '开始回测' })).toBeEnabled()
      expect(createRun).toHaveBeenCalledTimes(1)
      if (reply === '另起全新策略') {
        await user.click(screen.getByRole('button', { name: '开始回测' }))
        await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
        expect(createRun.mock.calls[1]?.[0].execution).toEqual(fixture.draft.execution)
        expect(createRun.mock.calls[1]?.[0].execution).not.toEqual(saved.execution)
      }
    }
  })

  it.each([[false, false], [true, false], [true, true]])(
    'uses the model run intent for a typed follow-up: run=%s refresh=%s',
    async (runRequested, refreshData) => {
    // Component wiring only; acceptance also uses the real model and the live browser.
    settleMockRunOnFirstPoll()
    const compile = vi.spyOn(strategyApi, 'compile')
    const createRun = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    const firstRun = await createRun.mock.results[0]?.value
    const saved = createRun.mock.calls[0]?.[0]
    if (!saved) throw new Error('expected the completed baseline draft')
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '收起策略审阅' }))
    compile.mockResolvedValueOnce({ status: 'compiled', runRequested, refreshData,
      draft: { ...saved, id: 'typed-edit' } })
    await user.type(screen.getByLabelText('交易规则'), refreshData
      ? '不要缓存，重新取数后再回测' : runRequested
        ? '买入均线改成5日，再跑一次' : '买入均线改成5日，先给我看看')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile.mock.calls[1]?.[0].relatedRunIds).toEqual([firstRun.id])
    if (runRequested) {
      await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
      expect(createRun.mock.calls[1]?.[0].id).toBe('typed-edit')
      expect(createRun.mock.calls[1]?.[1]).toEqual({ refreshData })
    } else {
      expect(await screen.findByRole('button', { name: '开始回测' })).toBeEnabled()
      expect(createRun).toHaveBeenCalledTimes(1)
    }
  })

  it('keeps each historical execution summary bound to its completed run', async () => {
    // Snapshot/rendering regression only; real model/data acceptance is separate.
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected the component fixture')
    const compile = vi.spyOn(strategyApi, 'compile')
    vi.spyOn(backtestApi, 'review').mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    const { container } = renderApp()
    const versions = [
      { slippageBps: 0, commissionRate: 0, minimumCommissionCny: 0 },
      { slippageBps: 8, commissionRate: 0.0003, minimumCommissionCny: 0 },
      { slippageBps: 8, commissionRate: 0.0003, minimumCommissionCny: 5 },
    ]
    for (const [index, settings] of versions.entries()) {
      compile.mockResolvedValueOnce({ status: 'compiled', executionSettings: settings,
        draft: { ...fixture.draft, id: `history-fee-${index}`,
          execution: { ...fixture.draft.execution, ...settings } } })
      fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: VOLUME_EXAMPLE } })
      await user.click(screen.getByRole('button', { name: '识别交易规则' }))
      await screen.findByText('预览策略')
      await user.click(screen.getByRole('button', { name: '开始回测' }))
      await screen.findByRole('heading', { name: '回测报告' })
      await user.click(screen.getByRole('button', { name: '回到对话' }))
      await user.click(screen.getByRole('button', { name: '收起策略审阅' }))
    }
    const historicalSummaries = () => Array.from(
      container.querySelectorAll('.stream .exec-entry .v'), element => element.textContent,
    )
    expect(historicalSummaries()).toEqual(['已调整 3 项', '已调整 2 项'])

    // A fresh default draft must not relabel already completed run snapshots.
    compile.mockResolvedValueOnce({ status: 'compiled', executionSettings: {},
      draft: { ...fixture.draft, id: 'fresh-default-fees' } })
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    expect(within(screen.getByLabelText('策略审阅'))
      .getByRole('button', { name: '成交设置默认' })).toBeVisible()
    expect(historicalSummaries()).toEqual(['已调整 3 项', '已调整 2 项', '已调整 1 项'])
  }, 15_000)

  it.each(['direct', 'clarified', 'fresh'] as const)(
    'uses server execution settings instead of stale local fees: %s', async (path) => {
    // Configuration wiring only; real model/data acceptance runs in the live browser.
    settleMockRunOnFirstPoll()
    const fixture = await mockApi.compile({ utterance: VOLUME_EXAMPLE,
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
    if (fixture.status !== 'compiled') throw new Error('expected the component fixture')
    const configured = { ...fixture.draft, execution: { ...fixture.draft.execution,
      slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0 } }
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'compiled', draft: configured,
      executionSettings: { slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0 },
    })
    const answer = vi.spyOn(strategyApi, 'answerClarification')
    const createRun = vi.spyOn(backtestApi, 'create')
    vi.spyOn(backtestApi, 'review').mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '收起策略审阅' }))

    const settings = path === 'fresh' ? {} : {
      slippageBps: 3, commissionRate: path === 'clarified' ? 0.0001 : 0, minimumCommissionCny: 0,
    }
    const resolved = { status: 'compiled' as const, isStrategyEdit: path !== 'fresh',
      executionSettings: settings, runRequested: false,
      draft: { ...fixture.draft, id: 'server-fee-draft',
        execution: { ...fixture.draft.execution, ...settings } },
    }
    const pendingSettings = { slippageBps: 3, commissionRate: 0, minimumCommissionCny: 0 }
    compile.mockResolvedValueOnce(path === 'clarified' ? {
      status: 'needs_clarification', draftId: 'fee-unit-clarification', revision: 2,
      isStrategyEdit: true, executionSettings: pendingSettings,
      clarification: { id: 'strategy_edit_clarification', question: '佣金 1 是万分之一吗？',
        reason: '', choices: [] },
    } : resolved)
    await user.type(screen.getByLabelText('交易规则'), path === 'fresh'
      ? '另起一条新策略，先给我看看' : path === 'clarified'
        ? '滑点改成3基点，佣金改成1，先别跑' : '滑点改成3基点，其他不变，先别跑')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile.mock.calls[0]?.[0].executionSettings).toBeUndefined()
    expect(compile.mock.calls[1]?.[0].executionSettings).toMatchObject({
      slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0,
    })
    if (path === 'clarified') {
      expect(await screen.findByText('佣金 1 是万分之一吗？')).toBeVisible()
      answer.mockResolvedValueOnce({ replyKind: 'accepted', assistantMessage: '已改好，暂不回测。',
        suggestions: [], outcome: resolved })
      await user.type(screen.getByLabelText('交易规则'), '对，万分之一，先别跑')
      await user.click(screen.getByRole('button', { name: '识别交易规则' }))
      await waitFor(() => expect(answer).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
        draftId: 'fee-unit-clarification', revision: 2, executionSettings: pendingSettings,
      })))
    }
    expect(await screen.findByRole('button', { name: '开始回测' })).toBeEnabled()
    expect(createRun).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
    expect(createRun.mock.calls[1]?.[0].execution).toEqual(resolved.draft.execution)
    expect(createRun.mock.calls[1]?.[0].entry).toEqual(configured.entry)
    expect(createRun.mock.calls[1]?.[0].exit).toEqual(configured.exit)
    expect(createRun.mock.calls[1]?.[0].backtest).toEqual(configured.backtest)
  })

  it('clears an unconsumed refresh intent before a typed preview-only follow-up', async () => {
    // Component state regression only; these mocked outcomes are not live acceptance.
    settleMockRunOnFirstPoll()
    const compile = vi.spyOn(strategyApi, 'compile')
    const createRun = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    const saved = createRun.mock.calls[0]?.[0]
    if (!saved) throw new Error('expected the completed baseline draft')
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '收起策略审阅' }))

    compile.mockResolvedValueOnce({
      status: 'compiled', runRequested: true, refreshData: true,
      draft: {
        ...saved, id: 'blocked-refresh',
        backtest: { ...saved.backtest, initialCashCny: 1 },
      },
    })
    await user.type(screen.getByLabelText('交易规则'), '不要缓存，重新取数后再回测')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByRole('button', { name: '请检查设置' })).toBeDisabled()
    expect(screen.getByText('初始资金需为 1 万元至 10 亿元之间的整数。')).toBeVisible()
    expect(createRun).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '收起策略审阅' }))

    compile.mockResolvedValueOnce({
      status: 'compiled', draft: { ...saved, id: 'preview-only-after-refresh' },
    })
    await user.type(screen.getByLabelText('交易规则'), '先按原条件给我看看，不要运行')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(screen.getByRole('button', { name: '开始回测' })).toBeEnabled())
    expect(compile.mock.calls[2]?.[1]).toBe('blocked-refresh')
    expect(createRun).toHaveBeenCalledTimes(1)
  })

  it('keeps draft lineage for a new strategy and clears it only for a new conversation', async () => {
    const pending = (draftId: string) => ({
      status: 'needs_clarification' as const,
      draftId,
      revision: 1,
      clarification: {
        id: 'strategy_rule_incomplete',
        question: '请补充完整的买入和卖出条件。',
        reason: '还缺买卖条件。',
        choices: [],
      },
    })
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce(pending('conversation-draft-1'))
      .mockResolvedValueOnce(pending('conversation-draft-2'))
      .mockResolvedValueOnce(pending('new-conversation-draft'))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.type(input, 'MACD')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/请补充完整的买入和卖出条件/)).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: '新建策略', hidden: true }))
    await user.type(input, 'RSI')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile.mock.calls[1]?.[1]).toBe('conversation-draft-1')

    fireEvent.click(screen.getByRole('button', {
      name: '新建会话并清空上下文', hidden: true,
    }))
    await user.type(input, 'KDJ')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(3))
    expect(compile.mock.calls[2]).toHaveLength(1)
  })

  it('shows a direct current-data answer and three follow-up strategies without provider internals', async () => {
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'live-data-draft',
      revision: 1,
      assistantMessage: '东方财富昨天换手率为 2.37%。成交活跃度需要结合趋势确认，你可以从下面三种规则里选一种继续回测。',
      clarification: {
        id: 'live_data_query',
        question: '选一种规则继续回测。',
        reason: '',
        choices: [],
        ideaRoute: {
          schema_version: 'idea-route.v1',
          understanding: '已查到换手率，下面只提出待验证方向。',
          hypothesis: '成交活跃度需要与价格趋势共同验证。',
          asset_mapping: {
            instrument_symbol: '300059.SZ',
            relation: 'current_page_proxy',
            rationale: '查询实体已由数据服务识别为东方财富。',
            evidence_status: 'host_context_only',
          },
          proposals: [
            {
              id: 'trend', title: '趋势确认', hypothesis: '确认趋势后参与',
              entry_summary: '价格上穿 20 日均线', exit_summary: '价格跌破 20 日均线',
              suggested_utterance: '东方财富价格上穿20日均线买入，跌破20日均线卖出，回测近1年',
              instrument_symbol: '300059.SZ', capability_ids: ['technical.ma'], assumptions: [], confidence: 0.8,
            },
            {
              id: 'reversal', title: '超跌反转', hypothesis: '验证超跌后的反弹',
              entry_summary: 'RSI 低于 30', exit_summary: 'RSI 高于 70',
              suggested_utterance: '东方财富RSI低于30买入，高于70卖出，回测近1年',
              instrument_symbol: '300059.SZ', capability_ids: ['technical.rsi'], assumptions: [], confidence: 0.8,
            },
            {
              id: 'momentum', title: '动量转强', hypothesis: '验证动量由弱转强',
              entry_summary: 'MACD 金叉', exit_summary: 'MACD 死叉',
              suggested_utterance: '东方财富MACD金叉买入，死叉卖出，回测近1年',
              instrument_symbol: '300059.SZ', capability_ids: ['technical.macd'], assumptions: [], confidence: 0.8,
            },
          ],
        },
      },
      data: {
        usageScope: 'current_query_only',
        historicalBacktestEligible: false,
        kind: 'finance',
        finance: {
          provider: 'eastmoney_mx_finance_data',
          query: '东方财富昨天的换手率是多少',
          indicators: '昨天的换手率',
          tables: [{
            title: '东方财富换手率',
            rawTable: {
              headers: ['日期', '换手率(%)'],
              data: [['2026-09-02', '2.37']],
            },
          }],
          provenance: {
            responseSha256: `sha256:${'a'.repeat(64)}`,
            retrievedAt: '2026-09-03T09:31:00+08:00',
            schemaVersion: 'eastmoney-mx.search-data.v1',
          },
        },
      },
    })
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('交易规则'), '东方财富昨天的换手率是多少')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText(/东方财富昨天换手率为 2.37%/)).toBeVisible()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText(/eastmoney_mx_finance_data/)).not.toBeInTheDocument()
    expect(screen.queryByText(/不作为历史回测数据/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '趋势确认' })).toBeVisible()
    expect(screen.getByRole('button', { name: '超跌反转' })).toBeVisible()
    expect(screen.getByRole('button', { name: '动量转强' })).toBeVisible()
    expect(screen.getByText('价格上穿 20 日均线')).toBeVisible()
    expect(screen.getByText('价格跌破 20 日均线')).toBeVisible()
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
  })

  it('starts a fresh network-analysis draft with the selected screen entity as context', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'screen-data-draft',
        revision: 1,
        assistantMessage: '查到了 2 条 A 股筛选结果。',
        clarification: {
          id: 'live_data_query',
          question: '查到了 2 条 A 股筛选结果。',
          reason: '',
          choices: [],
        },
        data: {
          usageScope: 'current_query_only',
          historicalBacktestEligible: false,
          kind: 'screen',
          screen: {
            provider: 'eastmoney_mx_stocks_screener',
            query: '筛选 A 股',
            assetType: 'A股',
            columns: ['证券代码', '证券简称'],
            rows: [
              { '证券代码': '300059', '证券简称': '东方财富' },
              { '证券代码': '300033', '证券简称': '同花顺' },
            ],
            provenance: {
              responseSha256: `sha256:${'b'.repeat(64)}`,
              retrievedAt: '2026-09-04T09:31:00+08:00',
              schemaVersion: 'eastmoney-mx.select-security.v1',
            },
          },
        },
      })
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'analysis-draft',
        revision: 1,
        clarification: {
          id: 'idea_guidance_required',
          question: '选一条继续。',
          reason: '正在把分析变成待确认规则。',
          choices: [],
        },
      })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
    const user = userEvent.setup()
    renderApp({
      name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE',
    }, { instrumentContextSource: 'stock_page' })

    await user.type(screen.getByLabelText('交易规则'), '筛选 A 股')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', {
      name: '联网分析并给回测策略：同花顺 · 300033.SZ',
    }))

    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile).toHaveBeenLastCalledWith({
      instrument: {
        name: '同花顺', symbol: '300033.SZ', market: 'CN_A', exchange: 'SZSE',
      },
      instrumentContextSource: 'stock_page',
      utterance: '分析同花顺（300033.SZ）的相关公开信息，给我几个可回测策略',
    })
    expect(answerClarification).not.toHaveBeenCalled()
  })

  it('asks for a missing stock as plain text and keeps the bottom input usable', async () => {
    const onReturnToStockPage = vi.fn()
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    renderApp(undefined, {
      instrumentContextError: '股票页没有提供有效的 A 股代码。',
      onReturnToStockPage,
    })

    expect(screen.getByText(/没有识别到当前股票/)).toBeInTheDocument()
    expect(screen.queryByText('股票页上下文无效')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '使用东方财富示例' })).not.toBeInTheDocument()
    const input = screen.getByLabelText('交易规则')
    expect(input).toBeEnabled()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '输入股票名称、买入和卖出条件')
    await waitFor(() => expect(input).toHaveFocus())
    expect(screen.queryByLabelText('策略示例')).not.toBeInTheDocument()

    await user.type(input, '同花顺 MACD金叉买入，死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(input).toHaveValue('')
    await waitFor(() => expect(compile).toHaveBeenCalledWith(expect.objectContaining({
      utterance: '同花顺 MACD金叉买入，死叉卖出',
    })))
  })

  it('uses the A-share instrument supplied by the host stock page', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    const instrument: Instrument = {
      name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE',
    }
    renderApp(instrument)

    expect(screen.getByText('想怎么交易？用一句话告诉我，我来帮你把它变成可回测的策略。').closest('.say'))
      .toBeInTheDocument()
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    expect(screen.getByRole('button', {
      name: '贵州茅台5日均线上穿20日均线买入，5日均线下穿20日均线卖出',
    })).toBeVisible()
    await user.click(screen.getByRole('button', {
      name: '贵州茅台5日均线上穿20日均线买入，5日均线下穿20日均线卖出',
    }))
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    const thinking = screen.getByRole('status', { name: '处理进度' })
    expect(thinking).toHaveTextContent('处理中')
    expect(thinking.closest('.thinking-stream')).not.toHaveClass('mcard')
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(screen.queryByText('处理记录')).not.toBeInTheDocument()
    expect(compile).toHaveBeenCalledWith(expect.objectContaining({ instrument }))
  })

  it('keeps only service progress events in the completed processing record', async () => {
    const instrument: Instrument = {
      name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE',
    }
    const utterance = '贵州茅台收盘价上穿20日均线买入，下穿20日均线卖出'
    const compiled = await mockApi.compile({ instrument, utterance })
    if (compiled.status !== 'compiled') throw new Error('expected compiled strategy')

    let release: (() => void) | undefined
    vi.spyOn(strategyApi, 'compile').mockImplementation(async (input) => {
      input.dialogueProgress?.onProgress([
        { stage: 'model', message: '已调用策略生成模型', elapsedMs: 1_200 },
        { stage: 'validation', message: '策略结构已通过校验', elapsedMs: 2_450 },
      ])
      await new Promise<void>((resolve) => { release = resolve })
      return compiled
    })
    const user = userEvent.setup()
    renderApp(instrument)

    await user.type(screen.getByLabelText('交易规则'), utterance)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(screen.getByRole('status', { name: '处理进度' }))
      .toHaveTextContent('正在核对策略')

    release?.()
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    const record = screen.getByText('处理过程')
    await user.click(record)
    expect(screen.getByText('策略结构已通过校验')).toBeVisible()
    expect(screen.queryByText('已调用策略生成模型')).not.toBeInTheDocument()
    expect(screen.queryByText(/^买入：/)).not.toBeInTheDocument()
    expect(screen.queryByText(/技术信号按日线收盘确认/)).not.toBeInTheDocument()
  })

  it('marks the Mock runtime internally and never labels Mock evidence as proved', async () => {
    const user = userEvent.setup()
    const { container } = renderApp()

    expect(container.querySelector('.app')).toHaveAttribute('data-api-mode', 'mock')
    expectMockRuntimeMarker()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expectMockRuntimeMarker()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()
  })

  it('only shows the return-to-bottom control after the conversation is scrolled away', () => {
    const { container } = renderApp()
    const scroll = container.querySelector<HTMLElement>('#pg-chat .scroll')
    if (!scroll) throw new Error('missing chat scroll container')

    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1_000 },
      clientHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, value: 100, writable: true },
    })

    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    fireEvent.scroll(scroll)
    expect(screen.getByRole('button', { name: '回到底部' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '回到底部' }))
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('runs the current technical journey through settings, report, drawdown, and causal trace', async () => {
    settleMockRunOnFirstPoll({ preserveFirstRunningState: true })
    const user = userEvent.setup()
    const { container } = renderApp()

    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    const examples = within(screen.getByLabelText('策略示例'))
    expect(examples.getAllByRole('button')).toHaveLength(2)
    for (const example of DEFAULT_STRATEGY_EXAMPLES) {
      expect(examples.getByRole('button', { name: example.utterance })).toBeVisible()
    }
    expect(examples.queryByRole('button', { name: VOLUME_EXAMPLE })).not.toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(screen.getAllByText('创 20 日新高').length).toBeGreaterThan(0)
    expect(screen.getAllByText('放量 1.5 倍').length).toBeGreaterThan(0)
    expect(screen.getAllByText('收盘跌破 20 日均线').length).toBeGreaterThan(0)
    expectHomeToHideDefaultCapital(container)

    await user.click(screen.getByRole('button', { name: /区间/ }))
    expect(screen.getByRole('heading', { name: '策略设置' })).toBeInTheDocument()
    const initialCash = screen.getByRole('spinbutton', { name: '初始资金' })
    expect(initialCash).toHaveValue(1000000)
    await user.clear(initialCash)
    await user.type(initialCash, '500000')
    await user.click(screen.getByRole('button', { name: '完成' }))
    expectHomeToHideDefaultCapital(container)

    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText(/^预览：/)).toBeInTheDocument()
    expect(screen.queryByText(/后台返回的处理阶段/)).not.toBeInTheDocument()
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()
    expectMockRuntimeMarker()
    expectHomeToHideDefaultCapital(container)
    expect(screen.getAllByText(
      '策略亏损 2.54%，同样的钱买入后一直持有亏损 39.23%，相对少亏 36.69 个百分点。',
    ).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /^换个条件$/ }))
      .not.toBeInTheDocument()

    // 回测跑完直接落在详情态的报告分区，不需要再点一次「查看完整报告」
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(document.querySelector('#pg-report .report-summary-meta .tag')).toBeNull()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()
    expect(document.querySelectorAll('#pg-report .report-risk .notice')).toHaveLength(0)
    expect(within(document.querySelector('#pg-report') as HTMLElement)
      .queryByText(/风险提示/)).not.toBeInTheDocument()
    const reportPage = within(document.querySelector('#pg-report') as HTMLElement)
    expect(reportPage.queryByText('初始资金')).not.toBeInTheDocument()
    expect(reportPage.queryByText('期末资产')).not.toBeInTheDocument()
    expect(reportPage.queryByRole('tablist')).not.toBeInTheDocument()
    expect(reportPage.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
    expect(reportPage.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    expectHomeToHideDefaultCapital(container)
    await user.click(screen.getByRole('button', { name: '查看完整报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.getAllByText('最大回撤').length).toBeGreaterThan(0)
    // 点位明细已经并进「每笔委托」列表，图表下面不再有第二套入口。
    expect(screen.queryByRole('button', { name: '选择点位' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    for (const label of ['方向 / 时间', '信号', '委托价', '状态 / 成交价']) {
      expect(screen.getAllByText(label, { exact: true }).length).toBeGreaterThan(0)
    }

    await user.click(screen.getByRole('button', { name: /买入 MACD 金叉确认 未成/ }))
    expect(screen.getByText('从你那句话到账户变化')).toBeInTheDocument()
    expect(screen.getByText('用户原话')).toBeInTheDocument()
    expect(screen.getByText('规范化条件')).toBeInTheDocument()
    for (const label of ['MACD 金叉确认', '形成买入决策', '提交买入委托', '涨停未成交', '账户净值记录']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    const chainPage = within(document.querySelector('#pg-chain') as HTMLElement)
    expect(chainPage.getByText('技术详情')).toBeVisible()
    for (const identity of chainPage.getAllByText(/chain decision_buy_blocked/)) {
      expect(identity).not.toBeVisible()
    }
    await user.click(chainPage.getByText('技术详情'))
    for (const identity of chainPage.getAllByText(/chain decision_buy_blocked/)) {
      expect(identity).toBeVisible()
    }
  }, 12_000)

  it.each([
    ['skill_mx_read_timeout', '等待东方财富查数 Skill响应超时，本次取数未完成，可以重试。'],
    ['skill_history_before_listing', '这只股票于 2021-04-09 上市，你选择的区间从 2020-09-06 开始，包含上市前日期。请修改回测区间；买卖规则和成交设置已保留，不会自动缩短区间。'],
    ['skill_history_fields_missing', '查询 2010-03-09 至 2012-03-08 的历史数据时，东方财富未返回涨停价、跌停价。本次回测未完成，原区间和规则已保留；可修改区间或稍后重新读取。'],
  ])('retries and edits a failed run without resetting or replacing its strategy: %s', async (error, progressLabel) => {
    const instrument: Instrument = {
      name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE',
    }
    const compiled = await mockApi.compile({
      instrument,
      instrumentContextSource: 'stock_page',
      utterance: MOVING_AVERAGE_EXAMPLE,
    })
    if (compiled.status !== 'compiled') throw new Error('expected compiled moving-average draft')

    const capabilities: CapabilitiesResponse = {
      markets: ['CN_A'],
      input_modes: ['natural_language_zh'],
      strategy_scopes: ['single_instrument', 'long_only'],
      indicators: [{
        indicator_id: 'technical.ma',
        definition_version: '1.0.0',
        status: 'stable',
        display_name: '移动平均线',
        description: '移动平均线',
        warmup_bars: 21,
        timeframes: ['1d'],
        evaluation_modes: ['bar_close_confirmed'],
        triggers: ['price_crosses_above', 'price_crosses_below'],
        parameters: [],
        trigger_definitions: [],
      }],
      events: [],
      execution_policies: ['next_tradable_session_open'],
      event_catalog_status: 'published',
      event_backtest_available: false,
      event_preparation_available: false,
      event_availability_scope: 'unavailable',
      backtest_execution_available: true,
      limits: {
        max_body_bytes: 16_384,
        max_utterance_characters: 2000,
        max_instrument_context_characters: 32,
      },
    }
    const terminalRun = (id: string): BacktestRun => ({
      id,
      state: 'failed',
      progress: 10,
      progressLabel,
      createdAt: '2026-09-05T00:00:00Z',
      updatedAt: '2026-09-05T00:00:00Z',
      fingerprint: id,
      error,
      resultAvailable: false,
    })
    vi.spyOn(systemApi, 'capabilities').mockResolvedValue(capabilities)
    vi.spyOn(strategyApi, 'compile').mockResolvedValue(compiled)
    const revise = vi.spyOn(strategyApi, 'revise').mockImplementation(async (draft) => ({
      ...draft,
      revision: draft.revision + 1,
    }))
    const create = vi.spyOn(backtestApi, 'create')
      .mockResolvedValueOnce(terminalRun('run:unchanged'))
      .mockResolvedValueOnce(terminalRun('run:retried'))
      .mockResolvedValueOnce(terminalRun('run:edited'))
    vi.spyOn(backtestApi, 'get').mockImplementation(async (runId) => terminalRun(runId))
    const user = userEvent.setup()

    const unchangedView = renderApp(instrument)
    await user.type(screen.getByLabelText('交易规则'), MOVING_AVERAGE_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: '开始回测' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    expect(revise).not.toHaveBeenCalled()
    expect(create).toHaveBeenNthCalledWith(1, compiled.draft, { refreshData: false })
    expect(await screen.findByText(progressLabel))
      .toBeVisible()
    if (error === 'skill_history_before_listing') {
      expect(screen.getByText('请调整回测区间')).toBeVisible()
      expect(screen.queryByRole('button', { name: '重新读取' })).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: '修改回测区间' }))
      expect(screen.getByRole('spinbutton', { name: '初始资金' })).toBeVisible()
      expect(create).toHaveBeenCalledTimes(1)
      expect(revise).not.toHaveBeenCalled()
      expect(strategyApi.compile).toHaveBeenCalledTimes(1)
      unchangedView.unmount()
      return
    }
    await user.click(screen.getByRole('button', { name: '重新读取' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2))
    expect(create).toHaveBeenNthCalledWith(2, compiled.draft, { refreshData: true })
    expect(revise).not.toHaveBeenCalled()
    expect(strategyApi.compile).toHaveBeenCalledTimes(1)
    await user.click(await screen.findByRole('button', { name: '修改规则' }))
    expect(screen.getByText('预览策略')).toBeVisible()
    expect(screen.getByRole('button', { name: '开始回测' })).toBeVisible()
    expect(strategyApi.compile).toHaveBeenCalledTimes(1)
    expect(create).toHaveBeenCalledTimes(2)
    unchangedView.unmount()

    renderApp(instrument)
    await user.type(screen.getByLabelText('交易规则'), MOVING_AVERAGE_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: /区间/ }))
    const initialCash = screen.getByRole('spinbutton', { name: '初始资金' })
    await user.clear(initialCash)
    await user.type(initialCash, '500000')
    await user.click(screen.getByRole('button', { name: '完成' }))
    await user.click(screen.getByRole('button', { name: '开始回测' }))

    await waitFor(() => expect(create).toHaveBeenCalledTimes(3))
    expect(revise).toHaveBeenCalledTimes(1)
    expect(revise.mock.calls[0]?.[0].backtest.initialCashCny).toBe(500000)
    expect(create.mock.calls[2]?.[0].revision).toBe(compiled.draft.revision + 1)
  })

  it('keeps a complete result authoritative when a later run-status refresh returns 404', async () => {
    const getRun = settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { client } = renderApp()

    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText('回测结果', {}, { timeout: 6_000 })).toBeInTheDocument()

    getRun.mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '请求未完成',
      status: 404,
      detail: 'Backtest run was not found',
      code: 'backtest_run_not_found',
    }))
    await client.refetchQueries({ queryKey: ['backtest-run'] })

    expect(screen.getByText('回测结果')).toBeInTheDocument()
    expect(screen.queryByText('任务状态读取失败')).not.toBeInTheDocument()
    expect(screen.queryByText('Backtest run was not found')).not.toBeInTheDocument()
  }, 8_000)

  it('runs the volume-breakout example as two technical entry conditions', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(screen.getAllByText('创 20 日新高').length).toBeGreaterThan(0)
    expect(screen.getAllByText('放量 1.5 倍').length).toBeGreaterThan(0)
    expect(screen.getAllByText('收盘跌破 20 日均线').length).toBeGreaterThan(0)
    expect(screen.queryByText('业绩预告发布')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
  }, 9_000)

  it('shows the exact report-text threshold and fill-anchored trading-session exit as Mock', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    renderApp({ name: '同花顺', symbol: '300033.SZ', market: 'CN_A', exchange: 'SZSE' })

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(screen.getAllByText('年度报告正文中“AI”完整词出现 > 5 次').length).toBeGreaterThan(0)
    expect(screen.getAllByText('实际买入成交后第 3 个交易日卖出').length).toBeGreaterThan(0)
    expect(screen.getByText(/没有读取年报正文，也没有计算词频/)).toBeInTheDocument()
    expectMockRuntimeMarker()

    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()

    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', {
      name: /买入 年度报告正文词频条件确认 已成/,
    }))

    expect(screen.getByRole('heading', { name: /买入成交 · 因果轨迹/ })).toBeInTheDocument()
    const chainPage = within(document.querySelector('#pg-chain') as HTMLElement)
    expect(chainPage.getByText('从你那句话到账户变化')).toBeInTheDocument()
    expect(chainPage.getByText('用户原话')).toBeInTheDocument()
    expect(chainPage.getByText('规范化条件')).toBeInTheDocument()
    expect(chainPage.getByText('同花顺发年报提到ai次数超过5次的话就买入，3天后卖出')).toBeInTheDocument()
    expect(chainPage.getByText(/买入：年度报告正文中“AI”完整词出现 > 5 次/)).toBeInTheDocument()
    expect(chainPage.getByText(/卖出：实际买入成交后第 3 个交易日卖出/)).toBeInTheDocument()
    expect(chainPage.getByText(/^成交时间质量：$/)).toBeInTheDocument()
    expect(chainPage.getByText(/不代表已观测到该时刻的真实成交/)).toBeInTheDocument()
    expectMockRuntimeMarker()
  }, 10_000)

  it('keeps a clarification editable without repeating recognized strategy fields in the reply', async () => {
    const original = '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出'
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_low_confidence',
      clarification: {
        id: 'candidate_provider_low_confidence',
        question: '这条规则里有一个低置信度片段，请补充后再继续。',
        reason: '系统只集中确认一次，不会丢掉已经识别的条件。',
        recognized: [
          { label: '股票', value: '同花顺 300033.SZ' },
          { label: '公告事件', value: '年度报告' },
          { label: '正文条件', value: 'AI 完整词出现 > 5 次' },
          { label: '卖出', value: '实际买入成交后持有 3 个交易日' },
        ],
        choices: [{
          id: 'edit-utterance',
          label: '补充这一个片段',
          description: '返回原话继续补充；已识别内容保持不变。',
          recommended: true,
          action: 'edit_utterance',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp({ name: '同花顺', symbol: '300033.SZ', market: 'CN_A', exchange: 'SZSE' })

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, original)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const prompt = await screen.findByText('这条规则里有一个低置信度片段，请补充后再继续。')
    expect(prompt.textContent).toBe('这条规则里有一个低置信度片段，请补充后再继续。')
    expect(prompt).not.toHaveTextContent('股票是同花顺 300033.SZ')
    expect(prompt).not.toHaveTextContent('公告事件是年度报告')
    expect(prompt).not.toHaveTextContent('正文条件是AI 完整词出现 > 5 次')
    expect(prompt).not.toHaveTextContent('卖出是实际买入成交后持有 3 个交易日')
    expect(screen.queryByText('只问这一次')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补充这一个片段/ })).not.toBeInTheDocument()
    expect(screen.queryByText('无法识别这条策略')).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '补充完整规则，或直接换一种说法')
    await waitFor(() => expect(input).toHaveFocus())
    expect(compile).toHaveBeenCalledTimes(1)
  })

  it('asks for a missing standalone stock in text and merges the typed reply with the saved rule', async () => {
    const original = 'MACD金叉买入，MACD死叉卖出，回测近1年'
    const originalCompile = strategyApi.compile
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_missing_instrument',
      revision: 1,
      clarification: {
        id: 'instrument_required',
        question: '请直接告诉我想回测的股票名称或 6 位证券代码；前面已经识别到的买卖条件我会继续保留。',
        reason: '',
        choices: [],
      },
    })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
      .mockImplementationOnce(async (turn) => ({
        replyKind: 'accepted',
        assistantMessage: '好，股票已经确认，刚才的买卖规则也都保留了。',
        suggestions: [],
        outcome: await originalCompile({
          ...turn.originalRequest,
          utterance: `同花顺，${original}`,
        }),
      }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.type(input, original)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const prompt = await screen.findByText(/请直接告诉我想回测的股票名称或 6 位证券代码/)
    expect(prompt).not.toHaveTextContent('现在只缺回测标的')
    expect(screen.queryByRole('button', { name: '补充股票代码' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '输入股票名称或 6 位代码')
    await waitFor(() => expect(input).toHaveFocus())

    await user.type(input, '同花顺')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(input).toHaveValue('')

    await waitFor(() => expect(answerClarification).toHaveBeenCalledTimes(1))
    expect(compile).toHaveBeenCalledTimes(1)
    expect(answerClarification).toHaveBeenCalledWith(expect.objectContaining({
      draftId: 'draft_missing_instrument',
      revision: 1,
      answer: '同花顺',
      originalRequest: expect.objectContaining({
        utterance: original,
        instrumentContextSource: 'standalone_default',
      }),
    }))
    expect(await screen.findByText(/股票已经确认/)).toBeInTheDocument()
    expect(screen.getAllByText('好，股票已经确认，刚才的买卖规则也都保留了。')).toHaveLength(1)
    const readyTurn = screen.getByText('预览策略').closest('.turn')
    expect(readyTurn).toContainElement(screen.getByText('好，股票已经确认，刚才的买卖规则也都保留了。'))
    expect(readyTurn).not.toContainElement(screen.getByText(/请直接告诉我想回测的股票名称或 6 位证券代码/))
    expect(screen.queryByText(/已经把这句话整理成买卖规则/)).not.toBeInTheDocument()
  })

  it('shows the complete clarification without appending reason recognized fields or an options hint', async () => {
    const question = '用贵州茅台试试，还是换一只股票？'
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_complete_clarification',
      clarification: {
        id: 'instrument_required',
        question,
        reason: '旧版解释，不应再拼到回复前。',
        recognized: [{ label: '股票', value: '贵州茅台 600519.SH' }],
        choices: [{
          id: 'use-current-instrument',
          label: '使用 贵州茅台',
          description: '继续回测 600519.SH。',
          recommended: true,
          action: 'submit_clarification',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp(
      { name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE' },
      { instrumentContextSource: 'stock_page' },
    )

    await user.type(screen.getByLabelText('交易规则'), 'MACD金叉买入，MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const reply = await screen.findByText(question)
    expect(reply.textContent).toBe(question)
    expect(document.body).not.toHaveTextContent('旧版解释，不应再拼到回复前。')
    expect(document.body).not.toHaveTextContent('我已保留这些内容')
    expect(document.body).not.toHaveTextContent('股票是贵州茅台')
    expect(document.body).not.toHaveTextContent('选一个试试，或说说你想怎么改。')
    expect(screen.getByRole('button', { name: '使用 贵州茅台' })).toBeVisible()
  })

  it('offers a trusted stock-page instrument while keeping free stock input available', async () => {
    const instrument: Instrument = {
      name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE',
    }
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_stock_page_instrument',
      clarification: {
        id: 'instrument_required',
        question: '请确认使用当前股票“贵州茅台”，或直接输入其他股票名称或 6 位证券代码。',
        reason: '',
        choices: [{
          id: 'use-current-instrument',
          label: '使用 贵州茅台',
          description: '继续回测 600519.SH。',
          recommended: true,
          action: 'submit_clarification',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp(instrument, { instrumentContextSource: 'stock_page' })

    const input = screen.getByLabelText('交易规则')
    await user.type(input, 'MACD金叉买入，MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const prompt = await screen.findByText(/请确认使用当前股票“贵州茅台”/)
    expect(prompt.textContent).toBe('请确认使用当前股票“贵州茅台”，或直接输入其他股票名称或 6 位证券代码。')
    expect(prompt).not.toHaveTextContent('选一个试试，或说说你想怎么改。')
    expect(screen.getByRole('button', { name: '使用 贵州茅台' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '补充股票代码' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveAttribute('placeholder', '输入股票名称或 6 位代码')
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('shows the specific Skill data failure without an understanding disclaimer', async () => {
    const detail = '东方财富查数 Skill 的历史指标数据暂不满足本次回测要求：缺少20日平均成交量列。'
    vi.spyOn(strategyApi, 'compile').mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '历史指标数据暂不可用',
      status: 422,
      detail,
      code: 'skill_indicator_unavailable',
    }))
    const user = userEvent.setup()
    renderApp()
    await user.type(screen.getByLabelText('交易规则'), '放量买入，跌破20日线卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText(detail)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('我已理解')
    expect(document.body).not.toHaveTextContent('不是模型')
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
  })

  it('separates recognized document rules with missing data from unknown language', async () => {
    vi.spyOn(strategyApi, 'compile').mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '报告正文数据不可用',
      status: 422,
      detail: '当前固定快照没有年度报告正文提取结果。',
      code: 'event_document_text_data_unavailable',
    }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '年报正文 AI 超过 5 次买入，3 个交易日后卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const recovery = await screen.findByText(/我已理解你想用报告正文作为条件/)
    expect(recovery).toHaveTextContent('不会用公告标题代替')
    expect(screen.queryByText('无法识别这条策略')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改规则' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveValue('')
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('rejects an unsupported sentence instead of silently falling back to MACD', async () => {
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '火星逆行时满仓，月圆时卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const recovery = await screen.findByText(/我还没能把这句话还原成完整的买卖规则/)
    expect(recovery).toHaveTextContent('价格阈值、涨跌幅')
    expect(screen.queryByText('无法识别这条策略')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改规则' })).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('no_supported_signal_recognized')
    expect(screen.queryByText('MACD 金叉')).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('guides an unavailable previous-session limit-up rule through plain text input', async () => {
    const original = '东方财富昨天涨停今天买入，短线卖出'
    const compile = vi.spyOn(strategyApi, 'compile').mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '前一交易日涨停能力不可用',
      status: 422,
      detail: '当前运行时尚未发布前一交易日涨停信号。',
      code: 'previous_session_limit_up_capability_unavailable',
    }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.type(input, original)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText(/我已理解你想用“前一交易日涨停”作为买入条件/))
      .toBeInTheDocument()
    expect(screen.queryByText('这句话暂时不能还原')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改规则' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '使用技术示例' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '改用价格、涨跌幅或技术指标条件')
    await waitFor(() => expect(input).toHaveFocus())
    expect(screen.getByText(original)).toBeInTheDocument()

    const replacement = '东方财富MACD金叉买入，MACD死叉卖出'
    await user.type(input, replacement)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile).toHaveBeenLastCalledWith(expect.objectContaining({ utterance: replacement }))
  })

  it('guides an opinion into explicit A-share strategy choices before allowing a backtest', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'idea-route-draft',
        revision: 4,
        clarification: {
          id: 'idea_guidance_required',
          question: '选一个方向，我会把它变成完整买卖规则再识别。',
          reason: '你在表达对特朗普相关政策的不认同，但还没有给出可回测的买卖条件。',
          ideaRoute: {
            schema_version: 'idea-route.v1',
            understanding: '你在表达对特朗普相关政策的不认同。',
            hypothesis: '先把观点转换成当前股票可检验的价格代理。',
            asset_mapping: {
              instrument_symbol: '300059.SZ',
              relation: 'current_page_proxy',
              rationale: '当前页面是东方财富，只围绕这只 A 股提出候选。',
              evidence_status: 'host_context_only',
            },
            proposals: [{
              id: 'trend-confirmation',
              title: '等趋势确认',
              hypothesis: '价格与趋势同时转强后再进入。',
              entry_summary: 'MACD 金叉且站上 20 日均线',
              exit_summary: 'MACD 死叉',
              suggested_utterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
              capability_ids: ['technical.macd', 'technical.ma'],
              assumptions: ['仅使用价格代理'],
              confidence: 0.82,
            }, {
              id: 'oversold-rebound',
              title: '等超跌反弹',
              hypothesis: '仅在超跌后恢复时进入。',
              entry_summary: 'RSI 低于 30',
              exit_summary: 'RSI 高于 70',
              suggested_utterance: 'RSI 低于 30 买入，RSI 高于 70 卖出，回测近 5 年',
              capability_ids: ['technical.rsi'],
              assumptions: ['仅使用价格代理'],
              confidence: 0.75,
            }],
            research: {
              provider: 'deepseek',
              model: 'deepseek-v4-flash',
              provider_response_id: 'resp-research-1',
              query: '特朗普相关政策',
              purpose: 'viewpoint',
              as_of: '2026-09-04T09:30:00+08:00',
              summary: '公开信息显示，相关政策不确定性仍需持续核实。',
              facts: [{
                statement: '相关政策时间表尚未完全确定。',
                fact_kind: 'policy',
                source_ids: ['src-1'],
                time_scope: '2026-09-04',
              }],
              sources: [{
                source_id: 'src-1',
                title: '权威来源',
                url: 'https://example.com/source',
                publisher: '测试出版方',
                published_at: '2026-09-04',
              }],
              unresolved_questions: [],
              retrieved_at: '2026-09-04T09:31:00+08:00',
              response_sha256: `sha256:${'c'.repeat(64)}`,
              search_call_count: 1,
              schema_version: 'current-fact-research.v1',
            },
          },
          choices: [{
            id: 'trend-confirmation',
            label: '等趋势确认',
            description: '价格与趋势同时转强后再进入。',
            action: 'replace_and_compile',
            suggestedUtterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
            instrumentSymbol: '300059.SZ',
            instrumentName: '东方财富',
          }, {
            id: 'oversold-rebound',
            label: '等超跌反弹',
            description: '仅在超跌后恢复时进入。',
            action: 'replace_and_compile',
            suggestedUtterance: 'RSI 低于 30 买入，RSI 高于 70 卖出，回测近 5 年',
            instrumentSymbol: '300059.SZ',
            instrumentName: '东方财富',
          }],
        },
      })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
      .mockImplementationOnce(async (turn) => ({
        replyKind: 'accepted',
        assistantMessage: '明白，你选的是等趋势确认；股票和退出规则都保留了。',
        suggestions: [],
        outcome: await mockApi.compile({
          ...turn.originalRequest,
          utterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
        }),
      }))
    const revise = vi.spyOn(strategyApi, 'revise')
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '我讨厌特朗普')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const guidance = await screen.findByText('选一个方向，我会把它变成完整买卖规则再识别。')
    expect(guidance.textContent).toBe('选一个方向，我会把它变成完整买卖规则再识别。')
    expect(guidance).not.toHaveTextContent('选一个试试，或说说你想怎么改。')
    const trendProposal = screen.getByRole('button', { name: '等趋势确认' })
    expect(trendProposal).toHaveTextContent('东方财富 · 300059.SZ')
    expect(trendProposal).not.toHaveTextContent('待确认')
    expect(trendProposal).toHaveTextContent('买入MACD 金叉且站上 20 日均线')
    expect(trendProposal).toHaveTextContent('卖出MACD 死叉')
    expect(trendProposal).not.toHaveTextContent('为何相关')
    expect(screen.getByRole('button', { name: '等超跌反弹' })).toBeInTheDocument()
    expect(screen.getByLabelText('联网分析依据')).not.toHaveAttribute('open')
    expect(screen.getByLabelText('联网分析依据')).not.toHaveTextContent('不是回测行情')
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '回复序号，或直接说完整规则')
    await waitFor(() => expect(input).toHaveFocus())
    await user.click(screen.getByText('查看参考来源'))
    expect(screen.getByRole('link', { name: '权威来源' }))
      .toHaveAttribute('href', 'https://example.com/source')
    expect(screen.queryByText('已理解观点：')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始回测' })).not.toBeInTheDocument()
    expect(compile).toHaveBeenCalledTimes(1)
    expect(revise).not.toHaveBeenCalled()

    await user.click(trendProposal)
    await waitFor(() => expect(answerClarification).toHaveBeenCalledTimes(1))
    expect(compile).toHaveBeenCalledTimes(1)
    expect(answerClarification).toHaveBeenCalledWith(expect.objectContaining({
      draftId: 'idea-route-draft',
      revision: 4,
      answer: 'trend-confirmation',
      originalRequest: expect.objectContaining({ utterance: '我讨厌特朗普' }),
    }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(revise).not.toHaveBeenCalled()

    // 回答后选项退出当前交互；完整模型回复留在历史，不再自动补选项提示。
    expect(screen.queryByRole('button', { name: '等趋势确认' })).not.toBeInTheDocument()
    expect(screen.queryByText(/选一个试试，或说说你想怎么改/)).not.toBeInTheDocument()
    expect(screen.getByText('选一个方向，我会把它变成完整买卖规则再识别。')).toBeInTheDocument()
  })

  it('uses a proposal-specific instrument instead of the standalone default', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'multi-instrument-ideas',
        revision: 1,
        clarification: {
          id: 'idea_guidance_required',
          question: '选一只标的和方向继续。',
          reason: '联网分析返回了两个待验证标的。',
          ideaRoute: {
            schema_version: 'idea-route.v1',
            understanding: '两只股票需要分别回测。',
            hypothesis: '先选择具体标的。',
            asset_mapping: {
              instrument_symbol: null,
              relation: 'unbound',
              rationale: '没有沿用页面默认股票。',
              evidence_status: 'instrument_required',
            },
            proposals: [{
              id: 'eastmoney-trend',
              title: '东方财富趋势',
              hypothesis: '用均线验证。',
              entry_summary: '上穿 20 日均线',
              exit_summary: '跌破 20 日均线',
              suggested_utterance: '东方财富上穿20日均线买入，跌破20日均线卖出',
              instrument_symbol: '300059.SZ',
              capability_ids: ['technical.ma'],
              assumptions: [],
              confidence: 0.8,
            }, {
              id: 'hithink-reversal',
              title: '同花顺反转',
              hypothesis: '用 RSI 验证。',
              entry_summary: 'RSI 低于 30',
              exit_summary: 'RSI 高于 70',
              suggested_utterance: '同花顺RSI低于30买入，高于70卖出',
              instrument_symbol: '300033.SZ',
              capability_ids: ['technical.rsi'],
              assumptions: [],
              confidence: 0.75,
            }],
          },
          choices: [{
            id: 'eastmoney-trend',
            label: '东方财富趋势',
            description: '用均线验证。',
            action: 'replace_and_compile',
            suggestedUtterance: '东方财富上穿20日均线买入，跌破20日均线卖出',
            instrumentSymbol: '300059.SZ',
            instrumentName: '东方财富',
          }, {
            id: 'hithink-reversal',
            label: '同花顺反转',
            description: '用 RSI 验证。',
            action: 'replace_and_compile',
            suggestedUtterance: '同花顺RSI低于30买入，高于70卖出',
            instrumentSymbol: '300033.SZ',
            instrumentName: '同花顺',
          }],
        },
      })
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'hithink-strategy',
        revision: 1,
        clarification: {
          id: 'strategy_rule_incomplete',
          question: '补齐规则。',
          reason: '',
          choices: [],
        },
      })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('交易规则'), '给我两个策略')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    const hithink = await screen.findByRole('button', { name: '同花顺反转' })
    expect(hithink).toHaveTextContent('同花顺 · 300033.SZ')
    expect(hithink).not.toHaveTextContent('300059.SZ')
    await user.click(hithink)

    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile).toHaveBeenLastCalledWith(expect.objectContaining({
      instrument: expect.objectContaining({ name: '同花顺', symbol: '300033.SZ' }),
      instrumentContextSource: 'stock_page',
    }))
    expect(answerClarification).not.toHaveBeenCalled()
  })

  it('hides an idea candidate without a complete suggested buy-and-sell sentence', async () => {
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'filtered-idea-route',
      revision: 1,
      clarification: {
        id: 'idea_guidance_required',
        question: '选一个方向继续。',
        reason: '先把观点变成可以核对的规则。',
        ideaRoute: {
          schema_version: 'idea-route.v1',
          understanding: '你想验证一个市场观点。',
          hypothesis: '用价格行为作为代理。',
          asset_mapping: {
            instrument_symbol: '300059.SZ',
            relation: 'current_page_proxy',
            rationale: '当前页面是东方财富。',
            evidence_status: 'host_context_only',
          },
          proposals: [{
            id: 'incomplete-direction',
            title: '只有一句口号',
            hypothesis: '没有完整离场条件。',
            entry_summary: '价格突破 20 日新高',
            exit_summary: '尚未说明',
            suggested_utterance: '价格突破 20 日新高买入',
            capability_ids: ['price.breakout'],
            assumptions: [],
            confidence: 0.3,
          }, {
            id: 'complete-direction',
            title: '趋势确认',
            hypothesis: '等趋势转强后参与。',
            entry_summary: '价格上穿 20 日均线',
            exit_summary: '价格跌破 20 日均线',
            suggested_utterance: '价格上穿20日均线买入，跌破20日均线卖出，回测近1年',
            capability_ids: ['technical.ma'],
            assumptions: [],
            confidence: 0.8,
          }],
        },
        choices: [{
          id: 'incomplete-direction',
          label: '只有一句口号',
          description: '这是不应展示的通用卡。',
          action: 'replace_and_compile',
          suggestedUtterance: '价格突破 20 日新高买入',
        }, {
          id: 'complete-direction',
          label: '趋势确认',
          description: '完整规则。',
          action: 'replace_and_compile',
          suggestedUtterance: '价格上穿20日均线买入，跌破20日均线卖出，回测近1年',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('交易规则'), '我看好东方财富')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByRole('button', { name: '趋势确认' })).toBeVisible()
    expect(screen.queryByRole('button', { name: '只有一句口号' })).not.toBeInTheDocument()
    expect(screen.queryByText('这是不应展示的通用卡。')).not.toBeInTheDocument()
  })

  it('does not leave stale viewpoint choices visible when the second turn fails', async () => {
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'viewpoint-timeout-draft',
      revision: 1,
      clarification: {
        id: 'idea_guidance_required',
        question: '你更想验证哪一种方向？',
        reason: '先把观点变成可以回测的假设。',
        choices: [{
          id: 'trend-confirmation',
          label: '等趋势确认',
          description: 'MACD 金叉且站上 20 日均线买入。',
          action: 'replace_and_compile',
          suggestedUtterance: 'MACD金叉且站上20日均线买入，MACD死叉卖出',
        }],
      },
    })
    vi.spyOn(strategyApi, 'answerClarification').mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '接口响应超时',
      status: 0,
      detail: '回测服务超过 20 秒没有响应，请稍后重试。',
      code: 'api_timeout',
    }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.type(input, '讨厌特朗普')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByRole('button', { name: '等趋势确认' })).toBeVisible()

    await user.type(input, '而且我讨厌wash')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText(/这次识别等待超时了/)).toBeVisible()
    expect(screen.queryByRole('button', { name: '等趋势确认' })).not.toBeInTheDocument()
  })

  it('shows a colloquial trading interpretation as a confirmation-only preview', async () => {
    const source = await strategyApi.compile({
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
      utterance: 'RSI低于30买入，高于70卖出，回测近1年',
    })
    if (source.status !== 'compiled') throw new Error('missing mock strategy')
    const originalCompile = strategyApi.compile
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'colloquial-preview',
      revision: 1,
      clarification: {
        id: 'idea_guidance_required',
        question: '我把你的说法先还原成一个可验证的假设，你可以确认或换一个方向。',
        reason: '“低买高卖”需要落到明确的进出场条件才可以回测。',
        choices: [{
          id: 'rsi_reversal',
          label: '检验超跌后的反转',
          description: 'RSI 低于 30 买入，高于 70 卖出。',
          action: 'replace_and_compile',
          suggestedUtterance: 'RSI低于30买入，高于70卖出，回测近1年',
        }, {
          id: 'ma20_trend',
          label: '等趋势确认后参与',
          description: '上穿 20 日均线买入，跌破卖出。',
          action: 'replace_and_compile',
          suggestedUtterance: '上穿20日均线买入，跌破20日均线卖出，回测近1年',
        }],
        provisionalDraft: source.draft,
        provisionalChoiceId: 'rsi_reversal',
        provisionalNote: '我把你说的口语表达暂时理解成这条规则；请确认后再回测。',
      },
    })
    const answer = vi.spyOn(strategyApi, 'answerClarification').mockImplementationOnce(async (turn) => ({
      replyKind: 'accepted',
      assistantMessage: '好的，按这个规则继续。',
      suggestions: [],
      outcome: await originalCompile({
        ...turn.originalRequest,
        utterance: turn.answer,
      }),
    }))
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('交易规则'), '东方财富低买高卖')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByTestId('provisional-strategy')).toHaveTextContent('RSI 低于 30')
    expect(screen.queryByRole('button', { name: '开始回测' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '检验超跌后的反转' }))
      .toHaveTextContent('RSI 低于 30 买入，高于 70 卖出。')
    await user.click(screen.getByRole('button', { name: '检验超跌后的反转' }))
    await waitFor(() => expect(answer).toHaveBeenCalledWith(expect.objectContaining({
      answer: 'RSI低于30买入，高于70卖出，回测近1年',
    })))
    expect(compile).toHaveBeenCalledTimes(1)
  })

  it('keeps the grounded instrument in plain-text directions for an ambiguous indicator', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'ambiguous-cross-draft',
        revision: 2,
        clarification: {
          id: 'ambiguous_cross_indicator',
          question: '“金叉/死叉”指的是哪一类指标？',
          reason: '已确认标的为汤姆猫，但金叉可能指多种指标。',
          ideaRoute: {
            schema_version: 'idea-route.v1',
            understanding: '你想在汤姆猫出现金叉时买入，死叉时卖出。',
            hypothesis: '先确认金叉所属指标。',
            asset_mapping: {
              instrument_symbol: '300459.SZ',
              relation: 'current_page_proxy',
              rationale: '服务端证券主数据已确认汤姆猫。',
              evidence_status: 'host_context_only',
            },
            proposals: [{
              id: 'macd-cross',
              title: 'MACD 金叉 / 死叉',
              hypothesis: '用 MACD 的 DIF 与 DEA 交叉确认方向。',
              entry_summary: 'MACD 金叉',
              exit_summary: 'MACD 死叉',
              suggested_utterance: 'MACD金叉买入，MACD死叉卖出，回测近1年',
              capability_ids: ['technical.macd'],
              assumptions: [],
              confidence: 0.9,
            }, {
              id: 'kdj-cross',
              title: 'KDJ 金叉 / 死叉',
              hypothesis: '用 KDJ 的 K 线与 D 线交叉确认方向。',
              entry_summary: 'KDJ 金叉',
              exit_summary: 'KDJ 死叉',
              suggested_utterance: 'KDJ金叉买入，KDJ死叉卖出，回测近1年',
              capability_ids: ['technical.kdj'],
              assumptions: [],
              confidence: 0.85,
            }],
          },
          choices: [{
            id: 'macd-cross',
            label: 'MACD 金叉 / 死叉',
            description: 'MACD 金叉买入，MACD 死叉卖出',
            action: 'replace_and_compile',
            suggestedUtterance: 'MACD金叉买入，MACD死叉卖出，回测近1年',
            instrumentSymbol: '300459.SZ',
            instrumentName: '汤姆猫',
          }, {
            id: 'kdj-cross',
            label: 'KDJ 金叉 / 死叉',
            description: 'KDJ 金叉买入，KDJ 死叉卖出',
            action: 'replace_and_compile',
            suggestedUtterance: 'KDJ金叉买入，KDJ死叉卖出，回测近1年',
            instrumentSymbol: '300459.SZ',
            instrumentName: '汤姆猫',
          }],
        },
      })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
      .mockImplementationOnce(async (turn) => ({
        replyKind: 'accepted',
        assistantMessage: '明白，你说的是 MACD 金叉和死叉，汤姆猫这只股票也已经保留。',
        suggestions: [],
        outcome: await mockApi.compile({
          ...turn.originalRequest,
          utterance: '汤姆猫MACD金叉买入，MACD死叉卖出，回测近1年',
        }),
      }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '汤姆猫金叉买死叉卖')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    const guidance = await screen.findByText('“金叉/死叉”指的是哪一类指标？')
    expect(guidance.textContent).toBe('“金叉/死叉”指的是哪一类指标？')
    expect(screen.getByRole('button', { name: 'MACD 金叉 / 死叉' })).toBeInTheDocument()
    expect(input).toHaveValue('')
    await waitFor(() => expect(input).toHaveFocus())

    const macdProposal = screen.getByRole('button', { name: 'MACD 金叉 / 死叉' })
    expect(macdProposal).toHaveTextContent('汤姆猫 · 300459.SZ')
    expect(macdProposal).toHaveTextContent('买入MACD 金叉')
    expect(macdProposal).toHaveTextContent('卖出MACD 死叉')
    await user.click(macdProposal)

    await waitFor(() => expect(answerClarification).toHaveBeenCalledTimes(1))
    expect(compile).toHaveBeenCalledTimes(1)
    expect(answerClarification).toHaveBeenCalledWith(expect.objectContaining({
      draftId: 'ambiguous-cross-draft',
      revision: 2,
      answer: 'macd-cross',
      originalRequest: expect.objectContaining({ utterance: '汤姆猫金叉买死叉卖' }),
    }))
    expect(await screen.findByText('MACD 金叉 / 死叉', { selector: '.bubble.me p' })).toBeVisible()
  })

  it('does not present uncovered catalog events as runnable event strategies', async () => {
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '东方财富最终中标后买入，MACD 死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText(/我还没能把这句话还原成完整的买卖规则/)).toBeInTheDocument()
    expect(screen.queryByText('无法识别这条策略')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改规则' })).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('no_supported_signal_recognized')
    expect(screen.queryByText('最终中标')).not.toBeInTheDocument()
  })

  it('returns a bare indicator name to the input instead of inventing a strategy', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, 'MACD')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    const prompt = await screen.findByText('请一次写清什么时候买入、什么时候卖出。')
    expect(prompt.textContent).toBe('请一次写清什么时候买入、什么时候卖出。')
    expect(screen.queryByText('只问这一次')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补充完整规则/ })).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    await waitFor(() => expect(input).toHaveFocus())
    expect(compile).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/用到 \d+ 个条件/)).not.toBeInTheDocument()
  })

  it('returns a missing-exit clarification to the original input without inventing a rule', async () => {
    const originalCompile = strategyApi.compile
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_missing_exit',
      revision: 5,
      clarification: {
        id: 'exit_rule_not_recognized',
        question: '已识别买入条件。你想在什么条件下卖出？',
        reason: '卖出条件决定何时结束持仓。系统不会替你补一条默认策略。',
        choices: [{
          id: 'edit-utterance',
          label: '补充卖出条件',
          description: '返回输入框，在原话后补充明确的卖出条件。',
          recommended: true,
          action: 'edit_utterance',
        }],
      },
    })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
      .mockImplementationOnce(async (turn) => ({
        replyKind: 'accepted',
        assistantMessage: '明白，已经保留年度报告买入条件，并接上 MACD 死叉卖出。',
        suggestions: [],
        outcome: await originalCompile({
          ...turn.originalRequest,
          utterance: '年度报告发布后买入，MACD死叉卖出',
        }),
      }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '年度报告发布后买入')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const prompt = await screen.findByText(/系统不会替你补一条默认策略/)
    expect(prompt).toHaveTextContent('已识别买入条件')
    expect(screen.queryByText('只问这一次')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /使用东方财富/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补充卖出条件/ })).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '补充什么时候卖出')
    await waitFor(() => expect(input).toHaveFocus())

    await user.type(input, 'MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(answerClarification).toHaveBeenCalledTimes(1))
    expect(compile).toHaveBeenCalledTimes(1)
    expect(answerClarification).toHaveBeenCalledWith(expect.objectContaining({
      draftId: 'draft_missing_exit',
      revision: 5,
      answer: 'MACD死叉卖出',
      originalRequest: expect.objectContaining({ utterance: '年度报告发布后买入' }),
    }))
    expect(await screen.findByText(/已经保留年度报告买入条件/)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: '开始回测' })).toBeInTheDocument()
  })

  it('returns a missing-entry clarification with the correct buy direction', async () => {
    const pendingClarification = {
      id: 'entry_rule_not_recognized',
      question: '已识别卖出条件。你想在什么条件下买入？',
      reason: '买入条件决定什么时候建立持仓。系统不会替你补一条默认策略。',
      choices: [{
        id: 'edit-utterance',
        label: '补充买入条件',
        description: '返回输入框，在原话前补充明确的买入条件。',
        recommended: true,
        action: 'edit_utterance' as const,
      }],
    }
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_missing_entry',
      revision: 6,
      clarification: pendingClarification,
    })
    const answerClarification = vi.spyOn(strategyApi, 'answerClarification')
      .mockResolvedValueOnce({
        replyKind: 'clarification',
        assistantMessage: '听到了，你想在 RSI 较低时买入。MACD 死叉卖出已经保留；现在只差：请确认 RSI 的买入阈值。',
        suggestions: [{
          id: 'idea_aaaaaaaaaaaa',
          title: 'RSI 低于 30 买入',
          preview: 'RSI低于30买入，MACD死叉卖出',
        }, {
          id: 'idea_bbbbbbbbbbbb',
          title: 'RSI 低于 25 买入',
          preview: 'RSI低于25买入，MACD死叉卖出',
        }],
        outcome: {
          status: 'needs_clarification',
          draftId: 'draft_missing_entry',
          revision: 6,
          clarification: pendingClarification,
        },
      })
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, 'MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    const prompt = await screen.findByText(/已识别卖出条件/)
    expect(prompt).toHaveTextContent('你想在什么条件下买入')
    expect(screen.queryByText('只问这一次')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补充买入条件/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补充卖出条件/ })).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '补充什么时候买入')
    await waitFor(() => expect(input).toHaveFocus())

    await user.type(input, 'RSI低于30买入')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(answerClarification).toHaveBeenCalledTimes(1))
    expect(compile).toHaveBeenCalledTimes(1)
    expect(answerClarification).toHaveBeenCalledWith(expect.objectContaining({
      draftId: 'draft_missing_entry',
      revision: 6,
      answer: 'RSI低于30买入',
      originalRequest: expect.objectContaining({ utterance: 'MACD死叉卖出' }),
    }))
    const followUp = await screen.findByText(/RSI 的买入阈值/)
    expect(followUp).not.toHaveTextContent('1.')
    expect(screen.getByRole('button', { name: 'RSI 低于 30 买入' }))
      .toHaveTextContent('RSI低于30买入，MACD死叉卖出')
    expect(screen.getByRole('button', { name: 'RSI 低于 25 买入' }))
      .toHaveTextContent('RSI低于25买入，MACD死叉卖出')
    expect(input).toHaveValue('')
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('runs the financial example without dropping its PE condition', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByRole('textbox', { name: '交易规则' }), '东方财富PE低于35且MACD金叉买入，MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(screen.getAllByText('市盈率 < 35').length).toBeGreaterThan(0)
    expect(screen.getAllByText('MACD 金叉').length).toBeGreaterThan(0)
    expect(screen.getAllByText('MACD 死叉').length).toBeGreaterThan(0)
  })

  it('exposes a user-cancelled run as an actionable terminal state', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText(/^预览：/)).toBeInTheDocument()
    const runProgress = screen.getByRole('status', { name: '回测进度' })
    expect(runProgress.closest('.thinking-stream')).not.toHaveClass('mcard')
    expect(runProgress.closest('.thinking-stream')).toHaveTextContent('处理过程')
    await user.click(await screen.findByRole(
      'button',
      { name: '取消回测' },
      { timeout: 4_000 },
    ))
    expect(await screen.findByText('用户取消')).toBeInTheDocument()
    expect(screen.getByText(/任务已停止/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '修改规则' })).toBeEnabled()
  })

  it('removes redundant follow-up actions without surfacing capital on the home journey', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { container } = renderApp()

    expectHomeToHideDefaultCapital(container)
    await user.type(screen.getByLabelText('交易规则'), VOLUME_EXAMPLE)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)

    // The redundant footer and local-only reminder receipt are removed.
    expect(screen.queryByRole('group', { name: '可选的下一步' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '把这条设成盯盘提醒' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '收起策略审阅' }))
    // Desktop rail visibility is verified in the real browser, not jsdom.
    await user.click(screen.getByRole('button', { name: '新建策略', hidden: true }))
    expectHomeToHideDefaultCapital(container)
    const nextInput = screen.getByLabelText('交易规则') as HTMLInputElement
    expect(nextInput.value).not.toContain('【')
    expect(screen.queryByText('盯盘提醒已设置')).not.toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)
  }, 10_000)
})
