import { ApiError } from './types'
import type {
  BacktestOptimizationCandidate,
  BacktestReviewResponse,
  CapabilitiesResponse,
  CandidateAlternativeItem,
  CandidateGroundingPayload,
  CandidateProvenanceItem,
  CandidateRejectionItem,
  CapabilityParameter,
  CapabilityTriggerDefinition,
  Clarification,
  ClarificationAnswerInput,
  ClarificationAnswerOutcome,
  ClarificationData,
  CompileRequest,
  CompileResponse,
  IdeaRoute,
  Instrument,
  StrategyCondition,
  StrategyDraft,
  StrategyEventCondition,
  StrategyFinancialCondition,
  StrategyHoldingPeriodCondition,
  StrategyIndicatorCondition,
  StrategyLeg,
  StrategyParameter,
  StrategyPositionReturnCondition,
  StrategySpec,
  StrategySpecCondition,
  StrategySpecExitRule,
  StrategySpecEventCondition,
  StrategySpecFinancialCondition,
  StrategySpecHoldingPeriodExit,
  StrategySpecIndicatorCondition,
  StrategySpecPositionReturnExit,
  StrategySpecTrailingDrawdownExit,
  StrategyTrailingDrawdownCondition,
} from './types'

export type LiveCompileBody = {
  utterance: string
  instrument_context: string | null
  as_of_date: string
  edit_current_strategy?: boolean
  related_run_ids?: string[]
  related_review?: { run_id: string; response_hash: string }
}

export type LiveRevisionBody = {
  strategy: StrategySpec
  utterance: string
}

export type LiveBacktestBody = {
  strategy: StrategySpec
  config: {
    capacityMode: 'point_in_time_volume' | 'unlimited'
    participationRate: number
    slippageBps: number
    allocationRatio: number
    limitHandling: 'wait_for_unlock' | 'strict_no_fill_at_limit' | 'allow_limit_volume'
    commissionRate: number
    minimumCommissionCny: number
    retryUnfilledExits: boolean
    maxExitAttempts: number
    warmupCalendarDays: number
    settlementExtensionDays: number
    runRobustness: boolean
    refreshData?: boolean
  }
}

type LiveCurrentDataProvenance = {
  response_sha256: string
  retrieved_at: string
  schema_version: string
}

type LiveMarketScreen = {
  provider: string
  query: string
  asset_type: string
  columns: string[]
  rows: Array<Record<string, unknown>>
  provenance: LiveCurrentDataProvenance
}

type LiveFinanceQuery = {
  provider: string
  query: string
  indicators: string | null
  tables: Array<Record<string, unknown>>
  provenance: LiveCurrentDataProvenance
}

type LiveScreenedFinance = {
  usage_scope: 'current_query_only'
  historical_backtest_eligible: false
  screen: LiveMarketScreen
  entities: Array<{
    code: string
    name: string | null
    asset_type: string
  }>
  batches: LiveFinanceQuery[]
}

export type LiveClarificationData = {
  usage_scope: 'current_query_only'
  historical_backtest_eligible: false
} & (
  | { kind: 'screen'; screen: LiveMarketScreen; finance?: null; screened_finance?: null }
  | { kind: 'finance'; screen?: null; finance: LiveFinanceQuery; screened_finance?: null }
  | {
      kind: 'screened_finance'
      screen?: null
      finance?: null
      screened_finance: LiveScreenedFinance
    }
)

export type LiveDraftResponse = {
  draft_id: string
  revision: number
  status: 'ready' | 'needs_clarification' | 'unsupported' | 'invalid'
  run_requested?: boolean
  refresh_data?: boolean
  strategy: StrategySpec | null
  strategy_hash: string | null
  clarification: string | null
  diagnostic_code: string | null
  provenance: Array<{ path: string; source: string }>
  candidate_provenance: CandidateProvenanceItem | null
  candidate_grounding: CandidateGroundingPayload | null
  candidate_alternatives: CandidateAlternativeItem[]
  candidate_rejections: CandidateRejectionItem[]
  idea_route?: IdeaRoute | null
  instrument_suggestion?: Clarification['instrumentSuggestion'] | null
  instrument_suggestions?: Clarification['instrumentSuggestions'] | null
  verified_instrument?: Clarification['instrumentSuggestion'] | null
  backtest_review?: BacktestReviewResponse | null
  suggested_strategy?: StrategySpec | null
  suggested_strategy_hash?: string | null
  suggested_strategy_choice_id?: string | null
  suggested_strategy_note?: string | null
  assistant_message?: string | null
  data?: LiveClarificationData | null
  created_at: string
}

export type LiveClarificationAnswerResponse = {
  reply_kind: 'accepted' | 'clarification'
  assistant_message: string
  suggestions: Array<{
    id: string
    title: string
    preview: string
  }>
  data?: LiveClarificationData | null
  draft: LiveDraftResponse
}

const FALLBACK_EVENT_LABELS: Record<string, string> = {
  'event.financial_results.annual_report': '年度报告',
  'event.financial_results.semiannual_report': '半年度报告',
  'event.financial_results.quarterly_report': '季度报告',
  'event.financial_results.earnings_forecast_published': '业绩预告',
  'event.financial_results.earnings_flash_report': '业绩快报',
}

const readableIdentifier = (value: string): string => {
  const leaf = value.split('.').at(-1) ?? value
  if (/^(macd|rsi|ema|ma|kdj|cci|bbi|obv)$/i.test(leaf)) return leaf.toUpperCase()
  return leaf.replaceAll('_', ' ')
}

const triggerFallback = (trigger: string): string => ({
  golden_cross: '金叉',
  death_cross: '死叉',
  published: '首次发布',
  above: '高于',
  below: '低于',
  at_least: '不低于',
  at_most: '不高于',
  crosses_above: '由下向上穿过阈值',
  crosses_below: '由上向下穿过阈值',
  price_crosses_above: '价格上穿',
  price_crosses_below: '价格下穿',
  price_above: '价格高于',
  price_below: '价格低于',
  new_high: '创新高',
  new_low: '创新低',
  gte_multiple: '不低于',
  gt_multiple: '超过',
  lte_multiple: '不高于',
  consecutive_gte_multiple: '连续达到倍数',
  bearish: '顶背离',
  bullish: '底背离',
  turns_up: '转强',
  turns_down: '转弱',
} as Record<string, string>)[trigger] ?? trigger.replaceAll('_', ' ')

