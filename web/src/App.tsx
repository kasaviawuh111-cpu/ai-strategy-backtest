import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'

import { FailureCard, ResultCard, RunningCard, StrategyCard } from './components/SummaryCards'
import { BacktestReview } from './components/BacktestReview'
import { StrategySlotComposer } from './components/StrategySlotComposer'
import { StrategyExamples } from './components/StrategyExamples'
import { StrategyGallery } from './components/StrategyGallery'
import { UsageGuide } from './components/UsageGuide'
import { ModelReasoning } from './components/ModelReasoning'
import { ColumnResizer } from './components/ColumnResizer'
import { useStoredColumnWidths } from './components/column-widths'
import { Proposals, type ProposalItem } from './components/Proposals'
import { gridComparisonTitles, gridProposalPresentation } from './shared/price-plan'
import {
  BackIcon, Bubble, Chip, Chips, DayDivider, Notice, Say, ThinkBlock,
  ThinkingStream, Turn,
} from './components/primitives'
import { ExecutionDetailsScreen, ExecutionEntry, ParamsScreen, ReportBody } from './screens'
import { apiMode, backtestApi, instrumentApi, strategyApi, systemApi } from './shared/api/client'
import type { DialogueProgressEvent, DialogueProgressObserver, PreviewPollRecovery } from './shared/api/client'
import { STRATEGY_RETRY_MESSAGE, fromBacktestOptimizationCandidate, ideaProposalRuleSummaries, ideaProposalTitle, toLiveBacktestBody, toLiveRevisionBody } from './shared/api/contract'
import { ApiError } from './shared/api/types'
import type {
  BacktestOptimizationCandidate,
  BacktestReviewResponse,
  BacktestRun,
  CapabilitiesResponse,
  Clarification,
  ClarificationData,
  ClarificationSuggestion,
  CompileRequest,
  ExecutionSettings,
  IdeaRouteProposal,
  Instrument as ApiInstrument,
  StrategyDraft,
} from './shared/api/types'
import {
  MAXIMUM_INITIAL_CASH_CNY,
  MINIMUM_INITIAL_CASH_CNY,
} from './shared/config/backtest'
import { validateBacktestDates } from './shared/backtest-date-validation'
import { DEFAULT_INSTRUMENT, toAshareInstrument } from './shared/instrument-context'
import type { StrategyExample } from './shared/default-strategy-examples'
import {
  readRecentBacktests, loadCompletedReports, persistCompletedReports, MAX_RECENT_BACKTESTS,
  type CompletedReportSnapshot,
} from './shared/recent-backtests'
import type {
  EditableRow,
  FailureState,
  Instrument,
  StrategySummary,
} from './types'
import {
  assessStrategyCapabilities,
  backtestPreparationFailureReason,
  summarizeExecution,
  summarizeRule,
  strategyRuleTrees,
  toBacktestMetrics,
  toChartMarks,
  toRunEvidence,
  toSeries,
  toStrategySummary,
  toTradeRows,
  toUiInstrument,
} from './view-model'
import './styles/app.css'

type Overlay = 'params' | 'execution'
type WorkspaceView = 'chat' | 'detail' | 'gallery'
const isGalleryLocation = () => window.location.hash === '#strategies'

const executableRuleText = (draft: StrategyDraft): string => {
  const rules = toStrategySummary(draft).rows.slice(0, 2)
    .map((row) => `${row.label}：${row.value}`).join('；')
  return `${draft.instrument.name}；${rules}；${draft.backtest.start} 至 ${draft.backtest.end}`
}

type ClarificationMessage = {
  role: 'assistant' | 'user'
  text: string
  data?: ClarificationData
}

type ClarificationTarget = {
  draftId: string
  revision?: number
  originalRequest: CompileRequest
  executionContext?: Partial<ExecutionSettings>
}

type UnrunDraftSnapshot = {
  draft: StrategyDraft
  baseline?: StrategyDraft
  submittedText?: string
  fromPanelEdit: boolean
  messages: ClarificationMessage[]
  reviewOpen: boolean
}

type JourneySnapshot = CompletedReportSnapshot & {
  utterance: string
  fromPanelEdit?: boolean
  clarificationMessages: ClarificationMessage[]
}

const historyJourney = (snapshot: CompletedReportSnapshot): JourneySnapshot => ({
  ...snapshot, utterance: '', clarificationMessages: [],
})

/**
 * 策略名。必须由「编译出来的策略」推导，不能用用户第一句原话：
 * 多轮澄清之后，最终规则可能和第一句毫无关系（线上出现过标题是「你可以干啥」）。
 * 名字跟着要执行的东西走，才不会和实际跑的规则对不上。
 */
const compactLegTitle = (summary: string, action: '买入' | '卖出'): string =>
  summary.endsWith(action) || summary.endsWith('退出') ? summary : `${summary}${action}`

const planStrategyTitle = (strategy: StrategySummary): string | null => {
  if (!strategy.pricePlanKind) return null
  if (strategy.pricePlanKind !== 'grid') return strategy.title
  const gridBuy = Boolean(strategy.gridReview?.buy.length)
  const gridSell = Boolean(strategy.gridReview?.sell.length)
  if (gridBuy && gridSell) return '下跌补仓、上涨卖出网格'
  if (gridBuy) return '下跌补仓网格策略'
  if (gridSell) return '上涨卖出网格策略'
  return '网格交易策略'
}

const strategyTitle = (_instrument: Instrument, strategy: StrategySummary): string =>
  (strategy.independentPlanKinds ? strategy.title : planStrategyTitle(strategy))
    ?? `${compactLegTitle(summarizeRule(strategy.entryRule), '买入')}，${compactLegTitle(summarizeRule(strategy.exitRule), '卖出')}`

const terminalStates = new Set(['succeeded', 'failed', 'cancelled'])
/** 点「开始回测」时替用户发出的那句话；和按钮文案保持同一个词。 */
const RUN_COMMAND = '开始回测'
const cloneDraft = (draft: StrategyDraft): StrategyDraft => JSON.parse(JSON.stringify(draft)) as StrategyDraft
const isMobileReview = (): boolean =>
  window.matchMedia?.('(max-width: 719px)').matches ?? window.innerWidth <= 719
const sameExecutableDraft = (candidate: StrategyDraft, baseline: StrategyDraft): boolean =>
  JSON.stringify({
    strategy: toLiveRevisionBody(candidate).strategy,
    config: toLiveBacktestBody(candidate).config,
  }) === JSON.stringify({
    strategy: toLiveRevisionBody(baseline).strategy,
    config: toLiveBacktestBody(baseline).config,
  })
const instrumentClarificationIds = new Set([
  'instrument_required',
  'instrument_unconfirmed',
  'instrument_resolution_unavailable',
])
const isInstrumentClarification = (
  clarification: Clarification | undefined,
): boolean =>
  Boolean(clarification && instrumentClarificationIds.has(clarification.id))

const visibleClarificationChoices = (clarification: Clarification): typeof clarification.choices =>
  clarification.choices.filter((choice) => choice.action !== 'edit_utterance')

const ENTRY_ACTION = /(?:买入|买进|建仓|开仓|低吸|抄底)/
const EXIT_ACTION = /(?:卖出|卖掉|退出|平仓|清仓|止盈|止损)/

/**
 * Structured plans may intentionally contain only one side (for example DCA).
 * Displaying a server plan is not execution approval. Text-only suggestions
 * still need a complete buy/sell sentence; never invent the missing rules.
 */
const isCompleteIdeaProposal = (proposal: IdeaRouteProposal): boolean => {
  const utterance = proposal.suggested_utterance.trim()
  const plan = (proposal.strategy ?? proposal.strategy_template)?.trading_plan ?? proposal.grid_plan
  if (plan && utterance) {
    if (plan.kind === 'grid' || plan.kind === 'scheduled') return true
    if (plan.kind === 'conditional') return Array.isArray(plan.parameters.rules) && plan.parameters.rules.length > 0
  }
  return Boolean(
    proposal.entry_summary.trim()
    && proposal.exit_summary.trim()
    && ENTRY_ACTION.test(utterance)
    && EXIT_ACTION.test(utterance),
  )
}

const orderedIdeaProposals = (
  clarification: Clarification | undefined,
  suggestions: ClarificationSuggestion[],
): IdeaRouteProposal[] => {
  const proposals = clarification?.ideaRoute?.proposals ?? []
  if (!proposals.length) return []
  const byId = new Map(proposals.map((proposal) => [proposal.id, proposal]))
  const ranked = suggestions
    .map((suggestion) => byId.get(suggestion.id))
    .filter((proposal): proposal is IdeaRouteProposal => Boolean(proposal))
  const rankedIds = new Set(ranked.map((proposal) => proposal.id))
  return [...ranked, ...proposals.filter((proposal) => !rankedIds.has(proposal.id))]
    .filter(isCompleteIdeaProposal)
    .slice(0, 3)
}

const ideaProposalInstrument = (
  clarification: Clarification,
  target: ClarificationTarget | undefined,
  proposal: IdeaRouteProposal,
): string | undefined => {
  const symbol = proposal.instrument_symbol
    ?? clarification.ideaRoute?.asset_mapping.instrument_symbol
    ?? undefined
  if (!symbol) return undefined
  const groundedChoice = clarification.choices.find((choice) =>
    choice.id === proposal.id && choice.instrumentSymbol === symbol && choice.instrumentName)
  const targetInstrument = target?.originalRequest.instrument
  const targetIsGrounded = target?.originalRequest.instrumentContextSource !== 'standalone_default'
    && targetInstrument?.symbol === symbol
  const name = proposal.instrument_name ?? groundedChoice?.instrumentName
    ?? (targetIsGrounded ? targetInstrument?.name : undefined)
  return name ? `${name} · ${symbol}` : symbol
}

const ideaProposalCards = (
  clarification: Clarification | undefined,
  target: ClarificationTarget | undefined,
  suggestions: ClarificationSuggestion[],
  capabilities?: CapabilitiesResponse,
): ProposalItem[] => {
  if (!clarification?.ideaRoute) return []
  const proposals = orderedIdeaProposals(clarification, suggestions)
  const plans = proposals.map(proposal => proposal.grid_plan ?? proposal.strategy?.trading_plan ?? proposal.strategy_template?.trading_plan)
  const comparisonTitles = gridComparisonTitles(plans)
  return proposals.map((proposal, index) => {
    const plan = plans[index]
    return ({
    id: proposal.id,
    title: proposal.pairing_reason
      ? `${proposal.instrument_name ?? proposal.instrument_symbol} · ${ideaProposalTitle(proposal)}`
      : ideaProposalTitle(proposal),
    paired: Boolean(proposal.pairing_reason),
    detail: proposal.pairing_reason ?? undefined,
    instrument: ideaProposalInstrument(clarification, target, proposal),
    ...ideaProposalRuleSummaries(proposal, capabilities),
    ...(plan?.kind === 'grid' ? gridProposalPresentation(plan, comparisonTitles[index],
      proposal.strategy ?? proposal.strategy_template ?? undefined) : {}),
  })})
}

const clarificationAnswerMessage = (
  assistantMessage: string,
): string => assistantMessage

type DisplayDataTable = {
  title?: string
  columns: string[]
  rows: unknown[][]
}

const providerTable = (table: Record<string, unknown>): DisplayDataTable | undefined => {
  const title = typeof table.title === 'string' ? table.title : undefined
  const rawTable = table.rawTable
  const source = rawTable && typeof rawTable === 'object' && !Array.isArray(rawTable)
    ? rawTable as Record<string, unknown>
    : table
  const rawColumns = Array.isArray(source.headers)
    ? source.headers
    : Array.isArray(source.columns) ? source.columns : []
  const columns = rawColumns.map((value) => String(value))
  const rawRows = Array.isArray(source.data)
    ? source.data
    : Array.isArray(source.rows) ? source.rows : []
  if (!columns.length || !rawRows.length) return undefined
  const rows = rawRows.map((row) => {
    if (Array.isArray(row)) return row
    if (row && typeof row === 'object') {
      const record = row as Record<string, unknown>
      return columns.map((column) => record[column])
    }
    return [row]
  })
  return { title, columns, rows }
}

const currentDataTables = (data: ClarificationData): DisplayDataTable[] => {
  if (data.kind === 'screen') {
    return [{
      title: `${data.screen.assetType}选股结果`,
      columns: data.screen.columns,
      rows: data.screen.rows.map((row) => data.screen.columns.map((column) => row[column])),
    }]
  }
  if (data.kind === 'finance') {
    return data.finance.tables.map(providerTable).filter((table): table is DisplayDataTable => Boolean(table))
  }
  const screen = data.screenedFinance.screen
  return [
    {
      title: `先选出 ${data.screenedFinance.entities.length} 只标的`,
      columns: screen.columns,
      rows: screen.rows.map((row) => screen.columns.map((column) => row[column])),
    },
    ...data.screenedFinance.batches
      .flatMap((batch) => batch.tables)
      .map(providerTable)
      .filter((table): table is DisplayDataTable => Boolean(table)),
  ]
}

const firstText = (
  row: Record<string, unknown>,
  keys: readonly string[],
): string | undefined => {
  for (const key of keys) {
    const value = row[key]
    if (typeof value === 'string' || typeof value === 'number') {
      const text = String(value).trim()
      if (text) return text
    }
  }
  return undefined
}

const screenRowInstruments = (
  rows: Array<Record<string, unknown>>,
): ApiInstrument[] => rows.flatMap((row) => {
  const code = firstText(row, ['证券代码', '股票代码', '代码'])
  if (!code) return []
  const instrument = toAshareInstrument(
    code,
    firstText(row, ['证券简称', '股票简称', '股票名称', '名称', '简称', '股票']),
  )
  return instrument ? [instrument] : []
})

/** Only expose actions for provider-grounded A-share identities. */
const currentDataInstruments = (data: ClarificationData): ApiInstrument[] => {
  let instruments: ApiInstrument[] = []
  if (data.kind === 'screen' && data.screen.assetType === 'A股') {
    instruments = screenRowInstruments(data.screen.rows)
  } else if (data.kind === 'screened_finance') {
    instruments = data.screenedFinance.entities.flatMap((entity) => {
      if (entity.assetType !== 'A股') return []
      const instrument = toAshareInstrument(entity.code, entity.name)
      return instrument ? [instrument] : []
    })
  }
  const unique = new Map(instruments.map((item) => [item.symbol, item]))
  return [...unique.values()].slice(0, 8)
}

const researchSourceUrl = (url: string): string | undefined => {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : undefined
  } catch {
    return undefined
  }
}

