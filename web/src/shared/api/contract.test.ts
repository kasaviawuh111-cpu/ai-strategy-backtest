import {
  dataAsOfDate,
  fromLiveDraftResponse,
  mergeLiveRevision,
  toLiveBacktestBody,
  toLiveCompileBody,
  toLiveRevisionBody,
} from './contract'
import type { LiveDraftResponse } from './contract'
import type {
  CapabilitiesResponse,
  CompileRequest,
  IndicatorCapability,
  StrategyCondition,
  StrategyDraft,
  StrategySpec,
  StrategySpecIndicatorCondition,
} from './types'
import { toStrategySummary } from '../../view-model'

const capabilities = (overrides: Partial<CapabilitiesResponse> = {}): CapabilitiesResponse => ({
  markets: ['CN_A'],
  input_modes: ['natural_language_zh'],
  strategy_scopes: ['single_instrument', 'long_only'],
  indicators: [],
  events: [],
  execution_policies: ['next_tradable_session_open'],
  event_catalog_status: 'published',
  event_backtest_available: false,
  event_preparation_available: false,
  event_availability_scope: 'unavailable',
  backtest_execution_available: false,
  limits: {
    max_body_bytes: 16_384,
    max_utterance_characters: 2000,
    max_instrument_context_characters: 32,
  },
  ...overrides,
})

const conditionOrderCapability = (
  indicatorId: 'price.close' | 'price.return_pct',
  displayName: string,
): IndicatorCapability => {
  const triggers = ['crosses_above', 'crosses_below', 'above', 'below', 'at_least', 'at_most']
  return {
    indicator_id: indicatorId,
    definition_version: '1.0.0',
    status: 'stable',
    display_name: displayName,
    description: displayName,
    warmup_bars: indicatorId === 'price.close' ? 2 : 7,
    timeframes: ['1d'],
    evaluation_modes: ['bar_close_confirmed'],
    triggers,
    parameters: [],
    trigger_definitions: triggers.map((id) => ({
      id,
      display_name: null,
      description: null,
      value_requirement: 'required',
      minimum: null,
      maximum: null,
      unit: null,
      exclusive_minimum: false,
      exclusive_maximum: false,
    })),
  }
}

const request: CompileRequest = {
  utterance: '东方财富 MACD 金叉买入，死叉卖出，回测近 5 年',
  instrument: {
    name: '东方财富',
    symbol: '300059.SZ',
    market: 'CN_A',
    exchange: 'SZSE',
  },
}