const indicatorCapability = (capabilities: CapabilitiesResponse | undefined, id: string) =>
  capabilities?.indicators.find((item) => item.indicator_id === id)

const triggerDefinition = (
  definitions: CapabilityTriggerDefinition[] | undefined,
  trigger: string,
) => definitions?.find((item) => item.id === trigger)

const parameterDefinition = (
  parameters: CapabilityParameter[] | undefined,
  key: string,
) => parameters?.find((item) => item.name === key)

const numericBoundary = (value: number | null | undefined, fallback: number) =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback

const diagnosticMessages: Record<string, string> = {
  candidate_provider_timeout: '策略生成模型请求超时，本次未生成策略。请原样重试。',
  candidate_provider_unavailable: '策略生成模型服务调用未完成，请稍后原样重试。',
  candidate_provider_invalid_output: '这次策略解析未通过结构或条件校验，尚未生成可回测结果。',
  'template_not_published/big_drop_rebound': '“大跌反弹”会按选股模板处理，当前模板尚未发布，暂时不能执行回测。',
  previous_session_limit_up_capability_unavailable: '已理解为“前一交易日涨停、下一交易日买入”，但这个信号还缺逐证券逐交易日的涨停价/涨停状态，以及 DSL 的前一交易日引用；不能用单日涨 10% 替代。“做个短线”也没有说清卖出方式，请补充持有天数、止盈止损或技术卖出条件。原话会保留，系统不会猜。',
  event_catalog_not_published: '这类事件尚未进入可执行目录，请改用已支持的定期报告事件。',
  entry_rule_not_recognized: '已识别卖出条件，但没有识别到买入规则。请在原话中补充何时买入。',
  exit_rule_not_recognized: '已识别买入条件，但没有识别到卖出规则。请在原话中补充何时卖出。',
  strategy_rule_incomplete: '已识别指标或事件，但还缺什么时候买入和什么时候卖出。请补充完整规则。',
  no_supported_signal_recognized: '没有识别到当前可执行的技术指标或公告事件。请写清何时买入、何时卖出和回测区间。',
  invalid_a_share_instrument: '股票代码不是可验证的沪、深、北交所 A 股代码，或代码与交易所后缀不一致。',
  ambiguous_obv_direction: '请说明能量潮（OBV）上升还是下降时触发，系统不会替你猜方向。',
  ambiguous_volume_direction: '请说明放量、缩量或相对成交量倍数，系统不会替你猜方向。',
  ambiguous_boolean_expression: '买入或卖出条件的“且/或”关系不够明确。请用括号或分别写清每组条件。',
  ambiguous_document_text_qualifier: '公告正文的大小写要求相互冲突，请只保留一种。',
  ambiguous_event_qualifier: '事件限定条件同时指向多个值，请只保留一个报告期、方向或数据来源。',
  document_fuzzy_match_not_executable: '正式回测暂不支持正文近义词或语义模糊匹配，请改成明确的字词和次数。',
  document_regex_not_executable: '正式回测暂不执行用户提供的正则表达式，请改成明确的字词和次数。',
  document_text_qualifier_conflict: '公告正文的匹配方式与原话不一致，系统已拒绝执行，请重新识别。',
  document_text_qualifier_not_consumed: '原话中的公告正文限定没有完整进入策略，请简化后重试。',
  document_title_scope_not_executable: '当前只能对首次完整报告正文做字词统计，不会把“标题”自动改成“正文”。',
  event_document_variant_not_executable: '当前只接受首次可获得的完整定期报告；摘要、更正版、英文版、取消或延期公告不会被偷换成完整报告。',
  event_entity_not_consumed: '原话中有事件没有进入策略，系统已拒绝执行。请分别写清事件之间的“且/或”关系。',
  event_execution_timing_not_executable: '你指定的事件买入时点尚不能保真回测。当前仅支持事件可得后，下一可交易日开盘尝试成交。',
  event_free_text_filter_not_executable: '交易对方、项目名、许可证类型等自由文本还没有可校验的规范值，暂不用于正式回测。',
  event_lifecycle_qualifier_not_executable: '“取消、撤回、终止或吊销”是另一类事件，当前不会把它当成原事件执行。',
  event_materiality_conflicts_with_event: '原话说的是非重大或普通小额项目，不符合“重大合同中标”事件口径。',
  event_numeric_filter_not_executable: '当前事件快照还没有可稳定校验的公告金额、比例或业绩阈值，系统不会忽略这些数字继续回测。',
  event_qualifier_conflict: '识别出的事件限定值与原话冲突，系统已拒绝执行，请重新识别。',
  event_qualifier_not_consumed: '原话中的报告期、方向或来源等限定没有完整进入策略，系统已拒绝执行。',
  event_qualifier_not_executable: '原话中包含当前事件运行时无法精确执行的限定条件，请删除该条件或改用已支持的规则。',
  event_report_period_not_executable: '该事件的具体报告期还没有可校验的精确映射，暂不执行。',
  event_retry_policy_not_executable: '你指定了跨日重试或保留信号，但当前事件规则仅支持一次成交尝试。',
  event_source_qualifier_not_executable: '你限定的公告来源不在这类事件的可执行快照契约中。',
  multiple_document_predicates_not_executable: '一句话里的多个正文字词条件还无法完整保真组合，请拆分或改成一个明确条件。',
  multiple_event_expression_not_executable: '一句话里有多个公告事件，但它们的组合关系尚未完整进入策略。请明确“同时满足”或“任一满足”。',
  empty_utterance: '请输入一条完整的买卖规则。',
  clarification_choice_not_supported: '这个补充选项不在当前后端契约内，请返回重新识别。',
  event_not_available_for_backtest: '当前运行环境既没有这类事件的固定快照，也没有声明可按本次请求准备数据。',
  instrument_unconfirmed: '没能从可校验的 A 股证券主数据中唯一确认这个公司名称，请补充 6 位证券代码。',
  instrument_resolution_unavailable: '证券名称查询服务暂时不可用，系统没有换用默认股票；请补充 6 位证券代码后再试。',
  instrument_context_mismatch: '你说的股票与当前股票页不一致，系统已停止，不会偷换标的。',
}