function IdeaResearchEvidence({ clarification }: { clarification: Clarification }) {
  const research = clarification.ideaRoute?.research
  if (!research?.sources.length) return null
  return (
    <details className="idea-research" aria-label="联网分析依据">
      <summary>查看参考来源</summary>
        <div className="idea-research__sources" aria-label="联网信息来源">
          {research.sources.slice(0, 5).map((source) => {
            const href = researchSourceUrl(source.url)
            return href ? (
              <a key={source.source_id} href={href} target="_blank" rel="noreferrer">
                {source.title}
              </a>
            ) : null
          })}
        </div>
    </details>
  )
}

function CurrentDataResult({
  data,
  onAnalyze,
}: {
  data: ClarificationData
  onAnalyze?: (instrument: ApiInstrument) => void
}) {
  // A direct finance lookup is already summarized in the assistant sentence.
  // Keep the structured payload in state for auditability, but do not expose
  // provider field names, hashes or transport metadata in the conversation.
  if (data.kind === 'finance') return null
  const tables = currentDataTables(data)
  const instruments = currentDataInstruments(data)
  return (
    <div className="current-data" aria-label="实时查询结果">
      {tables.map((table, index) => (
        <section className="current-data__table" key={`${table.title ?? '查询结果'}-${index}`}>
          {table.title ? <h3>{table.title}</h3> : null}
          <div className="current-data__scroll">
            <table>
              <thead>
                <tr>{table.columns.slice(0, 8).map((column) => <th key={column}>{column}</th>)}</tr>
              </thead>
              <tbody>
                {table.rows.slice(0, 12).map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {table.columns.slice(0, 8).map((_, columnIndex) => (
                      <td key={columnIndex}>{String(row[columnIndex] ?? '—')}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {table.rows.length > 12 ? <p className="current-data__more">已展示前 12 条，共 {table.rows.length} 条</p> : null}
        </section>
      ))}
      {onAnalyze && instruments.length ? (
        <div className="current-data__actions" aria-label="选择标的继续分析">
          <p>选一只继续：先联网核实公开信息，再给出可由你确认的回测策略。</p>
          {instruments.map((instrument) => (
            <button
              type="button"
              key={instrument.symbol}
              onClick={() => onAnalyze(instrument)}
            >
              联网分析并给回测策略：{instrument.name} · {instrument.symbol}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

// The server returns one complete model reply; choices remain separate UI controls.
const clarificationMessage = (clarification: Clarification): string => clarification.question

const hasConfirmedIdeaInstrument = (clarification: Clarification | undefined): boolean => {
  const route = clarification?.ideaRoute
  const symbol = route?.asset_mapping.instrument_symbol
  return Boolean(symbol && route?.proposals.length
    && route.proposals.every(proposal => proposal.instrument_symbol === symbol))
}

const clarificationPlaceholder = (clarification: Clarification | undefined): string => {
  if (!clarification) return '说出什么时候买、什么时候卖'
  if (clarification.id === 'candidate_data_not_ready'
    || clarification.id === 'capability_research_fallback'
    || clarification.id === 'semantic_confirmation_required') {
    return '可以继续讨论策略，无需重复输入'
  }
  if (hasConfirmedIdeaInstrument(clarification)) return '选个方案，或说说你想怎么调整'
  if (clarification.instrumentSuggestions?.length) return '也可以输入你想用的股票'
  if (isInstrumentClarification(clarification)) return '输入股票名称或 6 位代码'
  if (clarification.id === 'entry_rule_not_recognized') return '补充什么时候买入'
  if (clarification.id === 'exit_rule_not_recognized') return '补充什么时候卖出'
  if (clarification.ideaRoute
    && clarification.ideaRoute.asset_mapping.instrument_symbol == null) {
    return '选个方向，或说说你想用哪只股票'
  }
  if (clarification.ideaRoute) return '回复序号，或直接说完整规则'
  return '补充完整规则，或直接换一种说法'
}

const contextRecoveryMessage = (error: unknown): string | undefined => {
  const code = errorCode(error)
  if (code === 'conversation_parent_draft_not_found' || code === 'strategy_draft_not_found') {
    return '这轮策略的服务端记录已无法读取，本次修改和回测未执行。请从原报告进入“换个条件再回测”或“换只股票试试”；没有报告时，请新建会话并重新输入完整策略。'
  }
  if (code === 'conversation_parent_draft_stale' || code === 'strategy_draft_revision_stale') {
    return '这轮策略版本已更新，本次修改未提交。请从原报告重新进入修改；没有报告时，请新建会话并重新输入完整策略。'
  }
  if (code === 'strategy_edit_context_required') {
    return '没有找到这次修改对应的原策略，本次未执行回测。请从原报告重新进入修改；没有报告时，请新建会话并重新输入完整策略。'
  }
  if (code === 'backtest_review_context_unavailable') {
    return '这版优化建议的服务端记录已无法读取，尚未执行新回测。请回到原报告，点击“重试 AI 分析”后重新选择。'
  }
  if (code === 'backtest_review_result_changed') {
    return '回测结果已更新，原优化建议不能直接沿用。请回到原报告，点击“重试 AI 分析”后重新选择。'
  }
  if (code === 'backtest_run_not_found') return '这次回测记录已无法读取，请重新发起回测。'
  return undefined
}

const isReviewContextError = (error: unknown): boolean =>
  errorCode(error) === 'backtest_review_context_unavailable'
  || errorCode(error) === 'backtest_review_result_changed'

const compileRecoveryMessage = (error: unknown): string => {
  const contextMessage = contextRecoveryMessage(error)
  if (contextMessage) return contextMessage
  const code = errorCode(error)?.toLowerCase() ?? ''
  if (code === 'api_timeout' || code === 'api_network_unavailable'
    || code.startsWith('candidate_provider_')) return STRATEGY_RETRY_MESSAGE
  if (code === 'skill_indicator_unavailable' || code.startsWith('live_market_data_')) {
    return errorMessage(error)
  }
  if (code === 'previous_session_limit_up_capability_unavailable') {
    return '已理解“前一交易日涨停”这一条件，所需的历史涨停状态与执行支持尚未齐备。原要求已保留，本次未开始回测。'
  }
  if (code.includes('document_text')) {
    return '已理解使用报告正文作为条件，所需的历史正文数据尚未齐备。原要求已保留，本次未开始回测。'
  }
  if (
    code.includes('no_candidate')
    || code.includes('no_supported_signal')
    || code.includes('not_recognized')
    || code.includes('compile_unsupported')
  ) {
    return '我还没能把这句话还原成完整的买卖规则。请在下方用一句话补充股票、买入和卖出条件；可以从价格阈值、涨跌幅，或 MACD / 均线 / RSI 中选一个方向。'
  }
  if (code.includes('capability') || code.includes('unavailable')) {
    return errorMessage(error)
  }
  return `策略生成请求未完成：${errorMessage(error)}`
}

const compileFailurePlaceholder = (error: unknown): string => {
  const code = errorCode(error) ?? ''
  if (code === 'candidate_provider_connection_failed' || code === 'candidate_provider_timeout') {
    return '你的输入和已有策略已保留'
  }
  if (code.startsWith('candidate_provider_') || code === 'api_timeout'
    || code === 'api_network_unavailable' || code.startsWith('preview_')) {
    return '你的输入和已有策略已保留'
  }
  if (code === 'entry_rule_not_recognized') return '补充什么时候买入'
  if (code === 'exit_rule_not_recognized') return '补充什么时候卖出'
  if (code === 'strategy_rule_incomplete' || code === 'no_supported_signal_recognized') {
    return '补充完整规则，或直接换一种说法'
  }
  return '原话已保留，可查看提示后继续'
}

const errorMessage = (error: unknown) => {
  const contextMessage = contextRecoveryMessage(error)
  if (contextMessage) return contextMessage
  if (error instanceof ApiError) {
    return error.problem.detail
  }
  if (error instanceof Error) return error.message
  return '操作未完成，请稍后重试。'
}

const errorCode = (error: unknown) => error instanceof ApiError ? error.problem.code : undefined

const backtestFailureMessage = (run: BacktestRun): string => {
  switch (run.error) {
    case 'skill_mx_auth_failed':
    case 'skill_mx_read_timeout':
    case 'skill_mx_connect_timeout':
    case 'skill_mx_transport_error':
    case 'skill_mx_http_error':
    case 'skill_MxSaasProviderAuthError':
    case 'skill_MxSaasProviderUnavailableError':
      return '这次没能取到回测所需的行情，暂时无法计算结果。你的策略和设置已保留，可以稍后重试。'
    case 'skill_mx_no_data':
    case 'skill_MxSaasProviderNoDataError':
      return '暂时没有这段时间的完整行情，无法计算回测结果。你的策略和设置已保留，可以稍后重试。'
    case 'skill_MxSaasProviderDataError':
    case 'skill_MxDailyHistoryError':
      return '这次取到的行情或指标还无法用于计算，没有生成回测结果。你的策略和设置已保留，可以稍后重试。'
    case 'skill_history_fields_missing':
    case 'skill_history_before_listing':
    case 'skill_history_dates_mismatch':
    case 'skill_history_validation_failed':
    case 'minute_data_unavailable':
    case 'minute_execution_unavailable':
    case 'grid_execution_unavailable':
    case 'grid_latest_quote_unavailable':
    case 'grid_parameters_unavailable':
    case 'opening_holdings_data_unavailable':
    case 'opening_holdings_exceed_equity':
    case 'condition_reference_unavailable':
    case 'condition_cost_unavailable':
    case 'execution_capability_unavailable':
    case 'scheduled_execution_unavailable':
    case 'corporate_action_data_unavailable':
    case 'market_calendar_unavailable':
    case 'daily_execution_data_unavailable':
      return run.progressLabel
    default:
      return run.progressLabel && run.progressLabel !== run.error
        ? run.progressLabel : '本次回测未完成，原规则与设置已保留。'
  }
}

const failureFor = (
  error: unknown,
  fallbackKey: string,
  fallbackTitle: string,
  instrument: ApiInstrument,
  runId?: string,
  failureCode?: string | null,
): FailureState => {
  // A terminal job returns a machine code separately from its user-facing reason.
  // Never classify that job by its translated prose.
  const code = failureCode ?? errorCode(error) ?? (error instanceof Error ? error.message : '')
  const normalized = code.toLowerCase()

  if (normalized === 'skill_history_before_listing') {
    return {
      key: 'history_range_invalid', status: 'unavailable', title: '请调整回测区间',
      reason: errorMessage(error), actions: [{ label: '修改回测区间', action: 'edit_range' }], runId,
    }
  }
  if (['grid_parameters_unavailable', 'grid_buy_quantity_invalid', 'opening_holdings_exceed_equity',
    'condition_reference_unavailable'].includes(normalized)) {
    return {
      key: 'execution_settings_invalid', status: 'unavailable', title: '请检查策略设置',
      reason: errorMessage(error), actions: [{ label: '检查策略设置', action: 'edit_settings' }], runId,
    }
  }
  if (['minute_execution_unavailable', 'grid_execution_unavailable',
    'scheduled_execution_unavailable'].includes(normalized)) {
    return {
      key: 'capability_unavailable', status: 'unavailable', title: '当前执行方式暂不支持',
      reason: errorMessage(error), actions: [{ label: '查看与修改规则', action: 'edit_rules' }], runId,
    }
  }

  if (normalized === 'skill_indicator_unavailable') {
    return {
      key: 'capability_unavailable',
      status: 'unavailable',
      title: '历史指标数据暂不可用',
      reason: errorMessage(error),
      actions: [{ label: '修改规则', action: 'edit_rules' }, { label: '使用技术示例', action: 'use_example' }],
      runId,
    }
  }
  if (normalized.includes('instrument_required')) {
    return {
      key: 'missing_stock',
      status: 'rejected',
      title: '缺少股票',
      reason: `请使用当前股票“${instrument.name} ${instrument.symbol}”，或返回股票页后再试。`,
      actions: [{ label: '检查股票与规则', action: 'edit_rules' }],
    }
  }
  if (
    normalized.includes('document_text')
    && /(unavailable|missing|coverage|extraction|not_executable)/.test(normalized)
  ) {
    return {
      key: 'data_incomplete',
      status: 'partial',
      title: '缺少报告正文数据',
      reason: `${errorMessage(error)} 系统不会用公告标题代替正文，也不会猜测词频。请补齐可校验的报告正文快照后再运行。`,
      actions: [{ label: '修改规则', action: 'edit_rules' }, { label: '使用技术示例', action: 'use_example' }],
      runId,
    }
  }
  if (
    normalized.includes('no_candidate')
    || normalized.includes('no_supported_signal')
    || normalized.includes('not_recognized')
    || normalized.includes('template_not_published')
    || normalized.includes('compile_unsupported')
  ) {
    return {
      key: 'unsupported_strategy',
      status: 'unavailable',
      title: '策略暂不可执行',
      reason: errorMessage(error),
      actions: [{ label: '修改规则', action: 'edit_rules' }, { label: '使用技术示例', action: 'use_example' }],
      runId,
    }
  }
  if (
    normalized.includes('event_not_available_for_backtest')
    || normalized.includes('financial_data_unavailable')
    || normalized.includes('backtest_service_unavailable')
    || normalized.includes('capability')
  ) {
    return {
      key: 'capability_unavailable',
      status: 'unavailable',
      title: '暂时无法回测',
      reason: errorMessage(error),
      actions: [{ label: '修改规则', action: 'edit_rules' }, { label: '使用技术示例', action: 'use_example' }],
      runId,
    }
  }
  if (
    normalized.includes('no_validated_second_precision_time')
    || normalized.includes('time_quality')
    || normalized.includes('date_only')
  ) {
    return {
      key: 'event_time_insufficient',
      status: 'unavailable',
      title: '事件时间质量不足',
      reason: '目前无法证明事件在历史上的精确首次可得时间。为避免偷看未来数据，本次不触发交易。',
      actions: [{ label: '改用技术策略', action: 'use_example' }, { label: '修改规则', action: 'edit_rules' }],
      runId,
    }
  }
  if (
    normalized.includes('event_data_unavailable')
    || normalized.includes('events_were_not_included')
    || normalized.includes('acquisition_coverage')
    || normalized.includes('snapshot')
  ) {
    return {
      key: 'data_incomplete',
      status: 'partial',
      title: '数据不完整',
      reason: '当前快照没有覆盖这条策略需要的事件或行情，系统不会自动联网补数或放宽规则。',
      actions: [{ label: '改用技术策略', action: 'use_example' }, { label: '修改规则', action: 'edit_rules' }],
      runId,
    }
  }

  return {
    key: fallbackKey,
    status: 'rejected',
    title: fallbackTitle,
    reason: errorMessage(error),
    actions: runId ? [{
      label: fallbackKey === 'run_failed' && normalized === 'query_temporarily_unavailable'
        ? '重新获取数据' : fallbackKey === 'run_failed'
        && !/(timeout|transport|provider|network|data|history|calendar|quote|cost)/.test(normalized)
        ? '重新回测' : '重新读取',
      action: fallbackKey === 'result_failed' ? 'read_results'
        : fallbackKey === 'run_failed' ? 'retry_run' : 'read_run',
    }, { label: '修改规则', action: 'edit_rules' }]
      : [{ label: '修改规则', action: 'edit_rules' }, { label: '使用技术示例', action: 'use_example' }],
    runId,
  }
}

const draftValidation = (draft: StrategyDraft) => {
  const conditions = [...draft.entry.conditions, ...draft.exit.conditions]
  const intrabar = conditions.some((condition) =>
    condition.kind === 'indicator' && condition.evaluationMode === 'intrabar_streaming')
  const invalidParameter = conditions.some((condition) => condition.kind === 'indicator'
    && condition.parameters.some((parameter) => !Number.isFinite(parameter.value)
      || parameter.value < parameter.min
      || parameter.value > parameter.max
      || Boolean(parameter.integer && !Number.isInteger(parameter.value))))
  const invalidCash = !Number.isInteger(draft.backtest.initialCashCny)
    || draft.backtest.initialCashCny < MINIMUM_INITIAL_CASH_CNY
    || draft.backtest.initialCashCny > MAXIMUM_INITIAL_CASH_CNY
  const dateValidation = validateBacktestDates(draft.backtest.start, draft.backtest.end)
  const invalidExecution = !Number.isFinite(draft.execution.participationRate)
    || draft.execution.participationRate <= 0
    || draft.execution.participationRate > 1
    || !Number.isFinite(draft.execution.allocationRatio)
    || draft.execution.allocationRatio <= 0
    || draft.execution.allocationRatio > 1
    || !Number.isFinite(draft.execution.slippageBps)
    || draft.execution.slippageBps < 0
    || draft.execution.slippageBps > 1_000
    || !Number.isFinite(draft.execution.commissionRate)
    || draft.execution.commissionRate < 0
    || !Number.isFinite(draft.execution.minimumCommissionCny)
    || draft.execution.minimumCommissionCny < 0
    || !Number.isInteger(draft.execution.maxExitAttempts)
    || draft.execution.maxExitAttempts < 1
    || draft.execution.maxExitAttempts > 1_000
    || !Number.isInteger(draft.execution.warmupCalendarDays)
    || draft.execution.warmupCalendarDays < 0
    || draft.execution.warmupCalendarDays > 3_650
    || !Number.isInteger(draft.execution.settlementExtensionDays)
    || draft.execution.settlementExtensionDays < 1
    || draft.execution.settlementExtensionDays > 365
  if (intrabar) return { valid: false, reason: '分钟行情与分钟撮合尚未接入，盘中策略暂不可运行。' }
  if (invalidParameter) return { valid: false, reason: '有指标参数超出允许范围，请打开参数页检查。' }
  if (invalidCash) return { valid: false, reason: '初始资金需为 1 万元至 10 亿元之间的整数。' }
  if (!dateValidation.valid) return dateValidation
  if (invalidExecution) return { valid: false, reason: '高级执行参数超出后端允许范围，请打开设置检查。' }
  return { valid: true, reason: undefined }
}

type AppProps = {
  instrument?: ApiInstrument
  instrumentContextSource?: 'stock_page' | 'standalone_default'
  instrumentContextError?: string
  /**
   * 宿主返回入口。顶栏移除后本页不再自绘返回键——嵌入方自己有导航，
   * 独立打开时浏览器后退就够了。保留在类型里，免得调用方要改签名。
   */
  onReturnToStockPage?: () => void
  onUseStandaloneExample?: () => void
}

export default function App({
  instrument = DEFAULT_INSTRUMENT,
  instrumentContextSource = 'standalone_default',
  instrumentContextError,
}: AppProps) {
  const queryClient = useQueryClient()
  const maCrossExample = `${instrument.name}5日均线上穿20日均线买入，5日均线下穿20日均线卖出`
  const [utterance, setUtterance] = useState('')
  const selectedExample = useRef<StrategyExample | undefined>(undefined)
  const [strategySlots, setStrategySlots] = useState<{ draft: StrategyDraft; mode: 'stock' | 'rules' }>()
  const rerunAfterEdit = useRef(false)
  const refreshEditedRun = useRef(false)
  const [submittedText, setSubmittedText] = useState<string>()
  const [fromPanelEdit, setFromPanelEdit] = useState(false)
  const [draft, setDraft] = useState<StrategyDraft>()
  const currentDraftRef = useRef<StrategyDraft | undefined>(draft)
  useEffect(() => { currentDraftRef.current = draft }, [draft])
  const [baselineDraft, setBaselineDraft] = useState<StrategyDraft>()
  const [clarification, setClarification] = useState<Clarification>()
  const [clarificationTarget, setClarificationTarget] = useState<ClarificationTarget>()
  const returnToCandidateBatch = useRef<(() => void) | null>(null)
  const [clarificationMessages, setClarificationMessages] = useState<ClarificationMessage[]>([])
  const [clarificationPrompt, setClarificationPrompt] = useState<string>()
  const [clarificationData, setClarificationData] = useState<ClarificationData>()
  const [clarificationSuggestions, setClarificationSuggestions] = useState<ClarificationSuggestion[]>([])
  const [clarificationRequestFailed, setClarificationRequestFailed] = useState(false)
  const [runId, setRunId] = useState<string>()
  /**
   * 点「开始回测」是用户下的指令，所以它应该像用户说的一句话那样进入对话流，
   * 而不是卡片自己悄悄变成运行态——否则回看会话时，这一步没有留下任何痕迹。
   */
  const [runCommand, setRunCommand] = useState<string>()
  const [journeyHistory, setJourneyHistory] = useState<JourneySnapshot[]>([])
  const [recentBacktests, setRecentBacktests] = useState(() => readRecentBacktests(apiMode))
  const recentBacktestsRef = useRef(recentBacktests)
  useEffect(() => {
    let active = true
    void loadCompletedReports(apiMode).then(loaded => {
      if (!active) return
      const reports = new Map(loaded.reports.map(report => [report.id, report]))
      recentBacktestsRef.current.reports.forEach(report => reports.set(report.id, report))
      const next = { ...loaded, reports: [...reports.values()].slice(-MAX_RECENT_BACKTESTS) }
      recentBacktestsRef.current = next
      setRecentBacktests(next)
    })
    return () => { active = false }
  }, [])
  const [historyOpen, setHistoryOpen] = useState(false)
  const railRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!historyOpen) return
    const rail = railRef.current
    const media = window.matchMedia('(max-width: 1023px)')
    if (!rail || !media.matches) { setHistoryOpen(false); return }
    const toggle = rail.querySelector<HTMLButtonElement>('.rail-history-toggle')
    // The mobile drawer is modal: background content must not receive keyboard focus.
    const background = [...(rail.parentElement?.children ?? [])]
      .filter((node): node is HTMLElement => node instanceof HTMLElement && node !== rail && !node.classList.contains('rail-backdrop'))
      .map(node => ({ node, inert: node.inert }))
    background.forEach(({ node }) => { node.inert = true })
    toggle?.focus()
    const focusable = () => [...rail.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]')]
      .filter(node => node.getClientRects().length > 0)
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && rail.querySelector('#usage-guide:popover-open')) return
      if (event.key === 'Escape') { event.preventDefault(); setHistoryOpen(false); return }
      if (event.key !== 'Tab') return
      const items = focusable()
      const first = items[0], last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    const onFocus = (event: FocusEvent) => { if (!rail.contains(event.target as Node)) toggle?.focus() }
    const onResize = () => { if (!media.matches) setHistoryOpen(false) }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('focusin', onFocus)
    media.addEventListener('change', onResize)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('focusin', onFocus)
      media.removeEventListener('change', onResize)
      background.forEach(({ node, inert }) => { node.inert = inert })
      if (media.matches) toggle?.focus()
    }
  }, [historyOpen])
  const reviewGeneration = useRef(0)
  const exposedReviewReferences = useRef<NonNullable<CompileRequest['relatedReviews']>>([])
  const rememberReviewReference = (review: BacktestReviewResponse) => {
    const reference = { runId: review.runId, responseHash: review.modelProvenance.responseHash }
    exposedReviewReferences.current = [
      ...exposedReviewReferences.current.filter(item => item.runId !== reference.runId
        || item.responseHash !== reference.responseHash),
      reference,
    ].slice(-20)
  }
  // Explicit new strategy clears lineage; archived reports never supply context.
  const [conversationTailDraftId, setConversationTailDraftId] = useState<string>()
  const [reportSnapshot, setReportSnapshot] = useState<JourneySnapshot>()
  const [galleryReportId, setGalleryReportId] = useState<string>()
  const [reviewContextError, setReviewContextError] = useState<{ runId: string; message: string }>()

  const [stack, setStack] = useState<Overlay[]>([])
  const [paramsFocus, setParamsFocus] = useState<EditableRow['key'] | 'more'>('entry')
  const [conditionPath, setConditionPath] = useState<string>()
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  /**
   * 策略审阅栏的展开态。桌面端是常驻右栏（这个值只控制手机端那块贴底面板），
   * 新策略识别出来时自动展开——刚生成就该被看见。
   */
  const [reviewOpen, setReviewOpen] = useState(false)
  /**
   * 主区呈现哪一种版式：
   *   'chat'   —— 左栏 + 对话（+ 可选的策略审阅栏），也就是两栏 / 三栏
   *   'detail' —— 左栏 + 一整块策略详情，中间的对话让位（回测跑完后的落点）
   * 详情态不是「另一个页面」，只是同一块工作区换了一种排布，所以做成状态而不是路由。
   */
  const [view, setWorkspaceView] = useState<WorkspaceView>(() => isGalleryLocation() ? 'gallery' : 'chat')
  const [galleryEntryKey, setGalleryEntryKey] = useState(0)
  const setView = (next: WorkspaceView) => {
    // Only own the gallery hash; preserve the host's pathname and query context.
    if ((next === 'gallery') !== isGalleryLocation()) {
      const url = new URL(window.location.href)
      if (next === 'gallery') url.hash = 'strategies'
      else url.hash = ''
      window.history.pushState(null, '', url)
    }
    setWorkspaceView(next)
  }
  useEffect(() => {
    const syncNavigation = () => {
      setWorkspaceView(current => isGalleryLocation() ? 'gallery' : current === 'gallery' ? 'chat' : current)
      setHistoryOpen(false)
    }
    window.addEventListener('popstate', syncNavigation)
    window.addEventListener('hashchange', syncNavigation)
    return () => {
      window.removeEventListener('popstate', syncNavigation)
      window.removeEventListener('hashchange', syncNavigation)
    }
  }, [])
  const [detailTab, setDetailTab] = useState<'flow' | 'report'>('flow')
  /** 详情区显示哪一次回测：默认当前这次，左栏点历史时切过去。 */
  const [detailJourneyId, setDetailJourneyId] = useState<string>()
  const [dialogueProgress, setDialogueProgress] = useState<readonly DialogueProgressEvent[]>([])
  const [dialogueRecovery, setDialogueRecovery] = useState<PreviewPollRecovery | null>(null)
  const [isConnectionChecking, setIsConnectionChecking] = useState(false)
  const dialogueProgressAbortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const workspaceRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useStoredColumnWidths(workspaceRef)

  useEffect(() => () => dialogueProgressAbortRef.current?.abort(), [])

  const beginDialogueProgress = (): DialogueProgressObserver => {
    dialogueProgressAbortRef.current?.abort()
    const controller = new AbortController()
    dialogueProgressAbortRef.current = controller
    setDialogueProgress([])
    setDialogueRecovery(null)
    return {
      signal: controller.signal,
      onProgress: (events) => {
        if (!controller.signal.aborted) setDialogueProgress(events.slice(-12))
      },
      onRecovery: (state) => {
        if (!controller.signal.aborted) setDialogueRecovery(state)
      },
    }
  }

  const open = (overlay: Overlay) => setStack((current) =>
    current.includes(overlay) ? current : [...current, overlay])
  const openParams = (focus: EditableRow['key'] | 'more', path?: string) => {
    setParamsFocus(focus)
    setConditionPath(path)
    open('params')
  }
  const back = () => setStack((current) => current.slice(0, -1))

  const compileRequestFor = ({ text, instrumentOverride }: {
    text: string
    instrumentOverride?: ApiInstrument
  }): CompileRequest => ({
    instrument: instrumentOverride ?? instrument,
    instrumentContextSource: instrumentOverride ? 'stock_page' : instrumentContextSource,
    utterance: text,
  })

  const compileMutation = useMutation({
    mutationFn: ({ text, instrumentOverride, parentDraftId, editCurrentStrategy, executionContext,
      relatedRunIds, relatedReview, relatedReviews, dialogueProgress: observer }: {
      text: string
      instrumentOverride?: ApiInstrument
      parentDraftId?: string
      editCurrentStrategy?: boolean
      executionContext?: Partial<ExecutionSettings>
      relatedRunIds?: string[]
      relatedReview?: CompileRequest['relatedReview']
      relatedReviews?: CompileRequest['relatedReviews']
      dialogueProgress: DialogueProgressObserver
      previousUnrunDraft?: UnrunDraftSnapshot
    }) => {
      const request = {
        ...compileRequestFor({ text, instrumentOverride }),
        editCurrentStrategy,
        executionSettings: parentDraftId ? executionContext : undefined,
        relatedRunIds,
        relatedReview,
        relatedReviews,
        dialogueProgress: observer,
      }
      return parentDraftId
        ? strategyApi.compile(request, parentDraftId)
        : strategyApi.compile(request)
    },
    onSuccess: (outcome, variables) => {
      if (variables.dialogueProgress.signal?.aborted) return
      setDialogueRecovery(null)
      // A not-yet-run draft is still part of this conversation. Preserve its
      // visible turns when the server confirms this is an edit, just as we do
      // for follow-ups after a completed backtest.
      const previous = outcome.isStrategyEdit ? variables.previousUnrunDraft : undefined
      const editMessages: ClarificationMessage[] = previous
        ? [...previous.messages, { role: 'user', text: variables.text }]
        : []
      if (previous) setSubmittedText(previous.submittedText)
      // Even a rerun chip may need clarification or a user-requested pause.
      // Only the resolved server turn can authorize the new calculation.
      rerunAfterEdit.current = outcome.status === 'compiled' && outcome.runRequested === true
      refreshEditedRun.current = outcome.status === 'compiled'
        && outcome.runRequested === true && outcome.refreshData === true
      setConversationTailDraftId(
        outcome.status === 'compiled' ? outcome.draft.id : outcome.draftId,
      )
      setClarificationRequestFailed(false)
      setRunId(undefined)
      setStack([])
      setRunCommand(undefined)
      // 新的识别结果永远属于对话；识别出草稿就顺手把审阅栏打开
      setView('chat')
      setDetailJourneyId(undefined)
      setReviewOpen(outcome.status !== 'needs_clarification')
      if (outcome.status === 'needs_clarification') {
        setDraft(undefined)
        setBaselineDraft(undefined)
        setClarification(outcome.clarification)
        const review = outcome.clarification.backtestReview
        if (review) {
          rememberReviewReference(review)
          setJourneyHistory(current => current.map(item => item.id === review.runId
            ? { ...item, review } : item))
        }
        setClarificationTarget({
          draftId: outcome.draftId,
          revision: outcome.revision,
          originalRequest: compileRequestFor(variables),
          executionContext: outcome.executionSettings
            ?? (outcome.isStrategyEdit ? variables.executionContext : undefined),
        })
        setClarificationMessages(editMessages)
        setClarificationSuggestions([])
        setClarificationPrompt(outcome.assistantMessage ?? clarificationMessage(outcome.clarification))
        setClarificationData(outcome.data)
        setUtterance('')
        window.setTimeout(() => inputRef.current?.focus(), 0)
      } else {
        const nextDraft = cloneDraft(outcome.draft)
        // New servers return saved settings, including explicit fee edits.
        // Only legacy responses need the browser's previous configuration.
        if (outcome.executionSettings === undefined && outcome.isStrategyEdit && variables.executionContext) {
          nextDraft.execution = { ...nextDraft.execution, ...variables.executionContext }
        }
        setClarification(undefined)
        setClarificationTarget(undefined)
        setClarificationSuggestions([])
        setClarificationPrompt(undefined)
        setClarificationData(undefined)
        setClarificationMessages(outcome.assistantMessage?.trim()
          ? [...editMessages, { role: 'assistant', text: outcome.assistantMessage }]
          : editMessages)
        setDraft(nextDraft)
        setBaselineDraft(cloneDraft(nextDraft))
        recoverCapabilitiesAfterReady()
      }
    },
    onError: (error, variables) => {
      if (variables.dialogueProgress.signal?.aborted) return
      setDialogueRecovery(null)
      rerunAfterEdit.current = false
      refreshEditedRun.current = false
      // Failed generation must not discard the last editable draft. Context
      // expiry/staleness still requires its existing explicit recovery path.
      const previous = variables.previousUnrunDraft
      if (previous && !contextRecoveryMessage(error)) {
        setDraft(cloneDraft(previous.draft))
        setBaselineDraft(previous.baseline ? cloneDraft(previous.baseline) : undefined)
        setSubmittedText(previous.submittedText)
        setFromPanelEdit(previous.fromPanelEdit)
        setClarificationMessages(previous.messages)
        setReviewOpen(previous.reviewOpen)
      }
      if (isReviewContextError(error) && variables.relatedReview) {
        setReviewContextError({ runId: variables.relatedReview.runId, message: errorMessage(error) })
      }
      setUtterance(variables.text)
      window.setTimeout(() => inputRef.current?.focus(), 0)
    },
  })

  const answerMutation = useMutation({
    mutationFn: ({ answer, target, pendingClarification, relatedRunIds, relatedReview, relatedReviews,
      dialogueProgress: observer }: {
      answer: string
      inputText: string
      target: ClarificationTarget
      pendingClarification: Clarification
      dialogueProgress: DialogueProgressObserver
      relatedRunIds?: string[]
      relatedReview?: CompileRequest['relatedReview']
      relatedReviews?: CompileRequest['relatedReviews']
    }) => strategyApi.answerClarification({
      draftId: target.draftId,
      revision: target.revision,
      answer,
      executionSettings: target.executionContext,
      relatedRunIds,
      relatedReview,
      relatedReviews,
      originalRequest: target.originalRequest,
      clarification: pendingClarification,
      dialogueProgress: observer,
    }),
    onSuccess: (turn, variables) => {
      if (variables.dialogueProgress.signal?.aborted) return
      setDialogueRecovery(null)
      // A clarification can confirm the pending run or explicitly cancel it.
      // Use the resolved turn's intent, never an earlier slot's run flag.
      rerunAfterEdit.current = turn.outcome.status === 'compiled'
        && turn.outcome.runRequested === true
      refreshEditedRun.current = turn.outcome.status === 'compiled'
        && turn.outcome.runRequested === true && turn.outcome.refreshData === true
      setConversationTailDraftId(
        turn.outcome.status === 'compiled'
          ? turn.outcome.draft.id
          : turn.outcome.draftId,
      )
      setClarificationRequestFailed(false)
      const assistantText = clarificationAnswerMessage(turn.assistantMessage)
      setRunId(undefined)
      setStack([])
      setRunCommand(undefined)
      setView('chat')
      setDetailJourneyId(undefined)
      setReviewOpen(turn.outcome.status !== 'needs_clarification')
      if (turn.outcome.status === 'needs_clarification') {
        setDraft(undefined)
        setBaselineDraft(undefined)
        setClarification(turn.outcome.clarification)
        const review = turn.outcome.clarification.backtestReview
        if (review) {
          rememberReviewReference(review)
          setJourneyHistory(current => current.map(item => item.id === review.runId
            ? { ...item, review } : item))
        }
        setClarificationTarget({
          draftId: turn.outcome.draftId,
          revision: turn.outcome.revision,
          originalRequest: variables.target.originalRequest,
          executionContext: turn.outcome.executionSettings
            ?? (turn.outcome.isStrategyEdit ? variables.target.executionContext : undefined),
        })
        setClarificationSuggestions(turn.suggestions)
        setClarificationPrompt(assistantText)
        setClarificationData(turn.data)
      } else {
        const nextDraft = cloneDraft(turn.outcome.draft)
        if (turn.outcome.executionSettings === undefined
          && turn.outcome.isStrategyEdit && variables.target.executionContext) {
          nextDraft.execution = { ...nextDraft.execution, ...variables.target.executionContext }
        }
        const selectedProposal = variables.pendingClarification.ideaRoute?.proposals.find(
          (proposal) => proposal.id === variables.answer && proposal.pairing_reason
            && proposal.instrument_symbol === nextDraft.instrument.symbol,
        )
        const verifiedName = selectedProposal?.instrument_name?.trim()
        if (verifiedName && (!nextDraft.instrument.name.trim()
          || nextDraft.instrument.name === nextDraft.instrument.symbol)) {
          // READY may omit the paired proposal's name; preserve only this exact selection.
          nextDraft.instrument.name = verifiedName
        }
        setClarification(undefined)
        setClarificationTarget(undefined)
        setClarificationSuggestions([])
        setClarificationPrompt(undefined)
        setClarificationData(undefined)
        if (assistantText.trim() || turn.data) {
          setClarificationMessages((current) => [
            ...current,
            { role: 'assistant', text: assistantText, data: turn.data },
          ])
        }
        setDraft(nextDraft)
        setBaselineDraft(cloneDraft(nextDraft))
        recoverCapabilitiesAfterReady()
      }
      setUtterance('')
      window.setTimeout(() => inputRef.current?.focus(), 0)
    },
    onError: (error, variables) => {
      if (variables.dialogueProgress.signal?.aborted) return
      setDialogueRecovery(null)
      // A failed second-turn request must not leave the previous turn's cards
      // visible underneath a new error message. They belong to an older turn
      // and make a transport failure look like a fresh model recommendation.
      setClarificationRequestFailed(true)
      setClarificationSuggestions([])
      setClarificationPrompt(compileRecoveryMessage(error))
      setClarificationData(undefined)
      if (isReviewContextError(error) && variables.relatedReview) {
        setReviewContextError({ runId: variables.relatedReview.runId, message: errorMessage(error) })
      }
      setUtterance(variables.inputText)
      window.setTimeout(() => inputRef.current?.focus(), 0)
    },
  })

  const capabilitiesQuery = useQuery({
    queryKey: ['capabilities'],
    queryFn: systemApi.capabilities,
    staleTime: 30_000,
    retry: apiMode === 'live' ? 1 : false,
  })

  const recoverCapabilitiesAfterReady = () => {
    const state = queryClient.getQueryState(['capabilities'])
    if (state?.status !== 'error') return
    // Recovery only prepares the existing draft; it does not authorize a run.
    rerunAfterEdit.current = false
    refreshEditedRun.current = false
    if (state.fetchStatus === 'idle') {
      void queryClient.refetchQueries(
        { queryKey: ['capabilities'], exact: true, type: 'active' },
        { cancelRefetch: false },
      )
    }
  }

  const startMutation = useMutation({
    mutationFn: async ({ candidate, refreshData = false }: {
      candidate: StrategyDraft; refreshData?: boolean
    }) => {
      const dateValidation = validateBacktestDates(candidate.backtest.start, candidate.backtest.end)
      if (!dateValidation.valid) throw new Error(dateValidation.reason)
      const savedDraft = baselineDraft && sameExecutableDraft(candidate, baselineDraft)
        ? candidate
        : await strategyApi.revise(candidate)
      // 保存成功后即保留版本；创建任务失败时仍能用同一份参数重试。
      setDraft(savedDraft)
      setBaselineDraft(cloneDraft(savedDraft))
      const run = await backtestApi.create(savedDraft, { refreshData })
      return { savedDraft, run }
    },
    onSuccess: ({ savedDraft, run }) => {
      setDraft(savedDraft)
      setBaselineDraft(cloneDraft(savedDraft))
      queryClient.setQueryData(['backtest-run', run.id], run)
      setRunId(run.id)
      setStack([])
      if (currentDraftRef.current?.id === savedDraft.id && isMobileReview()) {
        setReviewOpen(false)
      }
    },
    onError: (_error, { candidate }) => {
      if (currentDraftRef.current?.id === candidate.id && isMobileReview()) {
        setReviewOpen(false)
      }
    },
  })

  const runQuery = useQuery({
    queryKey: ['backtest-run', runId],
    queryFn: () => backtestApi.get(runId as string),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const state = query.state.data?.state
      return state && terminalStates.has(state) ? false : 700
    },
    /*
     * 回测是后台任务，用户十有八九会切到别处等它跑完。
     * react-query 默认在页面不可见时暂停轮询，那样切回来之前任务看着就是卡死的——
     * 这是状态查询，不是省流量的列表刷新，必须在后台继续问。
     */
    refetchIntervalInBackground: true,
  })

  const cancelMutation = useMutation({
    mutationFn: () => backtestApi.cancel(runId as string),
    onSuccess: (run) => queryClient.setQueryData(['backtest-run', run.id], run),
    onSettled: () => {
      if (runId) void queryClient.invalidateQueries({ queryKey: ['backtest-run', runId] })
    },
  })

  const hasResult = runQuery.data?.state === 'succeeded'
  const summaryQuery = useQuery({
    queryKey: ['backtest-summary', runId],
    queryFn: () => backtestApi.summary(runId as string),
    enabled: Boolean(runId) && hasResult,
  })
  const seriesQuery = useQuery({
    queryKey: ['backtest-series', runId],
    queryFn: () => backtestApi.series(runId as string),
    enabled: Boolean(runId) && hasResult,
  })
  const activitiesQuery = useQuery({
    queryKey: ['backtest-activities', runId],
    queryFn: () => backtestApi.activities(runId as string),
    enabled: Boolean(runId) && hasResult,
  })

  const [reviewProgress, setReviewProgress] = useState<{
    runId: string; events: readonly DialogueProgressEvent[]
  }>()
  const [reviewRecovery, setReviewRecovery] = useState<{ runId: string; state: PreviewPollRecovery | null }>()
  const reviewProgressAbortRef = useRef<AbortController | null>(null)
  useEffect(() => () => reviewProgressAbortRef.current?.abort(), [])
  const reviewMutation = useMutation({
    mutationFn: (source: JourneySnapshot): Promise<BacktestReviewResponse> => {
      reviewProgressAbortRef.current?.abort()
      const controller = new AbortController()
      reviewProgressAbortRef.current = controller
      setReviewProgress({ runId: source.id, events: [] })
      setReviewRecovery(undefined)
      const generation = reviewGeneration.current
      return backtestApi.review(source.id, {
        signal: controller.signal,
        onProgress: (events) => {
          if (!controller.signal.aborted) setReviewProgress({ runId: source.id, events })
        },
        onRecovery: (state) => {
          if (!controller.signal.aborted) setReviewRecovery({ runId: source.id, state })
        },
      }, conversationReferences(source)).then(review => {
        if (controller.signal.aborted || generation !== reviewGeneration.current) {
          throw new DOMException('Discarded previous conversation response', 'AbortError')
        }
        return review
      })
    },
    onSuccess: (review) => {
      rememberReviewReference(review)
      setJourneyHistory((current) => current.map((item) => item.id === review.runId
        ? { ...item, review } : item))
      setReviewContextError((current) => current?.runId === review.runId ? undefined : current)
    },
  })
  const autoReviewedRunIds = useRef(new Set<string>())

  const optimizationMutation = useMutation({
    mutationFn: async ({ source, candidate }: {
      source: JourneySnapshot
      candidate: BacktestOptimizationCandidate
    }) => ({
      source,
      candidate,
      ...await backtestApi.createOptimization(candidate, source.draft),
    }),
    onSuccess: ({ source, candidate, draft: optimizedDraft, run }) => {
      setConversationTailDraftId(optimizedDraft.id)
      setJourneyHistory((current) => current.some((item) => item.id === source.id)
        ? current
        : [...current, source])
      setUtterance('')
      setSubmittedText(`回测优化方案「${candidate.title}」：${executableRuleText(optimizedDraft)}`)
      setFromPanelEdit(false)
      setDraft(optimizedDraft)
      setBaselineDraft(cloneDraft(optimizedDraft))
      setClarification(undefined)
      setClarificationTarget(undefined)
      setClarificationMessages([])
      setClarificationSuggestions([])
      setClarificationPrompt(undefined)
      setClarificationData(undefined)
      setClarificationRequestFailed(false)
      queryClient.setQueryData(['backtest-run', run.id], run)
      setRunId(run.id)
      setRunCommand('开始优化回测')
      setReportSnapshot(undefined)
      setStack([])
      setDetailJourneyId(undefined)
      setReviewOpen(false)
      setView('chat')
    },
  })

  const resumeStrategyMutation = useMutation({
    mutationFn: ({ source }: { source: JourneySnapshot; mode: 'stock' | 'rules' }) =>
      strategyApi.revise(source.draft, true),
    onSuccess: (saved, { mode }) => {
      setConversationTailDraftId(saved.id)
      setView('chat')
      setReviewOpen(false)
      setDetailJourneyId(undefined)
      rememberCurrentJourney()
      resetForEdit()
      setUtterance('')
      setStrategySlots({ draft: saved, mode })
    },
  })

  const resultError = summaryQuery.error ?? seriesQuery.error ?? activitiesQuery.error
  const resultLoading = hasResult
    && (summaryQuery.isLoading || seriesQuery.isLoading || activitiesQuery.isLoading)
  const resultReady = Boolean(summaryQuery.data && seriesQuery.data && activitiesQuery.data)
  const isJourneyLocked = compileMutation.isPending
    || isConnectionChecking
    || answerMutation.isPending
    || startMutation.isPending
    || optimizationMutation.isPending
    || resumeStrategyMutation.isPending
    || Boolean(
    runQuery.data && !terminalStates.has(runQuery.data.state),
  )
  const validation = draft ? draftValidation(draft) : { valid: false, reason: undefined }
  const uiStrategy = useMemo(() => draft ? toStrategySummary(draft) : undefined, [draft])
  const readyAssistantMessageIndex = draft && uiStrategy
    && clarificationMessages.at(-1)?.role === 'assistant' ? clarificationMessages.length - 1 : -1
  const readyAssistantMessage = clarificationMessages[readyAssistantMessageIndex]
  const capability = useMemo(() => draft
    ? assessStrategyCapabilities(draft, capabilitiesQuery.data, apiMode)
    : undefined, [capabilitiesQuery.data, draft])
  // A failed metadata refresh does not invalidate the last successful catalog.
  // The real backend preparation still decides whether this exact draft can run.
  const canPrepare = Boolean(draft && validation.valid && capability?.canRun)
  const preparationBody = useMemo(() => canPrepare && draft ? toLiveBacktestBody(draft) : undefined,
    [canPrepare, draft])
  const preparationQuery = useQuery({
    // Bind readiness to exactly the strategy and execution settings sent to a run.
    // React Query cancels the consumed signal when edits replace this request.
    queryKey: ['backtest-preparation', preparationBody],
    queryFn: async ({ signal }) => {
      if (!draft) throw new Error('没有待检查的策略。')
      if (apiMode === 'live') {
        await new Promise<void>((resolve, reject) => {
          const abort = () => {
            window.clearTimeout(timer)
            reject(signal.reason ?? new DOMException('Aborted', 'AbortError'))
          }
          const timer = window.setTimeout(() => {
            signal.removeEventListener('abort', abort)
            resolve()
          }, 250)
          if (signal.aborted) abort()
          else signal.addEventListener('abort', abort, { once: true })
        })
      }
      return backtestApi.prepare(draft, signal)
    },
    enabled: canPrepare,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const retryConnection = async () => {
    const source = draft
    if (!source || isJourneyLocked || isConnectionChecking) return
    rerunAfterEdit.current = false
    refreshEditedRun.current = false
    setIsConnectionChecking(true)
    try {
      const preparedKey = ['backtest-preparation', toLiveBacktestBody(source)]
      await queryClient.invalidateQueries({ queryKey: preparedKey, exact: true, refetchType: 'none' })
      const result = await capabilitiesQuery.refetch({ cancelRefetch: false })
      if (currentDraftRef.current !== source
        || !assessStrategyCapabilities(source, result.data, apiMode).canRun) return
      // If newly available metadata just enabled preparation, reuse that fetch.
      // Otherwise refresh the existing failed preparation without changing inputs.
      await queryClient.refetchQueries(
        { queryKey: preparedKey, exact: true, type: 'active' },
        { cancelRefetch: false },
      )
    } finally {
      setIsConnectionChecking(false)
    }
  }
  const metrics = useMemo(() => summaryQuery.data ? toBacktestMetrics(summaryQuery.data) : undefined,
    [summaryQuery.data])
  const series = useMemo(() => toSeries(seriesQuery.data ?? []), [seriesQuery.data])
  const trades = useMemo(() => toTradeRows(activitiesQuery.data ?? []), [activitiesQuery.data])
  const marks = useMemo(() => toChartMarks(series, activitiesQuery.data ?? []), [series, activitiesQuery.data])
  const evidence = useMemo(() => summaryQuery.data ? toRunEvidence(summaryQuery.data) : undefined,
    [summaryQuery.data])
  const canStart = Boolean(canPrepare && preparationQuery.data?.ready
    && !preparationQuery.isFetching && !preparationQuery.isError)
  const startEditedRun = startMutation.mutate
  useEffect(() => {
    if (!rerunAfterEdit.current || !draft || !canStart || isJourneyLocked
      || compileMutation.isPending || clarification || runId) return
    rerunAfterEdit.current = false
    setRunCommand('按新条件重新回测')
    setReviewOpen(false)
    const refreshData = refreshEditedRun.current
    refreshEditedRun.current = false
    startEditedRun({ candidate: draft, refreshData })
  }, [draft, canStart, isJourneyLocked, compileMutation.isPending, clarification, runId, startEditedRun])
  const capabilitiesConnectionFailed = errorCode(capabilitiesQuery.error) === 'api_network_unavailable'
  const capabilitiesFailureReason = capabilitiesConnectionFailed
    ? '本次查询遇到网络异常，暂未确认回测准备状态。'
    : `${errorMessage(capabilitiesQuery.error)}${capabilitiesQuery.error instanceof ApiError
      && capabilitiesQuery.error.problem.status >= 400
      ? `（HTTP ${capabilitiesQuery.error.problem.status}）` : ''} 可重新读取能力。`
  const startDisabledReason = validation.reason
    ?? (capabilitiesQuery.isError && !capabilitiesQuery.data
      ? `暂未取得回测准备状态，策略和参数已保留；${capabilitiesFailureReason}`
      : !capability?.canRun
        ? capability?.reason ?? (capabilitiesQuery.isLoading ? '正在确认回测准备状态。' : undefined)
        : preparationQuery.isError ? draft && preparationQuery.error instanceof ApiError
          ? backtestPreparationFailureReason(draft, preparationQuery.error.problem)
          : errorMessage(preparationQuery.error)
          : !canStart ? '正在检查当前股票、回测区间和指标数据，检查完成后可开始回测。' : undefined)
  const startBlockedLabel = validation.reason ? undefined
    : capabilitiesQuery.isError && !capabilitiesQuery.data
      ? capabilitiesConnectionFailed ? '等待连接恢复' : '能力说明暂不可用'
      : capabilitiesQuery.data && !capability?.canRun
        ? capabilitiesQuery.data.backtest_execution_available ? '暂不支持回测' : '暂未准备好'
        : preparationQuery.isError ? '暂时无法回测' : undefined

  const currentSnapshot = useMemo<JourneySnapshot | undefined>(() => {
    if (
      !runId
      || !submittedText
      || !draft
      || !uiStrategy
      || !metrics
      || !evidence
      || !resultReady
      || !activitiesQuery.data
    ) return undefined
    return {
      id: runId,
      utterance: submittedText,
      fromPanelEdit,
      draft: cloneDraft(draft),
      instrument: toUiInstrument(draft),
      strategy: uiStrategy,
      metrics,
      series,
      marks,
      trades,
      evidence,
      activities: activitiesQuery.data,
      clarificationMessages,
      review: reviewMutation.data?.runId === runId ? reviewMutation.data : undefined,
    }
  }, [
    activitiesQuery.data,
    clarificationMessages,
    draft,
    evidence,
    fromPanelEdit,
    marks,
    metrics,
    resultReady,
    runId,
    series,
    submittedText,
    trades,
    uiStrategy,
    reviewMutation.data,
  ])

  const conversationReferences = (source?: JourneySnapshot): {
    relatedRunIds: string[]; relatedReviews: NonNullable<CompileRequest['relatedReviews']>
  } => {
    let relatedRunIds = [...new Set([
      ...journeyHistory.map(item => item.id),
      ...(currentSnapshot ? [currentSnapshot.id] : []),
      ...(source ? [source.id] : []),
    ])].slice(-20)
    // A manually opened older report remains the source of its own review.
    if (source && !relatedRunIds.includes(source.id)) {
      relatedRunIds = [source.id, ...relatedRunIds.slice(-19)]
    }
    return {
      relatedRunIds,
      relatedReviews: exposedReviewReferences.current
        .filter(reference => relatedRunIds.includes(reference.runId)),
    }
  }

  // Show the finished report immediately; request the real model independently.
  // Once per new run, never on history navigation or repeated status polling.
  const requestReview = reviewMutation.mutate
  useEffect(() => {
    if (apiMode !== 'live' || !currentSnapshot || reviewMutation.isPending
      || autoReviewedRunIds.current.has(currentSnapshot.id)) return
    autoReviewedRunIds.current.add(currentSnapshot.id)
    requestReview(currentSnapshot)
  }, [currentSnapshot, requestReview, reviewMutation.isPending])

  const storeCompletedReports = (reports: CompletedReportSnapshot[]) => {
    if (!reports.length) return
    void persistCompletedReports(apiMode, recentBacktestsRef.current.reports, reports).then(next => {
      recentBacktestsRef.current = next
      setRecentBacktests(next)
    }).catch(() => {
      setRecentBacktests(current => ({ ...current, notice: '本机报告保存失败，当前报告仍可查看。' }))
    })
  }
  useEffect(() => {
    storeCompletedReports([...journeyHistory, ...(currentSnapshot ? [currentSnapshot] : [])])
  }, [currentSnapshot, journeyHistory])

  const rememberCurrentJourney = () => {
    if (!currentSnapshot) return
    setJourneyHistory((current) => current.some((item) => item.id === currentSnapshot.id)
      ? current
      : [...current, currentSnapshot])
  }

  const resetForEdit = () => {
    returnToCandidateBatch.current = null
    dialogueProgressAbortRef.current?.abort()
    dialogueProgressAbortRef.current = null
    setDialogueProgress([])
    setDialogueRecovery(null)
    rerunAfterEdit.current = false
    refreshEditedRun.current = false
    setStrategySlots(undefined)
    setDraft(undefined)
    setBaselineDraft(undefined)
    setClarification(undefined)
    setClarificationTarget(undefined)
    setClarificationMessages([])
    setClarificationSuggestions([])
    setClarificationPrompt(undefined)
    setClarificationData(undefined)
    setClarificationRequestFailed(false)
    setRunId(undefined)
    setRunCommand(undefined)
    setSubmittedText(undefined)
    setFromPanelEdit(false)
    setReportSnapshot(undefined)
    setStack([])
    compileMutation.reset()
    answerMutation.reset()
    startMutation.reset()
    optimizationMutation.reset()
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  const startNewConversation = () => {
    selectedExample.current = undefined
    storeCompletedReports([...journeyHistory, ...(currentSnapshot ? [currentSnapshot] : [])])
    reviewGeneration.current += 1
    reviewProgressAbortRef.current?.abort()
    reviewProgressAbortRef.current = null
    reviewMutation.reset()
    resumeStrategyMutation.reset()
    cancelMutation.reset()
    setReviewProgress(undefined)
    setReviewRecovery(undefined)
    currentDraftRef.current = undefined
    exposedReviewReferences.current = []
    setConversationTailDraftId(undefined)
    setReviewContextError(undefined)
    setJourneyHistory([])
    setUtterance('')
    setView('chat')
    setReviewOpen(false)
    setDetailJourneyId(undefined)
    setHistoryOpen(false)
    setShowScrollToBottom(false)
    resetForEdit()
  }

  const submitText = (
    text: string,
    options: {
      instrumentOverride?: ApiInstrument; proposalId?: string
      editCurrentStrategy?: boolean; rerun?: boolean
    } = {},
  ) => {
    const normalized = text.trim()
    if (!normalized || isJourneyLocked) return
    rerunAfterEdit.current = Boolean(options.rerun)
    refreshEditedRun.current = false
    // Send references only; the server loads the verified report facts itself.
    const { relatedRunIds, relatedReviews } = conversationReferences()
    const displayedReview = clarification?.backtestReview
      ?? [...journeyHistory, ...(currentSnapshot ? [currentSnapshot] : [])]
      .reverse().filter(snapshot => relatedRunIds.includes(snapshot.id))
      .map(snapshot => snapshot.review ?? (reviewMutation.data?.runId === snapshot.id
        ? reviewMutation.data : undefined)).find(Boolean)
    const relatedReview = displayedReview && relatedRunIds.includes(displayedReview.runId) ? {
      runId: displayedReview.runId, responseHash: displayedReview.modelProvenance.responseHash,
    } : undefined
    setStrategySlots(undefined)
    if (clarification && clarificationTarget && !options.instrumentOverride) {
      const prompt = clarificationPrompt ?? clarificationMessage(clarification)
      setClarificationMessages((current) => [
        ...current,
        { role: 'assistant', text: prompt, data: clarificationData },
        { role: 'user', text: normalized },
      ])
      setClarificationPrompt(undefined)
      setClarificationData(undefined)
      setClarificationSuggestions([])
      setClarificationRequestFailed(false)
      setUtterance('')
      setDraft(undefined)
      setBaselineDraft(undefined)
      setRunId(undefined)
      setRunCommand(undefined)
      setReportSnapshot(undefined)
      setStack([])
      startMutation.reset()
      answerMutation.mutate({
        dialogueProgress: beginDialogueProgress(),
        answer: options.proposalId ?? normalized,
        inputText: normalized,
        target: clarificationTarget,
        pendingClarification: clarification,
        relatedRunIds,
        relatedReview,
        relatedReviews,
      })
      return
    }
    rememberCurrentJourney()
    setUtterance('')
    setSubmittedText(normalized)
    setFromPanelEdit(false)
    setDraft(undefined)
    setBaselineDraft(undefined)
    setClarification(undefined)
    setClarificationTarget(undefined)
    setClarificationMessages([])
    setClarificationSuggestions([])
    setClarificationPrompt(undefined)
    setClarificationData(undefined)
    setRunId(undefined)
    setRunCommand(undefined)
    setReportSnapshot(undefined)
    setStack([])
    answerMutation.reset()
    startMutation.reset()
    optimizationMutation.reset()
    compileMutation.mutate({
      dialogueProgress: beginDialogueProgress(),
      previousUnrunDraft: draft && !runId ? {
        draft: cloneDraft(draft),
        baseline: baselineDraft ? cloneDraft(baselineDraft) : undefined,
        submittedText,
        fromPanelEdit,
        messages: [...clarificationMessages],
        reviewOpen,
      } : undefined,
      text: normalized,
      instrumentOverride: options.instrumentOverride,
      parentDraftId: conversationTailDraftId,
      editCurrentStrategy: options.editCurrentStrategy,
      executionContext: strategySlots?.draft.execution ?? draft?.execution,
      relatedRunIds,
      relatedReview,
      relatedReviews,
    })
  }

  const analyzeCurrentInstrument = (selected: ApiInstrument) => {
    const identity = selected.name === selected.symbol
      ? selected.symbol
      : `${selected.name}（${selected.symbol}）`
    submitText(
      `分析${identity}的相关公开信息，给我几个可回测策略`,
      { instrumentOverride: selected },
    )
  }

  const submitClarificationChoice = (choice: Clarification['choices'][number]) => {
    const suggested = choice.suggestedUtterance ?? choice.label
    if (clarification?.id === 'strategy_edit_clarification') {
      // These candidates finish the pending edit. Recompiling with a new
      // instrument context would drop its exact parent revision and intent.
      submitText(suggested)
      return
    }
    const routeSymbol = clarification?.ideaRoute?.asset_mapping.instrument_symbol ?? undefined
    const targetInstrument = clarificationTarget?.originalRequest.instrument
    const targetIsGrounded = clarificationTarget?.originalRequest.instrumentContextSource !== 'standalone_default'
    const groundedInstrument = choice.instrumentName
      ?? choice.instrumentSymbol
      ?? (routeSymbol && targetInstrument?.symbol === routeSymbol ? targetInstrument.name : routeSymbol)
      ?? (targetIsGrounded ? targetInstrument?.name : undefined)
    const answer = groundedInstrument && !suggested.includes(groundedInstrument)
      ? `${groundedInstrument}${suggested}`
      : suggested
    const proposalInstrument = choice.instrumentSymbol
      ? toAshareInstrument(choice.instrumentSymbol, choice.instrumentName)
      : null
    const targetAlreadyGrounded = Boolean(
      proposalInstrument
      && targetIsGrounded
      && targetInstrument?.symbol === proposalInstrument.symbol,
    )
    const routeAlreadyGrounded = Boolean(
      proposalInstrument
      && routeSymbol === proposalInstrument.symbol,
    )
    submitText(
      answer,
      proposalInstrument && !targetAlreadyGrounded && !routeAlreadyGrounded
        ? { instrumentOverride: proposalInstrument }
        : undefined,
    )
  }

  const submitClarificationSuggestion = (suggestion: ClarificationSuggestion) => {
    const routeSymbol = clarification?.ideaRoute?.asset_mapping.instrument_symbol ?? undefined
    const targetInstrument = clarificationTarget?.originalRequest.instrument
    const targetIsGrounded = clarificationTarget?.originalRequest.instrumentContextSource !== 'standalone_default'
    const groundedInstrument = routeSymbol && targetInstrument?.symbol === routeSymbol
      ? targetInstrument.name
      : routeSymbol ?? (targetIsGrounded ? targetInstrument?.name : undefined)
    const answer = groundedInstrument && !suggestion.preview.includes(groundedInstrument)
      ? `${groundedInstrument}${suggestion.preview}`
      : suggestion.preview
    submitText(answer)
  }

  const submitIdeaProposal = (proposalId: string) => {
    const proposal = clarification?.ideaRoute?.proposals.find((item) => item.id === proposalId)
    if (proposal && isCompleteIdeaProposal(proposal) && clarificationTarget) {
      const batch = { clarification, clarificationTarget, clarificationMessages,
        clarificationPrompt, clarificationData, clarificationSuggestions, submittedText }
      returnToCandidateBatch.current = () => {
        resetForEdit()
        setClarification(batch.clarification)
        setClarificationTarget(batch.clarificationTarget)
        setClarificationMessages(batch.clarificationMessages)
        setClarificationPrompt(batch.clarificationPrompt)
        setClarificationData(batch.clarificationData)
        setClarificationSuggestions(batch.clarificationSuggestions)
        setSubmittedText(batch.submittedText)
        setReviewOpen(false)
      }
      const displayText = ideaProposalCards(
        clarification, clarificationTarget, clarificationSuggestions, capabilitiesQuery.data,
      ).find((card) => card.id === proposal.id)?.title
        || proposal.title.trim() || proposal.suggested_utterance
      submitText(displayText, {
        proposalId: proposal.id,
      })
      return
    }
    const choice = clarification?.choices.find((item) => item.id === proposalId)
    if (choice) {
      submitClarificationChoice(choice)
      return
    }
    const suggestion = clarificationSuggestions.find((item) => item.id === proposalId)
    if (suggestion) {
      submitClarificationSuggestion(suggestion)
      return
    }
    if (proposal && isCompleteIdeaProposal(proposal)) {
      submitText(proposal.suggested_utterance)
    }
  }

  const handleDraftChange = (nextDraft: StrategyDraft) => {
    rememberCurrentJourney()
    // A completed reply belongs to its archived run, not the newly edited draft.
    if (currentSnapshot) {
      setClarificationMessages([])
      setFromPanelEdit(true)
      dialogueProgressAbortRef.current?.abort()
      setDialogueProgress([])
    }
    setDraft(nextDraft)
    setRunId(undefined)
    setRunCommand(undefined)
  }

  const saveInlineStock = async (candidate: ApiInstrument, signal: AbortSignal) => {
    const source = draft
    if (!source || isJourneyLocked || signal.aborted) return
    const edited = cloneDraft(source)
    edited.instrument = { ...candidate }
    edited.strategySpec = { ...edited.strategySpec,
      instrument: { ...edited.strategySpec.instrument, symbol: candidate.symbol } }
    const saved = await strategyApi.revise(edited, false, signal)
    // A new conversation or any intervening draft edit owns the current UI.
    if (signal.aborted || currentDraftRef.current !== source) return
    rerunAfterEdit.current = false
    refreshEditedRun.current = false
    handleDraftChange(saved)
    setBaselineDraft(cloneDraft(saved))
    setConversationTailDraftId(saved.id)
  }

  /**
   * 看报告 = 切到详情态的报告分区。
   * 报告是这次回测的产物，它的位置在详情里；对话只负责记录「跑过这一次」，
   * 不再在对话里展开第二份同样的报告。
   */
  const openReportDetail = (snapshot: JourneySnapshot | undefined) => {
    if (!snapshot) return
    setReportSnapshot(snapshot)
    setDetailJourneyId(journeyHistory.some((item) => item.id === snapshot.id) ? snapshot.id : undefined)
    setDetailTab('report')
    setView('detail')
  }
  const openRecentReport = (snapshot: CompletedReportSnapshot) => {
    setReportSnapshot(historyJourney(snapshot))
    setDetailJourneyId(snapshot.id)
    setDetailTab('report')
    setView('detail')
    setHistoryOpen(false)
  }
  const detailSnapshot = detailJourneyId
    ? journeyHistory.find((item) => item.id === detailJourneyId)
      ?? recentBacktests.reports.map(historyJourney).find(item => item.id === detailJourneyId)
      ?? (reportSnapshot?.id === detailJourneyId ? reportSnapshot : undefined)
    : currentSnapshot
  const isReadOnlyHistory = detailJourneyId !== undefined
  const isGalleryReport = Boolean(detailSnapshot && detailSnapshot.id === galleryReportId)
  const detailReview = detailSnapshot?.review ?? (detailSnapshot && reviewMutation.data?.runId === detailSnapshot.id
    ? reviewMutation.data
    : undefined)
  const detailReviewIsActive = reviewMutation.variables?.id === detailSnapshot?.id
  const detailOptimizationIsActive = optimizationMutation.variables?.source.id === detailSnapshot?.id

  /** 成交规则仍用二级页；委托行只与报告图表联动。 */
  const reportBodyProps = (snapshot: JourneySnapshot) => ({
    metrics: snapshot.metrics,
    series: snapshot.series,
    marks: snapshot.marks,
    trades: snapshot.trades,
    evidence: snapshot.evidence,
    onOpenExecution: () => { setReportSnapshot(snapshot); openExecutionDetails() },
    mode: apiMode as 'mock' | 'live',
  })

  const openExecutionDetails = () => open('execution')

  const refreshResults = () => {
    void summaryQuery.refetch()
    void seriesQuery.refetch()
    void activitiesQuery.refetch()
  }

  let failure: FailureState | undefined
  if (runQuery.isError && !resultReady) {
    failure = failureFor(runQuery.error, 'run_read_failed', '任务状态读取失败', instrument, runId)
  } else if (cancelMutation.isError) {
    failure = failureFor(cancelMutation.error, 'cancel_failed', '取消请求没有完成', instrument, runId)
  } else if (runQuery.data?.state === 'failed') {
    failure = failureFor(
      new Error(backtestFailureMessage(runQuery.data)),
      'run_failed',
      '回测失败',
      instrument,
      runId,
      runQuery.data.error,
    )
  } else if (runQuery.data?.state === 'cancelled') {
    failure = {
      key: 'cancelled', status: 'implemented', title: '用户取消',
      reason: '任务已停止。修改规则或参数后可以重新提交。',
      actions: [{ label: '修改规则', action: 'edit_rules' }], runId,
    }
  } else if (resultError) {
    failure = failureFor(resultError, 'result_failed', '结果读取不完整', instrument, runId)
  }

  const handleFailureAction = (action: FailureState['actions'][number]['action']) => {
    if (action === 'read_results') {
      refreshResults()
      return
    }
    if (action === 'read_run') {
      cancelMutation.reset()
      void runQuery.refetch()
      return
    }
    if (action === 'use_example') {
      submitText(maCrossExample)
      return
    }
    // Editing and retrying both retain the original draft, baseline, and settings.
    // Only the explicit example action may compile a different strategy.
    if (!draft || startMutation.isPending) return
    setRunId(undefined)
    setRunCommand(undefined)
    setStack([])
    setView('chat')
    cancelMutation.reset()
    if (action === 'retry_run') {
      setRunCommand('重新回测')
      startMutation.mutate({ candidate: cloneDraft(draft), refreshData: true })
    } else {
      startMutation.reset()
      setReviewOpen(true)
      if (action === 'edit_range') openParams('range')
      if (action === 'edit_settings') openParams('more')
    }
  }

  const apiLabel = apiMode === 'mock' ? '界面预览' : '回测服务'
  const activeOverlay = stack.at(-1)
  const compileRecovery = compileMutation.isError
    ? compileRecoveryMessage(compileMutation.error)
    : undefined
  const activeIdeaProposalCards = ideaProposalCards(
    clarification,
    clarificationTarget,
    clarificationSuggestions,
    capabilitiesQuery.data,
  )
  const hasPairedProposals = activeIdeaProposalCards.some((proposal) => proposal.paired)
  const hasConfirmedIdeaStock = hasConfirmedIdeaInstrument(clarification)
  const instrumentSuggestions = hasConfirmedIdeaStock ? [] : (clarification?.instrumentSuggestions?.length
    ? clarification.instrumentSuggestions
    : clarification?.instrumentSuggestion ? [clarification.instrumentSuggestion] : [])
    .filter((item, index, items) => items.findIndex(other => other.symbol === item.symbol) === index)
    .slice(0, 3)
  const pendingStrategyDirection = dialogueProgress
    .filter((event) => event.stage === 'strategy_direction').at(-1)?.message
  const pendingProcessingEvents = dialogueProgress
    .filter((event) => event.stage !== 'strategy_direction')

  useEffect(() => {
    if (!instrumentContextError) return
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }, [instrumentContextError])

  useEffect(() => {
    const node = scrollRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
      setShowScrollToBottom(false)
    }
  }, [
    clarification,
    clarificationMessages.length,
    clarificationPrompt,
    answerMutation.isPending,
    compileMutation.isPending,
    draft,
    journeyHistory.length,
    resultReady,
    runQuery.data?.state,
    startMutation.isError,
    submittedText,
  ])

  /*
   * 回测跑完，落点是策略详情：这时候人要看的是结果，不是再回对话里找那张卡。
   *
   * 这一步只能写成 effect：结果就绪是三个查询各自返回后才成立的派生状态，
   * react-query v5 的查询没有 onSuccess 回调可挂，没有事件可以承接它。
   * 识别成功那一次切版式已经放进 mutation 的 onSuccess 了，不走 effect。
   */
  const settledRunId = resultReady ? runId : undefined
  useEffect(() => {
    if (!settledRunId) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 见上：异步结果就绪没有对应事件
    setDetailJourneyId(undefined)
    setDetailTab('report')
    setView('detail')
  }, [settledRunId])

  const latestJourney = journeyHistory.at(-1)
  const currentConversationTitle = draft && uiStrategy
    ? `${draft.instrument.name} · ${strategyTitle(toUiInstrument(draft), uiStrategy)}`
    : submittedText?.trim() || utterance.trim()
      || (latestJourney ? `${latestJourney.instrument.name} · ${strategyTitle(latestJourney.instrument, latestJourney.strategy)}` : '')
  const currentConversationRunIds = new Set([
    ...journeyHistory.map(item => item.id), ...(runId ? [runId] : []),
  ])
  const archivedReports = [...recentBacktests.reports].reverse()
    .filter(item => !currentConversationRunIds.has(item.id))

  return (
    <div className="app" data-api-mode={apiMode} data-has-overlay={stack.length > 0 ? 'true' : 'false'}>
      <div className="screens">
        <section className="page base" id="pg-chat" aria-hidden={Boolean(activeOverlay)} inert={Boolean(activeOverlay)}
          data-cold={!submittedText && journeyHistory.length === 0 ? 'true' : 'false'}
          data-view={view}
          data-review={view === 'chat' && reviewOpen && draft && uiStrategy ? 'true' : 'false'}>
          <div className="workspace" ref={workspaceRef}>
            {/* 分隔条绝对定位、不占 grid 区域，见 ColumnResizer 里的说明 */}
            <ColumnResizer side="rail" target={workspaceRef} />
            <ColumnResizer side="review" target={workspaceRef} />
          {historyOpen && <button type="button" className="rail-backdrop" tabIndex={-1} aria-label="关闭导航菜单" onClick={() => setHistoryOpen(false)} />}
          <aside ref={railRef} className="rail" aria-label="策略与历史" data-history-open={historyOpen}
            role={historyOpen ? 'dialog' : undefined} aria-modal={historyOpen || undefined}>
            <div className="rail-new-row">
            <button type="button" className="rail-history-toggle" aria-expanded={historyOpen}
              aria-label={historyOpen ? '收起导航菜单' : '打开导航菜单'}
              aria-controls="strategy-history" onClick={() => setHistoryOpen(open => !open)}>
              <svg width="20" height="20" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d={historyOpen ? 'm4 4 10 10M14 4 4 14' : 'M3 4h12M3 9h12M3 14h12'} stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <button type="button" className="rail-new" onClick={startNewConversation}
              disabled={isJourneyLocked}>
              <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M10.6 2.9H4.2A1.7 1.7 0 0 0 2.5 4.6v9.2a1.7 1.7 0 0 0 1.7 1.7h9.2a1.7 1.7 0 0 0 1.7-1.7V7.4"
                  stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                <path d="m12.4 2.2 3.4 3.4-4.6 1.2 1.2-4.6Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
              </svg>
              新建策略
            </button>
            </div>
            <button type="button" className="rail-gallery" aria-current={view === 'gallery' ? 'page' : undefined}
              disabled={isJourneyLocked} onClick={() => { setGalleryEntryKey(key => key + 1); setView('gallery'); setHistoryOpen(false) }}>
              <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M2.5 2.5h5v5h-5zM10.5 2.5h5v5h-5zM2.5 10.5h5v5h-5zM10.5 10.5h5v5h-5z"
                  stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
              </svg>
              策略广场
            </button>
            <h2 className="rail-sect">
              <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M2.5 8a6.5 6.5 0 1 1 1.7 5.4M2.5 3.5V8H7M9 5v4l2.5 1.5"
                  stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              最近
            </h2>
            <div className="rail-content" id="strategy-history">
              <nav className="rail-list" aria-label="最近记录">
                {currentConversationTitle ? <button type="button" data-current-conversation="true"
                  className={`rail-item${view !== 'gallery' && (view === 'chat' || !detailJourneyId || currentConversationRunIds.has(detailJourneyId)) ? ' is-active' : ''}`}
                  title={currentConversationTitle} aria-label={`${currentConversationTitle}，当前`}
                  onClick={() => { setDetailJourneyId(undefined); setReportSnapshot(undefined);
                    setView('chat'); setHistoryOpen(false) }}>
                  <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                    <path d="M3 3h12v10H7l-4 3V3Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
                  </svg>
                  <span>{currentConversationTitle}</span><small>当前</small>
                </button> : null}
                {archivedReports.map(journey => {
                  const title = `${journey.instrument.name} · ${strategyTitle(journey.instrument, journey.strategy)}`
                  const fullTitle = `${title} · ${journey.draft.backtest.start} 至 ${journey.draft.backtest.end}`
                  return <button key={journey.id} type="button" data-history-id={journey.id}
                    className={`rail-item rail-report${view === 'detail' && detailJourneyId === journey.id ? ' is-active' : ''}`}
                    title={fullTitle} aria-label={fullTitle}
                    onClick={() => openRecentReport(journey)}>
                    <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                      <path d="M4 2h7l3 3v11H4V2Zm7 0v3h3M7 8h4M7 11h4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                    <span>{title}</span>
                  </button>
                })}
              </nav>
            </div>
            {recentBacktests.notice ? <p className="rail-storage-notice" role="status">{recentBacktests.notice}</p> : null}
            <UsageGuide />
          </aside>

          <StrategyGallery active={view === 'gallery'} entryKey={galleryEntryKey} disabled={isJourneyLocked} onSearch={instrumentApi.search}
            onOpenReport={snapshot => { setGalleryReportId(snapshot.id); openRecentReport(snapshot) }}
            onUse={example => {
              if (isJourneyLocked) return
              startNewConversation()
              setSubmittedText(example.utterance)
              compileMutation.mutate({
                text: example.utterance,
                instrumentOverride: example.instrument,
                dialogueProgress: beginDialogueProgress(),
                relatedRunIds: [], relatedReviews: [],
              })
            }} />

          <div className="scroll" ref={scrollRef} onScroll={(event) => {
            const node = event.currentTarget
            setShowScrollToBottom(node.scrollHeight - node.scrollTop - node.clientHeight > 80)
          }}>
            <div className="stream" aria-live="polite">
              <DayDivider>当前会话</DayDivider>
              {apiMode === 'mock' ? <Notice tone="info">界面演示模式：未连接真实模型与行情，回测结果为演示数据。</Notice> : null}

              {journeyHistory.map((journey) => (
                <Fragment key={journey.id}>
                  {!journey.fromPanelEdit
                    ? <Turn id={`journey-${journey.id}`} mine><Bubble>{journey.utterance}</Bubble></Turn>
                    : null}
                  {journey.clarificationMessages.map((message, index) => (
                    <Turn key={`${journey.id}-clarification-${index}`} mine={message.role === 'user'}>
                      {message.role === 'user'
                        ? <Bubble>{message.text}</Bubble>
                        : <Say>{message.text}</Say>}
                    </Turn>
                  ))}
                  <Turn id={journey.fromPanelEdit ? `journey-${journey.id}` : undefined}>
                    <ThinkBlock
                      title="策略与回测摘要"
                      meta="历史记录"
                      lines={[
                        `买入：${summarizeRule(journey.strategy.entryRule)}`,
                        `卖出：${summarizeRule(journey.strategy.exitRule)}`,
                        '已提交并完成回测，这张卡片保留为不可编辑的记录',
                      ]}
                    />
                    <StrategyCard
                      instrument={journey.instrument}
                      strategy={journey.strategy}
                      onEditRow={() => undefined}
                      onOpenMore={() => undefined}
                      onRun={() => undefined}
                      settled="已完成回测"
                      executionSummary={summarizeExecution(journey.draft)}
                    />
                  </Turn>
                  {/* 历史回合里也保留那句「开始回测」，往回翻时这一步不会凭空消失 */}
                  <Turn mine><Bubble>{RUN_COMMAND}</Bubble></Turn>
                  <Turn>
                    <ResultCard
                      metrics={journey.metrics}
                      series={journey.series}
                      marks={journey.marks}
                      onOpenReport={() => openReportDetail(journey)}
                      historical
                    />
                  </Turn>
                </Fragment>
              ))}

              {journeyHistory.length > 0 && !submittedText ? <DayDivider>继续回测</DayDivider> : null}

              {!submittedText ? (
                <Turn>
                  <Say>
                    {instrumentContextError
                      ? '还没有识别到当前股票。你可以直接输入股票名称或 6 位代码，也可以连同买入和卖出条件一起告诉我。'
                      : strategySlots
                        ? '改一下下面的股票或买卖条件，再继续回测。'
                      : journeyHistory.length > 0
                        ? <>说出新的买卖规则，继续回测。</>
                        : <>今天，我们怎么交易？<br />从验证一个想法开始。</>}
                  </Say>
                  {/*
                    示例只在冷启动时出现：它的作用是告诉第一次来的人「一句话可以写成什么样」。
                    跑过一轮之后用户已经知道怎么写，再把同样两条推一遍既占地方，
                    也像是在暗示「你应该选我给的这几个」。
                  */}
                </Turn>
              ) : !fromPanelEdit ? <Turn mine><Bubble>{submittedText}</Bubble></Turn> : null}

              {clarificationMessages.map((message, index) => index === readyAssistantMessageIndex ? null : (
                <Turn key={`clarification-${index}`} mine={message.role === 'user'}>
                  {message.role === 'user'
                    ? <Bubble>{message.text}</Bubble>
                    : <Say>{message.text}</Say>}
                  {message.role === 'assistant' && message.data
                    ? <CurrentDataResult data={message.data} onAnalyze={analyzeCurrentInstrument} />
                    : null}
                </Turn>
              ))}

              {compileMutation.isPending ? (
                <Turn>
                  {pendingStrategyDirection ? <Say>{pendingStrategyDirection}</Say> : null}
                  <ThinkingStream
                    status={pendingStrategyDirection ? '正在准备组合' : '正在理解你的想法'}
                    summary={pendingProcessingEvents}
                    recovery={dialogueRecovery}
                  />
                </Turn>
              ) : null}

              {answerMutation.isPending ? (
                <Turn>
                  {pendingStrategyDirection ? <Say>{pendingStrategyDirection}</Say> : null}
                  <ThinkingStream
                    status={pendingStrategyDirection ? '正在准备组合' : '正在理解你的想法'}
                    summary={pendingProcessingEvents}
                    recovery={dialogueRecovery}
                  />
                </Turn>
              ) : null}

              {clarificationPrompt && !compileMutation.isPending && !answerMutation.isPending ? (
                <Turn>
                  <ModelReasoning events={dialogueProgress} failed={clarificationRequestFailed} />
                  <Say>{clarificationPrompt}</Say>
                  {!clarificationRequestFailed && clarification?.backtestReview ? (() => {
                    const review = clarification.backtestReview
                    const source = [...journeyHistory, ...(currentSnapshot ? [currentSnapshot] : [])]
                      .find(item => item.id === review.runId)
                    return <>
                      <Chips>{review.optimizationCandidates.slice(0, 3).map(candidate => (
                        <Chip key={candidate.id} disabled={isJourneyLocked || !source}
                          onClick={() => {
                            if (source) optimizationMutation.mutate({ source, candidate })
                          }}>用「{candidate.title}」再回测</Chip>
                      ))}</Chips>
                      {optimizationMutation.isError
                        ? <Say>{errorMessage(optimizationMutation.error)}</Say> : null}
                    </>
                  })() : null}
                  {!clarificationRequestFailed && instrumentSuggestions.length
                    && (!hasPairedProposals || clarification?.instrumentSuggestions?.length) ? (
                    <div className="stock-suggestions" aria-label="可选股票">
                      {instrumentSuggestions.some(item => item.source === 'eastmoney_mx_screener') ? (
                        <details className="idea-research stock-suggestions__evidence">
                          <summary>东方财富选股依据</summary>
                          {instrumentSuggestions.map(item => (
                            <p key={item.symbol}><strong>{item.name ?? item.symbol}</strong>：{item.evidence}</p>
                          ))}
                          <p className="stock-suggestions__time">取数时间：{instrumentSuggestions[0]?.retrieved_at}</p>
                        </details>
                      ) : null}
                      <Chips>
                        {instrumentSuggestions.map(item => (
                          <Chip key={item.symbol} disabled={isJourneyLocked}
                            onClick={() => {
                              const proposal = clarification?.ideaRoute?.proposals
                                .find(option => option.instrument_symbol === item.symbol)
                              if (proposal) submitIdeaProposal(proposal.id)
                              else submitText(`用${item.name ? `${item.name}（${item.symbol}）` : item.symbol}`)
                            }}>
                            {item.name ?? item.symbol}
                          </Chip>
                        ))}
                      </Chips>
                    </div>
                  ) : null}
                  {clarificationData ? (
                    <CurrentDataResult
                      data={clarificationData}
                      onAnalyze={analyzeCurrentInstrument}
                    />
                  ) : null}
                  {!clarificationRequestFailed && clarification?.provisionalDraft ? (() => {
                    const provisional = clarification.provisionalDraft
                    const trees = strategyRuleTrees(provisional)
                    const needsSemanticConfirmation = clarification.id === 'semantic_confirmation_required'
                    const needsRangeConfirmation = clarification.id === 'backtest_range_confirmation_required'
                    const executionAssessment = clarification.executionAssessment
                    return (
                      <div className="provisional-strategy" data-testid="provisional-strategy">
                        <h3 className="provisional-strategy__meta">
                          {needsRangeConfirmation ? '建议回测范围 · 等待接受' : executionAssessment ? '规则已识别 · 暂未执行回测' : needsSemanticConfirmation
                            ? '已识别部分 · 尚未完整实现'
                            : '按你的口语推测 · 等你确认'}
                        </h3>
                        {needsSemanticConfirmation || needsRangeConfirmation || executionAssessment ? (
                          <div className="provisional-strategy__rule">
                            <span>股票</span>
                            <div>{provisional.instrument.name === provisional.instrument.symbol
                              ? provisional.instrument.symbol
                              : `${provisional.instrument.name}（${provisional.instrument.symbol}）`}</div>
                          </div>
                        ) : null}
                        <div className="provisional-strategy__rule">
                          <span>买入</span><div>{summarizeRule(trees.entry)}</div>
                        </div>
                        <div className="provisional-strategy__rule">
                          <span>卖出</span><div>{summarizeRule(trees.exit)}</div>
                        </div>
                        {needsRangeConfirmation && provisional.strategySpec ? (
                          <div className="provisional-strategy__rule">
                            <span>建议区间</span><div>{provisional.strategySpec.backtest.start} 至 {provisional.strategySpec.backtest.end}</div>
                          </div>
                        ) : null}
                        <div className="provisional-strategy__note">
                          {executionAssessment
                            ? `待准备：${executionAssessment.missing.join('、')}。原规则已保留，可以继续修改。`
                            : clarification.provisionalNote ?? '这只是暂时理解，不会自动开始回测。'}
                        </div>
                      </div>
                    )
                  })() : null}
                  {!clarificationRequestFailed && clarification?.ideaRoute ? (
                    <>
                      {activeIdeaProposalCards.length > 0 && (!clarification.instrumentSuggestions?.length || hasConfirmedIdeaStock) ? (
                        <Proposals
                          items={activeIdeaProposalCards}
                          onPick={submitIdeaProposal}
                        />
                      ) : null}
                      <IdeaResearchEvidence clarification={clarification} />
                    </>
                  ) : !clarificationRequestFailed && clarificationSuggestions.length > 0 ? (
                    <Proposals
                      items={clarificationSuggestions.map((suggestion) => ({
                        id: suggestion.id,
                        title: suggestion.title,
                        detail: suggestion.preview,
                      }))}
                      onPick={(id) => {
                        const suggestion = clarificationSuggestions.find((item) => item.id === id)
                        if (suggestion) submitClarificationSuggestion(suggestion)
                      }}
                    />
                  ) : !clarificationRequestFailed
                    && clarification && visibleClarificationChoices(clarification).length ? (
                    <Proposals
                      items={visibleClarificationChoices(clarification).map((choice) => ({
                        id: choice.id,
                        title: choice.label,
                        detail: choice.description,
                      }))}
                      onPick={(id) => {
                        const choice = visibleClarificationChoices(clarification)
                          .find((item) => item.id === id)
                        if (choice) submitClarificationChoice(choice)
                      }}
                    />
                  ) : null}
                </Turn>
              ) : null}

              {draft && uiStrategy ? (
                <Turn>
                  <ModelReasoning events={dialogueProgress} />
                  {readyAssistantMessage ? <Say>{readyAssistantMessage.text}</Say> : null}
                  {readyAssistantMessage?.data
                    ? <CurrentDataResult data={readyAssistantMessage.data} onAnalyze={analyzeCurrentInstrument} />
                    : null}
                  {apiMode === 'mock' && draft.entry.conditions.some((condition) =>
                    condition.kind === 'event' && condition.documentText) ? (
                      <Notice tone="info">
                        界面预览只展示规则识别和卡片结构；没有读取年报正文，也没有计算词频。
                      </Notice>
                    ) : null}
                  <div className="preview-actions">
                    <button type="button" className="preview-card" onClick={() => setReviewOpen(true)}>
                      <span className="preview-name">{strategyTitle(toUiInstrument(draft), uiStrategy)}</span>
                      <span className="preview-action-label">
                        {isJourneyLocked ? '查看策略' : '查看并修改'}
                        <svg viewBox="0 0 18 18" aria-hidden="true">
                          <path d="m7 4 5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </span>
                    </button>
                    {returnToCandidateBatch.current ? <button type="button" className="preview-return"
                      disabled={isJourneyLocked} onClick={() => {
                        rememberCurrentJourney()
                        returnToCandidateBatch.current?.()
                      }}>
                      <span>返回其他方案</span>
                      <svg viewBox="0 0 18 18" aria-hidden="true">
                        <path d="m7 4 5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    </button> : null}
                  </div>
                </Turn>
              ) : null}

              {runCommand ? <Turn mine><Bubble>{runCommand}</Bubble></Turn> : null}

              {startMutation.isPending ? (
                <Turn>
                  <ThinkingStream
                    status="正在读取这段时间的行情，请稍候"
                  />
                </Turn>
              ) : null}

              {runQuery.data && !terminalStates.has(runQuery.data.state) ? (
                <Turn>
                  <RunningCard phase={runQuery.data.state} progressLabel={runQuery.data.progressLabel}
                    onCancel={() => cancelMutation.mutate()}
                    isCancelling={cancelMutation.isPending} isMock={apiMode === 'mock'} />
                </Turn>
              ) : null}

              {resultLoading ? (
                <Turn>
                  <ThinkingStream
                    status="正在整理回测结果"
                  />
                </Turn>
              ) : null}

              {draft && metrics && evidence && resultReady ? (
                <>
                  <Turn>
                    <ResultCard metrics={metrics} series={series} marks={marks}
                      onOpenReport={() => openReportDetail(currentSnapshot)} />
                  </Turn>
                </>
              ) : null}



              {startMutation.isError && draft && !reviewOpen ? (
                <Turn>
                  <div role="alert"><Say>{errorMessage(startMutation.error)}</Say></div>
                  <Chips>
                    <Chip disabled={isJourneyLocked} onClick={() => {
                      setRunCommand('重新提交回测')
                      setRunId(undefined)
                      startMutation.mutate({
                        candidate: draft,
                        refreshData: startMutation.variables?.refreshData ?? false,
                      })
                    }}>重试</Chip>
                    <Chip disabled={isJourneyLocked} onClick={() => setReviewOpen(true)}>审阅策略</Chip>
                  </Chips>
                </Turn>
              ) : null}

              {failure && !startMutation.isError ? (
                <Turn><FailureCard state={failure} onAction={handleFailureAction} /></Turn>
              ) : null}

              {compileRecovery ? <Turn><ModelReasoning events={dialogueProgress} failed /><Say>{compileRecovery}</Say></Turn> : null}
            </div>
          </div>

          {showScrollToBottom ? <button type="button" className="fab" aria-label="回到底部"
            onClick={() => {
              const node = scrollRef.current
              if (node) node.scrollTop = node.scrollHeight
              setShowScrollToBottom(false)
            }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M8 3v10M4 9l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button> : null}

          {/*
            底部只留输入区。买卖、语音、「+」、深度思考那一排能力胶囊、iOS home 指示条
            都是宿主外壳，不属于这个产品——留着只会让人以为这里能下单。
          */}
          {/*
            策略详情 —— 对应 Public 创建完 Agent 后落到的那一页：
            返回、大衬线标题、一句话描述、标签、运行信息条，然后是 tab 切换的正文。
            它和对话共用同一块工作区，只是 grid 把中间那栏收掉了。
          */}
          {detailSnapshot ? (
            <section className="detail" aria-label="策略详情">
              <div className="detail-inner">
                <div className="detail-top">
                  <button type="button" className="detail-back" aria-label={isGalleryReport ? '返回策略广场' : '回到对话'}
                    onClick={() => setView(isGalleryReport ? 'gallery' : 'chat')}>
                    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                      <path d="M12 4.5 6.5 10l5.5 5.5" stroke="currentColor" strokeWidth="1.6"
                        strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                    <span className="detail-back-label">{isGalleryReport ? '返回策略广场' : '返回对话'}</span>
                  </button>
                  <h1 className="detail-title">
                    {strategyTitle(detailSnapshot.instrument, detailSnapshot.strategy)}
                  </h1>
                  {!isReadOnlyHistory ? <button type="button" className="detail-edit"
                    onClick={() => { setView('chat'); setReviewOpen(true) }}>
                    编辑策略
                  </button> : null}
                </div>
                {/*
                  只留一行身份信息。买卖规则在「工作流」分区里逐条列着，数据区间既是
                  工作流的一个步骤、报告里也自带一行——头部再抄一遍就是三处同义重复。
                */}
                <p className="detail-meta">
                  <b>{detailSnapshot.instrument.name}</b>
                  <span className="num">{detailSnapshot.instrument.code}</span>
                  <em>{apiLabel}</em>
                  {isReadOnlyHistory ? <em>{isGalleryReport ? '样例回测 · 只读' : '只读历史'}</em> : null}
                </p>

                <div className="detail-tabs" role="tablist" aria-label="策略详情分区">
                  <button type="button" role="tab" id="detail-tab-flow"
                    aria-selected={detailTab === 'flow'} aria-controls="detail-panel-flow"
                    className={detailTab === 'flow' ? 'on' : undefined}
                    onClick={() => setDetailTab('flow')}>工作流</button>
                  <button type="button" role="tab" id="detail-tab-report"
                    aria-selected={detailTab === 'report'} aria-controls="detail-panel-report"
                    className={detailTab === 'report' ? 'on' : undefined}
                    onClick={() => setDetailTab('report')}>回测报告</button>
                </div>

                <div className="detail-panel" id="detail-panel-flow" role="tabpanel"
                  aria-labelledby="detail-tab-flow" hidden={detailTab !== 'flow'}>
                  <h2 className="detail-sub">这条策略怎么跑</h2>
                  <div className="erows erows--readonly">
                    {detailSnapshot.strategy.rows.map((row) => (
                      <div key={row.key} className={`erow${row.kind ? ` ${row.kind}` : ''} off`}>
                        <span className="lb">{row.label}</span>
                        <span className="val">
                          {row.value}
                          {row.sub ? <small>{row.sub}</small> : null}
                        </span>
                      </div>
                    ))}
                  </div>
                  <ExecutionEntry onOpen={() => {
                    setReportSnapshot(detailSnapshot)
                    openExecutionDetails()
                  }} />
                </div>

                <div className="detail-panel" id="detail-panel-report" role="tabpanel"
                  aria-labelledby="detail-tab-report" hidden={detailTab !== 'report'}>
                  <div id="pg-report">
                    <h2 className="sr-only">回测报告</h2>
                    <ReportBody {...reportBodyProps(detailSnapshot)} showExecutionEntry={false} />
                    {isReadOnlyHistory ? (detailReview || (detailReviewIsActive && (reviewMutation.isPending || reviewMutation.isError))
                      || reviewContextError?.runId === detailSnapshot.id) ? (
                      <section className="backtest-review" aria-label={detailReview ? '已保存的 AI 分析' : 'AI 分析与优化'}>
                        <h2>{detailReview ? '已保存的 AI 分析' : 'AI 分析与优化'}</h2>
                        {detailReviewIsActive && reviewMutation.isPending
                          ? <button type="button" disabled>AI 正在分析</button> : null}
                        {detailReview ? <><p>{detailReview.analysis}</p><p>{detailReview.conclusion}</p></> : null}
                        {(detailReviewIsActive && reviewMutation.isError) || reviewContextError?.runId === detailSnapshot.id ? (
                          <div className="backtest-review__error" role="alert">
                            <p>{detailReviewIsActive && reviewMutation.isError
                              ? errorMessage(reviewMutation.error) : reviewContextError?.message}</p>
                            <button type="button"
                              disabled={reviewMutation.isPending}
                              onClick={() => reviewMutation.mutate(detailSnapshot)}>
                              {detailReviewIsActive && reviewMutation.isPending ? '正在重新分析…' : '重试 AI 分析'}
                            </button>
                          </div>
                        ) : null}
                        {detailReview ? <h3>当时的优化建议</h3> : null}
                        {detailReview?.optimizationCandidates.map(candidate => (
                          <p key={candidate.id}><b>{candidate.title}</b><br />{candidate.suggestedUtterance}</p>
                        ))}
                      </section>
                    ) : null : <BacktestReview
                      review={detailReview}
                      describeCandidate={(candidate) => executableRuleText(
                        fromBacktestOptimizationCandidate(candidate, detailSnapshot.draft, capabilitiesQuery.data),
                      )}
                      progress={reviewProgress?.runId === detailSnapshot.id
                        ? reviewProgress.events : undefined}
                      recovery={reviewRecovery?.runId === detailSnapshot.id ? reviewRecovery.state : undefined}
                      isLoading={detailReviewIsActive && reviewMutation.isPending}
                      error={detailReviewIsActive && reviewMutation.isError
                        ? errorMessage(reviewMutation.error)
                        : reviewContextError?.runId === detailSnapshot.id
                          ? reviewContextError.message : undefined}
                      onRequest={() => reviewMutation.mutate(detailSnapshot)}
                      onChangeInstrument={() => resumeStrategyMutation.mutate({
                        source: detailSnapshot, mode: 'stock',
                      })}
                      onChangeRules={() => resumeStrategyMutation.mutate({
                        source: detailSnapshot, mode: 'rules',
                      })}
                      onRunCandidate={(candidate) => optimizationMutation.mutate({
                        source: detailSnapshot,
                        candidate,
                      })}
                      runningCandidateId={detailOptimizationIsActive && optimizationMutation.isPending
                        ? optimizationMutation.variables?.candidate.id
                        : undefined}
                      runError={detailOptimizationIsActive && optimizationMutation.isError
                        ? errorMessage(optimizationMutation.error)
                        : resumeStrategyMutation.isError ? errorMessage(resumeStrategyMutation.error)
                        : undefined}
                      candidateActionsDisabled={isJourneyLocked}
                    />}
                  </div>
                </div>


              </div>
            </section>
          ) : null}

          {/*
            策略审阅栏 —— 对应 Public 的「Review your Agent」。
            桌面端是常驻右栏，手机端塌成输入框上方的可折叠面板；同一份 DOM，
            靠 .workspace 的 grid-template-areas 换位置，没有为断点分叉的 JSX。
          */}
          {draft && uiStrategy ? (
            <aside className="review" aria-label="策略审阅">
              <button type="button" className="mobile-review-back" aria-label="返回对话"
                onClick={() => setReviewOpen(false)}>
                <BackIcon />
                <span>返回对话</span>
              </button>
              <div className="review-head">
                <span className="review-eyebrow">审阅你的策略</span>
                <span className="review-title">{strategyTitle(toUiInstrument(draft), uiStrategy)}</span>
                <button type="button" className="review-close" aria-label="收起策略审阅"
                  onClick={() => setReviewOpen(false)}>
                  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                    <path d="m4.6 4.6 8.8 8.8M13.4 4.6l-8.8 8.8" stroke="currentColor"
                      strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                </button>
              </div>
              <div className="review-body">
                <StrategyCard
                  instrument={toUiInstrument(draft)}
                  strategy={uiStrategy}
                  stockEditor={{ onSearch: instrumentApi.search, onSave: saveInlineStock }}
                  onEditRow={openParams}
                  onOpenMore={() => openParams('more')}
                  onRun={() => {
                    if (!canStart) return
                    setRunCommand(RUN_COMMAND)
                    startMutation.mutate({ candidate: draft })
                  }}
                  isStarting={startMutation.isPending}
                  isLocked={isJourneyLocked}
                  checkingSettings={preparationQuery.isFetching || isConnectionChecking
                    || (!capabilitiesQuery.data && capabilitiesQuery.isFetching)}
                  settled={runQuery.data?.state === 'succeeded' ? '已完成回测' : undefined}
                  editableAfterRun
                  canStart={canStart}
                  disabledReason={startDisabledReason}
                  blockedLabel={startBlockedLabel}
                  retryLabel={capabilitiesQuery.isError && !capabilitiesQuery.data
                    ? capabilitiesConnectionFailed ? '重新检查连接' : '重新读取能力'
                    : errorCode(preparationQuery.error) === 'api_network_unavailable'
                      ? '重新检查连接' : '重新检查数据'}
                  onRetryConnection={capabilitiesQuery.isError || preparationQuery.isError
                    ? () => { void retryConnection() } : undefined}
                  error={startMutation.isError && reviewOpen ? errorMessage(startMutation.error) : undefined}
                  executionSummary={summarizeExecution(draft)}
                />
              </div>
            </aside>
          ) : null}

          <div className="dock">
            {strategySlots ? (
              <StrategySlotComposer key={`${strategySlots.draft.id}@${strategySlots.draft.revision}`} {...strategySlots}
                disabled={isJourneyLocked || compileMutation.isPending}
                onSubmit={submitText}
                onClear={() => {
                  setStrategySlots(undefined)
                  setUtterance('')
                  window.setTimeout(() => inputRef.current?.focus(), 0)
                }} />
            ) : <form className="inputbar" onSubmit={(event) => {
              event.preventDefault()
              const example = selectedExample.current
              submitText(utterance, example?.utterance === utterance && example.instrument
                ? { instrumentOverride: example.instrument } : {})
              selectedExample.current = undefined
            }}>
              <input ref={inputRef} className="strategy-input" aria-label="交易规则" value={utterance}
                disabled={isJourneyLocked} onChange={(event) => {
                  selectedExample.current = undefined
                  setUtterance(event.target.value)
                }}
                placeholder={answerMutation.isError
                  ? compileFailurePlaceholder(answerMutation.error)
                  : clarification
                    ? clarificationPlaceholder(clarification)
                    : compileMutation.isError
                      ? compileFailurePlaceholder(compileMutation.error)
                      : instrumentContextError
                        ? '输入股票名称、买入和卖出条件'
                        : '说出什么时候买、什么时候卖'} />
              <button type="submit" className="send" aria-label="识别交易规则"
                disabled={isJourneyLocked || compileMutation.isPending || !utterance.trim()}>
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                  <path d="M9 14V4M4.6 8.4 9 4l4.4 4.4" stroke="currentColor" strokeWidth="1.8"
                    strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </form>}

            {/* 冷启动的示例句排在输入框下方，对应 Public 首屏输入框下那排分类 chip */}
            {!submittedText && !instrumentContextError && journeyHistory.length === 0 ? (
              <StrategyExamples inputHasText={Boolean(utterance.trim())} onChoose={example => {
                selectedExample.current = example
                setUtterance(example.utterance)
                inputRef.current?.focus()
              }} />
            ) : null}

            <p className="disclaimer">仅做历史回测，不构成投资建议</p>
          </div>

          {/* 冷启动时给下方留一段空白，让「标题 + 输入框」落在视觉中线偏上 */}
          <div className="coldpad" aria-hidden="true" />
          </div>
        </section>

        {draft ? (
          <ParamsScreen open={activeOverlay === 'params'} onBack={back} draft={draft}
            // Reuse the same verified search/save path as the review card.
            stockEditor={{ onSearch: instrumentApi.search, onSave: saveInlineStock }}
            onChange={handleDraftChange}
            onReset={() => baselineDraft && setDraft(cloneDraft(baselineDraft))}
            isLocked={isJourneyLocked} focus={paramsFocus} conditionPath={conditionPath} capabilities={capabilitiesQuery.data} />
        ) : null}
        {reportSnapshot ? (
          <ExecutionDetailsScreen open={activeOverlay === 'execution'} onBack={back}
            draft={reportSnapshot.draft} trades={reportSnapshot.trades}
            evidence={reportSnapshot.evidence} mode={apiMode} />
        ) : null}
      </div>
    </div>
  )
}
