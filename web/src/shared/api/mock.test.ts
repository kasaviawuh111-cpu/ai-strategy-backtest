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
  it('keeps the default Mock draft, result, series, and activities inside the one-year window', async () => {
    const outcome = await mockApi.compile(request('东方财富 MACD 金叉买入，死叉卖出'))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled MACD strategy')

    expect(outcome.draft.backtest).toMatchObject({
      start: '2025-08-06',
      end: '2026-08-06',
    })
    expect(outcome.draft.strategySpec.backtest).toMatchObject({
      start: '2025-08-06',
      end: '2026-08-06',
    })

    const run = await mockApi.createRun(outcome.draft)
    const [summary, series, activities] = await Promise.all([
      mockApi.getSummary(run.id),
      mockApi.getSeries(run.id),
      mockApi.getActivities(run.id),
    ])
    expect(summary.dataRange).toEqual({
      start: '2025-08-06',
      end: '2026-08-06',
      sessions: 243,
    })
    expect(series.at(0)?.date).toBe('2025-08-06')
    expect(series.at(-1)?.date).toBe('2026-08-06')
    expect(activities.every((activity) => (
      activity.occurredAt >= '2025-08-06T00:00:00+08:00'
      && activity.occurredAt <= '2026-08-06T23:59:59+08:00'
    ))).toBe(true)
  })

  it('keeps the trend example as an explicit MACD and MA20 conjunction', async () => {
    const outcome = await mockApi.compile(request(
      '东方财富 MACD 刚金叉，而且股价也站上 20 日线了就买入；MACD 死叉就卖出，看看近 1 年效果',
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

  it('keeps the volume-breakout example as a 20-day-high and relative-volume conjunction', async () => {
    const outcome = await mockApi.compile(request(
      '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    ))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled volume-breakout strategy')

    expect(outcome.draft.strategySpec.entry).toMatchObject({
      type: 'all',
      children: [
        {
          type: 'indicator_condition', indicator_id: 'price.rolling_high',
          trigger: 'new_high', params: { period: 20, price_field: 'close' },
        },
        {
          type: 'indicator_condition', indicator_id: 'volume.relative',
          trigger: 'gte_multiple', value: 1.5, params: { baseline_period: 20 },
        },
      ],
    })
    expect(outcome.draft.entry.conditions.map((condition) => condition.label)).toEqual([
      '创 20 日新高',
      '放量 1.5 倍',
    ])
    expect(outcome.draft.entry.conditions).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'event' }),
    ]))
    expect(outcome.draft.strategySpec.exit.children).toEqual([
      expect.objectContaining({
        type: 'indicator_condition', indicator_id: 'technical.ma', trigger: 'price_crosses_below',
      }),
    ])
  })

  it('keeps the financial example as a direct PE value plus MACD conjunction', async () => {
    const outcome = await mockApi.compile(request(
      '东方财富PE低于35且MACD金叉买入，MACD死叉卖出',
    ))
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled financial strategy')

    expect(outcome.draft.strategySpec.entry).toMatchObject({
      type: 'all',
      children: [
        {
          type: 'financial_condition', metric_id: 'valuation.pe', comparator: 'lt',
          value: 35, unit: 'TIMES', period_basis: 'point_in_time',
        },
        { type: 'indicator_condition', indicator_id: 'technical.macd', trigger: 'golden_cross' },
      ],
    })
    expect(outcome.draft.entry.conditions.map((condition) => condition.label)).toEqual([
      '市盈率 < 35',
      'MACD 金叉',
    ])
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

  it('keeps clarification merging inside the Mock API instead of the App', async () => {
    const originalRequest = request('MACD')
    const pending = await mockApi.compile(originalRequest)
    if (pending.status !== 'needs_clarification') throw new Error('expected clarification')

    const answer = await mockApi.answerClarification({
      draftId: pending.draftId,
      revision: pending.revision,
      answer: 'MACD 金叉买入，MACD 死叉卖出',
      originalRequest,
      clarification: pending.clarification,
    })

    expect(answer.replyKind).toBe('accepted')
    expect(answer.outcome.status).toBe('compiled')
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