const shanghaiCalendarDate = (now: Date): string => {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const value = (type: 'year' | 'month' | 'day') =>
    parts.find((part) => part.type === type)?.value ?? ''
  return `${value('year')}-${value('month')}-${value('day')}`
}

export const dataAsOfDate = (now = new Date()) =>
  import.meta.env.VITE_DATA_AS_OF_DATE?.trim() || shanghaiCalendarDate(now)

export const toLiveCompileBody = (
  input: CompileRequest,
  asOfDate = dataAsOfDate(),
): LiveCompileBody => {
  return {
    utterance: input.utterance,
    instrument_context: input.instrumentContextSource === 'standalone_default'
      ? null : input.instrument.symbol,
    as_of_date: asOfDate,
    ...(input.editCurrentStrategy ? { edit_current_strategy: true } : {}),
    ...(input.relatedRunIds?.length ? { related_run_ids: input.relatedRunIds.slice(-2) } : {}),
    ...(input.relatedReview ? { related_review: {
      run_id: input.relatedReview.runId, response_hash: input.relatedReview.responseHash,
    } } : {}),
  }
}

const clarificationProposalDescription = (
  diagnosticCode: string,
  proposal: IdeaRoute['proposals'][number],
): string => {
  if (diagnosticCode === 'entry_rule_not_recognized') {
    return `买入：${proposal.entry_summary}`
  }
  if (diagnosticCode === 'exit_rule_not_recognized') {
    return `卖出：${proposal.exit_summary}`
  }
  return `买入：${proposal.entry_summary}；卖出：${proposal.exit_summary}`
}

const instrumentClarificationCodes = new Set([
  'instrument_required',
  'instrument_unconfirmed',
  'instrument_resolution_unavailable',
])

/**
 * 澄清态只保留原话中可以逐字指认的片段，不在前端补成 StrategySpec。
 * 后端再次返回 ready 前，这些内容只能帮助用户少改一句话。
 */
function recognizedFragments(input: CompileRequest): Array<{ label: string; value: string }> {
  const fragments: Array<{ label: string; value: string }> = []
  const text = input.utterance
  const mentionedCompany = text.match(/([\u4e00-\u9fa5A-Za-z0-9]{2,16})(?:发|发布)(?:年度报告|年报)/)?.[1]
  if (mentionedCompany || input.instrumentContextSource !== 'standalone_default') {
    fragments.push({
      label: '股票',
      value: mentionedCompany
        ? `${mentionedCompany}（原话提及；代码仍待服务端确认）`
        : `${input.instrument.name} ${input.instrument.symbol}`,
    })
  }
  if (/(?:年度报告|年报)/.test(text)) {
    fragments.push({ label: '事件', value: '年度报告发布' })
  }
  const termCount = text.match(/(?:提到|出现)\s*([A-Za-z0-9\u4e00-\u9fa5_-]+?)\s*(?:次数?)?\s*(超过|大于|不少于|至少|>=|＞|>)\s*(\d+)\s*次?/i)
  if (termCount) {
    const comparator = /不少于|至少|>=/.test(termCount[2] ?? '') ? '≥' : '>'
    fragments.push({
      label: '正文条件',
      value: `完整词“${termCount[1]}”出现 ${comparator} ${termCount[3]} 次`,
    })
  }
  const holding = text.match(/(\d+)\s*(?:个)?(交易日|交易天|天|日)后(?:卖出|退出)/)
  if (holding) {
    const exactTradingSessions = /交易/.test(holding[2] ?? '')
    fragments.push({
      label: '持有期',
      value: exactTradingSessions
        ? `${holding[1]} 个交易日`
        : `${holding[1]} 天（自然日还是交易日待确认）`,
    })
  }
  return fragments
}

