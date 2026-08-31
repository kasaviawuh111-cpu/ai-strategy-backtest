import { mockApi } from './mock'
import type { CompileRequest } from './types'

const request = (utterance: string): CompileRequest => ({
  utterance,
  instrument: {
    name: '东方财富',
    symbol: '300059.SZ',
    market: 'CN_A',
    exchange: 'SZSE',
  },
})

describe('mock API trust boundary', () => {
  it('keeps the trend example as an explicit MACD and MA20 conjunction', async () => {
    const outcome = await mockApi.compile(request(
      '东方财富 MACD 刚金叉，而且股价也站上 20 日线了就买入；MACD 死叉就卖出，看看近 5 年效果',
    ))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled trend strategy')

    expect(outcome.draft.strategySpec.entry).toMatchObject({
      type: 'all',
      children: [
        { type: 'indicator_condition', indicator_id: 'technical.macd', trigger: 'golden_cross' },
        { type: 'indicator_condition', indicator_id: 'technical.ma', trigger: 'price_crosses_above', params: { period: 20 } },
      ],
    })
    expect(outcome.draft.entry.conditions.map((condition) => condition.label)).toEqual([
      'MACD 金叉',
      '收盘突破 20 日均线',
    ])
  })

  it('keeps the earnings-forecast example as an event and MACD conjunction', async () => {
    const outcome = await mockApi.compile(request(
      '东方财富 业绩预告发布且 MACD 金叉时买入，MACD 死叉卖出，回测近 5 年',
    ))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled earnings strategy')

    expect(outcome.draft.strategySpec.entry).toMatchObject({
      type: 'all',
      children: [
        { type: 'event_condition', event_code: 'event.financial_results.earnings_forecast_published' },
        { type: 'indicator_condition', indicator_id: 'technical.macd', trigger: 'golden_cross' },
      ],
    })
    expect(outcome.draft.entry.conditions.map((condition) => condition.label)).toEqual([
      '业绩预告发布',
      'MACD 金叉',
    ])
    expect(outcome.draft.entry.conditions).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ eventCode: 'event.financial_results.annual_report' }),
    ]))
  })

  it('rejects arbitrary language instead of silently creating a MACD strategy', async () => {
    await expect(mockApi.compile(request('火星逆行时满仓，月圆时卖出'))).rejects.toMatchObject({
      problem: {
        status: 422,
        code: 'no_supported_signal_recognized',
        detail: '没有识别到当前可执行的技术指标或公告事件。请写清何时买入、何时卖出和回测区间。',
      },
    })
  })

  it('rejects event families without current acquisition coverage', async () => {
    await expect(mockApi.compile(request('季报发布后买入，MACD 死叉卖出'))).rejects.toMatchObject({
      problem: { code: 'no_supported_signal_recognized' },
    })
    await expect(mockApi.compile(request('业务许可获批后买入，MACD 死叉卖出'))).rejects.toMatchObject({
      problem: { code: 'no_supported_signal_recognized' },
    })
    await expect(mockApi.compile(request('半年度报告发布后买入，MACD 死叉卖出'))).rejects.toMatchObject({
      problem: { code: 'no_supported_signal_recognized' },
    })
  })

  it('keeps annual report as the only runnable event example', async () => {
    const outcome = await mockApi.compile(request('年度报告发布后买入，MACD 死叉卖出'))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled annual-report strategy')
    expect(outcome.draft.entry.conditions).toEqual([
      expect.objectContaining({
        kind: 'event',
        eventCode: 'event.financial_results.annual_report',
      }),
    ])
  })

  it.each([
    '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出',
    '同花顺年度报告正文中 AI 出现大于 5 次就买入，买入后第 3 个交易日卖出',
    '同花顺年报包含 AI 超过5次建仓，3个交易日后平仓',
  ])('recognizes the bounded annual-report text-count rule: %s', async (utterance) => {
    const outcome = await mockApi.compile(request(utterance))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled document strategy')

    expect(outcome.draft.entry.conditions).toEqual([
      expect.objectContaining({
        kind: 'event',
        eventCode: 'event.financial_results.annual_report',
        label: '年度报告正文中“AI”完整词出现 > 5 次',
        documentText: expect.objectContaining({
          term: 'AI', comparator: 'gt', value: 5, match_mode: 'ascii_token',
        }),
      }),
    ])
    expect(outcome.draft.exit.conditions).toEqual([
      expect.objectContaining({
        kind: 'holding_period',
        sessions: 3,
        anchor: 'first_entry_fill',
        label: '实际买入成交后第 3 个交易日卖出',
      }),
    ])
    expect(outcome.draft.warnings).toEqual(expect.arrayContaining([
      expect.stringContaining('没有读取年度报告正文'),
    ]))
  })

  it('does not invent entry and exit rules for a bare indicator name', async () => {
    const pending = await mockApi.compile(request('MACD'))
    expect(pending.status).toBe('needs_clarification')
    if (pending.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(pending.clarification).toMatchObject({
      id: 'strategy_rule_incomplete',
      choices: [{ action: 'edit_utterance', label: '补充完整规则' }],
    })
  })

  it('fails closed for an unknown mock clarification answer', async () => {
    await expect(mockApi.compile({
      ...request('东方财富 MACD 金叉买入，死叉卖出'),
      clarification: { id: 'unknown', choiceId: 'guess' },
    })).rejects.toMatchObject({
      problem: { code: 'clarification_choice_not_supported' },
    })
  })

  it('never fabricates replayable identity or proved evidence for mock results', async () => {
    const outcome = await mockApi.compile(request('东方财富 MACD 金叉买入，死叉卖出'))
    if (outcome.status !== 'compiled') throw new Error('expected a compiled MACD strategy')
    const run = await mockApi.createRun(outcome.draft)
    const summary = await mockApi.getSummary(run.id)

    expect(run.fingerprint).toBe('mock:demo-only:not-replayable')
    expect(run.progressLabel).toMatch(/^预览：/)
    expect(summary.runEvidence).toBeNull()
    expect(summary.warnings).toEqual(expect.arrayContaining([
      expect.stringContaining('固定样例'),
      expect.stringContaining('不来自回测服务'),
    ]))
  })

  it('labels every mock progress stage as a demonstration', async () => {
    const outcome = await mockApi.compile(request('东方财富 MACD 金叉买入，死叉卖出'))
    if (outcome.status !== 'compiled') throw new Error('expected a compiled MACD strategy')
    const run = await mockApi.createRun(outcome.draft)

    for (let poll = 0; poll < 5; poll += 1) {
      const current = await mockApi.getRun(run.id)
      expect(current.progressLabel).toMatch(/^预览/)
    }
  })
})
