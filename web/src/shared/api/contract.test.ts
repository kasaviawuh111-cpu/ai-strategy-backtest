import {
  dataAsOfDate,
  fromLiveClarificationAnswerResponse,
  fromLiveDraftResponse,
  ideaProposalRuleSummaries,
  ideaProposalTitle,
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
  IdeaRouteProposal,
  StrategyCondition,
  StrategyDraft,
  StrategySpec,
  StrategySpecIndicatorCondition,
  PricePlan,
} from './types'
import { toStrategySummary } from '../../view-model'
import { DEFAULT_EXECUTION_SETTINGS } from '../config/backtest'

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
  it.each([true, false])('uses structured candidate comparisons without rewriting the source (bound=%s)', (bound) => {
    const condition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition', indicator_id: 'technical.adx', definition_version: '1.0.0',
      params: { period: 14 }, timeframe: '1d', evaluation_mode: 'bar_close_confirmed',
      trigger: 'above', value: 25,
    }
    const rules: StrategySpec = { ...strategy,
      entry: { type: 'all', children: [condition, { type: 'not', child: {
        ...condition, indicator_id: 'price.return_pct', params: { period: 5, price_field: 'close' },
        trigger: 'at_most', value: 12,
      } }] }, exit: { op: 'first_of', children: [{ ...condition, trigger: 'below', value: 20 }] },
    }
    const proposal: IdeaRouteProposal = {
      id: 'server-choice', title: '待选择的多条件建议', hypothesis: '待回测验证',
      entry_summary: 'ADX达到25并保证不过热', exit_summary: 'ADX达到20就止盈',
      suggested_utterance: '这是服务端原始建议句，必须保留买入卖出内容和选中引用。',
      capability_ids: [], assumptions: ['参数可编辑'], confidence: 0.8,
      ...(bound ? { strategy: rules } : { strategy_template: {
        catalog: rules.catalog, entry: rules.entry, exit: rules.exit,
        execution: rules.execution, backtest: rules.backtest,
      } }),
    }
    const before = structuredClone(proposal)
    const summaries = ideaProposalRuleSummaries(proposal)
    expect(summaries.entry).toContain('高于 25')
    expect(summaries.entry).toContain('且')
    expect(summaries.entry).toContain('非（近 5 个交易日涨幅 ≤ 12%）')
    expect(summaries.entry).not.toMatch(/达到|保证/)
    expect(summaries.exit).toContain('低于 20')
    expect(summaries.exit).not.toContain('止盈')
    expect(proposal).toEqual(before)
  })

  it('keeps text-only summaries but derives price-plan summaries from actual rules', () => {
    const proposal: IdeaRouteProposal = {
      id: 'text-only', title: '待解释方向', hypothesis: '待验证', entry_summary: '原始买入说明',
      exit_summary: '原始卖出说明', suggested_utterance: '按原始模型建议的买入卖出规则继续解释。',
      capability_ids: [], assumptions: [], confidence: 0.6,
    }
    const expected = { entry: proposal.entry_summary, exit: proposal.exit_summary }
    expect(ideaProposalRuleSummaries(proposal)).toEqual(expected)
    expect(ideaProposalTitle(proposal)).toBe('待解释方向')
    expect(ideaProposalTitle({ ...proposal, title: '可修改策略方向 1',
      capability_ids: ['technical.kdj', 'technical.rsi'],
    })).toBe('KDJ / RSI策略')
    expect(ideaProposalTitle({ ...proposal, title: '可修改策略方向 2', strategy: { ...strategy,
      entry: null, exit: null, trading_plan: { kind: 'conditional', parameters: { rules: [
        { kind: 'rebound', side: 'buy', gap: 2 }, { kind: 'pullback', side: 'sell', gap: 3 },
      ] } },
    } })).toBe('反弹买入 / 回落卖出')
    expect(ideaProposalRuleSummaries({ ...proposal, strategy: { ...strategy,
      entry: null, exit: null, trading_plan: { kind: 'scheduled', parameters: {
        budget_cny: 1000, sizing_mode: 'amount', frequency: 'monthly', day: 15, side: 'buy', at: 'close',
      } },
    } })).toEqual({ entry: '每月15日（月末不足则取月末），收盘买入1000元（含费用预算）；休市顺延', exit: '未设置卖出规则' })
  })

  it('retains a persisted provider failure as a resumable conversation, never executable', () => {
    const outcome = fromLiveDraftResponse({ ...response, status: 'unsupported',
      strategy: null, strategy_hash: null, diagnostic_code: 'candidate_provider_invalid_output',
    }, request)
    expect(outcome.status).toBe('needs_clarification')
    expect(outcome).toMatchObject({ draftId: response.draft_id, revision: response.revision })
    expect(outcome).not.toHaveProperty('draft')
    if (outcome.status === 'needs_clarification') {
      expect(outcome.clarification.question).toContain('不用重新输入')
      expect(outcome.clarification.choices).toEqual([expect.objectContaining({
        id: 'retry-strategy-generation', label: '重新生成', suggestedUtterance: request.utterance,
      })])
    }
  })

  it('retains prepared rules after a data failure without creating execution authority', () => {
    const result = fromLiveDraftResponse({ ...response, status: 'needs_clarification',
      strategy: null, strategy_hash: null, diagnostic_code: 'skill_numeric_unit_unconfirmed',
      execution_assessment: { status: 'understood_not_executable', message: '缺历史单位口径',
        missing: ['历史单位口径'], interpreted_strategy: strategy,
        strategy_hash: response.strategy_hash },
    }, request)
    expect(result.status).toBe('needs_clarification')
    if (result.status !== 'needs_clarification') return
    expect(result.clarification.question).toBe('缺历史单位口径')
    expect(result.clarification.reason).toBe('')
    expect(result.clarification.provisionalDraft?.strategySpec).toEqual(strategy)
    expect(result.clarification.choices).toEqual([])
    expect(result).not.toHaveProperty('draft')
    expect(result).not.toHaveProperty('runRequested', true)
  })

  it('roundtrips fixed CNY slippage independently of percentage and supports clearing it', () => {
    const result = fromLiveDraftResponse({ ...response,
      execution_settings: { slippage_cny: '0.02', slippage_bps: '0' },
    }, request)
    if (result.status !== 'compiled') throw new Error('expected draft')
    expect(toLiveRevisionBody(result.draft).execution_settings?.slippage_cny).toBe(0.02)
    expect(toLiveBacktestBody(result.draft).config.slippageCny).toBe(0.02)
    const cleared = mergeLiveRevision({ ...response, revision: 2,
      execution_settings: { slippage_cny: '0', slippage_bps: '5' } }, result.draft)
    expect(toLiveBacktestBody(cleared).config.slippageCny).toBe(0)
    expect(cleared.execution.slippageBps).toBe(5)
  })

  it('maps stored execution settings with decimal strings, explicit zero and false', () => {
    const outcome = fromLiveDraftResponse({ ...response, execution_settings: {
      slippage_bps: '0', commission_rate: '0', minimum_commission_cny: 0,
      participation_rate: '0.08', allocation_ratio: null, retry_unfilled_exits: false,
      run_robustness: false, limit_handling: 'strict_no_fill_at_limit',
    } }, request)
    if (outcome.status !== 'compiled') throw new Error('expected ready settings')
    expect(outcome.executionSettings).toEqual({
      slippageBps: 0, commissionRate: 0, minimumCommissionCny: 0,
      participationRate: 0.08, retryUnfilledExits: false, runRobustness: false,
      priceLimitMode: 'strict_no_fill_at_limit',
    })
    expect(outcome.draft.execution).toMatchObject({
      ...DEFAULT_EXECUTION_SETTINGS, ...outcome.executionSettings,
    })
  })

  it.each([{}, null])('does not inherit old fees into fresh server execution settings: %s', (settings) => {
    const outcome = fromLiveDraftResponse({ ...response, execution_settings: settings }, {
      ...request, executionSettings: { commissionRate: 0, slippageBps: 2 },
    })
    if (outcome.status !== 'compiled') throw new Error('expected a fresh strategy')
    expect(outcome.executionSettings).toEqual({})
    expect(outcome.draft.execution).toMatchObject(DEFAULT_EXECUTION_SETTINGS)
    expect(fromLiveDraftResponse(response, request)).not.toHaveProperty('executionSettings')
  })

  it('saves all execution settings and respects the returned single-field fee update', () => {
    const compiled = fromLiveDraftResponse(response, request)
    if (compiled.status !== 'compiled') throw new Error('expected a draft')
    const edited = { ...compiled.draft, execution: { ...compiled.draft.execution,
      slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0 } }
    const body = toLiveRevisionBody(edited)
    expect(body.execution_settings).toEqual({
      limit_handling: 'wait_for_unlock', capacity_mode: 'point_in_time_volume',
      participation_rate: 0.05, allocation_ratio: 1,
      commission_rate: 0, minimum_commission_cny: 0, slippage_bps: 2,
      retry_unfilled_exits: true, max_exit_attempts: 20, warmup_calendar_days: 180,
      settlement_extension_days: 14, run_robustness: true,
    })
    expect(toLiveCompileBody({ ...request, executionSettings: edited.execution }).execution_settings)
      .toEqual(body.execution_settings)
    const saved = mergeLiveRevision({ ...response, revision: 2,
      execution_settings: { ...body.execution_settings, slippage_bps: '3' },
    }, edited)
    expect(saved.execution).toEqual({ ...edited.execution, slippageBps: 3 })
    expect(mergeLiveRevision({ ...response, revision: 2 }, edited).execution)
      .toEqual(edited.execution)
  })

  it('displays only a verified name matching the executable stock code', () => {
    for (const verifiedSymbol of ['600183.SH', '600519.SH']) {
      const outcome = fromLiveDraftResponse({
        ...response,
        strategy: { ...strategy, instrument: { ...strategy.instrument, symbol: '600183.SH' } },
        verified_instrument: { symbol: verifiedSymbol, name: '生益科技',
          source: 'eastmoney_mx_finance_data', retrieved_at: '2026-09-05T10:00:00Z', evidence: null },
      }, request)
      if (outcome.status !== 'compiled') throw new Error('expected ready strategy')
      expect(outcome.draft.instrument.name).toBe(
        verifiedSymbol === '600183.SH' ? '生益科技' : '600183.SH')
      expect(outcome.draft.instrument.symbol).toBe('600183.SH')
    }
  })

  it.each([
    ['scheduled', { at: 'open' }, '在指定交易日开盘'],
    ['scheduled', { at: 'close' }, '在指定交易日收盘'],
    ['grid', { observation: 'minute_bar' }, '新触发委托最早下一根K线生效'],
    ['grid', { observation: null }, '当前尚未确认分钟数据可用'],
    ['grid', { observation: 'daily_close' }, '日线收盘确认信号'],
  ] as const)('describes the actual %s plan timing', (kind, parameters, expected) => {
    const outcome = fromLiveDraftResponse({ ...response, strategy: {
      ...strategy, entry: null, exit: null,
      trading_plan: { kind, parameters } as PricePlan,
    } }, request)
    if (outcome.status !== 'compiled') throw new Error('expected ready strategy')
    expect(outcome.draft.assumptions[0]).toContain(expected)
    if (!('observation' in parameters) || parameters.observation !== 'daily_close') {
      expect(outcome.draft.assumptions[0]).not.toContain('日线收盘确认信号')
    }
  })

  it.each(['buy', 'sell'] as const)('retains both composed legs and execution when saving a %s schedule', (side) => {
    const composed: StrategySpec = { ...strategy,
      entry: side === 'buy' ? null : strategy.entry,
      exit: side === 'sell' ? null : strategy.exit,
      trading_plan: { kind: 'scheduled', parameters: {
        side, frequency: 'monthly', day: 1, sizing_mode: 'shares', quantity: 100,
      } },
      execution: { ...strategy.execution, entry_policy: 'composed_entry_leg',
        exit_policy: 'composed_exit_leg', data_capability: 'daily_and_minute_ohlcv',
        execution_resolution: '1m', evaluation_frequency: 'daily_close_and_minute_bar',
        position_policy: 'bounded_inventory' },
    }
    const outcome = fromLiveDraftResponse({ ...response, strategy: composed }, request)
    if (outcome.status !== 'compiled') throw new Error('expected ready strategy')
    const summary = toStrategySummary(outcome.draft)
    expect(JSON.stringify(summary)).toContain(side === 'buy' ? 'MACD 死叉' : 'MACD 金叉')
    expect(JSON.stringify(summary)).not.toContain(side === 'buy' ? '未设置卖出规则' : '未设置买入规则')
    const saved = toLiveRevisionBody(outcome.draft).strategy
    expect(saved.execution).toEqual(composed.execution)
    expect(saved.entry).toEqual(composed.entry)
    expect(saved.exit).toEqual(composed.exit)
    expect(saved.trading_plan?.parameters.side).toBe(side)
  })

  it('round-trips two independent plans through revision and submission with one initial balance', () => {
    const composed: StrategySpec = { ...strategy, entry: null, exit: null,
      independent_plans: {
        entry_plan: { kind: 'scheduled', parameters: { side: 'buy', frequency: 'monthly',
          day: 1, sizing_mode: 'shares', quantity: 100, initial_cash_cny: 100000 } },
        exit_plan: { kind: 'conditional', parameters: { observation: 'minute_bar',
          initial_cash_cny: 100000, rules: [{ kind: 'price', side: 'sell',
            direction: 'up', target_price: 25, quantity: 100 }] } },
      },
      execution: { ...strategy.execution, entry_policy: 'composed_entry_leg',
        exit_policy: 'composed_exit_leg', data_capability: 'daily_and_minute_ohlcv',
        execution_resolution: '1m', evaluation_frequency: 'daily_close_and_minute_bar',
        position_policy: 'bounded_inventory' },
    }
    const original = structuredClone(composed)
    const outcome = fromLiveDraftResponse({ ...response, strategy: composed }, request)
    if (outcome.status !== 'compiled') throw new Error('expected ready strategy')
    const edited = { ...outcome.draft, backtest: { ...outcome.draft.backtest,
      initialCashCny: 200000, start: '2025-03-03' } }
    const summary = toStrategySummary(edited)
    expect(summary.title).toBe('定时买入、条件卖出')
    expect(outcome.draft.title).toBe(summary.title)
    expect(summary.rows.find(row => row.key === 'entry')?.value).toContain('每月1日')
    expect(summary.rows.find(row => row.key === 'exit')?.value).toContain('25')
    expect(summary.confirmation).toContain('各自计划')
    const revised = toLiveRevisionBody(edited).strategy
    expect(revised.independent_plans?.entry_plan.parameters).toEqual({
      ...composed.independent_plans!.entry_plan.parameters, initial_cash_cny: 200000 })
    expect(revised.independent_plans?.exit_plan.parameters).toEqual({
      ...composed.independent_plans!.exit_plan.parameters, initial_cash_cny: 200000 })
    expect(revised.execution).toEqual(composed.execution)
    expect(revised.entry).toBeNull()
    expect(revised.exit).toBeNull()
    expect(revised.backtest.start).toBe('2025-03-03')
    expect(toLiveBacktestBody(edited).strategy).toEqual(revised)
    expect(composed).toEqual(original)
  })

  it('renders daily-signal plus minute-protection timing without calling it daily-only', () => {
    const hybrid: StrategySpec = {
      ...strategy,
      exit: { op: 'first_of', children: [{
        type: 'minute_protection_exit', take_profit_pct: 5, stop_loss_pct: 3,
        trailing_drawdown_pct: null, limit_price_cny: null,
        anchor: 'fee_exclusive_weighted_acquisition_cost',
        observation: 'raw_minute_high_low', execution: 'next_bar_order_activation',
      }] },
      execution: {
        timezone: 'Asia/Shanghai', entry_policy: 'next_market_session_open',
        exit_policy: 'daily_signal_open_or_next_minute_activation',
        data_capability: 'daily_and_minute_ohlcv', execution_resolution: '1m',
        evaluation_frequency: 'daily_close_and_minute_bar',
        position_policy: 'single_position_no_pyramiding', t_plus_one: true,
      },
    }
    const outcome = fromLiveDraftResponse({ ...response, strategy: hybrid }, request)
    if (outcome.status !== 'compiled') throw new Error('expected ready hybrid strategy')
    expect(outcome.draft.exit.conditions[0]).toMatchObject({
      kind: 'minute_protection', takeProfitPct: 5, stopLossPct: 3,
    })
    expect(outcome.draft.assumptions[0]).toContain('日线条件收盘后确认')
    expect(outcome.draft.assumptions[0]).toContain('新委托下一分钟生效')
  })

  it('keeps a ready semantic edit executable even with an acknowledgement', () => {
    const outcome = fromLiveDraftResponse({
      ...response, assistant_message: '已修改入场周期，其余不变。', run_requested: true,
      is_strategy_edit: true,
    }, request)
    expect(outcome.status).toBe('compiled')
    if (outcome.status !== 'compiled') throw new Error('expected a ready edit')
    expect(outcome.runRequested).toBe(true)
    expect(outcome.isStrategyEdit).toBe(true)
    expect(outcome.assistantMessage).toBe('已修改入场周期，其余不变。')
    expect(outcome.draft.strategySpec).toEqual(strategy)
    expect(outcome.draft.assumptions.join('')).not.toContain('100 股整手')
  })

  it('maps verified stock-edit candidates to answers on the pending revision', () => {
    const candidates = [
      { symbol: '601995.SH', name: '中金公司' },
      { symbol: '600489.SH', name: '中金黄金' },
      { symbol: '000060.SZ', name: '中金岭南' },
      { symbol: '002500.SZ', name: '山西证券' },
    ].map(candidate => ({ ...candidate, source: 'eastmoney_mx_screener',
      retrieved_at: '2026-09-06T00:00:00Z', evidence: null }))
    const outcome = fromLiveDraftResponse({
      ...response, status: 'needs_clarification', strategy: null, strategy_hash: null,
      diagnostic_code: 'strategy_edit_clarification', is_strategy_edit: true,
      clarification: '你说的中金是哪只？', instrument_candidates: candidates,
    }, request)
    if (outcome.status !== 'needs_clarification') throw new Error('expected stock clarification')
    expect(outcome.isStrategyEdit).toBe(true)
    expect(outcome.clarification.choices).toEqual(candidates.slice(0, 3).map(candidate => ({
      id: candidate.symbol, label: candidate.name, description: candidate.symbol,
      action: 'submit_clarification', suggestedUtterance: candidate.name,
      instrumentSymbol: candidate.symbol, instrumentName: candidate.name,
    })))
    expect(outcome).not.toHaveProperty('runRequested')
    const fresh = fromLiveDraftResponse({ ...response, is_strategy_edit: false }, request)
    expect(fresh).not.toHaveProperty('isStrategyEdit')
  })

  it('passes explicit refresh only to that run, not into the saved strategy', () => {
    const outcome = fromLiveDraftResponse({
      ...response, run_requested: true, refresh_data: true,
    }, request)
    if (outcome.status !== 'compiled') throw new Error('expected ready strategy')
    expect(outcome.refreshData).toBe(true)
    expect(toLiveBacktestBody(outcome.draft, { refreshData: outcome.refreshData }).config.refreshData)
      .toBe(true)
    expect(toLiveBacktestBody(outcome.draft).config.refreshData).toBeUndefined()
    expect(outcome.draft.strategySpec).toEqual(strategy)
    const withoutRun = fromLiveDraftResponse({ ...response, refresh_data: true }, request)
    expect(withoutRun).not.toHaveProperty('refreshData')
  })

  it('preserves the pending semantic edit diagnostic instead of treating it as live data', () => {
    const outcome = fromLiveDraftResponse({
      ...response, status: 'needs_clarification', strategy: null, strategy_hash: null,
      diagnostic_code: 'strategy_edit_clarification',
      clarification: '买入还是卖出？', assistant_message: '买入还是卖出？',
    }, request)
    expect(outcome).toMatchObject({
      status: 'needs_clarification', clarification: { id: 'strategy_edit_clarification' },
    })
  })

  it('keeps optional current-data evidence separate from the executable draft', () => {
    const outcome = fromLiveClarificationAnswerResponse({
      reply_kind: 'accepted',
      assistant_message: '东方财富换手率已经查到。',
      suggestions: [],
      data: {
        usage_scope: 'current_query_only',
        historical_backtest_eligible: false,
        kind: 'finance',
        finance: {
          provider: 'eastmoney-mx-saas',
          query: '东方财富昨天的换手率是多少？',
          indicators: null,
          tables: [{ columns: ['日期', '换手率'], rows: [['2026-09-01', 2.37]] }],
          provenance: {
            response_sha256: `sha256:${'c'.repeat(64)}`,
            retrieved_at: '2026-09-02T08:00:00+08:00',
            schema_version: 'mx-saas.search-data.v1',
          },
        },
      },
      draft: response,
    }, {
      draftId: response.draft_id,
      revision: response.revision,
      answer: '东方财富昨天的换手率是多少？',
      originalRequest: request,
      clarification: { id: 'instrument_required', question: '请补股票', reason: '', choices: [] },
    })

    expect(outcome.data).toEqual(expect.objectContaining({
      kind: 'finance',
      historicalBacktestEligible: false,
      finance: expect.objectContaining({
        provider: 'eastmoney-mx-saas',
        query: '东方财富昨天的换手率是多少？',
        tables: [{ columns: ['日期', '换手率'], rows: [['2026-09-01', 2.37]] }],
        provenance: expect.objectContaining({
          responseSha256: `sha256:${'c'.repeat(64)}`,
          schemaVersion: 'mx-saas.search-data.v1',
        }),
      }),
    }))
    expect(outcome.outcome.status).toBe('compiled')
  })

  it('keeps a first-turn current-data answer even when the sentence is not a strategy', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'unsupported',
      strategy: null,
      strategy_hash: null,
      diagnostic_code: 'no_supported_signal_recognized',
      assistant_message: '查到了 2 条实时选股结果。',
      data: {
        usage_scope: 'current_query_only',
        historical_backtest_eligible: false,
        kind: 'screen',
        screen: {
          provider: 'eastmoney_mx_screener',
          query: '今天上涨的 A 股',
          asset_type: 'A股',
          columns: ['证券代码', '证券简称'],
          rows: [{ '证券代码': '300059', '证券简称': '东方财富' }],
          provenance: {
            response_sha256: `sha256:${'d'.repeat(64)}`,
            retrieved_at: '2026-09-03T09:31:00+08:00',
            schema_version: 'eastmoney-mx.select-security.v1',
          },
        },
      },
    }, request)

    expect(outcome).toMatchObject({
      status: 'needs_clarification',
      assistantMessage: '查到了 2 条实时选股结果。',
      clarification: { id: 'live_data_query', choices: [] },
      data: {
        kind: 'screen',
        usageScope: 'current_query_only',
        screen: {
          assetType: 'A股',
          rows: [{ '证券代码': '300059', '证券简称': '东方财富' }],
        },
      },
    })
  })

  it('maps screened-finance entities and batches without flattening their provenance', () => {
    const provenance = {
      response_sha256: `sha256:${'e'.repeat(64)}`,
      retrieved_at: '2026-09-03T09:32:00+08:00',
      schema_version: 'eastmoney-mx.test.v1',
    }
    const outcome = fromLiveClarificationAnswerResponse({
      reply_kind: 'clarification',
      assistant_message: '先选出了 1 只标的，并完成了 1 批实时查数。',
      suggestions: [],
      data: {
        usage_scope: 'current_query_only',
        historical_backtest_eligible: false,
        kind: 'screened_finance',
        screened_finance: {
          usage_scope: 'current_query_only',
          historical_backtest_eligible: false,
          screen: {
            provider: 'eastmoney_mx_screener',
            query: '上涨的股票',
            asset_type: 'A股',
            columns: ['证券代码'],
            rows: [{ '证券代码': '300059' }],
            provenance,
          },
          entities: [{ code: '300059', name: '东方财富', asset_type: 'A股' }],
          batches: [{
            provider: 'eastmoney_mx_finance_data',
            query: '查询东方财富归母净利润',
            indicators: '归母净利润',
            tables: [{ rawTable: { headers: ['证券代码', '值'], data: [['300059', 1]] } }],
            provenance,
          }],
        },
      },
      draft: {
        ...response,
        status: 'needs_clarification',
        strategy: null,
        strategy_hash: null,
        clarification: '请继续补充规则。',
        diagnostic_code: 'exit_rule_not_recognized',
      },
    }, {
      draftId: response.draft_id,
      revision: response.revision,
      answer: '上涨的股票；获取归母净利润',
      originalRequest: request,
      clarification: { id: 'exit_rule_not_recognized', question: '请补卖出', reason: '', choices: [] },
    })

    expect(outcome.data).toMatchObject({
      kind: 'screened_finance',
      screenedFinance: {
        entities: [{ code: '300059', name: '东方财富', assetType: 'A股' }],
        batches: [{ provider: 'eastmoney_mx_finance_data' }],
      },
    })
  })

  it('uses Shanghai today by default while preserving an explicit replay boundary', () => {
    try {
      vi.stubEnv('VITE_DATA_AS_OF_DATE', '')
      const now = new Date('2026-09-04T16:00:00.000Z')
      expect(dataAsOfDate(now)).toBe('2026-09-05')
      expect(toLiveCompileBody(request, dataAsOfDate(now))).toEqual({
        utterance: request.utterance,
        instrument_context: '300059.SZ',
        as_of_date: '2026-09-05',
      })

      vi.stubEnv('VITE_DATA_AS_OF_DATE', '2026-08-06')
      expect(dataAsOfDate(now)).toBe('2026-08-06')
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('passes the latest twenty report references in conversation order for a result follow-up', () => {
    const runIds = Array.from({ length: 23 }, (_, index) => `run:${index}`)
    expect(toLiveCompileBody({
      instrument: { symbol: '300059.SZ', name: '东方财富', market: 'CN_A', exchange: 'SZSE' },
      utterance: '这次和上次相比怎么样？',
      relatedRunIds: runIds,
    }).related_run_ids).toEqual(runIds.slice(-20))
  })

  it('passes the displayed optimization version by reference, not client strategy data', () => {
    const relatedReview = { runId: 'run:current', responseHash: `sha256:${'b'.repeat(64)}` }
    const body = toLiveCompileBody({ ...request, relatedRunIds: [relatedReview.runId],
      relatedReview, utterance: '用第一个优化方案再跑一次' })
    expect(body.related_review).toEqual({ run_id: relatedReview.runId,
      response_hash: relatedReview.responseHash })
    expect(body).not.toHaveProperty('optimizationCandidates')
  })

  it('keeps bounded exposed review versions only for submitted report references', () => {
    const reviews = Array.from({ length: 23 }, (_, index) => ({
      runId: 'run:current', responseHash: `sha256:review-${index}`,
    }))
    const body = toLiveCompileBody({ ...request, relatedRunIds: ['run:current'],
      relatedReview: { runId: 'run:absent', responseHash: 'sha256:absent' },
      relatedReviews: [...reviews, { runId: 'run:absent', responseHash: 'sha256:absent' }],
    })
    expect(body.related_reviews).toEqual(reviews.slice(-20).map(review => ({
      run_id: review.runId, response_hash: review.responseHash,
    })))
    expect(body).not.toHaveProperty('related_review')
  })

  it('does not send the standalone demo stock as authoritative context', () => {
    expect(toLiveCompileBody({
      ...request,
      instrumentContextSource: 'standalone_default',
      utterance: '同花顺ROE高于0%买入，MACD死叉卖出，回测近1年',
    }, '2026-08-06')).toEqual({
      utterance: '同花顺ROE高于0%买入，MACD死叉卖出，回测近1年',
      instrument_context: null,
      as_of_date: '2026-08-06',
    })
  })

  it('sends the stock-page instrument context without a legacy clarification envelope', () => {
    expect(toLiveCompileBody({
      ...request,
    }, '2026-08-06')).toEqual({
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

  it('keeps a server-compiled colloquial interpretation as confirmation-only preview', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '你可以选一个方向继续，也可以直接说自己的完整买卖规则。',
      diagnostic_code: 'idea_guidance_required',
      idea_route: {
        schema_version: 'idea-route.v1',
        understanding: '你想低买高卖，但还没有指定可检验的进出场规则。',
        hypothesis: '用超跌后的价格反转作为一个可验证假设。',
        asset_mapping: {
          instrument_symbol: '300059.SZ',
          relation: 'current_page_proxy',
          rationale: '只使用当前 A 股作为价格代理。',
          evidence_status: 'host_context_only',
        },
        proposals: [{
          id: 'rsi_reversal',
          title: '检验超跌后的反转',
          hypothesis: '用超跌后的价格反转作为一个可验证假设。',
          entry_summary: 'RSI 低于 30',
          exit_summary: 'RSI 高于 70',
          suggested_utterance: 'RSI低于30买入，高于70卖出，回测近1年',
          capability_ids: ['technical.rsi'],
          assumptions: ['不代表预测。'],
          confidence: 0.75,
        }, {
          id: 'macd_momentum',
          title: '用动量转强确认',
          hypothesis: '用动量转强作为一个可验证假设。',
          entry_summary: 'MACD 金叉',
          exit_summary: 'MACD 死叉',
          suggested_utterance: 'MACD金叉买入，死叉卖出，回测近1年',
          capability_ids: ['technical.macd'],
          assumptions: ['不代表预测。'],
          confidence: 0.75,
        }],
      },
      suggested_strategy: strategy,
      suggested_strategy_hash: `sha256:${'b'.repeat(64)}`,
      suggested_strategy_choice_id: 'rsi_reversal',
      suggested_strategy_note: '我把你说的口语表达暂时理解成这条规则；请确认后再回测。',
    }, request)

    expect(outcome.status).toBe('needs_clarification')
    if (outcome.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(outcome.clarification.provisionalDraft).toMatchObject({
      strategyHash: `sha256:${'b'.repeat(64)}`,
      instrument: { symbol: '300059.SZ' },
    })
    expect(outcome.clarification.provisionalChoiceId).toBe('rsi_reversal')
  })

  it('offers range acceptance without granting automatic execution', () => {
    const outcome = fromLiveDraftResponse({ ...response, status: 'needs_clarification',
      strategy: null, strategy_hash: null, run_requested: true,
      diagnostic_code: 'backtest_range_confirmation_required',
      clarification: '已预填可用范围，是否接受？', idea_route: null,
      suggested_strategy: strategy, suggested_strategy_hash: `sha256:${'b'.repeat(64)}`,
      suggested_strategy_note: '确认前保留原日期，不启动回测。',
    }, request)
    expect(outcome.status).toBe('needs_clarification')
    if (outcome.status !== 'needs_clarification') throw new Error('expected range proposal')
    expect(outcome).not.toHaveProperty('runRequested')
    expect(outcome.clarification.choices[0]).toMatchObject({
      label: '接受建议范围', action: 'submit_clarification', suggestedUtterance: '接受建议范围',
    })
    expect(outcome.clarification.provisionalDraft?.strategySpec).toEqual(strategy)
  })

  it.each([false, true])('keeps semantic confirmation non-executable without recommendations (run_requested=%s)', (runRequested) => {
    const note = '“放量后再买”的时序还不明确，请确认是金叉当天同时放量，还是放量后的下一次金叉。'
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      run_requested: runRequested,
      diagnostic_code: 'semantic_confirmation_required',
      clarification: '已识别买卖规则，还需要确认一处具体含义。',
      idea_route: null,
      suggested_strategy: strategy,
      suggested_strategy_hash: `sha256:${'b'.repeat(64)}`,
      suggested_strategy_choice_id: null,
      suggested_strategy_note: note,
    }, request)

    expect(outcome.status).toBe('needs_clarification')
    if (outcome.status !== 'needs_clarification') throw new Error('expected semantic confirmation')
    expect(outcome).not.toHaveProperty('draft')
    expect(outcome).not.toHaveProperty('runRequested')
    expect(outcome.clarification.id).toBe('semantic_confirmation_required')
    expect(outcome.clarification.choices).toEqual([])
    expect(outcome.clarification.ideaRoute).toBeUndefined()
    expect(outcome.clarification.provisionalChoiceId).toBeUndefined()
    expect(outcome.clarification.provisionalNote).toBe(note)
    expect(outcome.clarification.provisionalDraft).toMatchObject({
      strategyHash: `sha256:${'b'.repeat(64)}`,
      strategySpec: strategy,
      instrument: { name: '东方财富', symbol: '300059.SZ' },
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
        question: '请直接告诉我想回测的股票名称或 6 位证券代码；前面已经识别到的买卖条件我会继续保留。',
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

  it('turns a missing exit into text-only clarification instead of a stock choice or default rule', () => {
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
        choices: [],
      },
    })
    expect(outcome).not.toMatchObject({
      clarification: { choices: [{ id: 'use-current-instrument' }] },
    })
  })

  it('turns a missing entry into text-only clarification instead of asking for another exit', () => {
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
        choices: [],
      },
    })
    expect(outcome).not.toMatchObject({
      clarification: { choices: [{ label: '补充卖出条件' }] },
    })
  })

  it('returns an incomplete indicator name as text-only clarification without inventing rules', () => {
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
        choices: [],
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
          description: '买入：MACD 金叉且站上 20 日均线',
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
      description: '卖出：MACD 死叉',
    })
    const pendingData = fromLiveDraftResponse({
      ...guidedResponse,
      diagnostic_code: 'candidate_data_incomplete',
      run_requested: false,
      idea_route: {
        ...guidedResponse.idea_route!,
        proposals: guidedResponse.idea_route!.proposals.map((item) => ({
          ...item, assumptions: ['数据准备：所需指标尚未返回；尚未开始回测。'],
        })),
      },
    }, request)
    expect(pendingData.status).toBe('needs_clarification')
    if (pendingData.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(pendingData.clarification.choices.map((item) => item.id)).toEqual(['trend-confirmation', 'oversold-rebound'])
    expect(pendingData.clarification.ideaRoute?.proposals.every(
      (item) => item.assumptions[0]?.startsWith('数据准备：') === true,
    )).toBe(true)
    expect(pendingData).not.toHaveProperty('runRequested', true)
    for (const diagnosticCode of ['strategy_rule_incomplete', 'ambiguous_cross_indicator']) {
      const completeOutcome = fromLiveDraftResponse({
        ...guidedResponse,
        diagnostic_code: diagnosticCode,
      }, request)
      expect(completeOutcome.status).toBe('needs_clarification')
      if (completeOutcome.status !== 'needs_clarification') throw new Error('expected clarification')
      expect(completeOutcome.clarification.choices[0]).toMatchObject({
        id: 'trend-confirmation',
        description: '买入：MACD 金叉且站上 20 日均线；卖出：MACD 死叉',
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

  it('fails closed when no prepared direction is available', () => {
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
    }, request)).toThrow('服务没有返回可供选择的完整策略方向')
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
    expect(outcome.clarification.reason).toBe('你在表达对特朗普相关政策的不认同。')
    expect(outcome.clarification.choices[0]?.description).toBe('买入：股价上穿 20 日均线；卖出：股价跌破 20 日均线')
    expect(outcome.clarification.choices.every((choice) => choice.instrumentSymbol === undefined))
      .toBe(true)
  })

  it('keeps each researched proposal bound to its own grounded instrument', () => {
    const outcome = fromLiveDraftResponse({
      ...response,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      diagnostic_code: 'idea_guidance_required',
      clarification: '选一个标的和验证方向。',
      idea_route: {
        schema_version: 'idea-route.v1',
        understanding: '联网结果给出两个待验证标的。',
        hypothesis: '需分别用价格规则验证。',
        asset_mapping: {
          instrument_symbol: null,
          relation: 'unbound',
          rationale: '不使用页面默认股票。',
          evidence_status: 'instrument_required',
        },
        proposals: [{
          id: 'eastmoney-trend',
          title: '东方财富趋势验证',
          hypothesis: '用均线验证趋势。',
          entry_summary: '股价上穿 20 日均线',
          exit_summary: '股价跌破 20 日均线',
          suggested_utterance: '东方财富上穿20日均线买入，跌破20日均线卖出',
          instrument_symbol: '300059.SZ',
          capability_ids: ['technical.ma'],
          assumptions: [],
          confidence: 0.8,
        }, {
          id: 'hithink-reversal',
          title: '同花顺反转验证',
          hypothesis: '用 RSI 验证反转。',
          entry_summary: 'RSI 低于 30',
          exit_summary: 'RSI 高于 70',
          suggested_utterance: '同花顺RSI低于30买入，高于70卖出',
          instrument_symbol: '300033.SZ',
          capability_ids: ['technical.rsi'],
          assumptions: [],
          confidence: 0.75,
        }],
      },
    }, { ...request, instrumentContextSource: 'standalone_default' })

    expect(outcome.status).toBe('needs_clarification')
    if (outcome.status !== 'needs_clarification') throw new Error('expected clarification')
    expect(outcome.clarification.choices.map((choice) => choice.instrumentSymbol))
      .toEqual(['300059.SZ', '300033.SZ'])
    expect(outcome.clarification.ideaRoute?.proposals.map((proposal) => proposal.instrument_symbol))
      .toEqual(['300059.SZ', '300033.SZ'])
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

  it.each([
    ['candidate_provider_authentication_failed', '策略服务暂时无法使用，刚才的内容已经保留。', false],
    ['candidate_provider_permission_denied', '策略服务暂时无法使用，刚才的内容已经保留。', false],
    ['candidate_provider_insufficient_balance', '策略服务暂时无法使用，刚才的内容已经保留。', false],
    ['candidate_provider_billing_restricted', '策略服务暂时无法使用，刚才的内容已经保留。', false],
    ['candidate_provider_rate_limited', '刚才的连接中断了。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_service_unavailable', '刚才的连接中断了。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_timeout', '刚才的连接中断了。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_connection_failed', '刚才的连接中断了。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_invalid_response', '策略方案没有完整生成。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_incomplete_response', '策略方案没有完整生成。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_unavailable', '策略方案没有完整生成。点一下“重新生成”即可继续，不用重新输入。', true],
    ['candidate_provider_invalid_output', '策略方案没有完整生成。点一下“重新生成”即可继续，不用重新输入。', true],
  ])('preserves the bounded provider failure diagnostic %s', (diagnosticCode, message, retryable) => {
    const outcome = fromLiveDraftResponse({
        ...response,
        status: 'unsupported',
        strategy: null,
        strategy_hash: null,
        clarification: '策略处理链路授权或核对未完成，后台诊断。',
        diagnostic_code: diagnosticCode,
      }, request)
    expect(outcome).toMatchObject({
      status: 'needs_clarification', draftId: response.draft_id, revision: response.revision,
      clarification: {
        id: diagnosticCode,
        question: message,
        choices: retryable ? [expect.objectContaining({ label: '重新生成' })] : [],
      },
    })
  })

  it('preserves the specific server explanation for unsupported strategies', () => {
    const message = '当前正式回测只支持下一可交易日开盘尝试成交，不支持当天或立即成交。'
    expect(() => fromLiveDraftResponse({
      ...response, status: 'unsupported', strategy: null, strategy_hash: null,
      diagnostic_code: 'same_session_execution_not_supported', clarification: message,
    }, request)).toThrow(message)
    expect(() => fromLiveDraftResponse({
      ...response, status: 'unsupported', strategy: null, strategy_hash: null,
      diagnostic_code: 'no_supported_signal_recognized', clarification: '   ',
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
    expect(revisionBody.strategy.entry?.type).toBe('indicator_condition')
    if (revisionBody.strategy.entry?.type === 'indicator_condition') {
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
      commissionRate: 0.00025,
      minimumCommissionCny: 5,
      retryUnfilledExits: false,
      maxExitAttempts: 7,
      warmupCalendarDays: 365,
      settlementExtensionDays: 30,
      runRobustness: false,
    })
    expect(body.strategy.entry?.type).toBe('indicator_condition')
    if (body.strategy.entry?.type === 'indicator_condition') {
      expect(body.strategy.entry.params.fast).toBe(10)
    }
    const savedExit = body.strategy.exit!.children[0]
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

    expect(outcome.draft.title).toBe('收盘突破 20 日均线时买入，跌破 20 日线卖出')
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

  it.each([
    ['price_crosses_above_upper', '突破前 20 个交易日最高价'],
    ['price_crosses_below_lower', '跌破前 20 个交易日最低价'],
    ['price_above_upper', '高于前 20 个交易日最高价'],
    ['price_below_lower', '低于前 20 个交易日最低价'],
  ])('explains channel condition %s without changing its trigger', (trigger, label) => {
    const condition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition', indicator_id: 'technical.donchian', definition_version: '1.0.0',
      params: { period: 20 }, timeframe: '1d', evaluation_mode: 'bar_close_confirmed', trigger, value: null,
    }
    const outcome = fromLiveDraftResponse({ ...response, strategy: {
      ...strategy, entry: condition, exit: { op: 'first_of', children: [condition] },
    } }, request)
    if (outcome.status !== 'compiled') throw new Error('expected a compiled strategy')
    expect(outcome.draft.entry.conditions[0]).toMatchObject({ label, trigger: `${label}，以收盘价确认；统计窗口不含当天。` })
    expect(outcome.draft.title).toBe(`${label}时买入，${label}卖出`)
  })

  it('summarizes a multi-condition strategy as one trading idea instead of indicator fragments', () => {
    const indicator = (indicator_id: string, trigger: string, params: Record<string, number>, value: number | null = null): StrategySpecIndicatorCondition => ({
      type: 'indicator_condition', indicator_id, definition_version: '1.0.0', params,
      timeframe: '1d', evaluation_mode: 'bar_close_confirmed', trigger, value,
    })
    const outcome = fromLiveDraftResponse({ ...response, strategy: {
      ...strategy,
      entry: { type: 'all', children: [
        indicator('technical.ma_cross', 'fast_above_slow', { fast_period: 20, slow_period: 60 }),
        indicator('price.return_pct', 'below', { period: 1 }, 0),
        indicator('technical.ma', 'price_above', { period: 20 }),
      ] },
      exit: { op: 'first_of', children: [
        indicator('technical.ma', 'price_crosses_below', { period: 20 }),
        { type: 'holding_period_exit', sessions: 10, anchor: 'first_entry_fill',
          count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy' },
      ] },
    } }, request)
    if (outcome.status !== 'compiled') throw new Error('expected a compiled strategy')
    expect(outcome.draft.title).toBe('上涨趋势中回踩买入，跌破 20 日线或持有 10 个交易日卖出')
  })

  it('renders static price-to-average comparisons without raw trigger identifiers', () => {
    const condition: StrategySpecIndicatorCondition = {
      type: 'indicator_condition', indicator_id: 'technical.ma', definition_version: '1.0.0',
      params: { period: 20, price_field: 'close' }, timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed', trigger: 'price_below', value: null,
    }
    const outcome = fromLiveDraftResponse({
      ...response, strategy: {
        ...strategy, entry: condition,
        exit: { op: 'first_of', children: [{ ...condition, trigger: 'price_above' }] },
      },
    }, request)
    if (outcome.status !== 'compiled') throw new Error('expected a compiled strategy')
    expect(outcome.draft.entry.conditions[0]?.label).toBe('价格低于 20 日均线')
    expect(outcome.draft.exit.conditions[0]?.label).toBe('价格高于 20 日均线')
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
    expect(outcome.draft.entry.conditions[0]?.label).toBe('10 日均线上穿 30 日均线')
    expect(outcome.draft.exit.conditions[0]?.label).toBe('10 日均线下穿 30 日均线')
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
      label: '收盘价创前 20 日新高',
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

  it.each([
    ['above', '高于'], ['below', '低于'], ['at_least', '不低于'], ['at_most', '不高于'],
    ['crosses_above', '上穿'], ['crosses_below', '下穿'],
  ])('renders both queried series for %s and never keeps a scalar threshold', (trigger, expectedLabel) => {
    const pair: StrategySpecIndicatorCondition = {
      type: 'indicator_condition', indicator_id: 'provider.series_compare', definition_version: '1.0.0',
      params: { left_metric_query: '5日均线', right_metric_query: '20日均线', unit: '元' },
      timeframe: '1d', evaluation_mode: 'bar_close_confirmed', trigger, value: 99,
    }
    const outcome = fromLiveDraftResponse({ ...response, strategy: { ...strategy, entry: pair } }, request)
    if (outcome.status !== 'compiled') throw new Error('expected series comparison strategy')
    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      label: `5日均线 ${expectedLabel} 20日均线（共同单位：元）`,
      trigger: `日线收盘确认：5日均线 ${expectedLabel} 20日均线（共同单位：元）`,
      parameters: [],
    })
    expect(toLiveRevisionBody(outcome.draft).strategy.entry).toEqual({ ...pair, value: null })
  })

  it('keeps generic scalar query names, units and thresholds intact', () => {
    const numeric: StrategySpecIndicatorCondition = {
      type: 'indicator_condition', indicator_id: 'provider.numeric', definition_version: '1.0.0',
      params: { metric_query: '市盈率TTM', unit: '倍' },
      timeframe: '1d', evaluation_mode: 'bar_close_confirmed', trigger: 'below', value: 20,
    }
    const outcome = fromLiveDraftResponse({ ...response, strategy: { ...strategy, entry: numeric } }, request)
    if (outcome.status !== 'compiled') throw new Error('expected scalar comparison strategy')
    expect(outcome.draft.entry.conditions[0]).toMatchObject({
      label: '市盈率TTM 低于 20倍', parameters: [expect.objectContaining({ key: '$value', value: 20, unit: '倍' })],
    })
    expect(toLiveRevisionBody(outcome.draft).strategy.entry).toEqual(numeric)
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

  it.each(['gte_multiple', 'consecutive_gte_multiple'])(
    'only edits active relative-volume parameters for %s', (trigger) => {
      const volume: StrategySpecIndicatorCondition = {
        type: 'indicator_condition', indicator_id: 'volume.relative',
        definition_version: '1.0.0', params: { baseline_period: 20, consecutive_days: 3 },
        timeframe: '1d', evaluation_mode: 'bar_close_confirmed', trigger, value: 1.5,
      }
      const rawStrategy = { ...strategy, entry: volume }
      const outcome = fromLiveDraftResponse({ ...response, strategy: rawStrategy }, request)
      if (outcome.status !== 'compiled') throw new Error('expected volume strategy')
      const condition = outcome.draft.entry.conditions[0]
      if (!condition || condition.kind !== 'indicator') throw new Error('expected volume condition')
      expect(condition.parameters.some(parameter => parameter.key === 'consecutive_days'))
        .toBe(trigger === 'consecutive_gte_multiple')
      expect(condition.parameters.map(parameter => parameter.key))
        .toEqual(expect.arrayContaining(['baseline_period', '$value']))
      expect(toLiveBacktestBody(outcome.draft).strategy).toEqual(rawStrategy)
    },
  )

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
      label: '连续 3 日成交量不低于前 20 日均量的 1.2倍',
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

    expect(outcome.draft.title).toBe('年度报告发布时买入，MACD 死叉卖出')
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

    expect(outcome.draft.title).toBe('市盈率 PE < 20时买入，MACD 死叉卖出')
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
      label: '成交后第 3 个交易日尝试卖出',
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
        choices: [],
        recognized: [
          { label: '股票', value: '同花顺（原话提及；代码仍待服务端确认）' },
          { label: '事件', value: '年度报告发布' },
          { label: '正文条件', value: '完整词“ai”出现 > 5 次' },
          { label: '持有期', value: '3 天（自然日还是交易日待确认）' },
        ],
      },
    })
  })

  it('does not copy unused model evidence into the UI draft', () => {
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

    expect(withEvidence.draft).not.toHaveProperty('interpretationEvidence')
    expect(withEvidence.draft.strategySpec).toEqual(withoutEvidence.draft.strategySpec)
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