export const fromLiveDraftResponse = (
  response: LiveDraftResponse,
  input: CompileRequest,
  capabilities?: CapabilitiesResponse,
): CompileResponse => {
  const currentData = response.data ? fromLiveClarificationData(response.data) : undefined
  const responseIdeaRoute = response.idea_route ?? undefined
  // An acknowledgement can accompany a ready strategy (including semantic edits).
  // It must never downgrade executable rules into a data-query clarification.
  if (response.status === 'ready' && response.strategy) {
    return { status: 'compiled', draft: toDraft(response, input, capabilities),
      ...(response.run_requested ? { runRequested: true,
        refreshData: response.refresh_data === true } : {}) }
  }
  // A first-turn current-data question still creates a draft so the next user
  // message can resume the same conversation.  Its compiler status can be
  // `unsupported` because the sentence is deliberately not a trading rule;
  // the provider answer must nevertheless reach the conversation UI.
  // When the server also returns grounded strategy directions, do not stop at
  // the plain data answer: the same turn must be able to show both the answer
  // and the three user-selectable follow-up strategies.
  if (response.assistant_message && !responseIdeaRoute
    && (currentData || response.diagnostic_code === 'data_query_only')) {
    return {
      status: 'needs_clarification',
      draftId: response.draft_id,
      revision: response.revision,
      assistantMessage: response.assistant_message,
      data: currentData,
      clarification: {
        id: 'live_data_query',
        question: response.assistant_message,
        reason: '',
        choices: [],
      },
    }
  }
  const diagnosticCode = response.diagnostic_code ?? 'strategy_clarification'
  const asksForInstrument = instrumentClarificationCodes.has(diagnosticCode)
  if (response.status === 'needs_clarification' || asksForInstrument || responseIdeaRoute) {
    const ideaRoute = responseIdeaRoute
    if (ideaRoute && ideaRoute.proposals.length < 2) {
      throw new ApiError({
        type: 'about:blank',
        title: '观点引导信息不完整',
        status: 502,
        detail: '服务没有返回至少两个可供选择的完整策略方向，请稍后重试。',
        code: 'idea_route_invalid',
      })
    }
    const canUseCurrentInstrument = asksForInstrument
      && input.instrumentContextSource !== 'standalone_default'
    const asksForEntry = diagnosticCode === 'entry_rule_not_recognized'
    const groundedInstrumentName = response.candidate_grounding?.spans
      .find((item) => item.path === '/instrument/symbol')?.text.trim()
    const ideaAnalysis = ideaRoute?.understanding.trim()
    return {
      status: 'needs_clarification',
      draftId: response.draft_id,
      revision: response.revision,
      assistantMessage: response.assistant_message ?? undefined,
      data: currentData,
      clarification: {
        id: diagnosticCode,
        question: response.clarification
          ?? (asksForInstrument
            ? canUseCurrentInstrument
              ? `请确认使用当前股票“${input.instrument.name}”，或直接输入其他股票名称或 6 位证券代码。`
              : '请直接告诉我想回测的股票名称或 6 位证券代码；前面已经识别到的买卖条件我会继续保留。'
            : undefined)
            ?? (ideaRoute ? '挑一条，我把它变成完整规则再跑一次。' : '补一句我就能跑。'),
        reason: ideaRoute
          ? ideaAnalysis || '我已经保留你说清楚的部分；还差一个关键条件，我不替你决定。'
          : asksForEntry
            ? '买入条件我不能替你定——填哪个都是我在替你决定什么时候进场。'
          : diagnosticCode === 'exit_rule_not_recognized'
            ? '卖出条件我不能替你定——什么时候离场得你说了算。'
          : response.clarification
          ? ''
          : asksForInstrument
          ? '前面的买卖条件我已经记住了，还需要补上回测标的。'
            : '还差一点关键信息。我不猜规则，你补一句就能跑。',
        choices: ideaRoute ? ideaRoute.proposals.slice(0, 3).map((proposal) => {
          const proposalSymbol = proposal.instrument_symbol
            ?? ideaRoute.asset_mapping.instrument_symbol
            ?? undefined
          const inputNameMatchesProposal = input.instrumentContextSource !== 'standalone_default'
            && input.instrument.symbol === proposalSymbol
          const groundedNameMatchesProposal = Boolean(
            groundedInstrumentName
            && ideaRoute.asset_mapping.instrument_symbol
            && ideaRoute.asset_mapping.instrument_symbol === proposalSymbol,
          )
          return {
            id: proposal.id,
            label: proposal.title,
            description: clarificationProposalDescription(diagnosticCode, proposal),
            action: 'replace_and_compile' as const,
            suggestedUtterance: proposal.suggested_utterance,
            instrumentSymbol: proposalSymbol,
            instrumentName: proposal.instrument_name ?? (groundedNameMatchesProposal
              ? groundedInstrumentName
              : inputNameMatchesProposal ? input.instrument.name : undefined),
          }
        }) : asksForInstrument
          ? canUseCurrentInstrument
            ? [{
                id: 'use-current-instrument',
                label: `使用 ${input.instrument.name}`,
                description: `继续回测 ${input.instrument.symbol}。`,
                recommended: true,
                action: 'submit_clarification' as const,
              }]
            : []
          // 规则澄清统一由对话文本和输入框承接；不再展示空操作标签。
          : [],
        recognized: recognizedFragments(input),
        instrumentSuggestion: response.instrument_suggestion ?? undefined,
        instrumentSuggestions: response.instrument_suggestions ?? undefined,
        backtestReview: response.backtest_review ?? undefined,
        ideaRoute,
        provisionalDraft: response.suggested_strategy && response.suggested_strategy_hash
          ? toDraft({
              ...response,
              strategy: response.suggested_strategy,
              strategy_hash: response.suggested_strategy_hash,
            }, input, capabilities)
          : undefined,
        provisionalChoiceId: response.suggested_strategy_choice_id ?? undefined,
        provisionalNote: response.suggested_strategy_note ?? undefined,
      },
    }
  }

  const code = response.diagnostic_code ?? `compile_${response.status}`
  throw new ApiError({
    type: 'about:blank',
    title: '暂时不能生成这条策略',
    status: 422,
    detail: response.clarification?.trim() || diagnosticMessages[code]
      || '这句话还不能转换成可执行策略，请补充明确的买入和卖出条件。',
    code,
  })
}

const fromLiveProvenance = (provenance: LiveCurrentDataProvenance) => ({
  responseSha256: provenance.response_sha256,
  retrievedAt: provenance.retrieved_at,
  schemaVersion: provenance.schema_version,
})

const fromLiveScreen = (screen: LiveMarketScreen) => ({
  provider: screen.provider,
  query: screen.query,
  assetType: screen.asset_type,
  columns: [...screen.columns],
  rows: screen.rows.map((row) => ({ ...row })),
  provenance: fromLiveProvenance(screen.provenance),
})

const fromLiveFinance = (finance: LiveFinanceQuery) => ({
  provider: finance.provider,
  query: finance.query,
  indicators: finance.indicators,
  tables: finance.tables.map((table) => ({ ...table })),
  provenance: fromLiveProvenance(finance.provenance),
})

const fromLiveClarificationData = (data: LiveClarificationData): ClarificationData => {
  const common = {
    usageScope: data.usage_scope,
    historicalBacktestEligible: data.historical_backtest_eligible,
  } as const
  if (data.kind === 'screen') {
    return { ...common, kind: 'screen', screen: fromLiveScreen(data.screen) }
  }
  if (data.kind === 'finance') {
    return { ...common, kind: 'finance', finance: fromLiveFinance(data.finance) }
  }
  return {
    ...common,
    kind: 'screened_finance',
    screenedFinance: {
      usageScope: data.screened_finance.usage_scope,
      historicalBacktestEligible: data.screened_finance.historical_backtest_eligible,
      screen: fromLiveScreen(data.screened_finance.screen),
      entities: data.screened_finance.entities.map((entity) => ({
        code: entity.code,
        name: entity.name,
        assetType: entity.asset_type,
      })),
      batches: data.screened_finance.batches.map(fromLiveFinance),
    },
  }
}

export const fromLiveClarificationAnswerResponse = (
  response: LiveClarificationAnswerResponse,
  input: ClarificationAnswerInput,
  capabilities?: CapabilitiesResponse,
): ClarificationAnswerOutcome => ({
  replyKind: response.reply_kind,
  assistantMessage: response.assistant_message,
  suggestions: response.suggestions.map((item) => ({ ...item })),
  data: response.data ? fromLiveClarificationData(response.data) : undefined,
  // The server owns how the saved original sentence and this answer are merged.
  // The original request here is used only to label the returned UI draft; it
  // is never resubmitted as an executable strategy candidate.
  outcome: fromLiveDraftResponse(response.draft, input.originalRequest, capabilities),
})