const strategy: StrategySpec = {
  schema_version: 'strategy.v1',
  catalog: { catalog_id: 'cn_a.signals', release_version: '2026.08.29' },
  instrument: { market: 'CN_A', symbol: '300059.SZ', position_mode: 'long_only' },
  entry: {
    type: 'indicator_condition',
    indicator_id: 'technical.macd',
    definition_version: '1.0.0',
    params: { fast: 12, slow: 26, signal: 9 },
    timeframe: '1d',
    evaluation_mode: 'bar_close_confirmed',
    trigger: 'golden_cross',
    value: null,
  },
  exit: {
    op: 'first_of',
    children: [{
      type: 'indicator_condition',
      indicator_id: 'technical.macd',
      definition_version: '1.0.0',
      params: { fast: 12, slow: 26, signal: 9 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'death_cross',
      value: null,
    }],
  },
  execution: {
    timezone: 'Asia/Shanghai',
    entry_policy: 'next_tradable_session_open',
    exit_policy: 'next_tradable_session_open',
    data_capability: 'daily_ohlcv',
    execution_resolution: '1d',
    evaluation_frequency: '1d_close',
    position_policy: 'single_position_no_pyramiding',
    t_plus_one: true,
  },
  backtest: { start: '2021-08-06', end: '2026-08-06', initial_cash_cny: 100_000 },
}

const response: LiveDraftResponse = {
  draft_id: '8c91eb84-ab49-4b0c-890a-682e9cc6fe21',
  revision: 1,
  status: 'ready',
  strategy,
  strategy_hash: `sha256:${'a'.repeat(64)}`,
  clarification: null,
  diagnostic_code: null,
  provenance: [],
  candidate_provenance: null,
  candidate_grounding: null,
  candidate_alternatives: [],
  candidate_rejections: [],
  created_at: '2026-08-28T00:00:00Z',
}

const withFastParameter = (condition: StrategyCondition, value: number): StrategyCondition =>
  condition.kind !== 'indicator'
    ? condition
    : {
        ...condition,
        parameters: condition.parameters.map((parameter) =>
          parameter.key === 'fast' ? { ...parameter, value } : parameter,
        ),
      }

describe('live API contract adapter', () => {
  it('uses the configured data boundary and backend compile field names', () => {
    expect(dataAsOfDate()).toBe('2026-08-06')
    expect(toLiveCompileBody(request)).toEqual({
      utterance: request.utterance,
      instrument_context: '300059.SZ',
      as_of_date: '2026-08-06',
    })
  })

  it('does not send the standalone demo stock as authoritative context', () => {
    expect(toLiveCompileBody({
      ...request,
      instrumentContextSource: 'standalone_default',
      utterance: '同花顺ROE高于0%买入，MACD死叉卖出，回测近1年',
    })).toEqual({
      utterance: '同花顺ROE高于0%买入，MACD死叉卖出，回测近1年',
      instrument_context: null,
      as_of_date: '2026-08-06',
    })
  })

  it('answers the only backend v2 clarification through instrument_context', () => {
    expect(toLiveCompileBody({
      ...request,
      clarification: {
        id: 'instrument_required',
        choiceId: 'use-current-instrument',
      },
    })).toEqual({
      utterance: request.utterance,
      instrument_context: '300059.SZ',
      as_of_date: '2026-08-06',
    })
  })

  it('preserves the backend clarification code so the selected answer can round-trip', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '请确认要回测哪一只 A 股。',
      diagnostic_code: 'instrument_required',
    }, request)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'instrument_required',
        choices: [{ id: 'use-current-instrument', action: 'submit_clarification' }],
      },
    })
  })

  it('asks for a code instead of offering the demo stock in standalone mode', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '请确认要回测哪一只 A 股。',
      diagnostic_code: 'instrument_required',
    }, { ...request, instrumentContextSource: 'standalone_default' })

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        question: '请确认要回测哪一只 A 股。',
        choices: [],
      },
    })
    if (outcome.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(outcome.clarification.recognized).not.toContainEqual(expect.objectContaining({
      label: '股票',
      value: expect.stringContaining('东方财富'),
    }))
  })

  it('turns an unresolved instrument diagnostic into the same typed clarification', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'unsupported',
      strategy: null,
      strategy_hash: null,
      clarification: null,
      diagnostic_code: 'instrument_unconfirmed',
    }, { ...request, instrumentContextSource: 'standalone_default' })

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'instrument_unconfirmed',
        question: '请直接输入股票名称或 6 位证券代码，我会继续沿用刚才的买卖规则。',
        choices: [],
      },
    })
  })

  it('uses the grounded company name when the validated strategy changes symbol', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      strategy: {
        ...strategy,
        instrument: { ...strategy.instrument, symbol: '300033.SZ' },
      },
      candidate_grounding: {
        matched_spans: ['同花顺'],
        spans: [{ path: '/instrument/symbol', start: 0, end: 3, text: '同花顺' }],
      },
    }, {
      ...request,
      instrumentContextSource: 'standalone_default',
      utterance: '同花顺ROE高于0%买入，MACD死叉卖出',
    })

    if (outcome.status !== 'compiled') throw new Error('expected compiled strategy')
    expect(outcome.draft.instrument).toMatchObject({
      name: '同花顺',
      symbol: '300033.SZ',
      exchange: 'SZSE',
    })
  })

  it('turns a missing exit into an edit action instead of a stock choice or default rule', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '已识别买入条件。你想在什么条件下卖出？',
      diagnostic_code: 'exit_rule_not_recognized',
    }, request)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'exit_rule_not_recognized',
        reason: expect.stringContaining('不能替你定'),
        choices: [{
          id: 'edit-utterance',
          label: '补充卖出条件',
          action: 'edit_utterance',
        }],
      },
    })
    expect(outcome).not.toMatchObject({
      clarification: { choices: [{ id: 'use-current-instrument' }] },
    })
  })

  it('turns a missing entry into a buy-condition edit instead of asking for another exit', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '已识别卖出条件。你想在什么条件下买入？',
      diagnostic_code: 'entry_rule_not_recognized',
    }, request)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'entry_rule_not_recognized',
        reason: expect.stringContaining('不能替你定'),
        choices: [{
          id: 'edit-utterance',
          label: '补充买入条件',
          description: expect.stringContaining('什么时候买'),
          action: 'edit_utterance',
        }],
      },
    })
    expect(outcome).not.toMatchObject({
      clarification: { choices: [{ label: '补充卖出条件' }] },
    })
  })

  it('returns an incomplete indicator name to editing without inventing entry and exit rules', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '请一次补充什么时候买入、什么时候卖出。',
      diagnostic_code: 'strategy_rule_incomplete',
    }, request)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'strategy_rule_incomplete',
        choices: [{
          id: 'edit-utterance',
          label: '补充完整规则',
          action: 'edit_utterance',
        }],
      },
    })
  })

  it('maps a structured missing-entry clarification into replacement choices and uses its grounded reason', () => {
    const guidedResponse: LiveDraftResponse = {
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '选一个方向，我会把它变成完整买卖规则再识别。',
      diagnostic_code: 'entry_rule_not_recognized',
      idea_route: {
        schema_version: 'idea-route.v1',
        understanding: '你已经说清楚跌破 20 日线卖出，但还没说什么时候买入；我不会替你选择进场条件。',
        hypothesis: '把观点转换成当前股票可检验的价格代理，而不是假设观点直接导致股价变化。',
        asset_mapping: {
          instrument_symbol: '300059.SZ',
          relation: 'current_page_proxy',
          rationale: '当前页面是东方财富，因此只围绕这只 A 股提出候选。',
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
      },
    }
    const outcome = fromLiveDraftResponse(guidedResponse, request)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'entry_rule_not_recognized',
        reason: '你已经说清楚跌破 20 日线卖出，但还没说什么时候买入；我不会替你选择进场条件。',
        ideaRoute: {
          schema_version: 'idea-route.v1',
          asset_mapping: { instrument_symbol: '300059.SZ' },
        },
        choices: [{
          id: 'trend-confirmation',
          label: '等趋势确认',
          description: '价格与趋势同时转强后再进入；补充买入：MACD 金叉且站上 20 日均线',
          action: 'replace_and_compile',
          suggestedUtterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
          instrumentSymbol: '300059.SZ',
        }, {
          id: 'oversold-rebound',
          label: '等超跌反弹',
          action: 'replace_and_compile',
          suggestedUtterance: 'RSI 低于 30 买入，RSI 高于 70 卖出，回测近 5 年',
          instrumentSymbol: '300059.SZ',
        }],
      },
    })
    const exitOutcome = fromLiveDraftResponse({
      ...guidedResponse,
      diagnostic_code: 'exit_rule_not_recognized',
    }, request)
    expect(exitOutcome.status).toBe('needs_clarification')
    if (exitOutcome.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(exitOutcome.clarification.choices[0]).toMatchObject({
      id: 'trend-confirmation',
      description: '价格与趋势同时转强后再进入；补充卖出：MACD 死叉',
    })
    for (const diagnosticCode of ['strategy_rule_incomplete', 'ambiguous_cross_indicator']) {
      const completeOutcome = fromLiveDraftResponse({
        ...guidedResponse,
        diagnostic_code: diagnosticCode,
      }, request)
      expect(completeOutcome.status).toBe('needs_clarification')
      if (completeOutcome.status !== 'needs_clarification') throw new Error('expected clarification')
      expect(completeOutcome.clarification.choices[0]).toMatchObject({
        id: 'trend-confirmation',
        description: '价格与趋势同时转强后再进入；买入：MACD 金叉且站上 20 日均线；卖出：MACD 死叉',
      })
    }
    const remappedOutcome = fromLiveDraftResponse({
      ...guidedResponse,
      candidate_grounding: {
        matched_spans: ['汤姆猫'],
        spans: [{ path: '/instrument/symbol', start: 0, end: 3, text: '汤姆猫' }],
      },
      idea_route: {
        ...guidedResponse.idea_route!,
        asset_mapping: {
          ...guidedResponse.idea_route!.asset_mapping,
          instrument_symbol: '300459.SZ',
        },
      },
    }, request)
    expect(remappedOutcome.status).toBe('needs_clarification')
    if (remappedOutcome.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(remappedOutcome.clarification.choices.map((choice) => choice.instrumentSymbol))
      .toEqual(['300459.SZ', '300459.SZ'])
    expect(remappedOutcome.clarification.choices.map((choice) => choice.instrumentName))
      .toEqual(['汤姆猫', '汤姆猫'])
    expect(outcome).not.toHaveProperty('draft')
  })

  it('fails closed when idea guidance has fewer than two complete directions', () => {
    expect(() => fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: null,
      diagnostic_code: 'idea_guidance_required',
      idea_route: {
        schema_version: 'idea-route.v1',
        understanding: '一个观点',
        hypothesis: '一个待检验假设',
        asset_mapping: {
          instrument_symbol: '300059.SZ',
          relation: 'current_page_proxy',
          rationale: '仅使用当前股票页上下文。',
          evidence_status: 'host_context_only',
        },
        proposals: [],
      },
    }, request)).toThrow('服务没有返回至少两个可供选择的完整策略方向')
  })

  it('shows the model analysis for an unbound viewpoint without guessing a stock', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '选一个验证方向，并告诉我想回测的具体 A 股。',
      diagnostic_code: 'idea_guidance_required',
      idea_route: {
        schema_version: 'idea-route.v1',
        understanding: '你在表达对特朗普相关政策的不认同。',
        hypothesis: '如果这种政策不确定性影响风险偏好，价格趋势或超跌反弹可能出现可检验差异。',
        asset_mapping: {
          instrument_symbol: null,
          relation: 'unbound',
          rationale: '尚未绑定证券；选择方向后仍需补充具体 A 股。',
          evidence_status: 'instrument_required',
        },
        proposals: [{
          id: 'idea_000000000001',
          title: '等趋势确认后参与',
          hypothesis: '用趋势行为代理检验。',
          entry_summary: '股价上穿 20 日均线',
          exit_summary: '股价跌破 20 日均线',
          suggested_utterance: '股价上穿20日均线买入，跌破20日均线卖出，回测近1年',
          capability_ids: ['technical.ma'],
          assumptions: ['尚未绑定证券'],
          confidence: 0.75,
        }, {
          id: 'idea_000000000002',
          title: '检验超跌后的反转',
          hypothesis: '用超跌反弹代理检验。',
          entry_summary: 'RSI 低于 30',
          exit_summary: 'RSI 高于 70',
          suggested_utterance: 'RSI低于30买入，高于70卖出，回测近1年',
          capability_ids: ['technical.rsi'],
          assumptions: ['尚未绑定证券'],
          confidence: 0.75,
        }],
      },
    }, { ...request, instrumentContextSource: 'standalone_default' })

    expect(outcome.status).toBe('needs_clarification')
    if (outcome.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(outcome.clarification.reason).toContain('你在表达对特朗普相关政策的不认同')
    expect(outcome.clarification.reason).toContain('如果这种政策不确定性影响风险偏好')
    expect(outcome.clarification.reason).toContain('尚未绑定证券')
    expect(outcome.clarification.choices.every((choice) => choice.instrumentSymbol === undefined))
      .toBe(true)
  })

  it('fails closed for clarification choices that the backend v2 request cannot represent', () => {
    expect(() => toLiveCompileBody({
      ...request,
      clarification: {
        id: 'macd_confirmation_time',
        choiceId: 'intrabar',
      },
    })).toThrow('这个补充选项不在当前后端契约内')
  })

  it('turns unsupported compiler responses into an actionable API error', () => {
    expect(() => fromLiveDraftResponse({
      ...response,
      status: 'unsupported',
      strategy: null,
      strategy_hash: null,
      diagnostic_code: 'no_supported_signal_recognized',
    }, request)).toThrow('没有识别到当前可执行的技术指标或公告事件')
  })

  it('explains why a previous-session limit-up entry cannot be approximated', () => {
    expect(() => fromLiveDraftResponse({
      ...response,
      status: 'unsupported',
      strategy: null,
      strategy_hash: null,
      diagnostic_code: 'previous_session_limit_up_capability_unavailable',
    }, request)).toThrow('前一交易日涨停、下一交易日买入')
  })

  it.each([
    ['event_document_variant_not_executable', '摘要、更正版、英文版'],
    ['event_execution_timing_not_executable', '下一可交易日开盘'],
    ['event_retry_policy_not_executable', '一次成交尝试'],
    ['event_numeric_filter_not_executable', '公告金额、比例或业绩阈值'],
    ['event_qualifier_not_consumed', '限定没有完整进入策略'],
  ])('explains fail-closed event semantics for %s', (diagnosticCode, message) => {
    expect(() => fromLiveDraftResponse({
      ...response,
      status: 'unsupported',
      strategy: null,
      strategy_hash: null,
      diagnostic_code: diagnosticCode,
    }, request)).toThrow(message)
  })

  it('maps a ready StrategySpec and sends the edited spec plus execution config', () => {
    const outcome = fromLiveDraftResponse(response, request)
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled strategy')

    const edited: StrategyDraft = {
      ...outcome.draft,
      backtest: {
        start: '2022-01-04',
        end: '2026-07-31',
        initialCashCny: 200_000,
      },
      execution: {
        ...outcome.draft.execution,
        capacityMode: 'unlimited',
        participationRate: 0.08,
        slippageBps: 8,
        allocationRatio: 0.75,
        retryUnfilledExits: false,
        maxExitAttempts: 7,
        warmupCalendarDays: 365,
        settlementExtensionDays: 30,
        runRobustness: false,
      },
      entry: {
        ...outcome.draft.entry,
        conditions: outcome.draft.entry.conditions.map((condition) => withFastParameter(condition, 10)),
      },
      exit: {
        ...outcome.draft.exit,
        conditions: outcome.draft.exit.conditions.map((condition) => withFastParameter(condition, 10)),
      },
    }
    const revisionBody = toLiveRevisionBody(edited)

    expect(revisionBody.utterance).toBe(request.utterance)
    expect(revisionBody.strategy.backtest).toEqual({
      start: '2022-01-04',
      end: '2026-07-31',
      initial_cash_cny: 200_000,
    })
    expect(revisionBody.strategy.entry.type).toBe('indicator_condition')
    if (revisionBody.strategy.entry.type === 'indicator_condition') {
      expect(revisionBody.strategy.entry.params.fast).toBe(10)
    }
    const saved = mergeLiveRevision(
      { ...response, revision: 2, strategy: revisionBody.strategy },
      edited,
    )
    const body = toLiveBacktestBody(saved)

    expect(body.strategy).toMatchObject({
      schema_version: 'strategy.v1',
      backtest: { start: '2022-01-04', end: '2026-07-31', initial_cash_cny: 200_000 },
    })
    expect(body.config).toEqual({
      capacityMode: 'unlimited',
      participationRate: 0.08,
      slippageBps: 8,
      allocationRatio: 0.75,
      limitHandling: 'wait_for_unlock',
      commissionRate: 0.0003,
      minimumCommissionCny: 5,
      retryUnfilledExits: false,
      maxExitAttempts: 7,
      warmupCalendarDays: 365,
      settlementExtensionDays: 30,
      runRobustness: false,
    })
    expect(body.strategy.entry.type).toBe('indicator_condition')
    if (body.strategy.entry.type === 'indicator_condition') {
      expect(body.strategy.entry.params.fast).toBe(10)
    }
    const savedExit = body.strategy.exit.children[0]
    expect(savedExit?.type).toBe('indicator_condition')
    if (savedExit?.type === 'indicator_condition') {
      expect(savedExit.params.fast).toBe(10)
    }
    expect(saved.strategySpec).toEqual(revisionBody.strategy)
    expect(saved.revision).toBe(2)
  })

  it('keeps a Beijing Stock Exchange instrument inside the A-share boundary', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      strategy: {
        ...strategy,
        instrument: { ...strategy.instrument, symbol: '920001.BJ' },
      },
    }, request)

    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled strategy')
    expect(outcome.draft.instrument).toMatchObject({
      symbol: '920001.BJ',
      market: 'CN_A',
      exchange: 'BSE',
    })
  })

  it('keeps the real moving-average period visible in the review card', () => {
    const movingAverageCondition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'technical.ma',
      definition_version: '1.0.0',
      params: { period: 20, price_field: 'close' },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'price_crosses_above',
      value: null,
    }
    const movingAverageStrategy: StrategySpec = {
      ...strategy,
      entry: movingAverageCondition,
      exit: {
        op: 'first_of',
        children: [{ ...movingAverageCondition, trigger: 'price_crosses_below' }],
      },
    }
    const outcome = fromLiveDraftResponse(
      { ...response, strategy: movingAverageStrategy },
      { ...request, utterance: '股价突破 20 日均线买入，跌破均线卖出' },
      capabilities({
        indicators: [{
          indicator_id: 'technical.ma',
          definition_version: '1.0.0',
          status: 'stable',
          triggers: ['price_crosses_above', 'price_crosses_below'],
          display_name: '均线', description: '移动平均线', warmup_bars: 20,
          timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
          parameters: [{
            name: 'period',
            value_type: 'integer', required: true, default: 20,
            minimum: 1, maximum: 500, choices: [], display_name: '周期',
            unit: '交易日',
          }],
          trigger_definitions: [
            {
              id: 'price_crosses_above',
              display_name: '收盘突破', description: '收盘价由下向上穿过均线',
              value_requirement: 'forbidden', minimum: null, maximum: null, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            },
            {
              id: 'price_crosses_below',
              display_name: '收盘跌破', description: '收盘价由上向下穿过均线',
              value_requirement: 'forbidden', minimum: null, maximum: null, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            },
          ],
        }],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled moving-average strategy')

    expect(outcome.draft.title).toBe('MA 规则 · 日线')
    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      indicatorId: 'technical.ma',
      label: '收盘突破 20 日均线',
      trigger: '收盘价由下向上穿过均线',
      parameters: expect.arrayContaining([expect.objectContaining({
        key: 'period',
        label: '周期',
        value: 20,
        min: 1,
        max: 500,
        unit: '交易日',
      })]),
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      indicatorId: 'technical.ma',
      label: '收盘跌破 20 日均线',
    })
  })

  it('uses the executable catalog bounds for moving-average cross periods', () => {
    const movingAverageCrossCondition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'technical.ma_cross',
      definition_version: '1.0.0',
      params: { fast_period: 10, slow_period: 30 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'golden_cross',
      value: null,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry: movingAverageCrossCondition,
          exit: {
            op: 'first_of',
            children: [{ ...movingAverageCrossCondition, trigger: 'death_cross' }],
          },
        },
      },
      { ...request, utterance: '短期均线上穿长期均线买入，下穿卖出' },
      capabilities({
        indicators: [{
          indicator_id: 'technical.ma_cross',
          definition_version: '1.0.0',
          status: 'stable',
          triggers: ['golden_cross', 'death_cross'],
          display_name: '双均线', description: '双均线交叉', warmup_bars: 30,
          timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
          parameters: [
            {
              name: 'fast_period',
              value_type: 'integer', required: true, default: 10,
              minimum: 1, maximum: 500, choices: [], display_name: '快线周期',
              unit: '交易日',
            },
            {
              name: 'slow_period',
              value_type: 'integer', required: true, default: 30,
              minimum: 2, maximum: 1_000, choices: [], display_name: '慢线周期',
              unit: '交易日',
            },
          ],
          trigger_definitions: [],
        }],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled moving-average cross strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      indicatorId: 'technical.ma_cross',
      parameters: expect.arrayContaining([
        expect.objectContaining({ key: 'fast_period', integer: true, min: 1, max: 500 }),
        expect.objectContaining({ key: 'slow_period', integer: true, min: 2, max: 1_000 }),
      ]),
    })
  })

  it('renders a newly published indicator from the current FastAPI capability fields', () => {
    const serverDefinedCondition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'technical.server_defined_demo',
      definition_version: '2.1.0',
      params: { lookback: 7 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'turns_up',
      value: 1.5,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry: serverDefinedCondition,
          exit: {
            op: 'first_of',
            children: [{ ...serverDefinedCondition, trigger: 'turns_down', value: null }],
          },
        },
      },
      { ...request, utterance: '服务端新指标转强且超过1.5时买入，转弱卖出' },
      capabilities({
        indicators: [{
          indicator_id: 'technical.server_defined_demo',
          definition_version: '2.1.0',
          status: 'stable',
          triggers: ['turns_up', 'turns_down'],
          display_name: '服务端新指标', description: '服务端新指标说明', warmup_bars: 7,
          timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
          parameters: [{
            name: 'lookback',
            value_type: 'integer', required: true, default: 7,
            minimum: 2, maximum: 120, choices: [], display_name: '观察窗口',
            unit: '交易日',
          }],
          trigger_definitions: [
            {
              id: 'turns_up',
              display_name: '转强', description: '服务端定义的转强条件',
              value_requirement: 'required',
              minimum: 0, maximum: 10, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            },
            {
              id: 'turns_down',
              display_name: '转弱', description: '服务端定义的转弱条件',
              value_requirement: 'forbidden',
              minimum: null, maximum: null, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            },
          ],
        }],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled server-defined strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      indicatorId: 'technical.server_defined_demo',
      label: '服务端新指标 转强 1.5',
      trigger: '服务端定义的转强条件',
      parameters: [
        expect.objectContaining({
          key: 'lookback',
          label: '观察窗口',
          value: 7,
          min: 2,
          max: 120,
          unit: '交易日',
        }),
        expect.objectContaining({ key: '$value', label: '转强阈值', min: 0, max: 10 }),
      ],
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      label: '服务端新指标 转弱',
      trigger: '服务端定义的转弱条件',
    })
  })

  it('exposes the new price and amount indicator parameters with readable units', () => {
    const averageAmountCondition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'amount.average',
      definition_version: '1.0.0',
      params: { period: 5 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'above',
      value: 100_000_000,
    }
    const rollingHighCondition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'price.rolling_high',
      definition_version: '1.0.0',
      params: { period: 20, price_field: 'close' },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'new_high',
      value: null,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry: averageAmountCondition,
          exit: { op: 'first_of', children: [rollingHighCondition] },
        },
      },
      { ...request, utterance: '5日平均成交额超过1亿元买入，创20日新高卖出' },
      capabilities({
        indicators: [
          {
            indicator_id: 'amount.average',
            definition_version: '1.0.0',
            status: 'stable',
            triggers: ['above'],
            display_name: '平均成交额', description: '区间平均成交额', warmup_bars: 5,
            timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
            parameters: [{
              name: 'period',
              value_type: 'integer', required: true, default: 5,
              minimum: 1, maximum: 500, choices: [], display_name: '平均周期',
              unit: '交易日',
            }],
            trigger_definitions: [{
              id: 'above',
              display_name: '高于', description: '当前平均成交额高于设定阈值',
              value_requirement: 'required',
              minimum: 0, maximum: 10_000_000_000_000, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            }],
          },
          {
            indicator_id: 'price.rolling_high',
            definition_version: '1.0.0',
            status: 'stable',
            triggers: ['new_high'],
            display_name: '阶段新高', description: '滚动区间新高', warmup_bars: 20,
            timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
            parameters: [{
              name: 'period',
              value_type: 'integer', required: true, default: 20,
              minimum: 2, maximum: 5_000, choices: [], display_name: '观察周期',
              unit: '交易日',
            }],
            trigger_definitions: [{
              id: 'new_high',
              display_name: '创新高', description: '严格高于此前完整区间最高价',
              value_requirement: 'forbidden',
              minimum: null, maximum: null, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            }],
          },
        ],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled price/amount strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      indicatorId: 'amount.average',
      label: '平均成交额 高于 100000000元',
      parameters: expect.arrayContaining([
        expect.objectContaining({ key: 'period', label: '平均周期', value: 5 }),
        expect.objectContaining({
          key: '$value',
          label: '高于阈值',
          value: 100_000_000,
          min: 0,
          max: 10_000_000_000_000,
        }),
      ]),
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      indicatorId: 'price.rolling_high',
      label: '阶段新高 创新高',
      trigger: '严格高于此前完整区间最高价',
    })
  })

  it('shows catalog names, comparisons, values, and units for generic threshold indicators', () => {
    const turnover: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'market.turnover_rate',
      definition_version: '1.0.0',
      params: {},
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'above',
      value: 5,
    }
    const rsi: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'technical.rsi',
      definition_version: '1.0.0',
      params: { period: 14 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'below',
      value: 30,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry: turnover,
          exit: { op: 'first_of', children: [rsi] },
        },
      },
      { ...request, utterance: '东方财富换手率高于5%买入，RSI低于30卖出，回测近1年' },
      capabilities({
        indicators: [{
          indicator_id: 'market.turnover_rate',
          definition_version: '1.0.0',
          status: 'stable',
          display_name: '换手率',
          description: '供应商原始换手率',
          warmup_bars: 2,
          timeframes: ['1d'],
          evaluation_modes: ['bar_close_confirmed'],
          triggers: ['above'],
          parameters: [],
          trigger_definitions: [{
            id: 'above',
            display_name: '高于',
            description: '换手率高于设定阈值',
            value_requirement: 'required',
            minimum: 0,
            maximum: null,
            unit: '%',
            exclusive_minimum: false,
            exclusive_maximum: false,
          }],
        }, {
          indicator_id: 'technical.rsi',
          definition_version: '1.0.0',
          status: 'stable',
          display_name: 'RSI',
          description: '相对强弱指标',
          warmup_bars: 16,
          timeframes: ['1d'],
          evaluation_modes: ['bar_close_confirmed'],
          triggers: ['below'],
          parameters: [{
            name: 'period',
            value_type: 'integer',
            required: true,
            default: 14,
            minimum: 2,
            maximum: 1000,
            choices: [],
            display_name: '周期',
            unit: '交易日',
          }],
          trigger_definitions: [{
            id: 'below',
            display_name: '低于',
            description: 'RSI 低于设定阈值',
            value_requirement: 'required',
            minimum: 0,
            maximum: 100,
            unit: null,
            exclusive_minimum: false,
            exclusive_maximum: false,
          }],
        }],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected generic threshold strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      label: '换手率 高于 5%',
      parameters: [expect.objectContaining({ key: '$value', value: 5, unit: '%' })],
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      label: 'RSI 低于 30',
      parameters: expect.arrayContaining([
        expect.objectContaining({ key: '$value', value: 30 }),
      ]),
    })
    expect(toStrategySummary(outcome.draft).rows).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: '买入', value: '换手率 高于 5%' }),
      expect.objectContaining({ label: '卖出', value: 'RSI 低于 30' }),
    ]))
  })

  it('shows absolute-price condition orders with their thresholds in Chinese', () => {
    const entry: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'price.close',
      definition_version: '1.0.0',
      params: {},
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'crosses_above',
      value: 19,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry,
          exit: {
            op: 'first_of',
            children: [{ ...entry, trigger: 'crosses_below', value: 17.8 }],
          },
        },
      },
      { ...request, utterance: '东方财富涨超19块买，17.8卖，回测近1年' },
      capabilities({
        indicators: [conditionOrderCapability('price.close', '收盘价阈值')],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled price strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      label: '收盘价 上穿 19 元',
      trigger: '日线收盘确认：收盘价 上穿 19 元',
      parameters: [expect.objectContaining({ key: '$value', value: 19, unit: '元' })],
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      label: '收盘价 下穿 17.8 元',
    })
    expect(toStrategySummary(outcome.draft).rows).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: '买入', value: '收盘价 上穿 19 元' }),
      expect.objectContaining({ label: '卖出', value: '收盘价 下穿 17.8 元' }),
    ]))
  })

  it('shows one-session rise and decline conditions with values instead of raw triggers', () => {
    const entry: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'price.return_pct',
      definition_version: '1.0.0',
      params: { period: 1, price_field: 'close' },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'at_least',
      value: 5,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry,
          exit: {
            op: 'first_of',
            children: [{ ...entry, trigger: 'at_most', value: -5 }],
          },
        },
      },
      { ...request, utterance: '东方财富当日上涨5%买入，当日下跌5%卖出，回测近1年' },
      capabilities({
        indicators: [conditionOrderCapability('price.return_pct', '区间涨跌幅')],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled return strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      label: '当日涨幅 ≥ 5%',
      parameters: expect.arrayContaining([
        expect.objectContaining({ key: '$value', value: 5, unit: '%' }),
      ]),
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      label: '当日跌幅 ≥ 5%',
    })
    expect(toStrategySummary(outcome.draft).rows).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: '买入', value: '当日涨幅 ≥ 5%' }),
      expect.objectContaining({ label: '卖出', value: '当日跌幅 ≥ 5%' }),
    ]))
  })

  it('exposes volume, divergence, and trend parameters with catalog bounds', () => {
    const relativeVolume: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'volume.relative',
      definition_version: '1.0.0',
      params: { baseline_period: 20, consecutive_days: 3 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'consecutive_gte_multiple',
      value: 1.2,
    }
    const divergence: StrategySpecIndicatorCondition = {
      type: 'indicator_condition',
      indicator_id: 'volume.price_divergence',
      definition_version: '1.0.0',
      params: {
        left_bars: 3,
        right_bars: 3,
        min_separation: 5,
        max_separation: 60,
        price_threshold_pct: 2,
        obv_threshold_adv: 1,
        average_volume_period: 20,
      },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'bearish',
      value: null,
    }
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry: relativeVolume,
          exit: { op: 'first_of', children: [divergence] },
        },
      },
      { ...request, utterance: '持续放量买入，量价顶背离卖出' },
      capabilities({
        indicators: [
          {
            indicator_id: 'volume.relative',
            definition_version: '1.0.0',
            status: 'stable',
            triggers: ['consecutive_gte_multiple'],
            display_name: '相对成交量', description: '相对成交量', warmup_bars: 20,
            timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
            parameters: [
              {
                name: 'baseline_period',
                value_type: 'integer', required: true, default: 20,
                minimum: 1, maximum: 5_000, choices: [], display_name: '前序基准周期',
                unit: '交易日',
              },
              {
                name: 'consecutive_days',
                value_type: 'integer', required: true, default: 3,
                minimum: 1, maximum: 500, choices: [], display_name: '连续天数',
                unit: '交易日',
              },
            ],
            trigger_definitions: [{
              id: 'consecutive_gte_multiple',
              display_name: '连续达到倍数', description: '连续若干交易日达到相对成交量阈值',
              value_requirement: 'required',
              minimum: 0, maximum: 100, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            }],
          },
          {
            indicator_id: 'volume.price_divergence',
            definition_version: '1.0.0',
            status: 'stable',
            triggers: ['bearish'],
            display_name: '量价背离', description: '量价背离', warmup_bars: 60,
            timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
            parameters: [{
              name: 'max_separation',
              value_type: 'integer', required: true, default: 60,
              minimum: 1, maximum: 5_000, choices: [], display_name: '最大间隔',
              unit: '交易日',
            }],
            trigger_definitions: [{
              id: 'bearish',
              display_name: '顶背离', description: '价格走强而成交量能量未同步确认',
              value_requirement: 'forbidden', minimum: null, maximum: null, unit: null,
              exclusive_minimum: false, exclusive_maximum: false,
            }],
          },
        ],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled volume strategy')

    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      indicatorId: 'volume.relative',
      label: '相对成交量 连续达到倍数 1.2倍',
      parameters: expect.arrayContaining([
        expect.objectContaining({ key: 'baseline_period', label: '前序基准周期', value: 20 }),
        expect.objectContaining({ key: 'consecutive_days', label: '连续天数', value: 3 }),
        expect.objectContaining({ key: '$value', label: '连续达到倍数阈值', value: 1.2 }),
      ]),
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      indicatorId: 'volume.price_divergence',
      label: '量价背离 顶背离',
      parameters: expect.arrayContaining([
        expect.objectContaining({ key: 'max_separation', min: 1, max: 5_000 }),
      ]),
    })
  })

  it('keeps an event leaf intact and exposes its first-available timing in the UI model', () => {
    const eventStrategy: StrategySpec = {
      ...strategy,
      entry: {
        type: 'event_condition',
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        trigger: 'published',
        attributes: {},
      },
      execution: {
        ...strategy.execution,
        data_capability: 'daily_ohlcv_events',
        evaluation_frequency: 'event_available_plus_1d_close',
      },
    }
    const outcome = fromLiveDraftResponse(
      { ...response, strategy: eventStrategy },
      { ...request, utterance: '东方财富年报发布后买入，MACD死叉卖出' },
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled event strategy')

    expect(outcome.draft.title).toBe('年度报告 + MACD 规则 · 日线')
    expect(outcome.draft.execution).toMatchObject({
      dataCapability: 'daily_ohlcv_events',
      evaluationFrequency: 'event_available_plus_1d_close',
    })
    expect(outcome.draft.entry.conditions).toEqual([{
      id: 'entry',
      kind: 'event',
      eventCode: 'event.financial_results.annual_report',
      label: '年度报告发布',
      trigger: '按首次可获得时间确认',
      attributes: {},
    }])
    const edited: StrategyDraft = {
      ...outcome.draft,
      exit: {
        ...outcome.draft.exit,
        conditions: outcome.draft.exit.conditions.map((condition) => withFastParameter(condition, 8)),
      },
      backtest: {
        start: '2023-01-03',
        end: '2026-08-06',
        initialCashCny: 180_000,
      },
    }
    const revisionBody = toLiveRevisionBody(edited)
    expect(revisionBody.strategy.entry).toEqual(eventStrategy.entry)

    const saved = mergeLiveRevision(
      { ...response, revision: 2, strategy: revisionBody.strategy },
      edited,
    )
    expect(saved.strategySpec.entry).toEqual(eventStrategy.entry)
    expect(saved.entry.conditions).toEqual(outcome.draft.entry.conditions)
    expect(toLiveBacktestBody(saved).strategy).toEqual(revisionBody.strategy)
  })

  it('keeps a direct financial condition intact in the live strategy card', () => {
    const financialStrategy: StrategySpec = {
      ...strategy,
      entry: {
        type: 'financial_condition',
        metric_id: 'valuation.pe',
        definition_version: '1.0.0',
        report_type: null,
        period_basis: 'point_in_time',
        statement_scope: null,
        revision_policy: 'as_known_at_signal',
        comparator: 'lt',
        value: '20',
        unit: 'TIMES',
      },
      execution: {
        ...strategy.execution,
        data_capability: 'daily_ohlcv_financials',
        evaluation_frequency: 'financial_available_plus_1d_close',
      },
    }
    const outcome = fromLiveDraftResponse(
      { ...response, strategy: financialStrategy },
      { ...request, utterance: '东方财富市盈率低于 20 买入，MACD 死叉卖出' },
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled financial strategy')

    expect(outcome.draft.title).toBe('市盈率 PE + MACD 规则 · 日线')
    expect(outcome.draft.execution).toMatchObject({
      dataCapability: 'daily_ohlcv_financials',
      evaluationFrequency: 'financial_available_plus_1d_close',
    })
    expect(outcome.draft.entry.conditions).toEqual([{
      id: 'entry',
      kind: 'financial',
      metricId: 'valuation.pe',
      label: '市盈率 PE < 20',
      trigger: '只使用历史当时已可得的接口原始值，日线收盘确认',
      comparator: 'lt',
      value: '20',
      unit: 'TIMES',
      reportType: null,
      periodBasis: 'point_in_time',
    }])
    expect(toLiveRevisionBody(outcome.draft).strategy).toEqual(financialStrategy)
    expect(toLiveBacktestBody(outcome.draft).strategy).toEqual(financialStrategy)
  })

  it('maps report text count and fill-anchored trading-session exit without losing the DSL', () => {
    const documentStrategy: StrategySpec = {
      ...strategy,
      instrument: { ...strategy.instrument, symbol: '300033.SZ' },
      entry: {
        type: 'event_condition',
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        trigger: 'published',
        attributes: {},
        document_text: {
          metric_id: 'document.literal_mention_count',
          metric_version: '1.0.0',
          term: 'AI',
          normalization: 'nfkc',
          match_mode: 'ascii_token',
          case_sensitive: false,
          comparator: 'gt',
          value: 5,
        },
      },
      exit: {
        op: 'first_of',
        children: [{
          type: 'holding_period_exit',
          sessions: 3,
          anchor: 'first_entry_fill',
          count_mode: 'subsequent_trading_sessions',
          execution: 'target_session_open_proxy',
        }],
      },
      execution: {
        ...strategy.execution,
        data_capability: 'daily_ohlcv_events',
        evaluation_frequency: 'event_available_plus_1d_close',
      },
    }
    const documentRequest: CompileRequest = {
      instrument: {
        name: '同花顺',
        symbol: '300033.SZ',
        market: 'CN_A',
        exchange: 'SZSE',
      },
      utterance: '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出',
    }
    const outcome = fromLiveDraftResponse(
      { ...response, strategy: documentStrategy },
      documentRequest,
      capabilities({
        events: [{
          event_code: 'event.financial_results.annual_report',
          definition_version: '1.0.0',
          catalog_status: 'stable',
          status: 'unavailable',
          backtest_available: false,
          preparation_available: true,
          availability_scope: 'request_preparation',
          unavailable_reason: 'preparation_required',
          triggers: ['published'],
        }],
      }),
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled document strategy')

    expect(outcome.draft.instrument).toMatchObject({ name: '同花顺', symbol: '300033.SZ' })
    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      kind: 'event',
      label: '年度报告正文中“AI”完整词出现 > 5 次',
      documentText: { term: 'AI', comparator: 'gt', value: 5 },
    })
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      kind: 'holding_period',
      sessions: 3,
      label: '实际买入成交后第 3 个交易日卖出',
    })
    expect(toLiveRevisionBody(outcome.draft).strategy).toEqual(documentStrategy)
    expect(toLiveBacktestBody(outcome.draft).strategy).toEqual(documentStrategy)
  })

  it('preserves every position-aware exit rule as read-only Live StrategySpec semantics', () => {
    const positionExitStrategy: StrategySpec = {
      ...strategy,
      exit: {
        op: 'first_of',
        children: [
          {
            type: 'position_return_exit',
            trigger: 'take_profit',
            threshold_pct: 33,
            anchor: 'first_entry_fill',
            observation: 'back_adjusted_daily_close',
            evaluation_mode: 'bar_close_confirmed',
            execution: 'next_tradable_session_open',
          },
          {
            type: 'position_return_exit',
            trigger: 'stop_loss',
            threshold_pct: 8,
            anchor: 'first_entry_fill',
            observation: 'back_adjusted_daily_close',
            evaluation_mode: 'bar_close_confirmed',
            execution: 'next_tradable_session_open',
          },
          {
            type: 'trailing_drawdown_exit',
            threshold_pct: 3,
            anchor: 'first_entry_fill',
            peak_basis: 'back_adjusted_daily_close',
            evaluation_mode: 'bar_close_confirmed',
            execution: 'next_tradable_session_open',
          },
        ],
      },
    }
    const outcome = fromLiveDraftResponse(
      { ...response, strategy: positionExitStrategy },
      { ...request, utterance: 'MACD金叉买入，盈利33%后回撤3%卖出，亏损8%止损' },
    )
    if (outcome.status !== 'compiled') throw new Error('expected a compiled position-exit strategy')

    expect(outcome.draft.exit.conditions).toEqual([
      expect.objectContaining({
        kind: 'position_return', exitTrigger: 'take_profit', thresholdPct: 33,
        label: '持仓收益达到 33% 止盈', editable: false,
      }),
      expect.objectContaining({
        kind: 'position_return', exitTrigger: 'stop_loss', thresholdPct: 8,
        label: '持仓亏损达到 8% 止损', editable: false,
      }),
      expect.objectContaining({
        kind: 'trailing_drawdown', thresholdPct: 3,
        label: '持仓后收盘高点回撤 3% 卖出', editable: false,
      }),
    ])
    expect(toLiveRevisionBody(outcome.draft).strategy).toEqual(positionExitStrategy)
    expect(toLiveBacktestBody(outcome.draft).strategy).toEqual(positionExitStrategy)
  })

  it('keeps a recognized event visible while current capabilities mark it unavailable', () => {
    const eventCapabilities = capabilities({
      events: [{
        event_code: 'event.financial_results.quarterly_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'unavailable',
        backtest_available: false,
        preparation_available: false,
        availability_scope: 'unavailable',
        unavailable_reason: 'snapshot_coverage_unavailable',
        triggers: ['published'],
      }],
    })
    const outcome = fromLiveDraftResponse(
      {
        ...response,
        strategy: {
          ...strategy,
          entry: {
            type: 'event_condition',
            event_code: 'event.financial_results.quarterly_report',
            definition_version: '1.0.0',
            trigger: 'published',
            attributes: {},
          },
          execution: {
            ...strategy.execution,
            data_capability: 'daily_ohlcv_events',
            evaluation_frequency: 'event_available_plus_1d_close',
          },
        },
      },
      { ...request, utterance: '季报发布后买入，MACD 死叉卖出' },
      eventCapabilities,
    )
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a compiled event strategy')
    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      eventCode: 'event.financial_results.quarterly_report',
      label: '季度报告发布',
      trigger: '按首次可获得时间确认',
    })
    expect(eventCapabilities.events[0]).toMatchObject({
      backtest_available: false,
      preparation_available: false,
      availability_scope: 'unavailable',
    })
  })

  it('keeps recognized screenshot-sentence facts in a single low-confidence clarification', () => {
    const screenshotRequest: CompileRequest = {
      instrument: {
        name: '同花顺',
        symbol: '300033.SZ',
        market: 'CN_A',
        exchange: 'SZSE',
      },
      utterance: '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出',
    }
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '已保留识别结果，请确认“3天”是交易日还是自然日。',
      diagnostic_code: 'candidate_provider_low_confidence',
    }, screenshotRequest)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      clarification: {
        id: 'candidate_provider_low_confidence',
        question: '已保留识别结果，请确认“3天”是交易日还是自然日。',
        choices: [expect.objectContaining({ action: 'edit_utterance' })],
        recognized: [
          { label: '股票', value: '同花顺（原话提及；代码仍待服务端确认）' },
          { label: '事件', value: '年度报告发布' },
          { label: '正文条件', value: '完整词“ai”出现 > 5 次' },
          { label: '持有期', value: '3 天（自然日还是交易日待确认）' },
        ],
      },
    })
  })

  it('preserves candidate evidence only when the live response actually supplies it', () => {
    const withoutEvidence = fromLiveDraftResponse(response, request)
    if (withoutEvidence.status !== 'compiled') throw new Error('expected a compiled strategy')
    expect(withoutEvidence.draft).not.toHaveProperty('interpretationEvidence')

    const withEvidence = fromLiveDraftResponse({
      ...response,
      candidate_provenance: {
        source: 'bounded_provider',
        provider: 'candidate-parser-v2',
        model: 'bounded-model',
        prompt_version: 'zh-bounded.v1',
        schema_version: 'bounded-candidate.v1',
        capability_projection_version: 'candidate-capabilities.v1',
        capability_projection_hash: `sha256:${'b'.repeat(64)}`,
        upstream_pattern_commit: 'e90b6c6cd9fea23067a85667e7fbf74f9d73ea48',
        candidate_rank: 1,
      },
      candidate_grounding: {
        matched_spans: ['MACD 金叉买入', '死叉卖出'],
        spans: [
          { path: '/entry/0', start: 0, end: 8, text: 'MACD 金叉买入' },
          { path: '/exit/0', start: 9, end: 13, text: '死叉卖出' },
        ],
      },
      candidate_alternatives: [{ candidate_rank: 2, strategy_hash: `sha256:${'c'.repeat(64)}` }],
      candidate_rejections: [{ candidate_rank: 3, diagnostic_code: 'below_threshold' }],
    }, request)
    if (withEvidence.status !== 'compiled') throw new Error('expected a compiled strategy')

    expect(withEvidence.draft.interpretationEvidence).toEqual({
      candidateProvenance: expect.objectContaining({
        source: 'bounded_provider', provider: 'candidate-parser-v2', candidate_rank: 1,
      }),
      grounding: {
        matched_spans: ['MACD 金叉买入', '死叉卖出'],
        spans: [
          { path: '/entry/0', start: 0, end: 8, text: 'MACD 金叉买入' },
          { path: '/exit/0', start: 9, end: 13, text: '死叉卖出' },
        ],
      },
      alternatives: [{ candidate_rank: 2, strategy_hash: `sha256:${'c'.repeat(64)}` }],
      rejections: [{ candidate_rank: 3, diagnostic_code: 'below_threshold' }],
    })
  })

  it('preserves a non-periodic event for the live capability gate to decide', () => {
    const outcome = fromLiveDraftResponse(response, request)
    if (outcome.status !== 'compiled') throw new Error('expected a compiled strategy')
    const unsafeDraft: StrategyDraft = {
      ...outcome.draft,
      strategySpec: {
        ...outcome.draft.strategySpec,
        entry: {
          type: 'event_condition',
          event_code: 'event.contracts_orders.major_contract_won',
          definition_version: '1.0.0',
          trigger: 'published',
          attributes: {},
        },
      },
    }

    expect(toLiveRevisionBody(unsafeDraft).strategy.entry).toMatchObject({
      event_code: 'event.contracts_orders.major_contract_won',
    })
    expect(toLiveBacktestBody(unsafeDraft).strategy.entry).toMatchObject({
      event_code: 'event.contracts_orders.major_contract_won',
    })
  })
})