export const toLiveRevisionBody = (draft: StrategyDraft): LiveRevisionBody => ({
  utterance: draft.sourceText,
  strategy: strategySpecFromDraft(draft),
})

export const mergeLiveRevision = (
  response: LiveDraftResponse,
  editedDraft: StrategyDraft,
  capabilities?: CapabilitiesResponse,
): StrategyDraft => {
  if (response.status !== 'ready' || !response.strategy) {
    const code = response.diagnostic_code ?? `revision_${response.status}`
    throw new ApiError({
      type: 'about:blank',
      title: '策略版本未保存',
      status: 422,
      detail: diagnosticMessages[code] ?? '修改后的规则未通过校验，请检查买入、卖出和回测区间。',
      code,
    })
  }
  const savedDraft = toDraft(response, {
    instrument: editedDraft.instrument,
    utterance: editedDraft.sourceText,
  }, capabilities)
  return {
    ...savedDraft,
    confidence: editedDraft.confidence,
    execution: {
      ...savedDraft.execution,
      priceLimitMode: editedDraft.execution.priceLimitMode,
      capacityMode: editedDraft.execution.capacityMode,
      participationRate: editedDraft.execution.participationRate,
      allocationRatio: editedDraft.execution.allocationRatio,
      commissionRate: editedDraft.execution.commissionRate,
      minimumCommissionCny: editedDraft.execution.minimumCommissionCny,
      slippageBps: editedDraft.execution.slippageBps,
      retryUnfilledExits: editedDraft.execution.retryUnfilledExits,
      maxExitAttempts: editedDraft.execution.maxExitAttempts,
      warmupCalendarDays: editedDraft.execution.warmupCalendarDays,
      settlementExtensionDays: editedDraft.execution.settlementExtensionDays,
      runRobustness: editedDraft.execution.runRobustness,
    },
    warnings: editedDraft.warnings,
  }
}

/**
 * Project a server-compiled optimization candidate into the editable UI model.
 * This is presentation-only: the fresh run submits `candidate.strategy`
 * directly and never recompiles `suggestedUtterance` in the browser flow.
 */
export const fromBacktestOptimizationCandidate = (
  candidate: BacktestOptimizationCandidate,
  sourceDraft: StrategyDraft,
  capabilities?: CapabilitiesResponse,
): StrategyDraft => mergeLiveRevision({
  draft_id: sourceDraft.id,
  revision: sourceDraft.revision,
  status: 'ready',
  strategy: candidate.strategy,
  strategy_hash: candidate.strategyHash,
  clarification: null,
  diagnostic_code: null,
  provenance: [],
  candidate_provenance: null,
  candidate_grounding: null,
  candidate_alternatives: [],
  candidate_rejections: [],
  created_at: new Date().toISOString(),
}, {
  ...sourceDraft,
  sourceText: candidate.suggestedUtterance,
}, capabilities)

export const strategySpecFromDraft = (draft: StrategyDraft): StrategySpec => {
  return applyDraftEdits(draft.strategySpec, draft)
}

export const toLiveBacktestBody = (
  draft: StrategyDraft, options: { refreshData?: boolean } = {},
): LiveBacktestBody => ({
  strategy: strategySpecFromDraft(draft),
  config: {
    capacityMode: draft.execution.capacityMode,
    participationRate: draft.execution.participationRate,
    slippageBps: draft.execution.slippageBps,
    allocationRatio: draft.execution.allocationRatio,
    limitHandling: draft.execution.priceLimitMode,
    commissionRate: draft.execution.commissionRate,
    minimumCommissionCny: draft.execution.minimumCommissionCny,
    retryUnfilledExits: draft.execution.retryUnfilledExits,
    maxExitAttempts: draft.execution.maxExitAttempts,
    warmupCalendarDays: draft.execution.warmupCalendarDays,
    settlementExtensionDays: draft.execution.settlementExtensionDays,
    runRobustness: draft.execution.runRobustness,
    ...(options.refreshData ? { refreshData: true } : {}),
  },
})

function toDraft(
  response: LiveDraftResponse,
  input: CompileRequest,
  capabilities?: CapabilitiesResponse,
): StrategyDraft {
  const strategy = response.strategy as StrategySpec
  const entry = toLeg(strategy.entry, 'entry', capabilities)
  const exit = toExitLeg(strategy.exit.children, capabilities)
  return {
    id: response.draft_id,
    revision: response.revision,
    strategyHash: response.strategy_hash,
    sourceText: input.utterance,
    title: strategyTitle([...entry.conditions, ...exit.conditions]),
    instrument: instrumentFrom(strategy, input.instrument, response.candidate_grounding,
      response.verified_instrument),
    confidence: null,
    entry,
    exit,
    execution: {
      entryPolicy: strategy.execution.entry_policy,
      exitPolicy: strategy.execution.exit_policy,
      priceLimitMode: 'wait_for_unlock',
      tPlusOne: strategy.execution.t_plus_one,
      dataCapability: strategy.execution.data_capability,
      evaluationFrequency: strategy.execution.evaluation_frequency,
      capacityMode: 'point_in_time_volume',
      participationRate: 0.05,
      allocationRatio: 1,
      commissionRate: 0.0003,
      minimumCommissionCny: 5,
      slippageBps: 5,
      retryUnfilledExits: true,
      maxExitAttempts: 20,
      warmupCalendarDays: 180,
      settlementExtensionDays: 14,
      runRobustness: true,
    },
    backtest: {
      start: strategy.backtest.start,
      end: strategy.backtest.end,
      initialCashCny: strategy.backtest.initial_cash_cny,
    },
    assumptions: [
      ...(strategy.execution.data_capability === 'daily_ohlcv_events'
        ? [
            '事件按首次可获得时间确认，不倒填公告日期',
            '事件首次可得后按 09:15 截止规则选择可用的日线开盘价代理；记录时间不代表真实逐笔成交',
          ]
        : ['日线收盘确认信号，下一交易日使用开盘价代理；记录时间不代表真实逐笔成交']),
      '遵守 A 股 T+1；当天买入的股票下一交易日才可卖出',
      '按回测设置的资金比例投入，未投入的资金继续保留',
      '涨停买入或跌停卖出时等待开板；仅日线数据无法证明开板则保守记为未成交',
    ],
    warnings: [],
    strategySpec: strategy,
  }
}

function instrumentFrom(
  strategy: StrategySpec,
  fallback: Instrument,
  grounding?: CandidateGroundingPayload | null,
  verified?: Clarification['instrumentSuggestion'] | null,
): Instrument {
  const suffix = strategy.instrument.symbol.slice(-2)
  const exchange = suffix === 'SH' ? 'SSE' : suffix === 'BJ' ? 'BSE' : 'SZSE'
  const symbolChanged = strategy.instrument.symbol !== fallback.symbol
  const groundedName = grounding?.spans.find((item) => item.path === '/instrument/symbol')?.text.trim()
  const verifiedName = verified?.symbol === strategy.instrument.symbol ? verified.name : undefined
  return {
    ...fallback,
    name: verifiedName || (symbolChanged ? groundedName || strategy.instrument.symbol : fallback.name),
    symbol: strategy.instrument.symbol,
    exchange,
  }
}

function toLeg(
  condition: StrategySpecCondition,
  prefix: string,
  capabilities?: CapabilitiesResponse,
): StrategyLeg {
  const operator = condition.type === 'all' || condition.type === 'any' ? condition.type : 'all'
  return { operator, conditions: flattenConditions(condition, prefix, capabilities) }
}

function toExitLeg(
  conditions: StrategySpecExitRule[],
  capabilities?: CapabilitiesResponse,
): StrategyLeg {
  return {
    operator: 'first_of',
    conditions: conditions.flatMap((condition, index) => {
      const id = `exit-${index}`
      if (condition.type === 'holding_period_exit') return [toUiHoldingPeriod(condition, id)]
      if (condition.type === 'position_return_exit') return [toUiPositionReturn(condition, id)]
      if (condition.type === 'trailing_drawdown_exit') return [toUiTrailingDrawdown(condition, id)]
      return flattenConditions(condition, id, capabilities)
    }),
  }
}

function flattenConditions(
  condition: StrategySpecCondition,
  path: string,
  capabilities?: CapabilitiesResponse,
): StrategyCondition[] {
  if (condition.type === 'indicator_condition') {
    return [toUiCondition(condition, path, capabilities)]
  }
  if (condition.type === 'event_condition') {
    return [toUiEventCondition(condition, path)]
  }
  if (condition.type === 'financial_condition') {
    return [toUiFinancialCondition(condition, path)]
  }
  if (condition.type === 'not') {
    return flattenConditions(condition.child, `${path}-not`, capabilities)
  }
  return condition.children.flatMap((child, index) =>
    flattenConditions(child, `${path}-${index}`, capabilities))
}

function toUiCondition(
  condition: StrategySpecIndicatorCondition,
  id: string,
  capabilities?: CapabilitiesResponse,
): StrategyIndicatorCondition {
  const capability = indicatorCapability(capabilities, condition.indicator_id)
  const trigger = triggerDefinition(capability?.trigger_definitions, condition.trigger)
  const name = capability?.display_name ?? readableIdentifier(condition.indicator_id)
  const parameters: StrategyParameter[] = Object.entries(condition.params)
    .filter((entry): entry is [string, number] => typeof entry[1] === 'number')
    .filter(([key]) => !(condition.indicator_id === 'volume.relative'
      && key === 'consecutive_days' && condition.trigger !== 'consecutive_gte_multiple'))
    .map(([key, value]) => {
      const definition = parameterDefinition(capability?.parameters, key)
      return {
        key,
        label: definition?.display_name ?? parameterLabel(condition.indicator_id, key),
        value,
        integer: definition?.value_type === 'integer' || isIntegerIndicatorParameter(key),
        min: numericBoundary(definition?.minimum, -1_000_000_000),
        max: numericBoundary(definition?.maximum, 1_000_000_000),
        unit: definition?.unit ?? undefined,
      }
    })
  if (condition.value != null) {
    parameters.push({
      key: '$value',
      label: trigger?.display_name ? `${trigger.display_name}阈值` : `${triggerFallback(condition.trigger)}阈值`,
      value: condition.value,
      min: numericBoundary(trigger?.minimum, -1_000_000_000),
      max: numericBoundary(trigger?.maximum, 1_000_000_000),
      unit: trigger?.unit ?? indicatorValueUnit(condition.indicator_id),
    })
  }
  const triggerName = trigger?.display_name ?? triggerFallback(condition.trigger)
  const conditionOrder = conditionOrderLabel(condition)
  const thresholdUnit = trigger?.unit ?? indicatorValueUnit(condition.indicator_id)
  const thresholdLabel = condition.value != null && Number.isFinite(condition.value)
    ? `${name} ${triggerName} ${formatConditionValue(condition.value)}${thresholdUnit ?? ''}`
    : null
  return {
    id,
    kind: 'indicator',
    indicatorId: condition.indicator_id,
    label: conditionOrder
      ?? explicitWindowLabel(condition, triggerName)
      ?? movingAverageLabel(condition, triggerName)
      ?? thresholdLabel
      ?? `${name} ${triggerName}`,
    trigger: conditionOrder
      ? `日线收盘确认：${conditionOrder}`
      : trigger?.description ?? `${name}：${triggerName}`,
    timeframe: condition.timeframe,
    evaluationMode: condition.evaluation_mode,
    parameters,
  }
}

function indicatorValueUnit(indicatorId: string): string | undefined {
  if (indicatorId === 'price.close') return '元'
  if (['price.return_pct', 'price.amplitude', 'market.turnover_rate'].includes(indicatorId)) return '%'
  if (['volume.relative', 'market.volume'].includes(indicatorId)) return '倍'
  if (['market.amount', 'amount.average'].includes(indicatorId)) return '元'
  return undefined
}

function conditionOrderLabel(condition: StrategySpecIndicatorCondition): string | null {
  if (condition.value == null || !Number.isFinite(condition.value)) return null
  const value = formatConditionValue(condition.value)
  if (condition.indicator_id === 'price.close') {
    const operator = ({
      crosses_above: '上穿',
      crosses_below: '下穿',
      above: '>',
      below: '<',
      at_least: '≥',
      at_most: '≤',
    } as Record<string, string>)[condition.trigger]
    return operator ? `收盘价 ${operator} ${value} 元` : null
  }
  if (condition.indicator_id !== 'price.return_pct') return null

  const period = typeof condition.params.period === 'number' ? condition.params.period : null
  const scope = period === 1 ? '当日' : period == null ? '区间' : `近 ${period} 个交易日`
  const isDecline = condition.value < 0
  const operator = returnComparator(condition.trigger, isDecline)
  if (!operator) return null
  return `${scope}${isDecline ? '跌幅' : '涨幅'} ${operator} ${formatConditionValue(Math.abs(condition.value))}%`
}

function returnComparator(trigger: string, invert: boolean): string | null {
  const normal = ({
    crosses_above: '上穿',
    crosses_below: '下穿',
    above: '>',
    below: '<',
    at_least: '≥',
    at_most: '≤',
  } as Record<string, string>)[trigger]
  if (!normal || !invert) return normal ?? null
  return ({
    '上穿': '下穿',
    '下穿': '上穿',
    '>': '<',
    '<': '>',
    '≥': '≤',
    '≤': '≥',
  } as Record<string, string>)[normal] ?? null
}

function formatConditionValue(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(6)))
}

function isIntegerIndicatorParameter(key: string): boolean {
  return /(?:^fast$|^slow$|^signal$|period|days$|bars$|smoothing$|lookback$|separation$)/.test(key)
}

function parameterLabel(indicatorId: string, key: string): string {
  const scoped = ({
    'amount.average:period': '平均周期',
    'price.rolling_high:period': '观察周期',
    'price.rolling_low:period': '观察周期',
  } as Record<string, string>)[`${indicatorId}:${key}`]
  if (scoped) return scoped
  return ({
    fast: '快线',
    slow: '慢线',
    signal: '信号线',
    period: '周期',
    fast_period: '快线周期',
    slow_period: '慢线周期',
    lookback: '观察周期',
    average_period: '平均周期',
    baseline_period: '前序基准周期',
    baseline_lookback: '前序基准周期',
    consecutive_days: '连续天数',
    consecutive_sessions: '连续天数',
    max_spacing: '最大间隔',
  } as Record<string, string>)[key] ?? readableIdentifier(key)
}

function movingAverageLabel(
  condition: StrategySpecIndicatorCondition,
  triggerName: string,
): string | null {
  if (condition.indicator_id === 'technical.ma_cross') {
    const { fast_period: fast, slow_period: slow } = condition.params
    if (typeof fast !== 'number' || typeof slow !== 'number') return null
    const crossing = condition.trigger === 'golden_cross' ? '上穿'
      : condition.trigger === 'death_cross' ? '下穿' : null
    return crossing ? `${fast} 日均线${crossing} ${slow} 日均线` : null
  }
  if (condition.indicator_id !== 'technical.ma') return null
  const period = typeof condition.params.period === 'number' ? condition.params.period : null
  const prefix = period == null ? '均线' : `${period} 日均线`
  return `${triggerName} ${prefix}`
}

function explicitWindowLabel(
  condition: StrategySpecIndicatorCondition,
  triggerName: string,
): string | null {
  if (condition.indicator_id === 'price.rolling_high' && condition.trigger === 'new_high') {
    const field = ({ close: '收盘价', high: '最高价', low: '最低价', open: '开盘价' } as Record<string, string>)[String(condition.params.price_field)] ?? '价格'
    return `${field}创前 ${condition.params.period} 日新高`
  }
  if (condition.indicator_id === 'volume.relative' && condition.value != null) {
    const days = condition.trigger === 'consecutive_gte_multiple'
      ? `连续 ${condition.params.consecutive_days} 日` : ''
    const comparison = condition.trigger === 'consecutive_gte_multiple' ? '不低于' : triggerName
    return `${days}成交量${comparison}前 ${condition.params.baseline_period} 日均量的 ${formatConditionValue(condition.value)}倍`
  }
  return null
}

function toUiEventCondition(
  condition: StrategySpecEventCondition,
  id: string,
): StrategyEventCondition {
  const name = FALLBACK_EVENT_LABELS[condition.event_code]
    ?? readableEventCode(condition.event_code)
  const documentText = condition.document_text
  return {
    id,
    kind: 'event',
    eventCode: condition.event_code,
    label: documentText
      ? `${name}正文中“${documentText.term}”完整词出现 ${comparatorLabel(documentText.comparator)} ${documentText.value} 次`
      : `${name}发布`,
    trigger: documentText
      ? `先按首次可获得时间确认，再用冻结的正文提取版本计数`
      : '按首次可获得时间确认',
    attributes: condition.attributes,
    ...(documentText ? { documentText } : {}),
  }
}

const FINANCIAL_METRIC_LABELS: Record<string, string> = {
  'valuation.pe': '市盈率 PE',
  'valuation.pb': '市净率 PB',
  'valuation.ps': '市销率 PS',
  'valuation.pcf': '市现率 PCF',
  'valuation.pe_ttm': '滚动市盈率 PE-TTM',
  'valuation.pb_mrq': '市净率 PB-MRQ',
  'valuation.dividend_yield_ttm': '股息率 TTM',
  'financial.revenue': '营业收入',
  'financial.net_profit_parent': '归母净利润',
  'financial.revenue_yoy': '营收同比',
  'financial.net_profit_parent_yoy': '归母净利润同比',
  'financial.roe': '净资产收益率 ROE',
  'financial.gross_margin': '毛利率',
  'financial.net_margin': '净利率',
}

function financialMetricLabel(metricId: string): string {
  return FINANCIAL_METRIC_LABELS[metricId] ?? readableIdentifier(metricId)
}

function toUiFinancialCondition(
  condition: StrategySpecFinancialCondition,
  id: string,
): StrategyFinancialCondition {
  const name = financialMetricLabel(condition.metric_id)
  const comparison = `${comparatorLabel(condition.comparator)} ${condition.value}`
  return {
    id,
    kind: 'financial',
    metricId: condition.metric_id,
    label: `${name} ${comparison}`,
    trigger: '只使用历史当时已可得的接口原始值，日线收盘确认',
    comparator: condition.comparator,
    value: condition.value,
    unit: condition.unit,
    reportType: condition.report_type,
    periodBasis: condition.period_basis,
  }
}

function comparatorLabel(comparator: string): string {
  return ({ gt: '>', gte: '≥', eq: '=', lte: '≤', lt: '<' } as Record<string, string>)[comparator]
    ?? comparator
}

function toUiHoldingPeriod(
  condition: StrategySpecHoldingPeriodExit,
  id: string,
): StrategyHoldingPeriodCondition {
  return {
    id,
    kind: 'holding_period',
    label: `实际买入成交后第 ${condition.sessions} 个交易日卖出`,
    trigger: `从首次买入成交后的下一交易日起计，第 ${condition.sessions} 个 A 股交易日使用开盘价代理尝试卖出`,
    sessions: condition.sessions,
    anchor: condition.anchor,
    countMode: condition.count_mode,
    execution: condition.execution,
  }
}

function toUiPositionReturn(
  condition: StrategySpecPositionReturnExit,
  id: string,
): StrategyPositionReturnCondition {
  const isTakeProfit = condition.trigger === 'take_profit'
  return {
    id,
    kind: 'position_return',
    label: isTakeProfit
      ? `持仓收益达到 ${condition.threshold_pct}% 止盈`
      : `持仓亏损达到 ${condition.threshold_pct}% 止损`,
    trigger: '以首次实际买入成交为基准，后复权日线收盘确认；下一可交易日使用开盘价代理尝试卖出',
    exitTrigger: condition.trigger,
    thresholdPct: condition.threshold_pct,
    anchor: condition.anchor,
    observation: condition.observation,
    evaluationMode: condition.evaluation_mode,
    execution: condition.execution,
    editable: false,
  }
}

function toUiTrailingDrawdown(
  condition: StrategySpecTrailingDrawdownExit,
  id: string,
): StrategyTrailingDrawdownCondition {
  return {
    id,
    kind: 'trailing_drawdown',
    label: `持仓后收盘高点回撤 ${condition.threshold_pct}% 卖出`,
    trigger: '以实际买入后的后复权日线收盘高点为基准，收盘确认回撤；下一可交易日使用开盘价代理尝试卖出',
    thresholdPct: condition.threshold_pct,
    anchor: condition.anchor,
    peakBasis: condition.peak_basis,
    evaluationMode: condition.evaluation_mode,
    execution: condition.execution,
    editable: false,
  }
}

function readableEventCode(eventCode: string): string {
  const leaf = eventCode.split('.').at(-1) ?? eventCode
  return leaf.replace(/_published$/, '').replaceAll('_', ' ')
}

function strategyTitle(conditions: StrategyCondition[]): string {
  const names = [...new Set(conditions.map((condition) => {
    if (condition.kind === 'holding_period') return '持有期退出'
    if (condition.kind === 'position_return') return condition.exitTrigger === 'take_profit' ? '止盈' : '止损'
    if (condition.kind === 'trailing_drawdown') return '移动止盈'
    if (condition.kind === 'event') {
      return condition.label.split('正文')[0]?.replace(/发布$/, '') || readableEventCode(condition.eventCode)
    }
    if (condition.kind === 'financial') return financialMetricLabel(condition.metricId)
    return readableIdentifier(condition.indicatorId)
  }))]
  return `${names.join(' + ')} 规则 · 日线`
}

function applyDraftEdits(strategy: StrategySpec, draft: StrategyDraft): StrategySpec {
  return {
    ...strategy,
    entry: applyLegEdits(strategy.entry, draft.entry.conditions),
    exit: {
      ...strategy.exit,
      children: applyConditionList(strategy.exit.children, draft.exit.conditions),
    },
    backtest: {
      start: draft.backtest.start,
      end: draft.backtest.end,
      initial_cash_cny: draft.backtest.initialCashCny,
    },
  }
}

function applyLegEdits(
  condition: StrategySpecCondition,
  edits: StrategyCondition[],
): StrategySpecCondition {
  let cursor = 0
  const visit = (node: StrategySpecCondition): StrategySpecCondition => {
    if (
      node.type === 'indicator_condition'
      || node.type === 'financial_condition'
      || node.type === 'event_condition'
    ) {
      const edit = edits[cursor]
      cursor += 1
      return node.type === 'indicator_condition' && edit?.kind === 'indicator'
        ? applyIndicatorEdit(node, edit)
        : node
    }
    if (node.type === 'not') return { ...node, child: visit(node.child) }
    return { ...node, children: node.children.map(visit) }
  }
  return visit(condition)
}

function applyConditionList(
  conditions: StrategySpecExitRule[],
  edits: StrategyCondition[],
): StrategySpecExitRule[] {
  let cursor = 0
  const visitCondition = (node: StrategySpecCondition): StrategySpecCondition => {
    if (
      node.type === 'indicator_condition'
      || node.type === 'financial_condition'
      || node.type === 'event_condition'
    ) {
      const edit = edits[cursor]
      cursor += 1
      return node.type === 'indicator_condition' && edit?.kind === 'indicator'
        ? applyIndicatorEdit(node, edit)
        : node
    }
    if (node.type === 'not') return { ...node, child: visitCondition(node.child) }
    return { ...node, children: node.children.map(visitCondition) }
  }
  const visit = (node: StrategySpecExitRule): StrategySpecExitRule => {
    if (
      node.type === 'holding_period_exit'
      || node.type === 'position_return_exit'
      || node.type === 'trailing_drawdown_exit'
    ) {
      cursor += 1
      return node
    }
    return visitCondition(node)
  }
  return conditions.map(visit)
}

function applyIndicatorEdit(
  condition: StrategySpecIndicatorCondition,
  edit: StrategyIndicatorCondition,
): StrategySpecIndicatorCondition {
  const parameterValues = new Map(edit.parameters.map((parameter) => [parameter.key, parameter.value]))
  return {
    ...condition,
    params: Object.fromEntries(Object.entries(condition.params).map(([key, value]) => [
      key,
      typeof value === 'number' ? (parameterValues.get(key) ?? value) : value,
    ])),
    value: parameterValues.get('$value') ?? condition.value,
  }
}
